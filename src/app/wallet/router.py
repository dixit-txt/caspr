"""HTTP routes for the wallet bounded context.

Handlers moved verbatim from ``src/resources/routers/wallet_api.py``
during the R-STRUCT-1 migration, which split that module across wallet,
billing, and referrals.
"""

"""
wallet_api.py: REST API endpoints for wallet operations.
"""

import time

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from fastapi.responses import JSONResponse

from app.auth.token import get_current_active_user
from app.core.constants import COUNTRY_TO_CURRENCY
from app.core.db import async_session_scope
from app.core.logging import setup_logging
from app.wallet.repository import verify_webhook_api_key
from app.wallet.schemas import (
    PaymentStatusResponse,
    PaymentWebhookRequest,
    PaymentWebhookResponse,
    TaxCalculationRequest,
    TaxCalculationResponse,
    TopupInitiateRequest,
    TopupInitiateResponse,
    TransactionHistoryResponse,
    WalletBalanceResponse,
    WalletErrorResponse,
)
from app.wallet.service import WalletService

# Prefix carried over verbatim from the pre-migration wallet_api.py.
router = APIRouter(prefix="/wallet", tags=["Wallet"])
logger = setup_logging(__file__)


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
                    f"[BALANCE_VALIDATION_ERROR] user_id={user_id}, error={e!s}, "
                    f"elapsed_time={time.time() - start_time:.2f}s",
                    exc_info=True,
                )
                return JSONResponse(
                    status_code=400,
                    content={
                        "success": False,
                        "error": "Invalid request format. Please check your request and try again.",
                    },
                )
            except Exception as e:
                logger.critical(
                    f"[BALANCE_UNEXPECTED_ERROR] user_id={user_id}, error={e!s}, "
                    f"error_type={type(e).__name__}, elapsed_time={time.time() - start_time:.2f}s",
                    exc_info=True,
                )
                await session.rollback()
                raise

    except HTTPException:
        raise
    except Exception as e:
        logger.critical(
            f"[BALANCE_CRITICAL_ERROR] user_id={user_id}, error={e!s}, "
            f"error_type={type(e).__name__}, elapsed_time={time.time() - start_time:.2f}s",
            exc_info=True,
        )
        return JSONResponse(
            status_code=500,
            content={
                "success": False,
                "error": "An unexpected error occurred. Our team has been notified. Please try again later.",
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
    transaction_type: str | None = Query(None),
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
                "error": f"Invalid transaction type '{transaction_type}'. Must be one of: credit, debit, reserve, release",
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
                    f"[TRANSACTIONS_VALIDATION_ERROR] user_id={user_id}, error={e!s}, "
                    f"limit={limit}, offset={offset}, type={transaction_type}, "
                    f"elapsed_time={time.time() - start_time:.2f}s",
                    exc_info=True,
                )
                return JSONResponse(
                    status_code=400,
                    content={
                        "success": False,
                        "error": "Invalid request parameters. Please check limit, offset, and transaction type.",
                    },
                )
            except Exception as e:
                logger.critical(
                    f"[TRANSACTIONS_UNEXPECTED_ERROR] user_id={user_id}, error={e!s}, "
                    f"error_type={type(e).__name__}, elapsed_time={time.time() - start_time:.2f}s",
                    exc_info=True,
                )
                await session.rollback()
                raise

    except HTTPException:
        raise
    except Exception as e:
        logger.critical(
            f"[TRANSACTIONS_CRITICAL_ERROR] user_id={user_id}, error={e!s}, "
            f"error_type={type(e).__name__}, elapsed_time={time.time() - start_time:.2f}s",
            exc_info=True,
        )
        return JSONResponse(
            status_code=500,
            content={
                "success": False,
                "error": "An unexpected error occurred. Our team has been notified. Please try again later.",
            },
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

                return TopupInitiateResponse(success=True, data=result.get("data"))

            except ValueError as e:
                logger.error(
                    f"[TOPUP_VALIDATION_ERROR] user_id={user_id}, error={e!s}", exc_info=True
                )
                await session.rollback()
                return JSONResponse(
                    status_code=400,
                    content={"success": False, "error": "Invalid topup parameters"},
                )
            except Exception as e:
                logger.critical(
                    f"[TOPUP_UNEXPECTED_ERROR] user_id={user_id}, error={e!s}", exc_info=True
                )
                await session.rollback()
                raise

    except HTTPException:
        raise
    except Exception as e:
        logger.critical(f"[TOPUP_CRITICAL_ERROR] user_id={user_id}, error={e!s}", exc_info=True)
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
    x_api_key: str | None = Header(None, alias="X-API-Key"),
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
            content={"success": False, "error": "Unauthorized: Invalid or missing API key"},
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
                "error": f"Invalid payment status '{data.status}'. Must be one of: {', '.join(valid_statuses)}",
            },
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
                    payment_method_details=data.payment_method_details,
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
                    elif (
                        "already processed" in error_msg.lower() or "duplicate" in error_msg.lower()
                    ):
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
                    f"status={data.status}, error={e!s}, "
                    f"elapsed_time={time.time() - start_time:.2f}s",
                    exc_info=True,
                )
                await session.rollback()
                return JSONResponse(
                    status_code=400,
                    content={
                        "success": False,
                        "error": "Invalid payment data format. Please check the webhook payload.",
                    },
                )
            except Exception as e:
                logger.critical(
                    f"[PAYMENT_WEBHOOK_UNEXPECTED_ERROR] payment_id={data.payment_id}, "
                    f"status={data.status}, error={e!s}, error_type={type(e).__name__}, "
                    f"elapsed_time={time.time() - start_time:.2f}s",
                    exc_info=True,
                )
                await session.rollback()
                raise

    except HTTPException:
        raise
    except Exception as e:
        logger.critical(
            f"[PAYMENT_WEBHOOK_CRITICAL_ERROR] payment_id={data.payment_id}, "
            f"status={data.status}, error={e!s}, error_type={type(e).__name__}, "
            f"elapsed_time={time.time() - start_time:.2f}s",
            exc_info=True,
        )
        # Return 500 to trigger retry from payment service
        return JSONResponse(
            status_code=500,
            content={
                "success": False,
                "error": "Internal server error processing payment webhook. Will retry.",
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
async def get_payment_status(payment_id: str, user_id: str = Depends(get_current_active_user)):
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
                "error": "Invalid payment ID. Payment ID must be at least 3 characters.",
            },
        )

    logger.info(f"[GET_PAYMENT_STATUS_REQUEST] payment_id={payment_id}, user_id={user_id}")

    try:
        async with async_session_scope() as session:
            try:
                result = await WalletService.get_payment_status(
                    payment_id=payment_id, user_id=user_id, session=session
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
                        user_error = (
                            "Unable to retrieve payment status. Please try again in a moment."
                        )
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
                    f"user_id={user_id}, error={e!s}, error_type={type(e).__name__}, "
                    f"elapsed_time={time.time() - start_time:.2f}s",
                    exc_info=True,
                )
                await session.rollback()
                raise

    except HTTPException:
        raise
    except Exception as e:
        logger.critical(
            f"[GET_PAYMENT_STATUS_CRITICAL_ERROR] payment_id={payment_id}, "
            f"user_id={user_id}, error={e!s}, error_type={type(e).__name__}, "
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
            content={"success": False, "error": "Amount must be greater than 0"},
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
            f"amount={data.amount_in_usd}, error={e!s}, error_type={type(e).__name__}, "
            f"elapsed_time={time.time() - start_time:.2f}s",
            exc_info=True,
        )
        return JSONResponse(
            status_code=500,
            content={"success": False, "error": "Unable to calculate tax. Please try again later."},
        )
