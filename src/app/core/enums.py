"""
Database Enums Module

This module provides enum classes for database fields to ensure type safety
and consistency across the application.
"""

from enum import Enum


class ReportStatus(Enum):
    """
    Enum representing the status of a report or report version.

    Status Flow:
        DRAFT → ANALYSIS_IN_PROGRESS → ANALYSIS_COMPLETED → GENERATING_OUTPUT
        → OUTPUT_GENERATED

        Special States:
        - REDO_ANALYSIS: User refined a card, needs regeneration
        - UPDATES_AVAILABLE: New updates detected (TODO)
        - AWAITING_CONFIRMATION: Pending user approval (TODO)
        - ERROR_GENERATION_REPORT: Report generation failed

    Statuses:
        DRAFT: Chat just started, no cards generated yet
        AWAITING_CONFIRMATION: Pending user confirmation (TODO: not implemented)
        ANALYSIS_IN_PROGRESS: Card generation has started
        ANALYSIS_COMPLETED: All cards have been generated
        GENERATING_OUTPUT: Report PDF/HTML/PPTX/Info-PDF generation in progress
        OUTPUT_GENERATED: Report files have been successfully generated
        UPDATES_AVAILABLE: New updates detected (TODO: not implemented)
        REDO_ANALYSIS: User refined a card after generation
        ERROR_GENERATION_REPORT: Report generation failed
    """

    DRAFT = "draft"
    AWAITING_CONFIRMATION = "awaiting-confirmation"
    ANALYSIS_IN_PROGRESS = "analysis-in-progress"
    ANALYSIS_COMPLETED = "analysis-completed"
    GENERATING_OUTPUT = "generating-output"
    OUTPUT_GENERATED = "output-generated"
    UPDATES_AVAILABLE = "updates-available"
    REDO_ANALYSIS = "redo-analysis"
    ERROR_GENERATION_REPORT = "error-generation-report"

    # # Legacy statuses (commented out - may be restored if requirements change)
    # IN_PROGRESS = "in_progress"
    # FAILED = "failed"
    # REPORT_GENERATED = "report_generated"
    # PRESENTATION_GENERATED = "presentation_generated"

    @classmethod
    def values(cls):
        """Return list of all status values."""
        return [status.value for status in cls]

    # @classmethod
    # def from_s3_uri(cls, s3_uri: dict) -> str:
    #     """
    #     Determine status from s3_uri contents.
    #     (Commented out - uses legacy statuses)
    #
    #     Args:
    #         s3_uri: Dictionary containing file paths
    #
    #     Returns:
    #         Appropriate status value
    #     """
    #     if not s3_uri:
    #         return cls.IN_PROGRESS.value
    #
    #     if s3_uri.get('pptx'):
    #         return cls.PRESENTATION_GENERATED.value
    #     elif s3_uri.get('pdf') or s3_uri.get('html'):
    #         return cls.REPORT_GENERATED.value
    #     else:
    #         return cls.IN_PROGRESS.value


class FileType(Enum):
    """
    Enum representing supported file types for reports.
    """

    PDF = "pdf"
    MD = "md"
    HTML = "html"
    PPTX = "pptx"
    DOCX = "docx"


class MessageType(Enum):
    """
    Enum representing the type of message in chat.
    """

    HUMAN = "human"
    SYSTEM = "system"
    AI = "ai"
    TOOL = "tool"


class RefinementType(Enum):
    """
    Enum representing the type of card refinement or modification.

    Types:
        REFINE_SECTION: AI refinement of a section
        REFINE_SUBSECTION: AI refinement of a subsection
        REFINE_VISUALIZATION: AI refinement of a visualization
        REFINE_TOC: Table of contents update (after card deletion/modification)
        DELETE_SECTION: Section deletion
        DELETE_SUBSECTION: Subsection deletion
        MANUAL_EDIT: Manual edit by user via /edit-card-content (legacy)
        EDIT_SECTION: Manual section edit by user via /edit-card-content
    """

    REFINE_SECTION = "re_section"
    REFINE_SUBSECTION = "re_subsection"
    REFINE_VISUALIZATION = "re_visualization"
    REFINE_TOC = "re_toc"
    DELETE_SECTION = "del_section"
    DELETE_SUBSECTION = "del_subsection"
    MANUAL_EDIT = "manual_edit"
    EDIT_SECTION = "edit_section"

    @classmethod
    def values(cls):
        """Return list of all refinement type values."""
        return [refinement.value for refinement in cls]


# ============================================================================
# Wallet/Token System Enums
# ============================================================================


class SubscriptionTier(Enum):
    """
    Enum representing subscription tiers/plans.

    Tiers:
        FREE: Free tier with signup bonus only (50,000 tokens)
        PLUS: Plus tier ($20/month, +25,000 tokens/month)
        PRO: Pro tier ($100/month, +150,000 tokens/month)
    """

    FREE = "free"
    PLUS = "plus"
    PRO = "pro"

    @classmethod
    def values(cls):
        """Return list of all tier values."""
        return [tier.value for tier in cls]


class TransactionType(Enum):
    """
    Enum representing types of token transactions.

    Types:
        CREDIT: Tokens added to wallet (signup, subscription, topup)
        DEBIT: Tokens consumed (report generation completed)
        RESERVE: Tokens locked for pending operation (report generation started)
        RELEASE: Reserved tokens returned (operation failed/cancelled)
    """

    CREDIT = "credit"
    DEBIT = "debit"
    RESERVE = "reserve"
    RELEASE = "release"

    @classmethod
    def values(cls):
        """Return list of all transaction type values."""
        return [t.value for t in cls]


class TransactionSource(Enum):
    """
    Enum representing the source/reason for a token transaction.

    Sources:
        SIGNUP_BONUS: One-time tokens on user signup
        SUBSCRIPTION: Monthly tokens from Plus/Pro subscription
        TOPUP: Additional tokens purchased separately
        REPORT_GENERATION: Tokens consumed for generating a report
        REFUND: Tokens refunded (e.g., failed operation, customer service)
        EXPIRY: Tokens expired (batch reached expiry date)
        BONUS: Manual bonus credits added by administrators
    """

    SIGNUP_BONUS = "SIGNUP_BONUS"
    SUBSCRIPTION = "SUBSCRIPTION"
    TOPUP = "TOPUP"
    REPORT_GENERATION = "REPORT_GENERATION"
    REFUND = "REFUND"
    EXPIRY = "EXPIRY"
    BONUS = "BONUS"

    @classmethod
    def values(cls):
        """Return list of all transaction source values."""
        return [s.value for s in cls]


class TransactionStatus(Enum):
    """
    Enum representing the status of a token transaction or token batch.

    Statuses:
        COMPLETED: Transaction/batch successfully processed
        PENDING: Transaction in progress (e.g., reservation waiting for completion)
        FAILED: Transaction failed
        REVERSED: Transaction was reversed/cancelled (for transactions)
        INVALIDATED: Batch was invalidated due to plan change (for token batches)
    """

    COMPLETED = "COMPLETED"
    PENDING = "PENDING"
    FAILED = "FAILED"
    REVERSED = "REVERSED"
    INVALIDATED = "INVALIDATED"  # For token batches during plan changes

    @classmethod
    def values(cls):
        """Return list of all transaction status values."""
        return [s.value for s in cls]


class SubscriptionStatus(Enum):
    """
    Enum representing the status of a subscription or subscription interval.

    Statuses:
        ACTIVE: Subscription is currently active (Legacy)
        CREATED: Subscription created, awaiting user approval
        ACTIVATED: User approved, awaiting first charge
        CHARGED: Payment successful (recurring state)
        PENDING: Payment failed, retries in progress
        HALTED: Payment failed permanently
        PAUSED: Voluntarily paused
        CANCELLED: Subscription cancelled
        COMPLETED: All billing cycles completed
        EXPIRED: Subscription expired
        REFUNDED: Subscription was refunded
    """

    ACTIVE = "active"
    CREATED = "created"
    AUTHENTICATED = "authenticated"
    CHARGED = "charged"
    PENDING = "pending"
    HALTED = "halted"
    PAUSED = "paused"
    CANCELLED = "cancelled"
    COMPLETED = "completed"
    EXPIRED = "expired"
    REFUNDED = "refunded"

    @classmethod
    def values(cls):
        """Return list of all subscription status values."""
        return [s.value for s in cls]


# ============================================================================
# File Upload System Enums
# ============================================================================


class UploadedFileStatus(Enum):
    """
    Enum representing the lifecycle status of an uploaded file.

    Status Flow:
        PENDING_OPENAI_UPLOAD → PROCESSING → COMPLETED
                                           → FAILED

    Statuses:
        PENDING_OPENAI_UPLOAD: File saved to S3, awaiting OpenAI upload.
        PROCESSING: File is being uploaded to OpenAI / attached to vector store.
        COMPLETED: File fully processed and ready for queries.
        FAILED: Processing failed at some stage.
        S3_FILE_MISSING: S3 object was deleted or unreachable (detected by cron).
        EXPIRED: File auto-expired after retention period (future cron use).
    """

    PENDING_OPENAI_UPLOAD = "pending_openai_upload"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    S3_FILE_MISSING = "s3_file_missing"
    EXPIRED = "expired"

    @classmethod
    def values(cls):
        """Return list of all uploaded file status values."""
        return [s.value for s in cls]


class FileUsageType(Enum):
    """
    Enum indicating how a file was associated with a chat.

    Values:
        NEW_UPLOAD: The user uploaded a brand-new file in this chat.
        REFERENCE: The user picked this file from the memory library.
    """

    NEW_UPLOAD = "new_upload"
    REFERENCE = "reference"

    @classmethod
    def values(cls):
        """Return list of all file usage type values."""
        return [t.value for t in cls]


class FileUploadContext(Enum):
    """
    Enum representing the context in which a file was originally uploaded.

    Values:
        IN_CHAT: File was uploaded as part of a chat conversation.
        STANDALONE: File was uploaded independently (future use).
    """

    IN_CHAT = "in_chat"
    STANDALONE = "standalone"

    @classmethod
    def values(cls):
        """Return list of all file upload context values."""
        return [c.value for c in cls]
