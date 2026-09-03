"""
Global domain planner: shared data structures and utilities that every domain
subgraph reuses.  When a new domain is added (e.g. Due Diligence), it
instantiates its own ``DomainConfig`` and calls the helpers here -- nothing in
the main graph or in other domains needs to change.
"""

from __future__ import annotations

import asyncio
import datetime
import json as _json
import os
import tempfile
import uuid
from dataclasses import dataclass, field
from typing import Any, List, Optional, TypedDict

from langchain_core.messages import AIMessage
from langgraph.config import get_stream_writer

from app.core.constants import S3_REPORTS_BASE_PATH
from app.core.logging import setup_logging
from src.core.cards.card_fixer import fix_card
from app.observability.web_search_analytics import (
    enrich_terminal_search_analytics,
    logical_card_id,
    log_scheduled_analytics_batch,
    search_analytics_schedule_kwargs,
)
from src.db.web_search_db import log_web_search_event
from src.core.cards.card_utils import (
    add_viz_to_card,
    clean_drl,
    clean_drl_to_clean_rl,
    convert_json_to_md,
    extract_title_and_toc,
    gather_context_from_uploaded_file,
    generate_brief_cards,
    generate_cards,
    generate_cumulative_summary,
    generate_drl,
    generate_section_summary,
    modify_card,
    modify_report_layout,
    refine_cumulative_summary,
    remove_citations_from_DRL,
    replace_brief_citations,
    replace_citations,
    update_summaries_for_ask_caspr,
)

logger = setup_logging(__name__)

# ---------------------------------------------------------------------------
# Shared state that every domain subgraph can extend
# ---------------------------------------------------------------------------

class BaseDomainState(TypedDict, total=False):
    """Fields that EVERY domain subgraph must carry.

    Domain-specific subgraphs should create their own TypedDict that includes
    these keys plus any extras (e.g. ``document_analysis`` for Primary Research).
    """
    upload_file_config: dict
    grep_session: Any
    user_instructions: str
    report_layout: str
    report_length: str
    report_type: str  # 'study' or 'brief'
    report_title: str
    report_language: str
    user_name: str
    chat_id: str
    user_id: str
    web_search: bool
    s3_instance: Any
    mcp_server_url: str

    descriptive_report_layout: list
    cleaned_report_layout: str
    cumulative_summary: str
    all_report_citations: list
    table_and_table_id_map: dict
    final_message: Optional[AIMessage]
    error: Optional[str]


# ---------------------------------------------------------------------------
# Domain configuration -- one instance per domain
# ---------------------------------------------------------------------------

@dataclass
class DomainConfig:
    """Declarative configuration for a single report domain.

    Every domain creates one of these and passes it to the shared pipeline
    helpers.  The planner utilities read the flags / prompt overrides and
    adjust their behaviour accordingly.
    """
    domain_name: str
    display_name: str

    web_search_enabled: bool = False
    require_uploaded_file: bool = False

    max_sections_overview: int = 0
    max_sections_comprehensive: int = 0

    table_ratio_target: str = "50-60%"
    tone: str = "Professional analytical"
    citation_style: str = "document"

    source_rules_block: str = ""
    table_rules_block: str = ""
    formatting_rules_block: str = ""
    citation_rules_block: str = ""
    drl_addendum: str = ""

    extra_prompt_blocks: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# S3 metadata upload helpers (mirrors Casper._upload_* for domain subgraphs)
# ---------------------------------------------------------------------------

def _s3_metadata_prefix(user_name: str, chat_id: str) -> str:
    now = datetime.datetime.now()
    return (
        f"{S3_REPORTS_BASE_PATH}/caspr_metadata_reports/"
        f"{now.strftime('%Y')}/{now.strftime('%m')}/{now.strftime('%d')}/"
        f"{user_name}/chat_{chat_id}"
    )


async def _upload_report_layout_to_s3(
    s3_instance, report_layout, filename: str,
    user_name: str, chat_id: str,
) -> None:
    try:
        with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False, encoding='utf-8') as tmp:
            if isinstance(report_layout, str):
                tmp.write(report_layout)
            else:
                _json.dump(report_layout, tmp, indent=2, ensure_ascii=False)
            temp_path = tmp.name

        s3_key = f"{_s3_metadata_prefix(user_name, chat_id)}/Report_layout/{filename}"
        try:
            logger.info(f"[domain] Uploading report layout to S3: {s3_key}")
            s3_path = await asyncio.to_thread(s3_instance.upload_file, temp_path, s3_key)
            if s3_path:
                logger.info(f"[domain] Uploaded report layout to S3: {s3_path}")
            else:
                logger.error(f"[domain] Failed to upload report layout to S3")
        except Exception as e:
            logger.error(f"[domain] Error uploading report layout to S3: {e}")
        finally:
            try:
                os.unlink(temp_path)
            except Exception:
                pass
    except Exception as e:
        logger.error(f"[domain] Error in report layout S3 upload task: {e}")


async def _upload_descriptive_layout_to_s3(
    s3_instance, descriptive_layout: list | dict, filename: str,
    user_name: str, chat_id: str,
) -> None:
    try:
        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False, encoding='utf-8') as tmp:
            _json.dump(descriptive_layout, tmp, indent=2, ensure_ascii=False)
            temp_path = tmp.name

        s3_key = f"{_s3_metadata_prefix(user_name, chat_id)}/Descriptive_report_layout/{filename}"
        try:
            logger.info(f"[domain] Uploading descriptive layout to S3: {s3_key}")
            s3_path = await asyncio.to_thread(s3_instance.upload_file, temp_path, s3_key)
            if s3_path:
                logger.info(f"[domain] Uploaded descriptive layout to S3: {s3_path}")
            else:
                logger.error(f"[domain] Failed to upload descriptive layout to S3")
        except Exception as e:
            logger.error(f"[domain] Error uploading descriptive layout to S3: {e}")
        finally:
            try:
                os.unlink(temp_path)
            except Exception:
                pass
    except Exception as e:
        logger.error(f"[domain] Error in descriptive layout S3 upload task: {e}")


async def _upload_card_to_s3(
    s3_instance, card_with_citations: list | dict, card_name: str,
    user_name: str, chat_id: str,
) -> None:
    try:
        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False, encoding='utf-8') as tmp:
            _json.dump(card_with_citations, tmp, indent=2, ensure_ascii=False)
            temp_path = tmp.name

        s3_key = f"{_s3_metadata_prefix(user_name, chat_id)}/Cards/{card_name}.json"
        try:
            await asyncio.to_thread(s3_instance.upload_file, temp_path, s3_key)
            logger.info(f"[domain] Uploaded card to S3: {s3_key}")
        except Exception as e:
            logger.error(f"[domain] Error uploading card to S3: {e}")
        finally:
            try:
                os.unlink(temp_path)
            except Exception:
                pass
    except Exception as e:
        logger.error(f"[domain] Error in card S3 upload task: {e}")


async def _upload_fixed_card_to_s3(
    s3_instance, card: dict, card_name: str,
    user_name: str, chat_id: str,
) -> None:
    try:
        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False, encoding='utf-8') as tmp:
            _json.dump(card, tmp, indent=2, ensure_ascii=False)
            temp_path = tmp.name

        s3_key = f"{_s3_metadata_prefix(user_name, chat_id)}/Cards/LLM_Fixed_Cards/{card_name}.json"
        try:
            await asyncio.to_thread(s3_instance.upload_file, temp_path, s3_key)
            logger.info(f"[domain] Uploaded fixed card to S3: {s3_key}")
        except Exception as e:
            logger.error(f"[domain] Error uploading fixed card to S3: {e}")
        finally:
            try:
                os.unlink(temp_path)
            except Exception:
                pass
    except Exception as e:
        logger.error(f"[domain] Error in fixed card S3 upload task: {e}")


async def _upload_cards_for_db_to_s3(
    s3_instance, cards_for_db: list, table_and_table_id_map: dict,
    user_name: str, chat_id: str,
) -> None:
    file_path = None
    try:
        file_name = f"{user_name}_chat_{chat_id}_cards_for_db.json"
        file_path = os.path.join(os.getcwd(), "temp", file_name)
        payload = cards_for_db + [table_and_table_id_map]
        os.makedirs(os.path.dirname(file_path), exist_ok=True)
        with open(file_path, "w") as f:
            _json.dump(payload, f, indent=2, ensure_ascii=False)

        s3_key = f"{_s3_metadata_prefix(user_name, chat_id)}/Cards/Report_Card_Json/{file_name}"
        await asyncio.to_thread(s3_instance.upload_file, file_path, s3_key)
        logger.info(f"[domain] cards_for_db uploaded to S3: {s3_key} | user: {user_name} chat: {chat_id}")
    except Exception as e:
        logger.error(f"[domain] Error uploading cards_for_db to S3: {e} | user: {user_name} chat: {chat_id}")
    finally:
        if file_path and os.path.exists(file_path):
            try:
                os.remove(file_path)
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Planner helpers -- used by every domain subgraph
# ---------------------------------------------------------------------------

async def plan_report_layout(
    report_layout: str,
    user_instructions: str,
    domain_config: DomainConfig,
    s3_instance=None,
    user_name: str = "",
    chat_id: str = "",
    user_id: str = "",
    report_length: str = "OVERVIEW",
) -> tuple[list, str]:
    """Generate the DRL and cleaned report layout.

    Appends the domain's ``drl_addendum`` to the user instructions before
    calling ``generate_drl``, then cleans the result.

    Returns:
        (descriptive_report_layout, cleaned_report_layout)
    """
    logger.info(
        f"[{domain_config.domain_name}] plan_report_layout started | "
        f"domain='{domain_config.display_name}' | "
        f"has_drl_addendum={bool(domain_config.drl_addendum)} | "
        f"layout_len={len(report_layout)} | instructions_len={len(user_instructions)}"
    )

    augmented_instructions = user_instructions
    if domain_config.drl_addendum:
        augmented_instructions = f"{user_instructions}\n\n{domain_config.drl_addendum}"

    _uid = user_id or user_name
    drl = await asyncio.to_thread(
        generate_drl, report_layout, augmented_instructions, report_length,
        chat_id=chat_id or None, user_id=_uid or None,
    )
    if not drl:
        raise ValueError("Failed to generate descriptive report layout")

    logger.info(
        f"[{domain_config.domain_name}] DRL generated with {len(drl)} sections — cleaning and removing citations"
    )

    drl = await asyncio.to_thread(clean_drl, drl)
    drl = await asyncio.to_thread(remove_citations_from_DRL, drl)
    if not drl:
        raise ValueError("DRL was empty after cleaning")

    cleaned_rl = await asyncio.to_thread(clean_drl_to_clean_rl, drl)
    if not cleaned_rl:
        raise ValueError("Failed to derive cleaned report layout from DRL")
    cleaned_rl = await asyncio.to_thread(modify_report_layout, cleaned_rl)

    if s3_instance:
        ts = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
        asyncio.create_task(_upload_report_layout_to_s3(
            s3_instance, cleaned_rl,
            f"{domain_config.domain_name}_cleaned_report_layout_{ts}.txt",
            user_name, chat_id,
        ))
        asyncio.create_task(_upload_descriptive_layout_to_s3(
            s3_instance, drl,
            f"{domain_config.domain_name}_descriptive_report_layout_{ts}.json",
            user_name, chat_id,
        ))

    logger.info(
        f"[{domain_config.domain_name}] plan_report_layout completed | "
        f"drl_sections={len(drl)} | cleaned_layout_len={len(cleaned_rl)}"
    )

    return drl, cleaned_rl


def _drl_heartbeat_lines(section: dict) -> List[str]:
    """Return the DRL-authored ``heartbeat`` lines for a section being generated.

    Heartbeats now live only at the section level (the section's 5 lines are
    authored to cover the whole section, including all of its subsections), so
    we simply surface those. Sub-section ``heartbeat`` lines are still appended
    if present, for backward compatibility with older DRLs.
    """
    lines: List[str] = [str(line) for line in section.get("heartbeat", []) if line]
    for sub in section.get("sub_sections", []) or []:
        lines.extend(str(line) for line in sub.get("heartbeat", []) if line)
    return lines


async def _run_card_heartbeat(
    section_name: str,
    event_writer,
    heartbeat_lines: Optional[List[str]] = None,
    interval: float = 5.0,
) -> None:
    """Emit the DRL-authored heartbeat lines for a section while its card is generated.

    Generating a section's card is a single long, blocking LLM call, so there is
    no natural progress signal to surface to the frontend.  This coroutine runs
    alongside that call: it steps through the DRL's own ``heartbeat`` lines for
    this specific section (and its sub-sections, flattened via
    ``_drl_heartbeat_lines``), emitting the next one every ``interval`` seconds
    so the user sees concrete, topic-specific progress instead of a blank wait.

    The first line is emitted immediately, then subsequent lines advance on the
    timer. Each line is emitted exactly once (no duplicates and no cycling back
    to the start); once the lines are exhausted the task stays alive silently
    (no further emits) until the caller cancels it (i.e. when the card is ready).
    """
    lines = [line for line in (heartbeat_lines or []) if line]
    if not lines:
        return
    # Emit each line exactly once (no duplicates, no cycling back to the start),
    # advancing on the timer. Once every line has been sent, keep the task alive
    # silently (no further emits) until the caller cancels it when the card is ready.
    for line in lines:
        event_writer({
            "name": "generate_report",
            "status": "card_generation_heartbeat",
            "section": section_name,
            "step": line,
        })
        await asyncio.sleep(interval)
    while True:
        await asyncio.sleep(interval)


async def generate_section_cards(
    descriptive_report_layout: list,
    user_instructions: str,
    upload_file_config: Optional[dict],
    report_length: str,
    web_search: bool,
    user_name: str,
    chat_id: str,
    domain_config: DomainConfig,
    event_writer=None,
    s3_instance=None,
    card_generator_async=None,
    external_tools: list = None,
    grep_session=None,
    report_type: str = "study",
    user_id: str = "",
) -> tuple[list, str, list, dict]:
    """Run the card-generation loop for all sections.

    This is the domain-aware wrapper around the per-section ``generate_cards``
    call, citation processing, summary accumulation, etc.

    When ``report_type`` is ``'brief'``, runs ``gather_context_from_uploaded_file``
    (grep enrichment) on the DRL first, then uses ``generate_brief_cards``
    instead of the standard per-section loop.

    Args:
        card_generator_async: Optional async callable with the same signature
            as ``generate_cards``.  When provided the loop ``await``-s it
            directly instead of running the default sync ``generate_cards``
            inside ``asyncio.to_thread``.  Used by the Due Diligence domain
            to inject ``run_dd_orchestrator`` as a tool.
        report_type: 'study' (default) or 'brief'. When 'brief', uses the
            brief card generation pipeline with grep context enrichment.

    Returns:
        (cards_for_db, cumulative_summary, all_report_citations,
         table_and_table_id_map)
    """
    if event_writer is None:
        event_writer = get_stream_writer()

    effective_web_search = web_search and domain_config.web_search_enabled
    _uid = user_id or user_name

    logger.info(
        f"[{domain_config.domain_name}] generate_section_cards started | "
        f"web_search_requested={web_search} | web_search_enabled={domain_config.web_search_enabled} | "
        f"effective_web_search={effective_web_search} | "
        f"report_length='{report_length}' | has_upload_config={bool(upload_file_config)} | "
        f"drl_sections={len(descriptive_report_layout)} | "
        f"user: {user_name} chat: {chat_id}"
    )

    table_of_contents, title, subtitle = await asyncio.to_thread(
        extract_title_and_toc, descriptive_report_layout
    )

    event_writer({"name": "retrieve", "status": "card_stream_start", "title": title})

    cards_for_db: list = []
    _append_meta_card(cards_for_db, "title", title, event_writer)
    _append_meta_card(cards_for_db, "subtitle", subtitle, event_writer)
    _append_meta_card(cards_for_db, "table_of_contents", table_of_contents, event_writer)

    all_report_citations: list = []
    citation_url_map: dict = {}
    # cumulative_summary = ""  # PAUSED: Using full previous cards as context instead
    table_and_table_id_map: dict = {}
    sections = descriptive_report_layout[1:]  # skip title section
    report_length_upper = report_length.upper().strip()

    if report_type.lower() == 'brief':
        logger.info(
            f"[{domain_config.domain_name}] Brief report mode — using gather_context_from_uploaded_file + generate_brief_cards | "
            f"user: {user_name} chat: {chat_id}"
        )
        if grep_session:
            sections = await asyncio.to_thread(
                gather_context_from_uploaded_file, sections, grep_session, user_instructions
            )

        # Web-search analytics for brief reports: `generate_brief_cards` makes a
        # single provider call for the *entire* report (all sections at once),
        # unlike the per-section study path below. `analytics_collector` therefore
        # holds at most one entry, persisted once after the loop below finishes
        # (see the scheduling block after `return cards_for_db, ...` is reached).
        analytics_collector: list[dict] = []
        analytics_operation_id = str(uuid.uuid4())

        brief_gen = generate_brief_cards(
            drl=sections, user_instructions=user_instructions,
            chat_id=chat_id or None, user_id=_uid or None,
            analytics_collector=analytics_collector,
            analytics_operation_id=analytics_operation_id,
        )
        idx = 0
        while True:
            # Brief cards stream in DRL order, so map the card about to be
            # produced to its DRL section (best-effort) and surface that
            # section's own heartbeat lines while the model generates it.
            _next_section = sections[idx] if idx < len(sections) else {}
            _hb_task = asyncio.create_task(
                _run_card_heartbeat(
                    _next_section.get("section", f"Section {idx + 1}"),
                    event_writer,
                    heartbeat_lines=_drl_heartbeat_lines(_next_section),
                )
            )
            try:
                raw_card = await asyncio.to_thread(lambda: next(brief_gen, None))
            finally:
                _hb_task.cancel()
                try:
                    await _hb_task
                except asyncio.CancelledError:
                    pass
            if raw_card is None:
                break
            idx += 1
            try:
                logger.info(
                    f"[{domain_config.domain_name}][brief] Processing card {idx}: "
                    f"{raw_card.get('section', 'unknown')} | user: {user_name} chat: {chat_id}"
                )

                brief_section_name = raw_card.get("section", f"Section {idx}")

                def _post_step(step: str):
                    event_writer({
                        "name": "generate_report",
                        "status": "card_generation_heartbeat",
                        "section": brief_section_name,
                        "step": step,
                    })

                _post_step("Linking sources…")
                card = replace_brief_citations(raw_card, citation_url_map, all_report_citations)

                _post_step("Summarizing section…")
                card_summary = await asyncio.to_thread(
                    generate_section_summary, card, chat_id=chat_id or None, user_id=_uid or None,
                )
                card["summary"] = card_summary

                _post_step("Building charts…")
                modified_card = await asyncio.to_thread(modify_card, card)
                card, tmap = await asyncio.to_thread(
                    add_viz_to_card, modified_card, user_name, chat_id,
                    report_type=report_type, user_id=_uid or None,
                )
                table_and_table_id_map.update(tmap)
                card = await asyncio.to_thread(update_summaries_for_ask_caspr, card)

                for part in [card["section"][0]] + card.get("sub_sections", []):
                    part["content"] = (
                        part["content"]
                        .replace("?utm_source=openai", "")
                        .replace("&utm_source=openai", "")
                    )

                cards_for_db.append(card)
                event_writer({"name": "generate_report", "card_db": card, "card_type": "section"})
            except Exception as e:
                logger.error(
                    f"[{domain_config.domain_name}][brief] Error processing card {idx}: {str(e)} | "
                    f"user: {user_name} chat: {chat_id}"
                )
                cards_for_db.append(_empty_card(raw_card.get('section', f'Section {idx}')))
                event_writer({
                    "name": "generate_report",
                    "card_db": cards_for_db[-1],
                    "card_type": "section",
                })

        event_writer({"name": "generate_report", "report_citations": citation_url_map})
        cumulative_summary = "\n".join(
            card.get("summary", "") for card in cards_for_db if card.get("summary")
        )
        event_writer({"name": "generate_report", "report_summary": cumulative_summary})

        # Persist web-search analytics for the whole brief report now that every
        # section card has been emitted into `cards_for_db`. Enrichment scans the
        # full, final content for URLs actually retained in output, then schedules
        # the single collected entry (if any) for a non-blocking DB write via
        # `asyncio.create_task`, mirroring the study-report path below. All steps
        # are wrapped so analytics can never affect report generation.
        try:
            if analytics_collector:
                enrich_terminal_search_analytics(analytics_collector, cards_for_db)
                log_scheduled_analytics_batch(
                    "card_generation",
                    analytics_collector,
                    operation_id=analytics_operation_id,
                    section_name="brief_report",
                )
                for analytics_entry in analytics_collector:
                    try:
                        event = search_analytics_schedule_kwargs(analytics_entry)
                        event.update(
                            {
                                "trigger_source": "card_generation",
                                "user_query": user_instructions,
                                "user_id": user_id,
                                "chat_id": chat_id or None,
                                "section_name": "brief_report",
                            }
                        )
                        asyncio.create_task(log_web_search_event(**event))
                    except Exception:
                        logger.exception(
                            f"[{domain_config.domain_name}][brief] Failed to schedule "
                            "non-fatal search analytics"
                        )
        except Exception:
            logger.exception(
                f"[{domain_config.domain_name}][brief] Failed to enrich non-fatal "
                "terminal search analytics"
            )

        logger.info(
            f"[{domain_config.domain_name}] generate_section_cards (brief) completed | "
            f"total_cards={len(cards_for_db)} | total_citations={len(all_report_citations)} | "
            f"user: {user_name} chat: {chat_id}"
        )
        return cards_for_db, cumulative_summary, all_report_citations, table_and_table_id_map

    for idx, section in enumerate(sections):
        section_name = section.get("section", f"Section {idx + 1}")
        analytics_collector: list[dict] = []
        analytics_operation_id = str(uuid.uuid4())
        analytics_scheduled = False
        card = None

        def _schedule_collected_analytics(rendered_card=None):
            nonlocal analytics_scheduled
            if analytics_scheduled:
                return
            analytics_scheduled = True
            if rendered_card is not None:
                try:
                    enrich_terminal_search_analytics(
                        analytics_collector,
                        rendered_card,
                    )
                except Exception:
                    logger.exception(
                        "[generate_section_cards] Failed to enrich non-fatal "
                        "terminal search analytics"
                    )
            card_id = logical_card_id(rendered_card)
            log_scheduled_analytics_batch(
                "card_generation",
                analytics_collector,
                operation_id=analytics_operation_id,
                section_name=section_name,
                card_id=card_id,
            )
            for analytics_entry in analytics_collector:
                try:
                    event = search_analytics_schedule_kwargs(analytics_entry)
                    event.update(
                        {
                            "trigger_source": "card_generation",
                            "user_query": section.get("section", section_name),
                            "user_id": user_id,
                            "chat_id": chat_id or None,
                            "section_name": section_name,
                            "card_id": card_id,
                        }
                    )
                    asyncio.create_task(log_web_search_event(**event))
                except Exception:
                    logger.exception(
                        "[generate_section_cards] Failed to schedule "
                        "non-fatal search analytics"
                    )

        try:
            logger.info(
                f"[{domain_config.domain_name}] Processing section {idx+1}/{len(sections)}: "
                f"{section.get('section', '?')} | user: {user_name} chat: {chat_id}"
            )

            _hb_task = asyncio.create_task(
                _run_card_heartbeat(
                    section_name, event_writer,
                    heartbeat_lines=_drl_heartbeat_lines(section),
                )
            )
            try:
                if card_generator_async is not None:
                    card, card_citations = await card_generator_async(
                        section,
                        "",  # cumulative_summary param kept for signature compat but unused
                        is_first_section=(idx == 0),
                        descriptive_report_layout=sections,
                        user_instructions=user_instructions,
                        report_length=report_length_upper,
                        upload_file_config=upload_file_config,
                        web_search=effective_web_search,
                        external_tools=external_tools,
                        previous_cards=cards_for_db,
                        grep_session=grep_session,
                        chat_id=chat_id or None,
                        user_id=_uid or None,
                    )
                else:
                    card, card_citations = await asyncio.to_thread(
                        generate_cards,
                        section,
                        "",  # cumulative_summary param kept for signature compat but unused
                        is_first_section=(idx == 0),
                        descriptive_report_layout=sections,
                        user_instructions=user_instructions,
                        report_length=report_length_upper,
                        upload_file_config=upload_file_config,
                        web_search=effective_web_search,
                        external_tools=external_tools,
                        previous_cards=cards_for_db,
                        grep_session=grep_session,
                        chat_id=chat_id or None,
                        user_id=_uid or None,
                        analytics_collector=analytics_collector,
                        analytics_operation_id=analytics_operation_id,
                    )
            finally:
                _hb_task.cancel()
                try:
                    await _hb_task
                except asyncio.CancelledError:
                    pass

            if s3_instance:
                card_name = f"{card.get('section', f'section_{idx}')}"
                asyncio.create_task(_upload_card_to_s3(
                    s3_instance, [card, card_citations], card_name,
                    user_name, chat_id,
                ))

            def _post_step(step: str):
                event_writer({
                    "name": "generate_report",
                    "status": "card_generation_heartbeat",
                    "section": section_name,
                    "step": step,
                })

            _post_step("Linking sources…")
            card = fix_card(card, chat_id=chat_id or None, user_id=_uid or None)

            if s3_instance:
                asyncio.create_task(_upload_fixed_card_to_s3(
                    s3_instance, card, card_name,
                    user_name, chat_id,
                ))

            card_citations = _normalise_citations(card_citations, user_name, chat_id)

            processed_citations, citation_indices = _process_citation_indices(
                card_citations, all_report_citations, citation_url_map
            )

            card["content"] = await asyncio.to_thread(
                replace_citations,
                card["content"],
                citation_indices,
                processed_citations,
                citation_url_map,
                all_report_citations,
            )
            for i, sub in enumerate(card.get("sub_sections", [])):
                card["sub_sections"][i]["content"] = await asyncio.to_thread(
                    replace_citations,
                    sub["content"],
                    citation_indices,
                    processed_citations,
                    citation_url_map,
                    all_report_citations,
                )

            _post_step("Summarizing section…")
            card_summary = await asyncio.to_thread(
                generate_section_summary, card, chat_id=chat_id or None, user_id=_uid or None,
            )
            card["citations"] = {
                url: citation_url_map.get(url, 0) for url in processed_citations if url in citation_url_map
            }
            card["summary"] = card_summary

            _post_step("Building charts…")
            modified_card = await asyncio.to_thread(modify_card, card)
            card, tmap = await asyncio.to_thread(
                add_viz_to_card, modified_card, user_name, chat_id,
                report_type=report_type, user_id=_uid or None,
            )
            table_and_table_id_map.update(tmap)
            card = await asyncio.to_thread(update_summaries_for_ask_caspr, card)

            for part in [card["section"][0]] + card.get("sub_sections", []):
                part["content"] = (
                    part["content"]
                    .replace("?utm_source=openai", "")
                    .replace("&utm_source=openai", "")
                )

            _schedule_collected_analytics(card)
            cards_for_db.append(card)
            event_writer({"name": "generate_report", "card_db": card, "card_type": "section"})

            # PAUSED: Cumulative summary generation - using full previous cards as context instead
            # if idx == 0:
            #     cumulative_summary = card_summary
            # else:
            #     cumulative_summary = await asyncio.to_thread(
            #         generate_cumulative_summary, cumulative_summary, card_summary
            #     )

        except Exception as exc:
            _schedule_collected_analytics()
            logger.error(
                f"[{domain_config.domain_name}] Error on section {idx+1}: {exc} | "
                f"user: {user_name} chat: {chat_id}"
            )
            cards_for_db.append(_empty_card(section.get("section", "Unknown")))
            event_writer({
                "name": "generate_report",
                "card_db": cards_for_db[-1],
                "card_type": "section",
            })

    event_writer({"name": "generate_report", "report_citations": citation_url_map})
    # Build cumulative_summary from individual card summaries for executive summary generation
    cumulative_summary = "\n".join(
        card.get("summary", "") for card in cards_for_db if card.get("summary")
    )
    event_writer({"name": "generate_report", "report_summary": cumulative_summary})

    logger.info(
        f"[{domain_config.domain_name}] generate_section_cards completed | "
        f"total_cards={len(cards_for_db)} | total_citations={len(all_report_citations)} | "
        f"tables_generated={len(table_and_table_id_map)} | "
        f"summary_len={len(cumulative_summary)} | "
        f"user: {user_name} chat: {chat_id}"
    )

    return cards_for_db, cumulative_summary, all_report_citations, table_and_table_id_map


async def synthesize_report(
    cards_for_db: list,
    cumulative_summary: str,
    table_and_table_id_map: dict,
    report_title: str,
    user_name: str,
    chat_id: str,
    domain_config: DomainConfig,
    event_writer=None,
    s3_instance=None,
    user_id: str = "",
) -> AIMessage:
    """Produce executive summary, assemble markdown, return final AIMessage."""
    if event_writer is None:
        event_writer = get_stream_writer()

    _uid = user_id or user_name

    logger.info(
        f"[{domain_config.domain_name}] synthesize_report started | "
        f"report_title='{report_title}' | cards_count={len(cards_for_db)} | "
        f"has_cumulative_summary={bool(cumulative_summary)} | "
        f"tables_count={len(table_and_table_id_map)} | "
        f"user: {user_name} chat: {chat_id}"
    )

    executive_summary = None
    if cumulative_summary:
        try:
            # Old approach: refine_cumulative_summary(cumulative_summary)
            # New approach: pass cards_for_db, function extracts summaries internally
            executive_summary = await asyncio.to_thread(
                refine_cumulative_summary, cards_for_db,
                chat_id=chat_id or None, user_id=_uid or None,
            )
        except Exception as exc:
            logger.error(f"[{domain_config.domain_name}] Executive summary failed: {exc}")

    if executive_summary:
        es_card = _make_meta_card_dict("executive_summary", executive_summary)
        cards_for_db.insert(3, es_card)
        event_writer({
            "name": "generate_report",
            "card_db": {"section": [{"name": "executive_summary", "content": executive_summary}]},
            "card_type": "es",
        })

    event_writer({"name": "generate_report", "table_and_table_id_map": table_and_table_id_map})
    event_writer({"name": "generate_report", "status": "card_stream_complete"})

    md_content = await asyncio.to_thread(convert_json_to_md, cards_for_db, table_and_table_id_map)
    event_writer({"name": "generate_report", "status": "md_content", "md_content": md_content})

    if s3_instance:
        asyncio.create_task(_upload_cards_for_db_to_s3(
            s3_instance, cards_for_db, table_and_table_id_map,
            user_name, chat_id,
        ))

    logger.info(
        f"[{domain_config.domain_name}] synthesize_report completed | "
        f"has_executive_summary={executive_summary is not None} | "
        f"markdown_length={len(md_content)} chars | "
        f"user: {user_name} chat: {chat_id}"
    )

    return AIMessage(content=md_content)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _append_meta_card(cards: list, name: str, content: str, event_writer) -> None:
    card = _make_meta_card_dict(name, content)
    cards.append(card)
    event_writer({
        "name": "generate_report",
        "card_db": card,
        "card_type": name if name != "table_of_contents" else "toc",
    })

def _make_meta_card_dict(name: str, content: str) -> dict:
    return {
        "section": [{
            "name": name,
            "content": content,
            "tables": [{"visualization": "", "table_id": "", "table_title": "", "visualization_type": ""}],
            "id": str(uuid.uuid4()),
            "summary": "",
        }],
        "sub_sections": [{
            "name": "",
            "content": "",
            "tables": [{"visualization": "", "table_id": "", "table_title": "", "visualization_type": ""}],
            "id": str(uuid.uuid4()),
            "summary": "",
        }],
        "citations": {},
        "summary": "",
    }


def _empty_card(section_name: str) -> dict:
    return _make_meta_card_dict(section_name, "")


def _normalise_citations(
    raw_citations: list | None,
    user_name: str,
    chat_id: str,
) -> list:
    """Turn the heterogeneous citation list from ``generate_cards`` into a
    flat list of URL strings (or empty list)."""
    if not raw_citations:
        return []

    if isinstance(raw_citations[0], dict):
        urls = []
        for c in raw_citations:
            url = c.get("url")
            if url:
                from src.core.cards.card_utils import clean_url
                try:
                    urls.append(clean_url(url))
                except Exception:
                    urls.append(url.replace("?utm_source=openai", "").replace("&utm_source=openai", ""))
        return urls

    return list(raw_citations)


def _process_citation_indices(
    card_citations: list,
    all_report_citations: list,
    citation_url_map: dict,
) -> tuple[list, list]:
    processed: list = []
    indices: list = []
    for url in card_citations:
        if url in citation_url_map:
            indices.append(citation_url_map[url])
        else:
            all_report_citations.append(url)
            citation_url_map[url] = len(all_report_citations)
            indices.append(len(all_report_citations))
        processed.append(url)
    return processed, indices
