"""Shared state, schemas, and constants for the Casper agent."""

from typing import Any

from langgraph.graph import MessagesState
from pydantic import BaseModel, Field

GENERIC_TOOL_ERROR_MSG = "Something went wrong while processing your request. Please try again."

# Marker stamped on the synthetic ToolMessages the layout-ordering guard emits
# when the model tries to call ask_user / retrieve before propose_report_layout.
# Used to recognise those nudges later (bounded retry, ask_user pause exemption).
_LAYOUT_GUARD_MARKER = "[layout-guard]"

# ---------------------------------------------------------------------------
# Chat-while-a-report-is-generating support.
#
# When `retrieve` has fired and report analysis is running, follow-up chat
# messages are routed (from START) to `respond_during_report` instead of the
# normal `report_or_respond` planner. That node keeps the full chat context but
# binds NO report tools — it cannot start, restart or re-plan a report. Its only
# tool is `flag_report_change_request`, used to capture any change the user asks
# for in the report currently being built. Each captured request is emitted as a
# `post_report_edit_request` custom stream event; applying it is done later
# (downstream, via ask-caspr) — this node only captures and acknowledges.
# ---------------------------------------------------------------------------

FLAG_REPORT_CHANGE_REQUEST_SCHEMA = {
    "type": "function",
    "function": {
        "name": "flag_report_change_request",
        "description": (
            "Call this ONLY when the user explicitly asks to change, add, remove, "
            "reword or restructure something in the report that is CURRENTLY being "
            "generated. Capture every distinct change the user wants as its own entry. "
            "Do NOT call this for general questions, status checks, clarifications or "
            "discussion — only when the user wants the in-progress report altered. "
            "After calling it, briefly confirm to the user that the change has been "
            "noted and will be applied to the report."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "requests": {
                    "type": "array",
                    "description": "One entry per distinct change the user asked for.",
                    "items": {
                        "type": "string",
                        "description": "The user's own words requesting this change, verbatim.",
                    },
                }
            },
            "required": ["requests"],
        },
    },
}

_RESPOND_DURING_REPORT_NOTE = """

**IMPORTANT — A REPORT IS CURRENTLY BEING GENERATED FOR THIS CHAT.**
The full report is being written right now in the background. For this message:
- You CANNOT start, restart, re-plan, or re-scope a report, and you have no tools to do so. Never tell the user you are "starting" or "regenerating" the report.
- Answer the user's questions and talk with them normally, using everything you know about this chat and the report that is being built.
- If the user explicitly asks to change / add / remove / reword / restructure something in the report being generated, call `flag_report_change_request` immediately (with no text before the call), capturing each distinct change as its own entry. Then reply with one or two short sentences telling the user the change has been noted and will be applied to the report once the current pass finishes.
- If the user is only asking a question, checking status, or chatting, just reply — do NOT call `flag_report_change_request`.
- Do not promise the change is already done; it is queued to be applied.
- Never mention tool names, nodes, or internal mechanisms to the user.
"""


class DocumentQueryResponse(BaseModel):
    """Response model for document queries"""

    answer: str = Field(description="The answer to the user's question about the document")
    citations: list[str] = Field(
        default_factory=list, description="List of citation URLs from retrieved sources"
    )


class SearchQueries(BaseModel):
    """Model for generating search queries based on user instructions and report layout"""

    queries: list[str] = Field(
        description="List of search queries generated for each topic", min_items=1
    )


class UpdatedLayoutSection(BaseModel):
    """One section of a web-refreshed report layout."""

    section: str = Field(
        description="Section heading text WITHOUT numbering, e.g. 'Competitive Landscape'."
    )
    sub_sections: list[str] = Field(
        description=(
            "Subsection names under this section, in reading order. Empty list when the "
            "original section had no subsections (brief-style layouts)."
        )
    )


class UpdatedProposedReportLayout(BaseModel):
    """Web-refreshed version of a proposed report layout, in the same shape as the original."""

    title: str = Field(description="Report title in 7 words or less, in English.")
    sections: list[UpdatedLayoutSection] = Field(
        description="All sections of the updated layout, in reading order."
    )
    change_summary: str = Field(
        description=(
            "One or two sentences naming what current information drove the changes. "
            "Empty string when the search confirmed the layout needed no changes."
        )
    )


class CasperState(MessagesState):
    """Graph state: the message list, plus what the report-config pause needs.

    Both extra keys exist so the pause can survive the request that created it.
    ``layout_pair`` runs before the interrupt and never runs again, so the pair
    it produced reaches the resumed run only by being checkpointed here; the
    resumed ``report_config`` reads it back onto the fresh ``Casper``.
    """

    report_layout_pair: dict[str, Any]
    report_config: dict[str, Any]


#: The two report tiers. The confirmed one is the sole authority (R10).
REPORT_TIERS = ("study", "brief")

#: v1 accepts one card-writing style; anything else falls back to it (R13, R14).
CARD_STYLES = ("investor",)
DEFAULT_CARD_STYLE = "investor"
