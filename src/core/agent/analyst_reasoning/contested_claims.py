"""Part 1 (fallback) — flag contested claims when card generation didn't.

The cheap, correct place to flag contested claims is card generation itself:
the model just saw the conflicting figures, so add a ``contested_claims`` field
to the card schema and read it here for free (``contested_claims_from_card``).

When that field is absent (older cards, or a generation path not yet updated),
``triage_card`` runs ONE cheap-model call per card to recover the same list.
It is deliberately conservative: prose facts with a single source return
nothing, so downstream reasoning only fires on cards that genuinely need it.

Conservative is not the same as inattentive, though, and the failure mode that
matters is a long card where the reviewer reports the first obvious hit and
stops. ``location_sweep`` is the counterweight: the tool schema makes the model
commit to a one-line verdict for the top-level content and for every sub-section
before it settles on the claim list, and that sweep is logged so a card that
comes back clean still says why.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any, Dict, List

from google import genai
from openai import OpenAI

from src.config.constants import (
    ANALYST_CLAIM_TRIAGE_MODEL,
    GEMINI_ANALYST_REASONING_MODEL,
    GEMINI_API_KEY,
)
from src.config.log_helper import setup_logging
from src.core.agent.analyst_reasoning.schemas import (
    GEMINI_TRIAGE_SCHEMA,
    TOP_LEVEL,
    TRIAGE_TOOL_SCHEMA,
    ContestedClaim,
)
from src.core.observability.llm_response_logger import save_raw_llm_response, strip_json_code_fence

logger = setup_logging(__name__)


TRIAGE_PROMPT = """You are a research QA reviewer. You will receive one report card as JSON.

Identify claims that CANNOT be honestly supported by simply attaching a citation, because
the report made an analytical choice the reader can't see. Only these three kinds count:

1. conflicting_sources — the card states a quantity (market size, TAM, forecast, growth
   rate, valuation multiple, share, employment or wage effect) that published sources are
   known to disagree on, OR the card itself puts two figures for the same underlying thing
   near each other. That pairing counts wherever it happens: two bullets, two rows of a
   table, or two different sub-sections. Hedging prose around a pair of numbers — "evidence
   is mixed", "estimates vary", "however", "on the other hand" — is a strong signal. Flag
   the pair as ONE claim, quoting the passage that states the figure being stood behind.
2. derived_number — the card presents a number it computed (a sum, a conversion, a weighted
   average, a per-unit figure) rather than one lifted verbatim from a source.
3. scope_mismatch — the card applies a figure at a different scope than the source measured:
   a national figure read onto a region or segment, a cross-sector average read onto one
   occupation, or a survey of one population read onto another.

WORK THROUGH THE CARD ONE LOCATION AT A TIME
Return `location_sweep` with exactly one entry per location: first the top-level "content"
(location ""), then EVERY sub_section in order, using its exact "name". For each, give a
one-line finding — the claim_ids you flagged there, or why nothing there qualified. Do this
before you settle on `contested_claims`. A card commonly has contested claims in more than
one location, and a location you never examined is not a location you have cleared.

Do NOT flag:
- Qualitative statements, background, or definitions.
- A single quantity with a single source, where no other figure in the card measures the
  same thing and publishers are not known to disagree on it.
- A passage that ALREADY does the analyst's work: it names what each source counted AND says
  which figure the report stands behind. Attribution on its own is not enough — a table of
  figures with a source line, or a sentence naming a publisher, still needs flagging if it
  never reconciles those figures against each other.

For each flagged claim return: a stable claim_id ("c1", "c2", ...), the exact claim_text
from the card, the source_urls cited for it (copy them as-is), the kind, a one-line why, and
the location — the exact "name" of the sub_section the claim appears under, copied verbatim
from the card JSON, or an empty string if the claim is in the card's top-level "content".
Return an empty `contested_claims` list if the card genuinely has none.

Card JSON:
{card_json}"""


def _sub_section_names(card: Dict[str, Any]) -> List[str]:
    return [
        s["name"]
        for s in card.get("sub_sections") or []
        if isinstance(s, dict) and isinstance(s.get("name"), str) and s["name"].strip()
    ]


def resolve_location(card: Dict[str, Any], raw: Any, claim_text: str = "") -> str:
    """Pin a claim to a real sub-section name, or ``TOP_LEVEL`` for the card's own content.

    Trusts the model's label only when it matches a sub-section the card actually has;
    otherwise falls back to finding the claim text in the sub-section bodies, which
    needs no LLM and no extra call.
    """
    names = _sub_section_names(card)
    if isinstance(raw, str) and raw.strip():
        wanted = raw.strip().casefold()
        for name in names:
            if name.casefold() == wanted:
                return name

    needle = (claim_text or "").strip()
    if needle:
        for sub in card.get("sub_sections") or []:
            if isinstance(sub, dict) and needle in (sub.get("content") or ""):
                return sub.get("name") or TOP_LEVEL
    return TOP_LEVEL


def contested_claims_from_card(card: Dict[str, Any]) -> List[ContestedClaim]:
    """Read claims flagged at generation time from ``card['contested_claims']``."""
    raw = card.get("contested_claims") if isinstance(card, dict) else None
    if not isinstance(raw, list):
        return []
    claims: List[ContestedClaim] = []
    for i, entry in enumerate(raw, 1):
        if not isinstance(entry, dict) or not entry.get("claim_text"):
            continue
        try:
            claims.append(
                ContestedClaim(
                    claim_id=str(entry.get("claim_id") or f"c{i}"),
                    claim_text=entry["claim_text"],
                    source_urls=list(entry.get("source_urls") or []),
                    kind=entry.get("kind") or "conflicting_sources",
                    why=entry.get("why") or "",
                    location=resolve_location(card, entry.get("location"), entry["claim_text"]),
                )
            )
        except Exception:
            logger.warning("[analyst_reasoning.triage] Skipped malformed flagged claim: %r", entry)
    return claims


def _card_for_prompt(card: Dict[str, Any]) -> str:
    slim = {
        "section": card.get("section"),
        "content": card.get("content"),
        "sub_sections": [
            {"name": s.get("name"), "content": s.get("content")}
            for s in card.get("sub_sections") or []
            if isinstance(s, dict)
        ],
    }
    return json.dumps(slim, ensure_ascii=False, indent=2)


def _log_sweep(payload: dict, section_name: str) -> None:
    """Record the per-location verdict — the only trace left when a card comes back clean."""
    sweep = payload.get("location_sweep")
    if not isinstance(sweep, list) or not sweep:
        logger.warning("[analyst_reasoning.triage] %s: no location_sweep returned", section_name)
        return
    lines = [
        f"{entry.get('location') or 'TOP-LEVEL'} -> {entry.get('finding') or '(none)'}"
        for entry in sweep
        if isinstance(entry, dict)
    ]
    logger.info("[analyst_reasoning.triage] %s sweep: %s", section_name, " | ".join(lines))


def _parse_claims(payload: dict, card: Dict[str, Any]) -> List[ContestedClaim]:
    out: List[ContestedClaim] = []
    for i, entry in enumerate(payload.get("contested_claims") or [], 1):
        if not isinstance(entry, dict) or not entry.get("claim_text"):
            continue
        try:
            out.append(
                ContestedClaim(
                    claim_id=str(entry.get("claim_id") or f"c{i}"),
                    claim_text=entry["claim_text"],
                    source_urls=list(entry.get("source_urls") or []),
                    kind=entry.get("kind") or "conflicting_sources",
                    why=entry.get("why") or "",
                    location=resolve_location(card, entry.get("location"), entry["claim_text"]),
                )
            )
        except Exception:
            continue
    return out


def triage_card(
    card: Dict[str, Any],
    chat_id: str | None = None,
    user_id: str | None = None,
) -> List[ContestedClaim]:
    """One cheap-model pass to flag contested claims. Returns [] on any failure."""
    flagged = contested_claims_from_card(card)
    if flagged:
        logger.info("[analyst_reasoning.triage] Using %d claim(s) flagged at generation time", len(flagged))
        return flagged

    if not card or not (card.get("content") or card.get("sub_sections") or card.get("section")):
        return []

    prompt = TRIAGE_PROMPT.format(card_json=_card_for_prompt(card))
    section_name = card.get("section") if isinstance(card.get("section"), str) else "card"
    start = time.time()

    try:
        client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
        response = client.chat.completions.create(
            model=ANALYST_CLAIM_TRIAGE_MODEL,
            messages=[{"role": "user", "content": prompt}],
            tools=[TRIAGE_TOOL_SCHEMA],
            tool_choice={"type": "function", "function": {"name": "flag_contested_claims"}},
        )
        save_raw_llm_response(
            response, ANALYST_CLAIM_TRIAGE_MODEL, "Triaging contested claims", chat_id, user_id=user_id
        )
        args = json.loads(response.choices[0].message.tool_calls[0].function.arguments)
        _log_sweep(args, section_name)
        claims = _parse_claims(args, card)
        logger.info(
            "[analyst_reasoning.triage] %s: %d contested claim(s) across %d location(s) in %.2fs",
            section_name, len(claims), len({c.location for c in claims}), time.time() - start,
        )
        return claims
    except Exception as exc:
        logger.warning(
            "[analyst_reasoning.triage] OpenAI failed (%s: %s), trying Gemini", type(exc).__name__, exc
        )

    try:
        gemini_client = genai.Client(api_key=GEMINI_API_KEY)
        interaction = gemini_client.interactions.create(
            model=GEMINI_ANALYST_REASONING_MODEL,
            input=prompt,
            response_format={
                "type": "text",
                "mime_type": "application/json",
                "schema": GEMINI_TRIAGE_SCHEMA,
            },
            generation_config={"thinking_level": "low"},
        )
        save_raw_llm_response(
            interaction, GEMINI_ANALYST_REASONING_MODEL,
            "Triaging contested claims (backup)", chat_id, user_id=user_id,
        )
        args = json.loads(strip_json_code_fence(interaction.output_text))
        _log_sweep(args, section_name)
        return _parse_claims(args, card)
    except Exception as exc:
        logger.error(
            "[analyst_reasoning.triage] Both models failed (%s: %s). No claims flagged.",
            type(exc).__name__, exc,
        )
        return []
