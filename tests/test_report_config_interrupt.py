"""U4: the graph pauses before retrieval and resumes only on a submission.

The load-bearing risk here is LangGraph's re-execution semantics: on resume it
re-runs the interrupted node from its first line and runs nothing that
completed before it. Every node split in this feature rests on that, so these
tests drive the real ``Casper`` graph. Only what would reach the network is
stubbed — the two layout LLM calls, retrieval, and the domain subgraphs.
"""

from __future__ import annotations

from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver

from app.core.checkpointer import set_checkpointer
from app.research.agent import model as model_module
from app.research.agent.model import (
    Casper,
    _keep_finished_chat_history,
    _normalize_report_config,
    _sibling_tier,
)

pytestmark = pytest.mark.unit

STUDY_LAYOUT = "# UK Fintech\n\n## Market Size\n- Segments\n- Growth\n\n## Regulation\n- FCA\n"
BRIEF_LAYOUT = "# UK Fintech\n\n## Market Size\n\n## Regulation\n"


def _retrieve_call(report_type: str = "study") -> dict[str, Any]:
    return {
        "name": "retrieve",
        "id": "call_retrieve_1",
        "args": {
            "user_instructions": "Assess the UK fintech market.",
            "report_layout": STUDY_LAYOUT,
            "report_language": "English",
            "report_title": "UK Fintech",
            "domain_name": "default",
            "report_type": report_type,
        },
    }


#: What `propose_report_layout` left in history. The gate's layout-first guard
#: suppresses a retrieve step until one of these exists, so a run that is
#: supposed to reach the pause has to have shown a layout first.
PROPOSED_TOOL_RESULT = f"----- CURRENT PROPOSED REPORT LAYOUT -----\n{STUDY_LAYOUT}"


def _messages(tool_calls: list[dict[str, Any]]) -> list[Any]:
    return [
        HumanMessage(content="Write me a report on UK fintech."),
        AIMessage(
            content="Here is a layout.",
            id="ai_0",
            tool_calls=[
                {
                    "name": "propose_report_layout",
                    "id": "call_layout_1",
                    "args": {"report_layout": STUDY_LAYOUT, "report_title": "UK Fintech"},
                }
            ],
        ),
        ToolMessage(
            content=PROPOSED_TOOL_RESULT,
            tool_call_id="call_layout_1",
            name="propose_report_layout",
            id="tool_layout_1",
        ),
        AIMessage(content="Starting.", id="ai_1", tool_calls=tool_calls),
    ]


class _Harness:
    """One turn's worth of stubs, shared by every Casper the turn builds."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []
        self.updates: list[dict[str, Any]] = []
        self.retrieve_calls: list[dict[str, Any]] = []
        self.mint_calls = 0

    async def mint(self, report_layout, report_title, target_tier, messages=None) -> str:
        self.mint_calls += 1
        return BRIEF_LAYOUT if target_tier == "brief" else STUDY_LAYOUT

    async def refresh(self, report_layout, report_title="", messages=None):
        # "The refresh produced nothing usable" — both layouts must still be emitted.
        return None

    async def await_refresh(self) -> None:
        return None

    async def retrieve(
        self,
        user_instructions: str,
        report_layout: str,
        report_language: str,
        report_title: str,
        domain_name: str,
        report_type: str = "study",
    ):
        """Stands in for the retrieval pipeline. Signature mirrors the real tool,
        which is what the graph builds its tool schema from."""
        self.retrieve_calls.append(
            {
                "user_instructions": user_instructions,
                "report_layout": report_layout,
                "report_language": report_language,
                "report_title": report_title,
                "domain_name": domain_name,
                "report_type": report_type,
            }
        )
        return "", {"report_layout": report_layout, "domain_name": domain_name}

    async def domain_router(self, state):
        return state

    async def report_or_respond(self, state):
        """Leave the seeded tool step alone. The planner LLM decides *whether* to
        call retrieve; these tests are about what happens once it has."""
        return {"messages": []}

    def attach(self, instance: Casper) -> Casper:
        instance._mint_sibling_layout = self.mint
        instance._web_refreshed_layout = self.refresh
        instance._await_layout_refresh = self.await_refresh
        instance.retrieve = self.retrieve
        instance.domain_router = self.domain_router
        instance.report_or_respond = self.report_or_respond
        return instance

    async def drain(self, stream) -> None:
        async for mode, output in stream:
            if mode == "custom":
                self.events.append(output)
            elif mode == "updates":
                self.updates.append(output)

    def named(self, name: str) -> list[dict[str, Any]]:
        return [e for e in self.events if e.get("name") == name]


@pytest.fixture(autouse=True)
def _isolated_checkpointer():
    set_checkpointer(InMemorySaver())
    yield
    set_checkpointer(None)


@pytest.fixture
def make_casper(harness: _Harness):
    async def _make(*, thread_id: str | None = None, **overrides: Any) -> Casper:
        config: dict[str, Any] = {
            "user_name": "tester",
            "chat_id": "chat-under-test",
            "user_id": "user-under-test",
            "user_previous_messages": [],
            "web_search": False,
        }
        config.update(overrides)
        if thread_id is not None:
            config["thread_id"] = thread_id
        instance = harness.attach(Casper(config))
        await instance.async_init()
        return instance

    return _make


@pytest.fixture
def harness() -> _Harness:
    return _Harness()


@pytest.fixture
async def casper(make_casper) -> Casper:
    return await make_casper()


@pytest.fixture
async def paused(make_casper, harness: _Harness) -> Casper:
    """Drive one turn up to the report-config pause."""
    instance = await make_casper()
    await harness.drain(
        instance.graph.astream(
            {"messages": _messages([_retrieve_call()])},
            config=instance.graph_config(),
            stream_mode=["custom", "updates"],
        )
    )
    return instance


# --- routing ---------------------------------------------------------------


async def test_retrieve_step_routes_to_layout_pair(casper: Casper) -> None:
    assert casper._route_after_gate({"messages": _messages([_retrieve_call()])}) == "layout_pair"


async def test_ask_user_step_still_routes_straight_to_tools(casper: Casper) -> None:
    """KD3: the simulated ask_user pause keeps its existing behaviour."""
    state = {
        "messages": _messages(
            [{"name": "ask_user", "id": "call_ask_1", "args": {"questions": ["A", "B"]}}]
        )
    }
    assert casper._route_after_gate(state) == "tools"


async def test_read_tool_step_routes_to_sequential_tools(casper: Casper) -> None:
    state = {
        "messages": _messages(
            [{"name": "query_document", "id": "call_doc_1", "args": {"user_query": "x"}}]
        )
    }
    assert casper._route_after_gate(state) == "sequential_tools"


async def test_layout_guard_nudge_step_does_not_reach_layout_pair(casper: Casper) -> None:
    """An already-answered retrieve call is a nudge, not a real step — no pause."""
    messages = _messages([_retrieve_call()])
    messages.append(
        ToolMessage(content="not executed", tool_call_id="call_retrieve_1", name="retrieve")
    )
    assert casper._route_after_gate({"messages": messages}) == "report_or_respond"


# --- the pause -------------------------------------------------------------


async def test_graph_pauses_before_retrieval(paused: Casper, harness: _Harness) -> None:
    """AE4/R1: retrieval has not run and the thread is still awaiting input."""
    assert harness.retrieve_calls == [], "retrieve must not run before the pause"
    state = await paused.graph.aget_state(paused.graph_config())
    assert state.next == ("report_config",)
    assert state.tasks[0].interrupts, "the thread reports as awaiting input"


async def test_pause_event_carries_thread_id_and_both_tagged_layouts(
    paused: Casper, harness: _Harness
) -> None:
    """R2, R7: the frontend is told it is waiting, and what it is choosing between."""
    pause_events = harness.named("report_config")
    assert len(pause_events) == 1

    event = pause_events[0]
    assert event["status"] == "awaiting_configuration"
    assert event["thread_id"] == paused.turn_thread_id
    assert event["default_tier"] == "study"
    assert {layout["report_tier"] for layout in event["layouts"]} == {"study", "brief"}


async def test_both_layouts_are_emitted_before_the_pause(harness: _Harness, paused: Casper) -> None:
    """R7: each emitted layout states which tier it represents, pair before pause."""
    names = [e.get("name") for e in harness.events]
    assert names.count("layout_pair") == 2
    assert names.index("report_config") > max(i for i, n in enumerate(names) if n == "layout_pair")

    tiers = [e["report_tier"] for e in harness.named("layout_pair")]
    assert tiers == ["study", "brief"]


async def test_the_brief_sibling_is_flat_and_the_study_one_is_not(
    paused: Casper, harness: _Harness
) -> None:
    """The two tiers are different shapes, which is why a switch needs a layout."""
    assert "- " in paused.report_layout_pair["study"]["markdown"]
    assert "- " not in paused.report_layout_pair["brief"]["markdown"]


async def test_resume_returns_the_submission_and_reaches_retrieval(
    paused: Casper, harness: _Harness
) -> None:
    """R3: a Confirm submission is what resumes the run, and it drives retrieval."""
    await harness.drain(paused.resume_report_config({"report_tier": "brief"}))

    assert paused.report_config_submission["report_tier"] == "brief"
    assert harness.retrieve_calls, "retrieval runs once the configuration arrives"
    assert (await paused.graph.aget_state(paused.graph_config())).next == ()


async def test_resume_replays_the_update_the_pause_cut_short(
    paused: Casper, harness: _Harness
) -> None:
    """`report_or_respond` never re-runs, but its consumer still has to see it:
    that update is what creates the report row and reserves the tokens."""
    harness.updates.clear()

    await harness.drain(paused.resume_report_config({"report_tier": "brief"}))

    assert "report_or_respond" in harness.updates[0]
    replayed = harness.updates[0]["report_or_respond"]["messages"][0]
    assert [call["name"] for call in replayed.tool_calls] == ["retrieve"]


async def test_nothing_is_replayed_for_a_thread_that_is_not_parked(
    casper: Casper,
) -> None:
    """A thread with no pending retrieve step has no update to replay."""
    assert await casper.paused_report_or_respond_update() is None


async def test_resume_does_not_emit_a_second_pause_event(paused: Casper, harness: _Harness) -> None:
    """The interrupted node re-runs on resume; the pause event must not repeat."""
    await harness.drain(paused.resume_report_config({"report_tier": "study"}))
    assert len(harness.named("report_config")) == 1


async def test_resume_does_not_re_mint_the_layout_pair(paused: Casper, harness: _Harness) -> None:
    """R6/KTD2: `layout_pair` sits before the interrupt, so it runs exactly once."""
    assert harness.mint_calls == 1

    await harness.drain(paused.resume_report_config({"report_tier": "brief"}))

    assert harness.mint_calls == 1
    assert len(harness.named("layout_pair")) == 2


async def test_the_pair_never_enters_the_conversation(paused: Casper) -> None:
    """R8: pairing is display-only — the model's view of the live layout is unchanged."""
    state = await paused.graph.aget_state(paused.graph_config())
    layout_messages = [
        m
        for m in state.values["messages"]
        if getattr(m, "type", None) == "tool" and getattr(m, "name", "") == "propose_report_layout"
    ]
    assert len(layout_messages) == 1, "the sibling must not add a second proposal"
    assert layout_messages[0].content == PROPOSED_TOOL_RESULT
    assert BRIEF_LAYOUT not in layout_messages[0].content


async def test_a_resumed_casper_recovers_the_pair_from_checkpointed_state(
    paused: Casper, make_casper, harness: _Harness
) -> None:
    """R4: the resuming request builds a fresh Casper; only graph state carries over."""
    resumed = await make_casper(thread_id=paused.turn_thread_id)
    assert resumed is not paused
    assert resumed.report_layout_pair == {}

    await harness.drain(resumed.resume_report_config({"report_tier": "brief"}))

    assert set(resumed.report_layout_pair) == {"study", "brief"}
    assert resumed.report_config_submission["report_tier"] == "brief"


# --- submission normalization ---------------------------------------------


def test_unknown_tier_falls_back_to_the_default() -> None:
    assert _normalize_report_config({"report_tier": "epic"}, "brief")["report_tier"] == "brief"


def test_every_submitted_field_is_kept() -> None:
    """R12: including the fields v1 does not act on."""
    confirmed = _normalize_report_config(
        {
            "report_tier": "brief",
            "style": "investor",
            "output_formats": ["pdf", "pptx"],
            "language": "English",
            "data_sources": ["web"],
        }
    )
    assert confirmed == {
        "report_tier": "brief",
        "style": "investor",
        "output_formats": ["pdf", "pptx"],
        "language": "English",
        "data_sources": ["web"],
    }


def test_unrecognized_style_falls_back_to_investor() -> None:
    """R13/R14: investor is the only style v1 accepts."""
    assert _normalize_report_config({"style": "casual"})["style"] == "investor"
    assert _normalize_report_config({})["style"] == "investor"


def test_sibling_tier_is_the_other_one() -> None:
    assert _sibling_tier("study") == "brief"
    assert _sibling_tier("brief") == "study"


def test_module_exposes_both_tiers() -> None:
    assert model_module.REPORT_TIERS == ("study", "brief")


def test_unanswered_ask_user_does_not_erase_the_topic() -> None:
    """The human turn that triggered ask_user must still be in history next turn."""
    history = [
        {"type": "human", "content": "hi"},
        {"type": "ai", "content": "Welcome.", "response_metadata": {"stop_reason": "end_turn"}},
        {"type": "human", "content": "make me a report on saudi vision 2030"},
        {
            "type": "ai",
            "content": "A few questions before we proceed.",
            "response_metadata": {"stop_reason": "tool_use"},
            "tool_calls": [{"name": "ask_user", "id": "c1", "args": {}}],
        },
        {"type": "tool", "content": "options", "tool_call_id": "c1", "name": "ask_user"},
    ]
    kept = _keep_finished_chat_history(history)
    texts = [m["content"] for m in kept]
    assert "make me a report on saudi vision 2030" in texts
    assert "hi" in texts
    assert all(m["type"] != "tool" for m in kept)
    assert not any(
        (m.get("response_metadata") or {}).get("stop_reason") == "tool_use" for m in kept
    )


def test_finished_tool_exchange_is_kept() -> None:
    history = [
        {"type": "human", "content": "make me a report on saudi vision 2030"},
        {
            "type": "ai",
            "content": "asking",
            "response_metadata": {"stop_reason": "tool_use"},
            "tool_calls": [{"name": "ask_user", "id": "c1", "args": {}}],
        },
        {"type": "tool", "content": "options", "tool_call_id": "c1", "name": "ask_user"},
        {
            "type": "ai",
            "content": "Please pick a domain.",
            "response_metadata": {"stop_reason": "end_turn"},
        },
    ]
    kept = _keep_finished_chat_history(history)
    assert [m["type"] for m in kept] == ["human", "ai", "tool", "ai"]
