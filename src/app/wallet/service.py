"""
wallet_service.py
Business logic service for wallet operations.
"""

import time
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from dateutil.parser import parse
from sqlalchemy.ext.asyncio import AsyncSession
from uuid_utils import uuid7

from app.adapters.email import send_payment_notification_email
from app.core.constants import (
    CASPR_PAYMENT_API_KEY,
    CASPR_PAYMENT_BASE_URL,
    COUNTRY_TO_CURRENCY,
    DEFAULT_SUBSCRIPTION_TOTAL_COUNT,
    REPORT_GENERATION_COST,
    SIGNUP_BONUS_TOKENS,
    SUBSCRIPTION_INFO,
    TAX_INFO,
    TOKEN_PRICE_INFO,
)
from app.core.enums import (
    SubscriptionStatus,
    SubscriptionTier,
    TransactionSource,
    TransactionStatus,
    TransactionType,
)
from app.core.logging import setup_logging
from app.models import SubscriptionInterval, TokenTransaction, User
from app.wallet.repository import (
    check_existing_token_batch,
    confirm_debit,
    confirm_token_batch,
    create_pending_subscription,
    create_token_batch,
    create_wallet,
    fail_token_batch,
    get_active_subscription,
    get_subscription_by_subscription_id,
    get_subscription_history,
    get_token_batch_by_payment_id,
    get_transaction_history,
    get_user_email,
    get_wallet_balance,
    get_wallet_by_user_id,
    get_wallet_id_by_user_id,
    process_subscription_charge,
    release_reservation,
    reserve_tokens_fifo,
    update_subscription_metadata,
)
from app.wallet.schemas import SubscriptionWebhookEvent
from app.wallet.service_payment import PaymentService

logger = setup_logging(__file__)


async def _fire_payment_email(event_type: str, payment_data: dict) -> None:
    """Send payment notification email off the event loop via a thread pool."""
    from fastapi.concurrency import run_in_threadpool

    try:
        await run_in_threadpool(
            send_payment_notification_email,
            event_type=event_type,
            payment_data=payment_data,
        )
    except Exception:
        logger.warning(
            f"[PAYMENT_EMAIL] Failed to send notification for event={event_type}",
            exc_info=True,
        )


class WalletService:
    """High-level service for wallet operations."""

    @staticmethod
    async def initialize_wallet(
        user_id: str, session: AsyncSession, bonus_tokens: int = 0, bonus_reason: str = None
    ) -> dict[str, Any]:
        """
        Initialize a new wallet for a user with signup bonus + optional referral bonus.

        Args:
            user_id: User ID
            session: DB session
            bonus_tokens: Additional bonus tokens (e.g., 25000 for referral)
            bonus_reason: Reason for bonus ("referral_bonus_received")

        Returns:
            Dict with success status and wallet details
        """
        total_tokens = SIGNUP_BONUS_TOKENS + bonus_tokens
        logger.info(
            f"[WALLET_INIT_START] user_id={user_id}, "
            f"signup_bonus={SIGNUP_BONUS_TOKENS}, bonus_tokens={bonus_tokens}, "
            f"total={total_tokens}, reason={bonus_reason}"
        )

        try:
            # Validate user_id
            if not user_id or not isinstance(user_id, str):
                logger.error(f"[WALLET_INIT_INVALID_USER_ID] user_id={user_id}")
                return {"success": False, "error": "Invalid user ID"}

            # Create wallet with base signup bonus
            result = await create_wallet(user_id=user_id, session=session)

            if not result.get("success"):
                error_msg = result.get("error", "Unknown error")
                logger.error(f"[WALLET_INIT_FAILED] user_id={user_id}, error={error_msg}")
                return result

            # If referral bonus, credit additional tokens
            if bonus_tokens > 0:
                from app.referrals.repository import credit_referral_bonus

                bonus_result = await credit_referral_bonus(
                    user_id=user_id,
                    tokens=bonus_tokens,
                    reason=bonus_reason or "referral_bonus",
                    session=session,
                )

                if not bonus_result.get("success"):
                    logger.error(
                        f"[WALLET_INIT_BONUS_FAILED] user_id={user_id}, "
                        f"bonus_tokens={bonus_tokens}, error={bonus_result.get('error')}"
                    )
                    # Don't fail the entire wallet initialization if bonus fails
                    # User still gets base signup bonus
                else:
                    logger.info(
                        f"[WALLET_INIT_BONUS_SUCCESS] user_id={user_id}, "
                        f"bonus_tokens={bonus_tokens}, batch_id={bonus_result.get('batch_id')}"
                    )

            logger.info(
                f"[WALLET_INIT_SUCCESS] user_id={user_id}, "
                f"wallet_id={result.get('wallet_id', 'N/A')}, "
                f"base_tokens={result.get('tokens_credited', 0)}, "
                f"bonus_tokens={bonus_tokens}, "
                f"total_tokens={total_tokens}"
            )

            return result

        except Exception as e:
            logger.critical(
                f"[WALLET_INIT_EXCEPTION] user_id={user_id}, error={e!s}", exc_info=True
            )
            return {"success": False, "error": "Failed to initialize wallet"}

    @staticmethod
    async def get_wallet(user_id: str, session: AsyncSession) -> dict[str, Any]:
        """Get wallet details for a user."""
        return await get_wallet_by_user_id(user_id, session)

    @staticmethod
    async def get_balance(user_id: str, session: AsyncSession) -> dict[str, Any]:
        """Get current wallet balance for a user."""
        # Get wallet balance
        balance_result = await get_wallet_balance(user_id, session)

        if not balance_result.get("success"):
            return balance_result

        # Get subscription information
        subscription_result = await get_active_subscription(session, user_id=user_id)

        subscription_tier = None
        subscription_expires_at = None

        if subscription_result.get("success") and subscription_result.get("subscription"):
            subscription_data = subscription_result.get("subscription", {})
            subscription_tier = subscription_data.get("current_tier")

            # Try to get expiry from meta_data first, then from current_interval
            meta_data = subscription_data.get("meta_data", {})
            if meta_data and "next_payment_datetime" in meta_data:
                subscription_expires_at = meta_data.get("next_payment_datetime")
            else:
                # Fallback to current_interval end_date if available
                current_interval = subscription_result.get("current_interval")
                if current_interval and "end_date" in current_interval:
                    subscription_expires_at = current_interval.get("end_date")

        # Merge subscription info into balance result
        balance_result["subscription_tier"] = subscription_tier
        balance_result["subscription_expires_at"] = subscription_expires_at

        return balance_result

    @staticmethod
    async def has_sufficient_balance(
        user_id: str, tokens_needed: int, session: AsyncSession
    ) -> dict[str, Any]:
        """Check if user has sufficient token balance."""
        balance = await get_wallet_balance(user_id, session)
        if not balance.get("success"):
            return balance
        available = balance.get("available_balance", 0)
        has_balance = available >= tokens_needed
        return {
            "success": True,
            "has_balance": has_balance,
            "available": available,
            "required": tokens_needed,
            "shortfall": max(0, tokens_needed - available),
            "wallet_id": balance.get("wallet_id"),
        }

    @staticmethod
    async def check_and_reserve(
        user_id: str,
        session: AsyncSession,
        report_id: str | None = None,
        tokens_needed: int = REPORT_GENERATION_COST,
    ) -> dict[str, Any]:
        """Check balance and reserve tokens for report generation."""
        logger.info(f"Check and reserve for user_id: {user_id}, tokens: {tokens_needed}")

        balance_check = await WalletService.has_sufficient_balance(user_id, tokens_needed, session)
        if not balance_check.get("success"):
            return balance_check

        if not balance_check.get("has_balance"):
            logger.warning(f"Insufficient balance for user_id: {user_id}")
            return {
                "success": False,
                "error": "Insufficient token balance",
                "error_code": "INSUFFICIENT_BALANCE",
                "available": balance_check.get("available"),
                "required": balance_check.get("required"),
                "shortfall": balance_check.get("shortfall"),
            }

        return await reserve_tokens_fifo(
            user_id=user_id,
            tokens_needed=tokens_needed,
            session=session,
            report_id=report_id,
            wallet_id=balance_check.get("wallet_id"),
        )

    @staticmethod
    async def confirm_report_debit(report_id: str, session: AsyncSession) -> dict[str, Any]:
        """Confirm token debit after successful report generation."""
        logger.info(f"Confirming debit for report_id: {report_id}")
        return await confirm_debit(report_id, session)

    @staticmethod
    async def release_report_reservation(
        report_id: str, session: AsyncSession, reason: str = "generation_failed"
    ) -> dict[str, Any]:
        """Release token reservation after failed report generation."""
        logger.info(f"Releasing reservation: {report_id}, reason: {reason}")
        return await release_reservation(report_id, session, reason)

    # ========================================================================
    # Subscription Management
    # ========================================================================

    # @staticmethod
    # async def subscribe(
    #     user_id: str,
    #     tier: str,
    #     payment_id: str,
    #     session: AsyncSession,
    #     amount_usd: Optional[float] = None
    # ) -> Dict[str, Any]:
    #     """Create a new subscription for a user."""
    #     logger.info(f"Creating subscription for user_id: {user_id}, tier: {tier}")

    # if tier not in [SubscriptionTier.PLUS.value, SubscriptionTier.PRO.value]:
    #     return {"success": False, "error": "Invalid tier. Must be 'plus' or 'pro'"}

    # wallet_id = await get_wallet_id_by_user_id(user_id, session)
    # if not wallet_id:
    #     return {"success": False, "error": "Wallet not found"}

    # tokens_to_credit = WalletService.TIER_MONTHLY_TOKENS.get(tier, 0)

    # return await create_subscription_interval(
    #     user_id=user_id,
    #     tier=tier,
    #     session=session,
    #     payment_id=payment_id,
    #     tokens_to_credit=tokens_to_credit
    # )

    @staticmethod
    async def get_active_subscription(user_id: str, session: AsyncSession) -> dict[str, Any]:
        """Get the active subscription for a user."""
        return await get_active_subscription(session, user_id=user_id)

    @staticmethod
    async def get_subscription_history(
        user_id: str, session: AsyncSession, limit: int = 10
    ) -> dict[str, Any]:
        """Get subscription history for a user."""
        return await get_subscription_history(user_id, session, limit)

    @staticmethod
    async def create_pending_subscription(
        user_id: str,
        tier: str,
        subscription_id: str,
        session: AsyncSession,
        amount: float,
        tokens_per_month: int,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Create pending subscription record before payment."""
        logger.info(
            f"Creating pending subscription: user_id={user_id}, tier={tier}, external_id={subscription_id}"
        )
        result = await create_pending_subscription(
            user_id=user_id,
            tier=tier,
            subscription_id=subscription_id,
            session=session,
            amount=amount,
            tokens_per_month=tokens_per_month,
            metadata=metadata,
        )
        if result.get("success"):
            # Ensure return keys match expected generalized naming
            return {
                "success": True,
                "subscription_id": result.get("subscription_id"),
                "external_subscription_id": result.get("external_subscription_id"),
            }
        return result

    @staticmethod
    async def create_subscription_order(
        idempotency_key: str,
        plan_tier: str,
        plan_duration: str,
        country: str,
        user_data: dict[str, Any],
    ) -> dict[str, Any]:
        """Call payment service to create a subscription."""

        logger.info(
            f"Creating subscription (via PaymentService): plan={plan_tier}, duration={plan_duration}"
        )

        return await PaymentService.create_subscription(
            idempotency_key=idempotency_key,
            plan_tier=plan_tier,
            plan_duration=plan_duration,
            country=country,
            user_data=user_data,
            total_count=DEFAULT_SUBSCRIPTION_TOTAL_COUNT,
        )

    @staticmethod
    async def change_subscription_plan(
        user_id: str,
        new_plan_tier: str,
        new_plan_duration: str,
        idempotency_key: str,
        session: AsyncSession,
    ) -> dict[str, Any]:
        """
        Change existing subscription plan (tier/duration) with validation and prorated calculations.
        Called when to_update=True on /subscribe.

        Flow:
        1. Validate plan transition is allowed
        2. Calculate prorated usage and fair token consumption
        3. Store calculation metadata
        4. Call payment service to update subscription
        5. SUBSCRIPTION_UPDATED webhook credits adjusted tokens
        """
        from app.billing.service_validation import (
            calculate_fair_token_usage,
            is_plan_transition_allowed,
        )

        logger.info(
            f"[SUBSCRIBE_CHANGE_PLAN_START] user_id={user_id}, "
            f"new_tier={new_plan_tier}, new_duration={new_plan_duration}"
        )

        try:
            # 1. Get active subscription
            active_subscription = await get_active_subscription(session, user_id=user_id)
            if not active_subscription.get("success") or not active_subscription.get(
                "subscription"
            ):
                logger.error(f"[SUBSCRIBE_CHANGE_PLAN_NO_ACTIVE] user_id={user_id}")
                return {
                    "success": False,
                    "error": "No active subscription found. Please subscribe first.",
                    "status_code": 404,
                }

            subscription_data = active_subscription.get("subscription", {})
            current_tier = subscription_data.get("current_tier")
            external_sub_id = subscription_data.get("external_subscription_id")
            internal_sub_id = subscription_data.get("id")
            meta_data = subscription_data.get("meta_data", {})
            current_duration = (meta_data.get("plan_duration") or "monthly").lower()

            logger.info(
                f"[SUBSCRIBE_CHANGE_PLAN_CURRENT] user_id={user_id}, "
                f"current_tier={current_tier}, current_duration={current_duration}, "
                f"external_sub_id={external_sub_id}"
            )

            # 2. Validate plan transition
            is_allowed, error_message = is_plan_transition_allowed(
                current_tier, current_duration, new_plan_tier, new_plan_duration
            )

            if not is_allowed:
                logger.warning(
                    f"[SUBSCRIBE_CHANGE_PLAN_NOT_ALLOWED] user_id={user_id}, "
                    f"from={current_tier}_{current_duration}, to={new_plan_tier}_{new_plan_duration}"
                )
                return {"success": False, "error": error_message, "status_code": 400}

            # Handle all allowed plan changes with fair-use calculation
            # Supports:
            # - Same-tier duration changes: Plus Monthly → Plus Yearly, Pro Monthly → Pro Yearly
            # - Tier upgrades: Plus Monthly → Pro Monthly/Yearly, Plus Yearly → Pro Monthly/Yearly
            #
            # Fair usage calculation aligns token adjustments with financial proration:
            # - Payment gateway gives prorated refund for unused days in current cycle
            # - Example: User switches 10 days into a 30-day cycle
            #   - Fair usage: (tokens_allocated / 30) * 10 = expected consumption
            #   - If user consumed less: under-consumed → add tokens back
            #   - If user consumed more: over-consumed → deduct tokens
            #   - Without this, we'd give too many tokens and be at a financial loss

            # 3. Calculate fair token usage based on time elapsed
            token_usage_result = await calculate_fair_token_usage(external_sub_id, user_id, session)
            if not token_usage_result.get("success"):
                logger.error(
                    f"[SUBSCRIBE_CHANGE_PLAN_TOKEN_CALC_FAILED] user_id={user_id}, "
                    f"error={token_usage_result.get('error')}"
                )
                return {
                    "success": False,
                    "error": "Failed to calculate token usage",
                    "status_code": 500,
                }
            logger.info(
                f"[SUBSCRIBE_CHANGE_PLAN_FAIR_USAGE_CALCULATED] user_id={user_id}, "
                f"from={current_tier}_{current_duration}, to={new_plan_tier}_{new_plan_duration}, "
                f"over_under={token_usage_result.get('over_under_consumption')}"
            )

            # 4. Invalidate old subscription batches and get reserved tokens
            from app.wallet.repository import (
                get_wallet_id_by_user_id,
                invalidate_subscription_batches_and_get_reserved,
            )

            wallet_id = await get_wallet_id_by_user_id(user_id, session)
            if not wallet_id:
                logger.error(f"[SUBSCRIBE_CHANGE_PLAN_NO_WALLET] user_id={user_id}")
                return {"success": False, "error": "Wallet not found", "status_code": 404}

            invalidation_result = await invalidate_subscription_batches_and_get_reserved(
                wallet_id=wallet_id,
                subscription_id=internal_sub_id,
                session=session,
                source_plan_duration=current_duration,  # Pass source plan duration for correct batch filtering
            )

            if not invalidation_result.get("success"):
                logger.error(
                    f"[SUBSCRIBE_CHANGE_PLAN_INVALIDATION_FAILED] user_id={user_id}, "
                    f"error={invalidation_result.get('error')}"
                )
                return {
                    "success": False,
                    "error": "Failed to process plan change",
                    "status_code": 500,
                }

            # Extract token values from invalidation result
            # - total_remaining: Sum of all remaining_tokens from old batches
            # - total_reserved: Sum of all reserved_tokens (for ongoing reports)
            # - total_available: remaining - reserved (what user can use)
            total_remaining_tokens = invalidation_result.get("total_remaining_tokens", 0)
            total_reserved_tokens = invalidation_result.get("total_reserved_tokens", 0)
            total_available_tokens = invalidation_result.get("total_available_tokens", 0)
            invalidated_count = invalidation_result.get("invalidated_count", 0)

            logger.info(
                f"[SUBSCRIBE_CHANGE_PLAN_BATCHES_INVALIDATED] user_id={user_id}, "
                f"invalidated_count={invalidated_count}, "
                f"total_remaining={total_remaining_tokens}, "
                f"total_reserved={total_reserved_tokens}, "
                f"total_available={total_available_tokens}"
            )

            # 5. Store calculation metadata for webhook processing
            # IMPORTANT:
            # - Update plan_tier and plan_duration to NEW values (don't create separate fields)
            # - Append to plan_change_history list for complete audit trail
            # - Webhook will use available_tokens and reserved_tokens from metadata

            # Get existing plan change history (list of all changes)
            existing_history = meta_data.get("plan_change_history", [])
            if not isinstance(existing_history, list):
                existing_history = []  # Convert old format to list

            # Create change record (changed_at will be set when webhook confirms)
            requested_at = datetime.now(UTC).isoformat()
            change_record = {
                "from_tier": current_tier,
                "from_duration": current_duration,
                "to_tier": new_plan_tier.lower(),
                "to_duration": new_plan_duration.lower(),
                "requested_at": requested_at,  # When change was requested
                "changed_at": None,  # Will be set when webhook confirms
                "total_remaining_tokens": total_remaining_tokens,
                "reserved_tokens": total_reserved_tokens,
                "available_tokens": total_available_tokens,
                "over_under_consumption": token_usage_result.get("over_under_consumption"),
                "days_elapsed": token_usage_result.get(
                    "days_elapsed", 0
                ),  # NEW: For same-day detection
                "actual_consumed_tokens": token_usage_result.get(
                    "actual_consumed_tokens", 0
                ),  # NEW: For same-day logic
            }
            existing_history.append(change_record)

            update_metadata = {
                # DON'T update plan_tier/plan_duration yet - wait for webhook confirmation
                # This prevents inconsistent state if payment gateway rejects the change
                # Plan change tracking (for audit and webhook processing)
                "plan_update_requested_at": requested_at,
                "plan_change_history": existing_history,  # List of all changes with token details
            }

            # Update subscription metadata
            from app.wallet.repository import update_subscription_metadata

            await update_subscription_metadata(
                external_sub_id,
                update_metadata,
                session,
                None,  # Don't change status yet
            )

            logger.info(
                f"[SUBSCRIBE_CHANGE_PLAN_METADATA_STORED] user_id={user_id}, "
                f"plan_tier={new_plan_tier}, plan_duration={new_plan_duration}, "
                f"over_under_consumption={token_usage_result.get('over_under_consumption')}, "
                f"available_tokens={total_available_tokens}, reserved_tokens={total_reserved_tokens}"
            )

            # 5. Call payment service to update subscription
            # The payment service will handle the actual subscription update at the gateway
            # and send back SUBSCRIPTION_UPDATED webhook
            from app.wallet.service_payment import PaymentService

            # Get country from metadata
            country = meta_data.get("country", "default")

            payment_result = await PaymentService.update_subscription(
                subscription_id=external_sub_id,
                new_plan_tier=new_plan_tier.upper(),
                new_plan_duration=new_plan_duration.upper(),
                country=country,
                idempotency_key=idempotency_key,
            )

            if not payment_result.get("success"):
                logger.error(
                    f"[SUBSCRIBE_CHANGE_PLAN_PAYMENT_FAILED] user_id={user_id}, "
                    f"error={payment_result.get('error')}"
                )
                return {
                    "success": False,
                    "error": payment_result.get(
                        "error", "Failed to update subscription at payment gateway"
                    ),
                    "status_code": 500,
                }

            logger.info(
                f"[SUBSCRIBE_CHANGE_PLAN_SUCCESS] user_id={user_id}, "
                f"old_plan={current_tier}_{current_duration}, "
                f"new_plan={new_plan_tier}_{new_plan_duration}"
            )

            return {
                "success": True,
                "data": payment_result.get("data", {}),
                "subscription_id": internal_sub_id,
                "message": (
                    f"Subscription update initiated from {current_tier.upper()} {current_duration.upper()} "
                    f"to {new_plan_tier.upper()} {new_plan_duration.upper()}. "
                    f"Tokens will be adjusted based on fair usage."
                ),
            }

        except Exception as e:
            logger.critical(
                f"[SUBSCRIBE_CHANGE_PLAN_EXCEPTION] user_id={user_id}, error={e!s}",
                exc_info=True,
            )
            return {
                "success": False,
                "error": "An unexpected error occurred while updating subscription",
                "status_code": 500,
            }

    @staticmethod
    async def handle_subscription_updated_webhook(
        subscription_id: str, event_data: Any, session: AsyncSession
    ) -> dict[str, Any]:
        """
        Handle SUBSCRIPTION_UPDATED webhook event for plan changes.
        Currently only supports Plus Monthly → Plus Yearly transitions.

        Flow:
        1. Validate subscription exists and has plan change metadata
        2. Validate the transition type (plus monthly → plus yearly)
        3. Expire all old subscription intervals
        4. Create 12 new monthly token batches for the yearly plan
        5. Create 12 transaction records (one per batch for transparency)
        6. Update subscription metadata to reflect new plan
        7. Sync wallet balance

        Args:
            subscription_id: External subscription ID from payment gateway
            event_data: Webhook event data containing plan details
            session: Database session

        Returns:
            Dict with success status and details
        """
        logger.info(
            f"[SUBSCRIPTION_UPDATED_RECEIVED] subscription_id={subscription_id}, "
            f"plan_tier={event_data.plan_tier}, plan_duration={event_data.plan_duration}, "
            f"gateway_plan_id={event_data.gateway_plan_id}, status={event_data.status}"
        )

        # ========================================================================
        # STEP 1: Look up the subscription record
        # ========================================================================
        subscription = await get_subscription_by_subscription_id(
            subscription_id=subscription_id, session=session
        )

        if not subscription:
            logger.error(
                f"[SUBSCRIPTION_UPDATED_NOT_FOUND] subscription_id={subscription_id}, "
                f"No subscription record found in database"
            )
            return {
                "success": False,
                "error": f"Subscription record not found for ID: {subscription_id}",
            }

        # ========================================================================
        # STEP 2: Validate this is a plan change request
        # ========================================================================
        # Plan changes are identified by the presence of "plan_update_requested_at" in metadata
        # This was set during change_subscription_plan() before calling payment service
        meta_data = subscription.meta_data or {}
        is_plan_change = "plan_update_requested_at" in meta_data

        if not is_plan_change:
            # This webhook is not for a plan change - ignore it
            logger.warning(
                f"[SUBSCRIPTION_UPDATED_NO_PLAN_CHANGE] subscription_id={subscription_id}, "
                f"received SUBSCRIPTION_UPDATED without plan change metadata"
            )
            return {
                "success": False,
                "error": "SUBSCRIPTION_UPDATED event received without plan change metadata",
            }

        # ========================================================================
        # STEP 3: Validate the transition type (only Plus Monthly → Plus Yearly)
        # ========================================================================
        # Get current plan from metadata (NOT updated yet - we do that after confirmation)
        current_plan_tier = meta_data.get("plan_tier", "").lower()
        current_plan_duration = meta_data.get("plan_duration", "").lower()

        # Get the pending plan change from history (list of all changes, get the most recent)
        plan_change_history = meta_data.get("plan_change_history", [])
        if not isinstance(plan_change_history, list):
            plan_change_history = [plan_change_history] if plan_change_history else []

        # Get the most recent plan change (last item in list)
        if not plan_change_history:
            # No history found - this webhook is not for a plan change
            logger.warning(
                f"[SUBSCRIPTION_UPDATED_NO_HISTORY] subscription_id={subscription_id}, "
                f"plan_change_history is empty"
            )
            return {
                "success": False,
                "error": "Plan change history not found in subscription metadata",
            }

        latest_change = plan_change_history[-1]
        old_tier = latest_change.get("from_tier", "").lower()
        old_duration = latest_change.get("from_duration", "").lower()
        new_tier = latest_change.get("to_tier", "").lower()
        new_duration = latest_change.get("to_duration", "").lower()

        # Validate we have a valid transition
        if not old_tier or not new_tier:
            logger.error(
                f"[SUBSCRIPTION_UPDATED_INVALID_TRANSITION] subscription_id={subscription_id}, "
                f"missing tier information: old_tier={old_tier}, new_tier={new_tier}"
            )
            return {"success": False, "error": "Invalid plan change data: missing tier information"}

        logger.info(
            f"[SUBSCRIPTION_UPDATED_PLAN_CHANGE] subscription_id={subscription_id}, "
            f"processing plan change: {old_tier}_{old_duration} → {new_tier}_{new_duration}, "
            f"total_changes={len(plan_change_history)}"
        )

        # ========================================================================
        # STEP 4: Get token adjustment values from plan change history
        # ========================================================================
        # Retrieve token details from the most recent plan change in history list
        # These were calculated and stored during change_subscription_plan():
        # - over_under_consumption: Fair usage adjustment based on time in current cycle
        #   - Positive = over-consumed (reduce first batch)
        #   - Negative = under-consumed (increase first batch)
        #   - Aligns token allocation with Razorpay's financial proration
        # - available_tokens: tokens user can use immediately (remaining - reserved)
        # - reserved_tokens: tokens reserved for ongoing reports
        #
        # IMPORTANT: We use AVAILABLE tokens for initial_tokens, not total remaining!
        # Formula: available = total_remaining - total_reserved

        # Get the most recent change and extract token details
        latest_change = plan_change_history[-1]
        over_under_consumption = latest_change.get("over_under_consumption", 0)
        available_tokens = latest_change.get("available_tokens", 0)
        reserved_tokens = latest_change.get("reserved_tokens", 0)
        total_remaining = latest_change.get("total_remaining_tokens", 0)
        days_elapsed = latest_change.get("days_elapsed", 0)
        actual_consumed_tokens = latest_change.get("actual_consumed_tokens", 0)

        logger.info(
            f"[SUBSCRIPTION_UPDATED_TOKEN_ADJUSTMENT] subscription_id={subscription_id}, "
            f"total_remaining={total_remaining}, available={available_tokens}, "
            f"reserved={reserved_tokens}, over_under={over_under_consumption}, "
            f"days_elapsed={days_elapsed}, actual_consumed={actual_consumed_tokens}"
        )

        # ========================================================================
        # STEP 5: Get wallet and token configuration for NEW plan
        # ========================================================================
        country = meta_data.get("country", "default")
        tier_info = WalletService.get_tier_info(new_tier, country)
        interval_data = tier_info.get(new_duration, {})
        tokens_per_month = interval_data.get("tokens_per_month", 0)

        if tokens_per_month == 0:
            logger.error(
                f"[SUBSCRIPTION_UPDATED_NO_TOKENS_PER_MONTH] subscription_id={subscription_id}, "
                f"tokens_per_month={tokens_per_month} for {new_tier} {new_duration}"
            )
            return {"success": False, "error": "Invalid tokens per month configuration"}

        from app.wallet.repository import _sync_wallet_balances

        wallet_id = await get_wallet_id_by_user_id(subscription.user_id, session)
        if not wallet_id:
            logger.error(
                f"[SUBSCRIPTION_UPDATED_NO_WALLET] subscription_id={subscription_id}, "
                f"wallet not found for user_id={subscription.user_id}"
            )
            return {"success": False, "error": "Wallet not found"}

        # ========================================================================
        # STEP 6: Expire all old subscription intervals
        # ========================================================================
        # Mark all previous subscription intervals as EXPIRED since we're moving to a new plan
        # This ensures old billing cycles don't interfere with the new yearly plan
        from sqlalchemy import update

        expired_intervals_result = await session.execute(
            update(SubscriptionInterval)
            .where(
                SubscriptionInterval.subscription_id == subscription.id,
                SubscriptionInterval.status == SubscriptionStatus.CHARGED,
            )
            .values(status=SubscriptionStatus.EXPIRED)
        )

        expired_count = expired_intervals_result.rowcount
        logger.info(
            f"[SUBSCRIPTION_UPDATED_INTERVALS_EXPIRED] subscription_id={subscription_id}, "
            f"expired_intervals={expired_count}"
        )

        # ========================================================================
        # STEP 7: Create token batches for the new plan
        # ========================================================================
        # For monthly plans: 1 batch
        # For yearly plans: 12 monthly batches
        #
        # Batch 1 structure:
        #   - initial_tokens: tokens_per_month + available_tokens - over_under
        #   - reserved_tokens: reserved_tokens (preserved for ongoing reports)
        #   - User can use: initial - reserved
        # Remaining batches: standard tokens_per_month each
        from app.wallet.repository import create_plan_change_token_batches

        # Get payment details from subscription metadata (always available)
        # Note: meta_data["amount"] has already been updated to new plan amount by this point
        # So we need to get old amount from plan_change_history
        plan_change_history = meta_data.get("plan_change_history", [])
        latest_change = plan_change_history[-1] if plan_change_history else {}

        # Calculate amounts: We need the OLD amount before the update
        # The current meta_data["amount"] is already updated to the NEW plan amount
        # So we calculate old amount from the plan details
        country = (meta_data.get("country", "default") or "default").lower()
        old_tier_info = WalletService.get_tier_info(old_tier, country)
        old_interval_data = old_tier_info.get(old_duration, {})
        old_plan_amount = old_interval_data.get("cost", 0)

        # New plan amount is already in interval_data
        new_plan_amount = interval_data.get("cost", 0)

        # For Indian users, add 18% GST to the amounts
        # SUBSCRIPTION_INFO contains costs WITHOUT GST, but users pay inclusive of GST
        if country == "in":
            old_plan_amount = old_plan_amount * 1.18  # Add 18% GST
            new_plan_amount = new_plan_amount * 1.18  # Add 18% GST

        amount_difference = new_plan_amount - old_plan_amount

        # Get payment details from subscription metadata
        payment_method = meta_data.get("payment_method")
        payment_method_details = meta_data.get("payment_method_details")
        currency = meta_data.get("currency")

        logger.info(
            f"[SUBSCRIPTION_UPDATED_PAYMENT_INFO] subscription_id={subscription_id}, "
            f"country={country}, old_amount={old_plan_amount}, new_amount={new_plan_amount}, "
            f"difference={amount_difference}, currency={currency}, "
            f"gst_applied={'Yes (18%)' if country == 'in' else 'No'}"
        )

        batch_result = await create_plan_change_token_batches(
            wallet_id=wallet_id,
            subscription_id=subscription.id,
            plan_tier=new_tier,
            plan_duration=new_duration,
            tokens_per_month=tokens_per_month,
            available_tokens=available_tokens,  # Available from old plan (goes to initial)
            reserved_tokens=reserved_tokens,  # Reserved from old plan (goes to reserved field)
            over_under_consumption=over_under_consumption,
            days_elapsed=days_elapsed,  # NEW: For same-day upgrade detection
            actual_consumed_tokens=actual_consumed_tokens,  # NEW: For same-day calculation
            payment_id=event_data.payment_id,
            session=session,
            metadata={
                "gateway": event_data.gateway,
                "payment_method": payment_method,
                "payment_method_details": payment_method_details,
                "amount": amount_difference,  # Difference between new and old plan costs
                "currency": currency,
                "old_plan_amount": old_plan_amount,
                "new_plan_amount": new_plan_amount,
                "transition_type": f"{old_tier}_{old_duration}_to_{new_tier}_{new_duration}",
            },
        )

        if not batch_result.get("success"):
            logger.error(
                f"[SUBSCRIPTION_UPDATED_BATCH_CREATION_FAILED] subscription_id={subscription_id}, "
                f"error={batch_result.get('error')}"
            )
            return {
                "success": False,
                "error": f"Failed to create token batches: {batch_result.get('error')}",
            }

        # ========================================================================
        # STEP 8: Create transaction records for transparency
        # ========================================================================
        # Create 12 transaction records (one per batch) so users can see the unlock schedule
        # This is better than 1 transaction because it shows monthly unlocks clearly
        batches_created = batch_result.get("batches_created", 0)  # Should be 12
        batch_ids = batch_result.get("batch_ids", [])
        first_batch_initial = batch_result.get("first_batch_initial_tokens", 0)
        first_batch_reserved = batch_result.get("first_batch_reserved_tokens", 0)
        total_tokens = batch_result.get("total_tokens_credited", 0)

        # Sync wallet balance before creating transactions
        balances = await _sync_wallet_balances(wallet_id, session)

        # Create transaction records for each batch
        for i, batch_id in enumerate(batch_ids):
            is_first_batch = i == 0
            batch_initial_tokens = first_batch_initial if is_first_batch else tokens_per_month
            batch_reserved_tokens = first_batch_reserved if is_first_batch else 0

            # Build descriptive transaction message
            if is_first_batch:
                # First batch description shows breakdown of tokens
                available_in_first = batch_initial_tokens - batch_reserved_tokens
                description_parts = [
                    f"{old_tier.upper()} {old_duration.upper()} → {new_tier.upper()} {new_duration.upper()} (Batch 1/{batches_created})",
                    f"{batch_initial_tokens:,} tokens",
                ]

                # CRITICAL: Different descriptions for same-day vs multi-day upgrades
                if days_elapsed == 0:
                    # Same-day upgrade: Full refund scenario
                    description_parts.append("(Same-day upgrade)")
                    if actual_consumed_tokens > 0:
                        description_parts.append(
                            f"(-{actual_consumed_tokens:,} consumed from old plan)"
                        )
                    if reserved_tokens > 0:
                        description_parts.append(f"({reserved_tokens:,} reserved for reports)")
                else:
                    # Multi-day upgrade: Normal fair usage
                    if available_tokens > 0:
                        description_parts.append(f"(+{available_tokens:,} available from old plan)")
                    if reserved_tokens > 0:
                        description_parts.append(f"({reserved_tokens:,} reserved for reports)")
                    # Show over/under consumption adjustment for transparency
                    if over_under_consumption != 0:
                        if over_under_consumption < 0:
                            # Under-consumed: user gets credit
                            description_parts.append(
                                f"(+{abs(over_under_consumption):,} fair-use credit)"
                            )
                        else:
                            # Over-consumed: tokens deducted
                            description_parts.append(
                                f"(-{over_under_consumption:,} fair-use deduction)"
                            )

                description = " - ".join(description_parts)
            else:
                # Remaining batches: standard unlock (monthly for yearly plans)
                batch_label = (
                    "Monthly Unlock" if new_duration == "yearly" else "Subscription Renewal"
                )
                description = f"{new_tier.upper()} {new_duration.upper()} (Batch {i + 1}/{batches_created}) - {batch_initial_tokens:,} tokens ({batch_label})"

            transaction = TokenTransaction(
                id=str(uuid7()),
                wallet_id=wallet_id,
                transaction_type=TransactionType.CREDIT,
                source_type=TransactionSource.SUBSCRIPTION,
                tokens=batch_initial_tokens,  # Initial tokens (what's added to wallet)
                balance_after=balances["available_balance"],
                reserved_after=balances["reserved_balance"],
                report_id=None,
                token_batch_id=batch_id,
                status=TransactionStatus.COMPLETED,
                description=description,
                meta_data={
                    "subscription_id": subscription.id,
                    "transition_type": f"{old_tier}_{old_duration}_to_{new_tier}_{new_duration}",
                    "batch_number": i + 1,
                    "total_batches": batches_created,
                    "is_first_batch": is_first_batch,
                    "batch_initial_tokens": batch_initial_tokens,
                    "batch_reserved_tokens": batch_reserved_tokens,
                    "available_tokens_from_old_plan": available_tokens if is_first_batch else 0,
                    "reserved_tokens_from_old_plan": reserved_tokens if is_first_batch else 0,
                    "is_same_day_upgrade": days_elapsed == 0 if is_first_batch else False,
                    "days_elapsed": days_elapsed if is_first_batch else None,
                    "actual_consumed_tokens": actual_consumed_tokens if is_first_batch else 0,
                    "over_under_consumption": over_under_consumption if is_first_batch else 0,
                },
            )
            session.add(transaction)

        await session.flush()

        logger.info(
            f"[SUBSCRIPTION_UPDATED_TRANSACTIONS_CREATED] subscription_id={subscription_id}, "
            f"created_{batches_created}_transactions, "
            f"first_batch_initial={first_batch_initial}, first_batch_reserved={first_batch_reserved}, "
            f"total_yearly_tokens={total_tokens}"
        )

        # ========================================================================
        # STEP 8.5: Create a new subscription interval for the new plan
        # ========================================================================
        # Create one interval representing the billing cycle (yearly = 1 year, monthly = 1 month)
        from dateutil.relativedelta import relativedelta

        # Parse dates from event_data
        if event_data.current_start and event_data.current_end:
            interval_start = (
                parse(event_data.current_start)
                if isinstance(event_data.current_start, str)
                else event_data.current_start
            )
            interval_end = (
                parse(event_data.current_end)
                if isinstance(event_data.current_end, str)
                else event_data.current_end
            )
        else:
            # Fallback if dates not provided
            interval_start = datetime.now(UTC)
            interval_end = interval_start + relativedelta(years=1)

        # Get amount from event_data or tier_info (NEW plan pricing, not old metadata)
        interval_amount = getattr(event_data, "amount", None)
        interval_currency = getattr(event_data, "currency", None)

        # Fallback to tier info if webhook doesn't provide amount/currency
        if not interval_amount or not interval_currency:
            interval_amount = interval_data.get("cost", interval_amount)
            interval_currency = interval_data.get("currency", interval_currency or "USD")

        # Link to the first batch (for reference, even though there are 12 batches)
        first_batch_id = batch_ids[0] if batch_ids else None

        # subscription_intervals.payment_id is NOT NULL; use fallback when webhook omits it
        interval_payment_id = (getattr(event_data, "payment_id", None) or "").strip() or None
        if not interval_payment_id:
            interval_payment_id = (
                f"pc_{subscription_id[-20:]}_{int(datetime.now(UTC).timestamp())}"[:50]
            )

        # Convert new_tier string to enum
        new_tier_enum = SubscriptionTier.PLUS if new_tier == "plus" else SubscriptionTier.PRO

        new_interval = SubscriptionInterval(
            id=str(uuid7()),
            subscription_id=subscription.id,
            tier=new_tier_enum,
            start_date=interval_start,
            end_date=interval_end,
            payment_id=interval_payment_id,
            amount=interval_amount,
            currency=interval_currency,
            tokens_credited=total_tokens,
            token_batch_id=first_batch_id,  # Link to first batch for reference
            status=SubscriptionStatus.CHARGED,  # Charged status for active interval
            created_at=datetime.now(UTC),
        )
        session.add(new_interval)
        await session.flush()

        logger.info(
            f"[SUBSCRIPTION_UPDATED_INTERVAL_CREATED] subscription_id={subscription_id}, "
            f"interval_id={new_interval.id}, tier={new_tier}, duration={new_duration}, "
            f"period={interval_start} to {interval_end}, tokens_credited={total_tokens}"
        )

        # ========================================================================
        # STEP 9: Update subscription metadata with gateway confirmation details
        # ========================================================================
        # NOW we update plan_tier and plan_duration after successful token allocation
        # This ensures the plan change is atomic - either everything succeeds or nothing changes

        # Update the most recent plan change record with confirmed timestamp
        latest_change["changed_at"] = datetime.now(UTC).isoformat()
        plan_change_history[-1] = latest_change

        # Next payment for yearly = end of current period (charge_at or current_end)
        next_payment = (
            getattr(event_data, "charge_at", None)
            or event_data.current_end
            or getattr(event_data, "next_payment_datetime", None)
        )

        # Get amount and currency from event_data or tier_info
        new_amount = getattr(event_data, "amount", None)
        new_currency = getattr(event_data, "currency", None)

        # Fallback to tier info if webhook doesn't provide amount/currency
        if not new_amount or not new_currency:
            new_amount = interval_data.get("cost", new_amount)
            new_currency = interval_data.get("currency", new_currency)

        update_metadata = {
            # Update to NEW plan NOW (after tokens are successfully allocated)
            "plan_tier": new_tier,
            "plan_duration": new_duration,
            # Pricing details for new plan
            "amount": new_amount,
            "currency": new_currency,
            "tokens_per_month": tokens_per_month,
            # Gateway confirmation details
            "gateway_plan_id": event_data.gateway_plan_id,
            "current_start": event_data.current_start,
            "current_end": event_data.current_end,
            "next_payment_datetime": next_payment,  # So GET /subscription shows correct next charge (e.g. 2027-02-04 for yearly)
            "gateway": event_data.gateway,
            "plan_change_completed_at": datetime.now(UTC).isoformat(),
            "plan_change_history": plan_change_history,  # Update with changed_at timestamp
            "updated_at": datetime.now(UTC).isoformat(),
        }

        # Remove None values from metadata
        update_metadata = {k: v for k, v in update_metadata.items() if v is not None}

        # Update subscription status to ACTIVE if provided
        new_status = None
        if event_data.status:
            status_upper = event_data.status.upper()
            if status_upper == "ACTIVE":
                new_status = SubscriptionStatus.ACTIVE
            elif status_upper == "PENDING":
                new_status = SubscriptionStatus.PENDING

        # Update subscription table with new plan details
        # Pass tier enum if tier changed (e.g. Plus → Pro), otherwise None
        new_tier_for_update = new_tier_enum if old_tier != new_tier else None

        result = await update_subscription_metadata(
            subscription_id,
            update_metadata,
            session,
            new_status,
            new_tier_for_update,  # Update tier enum if tier changed
        )

        # ========================================================================
        # STEP 10: Final sync and return
        # ========================================================================
        # Sync wallet balance one final time to ensure consistency
        final_balances = await _sync_wallet_balances(wallet_id, session)

        if result.get("success"):
            logger.info(
                f"[SUBSCRIPTION_UPDATED_SUCCESS] subscription_id={subscription_id}, "
                f"successfully transitioned from {old_tier.upper()} {old_duration.upper()} to {new_tier.upper()} {new_duration.upper()}, "
                f"user_id={subscription.user_id}, final_balance={final_balances['available_balance']}"
            )
        else:
            logger.error(
                f"[SUBSCRIPTION_UPDATED_METADATA_FAILED] subscription_id={subscription_id}, "
                f"failed to update subscription metadata: {result.get('error', 'Unknown error')}"
            )

        return result

    @staticmethod
    async def handle_subscription_upgrade(
        user_id: str,
        old_subscription_data: dict[str, Any],
        new_plan_tier: str,
        new_plan_duration: str,
        country: str,
        idempotency_key: str,
        session: AsyncSession,
    ) -> dict[str, Any]:
        """
        Handle subscription upgrade from Plus to Pro.

        This method handles three upgrade scenarios:
        1. Plus Monthly → Pro Monthly
        2. Plus Yearly → Pro Yearly
        3. Plus Yearly → Pro Monthly

        Process:
        1. Cancel old subscription at Razorpay (immediately, not at cycle end)
        2. Expire all active tokens from old subscription
        3. Cancel all future token batches
        4. Create new Pro subscription
        5. Razorpay will handle refunds automatically

        Args:
            user_id: User ID
            old_subscription_data: Current subscription data
            new_plan_tier: New tier (should be 'PRO')
            new_plan_duration: New duration ('MONTHLY' or 'YEARLY')
            country: Country code
            idempotency_key: Idempotency key for payment gateway
            session: Database session

        Returns:
            Dict with success status and subscription details
        """
        old_tier = old_subscription_data.get("current_tier", "").upper()
        old_sub_id = old_subscription_data.get("id")
        external_sub_id = old_subscription_data.get("external_subscription_id")

        logger.info(
            f"[SUBSCRIPTION_UPGRADE_START] user_id={user_id}, "
            f"upgrade_path={old_tier} -> {new_plan_tier.upper()}, "
            f"new_duration={new_plan_duration}, "
            f"old_internal_sub_id={old_sub_id}, "
            f"old_external_sub_id={external_sub_id}"
        )

        try:
            # Step 1: Get wallet ID
            from app.wallet.repository import (
                cancel_future_subscription_batches,
                expire_active_subscription_tokens,
                get_user_email,
                get_wallet_id_by_user_id,
                update_subscription_metadata,
            )

            wallet_id = await get_wallet_id_by_user_id(user_id, session)
            if not wallet_id:
                logger.error(f"[SUBSCRIPTION_UPGRADE_NO_WALLET] user_id={user_id}")
                return {"success": False, "error": "Wallet not found", "status_code": 404}

            # Step 2: Cancel old subscription at Razorpay IMMEDIATELY
            if external_sub_id:
                logger.info(
                    f"[SUBSCRIPTION_UPGRADE_CANCELLING_OLD] user_id={user_id}, "
                    f"external_sub_id={external_sub_id}"
                )

                from app.wallet.service_payment import PaymentService

                cancel_result = await PaymentService.cancel_subscription(
                    subscription_id=external_sub_id.strip(),
                    cancel_at_cycle_end=False,  # Cancel immediately, not at cycle end
                )

                if not cancel_result.get("success"):
                    logger.error(
                        f"[SUBSCRIPTION_UPGRADE_CANCEL_FAILED] user_id={user_id}, "
                        f"external_sub_id={external_sub_id}, "
                        f"error={cancel_result.get('error')}"
                    )
                    return {
                        "success": False,
                        "error": f"Failed to cancel old subscription: {cancel_result.get('error')}",
                        "status_code": 500,
                    }

                logger.info(
                    f"[SUBSCRIPTION_UPGRADE_CANCELLED_OLD] user_id={user_id}, "
                    f"external_sub_id={external_sub_id}"
                )
            else:
                logger.warning(
                    f"[SUBSCRIPTION_UPGRADE_NO_EXTERNAL_ID] user_id={user_id}, "
                    f"internal_sub_id={old_sub_id}"
                )

            # Step 3: Expire active tokens from old subscription
            logger.info(
                f"[SUBSCRIPTION_UPGRADE_EXPIRING_TOKENS] user_id={user_id}, "
                f"wallet_id={wallet_id}, subscription_id={old_sub_id}"
            )

            expire_result = await expire_active_subscription_tokens(
                wallet_id=wallet_id,
                subscription_id=old_sub_id,
                session=session,
                reason=f"upgrade_from_{old_tier.lower()}_to_{new_plan_tier.lower()}",
            )

            if not expire_result.get("success"):
                logger.error(
                    f"[SUBSCRIPTION_UPGRADE_EXPIRE_FAILED] user_id={user_id}, "
                    f"error={expire_result.get('error')}"
                )
                await session.rollback()
                return {
                    "success": False,
                    "error": f"Failed to expire old tokens: {expire_result.get('error')}",
                    "status_code": 500,
                }

            expired_tokens = expire_result.get("expired_tokens", 0)
            new_balance = expire_result.get("new_balance")

            logger.info(
                f"[SUBSCRIPTION_UPGRADE_TOKENS_EXPIRED] user_id={user_id}, "
                f"expired_tokens={expired_tokens}, new_balance={new_balance}"
            )

            # Step 4: Cancel future token batches
            logger.info(f"[SUBSCRIPTION_UPGRADE_CANCELLING_FUTURE] user_id={user_id}")

            cancel_batches_result = await cancel_future_subscription_batches(
                wallet_id=wallet_id, subscription_id=old_sub_id, session=session
            )

            if not cancel_batches_result.get("success"):
                logger.error(
                    f"[SUBSCRIPTION_UPGRADE_CANCEL_BATCHES_FAILED] user_id={user_id}, "
                    f"error={cancel_batches_result.get('error')}"
                )
                await session.rollback()
                return {
                    "success": False,
                    "error": f"Failed to cancel future batches: {cancel_batches_result.get('error')}",
                    "status_code": 500,
                }

            cancelled_batches = cancel_batches_result.get("cancelled_count", 0)
            cancelled_future_tokens = cancel_batches_result.get("cancelled_tokens", 0)

            logger.info(
                f"[SUBSCRIPTION_UPGRADE_FUTURE_CANCELLED] user_id={user_id}, "
                f"cancelled_batches={cancelled_batches}, "
                f"cancelled_future_tokens={cancelled_future_tokens}"
            )

            # Step 5: Mark old subscription as cancelled in our DB
            await update_subscription_metadata(
                subscription_id_ext=external_sub_id.strip() if external_sub_id else "",
                metadata={
                    "cancellation_reason": "upgraded_to_pro",
                    "cancelled_at": datetime.now(UTC).isoformat(),
                },
                session=session,
                status=SubscriptionStatus.CANCELLED,
            )

            # Step 6: Get user email
            email = await get_user_email(user_id, session)
            if not email:
                logger.error(f"[SUBSCRIPTION_UPGRADE_NO_EMAIL] user_id={user_id}")
                await session.rollback()
                return {"success": False, "error": "User email not found", "status_code": 404}

            # Step 7: Create new Pro subscription at payment gateway
            logger.info(
                f"[SUBSCRIPTION_UPGRADE_CREATING_NEW] user_id={user_id}, "
                f"new_tier={new_plan_tier}, new_duration={new_plan_duration}"
            )

            user_data = {
                "user_id": user_id,
                "email": email,
            }

            new_subscription_result = await WalletService.create_subscription_order(
                idempotency_key=idempotency_key,
                plan_tier=new_plan_tier,
                plan_duration=new_plan_duration,
                country=country,
                user_data=user_data,
            )

            if not new_subscription_result.get("success"):
                logger.error(
                    f"[SUBSCRIPTION_UPGRADE_CREATE_NEW_FAILED] user_id={user_id}, "
                    f"error={new_subscription_result.get('error')}"
                )
                await session.rollback()
                return {
                    "success": False,
                    "error": f"Failed to create new subscription: {new_subscription_result.get('error')}",
                    "status_code": 500,
                }

            payment_data = new_subscription_result.get("data", {})
            new_external_subscription_id = payment_data.get("subscription_id")

            if not new_external_subscription_id:
                logger.error(f"[SUBSCRIPTION_UPGRADE_MISSING_EXTERNAL_ID] user_id={user_id}")
                await session.rollback()
                return {
                    "success": False,
                    "error": "Invalid response from payment service: missing subscription_id",
                    "status_code": 500,
                }

            # Step 8: Get tier info for new subscription
            tier_info = WalletService.get_tier_info(new_plan_tier.lower(), country)
            interval_data = tier_info.get(new_plan_duration.lower(), {})
            tokens_per_month = interval_data.get("tokens_per_month", 0)

            if tokens_per_month == 0:
                logger.error(f"[SUBSCRIPTION_UPGRADE_INVALID_TIER] user_id={user_id}")
                await session.rollback()
                return {"success": False, "error": "Invalid tier configuration", "status_code": 500}

            amount_val = float(payment_data.get("amount", 0))
            currency = payment_data.get("currency")
            gateway = payment_data.get("gateway")

            # Step 9: Create pending subscription records in our DB
            pending_result = await WalletService.create_pending_subscription(
                user_id=user_id,
                tier=new_plan_tier.lower(),
                subscription_id=new_external_subscription_id,
                session=session,
                amount=amount_val,
                tokens_per_month=tokens_per_month,
                metadata={
                    "gateway": gateway,
                    "plan_duration": (new_plan_duration or "monthly").lower(),
                    "country": country,
                    "currency": currency,
                    "amount": amount_val,
                    "tokens_per_month": tokens_per_month,
                    "upgraded_from": old_tier.lower(),
                    "upgrade_date": datetime.now(UTC).isoformat(),
                    "expired_active_tokens": expired_tokens,
                    "cancelled_future_batches": cancelled_batches,
                    "cancelled_future_tokens": cancelled_future_tokens,
                },
            )

            if not pending_result.get("success"):
                logger.error(
                    f"[SUBSCRIPTION_UPGRADE_DB_CREATE_FAILED] user_id={user_id}, "
                    f"error={pending_result.get('error')}"
                )
                await session.rollback()
                return {
                    "success": False,
                    "error": pending_result.get("error", "Failed to create subscription records"),
                    "status_code": 500,
                }

            logger.info(
                f"[SUBSCRIPTION_UPGRADE_SUCCESS] user_id={user_id}, "
                f"upgrade_path={old_tier} -> {new_plan_tier.upper()}, "
                f"expired_tokens={expired_tokens}, "
                f"cancelled_future_tokens={cancelled_future_tokens}, "
                f"new_subscription_id={new_external_subscription_id}"
            )

            return {
                "success": True,
                "data": payment_data,
                "subscription_id": pending_result.get("subscription_id"),
                "message": f"Successfully upgraded from {old_tier} to {new_plan_tier.upper()}",
                "upgrade_summary": {
                    "old_tier": old_tier,
                    "new_tier": new_plan_tier.upper(),
                    "new_duration": new_plan_duration,
                    "expired_active_tokens": expired_tokens,
                    "cancelled_future_batches": cancelled_batches,
                    "cancelled_future_tokens": cancelled_future_tokens,
                    "new_balance": new_balance,
                },
            }

        except Exception as e:
            logger.critical(
                f"[SUBSCRIPTION_UPGRADE_EXCEPTION] user_id={user_id}, "
                f"upgrade_path={old_tier} -> {new_plan_tier}, error={e!s}",
                exc_info=True,
            )
            try:
                await session.rollback()
                logger.info(f"[SUBSCRIPTION_UPGRADE_ROLLBACK_SUCCESS] user_id={user_id}")
            except Exception as rollback_error:
                logger.error(
                    f"[SUBSCRIPTION_UPGRADE_ROLLBACK_FAILED] user_id={user_id}, "
                    f"error={rollback_error!s}"
                )

            return {
                "success": False,
                "error": "Failed to upgrade subscription. Please try again or contact support.",
                "status_code": 500,
            }

    @staticmethod
    async def create_subscription(
        user_id: str,
        plan_tier: str,
        plan_duration: str,
        country: str,
        idempotency_key: str,
        session: AsyncSession,
    ) -> dict[str, Any]:
        """
        Create a new subscription (user must not have an active subscription).
        Called when to_update=False on /subscribe.
        If the user already has an active subscription, returns an error directing
        them to use the same /subscribe endpoint with to_update=True.
        """
        logger.info(
            f"[SUBSCRIBE_CREATE_START] user_id={user_id}, tier={plan_tier}, "
            f"duration={plan_duration}, country={country}, "
            f"idempotency_key={idempotency_key[:16]}..."
        )

        try:
            # 1. Check if user already has a subscription
            active_subscription = await get_active_subscription(
                session, user_id=user_id, filter_created_status=True
            )
            if active_subscription.get("success") and active_subscription.get("current_interval"):
                logger.warning(
                    f"[SUBSCRIBE_CREATE_ALREADY_ACTIVE] user_id={user_id}, "
                    f"use_subscribe_with_to_update_true"
                )
                return {
                    "success": False,
                    "error": "You already have an active subscription. To change your plan, please use the subscription update endpoint.",
                    "status_code": 400,
                }

            # Check subscription object status
            if (
                active_subscription.get("success")
                and active_subscription.get("subscription")
                and active_subscription.get("subscription").get("status")
                not in (
                    SubscriptionStatus.CANCELLED.value,
                    SubscriptionStatus.EXPIRED.value,
                    SubscriptionStatus.COMPLETED.value,
                )
            ):
                sub_status = active_subscription.get("subscription").get("status")
                has_active_interval = active_subscription.get("current_interval") is not None

                if (
                    sub_status
                    in (SubscriptionStatus.ACTIVE.value, SubscriptionStatus.PENDING.value)
                    and has_active_interval
                ):
                    logger.warning(
                        f"[SUBSCRIBE_CREATE_ALREADY_EXISTS] user_id={user_id}, "
                        f"existing_subscription_id={active_subscription.get('subscription').get('id')}"
                    )
                    return {
                        "success": False,
                        "error": "You already have an active subscription. To change your plan, please use the subscription update endpoint.",
                        "status_code": 400,
                    }
                elif (
                    sub_status
                    in (SubscriptionStatus.ACTIVE.value, SubscriptionStatus.PENDING.value)
                    and not has_active_interval
                ):
                    logger.info(
                        f"[SUBSCRIBE_CREATE_LAPSED_FOUND] user_id={user_id}, "
                        f"existing_subscription_id={active_subscription.get('subscription').get('id')}, "
                        f"status={sub_status}, marking stale subscription as expired to allow new creation"
                    )
                    await update_subscription_metadata(
                        active_subscription.get("subscription").get("id").strip()
                        if active_subscription.get("subscription").get("id")
                        else "",
                        {},
                        session,
                        SubscriptionStatus.EXPIRED,
                    )
                elif sub_status == SubscriptionStatus.CREATED.value:
                    logger.info(
                        f"[SUBSCRIBE_CREATE_PENDING_FOUND] user_id={user_id}, "
                        f"subscription_id={active_subscription.get('subscription').get('id')}"
                    )

                    await update_subscription_metadata(
                        active_subscription.get("subscription").get("id").strip()
                        if active_subscription.get("subscription").get("id")
                        else "",
                        {},
                        session,
                        SubscriptionStatus.CANCELLED,
                    )

                    # if not subscription.subscription_id:
                    #     return {
                    #         "success": False,
                    #         "error": "Pending subscription found but missing external ID",
                    #         "status_code": 500
                    #     }

                    # Fetch details from payment service
                    # sub_details = await PaymentService.get_subscription(subscription.subscription_id)

                    # if not sub_details.get("success"):
                    #      logger.error(
                    #         f"[SUBSCRIPTION_FETCH_FAILED] user_id={user_id}, "
                    #         f"subscription_id={subscription.subscription_id}, "
                    #         f"error={sub_details.get('error')}"
                    #     )
                    #      return {
                    #         "success": False,
                    #         "error": "Failed to fetch pending subscription details",
                    #         "status_code": 500
                    #     }

                    # return {
                    #     "success": True,
                    #     "data": sub_details.get("data"),
                    #     "subscription_id": subscription.subscription_id,
                    # }
                else:
                    # TODO: Handle other subscription statuses
                    logger.warning(
                        f"[SUBSCRIBE_CREATE_ALREADY_EXISTS] user_id={user_id}, "
                        f"existing_subscription_id={active_subscription.get('subscription').get('id')}"
                    )
                    return {
                        "success": False,
                        "error": f"User already has a subscription with status {active_subscription.get('subscription').get('status')}. Please resolve or cancel existing subscription before subscribing again.",
                        "status_code": 400,
                    }

            # 2. Get user info for payment service
            email = await get_user_email(user_id, session)
            if not email:
                logger.error(f"[SUBSCRIBE_CREATE_USER_EMAIL_NOT_FOUND] user_id={user_id}")
                return {"success": False, "error": "User email not found", "status_code": 404}

            user_name = email.split("@")[0] if email else "User"
            logger.debug(f"[SUBSCRIBE_CREATE_USER_INFO] user_id={user_id}, email={email[:3]}***")

            user_data = {"user_id": user_id, "email": email, "name": user_name}

            # 3. Call payment service
            logger.info(f"[SUBSCRIBE_CREATE_CALLING_PAYMENT_SERVICE] user_id={user_id}")
            result = await WalletService.create_subscription_order(
                idempotency_key=idempotency_key,
                plan_tier=plan_tier,
                plan_duration=plan_duration,
                country=country,
                user_data=user_data,
            )

            if not result.get("success"):
                logger.error(
                    f"[SUBSCRIBE_CREATE_PAYMENT_SERVICE_FAILED] user_id={user_id}, "
                    f"error={result.get('error', 'Unknown error')}"
                )
                return result

            # 4. Process payment service response
            payment_data = result.get("data", {})
            gateway = payment_data.get("gateway")
            external_subscription_id = payment_data.get("subscription_id")

            if not external_subscription_id:
                logger.error(
                    f"[SUBSCRIBE_CREATE_MISSING_EXTERNAL_ID] user_id={user_id}, "
                    f"payment_data={payment_data}"
                )
                return {
                    "success": False,
                    "error": "Invalid response from payment service: missing subscription_id",
                    "status_code": 500,
                }

            logger.info(
                f"[SUBSCRIBE_CREATE_PAYMENT_SERVICE_SUCCESS] user_id={user_id}, "
                f"external_subscription_id={external_subscription_id}, gateway={gateway}"
            )

            # 5. Resolve tokens and pricing details locally for the DB record
            tier_info = WalletService.get_tier_info(plan_tier.lower(), country)
            interval_data = tier_info.get(plan_duration.lower(), {})
            tokens_per_month = interval_data.get("tokens_per_month", 0)

            if tokens_per_month == 0:
                logger.error(
                    f"[SUBSCRIBE_CREATE_INVALID_TIER_INFO] user_id={user_id}, "
                    f"tier={plan_tier}, duration={plan_duration}, country={country}"
                )
                return {"success": False, "error": "Invalid tier configuration", "status_code": 500}

            amount_val = float(payment_data.get("amount", 0))
            currency = payment_data.get("currency")

            # 6. Create pending subscription records in database
            logger.info(f"[SUBSCRIBE_CREATE_PENDING_RECORD] user_id={user_id}")
            pending_result = await WalletService.create_pending_subscription(
                user_id=user_id,
                tier=plan_tier.lower(),
                subscription_id=external_subscription_id,
                session=session,
                amount=amount_val,
                tokens_per_month=tokens_per_month,
                metadata={
                    "gateway": gateway,
                    "plan_duration": plan_duration,
                    "country": country,
                    "currency": currency,
                    "amount": amount_val,
                    "tokens_per_month": tokens_per_month,
                },
            )

            if not pending_result.get("success"):
                logger.error(
                    f"[SUBSCRIBE_CREATE_DB_FAILED] user_id={user_id}, "
                    f"error={pending_result.get('error', 'Unknown error')}"
                )
                return {
                    "success": False,
                    "error": pending_result.get("error", "Failed to create subscription records"),
                    "status_code": 500,
                }

            logger.info(
                f"[SUBSCRIBE_CREATE_SUCCESS] user_id={user_id}, "
                f"subscription_id={pending_result.get('subscription_id')}, "
                f"external_subscription_id={external_subscription_id}, "
                f"tokens_per_month={tokens_per_month}"
            )

            return {
                "success": True,
                "data": payment_data,
                "subscription_id": pending_result.get("subscription_id"),
            }

        except ValueError as e:
            logger.error(
                f"[SUBSCRIBE_CREATE_VALUE_ERROR] user_id={user_id}, error={e!s}", exc_info=True
            )
            return {"success": False, "error": f"Invalid parameter: {e!s}", "status_code": 400}
        except Exception as e:
            logger.critical(
                f"[SUBSCRIBE_CREATE_EXCEPTION] user_id={user_id}, error={e!s}", exc_info=True
            )
            return {
                "success": False,
                "error": "Failed to initiate subscription",
                "status_code": 500,
            }

    @staticmethod
    async def cancel_subscription(
        user_id: str, session: AsyncSession, reason: str | None = None
    ) -> dict[str, Any]:
        """
        Cancel a user's active subscription at the end of the current billing cycle.

        Business Logic:
        - User has already paid for the current billing cycle
        - They keep access and tokens until the cycle ends (no refund)
        - This just stops the auto-renewal for the next cycle
        - Subscription will be marked as cancelled when cycle ends

        Args:
            user_id: User ID
            session: Database session
            reason: Optional reason for cancellation

        Returns:
            Dict with success status and cancellation details
        """
        logger.info(
            f"[SUBSCRIPTION_CANCEL_START] user_id={user_id}, "
            f"cancel_at_cycle_end=True (always), reason={reason}"
        )

        try:
            # 1. Get active subscription
            active_subscription = await get_active_subscription(session, user_id=user_id)

            if not active_subscription.get("success"):
                logger.error(
                    f"[SUBSCRIPTION_CANCEL_DB_ERROR] user_id={user_id}, "
                    f"error={active_subscription.get('error')}"
                )
                return {
                    "success": False,
                    "error": "Failed to retrieve subscription details",
                    "status_code": 500,
                }

            subscription_data = active_subscription.get("subscription")
            if not subscription_data:
                logger.warning(f"[SUBSCRIPTION_CANCEL_NO_SUBSCRIPTION] user_id={user_id}")
                return {
                    "success": False,
                    "error": "No active subscription found to cancel",
                    "status_code": 404,
                }

            external_sub_id = subscription_data.get("external_subscription_id")
            internal_sub_id = subscription_data.get("id")
            current_tier = subscription_data.get("current_tier", "unknown")
            current_status = subscription_data.get("status", "unknown")

            logger.info(
                f"[SUBSCRIPTION_CANCEL_FOUND] user_id={user_id}, "
                f"tier={current_tier}, status={current_status}, "
                f"external_sub_id={external_sub_id}"
            )

            # 2. Check if subscription is in cancellable state
            cancellable_statuses = ["active", "authenticated", "pending", "halted"]
            if current_status.lower() not in cancellable_statuses:
                logger.warning(
                    f"[SUBSCRIPTION_CANCEL_INVALID_STATUS] user_id={user_id}, "
                    f"status={current_status}"
                )
                return {
                    "success": False,
                    "error": f"Cannot cancel subscription with status '{current_status}'. Already cancelled, completed, or expired.",
                    "status_code": 400,
                }

            # 3. Cancel at Razorpay payment gateway (always at cycle end)
            if external_sub_id:
                logger.info(
                    f"[SUBSCRIPTION_CANCEL_RAZORPAY] user_id={user_id}, "
                    f"external_sub_id={external_sub_id}, cancel_at_cycle_end=True"
                )

                from app.wallet.service_payment import PaymentService

                cancel_result = await PaymentService.cancel_subscription(
                    subscription_id=external_sub_id.strip(),
                    cancel_at_cycle_end=True,  # Always cancel at cycle end
                    reason=reason,
                )

                if not cancel_result.get("success"):
                    logger.error(
                        f"[SUBSCRIPTION_CANCEL_RAZORPAY_FAILED] user_id={user_id}, "
                        f"external_sub_id={external_sub_id}, "
                        f"error={cancel_result.get('error')}"
                    )
                    return {
                        "success": False,
                        "error": f"Failed to cancel subscription at payment gateway: {cancel_result.get('error')}",
                        "status_code": 500,
                    }

                logger.info(
                    f"[SUBSCRIPTION_CANCEL_RAZORPAY_SUCCESS] user_id={user_id}, "
                    f"external_sub_id={external_sub_id}"
                )
            else:
                logger.warning(
                    f"[SUBSCRIPTION_CANCEL_NO_EXTERNAL_ID] user_id={user_id}, "
                    f"internal_sub_id={internal_sub_id}"
                )

            # 4. Update subscription metadata to show it's been cancelled
            #    This allows UI to show cancellation status immediately, before webhook at cycle end
            cancelled_at_iso = datetime.now(UTC).isoformat()

            # Get cancel_at from payment service response
            payment_cancel_data = cancel_result.get("data", {}) if cancel_result else {}
            cancel_at_future = payment_cancel_data.get(
                "cancel_at"
            )  # Future datetime from payment service

            # Update subscription metadata with cancellation details (only 3 fields as specified)
            cancellation_metadata = {
                "is_cancel": True,
                "cancel_at": cancel_at_future,  # Future datetime when it will actually be cancelled
                "cancel_at_cycle_end": True,
            }

            from app.wallet.repository import update_subscription_metadata

            metadata_result = await update_subscription_metadata(
                external_sub_id,
                cancellation_metadata,
                session,
                None,  # Don't change status yet - webhook will change it to CANCELLED at cycle end
            )

            if not metadata_result.get("success"):
                logger.warning(
                    f"[SUBSCRIPTION_CANCEL_METADATA_UPDATE_FAILED] user_id={user_id}, "
                    f"external_sub_id={external_sub_id}, error={metadata_result.get('error')}"
                )
                # Don't fail the whole operation if metadata update fails
            else:
                logger.info(
                    f"[SUBSCRIPTION_CANCEL_METADATA_UPDATED] user_id={user_id}, "
                    f"external_sub_id={external_sub_id}, cancel_at={cancel_at_future}"
                )

            logger.info(
                f"[SUBSCRIPTION_CANCEL_SUCCESS] user_id={user_id}, "
                f"tier={current_tier}, cancel_at_cycle_end=True"
            )

            # 5. Prepare response message
            current_interval = active_subscription.get("current_interval")
            end_date = current_interval.get("end_date") if current_interval else None

            # Use cancel_at from payment service if available, otherwise use current interval end_date
            payment_cancel_data = cancel_result.get("data", {}) if cancel_result else {}
            actual_cancel_date = payment_cancel_data.get("cancel_at") or end_date

            message = f"Your {current_tier.upper()} subscription auto-renewal has been cancelled. "
            message += "You'll keep full access and your tokens until the end of your current billing cycle"
            if actual_cancel_date:
                message += f" ({actual_cancel_date})"
            message += ". No refund will be issued as you've already paid for this period."

            return {
                "success": True,
                "message": message,
                "data": {
                    "subscription_id": internal_sub_id,
                    "external_subscription_id": external_sub_id,
                    "tier": current_tier,
                    "cancel_at_cycle_end": True,
                    "cancelled_at": cancelled_at_iso,
                    "cycle_end_date": actual_cancel_date,  # Use payment service cancel_at
                    "status": "active_until_cycle_end",
                    "is_cancel": True,
                },
            }

        except Exception as e:
            logger.critical(
                f"[SUBSCRIPTION_CANCEL_EXCEPTION] user_id={user_id}, error={e!s}", exc_info=True
            )
            return {
                "success": False,
                "error": "Failed to cancel subscription. Please try again or contact support.",
                "status_code": 500,
            }

    # ========================================================================
    # Token Credits
    # ========================================================================

    @staticmethod
    async def payment_webhook_handler(
        payment_id: str,
        session: AsyncSession,
        status: str,
        amount: float | None = None,
        currency: str | None = None,
        payment_method: str | None = None,
        payment_method_details: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Credit tokens from a completed payment (top-up)."""
        logger.info(
            f"[PAYMENT_WEBHOOK_HANDLER_START] payment_id={payment_id}, status={status}, "
            f"amount={amount}, currency={currency}, payment_method={payment_method}"
        )

        try:
            # Validate inputs
            if not payment_id or not isinstance(payment_id, str):
                logger.error(f"[PAYMENT_WEBHOOK_INVALID_ID] payment_id={payment_id}")
                return {"success": False, "error": "Invalid payment ID"}

            if not status or not isinstance(status, str):
                logger.error(
                    f"[PAYMENT_WEBHOOK_INVALID_STATUS] payment_id={payment_id}, status={status}"
                )
                return {"success": False, "error": "Invalid status"}

            # Determine strict source type Enum if string passed
            if status == "COMPLETED":
                logger.info(
                    f"[PAYMENT_WEBHOOK_PROCESSING_COMPLETION] payment_id={payment_id}, "
                    f"amount={amount}, currency={currency}, payment_method={payment_method}"
                )
                result = await WalletService.process_topup_confirmation(
                    payment_id=payment_id,
                    session=session,
                    amount=amount,
                    currency=currency,
                    payment_method=payment_method,
                    payment_method_details=payment_method_details,
                )

            elif status == "FAILED":
                logger.info(f"[PAYMENT_WEBHOOK_PROCESSING_FAILURE] payment_id={payment_id}")
                result = await WalletService.process_topup_failure(
                    payment_id=payment_id,
                    session=session,
                    amount=amount,
                    currency=currency,
                    payment_method=payment_method,
                    payment_method_details=payment_method_details,
                )

            elif status == "CANCELLED":
                logger.info(f"[PAYMENT_WEBHOOK_PROCESSING_CANCELLATION] payment_id={payment_id}")
                result = await WalletService.process_topup_failure(
                    payment_id=payment_id,
                    session=session,
                    amount=amount,
                    currency=currency,
                    payment_method=payment_method,
                    payment_method_details=payment_method_details,
                )

            else:
                logger.warning(
                    f"[PAYMENT_WEBHOOK_UNKNOWN_STATUS] payment_id={payment_id}, status={status}"
                )
                result = {
                    "success": False,
                    "error": f"Invalid status: {status}. Must be COMPLETED, FAILED, or CANCELLED",
                }

            if result.get("success"):
                logger.info(
                    f"[PAYMENT_WEBHOOK_HANDLER_SUCCESS] payment_id={payment_id}, "
                    f"status={status}, result={result}"
                )
            else:
                logger.error(
                    f"[PAYMENT_WEBHOOK_HANDLER_FAILED] payment_id={payment_id}, "
                    f"status={status}, error={result.get('error', 'Unknown error')}"
                )

            await _fire_payment_email(
                event_type=f"TOPUP_{status}",
                payment_data={
                    "payment_id": payment_id,
                    "status": status,
                    "amount": amount,
                    "currency": currency,
                    "payment_method": payment_method,
                    "user_id": result.get("user_id"),
                    "tokens_credited": result.get("tokens_credited"),
                    "balance_after": result.get("balance_after"),
                },
            )

            return result

        except Exception as e:
            logger.critical(
                f"[PAYMENT_WEBHOOK_HANDLER_EXCEPTION] payment_id={payment_id}, "
                f"status={status}, error={e!s}",
                exc_info=True,
            )
            return {"success": False, "error": "Failed to process payment webhook"}

    @staticmethod
    async def handle_subscription_webhook(
        event_data: SubscriptionWebhookEvent, session: AsyncSession
    ) -> dict[str, Any]:
        """Handle incoming subscription webhook events."""
        event_type = event_data.event
        sub_id_ext = event_data.subscription_id

        logger.info(
            f"[SUBSCRIPTION_WEBHOOK_HANDLER_START] event={event_type}, subscription_id={sub_id_ext}"
        )

        # Validate event data
        if not event_type or not sub_id_ext:
            logger.error(
                f"[SUBSCRIPTION_WEBHOOK_INVALID_DATA] event={event_type}, "
                f"subscription_id={sub_id_ext}"
            )
            return {
                "success": False,
                "error": "Invalid webhook data: missing event or subscription_id",
            }

        try:
            # 1. Handle METADATA update events (Legacy or Specialized)
            if event_type == "SUBSCRIPTION_ACTIVATED":
                return await update_subscription_metadata(
                    sub_id_ext, {}, session, SubscriptionStatus.ACTIVE
                )

            # 2. Handle CHARGE events (Credit tokens)
            elif event_type == "SUBSCRIPTION_CHARGED":
                ## TODO : Need to cancel pending subscriptio of the same user.
                tier = (event_data.plan_tier or "plus").lower()
                duration = (event_data.plan_duration or "monthly").lower()

                logger.info(
                    f"[SUBSCRIPTION_CHARGED_DETAILS] subscription_id={sub_id_ext}, "
                    f"tier={tier}, duration={duration}, amount={event_data.amount}, "
                    f"currency={event_data.currency}, paid_count={event_data.paid_count}"
                )

                subscription = await get_subscription_by_subscription_id(
                    subscription_id=sub_id_ext, session=session
                )

                if not subscription:
                    logger.error(
                        f"[SUBSCRIPTION_CHARGED_NO_RECORD] subscription_id={sub_id_ext}, "
                        f"No subscription record found in database"
                    )
                    return {
                        "success": False,
                        "error": f"Subscription record not found for ID: {sub_id_ext}",
                    }

                logger.info(
                    f"[SUBSCRIPTION_FOUND] subscription_id={sub_id_ext}, "
                    f"user_id={subscription.user_id}, status={subscription.status.value}"
                )

                country = "default"
                if subscription and subscription.meta_data:
                    country = subscription.meta_data.get("country", "default")

                tier_info = WalletService.get_tier_info(tier, country)
                interval_data = tier_info.get(duration, {})
                tokens_per_month = interval_data.get("tokens_per_month", 0)
                amount = event_data.amount or interval_data.get("cost", 0)
                currency = event_data.currency or interval_data.get("currency", "USD")

                logger.info(
                    f"[SUBSCRIPTION_TOKEN_CALCULATION] subscription_id={sub_id_ext}, "
                    f"tokens_per_month={tokens_per_month}, amount={amount}, currency={currency}"
                )

                start_date = (
                    parse(event_data.start_date) if event_data.start_date else datetime.now(UTC)
                )
                end_date = (
                    parse(event_data.end_date)
                    if event_data.end_date
                    else start_date + timedelta(days=30)
                )

                # Update subscription metadata with details from charge event
                await update_subscription_metadata(
                    sub_id_ext,
                    {
                        "payment_method": event_data.payment_method,
                        "payment_method_details": event_data.payment_method_details,
                        "next_payment_datetime": event_data.next_payment_datetime,
                        "paid_count": event_data.paid_count,
                        "gateway": event_data.gateway,
                    },
                    session,
                )

                logger.info(
                    f"[SUBSCRIPTION_CALLING_PROCESS_CHARGE] subscription_id={sub_id_ext}, "
                    f"tier={tier}, tokens_per_month={tokens_per_month}"
                )

                result = await process_subscription_charge(
                    subscription_id_ext=sub_id_ext,
                    tier=tier,
                    start_date=start_date,
                    end_date=end_date,
                    tokens_per_month=tokens_per_month,
                    session=session,
                    payment_id=getattr(event_data, "payment_id", None)
                    or f"charge_{sub_id_ext}_{event_data.paid_count}",
                    metadata={
                        "amount": amount,
                        "currency": currency,
                        "plan_tier": tier.upper(),
                        "plan_duration": duration.upper(),
                        "payment_method": event_data.payment_method,
                        "payment_method_details": event_data.payment_method_details,
                        "gateway": event_data.gateway,
                    },
                    sub_metadata={
                        "payment_id": event_data.payment_id,
                        "amount": amount,
                        "currency": currency,
                        "plan_tier": tier.upper(),
                        "plan_duration": duration.upper(),
                        "gateway": event_data.gateway,
                        "payment_method": event_data.payment_method,
                        "paid_count": event_data.paid_count,
                        "processed_at": datetime.now(UTC).isoformat(),
                        "payment_method_details": event_data.payment_method_details,
                    },
                    subscription=subscription,
                    plan_duration=duration.lower(),
                )

                if result.get("success"):
                    logger.info(
                        f"[SUBSCRIPTION_CHARGED_SUCCESS] subscription_id={sub_id_ext}, "
                        f"tokens_credited={result.get('tokens_credited', 0)}, "
                        f"balance_after={result.get('balance_after', 'N/A')}"
                    )
                else:
                    logger.error(
                        f"[SUBSCRIPTION_CHARGED_FAILED] subscription_id={sub_id_ext}, "
                        f"error={result.get('error', 'Unknown error')}, "
                        f"full_result={result}"
                    )
                return result

            elif event_type == "SUBSCRIPTION_HALTED":
                active_subscription = await get_active_subscription(
                    session, subscription_id=sub_id_ext
                )
                if not active_subscription.get("success") or not active_subscription.get(
                    "current_interval"
                ):
                    return await update_subscription_metadata(
                        sub_id_ext,
                        {
                            "error_code": event_data.error_code,
                            "error_message": event_data.error_message,
                        },
                        session,
                        SubscriptionStatus.HALTED,
                        current_tier=SubscriptionTier.FREE,
                    )
                else:
                    logger.info(
                        f"[SUBSCRIPTION_HALTED_SUCCESS] subscription_id={sub_id_ext}, "
                        f"current_interval={active_subscription.get('current_interval')}"
                    )
                    return {"success": True, "message": "Subscription already active"}

            # TODO : do we need to do anything with completed or expired subscription intervals, how to renew
            elif event_type == "SUBSCRIPTION_COMPLETED":
                active_subscription = await get_active_subscription(
                    session, subscription_id=sub_id_ext
                )
                if not active_subscription.get("success") or not active_subscription.get(
                    "current_interval"
                ):
                    return await update_subscription_metadata(
                        sub_id_ext,
                        {"next_payment_datetime": "Completed"},
                        session,
                        SubscriptionStatus.COMPLETED,
                        current_tier=SubscriptionTier.FREE,
                    )
                else:
                    logger.info(
                        f"[SUBSCRIPTION_COMPLETED_SUCCESS] subscription_id={sub_id_ext}, "
                        f"current_interval={active_subscription.get('current_interval')}"
                    )
                    return {"success": True, "message": "Subscription already active"}

            elif event_type == "SUBSCRIPTION_EXPIRED":
                active_subscription = await get_active_subscription(
                    session, subscription_id=sub_id_ext
                )
                if not active_subscription.get("success") or not active_subscription.get(
                    "current_interval"
                ):
                    return await update_subscription_metadata(
                        sub_id_ext,
                        {"next_payment_datetime": "Expired"},
                        session,
                        SubscriptionStatus.EXPIRED,
                        current_tier=SubscriptionTier.FREE,
                    )
                else:
                    logger.info(
                        f"[SUBSCRIPTION_EXPIRED_SUCCESS] subscription_id={sub_id_ext}, "
                        f"current_interval={active_subscription.get('current_interval')}"
                    )
                    return {"success": True, "message": "Subscription already active"}
            # TODO : do we need to do anything with active subscription intervals on pause, resume, cancelled
            elif event_type == "SUBSCRIPTION_PAUSED" or event_type == "SUBSCRIPTION_RESUMED":
                pass
            elif event_type == "SUBSCRIPTION_CANCELLED":
                # Add cancelled_at to existing metadata (which should already have is_cancel, cancel_at, cancel_at_cycle_end)
                cancellation_metadata = {"cancelled_at": datetime.now(UTC).isoformat()}
                return await update_subscription_metadata(
                    sub_id_ext, cancellation_metadata, session, SubscriptionStatus.CANCELLED
                )

            # Handle subscription plan updates (upgrade/downgrade via Razorpay webhook)
            elif event_type == "SUBSCRIPTION_UPDATED":
                # Handle subscription plan change (Plus Monthly → Plus Yearly)
                return await WalletService.handle_subscription_updated_webhook(
                    subscription_id=sub_id_ext, event_data=event_data, session=session
                )

            elif event_type == "SUBSCRIPTION_PAYMENT_REFUNDED":
                logger.info(
                    f"[SUBSCRIPTION_PAYMENT_REFUNDED_RECEIVED] subscription_id={sub_id_ext}, "
                    f"refund_id={event_data.refund_id}, amount_refunded={event_data.amount_refunded_total}, "
                    f"refund_status={event_data.refund_status}"
                )

                # Look up the subscription
                subscription = await get_subscription_by_subscription_id(
                    subscription_id=sub_id_ext, session=session
                )

                if not subscription:
                    logger.error(
                        f"[SUBSCRIPTION_PAYMENT_REFUNDED_NOT_FOUND] subscription_id={sub_id_ext}, "
                        f"No subscription record found in database"
                    )
                    return {
                        "success": False,
                        "error": f"Subscription record not found for ID: {sub_id_ext}",
                    }

                # Build refund metadata
                refund_metadata = {
                    "refund_id": event_data.refund_id,
                    "refund_status": event_data.refund_status,
                    "refund_created_at": event_data.refund_created_at,
                    "refund_speed_requested": event_data.refund_speed_requested,
                    "refund_speed_processed": event_data.refund_speed_processed,
                    "gateway_payment_id": event_data.gateway_payment_id,
                    "gateway_invoice_id": event_data.gateway_invoice_id,
                    "gateway_order_id": event_data.gateway_order_id,
                    "original_payment_amount": event_data.original_payment_amount,
                    "amount_refunded_total": event_data.amount_refunded_total,
                    "payment_refund_status": event_data.payment_refund_status,
                    "payment_created_at": event_data.payment_created_at,
                    "payment_description": event_data.payment_description,
                    "razorpay_event": event_data.razorpay_event,
                    "refund_processed_at": datetime.now(UTC).isoformat(),
                }

                # Remove None values
                refund_metadata = {k: v for k, v in refund_metadata.items() if v is not None}

                # Update subscription metadata with refund details
                result = await update_subscription_metadata(
                    sub_id_ext,
                    {"refund_details": refund_metadata},
                    session,
                    None,  # Don't change status
                )

                if result.get("success"):
                    logger.info(
                        f"[SUBSCRIPTION_PAYMENT_REFUNDED_SUCCESS] subscription_id={sub_id_ext}, "
                        f"refund_id={event_data.refund_id}, "
                        f"amount_refunded={event_data.amount_refunded_total}, "
                        f"user_id={subscription.user_id}"
                    )
                else:
                    logger.error(
                        f"[SUBSCRIPTION_PAYMENT_REFUNDED_FAILED] subscription_id={sub_id_ext}, "
                        f"error={result.get('error', 'Unknown error')}"
                    )

                return result

            elif event_type == "SUBSCRIPTION_PENDING":
                active_subscription = await get_active_subscription(
                    session, subscription_id=sub_id_ext
                )
                if not active_subscription.get("success") or not active_subscription.get(
                    "current_interval"
                ):
                    return await update_subscription_metadata(
                        sub_id_ext,
                        {
                            "error_code": event_data.error_code,
                            "error_message": event_data.error_message or "Subscription pending",
                            "gateway": event_data.gateway,
                        },
                        session,
                        SubscriptionStatus.PENDING,
                    )
                else:
                    logger.info(
                        f"[SUBSCRIPTION_PENDING_SUCCESS] subscription_id={sub_id_ext}, "
                        f"current_interval={active_subscription.get('current_interval')}"
                    )
                    return {"success": True, "message": "Subscription already active"}

            logger.warning(
                f"[SUBSCRIPTION_WEBHOOK_UNSUPPORTED_EVENT] event={event_type}, "
                f"subscription_id={sub_id_ext}"
            )
            return {"success": False, "error": f"Unsupported event type: {event_type}"}

        except ValueError as e:
            logger.error(
                f"[SUBSCRIPTION_WEBHOOK_VALUE_ERROR] event={event_type}, "
                f"subscription_id={sub_id_ext}, error={e!s}",
                exc_info=True,
            )
            return {"success": False, "error": f"Invalid data: {e!s}"}
        except KeyError as e:
            logger.error(
                f"[SUBSCRIPTION_WEBHOOK_KEY_ERROR] event={event_type}, "
                f"subscription_id={sub_id_ext}, missing_key={e!s}",
                exc_info=True,
            )
            return {"success": False, "error": f"Missing required field: {e!s}"}
        except Exception as e:
            logger.critical(
                f"[SUBSCRIPTION_WEBHOOK_EXCEPTION] event={event_type}, "
                f"subscription_id={sub_id_ext}, error={e!s}",
                exc_info=True,
            )
            return {"success": False, "error": "Failed to process subscription webhook"}
        finally:
            await _fire_payment_email(
                event_type=event_type,
                payment_data={
                    "subscription_id": sub_id_ext,
                    "payment_id": getattr(event_data, "payment_id", None),
                    "plan_tier": getattr(event_data, "plan_tier", None),
                    "plan_duration": getattr(event_data, "plan_duration", None),
                    "amount": getattr(event_data, "amount", None),
                    "currency": getattr(event_data, "currency", None),
                    "gateway": getattr(event_data, "gateway", None),
                    "paid_count": getattr(event_data, "paid_count", None),
                    "payment_method": getattr(event_data, "payment_method", None),
                    "refund_id": getattr(event_data, "refund_id", None),
                    "amount_refunded_total": getattr(event_data, "amount_refunded_total", None),
                    "status": getattr(event_data, "status", None),
                },
            )

    @staticmethod
    async def create_topup_order(
        user_id: str, amount: int, country_code: str, idempotency_key: str, session: AsyncSession
    ) -> dict[str, Any]:
        """Create a top-up order with comprehensive error handling."""

        logger.info(
            f"[TOPUP_ORDER_START] user_id={user_id}, amount={amount}, "
            f"country={country_code}, idempotency_key={idempotency_key[:16]}..."
        )

        try:
            # Validate amount
            if amount <= 0:
                logger.warning(f"[TOPUP_INVALID_AMOUNT] user_id={user_id}, amount={amount}")
                return {"success": False, "error": "Amount must be greater than 0"}

            # if amount > 10000:
            #     logger.warning(f"[TOPUP_AMOUNT_EXCEEDED] user_id={user_id}, amount={amount}")
            #     return {"success": False, "error": "Amount exceeds maximum limit"}

            # Resolve target currency based on region
            country_norm = country_code.lower() if country_code else "default"
            currency = COUNTRY_TO_CURRENCY.get(country_norm, COUNTRY_TO_CURRENCY["default"])

            # 1. Calculate Tax, Tokens, and Total Amount (handles conversion from USD)
            tax_result = WalletService.calculate_tax(country_code, float(amount), currency)

            total_amount = tax_result["total_amount"]
            tax_amount = tax_result["tax_amount"]
            tokens = tax_result["tokens_per_month"]  # Fixed: Use correct key name
            amount_in_currency = tax_result["amount_in_currency"]

            logger.info(
                f"[TOPUP_TOKEN_CALCULATION] user_id={user_id}, amount={amount}, "
                f"currency={currency}, tokens={tokens}, total_amount={total_amount}"
            )

            email = await get_user_email(user_id, session)
            if not email:
                logger.error(f"[TOPUP_USER_EMAIL_NOT_FOUND] user_id={user_id}")
                return {
                    "success": False,
                    "error": "User email not found. Please ensure your account has a valid email address.",
                    "status_code": 400,
                }

            existing_batch = await check_existing_token_batch(idempotency_key, session)
            if existing_batch:
                status = existing_batch.status
                payment_id = existing_batch.payment_id
                if status in [TransactionStatus.COMPLETED, TransactionStatus.FAILED]:
                    return {
                        "success": False,
                        "error": f"Top-up already exists with status {status.value}",
                        "payment_id": payment_id,
                        "status": status.value,
                    }

                elif status == TransactionStatus.PENDING:
                    # Get razorpay order id and status from payment service
                    razorpay_result = await PaymentService.get_payment_order(payment_id=payment_id)

                    razorpay_order = (
                        razorpay_result.get("data") if razorpay_result.get("success") else None
                    )
                    razorpay_status = razorpay_order.get("status") if razorpay_order else None

                    if razorpay_status in ("COMPLETED", "FAILED", "CANCELLED"):
                        wallet_result = await WalletService.payment_webhook_handler(
                            payment_id, session, razorpay_status
                        )
                        if wallet_result.get("success"):
                            return {
                                "success": False,
                                "error": f"Top-up already exists with status {razorpay_status}",
                                "payment_id": payment_id,
                                "status": razorpay_status,
                            }

                    return {"success": True, "data": razorpay_order}
                else:
                    # TODO: Handle other statuses
                    pass

            # 2. Create Order via PaymentService
            result = await PaymentService.create_topup_order(
                idempotency_key=idempotency_key,
                user_id=user_id,
                amount=total_amount,  # Including taxes and converted to target currency
                currency=currency,
                country_code=country_code,
                email=email,
                tokens=tokens,
            )

            if not result.get("success"):
                return result

            data = result.get("data", {})
            payment_id = data.get("payment_id")
            if not payment_id:
                return {
                    "success": False,
                    "error": "Invalid response from payment service: missing payment ID",
                }

            gateway = data.get("gateway")

            # 3. Create PENDING Token Batch
            wallet_id = await get_wallet_id_by_user_id(user_id, session)
            if not wallet_id:
                return {"success": False, "error": "Wallet not found"}

            batch_result = await create_token_batch(
                wallet_id=wallet_id,
                tokens=tokens,
                source_type=TransactionSource.TOPUP,
                currency=currency,
                session=session,
                payment_id=payment_id,
                amount=total_amount,
                status=TransactionStatus.PENDING,
                idempotency_key=idempotency_key,
                metadata={
                    "tax_amount": tax_amount,
                    "base_amount": amount_in_currency,
                    "tax_percentage": tax_result["tax_percentage"],
                    "tax_details": tax_result["tax_details"],
                    "country_code": country_code,
                    "token_rate": tokens / amount_in_currency if amount_in_currency > 0 else 0,
                    "gateway": gateway,
                },
            )

            if not batch_result.get("success"):
                logger.error(
                    f"[TOPUP_BATCH_CREATE_FAILED] user_id={user_id}, "
                    f"error={batch_result.get('error')}"
                )
                return {"success": False, "error": batch_result.get("error")}

            logger.info(
                f"[TOPUP_ORDER_SUCCESS] user_id={user_id}, payment_id={payment_id}, "
                f"tokens={tokens}, amount={total_amount}, currency={currency}"
            )

            return {"success": True, "data": data}

        except ValueError as e:
            logger.error(f"[TOPUP_ORDER_VALUE_ERROR] user_id={user_id}, error={e!s}", exc_info=True)
            return {"success": False, "error": f"Invalid parameter: {e!s}"}
        except KeyError as e:
            logger.error(
                f"[TOPUP_ORDER_KEY_ERROR] user_id={user_id}, missing_key={e!s}", exc_info=True
            )
            return {"success": False, "error": f"Missing required field: {e!s}"}
        except Exception as e:
            logger.critical(
                f"[TOPUP_ORDER_EXCEPTION] user_id={user_id}, error={e!s}", exc_info=True
            )
            return {"success": False, "error": "Failed to create topup order"}

    @staticmethod
    async def process_topup(
        user_id: str, tokens: int, payment_id: str, amount: float, session: AsyncSession
    ) -> dict[str, Any]:
        """Process a token top-up payment."""
        return await WalletService.credit_from_payment(
            user_id=user_id,
            tokens=tokens,
            source_type=TransactionSource.TOPUP.value,
            payment_id=payment_id,
            session=session,
            amount=amount,
            description=f"Wallet Top-up - {tokens:,} tokens",
        )

    @staticmethod
    async def process_topup_confirmation(
        payment_id: str,
        session: AsyncSession,
        amount: float | None = None,
        currency: str | None = None,
        payment_method: str | None = None,
        payment_method_details: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """
        Process confirmation of a top-up payment via webhook.
        Verifies payment status and confirms the pending batch.

        Args:
            payment_id: Payment ID from payment service
            session: Database session
            amount: Payment amount (for verification/update)
            currency: Payment currency (for verification/update)
            payment_method: Payment method type (card, upi, etc.)
            payment_method_details: Detailed payment method information
        """

        logger.info(
            f"[TOPUP_CONFIRMATION_START] payment_id={payment_id}, amount={amount}, "
            f"currency={currency}, payment_method={payment_method}"
        )

        # 1. Verify payment status with PaymentService
        # The ID coming from webhook might be "order_..." or "pay_..."

        # Verify status (optional, but good for security)
        # In a real webhook, we trust the signature verification (done at router level usually)
        # But we can double check status if needed.
        # For now, let's assume valid webhook means valid payment success.

        # 2. Confirm batch with payment details
        result = await confirm_token_batch(
            payment_id=payment_id,
            session=session,
            amount=amount,
            currency=currency,
            payment_method=payment_method,
            payment_method_details=payment_method_details,
        )

        if result.get("success"):
            logger.info(
                f"[TOPUP_CONFIRMATION_SUCCESS] payment_id={payment_id}, "
                f"tokens={result.get('tokens')}, balance_after={result.get('balance_after')}"
            )
            return {
                "success": True,
                "tokens_credited": result.get("tokens"),
                "balance_after": result.get("balance_after"),
                "message": "Top-up confirmed successfully",
            }
        else:
            logger.error(
                f"[TOPUP_CONFIRMATION_FAILED] payment_id={payment_id}, error={result.get('error')}"
            )
            return result

    @staticmethod
    async def process_topup_failure(
        payment_id: str,
        session: AsyncSession,
        amount: float | None = None,
        currency: str | None = None,
        payment_method: str | None = None,
        payment_method_details: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """
        Process failure of a top-up payment via webhook.
        Marks the pending batch and transaction as FAILED.

        Args:
            payment_id: Payment ID from payment service
            session: Database session
            amount: Payment amount (for logging/audit)
            currency: Payment currency (for logging/audit)
            payment_method: Payment method type (card, upi, etc.)
            payment_method_details: Detailed payment method information
        """

        logger.info(
            f"[TOPUP_FAILURE_START] payment_id={payment_id}, amount={amount}, "
            f"currency={currency}, payment_method={payment_method}"
        )

        result = await fail_token_batch(
            payment_id=payment_id,
            session=session,
            amount=amount,
            currency=currency,
            payment_method=payment_method,
            payment_method_details=payment_method_details,
        )

        if result.get("success"):
            logger.info(f"[TOPUP_FAILURE_SUCCESS] payment_id={payment_id}, marked as failed")
            return {"success": True, "status": "failed", "message": "Top-up marked as failed"}
        else:
            logger.error(
                f"[TOPUP_FAILURE_FAILED] payment_id={payment_id}, error={result.get('error')}"
            )
            return result

    @staticmethod
    async def get_payment_status(
        payment_id: str, user_id: str, session: AsyncSession
    ) -> dict[str, Any]:
        """
        Get the status of a token batch by payment_id.
        Used for polling from frontend after payment initiation.

        Args:
            payment_id: The payment ID to check
            user_id: The user ID to verify ownership
            session: Database session

        Returns:
            Dict containing success status and token batch details
        """
        logger.info(f"[GET_PAYMENT_STATUS] payment_id={payment_id}, user_id={user_id}")

        try:
            # Try exact match first
            token_batch = await get_token_batch_by_payment_id(payment_id, session)

            if not token_batch:
                logger.warning(
                    f"[GET_PAYMENT_STATUS_NOT_FOUND] payment_id={payment_id}, user_id={user_id}"
                )
                return {"success": False, "error": "Payment not found", "status": None}

            # SECURITY: Verify ownership - TokenBatch has wallet_id, not user_id
            if not token_batch.wallet or token_batch.wallet.user_id != user_id:
                logger.error(
                    f"[GET_PAYMENT_STATUS_UNAUTHORIZED] payment_id={payment_id}, "
                    f"requested_by={user_id}, actual_owner={token_batch.wallet.user_id if token_batch.wallet else 'UNKNOWN'}"
                )
                return {
                    "success": False,
                    "error": "Payment not found",  # Don't reveal it exists
                    "status": None,
                }
            status = token_batch.status.value
            if token_batch.status == TransactionStatus.PENDING:
                razorpay_result = await PaymentService.get_payment_order(payment_id=payment_id)

                razorpay_order = (
                    razorpay_result.get("data") if razorpay_result.get("success") else None
                )
                razorpay_status = razorpay_order.get("status") if razorpay_order else None

                if razorpay_status in ("COMPLETED", "FAILED", "CANCELLED"):
                    wallet_result = await WalletService.payment_webhook_handler(
                        payment_id,
                        session,
                        razorpay_status,
                        razorpay_order.get("amount"),
                        razorpay_order.get("currency"),
                        razorpay_order.get("payment_method"),
                        razorpay_order.get("payment_method_details"),
                    )
                    await session.commit()
                    if wallet_result.get("success"):
                        status = razorpay_status

            return {
                "success": True,
                "payment_id": token_batch.payment_id,
                "status": status,
                "tokens": token_batch.initial_tokens,
                "amount": float(token_batch.amount) if token_batch.amount else None,
                "currency": token_batch.currency,
                "source_type": token_batch.source_type.value,
                "created_at": token_batch.created_at.isoformat()
                if token_batch.created_at
                else None,
                "expires_at": token_batch.expires_at.isoformat()
                if token_batch.expires_at
                else None,
            }

        except Exception as e:
            logger.error(f"Error getting payment status: {e!s}", exc_info=True)
            return {"success": False, "error": "Failed to get payment status"}

    # ========================================================================
    # Transaction History
    # ========================================================================

    @staticmethod
    async def get_transactions(
        user_id: str,
        session: AsyncSession,
        limit: int = 50,
        offset: int = 0,
        transaction_type: str | None = None,
    ) -> dict[str, Any]:
        """Get transaction history for a user."""
        return await get_transaction_history(
            user_id=user_id,
            session=session,
            limit=limit,
            offset=offset,
            transaction_type=transaction_type,
        )

    # ========================================================================
    # Tier Information
    # ========================================================================

    @staticmethod
    def get_tier_info(tier: str, country_code: str = "default") -> dict[str, Any]:
        """Get information about a subscription tier."""
        country_key = country_code.lower() if country_code else "default"

        # Helper to get price for an interval
        def get_price(interval: str):
            tier_data = SUBSCRIPTION_INFO.get(interval, {}).get(tier, {})
            # Try specific country, fallback to default
            price_info = tier_data.get(country_key) or tier_data.get("default")
            return price_info

        return {
            "tier": tier,
            "monthly": get_price("monthly"),
            "yearly": get_price("yearly"),
            "signup_bonus": SIGNUP_BONUS_TOKENS,
            "report_cost": REPORT_GENERATION_COST,
        }

    @staticmethod
    def get_all_tiers(country_code: str = "default") -> dict[str, Any]:
        """Get information about all subscription tiers."""
        return {
            tier.value: WalletService.get_tier_info(tier.value, country_code)
            for tier in SubscriptionTier
        }

    @staticmethod
    def calculate_tax(country_code: str, amount: float, currency: str = "USD") -> dict[str, Any]:
        """
        Calculate tax based on country code, amount, and target currency.

        Args:
            country_code: ISO country code (e.g. 'in', 'us')
            amount: Amount to calculate tax on
            currency: Target currency code (e.g. 'INR', 'USD')

        Returns:
            Dict containing tax details, total amount, and tokens.
        """
        country_key = country_code.lower() if country_code else "default"

        # 1. Resolve base amount and token rate in target currency
        if currency == "INR":
            base_amount = amount
            token_rate = TOKEN_PRICE_INFO.get("INR", 6.25)
        else:
            base_amount = amount
            token_rate = TOKEN_PRICE_INFO.get("USD", 500)

        tax_info_list = TAX_INFO.get(country_key, [])

        total_tax_percentage = 0.0
        details = []

        # 2. Calculate total tax percentage and details
        for tax_item in tax_info_list:
            percentage = tax_item.get("tax_percentage", 0)
            total_tax_percentage += percentage
            details.append(
                {
                    "tax_name": tax_item.get("tax"),
                    "percentage": percentage,
                    "amount": base_amount * (percentage / 100.0),
                }
            )

        tax_amount = base_amount * (total_tax_percentage / 100.0)
        total_amount = base_amount + tax_amount
        tokens = int(base_amount * token_rate)

        return {
            "success": True,
            "country_code": country_code,
            "currency": currency,
            "amount_in_currency": base_amount,
            "tokens_per_month": tokens,  # For topup it's one-time tokens, but using same field name for consistency
            "tax_percentage": total_tax_percentage,
            "tax_amount": tax_amount,
            "total_amount": total_amount,
            "tax_details": details,
        }

    @staticmethod
    def calculate_subscription_tax(
        country_code: str, plan_name: str, plan_type: str
    ) -> dict[str, Any]:
        """
        Calculate tax for a subscription plan based on country code.
        """
        plan_type_key = plan_type.lower() if plan_type else ""
        plan_name_key = plan_name.lower() if plan_name else ""
        country_key = country_code.lower() if country_code else "default"

        # 1. Resolve pricing from SUBSCRIPTION_INFO
        plan_data = SUBSCRIPTION_INFO.get(plan_type_key, {}).get(plan_name_key, {})
        if not plan_data:
            return {
                "success": False,
                "error": f"Plan '{plan_name}' or type '{plan_type}' not found",
            }

        # Resolve price for country or fallback to default
        price_info = plan_data.get(country_key) or plan_data.get("default")
        if not price_info:
            return {"success": False, "error": f"Pricing not found for country '{country_code}'"}

        base_amount = float(price_info["cost"])
        currency = price_info["currency"]
        tokens_per_month = price_info.get("tokens_per_month", 0)

        # 2. Calculate Tax
        tax_info_list = TAX_INFO.get(country_key, [])
        total_tax_percentage = 0.0
        details = []

        for tax_item in tax_info_list:
            percentage = tax_item.get("tax_percentage", 0)
            total_tax_percentage += percentage
            details.append(
                {
                    "tax_name": tax_item.get("tax"),
                    "percentage": percentage,
                    "amount": base_amount * (percentage / 100.0),
                }
            )

        tax_amount = base_amount * (total_tax_percentage / 100.0)
        total_amount = base_amount + tax_amount

        return {
            "success": True,
            "country_code": country_code,
            "currency": currency,
            "amount_in_currency": base_amount,
            "tokens_per_month": tokens_per_month,
            "tax_percentage": total_tax_percentage,
            "tax_amount": tax_amount,
            "total_amount": total_amount,
            "tax_details": details,
        }

    @staticmethod
    async def update_payment_method(
        subscription_id: str, user_id: str, session: AsyncSession, cancel_at_cycle_end: bool = True
    ) -> dict[str, Any]:
        """
        Update payment method by canceling current subscription and creating new one.

        Steps:
        1. Get current subscription details (including current_end date)
        2. Verify user owns this subscription
        3. Cancel existing subscription (at cycle end or immediately)
        4. Create new subscription with start_at = old subscription's current_end
        5. Return auth_link for user to authorize new payment method

        Args:
            subscription_id: External subscription ID from payment service
            user_id: User ID to verify ownership
            session: Database session
            cancel_at_cycle_end: If True, cancel at cycle end; if False, cancel immediately

        Returns:
            Dict containing success status, new subscription details, and auth_link
        """
        logger.info(
            f"[UPDATE_PAYMENT_METHOD_START] subscription_id={subscription_id}, "
            f"user_id={user_id}, cancel_at_cycle_end={cancel_at_cycle_end}"
        )

        try:
            # Step 1: Get current subscription from DB
            subscription = await get_subscription_by_subscription_id(subscription_id, session)

            if not subscription:
                logger.error(f"[UPDATE_PAYMENT_METHOD_NOT_FOUND] subscription_id={subscription_id}")
                return {"success": False, "error": "Subscription not found"}

            # Step 2: Verify user owns this subscription
            if str(subscription.user_id) != str(user_id):
                logger.error(
                    f"[UPDATE_PAYMENT_METHOD_UNAUTHORIZED] subscription_id={subscription_id}, "
                    f"subscription_user={subscription.user_id}, requesting_user={user_id}"
                )
                return {
                    "success": False,
                    "error": "Unauthorized: Subscription does not belong to this user",
                }

            # Step 3: Check subscription is in a valid state for updating
            valid_statuses = [
                SubscriptionStatus.CREATED,
                SubscriptionStatus.AUTHENTICATED,
                SubscriptionStatus.ACTIVE,
                SubscriptionStatus.PENDING,
            ]

            if subscription.status not in valid_statuses:
                logger.error(
                    f"[UPDATE_PAYMENT_METHOD_INVALID_STATUS] subscription_id={subscription_id}, "
                    f"status={subscription.status.value}, valid_statuses={[s.value for s in valid_statuses]}"
                )
                return {
                    "success": False,
                    "error": f"Cannot update payment method for subscription with status: {subscription.status.value}. "
                    f"Valid statuses: {', '.join([s.value for s in valid_statuses])}",
                }

            # Step 4: Get current subscription details from Payment Service
            payment_service_url = CASPR_PAYMENT_BASE_URL
            api_key = CASPR_PAYMENT_API_KEY

            if not payment_service_url or not api_key:
                logger.error(
                    "[UPDATE_PAYMENT_METHOD_CONFIG_ERROR] Payment service configuration missing"
                )
                return {"success": False, "error": "Payment service configuration error"}

            headers = {"X-API-Key": api_key}

            # Get subscription details to obtain current_end
            async with httpx.AsyncClient() as client:
                get_response = await client.get(
                    f"{payment_service_url}/api/v1/subscription",
                    params={"subscription_id": subscription_id},
                    headers=headers,
                    timeout=30.0,
                )

                if get_response.status_code != 200:
                    logger.error(
                        f"[UPDATE_PAYMENT_METHOD_GET_FAILED] subscription_id={subscription_id}, "
                        f"status={get_response.status_code}, response={get_response.text}"
                    )
                    return {
                        "success": False,
                        "error": "Failed to get current subscription details from payment service",
                    }

                subscription_data = get_response.json().get("data", {})
                current_end = subscription_data.get("current_end")
                subscription_status = subscription_data.get("status", "").upper()

                # For CREATED/AUTHENTICATED subscriptions without current_end,
                # new subscription can start immediately
                if not current_end and subscription_status in ["CREATED", "AUTHENTICATED"]:
                    logger.info(
                        f"[UPDATE_PAYMENT_METHOD_NO_CYCLE] subscription_id={subscription_id}, "
                        f"status={subscription_status}, creating immediate subscription"
                    )
                    current_end = None  # Will create subscription without start_at
                elif not current_end:
                    logger.error(
                        f"[UPDATE_PAYMENT_METHOD_NO_CURRENT_END] subscription_id={subscription_id}, "
                        f"status={subscription_status}"
                    )
                    return {
                        "success": False,
                        "error": "Cannot determine subscription cycle end date",
                    }
                else:
                    logger.info(
                        f"[UPDATE_PAYMENT_METHOD_CURRENT_END] subscription_id={subscription_id}, "
                        f"current_end={current_end}"
                    )

            # Step 5: Cancel existing subscription
            async with httpx.AsyncClient() as client:
                cancel_response = await client.post(
                    f"{payment_service_url}/api/v1/subscription/cancel",
                    json={
                        "subscription_id": subscription_id,
                        "cancel_at_cycle_end": cancel_at_cycle_end,
                        "reason": "User requested payment method update",
                    },
                    headers=headers,
                    timeout=30.0,
                )

                if cancel_response.status_code != 200:
                    logger.error(
                        f"[UPDATE_PAYMENT_METHOD_CANCEL_FAILED] subscription_id={subscription_id}, "
                        f"status={cancel_response.status_code}, response={cancel_response.text}"
                    )
                    return {"success": False, "error": "Failed to cancel existing subscription"}

                logger.info(
                    f"[UPDATE_PAYMENT_METHOD_CANCELLED] subscription_id={subscription_id}, "
                    f"cancel_at_cycle_end={cancel_at_cycle_end}"
                )

            # Step 6: Create new subscription with same plan, starting at current_end
            plan_tier = (
                subscription.meta_data.get("plan_tier", "PRO") if subscription.meta_data else "PRO"
            )
            plan_duration = (
                (subscription.meta_data.get("plan_duration") or "monthly").lower()
                if subscription.meta_data
                else "monthly"
            )
            country = (
                subscription.meta_data.get("country", "US") if subscription.meta_data else "US"
            )

            # Get user details
            user = await session.get(User, user_id)
            if not user:
                logger.error(f"[UPDATE_PAYMENT_METHOD_USER_NOT_FOUND] user_id={user_id}")
                return {"success": False, "error": "User not found"}

            new_idempotency_key = f"sub_payment_update_{user_id}_{int(time.time() * 1000)}"

            async with httpx.AsyncClient() as client:
                create_payload = {
                    "idempotency_key": new_idempotency_key,
                    "plan_tier": plan_tier,
                    "plan_duration": plan_duration,
                    "country": country,
                    "user_data": {
                        "email": user.email,
                        "user_id": str(user_id),
                        "name": user.name if hasattr(user, "name") else user.email.split("@")[0],
                    },
                }

                # Only set start_at if we have a current_end (for active subscriptions)
                if current_end:
                    create_payload["start_at"] = current_end

                create_response = await client.post(
                    f"{payment_service_url}/api/v1/subscription",
                    json=create_payload,
                    headers=headers,
                    timeout=30.0,
                )

                if create_response.status_code != 200:
                    logger.error(
                        f"[UPDATE_PAYMENT_METHOD_CREATE_FAILED] user_id={user_id}, "
                        f"status={create_response.status_code}, response={create_response.text}"
                    )
                    return {"success": False, "error": "Failed to create new subscription"}

                new_subscription_data = create_response.json().get("data", {})
                new_subscription_id = new_subscription_data.get("subscription_id")
                auth_link = new_subscription_data.get("auth_link")

            logger.info(
                f"[UPDATE_PAYMENT_METHOD_SUCCESS] old_subscription={subscription_id}, "
                f"new_subscription={new_subscription_id}, current_end={current_end}"
            )

            # Build response with appropriate instructions based on subscription state
            if current_end:
                # Active subscription with billing cycle
                instructions = [
                    "1. Your current subscription will remain active until the cycle end",
                    "2. Visit the auth_link to authorize your new payment method",
                    f"3. On {current_end}, your old subscription will end and the new one will start",
                    "4. You'll maintain uninterrupted access throughout the transition",
                ]
            else:
                # CREATED/AUTHENTICATED subscription without active billing
                instructions = [
                    "1. Your previous subscription has been cancelled",
                    "2. Visit the auth_link to authorize your new subscription with the new payment method",
                    "3. The new subscription will start immediately after authorization",
                    "4. No billing has occurred yet on the old subscription",
                ]

            return {
                "success": True,
                "message": "Payment method update initiated. Please authorize the new subscription.",
                "data": {
                    "old_subscription_id": subscription_id,
                    "new_subscription_id": new_subscription_id,
                    "auth_link": auth_link,
                    "transition_date": current_end,
                    "cancel_at_cycle_end": cancel_at_cycle_end,
                    "plan_tier": plan_tier,
                    "plan_duration": plan_duration,
                    "instructions": instructions,
                },
            }

        except httpx.HTTPError as e:
            logger.error(
                f"[UPDATE_PAYMENT_METHOD_HTTP_ERROR] subscription_id={subscription_id}, "
                f"error={e!s}",
                exc_info=True,
            )
            return {"success": False, "error": f"Payment service communication error: {e!s}"}
        except Exception as e:
            logger.error(
                f"[UPDATE_PAYMENT_METHOD_EXCEPTION] subscription_id={subscription_id}, error={e!s}",
                exc_info=True,
            )
            return {"success": False, "error": f"Internal error: {e!s}"}
