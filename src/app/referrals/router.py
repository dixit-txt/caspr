"""HTTP routes for the referrals bounded context.

Handlers moved verbatim from ``src/resources/routers/wallet_api.py``
during the R-STRUCT-1 migration, which split that module across wallet,
billing, and referrals.
"""

"""
wallet_api.py: REST API endpoints for wallet operations.
"""

import time

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse

from app.auth.token import get_current_active_user
from app.core.constants import FRONTEND_URL
from app.core.db import async_session_scope
from app.core.logging import setup_logging
from app.referrals.schemas import ReferralErrorResponse, ReferralInfoResponse
from app.wallet.schemas import WalletErrorResponse

# Preserved as /wallet/referral for the same reason as billing: the path is
# part of the live contract, and this migration does not change contracts.
router = APIRouter(prefix="/wallet", tags=["Referrals"])
logger = setup_logging(__file__)


@router.get(
    "/referral",
    response_model=ReferralInfoResponse,
    responses={
        401: {"model": WalletErrorResponse},
        404: {"model": ReferralErrorResponse},
        500: {"model": ReferralErrorResponse},
    },
    summary="Get referral information",
    description="Get user's referral code and statistics including total referrals and tokens earned",
)
async def get_referral_info(user_id: str = Depends(get_current_active_user)):
    """
    Get referral code and stats for authenticated user.

    Returns:
        - referral_code: User's unique referral code
        - total_referrals: Number of successful referrals
        - total_tokens_earned: Total tokens earned from referrals
        - referral_link: Complete referral link to share
    """
    start_time = time.time()
    logger.info(f"[REFERRAL_INFO_REQUEST] user_id={user_id}")

    try:
        async with async_session_scope() as session:
            from app.auth.repository import (
                get_user_details,
            )
            from app.referrals.repository import get_referral_stats, get_user_referral_code

            # Get user details to verify user exists
            user_details = await get_user_details(user_id, session)
            if not user_details.get("success"):
                logger.error(f"[REFERRAL_INFO_USER_NOT_FOUND] user_id={user_id}")
                return JSONResponse(
                    status_code=404, content={"success": False, "error": "User not found"}
                )

            # Get user's referral code
            referral_code = await get_user_referral_code(user_id, session)
            if not referral_code:
                logger.error(f"[REFERRAL_INFO_CODE_NOT_FOUND] user_id={user_id}")
                return JSONResponse(
                    status_code=404,
                    content={"success": False, "error": "Referral code not found for user"},
                )

            # Get referral stats
            stats = await get_referral_stats(user_id, session)

            if not stats.get("success"):
                logger.error(
                    f"[REFERRAL_INFO_STATS_FAILED] user_id={user_id}, error={stats.get('error')}"
                )
                return JSONResponse(
                    status_code=500,
                    content={"success": False, "error": "Failed to retrieve referral statistics"},
                )

            # Build referral link (adjust frontend URL as needed)
            # TODO: Get frontend URL from environment variable
            frontend_url = FRONTEND_URL  # Replace with actual frontend URL
            referral_link = f"{frontend_url}/signup?ref={referral_code}"

            elapsed_time = time.time() - start_time
            logger.info(
                f"[REFERRAL_INFO_SUCCESS] user_id={user_id}, "
                f"code={referral_code}, total_referrals={stats['total_referrals']}, "
                f"total_tokens={stats['total_tokens_earned']}, "
                f"elapsed_time={elapsed_time:.2f}s"
            )

            return ReferralInfoResponse(
                success=True,
                referral_code=referral_code,
                total_referrals=stats["total_referrals"],
                total_tokens_earned=stats["total_tokens_earned"],
                referral_link=referral_link,
            )

    except HTTPException:
        raise
    except Exception as e:
        logger.critical(
            f"[REFERRAL_INFO_CRITICAL_ERROR] user_id={user_id}, "
            f"error={e!s}, error_type={type(e).__name__}, "
            f"elapsed_time={time.time() - start_time:.2f}s",
            exc_info=True,
        )
        return JSONResponse(
            status_code=500,
            content={
                "success": False,
                "error": "An unexpected error occurred while retrieving referral information. Please try again later.",
            },
        )
