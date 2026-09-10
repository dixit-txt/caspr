"""Chat-while-a-report-is-generating: the `respond_during_report` graph node.

When `retrieve` has fired and report analysis is running, a follow-up chat
message is routed (from START) to `respond_during_report` instead of the normal
`report_or_respond` planner. That node:

  - binds NO report tools (cannot start / restart / re-plan a report); its only
    tool is `flag_report_change_request`,
  - emits one `post_report_edit_request` custom event per change the user asks
    for in the report being generated (applied later, downstream — not here),
  - otherwise just answers the user.

Two layers of coverage:

  * Structural guards read the source with ``ast`` — no import, no env — so a
    refactor that drops the node, the conditional entry, or the tool schema
    fails loudly.
  * Behavioural tests drive the node with a fake LLM. They need
    ``src.core.agent.model`` to import (which builds LLM/DB clients at import
    time); if that fails for lack of env the whole behavioural block skips
    rather than errors.
"""
from __future__ import annotations

import ast
import os
import sys
import unittest
from unittest.mock import patch

# This file lives at caspr-core/src/core/test_respond_during_report.py
_CORE_DIR = os.path.dirname(os.path.abspath(__file__))
_SRC_DIR = os.path.dirname(_CORE_DIR)
_REPO = os.path.dirname(_SRC_DIR)
_MODEL_SOURCE = os.path.join(_CORE_DIR, "agent", "model.py")
_API_SOURCE = os.path.join(_SRC_DIR, "resources", "routers", "api.py")

# Running this file as a script puts src/core on sys.path; the package root is
# caspr-core so `from src.core.agent import model` can resolve.
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)


def _module_tree(path: str) -> ast.Module:
    with open(path) as fh:
        return ast.parse(fh.read())


def _find_function(tree: ast.AST, name: str):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    return None


def _find_class(tree: ast.AST, name: str):
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == name:
            return node
    return None


def _call_name(call: ast.Call):
    """`foo(...)` -> 'foo'; `obj.foo(...)` -> 'foo'."""
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    if isinstance(call.func, ast.Name):
        return call.func.id
    return None


# ---------------------------------------------------------------------------
# Structural guards — ast only, no import
# ---------------------------------------------------------------------------
class NodeWiringStructureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.model = _module_tree(_MODEL_SOURCE)
        cls.casper = _find_class(cls.model, "Casper")
        assert cls.casper is not None, "class Casper not found in model.py"

    def _casper_methods(self):
        return {
            n.name
            for n in self.casper.body
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        }

    def test_node_and_router_methods_exist(self):
        methods = self._casper_methods()
        self.assertIn("respond_during_report", methods)
        self.assertIn("_route_entry", methods)
        self.assertIn("_extract_in_progress_layout", methods)

    def test_build_graph_registers_the_node(self):
        build = _find_function(self.casper, "_build_graph")
        self.assertIsNotNone(build)
        added_nodes = [
            c.args[0].value
            for c in ast.walk(build)
            if isinstance(c, ast.Call)
            and _call_name(c) == "add_node"
            and c.args
            and isinstance(c.args[0], ast.Constant)
        ]
        self.assertIn("respond_during_report", added_nodes)
        self.assertIn("report_or_respond", added_nodes)

    def test_conditional_entry_is_wired_from_START(self):
        build = _find_function(self.casper, "_build_graph")
        entry_calls = [
            c
            for c in ast.walk(build)
            if isinstance(c, ast.Call)
            and _call_name(c) == "add_conditional_edges"
            and c.args
            and isinstance(c.args[0], ast.Name)
            and c.args[0].id == "START"
        ]
        self.assertEqual(
            len(entry_calls), 1, "expected exactly one add_conditional_edges(START, ...)"
        )
        # routes to the two entry nodes and nothing else
        mapping = entry_calls[0].args[2]
        self.assertIsInstance(mapping, ast.Dict)
        targets = {k.value for k in mapping.keys if isinstance(k, ast.Constant)}
        self.assertEqual(targets, {"report_or_respond", "respond_during_report"})

    def test_no_stale_set_entry_point(self):
        build = _find_function(self.casper, "_build_graph")
        entry_point_calls = [
            c for c in ast.walk(build)
            if isinstance(c, ast.Call) and _call_name(c) == "set_entry_point"
        ]
        self.assertEqual(
            entry_point_calls, [],
            "set_entry_point should have been replaced by the conditional START edge",
        )

    def test_node_terminates_at_END(self):
        build = _find_function(self.casper, "_build_graph")
        edges = [
            (c.args[0].value, c.args[1].id if isinstance(c.args[1], ast.Name) else c.args[1])
            for c in ast.walk(build)
            if isinstance(c, ast.Call)
            and _call_name(c) == "add_edge"
            and len(c.args) == 2
            and isinstance(c.args[0], ast.Constant)
        ]
        self.assertIn(("respond_during_report", "END"), edges)

    def test_route_entry_returns_the_two_entry_nodes(self):
        route = _find_function(self.casper, "_route_entry")
        returned = {
            n.value.value
            for n in ast.walk(route)
            if isinstance(n, ast.Return) and isinstance(n.value, ast.Constant)
        }
        self.assertEqual(returned, {"respond_during_report", "report_or_respond"})


class FlagToolSchemaStructureTests(unittest.TestCase):
    """`flag_report_change_request` is the node's ONLY tool and has the shape the
    api-layer / ask-caspr contract depends on."""

    @classmethod
    def setUpClass(cls):
        tree = _module_tree(_MODEL_SOURCE)
        cls.schema = None
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for tgt in node.targets:
                    if isinstance(tgt, ast.Name) and tgt.id == "FLAG_REPORT_CHANGE_REQUEST_SCHEMA":
                        cls.schema = ast.literal_eval(node.value)
        assert cls.schema is not None, "FLAG_REPORT_CHANGE_REQUEST_SCHEMA not found"

    def test_is_a_function_tool_named_flag_report_change_request(self):
        self.assertEqual(self.schema["type"], "function")
        self.assertEqual(self.schema["function"]["name"], "flag_report_change_request")

    def test_requires_a_requests_array_of_verbatim_strings(self):
        params = self.schema["function"]["parameters"]
        self.assertEqual(params["required"], ["requests"])
        requests = params["properties"]["requests"]
        self.assertEqual(requests["type"], "array")
        self.assertEqual(requests["items"]["type"], "string")

    def test_node_does_not_bind_report_tools(self):
        """The node body must never bind `retrieve` / `propose_report_layout`."""
        with open(_MODEL_SOURCE) as fh:
            source = fh.read()
        node = _find_function(_find_class(ast.parse(source), "Casper"), "respond_during_report")
        src = ast.get_source_segment(source, node)
        self.assertNotIn("retrieve", src)
        self.assertNotIn("propose_report_layout", src)
        self.assertIn("FLAG_REPORT_CHANGE_REQUEST_SCHEMA", src)


class ApiPlumbingStructureTests(unittest.TestCase):
    """The producer side: detect an in-progress report, stream the node's deltas,
    forward + durably queue `post_report_edit_request`, and buffer interim turns
    instead of clobbering chat history."""

    @classmethod
    def setUpClass(cls):
        with open(_API_SOURCE) as fh:
            cls.src = fh.read()
        cls.tree = ast.parse(cls.src)

    def test_helpers_defined(self):
        fns = {
            n.name
            for n in ast.walk(self.tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        self.assertIn("_interim_turns_key", fns)
        self.assertIn("_drain_interim_turns", fns)

    def test_in_progress_report_detection_uses_analysis_in_progress_status(self):
        self.assertIn("ANALYSIS_IN_PROGRESS", self.src)
        self.assertIn('"report_in_progress"', self.src)
        self.assertIn('"in_progress_report_id"', self.src)

    def test_node_deltas_are_streamed(self):
        # the messages-mode node filter must include the new node
        self.assertIn(
            '("report_or_respond", "respond_during_report")', self.src
        )

    def test_edit_request_event_is_forwarded_and_queued(self):
        self.assertIn('custom_name == "post_report_edit_request"', self.src)
        self.assertIn("report:{edit_report_id}:edit_requests", self.src)
        self.assertIn('"type": "post_report_edit_request"', self.src)

    def test_interim_turns_are_buffered_not_persisted_inline(self):
        producer = _find_function(self.tree, "chat_producer")
        self.assertIsNotNone(producer)
        body = ast.get_source_segment(self.src, producer)
        self.assertIn("_interim_turns_key", body)
        self.assertIn("_drain_interim_turns", body)
        # the interim branch must return before the normal insert_chats path
        self.assertIn("in_progress_report_id is not None", body)


# ---------------------------------------------------------------------------
# Behavioural tests — need src.core.agent.model to import
# ---------------------------------------------------------------------------
try:  # pragma: no cover - import guard
    from src.core.agent import model as _model
    from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

    _IMPORT_ERROR = None
except Exception as exc:  # pragma: no cover
    _model = None
    _IMPORT_ERROR = exc


class _FakeLLM:
    """Records every ainvoke call and what tools were bound to it."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []  # list of dict(messages=..., tools=...)
        self._pending_tools = None

    def bind_tools(self, tools):
        self._pending_tools = tools
        return self

    async def ainvoke(self, messages):
        self.calls.append({"messages": list(messages), "tools": self._pending_tools})
        self._pending_tools = None
        resp = self._responses.pop(0)
        if isinstance(resp, BaseException):
            raise resp
        return resp


def _stub_casper(anthropic_llm, **overrides):
    """A Casper with only the attributes `respond_during_report` / `_route_entry`
    touch — skips the heavy __init__ (LLM clients, S3, graph build)."""
    c = _model.Casper.__new__(_model.Casper)
    c.user_name = "tester"
    c.chat_id = "chat-1"
    c.user_id = "user-1"
    c.report_in_progress = True
    c.in_progress_report_id = "report-1"
    c.in_progress_report_title = "EV Market Report"
    c._current_turn_messages = []
    c.anthropic_llm = anthropic_llm
    for key, value in overrides.items():
        setattr(c, key, value)
    return c


def _flag_tool_call(requests, call_id="call-1"):
    return {
        "name": "flag_report_change_request",
        "id": call_id,
        "args": {"requests": requests},
    }


@unittest.skipIf(_model is None, f"src.core.agent.model failed to import: {_IMPORT_ERROR!r}")
class RespondDuringReportBehaviourTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.events = []
        self._patchers = [
            patch.object(_model, "get_stream_writer", lambda: self.events.append),
            patch.object(_model, "save_raw_llm_response", lambda *a, **k: None),
        ]
        for p in self._patchers:
            p.start()
            self.addCleanup(p.stop)

    def _state(self, user_text, system="You are Caspr.", extra=None):
        msgs = [SystemMessage(content=system)]
        if extra:
            msgs.extend(extra)
        msgs.append(HumanMessage(content=user_text))
        return {"messages": msgs}

    def _edit_events(self):
        return [e for e in self.events if e.get("name") == "post_report_edit_request"]

    # -- plain conversation ------------------------------------------------
    async def test_plain_chat_returns_one_message_and_no_edit_event(self):
        llm = _FakeLLM([AIMessage(content="It has 8 sections so far.")])
        casper = _stub_casper(llm)

        out = await casper.respond_during_report(self._state("how many sections?"))

        self.assertEqual(len(out["messages"]), 1)
        self.assertEqual(out["messages"][0].content, "It has 8 sections so far.")
        self.assertEqual(self._edit_events(), [])
        self.assertEqual(len(llm.calls), 1, "plain chat must not make a second LLM call")

    async def test_first_call_binds_only_the_flag_tool(self):
        llm = _FakeLLM([AIMessage(content="hi")])
        casper = _stub_casper(llm)

        await casper.respond_during_report(self._state("hello"))

        tools = llm.calls[0]["tools"]
        self.assertEqual(len(tools), 1)
        self.assertIs(tools[0], _model.FLAG_REPORT_CHANGE_REQUEST_SCHEMA)

    async def test_system_note_and_report_title_injected(self):
        llm = _FakeLLM([AIMessage(content="hi")])
        casper = _stub_casper(llm)

        await casper.respond_during_report(self._state("hello"))

        system_text = llm.calls[0]["messages"][0].content
        self.assertIn("REPORT IS CURRENTLY BEING GENERATED", system_text)
        self.assertIn("EV Market Report", system_text)

    # -- change requests -------------------------------------------------
    async def test_change_request_emits_one_event_with_raw_user_message(self):
        tool_call = AIMessage(
            content="",
            tool_calls=[
                _flag_tool_call(["add a bit comparing BYD and Tesla margins"])
            ],
        )
        ack = AIMessage(content="Noted — I'll add that to the Overview.")
        casper = _stub_casper(_FakeLLM([tool_call, ack]))

        out = await casper.respond_during_report(
            self._state("add a bit comparing BYD and Tesla margins")
        )

        events = self._edit_events()
        self.assertEqual(len(events), 1)
        evt = events[0]
        self.assertEqual(evt["status"], "captured")
        self.assertEqual(evt["chat_id"], "chat-1")
        self.assertEqual(evt["report_id"], "report-1")
        self.assertEqual(evt["request"], "add a bit comparing BYD and Tesla margins")

    async def test_change_request_returns_toolcall_then_tool_then_ack(self):
        tool_call = AIMessage(
            content="",
            tool_calls=[_flag_tool_call(["remove the regulatory risk point"])],
        )
        ack = AIMessage(content="Done — that will be dropped from Risks.")
        casper = _stub_casper(_FakeLLM([tool_call, ack]))

        out = await casper.respond_during_report(self._state("remove the regulatory risk point"))

        kinds = [type(m).__name__ for m in out["messages"]]
        self.assertEqual(kinds, ["AIMessage", "ToolMessage", "AIMessage"])
        self.assertIs(out["messages"][0], tool_call)
        self.assertEqual(out["messages"][1].name, "flag_report_change_request")
        self.assertEqual(out["messages"][1].tool_call_id, "call-1")
        self.assertIs(out["messages"][2], ack)

    async def test_second_call_is_made_without_tools(self):
        tool_call = AIMessage(
            content="",
            tool_calls=[_flag_tool_call(["make it more formal"])],
        )
        llm = _FakeLLM([tool_call, AIMessage(content="ok")])
        casper = _stub_casper(llm)

        await casper.respond_during_report(self._state("make it more formal"))

        self.assertEqual(len(llm.calls), 2)
        self.assertIsNone(llm.calls[1]["tools"])
        # the follow-up call sees the tool result
        self.assertIsInstance(llm.calls[1]["messages"][-1], ToolMessage)

    async def test_multiple_requests_in_one_tool_call_emit_multiple_events(self):
        tool_call = AIMessage(
            content="",
            tool_calls=[
                _flag_tool_call(["add the TAM", "punchier ending"])
            ],
        )
        casper = _stub_casper(_FakeLLM([tool_call, AIMessage(content="both noted")]))

        await casper.respond_during_report(self._state("add the TAM and a punchier ending"))

        events = self._edit_events()
        self.assertEqual(len(events), 2)
        self.assertEqual(
            [e["request"] for e in events], ["add the TAM", "punchier ending"]
        )

    async def test_blank_request_entries_are_skipped(self):
        tool_call = AIMessage(
            content="",
            tool_calls=[_flag_tool_call(["", "tighten it up"])],
        )
        casper = _stub_casper(_FakeLLM([tool_call, AIMessage(content="ok")]))

        await casper.respond_during_report(self._state("tighten the report"))

        events = self._edit_events()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["request"], "tighten it up")

    # -- resilience -----------------------------------------------------
    async def test_llm_error_returns_graceful_message_and_error_event(self):
        casper = _stub_casper(_FakeLLM([RuntimeError("boom")]))
        # openai fallback also unavailable
        with patch.object(_model, "OPENAI_LLM_LANGCHAIN", _FakeLLM([RuntimeError("boom too")])):
            out = await casper.respond_during_report(self._state("hi"))

        self.assertEqual(len(out["messages"]), 1)
        self.assertIn("still being generated", out["messages"][0].content)
        self.assertTrue(
            any(e.get("name") == "respond_during_report" and e.get("status") == "error"
                for e in self.events)
        )

    async def test_anthropic_failure_falls_back_to_openai(self):
        anthropic = _FakeLLM([RuntimeError("anthropic down")])
        openai = _FakeLLM([AIMessage(content="answered via openai")])
        casper = _stub_casper(anthropic)

        with patch.object(_model, "OPENAI_LLM_LANGCHAIN", openai):
            out = await casper.respond_during_report(self._state("hi"))

        self.assertEqual(out["messages"][0].content, "answered via openai")
        self.assertEqual(len(openai.calls), 1)

    async def test_stream_lifecycle_events_emitted(self):
        casper = _stub_casper(_FakeLLM([AIMessage(content="hello")]))
        await casper.respond_during_report(self._state("hi"))
        names = [(e.get("name"), e.get("status")) for e in self.events]
        self.assertIn(("respond_during_report", "message_stream_start"), names)
        self.assertIn(("respond_during_report", "message_stream_complete"), names)


@unittest.skipIf(_model is None, f"src.core.agent.model failed to import: {_IMPORT_ERROR!r}")
class RouteEntryTests(unittest.TestCase):
    def test_routes_to_node_when_report_in_progress(self):
        casper = _model.Casper.__new__(_model.Casper)
        casper.report_in_progress = True
        casper.in_progress_report_id = "r1"
        casper.user_name = "u"
        casper.chat_id = "c"
        self.assertEqual(casper._route_entry({"messages": []}), "respond_during_report")

    def test_routes_to_planner_otherwise(self):
        casper = _model.Casper.__new__(_model.Casper)
        casper.report_in_progress = False
        casper.in_progress_report_id = None
        casper.user_name = "u"
        casper.chat_id = "c"
        self.assertEqual(casper._route_entry({"messages": []}), "report_or_respond")


@unittest.skipIf(_model is None, f"src.core.agent.model failed to import: {_IMPORT_ERROR!r}")
class ExtractInProgressLayoutTests(unittest.TestCase):
    def _casper(self):
        c = _model.Casper.__new__(_model.Casper)
        return c

    def test_reads_the_last_propose_report_layout_tool_message(self):
        msgs = [
            ToolMessage(content="OLD LAYOUT", name="propose_report_layout", tool_call_id="a"),
            ToolMessage(content="## Section 1\n## Section 2", name="propose_report_layout", tool_call_id="b"),
        ]
        self.assertEqual(
            self._casper()._extract_in_progress_layout(msgs), "## Section 1\n## Section 2"
        )

    def test_returns_empty_when_no_layout_present(self):
        msgs = [ToolMessage(content="x", name="retrieve", tool_call_id="a")]
        self.assertEqual(self._casper()._extract_in_progress_layout(msgs), "")

    def test_handles_block_style_content(self):
        msgs = [
            ToolMessage(
                content=[{"type": "text", "text": "## A"}, {"type": "text", "text": "## B"}],
                name="propose_report_layout",
                tool_call_id="a",
            )
        ]
        self.assertEqual(self._casper()._extract_in_progress_layout(msgs), "## A ## B")


@unittest.skipIf(_model is None, f"src.core.agent.model failed to import: {_IMPORT_ERROR!r}")
class CompiledGraphEntryTests(unittest.IsolatedAsyncioTestCase):
    """End-to-end: a fully built graph enters `respond_during_report` (not the
    planner) when a report is in progress, and ends after one node."""

    async def _build(self, **cfg):
        base = {
            "user_name": "tester",
            "chat_id": "chat-1",
            "user_previous_messages": [],
            "user_id": "user-1",
        }
        base.update(cfg)
        try:
            casper = _model.Casper(base)
            await casper.async_init()
        except Exception as exc:  # pragma: no cover - env dependent
            self.skipTest(f"Casper construction needs env: {exc!r}")
        return casper

    async def test_report_in_progress_runs_only_the_respond_node(self):
        casper = await self._build(
            report_in_progress=True,
            in_progress_report_id="report-1",
            in_progress_report_title="EV Market Report",
        )
        with patch.object(_model, "save_raw_llm_response", lambda *a, **k: None):
            casper.anthropic_llm = _FakeLLM([AIMessage(content="sure, ask away")])
            visited = []
            async for _mode, chunk in casper.graph.astream(
                {"messages": [SystemMessage(content="sys"), HumanMessage(content="hi")]},
                stream_mode=["updates"],
            ):
                visited.extend(chunk.keys())

        self.assertIn("respond_during_report", visited)
        self.assertNotIn("report_or_respond", visited)
        self.assertNotIn("generate_report", visited)

    async def test_no_report_runs_the_planner(self):
        casper = await self._build()
        with patch.object(_model, "save_raw_llm_response", lambda *a, **k: None):
            casper.anthropic_llm = _FakeLLM([AIMessage(content="hello there")])
            visited = []
            async for _mode, chunk in casper.graph.astream(
                {"messages": [SystemMessage(content="sys"), HumanMessage(content="hi")]},
                stream_mode=["updates"],
            ):
                visited.extend(chunk.keys())

        self.assertIn("report_or_respond", visited)
        self.assertNotIn("respond_during_report", visited)


if __name__ == "__main__":
    unittest.main()
