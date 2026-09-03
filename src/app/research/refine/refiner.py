import base64
import datetime
import json
import os
import re
import shutil
import tempfile
import time
from typing import Any

from google import genai
from pydantic import BaseModel, Field
from uuid_utils import uuid7

from app.adapters.grep_agent_2 import PdfSession
from app.adapters.grep_agent_2 import ask_pdfs as grep_ask_pdfs
from app.adapters.s3 import get_s3_instance
from app.cards.service_cards import (
    _enforce_numbered_citations,
    _is_small_table,
    clean_url,
    extract_markdown_tables,
    extract_title_and_description_for_table,
    fix_numeric_url_citations,
    generate_section_summary,
    repair_malformed_citation_markdown,
    replace_table_in_content,
    update_summaries_for_ask_caspr,
)
from app.cards.service_citations import generate_citation_url
from app.cards.service_fixer import fix_content
from app.core.constants import (
    GEMINI_API_KEY,
    GEMINI_ES_MODEL_ID,
    REFINER_MODEL,
    S3_REPORTS_BASE_PATH,
    SYNC_OPENAI_CLIENT,
    VISUALIZATION_MODEL,
)
from app.core.logging import setup_logging
from app.observability.llm_response_logger import save_raw_llm_response, strip_json_code_fence
from app.observability.web_search_analytics import (
    collect_openai_search_analytics,
    # collect_perplexity_search_analytics,  # DISABLED: superseded by Gemini fallback
    # collect_gemini_search_analytics,  # DISABLED: not applicable to Interactions API response shape
)
from app.research.prompts.dynamic_prompting import decide_if_table_needs_visualization
from app.research.prompts.prompt_utils import (
    REFINE_PROMPT_WITH_UPLOAD,
    REFINE_PROMPT_WITHOUT_UPLOAD,
    REFINE_SCHEMA_GEMINI,
    TABLE_TO_VIZ_PROMPT,
)
from app.research.refine.refine_table_policy import (
    classify_visualization_intent,
    process_refined_tables,
)
from app.research.refine.visualizer.viz_refine import (
    generate_visualization as generate_forced_visualization,
)
from app.research.visualization.html_graph_maker import generate_html_visualization

s3_instance = get_s3_instance()

logger = setup_logging(__file__)

_INLINE_CITATION_RE = re.compile(r"\[(\d+)\]\((https?://[^)\s]+)\)")
_NAMED_CITATION_RE = re.compile(r"(?<!!)\[([^\]]+)\]\((https?://[^)]+)\)")


def _extract_content_string(content) -> str:
    """Normalize section/subsection content to a plain markdown string."""
    if content is None:
        return ""
    if isinstance(content, dict):
        inner = content.get("content", "")
        if isinstance(inner, str):
            return inner
        return str(inner) if inner is not None else ""
    return content if isinstance(content, str) else str(content)


def _collect_openai_refinement_citations(output: list[Any]) -> tuple[list[str], dict[str, str]]:
    """Extract citation URLs and snippets from an OpenAI responses API output."""
    citations: list[str] = []
    url_to_snippet: dict[str, str] = {}
    seen_urls = set()

    def _add_url(url: str, snippet: str = "") -> None:
        if not url or not url.startswith(("http://", "https://")):
            return
        cleaned = clean_url(url)
        if cleaned not in seen_urls:
            seen_urls.add(cleaned)
            citations.append(url)
        if snippet and cleaned not in url_to_snippet:
            url_to_snippet[cleaned] = snippet

    if output:
        last_output = output[-1]
        content = last_output.get("content", [])
        if content:
            first_content = content[0]
            for annotation in first_content.get("annotations", []):
                url = annotation.get("url")
                snippet = annotation.get("snippet", "")
                if url:
                    _add_url(url, snippet)

    for item in output or []:
        if isinstance(item, dict) and item.get("type") == "web_search_call":
            for result in item.get("results", []):
                url = result.get("url", "")
                snippet = result.get("snippet", "")
                if url:
                    _add_url(url, snippet)

    return citations, url_to_snippet


def _output_used_web_search(output: list[Any]) -> bool:
    """Return True if the OpenAI response includes a web_search_call step."""
    for item in output or []:
        if isinstance(item, dict) and item.get("type") == "web_search_call":
            return True
        if getattr(item, "type", None) == "web_search_call":
            return True
    return False


def _extract_gemini_refine_citations(interaction, content_text: str) -> list[str]:
    """Collect citation URLs from Gemini interaction annotations, search results, and inline links."""
    citations: list[str] = []
    seen = set()

    def _add(url: str | None) -> None:
        if not url or url in seen:
            return
        seen.add(url)
        citations.append(url)

    for step in getattr(interaction, "steps", None) or []:
        step_type = getattr(step, "type", None)
        if step_type == "model_output":
            for content_block in getattr(step, "content", None) or []:
                if getattr(content_block, "type", None) != "text":
                    continue
                for annotation in getattr(content_block, "annotations", None) or []:
                    ann_type = getattr(annotation, "type", None)
                    if ann_type == "url_citation" and getattr(annotation, "url", None):
                        _add(annotation.url)
        elif step_type == "google_search_call":
            for result in getattr(step, "results", None) or []:
                if isinstance(result, dict):
                    _add(result.get("url"))
                else:
                    _add(getattr(result, "url", None))

    for match in _NAMED_CITATION_RE.finditer(content_text or ""):
        _add(match.group(2))

    return citations


class RefineCard(BaseModel):
    content: str = Field(..., description="The refined content of the section or subsection")


def find_target_section_or_subsection(cards_for_db, targeted_section_or_subsection) -> dict:
    """
    Find the target section or subsection in the cards for the database

    Args:
        cards_for_db: The cards for the database
        targeted_section_or_subsection: The targeted section or subsection
    Returns:
        The target section or subsection
    """
    refine_target = None

    for idx, card in enumerate(cards_for_db):
        for section in card.get("section", []):
            if section.get("name") == targeted_section_or_subsection:
                refine_target = {
                    "name": section["name"],
                    "content": section["content"],
                    "idx": idx,
                    "sub_idx": None,
                }
                break
        for sub_idx, subsection in enumerate(card.get("sub_sections", [])):
            if subsection.get("name") == targeted_section_or_subsection:
                refine_target = {
                    "name": subsection["name"],
                    "content": subsection["content"],
                    "idx": idx,
                    "sub_idx": sub_idx,
                }
                break

        if refine_target:
            break

    return refine_target


def generate_refined_content(
    user_prompt,
    refine_target,
    max_retries=3,
    retry_delay=5,
    refinement_history=None,
    file_id=None,
    upload_file_config: dict[str, Any] | None = None,
    user_id=None,
    web_search=True,
    grep_session: PdfSession | None = None,
    chat_id=None,
    analytics_collector: list[dict] | None = None,
    analytics_operation_id: str | None = None,
) -> tuple[str, list[str], dict[str, str]]:
    """
    Generate refined content with full report context.

    Args:
        user_prompt: User's refinement request
        refine_target: The target section/subsection to refine
        max_retries: Maximum retry attempts
        retry_delay: Delay between retries
        file_id: OpenAI file_id of the uploaded report (optional)
        refinement_history: The refinement history for the refinement
        file_id: OpenAI file_id of the uploaded report (optional)
        upload_file_config: The upload file config
        # user_id: The user id
        web_search: The web search flag
        grep_session: Optional PdfSession from grep_agent_2 for document search
        analytics_collector: Optional list the caller passes in to receive one
            web-search analytics entry per successful provider response. A
            no-op when None (default), so existing callers are unaffected.
        analytics_operation_id: Optional id correlating all entries collected
            for this single refinement operation.
    Returns:
        tuple: (refined_content, citations, url_to_snippet)
    """
    model = REFINER_MODEL
    current_date = datetime.datetime.now().strftime("%B %d, %Y")
    if file_id is None:
        logger.warning("No file_id provided for the refinement.")

    if refinement_history and len(refinement_history) > 0:
        history_text = "**Previously Refined Sections:**\n\n"
        for idx, history_item in enumerate(refinement_history, 1):
            section_name = history_item[
                "name_of_the_section_or_subsection_that_user_previously_refined"
            ]
            refined_content = history_item["refined_content_for_the_section_or_subsection"]
            refine_request = history_item["prompt_used_for_refinement"]

            history_text += f"{idx}. **Section Name:** {section_name}\n"
            history_text += f"   **User's Refinement Request:** {refine_request}\n"
            history_text += f"   **Refined Content (Latest Version):**\n   {refined_content}\n\n"

        refinement_history_context = history_text
    else:
        refinement_history_context = "No sections have been refined yet. This is the first refinement in this session. Use the original report content for all sections."

    # Either document source counts as "with upload": a vector store via upload_file_config, or
    # the user's uploaded documents reachable through grep's search_documents tool.
    if upload_file_config is not None or grep_session is not None:
        refine_prompt_template = REFINE_PROMPT_WITH_UPLOAD
    else:
        refine_prompt_template = REFINE_PROMPT_WITHOUT_UPLOAD

    refine_prompt = refine_prompt_template.format(
        refine_target=refine_target,
        user_prompt=user_prompt,
        current_date=current_date,
        refinement_history_context=refinement_history_context,
    )

    # --- Grep session branch: use grep_agent_2 as a search_documents tool for the user's
    # uploaded documents. The generated report itself still travels as an attached input_file,
    # so this branch is a hybrid rather than a document-only path. ---
    if grep_session is not None:
        logger.info(
            f"Refining with grep_agent_2 tool (grep_session provided) | has_report_file={bool(file_id)}"
        )

        def _grep_tool_handler(args: dict) -> str:
            question = args.get("question", "")
            logger.info(f"[grep_agent_2] search_documents called with question: '{question[:120]}'")
            try:
                result = grep_ask_pdfs(grep_session, question)
                return result.get("answer", "No relevant content found in the documents.")
            except Exception as exc:
                logger.warning(f"[grep_agent_2] search_documents failed: {exc}")
                return f"Error searching documents: {exc}"

        grep_fn_tool = {
            "type": "function",
            "name": "search_documents",
            "description": (
                "Search the uploaded documents for information relevant to a specific question. "
                "Call this tool with a focused question to retrieve excerpts and facts from the documents. "
                "Use the returned content as your primary source when writing the refined section."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "question": {
                        "type": "string",
                        "description": "The specific question to search for in the uploaded documents.",
                    }
                },
                "required": ["question"],
            },
        }

        api_tools = [grep_fn_tool]
        if web_search:
            api_tools.append({"type": "web_search"})

        system_content = (
            "You are a precise researcher and technical writer helping to refine a report section. "
            "You have three distinct sources of context, and you must keep them separate:\n"
            "1. The attached report file — the full report that Caspr itself generated. Use it to keep "
            "the refined section consistent with the rest of the report.\n"
            "2. The selected section/subsection content in the user prompt — the current, possibly "
            "already refined, state of what you are rewriting. When it disagrees with the attached "
            "report file, this selected content is newer and wins.\n"
            "3. The search_documents tool — the user's own uploaded source documents.\n"
            "For wording, clarity, concision, tone, structure, or formatting requests, preserve the "
            "existing selected content as the base and rewrite it directly. "
            "ALWAYS call search_documents first with a focused question to retrieve relevant content "
            "from the documents before writing. Use document results to verify, supplement, or correct facts; "
            "do not replace existing selected content with a refusal just because the exact paragraph is not found in the documents. "
            "Attribute facts to the source they actually came from; never present a claim from the "
            "user's uploaded documents as though it came from the report, or vice versa. "
        )
        if web_search:
            system_content += (
                "You MUST call web_search before writing the refined section. "
                "Every factual claim, statistic, or data point in the output MUST be supported by an "
                "inline citation using a real URL returned by web_search — format: [Source Name](URL). "
                "Do NOT reuse or copy citation links from the input section; generate fresh citations "
                "from the URLs web_search returns. "
                "The uploaded document is your primary source — enrich it with web results where useful. "
                "Never contradict document content with web-sourced information. "
            )
        system_content += (
            "Do NOT fabricate information not found in either source. "
            "Do NOT invent URLs. "
            f"Today's date is {current_date}."
        )

        user_content: list[dict[str, Any]] = [
            {"type": "input_text", "text": refine_prompt.strip()},
        ]
        if file_id:
            user_content.append({"type": "input_file", "file_id": file_id})
        else:
            logger.error(
                "Refiner grep branch running without report file | "
                "full-report context unavailable, refining from selected content and documents only"
            )

        messages = [
            {"role": "system", "content": system_content},
            {"role": "user", "content": user_content},
        ]

        try:
            max_tool_rounds = 5
            for _round in range(max_tool_rounds):
                create_kwargs = {
                    "model": model,
                    "input": messages,
                    "tools": api_tools,
                    "temperature": 0.1,
                    "include": ["web_search_call.results"] if web_search else [],
                }
                if web_search:
                    create_kwargs["tool_choice"] = "required"
                response = SYNC_OPENAI_CLIENT.responses.parse(**create_kwargs)
                save_raw_llm_response(
                    response,
                    model,
                    "Refining a report card using document search",
                    chat_id,
                    user_id=user_id,
                )

                fn_calls = [
                    item
                    for item in response.output
                    if getattr(item, "type", None) == "function_call"
                    and getattr(item, "name", None) == "search_documents"
                ]

                if not fn_calls:
                    break

                for fc in fn_calls:
                    fn_args = (
                        json.loads(fc.arguments) if isinstance(fc.arguments, str) else fc.arguments
                    )
                    call_id = fc.call_id
                    result_text = _grep_tool_handler(fn_args)
                    messages.append(
                        {
                            "type": "function_call",
                            "call_id": call_id,
                            "name": "search_documents",
                            "arguments": json.dumps(fn_args)
                            if isinstance(fn_args, dict)
                            else fn_args,
                        }
                    )
                    messages.append(
                        {
                            "type": "function_call_output",
                            "call_id": call_id,
                            "output": result_text,
                        }
                    )

                logger.info(
                    f"[grep_agent_2] Round {_round + 1} — resolved {len(fn_calls)} search_documents call(s)"
                )

            if web_search:
                collect_openai_search_analytics(
                    response, analytics_collector, analytics_operation_id
                )

            output = response.model_dump().get("output", [])
            citations, url_to_snippet = _collect_openai_refinement_citations(output)

            return output[-1]["content"][0]["text"], citations, url_to_snippet
        except Exception as e:
            logger.error(f"Error with OpenAI API (grep-based refinement): {e!s}")
            logger.info("Falling back to Gemini API for grep-based refinement")

        # Gemini fallback: function calling with the same search_documents tool via the
        # Interactions API, which supports multi-round client-side function calling
        # analogous to the OpenAI loop above. Gemini has no equivalent of attaching an
        # OpenAI file_id, so the attached full-report context (file_id) is not available
        # here; the model relies on refine_prompt + search_documents results instead.
        gemini_retry_count = 0
        while gemini_retry_count < max_retries:
            try:
                logger.info(
                    f"Falling back to Gemini API for grep-based refinement (attempt {gemini_retry_count + 1}/{max_retries})"
                )
                gemini_client = genai.Client(api_key=GEMINI_API_KEY)

                gemini_tools = [grep_fn_tool]
                if web_search:
                    gemini_tools.append({"type": "google_search"})

                gemini_input = refine_prompt.strip()
                previous_interaction_id = None
                final_interaction = None
                max_tool_rounds = 5
                for _round in range(max_tool_rounds):
                    interaction = gemini_client.interactions.create(
                        model=GEMINI_ES_MODEL_ID,
                        input=gemini_input,
                        tools=gemini_tools,
                        previous_interaction_id=previous_interaction_id,
                        generation_config={"temperature": 0.1},
                    )
                    final_interaction = interaction

                    fn_calls = [
                        step
                        for step in interaction.steps
                        if step.type == "function_call" and step.name == "search_documents"
                    ]
                    if not fn_calls:
                        break

                    function_results = []
                    for fc in fn_calls:
                        fn_args = (
                            fc.arguments
                            if isinstance(fc.arguments, dict)
                            else json.loads(fc.arguments)
                        )
                        result_text = _grep_tool_handler(fn_args)
                        function_results.append(
                            {
                                "type": "function_result",
                                "name": "search_documents",
                                "call_id": fc.id,
                                "result": [{"type": "text", "text": result_text}],
                            }
                        )

                    logger.info(
                        f"[grep_agent_2][Gemini] Round {_round + 1} — resolved {len(fn_calls)} search_documents call(s)"
                    )
                    gemini_input = function_results
                    previous_interaction_id = interaction.id

                save_raw_llm_response(
                    final_interaction,
                    GEMINI_ES_MODEL_ID,
                    "Refining a report card using document search (backup)",
                    chat_id,
                    user_id=user_id,
                )

                gemini_citations = _extract_gemini_refine_citations(
                    final_interaction, final_interaction.output_text
                )
                return final_interaction.output_text, gemini_citations, {}
            except Exception as e:
                gemini_retry_count += 1
                logger.warning(
                    f"Gemini API attempt {gemini_retry_count}/{max_retries} failed (grep-based refinement): {e!s}"
                )
                if gemini_retry_count < max_retries:
                    logger.info(f"Retrying Gemini in {retry_delay} seconds...")
                    time.sleep(retry_delay)
                else:
                    logger.critical(
                        f"All Gemini API retry attempts failed (grep-based refinement): {e!s}"
                    )
                    return None, None, {}
    # --- End grep session branch ---

    retry_count = 0
    while retry_count < max_retries:
        try:
            logger.info(
                f"Trying OpenAI API for refinement (attempt {retry_count + 1}/{max_retries})"
            )

            refined_prompt = refine_prompt.strip()
            logger.info("Sending request to OpenAI API")

            user_messages = [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": f"Make Sure to use internet always. {refined_prompt}",
                        }
                    ],
                }
            ]

            # Initialize default values
            file_ids = []
            vector_store_id = None
            tools = [{"type": "web_search"}]

            # Check if upload_file_config is provided and configure accordingly
            if upload_file_config is not None:
                file_ids = upload_file_config.get("file_ids", [])
                vector_store_id = upload_file_config.get("vector_store_id")

                if vector_store_id and file_ids:
                    filters = {
                        "type": "and",
                        "filters": [
                            {
                                "type": "in",
                                "key": "file_id",
                                "value": file_ids,  # expecting list of file_ids
                            },
                            # {
                            #         "type": "in",
                            #         "key": "chat_id",
                            #         "value": user_id # expecting list of chat_ids
                            # }
                        ],
                    }
                    tools = [
                        {"type": "web_search"},
                        {
                            "type": "file_search",
                            "vector_store_ids": [vector_store_id],
                            "filters": filters,
                        },
                    ]
            if file_id:
                user_messages[0]["content"].append({"type": "input_file", "file_id": file_id})

            if web_search and not vector_store_id:
                response = SYNC_OPENAI_CLIENT.responses.parse(
                    model=model,
                    input=user_messages,
                    tools=[{"type": "web_search"}],
                    tool_choice={"type": "web_search"},
                    # text_format=RefineCard,
                    temperature=0.1,
                    include=["web_search_call.results"],
                )
                collect_openai_search_analytics(
                    response, analytics_collector, analytics_operation_id
                )
                save_raw_llm_response(
                    response,
                    model,
                    "Refining a report card using web research",
                    chat_id,
                    user_id=user_id,
                )
            elif not web_search and vector_store_id:
                response = SYNC_OPENAI_CLIENT.responses.parse(
                    model=model,
                    input=user_messages,
                    tools=tools,
                    tool_choice="auto",
                    # text_format=RefineCard,
                    temperature=0.1,
                    include=["web_search_call.results"],
                )
                collect_openai_search_analytics(
                    response, analytics_collector, analytics_operation_id
                )
                save_raw_llm_response(
                    response,
                    model,
                    "Refining a report card from uploaded documents",
                    chat_id,
                    user_id=user_id,
                )
            elif web_search and vector_store_id:
                response = SYNC_OPENAI_CLIENT.responses.parse(
                    model=model,
                    input=user_messages,
                    tools=tools,
                    tool_choice="required",
                    # text_format=RefineCard,
                    temperature=0.1,
                    include=["web_search_call.results"],
                )
                collect_openai_search_analytics(
                    response, analytics_collector, analytics_operation_id
                )
                save_raw_llm_response(
                    response,
                    model,
                    "Refining a report card using documents and web research",
                    chat_id,
                    user_id=user_id,
                )
            else:
                # Fallback case when no web_search and no vector_store_id
                response = SYNC_OPENAI_CLIENT.responses.parse(
                    model=model,
                    input=user_messages,
                    tools=[{"type": "web_search"}],
                    tool_choice={"type": "web_search"},
                    # text_format=RefineCard,
                    temperature=0.1,
                    include=["web_search_call.results"],
                )
                collect_openai_search_analytics(
                    response, analytics_collector, analytics_operation_id
                )
                save_raw_llm_response(
                    response, model, "Refining a report card", chat_id, user_id=user_id
                )

            output = response.model_dump().get("output", [])
            if web_search and not _output_used_web_search(output):
                logger.warning(
                    "Refinement response did not include a web_search_call step; "
                    "citations may be missing from the refined section"
                )
            citations, url_to_snippet = _collect_openai_refinement_citations(output)

            return output[-1]["content"][0]["text"], citations, url_to_snippet

        except Exception as e:
            retry_count += 1
            logger.warning(f"OpenAI API attempt {retry_count}/{max_retries} failed: {e!s}")
            logger.info(f"Error type: {type(e).__name__}")
            if retry_count < max_retries:
                logger.info(f"Retrying OpenAI in {retry_delay} seconds...")
                time.sleep(retry_delay)
            else:
                logger.critical(f"All OpenAI API retry attempts failed: {e!s}")
                break
    # DISABLED: Perplexity fallback, superseded by the Gemini fallback below
    # (kept for reference/rollback).
    # refine_schema_perplexity = REFINE_SCHEMA_PERPLEXITY
    # payload = {
    #     "model": "sonar-pro",
    #     "messages": [{"role": "user", "content": refine_prompt.strip()}],
    #     "response_format": {
    #         "type": "json_schema",
    #         "json_schema": {"schema": refine_schema_perplexity}
    #     },
    #     "temperature": 0.1,
    #     "max_tokens": 8000
    # }
    # headers = {
    #     "Authorization": f"Bearer {PERPLEXITY_API_KEY}",
    #     "Content-Type": "application/json"
    # }
    # resp = requests.post("https://api.perplexity.ai/chat/completions", headers=headers, json=payload)
    # result = json.loads(resp.json()["choices"][0]["message"]["content"])
    # return result['content'], resp.json()['citations'], {}

    # Fallback: Gemini API via the Interactions API (client.interactions.create),
    # which lets Gemini 3-series models combine google_search grounding with
    # structured JSON output in a SINGLE call (unlike the legacy generateContent
    # API used by client.models.generate_content, which can't mix tool use with
    # response_schema). Note: when the response is constrained to JSON, Gemini
    # does not attach per-source url_citation annotations the way it does for
    # plain-text grounded output, so gemini_citations will typically be empty
    # here even when the model did use the search tool.
    retry_count = 0
    while retry_count < max_retries:
        try:
            logger.info(f"Falling back to Gemini API (attempt {retry_count + 1}/{max_retries})")

            gemini_client = genai.Client(api_key=GEMINI_API_KEY)

            create_kwargs = {
                "model": GEMINI_ES_MODEL_ID,
                "input": refine_prompt.strip(),
                "response_format": {
                    "type": "text",
                    "mime_type": "application/json",
                    "schema": REFINE_SCHEMA_GEMINI,
                },
                "generation_config": {
                    "temperature": 0.1,
                    "max_output_tokens": 8000,
                    # "Thinking" tokens are billed/counted against max_output_tokens on
                    # this model; disabling them leaves the full budget for the actual
                    # JSON output and avoids silent truncation (JSONDecodeError).
                    "thinking_config": {"thinking_budget": 0},
                },
            }
            if web_search:
                create_kwargs["tools"] = [{"type": "google_search"}]

            interaction = gemini_client.interactions.create(**create_kwargs)
            save_raw_llm_response(
                interaction,
                GEMINI_ES_MODEL_ID,
                "Refining a report card (backup)",
                chat_id,
                user_id=user_id,
            )

            result = json.loads(strip_json_code_fence(interaction.output_text))
            content = result.get("content", "")
            gemini_citations = _extract_gemini_refine_citations(interaction, content)
            return content, gemini_citations, {}
        except Exception as e:
            retry_count += 1
            logger.warning(f"Gemini API attempt {retry_count}/{max_retries} failed: {e!s}")
            logger.info(f"Error type: {type(e).__name__}")
            if retry_count < max_retries:
                logger.info(f"Retrying Gemini in {retry_delay} seconds...")
                time.sleep(retry_delay)
            else:
                logger.critical(f"All Gemini API retry attempts failed: {e!s}")
                return None, None, {}


def replace_citations(
    cards_for_db, content, citation_urls, url_to_snippet: dict[str, str] | None = None
):
    """Replace citations in the content"""

    try:
        logger.info("Starting replace_citations")

        if url_to_snippet is None:
            url_to_snippet = {}

        def _build_citation_url(url: str) -> str:
            """Build the final citation URL, using text fragment if snippet is available."""
            if url in url_to_snippet:
                return generate_citation_url(url, url_to_snippet[url])
            return url

        # Pre-process content to fix broken URLs across lines
        def fix_broken_urls(text):
            """Fix URLs that are broken across multiple lines"""
            # Pattern to find potential broken URLs - looks for domain patterns that continue on next line
            # This handles cases like: "intermediation/www.imf.org/en/news/\narticles/..."
            broken_url_pattern = (
                r"(/(?:www\.|[a-zA-Z0-9-]+\.)[a-zA-Z0-9.-]+(?:/[^\s)]*)?)\s*\n\s*([^\s)]+)"
            )

            def url_fixer(match):
                part1 = match.group(1)
                part2 = match.group(2)
                # Join the parts without newline
                fixed_url = part1 + part2
                logger.info(f"Fixed broken URL: {part1}\\n{part2} -> {fixed_url}")
                return fixed_url

            # Fix broken URLs
            text = re.sub(broken_url_pattern, url_fixer, text)

            # Also fix cases where URL starts without protocol and is broken
            # Pattern for URLs starting with www. or domain that are broken across lines
            domain_broken_pattern = (
                r"((?:www\.|[a-zA-Z0-9-]+\.)[a-zA-Z0-9.-]+(?:/[^\s)]*)?)\s*\n\s*([^\s)]+)"
            )
            text = re.sub(domain_broken_pattern, url_fixer, text)

            return text

        # Fix broken URLs first
        updated_content = fix_broken_urls(content)

        # Repair [n](url] / [[n]](url) before any citation matching.
        updated_content = repair_malformed_citation_markdown(updated_content)

        all_unique_citations = {}
        for item in cards_for_db:
            all_unique_citations.update(item.get("citations", {}))
        logger.debug(f"Collected all_unique_citations: {all_unique_citations}")

        next_citation_number = len(all_unique_citations) + 1
        citation_dict = {}
        citation_urls = citation_urls or []

        logger.info("Building citation dictionary from citation_urls")
        for index, url in enumerate(citation_urls):
            # Clean URL using the clean_url function
            url = clean_url(url)
            logger.debug(f"Cleaned URL: {url}")

            if url in all_unique_citations:
                citation_dict[url] = all_unique_citations[url]
                logger.debug(f"URL already in all_unique_citations: {url} -> {citation_dict[url]}")
            else:
                citation_dict[url] = next_citation_number
                logger.debug(f"Adding new URL to citation_dict: {url} -> {next_citation_number}")
                next_citation_number += 1

        # First handle standard citation markers [n]
        pattern = r"\[(\d+)\](?!\()"

        def replacer(match):
            nonlocal next_citation_number
            index = int(match.group(1)) - 1
            if 0 <= index < len(citation_urls):
                url = clean_url(citation_urls[index])

                if url not in citation_dict:
                    citation_dict[url] = next_citation_number
                    logger.debug(
                        f"Numbered citation - new url assigned: {url} -> {next_citation_number}"
                    )
                    next_citation_number += 1

                citation_number = citation_dict[url]
                logger.debug(
                    f"Numbered citation - using citation_number: {citation_number} for url: {url}"
                )
                final_url = _build_citation_url(url)
                return f"[{citation_number}]({final_url})"
            return match.group(0)

        logger.info("Processing numbered citations [n]")
        updated_content = re.sub(pattern, replacer, updated_content)

        # Handle OpenAI format citations: ([text](url))
        openai_pattern = r"\(\[([^]]+)\]\(([^)]+)\)\)"

        def openai_replacer(match):
            nonlocal next_citation_number
            text = match.group(1)
            url = str(match.group(2))

            # Clean URL
            url = clean_url(url)
            logger.debug(f"OpenAI citation - cleaned URL: {url}")

            if url not in citation_dict:
                citation_dict[url] = next_citation_number
                logger.debug(f"OpenAI citation - new url assigned: {url} -> {next_citation_number}")
                next_citation_number += 1

            citation_number = citation_dict[url]
            logger.debug(
                f"OpenAI citation - using citation_number: {citation_number} for url: {url}"
            )
            final_url = _build_citation_url(url)
            return f"[{citation_number}]({final_url})"

        logger.info("Processing OpenAI format citations ([text](url))")
        updated_content = re.sub(openai_pattern, openai_replacer, updated_content)

        # Handle standard OpenAI format: (text)[url]
        standard_openai_pattern = r"\(([^)]+)\)\[([^]]+)\]"

        def standard_openai_replacer(match):
            nonlocal next_citation_number
            text = match.group(1)
            url = str(match.group(2))

            # Clean URL
            url = clean_url(url)
            logger.debug(f"Standard OpenAI citation - cleaned URL: {url}")

            if url not in citation_dict:
                citation_dict[url] = next_citation_number
                logger.debug(
                    f"Standard OpenAI citation - new url assigned: {url} -> {next_citation_number}"
                )
                next_citation_number += 1

            citation_number = citation_dict[url]
            logger.debug(
                f"Standard OpenAI citation - using citation_number: {citation_number} for url: {url}"
            )
            final_url = _build_citation_url(url)
            return f"[{citation_number}]({final_url})"

        logger.info("Processing standard OpenAI citations (text)[url]")
        updated_content = re.sub(standard_openai_pattern, standard_openai_replacer, updated_content)

        # Handle Markdown-style links [link_text](url) and convert to numbered citations
        logger.info("Processing markdown-style links for citation replacement")
        markdown_links_found = 0

        markdown_link_pattern = r"\[([^\]]+)\]\(([^)]+)\)"
        # Handle domain-only citations in parentheses: (domain.com/path)
        logger.info("Processing domain-only citations in parentheses")
        domain_citations_found = 0

        def domain_citation_replacer(match):
            nonlocal next_citation_number, domain_citations_found
            domain_citations_found += 1

            domain_path = match.group(1)
            # Convert to full URL by adding https://
            url = f"https://{domain_path}"

            # Clean URL
            url = clean_url(url)
            logger.debug(f"Domain citation - cleaned URL: {url}")

            if url not in citation_dict:
                citation_dict[url] = next_citation_number
                logger.debug(f"Domain citation - new url assigned: {url} -> {next_citation_number}")
                next_citation_number += 1

            citation_number = citation_dict[url]
            logger.debug(
                f"Domain citation - using citation_number: {citation_number} for url: {url}"
            )
            final_url = _build_citation_url(url)
            return f"[{citation_number}]({final_url})"

        # Pattern to match domain-only citations: (domain.com/path)
        # This matches domains with optional paths but no protocol
        domain_citation_pattern = r"\(([a-zA-Z0-9-]+\.[a-zA-Z]{2,}[^\s)]*)\)"
        updated_content = re.sub(domain_citation_pattern, domain_citation_replacer, updated_content)
        logger.info(f"Processed {domain_citations_found} domain-only citations")

        # Handle parenthetical citations with descriptive text: (descriptive text URL)
        logger.info("Processing parenthetical citations with descriptive text")
        descriptive_citations_found = 0

        markdown_link_pattern = r"\[([^\]]+)\]\(([^)]+)\)"

        def descriptive_citation_replacer(match):
            nonlocal next_citation_number, descriptive_citations_found
            descriptive_citations_found += 1

            full_content = match.group(1)
            # Extract URL from the end of the content
            url_match = re.search(r"(https?://[^\s)]+)$", full_content.strip())
            if url_match:
                url = url_match.group(1)
                url = str(url)

                # Clean URL
                url = clean_url(url)
                logger.debug(f"Descriptive citation - cleaned URL: {url}")

                if url not in citation_dict:
                    citation_dict[url] = next_citation_number
                    logger.debug(
                        f"Descriptive citation - new url assigned: {url} -> {next_citation_number}"
                    )
                    next_citation_number += 1

                citation_number = citation_dict[url]
                logger.debug(
                    f"Descriptive citation - using citation_number: {citation_number} for url: {url}"
                )
                final_url = _build_citation_url(url)
                return f"[{citation_number}]({final_url})"
            else:
                # No URL found, return original
                return match.group(0)

        # Pattern to match parenthetical citations with descriptive text and URL
        # This pattern captures content in parentheses that ends with a URL and contains descriptive text
        descriptive_citation_pattern = r"\(([^)]*[a-zA-Z][^)]*https?://[^)]+)\)"
        updated_content = re.sub(
            descriptive_citation_pattern, descriptive_citation_replacer, updated_content
        )
        logger.info(f"Processed {descriptive_citations_found} descriptive parenthetical citations")

        def markdown_link_replacer(match):
            nonlocal next_citation_number, markdown_links_found

            link_text = match.group(1)
            url = str(match.group(2))

            # Skip if link_text is already a pure number (already a citation)
            if link_text.strip().isdigit():
                logger.debug(f"Skipping markdown link with numeric text: [{link_text}]({url})")
                return match.group(0)

            # Only process if it's a citation link (contains domain.com)
            if re.search(r"[a-zA-Z0-9-]+\.[a-zA-Z]{2,}", url):
                markdown_links_found += 1

                # Clean URL
                url = clean_url(url)
                logger.debug(f"Markdown link - cleaned URL: {url}")

                # Check if URL already exists in citation_dict
                if url in citation_dict:
                    citation_number = citation_dict[url]
                    logger.info(
                        f"Found existing citation: [{link_text}] -> [{citation_number}]({url})"
                    )
                else:
                    # Assign new citation number
                    citation_dict[url] = next_citation_number
                    citation_number = next_citation_number
                    logger.info(
                        f"Created new citation: [{link_text}] -> [{citation_number}]({url})"
                    )
                    next_citation_number += 1

                # Replace link_text with citation number, preserve Markdown link structure
                final_url = _build_citation_url(url)
                return f"[{citation_number}]({final_url})"

            return match.group(0)  # Return unchanged if not a citation

        logger.info("Processing markdown link citations [text](url)")
        updated_content = re.sub(markdown_link_pattern, markdown_link_replacer, updated_content)
        logger.info(f"Processed {markdown_links_found} markdown-style links")

        # Handle plain URLs in parentheses: (url) - but not if preceded by ]
        # This handles pattern like "this is text cited from (url)"
        logger.info("Processing plain URLs in parentheses")
        plain_urls_found = 0

        plain_url_pattern = r"(?<!\])\((https?://[^)]+)\)"

        def plain_url_replacer(match):
            nonlocal next_citation_number, plain_urls_found
            plain_urls_found += 1

            url = str(match.group(1))

            # Clean URL
            url = clean_url(url)
            logger.debug(f"Plain URL - cleaned: {url}")

            # Check if URL already exists in citation_dict
            if url in citation_dict:
                citation_number = citation_dict[url]
                logger.info(f"Found existing citation for plain URL: [{citation_number}]({url})")
            else:
                # Assign new citation number
                citation_dict[url] = next_citation_number
                citation_number = next_citation_number
                logger.info(f"Created new citation for plain URL: [{citation_number}]({url})")
                next_citation_number += 1

            final_url = _build_citation_url(url)
            return f"[{citation_number}]({final_url})"

        # Pattern to match (url) but not when preceded by ] (to avoid matching markdown links)
        # This pattern matches plain URLs in parentheses that weren't caught by descriptive citations
        plain_url_pattern = r"(?<!\])\((https?://[^)\s]+)\)"
        updated_content = re.sub(plain_url_pattern, plain_url_replacer, updated_content)
        logger.info(f"Processed {plain_urls_found} plain URLs")

        # Handle malformed citations with partial URLs and double parentheses
        # Pattern: text/partial-url))
        logger.info("Processing malformed citations with partial URLs")
        malformed_citations_found = 0

        def malformed_citation_replacer(match):
            nonlocal next_citation_number, malformed_citations_found
            malformed_citations_found += 1

            content = match.group(1)
            # Try to extract URL-like content
            # Look for patterns like "text/www.domain.com/path" or "text/domain.com/path"
            url_match = re.search(r"/((?:www\.)?[a-zA-Z0-9-]+\.[a-zA-Z]{2,}[^\s)]*)", content)

            if url_match:
                domain_path = url_match.group(1)
                # Convert to full URL by adding https://
                url = f"https://{domain_path}"

                # Clean URL
                url = clean_url(url)
                logger.debug(f"Malformed citation - cleaned URL: {url}")

                # Check if URL already exists in citation_dict
                if url in citation_dict:
                    citation_number = citation_dict[url]
                    logger.info(
                        f"Found existing citation for malformed URL: [{citation_number}]({url})"
                    )
                else:
                    # Assign new citation number
                    citation_dict[url] = next_citation_number
                    citation_number = next_citation_number
                    logger.info(
                        f"Created new citation for malformed URL: [{citation_number}]({url})"
                    )
                    next_citation_number += 1

                # Extract the text before the URL
                text_before_url = content[: url_match.start()]
                final_url = _build_citation_url(url)
                return f"{text_before_url}[{citation_number}]({final_url})"
            else:
                # If no URL found, return original without the extra parentheses
                return content

        # Pattern to match malformed citations ending with ))
        malformed_pattern = r"([^)]+/[^\s)]+)\)\)"
        updated_content = re.sub(malformed_pattern, malformed_citation_replacer, updated_content)
        logger.info(f"Processed {malformed_citations_found} malformed citations")

        # Collapse a leftover bare numeric marker immediately followed by a linked
        # citation into just the linked citation. This fixes cases like
        # "[5]([Source](url))" which earlier steps turn into "[5][9](url)" — the
        # leading bare number is redundant and must be removed so we keep "[9](url)".
        # (A legitimate "[1](url)[2](url)" is untouched: the first ] is followed by
        # "(", not "[", so it never matches this bare-marker pattern.)
        prev_text = None
        while prev_text != updated_content:
            prev_text = updated_content
            updated_content = re.sub(r"\[\d+\](?=\[[^\]]+\]\([^)]+\))", "", updated_content)

        # Fix periods between multiple citations
        # Pattern: [n](url). followed by [m](url) -> [n](url) [m](url) (remove periods between)
        # Then ensure period is only after the last citation in a sequence
        def fix_citation_periods(text):
            # Step 1: Remove periods that appear immediately after any citation [text](url). or [number](url).
            # Handles both numbered and text-label citations
            pattern_between = r"(\[[^\]]+\]\([^)]+\))\.(\s*)(?=\[[^\]]+\]\([^)]+\))"
            prev_text = None
            while prev_text != text:
                prev_text = text
                text = re.sub(pattern_between, r"\1\2", text)

            # Step 2: Remove any trailing period immediately after a citation
            text = re.sub(r"(\[[^\]]+\]\([^)]+\))\.", r"\1", text)

            # Step 3: Fix any double periods that might have been created
            text = re.sub(r"\.\.+", ".", text)

            return text

        logger.info("Fixing citation periods")
        updated_content = fix_citation_periods(updated_content)

        # Fix spacing between closing parenthesis and opening bracket
        updated_content = re.sub(r"\)\[", ") [", updated_content)

        def _resolve_numeric_citation(num_str: str):
            idx = int(num_str) - 1
            if 0 <= idx < len(citation_urls):
                url = clean_url(citation_urls[idx])
                num = citation_dict.get(url) or all_unique_citations.get(url)
                if num is not None:
                    return str(num), _build_citation_url(url)
            for url, num in all_unique_citations.items():
                if num == int(num_str):
                    return str(num), _build_citation_url(url)
            for url, num in citation_dict.items():
                if num == int(num_str):
                    return str(num), _build_citation_url(url)
            return None

        updated_content = fix_numeric_url_citations(updated_content, _resolve_numeric_citation)

        logger.info("Citations replacement completed successfully")
        return updated_content, citation_dict

    except Exception as e:
        logger.error(f"Error replacing citations: {e!s}")
        return content, {}


def id_to_target(cards_for_db, fe_json_for_refine) -> tuple[str, str]:
    """Convert the id of the fe_json_for_refine to the target section and refine prompt
    Args:
        cards_for_db: The cards for the database
        fe_json_for_refine: The fe_json_for_refine for the refinement
    Returns:
        The target section and refine prompt
    """

    if "id" in fe_json_for_refine and fe_json_for_refine["refine_or_delete_prompt"] is not None:
        id = fe_json_for_refine["id"]
        refine_prompt = fe_json_for_refine["refine_or_delete_prompt"]
        for card in cards_for_db:
            if card["section"][0]["id"] == id:
                target_section = card["section"][0]["name"]
                return target_section, refine_prompt
            for sub_idx, sub_section in enumerate(card["sub_sections"]):
                if sub_section["id"] == id:
                    target_section = card["sub_sections"][sub_idx]["name"]
                    return target_section, refine_prompt
    if "subsection" in fe_json_for_refine and fe_json_for_refine["subsection"] is not None:
        subsection = fe_json_for_refine["subsection"]
        if (
            "id" in subsection
            and "refine_or_delete_prompt" in subsection
            and subsection["refine_or_delete_prompt"] is not None
        ):
            id = subsection["id"]
            refine_prompt = subsection["refine_or_delete_prompt"]
            for card in cards_for_db:
                if card["section"][0]["id"] == id:
                    target_section = card["section"][0]["name"]
                    return target_section, refine_prompt
                for sub_idx, sub_section in enumerate(card["sub_sections"]):
                    if sub_section["id"] == id:
                        target_section = card["sub_sections"][sub_idx]["name"]
                        return target_section, refine_prompt

    return "", ""


"""
Example of fe_json_for_refine:
{
    "id" : "f3b71d44-af8f-4545-8a6d-b603e5cee954",
    "refine_or_delete_prompt"  : "add a table here",
    "subsection" : {
        "id" : "d794b99b-6fc8-41c4-b4cf-7ff5b3fed1bd",
        "refine_or_delete_prompt" : "null"
    }
}
"""


def generate_refine_table_visualization(
    table: str,
    chat_id: str,
    aspect_ratio: str | None = "4:3",
    max_retries: int = 6,
    report_type: str = "study",
    user_id: str | None = None,
) -> str:
    """Generate refine table visualization with decision-based HTML first, then a Gemini fallback.

    Flow:
        1. For brief reports, skip very small tables (<= 3 rows and <= 2 columns)
        2. Run decision check (quantitative vs qualitative)
        3. If quantitative (True) -> html_graph_maker (charts)
        4. If qualitative (False) -> skip HTML infographics (disabled)
        5. If HTML fails -> Gemini image generation fallback

    Args:
        table: The table to visualize.
        chat_id: The id of the chat.
        aspect_ratio: The aspect ratio of the image.
        max_retries: The maximum number of retries.
        report_type: The report type ('study' or 'brief').
    Returns:
        HTML string for HTML visualizations, or image path for image-based ones.
    """

    if not table or not table.strip():
        logger.error("Table content is empty or None")
        return None

    if table.strip() in ["<table>", "</table>", "<table></table>"]:
        logger.error(f"Table contains only empty HTML tags: {table.strip()}")
        return None

    logger.info("Generating refine table visualization for table")

    if (report_type or "").lower() == "brief" and _is_small_table(table):
        logger.info("Skipping refine visualization for small table in brief report")
        return None

    # Step 1: Decision — is this table quantitative (suitable for charts) or not?
    is_quantitative = decide_if_table_needs_visualization(table, chat_id=chat_id, user_id=user_id)
    logger.info(f"Table visualization decision: is_quantitative={is_quantitative}")

    # Step 2: HTML visualization based on decision
    if is_quantitative:
        try:
            # raise Exception("test")
            logger.info("Decision=True: Trying HTML graph visualization (charts)")
            image_html = generate_html_visualization(table, chat_id=chat_id, user_id=user_id)
            if image_html is not None:
                logger.info("HTML graph visualization succeeded")
                return image_html.html_code
        except Exception as e:
            logger.error(f"HTML graph visualization failed: {e!s}")
    # Qualitative HTML visuals disabled — do not generate infographics for qualitative data
    # else:
    #     try:
    #         # raise Exception("test")
    #         logger.info("Decision=False: Trying infographic visualization")
    #         info_viz = generate_info_visualization(table, chat_id=chat_id, user_id=user_id)
    #         if info_viz is not None:
    #             logger.info(f"Infographic visualization succeeded: {info_viz.visualization_type}")
    #             return info_viz.html_code
    #     except Exception as e:
    #         logger.error(f"Infographic visualization failed: {str(e)}")
    else:
        logger.info("Decision=False: Skipping HTML infographic visualization for qualitative data")
        return None

    # Step 3: Fallback to Gemini image generation
    REFINE_VIZ_PROMPT = TABLE_TO_VIZ_PROMPT.format(table=table)
    try:
        # raise Exception("test")
        for attempt in range(1, max_retries + 1):
            api_key_num = ((attempt - 1) % 6) + 1
            Google_client = genai.Client(api_key=os.getenv(f"GEMINI_API_KEY_{api_key_num}"))
            try:
                logger.info(
                    f"Gemini image gen attempt {attempt}/{max_retries} using GEMINI_API_KEY_{api_key_num}"
                )
                interaction = Google_client.interactions.create(
                    model=VISUALIZATION_MODEL,
                    input=REFINE_VIZ_PROMPT,
                    response_format={
                        "type": "image",
                        "aspect_ratio": aspect_ratio,
                    },
                )
                save_raw_llm_response(
                    interaction,
                    VISUALIZATION_MODEL,
                    "Updating a chart image after card edits",
                    chat_id,
                    user_id=user_id,
                )

                if interaction.output_image is not None:
                    image_path = f"{uuid7()}.png"
                    with open(image_path, "wb") as f:
                        f.write(base64.b64decode(interaction.output_image.data))
                    logger.info(f"Generated image: {image_path}")
                    return image_path

            except Exception as e:
                logger.error(
                    f"Gemini attempt {attempt}/{max_retries} (GEMINI_API_KEY_{api_key_num}) failed: {e}"
                )
                if attempt == max_retries:
                    raise
                continue

    except Exception as e:
        logger.error(f"All Gemini attempts failed: {e!s}")

    # PandasAI fallback removed: pandasai has no build for Python 3.14 (see
    # pyproject requires-python). html_graph_maker + Gemini remain as the
    # visualization path; if both fail there is no chart for this table.
    return None


def generate_visualization(
    table: str,
    user_name: str,
    chat_id: str,
    report_type: str = "study",
    user_id: str | None = None,
):
    """
    Generate a visualization for a given table.
    Args:
        table: The table to generate a visualization for.
        user_name: The name of the user.
        chat_id: The id of the chat.
    Returns:
        The S3 path to the generated visualization (.png or .html), or empty string on failure.
    """

    # Validate input table content
    if not table or not table.strip():
        logger.error("Table content is empty or None")
        return None

    normalized_chat_id = (chat_id or "").strip()
    image_path = None
    html_temp_path = None
    try:
        image_path = generate_refine_table_visualization(
            table, chat_id, report_type=report_type, user_id=user_id
        )
        if image_path and isinstance(image_path, str) and not image_path.endswith(".png"):
            s3_controls = get_s3_instance()
            now = datetime.datetime.now()
            year = now.strftime("%Y")
            month = now.strftime("%m")
            day = now.strftime("%d")

            html_filename = f"{uuid7()}.html"
            html_temp_path = html_filename
            with open(html_temp_path, "w", encoding="utf-8") as f:
                f.write(image_path)

            s3_key = f"{S3_REPORTS_BASE_PATH}/visualizations/{year}/{month}/{day}/{user_name}/chat_{normalized_chat_id}/{html_filename}"
            s3_path = s3_controls.upload_file(html_temp_path, s3_key)
            if s3_path:
                logger.info(f"Uploaded HTML visualization to S3: {s3_path}")
                return s3_path
            else:
                logger.error("Failed to upload HTML visualization to S3")
                return image_path
        if image_path:
            s3_controls = get_s3_instance()
            now = datetime.datetime.now()
            year = now.strftime("%Y")
            month = now.strftime("%m")
            day = now.strftime("%d")
            s3_key = f"{S3_REPORTS_BASE_PATH}/visualizations/{year}/{month}/{day}/{user_name}/chat_{normalized_chat_id}/{image_path}"
            s3_path = s3_controls.upload_file(image_path, s3_key)
            if s3_path:
                logger.info(f"Uploaded image to S3: {s3_path}")
                return s3_path
            else:
                logger.error(f"Failed to upload image to S3: {s3_path}")
                return ""
    finally:
        if html_temp_path and os.path.exists(html_temp_path):
            os.remove(html_temp_path)
            logger.info(f"Deleted temp HTML file: {html_temp_path}")
        if image_path and os.path.exists(str(image_path)):
            os.remove(image_path)
            logger.info(f"Deleted image from local directory: {image_path}")
        if os.path.exists(f"temp_viz_dir_{chat_id}"):
            shutil.rmtree(f"temp_viz_dir_{chat_id}")
            logger.info(f"Deleted temporary directory: temp_viz_dir_{chat_id}")
    return ""


def refine_card(
    cards_for_db,
    fe_json_for_refine,
    user_name=None,
    chat_id=None,
    latest_version=None,
    refinement_history=[],
    file_id=None,
    upload_file_config: dict[str, Any] | None = None,
    user_id: str | None = None,
    web_search: bool | None = True,
    grep_session: PdfSession | None = None,
    report_type: str = "study",
    analytics_collector: list[dict] | None = None,
    analytics_operation_id: str | None = None,
) -> tuple[list[dict], dict, dict, list[dict]]:
    """
    Refine a card and return the updated cards, updated card, updated card table map, and refinement history
    Args:
        cards_for_db: The cards for the database
        fe_json_for_refine: The fe_json_for_refine for the refinement
        user_name: The name of the user
        chat_id: The id of the chat
        latest_version: The latest version of the card
        refinement_history: The refinement history for the card
        file_id: OpenAI file_id of the uploaded report (optional)
        upload_file_config: The upload file config
        user_id: The user id
        web_search: The web search flag
        grep_session: Optional PdfSession from grep_agent_2 for document search
        analytics_collector: Optional list passed straight through to
            generate_refined_content() to receive web-search analytics
            entries. No-op when None (default).
        analytics_operation_id: Optional id correlating analytics entries for
            this refinement operation.
    Returns:
        The updated cards, updated card, updated card table map, and refinement history
    """
    global s3_instance
    updated_card_table_map = {}
    _uid = user_id or user_name
    target_section, refine_prompt = id_to_target(cards_for_db, fe_json_for_refine)
    target = find_target_section_or_subsection(cards_for_db, target_section)

    # Validate that target was found
    if target is None:
        logger.error(
            f"Target section '{target_section}' not found in cards_for_db. Card ID may be invalid."
        )
        return cards_for_db, None, {}  # Return without modification

    original_content = _extract_content_string(target["content"])
    refined_content, citations, url_to_snippet = generate_refined_content(
        refine_prompt,
        original_content,
        refinement_history=refinement_history,
        file_id=file_id,
        upload_file_config=upload_file_config,
        user_id=_uid,
        web_search=web_search,
        grep_session=grep_session,
        chat_id=chat_id,
        analytics_collector=analytics_collector,
        analytics_operation_id=analytics_operation_id,
    )

    # Fix spelling, citation formatting, and table layout before further processing
    if refined_content:
        refined_content = fix_content(refined_content, chat_id=chat_id, user_id=_uid)

    # Update refinement history with the latest refinement
    # Check if this section was already refined - if so, update it; otherwise append
    section_already_refined = False
    for i, history_item in enumerate(refinement_history):
        if (
            history_item["name_of_the_section_or_subsection_that_user_previously_refined"]
            == target["name"]
        ):
            refinement_history[i] = {
                "refined_content_for_the_section_or_subsection": refined_content,
                "prompt_used_for_refinement": refine_prompt,
                "name_of_the_section_or_subsection_that_user_previously_refined": target["name"],
            }
            section_already_refined = True
            logger.info(f"Updated existing refinement history for section: {target['name']}")
            break

    if not section_already_refined:
        refinement_history.append(
            {
                "refined_content_for_the_section_or_subsection": refined_content,
                "prompt_used_for_refinement": refine_prompt,
                "name_of_the_section_or_subsection_that_user_previously_refined": target["name"],
            }
        )

        logger.info(f"Added new refinement to history for section: {target['name']}")

    try:
        now = datetime.datetime.now()
        year = now.strftime("%Y")
        month = now.strftime("%m")
        day = now.strftime("%d")
        normalized_chat_id = (chat_id or "").strip()
        if latest_version:
            card_name = f"{target_section}_Refined_version_{latest_version}"
        else:
            card_name = f"{target_section}_Refined_version"
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False, encoding="utf-8"
        ) as tmp_file:
            json.dump(
                {
                    "section": [
                        {"name": "Refined_version", "content": refined_content},
                    ]
                },
                tmp_file,
                indent=2,
                ensure_ascii=False,
            )
            card_temp_path = tmp_file.name
        if target["sub_idx"] is None:
            s3_key_card = f"{S3_REPORTS_BASE_PATH}/caspr_metadata_reports/{year}/{month}/{day}/{user_name}/chat_{normalized_chat_id}/Cards/Refined_versions/section/{card_name}.json"
        else:
            s3_key_card = f"{S3_REPORTS_BASE_PATH}/caspr_metadata_reports/{year}/{month}/{day}/{user_name}/chat_{normalized_chat_id}/Cards/Refined_versions/subsection/{card_name}.json"
        s3_instance.upload_file(card_temp_path, s3_key_card)
    except Exception as e:
        logger.error(f"Error uploading card to S3: {e}")

    updated_content, citations_dict = replace_citations(
        cards_for_db, refined_content, citations, url_to_snippet
    )
    updated_content = _enforce_numbered_citations(
        updated_content, complete_report_json=cards_for_db
    )
    updated_card_table_map = {}
    if target["sub_idx"] is None:
        if "section" in cards_for_db[target["idx"]]:
            for index, content in enumerate(cards_for_db[target["idx"]]["section"]):
                if content["name"] == target["name"]:
                    table_result = process_refined_tables(
                        previous_content=content.get("content", ""),
                        previous_tables=content.get("tables", []),
                        refined_content=updated_content,
                        user_prompt=refine_prompt,
                        extract_tables=extract_markdown_tables,
                        prepare_table=extract_title_and_description_for_table,
                        replace_table=replace_table_in_content,
                        auto_generate=lambda table: generate_visualization(
                            table, user_name, chat_id, report_type=report_type, user_id=_uid
                        ),
                        force_generate=lambda table, prompt, existing_viz: (
                            generate_forced_visualization(
                                table,
                                prompt,
                                user_name,
                                chat_id,
                                existing_viz=existing_viz,
                                user_id=_uid,
                            )
                        ),
                        intent_classifier=lambda prompt: classify_visualization_intent(
                            prompt,
                            chat_id=chat_id,
                            user_id=_uid,
                        ),
                        new_id=lambda: str(uuid7()),
                        default_visualization_type="table_graph",
                    )
                    updated_content = table_result.content
                    updated_card_table_map.update(table_result.table_markdown_map)
                    cards_for_db[target["idx"]]["section"][index]["content"] = updated_content
                    cards_for_db[target["idx"]]["section"][index]["tables"] = (
                        table_result.tables
                        or [
                            {
                                "visualization": "",
                                "table_id": "",
                                "table_title": "",
                                "visualization_type": "",
                            }
                        ]
                    )
                    break
    elif "sub_sections" in cards_for_db[target["idx"]]:
        for sub_index, sub_content in enumerate(cards_for_db[target["idx"]]["sub_sections"]):
            if sub_content["name"] == target["name"]:
                table_result = process_refined_tables(
                    previous_content=sub_content.get("content", ""),
                    previous_tables=sub_content.get("tables", []),
                    refined_content=updated_content,
                    user_prompt=refine_prompt,
                    extract_tables=extract_markdown_tables,
                    prepare_table=extract_title_and_description_for_table,
                    replace_table=replace_table_in_content,
                    auto_generate=lambda table: generate_visualization(
                        table, user_name, chat_id, report_type=report_type, user_id=_uid
                    ),
                    force_generate=lambda table, prompt, existing_viz: (
                        generate_forced_visualization(
                            table,
                            prompt,
                            user_name,
                            chat_id,
                            existing_viz=existing_viz,
                            user_id=_uid,
                        )
                    ),
                    intent_classifier=lambda prompt: classify_visualization_intent(
                        prompt,
                        chat_id=chat_id,
                        user_id=_uid,
                    ),
                    new_id=lambda: str(uuid7()),
                    default_visualization_type="table_graphs",
                )
                updated_content = table_result.content
                updated_card_table_map.update(table_result.table_markdown_map)
                cards_for_db[target["idx"]]["sub_sections"][sub_index]["content"] = updated_content
                cards_for_db[target["idx"]]["sub_sections"][sub_index]["tables"] = (
                    table_result.tables
                    or [
                        {
                            "visualization": "",
                            "table_id": "",
                            "table_title": "",
                            "visualization_type": "",
                        }
                    ]
                )
                break
    old_citations = cards_for_db[target["idx"]]["citations"]
    old_citations.update(citations_dict)
    updated_card = cards_for_db[target["idx"]]
    try:
        for index, item in enumerate(updated_card["section"]):
            try:
                section_content = item["content"]
                section_tables = extract_markdown_tables(section_content)
                if section_tables:
                    for table_index, table in enumerate(section_tables):
                        table_items = item.get("tables", [])
                        table_id = (
                            table_items[table_index].get("table_id")
                            if table_index < len(table_items)
                            else None
                        )

                        # If no table ID found, generate a new one
                        if not table_id:
                            table_id = str(uuid7())

                        # Store the table markdown with the table ID
                        updated_card_table_map[table_id] = table
                        # table_title = extract_title_and_description_for_table(section_content, table)
                #         viz_str = generate_visualization(table, dest_dir)
                #         for table_item in item['tables']:
                #             table_item['visualization'] = viz_str
                #             table_item['table_id'] = table_id
                #             table_item['table_title'] = table_title
                else:
                    logger.info(f"No tables found in section {item['name']}")
            except Exception as e:
                logger.error(
                    f"Error generating visualization for refined section {item['name']}: {e}"
                )
        for index, item in enumerate(updated_card["sub_sections"]):
            try:
                section_content = item["content"]
                section_tables = extract_markdown_tables(section_content)
                if section_tables:
                    for table_index, table in enumerate(section_tables):
                        table_items = item.get("tables", [])
                        table_id = (
                            table_items[table_index].get("table_id")
                            if table_index < len(table_items)
                            else None
                        )

                        # If no table ID found, generate a new one
                        if not table_id:
                            table_id = str(uuid7())

                        updated_card_table_map[table_id] = table
                        # table_title = await asyncio.to_thread(extract_title_and_description_for_table, section_content, table)
                        # viz_str = await asyncio.to_thread(generate_visualization, table, dest_dir)
                        # for table_item in item['tables']:
                        #     table_item['visualization'] = viz_str
                        #     table_item['table_id'] = table_id
                        #     table_item['table_title'] = table_title
                else:
                    logger.info(f"No tables found in sub_section {item['name']}")
            except Exception as e:
                logger.error(
                    f"Error generating visualization for refined sub_section {item['name']}: {e}"
                )
    except Exception as e:
        logger.error(f"Error generating visualization for refined section: {e}")

    section_content = ""
    section_content += updated_card["section"][0]["name"] + "\n"

    # Handle new JSONB content structure
    content_data = updated_card["section"][0]["content"]
    if isinstance(content_data, dict):
        # Extract the actual content string from the JSONB structure
        actual_content = content_data.get("content", "")
        if isinstance(actual_content, str):
            section_content += actual_content
        else:
            # If content is still not a string, convert to string
            section_content += str(actual_content)
    else:
        # Legacy string content
        section_content += content_data

    for idx, item in enumerate(updated_card["sub_sections"]):
        section_content += item["name"] + "\n"

        # Handle new JSONB content structure for subsections
        subsection_content_data = item["content"]
        if isinstance(subsection_content_data, dict):
            # Extract the actual content string from the JSONB structure
            actual_subsection_content = subsection_content_data.get("content", "")
            if isinstance(actual_subsection_content, str):
                section_content += actual_subsection_content
            else:
                # If content is still not a string, convert to string
                section_content += str(actual_subsection_content)
        else:
            # Legacy string content
            section_content += subsection_content_data

    summary = generate_section_summary(section_content)
    logger.info(f"updated summary: {summary}")
    updated_card["summary"] = summary
    updated_card = update_summaries_for_ask_caspr(updated_card)
    updated_cards = cards_for_db

    # Log the table map to help with debugging
    logger.info(f"Table map contains {len(updated_card_table_map)} entries")
    for table_id, markdown in updated_card_table_map.items():
        logger.info(f"Table ID: {table_id}, Markdown length: {len(markdown) if markdown else 0}")

    # Log table IDs in the updated card to verify they match the table map
    for section in updated_card.get("section", []):
        for table in section.get("tables", []):
            table_id = table.get("table_id")
            if table_id:
                if table_id in updated_card_table_map:
                    logger.info(f"Section table ID {table_id} found in table map")
                else:
                    logger.warning(f"Section table ID {table_id} NOT found in table map")

    for subsection in updated_card.get("sub_sections", []):
        for table in subsection.get("tables", []):
            table_id = table.get("table_id")
            if table_id:
                if table_id in updated_card_table_map:
                    logger.info(f"Subsection table ID {table_id} found in table map")
                else:
                    logger.warning(f"Subsection table ID {table_id} NOT found in table map")

    return updated_cards, updated_card, updated_card_table_map, refinement_history
