"""reports routes: versions.

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
import re

from fastapi import APIRouter, Depends, HTTPException
from fastapi.concurrency import run_in_threadpool
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.adapters.s3 import (
    get_s3_instance,
    replace_visualization_uris_in_reports,
)
from app.auth.repository import (
    check_user_by_id,
)
from app.auth.schemas import ErrorResponse
from app.auth.token import get_current_active_user
from app.core.constants import (
    S3_REPORTS_BASE_PATH,
)
from app.core.db import async_session_scope
from app.core.logging import setup_logging
from app.core.redis import get_redis_instance
from app.reports.repository import (
    get_all_version_outputs_list,
    get_report_info_by_version,
    get_report_version_history_data,
    get_version_file_for_download,
    verify_report_ownership,
)
from app.reports.schemas import (
    ListVersionOutputsResponse,
    ReportInfoResponse,
    ReportVersionHistoryResponse,
    VersionDownloadResponse,
)

router = APIRouter()
logger = setup_logging(__file__)
redis_instance = get_redis_instance()


@router.get(
    "/list-version-outputs",
    response_model=ListVersionOutputsResponse,
    status_code=200,
    responses={
        404: {"model": ErrorResponse},
        401: {"model": ErrorResponse},
        422: {"model": ErrorResponse},
        500: {"model": ErrorResponse},
    },
)
async def list_version_outputs(report_id: str, user_id: str = Depends(get_current_active_user)):
    """
    List all available output files (non-null) across all versions of a report.
    Returns a simple list for frontend to display all available downloads.

    Args:
        report_id: The report ID
        user_id: Authenticated user ID (from token)

    Returns:
        List of all available outputs with version, file_type, and generated_at
    """
    logger.info(f"List outputs request for report_id: {report_id}, user_id: {user_id}")

    try:
        async with async_session_scope() as session:
            # Verify the report exists and belongs to the user
            ownership = await verify_report_ownership(
                report_id=report_id, user_id=user_id, session=session
            )
            if not ownership["authorized"]:
                return JSONResponse(
                    status_code=ownership["status_code"],
                    content={"success": False, "error": ownership["error"]},
                )

            # Fetch all version outputs
            result = await get_all_version_outputs_list(report_id=report_id, session=session)

            if not result.get("success"):
                logger.error(f"Error fetching version outputs: {result.get('error')}")
                return JSONResponse(
                    status_code=500,
                    content={
                        "success": False,
                        "error": "Unable to fetch report outputs. Please try again later.",
                    },
                )

            outputs = result.get("outputs", [])
            logger.info(f"Successfully fetched {len(outputs)} outputs for report {report_id}")

            return ListVersionOutputsResponse(success=True, report_id=report_id, outputs=outputs)

    except RequestValidationError as e:
        logger.error(f"Validation error in list-version-outputs endpoint: {e}")
        return JSONResponse(
            status_code=422,
            content={
                "success": False,
                "error": "Invalid request format. Please check your input and try again.",
            },
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Unexpected error in list-version-outputs endpoint: {e!s}", exc_info=True)
        return JSONResponse(
            status_code=500,
            content={
                "success": False,
                "error": "Something went wrong while fetching outputs. Please try again.",
            },
        )


@router.get(
    "/report-version-history/{report_id}",
    response_model=ReportVersionHistoryResponse,
    status_code=200,
    responses={
        404: {"model": ErrorResponse},
        401: {"model": ErrorResponse},
        500: {"model": ErrorResponse},
    },
)
async def get_report_version_history(
    report_id: str, user_id: str = Depends(get_current_active_user)
):
    """
    Get version history for a report.
    Returns all versions with their generated outputs status.
    Used by frontend to populate the version history dropdown.
    """
    logger.info(f"Get version history request for report_id: {report_id}, user_id: {user_id}")

    try:
        # Verify user has access to this report
        async with async_session_scope() as session:
            db_response = await check_user_by_id(user_id=user_id, session=session)
            if not db_response.get("success", False):
                return JSONResponse(
                    status_code=500,
                    content={
                        "success": False,
                        "error": "Something unexpected happened. Please try again later.",
                    },
                )
            if not db_response.get("exists", False):
                return JSONResponse(
                    status_code=401, content={"success": False, "error": "Unauthorized request"}
                )

        # Get version history (with ownership check)
        async with async_session_scope() as session:
            ownership = await verify_report_ownership(
                report_id=report_id, user_id=user_id, session=session
            )
            if not ownership["authorized"]:
                return JSONResponse(
                    status_code=ownership["status_code"],
                    content={"success": False, "error": ownership["error"]},
                )

            result = await get_report_version_history_data(report_id=report_id, session=session)

            if not result.get("success"):
                error_msg = result.get("error", "Failed to get version history")
                return JSONResponse(
                    status_code=404 if "not found" in error_msg.lower() else 500,
                    content={"success": False, "error": error_msg},
                )

            return ReportVersionHistoryResponse(
                success=True,
                report_id=report_id,
                s3_base_path=result.get("s3_base_path", ""),
                versions=result.get("versions", []),
            )

    except Exception as e:
        logger.error(f"Error getting version history: {e!s}", exc_info=True)
        return JSONResponse(
            status_code=500,
            content={
                "success": False,
                "error": "Something unexpected happened. Please try again later.",
            },
        )


@router.get(
    "/report-info",
    response_model=ReportInfoResponse,
    status_code=200,
    responses={
        404: {"model": ErrorResponse},
        401: {"model": ErrorResponse},
        500: {"model": ErrorResponse},
    },
)
async def get_report_info(
    report_id: str, report_version_id: str, user_id: str = Depends(get_current_active_user)
):
    """
    Get report info for a specific version, including cards.
    Returns data in the same format as reports in previous chat messages endpoint.

    Args:
        report_id: The ID of the report
        report_version_id: The ID of the specific report version
        user_id: Authenticated user ID (from token)

    Returns:
        Report data with cards for the specified version
    """
    logger.info(
        f"Get report info request for report_id: {report_id}, version_id: {report_version_id}, user_id: {user_id}"
    )

    try:
        async with async_session_scope() as session:
            db_response = await check_user_by_id(user_id=user_id, session=session)
            if not db_response.get("success", False):
                return JSONResponse(
                    status_code=500,
                    content={
                        "success": False,
                        "error": "Something unexpected happened. Please try again later.",
                    },
                )
            if not db_response.get("exists", False):
                return JSONResponse(
                    status_code=401, content={"success": False, "error": "Unauthorized request"}
                )

            result = await get_report_info_by_version(
                report_id=report_id, report_version_id=report_version_id, session=session
            )

            if not result.get("success"):
                error_msg = result.get("error", "Failed to get report info")
                return JSONResponse(
                    status_code=404 if "not found" in error_msg.lower() else 500,
                    content={"success": False, "error": error_msg},
                )

            version_reports = result.get("reports", [])
            await replace_visualization_uris_in_reports(
                version_reports, redis_instance.redis_client
            )

            return ReportInfoResponse(success=True, reports=version_reports)

    except Exception as e:
        logger.error(f"Error getting report info: {e!s}", exc_info=True)
        return JSONResponse(
            status_code=500,
            content={
                "success": False,
                "error": "Something unexpected happened. Please try again later.",
            },
        )


@router.get(
    "/download-version",
    response_model=VersionDownloadResponse,
    status_code=200,
    responses={
        404: {"model": ErrorResponse},
        401: {"model": ErrorResponse},
        422: {"model": ErrorResponse},
        500: {"model": ErrorResponse},
    },
)
async def download_version_file(
    report_id: str, version: int, file_type: str, user_id: str = Depends(get_current_active_user)
):
    """
    Download a specific file type from a specific version of a report.
    Returns a presigned URL for direct download.

    Args:
        report_id: The ID of the report
        version: The version number to download
        file_type: The type of file to download (pdf, html, md, pptx, info_pdf)
        user_id: Authenticated user ID (from token)

    Returns:
        Presigned URL for downloading the file (valid for 1 hour)
    """
    logger.info(
        f"Download request for report_id: {report_id}, version: {version}, file_type: {file_type}, user_id: {user_id}"
    )

    try:
        async with async_session_scope() as session:
            # Verify the report exists and belongs to the user
            ownership = await verify_report_ownership(
                report_id=report_id, user_id=user_id, session=session
            )
            if not ownership["authorized"]:
                return JSONResponse(
                    status_code=ownership["status_code"],
                    content={"success": False, "error": ownership["error"]},
                )
            report_data = ownership["report"]

            # Get the file path for the specified version and file type
            file_result = await get_version_file_for_download(
                report_id=report_id, version=version, file_type=file_type, session=session
            )

            if not file_result.get("success"):
                error_msg = file_result.get("error", "File not found")
                logger.warning(f"File not found: {error_msg}")

                # Determine appropriate status code based on error
                if "not found" in error_msg.lower():
                    status_code = 404
                elif "Invalid file type" in error_msg:
                    status_code = 422
                else:
                    status_code = 500

                return JSONResponse(
                    status_code=status_code, content={"success": False, "error": error_msg}
                )

            # Generate presigned URL asynchronously
            s3_path = file_result.get("s3_path")

            # Extract S3 key from full S3 path (same logic as get_report_presigned_url)
            if s3_path and S3_REPORTS_BASE_PATH in s3_path:
                s3_key = s3_path[s3_path.find(S3_REPORTS_BASE_PATH) :]
            elif s3_path and "casper_reports" in s3_path:  # For testing with prod records in dev DB
                s3_key = s3_path[s3_path.find("casper_reports") :]
            else:
                logger.error(
                    f"Invalid s3 path: {s3_path} for report_id: {report_id}, version: {version}, file_type: {file_type}"
                )
                return JSONResponse(
                    status_code=404,
                    content={
                        "success": False,
                        "error": "I'm having trouble locating the file. We're looking into it. Please try again shortly.",
                    },
                )

            # Use global S3 instance and run in threadpool for async operation
            s3_instance = get_s3_instance()
            # presigned_url = await run_in_threadpool(
            #     s3_instance.get_download_presigned_url,
            #     s3_key=s3_key,
            #     timeout=604800
            # )
            # Generate custom filename with version number
            report_title = report_data.get("title", "report")
            # Clean up the title for filename (keep spaces, remove problematic characters)
            safe_title = report_title.strip(".").strip().replace(":", " ").replace("_", " ")
            # Collapse multiple spaces into one
            safe_title = re.sub(r"\s+", " ", safe_title)

            # Construct filename based on file type
            if file_type == "info_pdf":
                download_filename = f"{safe_title} v{version} visual brief.pdf"
            elif file_type == "pdf":
                download_filename = f"{safe_title} v{version}.pdf"
            elif file_type == "html":
                download_filename = f"{safe_title} v{version}.html"
            elif file_type == "md":
                download_filename = f"{safe_title} v{version}.md"
            elif file_type == "pptx":
                download_filename = f"{safe_title} v{version}.pptx"
            else:
                download_filename = f"{safe_title} v{version}.{file_type}"

            # Generate presigned URL with custom filename
            presigned_url = await run_in_threadpool(
                s3_instance.get_download_presigned_url_with_filename,
                s3_key=s3_key,
                filename=download_filename,
                timeout=3600,  # 1 hour — reduced from 7 days for security
            )

            if not presigned_url:
                logger.error(f"Failed to generate presigned URL for s3_key: {s3_key}")
                return JSONResponse(
                    status_code=500,
                    content={
                        "success": False,
                        "error": "Failed to generate download link. Please try again.",
                    },
                )

            logger.info(f"Successfully generated presigned URL for {file_type} version {version}")
            return VersionDownloadResponse(
                success=True,
                presigned_url=presigned_url,
                report_id=report_id,
                version=version,
                file_type=file_type,
                expires_in=3600,
            )

    except RequestValidationError as e:
        logger.error(f"Validation error in download-version endpoint: {e}")
        return JSONResponse(
            status_code=422,
            content={
                "success": False,
                "error": "Invalid request format. Please check your input and try again.",
            },
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Unexpected error in download-version endpoint: {e!s}", exc_info=True)
        return JSONResponse(
            status_code=500,
            content={
                "success": False,
                "error": "Something went wrong while preparing your download. Please try again.",
            },
        )
