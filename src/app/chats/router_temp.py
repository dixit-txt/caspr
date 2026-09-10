"""chats routes: temp.

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

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from uuid_utils import uuid7

from app.auth.schemas import ErrorResponse
from app.chats.schemas import (
    CreateTempSessionRequest,
    TempChatMessagesResponse,
    TempChatRequest,
)
from app.core.logging import setup_logging
from app.core.redis import get_redis_instance
from app.research.agent.model import Casper

router = APIRouter()
logger = setup_logging(__file__)
redis_instance = get_redis_instance()
_CHAT_SESSION_TTL = timedelta(hours=3)


@router.post(
    "/create-temp-session",
    status_code=200,
    response_class=JSONResponse,
    summary="Create a new temporary session",
    description="Create a new temporary session",
)
async def create_temp_session(data: CreateTempSessionRequest):
    """Create a new temporary session"""
    logger.info(f"Recieved request to create new session for temporary chat_id: {data.chat_id}")
    try:
        logger.info(f"Creating new session for temporary chat_id: {data.chat_id}")
        session_id = str(uuid7())
        stream_key = f"session:{session_id}"
        chat_id = data.chat_id if data.chat_id else str(uuid7())
        await redis_instance.redis_client.set(
            f"temp_chat_session:{chat_id}", f"session:{session_id}", ex=timedelta(hours=3)
        )
        event_data = {"event": "message", "data": json.dumps({"type": "session_created"})}
        await redis_instance.redis_client.xadd(stream_key, event_data)
        await redis_instance.redis_client.expire(stream_key, timedelta(hours=3))
        logger.info(f"Session created successfully for temporary chat_id: {chat_id}")
        return {"session_id": session_id, "chat_id": chat_id}

    except Exception as e:
        logger.error(f"Error in create_temp_session: {e}")
        return JSONResponse(
            status_code=500,
            content={
                "success": False,
                "error": "A system error prevented a new temporary chat from starting. Please try again.",
            },
        )


@router.get(
    "/temp-chat-stream/{session_id}",
    status_code=200,
    response_class=StreamingResponse,
    summary="Get temporary chat events from redis stream",
    description="Get temporary chat events from redis stream",
)
async def temp_chat_stream(request: Request, session_id: str, chat_id: str, event_id: str = "0"):
    """Get temporary chat events from redis stream"""
    logger.info(
        f"[SSE] Temporary chat connection establishment request for session_id: {session_id} and temporary chat_id: {chat_id}"
    )
    # Check if session id is valid
    stream_key = f"session:{session_id}"

    try:
        redis_session_id = await redis_instance.redis_client.get(f"temp_chat_session:{chat_id}")
        if redis_session_id != stream_key:
            logger.error(f"[SSE] Invalid session_id: {session_id} for temporary chat_id: {chat_id}")
            return JSONResponse(
                status_code=401,
                content={"success": False, "error": "This temporary chat has timed out."},
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

                                # await asyncio.sleep(0.1)
                                yield f"event: {event}\ndata: {json.dumps({'event_id': entry_id, **data})}\n\n"
                                if not events_yielded_yet:
                                    logger.info(
                                        f"[SSE] Started streaming for temporary chat_id: {chat_id} and session_id: {session_id}"
                                    )
                                    events_yielded_yet = True

                                await redis_instance.redis_client.expire(
                                    stream_key, timedelta(hours=3)
                                )
                                await redis_instance.redis_client.expire(
                                    f"temp_chat_session:{chat_id}", timedelta(hours=3)
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
                                    f"[SSE] Timeout for session {session_id} and temporary chat_id: {chat_id}"
                                )
                                return
                    except Exception as e:
                        logger.error(
                            f"[SSE] Error in event stream: {e} for temporary chat_id: {chat_id} and session_id: {session_id}"
                        )
                        yield f"event: error\ndata: {json.dumps({'response': 'Connection error'})}\n\n"
                        return

            except Exception as e:
                logger.error(
                    f"[SSE] Unhandled error in event generator: {e} for temporary chat_id: {chat_id} and session_id: {session_id}"
                )
                yield f"event: error\ndata: {json.dumps({'response': 'We have encountered a connection error. Please refresh to try again.'})}\n\n"
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
            f"[SSE] Error establishing temporary chat stream: {e} for temporary chat_id: {chat_id} and session_id: {session_id}"
        )
        return JSONResponse(
            status_code=500,
            content={
                "success": False,
                "error": "We're having some trouble with the connection. Please check your network.",
            },
        )


async def temp_chat_producer(data: TempChatRequest):
    """Temporary chat endpoint with streaming response"""
    logger.info(f"Processing temporary chat request for temporary chat_id: {data.chat_id}")
    stream_key = f"session:{data.session_id}"

    # Seed the error-digest context (temp chats are anonymous so we only have
    # a chat_id to attribute errors to in the digest email).
    from app.observability.error_alerter import _init_request_context, set_alert_request_context

    _init_request_context()
    set_alert_request_context(
        method="BACKGROUND",
        path=f"temp_chat_producer:/api/v1/temp_chat (chat_id={data.chat_id})",
    )

    event_data = {"event": "message", "data": json.dumps({"type": "start_stream"})}
    await redis_instance.redis_client.xadd(stream_key, event_data)
    query_received_time = datetime.now(UTC)

    if not data.message:
        logger.error(f"Message is empty for temporary chat_id: {data.chat_id}")
        event_data = {
            "event": "error",
            "data": json.dumps(
                {
                    "response": "I couldn't process your request at this time. Please try again later."
                }
            ),
        }
        await redis_instance.redis_client.xadd(stream_key, event_data)
        return

    chat_id = data.chat_id
    message = data.message
    is_new_chat = False
    user_previous_messages = []

    try:
        logger.info(f"Checking if temporary chat with ID: {chat_id} exists")
        redis_response = await redis_instance.check_chat_exists(chat_id=chat_id)
        if not redis_response.get("success"):
            logger.error(
                f"Failed to get chat from Redis for chat_id: {chat_id}: {redis_response.get('error')}"
            )
            event_data = {
                "event": "error",
                "data": json.dumps({"response": "Your chat has expired. Please start a new chat."}),
            }
            await redis_instance.redis_client.xadd(stream_key, event_data)
            return

        if not redis_response.get("exists"):
            is_new_chat = True

        if not is_new_chat:
            logger.info(f"Retrieving chat data for temporary chat_id: {chat_id}")
            redis_response = await redis_instance.get_chat(chat_id=chat_id)
            if not redis_response.get("success"):
                logger.error(
                    f"Failed to get chat from Redis for temporary chat_id: {chat_id}: {redis_response.get('error')}"
                )
                event_data = {
                    "event": "error",
                    "data": json.dumps(
                        {"response": "Your chat has expired. Please start a new chat."}
                    ),
                }
                await redis_instance.redis_client.xadd(stream_key, event_data)
                return

            user_previous_messages = redis_response.get("chat_data", {}).get("chat_messages", [])
            logger.info(
                f"Retrieved {len(user_previous_messages)} previous messages for temporary chat ID: {chat_id}"
            )

        # Initialize Casper model (temp/guest users default to free plan)
        casper = await run_in_threadpool(
            lambda: Casper(
                {
                    "user_name": "User",
                    "chat_id": "TEMP-" + chat_id,
                    "user_previous_messages": user_previous_messages,
                    # "user_plan": "free"
                }
            )
        )
        await casper.async_init()
        logger.info(f"Casper model initialized for temporary chat ID: {chat_id}")

        all_messages_after_query = []
        message_stream_complete_time = datetime.now(UTC)

        graph_state = await casper.get_processing_state(message)
        async for mode, output in graph_state:
            if mode == "messages":
                chunk, metadata = output
                # if chunk.content and 'text' in chunk.content[0]:
                #     ai_chunk = chunk.content[0]['text']
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
                    event_data = {"event": "delta", "data": json.dumps({"chunk": ai_chunk})}
                    await redis_instance.redis_client.xadd(stream_key, event_data)

            elif mode == "custom":
                event_status = output.get("status", "")
                custom_name = output.get("name", "")

                if event_status == "error":
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

                if custom_name in ("retrieve_latest_info", "query_document") and event_status in (
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
                    sse_type, sse_msg = type_map[(custom_name, event_status)]
                    # Document start always uses the fixed Learning Brain label; heartbeats
                    # prefer model-authored progress lines; other starts/end fall back to default.
                    if custom_name == "query_document" and event_status == "start":
                        payload = {"type": sse_type, "message": sse_msg}
                    else:
                        payload = {"type": sse_type, "message": output.get("message") or sse_msg}
                    if event_status == "heartbeat":
                        payload["elapsed"] = output.get("elapsed", "")
                    elif event_status == "end":
                        payload["citations"] = output.get("citations", [])
                        payload["elapsed"] = output.get("elapsed", "")
                    await redis_instance.redis_client.xadd(
                        stream_key, {"event": "message", "data": json.dumps(payload)}
                    )
                else:
                    event_data = {"event": "message", "data": json.dumps({"type": event_status})}
                    await redis_instance.redis_client.xadd(stream_key, event_data)

                if event_status == "message_stream_complete":
                    message_stream_complete_time = datetime.now(UTC)

            elif mode == "updates":
                if "report_or_respond" in output:
                    AIMessage_object = output["report_or_respond"]["messages"][0]

                    # Check response metadata safely
                    response_metadata = getattr(AIMessage_object, "response_metadata", {})
                    # bedrock gives stopReason="tool_use", anthropic gives stop_reason="tool_use", openai gives finish_reason="tool_calls"
                    stop_reason = (
                        response_metadata.get("stopReason", "")
                        or response_metadata.get("stop_reason", "")
                        or response_metadata.get("finish_reason", "")
                    )

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
                            f"Non-retrieve tool call ({called_tool_names}) for temp chat_id={chat_id}, "
                            f"graph handles internally — continuing stream"
                        )

                    elif is_tool_call and is_retrieve_call:
                        event_data = {"event": "message", "data": json.dumps({"type": "tool_call"})}
                        await redis_instance.redis_client.xadd(stream_key, event_data)
                        logger.info(f"Temp user tried to generate a report with chat_id: {chat_id}")

                        event_data = {
                            "event": "message",
                            "data": json.dumps({"type": "message_stream_start"}),
                        }
                        await redis_instance.redis_client.xadd(stream_key, event_data)

                        ai_answer_string = "Please login to generate a report. This feature is only available for registered users."
                        for char in range(0, len(ai_answer_string), 4):
                            event_data = {
                                "event": "delta",
                                "data": json.dumps({"chunk": ai_answer_string[char : char + 4]}),
                            }
                            await redis_instance.redis_client.xadd(stream_key, event_data)
                            await asyncio.sleep(0.01)

                        event_data = {
                            "event": "message",
                            "data": json.dumps({"type": "message_stream_complete"}),
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
                                    "chat_id": chat_id,
                                }
                            ),
                        }
                        await redis_instance.redis_client.xadd(stream_key, event_data)

        if isinstance(output, dict) and "messages" in output:
            all_messages_after_query = [msg.model_dump() for msg in output["messages"][1:]]
        else:
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

        if is_new_chat:
            chat_data = {
                "chat_id": chat_id,
                "user_id": "User",
                "chat_title": chat_title,
                "chat_messages": all_messages_after_query,
                "created_at": query_received_time,
                "updated_at": message_stream_complete_time,
            }
            redis_response = await redis_instance.create_chat(chat_data=chat_data)
            if not redis_response.get("success"):
                logger.error(
                    f"Failed to create new chat in Redis for chat_id: {chat_id}: {redis_response.get('error')}"
                )
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
        else:
            chat_data = {
                "chat_id": chat_id,
                "chat_messages": all_messages_after_query,
                "updated_at": message_stream_complete_time,
            }
            redis_response = await redis_instance.update_chat(chat_data=chat_data)

            if not redis_response.get("success"):
                logger.error(
                    f"Failed to update chat in Redis for chat_id: {chat_id}: {redis_response.get('error')}"
                )
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

    except Exception as e:
        logger.error(f"Error in temp_chat_producer: {e}")

        # Surface the failure to the error-digest email pipeline.  This block
        # runs inside a FastAPI BackgroundTask, so the HTTP 500-middleware in
        # main.py never sees it; without this explicit queueing the operator
        # would only get a CloudWatch line and no alert email.
        try:
            from app.observability.error_alerter import queue_background_error

            chat_id_for_log = getattr(data, "chat_id", None) or "unknown"
            await queue_background_error(
                path=f"temp_chat_producer:/api/v1/temp_chat (chat_id={chat_id_for_log})",
                error_message=f"temp_chat_producer failed: {e}",
                user_id=None,
                exc=e,
            )
        except Exception as alert_error:
            logger.warning(f"Failed to queue background error alert: {alert_error}")

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
        logger.info(f"Stream completed for temporary chat_id: {chat_id}")
        event_data = {"event": "message", "data": json.dumps({"type": "end_stream"})}
        await redis_instance.redis_client.xadd(stream_key, event_data)


@router.post(
    "/temp-chat",
    status_code=200,
    response_class=JSONResponse,
    summary="Request for processing temporary chat",
    description="Request for processing temporary chat",
    responses={
        200: {"content": {"application/json": {}}},
        401: {"model": ErrorResponse},
        422: {"model": ErrorResponse},
        500: {"model": ErrorResponse},
    },
)
async def temp_chat(data: TempChatRequest, background_tasks: BackgroundTasks):
    logger.info(f"Received temporary chat request for temporary chat_id: {data.chat_id}")
    try:
        # Check if session id is valid
        redis_session_id = await redis_instance.redis_client.get(
            f"temp_chat_session:{data.chat_id}"
        )
        if redis_session_id != f"session:{data.session_id}":
            return JSONResponse(
                status_code=401,
                content={
                    "success": False,
                    "error": "This temporary chat has timed out. Let's start a fresh conversation.",
                },
            )

        stream_key = f"session:{data.session_id}"
        if not await redis_instance.redis_client.exists(stream_key):
            logger.warning(
                f"[SSE] Session expired for temporary chat_id: {data.chat_id} and session_id: {data.session_id}"
            )
            return JSONResponse(
                status_code=401, content={"success": False, "error": "Session expired!"}
            )

        await redis_instance.redis_client.expire(
            f"temp_chat_session:{data.chat_id}", timedelta(hours=3)
        )
        await redis_instance.redis_client.expire(stream_key, timedelta(hours=3))

        # Add the chat_producer function directly to background tasks
        logger.info(f"Creating background task for temporary chat_id: {data.chat_id}")
        background_tasks.add_task(temp_chat_producer, data)
        logger.info("Background task created for temporary chat_id: {data.chat_id}")
        return JSONResponse(
            status_code=200, content={"success": True, "message": "Temporary chat stream started"}
        )

    except RequestValidationError as e:
        # Validation errors
        logger.error(f"Validation error in temporary chat endpoint: {e}")
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
        logger.error(f"Error in temporary chat endpoint: {e}")
        return JSONResponse(
            status_code=500,
            content={
                "success": False,
                "error": "We're experiencing technical difficulties. Please try again later.",
            },
        )


@router.get(
    "/temp-chat/{chat_id}",
    response_model=TempChatMessagesResponse,
    status_code=200,
    responses={
        404: {"model": ErrorResponse},
        401: {"model": ErrorResponse},
        422: {"model": ErrorResponse},
        500: {"model": ErrorResponse},
    },
)
async def get_temp_chat_messages(chat_id: str):
    """Get temporary chat messages endpoint"""
    logger.info(f"Get temp chat messages request received for chat_id: {chat_id}")

    try:
        # Get temp chat data from Redis
        redis_response = await redis_instance.get_chat(chat_id=chat_id)
        if not redis_response.get("success"):
            logger.error(
                f"Failed to get temp chat from Redis for chat_id: {chat_id}: {redis_response.get('error')}"
            )
            return JSONResponse(
                status_code=404,
                content={
                    "success": False,
                    "error": "Temporary chat not found. It may have expired.",
                },
            )

        chat_data = redis_response.get("chat_data", {})
        chat_messages = chat_data.get("chat_messages", [])

        if not chat_messages:
            logger.warning(f"No chat messages found for temp chat_id: {chat_id}")
            return TempChatMessagesResponse(success=True, messages=[])

        # Format chat messages (same logic as regular chat)
        formatted_chat_messages = []
        for i, msg in enumerate(chat_messages):
            msg_type = msg.get("type")
            msg_content = msg.get("content")

            if msg_type == "human":
                msg_object = {"type": msg_type, "content": msg_content}
                formatted_chat_messages.append(msg_object)
            elif msg_type == "ai":
                msg_metadata = msg.get("response_metadata", {})
                msg_stop = (
                    msg_metadata.get("stopReason", "")
                    or msg_metadata.get("stop_reason", "")
                    or msg_metadata.get("finish_reason", "")
                )
                if msg_stop in ("tool_use", "tool_calls"):
                    called_names = [tc.get("name", "") for tc in msg.get("tool_calls", [])]
                    if "retrieve" not in called_names:
                        continue
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
                formatted_chat_messages.append(msg_object)

        return TempChatMessagesResponse(success=True, messages=formatted_chat_messages)

    except RequestValidationError as e:
        logger.error(
            f"Validation error in get temp chat messages endpoint: {e} for chat_id: {chat_id}"
        )
        return JSONResponse(
            status_code=422,
            content={
                "success": False,
                "error": "Your request couldn't be understood. Please check your input and try again.",
            },
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in get temp chat messages endpoint: {e} for chat_id: {chat_id}")
        return JSONResponse(
            status_code=500,
            content={
                "success": False,
                "error": "Something unexpected happened. Please try again later.",
            },
        )
