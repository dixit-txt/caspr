"""Analyst-reasoning pipeline.

Replaces inline / numbered citations with visible source-evaluation prose on
the small subset of claims that need it (contested published figures, derived
numbers, inherited-scope figures), and cleans tracking parameters off every
citation URL.

Entry points (each returns the card, with the analyst notes attached inside it
under ``analyst_reasoning`` — see :data:`REASONING_KEY`):
    apply_analyst_reasoning(card, report_id, ...)            -> dict
    apply_analyst_reasoning_to_report(cards, report_id, ...) -> list[dict]

Cheaper building blocks, if you want them directly:
    url_cleaner.clean_card_citation_urls(card)               (deterministic, no LLM)
    contested_claims.triage_card(card)                       (one cheap-model call)
    evidence_store.build_evidence_store(report_id)           (DB only, no network)
    reasoning_pass.generate_card_reasoning(card, claims, evidence)
"""

from src.core.agent.analyst_reasoning.evidence_store import (
    build_evidence_store,
    build_evidence_store_from_analytics,
)
from src.core.agent.analyst_reasoning.pipeline import (
    apply_analyst_reasoning,
    apply_analyst_reasoning_to_content,
    apply_analyst_reasoning_to_report,
)
from src.core.agent.analyst_reasoning.reasoning_pass import (
    count_reasoning,
    ensure_reasoning_keys,
)
from src.core.agent.analyst_reasoning.schemas import (
    REASONING_KEY,
    TOP_LEVEL,
    ClaimReasoning,
    ContestedClaim,
    EvidenceItem,
)
from src.core.agent.analyst_reasoning.url_cleaner import (
    clean_card_citation_urls,
    clean_citation_urls,
    dress_inline_citations,
    normalize_url,
)

__all__ = [
    "apply_analyst_reasoning",
    "apply_analyst_reasoning_to_content",
    "apply_analyst_reasoning_to_report",
    "build_evidence_store",
    "build_evidence_store_from_analytics",
    "count_reasoning",
    "ensure_reasoning_keys",
    "ClaimReasoning",
    "ContestedClaim",
    "EvidenceItem",
    "REASONING_KEY",
    "TOP_LEVEL",
    "clean_card_citation_urls",
    "clean_citation_urls",
    "dress_inline_citations",
    "normalize_url",
]
