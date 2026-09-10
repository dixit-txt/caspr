"""chats routes: management.

Split out of ``app/chats/router.py`` to keep each router file under
the ~400-line ceiling in R-STRUCT-3. Handlers are unchanged.
"""

"""HTTP routes for the chats bounded context.

Handlers moved verbatim from ``src/resources/routers/api.py`` during the
R-STRUCT-1 migration, which split that 10,257-line module across seven
contexts. Handler bodies are unchanged; only the import block and the
``APIRouter`` they attach to are new.
"""

"""api.py: Authentication API for the Casper backend"""
import json
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy import select

from app.adapters.s3 import (
    replace_visualization_uris_in_reports,
)
from app.auth.repository import (
    check_user_by_id,
)
from app.auth.schemas import ErrorResponse
from app.auth.token import get_current_active_user
from app.chats.repository import (
    get_draft_chats_grouped,
    get_user_chat,
    get_user_chats,
    mark_chat_deleted,
    rename_chat,
)
from app.chats.schemas import (
    DeleteChatRequest,
    DeleteChatResponse,
    PreviousChatMessagesResponse,
    RenameChatRequest,
    RenameChatResponse,
    UserChatsResponse,
)
from app.core.db import async_session_scope
from app.core.enums import ReportStatus
from app.core.logging import setup_logging
from app.core.redis import get_redis_instance
from app.dashboard.schemas import (
    OngoingChatItem,
    OngoingChatsResponse,
)
from app.models import Report
from app.reports.constants import (
    STANDARD_CATEGORY_SLUG,
)
from app.reports.repository import (
    get_chat_reports_and_cards,
)

router = APIRouter()
logger = setup_logging(__file__)
redis_instance = get_redis_instance()
_CHAT_SESSION_TTL = timedelta(hours=3)


@router.get(
    "/chat/{chat_id}",
    response_model=PreviousChatMessagesResponse,
    status_code=200,
    responses={
        404: {"model": ErrorResponse},
        401: {"model": ErrorResponse},
        422: {"model": ErrorResponse},
        500: {"model": ErrorResponse},
    },
)
async def get_chat_messages(chat_id: str, user_id: str = Depends(get_current_active_user)):
    """Get chat messages endpoint"""
    logger.info(f"Get chat messages request received for user_id: {user_id} and chat_id: {chat_id}")
    try:
        async with async_session_scope() as session:
            # Check if user exists and credentials are valid
            db_response = await check_user_by_id(user_id=user_id, session=session)
            if not db_response.get("success", False):
                logger.error(
                    f"Failed to check user by id: {db_response.get('error')} for user_id: {user_id}"
                )
                return JSONResponse(
                    status_code=500,
                    content={
                        "success": False,
                        "error": "I'm having an issue verifying your account for this chat. Please refresh.",
                    },
                )
            if not db_response.get("exists", False):
                logger.error(f"User with ID {user_id} not found or token is invalid")
                return JSONResponse(
                    status_code=401,
                    content={
                        "success": False,
                        "error": "I can't find an account associated with this chat. Please log in again to view your history.",
                    },
                )

            db_response = await get_user_chat(user_id=user_id, chat_id=chat_id, session=session)
            if not db_response.get("success", False):
                logger.error(
                    f"Database error: Failed to get user chats: {db_response.get('error')} for user_id: {user_id}, chat_id: {chat_id}"
                )
                return JSONResponse(
                    status_code=500,
                    content={
                        "success": False,
                        "error": "I'm unable to load your conversation due to a system error. Please try again.",
                    },
                )

            processing_user_message = await redis_instance.redis_client.get(
                f"chat:{chat_id}:processing_user_message"
            )
            if processing_user_message:
                processing_user_message = [json.loads(processing_user_message)]
            else:
                processing_user_message = []

            chat_messages = db_response.get("message", {}).get("chat_messages", [])
            message_citations = db_response.get("message", {}).get("message_citations", {}) or {}
            if not chat_messages:
                logger.warning(f"No chat messages found for chat_id: {chat_id}, user_id: {user_id}")
                return PreviousChatMessagesResponse(
                    success=True,
                    messages=processing_user_message,
                    reports=[],
                    turn_in_progress=bool(processing_user_message),
                )
                # return JSONResponse(
                #     status_code=404,
                #     content={"success": False, "error": "No chat messages found"}
                # )

            formatted_chat_messages = []
            for i, msg in enumerate(chat_messages):
                msg_type = msg["type"]
                msg_content = msg["content"]

                if msg_type == "human":
                    msg_object = {"type": msg_type, "content": msg_content}
                    formatted_chat_messages.append(msg_object)
                elif msg_type == "ai":
                    msg_metadata = msg.get("response_metadata", {})
                    # msg_stop = (
                    #     msg_metadata.get('stopReason', '')
                    #     or msg_metadata.get('stop_reason', '')
                    #     or msg_metadata.get('finish_reason', '')
                    # )
                    # if msg_stop in ('tool_use', 'tool_calls'):
                    #     called_names = [tc.get('name', '') for tc in msg.get('tool_calls', [])]
                    #     if 'retrieve' not in called_names:
                    #         continue
                    if (
                        i > 0
                        and chat_messages[i - 1].get("type") == "tool"
                        and chat_messages[i - 1].get("name") == "retrieve"
                    ):
                        continue
                    if isinstance(msg_content, str):
                        msg_object = {"type": msg_type, "content": msg_content}
                    elif (
                        isinstance(msg_content, list)
                        and isinstance(msg_content[0], dict)
                        and "text" in msg_content[0]
                    ):
                        msg_object = {"type": msg_type, "content": msg_content[0]["text"]}
                    else:
                        continue
                    msg_id = msg.get("id")
                    if msg_id and msg_id in message_citations:
                        msg_object["citations"] = message_citations[msg_id]
                    formatted_chat_messages.append(msg_object)

            db_response = await get_chat_reports_and_cards(chat_id=chat_id, session=session)
            if not db_response.get("success", False):
                logger.error(
                    f"Database error: Failed to get chat report: {db_response.get('error')} for user_id: {user_id}, chat_id: {chat_id}"
                )
                return JSONResponse(
                    status_code=500,
                    content={
                        "success": False,
                        "error": "something unexpected happened. Please try again later.",
                    },
                )

            reports_with_cards = db_response.get("reports", [])

            await replace_visualization_uris_in_reports(
                reports_with_cards, redis_instance.redis_client
            )

            return PreviousChatMessagesResponse(
                success=True,
                messages=formatted_chat_messages + processing_user_message,
                reports=reports_with_cards,
                turn_in_progress=bool(processing_user_message),
            )

    except RequestValidationError as e:
        # Validation errors (like missing fields) return 422
        logger.error(
            f"Validation error in get chat messages endpoint: {e} for user_id: {user_id}, chat_id: {chat_id}"
        )
        return JSONResponse(
            status_code=422,
            content={
                "success": False,
                "error": "Your request couldn't be understood. Please check your input and try again.",
            },
        )
    except HTTPException:
        # Re-raise HTTP exceptions to let the global handler handle them
        raise
    except Exception as e:
        # All other errors return 500
        logger.error(
            f"Error in get chat messages endpoint: {e} for user_id: {user_id}, chat_id: {chat_id}"
        )
        return JSONResponse(
            status_code=500,
            content={
                "success": False,
                "error": "Something unexpected happened. Please try again later.",
            },
        )


@router.get(
    "/chat-list",
    response_model=UserChatsResponse,
    status_code=200,
    responses={
        404: {"model": ErrorResponse},
        401: {"model": ErrorResponse},
        422: {"model": ErrorResponse},
        500: {"model": ErrorResponse},
    },
)
async def chat_list(
    user_id: str = Depends(get_current_active_user),
    status: str | None = Query(
        None,
        description="Comma-separated status values to filter by (e.g., 'draft,analysis-completed')",
    ),
    search: str | None = Query(
        None, description="Search term to filter chat titles (case-insensitive substring match)"
    ),
):
    """
    Get user chats endpoint with optional status filtering and search

    Query Parameters:
    - status: Optional comma-separated list of status values to filter by
    - search: Optional search term to filter chat titles (case-insensitive)

    Examples:
    - /chat-list (returns all chats)
    - /chat-list?status=draft (returns only draft chats)
    - /chat-list?search=AI (returns chats with "AI" in title)
    - /chat-list?search=wealth&status=draft,analysis-completed (returns chats matching both filters)

    Valid status values:
    - draft
    - awaiting-confirmation
    - analysis-in-progress
    - analysis-completed
    - generating-output
    - output-generated
    - updates-available
    - redo-analysis
    - error-generation-report
    """
    logger.info(
        f"Get user chats request received for user_id: {user_id}, status filter: {status}, search: {search}"
    )
    try:
        async with async_session_scope() as session:
            # Check if user exists and credentials are valid
            db_response = await check_user_by_id(user_id=user_id, session=session)
            if not db_response.get("success", False):
                logger.error(f"Failed to check user by id: {db_response.get('error')}")
                return JSONResponse(
                    status_code=500,
                    content={
                        "success": False,
                        "error": "I couldn't verify your account to load the chat list. Please refresh and try again.",
                    },
                )
            if not db_response.get("exists", False):
                logger.error(f"User with ID {user_id} not found or token is invalid")
                return JSONResponse(
                    status_code=401,
                    content={
                        "success": False,
                        "error": "I can't seem to find your account. Please log in to see your chat list.",
                    },
                )

            # Get all chats for the user
            db_response = await get_user_chats(user_id=user_id, session=session)
            if not db_response.get("success", False):
                logger.error(
                    f"Database error: Failed to get user chats: {db_response.get('error')}"
                )
                return JSONResponse(
                    status_code=500,
                    content={
                        "success": False,
                        "error": "I'm having trouble retrieving your chats right now. Please refresh and try again.",
                    },
                )

            chats = db_response.get("chats", [])
            if not chats:
                logger.info(f"No chats found for user_id: {user_id}")
                return UserChatsResponse(success=True, chats=None)

            # Parse status filter if provided
            status_filter = None
            if status:
                # Split comma-separated values and strip whitespace
                status_filter = [s.strip() for s in status.split(",") if s.strip()]
                logger.info(f"Filtering chats by status: {status_filter}")

            # Parse search query if provided
            search_query = search.strip().lower() if search else None
            if search_query:
                logger.info(f"Filtering chats by search query: {search_query}")

            chats_with_reports = []
            # Sort chats by created_at in descending order (newest first)
            sorted_chats = sorted(chats, key=lambda x: x["created_at"], reverse=True)

            async with async_session_scope() as session:
                for chat in sorted_chats:
                    session_id = await redis_instance.redis_client.get(f"chat_session:{chat['id']}")
                    if session_id and isinstance(session_id, str):
                        session_id = session_id.split(":")[-1]

                    # Get report status for this chat
                    chat_status = ReportStatus.DRAFT.value  # Default for chats without reports
                    chat_image = None  # TODO: Add chat image

                    # Query for the latest report associated with this chat (order by created_at desc)
                    # This I have to do because before one chat can have multiple reports else I would have use scaler or none
                    report_stmt = (
                        select(Report)
                        .where(Report.chat_id == chat["id"])
                        .order_by(Report.created_at.desc())
                        .limit(1)
                    )
                    report_result = await session.execute(report_stmt)
                    report = report_result.scalar_one_or_none()

                    if report:
                        chat_status = report.status
                        chat_image = report.poster_image_url

                    # Apply status filter if provided
                    if status_filter and chat_status not in status_filter:
                        # Skip this chat if it doesn't match the filter
                        continue

                    # Apply search filter (case-insensitive substring match)
                    if search_query and search_query not in chat["chat_title"].lower():
                        # Skip this chat if title doesn't match search query
                        continue

                    chats_with_reports.append(
                        {
                            "chat_id": chat["id"],
                            "chat_title": chat["chat_title"],
                            "chat_image": chat_image,
                            "chat_status": chat_status,
                            "updated_at": chat["updated_at"],
                            "created_at": chat["created_at"],
                            "session_id": session_id,
                        }
                    )

            return UserChatsResponse(success=True, chats=chats_with_reports)

    except RequestValidationError as e:
        # Validation errors (like missing fields) return 422
        logger.error(f"Validation error in get user chats endpoint: {e}")
        return JSONResponse(
            status_code=422,
            content={
                "success": False,
                "error": "Your request couldn't be understood. Please check your input and try again.",
            },
        )
    except HTTPException:
        # Re-raise HTTP exceptions to let the global handler handle them
        raise
    except Exception as e:
        # All other errors return 500
        logger.error(f"Error in get user chats endpoint: {e}")
        return JSONResponse(
            status_code=500,
            content={
                "success": False,
                "error": "An unexpected error occurred while loading your chat list. Our team has been alerted.",
            },
        )


@router.post(
    "/delete-chat",
    response_model=DeleteChatResponse,
    status_code=200,
    responses={
        404: {"model": ErrorResponse},
        401: {"model": ErrorResponse},
        422: {"model": ErrorResponse},
        500: {"model": ErrorResponse},
    },
)
async def delete_chat(data: DeleteChatRequest, user_id: str = Depends(get_current_active_user)):
    """Mark a chat as deleted"""
    logger.info(f"Delete chat request received for user_id: {user_id}, chat_id: {data.chat_id}")

    try:
        # Validate input data
        if not data.chat_id:
            logger.error("Missing required field: chat_id")
            return JSONResponse(
                status_code=422,
                content={
                    "success": False,
                    "error": "I'll need a chat ID to know which conversation to delete.",
                },
            )

        async with async_session_scope() as session:
            # Check if user exists and credentials are valid
            db_response = await check_user_by_id(user_id=user_id, session=session)
            if not db_response.get("success", False):
                logger.error(f"Failed to check user by id: {db_response.get('error')}")
                return JSONResponse(
                    status_code=500,
                    content={
                        "success": False,
                        "error": "I've run into a technical problem on my end. Please try that again in a moment.",
                    },
                )
            if not db_response.get("exists", False):
                logger.error(f"User with ID {user_id} not found or token is invalid")
                return JSONResponse(
                    status_code=401,
                    content={
                        "success": False,
                        "error": "Your session seems to have expired. Please log in again to make changes.",
                    },
                )

            # Mark the chat as deleted
            db_response = await mark_chat_deleted(
                chat_id=data.chat_id, user_id=user_id, session=session
            )
            if not db_response.get("success", False):
                logger.error(f"Failed to mark chat as deleted: {db_response.get('error')}")
                return JSONResponse(
                    status_code=500,
                    content={
                        "success": False,
                        "error": "I've run into a technical problem on my end. Please try that again in a moment.",
                    },
                )

            return DeleteChatResponse(
                success=True, message=db_response.get("message", "Chat deleted successfully")
            )

    except RequestValidationError as e:
        # Validation errors (like missing fields) return 422
        logger.error(f"Validation error in delete chat endpoint: {e}")
        return JSONResponse(
            status_code=422,
            content={
                "success": False,
                "error": "Your request couldn't be understood. Please check your input and try again.",
            },
        )
    except HTTPException:
        # Re-raise HTTP exceptions to let the global handler handle them
        raise
    except Exception as e:
        # All other errors return 500
        logger.error(f"Error in delete chat endpoint: {e}")
        return JSONResponse(
            status_code=500,
            content={
                "success": False,
                "error": "An unexpected error occurred while deleting the chat. Please try that again.",
            },
        )


@router.post(
    "/rename-chat-title",
    response_model=RenameChatResponse,
    status_code=200,
    responses={
        404: {"model": ErrorResponse},
        401: {"model": ErrorResponse},
        422: {"model": ErrorResponse},
        500: {"model": ErrorResponse},
    },
)
async def rename_chat_endpoint(
    data: RenameChatRequest, user_id: str = Depends(get_current_active_user)
):
    """Rename a chat by updating its title"""
    logger.info(f"Rename chat request received for user_id: {user_id}, chat_id: {data.chat_id}")

    try:
        # Validate input data
        if not data.chat_id:
            logger.error("Missing required field: chat_id")
            return JSONResponse(
                status_code=422,
                content={"success": False, "error": "Missing required field: chat_id"},
            )

        if not data.new_title:
            logger.error("Missing required field: new_title")
            return JSONResponse(
                status_code=422,
                content={"success": False, "error": "Missing required field: new_title"},
            )

        # Trim whitespace from title
        new_title = data.new_title.strip()
        if not new_title:
            logger.error("New title cannot be empty")
            return JSONResponse(
                status_code=422, content={"success": False, "error": "New title cannot be empty"}
            )

        async with async_session_scope() as session:
            # Check if user exists and credentials are valid
            db_response = await check_user_by_id(user_id=user_id, session=session)
            if not db_response.get("success", False):
                logger.error(f"Failed to check user by id: {db_response.get('error')}")
                return JSONResponse(
                    status_code=500,
                    content={
                        "success": False,
                        "error": "An unexpected error stopped me from renaming that chat. Let's give it another try.",
                    },
                )
            if not db_response.get("exists", False):
                logger.error(f"User with ID {user_id} not found or token is invalid")
                return JSONResponse(
                    status_code=401,
                    content={
                        "success": False,
                        "error": "It looks like your session has expired. Please log in again to rename the chat.",
                    },
                )

            # Rename the chat
            db_response = await rename_chat(
                chat_id=data.chat_id, user_id=user_id, new_title=new_title, session=session
            )
            if not db_response.get("success", False):
                logger.error(f"Failed to rename chat: {db_response.get('error')}")
                return JSONResponse(
                    status_code=500,
                    content={
                        "success": False,
                        "error": "An unexpected error stopped me from renaming that chat. Let's give it another try.",
                    },
                )

            return RenameChatResponse(
                success=True, message=db_response.get("message", "Chat renamed successfully")
            )

    except RequestValidationError as e:
        # Validation errors (like missing fields) return 422
        logger.error(f"Validation error in rename chat endpoint: {e}")
        return JSONResponse(
            status_code=422,
            content={
                "success": False,
                "error": "Your request couldn't be understood. Please check your input and try again.",
            },
        )
    except HTTPException:
        # Re-raise HTTP exceptions to let the global handler handle them
        raise
    except Exception as e:
        # All other errors return 500
        logger.error(f"Error in rename chat endpoint: {e}")
        return JSONResponse(
            status_code=500,
            content={
                "success": False,
                "error": "An unexpected error stopped me from renaming that chat. Let's give it another try.",
            },
        )


@router.get(
    "/ongoing-chats",
    response_model=OngoingChatsResponse,
    status_code=200,
    responses={401: {"model": ErrorResponse}, 500: {"model": ErrorResponse}},
)
async def ongoing_chats(user_id: str = Depends(get_current_active_user)):
    """Return the user's ongoing (draft) chats, grouped by category.

    A chat is included here when its latest report is in ``draft`` status,
    or when no report has been generated yet. Because draft reports do not
    yet have a ``domain_name`` in the DB, every entry currently lands under
    the ``standard`` key. Additional category keys will start appearing on
    this response once drafts begin carrying a domain.

    Within each group, chats are ordered by ``updated_at`` descending
    (most-recent first).

    Authentication Required: Yes (JWT token)
    """
    logger.info(f"Ongoing-chats request received for user_id={user_id}")
    try:
        async with async_session_scope() as session:
            result = await get_draft_chats_grouped(user_id=user_id, session=session)

            if not result.get("success"):
                logger.error(
                    f"ongoing-chats DB failure for user_id={user_id}: {result.get('error')}"
                )
                return JSONResponse(
                    status_code=500,
                    content={
                        "success": False,
                        "error": "Failed to load ongoing chats. Please try again.",
                    },
                )

            groups = result.get("groups", {})
            standard_items = [
                OngoingChatItem(**item) for item in groups.get(STANDARD_CATEGORY_SLUG, [])
            ]

            # Surface any other category groups that drift in (forward
            # compatibility — today the DB always groups drafts under
            # 'standard', but we don't want to silently drop a real bucket).
            extras = {
                slug: items
                for slug, items in groups.items()
                if slug != STANDARD_CATEGORY_SLUG and items
            }
            if extras:
                logger.warning(
                    f"ongoing-chats: drafts found under non-standard groups "
                    f"for user_id={user_id}: {sorted(extras.keys())}"
                )

            return OngoingChatsResponse(success=True, standard=standard_items)

    except Exception as e:
        logger.error(f"Error in ongoing_chats: {e}", exc_info=True)
        return JSONResponse(
            status_code=500,
            content={"success": False, "error": "Internal server error"},
        )
