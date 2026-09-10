"""Chat / planning nodes: report_or_respond, respond_during_report, ask_user, chat title."""

import json

from google import genai

# from langchain_aws import ChatBedrock  # Bedrock disabled — using direct Anthropic API instead
from langchain_core.messages import (
    AIMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.tools import tool
from langgraph.config import get_stream_writer

# from langchain_core.messages.base import BaseMessage
# from langchain_tavily import TavilySearch
from langgraph.graph import MessagesState

# from app.research.infographics import process_infographics
# from app.deliverables.helper_functions import generate_image_with_Google, generate_image_with_openai
from app.core.constants import (
    # ANTHROPIC_MODEL_ID,
    # BEDROCK_LLM_ID,
    # BEDROCK_REPORT_LLM,
    # REPORT_LLM,
    ANTHROPIC_MODEL_ID,
    ASYNC_OPENAI_CLIENT,
    CHAT_TITLE_MODEL,
    GEMINI_API_KEY,
    GEMINI_CHAT_TITLE_MODEL,
    MAX_QUERY_DOC_CALLS_PER_TURN,
    OPENAI_CHAT_MODEL_ID,
    OPENAI_LLM_LANGCHAIN,
    STRUCTURED_LLM,
)
from app.core.logging import setup_logging
from app.observability.llm_response_logger import save_raw_llm_response, strip_json_code_fence
from app.research.agent._base import CasperBase
from app.research.agent._helpers import (
    _normalize_tool_call,
)
from app.research.agent._state import (
    _LAYOUT_GUARD_MARKER,
    _RESPOND_DURING_REPORT_NOTE,
    FLAG_REPORT_CHANGE_REQUEST_SCHEMA,
)
from app.research.prompts.prompt_utils import (
    CHAT_TITLE_PROMPT,
)

logger = setup_logging(__name__)


class ChatMixin(CasperBase):
    """Chat / planning nodes: report_or_respond, respond_during_report, ask_user, chat title."""

    async def ask_user(self, questions: list[str]) -> str:
        """Ask the user to pick one option from a list, keeping a human in the loop.

        Use this INSTEAD of asking the user to choose something in plain prose whenever
        the next step depends on a decision only the user can make from a fixed set of
        choices — most importantly the **report domain selection** (Primary Research,
        Due Diligence, Industry Benchmarking, Market Insight, RFP, Business Plan,
        Standard Report), and also confirming brief vs. study, or any other
        mutually-exclusive choice.

        Pass the exact list of options you want the user to see. Each list item is one
        selectable option and is shown to the user verbatim. Calling this emits the
        options to the frontend as selectable choices and ends your turn; the user's
        selection arrives as their next message and the flow then continues. Do NOT
        call ``retrieve`` in the same step as this tool, and after calling it only add
        a short line telling the user to pick one of the options.

        Args:
            questions (List[str]): Ordered list of options/questions to present to the
                user. Shown exactly as given, one selectable option per item.

        Returns:
            str: The option list, echoed back so it stays in the conversation context.
        """
        raw = questions
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except json.JSONDecodeError:
                raw = [raw]
        options = [str(q) for q in (raw or [])]

        logger.info(
            f"[ask_user] Presenting {len(options)} option(s) to the user | "
            f"user: {self.user_name} - chat_id: {self.chat_id}"
        )
        try:
            get_stream_writer()(
                {
                    "name": "ask_user",
                    "status": "options",
                    "questions": options,
                }
            )
        except Exception as e:
            logger.warning(
                f"[ask_user] Failed to emit options event: {e} | "
                f"user: {self.user_name} - chat_id: {self.chat_id}"
            )

        return (
            "Options presented to the user as selectable choices: "
            + json.dumps(options, ensure_ascii=False)
            + "."
        )

    async def report_or_respond(self, state: MessagesState) -> dict[str, list]:
        """Generate tool call to retrieve relevant information for generating a report in markdown according to the user's instructions or respond to the user's query directly."""
        event_writer = get_stream_writer()
        try:
            # Process messages
            messages = list(state["messages"])  # Make a copy to avoid modifying original
            # Tools are invoked without state, so keep the conversation reachable for the
            # ones that need to know what the user asked for.
            self._current_turn_messages = messages

            # Create the retrieve tool with proper decoration
            retrieve_tool = tool(response_format="content_and_artifact")(self.retrieve)

            event_writer({"name": "report_or_respond", "status": "message_stream_start"})
            response = None

            # --- Per-tool call counting (shared by both balance paths) ---
            # Walk backwards to find the last human message, then count how many
            # times each auxiliary tool has already been called in this turn so we
            # can enforce per-tool rate limits consistently regardless of balance.
            last_human_idx = -1
            for idx, m in enumerate(messages):
                if getattr(m, "type", None) == "human":
                    last_human_idx = idx

            current_turn_msgs = messages[last_human_idx + 1 :]

            web_search_count = 0
            query_doc_count = 0
            for m in current_turn_msgs:
                if getattr(m, "type", None) != "tool":
                    continue
                name = getattr(m, "name", "")
                if name == "retrieve_latest_info":
                    web_search_count += 1
                if name == "query_document":
                    query_doc_count += 1

            has_file_ids = self._has_uploaded_documents()

            # All tools are always bound. caspr-api no longer checks a wallet
            # before answering — spend authority moved to caspr-backend, which
            # reserves funds before it ever calls us.
            bound_tools = [retrieve_tool, tool(self.ask_user), tool(self.propose_report_layout)]
            withheld_tools = []

            # retrieve_latest_info tool binding — disabled. The layout refresh
            # (update_proposed_report_layout) is the only web lookup now.
            # if web_search_count < MAX_WEB_SEARCH_CALLS_PER_TURN:
            #     bound_tools.append(tool(self.retrieve_latest_info))
            # else:
            #     withheld_tools.append("retrieve_latest_info")
            #     logger.info(f"retrieve_latest_info limit reached ({web_search_count}/{MAX_WEB_SEARCH_CALLS_PER_TURN}) for user: {self.user_name} - chat_id: {self.chat_id}")

            if has_file_ids and query_doc_count < MAX_QUERY_DOC_CALLS_PER_TURN:
                bound_tools.append(tool(self.query_document))
            elif has_file_ids:
                withheld_tools.append("query_document")
                logger.info(
                    f"query_document limit reached ({query_doc_count}/{MAX_QUERY_DOC_CALLS_PER_TURN}) for user: {self.user_name} - chat_id: {self.chat_id}"
                )

            # After ask_user, the run must pause for the human: the model should
            # only add a short line pointing at the options, never call a tool.
            # (A layout-guard nudge is also a ToolMessage named "ask_user" — it
            # must NOT trigger the pause; the model should keep going and propose
            # the layout.)
            last_msg = messages[-1] if messages else None
            if (
                getattr(last_msg, "type", None) == "tool"
                and getattr(last_msg, "name", "") == "ask_user"
                and _LAYOUT_GUARD_MARKER not in (getattr(last_msg, "content", "") or "")
            ):
                ask_user_note = (
                    "\n\nIMPORTANT — WAITING FOR THE USER'S SELECTION:\n"
                    "You just presented options to the user with the ask_user tool. "
                    "Do NOT call any tool now (especially not retrieve). Reply with a "
                    "single short sentence asking the user to pick one of the options "
                    "shown, then stop and wait for their next message."
                )
                original_sys = messages[0]
                messages[0] = SystemMessage(content=original_sys.content + ask_user_note)

            # Before any layout has been proposed, the first tool call on a report
            # request MUST be propose_report_layout — never ask_user or retrieve.
            # This keeps the layout-first flow intact (the layout drives the
            # background refresh and the re-proposal after the user's answers).
            if not self._layout_ever_proposed(messages):
                first_call_note = (
                    "\n\nIMPORTANT — PROPOSE THE LAYOUT FIRST:\n"
                    "No report layout has been proposed in this conversation yet. "
                    "If the user is asking for a report, your FIRST tool call MUST "
                    "be `propose_report_layout` with the full Markdown layout. Do "
                    "NOT call `ask_user` or `retrieve` yet. Propose a sensible "
                    "default layout now (a Standard Report study layout unless the "
                    "user clearly asked for a specific domain or a brief); "
                    "domain-selection and refinement questions come afterward, "
                    "once the layout has been shown. Write one short sentence to "
                    "the user first announcing that you are putting a layout "
                    "together now (e.g. 'Let me put a layout together for that.'), "
                    "then make the call — never call it silently. Do not word that "
                    "line as if the layout is already on screen; it is not shown "
                    "until after the call."
                )
                original_sys = messages[0]
                messages[0] = SystemMessage(content=original_sys.content + first_call_note)

            if withheld_tools:
                limit_note = (
                    f"\n\nIMPORTANT — TOOL USAGE LIMIT REACHED:\n"
                    f"The following tools are NO LONGER AVAILABLE for this message "
                    f"because you have already used them the maximum number of times: "
                    f"{', '.join(withheld_tools)}.\n"
                    f"Do NOT attempt to call these tools. Use the information you have "
                    f"already gathered from previous tool calls to formulate your response."
                )
                original_sys = messages[0]
                messages[0] = SystemMessage(content=original_sys.content + limit_note)

            logger.info(
                f"[normal_flow] Invoking LLM with all tools for user: {self.user_name} - chat_id: {self.chat_id}. "
                f"Bound tools: {[t.name for t in bound_tools]}, Withheld tools: {withheld_tools}"
            )

            try:
                # raise Exception("test")
                logger.info(
                    f"[normal_flow] Using anthropic llm for user: {self.user_name} - chat_id: {self.chat_id}"
                )
                llm_with_tools = self.anthropic_llm.bind_tools(bound_tools)
                response = await llm_with_tools.ainvoke(messages)
                save_raw_llm_response(
                    response,
                    ANTHROPIC_MODEL_ID,
                    "Planning and answering the main research request",
                    self.chat_id,
                    user_id=self.user_id or self.user_name,
                )
                # logger.info(f"[normal_flow] Using bedrock llm for user: {self.user_name} - chat_id: {self.chat_id}")
                # llm_with_tools = LLM.bind_tools(bound_tools)
                # response = await llm_with_tools.ainvoke(messages)
            except Exception as e:
                logger.error(
                    f"[normal_flow] Anthropic failed: {e}, falling back to openai for user: {self.user_name} - chat_id: {self.chat_id}"
                )
                try:
                    logger.info(
                        f"[normal_flow] Using openai llm for user: {self.user_name} - chat_id: {self.chat_id}"
                    )
                    llm_with_tools = OPENAI_LLM_LANGCHAIN.bind_tools(bound_tools)
                    response = await llm_with_tools.ainvoke(messages)
                    save_raw_llm_response(
                        response,
                        OPENAI_CHAT_MODEL_ID,
                        "Planning and answering the main research request (backup)",
                        self.chat_id,
                        user_id=self.user_id or self.user_name,
                    )
                except Exception as e:
                    logger.error(
                        f"[normal_flow] All LLM providers failed for user: {self.user_name} - chat_id: {self.chat_id}. Last error: {e}"
                    )
                    raise Exception(f"All LLM providers failed. Last error: {e}")

            event_writer({"name": "report_or_respond", "status": "message_stream_complete"})

            # When this reply closes the turn, the graph is about to end and the stream
            # writer goes with it — so wait out whatever is left of the layout refresh
            # that has been running in the background since the layout was proposed.
            new_messages = [response]
            if not getattr(response, "tool_calls", None):
                await self._await_layout_refresh()
                patched_layout_message = self._patched_layout_tool_message(messages)
                if patched_layout_message is not None:
                    new_messages.append(patched_layout_message)

            return {"messages": new_messages}
        except Exception as e:
            logger.error(
                f"Error in report_or_respond: {e} for user: {self.user_name} - chat_id: {self.chat_id}"
            )
            # Return error message
            error_msg = AIMessage(
                content="I apologize, but I encountered an error while processing your request."
            )
            event_writer({"name": "report_or_respond", "status": "error"})
            return {"messages": [error_msg]}

    def _extract_in_progress_layout(self, messages: list) -> str:
        """Best-effort: pull the proposed report layout out of the chat history so
        `respond_during_report` can name the sections the user is asking about.
        Returns "" when nothing usable is found."""
        target = next(
            (
                m
                for m in reversed(messages or [])
                if getattr(m, "type", None) == "tool"
                and getattr(m, "name", "") == "propose_report_layout"
            ),
            None,
        )
        content = getattr(target, "content", "") or "" if target is not None else ""
        if isinstance(content, list):
            content = " ".join(
                part.get("text", "") if isinstance(part, dict) else str(part) for part in content
            )
        return content.strip()[:6000]

    async def respond_during_report(self, state: MessagesState) -> dict[str, list]:
        """Respond to a follow-up chat message while a report is still being generated.

        Entered from START (instead of `report_or_respond`) whenever
        `self.report_in_progress` is set. Binds NO report tools — it cannot start,
        restart or re-plan a report. Its only tool is `flag_report_change_request`,
        used to capture changes the user asks for in the in-progress report; each
        captured request is emitted as a `post_report_edit_request` custom event
        and is applied downstream (via ask-caspr), not here.
        """
        event_writer = get_stream_writer()
        try:
            messages = list(state["messages"])
            self._current_turn_messages = messages

            event_writer({"name": "respond_during_report", "status": "message_stream_start"})

            layout = self._extract_in_progress_layout(messages)
            note = _RESPOND_DURING_REPORT_NOTE
            if self.in_progress_report_title:
                note += (
                    f'\nThe report being generated is titled: "{self.in_progress_report_title}".\n'
                )
            if layout:
                note += (
                    "\nFor reference, the approved report layout being built is:\n"
                    f"-----\n{layout}\n-----\n"
                )

            if messages and getattr(messages[0], "type", None) == "system":
                messages = [SystemMessage(content=messages[0].content + note)] + messages[1:]
            else:
                messages = [SystemMessage(content=note)] + messages

            async def _invoke(msgs, tools):
                """anthropic → openai fallback, mirroring report_or_respond."""
                try:
                    llm = self.anthropic_llm.bind_tools(tools) if tools else self.anthropic_llm
                    resp = await llm.ainvoke(msgs)
                    save_raw_llm_response(
                        resp,
                        ANTHROPIC_MODEL_ID,
                        "Responding while the report is generating",
                        self.chat_id,
                        user_id=self.user_id or self.user_name,
                    )
                    return resp
                except Exception as e:
                    logger.error(
                        f"[respond_during_report] Anthropic failed: {e}, falling back to openai "
                        f"for user: {self.user_name} - chat_id: {self.chat_id}"
                    )
                    llm = OPENAI_LLM_LANGCHAIN.bind_tools(tools) if tools else OPENAI_LLM_LANGCHAIN
                    resp = await llm.ainvoke(msgs)
                    save_raw_llm_response(
                        resp,
                        OPENAI_CHAT_MODEL_ID,
                        "Responding while the report is generating (backup)",
                        self.chat_id,
                        user_id=self.user_id or self.user_name,
                    )
                    return resp

            response = await _invoke(messages, [FLAG_REPORT_CHANGE_REQUEST_SCHEMA])

            change_calls = [
                _normalize_tool_call(tc)
                for tc in (getattr(response, "tool_calls", None) or [])
                if (tc.get("name") if isinstance(tc, dict) else getattr(tc, "name", None))
                == "flag_report_change_request"
            ]

            if not change_calls:
                event_writer({"name": "respond_during_report", "status": "message_stream_complete"})
                return {"messages": [response]}

            # Capture each requested change and emit it for downstream application.
            tool_messages: list = []
            total_captured = 0
            for tc in change_calls:
                args = tc.get("args") or {}
                requests = args.get("requests") or []
                if isinstance(requests, (str, dict)):
                    requests = [requests]
                recorded = 0
                for req in requests:
                    raw_user_message = (
                        req if isinstance(req, str) else (req or {}).get("raw_user_message", "")
                    ).strip()
                    if not raw_user_message:
                        continue
                    total_captured += 1
                    recorded += 1
                    event_writer(
                        {
                            "name": "post_report_edit_request",
                            "status": "captured",
                            "chat_id": self.chat_id,
                            "report_id": self.in_progress_report_id,
                            "request": raw_user_message,
                        }
                    )
                tool_messages.append(
                    ToolMessage(
                        content=(
                            f"Recorded {recorded} report change request(s). They are queued "
                            f"to be applied to the report that is generating. Now give the user a "
                            f"one or two sentence confirmation — do not claim it is already done."
                        ),
                        tool_call_id=tc.get("id") or "",
                        name="flag_report_change_request",
                    )
                )

            logger.info(
                f"[respond_during_report] Captured {total_captured} report change request(s) | "
                f"report_id={self.in_progress_report_id} | user: {self.user_name} - chat_id: {self.chat_id}"
            )

            follow_up = await _invoke(messages + [response] + tool_messages, [])
            event_writer({"name": "respond_during_report", "status": "message_stream_complete"})
            return {"messages": [response, *tool_messages, follow_up]}

        except Exception as e:
            logger.error(
                f"Error in respond_during_report: {e} for user: {self.user_name} - chat_id: {self.chat_id}"
            )
            event_writer({"name": "respond_during_report", "status": "error"})
            return {
                "messages": [
                    AIMessage(
                        content="I ran into a problem replying just now — your report is still being generated. Please try again in a moment."
                    )
                ]
            }

    def _route_entry(self, state: MessagesState) -> str:
        """Conditional entry point: send follow-up messages to
        `respond_during_report` while a report is generating, otherwise run the
        normal `report_or_respond` planner."""
        if self.report_in_progress:
            logger.info(
                f"[route_entry] Report in progress — routing to respond_during_report | "
                f"report_id={self.in_progress_report_id} | user: {self.user_name} - chat_id: {self.chat_id}"
            )
            return "respond_during_report"
        return "report_or_respond"

    async def generate_chat_title(self, user_query: str, ai_response: str) -> str:
        """Generate a chat title based on the first exchange between a user and Caspr."""
        logger.info(f"Generating chat title for user: {self.user_name} - chat_id: {self.chat_id}")
        chat_title_schema = {
            "type": "function",
            "function": {
                "name": "create_chat_title",
                "description": "Generates a short and meaningful chat title (max 40 characters) based on a conversation in English only",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "title": {
                            "type": "string",
                            "description": "Concise title summarizing the conversation (max 40 characters) in English only",
                        }
                    },
                    "required": ["title"],
                },
            },
        }
        try:
            # raise Exception("test")
            title_prompt = ChatPromptTemplate.from_template(template=CHAT_TITLE_PROMPT)
            title_prompt = title_prompt.format(
                user_query=user_query, ai_response=ai_response[:500] + "..."
            )

            response = await ASYNC_OPENAI_CLIENT.chat.completions.create(
                model=CHAT_TITLE_MODEL,
                messages=[{"role": "user", "content": title_prompt}],
                tools=[chat_title_schema],
                tool_choice={"type": "function", "function": {"name": "create_chat_title"}},
            )
            save_raw_llm_response(
                response,
                CHAT_TITLE_MODEL,
                "create_chat_title",
                self.chat_id,
                user_id=self.user_id or self.user_name,
            )

            tool_call = response.choices[0].message.tool_calls[0]
            structured_json = json.loads(tool_call.function.arguments)
            title = structured_json.get("title", "")
            if not title:
                raise Exception("No title generated")

            return title

        except Exception as e:
            logger.error(f"Error generating chat title using openai strict json: {e}")
            logger.info("Falling back to pydantic structured output.")

            try:
                title_prompt = ChatPromptTemplate.from_template(template=CHAT_TITLE_PROMPT)
                title_prompt = title_prompt.format(
                    user_query=user_query, ai_response=ai_response[:50] + "..."
                )
                title_response = await STRUCTURED_LLM.ainvoke(title_prompt)
                save_raw_llm_response(
                    title_response["raw"],
                    ANTHROPIC_MODEL_ID,
                    "Creating a title for the chat",
                    self.chat_id,
                    user_id=self.user_id or self.user_name,
                )
                if title_response["parsing_error"] is not None:
                    raise title_response["parsing_error"]
                return title_response["parsed"].chat_title
            except Exception as e:
                logger.error(f"Error generating chat title: {e}")
                logger.info("Falling back to simple naive title generation prompt.")
                try:
                    fallback_response = await ASYNC_OPENAI_CLIENT.chat.completions.create(
                        model=CHAT_TITLE_MODEL,
                        messages=[
                            {"role": "system", "content": "Answer in 5 words or less."},
                            {
                                "role": "user",
                                "content": f"Create a chat title for this chat conversation between a user and a report generator AI assitant \nUser: {user_query}\nAssistant: {ai_response[:50] + '...'}",
                            },
                        ],
                        tools=[chat_title_schema],
                        tool_choice={"type": "function", "function": {"name": "create_chat_title"}},
                    )
                    save_raw_llm_response(
                        fallback_response,
                        CHAT_TITLE_MODEL,
                        "create_chat_title_backup",
                        self.chat_id,
                        user_id=self.user_id or self.user_name,
                    )

                    tool_call = fallback_response.choices[0].message.tool_calls[0]
                    structured_json = json.loads(tool_call.function.arguments)
                    title_fallback_response = (
                        structured_json.get("title", "").replace("'", "").replace('"', "").strip()
                    )
                    if not title_fallback_response:
                        raise Exception("No title generated")
                    return title_fallback_response

                    # fallback_prompt_messages = [SystemMessage(content="Answer in 5 words or less.")]
                    # title_fallback_response = await LLM.ainvoke(fallback_prompt_messages + [
                    #     HumanMessage(content=f"Create a chat title for this chat conversation between a user and a report generator AI assitant \nUser: {user_query}\nAssistant: {ai_response[:50]+'...'}")
                    # ]
                    # )
                    # title_fallback_response = title_fallback_response.content[0]['text'].replace("'", "").replace('"','').strip()
                    # return title_fallback_response
                except Exception as e:
                    logger.error(f"Error generating chat title in fallback: {e}")
                    logger.info("Falling back to Gemini API for chat title.")
                    try:
                        chat_title_schema_gemini = {
                            "type": "object",
                            "properties": {
                                "title": {
                                    "type": "string",
                                    "description": "Concise title summarizing the conversation (max 40 characters) in English only",
                                }
                            },
                            "required": ["title"],
                        }
                        gemini_title_prompt = f"Answer in 5 words or less.\nCreate a chat title for this chat conversation between a user and a report generator AI assitant \nUser: {user_query}\nAssistant: {ai_response[:50] + '...'}"
                        gemini_client = genai.Client(api_key=GEMINI_API_KEY)
                        interaction = gemini_client.interactions.create(
                            model=GEMINI_CHAT_TITLE_MODEL,
                            input=gemini_title_prompt,
                            response_format={
                                "type": "text",
                                "mime_type": "application/json",
                                "schema": chat_title_schema_gemini,
                            },
                            generation_config={
                                "temperature": 0.1,
                                "thinking_config": {"thinking_budget": 0},
                            },
                        )
                        save_raw_llm_response(
                            interaction,
                            GEMINI_CHAT_TITLE_MODEL,
                            "create_chat_title_backup_gemini",
                            self.chat_id,
                            user_id=self.user_id or self.user_name,
                        )
                        result = json.loads(strip_json_code_fence(interaction.output_text))
                        gemini_title = (
                            (result.get("title") or "").replace("'", "").replace('"', "").strip()
                        )
                        if not gemini_title:
                            raise Exception("No title generated by Gemini")
                        return gemini_title
                    except Exception as gemini_e:
                        logger.error(
                            f"Error generating chat title with Gemini fallback: {gemini_e}"
                        )
                        return ai_response[:30]
