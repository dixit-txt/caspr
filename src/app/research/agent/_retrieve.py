"""Retrieval + read-tool nodes: retrieve, query_document, retrieve_latest_info, tool dispatch."""

import asyncio
import datetime
import json
import time
from typing import Any, Literal

from google import genai

# from langchain_aws import ChatBedrock  # Bedrock disabled — using direct Anthropic API instead
from langchain_core.documents.base import Document
from langchain_core.messages import (
    AIMessage,
    ToolMessage,
)
from langgraph.config import get_stream_writer

# from langchain_core.messages.base import BaseMessage
# from langchain_tavily import TavilySearch
from langgraph.graph import MessagesState
from uuid_utils import uuid7

from app.adapters.grep_agent_2 import ask_pdfs
from app.admin.repository_web_search import log_web_search_event
from app.cards.service_cards import (
    _updated_layout_to_markdown,
    clean_drl,
    clean_drl_to_clean_rl,
    clean_url,
    generate_drl,
    modify_report_layout,
    remove_citations_from_DRL,
    remove_citations_from_RL,
)

# from app.research.infographics import process_infographics
# from app.deliverables.helper_functions import generate_image_with_Google, generate_image_with_openai
from app.core.constants import (
    ASYNC_OPENAI_CLIENT,
    GEMINI_API_KEY,
    GEMINI_QUERY_DOC_MODEL,
    GEMINI_RETRIEVE_LATEST_INFO_MODEL,
    MAX_QUERY_DOC_CALLS_PER_TURN,
    MAX_WEB_SEARCH_CALLS_PER_TURN,
    QUERY_DOC_MODEL,
    RETRIEVE_LATEST_INFO_MODEL,
)
from app.core.logging import setup_logging
from app.observability.llm_response_logger import save_raw_llm_response, strip_json_code_fence
from app.observability.web_search_analytics import (
    extract_gemini_search_analytics,
    extract_openai_search_analytics,
)
from app.research.agent._base import CasperBase
from app.research.agent._helpers import (
    _normalize_report_config,
    _normalize_tool_call,
    _run_heartbeat,
)
from app.research.agent._state import (
    GENERIC_TOOL_ERROR_MSG,
    REPORT_TIERS,
    DocumentQueryResponse,
)

logger = setup_logging(__name__)


class RetrieveMixin(CasperBase):
    """Retrieval + read-tool nodes: retrieve, query_document, retrieve_latest_info, tool dispatch."""

    def _uses_grep_file_search(self) -> bool:
        return self.file_search_mode == "grep"

    def _fallback_to_openai_file_search(self) -> bool:
        """Switch from grep to OpenAI file search after a grep_agent failure."""
        openai_config = self.openai_upload_file_config
        file_ids = (openai_config or {}).get("file_ids")
        if not openai_config or not file_ids:
            logger.error(
                f"Cannot fall back to OpenAI file search: missing upload_file_config "
                f"for user: {self.user_name} - chat_id: {self.chat_id}"
            )
            return False
        self.file_search_mode = "openai"
        self.upload_file_config = openai_config
        logger.warning(
            f"grep_agent failed; switched file_search_mode to openai for "
            f"user: {self.user_name} - chat_id: {self.chat_id}"
        )
        return True

    @staticmethod
    def _grep_result_failed(result: dict, answer: str) -> bool:
        if not answer or not answer.strip():
            return True
        lowered = answer.strip().lower()
        return (
            lowered.startswith("error processing documents")
            or lowered.startswith("no pdf paths provided")
            or "error processing documents:" in lowered
        )

    def _has_uploaded_documents(self) -> bool:
        """True when the session has documents for query_document (mode-specific)."""
        if self._uses_grep_file_search():
            return self.grep_session is not None
        config = self.upload_file_config
        if not config or not isinstance(config, dict):
            return False
        file_ids = config.get("file_ids")
        return bool(file_ids) and file_ids not in ("", "null", "None")

    async def _query_document_openai(self, user_query: str) -> str:
        """Query documents via OpenAI vector store file_search."""
        config = self.upload_file_config
        if not config:
            logger.error(
                f"No OpenAI document configuration available for user: {self.user_name} - "
                f"chat_id: {self.chat_id}"
            )
            return GENERIC_TOOL_ERROR_MSG

        file_ids = config.get("file_ids")
        vector_store_id = config.get("vector_store_id")

        if not file_ids:
            logger.error(
                f"No document file_ids available for user: {self.user_name} - chat_id: {self.chat_id}"
            )
            return GENERIC_TOOL_ERROR_MSG

        try:
            for file_id in config["file_ids"]:
                await ASYNC_OPENAI_CLIENT.files.retrieve(file_id)
                logger.info(
                    f"File {file_id} verified for user: {self.user_name} - chat_id: {self.chat_id}"
                )
        except Exception as file_check_error:
            logger.error(
                f"OpenAI file check failed: {file_check_error} for user: {self.user_name} - "
                f"chat_id: {self.chat_id}"
            )
            return GENERIC_TOOL_ERROR_MSG

        if not vector_store_id:
            logger.error(
                f"No vector_store_id for OpenAI file search | user: {self.user_name} - "
                f"chat_id: {self.chat_id}"
            )
            return GENERIC_TOOL_ERROR_MSG

        current_date = datetime.datetime.now().strftime("%B %d, %Y")
        logger.info(
            f"Querying document via OpenAI file_search | vector_store={vector_store_id} | "
            f"file_ids={file_ids} | user: {self.user_name} - chat_id: {self.chat_id}"
        )

        prompt = f"""Current Date: {current_date}

User Question: {user_query}

Please analyze the uploaded document to answer the question based on the document content.
Provide specific references to sections or pages if available."""

        filters = {
            "type": "and",
            "filters": [
                {
                    "type": "in",
                    "key": "file_id",
                    "value": config["file_ids"],
                },
            ],
        }

        try:
            response = await ASYNC_OPENAI_CLIENT.responses.create(
                model=QUERY_DOC_MODEL,
                input=prompt,
                tools=[
                    {
                        "type": "file_search",
                        "vector_store_ids": [vector_store_id],
                        "filters": filters,
                    }
                ],
                tool_choice="auto",
            )
            save_raw_llm_response(
                response,
                QUERY_DOC_MODEL,
                "Answering a question from uploaded documents",
                self.chat_id,
                user_id=self.user_id or self.user_name,
            )

            answer = response.output_text
            logger.info(
                f"OpenAI file_search query complete for user: {self.user_name} - chat_id: {self.chat_id}"
            )
            return answer
        except Exception as e:
            logger.error(
                f"OpenAI file_search query failed ({e}), falling back to Gemini API | "
                f"user: {self.user_name} - chat_id: {self.chat_id}"
            )
            # Best-effort fallback: Gemini's file_search tool expects a Gemini-native
            # vector store, so this only succeeds if the OpenAI vector_store_id is
            # also reachable from Gemini (e.g. shared/mirrored store). If not, this
            # raises and the caller's own except handles the GENERIC_TOOL_ERROR_MSG.
            gemini_client = genai.Client(api_key=GEMINI_API_KEY)
            interaction = gemini_client.interactions.create(
                model=GEMINI_QUERY_DOC_MODEL,
                input=prompt,
                tools=[
                    {
                        "type": "file_search",
                        "vector_store_ids": [vector_store_id],
                        "filters": filters,
                    }
                ],
            )
            save_raw_llm_response(
                interaction,
                GEMINI_QUERY_DOC_MODEL,
                "Answering a question from uploaded documents (backup)",
                self.chat_id,
                user_id=self.user_id or self.user_name,
            )

            answer = interaction.output_text
            logger.info(
                f"Gemini file_search fallback query complete for user: {self.user_name} - chat_id: {self.chat_id}"
            )
            return answer

    async def _query_document_grep(self, user_query: str) -> str:
        """Query documents via grep_agent_2 using the pre-built PdfSession; raises on failure so caller can fall back to OpenAI."""
        if not self.grep_session:
            raise RuntimeError("No PdfSession available for grep search")

        logger.info(
            f"Querying document via grep_agent_2 | user: {self.user_name} - chat_id: {self.chat_id}"
        )
        result = await asyncio.to_thread(
            ask_pdfs,
            self.grep_session,
            user_query,
            chat_id=self.chat_id,
            user_id=self.user_id or self.user_name,
        )
        answer = result.get("answer", "")
        if self._grep_result_failed(result, answer):
            raise RuntimeError(f"grep_agent_2 failed: {answer}")

        citations = result.get("citations") or []
        if citations:
            cite_lines = []
            for c in citations:
                doc = c.get("document", "")
                page = c.get("page", "")
                section = c.get("section", "")
                cite_lines.append(f"- {doc} (page {page}, {section})")
            answer = f"{answer}\n\nSources from documents:\n" + "\n".join(cite_lines)

        logger.info(
            f"grep_agent_2 query complete | workers_used={result.get('workers_used')} | "
            f"user: {self.user_name} - chat_id: {self.chat_id}"
        )
        return answer

    async def query_document(
        self,
        user_query: str,
        status_message: str = "",
        progress_updates: list[str] | None = None,
    ) -> str:
        """Query an uploaded document to extract information and answer questions.

        Use this tool when the user has uploaded a document and wants to ask questions about it.
        This tool analyzes only the document content.

        Args:
            user_query (str): The user's question about the document.
            status_message (str): Short user-facing message shown before the query starts,
                e.g. "Reading through your uploaded document".  Never mention tool names or
                internal details.
            progress_updates (List[str]): Ordered list of EXACTLY 5 short, query-specific
                lines describing how this document lookup unfolds.  Shown one at a time
                (about every 4 seconds) while the query is in flight.  Always provide all 5 —
                they should narrate the lookup from start to finish so a longer read stays
                well-narrated.
                Example: ["Scanning the document for relevant sections",
                          "Reading the matching passages carefully",
                          "Checking the supporting details",
                          "Connecting the findings together",
                          "Pulling the key details together"].

        Returns:
            str: The answer to the user's question based on the document content
        """
        try:
            event_writer = get_stream_writer()
            event_writer(
                {
                    "name": "query_document",
                    "status": "start",
                    "message": status_message,
                    "progress_updates": progress_updates or [],
                }
            )

            if not self._has_uploaded_documents():
                logger.error(
                    f"No document configuration available for user: {self.user_name} - "
                    f"chat_id: {self.chat_id}"
                )
                return GENERIC_TOOL_ERROR_MSG

            if self._uses_grep_file_search():
                try:
                    started = time.time()
                    heartbeat_task = asyncio.create_task(
                        _run_heartbeat(
                            user_query,
                            progress_updates or [],
                            event_writer,
                            started,
                            tool_name="query_document",
                        )
                    )
                    try:
                        answer = await self._query_document_grep(user_query)
                    finally:
                        heartbeat_task.cancel()
                        try:
                            await heartbeat_task
                        except asyncio.CancelledError:
                            pass
                except Exception as grep_err:
                    logger.error(
                        f"grep_agent_2 error: {grep_err} for user: {self.user_name} - "
                        f"chat_id: {self.chat_id}",
                        exc_info=True,
                    )
                    if self._fallback_to_openai_file_search():
                        started = time.time()
                        heartbeat_task = asyncio.create_task(
                            _run_heartbeat(
                                user_query,
                                progress_updates or [],
                                event_writer,
                                started,
                                tool_name="query_document",
                            )
                        )
                        try:
                            answer = await self._query_document_openai(user_query)
                        finally:
                            heartbeat_task.cancel()
                            try:
                                await heartbeat_task
                            except asyncio.CancelledError:
                                pass
                    else:
                        return GENERIC_TOOL_ERROR_MSG
            else:
                logger.info(
                    f"Using OpenAI file_search (file_search_mode=openai) for user: "
                    f"{self.user_name} - chat_id: {self.chat_id}"
                )
                started = time.time()
                heartbeat_task = asyncio.create_task(
                    _run_heartbeat(
                        user_query,
                        progress_updates or [],
                        event_writer,
                        started,
                        tool_name="query_document",
                    )
                )
                try:
                    answer = await self._query_document_openai(user_query)
                finally:
                    heartbeat_task.cancel()
                    try:
                        await heartbeat_task
                    except asyncio.CancelledError:
                        pass

            event_writer(
                {
                    "name": "query_document",
                    "status": "end",
                    "elapsed": f"{time.time() - started:.1f}s",
                }
            )
            return answer

        #             else:
        #                 # Use traditional file-based approach
        #                 logger.info(f"Querying document {file_id} using traditional approach for user: {self.user_name} - chat_id: {self.chat_id}")

        #                 prompt = f"""Current Date: {current_date}

        # User Question: {user_query}

        # Please analyze the uploaded document to answer the question based on the document content.
        # Provide specific references to sections or pages if available."""

        #                 user_content = [
        #                     {
        #                         "type": "input_text",
        #                         "text": prompt,
        #                     },
        #                     {
        #                         "type": "input_file",
        #                         "file_id": file_id
        #                     }
        #                 ]
        #                 response = await ASYNC_OPENAI_CLIENT.responses.parse(
        #                     model="gpt-5.2",
        #                     input=[
        #                         {"role": "system", "content": "You are a helpful assistant that answers questions based on the provided document content."},
        #                         {"role": "user", "content": user_content}
        #                     ],
        #                     text_format=DocumentQueryResponse,
        #                     temperature=0.1,
        #                 )

        #                 output = response.model_dump()['output'][-1]
        #                 json_output = output['content'][0]['parsed']
        #                 answer = json_output['answer']

        #                 logger.info(f"Successfully queried document using traditional approach for user: {self.user_name} - chat_id: {self.chat_id}")
        #                 return answer

        except Exception as e:
            logger.error(
                f"Error querying document: {e} for user: {self.user_name} - chat_id: {self.chat_id}"
            )
            event_writer({"name": "query_document", "status": "end", "elapsed": ""})
            return GENERIC_TOOL_ERROR_MSG

    async def retrieve_latest_info(
        self,
        user_query: str,
        status_message: str = "",
        progress_updates: list[str] | None = None,
    ) -> str:
        """
        Retrieve the latest up-to-date information to answer questions accurately.

        DISABLED: this tool is no longer bound to the model or dispatched anywhere — the
        conversation's only web lookup is now the background layout refresh in
        `update_proposed_report_layout`. The implementation is kept intact so the tool can
        be restored by uncommenting its bindings in `_initialize_components`,
        `report_or_respond`, `_build_graph` and `_dispatch_read_tool`.

        Use this tool when the user asks questions that require current, real-time, or up-to-date information.
        This is useful for:
        - Current events and news
        - Latest data, statistics, or trends
        - Real-time information (stock prices, weather, etc.)
        - Questions about recent developments
        - Any information that needs to be current and verified

        Args:
            user_query (str): The user's question that requires up-to-date information.
            status_message (str): Short user-facing message shown before the search starts,
                e.g. "Checking Nvidia's latest AI chip updates".  Never mention tool names,
                APIs, web_search, or internal details.
            progress_updates (List[str]): Ordered list of EXACTLY 5 short, query-specific
                lines describing how this search unfolds.  Shown one at a time (about every
                4 seconds) while the search is in flight so the user sees the assistant's own
                evolving narration.  Always provide all 5 — they should narrate the search
                from start to finish so a longer search stays well-narrated.
                Example: ["Looking for Nvidia's most recent earnings",
                          "Reading analyst takes on GPU demand",
                          "Cross-checking the latest figures",
                          "Comparing against competitor trends",
                          "Pulling the key numbers together"].
                Never generic filler; never mention tools/APIs/internal details.

        Returns:
            str: The answer to the user's question with citations from verified sources
        """
        try:
            event_writer = get_stream_writer()
            event_writer(
                {
                    "name": "retrieve_latest_info",
                    "status": "start",
                    "message": status_message,
                    "progress_updates": progress_updates or [],
                }
            )

            search_mode = "forced" if self.web_search else "intelligent"
            logger.info(
                f"Retrieving latest info ({search_mode} mode) for query: {user_query[:100]}... for user: {self.user_name} - chat_id: {self.chat_id}"
            )

            current_date = datetime.datetime.now().strftime("%B %d, %Y")

            prompt = f"""Current Date: {current_date}

User Question: {user_query}

Please search the web for the latest and most accurate information to answer this question comprehensively.
Make sure to cite your sources appropriately."""

            started = time.time()
            operation_id = str(uuid7())
            heartbeat_task = asyncio.create_task(
                _run_heartbeat(user_query, progress_updates or [], event_writer, started)
            )

            try:
                request_started = time.perf_counter()
                try:
                    response = await ASYNC_OPENAI_CLIENT.responses.parse(
                        model=RETRIEVE_LATEST_INFO_MODEL,
                        input=[
                            {
                                "role": "system",
                                "content": "You are Caspr, an analyst. Search the web for the most accurate, up-to-date information, answer in conclusions with the numbers first, no hedging and no exclamation points, and cite every claim to its source.",
                            },
                            {"role": "user", "content": prompt},
                        ],
                        tools=[{"type": "web_search"}],
                        tool_choice={"type": "web_search"},
                        text_format=DocumentQueryResponse,
                        temperature=0.1,
                        include=["web_search_call.results"],
                    )
                except Exception as provider_exc:
                    try:
                        asyncio.create_task(
                            log_web_search_event(
                                trigger_source="chat",
                                user_query=user_query,
                                candidate_links=[],
                                cited_links=[],
                                operation_id=operation_id,
                                attempt_number=1,
                                provider="openai",
                                model_used=RETRIEVE_LATEST_INFO_MODEL,
                                status="failed",
                                error_type=type(provider_exc).__name__,
                                duration_ms=round((time.perf_counter() - request_started) * 1000),
                                search_call_count=0,
                                user_id=self.user_id,
                                chat_id=self.chat_id,
                            )
                        )
                    except Exception:
                        logger.exception(
                            "[retrieve_latest_info] Failed to schedule non-fatal "
                            "provider failure analytics"
                        )

                    logger.info(
                        f"[retrieve_latest_info] OpenAI web search failed ({provider_exc}), falling back to Gemini | chat_id={self.chat_id}"
                    )
                    gemini_request_started = time.perf_counter()
                    try:
                        gemini_client = genai.Client(api_key=GEMINI_API_KEY)
                        interaction = gemini_client.interactions.create(
                            model=GEMINI_RETRIEVE_LATEST_INFO_MODEL,
                            input=prompt,
                            system_instruction="You are Caspr, an analyst. Search the web for the most accurate, up-to-date information, answer in conclusions with the numbers first, no hedging and no exclamation points, and cite every claim to its source.",
                            tools=[{"type": "google_search"}],
                            response_format={
                                "type": "text",
                                "mime_type": "application/json",
                                "schema": DocumentQueryResponse.model_json_schema(),
                            },
                            generation_config={
                                "temperature": 0.1,
                                "thinking_config": {"thinking_budget": 0},
                            },
                        )
                    except Exception as gemini_exc:
                        try:
                            asyncio.create_task(
                                log_web_search_event(
                                    trigger_source="chat",
                                    user_query=user_query,
                                    candidate_links=[],
                                    cited_links=[],
                                    operation_id=operation_id,
                                    attempt_number=2,
                                    provider="gemini",
                                    model_used=GEMINI_RETRIEVE_LATEST_INFO_MODEL,
                                    status="failed",
                                    error_type=type(gemini_exc).__name__,
                                    duration_ms=round(
                                        (time.perf_counter() - gemini_request_started) * 1000
                                    ),
                                    search_call_count=0,
                                    user_id=self.user_id,
                                    chat_id=self.chat_id,
                                )
                            )
                        except Exception:
                            logger.exception(
                                "[retrieve_latest_info] Failed to schedule non-fatal "
                                "gemini failure analytics"
                            )
                        # Both providers failed — surface the original OpenAI error
                        # so the outer except returns GENERIC_TOOL_ERROR_MSG.
                        raise provider_exc

                    gemini_request_duration_ms = round(
                        (time.perf_counter() - gemini_request_started) * 1000
                    )
                    save_raw_llm_response(
                        interaction,
                        GEMINI_RETRIEVE_LATEST_INFO_MODEL,
                        "retrieve_latest_info (backup)",
                        self.chat_id,
                        user_id=self.user_id,
                    )

                    try:
                        gemini_analytics_event = extract_gemini_search_analytics(
                            interaction, model_used=GEMINI_RETRIEVE_LATEST_INFO_MODEL
                        )
                    except Exception:
                        logger.exception(
                            "[retrieve_latest_info] Failed to extract Gemini analytics; "
                            "scheduling minimal successful event"
                        )
                        gemini_analytics_event = {
                            "provider": "gemini",
                            "provider_response_id": getattr(interaction, "id", None),
                            "provider_queries": [],
                            "model_used": GEMINI_RETRIEVE_LATEST_INFO_MODEL,
                            "status": "succeeded",
                            "usage_metadata": None,
                            "search_call_count": 0,
                            "candidate_links": [],
                            "cited_links": [],
                        }
                    gemini_analytics_event.update(
                        {
                            "trigger_source": "chat",
                            "user_query": user_query,
                            "operation_id": operation_id,
                            "attempt_number": 2,
                            "model_used": gemini_analytics_event.get("model_used")
                            or GEMINI_RETRIEVE_LATEST_INFO_MODEL,
                            "status": "succeeded",
                            "duration_ms": gemini_request_duration_ms,
                            "user_id": self.user_id,
                            "chat_id": self.chat_id,
                        }
                    )
                    try:
                        asyncio.create_task(log_web_search_event(**gemini_analytics_event))
                    except Exception:
                        logger.exception(
                            "[retrieve_latest_info] Failed to schedule non-fatal "
                            "gemini success analytics"
                        )

                    gemini_parsed = json.loads(strip_json_code_fence(interaction.output_text))
                    gemini_answer = gemini_parsed.get("answer", "")
                    gemini_citations = [
                        clean_url(u) for u in (gemini_parsed.get("citations") or []) if clean_url(u)
                    ]

                    logger.info(
                        f"[retrieve_latest_info] Gemini fallback succeeded with {len(gemini_citations)} "
                        f"citations for user: {self.user_name} - chat_id: {self.chat_id}"
                    )
                    event_writer(
                        {
                            "name": "retrieve_latest_info",
                            "status": "end",
                            "citations": gemini_citations,
                            "elapsed": f"{time.time() - started:.1f}s",
                        }
                    )
                    if not hasattr(self, "_latest_info_context"):
                        self._latest_info_context = []
                    self._latest_info_context.append(gemini_answer)
                    return gemini_answer
                request_duration_ms = round((time.perf_counter() - request_started) * 1000)
                try:
                    analytics_event = extract_openai_search_analytics(response)
                except Exception:
                    logger.exception(
                        "[retrieve_latest_info] Failed to extract analytics; "
                        "scheduling minimal successful event"
                    )
                    analytics_event = {
                        "provider": "openai",
                        "provider_response_id": getattr(response, "id", None),
                        "provider_queries": [],
                        "model_used": RETRIEVE_LATEST_INFO_MODEL,
                        "status": "succeeded",
                        "usage_metadata": None,
                        "search_call_count": 0,
                        "candidate_links": [],
                        "cited_links": [],
                    }
                analytics_event.update(
                    {
                        "trigger_source": "chat",
                        "user_query": user_query,
                        "operation_id": operation_id,
                        "attempt_number": 1,
                        "model_used": analytics_event.get("model_used")
                        or RETRIEVE_LATEST_INFO_MODEL,
                        "status": "succeeded",
                        "duration_ms": request_duration_ms,
                        "user_id": self.user_id,
                        "chat_id": self.chat_id,
                    }
                )
                try:
                    asyncio.create_task(log_web_search_event(**analytics_event))
                except Exception:
                    logger.exception(
                        "[retrieve_latest_info] Failed to schedule non-fatal "
                        "provider success analytics"
                    )
                save_raw_llm_response(
                    response,
                    RETRIEVE_LATEST_INFO_MODEL,
                    "retrieve_latest_info",
                    self.chat_id,
                    user_id=self.user_id,
                )
            finally:
                heartbeat_task.cancel()
                try:
                    await heartbeat_task
                except asyncio.CancelledError:
                    pass

            # Parallel search path kept for reference; OpenAI web_search is the active provider.
            # from parallel import AsyncParallel
            # client = AsyncParallel(api_key=PARALLEL_API_KEY)
            # search = await client.search(
            #     objective="Find latest information about the user's question",
            #     search_queries=[user_query],
            # )
            # results = []
            # results.extend(search.results)
            # return str(results)

            output_items = response.model_dump().get("output", [])

            # Collect citations from two places in the response:
            #   1. web_search_call.results  — rich objects with url/title/snippet
            #   2. message content annotations — inline URL references added by
            #      the model inside its final answer text
            # Both sources are de-duplicated via _seen_urls so no URL is counted twice.
            # raw_search_results retains title + snippet for the existing completion
            # log; citations holds only the cleaned URLs returned to the caller.
            _seen_urls: set = set()
            citations: list = []
            raw_search_results: list = []

            def _add_citation(url: str, title: str = None, snippet: str = None) -> None:
                cleaned = clean_url(url)
                if cleaned and cleaned not in _seen_urls:
                    _seen_urls.add(cleaned)
                    citations.append(cleaned)
                    raw_search_results.append(
                        {
                            "url": cleaned,
                            "title": title or None,
                            "snippet": snippet or None,
                        }
                    )

            for _item in output_items:
                if _item.get("type") == "web_search_call":
                    for _result in _item.get("results") or []:
                        if _result.get("url"):
                            _add_citation(
                                _result["url"],
                                title=_result.get("title"),
                                snippet=_result.get("snippet"),
                            )
                if _item.get("type") == "message":
                    for _block in _item.get("content") or []:
                        for _ann in _block.get("annotations") or []:
                            if "url" in _ann:
                                _add_citation(_ann["url"])

            logger.info(
                f"[retrieve_latest_info] Web search complete | "
                f"unique_citations={len(raw_search_results)} chat_id={self.chat_id}"
            )

            # Extract the answer from the last message-type output item.
            answer = ""
            for _item in reversed(output_items):
                if _item.get("type") != "message":
                    continue
                for _block in _item.get("content") or []:
                    _parsed = _block.get("parsed")
                    if isinstance(_parsed, dict) and _parsed.get("answer"):
                        answer = _parsed["answer"]
                        break
                    _text = _block.get("text") or _block.get("output_text")
                    if _text:
                        answer = _text
                        break
                if answer:
                    break

            # Format response with citations
            # if citations:
            #     answer += "\n\n**Sources:**\n"
            #     for idx, url in enumerate(citations, 1):
            #         answer += f"{idx}. {clean_url(url)}\n"

            logger.info(
                f"Successfully retrieved latest info with {len(citations)} citations for user: {self.user_name} - chat_id: {self.chat_id}"
            )
            event_writer(
                {
                    "name": "retrieve_latest_info",
                    "status": "end",
                    "citations": [clean_url(url) for url in citations],
                    "elapsed": f"{time.time() - started:.1f}s",
                }
            )
            # Store the retrieved info for use in DRL generation
            if not hasattr(self, "_latest_info_context"):
                self._latest_info_context = []
            self._latest_info_context.append(answer)
            return answer

        except Exception as e:
            logger.error(
                f"Error retrieving latest info: {e} for user: {self.user_name} - chat_id: {self.chat_id}"
            )
            event_writer({"name": "retrieve_latest_info", "status": "end"})
            return GENERIC_TOOL_ERROR_MSG

    async def retrieve(
        self,
        user_instructions: str,
        report_layout: str,
        report_language: str,
        report_title: str,
        domain_name: Literal[
            "default",
            "primary_research",
            "due_diligence",
            "industry_benchmarking",
            "market_insight",
            "rfp",
            "business_plan",
        ],
        # Do not add report_layout_updated here — retrieve is an LLM tool, and
        # the web-refreshed layout is already on self.updated_proposed_report_layout.
        report_type: Literal["study", "brief"] = "study",
    ) -> tuple[str, list[Document]]:
        """Retrieve information as per user_instructions from the vector store and verified sources and generate report according to the report_layout

        This method is used as a tool by the LLM.

        Args:
            user_instructions (str): Detailed instructions captured from the user about report requirements. Do mention in the user_instructions that the report should be generated in the English only never user any other language.
            report_layout (str): The proposed markdown structure/layout of the report that has been approved by the user. Make sure to always generate report layout in ENglish only no other language.
            report_language (str): The report should be generated in the English only never user any other language.
            report_title (str): The title of the report in 7 words or less.
            domain_name (Literal['default', 'primary_research', 'due_diligence', 'industry_benchmarking', 'market_insight', 'rfp', 'business_plan']): **REQUIRED**. The report domain type. You MUST pass this parameter explicitly on every call — do NOT omit it. Pick the value that matches the report type the user confirmed in the feedback phase. Use 'primary_research' when the user wants a report that analyses their own uploaded research data (surveys, interviews, experiments, datasets) using ONLY the uploaded document as the source. Use 'due_diligence' when the user wants a due diligence investigation report that uses the Learning Brain and external data feeds (financial databases, regulatory filings, news, court records) to investigate and assess an entity. Use 'industry_benchmarking' when the user wants to benchmark companies/industries against competitors or industry standards. Use 'market_insight' when the user wants market intelligence, trends analysis, or market opportunity reports. Use 'rfp' when the user wants to create or respond to a Request for Proposal. Use 'business_plan' when the user wants to generate a business plan or business strategy document. Use 'default' ONLY when the user explicitly selected 'Standard Report' option 7, or when the request genuinely does not fit any specialized domain.
            report_type (Literal['study', 'brief']): The type of report to generate. 'study' is the default full-length detailed research report with comprehensive analysis, citations, and deep coverage of 8-10+ sections. 'brief' generates a shorter, concise research brief with a maximum of 5 sections that summarizes key findings quickly. Default is 'study'. Use 'brief' ONLY when the user explicitly asks for a brief or short summary report.
        """
        # async def retrieve(self, user_instructions: str, report_layout: str, report_language:str ,report_title: str) -> Tuple[str, List[Document]]:
        #     """Retrieve information as per user_instructions from the vector store and web search and generate report according to the report_layout

        #     This method is used as a tool by the LLM.

        #     Args:
        #         user_instructions (str): Detailed instructions captured from the user about report requirements.
        #         report_layout (str): The proposed markdown structure/layout of the report that has been approved by the user.
        #         report_language (str): The language in which the report should be generated.
        #         report_title (str): The title of the report in 7 words or less.
        #     """
        try:
            event_writer = get_stream_writer()

            event_writer({"name": "retrieve", "report_title": report_title})

            # Input validation - Added
            if not user_instructions or not report_layout:
                logger.error(
                    f"Missing required parameters: user_instructions or report_layout for user: {self.user_name} - chat_id: {self.chat_id}"
                )
                return GENERIC_TOOL_ERROR_MSG, []

            # The tier the user confirmed on the popup is the only tier authority
            # (R10). It outranks this call's own `report_type` argument, which was
            # inferred from the wording of the conversation before the layouts
            # existed, and it outranks any pre-paid tier: an explicit selection
            # made with both layouts in view is the stronger signal.
            confirmed_tier = (self.report_config_submission or {}).get("report_tier")
            if confirmed_tier in REPORT_TIERS:
                if confirmed_tier != report_type:
                    logger.info(
                        f"[retrieve] Confirmed tier '{confirmed_tier}' overrides the "
                        f"model's report_type='{report_type}' for chat_id: {self.chat_id}"
                    )
                report_type = confirmed_tier

            # The layout the model hands over was drafted without web access. When the
            # background refresh produced a current version of it, the report is built from
            # THAT one instead; everything below then treats it like any other layout.
            # (A refresh still in flight is waited out — it started when the layout was
            # proposed, so what is left of it is usually short.)
            await self._await_layout_refresh()
            confirmed_layout = (self.report_layout_pair or {}).get(report_type) or {}
            if confirmed_layout.get("markdown"):
                # Already web-refreshed by `layout_pair` (R9), for whichever tier the
                # user landed on — including the one they switched to.
                report_layout = confirmed_layout["markdown"]
                logger.info(
                    f"[retrieve] Building the report from the confirmed '{report_type}' layout | "
                    f"sections={len(confirmed_layout.get('report_layout') or [])} | "
                    f"user: {self.user_name} - chat_id: {self.chat_id}"
                )
            elif self.updated_proposed_report_layout:
                report_layout = _updated_layout_to_markdown(self.updated_proposed_report_layout)
                logger.info(
                    f"[retrieve] Building the report from the web-updated proposed layout | "
                    f"sections={len(self.updated_proposed_report_layout.sections)} | "
                    f"user: {self.user_name} - chat_id: {self.chat_id}"
                )

            # Validating report layout
            report_layout_title = report_layout.strip().split()[0]
            now = datetime.datetime.now()
            year = now.strftime("%Y")
            month = now.strftime("%m")
            day = now.strftime("%d")

            # Upload original report layout to S3 as a background task
            original_report_layout_filename = (
                f"report_layout_original_{self.user_name}_chat_{self.chat_id}.txt"
            )
            asyncio.create_task(
                self._upload_report_layout_to_s3_background(
                    report_layout, original_report_layout_filename
                )
            )

            _SPECIALIZED_DOMAINS = {"primary_research", "due_diligence"}

            if report_layout_title.count("#") != 1:
                logger.warning(
                    f"Report layout title must start with exactly one '#' heading for user: {self.user_name} - chat_id: {self.chat_id}"
                )

                report_layout = await asyncio.to_thread(remove_citations_from_RL, report_layout)
                logger.info("Removed citations from report layout")
                if domain_name not in _SPECIALIZED_DOMAINS:
                    cleaned_report_layout_filename = (
                        f"report_layout_cleaned_{self.user_name}__chat_{self.chat_id}.txt"
                    )
                    asyncio.create_task(
                        self._upload_report_layout_to_s3_background(
                            report_layout, cleaned_report_layout_filename
                        )
                    )
                report_layout = f"# {report_title}\n\n" + report_layout

                logger.info(
                    f"Refined report layout for user: {self.user_name} - chat_id: {self.chat_id}"
                )
            else:
                logger.info(
                    f"Refining report layout for user: {self.user_name} - chat_id: {self.chat_id}"
                )
                report_layout = await asyncio.to_thread(remove_citations_from_RL, report_layout)
                if domain_name not in _SPECIALIZED_DOMAINS:
                    cleaned_report_layout_filename = (
                        f"report_layout_cleaned_{self.user_name}__chat_{self.chat_id}.txt"
                    )
                    asyncio.create_task(
                        self._upload_report_layout_to_s3_background(
                            report_layout, cleaned_report_layout_filename
                        )
                    )

            # Store configuration
            self.retrieve_config["user_instructions"] = user_instructions
            self.retrieve_config["report_layout"] = report_layout
            self.retrieve_config["report_length"] = report_type
            # self.retrieve_config['report_language'] = report_language
            self.retrieve_config["report_language"] = "English"
            self.retrieve_config["domain_name"] = domain_name
            self.retrieve_config["report_title"] = report_title
            self.retrieve_config["report_type"] = report_type
            # The whole confirmed submission lands here in one write (R12). Only
            # tier and style are read; output formats, language and data sources
            # are stored so the features that use them need no new plumbing.
            self.retrieve_config.update(
                self.report_config_submission or _normalize_report_config({}, report_type)
            )
            self.card_style = self.retrieve_config["style"]
            self.domain_name = domain_name

            logger.info(
                f"[retrieve] Domain routing decision: domain_name='{domain_name}' | "
                f"report_title='{report_title}' | "
                f"report_type='{report_type}' | "
                f"report_language='{report_language}' | "
                f"has_files={self._has_uploaded_documents()} | "
                f"web_search={self.web_search} | "
                f"user: {self.user_name} - chat_id: {self.chat_id}"
            )
            logger.info(
                f"[retrieve] Config stored: user_instructions_len={len(user_instructions)} | "
                f"report_layout_len={len(report_layout)} | "
                f"user: {self.user_name} - chat_id: {self.chat_id}"
            )

            event_writer({"name": "retrieve", "domain_name": domain_name})
            event_writer({"name": "retrieve", "report_type": report_type})

            if domain_name in _SPECIALIZED_DOMAINS:
                logger.info(
                    f"[retrieve] Specialized domain detected: domain_name='{domain_name}' — "
                    f"delegating DRL generation and report pipeline to '{domain_name}' subgraph | "
                    f"user: {self.user_name} - chat_id: {self.chat_id}"
                )
                return "", {
                    "report_layout": report_layout,
                    "domain_name": domain_name,
                }
            logger.info(
                f"[retrieve] Domain '{domain_name}' — proceeding with standard DRL generation pipeline | "
                f"user: {self.user_name} - chat_id: {self.chat_id}"
            )

            event_writer({"name": "retrieve", "status": "card_stream_start", "title": report_title})

            logger.info(
                f"Generating descriptive report layout for user: {self.user_name} - chat_id: {self.chat_id}"
            )
            # Pass any previously retrieved latest info as context for DRL generation
            latest_info_context = "\n\n".join(getattr(self, "_latest_info_context", [])) or None
            if latest_info_context:
                logger.info(
                    f"Passing {len(self._latest_info_context)} retrieve_latest_info results as context to DRL generation for user: {self.user_name} - chat_id: {self.chat_id}"
                )
            descriptive_report_layout = await asyncio.to_thread(
                generate_drl,
                report_layout,
                user_instructions,
                report_type,
                latest_info_context=latest_info_context,
                chat_id=self.chat_id,
                user_id=self.user_id or self.user_name,
            )
            if not descriptive_report_layout:
                logger.error(
                    f"Error generating descriptive report layout for user: {self.user_name} - chat_id: {self.chat_id}"
                )
                return GENERIC_TOOL_ERROR_MSG, {}
            logger.info(
                f"Loaded {len(descriptive_report_layout)} sections from layout for user: {self.user_name} - chat_id: {self.chat_id}"
            )

            original_descriptive_layout_filename = (
                f"descriptive_report_layout_original_{self.user_name}__chat_{self.chat_id}.json"
            )
            asyncio.create_task(
                self._upload_descriptive_layout_to_s3_background(
                    descriptive_report_layout, original_descriptive_layout_filename
                )
            )

            logger.info(
                f"Cleaning descriptive report layout for user: {self.user_name} - chat_id: {self.chat_id}"
            )
            descriptive_report_layout = await asyncio.to_thread(
                clean_drl, descriptive_report_layout
            )
            descriptive_report_layout = await asyncio.to_thread(
                remove_citations_from_DRL, descriptive_report_layout
            )
            cleaned_descriptive_layout_filename = (
                f"descriptive_report_layout_cleaned_{self.user_name}__chat_{self.chat_id}.json"
            )
            asyncio.create_task(
                self._upload_descriptive_layout_to_s3_background(
                    descriptive_report_layout, cleaned_descriptive_layout_filename
                )
            )
            if not descriptive_report_layout:
                logger.error(
                    f"Error cleaning descriptive report layout for user: {self.user_name} - chat_id: {self.chat_id}"
                )
                return GENERIC_TOOL_ERROR_MSG, {}
            logger.info(
                f"Cleaned descriptive report layout for user: {self.user_name} - chat_id: {self.chat_id}"
            )

            cleaned_report_layout = await asyncio.to_thread(
                clean_drl_to_clean_rl, descriptive_report_layout
            )
            if not cleaned_report_layout:
                logger.error(
                    f"Error cleaning descriptive report layout for user: {self.user_name} - chat_id: {self.chat_id}"
                )
                return GENERIC_TOOL_ERROR_MSG, {}
            logger.info(
                f"Cleaned descriptive report layout for user: {self.user_name} - chat_id: {self.chat_id}"
            )
            cleaned_report_layout = await asyncio.to_thread(
                modify_report_layout, cleaned_report_layout
            )
            event_writer({"name": "retrieve", "report_layout": cleaned_report_layout})

            return "", {
                "descriptive_report_layout": descriptive_report_layout,
                "report_layout": report_layout,
                "domain_name": domain_name,
            }

        except Exception as e:
            event_writer({"name": "retrieve", "error": e})
            logger.error(f"Error in retrieve: {e}")
            return GENERIC_TOOL_ERROR_MSG, {}

        # Generate search queries based on user instructions and report layout
        # event_writer({"name": "retrieve", "status": "generating_search_queries"})
        # await self._get_search_queries()

        # # Search the internet using Tavily
        # event_writer({"name": "retrieve", "status": "searching_internet"})
        # await self._tavily_internet_search()

        # # Search the knowledge base
        # event_writer({"name": "retrieve", "status": "searching_knowledge_base"})
        # await self._knowledge_base_search()

        # Combine the web and knowledge base search results
        # web_serialized = self.retrieve_config.get('web_serialized', '')
        # kb_serialized = self.retrieve_config.get('kb_serialized', '')
        # retrieved_content = web_serialized + kb_serialized

        # web_docs = self.retrieve_config.get('web_retrieved_docs', [])
        # kb_docs = self.retrieve_config.get('kb_retrieved_docs', [])
        # all_retrieved_docs = web_docs + kb_docs

        # if not all_retrieved_docs:
        #     logger.error(f"No retrieved documents found for user: {self.user_name} - chat_id: {self.chat_id}")
        #     event_writer({"name": "retrieve", "status": "error"})

        # # Store all retrieved docs for later use
        # self.retrieve_config['all_retrieved_docs'] = all_retrieved_docs

        # logger.info(f'Total retrieved documents: {len(all_retrieved_docs)} for user: {self.user_name} - chat_id: {self.chat_id}')
        # return retrieved_content, all_retrieved_docs

    # def _prepare_messages(self, messages: List, system_msg: SystemMessage) -> List:
    #     """Prepare messages by ensuring proper system message placement."""
    #     prepared_messages = []
    #     has_system = False

    #     for msg in messages:
    #         if msg.type == "system":
    #             has_system = True
    #             continue
    #         prepared_messages.append(msg)

    #     # Add system message at the beginning
    #     if has_system:
    #         prepared_messages = [system_msg] + prepared_messages
    #     else:
    #         prepared_messages.insert(0, system_msg)

    #     return prepared_messages

    async def _dispatch_read_tool(self, name: str, args: dict[str, Any]) -> str:
        """Invoke a single read tool (retrieve_latest_info / query_document) and
        return its string result.  Called from the sequential dispatcher so each
        tool's own start/heartbeat/end events stream fully before the next begins.
        """
        # retrieve_latest_info is no longer bound, so it can never be dispatched here.
        # if name == "retrieve_latest_info":
        #     return await self.retrieve_latest_info(
        #         user_query=args.get("user_query", ""),
        #         status_message=args.get("status_message", ""),
        #         progress_updates=args.get("progress_updates"),
        #     )
        if name == "query_document":
            return await self.query_document(
                user_query=args.get("user_query", ""),
                status_message=args.get("status_message", ""),
                progress_updates=args.get("progress_updates"),
            )
        if name == "propose_report_layout":
            return await self.propose_report_layout(
                report_layout=args.get("report_layout", ""),
                report_title=args.get("report_title", ""),
            )
        logger.warning(
            f"[sequential_tools] Unknown read tool '{name}' — returning error stub | "
            f"user: {self.user_name} - chat_id: {self.chat_id}"
        )
        return GENERIC_TOOL_ERROR_MSG

    async def _sequential_tools_node(self, state: MessagesState) -> dict[str, list]:
        """
        Execute the read-tool calls in the last AIMessage STRICTLY one at a time,
        in the order the model emitted them.  Because each call is awaited before
        the next begins, its start/heartbeat/end events stream as a clean,
        non-interleaved block — the user sees tool A's narration fully, then tool
        B's.  Every tool_call_id receives exactly one matching ToolMessage
        (skipped calls get a stub) so provider history stays valid.

        Per-turn caps (MAX_WEB_SEARCH_CALLS_PER_TURN / MAX_QUERY_DOC_CALLS_PER_TURN)
        are enforced as we iterate, honouring all calls up to the limit and
        stubbing the rest.
        """
        messages = state["messages"]
        last_ai: AIMessage | None = next(
            (m for m in reversed(messages) if isinstance(m, AIMessage)), None
        )
        if last_ai is None:
            logger.warning(
                f"[sequential_tools] No AIMessage found in state — nothing to dispatch | "
                f"user: {self.user_name} - chat_id: {self.chat_id}"
            )
            return {"messages": []}

        calls = [_normalize_tool_call(c) for c in (getattr(last_ai, "tool_calls", []) or [])]

        web_used, doc_used = self._count_turn_tool_calls(messages)

        call_names = [c.get("name", "?") for c in calls]
        is_parallel = len(calls) > 1
        logger.info(
            f"[sequential_tools] Dispatching {len(calls)} read-tool call(s) "
            f"sequentially{' (PARALLEL batch from model)' if is_parallel else ''}: "
            f"{call_names} | prior_turn_usage: web={web_used}/{MAX_WEB_SEARCH_CALLS_PER_TURN}, "
            f"doc={doc_used}/{MAX_QUERY_DOC_CALLS_PER_TURN} | "
            f"user: {self.user_name} - chat_id: {self.chat_id}"
        )

        batch_started = time.time()
        ran = 0
        skipped = 0
        errored = 0

        out_messages: list[ToolMessage] = []
        for position, call in enumerate(calls, start=1):
            name = call.get("name", "")
            call_id = call.get("id", "")
            args = call.get("args", {}) or {}
            query_preview = str(args.get("user_query", ""))[:120]

            if name == "retrieve_latest_info" and web_used >= MAX_WEB_SEARCH_CALLS_PER_TURN:
                skipped += 1
                logger.warning(
                    f"[sequential_tools] Skipping call {position}/{len(calls)} "
                    f"retrieve_latest_info — per-turn limit reached "
                    f"({web_used}/{MAX_WEB_SEARCH_CALLS_PER_TURN}) | query='{query_preview}' | "
                    f"user: {self.user_name} - chat_id: {self.chat_id}"
                )
                out_messages.append(
                    ToolMessage(
                        content="Note: the latest-information lookup limit for this turn has been "
                        "reached. Use the information already gathered to answer.",
                        tool_call_id=call_id,
                        name=name,
                    )
                )
                continue

            if name == "query_document" and doc_used >= MAX_QUERY_DOC_CALLS_PER_TURN:
                skipped += 1
                logger.warning(
                    f"[sequential_tools] Skipping call {position}/{len(calls)} "
                    f"query_document — per-turn limit reached "
                    f"({doc_used}/{MAX_QUERY_DOC_CALLS_PER_TURN}) | query='{query_preview}' | "
                    f"user: {self.user_name} - chat_id: {self.chat_id}"
                )
                out_messages.append(
                    ToolMessage(
                        content="Note: the document-lookup limit for this turn has been reached. "
                        "Use the information already gathered to answer.",
                        tool_call_id=call_id,
                        name=name,
                    )
                )
                continue

            logger.info(
                f"[sequential_tools] Running call {position}/{len(calls)} '{name}' | "
                f"query='{query_preview}' | "
                f"user: {self.user_name} - chat_id: {self.chat_id}"
            )
            call_started = time.time()
            try:
                result = await self._dispatch_read_tool(name, args)
                elapsed = time.time() - call_started
                result_len = len(result) if isinstance(result, str) else len(str(result))
                ran += 1
                logger.info(
                    f"[sequential_tools] Completed call {position}/{len(calls)} '{name}' in "
                    f"{elapsed:.1f}s | result_chars={result_len} | "
                    f"user: {self.user_name} - chat_id: {self.chat_id}"
                )
            except Exception as e:
                elapsed = time.time() - call_started
                errored += 1
                logger.error(
                    f"[sequential_tools] Call {position}/{len(calls)} '{name}' raised after "
                    f"{elapsed:.1f}s: {e} | query='{query_preview}' | "
                    f"user: {self.user_name} - chat_id: {self.chat_id}",
                    exc_info=True,
                )
                result = GENERIC_TOOL_ERROR_MSG

            out_messages.append(
                ToolMessage(
                    content=result if isinstance(result, str) else str(result),
                    tool_call_id=call_id,
                    name=name,
                )
            )

            if name == "retrieve_latest_info":
                web_used += 1
            elif name == "query_document":
                doc_used += 1

        batch_elapsed = time.time() - batch_started
        logger.info(
            f"[sequential_tools] Batch complete in {batch_elapsed:.1f}s | "
            f"requested={len(calls)}, ran={ran}, skipped={skipped}, errored={errored} | "
            f"turn_usage_after: web={web_used}/{MAX_WEB_SEARCH_CALLS_PER_TURN}, "
            f"doc={doc_used}/{MAX_QUERY_DOC_CALLS_PER_TURN} | "
            f"user: {self.user_name} - chat_id: {self.chat_id}"
        )
        return {"messages": out_messages}

    def _route_after_tools(self, state: MessagesState) -> str:
        """Route after tools based on which tool was called.

        If retrieve tool was called, go to domain_router.
        If document query tools or info retrieval tool were called, go back to report_or_respond.
        """
        messages = state["messages"]
        last_message = messages[-1]

        if hasattr(last_message, "name"):
            tool_name = last_message.name
            logger.info(
                f"[route_after_tools] Tool completed: tool_name='{tool_name}' | "
                f"domain_name='{self.domain_name}' | "
                f"user: {self.user_name} - chat_id: {self.chat_id}"
            )
            if tool_name == "retrieve":
                logger.info(
                    f"[route_after_tools] Routing to domain_router (domain_name='{self.domain_name}') | "
                    f"user: {self.user_name} - chat_id: {self.chat_id}"
                )
                return "domain_router"
            elif tool_name in ["query_document", "ask_user", "propose_report_layout"]:
                logger.info(
                    f"[route_after_tools] Routing back to report_or_respond after '{tool_name}' | user: {self.user_name} - chat_id: {self.chat_id}"
                )
                return "report_or_respond"
            elif tool_name == "retrieve_latest_info":
                logger.info(
                    f"Routing back to report_or_respond after {tool_name} tool for user: {self.user_name} - chat_id: {self.chat_id}"
                )
                return "report_or_respond"

        logger.warning(
            f"[route_after_tools] No tool name found on last message — defaulting to report_or_respond | user: {self.user_name} - chat_id: {self.chat_id}"
        )
        return "report_or_respond"
