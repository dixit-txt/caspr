"""refine_request_router.py — LangGraph state machine that tracks the cards a
report emits while it is being generated and turns a free-text "change the
report" request into the exact JSON payload the refiner expects.

Where it sits
-------------
This lives on the agent side, next to ``model.py``. While a report is
generating, ``model.py`` streams ``generate_report`` card events
(``{"name": "generate_report", "card_db": <card>, "card_type": ...}``).
Separately, a follow-up chat message asking to change that in-progress report is
captured by ``model.py``'s ``respond_during_report`` node and emitted as a
``post_report_edit_request`` event whose ``request`` field is the user's verbatim
message.

This module consumes both:

1. Every card event is fed to :meth:`RefineRequestRouter.ingest_card_event`. The
   graph parses it into a lightweight *tracked-card* record — ids, names and
   plain-text content only, never the whole card JSON — and accumulates it in the
   graph state (persisted per report via a checkpointer).

2. Each captured edit request is fed to
   :meth:`RefineRequestRouter.route_edit_request`. One LLM call decides which
   tracked section / subsection the user means and rewrites the request as a
   self-contained instruction. The result is emitted as a
   ``refine_request_routed`` custom stream event whose ``fe_json_for_refine``
   field is the payload the refiner takes.

This module NEVER calls a refiner and NEVER touches any refiner file. It only
tracks cards, routes requests, and emits an event. Applying the change is done
downstream by the refiner (the ask-caspr service) off that event / the queued
request — its ``fe_json_for_refine`` shape matches ask-caspr's
``build_refine_fe_json``.
"""

from __future__ import annotations

from typing import Annotated, Any, Dict, List, Literal, Optional, TypedDict
from uuid import uuid7
import re

from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, Field

from src.config.constants import (
    ANTHROPIC_LLM,
    ANTHROPIC_MODEL_ID,
    OPENAI_CHAT_MODEL_ID,
    OPENAI_LLM_LANGCHAIN,
)
from src.config.log_helper import setup_logging
from src.core.observability.llm_response_logger import save_raw_llm_response

logger = setup_logging(__name__)

# Custom stream event this module emits once a request has been routed. The API
# layer forwards it to the frontend and queues `fe_json_for_refine` for the
# refiner (ask-caspr service) to apply.
ROUTED_EVENT_NAME = "refine_request_routed"

# Card types that are never a refinement target (structural, not content).
_NON_TARGET_CARD_TYPES = {"title", "subtitle", "toc"}

_CITATION_LINK_RE = re.compile(r"\[([^\]]*?)\]\((?:https?://|/)[^)\s]+\)")
_MULTI_BLANKLINE_RE = re.compile(r"\n{3,}")

# How much of each section / subsection body to show the routing model. The match
# is on meaning, so a snippet is enough and keeps the prompt bounded.
_MAX_SECTION_CHARS_IN_PROMPT = 700
_MAX_SUBSECTION_CHARS_IN_PROMPT = 350


# ---------------------------------------------------------------------------
# Tracked-card records — the parsed shape kept in state
# ---------------------------------------------------------------------------

class TrackedSubsection(TypedDict):
    id: str
    name: str
    content: str


class TrackedCard(TypedDict):
    card_id: str          # stable key we upsert on — the section id when present
    card_type: str        # "section" | "title" | "subtitle" | "toc" | "es" | ...
    section_id: str
    section_name: str
    section_content: str  # plain text, citation links flattened to their label
    subsections: List[TrackedSubsection]


def _plain_text(content: Any) -> str:
    """Flatten card content to readable plain text.

    Handles the string / content-block-list / ``{"content": ...}`` shapes cards
    use, drops inline citation links (keeping their visible label) and collapses
    runs of blank lines.
    """
    if content is None:
        return ""
    if isinstance(content, dict):
        content = content.get("content", "")
    if isinstance(content, list):
        content = "\n".join(
            part.get("text", "") if isinstance(part, dict) else str(part)
            for part in content
        )
    if not isinstance(content, str):
        content = str(content)
    text = _CITATION_LINK_RE.sub(lambda m: m.group(1), content)
    text = _MULTI_BLANKLINE_RE.sub("\n\n", text)
    return text.strip()


def parse_card_event(event: Dict[str, Any]) -> Optional[TrackedCard]:
    """Turn a ``model.py`` ``generate_report`` card event into a tracked record.

    Returns ``None`` for frames that carry no usable card (status frames,
    heartbeats, fully-empty placeholder cards).
    """
    if not isinstance(event, dict):
        return None
    card = event.get("card_db")
    if not isinstance(card, dict):
        return None
    card_type = str(event.get("card_type") or "section")

    sections = card.get("section") or []
    section = sections[0] if isinstance(sections, list) and sections else {}
    if not isinstance(section, dict):
        section = {}
    section_id = str(section.get("id") or "")
    section_name = (section.get("name") or "").strip()
    section_content = _plain_text(section.get("content"))

    subsections: List[TrackedSubsection] = []
    for sub in card.get("sub_sections") or []:
        if not isinstance(sub, dict):
            continue
        sub_name = (sub.get("name") or "").strip()
        sub_content = _plain_text(sub.get("content"))
        if not sub_name and not sub_content:
            continue  # empty placeholder subsection cards carry one of these
        subsections.append(
            {"id": str(sub.get("id") or ""), "name": sub_name, "content": sub_content}
        )

    if not section_name and not section_content and not subsections:
        return None

    card_id = section_id or (
        f"{card_type}:{section_name}" if section_name else f"{card_type}:{uuid7()}"
    )
    return {
        "card_id": card_id,
        "card_type": card_type,
        "section_id": section_id,
        "section_name": section_name,
        "section_content": section_content,
        "subsections": subsections,
    }


def _merge_tracked_cards(
    existing: Optional[List[TrackedCard]],
    incoming: Optional[List[TrackedCard]],
) -> List[TrackedCard]:
    """State reducer: upsert incoming cards by ``card_id`` (newest wins), keeping
    first-seen order so the tracked list mirrors the report's reading order."""
    merged = list(existing or [])
    if not incoming:
        return merged
    index = {c["card_id"]: i for i, c in enumerate(merged)}
    for card in incoming:
        key = card["card_id"]
        if key in index:
            merged[index[key]] = card
        else:
            index[key] = len(merged)
            merged.append(card)
    return merged


# ---------------------------------------------------------------------------
# Graph state
# ---------------------------------------------------------------------------

class RefineRouterState(TypedDict, total=False):
    chat_id: str
    report_id: str
    report_title: str
    # Accumulated across every card event for this report.
    cards: Annotated[List[TrackedCard], _merge_tracked_cards]
    # Transient per-invocation input / output. Nodes clear their own input so a
    # later invocation of the other kind is routed correctly.
    incoming_cards: List[Dict[str, Any]]
    pending_request: str
    routed: Optional[Dict[str, Any]]


# ---------------------------------------------------------------------------
# LLM routing output
# ---------------------------------------------------------------------------

class RoutedRefineTarget(BaseModel):
    """Which part of the report a change request applies to."""

    target_kind: Literal["section", "subsection", "general", "unknown"] = Field(
        description=(
            "'section' when the user wants a whole section changed, 'subsection' for a "
            "single subsection, 'general' when the change applies to the whole report "
            "(tone, length, formatting everywhere), 'unknown' when no tracked section "
            "plausibly matches (e.g. they ask about a section that has not been "
            "generated yet)."
        )
    )
    section_id: str = Field(
        default="",
        description=(
            "id of the matched section. For a subsection match this is the PARENT "
            "section's id. Empty for 'general' / 'unknown'."
        ),
    )
    subsection_id: str = Field(
        default="",
        description="id of the matched subsection. Empty unless target_kind is 'subsection'.",
    )
    matched_name: str = Field(
        default="",
        description="Human-readable name of the matched section or subsection.",
    )
    refine_instruction: str = Field(
        description=(
            "The user's request rewritten as a clear, self-contained instruction the "
            "refiner can apply to that section without the surrounding conversation. "
            "Preserve the user's intent exactly; do not widen or narrow the scope."
        )
    )
    confidence: float = Field(
        description="0..1 confidence that this is the section/subsection the user meant."
    )
    reasoning: str = Field(default="", description="One sentence explaining the match.")


_ROUTER_SYSTEM_PROMPT = """You route a user's free-text request to change an in-progress report to the single section (or subsection) of that report it applies to.

You are given:
- The report's sections and subsections generated so far — each with its id and a snippet of its content.
- One user message asking for a change.

Decide:
- The ONE section or subsection the user means. Match on meaning, not just shared words. If they clearly mean a subsection, set target_kind="subsection" and return BOTH its id (subsection_id) and its parent section's id (section_id).
- If the request is about the whole report (overall tone, length, formatting everywhere), set target_kind="general".
- If nothing generated so far plausibly matches (for example they ask to change a section that does not exist yet), set target_kind="unknown".
- refine_instruction: rewrite the user's request as a clear, self-contained instruction the refiner can apply to that section on its own. Keep their intent exactly — do not invent scope, do not add requirements they did not state.

Return only the structured object."""


class RefineRequestRouter:
    """Per-report tracker + router. Create one when report generation starts,
    feed it every card event, and hand it each captured edit request."""

    def __init__(
        self,
        chat_id: str,
        report_id: Optional[str] = None,
        report_title: str = "",
        *,
        user_id: Optional[str] = None,
        event_writer=None,
    ) -> None:
        """
        Args:
            chat_id: Chat the report belongs to.
            report_id: In-progress report id (used as the checkpointer thread id
                so tracked cards persist across many ``ingest_card_event`` calls).
            report_title: Optional title, shown to the routing model for context.
            user_id: Optional, for LLM cost attribution.
            event_writer: Optional stream writer captured by the caller (the same
                one the report stream uses). Falls back to
                ``langgraph.config.get_stream_writer()`` inside a graph run.
        """
        self.chat_id = chat_id
        self.report_id = report_id
        self.report_title = report_title or ""
        self.user_id = user_id
        self._event_writer = event_writer
        self._thread_id = report_id or chat_id or str(uuid7())
        self._checkpointer = MemorySaver()
        self.graph = self._build_graph()

    # -- public API ------------------------------------------------------

    async def ingest_card_event(self, event: Dict[str, Any]) -> None:
        """Record one ``generate_report`` card event in the tracked-card state."""
        await self.ingest_card_events([event])

    async def ingest_card_events(self, events: List[Dict[str, Any]]) -> None:
        """Record a batch of card events in one graph step."""
        if not events:
            return
        await self.graph.ainvoke(
            {**self._seed(), "incoming_cards": list(events)}, self._config
        )

    async def route_edit_request(self, raw_user_message: str) -> Optional[Dict[str, Any]]:
        """Route one free-text change request against the tracked cards.

        Emits a ``refine_request_routed`` custom event and returns that same
        payload (``fe_json_for_refine`` included), or ``None`` for an empty
        message.
        """
        message = (raw_user_message or "").strip()
        if not message:
            return None
        result = await self.graph.ainvoke(
            {**self._seed(), "pending_request": message}, self._config
        )
        return result.get("routed")

    async def handle_post_report_edit_request(
        self, event: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        """Route straight from a ``post_report_edit_request`` event (model.py).

        The event's ``request`` field is the verbatim user message (a string;
        the older dict shape is tolerated).
        """
        request = (event or {}).get("request")
        if isinstance(request, dict):
            request = request.get("raw_user_message", "")
        if not request:
            return None
        return await self.route_edit_request(str(request))

    async def tracked_cards(self) -> List[TrackedCard]:
        """Snapshot of every card tracked for this report so far."""
        snapshot = await self.graph.aget_state(self._config)
        return list((snapshot.values or {}).get("cards", []))

    # -- graph ---------------------------------------------------------------

    def _build_graph(self):
        builder = StateGraph(RefineRouterState)
        builder.add_node("ingest_cards", self._ingest_cards_node)
        builder.add_node("route_request", self._route_request_node)
        builder.add_conditional_edges(
            START,
            self._entry,
            {"ingest_cards": "ingest_cards", "route_request": "route_request", END: END},
        )
        builder.add_edge("ingest_cards", END)
        builder.add_edge("route_request", END)
        return builder.compile(checkpointer=self._checkpointer)

    @staticmethod
    def _entry(state: RefineRouterState) -> str:
        if state.get("incoming_cards"):
            return "ingest_cards"
        if (state.get("pending_request") or "").strip():
            return "route_request"
        return END

    async def _ingest_cards_node(self, state: RefineRouterState) -> Dict[str, Any]:
        parsed: List[TrackedCard] = []
        for event in state.get("incoming_cards") or []:
            record = parse_card_event(event)
            if record is not None:
                parsed.append(record)
        if parsed:
            logger.info(
                "[refine_router] tracked %d card(s) [%s] | chat_id=%s report_id=%s",
                len(parsed),
                ", ".join(c["section_name"] or c["card_type"] for c in parsed),
                self.chat_id,
                self.report_id,
            )
        # Clear the input so a later route-only invocation is not re-routed here.
        return {"cards": parsed, "incoming_cards": []}

    async def _route_request_node(self, state: RefineRouterState) -> Dict[str, Any]:
        message = (state.get("pending_request") or "").strip()
        cards: List[TrackedCard] = state.get("cards") or []
        if not message:
            return {"pending_request": "", "routed": None}

        try:
            routed = await self._route_with_llm(cards, message)
        except Exception as exc:  # noqa: BLE001 — never let routing crash the caller
            logger.error(
                "[refine_router] routing failed: %s | chat_id=%s report_id=%s",
                exc, self.chat_id, self.report_id,
            )
            routed = RoutedRefineTarget(
                target_kind="unknown",
                refine_instruction=message,
                confidence=0.0,
                reasoning="routing failed",
            )

        fe_json = self._to_fe_json_for_refine(routed, message)
        payload = {
            "name": ROUTED_EVENT_NAME,
            "status": "routed" if routed.target_kind != "unknown" else "unresolved",
            "chat_id": self.chat_id,
            "report_id": self.report_id,
            "raw_user_message": message,
            "target": {
                "kind": routed.target_kind,
                "section_id": routed.section_id,
                "subsection_id": routed.subsection_id,
                "matched_name": routed.matched_name,
                "confidence": routed.confidence,
                "reasoning": routed.reasoning,
            },
            "fe_json_for_refine": fe_json,
        }
        self._writer()(payload)
        logger.info(
            "[refine_router] routed edit request -> kind=%s name=%r confidence=%.2f | chat_id=%s",
            routed.target_kind, routed.matched_name, routed.confidence, self.chat_id,
        )
        return {"pending_request": "", "routed": payload}

    # -- helpers -----------------------------------------------------------

    @property
    def _config(self) -> Dict[str, Any]:
        return {"configurable": {"thread_id": self._thread_id}}

    def _seed(self) -> Dict[str, Any]:
        return {
            "chat_id": self.chat_id,
            "report_id": self.report_id,
            "report_title": self.report_title,
        }

    def _writer(self):
        if self._event_writer is not None:
            return self._event_writer
        try:
            return get_stream_writer()
        except Exception:  # noqa: BLE001 — no active graph stream writer
            return lambda *_a, **_k: None

    def _build_router_prompt(self, cards: List[TrackedCard], raw_message: str) -> str:
        lines: List[str] = []
        if self.report_title:
            lines.append(f'Report title: "{self.report_title}"')
            lines.append("")
        lines.append("SECTIONS GENERATED SO FAR:")
        content_cards = [c for c in cards if c["card_type"] not in _NON_TARGET_CARD_TYPES]
        if not content_cards:
            lines.append("(none yet — no report sections have been generated)")
        for card in content_cards:
            section_id = card["section_id"] or "(no id)"
            name = card["section_name"] or card["card_type"]
            lines.append(f'- section_id={section_id} | name="{name}"')
            if card["section_content"]:
                lines.append(
                    f"    content: {card['section_content'][:_MAX_SECTION_CHARS_IN_PROMPT]}"
                )
            for sub in card["subsections"]:
                sub_id = sub["id"] or "(no id)"
                lines.append(f'    - subsection_id={sub_id} | name="{sub["name"]}"')
                if sub["content"]:
                    lines.append(
                        f"        content: {sub['content'][:_MAX_SUBSECTION_CHARS_IN_PROMPT]}"
                    )
        lines.append("")
        lines.append(f"USER REQUEST:\n{raw_message}")
        return "\n".join(lines)

    async def _route_with_llm(
        self, cards: List[TrackedCard], raw_message: str
    ) -> RoutedRefineTarget:
        messages = [
            SystemMessage(content=_ROUTER_SYSTEM_PROMPT),
            HumanMessage(content=self._build_router_prompt(cards, raw_message)),
        ]
        try:
            structured = ANTHROPIC_LLM.with_structured_output(
                RoutedRefineTarget, include_raw=True
            )
            result = await structured.ainvoke(messages)
            raw = result.get("raw") if isinstance(result, dict) else None
            parsed = result.get("parsed") if isinstance(result, dict) else result
            if raw is not None:
                save_raw_llm_response(
                    raw, ANTHROPIC_MODEL_ID, "Routing an in-progress report edit request",
                    self.chat_id, user_id=self.user_id,
                )
            if isinstance(parsed, RoutedRefineTarget):
                return parsed
            raise ValueError("anthropic returned no parsed RoutedRefineTarget")
        except Exception as exc:  # noqa: BLE001 — fall back to OpenAI
            logger.warning(
                "[refine_router] anthropic routing failed (%s); falling back to openai | chat_id=%s",
                exc, self.chat_id,
            )
            structured = OPENAI_LLM_LANGCHAIN.with_structured_output(
                RoutedRefineTarget, include_raw=True
            )
            result = await structured.ainvoke(messages)
            raw = result.get("raw") if isinstance(result, dict) else None
            parsed = result.get("parsed") if isinstance(result, dict) else result
            if raw is not None:
                save_raw_llm_response(
                    raw, OPENAI_CHAT_MODEL_ID,
                    "Routing an in-progress report edit request (backup)",
                    self.chat_id, user_id=self.user_id,
                )
            if isinstance(parsed, RoutedRefineTarget):
                return parsed
            raise ValueError("openai returned no parsed RoutedRefineTarget")

    def _to_fe_json_for_refine(
        self, routed: RoutedRefineTarget, raw_message: str
    ) -> Dict[str, Any]:
        """Build the ``fe_json_for_refine`` dict the refiner takes.

        Same shape as ask-caspr's ``build_refine_fe_json``: a section-level change
        is ``{"id", "refine_or_delete_prompt"}``; a subsection change is
        ``{"id": <parent section id>, "subsection": {"id", "refine_or_delete_prompt"}}``
        with no top-level prompt. This module only emits it — the refiner
        (ask-caspr service) consumes it, unchanged.
        """
        instruction = (routed.refine_instruction or raw_message or "").strip()
        if routed.target_kind == "subsection" and routed.subsection_id:
            return {
                "id": routed.section_id or routed.subsection_id,
                "subsection": {
                    "id": routed.subsection_id,
                    "refine_or_delete_prompt": instruction,
                },
            }
        # section-level, or a best-effort payload for general / unknown
        return {
            "id": routed.section_id,
            "refine_or_delete_prompt": instruction,
        }
