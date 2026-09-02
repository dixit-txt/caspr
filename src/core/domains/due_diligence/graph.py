"""
Due Diligence subgraph.

Flow: dd_generate_drl -> dd_generate_cards_and_synthesize -> END

The card-generation step passes ``dd_research`` as an external function tool
to ``generate_cards`` (Case 5 in card_utils.py). The OpenAI model decides at
runtime what to query via the DDToolsAgent (22 MCP tools).
"""

from __future__ import annotations

import asyncio
import concurrent.futures

from langgraph.config import get_stream_writer
from langgraph.graph import END, StateGraph

from src.config.log_helper import setup_logging
from src.core.domains.planner import (
    BaseDomainState,
    generate_section_cards,
    plan_report_layout,
    synthesize_report,
)
from src.core.domains.due_diligence.config import DUE_DILIGENCE_CONFIG

logger = setup_logging(__name__)
_CFG = DUE_DILIGENCE_CONFIG


DD_RESEARCH_TOOL_DEFINITION = {
    "type": "function",
    "function": {
        "name": "dd_research",
        "description": (
            "Run a Due Diligence research query. An inner LLM agent with 22 tools "
            "(Yahoo Finance, court records, news sentiment, deep web research) "
            "autonomously decides which tools to call and returns a synthesised "
            "answer. Use this to fetch real financial data, court filings, "
            "news sentiment, ESG scores, insider transactions, analyst targets, etc."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Natural-language DD question, e.g. 'Get AAPL balance sheet and income statement'",
                },
            },
            "required": ["query"],
        },
    },
}


class DueDiligenceState(BaseDomainState, total=False):
    pass


def _dd_research_handler(
    mcp_server_url: str,
    args: dict,
    chat_id: str = "",
    user_id: str = "",
) -> str:
    """Sync handler called by generate_cards when the model invokes dd_research."""
    from src.core.domains.due_diligence.make_dd_llm_as_tool import _run_dd_query

    query = args.get("query", "")
    logger.info(f"[dd_research] query={query[:200]}")

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop and loop.is_running():
        with concurrent.futures.ThreadPoolExecutor() as pool:
            result = pool.submit(
                asyncio.run,
                _run_dd_query(
                    query,
                    mcp_server_url=mcp_server_url,
                    chat_id=chat_id or None,
                    user_id=user_id or None,
                ),
            ).result()
    else:
        result = asyncio.run(
            _run_dd_query(
                query,
                mcp_server_url=mcp_server_url,
                chat_id=chat_id or None,
                user_id=user_id or None,
            )
        )

    logger.info(f"[dd_research] returned {len(result)} chars")
    return result


def _build_dd_external_tools(
    mcp_server_url: str = "",
    chat_id: str = "",
    user_id: str = "",
) -> list[dict]:
    def handler(args: dict) -> str:
        return _dd_research_handler(mcp_server_url, args, chat_id=chat_id, user_id=user_id)
    return [{**DD_RESEARCH_TOOL_DEFINITION, "_handler": handler}]


async def dd_generate_drl(state: DueDiligenceState) -> DueDiligenceState:
    event_writer = get_stream_writer()
    event_writer({"name": "dd_generate_drl", "status": "start"})

    logger.info(
        f"[due_diligence] dd_generate_drl started | "
        f"user: {state.get('user_name')} chat: {state.get('chat_id')}"
    )

    try:
        drl, cleaned_rl = await plan_report_layout(
            report_layout=state.get("report_layout", ""),
            user_instructions=state.get("user_instructions", ""),
            domain_config=_CFG,
            s3_instance=state.get("s3_instance"),
            user_name=state.get("user_name", ""),
            chat_id=state.get("chat_id", ""),
            user_id=state.get("user_id") or state.get("user_name", ""),
            report_length=state.get("report_length", "OVERVIEW"),
        )
    except ValueError as exc:
        logger.error(f"[due_diligence] DRL generation failed: {exc}")
        return {**state, "error": str(exc)}

    event_writer({"name": "dd_generate_drl", "report_layout": cleaned_rl})
    event_writer({"name": "dd_generate_drl", "status": "end"})
    logger.info(f"[due_diligence] dd_generate_drl completed | drl_sections={len(drl)}")

    return {**state, "descriptive_report_layout": drl}


async def dd_generate_cards_and_synthesize(state: DueDiligenceState) -> DueDiligenceState:
    event_writer = get_stream_writer()

    logger.info(
        f"[due_diligence] dd_generate_cards_and_synthesize started | "
        f"drl_sections={len(state.get('descriptive_report_layout', []))} | "
        f"user: {state.get('user_name')} chat: {state.get('chat_id')}"
    )

    _user_id = state.get("user_id") or state.get("user_name", "")
    cards_for_db, cumulative_summary, all_citations, table_map = (
        await generate_section_cards(
            descriptive_report_layout=state["descriptive_report_layout"],
            user_instructions=state.get("user_instructions", ""),
            upload_file_config=state.get("upload_file_config"),
            report_length=state.get("report_length", "overview"),
            web_search=True,
            user_name=state.get("user_name", ""),
            chat_id=state.get("chat_id", ""),
            user_id=_user_id,
            domain_config=_CFG,
            event_writer=event_writer,
            s3_instance=state.get("s3_instance"),
            external_tools=_build_dd_external_tools(
                state.get("mcp_server_url", ""),
                chat_id=state.get("chat_id", ""),
                user_id=_user_id,
            ),
            grep_session=state.get("grep_session"),
            report_type="study",
        )
    )

    ai_message = await synthesize_report(
        cards_for_db=cards_for_db,
        cumulative_summary=cumulative_summary,
        table_and_table_id_map=table_map,
        report_title=state.get("report_title", "Due Diligence Report"),
        user_name=state.get("user_name", ""),
        chat_id=state.get("chat_id", ""),
        user_id=_user_id,
        domain_config=_CFG,
        event_writer=event_writer,
        s3_instance=state.get("s3_instance"),
    )

    logger.info(
        f"[due_diligence] dd_generate_cards_and_synthesize completed | "
        f"cards={len(cards_for_db)} | citations={len(all_citations)} | "
        f"user: {state.get('user_name')} chat: {state.get('chat_id')}"
    )

    return {
        **state,
        "cumulative_summary": cumulative_summary,
        "all_report_citations": all_citations,
        "table_and_table_id_map": table_map,
        "final_message": ai_message,
    }


def build_due_diligence_subgraph():
    builder = StateGraph(DueDiligenceState)
    builder.add_node("dd_generate_drl", dd_generate_drl)
    builder.add_node("dd_generate_cards_and_synthesize", dd_generate_cards_and_synthesize)
    builder.set_entry_point("dd_generate_drl")
    builder.add_edge("dd_generate_drl", "dd_generate_cards_and_synthesize")
    builder.add_edge("dd_generate_cards_and_synthesize", END)
    return builder.compile()