"""
wallet_functions.py
Database operation functions for wallet, subscription, and token management.
"""

from datetime import UTC, datetime, timedelta
from typing import Any

from dateutil.relativedelta import relativedelta
from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from uuid_utils import uuid7

from app.core.constants import SIGNUP_BONUS_TOKENS, TIER_COST_USD, TOKEN_EXPIRY_DAYS, X_API_KEY
from app.core.enums import (
    SubscriptionStatus,
    SubscriptionTier,
    TransactionSource,
    TransactionStatus,
    TransactionType,
)
from app.core.logging import setup_logging
from app.models import Subscription, SubscriptionInterval, TokenBatch, TokenTransaction, Wallet

logger = setup_logging(__file__)


# ============================================================================
# Helper: Update Wallet Balances (sync with TokenBatch)
# ============================================================================


async def _sync_wallet_balances(wallet_id: str, session: AsyncSession) -> dict[str, int]:
    """Calculate and update wallet balances from TokenBatch (internal helper)."""
    now = datetime.now(UTC)

    # Calculate balances from TokenBatch (expiry checked via expires_at > now)
    result = await session.execute(
        select(
            func.coalesce(func.sum(TokenBatch.remaining_tokens), 0).label("available"),
            func.coalesce(func.sum(TokenBatch.reserved_tokens), 0).label("reserved"),
        ).where(
            TokenBatch.wallet_id == wallet_id,
            TokenBatch.expires_at > now,
            TokenBatch.start_at <= now,
            TokenBatch.status == TransactionStatus.COMPLETED,
        )
    )
    row = result.fetchone()
    available = int(row.available) if row else 0
    reserved = int(row.reserved) if row else 0

    # Update wallet table
    await session.execute(
        update(Wallet)
        .where(Wallet.id == wallet_id)
        .values(available_balance=available, reserved_balance=reserved, updated_at=now)
    )

    return {"available_balance": available, "reserved_balance": reserved}


# ============================================================================
# Wallet Operations
# ============================================================================


async def create_wallet(
    user_id: str, session: AsyncSession, tier: str = SubscriptionTier.FREE.value
) -> dict[str, Any]:
    """Create a new wallet for a user with signup bonus tokens."""
    logger.info(f"Creating wallet for user_id: {user_id}, tier: {tier}")

    try:
        existing = await session.execute(select(Wallet).where(Wallet.user_id == user_id))
        if existing.scalar_one_or_none():
            return {"success": False, "error": "Wallet already exists for this user"}

        wallet_id = str(uuid7())
        now = datetime.now(UTC)

        # Create wallet with initial balances
        wallet = Wallet(
            id=wallet_id, user_id=user_id, available_balance=SIGNUP_BONUS_TOKENS, reserved_balance=0
        )
        session.add(wallet)
        await session.flush()

        # Create signup bonus token batch
        batch_id = str(uuid7())
        expires_at = now + timedelta(days=TOKEN_EXPIRY_DAYS)

        token_batch = TokenBatch(
            id=batch_id,
            wallet_id=wallet_id,
            initial_tokens=SIGNUP_BONUS_TOKENS,
            remaining_tokens=SIGNUP_BONUS_TOKENS,
            reserved_tokens=0,
            source_type=TransactionSource.SIGNUP_BONUS,
            status=TransactionStatus.COMPLETED,
            payment_id=None,
            start_at=now,  # Signup bonus tokens are immediately available
            expires_at=expires_at,
        )
        session.add(token_batch)
        await session.flush()

        # Create transaction record
        transaction = TokenTransaction(
            wallet_id=wallet_id,
            transaction_type=TransactionType.CREDIT,
            source_type=TransactionSource.SIGNUP_BONUS,
            tokens=SIGNUP_BONUS_TOKENS,
            balance_after=SIGNUP_BONUS_TOKENS,
            reserved_after=0,
            report_id=None,
            token_batch_id=batch_id,
            status=TransactionStatus.COMPLETED,
            description=f"Signup bonus - {SIGNUP_BONUS_TOKENS:,} tokens",
        )
        session.add(transaction)
        await session.flush()  # Use flush instead of commit to keep transaction open for rollback

        logger.info(f"Wallet created for user_id: {user_id}")
        return {
            "success": True,
            "wallet_id": wallet_id,
            "tier": tier,
            "tokens_credited": SIGNUP_BONUS_TOKENS,
            "batch_id": batch_id,
            "expires_at": expires_at.isoformat(),
        }

    except SQLAlchemyError as e:
        logger.error(f"Database error creating wallet: {e!s}", exc_info=True)
        await session.rollback()
        return {"success": False, "error": f"Database error: {e!s}"}
    except Exception as e:
        logger.error(f"Unexpected error creating wallet: {e!s}", exc_info=True)
        await session.rollback()
        return {"success": False, "error": str(e)}


async def get_wallet_by_user_id(user_id: str, session: AsyncSession) -> dict[str, Any]:
    """Get wallet details by user ID."""
    try:
        result = await session.execute(select(Wallet).where(Wallet.user_id == user_id))
        wallet = result.scalar_one_or_none()
        if not wallet:
            return {"success": True, "wallet": None}

        # Get subscription tier from Subscription table
        sub_result = await session.execute(
            select(Subscription).where(Subscription.user_id == user_id)
        )
        subscription = sub_result.scalar_one_or_none()

        return {
            "success": True,
            "wallet": {
                "id": wallet.id,
                "user_id": wallet.user_id,
                "available_balance": wallet.available_balance,
                "reserved_balance": wallet.reserved_balance,
                "created_at": wallet.created_at.isoformat(),
                "subscription_tier": subscription.current_tier.value
                if subscription
                else SubscriptionTier.FREE.value,
                "subscription_status": subscription.status.value if subscription else None,
            },
        }
    except SQLAlchemyError as e:
        logger.error(f"Database error getting wallet: {e!s}", exc_info=True)
        return {"success": False, "error": f"Database error: {e!s}"}


async def get_wallet_balance(
    user_id: str,
    session: AsyncSession,
    wallet_id: str | None = None,
    subscription_tier: str | None = None,
) -> dict[str, Any]:
    """Get wallet balance (calculated from TokenBatch)."""
    try:
        # Get wallet id if not provided
        if not wallet_id:
            result = await session.execute(select(Wallet.id).where(Wallet.user_id == user_id))
            wallet_id = result.scalar_one_or_none()

        if not wallet_id:
            return {
                "success": True,
                "available_balance": 0,
                "reserved_balance": 0,
                "total_balance": 0,
            }

        now = datetime.now(UTC)

        # Calculate balances from TokenBatch (source of truth)
        balance_result = await session.execute(
            select(
                func.coalesce(func.sum(TokenBatch.remaining_tokens), 0).label("available"),
                func.coalesce(func.sum(TokenBatch.reserved_tokens), 0).label("reserved"),
            ).where(
                TokenBatch.wallet_id == wallet_id,
                TokenBatch.expires_at > now,
                TokenBatch.start_at <= now,
                TokenBatch.status == TransactionStatus.COMPLETED,
            )
        )

        balance_row = balance_result.fetchone()
        available = int(balance_row.available) if balance_row else 0
        reserved = int(balance_row.reserved) if balance_row else 0

        return {
            "success": True,
            "wallet_id": wallet_id,
            "available_balance": available - reserved,
            "reserved_balance": reserved,
            "total_balance": available,
        }

    except SQLAlchemyError as e:
        logger.error(f"Database error getting balance: {e!s}", exc_info=True)
        return {"success": False, "error": f"Database error: {e!s}"}


async def get_wallet_id_by_user_id(user_id: str, session: AsyncSession) -> str | None:
    """Get wallet ID for a user."""
    result = await session.execute(select(Wallet.id).where(Wallet.user_id == user_id))
    return result.scalar_one_or_none()


async def get_user_email(user_id: str, session: AsyncSession) -> str | None:
    """Get user email by user ID."""
    from app.models import User

    result = await session.execute(select(User.email).where(User.id == user_id))
    return result.scalar_one_or_none()


# ============================================================================
# Subscription Operations
# ============================================================================


async def get_or_create_subscription(
    user_id: str, session: AsyncSession, subscription_id_ext: str | None = None
) -> Subscription:
    """Get or create the parent subscription record for a user."""
    result = await session.execute(select(Subscription).where(Subscription.user_id == user_id))
    subscription = result.scalar_one_or_none()

    if not subscription:
        subscription = Subscription(
            id=str(uuid7()),
            user_id=user_id,
            subscription_id=subscription_id_ext,
            current_tier=SubscriptionTier.FREE,
            status=SubscriptionStatus.ACTIVE,
        )
        session.add(subscription)
        await session.flush()
    elif subscription_id_ext and subscription.subscription_id != subscription_id_ext:
        subscription.subscription_id = subscription_id_ext
        await session.flush()

    return subscription


async def get_subscription_by_user_id(user_id: str, session: AsyncSession) -> Subscription | None:
    """Plain getter for subscription object."""
    result = await session.execute(
        select(Subscription)
        .where(Subscription.user_id == user_id)
        .order_by(Subscription.created_at.desc())
    )
    return result.scalars().all()


async def get_subscription_by_subscription_id(
    subscription_id: str, session: AsyncSession
) -> Subscription | None:
    """Plain getter for subscription object."""
    result = await session.execute(
        select(Subscription).where(Subscription.subscription_id == subscription_id)
    )
    return result.scalar_one_or_none()


async def create_subscription_interval(
    user_id: str,
    tier: str,
    session: AsyncSession,
    payment_id: str,
    tokens_per_month: int,
    start_date: datetime,
    end_date: datetime,
    subscription_id_ext: str | None = None,
    metadata: dict[str, Any] | None = None,
    sub_metadata: dict[str, Any] | None = None,
    subscription: Subscription | None = None,
    wallet_id: str | None = None,
    plan_duration: str | None = "monthly",
) -> dict[str, Any]:
    """Create a new subscription interval (billing cycle) and credit tokens."""
    logger.info(f"Creating subscription interval for user_id: {user_id}, tier: {tier}")

    try:
        now = datetime.now(UTC)
        # subscription_intervals.payment_id is NOT NULL; ensure we never pass None/empty
        if not payment_id or not str(payment_id).strip():
            payment_id = f"si_{subscription_id_ext or user_id}_{int(now.timestamp())}"[:50]

        # Get parent subscription if not provided
        if not subscription:
            subscription = await get_subscription_by_subscription_id(subscription_id_ext, session)

        if not subscription:
            return {"success": False, "error": "Subscription record not found"}

        # Convert tier string to enum
        tier_enum = SubscriptionTier(tier)

        # Expire previous active intervals
        await session.execute(
            update(SubscriptionInterval)
            .where(
                SubscriptionInterval.subscription_id == subscription.id,
                SubscriptionInterval.status == SubscriptionStatus.CHARGED,
            )
            .values(status=SubscriptionStatus.EXPIRED)
        )  # TODO de we need to expire all the old intervals?

        # Get wallet if not provided
        if not wallet_id:
            wallet_id = await get_wallet_id_by_user_id(user_id, session)

        if not wallet_id:
            return {"success": False, "error": "Wallet not found"}

        # Determine number of batches (splitting for yearly)
        # Check plan duration from metadata or assume based on start/end date?
        # A simpler robust check: if end_date - start_date > 300 days roughly.
        if plan_duration == "yearly":
            is_yearly = True
        else:
            is_yearly = False

        splits = 12 if is_yearly else 1

        first_batch_id = None
        current_balance = 0
        token_batch_ids = []
        batch_info_list = []  # Store batch info for transaction creation

        # Calculate per-batch amount for accurate transaction history
        # For yearly subscriptions, divide total amount by 12 months
        total_amount = metadata.get("amount") if metadata else None
        per_batch_amount = (total_amount / splits) if total_amount and splits > 1 else total_amount

        # Create all token batches first
        for i in range(splits):
            batch_start_at = now + relativedelta(
                months=i
            )  # Proper monthly calculation accounting for varying month durations
            batch_tokens = tokens_per_month

            # Create token batch for subscription
            batch_id = str(uuid7())
            expires_at = batch_start_at + timedelta(days=TOKEN_EXPIRY_DAYS)

            # Prepare batch metadata with subscription_id for tracking
            batch_metadata = {**(metadata or {}), "subscription_id": subscription.id}

            token_batch = TokenBatch(
                id=batch_id,
                wallet_id=wallet_id,
                initial_tokens=batch_tokens,
                remaining_tokens=batch_tokens,
                reserved_tokens=0,
                amount=per_batch_amount,  # Use calculated per-batch amount
                currency=metadata.get("currency")
                if metadata and metadata.get("currency")
                else None,
                source_type=TransactionSource.SUBSCRIPTION,
                payment_id=payment_id,
                start_at=batch_start_at,
                expires_at=expires_at,
                meta_data=batch_metadata,  # Include subscription_id for invalidation queries
                status=TransactionStatus.COMPLETED,  # Subscription batches are completed (paid) source
            )
            session.add(token_batch)
            token_batch_ids.append(batch_id)
            batch_info_list.append({"batch_id": batch_id, "batch_tokens": batch_tokens})

            if i == 0:
                first_batch_id = batch_id

        # Flush all batches to database
        await session.flush()

        # Now sync wallet balance once after all batches are added
        balances = await _sync_wallet_balances(wallet_id, session)

        # Create transaction records for each batch with correct balance
        for batch_info in batch_info_list:
            transaction = TokenTransaction(
                id=str(uuid7()),
                wallet_id=wallet_id,
                transaction_type=TransactionType.CREDIT,
                source_type=TransactionSource.SUBSCRIPTION,
                tokens=batch_info["batch_tokens"],
                balance_after=balances["available_balance"],
                reserved_after=balances["reserved_balance"],
                report_id=None,
                token_batch_id=batch_info["batch_id"],
                status=TransactionStatus.COMPLETED,
                description=f"{tier.title()} subscription - {batch_info['batch_tokens']:,} tokens {'(Monthly Unlock)' if is_yearly else ''}",
                meta_data={
                    "subscription_id": subscription.id,
                    "tier": tier,
                    "payment_id": payment_id,
                    "is_yearly": is_yearly,
                },
            )
            session.add(transaction)

        await session.flush()

        # Create subscription interval record
        interval_id = str(uuid7())
        interval = SubscriptionInterval(
            id=interval_id,
            subscription_id=subscription.id,
            tier=tier_enum,
            start_date=start_date,
            end_date=end_date,
            payment_id=payment_id,
            amount=sub_metadata.get("amount") if sub_metadata else TIER_COST_USD.get(tier),
            currency=sub_metadata.get("currency") if sub_metadata else None,
            tokens_credited=batch_tokens,
            token_batch_id=first_batch_id,
            meta_data={
                "token_batch_ids": token_batch_ids,
                "plan_duration": (plan_duration or "monthly").lower(),
                **sub_metadata,
            },
            status=SubscriptionStatus.CHARGED,
        )
        session.add(interval)

        # Update parent subscription tier (status is set by SUBSCRIPTION_ACTIVATED event)
        subscription.current_tier = tier_enum
        subscription.updated_at = now

        # Sync meta_data to parent subscription if gateway is present
        if sub_metadata and sub_metadata.get("gateway"):
            if not subscription.meta_data:
                subscription.meta_data = {}
            subscription.meta_data.update(sub_metadata)

        # Sync wallet balances
        await session.flush()
        current_balance = balances["available_balance"]

        # Create transaction record (One transaction for the FULL subscription, or per batch?)
        # For user clarity, maybe one transaction "Yearly Subscription - 300,000 tokens"
        # but availability is restricted by batches.
        # However, listing 300k and seeing 25k balance is confusing.
        # So we should probably log 12 transactions?
        # But 'process_subscription_charge' returns one 'tokens_credited'.

        # Let's create transactions corresponding to batches for transparency if they look at history
        # (Assuming history view shows date).
        # Actually, simpler: 1 transaction linked to first batch.
        # ISSUE: If we have 1 transaction saying "Credit 300k", but balance increases by 25k.
        # The balance_after in ledger would show current balance.
        # This might be acceptable. "Subscription verified. Tokens will unlock monthly."
        # Or create 12 transactions.

        # Let's stick to 1 transaction for now to minimize noise, linked to first batch.

        await session.commit()

        logger.info(f"Subscription interval created: {interval_id}")
        return {
            "success": True,
            "subscription_id": subscription.id,
            "interval_id": interval_id,
            "tier": tier,
            "tokens_credited": tokens_per_month,
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "balance_after": current_balance,
        }

    except SQLAlchemyError as e:
        logger.error(f"Database error creating subscription interval: {e!s}", exc_info=True)
        await session.rollback()
        return {"success": False, "error": f"Database error: {e!s}"}


async def get_active_subscription(
    session: AsyncSession,
    user_id: str | None = None,
    subscription_id: str | None = None,
    filter_created_status: bool | None = False,
) -> dict[str, Any]:
    """Get the active subscription and current interval for a user."""
    try:
        if not user_id and not subscription_id:
            raise ValueError("Either user_id or subscription_id must be provided")

        now = datetime.now(UTC)

        # Get parent subscription - only return ACTIVE subscriptions
        if user_id:
            stmt = (
                select(Subscription)
                .where(Subscription.user_id == user_id)
                .order_by(Subscription.created_at.desc())
            )

            if filter_created_status:
                stmt = stmt.where(Subscription.status != SubscriptionStatus.CREATED)
        else:
            stmt = (
                select(Subscription)
                .where(Subscription.id == subscription_id)
                .order_by(Subscription.created_at.desc())
            )

            if filter_created_status:
                stmt = stmt.where(Subscription.status != SubscriptionStatus.CREATED)

        sub_result = await session.execute(stmt)
        subscriptions = sub_result.scalars().all()

        if not subscriptions:
            # No subscription found is a valid state (user is on FREE tier)
            return {"success": True, "subscription": None, "current_interval": None}
        subscription = None
        current_interval = None
        for sub in subscriptions:
            # Get active interval
            interval_result = await session.execute(
                select(SubscriptionInterval)
                .where(
                    SubscriptionInterval.subscription_id == sub.id,
                    SubscriptionInterval.status == SubscriptionStatus.CHARGED,
                    SubscriptionInterval.end_date >= now,
                    SubscriptionInterval.start_date <= now,
                )
                .order_by(SubscriptionInterval.end_date.desc())
            )
            current_intervals = interval_result.scalars().all()
            if len(current_intervals) > 1:
                logger.error(
                    f"Multiple active intervals found for user {user_id} and subscription {sub.id}"
                )

            current_interval = current_intervals[0] if current_intervals else None

            if current_interval:
                subscription = sub
                break

        if not subscription:
            subscription = subscriptions[0] if subscriptions else None
        if not subscription:
            return {"success": True, "subscription": None, "current_interval": None}

        # Determine effective tier and status based on current interval
        if current_interval:
            # Has active interval - subscription is ACTIVE with its stored tier
            subscription_status = SubscriptionStatus.ACTIVE.value
            effective_tier = subscription.current_tier.value
        else:
            # No active interval - user should be treated as FREE tier
            effective_tier = SubscriptionTier.FREE.value
            if subscription.status == SubscriptionStatus.ACTIVE:
                subscription_status = SubscriptionStatus.PENDING.value
            else:
                subscription_status = subscription.status.value

        return {
            "success": True,
            "subscription": {
                "id": subscription.id,
                "current_tier": effective_tier,
                "subscription_id": subscription.id,
                "external_subscription_id": subscription.subscription_id,
                "status": subscription_status,
                "meta_data": subscription.meta_data,
            },
            "current_interval": {
                "id": current_interval.id,
                "tier": current_interval.tier.value,
                "start_date": current_interval.start_date.isoformat(),
                "end_date": current_interval.end_date.isoformat(),
                "tokens_credited": current_interval.tokens_credited,
                "token_batch_id": current_interval.token_batch_id,
                "status": current_interval.status.value,
            }
            if current_interval
            else None,
        }
    except SQLAlchemyError as e:
        logger.error(f"Database error getting subscription: {e!s}", exc_info=True)
        return {"success": False, "error": f"Database error: {e!s}"}


async def get_subscription_history(
    user_id: str, session: AsyncSession, limit: int = 10
) -> dict[str, Any]:
    """Get subscription interval history for a user."""
    try:
        # Get parent subscription
        sub_result = await session.execute(
            select(Subscription).where(Subscription.user_id == user_id)
        )
        subscription = sub_result.scalar_one_or_none()

        if not subscription:
            return {"success": True, "intervals": []}

        # Get all intervals
        result = await session.execute(
            select(SubscriptionInterval)
            .where(SubscriptionInterval.subscription_id == subscription.id)
            .order_by(SubscriptionInterval.start_date.desc())
            .limit(limit)
        )
        intervals = result.scalars().all()

        return {
            "success": True,
            "intervals": [
                {
                    "id": i.id,
                    "tier": i.tier.value,
                    "start_date": i.start_date.isoformat(),
                    "end_date": i.end_date.isoformat(),
                    "payment_id": i.payment_id,
                    "amount": float(i.amount) if i.amount else None,
                    "tokens_credited": i.tokens_credited,
                    "status": i.status.value,
                    "created_at": i.created_at.isoformat(),
                }
                for i in intervals
            ],
        }
    except SQLAlchemyError as e:
        logger.error(f"Database error getting subscription history: {e!s}", exc_info=True)
        return {"success": False, "error": f"Database error: {e!s}"}


# Already handled above in standardized getter.


async def create_pending_subscription(
    user_id: str,
    tier: str,
    subscription_id: str,  # External ID
    session: AsyncSession,
    amount: float = 0.0,
    tokens_per_month: int = 0,
    # Legacy/Optional arguments
    start_date: datetime | None = None,
    end_date: datetime | None = None,
    currency: str = "USD",
    tokens_credited: int = 0,
    metadata: dict[str, Any] | None = None,
    subscription: Subscription | None = None,
) -> dict[str, Any]:
    """Create pending subscription and interval records (before payment confirmation)."""
    logger.info(
        f"Creating pending subscription for user_id: {user_id}, tier: {tier}, external_id: {subscription_id}"
    )

    try:
        # Check if user already has a subscription if not provided
        # if not subscription:
        #     existing_sub = await session.execute(select(Subscription).where(Subscription.user_id == user_id))
        #     subscription = existing_sub.scalar_one_or_none()

        # if subscription:
        #     return {"success": False, "error": "User already has a subscription"}

        # Get wallet
        wallet_id = await get_wallet_id_by_user_id(user_id, session)
        if not wallet_id:
            return {"success": False, "error": "Wallet not found"}

        # Convert tier string to enum
        tier_enum = SubscriptionTier(tier)

        # Create parent subscription
        subscription_id_internal = str(uuid7())
        subscription = Subscription(
            id=subscription_id_internal,
            user_id=user_id,
            subscription_id=subscription_id,
            current_tier=tier_enum,
            status=SubscriptionStatus.CREATED,
            meta_data=metadata,
        )
        session.add(subscription)
        await session.flush()

        await session.commit()

        logger.info(f"Pending subscription created: subscription_id={subscription_id_internal}")
        return {
            "success": True,
            "subscription_id": subscription_id_internal,
            "external_subscription_id": subscription_id,
        }

    except SQLAlchemyError as e:
        logger.error(f"Database error creating pending subscription: {e!s}", exc_info=True)
        await session.rollback()
        return {"success": False, "error": f"Database error: {e!s}"}
    except Exception as e:
        logger.error(f"Unexpected error creating pending subscription: {e!s}", exc_info=True)
        await session.rollback()
        return {"success": False, "error": str(e)}


async def update_subscription_metadata(
    subscription_id_ext: str,
    metadata: dict[str, Any],
    session: AsyncSession,
    status: SubscriptionStatus | None = None,
    current_tier: SubscriptionTier | None = None,
) -> dict[str, Any]:
    """Update subscription metadata (usually from AUTHENTICATED event)."""
    try:
        result = await session.execute(
            select(Subscription)
            .where(Subscription.subscription_id == subscription_id_ext)
            .with_for_update()
        )
        subscription = result.scalar_one_or_none()

        if not subscription:
            return {"success": False, "error": "Subscription not found"}

        # Merge metadata
        current_meta = subscription.meta_data or {}
        # SQLA JSON columns can sometimes be tricky with in-place updates
        new_meta = dict(current_meta)
        new_meta.update(metadata)
        subscription.meta_data = new_meta
        if status:
            subscription.status = status
        if current_tier:
            subscription.current_tier = current_tier

        await session.flush()
        return {"success": True}
    except Exception as e:
        logger.error(f"Error updating subscription metadata: {e!s}", exc_info=True)
        return {"success": False, "error": str(e)}


async def process_subscription_charge(
    subscription_id_ext: str,
    tier: str,
    tokens_per_month: int,
    start_date: datetime,
    end_date: datetime,
    session: AsyncSession,
    payment_id: str | None = None,
    metadata: dict[str, Any] | None = None,
    sub_metadata: dict[str, Any] | None = None,
    subscription: Subscription | None = None,
    plan_duration: str | None = "monthly",
) -> dict[str, Any]:
    """Process a successful subscription charge using existing interval logic."""
    try:
        if not subscription:
            subscription = await get_subscription_by_subscription_id(
                subscription_id=subscription_id_ext, session=session
            )

        if not subscription:
            return {"success": False, "error": "Subscription not found for external ID"}

        return await create_subscription_interval(
            user_id=subscription.user_id,
            tier=tier.lower(),
            session=session,
            start_date=start_date,
            end_date=end_date,
            payment_id=payment_id,
            tokens_per_month=tokens_per_month,
            subscription_id_ext=subscription_id_ext,
            metadata=metadata,
            sub_metadata=sub_metadata,
            subscription=subscription,
            plan_duration=plan_duration,
        )
    except Exception as e:
        logger.error(f"Error processing subscription charge: {e!s}", exc_info=True)
        return {"success": False, "error": str(e)}


async def cancel_future_subscription_batches(
    wallet_id: str, subscription_id: str, session: AsyncSession
) -> dict[str, Any]:
    """
    Cancel all future token batches from a subscription that haven't started yet.
    This is used when upgrading/downgrading subscriptions.

    Args:
        wallet_id: The wallet ID
        subscription_id: The internal subscription ID (not external)
        session: Database session

    Returns:
        Dict with success status, cancelled_count, and cancelled_tokens
    """
    logger.info(f"[CANCEL_FUTURE_BATCHES] wallet_id={wallet_id}, subscription_id={subscription_id}")

    try:
        now = datetime.now(UTC)

        # Find all future batches from this subscription
        # These are batches with start_at > now that user hasn't accessed yet
        result = await session.execute(
            select(TokenBatch)
            .where(
                TokenBatch.wallet_id == wallet_id,
                TokenBatch.source_type == TransactionSource.SUBSCRIPTION,
                TokenBatch.start_at > now,  # Future batches only
                TokenBatch.status == TransactionStatus.COMPLETED,  # Only cancel paid batches
                TokenBatch.meta_data["subscription_id"].astext
                == subscription_id,  # Match subscription
            )
            .with_for_update()
        )
        future_batches = result.scalars().all()

        cancelled_count = 0
        cancelled_tokens = 0

        for batch in future_batches:
            # Mark batch as cancelled
            batch.status = TransactionStatus.REVERSED

            # Track cancellations
            cancelled_count += 1
            cancelled_tokens += batch.initial_tokens

            logger.info(
                f"[CANCEL_BATCH] batch_id={batch.id}, "
                f"start_at={batch.start_at.isoformat()}, "
                f"tokens={batch.initial_tokens}"
            )

        await session.flush()

        logger.info(
            f"[CANCEL_FUTURE_BATCHES_SUCCESS] wallet_id={wallet_id}, "
            f"cancelled_count={cancelled_count}, cancelled_tokens={cancelled_tokens}"
        )

        return {
            "success": True,
            "cancelled_count": cancelled_count,
            "cancelled_tokens": cancelled_tokens,
        }

    except SQLAlchemyError as e:
        logger.error(
            f"[CANCEL_FUTURE_BATCHES_ERROR] wallet_id={wallet_id}, "
            f"subscription_id={subscription_id}, error={e!s}",
            exc_info=True,
        )
        return {
            "success": False,
            "error": f"Database error: {e!s}",
            "cancelled_count": 0,
            "cancelled_tokens": 0,
        }
    except Exception as e:
        logger.error(
            f"[CANCEL_FUTURE_BATCHES_EXCEPTION] wallet_id={wallet_id}, "
            f"subscription_id={subscription_id}, error={e!s}",
            exc_info=True,
        )
        return {"success": False, "error": str(e), "cancelled_count": 0, "cancelled_tokens": 0}


async def invalidate_subscription_batches_and_get_reserved(
    wallet_id: str,
    subscription_id: str,
    session: AsyncSession,
    source_plan_duration: str = "monthly",
) -> dict[str, Any]:
    """
    Invalidate ALL token batches from a subscription and carry forward remaining tokens.

    Used when changing subscription plans. This function:
    1. Finds ALL batches from the old subscription (past, current, and future)
    2. Invalidates all batches by setting status to INVALIDATED
    3. Calculates total remaining tokens across all batches
    4. These remaining tokens are carried forward to the new subscription plan

    This approach ensures clean accounting:
    - For monthly plans: Invalidates current batch + any old expired batches
    - For yearly plans: Invalidates all 12 batches (regardless of start_at)
    - No mixed batches from different subscriptions in the wallet
    - All tokens from old plan properly accounted for in new plan

    Args:
        wallet_id: The wallet ID
        subscription_id: The internal subscription ID (not external)
        session: Database session
        source_plan_duration: Duration of the OLD plan ("monthly" or "yearly")

    Returns:
        Dict with:
            - success: bool
            - invalidated_count: int (number of batches invalidated)
            - total_remaining_tokens: int (sum of remaining_tokens from ALL invalidated batches)
            - total_reserved_tokens: int (sum of reserved_tokens from ALL invalidated batches)
            - total_available_tokens: int (remaining - reserved from ALL invalidated batches)
            - invalidated_batches: list of invalidated batch details

    Note: Invalidates ALL batches from the old subscription regardless of start_at.
    This includes expired batches (0 remaining), current batches, and future batches.
    """
    logger.info(
        f"[INVALIDATE_SUBSCRIPTION_BATCHES] wallet_id={wallet_id}, "
        f"subscription_id={subscription_id}"
    )

    try:
        now = datetime.now(UTC)
        source_duration = (source_plan_duration or "monthly").lower()

        # Find ALL batches from the old subscription regardless of plan duration
        # This includes:
        # - For monthly: current batch + any old expired batches (clean up)
        # - For yearly: all 12 batches (past + current + future)
        #
        # We invalidate everything from the old subscription and carry forward
        # all remaining tokens to the new subscription plan
        batch_filters = [
            TokenBatch.wallet_id == wallet_id,
            TokenBatch.source_type == TransactionSource.SUBSCRIPTION,
            TokenBatch.status == TransactionStatus.COMPLETED,
            TokenBatch.meta_data["subscription_id"].astext == subscription_id,
        ]

        logger.info(
            f"[INVALIDATE_BATCHES] Finding all batches from old subscription, "
            f"wallet_id={wallet_id}, subscription_id={subscription_id}, source_duration={source_duration}"
        )

        result = await session.execute(select(TokenBatch).where(*batch_filters).with_for_update())
        batches = result.scalars().all()

        if not batches:
            logger.info(
                f"[INVALIDATE_SUBSCRIPTION_BATCHES_NO_BATCHES] "
                f"wallet_id={wallet_id}, subscription_id={subscription_id}, "
                f"source_duration={source_duration}, no batches found to invalidate"
            )
            return {
                "success": True,
                "invalidated_count": 0,
                "total_remaining_tokens": 0,
                "total_reserved_tokens": 0,
                "total_available_tokens": 0,
                "invalidated_batches": [],
            }

        invalidated_count = 0
        total_remaining_tokens = 0  # Sum of remaining_tokens from UNLOCKED batches
        total_reserved_tokens = 0  # Sum of reserved_tokens from UNLOCKED batches
        invalidated_batches = []

        for batch in batches:
            # For YEARLY plans: Only count tokens from UNLOCKED batches (start_at <= now)
            # For MONTHLY plans: Count all tokens (monthly only has 1 batch anyway)
            #
            # This ensures we only carry forward tokens the user had ACCESS to,
            # not future locked tokens they couldn't use yet
            is_unlocked = batch.start_at <= now

            if source_duration == "yearly":
                # For yearly: Only accumulate from unlocked batches
                if is_unlocked:
                    total_remaining_tokens += batch.remaining_tokens
                    total_reserved_tokens += batch.reserved_tokens
            else:
                # For monthly: Accumulate from all batches (there's typically only 1)
                total_remaining_tokens += batch.remaining_tokens
                total_reserved_tokens += batch.reserved_tokens

            # Mark batch as invalidated (due to plan change)
            # INVALIDATED status indicates this batch is no longer usable
            # but tokens are carried forward to the new plan
            batch.status = TransactionStatus.INVALIDATED

            # Update metadata to track when and why it was invalidated
            if not batch.meta_data:
                batch.meta_data = {}
            batch.meta_data.update(
                {
                    "invalidated_at": now.isoformat(),
                    "invalidation_reason": "subscription_plan_change",
                }
            )

            # Track invalidation details for logging/debugging
            invalidated_count += 1
            invalidated_batches.append(
                {
                    "batch_id": batch.id,
                    "initial_tokens": batch.initial_tokens,
                    "remaining_tokens": batch.remaining_tokens,
                    "reserved_tokens": batch.reserved_tokens,
                    "start_at": batch.start_at.isoformat() if batch.start_at else None,
                    "expires_at": batch.expires_at.isoformat() if batch.expires_at else None,
                }
            )

            logger.info(
                f"[INVALIDATE_BATCH] batch_id={batch.id}, "
                f"remaining={batch.remaining_tokens}, reserved={batch.reserved_tokens}"
            )

        # Calculate available tokens (what user can actually use right now)
        # Formula: available = remaining - reserved
        total_available_tokens = total_remaining_tokens - total_reserved_tokens

        await session.flush()

        # ========================================================================
        # Invalidate future token transactions (yearly plans only)
        # ========================================================================
        # Strategy:
        # - Monthly plans: Do NOT invalidate any transactions (keep transaction history intact)
        # - Yearly plans: Invalidate only FUTURE transactions (preserve past transaction history)
        #
        # This ensures users can see their past token allocations while cleaning up
        # future expectations that will never happen due to the plan change
        if invalidated_count > 0 and source_duration == "yearly":
            from app.models import TokenTransaction

            batch_ids = [batch.id for batch in batches]

            # Find all transactions linked to invalidated batches
            transaction_result = await session.execute(
                select(TokenTransaction)
                .where(
                    TokenTransaction.token_batch_id.in_(batch_ids),
                    TokenTransaction.status == TransactionStatus.COMPLETED,
                )
                .with_for_update()
            )
            transactions = transaction_result.scalars().all()

            invalidated_transaction_count = 0
            for txn in transactions:
                # Only invalidate transactions for future batches (start_at > now)
                batch = next((b for b in batches if b.id == txn.token_batch_id), None)
                if batch and batch.start_at > now:
                    # Mark transaction as INVALIDATED (future transaction that never unlocked)
                    txn.status = TransactionStatus.INVALIDATED

                    # Add metadata to track why it was invalidated
                    if not txn.meta_data:
                        txn.meta_data = {}
                    txn.meta_data.update(
                        {
                            "invalidated_at": now.isoformat(),
                            "invalidation_reason": "subscription_plan_change",
                            "original_batch_id": txn.token_batch_id,
                        }
                    )
                    invalidated_transaction_count += 1

            if invalidated_transaction_count > 0:
                await session.flush()
                logger.info(
                    f"[INVALIDATE_FUTURE_TRANSACTIONS] wallet_id={wallet_id}, "
                    f"invalidated {invalidated_transaction_count} future transactions for yearly plan change"
                )

        # Sync wallet balances to reflect invalidated batches
        from app.wallet.repository import _sync_wallet_balances

        await _sync_wallet_balances(wallet_id, session)

        logger.info(
            f"[INVALIDATE_SUBSCRIPTION_BATCHES_SUCCESS] wallet_id={wallet_id}, "
            f"invalidated_count={invalidated_count}, "
            f"total_remaining={total_remaining_tokens}, "
            f"total_reserved={total_reserved_tokens}, "
            f"total_available={total_available_tokens}"
        )

        return {
            "success": True,
            "invalidated_count": invalidated_count,
            "total_remaining_tokens": total_remaining_tokens,  # Sum of remaining
            "total_reserved_tokens": total_reserved_tokens,  # Sum of reserved
            "total_available_tokens": total_available_tokens,  # remaining - reserved
            "invalidated_batches": invalidated_batches,
        }

    except SQLAlchemyError as e:
        logger.error(
            f"[INVALIDATE_SUBSCRIPTION_BATCHES_ERROR] wallet_id={wallet_id}, "
            f"subscription_id={subscription_id}, error={e!s}",
            exc_info=True,
        )
        return {
            "success": False,
            "error": f"Database error: {e!s}",
            "invalidated_count": 0,
            "total_remaining_tokens": 0,
            "total_reserved_tokens": 0,
            "total_available_tokens": 0,
            "invalidated_batches": [],
        }
    except Exception as e:
        logger.error(
            f"[INVALIDATE_SUBSCRIPTION_BATCHES_EXCEPTION] wallet_id={wallet_id}, "
            f"subscription_id={subscription_id}, error={e!s}",
            exc_info=True,
        )
        return {
            "success": False,
            "error": str(e),
            "invalidated_count": 0,
            "total_remaining_tokens": 0,
            "total_reserved_tokens": 0,
            "total_available_tokens": 0,
            "invalidated_batches": [],
        }


async def create_plan_change_token_batches(
    wallet_id: str,
    subscription_id: str,
    plan_tier: str,
    plan_duration: str,
    tokens_per_month: int,
    available_tokens: int,
    reserved_tokens: int,
    over_under_consumption: int,
    days_elapsed: int,
    actual_consumed_tokens: int,
    payment_id: str,
    session: AsyncSession,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Create token batches for a plan change with token adjustments.

    This function creates the appropriate number of token batches for the new plan
    and adjusts the first batch to account for:
    1. Reserved tokens from the old plan (unused tokens)
    2. Over/under consumption adjustment (if applicable)
    3. Same-day upgrade special handling (Razorpay full refund case)

    Batch Structure:
    - Yearly plans: Creates 12 monthly batches
    - Monthly plans: Creates 1 batch

    First Batch Calculation:
    - SAME-DAY UPGRADES (days_elapsed = 0):
      * Razorpay provides full refund of old plan
      * User consumed tokens for free (due to refund)
      * Formula: initial = tokens_per_month - actual_consumed_tokens
    - MULTI-DAY UPGRADES (days_elapsed > 0):
      * Normal fair usage applies
      * Formula: initial = tokens_per_month - over_under_consumption + available_tokens

    Reserved tokens: preserved separately in reserved_tokens field

    IMPORTANT: available_tokens vs reserved_tokens
    - available_tokens: What user can use immediately (added to initial_tokens)
    - reserved_tokens: For ongoing reports (set in reserved_tokens field, NOT in initial)
    - Formula: available = total_remaining - total_reserved

    Example (Plus Monthly → Plus Yearly):
    - Jan 15: User subscribes to Plus Monthly (25,000 tokens/month)
    - Mar 25: User switches to Plus Yearly
    - Old batches: 30,000 remaining, 10,000 reserved (for reports in progress)
    - Available: 30,000 - 10,000 = 20,000 tokens
    - Result:
      * Batch 1 (Mar 25):
        - initial_tokens: 25,000 + 20,000 = 45,000 (base + available)
        - reserved_tokens: 10,000 (preserved for reports)
        - User can use: 45,000 - 10,000 = 35,000 tokens
      * Batches 2-12 (monthly): 25,000 tokens each (unlock monthly)
      * Total: 45,000 + (11 × 25,000) = 320,000 tokens for the year

    Args:
        wallet_id: The wallet ID
        subscription_id: The internal subscription ID (not external/gateway ID)
        plan_tier: New plan tier (e.g., "plus", "pro")
        plan_duration: New plan duration ("monthly" or "yearly")
        tokens_per_month: Standard tokens per month for the new plan
        available_tokens: Available tokens from old plan (remaining - reserved)
        reserved_tokens: Reserved tokens from old plan (for ongoing reports)
        over_under_consumption: Token adjustment (positive = over-consumed, negative = under-consumed)
        days_elapsed: Days elapsed in old subscription cycle
        actual_consumed_tokens: Actual tokens consumed from old subscription
        payment_id: Payment ID from webhook for tracking
        session: Database session
        metadata: Additional metadata for batches (gateway info, payment method, etc.)

    Returns:
        Dict with:
            - success: bool - Whether batch creation succeeded
            - batches_created: int - Number of batches created (12 for yearly, 1 for monthly)
            - first_batch_id: str - ID of the first batch
            - total_tokens_credited: int - Total tokens across all batches
            - batch_ids: list - List of all batch IDs created
            - first_batch_tokens: int - Tokens in the first batch
    """
    logger.info(
        f"[CREATE_PLAN_CHANGE_BATCHES] wallet_id={wallet_id}, "
        f"plan={plan_tier}_{plan_duration}, tokens_per_month={tokens_per_month}, "
        f"available_tokens={available_tokens}, reserved_tokens={reserved_tokens}, "
        f"over_under_consumption={over_under_consumption}, "
        f"days_elapsed={days_elapsed}, actual_consumed={actual_consumed_tokens}"
    )

    try:
        now = datetime.now(UTC)

        # ========================================================================
        # STEP 1: Determine number of batches based on plan duration
        # ========================================================================
        # Yearly: 12 monthly batches (tokens unlock monthly)
        # Monthly: 1 batch (all tokens available immediately)
        num_batches = 12 if plan_duration == "yearly" else 1

        # ========================================================================
        # STEP 2: Calculate first batch initial tokens with adjustments
        # ========================================================================
        # CRITICAL: Same-day upgrades vs Multi-day upgrades have different logic!
        #
        # SAME-DAY UPGRADES (days_elapsed = 0):
        # - Razorpay provides FULL REFUND of old plan
        # - User consumed tokens for free (due to refund)
        # - We must deduct consumed tokens to align token value with amount paid
        # - Formula: initial = tokens_per_month - actual_consumed_tokens
        # - Example: Plus Monthly → Pro Monthly on Jan 1
        #   * Used 10k tokens from Plus → get refunded for Plus
        #   * New plan: 150k - 10k = 140k tokens (correct)
        #
        # MULTI-DAY UPGRADES (days_elapsed > 0):
        # - Normal fair usage applies (prorated refund, not full refund)
        # - Formula: initial = tokens_per_month - over_under_consumption + available_tokens
        # - Example: Plus Monthly → Plus Yearly on Mar 25
        #   * Old batches: 30k remaining, 10k reserved
        #   * Available: 20k
        #   * first_batch_initial = 25k - 0 + 20k = 45k ✅

        if days_elapsed == 0:
            # Same-day upgrade: deduct consumed tokens (full refund case)
            first_batch_initial_tokens = tokens_per_month - actual_consumed_tokens
            logger.info(
                f"[SAME_DAY_UPGRADE_LOGIC] wallet_id={wallet_id}, "
                f"new_plan_tokens={tokens_per_month}, consumed={actual_consumed_tokens}, "
                f"first_batch={first_batch_initial_tokens}"
            )
        else:
            # Multi-day upgrade: normal fair usage
            first_batch_initial_tokens = (
                tokens_per_month - over_under_consumption + available_tokens
            )
            logger.info(
                f"[MULTI_DAY_UPGRADE_LOGIC] wallet_id={wallet_id}, "
                f"base={tokens_per_month}, over_under={over_under_consumption}, "
                f"available={available_tokens}, first_batch={first_batch_initial_tokens}"
            )

        first_batch_initial_tokens = max(
            0, first_batch_initial_tokens
        )  # Ensure non-negative (safety check)

        # Reserved tokens for first batch (from old plan's ongoing reports)
        first_batch_reserved_tokens = reserved_tokens

        batches_created = 0
        first_batch_id = None
        total_tokens_credited = 0
        batch_ids = []

        # ========================================================================
        # STEP 3: Create token batches with monthly intervals
        # ========================================================================
        for i in range(num_batches):
            batch_id = str(uuid7())

            # Calculate batch start date using relativedelta for accurate monthly calculations
            # This handles varying month lengths (28, 29, 30, 31 days)
            from dateutil.relativedelta import relativedelta

            batch_start_at = now + relativedelta(months=i)
            batch_expires_at = batch_start_at + timedelta(days=TOKEN_EXPIRY_DAYS)

            # Determine tokens for this batch
            if i == 0:
                # ============================================================
                # First batch: includes adjustments and PRESERVED reserved tokens
                # ============================================================
                batch_initial_tokens = first_batch_initial_tokens
                batch_reserved_tokens = first_batch_reserved_tokens

                batch_metadata = {
                    **(metadata or {}),
                    "subscription_id": subscription_id,
                    "plan_tier": plan_tier,
                    "plan_duration": plan_duration,
                    "is_plan_change_adjustment": True,  # Flag for tracking
                    "is_first_batch": True,
                    "over_under_consumption": over_under_consumption,
                    "available_tokens_from_old_plan": available_tokens,
                    "reserved_tokens_from_old_plan": reserved_tokens,
                    "adjusted_tokens": tokens_per_month - over_under_consumption,
                    "is_same_day_upgrade": days_elapsed == 0,  # NEW: Flag for same-day upgrades
                    "actual_consumed_tokens": actual_consumed_tokens,  # NEW: For transparency
                    "batch_number": 1,
                    "total_batches": num_batches,
                }
            else:
                # ============================================================
                # Remaining batches: standard tokens per month, no reserved
                # ============================================================
                batch_initial_tokens = tokens_per_month
                batch_reserved_tokens = 0  # Only first batch has reserved tokens

                batch_metadata = {
                    **(metadata or {}),
                    "subscription_id": subscription_id,
                    "plan_tier": plan_tier,
                    "plan_duration": (plan_duration or "monthly").lower(),
                    "is_plan_change_adjustment": False,
                    "is_first_batch": False,
                    "batch_number": i + 1,
                    "total_batches": num_batches,
                }

            # Create token batch record
            # - initial_tokens: total tokens in this batch (available for use)
            # - remaining_tokens: starts same as initial (decremented as consumed)
            # - reserved_tokens: tokens reserved for ongoing reports (only in first batch)
            # - start_at: when this batch becomes available
            # - status: COMPLETED (already paid for via subscription)
            #
            # CRITICAL: For first batch, reserved_tokens preserves allocations from old plan
            # User's effective balance = remaining_tokens - reserved_tokens
            token_batch = TokenBatch(
                id=batch_id,
                wallet_id=wallet_id,
                initial_tokens=batch_initial_tokens,
                remaining_tokens=batch_initial_tokens,
                reserved_tokens=batch_reserved_tokens,  # Preserved from old plan (first batch only)
                amount=metadata.get("amount")
                if metadata
                else None,  # Amount difference for plan change
                currency=metadata.get("currency")
                if metadata
                else None,  # Currency from subscription
                source_type=TransactionSource.SUBSCRIPTION,
                payment_id=payment_id,
                start_at=batch_start_at,
                expires_at=batch_expires_at,
                meta_data=batch_metadata,
                status=TransactionStatus.COMPLETED,  # Subscription batches are pre-paid
            )
            session.add(token_batch)

            batches_created += 1
            total_tokens_credited += batch_initial_tokens
            batch_ids.append(batch_id)

            if i == 0:
                first_batch_id = batch_id

            logger.info(
                f"[CREATE_BATCH] batch_id={batch_id}, batch_number={i + 1}/{num_batches}, "
                f"initial_tokens={batch_initial_tokens}, reserved_tokens={batch_reserved_tokens}, "
                f"available={batch_initial_tokens - batch_reserved_tokens}, start_at={batch_start_at.isoformat()}"
            )

        await session.flush()

        # ========================================================================
        # STEP 4: Sync wallet balance to reflect new batches
        # ========================================================================
        # This recalculates available_balance based on all active batches
        await _sync_wallet_balances(wallet_id, session)

        logger.info(
            f"[CREATE_PLAN_CHANGE_BATCHES_SUCCESS] wallet_id={wallet_id}, "
            f"batches_created={batches_created}, total_tokens={total_tokens_credited}, "
            f"first_batch_initial={first_batch_initial_tokens}, first_batch_reserved={first_batch_reserved_tokens}"
        )

        return {
            "success": True,
            "batches_created": batches_created,
            "first_batch_id": first_batch_id,
            "total_tokens_credited": total_tokens_credited,
            "batch_ids": batch_ids,
            "first_batch_initial_tokens": first_batch_initial_tokens,
            "first_batch_reserved_tokens": first_batch_reserved_tokens,
        }

    except SQLAlchemyError as e:
        logger.error(
            f"[CREATE_PLAN_CHANGE_BATCHES_ERROR] wallet_id={wallet_id}, error={e!s}",
            exc_info=True,
        )
        return {
            "success": False,
            "error": f"Database error: {e!s}",
            "batches_created": 0,
            "total_tokens_credited": 0,
        }
    except Exception as e:
        logger.error(
            f"[CREATE_PLAN_CHANGE_BATCHES_EXCEPTION] wallet_id={wallet_id}, error={e!s}",
            exc_info=True,
        )
        return {"success": False, "error": str(e), "batches_created": 0, "total_tokens_credited": 0}


async def expire_active_subscription_tokens(
    wallet_id: str,
    subscription_id: str,
    session: AsyncSession,
    reason: str = "subscription_upgrade",
) -> dict[str, Any]:
    """
    Expire all currently active tokens from a subscription when upgrading.
    This removes remaining tokens from the old subscription tier.

    Args:
        wallet_id: The wallet ID
        subscription_id: The internal subscription ID (not external)
        session: Database session
        reason: Reason for expiration (e.g., "subscription_upgrade")

    Returns:
        Dict with success status, expired_tokens, and new_balance
    """
    logger.info(
        f"[EXPIRE_ACTIVE_TOKENS_START] wallet_id={wallet_id}, "
        f"subscription_id={subscription_id}, reason={reason}"
    )

    try:
        now = datetime.now(UTC)

        # Find all active/available token batches from this subscription
        # These are batches that:
        # 1. Have start_at <= now (already started or current)
        # 2. Have expires_at > now (not expired)
        # 3. Have available_tokens > 0 (still have tokens)
        # 4. Are from this subscription
        result = await session.execute(
            select(TokenBatch)
            .where(
                TokenBatch.wallet_id == wallet_id,
                TokenBatch.source_type == TransactionSource.SUBSCRIPTION,
                TokenBatch.start_at <= now,  # Already started
                TokenBatch.expires_at > now,  # Not expired yet
                TokenBatch.remaining_tokens > 0,  # Has tokens remaining
                TokenBatch.status == TransactionStatus.COMPLETED,  # Only active batches
                TokenBatch.meta_data["subscription_id"].astext
                == subscription_id,  # Match subscription
            )
            .with_for_update()
        )
        active_batches = result.scalars().all()

        total_expired_tokens = 0
        expired_batch_count = 0

        # Calculate total tokens to expire
        for batch in active_batches:
            total_expired_tokens += batch.remaining_tokens
            expired_batch_count += 1

            logger.info(
                f"[EXPIRE_BATCH] batch_id={batch.id}, "
                f"available_tokens={batch.remaining_tokens}, "
                f"start_at={batch.start_at.isoformat()}"
            )

        if total_expired_tokens == 0:
            logger.info(
                f"[EXPIRE_ACTIVE_TOKENS_NONE] wallet_id={wallet_id}, "
                f"subscription_id={subscription_id}"
            )
            return {
                "success": True,
                "expired_tokens": 0,
                "expired_batch_count": 0,
                "new_balance": None,
            }

        # Get current wallet
        wallet_result = await session.execute(
            select(Wallet).where(Wallet.id == wallet_id).with_for_update()
        )
        wallet = wallet_result.scalar_one_or_none()

        if not wallet:
            logger.error(f"[EXPIRE_TOKENS_WALLET_NOT_FOUND] wallet_id={wallet_id}")
            return {"success": False, "error": "Wallet not found"}

        # Expire all the batches and update wallet balance
        for batch in active_batches:
            tokens_to_remove = batch.remaining_tokens

            # Update batch
            batch.available_tokens = 0
            batch.expires_at = now  # Mark as expired immediately
            batch.status = TransactionStatus.REVERSED

            # Update wallet balance
            wallet.balance -= tokens_to_remove

        # Record a debit transaction for audit trail
        transaction_id = str(uuid7())
        transaction = TokenTransaction(
            id=transaction_id,
            wallet_id=wallet_id,
            transaction_type=TransactionType.DEBIT,
            source_type=TransactionSource.SUBSCRIPTION,
            tokens=total_expired_tokens,
            balance_after=wallet.balance,
            reserved_after=wallet.reserved,
            status=TransactionStatus.COMPLETED,
            description=f"Tokens expired due to {reason}",
            meta_data={
                "reason": reason,
                "subscription_id": subscription_id,
                "expired_batch_count": expired_batch_count,
            },
        )
        session.add(transaction)

        await session.flush()

        logger.info(
            f"[EXPIRE_ACTIVE_TOKENS_SUCCESS] wallet_id={wallet_id}, "
            f"expired_tokens={total_expired_tokens}, "
            f"expired_batch_count={expired_batch_count}, "
            f"new_balance={wallet.balance}"
        )

        return {
            "success": True,
            "expired_tokens": total_expired_tokens,
            "expired_batch_count": expired_batch_count,
            "new_balance": wallet.balance,
            "transaction_id": transaction_id,
        }

    except SQLAlchemyError as e:
        logger.error(
            f"[EXPIRE_ACTIVE_TOKENS_DB_ERROR] wallet_id={wallet_id}, "
            f"subscription_id={subscription_id}, error={e!s}",
            exc_info=True,
        )
        return {"success": False, "error": f"Database error: {e!s}"}
    except Exception as e:
        logger.error(
            f"[EXPIRE_ACTIVE_TOKENS_ERROR] wallet_id={wallet_id}, "
            f"subscription_id={subscription_id}, error={e!s}",
            exc_info=True,
        )
        return {"success": False, "error": str(e)}


# ============================================================================
# Token Batch Operations
# ============================================================================


async def create_token_batch(
    wallet_id: str,
    tokens: int,
    source_type,  # Can be string or TransactionSource enum
    amount: float,
    currency: str,
    idempotency_key: str,
    session: AsyncSession,
    status: TransactionStatus,
    payment_id: str | None = None,
    expires_in_days: int = TOKEN_EXPIRY_DAYS,
    start_at: datetime | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create a new token batch and sync wallet balances."""
    try:
        now = datetime.now(UTC)
        batch_start_at = start_at if start_at else now
        batch_id = str(uuid7())
        # Expiry is relative to start_at
        expires_at = batch_start_at + timedelta(days=expires_in_days)

        # Convert source_type to enum if string
        source_type_enum = (
            TransactionSource(source_type) if isinstance(source_type, str) else source_type
        )

        token_batch = TokenBatch(
            id=batch_id,
            wallet_id=wallet_id,
            idempotency_key=idempotency_key,
            initial_tokens=tokens,
            remaining_tokens=tokens,
            amount=amount,
            currency=currency,
            reserved_tokens=0,
            source_type=source_type_enum,
            payment_id=payment_id,
            start_at=batch_start_at,
            expires_at=expires_at,
            status=status,
            meta_data=metadata,
        )
        session.add(token_batch)
        await session.flush()

        # Sync wallet balances (only if completed)
        balance_av = 0
        reserved_av = 0
        if status == TransactionStatus.COMPLETED:
            balances = await _sync_wallet_balances(wallet_id, session)
            balance_av = balances["available_balance"]
            reserved_av = balances["reserved_balance"]

        # Create transaction record (even for pending)
        transaction_status = status

        transaction = TokenTransaction(
            id=str(uuid7()),
            wallet_id=wallet_id,
            transaction_type=TransactionType.CREDIT,
            source_type=source_type_enum,
            tokens=tokens,
            balance_after=balance_av,  # 0 if pending, actual if completed
            reserved_after=reserved_av,  # 0 if pending, actual if completed
            report_id=None,
            token_batch_id=batch_id,
            status=transaction_status,
            description=f"Token purchase - {tokens:,} tokens",
            meta_data={"payment_id": payment_id},
        )
        session.add(transaction)
        await session.flush()

        return {
            "success": True,
            "batch_id": batch_id,
            "transaction_id": transaction.id,
            "tokens": tokens,
            "expires_at": expires_at.isoformat(),
            "balance_after": balance_av,
            "status": status.value,
        }
    except SQLAlchemyError as e:
        logger.error(f"Database error creating token batch: {e!s}", exc_info=True)
        return {"success": False, "error": f"Database error: {e!s}"}


async def check_existing_token_batch(idempotency_key: str, session: AsyncSession) -> dict[str, Any]:
    """Check if a token batch with the given idempotency key exists."""
    result = await session.execute(
        select(TokenBatch.payment_id, TokenBatch.status).where(
            TokenBatch.idempotency_key == idempotency_key
        )
    )
    existing_batch = result.fetchone()
    return existing_batch


async def get_token_batch_by_payment_id(
    payment_id: str, session: AsyncSession
) -> TokenBatch | None:
    """
    Get token batch details by payment_id.

    Args:
        payment_id: The payment ID to search for
        session: Database session

    Returns:
        TokenBatch object if found, None otherwise
    """
    try:
        result = await session.execute(
            select(TokenBatch)
            .options(selectinload(TokenBatch.wallet))
            .where(TokenBatch.payment_id == payment_id)
        )
        token_batch = result.scalar_one_or_none()
        return token_batch
    except Exception as e:
        logger.error(f"Error getting token batch by payment_id: {e!s}", exc_info=True)
        return None


async def confirm_token_batch(
    payment_id: str,
    session: AsyncSession,
    amount: float | None = None,
    currency: str | None = None,
    payment_method: str | None = None,
    payment_method_details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Confirm a pending token batch after successful payment.
    Updates batch status to COMPLETED and syncs wallet balance.

    Args:
        payment_id: Payment ID from payment service
        session: Database session
        amount: Payment amount (updates batch if not set)
        currency: Payment currency (updates batch if not set)
        payment_method: Payment method type (stored in metadata)
        payment_method_details: Detailed payment method info (stored in metadata)

    Returns:
        Dict with success status, tokens credited, and balance
    """
    try:
        # Find the pending batch by payment_id
        # Note: payment_id in batch might be "pending_orderId", but Razorpay returns "orderId" or "payId"
        # We need to be careful with matching.
        # If the webhook sends razorpay_order_id, we should match against that if stored, or payment_id if stored.

        # Actually, simpler: query for batch where payment_id LIKE %payment_id OR payment_id = payment_id

        # Better approach: The webhook logic (service layer) should handle ID resolution.
        # This DB function should expect the exact payment_id stored in the DB, or we search by idempotency if available?
        # Let's assume the service layer passes the CORRECT ID that matches what's in the DB.
        # But wait, we stored `pending_order_...`.
        # Let's make this function search flexibly or expect the service to pass the right thing.
        # Service layer knows the logic. Let's implementing strict matching here for safety.

        result = await session.execute(
            select(TokenBatch)
            .where(
                TokenBatch.payment_id == payment_id,
                # TokenBatch.status == TransactionStatus.PENDING
            )
            .with_for_update()
        )
        batch = result.scalar_one_or_none()

        if not batch:
            logger.error(f"[CONFIRM_BATCH_NOT_FOUND] payment_id={payment_id}")
            return {"success": False, "error": "Pending batch not found"}

        logger.info(
            f"[CONFIRM_BATCH_FOUND] batch_id={batch.id}, payment_id={payment_id}, "
            f"tokens={batch.initial_tokens}"
        )

        batch.status = TransactionStatus.COMPLETED
        # Find and update associated transaction
        trans_result = await session.execute(
            select(TokenTransaction)
            .where(
                TokenTransaction.token_batch_id == batch.id,
                # TokenTransaction.status == TransactionStatus.PENDING
            )
            .with_for_update()
        )

        transaction = trans_result.scalar_one_or_none()

        if transaction:
            transaction.status = TransactionStatus.COMPLETED
        else:
            logger.info(f"[CONFIRM_BATCH_TRANSACTION_NOT_FOUND] batch_id={batch.id}")
            transaction = TokenTransaction(
                wallet_id=batch.wallet_id,
                transaction_type=TransactionType.CREDIT,
                balance_after=batch.wallet.balance + batch.initial_tokens,
                reserved_after=batch.wallet.reserved_tokens,
                report_id=None,
                token_batch_id=batch.id,
                status=TransactionStatus.COMPLETED,
                description=f"Token top-up via {payment_method}",
                created_at=datetime.now(UTC),
            )
            session.add(transaction)
        # Update batch status

        # Update amount/currency if provided and not already set
        if amount is not None and batch.amount is None:
            logger.info(f"[CONFIRM_BATCH_UPDATE_AMOUNT] batch_id={batch.id}, amount={amount}")
            batch.amount = amount
        if currency is not None and batch.currency is None:
            logger.info(f"[CONFIRM_BATCH_UPDATE_CURRENCY] batch_id={batch.id}, currency={currency}")
            batch.currency = currency

        # Store payment method details in metadata
        if payment_method or payment_method_details:
            # Create copies of metadata dicts to ensure SQLAlchemy detects the change
            # (SQLAlchemy doesn't detect in-place mutations to JSONB columns)
            batch_meta_data = dict(batch.meta_data) if batch.meta_data else {}
            transaction_meta_data = dict(transaction.meta_data) if transaction.meta_data else {}
            if payment_method:
                batch_meta_data["payment_method"] = payment_method
                transaction_meta_data["payment_method"] = payment_method
                logger.info(
                    f"[CONFIRM_BATCH_PAYMENT_METHOD] batch_id={batch.id}, "
                    f"payment_method={payment_method}"
                )

            if payment_method_details:
                batch_meta_data["payment_method_details"] = payment_method_details
                transaction_meta_data["payment_method_details"] = payment_method_details
                # Log only non-sensitive details
                method_type = payment_method_details.get("type")
                logger.info(
                    f"[CONFIRM_BATCH_PAYMENT_DETAILS] batch_id={batch.id}, "
                    f"method_type={method_type}"
                )
            batch.meta_data = batch_meta_data
            transaction.meta_data = transaction_meta_data

        await session.flush()

        # Sync balances
        balances = await _sync_wallet_balances(batch.wallet_id, session)

        # Update transaction snapshots
        if transaction:
            transaction.balance_after = balances["available_balance"]
            transaction.reserved_after = balances["reserved_balance"]
            logger.info(
                f"[CONFIRM_BATCH_TRANSACTION_UPDATED] transaction_id={transaction.id}, "
                f"balance_after={balances['available_balance']}"
            )
        await session.flush()
        logger.info(
            f"[CONFIRM_BATCH_SUCCESS] batch_id={batch.id}, payment_id={payment_id}, "
            f"tokens={batch.initial_tokens}, new_balance={balances['available_balance']}"
        )

        return {
            "success": True,
            "batch_id": batch.id,
            "tokens": batch.initial_tokens,
            "balance_after": balances["available_balance"],
        }

    except SQLAlchemyError as e:
        logger.error(
            f"[CONFIRM_BATCH_DB_ERROR] payment_id={payment_id}, error={e!s}", exc_info=True
        )
        return {"success": False, "error": f"Database error: {e!s}"}
    except Exception as e:
        logger.error(
            f"[CONFIRM_BATCH_UNEXPECTED_ERROR] payment_id={payment_id}, error={e!s}",
            exc_info=True,
        )
        return {"success": False, "error": f"Unexpected error: {e!s}"}


async def fail_token_batch(
    payment_id: str,
    session: AsyncSession,
    amount: float | None = None,
    currency: str | None = None,
    payment_method: str | None = None,
    payment_method_details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Mark a pending token batch as FAILED after failed payment.
    Updates batch and transaction status, and stores payment details in metadata.
    Structure is consistent with confirm_token_batch for uniformity.

    Args:
        payment_id: Payment ID from payment service
        session: Database session
        amount: Payment amount (updates batch if not set)
        currency: Payment currency (updates batch if not set)
        payment_method: Payment method type (stored in metadata)
        payment_method_details: Detailed payment method info (stored in metadata)

    Returns:
        Dict with success status, batch_id, tokens, and status
    """
    try:
        result = await session.execute(
            select(TokenBatch)
            .where(
                TokenBatch.payment_id == payment_id, TokenBatch.status == TransactionStatus.PENDING
            )
            .with_for_update()
        )
        batch = result.scalar_one_or_none()

        if not batch:
            logger.error(f"[FAIL_BATCH_NOT_FOUND] payment_id={payment_id}")
            return {"success": False, "error": "Pending batch not found"}

        logger.info(
            f"[FAIL_BATCH_FOUND] batch_id={batch.id}, payment_id={payment_id}, "
            f"tokens={batch.initial_tokens}"
        )

        # Update batch status
        batch.status = TransactionStatus.FAILED

        # Find and update associated transaction
        trans_result = await session.execute(
            select(TokenTransaction)
            .where(
                TokenTransaction.token_batch_id == batch.id,
                TokenTransaction.status == TransactionStatus.PENDING,
            )
            .with_for_update()
        )

        transaction = trans_result.scalar_one_or_none()

        if transaction:
            transaction.status = TransactionStatus.FAILED
        else:
            logger.info(f"[FAIL_BATCH_TRANSACTION_NOT_FOUND] batch_id={batch.id}")

        # Update amount/currency if provided and not already set
        if amount is not None and batch.amount is None:
            logger.info(f"[FAIL_BATCH_UPDATE_AMOUNT] batch_id={batch.id}, amount={amount}")
            batch.amount = amount
        if currency is not None and batch.currency is None:
            logger.info(f"[FAIL_BATCH_UPDATE_CURRENCY] batch_id={batch.id}, currency={currency}")
            batch.currency = currency

        # Store payment method details in metadata (consistent with confirm_token_batch)
        # This keeps failed and completed payments in the same structure
        if payment_method or payment_method_details:
            # Create copies of metadata dicts to ensure SQLAlchemy detects the change
            # (SQLAlchemy doesn't detect in-place mutations to JSONB columns)
            batch_meta_data = dict(batch.meta_data) if batch.meta_data else {}
            transaction_meta_data = (
                dict(transaction.meta_data) if transaction and transaction.meta_data else {}
            )

            # Add failed timestamp to distinguish from completed payments
            batch_meta_data["failed_at"] = datetime.now(UTC).isoformat()
            if transaction:
                transaction_meta_data["failed_at"] = datetime.now(UTC).isoformat()

            if payment_method:
                batch_meta_data["payment_method"] = payment_method
                if transaction:
                    transaction_meta_data["payment_method"] = payment_method
                logger.info(
                    f"[FAIL_BATCH_PAYMENT_METHOD] batch_id={batch.id}, "
                    f"payment_method={payment_method}"
                )

            if payment_method_details:
                batch_meta_data["payment_method_details"] = payment_method_details
                if transaction:
                    transaction_meta_data["payment_method_details"] = payment_method_details
                # Log only non-sensitive details
                method_type = payment_method_details.get("type")
                logger.info(
                    f"[FAIL_BATCH_PAYMENT_DETAILS] batch_id={batch.id}, method_type={method_type}"
                )

            batch.meta_data = batch_meta_data
            if transaction:
                transaction.meta_data = transaction_meta_data

        await session.flush()

        logger.info(
            f"[FAIL_BATCH_SUCCESS] batch_id={batch.id}, payment_id={payment_id}, "
            f"tokens={batch.initial_tokens}, status=FAILED"
        )

        return {
            "success": True,
            "batch_id": batch.id,
            "tokens": batch.initial_tokens,
            "status": "failed",
        }

    except SQLAlchemyError as e:
        logger.error(f"[FAIL_BATCH_DB_ERROR] payment_id={payment_id}, error={e!s}", exc_info=True)
        return {"success": False, "error": f"Database error: {e!s}"}
    except Exception as e:
        logger.error(
            f"[FAIL_BATCH_UNEXPECTED_ERROR] payment_id={payment_id}, error={e!s}", exc_info=True
        )
        return {"success": False, "error": f"Unexpected error: {e!s}"}


async def get_token_batches_fifo(
    wallet_id: str,
    session: AsyncSession,
    include_expired: bool = True,
    lock_for_update: bool = False,
) -> list[TokenBatch]:
    """
    Get token batches in FIFO order (oldest expiry first).

    Args:
        wallet_id: The wallet ID to fetch batches for
        session: Database session
        include_expired: Whether to include expired batches
        lock_for_update: Whether to lock rows for update (prevents race conditions in reservations)

    Returns:
        List of TokenBatch objects
    """
    now = datetime.now(UTC)
    query = select(TokenBatch).where(
        TokenBatch.wallet_id == wallet_id, TokenBatch.status == TransactionStatus.COMPLETED
    )
    if not include_expired:
        query = query.where(TokenBatch.expires_at > now)

    # Only include batches that have started
    query = query.where(TokenBatch.start_at <= now)

    # Add row-level lock to prevent concurrent reservations
    if lock_for_update:
        query = query.with_for_update()

    query = query.order_by(TokenBatch.expires_at.asc())
    result = await session.execute(query)
    return result.scalars().all()


# ============================================================================
# Token Reservation and Consumption (FIFO)
# ============================================================================


async def reserve_tokens_fifo(
    user_id: str,
    tokens_needed: int,
    session: AsyncSession,
    report_id: str | None = None,
    wallet_id: str | None = None,
) -> dict[str, Any]:
    """Reserve tokens using FIFO consumption (oldest batch first)."""
    logger.info(f"Reserving {tokens_needed} tokens for user_id: {user_id}")

    try:
        if not wallet_id:
            wallet_id = await get_wallet_id_by_user_id(user_id, session)

        if not wallet_id:
            return {"success": False, "error": "Wallet not found"}

        # Check balance from wallet table (fast)
        balance = await get_wallet_balance(user_id, session, wallet_id=wallet_id)
        if not balance.get("success"):
            return balance

        available = balance.get("available_balance", 0)
        if available < tokens_needed:
            return {
                "success": False,
                "error": "Insufficient token balance",
                "error_code": "INSUFFICIENT_BALANCE",
                "available": available,
                "required": tokens_needed,
            }

        # Get batches and reserve using FIFO (with row-level lock to prevent race conditions)
        batches = await get_token_batches_fifo(wallet_id, session, lock_for_update=True)
        token_reserve_remaining = tokens_needed
        affected_batches = []

        for batch in batches:
            if token_reserve_remaining <= 0:
                break
            available_in_batch = batch.remaining_tokens - batch.reserved_tokens
            if available_in_batch <= 0:
                continue
            to_reserve = min(available_in_batch, token_reserve_remaining)
            # batch.remaining_tokens -= to_reserve
            batch.reserved_tokens += to_reserve
            token_reserve_remaining -= to_reserve
            affected_batches.append({"token_batch_id": batch.id, "reserved": to_reserve})

        if token_reserve_remaining > 0:
            await session.rollback()
            return {
                "success": False,
                "error": "Failed to reserve tokens, Insufficient token balance",
            }

        # Sync wallet balances
        await session.flush()
        balances = await _sync_wallet_balances(wallet_id, session)

        # Create transaction record
        transaction_id = str(uuid7())
        transaction = TokenTransaction(
            id=transaction_id,
            wallet_id=wallet_id,
            transaction_type=TransactionType.RESERVE,
            source_type=TransactionSource.REPORT_GENERATION,
            tokens=tokens_needed,
            balance_after=balances["available_balance"],
            reserved_after=balances["reserved_balance"],
            report_id=report_id,
            token_batch_id=None,
            status=TransactionStatus.PENDING,
            description="Token reservation for report generation",
            meta_data={"affected_batches": affected_batches, "report_id": report_id},
        )
        session.add(transaction)
        await session.commit()

        logger.info(f"Reserved {tokens_needed} tokens, transaction_id: {transaction_id}")
        return {
            "success": True,
            "reservation_id": transaction_id,
            "tokens_reserved": tokens_needed,
            "affected_batches": affected_batches,
            "balance_after": balances["available_balance"],
            "reserved_after": balances["reserved_balance"],
        }

    except SQLAlchemyError as e:
        logger.error(f"Database error reserving tokens: {e!s}", exc_info=True)
        await session.rollback()
        return {"success": False, "error": f"Database error: {e!s}"}
    except Exception as e:
        logger.error(f"Unexpected error reserving tokens: {e!s}", exc_info=True)
        await session.rollback()
        return {"success": False, "error": str(e)}


async def confirm_debit(report_id: str, session: AsyncSession) -> dict[str, Any]:
    """Confirm a token reservation and convert it to a debit."""
    logger.info(f"Confirming debit for report_id: {report_id}")

    try:
        result = await session.execute(
            select(TokenTransaction).where(
                TokenTransaction.report_id == report_id,
                TokenTransaction.transaction_type == TransactionType.RESERVE,
                TokenTransaction.status == TransactionStatus.PENDING,
            )
        )
        reservation = result.scalar_one_or_none()

        if not reservation:
            return {"success": False, "error": "Reservation not found or already processed"}

        tokens_to_debit = abs(reservation.tokens)
        meta_data = reservation.meta_data or {}
        affected_batches = meta_data.get("affected_batches", [])

        # Reduce reserved tokens from batches (tokens consumed)
        for batch_info in affected_batches:
            batch_result = await session.execute(
                select(TokenBatch).where(TokenBatch.id == batch_info["token_batch_id"])
            )
            batch = batch_result.scalar_one_or_none()

            if not batch:
                logger.warning(
                    f"Batch {batch_info['token_batch_id']} not found during confirm_debit for report {report_id}"
                )
                continue

            reserved_amount = batch_info["reserved"]

            # Validate batch state to prevent negative balances
            if batch.reserved_tokens < reserved_amount:
                logger.error(
                    f"Invalid batch state: batch {batch.id} has {batch.reserved_tokens} reserved "
                    f"but trying to debit {reserved_amount}. Report: {report_id}"
                )
                await session.rollback()
                return {
                    "success": False,
                    "error": "Invalid batch state: insufficient reserved tokens",
                    "error_code": "BATCH_STATE_ERROR",
                }

            if batch.remaining_tokens < reserved_amount:
                logger.error(
                    f"Invalid batch state: batch {batch.id} has {batch.remaining_tokens} remaining "
                    f"but trying to debit {reserved_amount}. Report: {report_id}"
                )
                await session.rollback()
                return {
                    "success": False,
                    "error": "Invalid batch state: insufficient remaining tokens",
                    "error_code": "BATCH_STATE_ERROR",
                }

            # Safe to reduce tokens
            batch.reserved_tokens -= reserved_amount
            batch.remaining_tokens -= reserved_amount

        reservation.status = TransactionStatus.COMPLETED

        # Sync wallet balances
        await session.flush()
        wallet_id = reservation.wallet_id
        balances = await _sync_wallet_balances(wallet_id, session)

        # Create debit transaction
        transaction = TokenTransaction(
            id=str(uuid7()),
            wallet_id=wallet_id,
            transaction_type=TransactionType.DEBIT,
            source_type=TransactionSource.REPORT_GENERATION,
            tokens=tokens_to_debit,
            balance_after=balances["available_balance"],
            reserved_after=balances["reserved_balance"],
            report_id=reservation.report_id,
            token_batch_id=reservation.token_batch_id,
            status=TransactionStatus.COMPLETED,
            description=f"Report generation - {tokens_to_debit:,} tokens consumed",
            meta_data={"report_id": report_id, "reservation_id": reservation.id},
        )
        session.add(transaction)
        await session.commit()

        return {
            "success": True,
            "tokens_debited": tokens_to_debit,
            "balance_after": balances["available_balance"],
        }

    except SQLAlchemyError as e:
        logger.error(f"Database error confirming debit: {e!s}", exc_info=True)
        await session.rollback()
        return {"success": False, "error": f"Database error: {e!s}"}


async def release_reservation(
    report_id: str, session: AsyncSession, reason: str = "generation_failed"
) -> dict[str, Any]:
    """Release a token reservation (return tokens to available)."""
    logger.info(f"Releasing reservation: {report_id}, reason: {reason}")

    try:
        result = await session.execute(
            select(TokenTransaction).where(
                TokenTransaction.report_id == report_id,
                TokenTransaction.transaction_type == TransactionType.RESERVE,
                TokenTransaction.status == TransactionStatus.PENDING,
            )
        )
        reservation = result.scalar_one_or_none()

        if not reservation:
            return {"success": False, "error": "Reservation not found or already processed"}

        tokens_to_release = abs(reservation.tokens)
        meta_data = reservation.meta_data or {}
        affected_batches = meta_data.get("affected_batches", [])

        # Return tokens to batches
        for batch_info in affected_batches:
            batch_result = await session.execute(
                select(TokenBatch).where(TokenBatch.id == batch_info["token_batch_id"])
            )
            batch = batch_result.scalar_one_or_none()

            if not batch:
                logger.warning(
                    f"Batch {batch_info['token_batch_id']} not found during release for report {report_id}"
                )
                continue

            reserved_amount = batch_info["reserved"]

            # Validate batch state (use best-effort approach for releases)
            if batch.reserved_tokens < reserved_amount:
                logger.warning(
                    f"Batch {batch.id} has {batch.reserved_tokens} reserved "
                    f"but trying to release {reserved_amount}. Releasing available amount. Report: {report_id}"
                )
                # Best effort: release only what's actually reserved
                reserved_amount = max(0, batch.reserved_tokens)

            # Safe to release tokens
            batch.reserved_tokens -= reserved_amount

        reservation.status = TransactionStatus.REVERSED

        # Sync wallet balances
        await session.flush()
        wallet_id = reservation.wallet_id
        balances = await _sync_wallet_balances(wallet_id, session)

        # Create release transaction
        transaction = TokenTransaction(
            id=str(uuid7()),
            wallet_id=wallet_id,
            transaction_type=TransactionType.RELEASE,
            source_type=TransactionSource.REPORT_GENERATION,
            tokens=tokens_to_release,
            balance_after=balances["available_balance"],
            reserved_after=balances["reserved_balance"],
            report_id=reservation.report_id,
            token_batch_id=reservation.token_batch_id,
            status=TransactionStatus.COMPLETED,
            description=f"Token release - {reason}",
            meta_data={"reservation_id": reservation.id, "reason": reason},
        )
        session.add(transaction)
        await session.commit()

        return {
            "success": True,
            "tokens_released": tokens_to_release,
            "balance_after": balances["available_balance"],
        }

    except SQLAlchemyError as e:
        logger.error(f"Database error releasing reservation: {e!s}", exc_info=True)
        await session.rollback()
        return {"success": False, "error": f"Database error: {e!s}"}


# ============================================================================
# Credit Operations (for payments)
# ============================================================================


async def credit_tokens_from_payment(
    user_id: str,
    tokens: int,
    source_type: str,
    start_date: datetime,
    end_date: datetime,
    session: AsyncSession,
    payment_id: str,
    amount: float | None = None,
    currency: str = "USD",
    subscription_tier: str | None = None,
    subscription_id_ext: str | None = None,
    description: str | None = None,
) -> dict[str, Any]:
    """Credit tokens to a user's wallet from a payment."""
    logger.info(f"Crediting {tokens} tokens for user_id: {user_id}, payment_id: {payment_id}")

    try:
        # Idempotency check - use TokenBatch.payment_id
        existing = await session.execute(
            select(TokenBatch).where(TokenBatch.payment_id == payment_id)
        )
        if existing.scalar_one_or_none():
            balance = await get_wallet_balance(user_id, session)
            return {
                "success": True,
                "message": "Payment already processed",
                "idempotent": True,
                "balance": balance.get("available_balance", 0),
            }

        wallet_id = await get_wallet_id_by_user_id(user_id, session)
        if not wallet_id:
            return {"success": False, "error": "Wallet not found"}

        # Convert source_type string to enum
        source_type_enum = (
            TransactionSource(source_type) if isinstance(source_type, str) else source_type
        )

        # Handle subscription upgrade
        if subscription_tier and source_type_enum == TransactionSource.SUBSCRIPTION:
            result = await create_subscription_interval(
                user_id=user_id,
                tier=subscription_tier,
                session=session,
                payment_id=payment_id,
                tokens_to_credit=tokens,
                subscription_id_ext=subscription_id_ext,
                start_date=start_date,
                end_date=end_date,
            )
            return result

        # Regular token credit (topup)
        batch_result = await create_token_batch(
            wallet_id=wallet_id,
            tokens=tokens,
            source_type=source_type_enum,
            session=session,
            payment_id=payment_id,
        )

        if not batch_result.get("success"):
            return batch_result

        # Get updated balance
        balance = await get_wallet_balance(user_id, session)

        # Create transaction record
        transaction = TokenTransaction(
            id=str(uuid7()),
            wallet_id=wallet_id,
            transaction_type=TransactionType.CREDIT,
            source_type=source_type_enum,
            tokens=tokens,
            balance_after=balance.get("available_balance", 0),
            reserved_after=balance.get("reserved_balance", 0),
            report_id=None,
            token_batch_id=batch_result.get("batch_id"),
            status=TransactionStatus.COMPLETED,
            description=description
            or f"{source_type.replace('_', ' ').title()} - {tokens:,} tokens",
            meta_data={
                "subscription_tier": subscription_tier,
                "payment_id": payment_id,
                "amount": amount,
                "currency": currency,
            }
            if subscription_tier
            else {"payment_id": payment_id, "amount": amount, "currency": currency},
        )
        session.add(transaction)
        await session.commit()

        return {
            "success": True,
            "tokens_credited": tokens,
            "batch_id": batch_result.get("batch_id"),
            "balance_after": balance.get("available_balance", 0),
        }

    except SQLAlchemyError as e:
        logger.error(f"Database error crediting tokens: {e!s}", exc_info=True)
        await session.rollback()
        return {"success": False, "error": f"Database error: {e!s}"}


# ============================================================================
# Transaction History
# ============================================================================


async def get_transaction_history(
    user_id: str,
    session: AsyncSession,
    limit: int = 50,
    offset: int = 0,
    transaction_type: str | None = None,
) -> dict[str, Any]:
    """Get transaction history for a user enriched with batch billing info."""
    try:
        logger.info(
            f"[GET_TRANSACTION_HISTORY_START] user_id={user_id}, "
            f"limit={limit}, offset={offset}, transaction_type={transaction_type}"
        )

        wallet_id = await get_wallet_id_by_user_id(user_id, session)
        if not wallet_id:
            logger.warning(f"[GET_TRANSACTION_HISTORY_NO_WALLET] user_id={user_id}")
            return {
                "success": True,
                "transactions": [],
                "total": 0,
                "limit": limit,
                "offset": offset,
            }

        base_filters = [
            TokenTransaction.wallet_id == wallet_id,
            TokenTransaction.status != TransactionStatus.REVERSED,
            TokenTransaction.status
            != TransactionStatus.INVALIDATED,  # Exclude invalidated transactions from plan changes
            # Exclude PENDING credit transactions (initiated but not completed)
            ~and_(
                TokenTransaction.status == TransactionStatus.PENDING,
                TokenTransaction.transaction_type == TransactionType.CREDIT,
            ),
            # Allow transactions without batch OR batch that has started
            or_(
                TokenBatch.id.is_(None),
                TokenBatch.start_at <= datetime.now(UTC),
            ),
        ]

        if transaction_type:
            base_filters.append(TokenTransaction.transaction_type == transaction_type)

        # ---------- Main Query ----------
        # Join with SubscriptionInterval to get full amount for yearly subscriptions
        query = (
            select(TokenTransaction, TokenBatch, SubscriptionInterval)
            .outerjoin(
                TokenBatch,
                TokenTransaction.token_batch_id == TokenBatch.id,
            )
            .outerjoin(
                SubscriptionInterval,
                and_(
                    TokenBatch.payment_id == SubscriptionInterval.payment_id,
                    TokenBatch.source_type == TransactionSource.SUBSCRIPTION,
                ),
            )
            .where(*base_filters)
            .order_by(TokenTransaction.created_at.desc())
            .limit(limit)
            .offset(offset)
        )

        # ---------- Count Query ----------
        count_query = (
            select(func.count(TokenTransaction.id))
            .outerjoin(
                TokenBatch,
                TokenTransaction.token_batch_id == TokenBatch.id,
            )
            .where(*base_filters)
        )

        count_result = await session.execute(count_query)
        total = count_result.scalar() or 0

        result = await session.execute(query)
        rows = result.fetchall()

        transactions_data = []
        for transaction, batch, interval in rows:
            batch_meta = batch.meta_data if batch and batch.meta_data else {}

            # For subscription transactions, prefer SubscriptionInterval amount (full amount)
            # This ensures yearly subscriptions show the full yearly amount, not monthly split
            if interval and batch and batch.source_type == TransactionSource.SUBSCRIPTION:
                # Use the full amount from SubscriptionInterval for subscription transactions
                display_amount = (
                    float(interval.amount)
                    if interval.amount
                    else (float(batch.amount) if batch and batch.amount else None)
                )
                display_currency = (
                    interval.currency if interval.currency else (batch.currency if batch else None)
                )
            else:
                # For non-subscription transactions, use TokenBatch amount
                display_amount = float(batch.amount) if batch and batch.amount else None
                display_currency = batch.currency if batch else None

            transactions_data.append(
                {
                    "id": transaction.id,
                    "transaction_type": transaction.transaction_type.value,
                    "source_type": transaction.source_type.value,
                    "tokens": transaction.tokens,
                    "balance_after": transaction.balance_after,
                    "reserved_after": transaction.reserved_after,
                    "report_id": transaction.report_id,
                    "token_batch_id": transaction.token_batch_id,
                    "status": transaction.status.value,
                    "description": transaction.description,
                    "meta_data": transaction.meta_data,
                    "created_at": transaction.created_at.isoformat(),
                    # ---- Payment / Billing Info (from TokenBatch or SubscriptionInterval) ----
                    "payment": {
                        "payment_id": batch.payment_id if batch else None,
                        "amount": display_amount,
                        "currency": display_currency,
                        "payment_method": batch_meta.get("payment_method"),
                        "payment_method_details": batch_meta.get("payment_method_details"),
                    },
                }
            )

        logger.info(
            f"[GET_TRANSACTION_HISTORY_SUCCESS] user_id={user_id}, "
            f"returned={len(transactions_data)}, total={total}"
        )

        return {
            "success": True,
            "transactions": transactions_data,
            "total": total,
            "limit": limit,
            "offset": offset,
        }

    except SQLAlchemyError as e:
        logger.error(
            f"[GET_TRANSACTION_HISTORY_DB_ERROR] user_id={user_id}, error={e!s}", exc_info=True
        )
        logger.error(f"Database error getting transactions: {e!s}", exc_info=True)
        return {"success": False, "error": f"Database error: {e!s}"}


# ============================================================================
# Webhook Security
# ============================================================================


def verify_webhook_api_key(provided_key: str | None) -> bool:
    """
    Verify the API key for webhook requests.

    Args:
        provided_key: The API key provided in the request header

    Returns:
        True if the key is valid, False otherwise
    """
    if not provided_key:
        logger.warning("Webhook request received without API key")
        return False

    if not X_API_KEY:
        logger.error("X_API_KEY not configured in environment")
        return False

    is_valid = provided_key == X_API_KEY

    if not is_valid:
        logger.warning("Webhook request received with invalid API key")

    return is_valid
