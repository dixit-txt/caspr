"""pptx_utils.py — trimmed for caspr-api.

Keeps the lightweight, purely-textual card-JSON -> markdown helpers
(`generate_markdown_from_report`, `generate_pptx_markdown_from_report`,
`content_slides_for_report[_length]`) verbatim from the monolith — they need
none of the heavy PPTX-build toolchain, so moving them would have added a
network hop for no benefit (see report-render-service/README.md).

`generate_pptx_and_upload_s3` — the actual PPTX-binary-generation-and-upload
call — is an HTTP shim here: same original signature, forwards to
report-render-service's `POST /internal/render/presentation`. The real
implementation (python-pptx, pptxgenjs, LibreOffice disclaimer-slide merge)
lives there now, not in this file.
"""
import json
import os
from typing import Tuple, Dict, List, Any, Optional

from fastapi.concurrency import run_in_threadpool
import httpx

from src.core.integrations.s3_utils import get_s3_instance, build_report_s3_prefix
from src.config.log_helper import setup_logging
from src.config.constants import S3_REPORTS_BASE_PATH, REPORT_RENDER_SERVICE_BASE_URL
from src.core.cards.card_utils import convert_json_to_md, extract_markdown_tables
from src.core.common.utils import extract_content

logger = setup_logging(__file__)

_RENDER_TIMEOUT = httpx.Timeout(connect=15.0, read=1800.0, write=60.0, pool=15.0)


async def generate_markdown_from_report(report_id: str,cards: List[Dict[str, Any]]) -> str:
    """
    Generate markdown content from a report's cards.
    
    Args:
        report_id: The ID of the report to generate markdown for
        generate_markdown_from_report
    Returns:
        str: The generated markdown content
    """
    markdown_content = ""
    

    if not cards:
        logger.error(f"No cards found for report ID: {report_id}")
        return ""
    
    report_cards = []
    for card in cards:
        if card.get('type') == 'title':
            report_cards.append({
                "section": [
                    {
                        "name": "title",
                        "content": extract_content(card, "title"),
                        "tables": [{"visualization": "", "table_id": "", "table_title": "", "s3_uri": ""}]
                    }
                ],
                "sub_sections": [],
                "citations": {},
                "summary": ""
            })

        elif card.get('type') == 'subtitle':
            report_cards.append({
                "section": [
                    {
                        "name": "subtitle",
                        "content": extract_content(card, "content"),
                        "tables": [{"visualization": "", "table_id": "", "table_title": "", "s3_uri": ""}]
                    }
                ],
                "sub_sections": [],
                "citations": {},
                "summary": ""
            })

        elif card.get('type') == 'toc':
            report_cards.append({
                "section": [
                    {
                        "name": "table_of_contents",
                        "content": extract_content(card, "content"),
                        "tables": [{"visualization": "", "table_id": "", "table_title": "", "s3_uri": ""}]
                    }
                ],
                "sub_sections": [],
                "citations": {},
                "summary": ""
            })

        elif card.get('type') == 'es':
            report_cards.append({
                "section": [
                    {
                        "name": "executive_summary",
                        "content": extract_content(card, "content"),
                        "tables": [{"visualization": "", "table_id": "", "table_title": "", "s3_uri": ""}]
                    }
                ],
                "sub_sections": [],
                "citations": {},
                "summary": ""
            })

        elif card.get('type') == 'section':
            if card.get('section') and isinstance(card.get('section'), list):
                report_cards.append({
                    "section": card.get("section", []),
                    "sub_sections": card.get("sub_sections", []),
                    "citations": card.get("citations", {}),
                    "summary": card.get("summary", "")
                })
            else:
                report_cards.append({
                    "section": [
                        {
                            "name": card.get("title", ""),
                            "content": extract_content(card, "content"),
                            "tables": [{"visualization": "", "table_id": "", "table_title": "", "s3_uri": ""}]
                        }
                    ],
                    "sub_sections": card.get("sub_sections", []),
                    "citations": card.get("citations", {}),
                    "summary": card.get("summary", "")
                })
        else:
            logger.error(f"Unknown card type: {card.get('type')} for report_id: {report_id} at sequence: {card.get('sequence')}")
            continue
        
        # Convert the cards to markdown
        # markdown_content = convert_json_to_md(report_cards)
    markdown_content = await run_in_threadpool(convert_json_to_md, report_cards)
        
    return markdown_content


def _extract_table_markdown_lines(tables: list) -> List[str]:
    """Return non-empty table_markdown strings from a tables list."""
    lines = []
    for t in (tables or []):
        if isinstance(t, dict):
            md = (t.get("table_markdown") or "").strip()
            if md:
                lines.append(md)
    return lines


def _extract_inline_tables(content: Any) -> List[str]:
    """Extract inline markdown tables from a content field (str or dict).

    Falls back to this when table_markdown is NULL in the DB — tables may be
    embedded as pipe-delimited rows inside the prose content field.
    """
    if isinstance(content, dict):
        content = content.get("content") or ""
    if not isinstance(content, str) or not content.strip():
        return []
    tables = extract_markdown_tables(content)
    return [t.strip() for t in (tables or []) if t.strip()]


def _build_pptx_markdown_from_cards(cards: List[Dict[str, Any]]) -> str:
    """Build condensed markdown for PPTX generation.

    Uses each card's pre-computed ``summary`` field instead of full section
    prose, which significantly reduces the token count fed into the structurer,
    interpreter, and planner without losing the key facts those agents need.

    Structure per card:
      - Title card           → # <title>
      - Executive summary    → ## Executive Summary\\n<summary or content>
      - Section card         → ## <section_name>\\n<summary>
                               ### <sub_name> (heading only, no body)
                               <table_markdown for any tables>
      - Subtitle / TOC       → skipped (not useful for slides)

    Falls back to the first 400 chars of ``content`` when ``summary`` is empty.
    Tables are always included verbatim so the interpreter can assign chart types.
    """
    md_lines: List[str] = []
    section_counter = 0

    for card in cards:
        card_type = card.get("type", "")

        if card_type == "title":
            title = extract_content(card, "title")
            if title:
                md_lines.append(f"# {title}\n")

        elif card_type == "es":
            content = extract_content(card, "content")
            summary = (card.get("summary") or "").strip()
            body = summary or content[:500] if content else ""
            if body:
                md_lines.append(f"## Executive Summary\n\n{body}\n")

        elif card_type == "section":
            section_counter += 1

            # --- section heading ---
            section_list = card.get("section") or []
            section_name = ""
            section_tables: list = []
            if isinstance(section_list, list) and section_list:
                section_name = (section_list[0].get("name") or "").strip()
                section_tables = section_list[0].get("tables") or []

            if not section_name and card.get("title"):
                section_name = card["title"]

            heading = f"## {section_counter}. {section_name}" if section_name else f"## Section {section_counter}"
            md_lines.append(f"{heading}\n")

            # --- section body: summary preferred, brief content fallback ---
            summary = (card.get("summary") or "").strip()
            if not summary:
                # fallback: first 500 chars of section content
                raw_content = ""
                if isinstance(section_list, list) and section_list:
                    raw_content = (section_list[0].get("content") or "")
                    if isinstance(raw_content, dict):
                        raw_content = raw_content.get("content") or ""
                summary = raw_content[:500].strip()

            if summary:
                md_lines.append(f"{summary}\n")

            # Build a table_id → table_markdown lookup from section-level tables
            # (table_markdown lives on the ORM Table model, exposed via
            # card["section"][0]["tables"]; sub-sections only store table_id).
            tid_to_md: Dict[str, str] = {}
            for t in section_tables:
                if isinstance(t, dict):
                    tid = t.get("table_id") or ""
                    tmd = (t.get("table_markdown") or "").strip()
                    if tid and tmd:
                        tid_to_md[tid] = tmd

            # When table_markdown is NULL for all tables (older reports), fall
            # back to extracting inline tables directly from content prose.
            any_tmd = bool(tid_to_md)

            # --- sub-section names + their tables (resolved via table_id) ---
            emitted_inline: set = set()  # dedupe inline tables across sub-sections
            for sub_idx, sub in enumerate(card.get("sub_sections") or []):
                sub_name = (sub.get("name") or "").strip()
                if sub_name:
                    md_lines.append(f"### {section_counter}.{sub_idx + 1}. {sub_name}\n")

                sub_tables_added = False
                for t in (sub.get("tables") or []):
                    if not isinstance(t, dict):
                        continue
                    tmd = (t.get("table_markdown") or "").strip()
                    if not tmd:
                        tmd = tid_to_md.get(t.get("table_id") or "", "")
                    if tmd:
                        md_lines.append(f"{tmd}\n")
                        sub_tables_added = True

                # Inline fallback: pull pipe tables from the sub-section prose
                if not sub_tables_added and not any_tmd:
                    for tmd in _extract_inline_tables(sub.get("content") or ""):
                        if tmd not in emitted_inline:
                            md_lines.append(f"{tmd}\n")
                            emitted_inline.add(tmd)

            # --- section-level tables not already emitted under a sub-section ---
            emitted_tids = {
                t.get("table_id") or ""
                for sub in (card.get("sub_sections") or [])
                for t in (sub.get("tables") or [])
                if isinstance(t, dict)
            }
            section_tmd_added = False
            for t in section_tables:
                if not isinstance(t, dict):
                    continue
                tmd = (t.get("table_markdown") or "").strip()
                tid = t.get("table_id") or ""
                if tmd and tid not in emitted_tids:
                    md_lines.append(f"{tmd}\n")
                    section_tmd_added = True

            # Inline fallback from section content prose
            if not section_tmd_added and not any_tmd:
                sec_content = ""
                if isinstance(section_list, list) and section_list:
                    sec_content = section_list[0].get("content") or ""
                for tmd in _extract_inline_tables(sec_content):
                    if tmd not in emitted_inline:
                        md_lines.append(f"{tmd}\n")
                        emitted_inline.add(tmd)

        # subtitle and toc are intentionally skipped for PPTX

    return "\n".join(md_lines)


async def generate_pptx_markdown_from_report(
    report_id: str,
    cards: List[Dict[str, Any]],
) -> str:
    """Condensed markdown for PPTX generation.

    Replaces full section prose with pre-computed card summaries while keeping
    section headings (for TOC) and raw table markdown (for chart-type
    assignment in the interpreter).  Reduces structurer/interpreter/planner
    input tokens by ~4–5× on a typical study report.
    """
    if not cards:
        logger.error(f"No cards found for report ID: {report_id}")
        return ""

    markdown_content = await run_in_threadpool(_build_pptx_markdown_from_cards, cards)
    logger.info(
        f"generate_pptx_markdown_from_report: built condensed markdown "
        f"({len(markdown_content):,} chars) for report_id={report_id}"
    )
    return markdown_content


_BRIEF_ALIASES = frozenset({"brief", "overview"})
_STUDY_ALIASES = frozenset({"comprehensive study", "comprehensive", "study"})

_BRIEF_CONTENT_SLIDES = 4
_STUDY_CONTENT_SLIDES = 10


def content_slides_for_report(
    report_type: str | None,
    length: str | None = None,
) -> int:
    """
    Map report_type and length to the content slide target for PPTX generation.

    Priority:
      1. report_type 'brief'  → 4 content slides
      2. report_type 'study'  → 10 content slides
      3. report_type is None  → fall back to length:
           'comprehensive' / 'study' → 10 (study)
           anything else             → 4  (brief)

    Three structural slides (title, topics_to_cover, conclusion) are added on
    top by the pptx_generator pipeline.  The disclaimer page is appended
    separately and is not counted here.
    """
    if report_type:
        normalized_type = report_type.strip().lower().replace("_", " ")
        if normalized_type in _BRIEF_ALIASES:
            return _BRIEF_CONTENT_SLIDES
        if normalized_type in _STUDY_ALIASES:
            return _STUDY_CONTENT_SLIDES

    # report_type absent — fall back to length field
    normalized_length = (length or "").strip().lower().replace("_", " ")
    if normalized_length in _STUDY_ALIASES:
        return _STUDY_CONTENT_SLIDES
    return _BRIEF_CONTENT_SLIDES


def content_slides_for_report_length(report_length: str | None) -> int:
    """Deprecated shim — prefer content_slides_for_report()."""
    return content_slides_for_report(report_type=None, length=report_length)



async def generate_pptx_and_upload_s3(
    md_content: str,
    total_slides: int,
    s3_key: str,
    report_id: str,
    brand_colors: Optional[List[str]] = None,
    generation_mode: str = "template",
    user_id: Optional[str] = None,
    chat_id: Optional[str] = None,
    report_version_id: Optional[str] = None,
) -> str:
    """Same signature/contract as the original: builds a PPTX from markdown,
    appends the disclaimer slide, uploads to S3, returns the S3 URI. The
    actual work now happens in report-render-service — see that service's
    README for why (LibreOffice/pptxgenjs/Playwright toolchain)."""
    payload = {
        "md_content": md_content,
        "total_slides": total_slides,
        "s3_key": s3_key,
        "report_id": report_id,
        "brand_colors": brand_colors,
        "generation_mode": generation_mode,
        "user_id": user_id,
        "chat_id": chat_id,
        "report_version_id": report_version_id,
    }
    async with httpx.AsyncClient(timeout=_RENDER_TIMEOUT) as client:
        resp = await client.post(
            f"{REPORT_RENDER_SERVICE_BASE_URL}/internal/render/presentation",
            json=payload,
        )
    resp.raise_for_status()
    return resp.json()["s3_uri"]


def generate_pptx_deck_local(*args, **kwargs):
    """Not shimmed: imported by api.py but never actually called there in
    the monolith (dead import, verified by grep). Kept as a loud stub so the
    import succeeds but a stray call fails clearly instead of doing nothing.
    The real implementation lives in report-render-service's pptx_utils.py
    if you need to wire this up later."""
    raise NotImplementedError(
        "generate_pptx_deck_local has no HTTP shim — it was an unused "
        "import in the monolith. See report-render-service/src/core/pptx_utils.py "
        "for the real implementation if you need to expose it."
    )
