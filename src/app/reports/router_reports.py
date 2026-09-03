"""reports routes: reports.

Split out of ``app/reports/router.py`` to keep each router file under
the ~400-line ceiling in R-STRUCT-3. Handlers are unchanged.
"""

"""HTTP routes for the reports bounded context.

Handlers moved verbatim from ``src/resources/routers/api.py`` during the
R-STRUCT-1 migration, which split that 10,257-line module across seven
contexts. Handler bodies are unchanged; only the import block and the
``APIRouter`` they attach to are new.
"""

"""api.py: Authentication API for the Casper backend"""
import asyncio
import re
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.concurrency import run_in_threadpool
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.email import send_report_notification_email
from app.adapters.s3 import (
    extract_s3_key,
    get_or_create_presigned_url,
    get_s3_instance,
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
    get_table_id_markdown_map,
)
from app.chats.repository import get_user_chat
from app.core.constants import (
    MAX_REPORT_VERSIONS_FREE,
    MAX_REPORT_VERSIONS_PAID,
    S3_REPORTS_BASE_PATH,
)
from app.core.db import async_session_scope
from app.core.enums import ReportStatus, SubscriptionTier
from app.core.logging import setup_logging
from app.core.redis import get_redis_instance
from app.core.utils import extract_content
from app.deliverables.service_entry import generate_report_output
from app.models import Report, ReportVersion
from app.observability.cloudwatch_utils import insert_cloudwatch_logs
from app.reports.repository import (
    create_new_report_version,
    detect_modified_cards,
    finalize_report_version,
    get_file_s3_path,
    update_report,
    update_report_status_by_chat_or_report_id,
    update_specific_version_s3_uri,
    verify_report_ownership,
)
from app.reports.schemas import (
    BatchPresignedUrlRequest,
    BatchPresignedUrlResponse,
    GenerateReportRequest,
    GenerateReportResponse,
    ReportPresignedUrlResponse,
)
from app.wallet.repository import get_active_subscription

router = APIRouter()
logger = setup_logging(__file__)
redis_instance = get_redis_instance()


async def create_new_version_internal(report_id: str, session: AsyncSession) -> dict[str, Any]:
    """
    Internal function to create a new report version when active version is already generated.

    Args:
        report_id: The report ID
        session: Database session

    Returns:
        Dict with success status, new version details, and card counts
    """
    try:
        # First, get the poster_image_url from Version 1's ReportVersion to preserve it
        version1_stmt = select(ReportVersion).where(
            ReportVersion.report_id == report_id, ReportVersion.version == 1
        )
        version1_result = await session.execute(version1_stmt)
        version1 = version1_result.scalar_one_or_none()

        existing_poster_url = None
        if version1 and version1.poster_image_url:
            existing_poster_url = version1.poster_image_url
            logger.info(f"Preserving poster_image_url from Version 1: {existing_poster_url}")
        else:
            logger.warning(f"No poster_image_url found in Version 1 for report {report_id}")

        # Detect which cards were modified
        logger.info(f"Detecting modified cards for report {report_id}")
        detection_result = await detect_modified_cards(report_id=report_id, session=session)

        if not detection_result.get("success"):
            logger.error(f"Failed to detect modified cards: {detection_result.get('error')}")
            return {"success": False, "error": "Failed to analyze report cards"}

        modified_cards_data = detection_result.get("modified_cards", [])
        unchanged_cards_data = detection_result.get("unchanged_cards", [])
        modified_count = detection_result.get("modified_count", 0)
        unchanged_count = detection_result.get("unchanged_count", 0)

        logger.info(f"Detection complete: {modified_count} modified, {unchanged_count} unchanged")

        # Combine both lists for create_new_report_version
        all_cards_data = modified_cards_data + unchanged_cards_data

        # Sort by sequence to maintain order
        all_cards_data.sort(key=lambda x: x.get("sequence", 0))

        # Create the new report version
        logger.info(f"Creating new version for report {report_id}")
        version_response = await create_new_report_version(
            session=session, report_id=report_id, cards_data=all_cards_data
        )

        if not version_response.get("success"):
            logger.error(f"Failed to create new report version: {version_response.get('error')}")
            return {"success": False, "error": "Failed to create new report version"}

        new_version = version_response.get("version")
        version_id = version_response.get("version_id")

        # Reset Report table's denormalized fields for new version
        logger.info(f"Resetting Report table fields for new version {new_version}")
        reset_result = await update_report(
            session=session,
            report_id=report_id,
            update_data={
                "generated_at": None,  # Reset - new version not generated yet
                "s3_uri": {
                    "md": None,
                    "pdf": None,
                    "html": None,
                    "pptx": None,
                    "info_pdf": None,
                    "md_explicit": False,
                    "html_explicit": False,
                },
                "status": ReportStatus.ANALYSIS_COMPLETED.value,
                "poster_image_url": existing_poster_url,  # Preserve poster URL across versions
            },
        )

        if not reset_result.get("success"):
            logger.warning(f"Failed to reset Report fields: {reset_result.get('error')}")
            # Continue anyway - version was created successfully

        logger.info(f"Successfully created version {new_version} with ID {version_id}")

        return {
            "success": True,
            "version": new_version,
            "version_id": version_id,
            "modified_count": modified_count,
            "unchanged_count": unchanged_count,
        }

    except Exception as e:
        logger.error(f"Error in create_new_version_internal: {e!s}", exc_info=True)
        return {"success": False, "error": str(e)}


@router.post(
    "/generate-report",
    response_model=GenerateReportResponse,
    status_code=200,
    responses={
        404: {"model": ErrorResponse},
        401: {"model": ErrorResponse},
        422: {"model": ErrorResponse},
        500: {"model": ErrorResponse},
    },
)
async def generate_report(
    data: GenerateReportRequest, user_id: str = Depends(get_current_active_user)
):
    """
    Generate report endpoint - handles both initial generation (v1) and regeneration (v2+).

    **Auto-Detection:**
    - If active version not generated → Generates v1
    - If active version already generated → Creates new version automatically, then generates

    Frontend always calls this single endpoint - no need to know about versions!
    """
    logger.info(
        f"Generate report request received for user_id: {user_id} and report_id: {data.report_id}"
    )
    report_id = data.report_id
    output_type = (data.output_type or "pdf").lower().strip()
    if output_type not in ("pdf", "html", "md"):
        return JSONResponse(
            status_code=422,
            content={
                "success": False,
                "error": "Invalid output_type. Must be 'pdf', 'html', or 'md'.",
            },
        )
    logger.info(f"Output type requested: {output_type} for report_id: {report_id}")
    _INTERNAL_S3_KEYS = {"md_explicit", "html_explicit"}

    def _clean_s3_uri(uri: dict, for_output_type: str = None) -> dict:
        cleaned = {k: v for k, v in (uri or {}).items() if k not in _INTERNAL_S3_KEYS}
        if for_output_type:
            result = {for_output_type: cleaned.get(for_output_type)}
            time_key = f"{for_output_type}_generation_time"
            if cleaned.get(time_key):
                result[time_key] = cleaned[time_key]
            return result
        return cleaned

    try:
        async with async_session_scope() as session:
            # Check if user exists and credentials are valid
            db_response = await check_user_by_id(user_id=user_id, session=session)
            if not db_response.get("success", False):
                logger.error(
                    f"Failed to check user by id: {db_response.get('error')} for user_id: {user_id} and report_id: {data.report_id}"
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
                    f"User with ID {user_id} not found or token is invalid for report_id: {data.report_id}"
                )
                return JSONResponse(
                    status_code=401, content={"success": False, "error": "Unauthorized request"}
                )

        async with async_session_scope() as session:
            user_plan_result = await get_active_subscription(user_id=user_id, session=session)
            user_tier = (
                user_plan_result.get("subscription", {}).get(
                    "current_tier", SubscriptionTier.FREE.value
                )
                if user_plan_result.get("subscription")
                else SubscriptionTier.FREE.value
            )

        if output_type in ("md", "html") and user_tier == SubscriptionTier.FREE.value:
            logger.warning(
                f"Free-tier user {user_id} attempted to generate {output_type.upper()} for report_id: {report_id}"
            )
            return JSONResponse(
                status_code=403,
                content={
                    "success": False,
                    "error": f"{output_type.upper()} export is available on paid plans. Please upgrade to use this feature.",
                },
            )

        async with async_session_scope() as session:
            db_response = await get_user_details(user_id=user_id, session=session)
            if not db_response.get("success", False):
                logger.error(
                    f"Database error: Failed to get user details: {db_response.get('error')} for user_id: {user_id} and report_id: {data.report_id}"
                )
                return JSONResponse(
                    status_code=500,
                    content={
                        "success": False,
                        "error": "Something unexpected happened. Please try again later.",
                    },
                )

        user_name = db_response.get("user", {}).get("user_name", None)
        user_email = db_response.get("user", {}).get("email", None)

        async with async_session_scope() as session:
            ownership = await verify_report_ownership(
                report_id=data.report_id, user_id=user_id, session=session
            )
            if not ownership["authorized"]:
                return JSONResponse(
                    status_code=ownership["status_code"],
                    content={"success": False, "error": ownership["error"]},
                )

        report_data = ownership["report"]

        # Sanity check: If specific version requested, check if PDF already exists
        target_version_for_generation = None  # Will be set if generating for specific old version
        use_specific_version = False

        if data.report_version_id:
            async with async_session_scope() as session:
                version_stmt = select(ReportVersion).where(
                    ReportVersion.id == data.report_version_id, ReportVersion.report_id == report_id
                )
                version_result = await session.execute(version_stmt)
                target_version = version_result.scalar_one_or_none()

                if not target_version:
                    logger.warning(
                        f"Version not found: {data.report_version_id} for report_id: {report_id}"
                    )
                    return JSONResponse(
                        status_code=404,
                        content={"success": False, "error": "The requested version was not found."},
                    )

                s3_uri = target_version.s3_uri or {}
                explicit_key = f"{output_type}_explicit"  # e.g. "md_explicit", "html_explicit"

                if s3_uri.get(output_type):
                    # File exists in S3. Check if it was explicitly generated or a byproduct.
                    # For PDF: always treat as explicit (no flag needed).
                    # For md/html: check the *_explicit flag. Key absent = old entry = treat as explicit.
                    is_explicit = (
                        output_type == "pdf"
                        or explicit_key not in s3_uri
                        or s3_uri.get(explicit_key) == True
                    )

                    if is_explicit:
                        logger.info(
                            f"{output_type.upper()} already explicitly generated for version {target_version.version}, returning existing"
                        )
                        return JSONResponse(
                            status_code=200,
                            content={
                                "success": True,
                                "message": "Your report is up to date.",
                                "report_id": report_id,
                                "report_version_id": target_version.id,
                                "version": target_version.version,
                                "output_type": output_type,
                                "s3_uri": _clean_s3_uri(s3_uri, for_output_type=output_type),
                                "already_exists": True,
                            },
                        )
                    else:
                        # File exists as byproduct — flip flag to explicit and return success
                        logger.info(
                            f"{output_type.upper()} exists as byproduct for version {target_version.version}, marking as explicit"
                        )
                        target_version.s3_uri = {**s3_uri, explicit_key: True}
                        await session.commit()

                        return JSONResponse(
                            status_code=200,
                            content={
                                "success": True,
                                "message": f"Report {output_type.upper()} has been generated successfully.",
                                "report_id": report_id,
                                "report_version_id": target_version.id,
                                "version": target_version.version,
                                "output_type": output_type,
                                "s3_uri": _clean_s3_uri(
                                    target_version.s3_uri, for_output_type=output_type
                                ),
                                "already_exists": False,
                            },
                        )

                # Use this specific version for generation
                target_version_for_generation = target_version
                use_specific_version = True
                logger.info(
                    f"Will generate {output_type.upper()} for specific version {target_version.version}"
                )

        async def send_cloudwatch_logs():
            """Send cloudwatch logs for report generation events"""
            try:
                # Get chat_id from report_data
                chat_id = report_data.get("chat_id")
                if not chat_id:
                    logger.warning(f"No chat_id found for report_id: {report_id}")
                    return

                # Get chat title from chat_id
                chat_title = None
                async with async_session_scope() as session:
                    chat_response = await get_user_chat(
                        user_id=user_id, chat_id=chat_id, session=session
                    )
                    if chat_response.get("success", False) and chat_response.get("message"):
                        chat_title = chat_response.get("message", {}).get("chat_title")

                # Log "Report generation started"
                start_data = {
                    "event": "Report generation started",
                    "event_success": True,
                    "timestamp": datetime.now(UTC).isoformat(),
                    "chat_id": chat_id,
                    "chat_title": chat_title or "N/A",
                }
                await insert_cloudwatch_logs(data=start_data, user_id=user_id)
                logger.info(f"Logged 'Report generation started' for report_id: {report_id}")

            except Exception as e:
                logger.error(f"Error in send_cloudwatch_logs start: {e} for report_id: {report_id}")

        # Start the cloudwatch logging in background
        asyncio.create_task(send_cloudwatch_logs())

        # Set status to GENERATING_OUTPUT when report generation starts
        async with async_session_scope() as session:
            await update_report_status_by_chat_or_report_id(
                report_id=report_id, status=ReportStatus.GENERATING_OUTPUT.value, session=session
            )
            logger.info(f"Set report status to GENERATING_OUTPUT for report_id: {report_id}")

        # Check if the ACTIVE VERSION is already generated
        # If YES: Check for changes before auto-creating new version
        # If NO: Generate the current version (first time generation)
        # Skip this logic if generating for a specific old version (report_version_id provided)

        # Track if a new version was created (important for s3_uri handling)
        new_version_created = False

        if use_specific_version and target_version_for_generation:
            # Generating for a specific old version - use that version directly
            current_version = target_version_for_generation.version
            target_version_id = target_version_for_generation.id
            logger.info(
                f"Generating for specific version {current_version} (version_id: {target_version_id}) for report_id: {report_id}"
            )
        else:
            target_version_id = None  # Will be set after we determine the version
            async with async_session_scope() as session:
                version_stmt = select(ReportVersion).where(
                    ReportVersion.report_id == report_id, ReportVersion.is_active.is_(True)
                )
                version_result = await session.execute(version_stmt)
                active_version = version_result.scalar_one_or_none()

                # Check if ANY output exists (not just generated_at)
                # This includes md/html generation times, info_pdf_generation_time and pptx_generation_time
                s3_uri_check = active_version.s3_uri or {} if active_version else {}
                has_any_output = (
                    (active_version and active_version.generated_at)
                    or s3_uri_check.get("md_generation_time")
                    or s3_uri_check.get("html_generation_time")
                    or s3_uri_check.get("info_pdf_generation_time")
                    or s3_uri_check.get("pptx_generation_time")
                )

                if has_any_output:
                    # Version already has some output → Check if user made changes first
                    logger.info(
                        f"Version {active_version.version} already has output(s). Checking for changes before regeneration for report_id: {report_id}"
                    )

                    # Check if user has made any changes since last generation
                    detection_result = await detect_modified_cards(
                        report_id=report_id, session=session
                    )

                    if detection_result.get("success"):
                        modified_count = detection_result.get("modified_count", 0)
                        unchanged_count = detection_result.get("unchanged_count", 0)

                        logger.info(
                            f"Report regeneration - Change detection: {modified_count} modified, {unchanged_count} unchanged"
                        )

                        if modified_count == 0:
                            # No changes made - check if requested output_type exists for this version
                            current_s3_uri = active_version.s3_uri or {}
                            explicit_key = f"{output_type}_explicit"

                            if current_s3_uri.get(output_type):
                                is_explicit = (
                                    output_type == "pdf"
                                    or explicit_key not in current_s3_uri
                                    or current_s3_uri.get(explicit_key) == True
                                )

                                if is_explicit:
                                    logger.warning(
                                        f"{output_type.upper()} already explicitly generated for version {active_version.version} and no changes detected"
                                    )
                                    return JSONResponse(
                                        status_code=200,
                                        content={
                                            "success": True,
                                            "message": "Your report is up to date.",
                                            "report_id": report_id,
                                            "report_version_id": active_version.id,
                                            "version": active_version.version,
                                            "output_type": output_type,
                                            "s3_uri": _clean_s3_uri(
                                                current_s3_uri, for_output_type=output_type
                                            ),
                                            "generated_at": active_version.generated_at.isoformat()
                                            if active_version.generated_at
                                            else None,
                                            "modified": False,
                                            "already_exists": True,
                                        },
                                    )
                                else:
                                    # File exists as byproduct — flip flag to explicit and return
                                    logger.info(
                                        f"{output_type.upper()} exists as byproduct for version {active_version.version}, marking as explicit"
                                    )
                                    active_version.s3_uri = {**current_s3_uri, explicit_key: True}
                                    await session.commit()

                                    return JSONResponse(
                                        status_code=200,
                                        content={
                                            "success": True,
                                            "message": f"Report {output_type.upper()} has been generated successfully.",
                                            "report_id": report_id,
                                            "report_version_id": active_version.id,
                                            "version": active_version.version,
                                            "output_type": output_type,
                                            "s3_uri": _clean_s3_uri(
                                                active_version.s3_uri, for_output_type=output_type
                                            ),
                                            "generated_at": active_version.generated_at.isoformat()
                                            if active_version.generated_at
                                            else None,
                                            "modified": False,
                                            "already_exists": False,
                                        },
                                    )
                            else:
                                # Other output exists but not the requested type - generate for same version
                                current_version = active_version.version
                                target_version_id = active_version.id
                                logger.info(
                                    f"Generating {output_type.upper()} for existing version {current_version} (other outputs exist but not {output_type})"
                                )
                        else:
                            # User made changes, proceed with creating new version
                            logger.info(
                                f"User made {modified_count} changes, proceeding to create new version for report"
                            )

                            # Enforce per-tier version limit before creating a new version
                            _is_paid = user_tier != SubscriptionTier.FREE.value
                            _max_versions = (
                                MAX_REPORT_VERSIONS_PAID if _is_paid else MAX_REPORT_VERSIONS_FREE
                            )
                            if active_version.version >= _max_versions:
                                _upgrade_hint = (
                                    ""
                                    if _is_paid
                                    else " Upgrade to a paid plan to unlock up to 5 versions."
                                )
                                logger.warning(
                                    f"User {user_id} hit version limit ({_max_versions}) for report_id: {report_id}"
                                )
                                return JSONResponse(
                                    status_code=403,
                                    content={
                                        "success": False,
                                        "error": f"This report has reached the maximum of {_max_versions} versions.{_upgrade_hint}",
                                    },
                                )

                            # Create new version
                            new_version_result = await create_new_version_internal(
                                report_id=report_id, session=session
                            )

                            if not new_version_result.get("success"):
                                logger.error(
                                    f"Failed to create new version: {new_version_result.get('error')}"
                                )
                                return JSONResponse(
                                    status_code=500,
                                    content={
                                        "success": False,
                                        "error": "Failed to create new version for regeneration. Please try again.",
                                    },
                                )

                            current_version = new_version_result.get("version")
                            target_version_id = new_version_result.get("version_id")
                            modified_count = new_version_result.get("modified_count", 0)
                            unchanged_count = new_version_result.get("unchanged_count", 0)
                            new_version_created = True  # Flag that new version was created

                            logger.info(
                                f"New version {current_version} created successfully. Modified: {modified_count}, Unchanged: {unchanged_count}"
                            )
                    else:
                        # If detection fails, proceed anyway (don't block user)
                        logger.warning(
                            f"Could not detect changes for report regeneration: {detection_result.get('error')}, proceeding anyway"
                        )

                        # Enforce per-tier version limit before creating a new version
                        _is_paid = user_tier != SubscriptionTier.FREE.value
                        _max_versions = (
                            MAX_REPORT_VERSIONS_PAID if _is_paid else MAX_REPORT_VERSIONS_FREE
                        )
                        if active_version.version >= _max_versions:
                            _upgrade_hint = (
                                ""
                                if _is_paid
                                else " Upgrade to a paid plan to unlock up to 5 versions."
                            )
                            logger.warning(
                                f"User {user_id} hit version limit ({_max_versions}) for report_id: {report_id}"
                            )
                            return JSONResponse(
                                status_code=403,
                                content={
                                    "success": False,
                                    "error": f"This report has reached the maximum of {_max_versions} versions.{_upgrade_hint}",
                                },
                            )

                        # Create new version anyway
                        new_version_result = await create_new_version_internal(
                            report_id=report_id, session=session
                        )

                        if not new_version_result.get("success"):
                            logger.error(
                                f"Failed to create new version: {new_version_result.get('error')}"
                            )
                            return JSONResponse(
                                status_code=500,
                                content={
                                    "success": False,
                                    "error": "Failed to create new version for regeneration. Please try again.",
                                },
                            )

                        current_version = new_version_result.get("version")
                        target_version_id = new_version_result.get("version_id")
                        new_version_created = True  # Flag that new version was created
                else:
                    # First time generation for this version
                    current_version = active_version.version if active_version else 1
                    target_version_id = active_version.id if active_version else None
                    logger.info(
                        f"First time generation for report_id: {report_id}, version: {current_version}"
                    )

        # Get cards - use version-specific cards if generating for old version
        async with async_session_scope() as session:
            if use_specific_version and target_version_for_generation:
                # Get cards linked to the specific version
                db_response = await get_cards_for_version(
                    report_id=report_id,
                    version_id=target_version_for_generation.id,
                    session=session,
                )
                logger.info(
                    f"Using cards from version snapshot for version {target_version_for_generation.version}"
                )
            else:
                # Get current active cards
                db_response = await get_report_cards(report_id=report_id, session=session)

            if not db_response.get("success", False):
                logger.error(
                    f"Failed to get report cards: {db_response.get('error')} for report_id: {report_id}"
                )
                return JSONResponse(
                    status_code=500,
                    content={
                        "success": False,
                        "error": "Something unexpected happened. Please try again later.",
                    },
                )

        cards = db_response.get("cards", [])
        if not cards:
            logger.error(f"No report cards found for report_id: {report_id}")
            return JSONResponse(
                status_code=404, content={"success": False, "error": "Report not found"}
            )

        report_title = report_data.get("title", "")

        report_cards = []
        for card in cards:
            if card.get("type") == "title":
                report_cards.append(
                    {
                        "section": [
                            {
                                "name": "title",
                                "content": extract_content(card, "title"),
                                "tables": [
                                    {
                                        "visualization": "",
                                        "table_id": "",
                                        "table_title": "",
                                        "s3_uri": "",
                                    }
                                ],
                            }
                        ],
                        "sub_sections": [],
                        "citations": {},
                        "summary": "",
                    }
                )

            elif card.get("type") == "subtitle":
                report_cards.append(
                    {
                        "section": [
                            {
                                "name": "subtitle",
                                "content": extract_content(card, "content"),
                                "tables": [
                                    {
                                        "visualization": "",
                                        "table_id": "",
                                        "table_title": "",
                                        "s3_uri": "",
                                    }
                                ],
                            }
                        ],
                        "sub_sections": [],
                        "citations": {},
                        "summary": "",
                    }
                )

            elif card.get("type") == "toc":
                report_cards.append(
                    {
                        "section": [
                            {
                                "name": "table_of_contents",
                                "content": extract_content(card, "content"),
                                "tables": [
                                    {
                                        "visualization": "",
                                        "table_id": "",
                                        "table_title": "",
                                        "s3_uri": "",
                                    }
                                ],
                            }
                        ],
                        "sub_sections": [],
                        "citations": {},
                        "summary": "",
                    }
                )

            elif card.get("type") == "es":
                report_cards.append(
                    {
                        "section": [
                            {
                                "name": "executive_summary",
                                "content": extract_content(card, "content"),
                                "tables": [
                                    {
                                        "visualization": "",
                                        "table_id": "",
                                        "table_title": "",
                                        "s3_uri": "",
                                    }
                                ],
                            }
                        ],
                        "sub_sections": [],
                        "citations": {},
                        "summary": "",
                    }
                )

            elif card.get("type") == "section":
                if card.get("section") and isinstance(card.get("section"), list):
                    report_cards.append(
                        {
                            "section": card.get("section", []),
                            "sub_sections": card.get("sub_sections", []),
                            "citations": card.get("citations", {}),
                            "summary": card.get("summary", ""),
                        }
                    )
                else:
                    report_cards.append(
                        {
                            "section": [
                                {
                                    "name": card.get("title", ""),
                                    "content": extract_content(card, "content"),
                                    "tables": [
                                        {
                                            "visualization": "",
                                            "table_id": "",
                                            "table_title": "",
                                            "s3_uri": "",
                                        }
                                    ],
                                }
                            ],
                            "sub_sections": card.get("sub_sections", []),
                            "citations": card.get("citations", {}),
                            "summary": card.get("summary", ""),
                        }
                    )
            else:
                logger.error(
                    f"Unknown card type: {card.get('type')} for report_id: {report_id} at sequence: {card.get('sequence')}"
                )
                continue

        # Use report title only in the file name (no report_id for better UX)
        base_filename = (
            report_title.strip(".").strip().replace(":", " ").replace("-", " ").replace(" ", "_")
        )
        base_filename = re.sub(r"_+", "_", base_filename)  # collapse multiple underscores

        logger.info(f"Getting table_id_markdown_map for report_id: {report_id}")
        async with async_session_scope() as session:
            table_id_map = await get_table_id_markdown_map(report_id=report_id, session=session)
            if not table_id_map.get("success", False):
                logger.error(
                    f"Failed to get table_id_markdown_map: {table_id_map.get('error')} for report_id: {report_id}"
                )
                return JSONResponse(
                    status_code=500, content={"success": False, "error": "Internal server error"}
                )
            table_id_map = table_id_map.get("table_id_markdown_map", {})

        report_type = report_data.get("report_type") or "study"

        # Get existing poster_image_url if it exists (needed for PDF and HTML output)
        existing_poster_url = None

        if output_type in ("pdf", "html"):
            async with async_session_scope() as session:
                report_stmt = select(Report).where(Report.id == report_id)
                report_result = await session.execute(report_stmt)
                report = report_result.scalar_one_or_none()
                if report and report.report_type:
                    report_type = report.report_type
                if report and report.domain_name == "due_diligence" and report_type == "brief":
                    report_type = "study"
                    await update_report(
                        report_id=report_id,
                        update_data={"report_type": report_type},
                        session=session,
                    )
                    logger.warning(
                        "Corrected invalid persisted due_diligence report_type='brief' "
                        f"to 'study' before output generation for report_id: {report_id}"
                    )

                if report and report.poster_image_url:
                    existing_poster_url = report.poster_image_url
                    logger.info(f"Found existing poster URL in Report table: {existing_poster_url}")
                elif current_version > 1:
                    version1_stmt = select(ReportVersion).where(
                        ReportVersion.report_id == report_id, ReportVersion.version == 1
                    )
                    version1_result = await session.execute(version1_stmt)
                    version1 = version1_result.scalar_one_or_none()

                    if version1 and version1.poster_image_url:
                        existing_poster_url = version1.poster_image_url
                        logger.info(
                            f"Found existing poster URL from Version 1's ReportVersion: {existing_poster_url}"
                        )
                    else:
                        logger.warning(
                            f"No poster_image_url found in Version 1's ReportVersion for report_id {report_id}, will generate new poster"
                        )
                else:
                    logger.info(
                        "Version 1 generation - no existing poster found, will create new poster image"
                    )

            logger.info(
                f"Existing poster URL for report_id {report_id}: {existing_poster_url} (version: {current_version})"
            )

        # Get existing s3_uri for the resolved version (for reuse logic in generate_report_output)
        _existing_s3 = {}
        if target_version_id:
            async with async_session_scope() as session:
                _ver_stmt = select(ReportVersion).where(ReportVersion.id == target_version_id)
                _ver_result = await session.execute(_ver_stmt)
                _ver_obj = _ver_result.scalar_one_or_none()
                if _ver_obj:
                    _existing_s3 = _ver_obj.s3_uri or {}

        _reuse_keys = [k for k in ("md", "html") if _existing_s3.get(k)]
        logger.info(
            f"Calling generate_report_output for report_id: {report_id}, version: {current_version}, "
            f"output_type: {output_type}, reusable_intermediates: {_reuse_keys or 'none'}, "
            f"existing_poster: {bool(existing_poster_url)}, report_type: {report_type}"
        )
        generation_result = await generate_report_output(
            report_cards=report_cards,
            table_id_map=table_id_map,
            user_id=user_id,
            user_name=user_name,
            report_id=report_id,
            base_filename=base_filename,
            report_title=report_title,
            output_type=output_type,
            existing_s3_uri=_existing_s3,
            chat_id=report_data.get("chat_id"),
            chat_title=report_title,
            version=current_version,
            existing_poster_url=existing_poster_url,
            report_type=report_type,
        )
        logger.info(
            f"generate_report_output completed for report_id: {report_id}, version: {current_version}, "
            f"output_type: {output_type}, success: {generation_result.get('success')}"
        )

        if not generation_result.get("success"):
            logger.error(
                f"generate_report_output failed for user {user_id} and report_id: {report_id}: {generation_result}"
            )
            return JSONResponse(
                status_code=500,
                content={
                    "success": False,
                    "error": "Something unexpected happened. Please try again later.",
                },
            )

        result_data = generation_result["data"]
        s3_results = result_data["s3_paths"]
        attachment_data = result_data["attachment_data"]
        final_report_generation_time = result_data["report_generation_time"]
        poster_image_url = result_data["poster_image_url"]

        uploaded_file_types = []
        s3_paths = {}
        file_types = ["pdf", "md", "html"]
        for file_type, path in s3_results.items():
            if file_type in file_types and path:
                uploaded_file_types.append(file_type)
                s3_paths[file_type] = path

        if output_type not in uploaded_file_types:
            logger.error(
                f"Requested {output_type} not uploaded to s3 for user {user_id} and report_id: {report_id}"
            )
            return JSONResponse(
                status_code=500,
                content={
                    "success": False,
                    "error": "Something unexpected happened. Please try again later.",
                },
            )

        # Add generation time markers to s3_paths for md/html
        if s3_paths.get("md") and output_type in ("md", "html"):
            s3_paths["md_generation_time"] = final_report_generation_time.isoformat()
        if s3_paths.get("html") and output_type in ("html",):
            s3_paths["html_generation_time"] = final_report_generation_time.isoformat()
        # For pdf output_type: process_report_cards generates all three so mark them all
        if output_type == "pdf":
            if s3_paths.get("md"):
                s3_paths["md_generation_time"] = final_report_generation_time.isoformat()
            if s3_paths.get("html"):
                s3_paths["html_generation_time"] = final_report_generation_time.isoformat()

        # Set explicit flag for the requested output_type.
        # For byproducts: set *_explicit = False so old records (missing the key)
        # don't incorrectly appear as explicit in version history.
        # Guard: never downgrade an existing True to False (user already requested it before).
        if output_type == "md":
            s3_paths["md_explicit"] = True
            if not _existing_s3.get("html") and _existing_s3.get("html_explicit") is not True:
                s3_paths["html_explicit"] = False
        elif output_type == "html":
            s3_paths["html_explicit"] = True
            if s3_paths.get("md") and _existing_s3.get("md_explicit") is not True:
                s3_paths["md_explicit"] = False
        elif output_type == "pdf":
            if s3_paths.get("md") and _existing_s3.get("md_explicit") is not True:
                s3_paths["md_explicit"] = False
            if s3_paths.get("html") and _existing_s3.get("html_explicit") is not True:
                s3_paths["html_explicit"] = False

        # Email notification only for PDF output
        if output_type == "pdf":
            if attachment_data:
                attachment_data = [
                    data for data in attachment_data if data.get("mime_type") == "application/pdf"
                ]
            else:
                logger.warning(
                    f"No attachment data for report_id: {report_id} (PDF generated from cached intermediates) — skipping email"
                )

        if output_type == "pdf" and attachment_data and report_title and user_email:
            logger.info(
                f"Preparing data for email notification for user {user_id} and report_id: {report_id}"
            )

            async def send_email_background():
                try:
                    is_email_sent = await run_in_threadpool(
                        lambda: send_report_notification_email(
                            recipient_email=user_email,
                            user_name=user_name,
                            report_title=report_title,
                            attachment_data=attachment_data,
                            report_generation_time=final_report_generation_time,
                        )
                    )
                    if is_email_sent:
                        # Get chat_id and chat_title for cloudwatch logging
                        chat_id = report_data.get("chat_id")
                        chat_title = None
                        if chat_id:
                            async with async_session_scope() as session:
                                chat_response = await get_user_chat(
                                    user_id=user_id, chat_id=chat_id, session=session
                                )
                                if chat_response.get("success", False) and chat_response.get(
                                    "message"
                                ):
                                    chat_title = chat_response.get("message", {}).get("chat_title")

                        cloudwatch_data = {
                            "event": "Report sent via email",
                            "event_success": True,
                            "timestamp": final_report_generation_time.isoformat(),
                            "chat_id": chat_id or "N/A",
                            "chat_title": chat_title or "N/A",
                        }
                        try:
                            await insert_cloudwatch_logs(data=cloudwatch_data, user_id=user_id)
                        except Exception as e:
                            logger.error(
                                f"Error in inserting cloudwatch logs: {e} for user_id: {user_id} and chat_id: {chat_id}"
                            )
                        logger.info(
                            f"Email sent successfully to user {user_name} with user_id {user_id} and report_id: {report_id}"
                        )
                    else:
                        logger.error(
                            f"Failed to send email to user {user_name} with user_id {user_id} and report_id: {report_id}"
                        )
                except Exception as e:
                    logger.error(
                        f"Error in sending email to user {user_name} with user_id {user_id} and report_id: {report_id}: {e}"
                    )

            asyncio.create_task(send_email_background())

        # Update database with generated output paths
        # IMPORTANT: When generating for a specific OLD (non-active) version, only update that version's s3_uri
        # When generating for the ACTIVE version (whether report_version_id provided or not), update BOTH tables
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
                # ========================================
                logger.info(
                    f"Updating s3_uri for specific OLD version {current_version} (NOT updating Report table)"
                )
                db_response = await update_specific_version_s3_uri(
                    report_id=report_id,
                    version_id=target_version_for_generation.id,
                    s3_uri_updates=s3_paths,
                    generated_at=final_report_generation_time if output_type == "pdf" else None,
                    session=session,
                )
                if not db_response.get("success", False):
                    logger.error(
                        f"Database error: Failed to update specific version s3_uri: {db_response.get('error')} for report_id: {report_id}"
                    )
                    return JSONResponse(
                        status_code=500,
                        content={
                            "success": False,
                            "error": "Something unexpected happened. Please try again later.",
                        },
                    )
                logger.info(
                    f"Generated {output_type.upper()} for specific old version {current_version} - cards already linked from version snapshot"
                )
            else:
                # ========================================
                # FLOW B: Generating for CURRENT/ACTIVE version
                # Update both Report table and active ReportVersion
                # ========================================
                updated_report_data = {
                    "s3_uri": s3_paths,
                    "status": ReportStatus.OUTPUT_GENERATED.value,
                }
                if output_type in ("pdf", "html"):
                    if output_type == "pdf":
                        updated_report_data["generated_at"] = final_report_generation_time
                    if poster_image_url:
                        updated_report_data["poster_image_url"] = poster_image_url
                db_response = await update_report(
                    report_id=report_id, update_data=updated_report_data, session=session
                )
                if not db_response.get("success", False):
                    logger.error(
                        f"Database error: Failed to update report: {db_response.get('error')} for user_id: {user_id} and report_id: {report_id}"
                    )
                    return JSONResponse(
                        status_code=500,
                        content={
                            "success": False,
                            "error": "Something unexpected happened. Please try again later.",
                        },
                    )

                # Finalize the report version by linking current cards
                # For version 1 (first time generation): Call finalize_report_version to populate report_version_cards
                # For version 2+: Already populated by create_new_report_version, skip finalization
                if current_version == 1:
                    logger.info(f"Finalizing version 1 for report_id: {report_id}")
                    finalize_result = await finalize_report_version(
                        session=session, report_id=report_id
                    )

                    if finalize_result.get("success"):
                        cards_linked = finalize_result.get("cards_linked", 0)
                        already_finalized = finalize_result.get("already_finalized", False)
                        if already_finalized:
                            logger.info(f"Version 1 already finalized for report_id: {report_id}")
                        else:
                            logger.info(
                                f"Successfully finalized version 1 with {cards_linked} cards for report_id: {report_id}"
                            )
                    else:
                        logger.warning(
                            f"Failed to finalize version 1: {finalize_result.get('error')} for report_id: {report_id}"
                        )
                        logger.warning(
                            "Report generation succeeded but version finalization failed - this is not critical"
                        )
                else:
                    logger.info(
                        f"Version {current_version} already finalized during version creation - skipping finalize call"
                    )

        # if raw_md_content:
        #     try:
        #         async def publish_report_background():
        #             try:
        #                 publish_report_data = await publish_report(raw_md_content=raw_md_content, user_id=user_id, user_name=user_name, report_title=report_title)
        #                 publish_data = {
        #                     **publish_report_data,
        #                     "report_id": report_id,
        #                     "page_count": page_count,
        #                     "table_count": table_count,
        #                     "viz_count": viz_count,
        #                     "source_count": len(report_data.get('citations', []))
        #                 }
        #                 if publish_report_data:
        #                     async with async_session_scope() as session:
        #                         db_response = await insert_publish_details(publish_data=publish_data, session=session)
        #                         if not db_response.get('success', False):
        #                             logger.error(f"Database error: Failed to insert publish details: {db_response.get('error')} for user_id: {user_id} and report_id: {report_id}")
        #             except Exception as e:

        #                 logger.error(f"Error in publishing report: {e} for user_id: {user_id} and report_id: {report_id}")
        #         # TODO: Currently disabled due to increased costing of heygen
        #         # asyncio.create_task(publish_report_background())
        #     except Exception as e:
        #         logger.error(f"Error in publishing report: {e} for user_id: {user_id} and report_id: {report_id}")

        # Log "Report generation completed" in background
        async def log_completion():
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
                    "event": "Report generation completed",
                    "event_success": True,
                    "timestamp": datetime.now(UTC).isoformat(),
                    "chat_id": chat_id or "N/A",
                    "chat_title": chat_title or "N/A",
                }
                await insert_cloudwatch_logs(data=completion_data, user_id=user_id)
                logger.info(f"Logged 'Report generation completed' for report_id: {report_id}")
            except Exception as e:
                logger.error(f"Error logging completion: {e} for report_id: {report_id}")

        asyncio.create_task(log_completion())

        # Set status to OUTPUT_GENERATED when report generation completes successfully
        async with async_session_scope() as session:
            await update_report_status_by_chat_or_report_id(
                report_id=report_id, status=ReportStatus.OUTPUT_GENERATED.value, session=session
            )
            logger.info(f"Set report status to OUTPUT_GENERATED for report_id: {report_id}")

        # Get the final version ID if not already set
        final_version_id = target_version_id
        if not final_version_id and use_specific_version and target_version_for_generation:
            final_version_id = target_version_for_generation.id
        elif not final_version_id:
            # Need to get the version ID from the database
            async with async_session_scope() as session:
                version_stmt = select(ReportVersion).where(
                    ReportVersion.report_id == report_id, ReportVersion.version == current_version
                )
                version_result = await session.execute(version_stmt)
                version_obj = version_result.scalar_one_or_none()
                if version_obj:
                    final_version_id = version_obj.id

        msg = f"Report {output_type.upper()} has been generated successfully."
        if output_type == "pdf":
            msg += " Please check your email for the report."
        return GenerateReportResponse(
            success=True,
            message=msg,
            report_id=report_id,
            report_version_id=final_version_id,
            version=current_version,
            output_type=output_type,
            s3_uri=_clean_s3_uri(s3_paths, for_output_type=output_type),
            modified=True,
            already_exists=False,
        )

    except Exception as e:
        logger.error(f"Error in generate report endpoint: {e}")
        # Update report status to ERROR_GENERATION_REPORT on error
        try:
            async with async_session_scope() as session:
                await update_report_status_by_chat_or_report_id(
                    report_id=report_id,
                    status=ReportStatus.ERROR_GENERATION_REPORT.value,
                    session=session,
                )
                logger.info(
                    f"Set report status to ERROR_GENERATION_REPORT for report_id: {report_id}"
                )
        except Exception as update_error:
            logger.error(
                f"Failed to update report status to ERROR_GENERATION_REPORT: {update_error}"
            )

        return JSONResponse(
            status_code=500,
            content={
                "success": False,
                "error": "Something unexpected happened. Please try again later.",
            },
        )


@router.get(
    "/report",
    response_model=ReportPresignedUrlResponse,
    status_code=200,
    responses={
        404: {"model": ErrorResponse},
        401: {"model": ErrorResponse},
        422: {"model": ErrorResponse},
        500: {"model": ErrorResponse},
    },
)
async def get_report_presigned_url(
    id: str, file_type: str, user_id: str = Depends(get_current_active_user)
):
    """Get report presigned url endpoint"""
    logger.info(
        f"Get report presigned url request received for report_id: {id}, file_type: {file_type}, user_id: {user_id}"
    )
    try:
        async with async_session_scope() as session:
            # Check if user exists and credentials are valid
            db_response = await check_user_by_id(user_id=user_id, session=session)
            if not db_response.get("success", False):
                logger.error(
                    f"Failed to check user by id: {db_response.get('error')} for user_id: {user_id}, report_id: {id}"
                )
                return JSONResponse(
                    status_code=500,
                    content={
                        "success": False,
                        "error": "Something went wrong on our end while preparing your download. Please try that again.",
                    },
                )
            if not db_response.get("exists", False):
                logger.error(f"User with ID {user_id} not found or token is invalid")
                return JSONResponse(
                    status_code=401, content={"success": False, "error": "Unauthorized request"}
                )

            # Verify report exists and user owns it
            ownership = await verify_report_ownership(
                report_id=id, user_id=user_id, session=session
            )
            if not ownership["authorized"]:
                return JSONResponse(
                    status_code=ownership["status_code"],
                    content={"success": False, "error": ownership["error"]},
                )

            db_response = await get_file_s3_path(id=id, file_type=file_type, session=session)
            if not db_response.get("success", False):
                logger.error(
                    f"Database error: Failed to get file s3 path: {db_response.get('error')} for user_id: {user_id}, report_id: {id}"
                )
                return JSONResponse(
                    status_code=500,
                    content={
                        "success": False,
                        "error": "Something went wrong on our end while preparing your download. Please try that again.",
                    },
                )

            s3_uri = db_response.get("s3_uri", None)
            if not s3_uri:
                logger.error(
                    f"No s3 uri found for report_id: {id} with file_type: {file_type}, user_id: {user_id}"
                )
                return JSONResponse(
                    status_code=404,
                    content={
                        "success": False,
                        "error": "I'm having trouble locating the report file. We're looking into it. Please try again shortly.",
                    },
                )

            if s3_uri and S3_REPORTS_BASE_PATH in s3_uri:
                s3_key = s3_uri[s3_uri.find(S3_REPORTS_BASE_PATH) :]
            elif (
                s3_uri and "casper_reports" in s3_uri
            ):  # This is been done for testing since sometime we do testing after putting prod records in the dev DB.
                s3_key = s3_uri[s3_uri.find("casper_reports") :]
            else:
                logger.error(
                    f"Invalid s3 path: {s3_uri} of report_id: {id} with file_type: {file_type}, user_id: {user_id}"
                )
                return JSONResponse(
                    status_code=404,
                    content={
                        "success": False,
                        "error": "I'm having trouble locating the report file. We're looking into it. Please try again shortly.",
                    },
                )
            s3_instance = get_s3_instance()
            presigned_url = await run_in_threadpool(
                s3_instance.get_download_presigned_url,
                s3_key=s3_key,
                timeout=3600,  # 1 hour — reduced from 7 days for security
            )

            logger.info(
                f"Successfully generated presigned url of report_id: {id} with file_type: {file_type} for user_id: {user_id}"
            )

            return JSONResponse(
                status_code=200, content={"success": True, "presigned_url": presigned_url}
            )

    except RequestValidationError as e:
        # Validation errors (like missing fields) return 422
        logger.error(
            f"Validation error in report presigned url endpoint: {e} for user_id: {user_id}, report_id: {id}"
        )
        return JSONResponse(
            status_code=422,
            content={
                "success": False,
                "error": "Your request couldn't be understood. Please check your input and try again.",
            },
        )
    except HTTPException:
        # Re-raise HTTP exceptions to let the global handler handle them
        raise
    except Exception as e:
        # All other errors return 500
        logger.error(
            f"Error in report presigned url endpoint: {e} for user_id: {user_id}, report_id: {id}"
        )
        return JSONResponse(
            status_code=500,
            content={
                "success": False,
                "error": "Something went wrong on our end while preparing your download. Please try that again.",
            },
        )


@router.post(
    "/presigned-urls",
    response_model=BatchPresignedUrlResponse,
    status_code=200,
    responses={
        401: {"model": ErrorResponse},
        422: {"model": ErrorResponse},
        429: {"model": ErrorResponse},
        500: {"model": ErrorResponse},
    },
)
async def batch_presigned_urls(
    data: BatchPresignedUrlRequest, user_id: str = Depends(get_current_active_user)
):
    """Generate presigned URLs for a batch of S3 visualization URIs.

    Intended for refreshing expired visualization URLs on long-lived pages.
    Rate-limited to 10 requests per minute per user, max 20 URIs per call.
    """
    logger.info(f"Batch presigned URL request from user_id: {user_id}, count: {len(data.s3_uris)}")
    try:
        if len(data.s3_uris) > 20:
            return JSONResponse(
                status_code=422, content={"success": False, "error": "Maximum 20 URIs per request."}
            )

        rate_key = f"rate_limit:presigned_urls:{user_id}"
        count = await redis_instance.redis_client.incr(rate_key)
        if count == 1:
            await redis_instance.redis_client.expire(rate_key, 60)
        if count > 10:
            logger.warning(f"Rate limit exceeded for presigned URLs by user_id: {user_id}")
            return JSONResponse(
                status_code=429,
                content={"success": False, "error": "Too many requests. Please try again shortly."},
            )

        async with async_session_scope() as session:
            db_response = await check_user_by_id(user_id=user_id, session=session)
            if not db_response.get("success", False) or not db_response.get("exists", False):
                return JSONResponse(
                    status_code=401, content={"success": False, "error": "Unauthorized request"}
                )

        s3_instance = get_s3_instance()
        results: dict[str, str | None] = {}
        for s3_uri in data.s3_uris:
            if not s3_uri or not s3_uri.startswith("s3://"):
                results[s3_uri] = None
                continue
            s3_key = extract_s3_key(s3_uri)
            if not s3_key or S3_REPORTS_BASE_PATH not in s3_key:
                results[s3_uri] = None
                continue
            url = await get_or_create_presigned_url(
                s3_uri, redis_instance.redis_client, s3_instance
            )
            results[s3_uri] = url

        return BatchPresignedUrlResponse(success=True, urls=results)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in batch presigned URLs endpoint: {e}", exc_info=True)
        return JSONResponse(
            status_code=500,
            content={"success": False, "error": "Something went wrong. Please try again."},
        )
