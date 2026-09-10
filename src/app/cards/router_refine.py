"""cards routes: refine.

Split out of ``app/cards/router.py`` to keep each router file under
the ~400-line ceiling in R-STRUCT-3. Handlers are unchanged.
"""

"""HTTP routes for the cards bounded context.

Handlers moved verbatim from ``src/resources/routers/api.py`` during the
R-STRUCT-1 migration, which split that 10,257-line module across seven
contexts. Handler bodies are unchanged; only the import block and the
``APIRouter`` they attach to are new.
"""

"""api.py: Authentication API for the Casper backend"""
import asyncio
from datetime import UTC, datetime

from fastapi import APIRouter, Depends
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from sqlalchemy import select
from uuid_utils import uuid7

from app.adapters.grep_agent_2 import get_or_create_grep_session
from app.adapters.openai_files import ensure_report_file_id
from app.adapters.s3 import (
    replace_visualization_uris_in_card,
)
from app.admin.repository_web_search import log_web_search_event
from app.auth.repository import (
    check_user_by_id,
    get_user_details,
)
from app.auth.schemas import ErrorResponse
from app.auth.token import get_current_active_user
from app.cards.repository import (
    delete_card_or_subsection,
    get_latest_card_version,
    get_refinement_history,
    get_report_in_cards_format,
    revert_card_to_version,
)
from app.cards.schemas import (
    RefineOrDeleteRequest,
    RefineOrDeleteResponse,
    RevertCardRequest,
    RevertCardResponse,
)
from app.cards.service_cards import generate_section_summary
from app.cards.service_remove import extract_toc_after_delete
from app.core.db import async_session_scope
from app.core.enums import RefinementType, ReportStatus
from app.core.logging import setup_logging
from app.core.redis import get_redis_instance
from app.internal.repository_grep import get_chat_uploaded_file_ids
from app.models import Card, Table
from app.observability.functionality_context import Functionality, set_functionality
from app.observability.web_search_analytics import (
    enrich_terminal_search_analytics,
    log_scheduled_analytics_batch,
    search_analytics_schedule_kwargs,
)
from app.reports.repository import (
    get_report_details,
    update_report_status_if_needed,
)
from app.research.refine.refine_persist import THREAD_POLICY_RESET, persist_refined_card
from app.research.refine.refiner import refine_card

router = APIRouter()
logger = setup_logging(__file__)
redis_instance = get_redis_instance()


@router.post(
    "/refine-card",
    response_model=RefineOrDeleteResponse,
    status_code=200,
    responses={
        404: {"model": ErrorResponse},
        401: {"model": ErrorResponse},
        422: {"model": ErrorResponse},
        500: {"model": ErrorResponse},
    },
)
async def refine_content(
    data: RefineOrDeleteRequest, user_id: str = Depends(get_current_active_user)
):
    """Refine a card from the report"""
    set_functionality(Functionality.REFINE)
    logger.info(f"Refine card request received for user_id: {user_id}")

    try:
        # Validate input data
        if not data.report_id:
            logger.error("Missing required field: report_id")
            return JSONResponse(
                status_code=422,
                content={"success": False, "error": "Missing required field: report_id"},
            )

        if not data.card:
            logger.error("Missing required field: card")
            return JSONResponse(
                status_code=422, content={"success": False, "error": "Missing required field: card"}
            )

        report_id = data.report_id
        fe_input_data = data.card
        card_id = fe_input_data.get("id")

        async with async_session_scope() as session:
            db_response = await get_latest_card_version(session=session, card_id=card_id)
            if not db_response.get("success", False):
                logger.error(
                    f"Failed to get latest card version: {db_response.get('error')} for card_id: {card_id}"
                )
                return JSONResponse(
                    status_code=500,
                    content={
                        "success": False,
                        "error": "Something went wrong while I was refining your content. Let's give it another try.",
                    },
                )
            current_card_version = db_response.get("version", None)

            # Check if card exists
            if current_card_version is None:
                logger.error(f"Card with ID {card_id} not found in the database")
                return JSONResponse(
                    status_code=404,
                    content={"success": False, "error": f"Card with ID {card_id} not found"},
                )

        async with async_session_scope() as session:
            db_response = await get_report_details(report_id=report_id, session=session)
            if not db_response.get("success", False) or not db_response.get("report"):
                if not db_response.get("report"):
                    logger.warning(f"Report not found: {report_id}")
                    return JSONResponse(
                        status_code=404,
                        content={
                            "success": False,
                            "error": "Report not found. Please check the report ID and try again.",
                        },
                    )
                logger.error(
                    f"Failed to get report details: {db_response.get('error')} for report_id: {report_id}"
                )
                return JSONResponse(
                    status_code=500,
                    content={
                        "success": False,
                        "error": "Something went wrong while I was refining your content. Let's give it another try.",
                    },
                )
            report_data = db_response.get("report", {})
            chat_id = report_data.get("chat_id")
            report_type = report_data.get("report_type") or "study"
            initial_markdown_s3_path = report_data.get("initial_markdown")
            report_file_id = report_data.get("file_id")
            logger.info(
                f"Report data fetched for refine-card | report_id={report_id} | file_id={report_file_id} | has_s3_path={bool(initial_markdown_s3_path)}"
            )

        # Resolve the OpenAI file_id for the report's initial markdown so the refiner always has
        # full-report context. grep only supplies the user's uploaded reference documents.
        report_file_id = await ensure_report_file_id(
            report_id=report_id,
            file_id=report_file_id,
            file_s3_path=initial_markdown_s3_path,
            log_prefix="REFINE_CARD",
        )
        if not report_file_id:
            # Degrade rather than fail: the refinement still uses the current card content plus
            # any grep-backed reference documents.
            logger.error(
                f"Refining without full report file context | report_id={report_id} "
                f"| has_s3_path={bool(initial_markdown_s3_path)}"
            )

        # Check if user exists and has access to this report
        async with async_session_scope() as session:
            db_response = await check_user_by_id(user_id=user_id, session=session)
            if not db_response.get("success", False):
                logger.error(f"Failed to check user by id: {db_response.get('error')}")
                return JSONResponse(
                    status_code=500,
                    content={
                        "success": False,
                        "error": "Something went wrong while I was refining your content. Let's give it another try.",
                    },
                )
            if not db_response.get("exists", False):
                logger.error(f"User with ID {user_id} not found or token is invalid")
                return JSONResponse(
                    status_code=401, content={"success": False, "error": "Unauthorized request"}
                )

        # Get user details for refine_card function
        async with async_session_scope() as session:
            user_details_response = await get_user_details(user_id=user_id, session=session)
            if not user_details_response.get("success", False):
                logger.error(f"Failed to get user details: {user_details_response.get('error')}")
                return JSONResponse(
                    status_code=500,
                    content={
                        "success": False,
                        "error": "Something went wrong while I was refining your content. Let's give it another try.",
                    },
                )

            user_name = user_details_response.get("user", {}).get("user_name", "User")

            # Get user subscription plan
            # user_plan = await get_active_subscription(user_id=user_id, session=session)
            # logger.info(f"User {user_id} subscription plan for card refinement: {user_plan}")

            # ── Build grep_session from chat reference files ──
            grep_session = None
            try:
                uploaded_file_ids = await get_chat_uploaded_file_ids(chat_id, session)
                if uploaded_file_ids:
                    grep_session = await get_or_create_grep_session(uploaded_file_ids, session)
                    logger.info(
                        f"Built grep_session for refine-card: "
                        f"chat_id={chat_id}, docs={len(grep_session.labels) if grep_session else 0}"
                    )
                else:
                    logger.info(f"No reference files found for chat_id={chat_id}")
            except Exception as gs_err:
                logger.warning(
                    f"Could not build grep_session for chat_id={chat_id}, "
                    f"proceeding without reference files: {gs_err}",
                    exc_info=True,
                )

            db_response = await get_report_in_cards_format(report_id=report_id, session=session)
            if not db_response.get("success", False):
                logger.error(
                    f"Failed to get report in cards format: {db_response.get('error')} for report_id: {report_id}"
                )
                return JSONResponse(
                    status_code=500,
                    content={
                        "success": False,
                        "error": "Something went wrong while I was refining your content. Let's give it another try.",
                    },
                )
            cards_for_db = db_response.get("cards", [])
            if not cards_for_db:
                logger.error(f"No cards found for report_id: {report_id}")
                return JSONResponse(
                    status_code=404,
                    content={"success": False, "error": "No cards found for this report"},
                )

            # Fetch existing refinement history from DB
            refinement_history_response = await get_refinement_history(
                report_id=report_id, session=session
            )
            existing_refinement_history = (
                refinement_history_response.get("refine_history", [])
                if refinement_history_response.get("success")
                else []
            )
            logger.info(
                f"Fetched {len(existing_refinement_history)} existing refinement history entries for report_id: {report_id}"
            )

            # Check if we have refinement instructions
            user_instruction = fe_input_data.get("user_instruction")
            subsection_instruction = (
                None
                if user_instruction
                else fe_input_data.get("subsection", {}).get("user_instruction")
            )

            if user_instruction or subsection_instruction:
                # Create the format expected by refine.py
                refine_data = {
                    "id": fe_input_data.get("id"),  # Use the id from the card data
                    "refine_or_delete_prompt": user_instruction if user_instruction else None,
                }

                if subsection_instruction:
                    # Add subsection data only if section-level instruction is not present
                    refine_data["subsection"] = {
                        "id": fe_input_data.get("subsection", {}).get("id"),
                        "refine_or_delete_prompt": subsection_instruction,
                    }

                logger.info(
                    f"Calling refine_card for report_id={report_id}, chat_id={chat_id}, card_id={fe_input_data.get('id')}: "
                    f"grep_session={'present' if grep_session else 'None'}, "
                    f"docs={len(grep_session.labels) if grep_session else 0}, "
                    f"report_file={'present' if report_file_id else 'None'}"
                )
                # Web-search analytics: collected inside the sync refine_card call
                # (in-memory only, no I/O) and scheduled for a non-blocking DB
                # write via asyncio.create_task below, after the threadpool call
                # returns. Never scheduled from sync code.
                analytics_collector: list[dict] = []
                analytics_operation_id = str(uuid7())
                (
                    updated_cards,
                    updated_card,
                    new_table_map,
                    updated_refinement_history,
                ) = await run_in_threadpool(
                    refine_card,
                    cards_for_db=cards_for_db,
                    fe_json_for_refine=refine_data,
                    user_name=user_name,
                    chat_id=chat_id,
                    latest_version=current_card_version,
                    refinement_history=existing_refinement_history,
                    file_id=report_file_id,
                    grep_session=grep_session,
                    report_type=report_type,
                    user_id=user_id,
                    analytics_collector=analytics_collector,
                    analytics_operation_id=analytics_operation_id,
                )

                # Save the refined card to database
                if updated_card:
                    # Get subsection_id if this is a subsection refinement
                    subsection_id = (
                        None if user_instruction else fe_input_data.get("subsection", {}).get("id")
                    )
                    section_id = fe_input_data.get("id")  # business card_id (section_id)

                    db_response = await persist_refined_card(
                        session=session,
                        report_id=report_id,
                        updated_card=updated_card,
                        user_instruction=user_instruction or subsection_instruction,
                        refinement_type=RefinementType.REFINE_SECTION.value
                        if user_instruction
                        else RefinementType.REFINE_SUBSECTION.value,
                        table_id_markdown_map=new_table_map,
                        subsection_id=subsection_id,
                        updated_refinement_history=updated_refinement_history,
                        section_id=section_id,
                        thread_policy=THREAD_POLICY_RESET,
                    )

                    if not db_response.get("success", False):
                        logger.error(
                            f"Failed to save refined card to database: {db_response.get('error')}"
                        )
                        return JSONResponse(
                            status_code=500,
                            content={
                                "success": False,
                                "error": "Something went wrong while I was refining your content. Let's give it another try.",
                            },
                        )

                    # Persist web-search analytics collected during refine_card, if any.
                    # Enrichment scans the final refined card content for URLs actually
                    # retained in output, then schedules each collected entry for a
                    # non-blocking DB write via asyncio.create_task. All steps are
                    # wrapped so analytics can never affect the refine response.
                    try:
                        if analytics_collector:
                            enrich_terminal_search_analytics(analytics_collector, updated_card)
                            section_name = None
                            if updated_card.get("section"):
                                section_name = updated_card["section"][0].get("name")
                            if not section_name and updated_card.get("sub_sections"):
                                section_name = updated_card["sub_sections"][0].get("name")
                            log_scheduled_analytics_batch(
                                "card_refinement",
                                analytics_collector,
                                operation_id=analytics_operation_id,
                                section_name=section_name,
                                card_id=fe_input_data.get("id"),
                            )
                            for analytics_entry in analytics_collector:
                                try:
                                    event = search_analytics_schedule_kwargs(analytics_entry)
                                    event.update(
                                        {
                                            "trigger_source": "card_refinement",
                                            "user_query": user_instruction
                                            or subsection_instruction,
                                            "user_id": user_id,
                                            "chat_id": chat_id,
                                            "report_id": report_id,
                                            "section_name": section_name,
                                            "card_id": fe_input_data.get("id"),
                                        }
                                    )
                                    asyncio.create_task(log_web_search_event(**event))
                                except Exception:
                                    logger.exception(
                                        "[card_refinement] Failed to schedule non-fatal "
                                        "search analytics"
                                    )
                    except Exception:
                        logger.exception(
                            "[card_refinement] Failed to enrich non-fatal terminal search analytics"
                        )

                def _normalize_table_title_for_response(value):
                    if value is None:
                        return ""
                    if isinstance(value, (tuple, list)):
                        return _normalize_table_title_for_response(value[0] if value else "")
                    if isinstance(value, str):
                        return value
                    return str(value)

                # Keep refine API contract stable: table_title must be a string.
                for section in updated_card.get("section", []) or []:
                    for table in section.get("tables", []) or []:
                        table["table_title"] = _normalize_table_title_for_response(
                            table.get("table_title", "")
                        )

                for subsection in updated_card.get("sub_sections", []) or []:
                    for table in subsection.get("tables", []) or []:
                        table["table_title"] = _normalize_table_title_for_response(
                            table.get("table_title", "")
                        )

                await replace_visualization_uris_in_card(updated_card, redis_instance.redis_client)
                return JSONResponse(
                    status_code=200,
                    content={
                        "success": True,
                        "message": "Card refined successfully",
                        "refine_card": updated_card,
                    },
                )
            else:
                logger.error(f"No user instruction found for card_id: {fe_input_data.get('id')}")
                return JSONResponse(
                    status_code=422,
                    content={"success": False, "error": "No user instruction found for card"},
                )

    except Exception as e:
        logger.error(f"Error in refine_card: {e}")
        return JSONResponse(
            status_code=500,
            content={
                "success": False,
                "error": "Something went wrong while I was refining your content. Let's give it another try.",
            },
        )


@router.post(
    "/revert-card",
    response_model=RevertCardResponse,
    status_code=200,
    responses={
        404: {"model": ErrorResponse},
        401: {"model": ErrorResponse},
        422: {"model": ErrorResponse},
        500: {"model": ErrorResponse},
    },
)
async def revert_card(data: RevertCardRequest, user_id: str = Depends(get_current_active_user)):
    """
    Revert a card to its immediate previous version (undo last refinement).

    User can only undo to immediate previous version (v3 → v2, not v3 → v1).
    Current version is marked as deleted and previous version is reactivated.
    """
    logger.info(f"Revert card request received for user_id: {user_id}, card_id: {data.card_id}")

    try:
        # Validate input data
        if not data.report_id:
            logger.error("Missing required field: report_id")
            return JSONResponse(
                status_code=422,
                content={"success": False, "error": "Missing required field: report_id"},
            )

        if not data.card_id:
            logger.error("Missing required field: card_id")
            return JSONResponse(
                status_code=422,
                content={"success": False, "error": "Missing required field: card_id"},
            )

        # Check if user exists and has access to this report
        async with async_session_scope() as session:
            db_response = await check_user_by_id(user_id=user_id, session=session)
            if not db_response.get("success", False):
                logger.error(f"Failed to check user by id: {db_response.get('error')}")
                return JSONResponse(
                    status_code=500,
                    content={
                        "success": False,
                        "error": "Something went wrong while reverting your card. Please try again.",
                    },
                )
            if not db_response.get("exists", False):
                logger.error(f"User with ID {user_id} not found or token is invalid")
                return JSONResponse(
                    status_code=401, content={"success": False, "error": "Unauthorized request"}
                )

            # Revert the card to immediate previous version
            revert_response = await revert_card_to_version(
                session=session, report_id=data.report_id, card_id=data.card_id
            )

            if not revert_response.get("success"):
                logger.error(f"Failed to revert card: {revert_response.get('error')}")
                return JSONResponse(
                    status_code=400,
                    content={
                        "success": False,
                        "error": revert_response.get("error", "Failed to revert card"),
                    },
                )

            # Update report status to REDO_ANALYSIS since card was reverted
            await update_report_status_if_needed(
                report_id=data.report_id,
                new_status=ReportStatus.REDO_ANALYSIS.value,
                session=session,
            )
            logger.info(f"Set report status to REDO_ANALYSIS after reverting card {data.card_id}")

            reverted_card_data = revert_response.get("reverted_card")
            if reverted_card_data:
                await replace_visualization_uris_in_card(
                    reverted_card_data, redis_instance.redis_client
                )

            return RevertCardResponse(
                success=True,
                message=revert_response.get("message"),
                reverted_to_version=revert_response.get("reverted_to_version"),
                card_id=revert_response.get("card_id"),
                primary_card_id=revert_response.get("primary_card_id"),
                deleted_versions=revert_response.get("deleted_versions"),
                reverted_card=reverted_card_data,
            )

    except Exception as e:
        logger.error(f"Unexpected error in revert_card: {e!s}", exc_info=True)
        return JSONResponse(
            status_code=500,
            content={
                "success": False,
                "error": "Something unexpected happened while reverting your card. Please try again.",
            },
        )


@router.post(
    "/delete-card",
    response_model=RefineOrDeleteResponse,
    status_code=200,
    responses={
        404: {"model": ErrorResponse},
        401: {"model": ErrorResponse},
        422: {"model": ErrorResponse},
        500: {"model": ErrorResponse},
    },
)
async def delete_card(data: RefineOrDeleteRequest, user_id: str = Depends(get_current_active_user)):
    """Delete a card or subsection from the report"""
    logger.info(f"Delete card request received for user_id: {user_id}")

    try:
        # Validate input data
        if not data.report_id:
            logger.error("Missing required field: report_id")
            return JSONResponse(
                status_code=422,
                content={"success": False, "error": "Missing required field: report_id"},
            )

        if not data.card:
            logger.error("Missing required field: card")
            return JSONResponse(
                status_code=422, content={"success": False, "error": "Missing required field: card"}
            )

        report_id = data.report_id
        fe_input_data = data.card

        # Check if user exists and has access to this report
        async with async_session_scope() as session:
            db_response = await check_user_by_id(user_id=user_id, session=session)
            if not db_response.get("success", False):
                logger.error(f"Failed to check user by id: {db_response.get('error')}")
                return JSONResponse(
                    status_code=500,
                    content={
                        "success": False,
                        "error": "Something unexpected went wrong while deleting that. Please try it again.",
                    },
                )
            if not db_response.get("exists", False):
                logger.error(f"User with ID {user_id} not found or token is invalid")
                return JSONResponse(
                    status_code=401, content={"success": False, "error": "Unauthorized request"}
                )

        async with async_session_scope() as session:
            db_response = await get_report_in_cards_format(report_id=report_id, session=session)
            if not db_response.get("success", False):
                logger.error(
                    f"Failed to get report in cards format: {db_response.get('error')} for report_id: {report_id}"
                )
                return JSONResponse(
                    status_code=500,
                    content={
                        "success": False,
                        "error": "Something unexpected went wrong while deleting that. Please try it again.",
                    },
                )
            cards_for_db = db_response.get("cards", [])
            if not cards_for_db:
                logger.error(f"No cards found for report_id: {report_id}")
                return JSONResponse(
                    status_code=404,
                    content={
                        "success": False,
                        "error": "I can't find the content you're trying to delete. It might have been removed already.",
                    },
                )

            # Check if we have deletion request
            section_id = fe_input_data.get("id")
            subsection_id = fe_input_data.get("subsection")

        updated_toc = await run_in_threadpool(extract_toc_after_delete, cards_for_db, fe_input_data)

        async with async_session_scope() as session:
            db_response = await delete_card_or_subsection(
                session=session, card_id=section_id, subsection_id=subsection_id, toc=updated_toc
            )
            if not db_response.get("success", False):
                logger.error(
                    f"Failed to delete card or subsection: {db_response.get('error')} for report_id: {report_id}"
                )
                return JSONResponse(
                    status_code=500,
                    content={
                        "success": False,
                        "error": "Something unexpected went wrong while deleting that. Please try it again.",
                    },
                )

            # Set status to REDO_ANALYSIS after deleting a card
            await update_report_status_if_needed(
                report_id=report_id, new_status=ReportStatus.REDO_ANALYSIS.value, session=session
            )
            logger.info(
                f"Set report status to REDO_ANALYSIS after card deletion for report_id: {report_id}"
            )

        return JSONResponse(
            status_code=200,
            content={"success": True, "message": "Card or subsection deleted successfully"},
        )

    except Exception as e:
        logger.error(f"Error in delete_card: {e}")
        return JSONResponse(
            status_code=500,
            content={
                "success": False,
                "error": "Something unexpected went wrong while deleting that. Please try it again.",
            },
        )


def extract_table_markdown_from_card_content(
    content_dict: dict, sub_sections: list, target_table_id: str
) -> str | None:
    """
    Extract table markdown from card's content or sub_sections for a specific table_id.

    Args:
        content_dict: The card's content dictionary
        sub_sections: The card's sub_sections list
        target_table_id: The table_id to find

    Returns:
        The markdown table string if found, None otherwise
    """
    from app.cards.service_cards import extract_markdown_tables

    # Check section-level content
    if isinstance(content_dict, dict):
        section_content = content_dict.get("content", "")

        # Handle nested content structure
        if isinstance(section_content, dict):
            section_content = section_content.get("content", "")

        if isinstance(section_content, str) and section_content:
            # Check if this section has the target table_id
            section_tables = content_dict.get("tables", [])
            for idx, table_ref in enumerate(section_tables):
                if table_ref.get("table_id") == target_table_id:
                    # Extract all tables from content
                    extracted_tables = extract_markdown_tables(section_content)
                    if extracted_tables:
                        # Return table at matching position
                        if idx < len(extracted_tables):
                            return extracted_tables[idx]
                        else:
                            # Fallback if position mismatch
                            logger.warning(
                                f"Position mismatch for table {target_table_id}: idx={idx}, extracted={len(extracted_tables)}"
                            )
                            return extracted_tables[0] if len(extracted_tables) > 0 else None
                    return None

    # Check subsection-level content
    if sub_sections:
        for subsection in sub_sections:
            if isinstance(subsection, dict):
                sub_content = subsection.get("content", "")

                # Handle nested content structure
                if isinstance(sub_content, dict):
                    sub_content = sub_content.get("content", "")

                if isinstance(sub_content, str) and sub_content:
                    # Check if this subsection has the target table_id
                    sub_tables = subsection.get("tables", [])
                    for idx, table_ref in enumerate(sub_tables):
                        if table_ref.get("table_id") == target_table_id:
                            # Extract all tables from subsection content
                            extracted_tables = extract_markdown_tables(sub_content)
                            if extracted_tables:
                                # Return table at matching position
                                if idx < len(extracted_tables):
                                    return extracted_tables[idx]
                                else:
                                    # Fallback if position mismatch
                                    logger.warning(
                                        f"Position mismatch for table {target_table_id} in subsection: idx={idx}, extracted={len(extracted_tables)}"
                                    )
                                    return (
                                        extracted_tables[0] if len(extracted_tables) > 0 else None
                                    )
                            return None

    return None


@router.post(
    "/edit-card-content",
    response_model=RefineOrDeleteResponse,
    status_code=200,
    responses={
        404: {"model": ErrorResponse},
        401: {"model": ErrorResponse},
        422: {"model": ErrorResponse},
        500: {"model": ErrorResponse},
    },
)
async def replace_card_content(
    data: RefineOrDeleteRequest, user_id: str = Depends(get_current_active_user)
):
    """Replace section or subsection content for a particular active card"""
    logger.info(f"Replace card content request received for user_id: {user_id}")

    try:
        # Validate input data
        if not data.report_id:
            logger.error("Missing required field: report_id")
            return JSONResponse(
                status_code=422,
                content={
                    "success": False,
                    "error": "Please provide all required information for content editing.",
                },
            )

        if not data.card:
            logger.error("Missing required field: card")
            return JSONResponse(
                status_code=422,
                content={
                    "success": False,
                    "error": "Please provide all required information for content editing.",
                },
            )

        if not data.card.get("id"):
            logger.error("Missing required field: card.id")
            return JSONResponse(
                status_code=422,
                content={
                    "success": False,
                    "error": "Please provide all required information for content editing.",
                },
            )

        report_id = data.report_id
        card_id = data.card.get("id")
        subsection_data = data.card.get("subsection")

        # Check if user exists and has access to this report
        async with async_session_scope() as session:
            db_response = await check_user_by_id(user_id=user_id, session=session)
            if not db_response.get("success", False):
                logger.error(f"Failed to check user by id: {db_response.get('error')}")
                return JSONResponse(
                    status_code=500,
                    content={
                        "success": False,
                        "error": "Something went wrong while editing your content. Please try again.",
                    },
                )
            if not db_response.get("exists", False):
                logger.error(f"User with ID {user_id} not found or token is invalid")
                return JSONResponse(
                    status_code=401, content={"success": False, "error": "Unauthorized request"}
                )

            # Get all versions of the card to calculate next version number
            all_cards_stmt = (
                select(Card)
                .where(Card.card_id == card_id, Card.report_id == report_id)
                .order_by(Card.version.desc())
            )
            all_cards_result = await session.execute(all_cards_stmt)
            all_cards = all_cards_result.scalars().all()

            if not all_cards:
                logger.error(f"Card with ID {card_id} not found for report_id: {report_id}")
                return JSONResponse(
                    status_code=404,
                    content={
                        "success": False,
                        "error": "The content you're trying to edit doesn't exist or may have been deleted.",
                    },
                )

            # Get the current active card
            existing_card = next((c for c in all_cards if c.is_active), None)
            if not existing_card:
                logger.error(f"No active card with ID {card_id} found for report_id: {report_id}")
                return JSONResponse(
                    status_code=404,
                    content={
                        "success": False,
                        "error": "The content you're trying to edit doesn't exist or may have been deleted.",
                    },
                )

            # Calculate next version number
            new_version = max(card.version for card in all_cards) + 1

            # Determine if this is a section or subsection edit
            is_subsection_edit = subsection_data is not None and subsection_data.get("id")

            # Initialize subsection_id (will be set if this is a subsection edit)
            subsection_id = None

            # Prepare new content by copying existing card data
            import copy

            new_content = copy.deepcopy(existing_card.content) if existing_card.content else {}
            new_sub_sections = (
                copy.deepcopy(existing_card.sub_sections) if existing_card.sub_sections else []
            )

            if is_subsection_edit:
                new_subsection_content = subsection_data.get("content")
                subsection_id = subsection_data.get("id")

                if not subsection_id:
                    logger.error("Missing subsection ID for subsection edit")
                    return JSONResponse(
                        status_code=422,
                        content={
                            "success": False,
                            "error": "The content you're trying to edit doesn't exist or may have been deleted.",
                        },
                    )

                # Update subsection content in the copied sub_sections
                subsection_updated = False
                for subsection in new_sub_sections:
                    if isinstance(subsection, dict) and subsection.get("id") == subsection_id:
                        subsection["content"] = new_subsection_content
                        subsection_updated = True
                        break

                if not subsection_updated:
                    logger.error(f"Subsection with ID {subsection_id} not found in card {card_id}")
                    return JSONResponse(
                        status_code=404, content={"success": False, "error": "Subsection not found"}
                    )

                logger.info(
                    f"Prepared updated subsection content for card {card_id}, subsection {subsection_id}"
                )

            else:
                # Editing section content
                new_card_content = data.card.get("content")

                # Update the content in the copy
                if isinstance(new_content, dict):
                    if "content" in new_content:
                        new_content["content"] = new_card_content
                    else:
                        new_content = {
                            "name": new_content.get("name", "section"),
                            "content": new_card_content,
                            "tables": new_content.get(
                                "tables", [{"visualization": "", "table_id": "", "table_title": ""}]
                            ),
                            "id": new_content.get(
                                "id", card_id
                            ),  # Preserve original ID (same as card_id)
                        }
                else:
                    # Legacy string content - convert to new structure
                    new_content = {
                        "name": "section",
                        "content": new_card_content,
                        "tables": [{"visualization": "", "table_id": "", "table_title": ""}],
                        "id": card_id,  # Use card_id, not a new UUID
                    }

                logger.info(f"Prepared updated section content for card {card_id}")

            # Generate summary for the new content
            try:
                logger.info(f"Generating summary for edited card {card_id}")

                # Extract full section content for summary generation
                section_content = ""

                # Get section name and content from NEW content
                if new_content and isinstance(new_content, dict):
                    section_content += new_content.get("name", "") + "\n\n"

                    # Get section-level content
                    content_data = new_content.get("content", "")
                    if isinstance(content_data, dict):
                        section_content += content_data.get("content", "")
                    else:
                        section_content += str(content_data)
                    section_content += "\n\n"

                # Get all subsection content from NEW sub_sections
                if new_sub_sections:
                    for subsection in new_sub_sections:
                        if isinstance(subsection, dict):
                            section_content += subsection.get("name", "") + "\n"
                            subsection_content = subsection.get("content", "")
                            if isinstance(subsection_content, dict):
                                section_content += subsection_content.get("content", "")
                            else:
                                section_content += str(subsection_content)
                            section_content += "\n\n"

                # Generate new summary using generate_section_summary
                new_summary = await run_in_threadpool(generate_section_summary, section_content)
                logger.info(f"Successfully generated summary for edited card {card_id}")

            except Exception as e:
                logger.error(f"Error generating summary for card {card_id}: {e!s}")
                new_summary = existing_card.summary  # Fall back to old summary if generation fails

            # Deactivate ALL previous versions of this card (not just one)
            logger.info(f"Deactivating {len(all_cards)} previous versions of card_id: {card_id}")
            for card in all_cards:
                card.is_active = False

            # Create new card version with updated content
            new_card_id = str(uuid7())
            new_card = Card(
                id=new_card_id,
                card_id=card_id,  # Same business card_id
                report_id=report_id,
                title=existing_card.title,
                sequence=existing_card.sequence,
                content=new_content,
                sub_sections=new_sub_sections,
                citations=existing_card.citations if existing_card.citations else {},
                created_at=datetime.now(UTC),
                summary=new_summary,
                type=existing_card.type,
                version=new_version,
                is_active=True,
                is_deleted=False,
                changed_since_es=True,  # Mark as changed to trigger ES regeneration
                last_es_version_used=None,  # Not yet used in any ES
            )

            session.add(new_card)
            await session.flush()

            # Copy all table records from old card to new card version
            # Extract updated table markdown from new content if available
            logger.info(
                f"Copying table records from old card {existing_card.id} to new card {new_card_id}"
            )

            old_tables_stmt = select(Table).where(Table.parent_card_id == existing_card.id)
            old_tables_result = await session.execute(old_tables_stmt)
            old_tables = old_tables_result.scalars().all()

            tables_created = 0
            tables_updated = 0

            for old_table in old_tables:
                # Try to extract updated table markdown from new content
                extracted_markdown = extract_table_markdown_from_card_content(
                    new_content, new_sub_sections, old_table.table_id
                )

                # Use extracted markdown if found, otherwise fallback to old markdown
                final_markdown = (
                    extracted_markdown if extracted_markdown else old_table.table_markdown
                )

                if extracted_markdown and extracted_markdown != old_table.table_markdown:
                    logger.info(f"Table {old_table.table_id} content was updated")
                    tables_updated += 1

                new_table = Table(
                    id=str(uuid7()),
                    report_id=old_table.report_id,
                    parent_card_id=new_card_id,
                    table_id=old_table.table_id,
                    table_title=old_table.table_title,
                    table_markdown=final_markdown,
                    base64_s3_uri=old_table.base64_s3_uri,
                )
                session.add(new_table)
                tables_created += 1

            logger.info(
                f"Successfully created {tables_created} table records for new card version ({tables_updated} with updated content)"
            )

            # Create card version record for tracking
            from app.cards.repository import (
                insert_card_version,
            )

            version_result = await insert_card_version(
                session=session,
                card_id=new_card_id,  # Primary key (cards.id)
                section_id=card_id,  # Business card_id (cards.card_id)
                user_instruction="user edited manually",
                refinement_type=RefinementType.EDIT_SECTION.value,
                subsection_id=subsection_id if is_subsection_edit else None,
            )

            if not version_result.get("success"):
                logger.warning(
                    f"Failed to create card version record: {version_result.get('error')}"
                )

            # Set status to REDO_ANALYSIS after editing card content
            await update_report_status_if_needed(
                report_id=report_id, new_status=ReportStatus.REDO_ANALYSIS.value, session=session
            )
            logger.info(
                f"Set report status to REDO_ANALYSIS after editing card content for report_id: {report_id}"
            )

            # Commit the changes
            await session.commit()

            logger.info(
                f"Successfully created new version {new_version} for card {card_id} (primary key: {new_card_id})"
            )

            # NOTE: We intentionally do NOT touch ask_caspr_chats on edit.
            # The existing chat entry keeps its original card_version, and the
            # historical query (card_version <= target) naturally includes it
            # for both the old and new report versions.  Bumping card_version
            # here would break historical lookups for versions between a prior
            # refine and this edit.  See edge_case_3_edit_after_refine.md.

            return JSONResponse(
                status_code=200,
                content={
                    "success": True,
                    "message": f"{'Subsection' if is_subsection_edit else 'Section'} content updated successfully",
                    "card_id": card_id,
                    "version": new_version,
                    "primary_card_id": new_card_id,
                },
            )

    except Exception as e:
        logger.error(f"Error in replace_card_content: {e}")
        return JSONResponse(
            status_code=500,
            content={
                "success": False,
                "error": "Something went wrong while editing your content. Please try again.",
            },
        )
