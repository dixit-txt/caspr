"""
ask_caspr_db.py
Database operation functions for Ask Caspr chat functionality.

This module provides async functions for managing Ask Caspr chat entries,
including fetching the latest chat, updating conversations, and retrieving
card data needed for the Ask Caspr session.
"""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from app.core.logging import setup_logging
from app.models import AskCasprChat, Card, RefinementHistory, Report, ReportVersionCard

logger = setup_logging(__file__)


async def validate_section_exists_in_ask_caspr(
    report_id: str, section_id: str, session: AsyncSession, subsection_id: str | None = None
) -> dict[str, Any]:
    """
    Validate that a section (and optionally subsection) exists in ask_caspr_chats table.

    This ensures the report has the specified section/subsection before allowing
    a session to be created.

    Args:
        report_id (str): ID of the report.
        section_id (str): Business card_id (section ID).
        session (AsyncSession): SQLAlchemy async session.
        subsection_id (str, optional): Subsection ID. None for section-level validation.

    Returns:
        Dict[str, Any]: Dictionary with success status and exists flag.
    """
    logger.info(
        f"[DB_VALIDATE_SECTION] Starting validation | report_id={report_id} | section_id={section_id} | subsection_id={subsection_id}"
    )

    try:
        stmt = select(AskCasprChat).where(
            AskCasprChat.report_id == report_id, AskCasprChat.section_id == section_id
        )

        if subsection_id:
            stmt = stmt.where(AskCasprChat.subsection_id == subsection_id)
        else:
            stmt = stmt.where(AskCasprChat.subsection_id.is_(None))

        stmt = stmt.limit(1)
        logger.info("[DB_VALIDATE_SECTION] Executing query")

        result = await session.execute(stmt)
        entry = result.scalar_one_or_none()

        if entry is None:
            if subsection_id:
                logger.warning(
                    f"[DB_VALIDATE_SECTION] Subsection not found | report_id={report_id} | section_id={section_id} | subsection_id={subsection_id}"
                )
                return {
                    "success": True,
                    "exists": False,
                    "message": f"Subsection '{subsection_id}' not found in section '{section_id}'",
                }
            else:
                logger.warning(
                    f"[DB_VALIDATE_SECTION] Section not found | report_id={report_id} | section_id={section_id}"
                )
                return {
                    "success": True,
                    "exists": False,
                    "message": f"Section '{section_id}' not found in report",
                }

        logger.info(
            f"[DB_VALIDATE_SECTION] Validation successful | entry_id={entry.id} | version={entry.version}"
        )
        return {"success": True, "exists": True, "entry_id": entry.id}

    except SQLAlchemyError as e:
        logger.error(
            f"[DB_VALIDATE_SECTION] Database error | report_id={report_id} | section_id={section_id} | error={e!s}",
            exc_info=True,
        )
        return {"success": False, "error": f"Database error: {e!s}"}
    except Exception as e:
        logger.error(
            f"[DB_VALIDATE_SECTION] Unexpected error | report_id={report_id} | section_id={section_id} | error={e!s}",
            exc_info=True,
        )
        return {"success": False, "error": f"Unexpected error: {e!s}"}


async def get_latest_ask_caspr_chat_entry(
    report_id: str, section_id: str, session: AsyncSession, subsection_id: str | None = None
) -> dict[str, Any]:
    """
    Fetch the latest Ask Caspr chat entry for a given section or subsection.

    The latest entry is determined by created_at DESC — this is the one
    the frontend uses and the one whose conversation should be continued.

    Args:
        report_id (str): ID of the report.
        section_id (str): Business card_id (section ID).
        session (AsyncSession): SQLAlchemy async session.
        subsection_id (str, optional): Subsection ID. None for section-level chat.

    Returns:
        Dict[str, Any]: Dictionary with success status and chat entry data.
    """
    logger.info(
        f"[DB_GET_CHAT_ENTRY] Fetching latest chat entry | report_id={report_id} | section_id={section_id} | subsection_id={subsection_id}"
    )

    try:
        stmt = select(AskCasprChat).where(
            AskCasprChat.report_id == report_id, AskCasprChat.section_id == section_id
        )

        if subsection_id:
            stmt = stmt.where(AskCasprChat.subsection_id == subsection_id)
        else:
            stmt = stmt.where(AskCasprChat.subsection_id.is_(None))

        stmt = stmt.order_by(AskCasprChat.created_at.desc()).limit(1)
        logger.info("[DB_GET_CHAT_ENTRY] Executing query")

        result = await session.execute(stmt)
        entry = result.scalar_one_or_none()

        if entry is None:
            logger.warning(
                f"[DB_GET_CHAT_ENTRY] No chat entry found | report_id={report_id} | section_id={section_id} | subsection_id={subsection_id}"
            )
            return {"success": True, "entry": None, "chat": None, "entry_id": None, "version": None}

        chat_length = len(entry.chat) if entry.chat else 0
        logger.info(
            f"[DB_GET_CHAT_ENTRY] Chat entry found | entry_id={entry.id} | version={entry.version} | chat_length={chat_length} | created_at={entry.created_at}"
        )
        return {
            "success": True,
            "entry_id": entry.id,
            "chat": entry.chat,
            "version": entry.version,
            "created_at": entry.created_at.isoformat() if entry.created_at else None,
        }

    except SQLAlchemyError as e:
        logger.error(
            f"[DB_GET_CHAT_ENTRY] Database error | report_id={report_id} | section_id={section_id} | error={e!s}",
            exc_info=True,
        )
        return {"success": False, "error": f"Database error: {e!s}"}
    except Exception as e:
        logger.error(
            f"[DB_GET_CHAT_ENTRY] Unexpected error | report_id={report_id} | section_id={section_id} | error={e!s}",
            exc_info=True,
        )
        return {"success": False, "error": f"Unexpected error: {e!s}"}


async def update_ask_caspr_chat_conversation(
    entry_id: str, chat: list[dict[str, Any]], session: AsyncSession
) -> dict[str, Any]:
    """
    Update the chat JSONB field for an existing Ask Caspr chat entry.

    This stores the full conversation history. Assistant messages may include
    extra keys such as refine_preview; unknown keys are kept as-is.

    Args:
        entry_id (str): ID of the Ask Caspr chat entry to update.
        chat (list): The full conversation history as a list of dicts.
        session (AsyncSession): SQLAlchemy async session.

    Returns:
        Dict[str, Any]: Dictionary with success status.
    """
    logger.info(
        f"[DB_UPDATE_CHAT] Updating chat conversation | entry_id={entry_id} | new_messages_count={len(chat)}"
    )

    try:
        stmt = select(AskCasprChat).where(AskCasprChat.id == entry_id)
        result = await session.execute(stmt)
        entry = result.scalar_one_or_none()

        if entry is None:
            logger.error(f"[DB_UPDATE_CHAT] Chat entry not found | entry_id={entry_id}")
            return {"success": False, "error": f"Chat entry not found: {entry_id}"}

        old_chat_length = len(entry.chat) if entry.chat else 0
        logger.info(
            f"[DB_UPDATE_CHAT] Found entry | entry_id={entry_id} | old_chat_length={old_chat_length} | new_chat_length={len(chat)}"
        )

        entry.chat = chat
        flag_modified(entry, "chat")
        await session.commit()

        logger.info(
            f"[DB_UPDATE_CHAT] Chat updated successfully | entry_id={entry_id} | messages_count={len(chat)}"
        )
        return {"success": True}

    except SQLAlchemyError as e:
        await session.rollback()
        logger.error(
            f"[DB_UPDATE_CHAT] Database error | entry_id={entry_id} | error={e!s}", exc_info=True
        )
        return {"success": False, "error": f"Database error: {e!s}"}
    except Exception as e:
        await session.rollback()
        logger.error(
            f"[DB_UPDATE_CHAT] Unexpected error | entry_id={entry_id} | error={e!s}",
            exc_info=True,
        )
        return {"success": False, "error": f"Unexpected error: {e!s}"}


async def update_ask_caspr_chat_after_refine(
    entry_id: str,
    card_version: int,
    session: AsyncSession,
    chat: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Update the existing Ask Caspr row after a refine (keep_thread).

    Same row. No insert. Sets ``card_version`` to the version returned by
    ``refine_card_in_db``. Replaces ``chat`` JSON only when the caller
    passes ``chat`` (including an empty list). ``chat=None`` leaves the
    conversation untouched — it does **not** null the field.

    Uses flush() (not commit) so the persist helper owns the transaction.
    """
    logger.info(
        f"[DB_UPDATE_AFTER_REFINE] Updating chat after refine | "
        f"entry_id={entry_id} | card_version={card_version} | "
        f"chat_provided={chat is not None}"
    )

    try:
        stmt = select(AskCasprChat).where(AskCasprChat.id == entry_id)
        result = await session.execute(stmt)
        entry = result.scalar_one_or_none()

        if entry is None:
            logger.error(f"[DB_UPDATE_AFTER_REFINE] Chat entry not found | entry_id={entry_id}")
            return {"success": False, "error": f"Chat entry not found: {entry_id}"}

        entry.card_version = card_version
        if chat is not None:
            entry.chat = chat
            flag_modified(entry, "chat")
        entry.updated_at = datetime.now(UTC)
        await session.flush()

        logger.info(
            f"[DB_UPDATE_AFTER_REFINE] Staged keep_thread update | "
            f"entry_id={entry_id} | card_version={card_version}"
        )
        return {"success": True, "entry_id": entry.id, "card_version": card_version}

    except SQLAlchemyError as e:
        logger.error(
            f"[DB_UPDATE_AFTER_REFINE] Database error | entry_id={entry_id} | error={e!s}",
            exc_info=True,
        )
        return {"success": False, "error": f"Database error: {e!s}"}
    except Exception as e:
        logger.error(
            f"[DB_UPDATE_AFTER_REFINE] Unexpected error | entry_id={entry_id} | error={e!s}",
            exc_info=True,
        )
        return {"success": False, "error": f"Unexpected error: {e!s}"}


async def get_card_data_for_ask_caspr(
    report_id: str, section_id: str, session: AsyncSession, subsection_id: str | None = None
) -> dict[str, Any]:
    """
    Get the card data needed for an Ask Caspr session.

    Fetches the active card matching the section_id and extracts:
    - section_or_subsection: Name of the section or specific subsection
    - citations_for_current_section: Citations dict for the card
    - content: The markdown content of the section or subsection

    Args:
        report_id (str): ID of the report.
        section_id (str): Business card_id (section ID).
        session (AsyncSession): SQLAlchemy async session.
        subsection_id (str, optional): If provided, returns subsection-specific data.

    Returns:
        Dict[str, Any]: Dictionary with card data for Ask Caspr session.
    """
    logger.info(
        f"[DB_GET_CARD_DATA] Fetching card data | report_id={report_id} | section_id={section_id} | subsection_id={subsection_id}"
    )

    try:
        # Get the active card by business card_id
        stmt = select(Card).where(
            Card.card_id == section_id,
            Card.report_id == report_id,
            Card.is_active == True,
            Card.is_deleted == False,
        )
        logger.info("[DB_GET_CARD_DATA] Executing query")
        result = await session.execute(stmt)
        card = result.scalar_one_or_none()

        if card is None:
            logger.warning(
                f"[DB_GET_CARD_DATA] No active card found | section_id={section_id} | report_id={report_id}"
            )
            return {"success": False, "error": "Card not found for the given section"}

        logger.info(
            f"[DB_GET_CARD_DATA] Card found | card_id={card.id} | card_title={card.title} | version={card.version}"
        )

        # Extract citations for this section
        citations = card.citations or {}
        logger.info(f"[DB_GET_CARD_DATA] Card citations | count={len(citations)}")

        # Determine section_or_subsection name and content
        if subsection_id:
            # Find the specific subsection
            logger.info(
                f"[DB_GET_CARD_DATA] Looking for subsection | subsection_id={subsection_id}"
            )
            subsection_data = None
            if card.sub_sections:
                for sub in card.sub_sections:
                    if isinstance(sub, dict) and sub.get("id") == subsection_id:
                        subsection_data = sub
                        break

            if subsection_data is None:
                logger.warning(
                    f"[DB_GET_CARD_DATA] Subsection not found | subsection_id={subsection_id} | section_id={section_id}"
                )
                return {"success": False, "error": f"Subsection not found: {subsection_id}"}

            section_or_subsection = subsection_data.get("name", "")
            content = subsection_data.get("content", "")
            logger.info(
                f"[DB_GET_CARD_DATA] Subsection data extracted | name={section_or_subsection[:80]}... | content_length={len(content)}"
            )
        else:
            # Section-level: extract from content JSONB
            if isinstance(card.content, dict):
                section_or_subsection = card.content.get("name", card.title or "")
                content = card.content.get("content", "")
                logger.info(
                    f"[DB_GET_CARD_DATA] Section data from content dict | name={section_or_subsection[:80]}... | content_length={len(content)}"
                )
            else:
                section_or_subsection = card.title or ""
                content = card.content or ""
                logger.info(
                    f"[DB_GET_CARD_DATA] Section data from card fields | name={section_or_subsection[:80]}... | content_length={len(content)}"
                )

        logger.info(
            f"[DB_GET_CARD_DATA] Card data fetch successful | section={section_or_subsection[:80]}... | citations_count={len(citations)}"
        )
        return {
            "success": True,
            "section_or_subsection": section_or_subsection,
            "citations_for_current_section": citations,
            "content": content,
            "card_title": card.title,
        }

    except SQLAlchemyError as e:
        logger.error(
            f"[DB_GET_CARD_DATA] Database error | report_id={report_id} | section_id={section_id} | error={e!s}",
            exc_info=True,
        )
        return {"success": False, "error": f"Database error: {e!s}"}
    except Exception as e:
        logger.error(
            f"[DB_GET_CARD_DATA] Unexpected error | report_id={report_id} | section_id={section_id} | error={e!s}",
            exc_info=True,
        )
        return {"success": False, "error": f"Unexpected error: {e!s}"}


async def get_report_data_for_ask_caspr(report_id: str, session: AsyncSession) -> dict[str, Any]:
    """
    Get report-level data needed for Ask Caspr session.

    Fetches file_id, citations, and s3_uri from the reports table.

    Args:
        report_id (str): ID of the report.
        session (AsyncSession): SQLAlchemy async session.

    Returns:
        Dict[str, Any]: Dictionary with report data including s3_uri for file paths.
    """
    logger.info(f"[DB_GET_REPORT_DATA] Fetching report data | report_id={report_id}")

    try:
        stmt = select(Report).where(Report.id == report_id)
        logger.info("[DB_GET_REPORT_DATA] Executing query")
        result = await session.execute(stmt)
        report = result.scalar_one_or_none()

        if report is None:
            logger.warning(f"[DB_GET_REPORT_DATA] Report not found | report_id={report_id}")
            return {"success": False, "error": "Report not found"}

        citations_count = len(report.citations) if report.citations else 0
        logger.info(
            f"[DB_GET_REPORT_DATA] Report found | report_id={report_id} | title={report.title} | file_id={report.file_id} | chat_id={report.chat_id} | citations_count={citations_count} | has_markdown={bool(report.initial_markdown)}"
        )

        return {
            "success": True,
            "file_id": report.file_id,
            "citations": report.citations or {},
            "chat_id": report.chat_id,
            "title": report.title,
            "initial_markdown": report.initial_markdown,
        }

    except SQLAlchemyError as e:
        logger.error(
            f"[DB_GET_REPORT_DATA] Database error | report_id={report_id} | error={e!s}",
            exc_info=True,
        )
        return {"success": False, "error": f"Database error: {e!s}"}
    except Exception as e:
        logger.error(
            f"[DB_GET_REPORT_DATA] Unexpected error | report_id={report_id} | error={e!s}",
            exc_info=True,
        )
        return {"success": False, "error": f"Unexpected error: {e!s}"}


async def get_refinement_history_for_ask_caspr(
    report_id: str, session: AsyncSession
) -> list[dict[str, Any]]:
    """
    Get the refinement history for a report (used as context for Ask Caspr).

    Args:
        report_id (str): ID of the report.
        session (AsyncSession): SQLAlchemy async session.

    Returns:
        List[Dict[str, Any]]: List of refinement history entries (empty list if none).
    """
    logger.info(f"[DB_GET_REFINEMENT] Fetching refinement history | report_id={report_id}")

    try:
        stmt = select(RefinementHistory).where(RefinementHistory.report_id == report_id)
        logger.info("[DB_GET_REFINEMENT] Executing query")
        result = await session.execute(stmt)
        record = result.scalar_one_or_none()

        if record is None or record.refine_history is None:
            logger.info(f"[DB_GET_REFINEMENT] No refinement history found | report_id={report_id}")
            return []

        refinement_count = len(record.refine_history)
        logger.info(
            f"[DB_GET_REFINEMENT] Refinement history found | report_id={report_id} | refinement_count={refinement_count}"
        )
        return record.refine_history

    except Exception as e:
        logger.error(
            f"[DB_GET_REFINEMENT] Error fetching refinement history | report_id={report_id} | error={e!s}",
            exc_info=True,
        )
        return []


async def get_ask_caspr_chat_for_version(
    report_id: str,
    section_id: str,
    session: AsyncSession,
    subsection_id: str | None = None,
    report_version_id: str | None = None,
) -> dict[str, Any]:
    """
    Get the Ask Caspr chat entry for a section/subsection.

    Two modes:
    1. report_version_id is None  → return the LATEST entry (by created_at DESC).
       This is used for the current/active report version.
    2. report_version_id is given → find which cards.version was linked to that
       report version for the given section_id, then return the ask_caspr_chats
       entry whose card_version <= that version (latest match).
       This is used for historical report version navigation.

    Args:
        report_id:         ID of the report.
        section_id:        Business card_id (cards.card_id).
        session:           SQLAlchemy async session.
        subsection_id:     Subsection ID. None for section-level chat.
        report_version_id: ReportVersion.id. None → latest.

    Returns:
        Dict with success, entry_id, chat, version, card_version, created_at.
    """
    logger.info(
        f"[DB_GET_CHAT_VERSION] Fetching chat for version | report_id={report_id} | section_id={section_id} | subsection_id={subsection_id} | report_version_id={report_version_id}"
    )

    try:
        # ── Mode 1: Latest (no report_version_id) ──────────────────────────
        if not report_version_id:
            logger.info("[DB_GET_CHAT_VERSION] Mode: LATEST (no version specified)")
            stmt = select(AskCasprChat).where(
                AskCasprChat.report_id == report_id, AskCasprChat.section_id == section_id
            )
            if subsection_id:
                stmt = stmt.where(AskCasprChat.subsection_id == subsection_id)
            else:
                stmt = stmt.where(AskCasprChat.subsection_id.is_(None))

            stmt = stmt.order_by(AskCasprChat.created_at.desc()).limit(1)
            logger.info("[DB_GET_CHAT_VERSION] Executing latest chat query")
            result = await session.execute(stmt)
            entry = result.scalar_one_or_none()

            if entry is None:
                logger.info("[DB_GET_CHAT_VERSION] No chat found (latest mode)")
                return {
                    "success": True,
                    "entry_id": None,
                    "chat": None,
                    "version": None,
                    "card_version": None,
                    "created_at": None,
                }

            chat_length = len(entry.chat) if entry.chat else 0
            logger.info(
                f"[DB_GET_CHAT_VERSION] Latest chat found | entry_id={entry.id} | version={entry.version} | card_version={entry.card_version} | chat_length={chat_length}"
            )
            return {
                "success": True,
                "entry_id": entry.id,
                "chat": entry.chat,
                "version": entry.version,
                "card_version": entry.card_version,
                "created_at": entry.created_at.isoformat() if entry.created_at else None,
            }

        # ── Mode 2: Historical (report_version_id provided) ────────────────
        logger.info(
            f"[DB_GET_CHAT_VERSION] Mode: HISTORICAL | report_version_id={report_version_id}"
        )

        # Step 1: Find the cards.version for this section in the given report version.
        #   report_version_cards.report_version_id → cards.id (parent_card_id)
        #   cards.card_id == section_id  (business card id)
        card_stmt = (
            select(Card.version)
            .join(ReportVersionCard, ReportVersionCard.parent_card_id == Card.id)
            .where(
                ReportVersionCard.report_version_id == report_version_id, Card.card_id == section_id
            )
        )
        logger.info(
            f"[DB_GET_CHAT_VERSION] Finding target card version | report_version_id={report_version_id} | section_id={section_id}"
        )
        card_result = await session.execute(card_stmt)
        target_card_version = card_result.scalar_one_or_none()

        if target_card_version is None:
            logger.warning(
                f"[DB_GET_CHAT_VERSION] No card found for section in version | section_id={section_id} | report_version_id={report_version_id}"
            )
            return {
                "success": True,
                "entry_id": None,
                "chat": None,
                "version": None,
                "card_version": None,
                "created_at": None,
                "message": "No card found for this section in the given report version.",
            }

        logger.info(
            f"[DB_GET_CHAT_VERSION] Target card version found | section_id={section_id} | target_card_version={target_card_version}"
        )

        # Step 2: Get the ask_caspr_chats entry where card_version <= target_card_version
        chat_stmt = select(AskCasprChat).where(
            AskCasprChat.report_id == report_id,
            AskCasprChat.section_id == section_id,
            AskCasprChat.card_version <= target_card_version,
        )
        if subsection_id:
            chat_stmt = chat_stmt.where(AskCasprChat.subsection_id == subsection_id)
        else:
            chat_stmt = chat_stmt.where(AskCasprChat.subsection_id.is_(None))

        chat_stmt = chat_stmt.order_by(
            AskCasprChat.card_version.desc(), AskCasprChat.created_at.desc()
        ).limit(1)

        logger.info(
            f"[DB_GET_CHAT_VERSION] Executing historical chat query | target_card_version={target_card_version}"
        )
        chat_result = await session.execute(chat_stmt)
        entry = chat_result.scalar_one_or_none()

        if entry is None:
            logger.info(
                f"[DB_GET_CHAT_VERSION] No chat found for historical version | target_card_version={target_card_version}"
            )
            return {
                "success": True,
                "entry_id": None,
                "chat": None,
                "version": None,
                "card_version": None,
                "created_at": None,
                "target_card_version": target_card_version,
                "message": "No chat history found for this section at the given report version.",
            }

        chat_length = len(entry.chat) if entry.chat else 0
        logger.info(
            f"[DB_GET_CHAT_VERSION] Historical chat found | entry_id={entry.id} | version={entry.version} | card_version={entry.card_version} | target_card_version={target_card_version} | chat_length={chat_length}"
        )
        return {
            "success": True,
            "entry_id": entry.id,
            "chat": entry.chat,
            "version": entry.version,
            "card_version": entry.card_version,
            "created_at": entry.created_at.isoformat() if entry.created_at else None,
            "target_card_version": target_card_version,
        }

    except SQLAlchemyError as e:
        logger.error(
            f"[DB_GET_CHAT_VERSION] Database error | report_id={report_id} | section_id={section_id} | error={e!s}",
            exc_info=True,
        )
        return {"success": False, "error": f"Database error: {e!s}"}
    except Exception as e:
        logger.error(
            f"[DB_GET_CHAT_VERSION] Unexpected error | report_id={report_id} | section_id={section_id} | error={e!s}",
            exc_info=True,
        )
        return {"success": False, "error": f"Unexpected error: {e!s}"}
