"""LangGraph wiring: gate, routing, domain dispatch, graph build, pause/resume, get_processing_state."""

from typing import Any, AsyncGenerator

# from langchain_aws import ChatBedrock  # Bedrock disabled — using direct Anthropic API instead
from langchain_core.messages import (
    AIMessage,
    ToolMessage,
)
from langchain_core.tools import tool

# from langchain_core.messages.base import BaseMessage
# from langchain_tavily import TavilySearch
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode
from langgraph.types import Command

from app.core.checkpointer import get_checkpointer
from app.core.logging import setup_logging
from app.research.agent._base import CasperBase
from app.research.agent._helpers import (
    _keep_finished_chat_history,
    _normalize_tool_call,
)
from app.research.agent._state import (
    _LAYOUT_GUARD_MARKER,
    CasperState,
)
from app.research.domains.due_diligence import build_due_diligence_subgraph

# from app.research.infographics import process_infographics
# from app.deliverables.helper_functions import generate_image_with_Google, generate_image_with_openai
from app.research.domains.primary_research import build_primary_research_subgraph

logger = setup_logging(__name__)


class GraphMixin(CasperBase):
    """LangGraph wiring: gate, routing, domain dispatch, graph build, pause/resume, get_processing_state."""

    def _count_turn_tool_calls(self, messages: list) -> tuple[int, int]:
        """Count how many web-search / document-query ToolMessages have already
        been produced in the CURRENT turn (since the last human message).

        Mirrors the counting logic used in ``report_or_respond`` so the
        sequential dispatcher can enforce the same per-turn caps as it iterates
        over a multi-call AIMessage.
        """
        last_human_idx = -1
        for idx, m in enumerate(messages):
            if getattr(m, "type", None) == "human":
                last_human_idx = idx

        web_used = 0
        doc_used = 0
        for m in messages[last_human_idx + 1 :]:
            if getattr(m, "type", None) != "tool":
                continue
            name = getattr(m, "name", "")
            if name == "retrieve_latest_info":
                web_used += 1
            elif name == "query_document":
                doc_used += 1
        return web_used, doc_used

    @staticmethod
    def _layout_ever_proposed(messages: list) -> bool:
        """True once `propose_report_layout` has produced a ToolMessage in this
        conversation — i.e. the user has been shown at least one layout."""
        return any(
            getattr(m, "type", None) == "tool" and getattr(m, "name", "") == "propose_report_layout"
            for m in messages
        )

    def _gate_node(self, state: MessagesState) -> dict[str, list]:
        """
        Pre-execution gate — classifies the assistant's tool step and enforces
        the exclusivity rules we care about: ``ask_user`` (human-in-the-loop
        choice) and report generation (``retrieve``) must each run alone.

        Behaviour:
          - ``ask_user`` / ``retrieve`` before any ``propose_report_layout`` has
            run: suppress the call(s) and answer them with a nudge ToolMessage so
            the model proposes the layout first (bounded to 2 nudges/turn, then
            fails open). ``query_document`` is allowed through.
          - 0 or 1 tool calls: pass through unchanged.
          - 2+ tool calls that INCLUDE ``ask_user``: keep ONLY the first
            ``ask_user`` call so the run pauses for the user's selection.
          - 2+ tool calls, NONE of which is ``retrieve``: pass through unchanged
            so the sequential dispatcher can honour every call one-at-a-time
            (each tool_call_id still gets a matching ToolMessage downstream, so
            provider history stays valid).
          - 2+ tool calls that INCLUDE ``retrieve``: report generation is
            exclusive — rewrite the AIMessage (same id so add_messages overwrites
            in place) to keep ONLY the first ``retrieve`` call and drop the rest.

        Routing (web/doc sequential vs retrieve) is decided in ``_route_after_gate``.
        """
        messages = state["messages"]
        last_ai: AIMessage | None = next(
            (m for m in reversed(messages) if isinstance(m, AIMessage)), None
        )

        if last_ai is None:
            return {"messages": []}

        tool_calls = [_normalize_tool_call(c) for c in (getattr(last_ai, "tool_calls", []) or [])]

        if not tool_calls:
            return {"messages": []}

        # --- Layout-first ordering guard --------------------------------------
        # On a new report request the first layout/report action must be
        # propose_report_layout. If the model jumps to ask_user (domain
        # questions) or retrieve (report generation) before any layout has been
        # proposed, don't execute those calls — answer each with a nudge so the
        # model proposes the layout first. query_document is fine (reading an
        # uploaded doc informs the layout). Bounded to 2 nudges per turn so a
        # stubborn model can never hang the graph.
        _GUARD_TOOLS = {"ask_user", "retrieve"}
        premature = [c for c in tool_calls if c.get("name") in _GUARD_TOOLS]
        if premature and not self._layout_ever_proposed(messages):
            nudges_so_far = sum(
                1
                for m in messages
                if getattr(m, "type", None) == "tool"
                and _LAYOUT_GUARD_MARKER in (getattr(m, "content", "") or "")
            )
            names = ", ".join(c.get("name", "?") for c in tool_calls)
            if nudges_so_far < 2:
                logger.warning(
                    f"[gate] '{names}' called before propose_report_layout — "
                    f"suppressing and nudging (attempt {nudges_so_far + 1}/2) | "
                    f"user: {self.user_name} - chat_id: {self.chat_id}"
                )
                nudge = (
                    f"{_LAYOUT_GUARD_MARKER} This call was NOT executed. No report "
                    f"layout has been proposed yet. Your first action on a report "
                    f"request must be a `propose_report_layout` call carrying the "
                    f"full Markdown layout. Propose a sensible default layout now "
                    f"(a Standard Report study layout unless the user clearly asked "
                    f"for a specific domain or a brief). Ask domain-selection and "
                    f"refinement questions with `ask_user` on a later step, after "
                    f"the layout has been shown."
                )
                return {
                    "messages": [
                        ToolMessage(
                            tool_call_id=c["id"],
                            name=c.get("name", ""),
                            content=nudge,
                        )
                        for c in tool_calls
                    ]
                }
            logger.warning(
                f"[gate] layout-first guard exhausted ({nudges_so_far} nudges) — "
                f"letting '{names}' through | "
                f"user: {self.user_name} - chat_id: {self.chat_id}"
            )

        if len(tool_calls) <= 1:
            return {"messages": []}

        # ask_user is exclusive and takes priority: if the model asked the user to
        # choose AND requested other tools in the same step, keep only the first
        # ask_user call so the run pauses for the human before anything else runs.
        ask_user_calls = [c for c in tool_calls if c.get("name") == "ask_user"]
        if ask_user_calls:
            names = ", ".join(c.get("name", "?") for c in tool_calls)
            logger.warning(
                f"[gate] ask_user is exclusive — collapsing {len(tool_calls)}-tool step "
                f"({names}) to a single ask_user call | "
                f"user: {self.user_name} - chat_id: {self.chat_id}"
            )
            return {
                "messages": [
                    AIMessage(
                        content=last_ai.content,
                        id=last_ai.id,
                        tool_calls=[ask_user_calls[0]],
                    )
                ]
            }

        retrieve_calls = [c for c in tool_calls if c.get("name") == "retrieve"]
        if not retrieve_calls:
            # All read tools — honour them all, run sequentially downstream.
            names = ", ".join(c.get("name", "?") for c in tool_calls)
            logger.info(
                f"[gate] Model requested {len(tool_calls)} parallel read-tool calls "
                f"({names}) — passing through to sequential dispatcher | "
                f"user: {self.user_name} - chat_id: {self.chat_id}"
            )
            return {"messages": []}

        # retrieve is exclusive: keep only the first retrieve call, drop the rest.
        names = ", ".join(c.get("name", "?") for c in tool_calls)
        logger.warning(
            f"[gate] retrieve is exclusive — collapsing {len(tool_calls)}-tool step "
            f"({names}) to a single retrieve call | "
            f"user: {self.user_name} - chat_id: {self.chat_id}"
        )
        collapsed_ai = AIMessage(
            content=last_ai.content,
            id=last_ai.id,
            tool_calls=[retrieve_calls[0]],
        )
        return {"messages": [collapsed_ai]}

    def _route_after_report_or_respond(self, state: MessagesState) -> str:
        """
        Conditional edge leaving report_or_respond — two outcomes:
          - "gate"             : last AI message has tool calls → classify the step.
          - END                : no tool calls → final answer, graph ends here.
        """
        messages = state["messages"]
        last_ai: AIMessage | None = next(
            (m for m in reversed(messages) if isinstance(m, AIMessage)), None
        )
        if last_ai is not None and getattr(last_ai, "tool_calls", []):
            return "gate"
        return END

    def _route_after_gate(self, state: MessagesState) -> str:
        """
        Conditional edge after gate — three outcomes based on the (possibly
        collapsed) last AIMessage:
          - "tools"            : the step contains ``retrieve`` or ``ask_user`` →
                                 prebuilt ToolNode (retrieve handles the
                                 content_and_artifact tuple; ask_user emits the
                                 option list and loops back to close the turn).
          - "sequential_tools" : one or more read tools (retrieve_latest_info /
                                 query_document) → run them sequentially with
                                 per-call heartbeats.
          - "report_or_respond": no tool calls remain → loop back to the LLM.
        """
        messages = state["messages"]
        last_ai: AIMessage | None = next(
            (m for m in reversed(messages) if isinstance(m, AIMessage)), None
        )
        if last_ai is None:
            return "report_or_respond"

        tool_calls = getattr(last_ai, "tool_calls", []) or []
        if not tool_calls:
            return "report_or_respond"

        def _name(c):
            return c.get("name") if isinstance(c, dict) else getattr(c, "name", None)

        def _id(c):
            return c.get("id") if isinstance(c, dict) else getattr(c, "id", None)

        # The layout-first guard in _gate_node may have answered the pending tool
        # calls with nudge ToolMessages instead of executing them. When every
        # pending call already has a response, don't dispatch again — loop back so
        # the model can propose the layout.
        ai_idx = next(
            (i for i in range(len(messages) - 1, -1, -1) if messages[i] is last_ai),
            len(messages) - 1,
        )
        answered = {
            getattr(m, "tool_call_id", None)
            for m in messages[ai_idx + 1 :]
            if getattr(m, "type", None) == "tool"
        }
        if all(_id(c) in answered for c in tool_calls):
            return "report_or_respond"

        # Retrieval is expensive and effectively irreversible, so it is the one
        # place a real pause is worth having: mint the second tier's layout, then
        # wait for the user's configuration before any of it runs. This branch
        # comes first so `ask_user` — which keeps its simulated pause — still
        # goes straight to the ToolNode.
        if any(_name(c) == "retrieve" for c in tool_calls):
            return "layout_pair"

        # ask_user is handled by the prebuilt ToolNode.
        if any(_name(c) == "ask_user" for c in tool_calls):
            return "tools"
        return "sequential_tools"

    async def domain_router(self, state: MessagesState) -> MessagesState:
        """Pass-through node that sits between tools and the domain-specific
        subgraph.  The actual routing decision is made by ``_route_by_domain``
        on the conditional edge leaving this node.

        Note: This node intentionally performs no DB writes. The ``domain_name``
        is emitted via the ``retrieve`` event in ``retrieve()`` and the API
        layer is responsible for persisting it to the ``reports`` table."""
        logger.info(
            f"[domain_router] Entered domain router | domain_name='{self.domain_name}' | "
            f"available_domains=['default', 'primary_research', 'due_diligence', "
            f"'industry_benchmarking', 'market_insight', 'rfp', 'business_plan'] | "
            f"retrieve_config_keys={list(self.retrieve_config.keys())} | "
            f"user: {self.user_name} - chat_id: {self.chat_id}"
        )
        return state

    def _route_by_domain(self, state: MessagesState) -> str:
        """Conditional edge function: dispatch to the correct domain subgraph
        based on ``self.domain_name``.

        Specialized domains (primary_research, due_diligence) have their own
        subgraphs. All other domains (industry_benchmarking, market_insight,
        rfp, business_plan, default) use the standard report pipeline."""
        domain = self.domain_name
        if domain == "primary_research":
            logger.info(
                f"[domain_router] Dispatching to PRIMARY_RESEARCH subgraph | "
                f"has_upload_files={self._has_uploaded_documents()} | "
                f"user: {self.user_name} - chat_id: {self.chat_id}"
            )
            return "primary_research"
        if domain == "due_diligence":
            logger.info(
                f"[domain_router] Dispatching to DUE_DILIGENCE subgraph | "
                f"entity='{self.retrieve_config.get('report_title', 'N/A')}' | "
                f"user: {self.user_name} - chat_id: {self.chat_id}"
            )
            return "due_diligence"
        logger.info(
            f"[domain_router] Dispatching to DEFAULT report generation pipeline "
            f"(domain_name='{domain}') | "
            f"user: {self.user_name} - chat_id: {self.chat_id}"
        )
        return "default"

    async def _build_graph(self):
        """Build the LangGraph workflow."""
        try:
            graph_builder = StateGraph(CasperState)

            retrieve_tool = tool(response_format="content_and_artifact")(self.retrieve)
            query_doc_tool = tool(self.query_document)
            # web_search_tool = tool(self.retrieve_latest_info)  # disabled — see update_proposed_report_layout
            ask_user_tool = tool(self.ask_user)
            propose_layout_tool = tool(self.propose_report_layout)

            graph_builder.add_node("report_or_respond", self.report_or_respond)
            # Respond-only node used while a report is generating for this chat.
            graph_builder.add_node("respond_during_report", self.respond_during_report)
            if not self._has_uploaded_documents():
                tools = [retrieve_tool, ask_user_tool, propose_layout_tool]
            else:
                tools = [retrieve_tool, query_doc_tool, ask_user_tool, propose_layout_tool]
            graph_builder.add_node("tools", ToolNode(tools))
            graph_builder.add_node("sequential_tools", self._sequential_tools_node)
            graph_builder.add_node("generate_report", self.generate_report)
            graph_builder.add_node("domain_router", self.domain_router)

            self._pr_subgraph = build_primary_research_subgraph()
            graph_builder.add_node("pr_subgraph", self.run_primary_research)

            self._dd_subgraph = build_due_diligence_subgraph()
            graph_builder.add_node("dd_subgraph", self.run_due_diligence)

            logger.info(
                f"[build_graph] Domain subgraphs registered: "
                f"['default/industry_benchmarking/market_insight/rfp/business_plan' -> generate_report, "
                f"'primary_research' -> pr_subgraph, 'due_diligence' -> dd_subgraph] | "
                f"tools_registered={['retrieve', 'query_document', 'ask_user', 'propose_report_layout'] if self._has_uploaded_documents() else ['retrieve', 'ask_user', 'propose_report_layout']} | "
                f"user: {self.user_name} - chat_id: {self.chat_id}"
            )

            # Conditional entry: while a report is generating for this chat, a
            # follow-up message goes to `respond_during_report` (chat only, no
            # report tools); otherwise the normal planner runs.
            graph_builder.add_conditional_edges(
                START,
                self._route_entry,
                {
                    "report_or_respond": "report_or_respond",
                    "respond_during_report": "respond_during_report",
                },
            )
            graph_builder.add_edge("respond_during_report", END)

            # report_or_respond routes to gate when tool calls are present,
            # or directly to END when the LLM produces a final answer.
            # gate classifies the step: it collapses a retrieve step to a single
            # call (report generation is exclusive) and otherwise leaves read-tool
            # calls intact.  _route_after_gate then dispatches:
            #   - retrieve  -> "tools" (prebuilt ToolNode, handles the artifact)
            #   - read tools-> "sequential_tools" (run one-at-a-time with heartbeats)
            # The graph therefore always ends on report_or_respond, never on gate.
            graph_builder.add_node("gate", self._gate_node)

            # A retrieve step detours through these two before reaching `tools`:
            # `layout_pair` mints the sibling tier's layout and runs once,
            # `report_config` holds the interrupt and re-runs on every resume.
            # Splitting them is what keeps a resume from re-minting the pair.
            graph_builder.add_node("layout_pair", self.layout_pair)
            graph_builder.add_node("report_config", self.report_config)
            graph_builder.add_edge("layout_pair", "report_config")
            graph_builder.add_edge("report_config", "tools")

            graph_builder.add_conditional_edges(
                "report_or_respond",
                self._route_after_report_or_respond,
                {"gate": "gate", END: END},
            )
            graph_builder.add_conditional_edges(
                "gate",
                self._route_after_gate,
                {
                    "layout_pair": "layout_pair",
                    "tools": "tools",
                    "sequential_tools": "sequential_tools",
                    "report_or_respond": "report_or_respond",
                },
            )

            # Read tools always loop back to report_or_respond so the model can
            # reason over the gathered results (and optionally search again).
            graph_builder.add_edge("sequential_tools", "report_or_respond")

            # Route after tools: retrieve -> domain_router, others (incl. ask_user)
            # -> loop back to report_or_respond so the model closes the turn.
            graph_builder.add_conditional_edges(
                "tools",
                self._route_after_tools,
                {"domain_router": "domain_router", "report_or_respond": "report_or_respond"},
            )

            # Domain router dispatches to the correct subgraph
            graph_builder.add_conditional_edges(
                "domain_router",
                self._route_by_domain,
                {
                    "default": "generate_report",
                    "primary_research": "pr_subgraph",
                    "due_diligence": "dd_subgraph",
                },
            )

            graph_builder.add_edge("generate_report", END)
            graph_builder.add_edge("pr_subgraph", END)
            graph_builder.add_edge("dd_subgraph", END)

            # The checkpointer is what makes `report_config`'s interrupt durable:
            # the turn that pauses ends, and a later request resumes the same
            # thread. Without it `interrupt()` cannot run at all.
            return graph_builder.compile(checkpointer=get_checkpointer())

        except Exception as e:
            logger.error(
                f"Error building graph: {e} for user: {self.user_name} - chat_id: {self.chat_id}"
            )
            raise

    def graph_config(self) -> dict[str, Any]:
        """Checkpointer config for this turn.

        The thread is per-turn, not per-chat (KTD4): `get_processing_state`
        rebuilds the whole conversation every turn, so a chat-scoped thread
        would stack that rebuild on top of checkpointed history and the
        `messages` reducer would duplicate every message.
        """
        return {"configurable": {"thread_id": self.turn_thread_id}}

    async def paused_report_or_respond_update(self) -> dict[str, Any] | None:
        """The `report_or_respond` update the pause cut short, read back.

        `report_or_respond` completed before the interrupt, so a resume never
        re-runs it and never re-emits its update. The consumer of that update
        is the one that creates the report row and reserves tokens, and doing
        that on the paused turn would leave a report in progress for a user who
        never confirms. Replaying it on resume puts the work where the user's
        confirmation is. Returns None when the thread is not parked on a
        retrieve step.
        """
        state = await self.graph.aget_state(self.graph_config())
        messages = list((state.values or {}).get("messages") or [])
        if not self._pending_retrieve_call(messages):
            return None
        last_ai = next((m for m in reversed(messages) if isinstance(m, AIMessage)), None)
        if last_ai is None:
            return None
        return {"report_or_respond": {"messages": [last_ai]}}

    async def resume_report_config(
        self, submission: dict[str, Any]
    ) -> AsyncGenerator[tuple[str, Any]]:
        """Resume a paused turn with the user's confirmed configuration (R3, R4).

        Carries no conversation input at all — only the resume command against
        the paused thread — which is what makes the resume free of a replay.
        The one thing replayed is the update above, so a consumer sees the same
        sequence of events an uninterrupted turn would have produced.
        """
        self._resuming_report_config = True
        logger.info(
            f"[report_config] Resuming thread_id={self.turn_thread_id} | "
            f"user: {self.user_name} - chat_id: {self.chat_id}"
        )

        replay = await self.paused_report_or_respond_update()
        if replay is not None:
            yield "updates", replay

        async for chunk in self.graph.astream(
            Command(resume=submission),
            config=self.graph_config(),
            stream_mode=["messages", "values", "updates", "custom"],
        ):
            yield chunk

    async def get_processing_state(self, query: str) -> AsyncGenerator[tuple[str, dict]]:
        """Returns the state to be processed"""
        try:
            chat_messages = []

            if not query or not isinstance(query, str):
                logger.error("Invalid query provided")
                return {"error": "Invalid query provided"}

            input_message = {"type": "human", "content": query.strip()}

            if not self.user_previous_messages:
                # is_first_query = True
                chat_messages = [self.system_message.model_dump()] + [input_message]
                logger.info(
                    f"Starting new chat for user: {self.user_name} - chat_id: {self.chat_id}"
                )
            else:
                before = len(self.user_previous_messages)
                self.user_previous_messages = _keep_finished_chat_history(
                    self.user_previous_messages
                )
                if len(self.user_previous_messages) != before:
                    logger.info(
                        f"Dropped {before - len(self.user_previous_messages)} unfinished "
                        f"tool-call message(s) from history for user: {self.user_name} - "
                        f"chat_id: {self.chat_id}"
                    )

                chat_messages = (
                    [self.system_message.model_dump()]
                    + self.user_previous_messages
                    + [input_message]
                )
                logger.info(
                    f"Continuing chat with {len(self.user_previous_messages)} previous messages for user: {self.user_name} - chat_id: {self.chat_id}"
                )

            initial_state = {"messages": chat_messages}

            return self.graph.astream(
                initial_state,
                config=self.graph_config(),
                stream_mode=["messages", "values", "updates", "custom"],
            )
        except Exception as e:
            # import traceback
            # tb_str = traceback.format_exc()
            logger.error(f"Error in get_processing_state: {e}")
            return {"error": "Error in get_processing_state"}
