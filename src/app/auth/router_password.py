"""auth routes: password.

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

from app.adapters.email import send_password_reset_email
from app.auth.repository import (
    check_user_exists_by_email,
    update_user_password,
)
from app.auth.schemas import (
    ErrorResponse,
    ForgotPasswordRequest,
    ForgotPasswordResponse,
    ResetPasswordRequest,
    ResetPasswordResponse,
    VerifyResetTokenResponse,
)
from app.auth.token import (
    create_reset_password_token,
)
from app.core.constants import (
    FORGOT_PASSWORD_EMAIL_COOLDOWN_SECONDS,
    FORGOT_PASSWORD_EXPIRE_MINUTES,
    FRONTEND_RESET_PASSWORD_URL,
    JWT_ALGORITHM,
    JWT_SECRET_KEY,
)
from app.core.db import async_session_scope
from app.core.logging import setup_logging
from app.core.redis import get_redis_instance
from app.core.utils import generate_password_hash, verify_password
from app.observability.cloudwatch_utils import insert_cloudwatch_logs

router = APIRouter()
logger = setup_logging(__file__)
redis_instance = get_redis_instance()


@router.post(
    "/forgot-password",
    response_model=ForgotPasswordResponse,
    responses={422: {"model": ErrorResponse}, 500: {"model": ErrorResponse}},
)
async def forgot_password(data: ForgotPasswordRequest):
    """Forgot password endpoint"""
    logger.info(f"Forgot password request received for email: {data.email}")

    # Rate limit: max 5 forgot-password requests per email per 15 minutes
    rate_key = f"rate_limit:forgot_password:{data.email.strip().lower()}"
    try:
        attempts = await redis_instance.redis_client.incr(rate_key)
        if attempts == 1:
            await redis_instance.redis_client.expire(rate_key, 900)
        if attempts > 5:
            ttl = await redis_instance.redis_client.ttl(rate_key)
            logger.warning(
                f"Forgot-password rate limit exceeded for email: {data.email.strip().lower()}, attempts: {attempts}"
            )
            return JSONResponse(
                status_code=429,
                content={
                    "success": False,
                    "message": f"Too many password reset requests. Please try again in {ttl} seconds.",
                },
            )
    except Exception as e:
        logger.error(f"Rate limit check failed for forgot-password: {e}")

    try:
        forgot_password_time = datetime.now(UTC)
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

        # Check if password reset email has been sent recently
        forgot_password_email_key = f"forgot_password_email_sent:{email}"
        is_forgot_password_email_sent = await redis_instance.redis_client.exists(
            forgot_password_email_key
        )

        if is_forgot_password_email_sent:
            logger.info(f"Password reset email has already been sent recently to {email}")
            return ForgotPasswordResponse(
                success=False,
                message="A password reset link was recently sent. Please check your inbox or try again later.",
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
                        "message": "A system error occurred while processing your request. Please try again.",
                    },
                )

            if not db_response.get("exists", False):
                logger.warning(f"User with email {email} not found")
                # For security reasons, don't reveal that the email doesn't exist
                return JSONResponse(
                    status_code=422,
                    content={
                        "success": False,
                        "message": "I don't recognize that email address. Perhaps you signed up with a different one?",
                    },
                )
            if db_response.get("is_google_verified", False):
                logger.warning(f"User with email {email} is Google verified")
                return JSONResponse(
                    status_code=422,
                    content={
                        "success": False,
                        "message": "This account uses Google Sign-In. Please use the 'Sign in with Google' option to access your account.",
                    },
                )

            if not db_response.get("is_verified", False):
                logger.warning(f"User with email {email} is not verified")
                return JSONResponse(
                    status_code=422,
                    content={
                        "success": False,
                        "message": "This account is not verified. Please verify your account to reset your password.",
                    },
                )

            # User exists, generate reset token
            user_id = db_response.get("user_id")
            user_name = db_response.get("user_name")

            # Create reset token
            reset_token = create_reset_password_token(
                user_id=user_id, password_hash=db_response.get("password_hash")
            )

            # Generate reset link with token
            reset_link = f"{FRONTEND_RESET_PASSWORD_URL}?token={reset_token}"

            # Send reset email
            is_email_sent = await run_in_threadpool(
                lambda: send_password_reset_email(
                    recipient_email=email, user_name=user_name, reset_link=reset_link
                )
            )

            if not is_email_sent:
                logger.error(
                    f"Failed to send password reset email to {email} for user_id: {user_id}"
                )
                return JSONResponse(
                    status_code=500,
                    content={
                        "success": False,
                        "message": "Failed to send password reset email. Please try again later.",
                    },
                )

            # Set reset email sent key in redis with cooldown period
            await redis_instance.redis_client.setex(
                forgot_password_email_key, FORGOT_PASSWORD_EMAIL_COOLDOWN_SECONDS, 1
            )

            # Set token in redis with expiry time
            await redis_instance.redis_client.setex(
                f"reset_password_token_for_user:{user_id}",
                FORGOT_PASSWORD_EXPIRE_MINUTES * 60,
                reset_token,
            )

            logger.info(f"Password reset email sent to {email} for user_id: {user_id}")

            # Log the event
            cloudwatch_data = {
                "event": "Forgot password",
                "event_success": True,
                "timestamp": forgot_password_time.isoformat(),
            }
            try:
                asyncio.create_task(insert_cloudwatch_logs(data=cloudwatch_data, user_id=user_id))
            except Exception as e:
                logger.error(f"Error in inserting cloudwatch logs for user_id: {user_id}: {e}")

            return ForgotPasswordResponse(
                success=True,
                message="If your email is registered, you will receive a password reset link shortly.",
            )

    except ValueError as e:
        # This catches email validation errors from Pydantic
        logger.error(f"Validation error in forgot password: {e}")
        return JSONResponse(
            status_code=422, content={"success": False, "message": "Invalid email format"}
        )
    except Exception as e:
        logger.error(f"Error in forgot password: {e}")
        return JSONResponse(
            status_code=500,
            content={
                "success": False,
                "message": "A system error occurred while processing your request. Please try again.",
            },
        )


@router.get(
    "/verify-reset-token",
    response_model=VerifyResetTokenResponse,
    responses={422: {"model": ErrorResponse}, 500: {"model": ErrorResponse}},
)
async def verify_reset_token(token: str):
    """Verify reset token endpoint"""
    logger.info("Verify reset token request received")

    try:
        token = token.strip()
        verify_reset_token_time = datetime.now(UTC)

        # Validate input data
        if len(token) == 0:
            logger.error("Empty token received after trimming whitespace")
            return JSONResponse(
                status_code=422,
                content={"success": False, "valid": False, "message": "Token cannot be empty"},
            )

        # Verify token
        try:
            # Decode token
            payload = jwt.decode(token, JWT_SECRET_KEY, algorithms=[JWT_ALGORITHM])

            # Check token type
            if payload.get("type") != "reset_password":
                logger.error(
                    f"Invalid token type for reset password for user_id: {payload.get('user_id')}"
                )
                return JSONResponse(
                    status_code=401,
                    content={
                        "success": False,
                        "message": "That password reset link isn't valid. It might be incorrect or has been used before.",
                    },
                )

            # Get user_id from token
            user_id = payload.get("user_id")
            if not user_id:
                logger.error("User ID not found in token for reset password")
                return JSONResponse(
                    status_code=401,
                    content={
                        "success": False,
                        "message": "That password reset link isn't valid. It might be incorrect or has been used before.",
                    },
                )

            # Check if reset token of user is valid or not
            reset_token = await redis_instance.redis_client.get(
                f"reset_password_token_for_user:{user_id}"
            )
            if not reset_token:
                logger.error(f"Reset token not found for user_id: {user_id}")
                return JSONResponse(
                    status_code=401,
                    content={
                        "success": False,
                        "message": "The link has expired. Please request a new one.",
                    },
                )
            if reset_token != token:
                logger.error(f"Reset token mismatch for user_id: {user_id}")
                return JSONResponse(
                    status_code=401,
                    content={
                        "success": False,
                        "message": "The link has expired. Please request a new one.",
                    },
                )

            # Add cloudwatch logs
            cloudwatch_data = {
                "event": "Reset password link clicked",
                "event_success": True,
                "timestamp": verify_reset_token_time.isoformat(),
            }
            try:
                asyncio.create_task(insert_cloudwatch_logs(data=cloudwatch_data, user_id=user_id))
            except Exception as e:
                logger.error(f"Error in inserting cloudwatch logs for user_id: {user_id}: {e}")

            logger.info(f"Reset token verified successfully for user_id: {user_id}")
            # Token is valid
            return VerifyResetTokenResponse(success=True, valid=True, message="Token is valid")

        except ExpiredSignatureError:
            logger.error("Token has expired for reset password")
            return VerifyResetTokenResponse(
                success=True,
                valid=False,
                message="For security, that password reset link has expired. You can request a new one.",
            )
        except JWTError:
            logger.error("Invalid token for reset password")
            return VerifyResetTokenResponse(
                success=True,
                valid=False,
                message="That password reset link isn't valid. It might be incorrect or has been used before.",
            )

    except Exception as e:
        logger.error(f"Error in verify reset token: {e}")
        return JSONResponse(
            status_code=500,
            content={
                "success": False,
                "valid": False,
                "message": "I couldn't verify that link due to a system error. Please try it again.",
            },
        )


@router.post(
    "/reset-password",
    response_model=ResetPasswordResponse,
    responses={422: {"model": ErrorResponse}, 500: {"model": ErrorResponse}},
)
async def reset_password(data: ResetPasswordRequest):
    """Reset password endpoint"""
    logger.info("Reset password request received")

    # Rate limit: max 5 reset-password attempts per token per 15 minutes
    token_hash = data.token.strip()[
        :32
    ]  # Use first 32 chars as key prefix (avoid full token in Redis key)
    rate_key = f"rate_limit:reset_password:{token_hash}"
    try:
        attempts = await redis_instance.redis_client.incr(rate_key)
        if attempts == 1:
            await redis_instance.redis_client.expire(rate_key, 900)
        if attempts > 5:
            ttl = await redis_instance.redis_client.ttl(rate_key)
            logger.warning(f"Reset-password rate limit exceeded, attempts: {attempts}")
            return JSONResponse(
                status_code=429,
                content={
                    "success": False,
                    "message": f"Too many password reset attempts. Please try again in {ttl} seconds.",
                },
            )
    except Exception as e:
        logger.error(f"Rate limit check failed for reset-password: {e}")

    try:
        token = data.token.strip()
        password = data.password

        # Validate input data
        if len(token) == 0:
            logger.error("Empty token received after trimming whitespace")
            return JSONResponse(
                status_code=422, content={"success": False, "message": "Token cannot be empty"}
            )

        if len(password) == 0:
            logger.error("Empty password received")
            return JSONResponse(
                status_code=422, content={"success": False, "message": "Password cannot be empty"}
            )

        if len(password) > 50:
            logger.error("Password is too long")
            return JSONResponse(
                status_code=422,
                content={
                    "success": False,
                    "message": "Password cannot be longer than 50 characters",
                },
            )

        # Verify token
        try:
            # Decode token
            payload = jwt.decode(token, JWT_SECRET_KEY, algorithms=[JWT_ALGORITHM])

            # Check token type
            if payload.get("type") != "reset_password":
                logger.error(
                    f"Invalid token type for reset password for user_id: {payload.get('user_id')}"
                )
                return JSONResponse(
                    status_code=401,
                    content={
                        "success": False,
                        "message": "That reset link is invalid. Please start the password reset process over.",
                    },
                )

            # Get user_id from token
            user_id = payload.get("user_id")
            if not user_id:
                logger.error("User ID not found in token for reset password")
                return JSONResponse(
                    status_code=401,
                    content={
                        "success": False,
                        "message": "That reset link is invalid. Please start the password reset process over.",
                    },
                )

            # Check if reset token of user is valid or not
            reset_token = await redis_instance.redis_client.get(
                f"reset_password_token_for_user:{user_id}"
            )
            if not reset_token:
                logger.error(f"Reset token not found for user_id: {user_id}")
                return JSONResponse(
                    status_code=401,
                    content={
                        "success": False,
                        "message": "The link has expired. Please request a new one.",
                    },
                )
            if reset_token != token:
                logger.error(f"Reset token mismatch for user_id: {user_id}")
                return JSONResponse(
                    status_code=401,
                    content={
                        "success": False,
                        "message": "The link has expired. Please request a new one.",
                    },
                )

            old_password_hash = payload.get("password_hash")
            if not old_password_hash:
                logger.error(
                    f"Old password hash not found in token for reset password for user_id: {user_id}"
                )
                return JSONResponse(
                    status_code=401,
                    content={
                        "success": False,
                        "message": "That reset link is invalid. Please start the password reset process over.",
                    },
                )

            if verify_password(plain_password=password, hashed_password=old_password_hash):
                logger.error(
                    f"New password cannot be the same as the old password for user_id: {user_id}"
                )
                return JSONResponse(
                    status_code=401,
                    content={
                        "success": False,
                        "message": "Your new password needs to be different from your old one. Please choose a new one.",
                    },
                )

            # Update user password
            async with async_session_scope() as session:
                # Generate password hash
                password_hash = generate_password_hash(password)

                # Update user password
                db_response = await update_user_password(
                    user_id=user_id, password_hash=password_hash, session=session
                )
                if not db_response.get("success", False):
                    logger.error(
                        f"Database error: {db_response.get('error')} for user_id: {user_id}"
                    )
                    return JSONResponse(
                        status_code=500,
                        content={
                            "success": False,
                            "message": "I was unable to update your password due to a system error. Please try again.",
                        },
                    )

                # await session.commit()
                # logger.info(f"Password updated successfully for user_id: {user_id}")

                # Log the event
                cloudwatch_data = {
                    "event": "Password reset",
                    "event_success": True,
                    "timestamp": datetime.now(UTC).isoformat(),
                }
                try:
                    asyncio.create_task(
                        insert_cloudwatch_logs(data=cloudwatch_data, user_id=user_id)
                    )
                except Exception as e:
                    logger.error(f"Error in inserting cloudwatch logs for user_id: {user_id}: {e}")

                # blacklist the token
                # await redis_instance.redis_client.setex(f"blacklisted_token:{token}", (FORGOT_PASSWORD_EXPIRE_MINUTES*60)+3600, 1)

                # delete the reset token from redis
                await redis_instance.redis_client.delete(f"reset_password_token_for_user:{user_id}")

                logger.info(f"Password reset successfully for user_id: {user_id}")
                return ResetPasswordResponse(
                    success=True, message="Password has been reset successfully"
                )

        except ExpiredSignatureError:
            logger.error("Token has expired for reset password")
            return JSONResponse(
                status_code=401,
                content={
                    "success": False,
                    "message": "Reset link has expired. Please request a new one.",
                },
            )
        except JWTError:
            logger.error("Invalid token for reset password")
            return JSONResponse(
                status_code=401,
                content={
                    "success": False,
                    "message": "That reset link is invalid. Please start the password reset process over.",
                },
            )

    except Exception as e:
        logger.error(f"Error in reset password: {e}")
        return JSONResponse(
            status_code=500,
            content={
                "success": False,
                "message": "I was unable to update your password due to a system error. Please try again.",
            },
        )
