"""wallet.py: Wallet and token-related schemas"""

from typing import Any

from pydantic import BaseModel, Field, model_validator


class WalletBalanceResponse(BaseModel):
    """Response schema for wallet balance endpoint."""

    success: bool = True
    available_balance: int = Field(..., description="Tokens available for use")
    reserved_balance: int = Field(..., description="Tokens locked for pending operations")
    total_balance: int = Field(..., description="Total tokens (available + reserved)")
    subscription_tier: str | None = Field(None, description="Current subscription tier")
    subscription_expires_at: str | None = Field(None, description="Subscription expiry date")


class TransactionItem(BaseModel):
    """Schema for a single transaction."""

    id: str
    transaction_type: str
    source_type: str
    tokens: int
    amount: float | None = None
    balance_after: int
    reserved_after: int
    report_id: str | None = None
    payment_id: str | None = None
    payment_method: str | None = None
    payment_method_details: dict[str, Any] | None = None
    status: str
    description: str | None = None
    created_at: str


class TransactionHistoryResponse(BaseModel):
    """Response schema for transaction history endpoint."""

    success: bool = True
    transactions: list[dict[str, Any]]
    total: int
    limit: int
    offset: int


class WalletResponse(BaseModel):
    """Response schema for wallet details."""

    success: bool = True
    wallet: dict[str, Any] | None = None


class SubscriptionResponse(BaseModel):
    """Response schema for subscription details."""

    success: bool = True
    subscription: dict[str, Any] | None = None
    current_interval: dict[str, Any] | None = None


class TierPricing(BaseModel):
    """Pricing details for a tier interval."""

    cost: float
    currency: str
    tokens_per_month: int


class TierInfo(BaseModel):
    """Information about a subscription tier."""

    tier: str
    monthly: TierPricing
    yearly: TierPricing
    signup_bonus: int
    report_cost: int


class AllTiersResponse(BaseModel):
    """Response with all tier information."""

    success: bool = True
    tiers: dict[str, TierInfo]


class PaymentWebhookRequest(BaseModel):
    """Request to credit tokens from a payment."""

    payment_id: str
    status: str
    amount: float | None = None
    currency: str | None = None
    payment_method: str | None = None
    payment_method_details: dict[str, Any] | None = None


class PaymentWebhookResponse(BaseModel):
    """Response for credit from payment."""

    success: bool = True
    tokens_credited: int | None = None
    batch_id: str | None = None
    balance_after: int | None = None
    message: str | None = None
    idempotent: bool = False


class TopupInitiateRequest(BaseModel):
    """Request to initiate a token top-up."""

    amount: int = Field(..., gt=0)
    country_code: str = Field(..., description="ISO country code (e.g. IN, US)")
    idempotency_key: str = Field(..., description="Unique key for idempotency")

    @model_validator(mode="after")
    def validate_amounts(self) -> TopupInitiateRequest:
        country = self.country_code.upper()
        if country == "IN" and self.amount < 1000:
            raise ValueError("Minimum top-up amount for India is 1000")
        elif country != "IN" and self.amount < 10:
            raise ValueError("Minimum top-up amount is 10")
        return self


class TopupInitiateResponse(BaseModel):
    """Response with payment parameters for top-up."""

    success: bool = True
    data: dict[str, Any] | None = None
    payment_params: dict[str, Any] | None = None  # Legacy support


class SubscribeRequest(BaseModel):
    """
    Request for /subscribe endpoint. Single endpoint for both new subscription and plan change.
    - to_update=False: create new subscription (plan_tier, plan_duration, country required).
    - to_update=True: change existing plan (plan_tier, plan_duration required).
    """

    to_update: bool = Field(
        ..., description="True to change existing plan, False to create new subscription"
    )
    idempotency_key: str = Field(..., description="Unique key for idempotency")
    plan_tier: str | None = Field(
        None, description="PLUS or PRO (required for both create and update)"
    )
    plan_duration: str | None = Field(
        None, description="MONTHLY or YEARLY (required for both create and update)"
    )
    country: str | None = Field(
        None, description="ISO country code e.g. IN, US (required when to_update=False)"
    )


class UpdateSubscriptionRequest(BaseModel):
    """Legacy: Request to change subscription plan. Prefer SubscribeRequest with to_update=True."""

    new_plan_tier: str = Field(..., description="PLUS or PRO")
    new_plan_duration: str = Field(..., description="MONTHLY or YEARLY")
    idempotency_key: str = Field(..., description="Unique key for idempotency")


class SubscribeResponse(BaseModel):
    """Response with subscription details."""

    success: bool = True
    data: dict[str, Any] | None = None
    subscription_id: str | None = None


class SubscriptionWebhookEvent(BaseModel):
    """Schema for subscription webhook events."""

    subscription_id: str
    event: str  # SUBSCRIPTION_*
    payment_id: str | None = None
    status: str | None = None
    gateway: str | None = None
    plan_tier: str | None = None
    plan_duration: str | None = None
    paid_count: int | None = None
    payment_method: str | None = None
    payment_method_details: dict[str, Any] | None = None
    start_date: str | None = None
    end_date: str | None = None
    next_payment_datetime: str | None = None
    amount: float | None = None
    currency: str | None = None
    sale_id: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    # Fields for SUBSCRIPTION_UPDATED event (plan upgrade/downgrade)
    gateway_plan_id: str | None = None  # Razorpay plan ID (e.g. plan_xxxxxxxxxxxx)
    current_start: str | None = None  # Current billing period start, ISO 8601 (UTC)
    current_end: str | None = None  # Current billing period end, ISO 8601 (UTC)
    # Fields for SUBSCRIPTION_PAYMENT_REFUNDED event
    refund_id: str | None = None
    refund_status: str | None = None
    refund_created_at: str | None = None
    refund_speed_requested: str | None = None
    refund_speed_processed: str | None = None
    gateway_payment_id: str | None = None
    gateway_invoice_id: str | None = None
    gateway_order_id: str | None = None
    original_payment_amount: float | None = None
    amount_refunded_total: float | None = None
    payment_refund_status: str | None = None  # partial/full
    payment_created_at: str | None = None
    payment_description: str | None = None
    razorpay_event: str | None = None


class WalletErrorResponse(BaseModel):
    """Generic error response for wallet operations."""

    success: bool = False
    error: str
    error_code: str | None = None


class PaymentStatusResponse(BaseModel):
    """Response schema for payment status polling endpoint."""

    success: bool = True
    payment_id: str | None = None
    status: str | None = Field(None, description="Status: completed, pending, failed, reversed")
    tokens: int | None = Field(None, description="Number of tokens in the batch")
    amount: float | None = Field(None, description="Amount paid in the specified currency")
    currency: str | None = Field(None, description="Currency (USD, INR, etc.)")
    source_type: str | None = Field(None, description="Source type: topup, subscription, etc.")
    created_at: str | None = Field(None, description="When the payment was created")
    expires_at: str | None = Field(None, description="When the tokens will expire")


class TaxCalculationRequest(BaseModel):
    """Request schema for calculating taxes."""

    country_code: str = Field(..., description="ISO country code (e.g. IN, US)")
    amount_in_usd: float = Field(..., gt=0, description="Amount in USD to calculate tax on")


class SubscriptionTaxCalculationRequest(BaseModel):
    """Request schema for calculating taxes for a subscription plan."""

    country_code: str = Field(..., description="ISO country code (e.g. IN, US)")
    plan_name: str = Field(..., description="Subscription plan name (e.g. free, plus, pro)")
    plan_type: str = Field(..., description="Plan type (e.g. monthly, yearly)")


class TaxCalculationResponse(BaseModel):
    """
    Response schema for tax calculation.

    Note: tokens_per_month represents:
    - For subscription: tokens credited per month
    - For topup: total tokens in single purchase (field name kept for consistency)
    """

    success: bool = True
    country_code: str
    currency: str
    amount_in_currency: float
    tokens_per_month: int
    tax_percentage: float
    tax_amount: float
    total_amount: float
    tax_details: list[dict[str, Any]] | None = None
    error: str | None = None


class UpdatePaymentMethodRequest(BaseModel):
    """Request to update subscription payment method."""

    subscription_id: str = Field(..., description="Current subscription ID")
    cancel_at_cycle_end: bool = Field(
        default=True, description="Cancel at cycle end (recommended) or immediately"
    )


class UpdatePaymentMethodResponse(BaseModel):
    """Response for payment method update."""

    success: bool
    message: str
    data: dict[str, Any] | None = None
    error: str | None = None


class CancelSubscriptionRequest(BaseModel):
    """Request to cancel a subscription."""

    reason: str | None = Field(default=None, description="Optional reason for cancellation")


class CancelSubscriptionResponse(BaseModel):
    """Response for subscription cancellation."""

    success: bool
    message: str
    data: dict[str, Any] | None = None
    error: str | None = None
