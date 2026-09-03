"""auth routes: profile.

Split out of ``app/auth/router.py`` to keep each router file under
the ~400-line ceiling in R-STRUCT-3. Handlers are unchanged.
"""

"""HTTP routes for the auth bounded context.

Handlers moved verbatim from ``src/resources/routers/api.py`` during the
R-STRUCT-1 migration, which split that 10,257-line module across seven
contexts. Handler bodies are unchanged; only the import block and the
``APIRouter`` they attach to are new.
"""

"""api.py: Authentication API for the Casper backend"""
import asyncio
from datetime import UTC, datetime

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from app.auth.repository import (
    check_user_by_id,
    get_user_walkover_status,
    mark_walkover_completed,
    update_user_tc_verified,
)
from app.auth.schemas import (
    CompleteWalkoverResponse,
    ErrorResponse,
    GetTCVerifiedResponse,
    GetWalkoverStatusResponse,
    UpdateTCVerifiedRequest,
    UpdateTCVerifiedResponse,
)
from app.auth.token import (
    get_current_active_user,
)
from app.core.db import async_session_scope
from app.core.logging import setup_logging
from app.core.redis import get_redis_instance
from app.observability.cloudwatch_utils import insert_cloudwatch_logs

router = APIRouter()
logger = setup_logging(__file__)
redis_instance = get_redis_instance()


@router.patch(
    "/update-tc-verified",
    response_model=UpdateTCVerifiedResponse,
    responses={
        401: {"model": ErrorResponse},
        422: {"model": ErrorResponse},
        500: {"model": ErrorResponse},
    },
)
async def update_tc_verified(
    data: UpdateTCVerifiedRequest, user_id: str = Depends(get_current_active_user)
):
    """
    Update user's Terms and Conditions verification status.
    This endpoint allows updating the t_c_verified field for a user.
    """
    logger.info(f"Update T&C verification request received for user_id: {user_id}")

    try:
        async with async_session_scope() as session:
            # Verify user exists
            db_response = await check_user_by_id(user_id=user_id, session=session)

            if not db_response.get("success", False):
                logger.error(f"Failed to check user: {db_response.get('error')}")
                return JSONResponse(
                    status_code=500,
                    content={
                        "success": False,
                        "error": "Something went wrong while updating your preferences. Please try again.",
                    },
                )

            if not db_response.get("exists", False):
                logger.error(f"User with ID {user_id} not found")
                return JSONResponse(
                    status_code=401,
                    content={"success": False, "error": "User not found or session expired"},
                )

            # Update t_c_verified status
            update_response = await update_user_tc_verified(
                user_id=user_id, t_c_verified=data.t_c_verified, session=session
            )

            if not update_response.get("success", False):
                logger.error(f"Failed to update T&C status: {update_response.get('error')}")
                return JSONResponse(
                    status_code=500,
                    content={
                        "success": False,
                        "error": "Something went wrong while updating your preferences. Please try again.",
                    },
                )

            # Log to CloudWatch
            cloudwatch_data = {
                "event": "T&C Verification Updated",
                "event_success": True,
                "t_c_verified": data.t_c_verified,
                "timestamp": datetime.now(UTC).isoformat(),
            }
            try:
                asyncio.create_task(insert_cloudwatch_logs(data=cloudwatch_data, user_id=user_id))
            except Exception as e:
                logger.error(f"Error in inserting cloudwatch logs for user_id: {user_id}: {e}")

            logger.info(
                f"T&C verification status updated successfully for user_id: {user_id} to {data.t_c_verified}"
            )

            return UpdateTCVerifiedResponse(
                success=True,
                message="Terms and Conditions verification status updated successfully",
                t_c_verified=data.t_c_verified,
            )

    except Exception as e:
        logger.error(f"Error in update_tc_verified: {e}")
        return JSONResponse(
            status_code=500,
            content={
                "success": False,
                "error": "Something went wrong while updating your preferences. Please try again.",
            },
        )


@router.get(
    "/get-tc-verified",
    response_model=GetTCVerifiedResponse,
    responses={401: {"model": ErrorResponse}, 500: {"model": ErrorResponse}},
)
async def get_tc_verified(user_id: str = Depends(get_current_active_user)):
    """
    Get user's Terms and Conditions verification status.
    Returns the current t_c_verified status from the users table.
    """
    logger.info(f"Get T&C verification request received for user_id: {user_id}")

    try:
        async with async_session_scope() as session:
            # Get user details
            db_response = await check_user_by_id(user_id=user_id, session=session)

            if not db_response.get("success", False):
                logger.error(f"Failed to check user: {db_response.get('error')}")
                return JSONResponse(
                    status_code=500,
                    content={
                        "success": False,
                        "error": "Something went wrong while retrieving your status. Please try again.",
                    },
                )

            if not db_response.get("exists", False):
                logger.error(f"User with ID {user_id} not found")
                return JSONResponse(
                    status_code=401,
                    content={"success": False, "error": "User not found or session expired"},
                )

            # Get t_c_verified status
            t_c_verified = db_response.get("t_c_verified", False)

            logger.info(
                f"T&C verification status retrieved successfully for user_id: {user_id}, status: {t_c_verified}"
            )

            return GetTCVerifiedResponse(success=True, t_c_verified=t_c_verified)

    except Exception as e:
        logger.error(f"Error in get_tc_verified: {e}")
        return JSONResponse(
            status_code=500,
            content={
                "success": False,
                "error": "Something went wrong while retrieving your status. Please try again.",
            },
        )


@router.get(
    "/get-walkover-status",
    response_model=GetWalkoverStatusResponse,
    responses={401: {"model": ErrorResponse}, 500: {"model": ErrorResponse}},
)
async def get_walkover_status(user_id: str = Depends(get_current_active_user)):
    """Return whether the authenticated user has finished the in-app walkover.

    Defaults to ``False`` from account creation and stays that way until the
    frontend calls ``POST /complete-walkover`` after the user finishes the
    walkthrough. Frontend should call this on every authenticated session and
    show the walkover whenever it is ``False``.
    """
    logger.info(f"Get walkover-status request received for user_id: {user_id}")

    try:
        async with async_session_scope() as session:
            db_response = await get_user_walkover_status(user_id=user_id, session=session)

            if not db_response.get("success", False):
                logger.error(
                    f"Failed to fetch walkover_completed for user_id: {user_id}: "
                    f"{db_response.get('error')}"
                )
                return JSONResponse(
                    status_code=500,
                    content={
                        "success": False,
                        "error": "Something went wrong while retrieving your status. Please try again.",
                    },
                )

            if not db_response.get("exists", False):
                logger.warning(f"User with ID {user_id} not found while reading walkover_completed")
                return JSONResponse(
                    status_code=401,
                    content={"success": False, "error": "User not found or session expired"},
                )

            walkover_completed = bool(db_response.get("walkover_completed", False))
            logger.info(
                f"walkover_completed retrieved successfully for user_id: {user_id}, "
                f"walkover_completed: {walkover_completed}"
            )
            return GetWalkoverStatusResponse(success=True, walkover_completed=walkover_completed)

    except Exception as e:
        logger.error(f"Error in get_walkover_status: {e}")
        return JSONResponse(
            status_code=500,
            content={
                "success": False,
                "error": "Something went wrong while retrieving your status. Please try again.",
            },
        )


@router.post(
    "/complete-walkover",
    response_model=CompleteWalkoverResponse,
    responses={401: {"model": ErrorResponse}, 500: {"model": ErrorResponse}},
)
async def complete_walkover(user_id: str = Depends(get_current_active_user)):
    """Mark the authenticated user's walkover as completed.

    Idempotent — calling this multiple times is safe; the flag will simply
    stay ``True``. The frontend should hit this once the user finishes (or
    explicitly skips) the in-app walkthrough.
    """
    logger.info(f"Complete walkover request received for user_id: {user_id}")

    try:
        async with async_session_scope() as session:
            db_response = await mark_walkover_completed(user_id=user_id, session=session)

            if not db_response.get("success", False):
                error = db_response.get("error", "")
                if error == "User not found":
                    logger.warning(
                        f"User with ID {user_id} not found while marking walkover_completed"
                    )
                    return JSONResponse(
                        status_code=401,
                        content={"success": False, "error": "User not found or session expired"},
                    )
                logger.error(f"Failed to mark walkover_completed for user_id: {user_id}: {error}")
                return JSONResponse(
                    status_code=500,
                    content={
                        "success": False,
                        "error": "Something went wrong while updating your status. Please try again.",
                    },
                )

            logger.info(f"walkover_completed marked True for user_id: {user_id}")
            return CompleteWalkoverResponse(
                success=True,
                message="Walkover marked as completed.",
                walkover_completed=True,
            )

    except Exception as e:
        logger.error(f"Error in complete_walkover: {e}")
        return JSONResponse(
            status_code=500,
            content={
                "success": False,
                "error": "Something went wrong while updating your status. Please try again.",
            },
        )
