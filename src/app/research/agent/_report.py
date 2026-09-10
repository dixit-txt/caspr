"""Report generation: layout proposal/refresh, report-config pause, card pipeline, generate_report."""

import asyncio
import datetime
import json
import os
import tempfile
import time
from typing import Any

# from langchain_aws import ChatBedrock  # Bedrock disabled — using direct Anthropic API instead
from langchain_core.messages import (
    AIMessage,
    ToolMessage,
)
from langgraph.config import get_stream_writer

# from langchain_core.messages.base import BaseMessage
# from langchain_tavily import TavilySearch
from langgraph.graph import MessagesState
from langgraph.types import interrupt
from uuid_utils import uuid7

from app.admin.repository_web_search import log_web_search_event
from app.cards.service_cards import (
    _updated_layout_to_markdown,
    add_viz_to_card,
    clean_url,
    convert_json_to_md,
    extract_title_and_toc,
    gather_context_from_uploaded_file,
    generate_brief_cards,
    generate_cards,
    generate_section_summary,
    modify_card,
    modify_report_layout,
    parse_markdown_report_layout,
    refine_cumulative_summary,
    replace_brief_citations,
    update_summaries_for_ask_caspr,
)

# from app.research.infographics import process_infographics
# from app.deliverables.helper_functions import generate_image_with_Google, generate_image_with_openai
from app.cards.service_fixer import fix_card
from app.core.constants import (
    ANALYST_REASONING_ENABLED,
    ASYNC_OPENAI_CLIENT,
    S3_REPORTS_BASE_PATH,
    UPDATE_PROPOSED_REPORT_LAYOUT_MODEL,
)
from app.core.logging import setup_logging
from app.observability.llm_response_logger import save_raw_llm_response
from app.observability.web_search_analytics import (
    enrich_terminal_search_analytics,
    log_scheduled_analytics_batch,
    logical_card_id,
    search_analytics_schedule_kwargs,
)
from app.research.agent._base import CasperBase
from app.research.agent._helpers import (
    _conversation_excerpt,
    _normalize_report_config,
    _normalize_tool_call,
    _proposed_layout_tool_result,
    _sibling_tier,
)
from app.research.agent._state import (
    CARD_STYLES,
    GENERIC_TOOL_ERROR_MSG,
    REPORT_TIERS,
    CasperState,
    UpdatedLayoutSection,
    UpdatedProposedReportLayout,
)
from app.research.domains.planner import _drl_heartbeat_lines, _run_card_heartbeat

logger = setup_logging(__name__)


class ReportMixin(CasperBase):
    """Report generation: layout proposal/refresh, report-config pause, card pipeline, generate_report."""

    def _spawn_background_task(self, coro) -> asyncio.Task:
        """Run a coroutine fire-and-forget while keeping a strong reference to it."""
        task = asyncio.create_task(coro)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        return task

    async def _await_layout_refresh(self, timeout: float = 150.0) -> None:
        """Let a running layout refresh finish before the graph run ends.

        The refresh emits its event through the run's stream writer, so it has nowhere to
        write once the graph closes. It is started when the layout is proposed and runs
        while the model writes its reply, so this usually waits out only the remainder.
        """
        task = self._layout_refresh_task
        if task is None or task.done():
            return

        started = time.time()
        _, pending = await asyncio.wait({task}, timeout=timeout)
        if pending:
            logger.warning(
                f"[update_proposed_report_layout] Refresh still running after {timeout:.0f}s "
                f"— cancelling so the turn can close | user: {self.user_name} - "
                f"chat_id: {self.chat_id}"
            )
            task.cancel()
            return
        logger.info(
            f"[update_proposed_report_layout] Waited {time.time() - started:.1f}s for the "
            f"refresh to land | user: {self.user_name} - chat_id: {self.chat_id}"
        )

    def _patched_layout_tool_message(self, messages: list) -> ToolMessage | None:
        """Rewrite the last `propose_report_layout` result so it carries the updated layout.

        Returned with the same message id, so ``add_messages`` overwrites the original in
        place instead of appending. That rewrite is what makes the refreshed layout the one
        the model works from afterwards: it is persisted with the chat history and reloaded
        on later turns, where a fresh Casper has no memory of the refresh itself.
        """
        if not self.updated_proposed_report_layout:
            return None
        updated_markdown = _updated_layout_to_markdown(self.updated_proposed_report_layout)

        target = next(
            (
                m
                for m in reversed(messages or [])
                if getattr(m, "type", None) == "tool"
                and getattr(m, "name", "") == "propose_report_layout"
            ),
            None,
        )
        if target is None:
            logger.warning(
                f"[update_proposed_report_layout] No propose_report_layout result to update — "
                f"the refreshed layout will not reach later turns | "
                f"user: {self.user_name} - chat_id: {self.chat_id}"
            )
            return None

        if not target.id:
            # Without an id the rewrite would be appended as a second result for the same
            # tool call, which breaks tool-call pairing for the provider.
            logger.warning(
                f"[update_proposed_report_layout] propose_report_layout result has no id — "
                f"skipping the rewrite | user: {self.user_name} - chat_id: {self.chat_id}"
            )
            return None

        if updated_markdown in (target.content or ""):
            return None

        logger.info(
            f"[update_proposed_report_layout] Rewrote the proposed layout in message history "
            f"with the web-updated version | user: {self.user_name} - chat_id: {self.chat_id}"
        )
        return ToolMessage(
            content=_proposed_layout_tool_result(
                self.updated_proposed_report_layout.title, updated_markdown, web_updated=True
            ),
            tool_call_id=target.tool_call_id,
            name="propose_report_layout",
            id=target.id,
        )

    async def _upload_report_layout_to_s3_background(
        self, report_layout: str, filename: str
    ) -> None:
        """Background task to upload report layout to S3.

        Args:
            report_layout (str): The report layout content to upload
            filename (str): The filename to use for the upload
        """
        try:
            now = datetime.datetime.now()
            year = now.strftime("%Y")
            month = now.strftime("%m")
            day = now.strftime("%d")

            # Create temporary file
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".txt", delete=False, encoding="utf-8"
            ) as tmp_file:
                tmp_file.write(report_layout)
                temp_path = tmp_file.name

            # Build S3 key
            s3_key = f"{S3_REPORTS_BASE_PATH}/caspr_metadata_reports/{year}/{month}/{day}/{self.user_name}/chat_{self.chat_id}/Report_layout/{filename}"

            try:
                logger.info(f"Uploading report layout to S3: {s3_key}")
                s3_path = await asyncio.to_thread(self.s3_instance.upload_file, temp_path, s3_key)
                if s3_path:
                    logger.info(f"Uploaded report layout to S3: {s3_path}")
                else:
                    logger.error("Failed to upload report layout to S3")
            except Exception as e:
                logger.error(f"Error uploading report layout to S3: {e}")
            finally:
                try:
                    os.unlink(temp_path)
                except Exception as e:
                    logger.warning(f"Failed to cleanup temporary report layout file: {e}")
        except Exception as e:
            logger.error(f"Error in background task for uploading report layout: {e}")

    async def _upload_descriptive_layout_to_s3_background(
        self, descriptive_layout: dict, filename: str
    ) -> None:
        """Background task to upload descriptive report layout to S3.

        Args:
            descriptive_layout (dict): The descriptive layout content to upload
            filename (str): The filename to use for the upload
        """
        try:
            now = datetime.datetime.now()
            year = now.strftime("%Y")
            month = now.strftime("%m")
            day = now.strftime("%d")

            # Create temporary file
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".json", delete=False, encoding="utf-8"
            ) as tmp_file:
                json.dump(descriptive_layout, tmp_file, indent=2, ensure_ascii=False)
                temp_path = tmp_file.name

            # Build S3 key
            s3_key = f"{S3_REPORTS_BASE_PATH}/caspr_metadata_reports/{year}/{month}/{day}/{self.user_name}/chat_{self.chat_id}/Descriptive_report_layout/{filename}"

            try:
                logger.info(f"Uploading descriptive layout to S3: {s3_key}")
                s3_path = await asyncio.to_thread(self.s3_instance.upload_file, temp_path, s3_key)
                if s3_path:
                    logger.info(f"Uploaded descriptive layout to S3: {s3_path}")
                else:
                    logger.error("Failed to upload descriptive layout to S3")
            except Exception as e:
                logger.error(f"Error uploading descriptive layout to S3: {e}")
            finally:
                try:
                    os.unlink(temp_path)
                except Exception as e:
                    logger.warning(f"Failed to cleanup temporary descriptive layout file: {e}")
        except Exception as e:
            logger.error(f"Error in background task for uploading descriptive layout: {e}")

    async def _upload_card_to_s3_background(self, card: dict, card_name: str) -> None:
        """Background task to upload individual card to S3.

        Args:
            card (dict): The card content to upload
            card_name (str): The name of the card section
        """
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".json", delete=False, encoding="utf-8"
            ) as tmp_file:
                json.dump(card, tmp_file, indent=2, ensure_ascii=False)
                temp_path = tmp_file.name

            now = datetime.datetime.now()
            year = now.strftime("%Y")
            month = now.strftime("%m")
            day = now.strftime("%d")
            s3_key = f"{S3_REPORTS_BASE_PATH}/caspr_metadata_reports/{year}/{month}/{day}/{self.user_name}/chat_{self.chat_id}/Cards/{card_name}.json"

            try:
                await asyncio.to_thread(self.s3_instance.upload_file, temp_path, s3_key)
                logger.info(f"Uploaded card to S3: {s3_key}")
            except Exception as e:
                logger.error(f"Error uploading card to S3: {e}")
            finally:
                try:
                    os.unlink(temp_path)
                except Exception as e:
                    logger.warning(f"Failed to cleanup temporary card file: {e}")
        except Exception as e:
            logger.error(f"Error in background task for uploading card: {e}")

    async def _upload_fixed_card_to_s3_background(self, card: dict, card_name: str) -> None:
        """Background task to upload LLM-fixed card to S3."""
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".json", delete=False, encoding="utf-8"
            ) as tmp_file:
                json.dump(card, tmp_file, indent=2, ensure_ascii=False)
                temp_path = tmp_file.name

            now = datetime.datetime.now()
            year = now.strftime("%Y")
            month = now.strftime("%m")
            day = now.strftime("%d")
            s3_key = f"{S3_REPORTS_BASE_PATH}/caspr_metadata_reports/{year}/{month}/{day}/{self.user_name}/chat_{self.chat_id}/Cards/LLM_Fixed_Cards/{card_name}.json"

            try:
                await asyncio.to_thread(self.s3_instance.upload_file, temp_path, s3_key)
                logger.info(f"Uploaded fixed card to S3: {s3_key}")
            except Exception as e:
                logger.error(f"Error uploading fixed card to S3: {e}")
            finally:
                try:
                    os.unlink(temp_path)
                except Exception as e:
                    logger.warning(f"Failed to cleanup temporary fixed card file: {e}")
        except Exception as e:
            logger.error(f"Error in background task for uploading fixed card: {e}")

    async def _upload_cards_for_db_to_s3_background(
        self, cards_for_db: list, table_and_table_id_map: dict
    ) -> None:
        """Background task to upload cards_for_db to S3.

        Args:
            cards_for_db (list): The cards for database
            table_and_table_id_map (dict): Table and table ID mapping
        """
        cards_for_db_file_path = None
        try:
            now = datetime.datetime.now()
            year = now.strftime("%Y")
            month = now.strftime("%m")
            day = now.strftime("%d")

            cards_for_db_file_name = f"{self.user_name}_chat_{self.chat_id}_cards_for_db.json"
            cards_for_db_file_path = os.path.join(os.getcwd(), "temp", cards_for_db_file_name)
            cards_for_db_with_table_and_table_id_map = cards_for_db + [table_and_table_id_map]
            os.makedirs(os.path.dirname(cards_for_db_file_path), exist_ok=True)
            with open(cards_for_db_file_path, "w") as f:
                json.dump(cards_for_db_with_table_and_table_id_map, f, indent=2, ensure_ascii=False)
            s3_key = f"{S3_REPORTS_BASE_PATH}/caspr_metadata_reports/{year}/{month}/{day}/{self.user_name}/chat_{self.chat_id}/Cards/Report_Card_Json/{cards_for_db_file_name}"
            await asyncio.to_thread(self.s3_instance.upload_file, cards_for_db_file_path, s3_key)
            logger.info(
                f"Cards_for_db uploaded successfully to S3: {s3_key} for user: {self.user_name} - chat_id: {self.chat_id}"
            )
        except Exception as e:
            logger.error(
                f"Error uploading cards_for_db to S3: {e} for user: {self.user_name} - chat_id: {self.chat_id}"
            )
        finally:
            if cards_for_db_file_path and os.path.exists(cards_for_db_file_path):
                try:
                    os.remove(cards_for_db_file_path)
                    logger.info(
                        f"Cards_for_db file removed successfully for user: {self.user_name} - chat_id: {self.chat_id}"
                    )
                except Exception as e:
                    logger.warning(f"Failed to cleanup cards_for_db file: {e}")

    async def propose_report_layout(
        self,
        report_layout: str,
        report_title: str = "",
    ) -> str:
        """Present a proposed report layout to the user as a structured preview.

        Call this EVERY time you have a report layout to show the user — the very
        first proposed layout AND every revised layout after the user asks for a
        change. Do NOT write the layout as Markdown headings in your chat message;
        pass the full Markdown layout to this tool instead and it is rendered for
        the user. Call it on its own step (not combined with any other tool).

        ALWAYS write ONE short sentence to the user immediately BEFORE calling this
        tool, and never call it silently. That line is read BEFORE the layout
        exists, so write it as the action you are taking right now — not as a
        finished thing you are handing over.

        Good (announces the work):
          "Let me put a layout together for that."
          "I'm drafting a study layout on Saudi Vision 2030 now."
          for a revision: "I'm reworking the layout around the risk angle."
        Wrong (talks as if the layout is already on screen — it is not yet):
          "Here is a layout I would suggest…"
          "That is the proposed layout, covering 11 sections…"

        Keep it to one line and do NOT describe or list the sections in it — the
        preview renders straight after and already shows them.

        Args:
            report_layout (str): The full proposed report layout in Markdown — a
                single "# Title" line, "## Section" headings (numbering optional),
                and "- " bullet subsections for STUDY reports (no bullet
                subsections for BRIEF). No "---" rules, no code fences. English only.
            report_title (str): The report title in 7 words or less. Optional; the
                "# " line from report_layout is used when this is omitted.

        Returns:
            str: Confirmation that the layout was shown, so you can continue asking
                focused refinement questions or move on to the final summary.
        """
        try:
            event_writer = get_stream_writer()

            if not report_layout or not report_layout.strip():
                logger.warning(
                    f"[propose_report_layout] Empty report_layout received | "
                    f"user: {self.user_name} - chat_id: {self.chat_id}"
                )
                return (
                    "No layout was provided. Compose the full Markdown layout and "
                    "call this tool again."
                )

            cleaned_rl = await asyncio.to_thread(parse_markdown_report_layout, report_layout)
            if not cleaned_rl:
                logger.error(
                    f"[propose_report_layout] Failed to parse layout markdown | "
                    f"user: {self.user_name} - chat_id: {self.chat_id}"
                )
                return GENERIC_TOOL_ERROR_MSG

            if report_title:
                cleaned_rl[0] = {"title": report_title}
            resolved_title = cleaned_rl[0].get("title", report_title or "")

            parsed_layout = await asyncio.to_thread(modify_report_layout, cleaned_rl)
            if not parsed_layout:
                logger.error(
                    f"[propose_report_layout] modify_report_layout returned empty | "
                    f"user: {self.user_name} - chat_id: {self.chat_id}"
                )
                return GENERIC_TOOL_ERROR_MSG

            # Distinct event — this is a PROPOSED layout in the feedback phase, not
            # the final one `retrieve` emits. api.py forwards it as SSE type
            # "report_layout_proposal".
            event_writer(
                {
                    "name": "propose_report_layout",
                    "report_layout": parsed_layout,
                    "report_title": resolved_title,
                }
            )

            # Keep a copy on disk for debugging, mirroring retrieve().
            proposed_layout_filename = (
                f"report_layout_proposed_{self.user_name}_chat_{self.chat_id}.txt"
            )
            asyncio.create_task(
                self._upload_report_layout_to_s3_background(report_layout, proposed_layout_filename)
            )

            logger.info(
                f"[propose_report_layout] Emitted proposed layout | title='{resolved_title}' | "
                f"cards={len(parsed_layout)} | "
                f"user: {self.user_name} - chat_id: {self.chat_id}"
            )

            # The layout above was written by a model with no web access. Refresh it
            # against live sources in the background so the tool returns immediately
            # and the conversation keeps moving while the search runs.
            if self._layout_refresh_task and not self._layout_refresh_task.done():
                # This proposal supersedes the layout the running refresh is working on.
                self._layout_refresh_task.cancel()
            # Anything the previous refresh produced describes a superseded layout.
            self.updated_proposed_report_layout = None
            self._layout_refresh_task = self._spawn_background_task(
                self.update_proposed_report_layout(
                    report_layout=report_layout,
                    report_title=resolved_title,
                    messages=list(self._current_turn_messages),
                    event_writer=event_writer,
                )
            )

            # Echo the exact layout back (like ask_user echoes its options) so the
            # currently-proposed layout stays visible in the message history even
            # after context compaction. This is the layout you must pass verbatim
            # (or with the user's requested edits) as `report_layout` to `retrieve`.
            # `report_or_respond` rewrites this same tool result once the background
            # refresh lands, so the layout carried forward is the web-updated one.
            return _proposed_layout_tool_result(resolved_title, report_layout)
        except Exception as e:
            logger.error(
                f"[propose_report_layout] Error: {e} for user: {self.user_name} - "
                f"chat_id: {self.chat_id}",
                exc_info=True,
            )
            return GENERIC_TOOL_ERROR_MSG

    async def update_proposed_report_layout(
        self,
        report_layout: str,
        report_title: str = "",
        messages: list | None = None,
        event_writer=None,
    ) -> str:
        """Re-check a proposed report layout against live web sources and emit the updated one.

        `propose_report_layout` shows a layout written by a model with no web access, so its
        sections can be built on stale assumptions — superseded events, renamed entities,
        metrics that no longer matter. This runs a web search at high reasoning effort over
        that layout plus the conversation that produced it, and emits the refreshed layout as
        an `updated_proposed_report_layout` event in the SAME structured shape the original
        was sent in. It never raises: it is started as a background task, so a failure just
        means the user keeps the originally proposed layout.

        Args:
            report_layout (str): The proposed layout in Markdown — the exact source of the
                structure that was emitted to the user.
            report_title (str): Title resolved for the proposed layout.
            messages (List): The current conversation messages, so the refresh stays anchored
                to what the user actually asked for.
            event_writer: Stream writer captured by the caller. Falls back to
                ``get_stream_writer()`` when omitted.

        Returns:
            str: The updated layout in Markdown, or an empty string when the refresh did not
                produce a usable layout.
        """
        started = time.time()
        try:
            if event_writer is None:
                event_writer = get_stream_writer()

            updated = await self._web_refreshed_layout(report_layout, report_title, messages)
            if updated is None:
                return ""

            updated_markdown = _updated_layout_to_markdown(updated)
            parsed_layout = await self._parsed_layout_cards(updated_markdown)
            if not parsed_layout:
                return ""

            cleaned_rl = await asyncio.to_thread(parse_markdown_report_layout, updated_markdown)
            resolved_title = (cleaned_rl[0].get("title", "") if cleaned_rl else "") or report_title

            # Same structured payload as `propose_report_layout`, under its own name so the
            # frontend can replace the preview it is already showing.
            event_writer(
                {
                    "name": "updated_proposed_report_layout",
                    "report_layout": parsed_layout,
                    "report_title": resolved_title,
                    "change_summary": (updated.change_summary or "").strip(),
                }
            )

            self.updated_proposed_report_layout = updated

            updated_layout_filename = (
                f"report_layout_updated_proposed_{self.user_name}_chat_{self.chat_id}.txt"
            )
            self._spawn_background_task(
                self._upload_report_layout_to_s3_background(
                    updated_markdown, updated_layout_filename
                )
            )

            logger.info(
                f"[update_proposed_report_layout] Emitted updated layout in "
                f"{time.time() - started:.1f}s | title='{resolved_title}' | "
                f"sections={len(updated.sections)} | cards={len(parsed_layout)} | "
                f"changes='{(updated.change_summary or '')[:160]}' | "
                f"user: {self.user_name} - chat_id: {self.chat_id}"
            )
            return updated_markdown

        except asyncio.CancelledError:
            raise
        except Exception as e:
            # Background refresh — a failure must never disturb the conversation, the user
            # simply keeps the layout that was already proposed.
            logger.error(
                f"[update_proposed_report_layout] Refresh failed after "
                f"{time.time() - started:.1f}s: {e} | user: {self.user_name} - "
                f"chat_id: {self.chat_id}",
                exc_info=True,
            )
            return ""

    async def _parsed_layout_cards(self, layout_markdown: str) -> list[dict]:
        """Markdown layout -> the card list the frontend renders. Empty on failure."""
        cleaned_rl = await asyncio.to_thread(parse_markdown_report_layout, layout_markdown)
        if not cleaned_rl:
            logger.error(
                f"[layout] Failed to parse layout markdown | "
                f"user: {self.user_name} - chat_id: {self.chat_id}"
            )
            return []
        parsed_layout = await asyncio.to_thread(modify_report_layout, cleaned_rl)
        if not parsed_layout:
            logger.error(
                f"[layout] modify_report_layout returned empty | "
                f"user: {self.user_name} - chat_id: {self.chat_id}"
            )
            return []
        return parsed_layout

    async def _web_refreshed_layout(
        self,
        report_layout: str,
        report_title: str = "",
        messages: list | None = None,
    ) -> UpdatedProposedReportLayout | None:
        """Run the web refresh over one layout and return the structured result.

        Deliberately free of side effects — it emits nothing and stores nothing —
        so the sibling layout minted at the pause can go through the same refresh
        the live layout went through (R6) without becoming the live layout (R8).
        Returns ``None`` when the refresh produced nothing usable.
        """
        started = time.time()
        try:
            if not report_layout or not report_layout.strip():
                logger.warning(
                    f"[update_proposed_report_layout] Empty layout — skipping refresh | "
                    f"user: {self.user_name} - chat_id: {self.chat_id}"
                )
                return None

            conversation = _conversation_excerpt(messages)
            current_date = datetime.datetime.now().strftime("%B %d, %Y")

            prompt = f"""Current Date: {current_date}

A research assistant drafted the report layout below for this user. That assistant has NO web access, so the layout reflects only its training data: the topics, named entities, timeframes and metrics in it may be outdated, superseded, or missing developments that have happened since.

Search the web for the current state of this subject, then return an UPDATED version of the SAME layout.

Rules:
- Keep the same overall shape: one title, the same kind of section headings, and subsections ONLY where the original layout has them. Never add subsections to a layout that has none, and never strip subsections from a layout that has them.
- Stay close to the original section count and ordering. Change a section only where current information justifies it — retarget an outdated focus, rename an entity that has changed, add a section for a genuinely significant recent development, or drop one that current sources show is no longer relevant.
- Preserve everything the user explicitly asked for in the conversation below: their scope, focus areas, ordering, and any section they requested by name must survive.
- Section and subsection names stay short headings. Never put URLs, citations, source names, or commentary inside them.
- Write in English only.
- If your search confirms the layout is already current, return it unchanged and leave change_summary empty.

----- WHAT THE USER ASKED FOR (conversation so far) -----
{conversation or "(no conversation context available)"}

----- PROPOSED REPORT LAYOUT (drafted without web access) -----
{report_layout.strip()}"""

            logger.info(
                f"[update_proposed_report_layout] Refreshing proposed layout against web | "
                f"title='{report_title}' | model={UPDATE_PROPOSED_REPORT_LAYOUT_MODEL} | "
                f"user: {self.user_name} - chat_id: {self.chat_id}"
            )

            # No temperature — reasoning models ignore it.
            response = await ASYNC_OPENAI_CLIENT.responses.parse(
                model=UPDATE_PROPOSED_REPORT_LAYOUT_MODEL,
                input=[
                    {
                        "role": "system",
                        "content": (
                            "You are Caspr, a research analyst who verifies a proposed report outline "
                            "against current web sources and returns a corrected outline. Always "
                            "search before answering, and change only what current information "
                            "actually requires. A good section title states a finding, not a topic "
                            "label; where current research supports it, sharpen a bare label into a "
                            "claim. No exclamation points."
                        ),
                    },
                    {"role": "user", "content": prompt},
                ],
                tools=[{"type": "web_search"}],
                tool_choice={"type": "web_search"},
                text_format=UpdatedProposedReportLayout,
                # Reasoning disabled: gpt-4o is not a reasoning model and rejects
                # this outright ("Unsupported parameter: 'reasoning.effort' is not
                # supported with this model"). It was also the whole cost of this
                # call — measured 98.4s at effort=high vs 5.3s with it off, and
                # the turn only waits 150s before discarding the result.
                # reasoning={"effort": UPDATE_PROPOSED_REPORT_LAYOUT_REASONING_EFFORT},
                # (re-add the UPDATE_PROPOSED_REPORT_LAYOUT_REASONING_EFFORT import to restore)
                include=["web_search_call.results"],
            )

            save_raw_llm_response(
                response,
                UPDATE_PROPOSED_REPORT_LAYOUT_MODEL,
                "Refreshing the proposed report layout with current web information",
                self.chat_id,
                user_id=self.user_id,
            )

            updated: UpdatedProposedReportLayout | None = getattr(response, "output_parsed", None)
            if not updated or not updated.sections:
                logger.warning(
                    f"[update_proposed_report_layout] No usable structured layout returned — "
                    f"keeping the original proposal | user: {self.user_name} - chat_id: {self.chat_id}"
                )
                return None

            logger.info(
                f"[update_proposed_report_layout] Refresh returned in {time.time() - started:.1f}s | "
                f"sections={len(updated.sections)} | "
                f"changes='{(updated.change_summary or '')[:160]}' | "
                f"user: {self.user_name} - chat_id: {self.chat_id}"
            )
            return updated

        except asyncio.CancelledError:
            raise
        except Exception as e:
            # Background refresh — a failure must never disturb the conversation, the user
            # simply keeps the layout that was already proposed.
            logger.error(
                f"[update_proposed_report_layout] Refresh failed after "
                f"{time.time() - started:.1f}s: {e} | user: {self.user_name} - "
                f"chat_id: {self.chat_id}",
                exc_info=True,
            )
            return None

    # -- report-config pause -------------------------------------------------

    async def _mint_sibling_layout(
        self,
        report_layout: str,
        report_title: str,
        target_tier: str,
        messages: list | None = None,
    ) -> str:
        """Rewrite a layout for the other tier, keeping the same subject.

        The two tiers are different shapes, not different renderings of one
        shape — a study carries bullet subsections, a brief is flat and capped
        at five sections — so switching tier at the pause needs a layout that
        was actually written for that tier.
        """
        shape = (
            "A STUDY layout: 8-10 '## Section' headings, each followed by two to four "
            "'- ' bullet subsections naming what that section examines."
            if target_tier == "study"
            else "A BRIEF layout: at most FIVE '## Section' headings and NO bullet "
            "subsections at all. Cover the same subject in less space by merging "
            "related ground, never by truncating it."
        )
        prompt = f"""A report layout was written for a {_sibling_tier(target_tier).upper()} report. Rewrite it as a {target_tier.upper()} layout.

Target shape:
- {shape}
- Keep the same title, the same subject, and the same reading order wherever the shape allows.
- Preserve everything the user explicitly asked for in the conversation below.
- Section names stay short headings. No URLs, citations, source names, or commentary in them.
- Write in English only.
- Leave change_summary empty.

----- WHAT THE USER ASKED FOR (conversation so far) -----
{_conversation_excerpt(messages) or "(no conversation context available)"}

----- LAYOUT TO REWRITE -----
{report_layout.strip()}"""

        response = await ASYNC_OPENAI_CLIENT.responses.parse(
            model=UPDATE_PROPOSED_REPORT_LAYOUT_MODEL,
            input=[
                {
                    "role": "system",
                    "content": (
                        "You are Caspr, a research analyst who restates a report outline at a "
                        "different depth without changing what it is about. A good section "
                        "title states a finding, not a topic label. No exclamation points."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            text_format=UpdatedProposedReportLayout,
            # Shares UPDATE_PROPOSED_REPORT_LAYOUT_MODEL with the web refresh, so
            # this has to drop `reasoning` too — gpt-4o 400s on it, and this call
            # sits on the layout_pair -> report_config path.
            # reasoning={"effort": UPDATE_PROPOSED_REPORT_LAYOUT_REASONING_EFFORT},
        )

        save_raw_llm_response(
            response,
            UPDATE_PROPOSED_REPORT_LAYOUT_MODEL,
            f"Minting the {target_tier} sibling of the proposed report layout",
            self.chat_id,
            user_id=self.user_id,
        )

        minted: UpdatedProposedReportLayout | None = getattr(response, "output_parsed", None)
        if not minted or not minted.sections:
            return ""
        if target_tier == "brief":
            # The brief shape is a product rule, not a suggestion: enforce it here
            # rather than trusting the model to have honoured the cap.
            minted.sections = [
                UpdatedLayoutSection(section=s.section, sub_sections=[])
                for s in minted.sections[:5]
            ]
        if not minted.title:
            minted.title = report_title
        return _updated_layout_to_markdown(minted)

    async def layout_pair(self, state: CasperState) -> dict[str, Any]:
        """Produce and emit both tiers' layouts, tagged, before the pause.

        Runs ahead of the interrupt, so it executes exactly once per turn: a
        resume re-enters ``report_config``, never this node. That is the whole
        reason the two are separate nodes — minting here means a tier switch at
        the popup costs nothing at confirm time, and a resume cannot re-mint.

        It deliberately leaves the conversation alone (R8). No
        ``propose_report_layout`` tool message is written for the sibling, so
        the layout the model sees is still the one it proposed.
        """
        event_writer = get_stream_writer()
        messages = state["messages"]

        pending = self._pending_retrieve_call(messages)
        args = (pending or {}).get("args") or {}
        live_tier = str(args.get("report_type") or "study").strip().lower()
        if live_tier not in REPORT_TIERS:
            live_tier = "study"
        report_title = str(args.get("report_title") or "")
        live_markdown = str(args.get("report_layout") or "")

        # The live layout's own refresh started when it was proposed. Waiting it
        # out here is what makes "both layouts are current when the popup opens"
        # true rather than aspirational (R6).
        await self._await_layout_refresh()
        if self.updated_proposed_report_layout:
            live_markdown = _updated_layout_to_markdown(self.updated_proposed_report_layout)
            report_title = self.updated_proposed_report_layout.title or report_title

        sibling_tier = _sibling_tier(live_tier)
        sibling_markdown = ""
        try:
            sibling_markdown = await self._mint_sibling_layout(
                live_markdown, report_title, sibling_tier, messages
            )
            if sibling_markdown:
                refreshed = await self._web_refreshed_layout(
                    sibling_markdown, report_title, messages
                )
                if refreshed is not None:
                    if sibling_tier == "brief":
                        refreshed.sections = [
                            UpdatedLayoutSection(section=s.section, sub_sections=[])
                            for s in refreshed.sections[:5]
                        ]
                    sibling_markdown = _updated_layout_to_markdown(refreshed)
        except Exception as e:
            # A sibling that could not be minted or refreshed must not cost the
            # user the pause. Whatever exists is emitted; the popup still opens.
            logger.error(
                f"[layout_pair] Failed to prepare the '{sibling_tier}' layout: {e} | "
                f"user: {self.user_name} - chat_id: {self.chat_id}",
                exc_info=True,
            )

        pair: dict[str, Any] = {}
        for tier, markdown in ((live_tier, live_markdown), (sibling_tier, sibling_markdown)):
            if not markdown.strip():
                continue
            cards = await self._parsed_layout_cards(markdown)
            if not cards:
                continue
            pair[tier] = {
                "report_tier": tier,
                "report_title": report_title,
                "markdown": markdown,
                "report_layout": cards,
            }

        # Study first so the popup has a stable order regardless of which tier
        # the conversation happened to be built around.
        for tier in REPORT_TIERS:
            if tier in pair:
                event_writer({"name": "layout_pair", **pair[tier]})

        logger.info(
            f"[layout_pair] Emitted {len(pair)} tagged layout(s) "
            f"(live='{live_tier}', sibling='{sibling_tier}') | "
            f"user: {self.user_name} - chat_id: {self.chat_id}"
        )

        self.report_layout_pair = pair
        return {
            "messages": [],
            "report_layout_pair": pair,
            "report_config": {"default_tier": live_tier},
        }

    async def report_config(self, state: CasperState) -> dict[str, Any]:
        """Pause for the user's report configuration, and resume on their Confirm.

        The node holds nothing but the payload and the ``interrupt()`` call.
        LangGraph re-executes an interrupted node from its first line when the
        thread resumes, so anything else here would run twice.
        """
        # `layout_pair` does not re-run on resume, so the pair reaches a resumed
        # (freshly constructed) Casper only through checkpointed state.
        self.report_layout_pair = state.get("report_layout_pair") or {}
        default_tier = str((state.get("report_config") or {}).get("default_tier") or "study")

        payload = {
            "thread_id": self.turn_thread_id,
            "chat_id": self.chat_id,
            "default_tier": default_tier,
            "layouts": [
                self.report_layout_pair[tier]
                for tier in REPORT_TIERS
                if tier in self.report_layout_pair
            ],
            "tiers": list(REPORT_TIERS),
            "styles": list(CARD_STYLES),
        }

        if not self._resuming_report_config:
            try:
                get_stream_writer()(
                    {
                        "name": "report_config",
                        "status": "awaiting_configuration",
                        **payload,
                    }
                )
            except Exception as e:
                logger.warning(
                    f"[report_config] Failed to emit the pause event: {e} | "
                    f"user: {self.user_name} - chat_id: {self.chat_id}"
                )
            logger.info(
                f"[report_config] Pausing before retrieval | thread_id={self.turn_thread_id} | "
                f"default_tier='{default_tier}' | layouts={len(payload['layouts'])} | "
                f"user: {self.user_name} - chat_id: {self.chat_id}"
            )

        submitted = interrupt(payload)

        confirmed = _normalize_report_config(submitted, default_tier)
        self.report_config_submission = confirmed
        logger.info(
            f"[report_config] Resumed with tier='{confirmed['report_tier']}' "
            f"style='{confirmed['style']}' | thread_id={self.turn_thread_id} | "
            f"user: {self.user_name} - chat_id: {self.chat_id}"
        )
        return {"messages": [], "report_config": {**confirmed, "default_tier": default_tier}}

    @staticmethod
    def _pending_retrieve_call(messages: list) -> dict[str, Any] | None:
        """The `retrieve` call on the last AIMessage, normalized. None if absent."""
        last_ai: AIMessage | None = next(
            (m for m in reversed(messages or []) if isinstance(m, AIMessage)), None
        )
        if last_ai is None:
            return None
        for call in getattr(last_ai, "tool_calls", []) or []:
            normalized = _normalize_tool_call(call)
            if normalized.get("name") == "retrieve":
                return normalized
        return None

    def _get_recent_tool_messages(self, messages: list) -> list:
        """Extract the most recent tool message from the message history."""
        for message in reversed(messages):
            if hasattr(message, "type") and message.type == "tool":  # Added: hasattr check
                return [message]
        return []

    # async def _raw_report_generation(self, final_prompt: str) -> str:
    #     """Generate the raw report using the LLM"""
    #     try:
    #         response = await self.anthropic_report_llm.ainvoke(final_prompt)
    #     except Exception as e:
    #         logger.error(f"Error generating raw markdown report using anthropic api: {e} for user: {self.user_name} - chat_id: {self.chat_id}")
    #         # try:
    #         logger.info(f"Now using bedrock api to generate the report for user: {self.user_name} - chat_id: {self.chat_id}")
    #         response = await BEDROCK_REPORT_LLM.ainvoke(final_prompt)
    # except Exception as e:
    # logger.error(f"Error generating raw markdown report using bedrock api: {e} for user: {self.user_name} - chat_id: {self.chat_id}")
    # raise Exception(f"Error generating raw markdown report using bedrock api: {e}")
    # response = AIMessage(content="Error: There was an error while generating the report. Please try again.")

    # return response

    def _emit_card_step(self, event_writer, section_name: str, step: str) -> None:
        """Emit a post-processing progress beat for the card currently being finalized.

        Mirrors the ``card_generation_heartbeat`` events sent during generation so
        the frontend can keep showing concrete progress ("Summarizing section…",
        "Building charts…", …) after the LLM response has been received.
        """
        if event_writer is None or not section_name:
            return
        event_writer(
            {
                "name": "generate_report",
                "status": "card_generation_heartbeat",
                "section": section_name,
                "step": step,
            }
        )

    @staticmethod
    def _cite_counts(text) -> str:
        """DIAGNOSTIC: numbered vs named inline citations in a blob of text."""
        import json as _j
        import re as _re
        blob = text if isinstance(text, str) else _j.dumps(text, ensure_ascii=False)
        n = len(_re.findall(r"\[\d+\]\(https?://", blob))
        m = len(_re.findall(r"\[[A-Za-z][^\]]*\]\(https?://", blob))
        return f"num={n} named={m}"

    async def _prepare_card_through_pipeline(
        self,
        card: dict,
        report_type: str = "study",
        event_writer=None,
        section_name: str = "",
    ) -> tuple[dict, dict]:
        """Run the expensive card processing steps without emitting or appending.

        This is intentionally side-effect-light so multiple cards can be prepared
        concurrently and then stored/streamed in their original order.

        When ``event_writer`` and ``section_name`` are provided, post-processing
        progress beats are emitted for each stage (summary, charts) so the
        frontend can surface what is happening after the card's LLM response.
        """
        self._emit_card_step(event_writer, section_name, "Summarizing section…")
        try:
            card_summary = await asyncio.to_thread(
                generate_section_summary,
                card,
                chat_id=self.chat_id,
                user_id=self.user_id or self.user_name,
            )
        except Exception as e:
            logger.error(
                f"Error generating summary: {e!s} for user: {self.user_name} - chat_id: {self.chat_id}"
            )
            card_summary = ""

        card["summary"] = card_summary
        self._emit_card_step(event_writer, section_name, "Building charts…")
        modified_card = await asyncio.to_thread(modify_card, card)
        card, table_and_table_id_map_for_current_card = await asyncio.to_thread(
            add_viz_to_card,
            modified_card,
            self.user_name,
            self.chat_id,
            report_type,
            user_id=self.user_id or self.user_name,
        )
        logger.info(
            f"Table and table id map for current card: {table_and_table_id_map_for_current_card}"
        )
        card = await asyncio.to_thread(
            update_summaries_for_ask_caspr,
            card,
            chat_id=self.chat_id,
            user_id=self.user_id or self.user_name,
        )
        card["section"][0]["content"] = (
            card["section"][0]["content"]
            .replace("?utm_source=openai", "")
            .replace("&utm_source=openai", "")
        )
        for sub_section in card["sub_sections"]:
            sub_section["content"] = (
                sub_section["content"]
                .replace("?utm_source=openai", "")
                .replace("&utm_source=openai", "")
            )
        return card, table_and_table_id_map_for_current_card

    async def _process_card_through_pipeline(
        self,
        card: dict,
        cards_for_db: list,
        table_and_table_id_map: dict,
        event_writer,
        report_type: str = "study",
        section_name: str = "",
    ) -> dict:
        """Common card processing pipeline shared by study and brief flows.

        Takes a raw card (after fix_card), generates summary, modifies structure,
        adds visualizations, updates summaries, cleans URLs, appends to cards_for_db,
        and emits via event_writer.

        Returns the processed card.
        """
        card, table_and_table_id_map_for_current_card = await self._prepare_card_through_pipeline(
            card,
            report_type=report_type,
            event_writer=event_writer,
            section_name=section_name,
        )
        table_and_table_id_map.update(table_and_table_id_map_for_current_card)
        cards_for_db.append(card)
        event_writer({"name": "generate_report", "card_db": card, "card_type": "section"})
        return card

    async def _process_brief_card_task(
        self,
        idx: int,
        card: dict,
        event_writer=None,
        section_name: str = "",
    ) -> tuple[int, dict, dict]:
        """Prepare a brief card concurrently while preserving its original index."""
        try:
            logger.info(
                f"[brief] Processing card {idx}: {card.get('section', 'unknown')} for user: {self.user_name} - chat_id: {self.chat_id}"
            )
            processed_card, table_map = await self._prepare_card_through_pipeline(
                card,
                report_type="brief",
                event_writer=event_writer,
                section_name=section_name,
            )
            return idx, processed_card, table_map
        except Exception as e:
            logger.error(
                f"[brief] Error processing card {idx}: {e!s} for user: {self.user_name} - chat_id: {self.chat_id}"
            )
            fallback_card = {
                "section": [
                    {
                        "name": card.get("section", f"Section {idx}"),
                        "content": "",
                        "tables": [
                            {
                                "visualization": "",
                                "table_id": "",
                                "table_title": "",
                                "visualization_type": "",
                            }
                        ],
                        "id": str(uuid7()),
                        "summary": "",
                        "analyst_reasoning": [],
                    }
                ],
                "sub_sections": [
                    {
                        "name": "",
                        "content": "",
                        "tables": [
                            {
                                "visualization": "",
                                "table_id": "",
                                "table_title": "",
                                "visualization_type": "",
                            }
                        ],
                        "id": str(uuid7()),
                        "summary": "",
                        "analyst_reasoning": [],
                    }
                ],
                "citations": {},
                "summary": "",
                "analyst_reasoning": [],
            }
            return idx, fallback_card, {}

    async def generate_report(self, state: MessagesState) -> dict[str, list]:
        """Generate the final report using retrieved information.

        Note: This method should only be called after the retrieve tool has been executed.
        Document query tools should not route here.
        """
        try:
            event_writer = get_stream_writer()
            # Extract tool messages (retrieval results)
            tool_messages = self._get_recent_tool_messages(state["messages"])

            if not tool_messages:
                error_msg = "No tool messages found. The generate_report method should only be called after the retrieve tool."
                logger.error(f"{error_msg} for user: {self.user_name} - chat_id: {self.chat_id}")
                raise Exception(error_msg)

            if not hasattr(tool_messages[0], "artifact") or not tool_messages[0].artifact:
                error_msg = f"No artifact found in tool message. Tool called was: {tool_messages[0].name if hasattr(tool_messages[0], 'name') else 'unknown'}. The generate_report method requires the retrieve tool's artifact."
                logger.error(f"{error_msg} for user: {self.user_name} - chat_id: {self.chat_id}")
                raise Exception(error_msg)

            artifact = tool_messages[0].artifact
            if not artifact:
                logger.error(
                    f"No artifact found in tool_messages for user: {self.user_name} - chat_id: {self.chat_id}"
                )
                raise

            descriptive_report_layout = artifact.get("descriptive_report_layout", "")
            # modified_report_layout = artifact.get('modified_report_layout', '')
            report_layout = artifact.get("report_layout", "")

            if not descriptive_report_layout:
                logger.error(
                    f"No descriptive report layout found in artifact for user: {self.user_name} - chat_id: {self.chat_id}"
                )
                raise Exception(
                    f"No descriptive report layout found in artifact for user: {self.user_name} - chat_id: {self.chat_id}"
                )

            # if not modified_report_layout:
            #     logger.error(f"No modified report layout found in artifact for user: {self.user_name} - chat_id: {self.chat_id}")
            #     raise Exception(f"No modified report layout found in artifact for user: {self.user_name} - chat_id: {self.chat_id}")

            if not report_layout:
                logger.error(
                    f"No report layout found in artifact for user: {self.user_name} - chat_id: {self.chat_id}"
                )
                raise Exception(
                    f"No report layout found in artifact for user: {self.user_name} - chat_id: {self.chat_id}"
                )

            # Get configuration from retrieve_config
            user_instructions = self.retrieve_config.get("user_instructions", "")
            report_layout = self.retrieve_config.get("report_layout", "")
            report_type = self.retrieve_config.get("report_type", "study")
            # report_language = self.retrieve_config.get('report_language', 'English')
            # Validation - Added
            if not user_instructions or not report_layout:
                logger.error(
                    f"Missing user_instructions or report_layout in retrieve_config for user: {self.user_name} - chat_id: {self.chat_id}"
                )
                raise Exception(
                    f"Missing user_instructions or report_layout in retrieve_config for user: {self.user_name} - chat_id: {self.chat_id}"
                )
            table_of_contents, title, subtitle = await asyncio.to_thread(
                extract_title_and_toc, descriptive_report_layout
            )
            title = self.retrieve_config["report_title"]
            logger.info(f"Title from LG: {title}")
            if not table_of_contents or not title or not subtitle:
                logger.error(
                    f"Error extracting title and toc for user: {self.user_name} - chat_id: {self.chat_id}"
                )
                raise Exception(
                    f"Error extracting title and toc for user: {self.user_name} - chat_id: {self.chat_id}"
                )

            cards_for_db = []
            title_section_id = str(uuid7())
            title_sub_section_id = str(uuid7())
            cards_for_db.append(
                {
                    "section": [
                        {
                            "name": "title",
                            "content": title,
                            "tables": [
                                {
                                    "visualization": "",
                                    "table_id": "",
                                    "table_title": "",
                                    "visualization_type": "",
                                }
                            ],
                            "id": title_section_id,
                            "summary": "",
                        }
                    ],
                    "sub_sections": [
                        {
                            "name": "",
                            "content": "",
                            "tables": [
                                {
                                    "visualization": "",
                                    "table_id": "",
                                    "table_title": "",
                                    "visualization_type": "",
                                }
                            ],
                            "id": title_sub_section_id,
                            "summary": "",
                        }
                    ],
                    "citations": {},
                    "summary": "",
                }
            )
            event_writer(
                {
                    "name": "generate_report",
                    "card_db": {
                        "section": [
                            {
                                "name": "title",
                                "content": title,
                                "tables": [
                                    {
                                        "visualization": "",
                                        "table_id": "",
                                        "table_title": "",
                                        "visualization_type": "",
                                    }
                                ],
                                "id": title_section_id,
                                "summary": "",
                            }
                        ],
                        "sub_sections": [
                            {
                                "name": "",
                                "content": "",
                                "tables": [
                                    {
                                        "visualization": "",
                                        "table_id": "",
                                        "table_title": "",
                                        "visualization_type": "",
                                    }
                                ],
                                "id": title_sub_section_id,
                                "summary": "",
                            }
                        ],
                        "citations": {},
                        "summary": "",
                    },
                    "card_type": "title",
                }
            )

            subtitle_section_id = str(uuid7())
            subtitle_sub_section_id = str(uuid7())
            cards_for_db.append(
                {
                    "section": [
                        {
                            "name": "subtitle",
                            "content": subtitle,
                            "tables": [
                                {
                                    "visualization": "",
                                    "table_id": "",
                                    "table_title": "",
                                    "visualization_type": "",
                                }
                            ],
                            "id": subtitle_section_id,
                            "summary": "",
                        }
                    ],
                    "sub_sections": [
                        {
                            "name": "",
                            "content": "",
                            "tables": [
                                {
                                    "visualization": "",
                                    "table_id": "",
                                    "table_title": "",
                                    "visualization_type": "",
                                }
                            ],
                            "id": subtitle_sub_section_id,
                            "summary": "",
                        }
                    ],
                    "citations": {},
                    "summary": "",
                }
            )
            event_writer(
                {
                    "name": "generate_report",
                    "card_db": {
                        "section": [
                            {
                                "name": "subtitle",
                                "content": subtitle,
                                "tables": [
                                    {
                                        "visualization": "",
                                        "table_id": "",
                                        "table_title": "",
                                        "visualization_type": "",
                                    }
                                ],
                                "id": subtitle_section_id,
                                "summary": "",
                            }
                        ],
                        "sub_sections": [
                            {
                                "name": "",
                                "content": "",
                                "tables": [
                                    {
                                        "visualization": "",
                                        "table_id": "",
                                        "table_title": "",
                                        "visualization_type": "",
                                    }
                                ],
                                "id": subtitle_sub_section_id,
                                "summary": "",
                            }
                        ],
                        "citations": {},
                        "summary": "",
                    },
                    "card_type": "subtitle",
                }
            )

            # cards_for_db.append({"table_of_contents": f"Table of Contents\n\n{table_of_contents}"})
            toc_section_id = str(uuid7())
            toc_sub_section_id = str(uuid7())
            cards_for_db.append(
                {
                    "section": [
                        {
                            "name": "table_of_contents",
                            "content": table_of_contents,
                            "tables": [
                                {
                                    "visualization": "",
                                    "table_id": "",
                                    "table_title": "",
                                    "visualization_type": "",
                                }
                            ],
                            "id": toc_section_id,
                            "summary": "",
                        }
                    ],
                    "sub_sections": [
                        {
                            "name": "",
                            "content": "",
                            "tables": [
                                {
                                    "visualization": "",
                                    "table_id": "",
                                    "table_title": "",
                                    "visualization_type": "",
                                }
                            ],
                            "id": toc_sub_section_id,
                            "summary": "",
                        }
                    ],
                    "citations": {},
                    "summary": "",
                }
            )
            event_writer(
                {
                    "name": "generate_report",
                    "card_db": {
                        "section": [
                            {
                                "name": "table_of_contents",
                                "content": table_of_contents,
                                "tables": [
                                    {
                                        "visualization": "",
                                        "table_id": "",
                                        "table_title": "",
                                        "visualization_type": "",
                                    }
                                ],
                                "id": toc_section_id,
                                "summary": "",
                            }
                        ],
                        "sub_sections": [
                            {
                                "name": "",
                                "content": "",
                                "tables": [
                                    {
                                        "visualization": "",
                                        "table_id": "",
                                        "table_title": "",
                                        "visualization_type": "",
                                    }
                                ],
                                "id": toc_sub_section_id,
                                "summary": "",
                            }
                        ],
                        "citations": {},
                        "summary": "",
                    },
                    "card_type": "toc",
                }
            )

            all_report_citations = []  # Here we store all the citations from all the sections with right sequence
            openai_citations = []  # Here we store all the citations from openai in raw json format
            citation_url_map = {}  # Here we store url-citation mapping (key: url, value: citation)
            descriptive_report_layout = descriptive_report_layout[
                1:
            ]  # Removing the first section (title)
            # cumulative_summary = ""  # PAUSED: Using full previous cards as context instead
            table_and_table_id_map = {}

            if report_type == "brief":
                logger.info(
                    f"[generate_report] Brief report mode — using generate_brief_cards | user: {self.user_name} - chat_id: {self.chat_id}"
                )
                # Only enrich DRL with document context when a grep session is available
                if self.grep_session:
                    descriptive_report_layout = gather_context_from_uploaded_file(
                        descriptive_report_layout,
                        self.grep_session,
                        user_instructions,
                        chat_id=self.chat_id,
                        user_id=self.user_id or self.user_name,
                    )

                # Brief card streaming has two stages running together:
                # 1. `brief_gen` yields raw cards as soon as the brief LLM finishes each JSON section.
                #    Example raw_card:
                #    {
                #        "section": "External Shock Risks",
                #        "content": "The main near-term crisis triggers are...",
                #        "sub_sections": [{"name": "Impact of US tariffs", "content": "..."}],
                #    }
                # 2. `_process_brief_card_task` performs slower post-processing for that card:
                #    summaries, markdown cleanup, table extraction, and visualization upload.
                #
                # We process cards concurrently for speed, but emit them in report order.
                # Example:
                # - Card 1 and card 2 are processing.
                # - Card 2 finishes first, so we store it in `brief_card_results`.
                # - Card 1 finishes later; `flush_ready_brief_cards()` emits card 1, then
                #   immediately emits already-ready card 2.

                # Web-search analytics for brief reports: a single `generate_brief_cards`
                # call covers the *entire* report (all sections in one LLM call), unlike
                # the study path below which runs one `generate_cards` call per section.
                # `analytics_collector` therefore ends up holding at most one entry, and
                # persistence is scheduled once after all brief cards are emitted (see
                # `_schedule_brief_analytics` further down), instead of once per section.
                analytics_collector: list[dict] = []
                analytics_operation_id = str(uuid7())

                brief_gen = generate_brief_cards(
                    drl=descriptive_report_layout,
                    user_instructions=user_instructions,
                    chat_id=self.chat_id,
                    user_id=self.user_id or self.user_name,
                    analytics_collector=analytics_collector,
                    analytics_operation_id=analytics_operation_id,
                    prioritize_arxiv=self.prioritize_arxiv,
                    style=self.card_style,
                )
                idx = 0  # Last raw card number received from `brief_gen`; example: 0 -> 1 -> 2.
                next_emit_idx = 1  # Next card number allowed to stream to UI; example: waits for card 1 before card 2.
                brief_card_tasks = {}  # Maps active asyncio tasks to card indexes; example: {<Task pending>: 2}.
                brief_card_results = {}  # Holds completed cards waiting for order; example: {2: (card_db, table_map)}.
                brief_stream_finished = (
                    False  # True only after `brief_gen` returns None, meaning no more raw cards.
                )
                # Per-card heartbeat tasks: each card keeps its OWN heartbeat running
                # from the moment it starts streaming, through its (concurrent)
                # post-processing, until its `card_db` event is actually sent. Multiple
                # cards' heartbeats therefore run at the same time (e.g. card A finishing
                # post-processing while card B is still being generated).
                card_hb_tasks = {}  # {card_idx: hb_task}
                hb_reap = []  # cancelled heartbeat tasks awaiting a final await to avoid warnings

                def flush_ready_brief_cards():
                    """Emit every completed card that is ready in strict report order.

                    Example before flushing:
                    - next_emit_idx = 1
                    - brief_card_results = {2: (card2, table_map2)}
                    Nothing emits because card 1 is still missing.

                    Example after card 1 arrives:
                    - next_emit_idx = 1
                    - brief_card_results = {1: (card1, table_map1), 2: (card2, table_map2)}
                    This emits card 1, increments next_emit_idx to 2, then emits card 2.
                    """
                    nonlocal next_emit_idx
                    while next_emit_idx in brief_card_results:
                        card, table_map = brief_card_results.pop(next_emit_idx)
                        table_and_table_id_map.update(table_map)
                        cards_for_db.append(card)
                        logger.info(
                            f"[brief] Emitting card {next_emit_idx} for user: {self.user_name} - chat_id: {self.chat_id}"
                        )
                        event_writer(
                            {"name": "generate_report", "card_db": card, "card_type": "section"}
                        )
                        # This card's event has now been sent, so its heartbeat is no
                        # longer needed — stop it (later cards keep their own running).
                        emitted_hb = card_hb_tasks.pop(next_emit_idx, None)
                        if emitted_hb is not None:
                            emitted_hb.cancel()
                            hb_reap.append(emitted_hb)
                        next_emit_idx += 1

                # Brief cards stream in DRL order, so while the LLM is producing the
                # next card we surface that section's own DRL heartbeat lines. Each
                # raw-card fetch gets a paired heartbeat task keyed off the DRL section
                # about to arrive (descriptive_report_layout[<cards received so far>]).
                def _start_brief_heartbeat(card_number: int):
                    # Heartbeat for the section currently being streamed by the LLM.
                    # Out of range (no more sections to stream) yields an empty no-op:
                    # the per-card heartbeats in `card_hb_tasks` cover post-processing.
                    section = (
                        descriptive_report_layout[card_number]
                        if card_number < len(descriptive_report_layout)
                        else {}
                    )
                    return asyncio.create_task(
                        _run_card_heartbeat(
                            section.get("section", f"Section {card_number + 1}"),
                            event_writer,
                            heartbeat_lines=_drl_heartbeat_lines(section),
                        )
                    )

                async def _stop_brief_heartbeat(hb_task):
                    if hb_task is None:
                        return
                    hb_task.cancel()
                    try:
                        await hb_task
                    except asyncio.CancelledError:
                        pass

                # Pulling from the sync generator can block while the LLM streams the next JSON object,
                # so we run `next(brief_gen, None)` in a thread and race it against card processing.
                # Example result: raw_card dict for card 3, or None when the brief stream is complete.
                next_raw_card_task = asyncio.create_task(
                    asyncio.to_thread(lambda: next(brief_gen, None))
                )
                next_raw_card_hb = _start_brief_heartbeat(idx)
                await asyncio.sleep(0)  # yield once so the heartbeat task can emit its first line
                while not brief_stream_finished or brief_card_tasks:
                    # Wait for either:
                    # - the next raw card to arrive from the LLM stream, or
                    # - any already-started card processing task to finish.
                    # This is what lets card 1 emit as soon as it finishes, without waiting for all cards.
                    wait_tasks = set(brief_card_tasks.keys())
                    if next_raw_card_task is not None:
                        wait_tasks.add(next_raw_card_task)

                    done, _ = await asyncio.wait(wait_tasks, return_when=asyncio.FIRST_COMPLETED)
                    raw_card_task_done = next_raw_card_task if next_raw_card_task in done else None

                    if raw_card_task_done is not None:
                        raw_card = raw_card_task_done.result()
                        if raw_card is None:
                            # LLM stream is done. This heartbeat was anticipating a
                            # section that will never stream, so stop it. Cards already
                            # in flight keep their own heartbeats (card_hb_tasks) running
                            # through post-processing until their card events are sent.
                            brief_stream_finished = True
                            next_raw_card_task = None
                            await _stop_brief_heartbeat(next_raw_card_hb)
                            next_raw_card_hb = None
                        else:
                            # A real card arrived. Its section has finished streaming,
                            # but we DON'T stop its heartbeat — we hand it off to
                            # card_hb_tasks so it keeps running through this card's
                            # post-processing until its card event is sent. A fresh
                            # heartbeat is started below for the next streaming section.
                            idx += 1
                            card_hb_tasks[idx] = next_raw_card_hb
                            next_raw_card_hb = None
                            try:
                                logger.info(
                                    f"[brief] Queueing card {idx}: {raw_card.get('section', 'unknown')} for user: {self.user_name} - chat_id: {self.chat_id}"
                                )
                                _before = self._cite_counts(raw_card.get("content", ""))
                                card = replace_brief_citations(
                                    raw_card,
                                    citation_url_map,
                                    all_report_citations,
                                    keep_labels=True,
                                )
                                event_writer({"name": "citation_debug", "branch": "brief",
                                              "stage": "replace_brief_citations",
                                              "before": _before,
                                              "after": self._cite_counts(card.get("content", ""))})
                                # Start expensive card processing but do not await it here.
                                # Example task result shape: (2, processed_card_db, {"table-id": "| table markdown |"}).
                                task = asyncio.create_task(
                                    self._process_brief_card_task(
                                        idx,
                                        card,
                                        event_writer=event_writer,
                                        section_name=raw_card.get("section", f"Section {idx}"),
                                    )
                                )
                                brief_card_tasks[task] = idx

                            except Exception as e:
                                logger.error(
                                    f"[brief] Error queueing card {idx}: {e!s} for user: {self.user_name} - chat_id: {self.chat_id}"
                                )
                                fallback_card = {
                                    "section": [
                                        {
                                            "name": raw_card.get("section", f"Section {idx}"),
                                            "content": "",
                                            "tables": [
                                                {
                                                    "visualization": "",
                                                    "table_id": "",
                                                    "table_title": "",
                                                    "visualization_type": "",
                                                }
                                            ],
                                            "id": str(uuid7()),
                                            "summary": "",
                                            "analyst_reasoning": [],
                                        }
                                    ],
                                    "sub_sections": [
                                        {
                                            "name": "",
                                            "content": "",
                                            "tables": [
                                                {
                                                    "visualization": "",
                                                    "table_id": "",
                                                    "table_title": "",
                                                    "visualization_type": "",
                                                }
                                            ],
                                            "id": str(uuid7()),
                                            "summary": "",
                                            "analyst_reasoning": [],
                                        }
                                    ],
                                    "citations": {},
                                    "summary": "",
                                    "analyst_reasoning": [],
                                }
                                brief_card_results[idx] = (fallback_card, {})
                                flush_ready_brief_cards()

                            # Keep reading the next raw card while existing cards continue processing.
                            next_raw_card_task = asyncio.create_task(
                                asyncio.to_thread(lambda: next(brief_gen, None))
                            )
                            next_raw_card_hb = _start_brief_heartbeat(idx)
                            await asyncio.sleep(
                                0
                            )  # yield once so the heartbeat task can emit its first line

                    for completed_task in done:
                        if completed_task not in brief_card_tasks:
                            continue

                        task_idx = brief_card_tasks.pop(completed_task)
                        try:
                            result_idx, card, table_map = completed_task.result()
                        except Exception as e:
                            logger.error(
                                f"[brief] Error processing card {task_idx}: {e!s} for user: {self.user_name} - chat_id: {self.chat_id}"
                            )
                            result_idx = task_idx
                            card = {
                                "section": [
                                    {
                                        "name": f"Section {task_idx}",
                                        "content": "",
                                        "tables": [
                                            {
                                                "visualization": "",
                                                "table_id": "",
                                                "table_title": "",
                                                "visualization_type": "",
                                            }
                                        ],
                                        "id": str(uuid7()),
                                        "summary": "",
                                        "analyst_reasoning": [],
                                    }
                                ],
                                "sub_sections": [
                                    {
                                        "name": "",
                                        "content": "",
                                        "tables": [
                                            {
                                                "visualization": "",
                                                "table_id": "",
                                                "table_title": "",
                                                "visualization_type": "",
                                            }
                                        ],
                                        "id": str(uuid7()),
                                        "summary": "",
                                        "analyst_reasoning": [],
                                    }
                                ],
                                "citations": {},
                                "summary": "",
                                "analyst_reasoning": [],
                            }
                            table_map = {}

                        # Store the completed result by its original card index. It may emit now
                        # or wait for earlier cards. Example: result_idx=2 waits if card 1 is missing.
                        brief_card_results[result_idx] = (card, table_map)
                        flush_ready_brief_cards()

                # Every card has been post-processed and streamed in order. Reap any
                # heartbeats: the (possibly None) streaming heartbeat plus any per-card
                # heartbeats that flush already cancelled, and any leftovers as a
                # safety net.
                await _stop_brief_heartbeat(next_raw_card_hb)
                next_raw_card_hb = None
                for leftover_hb in list(card_hb_tasks.values()):
                    leftover_hb.cancel()
                    hb_reap.append(leftover_hb)
                card_hb_tasks.clear()
                if hb_reap:
                    await asyncio.gather(*hb_reap, return_exceptions=True)
                    hb_reap.clear()

                # Persist web-search analytics for the whole brief report now that
                # every section card has been emitted into `cards_for_db` (title,
                # subtitle, TOC, and all brief sections). Enrichment scans that full,
                # final content for URLs actually retained in output, then schedules
                # the single collected entry (if any) for a non-blocking DB write via
                # `asyncio.create_task`, mirroring the study-report path below. All
                # steps are wrapped so analytics can never affect report generation.
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
                                        "user_id": self.user_id,
                                        "chat_id": self.chat_id,
                                        "section_name": "brief_report",
                                    }
                                )
                                asyncio.create_task(log_web_search_event(**event))
                            except Exception:
                                logger.exception(
                                    "[brief] Failed to schedule non-fatal search analytics"
                                )
                except Exception:
                    logger.exception("[brief] Failed to enrich non-fatal terminal search analytics")

            else:
                for idx, section in enumerate(descriptive_report_layout):
                    section_name = section.get("section", f"Section {idx + 1}")
                    analytics_collector: list[dict] = []
                    analytics_operation_id = str(uuid7())
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
                                    "[card_generation] Failed to enrich non-fatal "
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
                                        "user_id": self.user_id,
                                        "chat_id": self.chat_id,
                                        "section_name": section_name,
                                        "card_id": card_id,
                                    }
                                )
                                asyncio.create_task(log_web_search_event(**event))
                            except Exception:
                                logger.exception(
                                    "[card_generation] Failed to schedule "
                                    "non-fatal search analytics"
                                )

                    try:
                        logger.info(
                            f"Processing section {idx + 1}/{len(descriptive_report_layout)}: {section['section']} for user: {self.user_name} - chat_id: {self.chat_id}"
                        )

                        # Surface this section's own DRL-authored heartbeat lines while
                        # its card is generated (a single long, blocking LLM call).
                        _hb_task = asyncio.create_task(
                            _run_card_heartbeat(
                                section_name,
                                event_writer,
                                heartbeat_lines=_drl_heartbeat_lines(section),
                            )
                        )
                        try:
                            card, card_citations = await asyncio.to_thread(
                                generate_cards,
                                section,
                                "",
                                is_first_section=idx == 0,
                                descriptive_report_layout=descriptive_report_layout,
                                user_instructions=user_instructions,
                                report_length=report_type.upper(),
                                upload_file_config=self.upload_file_config,
                                web_search=self.web_search,
                                previous_cards=cards_for_db,
                                grep_session=self.grep_session,
                                chat_id=self.chat_id,
                                user_id=self.user_id,
                                analytics_collector=analytics_collector,
                                analytics_operation_id=analytics_operation_id,
                                prioritize_arxiv=self.prioritize_arxiv,
                                style=self.card_style,
                            )
                        finally:
                            _hb_task.cancel()
                            try:
                                await _hb_task
                            except asyncio.CancelledError:
                                pass
                        # Note: generate_cards already falls back to Gemini internally
                        # (app.cards.service_cards.generate_cards) when OpenAI fails, so
                        # `card`/`card_citations` are only None here if OpenAI AND the
                        # Gemini fallback both failed for this section.
                        card_with_citations = [card, card_citations]
                        card_name = f"{card['section']}"
                        asyncio.create_task(
                            self._upload_card_to_s3_background(card_with_citations, card_name)
                        )

                        self._emit_card_step(event_writer, section_name, "Linking sources…")
                        logger.info(
                            f"Running card fixer for section: {card.get('section', 'unknown')}"
                        )
                        card = fix_card(
                            card,
                            chat_id=self.chat_id,
                            user_id=self.user_id or self.user_name,
                        )
                        logger.info(
                            f"Card fixer completed for section: {card.get('section', 'unknown')}"
                        )
                        asyncio.create_task(
                            self._upload_fixed_card_to_s3_background(card, card_name)
                        )

                        # Analyst reasoning: on contested / derived claims only, replace
                        # bare citations with visible source-evaluation prose. Evidence
                        # is built in-memory from this section's analytics collector, so
                        # there is no DB round trip. Fully non-fatal — on any failure the
                        # fixed card is used unchanged.
                        if ANALYST_REASONING_ENABLED:
                            try:
                                from app.agent.analyst_reasoning import (
                                    apply_analyst_reasoning,
                                    build_evidence_store_from_analytics,
                                    count_reasoning,
                                )

                                card = await apply_analyst_reasoning(
                                    card,
                                    card_id=logical_card_id(card),
                                    chat_id=self.chat_id,
                                    user_id=self.user_id or self.user_name,
                                    evidence=build_evidence_store_from_analytics(
                                        analytics_collector
                                    ),
                                )
                                _note_count = count_reasoning(card)
                                if _note_count:
                                    logger.info(
                                        f"[analyst_reasoning] Rewrote {_note_count} "
                                        f"contested claim(s) in section: {card.get('section', 'unknown')}"
                                    )
                            except Exception:
                                logger.exception(
                                    "[analyst_reasoning] Non-fatal failure; using fixed card as-is "
                                    f"for section: {card.get('section', 'unknown')}"
                                )

                        if card_citations and isinstance(card_citations[0], dict):
                            logger.info(
                                f"Openai citations detected for section {idx + 1}: {section['section']} for user: {self.user_name} - chat_id: {self.chat_id}"
                            )
                            openai_citations.extend(card_citations)

                            web_urls = []
                            file_citation_count = 0
                            url_to_snippet = {}
                            for citation in card_citations:
                                if citation.get("url"):
                                    web_urls.append(citation["url"])
                                    snippet = citation.get("snippet", "")
                                    if snippet:
                                        cleaned_url = clean_url(citation["url"])
                                        if cleaned_url not in url_to_snippet:
                                            url_to_snippet[cleaned_url] = snippet
                                elif citation.get("type") == "file_citation" or citation.get(
                                    "file_id"
                                ):
                                    file_citation_count += 1

                            if web_urls:
                                try:
                                    card_citations = [
                                        await asyncio.to_thread(clean_url, url) for url in web_urls
                                    ]
                                    logger.info(
                                        f"Processed {len(card_citations)} web citations for user: {self.user_name} - chat_id: {self.chat_id}"
                                    )
                                except Exception as e:
                                    logger.error(
                                        f"Error cleaning web citation URLs for user: {self.user_name} - chat_id: {self.chat_id}: {e}"
                                    )
                                    card_citations = [
                                        url.replace("?utm_source=openai", "").replace(
                                            "&utm_source=openai", ""
                                        )
                                        for url in web_urls
                                    ]
                            else:
                                card_citations = []

                            if file_citation_count > 0:
                                logger.info(
                                    f"Skipped {file_citation_count} file_citation annotations (no URL) for section {idx + 1}: {section['section']} for user: {self.user_name} - chat_id: {self.chat_id}"
                                )

                        elif card_citations:
                            logger.info(
                                f"Perplexity citations detected for section {idx + 1}: {section['section']} for user: {self.user_name} - chat_id: {self.chat_id}"
                            )
                            url_to_snippet = {}
                        else:
                            logger.warning(
                                f"No citations found for section {idx + 1}: {section['section']} for user: {self.user_name} - chat_id: {self.chat_id}"
                            )
                            card_citations = []
                            url_to_snippet = {}

                        if card is None or card_citations is None:
                            logger.error(
                                f"Failed to generate content for section {idx + 1} for user: {self.user_name} - chat_id: {self.chat_id}"
                            )
                            raise Exception(
                                f"Card generation failed for section {section['section']}"
                            )

                        processed_citations = []
                        citation_indices = []

                        for url in card_citations:
                            if url in citation_url_map:
                                citation_indices.append(citation_url_map[url])
                            else:
                                all_report_citations.append(url)
                                citation_url_map[url] = len(all_report_citations)
                                citation_indices.append(len(all_report_citations))
                            processed_citations.append(url)

                        logger.info(
                            f"Total unique citations count: {len(all_report_citations)} for section {idx + 1}: {section['section']} for user: {self.user_name} - chat_id: {self.chat_id}"
                        )
                        logger.info(
                            f"Generated content for section {idx + 1} for user: {self.user_name} - chat_id: {self.chat_id}"
                        )
                        logger.info(
                            f"Retrieved {len(card_citations)} citations, {len(processed_citations)} after processing for user: {self.user_name} - chat_id: {self.chat_id}"
                        )

                        # Keep OpenAI's inline [Source Name](url) citations as written —
                        # only strip tracking params and deep-link to the cited passage.
                        # (Was replace_citations, which renumbered every link to [N].)
                        _before = self._cite_counts(card.get("content", ""))
                        try:
                            from app.agent.analyst_reasoning import dress_inline_citations

                            card["content"] = (
                                await asyncio.to_thread(
                                    dress_inline_citations,
                                    card["content"],
                                    url_to_snippet=url_to_snippet,
                                    citation_url_map=citation_url_map,
                                    report_citations=all_report_citations,
                                )
                                or card["content"]
                            )
                            for i in range(len(card["sub_sections"])):
                                card["sub_sections"][i]["content"] = (
                                    await asyncio.to_thread(
                                        dress_inline_citations,
                                        card["sub_sections"][i]["content"],
                                        url_to_snippet=url_to_snippet,
                                        citation_url_map=citation_url_map,
                                        report_citations=all_report_citations,
                                    )
                                    or card["sub_sections"][i]["content"]
                                )
                        except Exception as e:
                            logger.error(f"Error dressing citations in section {idx + 1}: {e!s}")
                            event_writer({"name": "citation_debug", "branch": "study",
                                          "stage": "dress_FAILED", "error": repr(e),
                                          "before": _before})
                        else:
                            event_writer({"name": "citation_debug", "branch": "study",
                                          "stage": "dressed", "before": _before,
                                          "after": self._cite_counts(card.get("content", ""))})

                        logger.info(
                            f"Added section {idx + 1} to cards_for_db for user: {self.user_name} - chat_id: {self.chat_id}"
                        )

                        card["citations"] = {
                            url: citation_url_map.get(url, 0)
                            for url in processed_citations
                            if url in citation_url_map
                        }
                        card = await self._process_card_through_pipeline(
                            card,
                            cards_for_db,
                            table_and_table_id_map,
                            event_writer,
                            report_type=report_type,
                            section_name=section_name,
                        )
                        _schedule_collected_analytics(card)

                        logger.info(
                            f"Updating cumulative summary after section {idx + 1} for user: {self.user_name} - chat_id: {self.chat_id}"
                        )

                    except Exception as e:
                        _schedule_collected_analytics()
                        logger.error(
                            f"Error processing section {idx + 1}: {e!s} for user: {self.user_name} - chat_id: {self.chat_id}"
                        )
                        card = {
                            "section": [
                                {
                                    "name": section["section"],
                                    "content": "",
                                    "tables": [
                                        {
                                            "visualization": "",
                                            "table_id": "",
                                            "table_title": "",
                                            "visualization_type": "",
                                        }
                                    ],
                                    "id": str(uuid7()),
                                    "summary": "",
                                    "analyst_reasoning": [],
                                }
                            ],
                            "sub_sections": [
                                {
                                    "name": "",
                                    "content": "",
                                    "tables": [
                                        {
                                            "visualization": "",
                                            "table_id": "",
                                            "table_title": "",
                                            "visualization_type": "",
                                        }
                                    ],
                                    "id": str(uuid7()),
                                    "summary": "",
                                    "analyst_reasoning": [],
                                }
                            ],
                            "citations": {},
                            "summary": "",
                            "analyst_reasoning": [],
                        }
                        cards_for_db.append(card)
                        card_citations = []
                        event_writer(
                            {"name": "generate_report", "card_db": card, "card_type": "section"}
                        )

            event_writer({"name": "generate_report", "report_citations": citation_url_map})
            # Build cumulative_summary from individual card summaries for executive summary generation
            cumulative_summary = "\n".join(
                card.get("summary", "") for card in cards_for_db if card.get("summary")
            )
            event_writer({"name": "generate_report", "report_summary": cumulative_summary})

            try:
                if not cumulative_summary:
                    logger.warning(
                        f"Invalid cumulative summary for executive summary generation for user: {self.user_name} - chat_id: {self.chat_id}"
                    )
                else:
                    # Old approach: refine_cumulative_summary(cumulative_summary) - took a string
                    # New approach: pass cards_for_db directly, function extracts summaries internally
                    executive_summary = await asyncio.to_thread(
                        refine_cumulative_summary,
                        cards_for_db,
                        chat_id=self.chat_id,
                        user_id=self.user_id or self.user_name,
                    )

                logger.info(
                    f"Executive summary generated successfully for user: {self.user_name} - chat_id: {self.chat_id}"
                )
            except Exception as e:
                logger.error(
                    f"Error generating executive summary: {e!s} for user: {self.user_name} - chat_id: {self.chat_id}"
                )
                executive_summary = None
            # cards_for_db.insert(3, {'executive_summary': f"Executive Summary\n\n{executive_summary}"})
            if executive_summary:
                cards_for_db.insert(
                    3,
                    {
                        "section": [
                            {
                                "name": "executive_summary",
                                "content": executive_summary,
                                "tables": [
                                    {
                                        "visualization": "",
                                        "table_id": "",
                                        "table_title": "",
                                        "visualization_type": "",
                                    }
                                ],
                                "id": str(uuid7()),
                                "summary": "",
                            }
                        ],
                        "sub_sections": [
                            {
                                "name": "",
                                "content": "",
                                "tables": [
                                    {
                                        "visualization": "",
                                        "table_id": "",
                                        "table_title": "",
                                        "visualization_type": "",
                                    }
                                ],
                                "id": str(uuid7()),
                                "summary": "",
                            }
                        ],
                        "citations": {},
                        "summary": "",
                    },
                )
                event_writer(
                    {
                        "name": "generate_report",
                        "card_db": {
                            "section": [{"name": "executive_summary", "content": executive_summary}]
                        },
                        "card_type": "es",
                    }
                )
            event_writer(
                {"name": "generate_report", "table_and_table_id_map": table_and_table_id_map}
            )
            event_writer({"name": "generate_report", "status": "card_stream_complete"})
            logger.info(
                f"Converting cards_for_db to markdown for user: {self.user_name} - chat_id: {self.chat_id}"
            )
            md_content = await asyncio.to_thread(
                convert_json_to_md, cards_for_db, table_and_table_id_map
            )
            # logger.info(f"Markdown content generated successfully for user: {self.user_name} - chat_id: {self.chat_id}")
            # Upload cards_for_db to S3 as background task
            asyncio.create_task(
                self._upload_cards_for_db_to_s3_background(cards_for_db, table_and_table_id_map)
            )
            event_writer(
                {"name": "generate_report", "status": "md_content", "md_content": md_content}
            )
            return {"messages": [AIMessage(content=md_content)]}
            # return {"messages": [AIMessage(content=cumulative_summary)]}
            # try:
            #     image_url, image_data = generate_image_with_Google(cards_for_db['section'][0]['content'], self.user_name, self.user_id, self.report_id)
            #     image_name = f"{self.report_title}_{self.report_generation_time.strftime('%Y%m%d_%H%M%S')}.png"
            #     image_data.save(os.path.join(os.getcwd(), "temp", image_name))
            #     prefix = build_report_s3_prefix(user_id=self.user_id, user_name=self.user_name, chat_id=self.chat_id, chat_title=self.chat_title, report_id=self.report_id, version=self.version, when=self.report_generation_time)
            #     image_s3_key = f"{prefix}/report/{image_name}"
            #     poster_image_s3_path = self.s3_instance.upload_file(os.path.join(os.getcwd(), "temp", image_name), image_s3_key)
            #     event_writer({"name": "generate_report", "poster_image_s3_path": poster_image_s3_path})
            #     logger.info(f"Poster image uploaded successfully to S3: {image_s3_key} for user: {self.user_name} - chat_id: {self.chat_id}")
            # except Exception as e:
            #     image_url, image_data = generate_image_with_openai(cards_for_db['section'][0]['content'], self.user_name, self.user_id, self.report_id)
            #     image_name = f"{self.report_title}_{self.report_generation_time.strftime('%Y%m%d_%H%M%S')}.png"
            #     image_data.save(os.path.join(os.getcwd(), "temp", image_name))
            #     prefix = build_report_s3_prefix(user_id=self.user_id, user_name=self.user_name, chat_id=self.chat_id, chat_title=self.chat_title, report_id=self.report_id, version=self.version, when=self.report_generation_time)
            #     image_s3_key = f"{prefix}/report/{image_name}"
            #     poster_image_s3_path = self.s3_instance.upload_file(os.path.join(os.getcwd(), "temp", image_name), image_s3_key)
            #     event_writer({"name": "generate_report", "poster_image_s3_path": poster_image_s3_path})
            #     logger.info(f"Poster image uploaded successfully to S3: {image_s3_key} for user: {self.user_name} - chat_id: {self.chat_id}")
            # finally:
            #     os.remove(os.path.join(os.getcwd(), "temp", image_name))
            # return {"messages": [AIMessage(content=final_report, usage_metadata=response.usage_metadata)]}

        except Exception as e:
            logger.error(
                f"Error in generate_report: {e} for user: {self.user_name} - chat_id: {self.chat_id}"
            )
            error_msg = AIMessage(
                content="I apologize, but I encountered an error while generating the report. Please try again."
            )
            event_writer({"name": "generate_report", "error": e})
            return {"messages": [error_msg]}

    async def run_primary_research(self, state: MessagesState) -> dict[str, list]:
        """Wrapper that bridges MessagesState -> PrimaryResearchState, runs the
        PR subgraph, and returns the result as MessagesState."""
        try:
            event_writer = get_stream_writer()

            tool_messages = self._get_recent_tool_messages(state["messages"])
            if not tool_messages:
                logger.error(
                    f"[primary_research] No retrieve tool message found for user: {self.user_name} - chat_id: {self.chat_id}"
                )
                error_msg = AIMessage(
                    content="I apologize, but I encountered an error while generating the report. Please try again."
                )
                event_writer({"name": "generate_report", "error": "No retrieve tool message"})
                return {"messages": [error_msg]}

            pr_initial_state = {
                "upload_file_config": self.upload_file_config,
                "grep_session": self.grep_session,
                "user_instructions": self.retrieve_config.get("user_instructions", ""),
                "report_layout": self.retrieve_config.get("report_layout", ""),
                "report_length": self.retrieve_config.get("report_type", "study").upper(),
                "report_type": self.retrieve_config.get("report_type", "study"),
                "report_title": self.retrieve_config.get("report_title", ""),
                "report_language": self.retrieve_config.get("report_language", "English"),
                "user_name": self.user_name,
                "chat_id": self.chat_id,
                "user_id": self.user_id or self.user_name,
                "web_search": False,
                "s3_instance": self.s3_instance,
            }

            logger.info(
                f"[primary_research] Invoking PR subgraph | "
                f"report_title='{pr_initial_state['report_title']}' | "
                f"report_type='{pr_initial_state['report_length']}' | "
                f"report_language='{pr_initial_state['report_language']}' | "
                f"has_upload_config={bool(self.upload_file_config)} | "
                f"file_ids={self.upload_file_config.get('file_ids') if self.upload_file_config else None} | "
                f"web_search=False | "
                f"user: {self.user_name} - chat_id: {self.chat_id}"
            )

            # Stream the subgraph instead of invoking it so that custom events
            # emitted via get_stream_writer() inside subgraph nodes are forwarded
            # to the parent graph's stream. Without this, LangGraph isolates the
            # subgraph's writer and the frontend would see no progress events
            # between `domain_name` and the final report.
            final_state: dict = {}
            async for sub_mode, sub_data in self._pr_subgraph.astream(
                pr_initial_state,
                stream_mode=["values", "custom"],
            ):
                if sub_mode == "custom":
                    event_writer(sub_data)
                elif sub_mode == "values":
                    final_state = sub_data

            if final_state.get("error") and not final_state.get("final_message"):
                error_text = final_state["error"]
                logger.info(
                    f"[primary_research] Subgraph completed with error: {error_text} | "
                    f"user: {self.user_name} - chat_id: {self.chat_id}"
                )
                event_writer({"name": "generate_report", "status": "card_stream_complete"})
                event_writer(
                    {"name": "generate_report", "status": "md_content", "md_content": error_text}
                )
                return {"messages": [AIMessage(content=error_text)]}

            ai_message = final_state.get("final_message")
            if ai_message:
                logger.info(
                    f"[primary_research] Subgraph completed successfully | "
                    f"output_length={len(ai_message.content)} chars | "
                    f"user: {self.user_name} - chat_id: {self.chat_id}"
                )
                return {"messages": [ai_message]}

            logger.info(
                f"[primary_research] Subgraph returned no final_message and no error | "
                f"user: {self.user_name} - chat_id: {self.chat_id}"
            )
            error_msg = AIMessage(
                content="I apologize, but I encountered an error while generating the report. Please try again."
            )
            event_writer({"name": "generate_report", "error": "No final message from PR subgraph"})
            event_writer({"name": "generate_report", "status": "card_stream_complete"})
            event_writer(
                {"name": "generate_report", "status": "md_content", "md_content": error_msg.content}
            )
            return {"messages": [error_msg]}

        except Exception as e:
            logger.error(
                f"[primary_research] Error in run_primary_research: {e} for user: {self.user_name} - chat_id: {self.chat_id}"
            )
            event_writer = get_stream_writer()
            event_writer({"name": "generate_report", "error": str(e)})
            event_writer({"name": "generate_report", "status": "card_stream_complete"})
            error_msg = AIMessage(
                content="I apologize, but I encountered an error while generating the report. Please try again."
            )
            event_writer(
                {"name": "generate_report", "status": "md_content", "md_content": error_msg.content}
            )
            return {"messages": [error_msg]}

    async def run_due_diligence(self, state: MessagesState) -> dict[str, list]:
        """Wrapper that bridges MessagesState -> DueDiligenceState, runs the
        DD subgraph, and returns the result as MessagesState."""
        try:
            event_writer = get_stream_writer()

            tool_messages = self._get_recent_tool_messages(state["messages"])
            if not tool_messages:
                logger.error(
                    f"[due_diligence] No retrieve tool message found for user: {self.user_name} - chat_id: {self.chat_id}"
                )
                error_msg = AIMessage(
                    content="I apologize, but I encountered an error while generating the report. Please try again."
                )
                event_writer({"name": "generate_report", "error": "No retrieve tool message"})
                return {"messages": [error_msg]}

            dd_initial_state = {
                "upload_file_config": self.upload_file_config,
                "grep_session": self.grep_session,
                "user_instructions": self.retrieve_config.get("user_instructions", ""),
                "report_layout": self.retrieve_config.get("report_layout", ""),
                "report_length": self.retrieve_config.get("report_type", "study").upper(),
                "report_type": self.retrieve_config.get("report_type", "study"),
                "report_title": self.retrieve_config.get("report_title", ""),
                "report_language": self.retrieve_config.get("report_language", "English"),
                "user_name": self.user_name,
                "chat_id": self.chat_id,
                "user_id": self.user_id or self.user_name,
                "web_search": True,
                "entity_name": self.retrieve_config.get("report_title", ""),
                "s3_instance": self.s3_instance,
                "mcp_server_url": self.mcp_server_url,
            }

            logger.info(
                f"[due_diligence] Invoking DD subgraph | "
                f"entity_name='{dd_initial_state['entity_name']}' | "
                f"report_title='{dd_initial_state['report_title']}' | "
                f"report_type='{dd_initial_state['report_length']}' | "
                f"report_language='{dd_initial_state['report_language']}' | "
                f"has_upload_config={bool(self.upload_file_config)} | "
                f"file_ids={self.upload_file_config.get('file_ids') if self.upload_file_config else None} | "
                f"web_search=True | "
                f"user: {self.user_name} - chat_id: {self.chat_id}"
            )

            # Stream the subgraph instead of invoking it so that custom events
            # emitted via get_stream_writer() inside subgraph nodes are forwarded
            # to the parent graph's stream. Without this, LangGraph isolates the
            # subgraph's writer and the frontend would see no progress events
            # between `domain_name` and the final report.
            final_state: dict = {}
            async for sub_mode, sub_data in self._dd_subgraph.astream(
                dd_initial_state,
                stream_mode=["values", "custom"],
            ):
                if sub_mode == "custom":
                    event_writer(sub_data)
                elif sub_mode == "values":
                    final_state = sub_data

            if final_state.get("error") and not final_state.get("final_message"):
                error_text = final_state["error"]
                logger.info(
                    f"[due_diligence] Subgraph completed with error: {error_text} | "
                    f"user: {self.user_name} - chat_id: {self.chat_id}"
                )
                event_writer({"name": "generate_report", "status": "card_stream_complete"})
                event_writer(
                    {"name": "generate_report", "status": "md_content", "md_content": error_text}
                )
                return {"messages": [AIMessage(content=error_text)]}

            ai_message = final_state.get("final_message")
            if ai_message:
                logger.info(
                    f"[due_diligence] Subgraph completed successfully | "
                    f"output_length={len(ai_message.content)} chars | "
                    f"user: {self.user_name} - chat_id: {self.chat_id}"
                )
                return {"messages": [ai_message]}

            logger.info(
                f"[due_diligence] Subgraph returned no final_message and no error | "
                f"user: {self.user_name} - chat_id: {self.chat_id}"
            )
            error_msg = AIMessage(
                content="I apologize, but I encountered an error while generating the report. Please try again."
            )
            event_writer({"name": "generate_report", "error": "No final message from DD subgraph"})
            event_writer({"name": "generate_report", "status": "card_stream_complete"})
            event_writer(
                {"name": "generate_report", "status": "md_content", "md_content": error_msg.content}
            )
            return {"messages": [error_msg]}

        except Exception as e:
            logger.error(
                f"[due_diligence] Error in run_due_diligence: {e} for user: {self.user_name} - chat_id: {self.chat_id}"
            )
            event_writer = get_stream_writer()
            event_writer({"name": "generate_report", "error": str(e)})
            event_writer({"name": "generate_report", "status": "card_stream_complete"})
            error_msg = AIMessage(
                content="I apologize, but I encountered an error while generating the report. Please try again."
            )
            event_writer(
                {"name": "generate_report", "status": "md_content", "md_content": error_msg.content}
            )
            return {"messages": [error_msg]}
