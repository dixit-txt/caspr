"""Orchestration for the analyst-reasoning pipeline.

    card in ──► clean citation URLs (always, deterministic)
             ──► flag contested claims (from card, else one cheap triage call)
             ──► if none: done — return the cleaned card
             ──► build the evidence store from data already persisted (1 per report)
             ──► one strong-model call to rewrite the contested passages
             ──► clean URLs again (the rewrite may reintroduce tracking params)
    card out

Every entry point returns the card itself. The analyst notes ride along inside
it under ``analyst_reasoning`` — on the card for top-level content claims, and
on each sub-section for that sub-section's claims — so a caller never has to
join a separate log back onto the text it explains.

Cost per report ≈ (cards without a generation-time flag) cheap triage calls
+ (cards with a contested claim) strong calls + 0 network fetches. Scales with
contested claims, not citation count.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional

from src.config.log_helper import setup_logging
from src.core.agent.analyst_reasoning.contested_claims import triage_card
from src.core.agent.analyst_reasoning.evidence_store import (
    build_evidence_store,
    build_evidence_store_from_analytics,
    missing_evidence_urls,
)
from src.core.agent.analyst_reasoning.reasoning_pass import (
    count_reasoning,
    ensure_reasoning_keys,
    generate_card_reasoning,
)
from src.core.agent.analyst_reasoning.schemas import EvidenceItem
from src.core.agent.analyst_reasoning.url_cleaner import (
    clean_card_citation_urls,
    clean_citation_urls,
)

logger = setup_logging(__name__)

# Cap concurrent reasoning calls when processing a whole report.
_MAX_CONCURRENCY = 4


async def apply_analyst_reasoning(
    card: Dict[str, Any],
    report_id: str = "",
    *,
    card_id: Optional[str] = None,
    chat_id: Optional[str] = None,
    user_id: Optional[str] = None,
    run_triage: bool = True,
    evidence: Optional[Dict[str, EvidenceItem]] = None,
) -> Dict[str, Any]:
    """Clean citation URLs and, for contested cards only, add visible analyst reasoning.

    ``evidence`` may be passed in — e.g. built in-memory from the generation
    loop's analytics collector (:func:`build_evidence_store_from_analytics`), or
    once per report when processing many cards. When it is ``None`` the store is
    built from the DB using ``report_id`` (falling back to ``chat_id``).
    Never raises — returns the best card it has.
    """
    # Set the empty-list invariant up front and in place, so the caller's card
    # satisfies it even if a later step raises out of here.
    card = ensure_reasoning_keys(clean_card_citation_urls(card))

    contested = (
        await asyncio.to_thread(triage_card, card, chat_id, user_id) if run_triage else []
    )
    if not contested:
        return card

    if evidence is None:
        evidence = await build_evidence_store(report_id, chat_id=chat_id, card_id=card_id)

    for claim in contested:
        gaps = missing_evidence_urls(evidence, claim.source_urls)
        if gaps:
            logger.info(
                "[analyst_reasoning.pipeline] claim %s has %d source(s) with no stored evidence "
                "(gap-fill candidates): %s",
                claim.claim_id, len(gaps), gaps,
            )

    card = await asyncio.to_thread(
        generate_card_reasoning, card, contested, evidence, chat_id, user_id
    )
    return clean_card_citation_urls(card)


async def apply_analyst_reasoning_to_report(
    cards: List[Dict[str, Any]],
    report_id: str,
    *,
    chat_id: Optional[str] = None,
    user_id: Optional[str] = None,
    card_id_getter=lambda c: c.get("card_id") or c.get("id"),
) -> List[Dict[str, Any]]:
    """Run the pipeline across every card of a report, sharing one evidence store."""
    for card in cards:
        ensure_reasoning_keys(clean_card_citation_urls(card))

    triaged = await asyncio.gather(
        *(asyncio.to_thread(triage_card, card, chat_id, user_id) for card in cards)
    )
    contested_total = sum(len(t) for t in triaged)
    contested_cards = sum(1 for t in triaged if t)
    logger.info(
        "[analyst_reasoning.pipeline] report %s: %d/%d cards contested, %d claims total",
        report_id, contested_cards, len(cards), contested_total,
    )

    if not contested_cards:
        return cards

    evidence = await build_evidence_store(report_id)
    semaphore = asyncio.Semaphore(_MAX_CONCURRENCY)

    async def _one(card: Dict[str, Any], contested) -> Dict[str, Any]:
        if not contested:
            return card
        async with semaphore:
            card = await asyncio.to_thread(
                generate_card_reasoning, card, contested, evidence, chat_id, user_id
            )
        return clean_card_citation_urls(card)

    return await asyncio.gather(
        *(_one(card, contested) for card, contested in zip(cards, triaged))
    )


def apply_analyst_reasoning_to_content(
    content: str,
    section_name: str,
    *,
    analytics_collector: Optional[List[dict]] = None,
    evidence: Optional[Dict[str, EvidenceItem]] = None,
    chat_id: Optional[str] = None,
    user_id: Optional[str] = None,
) -> str:
    """Synchronous single-section variant for the refine flow.

    ``refine_card`` works on one section's content string (not a card dict) and
    runs in a worker thread, so this is sync. Evidence comes from the in-memory
    ``analytics_collector`` the refine call already fills — no DB, no fetches.

    Cleans citation URLs, then if the section has a contested / derived claim,
    rewrites just that passage with visible source reasoning. Returns the
    rewritten content string; there are no sub-sections here, so the analyst
    notes are only logged. Returns the original content on anything unexpected.
    """
    if not content or not content.strip():
        return content

    content = clean_citation_urls(content)
    card = {"section": section_name or "section", "content": content, "sub_sections": []}

    try:
        contested = triage_card(card, chat_id=chat_id, user_id=user_id)
        if not contested:
            return content
        if evidence is None:
            evidence = build_evidence_store_from_analytics(analytics_collector)
        reasoned = generate_card_reasoning(card, contested, evidence, chat_id, user_id)
        note_count = count_reasoning(reasoned)
        if note_count:
            logger.info(
                "[analyst_reasoning.pipeline] refine: rewrote %d contested claim(s) in section '%s'",
                note_count, section_name,
            )
        return clean_citation_urls(reasoned.get("content") or content)
    except Exception:
        logger.exception(
            "[analyst_reasoning.pipeline] refine reasoning failed for section '%s'; using content as-is",
            section_name,
        )
        return content
