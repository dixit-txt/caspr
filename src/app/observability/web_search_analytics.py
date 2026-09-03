"""Lightweight extraction helpers for web-search analytics."""

from __future__ import annotations

import re
from typing import Any

from app.core.logging import setup_logging

_ANALYTICS_URL_RE = re.compile(r"https?://[^\s<>\]\[()\"']+")
logger = setup_logging(__name__)


def _analytics_dict(value: Any) -> dict:
    if isinstance(value, dict):
        return value
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        dumped = model_dump()
        return dumped if isinstance(dumped, dict) else {}
    return {}


def _urls_in_rendered_output(value: Any) -> list[str]:
    urls: list[str] = []
    if isinstance(value, str):
        urls.extend(match.rstrip(".,;:!?") for match in _ANALYTICS_URL_RE.findall(value))
    elif isinstance(value, dict):
        for child in value.values():
            urls.extend(_urls_in_rendered_output(child))
    elif isinstance(value, (list, tuple)):
        for child in value:
            urls.extend(_urls_in_rendered_output(child))
    return urls


def _raw_response_json(response: Any) -> dict[str, Any] | None:
    """Full JSON-serializable dump of the raw SDK response object, or None."""
    model_dump = getattr(response, "model_dump", None)
    if callable(model_dump):
        try:
            dumped = model_dump(mode="json")
        except TypeError:
            dumped = model_dump()
        return dumped if isinstance(dumped, dict) else None
    if isinstance(response, dict):
        return response
    return None


def _annotation_link(annotation: Any) -> dict | None:
    payload = _analytics_dict(annotation)
    nested = _analytics_dict(payload.get("url_citation"))
    source = nested or payload
    url = source.get("url")
    if not url:
        return None
    return {
        "url": url,
        "title": source.get("title") or payload.get("title"),
        "snippet": source.get("snippet") or payload.get("snippet"),
    }


def extract_openai_search_analytics(
    response: Any,
    rendered_output: Any = None,
    *,
    include_raw_response: bool = False,
) -> dict[str, Any]:
    """Extract OpenAI candidates and citations without inferring usage.

    ``include_raw_response`` is opt-in: it attaches the full
    ``response.model_dump(mode="json")`` payload (plus a couple of extra
    provider-identity fields) for callers that persist raw responses for
    later analysis (card generation, card refinement, Ask Caspr). Left off
    by default so callers that don't need it (e.g. the chat path) don't pay
    for holding the full response body in memory.
    """
    payload = _analytics_dict(response)
    output = payload.get("output") or []
    candidate_links: list[dict[str, Any]] = []
    cited_links: list[dict[str, Any]] = []
    provider_queries: list[str] = []
    search_call_count = 0
    final_message_index = next(
        (
            index
            for index in range(len(output) - 1, -1, -1)
            if _analytics_dict(output[index]).get("type") == "message"
        ),
        None,
    )

    for output_index, item in enumerate(output):
        item_dict = _analytics_dict(item)
        item_type = item_dict.get("type")
        if item_type == "web_search_call":
            call_index = search_call_count
            search_call_count += 1
            action = _analytics_dict(item_dict.get("action"))
            query = action.get("query") or item_dict.get("query")
            queries = action.get("queries") or item_dict.get("queries")
            # A single web_search_call can batch multiple queries under
            # `action.queries`, with `action.query` simply echoing the first
            # one. Prefer the (complete) list and fall back to the single
            # query only when no list is present, to avoid double-counting.
            if isinstance(queries, str) and queries:
                provider_queries.append(queries)
            elif isinstance(queries, list) and queries:
                provider_queries.extend(
                    value for value in queries if isinstance(value, str) and value
                )
            elif isinstance(query, str) and query:
                provider_queries.append(query)

            results = []
            for result_group in (
                item_dict.get("results"),
                action.get("results"),
                action.get("sources"),
            ):
                if isinstance(result_group, list):
                    results.extend(result_group)
            call_id = item_dict.get("call_id") or item_dict.get("id") or action.get("call_id")
            for result_rank, result in enumerate(results, 1):
                result_dict = _analytics_dict(result)
                if result_dict.get("url"):
                    candidate_links.append(
                        {
                            "url": result_dict["url"],
                            "title": result_dict.get("title"),
                            "snippet": (
                                result_dict.get("snippet") or result_dict.get("description")
                            ),
                            "search_call_id": call_id,
                            "search_call_index": call_index,
                            "result_rank": result_rank,
                        }
                    )
        elif item_type == "message":
            for block in item_dict.get("content") or []:
                block_dict = _analytics_dict(block)
                for annotation in block_dict.get("annotations") or []:
                    link = _annotation_link(annotation)
                    if link:
                        link["citation_order"] = len(cited_links)
                        cited_links.append(link)
                if rendered_output is None and output_index == final_message_index:
                    rendered_value = block_dict.get("parsed")
                    if rendered_value is None:
                        rendered_value = block_dict.get("text") or block_dict.get("output_text")
                    for url in _urls_in_rendered_output(rendered_value):
                        cited_links.append({"url": url, "citation_order": len(cited_links)})

    for url in _urls_in_rendered_output(rendered_output):
        cited_links.append({"url": url, "citation_order": len(cited_links)})

    usage = _analytics_dict(payload.get("usage"))
    result = {
        "provider": "openai",
        "provider_response_id": payload.get("id"),
        "provider_queries": provider_queries,
        "model_used": payload.get("model"),
        "status": payload.get("status") or "succeeded",
        "usage_metadata": usage or None,
        "search_call_count": search_call_count,
        "candidate_links": candidate_links,
        "cited_links": cited_links,
    }
    if include_raw_response:
        result["previous_response_id"] = payload.get("previous_response_id")
        result["response_created_at"] = payload.get("created_at")
        result["raw_response"] = _raw_response_json(response)
    return result


def extract_perplexity_search_analytics(response: Any) -> dict[str, Any]:
    """Extract Perplexity candidates and citations as independent lists."""
    payload = _analytics_dict(response)
    candidate_links: list[dict[str, Any]] = []
    for rank, result in enumerate(payload.get("search_results") or [], 1):
        result_dict = _analytics_dict(result)
        if result_dict.get("url"):
            candidate_links.append(
                {
                    "url": result_dict["url"],
                    "title": result_dict.get("title"),
                    "snippet": (result_dict.get("snippet") or result_dict.get("description")),
                    "result_rank": rank,
                }
            )

    cited_links: list[dict[str, Any]] = []
    for citation in payload.get("citations") or []:
        citation_dict = _analytics_dict(citation)
        if isinstance(citation, str):
            citation_dict = {"url": citation}
        if citation_dict.get("url"):
            cited_links.append(
                {
                    "url": citation_dict["url"],
                    "title": citation_dict.get("title"),
                    "snippet": citation_dict.get("snippet"),
                    "citation_order": len(cited_links),
                }
            )

    queries = payload.get("queries") or payload.get("query")
    if isinstance(queries, str):
        provider_queries = [queries]
    elif isinstance(queries, list):
        provider_queries = [value for value in queries if isinstance(value, str) and value]
    else:
        provider_queries = []

    usage = _analytics_dict(payload.get("usage"))
    search_call_count = payload.get("search_call_count")
    if not isinstance(search_call_count, int):
        search_call_count = usage.get("num_search_queries")
    if not isinstance(search_call_count, int):
        search_call_count = 1 if payload.get("search_results") else 0
    return {
        "provider": "perplexity",
        "provider_response_id": payload.get("id"),
        "provider_queries": provider_queries,
        "model_used": payload.get("model"),
        "status": payload.get("status") or "succeeded",
        "usage_metadata": usage or None,
        "search_call_count": search_call_count,
        "candidate_links": candidate_links,
        "cited_links": cited_links,
    }


def extract_gemini_search_analytics(
    response: Any,
    model_used: str | None = None,
) -> dict[str, Any]:
    """Extract Gemini search queries/citations from either response shape.

    Supports both the legacy ``generate_content`` response (grounding metadata
    at ``candidates[0].grounding_metadata``, carrying ``web_search_queries``
    and ``grounding_chunks`` with a ``web``/``retrieved_context`` payload) and
    the newer Interactions API ``Interaction`` object (``steps`` array with
    ``google_search_call`` steps for issued queries and a ``model_output``
    step whose text content block carries ``url_citation``/``file_citation``
    annotations instead of grounding chunks).
    """
    payload = _analytics_dict(response)

    if "steps" in payload:
        return _extract_gemini_search_analytics_from_interaction(payload, model_used=model_used)

    candidates = payload.get("candidates") or []
    candidate_payload = _analytics_dict(candidates[0]) if candidates else {}
    grounding = _analytics_dict(candidate_payload.get("grounding_metadata"))

    provider_queries: list[str] = []
    queries = grounding.get("web_search_queries")
    if isinstance(queries, list):
        provider_queries.extend(q for q in queries if isinstance(q, str) and q)

    candidate_links: list[dict[str, Any]] = []
    chunks = grounding.get("grounding_chunks") or []
    for rank, chunk in enumerate(chunks, 1):
        chunk_dict = _analytics_dict(chunk)
        web = _analytics_dict(chunk_dict.get("web"))
        retrieved = _analytics_dict(chunk_dict.get("retrieved_context"))
        source = web or retrieved
        url = source.get("uri") or source.get("url")
        if not url:
            continue
        candidate_links.append(
            {
                "url": url,
                "title": source.get("title"),
                "snippet": (source.get("text") or "")[:200] or None,
                "result_rank": rank,
            }
        )

    usage = _analytics_dict(payload.get("usage_metadata"))
    search_call_count = len(provider_queries) or (1 if candidate_links else 0)

    return {
        "provider": "gemini",
        "provider_response_id": payload.get("response_id") or payload.get("id"),
        "provider_queries": provider_queries,
        "model_used": model_used or payload.get("model_version"),
        "status": "succeeded",
        "usage_metadata": usage or None,
        "search_call_count": search_call_count,
        "candidate_links": candidate_links,
        "cited_links": list(candidate_links),
    }


def _extract_gemini_search_analytics_from_interaction(
    payload: dict[str, Any],
    model_used: str | None = None,
) -> dict[str, Any]:
    """Extract search analytics from an Interactions API ``Interaction`` payload."""
    steps = payload.get("steps") or []

    provider_queries: list[str] = []
    for step in steps:
        step_dict = _analytics_dict(step)
        if step_dict.get("type") != "google_search_call":
            continue
        arguments = step_dict.get("arguments") or {}
        queries = arguments.get("queries") if isinstance(arguments, dict) else None
        if isinstance(queries, list):
            provider_queries.extend(q for q in queries if isinstance(q, str) and q)

    candidate_links: list[dict[str, Any]] = []
    for step in steps:
        step_dict = _analytics_dict(step)
        if step_dict.get("type") != "model_output":
            continue
        for content_block in step_dict.get("content") or []:
            block_dict = _analytics_dict(content_block)
            if block_dict.get("type") != "text":
                continue
            for rank, annotation in enumerate(block_dict.get("annotations") or [], 1):
                ann_dict = _analytics_dict(annotation)
                ann_type = ann_dict.get("type")
                if ann_type == "url_citation" and ann_dict.get("url"):
                    candidate_links.append(
                        {
                            "url": ann_dict["url"],
                            "title": ann_dict.get("title"),
                            "snippet": None,
                            "result_rank": rank,
                        }
                    )
                elif ann_type == "file_citation" and ann_dict.get("file_name"):
                    candidate_links.append(
                        {
                            "url": ann_dict.get("source") or ann_dict["file_name"],
                            "title": ann_dict.get("file_name"),
                            "snippet": None,
                            "result_rank": rank,
                        }
                    )

    usage = _analytics_dict(payload.get("usage"))
    search_call_count = len(provider_queries) or (1 if candidate_links else 0)

    return {
        "provider": "gemini",
        "provider_response_id": payload.get("id"),
        "provider_queries": provider_queries,
        "model_used": model_used or payload.get("model"),
        "status": "succeeded",
        "usage_metadata": usage or None,
        "search_call_count": search_call_count,
        "candidate_links": candidate_links,
        "cited_links": list(candidate_links),
    }


def defer_provider_citations(entry: dict[str, Any]) -> dict[str, Any]:
    """Keep provider citations private until an entry produces the final card."""
    entry["_provider_cited_links"] = list(entry.get("cited_links") or [])
    entry["cited_links"] = []
    return entry


def collect_openai_search_analytics(
    response: Any,
    collector: list[dict[str, Any]] | None,
    operation_id: str | None,
) -> dict[str, Any] | None:
    """Append analytics for one successful OpenAI web-enabled response.

    Used exclusively by card generation, card refinement, and Ask Caspr, so
    this is also where the raw response payload is captured for the
    ``web_search_raw_responses`` table (via ``include_raw_response=True``).
    The chat path calls ``extract_openai_search_analytics`` directly instead
    and does not persist raw responses.
    """
    if collector is None:
        return None
    try:
        entry = defer_provider_citations(
            extract_openai_search_analytics(response, {}, include_raw_response=True)
        )
        entry["operation_id"] = operation_id
        entry["attempt_number"] = len(collector) + 1
        collector.append(entry)
        logger.info(
            "[web_search_analytics] Collected OpenAI search analytics | "
            f"operation_id={operation_id} attempt={entry['attempt_number']} "
            f"provider={entry.get('provider')} status={entry.get('status')} "
            f"candidates={len(entry.get('candidate_links') or [])} "
            f"provider_citations={len(entry.get('_provider_cited_links') or [])} "
            f"raw_response_captured={entry.get('raw_response') is not None}"
        )
        return entry
    except Exception:
        logger.exception("[web_search_analytics] Failed to collect OpenAI web-search analytics")
        return None


def collect_perplexity_search_analytics(
    response_dict: Any,
    collector: list[dict[str, Any]] | None,
    operation_id: str | None,
) -> dict[str, Any] | None:
    """Append analytics for one successful Perplexity response."""
    if collector is None:
        return None
    try:
        entry = defer_provider_citations(extract_perplexity_search_analytics(response_dict))
        entry["operation_id"] = operation_id
        entry["attempt_number"] = len(collector) + 1
        collector.append(entry)
        logger.info(
            "[web_search_analytics] Collected Perplexity search analytics | "
            f"operation_id={operation_id} attempt={entry['attempt_number']} "
            f"provider={entry.get('provider')} status={entry.get('status')} "
            f"candidates={len(entry.get('candidate_links') or [])} "
            f"provider_citations={len(entry.get('_provider_cited_links') or [])}"
        )
        return entry
    except Exception:
        logger.exception("[web_search_analytics] Failed to collect Perplexity web-search analytics")
        return None


def collect_gemini_search_analytics(
    response: Any,
    collector: list[dict[str, Any]] | None,
    operation_id: str | None,
    model_used: str | None = None,
) -> dict[str, Any] | None:
    """Append analytics for one successful Gemini grounded response."""
    if collector is None:
        return None
    try:
        entry = defer_provider_citations(
            extract_gemini_search_analytics(response, model_used=model_used)
        )
        entry["operation_id"] = operation_id
        entry["attempt_number"] = len(collector) + 1
        collector.append(entry)
        logger.info(
            "[web_search_analytics] Collected Gemini search analytics | "
            f"operation_id={operation_id} attempt={entry['attempt_number']} "
            f"provider={entry.get('provider')} status={entry.get('status')} "
            f"candidates={len(entry.get('candidate_links') or [])} "
            f"provider_citations={len(entry.get('_provider_cited_links') or [])}"
        )
        return entry
    except Exception:
        logger.exception("[web_search_analytics] Failed to collect Gemini web-search analytics")
        return None


def enrich_terminal_search_analytics(
    collector: list[dict[str, Any]],
    rendered_card: Any,
) -> dict[str, Any] | None:
    """Attribute terminal provider citations and final rendered URLs only.

    The returned ``cited_links`` intentionally concatenates
    ``_provider_cited_links`` (from OpenAI's own ``url_citation``
    annotations) with every URL the regex fallback finds in
    ``rendered_card`` — the same URL commonly appears in both, since the
    final rendered content usually embeds the annotated URL directly.
    Deduplication is NOT done here; it happens at persistence time in
    ``app.admin.repository_web_search.py::_normalize_links`` (keyed by canonical URL,
    cited role only), so this function's raw, possibly-duplicated list is
    still useful for callers that want the unmodified provider vs.
    rendered-text signal.
    """
    terminal_entry = next(
        (
            entry
            for entry in reversed(collector)
            if entry.get("status") in {"succeeded", "completed", "success"}
        ),
        None,
    )
    if terminal_entry is None:
        return None

    cited_links: list[dict[str, Any]] = []
    for link in terminal_entry.get("_provider_cited_links") or []:
        link_dict = _analytics_dict(link)
        if link_dict.get("url"):
            cited_link = dict(link_dict)
            cited_link["citation_order"] = len(cited_links)
            cited_links.append(cited_link)
    for url in _urls_in_rendered_output(rendered_card):
        cited_links.append({"url": url, "citation_order": len(cited_links)})
    terminal_entry["cited_links"] = cited_links
    logger.info(
        "[web_search_analytics] Enriched terminal search analytics | "
        f"operation_id={terminal_entry.get('operation_id')} "
        f"attempt={terminal_entry.get('attempt_number')} "
        f"provider={terminal_entry.get('provider')} cited={len(cited_links)}"
    )
    return terminal_entry


def log_scheduled_analytics_batch(
    trigger_source: str,
    collector: list[dict[str, Any]],
    *,
    operation_id: str | None = None,
    section_name: str | None = None,
    card_id: str | None = None,
) -> None:
    """Log a summary before flushing collected card-generation analytics."""
    if not collector:
        return
    logger.info(
        "[web_search_analytics] Flushing search analytics batch | "
        f"trigger={trigger_source} operation_id={operation_id} "
        f"entries={len(collector)} section={section_name} card_id={card_id}"
    )


_SEARCH_ANALYTICS_SCHEDULE_FIELDS = {
    "candidate_links",
    "cited_links",
    "operation_id",
    "attempt_number",
    "provider",
    "provider_response_id",
    "provider_queries",
    "status",
    "error_type",
    "duration_ms",
    "search_call_count",
    "usage_metadata",
    "model_used",
    "previous_response_id",
    "response_created_at",
    "raw_response",
}


def search_analytics_schedule_kwargs(entry: dict[str, Any]) -> dict[str, Any]:
    """Return only fields accepted by ``log_web_search_event``."""
    return {key: value for key, value in entry.items() if key in _SEARCH_ANALYTICS_SCHEDULE_FIELDS}


def logical_card_id(card: Any) -> str | None:
    payload = _analytics_dict(card)
    sections = payload.get("section")
    if not isinstance(sections, list) or not sections:
        return None
    section = _analytics_dict(sections[0])
    card_id = section.get("id")
    return str(card_id) if card_id else None
