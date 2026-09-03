import json
import os
import re

import boto3
from botocore.config import Config
from google import genai
# from langchain_aws import ChatBedrock  # Bedrock disabled — using direct Anthropic API instead
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import SystemMessage, HumanMessage
from pydantic import BaseModel, Field

from app.core.constants import (
    # BEDROCK_ACCESS_KEY_ID,
    # BEDROCK_SECRET_ACCESS_KEY,
    # BEDROCK_REGION_NAME,
    ANTHROPIC_MODEL_ID,
    ANTHROPIC_API_KEY,
    SYNC_OPENAI_CLIENT,
    GEMINI_API_KEY,
    GEMINI_HTML_MODEL,
    OPENAI_VALIDATION_MODEL,
    GEMINI_VALIDATION_MODEL,
)
from app.core.logging import setup_logging
from app.observability.llm_response_logger import save_raw_llm_response, strip_json_code_fence

logger = setup_logging(__name__)

GEMINI_CLIENT = genai.Client(api_key=GEMINI_API_KEY)

# BEDROCK_CLIENT = boto3.client(
#     service_name="bedrock-runtime",
#     aws_access_key_id=BEDROCK_ACCESS_KEY_ID,
#     aws_secret_access_key=BEDROCK_SECRET_ACCESS_KEY,
#     region_name=BEDROCK_REGION_NAME,
#     config=Config(read_timeout=3600),
# )

# BEDROCK_FALLBACK_LLM = ChatBedrock(
#     client=BEDROCK_CLIENT,
#     model_id=ANTHROPIC_OPUS_4_MODEL_ID,
#     streaming=False,
#     beta_use_converse_api=True,
# )

ANTHROPIC_VIZ_LLM = ChatAnthropic(
    model=ANTHROPIC_MODEL_ID,
    timeout=None,
    max_retries=5,
    api_key=ANTHROPIC_API_KEY
)


class HTMLVisualizationRefine(BaseModel):
    """Structured output for refined HTML visualization generation from table data."""

    html_code: str = Field(
        description="The complete, valid, self-contained HTML code with inline CSS that visualizes the table data. No JavaScript, no external dependencies."
    )
    visualization_types: str = Field(
        description="Comma-separated list of visual formats used (e.g., 'KPI cards, horizontal bar chart, styled table')"
    )
    reasoning: str = Field(
        description="Brief explanation of why these visualization types were chosen for the given data"
    )


_GEMINI_HTML_REFINE_SCHEMA = {
    "type": "object",
    "properties": {
        "html_code": {
            "type": "string",
            "description": "The complete, valid, self-contained HTML code with inline CSS that visualizes the table data. No JavaScript, no external dependencies.",
        },
        "visualization_types": {
            "type": "string",
            "description": "Comma-separated list of visual formats used (e.g., 'KPI cards, horizontal bar chart, styled table')",
        },
        "reasoning": {
            "type": "string",
            "description": "Brief explanation of why these visualization types were chosen for the given data",
        },
    },
    "required": ["html_code", "visualization_types", "reasoning"],
}


HTML_GRAPH_REFINE_SYSTEM_PROMPT = """You are an expert at creating clean HTML+CSS charts and graphs, specializing in REFINING visualizations based on user feedback.

Your task: Convert the input table into a CHART or GRAPH using ONLY HTML and inline CSS, following the user's specific refinement instructions.

RULES:
- Output ONLY charts and graphs (bar charts, horizontal bar charts, progress bars, stacked bars, grouped bars, donut/pie approximations using CSS). NOTHING ELSE.
- NEVER create infographics, KPI cards, icon grids, step timelines, card layouts, badge lists, or any decorative non-chart visual.
- NEVER just re-create the table with styled borders — that adds no value.
- Keep it simple, clean, and data-focused. The chart should communicate the data clearly at a glance.
- Use pure HTML + inline CSS only. ABSOLUTELY NO JavaScript — no <script> tags, no inline event handlers (onclick, onload, etc.), no javascript: URLs. JS will NOT execute in PDF.
- No SVG, no external libraries, no CDN links, no external fonts or images.
- Must render in markdown files and PDF exports.
- Keep it compact — the chart should fit naturally below a table in a report.
- Do NOT include any title or heading for the chart. Just the chart itself.
- FOLLOW THE USER'S REFINEMENT INSTRUCTIONS PRECISELY. The user is refining an existing visualization — pay close attention to what they want changed.

CHART TYPES ALLOWED (use the most appropriate one for the data):
- Horizontal bar chart — best for comparing values across categories
- Vertical bar chart (using inline-block divs) — best for time series or sequential data
- Stacked bar chart — best for part-to-whole comparisons across categories
- Grouped bar chart — best for multi-metric comparisons
- Progress bars — best for percentages, completion rates, proportions
- Bullet chart — best for actual vs target comparisons

CHART SELECTION GUIDE:
- Numeric comparisons across categories → horizontal bar chart
- Percentages / proportions / rates → progress bars with percentage labels
- Rankings / sorted values → ordered horizontal bar chart (largest to smallest)
- Time-series or sequential numeric data → vertical bar chart
- Multiple metrics per category → grouped horizontal bar chart
- Part-to-whole relationships → stacked bar chart

WHAT TO NEVER DO:
- NEVER output styled cards, KPI tiles, icon badges, or infographic-style layouts
- NEVER output timelines, step indicators, or process flows
- NEVER output colored pill badges or status indicators as standalone visuals
- NEVER output a styled HTML <table> — that is NOT a chart
- NEVER add decorative elements that don't represent data
- If the data is purely textual with no numeric values, output a simple horizontal bar chart using text-length or count as the metric, OR return a minimal representation

CRITICAL — NO CITATIONS OR LINKS IN VISUALIZATIONS:
- NEVER include citations, footnotes, reference numbers (e.g., [1], [2], [3]), or source links in any part of the visualization.
- NEVER include hyperlinks (<a> tags), URLs, or any clickable references in the chart.
- NEVER add citation markers like [1], [2], superscript numbers, or footnote references in chart labels, axis labels, bar labels, legend text, or any text within the visualization.
- The visualization must contain ONLY clean data labels and values — no references, no sources, no links.
- If the input data contains citations or reference markers in text, STRIP THEM OUT before using the text as labels.
- This applies to ALL text in the chart: titles, axis labels, bar labels, legend entries, annotations, value labels, category names — EVERYTHING must be citation-free.

CRITICAL — NO SIDE-BY-SIDE COLUMNS FOR TEXT CONTENT:
- NEVER use side-by-side flex columns or inline-block columns to display text content. This causes text overflow and clipping in PDF.
- NEVER use "display: flex" with two "flex: 1" children to put text content side by side.
- The ONLY acceptable side-by-side layout is a short label (max 120px fixed width) next to a bar in a chart.

CRITICAL PAGE-BREAK & HEIGHT RULE (MUST FIT ON ONE PAGE):
- The ENTIRE chart must stay on ONE page. It must NEVER be split across pages or overflow off the bottom of a page.
- Wrap everything in a single container div with these styles: page-break-inside: avoid; break-inside: avoid;
- The TOTAL rendered height of the chart MUST NOT exceed 720px. A visualization taller than one page cannot avoid a page break, so keep it short and compact.
- If the data has many rows/items, keep each row compact (bar height 14-22px, 4-6px gaps between rows) OR show fewer rows / aggregate the data — do NOT build a tall, long chart that runs off the page.
- Prefer horizontal, compact layouts over tall vertical stacks. Everything must be visible within a single compact block; never assume the reader can scroll.

CRITICAL — NO OVERLAPPING ELEMENTS (STRICT — NOTHING MAY OVERLAP):
- Text must NEVER overlap other text. Text must NEVER overlap or be overlapped by any visual element (bar, dot, line, circle, block, gauge, or background shape). Visuals must NEVER overlap other visuals.
- Every label, value, and title must sit in its own clear space with at least 4px of padding/margin separating it from every neighbouring element.
- Value labels must sit EITHER fully inside a bar/segment that is large enough to contain them, OR fully outside it — never straddling a bar edge, and never on top of an adjacent bar or label.
- Do NOT use negative margins, overlapping absolute positions, or z-index tricks to stack elements on top of one another.
- If two pieces of content would collide, add spacing or move one to a new row/line. It is ALWAYS better to make the chart slightly plainer than to let anything overlap.

CRITICAL — NO HTML COMMENTS:
- Do NOT include any HTML comments (<!-- ... -->) in the output. Comments break markdown rendering when the HTML is embedded in markdown files.

CRITICAL — NO EMPTY LINES INSIDE HTML:
- Do NOT include blank/empty lines between HTML elements inside a container.
- When this HTML is embedded in markdown, blank lines inside HTML blocks cause the markdown parser to interpret them as paragraph boundaries, breaking the HTML structure.
- Keep all HTML tags on consecutive lines with NO blank lines between them.

CRITICAL — OVERFLOW PREVENTION:
- Every text container MUST have: overflow-wrap: break-word; word-wrap: break-word;
- The outermost container MUST have: max-width: 700px; box-sizing: border-box; overflow-x: hidden;
- NEVER put `overflow: hidden` on both axes and NEVER combine a fixed height / max-height with overflow:hidden around chart content — that silently clips bars, labels, or legends ("visual not fully generated"). Clip on the X axis only; let height grow with content.
- ALL child elements must have: box-sizing: border-box;
- NEVER use fixed widths on text elements that could exceed their parent container.
- NEVER use white-space: nowrap on any text that could be longer than 100px.
- Text must ALWAYS be allowed to wrap to the next line.
- NEVER use `transform: scale(...)` to shrink the chart — a CSS scale keeps the original layout box size, leaving a large empty gap after the chart / a blank page. Size the chart correctly at 1:1 and keep it compact instead.

CRITICAL — FONT RESTRICTIONS:
- You MUST ONLY use the following fonts that are available in the PDF template: 'Calibri', 'Baskerville', 'Inter', 'DejaVu Serif'.
- Use 'Calibri' as the default/primary font for body text, labels, and values.
- Use 'Baskerville' for any serif text if needed.
- Use 'Inter' for bold headings or emphasis if needed.
- Use 'DejaVu Serif' as a fallback serif font.
- NEVER use Arial, Helvetica, Verdana, Roboto, Open Sans, Segoe UI, Tahoma, Georgia, or any other font not listed above.
- NEVER use generic CSS font families alone (sans-serif, serif, monospace) without specifying one of the allowed fonts first.
- Always specify the font-family with an allowed font as: font-family: 'Calibri', sans-serif;
- If you don't know which font to pick, default to 'Calibri', sans-serif.

DESIGN PRINCIPLES FOR CLEAN, INFORMATIVE CHARTS:
- Professional, minimal, data-first design
- Every visual element must encode data — no decoration
- Clear axis labels on each bar/element (category name + value)
- Readable font sizes (12-14px for labels, 11px minimum for small annotations)
- STRICT COLOR PALETTE — use ONLY these Caspr brand colors:
  * #E8453C (caspr-red) — highlights, key insights, primary bars, emphasis
  * #0B0B09 (caspr-black) — primary typography, headings, axis labels
  * #1A1A18 (caspr-grey-dark) — secondary text, dark chart elements
  * #6B6B66 (caspr-grey-mid) — secondary/supporting data, labels, annotations
  * #F2F1EF (caspr-grey-light) — subtle backgrounds, card fills, containers
  * #D6D4CF (caspr-rule) — borders, dividers, grid lines, separators
  * #FFFFFF (caspr-white) — chart background
- NEVER use any color outside this palette (no blues, greens, purples, oranges, teals)
- Use caspr-red (#E8453C) for the primary/most important data series
- Use caspr-grey-dark (#1A1A18) and caspr-grey-mid (#6B6B66) for secondary data series
- Adequate spacing between bars (4-8px gaps)
- Value labels displayed at the end of each bar or inside if bar is large enough
- NO title, NO heading, NO legend unless absolutely necessary for multi-series charts
- Max width 700px, all content must fit within this width with no overflow
- All text must wrap naturally — never clip or overflow

OUTPUT: Return ONLY the HTML for the chart wrapped in a page-break-avoiding container. Nothing else.

YOU MUST ALWAYS PROVIDE ALL THREE REQUIRED FIELDS IN YOUR RESPONSE:
1. html_code — the full HTML string
2. visualization_types — comma-separated list of chart types used (e.g., 'horizontal bar chart', 'progress bars')
3. reasoning — brief explanation of why you chose this chart type for the given data

NEVER return an empty response. Even if the input data is minimal, always generate a valid chart."""


MAX_VALIDATION_RETRIES = 1

HTML_REFINE_VALIDATION_SYSTEM_PROMPT = """You are an expert HTML/CSS validator specializing in PDF rendering compatibility.

Your task: Inspect the provided HTML code and determine if it will render correctly when converted to PDF.

CHECK FOR THESE ISSUES (in priority order):

1. **CRITICAL — No JavaScript Allowed**: ANY <script> tags, inline event handlers (onclick, onload, onmouseover, etc.), or JavaScript expressions MUST be flagged as critical.

2. **CRITICAL — Page Break Safety**: The entire visualization MUST be wrapped in a container with `page-break-inside: avoid; break-inside: avoid;`.

3. **CRITICAL — No HTML Comments**: The HTML must NOT contain any HTML comments (<!-- ... -->).

4. **CRITICAL — No Empty Lines Inside HTML**: The HTML must NOT contain blank/empty lines between elements inside a container.

5. **CRITICAL — No Side-by-Side Text Columns**: The HTML must NOT use side-by-side flex columns for TEXT content.

6. **CRITICAL — Overflow Prevention**: All text containers must have overflow-wrap: break-word. The outer container must have max-width: 700px with box-sizing: border-box and `overflow-x: hidden` (X axis only). Flag as critical if the outer container uses `overflow: hidden` on BOTH axes, or combines a fixed height / max-height with `overflow: hidden` around chart content (clips bars/labels/legends vertically), or uses `transform: scale(...)` to size the chart (leaves phantom layout height / a blank page).

7. **PDF Rendering Breakers**: Flexbox/Grid issues, viewport-relative units (vw/vh), position:fixed/sticky, CSS animations/transitions, media queries.

8. **CRITICAL — No Overlapping Elements**: NOTHING may overlap. If any text overlaps other text, if any text overlaps or is overlapped by a visual element (bar, dot, line, circle, block, gauge, background shape), or if any visual overlaps another visual, flag as critical. Value labels straddling a bar edge or sitting on a neighbouring bar/label also count as overlap. Instruct the fix to add spacing / move colliding content to its own row so nothing overlaps.

9. **CRITICAL — Height Fits One Page**: The total rendered height of the visualization MUST NOT exceed 720px. A chart taller than one page will overflow or be split across pages. If the layout looks like it would exceed ~720px tall (too many rows, oversized bars, large vertical stacks), flag as critical and instruct making it more compact.

10. **Visual Quality**: Inconsistent spacing, text truncation, poor contrast, elements wider than 700px.

11. **No Infographics**: The output must be a DATA CHART. If it contains KPI cards, icon badges, timeline blocks, styled card grids, flag as critical.

12. **External Dependencies**: CDN links, external fonts, icon libraries, external images.

13. **CRITICAL — Font Compliance**: The HTML must ONLY use: 'Calibri', 'Baskerville', 'Inter', 'DejaVu Serif'.

14. **CRITICAL — Hard Width Enforcement**: No element at any nesting level should render wider than 700px. Check for fixed widths > 700px, min-width > 700px, or layouts (flex/grid with large children) that could push content beyond 700px total width. The outermost container MUST have max-width: 700px with overflow: hidden.

15. **User Instruction Compliance**: The visualization must follow the user's refinement instructions.

16. **CRITICAL — No Citations or Links**: The visualization must NOT contain any citations, footnotes, reference numbers (e.g., [1], [2], [3]), hyperlinks (<a> tags), URLs, or source references anywhere in the chart. This includes chart labels, axis labels, bar labels, legend text, annotations, value labels, and category names. If any citation markers, bracketed numbers, or hyperlinks are found, flag as critical and instruct their removal.

RESPOND WITH JSON:
{
    "is_valid": true/false,
    "issues": ["list of specific issues found, empty if valid"],
    "severity": "pass" | "minor" | "critical",
    "fix_instructions": "Detailed instructions on how to fix the issues. Empty string if valid."
}"""


class HTMLRefineValidationResult(BaseModel):
    is_valid: bool
    issues: list[str] = Field(default_factory=list)
    severity: str = Field(description="pass, minor, or critical")
    fix_instructions: str = Field(default="")


def _cached_refine_system_message(text: str) -> SystemMessage:
    return SystemMessage(
        content=[
            {"type": "text", "text": text, "cache_control": {"type": "ephemeral"}},
        ]
    )


def _invoke_gemini_graph_refine(
    user_prompt: str,
    chat_id: str | None = None,
    user_id: str | None = None,
    *,
    log_context: str = "Updating a chart visualization based on feedback (backup)",
) -> HTMLVisualizationRefine:
    interaction = GEMINI_CLIENT.interactions.create(
        model=GEMINI_HTML_MODEL,
        input=user_prompt,
        system_instruction=HTML_GRAPH_REFINE_SYSTEM_PROMPT,
        response_format={
            "type": "text",
            "mime_type": "application/json",
            "schema": _GEMINI_HTML_REFINE_SCHEMA,
        },
        generation_config={
            "temperature": 0.2,
            "max_output_tokens": 16000,
        },
    )
    save_raw_llm_response(interaction, GEMINI_HTML_MODEL, log_context, chat_id, user_id=user_id)
    parsed = json.loads(strip_json_code_fence(interaction.output_text))
    result = HTMLVisualizationRefine(
        html_code=parsed["html_code"],
        visualization_types=parsed["visualization_types"],
        reasoning=parsed["reasoning"],
    )
    if not result.html_code:
        raise ValueError("Model returned empty structured output")
    return result


def _invoke_anthropic_viz_graph_refine(
    user_prompt: str,
    chat_id: str | None = None,
    user_id: str | None = None,
    *,
    log_context: str = "Updating a chart visualization based on feedback",
) -> HTMLVisualizationRefine:
    messages = [
        _cached_refine_system_message(HTML_GRAPH_REFINE_SYSTEM_PROMPT),
        HumanMessage(content=user_prompt),
    ]
    structured_llm = ANTHROPIC_VIZ_LLM.with_structured_output(HTMLVisualizationRefine, include_raw=True)
    raw_result = structured_llm.invoke(messages)
    save_raw_llm_response(raw_result["raw"], ANTHROPIC_MODEL_ID, log_context, chat_id, user_id=user_id)
    if raw_result["parsing_error"] is not None:
        raise raw_result["parsing_error"]
    result: HTMLVisualizationRefine = raw_result["parsed"]
    if not result or not result.html_code:
        raise ValueError("Model returned empty structured output")
    return result


def _validate_refine_html_for_pdf(html_code: str, table_data: str, user_prompt: str = None, chat_id: str | None = None, user_id: str | None = None) -> HTMLRefineValidationResult:
    """Validate generated HTML for PDF rendering issues."""
    js_patterns = [
        r'<script[\s>]', r'</script>', r'\bon\w+\s*=',
        r'javascript:', r'document\.', r'window\.',
    ]
    js_found = [p for p in js_patterns if re.search(p, html_code, re.IGNORECASE)]
    if js_found:
        return HTMLRefineValidationResult(
            is_valid=False,
            issues=[f"HTML contains JavaScript. Detected patterns: {js_found}"],
            severity="critical",
            fix_instructions="Remove ALL JavaScript. Use pure HTML+CSS only.",
        )

    oversized_widths = re.findall(r'width:\s*(\d+)px', html_code)
    oversized = [w for w in oversized_widths if int(w) > 750]
    if oversized:
        return HTMLRefineValidationResult(
            is_valid=False,
            issues=[f"Elements have widths exceeding PDF margins: {oversized}px. Max allowed is 700px."],
            severity="critical",
            fix_instructions="Reduce all element widths to max 700px. Use max-width: 700px on the outermost container with overflow: hidden and ensure all children fit within.",
        )

    user_instruction_check = ""
    if user_prompt:
        user_instruction_check = f"\n\n--- USER REFINEMENT INSTRUCTION ---\n{user_prompt}\n\nIMPORTANT: Verify that the visualization follows the user's refinement instruction above."

    validation_prompt = f"""Validate this HTML visualization for PDF rendering:

--- HTML CODE ---
{html_code}

--- ORIGINAL TABLE DATA ---
{table_data}{user_instruction_check}

Check if the HTML will render properly in PDF, looks professional, and correctly represents the table data."""

    try:
        response = SYNC_OPENAI_CLIENT.chat.completions.create(
            model=OPENAI_VALIDATION_MODEL,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": HTML_REFINE_VALIDATION_SYSTEM_PROMPT},
                {"role": "user", "content": validation_prompt},
            ],
        )
        save_raw_llm_response(response, OPENAI_VALIDATION_MODEL, "Checking a refined chart visualization", chat_id, user_id=user_id)
        result = json.loads(response.choices[0].message.content)
        return HTMLRefineValidationResult(
            is_valid=result.get("is_valid", False),
            issues=result.get("issues", []),
            severity=result.get("severity", "critical"),
            fix_instructions=result.get("fix_instructions", ""),
        )
    except Exception as e:
        logger.warning(f"HTML validation with OpenAI failed with error: {e}, falling back to Gemini")
        try:
            interaction = GEMINI_CLIENT.interactions.create(
                model=GEMINI_VALIDATION_MODEL,
                input=validation_prompt,
                system_instruction=HTML_REFINE_VALIDATION_SYSTEM_PROMPT,
                response_format={
                    "type": "text",
                    "mime_type": "application/json",
                    "schema": HTMLRefineValidationResult.model_json_schema(),
                },
                generation_config={
                    "temperature": 0.1,
                    "thinking_config": {"thinking_budget": 0},
                },
            )
            save_raw_llm_response(interaction, GEMINI_VALIDATION_MODEL, "Checking a refined chart visualization (backup)", chat_id, user_id=user_id)
            result = json.loads(strip_json_code_fence(interaction.output_text))
            return HTMLRefineValidationResult(
                is_valid=result.get("is_valid", False),
                issues=result.get("issues", []),
                severity=result.get("severity", "critical"),
                fix_instructions=result.get("fix_instructions", ""),
            )
        except Exception as gemini_e:
            logger.warning(f"HTML validation with Gemini also failed with error: {gemini_e}, skipping validation")
            return HTMLRefineValidationResult(is_valid=True, issues=[], severity="pass", fix_instructions="")


def _invoke_refine_with_fallback(
    user_prompt: str,
    chat_id: str | None = None,
    user_id: str | None = None,
) -> HTMLVisualizationRefine | None:
    """Try Anthropic Sonnet 4.6 (primary) -> Gemini 3.1 Pro fallback."""
    try:
        logger.info(f"[GraphRefine] Trying Anthropic Sonnet 4.6 ({ANTHROPIC_MODEL_ID})")
        result = _invoke_anthropic_viz_graph_refine(user_prompt, chat_id, user_id)
        logger.info(f"[GraphRefine] Anthropic Sonnet 4.6 succeeded: {result.visualization_types}")
        return result
    except Exception as e:
        logger.error(f"[GraphRefine] Anthropic Sonnet 4.6 failed: {e}")

    try:
        logger.info(f"[GraphRefine] Trying Gemini ({GEMINI_HTML_MODEL})")
        result = _invoke_gemini_graph_refine(user_prompt, chat_id, user_id)
        logger.info(f"[GraphRefine] Gemini succeeded: {result.visualization_types}")
        return result
    except Exception as e:
        logger.error(f"[GraphRefine] Gemini also failed: {e}")
        return None


def generate_html_visualization_refine(table_data: str, user_refine_prompt: str = None, existing_viz: str = None, chat_id: str | None = None, user_id: str | None = None) -> HTMLVisualizationRefine | None:
    """
    Generate a refined HTML chart visualization from table data using user instructions.

    Takes existing_viz so it can refine the current visualization based on user feedback.
    Fallback order: Sonnet 4.6 -> Gemini 3.1 Pro.
    Validates output for PDF safety.

    Args:
        table_data: The table data (markdown table, CSV, or structured text).
        user_refine_prompt: User instruction for customizing/refining the visualization.
        existing_viz: The existing HTML visualization to refine.

    Returns:
        HTMLVisualizationRefine with html_code, visualization_types, and reasoning.
        None if all providers fail.
    """
    user_instruction = ""
    if user_refine_prompt:
        user_instruction = f"\n\nUSER REFINEMENT INSTRUCTION: {user_refine_prompt}\nFollow the user's instruction precisely while refining the visualization."

    existing_viz_context = ""
    if existing_viz:
        existing_viz_context = f"\n\n--- EXISTING VISUALIZATION (TO REFINE) ---\n{existing_viz}\n\nRefine the above visualization based on the user's instruction. Keep what works, change what the user asks for."

    prompt_text = f"""INPUT DATA:
{table_data}{existing_viz_context}{user_instruction}

Analyze the above data and refine the existing visualization (or generate a new one if no existing viz provided) following the user's refinement instructions. You MUST provide all three fields: html_code, visualization_types, and reasoning."""

    visualization = _invoke_refine_with_fallback(prompt_text, chat_id=chat_id, user_id=user_id)

    if visualization is None:
        return None

    for attempt in range(MAX_VALIDATION_RETRIES):
        logger.info(f"[GraphRefine] Validating HTML (attempt {attempt + 1}/{MAX_VALIDATION_RETRIES})")
        validation = _validate_refine_html_for_pdf(visualization.html_code, table_data, user_prompt=user_refine_prompt, chat_id=chat_id, user_id=user_id)

        if validation.is_valid or validation.severity == "pass":
            logger.info("[GraphRefine] HTML validation passed")
            visualization.html_code = _sanitize_refine_html(visualization.html_code)
            visualization.html_code = _ensure_page_break_avoid(visualization.html_code)
            return visualization

        logger.warning(f"[GraphRefine] Validation failed ({validation.severity}): {validation.issues}")

        user_instruction_context = f"\n\nUSER REFINEMENT INSTRUCTION (must be followed): {user_refine_prompt}" if user_refine_prompt else ""
        fix_user_prompt = f"""INPUT DATA:
{table_data}

--- PREVIOUS ATTEMPT (HAD ISSUES) ---
{visualization.html_code}

--- FIX INSTRUCTIONS ---
{validation.fix_instructions}{user_instruction_context}

Generate a corrected HTML visualization that addresses all the issues above. Ensure it renders cleanly in PDF."""

        try:
            regen_result = _invoke_refine_with_fallback(fix_user_prompt, chat_id=chat_id, user_id=user_id)
            if regen_result is None:
                logger.error(f"[GraphRefine] Regeneration attempt {attempt + 1} failed")
                break
            visualization = regen_result
        except Exception as e:
            logger.error(f"[GraphRefine] Regeneration attempt {attempt + 1} failed: {e}")
            break

    visualization.html_code = _sanitize_refine_html(visualization.html_code)
    visualization.html_code = _ensure_page_break_avoid(visualization.html_code)
    return visualization


def _ensure_page_break_avoid(html_code: str) -> str:
    """Wrap HTML in a page-break-avoiding container if not already wrapped."""
    if "page-break-inside" in html_code and "break-inside" in html_code:
        return html_code
    return (
        '<div style="page-break-inside: avoid; break-inside: avoid;">'
        f"{html_code}"
        "</div>"
    )


# A4 page (297mm) minus 25mm top + 20mm bottom margins ≈ 252mm ≈ 952px of content
# height at 96dpi. Cap visualizations below this so a single viz can never be taller
# than one page (which would force a page break / overflow off the page).
MAX_VIZ_HEIGHT_PX = 900


def _enforce_pdf_size_constraints(html_code: str) -> str:
    """
    Enforce a hard WIDTH constraint on the outermost container so a chart can
    never exceed the PDF text column (which would clip into the page margin).
    A4 page with 22mm side margins = 166mm ≈ 628px content width at 96dpi.

    We clip on the X axis only (overflow-x). We deliberately do NOT clip
    vertically here — see _enforce_pdf_height_constraint.
    """
    if not (
        'max-width: 700px' in html_code
        or 'max-width:700px' in html_code
        or 'max-width: 620px' in html_code
    ):
        html_code = (
            '<div style="max-width: 620px; width: 100%; box-sizing: border-box; '
            'overflow-x: hidden; overflow-y: visible; margin: 0 auto;">'
            f"{html_code}"
            "</div>"
        )

    return _enforce_pdf_height_constraint(html_code)


def _enforce_pdf_height_constraint(html_code: str) -> str:
    """
    Keep the visualization together on one page WITHOUT clipping its content.

    We intentionally do NOT impose a fixed max-height with overflow:hidden:
    that silently truncated any visualization taller than the cap ("visual not
    completely generated" / "text missing beneath the visual"). Height is kept
    within one page by the generation prompt (compact, content-driven layouts),
    not by cutting content off here. No-op passthrough kept for backward
    compatibility with existing call sites.
    """
    return html_code


def _sanitize_refine_html(html_code: str) -> str:
    """Strip HTML comments, collapse empty lines, fix overflow issues, and strip citations/links."""
    html_code = re.sub(r'<!--.*?-->', '', html_code, flags=re.DOTALL)
    html_code = re.sub(r'\n\s*\n', '\n', html_code)

    html_code = re.sub(
        r'white-space:\s*nowrap',
        'white-space: normal',
        html_code
    )

    if 'max-width' in html_code and 'overflow-wrap' not in html_code:
        html_code = re.sub(
            r'(max-width:\s*700px[^;"]*;)',
            r'\1 box-sizing: border-box; overflow-x: hidden; overflow-wrap: break-word; word-wrap: break-word;',
            html_code,
            count=1
        )

    html_code = re.sub(
        r'(width:\s*\d{4,}px)',
        'width: 700px',
        html_code
    )
    html_code = re.sub(
        r'min-width:\s*\d{4,}px',
        'min-width: 0',
        html_code
    )

    html_code = re.sub(r'<a\s[^>]*>(.*?)</a>', r'\1', html_code, flags=re.DOTALL | re.IGNORECASE)
    html_code = re.sub(r'\[\d+\]', '', html_code)
    html_code = re.sub(r'<sup>\s*\d+\s*</sup>', '', html_code, flags=re.IGNORECASE)
    html_code = re.sub(r'<sup>\s*\[\d+\]\s*</sup>', '', html_code, flags=re.IGNORECASE)

    html_code = _enforce_pdf_size_constraints(html_code)

    return html_code.strip()
