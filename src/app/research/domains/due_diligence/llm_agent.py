"""
DD Tools LLM Agent — LangGraph + MCP (SSE).

Connects to a running DD Tools MCP server over SSE, discovers all tools
(DD + Yahoo Finance), wraps them with LangGraph, and lets an LLM decide
which tool(s) to call.

The MCP server URL is resolved in order:
  1. ``mcp_server_url`` constructor argument
  2. ``DD_MCP_SERVER_URL`` environment variable

Usage:
    # From code:
    agent = DDToolsAgent(mcp_server_url="http://10.0.0.5:8000/sse")
    await agent.initialize()
    answer, tool_called = await agent.query("Due diligence on Tata Motors")
    await agent.close()

    # Via env var:
    #   DD_MCP_SERVER_URL=http://10.0.0.5:8000/sse
    agent = DDToolsAgent()
    await agent.initialize()
    ...
"""

from __future__ import annotations

import os
import sys
from datetime import datetime
from typing import Annotated, Literal, TypedDict

from dotenv import load_dotenv
from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import StructuredTool
from langchain_openai import ChatOpenAI
from langgraph.graph import END, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode
from mcp import ClientSession
from mcp.client.sse import sse_client
from pydantic import Field, create_model

from app.core.constants import DUE_DILIGENCE_MODEL
from app.core.logging import setup_logging
from app.observability.llm_response_logger import save_raw_llm_response

logger = setup_logging(__name__)

load_dotenv()

try:
    from app.research.domains.due_diligence.prompts import DD_MCP_TOOL_CALLING_SYSTEM_PROMPT
except ModuleNotFoundError:
    _this_file = os.path.abspath(__file__)
    _project_root = os.path.abspath(os.path.join(_this_file, "..", "..", "..", ".."))
    if _project_root not in sys.path:
        sys.path.insert(0, _project_root)
    from app.research.domains.due_diligence.prompts import DD_MCP_TOOL_CALLING_SYSTEM_PROMPT


def _build_system_message() -> SystemMessage:
    return SystemMessage(
        content=DD_MCP_TOOL_CALLING_SYSTEM_PROMPT.format(
            current_date=datetime.now().strftime("%Y-%m-%d"),
        )
    )


class AgentState(TypedDict):
    messages: Annotated[list, add_messages]


class DDToolsAgent:
    """Due Diligence Agent using LangGraph + MCP (SSE transport).

    Connects to a running MCP server.  The URL is resolved from
    *mcp_server_url* or the ``DD_MCP_SERVER_URL`` env-var.
    """

    def __init__(
        self,
        model_name: str = DUE_DILIGENCE_MODEL,
        mcp_server_url: str | None = None,
        chat_id: str | None = None,
        user_id: str | None = None,
    ):
        self.model_name = model_name
        self.mcp_server_url = mcp_server_url or os.environ.get("DD_MCP_SERVER_URL", "")
        if not self.mcp_server_url:
            raise ValueError(
                "MCP server URL is required. Pass mcp_server_url or set "
                "the DD_MCP_SERVER_URL environment variable."
            )
        self.chat_id = chat_id
        self.user_id = user_id
        self.session: ClientSession | None = None
        self._transport_ctx = None
        self.tools: list[StructuredTool] = []
        self.app = None

    async def initialize(self):
        logger.info(f"Connecting to MCP server via SSE: {self.mcp_server_url}")
        self._transport_ctx = sse_client(url=self.mcp_server_url, sse_read_timeout=600)
        read_stream, write_stream = await self._transport_ctx.__aenter__()
        self.session = ClientSession(read_stream, write_stream)
        await self.session.__aenter__()
        await self.session.initialize()

        response = await self.session.list_tools()
        self.tools = self._create_langchain_tools(response.tools)
        self.app = self._create_workflow()

        logger.info(f"Agent initialized | server={self.mcp_server_url} | tools={len(self.tools)}")
        logger.info(f"Available tools: {[t.name for t in self.tools]}")

    def _create_langchain_tools(self, mcp_tools) -> list[StructuredTool]:
        langchain_tools: list[StructuredTool] = []

        for mcp_tool in mcp_tools:
            input_schema = mcp_tool.inputSchema if hasattr(mcp_tool, "inputSchema") else {}
            properties = input_schema.get("properties", {})
            required = input_schema.get("required", [])

            args_schema = None
            if properties:
                field_definitions = {}
                for prop_name, prop_info in properties.items():
                    prop_description = prop_info.get("description", "")
                    default = ... if prop_name in required else None
                    field_definitions[prop_name] = (
                        str,
                        Field(default=default, description=prop_description),
                    )
                args_schema = create_model(f"{mcp_tool.name}_args", **field_definitions)

            def create_tool_func(tool_name: str):
                async def tool_func(**kwargs):
                    logger.info(f"[MCP CALL] tool={tool_name} | args={kwargs}")
                    result = await self.session.call_tool(tool_name, arguments=kwargs)
                    if result.content:
                        text = result.content[0].text
                        logger.info(
                            f"[MCP RESULT] tool={tool_name} | "
                            f"chars={len(text)} | "
                            f"preview={text[:500]}"
                        )
                        return text
                    logger.warning(f"[MCP RESULT] tool={tool_name} | empty response")
                    return "No result returned"

                return tool_func

            tool_kwargs = {
                "coroutine": create_tool_func(mcp_tool.name),
                "name": mcp_tool.name,
                "description": mcp_tool.description or f"Tool: {mcp_tool.name}",
            }
            if args_schema:
                tool_kwargs["args_schema"] = args_schema

            langchain_tools.append(StructuredTool.from_function(**tool_kwargs))

        return langchain_tools

    def _create_workflow(self):
        llm = ChatOpenAI(model=self.model_name, temperature=0)
        llm_with_tools = llm.bind_tools(self.tools)

        def call_model(state: AgentState):
            messages = state["messages"]
            response = llm_with_tools.invoke([_build_system_message()] + messages)
            save_raw_llm_response(
                response,
                self.model_name,
                "Running due diligence research analysis",
                self.chat_id,
                user_id=self.user_id,
            )
            return {"messages": [response]}

        def should_continue(state: AgentState) -> Literal["tools", "end"]:
            last_message = state["messages"][-1]
            if not last_message.tool_calls:
                return "end"
            return "tools"

        workflow = StateGraph(AgentState)
        workflow.add_node("agent", call_model)
        workflow.add_node("tools", ToolNode(self.tools))
        workflow.set_entry_point("agent")
        workflow.add_conditional_edges(
            "agent",
            should_continue,
            {"tools": "tools", "end": END},
        )
        workflow.add_edge("tools", "agent")

        return workflow.compile()

    async def query(self, user_query: str) -> tuple[str, bool]:
        """Send a query to the agent. Returns (answer, tool_called)."""
        if not self.app:
            raise RuntimeError("Agent not initialized. Call initialize() first.")

        initial_state = {"messages": [HumanMessage(content=user_query)]}
        config = {"recursion_limit": 50}
        final_state = await self.app.ainvoke(initial_state, config=config)

        tool_called = any(isinstance(msg, ToolMessage) for msg in final_state["messages"])
        final_message = final_state["messages"][-1]
        return final_message.content, tool_called

    async def process_queries(self, queries: list[str]) -> dict[str, str]:
        """Process multiple queries. Returns results only for queries that triggered tool calls."""
        results: dict[str, str] = {}
        for q in queries:
            logger.info(f"Processing: {q}")
            try:
                answer, tool_called = await self.query(q)
                if tool_called:
                    results[q] = answer
                    logger.info("  -> Tool called — answer saved")
                else:
                    logger.info("  -> No tool call — skipped")
            except Exception as e:
                logger.info(f"  -> Error: {e}")
        return results

    async def close(self):
        if self.session:
            await self.session.__aexit__(None, None, None)
        if self._transport_ctx:
            await self._transport_ctx.__aexit__(None, None, None)
        logger.info("Session closed")


async def run_agent(
    queries: list[str],
    model_name: str = DUE_DILIGENCE_MODEL,
    mcp_server_url: str | None = None,
) -> dict[str, str]:
    """Convenience function: initialize agent, run queries, close."""
    agent = DDToolsAgent(model_name=model_name, mcp_server_url=mcp_server_url)
    try:
        await agent.initialize()
        return await agent.process_queries(queries)
    finally:
        await agent.close()


# if __name__ == "__main__":
#     import argparse

#     parser = argparse.ArgumentParser(description="DD Tools LLM Agent")
#     parser.add_argument(
#         "query",
#         nargs="?",
#         default="Give me a due diligence about Tata Motors",
#     )
#     parser.add_argument("--model", default="gpt-5.4")
#     parser.add_argument(
#         "--url",
#         default=None,
#         help="MCP server SSE URL (e.g. http://localhost:8000/sse). "
#              "Falls back to DD_MCP_SERVER_URL env var.",
#     )
#     args = parser.parse_args()

#     async def main():
#         agent = DDToolsAgent(model_name=args.model, mcp_server_url=args.url)
#         try:
#             await agent.initialize()
#             answer, tool_called = await agent.query(args.query)
#             logger.info("\n" + "=" * 80)
#             logger.info(f"TOOL CALLED: {tool_called}")
#             logger.info("=" * 80)
#             logger.info("ANSWER:")
#             logger.info(answer)
#         finally:
#             await agent.close()

#     asyncio.run(main())
