"""HTTP routes for the billing bounded context.

Handlers moved verbatim from ``src/resources/routers/wallet_api.py``
during the R-STRUCT-1 migration, which split that module across wallet,
billing, and referrals.
"""

"""
wallet_api.py: REST API endpoints for wallet operations.
"""

import time
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from fastapi.responses import JSONResponse

from app.auth.token import get_current_active_user
from app.core.db import async_session_scope
from app.core.logging import setup_logging
from app.wallet.repository import verify_webhook_api_key
from app.wallet.schemas import (
    AllTiersResponse,
    CancelSubscriptionRequest,
    CancelSubscriptionResponse,
    SubscribeRequest,
    SubscribeResponse,
    SubscriptionResponse,
    SubscriptionTaxCalculationRequest,
    SubscriptionWebhookEvent,
    TaxCalculationResponse,
    UpdatePaymentMethodRequest,
    UpdatePaymentMethodResponse,
    WalletErrorResponse,
)
from app.wallet.service import WalletService

# The /wallet prefix is preserved deliberately. Billing was split out of
# wallet_api.py, but its paths are /wallet/subscription, /wallet/tiers and so
# on — changing the prefix to /billing would rename live endpoints, which this
# structural migration is not permitted to do. Renaming them is a client-facing
# API change and belongs to its own versioned deprecation.
router = APIRouter(prefix="/wallet", tags=["Billing"])
logger = setup_logging(__file__)


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
            subscription_tier = (
                result.get("subscription", {}).get("current_tier", "FREE")
                if result.get("subscription")
                else "FREE"
            )
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
            f"error={e!s}, error_type={type(e).__name__}, "
            f"elapsed_time={time.time() - start_time:.2f}s",
            exc_info=True,
        )
        return JSONResponse(
            status_code=500,
            content={
                "success": False,
                "error": "An unexpected error occurred. Our team has been notified. Please try again later.",
            },
        )


@router.get("/tiers", response_model=AllTiersResponse)
async def get_all_tiers(country_code: str | None = Query("default")):
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
            f"[TIERS_ERROR] country_code={country_code}, error={e!s}, "
            f"error_type={type(e).__name__}, elapsed_time={time.time() - start_time:.2f}s",
            exc_info=True,
        )
        return JSONResponse(
            status_code=500,
            content={
                "success": False,
                "error": "Unable to retrieve subscription tiers. Please try again later.",
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
async def subscribe(data: SubscribeRequest, user_id: str = Depends(get_current_active_user)):
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
                            if (
                                "already subscribed" in error_msg.lower()
                                or "cannot change" in error_msg.lower()
                                or "not allowed" in error_msg.lower()
                            ):
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
                                user_error = (
                                    "Your wallet could not be found. Please contact support."
                                )
                            elif (
                                "token usage" in error_msg.lower()
                                or "calculate" in error_msg.lower()
                            ):
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
                            if "already" in error_msg.lower() or (
                                "subscription" in error_msg.lower()
                                and "status" in error_msg.lower()
                            ):
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
                            elif (
                                "subscription_id" in error_msg.lower()
                                or "invalid response" in error_msg.lower()
                            ):
                                user_error = "The payment provider didn't return a valid response. Please try again or contact support."
                            elif (
                                "tier" in error_msg.lower() or "configuration" in error_msg.lower()
                            ):
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
                    f"[SUBSCRIBE_VALUE_ERROR] user_id={user_id}, error={e!s}, "
                    f"to_update={is_update}, elapsed_time={time.time() - start_time:.2f}s",
                    exc_info=True,
                )
                await session.rollback()
                return JSONResponse(
                    status_code=400,
                    content={
                        "success": False,
                        "error": f"Invalid request: {e!s}. Check plan_tier, plan_duration, country, and idempotency_key.",
                    },
                )
            except Exception as e:
                logger.critical(
                    f"[SUBSCRIBE_UNEXPECTED_ERROR] user_id={user_id}, error={e!s}, "
                    f"error_type={type(e).__name__}, to_update={is_update}, "
                    f"elapsed_time={time.time() - start_time:.2f}s",
                    exc_info=True,
                )
                await session.rollback()
                raise

    except HTTPException:
        raise
    except Exception as e:
        logger.critical(
            f"[SUBSCRIBE_CRITICAL_ERROR] user_id={user_id}, error={e!s}, "
            f"error_type={type(e).__name__}, to_update={is_update}, "
            f"elapsed_time={time.time() - start_time:.2f}s",
            exc_info=True,
        )
        return JSONResponse(
            status_code=500,
            content={
                "success": False,
                "error": "An unexpected error occurred while processing your subscription. Our team has been notified. Please contact support if this persists.",
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
    description="Cancel the user's active subscription at the end of the current billing cycle. User keeps access until cycle ends.",
)
async def cancel_user_subscription(
    data: CancelSubscriptionRequest, user_id: str = Depends(get_current_active_user)
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
                    user_id=user_id, session=session, reason=data.reason
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
                    data=result.get("data"),
                )

            except ValueError as e:
                logger.error(
                    f"[SUBSCRIPTION_CANCEL_VALIDATION_ERROR] user_id={user_id}, "
                    f"error={e!s}, elapsed_time={time.time() - start_time:.2f}s",
                    exc_info=True,
                )
                await session.rollback()
                return JSONResponse(
                    status_code=400,
                    content={
                        "success": False,
                        "error": "Invalid cancellation request. Please check your parameters and try again.",
                    },
                )
            except Exception as e:
                logger.critical(
                    f"[SUBSCRIPTION_CANCEL_UNEXPECTED_ERROR] user_id={user_id}, "
                    f"error={e!s}, error_type={type(e).__name__}, "
                    f"elapsed_time={time.time() - start_time:.2f}s",
                    exc_info=True,
                )
                await session.rollback()
                raise

    except HTTPException:
        raise
    except Exception as e:
        logger.critical(
            f"[SUBSCRIPTION_CANCEL_CRITICAL_ERROR] user_id={user_id}, "
            f"error={e!s}, error_type={type(e).__name__}, "
            f"elapsed_time={time.time() - start_time:.2f}s",
            exc_info=True,
        )
        return JSONResponse(
            status_code=500,
            content={
                "success": False,
                "error": "An unexpected error occurred while cancelling your subscription. Our team has been notified. Please contact support if this persists.",
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
    description="Cancel current subscription and create a new one with updated payment method at cycle end",
)
async def update_subscription_payment_method(
    request: UpdatePaymentMethodRequest, user_id: str = Depends(get_current_active_user)
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
                cancel_at_cycle_end=request.cancel_at_cycle_end,
            )

            if not result.get("success"):
                error_msg = result.get("error", "Failed to update payment method")

                # Determine status code and user-friendly message based on error
                if "not found" in error_msg.lower():
                    status_code = 404
                    user_error = (
                        "Subscription not found. Please check your subscription ID and try again."
                    )
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
                    user_error = (
                        "Unable to update payment method. Please try again or contact support."
                    )
                    logger.error(
                        f"[UPDATE_PAYMENT_METHOD_FAILED] user_id={user_id}, "
                        f"subscription_id={request.subscription_id}, error={error_msg}, "
                        f"elapsed_time={time.time() - start_time:.2f}s"
                    )

                return JSONResponse(
                    status_code=status_code, content={"success": False, "error": user_error}
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
            f"subscription_id={request.subscription_id}, error={e!s}, "
            f"error_type={type(e).__name__}, elapsed_time={time.time() - start_time:.2f}s",
            exc_info=True,
        )
        return JSONResponse(
            status_code=500,
            content={
                "success": False,
                "error": "An unexpected error occurred while updating your payment method. Our team has been notified. Please try again later.",
            },
        )


@router.post(
    "/subscription/webhook",
    response_model=dict[str, Any],
    responses={
        400: {"model": WalletErrorResponse},
        401: {"model": WalletErrorResponse},
    },
)
async def subscription_webhook(
    data: SubscriptionWebhookEvent, x_api_key: str | None = Header(None, alias="X-API-Key")
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
            content={"success": False, "error": "Unauthorized: Invalid or missing API key"},
        )

    # Log full webhook payload for audit (be careful with sensitive data)
    logger.debug(
        f"[SUBSCRIPTION_WEBHOOK_PAYLOAD] subscription_id={data.subscription_id}, data={data.dict()}"
    )

    try:
        async with async_session_scope() as session:
            try:
                result = await WalletService.handle_subscription_webhook(data, session)

                if not result.get("success"):
                    error_msg = result.get("error", "Unknown error")
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

                    return JSONResponse(status_code=status_code, content=result)

                await session.commit()

                logger.info(
                    f"[SUBSCRIPTION_WEBHOOK_SUCCESS] event={data.event}, "
                    f"subscription_id={data.subscription_id}"
                )

                return {"success": True, "message": "Webhook processed successfully"}

            except ValueError as e:
                logger.error(
                    f"[SUBSCRIPTION_WEBHOOK_VALIDATION_ERROR] event={data.event}, "
                    f"subscription_id={data.subscription_id}, error={e!s}",
                    exc_info=True,
                )
                await session.rollback()
                return JSONResponse(
                    status_code=400,
                    content={"success": False, "error": "Invalid webhook data"},
                )
            except Exception as e:
                logger.critical(
                    f"[SUBSCRIPTION_WEBHOOK_UNEXPECTED_ERROR] event={data.event}, "
                    f"subscription_id={data.subscription_id}, error={e!s}",
                    exc_info=True,
                )
                await session.rollback()
                raise

    except HTTPException:
        raise
    except Exception as e:
        logger.critical(
            f"[SUBSCRIPTION_WEBHOOK_CRITICAL_ERROR] event={data.event}, "
            f"subscription_id={data.subscription_id}, error={e!s}",
            exc_info=True,
        )
        # Return 500 to trigger retry from payment service
        return JSONResponse(
            status_code=500,
            content={"success": False, "error": "Internal server error"},
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
                content={"success": False, "error": f"Unable to calculate tax: {error_msg}"},
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
            f"plan={data.plan_name}, type={data.plan_type}, error={e!s}, "
            f"error_type={type(e).__name__}, elapsed_time={time.time() - start_time:.2f}s",
            exc_info=True,
        )
        return JSONResponse(
            status_code=500,
            content={
                "success": False,
                "error": "Unable to calculate subscription tax. Please try again later.",
            },
        )
