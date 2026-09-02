"""
wallet_api.py: REST API endpoints for wallet operations.
"""

from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Header, Query
from fastapi.responses import JSONResponse
import time

from src.core.token_auth import get_current_active_user
from src.db.db_utils import async_session_scope
from src.services.wallet_service import WalletService
from src.config.log_helper import setup_logging
from src.config.constants import COUNTRY_TO_CURRENCY, FRONTEND_URL
from src.resources.schemas.wallet import (
    SubscriptionTaxCalculationRequest,
    WalletBalanceResponse,
    TransactionHistoryResponse,
    AllTiersResponse,
    PaymentWebhookRequest,
    PaymentWebhookResponse,
    TopupInitiateRequest,
    TopupInitiateResponse,
    SubscribeRequest,
    SubscribeResponse,
    WalletErrorResponse,
    SubscriptionResponse,
    PaymentStatusResponse,
    TaxCalculationRequest,
    TaxCalculationResponse,
    SubscriptionWebhookEvent,
    UpdatePaymentMethodRequest,
    UpdatePaymentMethodResponse,
    CancelSubscriptionRequest,
    CancelSubscriptionResponse
)
from src.resources.schemas.referral import (
    ReferralInfoResponse,
    ReferralErrorResponse
)
from src.db.wallet_functions import verify_webhook_api_key

from typing import Dict, Any

logger = setup_logging(__file__)
router = APIRouter(prefix="/wallet", tags=["Wallet"])


@router.get(
    "/balance",
    response_model=WalletBalanceResponse,
    responses={
        401: {"model": WalletErrorResponse},
        500: {"model": WalletErrorResponse},
    },
)
async def get_wallet_balance(user_id: str = Depends(get_current_active_user)):
    """Get current wallet balance for the authenticated user."""
    start_time = time.time()
    logger.info(f"[BALANCE_REQUEST] user_id={user_id}")
    
    try:
        async with async_session_scope() as session:
            try:
                result = await WalletService.get_balance(user_id, session)
                
                if not result.get("success"):
                    error_msg = result.get("error", "Failed to get balance")
                    logger.error(
                        f"[BALANCE_FAILED] user_id={user_id}, error={error_msg}, "
                        f"elapsed_time={time.time() - start_time:.2f}s"
                    )
                    return JSONResponse(
                        status_code=500,
                        content={
                            "success": False,
                            "error": "Unable to retrieve your wallet balance. Please try again in a moment.",
                        },
                    )
                
                elapsed_time = time.time() - start_time
                logger.info(
                    f"[BALANCE_SUCCESS] user_id={user_id}, "
                    f"available={result.get('available_balance', 0)}, "
                    f"reserved={result.get('reserved_balance', 0)}, "
                    f"total={result.get('total_balance', 0)}, "
                    f"tier={result.get('subscription_tier', 'FREE')}, "
                    f"elapsed_time={elapsed_time:.2f}s"
                )
                
                return WalletBalanceResponse(
                    success=True,
                    available_balance=result.get("available_balance", 0),
                    reserved_balance=result.get("reserved_balance", 0),
                    total_balance=result.get("total_balance", 0),
                    subscription_tier=result.get("subscription_tier"),
                    subscription_expires_at=result.get("subscription_expires_at"),
                )
                
            except ValueError as e:
                logger.error(
                    f"[BALANCE_VALIDATION_ERROR] user_id={user_id}, error={str(e)}, "
                    f"elapsed_time={time.time() - start_time:.2f}s",
                    exc_info=True
                )
                return JSONResponse(
                    status_code=400,
                    content={
                        "success": False,
                        "error": "Invalid request format. Please check your request and try again."
                    },
                )
            except Exception as e:
                logger.critical(
                    f"[BALANCE_UNEXPECTED_ERROR] user_id={user_id}, error={str(e)}, "
                    f"error_type={type(e).__name__}, elapsed_time={time.time() - start_time:.2f}s",
                    exc_info=True
                )
                await session.rollback()
                raise
                
    except HTTPException:
        raise
    except Exception as e:
        logger.critical(
            f"[BALANCE_CRITICAL_ERROR] user_id={user_id}, error={str(e)}, "
            f"error_type={type(e).__name__}, elapsed_time={time.time() - start_time:.2f}s",
            exc_info=True
        )
        return JSONResponse(
            status_code=500,
            content={
                "success": False,
                "error": "An unexpected error occurred. Our team has been notified. Please try again later."
            },
        )


@router.get(
    "/transactions",
    response_model=TransactionHistoryResponse,
    responses={
        401: {"model": WalletErrorResponse},
        500: {"model": WalletErrorResponse},
    },
)
async def get_transaction_history(
    user_id: str = Depends(get_current_active_user),
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    transaction_type: Optional[str] = Query(None),
):
    """Get transaction history for the authenticated user."""
    start_time = time.time()
    logger.info(
        f"[TRANSACTIONS_REQUEST] user_id={user_id}, limit={limit}, "
        f"offset={offset}, type={transaction_type or 'all'}"
    )
    
    # Validate transaction type if provided
    if transaction_type and transaction_type not in ["credit", "debit", "reserve", "release"]:
        logger.warning(
            f"[TRANSACTIONS_INVALID_TYPE] user_id={user_id}, invalid_type={transaction_type}, "
            f"valid_types=[credit, debit, reserve, release]"
        )
        return JSONResponse(
            status_code=400,
            content={
                "success": False,
                "error": f"Invalid transaction type '{transaction_type}'. Must be one of: credit, debit, reserve, release"
            },
        )
    
    try:
        async with async_session_scope() as session:
            try:
                result = await WalletService.get_transactions(
                    user_id=user_id,
                    session=session,
                    limit=limit,
                    offset=offset,
                    transaction_type=transaction_type,
                )
                
                if not result.get("success"):
                    error_msg = result.get("error", "Failed to get transactions")
                    logger.error(
                        f"[TRANSACTIONS_FAILED] user_id={user_id}, error={error_msg}, "
                        f"elapsed_time={time.time() - start_time:.2f}s"
                    )
                    return JSONResponse(
                        status_code=500,
                        content={
                            "success": False,
                            "error": "Unable to retrieve your transaction history. Please try again in a moment.",
                        },
                    )
                
                transaction_count = len(result.get("transactions", []))
                elapsed_time = time.time() - start_time
                logger.info(
                    f"[TRANSACTIONS_SUCCESS] user_id={user_id}, "
                    f"returned={transaction_count}, total={result.get('total', 0)}, "
                    f"limit={limit}, offset={offset}, type_filter={transaction_type or 'none'}, "
                    f"elapsed_time={elapsed_time:.2f}s"
                )
                
                return TransactionHistoryResponse(
                    success=True,
                    transactions=result.get("transactions", []),
                    total=result.get("total", 0),
                    limit=limit,
                    offset=offset,
                )
                
            except ValueError as e:
                logger.error(
                    f"[TRANSACTIONS_VALIDATION_ERROR] user_id={user_id}, error={str(e)}, "
                    f"limit={limit}, offset={offset}, type={transaction_type}, "
                    f"elapsed_time={time.time() - start_time:.2f}s",
                    exc_info=True
                )
                return JSONResponse(
                    status_code=400,
                    content={
                        "success": False,
                        "error": "Invalid request parameters. Please check limit, offset, and transaction type."
                    },
                )
            except Exception as e:
                logger.critical(
                    f"[TRANSACTIONS_UNEXPECTED_ERROR] user_id={user_id}, error={str(e)}, "
                    f"error_type={type(e).__name__}, elapsed_time={time.time() - start_time:.2f}s",
                    exc_info=True
                )
                await session.rollback()
                raise
                
    except HTTPException:
        raise
    except Exception as e:
        logger.critical(
            f"[TRANSACTIONS_CRITICAL_ERROR] user_id={user_id}, error={str(e)}, "
            f"error_type={type(e).__name__}, elapsed_time={time.time() - start_time:.2f}s",
            exc_info=True
        )
        return JSONResponse(
            status_code=500,
            content={
                "success": False,
                "error": "An unexpected error occurred. Our team has been notified. Please try again later."
            },
        )


@router.get(
    "/subscription",
    response_model=SubscriptionResponse,
    responses={
        401: {"model": WalletErrorResponse},
        500: {"model": WalletErrorResponse},
    },
)
async def get_subscription(user_id: str = Depends(get_current_active_user)):
    """
    Get subscription details and active subscription plan for the authenticated user.
    
    Returns:
        - If user has a subscription: returns subscription details
        - If user has no subscription (FREE tier): returns success=True with subscription=None
        - Only returns error for actual failures (e.g., database errors)
    """
    start_time = time.time()
    logger.info(f"[SUBSCRIPTION_GET_REQUEST] user_id={user_id}")
    
    try:
        async with async_session_scope() as session:
            result = await WalletService.get_active_subscription(user_id, session)
            
            # Only treat it as an error if there's an actual error message
            # subscription=None is valid (user is on FREE tier)
            if not result.get("success") and result.get("error"):
                logger.error(
                    f"[SUBSCRIPTION_GET_FAILED] user_id={user_id}, "
                    f"error={result.get('error')}, elapsed_time={time.time() - start_time:.2f}s"
                )
                return JSONResponse(
                    status_code=500,
                    content={
                        "success": False,
                        "error": "Unable to retrieve your subscription details. Please try again in a moment.",
                    },
                )
            
            # Success case - may have subscription or be on FREE tier (subscription=None)
            subscription_tier = result.get('subscription', {}).get('current_tier', 'FREE') if result.get('subscription') else 'FREE'
            elapsed_time = time.time() - start_time
            logger.info(
                f"[SUBSCRIPTION_GET_SUCCESS] user_id={user_id}, "
                f"has_subscription={result.get('subscription') is not None}, "
                f"tier={subscription_tier}, elapsed_time={elapsed_time:.2f}s"
            )
            
            return SubscriptionResponse(
                success=True,
                subscription=result.get("subscription"),
                current_interval=result.get("current_interval"),
            )
    except Exception as e:
        logger.critical(
            f"[SUBSCRIPTION_GET_CRITICAL_ERROR] user_id={user_id}, "
            f"error={str(e)}, error_type={type(e).__name__}, "
            f"elapsed_time={time.time() - start_time:.2f}s",
            exc_info=True
        )
        return JSONResponse(
            status_code=500,
            content={
                "success": False,
                "error": "An unexpected error occurred. Our team has been notified. Please try again later."
            },
        )


@router.get("/tiers", response_model=AllTiersResponse)
async def get_all_tiers(country_code: Optional[str] = Query("default")):
    """Get information about all subscription tiers (no auth required)."""
    start_time = time.time()
    logger.info(f"[TIERS_REQUEST] country_code={country_code}")
    
    try:
        tiers = WalletService.get_all_tiers(country_code)
        elapsed_time = time.time() - start_time
        logger.info(
            f"[TIERS_SUCCESS] country_code={country_code}, "
            f"tiers_count={len(tiers)}, elapsed_time={elapsed_time:.2f}s"
        )
        return AllTiersResponse(success=True, tiers=tiers)
    except Exception as e:
        logger.critical(
            f"[TIERS_ERROR] country_code={country_code}, error={str(e)}, "
            f"error_type={type(e).__name__}, elapsed_time={time.time() - start_time:.2f}s",
            exc_info=True
        )
        return JSONResponse(
            status_code=500,
            content={
                "success": False,
                "error": "Unable to retrieve subscription tiers. Please try again later."
            },
        )


@router.post(
    "/subscribe",
    response_model=SubscribeResponse,
    responses={
        400: {"model": WalletErrorResponse},
        401: {"model": WalletErrorResponse},
        404: {"model": WalletErrorResponse},
    },
    summary="Subscribe or change plan",
    description="Create a new subscription (to_update=False) or change existing plan (to_update=True).",
)
async def subscribe(
    data: SubscribeRequest, user_id: str = Depends(get_current_active_user)
):
    """Single endpoint: create new subscription or change plan based on to_update."""
    start_time = time.time()
    is_update = data.to_update

    logger.info(
        f"[SUBSCRIBE_REQUEST] user_id={user_id}, to_update={is_update}, "
        f"idempotency_key={data.idempotency_key[:16]}..."
    )

    # plan_tier and plan_duration are used for both create and update
    plan_tier_raw = (data.plan_tier or "").strip().upper()
    plan_duration_raw = (data.plan_duration or "").strip().upper()
    country_val = (data.country or "").strip() if data.country else ""

    # Validate idempotency_key
    if not (data.idempotency_key or data.idempotency_key.strip()):
        return JSONResponse(
            status_code=400,
            content={
                "success": False,
                "error": "idempotency_key is required and cannot be empty. Provide a unique key (e.g. UUID) to prevent duplicate charges.",
            },
        )

    if is_update:
        # Change existing plan: require plan_tier, plan_duration
        missing = []
        if not plan_tier_raw:
            missing.append("plan_tier")
        if not plan_duration_raw:
            missing.append("plan_duration")
        if missing:
            logger.warning(
                f"[SUBSCRIBE_UPDATE_MISSING_FIELDS] user_id={user_id}, missing={missing}"
            )
            return JSONResponse(
                status_code=400,
                content={
                    "success": False,
                    "error": f"To change your plan, please provide: {', '.join(missing)}. Valid values: plan_tier = PLUS or PRO, plan_duration = MONTHLY or YEARLY.",
                },
            )
        if plan_tier_raw not in ["PLUS", "PRO"]:
            return JSONResponse(
                status_code=400,
                content={
                    "success": False,
                    "error": f"Invalid plan_tier '{data.plan_tier}'. Allowed values: PLUS, PRO.",
                },
            )
        if plan_duration_raw not in ["MONTHLY", "YEARLY"]:
            return JSONResponse(
                status_code=400,
                content={
                    "success": False,
                    "error": f"Invalid plan_duration '{data.plan_duration}'. Allowed values: MONTHLY, YEARLY.",
                },
            )
    else:
        # New subscription: require plan_tier, plan_duration, country
        missing = []
        if not plan_tier_raw:
            missing.append("plan_tier")
        if not plan_duration_raw:
            missing.append("plan_duration")
        if not country_val:
            missing.append("country")
        if missing:
            logger.warning(
                f"[SUBSCRIBE_CREATE_MISSING_FIELDS] user_id={user_id}, missing={missing}"
            )
            return JSONResponse(
                status_code=400,
                content={
                    "success": False,
                    "error": f"To start a new subscription, please provide: {', '.join(missing)}. Valid values: plan_tier = PLUS or PRO, plan_duration = MONTHLY or YEARLY, country = ISO code (e.g. IN, US).",
                },
            )
        if plan_tier_raw not in ["PLUS", "PRO"]:
            return JSONResponse(
                status_code=400,
                content={
                    "success": False,
                    "error": f"Invalid plan_tier '{data.plan_tier}'. Allowed values: PLUS, PRO.",
                },
            )
        if plan_duration_raw not in ["MONTHLY", "YEARLY"]:
            return JSONResponse(
                status_code=400,
                content={
                    "success": False,
                    "error": f"Invalid plan_duration '{data.plan_duration}'. Allowed values: MONTHLY, YEARLY.",
                },
            )

    try:
        async with async_session_scope() as session:
            try:
                if is_update:
                    result = await WalletService.change_subscription_plan(
                        user_id=user_id,
                        new_plan_tier=plan_tier_raw,
                        new_plan_duration=plan_duration_raw.lower(),
                        idempotency_key=data.idempotency_key,
                        session=session,
                    )
                    result_status_code = result.get("status_code", 500)
                    error_msg = result.get("error", "Internal server error")
                    if not result.get("success"):
                        if result_status_code == 400:
                            logger.warning(
                                f"[SUBSCRIBE_UPDATE_VALIDATION_FAILED] user_id={user_id}, error={error_msg}, "
                                f"elapsed_time={time.time() - start_time:.2f}s"
                            )
                            # Pass through validation errors (same plan, transition not allowed)
                            if "already subscribed" in error_msg.lower() or "cannot change" in error_msg.lower() or "not allowed" in error_msg.lower():
                                user_error = error_msg
                            else:
                                user_error = "We couldn't validate your plan change. Please check plan_tier (PLUS or PRO) and plan_duration (MONTHLY or YEARLY) and try again."
                        elif result_status_code == 404:
                            logger.warning(
                                f"[SUBSCRIBE_UPDATE_NOT_FOUND] user_id={user_id}, error={error_msg}, "
                                f"elapsed_time={time.time() - start_time:.2f}s"
                            )
                            user_error = "You don't have an active subscription. To change plan, subscribe first with to_update=false, then use to_update=true to change plan."
                        else:
                            logger.error(
                                f"[SUBSCRIBE_UPDATE_FAILED] user_id={user_id}, error={error_msg}, "
                                f"elapsed_time={time.time() - start_time:.2f}s"
                            )
                            if "wallet not found" in error_msg.lower():
                                user_error = "Your wallet could not be found. Please contact support."
                            elif "token usage" in error_msg.lower() or "calculate" in error_msg.lower():
                                user_error = "We couldn't compute your usage for the plan change. Please try again in a few minutes or contact support."
                            elif "timeout" in error_msg.lower():
                                user_error = "The payment provider is taking too long to respond. Please try again in a few minutes."
                            elif "payment" in error_msg.lower() or "gateway" in error_msg.lower():
                                user_error = "The payment provider couldn't process the plan change. Please try again or use a different payment method."
                            else:
                                user_error = "We couldn't complete your plan change. Please try again in a moment or contact support if it persists."
                        return JSONResponse(
                            status_code=result_status_code,
                            content={"success": False, "error": user_error},
                        )
                    await session.commit()
                    elapsed_time = time.time() - start_time
                    logger.info(
                        f"[SUBSCRIBE_UPDATE_SUCCESS] user_id={user_id}, "
                        f"plan_tier={plan_tier_raw}, plan_duration={plan_duration_raw}, "
                        f"subscription_id={result.get('subscription_id', 'N/A')}, "
                        f"elapsed_time={elapsed_time:.2f}s"
                    )
                    return SubscribeResponse(**result)
                else:
                    result = await WalletService.create_subscription(
                        user_id=user_id,
                        plan_tier=plan_tier_raw,
                        plan_duration=plan_duration_raw.lower(),
                        country=country_val,
                        idempotency_key=data.idempotency_key,
                        session=session,
                    )
                    result_status_code = result.get("status_code", 500)
                    error_msg = result.get("error", "Internal server error")
                    if not result.get("success"):
                        if result_status_code == 400:
                            logger.warning(
                                f"[SUBSCRIBE_CREATE_VALIDATION_FAILED] user_id={user_id}, error={error_msg}, "
                                f"tier={plan_tier_raw}, duration={plan_duration_raw}, "
                                f"elapsed_time={time.time() - start_time:.2f}s"
                            )
                            if "already" in error_msg.lower() or ("subscription" in error_msg.lower() and "status" in error_msg.lower()):
                                user_error = (
                                    "You already have an active or pending subscription. "
                                    "To change your plan, call this endpoint with to_update=true and your new plan_tier and plan_duration."
                                )
                            else:
                                user_error = "We couldn't start your subscription. Please check plan_tier (PLUS or PRO), plan_duration (MONTHLY or YEARLY), and country (e.g. IN, US), then try again."
                        else:
                            logger.error(
                                f"[SUBSCRIBE_CREATE_FAILED] user_id={user_id}, error={error_msg}, "
                                f"tier={plan_tier_raw}, duration={plan_duration_raw}, "
                                f"elapsed_time={time.time() - start_time:.2f}s"
                            )
                            if "email" in error_msg.lower() and "not found" in error_msg.lower():
                                user_error = "We couldn't find your account email. Please complete your profile and try again."
                            elif "timeout" in error_msg.lower():
                                user_error = "The payment provider is taking too long to respond. Please try again in a few minutes."
                            elif "subscription_id" in error_msg.lower() or "invalid response" in error_msg.lower():
                                user_error = "The payment provider didn't return a valid response. Please try again or contact support."
                            elif "tier" in error_msg.lower() or "configuration" in error_msg.lower():
                                user_error = "This plan or region isn't available right now. Please try a different plan or country, or contact support."
                            else:
                                user_error = "We couldn't start your subscription. Please try again in a moment or contact support if it persists."
                        return JSONResponse(
                            status_code=result_status_code,
                            content={"success": False, "error": user_error},
                        )
                    await session.commit()
                    elapsed_time = time.time() - start_time
                    logger.info(
                        f"[SUBSCRIBE_CREATE_SUCCESS] user_id={user_id}, "
                        f"subscription_id={result.get('subscription_id', 'N/A')}, "
                        f"tier={plan_tier_raw}, duration={plan_duration_raw}, "
                        f"payment_id={result.get('data', {}).get('payment_id', 'N/A')}, "
                        f"elapsed_time={elapsed_time:.2f}s"
                    )
                    return SubscribeResponse(**result)

            except ValueError as e:
                logger.error(
                    f"[SUBSCRIBE_VALUE_ERROR] user_id={user_id}, error={str(e)}, "
                    f"to_update={is_update}, elapsed_time={time.time() - start_time:.2f}s",
                    exc_info=True
                )
                await session.rollback()
                return JSONResponse(
                    status_code=400,
                    content={
                        "success": False,
                        "error": f"Invalid request: {str(e)}. Check plan_tier, plan_duration, country, and idempotency_key."
                    },
                )
            except Exception as e:
                logger.critical(
                    f"[SUBSCRIBE_UNEXPECTED_ERROR] user_id={user_id}, error={str(e)}, "
                    f"error_type={type(e).__name__}, to_update={is_update}, "
                    f"elapsed_time={time.time() - start_time:.2f}s",
                    exc_info=True
                )
                await session.rollback()
                raise

    except HTTPException:
        raise
    except Exception as e:
        logger.critical(
            f"[SUBSCRIBE_CRITICAL_ERROR] user_id={user_id}, error={str(e)}, "
            f"error_type={type(e).__name__}, to_update={is_update}, "
            f"elapsed_time={time.time() - start_time:.2f}s",
            exc_info=True
        )
        return JSONResponse(
            status_code=500,
            content={
                "success": False,
                "error": "An unexpected error occurred while processing your subscription. Our team has been notified. Please contact support if this persists."
            },
        )


@router.post(
    "/subscription/cancel",
    response_model=CancelSubscriptionResponse,
    responses={
        400: {"model": WalletErrorResponse},
        404: {"model": WalletErrorResponse},
        500: {"model": WalletErrorResponse},
    },
    summary="Cancel active subscription",
    description="Cancel the user's active subscription at the end of the current billing cycle. User keeps access until cycle ends."
)
async def cancel_user_subscription(
    data: CancelSubscriptionRequest,
    user_id: str = Depends(get_current_active_user)
):
    """
    Cancel a user's active subscription at the end of the current billing cycle.
    
    Business Logic:
    - User has already paid for the current billing cycle
    - They keep full access and tokens until the cycle ends
    - No refund is issued
    - This only stops the auto-renewal for the next cycle
    
    The subscription will be cancelled at Razorpay at cycle end.
    """
    start_time = time.time()
    logger.info(
        f"[SUBSCRIPTION_CANCEL_REQUEST] user_id={user_id}, "
        f"cancel_at_cycle_end=True (always), "
        f"reason={data.reason[:50] if data.reason else 'None'}..."
    )
    
    try:
        async with async_session_scope() as session:
            try:
                result = await WalletService.cancel_subscription(
                    user_id=user_id,
                    session=session,
                    reason=data.reason
                )
                
                if not result.get("success"):
                    status_code = result.get("status_code", 500)
                    error_msg = result.get("error", "Failed to cancel subscription")
                    
                    logger.error(
                        f"[SUBSCRIPTION_CANCEL_FAILED] user_id={user_id}, "
                        f"error={error_msg}, status_code={status_code}, "
                        f"elapsed_time={time.time() - start_time:.2f}s"
                    )
                    
                    return JSONResponse(
                        status_code=status_code,
                        content={
                            "success": False,
                            "error": error_msg,
                        },
                    )
                
                await session.commit()
                
                elapsed_time = time.time() - start_time
                logger.info(
                    f"[SUBSCRIPTION_CANCEL_SUCCESS] user_id={user_id}, "
                    f"cancel_at_cycle_end=True (always), "
                    f"elapsed_time={elapsed_time:.2f}s"
                )
                
                return CancelSubscriptionResponse(
                    success=True,
                    message=result.get("message", "Subscription cancelled successfully"),
                    data=result.get("data")
                )
                
            except ValueError as e:
                logger.error(
                    f"[SUBSCRIPTION_CANCEL_VALIDATION_ERROR] user_id={user_id}, "
                    f"error={str(e)}, elapsed_time={time.time() - start_time:.2f}s",
                    exc_info=True
                )
                await session.rollback()
                return JSONResponse(
                    status_code=400,
                    content={
                        "success": False,
                        "error": "Invalid cancellation request. Please check your parameters and try again."
                    },
                )
            except Exception as e:
                logger.critical(
                    f"[SUBSCRIPTION_CANCEL_UNEXPECTED_ERROR] user_id={user_id}, "
                    f"error={str(e)}, error_type={type(e).__name__}, "
                    f"elapsed_time={time.time() - start_time:.2f}s",
                    exc_info=True
                )
                await session.rollback()
                raise
    
    except HTTPException:
        raise
    except Exception as e:
        logger.critical(
            f"[SUBSCRIPTION_CANCEL_CRITICAL_ERROR] user_id={user_id}, "
            f"error={str(e)}, error_type={type(e).__name__}, "
            f"elapsed_time={time.time() - start_time:.2f}s",
            exc_info=True
        )
        return JSONResponse(
            status_code=500,
            content={
                "success": False,
                "error": "An unexpected error occurred while cancelling your subscription. Our team has been notified. Please contact support if this persists."
            },
        )


@router.post(
    "/subscription/update-payment-method",
    response_model=UpdatePaymentMethodResponse,
    responses={
        400: {"model": WalletErrorResponse},
        403: {"model": WalletErrorResponse},
        404: {"model": WalletErrorResponse},
        500: {"model": WalletErrorResponse},
    },
    summary="Update subscription payment method",
    description="Cancel current subscription and create a new one with updated payment method at cycle end"
)
async def update_subscription_payment_method(
    request: UpdatePaymentMethodRequest,
    user_id: str = Depends(get_current_active_user)
):
    """
    Update payment method for an active subscription.
    
    Process:
    1. Get current subscription details and current_end date
    2. Cancel existing subscription at cycle end (or immediately based on request)
    3. Create new subscription starting at old subscription's current_end
    4. Return auth_link for user to authorize new payment method
    
    The user maintains access until the current billing cycle ends (if cancel_at_cycle_end=true).
    """
    start_time = time.time()
    logger.info(
        f"[UPDATE_PAYMENT_METHOD_REQUEST] user_id={user_id}, "
        f"subscription_id={request.subscription_id}, "
        f"cancel_at_cycle_end={request.cancel_at_cycle_end}"
    )
    
    try:
        async with async_session_scope() as session:
            result = await WalletService.update_payment_method(
                subscription_id=request.subscription_id,
                user_id=user_id,
                session=session,
                cancel_at_cycle_end=request.cancel_at_cycle_end
            )
            
            if not result.get("success"):
                error_msg = result.get("error", "Failed to update payment method")
                
                # Determine status code and user-friendly message based on error
                if "not found" in error_msg.lower():
                    status_code = 404
                    user_error = "Subscription not found. Please check your subscription ID and try again."
                    logger.warning(
                        f"[UPDATE_PAYMENT_METHOD_NOT_FOUND] user_id={user_id}, "
                        f"subscription_id={request.subscription_id}, "
                        f"elapsed_time={time.time() - start_time:.2f}s"
                    )
                elif "unauthorized" in error_msg.lower() or "does not belong" in error_msg.lower():
                    status_code = 403
                    user_error = "You don't have permission to update this subscription."
                    logger.warning(
                        f"[UPDATE_PAYMENT_METHOD_UNAUTHORIZED] user_id={user_id}, "
                        f"subscription_id={request.subscription_id}, "
                        f"elapsed_time={time.time() - start_time:.2f}s"
                    )
                elif "cannot update" in error_msg.lower() or "status" in error_msg.lower():
                    status_code = 400
                    user_error = error_msg  # Pass through specific status messages
                    logger.warning(
                        f"[UPDATE_PAYMENT_METHOD_INVALID_STATE] user_id={user_id}, "
                        f"subscription_id={request.subscription_id}, error={error_msg}, "
                        f"elapsed_time={time.time() - start_time:.2f}s"
                    )
                elif "configuration" in error_msg.lower() or "internal error" in error_msg.lower():
                    status_code = 500
                    user_error = "A configuration error occurred. Please contact support."
                    logger.error(
                        f"[UPDATE_PAYMENT_METHOD_CONFIG_ERROR] user_id={user_id}, "
                        f"subscription_id={request.subscription_id}, error={error_msg}, "
                        f"elapsed_time={time.time() - start_time:.2f}s"
                    )
                else:
                    status_code = 400
                    user_error = "Unable to update payment method. Please try again or contact support."
                    logger.error(
                        f"[UPDATE_PAYMENT_METHOD_FAILED] user_id={user_id}, "
                        f"subscription_id={request.subscription_id}, error={error_msg}, "
                        f"elapsed_time={time.time() - start_time:.2f}s"
                    )
                
                return JSONResponse(
                    status_code=status_code,
                    content={"success": False, "error": user_error}
                )
            
            elapsed_time = time.time() - start_time
            logger.info(
                f"[UPDATE_PAYMENT_METHOD_SUCCESS] user_id={user_id}, "
                f"old_subscription={request.subscription_id}, "
                f"new_subscription={result['data']['new_subscription_id']}, "
                f"cancel_at_cycle_end={request.cancel_at_cycle_end}, "
                f"elapsed_time={elapsed_time:.2f}s"
            )
            
            return UpdatePaymentMethodResponse(**result)
            
    except Exception as e:
        logger.critical(
            f"[UPDATE_PAYMENT_METHOD_CRITICAL_ERROR] user_id={user_id}, "
            f"subscription_id={request.subscription_id}, error={str(e)}, "
            f"error_type={type(e).__name__}, elapsed_time={time.time() - start_time:.2f}s",
            exc_info=True
        )
        return JSONResponse(
            status_code=500,
            content={
                "success": False,
                "error": "An unexpected error occurred while updating your payment method. Our team has been notified. Please try again later."
            }
        )


@router.post(
    "/subscription/webhook",
    response_model=Dict[str, Any],
    responses={
        400: {"model": WalletErrorResponse},
        401: {"model": WalletErrorResponse},
    },
)
async def subscription_webhook(
    data: SubscriptionWebhookEvent,
    x_api_key: Optional[str] = Header(None, alias="X-API-Key")
):
    """Handle subscription status webhooks from payment service."""
    logger.info(
        f"[SUBSCRIPTION_WEBHOOK_RECEIVED] event={data.event}, "
        f"subscription_id={data.subscription_id}, "
        f"gateway={getattr(data, 'gateway', 'N/A')}, "
        f"status={getattr(data, 'status', 'N/A')}, "
        f"plan_tier={getattr(data, 'plan_tier', 'N/A')}, "
        f"plan_duration={getattr(data, 'plan_duration', 'N/A')}"
    )
    
    # Verify API key for webhook security
    if not verify_webhook_api_key(x_api_key):
        logger.error(
            f"[SUBSCRIPTION_WEBHOOK_UNAUTHORIZED] subscription_id={data.subscription_id}, "
            f"event={data.event}, reason=Invalid or missing API key"
        )
        return JSONResponse(
            status_code=401,
            content={
                "success": False,
                "error": "Unauthorized: Invalid or missing API key"
            }
        )
    
    # Log full webhook payload for audit (be careful with sensitive data)
    logger.debug(f"[SUBSCRIPTION_WEBHOOK_PAYLOAD] subscription_id={data.subscription_id}, data={data.dict()}")
    
    try:
        async with async_session_scope() as session:
            try:
                result = await WalletService.handle_subscription_webhook(data, session)
                
                if not result.get("success"):
                    error_msg = result.get('error', 'Unknown error')
                    logger.error(
                        f"[SUBSCRIPTION_WEBHOOK_FAILED] event={data.event}, "
                        f"subscription_id={data.subscription_id}, error={error_msg}"
                    )
                    
                    # Return 200 to acknowledge receipt (prevent retries for known errors)
                    # Return 400 for invalid data (will be retried by payment service)
                    # This prevents webhook storms
                    if "not found" in error_msg.lower() or "invalid" in error_msg.lower():
                        status_code = 400
                    else:
                        status_code = 200  # Acknowledge but log error
                    
                    return JSONResponse(
                        status_code=status_code,
                        content=result
                    )
                
                await session.commit()
                
                logger.info(
                    f"[SUBSCRIPTION_WEBHOOK_SUCCESS] event={data.event}, "
                    f"subscription_id={data.subscription_id}"
                )
                
                return {"success": True, "message": "Webhook processed successfully"}
                
            except ValueError as e:
                logger.error(
                    f"[SUBSCRIPTION_WEBHOOK_VALIDATION_ERROR] event={data.event}, "
                    f"subscription_id={data.subscription_id}, error={str(e)}", 
                    exc_info=True
                )
                await session.rollback()
                return JSONResponse(
                    status_code=400,
                    content={"success": False, "error": "Invalid webhook data"},
                )
            except Exception as e:
                logger.critical(
                    f"[SUBSCRIPTION_WEBHOOK_UNEXPECTED_ERROR] event={data.event}, "
                    f"subscription_id={data.subscription_id}, error={str(e)}", 
                    exc_info=True
                )
                await session.rollback()
                raise

    except HTTPException:
        raise
    except Exception as e:
        logger.critical(
            f"[SUBSCRIPTION_WEBHOOK_CRITICAL_ERROR] event={data.event}, "
            f"subscription_id={data.subscription_id}, error={str(e)}", 
            exc_info=True
        )
        # Return 500 to trigger retry from payment service
        return JSONResponse(
            status_code=500,
            content={"success": False, "error": "Internal server error"},
        )


@router.post(
    "/topup/initiate",
    response_model=TopupInitiateResponse,
    responses={
        400: {"model": WalletErrorResponse},
        401: {"model": WalletErrorResponse},
    },
)
async def initiate_topup(
    data: TopupInitiateRequest, user_id: str = Depends(get_current_active_user)
):
    """Initiate a token top-up purchase."""
    start_time = time.time()
    logger.info(
        f"[TOPUP_INITIATE] user_id={user_id}, amount={data.amount}, "
        f"country={data.country_code}, idempotency_key={data.idempotency_key[:16]}..."
    )
    
    # Validate amount
    if data.amount <= 0:
        logger.warning(f"[TOPUP_INVALID_AMOUNT] user_id={user_id}, amount={data.amount}")
        return JSONResponse(
            status_code=400,
            content={"success": False, "error": "Amount must be greater than 0"},
        )
    

    try:
        async with async_session_scope() as session:
            try:
                result = await WalletService.create_topup_order(
                    user_id=user_id,
                    amount=data.amount,
                    country_code=data.country_code,
                    idempotency_key=data.idempotency_key,
                    session=session,
                )
                
                if not result.get("success"):
                    error_msg = result.get("error", "Failed to initiate topup")
                    logger.error(f"[TOPUP_INITIATE_FAILED] user_id={user_id}, error={error_msg}")
                    
                    # Determine appropriate status code
                    if "already exists" in error_msg.lower() or "duplicate" in error_msg.lower():
                        status_code = 409  # Conflict
                    elif "wallet not found" in error_msg.lower():
                        status_code = 404
                    else:
                        status_code = 400
                    
                    return JSONResponse(
                        status_code=status_code,
                        content={
                            "success": False,
                            "error": error_msg,
                        },
                    )
                
                await session.commit()
                
                elapsed_time = time.time() - start_time
                logger.info(
                    f"[TOPUP_INITIATE_SUCCESS] user_id={user_id}, "
                    f"payment_id={result.get('data', {}).get('payment_id', 'N/A')}, "
                    f"elapsed_time={elapsed_time:.2f}s"
                )

                return TopupInitiateResponse(
                    success=True,
                    data=result.get("data")
                )
                
            except ValueError as e:
                logger.error(f"[TOPUP_VALIDATION_ERROR] user_id={user_id}, error={str(e)}", exc_info=True)
                await session.rollback()
                return JSONResponse(
                    status_code=400,
                    content={"success": False, "error": "Invalid topup parameters"},
                )
            except Exception as e:
                logger.critical(f"[TOPUP_UNEXPECTED_ERROR] user_id={user_id}, error={str(e)}", exc_info=True)
                await session.rollback()
                raise

    except HTTPException:
        raise
    except Exception as e:
        logger.critical(f"[TOPUP_CRITICAL_ERROR] user_id={user_id}, error={str(e)}", exc_info=True)
        return JSONResponse(
            status_code=500,
            content={"success": False, "error": "Unable to process topup. Please try again later."},
        )


@router.post(
    "/payment-webhook",
    response_model=PaymentWebhookResponse,
    responses={
        400: {"model": WalletErrorResponse},
        401: {"model": WalletErrorResponse},
        500: {"model": WalletErrorResponse},
    },
)
async def payment_webhook_handler(
    data: PaymentWebhookRequest,
    x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
):
    """Credit tokens to a user's wallet from a completed payment (called by payment service)."""
    start_time = time.time()
    logger.info(
        f"[PAYMENT_WEBHOOK_RECEIVED] payment_id={data.payment_id}, status={data.status}, "
        f"amount={data.amount}, currency={data.currency}, payment_method={data.payment_method}, "
        f"has_api_key={x_api_key is not None}"
    )
    
    # Verify API key for webhook security
    if not verify_webhook_api_key(x_api_key):
        logger.error(
            f"[PAYMENT_WEBHOOK_UNAUTHORIZED] payment_id={data.payment_id}, "
            f"status={data.status}, reason=Invalid or missing API key, "
            f"elapsed_time={time.time() - start_time:.2f}s"
        )
        return JSONResponse(
            status_code=401,
            content={
                "success": False,
                "error": "Unauthorized: Invalid or missing API key"
            }
        )
    
    # Validate payment status
    valid_statuses = ["COMPLETED", "FAILED", "CANCELLED", "PENDING"]
    if data.status not in valid_statuses:
        logger.warning(
            f"[PAYMENT_WEBHOOK_INVALID_STATUS] payment_id={data.payment_id}, "
            f"status={data.status}, valid_statuses={valid_statuses}, "
            f"elapsed_time={time.time() - start_time:.2f}s"
        )
        return JSONResponse(
            status_code=400,
            content={
                "success": False,
                "error": f"Invalid payment status '{data.status}'. Must be one of: {', '.join(valid_statuses)}"
            }
        )
    
    try:
        async with async_session_scope() as session:
            try:
                result = await WalletService.payment_webhook_handler(
                    payment_id=data.payment_id, 
                    session=session, 
                    status=data.status,
                    amount=data.amount,
                    currency=data.currency,
                    payment_method=data.payment_method,
                    payment_method_details=data.payment_method_details
                )
                
                if not result.get("success"):
                    error_msg = result.get("error", "Failed to credit tokens")
                    
                    # Determine appropriate status code
                    if "not found" in error_msg.lower():
                        status_code = 404
                        logger.error(
                            f"[PAYMENT_WEBHOOK_NOT_FOUND] payment_id={data.payment_id}, "
                            f"status={data.status}, error={error_msg}, "
                            f"elapsed_time={time.time() - start_time:.2f}s"
                        )
                    elif "already processed" in error_msg.lower() or "duplicate" in error_msg.lower():
                        status_code = 200  # Acknowledge idempotent request
                        logger.info(
                            f"[PAYMENT_WEBHOOK_DUPLICATE] payment_id={data.payment_id}, "
                            f"status={data.status}, idempotent_request=true, "
                            f"elapsed_time={time.time() - start_time:.2f}s"
                        )
                    else:
                        status_code = 500
                        logger.error(
                            f"[PAYMENT_WEBHOOK_FAILED] payment_id={data.payment_id}, "
                            f"status={data.status}, error={error_msg}, "
                            f"elapsed_time={time.time() - start_time:.2f}s"
                        )
                    
                    return JSONResponse(
                        status_code=status_code,
                        content={
                            "success": False,
                            "error": error_msg,
                        },
                    )
                
                await session.commit()
                
                elapsed_time = time.time() - start_time
                logger.info(
                    f"[PAYMENT_WEBHOOK_SUCCESS] payment_id={data.payment_id}, "
                    f"status={data.status}, tokens_credited={result.get('tokens_credited', 0)}, "
                    f"balance_after={result.get('balance_after', 0)}, "
                    f"batch_id={result.get('batch_id', 'N/A')}, "
                    f"idempotent={result.get('idempotent', False)}, "
                    f"amount={data.amount}, currency={data.currency}, "
                    f"payment_method={data.payment_method}, elapsed_time={elapsed_time:.2f}s"
                )
                
                return PaymentWebhookResponse(
                    success=True,
                    tokens_credited=result.get("tokens_credited"),
                    batch_id=result.get("batch_id"),
                    balance_after=result.get("balance_after"),
                    message=result.get("message"),
                    idempotent=result.get("idempotent", False),
                )
                
            except ValueError as e:
                logger.error(
                    f"[PAYMENT_WEBHOOK_VALIDATION_ERROR] payment_id={data.payment_id}, "
                    f"status={data.status}, error={str(e)}, "
                    f"elapsed_time={time.time() - start_time:.2f}s", 
                    exc_info=True
                )
                await session.rollback()
                return JSONResponse(
                    status_code=400,
                    content={
                        "success": False,
                        "error": "Invalid payment data format. Please check the webhook payload."
                    },
                )
            except Exception as e:
                logger.critical(
                    f"[PAYMENT_WEBHOOK_UNEXPECTED_ERROR] payment_id={data.payment_id}, "
                    f"status={data.status}, error={str(e)}, error_type={type(e).__name__}, "
                    f"elapsed_time={time.time() - start_time:.2f}s", 
                    exc_info=True
                )
                await session.rollback()
                raise
                
    except HTTPException:
        raise
    except Exception as e:
        logger.critical(
            f"[PAYMENT_WEBHOOK_CRITICAL_ERROR] payment_id={data.payment_id}, "
            f"status={data.status}, error={str(e)}, error_type={type(e).__name__}, "
            f"elapsed_time={time.time() - start_time:.2f}s", 
            exc_info=True
        )
        # Return 500 to trigger retry from payment service
        return JSONResponse(
            status_code=500,
            content={
                "success": False,
                "error": "Internal server error processing payment webhook. Will retry."
            },
        )


@router.get(
    "/payment-status/{payment_id}",
    response_model=PaymentStatusResponse,
    responses={
        400: {"model": WalletErrorResponse},
        401: {"model": WalletErrorResponse},
        404: {"model": WalletErrorResponse},
        500: {"model": WalletErrorResponse},
    },
)
async def get_payment_status(
    payment_id: str, user_id: str = Depends(get_current_active_user)
):
    """
    Get the status of a payment/token batch by payment_id.
    Used for polling from frontend to check if tokens have been credited.
    SECURITY: Only returns payment info if it belongs to the requesting user.
    """
    start_time = time.time()
    
    # Input validation: payment_id should not be empty
    if not payment_id or len(payment_id) < 3:
        logger.warning(
            f"[GET_PAYMENT_STATUS_INVALID_ID] payment_id='{payment_id}', user_id={user_id}, "
            f"reason=Empty or too short"
        )
        return JSONResponse(
            status_code=400,
            content={
                "success": False,
                "error": "Invalid payment ID. Payment ID must be at least 3 characters."
            },
        )
    
    logger.info(f"[GET_PAYMENT_STATUS_REQUEST] payment_id={payment_id}, user_id={user_id}")
    
    try:
        async with async_session_scope() as session:
            try:
                result = await WalletService.get_payment_status(
                    payment_id=payment_id, 
                    user_id=user_id,
                    session=session
                )

                if not result.get("success"):
                    error_msg = result.get("error", "Failed to get payment status")
                    
                    if "not found" in error_msg.lower():
                        status_code = 404
                        user_error = "Payment not found. Please check the payment ID and try again."
                        logger.warning(
                            f"[GET_PAYMENT_STATUS_NOT_FOUND] payment_id={payment_id}, "
                            f"user_id={user_id}, elapsed_time={time.time() - start_time:.2f}s"
                        )
                    else:
                        status_code = 500
                        user_error = "Unable to retrieve payment status. Please try again in a moment."
                        logger.error(
                            f"[GET_PAYMENT_STATUS_FAILED] payment_id={payment_id}, "
                            f"user_id={user_id}, error={error_msg}, "
                            f"elapsed_time={time.time() - start_time:.2f}s"
                        )
                    
                    return JSONResponse(
                        status_code=status_code,
                        content={
                            "success": False,
                            "error": user_error,
                        },
                    )

                elapsed_time = time.time() - start_time
                logger.info(
                    f"[GET_PAYMENT_STATUS_SUCCESS] payment_id={payment_id}, "
                    f"user_id={user_id}, status={result.get('status')}, "
                    f"tokens={result.get('tokens')}, amount={result.get('amount')}, "
                    f"elapsed_time={elapsed_time:.2f}s"
                )

                return PaymentStatusResponse(
                    success=True,
                    payment_id=result.get("payment_id"),
                    status=result.get("status"),
                    tokens=result.get("tokens"),
                    amount=result.get("amount"),
                    currency=result.get("currency"),
                    source_type=result.get("source_type"),
                    created_at=result.get("created_at"),
                    expires_at=result.get("expires_at"),
                )
            except Exception as e:
                logger.critical(
                    f"[GET_PAYMENT_STATUS_UNEXPECTED_ERROR] payment_id={payment_id}, "
                    f"user_id={user_id}, error={str(e)}, error_type={type(e).__name__}, "
                    f"elapsed_time={time.time() - start_time:.2f}s", 
                    exc_info=True
                )
                await session.rollback()
                raise
                
    except HTTPException:
        raise
    except Exception as e:
        logger.critical(
            f"[GET_PAYMENT_STATUS_CRITICAL_ERROR] payment_id={payment_id}, "
            f"user_id={user_id}, error={str(e)}, error_type={type(e).__name__}, "
            f"elapsed_time={time.time() - start_time:.2f}s", 
            exc_info=True
        )
        return JSONResponse(
            status_code=500,
            content={
                "success": False,
                "error": "An unexpected error occurred. Our team has been notified. Please try again later."
            },
        )

@router.post(
    "/calculate-tax",
    response_model=TaxCalculationResponse,
    responses={
        400: {"model": WalletErrorResponse},
        401: {"model": WalletErrorResponse},
        500: {"model": WalletErrorResponse},
    },
)
async def calculate_tax(
    data: TaxCalculationRequest, user_id: str = Depends(get_current_active_user)
):
    """
    Calculate tax for a top-up or subscription based on country code.
    """
    start_time = time.time()
    logger.info(
        f"[TAX_CALCULATE_REQUEST] user_id={user_id}, country={data.country_code}, "
        f"amount_usd={data.amount_in_usd}"
    )
    
    # Validate amount
    if data.amount_in_usd <= 0:
        logger.warning(
            f"[TAX_CALCULATE_INVALID_AMOUNT] user_id={user_id}, amount={data.amount_in_usd}"
        )
        return JSONResponse(
            status_code=400,
            content={
                "success": False,
                "error": "Amount must be greater than 0"
            },
        )
    
    try:
        # Resolve currency based on country
        country_norm = data.country_code.lower() if data.country_code else "default"
        currency = COUNTRY_TO_CURRENCY.get(country_norm, COUNTRY_TO_CURRENCY["default"])
        
        result = WalletService.calculate_tax(data.country_code, data.amount_in_usd, currency)
        
        elapsed_time = time.time() - start_time
        logger.info(
            f"[TAX_CALCULATE_SUCCESS] user_id={user_id}, country={data.country_code}, "
            f"amount_usd={data.amount_in_usd}, currency={currency}, "
            f"total_amount={result.get('total_amount_with_tax')}, "
            f"elapsed_time={elapsed_time:.2f}s"
        )
        
        return TaxCalculationResponse(**result)
    except Exception as e:
        logger.critical(
            f"[TAX_CALCULATE_ERROR] user_id={user_id}, country={data.country_code}, "
            f"amount={data.amount_in_usd}, error={str(e)}, error_type={type(e).__name__}, "
            f"elapsed_time={time.time() - start_time:.2f}s",
            exc_info=True
        )
        return JSONResponse(
            status_code=500,
            content={
                "success": False,
                "error": "Unable to calculate tax. Please try again later."
            },
        )


@router.post(
    "/subscription/calculate-tax",
    response_model=TaxCalculationResponse,
    responses={
        400: {"model": WalletErrorResponse},
        401: {"model": WalletErrorResponse},
        500: {"model": WalletErrorResponse},
    },
)
async def calculate_subscription_tax(
    data: SubscriptionTaxCalculationRequest,
    user_id: str = Depends(get_current_active_user),
):
    """
    Calculate tax for a specific subscription plan and type based on country code.
    """
    start_time = time.time()
    logger.info(
        f"[SUBSCRIPTION_TAX_CALCULATE_REQUEST] user_id={user_id}, country={data.country_code}, "
        f"plan={data.plan_name}, type={data.plan_type}"
    )
    
    try:
        result = WalletService.calculate_subscription_tax(
            data.country_code, data.plan_name, data.plan_type
        )
        
        if not result.get("success"):
            error_msg = result.get("error", "Failed to calculate tax")
            logger.warning(
                f"[SUBSCRIPTION_TAX_CALCULATE_FAILED] user_id={user_id}, country={data.country_code}, "
                f"plan={data.plan_name}, type={data.plan_type}, error={error_msg}, "
                f"elapsed_time={time.time() - start_time:.2f}s"
            )
            return JSONResponse(
                status_code=400,
                content={
                    "success": False,
                    "error": f"Unable to calculate tax: {error_msg}"
                },
            )
        
        elapsed_time = time.time() - start_time
        logger.info(
            f"[SUBSCRIPTION_TAX_CALCULATE_SUCCESS] user_id={user_id}, country={data.country_code}, "
            f"plan={data.plan_name}, type={data.plan_type}, "
            f"total_amount={result.get('total_amount_with_tax')}, "
            f"elapsed_time={elapsed_time:.2f}s"
        )
        
        return TaxCalculationResponse(**result)
    except Exception as e:
        logger.critical(
            f"[SUBSCRIPTION_TAX_CALCULATE_ERROR] user_id={user_id}, country={data.country_code}, "
            f"plan={data.plan_name}, type={data.plan_type}, error={str(e)}, "
            f"error_type={type(e).__name__}, elapsed_time={time.time() - start_time:.2f}s",
            exc_info=True
        )
        return JSONResponse(
            status_code=500,
            content={
                "success": False,
                "error": "Unable to calculate subscription tax. Please try again later."
            },
        )


@router.get(
    "/referral",
    response_model=ReferralInfoResponse,
    responses={
        401: {"model": WalletErrorResponse},
        404: {"model": ReferralErrorResponse},
        500: {"model": ReferralErrorResponse},
    },
    summary="Get referral information",
    description="Get user's referral code and statistics including total referrals and tokens earned"
)
async def get_referral_info(
    user_id: str = Depends(get_current_active_user)
):
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
            from src.db.referral_functions import get_referral_stats, get_user_referral_code
            from src.db.async_db_functions import get_user_details
            
            # Get user details to verify user exists
            user_details = await get_user_details(user_id, session)
            if not user_details.get("success"):
                logger.error(f"[REFERRAL_INFO_USER_NOT_FOUND] user_id={user_id}")
                return JSONResponse(
                    status_code=404,
                    content={
                        "success": False,
                        "error": "User not found"
                    }
                )
            
            # Get user's referral code
            referral_code = await get_user_referral_code(user_id, session)
            if not referral_code:
                logger.error(f"[REFERRAL_INFO_CODE_NOT_FOUND] user_id={user_id}")
                return JSONResponse(
                    status_code=404,
                    content={
                        "success": False,
                        "error": "Referral code not found for user"
                    }
                )
            
            # Get referral stats
            stats = await get_referral_stats(user_id, session)
            
            if not stats.get("success"):
                logger.error(
                    f"[REFERRAL_INFO_STATS_FAILED] user_id={user_id}, "
                    f"error={stats.get('error')}"
                )
                return JSONResponse(
                    status_code=500,
                    content={
                        "success": False,
                        "error": "Failed to retrieve referral statistics"
                    }
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
                total_referrals=stats['total_referrals'],
                total_tokens_earned=stats['total_tokens_earned'],
                referral_link=referral_link
            )
            
    except HTTPException:
        raise
    except Exception as e:
        logger.critical(
            f"[REFERRAL_INFO_CRITICAL_ERROR] user_id={user_id}, "
            f"error={str(e)}, error_type={type(e).__name__}, "
            f"elapsed_time={time.time() - start_time:.2f}s",
            exc_info=True
        )
        return JSONResponse(
            status_code=500,
            content={
                "success": False,
                "error": "An unexpected error occurred while retrieving referral information. Please try again later."
            }
        )
