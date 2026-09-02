"""functionality_context.py: stable, user-facing "functionality" tagging for LLM cost.

Why this exists
---------------
Every LLM call is logged to ``costtracker`` with a free-text ``context`` label
(e.g. ``card_utils.generate_drl``). Those labels are great for engineers but
too granular / unstable for a cost dashboard that non-technical people read.

This module maps every LLM call to ONE of a small, stable set of *product
functionalities* (chat, report generation, refine, refine visualization,
executive summary, infographic, PPTX generation, Ask CASPR).

How the functionality is resolved (in ``resolve_functionality``), highest
precedence first:

1. An explicit value passed by the caller.
2. A value set on the current execution scope via :func:`functionality_scope`
   (a ``ContextVar``; set once at an API entry point and inherited by all
   nested async tasks / threadpool work).
3. A deterministic fallback derived from the call's ``context`` / ``agent_name``
   label. This guarantees a sensible bucket even when a shared helper runs
   somewhere the ``ContextVar`` did not propagate (e.g. a raw thread pool).

NOTE (per product decision): we deliberately use a plain Python ``Enum`` here,
NOT a SQLAlchemy Enum / Postgres native enum. The ``costtracker.functionality``
column is a plain string, so adding / renaming buckets never needs a DB enum
migration — we just change this file.
"""
from __future__ import annotations

import contextvars
from contextlib import contextmanager
from enum import Enum
from typing import Iterator, Optional


class Functionality(str, Enum):
    """Stable, user-facing product functionalities for cost attribution.

    The *value* is what gets written to ``costtracker.functionality`` and what
    the dashboard groups by. Keep values short + stable; use
    :data:`FUNCTIONALITY_LABELS` for the human-friendly display name.
    """

    CHAT = "chat"
    REPORT_GENERATION = "report_generation"
    REFINE = "refine"
    REFINE_VISUALIZATION = "refine_visualization"
    EXECUTIVE_SUMMARY = "executive_summary"
    INFOGRAPHIC = "infographic"
    PPTX_GENERATION = "pptx_generation"
    ASK_CASPR = "ask_caspr"
    LEARNING_BRAIN = "learning_brain"
    OTHER = "other"


# Human-friendly names for the dashboard (non-technical readers).
FUNCTIONALITY_LABELS: dict[str, str] = {
    Functionality.CHAT.value: "Chat",
    Functionality.REPORT_GENERATION.value: "Report Generation",
    Functionality.REFINE.value: "Refine",
    Functionality.REFINE_VISUALIZATION.value: "Refine Visualization",
    Functionality.EXECUTIVE_SUMMARY.value: "Executive Summary",
    Functionality.INFOGRAPHIC.value: "Infographic",
    Functionality.PPTX_GENERATION.value: "PPTX Generation",
    Functionality.ASK_CASPR.value: "Ask CASPR",
    Functionality.LEARNING_BRAIN.value: "Learning Brain",
    Functionality.OTHER.value: "Other",
}

# Internal / legacy keys that must never appear as-is on the admin dashboard.
# OpenAI's tool is still called ``web_search`` internally; admins see Learning Brain.
_FUNCTIONALITY_ALIASES: dict[str, str] = {
    "websearch": Functionality.LEARNING_BRAIN.value,
    "web_search": Functionality.LEARNING_BRAIN.value,
    "web search": Functionality.LEARNING_BRAIN.value,
}

# Raw costtracker.context / agent_name strings that map to "Learning Brain"
# for by-feature dashboard responses. Matched case-insensitively.
_LEARNING_BRAIN_CONTEXT_PATTERNS: tuple[str, ...] = (
    "websearch",
    "web_search",
    "web search",
    "retrieve_latest_info",
    "retrieve_latest",
    "learning brain",
    "learning_brain",
    "using learning brain",
    "latest information on the topic",
)

_LEARNING_BRAIN_DISPLAY = "Learning Brain"

# Exact-match replacements for dashboard display (case-insensitive key lookup).
# Used to clean up raw context/agent_name labels that contain internal details
# (e.g. "web research") that should not be surfaced on the admin dashboard.
_FEATURE_LABEL_REPLACEMENTS: dict[str, str] = {
    "writing report section content using web research": "Writing report section content",
}


def remap_feature_label(raw: Optional[str]) -> str:
    """Return the user-facing display label for a costtracker context / agent_name.

    Any string that is a raw internal web-search label (``websearch``,
    ``retrieve_latest_info``, etc.) is shown as ``Learning Brain``.
    Strings in ``_FEATURE_LABEL_REPLACEMENTS`` are substituted with their
    cleaned display version. All other values pass through unchanged.
    """
    if not raw:
        return raw or ""
    key = raw.strip().lower()
    if key in _LEARNING_BRAIN_CONTEXT_PATTERNS:
        return _LEARNING_BRAIN_DISPLAY
    replacement = _FEATURE_LABEL_REPLACEMENTS.get(key)
    if replacement:
        return replacement
    return raw


def normalize_functionality(functionality: Optional[str]) -> str:
    """Canonical functionality key for dashboard / logging (aliases remapped)."""
    if not functionality:
        return Functionality.OTHER.value
    key = str(functionality).strip().lower()
    return _FUNCTIONALITY_ALIASES.get(key, key)


def label_for(functionality: Optional[str]) -> str:
    """Display name for a functionality value (falls back to Other).

    ``websearch`` / ``web_search`` are always shown as ``Learning Brain``.
    """
    key = normalize_functionality(functionality)
    return FUNCTIONALITY_LABELS.get(
        key, key.replace("_", " ").title()
    )


# ---------------------------------------------------------------------------
# ContextVar scope — set once at an entry point, inherited by nested work.
# ---------------------------------------------------------------------------

_functionality_var: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "caspr_functionality", default=None
)


def get_functionality() -> Optional[str]:
    """Current scoped functionality value, or ``None`` if unscoped."""
    return _functionality_var.get()


def set_functionality(functionality: "Functionality | str | None") -> contextvars.Token:
    """Set the current functionality; returns a token to restore it later."""
    value = functionality.value if isinstance(functionality, Functionality) else functionality
    return _functionality_var.set(value)


def reset_functionality(token: contextvars.Token) -> None:
    """Restore the functionality set before the matching :func:`set_functionality`."""
    try:
        _functionality_var.reset(token)
    except (ValueError, LookupError):
        # Token created in a different context (e.g. across threads) — ignore.
        pass


@contextmanager
def functionality_scope(functionality: "Functionality | str") -> Iterator[None]:
    """Scope a block of work to one functionality.

    Use at API entry points / sub-flow boundaries. All LLM calls made while
    this scope is active are attributed to ``functionality`` (unless a call
    passes its own explicit value)::

        with functionality_scope(Functionality.REPORT_GENERATION):
            await generate_report(...)
    """
    token = set_functionality(functionality)
    try:
        yield
    finally:
        reset_functionality(token)


# ---------------------------------------------------------------------------
# Deterministic fallback: derive functionality from a context/agent label.
# ---------------------------------------------------------------------------

# Ordered (substrings, functionality). FIRST MATCH WINS, so order matters: the
# more specific / disambiguating phrases must come BEFORE broader ones (e.g.
# "summary for ask caspr" is report-building, so it is checked before the
# generic "ask caspr question" rule). Matching is case-insensitive on the
# combined "context agent_name model" string.
#
# The ``context`` labels stored in costtracker are the human-readable phrases
# passed to ``save_raw_llm_response`` at each call site (e.g. "Writing report
# section content using web research"), so the needles below are those phrases.
# Code-identifier fragments (card_utils, pptx_generator, …) are kept too for
# robustness.
#
# IMPORTANT: in the ``costtracker`` rows the ``agent_name`` is usually the SAME
# human phrase as ``context`` (e.g. "Building an interactive table
# visualization"), NOT the code module. So the needles below are the *human
# phrases* passed to ``save_raw_llm_response`` at each call site. Code-identifier
# fragments are kept as an extra safety net.
#
# The buckets are ordered so the more specific flows (ask caspr, refine,
# executive summary, infographic, chat) claim their calls BEFORE the broad
# report-generation catch-all at the bottom. Every phrase currently emitted by
# the product is covered here — ``other`` should only ever appear for a brand
# new, not-yet-mapped call site (which is the signal to add it below).
_CONTEXT_RULES: list[tuple[tuple[str, ...], Functionality]] = [
    # Built during report generation to seed Ask CASPR — NOT an Ask CASPR call.
    (("summary for ask caspr",), Functionality.REPORT_GENERATION),
    # Ask CASPR Q&A (all variants: card / documents / web research / backup).
    (("ask caspr question", "ask_caspr", "askcaspr"), Functionality.ASK_CASPR),
    # PPTX deck generation.
    (("pptx", "presentation", "slide deck"), Functionality.PPTX_GENERATION),
    # Executive-summary updater (post-generation refresh). NOTE: "polishing the
    # executive summary" is part of INITIAL generation and is intentionally NOT
    # here — it falls through to report_generation below.
    (
        ("executive summary needs an update", "checking whether the executive summary",
         "updating the executive summary", "executive summary after report changes",
         "executive_summary_updater"),
        Functionality.EXECUTIVE_SUMMARY,
    ),
    # Visualization refinement (user edits an existing chart/table viz).
    (
        ("visualization based on feedback", "refined table visualization",
         "refined chart visualization", "regenerating a chart image after edits",
         "refine_visualiz", "viz_refine", "visualizer_refine", "graph_maker_refine",
         "table_visualizer_refine"),
        Functionality.REFINE_VISUALIZATION,
    ),
    # Card refinement (user refines a report card). Includes the refiner's
    # post-edit chart refresh ("...after card edits").
    (
        ("refining a report card", "after card edits", "refine_card", "refiner"),
        Functionality.REFINE,
    ),
    # Infographic / one-pager (one_pager.py).
    (
        ("infographic", "one-page", "one_pager", "illustration for the report"),
        Functionality.INFOGRAPHIC,
    ),
    # Learning Brain (product name for internal web_search / retrieve_latest).
    # Must come before chat so "Looking up the latest information…" is not
    # mis-attributed to chat.
    (
        ("latest information on the topic", "using learning brain", "learning brain",
         "retrieve_latest", "web_search", "websearch"),
        Functionality.LEARNING_BRAIN,
    ),
    # Interactive chat / planning agent (NOT the report-writing pipeline).
    (
        ("main research request", "title for the chat",
         "low-balance research request", "question from uploaded documents",
         "normal_flow", "chat_title", "query_document"),
        Functionality.CHAT,
    ),
    # Report generation pipeline — the broad catch-all for everything that
    # happens while building / publishing a report: outline, section writing,
    # summaries, charts & tables, flowcharts, posters, cover images, styling,
    # document search, findings, dashboards, primary research, due diligence.
    (
        (
            # outline / sections / summaries
            "report outline", "report section content", "report section",
            "summarizing a report section", "section summaries",
            "overall report summary", "research preview", "report structure",
            "polishing the executive summary", "fixing report section",
            # tables & charts built during generation
            "title for a table", "chart type for the data", "chart image",
            "table should become a chart", "table needs a visual chart",
            "visual chart", "chart instructions", "draw a chart",
            "flowchart or diagram", "interactive chart", "chart visualization",
            "table visualization",
            # cards
            "fixing a report card", "report card",
            # findings / metadata / dashboards
            "key findings from the report", "industry or sector",
            "dashboard highlights",
            # imagery / posters / publishing / styling
            "cover image for the report", "report poster", "poster",
            "report text for publishing", "publishing", "published report",
            "matching poster", "for the report", "the report",
            # document search subsystem (used while writing sections)
            "document search", "search across documents", "searching documents",
            "search documents", "document context", "into an answer",
            "answer a question",
            # research domains
            "primary research document", "due diligence", "research analysis",
            # code-identifier safety net
            "card_utils", "card_fixer", "viz_pipeline", "html_graph_maker",
            "html_table_visualizer", "publish", "grep", "primary_research",
            "due_diligence", "content_processor", "poster_generator",
        ),
        Functionality.REPORT_GENERATION,
    ),
]


def functionality_from_context(
    context: Optional[str] = None,
    agent_name: Optional[str] = None,
    model_name: Optional[str] = None,
) -> str:
    """Best-effort bucket from the raw call labels. Never raises; defaults OTHER."""
    haystack = " ".join(
        str(part) for part in (context, agent_name, model_name) if part
    ).lower()
    if not haystack:
        return Functionality.OTHER.value
    for needles, functionality in _CONTEXT_RULES:
        if any(needle in haystack for needle in needles):
            return functionality.value
    return Functionality.OTHER.value


def resolve_functionality(
    *,
    explicit: "Functionality | str | None" = None,
    context: Optional[str] = None,
    agent_name: Optional[str] = None,
    model_name: Optional[str] = None,
) -> str:
    """Resolve the functionality bucket for one LLM call.

    Precedence: explicit arg → scoped ContextVar → derived from context label.
    """
    if explicit is not None:
        raw = explicit.value if isinstance(explicit, Functionality) else str(explicit)
        return normalize_functionality(raw)
    scoped = get_functionality()
    if scoped:
        return normalize_functionality(scoped)
    return normalize_functionality(
        functionality_from_context(context, agent_name, model_name)
    )
