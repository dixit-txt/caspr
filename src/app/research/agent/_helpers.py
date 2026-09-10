"""Pure helper functions for the Casper agent (no ``self``)."""

import asyncio
import json
import time
from typing import Any

from app.cards.service_cards import modify_report_layout, parse_markdown_report_layout
from app.core.logging import setup_logging
from app.research.agent._state import CARD_STYLES, DEFAULT_CARD_STYLE, REPORT_TIERS

logger = setup_logging(__name__)


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


# ---------------------------------------------------------------------------
# Streaming preview of a proposed report layout
# ---------------------------------------------------------------------------

# Placeholder cards `parse_markdown_report_layout` always emits between the
# title and the first real section. They carry no model-authored content, so
# the stream can emit them the moment the title line lands.
_LAYOUT_PLACEHOLDER_KEYS = ("subtitle", "table_of_contents", "executive_summary")


def _decode_json_string_prefix(fragment: str) -> str:
    """Decode as much of a partial JSON string body as is safely complete.

    ``fragment`` is the raw text between the opening quote of a JSON string and
    wherever the stream happens to have stopped — so it can end mid-escape
    (``"…\\`` or ``"…\\u00e"``). Decoding stops before any such tail and resumes
    on the next call, once more characters have arrived.
    """
    out = []
    i = 0
    length = len(fragment)
    while i < length:
        ch = fragment[i]
        if ch != "\\":
            out.append(ch)
            i += 1
            continue
        # An escape needs at least one more character, and \uXXXX needs five.
        if i + 1 >= length:
            break
        code = fragment[i + 1]
        if code == "u":
            if i + 5 >= length:
                break
            try:
                out.append(chr(int(fragment[i + 2 : i + 6], 16)))
            except ValueError:
                out.append(fragment[i : i + 6])
            i += 6
            continue
        out.append(
            {"n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f"}.get(code, code)
        )
        i += 2
    return "".join(out)


def _partial_json_string_value(partial_args: str, key: str) -> str:
    """Pull the (possibly unfinished) value of one string key out of partial JSON.

    Tool-call arguments stream in as JSON fragments, so ``json.loads`` only
    works once the model has finished writing them. This reads just the one key
    the layout preview needs, returning whatever of its value has arrived.
    """
    marker = f'"{key}"'
    start = partial_args.find(marker)
    if start == -1:
        return ""
    cursor = start + len(marker)
    # Skip whitespace and the colon, then find the opening quote of the value.
    while cursor < len(partial_args) and partial_args[cursor] in " \t\r\n:":
        cursor += 1
    if cursor >= len(partial_args) or partial_args[cursor] != '"':
        return ""
    cursor += 1

    # Walk to the closing quote, honouring escapes; a missing one just means the
    # value is still streaming and the whole remainder is its body so far.
    end = cursor
    while end < len(partial_args):
        ch = partial_args[end]
        if ch == "\\":
            end += 2
            continue
        if ch == '"':
            break
        end += 1
    return _decode_json_string_prefix(partial_args[cursor:end])


class ProposedLayoutStreamer:
    """Turn a streaming ``propose_report_layout`` tool call into per-card events.

    The model writes the layout as one Markdown string inside the tool-call
    arguments, so nothing is parseable until the arguments are complete — which
    is exactly the wait this class removes. Feed it the accumulated argument
    fragments as they arrive and it emits each card the moment that card's
    Markdown is provably finished: the title as soon as its line ends, and each
    ``## Section`` once the *next* heading proves its bullets are all in (a
    section's subsections follow its heading, so it is only closed by what comes
    after it). ``finish`` flushes the trailing section.

    Cards are built with the same ``parse_markdown_report_layout`` /
    ``modify_report_layout`` pair the tool itself uses, so a streamed card is
    identical in shape to the ones in the final ``propose_report_layout`` event
    and renders through the same frontend path.
    """

    def __init__(self, emit, proposal_id: str = ""):
        self._emit = emit
        self.proposal_id = proposal_id
        self.started = False
        self.finished = False
        self.index = 0
        self.title = ""
        # Markdown consumed so far; everything after it is still in flight.
        self._consumed = 0
        self._markdown = ""

    def _emit_event(self, name: str, **payload) -> None:
        try:
            self._emit({"name": name, "proposal_id": self.proposal_id, **payload})
        except Exception:  # noqa: BLE001 - a preview must never break the turn
            logger.warning(
                "[propose_report_layout] Failed to emit streaming layout event "
                f"'{name}' | proposal_id={self.proposal_id}",
                exc_info=True,
            )

    def start(self) -> None:
        """Announce that a layout is about to stream in. Safe to call repeatedly."""
        if self.started or self.finished:
            return
        self.started = True
        self._emit_event("proposed_report_layout_start")

    def _emit_card(self, item: dict) -> None:
        cards = modify_report_layout([item])
        if not cards:
            return
        self._emit_event("proposed_report_layout_card", index=self.index, card=cards[0])
        self.index += 1

    def _emit_section(self, section_markdown: str) -> None:
        """Emit one finished ``## Section`` block (heading plus its bullets)."""
        parsed = parse_markdown_report_layout(section_markdown)
        # parse_markdown_report_layout always prefixes the four fixed cards;
        # only the section it parsed out of this block is new.
        for item in parsed[len(_LAYOUT_PLACEHOLDER_KEYS) + 1 :]:
            self._emit_card(item)

    def feed(self, partial_args: str) -> None:
        """Consume the accumulated tool-call arguments seen so far."""
        if self.finished:
            return
        markdown = _partial_json_string_value(partial_args, "report_layout")
        if not markdown:
            return
        self.start()
        self._markdown = markdown

        while True:
            boundary = self._next_boundary()
            if boundary is None:
                return
            block = self._markdown[self._consumed : boundary]
            self._consumed = boundary
            self._flush_block(block)

    def _next_boundary(self) -> int | None:
        """Index at which the block starting at ``_consumed`` is provably closed.

        Before the title card that is the newline ending the ``# `` line; after
        it, the start of the next ``## `` heading.
        """
        rest = self._markdown[self._consumed :]
        if self.index == 0:
            newline = rest.find("\n")
            if newline == -1 or not rest.lstrip().startswith("# "):
                return None
            return self._consumed + newline + 1
        # Look for a "## " at the start of a line, past the current block's own.
        search_from = 1 if rest.startswith("## ") else 0
        next_heading = rest.find("\n## ", search_from)
        if next_heading == -1:
            return None
        return self._consumed + next_heading + 1

    def _flush_block(self, block: str) -> None:
        if self.index == 0:
            self.title = block.strip().lstrip("#").strip()
            self._emit_card({"title": self.title or "Report"})
            for key in _LAYOUT_PLACEHOLDER_KEYS:
                self._emit_card({key: ""})
            return
        if block.strip():
            self._emit_section(block)

    def finish(
        self, report_layout: str = "", report_title: str = "", aborted: bool = False
    ) -> None:
        """Flush the trailing section and close the stream.

        ``report_layout`` is the complete Markdown once the tool call is done —
        passing it lets the last section be emitted from the authoritative text
        rather than from whatever the last fragment happened to contain.
        ``aborted`` marks a stream cut short (the model call failed part-way), so
        a consumer knows the preview it built is incomplete and must be replaced
        by whatever the tool itself emits.
        """
        if self.finished:
            return
        if report_layout:
            self._markdown = report_layout
        if not self.started:
            # Nothing ever streamed (a non-streaming provider, or an empty
            # layout): stay silent and let the tool's own event stand alone.
            self.finished = True
            return

        tail = self._markdown[self._consumed :]
        self._consumed = len(self._markdown)
        if self.index == 0:
            self._flush_block(tail.split("\n", 1)[0] if tail else "")
            tail = tail.split("\n", 1)[1] if "\n" in tail else ""
        if tail.strip():
            self._emit_section(tail)

        self.finished = True
        self._emit_event(
            "proposed_report_layout_end",
            report_title=report_title or self.title,
            cards=self.index,
            aborted=aborted,
        )
