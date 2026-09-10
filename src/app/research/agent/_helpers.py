"""Pure helper functions for the Casper agent (no ``self``)."""

import asyncio
import json
import time
from typing import Any

from app.research.agent._state import CARD_STYLES, DEFAULT_CARD_STYLE, REPORT_TIERS


def _sibling_tier(tier: str) -> str:
    """The tier the user did *not* ask for — the one minted at the pause."""
    return "brief" if tier == "study" else "study"


def _keep_finished_chat_history(messages: list[Any]) -> list[Any]:
    """Drop an unanswered tool-call tail; keep the human turn that started it.

    ``get_processing_state`` rebuilds history every turn. An ``ask_user`` or
    ``retrieve`` that has not yet produced a follow-up AI used to delete the
    preceding human message as well, so the next turn forgot the topic. The
    dangling AI / tool messages are what must not be replayed; the user's
    words stay.
    """
    if not messages:
        return []

    should_include = [True] * len(messages)
    for idx, msg in enumerate(messages):
        if not isinstance(msg, dict):
            continue
        kind = msg.get("type")
        meta = msg.get("response_metadata") or {}
        stop_reason = (
            meta.get("stopReason") or meta.get("stop_reason") or meta.get("finish_reason") or ""
        )
        if kind != "ai" or stop_reason not in ("tool_use", "tool_calls"):
            continue

        if (
            idx + 1 < len(messages)
            and isinstance(messages[idx + 1], dict)
            and messages[idx + 1].get("type") == "tool"
        ):
            end = idx + 1
            while (
                end < len(messages)
                and isinstance(messages[end], dict)
                and messages[end].get("type") == "tool"
            ):
                end += 1
            if (
                end < len(messages)
                and isinstance(messages[end], dict)
                and messages[end].get("type") == "ai"
            ):
                continue
            should_include[idx:end] = [False] * (end - idx)
        else:
            should_include[idx] = False

    kept = [msg for msg, keep in zip(messages, should_include) if keep]
    while kept and isinstance(kept[0], dict) and kept[0].get("type") == "tool":
        kept.pop(0)
    if kept and isinstance(kept[0], dict) and kept[0].get("type") == "system":
        kept = kept[1:]
    return kept


def _normalize_report_config(submission: Any, default_tier: str = "study") -> dict[str, Any]:
    """Coerce a Confirm submission into the shape the rest of the run reads.

    Every submitted field is kept, including the ones v1 does not act on (R12):
    storing them now means the features that use output formats, language, or
    data sources read configuration instead of re-deriving it.
    """
    payload = submission if isinstance(submission, dict) else {}

    tier = str(payload.get("report_tier") or payload.get("report_type") or "").strip().lower()
    if tier not in REPORT_TIERS:
        tier = default_tier if default_tier in REPORT_TIERS else "study"

    style = str(payload.get("style") or "").strip().lower()
    if style not in CARD_STYLES:
        style = DEFAULT_CARD_STYLE

    output_formats = payload.get("output_formats") or []
    if isinstance(output_formats, str):
        output_formats = [output_formats]

    data_sources = payload.get("data_sources") or []
    if isinstance(data_sources, str):
        data_sources = [data_sources]

    return {
        "report_tier": tier,
        "style": style,
        # Stored and otherwise inert in v1 (R13).
        "output_formats": [str(f) for f in output_formats],
        "language": str(payload.get("language") or "English"),
        "data_sources": [str(s) for s in data_sources],
    }


def _message_text(message) -> str:
    """Best-effort plain text of a LangChain message (Anthropic returns content blocks)."""
    content = getattr(message, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") == "text" and block.get("text"):
                parts.append(block["text"])
        return "\n".join(parts)
    return ""


def _conversation_excerpt(messages: list | None, max_chars: int = 6000) -> str:
    """Render the user/assistant turns of a conversation as a compact transcript.

    Only the tail is kept for long conversations — the most recent turns are the ones a
    report layout has to satisfy.
    """
    lines = []
    for message in messages or []:
        msg_type = getattr(message, "type", "")
        if msg_type not in ("human", "ai"):
            continue
        text = _message_text(message).strip()
        if not text:
            continue
        lines.append(f"{'User' if msg_type == 'human' else 'Caspr'}: {text}")

    transcript = "\n\n".join(lines)
    if len(transcript) > max_chars:
        transcript = "…\n\n" + transcript[-max_chars:]
    return transcript


def _proposed_layout_tool_result(
    report_title: str, report_layout: str, web_updated: bool = False
) -> str:
    """Build the string `propose_report_layout` returns to the model.

    The layout is echoed back so it stays on the table for the model after context
    compaction and — because tool results are persisted with the chat history — on later
    turns too, where a fresh Casper has no memory of this one.
    """
    refreshed_note = (
        "\n\nIMPORTANT — THIS LAYOUT HAS BEEN UPDATED WITH CURRENT INFORMATION:\n"
        "The layout you drafted was checked against current sources and revised. The version "
        "below REPLACES the one you wrote — it is the only layout on the table now. Use it "
        "verbatim (plus any edits the user asks for) as `report_layout` when you call "
        "`retrieve`, and treat it as the base for any further revisions."
        if web_updated
        else ""
    )
    header = "CURRENT PROPOSED REPORT LAYOUT" + (" (web-updated)" if web_updated else "")
    return (
        "The proposed report layout has been shown to the user as a structured "
        "preview. This is the layout currently on the table — keep it in mind "
        "and pass it (with any edits the user asks for) as `report_layout` when "
        "you eventually call `retrieve`. Continue with focused refinement "
        "questions, or present the FINAL REPORT REQUIREMENTS summary and ask for "
        "confirmation. Call this tool again with the full Markdown layout any "
        "time the layout changes."
        f"{refreshed_note}\n\n----- {header} (title: {report_title}) -----\n"
        f"{report_layout.strip()}"
    )


# ---------------------------------------------------------------------------
# Heartbeat helpers for retrieve_latest_info
# ---------------------------------------------------------------------------

_HEARTBEAT_STOP_WORDS = {
    "the",
    "a",
    "an",
    "of",
    "in",
    "on",
    "and",
    "for",
    "to",
    "with",
    "is",
    "are",
    "was",
    "latest",
    "current",
    "recent",
    "about",
}


def _topic_from_query(query: str, max_words: int = 7) -> str:
    words = [w for w in query.split() if w.lower() not in _HEARTBEAT_STOP_WORDS]
    return " ".join(words[:max_words]).rstrip(".,;") or query[:50]


def _heartbeat_messages(search_query: str, progress_updates: list[str]) -> list[str]:
    """Build ordered heartbeat lines for a search from the model's own progress_updates."""
    cleaned: list[str] = []
    seen: set = set()
    for msg in progress_updates or []:
        text = (msg or "").strip()
        key = text.lower()
        if text and key not in seen:
            seen.add(key)
            cleaned.append(text)
    if cleaned:
        return cleaned
    return [f"Still gathering sources on {_topic_from_query(search_query)}"]


async def _run_heartbeat(
    search_query: str,
    progress_updates: list[str],
    event_writer,
    started: float,
    interval: float = 4.0,
    tool_name: str = "retrieve_latest_info",
) -> None:
    """
    Emit heartbeat events via event_writer while a search/lookup is in flight.

    Paces the model's own progress_updates lines out over time, one per tick.
    If the lookup outlasts the supplied lines, the LAST line is held until the
    work completes (the caller cancels this task as soon as it finishes).  Falls
    back to a topic-derived line if the model supplied none.

    `tool_name` must match the name the API layer uses to map to the correct
    SSE event type (e.g. "retrieve_latest_info" → learning_brain_latest_*,
    "query_document" → learning_brain_document_*).
    """
    messages = _heartbeat_messages(search_query, progress_updates)
    tick = 0
    while True:
        await asyncio.sleep(interval)
        elapsed = time.time() - started
        # Play the model's lines in order; once exhausted, hold the last line
        # until the lookup completes.
        msg = messages[min(tick, len(messages) - 1)]
        event_writer(
            {
                "name": tool_name,
                "status": "heartbeat",
                "message": msg,
                "elapsed": f"{elapsed:.0f}s",
            }
        )
        tick += 1


# ---------------------------------------------------------------------------
# Gate node helpers — enforce one tool call per step
# ---------------------------------------------------------------------------

_TRANSIENT_FLAG = "_caspr_transient"

_GATE_REJECTION_TEMPLATE = (
    "Please call only ONE tool at a time. "
    "You just requested {n} tool calls in a single step ({names}). "
    "Pick the single most important action right now and call only that one tool."
)


def _is_transient(message) -> bool:
    """Return True if this message is a transient gate-correction artifact."""
    meta = getattr(message, "additional_kwargs", None) or {}
    return bool(meta.get(_TRANSIENT_FLAG))


def _compact_history(messages: list) -> list:
    """Drop transient gate-correction messages while preserving all real tool-call/result pairs."""
    return [m for m in messages if not _is_transient(m)]


def _normalize_tool_call(call: dict[str, Any]) -> dict[str, Any]:
    """Ensure tool-call args are a dict (some providers serialise them as a JSON string)."""
    normalised = dict(call)
    args = normalised.get("args")
    if isinstance(args, str):
        try:
            normalised["args"] = json.loads(args)
        except json.JSONDecodeError:
            normalised["args"] = {}
    elif args is None:
        normalised["args"] = {}
    return normalised
