"""Database access for the chats bounded context.

Moved verbatim from ``src/db/async_db_functions.py`` during the R-STRUCT-1
migration. Function bodies are unchanged; only the import block was retargeted
at the new module paths.
"""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from uuid_utils import uuid7

from app.core.enums import ReportStatus
from app.core.logging import setup_logging
from app.dashboard.repository import _latest_report_per_chat_subq
from app.models import (
    AskCasprChat,
    Message,
)
from app.reports.constants import DEFAULT_DOMAIN_INTERNAL
from app.reports.repository import _to_external_domain

logger = setup_logging(__file__)


async def get_user_chat(user_id: str, chat_id: str, session: AsyncSession) -> dict[str, Any]:
    """
    Get message details for a specific chat_id.

    Args:
        chat_id (str): Chat ID to retrieve message for
        session (AsyncSession): SQLAlchemy async session

    Returns:
        Dict[str, Any]: Dictionary with success status and message if found
    """
    logger.info(f"Getting messages for chat_id: {chat_id}")

    if not chat_id:
        logger.error("Invalid chat_id: empty value provided")
        return {"success": False, "error": "Invalid chat ID: empty value provided"}

    if not user_id:
        logger.error("Invalid user_id: empty value provided")
        return {"success": False, "error": "Invalid user_id: empty value provided"}

    # redis_instance = get_redis_instance()
    # logger.info(f"Checking redis for chat_id: {chat_id}")
    # redis_response = await redis_instance.get_chat(chat_id, ttl=3600)
    # if redis_response.get('success'):
    #     logger.info(f"Chat with ID {chat_id} found in redis")
    #     return {
    #         "success": True,
    #         "message": {
    #             "id": redis_response.get('chat_data').get('chat_id'),
    #             "user_id": redis_response.get('chat_data').get('user_id'),
    #             "chat_title": redis_response.get('chat_data').get('chat_title'),
    #             "chat_messages": redis_response.get('chat_data').get('chat_messages'),
    #             "created_at": redis_response.get('chat_data').get('created_at'),
    #             "updated_at": redis_response.get('chat_data').get('updated_at')
    #         }
    #     }

    try:
        # Query for specific message by chat_id
        stmt = select(Message).where(Message.id == chat_id, Message.user_id == user_id)
        result = await session.execute(stmt)
        message = result.scalar_one_or_none()

        if message:
            logger.info(f"Found message with chat_id: {chat_id}")
            message_data = {
                "id": message.id,
                "user_id": message.user_id,
                "chat_title": message.chat_title,
                "chat_messages": message.chat_messages,
                "message_citations": message.message_citations or {},
                "created_at": message.created_at,
                "updated_at": message.updated_at,
            }
            return {"success": True, "message": message_data}
        else:
            # logger.warning(f"No message found with chat_id: {chat_id}")
            return {"success": True, "message": {}}

    except SQLAlchemyError as e:
        logger.error(f"Database error retrieving chat message: {e!s}", exc_info=True)
        return {"success": False, "error": str(e)}
    except Exception as e:
        logger.error(f"Unexpected error retrieving chat message: {e!s}", exc_info=True)
        return {"success": False, "error": str(e)}


async def insert_chats(message_data: dict[str, Any], session: AsyncSession) -> dict[str, Any]:
    """
    Insert a new message or update an existing message based on the presence of created_at.
    If created_at is present, it's a new chat; otherwise, it's an update to an existing chat.

    Args:
        message_data (Dict[str, Any]): Dictionary containing message data with keys:
            - chat_id: Chat identifier (required)
            - user_id: User ID (required)
            - created_at: Creation timestamp (required for new chats)
            - chat_title: Title of the chat (optional)
            - chat_messages: Complete message data stored as JSONB (optional)
            - updated_at: Update timestamp (optional, will be set if not provided)
        session (AsyncSession): SQLAlchemy async session

    Returns:
        Dict[str, Any]: Dictionary with success status and operation performed
    """
    logger.info(
        f"Processing chat for user_id: {message_data.get('user_id')} with chat_id: {message_data.get('chat_id')}"
    )

    if "user_id" not in message_data or not message_data["user_id"]:
        logger.error("Missing required field: user_id")
        return {"success": False, "error": "Missing required field: user_id"}

    # redis_instance = get_redis_instance()

    try:
        # First, check if the chat already exists in the database
        chat_id = message_data.get("chat_id")
        stmt = select(Message).where(Message.id == chat_id)
        result = await session.execute(stmt)
        existing_chat = result.scalar_one_or_none()

        if existing_chat:
            # Chat exists - UPDATE it
            logger.info(f"Updating existing chat with chat_id: {chat_id}")

            # Validate ownership before updating
            if existing_chat.user_id != message_data.get("user_id"):
                logger.error(
                    f"Ownership validation failed: User {message_data.get('user_id')} attempted to update chat {chat_id} owned by user {existing_chat.user_id}"
                )
                return {
                    "success": False,
                    "error": "Unauthorized: Cannot update chat owned by another user",
                }

            # Build update values dynamically
            update_values = {}
            if message_data.get("chat_messages") is not None:
                update_values["chat_messages"] = message_data["chat_messages"]
            if message_data.get("chat_title") is not None:
                update_values["chat_title"] = message_data["chat_title"]
            if message_data.get("updated_at") is not None:
                update_values["updated_at"] = message_data["updated_at"]
            if message_data.get("message_citations") is not None:
                # Merge new citations into the existing dict (preserves prior turns)
                merged = {
                    **(existing_chat.message_citations or {}),
                    **message_data["message_citations"],
                }
                update_values["message_citations"] = merged

            if update_values:
                stmt = update(Message).values(**update_values).where(Message.id == chat_id)
                await session.execute(stmt)
                await session.commit()
                logger.info(f"Chat updated with chat_id: {chat_id}")
            else:
                logger.warning(f"No fields to update for chat_id: {chat_id}")

            # Update redis
            # logger.info(f"Updating chat in redis with chat_id: {chat_id}")
            # redis_response = await redis_instance.update_chat(
            #     chat_data={
            #         'chat_id': chat_id,
            #         'chat_messages': message_data.get('chat_messages'),
            #         'chat_title': message_data.get('chat_title'),
            #         'updated_at': message_data.get('updated_at')
            #     },
            #     ttl=3600
            # )
            # if redis_response.get('success'):
            #     logger.info(f"Chat updated in redis with chat_id: {chat_id}")
            # else:
            #     logger.error(f"Failed to update chat in redis with chat_id: {chat_id}")

            return {"success": True, "chat_id": chat_id, "is_new_chat": False}
        else:
            # Chat doesn't exist - CREATE it
            logger.info(f"Creating new chat with chat_id: {chat_id}")

            if not message_data.get("created_at"):
                logger.error(f"Cannot create new chat without created_at for chat_id: {chat_id}")
                return {
                    "success": False,
                    "error": "Cannot create new chat without created_at timestamp",
                }

            # Create message record
            new_message = Message(
                user_id=message_data["user_id"],
                created_at=message_data["created_at"],
                chat_title=message_data.get("chat_title"),
                chat_messages=message_data.get("chat_messages"),
                message_citations=message_data.get("message_citations"),
                updated_at=message_data.get("updated_at"),
                id=chat_id,
            )

            session.add(new_message)
            await session.commit()
            logger.info(f"New chat created with chat_id {new_message.id}")

            # logger.info(f"Creating chat in redis with chat_id: {new_message.id}")
            # redis_response = await redis_instance.check_chat_exists(new_message.id, ttl=3600)
            # if redis_response.get('success') and not redis_response.get('exists'):
            # redis_response = await redis_instance.create_chat(message_data, ttl=3600)
            # if redis_response.get('success'):
            # logger.info(f"Chat created in redis with chat_id: {new_message.id}")
            # redis_response = await redis_instance.create_chat(message_data, ttl=3600)
            # if redis_response.get('success'):
            # logger.info(f"Chat created in redis with chat_id: {new_message.id}")

            return {"success": True, "chat_id": new_message.id, "is_new_chat": True}

    except SQLAlchemyError as e:
        logger.error(f"Database error inserting/updating message: {e!s}", exc_info=True)
        return {"success": False, "error": str(e)}
    except Exception as e:
        logger.error(f"Unexpected error inserting/updating message: {e!s}", exc_info=True)
        return {"success": False, "error": str(e)}


async def get_user_chats(
    user_id: int,
    session: AsyncSession,
    limit: int | None = None,
    offset: int = 0,
) -> dict[str, Any]:
    """
    Get chat list for a specific user_id.

    Performance notes:
    - Projects only the columns needed to render the sidebar/chat-list.
      Previously this SELECT pulled the entire `Message` row, including the
      large ``chat_messages`` JSONB column (can be many MB per row on active
      users). That dominates RDS "ClientWrite" time and wastes bandwidth.
    - Adds ``ORDER BY updated_at DESC`` so results are deterministic and the
      most-recent chats come first (what every UI expects).
    - Accepts optional ``limit``/``offset`` so callers can paginate. The
      default is unlimited for backwards compatibility with existing callers.

    Args:
        user_id (int): The user_id to get chats for.
        session (AsyncSession): SQLAlchemy async session.
        limit (Optional[int]): Max rows to return. ``None`` = no limit.
        offset (int): Rows to skip for pagination.

    Returns:
        Dict[str, Any]: {"success": bool, "chats": [...]} or error dict.
    """
    logger.info(f"Getting chat messages for user_id: {user_id} (limit={limit}, offset={offset})")

    if not user_id:
        logger.error("Invalid user_id: empty value provided")
        return {"success": False, "error": "Invalid user ID: empty value provided"}

    try:
        stmt = (
            select(
                Message.id,
                Message.chat_title,
                Message.created_at,
                Message.updated_at,
                Message.is_deleted,
                Message.deleted_at,
            )
            .where(Message.user_id == user_id, Message.is_deleted.is_(False))
            .order_by(Message.updated_at.desc().nullslast())
        )
        if offset:
            stmt = stmt.offset(offset)
        if limit is not None:
            stmt = stmt.limit(limit)

        result = await session.execute(stmt)
        rows = result.all()

        if not rows:
            logger.info(f"No chat messages found for user_id: {user_id}")
            return {"success": True, "chats": []}

        chat_list = [
            {
                "id": row.id,
                "chat_title": row.chat_title,
                "created_at": row.created_at.isoformat() if row.created_at else None,
                "updated_at": row.updated_at.isoformat() if row.updated_at else None,
                "is_deleted": row.is_deleted,
                "deleted_at": row.deleted_at.isoformat() if row.deleted_at else None,
            }
            for row in rows
        ]

        logger.info(f"Successfully retrieved {len(chat_list)} chats for user_id: {user_id}")
        return {"success": True, "chats": chat_list}

    except SQLAlchemyError as e:
        logger.error(f"Database error retrieving chat messages: {e!s}", exc_info=True)
        return {"success": False, "error": f"Database error: {e!s}"}

    except Exception as e:
        logger.error(f"Unexpected error retrieving chat messages: {e!s}", exc_info=True)
        return {"success": False, "error": f"Unexpected error: {e!s}"}


async def rename_chat(
    chat_id: str, user_id: str, new_title: str, session: AsyncSession
) -> dict[str, Any]:
    """
    Rename a chat by updating its title.

    Args:
        chat_id (str): The ID of the chat to rename
        user_id (str): The ID of the user who owns the chat
        new_title (str): The new title for the chat
        session (AsyncSession): SQLAlchemy async session

    Returns:
        Dict[str, Any]: Dictionary with success status and operation details
    """
    logger.info(f"Renaming chat with ID: {chat_id} for user_id: {user_id}")

    try:
        # Check if the chat exists and belongs to the user
        stmt = select(Message).where(Message.id == chat_id, Message.user_id == user_id)
        result = await session.execute(stmt)
        message = result.scalar_one_or_none()

        if not message:
            logger.error(f"Chat with ID: {chat_id} not found or does not belong to user: {user_id}")
            return {"success": False, "error": "Chat not found or does not belong to the user"}

        # Update the chat title
        message.chat_title = new_title
        message.updated_at = datetime.now(UTC)
        await session.commit()

        logger.info(
            f"Chat with ID: {chat_id} renamed successfully to '{new_title}' for user_id: {user_id}"
        )
        return {"success": True, "message": "Chat renamed successfully"}

    except SQLAlchemyError as e:
        logger.error(f"Database error renaming chat: {e!s}", exc_info=True)
        return {"success": False, "error": f"Database error: {e!s}"}

    except Exception as e:
        logger.error(f"Unexpected error renaming chat: {e!s}", exc_info=True)
        return {"success": False, "error": f"Unexpected error: {e!s}"}


async def mark_chat_deleted(chat_id: str, user_id: str, session: AsyncSession) -> dict[str, Any]:
    """
    Mark a chat as deleted without physically removing it from the database.

    Args:
        chat_id (str): The ID of the chat to mark as deleted
        user_id (str): The ID of the user who owns the chat
        session (AsyncSession): SQLAlchemy async session

    Returns:
        Dict[str, Any]: Dictionary with success status and operation details
    """
    logger.info(f"Marking chat with ID: {chat_id} as deleted for user_id: {user_id}")

    try:
        # Check if the chat exists and belongs to the user
        stmt = select(Message).where(Message.id == chat_id, Message.user_id == user_id)
        result = await session.execute(stmt)
        message = result.scalar_one_or_none()

        if not message:
            logger.error(f"Chat with ID: {chat_id} not found or does not belong to user: {user_id}")
            return {"success": False, "error": "Chat not found or does not belong to the user"}

        # Check if the chat is already deleted
        if message.is_deleted:
            logger.warning(f"Chat with ID: {chat_id} is already marked as deleted")
            return {"success": True, "message": "Chat was already marked as deleted"}

        # Mark the chat as deleted
        message.is_deleted = True
        message.deleted_at = datetime.now(UTC)
        await session.commit()

        logger.info(
            f"Chat with ID: {chat_id} marked as deleted successfully for user_id: {user_id}"
        )
        return {"success": True, "message": "Chat marked as deleted successfully"}

    except SQLAlchemyError as e:
        logger.error(f"Database error marking chat as deleted: {e!s}", exc_info=True)
        return {"success": False, "error": f"Database error: {e!s}"}

    except Exception as e:
        logger.error(f"Unexpected error marking chat as deleted: {e!s}", exc_info=True)
        return {"success": False, "error": f"Unexpected error: {e!s}"}


async def get_draft_chats_grouped(
    user_id: str,
    session: AsyncSession,
) -> dict[str, Any]:
    """Return the user's ongoing/draft chats, grouped by external category.

    A chat is "ongoing" when:
      - It has at least one report and that latest report's status is
        :pyattr:`ReportStatus.DRAFT`, OR
      - It has no report yet (the chat exists but no report has been
        generated). These are treated as drafts too.

    Drafts currently never carry a ``domain_name`` (the domain is written
    only after the report-generation tool call resolves), so today every
    item lands under the ``standard`` key. The grouping shape future-proofs
    the response so additional category keys can appear once drafts start
    carrying a domain.

    Returns:
        Dict with ``success`` and a ``groups`` dict keyed by external
        category slug, each value being a list of chat dicts ordered by
        ``updated_at`` desc.
    """
    logger.info(f"Fetching ongoing/draft chats for user_id={user_id}")

    try:
        latest = _latest_report_per_chat_subq()

        effective_status = func.coalesce(latest.c.status, ReportStatus.DRAFT.value)
        effective_domain = func.coalesce(latest.c.domain_name, DEFAULT_DOMAIN_INTERNAL)

        stmt = (
            select(
                Message.id.label("chat_id"),
                Message.chat_title.label("chat_title"),
                Message.created_at.label("created_at"),
                Message.updated_at.label("updated_at"),
                latest.c.poster_image_url,
                effective_status.label("eff_status"),
                effective_domain.label("eff_domain"),
            )
            .select_from(Message.__table__.outerjoin(latest, latest.c.chat_id == Message.id))
            .where(
                Message.user_id == user_id,
                Message.is_deleted.is_(False),
                effective_status == ReportStatus.DRAFT.value,
            )
            .order_by(Message.updated_at.desc().nullslast())
        )

        rows = (await session.execute(stmt)).all()

        groups: dict[str, list] = {}
        for r in rows:
            external_slug = _to_external_domain(r.eff_domain)
            groups.setdefault(external_slug, []).append(
                {
                    "chat_id": r.chat_id,
                    "chat_title": r.chat_title,
                    "status": r.eff_status,
                    "poster_image_url": r.poster_image_url,
                    "created_at": r.created_at.isoformat() if r.created_at else None,
                    "updated_at": r.updated_at.isoformat() if r.updated_at else None,
                }
            )

        logger.info(
            f"Found {len(rows)} draft chat(s) across {len(groups)} group(s) for user_id={user_id}"
        )
        return {"success": True, "groups": groups}

    except SQLAlchemyError as e:
        logger.error(f"DB error in get_draft_chats_grouped: {e!s}", exc_info=True)
        return {"success": False, "error": f"Database error: {e!s}"}
    except Exception as e:
        logger.error(f"Unexpected error in get_draft_chats_grouped: {e!s}", exc_info=True)
        return {"success": False, "error": f"Unexpected error: {e!s}"}


async def get_latest_ask_caspr_version(
    report_id: str, section_id: str, session: AsyncSession, subsection_id: str = None
) -> int:
    """
    Get the latest version number from ask_caspr_chats for a specific section or subsection.

    - For section refinement (subsection_id=None): fetches MAX(version) where section_id matches
      AND subsection_id IS NULL, so subsection records don't affect section versioning.
    - For subsection refinement: fetches MAX(version) where subsection_id matches.

    Args:
        report_id (str): ID of the report.
        section_id (str): Business card_id identifying the section.
        session (AsyncSession): SQLAlchemy async session.
        subsection_id (str, optional): Subsection ID. None for section-level lookup.

    Returns:
        int: The latest version found, or 0 if no records exist.
    """
    try:
        stmt = select(func.max(AskCasprChat.version)).where(
            AskCasprChat.report_id == report_id, AskCasprChat.section_id == section_id
        )

        if subsection_id:
            # Subsection refinement: match specific subsection
            stmt = stmt.where(AskCasprChat.subsection_id == subsection_id)
        else:
            # Section refinement: only look at section-level rows (subsection_id IS NULL)
            stmt = stmt.where(AskCasprChat.subsection_id.is_(None))

        result = await session.execute(stmt)
        max_version = result.scalar_one_or_none()

        version = max_version if max_version is not None else 0
        logger.info(
            f"Latest Ask Caspr version for section_id: {section_id}, subsection_id: {subsection_id} = {version}"
        )
        return version

    except Exception as e:
        logger.error(f"Error fetching latest Ask Caspr version: {e!s}", exc_info=True)
        return 0


async def insert_ask_caspr_chat_entry(
    report_id: str,
    section_id: str,
    session: AsyncSession,
    subsection_id: str = None,
    version: int = 1,
    card_version: int = 1,
) -> dict[str, Any]:
    """
    Insert a new Ask Caspr chat entry with chat=NULL for a section or subsection.

    This is called after a successful refinement to invalidate old chats.
    The frontend always picks the latest created_at entry, so inserting a new row
    with NULL chat effectively resets the conversation for that section/subsection.

    Note: This function uses flush() instead of commit() to allow the caller
    to control transaction boundaries. The caller MUST commit or rollback.

    Args:
        report_id (str): ID of the report.
        section_id (str): Business card_id (cards.card_id) identifying the logical section.
        session (AsyncSession): SQLAlchemy async session.
        subsection_id (str, optional): Subsection ID. NULL for section-level invalidation.
        version (int): Version number of the section/subsection content. Defaults to 1.
        card_version (int): cards.version at the time of creation — links to the card timeline. Defaults to 1.

    Returns:
        Dict[str, Any]: Dictionary with success status and entry_id.
    """
    logger.info(
        f"Inserting Ask Caspr chat entry for report_id: {report_id}, section_id: {section_id}, subsection_id: {subsection_id}, version: {version}, card_version: {card_version}"
    )

    try:
        new_entry = AskCasprChat(
            id=str(uuid7()),
            report_id=report_id,
            section_id=section_id,
            subsection_id=subsection_id,
            chat=None,
            version=version,
            card_version=card_version,
        )
        session.add(new_entry)
        await session.flush()

        logger.info(f"Successfully staged Ask Caspr chat entry with id: {new_entry.id}")
        return {"success": True, "entry_id": new_entry.id}

    except SQLAlchemyError as e:
        logger.error(f"Database error inserting Ask Caspr chat entry: {e!s}", exc_info=True)
        return {"success": False, "error": f"Database error: {e!s}"}
    except Exception as e:
        logger.error(f"Unexpected error inserting Ask Caspr chat entry: {e!s}", exc_info=True)
        return {"success": False, "error": f"Unexpected error: {e!s}"}
