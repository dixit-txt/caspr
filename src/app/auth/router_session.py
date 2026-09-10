"""auth routes: session.

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
import random
from datetime import UTC, datetime

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token

from app.auth.repository import (
    check_user_by_id,
    check_user_exists_by_email,
    check_user_exists_by_email_or_phone,
    create_user,
    update_user_verification_status,
    verify_user_credentials,
)
from app.auth.schemas import (
    ErrorResponse,
    GoogleLoginRequest,
    GoogleLoginResponse,
    LoginRequest,
    LoginResponse,
    LogoutRequest,
    LogoutResponse,
    SignupRequest,
    SignupResponse,
)
from app.auth.token import (
    create_access_token,
    create_refresh_token,
    verify_token,
)
from app.chats.repository import insert_chats
from app.core.constants import (
    GOOGLE_CLIENT_ID,
)
from app.core.db import async_session_scope
from app.core.logging import setup_logging
from app.core.redis import get_redis_instance
from app.core.utils import generate_password_hash
from app.observability.cloudwatch_utils import insert_cloudwatch_logs
from app.wallet.service import WalletService

router = APIRouter()
logger = setup_logging(__file__)
redis_instance = get_redis_instance()


# API Endpoints
@router.post(
    "/signup",
    response_model=SignupResponse,
    responses={422: {"model": ErrorResponse}, 500: {"model": ErrorResponse}},
)
async def signup(data: SignupRequest, chat_id: str | None = None):
    """User signup endpoint"""
    # Rate limit: max 5 signup attempts per email per 15 minutes
    rate_key = f"rate_limit:signup:{data.email.strip().lower()}"
    try:
        attempts = await redis_instance.redis_client.incr(rate_key)
        if attempts == 1:
            await redis_instance.redis_client.expire(rate_key, 900)
        if attempts > 5:
            ttl = await redis_instance.redis_client.ttl(rate_key)
            logger.warning(
                f"Signup rate limit exceeded for email: {data.email.strip().lower()}, attempts: {attempts}"
            )
            return JSONResponse(
                status_code=429,
                content={
                    "success": False,
                    "error": f"Too many signup attempts. Please try again in {ttl} seconds.",
                },
            )
    except Exception as e:
        logger.error(f"Rate limit check failed for signup: {e}")

    async with async_session_scope() as session:
        try:
            signup_request_time = datetime.now(UTC)
            name = data.name.strip()
            email = data.email.strip().lower()
            phone = data.phone.strip()
            phone_country_code = data.phone_country_code.strip()
            password = data.password

            # Validate input data
            if len(name) == 0:
                logger.error("Empty name received after trimming whitespace")
                return JSONResponse(
                    status_code=422, content={"success": False, "error": "Name cannot be empty"}
                )

            if len(phone) == 0:
                logger.error("Empty phone received after trimming whitespace")
                return JSONResponse(
                    status_code=422,
                    content={"success": False, "error": "Phone number cannot be empty"},
                )

            if len(phone_country_code) == 0:
                logger.error("Empty phone_country_code received after trimming whitespace")
                return JSONResponse(
                    status_code=422,
                    content={"success": False, "error": "Phone country code cannot be empty"},
                )

            if len(password) == 0:
                logger.error("Empty password received after trimming whitespace")
                return JSONResponse(
                    status_code=422, content={"success": False, "error": "Password cannot be empty"}
                )

            if len(email) == 0:
                logger.error("Empty email received after trimming whitespace")
                return JSONResponse(
                    status_code=422, content={"success": False, "error": "Email cannot be empty"}
                )

            if len(password) > 50:
                logger.error(f"Password received is too long for phone: {phone}")
                return JSONResponse(
                    status_code=422,
                    content={
                        "success": False,
                        "error": "Password cannot be longer than 50 characters",
                    },
                )

            if len(name) > 50:
                logger.error(f"Name received is too long for phone: {phone}")
                return JSONResponse(
                    status_code=422,
                    content={"success": False, "error": "Name cannot be longer than 50 characters"},
                )

            if len(email) > 50:
                logger.error(f"Email received is too long for phone: {phone}")
                return JSONResponse(
                    status_code=422,
                    content={
                        "success": False,
                        "error": "Email cannot be longer than 50 characters",
                    },
                )

            # Check if user already exists
            db_response = await check_user_exists_by_email_or_phone(
                email=email, phone=phone, session=session
            )
            if not db_response.get("success", False):
                logger.error(
                    f"Database error: Failed to check if user exists: {db_response.get('error')}"
                )
                return JSONResponse(
                    status_code=500,
                    content={
                        "success": False,
                        "error": "Something went wrong on our end while creating your account. We're looking into it.",
                    },
                )

            if db_response.get("exists", False):
                logger.warning(f"User with email {email} or phone {phone} already exists")
                return JSONResponse(
                    status_code=422,
                    content={
                        "success": False,
                        "error": "User with this email or phone already exists",
                    },
                )

            # Create new user
            logger.info(f"Creating new user: {name}, {email}, {phone}")

            # Create new user record
            new_user = {
                "user_name": name,
                "email": email,
                "phone": phone,
                "phone_country_code": phone_country_code,
                "password_hash": generate_password_hash(password),
                "is_google_verified": False,
                "created_at": signup_request_time,
                "is_verified": False,
                "verified_at": None,
            }

            db_response = await create_user(user_data=new_user, session=session)
            if not db_response.get("success", False):
                logger.error(f"Database error: Failed to create user: {db_response.get('error')}")
                return JSONResponse(
                    status_code=500,
                    content={
                        "success": False,
                        "error": "Something went wrong on our end while creating your account. We're looking into it.",
                    },
                )
            user_id = db_response.get("id")
            logger.info(f"User created successfully with ID: {user_id}")

            # Process referral code if provided
            referrer_id = None
            is_referral = False
            bonus_tokens = 0

            if data.referral_code:
                referral_code = data.referral_code.strip().upper()
                logger.info(f"[REFERRAL_SIGNUP_ATTEMPT] user_id={user_id}, code={referral_code}")

                from app.referrals.repository import (
                    check_user_already_referred,
                    create_referral_record,
                    credit_referral_bonus,
                    get_user_by_referral_code,
                )

                # Validate referral code
                referrer = await get_user_by_referral_code(referral_code, session)

                if referrer and referrer.id != user_id:
                    # Check if user already used a referral code
                    already_referred = await check_user_already_referred(user_id, session)

                    if not already_referred:
                        referrer_id = referrer.id
                        is_referral = True
                        bonus_tokens = 25000
                        logger.info(
                            f"[REFERRAL_VALID] user_id={user_id}, referrer_id={referrer_id}, "
                            f"code={referral_code}"
                        )
                    else:
                        logger.warning(
                            f"[REFERRAL_ALREADY_USED] user_id={user_id}, code={referral_code}"
                        )
                elif referrer and referrer.id == user_id:
                    logger.warning(
                        f"[REFERRAL_SELF_ATTEMPT] user_id={user_id}, code={referral_code}"
                    )
                else:
                    logger.warning(
                        f"[REFERRAL_INVALID_CODE] user_id={user_id}, code={referral_code}"
                    )

            # Initialize wallet with signup bonus + optional referral bonus
            wallet_response = await WalletService.initialize_wallet(
                user_id=user_id,
                session=session,
                bonus_tokens=bonus_tokens,
                bonus_reason="referral_bonus_received" if is_referral else None,
            )

            if not wallet_response.get("success", False):
                logger.error(
                    f"Failed to initialize wallet for user_id: {user_id}: {wallet_response.get('error')}"
                )
                # Rollback user creation if wallet creation fails
                await session.rollback()
                return JSONResponse(
                    status_code=500,
                    content={
                        "success": False,
                        "error": "Something went wrong on our end while creating your account. We're looking into it.",
                    },
                )

            # If valid referral, credit referrer and create referral record
            if is_referral and referrer_id:
                from app.referrals.repository import create_referral_record, credit_referral_bonus

                # Credit referrer with bonus tokens
                referrer_bonus_result = await credit_referral_bonus(
                    user_id=referrer_id,
                    tokens=25000,
                    reason="referral_bonus_given",
                    session=session,
                )

                if referrer_bonus_result.get("success"):
                    logger.info(
                        f"[REFERRAL_BONUS_CREDITED] referrer_id={referrer_id}, "
                        f"tokens=25000, batch_id={referrer_bonus_result.get('batch_id')}"
                    )
                else:
                    logger.error(
                        f"[REFERRAL_BONUS_FAILED] referrer_id={referrer_id}, "
                        f"error={referrer_bonus_result.get('error')}"
                    )

                # Create referral record
                referral_record_result = await create_referral_record(
                    referrer_id=referrer_id,
                    referee_id=user_id,
                    referral_code=data.referral_code.strip().upper(),
                    session=session,
                )

                if referral_record_result.get("success"):
                    logger.info(
                        f"[REFERRAL_RECORD_CREATED] referral_id={referral_record_result.get('referral_id')}, "
                        f"referrer_id={referrer_id}, referee_id={user_id}"
                    )
                else:
                    logger.error(
                        f"[REFERRAL_RECORD_FAILED] referrer_id={referrer_id}, "
                        f"referee_id={user_id}, error={referral_record_result.get('error')}"
                    )

            # Commit user, wallet, and referral data
            await session.commit()

            total_tokens = wallet_response.get("tokens_credited", 0) + bonus_tokens
            logger.info(
                f"User and wallet created successfully for user_id: {user_id} "
                f"with {total_tokens} tokens (base: {wallet_response.get('tokens_credited', 0)}, "
                f"referral_bonus: {bonus_tokens})"
            )

            # Temp user signup
            if chat_id:
                logger.info(
                    f"Temporary user signup with chat_id: {chat_id} and user_id: {user_id} and email: {email}"
                )

                redis_response = await redis_instance.check_chat_exists(chat_id=chat_id)
                if not redis_response.get("success"):
                    logger.error(
                        f"Failed to check if chat exists in redis with ID: {chat_id}: {redis_response.get('error')}"
                    )
                    return JSONResponse(
                        status_code=500,
                        content={
                            "success": False,
                            "error": "Something went wrong on our end while creating your account. We're looking into it.",
                        },
                    )

                temp_chat_exists_in_redis = redis_response.get("exists", False)

                if temp_chat_exists_in_redis:
                    redis_response = await redis_instance.get_chat(chat_id=chat_id)
                    if not redis_response.get("success"):
                        logger.error(
                            f"Failed to get chat from Redis for chat_id: {chat_id}, user_id: {user_id}: {redis_response.get('error')}"
                        )
                        return JSONResponse(
                            status_code=500,
                            content={
                                "success": False,
                                "error": "I couldn't access the temporary chat data right now. Please try the signup process again.",
                            },
                        )

                    message_data = {
                        "user_id": user_id,
                        "chat_id": chat_id,
                        "chat_messages": redis_response.get("chat_data", {}).get("chat_messages"),
                        "updated_at": redis_response.get("chat_data", {}).get("updated_at"),
                        "created_at": redis_response.get("chat_data", {}).get("created_at"),
                        "chat_title": redis_response.get("chat_data", {}).get("chat_title"),
                    }

                    db_response = await insert_chats(message_data=message_data, session=session)
                    if not db_response.get("success", False):
                        logger.error(
                            f"Database error: Failed to insert chat for chat_id: {chat_id}, user_id: {user_id}: {db_response.get('error')}"
                        )
                        return JSONResponse(
                            status_code=500,
                            content={
                                "success": False,
                                "error": "There was a problem saving your chat messages. Let's try that again.",
                            },
                        )

                    logger.info(
                        f"Temporary chat inserted successfully with ID: {chat_id} for user_id: {user_id} and user_name: {name} and email: {email}"
                    )

                    redis_response = await redis_instance.delete_chat(chat_id=chat_id)
                    if not redis_response.get("success"):
                        logger.error(
                            f"Failed to delete chat from redis with ID: {chat_id} for user_id: {user_id}: {redis_response.get('error')}"
                        )
                        return JSONResponse(
                            status_code=500,
                            content={
                                "success": False,
                                "error": "We've hit a snag trying to clean up temporary data. Please try to sign up again.",
                            },
                        )

                    logger.info(
                        f"Temporary chat deleted successfully with ID: {chat_id} from redis"
                    )

                    temp_chat_session = await redis_instance.redis_client.get(
                        f"temp_chat_session:{chat_id}"
                    )
                    if temp_chat_session:
                        await redis_instance.redis_client.delete(temp_chat_session)
                    await redis_instance.redis_client.delete(f"temp_chat_session:{chat_id}")
                    logger.info(f"Temporary chat session deleted successfully with ID: {chat_id}")

            else:
                logger.warning(
                    f"Temporary chat with ID: {chat_id} not found in redis, temporary chat session expired for user_id: {user_id}"
                )

            cloudwatch_data = {
                "event": "Signup",
                "event_success": True,
                "timestamp": datetime.now(UTC).isoformat(),
            }
            try:
                asyncio.create_task(insert_cloudwatch_logs(data=cloudwatch_data, user_id=user_id))
            except Exception as e:
                logger.error(f"Error in inserting cloudwatch logs for user_id: {user_id}: {e}")

            return SignupResponse(success=True)

        except ValueError as e:
            # This catches email validation errors from Pydantic
            logger.error(f"Validation error in signup: {e}")
            return JSONResponse(
                status_code=422, content={"success": False, "error": "Invalid email format"}
            )
        except Exception as e:
            logger.error(f"Error in signup: {e}")
            await session.rollback()
            return JSONResponse(
                status_code=500,
                content={
                    "success": False,
                    "error": "Something went wrong on our end while creating your account. We're looking into it.",
                },
            )


@router.post(
    "/login",
    response_model=LoginResponse,
    responses={422: {"model": ErrorResponse}, 500: {"model": ErrorResponse}},
)
async def login(data: LoginRequest, chat_id: str | None = None):
    """User login endpoint"""
    # Rate limit: max 10 login attempts per email per 15 minutes
    rate_key = f"rate_limit:login:{data.email.strip().lower()}"
    try:
        attempts = await redis_instance.redis_client.incr(rate_key)
        if attempts == 1:
            await redis_instance.redis_client.expire(rate_key, 900)
        if attempts > 10:
            ttl = await redis_instance.redis_client.ttl(rate_key)
            logger.warning(
                f"Login rate limit exceeded for email: {data.email.strip().lower()}, attempts: {attempts}"
            )
            return JSONResponse(
                status_code=429,
                content={
                    "success": False,
                    "error": f"Too many login attempts. Please try again in {ttl} seconds.",
                },
            )
    except Exception as e:
        logger.error(f"Rate limit check failed for login: {e}")

    async with async_session_scope() as session:
        try:
            email = data.email.strip().lower()
            password = data.password
            # Validate input data
            if len(email) > 50:
                logger.error(f"Email received is too long: {email}")
                return JSONResponse(
                    status_code=422,
                    content={
                        "success": False,
                        "error": "The email address you entered is too long. Please check it and try again.",
                    },
                )

            if len(password) > 50:
                logger.error(f"Password received is too long: {password}")
                return JSONResponse(
                    status_code=422,
                    content={
                        "success": False,
                        "error": "The password you entered is too long. Please try again.",
                    },
                )

            # Check if user exists and credentials are valid
            db_response = await verify_user_credentials(
                email=email, password=password, session=session
            )

            if not db_response.get("success", False):
                logger.error(f"Database error: {db_response.get('error')}")
                return JSONResponse(
                    status_code=500,
                    content={
                        "success": False,
                        "error": "I'm having trouble verifying your details right now. Please wait a moment and try again.",
                    },
                )

            if not db_response.get("valid", False):
                logger.warning(f"Invalid login attempt for email {email}")
                if "is_verified" in db_response and not db_response["is_verified"]:
                    return JSONResponse(
                        status_code=401,
                        content={
                            "success": False,
                            "error": db_response.get("error", "Invalid email or password"),
                            "is_verified": False,
                        },
                    )
                else:
                    return JSONResponse(
                        status_code=401,
                        content={
                            "success": False,
                            "error": db_response.get("error", "Invalid email or password"),
                        },
                    )

            user_id = db_response.get("id")
            user_name = db_response.get("user_name")
            is_verified = db_response.get("is_verified", False)
            t_c_verified = db_response.get("t_c_verified", False)
            onboarding_completed = db_response.get("onboarding_completed", False)

            # Temp user login
            if chat_id:
                logger.info(
                    f"Temporary user login with chat_id: {chat_id} and user_id: {user_id} and user_name: {user_name} and email: {email}"
                )

                redis_response = await redis_instance.check_chat_exists(chat_id=chat_id)
                if not redis_response.get("success"):
                    logger.error(
                        f"Failed to check if chat exists in redis with ID: {chat_id} for user_id: {user_id}: {redis_response.get('error')}"
                    )
                    return JSONResponse(
                        status_code=500,
                        content={
                            "success": False,
                            "error": "There was an issue accessing temporary chat data. Please try your login again.",
                        },
                    )

                temp_chat_exists_in_redis = redis_response.get("exists", False)

                # when existing user logs in from temp chat without signing up first from temp chat
                if temp_chat_exists_in_redis:
                    redis_response = await redis_instance.get_chat(chat_id=chat_id)
                    if not redis_response.get("success"):
                        logger.error(
                            f"Failed to get chat from Redis for chat_id: {chat_id}, user_id: {user_id}: {redis_response.get('error')}"
                        )
                        return JSONResponse(
                            status_code=500,
                            content={
                                "success": False,
                                "error": "I'm unable to retrieve temporary chat data at the moment. Please try logging in again.",
                            },
                        )

                    message_data = {
                        "user_id": user_id,
                        "chat_id": chat_id,
                        "chat_messages": redis_response.get("chat_data", {}).get("chat_messages"),
                        "updated_at": redis_response.get("chat_data", {}).get("updated_at"),
                        "created_at": redis_response.get("chat_data", {}).get("created_at"),
                        "chat_title": redis_response.get("chat_data", {}).get("chat_title"),
                    }

                    db_response = await insert_chats(message_data=message_data, session=session)
                    if not db_response.get("success", False):
                        logger.error(
                            f"Database error: Failed to insert chat for chat_id: {chat_id}, user_id: {user_id}: {db_response.get('error')}"
                        )
                        return JSONResponse(
                            status_code=500,
                            content={
                                "success": False,
                                "error": "There was a problem saving your chat messages during login. Please try again.",
                            },
                        )

                    logger.info(
                        f"Temporary chat inserted successfully with ID: {chat_id} for user_id: {user_id} and user_name: {user_name} and email: {email}"
                    )

                    # update user_id in chat in redis
                    redis_response = await redis_instance.delete_chat(chat_id=chat_id)
                    if not redis_response.get("success"):
                        logger.error(
                            f"Failed to delete chat from redis with ID: {chat_id} for user_id: {user_id}: {redis_response.get('error')}"
                        )
                        return JSONResponse(
                            status_code=500,
                            content={
                                "success": False,
                                "error": "We hit a snag cleaning up temporary data. Please attempt your login again.",
                            },
                        )

                    logger.info(
                        f"Temporary chat deleted successfully with ID: {chat_id} for user_id: {user_id} and user_name: {user_name} and email: {email}"
                    )

                    temp_chat_session = await redis_instance.redis_client.get(
                        f"temp_chat_session:{chat_id}"
                    )
                    if temp_chat_session:
                        await redis_instance.redis_client.delete(temp_chat_session)
                    await redis_instance.redis_client.delete(f"temp_chat_session:{chat_id}")
                    logger.info(f"Temporary chat session deleted successfully with ID: {chat_id}")

                else:
                    logger.warning(
                        f"Temporary chat with ID: {chat_id} not found in redis, temporary chat session expired for user_id: {user_id}"
                    )

            # Generate JWT tokens
            access_token = create_access_token(user_id)
            refresh_token = create_refresh_token(user_id)

            cloudwatch_data = {
                "event": "Login",
                "event_success": True,
                "timestamp": datetime.now(UTC).isoformat(),
            }
            try:
                asyncio.create_task(insert_cloudwatch_logs(data=cloudwatch_data, user_id=user_id))
            except Exception as e:
                logger.error(f"Error in inserting cloudwatch logs for user_id: {user_id}: {e}")

            logger.info(f"User logged in successfully with ID: {user_id}")
            return LoginResponse(
                success=True,
                token=access_token,
                refresh_token=refresh_token,
                user_id=user_id,
                user_name=user_name,
                t_c_verified=t_c_verified,
                onboarding_completed=onboarding_completed,
            )

        except ValueError as e:
            # This catches email validation errors from Pydantic
            logger.error(f"Validation error in login: {e}")
            return JSONResponse(
                status_code=422, content={"success": False, "error": "Invalid email format"}
            )
        except Exception as e:
            logger.error(f"Error in login: {e}")
            await session.rollback()
            return JSONResponse(
                status_code=500,
                content={
                    "success": False,
                    "error": "I'm having trouble verifying your details right now. Please wait a moment and try again.",
                },
            )


@router.post(
    "/logout",
    response_model=LogoutResponse,
    responses={
        401: {"model": ErrorResponse},
        422: {"model": ErrorResponse},
        500: {"model": ErrorResponse},
    },
)
async def logout(data: LogoutRequest):
    """
    User logout endpoint.
    Invalidates the user's token by clearing it from the database.
    """
    refresh_token = data.refresh_token

    # Validate empty user_id and token
    if not refresh_token:
        logger.error("Empty refresh_token received")
        return JSONResponse(
            status_code=422,
            content={
                "success": False,
                "error": "Something is missing in the logout request. Please try again.",
            },
        )

    async with async_session_scope() as session:
        try:
            # Clear the user's token
            user_id = verify_token(refresh_token, "refresh")
            if not user_id:
                logger.error("Invalid refresh token")
                return JSONResponse(
                    status_code=401,
                    content={
                        "success": False,
                        "error": "I'm having trouble confirming your session. A quick refresh should solve it.",
                    },
                )

            db_response = await check_user_by_id(user_id=user_id, session=session)
            if not db_response.get("success", False):
                logger.error(
                    f"Failed to logout user: {db_response.get('error')} for user_id: {user_id}"
                )
                return JSONResponse(
                    status_code=500,
                    content={
                        "success": False,
                        "error": "There was a system issue while logging you out. Please refresh the page to confirm.",
                    },
                )

            # Blacklist the refresh token so it cannot be reused after logout
            # blacklist_ttl = JWT_REFRESH_TOKEN_EXPIRE_DAYS * 86400  # days → seconds
            # try:
            #     await redis_instance.redis_client.setex(
            #         f"blacklisted_token:{refresh_token}",
            #         blacklist_ttl,
            #         1
            #     )
            #     logger.info(f"Refresh token blacklisted on logout for user_id: {user_id}")
            # except Exception as e:
            #     logger.error(f"Failed to blacklist refresh token for user_id: {user_id}: {e}")

            cloudwatch_data = {
                "event": "Logout",
                "event_success": True,
                "timestamp": datetime.now(UTC).isoformat(),
            }
            try:
                asyncio.create_task(insert_cloudwatch_logs(data=cloudwatch_data, user_id=user_id))
            except Exception as e:
                logger.error(f"Error in inserting cloudwatch logs for user_id: {user_id}: {e}")

            logger.info(f"User logged out successfully with ID: {user_id}")
            return LogoutResponse(success=True)

        except Exception as e:
            logger.error(f"Error in logout: {e}")
            await session.rollback()
            return JSONResponse(
                status_code=500,
                content={
                    "success": False,
                    "error": "There was a system issue while logging you out. Please refresh the page to confirm.",
                },
            )


@router.post(
    "/google-login",
    response_model=GoogleLoginResponse,
    responses={422: {"model": ErrorResponse}, 500: {"model": ErrorResponse}},
)
async def google_login(data: GoogleLoginRequest, chat_id: str | None = None):
    """Google login endpoint"""
    logger.info(f"Google login request received for chat_id: {chat_id}")

    # Rate limit: max 10 google-login attempts per id_token prefix per 15 minutes
    token_prefix = str(data.id_token)[:32] if data.id_token else "unknown"
    rate_key = f"rate_limit:google_login:{token_prefix}"
    try:
        attempts = await redis_instance.redis_client.incr(rate_key)
        if attempts == 1:
            await redis_instance.redis_client.expire(rate_key, 900)
        if attempts > 10:
            ttl = await redis_instance.redis_client.ttl(rate_key)
            logger.warning(f"Google login rate limit exceeded, attempts: {attempts}")
            return JSONResponse(
                status_code=429,
                content={
                    "success": False,
                    "error": f"Too many login attempts. Please try again in {ttl} seconds.",
                },
            )
    except Exception as e:
        logger.error(f"Rate limit check failed for google-login: {e}")

    google_login_time = datetime.now(UTC)
    login_or_signup = None
    # user_data_for_redis = {}
    async with async_session_scope() as session:
        try:
            # Generate random special character string for phone and phone_country_code
            special_chars = "!@#$%^&*()_+-=[]{}|;:,.<>?"
            random_phone = "".join(random.choice(special_chars) for _ in range(10))
            random_country_code = "".join(random.choice(special_chars) for _ in range(5))

            # Generate random password with special characters
            random_password = "".join(random.choice(special_chars) for _ in range(16))
            password_hash = generate_password_hash(random_password)

            id_token_str = str(data.id_token)
            logger.info(f"id_token_str: {id_token_str[:10]}...")
            # Validate input data
            if not id_token_str:
                logger.error("Empty id_token received")
                return JSONResponse(
                    status_code=422,
                    content={
                        "success": False,
                        "error": "Your request couldn't be understood. Please check your input and try again.",
                    },
                )

            # Validate that GOOGLE_CLIENT_ID is configured (critical security check)
            if not GOOGLE_CLIENT_ID:
                logger.error(
                    "GOOGLE_CLIENT_ID is not configured - Google login is disabled for security reasons"
                )
                return JSONResponse(
                    status_code=500,
                    content={
                        "success": False,
                        "error": "Google login is temporarily unavailable. We're on it. Please try again later.",
                    },
                )

            try:
                # Verify the ID token
                id_info = id_token.verify_oauth2_token(
                    id_token_str, google_requests.Request(), GOOGLE_CLIENT_ID
                )

                email = id_info.get("email")
                name = id_info.get("name")
                email_verified = id_info.get("email_verified", False)

                if not email or not name or not email_verified:
                    logger.error("Invalid Google ID token or email not verified")
                    return JSONResponse(
                        status_code=401,
                        content={
                            "success": False,
                            "error": "Invalid Google ID token or email not verified",
                        },
                    )
            except ValueError as e:
                if "Token expired" in str(e):
                    logger.error(f"Error verifying Google ID token: {e}")
                    return JSONResponse(
                        status_code=401,
                        content={
                            "success": False,
                            "error": "Your Google session has expired. Please sign back in to Google, then connect here.",
                        },
                    )
                logger.error(f"Error verifying Google ID token: {e}")
                return JSONResponse(
                    status_code=401, content={"success": False, "error": "Invalid Google ID token"}
                )
            except Exception as e:
                logger.error(f"Error verifying Google ID token: {e}")
                return JSONResponse(
                    status_code=401, content={"success": False, "error": "Invalid Google ID token"}
                )

            # Check if user already exists
            db_response = await check_user_exists_by_email(email=email, session=session)
            if not db_response.get("success", False):
                logger.error(
                    f"Database error: Failed to check if user exists: {db_response.get('error')}"
                )
                return JSONResponse(
                    status_code=500,
                    content={
                        "success": False,
                        "error": "We've hit a system error during the Google sign-in. Our team is aware. Please try again.",
                    },
                )

            user_id = db_response.get("user_id")
            user_name = db_response.get("user_name")
            t_c_verified = False  # Default value for new users
            onboarding_completed = False  # Default for new users

            if db_response.get("exists", False):
                login_or_signup = "login"
                t_c_verified = db_response.get("t_c_verified", False)
                onboarding_completed = db_response.get("onboarding_completed", False)
                if db_response.get("is_verified", False):
                    logger.info(
                        f"Verified user with email {email} logged in with user_id: {user_id}"
                    )
                else:
                    db_response = await update_user_verification_status(
                        email=email,
                        verified_at=google_login_time,
                        is_verified=True,
                        is_google_verified=True,
                        session=session,
                    )
                    if not db_response.get("success", False):
                        logger.error(
                            f"Database error: Failed to update user verification status: {db_response.get('error')} for user_id: {user_id}"
                        )
                        return JSONResponse(
                            status_code=500,
                            content={
                                "success": False,
                                "error": "I couldn't update your account's verification status. Please try that again.",
                            },
                        )
                    logger.info(
                        f"Unverified user with email {email} logged in with user_id: {user_id}"
                    )

            else:
                # Create new user with random phone and phone_country_code
                # user_id = str(uuid7())
                login_or_signup = "signup"
                user_name = name

                logger.info(f"Creating new Google verified user: {name}, {email}")

                # Create new user record
                new_user = {
                    # "id": user_id,
                    "user_name": name,
                    "email": email,
                    "phone": random_phone,
                    "phone_country_code": random_country_code,
                    "password_hash": password_hash,
                    "is_google_verified": True,
                    "created_at": google_login_time,
                    "is_verified": True,
                    "verified_at": google_login_time,
                }

                db_response = await create_user(user_data=new_user, session=session)
                if not db_response.get("success", False):
                    logger.error(
                        f"Database error: Failed to create user: {db_response.get('error')}"
                    )
                    return JSONResponse(
                        status_code=500,
                        content={
                            "success": False,
                            "error": "I ran into a system error while creating your account. We're on it. Please try again.",
                        },
                    )

                user_id = db_response.get("id")
                t_c_verified = False  # New users have t_c_verified as False by default
                logger.info(f"Google user created successfully with ID: {user_id}")

                # Initialize wallet with signup bonus tokens
                wallet_response = await WalletService.initialize_wallet(
                    user_id=user_id, session=session
                )
                if not wallet_response.get("success", False):
                    logger.error(
                        f"Failed to initialize wallet for Google user_id: {user_id}: {wallet_response.get('error')}"
                    )
                    # Rollback user creation if wallet creation fails
                    await session.rollback()
                    return JSONResponse(
                        status_code=500,
                        content={
                            "success": False,
                            "error": "I ran into a system error while creating your account. We're on it. Please try again.",
                        },
                    )

                # Commit user and wallet creation
                await session.commit()
                logger.info(f"Google user and wallet created successfully for user_id: {user_id}")
                # TODO: mail send here
                logger.info(
                    f"Wallet initialized for Google user_id: {user_id} with {wallet_response.get('tokens_credited', 0)} tokens"
                )

            # Temp user login
            if chat_id:
                logger.info(
                    f"Temporary user login with chat_id: {chat_id} and user_id: {user_id} and user_name: {user_name} and email: {email}"
                )

                redis_response = await redis_instance.check_chat_exists(chat_id=chat_id)
                if not redis_response.get("success"):
                    logger.error(
                        f"Failed to check if chat exists in redis with ID: {chat_id} for user_id: {user_id}: {redis_response.get('error')}"
                    )
                    return JSONResponse(
                        status_code=500,
                        content={
                            "success": False,
                            "error": "There was an issue accessing temporary chat data. Please try signing in again.",
                        },
                    )

                temp_chat_exists_in_redis = redis_response.get("exists", False)

                # when existing user logs in from temp chat without signing up first from temp chat
                if temp_chat_exists_in_redis:
                    redis_response = await redis_instance.get_chat(chat_id=chat_id)
                    if not redis_response.get("success"):
                        logger.error(
                            f"Failed to get chat from Redis for chat_id: {chat_id}, user_id: {user_id}: {redis_response.get('error')}"
                        )
                        return JSONResponse(
                            status_code=500,
                            content={
                                "success": False,
                                "error": "I couldn't retrieve the temporary chat data. Please try the Google sign-in again.",
                            },
                        )

                    message_data = {
                        "user_id": user_id,
                        "chat_id": chat_id,
                        "chat_messages": redis_response.get("chat_data", {}).get("chat_messages"),
                        "updated_at": redis_response.get("chat_data", {}).get("updated_at"),
                        "created_at": redis_response.get("chat_data", {}).get("created_at"),
                        "chat_title": redis_response.get("chat_data", {}).get("chat_title"),
                    }

                    db_response = await insert_chats(message_data=message_data, session=session)
                    if not db_response.get("success", False):
                        logger.error(
                            f"Database error: Failed to insert chat for chat_id: {chat_id}, user_id: {user_id}: {db_response.get('error')}"
                        )
                        return JSONResponse(
                            status_code=500,
                            content={
                                "success": False,
                                "error": "There was a problem saving chat messages. Please try signing in again.",
                            },
                        )

                    logger.info(
                        f"Temporary chat inserted successfully with ID: {chat_id} for user_id: {user_id} and user_name: {user_name} and email: {email}"
                    )

                    # update user_id in chat in redis
                    redis_response = await redis_instance.delete_chat(chat_id=chat_id)
                    if not redis_response.get("success"):
                        logger.error(
                            f"Failed to delete chat from redis with ID: {chat_id} for user_id: {user_id}: {redis_response.get('error')}"
                        )
                        return JSONResponse(
                            status_code=500,
                            content={
                                "success": False,
                                "error": "We've hit a system error during the Google sign-in. Our team is aware. Please try again.",
                            },
                        )
                    logger.info(
                        f"Temporary chat deleted successfully with ID: {chat_id} for user_id: {user_id} and user_name: {user_name} and email: {email}"
                    )

                    temp_chat_session = await redis_instance.redis_client.get(
                        f"temp_chat_session:{chat_id}"
                    )
                    if temp_chat_session:
                        await redis_instance.redis_client.delete(temp_chat_session)
                    await redis_instance.redis_client.delete(f"temp_chat_session:{chat_id}")
                    logger.info(f"Temporary chat session deleted successfully with ID: {chat_id}")

                else:
                    logger.warning(
                        f"Temporary chat with ID: {chat_id} not found in redis, temporary chat session expired for user_id: {user_id}"
                    )

            # Generate JWT tokens
            access_token = create_access_token(user_id)
            refresh_token = create_refresh_token(user_id)

            cloudwatch_data = {
                "event": f"Google {login_or_signup}",
                "event_success": True,
                "timestamp": datetime.now(UTC).isoformat(),
            }
            try:
                asyncio.create_task(insert_cloudwatch_logs(data=cloudwatch_data, user_id=user_id))
            except Exception as e:
                logger.error(f"Error in inserting cloudwatch logs for user_id: {user_id}: {e}")

            logger.info(f"User logged in successfully with ID: {user_id}")
            return GoogleLoginResponse(
                success=True,
                token=access_token,
                refresh_token=refresh_token,
                user_id=user_id,
                user_name=user_name,
                t_c_verified=t_c_verified,
                onboarding_completed=onboarding_completed,
            )

        except Exception as e:
            logger.error(f"Error in google login: {e}")
            await session.rollback()
            return JSONResponse(
                status_code=500,
                content={
                    "success": False,
                    "error": "We've hit a system error during the Google sign-in. Our team is aware. Please try again.",
                },
            )
