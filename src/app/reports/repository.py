"""Database access for the reports bounded context.

Moved verbatim from ``src/db/async_db_functions.py`` during the R-STRUCT-1
migration. Function bodies are unchanged; only the import block was retargeted
at the new module paths.
"""

from datetime import datetime, timezone
from typing import Any, Dict, Optional

from sqlalchemy import func, select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from uuid_utils import uuid7

from app.core.enums import ReportStatus
from app.core.logging import setup_logging
from app.core.utils import prune_report_layout_async
from app.models import (
    Card, Message, Publish,
    RefinementHistory, Report, ReportVersion, ReportVersionCard,
)

from app.reports.constants import (
    _NON_DRAFT_STATUSES,
    DEFAULT_DOMAIN_INTERNAL,
    STANDARD_CATEGORY_SLUG,
)

logger = setup_logging(__file__)


async def get_chat_reports(chat_id: int, session: AsyncSession) -> Dict[str, Any]:
    """
    Get all report files for a specific chat_id.
    
    Args:
        chat_id (int): The chat_id to get reports for
        session (AsyncSession): The SQLAlchemy async session
            
    Returns:
        Dict[str, Any]: Dictionary with success status and user details if found
    """
    logger.info(f"Getting report files for chat_id: {chat_id}")

    if not chat_id:
        logger.error("Invalid chat_id: empty value provided")
        return {
            "success": False,
            "error": "Invalid chat ID: empty value provided"
        }
    
    report_list = []

    # redis_instance = get_redis_instance()
    # logger.info(f"Checking redis for chat_id: {chat_id}")
    # redis_response = await redis_instance.get_chat_report(chat_id, ttl=3600)
    # if redis_response.get('success') and redis_response.get('reports'):
    #     logger.info(f"Reports retrieved from redis for chat_id: {chat_id}")
    #     for report in redis_response.get('reports'):
    #         report_list.append({
    #             "id": report.get('report_id'),
    #             "source_documents": report.get('source_documents'),
    #             "s3_uri": report.get('s3_uri'),
    #             "created_at": report.get('created_at').isoformat() if report.get('created_at') else None
    #         })
    #     return {
    #         "success": True,
    #         "reports": report_list
    #     }

    try:
        # Query to get all report files for the chat
        stmt = select(Report).where(Report.chat_id == chat_id)  
        result = await session.execute(stmt)
        reports = result.scalars().all()
        
        if not reports:
            logger.warning(f"No report files found for chat_id: {chat_id}")
            return {
                "success": True,
                "reports": []
            }
        
        for report in reports:
            report_list.append({
                "id": report.id,
                "citations": report.citations,
                "s3_uri": report.s3_uri,
                "created_at": report.created_at.isoformat() if report.created_at else None
            })

        response = {
            "success": True,    
            "reports": report_list
        }

        logger.info(f"Successfully retrieved {len(report_list)} reports for chat_id: {chat_id}")
        return response
    
    except SQLAlchemyError as e:
        logger.error(f"Database error retrieving report files: {str(e)}", exc_info=True)
        return {
            "success": False,
            "error": f"Database error: {str(e)}"
        }

    except Exception as e:
        logger.error(f"Unexpected error retrieving report files: {str(e)}", exc_info=True)
        return {
            "success": False,
            "error": f"Unexpected error: {str(e)}"
        }   
async def get_file_s3_path(id: int, file_type: str, session: AsyncSession) -> Dict[str, Any]:
    """
    Get the S3 path for a specific file based on its ID and file type.
    
    Args:
        id (int): The ID of the file
        file_type (str): The type of the file (e.g., 'report', 'chat_message')
        session (AsyncSession): The SQLAlchemy async session
            
    Returns:
        Dict[str, Any]: Dictionary with success status and file details if found
    """
    logger.info(f"Getting S3 path for file with ID: {id} and file_type: {file_type}")

    if not id:
        logger.error("Invalid file ID: empty value provided")
        return {
            "success": False,
            "error": "Invalid file ID: empty value provided"
        }
    
    if not file_type:
        logger.error("Invalid file type: empty value provided")
        return {
            "success": False,
            "error": "Invalid file type: empty value provided"
        }
    
    try:
        # Query to get the S3 path for the file
        stmt = select(Report.s3_uri).where(Report.id == id)
        result = await session.execute(stmt)
        s3_paths = result.scalar_one_or_none()
        
        if not s3_paths:
            logger.error(f"Report with ID {id} not found")
            return {
                "success": False,
                "error": f"Report with ID {id} not found"
            }
        
        s3_path = s3_paths.get(file_type)
        logger.info(f"S3 path for file with ID {id} and file_type {file_type}: {s3_path}")
        return {
            "success": True,
            "s3_uri": s3_path
        }
        
    except SQLAlchemyError as e:
        logger.error(f"Database error retrieving S3 path: {str(e)}", exc_info=True)
        return {
            "success": False,
            "error": f"Database error: {str(e)}"
        }
    
    except Exception as e:
        logger.error(f"Unexpected error retrieving S3 path: {str(e)}", exc_info=True)
        return {
            "success": False,
            "error": f"Unexpected error: {str(e)}"
        }
async def create_report(
    report_id: str,
    chat_id: str,
    created_at: datetime,
    s3_uri: dict,
    session: AsyncSession
) -> Dict[str, Any]:
    """
    Create a new report with minimal required fields and initial version.

    Args:
        report_id (str): Unique identifier for the report.
        chat_id (str): Chat identifier.
        created_at (datetime): Creation timestamp.
        s3_uri (Dict, optional): S3 URIs to the report files in JSON format. Defaults to {}.
        session (AsyncSession, optional): SQLAlchemy async session.

    Returns:
        Dict[str, Any]: Dictionary with success status, report_id, and version_id.
    """
    logger.info("Creating new report with ID: %s", report_id)

    # --- Validation ---
    missing_fields = [
        field for field, value in {
            "report_id": report_id,
            "chat_id": chat_id,
            "created_at": created_at
        }.items() if not value
    ]
    if missing_fields:
        error_msg = f"Missing required fields: {', '.join(missing_fields)}"
        logger.error(error_msg)
        return {"success": False, "error": error_msg}


    try:
        # Create base report
        new_report = Report(
            id=report_id,
            chat_id=chat_id,
            created_at=created_at,
            current_version=1,
            status=ReportStatus.DRAFT.value,
            last_activity_at=created_at,
            s3_uri={"md": None, "pdf": None, "html": None, "pptx": None, "info_pdf": None, "md_explicit": False, "html_explicit": False}
        )
        session.add(new_report)
        await session.flush()  # Flush to get the report_id
        
        # Create initial version (version 1)
        initial_version = ReportVersion(
            id=str(uuid7()),
            report_id=report_id,
            version=1,
            s3_uri={**(s3_uri or {}), "md_explicit": False, "html_explicit": False},
            is_active=True,
            status=ReportStatus.DRAFT.value,
            last_activity_at=created_at,
            created_at=created_at
        )
        session.add(initial_version)
        
        # Create refinement history entry with null refine_history
        refinement_history = RefinementHistory(
            id=str(uuid7()),
            report_id=report_id,
            refine_history=None
        )
        session.add(refinement_history)
        
        await session.commit()

        logger.info("Successfully created report with ID: %s, initial version, and refinement history", report_id)
        return {
            "success": True, 
            "report_id": report_id,
            "version_id": initial_version.id,
            "version": 1
        }

    except SQLAlchemyError as e:
        await session.rollback()
        error_msg = f"Database error: {str(e)}"
        logger.error(error_msg, exc_info=True)
        return {"success": False, "error": error_msg}

    except Exception as e:
        await session.rollback()
        error_msg = f"Unexpected error: {str(e)}"
        logger.error(error_msg, exc_info=True)
        return {"success": False, "error": error_msg}
async def update_report(report_id: str, update_data: Dict[str, Any], session: AsyncSession) -> Dict[str, Any]:
    """
    Update an existing report with the provided fields.
    
    Args:
        report_id (str): ID of the report to update
        update_data (Dict[str, Any]): Dictionary containing fields to update
        session (AsyncSession): SQLAlchemy async session
            
    Returns:
        Dict[str, Any]: Dictionary with success status
    """
    # logger.info(f"Updating report with ID: {report_id}")
    
    # if not report_id:
    #     logger.error("Missing required field: report_id")
    #     return {
    #         "success": False,
    #         "error": "Missing required field: report_id"
    #     }
    
    # if not update_data:
    #     logger.warning(f"No update data provided for report: {report_id}")
    #     return {
    #         "success": True,
    #         "message": "No changes to apply"
    #     }
    
    # try:
    #     # First check if report exists
    #     stmt = select(Report).where(Report.id == report_id)
    #     result = await session.execute(stmt)
    #     report = result.scalar_one_or_none()
        
    #     if not report:
    #         logger.error(f"Report with ID {report_id} not found")
    #         return {
    #             "success": False,
    #             "error": f"Report with ID {report_id} not found"
    #         }
        
    #     # Update only the fields provided in update_data
    #     for field, value in update_data.items():
    #         if hasattr(report, field):
    #             setattr(report, field, value)
    #         else:
    #             logger.warning(f"Ignoring unknown field: {field}")
        
    #     await session.commit()
        
    #     logger.info(f"Successfully updated report with ID: {report_id}")
    #     return {
    #         "success": True,
    #         "report_id": report_id
    #     }

    logger.info(f"Updating report with ID: {report_id}")
    
    if not report_id:
        logger.error("Missing required field: report_id")
        return {
            "success": False,
            "error": "Missing required field: report_id"
        }
    
    if not update_data:
        logger.warning(f"No update data provided for report: {report_id}")
        return {
            "success": True,
            "message": "No changes to apply"
        }
    
    try:
        # First check if report exists
        stmt = select(Report).where(Report.id == report_id)
        result = await session.execute(stmt)
        report = result.scalar_one_or_none()
        
        if not report:
            logger.error(f"Report with ID {report_id} not found")
            return {
                "success": False,
                "error": f"Report with ID {report_id} not found"
            }
        
        # Get active version
        active_version_stmt = select(ReportVersion).where(
            ReportVersion.report_id == report_id,
            ReportVersion.is_active == True
        )
        active_version_result = await session.execute(active_version_stmt)
        active_version = active_version_result.scalar_one_or_none()
        
        if not active_version:
            logger.error(f"No active version found for report {report_id}")
            return {
                "success": False,
                "error": f"No active version found for report {report_id}"
            }
        
        # Handle s3_uri - update in active version, not report
        if "s3_uri" in update_data:
            incoming = update_data.pop("s3_uri") or {}
            existing = active_version.s3_uri or {}

            # Always apply incoming values
            merged = {**existing, **incoming}

            # Ensure all required keys exist
            for required_key in ("md", "html", "pdf", "pptx", "info_pdf"):
                if required_key not in merged:
                    merged[required_key] = None

            active_version.s3_uri = dict(merged)
            report.s3_uri = dict(merged)
        
        # Handle status update - update both report and active version
        if "status" in update_data:
            status = update_data.pop("status")
            report.status = status
            active_version.status = status
        
        # Handle generated_at - update in active version
        if "generated_at" in update_data:
            generated_at = update_data.pop("generated_at")
            active_version.generated_at = generated_at
            report.generated_at = generated_at
        
        # Handle poster_image_url - update in active version
        if "poster_image_url" in update_data:
            poster_url = update_data.pop("poster_image_url")
            active_version.poster_image_url = poster_url
            report.poster_image_url = poster_url
        
        # Update last_activity_at in both
        if "last_activity_at" in update_data or "status" in locals():
            timestamp = update_data.pop("last_activity_at", datetime.now(timezone.utc))
            report.last_activity_at = timestamp
            active_version.last_activity_at = timestamp
        
        # Update other Report-level fields (title, layout, etc.)
        report_fields = ['title', 'layout', 'length', 'summary', 'citations', 'current_version', 'file_id', 'initial_markdown', 'domain_name', 'report_type']
        title_updated = False
        new_title = None
        
        for field, value in update_data.items():
            if field in report_fields and hasattr(report, field):
                setattr(report, field, value)
                # Track if title was updated
                if field == 'title':
                    title_updated = True
                    new_title = value
            else:
                logger.warning(f"Ignoring unknown field: {field}")
        
        # If title was updated, also update the chat title
        if title_updated and new_title:
            try:
                chat_stmt = select(Message).where(Message.id == report.chat_id)
                chat_result = await session.execute(chat_stmt)
                chat = chat_result.scalar_one_or_none()
                
                if chat:
                    chat.chat_title = new_title
                    chat.updated_at = datetime.now(timezone.utc)
                    logger.info(f"Updated chat title to '{new_title}' for chat_id: {report.chat_id}")
                else:
                    logger.warning(f"Chat not found with id: {report.chat_id}, could not update chat title")
            except Exception as e:
                logger.error(f"Error updating chat title: {e}", exc_info=True)
                # Don't fail the entire update if chat title update fails
        
        await session.commit()
        
        logger.info(f"Successfully updated report with ID: {report_id}")
        return {
            "success": True,
            "report_id": report_id
        }
        
    except SQLAlchemyError as e:
        logger.error(f"Database error updating report: {str(e)}", exc_info=True)
        await session.rollback()
        return {
            "success": False,
            "error": f"Database error: {str(e)}"
        }
    except Exception as e:
        logger.error(f"Unexpected error updating report: {str(e)}", exc_info=True)
        await session.rollback()
        return {
            "success": False,
            "error": f"Unexpected error: {str(e)}"
        }
async def update_specific_version_s3_uri(
    report_id: str,
    version_id: str,
    s3_uri_updates: Dict[str, Any],
    generated_at: datetime = None,
    session: AsyncSession = None
) -> Dict[str, Any]:
    """
    Update s3_uri for a SPECIFIC version (not necessarily the active one).
    Use this when generating outputs for old versions to avoid updating the Report table
    which should always reflect the current version's data.
    
    Args:
        report_id: Report ID for validation
        version_id: Specific version ID to update
        s3_uri_updates: Dict with s3_uri fields to update (e.g., {"pdf": "s3://...", "md": "s3://..."})
        generated_at: Optional timestamp for when PDF/HTML/MD was generated
        session: Database session
    
    Returns:
        Dict with success status and updated s3_uri
    """
    try:
        # Get the specific version
        stmt = select(ReportVersion).where(
            ReportVersion.id == version_id,
            ReportVersion.report_id == report_id
        )
        result = await session.execute(stmt)
        version = result.scalar_one_or_none()
        
        if not version:
            logger.error(f"Version {version_id} not found for report {report_id}")
            return {"success": False, "error": f"Version {version_id} not found for report {report_id}"}
        
        # Merge with existing s3_uri
        existing = version.s3_uri or {}
        merged = {**existing, **s3_uri_updates}
        
        # Ensure all required keys exist
        for required_key in ("md", "html", "pdf", "pptx", "info_pdf"):
            if required_key not in merged:
                merged[required_key] = None
        
        version.s3_uri = dict(merged)
        version.last_activity_at = datetime.now(timezone.utc)
        
        # Update generated_at if provided (for PDF/HTML/MD generation)
        if generated_at:
            version.generated_at = generated_at
        
        await session.commit()
        
        logger.info(f"Updated s3_uri for specific version {version_id} (v{version.version}) - NOT updating Report table")
        return {"success": True, "s3_uri": merged, "version": version.version}
        
    except SQLAlchemyError as e:
        logger.error(f"Database error updating specific version s3_uri: {str(e)}", exc_info=True)
        await session.rollback()
        return {"success": False, "error": f"Database error: {str(e)}"}
    except Exception as e:
        logger.error(f"Unexpected error updating specific version s3_uri: {str(e)}", exc_info=True)
        await session.rollback()
        return {"success": False, "error": str(e)}
async def update_report_status_by_chat_or_report_id(
    chat_id: str = None,
    report_id: str = None,
    status: str = None,
    session: AsyncSession = None
) -> Dict[str, Any]:
    """
    Update report status using either chat_id or report_id.
    
    Args:
        chat_id: Chat ID to find the report
        report_id: Report ID (if known)
        status: New status value
        session: Database session
        
    Returns:
        Dict with success status
    """
    if not status:
        return {"success": False, "error": "Status is required"}
    
    if not chat_id and not report_id:
        return {"success": False, "error": "Either chat_id or report_id is required"}
    
    try:
        # If report_id is provided, use it directly
        if report_id:
            target_report_id = report_id
        else:
            # Find report by chat_id
            stmt = select(Report).where(Report.chat_id == chat_id)
            result = await session.execute(stmt)
            report = result.scalar_one_or_none()
            
            if not report:
                logger.warning(f"No report found for chat_id: {chat_id}")
                return {"success": False, "error": f"No report found for chat_id: {chat_id}"}
            
            target_report_id = report.id
        
        # Update the report status
        update_data = {
            "status": status,
            "last_activity_at": datetime.now(timezone.utc)
        }
        
        return await update_report(report_id=target_report_id, update_data=update_data, session=session)
        
    except Exception as e:
        logger.error(f"Error updating report status: {str(e)}", exc_info=True)
        return {"success": False, "error": str(e)}
async def update_report_status_if_needed(
    report_id: str,
    new_status: str,
    session: AsyncSession
) -> Dict[str, Any]:
    """
    Update report status only if it's different from the current status.
    This optimization avoids unnecessary database writes.
    
    Args:
        report_id: Report ID
        new_status: New status value
        session: Database session
        
    Returns:
        Dict with success status and whether update was performed
    """
    try:
        # First, fetch the current status
        stmt = select(Report.status).where(Report.id == report_id)
        result = await session.execute(stmt)
        current_status = result.scalar_one_or_none()
        
        if current_status is None:
            logger.warning(f"Report not found: {report_id}")
            return {"success": False, "error": "Report not found"}
        
        # Always update last_activity_at to reflect user activity,
        # but only update status if it's actually different.
        now = datetime.now(timezone.utc)
        
        if current_status == new_status:
            # Status unchanged — still update last_activity_at to track activity
            logger.debug(f"Status already {new_status} for report {report_id}, updating last_activity_at only")
            update_data = {"last_activity_at": now}
            result = await update_report(report_id=report_id, update_data=update_data, session=session)
            if result.get("success"):
                result["updated"] = False
                result["message"] = "Status already set, last_activity_at updated"
            return result
        
        # Status is different, perform full update
        update_data = {
            "status": new_status,
            "last_activity_at": now
        }
        
        result = await update_report(report_id=report_id, update_data=update_data, session=session)
        if result.get("success"):
            result["updated"] = True
        return result
        
    except Exception as e:
        logger.error(f"Error checking/updating report status: {str(e)}", exc_info=True)
        return {"success": False, "error": str(e)}
async def get_report_details(report_id: str, session: AsyncSession) -> Dict[str, Any]:
    """
    Get detailed information about a specific report.
    
    Args:
        report_id (str): ID of the report to retrieve
        session (AsyncSession): SQLAlchemy async session
            
    Returns:
        Dict[str, Any]: Dictionary with success status and report details
    """
    logger.info(f"Getting details for report with ID: {report_id}")
    
    if not report_id:
        logger.error("Missing required field: report_id")
        return {
            "success": False,
            "error": "Missing required field: report_id"
        }
    
    try:
        # Query to get report details
        stmt = select(Report).where(Report.id == report_id)
        result = await session.execute(stmt)
        report = result.scalar_one_or_none()
        
        if not report:
            logger.warning(f"Report with ID {report_id} not found")
            return {
                "success": True,
                "report": None
            }
        
        # Convert report to dictionary
        report_data = {
            "id": report.id,
            "chat_id": report.chat_id,
            "s3_uri": report.s3_uri,
            "created_at": report.created_at.isoformat() if report.created_at else None,
            "title": report.title,
            "layout": report.layout,
            "length": report.length,
            "summary": report.summary,
            "citations": report.citations,
            "file_id": report.file_id,
            "initial_markdown": report.initial_markdown,
            "report_type": report.report_type,
        }
        
        logger.info(f"Successfully retrieved details for report with ID: {report_id}")
        return {
            "success": True,
            "report": report_data
        }
        
    except SQLAlchemyError as e:
        logger.error(f"Database error retrieving report details: {str(e)}", exc_info=True)
        return {
            "success": False,
            "error": f"Database error: {str(e)}"
        }
    except Exception as e:
        logger.error(f"Unexpected error retrieving report details: {str(e)}", exc_info=True)
        return {
            "success": False,
            "error": f"Unexpected error: {str(e)}"
        }
async def verify_report_ownership(report_id: str, user_id: str, session: AsyncSession) -> Dict[str, Any]:
    """
    Verify that a report exists and the requesting user owns it.

    Fetches report details, then checks ownership by verifying the report's
    chat_id corresponds to a Message owned by the given user_id.

    Args:
        report_id (str): ID of the report to verify.
        user_id (str): ID of the user requesting access.
        session (AsyncSession): SQLAlchemy async session.

    Returns:
        Dict[str, Any]: {
            "authorized": True,  "report": <report_dict>
        } on success, or {
            "authorized": False, "status_code": 404|401|500,
            "error": "<message>"
        } on failure.
    """
    try:
        report_result = await get_report_details(report_id=report_id, session=session)

        if not report_result or not report_result.get("success", False):
            error_msg = report_result.get("error", "Failed to retrieve report details.") if report_result else "Failed to retrieve report details."
            logger.error(f"Failed to get report details for report_id: {report_id}, error: {error_msg}")
            return {"authorized": False, "status_code": 500, "error": "Something unexpected happened. Please try again later."}

        if not report_result.get("report"):
            logger.warning(f"Report not found: {report_id} for user: {user_id}")
            return {"authorized": False, "status_code": 404, "error": "Report not found. Please check the report ID and try again."}

        report_data = report_result["report"]
        chat_id = report_data.get("chat_id")

        if not chat_id:
            logger.error(f"Report {report_id} has no associated chat_id")
            return {"authorized": False, "status_code": 500, "error": "Something unexpected happened. Please try again later."}

        chat_stmt = select(Message).where(
            Message.id == chat_id,
            Message.user_id == user_id,
            Message.is_deleted == False  # noqa: E712  — SQLAlchemy filter expression
        )
        chat_result = await session.execute(chat_stmt)
        chat = chat_result.scalar_one_or_none()

        if not chat:
            logger.warning(f"Unauthorized access attempt to report {report_id} by user {user_id}")
            return {"authorized": False, "status_code": 401, "error": "You don't have permission to access this report."}

        return {"authorized": True, "report": report_data}

    except SQLAlchemyError as e:
        logger.error(f"Database error verifying report ownership for report_id: {report_id}: {str(e)}", exc_info=True)
        return {"authorized": False, "status_code": 500, "error": "Something unexpected happened. Please try again later."}
    except Exception as e:
        logger.error(f"Unexpected error verifying report ownership for report_id: {report_id}: {str(e)}", exc_info=True)
        return {"authorized": False, "status_code": 500, "error": "Something unexpected happened. Please try again later."}
async def link_card_to_report_version(
    session: AsyncSession,
    report_version_id: str,
    card_primary_id: str,
    sequence: int,
    is_modified: bool = False
) -> Dict[str, Any]:
    """
    Link a card to a report version via ReportVersionCard junction table.
    
    Args:
        session (AsyncSession): SQLAlchemy async session
        report_version_id (str): ID of the report version
        card_primary_id (str): Primary key ID of the card (Card.id, not the business card_id)
        sequence (int): Position of card in this version
        is_modified (bool): Whether this card was modified in this version
        
    Returns:
        Dict[str, Any]: Dictionary with success status
    """
    try:
        mapping = ReportVersionCard(
            id=str(uuid7()),
            report_version_id=report_version_id,
            parent_card_id=card_primary_id,
            sequence=sequence,
            is_modified=is_modified,
            created_at=datetime.now(timezone.utc)
        )
        session.add(mapping)
        # Note: Do NOT flush here - let the parent transaction handle it
        # This ensures proper rollback if any subsequent operation fails
        
        logger.info(f"Linked card {card_primary_id} to report version {report_version_id} at sequence {sequence}")
        return {"success": True, "mapping_id": mapping.id}
        
    except Exception as e:
        logger.error(f"Error linking card to report version: {str(e)}", exc_info=True)
        return {"success": False, "error": str(e)}
async def finalize_report_version(
    session: AsyncSession,
    report_id: str,
    version_id: str = None
) -> Dict[str, Any]:
    """
    Finalize a report version by linking all active cards to it.
    Should ONLY be called when report is generated (PDF/HTML/PPTX created) for VERSION 1.
    For version 2+, use /regenerate-report which calls create_new_report_version.
    
    This ensures report_version_cards always reflects the ACTUAL cards used in generation.
    
    Args:
        session (AsyncSession): SQLAlchemy async session
        report_id (str): ID of the report
        version_id (str, optional): ID of the version to finalize. If None, uses active version.
        
    Returns:
        Dict[str, Any]: Dictionary with success status and number of cards linked
    """
    try:
        # Get the version to finalize
        if version_id:
            version_stmt = select(ReportVersion).where(ReportVersion.id == version_id)
        else:
            # Get active version
            version_stmt = select(ReportVersion).where(
                ReportVersion.report_id == report_id,
                ReportVersion.is_active == True
            )
        
        version_result = await session.execute(version_stmt)
        version = version_result.scalar_one_or_none()
        
        if not version:
            return {"success": False, "error": "Version not found"}
        
        # IMPORTANT: Only finalize version 1 here
        # Version 2+ should be finalized by create_new_report_version in /regenerate-report
        if version.version != 1:
            logger.warning(f"Attempted to finalize version {version.version} for report {report_id}. Only version 1 should be finalized via this function.")
            return {
                "success": False,
                "error": f"Version {version.version} should not be finalized here. Use /regenerate-report for version 2+"
            }
        
        # Check if this version is already finalized (has entries in report_version_cards)
        existing_mappings_stmt = select(ReportVersionCard).where(
            ReportVersionCard.report_version_id == version.id
        ).limit(1)
        existing_result = await session.execute(existing_mappings_stmt)
        existing_mapping = existing_result.scalar_one_or_none()
        
        if existing_mapping:
            logger.info(f"Version {version.id} already finalized, skipping")
            return {
                "success": True,
                "message": "Version already finalized",
                "cards_linked": 0,
                "already_finalized": True
            }
        
        # Get all active cards for this report (latest versions only)
        # Since is_active is already set to False for old versions, we just filter by is_active
        cards_stmt = (
            select(Card)
            .where(
                Card.report_id == report_id,
                Card.is_active == True,
                Card.is_deleted == False
            )
            .order_by(Card.sequence)
        )
        
        cards_result = await session.execute(cards_stmt)
        cards = cards_result.scalars().all()
        
        if not cards:
            logger.warning(f"No active cards found for report {report_id}")
            return {
                "success": False,
                "error": "No active cards found for this report"
            }
        
        # Link each card to the version
        for card in cards:
            mapping = ReportVersionCard(
                id=str(uuid7()),
                report_version_id=version.id,
                parent_card_id=card.id,  # Use the card's primary key
                sequence=card.sequence,
                is_modified=False,  # All cards in first generation are not "modified"
                created_at=datetime.now(timezone.utc)
            )
            session.add(mapping)
        
        # Update version's generated_at timestamp and status
        version.generated_at = datetime.now(timezone.utc)
        version.status = ReportStatus.OUTPUT_GENERATED.value
        
        await session.commit()
        
        logger.info(f"Finalized version {version.id} (v{version.version}) with {len(cards)} cards for report {report_id}")
        return {
            "success": True,
            "version_id": version.id,
            "version": version.version,
            "cards_linked": len(cards),
            "already_finalized": False
        }
        
    except Exception as e:
        logger.error(f"Error finalizing report version: {str(e)}", exc_info=True)
        return {"success": False, "error": str(e)}
async def create_new_report_version(
    session: AsyncSession,
    report_id: str,
    cards_data: list,
    previous_version_id: str = None
) -> Dict[str, Any]:
    """
    Create a new report version, reusing unchanged cards and creating new ones for modified cards.
    
    Args:
        session (AsyncSession): SQLAlchemy async session
        report_id (str): ID of the base report
        cards_data (list): List of card data dictionaries with structure:
            - For unchanged cards: {"card_id": "...", "sequence": N, "is_modified": False, "existing_card_pk_id": "..."}
            - For new/modified cards: complete card data with is_modified=True
        previous_version_id (str, optional): ID of previous version to copy from
        
    Returns:
        Dict[str, Any]: Dictionary with success status, new version details
    """
    logger.info(f"Creating new version for report {report_id}")
    
    try:
        # Get current report to determine next version number
        report_stmt = select(Report).where(Report.id == report_id)
        report_result = await session.execute(report_stmt)
        report = report_result.scalar_one_or_none()
        
        if not report:
            return {"success": False, "error": f"Report {report_id} not found"}
        
        next_version = report.current_version + 1
        
        # Deactivate previous active version
        if previous_version_id:
            prev_version_stmt = select(ReportVersion).where(ReportVersion.id == previous_version_id)
            prev_version_result = await session.execute(prev_version_stmt)
            prev_version = prev_version_result.scalar_one_or_none()
            if prev_version:
                prev_version.is_active = False
        else:
            # Deactivate all previous versions
            deactivate_stmt = (
                update(ReportVersion)
                .where(ReportVersion.report_id == report_id)
                .values(is_active=False)
            )
            await session.execute(deactivate_stmt)
        
        await session.flush()
        
        # Create new version
        new_version = ReportVersion(
            id=str(uuid7()),
            report_id=report_id,
            version=next_version,
            s3_uri={"md_explicit": False, "html_explicit": False},
            is_active=True,
            status=ReportStatus.ANALYSIS_COMPLETED.value,
            last_activity_at=datetime.now(timezone.utc),
            created_at=datetime.now(timezone.utc)
        )
        session.add(new_version)
        await session.flush()
        
        # Link cards to this version
        for card_info in cards_data:
            if card_info.get('is_modified', False):
                # Check if this modified card already exists (has pk_id from detect_modified_cards)
                if card_info.get('pk_id'):
                    # Modified card already exists - just reuse it (don't create duplicate!)
                    card_pk_id = card_info.get('pk_id')
                    logger.info(f"Reusing existing modified card with pk_id: {card_pk_id}")
                else:
                    # This is a truly new card - create it
                    card_result = await insert_card(
                        session=session,
                        report_id=report_id,
                        report_card=card_info
                    )
                    if not card_result.get('success'):
                        raise Exception(f"Failed to insert modified card: {card_result.get('error')}")
                    
                    card_pk_id = card_result.get('parent_card_id')
                    logger.info(f"Created new card with pk_id: {card_pk_id}")
            else:
                # Reuse existing unchanged card
                card_pk_id = card_info.get('existing_card_pk_id')
                if not card_pk_id:
                    logger.error(f"Missing existing_card_pk_id for unchanged card")
                    continue
            
            # Link card to version
            link_result = await link_card_to_report_version(
                session=session,
                report_version_id=new_version.id,
                card_primary_id=card_pk_id,
                sequence=card_info.get('sequence', 0),
                is_modified=card_info.get('is_modified', False)
            )
            
            if not link_result.get('success'):
                logger.warning(f"Failed to link card to version: {link_result.get('error')}")
        
        # Update report's current_version and last_activity_at
        report.current_version = next_version
        report.last_activity_at = datetime.now(timezone.utc)
        
        await session.commit()
        
        logger.info(f"Successfully created version {next_version} for report {report_id}")
        return {
            "success": True,
            "version_id": new_version.id,
            "version": next_version,
            "report_id": report_id
        }
        
    except Exception as e:
        await session.rollback()
        logger.error(f"Error creating new report version: {str(e)}", exc_info=True)
        return {"success": False, "error": str(e)}
async def detect_modified_cards(report_id: str, session: AsyncSession, generation_type: str = "report") -> Dict[str, Any]:
    """
    Automatically detect which cards have been modified since the last generation of ANY type.
    
    This function compares card updated_at timestamps with the MOST RECENT generation timestamp
    (from report, infographic, or presentation) to determine which cards are new/modified.
    
    Logic:
    - Find the most recent generation timestamp from: generated_at, info_pdf_generation_time, pptx_generation_time
    - Compare card timestamps against this most recent generation
    - If any card was modified after the most recent generation → new version needed
    
    If no generation has occurred yet (first time for this version), all cards are treated as "unchanged"
    since there's nothing to compare against.
    
    Note: updated_at/created_at is set when:
    - Card is created (created_at defaults to now, updated_at = server_default)
    - Card content is refined/edited (NEW card record created with created_at = now)
    - Visualization is refined or deleted (updated_at manually set)
    
    Args:
        report_id (str): ID of the report
        session (AsyncSession): SQLAlchemy async session
        generation_type (str): Type of generation requesting detection (for logging)
        
    Returns:
        Dict[str, Any]: Dictionary with:
            - success (bool): Operation status
            - modified_cards (list): Full card data for modified cards
            - unchanged_cards (list): Card info (id, pk_id, sequence) for unchanged cards
            - comparison_timestamp (datetime): The timestamp used for comparison
            - is_first_generation (bool): True if no generation has occurred for this version
    """
    logger.info(f"Detecting modified cards for report: {report_id}, requested by: {generation_type}")
    
    try:
        # Get the active report version
        version_stmt = select(ReportVersion).where(
            ReportVersion.report_id == report_id,
            ReportVersion.is_active == True
        )
        version_result = await session.execute(version_stmt)
        active_version = version_result.scalar_one_or_none()
        
        if not active_version:
            logger.error(f"No active version found for report {report_id}")
            return {
                "success": False,
                "error": "No active version found for this report"
            }
        
        # Get all active cards for this report first
        cards_stmt = select(Card).where(
            Card.report_id == report_id,
            Card.is_active == True
        ).order_by(Card.sequence)
        
        cards_result = await session.execute(cards_stmt)
        all_cards = cards_result.scalars().all()
        
        # Find the MOST RECENT generation timestamp from any output type
        # This ensures we detect modifications since the last time ANY output was generated
        timestamps = []
        s3_uri = active_version.s3_uri or {}
        
        # 1. Report PDF generation time (generated_at column)
        if active_version.generated_at:
            timestamps.append(('report_pdf', active_version.generated_at))
            logger.debug(f"Found generated_at: {active_version.generated_at}")
        
        # 2. MD generation time (md_generation_time in s3_uri)
        md_gen_time_str = s3_uri.get('md_generation_time')
        if md_gen_time_str:
            try:
                md_ts = datetime.fromisoformat(md_gen_time_str.replace('Z', '+00:00'))
                timestamps.append(('report_md', md_ts))
                logger.debug(f"Found md_generation_time: {md_ts}")
            except (ValueError, AttributeError):
                logger.warning(f"Could not parse md_generation_time: {md_gen_time_str}")
        
        # 3. HTML generation time (html_generation_time in s3_uri)
        html_gen_time_str = s3_uri.get('html_generation_time')
        if html_gen_time_str:
            try:
                html_ts = datetime.fromisoformat(html_gen_time_str.replace('Z', '+00:00'))
                timestamps.append(('report_html', html_ts))
                logger.debug(f"Found html_generation_time: {html_ts}")
            except (ValueError, AttributeError):
                logger.warning(f"Could not parse html_generation_time: {html_gen_time_str}")
        
        # 4. Infographic generation time (info_pdf_generation_time)
        info_pdf_gen_time_str = s3_uri.get('info_pdf_generation_time')
        if info_pdf_gen_time_str:
            try:
                info_ts = datetime.fromisoformat(info_pdf_gen_time_str.replace('Z', '+00:00'))
                timestamps.append(('infographic', info_ts))
                logger.debug(f"Found info_pdf_generation_time: {info_ts}")
            except (ValueError, AttributeError):
                logger.warning(f"Could not parse info_pdf_generation_time: {info_pdf_gen_time_str}")
        
        # 5. Presentation generation time (pptx_generation_time)
        pptx_gen_time_str = s3_uri.get('pptx_generation_time')
        if pptx_gen_time_str:
            try:
                pptx_ts = datetime.fromisoformat(pptx_gen_time_str.replace('Z', '+00:00'))
                timestamps.append(('presentation', pptx_ts))
                logger.debug(f"Found pptx_generation_time: {pptx_ts}")
            except (ValueError, AttributeError):
                logger.warning(f"Could not parse pptx_generation_time: {pptx_gen_time_str}")
        
        # Use the most recent timestamp for comparison
        comparison_timestamp = None
        comparison_source = None
        is_first_generation = False
        
        if timestamps:
            # Sort by timestamp descending and get the most recent
            timestamps.sort(key=lambda x: x[1], reverse=True)
            comparison_source, comparison_timestamp = timestamps[0]
            logger.info(f"Using most recent generation timestamp from {comparison_source}: {comparison_timestamp}")
        
        # If no comparison timestamp found, this is the first generation for this version
        if not comparison_timestamp:
            is_first_generation = True
            logger.info(f"No previous generation found for this version - first generation, treating all cards as unchanged")
            
            # All cards are "unchanged" for first generation
            unchanged_cards = []
            for card in all_cards:
                unchanged_cards.append({
                    "card_id": card.card_id,
                    "existing_card_pk_id": card.id,
                    "sequence": card.sequence,
                    "is_modified": False
                })
            
            return {
                "success": True,
                "modified_cards": [],
                "unchanged_cards": unchanged_cards,
                "comparison_timestamp": None,
                "total_cards": len(all_cards),
                "modified_count": 0,
                "unchanged_count": len(all_cards),
                "is_first_generation": True
            }
        
        modified_cards = []
        unchanged_cards = []
        
        for card in all_cards:
            # Use updated_at for comparison (falls back to created_at if not available for backward compatibility)
            last_modified = card.updated_at if hasattr(card, 'updated_at') and card.updated_at else card.created_at
            
            # Compare card last modification time with comparison timestamp
            if last_modified > comparison_timestamp:
                # Card was created/edited AFTER last generation → MODIFIED
                modified_cards.append({
                    "id": card.card_id,  # Business card_id
                    "pk_id": card.id,     # Primary key
                    "section": [card.content] if isinstance(card.content, dict) else card.content,
                    "sub_sections": card.sub_sections or [],
                    "citations": card.citations or {},
                    "summary": card.summary or "",
                    "type": card.type,
                    "sequence": card.sequence,
                    "is_modified": True
                })
                logger.info(f"Card {card.card_id} is MODIFIED (updated: {last_modified}, comparison: {comparison_timestamp})")
            else:
                # Card existed when last generation occurred → UNCHANGED
                unchanged_cards.append({
                    "card_id": card.card_id,
                    "existing_card_pk_id": card.id,
                    "sequence": card.sequence,
                    "is_modified": False
                })
                logger.debug(f"Card {card.card_id} is UNCHANGED (updated: {last_modified}, comparison: {comparison_timestamp})")
        
        logger.info(f"Detection complete for {generation_type}: {len(modified_cards)} modified, {len(unchanged_cards)} unchanged")
        
        return {
            "success": True,
            "modified_cards": modified_cards,
            "unchanged_cards": unchanged_cards,
            "comparison_timestamp": comparison_timestamp.isoformat() if comparison_timestamp else None,
            "total_cards": len(all_cards),
            "modified_count": len(modified_cards),
            "unchanged_count": len(unchanged_cards),
            "is_first_generation": False
        }
        
    except Exception as e:
        logger.error(f"Error detecting modified cards: {str(e)}", exc_info=True)
        return {"success": False, "error": str(e)}
async def get_active_report_version(report_id: str, session: AsyncSession) -> Dict[str, Any]:
    """
    Get the active version of a report with all its cards.
    
    Args:
        report_id (str): ID of the report
        session (AsyncSession): SQLAlchemy async session
        
    Returns:
        Dict[str, Any]: Dictionary with version info and cards
    """
    try:
        # Get active version
        version_stmt = select(ReportVersion).where(
            ReportVersion.report_id == report_id,
            ReportVersion.is_active == True
        )
        version_result = await session.execute(version_stmt)
        version = version_result.scalar_one_or_none()
        
        if not version:
            return {"success": True, "version": None, "cards": []}
        
        # Get cards for this version
        cards_stmt = (
            select(Card, ReportVersionCard.sequence, ReportVersionCard.is_modified)
            .join(ReportVersionCard, Card.id == ReportVersionCard.parent_card_id)
            .where(ReportVersionCard.report_version_id == version.id)
            .order_by(ReportVersionCard.sequence)
        )
        cards_result = await session.execute(cards_stmt)
        cards_data = cards_result.all()
        
        cards_list = []
        for card, sequence, is_modified in cards_data:
            cards_list.append({
                "id": card.id,
                "card_id": card.card_id,
                "title": card.title,
                "sequence": sequence,
                "content": card.content,
                "sub_sections": card.sub_sections,
                "citations": card.citations,
                "summary": card.summary,
                "type": card.type,
                "is_modified": is_modified,
                "version": card.version
            })
        
        return {
            "success": True,
            "version": {
                "id": version.id,
                "version": version.version,
                "status": version.status,
                "s3_uri": version.s3_uri,
                "generated_at": version.generated_at.isoformat() if version.generated_at else None,
                "last_activity_at": version.last_activity_at.isoformat() if version.last_activity_at else None
            },
            "cards": cards_list
        }
        
    except Exception as e:
        logger.error(f"Error getting active report version: {str(e)}", exc_info=True)
        return {"success": False, "error": str(e)}
async def get_chat_reports_and_cards(chat_id: int, session: AsyncSession) -> Dict[str, Any]:
    """
    Get all report files for a specific chat_id, including their associated cards.
    
    Args:
        chat_id (int): The chat_id to get reports for
        session (AsyncSession): The SQLAlchemy async session
            
    Returns:
        Dict[str, Any]: Dictionary with success status and reports with their cards
    """
    logger.info(f"Getting report files for chat_id: {chat_id}")

    if not chat_id:
        logger.error("Invalid chat_id: empty value provided")
        return {
            "success": False,
            "error": "Invalid chat ID: empty value provided"
        }
    
    report_list = []

    try:
        # A chat can have multiple reports (retries, reruns) — fetch all, newest first
        stmt = select(Report).where(Report.chat_id == chat_id).order_by(Report.created_at.desc())
        result = await session.execute(stmt)
        reports = result.scalars().all()

        if not reports:
            logger.warning(f"No report found for chat_id: {chat_id}")
            return {
                "success": True,
                "reports": []
            }

        for report in reports:
            # Get all active cards directly from Card table
            cards_stmt = select(Card).where(
                Card.report_id == report.id,
                Card.is_active == True,
                Card.is_deleted == False
            ).order_by(Card.sequence)

            cards_result = await session.execute(cards_stmt)
            cards = cards_result.scalars().all()

            # Convert cards to list of dictionaries
            cards_data = []
            for card in cards:
                content_obj = card.content if isinstance(card.content, dict) else {}
                cards_data.append({
                    "id": card.card_id,
                    "title": card.title,
                    "sequence": card.sequence,
                    "content": content_obj.get('content', ''),
                    "tables": _normalize_tables_for_response(content_obj.get('tables', [])),
                    "sub_sections": _normalize_subsections_for_response(card.sub_sections),
                    "citations": card.citations,
                    "summary": content_obj.get('summary', ''),
                    "type": card.type
                })

            filtered_layout = await prune_report_layout_async(report.layout, cards_data)

            # Build report response
            report_data = {
                "id": report.id,
                "version": report.current_version,
                "current_version": report.current_version,
                "poster_image_url": report.poster_image_url,
                "s3_uri": report.s3_uri,
                "status": report.status,
                "last_activity_at": report.last_activity_at.isoformat() if report.last_activity_at else None,
                "created_at": report.created_at.isoformat() if report.created_at else None,
                "generated_at": report.generated_at.isoformat() if report.generated_at else None,
                "title": report.title,
                "report_layout": filtered_layout,
                "summary": report.summary,
                "length": report.length,
                "domain_name": report.domain_name,
                "report_type": report.report_type,
                "citations": report.citations,
                "cards": cards_data
            }

            report_list.append(report_data)

        logger.info(f"Successfully retrieved {len(report_list)} report(s) for chat_id: {chat_id}")
        return {
            "success": True,
            "reports": report_list
        }

    except SQLAlchemyError as e:
        logger.error(f"Database error retrieving report files: {str(e)}", exc_info=True)
        return {
            "success": False,
            "error": f"Database error: {str(e)}"
        }

    except Exception as e:
        logger.error(f"Unexpected error retrieving report files: {str(e)}", exc_info=True)
        return {
            "success": False,
            "error": f"Unexpected error: {str(e)}"
        }
async def insert_publish_details(publish_data: Dict[str, Any], session: AsyncSession) -> Dict[str, Any]:
    """
    Insert publishing details for a report.
    
    Args:
        publish_data (Dict[str, Any]): Dictionary containing publish data with keys:
            - report_id (str): ID of the associated report (required)
            - faq (Dict/List, optional): Frequently asked questions related to the report
            - insights (Dict/List, optional): Key insights from the report
            - industries_jobs (str, optional): Industries or job sectors the report is relevant to
            - geographic_areas (str, optional): Geographic areas covered in the report
            - special_emphasis (str, optional): Special areas of emphasis in the report
            - audience (str, optional): Target audience for the report
            - purpose (str, optional): Purpose of the report
            - overview (str, optional): Overview or summary of the report
            - media_details (Dict, optional): Details about media elements in the report
            - page_count (int, optional): Number of pages in the report
            - source_count (int, optional): Number of sources cited in the report
            - table_count (int, optional): Number of tables in the report
            - viz_count (int, optional): Number of visualizations in the report
        session (AsyncSession): SQLAlchemy async session
            
    Returns:
        Dict[str, Any]: Dictionary with success status and publish_id
    """
    logger.info(f"Creating publish details for report: {publish_data.get('report_id')}")
    
    # Validate required fields
    if 'report_id' not in publish_data or not publish_data['report_id']:
        error_msg = "Missing required field: report_id"
        logger.error(error_msg)
        return {
            "success": False,
            "error": error_msg
        }
    
    try:
        # Check if report exists
        report_stmt = select(Report).where(Report.id == publish_data['report_id'])
        report_result = await session.execute(report_stmt)
        report = report_result.scalar_one_or_none()
        
        if not report:
            error_msg = f"Report with ID {publish_data['report_id']} not found"
            logger.error(error_msg)
            return {
                "success": False,
                "error": error_msg
            }
        
        # Generate a new UUID for the publish record
        publish_id = str(uuid7())
        
        # Create a new publish record
        new_publish = Publish(
            id=publish_id,
            report_id=publish_data['report_id'],
            faq=publish_data.get('faq'),
            insights=publish_data.get('insights'),
            industries_jobs=publish_data.get('industries_jobs'),
            geographic_areas=publish_data.get('geographic_areas'),
            special_emphasis=publish_data.get('special_emphasis'),
            audience=publish_data.get('audience'),
            purpose=publish_data.get('purpose'),
            overview=publish_data.get('overview'),
            media_details=publish_data.get('media_details'),
            page_count=publish_data.get('page_count'),
            source_count=publish_data.get('source_count'),
            table_count=publish_data.get('table_count'),
            viz_count=publish_data.get('viz_count')
        )
        
        session.add(new_publish)
        await session.commit()
        
        logger.info(f"Successfully created publish details with ID: {publish_id}")
        return {
            "success": True,
            "publish_id": publish_id
        }
        
    except SQLAlchemyError as e:
        await session.rollback()
        error_msg = f"Database error: {str(e)}"
        logger.error(error_msg, exc_info=True)
        return {
            "success": False,
            "error": error_msg
        }
    except Exception as e:
        await session.rollback()
        error_msg = f"Unexpected error: {str(e)}"
        logger.error(error_msg, exc_info=True)
        return {
            "success": False,
            "error": error_msg
        }
async def get_report_domain_summary(user_id: str, session: AsyncSession) -> Dict[str, Any]:
    """Return a per-domain summary of non-draft reports for a user.

    Only domains that have at least one non-draft report are included.

    Args:
        user_id: Authenticated user's ID.
        session: SQLAlchemy async session.

    Returns:
        Dict with keys:
            - ``success`` (bool)
            - ``domains`` (list[dict]): Each item has ``domain_name``,
              ``category_name`` (display label), and ``item_count``.
            - ``error`` (str, optional)
    """
    logger.info(f"Fetching report domain summary for user_id: {user_id}")

    try:
        stmt = (
            select(
                Report.domain_name,
                func.count(Report.id).label("item_count"),
            )
            .join(Message, Report.chat_id == Message.id)
            .where(
                Message.user_id == user_id,
                ~Message.is_deleted,
                Report.status.in_(_NON_DRAFT_STATUSES),
            )
            .group_by(Report.domain_name)
            .order_by(func.count(Report.id).desc())
        )

        result = await session.execute(stmt)
        rows = result.all()

        domains = []
        for row in rows:
            slug = row.domain_name or "default"
            domains.append(
                {
                    "domain_name": slug,
                    "category_name": DOMAIN_DISPLAY_NAMES.get(slug, slug.replace("_", " ").title()),
                    "item_count": row.item_count,
                }
            )

        logger.info(
            f"Domain summary for user_id: {user_id} — {len(domains)} domain(s) with non-draft reports"
        )
        return {"success": True, "domains": domains}

    except SQLAlchemyError as e:
        logger.error(f"DB error in get_report_domain_summary: {str(e)}", exc_info=True)
        return {"success": False, "error": f"Database error: {str(e)}"}
    except Exception as e:
        logger.error(f"Unexpected error in get_report_domain_summary: {str(e)}", exc_info=True)
        return {"success": False, "error": f"Unexpected error: {str(e)}"}
async def get_reports_by_domain(
    user_id: str,
    domain_name: str,
    limit: int,
    offset: int,
    session: AsyncSession,
) -> Dict[str, Any]:
    """Return paginated non-draft reports for a user filtered by domain.

    Results are ordered by ``last_activity_at`` descending (most recent first).

    Args:
        user_id: Authenticated user's ID.
        domain_name: Domain slug to filter by (e.g. ``"due_diligence"``).
            Pass ``"default"`` for reports with no specific domain.
        limit: Maximum number of records to return.
        offset: Number of records to skip (for pagination).
        session: SQLAlchemy async session.

    Returns:
        Dict with keys:
            - ``success`` (bool)
            - ``domain_name`` (str): Slug echoed back.
            - ``category_name`` (str): Human-readable display label.
            - ``total`` (int): Total non-draft reports in this domain (for the user).
            - ``limit`` (int): Applied limit.
            - ``offset`` (int): Applied offset.
            - ``reports`` (list[dict]): Paginated report records.
            - ``error`` (str, optional)
    """
    logger.info(
        f"Fetching reports for user_id: {user_id}, domain_name: {domain_name}, "
        f"limit: {limit}, offset: {offset}"
    )

    try:
        # Normalise: treat NULL domain_name in DB as "default"
        domain_filter = (
            Report.domain_name.is_(None)
            if domain_name == "default"
            else Report.domain_name == domain_name
        )

        base_where = [
            Message.user_id == user_id,
            ~Message.is_deleted,
            Report.status.in_(_NON_DRAFT_STATUSES),
            domain_filter,
        ]

        # Total count for pagination metadata
        count_stmt = (
            select(func.count(Report.id))
            .join(Message, Report.chat_id == Message.id)
            .where(*base_where)
        )
        total_result = await session.execute(count_stmt)
        total = total_result.scalar() or 0

        # Paginated records
        reports_stmt = (
            select(Report)
            .join(Message, Report.chat_id == Message.id)
            .where(*base_where)
            .order_by(Report.last_activity_at.desc())
            .limit(limit)
            .offset(offset)
        )
        reports_result = await session.execute(reports_stmt)
        reports = reports_result.scalars().all()

        reports_data = [
            {
                "report_id": r.id,
                "chat_id": r.chat_id,
                "title": r.title,
                "status": r.status,
                "domain_name": r.domain_name or "default",
                "poster_image_url": r.poster_image_url,
                "current_version": r.current_version,
                "created_at": r.created_at.isoformat() if r.created_at else None,
                "last_activity_at": r.last_activity_at.isoformat() if r.last_activity_at else None,
                "generated_at": r.generated_at.isoformat() if r.generated_at else None,
                "s3_uri": r.s3_uri,
            }
            for r in reports
        ]

        logger.info(
            f"Returning {len(reports_data)} of {total} reports for "
            f"user_id: {user_id}, domain_name: {domain_name}"
        )
        return {
            "success": True,
            "domain_name": domain_name,
            "category_name": DOMAIN_DISPLAY_NAMES.get(domain_name, domain_name.replace("_", " ").title()),
            "total": total,
            "limit": limit,
            "offset": offset,
            "reports": reports_data,
        }

    except SQLAlchemyError as e:
        logger.error(f"DB error in get_reports_by_domain: {str(e)}", exc_info=True)
        return {"success": False, "error": f"Database error: {str(e)}"}
    except Exception as e:
        logger.error(f"Unexpected error in get_reports_by_domain: {str(e)}", exc_info=True)
        return {"success": False, "error": f"Unexpected error: {str(e)}"}
def _to_external_domain(internal_slug: Optional[str]) -> str:
    """Translate the internal ``default`` slug (or NULL) to ``standard``.

    Every other slug is returned unchanged.
    """
    if not internal_slug or internal_slug == DEFAULT_DOMAIN_INTERNAL:
        return STANDARD_CATEGORY_SLUG
    return internal_slug
def _to_internal_domain(external_slug: str) -> str:
    """Reverse of :func:`_to_external_domain` — used for filter parameters."""
    if external_slug == STANDARD_CATEGORY_SLUG:
        return DEFAULT_DOMAIN_INTERNAL
    return external_slug
async def get_all_version_outputs_list(report_id: str, session: AsyncSession) -> Dict[str, Any]:
    """
    Get all available outputs (non-null files) across all versions for a report.
    Returns a simple list of {version, file_type, generated_at} for frontend display.
    
    Args:
        report_id (str): The report ID
        session (AsyncSession): Database session
        
    Returns:
        Dict containing list of all available outputs
    """
    try:
        logger.info(f"Fetching all version outputs list for report_id: {report_id}")
        
        # Fetch all versions for the report, ordered by version descending
        versions_stmt = (
            select(ReportVersion)
            .where(ReportVersion.report_id == report_id)
            .order_by(ReportVersion.version.desc())
        )
        result = await session.execute(versions_stmt)
        versions = result.scalars().all()
        
        if not versions:
            logger.warning(f"No versions found for report_id: {report_id}")
            return {
                "success": False,
                "error": f"No versions found for report {report_id}"
            }
        
        # Build list of all available outputs
        outputs = []
        
        for version in versions:
            s3_uri = version.s3_uri or {}
            version_num = version.version
            generated_at = version.generated_at.isoformat() if version.generated_at else None
            
            # Check each file type and add if not null
            if s3_uri.get('pdf'):
                outputs.append({
                    "version": version_num,
                    "file_type": "pdf",
                    "s3_path": s3_uri.get('pdf'),
                    "generated_at": generated_at
                })
            
            if s3_uri.get('html') and ('html_explicit' not in s3_uri or s3_uri.get('html_explicit') == True):
                html_generated_at = s3_uri.get('html_generation_time') or generated_at
                outputs.append({
                    "version": version_num,
                    "file_type": "html",
                    "s3_path": s3_uri.get('html'),
                    "generated_at": html_generated_at
                })
            
            if s3_uri.get('md') and ('md_explicit' not in s3_uri or s3_uri.get('md_explicit') == True):
                md_generated_at = s3_uri.get('md_generation_time') or generated_at
                outputs.append({
                    "version": version_num,
                    "file_type": "md",
                    "s3_path": s3_uri.get('md'),
                    "generated_at": md_generated_at
                })
            
            if s3_uri.get('pptx'):
                # Use pptx_generation_time if available
                pptx_generated_at = s3_uri.get('pptx_generation_time') or generated_at
                outputs.append({
                    "version": version_num,
                    "file_type": "pptx",
                    "s3_path": s3_uri.get('pptx'),
                    "generated_at": pptx_generated_at
                })
            
            if s3_uri.get('info_pdf'):
                # Use info_pdf_generation_time if available
                info_pdf_generated_at = s3_uri.get('info_pdf_generation_time') or generated_at
                outputs.append({
                    "version": version_num,
                    "file_type": "info_pdf",
                    "s3_path": s3_uri.get('info_pdf'),
                    "generated_at": info_pdf_generated_at
                })
        
        logger.info(f"Successfully fetched {len(outputs)} output files for report_id: {report_id}")
        
        return {
            "success": True,
            "report_id": report_id,
            "outputs": outputs
        }
        
    except Exception as e:
        logger.error(f"Error fetching version outputs list for report {report_id}: {str(e)}", exc_info=True)
        return {
            "success": False,
            "error": str(e)
        }
async def get_version_file_for_download(report_id: str, version: int, file_type: str, session: AsyncSession) -> Dict[str, Any]:
    """
    Get S3 path for a specific file type in a specific version for download.
    
    Args:
        report_id (str): The report ID
        version (int): The version number
        file_type (str): The file type (pdf, html, md, pptx, info_pdf)
        session (AsyncSession): Database session
        
    Returns:
        Dict with s3_path or error
    """
    try:
        logger.info(f"Fetching {file_type} for report_id: {report_id}, version: {version}")
        
        # Validate file_type
        if file_type not in ['pdf', 'html', 'md', 'pptx', 'info_pdf']:
            return {
                "success": False,
                "error": f"Invalid file type: {file_type}. Must be one of: pdf, html, md, pptx, info_pdf"
            }
        
        # Fetch the specific version
        version_stmt = (
            select(ReportVersion)
            .where(
                ReportVersion.report_id == report_id,
                ReportVersion.version == version
            )
        )
        result = await session.execute(version_stmt)
        report_version = result.scalar_one_or_none()
        
        if not report_version:
            logger.warning(f"Version {version} not found for report_id: {report_id}")
            return {
                "success": False,
                "error": f"Version {version} not found for this report"
            }
        
        # Get s3_uri
        s3_uri = report_version.s3_uri or {}
        s3_path = s3_uri.get(file_type)
        
        if not s3_path:
            logger.warning(f"{file_type} not found for report_id: {report_id}, version: {version}")
            return {
                "success": False,
                "error": f"{file_type.upper()} file not found for version {version}"
            }
        
        logger.info(f"Successfully fetched {file_type} path for version {version}")
        return {
            "success": True,
            "report_id": report_id,
            "version": version,
            "file_type": file_type,
            "s3_path": s3_path
        }
        
    except Exception as e:
        logger.error(f"Error fetching {file_type} for report {report_id} version {version}: {str(e)}", exc_info=True)
        return {
            "success": False,
            "error": str(e)
        }
async def get_report_version_history_data(
    report_id: str,
    session: AsyncSession
) -> Dict[str, Any]:
    """
    Get version history data for a report.
    Returns all versions with their output status for the dropdown.
    
    Args:
        report_id: ID of the report
        session: Database session
        
    Returns:
        Dict with success status, s3_base_path, and versions list
    """
    try:
        logger.info(f"Getting version history for report_id: {report_id}")
        
        # Verify report exists
        report_stmt = select(Report).where(Report.id == report_id)
        report_result = await session.execute(report_stmt)
        report = report_result.scalar_one_or_none()
        
        if not report:
            return {"success": False, "error": "Report not found"}
        
        # Get all versions ordered by version descending (newest first)
        versions_stmt = (
            select(ReportVersion)
            .where(ReportVersion.report_id == report_id)
            .order_by(ReportVersion.version.desc())
        )
        versions_result = await session.execute(versions_stmt)
        versions = versions_result.scalars().all()
        
        if not versions:
            return {"success": False, "error": "No versions found for this report"}
        
        # Get the active version for is_modified check
        active_version = next((v for v in versions if v.is_active), None)
        
        # Check if cards have been modified since last output (for active version only)
        is_modified = False
        if active_version:
            # Check if any output exists
            s3_uri = active_version.s3_uri or {}
            has_any_output = (
                active_version.generated_at or
                s3_uri.get('md_generation_time') or
                s3_uri.get('html_generation_time') or
                s3_uri.get('info_pdf_generation_time') or
                s3_uri.get('pptx_generation_time')
            )
            
            if has_any_output:
                # Check for modifications
                detection_result = await detect_modified_cards(report_id=report_id, session=session)
                if detection_result.get('success'):
                    is_modified = detection_result.get('modified_count', 0) > 0
        
        # Build s3_base_path by extracting from any existing s3_uri
        # S3 path structure: s3://bucket/{base_path}/report_{report_id}/v{version}/...
        # We need to find the base path up to and including report_{report_id}/
        report_marker = f"report_{report_id}/"
        s3_base_path = None
        
        # Try to extract base path from any existing s3_uri in versions
        for version in versions:
            version_s3_uri = version.s3_uri or {}
            for key in ['pdf', 'html', 'md', 'pptx', 'info_pdf']:
                uri = version_s3_uri.get(key)
                if uri and report_marker in uri:
                    # Extract path up to and including report_{report_id}/
                    idx = uri.find(report_marker)
                    s3_base_path = uri[:idx + len(report_marker)].rstrip('/')
                    break
            if s3_base_path:
                break
        
        # Fallback if no existing URIs found
        if not s3_base_path:
            from src.config.constants import S3_BUCKET_NAME
            s3_base_path = f"s3://{S3_BUCKET_NAME}/report_{report_id}"
        
        # Helper to extract relative path from full S3 URI
        def get_relative_path(full_uri: str) -> str:
            if not full_uri:
                return None
            # Extract path after report_{report_id}/
            # e.g., "s3://bucket/.../report_rpt-123/v1/report/file.pdf" -> "v1/report/file.pdf"
            if report_marker in full_uri:
                return full_uri.split(report_marker)[-1]
            return None
        
        # Build version list
        versions_data = []
        for version in versions:
            s3_uri = version.s3_uri or {}
            
            # Format created_at as human-readable label
            created_at = version.created_at
            if created_at:
                # Format: "3rd Nov 2025, 2:30 PM"
                day = created_at.day
                suffix = 'th' if 11 <= day <= 13 else {1: 'st', 2: 'nd', 3: 'rd'}.get(day % 10, 'th')
                label = created_at.strftime(f"%-d{suffix} %b %Y, %-I:%M %p")
            else:
                label = "Unknown"
            
            version_data = {
                "report_version_id": version.id,
                "version": version.version,
                "is_current": version.is_active,
                "is_modified": is_modified if version.is_active else False,
                "label": label,
                "created_at": created_at.isoformat() if created_at else None,
                "generated_outputs": {
                    "pdf": {
                        "generated": bool(s3_uri.get('pdf')),
                        "s3_path": get_relative_path(s3_uri.get('pdf')),
                        "generated_at": version.generated_at.isoformat() if version.generated_at else None
                    },
                    "html": {
                        "generated": bool(s3_uri.get('html')) and ('html_explicit' not in s3_uri or s3_uri.get('html_explicit') == True),
                        "s3_path": get_relative_path(s3_uri.get('html')) if ('html_explicit' not in s3_uri or s3_uri.get('html_explicit') == True) else None,
                        "generated_at": (s3_uri.get('html_generation_time') or (version.generated_at.isoformat() if version.generated_at else None)) if ('html_explicit' not in s3_uri or s3_uri.get('html_explicit') == True) else None
                    },
                    "md": {
                        "generated": bool(s3_uri.get('md')) and ('md_explicit' not in s3_uri or s3_uri.get('md_explicit') == True),
                        "s3_path": get_relative_path(s3_uri.get('md')) if ('md_explicit' not in s3_uri or s3_uri.get('md_explicit') == True) else None,
                        "generated_at": (s3_uri.get('md_generation_time') or (version.generated_at.isoformat() if version.generated_at else None)) if ('md_explicit' not in s3_uri or s3_uri.get('md_explicit') == True) else None
                    },
                    "info_pdf": {
                        "generated": bool(s3_uri.get('info_pdf')),
                        "s3_path": get_relative_path(s3_uri.get('info_pdf')),
                        "generated_at": s3_uri.get('info_pdf_generation_time')
                    },
                    "pptx": {
                        "generated": bool(s3_uri.get('pptx')),
                        "s3_path": get_relative_path(s3_uri.get('pptx')),
                        "generated_at": s3_uri.get('pptx_generation_time')
                    }
                }
            }
            versions_data.append(version_data)
        
        logger.info(f"Successfully retrieved {len(versions_data)} versions for report_id: {report_id}")
        return {
            "success": True,
            "report_id": report_id,
            "s3_base_path": s3_base_path,
            "versions": versions_data
        }
        
    except Exception as e:
        logger.error(f"Error getting version history: {str(e)}", exc_info=True)
        return {"success": False, "error": str(e)}
async def get_report_info_by_version(
    report_id: str,
    report_version_id: str,
    session: AsyncSession
) -> Dict[str, Any]:
    """
    Get report info for a specific version, including cards linked to that version.
    Returns data in the same format as get_chat_reports_and_cards.
    
    Args:
        report_id: ID of the report
        report_version_id: ID of the specific report version
        session: Database session
        
    Returns:
        Dict with success status and report data with cards
    """
    logger.info(f"Getting report info for report_id: {report_id}, version_id: {report_version_id}")
    
    if not report_id or not report_version_id:
        logger.error("Invalid report_id or report_version_id: empty value provided")
        return {
            "success": False,
            "error": "Invalid report ID or version ID: empty value provided"
        }
    
    try:
        # Get the report
        report_stmt = select(Report).where(Report.id == report_id)
        report_result = await session.execute(report_stmt)
        report = report_result.scalar_one_or_none()
        
        if not report:
            logger.warning(f"Report with ID {report_id} not found")
            return {
                "success": False,
                "error": f"Report with ID {report_id} not found"
            }
        
        # Get the specific version
        version_stmt = select(ReportVersion).where(
            ReportVersion.id == report_version_id,
            ReportVersion.report_id == report_id
        )
        version_result = await session.execute(version_stmt)
        version = version_result.scalar_one_or_none()
        
        if not version:
            logger.warning(f"Version with ID {report_version_id} not found for report {report_id}")
            return {
                "success": False,
                "error": f"Version with ID {report_version_id} not found"
            }
        
        # Get cards linked to this version via junction table
        cards_stmt = (
            select(Card)
            .join(ReportVersionCard, ReportVersionCard.parent_card_id == Card.id)
            .where(ReportVersionCard.report_version_id == report_version_id)
            .order_by(ReportVersionCard.sequence)
        )
        cards_result = await session.execute(cards_stmt)
        cards = cards_result.scalars().all()
        
        # If no cards in version snapshot, fall back to active cards
        if not cards:
            logger.info(f"No cards in version snapshot, falling back to active cards for report {report_id}")
            cards_stmt = select(Card).where(
                Card.report_id == report_id,
                Card.is_active == True,
                Card.is_deleted == False
            ).order_by(Card.sequence)
            cards_result = await session.execute(cards_stmt)
            cards = cards_result.scalars().all()
        
        # Convert cards to list of dictionaries (same format as get_chat_reports_and_cards)
        cards_data = []
        for card in cards:
            cards_data.append({
                "id": card.card_id,
                "title": card.title,
                "sequence": card.sequence,
                "content": card.content.get('content', '') if isinstance(card.content, dict) else card.content or '',
                "tables": card.content.get('tables', []) if isinstance(card.content, dict) else [],
                "sub_sections": card.sub_sections,
                "citations": card.citations,
                "summary": card.summary,
                "type": card.type
            })
        
        filtered_layout = await prune_report_layout_async(report.layout, cards_data)
        
        # Build report response (same format as get_chat_reports_and_cards)
        report_data = {
            "id": report.id,
            "version": version.version,
            "current_version": report.current_version,
            "poster_image_url": report.poster_image_url,
            "s3_uri": report.s3_uri,
            "status": report.status,
            "last_activity_at": report.last_activity_at.isoformat() if report.last_activity_at else None,
            "created_at": report.created_at.isoformat() if report.created_at else None,
            "generated_at": report.generated_at.isoformat() if report.generated_at else None,
            "title": report.title,
            "report_layout": filtered_layout,
            "summary": report.summary,
            "length": report.length,
            "domain_name": report.domain_name,
            "citations": report.citations,
            "cards": cards_data
        }
        
        logger.info(f"Successfully retrieved report info for report_id: {report_id}, version_id: {report_version_id}")
        return {
            "success": True,
            "reports": [report_data]
        }
    
    except SQLAlchemyError as e:
        logger.error(f"Database error retrieving report info: {str(e)}", exc_info=True)
        return {
            "success": False,
            "error": f"Database error: {str(e)}"
        }
    
    except Exception as e:
        logger.error(f"Unexpected error retrieving report info: {str(e)}", exc_info=True)
        return {
            "success": False,
            "error": f"Unexpected error: {str(e)}"
        }
