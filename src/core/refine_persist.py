"""Persist a generated refined card and apply the Ask Caspr thread policy.

This helper is persist-only. It does not generate section text — call
``refine_card()`` first, then pass the result here.

Thread policies (named, not a boolean):

- ``reset_thread`` — insert a new ``ask_caspr_chats`` row with ``chat=NULL``.
  Latest ``created_at`` wins, so the section thread is empty. Used by the
  old ``/refine-card`` button.
- ``keep_thread`` — update the existing row's ``card_version`` (and optional
  conversation). No insert. Phase 3 Ask Caspr confirm will use this.

Caller must still:

- run analytics (``enrich_terminal_search_analytics`` + ``log_web_search_event``)
- run ``replace_visualization_uris_in_card`` before returning the card to the client

``refine_card_in_db`` commits the new card version on its own. History +
thread policy then share one session commit so they stay together. If that
second step fails, the card is already saved (same as today's /refine-card).
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from src.config.log_helper import setup_logging
from src.db.ask_caspr_db import update_ask_caspr_chat_after_refine
from src.db.async_db_functions import (
    get_latest_ask_caspr_version,
    insert_ask_caspr_chat_entry,
    refine_card_in_db,
    update_refinement_history,
    update_report_status_if_needed,
)
from src.db.enums import ReportStatus

logger = setup_logging(__file__)

THREAD_POLICY_RESET = "reset_thread"
THREAD_POLICY_KEEP = "keep_thread"
_VALID_THREAD_POLICIES = frozenset({THREAD_POLICY_RESET, THREAD_POLICY_KEEP})


async def persist_refined_card(
    session: AsyncSession,
    report_id: str,
    updated_card: Dict[str, Any],
    user_instruction: str,
    refinement_type: str,
    table_id_markdown_map: Dict[str, str],
    subsection_id: Optional[str],
    updated_refinement_history: list,
    section_id: str,
    thread_policy: str,
    entry_id: Optional[str] = None,
    chat: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Save the refined card, history, thread policy, and REDO_ANALYSIS.

    Returns the fields ``/refine-card`` needs: ``success``, ``version``,
    ``parentcard_id``, ``card_id``, ``updated_card``.
    """
    if thread_policy not in _VALID_THREAD_POLICIES:
        return {"success": False, "error": f"Unknown thread_policy: {thread_policy}"}
    if thread_policy == THREAD_POLICY_KEEP and not entry_id:
        return {"success": False, "error": "keep_thread requires entry_id"}

    db_response = await refine_card_in_db(
        session=session,
        report_id=report_id,
        card_data=updated_card,
        user_instruction=user_instruction,
        refinement_type=refinement_type,
        table_id_markdown_map=table_id_markdown_map or {},
        subsection_id=subsection_id,
    )

    if not db_response.get("success", False):
        logger.error(f"Failed to save refined card to database: {db_response.get('error')}")
        return db_response

    logger.info(f"Successfully refined card with ID: {db_response.get('parentcard_id')}")
    new_card_version = db_response.get("version", 1)

    try:
        history_result = await update_refinement_history(
            report_id=report_id,
            refine_history=updated_refinement_history,
            session=session,
        )
        if not history_result.get("success"):
            raise Exception(f"Failed to update refinement history: {history_result.get('error')}")
        logger.info(
            f"Updated refinement history for report_id: {report_id} "
            f"with {len(updated_refinement_history)} entries"
        )

        if thread_policy == THREAD_POLICY_RESET:
            latest_ask_caspr_version = await get_latest_ask_caspr_version(
                report_id=report_id,
                section_id=section_id,
                session=session,
                subsection_id=subsection_id,
            )
            next_version = latest_ask_caspr_version + 1
            logger.info(
                f"Ask Caspr next version for section_id: {section_id}, "
                f"subsection_id: {subsection_id} = {next_version}, "
                f"card_version: {new_card_version}"
            )
            ask_caspr_result = await insert_ask_caspr_chat_entry(
                report_id=report_id,
                section_id=section_id,
                session=session,
                subsection_id=subsection_id,
                version=next_version,
                card_version=new_card_version,
            )
            if not ask_caspr_result.get("success"):
                raise Exception(
                    f"Failed to insert Ask Caspr chat entry: {ask_caspr_result.get('error')}"
                )
            logger.info(
                f"Inserted Ask Caspr chat entry for section_id: {section_id}, "
                f"subsection_id: {subsection_id}"
            )
        else:
            keep_result = await update_ask_caspr_chat_after_refine(
                entry_id=entry_id,
                card_version=new_card_version,
                session=session,
                chat=chat,
            )
            if not keep_result.get("success"):
                raise Exception(
                    f"Failed to update Ask Caspr chat after refine: {keep_result.get('error')}"
                )
            logger.info(
                f"Kept Ask Caspr thread entry_id={entry_id} "
                f"card_version={new_card_version}"
            )

        await session.commit()
        logger.info(
            f"Committed refinement history and Ask Caspr thread policy "
            f"({thread_policy}) for report_id: {report_id}"
        )

    except Exception as post_refine_err:
        await session.rollback()
        logger.error(
            f"Post-refine DB operations failed, rolled back: {post_refine_err}",
            exc_info=True,
        )
        # Card refinement itself already committed, so we still return success
        # but log the failure for refinement history / ask caspr chat

    await update_report_status_if_needed(
        report_id=report_id,
        new_status=ReportStatus.REDO_ANALYSIS.value,
        session=session,
    )
    logger.info(f"Set report status to REDO_ANALYSIS for report_id: {report_id}")

    return {
        "success": True,
        "parentcard_id": db_response.get("parentcard_id"),
        "card_id": db_response.get("card_id"),
        "version": new_card_version,
        "updated_card": updated_card,
    }
