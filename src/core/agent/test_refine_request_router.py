"""Tests for src.core.agent.refine_request_router.

Two layers, mirroring test_respond_during_report.py:
  * structural checks over the source (always run) — the fe_json / event contract
    the refiner (ask-caspr service) depends on;
  * behavioural tests over the compiled LangGraph with a fake routing LLM
    (skipped when the module cannot be imported in this environment).
"""

import ast
import os
import unittest
from unittest.mock import patch

_HERE = os.path.dirname(__file__)
_SOURCE = os.path.join(_HERE, "refine_request_router.py")

try:  # heavy deps (langgraph, langchain_anthropic) may be absent locally
    import src.core.agent.refine_request_router as _mod
    _IMPORT_ERROR = None
except Exception as exc:  # noqa: BLE001
    _mod = None
    _IMPORT_ERROR = exc


class SourceContractTests(unittest.TestCase):
    """The producer contract, checked without importing heavy deps."""

    @classmethod
    def setUpClass(cls):
        with open(_SOURCE) as fh:
            cls.src = fh.read()
        cls.tree = ast.parse(cls.src)

    def _func(self, name):
        for node in ast.walk(self.tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
                return node
        return None

    def test_emits_the_routed_event_name(self):
        self.assertIn('ROUTED_EVENT_NAME = "refine_request_routed"', self.src)

    def test_touches_no_refiner(self):
        """This module only emits an event — it must not import from or call a refiner."""
        imports = [
            ast.dump(n)
            for n in ast.walk(self.tree)
            if isinstance(n, (ast.Import, ast.ImportFrom))
        ]
        self.assertFalse([i for i in imports if "refin" in i.lower()], "must not import any refiner module")
        self.assertNotIn("refine_card(", self.src)

    def test_fe_json_matches_ask_caspr_build_refine_fe_json(self):
        body = ast.get_source_segment(self.src, self._func("_to_fe_json_for_refine"))
        self.assertIn('"refine_or_delete_prompt"', body)
        self.assertIn('"subsection"', body)
        self.assertIn('"id"', body)
        # subsection branch does NOT carry a top-level refine_or_delete_prompt
        self.assertNotIn('"refine_or_delete_prompt": None', body)

    def test_uuidv7_from_stdlib(self):
        self.assertIn("from uuid import uuid7", self.src)
        self.assertNotIn("uuid_utils", self.src)
        self.assertNotIn("uuid4", self.src)

    def test_state_tracks_ids_names_and_parsed_content(self):
        names = {
            n.target.id
            for n in ast.walk(self.tree)
            if isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name)
        }
        self.assertTrue({"card_id", "section_id", "section_name", "section_content"} <= names)
        self.assertIn("subsections", names)

    def test_graph_uses_a_checkpointer(self):
        self.assertIn("MemorySaver", self.src)
        self.assertIn("compile(checkpointer=", self.src)


@unittest.skipIf(_mod is None, f"refine_request_router import failed: {_IMPORT_ERROR!r}")
class PureHelperTests(unittest.TestCase):
    def test_plain_text_flattens_citation_links(self):
        got = _mod._plain_text("Revenue grew [BloombergNEF](https://example.com/x) sharply.")
        self.assertEqual(got, "Revenue grew BloombergNEF sharply.")

    def test_plain_text_handles_content_blocks_and_dicts(self):
        self.assertEqual(_mod._plain_text({"content": "hi"}), "hi")
        self.assertEqual(
            _mod._plain_text([{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]),
            "a\nb",
        )

    def test_parse_card_event_extracts_ids_names_content(self):
        event = {
            "name": "generate_report",
            "card_type": "section",
            "card_db": {
                "section": [{"id": "sec-1", "name": "Market Overview", "content": "The market is [x](https://e.com) large."}],
                "sub_sections": [
                    {"id": "sub-1", "name": "Regional split", "content": "APAC leads."},
                    {"id": "sub-blank", "name": "", "content": ""},
                ],
                "citations": {},
            },
        }
        rec = _mod.parse_card_event(event)
        self.assertEqual(rec["card_id"], "sec-1")
        self.assertEqual(rec["section_id"], "sec-1")
        self.assertEqual(rec["section_name"], "Market Overview")
        self.assertEqual(rec["section_content"], "The market is x large.")
        self.assertEqual([s["id"] for s in rec["subsections"]], ["sub-1"])
        self.assertEqual(rec["subsections"][0]["name"], "Regional split")

    def test_parse_card_event_ignores_non_cards(self):
        self.assertIsNone(_mod.parse_card_event({"name": "generate_report", "status": "heartbeat"}))
        self.assertIsNone(_mod.parse_card_event({"card_db": {"section": [{"id": "x", "name": "", "content": ""}], "sub_sections": []}}))

    def test_parse_card_event_without_id_still_tracks(self):
        rec = _mod.parse_card_event(
            {"card_type": "es", "card_db": {"section": [{"name": "executive_summary", "content": "Summary."}]}}
        )
        self.assertEqual(rec["card_id"], "es:executive_summary")
        self.assertEqual(rec["section_name"], "executive_summary")

    def test_merge_tracked_cards_upserts_by_id_keeps_order(self):
        a = {"card_id": "a", "card_type": "section", "section_id": "a", "section_name": "A", "section_content": "old", "subsections": []}
        b = {"card_id": "b", "card_type": "section", "section_id": "b", "section_name": "B", "section_content": "", "subsections": []}
        a2 = {**a, "section_content": "new"}
        merged = _mod._merge_tracked_cards([a, b], [a2])
        self.assertEqual([c["card_id"] for c in merged], ["a", "b"])
        self.assertEqual(merged[0]["section_content"], "new")


class _FakeStructured:
    def __init__(self, result):
        self._result = result

    async def ainvoke(self, _messages):
        return self._result


class _FakeLLM:
    """Stands in for ANTHROPIC_LLM / OPENAI_LLM_LANGCHAIN."""

    def __init__(self, result):
        self._result = result
        self.calls = 0

    def with_structured_output(self, _schema, **_kwargs):
        self.calls += 1
        return _FakeStructured(self._result)


@unittest.skipIf(_mod is None, f"refine_request_router import failed: {_IMPORT_ERROR!r}")
class RoutingBehaviourTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.events = []
        patcher = patch.object(_mod, "save_raw_llm_response", lambda *a, **k: None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _router(self, routed_target):
        r = _mod.RefineRequestRouter(
            chat_id="chat-1",
            report_id="report-1",
            report_title="EV Market Report",
            event_writer=self.events.append,
        )
        self._llm_patch = patch.object(_mod, "ANTHROPIC_LLM", _FakeLLM({"raw": None, "parsed": routed_target}))
        self._llm_patch.start()
        self.addCleanup(self._llm_patch.stop)
        return r

    def _section_event(self, sid, name, content, subs=None):
        return {
            "name": "generate_report",
            "card_type": "section",
            "card_db": {
                "section": [{"id": sid, "name": name, "content": content}],
                "sub_sections": subs or [{"id": "", "name": "", "content": ""}],
                "citations": {},
            },
        }

    async def test_ingested_cards_accumulate_in_state(self):
        router = self._router(_mod.RoutedRefineTarget(target_kind="general", refine_instruction="x", confidence=0.5))
        await router.ingest_card_event(self._section_event("s1", "Overview", "Intro text"))
        await router.ingest_card_event(self._section_event("s2", "Competitive Landscape", "Rivals text"))
        tracked = await router.tracked_cards()
        self.assertEqual([c["section_name"] for c in tracked], ["Overview", "Competitive Landscape"])

    async def test_section_request_emits_routed_event_with_fe_json(self):
        router = self._router(
            _mod.RoutedRefineTarget(
                target_kind="section",
                section_id="s2",
                matched_name="Competitive Landscape",
                refine_instruction="Add a paragraph comparing BYD and Tesla margins.",
                confidence=0.9,
            )
        )
        await router.ingest_card_event(self._section_event("s1", "Overview", "Intro"))
        await router.ingest_card_event(self._section_event("s2", "Competitive Landscape", "Rivals"))

        out = await router.route_edit_request("add something about BYD vs Tesla margins")

        self.assertEqual(len(self.events), 1)
        evt = self.events[0]
        self.assertEqual(evt["name"], "refine_request_routed")
        self.assertEqual(evt["status"], "routed")
        self.assertEqual(evt["fe_json_for_refine"], {
            "id": "s2",
            "refine_or_delete_prompt": "Add a paragraph comparing BYD and Tesla margins.",
        })
        self.assertEqual(out["fe_json_for_refine"]["id"], "s2")

    async def test_subsection_request_nests_prompt_under_subsection(self):
        router = self._router(
            _mod.RoutedRefineTarget(
                target_kind="subsection",
                section_id="s1",
                subsection_id="sub-a",
                matched_name="Regional split",
                refine_instruction="Break APAC into China and rest-of-APAC.",
                confidence=0.8,
            )
        )
        await router.ingest_card_event(
            self._section_event("s1", "Overview", "Intro", subs=[{"id": "sub-a", "name": "Regional split", "content": "APAC leads"}])
        )

        await router.route_edit_request("split the APAC row")

        fe = self.events[0]["fe_json_for_refine"]
        self.assertEqual(fe, {
            "id": "s1",
            "subsection": {
                "id": "sub-a",
                "refine_or_delete_prompt": "Break APAC into China and rest-of-APAC.",
            },
        })
        self.assertNotIn("refine_or_delete_prompt", fe)  # no top-level prompt

    async def test_unknown_target_is_marked_unresolved(self):
        router = self._router(
            _mod.RoutedRefineTarget(target_kind="unknown", refine_instruction="add a pricing section", confidence=0.1)
        )
        await router.ingest_card_event(self._section_event("s1", "Overview", "Intro"))

        await router.route_edit_request("add a whole section on pricing")

        self.assertEqual(self.events[0]["status"], "unresolved")
        self.assertEqual(self.events[0]["fe_json_for_refine"]["refine_or_delete_prompt"], "add a pricing section")

    async def test_handle_post_report_edit_request_reads_string_request(self):
        router = self._router(
            _mod.RoutedRefineTarget(target_kind="section", section_id="s1", refine_instruction="Make it shorter.", confidence=0.7)
        )
        await router.ingest_card_event(self._section_event("s1", "Overview", "Intro"))

        out = await router.handle_post_report_edit_request(
            {"name": "post_report_edit_request", "status": "captured", "request": "trim the overview"}
        )
        self.assertEqual(out["fe_json_for_refine"]["id"], "s1")

    async def test_empty_request_returns_none_and_emits_nothing(self):
        router = self._router(_mod.RoutedRefineTarget(target_kind="general", refine_instruction="", confidence=0.0))
        self.assertIsNone(await router.route_edit_request("   "))
        self.assertEqual(self.events, [])


if __name__ == "__main__":
    unittest.main()
