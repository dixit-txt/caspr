"""HTTP routes for the deliverables bounded context.

Handlers moved verbatim from ``src/resources/routers/api.py`` during the
R-STRUCT-1 migration, which split that 10,257-line module across seven
contexts. Handler bodies are unchanged; only the import block and the
``APIRouter`` they attach to are new.
"""

"""api.py: Authentication API for the Casper backend"""
import asyncio
import os
import re
from datetime import UTC, datetime

from fastapi import APIRouter, Depends
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from sqlalchemy import select

from app.adapters.s3 import (
    build_report_s3_prefix,
)
from app.auth.repository import (
    check_user_by_id,
    get_user_details,
)
from app.auth.schemas import ErrorResponse
from app.auth.token import get_current_active_user
from app.cards.repository import (
    get_cards_for_version,
    get_report_cards,
)
from app.chats.repository import get_user_chat
from app.core.constants import (
    MAX_REPORT_VERSIONS_FREE,
    MAX_REPORT_VERSIONS_PAID,
)
from app.core.db import async_session_scope
from app.core.enums import ReportStatus, SubscriptionTier
from app.core.logging import setup_logging
from app.core.utils import extract_content
from app.deliverables.schemas import (
    GenerateInfographicRequest,
    GenerateInfographicResponse,
    GeneratePresentationRequest,
    GeneratePresentationResponse,
)
from app.deliverables.service_pptx import (
    content_slides_for_report,
    generate_markdown_from_report,
    generate_pptx_and_upload_s3,
    generate_pptx_markdown_from_report,
)
from app.models import Report, ReportVersion
from app.observability.cloudwatch_utils import insert_cloudwatch_logs
from app.observability.functionality_context import Functionality, set_functionality
from app.reports.repository import (
    create_new_report_version,
    detect_modified_cards,
    finalize_report_version,
    update_report,
    update_report_status_by_chat_or_report_id,
    update_specific_version_s3_uri,
    verify_report_ownership,
)
from app.research.infographics.one_pager import generate_one_pager
from app.wallet.repository import get_active_subscription

router = APIRouter()
logger = setup_logging(__file__)


@router.post(
    "/generate-presentation",
    response_model=GeneratePresentationResponse,
    status_code=200,
    responses={
        404: {"model": ErrorResponse},
        401: {"model": ErrorResponse},
        422: {"model": ErrorResponse},
        500: {"model": ErrorResponse},
    },
)
async def generate_pptx(
    data: GeneratePresentationRequest, user_id: str = Depends(get_current_active_user)
):
    """Generate PowerPoint presentation from report. If report_version_id is provided, generates for that specific version."""
    set_functionality(Functionality.PPTX_GENERATION)
    report_id = data.report_id
    report_version_id = data.report_version_id
    generation_mode = (data.generation_mode or "template").strip().lower()
    if generation_mode not in ("template", "scratch"):
        generation_mode = "template"
    logger.info(
        f"Generate presentation request received for user_id: {user_id}, report_id: {report_id}, version_id: {report_version_id}, mode: {generation_mode}"
    )

    # Check if disclaimer template exists
    # from app.deliverables.service_pptx import TEMPLATE_PATH #TODO add the Template in a asset folder
    # if os.path.exists(TEMPLATE_PATH):
    #     logger.info(f"Disclaimer template found at {TEMPLATE_PATH}")
    # else:
    #     logger.warning(f"Disclaimer template not found at {TEMPLATE_PATH}")

    try:
        temp_files: list = []

        # 1. Verify user
        async with async_session_scope() as session:
            db_response = await check_user_by_id(user_id=user_id, session=session)
            if not db_response.get("success", False):
                logger.error(
                    f"Failed to check user by id: {db_response.get('error')} for user_id: {user_id}"
                )
                return JSONResponse(
                    status_code=500,
                    content={
                        "success": False,
                        "error": "I ran into a problem while creating your presentation. Please try generating it again.",
                    },
                )
            if not db_response.get("exists", False):
                logger.error(f"User with ID {user_id} not found or token is invalid")
                return JSONResponse(
                    status_code=401, content={"success": False, "error": "Unauthorized request"}
                )

        async with async_session_scope() as session:
            user_plan_result = await get_active_subscription(user_id=user_id, session=session)
            pptx_user_tier = (
                user_plan_result.get("subscription", {}).get(
                    "current_tier", SubscriptionTier.FREE.value
                )
                if user_plan_result.get("subscription")
                else SubscriptionTier.FREE.value
            )

        # 2. Get user details and report details
        async with async_session_scope() as session:
            # Get user details
            db_response = await get_user_details(user_id=user_id, session=session)
            if not db_response.get("success", False):
                logger.error(
                    f"Database error: Failed to get user details: {db_response.get('error')} for user_id: {user_id}"
                )
                return JSONResponse(
                    status_code=500,
                    content={
                        "success": False,
                        "error": "I ran into a problem while creating your presentation. Please try generating it again.",
                    },
                )
            user_name = db_response.get("user", {}).get("user_name")

            # Get report details and verify ownership
            ownership = await verify_report_ownership(
                report_id=report_id, user_id=user_id, session=session
            )
            if not ownership["authorized"]:
                return JSONResponse(
                    status_code=ownership["status_code"],
                    content={"success": False, "error": ownership["error"]},
                )
            report_data = ownership["report"]

            report_title = report_data.get("title", "")
            report_length = report_data.get("length")
            report_type_field = report_data.get("report_type")

            # ============================================================================
            # FLOW A: Specific report_version_id provided
            # ============================================================================
            target_version_for_generation = None
            use_specific_version = False
            new_version_created = (
                False  # Track if a new version was created (important for s3_uri handling)
            )

            if report_version_id:
                # Step 1: Validate report_version_id belongs to this report
                version_stmt = select(ReportVersion).where(
                    ReportVersion.id == report_version_id, ReportVersion.report_id == report_id
                )
                version_result = await session.execute(version_stmt)
                target_version = version_result.scalar_one_or_none()

                if not target_version:
                    logger.warning(
                        f"Version not found: {report_version_id} for report_id: {report_id}"
                    )
                    return JSONResponse(
                        status_code=404,
                        content={"success": False, "error": "The requested version was not found."},
                    )

                # Step 2: Check if PPTX already exists for this version
                s3_uri = target_version.s3_uri or {}
                if s3_uri.get("pptx"):
                    # Already exists → Return "up to date"
                    logger.info(
                        f"PPTX already exists for version {target_version.version}, returning existing"
                    )
                    return JSONResponse(
                        status_code=200,
                        content={
                            "success": True,
                            "message": "Your presentation is up to date. No new changes have been detected in your report since the last presentation was generated.",
                            "report_id": report_id,
                            "report_version_id": target_version.id,
                            "version": target_version.version,
                            "pptx_s3_uri": s3_uri.get("pptx"),
                            "pptx_generation_time": s3_uri.get("pptx_generation_time"),
                            "already_exists": True,
                        },
                    )

                # Step 3: PPTX doesn't exist → Will generate for this specific version
                target_version_for_generation = target_version
                use_specific_version = True
                # Set version info from the specific version (NOT from active version)
                current_version = target_version.version
                target_version_id = target_version.id
                logger.info(f"Will generate PPTX for specific version {target_version.version}")

            # ============================================================================
            # FLOW B: No report_version_id provided - Use Active Version
            # Only run this logic when NOT using a specific version
            # ============================================================================
            if not use_specific_version:
                # Step 1: Get active version (single DB fetch - reuse data throughout)
                version_stmt = select(ReportVersion).where(
                    ReportVersion.report_id == report_id, ReportVersion.is_active.is_(True)
                )
                version_result = await session.execute(version_stmt)
                active_version = version_result.scalar_one_or_none()

                if not active_version:
                    return JSONResponse(
                        status_code=404,
                        content={
                            "success": False,
                            "error": "No active version found for this report.",
                        },
                    )

                # Store version data for reuse (optimization: avoid extra DB calls)
                current_version = active_version.version
                current_s3_uri = active_version.s3_uri or {}
                target_version_id = active_version.id

                logger.info(
                    f"PPTX generation for report_id: {report_id}, active version: {current_version}"
                )

                # Step 2: Check if ANY output exists for this version
                # This determines if we need to check for card modifications
                has_any_output = (
                    active_version.generated_at  # PDF generated
                    or current_s3_uri.get("md_generation_time")  # MD generated
                    or current_s3_uri.get("html_generation_time")  # HTML generated
                    or current_s3_uri.get("info_pdf_generation_time")  # Infographic generated
                    or current_s3_uri.get("pptx_generation_time")  # PPTX generated
                )

                # Step 3: Apply versioning logic based on has_any_output
                if has_any_output:
                    # ----------------------------------------------------------------
                    # Version has at least one output → Check for card modifications
                    # ----------------------------------------------------------------
                    logger.info(
                        f"Version {current_version} has existing output(s). Checking for card modifications..."
                    )

                    detection_result = await detect_modified_cards(
                        report_id=report_id, session=session, generation_type="presentation"
                    )

                    if not detection_result.get("success"):
                        logger.error(
                            f"Failed to detect card modifications: {detection_result.get('error')}"
                        )
                        return JSONResponse(
                            status_code=500,
                            content={
                                "success": False,
                                "error": "I ran into a problem while creating your presentation. Please try generating it again.",
                            },
                        )

                    modified_count = detection_result.get("modified_count", 0)
                    logger.info(
                        f"Card modification check: {modified_count} modified, {detection_result.get('unchanged_count', 0)} unchanged"
                    )

                    if modified_count > 0:
                        # ----------------------------------------------------------------
                        # Cards MODIFIED → Create new version, then generate
                        # ----------------------------------------------------------------
                        logger.info("Cards modified since last output. Creating new version...")

                        # Enforce per-tier version limit before creating a new version
                        _pptx_is_paid = pptx_user_tier != SubscriptionTier.FREE.value
                        _pptx_max_versions = (
                            MAX_REPORT_VERSIONS_PAID if _pptx_is_paid else MAX_REPORT_VERSIONS_FREE
                        )
                        if active_version.version >= _pptx_max_versions:
                            _upgrade_hint = (
                                ""
                                if _pptx_is_paid
                                else " Upgrade to a paid plan to unlock up to 5 versions."
                            )
                            logger.warning(
                                f"User {user_id} hit version limit ({_pptx_max_versions}) for report_id: {report_id}"
                            )
                            return JSONResponse(
                                status_code=403,
                                content={
                                    "success": False,
                                    "error": f"This report has reached the maximum of {_pptx_max_versions} versions.{_upgrade_hint}",
                                },
                            )

                        # Prepare cards data for new version
                        modified_cards_data = detection_result.get("modified_cards", [])
                        unchanged_cards_data = detection_result.get("unchanged_cards", [])
                        cards_data = modified_cards_data + unchanged_cards_data
                        cards_data.sort(key=lambda x: x.get("sequence", 0))

                        # Create new version
                        version_response = await create_new_report_version(
                            session=session, report_id=report_id, cards_data=cards_data
                        )

                        if not version_response.get("success"):
                            return JSONResponse(
                                status_code=500,
                                content={
                                    "success": False,
                                    "error": "I ran into a problem while creating your presentation. Please try generating it again.",
                                },
                            )

                        # Update to new version
                        current_version = version_response.get("version")
                        target_version_id = version_response.get("version_id")
                        new_version_created = True  # Flag that new version was created
                        logger.info(f"Created new version {current_version} for PPTX generation")

                        # Reset Report table fields for new version
                        await update_report(
                            report_id=report_id,
                            update_data={
                                "s3_uri": {
                                    "md": None,
                                    "html": None,
                                    "pdf": None,
                                    "pptx": None,
                                    "info_pdf": None,
                                },
                                "generated_at": None,
                                "status": ReportStatus.REDO_ANALYSIS.value,
                                "current_version": current_version,
                            },
                            session=session,
                        )
                    else:
                        # ----------------------------------------------------------------
                        # Cards NOT modified → Check if PPTX already exists
                        # ----------------------------------------------------------------
                        if current_s3_uri.get("pptx"):
                            # PPTX already exists for this version → Return "up to date"
                            logger.info(
                                f"PPTX already exists for version {current_version} and no changes detected"
                            )
                            return JSONResponse(
                                status_code=200,
                                content={
                                    "success": True,
                                    "message": "Your presentation is up to date. No new changes have been detected in your report since the last presentation was generated.",
                                    "report_id": report_id,
                                    "report_version_id": active_version.id,
                                    "version": current_version,
                                    "pptx_s3_uri": current_s3_uri.get("pptx"),
                                    "pptx_generation_time": current_s3_uri.get(
                                        "pptx_generation_time"
                                    ),
                                    "already_exists": True,
                                },
                            )
                        else:
                            # PPTX doesn't exist → Generate for current version (no new version needed)
                            logger.info(
                                f"No modifications, but PPTX not yet generated. Generating for version {current_version}..."
                            )
                else:
                    # ----------------------------------------------------------------
                    # No outputs exist yet → Generate for current version (first output)
                    # This will "lock" the cards to this version
                    # ----------------------------------------------------------------
                    logger.info(
                        f"No outputs exist for version {current_version}. Generating PPTX (first output)..."
                    )

        # Log "Presentation generation started" in background
        async def log_presentation_start():
            try:
                # Get chat_id and chat_title
                chat_id = report_data.get("chat_id")
                chat_title = None
                if chat_id:
                    async with async_session_scope() as session:
                        chat_response = await get_user_chat(
                            user_id=user_id, chat_id=chat_id, session=session
                        )
                        if chat_response.get("success", False) and chat_response.get("message"):
                            chat_title = chat_response.get("message", {}).get("chat_title")

                start_data = {
                    "event": "Presentation generation started",
                    "event_success": True,
                    "timestamp": datetime.now(UTC).isoformat(),
                    "chat_id": chat_id or "N/A",
                    "chat_title": chat_title or "N/A",
                }
                await insert_cloudwatch_logs(data=start_data, user_id=user_id)
                logger.info(f"Logged 'Presentation generation started' for report_id: {report_id}")
            except Exception as e:
                logger.error(f"Error logging presentation start: {e} for report_id: {report_id}")

        asyncio.create_task(log_presentation_start())

        # Set status to GENERATING_OUTPUT when PPTX generation starts
        async with async_session_scope() as session:
            await update_report_status_by_chat_or_report_id(
                report_id=report_id, status=ReportStatus.GENERATING_OUTPUT.value, session=session
            )
            logger.info(
                f"Set report status to GENERATING_OUTPUT for PPTX generation, report_id: {report_id}"
            )

        async with async_session_scope() as session:
            total_slides = content_slides_for_report(
                report_type=report_type_field,
                length=report_length,
            )
            logger.info(
                "report_type=%s length=%s → content slides: %d (plus 3 structural slides)",
                report_type_field,
                report_length,
                total_slides,
            )

            # Get report cards
            # If specific version requested (Flow A), use version's locked snapshot
            # Otherwise use current active cards
            if use_specific_version and target_version_for_generation:
                db_response = await get_cards_for_version(
                    report_id=report_id,
                    version_id=target_version_for_generation.id,
                    session=session,
                )
                logger.info(
                    f"Using cards from version snapshot for version {target_version_for_generation.version}"
                )
            else:
                db_response = await get_report_cards(report_id=report_id, session=session)

            if not db_response.get("success", False):
                logger.error(
                    f"Failed to get report cards: {db_response.get('error')} for report_id: {report_id}"
                )
                return JSONResponse(
                    status_code=500,
                    content={
                        "success": False,
                        "error": "I ran into a problem while creating your presentation. Please try generating it again.",
                    },
                )
            cards = db_response.get("cards", [])

        # 4. Generate markdown from cards
        markdown_content = await generate_pptx_markdown_from_report(report_id, cards)
        if not markdown_content:
            logger.error(f"Failed to generate markdown content for report_id: {report_id}")
            return JSONResponse(
                status_code=500,
                content={
                    "success": False,
                    "error": "I ran into a problem while creating your presentation. Please try generating it again.",
                },
            )

        # 5–7. Generate PPTX, append disclaimer slide, upload to S3 in one call
        base_filename = (
            (report_title or "report")
            .strip(".")
            .strip()
            .replace(":", " ")
            .replace("-", " ")
            .replace(" ", "_")
        )
        base_filename = re.sub(r"_+", "_", base_filename)
        pptx_filename = f"{base_filename}.pptx"

        prefix = build_report_s3_prefix(
            user_id=user_id,
            user_name=user_name,
            chat_id=report_data.get("chat_id") or report_id,
            chat_title=report_title,
            report_id=report_id,
            version=current_version,
        )
        pptx_key = f"{prefix}/report/{pptx_filename}"
        logger.info(f"Generating and uploading PPTX to {pptx_key} for report_id={report_id}")

        try:
            pptx_s3_uri = await generate_pptx_and_upload_s3(
                md_content=markdown_content,
                total_slides=total_slides,
                s3_key=pptx_key,
                report_id=report_id,
                generation_mode=generation_mode,
                user_id=user_id,
                chat_id=report_data.get("chat_id"),
                report_version_id=(
                    target_version_for_generation.id
                    if use_specific_version and target_version_for_generation
                    else None
                ),
            )
        except Exception as e:
            logger.error(
                f"Failed to generate/upload PPTX for report_id={report_id}: {e}",
                exc_info=True,
            )
            return JSONResponse(
                status_code=500,
                content={
                    "success": False,
                    "error": "I ran into a problem while creating your presentation. Please try generating it again.",
                },
            )

        # 8. Update s3_uri JSON field and status
        # IMPORTANT: When generating for a specific OLD (non-active) version, only update that version's s3_uri
        # When generating for the ACTIVE version (whether report_version_id provided or not), update BOTH tables
        #
        # CRITICAL: When a NEW version was created, start with fresh s3_uri (don't carry over old version's outputs)
        # Otherwise, get the current s3_uri from the active version (NOT from stale report_data)
        if new_version_created:
            # New version created - start with fresh s3_uri
            updated_uris = {}
            logger.info(
                f"New version {current_version} created - starting PPTX update with fresh s3_uri"
            )
        else:
            # Existing version - get current s3_uri from active version (fresh from DB)
            async with async_session_scope() as session:
                version_stmt = select(ReportVersion).where(
                    ReportVersion.report_id == report_id, ReportVersion.is_active.is_(True)
                )
                version_result = await session.execute(version_stmt)
                active_ver = version_result.scalar_one_or_none()
                updated_uris = dict(active_ver.s3_uri or {}) if active_ver else {}
            logger.info(
                f"Existing version {current_version} - PPTX update will merge with current s3_uri"
            )

        updated_uris["pptx"] = pptx_s3_uri
        updated_uris["pptx_generation_time"] = datetime.now(UTC).isoformat()

        async with async_session_scope() as session:
            # Check if the provided version is the active version
            is_generating_for_old_version = (
                use_specific_version
                and target_version_for_generation
                and not target_version_for_generation.is_active
            )

            if is_generating_for_old_version:
                # ========================================
                # FLOW A: Generating for a SPECIFIC OLD (non-active) version
                # Update only the specific version's s3_uri, NOT the Report table
                # Report table should continue reflecting the current active version's data
                # ========================================
                logger.info(
                    f"Updating PPTX s3_uri for specific OLD version {current_version} (NOT updating Report table)"
                )
                db_response = await update_specific_version_s3_uri(
                    report_id=report_id,
                    version_id=target_version_for_generation.id,
                    s3_uri_updates={
                        "pptx": pptx_s3_uri,
                        "pptx_generation_time": updated_uris["pptx_generation_time"],
                    },
                    session=session,
                )
                if not db_response.get("success", False):
                    logger.error(
                        f"Failed to update specific version PPTX s3_uri: {db_response.get('error')} for report_id: {report_id}"
                    )
                    return JSONResponse(
                        status_code=500,
                        content={
                            "success": False,
                            "error": "I ran into a problem while creating your presentation. Please try generating it again.",
                        },
                    )
                logger.info(
                    f"Generated PPTX for specific old version {current_version} - cards already linked from version snapshot"
                )
            else:
                # ========================================
                # FLOW B: Generating for CURRENT/ACTIVE version
                # Update both Report table and active ReportVersion
                # This covers:
                #   - No report_version_id provided (use_specific_version=False)
                #   - report_version_id provided but it's the active version
                # ========================================
                logger.info(
                    f"Updating PPTX for active version {current_version}, preserving other files"
                )
                db_response = await update_report(
                    report_id=report_id,
                    update_data={
                        "s3_uri": updated_uris
                        # Status will be updated to OUTPUT_GENERATED after successful completion
                    },
                    session=session,
                )
                if not db_response.get("success", False):
                    logger.error(
                        f"Failed to update report: {db_response.get('error')} for report_id: {report_id}"
                    )
                    return JSONResponse(
                        status_code=500,
                        content={
                            "success": False,
                            "error": "I ran into a problem while creating your presentation. Please try generating it again.",
                        },
                    )

                # Finalize version by linking current cards (first output locks the cards)
                # For version 1 (first time generation): Call finalize_report_version to populate report_version_cards
                # For version 2+: Already populated by create_new_report_version, skip finalization
                if current_version == 1:
                    logger.info(f"Finalizing version 1 for PPTX generation, report_id: {report_id}")
                    finalize_result = await finalize_report_version(
                        session=session, report_id=report_id
                    )
                    if finalize_result.get("success"):
                        cards_linked = finalize_result.get("cards_linked", 0)
                        already_finalized = finalize_result.get("already_finalized", False)
                        if already_finalized:
                            logger.info(
                                f"Version 1 already finalized for PPTX, report_id: {report_id}"
                            )
                        else:
                            logger.info(
                                f"Finalized version 1 with {cards_linked} cards for PPTX, report_id: {report_id}"
                            )
                    else:
                        logger.warning(
                            f"Failed to finalize version 1 for PPTX: {finalize_result.get('error')}"
                        )

        logger.info(f"Presentation generated successfully for report_id: {report_id}")

        # Log "Presentation generation completed" in background
        async def log_presentation_completion():
            try:
                # Get chat_id and chat_title
                chat_id = report_data.get("chat_id")
                chat_title = None
                if chat_id:
                    async with async_session_scope() as session:
                        chat_response = await get_user_chat(
                            user_id=user_id, chat_id=chat_id, session=session
                        )
                        if chat_response.get("success", False) and chat_response.get("message"):
                            chat_title = chat_response.get("message", {}).get("chat_title")

                completion_data = {
                    "event": "Presentation generation completed",
                    "event_success": True,
                    "timestamp": datetime.now(UTC).isoformat(),
                    "chat_id": chat_id or "N/A",
                    "chat_title": chat_title or "N/A",
                }
                await insert_cloudwatch_logs(data=completion_data, user_id=user_id)
                logger.info(
                    f"Logged 'Presentation generation completed' for report_id: {report_id}"
                )
            except Exception as e:
                logger.error(
                    f"Error logging presentation completion: {e} for report_id: {report_id}"
                )

        asyncio.create_task(log_presentation_completion())

        # Set status to OUTPUT_GENERATED when PPTX generation completes successfully
        async with async_session_scope() as session:
            await update_report_status_by_chat_or_report_id(
                report_id=report_id, status=ReportStatus.OUTPUT_GENERATED.value, session=session
            )
            logger.info(
                f"Set report status to OUTPUT_GENERATED for PPTX generation, report_id: {report_id}"
            )

        # Get version ID for response
        final_version_id = None
        if use_specific_version and target_version_for_generation:
            final_version_id = target_version_for_generation.id
        else:
            async with async_session_scope() as session:
                version_stmt = select(ReportVersion).where(
                    ReportVersion.report_id == report_id, ReportVersion.version == current_version
                )
                version_result = await session.execute(version_stmt)
                version_obj = version_result.scalar_one_or_none()
                if version_obj:
                    final_version_id = version_obj.id

        return GeneratePresentationResponse(
            success=True,
            message="Presentation has been generated successfully.",
            report_version_id=final_version_id,
            version=current_version,
            pptx_s3_uri=pptx_s3_uri,
            pptx_generation_time=updated_uris.get("pptx_generation_time"),
            report_id=report_id,
            modified=True,  # New PPTX was generated
            already_exists=False,  # Newly generated, not pre-existing
        )

    except Exception as e:
        logger.error(f"Error generating presentation: {e}")
        # Update report status to ERROR_GENERATION_REPORT on error
        try:
            async with async_session_scope() as session:
                await update_report_status_by_chat_or_report_id(
                    report_id=report_id,
                    status=ReportStatus.ERROR_GENERATION_REPORT.value,
                    session=session,
                )
                logger.info(
                    f"Set report status to ERROR_GENERATION_REPORT for PPTX generation, report_id: {report_id}"
                )
        except Exception as update_error:
            logger.error(
                f"Failed to update report status to ERROR_GENERATION_REPORT: {update_error}"
            )

        # Clean up any temporary files
        for file_path in temp_files:
            try:
                if os.path.exists(file_path):
                    os.unlink(file_path)
            except:
                pass
        return JSONResponse(
            status_code=500,
            content={
                "success": False,
                "error": "I ran into a problem while creating your presentation. Please try generating it again.",
            },
        )


@router.post(
    "/generate-infographic",
    response_model=GenerateInfographicResponse,
    status_code=200,
    responses={
        404: {"model": ErrorResponse},
        401: {"model": ErrorResponse},
        422: {"model": ErrorResponse},
        500: {"model": ErrorResponse},
    },
)
async def generate_infographic(
    data: GenerateInfographicRequest, user_id: str = Depends(get_current_active_user)
):
    """
    Generate infographic PDF for a report.

    Logic:
    - Uses current timestamp for infographic generation (no correlation with Report.generated_at)
    - Stores info_pdf_generation_time in s3_uri JSONB only (like PPTX does with pptx_generation_time)
    - Follows same versioning logic as PPTX:
      - If infographic exists and no changes → return existing
      - If infographic exists and changes detected → create new version
      - If no infographic exists → generate for current version
    - Poster image: First generation (report OR infographic) creates it, subsequent reuse it
    - If report_version_id is provided, generates for that specific version
    """
    # Attribute all LLM spend in this request to the "infographic" functionality.
    set_functionality(Functionality.INFOGRAPHIC)
    report_id = data.report_id
    report_version_id = data.report_version_id
    logger.info(
        f"Generate infographic request received for user_id: {user_id}, report_id: {report_id}, version_id: {report_version_id}"
    )

    try:
        # 1. Validate user
        async with async_session_scope() as session:
            db_response = await check_user_by_id(user_id=user_id, session=session)
            if not db_response.get("success", False):
                logger.error(
                    f"Failed to check user by id: {db_response.get('error')} for user_id: {user_id} and report_id: {report_id}"
                )
                return JSONResponse(
                    status_code=500,
                    content={
                        "success": False,
                        "error": "Something unexpected happened. Please try again later.",
                    },
                )
            if not db_response.get("exists", False):
                logger.error(
                    f"User with ID {user_id} not found or token is invalid for report_id: {report_id}"
                )
                return JSONResponse(
                    status_code=401, content={"success": False, "error": "Unauthorized request"}
                )

        async with async_session_scope() as session:
            user_plan_result = await get_active_subscription(user_id=user_id, session=session)
            info_user_tier = (
                user_plan_result.get("subscription", {}).get(
                    "current_tier", SubscriptionTier.FREE.value
                )
                if user_plan_result.get("subscription")
                else SubscriptionTier.FREE.value
            )

        # 2. Get user details
        async with async_session_scope() as session:
            db_response = await get_user_details(user_id=user_id, session=session)
            if not db_response.get("success", False):
                logger.error(
                    f"Database error: Failed to get user details: {db_response.get('error')} for user_id: {user_id} and report_id: {report_id}"
                )
                return JSONResponse(
                    status_code=500,
                    content={
                        "success": False,
                        "error": "Something unexpected happened. Please try again later.",
                    },
                )

        user_name = db_response.get("user", {}).get("user_name", None)

        # 3. Get report details and verify ownership
        async with async_session_scope() as session:
            ownership = await verify_report_ownership(
                report_id=report_id, user_id=user_id, session=session
            )
            if not ownership["authorized"]:
                return JSONResponse(
                    status_code=ownership["status_code"],
                    content={"success": False, "error": ownership["error"]},
                )

        report_data = ownership["report"]
        report_title = report_data.get("title", "")

        # ============================================================================
        # FLOW A: Specific report_version_id provided
        # ============================================================================
        target_version_for_generation = None
        use_specific_version = False
        new_version_created = (
            False  # Track if a new version was created (important for s3_uri handling)
        )

        if report_version_id:
            async with async_session_scope() as session:
                # Step 1: Validate report_version_id belongs to this report
                version_stmt = select(ReportVersion).where(
                    ReportVersion.id == report_version_id, ReportVersion.report_id == report_id
                )
                version_result = await session.execute(version_stmt)
                target_version = version_result.scalar_one_or_none()

                if not target_version:
                    logger.warning(
                        f"Version not found: {report_version_id} for report_id: {report_id}"
                    )
                    return JSONResponse(
                        status_code=404,
                        content={"success": False, "error": "The requested version was not found."},
                    )

                # Step 2: Check if infographic already exists for this version
                s3_uri = target_version.s3_uri or {}
                if s3_uri.get("info_pdf"):
                    # Already exists → Return "up to date"
                    logger.info(
                        f"Infographic already exists for version {target_version.version}, returning existing"
                    )
                    return JSONResponse(
                        status_code=200,
                        content={
                            "success": True,
                            "message": "Your infographic is up to date. No new changes have been detected in your report since the last infographic was generated.",
                            "report_id": report_id,
                            "report_version_id": target_version.id,
                            "version": target_version.version,
                            "info_pdf_s3_uri": s3_uri.get("info_pdf"),
                            "info_pdf_generation_time": s3_uri.get("info_pdf_generation_time"),
                            "already_exists": True,
                        },
                    )

                # Step 3: Infographic doesn't exist → Will generate for this specific version
                target_version_for_generation = target_version
                use_specific_version = True
                # Set version info from the specific version (NOT from active version)
                current_version = target_version.version
                target_version_id = target_version.id
                logger.info(
                    f"Will generate infographic for specific version {target_version.version}"
                )

        # ============================================================================
        # FLOW B: No report_version_id provided - Use Active Version
        # Only run this logic when NOT using a specific version
        # ============================================================================
        if not use_specific_version:
            async with async_session_scope() as session:
                # Step 1: Get active version (single DB fetch - reuse data throughout)
                version_stmt = select(ReportVersion).where(
                    ReportVersion.report_id == report_id, ReportVersion.is_active.is_(True)
                )
                version_result = await session.execute(version_stmt)
                active_version = version_result.scalar_one_or_none()

                if not active_version:
                    return JSONResponse(
                        status_code=404,
                        content={
                            "success": False,
                            "error": "No active version found for this report.",
                        },
                    )

                # Store version data for reuse (optimization: avoid extra DB calls)
                current_version = active_version.version
                current_s3_uri = active_version.s3_uri or {}
                target_version_id = active_version.id

                logger.info(
                    f"Infographic generation for report_id: {report_id}, active version: {current_version}"
                )

                # Step 2: Check if ANY output exists for this version
                # This determines if we need to check for card modifications
                has_any_output = (
                    active_version.generated_at  # PDF generated
                    or current_s3_uri.get("md_generation_time")  # MD generated
                    or current_s3_uri.get("html_generation_time")  # HTML generated
                    or current_s3_uri.get("info_pdf_generation_time")  # Infographic generated
                    or current_s3_uri.get("pptx_generation_time")  # PPTX generated
                )

                # Step 3: Apply versioning logic based on has_any_output
                if has_any_output:
                    # ----------------------------------------------------------------
                    # Version has at least one output → Check for card modifications
                    # ----------------------------------------------------------------
                    logger.info(
                        f"Version {current_version} has existing output(s). Checking for card modifications..."
                    )

                    detection_result = await detect_modified_cards(
                        report_id=report_id, session=session, generation_type="infographic"
                    )

                    if not detection_result.get("success"):
                        logger.error(
                            f"Failed to detect card modifications: {detection_result.get('error')}"
                        )
                        return JSONResponse(
                            status_code=500,
                            content={
                                "success": False,
                                "error": "I ran into a problem while creating your infographic. Please try generating it again.",
                            },
                        )

                    modified_count = detection_result.get("modified_count", 0)
                    logger.info(
                        f"Card modification check: {modified_count} modified, {detection_result.get('unchanged_count', 0)} unchanged"
                    )

                    if modified_count > 0:
                        # ----------------------------------------------------------------
                        # Cards MODIFIED → Create new version, then generate
                        # ----------------------------------------------------------------
                        logger.info("Cards modified since last output. Creating new version...")

                        # Enforce per-tier version limit before creating a new version
                        _info_is_paid = info_user_tier != SubscriptionTier.FREE.value
                        _info_max_versions = (
                            MAX_REPORT_VERSIONS_PAID if _info_is_paid else MAX_REPORT_VERSIONS_FREE
                        )
                        if active_version.version >= _info_max_versions:
                            _upgrade_hint = (
                                ""
                                if _info_is_paid
                                else " Upgrade to a paid plan to unlock up to 5 versions."
                            )
                            logger.warning(
                                f"User {user_id} hit version limit ({_info_max_versions}) for report_id: {report_id}"
                            )
                            return JSONResponse(
                                status_code=403,
                                content={
                                    "success": False,
                                    "error": f"This report has reached the maximum of {_info_max_versions} versions.{_upgrade_hint}",
                                },
                            )

                        # Prepare cards data for new version
                        modified_cards_data = detection_result.get("modified_cards", [])
                        unchanged_cards_data = detection_result.get("unchanged_cards", [])
                        cards_data = modified_cards_data + unchanged_cards_data
                        cards_data.sort(key=lambda x: x.get("sequence", 0))

                        # Create new version
                        version_response = await create_new_report_version(
                            session=session, report_id=report_id, cards_data=cards_data
                        )

                        if not version_response.get("success"):
                            return JSONResponse(
                                status_code=500,
                                content={
                                    "success": False,
                                    "error": "I ran into a problem while creating your infographic. Please try generating it again.",
                                },
                            )

                        # Update to new version
                        current_version = version_response.get("version")
                        target_version_id = version_response.get("version_id")
                        new_version_created = True  # Flag that new version was created
                        logger.info(
                            f"Created new version {current_version} for infographic generation"
                        )

                        # Reset Report table fields for new version
                        await update_report(
                            report_id=report_id,
                            update_data={
                                "s3_uri": {
                                    "md": None,
                                    "html": None,
                                    "pdf": None,
                                    "pptx": None,
                                    "info_pdf": None,
                                },
                                "generated_at": None,
                                "status": ReportStatus.REDO_ANALYSIS.value,
                                "current_version": current_version,
                            },
                            session=session,
                        )
                    else:
                        # ----------------------------------------------------------------
                        # Cards NOT modified → Check if Infographic already exists
                        # ----------------------------------------------------------------
                        if current_s3_uri.get("info_pdf"):
                            # Infographic already exists for this version → Return "up to date"
                            logger.info(
                                f"Infographic already exists for version {current_version} and no changes detected"
                            )
                            return JSONResponse(
                                status_code=200,
                                content={
                                    "success": True,
                                    "message": "Your infographic is up to date. No new changes have been detected in your report since the last infographic was generated.",
                                    "report_id": report_id,
                                    "report_version_id": active_version.id,
                                    "version": current_version,
                                    "info_pdf_s3_uri": current_s3_uri.get("info_pdf"),
                                    "info_pdf_generation_time": current_s3_uri.get(
                                        "info_pdf_generation_time"
                                    ),
                                    "already_exists": True,
                                },
                            )
                        else:
                            # Infographic doesn't exist → Generate for current version (no new version needed)
                            logger.info(
                                f"No modifications, but infographic not yet generated. Generating for version {current_version}..."
                            )
                else:
                    # ----------------------------------------------------------------
                    # No outputs exist yet → Generate for current version (first output)
                    # This will "lock" the cards to this version
                    # ----------------------------------------------------------------
                    logger.info(
                        f"No outputs exist for version {current_version}. Generating infographic (first output)..."
                    )

        # Set status to GENERATING_OUTPUT when infographic generation starts
        async with async_session_scope() as session:
            await update_report_status_by_chat_or_report_id(
                report_id=report_id, status=ReportStatus.GENERATING_OUTPUT.value, session=session
            )
            logger.info(
                f"Set report status to GENERATING_OUTPUT for infographic generation, report_id: {report_id}"
            )

        # 5. Get report cards
        # If specific version requested (Flow A), use version's locked snapshot
        # Otherwise use current active cards
        async with async_session_scope() as session:
            if use_specific_version and target_version_for_generation:
                db_response = await get_cards_for_version(
                    report_id=report_id,
                    version_id=target_version_for_generation.id,
                    session=session,
                )
                logger.info(
                    f"Using cards from version snapshot for version {target_version_for_generation.version}"
                )
            else:
                db_response = await get_report_cards(report_id=report_id, session=session)

            if not db_response.get("success", False):
                return JSONResponse(
                    status_code=500,
                    content={"success": False, "error": "Failed to get report cards."},
                )

        cards = db_response.get("cards", [])
        if not cards:
            return JSONResponse(
                status_code=404, content={"success": False, "error": "No report cards found."}
            )

        # 6. Generate markdown from cards (using same function as PPTX)

        raw_md_content = await generate_markdown_from_report(report_id, cards)

        if not raw_md_content:
            logger.error(f"Failed to generate markdown for report_id: {report_id}")
            return JSONResponse(
                status_code=500,
                content={"success": False, "error": "Failed to build report content."},
            )

        # 7. Use current timestamp for infographic generation (no correlation with Report.generated_at)
        infographic_generation_time = datetime.now(UTC)
        logger.info(f"Infographic generation time: {infographic_generation_time}")

        # 8. Handle poster image (first generation creates it, subsequent reuse it)
        async with async_session_scope() as session:
            report_stmt = select(Report).where(Report.id == report_id)
            report_result = await session.execute(report_stmt)
            report = report_result.scalar_one_or_none()

            existing_poster_url = report.poster_image_url if report else None

        logger.info(f"Existing poster URL for infographic generation: {existing_poster_url}")

        # 9. Get chat info for S3 path
        chat_id = report_data.get("chat_id")
        chat_title = report_title  # Fallback
        if chat_id:
            async with async_session_scope() as session:
                chat_response = await get_user_chat(
                    user_id=user_id, chat_id=chat_id, session=session
                )
                if chat_response.get("success", False) and chat_response.get("message"):
                    chat_title = chat_response.get("message", {}).get("chat_title", chat_title)

        # Get report subtitle from cards
        report_subtitle = ""
        for card in cards:
            if card.get("type") == "subtitle":
                report_subtitle = extract_content(card, "content")
                break

        # 10. Generate infographic PDF
        infographic_s3_uri, newly_generated_poster_url = await run_in_threadpool(
            lambda: generate_one_pager(
                user_id=user_id,
                user_name=user_name,
                report_id=report_id,
                report_markdown=raw_md_content,
                report_title=report_title,
                report_subtitle=report_subtitle,
                poster_image_url=existing_poster_url,
                report_date=infographic_generation_time.strftime("%d %B %Y"),
                chat_id=chat_id,
                chat_title=chat_title,
                version=current_version,
                report_generation_time=infographic_generation_time,
            )
        )

        if not infographic_s3_uri:
            # Update status to error
            async with async_session_scope() as session:
                await update_report_status_by_chat_or_report_id(
                    report_id=report_id,
                    status=ReportStatus.ERROR_GENERATION_REPORT.value,
                    session=session,
                )
            return JSONResponse(
                status_code=500,
                content={
                    "success": False,
                    "error": "I ran into a problem while creating your infographic. Please try generating it again.",
                },
            )

        logger.info(f"Infographic PDF generated successfully: {infographic_s3_uri}")

        # 11. Save newly generated poster URL
        # IMPORTANT: When generating for a specific OLD (non-active) version, only update that version's poster
        # When generating for the ACTIVE version (whether report_version_id provided or not), update BOTH tables

        # Check if the provided version is the active version (for poster update)
        is_generating_for_old_version_poster = (
            use_specific_version
            and target_version_for_generation
            and not target_version_for_generation.is_active
        )

        if newly_generated_poster_url:
            logger.info(
                f"New poster image was generated during infographic creation: {newly_generated_poster_url}"
            )
            async with async_session_scope() as session:
                if is_generating_for_old_version_poster:
                    # Only update the specific old version's poster_image_url
                    version_stmt = select(ReportVersion).where(
                        ReportVersion.id == target_version_for_generation.id
                    )
                    version_result = await session.execute(version_stmt)
                    version = version_result.scalar_one_or_none()

                    if version:
                        version.poster_image_url = newly_generated_poster_url
                        await session.commit()
                        logger.info(
                            f"Saved new poster image URL to specific OLD ReportVersion (v{current_version}), NOT Report table: {newly_generated_poster_url}"
                        )
                else:
                    # Update both Report table and active ReportVersion
                    await update_report(
                        report_id=report_id,
                        update_data={"poster_image_url": newly_generated_poster_url},
                        session=session,
                    )
                    logger.info(
                        f"Saved new poster image URL to Report table: {newly_generated_poster_url}"
                    )

                    # Also update ReportVersion table with the poster URL
                    version_stmt = select(ReportVersion).where(
                        ReportVersion.report_id == report_id,
                        ReportVersion.version == current_version,
                    )
                    version_result = await session.execute(version_stmt)
                    version = version_result.scalar_one_or_none()

                    if version:
                        version.poster_image_url = newly_generated_poster_url
                        await session.commit()
                        logger.info(
                            f"Saved new poster image URL to ReportVersion table (version {current_version}): {newly_generated_poster_url}"
                        )

        # 12. Update s3_uri with infographic path (info_pdf_generation_time in s3_uri only - no Report.generated_at correlation)
        # IMPORTANT: When generating for a specific OLD (non-active) version, only update that version's s3_uri
        # When generating for the ACTIVE version (whether report_version_id provided or not), update BOTH tables

        # Check if the provided version is the active version
        is_generating_for_old_version = (
            use_specific_version
            and target_version_for_generation
            and not target_version_for_generation.is_active
        )

        # Get current s3_uri from the appropriate version
        # CRITICAL: When a NEW version was created, start with fresh s3_uri (don't carry over old version's outputs)
        if new_version_created:
            # New version created - start with fresh s3_uri
            current_s3_uri = {}
            logger.info(
                f"New version {current_version} created - starting infographic update with fresh s3_uri"
            )
        elif is_generating_for_old_version:
            # Get s3_uri from the specific old version
            current_s3_uri = target_version_for_generation.s3_uri or {}
        else:
            # Get s3_uri from active version (fresh from DB)
            async with async_session_scope() as session:
                version_stmt = select(ReportVersion).where(
                    ReportVersion.report_id == report_id, ReportVersion.is_active.is_(True)
                )
                version_result = await session.execute(version_stmt)
                active_version = version_result.scalar_one_or_none()
                current_s3_uri = active_version.s3_uri if active_version else {}
            logger.info(
                f"Existing version {current_version} - infographic update will merge with current s3_uri"
            )

        updated_uris = dict(current_s3_uri)
        updated_uris["info_pdf"] = infographic_s3_uri
        updated_uris["info_pdf_generation_time"] = datetime.now(UTC).isoformat()

        async with async_session_scope() as session:
            if is_generating_for_old_version:
                # ========================================
                # FLOW A: Generating for a SPECIFIC OLD (non-active) version
                # Update only the specific version's s3_uri, NOT the Report table
                # Report table should continue reflecting the current active version's data
                # ========================================
                logger.info(
                    f"Updating infographic s3_uri for specific OLD version {current_version} (NOT updating Report table)"
                )
                update_result = await update_specific_version_s3_uri(
                    report_id=report_id,
                    version_id=target_version_for_generation.id,
                    s3_uri_updates={
                        "info_pdf": infographic_s3_uri,
                        "info_pdf_generation_time": updated_uris["info_pdf_generation_time"],
                    },
                    session=session,
                )
                if not update_result.get("success"):
                    logger.error(
                        f"Failed to update specific version infographic s3_uri: {update_result.get('error')} for report_id: {report_id}"
                    )
                logger.info(
                    f"Generated infographic for specific old version {current_version} - cards already linked from version snapshot"
                )
            else:
                # ========================================
                # FLOW B: Generating for CURRENT/ACTIVE version
                # Update both Report table and active ReportVersion
                # This covers:
                #   - No report_version_id provided (use_specific_version=False)
                #   - report_version_id provided but it's the active version
                # ========================================
                logger.info(
                    f"Updating infographic for active version {current_version}, preserving other files"
                )
                update_result = await update_report(
                    report_id=report_id, update_data={"s3_uri": updated_uris}, session=session
                )

                if not update_result.get("success"):
                    logger.error(f"Failed to update s3_uri: {update_result.get('error')}")

                # Finalize version by linking current cards (first output locks the cards)
                # For version 1 (first time generation): Call finalize_report_version to populate report_version_cards
                # For version 2+: Already populated by create_new_report_version, skip finalization
                if current_version == 1:
                    logger.info(
                        f"Finalizing version 1 for infographic generation, report_id: {report_id}"
                    )
                    finalize_result = await finalize_report_version(
                        session=session, report_id=report_id
                    )
                    if finalize_result.get("success"):
                        cards_linked = finalize_result.get("cards_linked", 0)
                        already_finalized = finalize_result.get("already_finalized", False)
                        if already_finalized:
                            logger.info(
                                f"Version 1 already finalized for infographic, report_id: {report_id}"
                            )
                        else:
                            logger.info(
                                f"Finalized version 1 with {cards_linked} cards for infographic, report_id: {report_id}"
                            )
                    else:
                        logger.warning(
                            f"Failed to finalize version 1 for infographic: {finalize_result.get('error')}"
                        )

                # Update status to OUTPUT_GENERATED for active version generation
                await update_report_status_by_chat_or_report_id(
                    report_id=report_id, status=ReportStatus.OUTPUT_GENERATED.value, session=session
                )
                logger.info(
                    f"Set report status to OUTPUT_GENERATED for infographic generation, report_id: {report_id}"
                )

        # Get version ID for response
        final_version_id = None
        if use_specific_version and target_version_for_generation:
            final_version_id = target_version_for_generation.id
        else:
            async with async_session_scope() as session:
                version_stmt = select(ReportVersion).where(
                    ReportVersion.report_id == report_id, ReportVersion.version == current_version
                )
                version_result = await session.execute(version_stmt)
                version_obj = version_result.scalar_one_or_none()
                if version_obj:
                    final_version_id = version_obj.id

        return GenerateInfographicResponse(
            success=True,
            message="Infographic has been generated successfully.",
            report_version_id=final_version_id,
            version=current_version,
            info_pdf_s3_uri=infographic_s3_uri,
            info_pdf_generation_time=updated_uris.get("info_pdf_generation_time"),
            report_id=report_id,
            modified=True,
            already_exists=False,  # Newly generated, not pre-existing
        )

    except Exception as e:
        logger.error(f"Error generating infographic: {e}")
        # Update report status to ERROR_GENERATION_REPORT on error
        try:
            async with async_session_scope() as session:
                await update_report_status_by_chat_or_report_id(
                    report_id=report_id,
                    status=ReportStatus.ERROR_GENERATION_REPORT.value,
                    session=session,
                )
                logger.info(
                    f"Set report status to ERROR_GENERATION_REPORT for infographic generation, report_id: {report_id}"
                )
        except Exception as update_error:
            logger.error(
                f"Failed to update report status to ERROR_GENERATION_REPORT: {update_error}"
            )

        return JSONResponse(
            status_code=500,
            content={
                "success": False,
                "error": "I ran into a problem while creating your infographic. Please try generating it again.",
            },
        )
