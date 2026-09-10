"""U1/U2: durable pause state and per-turn thread identity.

The report-config pause ends one request and is resumed by a later one, so
these tests are about the two things that make that possible: a checkpointer
that outlives a single graph object, and a thread id that identifies the
paused turn.
"""

from __future__ import annotations

from typing import Any

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from typing_extensions import TypedDict

from app.core import checkpointer as checkpointer_module
from app.core.checkpointer import (
    close_checkpointer,
    get_checkpointer,
    set_checkpointer,
    setup_checkpointer,
)


class _PauseState(TypedDict, total=False):
    resumed: str


def _build_pausing_graph() -> Any:
    """A one-node graph that interrupts, compiled the way ``Casper`` compiles."""

    def pause(state: _PauseState) -> _PauseState:
        submitted = interrupt({"awaiting": "report_config"})
        return {"resumed": str(submitted)}

    builder: StateGraph[_PauseState, None, _PauseState, _PauseState] = StateGraph(_PauseState)
    builder.add_node("report_config", pause)
    builder.add_edge(START, "report_config")
    builder.add_edge("report_config", END)
    return builder.compile(checkpointer=get_checkpointer())


@pytest.fixture(autouse=True)
def _isolated_checkpointer():
    """Each test owns the process saver, and none leaks into the next."""
    set_checkpointer(InMemorySaver())
    yield
    set_checkpointer(None)


@pytest.mark.unit
async def test_interrupted_thread_is_resumable_and_stored() -> None:
    graph = _build_pausing_graph()
    config = {"configurable": {"thread_id": "thread-a"}}

    result = await graph.ainvoke({}, config)

    assert "__interrupt__" in result, "the run should end parked on the interrupt"
    state = await graph.aget_state(config)
    assert state.next == ("report_config",), "the paused node is still the next step"
    assert state.tasks[0].interrupts, "the stored state carries the interrupt payload"


@pytest.mark.unit
async def test_pause_survives_a_second_graph_instance() -> None:
    """R4: the request that pauses ends; a *different* graph object resumes it."""
    first = _build_pausing_graph()
    config = {"configurable": {"thread_id": "thread-b"}}
    await first.ainvoke({}, config)

    second = _build_pausing_graph()
    assert second is not first

    resumed = await second.ainvoke(Command(resume={"tier": "brief"}), config)

    assert resumed["resumed"] == str({"tier": "brief"})
    assert (await second.aget_state(config)).next == ()


@pytest.mark.unit
async def test_threads_do_not_share_state() -> None:
    """U2: a per-turn thread id keeps one turn's pause out of another's."""
    graph = _build_pausing_graph()
    await graph.ainvoke({}, {"configurable": {"thread_id": "turn-1"}})

    other = await graph.aget_state({"configurable": {"thread_id": "turn-2"}})

    assert other.next == (), "an unrelated turn has nothing pending"


@pytest.mark.unit
async def test_setup_falls_back_to_memory_when_postgres_is_unreachable(caplog) -> None:
    set_checkpointer(None)
    try:
        with caplog.at_level("WARNING"):
            saver = await setup_checkpointer("postgresql://nobody@127.0.0.1:1/nowhere")
        assert isinstance(saver, InMemorySaver)
        assert any("falling back to an in-memory saver" in r.message for r in caplog.records)
    finally:
        await close_checkpointer()


@pytest.mark.unit
async def test_setup_runs_once_per_process() -> None:
    """Setup issues DDL — repeated graph construction must not repeat it."""
    set_checkpointer(None)
    try:
        first = await setup_checkpointer("postgresql://nobody@127.0.0.1:1/nowhere")
        second = await setup_checkpointer("postgresql://nobody@127.0.0.1:1/nowhere")
        assert first is second
    finally:
        await close_checkpointer()


@pytest.mark.unit
def test_get_checkpointer_builds_a_saver_outside_the_application() -> None:
    """Compiling a graph must never depend on a reachable database."""
    set_checkpointer(None)
    assert isinstance(get_checkpointer(), InMemorySaver)
    assert get_checkpointer() is checkpointer_module._CHECKPOINTER
