"""Part 3 — write the analyst reasoning, one batched call per contested card.

Runs ONLY on cards that have ≥1 contested claim. Everything the model needs is
in the prompt: the card text, the flagged claims, and the already-retrieved
source snippets for those claims (from :mod:`evidence_store`). The model
rewrites just the contested passages — replacing bare ``[label](url)`` citations
with prose that shows what each source counted and which band the report stands
behind — and returns every other sentence verbatim.

Claims are grouped by location (the card's top-level content vs each named
sub-section) *inside* that single call, and the model returns its analyst notes
nested in the same place it rewrote the passage. So the notes are attributable
per sub-section without a second call: the pass writes them straight onto the
card under ``analyst_reasoning`` and returns the card.

This is the only place the strong model is used, and it is O(contested cards),
not O(citations).
"""

from __future__ import annotations

import copy
import json
import time
from typing import Any, Dict, List

from google import genai

from app.core.constants import (
    ANALYST_REASONING_EFFORT,
    ANALYST_REASONING_GEMINI_THINKING_LEVEL,
    ANALYST_REASONING_MODEL,
    GEMINI_ANALYST_REASONING_MODEL,
    GEMINI_API_KEY,
    SYNC_OPENAI_CLIENT,
)
from app.core.logging import setup_logging
from app.agent.analyst_reasoning.evidence_store import evidence_for_claim
from app.agent.analyst_reasoning.schemas import (
    GEMINI_REASONING_SCHEMA,
    REASONING_KEY,
    TOP_LEVEL,
    CardReasoningOutput,
    ClaimReasoning,
    ContestedClaim,
    EvidenceItem,
)
from app.observability.llm_response_logger import save_raw_llm_response, strip_json_code_fence

logger = setup_logging(__name__)


REASONING_PROMPT = """You are a research analyst finalising one card of a report.

Some claims in this card rest on an analytical judgement the reader currently cannot see —
publishers disagree, or a number was derived, or a figure was applied at a different scope.
Your job: rewrite ONLY those passages so the reasoning is visible, using the source
evidence provided. Leave every other sentence exactly as it is.

HOW TO REWRITE A CONTESTED PASSAGE
- Name the sources and what each one actually counted / measured. Use the evidence snippets;
  do not invent figures that aren't there.
- Say whether the gap is definitional (different scope) or genuine (real disagreement).
- State the working band the report stands behind and why it fits this report's scope.
- Keep the sources as inline markdown links [publisher](url) using the URLs given. A short
  3-column table (Publisher / Figure / What they counted) is often the cleanest form.
- Do NOT use numbered footnote markers like [1], [2]. The reasoning replaces them.
- Match the surrounding tone and length budget. 2-4 sentences (or one small table) per claim.

STRICT
- Do not touch any sentence that is not part of a contested claim below.
- Do not change headings, bullet structure, other numbers, or other citations.
- Do not add new claims or sources beyond the evidence provided.
- Return the FULL content and every sub-section, contested passages rewritten, the rest verbatim.
- Rewrite each claim in the part of the card it is listed under: claims under TOP-LEVEL CONTENT
  belong in `content`, claims under a sub-section name belong in that sub-section only.
- Attach `analyst_reasoning` to the SAME part of the card you rewrote the claim in: notes for
  top-level claims go in the top-level `analyst_reasoning`, notes for a sub-section's claims go
  in that sub-section's `analyst_reasoning`. Use an empty list where there were no claims.
- Each note carries the claim_id, your analyst_note, the working_band, and gap_type
  ("definitional" or "genuine").

SECTION: {section_name}

CONTESTED CLAIMS (grouped by where they sit in the card)
{claims_block}

SOURCE EVIDENCE (already retrieved — do not ask for more)
{evidence_block}

CARD CONTENT (JSON)
{card_json}"""


def _group_by_location(claims: List[ContestedClaim]) -> Dict[str, List[ContestedClaim]]:
    grouped: Dict[str, List[ContestedClaim]] = {}
    for claim in claims:
        grouped.setdefault(claim.location or TOP_LEVEL, []).append(claim)
    return grouped


def _claims_block(claims: List[ContestedClaim]) -> str:
    blocks = []
    for location, group in _group_by_location(claims).items():
        heading = f'SUB-SECTION "{location}"' if location else "TOP-LEVEL CONTENT"
        lines = [heading]
        for c in group:
            srcs = ", ".join(c.source_urls) or "(none cited)"
            lines.append(
                f'  {c.claim_id} [{c.kind}] "{c.claim_text}"\n'
                f"      why: {c.why or 'n/a'}\n"
                f"      sources: {srcs}"
            )
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def _evidence_block(
    claims: List[ContestedClaim],
    evidence: Dict[str, EvidenceItem],
) -> str:
    blocks = []
    for c in claims:
        items = evidence_for_claim(evidence, c.source_urls)
        if not items:
            blocks.append(f"{c.claim_id}: (no stored evidence for the cited URLs)")
            continue
        body = "\n".join(item.as_prompt_line() for item in items)
        blocks.append(f"{c.claim_id}:\n{body}")
    return "\n\n".join(blocks)


def _card_payload(card: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "content": card.get("content") or "",
        "sub_sections": [
            {"name": s.get("name"), "content": s.get("content") or ""}
            for s in card.get("sub_sections") or []
            if isinstance(s, dict)
        ],
    }


def _clean_notes(raw: Any) -> List[Dict[str, Any]]:
    """Validate the model's notes, dropping anything malformed."""
    notes: List[Dict[str, Any]] = []
    for entry in raw or []:
        if not isinstance(entry, dict) or not entry.get("claim_id") or not entry.get("analyst_note"):
            continue
        try:
            notes.append(ClaimReasoning(**entry).model_dump())
        except Exception:
            continue
    return notes


def ensure_reasoning_keys(card: Dict[str, Any]) -> Dict[str, Any]:
    """Guarantee ``analyst_reasoning`` exists on the card and on every sub-section.

    An uncontested passage gets an empty list rather than a missing key, so
    consumers can read ``sub["analyst_reasoning"]`` unconditionally and an absent
    key means "the pass never ran here", not "nothing was contested". Mutates in
    place, like the rest of the card helpers.
    """
    if not isinstance(card, dict):
        return card
    card.setdefault(REASONING_KEY, [])
    for sub in card.get("sub_sections") or []:
        if isinstance(sub, dict):
            sub.setdefault(REASONING_KEY, [])
    return card


def _apply_result(card: Dict[str, Any], result: Dict[str, Any]) -> Dict[str, Any]:
    """Return a copy of the card with passages rewritten and notes attached in place."""
    out = copy.deepcopy(card)

    if isinstance(result.get("content"), str) and result["content"].strip():
        out["content"] = result["content"]
        if isinstance(out.get("section"), list) and out["section"]:
            # keep the list-shaped mirror in sync if the card uses it
            if isinstance(out["section"][0], dict) and "content" in out["section"][0]:
                out["section"][0]["content"] = result["content"]

    out[REASONING_KEY] = _clean_notes(result.get(REASONING_KEY))

    returned = {
        s.get("name"): s
        for s in result.get("sub_sections") or []
        if isinstance(s, dict) and s.get("name") is not None
    }
    for sub in out.get("sub_sections") or []:
        if not isinstance(sub, dict):
            continue
        rewritten = returned.get(sub.get("name")) or {}
        if rewritten.get("content"):
            sub["content"] = rewritten["content"]
        sub[REASONING_KEY] = _clean_notes(rewritten.get(REASONING_KEY))
    return out


def count_reasoning(card: Dict[str, Any]) -> int:
    """How many analyst notes are attached anywhere on this card."""
    total = len(card.get(REASONING_KEY) or [])
    for sub in card.get("sub_sections") or []:
        if isinstance(sub, dict):
            total += len(sub.get(REASONING_KEY) or [])
    return total


def generate_card_reasoning(
    card: Dict[str, Any],
    contested_claims: List[ContestedClaim],
    evidence: Dict[str, EvidenceItem],
    chat_id: str | None = None,
    user_id: str | None = None,
) -> Dict[str, Any]:
    """Rewrite the contested passages of one card and attach the analyst notes in place.

    Returns the card. Notes live under ``analyst_reasoning`` on the card itself
    (top-level content claims) and on each sub-section (that sub-section's
    claims) — always present, an empty list where nothing was contested. On any
    failure the card's text is returned unchanged.
    """
    if not contested_claims:
        return ensure_reasoning_keys(card)

    section_name = card.get("section") if isinstance(card.get("section"), str) else "card"
    prompt = REASONING_PROMPT.format(
        section_name=section_name,
        claims_block=_claims_block(contested_claims),
        evidence_block=_evidence_block(contested_claims, evidence) or "(none)",
        card_json=json.dumps(_card_payload(card), ensure_ascii=False, indent=2),
    )
    start = time.time()

    try:
        # Highest-reasoning structured-output call. `reasoning.effort` is passed
        # through to the Responses API; xhigh is the ceiling for gpt-5.5. No
        # `temperature` — reasoning models ignore it.
        response = SYNC_OPENAI_CLIENT.responses.parse(
            model=ANALYST_REASONING_MODEL,
            input=[{"role": "user", "content": prompt}],
            text_format=CardReasoningOutput,
            reasoning={"effort": ANALYST_REASONING_EFFORT},
        )
        save_raw_llm_response(
            response, ANALYST_REASONING_MODEL,
            "Writing analyst reasoning for contested claims", chat_id, user_id=user_id,
        )
        parsed: CardReasoningOutput | None = response.output_parsed
        if parsed is None:
            raise ValueError("responses.parse returned no parsed output (refusal or incomplete)")
        out = _apply_result(card, parsed.model_dump())
        logger.info(
            "[analyst_reasoning.reasoning] %s: rewrote %d claim(s) across %d location(s) "
            "with OpenAI in %.2fs",
            section_name, len(contested_claims),
            len(_group_by_location(contested_claims)), time.time() - start,
        )
        return out
    except Exception as exc:
        logger.warning(
            "[analyst_reasoning.reasoning] OpenAI failed (%s: %s), trying Gemini",
            type(exc).__name__, exc,
        )

    try:
        # Fallback: Gemini Interactions API at maximum thinking. For Gemini 3 Pro
        # models `thinking_level` replaces the legacy `thinking_budget` (the two
        # are mutually exclusive) and "high" is the ceiling.
        gemini_client = genai.Client(api_key=GEMINI_API_KEY)
        interaction = gemini_client.interactions.create(
            model=GEMINI_ANALYST_REASONING_MODEL,
            input=prompt,
            response_format={
                "type": "text",
                "mime_type": "application/json",
                "schema": GEMINI_REASONING_SCHEMA,
            },
            generation_config={"thinking_level": ANALYST_REASONING_GEMINI_THINKING_LEVEL},
        )
        save_raw_llm_response(
            interaction, GEMINI_ANALYST_REASONING_MODEL,
            "Writing analyst reasoning for contested claims (backup)", chat_id, user_id=user_id,
        )
        result = json.loads(strip_json_code_fence(interaction.output_text))
        logger.info(
            "[analyst_reasoning.reasoning] %s: rewrote with Gemini fallback in %.2fs",
            section_name, time.time() - start,
        )
        return _apply_result(card, result)
    except Exception as exc:
        logger.error(
            "[analyst_reasoning.reasoning] Both models failed (%s: %s). Card returned unchanged.",
            type(exc).__name__, exc,
        )
        return ensure_reasoning_keys(card)
