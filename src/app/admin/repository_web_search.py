"""Persistence helpers for web-search analytics.

Callers persist analytics in the background with:

    asyncio.create_task(log_web_search_event(...))

directly from async code (``src/core/model.py`` and
``app.research.domains/planner.py``). This module intentionally has no scheduler,
task registry, or queue -- ``log_web_search_event`` is the only entry point,
and it never raises back to the caller.
"""

from __future__ import annotations

import datetime
import json
from typing import Any, TypedDict
from urllib.parse import SplitResult, parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import UUID

from uuid_utils import uuid7

from app.core.db import async_session_scope
from app.core.logging import setup_logging
from app.models import WebSearchCitation, WebSearchEvent, WebSearchRawResponse

logger = setup_logging(__name__)

# Guard against pathological payloads (e.g. many web_search_call.results
# blocks) bloating the JSONB column. When exceeded, we keep a small
# placeholder (still queryable by id/model/status) instead of the full body.
_MAX_RAW_RESPONSE_BYTES = 2_000_000


class SearchLink(TypedDict, total=False):
    url: str
    title: str
    snippet: str
    search_call_id: str
    search_call_index: int
    result_rank: int
    citation_order: int


LinkInput = str | SearchLink
_TRACKING_QUERY_PARAMETERS = {"gclid", "fbclid", "msclkid"}


def _valid_uuid(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return str(UUID(str(value)))
    except ValueError, TypeError, AttributeError:
        return None


def _normalize_url(raw_url: Any) -> str | None:
    """Return a deterministic HTTP(S) URL, excluding fragments."""
    if not isinstance(raw_url, str):
        return None

    value = raw_url.strip()
    if not value:
        return None
    try:
        parsed = urlsplit(value)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
            return None
        if parsed.username is not None or parsed.password is not None:
            return None

        hostname = parsed.hostname.lower()
        if ":" in hostname:
            hostname = f"[{hostname}]"
        port = parsed.port
        if port and not (
            (parsed.scheme.lower() == "http" and port == 80)
            or (parsed.scheme.lower() == "https" and port == 443)
        ):
            hostname = f"{hostname}:{port}"

        query_parameters = [
            (key, query_value)
            for key, query_value in parse_qsl(
                parsed.query,
                keep_blank_values=True,
            )
            if not (key.lower().startswith("utm_") or key.lower() in _TRACKING_QUERY_PARAMETERS)
        ]
        normalized = SplitResult(
            scheme=parsed.scheme.lower(),
            netloc=hostname,
            path=parsed.path,
            query=urlencode(query_parameters, doseq=True),
            fragment="",
        )
        return urlunsplit(normalized)
    except ValueError, TypeError:
        return None


def _extract_domain(url: str) -> str | None:
    """Return a hostname without a leading ``www.``."""
    try:
        hostname = urlsplit(url).hostname or ""
        return hostname.removeprefix("www.") or None
    except ValueError, TypeError:
        return None


def _normalize_links(
    links: list[LinkInput] | None,
    *,
    cited: bool,
) -> list[dict[str, Any]]:
    """Normalize one role's links (candidates or cited) into DB-ready rows.

    ``cited`` links are deduplicated by canonical URL: ``cited_links`` can
    legitimately contain the same URL twice before it reaches here — once
    from OpenAI's own ``url_citation`` annotations (rich metadata) and once
    from the plain-text URL regex fallback over the final rendered output
    (see ``enrich_terminal_search_analytics`` /
    ``_urls_in_rendered_output`` in ``src/core/web_search_analytics.py``).
    Without this dedup, the same source would be persisted as two
    ``was_cited_in_output=True`` rows, inflating ``cited_count`` and any
    naive ``COUNT(*)`` analysis. We keep a single row per cited URL, in
    first-seen order, backfilling ``title``/``snippet`` from a later
    duplicate if the one we kept so far is missing them.

    ``candidate`` links are intentionally NOT deduplicated here: the same
    URL can legitimately appear as a distinct candidate result at different
    ``search_call_index``/``result_rank`` positions across separate web
    search calls, and that positional information is worth keeping.
    """
    normalized: list[dict[str, Any]] = []
    cited_row_index_by_url: dict[str, int] = {}
    for occurrence_index, item in enumerate(links or []):
        payload: dict[str, Any]
        if isinstance(item, str):
            payload = {"url": item}
        elif isinstance(item, dict):
            payload = item
        else:
            continue

        raw_url = payload.get("url")
        clean_url = _normalize_url(raw_url)
        if clean_url is None:
            continue

        if cited and clean_url in cited_row_index_by_url:
            existing = normalized[cited_row_index_by_url[clean_url]]
            if not existing.get("title") and payload.get("title"):
                existing["title"] = payload.get("title")
            if not existing.get("snippet") and payload.get("snippet"):
                existing["snippet"] = payload.get("snippet")
            continue

        row = {
            "url": clean_url,
            "raw_url": raw_url,
            "domain": _extract_domain(clean_url),
            "title": payload.get("title") or None,
            "snippet": payload.get("snippet") or None,
            "search_call_id": payload.get("search_call_id") or None,
            "search_call_index": payload.get("search_call_index"),
            "result_rank": payload.get("result_rank"),
            "citation_order": (
                payload.get("citation_order", occurrence_index)
                if cited
                else payload.get("citation_order")
            ),
            "was_cited_in_output": cited,
        }
        if cited:
            cited_row_index_by_url[clean_url] = len(normalized)
        normalized.append(row)
    return normalized


def _prepare_raw_response(
    raw_response: dict[str, Any] | None,
) -> tuple[dict[str, Any] | None, int | None, bool]:
    """Return ``(stored_payload, size_bytes, is_truncated)`` for the raw_response column."""
    if not raw_response:
        return None, None, False
    try:
        size_bytes = len(json.dumps(raw_response, default=str).encode("utf-8"))
    except TypeError, ValueError:
        return None, None, False

    if size_bytes <= _MAX_RAW_RESPONSE_BYTES:
        return raw_response, size_bytes, False

    truncated_payload = {
        "id": raw_response.get("id"),
        "model": raw_response.get("model"),
        "status": raw_response.get("status"),
        "created_at": raw_response.get("created_at"),
        "_truncated": True,
        "_original_size_bytes": size_bytes,
        "_note": "Full response omitted — exceeded the web_search_raw_responses size guard.",
    }
    return truncated_payload, size_bytes, True


def _coerce_response_created_at(value: Any) -> datetime.datetime | None:
    """Convert OpenAI's epoch-seconds ``created_at`` to a UTC datetime."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.datetime.fromtimestamp(value, tz=datetime.UTC)
        except OverflowError, OSError, ValueError:
            return None
    return None


async def log_web_search_event(
    trigger_source: str,
    user_query: str,
    raw_citations: list[LinkInput] | None = None,
    model_used: str | None = None,
    user_id: str | None = None,
    chat_id: str | None = None,
    report_id: str | None = None,
    section_name: str | None = None,
    *,
    candidate_links: list[LinkInput] | None = None,
    cited_links: list[LinkInput] | None = None,
    operation_id: str | None = None,
    attempt_number: int = 0,
    provider: str | None = None,
    provider_response_id: str | None = None,
    provider_queries: list[str] | None = None,
    status: str | None = "succeeded",
    error_type: str | None = None,
    duration_ms: int | None = None,
    search_call_count: int = 0,
    usage_metadata: dict[str, Any] | None = None,
    card_id: str | None = None,
    previous_response_id: str | None = None,
    response_created_at: Any | None = None,
    raw_response: dict[str, Any] | None = None,
) -> None:
    """Persist one attempt and all valid candidate/used URL occurrences.

    ``raw_citations`` remains as a legacy alias for candidate links. Errors are
    intentionally swallowed so analytics can never affect the calling flow.
    Intended to be scheduled with ``asyncio.create_task`` from async callers
    so the database write never blocks the event loop.

    ``raw_response``, when provided (card generation, card refinement, and
    Ask Caspr only — see ``collect_openai_search_analytics``), is persisted
    in the same transaction as a ``WebSearchRawResponse`` row 1:1 with the
    ``WebSearchEvent`` created here.
    """
    try:
        logger.info(
            "[web_search_db] Persisting search event | "
            f"trigger={trigger_source} operation_id={operation_id} "
            f"attempt={attempt_number} provider={provider} status={status} "
            f"chat_id={chat_id} card_id={card_id}"
        )
        if candidate_links is None and raw_citations is not None:
            candidate_links = raw_citations

        candidates = _normalize_links(candidate_links, cited=False)
        cited = _normalize_links(cited_links, cited=True)
        valid_user_id = _valid_uuid(user_id)
        valid_report_id = _valid_uuid(report_id)
        valid_operation_id = _valid_uuid(operation_id) or str(uuid7())

        async with async_session_scope() as session:
            event = WebSearchEvent(
                operation_id=valid_operation_id,
                attempt_number=attempt_number,
                user_id=valid_user_id,
                chat_id=chat_id or None,
                report_id=valid_report_id,
                card_id=card_id or None,
                trigger_source=trigger_source,
                section_name=section_name or None,
                user_query=user_query or None,
                model_used=model_used or None,
                provider=provider or None,
                provider_response_id=provider_response_id or None,
                provider_queries=provider_queries,
                status=status or None,
                error_type=error_type or None,
                duration_ms=duration_ms,
                search_call_count=search_call_count,
                candidate_count=len(candidates),
                cited_count=len(cited),
                total_results_count=len(candidates),
                usage_metadata=usage_metadata,
            )
            session.add(event)
            await session.flush()

            for link in candidates + cited:
                session.add(
                    WebSearchCitation(
                        search_event_id=event.id,
                        user_id=valid_user_id,
                        report_id=valid_report_id,
                        **link,
                    )
                )

            stored_raw_response, payload_size_bytes, is_truncated = _prepare_raw_response(
                raw_response
            )
            if stored_raw_response is not None:
                session.add(
                    WebSearchRawResponse(
                        search_event_id=event.id,
                        operation_id=valid_operation_id,
                        attempt_number=attempt_number,
                        user_id=valid_user_id,
                        report_id=valid_report_id,
                        chat_id=chat_id or None,
                        card_id=card_id or None,
                        trigger_source=trigger_source,
                        provider=provider or None,
                        provider_response_id=provider_response_id or None,
                        previous_response_id=previous_response_id or None,
                        model_used=model_used or None,
                        response_status=status or None,
                        raw_response=stored_raw_response,
                        payload_size_bytes=payload_size_bytes,
                        is_truncated=is_truncated,
                        response_created_at=_coerce_response_created_at(response_created_at),
                    )
                )

            await session.commit()

        logger.info(
            "[web_search_db] Search event logged | "
            f"trigger={trigger_source} status={status} "
            f"candidates={len(candidates)} cited={len(cited)} chat_id={chat_id} "
            f"raw_response_stored={stored_raw_response is not None} "
            f"raw_response_truncated={is_truncated}"
        )
    except Exception as exc:
        logger.error(
            "[web_search_db] Failed to log search event (non-fatal) | "
            f"trigger={trigger_source} chat_id={chat_id} error={exc}",
            exc_info=True,
        )
