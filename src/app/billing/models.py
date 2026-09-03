"""ORM models for the billing bounded context.

Moved verbatim from ``src/db/database.py`` during the R-STRUCT-1 migration.
Column definitions, comments, and relationships are unchanged; only the
declarative base moved, from the module-local ``declarative_base()`` to the
single shared ``app.core.db.Base`` (spec §3.2).

Relationships that point at another context resolve through the shared
registry, which ``app/models.py`` guarantees is fully populated.
"""

from sqlalchemy import (
    CHAR, Boolean, CheckConstraint, Column, DateTime, Enum, Float, ForeignKey, Index,
    Integer, Numeric, String, Text, UniqueConstraint, func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import relationship
from uuid_utils import uuid7

from app.core.db import Base
from app.core.enums import (  # noqa: F401
    FileType, FileUploadContext, FileUsageType, MessageType, ReportStatus,
    SubscriptionStatus, SubscriptionTier, TransactionSource, TransactionStatus,
    TransactionType, UploadedFileStatus,
)


class Subscription(Base):
    """
    Subscription table (Parent) - manages user subscription status and tier.
    
    This is the parent table in the subscription system, maintaining the current state
    of each user's subscription. Each user has exactly one subscription record that tracks
    their current tier (free, plus, pro) and overall subscription status. The detailed
    billing history and payment intervals are stored in the child SubscriptionInterval table.
    
    Subscription Tiers:
        - FREE: Default tier with limited tokens (50,000 signup bonus)
        - PLUS: Paid tier with 25,000 tokens/month
        - PRO: Premium tier with 150,000 tokens/month
    
    Attributes:
        id (str): Unique identifier for the subscription record (UUID, primary key).
        user_id (str): Foreign key reference to users.id. Links this subscription to a user.
            Note: Not unique to support subscription history (user can have multiple subscriptions
            over time, though typically one active subscription).
        subscription_id (str): External subscription identifier from payment gateway
            (e.g., Razorpay subscription ID). Used for webhook processing and payment gateway
            integration. Nullable for free tier users. Indexed for quick payment gateway lookups.
        current_tier (str): Current active subscription tier. Uses SubscriptionTier enum:
            - FREE: No payment required, limited features
            - PLUS: Monthly subscription, moderate token allocation
            - PRO: Monthly subscription, high token allocation
            Defaults to FREE for new users.
        status (str): Overall subscription status. Uses SubscriptionStatus enum:
            - active: Subscription is currently active and valid
            - expired: Subscription period has ended, no auto-renewal
            - cancelled: User cancelled, may still be active until period ends
            - refunded: Payment was refunded, subscription terminated
            - paused: Temporarily paused (future feature)
            Defaults to ACTIVE.
        meta_data (JSONB): Flexible JSON storage for additional subscription metadata:
            - cancellation_reason: Why user cancelled
            - cancellation_date: When cancellation was requested
            - scheduled_tier_change: Upcoming tier change details
            - promotional_details: Special offers or discounts applied
            - payment_method: Preferred payment method
        created_at (datetime): Timestamp when subscription record was created (UTC, auto-generated).
        updated_at (datetime): Timestamp when subscription was last modified (UTC, auto-updated).
            Updates on tier changes, status changes, or metadata modifications.
    
    Relationships:
        user (User): One-to-one relationship with User table. Each user has one subscription
            record tracking their current subscription state.
        intervals (SubscriptionInterval): One-to-many relationship with SubscriptionInterval table.
            Contains complete billing and payment history, ordered by start_date descending.
            Each interval represents one billing cycle (typically monthly).
    
    Subscription Lifecycle:
        1. User signs up → Subscription created with tier=FREE, status=ACTIVE
        2. User subscribes to PLUS → tier=PLUS, status=ACTIVE, new SubscriptionInterval created
        3. Monthly renewal → New SubscriptionInterval created, status remains ACTIVE
        4. User upgrades to PRO → tier=PRO, new SubscriptionInterval created
        5. User cancels → status=CANCELLED (remains active until period ends)
        6. Period ends without renewal → status=EXPIRED, tier may revert to FREE
    
    Indexes:
        - idx_subscriptions_user_id: Fast lookup of user's subscription
        - idx_subscriptions_current_tier: Filter users by subscription tier
        - idx_subscriptions_status: Find subscriptions by status
        - idx_subscriptions_external_id: Quick lookup by payment gateway subscription ID
    
    Example:
        New user signup:
        - Subscription: user_id=<uuid>, tier=FREE, status=ACTIVE, subscription_id=NULL
        
        User subscribes to Plus:
        - Subscription: tier=PLUS, status=ACTIVE, subscription_id="sub_razorpay_123"
        - SubscriptionInterval: tier=PLUS, start_date=now, end_date=now+30days, status=ACTIVE
        
        User upgrades to Pro mid-cycle:
        - Subscription: tier=PRO, status=ACTIVE
        - Old SubscriptionInterval: status=EXPIRED (prorated)
        - New SubscriptionInterval: tier=PRO, start_date=now, end_date=now+30days
    """
    __tablename__ = 'subscriptions'
    
    id = Column(CHAR(36), primary_key=True, default=lambda: str(uuid7()), unique=True, nullable=False)
    user_id = Column(CHAR(36), ForeignKey('users.id', ondelete='CASCADE'), unique=False, nullable=False, comment="Reference to user (one subscription per user)")
    subscription_id = Column(String(100), nullable=True, comment="Reference to external subscription id (Razorpay subscription ID)")
    
    # Current subscription tier
    current_tier = Column(
        Enum(SubscriptionTier, name="subscription_tier_enum", native_enum=True),
        nullable=False,
        default=SubscriptionTier.FREE,
        comment="Current tier: free, plus, pro"
    )
    
    # Status
    status = Column(
        Enum(SubscriptionStatus, name="subscription_status_enum", native_enum=True),
        nullable=False,
        default=SubscriptionStatus.ACTIVE,
        comment="Status: active, expired, cancelled, refunded, paused"
    )
    meta_data = Column(JSONB, nullable=True, comment="Additional metadata")
    
    # Timestamps
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
    
    # Relationships
    user = relationship("User", back_populates="subscription")
    intervals = relationship("SubscriptionInterval", back_populates="subscription", cascade="all, delete-orphan", order_by="desc(SubscriptionInterval.start_date)")
    
    def __repr__(self):
        return f"<Subscription(id='{self.id}', user_id='{self.user_id}', tier='{self.current_tier}', status='{self.status}')>"
class SubscriptionInterval(Base):
    """
    SubscriptionInterval table (Child) - tracks individual billing cycles and payments.
    
    This is the child table in the subscription system, storing detailed records of each
    billing interval (typically monthly). A new row is created every time a subscription
    payment is processed, providing a complete audit trail of all subscription payments,
    token allocations, and billing periods.
    
    This table enables:
    - Complete payment history and financial reconciliation
    - Token allocation tracking per billing cycle
    - Subscription lifecycle management (renewals, upgrades, cancellations)
    - Prorated billing calculations for mid-cycle changes
    - Revenue analytics and reporting
    
    Attributes:
        id (str): Unique identifier for this billing interval (UUID, primary key).
        subscription_id (str): Foreign key reference to subscriptions.id (parent subscription).
            Links this interval to the user's subscription record. Indexed for fast lookup
            of all intervals for a subscription.
        tier (str): Subscription tier for this specific billing interval. Uses SubscriptionTier enum:
            - PLUS: 25,000 tokens/month tier
            - PRO: 150,000 tokens/month tier
            Note: FREE tier doesn't create intervals (no billing).
            This can differ from parent subscription's current_tier during upgrades/downgrades.
        start_date (datetime): Start timestamp of this billing interval (UTC). Typically the
            payment date or subscription start date. Indexed for time-based queries.
        end_date (datetime): End timestamp of this billing interval (UTC). Typically 30 days
            after start_date for monthly subscriptions. Indexed for finding active intervals
            and expiry checks.
        payment_id (str): External payment service reference ID (e.g., Razorpay payment_id).
            Used for payment reconciliation, refund processing, and webhook handling.
            Indexed for quick payment gateway lookups. Required for all paid intervals.
        amount (decimal): Amount charged for this billing interval in the specified currency.
            Precision: 10 digits total, 2 decimal places (e.g., 999.99).
            Nullable for promotional/free intervals.
        currency (str): ISO 4217 currency code (USD, INR, EUR, etc.). Should match the
            payment gateway currency. Nullable if amount is null.
        tokens_credited (int): Number of tokens allocated to the user for this billing interval.
            - PLUS tier: typically 25,000 tokens
            - PRO tier: typically 150,000 tokens
            These tokens are added to the user's wallet via a TokenBatch entry.
            Defaults to 0 (should be set based on tier).
        token_batch_id (str): Foreign key reference to token_batches.id. Links to the specific
            token batch created for this interval's token allocation. Used to track which tokens
            came from which subscription payment. Can be null if token crediting failed.
        status (str): Current status of this billing interval. Uses SubscriptionStatus enum:
            - active: Current active billing period
            - expired: Billing period has ended (normal completion)
            - cancelled: Subscription cancelled during this period
            - refunded: Payment was refunded, tokens may be revoked
            - paused: Billing paused (future feature)
            Defaults to ACTIVE. Indexed for finding active/expired intervals.
        meta_data (JSONB): Flexible JSON storage for additional billing details:
            - next_billing_date: Scheduled date for next auto-renewal
            - payment_method: Card/UPI/wallet details (masked)
            - payment_gateway_response: Full payment gateway webhook data
            - proration_details: Calculations for mid-cycle upgrades/downgrades
            - discount_applied: Promotional discount information
            - invoice_id: Reference to generated invoice
            - refund_reason: Why payment was refunded
            - autopay_enabled: Whether auto-renewal is enabled
        created_at (datetime): Timestamp when this interval record was created (UTC, auto-generated).
    
    Relationships:
        subscription (Subscription): Many-to-one relationship with Subscription table.
            Multiple intervals belong to one subscription (billing history).
        token_batch (TokenBatch): Many-to-one relationship with TokenBatch table.
            Links to the token batch created for this interval's token allocation.
    
    Billing Lifecycle Examples:
        
        1. Initial subscription to Plus tier:
           - subscription.tier = PLUS, subscription.status = ACTIVE
           - Interval: tier=PLUS, start=2026-01-01, end=2026-02-01, amount=9.99,
                      tokens_credited=25000, status=ACTIVE
        
        2. Auto-renewal after 30 days:
           - Previous interval: status changes from ACTIVE to EXPIRED
           - New interval: tier=PLUS, start=2026-02-01, end=2026-03-01, amount=9.99,
                          tokens_credited=25000, status=ACTIVE
        
        3. Mid-cycle upgrade from Plus to Pro:
           - subscription.tier = PRO
           - Old interval: status=EXPIRED (prorated refund calculated)
           - New interval: tier=PRO, start=2026-02-15, end=2026-03-15, amount=24.99,
                          tokens_credited=150000, status=ACTIVE
           - meta_data includes proration details
        
        4. User cancels subscription:
           - subscription.status = CANCELLED
           - Current interval: status=CANCELLED (remains until end_date)
           - No new interval created at end_date
        
        5. Payment fails during renewal:
           - Previous interval: status=EXPIRED
           - New interval: status=FAILED (created but payment unsuccessful)
           - subscription.status may change to EXPIRED
           - No tokens credited
        
        6. Refund processed:
           - Interval: status=REFUNDED
           - meta_data includes refund_reason and refund_date
           - Associated token_batch may be revoked/expired
    
    Indexes:
        - idx_subscription_intervals_subscription_id: Fast lookup of all intervals for a subscription
        - idx_subscription_intervals_tier: Filter intervals by subscription tier
        - idx_subscription_intervals_start_date: Time-based queries and sorting
        - idx_subscription_intervals_end_date: Find expiring/expired intervals
        - idx_subscription_intervals_payment_id: Quick payment gateway reference lookup
        - idx_subscription_intervals_status: Filter by interval status
        - idx_subscription_intervals_active_lookup: Composite index (subscription_id, status, end_date)
          for efficiently finding the current active interval
    
    Queries:
        Find current active interval:
        SELECT * FROM subscription_intervals 
        WHERE subscription_id = ? AND status = 'active' AND end_date > NOW()
        ORDER BY end_date DESC LIMIT 1;
        
        Calculate total revenue for a user:
        SELECT SUM(amount) FROM subscription_intervals 
        WHERE subscription_id = ? AND status IN ('active', 'expired');
        
        Get billing history:
        SELECT * FROM subscription_intervals 
        WHERE subscription_id = ? 
        ORDER BY start_date DESC;
    """
    __tablename__ = 'subscription_intervals'
    
    id = Column(CHAR(36), primary_key=True, default=lambda: str(uuid7()), unique=True, nullable=False)
    subscription_id = Column(CHAR(36), ForeignKey('subscriptions.id', ondelete='CASCADE'), nullable=False, comment="Reference to parent subscription")
    
    # Billing interval details
    tier = Column(
        Enum(SubscriptionTier, name="subscription_tier_enum", native_enum=True, create_constraint=False),
        nullable=False,
        comment="Subscription tier for this interval: plus, pro"
    )
    start_date = Column(DateTime(timezone=True), nullable=False, comment="Start of billing interval")
    end_date = Column(DateTime(timezone=True), nullable=False, comment="End of billing interval")
    
    # Payment tracking
    payment_id = Column(String(50), nullable=False, index=True, comment="Reference to payment_id in payment service")
    amount = Column(Numeric(10, 2), nullable=True, comment="Amount charged")
    currency = Column(String(10), nullable=True, comment="Currency of payment")
    
    # Token tracking
    tokens_credited = Column(Integer, nullable=False, default=0, comment="Tokens credited for this interval")
    token_batch_id = Column(CHAR(36), ForeignKey('token_batches.id', ondelete='SET NULL'), nullable=True, comment="Reference to token batch created")
    
    # Status tracking
    status = Column(
        Enum(SubscriptionStatus, name="subscription_status_enum", native_enum=True, create_constraint=False),
        nullable=False,
        default=SubscriptionStatus.ACTIVE,
        comment="Status: active, expired, cancelled, refunded"
    )
    meta_data = Column(JSONB, nullable=True, comment="Additional details like next autopay schedule date, payment method and other payment related data")
    
    # Timestamps
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    
    # Relationships
    subscription = relationship("Subscription", back_populates="intervals")
    token_batch = relationship("TokenBatch", foreign_keys=[token_batch_id])
    
    def __repr__(self):
        return f"<SubscriptionInterval(id='{self.id}', tier='{self.tier}', start='{self.start_date}', end='{self.end_date}', status='{self.status}')>"


# Standalone indexes, moved with the models they index.
Index('idx_subscriptions_user_id', Subscription.user_id)
Index('idx_subscriptions_current_tier', Subscription.current_tier)
Index('idx_subscriptions_status', Subscription.status)
Index('idx_subscriptions_external_id', Subscription.subscription_id)
Index('idx_subscription_intervals_subscription_id', SubscriptionInterval.subscription_id)
Index('idx_subscription_intervals_tier', SubscriptionInterval.tier)
Index('idx_subscription_intervals_start_date', SubscriptionInterval.start_date)
Index('idx_subscription_intervals_end_date', SubscriptionInterval.end_date)
Index('idx_subscription_intervals_payment_id', SubscriptionInterval.payment_id)
Index('idx_subscription_intervals_status', SubscriptionInterval.status)
Index('idx_subscription_intervals_active_lookup', SubscriptionInterval.subscription_id, SubscriptionInterval.status, SubscriptionInterval.end_date)
