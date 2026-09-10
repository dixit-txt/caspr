"""
subscription_update_validator.py
Validation and calculation logic for subscription plan updates with prorated refunds and fair token usage.
"""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import (
    SubscriptionStatus,
    TransactionSource,
)
from app.core.logging import setup_logging
from app.models import Subscription, SubscriptionInterval, TokenBatch, Wallet

logger = setup_logging(__file__)

# Allowed plan transition matrix
# Format: (old_tier, old_duration) -> [(new_tier, new_duration), ...]
ALLOWED_TRANSITIONS = {
    ("plus", "monthly"): [("plus", "yearly"), ("pro", "monthly"), ("pro", "yearly")],
    ("plus", "yearly"): [("pro", "yearly")],  # Removed pro/monthly - yearly to monthly not allowed
    ("pro", "monthly"): [("pro", "yearly")],
    ("pro", "yearly"): [],  # No downgrades allowed from pro yearly
}


def is_plan_transition_allowed(
    old_tier: str, old_duration: str, new_tier: str, new_duration: str
) -> tuple[bool, str]:
    """
    Validate if a plan transition is allowed based on the transition matrix.

    Args:
        old_tier: Current tier (plus/pro)
        old_duration: Current duration (monthly/yearly)
        new_tier: New tier (plus/pro)
        new_duration: New duration (monthly/yearly)

    Returns:
        Tuple of (is_allowed, error_message)
    """
    # Normalize inputs
    old_tier = old_tier.lower()
    old_duration = old_duration.lower()
    new_tier = new_tier.lower()
    new_duration = new_duration.lower()

    # Check if same plan
    if old_tier == new_tier and old_duration == new_duration:
        return (
            False,
            f"You are already subscribed to {new_tier.upper()} {new_duration.upper()}. No changes needed.",
        )

    # Get allowed transitions for current plan
    current_plan = (old_tier, old_duration)
    allowed = ALLOWED_TRANSITIONS.get(current_plan, [])

    # Check if new plan is in allowed list
    new_plan = (new_tier, new_duration)
    if new_plan not in allowed:
        return False, (
            f"Cannot change from {old_tier.upper()} {old_duration.upper()} to "
            f"{new_tier.upper()} {new_duration.upper()}. This transition is not allowed."
        )

    return True, ""


async def calculate_fair_token_usage(
    subscription_id: str, user_id: str, session: AsyncSession
) -> dict[str, Any]:
    """
    Calculate fair token usage based on time elapsed in the subscription period.

    Args:
        subscription_id: External subscription ID
        user_id: User ID
        session: Database session

    Returns:
        Dict containing:
        - total_tokens_allocated: Total tokens for the period
        - tokens_per_day: Daily token allocation
        - fair_usage_tokens: Expected tokens consumed based on days elapsed
        - actual_consumed_tokens: Actual tokens consumed
        - over_under_consumption: Positive if over-consumed, negative if under-consumed
    """
    logger.info(
        f"[CALCULATE_FAIR_TOKEN_USAGE] subscription_id={subscription_id}, user_id={user_id}"
    )

    try:
        # Get subscription
        result = await session.execute(
            select(Subscription).where(Subscription.subscription_id == subscription_id)
        )
        subscription = result.scalar_one_or_none()

        if not subscription:
            return {"success": False, "error": "Subscription not found"}

        # Get current active interval
        now = datetime.now(UTC)
        interval_result = await session.execute(
            select(SubscriptionInterval)
            .where(
                SubscriptionInterval.subscription_id == subscription.id,
                SubscriptionInterval.status == SubscriptionStatus.CHARGED,
                SubscriptionInterval.start_date <= now,
                SubscriptionInterval.end_date >= now,
            )
            .order_by(SubscriptionInterval.start_date.desc())
        )
        current_interval = interval_result.scalar_one_or_none()

        if not current_interval:
            return {"success": False, "error": "No active subscription interval found"}

        # Get plan details from metadata
        meta_data = subscription.meta_data or {}
        plan_duration = meta_data.get("plan_duration", "monthly").lower()
        tokens_per_month = meta_data.get("tokens_per_month", 0)

        # Get actual consumption from wallet
        # We need to get batches first to calculate total allocated tokens for yearly plans
        wallet_result = await session.execute(select(Wallet).where(Wallet.user_id == user_id))
        wallet = wallet_result.scalar_one_or_none()

        if not wallet:
            return {"success": False, "error": "Wallet not found"}

        # Get all token batches created for this subscription interval
        batch_result = await session.execute(
            select(TokenBatch).where(
                TokenBatch.wallet_id == wallet.id,
                TokenBatch.source_type == TransactionSource.SUBSCRIPTION,
                TokenBatch.payment_id == current_interval.payment_id,
            )
        )
        subscription_batches = batch_result.scalars().all()

        # Calculate total tokens allocated and actual consumption
        # For YEARLY plans: Only consider UNLOCKED batches (start_at <= now)
        #                   This accounts for multiple months of unlocked batches
        # For MONTHLY plans: Consider all batches from the interval
        if plan_duration == "yearly":
            # Only count unlocked batches for yearly plans
            unlocked_batches = [batch for batch in subscription_batches if batch.start_at <= now]
            initial_tokens = sum(batch.initial_tokens for batch in unlocked_batches)
            remaining_tokens = sum(batch.remaining_tokens for batch in unlocked_batches)
            # Total allocated = sum of all unlocked batch tokens
            # This handles cases where 2, 3, or more batches have unlocked
            total_tokens_allocated = initial_tokens
        else:
            # For monthly, count all batches
            initial_tokens = sum(batch.initial_tokens for batch in subscription_batches)
            remaining_tokens = sum(batch.remaining_tokens for batch in subscription_batches)
            # For monthly, use the full allocation
            total_tokens_allocated = current_interval.tokens_credited

        actual_consumed_tokens = initial_tokens - remaining_tokens

        # Calculate time-based values
        start_date = current_interval.start_date
        end_date = current_interval.end_date
        days_elapsed = (now - start_date).days
        total_days = (end_date - start_date).days

        # Calculate fair usage
        tokens_per_day = total_tokens_allocated / total_days if total_days > 0 else 0
        fair_usage_tokens = int(tokens_per_day * days_elapsed)

        # Calculate over/under consumption
        over_under_consumption = actual_consumed_tokens - fair_usage_tokens

        logger.info(
            f"[FAIR_TOKEN_USAGE_CALCULATED] subscription_id={subscription_id}, "
            f"total_allocated={total_tokens_allocated}, fair_usage={fair_usage_tokens}, "
            f"actual_consumed={actual_consumed_tokens}, over_under={over_under_consumption}"
        )

        return {
            "success": True,
            "total_tokens_allocated": total_tokens_allocated,
            "tokens_per_day": tokens_per_day,
            "fair_usage_tokens": fair_usage_tokens,
            "actual_consumed_tokens": actual_consumed_tokens,
            "over_under_consumption": over_under_consumption,
            "days_elapsed": days_elapsed,
            "total_days": total_days,
        }

    except Exception as e:
        logger.error(
            f"[FAIR_TOKEN_USAGE_ERROR] subscription_id={subscription_id}, error={e!s}",
            exc_info=True,
        )
        return {"success": False, "error": str(e)}


def calculate_adjusted_token_credit(
    new_plan_tier: str, new_plan_duration: str, over_under_consumption: int, tokens_per_month: int
) -> int:
    """
    Calculate adjusted token credit for the new plan based on over/under consumption.

    Args:
        new_plan_tier: New tier (plus/pro)
        new_plan_duration: New duration (monthly/yearly)
        over_under_consumption: Positive if over-consumed, negative if under-consumed
        tokens_per_month: Standard tokens per month for the new plan

    Returns:
        Adjusted token amount to credit
    """
    logger.info(
        f"[CALCULATE_ADJUSTED_CREDIT] new_tier={new_plan_tier}, "
        f"new_duration={new_plan_duration}, over_under={over_under_consumption}, "
        f"standard_tokens={tokens_per_month}"
    )

    # Standard token allocation for the new plan
    standard_tokens = tokens_per_month

    # Adjust based on over/under consumption
    # If over-consumed (positive), reduce the credit
    # If under-consumed (negative), increase the credit
    adjusted_tokens = standard_tokens - over_under_consumption

    # Ensure we don't go negative
    adjusted_tokens = max(0, adjusted_tokens)

    logger.info(
        f"[ADJUSTED_CREDIT_CALCULATED] standard={standard_tokens}, "
        f"adjusted={adjusted_tokens}, difference={standard_tokens - adjusted_tokens}"
    )

    return adjusted_tokens
