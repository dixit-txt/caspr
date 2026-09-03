"""chats routes: sessions.

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
import asyncio
import json
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy import select
from uuid_utils import uuid7

from app.adapters.grep_agent_2 import get_or_create_grep_session
from app.adapters.openai_files import ensure_report_file_id
from app.adapters.s3 import (
    upload_initial_markdown_to_s3,
)
from app.auth.repository import (
    check_user_by_id,
    get_user_details,
)
from app.auth.schemas import ErrorResponse
from app.auth.token import get_current_active_user
from app.cards.repository import (
    insert_card,
    insert_table,
)
from app.chats.repository import (
    get_user_chat,
    insert_ask_caspr_chat_entry,
    insert_chats,
)
from app.chats.schemas import (
    ChatRequest,
    CreateSessionRequest,
)
from app.core.db import async_session_scope
from app.core.enums import FileUploadContext, FileUsageType, ReportStatus, SubscriptionTier
from app.core.logging import setup_logging
from app.core.redis import get_redis_instance
from app.internal.repository_grep import get_chat_uploaded_file_ids
from app.models import Card, ChatFile, Message, Report
from app.observability.cloudwatch_utils import insert_cloudwatch_logs
from app.reports.repository import (
    create_report,
    update_report,
    update_report_status_by_chat_or_report_id,
)
from app.research.agent.model import Casper
from app.wallet.repository import get_active_subscription
from app.wallet.service import WalletService

router = APIRouter()
logger = setup_logging(__file__)
redis_instance = get_redis_instance()
_CHAT_SESSION_TTL = timedelta(hours=3)


async def _try_reuse_chat_session(chat_id: str) -> str | None:
    """Return an existing session_id when Redis still has a live stream for this chat."""
    chat_key = f"chat_session:{chat_id}"
    existing_stream = await redis_instance.redis_client.get(chat_key)
    if not existing_stream or not isinstance(existing_stream, str):
        return None
    if not existing_stream.startswith("session:"):
        return None
    if not await redis_instance.redis_client.exists(existing_stream):
        return None

    session_id = existing_stream.removeprefix("session:")
    await redis_instance.redis_client.expire(chat_key, _CHAT_SESSION_TTL)
    await redis_instance.redis_client.expire(existing_stream, _CHAT_SESSION_TTL)
    return session_id


@router.post(
    "/create-session",
    status_code=200,
    response_class=JSONResponse,
    summary="Create a new session",
    description="Create a new session",
)
async def create_session(
    data: CreateSessionRequest, user_id: str = Depends(get_current_active_user)
):
    """Create a new session"""
    logger.info(
        f"Recieved request to create new session for user_id: {user_id} and chat_id: {data.chat_id}"
    )
    try:
        async with async_session_scope() as session:
            db_response = await check_user_by_id(user_id=user_id, session=session)
            if not db_response.get("success", False):
                logger.error(f"Failed to check user by id: {db_response.get('error')}")
                return JSONResponse(
                    status_code=500,
                    content={
                        "success": False,
                        "error": "I couldn't start a new chat session due to a system error. Please try again.",
                    },
                )
            if not db_response.get("exists", False):
                logger.error(f"User with ID {user_id} not found or token is invalid")
                return JSONResponse(
                    status_code=401,
                    content={
                        "success": False,
                        "error": "I can't seem to find an account with those details. Could you check them and try again?",
                    },
                )

            # Validate chat_id ownership if provided
            if data.chat_id:
                logger.info(
                    f"Validating ownership of chat_id: {data.chat_id} for user_id: {user_id}"
                )

                # Check if chat_id exists in database (regardless of owner)
                stmt = select(Message).where(Message.id == data.chat_id)
                result = await session.execute(stmt)
                existing_chat = result.scalar_one_or_none()

                if existing_chat:
                    # Chat exists - verify ownership
                    if existing_chat.user_id != user_id:
                        logger.error(
                            f"User {user_id} attempted to create session for chat_id {data.chat_id} owned by user {existing_chat.user_id}"
                        )
                        return JSONResponse(
                            status_code=403,
                            content={
                                "success": False,
                                "error": "You don't have permission to access this chat.",
                            },
                        )
                    logger.info(
                        f"Chat ownership validated successfully for chat_id: {data.chat_id} and user_id: {user_id}"
                    )
                else:
                    # Chat doesn't exist yet - this is fine, will be created later
                    logger.info(
                        f"Chat {data.chat_id} does not exist yet, will be created for user_id: {user_id}"
                    )

        chat_id = data.chat_id if data.chat_id else str(uuid7())
        if data.chat_id:
            reused_session_id = await _try_reuse_chat_session(chat_id)
            if reused_session_id:
                logger.info(
                    f"Reusing existing session for user_id: {user_id} "
                    f"chat_id: {chat_id} session_id: {reused_session_id}"
                )
                return {"session_id": reused_session_id, "chat_id": chat_id}

        logger.info(f"Creating new session for user_id: {user_id} and chat_id: {data.chat_id}")
        session_id = str(uuid7())
        stream_key = f"session:{session_id}"
        await redis_instance.redis_client.set(
            f"chat_session:{chat_id}", stream_key, ex=_CHAT_SESSION_TTL
        )
        event_data = {"event": "message", "data": json.dumps({"type": "session_created"})}
        await redis_instance.redis_client.xadd(stream_key, event_data)
        await redis_instance.redis_client.expire(stream_key, _CHAT_SESSION_TTL)
        logger.info(f"Session created successfully for user_id: {user_id} and chat_id: {chat_id}")
        return {"session_id": session_id, "chat_id": chat_id}

    except Exception as e:
        logger.error(f"Error in create_session: {e}")
        return JSONResponse(
            status_code=500,
            content={
                "success": False,
                "error": "I couldn't start a new chat session due to a system error. Please try again.",
            },
        )


@router.get(
    "/chat-stream/{session_id}",
    status_code=200,
    response_class=StreamingResponse,
    summary="Get chat events from redis stream",
    description="Get chat events from redis stream",
)
async def chat_stream(request: Request, session_id: str, chat_id: str, event_id: str = "0"):
    """Get chat events from redis stream"""
    logger.info(
        f"[SSE] Chat connection establishment request for session_id: {session_id} and chat_id: {chat_id}"
    )
    # Check if session id is valid
    stream_key = f"session:{session_id}"

    try:
        redis_session_id = await redis_instance.redis_client.get(f"chat_session:{chat_id}")
        if redis_session_id != stream_key:
            logger.error(f"[SSE] Invalid session_id: {session_id} for chat_id: {chat_id}")
            return JSONResponse(
                status_code=401,
                content={
                    "success": False,
                    "error": "That chat session has ended. Please start a new one.",
                },
            )

        # if not await redis_instance.redis_client.exists(stream_key):
        #     logger.error(f"[SSE] Session expired for chat_id: {chat_id} and session_id: {session_id}")
        #     return JSONResponse(
        #         status_code=422,
        #         content={"success": False, "error": "Session expired!"}
        #     )

        timeout = timedelta(hours=3)
        # timeout = timedelta(seconds=30)
        start_time = datetime.now(UTC)
        last_seen_event_id = event_id

        async def event_generator():
            nonlocal start_time, last_seen_event_id
            events_yielded_yet = False
            try:
                while not await request.is_disconnected():
                    try:
                        response = await redis_instance.redis_client.xread(
                            {stream_key: last_seen_event_id}, block=2000
                        )
                        if response:
                            _, entries = response[0]
                            for entry_id, fields in entries:
                                last_seen_event_id = entry_id
                                event = fields.get("event", "null")
                                data = json.loads(fields.get("data", "{}"))

                                yield f"event: {event}\ndata: {json.dumps({'event_id': entry_id, **data})}\n\n"
                                if not events_yielded_yet:
                                    logger.info(
                                        f"[SSE] Started streaming for chat_id: {chat_id} and session_id: {session_id}"
                                    )
                                    events_yielded_yet = True

                                await redis_instance.redis_client.expire(
                                    stream_key, timedelta(hours=3)
                                )
                                await redis_instance.redis_client.expire(
                                    f"chat_session:{chat_id}", timedelta(hours=3)
                                )

                            start_time = datetime.now(UTC)
                        else:
                            # # sending a keep-alive comment every 15 seconds to prevent connection timeouts
                            # if (datetime.now(timezone.utc) - start_time).total_seconds() > 15:
                            #     yield ": keepalive\n\n"
                            #     start_time = datetime.now(timezone.utc)

                            if datetime.now(UTC) - start_time > timeout:
                                error_data = {"response": "Session timeout!"}
                                yield f"event: timeout\ndata: {json.dumps(error_data)}\n\n"
                                logger.warning(
                                    f"[SSE] Timeout for session {session_id} and chat_id: {chat_id}"
                                )
                                return
                    except Exception as e:
                        logger.error(
                            f"[SSE] Error in event stream: {e} for chat_id: {chat_id} and session_id: {session_id}"
                        )
                        yield f"event: error\ndata: {json.dumps({'response': 'We are having some trouble with the connection. Please check your network.'})}\n\n"
                        return

            except Exception as e:
                logger.error(
                    f"[SSE] Unhandled error in event generator: {e} for chat_id: {chat_id} and session_id: {session_id}"
                )
                yield f"event: error\ndata: {json.dumps({'response': 'We are having some trouble with the connection. Please check your network.'})}\n\n"
                return

        # Set proper SSE headers
        headers = {
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # Disable buffering in Nginx
        }

        return StreamingResponse(event_generator(), media_type="text/event-stream", headers=headers)
    except Exception as e:
        logger.error(
            f"[SSE] Error establishing chat stream: {e} for chat_id: {chat_id} and session_id: {session_id}"
        )
        return JSONResponse(
            status_code=500,
            content={
                "success": False,
                "error": "We've encountered a connection error. Please refresh to try again.",
            },
        )


async def refresh_session(stream_key: str, chat_id: str):
    try:
        logger.info(f"[SSE] Refreshing session for chat_id: {chat_id} and stream_key: {stream_key}")
        chat_key = f"chat_session:{chat_id}"

        # delete the session from redis
        await redis_instance.redis_client.delete(stream_key)

        # create a new session with same session_id
        chat_key_ttl = await redis_instance.redis_client.ttl(chat_key)
        if chat_key_ttl <= 0:
            await redis_instance.redis_client.set(chat_key, stream_key, ex=timedelta(hours=3))
            chat_key_ttl = timedelta(hours=3)

        event_data = {"event": "message", "data": json.dumps({"type": "session_created"})}
        await redis_instance.redis_client.xadd(stream_key, event_data)
        await redis_instance.redis_client.expire(stream_key, chat_key_ttl)
        logger.info(f"[SSE] Session refreshed for chat_id: {chat_id} and stream_key: {stream_key}")
        return True

    except Exception as e:
        logger.error(
            f"[SSE] Error refreshing session: {e} for chat_id: {chat_id} and stream_key: {stream_key}"
        )
        return False


async def chat_producer(data: ChatRequest, user_id: str):
    """Chat endpoint with streaming response"""
    logger.info(f"Processing chat request from user_id: {user_id} and chat_id: {data.chat_id}")
    stream_key = f"session:{data.session_id}"

    # Seed the error-digest context for this background task so any
    # logger.error() fired deep inside (model fallbacks, retries, etc.) gets
    # attributed to this user / chat in the digest email.  user_email is
    # filled in below once we load the user record.
    from app.observability.error_alerter import _init_request_context, set_alert_request_context

    _init_request_context()
    set_alert_request_context(
        method="BACKGROUND",
        path=f"chat_producer:/api/v1/chat (chat_id={data.chat_id})",
        user_id=user_id,
    )

    await redis_instance.redis_client.set(
        f"chat:{data.chat_id}:processing_user_message",
        json.dumps({"type": "human", "content": data.message}),
        ex=timedelta(hours=24),
    )
    logger.info(f"Set user_message_processing key in redis for chat_id: {data.chat_id}")

    event_data = {"event": "message", "data": json.dumps({"type": "start_stream"})}
    await redis_instance.redis_client.xadd(stream_key, event_data)
    query_received_time = datetime.now(UTC)
    user_name, user_email = None, None
    user_previous_messages, chat_title, is_new_chat = [], None, False
    ref_id_to_fv_id: dict = {}  # {uploaded_file_id: file_version_id} — set when ensure runs for new chat

    # cloudwatch_data = {
    #     "event": "User input",
    #     "event_success": True,
    #     "timestamp": query_received_time.isoformat(),
    #     "chat_id": data.chat_id,
    #     "chat_title": chat_title
    # }
    # try:
    #     asyncio.create_task(insert_cloudwatch_logs(data=cloudwatch_data, user_id=user_id))
    # except Exception as e:
    #     logger.error(f"Error in inserting cloudwatch logs: {e} for user_id: {user_id} and chat_id: {data.chat_id}")
    report_id = None
    try:
        # 1. Validate user
        async with async_session_scope() as session:
            db_response = await check_user_by_id(user_id=user_id, session=session)
            if not db_response.get("success", False):
                logger.error(
                    f"Failed to check user by id: {db_response.get('error')} for user_id: {user_id} and chat_id: {data.chat_id}"
                )
                event_data = {
                    "event": "error",
                    "data": json.dumps(
                        {
                            # "response": "I couldn't process your request at this time. Please try again later."
                            "response": "I can't verify your session at the moment. Please try logging in again."
                        }
                    ),
                }
                await redis_instance.redis_client.xadd(stream_key, event_data)
                return

            if not db_response.get("exists", False):
                logger.error(
                    f"User with ID {user_id} not found or token is invalid for user_id: {user_id} and chat_id: {data.chat_id}"
                )
                event_data = {
                    "event": "error",
                    "data": json.dumps(
                        {
                            # "response": "Your session has expired. Please log in again to continue."
                            "response": "I'm having trouble finding your account. Please log in again to continue.."
                        }
                    ),
                }
                await redis_instance.redis_client.xadd(stream_key, event_data)
                return

            # 2. Get user details
            db_response = await get_user_details(user_id=user_id, session=session)
            if not db_response.get("success", False):
                logger.error(
                    f"Database error: Failed to get user details: {db_response.get('error')} for user_id: {user_id} and chat_id: {data.chat_id}"
                )
                event_data = {
                    "event": "error",
                    "data": json.dumps(
                        {
                            # "response": "I'm having trouble accessing your account information. Please try refreshing the page."
                            "response": "I couldn't retrieve your account information right now. Please refresh and try again."
                        }
                    ),
                }
                await redis_instance.redis_client.xadd(stream_key, event_data)
                return

            user_name = db_response.get("user", {}).get("user_name", None)
            user_email = db_response.get("user", {}).get("email", None)

            # Now that we have the user's email, refresh the alert context so
            # any subsequent logger.error in this background task carries it.
            try:
                set_alert_request_context(user_email=user_email)
            except Exception:
                pass

            # 3. Handle chat context
            db_response = await get_user_chat(
                user_id=user_id, chat_id=data.chat_id, session=session
            )
            if not db_response.get("success", False):
                logger.error(
                    f"Database error: Failed to get user chats: {db_response.get('error')} for user_id: {user_id} and chat_id: {data.chat_id}"
                )
                event_data = {
                    "event": "error",
                    "data": json.dumps(
                        {
                            "response": "I'm having difficulty loading your previous messages. Please try again in a moment."
                        }
                    ),
                }
                await redis_instance.redis_client.xadd(stream_key, event_data)
                return

            message_data = db_response.get("message", {})
            if not message_data:
                # Check if chat_id exists under a different user (ownership validation)
                stmt = select(Message).where(Message.id == data.chat_id)
                result = await session.execute(stmt)
                existing_chat = result.scalar_one_or_none()

                if existing_chat and existing_chat.user_id != user_id:
                    # Chat exists but belongs to another user - deny access
                    logger.error(
                        f"User {user_id} attempted to access chat_id {data.chat_id} owned by user {existing_chat.user_id}"
                    )
                    event_data = {
                        "event": "error",
                        "data": json.dumps(
                            {"response": "You don't have permission to access this chat."}
                        ),
                    }
                    await redis_instance.redis_client.xadd(stream_key, event_data)
                    return

                # new chat
                is_new_chat = True
                logger.info(
                    f"Starting new chat for user: {user_name} with user_id: {user_id} and chat_id: {data.chat_id}"
                )
                # cloudwatch_data = {
                #     "event": "Chat started",
                #     "event_success": True,
                #     "timestamp": query_received_time.isoformat(),
                #     "chat_id": data.chat_id,
                #     "chat_title": chat_title
                # }
                # try:
                #     asyncio.create_task(insert_cloudwatch_logs(data=cloudwatch_data, user_id=user_id))
                # except Exception as e:
                #     logger.error(f"Error in inserting cloudwatch logs: {e} for user_id: {user_id} and chat_id: {data.chat_id}")

            else:
                # existing chat
                chat_title = db_response.get("message", {}).get("chat_title", None)

                user_previous_messages = db_response.get("message", {}).get("chat_messages", [])
                if user_previous_messages[0]["type"] == "system":
                    user_previous_messages = user_previous_messages[1:]
                logger.info(
                    f"Fetched {len(user_previous_messages)} previous messages for user {user_name} with user_id: {user_id} and chat_id: {data.chat_id}"
                )

                cloudwatch_data = {
                    "event": "User input",
                    "event_success": True,
                    "timestamp": query_received_time.isoformat(),
                    "chat_id": data.chat_id,
                    "chat_title": chat_title,
                }
                try:
                    asyncio.create_task(
                        insert_cloudwatch_logs(data=cloudwatch_data, user_id=user_id)
                    )
                except Exception as e:
                    logger.error(
                        f"Error in inserting cloudwatch logs: {e} for user_id: {user_id} and chat_id: {data.chat_id}"
                    )

            # 4. Get user subscription plan
            # user_plan = await get_user_subscription_plan(user_id=user_id, session=session)
            # logger.info(f"User {user_id} subscription plan: {user_plan}")

            # ── 4b. Build grep_session for document-based chat ──
            # Rules:
            #   - New chat + reference_ids → build grep_session from reference_ids
            #   - Existing chat → build grep_session from chat_files table
            #   - New chat without reference_ids → no session (regular chat)
            #   - Existing chat + reference_ids sent → reference_ids IGNORED,
            #     session built from chat_files instead
            grep_session = None
            if is_new_chat and data.reference_ids:
                try:
                    grep_session = await get_or_create_grep_session(
                        data.reference_ids,
                        session,
                    )
                    if grep_session:
                        logger.info(
                            f"Built grep_session for new chat chat_id={data.chat_id}: "
                            f"docs={len(grep_session.labels)}, labels={grep_session.labels}"
                        )
                except Exception as e:
                    logger.warning(
                        f"Could not build grep_session for chat_id={data.chat_id}: {e} "
                        f"— proceeding without document context",
                        exc_info=True,
                    )
            elif not is_new_chat:
                # Existing chat — ignore reference_ids and build from chat_files
                if data.reference_ids:
                    logger.info(
                        f"Ignoring reference_ids for existing chat chat_id={data.chat_id} "
                        f"(reference_ids are only used on the first message of a new chat)"
                    )
                try:
                    uploaded_file_ids = await get_chat_uploaded_file_ids(data.chat_id, session)
                    if uploaded_file_ids:
                        grep_session = await get_or_create_grep_session(uploaded_file_ids, session)
                    if grep_session:
                        logger.info(
                            f"Built grep_session from chat_files for existing chat "
                            f"chat_id={data.chat_id}: docs={len(grep_session.labels)}"
                        )
                except Exception as e:
                    logger.error(
                        f"Error building grep_session from chat_files "
                        f"for chat_id={data.chat_id}: {e}",
                        exc_info=True,
                    )

        # 5. Initialize Casper and process query
        casper = await run_in_threadpool(
            lambda: Casper(
                {
                    "user_name": user_name,
                    "chat_id": data.chat_id,
                    "user_previous_messages": user_previous_messages,
                    "user_id": user_id,
                    "grep_session": grep_session,
                    # "user_plan": user_plan
                }
            )
        )
        await casper.async_init()

        is_report_generated = False
        raw_report_generation_start_time = None
        raw_report_generation_end_time = None
        raw_md_report = None
        all_messages_after_query = []
        message_stream_complete_time = datetime.now(UTC)
        accumulated_citations: list = []

        report_id = str(uuid7())
        report_version_id = None  # Will be set after create_report
        report_version = 1  # Initial version
        report_title = None
        report_layout = None
        report_length = None
        domain_name = None
        selected_report_type = "study"
        report_summary = None
        report_citations = None
        report_cards = []
        table_markdown_map = {}  # Initialize table markdown map to collect all table data
        section_sequence = 5
        # For DD/PR domains the subgraph emits `report_layout` BEFORE
        # `card_stream_start` (their DRL planner runs in a separate node ahead
        # of card streaming), whereas the standard `retrieve` flow emits
        # `card_stream_start` first and then `report_layout`. Buffer the DD/PR
        # `report_layout` SSE event here and flush it right after we forward
        # `card_stream_start` so the frontend sees a consistent ordering across
        # all domains: card_stream_start -> report_layout.
        pending_report_layout_event = None
        pending_card_stream_complete_event = None
        graph_state = await casper.get_processing_state(data.message)

        async for mode, output in graph_state:
            if mode == "messages":
                chunk, metadata = output
                if metadata.get("langgraph_node", "") != "report_or_respond":
                    continue

                ai_chunk = ""
                if hasattr(chunk, "content"):
                    if (
                        isinstance(chunk.content, list)
                        and chunk.content
                        and isinstance(chunk.content[0], dict)
                        and "text" in chunk.content[0]
                    ):
                        ai_chunk = chunk.content[0]["text"]
                    elif isinstance(chunk.content, str):
                        ai_chunk = chunk.content
                    else:
                        continue

                if ai_chunk:
                    event_data = {
                        "event": "delta",
                        "data": json.dumps({"chunk": ai_chunk, "type": "ai_chunk"}),
                    }
                    await redis_instance.redis_client.xadd(stream_key, event_data)
                else:
                    logger.error(
                        f"No AI chunk found for user_id: {user_id} and chat_id: {data.chat_id}"
                    )
                    continue
                    event_data = {
                        "event": "error",
                        "data": json.dumps(
                            {
                                "response": "An unexpected error occurred while processing your request. Please try that again."
                            }
                        ),
                    }
                    await redis_instance.redis_client.xadd(stream_key, event_data)
                    return

            elif mode == "custom":
                if "error" in output:
                    event_data = {
                        "event": "error",
                        "data": json.dumps(
                            {
                                "response": "An unexpected error occurred while processing your request. Please try that again."
                            }
                        ),
                    }
                    await redis_instance.redis_client.xadd(stream_key, event_data)
                    return

                custom_name = output.get("name", "")
                custom_status = output.get("status", "")

                if custom_name in ("retrieve_latest_info", "query_document") and custom_status in (
                    "start",
                    "heartbeat",
                    "end",
                ):
                    type_map = {
                        ("retrieve_latest_info", "start"): (
                            "learning_brain_latest_start",
                            "Learning Brain is thinking it through",
                        ),
                        ("retrieve_latest_info", "heartbeat"): (
                            "learning_brain_latest_heartbeat",
                            "Learning Brain is still gathering the latest",
                        ),
                        ("retrieve_latest_info", "end"): (
                            "learning_brain_latest_end",
                            "Learning Brain latest signals synced",
                        ),
                        ("query_document", "start"): (
                            "learning_brain_document_start",
                            "Learning Brain is digesting your document",
                        ),
                        ("query_document", "heartbeat"): (
                            "learning_brain_document_heartbeat",
                            "Learning Brain is still reading your document",
                        ),
                        ("query_document", "end"): (
                            "learning_brain_document_end",
                            "Learning Brain document context synced",
                        ),
                    }
                    sse_type, sse_msg = type_map[(custom_name, custom_status)]
                    # Document start always uses the fixed Learning Brain label; heartbeats
                    # prefer model-authored progress lines; other starts/end fall back to default.
                    if custom_name == "query_document" and custom_status == "start":
                        payload = {"type": sse_type, "message": sse_msg}
                    else:
                        payload = {"type": sse_type, "message": output.get("message") or sse_msg}
                    if custom_status == "heartbeat":
                        payload["elapsed"] = output.get("elapsed", "")
                    elif custom_status == "end":
                        tool_citations = output.get("citations", [])
                        payload["citations"] = tool_citations
                        payload["elapsed"] = output.get("elapsed", "")
                        accumulated_citations.extend(tool_citations)
                    await redis_instance.redis_client.xadd(
                        stream_key, {"event": "message", "data": json.dumps(payload)}
                    )

                elif custom_name == "pr_analyze_document" and custom_status in ("start", "end"):
                    # Primary Research subgraph: the document-analysis step is user-visible
                    # progress (we're reading the uploaded research data). Surface it as a
                    # named SSE event mirroring the `query_document` / `retrieve_latest_info`
                    # pattern so the frontend can show a meaningful status.
                    type_map = {
                        ("pr_analyze_document", "start"): (
                            "document_analysis_start",
                            "Analyzing your uploaded research data…",
                        ),
                        ("pr_analyze_document", "end"): (
                            "document_analysis_end",
                            "Document analysis complete",
                        ),
                    }
                    sse_type, sse_msg = type_map[(custom_name, custom_status)]
                    await redis_instance.redis_client.xadd(
                        stream_key,
                        {
                            "event": "message",
                            "data": json.dumps({"type": sse_type, "message": sse_msg}),
                        },
                    )

                elif custom_name in ("pr_generate_drl", "dd_generate_drl") and custom_status in (
                    "start",
                    "end",
                ):
                    # PR/DD DRL-generation start/end are internal subgraph progress beats —
                    # drop them so they don't leak to the frontend as bare {"type": "start"} /
                    # {"type": "end"} via the generic status handler below. The actual
                    # `report_layout` payload is forwarded in its own branch further down.
                    continue

                elif "status" in output:
                    status = output.get("status", "")

                    # Handle insufficient balance error from report_or_respond
                    # if status == "insufficient_balance":
                    #     available = output.get("available", 0)
                    #     required = output.get("required", 25000)
                    #     shortfall = output.get("shortfall", required - available)
                    #     logger.warning(f"Insufficient balance detected in model for user_id: {user_id}. Available: {available}, Required: {required}")
                    #     event_data = {
                    #         "event": "error",
                    #         "data": json.dumps({
                    #             "response": f"Insufficient token balance. You need {required:,} tokens but only have {available:,}. Please top up your wallet to continue.",
                    #             "error_code": "INSUFFICIENT_BALANCE",
                    #             "available": available,
                    #             "required": required,
                    #             "shortfall": shortfall
                    #         })
                    #     }
                    #     await redis_instance.redis_client.xadd(stream_key, event_data)
                    #     return

                    if status == "card_stream_start":
                        event_data = {
                            "event": "message",
                            "data": json.dumps(
                                {
                                    "type": status,
                                    "report_title": output.get("title", ""),
                                    "report_id": report_id,
                                }
                            ),
                        }

                        cloudwatch_data = {
                            "event": "Card generation started",
                            "event_success": True,
                            "timestamp": datetime.now(UTC).isoformat(),
                            "chat_id": data.chat_id,
                            "chat_title": chat_title,
                        }
                        try:
                            asyncio.create_task(
                                insert_cloudwatch_logs(data=cloudwatch_data, user_id=user_id)
                            )
                        except Exception as e:
                            logger.error(
                                f"Error in inserting cloudwatch logs: {e} for user_id: {user_id} and chat_id: {data.chat_id}"
                            )

                    else:
                        if status == "message_stream_complete":
                            message_stream_complete_time = datetime.now(UTC)

                        if status == "card_stream_complete":
                            cloudwatch_data = {
                                "event": "Card generation completed",
                                "event_success": True,
                                "timestamp": datetime.now(UTC).isoformat(),
                                "chat_id": data.chat_id,
                                "chat_title": chat_title,
                            }
                            try:
                                asyncio.create_task(
                                    insert_cloudwatch_logs(data=cloudwatch_data, user_id=user_id)
                                )
                            except Exception as e:
                                logger.error(
                                    f"Error in inserting cloudwatch logs: {e} for user_id: {user_id} and chat_id: {data.chat_id}"
                                )

                            # The frontend uses this event as the signal that report cards
                            # can be used for output generation. Hold it until after the
                            # collected cards have been persisted below.
                            pending_card_stream_complete_event = {
                                "event": "message",
                                "data": json.dumps(
                                    {
                                        "type": status,
                                        "report_id": report_id,
                                        "report_version_id": report_version_id,
                                        "version": report_version,
                                    }
                                ),
                            }
                            continue
                        if status == "md_content":
                            md_content = output.get("md_content", "")

                            # 1. Upload initial markdown to S3 for Ask Caspr
                            initial_markdown_s3_path = None
                            try:
                                initial_markdown_s3_path = await run_in_threadpool(
                                    upload_initial_markdown_to_s3,
                                    md_content=md_content,
                                    user_id=user_id,
                                    user_name=user_name or "unknown",
                                    chat_id=data.chat_id,
                                    chat_title=chat_title or "untitled",
                                    report_id=report_id,
                                )
                                logger.info(
                                    f"Uploaded initial markdown to S3: {initial_markdown_s3_path} for report_id: {report_id}"
                                )
                            except Exception as e:
                                logger.error(
                                    f"Failed to upload initial markdown to S3 for report_id: {report_id}: {e!s}"
                                )

                            # 2. Store initial_markdown S3 path in reports table, then create the
                            # OpenAI file_id for it in the background so the first Ask Caspr
                            # question or card refinement does not pay the conversion/upload cost.
                            if initial_markdown_s3_path and report_id:
                                try:
                                    async with async_session_scope() as file_session:
                                        await update_report(
                                            report_id=report_id,
                                            update_data={
                                                "initial_markdown": initial_markdown_s3_path
                                            },
                                            session=file_session,
                                        )
                                    logger.info(
                                        f"Stored initial_markdown: {initial_markdown_s3_path} for report_id: {report_id}"
                                    )
                                except Exception as e:
                                    logger.error(
                                        f"Failed to store initial_markdown for report_id: {report_id}: {e!s}"
                                    )

                                try:
                                    asyncio.create_task(
                                        ensure_report_file_id(
                                            report_id=report_id,
                                            file_id=None,
                                            file_s3_path=initial_markdown_s3_path,
                                            log_prefix="REPORT_GEN_FILE_ID",
                                        )
                                    )
                                    logger.info(
                                        f"Scheduled OpenAI file_id creation for report_id: {report_id}"
                                    )
                                except Exception as e:
                                    logger.error(
                                        f"Failed to schedule OpenAI file_id creation for report_id: {report_id}: {e!s}"
                                    )
                            continue
                        elif status == "card_generation_heartbeat":
                            event_data = {
                                "event": "message",
                                "data": json.dumps(
                                    {
                                        "type": status,
                                        "section": output.get("section", ""),
                                        "step": output.get("step", ""),
                                    }
                                ),
                            }
                        else:
                            event_data = {"event": "message", "data": json.dumps({"type": status})}

                    await redis_instance.redis_client.xadd(stream_key, event_data)

                    # DD/PR subgraphs emit `report_layout` ahead of `card_stream_start`;
                    # flush the buffered event now so the frontend receives it right
                    # after `card_stream_start`, matching the ordering used by every
                    # other domain.
                    if status == "card_stream_start" and pending_report_layout_event is not None:
                        await redis_instance.redis_client.xadd(
                            stream_key, pending_report_layout_event
                        )
                        pending_report_layout_event = None

                elif output.get("name", "") == "retrieve":
                    if "report_title" in output:
                        report_title = output.get("report_title", "")
                    elif "report_length" in output:
                        report_length = output.get("report_length", "")
                    elif "domain_name" in output:
                        domain_name = output.get("domain_name", "")
                        update_data = {"domain_name": domain_name}
                        normalized_report_type = False
                        if domain_name == "due_diligence" and selected_report_type == "brief":
                            selected_report_type = "study"
                            update_data["report_type"] = selected_report_type
                            normalized_report_type = True
                            logger.warning(
                                "Normalized invalid due_diligence report_type='brief' to 'study' "
                                f"for report_id: {report_id}, chat_id: {data.chat_id}"
                            )
                        if domain_name and report_id:
                            try:
                                async with async_session_scope() as domain_session:
                                    await update_report(
                                        report_id=report_id,
                                        update_data=update_data,
                                        session=domain_session,
                                    )
                                logger.info(
                                    f"Updated report {report_id} with domain_name='{domain_name}' "
                                    f"for chat_id: {data.chat_id}"
                                )
                            except Exception as e:
                                logger.error(
                                    f"Failed to update domain_name='{domain_name}' for "
                                    f"report_id: {report_id}, chat_id: {data.chat_id}: {e}"
                                )
                            if normalized_report_type:
                                event_data = {
                                    "event": "message",
                                    "data": json.dumps(
                                        {
                                            "type": "report_type",
                                            "report_type": selected_report_type,
                                            "domain_name": domain_name,
                                            "report_id": report_id,
                                        }
                                    ),
                                }
                                await redis_instance.redis_client.xadd(stream_key, event_data)
                    elif "report_type" in output:
                        report_type_val = output.get("report_type", "study")
                        if domain_name == "due_diligence" and report_type_val == "brief":
                            report_type_val = "study"
                            logger.warning(
                                "Normalized invalid due_diligence report_type='brief' to 'study' "
                                f"for report_id: {report_id}, chat_id: {data.chat_id}"
                            )
                        selected_report_type = report_type_val
                        if report_type_val and report_id:
                            try:
                                async with async_session_scope() as rt_session:
                                    await update_report(
                                        report_id=report_id,
                                        update_data={"report_type": report_type_val},
                                        session=rt_session,
                                    )
                                logger.info(
                                    f"Updated report {report_id} with report_type='{report_type_val}' "
                                    f"for chat_id: {data.chat_id}"
                                )
                            except Exception as e:
                                logger.error(
                                    f"Failed to update report_type='{report_type_val}' for "
                                    f"report_id: {report_id}, chat_id: {data.chat_id}: {e}"
                                )

                            # Stream the selected report type so the chat UI can
                            # reflect brief/study mode independently of domain.
                            event_data = {
                                "event": "message",
                                "data": json.dumps(
                                    {
                                        "type": "report_type",
                                        "report_type": report_type_val,
                                        "domain_name": domain_name,
                                        "report_id": report_id,
                                    }
                                ),
                            }
                            await redis_instance.redis_client.xadd(stream_key, event_data)
                    elif "report_layout" in output:
                        report_layout = output.get("report_layout", "")
                        event_data = {
                            "event": "message",
                            "data": json.dumps(
                                {
                                    "type": "report_layout",
                                    "data": report_layout,
                                    "report_id": report_id,
                                }
                            ),
                        }
                        await redis_instance.redis_client.xadd(stream_key, event_data)

                elif (
                    output.get("name", "") in ("dd_generate_drl", "pr_generate_drl")
                    and "report_layout" in output
                ):
                    # Specialized-domain subgraphs (Due Diligence / Primary Research) emit their
                    # own DRL payload under a different `name`, so the original
                    # `name == "retrieve"` branch above never sees them. Capture the cleaned
                    # report layout the same way so the frontend renders it (and so
                    # `report_layout` is also retained locally for the later `update_report`
                    # call). Unlike the standard `retrieve` flow, DD/PR emit this BEFORE
                    # `card_stream_start`; buffer the SSE event and let the
                    # `card_stream_start` handler flush it so the frontend always receives
                    # `card_stream_start` first, matching every other domain.
                    report_layout = output.get("report_layout", "")
                    pending_report_layout_event = {
                        "event": "message",
                        "data": json.dumps(
                            {"type": "report_layout", "data": report_layout, "report_id": report_id}
                        ),
                    }

                # elif output.get("name", "") == "report_or_respond":
                #     event_data = {
                #         "event": "message",
                #         "data": json.dumps({
                #             "type": output.get("status", "")
                #         })
                #     }
                #     if output.get("status", "") == "message_stream_complete":
                #         message_stream_complete_time = datetime.now(timezone.utc)

                #     await redis_instance.redis_client.xadd(stream_key, event_data)

                elif output.get("name", "") == "generate_report":
                    if "report_summary" in output:
                        report_summary = output.get("report_summary", "")
                        # event_data = {
                        #     "event": "message",
                        #     "data": json.dumps({
                        #         "type": "report_summary",
                        #         "info": report_summary,
                        #         "report_id": report_id
                        #     })
                        # }
                        # await redis_instance.redis_client.xadd(stream_key, event_data)
                        continue

                    if "report_citations" in output:
                        report_citations = output.get("report_citations", {})
                        event_data = {
                            "event": "message",
                            "data": json.dumps(
                                {
                                    "type": "report_citations",
                                    "data": report_citations,
                                    "report_id": report_id,
                                }
                            ),
                        }
                        await redis_instance.redis_client.xadd(stream_key, event_data)
                        continue

                    if "table_and_table_id_map" in output:
                        current_table_map = output.get("table_and_table_id_map") or {}
                        if current_table_map:
                            added = 0
                            for raw_tid, md in current_table_map.items():
                                tid = (raw_tid or "").strip()
                                if tid and md and tid not in table_markdown_map:
                                    table_markdown_map[tid] = md
                                    added += 1
                            logger.info(
                                f"Collected {added} new tables (total={len(table_markdown_map)})"
                            )
                        else:
                            logger.warning("generate_report emitted empty table_and_table_id_map")

                    card = output.get("card_db", {})
                    card_type = output.get("card_type", "")

                    # Skip empty cards - no need to insert in Redis or DB if card_type is empty and card is empty
                    if not card_type and (not card or card == {}):
                        logger.info(f"Skipping empty card for report_id: {report_id}")
                        continue

                    event_data = {
                        "event": "delta",
                        "data": json.dumps(
                            {
                                "type": "card",
                                "data": {"card": card, "card_type": card_type},
                                "report_id": report_id,
                            }
                        ),
                    }
                    await redis_instance.redis_client.xadd(stream_key, event_data)
                    card_data = {"type": card_type}

                    # Handle new card structure format
                    if isinstance(card, dict) and "section" in card:
                        # New format with section array
                        card_data["section"] = card.get("section", [])
                        card_data["sub_sections"] = card.get("sub_sections", [])
                        card_data["citations"] = card.get("citations", {})
                        card_data["summary"] = card.get("summary", "")
                        sections = card.get("section", [])
                        # print(f"sections : {sections}")
                        if sections and len(sections) > 0 and "id" in sections[0]:
                            card_data["id"] = sections[0]["id"]
                        else:
                            card_data["id"] = str(uuid7())

                        # Set sequence based on card type
                        if card_type == "title":
                            card_data["sequence"] = 1
                        elif card_type == "subtitle":
                            card_data["sequence"] = 2
                        elif card_type == "toc":
                            card_data["sequence"] = 3
                        elif card_type == "es":
                            card_data["sequence"] = 4
                        else:
                            # For section type cards
                            card_data["sequence"] = section_sequence
                            section_sequence += 1

                        card_data["created_at"] = query_received_time

                        # Set content as JSONB structure (same as section structure)
                        if sections and len(sections) > 0:
                            card_data["content"] = sections[0]  # Use the first section as content
                        else:
                            card_data["content"] = {
                                "name": card_type,
                                "content": "",
                                "tables": [
                                    {"visualization": "", "table_id": "", "table_title": ""}
                                ],
                                "id": str(uuid7()),
                            }

                    # Legacy format handling
                    else:
                        if card_type == "title":
                            card_data["title"] = card.get("title", "")
                            card_data["sequence"] = 1
                            card_data["sub_sections"] = []
                            card_data["content"] = {
                                "name": "title",
                                "content": card.get("title", ""),
                                "tables": [
                                    {"visualization": "", "table_id": "", "table_title": ""}
                                ],
                                "id": str(uuid7()),
                            }
                            card_data["citations"] = {}
                            card_data["created_at"] = query_received_time
                            card_data["summary"] = ""
                            card_data["id"] = str(uuid7())
                        elif card_type == "subtitle":
                            card_data["title"] = "Subtitle"
                            card_data["sequence"] = 2
                            card_data["sub_sections"] = []
                            card_data["content"] = {
                                "name": "subtitle",
                                "content": card.get("subtitle", ""),
                                "tables": [
                                    {"visualization": "", "table_id": "", "table_title": ""}
                                ],
                                "id": str(uuid7()),
                            }
                            card_data["citations"] = {}
                            card_data["created_at"] = datetime.now(UTC)
                            card_data["summary"] = ""
                            card_data["id"] = str(uuid7())
                        elif card_type == "toc":
                            card_data["title"] = "Table of Contents"
                            card_data["sequence"] = 3
                            card_data["sub_sections"] = []
                            card_data["content"] = {
                                "name": "table_of_contents",
                                "content": card.get("table_of_contents", ""),
                                "tables": [
                                    {"visualization": "", "table_id": "", "table_title": ""}
                                ],
                                "id": str(uuid7()),
                            }
                            card_data["citations"] = {}
                            card_data["created_at"] = datetime.now(UTC)
                            card_data["summary"] = ""
                            card_data["id"] = str(uuid7())
                        elif card_type == "es":
                            card_data["title"] = "Executive Summary"
                            card_data["sequence"] = 4
                            card_data["sub_sections"] = []
                            card_data["content"] = {
                                "name": "executive_summary",
                                "content": card.get("executive_summary", ""),
                                "tables": [
                                    {"visualization": "", "table_id": "", "table_title": ""}
                                ],
                                "id": str(uuid7()),
                            }
                            card_data["citations"] = {}
                            card_data["created_at"] = datetime.now(UTC)
                            card_data["summary"] = ""
                            card_data["id"] = str(uuid7())
                        else:
                            # card_type == "section" - legacy format
                            card_data["title"] = card.get("section", "")
                            card_data["sequence"] = section_sequence
                            card_data["sub_sections"] = card.get("sub_sections", [])
                            card_data["content"] = {
                                "name": card.get("section", ""),
                                "content": card.get("content", ""),
                                "tables": [
                                    {"visualization": "", "table_id": "", "table_title": ""}
                                ],
                                "id": str(uuid7()),
                            }
                            card_data["citations"] = card.get("citations", {})
                            card_data["created_at"] = datetime.now(UTC)
                            card_data["summary"] = card.get("summary", "")
                            card_data["id"] = str(uuid7())
                            section_sequence += 1
                    report_cards.append(card_data)

            elif mode == "updates":
                if "report_or_respond" in output:
                    AIMessage_object = output["report_or_respond"]["messages"][0]
                    response_metadata = getattr(AIMessage_object, "response_metadata", {})
                    # bedrock gives stopReason="tool_use", anthropic gives stop_reason="tool_use", openai gives finish_reason="tool_calls"

                    stop_reason = (
                        response_metadata.get("stopReason", "")
                        or response_metadata.get("stop_reason", "")
                        or response_metadata.get("finish_reason", "")
                    )
                    # ai_answer_string = AIMessage_object.content[0]['text']
                    if hasattr(AIMessage_object, "content"):
                        if (
                            isinstance(AIMessage_object.content, list)
                            and AIMessage_object.content
                            and isinstance(AIMessage_object.content[0], dict)
                            and "text" in AIMessage_object.content[0]
                        ):
                            ai_answer_string = AIMessage_object.content[0]["text"]
                        elif isinstance(AIMessage_object.content, str):
                            ai_answer_string = AIMessage_object.content
                        else:
                            ai_answer_string = str(AIMessage_object)

                    is_tool_call = stop_reason in ("tool_use", "tool_calls")
                    tool_calls = getattr(AIMessage_object, "tool_calls", [])
                    called_tool_names = (
                        [tc.get("name", "") for tc in tool_calls] if tool_calls else []
                    )
                    is_retrieve_call = "retrieve" in called_tool_names

                    if is_tool_call and not is_retrieve_call:
                        logger.info(
                            f"Non-retrieve tool call ({called_tool_names}) for chat_id={data.chat_id}, "
                            f"graph handles internally — continuing stream"
                        )

                    elif is_tool_call and is_retrieve_call:
                        async with async_session_scope() as session:
                            dup = await session.execute(
                                select(Report.id)
                                .where(
                                    Report.chat_id == data.chat_id,
                                    Report.status == ReportStatus.ANALYSIS_IN_PROGRESS.value,
                                )
                                .limit(1)
                            )
                            if dup.scalar_one_or_none():
                                logger.warning(
                                    f"Duplicate retrieve blocked for chat_id={data.chat_id} "
                                    f"(report generation already in progress)"
                                )
                                return

                        # Check and reserve tokens for report generation
                        async with async_session_scope() as session:
                            token_result = await WalletService.check_and_reserve(
                                user_id=user_id, session=session, report_id=report_id
                            )
                            if not token_result.get("success", False):
                                logger.warning(
                                    f"Token check/reserve failed for user_id: {user_id}: {token_result.get('error')}"
                                )
                                error_code = token_result.get("error_code", "")
                                if error_code == "INSUFFICIENT_BALANCE":
                                    event_data = {
                                        "event": "error",
                                        "data": json.dumps(
                                            {
                                                "response": f"Insufficient token balance. You need {token_result.get('required', 25000):,} tokens but only have {token_result.get('available', 0):,}. Please top up your wallet to continue.",
                                                "error_code": "INSUFFICIENT_BALANCE",
                                                "available": token_result.get("available", 0),
                                                "required": token_result.get("required", 25000),
                                            }
                                        ),
                                    }
                                else:
                                    event_data = {
                                        "event": "error",
                                        "data": json.dumps(
                                            {
                                                "response": "Unable to process your request at this time. Please try again."
                                            }
                                        ),
                                    }
                                await redis_instance.redis_client.xadd(stream_key, event_data)
                                return
                            logger.info(
                                f"Reserved {token_result.get('tokens_reserved', 25000)} tokens for user_id: {user_id}"
                            )

                        async with async_session_scope() as session:
                            s3_uri = {
                                "md": None,
                                "html": None,
                                "pdf": None,
                                "pptx": None,
                                "info_pdf": None,
                            }
                            db_response = await create_report(
                                report_id=report_id,
                                chat_id=data.chat_id,
                                created_at=query_received_time,
                                s3_uri=s3_uri,
                                session=session,
                            )
                            if not db_response.get("success", False):
                                logger.error(
                                    f"Failed to create report: {db_response.get('error')} for user_id: {user_id} and chat_id: {data.chat_id}"
                                )
                                # Release token reservation on failure
                                async with async_session_scope() as release_session:
                                    await WalletService.release_report_reservation(
                                        report_id, release_session, "report_creation_failed"
                                    )
                                event_data = {
                                    "event": "error",
                                    "data": json.dumps(
                                        {
                                            "response": "The report file couldn't be created at this time. Please try generating it again."
                                        }
                                    ),
                                }
                                # TODO: mail send here
                                await redis_instance.redis_client.xadd(stream_key, event_data)
                                return

                            # Get version info from create_report response
                            report_version_id = db_response.get("version_id")
                            report_version = db_response.get("version", 1)

                            # Set status to ANALYSIS_IN_PROGRESS when report is created (card generation starts)
                            await update_report_status_by_chat_or_report_id(
                                report_id=report_id,
                                status=ReportStatus.ANALYSIS_IN_PROGRESS.value,
                                session=session,
                            )
                            logger.info(
                                f"Set report status to ANALYSIS_IN_PROGRESS for report_id: {report_id}"
                            )

                        event_data = {
                            "event": "message",
                            "data": json.dumps(
                                {
                                    "type": "tool_call",
                                    "report_id": report_id,
                                    "report_version_id": report_version_id,
                                    "version": report_version,
                                }
                            ),
                        }
                        await redis_instance.redis_client.xadd(stream_key, event_data)
                        messages_to_append = [
                            {"type": "human", "content": data.message},
                            AIMessage_object.model_dump(),
                        ]

                        async with async_session_scope() as session:
                            if is_new_chat:
                                message_data = {
                                    "user_id": user_id,
                                    "chat_id": data.chat_id,
                                    "chat_messages": messages_to_append,
                                    "updated_at": message_stream_complete_time,
                                    "created_at": query_received_time,
                                    "chat_title": chat_title,
                                }
                            else:
                                message_data = {
                                    "chat_id": data.chat_id,
                                    "chat_messages": user_previous_messages + messages_to_append,
                                    "updated_at": message_stream_complete_time,
                                    "user_id": user_id,
                                }

                            logger.info(
                                f"Inserting tool call message to database for user_id: {user_id} and chat_id: {data.chat_id}"
                            )
                            db_response = await insert_chats(
                                message_data=message_data, session=session
                            )
                            if not db_response.get("success", False):
                                logger.error(
                                    f"Database error: Failed to insert message: {db_response.get('error')} for user_id: {user_id} and chat_id: {data.chat_id}"
                                )
                                event_data = {
                                    "event": "error",
                                    "data": json.dumps(
                                        {
                                            "response": "Your last message couldn't be saved. Please try sending it again."
                                        }
                                    ),
                                }
                                await redis_instance.redis_client.xadd(stream_key, event_data)
                                return

                            # Link uploaded files to chat on first message (tool-call path, new chat only)
                            if is_new_chat and data.reference_ids:
                                try:
                                    for ref_id in data.reference_ids:
                                        chat_file = ChatFile(
                                            id=str(uuid7()),
                                            chat_id=data.chat_id,
                                            uploaded_file_id=ref_id,
                                            usage_type=FileUsageType.REFERENCE,
                                            upload_context=FileUploadContext.IN_CHAT,
                                        )
                                        session.add(chat_file)
                                    await session.commit()
                                    logger.info(
                                        f"Linked {len(data.reference_ids)} uploaded file(s) to chat_id: {data.chat_id} (tool-call path)"
                                    )
                                except Exception as e:
                                    logger.error(
                                        f"Failed to link uploaded files to chat {data.chat_id} (tool-call path): {e}",
                                        exc_info=True,
                                    )

                            # Log Casper response
                            cloudwatch_data = {
                                "event": "Caspr response",
                                "event_success": True,
                                "timestamp": message_stream_complete_time.isoformat(),
                                "chat_id": data.chat_id,
                                "chat_title": chat_title,
                            }
                            try:
                                asyncio.create_task(
                                    insert_cloudwatch_logs(data=cloudwatch_data, user_id=user_id)
                                )
                            except Exception as e:
                                logger.error(
                                    f"Error in inserting cloudwatch logs: {e} for user_id: {user_id} and chat_id: {data.chat_id}"
                                )

                            # delete the user_message_processing key from redis when user message is inserted in db
                            await redis_instance.redis_client.delete(
                                f"chat:{data.chat_id}:processing_user_message"
                            )
                            logger.info(
                                f"Deleted user_message_processing key from redis for chat_id: {data.chat_id}"
                            )

                            # so that frontend does not get the same chat messages from both redis and db, so we delete the redis stream in refresh_session()
                            session_refreshed = await refresh_session(
                                stream_key=stream_key, chat_id=data.chat_id
                            )
                            logger.info(f"session_refreshed: {session_refreshed}")
                            if not session_refreshed:
                                event_data = {
                                    "event": "error",
                                    "data": json.dumps(
                                        {
                                            "response": "Facing some issues while processing your request. Please try again in a few moments."
                                        }
                                    ),
                                }
                                await redis_instance.redis_client.xadd(stream_key, event_data)
                                return

                    elif stop_reason == "end_turn":
                        pass

                    if is_new_chat and ai_answer_string:
                        chat_title = await casper.generate_chat_title(
                            user_query=data.message, ai_response=ai_answer_string
                        )
                        event_data = {
                            "event": "message",
                            "data": json.dumps(
                                {
                                    "type": "new_chat_generation",
                                    "chat_title": chat_title,
                                    "chat_id": data.chat_id,
                                }
                            ),
                        }
                        await redis_instance.redis_client.xadd(stream_key, event_data)

                        cloudwatch_data = {
                            "event": "Chat started",
                            "event_success": True,
                            "timestamp": query_received_time.isoformat(),
                            "chat_id": data.chat_id,
                            "chat_title": chat_title,
                        }
                        try:
                            asyncio.create_task(
                                insert_cloudwatch_logs(data=cloudwatch_data, user_id=user_id)
                            )
                        except Exception as e:
                            logger.error(
                                f"Error in inserting cloudwatch logs: {e} for user_id: {user_id} and chat_id: {data.chat_id}"
                            )

                        cloudwatch_data = {
                            "event": "User input",
                            "event_success": True,
                            "timestamp": query_received_time.isoformat(),
                            "chat_id": data.chat_id,
                            "chat_title": chat_title,
                        }
                        try:
                            asyncio.create_task(
                                insert_cloudwatch_logs(data=cloudwatch_data, user_id=user_id)
                            )
                        except Exception as e:
                            logger.error(
                                f"Error in inserting cloudwatch logs: {e} for user_id: {user_id} and chat_id: {data.chat_id}"
                            )

                elif (
                    "generate_report" in output
                    or "pr_subgraph" in output
                    or "dd_subgraph" in output
                ):
                    # Specialized-domain subgraphs (Primary Research, Due Diligence)
                    # return their final AIMessage under their own node name
                    # (`pr_subgraph` / `dd_subgraph`). Treat those the same as
                    # `generate_report` for downstream report-output handling.
                    report_node_key = (
                        "generate_report"
                        if "generate_report" in output
                        else "pr_subgraph"
                        if "pr_subgraph" in output
                        else "dd_subgraph"
                    )
                    AIMessage_object = output[report_node_key]["messages"][0]
                    logger.info(
                        f"Consolidated raw report generated successfully "
                        f"(node='{report_node_key}') for user_id: {user_id} and chat_id: {data.chat_id}"
                    )
                    raw_md_report = AIMessage_object.content
                    is_report_generated = True

        if isinstance(output, dict) and "messages" in output:
            all_messages_after_query = [msg.model_dump() for msg in output["messages"][1:]]
        else:
            event_data = {
                "event": "error",
                "data": json.dumps(
                    {
                        "response": "Some issue occurred while processing your request. Please start a new chat."
                    }
                ),
            }
            await redis_instance.redis_client.xadd(stream_key, event_data)
            return

        # Handle regular chat
        if not all_messages_after_query:
            event_data = {
                "event": "error",
                "data": json.dumps(
                    {
                        "response": "I couldn't process the response. Please try again or start a new chat."
                    }
                ),
            }
            await redis_instance.redis_client.xadd(stream_key, event_data)
            return
        else:
            event_data = {
                "event": "message",
                "data": json.dumps({"type": "saving_checkpoint_data"}),
            }
            await redis_instance.redis_client.xadd(stream_key, event_data)

            async with async_session_scope() as session:
                # Build citation mapping: {ai_message_id: [url, ...]} for this turn.
                # Find the last AI message in the list and use its LangChain id as key.
                turn_citations: dict | None = None
                if accumulated_citations:
                    last_ai_msg = next(
                        (m for m in reversed(all_messages_after_query) if m.get("type") == "ai"),
                        None,
                    )
                    if last_ai_msg and last_ai_msg.get("id"):
                        turn_citations = {
                            last_ai_msg["id"]: list(dict.fromkeys(accumulated_citations))
                        }

                message_data = {
                    "user_id": user_id,
                    "chat_id": data.chat_id,
                    "chat_messages": all_messages_after_query,
                    "updated_at": message_stream_complete_time,
                    "created_at": query_received_time if is_new_chat else None,
                    "chat_title": chat_title,
                    "message_citations": turn_citations,
                }
                db_response = await insert_chats(message_data=message_data, session=session)
                if not db_response.get("success", False):
                    logger.error(
                        f"Database error: Failed to insert message: {db_response.get('error')} for user_id: {user_id} and chat_id: {data.chat_id}"
                    )
                    event_data = {
                        "event": "error",
                        "data": json.dumps(
                            {
                                "response": "Your last message couldn't be saved. Please try sending it again."
                            }
                        ),
                    }
                    await redis_instance.redis_client.xadd(stream_key, event_data)
                    return

                # Link uploaded files to chat on first message (new chat only)
                if is_new_chat and data.reference_ids:
                    try:
                        for ref_id in data.reference_ids:
                            chat_file = ChatFile(
                                id=str(uuid7()),
                                chat_id=data.chat_id,
                                uploaded_file_id=ref_id,
                                file_version_id=ref_id_to_fv_id.get(ref_id),
                                usage_type=FileUsageType.REFERENCE,
                                upload_context=FileUploadContext.IN_CHAT,
                            )
                            session.add(chat_file)
                        await session.commit()
                        logger.info(
                            f"Linked {len(data.reference_ids)} uploaded file(s) to chat_id: {data.chat_id}"
                        )
                    except Exception as e:
                        logger.error(
                            f"Failed to link uploaded files to chat {data.chat_id}: {e}",
                            exc_info=True,
                        )

                if not is_report_generated:
                    # Log Casper response
                    cloudwatch_data = {
                        "event": "Caspr response",
                        "event_success": True,
                        "timestamp": message_stream_complete_time.isoformat(),
                        "chat_id": data.chat_id,
                        "chat_title": chat_title,
                    }
                    try:
                        asyncio.create_task(
                            insert_cloudwatch_logs(data=cloudwatch_data, user_id=user_id)
                        )
                    except Exception as e:
                        logger.error(
                            f"Error in inserting cloudwatch logs: {e} for user_id: {user_id} and chat_id: {data.chat_id}"
                        )

                # delete the user_message_processing key from redis when user message is inserted in db
                await redis_instance.redis_client.delete(
                    f"chat:{data.chat_id}:processing_user_message"
                )
                logger.info(
                    f"Deleted user_message_processing key from redis for chat_id: {data.chat_id}"
                )

                session_refreshed = await refresh_session(
                    stream_key=stream_key, chat_id=data.chat_id
                )
                logger.info(
                    f"{len(all_messages_after_query)} messages inserted in database for user {user_name} with user_id {user_id} and chat_id: {data.chat_id}"
                )

        if is_report_generated:
            report_update_data = {
                "title": report_title,
                # "s3_uri": {},
                "layout": report_layout,
                "length": report_length,
                "summary": report_summary,
                "citations": report_citations,
            }
            async with async_session_scope() as session:
                db_response = await update_report(
                    report_id=report_id, update_data=report_update_data, session=session
                )
                if not db_response.get("success", False):
                    logger.error(
                        f"Database error: Failed to update report: {db_response.get('error')} for user_id: {user_id} and chat_id: {data.chat_id}"
                    )
                    event_data = {
                        "event": "error",
                        "data": json.dumps(
                            {
                                "response": "I couldn't save the report information to your account. Please try generating it again."
                            }
                        ),
                    }
                    await redis_instance.redis_client.xadd(stream_key, event_data)
                    return

            async with async_session_scope() as session:
                # Create a local table_markdown_map for this request
                # table_markdown_map = {}

                # We'll collect table markdown data directly from the model response
                # No need to read from Redis stream
                logger.info(f"Processing {len(report_cards)} report cards for table insertion")
                logger.info(
                    f"Total collected table markdown data: {len(table_markdown_map)} tables"
                )

                if not table_markdown_map:
                    logger.warning(
                        "⚠️  No table markdown data collected - check if model is generating table_and_table_id_map streams"
                    )

                for report_card in report_cards:
                    # Extract any tables from the section format if present
                    all_tables = []
                    if "section" in report_card and isinstance(report_card["section"], list):
                        for section in report_card["section"]:
                            if "tables" in section:
                                all_tables.extend(section.get("tables", []))

                    # Extract any tables from subsections
                    if "sub_sections" in report_card:
                        for subsection in report_card["sub_sections"]:
                            if "tables" in subsection:
                                all_tables.extend(subsection.get("tables", []))

                    # First insert the card
                    db_response = await insert_card(
                        report_id=report_id, report_card=report_card, session=session
                    )
                    if not db_response.get("success", False):
                        logger.error(
                            f"Database error: Failed to insert card: {db_response.get('error')} for user_id: {user_id} and chat_id: {data.chat_id}"
                        )
                        event_data = {
                            "event": "error",
                            "data": json.dumps(
                                {
                                    "response": "Something went wrong. Please try again. If the issue persists, delete this chat and start a new one."
                                }
                            ),
                        }
                        await redis_instance.redis_client.xadd(stream_key, event_data)
                        return

                    # Then insert table records using insert_table function
                    card_id = db_response.get("card_id")
                    if card_id and all_tables:
                        for table in all_tables:
                            table_id = table.get("table_id", "")
                            if table_id and table_id.strip():  # Check if table_id is not empty
                                # Get the actual table markdown from the collected data
                                table_markdown = table_markdown_map.get(table_id)

                                if table_markdown:
                                    # Prepare table data for insert_table function
                                    table_data = {
                                        "table_id": table_id,
                                        "report_id": report_id,
                                        "table_title": table.get("table_title", ""),
                                        "table_markdown": table_markdown,
                                        "visualization": table.get("visualization", ""),
                                    }
                                    # Insert table using insert_table function
                                    table_response = await insert_table(
                                        session=session, card_id=card_id, table_data=table_data
                                    )
                                    if not table_response.get("success", False):
                                        logger.error(
                                            f"Failed to insert table {table_id} for card {card_id}: {table_response.get('error')}"
                                        )
                                    else:
                                        logger.info(
                                            f"Successfully inserted table {table_id} for card {card_id}"
                                        )
                                else:
                                    logger.warning(
                                        f"No table markdown found for table_id: {table_id}"
                                    )

                                    # Insert table with NULL markdown instead of placeholder text
                                    table_data = {
                                        "table_id": table_id,
                                        "report_id": report_id,
                                        "table_title": table.get("table_title", ""),
                                        "table_markdown": None,  # Use None instead of placeholder
                                        "visualization": table.get("visualization", ""),
                                    }
                                    table_response = await insert_table(
                                        session=session, card_id=card_id, table_data=table_data
                                    )
                                    if not table_response.get("success", False):
                                        logger.error(
                                            f"Failed to insert table {table_id} for card {card_id}: {table_response.get('error')}"
                                        )
                                    else:
                                        logger.info(
                                            f"Successfully inserted table {table_id} for card {card_id} (with NULL markdown)"
                                        )

                # Insert Ask Caspr chat entries (chat=NULL) for all section cards and their subsections
                try:
                    for report_card in report_cards:
                        if report_card.get("type") == "section":
                            section_id = report_card.get("id")
                            if section_id:
                                # Section-level entry
                                await insert_ask_caspr_chat_entry(
                                    report_id=report_id,
                                    section_id=section_id,
                                    session=session,
                                    subsection_id=None,
                                    version=1,
                                    card_version=1,
                                )
                                # Subsection-level entries
                                for subsection in report_card.get("sub_sections", []) or []:
                                    sub_id = (
                                        subsection.get("id")
                                        if isinstance(subsection, dict)
                                        else None
                                    )
                                    if sub_id:
                                        await insert_ask_caspr_chat_entry(
                                            report_id=report_id,
                                            section_id=section_id,
                                            session=session,
                                            subsection_id=sub_id,
                                            version=1,
                                            card_version=1,
                                        )
                    await session.commit()
                    logger.info(
                        f"Inserted Ask Caspr chat entries for all sections/subsections of report_id: {report_id}"
                    )
                except Exception as e:
                    await session.rollback()
                    logger.error(
                        f"Failed to insert Ask Caspr chat entries for report_id: {report_id}: {e!s}"
                    )

                # Set status to ANALYSIS_COMPLETED when all cards have been inserted
                await update_report_status_by_chat_or_report_id(
                    report_id=report_id,
                    status=ReportStatus.ANALYSIS_COMPLETED.value,
                    session=session,
                )
                logger.info(f"Set report status to ANALYSIS_COMPLETED for report_id: {report_id}")

                # Confirm token debit after report status is set to ANALYSIS_COMPLETED
                # This ensures billing only happens after cards are successfully persisted
                try:
                    debit_result = await WalletService.confirm_report_debit(report_id, session)
                    if debit_result.get("success"):
                        logger.info(
                            f"Token debit confirmed for report_id: {report_id}, tokens: {debit_result.get('tokens_debited')}"
                        )
                    else:
                        logger.error(
                            f"Failed to confirm token debit for report_id: {report_id}: {debit_result.get('error')}"
                        )
                except Exception as e:
                    logger.error(f"Error confirming token debit for report_id: {report_id}: {e!s}")

                # Update all section cards to mark them as used in the initial ES version
                try:
                    # Get the ES card version
                    es_card_result = await session.execute(
                        select(Card).where(
                            Card.report_id == report_id,
                            Card.type == "es",
                            Card.is_active == True,
                            Card.is_deleted == False,
                        )
                    )
                    es_card = es_card_result.scalar_one_or_none()

                    if es_card:
                        es_version = es_card.version or 1

                        # Update all section cards to mark them as used in this ES version
                        section_cards_result = await session.execute(
                            select(Card).where(
                                Card.report_id == report_id,
                                Card.type == "section",
                                Card.is_active == True,
                                Card.is_deleted == False,
                            )
                        )
                        section_cards = section_cards_result.scalars().all()

                        for card in section_cards:
                            card.last_es_version_used = es_version

                        await session.commit()
                        logger.info(
                            f"Updated {len(section_cards)} section cards with last_es_version_used={es_version} for report_id: {report_id}"
                        )
                    else:
                        logger.warning(
                            f"No ES card found for report_id: {report_id}, skipping ES version tracking"
                        )
                except Exception as e:
                    logger.error(f"Error updating section cards with ES version: {e!s}")
                    # Don't fail the entire request if this fails, just log it

                if pending_card_stream_complete_event is not None:
                    await redis_instance.redis_client.xadd(
                        stream_key, pending_card_stream_complete_event
                    )
                    pending_card_stream_complete_event = None

            session_refreshed = await refresh_session(stream_key=stream_key, chat_id=data.chat_id)
            if not session_refreshed:
                event_data = {
                    "event": "error",
                    "data": json.dumps(
                        {
                            "response": "Facing some issues while processing your request. Please try again in a few moments."
                        }
                    ),
                }
                await redis_instance.redis_client.xadd(stream_key, event_data)
                return

            event_data = {
                "event": "message",
                "data": json.dumps({"type": "saving_checkpoint_data_done"}),
            }
            await redis_instance.redis_client.xadd(stream_key, event_data)

    except Exception as e:
        # Handle session errors
        logger.error(
            f"Encountered error in chat_producer: {e} for user_id: {user_id} and chat_id: {data.chat_id}"
        )

        # Surface the failure to the error-digest email pipeline.  This block
        # runs inside a FastAPI BackgroundTask, so the HTTP 500-middleware in
        # main.py never sees it; without this explicit queueing the operator
        # would only get a CloudWatch line and no alert email.
        try:
            from app.observability.error_alerter import queue_background_error

            await queue_background_error(
                path=f"chat_producer:/api/v1/chat (chat_id={data.chat_id})",
                error_message=f"chat_producer failed: {e}",
                user_id=user_id,
                user_email=user_email,
                exc=e,
            )
        except Exception as alert_error:
            logger.warning(f"Failed to queue background error alert: {alert_error}")

        # Release token reservation if report generation failed
        try:
            if report_id:
                async with async_session_scope() as release_session:
                    release_result = await WalletService.release_report_reservation(
                        report_id, release_session, "generation_failed"
                    )
                    if release_result.get("success"):
                        logger.info(f"Released token reservation for failed report_id: {report_id}")
                    else:
                        logger.error(
                            f"Failed to release token reservation: {release_result.get('error')}"
                        )

        except Exception as release_error:
            logger.error(f"Error releasing token reservation: {release_error!s}")

        event_data = {
            "event": "error",
            "data": json.dumps(
                {
                    "response": "An unexpected error occurred while processing your request. Please try that again."
                }
            ),
        }
        await redis_instance.redis_client.xadd(stream_key, event_data)
        return

    finally:
        logger.info(f"Stream completed for chat_id: {data.chat_id} and user_id: {user_id}")
        # delete the user_message_processing key from redis when stream is completed
        await redis_instance.redis_client.delete(f"chat:{data.chat_id}:processing_user_message")
        logger.info(f"Deleted user_message_processing key from redis for chat_id: {data.chat_id}")
        event_data = {"event": "message", "data": json.dumps({"type": "end_stream"})}
        await redis_instance.redis_client.xadd(stream_key, event_data)


@router.post(
    "/chat",
    status_code=200,
    response_class=JSONResponse,
    summary="Request for processing chat",
    description="Request for processing chat",
    responses={
        200: {"content": {"application/json": {}}},
        401: {"model": ErrorResponse},
        422: {"model": ErrorResponse},
        500: {"model": ErrorResponse},
    },
)
async def chat(
    data: ChatRequest,
    background_tasks: BackgroundTasks,
    user_id: str = Depends(get_current_active_user),
):
    """
    POST /chat – Process a chat message.

    **reference_ids handling rules (upload integration):**
      - ``reference_ids`` is a list of ``uploaded_files.id`` sent by the
        frontend on the **first message** of a new chat only.
      - If ``reference_ids`` is sent and the user is on the free tier →
        request is denied (403).
      - For existing chats (``is_new_chat=false``), ``reference_ids`` is
        **ignored** even if sent by the frontend.  The backend determines
        which files belong to the chat from the ``chat_files`` table.
      - For new chats (``is_new_chat=true``) with ``reference_ids`` and a
        paid plan → ``chat_files`` records are created in ``chat_producer``.
    """
    logger.info(f"Received chat request for user_id: {user_id} and chat_id: {data.chat_id}")
    try:
        # Check if session id is valid
        redis_session_id = await redis_instance.redis_client.get(f"chat_session:{data.chat_id}")
        if redis_session_id != f"session:{data.session_id}":
            return JSONResponse(
                status_code=401, content={"success": False, "error": "Session expired!"}
            )

        stream_key = f"session:{data.session_id}"
        if not await redis_instance.redis_client.exists(stream_key):
            logger.warning(
                f"[SSE] Session expired for chat_id: {data.chat_id} and session_id: {data.session_id}"
            )
            return JSONResponse(
                status_code=401, content={"success": False, "error": "Session expired!"}
            )

        await redis_instance.redis_client.expire(f"chat_session:{data.chat_id}", timedelta(hours=3))
        await redis_instance.redis_client.expire(stream_key, timedelta(hours=3))

        processing_payload = json.dumps({"type": "human", "content": data.message})
        was_set = await redis_instance.redis_client.set(
            f"chat:{data.chat_id}:processing_user_message",
            processing_payload,
            ex=int(timedelta(hours=24).total_seconds()),
            nx=True,
        )
        if not was_set:
            logger.info(
                f"Rejecting duplicate chat turn for chat_id={data.chat_id} "
                f"(turn already in progress)"
            )
            return JSONResponse(
                status_code=409,
                content={
                    "success": False,
                    "error": "A turn is already in progress for this chat.",
                    "error_code": "TURN_IN_PROGRESS",
                },
            )

        # ── Validate reference_ids: free-tier users cannot use file-based chat ──
        # NOTE: We validate free-tier here at the API level.  Whether reference_ids
        # are actually used (new chat) or ignored (existing chat) is determined
        # in chat_producer based on is_new_chat.
        if data.reference_ids:
            async with async_session_scope() as session:
                user_plan_result = await get_active_subscription(user_id=user_id, session=session)
                user_tier = (
                    user_plan_result.get("subscription", {}).get(
                        "current_tier", SubscriptionTier.FREE.value
                    )
                    if user_plan_result.get("subscription")
                    else SubscriptionTier.FREE.value
                )
            if user_tier == SubscriptionTier.FREE.value:
                logger.warning(
                    f"Free-tier user {user_id} attempted to attach files to chat {data.chat_id}"
                )
                return JSONResponse(
                    status_code=403,
                    content={
                        "success": False,
                        "error": "File-based chat is available on paid plans. Please upgrade to use this feature.",
                    },
                )

        # Add the chat_producer function directly to background tasks
        logger.info(f"Creating background task for user_id: {user_id} and chat_id: {data.chat_id}")
        background_tasks.add_task(chat_producer, data, user_id)
        logger.info(f"Background task created for user_id: {user_id} and chat_id: {data.chat_id}")
        return JSONResponse(
            status_code=200, content={"success": True, "message": "Chat stream started"}
        )

    except RequestValidationError as e:
        # Validation errors
        logger.error(f"Validation error in chat endpoint: {e}")
        return JSONResponse(
            status_code=422,
            content={
                "success": False,
                "error": "Your request couldn't be understood. Please check your input and try again.",
            },
        )
    except HTTPException:
        # Re-raise HTTP exceptions
        raise
    except Exception as e:
        # All other errors
        logger.error(f"Error in chat-stream endpoint: {e}")
        return JSONResponse(
            status_code=500,
            content={
                "success": False,
                "error": "We're experiencing technical difficulties. Please try again later.",
            },
        )
