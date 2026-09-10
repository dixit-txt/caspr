import base64
import datetime
import html
import io
import json
import os
import re
import shutil
import time
import urllib.parse
from collections.abc import Generator
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

import markdown
import pandas as pd
from dotenv import load_dotenv
from google import genai
from langchain_core.prompts import ChatPromptTemplate
from openai import OpenAI
from pydantic import BaseModel, Field
from uuid_utils import uuid7

from app.adapters.grep_agent_2 import PdfSession
from app.adapters.grep_agent_2 import ask_pdfs as grep_ask_pdfs

# from app.core.constants import LLM
from app.adapters.s3 import S3_boto3_client, get_s3_instance
from app.agent.analyst_reasoning.schemas import REASONING_KEY
from app.cards.service_citations import generate_citation_url
from app.core.constants import (
    ANTHROPIC_LLM,
    ANTHROPIC_MODEL_ID,
    CARD_GEN_GEMINI_THINKING_LEVEL,
    CARD_GEN_MODEL_OPENAI,
    CARD_GEN_REASONING_EFFORT,
    CARD_SUMMARY_MODEL,
    COMPRESS_CONTEXT_SUMMARY_MODEL,
    DRL_GEN_MODEL,
    GEMINI_API_KEY,
    GEMINI_BRIEF_STREAM_MODEL,
    GEMINI_COMPRESS_CONTEXT_SUMMARY_MODEL,
    GEMINI_DRL_GEN_MODEL,
    GEMINI_ES_MODEL_ID,
    GEMINI_GENERATE_TITLE_FOR_TABLE_MODEL,
    GEMINI_PLOT_TYPE_MODEL,
    GEMINI_SUMMARY_MODEL,
    GENERATE_TITLE_FOR_TABLE_MODEL,
    OPENAI_API_KEY,
    PLOT_TYPE_MODEL,
    PRIORITIZE_ARXIV,
    REFINE_REPORT_LAYOUT_MODEL,
    S3_REPORTS_BASE_PATH,
    SYNC_OPENAI_CLIENT,
    VISUALIZATION_MODEL,
)
from app.agent.analyst_reasoning.url_cleaner import name_bare_citation_labels
from app.core.logging import setup_logging
from app.observability.llm_response_logger import save_raw_llm_response, strip_json_code_fence
from app.observability.web_search_analytics import (
    # collect_perplexity_search_analytics,  # DISABLED: superseded by Gemini fallback
    collect_gemini_search_analytics,
    collect_openai_search_analytics,
)
from app.research.prompts.dynamic_prompting import decide_if_table_needs_visualization
from app.research.prompts.prompt_utils import (
    CARD_SUMMARY_PROMPT,
    CARD_SUMMARY_SCHEMA,
    CARD_SUMMARY_SCHEMA_GEMINI,
    COMPRESS_CONTEXT_SUMMARY_PROMPT,
    COMPRESS_CONTEXT_SUMMARY_SCHEMA,
    COMPRESS_CONTEXT_SUMMARY_SCHEMA_GEMINI,
    DEFAULT_AUDIENCE_STYLE,
    DRL_JSON_SCHEMA_GEMINI,
    DRL_JSON_SCHEMA_OPENAI,
    DRL_PROMPT,
    MERGE_CUMULATIVE_SUMMARY_PROMPT,
    MERGE_CUMULATIVE_SUMMARY_SCHEMA,
    MERGE_CUMULATIVE_SUMMARY_SCHEMA_GEMINI,
    REFINE_CUMULATIVE_SUMMARY_PROMPT,
    REFINE_CUMULATIVE_SUMMARY_SCHEMA,
    REFINE_CUMULATIVE_SUMMARY_SCHEMA_GEMINI,
    REFINE_REPORT_LAYOUT_PROMPT,
    REFINE_REPORT_LAYOUT_SCHEMA,
    SECTION_OR_SUBSECTION_SUMMARY_PROMPT,
    TABLE_TO_VIZ_PROMPT,
    build_card_gen_prompt,
)
from app.research.visualization.html_graph_maker import generate_html_visualization

load_dotenv()

logger = setup_logging(__file__)


def clean_svg_for_weasyprint(svg_content: str) -> str:
    """
    Remove external resource references from SVG that WeasyPrint can't handle.
    """
    # Remove @import url() statements
    svg_content = re.sub(r"@import\s+url\([^)]+\);?", "", svg_content)

    # Remove empty style tags
    svg_content = re.sub(r"<style[^>]*>\s*</style>", "", svg_content)

    return svg_content


def image_to_base64_s3(image_path: str):
    """
    Convert an S3 image to base64.
    - Returns None if image_path is empty.
    - Expects s3://bucket/key format.
    """
    if not image_path:
        return None  # or '' if you prefer

    if not image_path.startswith("s3://"):
        raise ValueError("Only s3 paths are supported in this function.")

    try:
        # Parse bucket and key
        s3_path = image_path.replace("s3://", "")
        bucket_name, object_key = s3_path.split("/", 1)

        response = S3_boto3_client.get_object(Bucket=bucket_name, Key=object_key)
        image_bytes = response["Body"].read()
        ext = os.path.splitext(object_key)[1].lower().lstrip(".")

        if ext == "svg":
            logger.info("Cleaning SVG for WeasyPrint")
            svg_content = image_bytes.decode("utf-8")
            cleaned_svg = clean_svg_for_weasyprint(svg_content)
            encoded = base64.b64encode(cleaned_svg.encode("utf-8")).decode("utf-8")
            return f"![Visualization](data:image/svg+xml;base64,{encoded})"
        encoded = base64.b64encode(image_bytes).decode("utf-8")
        return f"![Visualization](data:image/{ext};base64,{encoded})"

    except Exception as e:
        logger.error(f"Failed to convert S3 image to base64: {e!s}")
        raise


class SubSection(BaseModel):
    name: str = Field(
        ...,
        description="Plain text sub-section title without any markdown symbols. e.g., 'Overview', 'Key Findings'. Do NOT include '#', '##', '###', bullets, or numbering.",
    )
    content: str = Field(
        ...,
        description=(
            "Detailed markdown content for this sub-section. Cite every fact, statistic "
            "or data point inline as a markdown link that NAMES the source: "
            "[Source Name](https://full-url). Never use bare numbered citations such as "
            "[1], [2] or [3] — the report renders named sources, and a numeric label is "
            "kept verbatim rather than resolved to a name."
        ),
    )


class SectionOutput(BaseModel):
    section: str = Field(
        ...,
        description="Plain text section name without any markdown symbols. e.g., 'Market Analysis', 'Industry Overview'. Do NOT include '#', '##', bullets, or numbering.",
    )
    content: str = Field(
        ...,
        description="This should be the guidance about what to include here about the above section",
    )
    sub_sections: list[SubSection]


class SubSectionLayout(BaseModel):
    name: str = Field(..., description="Sub-section title in markdown format, e.g., '- Overview'")
    description: str = Field(
        ..., description="Detailed description of what this sub-section should contain"
    )


class SectionLayout(BaseModel):
    section: str = Field(..., description="Section title in markdown format, e.g., '## ABCD'")
    sub_sections: list[SubSectionLayout] = Field(
        default_factory=list, description="List of sub-sections within this section"
    )


def count_report_layout_sections(report_layout: str) -> int:
    """
    Count the number of sections in the report layout.
    Sections are lines that start with '##' (markdown heading level 2).
    """
    lines = report_layout.strip().split("\n")
    section_count = sum(1 for line in lines if line.strip().startswith("##"))
    return section_count


def generate_drl(
    report_layout: str,
    user_instructions: str,
    report_type: str = "study",
    max_retries=3,
    retry_delay=5,
    latest_info_context: str = None,
    chat_id: str = None,
    user_id: str = None,
) -> dict:
    """
    Generate a descriptive report layout (DRL) for a given report layout and user instructions.
    Args:
        report_layout: The report layout in markdown format.
        user_instructions: The user instructions in markdown format.
        report_type: The report type ('study' or 'brief'). Brief reports are limited to 5 sections.
        max_retries: The maximum number of retries to generate the DRL.
        retry_delay: The delay in seconds between retries.
        latest_info_context: Optional real-time information retrieved via web search to ground the DRL in current facts.
    Returns:
        The descriptive report layout (DRL) in JSON format.
    """
    expected_section_count = count_report_layout_sections(report_layout)
    logger.info(f"Expected number of sections from report_layout: {expected_section_count}")

    # Augment user instructions with latest retrieved info if available
    augmented_instructions = user_instructions
    if latest_info_context:
        augmented_instructions = f"{user_instructions}\n\n--- LATEST RETRIEVED INFORMATION (use this as factual context for the report) ---\n{latest_info_context}\n--- END OF LATEST RETRIEVED INFORMATION ---"
        logger.info(
            f"Augmented user instructions with latest_info_context ({len(latest_info_context)} chars)"
        )

    max_regeneration_attempts = 3
    regeneration_attempt = 0

    while regeneration_attempt < max_regeneration_attempts:
        try:
            if regeneration_attempt > 0:
                logger.info(
                    f"Regeneration attempt {regeneration_attempt}/{max_regeneration_attempts - 1} for DRL due to section count mismatch"
                )

            logger.info("Attempting to generate DRL using OpenAI API")
            response = SYNC_OPENAI_CLIENT.chat.completions.create(
                model=DRL_GEN_MODEL,
                messages=[
                    {
                        "role": "user",
                        "content": DRL_PROMPT.format(
                            report_layout=report_layout,
                            user_instructions=augmented_instructions,
                            report_type=report_type.upper(),
                            current_date=datetime.datetime.now().strftime("%B %d, %Y"),
                        ),
                    }
                ],
                tools=[DRL_JSON_SCHEMA_OPENAI],
                tool_choice={
                    "type": "function",
                    "function": {"name": "generate_descriptive_report_layout"},
                },
            )
            save_raw_llm_response(
                response,
                DRL_GEN_MODEL,
                "Creating the detailed report outline",
                chat_id,
                user_id=user_id,
            )

            result = json.loads(response.choices[0].message.tool_calls[0].function.arguments)
            logger.info("Successfully received response from OpenAI API")
            # Changed by Naman
            # Filter out sections with empty section names
            drl = result["descriptive_report_layout"]
            filtered_drl = [section for section in drl if section.get("section", "").strip()]
            if len(filtered_drl) < len(drl):
                logger.warning(
                    f"Removed {len(drl) - len(filtered_drl)} sections with empty section names from DRL"
                )
            # Filter out subsections with empty names within each section
            for section in filtered_drl:
                if "sub_sections" in section:
                    original_sub_count = len(section["sub_sections"])
                    section["sub_sections"] = [
                        sub_section
                        for sub_section in section["sub_sections"]
                        if sub_section.get("name", "").strip()
                    ]
                    filtered_sub_count = len(section["sub_sections"])
                    if filtered_sub_count < original_sub_count:
                        logger.warning(
                            f"Removed {original_sub_count - filtered_sub_count} subsections with empty names from section: {section.get('section', 'Unknown')}"
                        )

            actual_section_count = len(filtered_drl)
            logger.info(f"Generated DRL has {actual_section_count} sections")

            if actual_section_count < expected_section_count:
                logger.warning(
                    f"DRL has fewer sections ({actual_section_count}) than report_layout ({expected_section_count})"
                )
                regeneration_attempt += 1
                if regeneration_attempt < max_regeneration_attempts:
                    logger.info(
                        f"Will regenerate DRL (attempt {regeneration_attempt + 1}/{max_regeneration_attempts})"
                    )
                    time.sleep(2)
                    continue
                else:
                    logger.warning(
                        f"Max regeneration attempts reached. Proceeding with DRL containing {actual_section_count} sections"
                    )
                    return filtered_drl
            else:
                logger.info(
                    f"DRL section count matches or exceeds expected count. Proceeding with {actual_section_count} sections"
                )
                return filtered_drl

        except Exception as e:
            logger.error(f"Error with OpenAI API: {e!s}")

            # DISABLED: Perplexity fallback, superseded by the Gemini fallback below
            # (kept for reference/rollback).
            # retry_count = 0
            # while retry_count < max_retries:
            #     try:
            #         logger.info(f"Falling back to Perplexity API (attempt {retry_count + 1}/{max_retries})")
            #         payload = {
            #             "model": "sonar-pro",
            #             "messages": [{"role": "user", "content": DRL_PROMPT.format(report_layout=report_layout, user_instructions=augmented_instructions, report_type=report_type.upper(), current_date=datetime.datetime.now().strftime('%B %d, %Y')).strip()}],
            #             "response_format": {
            #                 "type": "json_schema",
            #                 "json_schema": {"schema": DRL_JSON_SCHEMA_PERPLEXITY}
            #             },
            #             "temperature": 0.1,
            #             "max_tokens": 8000,
            #         }
            #         headers = {
            #             "Authorization": f"Bearer {PERPLEXITY_API_KEY}",
            #             "Content-Type": "application/json"
            #         }
            #         resp = requests.post("https://api.perplexity.ai/chat/completions", headers=headers, json=payload)
            #         if resp.status_code != 200:
            #             raise Exception()
            #         result = json.loads(resp.json()["choices"][0]["message"]["content"])
            #     except Exception as e:
            #         ...

            # Fallback: Gemini API with structured JSON output
            retry_count = 0
            while retry_count < max_retries:
                try:
                    logger.info(
                        f"Falling back to Gemini API (attempt {retry_count + 1}/{max_retries})"
                    )

                    gemini_client = genai.Client(api_key=GEMINI_API_KEY)
                    drl_prompt = DRL_PROMPT.format(
                        report_layout=report_layout,
                        user_instructions=augmented_instructions,
                        report_type=report_type.upper(),
                        current_date=datetime.datetime.now().strftime("%B %d, %Y"),
                    ).strip()

                    interaction = gemini_client.interactions.create(
                        model=GEMINI_DRL_GEN_MODEL,
                        input=drl_prompt,
                        response_format={
                            "type": "text",
                            "mime_type": "application/json",
                            "schema": DRL_JSON_SCHEMA_GEMINI,
                        },
                        generation_config={
                            "temperature": 0.1,
                            "max_output_tokens": 8000,
                            # "Thinking" tokens are billed/counted against max_output_tokens on
                            # this model; disabling them leaves the full budget for the actual
                            # JSON output and avoids silent truncation (JSONDecodeError).
                            "thinking_config": {"thinking_budget": 0},
                        },
                    )
                    save_raw_llm_response(
                        interaction,
                        GEMINI_DRL_GEN_MODEL,
                        "Creating the detailed report outline (backup)",
                        chat_id,
                        user_id=user_id,
                    )
                    result = json.loads(strip_json_code_fence(interaction.output_text))
                    logger.info("Successfully received response from Gemini API")

                    # Changed by Naman
                    # Filter out sections with empty section names
                    drl = result["descriptive_report_layout"]
                    filtered_drl = [
                        section for section in drl if section.get("section", "").strip()
                    ]
                    if len(filtered_drl) < len(drl):
                        logger.warning(
                            f"Removed {len(drl) - len(filtered_drl)} sections with empty section names from DRL"
                        )

                    # Filter out subsections with empty names within each section
                    for section in filtered_drl:
                        if "sub_sections" in section:
                            original_sub_count = len(section["sub_sections"])
                            section["sub_sections"] = [
                                sub_section
                                for sub_section in section["sub_sections"]
                                if sub_section.get("name", "").strip()
                            ]
                            filtered_sub_count = len(section["sub_sections"])
                            if filtered_sub_count < original_sub_count:
                                logger.warning(
                                    f"Removed {original_sub_count - filtered_sub_count} subsections with empty names from section: {section.get('section', 'Unknown')}"
                                )

                    # Check if DRL has fewer sections than expected
                    actual_section_count = len(filtered_drl)
                    logger.info(f"Generated DRL (from Gemini) has {actual_section_count} sections")

                    if actual_section_count < expected_section_count:
                        logger.warning(
                            f"DRL has fewer sections ({actual_section_count}) than report_layout ({expected_section_count})"
                        )
                        regeneration_attempt += 1
                        if regeneration_attempt < max_regeneration_attempts:
                            logger.info(
                                f"Will regenerate DRL using Gemini (attempt {regeneration_attempt + 1}/{max_regeneration_attempts})"
                            )
                            time.sleep(2)  # Small delay before regeneration
                            # Break from Gemini retry loop to retry from OpenAI
                            break
                        else:
                            logger.warning(
                                f"Max regeneration attempts reached. Proceeding with DRL containing {actual_section_count} sections"
                            )
                            return filtered_drl
                    else:
                        logger.info(
                            f"DRL section count matches or exceeds expected count. Proceeding with {actual_section_count} sections"
                        )
                        return filtered_drl

                except Exception as e:
                    retry_count += 1
                    logger.warning(f"Gemini API attempt {retry_count}/{max_retries} failed: {e!s}")
                    if retry_count < max_retries:
                        logger.info(f"Retrying in {retry_delay} seconds...")
                        time.sleep(retry_delay)
                    else:
                        logger.critical(f"All Gemini API retry attempts failed: {e!s}")
                        return {}

    # Fallback if loop completes without successful generation
    logger.critical("Failed to generate DRL after all attempts")
    return {}


def clean_drl_to_clean_rl(clean_drl):
    try:
        cleaned_rl = [{"title": clean_drl[0]["section"]}]

        for section_layout in clean_drl[1:]:
            # print(section_layout)
            cleaned_rl.append(
                {
                    "section": section_layout["section"],
                    "sub_sections": [
                        sub_section["name"] for sub_section in section_layout["sub_sections"]
                    ],
                }
            )

        cleaned_rl.insert(1, {"subtitle": ""})
        cleaned_rl.insert(2, {"table_of_contents": ""})
        cleaned_rl.insert(3, {"executive_summary": ""})

        return cleaned_rl

    except Exception as e:
        logger.error(f"Error cleaning DRL to Rl: {e!s}")
        return []


def parse_markdown_report_layout(report_layout):
    """Parse a proposed markdown report layout into the same cleaned report-layout
    (clean RL) structure that ``clean_drl_to_clean_rl`` produces — but purely by
    parsing the markdown, with NO LLM call.

    This is used during the feedback phase when the model proposes (or revises) a
    layout via the ``propose_report_layout`` tool, so the frontend can render a
    structured preview identical in shape to the one ``retrieve`` emits later.

    Input (the markdown the model proposes)::

        # Report Title
        ## 1. Section One
        - Subsection A
        - Subsection B
        ## 2. Section Two
        A one-line description (brief reports) — ignored as a subsection.

    Output::

        [
            {"title": "Report Title"},
            {"subtitle": ""},
            {"table_of_contents": ""},
            {"executive_summary": ""},
            {"section": "Section One", "sub_sections": ["Subsection A", "Subsection B"]},
            {"section": "Section Two", "sub_sections": []},
        ]
    """
    try:
        title = ""
        sections = []
        current = None

        for raw_line in (report_layout or "").splitlines():
            line = raw_line.strip()
            if not line:
                continue
            # Defensively drop horizontal rules / code fences if the model added them.
            if line in ("---", "***", "___") or line.startswith("```"):
                continue

            if line.startswith("## "):
                section_text = line[3:].strip()
                # Strip leading numbering like "1.", "1.2.", "## 3 " etc.
                section_text = re.sub(r"^#*\s*\d+(\.\d+)*\.?\s*", "", section_text).strip()
                if not section_text or section_text.lower() == "executive summary":
                    current = None
                    continue
                current = {"section": section_text, "sub_sections": []}
                sections.append(current)
            elif line.startswith("# "):
                if not title:
                    title = line[2:].strip()
            elif line[:2] in ("- ", "* ") or line.startswith("• "):
                if current is not None:
                    name = re.sub(r"^[-•*]\s*", "", line).strip()
                    name = re.sub(r"^#*\s*\d+(\.\d+)*\.?\s*", "", name).strip()
                    if name:
                        current["sub_sections"].append(name)
            # Plain text lines (brief one-liners, prose) are intentionally ignored.

        cleaned_rl = [
            {"title": title or "Report"},
            {"subtitle": ""},
            {"table_of_contents": ""},
            {"executive_summary": ""},
        ]
        for s in sections:
            cleaned_rl.append({"section": s["section"], "sub_sections": s["sub_sections"]})
        return cleaned_rl

    except Exception as e:
        logger.error(f"Error parsing markdown report layout: {e!s}")
        return []


def generate_report_layout(drl):
    """
    Takes a descriptive report layout (DRL) and removes all description fields
    while preserving the structure.
    """
    try:
        report_layout = []

        for idx, section in enumerate(drl):
            section_text = section["section"].replace("#", "").strip()

            if idx == 0:
                cleaned_section = {"title": section_text}
            else:
                # Remove section numbers (e.g. "1.", "1.2", etc)
                # First check for numbers with dots
                while section_text.split(".")[0].strip().isdigit() and "." in section_text:
                    section_text = " ".join(section_text.split(".")[1:]).strip()

                # Then check for standalone numbers at start
                if section_text.split()[0].isdigit():
                    section_text = " ".join(section_text.split()[1:]).strip()
                # section_text = ' '.join(section_text.split()[1:]) if section_text.split()[0][0].isdigit() else section_text

                cleaned_section = {"section": section_text, "sub_sections": []}

                for sub_section in section.get("sub_sections", []):
                    name = sub_section["name"].replace("- ", "", 1)
                    cleaned_section["sub_sections"].append(name)
            report_layout.append(cleaned_section)

        report_layout.insert(1, {"subtitle": ""})
        report_layout.insert(2, {"table_of_contents": ""})
        report_layout.insert(3, {"executive_summary": ""})
        return report_layout

    except Exception as e:
        logger.error(f"Error generating report layout: {e!s}")
        return []


def clean_drl(drl):
    """
    Takes a descriptive report layout (DRL) and cleans section titles by removing '#', '-', and numeric prefixes
    while preserving the structure and sub-section details.
    """
    try:
        cleaned_drl = []

        for idx, section in enumerate(drl):
            # Clean section title by removing '#', numbers, and leading/trailing whitespace
            section_text = section["section"]
            section_text = section_text.replace("#", "").strip()

            # Remove section numbers using regex (handles "1.", "1.2.", "10.", "12.3", remove from the beginning of the string only not from the middle or end etc.)
            # This pattern matches: start of string, one or more digits, optional (dot + digits) pattern, optional final dot, optional spaces
            section_text = re.sub(r"^\d+(\.\d+)*\.?\s*", "", section_text).strip()

            # Skip if section is "executive summary" (case insensitive)
            if section_text.lower() == "executive summary":
                continue
            # Create cleaned section dictionary
            cleaned_section = {"section": section_text}
            # Add description if it exists
            if "description" in section:
                cleaned_section["description"] = section["description"].strip("#").strip()

            # Pass through the LLM-authored heartbeat lines for this section as-is
            if "heartbeat" in section:
                cleaned_section["heartbeat"] = section["heartbeat"]

            cleaned_section["sub_sections"] = []

            # Clean and add sub-sections if they exist
            if "sub_sections" in section:
                for sub_section in section["sub_sections"]:
                    # First remove bullet points (-, •, *) from sub-section name
                    cleaned_name = re.sub(r"^[-•*]\s*", "", sub_section["name"]).strip()

                    # Then remove numeric prefixes like "1.1", "10.2", "12.3", "# 1.", "## 1.2", "1.2.3.", "## 1.2.3." remove from the beginning of the string only not from the middle or end etc.
                    cleaned_name = re.sub(r"^#*\s*\d+(\.\d+)*\.?\s*", "", cleaned_name).strip()

                    cleaned_sub_section = {
                        "name": cleaned_name,
                        "description": sub_section["description"],
                    }
                    cleaned_section["sub_sections"].append(cleaned_sub_section)

            cleaned_drl.append(cleaned_section)

        return cleaned_drl

    except Exception as e:
        logger.error(f"Error cleaning descriptive report layout: {e}")
        return []


def extract_title_and_toc(descriptive_report_layout):
    try:
        title = descriptive_report_layout[0]["section"]
        subtitle = descriptive_report_layout[0]["sub_sections"][0]["description"]
        table_of_contents = ""
        for idx, section in enumerate(descriptive_report_layout[1:]):
            section_name = section["section"].lstrip("#").strip()
            section_name = section_name.replace("|", "-")
            modified_section_name = section_name.replace(" ", "-").strip()
            table_of_contents += (
                f"[{idx + 1}. {section_name}](#{idx + 1}.-{modified_section_name})\n"
            )
            for sub_idx, sub_section in enumerate(section["sub_sections"]):
                sub_section_name = sub_section["name"].lstrip("#").strip()
                sub_section_name = sub_section_name.replace("|", "-")
                sub_section_name = sub_section_name.lstrip("- ").strip()
                modified_sub_section_name = sub_section_name.lstrip("- ").strip()
                modified_sub_section_name = modified_sub_section_name.replace(" ", "-")
                table_of_contents += f"[{idx + 1}.{sub_idx + 1}. {sub_section_name}](#{idx + 1}.{sub_idx + 1}.-{modified_sub_section_name})\n"

        return table_of_contents, title, subtitle
    except Exception as e:
        logger.error(f"Error extracting title and toc: {e}")
        return "", "", ""


def build_highlight_urls(data):
    highlight_urls = []

    for item in data:
        url = item.get("url")
        snippet = item.get("snippet")

        if not url or not snippet:
            continue

        # Normalize snippet
        snippet = re.sub(r"\s+", " ", snippet).strip()

        # Cut to safe length (text fragments are fragile)
        snippet = snippet[:120]

        # Avoid cutting mid-word
        if " " in snippet:
            snippet = snippet.rsplit(" ", 1)[0]

        # URL encode snippet
        encoded_snippet = urllib.parse.quote(snippet, safe="")

        # Build text fragment URL
        highlight_url = f"{url}#:~:text={encoded_snippet}"

        highlight_urls.append(highlight_url)

    return highlight_urls


def _is_context_length_error(error: Exception) -> bool:
    """Check if an exception is a context/token limit error from the API."""
    error_str = str(error).lower()
    context_keywords = [
        "context_length_exceeded",
        "context length",
        "maximum context",
        "token limit",
        "max_tokens",
        "too many tokens",
        "input is too long",
        "request too large",
        "maximum number of tokens",
        "reduce the length",
        "input too long",
    ]
    return any(kw in error_str for kw in context_keywords)


def compress_context_summary(
    raw_summaries: str,
    max_retries: int = 3,
    retry_delay: int = 5,
    chat_id: str = None,
    user_id: str = None,
) -> str:
    """Compress concatenated section summaries into a concise context block (400-600 words).

    Unlike refine_cumulative_summary (which produces an executive summary in narrative form),
    this function produces a structured bullet-point summary optimized for use as context
    in subsequent card generation prompts.
    """
    if not raw_summaries or not raw_summaries.strip():
        return ""

    formatted_prompt = COMPRESS_CONTEXT_SUMMARY_PROMPT.format(raw_summaries=raw_summaries)

    for attempt in range(1, max_retries + 1):
        try:
            logger.info(f"Compressing context summary (attempt {attempt}/{max_retries})")
            response = SYNC_OPENAI_CLIENT.chat.completions.create(
                model=COMPRESS_CONTEXT_SUMMARY_MODEL,
                messages=[{"role": "user", "content": formatted_prompt}],
                tools=[COMPRESS_CONTEXT_SUMMARY_SCHEMA],
                tool_choice={"type": "function", "function": {"name": "compress_context_summary"}},
                max_tokens=1200,
            )
            save_raw_llm_response(
                response,
                COMPRESS_CONTEXT_SUMMARY_MODEL,
                "Condensing earlier section summaries for later writing",
                chat_id,
                user_id=user_id,
            )
            result = json.loads(response.choices[0].message.tool_calls[0].function.arguments)
            compressed = result["compressed_summary"]
            logger.info(
                f"Context summary compressed: {len(raw_summaries)} chars -> {len(compressed)} chars"
            )
            return compressed
        except Exception as e:
            logger.error(f"compress_context_summary attempt {attempt}/{max_retries} failed: {e}")
            if attempt < max_retries:
                time.sleep(retry_delay)

    logger.info(
        "OpenAI attempts exhausted for compress_context_summary, falling back to Gemini API"
    )
    try:
        gemini_client = genai.Client(api_key=GEMINI_API_KEY)
        interaction = gemini_client.interactions.create(
            model=GEMINI_COMPRESS_CONTEXT_SUMMARY_MODEL,
            input=formatted_prompt,
            response_format={
                "type": "text",
                "mime_type": "application/json",
                "schema": COMPRESS_CONTEXT_SUMMARY_SCHEMA_GEMINI,
            },
            generation_config={
                "temperature": 0.1,
                "max_output_tokens": 3000,
                "thinking_config": {"thinking_budget": 0},
            },
        )
        save_raw_llm_response(
            interaction,
            GEMINI_COMPRESS_CONTEXT_SUMMARY_MODEL,
            "Condensing earlier section summaries for later writing (backup)",
            chat_id,
            user_id=user_id,
        )
        result = json.loads(strip_json_code_fence(interaction.output_text))
        compressed = result["compressed_summary"]
        logger.info(
            f"Context summary compressed via Gemini fallback: {len(raw_summaries)} chars -> {len(compressed)} chars"
        )
        return compressed
    except Exception as e:
        logger.error(f"Gemini fallback for compress_context_summary also failed: {e}")

    logger.warning("All compress_context_summary attempts failed, returning raw summaries")
    return raw_summaries


def _build_cumulative_summary_from_cards(
    previous_cards: list,
    chat_id: str = None,
    user_id: str = None,
) -> str:
    """Build a compressed cumulative summary from previous cards' summaries.

    Concatenates individual card summaries then compresses them into a structured
    bullet-point context block via LLM, ensuring it stays well within context limits."""
    summaries = []
    for i, card in enumerate(previous_cards, 1):
        section_name = ""
        if card.get("section") and isinstance(card["section"], list) and card["section"]:
            section_name = card["section"][0].get("name", "")

        card_summary = card.get("summary", "")
        if card_summary:
            summaries.append(f"Section {i} ({section_name}): {card_summary}")

    raw_combined = "\n\n".join(summaries)

    if not raw_combined:
        return ""

    compressed = compress_context_summary(raw_combined, chat_id=chat_id, user_id=user_id)
    logger.info(
        f"Cumulative summary fallback: {len(raw_combined)} chars -> {len(compressed)} chars"
    )
    return compressed


def _build_previous_cards_context(previous_cards: list) -> str:
    """Convert a list of previously generated card dicts into a markdown string
    suitable for inclusion in the prompt as full context."""
    parts = []
    for i, card in enumerate(previous_cards, 1):
        section_name = ""
        section_content = ""
        if card.get("section") and isinstance(card["section"], list) and card["section"]:
            section_name = card["section"][0].get("name", "")
            section_content = card["section"][0].get("content", "")

        parts.append(f"## Section {i}: {section_name}")
        if section_content:
            parts.append(section_content)

        for sub in card.get("sub_sections", []):
            sub_name = sub.get("name", "")
            sub_content = sub.get("content", "")
            if sub_name:
                parts.append(f"### {sub_name}")
            if sub_content:
                parts.append(sub_content)

        parts.append("")  # blank line separator between cards

    return "\n".join(parts)


def _extract_citations_from_response(response) -> list:
    """
    Extract web search citations from an OpenAI Responses API response.

    When text_format (structured output) is used, the annotations field on the
    message content is empty due to a known API limitation. This function works
    around it by:
    1. Checking annotations on the last message output (works without text_format)
    2. If empty, collecting URLs from web_search_call output items that have results
    3. If still empty, scanning all output items for any annotations
    """
    response_dict = response.model_dump()
    output_items = response_dict.get("output", [])

    # Step 1: Try annotations on the last message content (standard path)
    last_message = None
    for item in reversed(output_items):
        if item.get("type") == "message":
            last_message = item
            break

    if last_message:
        content_list = last_message.get("content", [])
        if content_list:
            annotations = content_list[0].get("annotations", [])
            if annotations:
                return annotations

    # Step 2: Collect URLs from web_search_call items that have results
    citations = []
    seen_urls = set()
    for item in output_items:
        if item.get("type") == "web_search_call":
            results = item.get("results", [])
            for result in results:
                url = result.get("url", "")
                title = result.get("title", "")
                snippet = result.get("snippet", "")
                if url and url not in seen_urls:
                    seen_urls.add(url)
                    citations.append(
                        {
                            "type": "url_citation",
                            "url": url,
                            "title": title,
                            "snippet": snippet,
                        }
                    )

    if citations:
        return citations

    # Step 3: Scan all message output items for annotations
    for item in output_items:
        if item.get("type") == "message":
            for content_block in item.get("content", []):
                for ann in content_block.get("annotations", []):
                    if ann.get("url") and ann["url"] not in seen_urls:
                        seen_urls.add(ann["url"])
                        citations.append(ann)

    return citations


def generate_cards(
    card_layout,
    cumulative_summary="",
    is_first_section=False,
    descriptive_report_layout=None,
    max_retries=3,
    retry_delay=5,
    user_instructions: str = None,
    report_length: str = "OVERVIEW",
    upload_file_config: dict = None,
    web_search: bool = True,
    external_tools: list = None,
    previous_cards: list = None,
    grep_session: PdfSession | None = None,
    chat_id: str = None,
    user_id: str = None,
    analytics_collector: list[dict] | None = None,
    analytics_operation_id: str | None = None,
    prioritize_arxiv: bool = PRIORITIZE_ARXIV,
    style: str = DEFAULT_AUDIENCE_STYLE,
):
    """
    Generate cards for a given section.

    Args:
        card_layout: The card layout in JSON format.
        cumulative_summary: (DEPRECATED/commented out) The cumulative summary of the section.
        is_first_section: Whether the section is the first section.
        descriptive_report_layout: The descriptive report layout in JSON format.
        max_retries: The maximum number of retries to generate the cards.
        retry_delay: The delay in seconds between retries.
        user_instructions: The user instructions in markdown format.
        report_length: The report length in markdown format.
        previous_cards: List of previously generated card dicts to use as full context.
        external_tools: Optional list of OpenAI function tool definitions to add
            alongside web_search/file_search. Each entry should be a dict with
            ``{"type": "function", "function": {...}}``.  When provided, these
            tools are included in the API call and the model can invoke them.
            Requires an ``external_tool_handler`` callable on each dict under
            the key ``_handler`` (popped before sending to OpenAI).
        prioritize_arxiv: When True, and this section uses web search with no
            uploaded document (Case 1), instructs the model to spend its
            first few web searches scoped to arxiv.org before falling back
            to the broader web, and caps the total web search calls at 10
            via ``max_tool_calls``. No effect on the file/vector-store or
            grep/external-tools branches.
    Returns:
        The cards in JSON format.
    """
    logger.info(f"Generating cards for section: {card_layout.get('section', 'Unknown')}")
    sub_sections_str = ""

    current_date = datetime.datetime.now().strftime("%B %d, %Y")

    # raise Exception("test")
    for sub_section in card_layout["sub_sections"]:
        sub_sections_str += f"### {sub_section['name']}\n{sub_section['description']}\n\n"

    target_section = card_layout["section"]
    section_description = card_layout["description"]

    logger.info(f"Target section: {target_section}")

    # Build previous cards content string from full cards
    previous_cards_content = None
    if not is_first_section and previous_cards:
        previous_cards_content = _build_previous_cards_context(previous_cards)

    prompt = build_card_gen_prompt(
        target_section=target_section,
        section_description=section_description,
        sub_sections_str=sub_sections_str,
        descriptive_report_layout=descriptive_report_layout,
        current_date=current_date,
        user_instructions=user_instructions,
        report_length=report_length,
        # cumulative_summary=cumulative_summary if not is_first_section else None,
        previous_cards_content=previous_cards_content,
        has_file=bool(upload_file_config) or bool(grep_session),
        prioritize_arxiv=prioritize_arxiv,
        style=style,
    )
    logger.info(
        f"Built prompt for section '{target_section}' (first={is_first_section}, has_file={bool(upload_file_config)}, has_grep_session={bool(grep_session)}, prioritize_arxiv={prioritize_arxiv})"
    )

    # Extract file config values if available
    file_ids = upload_file_config.get("file_ids") if upload_file_config else None
    vector_store_id = upload_file_config.get("vector_store_id") if upload_file_config else None
    refined_prompt = prompt.strip()
    _used_context_fallback = False
    analytics_operation_id = analytics_operation_id or str(uuid7())

    openai_max_retries = 3
    for openai_attempt in range(1, openai_max_retries + 1):
        try:
            # raise Exception("test")
            logger.info(
                f"Sending request to OpenAI API (attempt {openai_attempt}/{openai_max_retries})"
            )

            # Case 1: Web search only (no file)
            if web_search and not upload_file_config:
                logger.info(
                    f"Generating cards for section: {target_section} with web search only (prioritize_arxiv={prioritize_arxiv})"
                )
                system_content = f"You are a precise researcher and technical writer. You must always perform a web search when providing facts, statistics, or statements about current data or real-world technical information. After performing the search, produce your answer: structured, clear, and factual. If you cannot find a reliable up-to-date source in your search, you must state that explicitly rather than speculate. The answer should be tailored to the date of {current_date}."
                call_kwargs = {}
                if prioritize_arxiv:
                    system_content += (
                        " You have up to 10 total web search calls available for this task. "
                        "Spend your first 2-3 searches scoped to arxiv.org (using 'site:arxiv.org' in the query) "
                        "to find relevant papers before searching the broader web — see the ARXIV-FIRST SEARCH STRATEGY below for the full policy."
                    )
                    call_kwargs["max_tool_calls"] = 10
                    call_kwargs["include"] = [
                        "web_search_call.results",
                        "web_search_call.action.sources",
                    ]
                else:
                    call_kwargs["include"] = ["web_search_call.results"]
                response = SYNC_OPENAI_CLIENT.responses.parse(
                    model=CARD_GEN_MODEL_OPENAI,
                    input=[
                        {"role": "system", "content": system_content},
                        {"role": "user", "content": refined_prompt},
                    ],
                    tools=[{"type": "web_search"}],
                    tool_choice={"type": "web_search"},
                    text_format=SectionOutput,
                    reasoning={"effort": CARD_GEN_REASONING_EFFORT},
                    **call_kwargs,
                )
                collect_openai_search_analytics(
                    response, analytics_collector, analytics_operation_id
                )
                save_raw_llm_response(
                    response,
                    CARD_GEN_MODEL_OPENAI,
                    "Writing report section content using web research",
                    chat_id,
                    user_id=user_id,
                )

            # Case 2: Web search + Vector Store
            elif web_search and vector_store_id:
                logger.info(
                    f"Generating cards for section: {target_section} with web search and vector store only"
                )
                # Build filters to retrieve from specific file and vector store
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

                response = SYNC_OPENAI_CLIENT.responses.parse(
                    model=CARD_GEN_MODEL_OPENAI,
                    input=[
                        {
                            "role": "system",
                            "content": (
                                "You are a precise researcher and technical writer. "
                                "You have access to an uploaded document via file search and to web search. "
                                "You MUST use BOTH tools for every response: "
                                "1) Use file search to retrieve relevant content from the uploaded document. "
                                "2) Use web search to supplement with current data, verify figures, and add context the document may not cover. "
                                "The uploaded document is your primary source — base your content on it, but always enrich it with web search results. "
                                "Never contradict the document with web-sourced information. "
                                "CRITICAL CITATION RULES: "
                                "For document-sourced facts, use plain text: 'Source: <document title or topic> (uploaded document)'. "
                                "Do NOT invent an author name. Do NOT use 'Caspr Research', 'Ghost Research', 'Caspr', or any brand name as the source author. "
                                "For web-sourced facts, use the actual full page URL returned by web search as a markdown link. "
                                "Do NOT fabricate URLs. Never use placeholder URLs, made-up domains, or illustrative example links. "
                                "Never use base-domain-only URLs — always use the full specific page URL from search results. "
                                f"The answer should be tailored to the date of {current_date}."
                            ),
                        },
                        {"role": "user", "content": refined_prompt},
                    ],
                    tools=[
                        {"type": "web_search"},
                        {
                            "type": "file_search",
                            "vector_store_ids": [vector_store_id],
                            "filters": filters,
                            "ranking_options": {"ranker": "auto"},
                        },
                    ],
                    tool_choice="required",
                    text_format=SectionOutput,
                    reasoning={"effort": CARD_GEN_REASONING_EFFORT},
                    include=["web_search_call.results"],
                )
                collect_openai_search_analytics(
                    response, analytics_collector, analytics_operation_id
                )
                save_raw_llm_response(
                    response,
                    CARD_GEN_MODEL_OPENAI,
                    "Writing report section content using uploaded documents and web research",
                    chat_id,
                    user_id=user_id,
                )

            # # Case 3: Web search + Traditional File
            # elif web_search and file_id:
            #     logger.info(f"Sending request to OpenAI API with web search and file {file_id}")
            #     response = SYNC_OPENAI_CLIENT.responses.parse(
            #         model="gpt-5.4",
            #         input=[
            #             {"role": "system", "content": refined_prompt},
            #             {"role": "user",
            #                 "content": [
            #                     {"type": "input_text", "text": "Generate the report section content using only the information in the file. Do not use any other information. Use the web search to get the latest information."},
            #                     {"type": "input_file", "file_id": file_id}
            #                 ]
            #             },
            #         ],
            #         tools=[{"type": "web_search"}],
            #         tool_choice={"type": "web_search"},
            #         text_format=SectionOutput,
            #         temperature=0.1,
            #     )

            # Case 4: Vector Store only (no web search)
            elif not web_search and vector_store_id:
                logger.info(
                    f"Generating cards for section: {target_section} with vector store only {vector_store_id} with file_id {file_ids} without web search"
                )

                # Build filters to retrieve from specific file and vector store
                filters = {
                    "type": "and",
                    "filters": [
                        {
                            "type": "in",
                            "key": "file_id",
                            "value": file_ids,  # expecting list of file_ids
                        },
                        # {
                        #     "type": "in",
                        #     "key": "chat_id",
                        #     "value": user_id # expecting list of chat_ids
                        # }
                    ],
                }

                response = SYNC_OPENAI_CLIENT.responses.parse(
                    model=CARD_GEN_MODEL_OPENAI,
                    input=[
                        {
                            "role": "system",
                            "content": (
                                "You are a precise researcher and technical writer. "
                                "You have access to an uploaded document via file search. "
                                "The uploaded document is your ONLY source — base all content strictly on it. "
                                "Do not fabricate information that is not in the document. "
                                "CRITICAL: Do NOT fabricate or invent any URLs. You have no web search access. "
                                "For all citations and table sources, use plain text attribution only "
                                "(e.g., 'Source: <document title or topic> (uploaded document)'). "
                                "Do NOT invent an author name. Do NOT use 'Caspr Research', 'Ghost Research', 'Caspr', or any brand name as the source author. "
                                "Never use placeholder URLs, made-up domains, or illustrative example links. "
                                f"The answer should be tailored to the date of {current_date}."
                            ),
                        },
                        {"role": "user", "content": refined_prompt},
                    ],
                    tools=[
                        {
                            "type": "file_search",
                            "vector_store_ids": [vector_store_id],
                            "filters": filters,
                            "ranking_options": {"ranker": "auto"},
                        },
                    ],
                    tool_choice="required",
                    text_format=SectionOutput,
                    reasoning={"effort": CARD_GEN_REASONING_EFFORT},
                )
                save_raw_llm_response(
                    response,
                    CARD_GEN_MODEL_OPENAI,
                    "Writing report section content from uploaded documents",
                    chat_id,
                    user_id=user_id,
                )

            # Case 5 (grep session): LLM calls search_documents tool with its own question,
            # gets document context back, and uses it to write the card.
            # Web search is added alongside if enabled.
            elif grep_session is not None and not external_tools:
                logger.info(
                    f"Generating cards for section: {target_section} with grep_agent_2 tool "
                    f"(web_search={web_search})"
                )

                def _grep_tool_handler(args: dict) -> str:
                    question = args.get("question", "")
                    logger.info(
                        f"[grep_agent_2] search_documents called with question: '{question[:120]}'"
                    )
                    try:
                        result = grep_ask_pdfs(
                            grep_session, question, chat_id=chat_id, user_id=user_id
                        )
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
                        "Use the returned content as your primary source when writing the section."
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
                    "You are a precise researcher and technical writer. "
                    "You have access to uploaded documents via the search_documents tool. "
                    "ALWAYS call search_documents first with a focused question to retrieve relevant content "
                    "from the documents before writing. Base your content primarily on the document results. "
                )
                if web_search:
                    system_content += (
                        "You may also use web_search to supplement with current data or verify figures. "
                        "The uploaded document is your primary source — enrich it with web results where useful. "
                        "Never contradict document content with web-sourced information. "
                    )
                system_content += (
                    "Do NOT fabricate information not found in either source. "
                    "Do NOT invent URLs. "
                    f"Today's date is {current_date}."
                )

                messages = [
                    {"role": "system", "content": system_content},
                    {"role": "user", "content": refined_prompt},
                ]

                max_tool_rounds = 5
                for _round in range(max_tool_rounds):
                    response = SYNC_OPENAI_CLIENT.responses.parse(
                        model=CARD_GEN_MODEL_OPENAI,
                        input=messages,
                        tools=api_tools,
                        text_format=SectionOutput,
                        reasoning={"effort": CARD_GEN_REASONING_EFFORT},
                        include=["web_search_call.results"] if web_search else [],
                    )
                    if web_search:
                        collect_openai_search_analytics(
                            response, analytics_collector, analytics_operation_id
                        )
                    save_raw_llm_response(
                        response,
                        CARD_GEN_MODEL_OPENAI,
                        "Writing report section content by searching your documents",
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
                            json.loads(fc.arguments)
                            if isinstance(fc.arguments, str)
                            else fc.arguments
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

            # Case 0 (catch-all fallback): none of the combos above matched -- most
            # commonly web_search=False with no vector store and no grep session
            # (e.g. primary_research's document-only mode when no file/grep context
            # is actually attached). Without this branch `response` is never assigned
            # and the code below raises UnboundLocalError on every attempt. Still
            # honors web_search / vector_store_id if either happens to be set without
            # matching one of the stricter cases above.
            elif not external_tools:
                logger.info(
                    f"Generating cards for section: {target_section} with no matching "
                    f"tool combo (plain generation, web_search={web_search})"
                )
                fallback_tools = []
                if web_search:
                    fallback_tools.append({"type": "web_search"})
                if vector_store_id:
                    fallback_tools.append(
                        {
                            "type": "file_search",
                            "vector_store_ids": [vector_store_id],
                            "ranking_options": {"ranker": "auto"},
                        }
                    )

                response = SYNC_OPENAI_CLIENT.responses.parse(
                    model=CARD_GEN_MODEL_OPENAI,
                    input=[
                        {
                            "role": "system",
                            "content": (
                                "You are a precise researcher and technical writer. Produce your answer: "
                                "structured, clear, and factual, based on the provided context and instructions. "
                                f"The answer should be tailored to the date of {current_date}."
                            ),
                        },
                        {"role": "user", "content": refined_prompt},
                    ],
                    tools=fallback_tools,
                    text_format=SectionOutput,
                    reasoning={"effort": CARD_GEN_REASONING_EFFORT},
                )
                if web_search:
                    collect_openai_search_analytics(
                        response, analytics_collector, analytics_operation_id
                    )
                save_raw_llm_response(
                    response,
                    CARD_GEN_MODEL_OPENAI,
                    "Writing report section content (plain generation)",
                    chat_id,
                    user_id=user_id,
                )

            # Case 6: External tools (e.g. dd_research) + optional web search + file_search
            if external_tools:
                logger.info(
                    f"Generating cards for section: {target_section} with external tools only"
                )
                ext_tool_handlers = {}
                api_tools = []
                for et in external_tools:
                    fn_def = et.get("function", {})
                    fn_name = fn_def.get("name")
                    api_tools.append(
                        {
                            "type": "function",
                            "name": fn_name,
                            "description": fn_def.get("description", ""),
                            "parameters": fn_def.get("parameters", {}),
                        }
                    )
                    if "_handler" in et and fn_name:
                        ext_tool_handlers[fn_name] = et["_handler"]

                if web_search:
                    api_tools.append({"type": "web_search"})
                if vector_store_id:
                    fs_tool = {"type": "file_search", "vector_store_ids": [vector_store_id]}
                    if file_ids:
                        fs_tool["filters"] = {
                            "type": "and",
                            "filters": [{"type": "in", "key": "file_id", "value": file_ids}],
                        }
                    api_tools.append(fs_tool)

                system_content = (
                    "You are a Due Diligence analyst and technical writer. "
                    "You have access to external research tools that can fetch live financial data, "
                    "court records, news sentiment, ESG scores, insider transactions, analyst "
                    "targets, and more. "
                    "ALWAYS call the research tool(s) to get real data before writing — do NOT "
                    "fabricate financial figures, legal cases, or statistics. "
                    "You may also use web_search for supplementary context. "
                    f"Today's date is {current_date}."
                )

                messages = [
                    {"role": "system", "content": system_content},
                    {"role": "user", "content": refined_prompt},
                ]

                max_tool_rounds = 5
                for _round in range(max_tool_rounds):
                    response = SYNC_OPENAI_CLIENT.responses.parse(
                        model=CARD_GEN_MODEL_OPENAI,
                        input=messages,
                        tools=api_tools,
                        text_format=SectionOutput,
                        reasoning={"effort": CARD_GEN_REASONING_EFFORT},
                        include=["web_search_call.results"],
                    )
                    if web_search:
                        collect_openai_search_analytics(
                            response, analytics_collector, analytics_operation_id
                        )
                    save_raw_llm_response(
                        response,
                        CARD_GEN_MODEL_OPENAI,
                        "Writing report section content using research tools",
                        chat_id,
                        user_id=user_id,
                    )

                    fn_calls = [
                        item
                        for item in response.output
                        if getattr(item, "type", None) == "function_call"
                    ]

                    if not fn_calls:
                        break

                    for fc in fn_calls:
                        fn_name = fc.name
                        fn_args = (
                            json.loads(fc.arguments)
                            if isinstance(fc.arguments, str)
                            else fc.arguments
                        )
                        call_id = fc.call_id

                        logger.info(
                            f"[external_tools] Round {_round + 1} — model called {fn_name}({json.dumps(fn_args, default=str)[:200]})"
                        )

                        handler = ext_tool_handlers.get(fn_name)
                        if handler:
                            try:
                                result_text = handler(fn_args)
                            except Exception as exc:
                                logger.error(f"[external_tools] {fn_name} failed: {exc}")
                                result_text = json.dumps({"error": str(exc)})
                        else:
                            result_text = json.dumps(
                                {"error": f"No handler for function: {fn_name}"}
                            )

                        messages.append(
                            {
                                "type": "function_call",
                                "call_id": call_id,
                                "name": fn_name,
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
                        f"[external_tools] Round {_round + 1} — resolved {len(fn_calls)} function call(s)"
                    )
            with open("response.json", "w") as f:
                json.dump(response.model_dump(), f)
            output = response.model_dump()["output"][-1]
            citations = _extract_citations_from_response(response)

            json_output = output["content"][0]["parsed"]

            json_output["section"] = target_section
            logger.info("Successfully received response from OpenAI API")
            for idx, sub_section in enumerate(json_output["sub_sections"]):
                if idx < len(card_layout["sub_sections"]):
                    sub_section["name"] = card_layout["sub_sections"][idx]["name"]

            return json_output, citations
        except Exception as e:
            # raise Exception("test")
            logger.error(f"OpenAI API attempt {openai_attempt}/{openai_max_retries} failed: {e!s}")
            if _is_context_length_error(e) and not _used_context_fallback and previous_cards:
                logger.warning(
                    f"Context length exceeded for section '{target_section}'. "
                    f"Falling back to cumulative summary approach instead of full previous cards."
                )
                cumulative_summary_fallback = _build_cumulative_summary_from_cards(
                    previous_cards,
                    chat_id=chat_id,
                    user_id=user_id,
                )
                prompt = build_card_gen_prompt(
                    target_section=target_section,
                    section_description=section_description,
                    sub_sections_str=sub_sections_str,
                    descriptive_report_layout=descriptive_report_layout,
                    current_date=current_date,
                    user_instructions=user_instructions,
                    report_length=report_length,
                    previous_cards_content=None,
                    cumulative_summary=cumulative_summary_fallback,
                    has_file=bool(upload_file_config) or bool(grep_session),
                    prioritize_arxiv=prioritize_arxiv,
                    style=style,
                )
                refined_prompt = prompt.strip()
                _used_context_fallback = True
                logger.info("Rebuilt prompt with cumulative summary fallback, retrying...")
                continue

            if openai_attempt < openai_max_retries:
                logger.info(f"Retrying OpenAI API in {retry_delay} seconds...")
                time.sleep(retry_delay)
            else:
                logger.error(
                    f"All {openai_max_retries} OpenAI API attempts failed, falling back to Gemini API"
                )

    # ------------------------------------------------------------------
    # Fallback: Gemini card generation (replaces the Perplexity fallback,
    # which is kept commented out below for reference/rollback).
    # Imported here (not at module level) to avoid a circular import:
    # gemini_card_utils -> ask_caspr_constants -> ask_caspr package ->
    # ask_caspr_utils -> card_utils.
    # ------------------------------------------------------------------
    # PRE-EXISTING BREAKAGE, untouched by the R-STRUCT-1 migration: this module
    # does not exist anywhere in the repository and has no history in git, so
    # this fallback path raises ModuleNotFoundError whenever it is reached. Left
    # exactly as found because this migration changes no logic; it needs its own
    # fix (restore the module, or delete the dead fallback).
    from src.core.gemini_card_utils import generate_cards_gemini

    logger.info(f"Falling back to Gemini card generation for section: {target_section}")
    return generate_cards_gemini(
        card_layout=card_layout,
        cumulative_summary=cumulative_summary,
        is_first_section=is_first_section,
        descriptive_report_layout=descriptive_report_layout,
        max_retries=max_retries,
        retry_delay=retry_delay,
        user_instructions=user_instructions,
        report_length=report_length,
        upload_file_config=upload_file_config,
        web_search=web_search,
        external_tools=external_tools,
        previous_cards=previous_cards,
        grep_session=grep_session,
        chat_id=chat_id,
        user_id=user_id,
        analytics_collector=analytics_collector,
        analytics_operation_id=analytics_operation_id,
    )

    # ---- DISABLED: Perplexity fallback (superseded by the Gemini fallback above) ----
    # retry_count = 0
    # while retry_count < max_retries:
    #     try:
    #         logger.info(f"Falling back to Perplexity API (attempt {retry_count + 1}/{max_retries})")
    #
    #         payload = {
    #             "model": "sonar-deep-research",
    #             "messages": [{"role": "user", "content": prompt.strip()}],
    #             "response_format": {
    #                 "type": "json_schema",
    #                 "json_schema": {
    #                     "name": "card_gen_output",
    #                     "schema": CARD_GEN_JSON_SCHEMA,
    #                 }
    #             },
    #             "temperature": 0.1,
    #             "max_tokens": 16000
    #         }
    #
    #         headers = {
    #             "Authorization": f"Bearer {PERPLEXITY_API_KEY}",
    #             "Content-Type": "application/json"
    #         }
    #
    #         logger.info("Sending request to Perplexity API")
    #         resp = requests.post("https://api.perplexity.ai/chat/completions", headers=headers, json=payload)
    #         if 200 <= resp.status_code < 300:
    #             collect_perplexity_search_analytics(
    #                 resp.json(), analytics_collector, analytics_operation_id
    #             )
    #         save_raw_llm_response(resp.json(), "sonar-pro", "Writing report section content (backup)", chat_id, user_id=user_id)
    #         result = json.loads(resp.json()["choices"][0]["message"]["content"])
    #         logger.info("Successfully received response from Perplexity API")
    #
    #         result['section'] = target_section
    #         for idx, sub_section in enumerate(result['sub_sections']):
    #             if idx < len(card_layout['sub_sections']):
    #                 sub_section['name'] = card_layout['sub_sections'][idx]['name']
    #
    #         logger.info(f"Running card fixer for section: {target_section}")
    #         result = fix_card(result)
    #         logger.info(f"Card fixer completed for section: {target_section}")
    #
    #         return result, resp.json()['citations']
    #     except Exception as e:
    #         retry_count += 1
    #         logger.warning(f"Perplexity API attempt {retry_count}/{max_retries} failed: {str(e)}")
    #         logger.info(f'Error type: {type(e).__name__}')
    #         if retry_count < max_retries:
    #             logger.info(f"Retrying in {retry_delay} seconds...")
    #             time.sleep(retry_delay)
    #         else:
    #             logger.critical(f"All Perplexity API retry attempts failed: {str(e)}")
    #             return None, None


def gather_context_from_uploaded_file(
    drl: list,
    grep_session: PdfSession,
    user_instructions: str,
    chat_id: str = None,
    user_id: str = None,
) -> list:
    """
    Run all DRL sections and sub-sections as parallel queries against a
    grep_agent_2 session. Enriches the DRL in-place by adding a
    'context_from_uploaded_file' key to each section and sub-section.

    All queries run concurrently via ThreadPoolExecutor for speed.

    Args:
        drl: Descriptive report layout — list of dicts with 'section' and 'sub_sections'.
        grep_session: An active PdfSession from grep_agent_2.create_session().
        user_instructions: The user's research topic (used to anchor queries).
        chat_id: Optional chat id for cost tracking.
        user_id: Optional user id for cost tracking.

    Returns:
        The same drl list, enriched with 'context_from_uploaded_file' on each
        section and sub-section dict.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    logger.info(
        f"[gather_context_from_uploaded_file] Gathering context for {len(drl)} sections in parallel"
    )

    def _query(query_text: str) -> str:
        try:
            result = grep_ask_pdfs(grep_session, query_text, chat_id=chat_id, user_id=user_id)
            return result.get("answer", "")
        except Exception as exc:
            logger.warning(f"[gather_context_from_uploaded_file] Query failed: {exc}")
            return ""

    # Build all tasks: (drl_index, sub_index_or_None, query_string)
    tasks = []
    for sec_idx, section in enumerate(drl):
        section_name = section.get("section", "")
        section_desc = section.get("description", "")
        sec_query = (
            f"Regarding '{user_instructions}': "
            f"Find all information about '{section_name}'. {section_desc}"
        )
        tasks.append((sec_idx, None, sec_query))

        for sub_idx, sub in enumerate(section.get("sub_sections", [])):
            sub_name = sub.get("name", "")
            sub_desc = sub.get("description", "")
            sub_query = (
                f"Regarding '{user_instructions}', section '{section_name}': "
                f"Find information about '{sub_name}'. {sub_desc}"
            )
            tasks.append((sec_idx, sub_idx, sub_query))

    logger.info(f"[gather_context_from_uploaded_file] Launching {len(tasks)} parallel queries")

    # Run all queries in parallel
    future_map = {}
    with ThreadPoolExecutor(max_workers=min(len(tasks), 10)) as executor:
        for sec_idx, sub_idx, query_text in tasks:
            future = executor.submit(_query, query_text)
            future_map[future] = (sec_idx, sub_idx)

        for future in as_completed(future_map):
            sec_idx, sub_idx = future_map[future]
            answer = future.result()
            if sub_idx is None:
                drl[sec_idx]["context_from_uploaded_file"] = answer
                logger.info(
                    f"[gather_context_from_uploaded_file] Section '{drl[sec_idx].get('section', '')}': {len(answer)} chars"
                )
            else:
                drl[sec_idx]["sub_sections"][sub_idx]["context_from_uploaded_file"] = answer
                logger.info(
                    f"[gather_context_from_uploaded_file] Sub-section '{drl[sec_idx]['sub_sections'][sub_idx].get('name', '')}': {len(answer)} chars"
                )

    total_chars = sum(
        len(s.get("context_from_uploaded_file", ""))
        + sum(len(sub.get("context_from_uploaded_file", "")) for sub in s.get("sub_sections", []))
        for s in drl
    )
    logger.info(
        f"[gather_context_from_uploaded_file] Done — {total_chars} total chars gathered across {len(tasks)} queries"
    )

    return drl


# ─────────────────────────────────────────────────────────────────────────────
# Brief report generation — streaming cards
# ─────────────────────────────────────────────────────────────────────────────

# ---------------------------------------------------------------------------
# DEPRECATED (kept for reference): old brief schemas with 2 sub-sections per
# section. Replaced by the flat, no-subsection schemas below.
# ---------------------------------------------------------------------------
# class BriefSubSection(BaseModel):
#     name: str = Field(..., description="Plain text sub-section title without markdown symbols, bullets, or numbering.")
#     content: str = Field(
#         ...,
#         description="Surface-level overview content only: 2-3 short sentences or bullets with headline facts, key numbers, essential takeaways, and compact tables where useful."
#     )
#
#
# class BriefSectionOutput(BaseModel):
#     section: str = Field(..., description="Plain text section name without markdown symbols, bullets, or numbering.")
#     content: str = Field(..., description="One or two short overview paragraphs, 3-5 sentences maximum total.")
#     sub_sections: List[BriefSubSection] = Field(..., description="Exactly 2 overview sub-sections.")
#
#
# class BriefOutput(BaseModel):
#     sections: List[BriefSectionOutput] = Field(..., description="Exactly 5 surface-level overview report sections")


class BriefSectionOutput(BaseModel):
    section: str = Field(
        ..., description="Plain text section name without markdown symbols, bullets, or numbering."
    )
    content: str = Field(
        ...,
        description=(
            "Flat section body: 3-4 short sentences (one short paragraph) covering the "
            "headline facts, key numbers, and essential takeaways. Optionally append ONE "
            "compact markdown table when the data warrants it. No sub-headers or nested "
            "headings of any kind."
        ),
    )


class BriefOutput(BaseModel):
    sections: list[BriefSectionOutput] = Field(
        ..., description="Surface-level overview report sections (flat, no sub-sections)"
    )


def _stream_brief_chunks(
    drl: list,
    user_instructions: str,
    chat_id: str = None,
    user_id: str = None,
    analytics_collector: list[dict] | None = None,
    analytics_operation_id: str | None = None,
    prioritize_arxiv: bool = PRIORITIZE_ARXIV,
    style: str = DEFAULT_AUDIENCE_STYLE,
) -> Generator[str]:
    """Stream raw JSON text chunks from gpt-5.4 with web_search + BriefOutput schema.

    Args:
        analytics_collector: Optional list the caller passes in to receive one
            web-search analytics entry for this call. Unlike the study-report
            path (one `generate_cards` call per section), a brief report makes
            a single provider call for the whole report, so this collects at
            most one entry regardless of how many DRL sections are requested.
        analytics_operation_id: Groups this call's analytics entry with any
            others from the same report-generation operation.
        prioritize_arxiv: When True, instructs the model to try arxiv.org-scoped
            searches first for each section before falling back to the
            broader web, and caps total web search calls at 10 via
            ``max_tool_calls``. Unlike the per-section `generate_cards` path,
            this single call covers ALL brief sections, so the budget and
            instructions are shared across the whole report.
    """
    from app.research.prompts.prompt_utils import (
        BRIEF_ARXIV_PRIORITY_ADDENDUM,
        build_brief_prompt,
        build_brief_system_prompt,
    )

    current_date = datetime.datetime.now().strftime("%B %d, %Y")
    prompt = build_brief_prompt(drl, user_instructions, current_date)

    system_msg = build_brief_system_prompt(current_date, style)
    call_kwargs = {}
    if prioritize_arxiv:
        system_msg += BRIEF_ARXIV_PRIORITY_ADDENDUM
        call_kwargs["max_tool_calls"] = 10
        call_kwargs["include"] = ["web_search_call.results", "web_search_call.action.sources"]
    else:
        call_kwargs["include"] = ["web_search_call.results"]

    logger.info(
        f"[brief_stream] Starting stream — {CARD_GEN_MODEL_OPENAI} + web_search + BriefOutput schema, DRL has {len(drl)} sections, prioritize_arxiv={prioritize_arxiv}"
    )

    yielded_any_chunk = False
    try:
        stream = SYNC_OPENAI_CLIENT.responses.stream(
            model=CARD_GEN_MODEL_OPENAI,
            input=[
                {"role": "system", "content": system_msg},
                {"role": "user", "content": prompt},
            ],
            tools=[{"type": "web_search"}],
            tool_choice={"type": "web_search"},
            text_format=BriefOutput,
            reasoning={"effort": CARD_GEN_REASONING_EFFORT},
            **call_kwargs,
        )

        with stream as s:
            for event in s:
                event_type = getattr(event, "type", "")
                if event_type == "response.output_text.delta":
                    yielded_any_chunk = True
                    yield event.delta

            # Fetch the completed response once and reuse it for both raw-response
            # logging and web-search analytics collection below.
            final_response = None
            try:
                final_response = s.get_final_response()
            except Exception:
                pass

            if final_response is not None:
                try:
                    save_raw_llm_response(
                        final_response,
                        CARD_GEN_MODEL_OPENAI,
                        "Streaming a short research preview",
                        chat_id,
                        user_id=user_id,
                    )
                except Exception:
                    pass

                # Non-fatal web-search analytics collection: extracts candidate/cited
                # links, provider queries, and usage from the one provider response
                # backing this entire brief report. `collect_openai_search_analytics`
                # is a no-op when `analytics_collector` is None and swallows/logs its
                # own failures, so this can never affect brief-report generation.
                collect_openai_search_analytics(
                    final_response, analytics_collector, analytics_operation_id
                )

        logger.info("[brief_stream] Stream finished.")
        return
    except Exception as e:
        if yielded_any_chunk:
            # Partial output already reached the caller — restarting on a
            # different provider would corrupt the JSON stream, so surface the error.
            logger.error(
                f"[brief_stream] OpenAI stream failed mid-output, cannot fall back to Gemini: {e}"
            )
            raise
        logger.error(
            f"[brief_stream] OpenAI stream failed before any output ({e}), falling back to Gemini API"
        )

    # Gemini fallback: Gemini's interactions API doesn't support this
    # response_format+tools combination in streaming mode, so this is a single
    # non-streaming call whose full JSON text is yielded as one chunk — still
    # compatible with `_parse_brief_stream`, which just concatenates chunks.
    gemini_client = genai.Client(api_key=GEMINI_API_KEY)
    interaction = gemini_client.interactions.create(
        model=GEMINI_BRIEF_STREAM_MODEL,
        input=prompt,
        system_instruction=system_msg,
        tools=[{"type": "google_search"}],
        response_format={
            "type": "text",
            "mime_type": "application/json",
            "schema": BriefOutput.model_json_schema(),
        },
        generation_config={
            "temperature": 0.1,
            "thinking_level": CARD_GEN_GEMINI_THINKING_LEVEL,
        },
    )
    try:
        save_raw_llm_response(
            interaction,
            GEMINI_BRIEF_STREAM_MODEL,
            "Streaming a short research preview (backup)",
            chat_id,
            user_id=user_id,
        )
    except Exception:
        pass

    try:
        collect_gemini_search_analytics(
            interaction,
            analytics_collector,
            analytics_operation_id,
            model_used=GEMINI_BRIEF_STREAM_MODEL,
        )
    except Exception:
        pass

    yield strip_json_code_fence(interaction.output_text)
    logger.info("[brief_stream] Gemini fallback finished.")


def _parse_brief_stream(chunk_generator) -> Generator[dict]:
    """
    Consume JSON chunks from _stream_brief_chunks.
    Track brace/bracket depth to detect when each section object completes.
    Yield section dicts (same structure as generate_cards output) one at a time.

    JSON structure: {"sections": [{...}, {...}, ...]}
    """
    json_buffer = ""
    brace_depth = 0
    in_string = False
    escape_next = False
    sections_array_started = False
    current_object_start = -1
    section_count = 0

    for chunk in chunk_generator:
        for char in chunk:
            json_buffer += char

            if escape_next:
                escape_next = False
                continue
            if char == "\\" and in_string:
                escape_next = True
                continue
            if char == '"':
                in_string = not in_string
                continue
            if in_string:
                continue

            if char == "[":
                if not sections_array_started and brace_depth >= 1:
                    sections_array_started = True
            elif char == "{":
                brace_depth += 1
                if sections_array_started and brace_depth == 2:
                    current_object_start = len(json_buffer) - 1
            elif char == "}":
                brace_depth -= 1
                if sections_array_started and brace_depth == 1 and current_object_start >= 0:
                    section_json = json_buffer[current_object_start:]
                    try:
                        section_data = json.loads(section_json)
                        if "section" in section_data and "content" in section_data:
                            section_count += 1
                            logger.info(
                                f"[brief_parse] Card {section_count}: "
                                f'"{section_data["section"]}" '
                                f"({len(section_data['content'])} chars, "
                                f"{len(section_data.get('sub_sections', []))} subs)"
                            )
                            yield section_data
                    except json.JSONDecodeError:
                        pass
                    current_object_start = -1


def generate_brief_cards(
    drl: list,
    user_instructions: str,
    chat_id: str = None,
    user_id: str = None,
    analytics_collector: list[dict] | None = None,
    analytics_operation_id: str | None = None,
    prioritize_arxiv: bool = PRIORITIZE_ARXIV,
    style: str = DEFAULT_AUDIENCE_STYLE,
) -> Generator[dict]:
    """
    Generate brief report cards via streaming.

    Streams gpt-5.4 with web_search + structured output (BriefOutput schema),
    parses the JSON stream incrementally, and yields section dicts one at a time
    as each section completes.

    Each yielded dict has the same structure as generate_cards output:
        {"section": "...", "content": "...", "sub_sections": [{"name": "...", "content": "..."}, ...]}

    Args:
        drl: Descriptive report layout — list of dicts with 'section' and 'sub_sections' keys.
        user_instructions: The research topic / user instructions for the brief.
        analytics_collector: Optional list passed straight through to
            `_stream_brief_chunks` to receive the single web-search analytics
            entry for this brief report (see that function's docstring).
        analytics_operation_id: Groups the collected analytics entry with the
            caller's report-generation operation.
        prioritize_arxiv: Forwarded to `_stream_brief_chunks` — see its
            docstring for behavior.

    Yields:
        dict: A section dict matching SectionOutput schema.
    """
    logger.info(
        f"[generate_brief_cards] Starting brief generation — {len(drl)} sections, prioritize_arxiv={prioritize_arxiv}"
    )
    chunks = _stream_brief_chunks(
        drl=drl,
        user_instructions=user_instructions,
        chat_id=chat_id,
        user_id=user_id,
        analytics_collector=analytics_collector,
        analytics_operation_id=analytics_operation_id,
        prioritize_arxiv=prioritize_arxiv,
        style=style,
    )
    for card in _parse_brief_stream(chunks):
        # Brief reports are flat: all content lives in the section 'content' field.
        # Attach an empty sub-section placeholder so the card keeps the exact same
        # structure as standard cards through modify_card / add_viz_to_card and the
        # rest of post-processing (mirrors the title/subtitle/TOC meta-cards).
        if not card.get("sub_sections"):
            card["sub_sections"] = [{"name": "", "content": ""}]
        yield card
    logger.info("[generate_brief_cards] All cards generated.")


def repair_malformed_citation_markdown(text: str) -> str:
    """Repair common LLM markdown-link typos so citations are always ``[n](url)``.

    Handles shapes the rest of the citation pipeline cannot match, notably a
    closing ``]`` used where a closing ``)`` is required:

        [318](https://example.com/path]  ->  [318](https://example.com/path)
        [Source](https://example.com/x]  ->  [Source](https://example.com/x)

    Also normalises nested brackets ``[[n]](url)`` / ``[[text]](url)`` that
    otherwise break numbered-citation matching.
    """
    if not text:
        return text

    # [label](https://url]  ->  [label](https://url)
    # URL body: no whitespace, and stop before the mistaken closing ].
    text = re.sub(
        r"\[([^\]]+)\]\((https?://[^\s\]\)]+)\]",
        r"[\1](\2)",
        text,
    )

    # [[42]](url) / [[text]](url) -> [42](url) / [text](url)
    text = re.sub(r"\[\[(\d+)\]\]\((https?://[^)]+)\)", r"[\1](\2)", text)
    text = re.sub(r"\[\[([^\]]+)\]\]\((https?://[^)]+)\)", r"[\1](\2)", text)

    return text


def fix_numeric_url_citations(text: str, resolve_url) -> str:
    """Repair citations whose URL slot holds a bare citation number.

    LLM output occasionally emits malformed citations where the URL is replaced
    by the citation number itself, e.g. ``[3](3)`` instead of
    ``[3](https://...)``. Two shapes are normalised:

        [N](M)[K](url)  ->  [K](url)   (drop the broken duplicate, keep the
                                        valid citation whose number sits next
                                        to the real URL)
        [N](M)          ->  [D](url)   (resolve the number to its real URL when
                                        known, otherwise drop the citation)

    where ``M`` is a bare number and ``url`` is a real http(s) link. A numeric
    citation that cannot be resolved to any URL is removed entirely rather than
    left as a dead ``[N](N)`` link.

    Args:
        text: Content containing citations.
        resolve_url: Callable ``(number_str) -> (display_number_str, url) | None``
            that maps a bare citation number to its display number and URL.

    Returns:
        Text with numeric-URL citations repaired.
    """
    if not text:
        return text

    # Case 2: a broken numeric-URL citation immediately followed by a valid one.
    # Keep the valid citation (its number is the one adjacent to the real URL).
    # [N](M)[K](url) -> [K](url)
    text = re.sub(
        r"\[\d+\]\(\d+\)\s*(\[\d+\]\(https?://[^)]+\))",
        r"\1",
        text,
    )

    # Case 1: standalone numeric-URL citations. [N](M) -> [D](url), or drop it
    # when the number maps to no known URL.
    def _resolve(match):
        display_num = match.group(1)
        url_num = match.group(2)
        resolved = resolve_url(url_num) or resolve_url(display_num)
        if resolved:
            disp, url = resolved
            return f"[{disp}]({url})"
        return ""

    text = re.sub(r"\[(\d+)\]\((\d+)\)", _resolve, text)

    # Tidy whitespace/punctuation left behind by removed citations.
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"\s+([.,;:])", r"\1", text)

    return text


def replace_brief_citations(
    card: dict, citation_url_map: dict, all_report_citations: list, keep_labels: bool = False
) -> dict:
    """Normalize inline citations in a brief card and populate card["citations"].

    Always cleans the URL (utm/tracking params) and registers each source in the
    report-wide ``citation_url_map`` / ``all_report_citations`` so the sources
    panel is complete.

    Args:
        card: A card dict with 'content' and 'sub_sections' (post fix_card format).
        citation_url_map: Running map of cleaned_url -> global citation number.
        all_report_citations: Running list of all unique citation URLs in order.
        keep_labels: When True, keep the model's ``[Source Name](url)`` label
            instead of rewriting it to ``[global_number](url)``. The report-wide
            map is still populated. This is the default rendering now that reports
            show named citations rather than ``[1] [2] [3]``.

    Returns:
        The mutated card with cleaned inline citations and populated citations dict.
    """
    card_citation_map = {}

    def _renumber(text: str) -> str:
        if not text:
            return text

        text = repair_malformed_citation_markdown(text)

        # Maps the model's per-section citation number -> cleaned URL, so that a
        # malformed [N](N) (where the URL slot holds the number) can be resolved.
        local_num_to_url = {}

        def _replacer(match):
            local_label = match.group(1).strip()
            url = match.group(2)
            cleaned = clean_url(url)
            if cleaned not in citation_url_map:
                all_report_citations.append(cleaned)
                citation_url_map[cleaned] = len(all_report_citations)
            global_num = citation_url_map[cleaned]
            card_citation_map[cleaned] = global_num
            if local_label.isdigit():
                local_num_to_url[local_label] = cleaned
            if keep_labels and local_label and not local_label.isdigit():
                return f"[{local_label}]({cleaned})"
            if keep_labels:
                # The model gave a bare number despite the schema asking for a
                # name. Fall back to the source's domain rather than cementing
                # "[1]" — renumbering here is what kept numbered citations in
                # briefs even with keep_labels=True.
                from app.agent.analyst_reasoning.url_cleaner import source_label

                named = source_label(cleaned)
                if named:
                    return f"[{named}]({cleaned})"
            return f"[{global_num}]({cleaned})"

        text = re.sub(r"\[([^\]]*)\]\((https?://[^)]+)\)", _replacer, text)

        def _resolve(num_str):
            cleaned = local_num_to_url.get(num_str)
            if not cleaned:
                idx = int(num_str) - 1
                if 0 <= idx < len(all_report_citations):
                    cleaned = all_report_citations[idx]
            if cleaned and cleaned in citation_url_map:
                global_num = citation_url_map[cleaned]
                card_citation_map[cleaned] = global_num
                return str(global_num), cleaned
            return None

        return fix_numeric_url_citations(text, _resolve)

    card["content"] = _renumber(card.get("content", ""))

    for sub in card.get("sub_sections", []):
        sub["content"] = _renumber(sub.get("content", ""))

    card["citations"] = card_citation_map
    return card


def replace_citations(
    text,
    citation_indices,
    processed_citations,
    citation_url_map,
    report_citations,
    url_to_snippet: dict[str, str] | None = None,
):
    try:
        if url_to_snippet is None:
            url_to_snippet = {}

        def _build_citation_url(url: str) -> str:
            """Build the final citation URL, using text fragment if snippet is available."""
            if url in url_to_snippet:
                return generate_citation_url(url, url_to_snippet[url])
            return url

        # Pre-process text to fix broken URLs across lines
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
        updated_text = fix_broken_urls(text)

        # Repair [n](url] / [[n]](url) before any citation matching.
        updated_text = repair_malformed_citation_markdown(updated_text)

        # First handle standard citation markers [n]
        def replacer(match):
            local_index = int(match.group(1))
            if local_index <= len(citation_indices):
                global_index = citation_indices[local_index - 1]
                url = processed_citations[local_index - 1]
                # Remove all variations of utm_source parameter
                url = url.replace("?utm_source=openai", "")
                url = url.replace("&utm_source=openai", "")
                final_url = _build_citation_url(url)
                return f"[{global_index}]({final_url})"
            return match.group(0)

        pattern = r"\[(\d+)\](?!\()"
        updated_text = re.sub(pattern, replacer, updated_text)

        # Then handle OpenAI formatted citations like ([site.com](https://url))
        def openai_replacer(match):
            url = match.group(2)
            url = str(url)
            # Remove all variations of utm_source parameter
            url = url.replace("?utm_source=openai", "")
            url = url.replace("&utm_source=openai", "")
            if url in citation_url_map:
                global_index = citation_url_map[url]
            else:
                # If URL isn't in the map yet, add it
                report_citations.append(url)
                global_index = len(report_citations)
                citation_url_map[url] = global_index
            final_url = _build_citation_url(url)
            return f"[{global_index}]({final_url})"

        # Updated pattern to handle citations after periods
        openai_pattern = r"\(\[(.*?)\]\((https?://[^)]+)\)\)"
        updated_text = re.sub(openai_pattern, openai_replacer, updated_text)

        # Handle Markdown-style links [link_text](url) and convert to numbered citations
        logger.info("Processing markdown-style links for citation replacement")
        markdown_links_found = 0

        def markdown_link_replacer(match):
            nonlocal markdown_links_found
            markdown_links_found += 1

            link_text = match.group(1)
            url = match.group(2)

            # Skip if link_text is already a pure number (already a citation)
            if link_text.strip().isdigit():
                logger.info(f"Skipping markdown link with numeric text: [{link_text}]({url})")
                return match.group(0)

            url = str(url)
            # Clean URL from utm parameters
            url = url.replace("?utm_source=openai", "")
            url = url.replace("&utm_source=openai", "")

            url = clean_url(url)

            # Check if URL already exists in citation_url_map
            if url in citation_url_map:
                global_index = citation_url_map[url]
                logger.info(f"Found existing citation: [{link_text}] -> [{global_index}]({url})")
            else:
                # Assign new citation number
                report_citations.append(url)
                global_index = len(report_citations)
                citation_url_map[url] = global_index
                logger.info(f"Created new citation: [{link_text}] -> [{global_index}]({url})")

            # Replace link_text with citation number, preserve Markdown link structure
            final_url = _build_citation_url(url)
            return f"[{global_index}]({final_url})"

        # Pattern to match [link_text](url) - match any text in brackets followed by URL
        markdown_link_pattern = r"\[([^\]]+)\]\((https?://[^)]+)\)"
        updated_text = re.sub(markdown_link_pattern, markdown_link_replacer, updated_text)
        logger.info(f"Processed {markdown_links_found} markdown-style links")

        # Handle domain-only citations in parentheses: (domain.com/path)
        logger.info("Processing domain-only citations in parentheses")
        domain_citations_found = 0

        def domain_citation_replacer(match):
            nonlocal domain_citations_found
            domain_citations_found += 1

            domain_path = match.group(1)
            # Convert to full URL by adding https://
            url = f"https://{domain_path}"

            # Clean URL from utm parameters
            url = url.replace("?utm_source=openai", "")
            url = url.replace("&utm_source=openai", "")
            url = clean_url(url)

            # Check if URL already exists in citation_url_map
            if url in citation_url_map:
                global_index = citation_url_map[url]
                logger.info(f"Found existing citation for domain URL: [{global_index}]({url})")
            else:
                # Assign new citation number
                report_citations.append(url)
                global_index = len(report_citations)
                citation_url_map[url] = global_index
                logger.info(f"Created new citation for domain URL: [{global_index}]({url})")

            final_url = _build_citation_url(url)
            return f"[{global_index}]({final_url})"

        # Pattern to match domain-only citations: (domain.com/path)
        # This matches domains with optional paths but no protocol
        domain_citation_pattern = r"\(([a-zA-Z0-9-]+\.[a-zA-Z]{2,}[^\s)]*)\)"
        updated_text = re.sub(domain_citation_pattern, domain_citation_replacer, updated_text)
        logger.info(f"Processed {domain_citations_found} domain-only citations")

        # Handle malformed citations with partial URLs and double parentheses
        # Pattern: text/partial-url))
        logger.info("Processing malformed citations with partial URLs")
        malformed_citations_found = 0

        def malformed_citation_replacer(match):
            nonlocal malformed_citations_found
            malformed_citations_found += 1

            content = match.group(1)
            # Try to extract URL-like content
            # Look for patterns like "text/www.domain.com/path" or "text/domain.com/path"
            url_match = re.search(r"/((?:www\.)?[a-zA-Z0-9-]+\.[a-zA-Z]{2,}[^\s)]*)", content)

            if url_match:
                domain_path = url_match.group(1)
                # Convert to full URL by adding https://
                url = f"https://{domain_path}"

                # Clean URL from utm parameters
                url = url.replace("?utm_source=openai", "")
                url = url.replace("&utm_source=openai", "")
                url = clean_url(url)

                # Check if URL already exists in citation_url_map
                if url in citation_url_map:
                    global_index = citation_url_map[url]
                    logger.info(
                        f"Found existing citation for malformed URL: [{global_index}]({url})"
                    )
                else:
                    # Assign new citation number
                    report_citations.append(url)
                    global_index = len(report_citations)
                    citation_url_map[url] = global_index
                    logger.info(f"Created new citation for malformed URL: [{global_index}]({url})")

                # Extract the text before the URL
                text_before_url = content[: url_match.start()]
                final_url = _build_citation_url(url)
                return f"{text_before_url}[{global_index}]({final_url})"
            else:
                # If no URL found, return original without the extra parentheses
                return content

        # Pattern to match malformed citations ending with ))
        malformed_pattern = r"([^)]+/[^\s)]+)\)\)"
        updated_text = re.sub(malformed_pattern, malformed_citation_replacer, updated_text)
        logger.info(f"Processed {malformed_citations_found} malformed citations")

        # Handle parenthetical citations with descriptive text: (descriptive text URL)
        logger.info("Processing parenthetical citations with descriptive text")
        descriptive_citations_found = 0

        def descriptive_citation_replacer(match):
            nonlocal descriptive_citations_found
            descriptive_citations_found += 1

            full_content = match.group(1)
            # Extract URL from the end of the content
            url_match = re.search(r"(https?://[^\s)]+)$", full_content.strip())
            if url_match:
                url = url_match.group(1)
                url = str(url)

                # Clean URL from utm parameters
                url = url.replace("?utm_source=openai", "")
                url = url.replace("&utm_source=openai", "")
                url = clean_url(url)

                # Check if URL already exists in citation_url_map
                if url in citation_url_map:
                    global_index = citation_url_map[url]
                    logger.info(
                        f"Found existing citation for descriptive URL: [{global_index}]({url})"
                    )
                else:
                    # Assign new citation number
                    report_citations.append(url)
                    global_index = len(report_citations)
                    citation_url_map[url] = global_index
                    logger.info(
                        f"Created new citation for descriptive URL: [{global_index}]({url})"
                    )

                final_url = _build_citation_url(url)
                return f"[{global_index}]({final_url})"
            else:
                # No URL found, return original
                return match.group(0)

        # Pattern to match parenthetical citations with descriptive text and URL
        # This pattern captures content in parentheses that ends with a URL and contains descriptive text
        descriptive_citation_pattern = r"\(([^)]*[a-zA-Z][^)]*https?://[^)]+)\)"
        updated_text = re.sub(
            descriptive_citation_pattern, descriptive_citation_replacer, updated_text
        )
        logger.info(f"Processed {descriptive_citations_found} descriptive parenthetical citations")

        # Handle plain URLs in parentheses: (url) - but not if preceded by ]
        # This handles pattern like "this is text cited from (url)"
        logger.info("Processing plain URLs in parentheses")
        plain_urls_found = 0

        def plain_url_replacer(match):
            nonlocal plain_urls_found
            plain_urls_found += 1

            url = match.group(1)
            url = str(url)

            # Clean URL from utm parameters
            url = url.replace("?utm_source=openai", "")
            url = url.replace("&utm_source=openai", "")
            url = clean_url(url)

            # Check if URL already exists in citation_url_map
            if url in citation_url_map:
                global_index = citation_url_map[url]
                logger.info(f"Found existing citation for plain URL: [{global_index}]({url})")
            else:
                # Assign new citation number
                report_citations.append(url)
                global_index = len(report_citations)
                citation_url_map[url] = global_index
                logger.info(f"Created new citation for plain URL: [{global_index}]({url})")

            final_url = _build_citation_url(url)
            return f"[{global_index}]({final_url})"

        # Pattern to match (url) but not when preceded by ] (to avoid matching markdown links)
        # This pattern matches plain URLs in parentheses that weren't caught by descriptive citations
        plain_url_pattern = r"(?<!\])\((https?://[^)\s]+)\)"
        updated_text = re.sub(plain_url_pattern, plain_url_replacer, updated_text)
        logger.info(f"Processed {plain_urls_found} plain URLs in parentheses")

        # Handle standalone plain URLs (not in parentheses or markdown format)
        # This matches URLs that are not already part of [text](url) or (url) formats
        logger.info("Processing standalone plain URLs")
        standalone_urls_found = 0

        def standalone_url_replacer(match):
            nonlocal standalone_urls_found
            standalone_urls_found += 1

            url = match.group(0)
            url = str(url)

            # Clean URL from utm parameters
            url = url.replace("?utm_source=openai", "")
            url = url.replace("&utm_source=openai", "")
            url = clean_url(url)

            # Check if URL already exists in citation_url_map
            if url in citation_url_map:
                global_index = citation_url_map[url]
                logger.info(f"Found existing citation for standalone URL: [{global_index}]({url})")
            else:
                # Assign new citation number
                report_citations.append(url)
                global_index = len(report_citations)
                citation_url_map[url] = global_index
                logger.info(f"Created new citation for standalone URL: [{global_index}]({url})")

            final_url = _build_citation_url(url)
            return f"[{global_index}]({final_url})"

        # Pattern to match standalone URLs not already in markdown or parentheses format
        # Requires proper URL format: http(s):// followed by domain with proper structure
        # - Must have double slashes ://
        # - Must have valid domain characters
        # - Negative lookbehinds prevent matching already-processed URLs
        standalone_url_pattern = r"(?<!\]\()(?<!\()(?<!href=\")(?<!\[)\b(https?://[a-zA-Z0-9][-a-zA-Z0-9._~:/?#[\]@!$&'()*+,;=%]*[a-zA-Z0-9/])(?!\))(?!\])"
        updated_text = re.sub(standalone_url_pattern, standalone_url_replacer, updated_text)
        logger.info(f"Processed {standalone_urls_found} standalone plain URLs")

        # Fix periods around citations to ensure correct placement:
        # Desired: "text ends here.[1](url) [2](url)"
        # Not: "text ends here [1](url)" or "text ends here [1](url)." or "text ends here. [1](url)."
        def fix_citation_periods(text):
            # Step 1: Remove periods that appear between consecutive citations
            pattern_between = r"(\[[^\]]+\]\([^)]+\))\.(\s*)(?=\[[^\]]+\]\([^)]+\))"
            prev_text = None
            while prev_text != text:
                prev_text = text
                text = re.sub(pattern_between, r"\1\2", text)

            # Step 2: Remove any trailing period immediately after a citation
            text = re.sub(r"(\[[^\]]+\]\([^)]+\))\.", r"\1", text)

            # Step 3: Ensure a period exists before the first citation in a group
            # when it appears at the end of a sentence (end of line or end of text).
            # "word [N](url)...[M](url)\n" → "word.[N](url)...[M](url)\n"
            # Also handle "word. [N](url)" → "word.[N](url)" (remove space after existing period)
            text = re.sub(r"(\w)\.\s+(\[\d+\]\([^)]+\))", r"\1.\2", text)
            # Add period before end-of-line citation groups that lack one
            text = re.sub(r"(\w)\s+((?:\[\d+\]\([^)]+\)\s*)+)$", r"\1.\2", text, flags=re.MULTILINE)

            # Step 4: Fix any double periods that might have been created
            text = re.sub(r"\.\.+", ".", text)

            return text

        # Repair malformed numeric-URL citations, e.g. [3](3) -> [3](url) and
        # [3](3)[3](url) -> [3](url). Study numbering is global, so the number
        # maps directly onto report_citations.
        def _resolve_numeric_citation(num_str):
            idx = int(num_str) - 1
            if 0 <= idx < len(report_citations):
                return num_str, _build_citation_url(report_citations[idx])
            return None

        updated_text = fix_numeric_url_citations(updated_text, _resolve_numeric_citation)

        updated_text = fix_citation_periods(updated_text)

        # Fix spacing between closing parenthesis and opening bracket
        updated_text = re.sub(r"\)\[", ") [", updated_text)

        logger.info("Citations replacement completed")
        return updated_text

    except Exception as e:
        logger.error(f"Error replacing citations: {e!s}")
        return ""


def generate_section_summary(section_content, chat_id: str = None, user_id: str = None):
    formatted_summary_prompt = CARD_SUMMARY_PROMPT.format(section_content=section_content)
    try:
        logger.info("Sending section summary request to OpenAI API")
        response = SYNC_OPENAI_CLIENT.chat.completions.create(
            model=CARD_SUMMARY_MODEL,
            messages=[{"role": "user", "content": formatted_summary_prompt}],
            tools=[CARD_SUMMARY_SCHEMA],
            tool_choice={
                "type": "function",
                "function": {"name": "generate_report_section_summary"},
            },
        )
        save_raw_llm_response(
            response, CARD_SUMMARY_MODEL, "Summarizing a report section", chat_id, user_id=user_id
        )
        result = json.loads(response.choices[0].message.tool_calls[0].function.arguments)
        summary = result["summary"]
        logger.info("Successfully generated section summary")
        return summary

    except Exception as e:
        logger.error(f"Error with OpenAI API: {e!s}")
        logger.info("Falling back to Gemini API")

        # DISABLED: Perplexity fallback, superseded by the Gemini fallback below
        # (kept for reference/rollback).
        # payload = {
        #     "model": "sonar-pro",
        #     "messages": [{"role": "user", "content": formatted_summary_prompt}],
        #     "temperature": 0.1,
        #     "max_tokens": 8000
        # }
        # headers = {
        #     "Authorization": f"Bearer {PERPLEXITY_API_KEY}",
        #     "Content-Type": "application/json"
        # }
        # resp = requests.post("https://api.perplexity.ai/chat/completions", headers=headers, json=payload)
        # response_content = resp.json()["choices"][0]["message"]["content"]

        gemini_client = genai.Client(api_key=GEMINI_API_KEY)
        interaction = gemini_client.interactions.create(
            model=GEMINI_SUMMARY_MODEL,
            input=formatted_summary_prompt,
            response_format={
                "type": "text",
                "mime_type": "application/json",
                "schema": CARD_SUMMARY_SCHEMA_GEMINI,
            },
            generation_config={
                "temperature": 0.1,
                "max_output_tokens": 8000,
                "thinking_config": {"thinking_budget": 0},
            },
        )
        save_raw_llm_response(
            interaction,
            GEMINI_SUMMARY_MODEL,
            "Summarizing a report section (backup)",
            chat_id,
            user_id=user_id,
        )

        response_content = strip_json_code_fence(interaction.output_text)

        # Try to parse as JSON, but handle plain text if needed
        try:
            result = json.loads(response_content)
            summary = result["summary"]
        except json.JSONDecodeError:
            # If not valid JSON, assume the content is the summary itself
            summary = response_content

        logger.info("Successfully received response from Gemini API")
        return summary


def extract_markdown_citations(text: str) -> list:
    """
    Extracts all markdown-style citations from a string.
    Expected format: [Name](URL)

    Args:
        text (str): Input text containing markdown citations.

    Returns:
        list: A list of unique citation strings in original format.
    """
    # Regex to match [Text](URL)
    pattern = r"\[[^\]]+\]\([^)]+\)"

    matches = re.findall(pattern, text)

    # Remove duplicates while preserving order
    seen = set()
    citations = []
    for match in matches:
        if match not in seen:
            seen.add(match)
            citations.append(match)

    return citations


def replace_citation_label_with_clean_domain(text: str) -> str:
    """
    Replaces markdown citation labels with clean domain.
    Args:
        text: The text in markdown format.
    Returns:
        The text with citation labels replaced with clean domain.
    """
    pattern = r"\[([^\]]+)\]\(([^)]+)\)"

    def extract_clean_domain(url):
        if not url.startswith(("http://", "https://")):
            temp_url = "http://" + url
        else:
            temp_url = url

        parsed = urlparse(temp_url)
        domain = parsed.netloc.lower().replace("www.", "")

        parts = domain.split(".")

        if len(parts) >= 3:
            return parts[-2]  # main domain
        elif len(parts) == 2:
            return parts[0]
        else:
            return domain

    def replacer(match):
        url = match.group(2)
        clean_name = extract_clean_domain(url)
        return f"[{clean_name}]({url})"

    return re.sub(pattern, replacer, text)


def generate_summary_for_ask_caspr(
    section_content: str, citations_list: list, chat_id: str = None, user_id: str = None
) -> str:
    """
    Generate a summary for the section content for ask caspr.
    Args:
        section_content: The section content in markdown format.
    Returns:
        The summary for the section content in markdown format.

    """
    formatted_summary_prompt = SECTION_OR_SUBSECTION_SUMMARY_PROMPT.format(
        section_content=section_content, citations_list=citations_list
    )
    try:
        logger.info("Sending section summary request to OpenAI API")
        response = SYNC_OPENAI_CLIENT.chat.completions.create(
            model=CARD_SUMMARY_MODEL,
            messages=[{"role": "user", "content": formatted_summary_prompt}],
            tools=[CARD_SUMMARY_SCHEMA],
            tool_choice={
                "type": "function",
                "function": {"name": "generate_report_section_summary"},
            },
        )
        save_raw_llm_response(
            response,
            CARD_SUMMARY_MODEL,
            "Creating a summary for Ask Caspr",
            chat_id,
            user_id=user_id,
        )
        result = json.loads(response.choices[0].message.tool_calls[0].function.arguments)
        summary = result["summary"]
        logger.info("Successfully generated section summary")
        return summary

    except Exception as e:
        logger.error(f"Error with OpenAI API: {e!s}")
        logger.info("Falling back to Gemini API")

        # DISABLED: Perplexity fallback, superseded by the Gemini fallback below
        # (kept for reference/rollback).
        # payload = {
        #     "model": "sonar-pro",
        #     "messages": [{"role": "user", "content": formatted_summary_prompt}],
        #     "temperature": 0.1,
        #     "max_tokens": 8000
        # }
        # headers = {
        #     "Authorization": f"Bearer {PERPLEXITY_API_KEY}",
        #     "Content-Type": "application/json"
        # }
        # resp = requests.post("https://api.perplexity.ai/chat/completions", headers=headers, json=payload)
        # response_content = resp.json()["choices"][0]["message"]["content"]

        gemini_client = genai.Client(api_key=GEMINI_API_KEY)
        interaction = gemini_client.interactions.create(
            model=GEMINI_SUMMARY_MODEL,
            input=formatted_summary_prompt,
            response_format={
                "type": "text",
                "mime_type": "application/json",
                "schema": CARD_SUMMARY_SCHEMA_GEMINI,
            },
            generation_config={
                "temperature": 0.1,
                "max_output_tokens": 8000,
                "thinking_config": {"thinking_budget": 0},
            },
        )
        save_raw_llm_response(
            interaction,
            GEMINI_SUMMARY_MODEL,
            "Creating a summary for Ask Caspr (backup)",
            chat_id,
            user_id=user_id,
        )

        response_content = strip_json_code_fence(interaction.output_text)

        # Try to parse as JSON, but handle plain text if needed
        try:
            result = json.loads(response_content)
            summary = result["summary"]
        except json.JSONDecodeError:
            # If not valid JSON, assume the content is the summary itself
            summary = response_content

        logger.info("Successfully received response from Gemini API")
        return summary


def update_summaries_for_ask_caspr(card: dict, chat_id: str = None, user_id: str = None) -> dict:
    """
    Updates the summaries for the card.
    Args:
        card: The card to update.
    Returns:
        The updated card.
    """
    try:
        logger.info("Updating summaries for ask caspr")
        if not card["section"][0]["content"] or card["section"][0]["content"].strip() == "":
            card["section"][0]["summary"] = ""
        else:
            citations = extract_markdown_citations(card["section"][0]["content"])
            section_summary = generate_summary_for_ask_caspr(
                card["section"][0]["content"], citations, chat_id=chat_id, user_id=user_id
            )
            section_summary = replace_citation_label_with_clean_domain(section_summary)
            card["section"][0]["summary"] = section_summary

        for sub_section in card["sub_sections"]:
            if not sub_section["content"] or sub_section["content"].strip() == "":
                sub_section["summary"] = ""
            else:
                citations = extract_markdown_citations(sub_section["content"])
                sub_section_summary = generate_summary_for_ask_caspr(
                    sub_section["content"], citations, chat_id=chat_id, user_id=user_id
                )
                sub_section_summary = replace_citation_label_with_clean_domain(sub_section_summary)
                sub_section["summary"] = sub_section_summary
    except Exception as e:
        logger.error(f"Error updating summaries for card: {e}")
        return card
    return card


def generate_cumulative_summary(
    previous_cumulative_summary, new_section_summary, chat_id: str = None, user_id: str = None
):
    try:
        formatted_merge_prompt = MERGE_CUMULATIVE_SUMMARY_PROMPT.format(
            previous_cumulative_summary=previous_cumulative_summary,
            new_section_summary=new_section_summary,
        )

        logger.info("Sending cumulative summary request to OpenAI API")
        response = SYNC_OPENAI_CLIENT.chat.completions.create(
            model=CARD_SUMMARY_MODEL,
            messages=[{"role": "user", "content": formatted_merge_prompt}],
            tools=[MERGE_CUMULATIVE_SUMMARY_SCHEMA],
            tool_choice={"type": "function", "function": {"name": "merge_into_cumulative_summary"}},
        )
        save_raw_llm_response(
            response,
            CARD_SUMMARY_MODEL,
            "Building the overall report summary",
            chat_id,
            user_id=user_id,
        )

        result = json.loads(response.choices[0].message.tool_calls[0].function.arguments)
        cm_summary = result["cumulative_summary"]
        logger.info("Successfully generated cumulative summary")
        return cm_summary

    except Exception as e:
        logger.error(f"Error with OpenAI API: {e!s}")
        logger.info("Falling back to Gemini API")

        # DISABLED: Perplexity fallback, superseded by the Gemini fallback below
        # (kept for reference/rollback).
        # payload = {
        #     "model": "sonar-pro",
        #     "messages": [{"role": "user", "content": formatted_merge_prompt}],
        #     "temperature": 0.1,
        #     "max_tokens": 8000
        # }
        # headers = {
        #     "Authorization": f"Bearer {PERPLEXITY_API_KEY}",
        #     "Content-Type": "application/json"
        # }
        # resp = requests.post("https://api.perplexity.ai/chat/completions", headers=headers, json=payload)
        # response_content = resp.json()["choices"][0]["message"]["content"]

        gemini_client = genai.Client(api_key=GEMINI_API_KEY)
        interaction = gemini_client.interactions.create(
            model=GEMINI_ES_MODEL_ID,
            input=formatted_merge_prompt,
            response_format={
                "type": "text",
                "mime_type": "application/json",
                "schema": MERGE_CUMULATIVE_SUMMARY_SCHEMA_GEMINI,
            },
            generation_config={
                "temperature": 0.1,
                "max_output_tokens": 8000,
                "thinking_config": {"thinking_budget": 0},
            },
        )
        save_raw_llm_response(
            interaction,
            GEMINI_ES_MODEL_ID,
            "Building the overall report summary (backup)",
            chat_id,
            user_id=user_id,
        )

        response_content = strip_json_code_fence(interaction.output_text)

        try:
            result = json.loads(response_content)
            cm_summary = result["cumulative_summary"]
        except json.JSONDecodeError:
            cm_summary = response_content

        logger.info("Successfully received response from Gemini API")

        return cm_summary


def refine_cumulative_summary(
    cards_for_db: list,
    max_retries: int = 3,
    retry_delay: int = 5,
    chat_id: str = None,
    user_id: str = None,
) -> str:
    """
    Extracts all section summaries from cards_for_db and generates a 500-600 word
    executive summary by sending the stacked summaries to the LLM.

    Args:
        cards_for_db: List of card dicts from the report generation pipeline.
        max_retries: Number of retry attempts for API calls.
        retry_delay: Seconds between retries.
    Returns:
        A 500-600 word executive summary string.
    """
    logger.info("Extracting summaries from cards_for_db for executive summary generation")

    summaries = []
    for card in cards_for_db:
        if card.get("summary"):
            summaries.append(card["summary"])

    if not summaries:
        logger.warning("No summaries found in cards_for_db")
        raise Exception("No summaries found in cards_for_db")

    stacked_summaries = "\n\n".join(summaries)
    logger.info(f"Extracted {len(summaries)} section summaries from cards_for_db")

    formatted_refine_prompt = REFINE_CUMULATIVE_SUMMARY_PROMPT.format(
        cumulative_summary=stacked_summaries
    )

    try:
        # raise Exception("test")
        retry_count = 0
        while retry_count < max_retries:
            try:
                logger.info(
                    f"OpenAI API attempt {retry_count + 1}/{max_retries} for executive summary"
                )
                response = SYNC_OPENAI_CLIENT.chat.completions.create(
                    model=CARD_SUMMARY_MODEL,
                    messages=[{"role": "user", "content": formatted_refine_prompt}],
                    tools=[REFINE_CUMULATIVE_SUMMARY_SCHEMA],
                    tool_choice={
                        "type": "function",
                        "function": {"name": "refine_cumulative_summary"},
                    },
                )
                save_raw_llm_response(
                    response,
                    CARD_SUMMARY_MODEL,
                    "Polishing the executive summary",
                    chat_id,
                    user_id=user_id,
                )
                result = json.loads(response.choices[0].message.tool_calls[0].function.arguments)
                refined_summary = result["cumulative_summary"]
                refined_summary = clean_executive_summary(refined_summary)
                logger.info("Successfully generated executive summary from cards_for_db summaries")
                return refined_summary

            except Exception as e:
                retry_count += 1
                logger.warning(f"OpenAI API attempt {retry_count}/{max_retries} failed: {e!s}")
                if retry_count < max_retries:
                    logger.info(f"Retrying in {retry_delay} seconds...")
                    time.sleep(retry_delay)
                else:
                    logger.error(f"All OpenAI API retry attempts failed: {e!s}")
                    raise Exception(f"OpenAI failed after {max_retries} retries: {e!s}")

    except Exception as e:
        logger.error(f"OpenAI failed: {e!s}, falling back to Gemini")

        # DISABLED: Perplexity fallback, superseded by the Gemini fallback below
        # (kept for reference/rollback).
        # payload = {
        #     "model": "sonar-pro",
        #     "messages": [{"role": "user", "content": formatted_refine_prompt}],
        #     "temperature": 0.1,
        # }
        # headers = {
        #     "Authorization": f"Bearer {PERPLEXITY_API_KEY}",
        #     "Content-Type": "application/json"
        # }
        # resp = requests.post("https://api.perplexity.ai/chat/completions", headers=headers, json=payload)
        # response_content = resp.json()["choices"][0]["message"]["content"]

        retry_count = 0
        while retry_count < max_retries:
            try:
                logger.info(f"Falling back to Gemini API (attempt {retry_count + 1}/{max_retries})")

                gemini_client = genai.Client(api_key=GEMINI_API_KEY)
                interaction = gemini_client.interactions.create(
                    model=GEMINI_ES_MODEL_ID,
                    input=formatted_refine_prompt,
                    response_format={
                        "type": "text",
                        "mime_type": "application/json",
                        "schema": REFINE_CUMULATIVE_SUMMARY_SCHEMA_GEMINI,
                    },
                    generation_config={
                        "temperature": 0.1,
                        "max_output_tokens": 8000,
                        "thinking_config": {"thinking_budget": 0},
                    },
                )
                save_raw_llm_response(
                    interaction,
                    GEMINI_ES_MODEL_ID,
                    "Polishing the executive summary (backup)",
                    chat_id,
                    user_id=user_id,
                )

                response_content = strip_json_code_fence(interaction.output_text)

                try:
                    result = json.loads(response_content)
                    refined_summary = result["cumulative_summary"]
                except json.JSONDecodeError:
                    refined_summary = response_content

                refined_summary = clean_executive_summary(refined_summary)
                logger.info("Successfully generated executive summary with Gemini API")
                return refined_summary

            except Exception as e:
                retry_count += 1
                logger.warning(f"Gemini API attempt {retry_count}/{max_retries} failed: {e!s}")
                if retry_count < max_retries:
                    logger.info(f"Retrying in {retry_delay} seconds...")
                    time.sleep(retry_delay)
                else:
                    logger.critical(f"All Gemini API retry attempts failed: {e!s}")
                    return stacked_summaries


# def refine_cumulative_summary(cumulative_summary, max_retries=3, retry_delay=5)->str:
#     """
#     Refines the cumulative summary using OpenAI API.
#     Args:
#         cumulative_summary: The cumulative summary to refine.
#     Returns:
#         The refined cumulative summary.
#     """
#     try:
#         logger.info("Attempting to refine cumulative summary using OpenAI API")

#         if not cumulative_summary:
#             # return "The report presents a comprehensive analysis of current challenges and opportunities, identifying critical issues such as operational inefficiencies, market shifts, and evolving customer needs. Major findings highlight persistent bottlenecks in workflow, a growing demand for digital solutions, and strong user preference for intuitive interfaces. Data indicates that targeted process improvements and technology upgrades could yield significant cost savings and enhance customer satisfaction. The report recommends prioritizing investment in automation, streamlining communication channels, and adopting a customer-centric approach to product development. These actions are projected to improve performance, capture emerging market opportunities, and strengthen competitive positioning. The overall message emphasizes the importance of agile adaptation and strategic innovation to address identified gaps and drive sustainable growth."
#             raise Exception("Cumulative summary is empty")

#         formatted_refine_prompt = REFINE_CUMULATIVE_SUMMARY_PROMPT.format(
#             cumulative_summary=cumulative_summary
#         )

#         retry_count = 0
#         while retry_count < max_retries:
#             try:
#                 logger.info(f"OpenAI API attempt {retry_count + 1}/{max_retries}")
#                 response = SYNC_OPENAI_CLIENT.chat.completions.create(
#                     model="gpt-4o",
#                     messages=[{"role": "user", "content": formatted_refine_prompt}],
#                     tools=[REFINE_CUMULATIVE_SUMMARY_SCHEMA],
#                     tool_choice={"type": "function", "function": {"name": "refine_cumulative_summary"}},
#                     max_tokens=1000
#                 )

#                 result = json.loads(response.choices[0].message.tool_calls[0].function.arguments)
#                 refined_summary = result['cumulative_summary']
#                 refined_summary = clean_executive_summary(refined_summary)
#                 logger.info("Successfully refined cumulative summary with OpenAI API")
#                 return refined_summary

#             except Exception as e:
#                 retry_count += 1
#                 logger.warning(f"OpenAI API attempt {retry_count}/{max_retries} failed: {str(e)}")
#                 if retry_count < max_retries:
#                     logger.info(f"Retrying in {retry_delay} seconds...")
#                     time.sleep(retry_delay)
#                 else:
#                     logger.error(f"All OpenAI API retry attempts failed: {str(e)}")
#                     return cumulative_summary

#     except Exception as e:
#         logger.error(f"Error with OpenAI API after all retries: {str(e)}")

#         formatted_refine_prompt = REFINE_CUMULATIVE_SUMMARY_PROMPT.format(
#             cumulative_summary=cumulative_summary
#         )
#         retry_count = 0
#         while retry_count < max_retries:
#             try:
#                 logger.info(f"Falling back to Perplexity API (attempt {retry_count + 1}/{max_retries})")

#                 payload = {
#                     "model": "sonar-pro",
#                     "messages": [{"role": "user", "content": formatted_refine_prompt}],
#                     "response_format": {
#                         "type": "json_schema",
#                         "json_schema": {"schema": REFINE_CUMULATIVE_SUMMARY_SCHEMA}
#                     },
#                     "temperature": 0.1,
#                     "max_tokens": 800
#                 }

#                 headers = {
#                     "Authorization": f"Bearer {PERPLEXITY_API_KEY}",
#                     "Content-Type": "application/json"
#                 }

#                 resp = requests.post("https://api.perplexity.ai/chat/completions", headers=headers, json=payload)
#                 if resp.status_code != 200:
#                     logger.error(f'Perplexity API error: {resp.text}')
#                     raise Exception(f"Perplexity API returned status code {resp.status_code}")

#                 response_content = resp.json()["choices"][0]["message"]["content"]

#                 try:
#                     result = json.loads(response_content)
#                     refined_summary = result['cumulative_summary']
#                 except json.JSONDecodeError:
#                     refined_summary = response_content

#                 refined_summary = clean_executive_summary(refined_summary)
#                 logger.info("Successfully refined cumulative summary with Perplexity API")
#                 return refined_summary

#             except Exception as e:
#                 retry_count += 1
#                 logger.warning(f"Perplexity API attempt {retry_count}/{max_retries} failed: {str(e)}")
#                 if retry_count < max_retries:
#                     logger.info(f"Retrying in {retry_delay} seconds...")
#                     time.sleep(retry_delay)
#                 else:
#                     logger.critical(f"All Perplexity API retry attempts failed: {str(e)}")
#                     return cumulative_summary


def _resolve_viz_for_md(viz: str):
    """Convert a visualization value to its markdown representation.
    - mermaid:: prefixed strings become fenced mermaid code blocks.
    - S3 .html paths are downloaded and returned as raw HTML content.
    - S3 .png/.svg paths are downloaded and converted to base64 inline images.
    - Empty/None/invalid values return None.
    """
    if not viz or not isinstance(viz, str) or not viz.strip():
        return None
    viz = viz.strip()
    if viz.startswith("mermaid::"):
        mermaid_code = viz[len("mermaid::") :]
        return f"\n```mermaid\n{mermaid_code}\n```\n"
    if not viz.startswith("s3://"):
        return None
    if viz.endswith(".html"):
        return _download_html_from_s3(viz)
    if viz.endswith(".png") or viz.endswith(".svg"):
        return image_to_base64_s3(viz)
    return None


def _download_html_from_s3(s3_path: str) -> str:
    """Download an HTML file from S3 and return its content as a string."""
    try:
        path = s3_path.replace("s3://", "")
        bucket_name, object_key = path.split("/", 1)
        response = S3_boto3_client.get_object(Bucket=bucket_name, Key=object_key)
        html_content = response["Body"].read().decode("utf-8")
        return html_content
    except Exception as e:
        logger.error(f"Failed to download HTML from S3: {s3_path} - {e!s}")
        return None


def convert_json_to_md(complete_report_json, table_visualization_map=None):
    md_lines = []
    after_exec_summary = False
    section_counter = 0

    for index, item in enumerate(complete_report_json):
        # ---- Wrapped "section" form: {"section":[{"name":..., "content":..., "tables":[...]}], "sub_sections":[...]}
        if "section" in item and isinstance(item["section"], list) and item["section"]:
            name = (item["section"][0].get("name") or "").strip()
            # Replace pipe characters with hyphens in section name
            name = name.replace("|", "-")

            # Handle content which could be a string or dictionary
            section_content = item["section"][0].get("content") or ""
            if isinstance(section_content, dict):
                # Extract the actual content from the dictionary if it's JSONB
                content = (section_content.get("content") or "").strip()
            else:
                content = section_content.strip()

            sec_tables = item["section"][0].get("tables", []) or []
            lname = name.lower()

            # Meta blocks (title, subtitle, toc, executive_summary) are wrapped the same way
            if lname == "title":
                if content:
                    md_lines.append(f"# {content}\n")
                continue
            if lname == "subtitle":
                if content:
                    md_lines.append(f"### {content}\n")
                continue
            if lname == "table_of_contents":
                md_lines.append(f"## Table of Contents\n\n{content}\n")
                continue
            if lname == "executive_summary":
                md_lines.append(f"## Executive Summary\n\n{content}\n")
                after_exec_summary = True
                continue

            # Real section
            section_counter += 1
            section_num = section_counter
            md_lines.append(f"## {section_num}. {name}\n")

            # If there's section-level content, we may need to insert viz under tables inside it
            if content:
                if after_exec_summary and sec_tables and table_visualization_map:
                    # try to inline each table's viz after its table markdown
                    handled_ids = set()
                    for t in sec_tables:
                        tid = (t.get("table_id") or "").strip()
                        viz = (t.get("visualization") or "").strip()
                        logger.info(f"Found visualization for table: {viz}")
                        viz = _resolve_viz_for_md(viz)
                        logger.info("Resolved visualization for markdown")
                        if not tid or not viz:
                            continue
                        table_md = table_visualization_map.get(tid)
                        if not table_md:
                            continue
                        pos = content.find(table_md)
                        if pos != -1:
                            insert_at = pos + len(table_md)
                            content = (
                                content[:insert_at] + "\n\n" + viz + "\n" + content[insert_at:]
                            )
                            handled_ids.add(tid)
                    md_lines.append(f"{content}\n")

                    # fallback: for any section table not found in content, append table + viz
                    for t in sec_tables:
                        tid = (t.get("table_id") or "").strip()
                        viz = (t.get("visualization") or "").strip()
                        viz = _resolve_viz_for_md(viz)
                        if not tid or not viz or tid in handled_ids:
                            continue
                        table_md = table_visualization_map.get(tid)
                        if table_md:
                            md_lines.append(f"{table_md}\n{viz}\n")
                        else:
                            # no table markdown in the map => just append viz so it's not lost
                            md_lines.append(f"{viz}\n")
                else:
                    # before Exec Summary or no tables => just push content
                    md_lines.append(f"{content}\n")

            # Also support a legacy top-level "section_tables" if present on item
            section_tables_top = item.get("section_tables", []) or []
            if after_exec_summary and section_tables_top and table_visualization_map:
                for t in section_tables_top:
                    tid = (t.get("table_id") or "").strip()
                    viz = (t.get("visualization") or "").strip()
                    viz = _resolve_viz_for_md(viz)
                    if not tid:
                        continue
                    table_md = table_visualization_map.get(tid)
                    if table_md and viz:
                        # naive append (no content to inline into at this point)
                        md_lines.append(f"{table_md}\n{viz}\n")
                    elif table_md:
                        md_lines.append(f"{table_md}\n")
                    elif viz:
                        md_lines.append(f"{viz}\n")

            # Sub-sections
            for sub_idx, sub in enumerate(item.get("sub_sections", [])):
                sub_name = (sub.get("name") or "").strip()
                # Replace pipe characters with hyphens in sub-section name
                sub_name = sub_name.replace("|", "-")

                # Handle sub-section content which could be a string or dictionary
                sub_section_content = sub.get("content") or ""
                if isinstance(sub_section_content, dict):
                    # Extract the actual content from the dictionary if it's JSONB
                    sub_content = (sub_section_content.get("content") or "").strip()
                else:
                    sub_content = sub_section_content.strip()

                sub_tables = sub.get("tables", []) or []  # NOTE: tables inside each subsection

                if sub_name:
                    md_lines.append(f"### {section_num}.{sub_idx + 1}. {sub_name}\n")

                if sub_content:
                    if after_exec_summary and sub_tables and table_visualization_map:
                        handled_ids = set()
                        # inline each table's viz right under its markdown in sub_content
                        for t in sub_tables:
                            tid = (t.get("table_id") or "").strip()
                            viz = (t.get("visualization") or "").strip()
                            viz = _resolve_viz_for_md(viz)
                            if not tid or not viz:
                                continue
                            table_md = table_visualization_map.get(tid)
                            if not table_md:
                                continue
                            pos = sub_content.find(table_md)
                            if pos != -1:
                                insert_at = pos + len(table_md)
                                sub_content = (
                                    sub_content[:insert_at]
                                    + "\n\n"
                                    + viz
                                    + "\n"
                                    + sub_content[insert_at:]
                                )
                                handled_ids.add(tid)
                        md_lines.append(f"{sub_content}\n")

                        # fallback: if a table wasn't found in content, append table + viz after content
                        for t in sub_tables:
                            tid = (t.get("table_id") or "").strip()
                            viz = (t.get("visualization") or "").strip()
                            viz = _resolve_viz_for_md(viz)
                            if not tid or not viz or tid in handled_ids:
                                continue
                            table_md = table_visualization_map.get(tid)
                            if table_md:
                                md_lines.append(f"{table_md}\n{viz}\n")
                            else:
                                md_lines.append(f"{viz}\n")
                    else:
                        md_lines.append(f"{sub_content}\n")

                # If there is no content but there are tables with ids, append them plainly
                if (
                    after_exec_summary
                    and not sub_content
                    and sub_tables
                    and table_visualization_map
                ):
                    for t in sub_tables:
                        tid = (t.get("table_id") or "").strip()
                        viz = (t.get("visualization") or "").strip()
                        viz = _resolve_viz_for_md(viz)
                        if not tid:
                            continue
                        table_md = table_visualization_map.get(tid)
                        if table_md and viz:
                            md_lines.append(f"{table_md}\n{viz}\n")
                        elif table_md:
                            md_lines.append(f"{table_md}\n")
                        elif viz:
                            md_lines.append(f"{viz}\n")

            continue  # done with wrapped form

        # Handle legacy flat format
        if "title" in item:
            md_lines.append(f"# {item['title']}\n")
        elif "subtitle" in item:
            md_lines.append(f"### {item['subtitle']}\n")
        elif "table_of_contents" in item:
            md_lines.append(f"## Table of Contents\n\n{item['table_of_contents']}\n")
        elif "executive_summary" in item:
            md_lines.append(f"## Executive Summary\n\n{item['executive_summary']}\n")
            after_exec_summary = True
        elif "section" in item and isinstance(item["section"], str):
            section_counter += 1
            section_num = section_counter
            # Replace pipe characters with hyphens in section name for legacy format
            section_name = item["section"].replace("|", "-")
            md_lines.append(f"## {section_num}. {section_name}\n")

            # Handle content which could be a string or dictionary
            if "content" in item:
                item_content = item["content"] or ""
                if isinstance(item_content, dict):
                    # Extract the actual content from the dictionary if it's JSONB
                    content_str = (item_content.get("content") or "").strip()
                    md_lines.append(f"{content_str}\n")
                elif item_content:
                    md_lines.append(f"{item_content}\n")

            # tables at flat level
            if after_exec_summary and table_visualization_map:
                for t in item.get("tables", []) or item.get("section_tables", []) or []:
                    tid = (t.get("table_id") or "").strip()
                    viz = (t.get("visualization") or "").strip()
                    viz = _resolve_viz_for_md(viz)
                    if not tid:
                        continue
                    table_md = table_visualization_map.get(tid)
                    if table_md and viz:
                        md_lines.append(f"{table_md}\n{viz}\n")
                    elif table_md:
                        md_lines.append(f"{table_md}\n")
                    elif viz:
                        md_lines.append(f"{viz}\n")
        elif "Summary" in item:
            section_counter += 1
            summary_content = item["Summary"]
            if isinstance(summary_content, dict):
                summary_text = (summary_content.get("content") or "").strip()
            else:
                summary_text = summary_content
            md_lines.append(f"## {section_counter}. Summary\n{summary_text}\n")

    final_md = "\n".join(md_lines)
    final_md = re.sub(r"&nbsp;", " ", final_md)
    final_md = html.unescape(final_md)
    final_md = re.sub(r" {2,}", " ", final_md)

    # Final citation guarantee: every inline markdown-link citation must render as
    # Reports render NAMED sources, so the old `_enforce_numbered_citations` pass
    # here was actively harmful: it rewrote every [Source Name](url) the cards had
    # produced back into [N](url), which is why numbered citations survived every
    # upstream fix. Keep only the guard it existed for — an LLM (the card fixer or
    # the executive summary) emitting an ugly [https://...](https://...) — by
    # naming labels that are a bare URL or empty, and leaving real names alone.
    final_md = name_bare_citation_labels(final_md)
    return final_md


def _enforce_numbered_citations(md_text, complete_report_json=None, citation_url_map=None):
    """Rewrite every inline markdown-link citation to the [number](url) format.

    A label is left untouched only when it is already a plain number. Markdown
    images (``![alt](url)``) are excluded via a negative lookbehind. Numbers are
    resolved in priority order: the explicit ``citation_url_map`` (the report's
    authoritative URL -> number map), then the cards' per-card ``citations``
    maps, then any ``[n](url)`` citations already present in the text, and
    finally by assigning the next available number for previously unseen URLs.

    Args:
        md_text: The markdown text to normalise.
        complete_report_json: Optional list of report cards; each card's
            ``citations`` dict ({url: number}) is used to seed the numbering.
        citation_url_map: Optional {url: number} mapping for the whole report.
            Pass this for accurate numbering that matches the report's sources
            list, especially for URLs that never appear as a valid [n](url).
    """
    if not md_text:
        return md_text

    md_text = repair_malformed_citation_markdown(md_text)

    def _base(url):
        # Drop any appended text fragment before looking the URL up in the map.
        stripped = url.split("#:~:text=")[0]
        try:
            return clean_url(stripped)
        except Exception:
            return stripped

    url_to_number = {}

    # 1) Seed URL -> number from the explicit report-wide citation map (most
    #    authoritative — matches the rendered sources list exactly).
    if isinstance(citation_url_map, dict):
        for url, num in citation_url_map.items():
            if isinstance(num, int) and num > 0:
                url_to_number.setdefault(_base(url), num)

    # 2) Seed from the authoritative per-card citation maps.
    for item in complete_report_json or []:
        citations = item.get("citations") if isinstance(item, dict) else None
        if isinstance(citations, dict):
            for url, num in citations.items():
                if isinstance(num, int) and num > 0:
                    url_to_number.setdefault(_base(url), num)

    # 3) Seed from citations already rendered as [n](url) in the text.
    for m in re.finditer(r"\[(\d+)\]\((https?://[^)\s]+)\)", md_text):
        url_to_number.setdefault(_base(m.group(2)), int(m.group(1)))

    def _number_for(url):
        for key in (_base(url), clean_url(url) if url else url):
            if key in url_to_number:
                return url_to_number[key]
        next_num = (max(url_to_number.values()) if url_to_number else 0) + 1
        url_to_number[_base(url)] = next_num
        return next_num

    def _replace(match):
        label = match.group(1)
        url = match.group(2)
        if label.strip().isdigit():
            return match.group(0)
        return f"[{_number_for(url)}]({url})"

    # Any non-image markdown link pointing to an http(s) URL is a citation.
    return re.sub(r"(?<!!)\[([^\]]*)\]\((https?://[^)]+)\)", _replace, md_text)


def cards_for_db_to_cards_for_fe(card):
    if "title" in card:
        return card
    if "subtitle" in card:
        return card
    if "table_of_content" in card:
        return card
    if "sub_sections" in card:
        content = ""
        for subsection in card["sub_sections"]:
            name = subsection["name"].replace("-", "").replace("#", "").strip()
            subsection_content = subsection["content"].strip()
            content += name + "\n\n" + subsection_content + "\n\n"
    new_citations = {url: idx + 1 for idx, url in enumerate(card["citations"])}
    citation_pattern = r"\[(\d+)\]\((https?://[^)]+)\)"
    updated_content = re.sub(
        citation_pattern,
        lambda match: f"[{new_citations[match.group(2)]}]({match.group(2)})",
        content,
    )
    modified_section_card = {
        "section": card["section"],
        "content": content,
        "citations": new_citations,
        "summary": card["summary"],
    }
    modified_section_card["content"] = updated_content

    return modified_section_card


class Title(BaseModel):
    title: str = Field(description="The title for the table")


from langchain_openai import ChatOpenAI


def generate_title_for_table(table, chat_id: str = None, user_id: str = None):
    """Generate a title for the given table using OpenAI
    Args:
        table: The table to generate a title for.
    Returns:
        The title for the table.
    """
    try:
        logger.info("Generating a title for the table")
        prompt = f"You are given a table markdown TABLE:{table}. Generate a Title for the table. Plain text no markdwon pattern"
        client = ChatOpenAI(api_key=OPENAI_API_KEY, model=GENERATE_TITLE_FOR_TABLE_MODEL)
        llm_with_structured_output = client.with_structured_output(Title, include_raw=True)
        title = llm_with_structured_output.invoke(prompt)
        save_raw_llm_response(
            title["raw"],
            GENERATE_TITLE_FOR_TABLE_MODEL,
            "Creating a title for a table",
            chat_id,
            user_id=user_id,
        )
        if title["parsing_error"] is not None:
            raise title["parsing_error"]
        return title["parsed"].title
    except Exception as e:
        logger.error(f"Error generating title for table: {e}")
        logger.info("Falling back to Gemini API for table title")
        try:
            gemini_client = genai.Client(api_key=GEMINI_API_KEY)
            interaction = gemini_client.interactions.create(
                model=GEMINI_GENERATE_TITLE_FOR_TABLE_MODEL,
                input=prompt,
                response_format={
                    "type": "text",
                    "mime_type": "application/json",
                    "schema": Title.model_json_schema(),
                },
                generation_config={
                    "temperature": 0.1,
                    "thinking_config": {"thinking_budget": 0},
                },
            )
            save_raw_llm_response(
                interaction,
                GEMINI_GENERATE_TITLE_FOR_TABLE_MODEL,
                "Creating a title for a table (backup)",
                chat_id,
                user_id=user_id,
            )
            result = json.loads(strip_json_code_fence(interaction.output_text))
            return result.get("title", "")
        except Exception as gemini_e:
            logger.error(f"Gemini fallback for table title also failed: {gemini_e}")
            return ""


def extract_title_and_description_for_table(section_or_subsection_content, table):
    """Extract title and description for the given table - handles multiple formats."""
    try:
        table_stripped = table.strip()

        # Find the table position in the content
        table_start = section_or_subsection_content.find(table_stripped)
        if table_start == -1:
            return "", table

        # Get content before the table
        content_before_table = section_or_subsection_content[:table_start]

        # Only match lines that explicitly start with "Title:" (case insensitive)
        matches = list(
            re.finditer(
                r"Title:\s*([^\n]+?)(?:\n|$)", content_before_table, re.MULTILINE | re.IGNORECASE
            )
        )

        if matches:
            potential_title = matches[-1].group(1).strip()
            if not re.match(r"^\s*(Source|Description):", potential_title, re.IGNORECASE):
                return potential_title, table

        title = generate_title_for_table(table)
        if not title:
            return "", table

        new_table = "Title: " + title + "\n\n" + table_stripped
        return title, new_table

    except Exception:
        return "", table


def replace_table_in_content(content, original_table, updated_table):
    """Replace a table in content when title generation changes the table text."""
    original_table = original_table.strip()
    updated_table = updated_table.strip()
    if not original_table or updated_table == original_table:
        return content
    return content.replace(original_table, updated_table, 1)


def extract_markdown_tables(content_string):

    try:
        table_pattern = re.compile(
            r"(\|[^\n]*\|[ \t]*\n"  # Header row
            + r"\|[ ]*[-:]+[-| :]+[ ]*\|[ \t]*\n"  # Separator row
            + r"(?:\|[^\n]*\|[ \t]*\n)+)"  # Data rows
        )

        tables = table_pattern.findall(content_string)
        logger.info(f"Found {len(tables)} tables in content")

        return tables

    except Exception as e:
        logger.error(f"Error extracting tables: {e!s}")
        return None


class PlotType(BaseModel):
    """To choose the best plot type"""

    plot_type: str = Field(description="the best plot type for the table")


def choosse_the_best_plot_type(table: str, chat_id: str = None, user_id: str = None):
    """Choose the best plot type for the given table using Gemini Nano Banana Pro Preview
    Args:
        table: The table to choose the best plot type for.
    Returns:
        The best plot type for the table.
    """
    try:
        logger.info("Choosing the best plot type for the table")
        structured_llm_for_decision = ANTHROPIC_LLM.with_structured_output(
            PlotType, include_raw=True
        )
        choose_the_best_plot_type_prompt = f"""Choose the best plot type for the following table:
        {table}
        We are interested in visualizing the table, so we need to choose the best plot type for the table.
        Only return the plot type, no other text or commentary.
        """
        decision = structured_llm_for_decision.invoke(choose_the_best_plot_type_prompt)
        save_raw_llm_response(
            decision["raw"],
            ANTHROPIC_MODEL_ID,
            "Choosing the best chart type for the data",
            chat_id,
            user_id=user_id,
        )
        if decision["parsing_error"] is not None:
            raise decision["parsing_error"]
        plot_type = decision["parsed"].plot_type
        should_visualize = plot_type
        return should_visualize
    except Exception:
        try:
            logger.info("Falling back to OpenAI structured output for choose the best plot type")
            client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

            choose_the_best_plot_type_prompt_schema = {
                "type": "function",
                "function": {
                    "name": "plot_type_decision",
                    "description": "Choose the best plot type for the table.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "plot_type": {
                                "type": "string",
                                "description": "The best plot type for the table.",
                            }
                        },
                        "required": ["plot_type"],
                    },
                },
            }
            choose_the_best_plot_type_prompt_template = ChatPromptTemplate.from_template(
                template=choose_the_best_plot_type_prompt
            )
            choose_the_best_plot_type_prompt = choose_the_best_plot_type_prompt_template.format(
                table=table
            )

            response = client.chat.completions.create(
                model=PLOT_TYPE_MODEL,
                messages=[{"role": "user", "content": choose_the_best_plot_type_prompt}],
                tools=[choose_the_best_plot_type_prompt_schema],
                tool_choice={"type": "function", "function": {"name": "plot_type_decision"}},
            )
            save_raw_llm_response(
                response,
                PLOT_TYPE_MODEL,
                "Choosing the best chart type for the data (backup)",
                chat_id,
                user_id=user_id,
            )
            tool_call = response.choices[0].message.tool_calls[0]
            structured_json = json.loads(tool_call.function.arguments)
            plot_type = structured_json.get("plot_type")

            if plot_type is None:
                raise Exception("No visualize_or_not returned by model")
            logger.info(f"Plot type result:----> {plot_type}")
            should_visualize = plot_type
            return should_visualize
        except Exception as e:
            logger.error(f"Error choosing the best plot type: {e}")
            logger.info("Falling back to Gemini API for choose the best plot type")
            try:
                choose_the_best_plot_type_prompt_gemini = f"""Choose the best plot type for the following table:
                {table}
                We are interested in visualizing the table, so we need to choose the best plot type for the table.
                Only return the plot type, no other text or commentary.
                """
                gemini_client = genai.Client(api_key=GEMINI_API_KEY)
                interaction = gemini_client.interactions.create(
                    model=GEMINI_PLOT_TYPE_MODEL,
                    input=choose_the_best_plot_type_prompt_gemini,
                    response_format={
                        "type": "text",
                        "mime_type": "application/json",
                        "schema": PlotType.model_json_schema(),
                    },
                    generation_config={
                        "temperature": 0.1,
                        "thinking_config": {"thinking_budget": 0},
                    },
                )
                save_raw_llm_response(
                    interaction,
                    GEMINI_PLOT_TYPE_MODEL,
                    "Choosing the best chart type for the data (Gemini backup)",
                    chat_id,
                    user_id=user_id,
                )
                result = json.loads(strip_json_code_fence(interaction.output_text))
                plot_type = result.get("plot_type")
                if plot_type is None:
                    raise Exception("No plot_type returned by Gemini")
                return plot_type
            except Exception as gemini_e:
                logger.error(
                    f"Gemini fallback for choose the best plot type also failed: {gemini_e}"
                )
                return None


def _parse_table_to_dataframe(table: str) -> pd.DataFrame | None:
    try:
        html_content = markdown.markdown(table, extensions=["tables"])
        dfs = pd.read_html(io.StringIO(html_content))
        if not dfs:
            return None
        return dfs[0]
    except Exception as e:
        logger.warning(f"Unable to parse table as dataframe: {e!s}")
        return None


def _is_small_table(table: str) -> bool:
    df = _parse_table_to_dataframe(table)
    if df is None or df.empty:
        return False

    row_count, column_count = df.shape
    logger.info(
        f"Parsed table shape before visualization decision: rows={row_count}, columns={column_count}"
    )
    return row_count <= 3 and column_count <= 2


def generate_table_visualization(
    table: str,
    chat_id: str,
    aspect_ratio: str | None = "4:3",
    max_retries: int = 6,
    report_type: str = "study",
    user_id: str = None,
):
    """Generate table visualization with decision-based HTML first, then a Gemini fallback.

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

    if (report_type or "").lower() == "brief" and _is_small_table(table):
        logger.info("Skipping visualization for small table")
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
    try:
        for attempt in range(1, max_retries + 1):
            api_key_num = ((attempt - 1) % 6) + 1
            Google_client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
            try:
                logger.info(f"Gemini image gen attempt {attempt}/{max_retries}")
                interaction = Google_client.interactions.create(
                    model=VISUALIZATION_MODEL,
                    input=TABLE_TO_VIZ_PROMPT.format(table=table),
                    response_format={
                        "type": "image",
                        "aspect_ratio": aspect_ratio,
                    },
                )
                save_raw_llm_response(
                    interaction,
                    VISUALIZATION_MODEL,
                    "Creating a chart image from table data",
                    chat_id,
                    user_id=user_id,
                )

                if interaction.output_image is not None:
                    image_path = f"{uuid7()}.png"
                    with open(image_path, "wb") as f:
                        f.write(base64.b64decode(interaction.output_image.data))
                    return image_path

            except Exception as e:
                logger.error(f"Gemini attempt {attempt}/{max_retries} failed: {e}")
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
    table: str, user_name: str, chat_id: str, report_type: str = "study", user_id: str = None
):
    """Generate visualization for the given table.
    Returns an S3 path for image-based viz (.png) or HTML viz (.html), or a 'mermaid::...' string for mermaid viz.
    """

    image_path = None
    html_temp_path = None

    try:
        image_path = generate_table_visualization(
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

            s3_key = f"{S3_REPORTS_BASE_PATH}/visualizations/{year}/{month}/{day}/{user_name}/chat_{chat_id}/{html_filename}"
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

            s3_key = f"{S3_REPORTS_BASE_PATH}/visualizations/{year}/{month}/{day}/{user_name}/chat_{chat_id}/{image_path}"
            s3_path = s3_controls.upload_file(image_path, s3_key)
            if s3_path:
                logger.info(f"Uploaded image to S3: {s3_path}")
                os.remove(image_path)
                logger.info(f"Deleted image from local directory: {image_path}")
                return s3_path
            else:
                logger.error(f"Failed to upload image to S3: {s3_path}")
                return ""
    except Exception as e:
        logger.error(f"Error generating visualization: {e!s}")
        return ""
    finally:
        if html_temp_path and os.path.exists(html_temp_path):
            os.remove(html_temp_path)
            logger.info(f"Deleted temp HTML file: {html_temp_path}")
        if image_path and os.path.exists(str(image_path)):
            os.remove(image_path)
            logger.info(f"Deleted image from local directory: {image_path}")
        if image_path:
            image_dir = os.path.dirname(str(image_path))
            if (
                image_dir
                and os.path.basename(image_dir).startswith(f"temp_viz_dir_{chat_id}_")
                and os.path.exists(image_dir)
            ):
                shutil.rmtree(image_dir)
                logger.info(f"Deleted temporary directory: {image_dir}")
    return ""


def add_viz_to_card(card, user_name, chat_id, report_type: str = "study", user_id: str = None):
    """Add visualizations to the card
    Args:
        card: The card to add visualizations to.
        user_name: The name of the user.
        chat_id: The id of the chat.
    Returns:
        The card with visualizations added and the table and table id map.
    """
    logger.info("Adding visualizations to card with section")

    section_tables = extract_markdown_tables(card["section"][0]["content"])
    # Here we willl be storing table id and table md in a map for example:
    # table_and_table_id_map = {
    #     'table_id': 'table_md'
    # }
    table_and_table_id_map = {}

    card["section"][0]["tables"] = []
    section_id = str(uuid7())
    card["section"][0]["id"] = section_id

    if section_tables:
        logger.info(f"Found {len(section_tables)} tables in main section content")
        for table in section_tables:
            table_id = str(uuid7())
            logger.debug(f"Generated table ID: {table_id}")
            table_title, updated_table = extract_title_and_description_for_table(
                card["section"][0]["content"], table
            )
            card["section"][0]["content"] = replace_table_in_content(
                card["section"][0]["content"], table, updated_table
            )
            table_and_table_id_map[table_id] = table
            logger.info(
                f"Generating visualization for table in section {card['section'][0]['name']}"
            )
            viz_str = generate_visualization(
                table, user_name, chat_id, report_type=report_type, user_id=user_id
            )
            if viz_str:
                # is_mermaid = isinstance(viz_str, str) and viz_str.startswith("mermaid::")
                is_html = isinstance(viz_str, str) and not viz_str.endswith(".png")
                viz_type = "table_graphs" if is_html else "mermaid"
                logger.info(
                    f"Successfully generated visualization for table {table_id} (type={viz_type})"
                )
                card["section"][0]["tables"].append(
                    {
                        "visualization": viz_str,
                        "table_id": table_id,
                        "table_title": table_title,
                        "visualization_type": viz_type,
                    }
                )
            else:
                logger.warning(f"No visualization generated for table {table_id}")
                card["section"][0]["tables"].append(
                    {
                        "visualization": "",
                        "table_id": "",
                        "table_title": "",
                        "visualization_type": "",
                    }
                )
    else:
        logger.info("No tables found in main section content")
        card["section"][0]["tables"].append(
            {"visualization": "", "table_id": "", "table_title": "", "visualization_type": ""}
        )
        # section_id = str(uuid7())
        # card['section'][0]['id'] = section_id

    logger.info(f"Processing {len(card['sub_sections'])} sub-sections")
    for idx, sub_section in enumerate(card["sub_sections"]):
        logger.info(f"Processing sub-section: {sub_section['name']}")
        card["sub_sections"][idx]["tables"] = []
        subsection_id = str(uuid7())
        card["sub_sections"][idx]["id"] = subsection_id
        subsection_tables = extract_markdown_tables(sub_section["content"])
        if subsection_tables:
            logger.info(
                f"Found {len(subsection_tables)} tables in subsection {sub_section['name']}"
            )
            for table in subsection_tables:
                table_title, updated_table = extract_title_and_description_for_table(
                    card["sub_sections"][idx]["content"], table
                )
                table_id = str(uuid7())
                logger.debug(f"Generated table ID: {table_id}")
                card["sub_sections"][idx]["content"] = replace_table_in_content(
                    card["sub_sections"][idx]["content"], table, updated_table
                )
                table_and_table_id_map[table_id] = table
                logger.info(
                    f"Generating visualization for table in subsection {sub_section['name']}"
                )
                viz_str = generate_visualization(
                    table, user_name, chat_id, report_type=report_type, user_id=user_id
                )
                if viz_str:
                    # is_mermaid = isinstance(viz_str, str) and viz_str.startswith("mermaid::")
                    is_html = isinstance(viz_str, str) and not viz_str.endswith(".png")
                    viz_type = "table_graphs" if is_html else "mermaid"
                    logger.info(
                        f"Successfully generated visualization for table {table_id} (type={viz_type})"
                    )
                    card["sub_sections"][idx]["tables"].append(
                        {
                            "visualization": viz_str,
                            "table_id": table_id,
                            "table_title": table_title,
                            "visualization_type": viz_type,
                        }
                    )
                else:
                    logger.warning(f"No visualization generated for table {table_id}")
                    card["sub_sections"][idx]["tables"].append(
                        {
                            "visualization": "",
                            "table_id": table_id,
                            "table_title": "",
                            "visualization_type": "",
                        }
                    )
        else:
            logger.info(f"No tables found in subsection {sub_section['name']}")
            card["sub_sections"][idx]["tables"] = [
                {"visualization": "", "table_id": "", "table_title": "", "visualization_type": ""}
            ]
    logger.info("Completed adding visualizations to card")
    return card, table_and_table_id_map


# def generate_new_viz_card(updated_card):
#     logger.info("Creating new visualization card")
#     vizualization = []

#     try:
#         logger.info("Processing section tables")
#         for idx, section_table in enumerate(updated_card['section_tables']):
#             try:
#                 if section_table['visualization'] and section_table['visualization'] != "":
#                     logger.debug(f"Adding visualization for section table {idx}")
#                     vizualization.append({
#                         "b_64_vizualization": section_table['visualization'],
#                         "card_id": updated_card['card_id'],
#                         "table_id": section_table['table_id'],
#                         "table_location": section_table['table_location']
#                     })
#             except KeyError as e:
#                 logger.error(f"Missing key in section table {idx}: {str(e)}")
#                 continue

#         logger.info("Processing subsection tables")
#         for subsection_idx, subsection in enumerate(updated_card['sub_sections']):
#             try:
#                 for table_idx, subsection_table in enumerate(subsection['sub_section_tables']):
#                     try:
#                         if subsection_table['visualization'] and subsection_table['visualization'] != "":
#                             logger.debug(f"Adding visualization for subsection {subsection_idx}, table {table_idx}")
#                             vizualization.append({
#                                 "b_64_vizualization": subsection_table['visualization'],
#                                 "card_id": updated_card['card_id'],
#                                 "table_id": subsection_table['table_id'],
#                                 "table_location": subsection_table['table_location']
#                             })
#                     except KeyError as e:
#                         logger.error(f"Missing key in subsection {subsection_idx} table {table_idx}: {str(e)}")
#                         continue
#             except KeyError as e:
#                 logger.error(f"Missing key in subsection {subsection_idx}: {str(e)}")
#                 continue

#         if len(vizualization) == 0:
#             logger.info("No visualizations found in card")
#             return None

#         viz_card = {"vizualization": vizualization}
#         logger.info(f"Created visualization card with {len(vizualization)} visualizations")
#         return viz_card

#     except Exception as e:
#         logger.error(f"Error creating visualization card: {str(e)}")
#         raise


def modify_card(card):
    try:
        new_card = {}
        if "title" in card:
            new_card["section"] = [{"name": "title", "content": card["title"]}]
            new_card["sub_sections"] = [{"name": "", "content": ""}]
            new_card["citations"] = card["citations"] if "citations" in card else {}
            new_card["summary"] = card["summary"] if "summary" in card else ""
            return new_card

        if "subtitle" in card:
            new_card["section"] = [{"name": "subtitle", "content": card["subtitle"]}]
            new_card["sub_sections"] = [{"name": "", "content": ""}]
            new_card["citations"] = card["citations"] if "citations" in card else {}
            new_card["summary"] = card["summary"] if "summary" in card else ""
            return new_card

        if "table_of_contents" in card:
            new_card["section"] = [
                {"name": "table_of_contents", "content": card["table_of_contents"]}
            ]
            new_card["sub_sections"] = [{"name": "", "content": ""}]
            new_card["citations"] = card["citations"] if "citations" in card else {}
            new_card["summary"] = card["summary"] if "summary" in card else ""
            return new_card

        if "executive_summary" in card:
            new_card["section"] = [
                {"name": "executive_summary", "content": card["executive_summary"]}
            ]
            new_card["sub_sections"] = [{"name": "", "content": ""}]
            new_card["citations"] = card["citations"] if "citations" in card else {}
            new_card["summary"] = card["summary"] if "summary" in card else ""
            return new_card

        if "section" in card:
            new_card["section"] = [{"name": card["section"], "content": card.get("content", "")}]

        if "sub_sections" in card:
            new_card["sub_sections"] = card["sub_sections"]

        if "citations" in card:
            new_card["citations"] = card["citations"] if "citations" in card else {}

        if "summary" in card:
            new_card["summary"] = card["summary"] if "summary" in card else ""

        # Analyst notes hang off the card / sub-section dicts rather than the
        # content, so they have to be carried across this rebuild explicitly.
        # The card's own notes are also mirrored onto section[0], where its prose
        # now lives, so every rendered block carries the notes for its own text.
        new_card[REASONING_KEY] = card.get(REASONING_KEY) or []
        for section in new_card.get("section") or []:
            if isinstance(section, dict):
                section[REASONING_KEY] = list(new_card[REASONING_KEY])
        for sub_section in new_card.get("sub_sections") or []:
            if isinstance(sub_section, dict):
                sub_section.setdefault(REASONING_KEY, [])

        return new_card

    except Exception as e:
        logger.error(f"Error converting card format: {e!s}")
        return {}


def modify_report_layout(report_layout):
    try:
        modified_report_layout = []
        for item in report_layout:
            new_card = {}

            if "title" in item:
                new_card["section"] = [
                    {
                        "name": "title",
                        "content": item["title"],
                        "tables": [{"visualization": "", "table_id": "", "table_title": ""}],
                    }
                ]
                new_card["sub_sections"] = [
                    {
                        "name": "",
                        "content": "",
                        "tables": [{"visualization": "", "table_id": "", "table_title": ""}],
                    }
                ]
                new_card["citations"] = {}
                new_card["summary"] = ""
                modified_report_layout.append(new_card)
                continue

            if "subtitle" in item:
                new_card["section"] = [
                    {
                        "name": "subtitle",
                        "content": item["subtitle"],
                        "tables": [{"visualization": "", "table_id": "", "table_title": ""}],
                    }
                ]
                new_card["sub_sections"] = [
                    {
                        "name": "",
                        "content": "",
                        "tables": [{"visualization": "", "table_id": "", "table_title": ""}],
                    }
                ]
                new_card["citations"] = {}
                new_card["summary"] = ""
                modified_report_layout.append(new_card)
                continue

            if "table_of_contents" in item:
                new_card["section"] = [
                    {
                        "name": "table_of_contents",
                        "content": item["table_of_contents"],
                        "tables": [{"visualization": "", "table_id": "", "table_title": ""}],
                    }
                ]
                new_card["sub_sections"] = [
                    {
                        "name": "",
                        "content": "",
                        "tables": [{"visualization": "", "table_id": "", "table_title": ""}],
                    }
                ]
                new_card["citations"] = {}
                new_card["summary"] = ""
                modified_report_layout.append(new_card)
                continue

            if "executive_summary" in item:
                new_card["section"] = [
                    {
                        "name": "executive_summary",
                        "content": item["executive_summary"],
                        "tables": [{"visualization": "", "table_id": "", "table_title": ""}],
                    }
                ]
                new_card["sub_sections"] = [
                    {
                        "name": "",
                        "content": "",
                        "tables": [{"visualization": "", "table_id": "", "table_title": ""}],
                    }
                ]
                new_card["citations"] = {}
                new_card["summary"] = ""
                modified_report_layout.append(new_card)
                continue

            if "section" in item:
                new_card["section"] = [
                    {
                        "name": item["section"],
                        "content": item.get("content", ""),
                        "tables": [{"visualization": "", "table_id": "", "table_title": ""}],
                    }
                ]

                if "sub_sections" in item:
                    formatted_subsections = []
                    for sub in item["sub_sections"]:
                        if isinstance(sub, str):
                            formatted_subsections.append(
                                {
                                    "name": sub,
                                    "content": "",
                                    "tables": [
                                        {"visualization": "", "table_id": "", "table_title": ""}
                                    ],
                                }
                            )
                        elif isinstance(sub, dict):
                            formatted_subsections.append(
                                {
                                    "name": sub["name"],
                                    "content": sub["content"],
                                    "tables": [
                                        {"visualization": "", "table_id": "", "table_title": ""}
                                    ],
                                }
                            )
                    new_card["sub_sections"] = formatted_subsections
                else:
                    new_card["sub_sections"] = [
                        {
                            "name": "",
                            "content": "",
                            "tables": [{"visualization": "", "table_id": "", "table_title": ""}],
                        }
                    ]

                new_card["citations"] = item.get("citations", {})
                new_card["summary"] = item.get("summary", "")

                modified_report_layout.append(new_card)

        return modified_report_layout

    except Exception as e:
        logger.error(f"Error modifying report layout: {e!s}")
        return []


class RefineReportLayout(BaseModel):
    report_layout: str = Field(
        ...,
        description="The refined and updated report layout in the exact same format as the input, with all sections enhanced using current internet-sourced information while preserving the original structure.",
    )


def refine_report_layout(
    report_layout, max_retries=3, retry_delay=5, chat_id: str = None, user_id: str = None
):
    try:
        # Try OpenAI first
        current_date = datetime.datetime.now().strftime("%B %d, %Y")
        prompt = REFINE_REPORT_LAYOUT_PROMPT.format(report_layout=report_layout)

        refined_prompt = prompt.strip()
        response = SYNC_OPENAI_CLIENT.responses.parse(
            model=REFINE_REPORT_LAYOUT_MODEL,
            input=[
                {
                    "role": "system",
                    "content": f"You are a precise researcher and technical writer. Use web search always(VERY VERY VERY IMPORTANT) for facts and statistics and retrieve the latest information as per the {current_date}",
                },
                {"role": "user", "content": refined_prompt},
            ],
            tools=[{"type": "web_search"}],
            tool_choice={"type": "web_search"},
            text_format=RefineReportLayout,
            temperature=0.1,
        )
        save_raw_llm_response(
            response,
            REFINE_REPORT_LAYOUT_MODEL,
            "Refining the report structure",
            chat_id,
            user_id=user_id,
        )
        output = response.model_dump()["output"][-1]

        json_output = output["content"][0]["parsed"]
        logger.info("successfully refined report layout with OpenAI")
        return json_output["report_layout"]
    except Exception as e:
        logger.error(f"Error with OpenAI API: {e!s}")

        # DISABLED: Perplexity fallback, superseded by the Gemini fallback below
        # (kept for reference/rollback).
        # payload = {
        #     "model": "sonar-pro",
        #     "messages": [{"role": "user", "content": prompt.strip()}],
        #     "response_format": {
        #         "type": "json_schema",
        #         "json_schema": {"schema": REFINE_REPORT_LAYOUT_SCHEMA}
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

        # Fallback: Gemini API. Uses the Interactions API (client.interactions.create),
        # which lets Gemini 3-series models combine google_search grounding with
        # structured JSON output in a SINGLE call (unlike the legacy generateContent
        # API used by client.models.generate_content, which can't mix tool use with
        # response_schema).
        retry_count = 0
        while retry_count < max_retries:
            try:
                logger.info(f"Falling back to Gemini API (attempt {retry_count + 1}/{max_retries})")
                prompt = REFINE_REPORT_LAYOUT_PROMPT.format(report_layout=report_layout).strip()

                gemini_client = genai.Client(api_key=GEMINI_API_KEY)
                interaction = gemini_client.interactions.create(
                    model=GEMINI_ES_MODEL_ID,
                    input=prompt,
                    tools=[{"type": "google_search"}],
                    response_format={
                        "type": "text",
                        "mime_type": "application/json",
                        "schema": REFINE_REPORT_LAYOUT_SCHEMA,
                    },
                    generation_config={
                        "temperature": 0.1,
                        "max_output_tokens": 8000,
                        "thinking_config": {"thinking_budget": 0},
                    },
                )
                save_raw_llm_response(
                    interaction,
                    GEMINI_ES_MODEL_ID,
                    "Refining the report structure (backup)",
                    chat_id,
                    user_id=user_id,
                )
                result = json.loads(strip_json_code_fence(interaction.output_text))
                logger.info(
                    "successfully refined report layout with Gemini (single Interactions API call)"
                )
                return result["report_layout"]
            except Exception as e:
                retry_count += 1
                logger.warning(f"Gemini API attempt {retry_count}/{max_retries} failed: {e!s}")
                logger.info(f"Error type: {type(e).__name__}")
                if retry_count < max_retries:
                    logger.info(f"Retrying in {retry_delay} seconds...")
                    time.sleep(retry_delay)
                else:
                    logger.critical(f"All Gemini API retry attempts failed: {e!s}")
                    return report_layout


def remove_citations_from_RL(report_layout: str):
    """
    Remove Markdown-style link citations in parentheses and numeric bracket citations.

    Examples removed:
      - ( [mckinsey.com](https://...) )
      - A whole line that is just: ([mckinsey.com](https://...))
      - [1], [1,2], [1-3], [1–3], Arc[2]
    """
    try:
        # Standalone line that is only a markdown link-in-parens
        text = re.sub(r"(?m)^\s*\(\[[^\]]+\]\([^)]+\)\)\s*$\n?", "", report_layout)

        # Inline markdown link-in-parens
        text = re.sub(r"\s*\(\[[^\]]+\]\([^)]+\)\)", "", text)

        # Numeric citations like [1], [1,2], [1-3], [1–3], Arc[2]
        # Updated to catch citations attached to words
        text = re.sub(r"\[\d+(?:\s*[,\u2013-]\s*\d+)*\](?=[\s\.\,\;\:\)\]\}–-]|$)", "", text)

        # Optional tidy: collapse extra spaces and excessive blank lines
        text = re.sub(r"[ \t]{2,}", " ", text)  # multiple spaces -> single
        text = re.sub(r"[ \t]+\n", "\n", text)  # trim trailing spaces
        text = re.sub(r"\n{3,}", "\n\n", text)  # max two consecutive newlines

        return text

    except Exception as e:
        logger.error(f"Error removing citations from RL: {e!s}")
        return report_layout


def remove_citations_from_DRL(DRL):
    """
    Remove Markdown-style link citations in parentheses and numeric bracket citations.

    Examples removed:
      - ( [mckinsey.com](https://...) )
      - A whole line that is just: ([mckinsey.com](https://...))
      - [1], [1,2], [1-3], [1–3], Arc[2]
    """
    logger.info("Removing citations from DRL")
    try:
        for idx, item in enumerate(DRL):
            if "section" in item:
                item["section"] = remove_citations_from_RL(item["section"])
            if "sub_sections" in item:
                for sub_idx, sub_section in enumerate(item["sub_sections"]):
                    if "name" in sub_section:
                        sub_section["name"] = remove_citations_from_RL(sub_section["name"])
                    if "description" in sub_section:
                        sub_section["description"] = remove_citations_from_RL(
                            sub_section["description"]
                        )
        logger.info("Removed citations from DRL")
        return DRL
    except Exception as e:
        logger.error(f"Error removing citations from DRL: {e!s}")
        return DRL


def standardize_url_citations(text, url_map):
    """
    Standardizes URL citations in text to a consistent format [number](url).

    Args:
        text (str): Input text containing URLs in various formats
        url_map (dict): Dictionary mapping URLs to citation numbers

    Returns:
        tuple: (updated_text, updated_url_map)
    """
    import re

    try:
        logger.info("Starting URL citation standardization")

        # Make a copy of the url_map to avoid modifying the original
        updated_url_map = url_map.copy()
        updated_text = repair_malformed_citation_markdown(text)

        # Get the current maximum number in the URL map
        max_num = max(updated_url_map.values()) if updated_url_map else 0

        # Track all URLs found and their assigned numbers
        url_to_number = {}

        # First, handle already formatted citations [number](url) and preserve them
        formatted_pattern = r"\[(\d+)\]\((https?://[^\)]+)\)"
        formatted_matches = list(re.finditer(formatted_pattern, updated_text))

        for match in formatted_matches:
            url = match.group(2)
            clean_url_val = clean_url(url)

            if clean_url_val in updated_url_map:
                # Use existing number from map
                url_to_number[clean_url_val] = updated_url_map[clean_url_val]
            else:
                # This URL doesn't exist in map, add it with current number
                number = int(match.group(1))
                url_to_number[clean_url_val] = number
                updated_url_map[clean_url_val] = number
                max_num = max(max_num, number)

        # Replace formatted citations with clean URLs (only if URL changed)
        def formatted_replacer(match):
            url = match.group(2)
            clean_url_val = clean_url(url)
            number = url_to_number[clean_url_val]
            if clean_url_val != url:  # Only replace if URL was cleaned
                return f"[{number}]({clean_url_val})"
            else:
                return match.group(0)  # Return original if no cleaning needed

        updated_text = re.sub(formatted_pattern, formatted_replacer, updated_text)

        # Then handle URLs in parentheses (url) - but avoid ones already in [number](url) format
        parentheses_pattern = r"(?<!\[\d+\])\((https?://[^\)\s]+)\)"
        parentheses_matches = list(re.finditer(parentheses_pattern, updated_text))

        for match in parentheses_matches:
            url = match.group(1)
            clean_url_val = clean_url(url)

            if clean_url_val not in url_to_number:
                if clean_url_val in updated_url_map:
                    url_to_number[clean_url_val] = updated_url_map[clean_url_val]
                else:
                    max_num += 1
                    url_to_number[clean_url_val] = max_num
                    updated_url_map[clean_url_val] = max_num

        def parentheses_replacer(match):
            url = match.group(1)
            clean_url_val = clean_url(url)
            number = url_to_number[clean_url_val]
            return f"[{number}]({clean_url_val})"

        updated_text = re.sub(parentheses_pattern, parentheses_replacer, updated_text)

        # Finally handle bare URLs - avoid ones already processed
        bare_pattern = r"(?<!\[)\b(?<!\[)\(?(https?://[^\s\)\]]+)(?!\])(?!\))\]?"
        # More specific pattern that won't match already processed URLs
        bare_pattern = r"(?<![\[\(])\b(https?://[^\s\)\]\[]+)(?![\]\)])"
        bare_matches = list(re.finditer(bare_pattern, updated_text))

        for match in bare_matches:
            url = match.group(1)
            clean_url_val = clean_url(url)

            if clean_url_val not in url_to_number:
                if clean_url_val in updated_url_map:
                    url_to_number[clean_url_val] = updated_url_map[clean_url_val]
                else:
                    max_num += 1
                    url_to_number[clean_url_val] = max_num
                    updated_url_map[clean_url_val] = max_num

        def bare_replacer(match):
            url = match.group(1)
            clean_url_val = clean_url(url)
            number = url_to_number[clean_url_val]
            return f"[{number}]({clean_url_val})"

        updated_text = re.sub(bare_pattern, bare_replacer, updated_text)

        logger.info(
            f"URL citation standardization completed. Processed {len(url_to_number)} unique URLs"
        )
        return updated_text, updated_url_map

    except Exception as e:
        logger.error(f"Error in standardize_url_citations: {e!s}")
        return text, url_map


def clean_url(url):
    try:
        parsed = urlparse(url)
        query_params = parse_qs(parsed.query)

        query_params = {k: v for k, v in query_params.items() if not k.startswith("utm_")}

        clean_query = urlencode(query_params, doseq=True)
        cleaned_url = urlunparse(parsed._replace(query=clean_query))
        return cleaned_url
    except Exception as e:
        logger.error(f"Error cleaning URL: {e!s}")
        # Handle both ? and & variations
        url = url.replace("?utm_source=openai", "")
        url = url.replace("&utm_source=openai", "")
        return url


def clean_executive_summary(executive_summary):
    """remove all the citations from the executive summary
    Example removed:
    # - [1]
    # - [1,2]
    # - [1-3]
    # - [1–3]
    # - Arc[2]
    # - [1][3][4][5][6][7][8][9][10]"""
    logger.info("Cleaning executive summary")
    try:
        cleaned_executive_summary = re.sub(r"\[(\d+)\]", "", executive_summary)
        logger.info("Successfully cleaned executive summary")
        return cleaned_executive_summary
    except Exception as e:
        logger.error(f"Error cleaning executive summary: {e!s}")
        return executive_summary


_LAYOUT_META_NAMES = {"title", "subtitle", "table_of_contents", "executive_summary"}


def _card_section_block(card: dict) -> dict:
    section = card.get("section") or []
    if isinstance(section, list) and section:
        return section[0] if isinstance(section[0], dict) else {}
    return {}


def _updated_layout_to_markdown(layout) -> str:
    """Render a structured updated layout back into the markdown shape the model proposes."""
    # Legacy path: LLM structured output (UpdatedProposedReportLayout)
    if hasattr(layout, "title") and hasattr(layout, "sections"):
        lines = [f"# {layout.title.strip()}"]
        for section in layout.sections:
            name = (section.section or "").strip()
            if not name:
                continue
            lines.append("")
            lines.append(f"## {name}")
            for sub in section.sub_sections or []:
                sub_name = (sub or "").strip()
                if sub_name:
                    lines.append(f"- {sub_name}")
        return "\n".join(lines)

    # Parsed card list from modify_report_layout
    cards = layout if isinstance(layout, list) else []
    title = "Report"
    section_lines: list[str] = []

    for card in cards:
        if not isinstance(card, dict):
            continue
        block = _card_section_block(card)
        name = (block.get("name") or "").strip()
        content = (block.get("content") or "").strip()
        if not name:
            continue

        lname = name.lower()
        if lname == "title":
            if content:
                title = content
            continue
        if lname in _LAYOUT_META_NAMES:
            continue

        section_lines.append("")
        section_lines.append(f"## {name}")
        for sub in card.get("sub_sections") or []:
            if isinstance(sub, str):
                sub_name = sub.strip()
            elif isinstance(sub, dict):
                sub_name = (sub.get("name") or "").strip()
            else:
                sub_name = ""
            if sub_name:
                section_lines.append(f"- {sub_name}")

    return "\n".join([f"# {title}"] + section_lines)
