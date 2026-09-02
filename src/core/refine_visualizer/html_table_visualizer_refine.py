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
    api_key=ANTHROPIC_API_KEY
)


class HTMLInfoVisualizationRefine(BaseModel):
    """Structured output for refined infographic-style HTML visualization from any table."""

    html_code: str = Field(
        description="Complete, self-contained HTML with inline CSS that transforms the table data into a creative visual infographic. No JavaScript, no external dependencies."
    )
    visualization_type: str = Field(
        description="The visual format used (e.g., 'bubble chart', 'timeline flow', 'proportional mosaic', 'gauge scorecard')"
    )
    reasoning: str = Field(
        description="Brief explanation of why this visual format was chosen for the given data"
    )


_GEMINI_INFO_REFINE_SCHEMA = {
    "type": "object",
    "properties": {
        "html_code": {
            "type": "string",
            "description": "Complete, self-contained HTML with inline CSS that transforms the table data into a creative visual infographic. No JavaScript, no external dependencies.",
        },
        "visualization_type": {
            "type": "string",
            "description": "The visual format used (e.g., 'bubble chart', 'timeline flow', 'proportional mosaic')",
        },
        "reasoning": {
            "type": "string",
            "description": "Brief explanation of why this visual format was chosen for the given data",
        },
    },
    "required": ["html_code", "visualization_type", "reasoning"],
}


INFO_VISUALIZER_REFINE_SYSTEM_PROMPT = """You are an expert information designer who transforms tabular data into creative, visually striking HTML infographics for PDF reports, specializing in REFINING visualizations based on user feedback.

Your task: Take input table data and TRANSFORM it into a VISUALLY CREATIVE infographic — NOT another table, NOT boring stacked cards. Think like a graphic designer creating a magazine infographic. The original table already exists in the report; your job is to make the data POP visually.

IMPORTANT: Follow the user's refinement instructions precisely. The user is providing specific guidance on how they want the visualization to look.

ABSOLUTE RULE — NEVER OUTPUT A TABLE:
- NEVER use <table>, <thead>, <tbody>, <tr>, <th>, or <td> tags.
- NEVER create anything that looks like a table with rows and columns.

VISUAL FORMATS TO USE (pick the MOST CREATIVE one that fits the data and user's instructions):

1. **Bubble / Circle Chart** — BEST for data with a numeric/size dimension:
   - Each item is a CIRCLE (div with border-radius: 50%) sized proportionally to its value
   - PREFERRED: lay the circles out in NORMAL FLOW using `display:flex; flex-wrap:wrap; gap:16px; align-items:center; justify-content:center;` so they can never overlap and the container grows to fit them
   - Only if you use position:absolute for artistic placement, the container's height MUST be large enough that every circle AND its labels sit fully inside it (lowest circle bottom + label < container height). Never rely on overflow to hide a circle. Never clip a circle.
   - Circles must be spaced apart with a clear visible gap (at least 8px) between them — they must NEVER overlap each other and must NEVER overlap any text or label
   - Each circle has: colored background, item name (bold, centered), key details (smaller text below name)
   - Size circles proportionally: largest value = largest circle (150-200px), smallest = 60-90px
   - Use DIFFERENT distinct colors for each bubble
   - Text inside circles: centered using display:flex; align-items:center; justify-content:center; flex-direction:column;
   - GREAT for: pricing, deal sizes, market caps, populations, revenue comparisons, any numeric magnitude

2. **Proportional Bars with Embedded Info** — For ranked/compared items:
   - Full-width blocks where the HEIGHT or visual weight corresponds to importance/value
   - Each block is a colored panel with the item info inside
   - Larger values get taller/more prominent panels
   - Good for: rankings, tiered comparisons

3. **Timeline / Flow** — For sequential or date-based data:
   - Build it as a NORMAL-FLOW vertical stack: an outer div that is `display:flex; flex-direction:column; gap:10px;`
   - Each event is ONE flex row: `display:flex; align-items:flex-start; gap:10px;` containing a small marker/dot (fixed ~14px circle) on the left and a content card on the right
   - The content card sizes to its own text (padding + auto height) — do NOT give it a fixed height and do NOT position it with absolute top/left
   - You MAY draw a connecting spine as a thin left border on the row container or a decorative 2px line, but the TEXT cards themselves must NEVER be absolutely positioned — flow layout guarantees rows can never overlap when text wraps
   - Good for: chronological events, process steps, milestones, dated records

4. **Concentric / Nested Rings** — For hierarchical or layered data:
   - Nested rounded containers, largest outside, smallest inside
   - Each layer represents a level/tier with its own background color
   - Text labels on each ring/layer
   - Good for: hierarchy, nested categories, containment relationships

5. **Visual Scorecard / Gauge Layout** — For ratings, confidence, performance data:
   - Each item gets a visual gauge: a circular progress indicator (using border tricks) or a filled dot scale
   - Circular gauges: div with border-radius: 50%, use border colors to show fill
   - Or use a row of dots (filled vs empty) to show rating
   - Good for: ratings, confidence levels, scores, completion percentages

6. **Mosaic / Treemap Style** — For part-to-whole relationships:
   - Blocks of different sizes arranged to fill a container
   - Size represents proportion/value; each block colored differently with SHORT text inside
   - Tile them with `display:flex; flex-wrap:wrap; gap:6px;` (adjust each block's flex-basis/width to its proportion) rather than absolute positioning, so blocks never overlap or clip
   - Good for: market share, portfolio breakdown, category distribution

7. **Radial / Hub Layout** — For items relating to a central concept:
   - A centered hub element (small circle or pill) holding the central topic at the top
   - The related items rendered BELOW as a responsive card grid (`display:grid; grid-template-columns: repeat(2, 1fr); gap:12px;`), each card auto-sizing to its content
   - Do NOT scatter text cards around the hub with absolute positioning — that causes overlap when text wraps. An optional single thin decorative line from the hub to the grid is fine.
   - Good for: related concepts, feature sets around a product, stakeholders around an entity

CRITICAL — LAYOUT METHOD (READ FIRST — PREVENTS OVERLAP, MISALIGNMENT & CLIPPING):
- Build the layout with NORMAL DOCUMENT FLOW: flexbox or CSS grid with `gap`. Flow layouts auto-size to their content, so cards can NEVER overlap each other and text can NEVER be clipped, even when text wraps to more lines than expected.
- `position: absolute` is RESTRICTED. You may use it ONLY for purely DECORATIVE, NON-TEXT elements: thin connector/spine lines, background rings, or a single centered hub circle with a very short label. You MUST NOT position any text-bearing card, box, label block, or content panel with `position: absolute` and hardcoded top/left — that is the #1 cause of overlapping and misaligned visuals in PDF.
- Every card/box/panel MUST size itself to its own content (use padding; height stays `auto`). See the NO FIXED HEIGHTS rule below.
- Arrange elements with flex/grid to create visual interest (rows, columns, wrapping grids, alternating alignment) — you do not need absolute positioning to look designed.
- Center the composition within the 620px max-width container.

CRITICAL — NO FIXED HEIGHTS ON CONTENT (PREVENTS CLIPPING / "TEXT MISSING BENEATH"):
- NEVER set a fixed `height` on any container, card, or shape that holds text. Fixed heights clip text when it wraps, or force siblings to overlap. Use `padding` and (optionally) `min-height` only — actual height must grow with the content.
- NEVER put `overflow: hidden` on a container that holds text; it silently cuts text off. Text-bearing containers must let their height grow. (`overflow: hidden` is acceptable ONLY on a purely decorative shape like a donut ring that contains no important text.)
- The visual must render COMPLETELY — every card, every label, every line of text fully visible. Nothing may be cut off at an edge.

CRITICAL — NO CSS TRANSFORMS FOR SIZING:
- NEVER use `transform: scale(...)` to shrink the visual. A CSS scale changes only the painted pixels; the layout box keeps its ORIGINAL size, leaving a large empty gap after the visual and pushing it onto a blank page. Size things correctly at 1:1 instead.

CRITICAL — NO OVERLAPPING ELEMENTS (STRICT — OVERRIDES ANY OTHER GUIDANCE):
- NOTHING may overlap. Shapes (circles, blocks, cards, bars) must NOT overlap each other — leave a clear visible gap (at least 8px) between every shape.
- Text must NEVER overlap other text, and text must NEVER overlap or be overlapped by any shape, line, gauge, or other visual element.
- Every label and value must sit in its own clear space with padding around it.
- When positioning absolute elements, compute their positions so that no two elements' bounding boxes intersect. If two elements would collide, space them out or switch to a VERTICAL STACKED layout.
- Connecting lines/spines must not run across or under any text.
- Do NOT rely on z-index to hide an overlap — avoid the overlap entirely. A plainer, non-overlapping layout is ALWAYS better than an overlapping one.

CRITICAL — WIDTH / BOUNDARY RULES (PDF MARGIN SAFETY):
- The outermost container MUST have: max-width: 620px; width: 100%; box-sizing: border-box; margin: 0 auto; overflow-x: hidden;
- Do NOT put `overflow: hidden` (both axes) on the outer container — clip on X only so tall content is never truncated.
- The TOTAL rendered width of ALL content must NOT exceed 620px. Maximum width for any child: 600px.
- NEVER use negative left/right/margin values to pull elements around.
- If you use any decorative absolute element, it must stay fully within the 620px width.

RADIAL / SPOKE / HUB LAYOUTS (e.g. a central concept with items around it):
- Do NOT scatter text cards around a center with absolute top/left — when the text wraps, the cards collide. This produced repeated overlap/misalignment bugs.
- Instead: put a centered hub header (a pill or small circle with the central concept) at the top, then render the surrounding items as a RESPONSIVE CARD GRID below it: `display:grid; grid-template-columns: repeat(2, 1fr); gap:12px;` (use 1 column if items are text-heavy, up to 3 columns for short items).
- Each grid card sizes to its content (padding, auto height). This looks intentional and can never overlap or clip.
- A single thin decorative connector line from the hub to the grid is fine, but it must carry no text and sit behind nothing important.

CARDS / TILED LAYOUTS (stakeholder maps, ecosystems, comparison cards):
- Use flexbox or grid with `gap` for all card grids. Every card auto-sizes to its text.
- Cards in the same row do NOT need equal height, but if you want them aligned use `align-items: stretch` — never a fixed pixel height.
- The bottom line of text in every card (e.g. a "USE:", "Trade-off:", "Payoff:" line) must be fully visible — never let a card's fixed height cut it off.

STRICT COLOR PALETTE — Use ONLY these 7 colors. No exceptions:
- caspr-black: #0B0B09 — primary typography, headings, dark shape fills
- caspr-white: #FFFFFF — backgrounds, text on dark shapes
- caspr-red: #E8453C — highlights, key insights, accent shapes, primary emphasis
- caspr-grey-dark: #1A1A18 — secondary text, dark UI elements, shape fills for variety
- caspr-grey-mid: #6B6B66 — secondary/supporting data, labels, metadata text
- caspr-grey-light: #F2F1EF — subtle backgrounds, cards, lighter shape fills
- caspr-rule: #D6D4CF — borders, dividers, connecting lines, separators

HOW TO USE COLORS FOR VISUAL VARIETY WITH ONLY THIS PALETTE:
- For bubble/circle charts with multiple items, alternate between: #E8453C (red), #0B0B09 (black), #1A1A18 (grey-dark), #6B6B66 (grey-mid)
- The MOST IMPORTANT or LARGEST item should be #E8453C (red) — it draws the eye
- Secondary items use #0B0B09 (black) or #1A1A18 (grey-dark)
- Less important / smaller items use #6B6B66 (grey-mid)
- Background shapes or subtle containers use #F2F1EF (grey-light)
- Connecting lines and dividers use #D6D4CF (rule)

TEXT COLOR RULES:
- On #E8453C (red) backgrounds: use #FFFFFF (white text)
- On #0B0B09 (black) backgrounds: use #FFFFFF (white text)
- On #1A1A18 (grey-dark) backgrounds: use #FFFFFF (white text)
- On #6B6B66 (grey-mid) backgrounds: use #FFFFFF (white text)
- On #F2F1EF (grey-light) backgrounds: use #0B0B09 (black text)
- On #FFFFFF (white) backgrounds: use #0B0B09 (black text)

NEVER USE ANY OTHER COLORS.

BADGE/TAG STYLING (for categorical values like confidence/status):
- display: inline-block; padding: 2px 8px; border-radius: 3px; font-size: 10px; font-weight: 600;
- High/Important: background #E8453C, text #FFFFFF
- Medium/Moderate: background #6B6B66, text #FFFFFF
- Low/Minor: background #F2F1EF, text #1A1A18

CRITICAL TECHNICAL RULES:

NO JavaScript:
- ABSOLUTELY NO <script> tags, event handlers (onclick, onload), or javascript: URLs.

NO SVG:
- Do NOT use <svg> tags. Use CSS-only shapes.

NO HTML COMMENTS:
- Do NOT include <!-- ... --> comments anywhere.

NO EMPTY LINES INSIDE HTML:
- No blank lines between HTML elements. Keep tags on consecutive lines.

PAGE-BREAK & HEIGHT SAFETY (MUST FIT ON ONE PAGE, WITHOUT CLIPPING):
- Wrap everything in: page-break-inside: avoid; break-inside: avoid;
- The TOTAL rendered height of the entire visualization MUST NOT exceed ~700px. A visualization taller than one page cannot avoid a page break and will spill off the bottom.
- Achieve this by DESIGNING COMPACTLY (fewer/smaller shapes, tighter spacing, shorter labels, aggregation) — NOT by setting a fixed height and clipping. The container height must stay `auto` (content-driven); never a fixed `height` that could cut content off.
- If the data genuinely has many items, show fewer/aggregated items or use a compact grid — do NOT build a tall infographic that runs off the page, and do NOT truncate to fit.

OVERFLOW PREVENTION:
- Outermost container: max-width: 620px; width: 100%; box-sizing: border-box; overflow-x: hidden; (NEVER `overflow: hidden` on both axes — it truncates content vertically).
- ALL text elements: overflow-wrap: break-word; word-wrap: break-word;
- NEVER use white-space: nowrap.
- Text inside circles: keep short, use smaller font if needed. If it won't fit, put the label BELOW/OUTSIDE the circle — never clip it.

FONT RESTRICTIONS:
- ONLY use: 'Calibri', 'Baskerville', 'Inter', 'DejaVu Serif'
- Default: font-family: 'Calibri', sans-serif;
- Bold headings: font-family: 'Inter', sans-serif;
- NEVER use Arial, Helvetica, Verdana, Roboto, or any other font.

TEXT INSIDE SHAPES:
- Circle text: centered with flexbox, keep minimal (Name bold 12-13px + 1-2 detail lines 10px)
- For small circles (under 80px): only show name, put details outside
- Ensure text color has strong contrast against the shape background

DATA INTEGRITY:
- ALL data from the input must be represented. Every row, every column value.
- Transform the PRESENTATION, not the content.
- If data won't fit inside shapes, use a compact legend below the visual.

CRITICAL — NO CITATIONS OR LINKS IN VISUALIZATIONS:
- NEVER include citations, footnotes, reference numbers (e.g., [1], [2], [3]), or source links in any part of the visualization.
- NEVER include hyperlinks (<a> tags), URLs, or any clickable references in the infographic.
- NEVER add citation markers like [1], [2], superscript numbers, or footnote references in any text within the visualization — including inside shapes, labels, legends, or annotations.
- The visualization must contain ONLY clean data labels and values — no references, no sources, no links.
- If the input data contains citations or reference markers in text, STRIP THEM OUT before using the text as labels.
- This applies to ALL text in the infographic: shape labels, detail text, legend entries, annotations, category names — EVERYTHING must be citation-free.

OUTPUT: Provide all three required fields:
1. html_code — the full HTML string (creative infographic, NOT a table)
2. visualization_type — what visual format you used (e.g., "bubble chart", "timeline flow")
3. reasoning — why this format works for this specific data"""


MAX_VALIDATION_RETRIES = 1

INFO_REFINE_VALIDATION_SYSTEM_PROMPT = """You are an expert HTML/CSS validator for PDF-rendered infographic layouts.

Your task: Check if the provided HTML infographic will render correctly when converted to PDF.

CHECK FOR THESE ISSUES:

1. **CRITICAL — No Tables**: The output must NOT contain <table>, <tr>, <td>, <th>, <thead>, <tbody> tags.

2. **CRITICAL — No JavaScript**: ANY <script> tags, event handlers, or JS expressions.

3. **CRITICAL — Page Break Safety**: Must have page-break-inside: avoid; break-inside: avoid; on the outer container.

4. **CRITICAL — No HTML Comments**: No <!-- ... --> allowed.

5. **CRITICAL — No Empty Lines**: No blank lines between HTML elements.

6. **CRITICAL — Overflow**: Outer container must have max-width: 620-700px with box-sizing: border-box and `overflow-x: hidden` (X axis only). Flag as critical if the outer container uses `overflow: hidden` on BOTH axes or has any fixed/max height combined with `overflow: hidden` around text — that clips content vertically ("visual not fully generated"). Text must wrap.

7. **CRITICAL — Font Compliance**: Only 'Calibri', 'Baskerville', 'Inter', 'DejaVu Serif' allowed.

8. **Data Integrity**: ALL rows and columns from the original data must be represented.

9. **CRITICAL — No Overlapping Elements**: NOTHING may overlap. If any shape (circle, block, card, bar) overlaps another shape, if any text overlaps other text, or if any text overlaps / is overlapped by a shape, line, or other visual element, flag as critical. Shapes must have clear gaps (at least ~8px) between them and text must sit in its own clear space. Instruct the fix to space elements apart or switch to a vertical stacked layout so nothing overlaps.

10. **CRITICAL — Height Fits One Page (WITHOUT CLIPPING)**: The total rendered height of the visualization MUST NOT exceed ~700px. If it would exceed that (too many items, oversized shapes, tall vertical stacks), flag as critical and instruct making it more compact (smaller shapes, less spacing, fewer/aggregated items). This must be achieved by compact design, NOT by clipping — see item 17.

11. **Visual Quality**: Readable text, adequate contrast, elements not clipped or overflowing.

12. **No External Dependencies**: No CDN, external fonts, images, or SVG.

13. **CRITICAL — No Absolutely-Positioned Text Cards**: Text-bearing cards, boxes, content panels, or label blocks must NOT use `position: absolute` with hardcoded top/left values. This is the #1 cause of overlapping/misaligned/hidden cards in PDF (when text wraps, absolutely-positioned siblings collide). Flag as critical and instruct converting to a normal-flow flexbox/grid layout with `gap`. `position: absolute` is acceptable ONLY for decorative non-text elements (thin connector/spine lines, background rings, a single centered hub circle with a very short label).

14. **Hard Width Enforcement**: No element at any nesting level should render wider than 700px. Check for fixed widths > 700px, percentage widths on containers wider than 700px, or flex/grid layouts that could push content beyond 700px total width.

15. **User Instruction Compliance**: The visualization must follow the user's refinement instructions.

16. **CRITICAL — No Citations or Links**: The visualization must NOT contain any citations, footnotes, reference numbers (e.g., [1], [2], [3]), hyperlinks (<a> tags), URLs, or source references anywhere in the infographic. This includes text inside shapes, labels, legends, annotations, and category names. If any citation markers, bracketed numbers, or hyperlinks are found, flag as critical and instruct their removal.

17. **CRITICAL — No Fixed Heights / No Vertical Clipping**: Flag as critical if any text-containing container/card/shape has a fixed `height` (e.g. `height: 480px`) or a `max-height` combined with `overflow: hidden`, because text that exceeds it gets cut off ("visualization not completely generated", "text missing beneath the visual"). Text containers must use padding with auto/min-height so they grow to fit. Instruct removing the fixed height and any vertical `overflow: hidden`.

18. **CRITICAL — No transform: scale()**: Flag as critical if `transform: scale(...)` is used to size the visual. A CSS scale leaves the original layout box size, creating a large empty gap after the visual / a blank page. Instruct sizing at 1:1 instead.

RESPOND WITH JSON:
{
    "is_valid": true/false,
    "issues": ["list of issues found"],
    "severity": "pass" | "minor" | "critical",
    "fix_instructions": "How to fix. Empty if valid."
}"""


class HTMLInfoRefineValidationResult(BaseModel):
    is_valid: bool
    issues: list[str] = Field(default_factory=list)
    severity: str = Field(description="pass, minor, or critical")
    fix_instructions: str = Field(default="")


def _cached_refine_system_message(text: str) -> SystemMessage:
    """Anthropic system prompt with ephemeral cache (reused across Sonnet viz calls)."""
    return SystemMessage(
        content=[
            {"type": "text", "text": text, "cache_control": {"type": "ephemeral"}},
        ]
    )


def _invoke_gemini_info_refine(
    user_prompt: str,
    chat_id: str | None = None,
    user_id: str | None = None,
    *,
    log_context: str = "Updating a table visualization based on feedback (backup)",
) -> HTMLInfoVisualizationRefine:
    interaction = GEMINI_CLIENT.interactions.create(
        model=GEMINI_HTML_MODEL,
        input=user_prompt,
        system_instruction=INFO_VISUALIZER_REFINE_SYSTEM_PROMPT,
        response_format={
            "type": "text",
            "mime_type": "application/json",
            "schema": _GEMINI_INFO_REFINE_SCHEMA,
        },
        generation_config={
            "temperature": 0.3,
            "max_output_tokens": 16000,
        },
    )
    save_raw_llm_response(interaction, GEMINI_HTML_MODEL, log_context, chat_id, user_id=user_id)
    parsed = json.loads(strip_json_code_fence(interaction.output_text))
    result = HTMLInfoVisualizationRefine(
        html_code=parsed["html_code"],
        visualization_type=parsed["visualization_type"],
        reasoning=parsed["reasoning"],
    )
    if not result.html_code:
        raise ValueError("Model returned empty structured output")
    return result


def _invoke_anthropic_viz_info_refine(
    user_prompt: str,
    chat_id: str | None = None,
    user_id: str | None = None,
    *,
    log_context: str = "Updating a table visualization based on feedback",
) -> HTMLInfoVisualizationRefine:
    messages = [
        _cached_refine_system_message(INFO_VISUALIZER_REFINE_SYSTEM_PROMPT),
        HumanMessage(content=user_prompt),
    ]
    structured_llm = ANTHROPIC_VIZ_LLM.with_structured_output(HTMLInfoVisualizationRefine, include_raw=True)
    raw_result = structured_llm.invoke(messages)
    save_raw_llm_response(raw_result["raw"], ANTHROPIC_MODEL_ID, log_context, chat_id, user_id=user_id)
    if raw_result["parsing_error"] is not None:
        raise raw_result["parsing_error"]
    result: HTMLInfoVisualizationRefine = raw_result["parsed"]
    if not result or not result.html_code:
        raise ValueError("Model returned empty structured output")
    return result


def _validate_info_refine_html(html_code: str, table_data: str, user_prompt: str = None, chat_id: str | None = None, user_id: str | None = None) -> HTMLInfoRefineValidationResult:
    """Validate generated HTML infographic for PDF rendering issues."""
    js_patterns = [
        r'<script[\s>]', r'</script>', r'\bon\w+\s*=',
        r'javascript:', r'document\.', r'window\.',
    ]
    js_found = [p for p in js_patterns if re.search(p, html_code, re.IGNORECASE)]
    if js_found:
        return HTMLInfoRefineValidationResult(
            is_valid=False,
            issues=[f"HTML contains JavaScript. Detected patterns: {js_found}"],
            severity="critical",
            fix_instructions="Remove ALL JavaScript. Use pure HTML+CSS only.",
        )

    table_tags = re.findall(r'<(table|thead|tbody|tr|th|td)[\s>]', html_code, re.IGNORECASE)
    if table_tags:
        return HTMLInfoRefineValidationResult(
            is_valid=False,
            issues=[f"Output contains table tags: {set(table_tags)}. Must use infographic layout."],
            severity="critical",
            fix_instructions="Replace all <table>/<tr>/<td> with div-based infographic layouts.",
        )

    oversized_widths = re.findall(r'width:\s*(\d+)px', html_code)
    oversized = [w for w in oversized_widths if int(w) > 750]
    if oversized:
        return HTMLInfoRefineValidationResult(
            is_valid=False,
            issues=[f"Elements have widths exceeding PDF margins: {oversized}px. Max allowed is 700px."],
            severity="critical",
            fix_instructions="Reduce all element widths to max 700px. Use max-width: 700px on the outermost container and ensure all children fit within.",
        )

    user_instruction_check = ""
    if user_prompt:
        user_instruction_check = f"\n\n--- USER REFINEMENT INSTRUCTION ---\n{user_prompt}\n\nIMPORTANT: Verify that the visualization follows the user's refinement instruction."

    validation_prompt = f"""Validate this HTML infographic visualization for PDF rendering:

--- HTML CODE ---
{html_code}

--- ORIGINAL TABLE DATA ---
{table_data}{user_instruction_check}

Check rendering safety, layout validity, data integrity, and visual quality."""

    try:
        response = SYNC_OPENAI_CLIENT.chat.completions.create(
            model=OPENAI_VALIDATION_MODEL,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": INFO_REFINE_VALIDATION_SYSTEM_PROMPT},
                {"role": "user", "content": validation_prompt},
            ],
        )
        save_raw_llm_response(response, OPENAI_VALIDATION_MODEL, "Checking a refined table visualization", chat_id, user_id=user_id)
        result = json.loads(response.choices[0].message.content)
        return HTMLInfoRefineValidationResult(
            is_valid=result.get("is_valid", False),
            issues=result.get("issues", []),
            severity=result.get("severity", "critical"),
            fix_instructions=result.get("fix_instructions", ""),
        )
    except Exception as e:
        logger.warning(f"Info HTML validation with OpenAI failed: {e}, falling back to Gemini")
        try:
            interaction = GEMINI_CLIENT.interactions.create(
                model=GEMINI_VALIDATION_MODEL,
                input=validation_prompt,
                system_instruction=INFO_REFINE_VALIDATION_SYSTEM_PROMPT,
                response_format={
                    "type": "text",
                    "mime_type": "application/json",
                    "schema": HTMLInfoRefineValidationResult.model_json_schema(),
                },
                generation_config={
                    "temperature": 0.1,
                    "thinking_config": {"thinking_budget": 0},
                },
            )
            save_raw_llm_response(interaction, GEMINI_VALIDATION_MODEL, "Checking a refined table visualization (backup)", chat_id, user_id=user_id)
            result = json.loads(strip_json_code_fence(interaction.output_text))
            return HTMLInfoRefineValidationResult(
                is_valid=result.get("is_valid", False),
                issues=result.get("issues", []),
                severity=result.get("severity", "critical"),
                fix_instructions=result.get("fix_instructions", ""),
            )
        except Exception as gemini_e:
            logger.warning(f"Info HTML validation with Gemini also failed: {gemini_e}, skipping validation")
            return HTMLInfoRefineValidationResult(is_valid=True, issues=[], severity="pass", fix_instructions="")


def _invoke_info_refine_with_fallback(
    user_prompt: str,
    chat_id: str | None = None,
    user_id: str | None = None,
) -> HTMLInfoVisualizationRefine | None:
    """Try Anthropic Sonnet 4.6 (primary) -> Gemini 3.1 Pro fallback on Sonnet failure."""
    try:
        logger.info(f"[InfoVizRefine] Trying Anthropic Sonnet 4.6 ({ANTHROPIC_MODEL_ID})")
        result = _invoke_anthropic_viz_info_refine(user_prompt, chat_id, user_id)
        logger.info(f"[InfoVizRefine] Anthropic Sonnet 4.6 succeeded: {result.visualization_type}")
        return result
    except Exception as e:
        logger.error(f"[InfoVizRefine] Anthropic Sonnet 4.6 failed: {e}")

    try:
        logger.info(f"[InfoVizRefine] Trying Gemini ({GEMINI_HTML_MODEL})")
        result = _invoke_gemini_info_refine(user_prompt, chat_id, user_id)
        logger.info(f"[InfoVizRefine] Gemini succeeded: {result.visualization_type}")
        return result
    except Exception as e:
        logger.error(f"[InfoVizRefine] Gemini also failed: {e}")
        return None


def generate_info_visualization_refine(table_data: str, user_refine_prompt: str = None, existing_viz: str = None, chat_id: str | None = None, user_id: str | None = None) -> HTMLInfoVisualizationRefine | None:
    """
    Generate a refined creative infographic-style HTML visualization from table data.

    Takes existing_viz so it can refine the current visualization based on user feedback.
    Transforms tables into bubble charts, timelines, mosaics, gauges, or other visual formats.
    Uses Sonnet 4.6 primary with Gemini 3.1 Pro fallback. Validates output for PDF safety.

    Args:
        table_data: The table data (markdown table, CSV, or structured text).
        user_refine_prompt: User instruction for customizing/refining the visualization.
        existing_viz: The existing HTML visualization to refine.

    Returns:
        HTMLInfoVisualizationRefine with html_code, visualization_type, and reasoning.
        None if all providers fail.
    """
    user_instruction = ""
    if user_refine_prompt:
        user_instruction = f"\n\nUSER REFINEMENT INSTRUCTION: {user_refine_prompt}\nFollow the user's instruction precisely for the visualization style."

    existing_viz_context = ""
    if existing_viz:
        existing_viz_context = f"\n\n--- EXISTING VISUALIZATION (TO REFINE) ---\n{existing_viz}\n\nRefine the above visualization based on the user's instruction. Keep what works, change what the user asks for."

    prompt_text = f"""INPUT TABLE DATA:
{table_data}{existing_viz_context}{user_instruction}

Refine the existing visualization (or generate a new one if no existing viz provided) into a CREATIVE visual infographic (bubble chart, timeline, mosaic, gauges, etc. — NOT a table) following the user's refinement instructions. You MUST provide all three fields: html_code, visualization_type, and reasoning."""

    visualization = _invoke_info_refine_with_fallback(prompt_text, chat_id=chat_id, user_id=user_id)

    if visualization is None:
        return None

    for attempt in range(MAX_VALIDATION_RETRIES):
        logger.info(f"[InfoVizRefine] Validating HTML (attempt {attempt + 1}/{MAX_VALIDATION_RETRIES})")
        validation = _validate_info_refine_html(visualization.html_code, table_data, user_prompt=user_refine_prompt, chat_id=chat_id, user_id=user_id)

        if validation.is_valid or validation.severity == "pass":
            logger.info("[InfoVizRefine] HTML validation passed")
            visualization.html_code = _sanitize_info_refine_html(visualization.html_code)
            visualization.html_code = _ensure_page_break_avoid(visualization.html_code)
            return visualization

        logger.warning(f"[InfoVizRefine] Validation failed ({validation.severity}): {validation.issues}")

        fix_user_prompt = f"""INPUT TABLE DATA:
{table_data}

--- PREVIOUS ATTEMPT (HAD ISSUES) ---
{visualization.html_code}

--- FIX INSTRUCTIONS ---
{validation.fix_instructions}

USER REFINEMENT INSTRUCTION: {user_refine_prompt or 'None'}

Generate a corrected CREATIVE infographic visualization (NOT a table). Ensure PDF safety."""

        try:
            regen_result = _invoke_info_refine_with_fallback(
                fix_user_prompt,
                chat_id=chat_id,
                user_id=user_id,
            )
            if regen_result is None:
                logger.error(f"[InfoVizRefine] Regeneration attempt {attempt + 1} failed")
                break
            visualization = regen_result
        except Exception as e:
            logger.error(f"[InfoVizRefine] Regeneration attempt {attempt + 1} failed: {e}")
            break

    visualization.html_code = _sanitize_info_refine_html(visualization.html_code)
    visualization.html_code = _ensure_page_break_avoid(visualization.html_code)
    return visualization


def _ensure_page_break_avoid(html_code: str) -> str:
    """Wrap HTML in a page-break-avoiding container if not already present."""
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
    Enforce a hard WIDTH constraint on the outermost container so the
    infographic can never exceed the PDF text column (which would clip into the
    page margin). A4 page with 22mm side margins = 166mm ≈ 628px content width.

    We clip on the X axis only (overflow-x). We deliberately do NOT use
    transform: scale() here (a CSS scale shrinks the visual but the layout box
    still reserves the UNSCALED height, producing large phantom gaps between the
    table and the visual and blank pages) and we do NOT clip vertically — see
    _enforce_pdf_height_constraint.
    """
    if not ('max-width: 620px' in html_code or 'max-width:620px' in html_code):
        html_code = (
            '<div style="max-width: 620px; width: 100%; box-sizing: border-box; '
            'overflow-x: hidden; overflow-y: visible; margin: 0 auto; position: relative;">'
            f"{html_code}"
            "</div>"
        )

    return _enforce_pdf_height_constraint(html_code)


def _enforce_pdf_height_constraint(html_code: str) -> str:
    """
    Keep the visualization together on one page WITHOUT clipping its content.

    We intentionally do NOT impose a fixed max-height with overflow:hidden:
    that silently truncated any infographic taller than the cap ("visual not
    completely generated" / "text missing beneath the visual"). Height is kept
    within one page by the generation prompt (compact, content-driven layouts),
    not by cutting content off here. No-op passthrough kept for backward
    compatibility with existing call sites.
    """
    return html_code


def _sanitize_info_refine_html(html_code: str) -> str:
    """Strip HTML comments, collapse empty lines, fix overflow issues, remove table tags, and strip citations/links."""
    html_code = re.sub(r'<!--.*?-->', '', html_code, flags=re.DOTALL)
    html_code = re.sub(r'\n\s*\n', '\n', html_code)

    html_code = re.sub(
        r'white-space:\s*nowrap',
        'white-space: normal',
        html_code
    )

    html_code = re.sub(
        r'max-width:\s*700px',
        'max-width: 620px',
        html_code
    )

    if 'max-width' in html_code and 'overflow-wrap' not in html_code:
        html_code = re.sub(
            r'(max-width:\s*\d+px[^;"]*;)',
            r'\1 box-sizing: border-box; overflow-x: hidden; overflow-wrap: break-word; word-wrap: break-word;',
            html_code,
            count=1
        )

    html_code = re.sub(
        r'width:\s*(\d+)px',
        lambda m: f'width: {min(int(m.group(1)), 620)}px' if int(m.group(1)) > 650 else m.group(0),
        html_code
    )
    html_code = re.sub(
        r'min-width:\s*\d{4,}px',
        'min-width: 0',
        html_code
    )

    html_code = re.sub(r'</?table[^>]*>', '', html_code, flags=re.IGNORECASE)
    html_code = re.sub(r'</?thead[^>]*>', '', html_code, flags=re.IGNORECASE)
    html_code = re.sub(r'</?tbody[^>]*>', '', html_code, flags=re.IGNORECASE)
    html_code = re.sub(r'</?tr[^>]*>', '', html_code, flags=re.IGNORECASE)
    html_code = re.sub(r'</?th[^>]*>', '', html_code, flags=re.IGNORECASE)
    html_code = re.sub(r'</?td[^>]*>', '', html_code, flags=re.IGNORECASE)

    html_code = re.sub(r'<a\s[^>]*>(.*?)</a>', r'\1', html_code, flags=re.DOTALL | re.IGNORECASE)
    html_code = re.sub(r'\[\d+\]', '', html_code)
    html_code = re.sub(r'<sup>\s*\d+\s*</sup>', '', html_code, flags=re.IGNORECASE)
    html_code = re.sub(r'<sup>\s*\[\d+\]\s*</sup>', '', html_code, flags=re.IGNORECASE)

    html_code = _enforce_pdf_size_constraints(html_code)

    return html_code.strip()
