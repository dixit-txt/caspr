import json
import os

import boto3
from botocore.config import Config
from google import genai
# from langchain_aws import ChatBedrock  # Bedrock disabled — using direct Anthropic API instead
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import SystemMessage, HumanMessage
from pydantic import BaseModel, Field

from src.config.constants import (
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
from src.config.log_helper import setup_logging
from src.core.llm_response_logger import save_raw_llm_response, strip_json_code_fence

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
    api_key=ANTHROPIC_API_KEY,
)

class HTMLVisualization(BaseModel):
    """Structured output for HTML visualization generation from table data."""

    html_code: str = Field(
        description="The complete, valid, self-contained HTML code with inline CSS that visualizes the table data. No JavaScript, no external dependencies."
    )
    visualization_types: str = Field(
        description="Comma-separated list of visual formats used (e.g., 'KPI cards, horizontal bar chart, styled table')"
    )
    reasoning: str = Field(
        description="Brief explanation of why these visualization types were chosen for the given data"
    )


_GEMINI_HTML_SCHEMA = {
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


HTML_GRAPH_SYSTEM_PROMPT = """You are an expert at creating clean HTML+CSS charts and graphs.

Your task: Convert the input table into a CHART or GRAPH using ONLY HTML and inline CSS.

RULES:
- Output ONLY charts and graphs. NOTHING ELSE.
- NEVER create infographics, KPI cards, icon grids, step timelines, card layouts, badge lists, or any decorative non-chart visual.
- NEVER just re-create the table with styled borders — that adds no value.
- Keep it simple, clean, and data-focused. The chart should communicate the data clearly at a glance.
- Use pure HTML + inline CSS only. ABSOLUTELY NO JavaScript — no <script> tags, no inline event handlers (onclick, onload, etc.), no javascript: URLs. JS will NOT execute in PDF.
- No SVG, no external libraries, no CDN links, no external fonts or images.
- Must render in markdown files and PDF exports.
- Keep it compact — the chart should fit naturally below a table in a report.
- Do NOT include any title or heading for the chart. Just the chart itself.
- IMPORTANT: Use VARIETY in chart types. Do NOT default to horizontal bar charts for everything. Choose the chart type that best fits the data shape.

CHART TYPES ALLOWED (pick the BEST fit — do NOT always use bar charts):

1. HORIZONTAL BAR CHART — comparing named categories by a single numeric value
   - CSS: rows with a fixed-width label + a colored div whose width is proportional to value

2. VERTICAL BAR CHART (column chart) — time-series, sequential, or ordered numeric data
   - CSS: inline-block divs with height proportional to value, aligned at the bottom using flexbox align-items: flex-end

3. STACKED BAR CHART — part-to-whole breakdown across categories
   - CSS: each row is a flex container with colored segments whose flex-basis is proportional to their share

4. GROUPED BAR CHART — multi-metric comparison across categories
   - CSS: each category row has multiple adjacent colored bars (side by side or stacked vertically in a group)

5. PROGRESS / GAUGE BARS — percentages, completion rates, scores out of 100
   - CSS: a grey track div with an inner colored div at the percentage width, label on the right

6. DONUT / PIE CHART (CSS conic-gradient) — proportional breakdown of a whole (2-6 segments max)
   - CSS: a square div with border-radius: 50% and background: conic-gradient(...) with color stops
   - Add a white inner circle for donut effect. Include a legend below with colored squares + labels.

7. HEAT MAP / MATRIX — two-dimensional categorical data with intensity values
   - CSS: grid of cells with background-color opacity or shade mapped to value intensity
   - Row headers on the left, column headers on top

8. LOLLIPOP CHART — similar to bar chart but lighter, a thin line with a circle at the end
   - CSS: each row has a thin 2px-height line + a 10px circle (border-radius:50%) at the value position

9. WATERFALL CHART — showing cumulative additions/subtractions (e.g., financial flows)
   - CSS: vertical bars positioned with margin-left to show running total, colored differently for positive/negative

10. RADIAL PROGRESS / RING GAUGES — individual metric scores (e.g., 72/100)
    - CSS: square div with conic-gradient showing filled arc, border-radius:50%, white inner circle, value centered

11. BULLET CHART — actual vs target comparisons
    - CSS: layered bars — a wider light background bar (target range) with a thinner dark bar (actual value) overlaid

12. SPARKLINE BAR STRIP — compact inline micro-chart for trends
    - CSS: a row of very thin vertical bars (2-4px wide) with varying heights showing a trend pattern

CHART SELECTION GUIDE — CHOOSE VARIETY:
- Single numeric value per category (≤8 categories) → LOLLIPOP CHART or HORIZONTAL BAR CHART (alternate between them)
- Single numeric value per category (>8 categories) → HORIZONTAL BAR CHART (sorted)
- Percentages / proportions / scores out of 100 → PROGRESS BARS or RADIAL RING GAUGES
- Part-to-whole breakdown (how a total splits into parts) → DONUT/PIE CHART or STACKED BAR CHART
- Time-series or sequential ordered data → VERTICAL BAR CHART or SPARKLINE STRIP
- Two-dimensional data (categories × categories with values) → HEAT MAP
- Multiple metrics per category → GROUPED BAR CHART
- Actual vs target/benchmark → BULLET CHART
- Cumulative additions and subtractions → WATERFALL CHART
- Rankings / sorted values → LOLLIPOP CHART (largest to smallest)
- Binary or few distinct states across items → HEAT MAP with 2-3 color levels
- Small number of proportions summing to 100% → DONUT CHART with legend

WHAT TO NEVER DO:
- NEVER output styled cards, KPI tiles, icon badges, or infographic-style layouts
- NEVER output timelines, step indicators, or process flows
- NEVER output colored pill badges or status indicators as standalone visuals
- NEVER output a styled HTML <table> — that is NOT a chart
- NEVER add decorative elements that don't represent data
- NEVER default to horizontal bar chart when another chart type fits better
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
  * #E8453C (caspr-red) — highlights, key insights, primary bars, emphasis, conic-gradient primary segment
  * #0B0B09 (caspr-black) — primary typography, headings, axis labels
  * #1A1A18 (caspr-grey-dark) — secondary text, dark chart elements, secondary segments in pie/donut
  * #6B6B66 (caspr-grey-mid) — secondary/supporting data, labels, annotations, tertiary segments
  * #F2F1EF (caspr-grey-light) — subtle backgrounds, card fills, containers, heat map low-intensity cells, track backgrounds for progress bars
  * #D6D4CF (caspr-rule) — borders, dividers, grid lines, separators, heat map medium-intensity cells
  * #FFFFFF (caspr-white) — chart background, donut inner circle
- NEVER use any color outside this palette (no blues, greens, purples, oranges, teals)
- Use caspr-red (#E8453C) for the primary/most important data series
- Use caspr-grey-dark (#1A1A18) and caspr-grey-mid (#6B6B66) for secondary data series
- For multi-segment charts (donut, stacked bar), use the full palette in order: #E8453C, #1A1A18, #6B6B66, #D6D4CF
- For heat maps, map intensity from #F2F1EF (low) → #D6D4CF (medium) → #E8453C (high)
- Adequate spacing between bars/elements (4-8px gaps)
- Value labels displayed at the end of each bar or inside if bar is large enough
- For donut/pie charts, always include a legend below the chart with colored squares + labels
- For lollipop charts, use a 2px line with a 10px dot at the value end
- NO title, NO heading, NO legend unless absolutely necessary for multi-series charts or donut/pie
- Max width 700px, all content must fit within this width with no overflow
- All text must wrap naturally — never clip or overflow

OUTPUT: Return ONLY the HTML for the chart wrapped in a page-break-avoiding container. Nothing else.

YOU MUST ALWAYS PROVIDE ALL THREE REQUIRED FIELDS IN YOUR RESPONSE:
1. html_code — the full HTML string
2. visualization_types — comma-separated list of chart types used (e.g., 'horizontal bar chart', 'progress bars')
3. reasoning — brief explanation of why you chose this chart type for the given data

NEVER return an empty response. Even if the input data is minimal, always generate a valid chart."""

MAX_VALIDATION_RETRIES = 1

HTML_VALIDATION_SYSTEM_PROMPT = """You are an expert HTML/CSS validator specializing in PDF rendering compatibility.

Your task: Inspect the provided HTML code and determine if it will render correctly when converted to PDF.

CHECK FOR THESE ISSUES (in priority order):

1. **CRITICAL — No JavaScript Allowed**: ANY <script> tags, inline event handlers (onclick, onload, onmouseover, etc.), or JavaScript expressions MUST be flagged as critical. JavaScript does NOT execute in PDF renderers and will result in broken/blank visualizations. This includes: <script>...</script>, <script src="...">, onclick="...", onload="...", onerror="...", javascript: URLs, dynamic DOM manipulation references, setTimeout/setInterval, document.write, or any JS framework code.

2. **CRITICAL — Page Break Safety**: The entire visualization MUST be wrapped in a container with `page-break-inside: avoid; break-inside: avoid;` so it never gets split across pages. If missing, flag as critical.

3. **CRITICAL — No HTML Comments**: The HTML must NOT contain any HTML comments (<!-- ... -->). Comments break markdown rendering when the HTML is embedded in markdown files. If any comments are found, flag as critical and instruct removal.

4. **CRITICAL — No Empty Lines Inside HTML**: The HTML must NOT contain blank/empty lines between elements inside a container. In markdown, blank lines inside HTML blocks cause paragraph breaks that destroy the HTML structure. If blank lines are found between HTML tags, flag as critical and instruct removal.

5. **CRITICAL — No Side-by-Side Text Columns**: The HTML must NOT use side-by-side flex columns (display:flex with two flex:1 children) or inline-block with percentage widths for TEXT content. This causes text overflow and clipping in PDF rendering. Side-by-side is ONLY acceptable for short labels (max 120px) next to bar charts. If text-heavy side-by-side layouts are found, flag as critical and instruct conversion to vertical stacked layout.

6. **CRITICAL — Overflow Prevention**: All text containers must have overflow-wrap: break-word. No element should use white-space: nowrap on text longer than a few words. The outer container must have max-width: 700px with box-sizing: border-box and `overflow-x: hidden` (X axis only). Flag as critical if the outer container uses `overflow: hidden` on BOTH axes, or combines a fixed height / max-height with `overflow: hidden` around chart content — that clips bars/labels/legends vertically. Also flag if `transform: scale(...)` is used to size the chart (it leaves phantom layout height / a blank page). If text can overflow its container, flag as critical.

7. **PDF Rendering Breakers**: Flexbox/Grid that wkhtmltopdf or weasyprint mishandle, viewport-relative units (vw/vh), position:fixed/sticky, CSS animations/transitions, media queries, overflow:hidden cutting off content, float collisions.

8. **CRITICAL — No Overlapping Elements**: NOTHING may overlap. If any text overlaps other text, if any text overlaps or is overlapped by a visual element (bar, dot, line, circle, block, gauge, background shape), or if any visual overlaps another visual, flag as critical. Value labels straddling a bar edge or sitting on a neighbouring bar/label also count as overlap. Every element must have clear spacing around it. Instruct the fix to add spacing / move colliding content to its own row so nothing overlaps.

9. **CRITICAL — Height Fits One Page**: The total rendered height of the visualization MUST NOT exceed 720px. A chart taller than one page will overflow or be split across pages. If the layout looks like it would exceed ~720px tall (too many rows, oversized bars, large vertical stacks), flag as critical and instruct making it more compact (smaller row heights, fewer rows, aggregation, or a more horizontal layout).

10. **Visual Ugliness**: Inconsistent spacing, text truncation, colors with poor contrast, elements wider than 700px causing horizontal overflow, unreadable font sizes (<10px or >24px), misaligned labels.

11. **No Infographics or Non-Chart Visuals**: The output must be a DATA CHART (bar chart, progress bars, grouped bars, stacked bars, donut/pie with conic-gradient, heat map grid, lollipop chart, waterfall chart, radial gauges, bullet chart, sparkline strip, etc.). If it contains KPI cards, icon badges, timeline blocks, styled card grids, pill badges, step indicators, or any infographic-style layout instead of a proper chart, flag as critical and instruct replacement with an appropriate chart type.

12. **External Dependencies**: CDN links, external fonts (Google Fonts), icon libraries (Font Awesome), external images, any resource requiring network access — these will fail in offline PDF rendering.

13. **CRITICAL — Font Compliance**: The HTML must ONLY use these fonts as the PRIMARY font-family: 'Calibri', 'Baskerville', 'Inter', 'DejaVu Serif'. If any other font is used as the PRIMARY font (Arial, Helvetica, Verdana, Roboto, Open Sans, Segoe UI, Tahoma, Georgia, etc.), flag as critical. NOTE: Using a generic fallback like sans-serif or serif AFTER an allowed font (e.g., font-family: 'Calibri', sans-serif) is PERFECTLY FINE and should NOT be flagged. Only flag if the primary font itself is not in the allowed list.

14. **CRITICAL — Hard Width Enforcement**: No element at any nesting level should render wider than 700px. Check for fixed widths > 700px, min-width > 700px, or layouts (flex/grid with large children) that could push content beyond 700px total width. The outermost container MUST have max-width: 700px with overflow: hidden.

15. **CRITICAL — No Citations or Links**: The visualization must NOT contain any citations, footnotes, reference numbers (e.g., [1], [2], [3]), hyperlinks (<a> tags), URLs, or source references anywhere in the chart. This includes chart labels, axis labels, bar labels, legend text, annotations, value labels, and category names. If any citation markers, bracketed numbers, or hyperlinks are found in the visualization text, flag as critical and instruct their removal.

RESPOND WITH JSON:
{
    "is_valid": true/false,
    "issues": ["list of specific issues found, empty if valid"],
    "severity": "pass" | "minor" | "critical",
    "fix_instructions": "Detailed instructions on how to fix the issues. Empty string if valid."
}

IMPORTANT: If ANY JavaScript is detected, immediately mark as is_valid: false with severity: "critical".
Be strict — if something MIGHT look bad in PDF, flag it."""


class HTMLValidationResult(BaseModel):
    is_valid: bool
    issues: list[str] = Field(default_factory=list)
    severity: str = Field(description="pass, minor, or critical")
    fix_instructions: str = Field(default="")


def _cached_system_message(text: str) -> SystemMessage:
    """Anthropic system prompt with ephemeral cache (reused across Sonnet viz calls)."""
    return SystemMessage(
        content=[
            {"type": "text", "text": text, "cache_control": {"type": "ephemeral"}},
        ]
    )


def _invoke_gemini_graph(
    user_prompt: str,
    chat_id: str | None = None,
    user_id: str | None = None,
    *,
    log_context: str = "Building an interactive chart visualization (backup)",
) -> HTMLVisualization:
    interaction = GEMINI_CLIENT.interactions.create(
        model=GEMINI_HTML_MODEL,
        input=user_prompt,
        system_instruction=HTML_GRAPH_SYSTEM_PROMPT,
        response_format={
            "type": "text",
            "mime_type": "application/json",
            "schema": _GEMINI_HTML_SCHEMA,
        },
        generation_config={
            "temperature": 0.2,
            "max_output_tokens": 16000,
        },
    )
    save_raw_llm_response(interaction, GEMINI_HTML_MODEL, log_context, chat_id, user_id=user_id)
    parsed = json.loads(strip_json_code_fence(interaction.output_text))
    result = HTMLVisualization(
        html_code=parsed["html_code"],
        visualization_types=parsed["visualization_types"],
        reasoning=parsed["reasoning"],
    )
    if not result.html_code:
        raise ValueError("Model returned empty structured output")
    return result


def _invoke_anthropic_viz_graph(
    user_prompt: str,
    chat_id: str | None = None,
    user_id: str | None = None,
    *,
    log_context: str = "Building an interactive chart visualization",
) -> HTMLVisualization:
    messages = [
        _cached_system_message(HTML_GRAPH_SYSTEM_PROMPT),
        HumanMessage(content=user_prompt),
    ]
    structured_llm = ANTHROPIC_VIZ_LLM.with_structured_output(HTMLVisualization, include_raw=True)
    raw_result = structured_llm.invoke(messages)
    save_raw_llm_response(raw_result["raw"], ANTHROPIC_MODEL_ID, log_context, chat_id, user_id=user_id)
    if raw_result["parsing_error"] is not None:
        raise raw_result["parsing_error"]
    result: HTMLVisualization = raw_result["parsed"]
    if not result or not result.html_code:
        raise ValueError("Model returned empty structured output")
    return result


def _validate_html_for_pdf(html_code: str, table_data: str, user_prompt: str = None, chat_id: str | None = None, user_id: str | None = None) -> HTMLValidationResult:
    """
    Validate generated HTML using OpenAI GPT-5.5 to check for PDF rendering issues.
    Includes a fast pre-check for JavaScript before calling the LLM.

    Args:
        html_code: The generated HTML code to validate.
        table_data: The original table data used to generate the HTML.
        user_prompt: Optional user instruction to validate compliance against.

    Returns:
        HTMLValidationResult with validation status and fix instructions if needed.
    """
    import re

    js_patterns = [
        r'<script[\s>]', r'</script>', r'\bon\w+\s*=',
        r'javascript:', r'document\.', r'window\.',
    ]
    js_found = [p for p in js_patterns if re.search(p, html_code, re.IGNORECASE)]
    if js_found:
        return HTMLValidationResult(
            is_valid=False,
            issues=["HTML contains JavaScript which will not render in PDF. "
                    f"Detected patterns: {js_found}"],
            severity="critical",
            fix_instructions="Remove ALL JavaScript: <script> tags, inline event handlers "
                            "(onclick, onload, etc.), and javascript: URLs. Replace any "
                            "JS-driven visuals with pure HTML+CSS equivalents.",
        )

    oversized_widths = re.findall(r'width:\s*(\d+)px', html_code)
    oversized = [w for w in oversized_widths if int(w) > 750]
    if oversized:
        return HTMLValidationResult(
            is_valid=False,
            issues=[f"Elements have widths exceeding PDF margins: {oversized}px. Max allowed is 700px."],
            severity="critical",
            fix_instructions="Reduce all element widths to max 700px. Use max-width: 700px on the outermost container with overflow: hidden and ensure all children fit within.",
        )

    user_instruction_check = ""
    if user_prompt:
        user_instruction_check = f"""

--- USER INSTRUCTION ---
{user_prompt}

IMPORTANT: In addition to PDF rendering checks, verify that the visualization follows the user's instruction above. If the chart type, color scheme, layout, or any other aspect contradicts what the user requested, mark as invalid with severity "critical" and provide fix instructions explaining how to align with the user's request."""

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
                {"role": "system", "content": HTML_VALIDATION_SYSTEM_PROMPT},
                {"role": "user", "content": validation_prompt},
            ],
        )
        save_raw_llm_response(response, OPENAI_VALIDATION_MODEL, "Checking that an interactive chart looks correct", chat_id, user_id=user_id)
        result = json.loads(response.choices[0].message.content)
        return HTMLValidationResult(
            is_valid=result.get("is_valid", False),
            issues=result.get("issues", []),
            severity=result.get("severity", "critical"),
            fix_instructions=result.get("fix_instructions", ""),
        )
    except Exception as e:
        logger.warning(f"HTML validation with OpenAI failed with error: {e}, falling back to Gemini")
        try:
            gemini_client = genai.Client(api_key=GEMINI_API_KEY)
            interaction = gemini_client.interactions.create(
                model=GEMINI_VALIDATION_MODEL,
                input=validation_prompt,
                system_instruction=HTML_VALIDATION_SYSTEM_PROMPT,
                response_format={
                    "type": "text",
                    "mime_type": "application/json",
                    "schema": HTMLValidationResult.model_json_schema(),
                },
                generation_config={
                    "temperature": 0.1,
                    "thinking_config": {"thinking_budget": 0},
                },
            )
            save_raw_llm_response(interaction, GEMINI_VALIDATION_MODEL, "Checking that an interactive chart looks correct (backup)", chat_id, user_id=user_id)
            result = json.loads(strip_json_code_fence(interaction.output_text))
            return HTMLValidationResult(
                is_valid=result.get("is_valid", False),
                issues=result.get("issues", []),
                severity=result.get("severity", "critical"),
                fix_instructions=result.get("fix_instructions", ""),
            )
        except Exception as gemini_e:
            logger.warning(f"HTML validation with Gemini also failed with error: {gemini_e}, skipping validation")
            return HTMLValidationResult(is_valid=True, issues=[], severity="pass", fix_instructions="")


def _invoke_with_fallback(
    user_prompt: str,
    chat_id: str | None = None,
    user_id: str | None = None,
) -> HTMLVisualization | None:
    """Try Anthropic Sonnet 4.6 (primary) -> Gemini 3.1 Pro fallback."""
    try:
        logger.info(f"Trying Anthropic Sonnet 4.6 ({ANTHROPIC_MODEL_ID})")
        result = _invoke_anthropic_viz_graph(user_prompt, chat_id, user_id)
        logger.info(f"Anthropic Sonnet 4.6 succeeded: {result.visualization_types}")
        return result
    except Exception as e:
        logger.error(f"Anthropic Sonnet 4.6 failed: {e}")

    try:
        logger.info(f"Trying Gemini ({GEMINI_HTML_MODEL})")
        result = _invoke_gemini_graph(user_prompt, chat_id, user_id)
        logger.info(f"Gemini succeeded: {result.visualization_types}")
        return result
    except Exception as e:
        logger.error(f"Gemini also failed: {e}")
        return None

def generate_html_visualization(table_data: str, user_refine_prompt: str = None, chat_id: str | None = None, user_id: str | None = None) -> HTMLVisualization | None:
    """
    Generate an HTML visualization from table data.

    Fallback order: Sonnet 4.6 -> Gemini 3.1 Pro.
    Whichever provider succeeds first, validate that HTML.
    Only regenerate if validation fails (up to MAX_VALIDATION_RETRIES times).

    Args:
        table_data: The table data (markdown table, CSV, or structured text).
        user_refine_prompt: Optional user instruction for customizing the visualization.

    Returns:
        HTMLVisualization with html_code, visualization_types, and reasoning.
        None if all providers fail.
    """
    user_instruction = ""
    if user_refine_prompt:
        user_instruction = f"\n\nUSER INSTRUCTION: {user_refine_prompt}\nFollow the user's instruction while generating the visualization."

    prompt_text = f"""INPUT DATA:
{table_data}{user_instruction}

Analyze the above data and generate a complete HTML+CSS visualization. You MUST provide all three fields: html_code, visualization_types, and reasoning."""

    visualization = _invoke_with_fallback(prompt_text, chat_id=chat_id, user_id=user_id)

    if visualization is None:
        return None

    # Validation loop
    for attempt in range(MAX_VALIDATION_RETRIES):
        logger.info(f"Validating HTML (attempt {attempt + 1}/{MAX_VALIDATION_RETRIES}) with {OPENAI_VALIDATION_MODEL}")
        validation = _validate_html_for_pdf(visualization.html_code, table_data, user_prompt=user_refine_prompt, chat_id=chat_id, user_id=user_id)

        if validation.is_valid or validation.severity == "pass":
            logger.info("HTML validation passed")
            visualization.html_code = _sanitize_html_for_markdown(visualization.html_code)
            visualization.html_code = _ensure_page_break_avoid(visualization.html_code)
            return visualization

        logger.warning(
            f"HTML validation failed (severity: {validation.severity}): {validation.issues}"
        )
        logger.info(f"Regenerating with fix instructions: {validation.fix_instructions[:200]}...")

        # Regenerate with fix context using the same fallback chain
        user_instruction_context = f"\n\nUSER INSTRUCTION (must be followed): {user_refine_prompt}" if user_refine_prompt else ""
        fix_user_prompt = f"""INPUT DATA:
{table_data}

--- PREVIOUS ATTEMPT (HAD ISSUES) ---
{visualization.html_code}

--- FIX INSTRUCTIONS ---
{validation.fix_instructions}{user_instruction_context}

Generate a corrected HTML visualization that addresses all the issues above. Ensure it renders cleanly in PDF without any overflow, broken layouts, or table representation problems."""

        try:
            regen_result = _invoke_with_fallback(fix_user_prompt, chat_id=chat_id, user_id=user_id)
            if regen_result is None:
                logger.error(f"Regeneration attempt {attempt + 1} failed: all providers returned None")
                break
            visualization = regen_result
        except Exception as e:
            logger.error(f"Regeneration attempt {attempt + 1} failed: {e}")
            break

    # Return best effort after retries exhausted
    visualization.html_code = _sanitize_html_for_markdown(visualization.html_code)
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
    not by cutting content off here. This is a no-op passthrough kept for
    backward compatibility with existing call sites.
    """
    return html_code


def _sanitize_html_for_markdown(html_code: str) -> str:
    """
    Strip HTML comments, collapse empty lines inside HTML blocks,
    fix common overflow issues, and remove citations/links to prevent rendering problems.
    """
    import re
    html_code = re.sub(r'<!--.*?-->', '', html_code, flags=re.DOTALL)
    html_code = re.sub(r'\n\s*\n', '\n', html_code)

    html_code = re.sub(
        r'white-space:\s*nowrap',
        'white-space: normal',
        html_code
    )

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
