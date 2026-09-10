"""Phase 1 persist helper: reset_thread vs keep_thread."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.core.enums import ReportStatus
from app.research.refine.refine_persist import (
    THREAD_POLICY_KEEP,
    THREAD_POLICY_RESET,
    persist_refined_card,
)


class FakeSession:
    def __init__(self):
        self.committed = False
        self.rolled_back = False

    async def commit(self):
        self.committed = True

    async def rollback(self):
        self.rolled_back = True


def _card():
    return {"section": [{"id": "sec-1", "name": "Intro", "content": "hi"}]}


def _persist_kwargs(session, **overrides):
    args = dict(
        session=session,
        report_id="rep-1",
        updated_card=_card(),
        user_instruction="make shorter",
        refinement_type="refine_section",
        table_id_markdown_map={},
        subsection_id=None,
        updated_refinement_history=[{"instruction": "make shorter"}],
        section_id="sec-1",
        thread_policy=THREAD_POLICY_RESET,
    )
    args.update(overrides)
    return args


class PersistRefinedCardTests(unittest.IsolatedAsyncioTestCase):
    async def test_unknown_policy_does_not_touch_db(self):
        session = FakeSession()
        with patch(
            "app.research.refine.refine_persist.refine_card_in_db", new_callable=AsyncMock
        ) as refine_db:
            result = await persist_refined_card(
                **_persist_kwargs(session, thread_policy="wipe_chat")
            )
        self.assertFalse(result["success"])
        self.assertIn("Unknown thread_policy", result["error"])
        refine_db.assert_not_called()

    async def test_keep_thread_requires_entry_id(self):
        session = FakeSession()
        with patch(
            "app.research.refine.refine_persist.refine_card_in_db", new_callable=AsyncMock
        ) as refine_db:
            result = await persist_refined_card(
                **_persist_kwargs(session, thread_policy=THREAD_POLICY_KEEP, entry_id=None)
            )
        self.assertFalse(result["success"])
        self.assertIn("entry_id", result["error"])
        refine_db.assert_not_called()

    async def test_reset_thread_saves_card_history_and_inserts_blank_chat(self):
        session = FakeSession()
        with (
            patch(
                "app.research.refine.refine_persist.refine_card_in_db",
                new_callable=AsyncMock,
                return_value={
                    "success": True,
                    "parentcard_id": "pc-9",
                    "card_id": "sec-1",
                    "version": 4,
                },
            ) as refine_db,
            patch(
                "app.research.refine.refine_persist.update_refinement_history",
                new_callable=AsyncMock,
                return_value={"success": True},
            ) as hist,
            patch(
                "app.research.refine.refine_persist.get_latest_ask_caspr_version",
                new_callable=AsyncMock,
                return_value=2,
            ),
            patch(
                "app.research.refine.refine_persist.insert_ask_caspr_chat_entry",
                new_callable=AsyncMock,
                return_value={"success": True, "entry_id": "new-row"},
            ) as insert_chat,
            patch(
                "app.research.refine.refine_persist.update_ask_caspr_chat_after_refine",
                new_callable=AsyncMock,
            ) as keep,
            patch(
                "app.research.refine.refine_persist.update_report_status_if_needed",
                new_callable=AsyncMock,
                return_value={"success": True},
            ) as redo,
        ):
            result = await persist_refined_card(**_persist_kwargs(session))

        self.assertTrue(result["success"])
        self.assertEqual(result["version"], 4)
        self.assertEqual(result["parentcard_id"], "pc-9")
        self.assertEqual(result["updated_card"], _card())
        refine_db.assert_awaited_once()
        hist.assert_awaited_once()
        insert_chat.assert_awaited_once()
        insert_kwargs = insert_chat.await_args.kwargs
        self.assertEqual(insert_kwargs["version"], 3)
        self.assertEqual(insert_kwargs["card_version"], 4)
        self.assertIsNone(insert_kwargs["subsection_id"])
        keep.assert_not_called()
        redo.assert_awaited_once()
        self.assertEqual(redo.await_args.kwargs["new_status"], ReportStatus.REDO_ANALYSIS.value)
        self.assertTrue(session.committed)
        self.assertFalse(session.rolled_back)

    async def test_keep_thread_updates_same_row_and_does_not_insert(self):
        session = FakeSession()
        existing_chat = [{"role": "user", "content": "what is this section?"}]
        with (
            patch(
                "app.research.refine.refine_persist.refine_card_in_db",
                new_callable=AsyncMock,
                return_value={
                    "success": True,
                    "parentcard_id": "pc-9",
                    "card_id": "sec-1",
                    "version": 5,
                },
            ),
            patch(
                "app.research.refine.refine_persist.update_refinement_history",
                new_callable=AsyncMock,
                return_value={"success": True},
            ) as hist,
            patch(
                "app.research.refine.refine_persist.insert_ask_caspr_chat_entry",
                new_callable=AsyncMock,
            ) as insert_chat,
            patch(
                "app.research.refine.refine_persist.update_ask_caspr_chat_after_refine",
                new_callable=AsyncMock,
                return_value={"success": True, "entry_id": "same-row", "card_version": 5},
            ) as keep,
            patch(
                "app.research.refine.refine_persist.update_report_status_if_needed",
                new_callable=AsyncMock,
                return_value={"success": True},
            ),
        ):
            result = await persist_refined_card(
                **_persist_kwargs(
                    session,
                    thread_policy=THREAD_POLICY_KEEP,
                    entry_id="same-row",
                    chat=existing_chat,
                )
            )

        self.assertTrue(result["success"])
        self.assertEqual(result["version"], 5)
        hist.assert_awaited_once()
        insert_chat.assert_not_called()
        keep.assert_awaited_once()
        keep_kwargs = keep.await_args.kwargs
        self.assertEqual(keep_kwargs["entry_id"], "same-row")
        self.assertEqual(keep_kwargs["card_version"], 5)
        self.assertEqual(keep_kwargs["chat"], existing_chat)
        self.assertTrue(session.committed)

    async def test_refine_card_in_db_failure_returns_error(self):
        session = FakeSession()
        with (
            patch(
                "app.research.refine.refine_persist.refine_card_in_db",
                new_callable=AsyncMock,
                return_value={"success": False, "error": "No business card_id found in card data"},
            ),
            patch(
                "app.research.refine.refine_persist.update_refinement_history",
                new_callable=AsyncMock,
            ) as hist,
        ):
            result = await persist_refined_card(**_persist_kwargs(session))
        self.assertFalse(result["success"])
        hist.assert_not_called()
        self.assertFalse(session.committed)


class UpdateAskCasprChatAfterRefineTests(unittest.IsolatedAsyncioTestCase):
    async def test_updates_card_version_and_leaves_chat_when_none(self):
        from app.internal.repository_ask_caspr import update_ask_caspr_chat_after_refine

        entry = SimpleNamespace(
            id="same-row",
            card_version=1,
            chat=[{"role": "user", "content": "keep me"}],
            updated_at=None,
        )

        class Result:
            def scalar_one_or_none(self):
                return entry

        session = FakeSession()
        session.execute = AsyncMock(return_value=Result())
        session.flush = AsyncMock()

        result = await update_ask_caspr_chat_after_refine(
            entry_id="same-row",
            card_version=7,
            session=session,
            chat=None,
        )

        self.assertTrue(result["success"])
        self.assertEqual(result["entry_id"], "same-row")
        self.assertEqual(entry.card_version, 7)
        self.assertEqual(entry.chat, [{"role": "user", "content": "keep me"}])
        session.flush.assert_awaited_once()
        self.assertFalse(session.committed)

    async def test_replaces_chat_only_when_caller_passes_it(self):
        from app.internal.repository_ask_caspr import update_ask_caspr_chat_after_refine

        entry = SimpleNamespace(
            id="same-row",
            card_version=1,
            chat=[{"role": "user", "content": "old"}],
            updated_at=None,
        )

        class Result:
            def scalar_one_or_none(self):
                return entry

        session = FakeSession()
        session.execute = AsyncMock(return_value=Result())
        session.flush = AsyncMock()
        new_chat = [{"role": "assistant", "content": "Section updated"}]

        with patch("app.internal.repository_ask_caspr.flag_modified"):
            result = await update_ask_caspr_chat_after_refine(
                entry_id="same-row",
                card_version=8,
                session=session,
                chat=new_chat,
            )

        self.assertTrue(result["success"])
        self.assertEqual(entry.card_version, 8)
        self.assertEqual(entry.chat, new_chat)

    async def test_missing_row_does_not_insert(self):
        from app.internal.repository_ask_caspr import update_ask_caspr_chat_after_refine

        class Result:
            def scalar_one_or_none(self):
                return None

        session = FakeSession()
        session.execute = AsyncMock(return_value=Result())
        session.flush = AsyncMock()
        session.add = unittest.mock.Mock()

        result = await update_ask_caspr_chat_after_refine(
            entry_id="missing",
            card_version=2,
            session=session,
        )

        self.assertFalse(result["success"])
        session.add.assert_not_called()
        session.flush.assert_not_called()


class RefineCardRouteWiringTests(unittest.TestCase):
    def test_refine_card_route_uses_reset_thread_helper(self):
        from pathlib import Path

        # The refine_card handler moved from src/resources/routers/api.py to
        # app/cards/router_refine.py in the R-STRUCT-1 migration. The assertions
        # below are unchanged; only the path they read follows the handler.
        api_src = Path(__file__).resolve().parents[1] / "src" / "app" / "cards" / "router_refine.py"
        text = api_src.read_text()
        refine_fn = text.split("async def refine_content", 1)[1].split("async def revert_card", 1)[
            0
        ]
        self.assertIn("persist_refined_card(", refine_fn)
        self.assertIn("thread_policy=THREAD_POLICY_RESET", refine_fn)
        self.assertNotIn("insert_ask_caspr_chat_entry(", refine_fn)
        self.assertIn('"refine_card": updated_card', refine_fn)


if __name__ == "__main__":
    unittest.main()
