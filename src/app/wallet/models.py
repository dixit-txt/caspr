"""ORM models for the wallet bounded context.

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


class Wallet(Base):
    """
    Wallet table - stores token balances and transaction history for each user.
    
    Each user has exactly one wallet that tracks their token balance. The wallet maintains
    both available tokens (ready to use) and reserved tokens (locked for pending operations).
    This table uses denormalized balance fields for performance, while TokenBatch remains
    the source of truth for detailed token tracking with expiry management.
    
    Attributes:
        id (str): Unique identifier for the wallet (UUID, primary key).
        user_id (str): Foreign key reference to users.id (unique constraint - one wallet per user).
        available_balance (int): Tokens currently available for use. This is a denormalized field
            updated on each transaction for fast reads. Defaults to 0.
        reserved_balance (int): Tokens temporarily locked for pending operations (e.g., report
            generation in progress). This is a denormalized field. Defaults to 0.
        created_at (datetime): Timestamp when the wallet was created (UTC, auto-generated).
        updated_at (datetime): Timestamp when the wallet was last modified (UTC, auto-updated).
    
    Relationships:
        user (User): One-to-one relationship with User table. Each user has exactly one wallet.
        token_batches (TokenBatch): One-to-many relationship with TokenBatch table. Stores all
            token batches with their expiry dates (source of truth for balance calculation).
        transactions (TokenTransaction): One-to-many relationship with TokenTransaction table.
            Complete audit trail of all token movements, ordered by created_at descending.
    
    Balance Management:
        - available_balance and reserved_balance are denormalized for performance.
        - TokenBatch table is the source of truth for detailed token tracking.
        - All balance updates must be atomic with TokenBatch changes.
        - Use database transactions to ensure consistency between Wallet and TokenBatch.
    
    Example:
        User signs up and receives 50,000 signup bonus tokens:
        - Wallet: available_balance=50000, reserved_balance=0
        - TokenBatch: 1 batch with 50000 tokens, expires in 90 days
        
        User starts generating a report (costs 5000 tokens):
        - Wallet: available_balance=45000, reserved_balance=5000
        - TokenBatch: remaining_tokens=45000, reserved_tokens=5000
        
        Report generation completes:
        - Wallet: available_balance=45000, reserved_balance=0
        - TokenBatch: remaining_tokens=45000, reserved_tokens=0
    """
    __tablename__ = 'wallets'
    
    id = Column(CHAR(36), primary_key=True, default=lambda: str(uuid7()), unique=True, nullable=False)
    user_id = Column(CHAR(36), ForeignKey('users.id', ondelete='CASCADE'), unique=True, nullable=False, comment="Reference to user (one wallet per user)")
    
    # Denormalized balances for fast reads (updated on each transaction)
    available_balance = Column(Integer, nullable=False, default=0, comment="Tokens available for use")
    reserved_balance = Column(Integer, nullable=False, default=0, comment="Tokens locked for pending operations")
    
    # Timestamps
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
    
    # Relationships
    user = relationship("User", back_populates="wallet")
    token_batches = relationship("TokenBatch", back_populates="wallet", cascade="all, delete-orphan")
    transactions = relationship("TokenTransaction", back_populates="wallet", cascade="all, delete-orphan", order_by="desc(TokenTransaction.created_at)")
    
    def __repr__(self):
        return f"<Wallet(id='{self.id}', user_id='{self.user_id}', balance={self.available_balance})>"
class TokenBatch(Base):
    """
    Token batches with FIFO expiry tracking - SOURCE OF TRUTH for token balances.
    
    This table tracks individual batches of tokens with their expiry dates, enabling
    FIFO (First-In-First-Out) consumption and automatic expiry management. Each batch
    represents tokens acquired from a specific source (signup bonus, subscription,
    top-up purchase, refund, etc.) and maintains its own lifecycle.
    
    Token Consumption Strategy:
        - Always consume from the oldest non-expired batch first (FIFO).
        - Check expiry using: expires_at > NOW() (no separate is_expired flag).
        - When a batch is fully consumed (remaining_tokens = 0), it can be archived.
        - Multi-batch operations are supported when a single batch has insufficient tokens.
    
    Attributes:
        id (str): Unique identifier for the token batch (UUID, primary key).
        wallet_id (str): Foreign key reference to wallets.id. Links this batch to a user's wallet.
        idempotency_key (str): Optional idempotency key for payment operations to prevent
            duplicate token credits from the same payment.
        initial_tokens (int): Original number of tokens when the batch was created (immutable).
        remaining_tokens (int): Tokens currently available for use in this batch. This is the
            source of truth for available balance calculation. Updated on each transaction.
        reserved_tokens (int): Tokens temporarily locked for pending operations (e.g., report
            generation in progress). Defaults to 0.
        amount (decimal): Amount paid for this batch in the specified currency. Null for
            free token sources like signup bonuses or referrals.
        currency (str): ISO currency code (USD, INR, EUR, etc.). Null for free token sources.
        source_type (str): How tokens were obtained. Uses TransactionSource enum values:
            - signup_bonus: Free tokens on account creation
            - subscription: Monthly subscription tokens
            - topup: One-time token purchase
            - referral: Bonus from referral program
            - refund: Tokens returned from failed operations
            - expiry: Negative batch for expired tokens (if tracked)
        payment_id (str): External payment service reference ID (e.g., Razorpay payment_id).
            Indexed for quick payment lookup. Null for free token sources.
        status (str): Current status of the batch. Uses TransactionStatus enum:
            - pending: Payment initiated but not confirmed
            - completed: Tokens successfully credited
            - failed: Payment or credit operation failed
        meta_data (JSONB): Additional flexible data storage for:
            - Error messages for failed batches
            - Token batch IDs involved in multi-batch operations
            - Payment gateway specific details
            - Promotional campaign information
        start_at (datetime): When this batch becomes active/usable (UTC).
        expires_at (datetime): When this batch expires and tokens become unusable (UTC).
            Used for expiry checks: WHERE expires_at > NOW().
        created_at (datetime): When this batch record was created (UTC, auto-generated).
    
    Relationships:
        wallet (Wallet): Many-to-one relationship with Wallet table. Multiple batches can
            belong to the same wallet.
    
    Balance Calculation:
        The wallet's denormalized balances are calculated from active batches:
        - available_balance = SUM(remaining_tokens) WHERE expires_at > NOW()
        - reserved_balance = SUM(reserved_tokens) WHERE expires_at > NOW()
        
        These calculations should be performed atomically during transactions to ensure
        consistency between TokenBatch (source of truth) and Wallet (denormalized cache).
    
    Indexes:
        - idx_token_batches_wallet_id: Fast lookup of all batches for a wallet
        - idx_token_batches_source_type: Filter by token source
        - idx_token_batches_expires_at: Efficient expiry checks
        - idx_token_batches_payment_id: Quick payment reference lookup
        - idx_token_batches_balance_calc: Composite index for balance calculation queries
    
    Example:
        User signs up and gets 50,000 tokens (90-day expiry):
        - Batch 1: initial=50000, remaining=50000, source=signup_bonus, expires in 90 days
        
        User subscribes to Plus tier (25,000 tokens, 30-day expiry):
        - Batch 2: initial=25000, remaining=25000, source=subscription, expires in 30 days
        
        User consumes 60,000 tokens (FIFO - oldest batch first):
        - Batch 1: remaining=15000 (50000 consumed)
        - Batch 2: remaining=15000 (10000 consumed)
    """
    __tablename__ = 'token_batches'
    
    id = Column(CHAR(36), primary_key=True, default=lambda: str(uuid7()), unique=True, nullable=False)
    wallet_id = Column(CHAR(36), ForeignKey('wallets.id', ondelete='CASCADE'), nullable=False, comment="Reference to wallet")
    idempotency_key = Column(String(100), nullable=True, comment="Idempotency key for payment")    
    # Token tracking - SOURCE OF TRUTH
    initial_tokens = Column(Integer, nullable=False, comment="Original tokens in batch")
    remaining_tokens = Column(Integer, nullable=False, comment="Available tokens (source of truth for balance)")
    reserved_tokens = Column(Integer, nullable=False, default=0, comment="Tokens locked for pending operations")
    
    # Payment tracking
    amount = Column(Numeric(10, 2), nullable=True, comment="Amount paid for this batch")
    currency = Column(String(10), nullable=True, comment="Currency: USD, INR, EUR, etc.")
    
    # Source tracking
    source_type = Column(
        Enum(TransactionSource, name="transaction_source_enum", native_enum=True),
        nullable=False,
        comment="Source: signup_bonus, subscription, topup, report_generation, refund, expiry"
    )
    payment_id = Column(String(50), nullable=True, index=True, comment="Reference to payment_id in payment service")
    
    # Status
    status = Column(
        Enum(TransactionStatus, name="transaction_status_enum", native_enum=True),
        nullable=False,
        default=TransactionStatus.PENDING,
        comment="Status: completed, pending, failed"
    )

    meta_data = Column(JSONB, nullable=True, comment="Additional data (error messages, token_batch_ids for multi-batch operations, etc.)")
    
    # Expiry (no is_expired flag - use expires_at > NOW() for expiry check)
    start_at = Column(DateTime(timezone=True), nullable=False, comment="When this batch expires")
    expires_at = Column(DateTime(timezone=True), nullable=False, comment="When this batch expires")
    
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    
    # Relationships
    wallet = relationship("Wallet", back_populates="token_batches")
    
    def __repr__(self):
        return f"<TokenBatch(id='{self.id}', remaining={self.remaining_tokens}, reserved={self.reserved_tokens}, expires='{self.expires_at}')>"
class TokenTransaction(Base):
    """
    Transaction ledger providing complete audit trail of all token movements.
    
    This table records every token operation (credits, debits, reservations, releases)
    with balance snapshots for historical tracking and auditing. It serves as an
    immutable log of all wallet activity, enabling transaction history, dispute
    resolution, and financial reconciliation.
    
    Important: The balance_after and reserved_after fields are SNAPSHOTS captured at
    transaction time for audit purposes. They are NOT the source of truth for current
    balances - always use TokenBatch for real-time balance calculations.
    
    Attributes:
        id (str): Unique identifier for the transaction (UUID, primary key).
        wallet_id (str): Foreign key reference to wallets.id. Links this transaction to
            a user's wallet.
        transaction_type (str): Type of token movement. Uses TransactionType enum:
            - credit: Adding tokens to wallet (signup, purchase, refund)
            - debit: Removing tokens from wallet (report generation, expiry)
            - reserve: Locking tokens for pending operations
            - release: Unlocking previously reserved tokens
        source_type (str): Business reason for the transaction. Uses TransactionSource enum:
            - signup_bonus: Free tokens on account creation
            - subscription: Monthly subscription token allocation
            - topup: One-time token purchase
            - referral: Bonus from referral program (referrer or referee)
            - report_generation: Tokens consumed for creating reports
            - refund: Tokens returned from failed/cancelled operations
            - expiry: Tokens removed due to batch expiration
        tokens (int): Number of tokens involved in this transaction:
            - Positive values: Credits (adding tokens)
            - Negative values: Debits (removing tokens)
            - Zero: Status changes without token movement
        balance_after (int): Snapshot of available balance immediately after this transaction
            completed. Used for audit trail and historical balance reconstruction.
        reserved_after (int): Snapshot of reserved balance immediately after this transaction
            completed. Used for tracking locked tokens over time.
        report_id (str): Foreign key reference to reports.id. Set when transaction is related
            to report generation (reserve, release, or debit for report). Indexed for quick
            report-related transaction lookup. Null for non-report transactions.
        token_batch_id (str): Foreign key reference to token_batches.id. Points to the primary
            token batch affected by this transaction. For multi-batch operations, this is the
            first batch, with others listed in meta_data. Can be null for failed transactions.
        status (str): Current status of the transaction. Uses TransactionStatus enum:
            - pending: Transaction initiated but not yet completed
            - completed: Transaction successfully processed
            - failed: Transaction failed (error details in meta_data)
            - reversed: Transaction was reversed/rolled back
        description (str): Human-readable description of the transaction for display in
            transaction history UI (e.g., "Report generation: AI Market Analysis").
        meta_data (JSONB): Flexible JSON storage for additional transaction details:
            - error_messages: Failure reasons for failed transactions
            - token_batch_ids: Array of batch IDs for multi-batch operations
            - report_title: Title of associated report
            - payment_gateway_response: External payment service data
            - reversal_reason: Why a transaction was reversed
            - original_transaction_id: Link to original transaction for reversals
        created_at (datetime): Timestamp when the transaction was created (UTC, auto-generated).
            Indexed for chronological queries and transaction history.
    
    Relationships:
        wallet (Wallet): Many-to-one relationship with Wallet table. All transactions for
            a wallet are accessible via wallet.transactions (ordered by created_at desc).
        token_batch (TokenBatch): Many-to-one relationship with TokenBatch table. Links to
            the primary batch affected by this transaction.
    
    Transaction Flow Examples:
        
        1. User signs up (50,000 token bonus):
           - type=credit, source=signup_bonus, tokens=+50000
           - balance_after=50000, reserved_after=0
           - status=completed
        
        2. User starts report generation (costs 5,000 tokens):
           - type=reserve, source=report_generation, tokens=0
           - balance_after=45000, reserved_after=5000
           - status=pending, report_id=<report_uuid>
        
        3. Report generation completes successfully:
           - type=debit, source=report_generation, tokens=-5000
           - balance_after=45000, reserved_after=0
           - status=completed, report_id=<report_uuid>
        
        4. Report generation fails:
           - type=release, source=report_generation, tokens=0
           - balance_after=50000, reserved_after=0
           - status=completed, report_id=<report_uuid>
           - meta_data={"error": "API timeout", "refund_reason": "generation_failed"}
    
    Indexes:
        - idx_token_transactions_wallet_id: Fast lookup of all transactions for a wallet
        - idx_token_transactions_transaction_type: Filter by transaction type
        - idx_token_transactions_source_type: Filter by source/reason
        - idx_token_transactions_status: Find pending or failed transactions
        - idx_token_transactions_report_id: Quick lookup of report-related transactions
        - idx_token_transactions_token_batch_id: Find all transactions affecting a batch
        - idx_token_transactions_created_at: Chronological ordering and time-based queries
    
    Audit Trail Usage:
        - Transaction history: SELECT * FROM token_transactions WHERE wallet_id=? ORDER BY created_at DESC
        - Balance at specific time: Calculate from snapshots up to that timestamp
        - Dispute resolution: Full history of all token movements with reasons
        - Financial reconciliation: Match transactions with payment gateway records
    """
    __tablename__ = 'token_transactions'
    
    id = Column(CHAR(36), primary_key=True, default=lambda: str(uuid7()), unique=True, nullable=False)
    wallet_id = Column(CHAR(36), ForeignKey('wallets.id', ondelete='CASCADE'), nullable=False, comment="Reference to wallet")
    
    # Transaction details
    transaction_type = Column(
        Enum(TransactionType, name="transaction_type_enum", native_enum=True),
        nullable=False,
        comment="Type: credit, debit, reserve, release"
    )
    source_type = Column(
        Enum(TransactionSource, name="transaction_source_enum", native_enum=True, create_constraint=False),
        nullable=False,
        comment="Source: signup_bonus, subscription, topup, report_generation, refund, expiry"
    )
    
    # Amounts
    tokens = Column(Integer, nullable=False, comment="Token amount (+ve for credit, -ve for debit)")
    
    # Balance snapshots (for audit trail, NOT source of truth)
    balance_after = Column(Integer, nullable=False, comment="Available balance snapshot after transaction")
    reserved_after = Column(Integer, nullable=False, comment="Reserved balance snapshot after transaction")
    
    # References
    report_id = Column(CHAR(36), nullable=True, index=True, comment="Reference to report (if applicable)")
    token_batch_id = Column(CHAR(36), ForeignKey('token_batches.id', ondelete='SET NULL'), nullable=True, comment="Reference to primary affected token batch")
    
    # Status and metadata
    status = Column(
        Enum(TransactionStatus, name="transaction_status_enum", native_enum=True),
        nullable=False,
        default=TransactionStatus.PENDING,
        comment="Status: completed, pending, failed, reversed"
    )
    description = Column(String(255), nullable=True, comment="Human-readable description")
    meta_data = Column(JSONB, nullable=True, comment="Additional data (error messages, token_batch_ids for multi-batch operations, etc.)")
    
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    
    # Relationships
    wallet = relationship("Wallet", back_populates="transactions")
    token_batch = relationship("TokenBatch", foreign_keys=[token_batch_id])
    
    def __repr__(self):
        return f"<TokenTransaction(id='{self.id}', type='{self.transaction_type}', tokens={self.tokens}, status='{self.status}')>"


# Standalone indexes, moved with the models they index.
Index('idx_wallets_user_id', Wallet.user_id)
Index('idx_token_batches_wallet_id', TokenBatch.wallet_id)
Index('idx_token_batches_source_type', TokenBatch.source_type)
Index('idx_token_batches_expires_at', TokenBatch.expires_at)
Index('idx_token_batches_payment_id', TokenBatch.payment_id)
Index('idx_token_batches_balance_calc', TokenBatch.wallet_id, TokenBatch.expires_at)
Index('idx_token_transactions_wallet_id', TokenTransaction.wallet_id)
Index('idx_token_transactions_transaction_type', TokenTransaction.transaction_type)
Index('idx_token_transactions_source_type', TokenTransaction.source_type)
Index('idx_token_transactions_status', TokenTransaction.status)
Index('idx_token_transactions_report_id', TokenTransaction.report_id)
Index('idx_token_transactions_token_batch_id', TokenTransaction.token_batch_id)
Index('idx_token_transactions_created_at', TokenTransaction.created_at)
