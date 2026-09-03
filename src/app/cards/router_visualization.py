"""cards routes: visualization.

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
from datetime import UTC, datetime

from fastapi import APIRouter, Depends
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm.attributes import flag_modified

from app.adapters.s3 import (
    get_or_create_presigned_url,
)
from app.auth.repository import (
    check_user_by_id,
)
from app.auth.schemas import ErrorResponse
from app.auth.token import get_current_active_user
from app.cards.repository import (
    delete_visualization,
)
from app.cards.schemas import (
    RefineOrDeleteRequest,
    RefineOrDeleteResponse,
    RefineVisualizationRequest,
    RefineVisualizationResponse,
)
from app.core.db import async_session_scope
from app.core.enums import ReportStatus
from app.core.logging import setup_logging
from app.core.redis import get_redis_instance
from app.models import Card, Table
from app.observability.functionality_context import Functionality, set_functionality
from app.reports.repository import (
    update_report_status_if_needed,
)
from app.research.refine.visualizer.viz_refine import refine_viz

router = APIRouter()
logger = setup_logging(__file__)
redis_instance = get_redis_instance()


@router.post(
    "/refine-visualization",
    response_model=RefineVisualizationResponse,
    status_code=200,
    responses={
        404: {"model": ErrorResponse},
        401: {"model": ErrorResponse},
        422: {"model": ErrorResponse},
        500: {"model": ErrorResponse},
    },
)
async def refine_visualization(
    data: RefineVisualizationRequest, user_id: str = Depends(get_current_active_user)
):
    """Refine visualization for a section or subsection in a report"""
    set_functionality(Functionality.REFINE_VISUALIZATION)
    logger.info(
        f"Refine visualization request received for user_id: {user_id} and report_id: {data.report_id}"
    )

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

        if not data.card.get("id"):
            logger.error("Missing required field: card.id")
            return JSONResponse(
                status_code=422,
                content={"success": False, "error": "Missing required field: card.id"},
            )

        report_id = data.report_id
        card_id = data.card.get("id")
        table_id = data.card.get("table_id")
        user_instruction = data.card.get("user_instruction")
        subsection_data = data.card.get("subsection")
        chat_id = data.card.get("chat_id")

        # Check if user exists and has access to this report
        async with async_session_scope() as session:
            db_response = await check_user_by_id(user_id=user_id, session=session)
            if not db_response.get("success", False):
                logger.error(f"Failed to check user by id: {db_response.get('error')}")
                return JSONResponse(
                    status_code=500,
                    content={
                        "success": False,
                        "error": "Something went wrong while refining your visualization. Please try again.",
                    },
                )
            if not db_response.get("exists", False):
                logger.error(f"User with ID {user_id} not found or token is invalid")
                return JSONResponse(
                    status_code=401, content={"success": False, "error": "Unauthorized request"}
                )

            user_name = db_response.get("user_name")

            # Get user subscription plan
            # user_plan = await get_active_subscription(user_id=user_id, session=session)
            # logger.info(f"User {user_id} subscription plan for visualization refinement: {user_plan}")

            # Check if the card exists and belongs to the specified report
            card_stmt = select(Card).where(
                Card.card_id == card_id, Card.report_id == report_id, Card.is_active
            )
            card_result = await session.execute(card_stmt)
            existing_card = card_result.scalar_one_or_none()

            if not existing_card:
                logger.error(f"Card with ID {card_id} not found for report_id: {report_id}")
                return JSONResponse(
                    status_code=404,
                    content={
                        "success": False,
                        "error": "I can't find the visualization you're trying to refine. It might have been deleted already.",
                    },
                )

            parent_card_id = existing_card.id

            # Get table markdown content
            table_markdown = ""
            subsection_id = None

            # Determine if this is a section or subsection visualization
            if subsection_data:
                subsection_id = subsection_data.get("id")
                table_id = subsection_data.get("table_id")
                user_instruction = subsection_data.get("user_instruction")

                if not subsection_id:
                    logger.error("Missing subsection ID for subsection visualization")
                    return JSONResponse(
                        status_code=422,
                        content={"success": False, "error": "Missing subsection ID"},
                    )

                # Find the subsection and its table
                if existing_card.sub_sections:
                    for subsection in existing_card.sub_sections:
                        if isinstance(subsection, dict) and subsection.get("id") == subsection_id:
                            # Find table markdown in subsection
                            for table in subsection.get("tables", []):
                                if table.get("table_id") == table_id:
                                    # Get table markdown from database using report_id + table_id (stable identifiers)
                                    # Don't filter by parent_card_id as it may be stale after card refinement
                                    # Order by created_at DESC to get the latest entry if duplicates exist
                                    table_stmt = (
                                        select(Table)
                                        .where(
                                            Table.table_id == table_id, Table.report_id == report_id
                                        )
                                        .order_by(Table.created_at.desc())
                                        .limit(1)
                                    )
                                    table_result = await session.execute(table_stmt)
                                    table_obj = table_result.scalar_one_or_none()

                                    if table_obj:
                                        # Self-healing: update stale parent_card_id if needed
                                        if table_obj.parent_card_id != parent_card_id:
                                            logger.info(
                                                f"Updating table {table_id} parent_card_id from {table_obj.parent_card_id} to {parent_card_id} (card was refined)"
                                            )
                                            table_obj.parent_card_id = parent_card_id
                                            await session.flush()
                                        table_markdown = table_obj.table_markdown
                                    break
                            break

                    if not table_markdown:
                        logger.error(
                            f"Table with ID {table_id} not found in subsection {subsection_id}"
                        )
                        return JSONResponse(
                            status_code=404,
                            content={
                                "success": False,
                                "error": "I can't find the visualization you're trying to refine. It might have been deleted already.",
                            },
                        )
            else:
                # Section visualization
                # Find the table in the section
                if existing_card.content and isinstance(existing_card.content, dict):
                    for table in existing_card.content.get("tables", []):
                        if table.get("table_id") == table_id:
                            # Get table markdown from database using report_id + table_id (stable identifiers)
                            # Don't filter by parent_card_id as it may be stale after card refinement
                            # Order by created_at DESC to get the latest entry if duplicates exist
                            table_stmt = (
                                select(Table)
                                .where(Table.table_id == table_id, Table.report_id == report_id)
                                .order_by(Table.created_at.desc())
                                .limit(1)
                            )
                            table_result = await session.execute(table_stmt)
                            table_obj = table_result.scalar_one_or_none()

                            if table_obj:
                                # Self-healing: update stale parent_card_id if needed
                                if table_obj.parent_card_id != parent_card_id:
                                    logger.info(
                                        f"Updating table {table_id} parent_card_id from {table_obj.parent_card_id} to {parent_card_id} (card was refined)"
                                    )
                                    table_obj.parent_card_id = parent_card_id
                                    await session.flush()
                                table_markdown = table_obj.table_markdown
                            break

                if not table_markdown:
                    logger.error(f"Table with ID {table_id} not found in section {card_id}")
                    return JSONResponse(
                        status_code=404,
                        content={
                            "success": False,
                            "error": "I can't find the visualization you're trying to refine. It might have been deleted already.",
                        },
                    )

            # Call viz_refine to generate new visualization
            input_json = {
                "user_ins": user_instruction,
                "table": table_markdown,
                "viz": table_obj.base64_s3_uri,
            }

            new_viz_s3_path = await run_in_threadpool(
                refine_viz, input_json=input_json, user_name=user_name, chat_id=chat_id
            )

            if not new_viz_s3_path:
                logger.error(f"Failed to generate visualization for table {table_id}")
                return JSONResponse(
                    status_code=500,
                    content={
                        "success": False,
                        "error": "Something went wrong while refining your visualization. Please try again.",
                    },
                )

            # Update the base64_s3_uri field in the Table record
            table_stmt = select(Table).where(
                Table.table_id == table_id,
                Table.report_id == report_id,
                Table.parent_card_id == parent_card_id,
            )
            table_result = await session.execute(table_stmt)
            table_obj = table_result.scalar_one_or_none()

            if table_obj:
                table_obj.base64_s3_uri = new_viz_s3_path
                logger.info(f"Updated base64_s3_uri for table {table_id} to {new_viz_s3_path}")

            # Update the card with new visualization S3 path
            if subsection_data:
                # Update subsection visualization
                for subsection in existing_card.sub_sections:
                    if isinstance(subsection, dict) and subsection.get("id") == subsection_id:
                        for table in subsection.get("tables", []):
                            if table.get("table_id") == table_id:
                                table["visualization"] = new_viz_s3_path
                                break
                        break

                # Mark the JSONB field as modified
                flag_modified(existing_card, "sub_sections")
                logger.info(
                    f"Updated visualization for subsection {subsection_id} in card {card_id}"
                )
            else:
                # Update section visualization
                if existing_card.content and isinstance(existing_card.content, dict):
                    for table in existing_card.content.get("tables", []):
                        if table.get("table_id") == table_id:
                            table["visualization"] = new_viz_s3_path
                            break

                    # Mark the JSONB field as modified
                    flag_modified(existing_card, "content")
                    logger.info(f"Updated visualization for section {card_id}")

            existing_card.updated_at = datetime.now(UTC)
            logger.info(f"Updated card {card_id} timestamp to mark visualization refinement")

            # Set status to REDO_ANALYSIS after refining visualization
            await update_report_status_if_needed(
                report_id=data.report_id,
                new_status=ReportStatus.REDO_ANALYSIS.value,
                session=session,
            )
            logger.info(
                f"Set report status to REDO_ANALYSIS after refining visualization for report_id: {data.report_id}"
            )

            # Commit the changes
            await session.commit()

            refined_viz_url = (
                await get_or_create_presigned_url(new_viz_s3_path, redis_instance.redis_client)
                or new_viz_s3_path
            )

            return JSONResponse(
                status_code=200,
                content={
                    "success": True,
                    "message": "Visualization updated successfully",
                    "refined_viz": refined_viz_url,
                },
            )

    except Exception as e:
        logger.error(f"Error in refine_visualization: {e}")
        return JSONResponse(
            status_code=500,
            content={
                "success": False,
                "error": "Something went wrong while refining your visualization. Please try again.",
            },
        )


@router.post(
    "/delete-visualization",
    response_model=RefineOrDeleteResponse,
    status_code=200,
    responses={
        404: {"model": ErrorResponse},
        401: {"model": ErrorResponse},
        422: {"model": ErrorResponse},
        500: {"model": ErrorResponse},
    },
)
async def delete_visualization_endpoint(
    data: RefineOrDeleteRequest, user_id: str = Depends(get_current_active_user)
):
    """Delete visualization for a table in a report"""
    logger.info(f"Delete visualization request received for user_id: {user_id}")

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

        if not data.card.get("id"):
            logger.error("Missing required field: card.id")
            return JSONResponse(
                status_code=422,
                content={
                    "success": False,
                    "error": "It looks like some required information is missing. Please check your request and try again.",
                },
            )

        report_id = data.report_id
        card_id = data.card.get("id")
        subsection_data = data.card.get("subsection")

        # Handle different input formats for section vs subsection table deletion
        if subsection_data:
            # Subsection table deletion format
            subsection_id = subsection_data.get("id")
            table_id = subsection_data.get("table_id")

            if not subsection_id:
                logger.error("Missing required field: card.subsection.id")
                return JSONResponse(
                    status_code=422,
                    content={
                        "success": False,
                        "error": "It looks like some required information is missing. Please check your request and try again.",
                    },
                )

            if not table_id:
                logger.error("Missing required field: card.subsection.table_id")
                return JSONResponse(
                    status_code=422,
                    content={
                        "success": False,
                        "error": "It looks like some required information is missing. Please check your request and try again.",
                    },
                )
        else:
            # Section table deletion format
            table_id = data.card.get("table_id")
            subsection_id = None

            if not table_id:
                logger.error("Missing required field: card.table_id")
                return JSONResponse(
                    status_code=422,
                    content={"success": False, "error": "Missing required field: card.table_id"},
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
                        "error": "Something went wrong while deleting your visualization. Please try again.",
                    },
                )
            if not db_response.get("exists", False):
                logger.error(f"User with ID {user_id} not found or token is invalid")
                return JSONResponse(
                    status_code=401, content={"success": False, "error": "Unauthorized request"}
                )

            # Delete the visualization
            db_response = await delete_visualization(
                session=session,
                report_id=report_id,
                card_id=card_id,
                table_id=table_id,
                subsection_id=subsection_id,
            )

            if not db_response.get("success", False):
                error_type = db_response.get("error_type", "server_error")
                error_message = db_response.get(
                    "error",
                    "Something went wrong while deleting your visualization. Please try again.",
                )

                # Determine appropriate status code based on error type
                if error_type == "not_found":
                    status_code = 404
                    logger.warning(f"Resource not found: {error_message}")
                elif error_type == "invalid_data":
                    status_code = 422
                    logger.warning(f"Invalid data: {error_message}")
                else:
                    status_code = 500
                    logger.error(f"Server error: {error_message}")

                return JSONResponse(
                    status_code=status_code, content={"success": False, "error": error_message}
                )

            # Set status to REDO_ANALYSIS after deleting visualization
            await update_report_status_if_needed(
                report_id=report_id, new_status=ReportStatus.REDO_ANALYSIS.value, session=session
            )
            logger.info(
                f"Set report status to REDO_ANALYSIS after deleting visualization for report_id: {report_id}"
            )

            return JSONResponse(
                status_code=200,
                content={
                    "success": True,
                    "message": "Visualization deleted successfully",
                    "card_id": card_id,
                    "table_id": table_id,
                    "subsection_id": subsection_id,
                },
            )

    except Exception as e:
        logger.error(f"Error in delete_visualization_endpoint: {e}")
        return JSONResponse(
            status_code=500,
            content={
                "success": False,
                "error": "Something went wrong while deleting your visualization. Please try again.",
            },
        )
