"""wallet.py: Wallet and token-related schemas"""
from typing import Optional, List, Any, Dict
from pydantic import BaseModel, Field, model_validator


class WalletBalanceResponse(BaseModel):
    """Response schema for wallet balance endpoint."""
    success: bool = True
    available_balance: int = Field(..., description="Tokens available for use")
    reserved_balance: int = Field(..., description="Tokens locked for pending operations")
    total_balance: int = Field(..., description="Total tokens (available + reserved)")
    subscription_tier: Optional[str] = Field(None, description="Current subscription tier")
    subscription_expires_at: Optional[str] = Field(None, description="Subscription expiry date")


class TransactionItem(BaseModel):
    """Schema for a single transaction."""
    id: str
    transaction_type: str
    source_type: str
    tokens: int
    amount: Optional[float] = None
    balance_after: int
    reserved_after: int
    report_id: Optional[str] = None
    payment_id: Optional[str] = None
    payment_method: Optional[str] = None
    payment_method_details: Optional[Dict[str, Any]] = None
    status: str
    description: Optional[str] = None
    created_at: str


class TransactionHistoryResponse(BaseModel):
    """Response schema for transaction history endpoint."""
    success: bool = True
    transactions: List[Dict[str, Any]]
    total: int
    limit: int
    offset: int


class WalletResponse(BaseModel):
    """Response schema for wallet details."""
    success: bool = True
    wallet: Optional[Dict[str, Any]] = None


class SubscriptionResponse(BaseModel):
    """Response schema for subscription details."""
    success: bool = True
    subscription: Optional[Dict[str, Any]] = None
    current_interval: Optional[Dict[str, Any]] = None


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
    tiers: Dict[str, TierInfo]


class PaymentWebhookRequest(BaseModel):
    """Request to credit tokens from a payment."""
    payment_id: str
    status: str
    amount: Optional[float] = None
    currency: Optional[str] = None
    payment_method: Optional[str] = None
    payment_method_details: Optional[Dict[str, Any]] = None
    


class PaymentWebhookResponse(BaseModel):
    """Response for credit from payment."""
    success: bool = True
    tokens_credited: Optional[int] = None
    batch_id: Optional[str] = None
    balance_after: Optional[int] = None
    message: Optional[str] = None
    idempotent: bool = False


class TopupInitiateRequest(BaseModel):
    """Request to initiate a token top-up."""
    amount: int = Field(..., gt=0)
    country_code: str = Field(..., description="ISO country code (e.g. IN, US)")
    idempotency_key: str = Field(..., description="Unique key for idempotency")

    @model_validator(mode='after')
    def validate_amounts(self) -> 'TopupInitiateRequest':
        country = self.country_code.upper()
        if country == 'IN' and self.amount < 1000:
            raise ValueError("Minimum top-up amount for India is 1000")
        elif country != 'IN' and self.amount < 10:
            raise ValueError("Minimum top-up amount is 10")
        return self


class TopupInitiateResponse(BaseModel):
    """Response with payment parameters for top-up."""
    success: bool = True
    data: Optional[Dict[str, Any]] = None
    payment_params: Optional[Dict[str, Any]] = None  # Legacy support


class SubscribeRequest(BaseModel):
    """
    Request for /subscribe endpoint. Single endpoint for both new subscription and plan change.
    - to_update=False: create new subscription (plan_tier, plan_duration, country required).
    - to_update=True: change existing plan (plan_tier, plan_duration required).
    """
    to_update: bool = Field(..., description="True to change existing plan, False to create new subscription")
    idempotency_key: str = Field(..., description="Unique key for idempotency")
    plan_tier: Optional[str] = Field(None, description="PLUS or PRO (required for both create and update)")
    plan_duration: Optional[str] = Field(None, description="MONTHLY or YEARLY (required for both create and update)")
    country: Optional[str] = Field(None, description="ISO country code e.g. IN, US (required when to_update=False)")


class UpdateSubscriptionRequest(BaseModel):
    """Legacy: Request to change subscription plan. Prefer SubscribeRequest with to_update=True."""
    new_plan_tier: str = Field(..., description="PLUS or PRO")
    new_plan_duration: str = Field(..., description="MONTHLY or YEARLY")
    idempotency_key: str = Field(..., description="Unique key for idempotency")


class SubscribeResponse(BaseModel):
    """Response with subscription details."""
    success: bool = True
    data: Optional[Dict[str, Any]] = None
    subscription_id: Optional[str] = None


class SubscriptionWebhookEvent(BaseModel):
    """Schema for subscription webhook events."""
    subscription_id: str
    event: str  # SUBSCRIPTION_*
    payment_id: Optional[str] = None
    status: Optional[str] = None
    gateway: Optional[str] = None
    plan_tier: Optional[str] = None
    plan_duration: Optional[str] = None
    paid_count: Optional[int] = None
    payment_method: Optional[str] = None
    payment_method_details: Optional[Dict[str, Any]] = None
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    next_payment_datetime: Optional[str] = None
    amount: Optional[float] = None
    currency: Optional[str] = None
    sale_id: Optional[str] = None
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    # Fields for SUBSCRIPTION_UPDATED event (plan upgrade/downgrade)
    gateway_plan_id: Optional[str] = None      # Razorpay plan ID (e.g. plan_xxxxxxxxxxxx)
    current_start: Optional[str] = None        # Current billing period start, ISO 8601 (UTC)
    current_end: Optional[str] = None          # Current billing period end, ISO 8601 (UTC)
    # Fields for SUBSCRIPTION_PAYMENT_REFUNDED event
    refund_id: Optional[str] = None
    refund_status: Optional[str] = None
    refund_created_at: Optional[str] = None
    refund_speed_requested: Optional[str] = None
    refund_speed_processed: Optional[str] = None
    gateway_payment_id: Optional[str] = None
    gateway_invoice_id: Optional[str] = None
    gateway_order_id: Optional[str] = None
    original_payment_amount: Optional[float] = None
    amount_refunded_total: Optional[float] = None
    payment_refund_status: Optional[str] = None  # partial/full
    payment_created_at: Optional[str] = None
    payment_description: Optional[str] = None
    razorpay_event: Optional[str] = None



class WalletErrorResponse(BaseModel):
    """Generic error response for wallet operations."""
    success: bool = False
    error: str
    error_code: Optional[str] = None


class PaymentStatusResponse(BaseModel):
    """Response schema for payment status polling endpoint."""
    success: bool = True
    payment_id: Optional[str] = None
    status: Optional[str] = Field(None, description="Status: completed, pending, failed, reversed")
    tokens: Optional[int] = Field(None, description="Number of tokens in the batch")
    amount: Optional[float] = Field(None, description="Amount paid in the specified currency")
    currency: Optional[str] = Field(None, description="Currency (USD, INR, etc.)")
    source_type: Optional[str] = Field(None, description="Source type: topup, subscription, etc.")
    created_at: Optional[str] = Field(None, description="When the payment was created")
    expires_at: Optional[str] = Field(None, description="When the tokens will expire")


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
    tax_details: Optional[List[Dict[str, Any]]] = None
    error: Optional[str] = None


class UpdatePaymentMethodRequest(BaseModel):
    """Request to update subscription payment method."""
    subscription_id: str = Field(..., description="Current subscription ID")
    cancel_at_cycle_end: bool = Field(
        default=True, 
        description="Cancel at cycle end (recommended) or immediately"
    )


class UpdatePaymentMethodResponse(BaseModel):
    """Response for payment method update."""
    success: bool
    message: str
    data: Optional[Dict[str, Any]] = None
    error: Optional[str] = None


class CancelSubscriptionRequest(BaseModel):
    """Request to cancel a subscription."""
    reason: Optional[str] = Field(
        default=None,
        description="Optional reason for cancellation"
    )


class CancelSubscriptionResponse(BaseModel):
    """Response for subscription cancellation."""
    success: bool
    message: str
    data: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
