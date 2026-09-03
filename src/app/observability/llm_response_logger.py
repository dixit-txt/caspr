"""llm_response_logger.py: Utility to extract LLM usage/token stats from a response.

Import `save_raw_llm_response` and call it right after any LLM call (langchain
`.invoke(...)` / `.ainvoke(...)`, or a raw OpenAI/Gemini/Anthropic/Perplexity SDK
call) to extract the response's usage/token metadata, tagged with which
agent/module made the call and which user/chat it was made for.

Only usage/token accounting info is extracted (not the full response body,
e.g. generated text/images/citations) to keep the returned payload small.

``save_raw_llm_response`` is fire-and-forget (extraction + ``costtracker`` persist):
- async context → ``asyncio.create_task`` (extract in a worker thread, persist
  on the same app event loop so the shared DB engine stays loop-safe)
- sync / worker-thread context → daemon thread that extracts sync then persists
  via a short-lived async engine (avoids "Future attached to a different loop")

Failures are logged and never raised to the caller.
"""
import asyncio
import datetime
import threading
from typing import Any, Dict, Optional

from app.core.logging import setup_logging

logger = setup_logging(__name__)

# Key names (case-insensitive) that hold usage/token accounting info across the
# various SDKs we call: langchain (usage_metadata), OpenAI Responses/Chat (usage),
# OpenAI built-in tool usage (tool_usage), Gemini (usageMetadata), Perplexity (usage).
_USAGE_KEYS = {
    "usage",
    "usage_metadata",
    "usagemetadata",
    "tool_usage",
}

# Per-image cost (USD) for image-generation models, since these calls don't
# carry token-based usage_metadata. Keyed by the exact model id/name string
# passed to save_raw_llm_response. Add entries here as pricing is confirmed.
_IMAGE_GEN_COST_PER_IMAGE_USD: Dict[str, float] = {
    "imagen-4.0-generate-001": 0.0400,
}

# Field names (in order of preference) whose list length represents the number
# of images returned by an image-generation call across SDKs: Gemini/Imagen
# (`generated_images`), OpenAI Images API (`data`).
_IMAGE_LIST_FIELDS = ("generated_images", "data")


def _count_generated_images(raw_data: Any) -> Optional[int]:
    """Best-effort count of images returned by an image-generation response."""
    if not isinstance(raw_data, dict):
        return None
    for field in _IMAGE_LIST_FIELDS:
        value = raw_data.get(field)
        if isinstance(value, list):
            return len(value)
    return None


def _extract_input_output_tokens(usage_data: Dict[str, Any]) -> "tuple[Optional[int], Optional[int]]":
    """Best-effort normalization of input/output token counts across SDK usage shapes.

    Different providers name these fields differently:
      - OpenAI Chat Completions / Perplexity: `prompt_tokens` (input) / `completion_tokens` (output)
      - OpenAI Responses API / langchain `usage_metadata` (incl. Anthropic): `input_tokens` / `output_tokens`
      - Gemini `usage_metadata` (legacy `generateContent`): `prompt_token_count` (input) /
        `candidates_token_count` (output), with `thoughts_token_count` (reasoning) also billed
        as output tokens.
      - Gemini `usage` (Interactions API): `total_input_tokens` / `total_output_tokens`, with
        `total_thought_tokens` (reasoning) also billed as output tokens.
    """
    for top_key in ("usage", "usage_metadata"):
        block = usage_data.get(top_key)
        if not isinstance(block, dict):
            continue

        if "input_tokens" in block or "output_tokens" in block:
            return block.get("input_tokens"), block.get("output_tokens")

        if "prompt_tokens" in block or "completion_tokens" in block:
            return block.get("prompt_tokens"), block.get("completion_tokens")

        if "prompt_token_count" in block or "candidates_token_count" in block:
            input_tokens = block.get("prompt_token_count")
            output_tokens = block.get("candidates_token_count")
            thoughts_tokens = block.get("thoughts_token_count") or 0
            if output_tokens is not None:
                output_tokens = output_tokens + thoughts_tokens
            return input_tokens, output_tokens

        if "total_input_tokens" in block or "total_output_tokens" in block:
            input_tokens = block.get("total_input_tokens")
            output_tokens = block.get("total_output_tokens")
            thoughts_tokens = block.get("total_thought_tokens") or 0
            if output_tokens is not None:
                output_tokens = output_tokens + thoughts_tokens
            return input_tokens, output_tokens

    return None, None


def _collect_usage_fields(obj: Any, found: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Recursively walk `obj` and collect any dict values found under usage-related keys."""
    if found is None:
        found = {}

    if isinstance(obj, dict):
        for key, value in obj.items():
            if isinstance(key, str) and key.lower() in _USAGE_KEYS:
                if key not in found or not found[key]:
                    found[key] = value
                elif isinstance(found[key], dict) and isinstance(value, dict):
                    merged = dict(value)
                    merged.update(found[key])
                    found[key] = merged
            else:
                _collect_usage_fields(value, found)
    elif isinstance(obj, list):
        for item in obj:
            _collect_usage_fields(item, found)

    return found


def _resolve_usage_metadata_blob(usage_data: Dict[str, Any]) -> Dict[str, Any]:
    """Prefer the nested ``usage_metadata`` block when present; else store the whole usage dict."""
    nested = usage_data.get("usage_metadata")
    if isinstance(nested, dict) and nested:
        return nested
    # Gemini / other casings collected as usagemetadata
    nested = usage_data.get("usagemetadata")
    if isinstance(nested, dict) and nested:
        return nested
    return usage_data


def _estimated_cost_from_payload(payload: Dict[str, Any]) -> Optional[float]:
    cost_block = payload.get("cost") if isinstance(payload.get("cost"), dict) else None
    usage_data = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
    if isinstance(cost_block, dict) and cost_block.get("estimated_cost_usd") is not None:
        try:
            return float(cost_block["estimated_cost_usd"])
        except (TypeError, ValueError):
            return None
    if usage_data.get("estimated_cost_usd") is not None:
        try:
            return float(usage_data["estimated_cost_usd"])
        except (TypeError, ValueError):
            return None
    return None


def _persist_kwargs_from_payload(
    payload: Dict[str, Any],
    *,
    timestamp: datetime.datetime,
) -> Dict[str, Any]:
    """Build kwargs for ``_persist_cost_tracker_row`` from an extracted payload."""
    meta = payload.get("metadata") or {}
    usage_data = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
    cost_block = payload.get("cost") if isinstance(payload.get("cost"), dict) else None
    return dict(
        timestamp=timestamp,
        model_name=meta.get("model_name") or "",
        context=meta.get("context") or "",
        functionality=meta.get("functionality"),
        agent_name=meta.get("agent_name"),
        chat_id=meta.get("chat_id"),
        user_id=meta.get("user_id"),
        usage_metadata=_resolve_usage_metadata_blob(usage_data),
        input_tokens=payload.get("input_tokens"),
        output_tokens=payload.get("output_tokens"),
        estimated_cost=_estimated_cost_from_payload(payload),
        cost_details=cost_block,
    )


async def _persist_cost_tracker_row(
    *,
    timestamp: datetime.datetime,
    model_name: str,
    context: str,
    functionality: Optional[str] = None,
    agent_name: Optional[str],
    chat_id: Optional[str],
    user_id: Optional[str],
    usage_metadata: Dict[str, Any],
    input_tokens: Optional[int],
    output_tokens: Optional[int],
    estimated_cost: Optional[float] = None,
    cost_details: Optional[Dict[str, Any]] = None,
) -> None:
    """Insert into ``costtracker`` using the shared app async engine/session."""
    try:
        from src.db.async_db_functions import insert_cost_tracker
        from app.core.db import async_session_scope

        async with async_session_scope() as session:
            result = await insert_cost_tracker(
                timestamp=timestamp,
                model_name=model_name,
                context=context,
                functionality=functionality,
                agent_name=agent_name,
                chat_id=chat_id,
                user_id=user_id,
                usage_metadata=usage_metadata,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                estimated_cost=estimated_cost,
                cost_details=cost_details,
                session=session,
            )
            if not result.get("success"):
                logger.error(
                    f"costtracker insert failed for context={context}, model={model_name}: "
                    f"{result.get('error')}"
                )
    except Exception as e:
        logger.error(
            f"costtracker persist failed for context={context}, model={model_name}: {e}",
            exc_info=True,
        )


async def _persist_cost_tracker_row_isolated(**kwargs: Any) -> None:
    """Persist using a short-lived engine bound to the current event loop.

    Used from sync/worker threads via ``asyncio.run`` so we never touch the
    shared app ENGINE (which belongs to the uvicorn loop).
    """
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
    from sqlalchemy.orm import sessionmaker

    from app.core.constants import DB_CONNECTION_LINK
    from src.db.async_db_functions import insert_cost_tracker

    engine = create_async_engine(
        DB_CONNECTION_LINK,
        echo=False,
        pool_size=1,
        max_overflow=0,
        pool_pre_ping=True,
    )
    factory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        async with factory() as session:
            result = await insert_cost_tracker(session=session, **{
                k: kwargs[k]
                for k in (
                    "timestamp",
                    "model_name",
                    "context",
                    "functionality",
                    "agent_name",
                    "chat_id",
                    "user_id",
                    "usage_metadata",
                    "input_tokens",
                    "output_tokens",
                    "estimated_cost",
                    "cost_details",
                )
                if k in kwargs
            })
            if not result.get("success"):
                logger.error(
                    f"costtracker isolated insert failed for context={kwargs.get('context')}, "
                    f"model={kwargs.get('model_name')}: {result.get('error')}"
                )
    except Exception as e:
        logger.error(
            f"costtracker isolated persist failed for context={kwargs.get('context')}, "
            f"model={kwargs.get('model_name')}: {e}",
            exc_info=True,
        )
    finally:
        await engine.dispose()


def _save_raw_llm_response_impl(
    response: Any,
    model_name: str,
    context: str,
    chat_id: Optional[str] = None,
    user_id: Optional[str] = None,
    agent_name: Optional[str] = None,
    agent_id: Optional[str] = None,
    functionality: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Extract usage/token/cost stats. Does not touch the DB. Never raises."""
    try:
        agent_name = agent_name or context
        agent_id = agent_id or agent_name

        if hasattr(response, "model_dump"):
            raw_data = response.model_dump(mode="json")
        elif hasattr(response, "dict"):
            raw_data = response.dict()
        else:
            raw_data = response

        usage_data = _collect_usage_fields(raw_data)

        # Image-generation calls have no token usage_metadata, but have a known
        # flat per-image price, so compute an estimated cost from the number of
        # images actually returned.
        cost_per_image = _IMAGE_GEN_COST_PER_IMAGE_USD.get(str(model_name or "").strip())
        if cost_per_image is not None:
            num_images = _count_generated_images(raw_data) or 0
            usage_data["estimated_cost_usd"] = round(num_images * cost_per_image, 4)
            usage_data["num_images"] = num_images
            usage_data["cost_per_image_usd"] = cost_per_image

        if not usage_data:
            usage_data = {"note": "No usage/token metadata found on this response."}

        input_tokens, output_tokens = _extract_input_output_tokens(usage_data)

        now_utc = datetime.datetime.now(datetime.timezone.utc)
        # Keep legacy string format in the returned payload until callers migrate;
        # DB column stores a proper timezone-aware datetime.
        timestamp_str = now_utc.strftime("%Y%m%d_%H%M%S_%f")

        payload = {
            "metadata": {
                "timestamp": timestamp_str,
                "model_name": model_name,
                "context": context,
                "functionality": functionality,
                "agent_name": agent_name,
                "agent_id": agent_id,
                "chat_id": chat_id,
                "user_id": user_id,
            },
            "usage": usage_data,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            # Internal: used by async/sync persist wrappers (not part of public JSON contract).
            "_db_timestamp": now_utc,
        }
        try:
            from app.observability.llm_cost_calculator import attach_cost_to_payload

            attach_cost_to_payload(payload)
        except Exception as cost_err:
            logger.warning(
                f"Failed to attach LLM cost for context={context}, model={model_name}: {cost_err}"
            )

        return payload
    except Exception as e:
        logger.error(f"Failed to extract raw LLM response usage for context={context}, model={model_name}: {e}")
        return None


async def _save_raw_llm_response_async(
    response: Any,
    model_name: str,
    context: str,
    chat_id: Optional[str] = None,
    user_id: Optional[str] = None,
    agent_name: Optional[str] = None,
    agent_id: Optional[str] = None,
    functionality: Optional[str] = None,
) -> None:
    """Extract off the event loop, then persist on the *current* loop (app-safe)."""
    try:
        payload = await asyncio.to_thread(
            _save_raw_llm_response_impl,
            response,
            model_name,
            context,
            chat_id,
            user_id,
            agent_name,
            agent_id,
            functionality,
        )
        if not payload:
            return

        ts = payload.pop("_db_timestamp", None) or datetime.datetime.now(datetime.timezone.utc)
        await _persist_cost_tracker_row(**_persist_kwargs_from_payload(payload, timestamp=ts))
    except Exception as e:
        logger.error(
            f"Background save_raw_llm_response failed for context={context}, "
            f"model={model_name}: {e}",
            exc_info=True,
        )


def save_raw_llm_response(
    response: Any,
    model_name: str,
    context: str,
    chat_id: Optional[str] = None,
    user_id: Optional[str] = None,
    agent_name: Optional[str] = None,
    agent_id: Optional[str] = None,
    functionality: Optional[str] = None,
) -> None:
    """Fire-and-forget extraction of LLM usage/token stats + ``costtracker`` insert.

    - Running event loop: ``asyncio.create_task`` (extract in worker thread, persist
      on the app loop with the shared engine).
    - No event loop (sync code / ``to_thread`` workers): daemon thread extracts
      sync and persists via an isolated short-lived async engine.

    Call sites keep the same sync ``save_raw_llm_response(...)`` form.

    ``functionality`` is the stable, user-facing cost bucket. It is resolved
    HERE (synchronously, in the caller's context) so the scoped ``ContextVar``
    is read before any work is dispatched to a background thread that would not
    inherit it. Precedence: explicit arg → scoped ContextVar → derived from the
    ``context`` / ``agent_name`` label.

    Returns:
        Always ``None`` (work is scheduled in the background).
    """
    try:
        from app.observability.functionality_context import resolve_functionality

        resolved_functionality = resolve_functionality(
            explicit=functionality,
            context=context,
            agent_name=agent_name,
            model_name=model_name,
        )
    except Exception:
        resolved_functionality = functionality

    kwargs = dict(
        response=response,
        model_name=model_name,
        context=context,
        chat_id=chat_id,
        user_id=user_id,
        agent_name=agent_name,
        agent_id=agent_id,
        functionality=resolved_functionality,
    )

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop is not None and loop.is_running():
        loop.create_task(_save_raw_llm_response_async(**kwargs))
        return None

    def _run_in_thread() -> None:
        try:
            payload = _save_raw_llm_response_impl(**kwargs)
            if not payload:
                return
            ts = payload.pop("_db_timestamp", None) or datetime.datetime.now(
                datetime.timezone.utc
            )
            persist_kwargs = _persist_kwargs_from_payload(payload, timestamp=ts)
            asyncio.run(_persist_cost_tracker_row_isolated(**persist_kwargs))
        except Exception as e:
            logger.error(
                f"Background save_raw_llm_response failed for context={context}, "
                f"model={model_name}: {e}",
                exc_info=True,
            )

    threading.Thread(
        target=_run_in_thread,
        daemon=True,
        name=f"llm-response-logger:{context}",
    ).start()
    return None


def strip_json_code_fence(text: str) -> str:
    """Strip a leading/trailing ```json ... ``` (or plain ``` ... ```) fence, if present.

    Gemini's Interactions API is expected to return raw JSON (no markdown) when
    ``response_format`` requests ``mime_type: application/json``, but some models
    (observed with gemini-2.5-flash) still wrap the JSON in a markdown code
    fence despite that. This is a defensive normalization applied before
    ``json.loads`` on any Gemini structured-output ``interaction.output_text``.
    """
    if not text:
        return text
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped[len("```"):]
        if stripped.lower().startswith("json"):
            stripped = stripped[len("json"):]
        stripped = stripped.lstrip("\n")
        if stripped.endswith("```"):
            stripped = stripped[: -len("```")]
        stripped = stripped.strip()
    return stripped
