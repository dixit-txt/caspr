"""
Primary Research domain -- prompt blocks.

These are appended/substituted into the card-generation prompt when the
Primary Research domain is active.  They override the default blocks in
``app.research.prompts.prompt_utils`` to enforce document-only sourcing, heavier table
usage, and data-centric tone.
"""

# ---------------------------------------------------------------------------
# DRL addendum -- injected into user_instructions before generate_drl
# ---------------------------------------------------------------------------

PR_DRL_ADDENDUM = """\
ADDITIONAL INSTRUCTIONS FOR REPORT STRUCTURE (Primary Research Analysis):

You are structuring a report that analyses PRIMARY RESEARCH DATA uploaded by
the user (e.g. survey results, interview transcripts, experimental data,
financial datasets).

Follow these rules when designing the section structure:

1. Organise sections around DATA THEMES found in the uploaded document, NOT
   generic topic headings.  Good examples: "Demographic Profile of
   Respondents", "Satisfaction Score Distribution", "Comparative Regional
   Analysis".  Bad examples: "Market Overview", "Industry Trends".
2. Include a "Research Methodology" section if the document describes any
   methodology, sampling, or data-collection process.
3. Include a "Key Findings / Data Analysis" section that presents the
   central quantitative or qualitative results.
4. Where relevant, add subsections for: distributions, trends over time,
   cross-tabulations / comparisons, and limitations / caveats.
5. Do NOT include sections that require external web data (e.g. "Competitive
   Landscape", "Market Forecast") -- the entire report is sourced from the
   uploaded document only."""


# ---------------------------------------------------------------------------
# Source rules -- replaces the default _FILE_SOURCE_BLOCK / _NO_FILE_SOURCE_BLOCK
# ---------------------------------------------------------------------------

PR_SOURCE_RULES = """\
SOURCE RULES (Primary Research -- uploaded document ONLY):
- The uploaded document is your ONLY source of information.
- Do NOT use web search.  All data, statistics, and findings must come
  exclusively from the uploaded document.
- If information is not available in the document, state that explicitly
  rather than speculating or filling with external data.

CITATION RULES (document-only):
- Use plain-text attribution: Source: <document title or topic> (uploaded document).
- Do NOT invent an author name.  Do NOT use "Caspr Research", "Ghost Research",
  or any brand/company name.
- Do NOT fabricate URLs.  There are no web citations in this report."""


# ---------------------------------------------------------------------------
# Table rules -- tighter table expectations for data-heavy reports
# ---------------------------------------------------------------------------

PR_TABLE_RULES = """\
Table Rules (Primary Research):
- **CRITICAL**: Each card (this section including ALL its sub-sections) may
  contain AT MOST ONE table total. Zero tables is fine. Prefer one data table
  summarising the most important findings when the document supports it; put
  secondary datasets in bullets or prose instead of extra tables.
- The section is mostly analytical prose. Use at most one table, and only where the document's data genuinely aids comparison.
- Use a table only for factual findings from the uploaded document.
- Every table MUST have a descriptive title line directly above it using the
  exact prefix "Title: " (capital T, colon, space). Keep titles under 15 words.
- Use 2-5 meaningful columns, consistent column counts, descriptive headers,
  and at least 3 data rows when enough data exists.
- Put units in headers, keep numbers consistently formatted, and avoid mostly
  empty/N/A tables.
- Source line below the table: "Source: <document title> (uploaded document)."
"""


# ---------------------------------------------------------------------------
# Formatting rules -- stricter analytical tone
# ---------------------------------------------------------------------------

PR_FORMATTING_RULES = """\
Writing and Formatting Rules (Primary Research):
- English only.  No emojis.
- Hold Caspr's analyst voice: conclusions not descriptions, numbers first, one plain sentence on what each finding means, no hedging language, no exclamation points.
- Mostly analytical prose. Lists are not analysis: use a bullet list only for genuinely discrete, parallel items (three or more), never to avoid a paragraph. Keep paragraphs to 2-3 sentences.
- Single-level bullets only ("- " prefix, one per line, end with period). When a bullet has a label followed by description, use a colon after the label.
- No filler words, no scene-setting intros, no repeated information.
- Do not include section or subsection headings inside content fields.
- Do not use numbering or lettering in section/subsection names. Section and subsection names state the finding, not the topic label.
- All content must be grammatically correct with proper spelling."""


# ---------------------------------------------------------------------------
# Citation rules (document-only)
# ---------------------------------------------------------------------------

PR_CITATION_RULES = """\
Citation Rules (Primary Research):
- Do NOT use markdown link citation format (no [Name](URL) links).
- Attribute all data to the uploaded document using plain text:
  Source: <document title or topic> (uploaded document).
- NEVER fabricate a URL or reference web/current-source retrieval internally; in user-facing terms, do not cite Learning Brain or external sources.
- NEVER list source citations as separate bullet points.  Inline only."""


# ---------------------------------------------------------------------------
# Document analysis prompt -- used by pr_analyze_document node
# ---------------------------------------------------------------------------

PR_DOCUMENT_ANALYSIS_PROMPT = """\
You are an expert research-data analyst.  The user has uploaded a document
containing primary research data.  Analyse the document and produce a
structured analysis answering the following questions:

1. **Data type**: What kind of research data is this?  (e.g. survey results,
   interview transcripts, experimental data, financial dataset, etc.)
2. **Key themes / variables**: What are the main themes, topics, or
   variables covered?
3. **Methodology**: Is a research methodology described?  If so, summarise it
   in 1-2 sentences.
4. **Sample information**: Is there information about sample size, population,
   or demographics?
5. **Key data points**: List the 5-10 most important quantitative or
   qualitative findings you can identify.
6. **Structure notes**: Any structural features of the document (tables,
   appendices, charts described in text, etc.).

Return your analysis as valid JSON matching this schema:

{{
  "data_type": "<string>",
  "key_themes": ["<string>", ...],
  "methodology_present": <true|false>,
  "methodology_summary": "<string or empty>",
  "sample_info": "<string or empty>",
  "key_data_points": ["<string>", ...],
  "structure_notes": "<string>"
}}"""

PR_DOCUMENT_ANALYSIS_SCHEMA = {
    "type": "object",
    "properties": {
        "data_type": {"type": "string"},
        "key_themes": {"type": "array", "items": {"type": "string"}},
        "methodology_present": {"type": "boolean"},
        "methodology_summary": {"type": "string"},
        "sample_info": {"type": "string"},
        "key_data_points": {"type": "array", "items": {"type": "string"}},
        "structure_notes": {"type": "string"},
    },
    "required": [
        "data_type",
        "key_themes",
        "methodology_present",
        "methodology_summary",
        "sample_info",
        "key_data_points",
        "structure_notes",
    ],
    "additionalProperties": False,
}
