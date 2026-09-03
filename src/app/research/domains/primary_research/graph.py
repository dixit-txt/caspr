"""
Primary Research subgraph.

Nodes
-----
pr_validate_upload  -> gate: files present?
pr_analyze_document -> LLM understands the data before layout generation
pr_generate_drl     -> domain-aware descriptive report layout
pr_generate_cards   -> card loop (web_search=False, document-only)
pr_synthesize       -> executive summary + markdown assembly

The compiled subgraph is returned by ``build_primary_research_subgraph()``
and plugged into the main graph via the domain router.
"""

from __future__ import annotations

import asyncio
import json
from typing import Optional

from langchain_core.messages import AIMessage
from langgraph.config import get_stream_writer
from langgraph.graph import END, StateGraph

from google import genai

from app.core.constants import (
    SYNC_OPENAI_CLIENT, PRIMARY_RESEARCH_MODEL, GEMINI_API_KEY, GEMINI_PRIMARY_RESEARCH_MODEL,
)
from app.core.logging import setup_logging
from app.observability.llm_response_logger import save_raw_llm_response, strip_json_code_fence
from app.research.domains.planner import (
    BaseDomainState,
    generate_section_cards,
    plan_report_layout,
    synthesize_report,
)
from app.research.domains.primary_research.config import PRIMARY_RESEARCH_CONFIG
from app.research.domains.primary_research.prompts import (
    PR_DOCUMENT_ANALYSIS_PROMPT,
    PR_DOCUMENT_ANALYSIS_SCHEMA,
)

logger = setup_logging(__name__)
_CFG = PRIMARY_RESEARCH_CONFIG


# ---------------------------------------------------------------------------
# Subgraph state (extends BaseDomainState with PR-specific fields)
# ---------------------------------------------------------------------------

class PrimaryResearchState(BaseDomainState, total=False):
    document_analysis: dict


# ---------------------------------------------------------------------------
# Node implementations
# ---------------------------------------------------------------------------

async def pr_validate_upload(state: PrimaryResearchState) -> PrimaryResearchState:
    """Gate node: ensures uploaded files or a grep session are present.

    If neither ``upload_file_config`` nor ``grep_session`` is present, sets
    ``state["error"]`` which the conditional edge reads to route to the error node.
    """
    config = state.get("upload_file_config") or {}
    file_ids = config.get("file_ids")
    vector_store_id = config.get("vector_store_id")
    grep_session = state.get("grep_session")

    if not file_ids and not vector_store_id and not grep_session:
        logger.warning(
            f"[primary_research] No uploaded files or grep session -- aborting | "
            f"user: {state.get('user_name')} chat: {state.get('chat_id')}"
        )
        return {**state, "error": "no_upload"}

    logger.info(
        f"[primary_research] Upload validated "
        f"(file_ids={file_ids}, grep_session={bool(grep_session)}) | "
        f"user: {state.get('user_name')} chat: {state.get('chat_id')}"
    )
    return state


async def pr_error_no_upload(state: PrimaryResearchState) -> PrimaryResearchState:
    """Terminal node: returns a friendly error when no files were uploaded."""
    msg = (
        "Primary Research Analysis requires you to upload your research data "
        "first.  Please upload your survey results, interview transcripts, or "
        "other research documents and try again."
    )
    logger.info(
        f"[primary_research] No upload files — returning error to wrapper | "
        f"user: {state.get('user_name')} chat: {state.get('chat_id')}"
    )
    return {**state, "error": msg}


async def pr_analyze_document(state: PrimaryResearchState) -> PrimaryResearchState:
    """Analyse the uploaded document to understand its data before layout
    generation.

    Uses vector-store file_search (OpenAI mode) or the grep_agent_2 session
    (grep mode) to ask structured questions about the document, producing a
    ``DocumentAnalysis`` dict that downstream nodes consume.
    """
    from app.adapters.grep_agent_2 import ask_pdfs as grep_ask_pdfs

    event_writer = get_stream_writer()
    event_writer({"name": "pr_analyze_document", "status": "start"})

    upload_config = state.get("upload_file_config") or {}
    vector_store_id = upload_config.get("vector_store_id")
    grep_session = state.get("grep_session")
    user_instructions = state.get("user_instructions", "")

    logger.info(
        f"[primary_research] pr_analyze_document started | "
        f"vector_store_id={vector_store_id} | grep_session={bool(grep_session)} | "
        f"has_user_instructions={bool(user_instructions)} | "
        f"user: {state.get('user_name')} chat: {state.get('chat_id')}"
    )

    analysis_prompt = PR_DOCUMENT_ANALYSIS_PROMPT
    if user_instructions:
        analysis_prompt += f"\n\nAdditional context from the user:\n{user_instructions}"

    try:
        if grep_session is not None:
            # Grep mode: ask_pdfs is already an LLM call — pass the analysis prompt
            # directly so the workers read the doc and return the JSON answer.
            _uid = state.get("user_id") or state.get("user_name")
            grep_result = await asyncio.to_thread(
                grep_ask_pdfs, grep_session, analysis_prompt,
                chat_id=state.get("chat_id"), user_id=_uid,
            )
            raw_text = grep_result.get("answer", "{}")
        else:
            # OpenAI vector-store mode
            tools = []
            tool_choice = "none"
            if vector_store_id:
                tools = [{"type": "file_search", "vector_store_ids": [vector_store_id]}]
                tool_choice = {"type": "file_search"}

            try:
                response = SYNC_OPENAI_CLIENT.responses.create(
                    model=PRIMARY_RESEARCH_MODEL,
                    input=[
                        {
                            "role": "system",
                            "content": (
                                "You are a research-data analyst.  Use the uploaded "
                                "document to answer the analysis prompt.  Return valid "
                                "JSON only."
                            ),
                        },
                        {"role": "user", "content": analysis_prompt},
                    ],
                    tools=tools if tools else None,
                    tool_choice=tool_choice if tools else None,
                )
                save_raw_llm_response(
                    response, PRIMARY_RESEARCH_MODEL, "Analyzing an uploaded primary research document",
                    state.get("chat_id"),
                    user_id=state.get("user_id") or state.get("user_name"),
                )
                raw_text = response.output_text
            except Exception as openai_exc:
                logger.warning(
                    f"[primary_research] OpenAI document analysis failed "
                    f"({type(openai_exc).__name__}: {openai_exc}), falling back to Gemini"
                )
                # NOTE: vector_store_id is OpenAI-specific — Gemini's file_search
                # would need its own native vector store, so this fallback answers
                # from the prompt alone (best effort, no file_search tool).
                gemini_client = genai.Client(api_key=GEMINI_API_KEY)
                interaction = gemini_client.interactions.create(
                    model=GEMINI_PRIMARY_RESEARCH_MODEL,
                    input=(
                        "You are a research-data analyst. Use the uploaded "
                        "document to answer the analysis prompt. Return valid "
                        "JSON only.\n\n" + analysis_prompt
                    ),
                    response_format={
                        "type": "text",
                        "mime_type": "application/json",
                        "schema": PR_DOCUMENT_ANALYSIS_SCHEMA,
                    },
                )
                save_raw_llm_response(
                    interaction, GEMINI_PRIMARY_RESEARCH_MODEL,
                    "Analyzing an uploaded primary research document (Gemini backup)",
                    state.get("chat_id"),
                    user_id=state.get("user_id") or state.get("user_name"),
                )
                raw_text = strip_json_code_fence(interaction.output_text)

        analysis = json.loads(raw_text)
        logger.info(
            f"[primary_research] Document analysis complete: data_type={analysis.get('data_type')} | "
            f"user: {state.get('user_name')} chat: {state.get('chat_id')}"
        )
    except Exception as exc:
        logger.error(f"[primary_research] Document analysis failed: {exc}")
        analysis = {
            "data_type": "unknown",
            "key_themes": [],
            "methodology_present": False,
            "methodology_summary": "",
            "sample_info": "",
            "key_data_points": [],
            "structure_notes": "",
        }

    event_writer({"name": "pr_analyze_document", "status": "end", "analysis": analysis})
    return {**state, "document_analysis": analysis}


async def pr_generate_drl(state: PrimaryResearchState) -> PrimaryResearchState:
    """Generate the descriptive report layout using the PR-specific DRL
    addendum and the document analysis context."""
    event_writer = get_stream_writer()
    event_writer({"name": "pr_generate_drl", "status": "start"})

    user_instructions = state.get("user_instructions", "")
    doc_analysis = state.get("document_analysis", {})

    logger.info(
        f"[primary_research] pr_generate_drl started | "
        f"has_doc_analysis={bool(doc_analysis)} | "
        f"data_type='{doc_analysis.get('data_type', 'N/A')}' | "
        f"themes_count={len(doc_analysis.get('key_themes', []))} | "
        f"report_layout_len={len(state.get('report_layout', ''))} | "
        f"user: {state.get('user_name')} chat: {state.get('chat_id')}"
    )

    if doc_analysis:
        analysis_context = (
            f"\n\n--- DOCUMENT ANALYSIS (use this to inform section structure) ---\n"
            f"Data type: {doc_analysis.get('data_type', 'N/A')}\n"
            f"Key themes: {', '.join(doc_analysis.get('key_themes', []))}\n"
            f"Methodology present: {doc_analysis.get('methodology_present', False)}\n"
            f"Sample info: {doc_analysis.get('sample_info', 'N/A')}\n"
            f"Key data points: {json.dumps(doc_analysis.get('key_data_points', []))}\n"
            f"--- END DOCUMENT ANALYSIS ---"
        )
        user_instructions = user_instructions + analysis_context

    report_layout = state.get("report_layout", "")

    try:
        drl, cleaned_rl = await plan_report_layout(
            report_layout=report_layout,
            user_instructions=user_instructions,
            domain_config=_CFG,
            s3_instance=state.get("s3_instance"),
            user_name=state.get("user_name", ""),
            chat_id=state.get("chat_id", ""),
            user_id=state.get("user_id") or state.get("user_name", ""),
            report_length=state.get("report_length", "OVERVIEW"),
        )
    except ValueError as exc:
        logger.error(f"[primary_research] DRL generation failed: {exc}")
        return {**state, "error": str(exc)}

    event_writer({"name": "pr_generate_drl", "report_layout": cleaned_rl})
    event_writer({"name": "pr_generate_drl", "status": "end"})

    logger.info(
        f"[primary_research] pr_generate_drl completed | "
        f"drl_sections={len(drl)} | "
        f"user: {state.get('user_name')} chat: {state.get('chat_id')}"
    )

    return {
        **state,
        "descriptive_report_layout": drl,
    }


async def pr_generate_cards_and_synthesize(state: PrimaryResearchState) -> PrimaryResearchState:
    """Run the card generation loop and synthesize the final report.

    cards_for_db stays a local variable -- never stored in LangGraph state.
    """
    event_writer = get_stream_writer()

    logger.info(
        f"[primary_research] pr_generate_cards_and_synthesize started | "
        f"drl_sections={len(state.get('descriptive_report_layout', []))} | "
        f"report_length='{state.get('report_length', 'overview')}' | "
        f"web_search=False (document-only mode) | "
        f"user: {state.get('user_name')} chat: {state.get('chat_id')}"
    )

    _user_id = state.get("user_id") or state.get("user_name", "")
    cards_for_db, cumulative_summary, all_citations, table_map = (
        await generate_section_cards(
            descriptive_report_layout=state["descriptive_report_layout"],
            user_instructions=state.get("user_instructions", ""),
            upload_file_config=state.get("upload_file_config"),
            report_length=state.get("report_length", "overview"),
            web_search=False,
            user_name=state.get("user_name", ""),
            chat_id=state.get("chat_id", ""),
            user_id=_user_id,
            domain_config=_CFG,
            event_writer=event_writer,
            s3_instance=state.get("s3_instance"),
            grep_session=state.get("grep_session"),
            report_type=state.get("report_type", "study"),
        )
    )

    ai_message = await synthesize_report(
        cards_for_db=cards_for_db,
        cumulative_summary=cumulative_summary,
        table_and_table_id_map=table_map,
        report_title=state.get("report_title", "Primary Research Report"),
        user_name=state.get("user_name", ""),
        chat_id=state.get("chat_id", ""),
        user_id=_user_id,
        domain_config=_CFG,
        event_writer=event_writer,
        s3_instance=state.get("s3_instance"),
    )

    logger.info(
        f"[primary_research] pr_generate_cards_and_synthesize completed | "
        f"cards={len(cards_for_db)} | citations={len(all_citations)} | "
        f"tables={len(table_map)} | output_len={len(ai_message.content)} chars | "
        f"user: {state.get('user_name')} chat: {state.get('chat_id')}"
    )

    return {
        **state,
        "cumulative_summary": cumulative_summary,
        "all_report_citations": all_citations,
        "table_and_table_id_map": table_map,
        "final_message": ai_message,
    }


# ---------------------------------------------------------------------------
# Routing function for the validation gate
# ---------------------------------------------------------------------------

def _route_after_validation(state: PrimaryResearchState) -> str:
    if state.get("error"):
        logger.info(
            f"[primary_research] Validation failed — routing to error node | "
            f"error='{state.get('error')}' | "
            f"user: {state.get('user_name')} chat: {state.get('chat_id')}"
        )
        return "pr_error_no_upload"
    logger.info(
        f"[primary_research] Validation passed — routing to document analysis | "
        f"user: {state.get('user_name')} chat: {state.get('chat_id')}"
    )
    return "pr_analyze_document"


# ---------------------------------------------------------------------------
# Build the compiled subgraph
# ---------------------------------------------------------------------------

def build_primary_research_subgraph():
    """Construct and compile the Primary Research LangGraph subgraph.

    Returns a compiled ``StateGraph`` that the main graph can add as a node
    via ``graph_builder.add_node("pr_subgraph", subgraph)``.
    """
    logger.info("[primary_research] Building Primary Research subgraph: validate -> analyze -> drl -> cards+synthesize")
    builder = StateGraph(PrimaryResearchState)

    builder.add_node("pr_validate_upload", pr_validate_upload)
    builder.add_node("pr_error_no_upload", pr_error_no_upload)
    builder.add_node("pr_analyze_document", pr_analyze_document)
    builder.add_node("pr_generate_drl", pr_generate_drl)
    builder.add_node("pr_generate_cards_and_synthesize", pr_generate_cards_and_synthesize)

    builder.set_entry_point("pr_validate_upload")

    builder.add_conditional_edges(
        "pr_validate_upload",
        _route_after_validation,
        {
            "pr_error_no_upload": "pr_error_no_upload",
            "pr_analyze_document": "pr_analyze_document",
        },
    )

    builder.add_edge("pr_error_no_upload", END)
    builder.add_edge("pr_analyze_document", "pr_generate_drl")
    builder.add_edge("pr_generate_drl", "pr_generate_cards_and_synthesize")
    builder.add_edge("pr_generate_cards_and_synthesize", END)

    return builder.compile()
