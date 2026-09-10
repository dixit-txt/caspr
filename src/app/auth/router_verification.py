"""auth routes: verification.

Split out of ``app/auth/router.py`` to keep each router file under
the ~400-line ceiling in R-STRUCT-3. Handlers are unchanged.
"""

"""HTTP routes for the auth bounded context.

Handlers moved verbatim from ``src/resources/routers/api.py`` during the
R-STRUCT-1 migration, which split that 10,257-line module across seven
contexts. Handler bodies are unchanged; only the import block and the
``APIRouter`` they attach to are new.
"""

"""api.py: Authentication API for the Casper backend"""
import asyncio
from datetime import UTC, datetime

from fastapi import APIRouter
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from jose import ExpiredSignatureError, JWTError, jwt

from app.adapters.email import send_signup_verification_email
from app.auth.repository import (
    check_user_exists_by_email,
    update_user_verification_status,
)
from app.auth.schemas import (
    ErrorResponse,
    SendVerificationLinkRequest,
    SendVerificationLinkResponse,
    VerifyAccountTokenRequest,
    VerifyAccountTokenResponse,
)
from app.auth.token import (
    create_access_token,
    create_refresh_token,
    create_verification_token,
)
from app.core.constants import (
    FRONTEND_VERIFICATION_URL,
    JWT_ALGORITHM,
    JWT_SECRET_KEY,
    SIGNUP_VERIFICATION_EMAIL_COOLDOWN_SECONDS,
    SIGNUP_VERIFICATION_EXPIRE_MINUTES,
)
from app.core.db import async_session_scope
from app.core.logging import setup_logging
from app.core.redis import get_redis_instance
from app.observability.cloudwatch_utils import insert_cloudwatch_logs

router = APIRouter()
logger = setup_logging(__file__)
redis_instance = get_redis_instance()


@router.post(
    "/send-verification-link",
    response_model=SendVerificationLinkResponse,
    responses={422: {"model": ErrorResponse}, 500: {"model": ErrorResponse}},
)
async def send_verification_link(data: SendVerificationLinkRequest):
    """Send verification link endpoint"""
    logger.info(f"Send verification link request received for email: {data.email}")

    try:
        email = data.email.strip().lower()

        # Validate input data
        if len(email) == 0:
            logger.error("Empty email received after trimming whitespace")
            return JSONResponse(
                status_code=422, content={"success": False, "message": "Email cannot be empty"}
            )

        if len(email) > 50:
            logger.error(f"Email received is too long: {email}")
            return JSONResponse(
                status_code=422,
                content={"success": False, "message": "Email cannot be longer than 50 characters"},
            )

        async with async_session_scope() as session:
            # Check if user exists
            db_response = await check_user_exists_by_email(email=email, session=session)

            if not db_response.get("success", False):
                logger.error(f"Database error: {db_response.get('error')}")
                return JSONResponse(
                    status_code=500,
                    content={
                        "success": False,
                        "message": "An unexpected error occurred. Please try requesting a new verification link.",
                    },
                )

            if not db_response.get("exists", False):
                logger.warning(f"User with email {email} not found")
                return JSONResponse(
                    status_code=401,
                    content={
                        "success": False,
                        "message": "I can't find that email in our system. That email is not registered. Please sign up to create an account.",
                    },
                )

            # Check if user is already verified
            if db_response.get("is_verified", False):
                logger.info(f"User with email {email} is already verified")
                return JSONResponse(
                    status_code=401,
                    content={"success": False, "message": "Your email is already verified."},
                )

            # Check if verification email has already been sent
            user_id = db_response.get("user_id")
            user_name = db_response.get("user_name")

            verification_email_key = f"verification_email_sent:{email}"
            is_verification_email_sent = await redis_instance.redis_client.exists(
                verification_email_key
            )

            if is_verification_email_sent:
                logger.info(
                    f"Verification email has already been sent to {email} for user_id: {user_id}"
                )
                return SendVerificationLinkResponse(
                    success=False,
                    message="Verification email has already been sent. Please check your inbox.",
                )

            # Create verification token
            verification_token = create_verification_token(email=email)

            # Generate verification link with token
            verification_link = f"{FRONTEND_VERIFICATION_URL}?token={verification_token}"

            # Send verification email
            is_email_sent = await run_in_threadpool(
                lambda: send_signup_verification_email(
                    recipient_email=email, user_name=user_name, verification_link=verification_link
                )
            )

            if not is_email_sent:
                logger.error(f"Failed to send verification email to {email} for user_id: {user_id}")
                return JSONResponse(
                    status_code=500,
                    content={
                        "success": False,
                        "message": "The verification email could not be sent due to a system error. Please try again.",
                    },
                )

            # Set verification email sent key in redis
            await redis_instance.redis_client.setex(
                verification_email_key, SIGNUP_VERIFICATION_EMAIL_COOLDOWN_SECONDS, 1
            )

            # set the verification token sent to user in redis
            await redis_instance.redis_client.setex(
                f"verification_token_for_user:{user_id}",
                SIGNUP_VERIFICATION_EXPIRE_MINUTES * 60,
                verification_token,
            )

            logger.info(f"Verification email sent to {email} for user_id: {user_id}")

            # Log the event
            cloudwatch_data = {
                "event": "Signup verification",
                "event_success": True,
                "timestamp": datetime.now(UTC).isoformat(),
            }
            try:
                asyncio.create_task(insert_cloudwatch_logs(data=cloudwatch_data, user_id=user_id))
            except Exception as e:
                logger.error(f"Error in inserting cloudwatch logs for user_id: {user_id}: {e}")

            return SendVerificationLinkResponse(
                success=True, message="Verification email has been sent. Please check your inbox."
            )

    except ValueError as e:
        # This catches email validation errors from Pydantic
        logger.error(f"Validation error in send verification link: {e}")
        return JSONResponse(
            status_code=422, content={"success": False, "message": "Invalid email format"}
        )
    except Exception as e:
        logger.error(f"Error in send verification link: {e}")
        return JSONResponse(
            status_code=500,
            content={
                "success": False,
                "message": "An unexpected error occurred. Please try requesting a new verification link.",
            },
        )


@router.post(
    "/verify-account-token",
    response_model=VerifyAccountTokenResponse,
    responses={422: {"model": ErrorResponse}, 500: {"model": ErrorResponse}},
)
async def verify_account_token(data: VerifyAccountTokenRequest):
    """Verify account token endpoint"""
    logger.info("Verify account token request received")

    try:
        verification_time = datetime.now(UTC)
        token = data.token.strip()

        # Validate input data
        if len(token) == 0:
            logger.error("Empty token received after trimming whitespace")
            return JSONResponse(
                status_code=422, content={"success": False, "message": "Token cannot be empty"}
            )

        # Verify token
        try:
            # Decode token
            payload = jwt.decode(token, JWT_SECRET_KEY, algorithms=[JWT_ALGORITHM])

            # Check if token is blacklisted
            # is_blacklisted = await redis_instance.redis_client.exists(f"blacklisted_token:{token}")
            # if is_blacklisted:
            #     logger.error("Token is blacklisted")
            #     return JSONResponse(
            #         status_code=422,
            #         content={"success": False, "message": "The That verification link has expired. Please request a fresh one."}
            #     )

            # Check token type
            if payload.get("type") != "verification":
                logger.error("Invalid token type for verification")
                return JSONResponse(
                    status_code=401,
                    content={
                        "success": False,
                        "message": "That verification link appears to be invalid. Please try requesting a new one.",
                    },
                )

            # Get email from token
            email = payload.get("email")
            if not email:
                logger.error("Email not found in token")
                return JSONResponse(
                    status_code=401,
                    content={
                        "success": False,
                        "message": "There seems to be an issue with that link. Please try generating a new one from your account.",
                    },
                )

            async with async_session_scope() as session:
                # Check if user exists
                db_response = await check_user_exists_by_email(email=email, session=session)

                if not db_response.get("success", False):
                    logger.error(f"Database error: {db_response.get('error')}")
                    return JSONResponse(
                        status_code=500,
                        content={
                            "success": False,
                            "message": "We've hit an unexpected error during verification. Our team is on it. Please try again.",
                        },
                    )

                if not db_response.get("exists", False):
                    logger.warning(f"User with email {email} not found")
                    return JSONResponse(
                        status_code=401,
                        content={
                            "success": False,
                            "message": "Your email is not registered with us.",
                        },
                    )

                user_id = db_response.get("user_id")
                user_name = db_response.get("user_name")
                t_c_verified = db_response.get("t_c_verified", False)

                if not user_id:
                    logger.error(f"User ID not found for email: {email}")
                    return JSONResponse(
                        status_code=401,
                        content={
                            "success": False,
                            "message": "This email isn't registered with us yet. Please sign up to create an account.",
                        },
                    )

                # check if the verification token is valid
                verification_token = await redis_instance.redis_client.get(
                    f"verification_token_for_user:{user_id}"
                )

                if verification_token is None:
                    # Redis key expired or was deleted
                    logger.error(f"Verification token expired or not found for user_id: {user_id}")
                    return JSONResponse(
                        status_code=401,
                        content={
                            "success": False,
                            "message": "That verification link has expired. Please request a fresh one.",
                        },
                    )

                if verification_token != token:
                    # Token exists but doesn't match - user requested a new link
                    logger.error(
                        f"Verification token mismatch for user_id: {user_id} - a newer link was issued"
                    )
                    return JSONResponse(
                        status_code=401,
                        content={
                            "success": False,
                            "message": "This verification link is no longer valid. Please use the most recent link sent to your email.",
                        },
                    )

                # Check if user is already verified
                if db_response.get("is_verified", False):
                    logger.info(
                        f"User with email {email} and user_id: {user_id} is already verified"
                    )

                    # Generate JWT tokens for auto-login
                    access_token = create_access_token(user_id)
                    refresh_token = create_refresh_token(user_id)

                    logger.info(
                        f"Auto-login successful for already verified user with ID: {user_id}"
                    )
                    return VerifyAccountTokenResponse(
                        success=True,
                        message="Your email is already verified. You are now logged in.",
                        token=access_token,
                        refresh_token=refresh_token,
                        user_id=user_id,
                        user_name=user_name,
                        t_c_verified=t_c_verified,
                    )

                # Update user verification status
                db_response = await update_user_verification_status(
                    email=email,
                    verified_at=verification_time,
                    is_verified=True,
                    is_google_verified=False,
                    session=session,
                )

                if not db_response.get("success", False):
                    logger.error(
                        f"Database error: {db_response.get('error')} for user_id: {user_id}"
                    )
                    return JSONResponse(
                        status_code=500,
                        content={
                            "success": False,
                            "message": "We've hit an unexpected error during verification. Our team is on it. Please try again.",
                        },
                    )

                # delete the verification token from redis
                await redis_instance.redis_client.delete(f"verification_token_for_user:{user_id}")

                # Generate JWT tokens for auto-login
                access_token = create_access_token(user_id)
                refresh_token = create_refresh_token(user_id)

                # Log the event
                cloudwatch_data = {
                    "event": "Account verified",
                    "event_success": True,
                    "timestamp": verification_time.isoformat(),
                }
                try:
                    asyncio.create_task(
                        insert_cloudwatch_logs(data=cloudwatch_data, user_id=user_id)
                    )
                except Exception as e:
                    logger.error(f"Error in inserting cloudwatch logs for user_id: {user_id}: {e}")

                logger.info(
                    f"Account verified and auto-login successful for email: {email} and user_id: {user_id}"
                )
                return VerifyAccountTokenResponse(
                    success=True,
                    message="Your email has been verified successfully. You are now logged in.",
                    token=access_token,
                    refresh_token=refresh_token,
                    user_id=user_id,
                    user_name=user_name,
                    t_c_verified=t_c_verified,
                )

        except ExpiredSignatureError:
            logger.error("Token has expired for verification")
            return JSONResponse(
                status_code=401,
                content={
                    "success": False,
                    "message": "That verification link has expired. Please request a fresh one.",
                },
            )
        except JWTError:
            logger.error("Invalid token for verification")
            return JSONResponse(
                status_code=401,
                content={
                    "success": False,
                    "message": "That verification link appears to be invalid. Please try requesting a new one.",
                },
            )

    except Exception as e:
        logger.error(f"Error in verify account token: {e}")
        return JSONResponse(
            status_code=500,
            content={
                "success": False,
                "message": "We've hit an unexpected error during verification. Our team is on it. Please try again.",
            },
        )
