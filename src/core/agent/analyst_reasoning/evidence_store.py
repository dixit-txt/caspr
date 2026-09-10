"""Part 2 — build a per-report evidence store from data we already persisted.

Nothing here calls the network. Card generation already stores, per report:

  * ``web_search_citations`` rows — one per URL, with ``title`` / ``snippet`` /
    ``domain`` (see ``src/db/web_search_db.py``).
  * ``web_search_raw_responses`` rows — the full OpenAI Responses payload, whose
    ``output[].message.content[].annotations[]`` carry ``url_citation`` entries
    with the exact passage the model quoted.

We fold both into ``{canonical_url: EvidenceItem}``. The reasoning pass then
gets real source text for each contested claim without a single Tavily call.
Only a contested claim whose source has *no* stored snippet is a candidate for
lazy gap-fill (left to the caller / a follow-up).
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, Optional

from src.config.log_helper import setup_logging
from src.core.agent.analyst_reasoning.schemas import EvidenceItem
from src.core.agent.analyst_reasoning.url_cleaner import canonical_key, normalize_url

logger = setup_logging(__name__)

# web_search_call snippets are terse; url_citation quotes are the good stuff.
# Cap what we hand the model per source so a pathological page can't blow up
# the reasoning prompt.
_MAX_SNIPPET_CHARS = 1200


def _clip(text: Optional[str]) -> Optional[str]:
    if not text:
        return None
    text = " ".join(text.split())
    return text[:_MAX_SNIPPET_CHARS] if len(text) > _MAX_SNIPPET_CHARS else text


def _merge(store: Dict[str, EvidenceItem], url: str, **fields: Any) -> None:
    key = canonical_key(url)
    if not key:
        return
    item = store.get(key)
    if item is None:
        display = normalize_url(url) or url
        domain = display.split("//", 1)[-1].split("/", 1)[0].removeprefix("www.") or None
        item = EvidenceItem(url=display, domain=domain)
        store[key] = item
    for name, value in fields.items():
        if value and not getattr(item, name, None):
            setattr(item, name, value)


def _walk_raw_response(payload: dict, store: Dict[str, EvidenceItem]) -> None:
    """Pull url_citation quotes and web_search_call result snippets from one payload."""
    if not isinstance(payload, dict):
        return
    for item in payload.get("output") or []:
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")

        if item_type == "message":
            for block in item.get("content") or []:
                if not isinstance(block, dict):
                    continue
                for ann in block.get("annotations") or []:
                    if not isinstance(ann, dict):
                        continue
                    nested = ann.get("url_citation") if isinstance(ann.get("url_citation"), dict) else ann
                    url = nested.get("url")
                    if url:
                        _merge(
                            store,
                            url,
                            title=nested.get("title"),
                            quoted=_clip(nested.get("snippet") or nested.get("text")),
                        )

        elif item_type == "web_search_call":
            action = item.get("action") if isinstance(item.get("action"), dict) else {}
            groups = [item.get("results"), action.get("results"), action.get("sources")]
            for group in groups:
                if not isinstance(group, list):
                    continue
                for result in group:
                    if not isinstance(result, dict) or not result.get("url"):
                        continue
                    _merge(
                        store,
                        result["url"],
                        title=result.get("title"),
                        snippet=_clip(result.get("snippet") or result.get("description")),
                    )


def _ingest_citation_row(store: Dict[str, EvidenceItem], url, domain, title, snippet) -> None:
    if not url:
        return
    _merge(store, url, title=title, snippet=_clip(snippet))
    if domain:
        key = canonical_key(url)
        if key and key in store and not store[key].domain:
            store[key].domain = domain

async def build_evidence_store(
    report_id: str = "",
    *,
    chat_id: Optional[str] = None,
    card_id: Optional[str] = None,
) -> Dict[str, EvidenceItem]:
    """Return ``{canonical_url: EvidenceItem}`` from persisted web-search data.

    Filters by ``report_id`` when given, else by ``chat_id`` (card generation
    persists analytics against ``chat_id`` — the report row may not exist yet).
    Deduplicated by canonical URL, so a source cited ten times is stored once.
    """
    store: Dict[str, EvidenceItem] = {}
    if not report_id and not chat_id:
        return store

    # Imported lazily so this module can be vendored into services that have no
    # direct Postgres access (e.g. ask-caspr-service, which only uses the
    # in-memory `build_evidence_store_from_analytics` path).
    try:
        from sqlalchemy import select
        from src.db.database import WebSearchCitation, WebSearchRawResponse
        from src.db.db_utils import async_session_scope
    except Exception:
        logger.warning(
            "[analyst_reasoning.evidence_store] No DB layer available; "
            "build_evidence_store returns empty. Use build_evidence_store_from_analytics."
        )
        return store

    if report_id:
        cite_filter = WebSearchCitation.report_id == report_id
        raw_filter = WebSearchRawResponse.report_id == report_id
    else:
        cite_filter = WebSearchCitation.search_event_id.in_(
            select(WebSearchRawResponse.search_event_id).where(
                WebSearchRawResponse.chat_id == chat_id
            )
        )
        raw_filter = WebSearchRawResponse.chat_id == chat_id

    try:
        async with async_session_scope() as session:
            citation_stmt = select(
                WebSearchCitation.url,
                WebSearchCitation.domain,
                WebSearchCitation.title,
                WebSearchCitation.snippet,
            ).where(cite_filter)
            for row in (await session.execute(citation_stmt)).all():
                _ingest_citation_row(store, *row)

            raw_stmt = select(WebSearchRawResponse.raw_response).where(raw_filter)
            if card_id:
                raw_stmt = raw_stmt.where(WebSearchRawResponse.card_id == card_id)
            for (payload,) in (await session.execute(raw_stmt)).all():
                _walk_raw_response(payload or {}, store)
    except Exception:
        logger.exception(
            "[analyst_reasoning.evidence_store] Failed to build evidence store | report_id=%s chat_id=%s",
            report_id, chat_id,
        )
        return store

    logger.info(
        "[analyst_reasoning.evidence_store] Built evidence store | report_id=%s chat_id=%s card_id=%s sources=%d with_text=%d",
        report_id, chat_id, card_id, len(store),
        sum(1 for i in store.values() if i.snippet or i.quoted),
    )
    return store


def build_evidence_store_from_analytics(
    analytics_collector: Optional[list[dict]],
) -> Dict[str, EvidenceItem]:
    """Build the evidence store in-memory from the generation loop's analytics.

    ``analytics_collector`` is the list ``model.py`` / ``planner.py`` pass to
    ``generate_cards``; each entry carries ``raw_response`` (full OpenAI payload),
    ``candidate_links`` and ``_provider_cited_links``. Using it avoids a DB round
    trip and any read-your-writes race with the fire-and-forget analytics writer.
    """
    store: Dict[str, EvidenceItem] = {}
    for entry in analytics_collector or []:
        if not isinstance(entry, dict):
            continue
        _walk_raw_response(entry.get("raw_response") or {}, store)
        for link in (entry.get("candidate_links") or []) + (entry.get("_provider_cited_links") or []):
            if isinstance(link, dict) and link.get("url"):
                _merge(
                    store,
                    link["url"],
                    title=link.get("title"),
                    snippet=_clip(link.get("snippet") or link.get("description")),
                )
    if store:
        logger.info(
            "[analyst_reasoning.evidence_store] Built in-memory evidence store | sources=%d with_text=%d",
            len(store), sum(1 for i in store.values() if i.snippet or i.quoted),
        )
    return store


def evidence_for_claim(
    store: Dict[str, EvidenceItem],
    source_urls: Iterable[str],
) -> list[EvidenceItem]:
    """Resolve a claim's cited URLs (tracking params allowed) to stored evidence."""
    out: list[EvidenceItem] = []
    seen: set[str] = set()
    for raw in source_urls or []:
        key = canonical_key(raw)
        if key and key in store and key not in seen:
            seen.add(key)
            out.append(store[key])
    return out


def missing_evidence_urls(
    store: Dict[str, EvidenceItem],
    source_urls: Iterable[str],
) -> list[str]:
    """Cited URLs with no usable stored text — candidates for lazy gap-fill."""
    missing: list[str] = []
    for raw in source_urls or []:
        key = canonical_key(raw)
        if not key:
            continue
        item = store.get(key)
        if item is None or not (item.snippet or item.quoted):
            missing.append(normalize_url(raw) or raw)
    return missing
