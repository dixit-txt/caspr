"""
Fix card content: correct minor spelling mistakes,
ensure all cited URLs follow the [site name](url) format,
and fix table formatting (proper row separation, consistent
column count with N/A for empty cells, correct spacing).
"""

import copy
import json
import os
import time
from typing import Any

from google import genai
from openai import OpenAI
from pydantic import BaseModel, Field

from app.core.constants import CARD_FIX_MODEL, GEMINI_API_KEY, GEMINI_ES_MODEL_ID
from app.core.logging import setup_logging
from app.observability.llm_response_logger import save_raw_llm_response, strip_json_code_fence

logger = setup_logging(__name__)


# ─── Pydantic schema for structured output ────────────────────────────────────


class FixedSubSection(BaseModel):
    name: str = Field(description="The sub-section name, unchanged.")
    content: str = Field(description="The fixed sub-section content.")


class FixedCard(BaseModel):
    """Full card with all content fields fixed."""

    section: str = Field(description="The section title, unchanged.")
    content: str = Field(description="The fixed top-level section content.")
    sub_sections: list[FixedSubSection] = Field(
        description="List of sub-sections with fixed content."
    )


# ─── Gemini JSON schema (for response_schema) ────────────────────────────────

_GEMINI_FIX_CARD_SCHEMA = {
    "type": "object",
    "properties": {
        "section": {"type": "string", "description": "The section title, unchanged."},
        "content": {"type": "string", "description": "The fixed top-level section content."},
        "sub_sections": {
            "type": "array",
            "description": "List of sub-sections with fixed content.",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Sub-section name, unchanged."},
                    "content": {"type": "string", "description": "The fixed sub-section content."},
                },
                "required": ["name", "content"],
            },
        },
    },
    "required": ["section", "content", "sub_sections"],
}


# ─── Prompt ───────────────────────────────────────────────────────────────────

CARD_FIX_PROMPT = """You are a precise content editor. You will receive a full report card as JSON.
The card has a top-level "content" field and a "sub_sections" array where each entry also has a "content" field.

Your job is to fix ALL content fields and return the ENTIRE card JSON with the same structure.

Apply ONLY these corrections to every content field:

1. **Spelling mistakes** – Fix obvious spelling/typo errors in the prose text.

2. **Citation URL format** – Every cited URL must follow this exact format:
   [site name](url)
   
   IMPORTANT: Only web URLs (http:// or https://) are allowed in citations.
   - **Preserve numbered citations:** Links already in `[N](url)` format (where N is a digit and url is a real http(s) URL from web search) MUST be kept exactly as-is. Do NOT remove valid citation links the model included.
   - Remove any `file://` citations entirely (e.g. `file://turn0file0`, `file://turn0file1`, etc.). Delete the entire citation including the link text — do not leave behind broken fragments.
   - Remove any placeholder or synthetic citation URLs entirely, including example.com, example.org, example.net, localhost, test URLs, mock URLs, and illustrative documentation links.
   
   Examples of WRONG formats and their corrections:
   - `[site name] (url)` → `[site name](url)`  (remove space before parenthesis)
   - `site name (url)` → `[site name](url)`  (add square brackets)
   - `[site name][url]` → `[site name](url)`  (use parentheses, not brackets, for URL)
   - `[site name](url` → `[site name](url)`  (close the parenthesis)
   - `(site name)[url]` → `[site name](url)`  (swap bracket types)
   - `[url](site name)` → `[site name](url)`  (swap link text and URL if clearly reversed)
   - Bare real source URLs that appear as citations should become markdown links using the URL's site name as link text.
   - `[any text](file://turn0file0)` → remove entirely
   - `[any text](file://...)` → remove entirely
   - Markdown links pointing to placeholder/example domains → remove entirely
   - `[5](https://example.com/article)` → keep unchanged (valid numbered citation)
   
   TRUNCATED / BROKEN URLs – These are critical to fix:
   - If a URL is clearly truncated or cut off mid-way (e.g. the URL ends abruptly without a closing parenthesis, or a query parameter is incomplete), repair it:
     - `[brecorder.com](https://www.brecorder.com/news/40398840?utm_source=o` → `[brecorder.com](https://www.brecorder.com/news/40398840)` (remove the truncated query parameter and close the parenthesis)
     - Remove truncated query parameters from real source URLs and close the parenthesis.
     - Remove all incomplete query parameters from real source URLs, then close the parenthesis.
   - In general: if a URL has an incomplete query parameter (the value after `=` is missing or cut off), remove the entire query string from the `?` or the truncated `&` parameter onward, then ensure the parenthesis is properly closed.
   - If a citation link `[text](url` has no closing `)` at all, close it after cleaning the URL.
   - Remove any `?utm_source=openai` or `&utm_source=openai` tracking parameters from URLs entirely, even when they are complete.
   - If a markdown link like `[text](url)` is itself wrapped in an extra set of parentheses like `([text](url))`, remove the outer wrapping parentheses so it becomes `[text](url)`.

3. **Table formatting** – Fix only the table structure:
   - Every table MUST have a descriptive title line directly above it using the exact prefix "Title: " (capital T, colon, space). If missing, infer a short title under 15 words from the table and nearby context.
   - Use one row per line. Every row must start and end with `|`.
   - Keep the same data rows and values. Do not add or remove data rows or columns.
   - Make column counts consistent across the header, separator, and data rows. Fill empty cells with `N/A`.
   - Ensure the first table row is a meaningful header row and the second row is a separator row. If the header is missing or malformed, infer descriptive column names from the table data.
   - Keep a blank line between prose, the title, the table, and the source line. Sources belong below the table, not inside cells.

   Tables must follow this spacing layout:

      ... prose content ...
      <blank line>
      Title: <table title>
      <blank line>
      | Header1 | Header2 | Header3 |
      |---------|---------|---------|
      | data    | data    | data    |
      <blank line>
      Source: <source text>
      <blank line>
      ... prose content continues ...

4. **Source citation layout** – If multiple source citations appear as a bulleted list or on separate lines (e.g. each on its own line prefixed with `- `), merge them into a single inline line separated by spaces.
   - WRONG (each source on a separate line):
     - [title](url)
     - [title](url)
   - CORRECT (all sources inline on one line):
     [title](url) [title](url)
   - This applies everywhere sources appear: after paragraphs, after tables, or anywhere in the content.

STRICT RULES – do NOT violate any of these:
- Do NOT rephrase, rewrite, summarize, or restructure any sentence.
- Do NOT add or remove any information (except N/A for empty table cells, and except removing broken/truncated URL parameters as described above).
- Do NOT change any markdown heading levels, bullet points, or list formatting.
- Do NOT change any numbers, dates, or data values.
- Do NOT change "section" or "name" fields – return them exactly as-is.
- ONLY fix spelling errors, citation URL formatting (including truncated/broken URLs), table formatting, and source citation layout (merge multi-line sources into single inline lines) inside content fields.
- If there are any fenced code blocks (```), ASCII art charts, or text-based bar charts (lines with characters like █, ▓, ■, #, = used as visual bars), remove them entirely. If the code block contains data that can be represented as a markdown table, convert it to a proper markdown table. Otherwise, just remove it.
- Return the COMPLETE card with ALL fields preserved.

Card JSON:
{card_json}"""


# ─── OpenAI tool-call schema (mirrors FixedCard pydantic model) ──────────────

_FIX_CARD_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "fix_card",
        "description": "Returns the full card JSON with all content fields fixed.",
        "parameters": {
            "type": "object",
            "properties": {
                "section": {"type": "string", "description": "The section title, unchanged."},
                "content": {
                    "type": "string",
                    "description": "The fixed top-level section content.",
                },
                "sub_sections": {
                    "type": "array",
                    "description": "List of sub-sections with fixed content.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {
                                "type": "string",
                                "description": "Sub-section name, unchanged.",
                            },
                            "content": {
                                "type": "string",
                                "description": "The fixed sub-section content.",
                            },
                        },
                        "required": ["name", "content"],
                    },
                },
            },
            "required": ["section", "content", "sub_sections"],
        },
    },
}


CONTENT_FIX_PROMPT = """You are a precise content editor. You will receive a section of report content as plain text (Markdown).

Apply ONLY these corrections and return the fixed content as a plain string:

1. **Spelling mistakes** – Fix obvious spelling/typo errors in the prose text.

2. **Citation URL format** – Every cited URL must follow this exact format:
   [site name](url)
   
   IMPORTANT: Only web URLs (http:// or https://) are allowed in citations.
   - **Preserve numbered citations:** Links already in `[N](url)` format (where N is a digit and url is a real http(s) URL from web search) MUST be kept exactly as-is. Do NOT remove valid citation links the model included.
   - Remove any `file://` citations entirely (e.g. `file://turn0file0`, `file://turn0file1`, etc.). Delete the entire citation including the link text — do not leave behind broken fragments.
   - Remove any placeholder or synthetic citation URLs entirely, including example.com, example.org, example.net, localhost, test URLs, mock URLs, and illustrative documentation links.
   
   Examples of WRONG formats and their corrections:
   - `[site name] (url)` → `[site name](url)`  (remove space before parenthesis)
   - `site name (url)` → `[site name](url)`  (add square brackets)
   - `[site name][url]` → `[site name](url)`  (use parentheses, not brackets, for URL)
   - `[site name](url` → `[site name](url)`  (close the parenthesis)
   - `(site name)[url]` → `[site name](url)`  (swap bracket types)
   - `[url](site name)` → `[site name](url)`  (swap link text and URL if clearly reversed)
   - Bare real source URLs that appear as citations should become markdown links using the URL's site name as link text.
   - `[any text](file://turn0file0)` → remove entirely
   - `[any text](file://...)` → remove entirely
   - Markdown links pointing to placeholder/example domains → remove entirely
   - `[5](https://example.com/article)` → keep unchanged (valid numbered citation)
   
   TRUNCATED / BROKEN URLs – These are critical to fix:
   - If a URL is clearly truncated or cut off mid-way, repair it:
     - Remove truncated query parameters (incomplete value after `=`) and close the parenthesis.
     - Remove any `?utm_source=openai` or `&utm_source=openai` tracking parameters entirely, even when complete.
   - If a markdown link like `[text](url)` is wrapped in extra parentheses like `([text](url))`, remove the outer wrapping parentheses.

3. **Table formatting** – Fix only the table structure:
   - Every table MUST have a descriptive title line directly above it using the exact prefix "Title: " (capital T, colon, space). If missing, infer a short title under 15 words from the table and nearby context.
   - Use one row per line. Every row must start and end with `|`.
   - Keep the same data rows and values. Do not add or remove data rows or columns.
   - Make column counts consistent across the header, separator, and data rows. Fill empty cells with `N/A`.
   - Ensure the first table row is a meaningful header row and the second row is a separator row. If the header is missing or malformed, infer descriptive column names from the table data.
   - Keep a blank line between prose, the title, the table, and the source line. Sources belong below the table, not inside cells.

   Tables must follow this spacing layout:

      ... prose content ...
      <blank line>
      Title: <table title>
      <blank line>
      | Header1 | Header2 | Header3 |
      |---------|---------|---------|
      | data    | data    | data    |
      <blank line>
      Source: <source text>
      <blank line>
      ... prose content continues ...

4. **Source citation layout** – If multiple source citations appear as a bulleted list or on separate lines, merge them into a single inline line separated by spaces.

STRICT RULES:
- Do NOT rephrase, rewrite, summarize, or restructure any sentence.
- Do NOT add or remove any information (except N/A for empty table cells, and removing broken URL parameters).
- Do NOT change markdown heading levels, bullet points, or list formatting.
- Do NOT change numbers, dates, or data values.
- ONLY fix spelling errors, citation URL formatting, table formatting, and source citation layout.
- Return ONLY the fixed content string. No JSON wrapping, no extra explanation.

Content:
{content}"""

_GEMINI_CONTENT_FIX_SCHEMA = {
    "type": "object",
    "properties": {"fixed_content": {"type": "string", "description": "The fixed content string."}},
    "required": ["fixed_content"],
}

_FIX_CONTENT_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "fix_content",
        "description": "Returns the fixed content string.",
        "parameters": {
            "type": "object",
            "properties": {
                "fixed_content": {"type": "string", "description": "The fixed content string."}
            },
            "required": ["fixed_content"],
        },
    },
}


def fix_content(content: str, chat_id: str = None, user_id: str = None) -> str:
    """
    Fix a single content string: correct minor spelling mistakes,
    ensure all cited URLs follow [site name](url) format, fix table
    formatting, and merge multi-line source citations into inline format.

    Uses OpenAI gpt-5.4 as primary, Gemini as fallback.

    Args:
        content: Raw markdown/text content string to fix.

    Returns:
        The fixed content string. Returns original on failure.
    """
    if not content or not content.strip():
        return content

    prompt = CONTENT_FIX_PROMPT.format(content=content)
    start_time = time.time()

    # ── Primary: OpenAI ──────────────────────────────────────────────────
    try:
        logger.info(f"[fix_content] Attempting fix with OpenAI {CARD_FIX_MODEL}...")
        client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

        response = client.chat.completions.create(
            model=CARD_FIX_MODEL,
            messages=[{"role": "user", "content": prompt}],
            tools=[_FIX_CONTENT_TOOL_SCHEMA],
            tool_choice={"type": "function", "function": {"name": "fix_content"}},
        )
        save_raw_llm_response(
            response, CARD_FIX_MODEL, "Fixing report section content", chat_id, user_id=user_id
        )

        tool_call = response.choices[0].message.tool_calls[0]
        result = json.loads(tool_call.function.arguments)
        fixed = result.get("fixed_content", content)

        elapsed = time.time() - start_time
        logger.info(f"[fix_content] Fixed with OpenAI in {elapsed:.2f}s")
        return fixed

    except Exception as e:
        logger.warning(
            f"[fix_content] OpenAI failed ({type(e).__name__}: {e}), falling back to Gemini"
        )

    # ── Fallback: Gemini ─────────────────────────────────────────────────
    try:
        logger.info("[fix_content] Attempting fix with Gemini...")
        gemini_client = genai.Client(api_key=GEMINI_API_KEY)

        interaction = gemini_client.interactions.create(
            model=GEMINI_ES_MODEL_ID,
            input=prompt,
            response_format={
                "type": "text",
                "mime_type": "application/json",
                "schema": _GEMINI_CONTENT_FIX_SCHEMA,
            },
            generation_config={
                "thinking_config": {"thinking_budget": 0},
            },
        )
        save_raw_llm_response(
            interaction,
            GEMINI_ES_MODEL_ID,
            "Fixing report section content (backup)",
            chat_id,
            user_id=user_id,
        )

        result = json.loads(strip_json_code_fence(interaction.output_text))
        fixed = result.get("fixed_content", content)

        elapsed = time.time() - start_time
        logger.info(f"[fix_content] Fixed with Gemini in {elapsed:.2f}s")
        return fixed

    except Exception as e:
        elapsed = time.time() - start_time
        logger.error(
            f"[fix_content] Both LLMs failed ({type(e).__name__}: {e}). "
            f"Returning original content. Total time: {elapsed:.2f}s"
        )
        return content


def fix_card(card: dict[str, Any], chat_id: str = None, user_id: str = None) -> dict[str, Any]:
    """
    Fix an entire card in a single LLM call:
      1. Serialize the card dict to JSON.
      2. Send to LLM and get back the full fixed card.
      3. Parse and return as a dict with the same structure.

    Uses OpenAI gpt-5.4 as primary, Gemini as fallback.

    Args:
        card: A card dictionary with the structure:
              {
                "section": "...",
                "content": "...",
                "sub_sections": [
                  {"name": "...", "content": "..."},
                  ...
                ]
              }

    Returns:
        A new card dict with the same structure but fixed content.
    """
    if not card:
        return card

    card = copy.deepcopy(card)
    section_name = card.get("section", "unknown")
    card_json = json.dumps(card, indent=2, ensure_ascii=False)
    prompt = CARD_FIX_PROMPT.format(card_json=card_json)
    start_time = time.time()

    # ── Primary: OpenAI gpt-5.4 ──────────────────────────────────────────
    try:
        logger.info(f'[Card "{section_name}"] Attempting fix with OpenAI {CARD_FIX_MODEL}...')
        client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

        response = client.chat.completions.create(
            model=CARD_FIX_MODEL,
            messages=[{"role": "user", "content": prompt}],
            tools=[_FIX_CARD_TOOL_SCHEMA],
            tool_choice={"type": "function", "function": {"name": "fix_card"}},
        )
        save_raw_llm_response(
            response, CARD_FIX_MODEL, "Fixing a report card", chat_id, user_id=user_id
        )

        tool_call = response.choices[0].message.tool_calls[0]
        fixed_card = json.loads(tool_call.function.arguments)

        elapsed = time.time() - start_time
        logger.info(f'[Card "{section_name}"] Fixed with OpenAI {CARD_FIX_MODEL} in {elapsed:.2f}s')
        return fixed_card

    except Exception as e:
        logger.warning(
            f'[Card "{section_name}"] OpenAI {CARD_FIX_MODEL} failed '
            f"({type(e).__name__}: {e}), falling back to Gemini"
        )

    # ── Fallback: Gemini with structured JSON output ─────────────────────
    try:
        logger.info(f'[Card "{section_name}"] Attempting fix with Gemini...')
        gemini_client = genai.Client(api_key=GEMINI_API_KEY)

        interaction = gemini_client.interactions.create(
            model=GEMINI_ES_MODEL_ID,
            input=prompt,
            response_format={
                "type": "text",
                "mime_type": "application/json",
                "schema": _GEMINI_FIX_CARD_SCHEMA,
            },
            generation_config={
                "thinking_config": {"thinking_budget": 0},
            },
        )
        save_raw_llm_response(
            interaction,
            GEMINI_ES_MODEL_ID,
            "Fixing a report card (backup)",
            chat_id,
            user_id=user_id,
        )

        fixed_card = json.loads(strip_json_code_fence(interaction.output_text))

        elapsed = time.time() - start_time
        logger.info(f'[Card "{section_name}"] Fixed with Gemini in {elapsed:.2f}s')
        return fixed_card

    except Exception as e:
        elapsed = time.time() - start_time
        logger.error(
            f'[Card "{section_name}"] Both LLMs failed '
            f"({type(e).__name__}: {e}). "
            f"Returning original card. Total time: {elapsed:.2f}s"
        )
        return card
