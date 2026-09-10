"""
referral_functions.py
Database functions for referral operations.
"""

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from uuid_utils import uuid7

from app.core.constants import TOKEN_EXPIRY_DAYS
from app.core.enums import TransactionSource, TransactionStatus
from app.core.logging import setup_logging
from app.models import Referral, TokenBatch, User, Wallet

logger = setup_logging(__file__)


async def get_user_by_referral_code(referral_code: str, session: AsyncSession) -> User | None:
    """
    Find user by referral code.

    Args:
        referral_code: The referral code to search for (case-insensitive)
        session: Database session

    Returns:
        User object if found, None otherwise
    """
    try:
        # Convert to uppercase for case-insensitive matching
        referral_code_upper = referral_code.upper().strip()

        result = await session.execute(
            select(User).where(func.upper(User.referral_code) == referral_code_upper)
        )
        user = result.scalar_one_or_none()

        if user:
            logger.info(f"[REFERRAL_CODE_FOUND] code={referral_code_upper}, user_id={user.id}")
        else:
            logger.warning(f"[REFERRAL_CODE_NOT_FOUND] code={referral_code_upper}")

        return user

    except SQLAlchemyError as e:
        logger.error(
            f"[REFERRAL_CODE_LOOKUP_ERROR] code={referral_code}, error={e!s}", exc_info=True
        )
        return None


async def check_user_already_referred(user_id: str, session: AsyncSession) -> bool:
    """
    Check if user has already used a referral code (has a referral record as referee).

    Args:
        user_id: User ID to check
        session: Database session

    Returns:
        True if user was already referred, False otherwise
    """
    try:
        result = await session.execute(select(Referral).where(Referral.referee_id == user_id))
        referral = result.scalar_one_or_none()

        is_referred = referral is not None

        if is_referred:
            logger.info(f"[USER_ALREADY_REFERRED] user_id={user_id}, referral_id={referral.id}")

        return is_referred

    except SQLAlchemyError as e:
        logger.error(f"[CHECK_REFERRED_ERROR] user_id={user_id}, error={e!s}", exc_info=True)
        return True  # Return True on error to prevent abuse


async def create_referral_record(
    referrer_id: str,
    referee_id: str,
    referral_code: str,
    session: AsyncSession,
    tokens_to_referrer: int = 25000,
    tokens_to_referee: int = 25000,
) -> dict[str, Any]:
    """
    Create referral record after successful signup.

    Args:
        referrer_id: User ID of the referrer (who gave the code)
        referee_id: User ID of the referee (who used the code)
        referral_code: The referral code that was used
        session: Database session
        tokens_to_referrer: Tokens credited to referrer (default 25,000)
        tokens_to_referee: Tokens credited to referee (default 25,000)

    Returns:
        Dict with success status and referral ID
    """
    try:
        logger.info(
            f"[CREATE_REFERRAL_RECORD_START] referrer_id={referrer_id}, "
            f"referee_id={referee_id}, code={referral_code}"
        )

        # Create referral record
        referral = Referral(
            id=str(uuid7()),
            referrer_id=referrer_id,
            referee_id=referee_id,
            referral_code_used=referral_code.upper(),
            tokens_credited_to_referrer=tokens_to_referrer,
            tokens_credited_to_referee=tokens_to_referee,
            status="completed",
            created_at=datetime.now(UTC),
        )

        session.add(referral)
        await session.flush()

        logger.info(
            f"[CREATE_REFERRAL_RECORD_SUCCESS] referral_id={referral.id}, "
            f"referrer_id={referrer_id}, referee_id={referee_id}"
        )

        return {
            "success": True,
            "referral_id": referral.id,
            "referrer_id": referrer_id,
            "referee_id": referee_id,
        }

    except SQLAlchemyError as e:
        logger.error(
            f"[CREATE_REFERRAL_RECORD_ERROR] referrer_id={referrer_id}, "
            f"referee_id={referee_id}, error={e!s}",
            exc_info=True,
        )
        return {"success": False, "error": f"Database error creating referral record: {e!s}"}


async def get_referral_stats(user_id: str, session: AsyncSession) -> dict[str, Any]:
    """
    Get referral statistics for a user.

    Args:
        user_id: User ID
        session: Database session

    Returns:
        Dict with referral statistics:
        - total_referrals: Number of successful referrals
        - total_tokens_earned: Total tokens earned from referrals
        - referral_code: User's referral code
    """
    try:
        logger.info(f"[GET_REFERRAL_STATS_START] user_id={user_id}")

        # Get user's referral code
        user_result = await session.execute(select(User.referral_code).where(User.id == user_id))
        user_code = user_result.scalar_one_or_none()

        if not user_code:
            logger.warning(f"[GET_REFERRAL_STATS_USER_NOT_FOUND] user_id={user_id}")
            return {
                "success": False,
                "error": "User not found",
                "total_referrals": 0,
                "total_tokens_earned": 0,
                "referral_code": None,
            }

        # Count total successful referrals
        count_result = await session.execute(
            select(func.count(Referral.id)).where(
                Referral.referrer_id == user_id, Referral.status == "completed"
            )
        )
        total_referrals = count_result.scalar_one() or 0

        # Sum total tokens earned from referrals
        tokens_result = await session.execute(
            select(func.sum(Referral.tokens_credited_to_referrer)).where(
                Referral.referrer_id == user_id, Referral.status == "completed"
            )
        )
        total_tokens_earned = tokens_result.scalar_one() or 0

        logger.info(
            f"[GET_REFERRAL_STATS_SUCCESS] user_id={user_id}, "
            f"total_referrals={total_referrals}, total_tokens={total_tokens_earned}"
        )

        return {
            "success": True,
            "total_referrals": total_referrals,
            "total_tokens_earned": total_tokens_earned,
            "referral_code": user_code,
        }

    except SQLAlchemyError as e:
        logger.error(f"[GET_REFERRAL_STATS_ERROR] user_id={user_id}, error={e!s}", exc_info=True)
        return {
            "success": False,
            "error": f"Database error: {e!s}",
            "total_referrals": 0,
            "total_tokens_earned": 0,
            "referral_code": None,
        }


async def credit_referral_bonus(
    user_id: str, tokens: int, reason: str, session: AsyncSession
) -> dict[str, Any]:
    """
    Credit referral bonus tokens to user's wallet.
    Creates a new token batch with referral bonus tokens.

    Args:
        user_id: User ID to credit tokens to
        tokens: Number of tokens to credit
        reason: Reason for the credit ("referral_bonus_given" or "referral_bonus_received")
        session: Database session

    Returns:
        Dict with success status and batch ID
    """
    try:
        logger.info(
            f"[CREDIT_REFERRAL_BONUS_START] user_id={user_id}, tokens={tokens}, reason={reason}"
        )

        # Get user's wallet ID
        wallet_result = await session.execute(select(Wallet.id).where(Wallet.user_id == user_id))
        wallet_id = wallet_result.scalar_one_or_none()

        if not wallet_id:
            logger.error(f"[CREDIT_REFERRAL_BONUS_NO_WALLET] user_id={user_id}")
            return {"success": False, "error": "Wallet not found for user"}

        # Create token batch for referral bonus
        now = datetime.now(UTC)
        batch_id = str(uuid7())

        token_batch = TokenBatch(
            id=batch_id,
            wallet_id=wallet_id,
            initial_tokens=tokens,
            remaining_tokens=tokens,
            reserved_tokens=0,
            amount=None,  # Free bonus
            currency=None,
            source_type=TransactionSource.SIGNUP_BONUS,  # Using signup_bonus type for referral bonuses
            payment_id=None,
            start_at=now,
            expires_at=now + timedelta(days=TOKEN_EXPIRY_DAYS),
            status=TransactionStatus.COMPLETED,
            meta_data={"reason": reason, "referral_bonus": True, "created_at": now.isoformat()},
        )

        session.add(token_batch)
        await session.flush()

        # Sync wallet balance to reflect new tokens
        from app.wallet.repository import _sync_wallet_balances

        await _sync_wallet_balances(wallet_id, session)

        logger.info(
            f"[CREDIT_REFERRAL_BONUS_SUCCESS] user_id={user_id}, "
            f"tokens={tokens}, batch_id={batch_id}"
        )

        return {
            "success": True,
            "batch_id": batch_id,
            "tokens_credited": tokens,
            "wallet_id": wallet_id,
        }

    except SQLAlchemyError as e:
        logger.error(
            f"[CREDIT_REFERRAL_BONUS_ERROR] user_id={user_id}, tokens={tokens}, error={e!s}",
            exc_info=True,
        )
        return {"success": False, "error": f"Database error crediting tokens: {e!s}"}


async def get_user_referral_code(user_id: str, session: AsyncSession) -> str | None:
    """
    Get user's referral code.

    Args:
        user_id: User ID
        session: Database session

    Returns:
        Referral code string if found, None otherwise
    """
    try:
        result = await session.execute(select(User.referral_code).where(User.id == user_id))
        return result.scalar_one_or_none()

    except SQLAlchemyError as e:
        logger.error(
            f"[GET_USER_REFERRAL_CODE_ERROR] user_id={user_id}, error={e!s}", exc_info=True
        )
        return None
