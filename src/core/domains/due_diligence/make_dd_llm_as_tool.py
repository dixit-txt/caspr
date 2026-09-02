"""
DD research query runner.

Exposes ``_run_dd_query`` which spins up the DDToolsAgent (22 MCP tools),
runs a single natural-language query, and returns the answer string.

Used by ``graph.py`` → ``_dd_research_handler`` → ``card_utils.generate_cards``
when the OpenAI model calls the ``dd_research`` function tool at runtime.
"""

from __future__ import annotations

from src.core.domains.due_diligence.llm_agent import DDToolsAgent
from src.config.constants import DUE_DILIGENCE_MODEL


async def _run_dd_query(
    query: str,
    model_name: str = DUE_DILIGENCE_MODEL,
    mcp_server_url: str | None = None,
    chat_id: str | None = None,
    user_id: str | None = None,
) -> str:
    """Spin up the DDToolsAgent, run a single query, return the answer.

    If *mcp_server_url* is provided (or ``DD_MCP_SERVER_URL`` env-var is set),
    connects to the running MCP server over SSE instead of spawning a subprocess.
    """
    agent = DDToolsAgent(
        model_name=model_name,
        mcp_server_url=mcp_server_url,
        chat_id=chat_id,
        user_id=user_id,
    )
    try:
        await agent.initialize()
        answer, _ = await agent.query(query)
        return answer
    finally:
        await agent.close()