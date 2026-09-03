"""Table identity and visualization policy for card refinement."""

from dataclasses import dataclass
import logging
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

from pydantic import BaseModel, Field


logger = logging.getLogger(__name__)


class VisualizationRefinementIntent(BaseModel):
    visualization_action_requested: bool = Field(
        description=(
            "True only when the user asks to create, regenerate, or modify a "
            "table visualization."
        )
    )
    reason: str = Field(description="Brief reason for the classification.")


@dataclass(frozen=True)
class RefinedTableResult:
    content: str
    tables: List[Dict[str, Any]]
    table_markdown_map: Dict[str, str]


def classify_visualization_intent(
    user_prompt: Optional[str],
    *,
    chat_id: Optional[str] = None,
    user_id: Optional[str] = None,
    client: Any = None,
) -> bool:
    """Use GPT-4o mini structured output to classify visualization intent."""
    if not user_prompt or not user_prompt.strip():
        return False

    from app.core.constants import REFINE_TABLE_POLICY_MODEL, SYNC_OPENAI_CLIENT, GEMINI_API_KEY, GEMINI_REFINE_TABLE_POLICY_MODEL

    model_id = REFINE_TABLE_POLICY_MODEL
    should_log_response = client is None
    if client is None:
        client = SYNC_OPENAI_CLIENT

    system_prompt = """Classify the user's card-refinement instruction.

Return visualization_action_requested=true only when the user explicitly asks
to create, generate, regenerate, replace, or modify a visualization for a table.

Return false when the user:
- only asks to edit written content or table data,
- merely mentions, describes, or explains an existing chart,
- asks to preserve or not change a visualization.

For mixed instructions, return true if any visualization creation or
modification is explicitly requested.
"""

    try:
        response = client.responses.parse(
            model=model_id,
            input=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            text_format=VisualizationRefinementIntent,
            temperature=0,
        )
        parsed = response.output_parsed

        if should_log_response:
            try:
                from app.observability.llm_response_logger import save_raw_llm_response

                save_raw_llm_response(
                    response,
                    model_id,
                    "Classifying card refinement visualization intent",
                    chat_id,
                    user_id=user_id,
                )
            except Exception:
                logger.exception("Failed to save visualization intent LLM response")

        if parsed is None:
            raise ValueError("Visualization intent classifier returned no parsed result")
        return bool(parsed.visualization_action_requested)
    except Exception as e:
        logger.warning(f"OpenAI visualization intent classification failed ({e}); falling back to Gemini")
        try:
            from google import genai
            from app.observability.llm_response_logger import save_raw_llm_response, strip_json_code_fence
            import json

            gemini_client = genai.Client(api_key=GEMINI_API_KEY)
            interaction = gemini_client.interactions.create(
                model=GEMINI_REFINE_TABLE_POLICY_MODEL,
                input=user_prompt,
                system_instruction=system_prompt,
                response_format={
                    "type": "text",
                    "mime_type": "application/json",
                    "schema": VisualizationRefinementIntent.model_json_schema(),
                },
                generation_config={
                    "temperature": 0,
                    "thinking_config": {"thinking_budget": 0},
                },
            )
            try:
                save_raw_llm_response(
                    interaction,
                    GEMINI_REFINE_TABLE_POLICY_MODEL,
                    "Classifying card refinement visualization intent (backup)",
                    chat_id,
                    user_id=user_id,
                )
            except Exception:
                logger.exception("Failed to save Gemini visualization intent LLM response")

            result = json.loads(strip_json_code_fence(interaction.output_text))
            return bool(result.get("visualization_action_requested", False))
        except Exception:
            logger.exception("Visualization intent classification failed on both providers; defaulting to preserve")
            return False


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, Mapping):
        nested_content = content.get("content", "")
        return nested_content if isinstance(nested_content, str) else str(nested_content)
    return "" if content is None else str(content)


def _normalize_table(table: str) -> str:
    """Normalize formatting-only whitespace without changing table values."""
    normalized_lines = []
    for line in (table or "").strip().splitlines():
        cells = [cell.strip() for cell in line.strip().split("|")]
        normalized_lines.append("|".join(cells))
    return "\n".join(normalized_lines)


def _table_header(table: str) -> str:
    lines = (table or "").strip().splitlines()
    return _normalize_table(lines[0]) if lines else ""


def _safe_generate(generator: Callable[..., Optional[str]], *args: Any) -> str:
    try:
        return generator(*args) or ""
    except Exception:
        logger.exception("Table visualization generation failed")
        return ""


def process_refined_tables(
    *,
    previous_content: Any,
    previous_tables: Optional[Sequence[Mapping[str, Any]]],
    refined_content: str,
    user_prompt: Optional[str],
    extract_tables: Callable[[str], Sequence[str]],
    prepare_table: Callable[[str, str], Any],
    replace_table: Callable[[str, str, str], str],
    auto_generate: Callable[[str], Optional[str]],
    force_generate: Callable[[str, str, str], Optional[str]],
    intent_classifier: Callable[[Optional[str]], bool],
    new_id: Callable[[], str],
    default_visualization_type: str,
) -> RefinedTableResult:
    """Reconcile refined tables and apply the card-refinement visualization policy.

    Unchanged markdown preserves its table ID and visualization. Changed or new
    markdown receives a new ID. Visualization generation runs only for changed
    tables, missing visualizations, or explicit visualization requests. A failed
    regeneration never replaces a previous visualization with an empty value.
    """
    old_metadata = list(previous_tables or [])
    old_markdown = list(extract_tables(_content_text(previous_content)))
    new_markdown = list(extract_tables(refined_content))
    force_visualization = bool(new_markdown) and intent_classifier(user_prompt)

    old_by_normalized_markdown: Dict[str, List[int]] = {}
    for index, table in enumerate(old_markdown):
        old_by_normalized_markdown.setdefault(_normalize_table(table), []).append(index)
    old_by_header: Dict[str, List[int]] = {}
    for index, table in enumerate(old_markdown):
        old_by_header.setdefault(_table_header(table), []).append(index)

    exact_matches_by_new_index: Dict[int, int] = {}
    reserved_old_indexes = set()
    for new_index, table in enumerate(new_markdown):
        for candidate in old_by_normalized_markdown.get(_normalize_table(table), []):
            if candidate not in reserved_old_indexes:
                exact_matches_by_new_index[new_index] = candidate
                reserved_old_indexes.add(candidate)
                break

    used_old_indexes = set(reserved_old_indexes)
    processed_content = refined_content
    processed_tables: List[Dict[str, Any]] = []
    table_markdown_map: Dict[str, str] = {}

    for index, table in enumerate(new_markdown):
        matching_index = exact_matches_by_new_index.get(index)

        unchanged = matching_index is not None
        previous_index = matching_index
        if not unchanged:
            header_candidates = [
                candidate
                for candidate in old_by_header.get(_table_header(table), [])
                if candidate not in used_old_indexes
            ]
            if len(header_candidates) == 1:
                previous_index = header_candidates[0]
                used_old_indexes.add(previous_index)
            elif (
                len(old_markdown) == len(new_markdown)
                and index < len(old_metadata)
                and index not in used_old_indexes
                and _table_header(old_markdown[index]) == _table_header(table)
            ):
                previous_index = index
                used_old_indexes.add(previous_index)
            elif len(old_markdown) == len(new_markdown) == 1 and old_metadata:
                previous_index = 0
                used_old_indexes.add(previous_index)
        previous = (
            dict(old_metadata[previous_index])
            if previous_index is not None and previous_index < len(old_metadata)
            else {}
        )
        previous_visualization = previous.get("visualization") or ""

        if unchanged and previous.get("table_id"):
            table_id = str(previous["table_id"])
            table_title = previous.get("table_title") or ""
        else:
            table_id = new_id()
            try:
                table_title, updated_table = prepare_table(processed_content, table)
                processed_content = replace_table(processed_content, table, updated_table)
            except Exception:
                logger.exception("Table title preparation failed")
                table_title = previous.get("table_title") or ""

        should_generate = (
            force_visualization or not unchanged or not previous_visualization
        )
        visualization = previous_visualization
        if should_generate:
            if force_visualization:
                generated = _safe_generate(
                    force_generate,
                    table,
                    user_prompt or "",
                    previous_visualization,
                )
            else:
                generated = _safe_generate(auto_generate, table)
            visualization = generated or previous_visualization

        visualization_type = (
            previous.get("visualization_type")
            if unchanged and previous.get("visualization_type")
            else default_visualization_type
        )
        processed_tables.append({
            "visualization": visualization,
            "table_id": table_id,
            "table_title": table_title,
            "visualization_type": visualization_type,
        })
        table_markdown_map[table_id] = table

    return RefinedTableResult(
        content=processed_content,
        tables=processed_tables,
        table_markdown_map=table_markdown_map,
    )
