"""api.py: Authentication API for the Casper backend"""
import asyncio
import json
import os
import re
import shutil
import tempfile
from typing import AsyncGenerator, Dict, List, Optional, Union, Any
from uuid_utils import uuid7
import aiofiles
from fastapi import APIRouter, BackgroundTasks, HTTPException, Request, Depends, UploadFile, File, Form, Query
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, EmailStr, Field, ValidationError
from sqlalchemy.orm.attributes import flag_modified
import uvicorn
from datetime import datetime, timedelta, timezone
from jose import ExpiredSignatureError, jwt, JWTError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from src.config.constants import (
    FORGOT_PASSWORD_EMAIL_COOLDOWN_SECONDS,
    FORGOT_PASSWORD_EXPIRE_MINUTES,
    FRONTEND_RESET_PASSWORD_URL,
    FRONTEND_VERIFICATION_URL,
    GOOGLE_CLIENT_ID,
    JWT_ALGORITHM,
    JWT_SECRET_KEY,
    LIVE_SOURCES_COUNT,
    REPORT_GEN_MESSAGE,
    REQUEST_BCC_EMAILS,
    REQUEST_CC_EMAILS,
    REQUEST_SENDER_EMAIL,
    REQUEST_SENDER_EMAIL_PASS,
    REQUEST_TO_EMAILS,
    BOOK_CALL_SENDER_EMAIL,
    BOOK_CALL_SENDER_EMAIL_PASS,
    BOOK_CALL_TO_EMAILS,
    BOOK_CALL_BCC_EMAILS,
    S3_REPORTS_BASE_PATH,
    SIGNUP_VERIFICATION_EMAIL_COOLDOWN_SECONDS,
    SIGNUP_VERIFICATION_EXPIRE_MINUTES,
    SUBSCRIBER_BCC_EMAILS,
    SUBSCRIBER_CC_EMAILS,
    SUBSCRIBER_SENDER_EMAIL,
    SUBSCRIBER_SENDER_EMAIL_PASS,
    SUBSCRIBER_TO_EMAILS,
    JWT_REFRESH_TOKEN_EXPIRE_DAYS,
    _VALID_DOMAIN_SLUGS,
    MAX_REPORT_VERSIONS_FREE,
    MAX_REPORT_VERSIONS_PAID,
)
from google.oauth2 import id_token
from google.auth.transport import requests as google_requests
import random


from src.core.integrations.email_utils import send_report_notification_email, send_password_reset_email, send_signup_verification_email, send_subscribe_confirmation_email, send_request_confirmation_email, send_book_call_email
from src.core.observability.cloudwatch_utils import insert_cloudwatch_logs, insert_cloudwatch_logs_for_caspr_page
from src.core.integrations.openai_file_utils import ensure_report_file_id
from src.core.grep_agent_2 import get_or_create_grep_session
from src.db.grep_db import get_chat_uploaded_file_ids
from src.core.observability.functionality_context import Functionality, set_functionality
from src.core.observability.web_search_analytics import (
    enrich_terminal_search_analytics,
    log_scheduled_analytics_batch,
    search_analytics_schedule_kwargs,
)
from src.db.web_search_db import log_web_search_event
from src.core.report_util.pptx_utils import (
    content_slides_for_report,
    content_slides_for_report_length,
    generate_markdown_from_report,
    generate_pptx_and_upload_s3,
    generate_pptx_deck_local,
    generate_pptx_markdown_from_report,
)
from src.core.integrations.redis_utils import get_redis_instance
from src.core.common.utils import extract_content, generate_password_hash, verify_password
from src.db.db_utils import async_session_scope
from src.config.log_helper import setup_logging
from src.core.cards.card_utils import generate_section_summary
from src.core.infographics.one_pager import generate_one_pager
from src.db.async_db_functions import (
    check_user_by_id,#redis
    check_user_exists_by_email,
    check_user_exists_by_email_or_phone,
    create_report,
    create_new_report_version,
    create_request,
    create_call_booking,
    create_subscriber,
    create_user,
    delete_card_or_subsection,#redis
    delete_visualization,
    finalize_report_version,
    detect_modified_cards,
    get_all_version_outputs_list,
    get_cards_for_version,
    get_chat_reports,
    get_chat_reports_and_cards,
    get_report_info_by_version,
    get_report_version_history_data,
    update_user_tc_verified,
    get_user_walkover_status,
    mark_walkover_completed,
    get_report_cards,
    get_report_details,
    get_report_in_cards_format,
    get_table_id_markdown_map,
    get_user_chats,
    get_version_file_for_download,
    insert_card,
    insert_publish_details,
    mark_chat_deleted,
    rename_chat,
    update_report,
    update_report_status_by_chat_or_report_id,
    update_specific_version_s3_uri,
    update_report_status_if_needed,
    verify_user_credentials,#redis
    get_user_details,#redis
    get_user_chat,#redis
    insert_chats,#redis
    get_file_s3_path,
    update_user_password,
    update_user_verification_status,
    insert_table,
    revert_card_to_version,
    get_refinement_history,
    insert_ask_caspr_chat_entry,
    verify_report_ownership
)
from src.core.refine.refine_persist import THREAD_POLICY_RESET, persist_refined_card
from src.db.database import Card, ChatFile, Message, Report, Table, ReportVersion
from src.db.enums import ReportStatus, RefinementType, SubscriptionTier, FileUsageType, FileUploadContext
from src.db.wallet_functions import get_active_subscription
from src.services.wallet_service import WalletService
from sqlalchemy import select, and_, or_
from sqlalchemy.ext.asyncio import AsyncSession
from src.core.refine.refiner import refine_card
from src.core.cards.remove import extract_toc_after_delete
from src.core.refine.visualizer.viz_refine import refine_viz
from src.db.db_utils import async_session_scope
from src.core.report_util.executive_summary_updater import update_executive_summary
from src.core.agent.model import Casper
from src.core.report_util.entry_point import process_report_cards, generate_report_output
from src.core.integrations.s3_utils import (
    get_s3_instance,
    build_report_s3_prefix,
    upload_initial_markdown_to_s3,
    replace_visualization_uris_in_reports,
    replace_visualization_uris_in_card,
    get_or_create_presigned_url,
    extract_s3_key,
)
from src.resources.schemas.chat import (
    BatchPresignedUrlRequest,
    BatchPresignedUrlResponse,
    CreateSessionRequest,
    CreateTempSessionRequest,
    DeleteChatRequest,
    DeleteChatResponse,
    DownloadPresentationResponse,
    GeneratePresentationRequest,
    GeneratePresentationResponse,
    GenerateReportRequest,
    GenerateReportResponse,
    GenerateInfographicRequest,
    GenerateInfographicResponse,
    RefineOrDeleteRequest,
    RefineOrDeleteResponse,
    RefineVisualizationRequest,
    RefineVisualizationResponse,
    RegenerateReportRequest,
    RegenerateReportResponse,
    RegenerateESRequest,
    RegenerateESResponse,
    RenameChatRequest,
    ReportInfoResponse,
    ReportVersionHistoryResponse,
    RevertCardRequest,
    RevertCardResponse,
    RenameChatResponse,
    S3Paths,
    ChatRequest,
    ChatResponse,
    PreviousChatMessagesResponse,
    TempChatMessagesResponse,
    TempChatRequest,
    UserChatsResponse,
    ReportPresignedUrlResponse,
    ReportRequest,
    ReportResponse,
    UserLogsRequest,
    UserReportsResponse,
    VersionDownloadResponse,
    ListVersionOutputsResponse
)
from src.resources.schemas.auth import ErrorResponse, GoogleLoginResponse, SendVerificationLinkRequest, SendVerificationLinkResponse, SignupRequest, SignupResponse, LoginRequest, LoginResponse, LogoutRequest, LogoutResponse, GoogleLoginRequest, ForgotPasswordRequest, ForgotPasswordResponse, VerifyResetTokenResponse, ResetPasswordRequest, ResetPasswordResponse, VerifyAccountTokenRequest, VerifyAccountTokenResponse, UpdateTCVerifiedRequest, UpdateTCVerifiedResponse, GetTCVerifiedResponse, GetUserTokensResponse, GetWalkoverStatusResponse, CompleteWalkoverResponse
from src.resources.schemas.subscription import LiveSourcesResponse, SubscribeRequest, SubscribeResponse, RequestRequest, RequestResponse, BookCallRequest, BookCallResponse
from src.resources.schemas.dashboard import (
    DashboardStatsResponse,
    DashboardInfoResponse,
    ReportDomainsResponse,
    DomainReportsResponse,
    CategoriesResponse,
    CategoryItem,
    HomeSearchResponse,
    HomeSearchItem,
    OngoingChatsResponse,
    OngoingChatItem,
)
from src.resources.exceptions import http_exception_handler, validation_exception_handler
from src.core.auth.token_auth import get_current_active_user, create_access_token, create_refresh_token, verify_token, create_reset_password_token, create_verification_token
from src.db.async_db_functions import (
    get_latest_card_version,
    get_dashboard_stats,
    get_report_domain_summary,
    get_reports_by_domain,
    list_all_categories,
    search_chats_and_reports,
    get_draft_chats_grouped,
    STANDARD_CATEGORY_SLUG,
)
# Configure logging
logger = setup_logging(__file__)

# Create router
router = APIRouter()

redis_instance = get_redis_instance()

# Exception handlers
# router.exception_handler(HTTPException)(http_exception_handler) TODO: Uncomment this
# router.exception_handler(RequestValidationError)(validation_exception_handler)

# API Endpoints
@router.post("/signup", response_model=SignupResponse, responses={422: {"model": ErrorResponse}, 500: {"model": ErrorResponse}})
async def signup(data: SignupRequest, chat_id: Optional[str] = None):
    """User signup endpoint"""
    # Rate limit: max 5 signup attempts per email per 15 minutes
    rate_key = f"rate_limit:signup:{data.email.strip().lower()}"
    try:
        attempts = await redis_instance.redis_client.incr(rate_key)
        if attempts == 1:
            await redis_instance.redis_client.expire(rate_key, 900)
        if attempts > 5:
            ttl = await redis_instance.redis_client.ttl(rate_key)
            logger.warning(f"Signup rate limit exceeded for email: {data.email.strip().lower()}, attempts: {attempts}")
            return JSONResponse(
                status_code=429,
                content={"success": False, "error": f"Too many signup attempts. Please try again in {ttl} seconds."}
            )
    except Exception as e:
        logger.error(f"Rate limit check failed for signup: {e}")

    async with async_session_scope() as session:
        try:
            signup_request_time = datetime.now(timezone.utc)
            name = data.name.strip()
            email = data.email.strip().lower()
            phone = data.phone.strip()
            phone_country_code = data.phone_country_code.strip()
            password = data.password
            
            # Validate input data
            if len(name) == 0:
                logger.error("Empty name received after trimming whitespace")
                return JSONResponse(
                    status_code=422,
                    content={"success": False, "error": "Name cannot be empty"}
                )
                
            if len(phone) == 0:
                logger.error("Empty phone received after trimming whitespace")
                return JSONResponse(
                    status_code=422,
                    content={"success": False, "error": "Phone number cannot be empty"}
                )
                
            if len(phone_country_code) == 0:
                logger.error("Empty phone_country_code received after trimming whitespace")
                return JSONResponse(
                    status_code=422,
                    content={"success": False, "error": "Phone country code cannot be empty"}
                )
                
            if len(password) == 0:
                logger.error("Empty password received after trimming whitespace")
                return JSONResponse(
                    status_code=422,
                    content={"success": False, "error": "Password cannot be empty"}
                )
            
            if len(email) == 0:
                logger.error("Empty email received after trimming whitespace")
                return JSONResponse(
                    status_code=422,
                    content={"success": False, "error": "Email cannot be empty"}
                )
            
            if len(password) > 50:
                logger.error(f"Password received is too long for phone: {phone}")
                return JSONResponse(
                    status_code=422,
                    content={"success": False, "error": "Password cannot be longer than 50 characters"}
                )
            
            if len(name) > 50:
                logger.error(f"Name received is too long for phone: {phone}")
                return JSONResponse(
                    status_code=422,
                    content={"success": False, "error": "Name cannot be longer than 50 characters"}
                )
            
            if len(email) > 50:
                logger.error(f"Email received is too long for phone: {phone}")
                return JSONResponse(
                    status_code=422,
                    content={"success": False, "error": "Email cannot be longer than 50 characters"}
                )
            
            
            # Check if user already exists
            db_response = await check_user_exists_by_email_or_phone(email=email, phone=phone, session=session)
            if not db_response.get('success', False):
                logger.error(f"Database error: Failed to check if user exists: {db_response.get('error')}")
                return JSONResponse(
                    status_code=500,
                    content={"success": False, "error": "Something went wrong on our end while creating your account. We're looking into it."}
                )
            
            if db_response.get('exists', False):
                logger.warning(f"User with email {email} or phone {phone} already exists")
                return JSONResponse(
                    status_code=422,
                    content={"success": False, "error": "User with this email or phone already exists"}
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
                "verified_at": None
            }
            
            db_response = await create_user(user_data=new_user, session=session)
            if not db_response.get('success', False):
                logger.error(f"Database error: Failed to create user: {db_response.get('error')}")
                return JSONResponse(
                    status_code=500,
                    content={"success": False, "error": "Something went wrong on our end while creating your account. We're looking into it."}
                )
            user_id = db_response.get('id')
            logger.info(f"User created successfully with ID: {user_id}")

            # Process referral code if provided
            referrer_id = None
            is_referral = False
            bonus_tokens = 0
            
            if data.referral_code:
                referral_code = data.referral_code.strip().upper()
                logger.info(f"[REFERRAL_SIGNUP_ATTEMPT] user_id={user_id}, code={referral_code}")
                
                from src.db.referral_functions import (
                    get_user_by_referral_code,
                    check_user_already_referred,
                    create_referral_record,
                    credit_referral_bonus
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
                bonus_reason="referral_bonus_received" if is_referral else None
            )
            
            if not wallet_response.get('success', False):
                logger.error(f"Failed to initialize wallet for user_id: {user_id}: {wallet_response.get('error')}")
                # Rollback user creation if wallet creation fails
                await session.rollback()
                return JSONResponse(
                    status_code=500,
                    content={"success": False, "error": "Something went wrong on our end while creating your account. We're looking into it."}
                )
            
            # If valid referral, credit referrer and create referral record
            if is_referral and referrer_id:
                from src.db.referral_functions import credit_referral_bonus, create_referral_record
                
                # Credit referrer with bonus tokens
                referrer_bonus_result = await credit_referral_bonus(
                    user_id=referrer_id,
                    tokens=25000,
                    reason="referral_bonus_given",
                    session=session
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
                    session=session
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
            
            total_tokens = wallet_response.get('tokens_credited', 0) + bonus_tokens
            logger.info(
                f"User and wallet created successfully for user_id: {user_id} "
                f"with {total_tokens} tokens (base: {wallet_response.get('tokens_credited', 0)}, "
                f"referral_bonus: {bonus_tokens})"
            )

            # Temp user signup
            if chat_id:
                logger.info(f"Temporary user signup with chat_id: {chat_id} and user_id: {user_id} and email: {email}")

                redis_response = await redis_instance.check_chat_exists(chat_id=chat_id)
                if not redis_response.get('success'):
                    logger.error(f"Failed to check if chat exists in redis with ID: {chat_id}: {redis_response.get('error')}")
                    return JSONResponse(
                        status_code=500,
                        content={"success": False, "error": "Something went wrong on our end while creating your account. We're looking into it."}
                    )

                temp_chat_exists_in_redis = redis_response.get('exists', False)

                if temp_chat_exists_in_redis:

                    redis_response = await redis_instance.get_chat(chat_id=chat_id)
                    if not redis_response.get('success'):
                        logger.error(f"Failed to get chat from Redis for chat_id: {chat_id}, user_id: {user_id}: {redis_response.get('error')}")
                        return JSONResponse(
                            status_code=500,
                            content={"success": False, "error": "I couldn't access the temporary chat data right now. Please try the signup process again."}
                        )
                    
                    message_data = {
                        'user_id': user_id,
                        'chat_id': chat_id,
                        'chat_messages': redis_response.get('chat_data', {}).get('chat_messages'),
                        'updated_at': redis_response.get('chat_data', {}).get('updated_at'),
                        'created_at': redis_response.get('chat_data', {}).get('created_at'),
                        'chat_title': redis_response.get('chat_data', {}).get('chat_title')
                    }

                    db_response = await insert_chats(message_data=message_data, session=session)
                    if not db_response.get('success', False):
                        logger.error(f"Database error: Failed to insert chat for chat_id: {chat_id}, user_id: {user_id}: {db_response.get('error')}")
                        return JSONResponse(
                            status_code=500,
                            content={"success": False, "error": "There was a problem saving your chat messages. Let's try that again."}
                        )
                    
                    logger.info(f"Temporary chat inserted successfully with ID: {chat_id} for user_id: {user_id} and user_name: {name} and email: {email}")

                    redis_response = await redis_instance.delete_chat(chat_id=chat_id)
                    if not redis_response.get('success'):
                        logger.error(f"Failed to delete chat from redis with ID: {chat_id} for user_id: {user_id}: {redis_response.get('error')}")
                        return JSONResponse(
                            status_code=500,
                            content={"success": False, "error": "We've hit a snag trying to clean up temporary data. Please try to sign up again."}
                        )
                    
                    logger.info(f"Temporary chat deleted successfully with ID: {chat_id} from redis")

                    temp_chat_session = await redis_instance.redis_client.get(f"temp_chat_session:{chat_id}")
                    if temp_chat_session:
                        await redis_instance.redis_client.delete(temp_chat_session)
                    await redis_instance.redis_client.delete(f"temp_chat_session:{chat_id}")
                    logger.info(f"Temporary chat session deleted successfully with ID: {chat_id}")

            else:
                logger.warning(f"Temporary chat with ID: {chat_id} not found in redis, temporary chat session expired for user_id: {user_id}")

            cloudwatch_data = {
                "event": "Signup",
                "event_success": True,
                "timestamp": datetime.now(timezone.utc).isoformat()
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
                status_code=422,
                content={"success": False, "error": "Invalid email format"}
            )
        except Exception as e:
            logger.error(f"Error in signup: {e}")
            await session.rollback()
            return JSONResponse(
                status_code=500,
                content={"success": False, "error": "Something went wrong on our end while creating your account. We're looking into it."}
            )

@router.post("/login", response_model=LoginResponse, responses={422: {"model": ErrorResponse}, 500: {"model": ErrorResponse}})
async def login(data: LoginRequest, chat_id: Optional[str] = None):
    """User login endpoint"""
    # Rate limit: max 10 login attempts per email per 15 minutes
    rate_key = f"rate_limit:login:{data.email.strip().lower()}"
    try:
        attempts = await redis_instance.redis_client.incr(rate_key)
        if attempts == 1:
            await redis_instance.redis_client.expire(rate_key, 900)
        if attempts > 10:
            ttl = await redis_instance.redis_client.ttl(rate_key)
            logger.warning(f"Login rate limit exceeded for email: {data.email.strip().lower()}, attempts: {attempts}")
            return JSONResponse(
                status_code=429,
                content={"success": False, "error": f"Too many login attempts. Please try again in {ttl} seconds."}
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
                    content={"success": False, "error": "The email address you entered is too long. Please check it and try again."}
                )
            
            if len(password) > 50:
                logger.error(f"Password received is too long: {password}")
                return JSONResponse(
                    status_code=422,
                    content={"success": False, "error": "The password you entered is too long. Please try again."}
                )


            # Check if user exists and credentials are valid
            db_response = await verify_user_credentials(email=email, password=password, session=session)
            
            if not db_response.get('success', False):
                logger.error(f"Database error: {db_response.get('error')}")
                return JSONResponse(
                    status_code=500,
                    content={"success": False, "error": "I'm having trouble verifying your details right now. Please wait a moment and try again."}
                )
            
            if not db_response.get('valid', False):
                logger.warning(f"Invalid login attempt for email {email}")
                if 'is_verified' in db_response and not db_response['is_verified']:
                    return JSONResponse(
                        status_code=401,
                        content={"success": False, "error": db_response.get('error', "Invalid email or password"), "is_verified": False}
                    )
                else:
                    return JSONResponse(
                        status_code=401,
                        content={"success": False, "error": db_response.get('error', "Invalid email or password")}
                    )
            
            user_id = db_response.get('id')
            user_name = db_response.get('user_name')
            is_verified = db_response.get('is_verified', False)
            t_c_verified = db_response.get('t_c_verified', False)
            onboarding_completed = db_response.get('onboarding_completed', False)

            # Temp user login
            if chat_id:                
                logger.info(f"Temporary user login with chat_id: {chat_id} and user_id: {user_id} and user_name: {user_name} and email: {email}")

                redis_response = await redis_instance.check_chat_exists(chat_id=chat_id)
                if not redis_response.get('success'):
                    logger.error(f"Failed to check if chat exists in redis with ID: {chat_id} for user_id: {user_id}: {redis_response.get('error')}")
                    return JSONResponse(
                        status_code=500,
                        content={"success": False, "error": "There was an issue accessing temporary chat data. Please try your login again."}
                    )
                
                temp_chat_exists_in_redis = redis_response.get('exists', False)
                
                # when existing user logs in from temp chat without signing up first from temp chat
                if temp_chat_exists_in_redis:

                    redis_response = await redis_instance.get_chat(chat_id=chat_id)
                    if not redis_response.get('success'):
                        logger.error(f"Failed to get chat from Redis for chat_id: {chat_id}, user_id: {user_id}: {redis_response.get('error')}")
                        return JSONResponse(
                            status_code=500,
                            content={"success": False, "error": "I'm unable to retrieve temporary chat data at the moment. Please try logging in again."}
                        )
                
                    message_data = {
                        'user_id': user_id,
                        'chat_id': chat_id,
                        'chat_messages': redis_response.get('chat_data', {}).get('chat_messages'),
                        'updated_at': redis_response.get('chat_data', {}).get('updated_at'),
                        'created_at': redis_response.get('chat_data', {}).get('created_at'),
                        'chat_title': redis_response.get('chat_data', {}).get('chat_title')
                    }
                
                    db_response = await insert_chats(message_data=message_data, session=session)
                    if not db_response.get('success', False):
                        logger.error(f"Database error: Failed to insert chat for chat_id: {chat_id}, user_id: {user_id}: {db_response.get('error')}")
                        return JSONResponse(
                            status_code=500,
                            content={"success": False, "error": "There was a problem saving your chat messages during login. Please try again."}
                        )
                
                    logger.info(f"Temporary chat inserted successfully with ID: {chat_id} for user_id: {user_id} and user_name: {user_name} and email: {email}")
                
                    # update user_id in chat in redis
                    redis_response = await redis_instance.delete_chat(chat_id=chat_id)
                    if not redis_response.get('success'):
                        logger.error(f"Failed to delete chat from redis with ID: {chat_id} for user_id: {user_id}: {redis_response.get('error')}")
                        return JSONResponse(
                            status_code=500,
                            content={"success": False, "error": "We hit a snag cleaning up temporary data. Please attempt your login again."}
                        )
                    
                    logger.info(f"Temporary chat deleted successfully with ID: {chat_id} for user_id: {user_id} and user_name: {user_name} and email: {email}")

                    temp_chat_session = await redis_instance.redis_client.get(f"temp_chat_session:{chat_id}")
                    if temp_chat_session:
                        await redis_instance.redis_client.delete(temp_chat_session)
                    await redis_instance.redis_client.delete(f"temp_chat_session:{chat_id}")
                    logger.info(f"Temporary chat session deleted successfully with ID: {chat_id}")

                else:
                    logger.warning(f"Temporary chat with ID: {chat_id} not found in redis, temporary chat session expired for user_id: {user_id}")
            
            # Generate JWT tokens
            access_token = create_access_token(user_id)
            refresh_token = create_refresh_token(user_id)

            cloudwatch_data = {
                "event": "Login",
                "event_success": True,
                "timestamp": datetime.now(timezone.utc).isoformat()
            }
            try:
                asyncio.create_task(insert_cloudwatch_logs(data=cloudwatch_data, user_id=user_id))
            except Exception as e:
                logger.error(f"Error in inserting cloudwatch logs for user_id: {user_id}: {e}")
            
            logger.info(f"User logged in successfully with ID: {user_id}")
            return LoginResponse(success=True, token=access_token, refresh_token=refresh_token, user_id=user_id, user_name=user_name, t_c_verified=t_c_verified, onboarding_completed=onboarding_completed)
        
        except ValueError as e:
            # This catches email validation errors from Pydantic
            logger.error(f"Validation error in login: {e}")
            return JSONResponse(
                status_code=422,
                content={"success": False, "error": "Invalid email format"}
            )
        except Exception as e:
            logger.error(f"Error in login: {e}")
            await session.rollback()
            return JSONResponse(
                status_code=500,
                content={"success": False, "error": "I'm having trouble verifying your details right now. Please wait a moment and try again."}
            )

@router.post("/logout", response_model=LogoutResponse, responses={401: {"model": ErrorResponse}, 422: {"model": ErrorResponse}, 500: {"model": ErrorResponse}})
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
            content={"success": False, "error": "Something is missing in the logout request. Please try again."}
        )
    
    async with async_session_scope() as session:
        try:
            # Clear the user's token
            user_id = verify_token(refresh_token, "refresh")     
            if not user_id:
                logger.error("Invalid refresh token")
                return JSONResponse(
                    status_code=401,
                    content={"success": False, "error": "I'm having trouble confirming your session. A quick refresh should solve it."}
                )
            
            db_response = await check_user_by_id(user_id=user_id, session=session)      
            if not db_response.get('success', False):
                logger.error(f"Failed to logout user: {db_response.get('error')} for user_id: {user_id}")
                return JSONResponse(
                    status_code=500,
                    content={"success": False, "error": "There was a system issue while logging you out. Please refresh the page to confirm."}
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
                "timestamp": datetime.now(timezone.utc).isoformat()
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
                content={"success": False, "error": "There was a system issue while logging you out. Please refresh the page to confirm."}
            )

@router.patch("/update-tc-verified", response_model=UpdateTCVerifiedResponse, responses={401: {"model": ErrorResponse}, 422: {"model": ErrorResponse}, 500: {"model": ErrorResponse}})
async def update_tc_verified(data: UpdateTCVerifiedRequest, user_id: str = Depends(get_current_active_user)):
    """
    Update user's Terms and Conditions verification status.
    This endpoint allows updating the t_c_verified field for a user.
    """
    logger.info(f"Update T&C verification request received for user_id: {user_id}")
    
    try:
        async with async_session_scope() as session:
            # Verify user exists
            db_response = await check_user_by_id(user_id=user_id, session=session)
            
            if not db_response.get('success', False):
                logger.error(f"Failed to check user: {db_response.get('error')}")
                return JSONResponse(
                    status_code=500,
                    content={"success": False, "error": "Something went wrong while updating your preferences. Please try again."}
                )
            
            if not db_response.get('exists', False):
                logger.error(f"User with ID {user_id} not found")
                return JSONResponse(
                    status_code=401,
                    content={"success": False, "error": "User not found or session expired"}
                )
            
            # Update t_c_verified status
            update_response = await update_user_tc_verified(
                user_id=user_id,
                t_c_verified=data.t_c_verified,
                session=session
            )
            
            if not update_response.get('success', False):
                logger.error(f"Failed to update T&C status: {update_response.get('error')}")
                return JSONResponse(
                    status_code=500,
                    content={"success": False, "error": "Something went wrong while updating your preferences. Please try again."}
                )
            
            # Log to CloudWatch
            cloudwatch_data = {
                "event": "T&C Verification Updated",
                "event_success": True,
                "t_c_verified": data.t_c_verified,
                "timestamp": datetime.now(timezone.utc).isoformat()
            }
            try:
                asyncio.create_task(insert_cloudwatch_logs(data=cloudwatch_data, user_id=user_id))
            except Exception as e:
                logger.error(f"Error in inserting cloudwatch logs for user_id: {user_id}: {e}")
            
            logger.info(f"T&C verification status updated successfully for user_id: {user_id} to {data.t_c_verified}")
            
            return UpdateTCVerifiedResponse(
                success=True,
                message="Terms and Conditions verification status updated successfully",
                t_c_verified=data.t_c_verified
            )
    
    except Exception as e:
        logger.error(f"Error in update_tc_verified: {e}")
        return JSONResponse(
            status_code=500,
            content={"success": False, "error": "Something went wrong while updating your preferences. Please try again."}
        )

@router.get("/get-tc-verified", response_model=GetTCVerifiedResponse, responses={401: {"model": ErrorResponse}, 500: {"model": ErrorResponse}})
async def get_tc_verified(user_id: str = Depends(get_current_active_user)):
    """
    Get user's Terms and Conditions verification status.
    Returns the current t_c_verified status from the users table.
    """
    logger.info(f"Get T&C verification request received for user_id: {user_id}")
    
    try:
        async with async_session_scope() as session:
            # Get user details
            db_response = await check_user_by_id(user_id=user_id, session=session)
            
            if not db_response.get('success', False):
                logger.error(f"Failed to check user: {db_response.get('error')}")
                return JSONResponse(
                    status_code=500,
                    content={"success": False, "error": "Something went wrong while retrieving your status. Please try again."}
                )
            
            if not db_response.get('exists', False):
                logger.error(f"User with ID {user_id} not found")
                return JSONResponse(
                    status_code=401,
                    content={"success": False, "error": "User not found or session expired"}
                )
            
            # Get t_c_verified status
            t_c_verified = db_response.get('t_c_verified', False)
            
            logger.info(f"T&C verification status retrieved successfully for user_id: {user_id}, status: {t_c_verified}")
            
            return GetTCVerifiedResponse(
                success=True,
                t_c_verified=t_c_verified
            )
    
    except Exception as e:
        logger.error(f"Error in get_tc_verified: {e}")
        return JSONResponse(
            status_code=500,
            content={"success": False, "error": "Something went wrong while retrieving your status. Please try again."}
        )


@router.get(
    "/get-walkover-status",
    response_model=GetWalkoverStatusResponse,
    responses={401: {"model": ErrorResponse}, 500: {"model": ErrorResponse}},
)
async def get_walkover_status(user_id: str = Depends(get_current_active_user)):
    """Return whether the authenticated user has finished the in-app walkover.

    Defaults to ``False`` from account creation and stays that way until the
    frontend calls ``POST /complete-walkover`` after the user finishes the
    walkthrough. Frontend should call this on every authenticated session and
    show the walkover whenever it is ``False``.
    """
    logger.info(f"Get walkover-status request received for user_id: {user_id}")

    try:
        async with async_session_scope() as session:
            db_response = await get_user_walkover_status(user_id=user_id, session=session)

            if not db_response.get("success", False):
                logger.error(
                    f"Failed to fetch walkover_completed for user_id: {user_id}: "
                    f"{db_response.get('error')}"
                )
                return JSONResponse(
                    status_code=500,
                    content={
                        "success": False,
                        "error": "Something went wrong while retrieving your status. Please try again.",
                    },
                )

            if not db_response.get("exists", False):
                logger.warning(f"User with ID {user_id} not found while reading walkover_completed")
                return JSONResponse(
                    status_code=401,
                    content={"success": False, "error": "User not found or session expired"},
                )

            walkover_completed = bool(db_response.get("walkover_completed", False))
            logger.info(
                f"walkover_completed retrieved successfully for user_id: {user_id}, "
                f"walkover_completed: {walkover_completed}"
            )
            return GetWalkoverStatusResponse(success=True, walkover_completed=walkover_completed)

    except Exception as e:
        logger.error(f"Error in get_walkover_status: {e}")
        return JSONResponse(
            status_code=500,
            content={
                "success": False,
                "error": "Something went wrong while retrieving your status. Please try again.",
            },
        )


@router.post(
    "/complete-walkover",
    response_model=CompleteWalkoverResponse,
    responses={401: {"model": ErrorResponse}, 500: {"model": ErrorResponse}},
)
async def complete_walkover(user_id: str = Depends(get_current_active_user)):
    """Mark the authenticated user's walkover as completed.

    Idempotent — calling this multiple times is safe; the flag will simply
    stay ``True``. The frontend should hit this once the user finishes (or
    explicitly skips) the in-app walkthrough.
    """
    logger.info(f"Complete walkover request received for user_id: {user_id}")

    try:
        async with async_session_scope() as session:
            db_response = await mark_walkover_completed(user_id=user_id, session=session)

            if not db_response.get("success", False):
                error = db_response.get("error", "")
                if error == "User not found":
                    logger.warning(
                        f"User with ID {user_id} not found while marking walkover_completed"
                    )
                    return JSONResponse(
                        status_code=401,
                        content={"success": False, "error": "User not found or session expired"},
                    )
                logger.error(
                    f"Failed to mark walkover_completed for user_id: {user_id}: {error}"
                )
                return JSONResponse(
                    status_code=500,
                    content={
                        "success": False,
                        "error": "Something went wrong while updating your status. Please try again.",
                    },
                )

            logger.info(f"walkover_completed marked True for user_id: {user_id}")
            return CompleteWalkoverResponse(
                success=True,
                message="Walkover marked as completed.",
                walkover_completed=True,
            )

    except Exception as e:
        logger.error(f"Error in complete_walkover: {e}")
        return JSONResponse(
            status_code=500,
            content={
                "success": False,
                "error": "Something went wrong while updating your status. Please try again.",
            },
        )


# @router.get("/get-signup-token", response_model=GetUserTokensResponse, responses={401: {"model": ErrorResponse}, 500: {"model": ErrorResponse}})
# async def get_user_tokens(user_id: str):
#     """
#     Get user tokens by user_id.
#     Returns user_name, access_token (token), and refresh_token for the given user_id.
#     """
#     logger.info(f"Get user tokens request received for user_id: {user_id}")
    
#     try:
#         async with async_session_scope() as session:
#             # Get user details
#             db_response = await check_user_by_id(user_id=user_id, session=session)
            
#             if not db_response.get('success', False):
#                 logger.error(f"Failed to check user: {db_response.get('error')}")
#                 return JSONResponse(
#                     status_code=500,
#                     content={"success": False, "error": "Something went wrong while retrieving user details. Please try again."}
#                 )
            
#             if not db_response.get('exists', False):
#                 logger.error(f"User with ID {user_id} not found")
#                 return JSONResponse(
#                     status_code=401,
#                     content={"success": False, "error": "User not found"}
#                 )
            
#             # Get user details
#             user_name = db_response.get('user_name')
#             t_c_verified = db_response.get('t_c_verified', False)
            
#             # Generate JWT tokens
#             access_token = create_access_token(user_id)
#             refresh_token = create_refresh_token(user_id)
            
#             logger.info(f"User tokens generated successfully for user_id: {user_id}")
            
#             return GetUserTokensResponse(
#                 success=True,
#                 user_id=user_id,
#                 user_name=user_name,
#                 token=access_token,
#                 refresh_token=refresh_token,
#                 t_c_verified=t_c_verified
#             )
    
#     except Exception as e:
#         logger.error(f"Error in get_user_tokens: {e}")
#         return JSONResponse(
#             status_code=500,
#             content={"success": False, "error": "Something went wrong while generating tokens. Please try again."}
#         )

@router.post("/subscribe",
    response_model=SubscribeResponse,
    status_code=200,
    summary="Subscribe to newsletter",
    description="Subscribe to newsletter with email address"
)
async def subscribe(data: SubscribeRequest):
    try:
        logger.info(f"Received subscription request for email: {data.email_id}")

        if not data.email_id:
            logger.error(f"Email address is required for subscription!")
            return JSONResponse(content={
                "success": False,
                "message": "Please provide a valid email address to subscribe."
            }, status_code=400)

        # Insert subscriber into database
        async with async_session_scope() as session:
            db_response = await create_subscriber(email=data.email_id, session=session)
        
        if not db_response.get("success"):
            logger.error(f"Failed to insert subscriber into database: {db_response.get('error')}")
            return JSONResponse(content={
                "success": False,
                "message": "We encountered an issue while processing your subscription."
            }, status_code=500)

        logger.info(f"Successfully inserted subscriber with ID: {db_response['data']['id']} for email: {data.email_id}")

        # Send subscription confirmation email
        email_sent = await run_in_threadpool(
            send_subscribe_confirmation_email,
            subscriber_email=data.email_id,
            sender_email=SUBSCRIBER_SENDER_EMAIL,
            sender_password=SUBSCRIBER_SENDER_EMAIL_PASS,
            to_emails=SUBSCRIBER_TO_EMAILS,
            cc_emails=SUBSCRIBER_CC_EMAILS, 
            bcc_emails=SUBSCRIBER_BCC_EMAILS
        )
        
        # Use database timestamp for CloudWatch logs
        db_timestamp = db_response['data']['created_at'].isoformat()
        
        try:
            cloudwatch_request_data = {
                "event": "Subscribe",
                "email": data.email_id,
                "subscriber_id": db_response['data']['id'],
                "timestamp": db_timestamp
            }
            asyncio.create_task(insert_cloudwatch_logs_for_caspr_page(request_data=cloudwatch_request_data))
        except Exception as e:
            logger.error(f"Error inserting cloudwatch logs: {str(e)} for {data.email_id}")

        if email_sent:
            logger.info(f"Subscription email sent successfully to {data.email_id}")    
        else:
            logger.error(f"Failed to send subscription email to {data.email_id}")
        
        return SubscribeResponse(
            success=True, 
            message=f"Successfully subscribed!"
        )
            
    except Exception as e:
        logger.error(f"Error processing subscription: {str(e)} for {data.email_id}")
        return JSONResponse(content={
            "success": False,
            "message": "We encountered an issue while processing your subscription."
        }, status_code=500)

@router.post("/request",
    response_model=RequestResponse,
    status_code=200,
    summary="Submit a request for Caspr.",
    description="Submit a request for Caspr. with client details"
)
async def submit_request(data: RequestRequest):
    try:
        logger.info(f"Received request from {data.name} ({data.email})")

        if not data.name:
            logger.error(f"Name is required for request!")
            return JSONResponse(content={
                "success": False,
                "message": "Please provide your name."
            }, status_code=400)
        
        if not data.email:
            logger.error(f"Email address is required for request!")
            return JSONResponse(content={
                "success": False,
                "message": "Please provide a valid email address."
            }, status_code=400)

        # Insert request into database
        async with async_session_scope() as session:
            db_response = await create_request(
                name=data.name,
                email=data.email,
                website=data.website,
                description=data.description,
                session=session
            )
        
        if not db_response.get("success"):
            logger.error(f"Failed to insert request into database: {db_response.get('error')}")
            return JSONResponse(content={
                "success": False,
                "message": "We encountered an issue while processing your request."
            }, status_code=500)

        logger.info(f"Successfully inserted request with ID: {db_response['data']['id']} for {data.name} ({data.email})")

        # Send request confirmation email
        email_sent = await run_in_threadpool(
            send_request_confirmation_email,
            client_email=data.email,
            client_name=data.name,
            client_website=data.website,
            description=data.description,
            sender_email=REQUEST_SENDER_EMAIL,
            sender_password=REQUEST_SENDER_EMAIL_PASS,
            to_emails=REQUEST_TO_EMAILS,
            cc_emails=[data.email],
            bcc_emails=REQUEST_BCC_EMAILS
        )
        
        # Use database timestamp for CloudWatch logs
        db_timestamp = db_response['data']['created_at'].isoformat()
        
        try:
            cloudwatch_request_data = {
                "event": "Request",
                "name": data.name,
                "email": data.email,
                "website": data.website,
                "description": data.description,
                "request_id": db_response['data']['id'],
                "timestamp": db_timestamp
            }
            asyncio.create_task(insert_cloudwatch_logs_for_caspr_page(request_data=cloudwatch_request_data))
        except Exception as e:
            logger.error(f"Error inserting cloudwatch logs: {str(e)} for {data.email}")

        if email_sent:
            logger.info(f"Request confirmation email sent successfully for {data.name} ({data.email})")    
        else:
            logger.error(f"Failed to send request confirmation email for {data.name} ({data.email})")
        
        return RequestResponse(
            success=True, 
            message=f"Request submitted successfully!"
        )
            
    except Exception as e:
        logger.error(f"Error processing request: {str(e)} for {data.name} ({data.email})")
        return JSONResponse(content={
            "success": False,
            "message": "We encountered an issue while processing your request."
        }, status_code=500)


@router.post(
    "/book-call",
    status_code=200,
    response_model=BookCallResponse,
    summary="Book a call",
    description="Submit a call booking request with name, email, phone and optional brief",
)
async def book_call(data: BookCallRequest):
    try:
        logger.info(f"Received call booking from {data.name} ({data.email})")

        if not data.name or not data.name.strip():
            return JSONResponse(
                content={"success": False, "message": "Please provide your name."},
                status_code=400,
            )
        if not data.email:
            return JSONResponse(
                content={"success": False, "message": "Please provide a valid email address."},
                status_code=400,
            )
        if not data.phone or not data.phone.country_code or not data.phone.number:
            return JSONResponse(
                content={"success": False, "message": "Please provide your phone number (country code and number)."},
                status_code=400,
            )

        # Rate limit: 1 request per 5 minutes per email (Redis)
        rate_key = f"book_call_rate:{data.email.strip().lower()}"
        try:
            n = await redis_instance.redis_client.incr(rate_key)
            if n == 1:
                await redis_instance.redis_client.expire(rate_key, 300)
            if n > 1:
                ttl = await redis_instance.redis_client.ttl(rate_key)
                return JSONResponse(
                    content={
                        "success": False,
                        "message": f"Please wait {ttl} seconds before submitting another request.",
                    },
                    status_code=429,
                )
        except Exception as e:
            logger.error(f"Rate limit check failed for book-call: {e}")

        async with async_session_scope() as session:
            db_response = await create_call_booking(
                name=data.name.strip(),
                email=data.email.strip().lower(),
                phone_country_code=data.phone.country_code.strip(),
                phone_number=data.phone.number.strip(),
                brief=data.brief.strip() if data.brief and data.brief.strip() else None,
                session=session,
            )

        if not db_response.get("success"):
            logger.error(f"Failed to insert call booking: {db_response.get('error')}")
            return JSONResponse(
                content={"success": False, "message": "We encountered an issue while processing your request."},
                status_code=500,
            )

        logger.info(f"Successfully inserted call booking with ID: {db_response['data']['id']} for {data.name} ({data.email})")

        if BOOK_CALL_TO_EMAILS:
            email_sent = await run_in_threadpool(
                send_book_call_email,
                name=data.name.strip(),
                email=data.email.strip().lower(),
                phone_country_code=data.phone.country_code.strip(),
                phone_number=data.phone.number.strip(),
                brief=data.brief.strip() if data.brief and data.brief.strip() else None,
                sender_email=BOOK_CALL_SENDER_EMAIL,
                sender_password=BOOK_CALL_SENDER_EMAIL_PASS,
                to_emails=BOOK_CALL_TO_EMAILS,
                cc_emails=[data.email.strip().lower()],
                bcc_emails=BOOK_CALL_BCC_EMAILS,
            )
            if email_sent:
                logger.info(f"Call booking email sent for {data.name} ({data.email})")
            else:
                logger.error(f"Failed to send call booking email for {data.name} ({data.email})")

        return BookCallResponse(success=True, message="Call booking submitted successfully. We will be in touch shortly.")

    except Exception as e:
        logger.error(f"Error processing call booking: {str(e)}")
        return JSONResponse(
            content={"success": False, "message": "We encountered an issue while processing your request."},
            status_code=500,
        )


@router.get("/live-sources",
    response_model=LiveSourcesResponse,
    status_code=200,
    summary="Get live sources count",
    description="Get the current count of live sources"
)
async def live_sources():
    try:
        logger.info("Received request to get live sources count")
        
        return LiveSourcesResponse(success=True, count=LIVE_SOURCES_COUNT)
    except Exception as e:
        logger.error(f"Error getting live sources count: {str(e)}")
        return JSONResponse(content={
            "success": False,
            "message": "We're having trouble loading the live sources information."
        }, status_code=500)


_CHAT_SESSION_TTL = timedelta(hours=3)


async def _try_reuse_chat_session(chat_id: str) -> Optional[str]:
    """Return an existing session_id when Redis still has a live stream for this chat."""
    chat_key = f"chat_session:{chat_id}"
    existing_stream = await redis_instance.redis_client.get(chat_key)
    if not existing_stream or not isinstance(existing_stream, str):
        return None
    if not existing_stream.startswith("session:"):
        return None
    if not await redis_instance.redis_client.exists(existing_stream):
        return None

    session_id = existing_stream.removeprefix("session:")
    await redis_instance.redis_client.expire(chat_key, _CHAT_SESSION_TTL)
    await redis_instance.redis_client.expire(existing_stream, _CHAT_SESSION_TTL)
    return session_id


@router.post(
    "/create-session",
    status_code=200,
    response_class=JSONResponse,
    summary="Create a new session",
    description="Create a new session"
)
async def create_session(data: CreateSessionRequest, user_id: str = Depends(get_current_active_user)):
    """Create a new session"""
    logger.info(f"Recieved request to create new session for user_id: {user_id} and chat_id: {data.chat_id}")
    try:
        async with async_session_scope() as session:
            db_response = await check_user_by_id(user_id=user_id, session=session)
            if not db_response.get('success', False):
                logger.error(f"Failed to check user by id: {db_response.get('error')}")
                return JSONResponse(
                    status_code=500,
                    content={"success": False, "error": "I couldn't start a new chat session due to a system error. Please try again."}
                )
            if not db_response.get('exists', False):
                logger.error(f"User with ID {user_id} not found or token is invalid")
                return JSONResponse(
                    status_code=401,
                    content={"success": False, "error": "I can't seem to find an account with those details. Could you check them and try again?"}
                )
            
            # Validate chat_id ownership if provided
            if data.chat_id:
                logger.info(f"Validating ownership of chat_id: {data.chat_id} for user_id: {user_id}")
                
                # Check if chat_id exists in database (regardless of owner)
                stmt = select(Message).where(Message.id == data.chat_id)
                result = await session.execute(stmt)
                existing_chat = result.scalar_one_or_none()
                
                if existing_chat:
                    # Chat exists - verify ownership
                    if existing_chat.user_id != user_id:
                        logger.error(f"User {user_id} attempted to create session for chat_id {data.chat_id} owned by user {existing_chat.user_id}")
                        return JSONResponse(
                            status_code=403,
                            content={"success": False, "error": "You don't have permission to access this chat."}
                        )
                    logger.info(f"Chat ownership validated successfully for chat_id: {data.chat_id} and user_id: {user_id}")
                else:
                    # Chat doesn't exist yet - this is fine, will be created later
                    logger.info(f"Chat {data.chat_id} does not exist yet, will be created for user_id: {user_id}")
        
        chat_id = data.chat_id if data.chat_id else str(uuid7())
        if data.chat_id:
            reused_session_id = await _try_reuse_chat_session(chat_id)
            if reused_session_id:
                logger.info(
                    f"Reusing existing session for user_id: {user_id} "
                    f"chat_id: {chat_id} session_id: {reused_session_id}"
                )
                return {"session_id": reused_session_id, "chat_id": chat_id}

        logger.info(f"Creating new session for user_id: {user_id} and chat_id: {data.chat_id}")
        session_id = str(uuid7())
        stream_key = f"session:{session_id}"
        await redis_instance.redis_client.set(
            f"chat_session:{chat_id}", stream_key, ex=_CHAT_SESSION_TTL
        )
        event_data = {
            "event": "message",
            "data": json.dumps({
                "type": "session_created"
            })
        }
        await redis_instance.redis_client.xadd(stream_key, event_data)
        await redis_instance.redis_client.expire(stream_key, _CHAT_SESSION_TTL)
        logger.info(f"Session created successfully for user_id: {user_id} and chat_id: {chat_id}")
        return {"session_id": session_id, "chat_id": chat_id}
    
    except Exception as e:
        logger.error(f"Error in create_session: {e}")
        return JSONResponse(
            status_code=500,
            content={"success": False, "error": "I couldn't start a new chat session due to a system error. Please try again."}
        )    

@router.get(
    "/chat-stream/{session_id}",
    status_code=200,
    response_class=StreamingResponse,
    summary="Get chat events from redis stream",
    description="Get chat events from redis stream"
)
async def chat_stream(request: Request, session_id: str, chat_id: str, event_id: str = "0"):
    """Get chat events from redis stream"""
    logger.info(f"[SSE] Chat connection establishment request for session_id: {session_id} and chat_id: {chat_id}")
    # Check if session id is valid
    stream_key = f"session:{session_id}"

    try:
        redis_session_id = await redis_instance.redis_client.get(f"chat_session:{chat_id}")
        if redis_session_id != stream_key:
            logger.error(f"[SSE] Invalid session_id: {session_id} for chat_id: {chat_id}")
            return JSONResponse(
                status_code=401,
                content={"success": False, "error": "That chat session has ended. Please start a new one."}
            )
        
        # if not await redis_instance.redis_client.exists(stream_key):
        #     logger.error(f"[SSE] Session expired for chat_id: {chat_id} and session_id: {session_id}")
        #     return JSONResponse(
        #         status_code=422,
        #         content={"success": False, "error": "Session expired!"}
        #     )

        timeout = timedelta(hours=3)
        # timeout = timedelta(seconds=30) 
        start_time = datetime.now(timezone.utc)
        last_seen_event_id = event_id

        async def event_generator():
            nonlocal start_time, last_seen_event_id
            events_yielded_yet = False
            try:
                while not await request.is_disconnected():
                    try:
                        response = await redis_instance.redis_client.xread({stream_key: last_seen_event_id}, block=2000)
                        if response:
                            _, entries = response[0]
                            for entry_id, fields in entries:
                                
                                last_seen_event_id = entry_id 
                                event = fields.get("event", "null")
                                data = json.loads(fields.get("data", "{}"))

                                yield f"event: {event}\ndata: {json.dumps({'event_id': entry_id, **data})}\n\n"
                                if not events_yielded_yet:
                                    logger.info(f"[SSE] Started streaming for chat_id: {chat_id} and session_id: {session_id}")
                                    events_yielded_yet = True
                                
                                await redis_instance.redis_client.expire(stream_key, timedelta(hours=3))
                                await redis_instance.redis_client.expire(f"chat_session:{chat_id}", timedelta(hours=3))
                            
                            start_time = datetime.now(timezone.utc)
                        else:
                            # # sending a keep-alive comment every 15 seconds to prevent connection timeouts
                            # if (datetime.now(timezone.utc) - start_time).total_seconds() > 15:
                            #     yield ": keepalive\n\n"
                            #     start_time = datetime.now(timezone.utc)
                                
                            if datetime.now(timezone.utc) - start_time > timeout:
                                error_data = {"response": "Session timeout!"}
                                yield f"event: timeout\ndata: {json.dumps(error_data)}\n\n"
                                logger.warning(f"[SSE] Timeout for session {session_id} and chat_id: {chat_id}")
                                return
                    except Exception as e:
                        logger.error(f"[SSE] Error in event stream: {e} for chat_id: {chat_id} and session_id: {session_id}")
                        yield f"event: error\ndata: {json.dumps({'response': 'We are having some trouble with the connection. Please check your network.'})}\n\n"
                        return
                        
            except Exception as e:
                logger.error(f"[SSE] Unhandled error in event generator: {e} for chat_id: {chat_id} and session_id: {session_id}")
                yield f"event: error\ndata: {json.dumps({'response': 'We are having some trouble with the connection. Please check your network.'})}\n\n"
                return

        # Set proper SSE headers
        headers = {
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no"  # Disable buffering in Nginx
        }
        
        return StreamingResponse(
            event_generator(), 
            media_type="text/event-stream",
            headers=headers
        )
    except Exception as e:
        logger.error(f"[SSE] Error establishing chat stream: {e} for chat_id: {chat_id} and session_id: {session_id}")
        return JSONResponse(
            status_code=500,
            content={"success": False, "error": "We've encountered a connection error. Please refresh to try again."}
        )


async def refresh_session(stream_key: str, chat_id: str):
    try:
        logger.info(f"[SSE] Refreshing session for chat_id: {chat_id} and stream_key: {stream_key}")
        chat_key = f"chat_session:{chat_id}"

        # delete the session from redis
        await redis_instance.redis_client.delete(stream_key)

        # create a new session with same session_id
        chat_key_ttl = await redis_instance.redis_client.ttl(chat_key)
        if chat_key_ttl <= 0:
            await redis_instance.redis_client.set(chat_key, stream_key, ex=timedelta(hours=3))
            chat_key_ttl = timedelta(hours=3)

        event_data = {
            "event": "message",
            "data": json.dumps({
                "type": "session_created"
            })
        }
        await redis_instance.redis_client.xadd(stream_key, event_data)
        await redis_instance.redis_client.expire(stream_key, chat_key_ttl)
        logger.info(f"[SSE] Session refreshed for chat_id: {chat_id} and stream_key: {stream_key}")
        return True
    
    except Exception as e:
        logger.error(f"[SSE] Error refreshing session: {e} for chat_id: {chat_id} and stream_key: {stream_key}")
        return False

#: Custom events that carry the layout the model proposes during the feedback
#: phase — as opposed to the final one `retrieve` emits. The three
#: `proposed_report_layout_*` events stream that layout card by card while the
#: model is still writing it, so the preview fills in live; `propose_report_layout`
#: then carries the authoritative layout (title resolved, fully parsed) and
#: REPLACES whatever the stream built, and `updated_proposed_report_layout`
#: replaces it once more when the background web refresh lands.
LAYOUT_PROPOSAL_EVENTS = {
    "proposed_report_layout_start": "report_layout_proposal_start",
    "proposed_report_layout_card": "report_layout_proposal_card",
    "proposed_report_layout_end": "report_layout_proposal_end",
    "propose_report_layout": "report_layout_proposal",
    "updated_proposed_report_layout": "report_layout_proposal_updated",
}


def _layout_proposal_sse_payload(output: dict, report_id: str = "") -> dict:
    """Build the SSE payload for one layout-proposal event.

    A `proposal_id` ties a streamed preview to the layout it belongs to, so a
    revised proposal later in the conversation replaces the first one instead of
    being appended to it.
    """
    name = output.get("name", "")
    payload = {"type": LAYOUT_PROPOSAL_EVENTS[name], "report_id": report_id}
    if "proposal_id" in output:
        payload["proposal_id"] = output.get("proposal_id", "")
    if name == "proposed_report_layout_card":
        payload["index"] = output.get("index", 0)
        payload["data"] = output.get("card", {})
        return payload

    payload["report_title"] = output.get("report_title", "")
    if name == "proposed_report_layout_end":
        payload["cards"] = output.get("cards", 0)
        payload["aborted"] = output.get("aborted", False)
    if name in ("propose_report_layout", "updated_proposed_report_layout"):
        payload["data"] = output.get("report_layout", [])
    if name == "updated_proposed_report_layout":
        payload["change_summary"] = output.get("change_summary", "")
    return payload


async def chat_producer(data: ChatRequest, user_id: str):
    """Chat endpoint with streaming response"""
    logger.info(f"Processing chat request from user_id: {user_id} and chat_id: {data.chat_id}")
    stream_key = f"session:{data.session_id}"

    # Seed the error-digest context for this background task so any
    # logger.error() fired deep inside (model fallbacks, retries, etc.) gets
    # attributed to this user / chat in the digest email.  user_email is
    # filled in below once we load the user record.
    from src.core.observability.error_alerter import _init_request_context, set_alert_request_context
    _init_request_context()
    set_alert_request_context(
        method="BACKGROUND",
        path=f"chat_producer:/api/v1/chat (chat_id={data.chat_id})",
        user_id=user_id,
    )

    await redis_instance.redis_client.set(f"chat:{data.chat_id}:processing_user_message", json.dumps({"type": "human", "content": data.message}), ex=timedelta(hours=24))
    logger.info(f"Set user_message_processing key in redis for chat_id: {data.chat_id}")

    event_data = {
        "event": "message",
        "data": json.dumps({
            "type": "start_stream"
        })
    }
    await redis_instance.redis_client.xadd(stream_key, event_data)
    query_received_time = datetime.now(timezone.utc)  
    user_name, user_email = None, None
    user_previous_messages, chat_title, is_new_chat = [], None, False
    ref_id_to_fv_id: dict = {}  # {uploaded_file_id: file_version_id} — set when ensure runs for new chat

    # cloudwatch_data = {
    #     "event": "User input",
    #     "event_success": True,
    #     "timestamp": query_received_time.isoformat(),
    #     "chat_id": data.chat_id,
    #     "chat_title": chat_title
    # }
    # try:
    #     asyncio.create_task(insert_cloudwatch_logs(data=cloudwatch_data, user_id=user_id))
    # except Exception as e:
    #     logger.error(f"Error in inserting cloudwatch logs: {e} for user_id: {user_id} and chat_id: {data.chat_id}")
    report_id = None
    try:
        # 1. Validate user
        async with async_session_scope() as session:
            db_response = await check_user_by_id(user_id=user_id, session=session)
            if not db_response.get('success', False):
                logger.error(f"Failed to check user by id: {db_response.get('error')} for user_id: {user_id} and chat_id: {data.chat_id}")
                event_data = {
                    "event": "error",
                    "data": json.dumps({
                        # "response": "I couldn't process your request at this time. Please try again later."
                        "response": "I can't verify your session at the moment. Please try logging in again."
                    })
                }
                await redis_instance.redis_client.xadd(stream_key, event_data)
                return

            if not db_response.get('exists', False):
                logger.error(f"User with ID {user_id} not found or token is invalid for user_id: {user_id} and chat_id: {data.chat_id}")
                event_data = {
                    "event": "error",
                    "data": json.dumps({
                        # "response": "Your session has expired. Please log in again to continue."
                        "response": "I'm having trouble finding your account. Please log in again to continue.."
                    })
                }
                await redis_instance.redis_client.xadd(stream_key, event_data)
                return

            # 2. Get user details
            db_response = await get_user_details(user_id=user_id, session=session)
            if not db_response.get('success', False):
                logger.error(f"Database error: Failed to get user details: {db_response.get('error')} for user_id: {user_id} and chat_id: {data.chat_id}")
                event_data = {
                    "event": "error",
                    "data": json.dumps({
                        # "response": "I'm having trouble accessing your account information. Please try refreshing the page."
                        "response": "I couldn't retrieve your account information right now. Please refresh and try again."
                    })
                }
                await redis_instance.redis_client.xadd(stream_key, event_data)
                return

            user_name = db_response.get("user", {}).get('user_name', None)
            user_email = db_response.get("user", {}).get('email', None)

            # Now that we have the user's email, refresh the alert context so
            # any subsequent logger.error in this background task carries it.
            try:
                set_alert_request_context(user_email=user_email)
            except Exception:
                pass

            # 3. Handle chat context
            db_response = await get_user_chat(user_id=user_id, chat_id=data.chat_id, session=session)
            if not db_response.get('success', False):
                logger.error(f"Database error: Failed to get user chats: {db_response.get('error')} for user_id: {user_id} and chat_id: {data.chat_id}")
                event_data = {
                    "event": "error",
                    "data": json.dumps({
                        "response": "I'm having difficulty loading your previous messages. Please try again in a moment."
                    })
                }
                await redis_instance.redis_client.xadd(stream_key, event_data)
                return
            
            message_data = db_response.get("message", {})
            if not message_data:
                # Check if chat_id exists under a different user (ownership validation)
                stmt = select(Message).where(Message.id == data.chat_id)
                result = await session.execute(stmt)
                existing_chat = result.scalar_one_or_none()
                
                if existing_chat and existing_chat.user_id != user_id:
                    # Chat exists but belongs to another user - deny access
                    logger.error(f"User {user_id} attempted to access chat_id {data.chat_id} owned by user {existing_chat.user_id}")
                    event_data = {
                        "event": "error",
                        "data": json.dumps({
                            "response": "You don't have permission to access this chat."
                        })
                    }
                    await redis_instance.redis_client.xadd(stream_key, event_data)
                    return
                
                # new chat
                is_new_chat = True
                logger.info(f"Starting new chat for user: {user_name} with user_id: {user_id} and chat_id: {data.chat_id}")
                # cloudwatch_data = {
                #     "event": "Chat started",
                #     "event_success": True,
                #     "timestamp": query_received_time.isoformat(),
                #     "chat_id": data.chat_id,
                #     "chat_title": chat_title
                # }
                # try:
                #     asyncio.create_task(insert_cloudwatch_logs(data=cloudwatch_data, user_id=user_id))
                # except Exception as e:
                #     logger.error(f"Error in inserting cloudwatch logs: {e} for user_id: {user_id} and chat_id: {data.chat_id}")
                
            else:
                # existing chat
                chat_title = db_response.get("message", {}).get('chat_title', None)

                user_previous_messages = db_response.get("message", {}).get('chat_messages', [])
                if user_previous_messages[0]['type'] == "system":
                    user_previous_messages = user_previous_messages[1:]
                logger.info(f"Fetched {len(user_previous_messages)} previous messages for user {user_name} with user_id: {user_id} and chat_id: {data.chat_id}")

                cloudwatch_data = {
                    "event": "User input",
                    "event_success": True,
                    "timestamp": query_received_time.isoformat(),
                    "chat_id": data.chat_id,
                    "chat_title": chat_title
                }
                try:
                    asyncio.create_task(insert_cloudwatch_logs(data=cloudwatch_data, user_id=user_id))
                except Exception as e:
                    logger.error(f"Error in inserting cloudwatch logs: {e} for user_id: {user_id} and chat_id: {data.chat_id}")

            # 4. Get user subscription plan
            # user_plan = await get_user_subscription_plan(user_id=user_id, session=session)
            # logger.info(f"User {user_id} subscription plan: {user_plan}")

            # ── 4b. Build grep_session for document-based chat ──
            # Rules:
            #   - New chat + reference_ids → build grep_session from reference_ids
            #   - Existing chat → build grep_session from chat_files table
            #   - New chat without reference_ids → no session (regular chat)
            #   - Existing chat + reference_ids sent → reference_ids IGNORED,
            #     session built from chat_files instead
            grep_session = None
            if is_new_chat and data.reference_ids:
                try:
                    grep_session = await get_or_create_grep_session(
                        data.reference_ids,
                        session,
                    )
                    if grep_session:
                        logger.info(
                            f"Built grep_session for new chat chat_id={data.chat_id}: "
                            f"docs={len(grep_session.labels)}, labels={grep_session.labels}"
                        )
                except Exception as e:
                    logger.warning(
                        f"Could not build grep_session for chat_id={data.chat_id}: {e} "
                        f"— proceeding without document context",
                        exc_info=True,
                    )
            elif not is_new_chat:
                # Existing chat — ignore reference_ids and build from chat_files
                if data.reference_ids:
                    logger.info(
                        f"Ignoring reference_ids for existing chat chat_id={data.chat_id} "
                        f"(reference_ids are only used on the first message of a new chat)"
                    )
                try:
                    uploaded_file_ids = await get_chat_uploaded_file_ids(data.chat_id, session)
                    if uploaded_file_ids:
                        grep_session = await get_or_create_grep_session(uploaded_file_ids, session)
                    if grep_session:
                        logger.info(
                            f"Built grep_session from chat_files for existing chat "
                            f"chat_id={data.chat_id}: docs={len(grep_session.labels)}"
                        )
                except Exception as e:
                    logger.error(
                        f"Error building grep_session from chat_files "
                        f"for chat_id={data.chat_id}: {e}",
                        exc_info=True,
                    )

        # 5. Initialize Casper and process query
        casper = await run_in_threadpool(lambda: Casper({
            "user_name": user_name,
            "chat_id": data.chat_id,
            "user_previous_messages": user_previous_messages,
            "user_id": user_id,
            "grep_session": grep_session,
            # "user_plan": user_plan
        }))
        await casper.async_init()

        is_report_generated = False
        raw_report_generation_start_time = None
        raw_report_generation_end_time = None
        raw_md_report = None
        all_messages_after_query = []
        message_stream_complete_time = datetime.now(timezone.utc)
        accumulated_citations: list = []

        report_id = str(uuid7())
        report_version_id = None  # Will be set after create_report
        report_version = 1  # Initial version
        report_title = None
        report_layout = None
        report_length = None
        domain_name = None
        selected_report_type = "study"
        report_summary = None
        report_citations = None
        report_cards = []
        table_markdown_map = {}  # Initialize table markdown map to collect all table data
        section_sequence = 5
        # For DD/PR domains the subgraph emits `report_layout` BEFORE
        # `card_stream_start` (their DRL planner runs in a separate node ahead
        # of card streaming), whereas the standard `retrieve` flow emits
        # `card_stream_start` first and then `report_layout`. Buffer the DD/PR
        # `report_layout` SSE event here and flush it right after we forward
        # `card_stream_start` so the frontend sees a consistent ordering across
        # all domains: card_stream_start -> report_layout.
        pending_report_layout_event = None
        pending_card_stream_complete_event = None
        graph_state = await casper.get_processing_state(data.message)

        async for mode, output in graph_state:
            if mode == "messages":
                chunk, metadata = output
                if metadata.get('langgraph_node', '') != "report_or_respond":
                    continue
                
                ai_chunk = ""
                if hasattr(chunk, 'content'):
                    if isinstance(chunk.content, list) and chunk.content and isinstance(chunk.content[0], dict) and 'text' in chunk.content[0]:
                        ai_chunk = chunk.content[0]['text']
                    elif isinstance(chunk.content, str):
                        ai_chunk = chunk.content
                    else:
                        continue

                if ai_chunk:
                    event_data = {
                        "event": "delta",
                        "data": json.dumps({
                            "chunk": ai_chunk,
                            "type": "ai_chunk"
                        })
                    }
                    await redis_instance.redis_client.xadd(stream_key, event_data)
                else:
                    logger.error(f"No AI chunk found for user_id: {user_id} and chat_id: {data.chat_id}")
                    continue
                    event_data = {
                        "event": "error",
                        "data": json.dumps({
                            "response": "An unexpected error occurred while processing your request. Please try that again."
                        })
                    }
                    await redis_instance.redis_client.xadd(stream_key, event_data)
                    return

            elif mode == "custom":
                if "error" in output:
                    event_data = {
                        "event": "error",
                        "data": json.dumps({
                            "response": "An unexpected error occurred while processing your request. Please try that again."
                        })
                    }
                    await redis_instance.redis_client.xadd(stream_key, event_data)
                    return

                custom_name = output.get("name", "")
                custom_status = output.get("status", "")

                if custom_name in ("retrieve_latest_info", "query_document") and custom_status in ("start", "heartbeat", "end"):
                    type_map = {
                        ("retrieve_latest_info", "start"):     ("learning_brain_latest_start",     "Learning Brain is thinking it through"),
                        ("retrieve_latest_info", "heartbeat"): ("learning_brain_latest_heartbeat", "Learning Brain is still gathering the latest"),
                        ("retrieve_latest_info", "end"):       ("learning_brain_latest_end",       "Learning Brain latest signals synced"),
                        ("query_document", "start"):     ("learning_brain_document_start",     "Learning Brain is digesting your document"),
                        ("query_document", "heartbeat"): ("learning_brain_document_heartbeat", "Learning Brain is still reading your document"),
                        ("query_document", "end"):       ("learning_brain_document_end",       "Learning Brain document context synced"),
                    }
                    sse_type, sse_msg = type_map[(custom_name, custom_status)]
                    # Document start always uses the fixed Learning Brain label; heartbeats
                    # prefer model-authored progress lines; other starts/end fall back to default.
                    if custom_name == "query_document" and custom_status == "start":
                        payload = {"type": sse_type, "message": sse_msg}
                    else:
                        payload = {"type": sse_type, "message": output.get("message") or sse_msg}
                    if custom_status == "heartbeat":
                        payload["elapsed"] = output.get("elapsed", "")
                    elif custom_status == "end":
                        tool_citations = output.get("citations", [])
                        payload["citations"] = tool_citations
                        payload["elapsed"] = output.get("elapsed", "")
                        accumulated_citations.extend(tool_citations)
                    await redis_instance.redis_client.xadd(stream_key, {
                        "event": "message",
                        "data": json.dumps(payload)
                    })

                elif custom_name == "pr_analyze_document" and custom_status in ("start", "end"):
                    # Primary Research subgraph: the document-analysis step is user-visible
                    # progress (we're reading the uploaded research data). Surface it as a
                    # named SSE event mirroring the `query_document` / `retrieve_latest_info`
                    # pattern so the frontend can show a meaningful status.
                    type_map = {
                        ("pr_analyze_document", "start"): ("document_analysis_start", "Analyzing your uploaded research data…"),
                        ("pr_analyze_document", "end"):   ("document_analysis_end",   "Document analysis complete"),
                    }
                    sse_type, sse_msg = type_map[(custom_name, custom_status)]
                    await redis_instance.redis_client.xadd(stream_key, {
                        "event": "message",
                        "data": json.dumps({"type": sse_type, "message": sse_msg})
                    })

                elif custom_name in LAYOUT_PROPOSAL_EVENTS:
                    await redis_instance.redis_client.xadd(stream_key, {
                        "event": "message",
                        "data": json.dumps(_layout_proposal_sse_payload(output, report_id))
                    })

                elif custom_name in ("pr_generate_drl", "dd_generate_drl") and custom_status in ("start", "end"):
                    # PR/DD DRL-generation start/end are internal subgraph progress beats —
                    # drop them so they don't leak to the frontend as bare {"type": "start"} /
                    # {"type": "end"} via the generic status handler below. The actual
                    # `report_layout` payload is forwarded in its own branch further down.
                    continue

                elif "status" in output:
                    status = output.get("status", "")
                    
                    # Handle insufficient balance error from report_or_respond
                    # if status == "insufficient_balance":
                    #     available = output.get("available", 0)
                    #     required = output.get("required", 25000)
                    #     shortfall = output.get("shortfall", required - available)
                    #     logger.warning(f"Insufficient balance detected in model for user_id: {user_id}. Available: {available}, Required: {required}")
                    #     event_data = {
                    #         "event": "error",
                    #         "data": json.dumps({
                    #             "response": f"Insufficient token balance. You need {required:,} tokens but only have {available:,}. Please top up your wallet to continue.",
                    #             "error_code": "INSUFFICIENT_BALANCE",
                    #             "available": available,
                    #             "required": required,
                    #             "shortfall": shortfall
                    #         })
                    #     }
                    #     await redis_instance.redis_client.xadd(stream_key, event_data)
                    #     return
                    
                    if status == "card_stream_start":
                        event_data = {
                            "event": "message",
                            "data": json.dumps({
                                "type": status,
                                "report_title": output.get("title", ""),
                                "report_id": report_id
                            })
                        }

                        cloudwatch_data = {
                            "event": "Card generation started",
                            "event_success": True,
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                            "chat_id": data.chat_id,
                            "chat_title": chat_title
                        }
                        try:
                            asyncio.create_task(insert_cloudwatch_logs(data=cloudwatch_data, user_id=user_id))
                        except Exception as e:
                            logger.error(f"Error in inserting cloudwatch logs: {e} for user_id: {user_id} and chat_id: {data.chat_id}")

                    else:
                        if status == "message_stream_complete":
                            message_stream_complete_time = datetime.now(timezone.utc)
                    
                        if status == "card_stream_complete":
                            cloudwatch_data = {
                                "event": "Card generation completed",
                                "event_success": True,
                                "timestamp": datetime.now(timezone.utc).isoformat(),
                                "chat_id": data.chat_id,
                                "chat_title": chat_title
                            }
                            try:
                                asyncio.create_task(insert_cloudwatch_logs(data=cloudwatch_data, user_id=user_id))
                            except Exception as e:
                                logger.error(f"Error in inserting cloudwatch logs: {e} for user_id: {user_id} and chat_id: {data.chat_id}")
                            
                            # The frontend uses this event as the signal that report cards
                            # can be used for output generation. Hold it until after the
                            # collected cards have been persisted below.
                            pending_card_stream_complete_event = {
                                "event": "message",
                                "data": json.dumps({
                                    "type": status,
                                    "report_id": report_id,
                                    "report_version_id": report_version_id,
                                    "version": report_version
                                })
                            }
                            continue
                        if status == "md_content":
                            md_content = output.get("md_content", "")
                            
                            # 1. Upload initial markdown to S3 for Ask Caspr
                            initial_markdown_s3_path = None
                            try:
                                initial_markdown_s3_path = await run_in_threadpool(
                                    upload_initial_markdown_to_s3,
                                    md_content=md_content,
                                    user_id=user_id,
                                    user_name=user_name or "unknown",
                                    chat_id=data.chat_id,
                                    chat_title=chat_title or "untitled",
                                    report_id=report_id
                                )
                                logger.info(f"Uploaded initial markdown to S3: {initial_markdown_s3_path} for report_id: {report_id}")
                            except Exception as e:
                                logger.error(f"Failed to upload initial markdown to S3 for report_id: {report_id}: {str(e)}")
                            
                            # 2. Store initial_markdown S3 path in reports table, then create the
                            # OpenAI file_id for it in the background so the first Ask Caspr
                            # question or card refinement does not pay the conversion/upload cost.
                            if initial_markdown_s3_path and report_id:
                                try:
                                    async with async_session_scope() as file_session:
                                        await update_report(
                                            report_id=report_id,
                                            update_data={"initial_markdown": initial_markdown_s3_path},
                                            session=file_session
                                        )
                                    logger.info(f"Stored initial_markdown: {initial_markdown_s3_path} for report_id: {report_id}")
                                except Exception as e:
                                    logger.error(f"Failed to store initial_markdown for report_id: {report_id}: {str(e)}")

                                try:
                                    asyncio.create_task(ensure_report_file_id(
                                        report_id=report_id,
                                        file_id=None,
                                        file_s3_path=initial_markdown_s3_path,
                                        log_prefix="REPORT_GEN_FILE_ID",
                                    ))
                                    logger.info(f"Scheduled OpenAI file_id creation for report_id: {report_id}")
                                except Exception as e:
                                    logger.error(f"Failed to schedule OpenAI file_id creation for report_id: {report_id}: {str(e)}")
                            continue
                        elif status == "card_generation_heartbeat":
                            event_data = {
                                "event": "message",
                                "data": json.dumps({
                                    "type": status,
                                    "section": output.get("section", ""),
                                    "step": output.get("step", ""),
                                })
                            }
                        else:
                            event_data = {
                                "event": "message",
                                "data": json.dumps({
                                    "type": status
                                })
                            }
                    
                    await redis_instance.redis_client.xadd(stream_key, event_data)

                    # DD/PR subgraphs emit `report_layout` ahead of `card_stream_start`;
                    # flush the buffered event now so the frontend receives it right
                    # after `card_stream_start`, matching the ordering used by every
                    # other domain.
                    if status == "card_stream_start" and pending_report_layout_event is not None:
                        await redis_instance.redis_client.xadd(stream_key, pending_report_layout_event)
                        pending_report_layout_event = None

                elif output.get("name", "") == "retrieve":

                    if "report_title" in output:
                        report_title = output.get("report_title", "")
                    elif "report_length" in output:
                        report_length = output.get("report_length", "")
                    elif "domain_name" in output:
                        domain_name = output.get("domain_name", "")
                        update_data = {"domain_name": domain_name}
                        normalized_report_type = False
                        if domain_name == "due_diligence" and selected_report_type == "brief":
                            selected_report_type = "study"
                            update_data["report_type"] = selected_report_type
                            normalized_report_type = True
                            logger.warning(
                                "Normalized invalid due_diligence report_type='brief' to 'study' "
                                f"for report_id: {report_id}, chat_id: {data.chat_id}"
                            )
                        if domain_name and report_id:
                            try:
                                async with async_session_scope() as domain_session:
                                    await update_report(
                                        report_id=report_id,
                                        update_data=update_data,
                                        session=domain_session
                                    )
                                logger.info(
                                    f"Updated report {report_id} with domain_name='{domain_name}' "
                                    f"for chat_id: {data.chat_id}"
                                )
                            except Exception as e:
                                logger.error(
                                    f"Failed to update domain_name='{domain_name}' for "
                                    f"report_id: {report_id}, chat_id: {data.chat_id}: {e}"
                                )
                            if normalized_report_type:
                                event_data = {
                                    "event": "message",
                                    "data": json.dumps({
                                        "type": "report_type",
                                        "report_type": selected_report_type,
                                        "domain_name": domain_name,
                                        "report_id": report_id,
                                    })
                                }
                                await redis_instance.redis_client.xadd(stream_key, event_data)
                    elif "report_type" in output:
                        report_type_val = output.get("report_type", "study")
                        if domain_name == "due_diligence" and report_type_val == "brief":
                            report_type_val = "study"
                            logger.warning(
                                "Normalized invalid due_diligence report_type='brief' to 'study' "
                                f"for report_id: {report_id}, chat_id: {data.chat_id}"
                            )
                        selected_report_type = report_type_val
                        if report_type_val and report_id:
                            try:
                                async with async_session_scope() as rt_session:
                                    await update_report(
                                        report_id=report_id,
                                        update_data={"report_type": report_type_val},
                                        session=rt_session
                                    )
                                logger.info(
                                    f"Updated report {report_id} with report_type='{report_type_val}' "
                                    f"for chat_id: {data.chat_id}"
                                )
                            except Exception as e:
                                logger.error(
                                    f"Failed to update report_type='{report_type_val}' for "
                                    f"report_id: {report_id}, chat_id: {data.chat_id}: {e}"
                                )

                            # Stream the selected report type so the chat UI can
                            # reflect brief/study mode independently of domain.
                            event_data = {
                                "event": "message",
                                "data": json.dumps({
                                    "type": "report_type",
                                    "report_type": report_type_val,
                                    "domain_name": domain_name,
                                    "report_id": report_id,
                                })
                            }
                            await redis_instance.redis_client.xadd(stream_key, event_data)
                    elif "report_layout" in output:
                        report_layout = output.get("report_layout", "")
                        event_data = {
                            "event": "message",
                            "data": json.dumps({
                                "type": "report_layout",
                                "data": report_layout,
                                "report_id": report_id
                            })
                        }
                        await redis_instance.redis_client.xadd(stream_key, event_data)

                elif output.get("name", "") in ("dd_generate_drl", "pr_generate_drl") and "report_layout" in output:
                    # Specialized-domain subgraphs (Due Diligence / Primary Research) emit their
                    # own DRL payload under a different `name`, so the original
                    # `name == "retrieve"` branch above never sees them. Capture the cleaned
                    # report layout the same way so the frontend renders it (and so
                    # `report_layout` is also retained locally for the later `update_report`
                    # call). Unlike the standard `retrieve` flow, DD/PR emit this BEFORE
                    # `card_stream_start`; buffer the SSE event and let the
                    # `card_stream_start` handler flush it so the frontend always receives
                    # `card_stream_start` first, matching every other domain.
                    report_layout = output.get("report_layout", "")
                    pending_report_layout_event = {
                        "event": "message",
                        "data": json.dumps({
                            "type": "report_layout",
                            "data": report_layout,
                            "report_id": report_id
                        })
                    }

                # elif output.get("name", "") == "report_or_respond":
                #     event_data = {
                #         "event": "message",
                #         "data": json.dumps({
                #             "type": output.get("status", "")
                #         })
                #     }
                #     if output.get("status", "") == "message_stream_complete":
                #         message_stream_complete_time = datetime.now(timezone.utc)

                #     await redis_instance.redis_client.xadd(stream_key, event_data)

                elif output.get("name", "") == "generate_report":
                    if "report_summary" in output:
                        report_summary = output.get("report_summary", "")
                        # event_data = {
                        #     "event": "message",
                        #     "data": json.dumps({
                        #         "type": "report_summary",
                        #         "info": report_summary,
                        #         "report_id": report_id
                        #     })
                        # }
                        # await redis_instance.redis_client.xadd(stream_key, event_data)
                        continue

                    if "report_citations" in output:
                        report_citations = output.get("report_citations", {})
                        event_data = {
                            "event": "message",
                            "data": json.dumps({
                                "type": "report_citations",
                                "data": report_citations,
                                "report_id": report_id
                            })
                        }
                        await redis_instance.redis_client.xadd(stream_key, event_data)
                        continue

                    if "table_and_table_id_map" in output:
                        current_table_map = output.get("table_and_table_id_map") or {}
                        if current_table_map:
                            added = 0
                            for raw_tid, md in current_table_map.items():
                                tid = (raw_tid or "").strip()
                                if tid and md and tid not in table_markdown_map:
                                    table_markdown_map[tid] = md
                                    added += 1
                            logger.info(f"Collected {added} new tables (total={len(table_markdown_map)})")
                        else:
                            logger.warning("generate_report emitted empty table_and_table_id_map")

                    card = output.get("card_db", {})
                    card_type = output.get("card_type", "")
                    
                    # Skip empty cards - no need to insert in Redis or DB if card_type is empty and card is empty
                    if not card_type and (not card or card == {}):
                        logger.info(f"Skipping empty card for report_id: {report_id}")
                        continue
                    

                    event_data = {
                        "event": "delta",
                        "data": json.dumps({
                            "type": "card",
                            "data": {
                                "card": card,
                                "card_type": card_type
                            },
                            "report_id": report_id
                        })
                    }
                    await redis_instance.redis_client.xadd(stream_key, event_data)
                    card_data = {"type": card_type}
                    
                    # Handle new card structure format
                    if isinstance(card, dict) and "section" in card:
                        # New format with section array
                        card_data["section"] = card.get("section", [])
                        card_data["sub_sections"] = card.get("sub_sections", [])
                        card_data["citations"] = card.get("citations", {})
                        card_data["summary"] = card.get("summary", "")
                        sections = card.get("section", [])
                        # print(f"sections : {sections}")
                        if sections and len(sections) > 0 and "id" in sections[0]:
                            card_data["id"] = sections[0]["id"]
                        else:
                            card_data["id"] = str(uuid7())
                        
                        # Set sequence based on card type
                        if card_type == "title":
                            card_data["sequence"] = 1
                        elif card_type == "subtitle":
                            card_data["sequence"] = 2
                        elif card_type == "toc":
                            card_data["sequence"] = 3
                        elif card_type == "es":
                            card_data["sequence"] = 4
                        else:
                            # For section type cards
                            card_data["sequence"] = section_sequence
                            section_sequence += 1
                            
                        card_data["created_at"] = query_received_time
                        
                        # Set content as JSONB structure (same as section structure)
                        if sections and len(sections) > 0:
                            card_data["content"] = sections[0]  # Use the first section as content
                        else:
                            card_data["content"] = {
                                "name": card_type,
                                "content": "",
                                "tables": [{"visualization": "", "table_id": "", "table_title": ""}],
                                "id": str(uuid7())
                            }

                        
                    # Legacy format handling
                    else:
                        if card_type == "title":
                            card_data["title"] = card.get("title", "")
                            card_data["sequence"] = 1
                            card_data["sub_sections"] = []
                            card_data["content"] = {
                                "name": "title",
                                "content": card.get("title", ""),
                                "tables": [{"visualization": "", "table_id": "", "table_title": ""}],
                                "id": str(uuid7())
                            }
                            card_data["citations"] = {}
                            card_data["created_at"] = query_received_time
                            card_data["summary"] = ""
                            card_data["id"] = str(uuid7())
                        elif card_type == "subtitle":
                            card_data["title"] = "Subtitle"
                            card_data["sequence"] = 2
                            card_data["sub_sections"] = []
                            card_data["content"] = {
                                "name": "subtitle",
                                "content": card.get("subtitle", ""),
                                "tables": [{"visualization": "", "table_id": "", "table_title": ""}],
                                "id": str(uuid7())
                            }
                            card_data["citations"] = {}
                            card_data["created_at"] = datetime.now(timezone.utc)
                            card_data["summary"] = ""
                            card_data["id"] = str(uuid7())
                        elif card_type == "toc":
                            card_data["title"] = "Table of Contents"
                            card_data["sequence"] = 3
                            card_data["sub_sections"] = []
                            card_data["content"] = {
                                "name": "table_of_contents",
                                "content": card.get("table_of_contents", ""),
                                "tables": [{"visualization": "", "table_id": "", "table_title": ""}],
                                "id": str(uuid7())
                            }
                            card_data["citations"] = {}
                            card_data["created_at"] = datetime.now(timezone.utc)
                            card_data["summary"] = ""
                            card_data["id"] = str(uuid7())
                        elif card_type == "es":
                            card_data["title"] = "Executive Summary"
                            card_data["sequence"] = 4
                            card_data["sub_sections"] = []
                            card_data["content"] = {
                                "name": "executive_summary",
                                "content": card.get("executive_summary", ""),
                                "tables": [{"visualization": "", "table_id": "", "table_title": ""}],
                                "id": str(uuid7())
                            }
                            card_data["citations"] = {}
                            card_data["created_at"] = datetime.now(timezone.utc)
                            card_data["summary"] = ""
                            card_data["id"] = str(uuid7())
                        else:
                            # card_type == "section" - legacy format
                            card_data["title"] = card.get("section", "")
                            card_data["sequence"] = section_sequence
                            card_data["sub_sections"] = card.get("sub_sections", [])
                            card_data["content"] = {
                                "name": card.get("section", ""),
                                "content": card.get("content", ""),
                                "tables": [{"visualization": "", "table_id": "", "table_title": ""}],
                                "id": str(uuid7())
                            }
                            card_data["citations"] = card.get("citations", {})
                            card_data["created_at"] = datetime.now(timezone.utc)
                            card_data["summary"] = card.get("summary", "")
                            card_data["id"] = str(uuid7())
                            section_sequence += 1
                    report_cards.append(card_data)

            elif mode == "updates":
                if 'report_or_respond' in output:
                    AIMessage_object = output['report_or_respond']['messages'][0]
                    response_metadata = getattr(AIMessage_object, 'response_metadata', {})
                    # bedrock gives stopReason="tool_use", anthropic gives stop_reason="tool_use", openai gives finish_reason="tool_calls"

                    stop_reason = response_metadata.get('stopReason', '') or response_metadata.get('stop_reason', '')  or response_metadata.get('finish_reason', '')
                    # ai_answer_string = AIMessage_object.content[0]['text']
                    if hasattr(AIMessage_object, 'content'):
                        if isinstance(AIMessage_object.content, list) and AIMessage_object.content and isinstance(AIMessage_object.content[0], dict) and 'text' in AIMessage_object.content[0]:
                            ai_answer_string = AIMessage_object.content[0]['text']
                        elif isinstance(AIMessage_object.content, str):
                            ai_answer_string = AIMessage_object.content
                        else:
                            ai_answer_string = str(AIMessage_object)

                    is_tool_call = stop_reason in ('tool_use', 'tool_calls')
                    tool_calls = getattr(AIMessage_object, 'tool_calls', [])
                    called_tool_names = [tc.get('name', '') for tc in tool_calls] if tool_calls else []
                    is_retrieve_call = 'retrieve' in called_tool_names

                    if is_tool_call and not is_retrieve_call:
                        logger.info(
                            f"Non-retrieve tool call ({called_tool_names}) for chat_id={data.chat_id}, "
                            f"graph handles internally — continuing stream"
                        )

                    elif is_tool_call and is_retrieve_call:
                        async with async_session_scope() as session:
                            dup = await session.execute(
                                select(Report.id).where(
                                    Report.chat_id == data.chat_id,
                                    Report.status == ReportStatus.ANALYSIS_IN_PROGRESS.value,
                                ).limit(1)
                            )
                            if dup.scalar_one_or_none():
                                logger.warning(
                                    f"Duplicate retrieve blocked for chat_id={data.chat_id} "
                                    f"(report generation already in progress)"
                                )
                                return

                        # Check and reserve tokens for report generation
                        async with async_session_scope() as session:
                            token_result = await WalletService.check_and_reserve(
                                user_id=user_id, session=session, report_id=report_id
                            )
                            if not token_result.get('success', False):
                                logger.warning(f"Token check/reserve failed for user_id: {user_id}: {token_result.get('error')}")
                                error_code = token_result.get('error_code', '')
                                if error_code == 'INSUFFICIENT_BALANCE':
                                    event_data = {
                                        "event": "error",
                                        "data": json.dumps({
                                            "response": f"Insufficient token balance. You need {token_result.get('required', 25000):,} tokens but only have {token_result.get('available', 0):,}. Please top up your wallet to continue.",
                                            "error_code": "INSUFFICIENT_BALANCE",
                                            "available": token_result.get('available', 0),
                                            "required": token_result.get('required', 25000)
                                        })
                                    }
                                else:
                                    event_data = {
                                        "event": "error",
                                        "data": json.dumps({
                                            "response": "Unable to process your request at this time. Please try again."
                                        })
                                    }
                                await redis_instance.redis_client.xadd(stream_key, event_data)
                                return
                            logger.info(f"Reserved {token_result.get('tokens_reserved', 25000)} tokens for user_id: {user_id}")

                        async with async_session_scope() as session:
                            s3_uri = {
                                "md": None,
                                "html": None,
                                "pdf": None,
                                "pptx": None,
                                "info_pdf": None,
                            }
                            db_response = await create_report(report_id=report_id, chat_id=data.chat_id, created_at=query_received_time, s3_uri=s3_uri, session=session)
                            if not db_response.get('success', False):
                                logger.error(f"Failed to create report: {db_response.get('error')} for user_id: {user_id} and chat_id: {data.chat_id}")
                                # Release token reservation on failure
                                async with async_session_scope() as release_session:
                                    await WalletService.release_report_reservation(report_id, release_session, "report_creation_failed")
                                event_data = {
                                    "event": "error",
                                    "data": json.dumps({
                                        "response": "The report file couldn't be created at this time. Please try generating it again."
                                    })
                                }
                                # TODO: mail send here
                                await redis_instance.redis_client.xadd(stream_key, event_data)
                                return
                            
                            # Get version info from create_report response
                            report_version_id = db_response.get('version_id')
                            report_version = db_response.get('version', 1)
                            
                            # Set status to ANALYSIS_IN_PROGRESS when report is created (card generation starts)
                            await update_report_status_by_chat_or_report_id(
                                report_id=report_id,
                                status=ReportStatus.ANALYSIS_IN_PROGRESS.value,
                                session=session
                            )
                            logger.info(f"Set report status to ANALYSIS_IN_PROGRESS for report_id: {report_id}")


                        event_data = {
                            "event": "message",
                            "data": json.dumps({
                                "type": "tool_call",
                                "report_id": report_id,
                                "report_version_id": report_version_id,
                                "version": report_version
                            })
                        }
                        await redis_instance.redis_client.xadd(stream_key, event_data)
                        messages_to_append = [
                            {'type': 'human', 'content': data.message},
                            AIMessage_object.model_dump()
                        ]

                        async with async_session_scope() as session:
                            if is_new_chat:
                                message_data = {
                                    'user_id': user_id,
                                    'chat_id': data.chat_id,
                                    'chat_messages': messages_to_append,
                                    'updated_at': message_stream_complete_time,
                                    'created_at': query_received_time,
                                    'chat_title': chat_title,
                                }
                            else:
                                message_data = {
                                    'chat_id': data.chat_id,
                                    'chat_messages': user_previous_messages + messages_to_append,
                                    'updated_at': message_stream_complete_time,
                                    'user_id': user_id
                                }

                            logger.info(f"Inserting tool call message to database for user_id: {user_id} and chat_id: {data.chat_id}")
                            db_response = await insert_chats(message_data=message_data, session=session)
                            if not db_response.get('success', False):
                                logger.error(f"Database error: Failed to insert message: {db_response.get('error')} for user_id: {user_id} and chat_id: {data.chat_id}")
                                event_data = {
                                    "event": "error",
                                    "data": json.dumps({
                                        "response": "Your last message couldn't be saved. Please try sending it again."
                                    })
                                }
                                await redis_instance.redis_client.xadd(stream_key, event_data)
                                return

                            # Link uploaded files to chat on first message (tool-call path, new chat only)
                            if is_new_chat and data.reference_ids:
                                try:
                                    for ref_id in data.reference_ids:
                                        chat_file = ChatFile(
                                            id=str(uuid7()),
                                            chat_id=data.chat_id,
                                            uploaded_file_id=ref_id,
                                            usage_type=FileUsageType.REFERENCE,
                                            upload_context=FileUploadContext.IN_CHAT,
                                        )
                                        session.add(chat_file)
                                    await session.commit()
                                    logger.info(f"Linked {len(data.reference_ids)} uploaded file(s) to chat_id: {data.chat_id} (tool-call path)")
                                except Exception as e:
                                    logger.error(f"Failed to link uploaded files to chat {data.chat_id} (tool-call path): {e}", exc_info=True)

                            # Log Casper response
                            cloudwatch_data = {
                                "event": "Caspr response",
                                "event_success": True,
                                "timestamp": message_stream_complete_time.isoformat(),
                                "chat_id": data.chat_id,
                                "chat_title": chat_title
                            }
                            try:
                                asyncio.create_task(insert_cloudwatch_logs(data=cloudwatch_data, user_id=user_id))
                            except Exception as e:
                                logger.error(f"Error in inserting cloudwatch logs: {e} for user_id: {user_id} and chat_id: {data.chat_id}")
                            
                            # delete the user_message_processing key from redis when user message is inserted in db
                            await redis_instance.redis_client.delete(f"chat:{data.chat_id}:processing_user_message")
                            logger.info(f"Deleted user_message_processing key from redis for chat_id: {data.chat_id}")

                            # so that frontend does not get the same chat messages from both redis and db, so we delete the redis stream in refresh_session()
                            session_refreshed = await refresh_session(stream_key=stream_key, chat_id=data.chat_id)
                            logger.info(f"session_refreshed: {session_refreshed}")
                            if not session_refreshed:
                                event_data = {
                                    "event": "error",
                                    "data": json.dumps({
                                        "response": "Facing some issues while processing your request. Please try again in a few moments."
                                    })
                                }
                                await redis_instance.redis_client.xadd(stream_key, event_data)
                                return

                    elif stop_reason == 'end_turn':
                        pass

                    if is_new_chat and ai_answer_string:
                        chat_title = await casper.generate_chat_title(user_query=data.message, ai_response=ai_answer_string)
                        event_data = {
                            "event": "message",
                            "data": json.dumps({
                                "type": "new_chat_generation",
                                "chat_title": chat_title,
                                "chat_id": data.chat_id
                            })
                        }
                        await redis_instance.redis_client.xadd(stream_key, event_data)

                        cloudwatch_data = {
                            "event": "Chat started",
                            "event_success": True,
                            "timestamp": query_received_time.isoformat(),
                            "chat_id": data.chat_id,
                            "chat_title": chat_title
                        }
                        try:
                            asyncio.create_task(insert_cloudwatch_logs(data=cloudwatch_data, user_id=user_id))
                        except Exception as e:
                            logger.error(f"Error in inserting cloudwatch logs: {e} for user_id: {user_id} and chat_id: {data.chat_id}")

                        cloudwatch_data = {
                            "event": "User input",
                            "event_success": True,
                            "timestamp": query_received_time.isoformat(),
                            "chat_id": data.chat_id,
                            "chat_title": chat_title
                        }
                        try:
                            asyncio.create_task(insert_cloudwatch_logs(data=cloudwatch_data, user_id=user_id))
                        except Exception as e:
                            logger.error(f"Error in inserting cloudwatch logs: {e} for user_id: {user_id} and chat_id: {data.chat_id}")

                elif 'generate_report' in output or 'pr_subgraph' in output or 'dd_subgraph' in output:
                    # Specialized-domain subgraphs (Primary Research, Due Diligence)
                    # return their final AIMessage under their own node name
                    # (`pr_subgraph` / `dd_subgraph`). Treat those the same as
                    # `generate_report` for downstream report-output handling.
                    report_node_key = (
                        'generate_report' if 'generate_report' in output
                        else 'pr_subgraph' if 'pr_subgraph' in output
                        else 'dd_subgraph'
                    )
                    AIMessage_object = output[report_node_key]['messages'][0]
                    logger.info(
                        f"Consolidated raw report generated successfully "
                        f"(node='{report_node_key}') for user_id: {user_id} and chat_id: {data.chat_id}"
                    )
                    raw_md_report = AIMessage_object.content
                    is_report_generated = True

        if isinstance(output, dict) and 'messages' in output:
            all_messages_after_query = [msg.model_dump() for msg in output['messages'][1:]]
        else:
            event_data = {
                "event": "error",
                "data": json.dumps({
                    "response": "Some issue occurred while processing your request. Please start a new chat."
                })
            }
            await redis_instance.redis_client.xadd(stream_key, event_data)
            return

        # Handle regular chat
        if not all_messages_after_query:
            event_data = {
                "event": "error",
                "data": json.dumps({
                    "response": "I couldn't process the response. Please try again or start a new chat."
                })
            }
            await redis_instance.redis_client.xadd(stream_key, event_data)
            return
        else:
            event_data = {
                "event": "message",
                "data": json.dumps({
                    "type": "saving_checkpoint_data"
                })
            }
            await redis_instance.redis_client.xadd(stream_key, event_data)

            async with async_session_scope() as session:
                # Build citation mapping: {ai_message_id: [url, ...]} for this turn.
                # Find the last AI message in the list and use its LangChain id as key.
                turn_citations: dict | None = None
                if accumulated_citations:
                    last_ai_msg = next(
                        (m for m in reversed(all_messages_after_query) if m.get('type') == 'ai'),
                        None
                    )
                    if last_ai_msg and last_ai_msg.get('id'):
                        turn_citations = {last_ai_msg['id']: list(dict.fromkeys(accumulated_citations))}

                message_data = {
                    'user_id': user_id,
                    'chat_id': data.chat_id,
                    'chat_messages': all_messages_after_query,
                    'updated_at': message_stream_complete_time,
                    'created_at': query_received_time if is_new_chat else None,
                    'chat_title': chat_title,
                    'message_citations': turn_citations,
                }
                db_response = await insert_chats(message_data=message_data, session=session)
                if not db_response.get('success', False):
                    logger.error(f"Database error: Failed to insert message: {db_response.get('error')} for user_id: {user_id} and chat_id: {data.chat_id}")
                    event_data = {
                        "event": "error",
                        "data": json.dumps({
                            "response": "Your last message couldn't be saved. Please try sending it again."
                        })
                    }
                    await redis_instance.redis_client.xadd(stream_key, event_data)
                    return

                # Link uploaded files to chat on first message (new chat only)
                if is_new_chat and data.reference_ids:
                    try:
                        for ref_id in data.reference_ids:
                            chat_file = ChatFile(
                                id=str(uuid7()),
                                chat_id=data.chat_id,
                                uploaded_file_id=ref_id,
                                file_version_id=ref_id_to_fv_id.get(ref_id),
                                usage_type=FileUsageType.REFERENCE,
                                upload_context=FileUploadContext.IN_CHAT,
                            )
                            session.add(chat_file)
                        await session.commit()
                        logger.info(f"Linked {len(data.reference_ids)} uploaded file(s) to chat_id: {data.chat_id}")
                    except Exception as e:
                        logger.error(f"Failed to link uploaded files to chat {data.chat_id}: {e}", exc_info=True)

                if not is_report_generated:
                    # Log Casper response
                    cloudwatch_data = {
                        "event": "Caspr response",
                        "event_success": True,
                        "timestamp": message_stream_complete_time.isoformat(),
                        "chat_id": data.chat_id,
                        "chat_title": chat_title
                    }
                    try:
                        asyncio.create_task(insert_cloudwatch_logs(data=cloudwatch_data, user_id=user_id))
                    except Exception as e:
                        logger.error(f"Error in inserting cloudwatch logs: {e} for user_id: {user_id} and chat_id: {data.chat_id}")
                
                # delete the user_message_processing key from redis when user message is inserted in db
                await redis_instance.redis_client.delete(f"chat:{data.chat_id}:processing_user_message")
                logger.info(f"Deleted user_message_processing key from redis for chat_id: {data.chat_id}")

                session_refreshed = await refresh_session(stream_key=stream_key, chat_id=data.chat_id)
                logger.info(f"{len(all_messages_after_query)} messages inserted in database for user {user_name} with user_id {user_id} and chat_id: {data.chat_id}")

        if is_report_generated:
            report_update_data = {
                "title": report_title,
                # "s3_uri": {},
                "layout": report_layout,
                "length": report_length,
                "summary": report_summary,
                "citations": report_citations
            }
            async with async_session_scope() as session:
                db_response = await update_report(report_id=report_id, update_data=report_update_data, session=session)
                if not db_response.get('success', False):
                    logger.error(f"Database error: Failed to update report: {db_response.get('error')} for user_id: {user_id} and chat_id: {data.chat_id}")
                    event_data = {
                        "event": "error",
                        "data": json.dumps({
                            "response": "I couldn't save the report information to your account. Please try generating it again."
                        })
                    }
                    await redis_instance.redis_client.xadd(stream_key, event_data)
                    return

            async with async_session_scope() as session:
                # Create a local table_markdown_map for this request
                # table_markdown_map = {}
                
                # We'll collect table markdown data directly from the model response
                # No need to read from Redis stream
                logger.info(f"Processing {len(report_cards)} report cards for table insertion")
                logger.info(f"Total collected table markdown data: {len(table_markdown_map)} tables")
                
                if not table_markdown_map:
                    logger.warning("⚠️  No table markdown data collected - check if model is generating table_and_table_id_map streams")
                
                for report_card in report_cards:
                    # Extract any tables from the section format if present
                    all_tables = []
                    if "section" in report_card and isinstance(report_card["section"], list):
                        for section in report_card["section"]:
                            if "tables" in section:
                                all_tables.extend(section.get("tables", []))
                    
                    # Extract any tables from subsections
                    if "sub_sections" in report_card:
                        for subsection in report_card["sub_sections"]:
                            if "tables" in subsection:
                                all_tables.extend(subsection.get("tables", []))
                    
                    # First insert the card
                    db_response = await insert_card(report_id=report_id, report_card=report_card, session=session)
                    if not db_response.get('success', False):
                        logger.error(f"Database error: Failed to insert card: {db_response.get('error')} for user_id: {user_id} and chat_id: {data.chat_id}")
                        event_data = {
                            "event": "error",
                            "data": json.dumps({
                                "response": "Something went wrong. Please try again. If the issue persists, delete this chat and start a new one."
                            })
                        }
                        await redis_instance.redis_client.xadd(stream_key, event_data)
                        return
                        
                    # Then insert table records using insert_table function
                    card_id = db_response.get("card_id")
                    if card_id and all_tables:
                        for table in all_tables:
                            table_id = table.get("table_id", "")
                            if table_id and table_id.strip():  # Check if table_id is not empty
                                # Get the actual table markdown from the collected data
                                table_markdown = table_markdown_map.get(table_id)
                                
                                if table_markdown:                                    
                                    # Prepare table data for insert_table function
                                    table_data = {
                                        "table_id": table_id,
                                        "report_id": report_id,
                                        "table_title": table.get("table_title", ""),
                                        "table_markdown": table_markdown,
                                        "visualization": table.get("visualization", "")
                                    }
                                    # Insert table using insert_table function
                                    table_response = await insert_table(session=session, card_id=card_id, table_data=table_data)
                                    if not table_response.get('success', False):
                                        logger.error(f"Failed to insert table {table_id} for card {card_id}: {table_response.get('error')}")
                                    else:
                                        logger.info(f"Successfully inserted table {table_id} for card {card_id}")
                                else:
                                    logger.warning(f"No table markdown found for table_id: {table_id}")
                                    
                                    # Insert table with NULL markdown instead of placeholder text
                                    table_data = {
                                        "table_id": table_id,
                                        "report_id": report_id,
                                        "table_title": table.get("table_title", ""),
                                        "table_markdown": None,  # Use None instead of placeholder
                                        "visualization": table.get("visualization", "")
                                    }
                                    table_response = await insert_table(session=session, card_id=card_id, table_data=table_data)
                                    if not table_response.get('success', False):
                                        logger.error(f"Failed to insert table {table_id} for card {card_id}: {table_response.get('error')}")
                                    else:
                                        logger.info(f"Successfully inserted table {table_id} for card {card_id} (with NULL markdown)")
                
                # Insert Ask Caspr chat entries (chat=NULL) for all section cards and their subsections
                try:
                    for report_card in report_cards:
                        if report_card.get('type') == 'section':
                            section_id = report_card.get('id')
                            if section_id:
                                # Section-level entry
                                await insert_ask_caspr_chat_entry(
                                    report_id=report_id,
                                    section_id=section_id,
                                    session=session,
                                    subsection_id=None,
                                    version=1,
                                    card_version=1
                                )
                                # Subsection-level entries
                                for subsection in report_card.get('sub_sections', []) or []:
                                    sub_id = subsection.get('id') if isinstance(subsection, dict) else None
                                    if sub_id:
                                        await insert_ask_caspr_chat_entry(
                                            report_id=report_id,
                                            section_id=section_id,
                                            session=session,
                                            subsection_id=sub_id,
                                            version=1,
                                            card_version=1
                                        )
                    await session.commit()
                    logger.info(f"Inserted Ask Caspr chat entries for all sections/subsections of report_id: {report_id}")
                except Exception as e:
                    await session.rollback()
                    logger.error(f"Failed to insert Ask Caspr chat entries for report_id: {report_id}: {str(e)}")

                # Set status to ANALYSIS_COMPLETED when all cards have been inserted
                await update_report_status_by_chat_or_report_id(
                    report_id=report_id,
                    status=ReportStatus.ANALYSIS_COMPLETED.value,
                    session=session
                )
                logger.info(f"Set report status to ANALYSIS_COMPLETED for report_id: {report_id}")
                
                # Confirm token debit after report status is set to ANALYSIS_COMPLETED
                # This ensures billing only happens after cards are successfully persisted
                try:
                    debit_result = await WalletService.confirm_report_debit(report_id, session)
                    if debit_result.get('success'):
                        logger.info(f"Token debit confirmed for report_id: {report_id}, tokens: {debit_result.get('tokens_debited')}")
                    else:
                        logger.error(f"Failed to confirm token debit for report_id: {report_id}: {debit_result.get('error')}")
                except Exception as e:
                    logger.error(f"Error confirming token debit for report_id: {report_id}: {str(e)}")
                
                # Update all section cards to mark them as used in the initial ES version
                try:
                    # Get the ES card version
                    es_card_result = await session.execute(
                        select(Card).where(
                            Card.report_id == report_id,
                            Card.type == 'es',
                            Card.is_active == True,
                            Card.is_deleted == False
                        )
                    )
                    es_card = es_card_result.scalar_one_or_none()
                    
                    if es_card:
                        es_version = es_card.version or 1
                        
                        # Update all section cards to mark them as used in this ES version
                        section_cards_result = await session.execute(
                            select(Card).where(
                                Card.report_id == report_id,
                                Card.type == 'section',
                                Card.is_active == True,
                                Card.is_deleted == False
                            )
                        )
                        section_cards = section_cards_result.scalars().all()
                        
                        for card in section_cards:
                            card.last_es_version_used = es_version
                        
                        await session.commit()
                        logger.info(f"Updated {len(section_cards)} section cards with last_es_version_used={es_version} for report_id: {report_id}")
                    else:
                        logger.warning(f"No ES card found for report_id: {report_id}, skipping ES version tracking")
                except Exception as e:
                    logger.error(f"Error updating section cards with ES version: {str(e)}")
                    # Don't fail the entire request if this fails, just log it

                if pending_card_stream_complete_event is not None:
                    await redis_instance.redis_client.xadd(stream_key, pending_card_stream_complete_event)
                    pending_card_stream_complete_event = None

            session_refreshed = await refresh_session(stream_key=stream_key, chat_id=data.chat_id)
            if not session_refreshed:
                event_data = {
                        "event": "error",
                        "data": json.dumps({
                        "response": "Facing some issues while processing your request. Please try again in a few moments."
                    })
                }
                await redis_instance.redis_client.xadd(stream_key, event_data)
                return
            
            event_data = {
                "event": "message",
                "data": json.dumps({
                    "type": "saving_checkpoint_data_done"
                })
            }
            await redis_instance.redis_client.xadd(stream_key, event_data)

    except Exception as e:
        # Handle session errors
        logger.error(f"Encountered error in chat_producer: {e} for user_id: {user_id} and chat_id: {data.chat_id}")

        # Surface the failure to the error-digest email pipeline.  This block
        # runs inside a FastAPI BackgroundTask, so the HTTP 500-middleware in
        # main.py never sees it; without this explicit queueing the operator
        # would only get a CloudWatch line and no alert email.
        try:
            from src.core.observability.error_alerter import queue_background_error
            await queue_background_error(
                path=f"chat_producer:/api/v1/chat (chat_id={data.chat_id})",
                error_message=f"chat_producer failed: {e}",
                user_id=user_id,
                user_email=user_email,
                exc=e,
            )
        except Exception as alert_error:
            logger.warning(f"Failed to queue background error alert: {alert_error}")

        # Release token reservation if report generation failed
        try:
            if report_id:
                async with async_session_scope() as release_session:
                    release_result = await WalletService.release_report_reservation(
                        report_id, release_session, "generation_failed"
                    )
                    if release_result.get('success'):
                        logger.info(f"Released token reservation for failed report_id: {report_id}")
                    else:
                        logger.error(f"Failed to release token reservation: {release_result.get('error')}")
                        
        except Exception as release_error:
            logger.error(f"Error releasing token reservation: {str(release_error)}")
        
        event_data = {
            "event": "error",
            "data": json.dumps({
                "response": "An unexpected error occurred while processing your request. Please try that again."
            })
        }
        await redis_instance.redis_client.xadd(stream_key, event_data)
        return
    
    finally:
        logger.info(f"Stream completed for chat_id: {data.chat_id} and user_id: {user_id}")
        # delete the user_message_processing key from redis when stream is completed
        await redis_instance.redis_client.delete(f"chat:{data.chat_id}:processing_user_message")
        logger.info(f"Deleted user_message_processing key from redis for chat_id: {data.chat_id}")
        event_data = {
            "event": "message",
            "data": json.dumps({
                "type": "end_stream"
            })
        }
        await redis_instance.redis_client.xadd(stream_key, event_data)



@router.post(
    "/chat",
    status_code=200,
    response_class=JSONResponse,
    summary="Request for processing chat",
    description="Request for processing chat",
    responses={
        200: {"content": {"application/json": {}}},
        401: {"model": ErrorResponse},
        422: {"model": ErrorResponse},
        500: {"model": ErrorResponse}
    },
)
async def chat(data: ChatRequest, background_tasks: BackgroundTasks, user_id: str = Depends(get_current_active_user)):
    """
    POST /chat – Process a chat message.

    **reference_ids handling rules (upload integration):**
      - ``reference_ids`` is a list of ``uploaded_files.id`` sent by the
        frontend on the **first message** of a new chat only.
      - If ``reference_ids`` is sent and the user is on the free tier →
        request is denied (403).
      - For existing chats (``is_new_chat=false``), ``reference_ids`` is
        **ignored** even if sent by the frontend.  The backend determines
        which files belong to the chat from the ``chat_files`` table.
      - For new chats (``is_new_chat=true``) with ``reference_ids`` and a
        paid plan → ``chat_files`` records are created in ``chat_producer``.
    """
    logger.info(f"Received chat request for user_id: {user_id} and chat_id: {data.chat_id}")
    try:
        # Check if session id is valid
        redis_session_id = await redis_instance.redis_client.get(f"chat_session:{data.chat_id}")
        if redis_session_id != f"session:{data.session_id}":
            return JSONResponse(
                status_code=401,
                content={"success": False, "error": "Session expired!"}
            )
        
        stream_key = f"session:{data.session_id}"
        if not await redis_instance.redis_client.exists(stream_key):
            logger.warning(f"[SSE] Session expired for chat_id: {data.chat_id} and session_id: {data.session_id}")
            return JSONResponse(
                status_code=401,
                content={"success": False, "error": "Session expired!"}
            )
        
        await redis_instance.redis_client.expire(f"chat_session:{data.chat_id}", timedelta(hours=3)) 
        await redis_instance.redis_client.expire(stream_key, timedelta(hours=3)) 

        processing_payload = json.dumps({"type": "human", "content": data.message})
        was_set = await redis_instance.redis_client.set(
            f"chat:{data.chat_id}:processing_user_message",
            processing_payload,
            ex=int(timedelta(hours=24).total_seconds()),
            nx=True,
        )
        if not was_set:
            logger.info(
                f"Rejecting duplicate chat turn for chat_id={data.chat_id} "
                f"(turn already in progress)"
            )
            return JSONResponse(
                status_code=409,
                content={
                    "success": False,
                    "error": "A turn is already in progress for this chat.",
                    "error_code": "TURN_IN_PROGRESS",
                },
            )

        # ── Validate reference_ids: free-tier users cannot use file-based chat ──
        # NOTE: We validate free-tier here at the API level.  Whether reference_ids
        # are actually used (new chat) or ignored (existing chat) is determined
        # in chat_producer based on is_new_chat.
        if data.reference_ids:
            async with async_session_scope() as session:
                user_plan_result = await get_active_subscription(user_id=user_id, session=session)
                user_tier = user_plan_result.get("subscription", {}).get("current_tier", SubscriptionTier.FREE.value) if user_plan_result.get("subscription") else SubscriptionTier.FREE.value
            if user_tier == SubscriptionTier.FREE.value:
                logger.warning(f"Free-tier user {user_id} attempted to attach files to chat {data.chat_id}")
                return JSONResponse(
                    status_code=403,
                    content={"success": False, "error": "File-based chat is available on paid plans. Please upgrade to use this feature."}
                )

        # Add the chat_producer function directly to background tasks
        logger.info(f"Creating background task for user_id: {user_id} and chat_id: {data.chat_id}")
        background_tasks.add_task(chat_producer, data, user_id)
        logger.info(f"Background task created for user_id: {user_id} and chat_id: {data.chat_id}")
        return JSONResponse(
            status_code=200,
            content={"success": True, "message": "Chat stream started"}
        )

    except RequestValidationError as e:
        # Validation errors
        logger.error(f"Validation error in chat endpoint: {e}")
        return JSONResponse(
            status_code=422,
            content={"success": False, "error": "Your request couldn't be understood. Please check your input and try again."}
        )
    except HTTPException as e:
        # Re-raise HTTP exceptions
        raise
    except Exception as e:
        # All other errors
        logger.error(f"Error in chat-stream endpoint: {e}")
        return JSONResponse(
            status_code=500,
            content={"success": False, "error": "We're experiencing technical difficulties. Please try again later."}
        )

@router.post(
    "/create-temp-session",
    status_code=200,
    response_class=JSONResponse,
    summary="Create a new temporary session",
    description="Create a new temporary session"
)
async def create_temp_session(data: CreateTempSessionRequest):
    """Create a new temporary session"""
    logger.info(f"Recieved request to create new session for temporary chat_id: {data.chat_id}")
    try:
        logger.info(f"Creating new session for temporary chat_id: {data.chat_id}")
        session_id = str(uuid7())
        stream_key = f"session:{session_id}"
        chat_id = data.chat_id if data.chat_id else str(uuid7())
        await redis_instance.redis_client.set(f"temp_chat_session:{chat_id}", f"session:{session_id}", ex=timedelta(hours=3))
        event_data = {
            "event": "message",
            "data": json.dumps({
                "type": "session_created"
            })
        }
        await redis_instance.redis_client.xadd(stream_key, event_data)
        await redis_instance.redis_client.expire(stream_key, timedelta(hours=3))
        logger.info(f"Session created successfully for temporary chat_id: {chat_id}")
        return {"session_id": session_id, "chat_id": chat_id}
    
    except Exception as e:
        logger.error(f"Error in create_temp_session: {e}")
        return JSONResponse(
            status_code=500,
            content={"success": False, "error": "A system error prevented a new temporary chat from starting. Please try again."}
        )

@router.get(
    "/temp-chat-stream/{session_id}",
    status_code=200,
    response_class=StreamingResponse,
    summary="Get temporary chat events from redis stream",
    description="Get temporary chat events from redis stream"
)
async def temp_chat_stream(request: Request, session_id: str, chat_id: str, event_id: str = "0"):
    """Get temporary chat events from redis stream"""
    logger.info(f"[SSE] Temporary chat connection establishment request for session_id: {session_id} and temporary chat_id: {chat_id}")
    # Check if session id is valid
    stream_key = f"session:{session_id}"

    try:
        redis_session_id = await redis_instance.redis_client.get(f"temp_chat_session:{chat_id}")
        if redis_session_id != stream_key:
            logger.error(f"[SSE] Invalid session_id: {session_id} for temporary chat_id: {chat_id}")
            return JSONResponse(
                status_code=401,
                content={"success": False, "error": "This temporary chat has timed out."}
            )
        
        # if not await redis_instance.redis_client.exists(stream_key):
        #     logger.error(f"[SSE] Session expired for chat_id: {chat_id} and session_id: {session_id}")
        #     return JSONResponse(
        #         status_code=422,
        #         content={"success": False, "error": "Session expired!"}
        #     )

        timeout = timedelta(hours=3)
        # timeout = timedelta(seconds=30) 
        start_time = datetime.now(timezone.utc)
        last_seen_event_id = event_id

        async def event_generator():
            nonlocal start_time, last_seen_event_id
            events_yielded_yet = False
            try:
                while not await request.is_disconnected():
                    try:
                        response = await redis_instance.redis_client.xread({stream_key: last_seen_event_id}, block=2000)
                        if response:
                            _, entries = response[0]
                            for entry_id, fields in entries:
                                
                                last_seen_event_id = entry_id 
                                event = fields.get("event", "null")
                                data = json.loads(fields.get("data", "{}"))

                                # await asyncio.sleep(0.1)
                                yield f"event: {event}\ndata: {json.dumps({'event_id': entry_id, **data})}\n\n"
                                if not events_yielded_yet:
                                    logger.info(f"[SSE] Started streaming for temporary chat_id: {chat_id} and session_id: {session_id}")
                                    events_yielded_yet = True
                                
                                await redis_instance.redis_client.expire(stream_key, timedelta(hours=3))
                                await redis_instance.redis_client.expire(f"temp_chat_session:{chat_id}", timedelta(hours=3))
                            
                            start_time = datetime.now(timezone.utc)
                        else:
                            # # sending a keep-alive comment every 15 seconds to prevent connection timeouts
                            # if (datetime.now(timezone.utc) - start_time).total_seconds() > 15:
                            #     yield ": keepalive\n\n"
                            #     start_time = datetime.now(timezone.utc)
                                
                            if datetime.now(timezone.utc) - start_time > timeout:
                                error_data = {"response": "Session timeout!"}
                                yield f"event: timeout\ndata: {json.dumps(error_data)}\n\n"
                                logger.warning(f"[SSE] Timeout for session {session_id} and temporary chat_id: {chat_id}")
                                return
                    except Exception as e:
                        logger.error(f"[SSE] Error in event stream: {e} for temporary chat_id: {chat_id} and session_id: {session_id}")
                        yield f"event: error\ndata: {json.dumps({'response': 'Connection error'})}\n\n"
                        return
                        
            except Exception as e:
                logger.error(f"[SSE] Unhandled error in event generator: {e} for temporary chat_id: {chat_id} and session_id: {session_id}")
                yield f"event: error\ndata: {json.dumps({'response': 'We have encountered a connection error. Please refresh to try again.'})}\n\n"
                return

        # Set proper SSE headers
        headers = {
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no"  # Disable buffering in Nginx
        }
        
        return StreamingResponse(
            event_generator(), 
            media_type="text/event-stream",
            headers=headers
        )
    except Exception as e:
        logger.error(f"[SSE] Error establishing temporary chat stream: {e} for temporary chat_id: {chat_id} and session_id: {session_id}")
        return JSONResponse(
            status_code=500,
            content={"success": False, "error": "We're having some trouble with the connection. Please check your network."}
        )


async def temp_chat_producer(data: TempChatRequest):
    """Temporary chat endpoint with streaming response"""
    logger.info(f"Processing temporary chat request for temporary chat_id: {data.chat_id}")
    stream_key = f"session:{data.session_id}"

    # Seed the error-digest context (temp chats are anonymous so we only have
    # a chat_id to attribute errors to in the digest email).
    from src.core.observability.error_alerter import _init_request_context, set_alert_request_context
    _init_request_context()
    set_alert_request_context(
        method="BACKGROUND",
        path=f"temp_chat_producer:/api/v1/temp_chat (chat_id={data.chat_id})",
    )

    event_data = {
        "event": "message",
        "data": json.dumps({
            "type": "start_stream"
        })
    }
    await redis_instance.redis_client.xadd(stream_key, event_data)
    query_received_time = datetime.now(timezone.utc) 

    if not data.message:
        logger.error(f"Message is empty for temporary chat_id: {data.chat_id}")
        event_data = {
            "event": "error",
            "data": json.dumps({
                "response": "I couldn't process your request at this time. Please try again later."
            })
        }
        await redis_instance.redis_client.xadd(stream_key, event_data)
        return
    
    chat_id = data.chat_id
    message = data.message
    is_new_chat = False
    user_previous_messages = []

    try:
        logger.info(f"Checking if temporary chat with ID: {chat_id} exists")
        redis_response = await redis_instance.check_chat_exists(chat_id=chat_id)
        if not redis_response.get('success'):
            logger.error(f"Failed to get chat from Redis for chat_id: {chat_id}: {redis_response.get('error')}")
            event_data = {
                "event": "error",
                "data": json.dumps({
                    "response": "Your chat has expired. Please start a new chat."
                })
            }
            await redis_instance.redis_client.xadd(stream_key, event_data)
            return
        
        if not redis_response.get('exists'):
            is_new_chat = True

        if not is_new_chat:
            logger.info(f"Retrieving chat data for temporary chat_id: {chat_id}")
            redis_response = await redis_instance.get_chat(chat_id=chat_id)
            if not redis_response.get('success'):
                logger.error(f"Failed to get chat from Redis for temporary chat_id: {chat_id}: {redis_response.get('error')}")
                event_data = {
                    "event": "error",
                    "data": json.dumps({
                        "response": "Your chat has expired. Please start a new chat."
                    })
                }
                await redis_instance.redis_client.xadd(stream_key, event_data)
                return
        
            user_previous_messages = redis_response.get('chat_data', {}).get('chat_messages', [])
            logger.info(f'Retrieved {len(user_previous_messages)} previous messages for temporary chat ID: {chat_id}')

        # Initialize Casper model (temp/guest users default to free plan)
        casper = await run_in_threadpool(lambda: Casper({
            "user_name": "User", 
            "chat_id": "TEMP-" + chat_id,
            "user_previous_messages": user_previous_messages,
            # "user_plan": "free"
        }))
        await casper.async_init()
        logger.info(f'Casper model initialized for temporary chat ID: {chat_id}')

        all_messages_after_query = []
        message_stream_complete_time = datetime.now(timezone.utc)

        graph_state = await casper.get_processing_state(message)
        async for mode, output in graph_state:
            if mode == "messages":
                chunk, metadata = output
                # if chunk.content and 'text' in chunk.content[0]:
                #     ai_chunk = chunk.content[0]['text']
                if metadata.get('langgraph_node', '') != "report_or_respond":
                    continue
                
                ai_chunk = ""
                if hasattr(chunk, 'content'):
                    if isinstance(chunk.content, list) and chunk.content and isinstance(chunk.content[0], dict) and 'text' in chunk.content[0]:
                        ai_chunk = chunk.content[0]['text']
                    elif isinstance(chunk.content, str):
                        ai_chunk = chunk.content
                    else:
                        continue

                if ai_chunk:
                    event_data = {
                        "event": "delta",
                        "data": json.dumps({
                            "chunk": ai_chunk
                        })
                    }
                    await redis_instance.redis_client.xadd(stream_key, event_data)

            elif mode == "custom":
                event_status = output.get('status', '')
                custom_name = output.get('name', '')

                if event_status == 'error':
                    event_data = {
                        "event": "error",
                        "data": json.dumps({
                            "response": "An unexpected error occurred while processing your request. Please try that again."
                        })
                    }
                    await redis_instance.redis_client.xadd(stream_key, event_data)
                    return

                if custom_name in ("retrieve_latest_info", "query_document") and event_status in ("start", "heartbeat", "end"):
                    type_map = {
                        ("retrieve_latest_info", "start"):     ("learning_brain_latest_start",     "Learning Brain is thinking it through"),
                        ("retrieve_latest_info", "heartbeat"): ("learning_brain_latest_heartbeat", "Learning Brain is still gathering the latest"),
                        ("retrieve_latest_info", "end"):       ("learning_brain_latest_end",       "Learning Brain latest signals synced"),
                        ("query_document", "start"):     ("learning_brain_document_start",     "Learning Brain is digesting your document"),
                        ("query_document", "heartbeat"): ("learning_brain_document_heartbeat", "Learning Brain is still reading your document"),
                        ("query_document", "end"):       ("learning_brain_document_end",       "Learning Brain document context synced"),
                    }
                    sse_type, sse_msg = type_map[(custom_name, event_status)]
                    # Document start always uses the fixed Learning Brain label; heartbeats
                    # prefer model-authored progress lines; other starts/end fall back to default.
                    if custom_name == "query_document" and event_status == "start":
                        payload = {"type": sse_type, "message": sse_msg}
                    else:
                        payload = {"type": sse_type, "message": output.get("message") or sse_msg}
                    if event_status == "heartbeat":
                        payload["elapsed"] = output.get("elapsed", "")
                    elif event_status == "end":
                        payload["citations"] = output.get("citations", [])
                        payload["elapsed"] = output.get("elapsed", "")
                    await redis_instance.redis_client.xadd(stream_key, {
                        "event": "message",
                        "data": json.dumps(payload)
                    })
                elif custom_name in LAYOUT_PROPOSAL_EVENTS:
                    await redis_instance.redis_client.xadd(stream_key, {
                        "event": "message",
                        "data": json.dumps(_layout_proposal_sse_payload(output))
                    })

                else:
                    event_data = {
                        "event": "message",
                        "data": json.dumps({
                            "type": event_status
                        })
                    }
                    await redis_instance.redis_client.xadd(stream_key, event_data)

                if event_status == 'message_stream_complete':
                    message_stream_complete_time = datetime.now(timezone.utc)

            elif mode == "updates":
                if 'report_or_respond' in output:
                    AIMessage_object = output['report_or_respond']['messages'][0]

                    # Check response metadata safely
                    response_metadata = getattr(AIMessage_object, 'response_metadata', {})
                    # bedrock gives stopReason="tool_use", anthropic gives stop_reason="tool_use", openai gives finish_reason="tool_calls"
                    stop_reason = response_metadata.get('stopReason', '') or response_metadata.get('stop_reason', '')  or response_metadata.get('finish_reason', '')

                    if hasattr(AIMessage_object, 'content'):
                        if isinstance(AIMessage_object.content, list) and AIMessage_object.content and isinstance(AIMessage_object.content[0], dict) and 'text' in AIMessage_object.content[0]:
                            ai_answer_string = AIMessage_object.content[0]['text']
                        elif isinstance(AIMessage_object.content, str):
                            ai_answer_string = AIMessage_object.content
                        else:
                            ai_answer_string = str(AIMessage_object)

                    is_tool_call = stop_reason in ('tool_use', 'tool_calls')
                    tool_calls = getattr(AIMessage_object, 'tool_calls', [])
                    called_tool_names = [tc.get('name', '') for tc in tool_calls] if tool_calls else []
                    is_retrieve_call = 'retrieve' in called_tool_names

                    if is_tool_call and not is_retrieve_call:
                        logger.info(
                            f"Non-retrieve tool call ({called_tool_names}) for temp chat_id={chat_id}, "
                            f"graph handles internally — continuing stream"
                        )

                    elif is_tool_call and is_retrieve_call:
                        event_data = {
                            "event": "message",
                            "data": json.dumps({
                                "type": "tool_call"
                            })
                        }
                        await redis_instance.redis_client.xadd(stream_key, event_data)
                        logger.info(f"Temp user tried to generate a report with chat_id: {chat_id}")

                        event_data = {
                            "event": "message",
                            "data": json.dumps({
                                "type": "message_stream_start"
                            })
                        }
                        await redis_instance.redis_client.xadd(stream_key, event_data)

                        ai_answer_string = "Please login to generate a report. This feature is only available for registered users."
                        for char in range(0, len(ai_answer_string), 4):
                            event_data = {
                                "event": "delta",
                                "data": json.dumps({
                                    "chunk": ai_answer_string[char:char+4]
                                })
                            }
                            await redis_instance.redis_client.xadd(stream_key, event_data)
                            await asyncio.sleep(0.01)

                        event_data = {
                            "event": "message",
                            "data": json.dumps({
                                "type": "message_stream_complete"
                            })
                        }
                        await redis_instance.redis_client.xadd(stream_key, event_data)
                        return
                        
                    elif stop_reason == 'end_turn':
                        pass

                    if is_new_chat and ai_answer_string:
                        chat_title = await casper.generate_chat_title(user_query=data.message, ai_response=ai_answer_string)
                        event_data = {
                            "event": "message",
                            "data": json.dumps({
                                "type": "new_chat_generation",
                                "chat_title": chat_title,
                                "chat_id": chat_id
                            })
                        }
                        await redis_instance.redis_client.xadd(stream_key, event_data)

        if isinstance(output, dict) and 'messages' in output:
            all_messages_after_query = [msg.model_dump() for msg in output['messages'][1:]]
        else: 
            event_data = {
                "event": "error",
                "data": json.dumps({
                    "response": "An unexpected error occurred while processing your request. Please try that again."
                })
            }
            await redis_instance.redis_client.xadd(stream_key, event_data)
            return  

        if is_new_chat:
            chat_data = {
                "chat_id": chat_id,
                "user_id": 'User',
                "chat_title": chat_title,
                "chat_messages": all_messages_after_query,
                "created_at": query_received_time,
                "updated_at": message_stream_complete_time
            }
            redis_response = await redis_instance.create_chat(chat_data=chat_data)
            if not redis_response.get('success'):
                logger.error(f"Failed to create new chat in Redis for chat_id: {chat_id}: {redis_response.get('error')}")
                event_data = {
                    "event": "error",
                    "data": json.dumps({
                        "response": "An unexpected error occurred while processing your request. Please try that again."
                    })
                }
                await redis_instance.redis_client.xadd(stream_key, event_data)
                return
        else:
            chat_data = {
                "chat_id": chat_id,
                "chat_messages": all_messages_after_query,
                "updated_at": message_stream_complete_time
            }
            redis_response = await redis_instance.update_chat(chat_data=chat_data)
        
            if not redis_response.get('success'):
                logger.error(f"Failed to update chat in Redis for chat_id: {chat_id}: {redis_response.get('error')}")
                event_data = {
                    "event": "error",
                    "data": json.dumps({
                        "response": "An unexpected error occurred while processing your request. Please try that again."
                    })
                }
                await redis_instance.redis_client.xadd(stream_key, event_data)
                return              

    except Exception as e:
        logger.error(f"Error in temp_chat_producer: {e}")

        # Surface the failure to the error-digest email pipeline.  This block
        # runs inside a FastAPI BackgroundTask, so the HTTP 500-middleware in
        # main.py never sees it; without this explicit queueing the operator
        # would only get a CloudWatch line and no alert email.
        try:
            from src.core.observability.error_alerter import queue_background_error
            chat_id_for_log = getattr(data, "chat_id", None) or "unknown"
            await queue_background_error(
                path=f"temp_chat_producer:/api/v1/temp_chat (chat_id={chat_id_for_log})",
                error_message=f"temp_chat_producer failed: {e}",
                user_id=None,
                exc=e,
            )
        except Exception as alert_error:
            logger.warning(f"Failed to queue background error alert: {alert_error}")

        event_data = {
            "event": "error",
            "data": json.dumps({
                "response": "An unexpected error occurred while processing your request. Please try that again."
            })
        }
        await redis_instance.redis_client.xadd(stream_key, event_data)
        return
    
    finally:
        logger.info(f"Stream completed for temporary chat_id: {chat_id}")
        event_data = {
            "event": "message",
            "data": json.dumps({
                "type": "end_stream"
            })
        }
        await redis_instance.redis_client.xadd(stream_key, event_data)

@router.post(
    "/temp-chat",
    status_code=200,
    response_class=JSONResponse,
    summary="Request for processing temporary chat",
    description="Request for processing temporary chat",
    responses={
        200: {"content": {"application/json": {}}},
        401: {"model": ErrorResponse},
        422: {"model": ErrorResponse},
        500: {"model": ErrorResponse}
    },
)
async def temp_chat(data: TempChatRequest, background_tasks: BackgroundTasks):
    logger.info(f"Received temporary chat request for temporary chat_id: {data.chat_id}")
    try:
        # Check if session id is valid
        redis_session_id = await redis_instance.redis_client.get(f"temp_chat_session:{data.chat_id}")
        if redis_session_id != f"session:{data.session_id}":
            return JSONResponse(
                status_code=401,
                content={"success": False, "error": "This temporary chat has timed out. Let's start a fresh conversation."}
            )
        
        stream_key = f"session:{data.session_id}"
        if not await redis_instance.redis_client.exists(stream_key):
            logger.warning(f"[SSE] Session expired for temporary chat_id: {data.chat_id} and session_id: {data.session_id}")
            return JSONResponse(
                status_code=401,
                content={"success": False, "error": "Session expired!"}
            )
        
        await redis_instance.redis_client.expire(f"temp_chat_session:{data.chat_id}", timedelta(hours=3)) 
        await redis_instance.redis_client.expire(stream_key, timedelta(hours=3)) 

        # Add the chat_producer function directly to background tasks
        logger.info(f"Creating background task for temporary chat_id: {data.chat_id}")
        background_tasks.add_task(temp_chat_producer, data)
        logger.info("Background task created for temporary chat_id: {data.chat_id}")
        return JSONResponse(
            status_code=200,
            content={"success": True, "message": "Temporary chat stream started"}
        )

    except RequestValidationError as e:
        # Validation errors
        logger.error(f"Validation error in temporary chat endpoint: {e}")
        return JSONResponse(
            status_code=422,
            content={"success": False, "error": "Your request couldn't be understood. Please check your input and try again."}
        )
    except HTTPException as e:
        # Re-raise HTTP exceptions
        raise
    except Exception as e:
        # All other errors
        logger.error(f"Error in temporary chat endpoint: {e}")
        return JSONResponse(
            status_code=500,
            content={"success": False, "error": "We're experiencing technical difficulties. Please try again later."}
        )


@router.get("/temp-chat/{chat_id}", response_model=TempChatMessagesResponse, status_code=200, responses={404: {"model": ErrorResponse}, 401: {"model": ErrorResponse}, 422: {"model": ErrorResponse}, 500: {"model": ErrorResponse}})
async def get_temp_chat_messages(chat_id: str):
    """Get temporary chat messages endpoint"""
    logger.info(f"Get temp chat messages request received for chat_id: {chat_id}")
    
    try:
        
        # Get temp chat data from Redis
        redis_response = await redis_instance.get_chat(chat_id=chat_id)
        if not redis_response.get('success'):
            logger.error(f"Failed to get temp chat from Redis for chat_id: {chat_id}: {redis_response.get('error')}")
            return JSONResponse(
                status_code=404,
                content={"success": False, "error": "Temporary chat not found. It may have expired."}
            )
        
        chat_data = redis_response.get('chat_data', {})
        chat_messages = chat_data.get('chat_messages', [])
        
        if not chat_messages:
            logger.warning(f"No chat messages found for temp chat_id: {chat_id}")
            return TempChatMessagesResponse(
                success=True,
                messages=[]
            )
        
        # Format chat messages (same logic as regular chat)
        formatted_chat_messages = []
        for i, msg in enumerate(chat_messages):
            msg_type = msg.get('type')
            msg_content = msg.get('content')
            
            if msg_type == 'human':
                msg_object = {'type': msg_type, 'content': msg_content}
                formatted_chat_messages.append(msg_object)
            elif msg_type == 'ai':
                msg_metadata = msg.get('response_metadata', {})
                msg_stop = (
                    msg_metadata.get('stopReason', '')
                    or msg_metadata.get('stop_reason', '')
                    or msg_metadata.get('finish_reason', '')
                )
                if msg_stop in ('tool_use', 'tool_calls'):
                    called_names = [tc.get('name', '') for tc in msg.get('tool_calls', [])]
                    if 'retrieve' not in called_names:
                        continue
                if i > 0 and chat_messages[i-1].get('type') == 'tool' and chat_messages[i-1].get('name') == 'retrieve':
                    continue
                if isinstance(msg_content, str):
                    msg_object = {'type': msg_type, 'content': msg_content}
                elif isinstance(msg_content, list) and isinstance(msg_content[0], dict) and 'text' in msg_content[0]:
                    msg_object = {'type': msg_type, 'content': msg_content[0]['text']}
                else:
                    continue
                formatted_chat_messages.append(msg_object)
        
        return TempChatMessagesResponse(
            success=True,
            messages=formatted_chat_messages
        )
    
    except RequestValidationError as e:
        logger.error(f"Validation error in get temp chat messages endpoint: {e} for chat_id: {chat_id}")
        return JSONResponse(
            status_code=422,
            content={"success": False, "error": "Your request couldn't be understood. Please check your input and try again."}
        )
    except HTTPException as e:
        raise
    except Exception as e:
        logger.error(f"Error in get temp chat messages endpoint: {e} for chat_id: {chat_id}")
        return JSONResponse(
            status_code=500,
            content={"success": False, "error": "Something unexpected happened. Please try again later."}
        )

    
# @router.post("/temp-chat", status_code=200, responses={401: {"model": ErrorResponse}, 422: {"model": ErrorResponse}, 500: {"model": ErrorResponse}})
# async def temp_chat(data: TempChatRequest):
#     """Temporary chat endpoint"""
#     query_received_time = datetime.now(timezone.utc)
#     logger.info(f"Temporary chat request received for chat_id: {data.chat_id} and message: {data.message}")

#     chat_id = data.chat_id
#     is_new_chat = not data.chat_id
#     message = data.message
    
#     # Check for empty message
#     if not message:
#         return JSONResponse(
#             status_code=400,
#             content={"success": False, "error": "Message cannot be empty"}
#         )
    
#     try:
#         # Generate response
#         async def generate_response():
#             try:
#                 nonlocal chat_id, query_received_time  # Add all nonlocal declarations at the beginning
                
#                 if is_new_chat:
#                     chat_id = str(uuid7())
#                     logger.info(f'Starting new temporary chat with ID: {chat_id}')
#                     user_previous_messages = []
#                 else:
#                     logger.info(f'Getting existing temporary chat with ID: {chat_id}')
#                     redis_response = await redis_instance.get_chat(chat_id=chat_id)
#                     if not redis_response.get('success'):
#                         logger.error(f"Failed to get chat from Redis for chat_id: {chat_id}: {redis_response.get('error')}")
#                         if not redis_instance.check_chat_exists(chat_id=chat_id):
#                             error_data = {
#                                 "response": "Your chat has expired. Please start a new chat."
#                             }
#                             yield f"event: error\ndata: {json.dumps(error_data)}\n\n"
#                             logger.info(f'Chat with ID: {chat_id} has expired.')
#                             return
#                         else:
#                             error_data = {
#                                 "response": "Internal server issue"
#                             }
#                             yield f"event: error\ndata: {json.dumps(error_data)}\n\n"
#                             return
                    
#                     user_previous_messages = redis_response.get('chat_data', {}).get('chat_messages', [])
#                     logger.info(f'Retrieved {len(user_previous_messages)} previous messages for chat ID: {chat_id}')
                    
#                 # Initialize Casper model
#                 casper = await run_in_threadpool(lambda: Casper({
#                     "user_name": "User", 
#                     "chat_id": "TEMP-" + chat_id,
#                     "user_previous_messages": user_previous_messages
#                 }))
#                 await casper.async_init()
#                 logger.info(f'Casper model initialized for temporary chat ID: {chat_id}')

#                 all_messages_after_query = []
#                 message_stream_complete_time = datetime.now(timezone.utc)

#                 graph_state = await casper.get_processing_state(message)
#                 async for mode, output in graph_state:
#                     if mode == "messages":
#                         chunk, metadata = output
#                         if chunk.content and 'text' in chunk.content[0]:
#                             ai_chunk = chunk.content[0]['text']
#                             delta_data = {
#                                 "chunk": ai_chunk
#                             }
#                             yield f"event: delta\ndata: {json.dumps(delta_data)}\n\n"

#                     elif mode == "custom":
#                         event_status = output.get('status', '')
#                         if event_status == 'error':
#                             error_data = {
#                                 "response": "I'm having trouble processing your request. Please try again later."
#                             }
#                             yield f"event: error\ndata: {json.dumps(error_data)}\n\n"
#                             return
#                         message_data = {
#                             "type": event_status
#                         }
#                         yield f"event: message\ndata: {json.dumps(message_data)}\n\n"

#                         if event_status == 'message_stream_complete':
#                             message_stream_complete_time = datetime.now(timezone.utc)

#                     elif mode == "updates":
#                         if 'report_or_respond' in output:
#                             AIMessage_object = output['report_or_respond']['messages'][0]

#                             # Check response metadata safely
#                             response_metadata = getattr(AIMessage_object, 'response_metadata', {})
#                             stop_reason = response_metadata.get('stopReason', '')

#                             if hasattr(AIMessage_object, 'content'):
#                                 if isinstance(AIMessage_object.content, list) and AIMessage_object.content and isinstance(AIMessage_object.content[0], dict) and 'text' in AIMessage_object.content[0]:
#                                     ai_answer_string = AIMessage_object.content[0]['text']
#                                 elif isinstance(AIMessage_object.content, str):
#                                     ai_answer_string = AIMessage_object.content
#                                 else:
#                                     ai_answer_string = str(AIMessage_object)

#                             if stop_reason == 'tool_use':
#                                 message_data = {
#                                     "type": "tool_call"
#                                 }
#                                 yield f"event: message\ndata: {json.dumps(message_data)}\n\n"
#                                 logger.info(f"Temp user tried to generate a report with chat_id: {chat_id}")

#                                 message_data = {
#                                     "type": "message_stream_start"
#                                 }
#                                 yield f"event: message\ndata: {json.dumps(message_data)}\n\n"

#                                 ai_answer_string = "Please login to generate a report. This feature is only available for registered users."
#                                 for char in range(0, len(ai_answer_string), 4):
#                                     delta_data = {
#                                         "chunk": ai_answer_string[char:char+4]
#                                     }
#                                     yield f"event: delta\ndata: {json.dumps(delta_data)}\n\n"
#                                     await asyncio.sleep(0.01)

#                                 message_data = {
#                                     "type": "message_stream_complete"
#                                 }
#                                 yield f"event: message\ndata: {json.dumps(message_data)}\n\n"
#                                 return
                                
#                             elif stop_reason == 'end_turn':
#                                 pass

#                             if is_new_chat and ai_answer_string:
#                                 chat_title = await casper.generate_chat_title(user_query=data.message, ai_response=ai_answer_string)
#                                 message_data = {
#                                     "type": "new_chat_generation",
#                                     "chat_title": chat_title,
#                                     "chat_id": chat_id
#                                 }
#                                 yield f"event: message\ndata: {json.dumps(message_data)}\n\n"

#                 if isinstance(output, dict) and 'messages' in output:
#                     all_messages_after_query = [msg.model_dump() for msg in output['messages'][1:]]
#                 else: 
#                     error_data = {
#                         "response": "Some issue occurred while processing your request. Please start a new chat."
#                     }
#                     yield f"event: error\ndata: {json.dumps(error_data)}\n\n"
#                     return
                
#                 if is_new_chat:
#                     chat_data = {
#                         "chat_id": chat_id,
#                         "user_id": 'User',
#                         "chat_title": chat_title,
#                         "chat_messages": all_messages_after_query,
#                         "created_at": query_received_time,
#                         "updated_at": message_stream_complete_time
#                     }
#                     redis_response = await redis_instance.create_chat(chat_data=chat_data)
#                     if not redis_response.get('success'):
#                         logger.error(f"Failed to create new chat in Redis for chat_id: {chat_id}: {redis_response.get('error')}")
#                         error_data = {
#                             "response": "Internal server issue"
#                         }
#                         yield f"event: error\ndata: {json.dumps(error_data)}\n\n"
#                         return
#                 else:
#                     chat_data = {
#                         "chat_id": chat_id,
#                         "chat_messages": all_messages_after_query,
#                         "updated_at": message_stream_complete_time
#                     }
#                     redis_response = await redis_instance.update_chat(chat_data=chat_data)
                
#                     if not redis_response.get('success'):
#                         logger.error(f"Failed to update chat in Redis for chat_id: {chat_id}: {redis_response.get('error')}")
#                         error_data = {
#                             "response": "Internal server issue"
#                         }
#                         yield f"event: error\ndata: {json.dumps(error_data)}\n\n"
#                         return
                    
#             except Exception as e:
#                 # Handle session errors
#                 logger.error(f"Error in generate_response for chat_id: {chat_id}: {e}")
#                 error_data = {
#                     "response": "Something unexpected happened. Please try again in a few moments."
#                 }
#                 yield f"event: error\ndata: {json.dumps(error_data)}\n\n"
#                 return
            
#             finally:
#                 logger.info(f"Stream completed for temporary chat_id: {chat_id}")
#                 message_data = {
#                     "type": "end_stream"
#                 }
#                 yield f"event: message\ndata: {json.dumps(message_data)}\n\n"
            
#         # Return streaming response
#         return StreamingResponse(
#             generate_response(),
#             media_type="text/event-stream",
#             status_code=200
#         )
    
#     except RequestValidationError as e:
#         # Validation errors
#         logger.error(f"Validation error in temp chat endpoint for chat_id: {chat_id}: {e}")
#         return JSONResponse(
#             status_code=422,
#             content={"success": False, "error": "Your request couldn't be understood. Please check your input and try again."}
#         )
#     except HTTPException as e:
#         # Re-raise HTTP exceptions
#         raise
#     except Exception as e:
#         # All other errors
#         logger.error(f"Error in temp chat endpoint for chat_id: {chat_id}: {e}")
#         return JSONResponse(# async def _run(cmd: list[str], cwd: str | None = None) -> None:
#     proc = await asyncio.create_subprocess_exec(
#         *cmd, cwd=cwd,
#         stdout=asyncio.subprocess.PIPE,
#         stderr=asyncio.subprocess.PIPE,
#     )
#     out, err = await proc.communicate()
#     if proc.returncode != 0:
#         raise RuntimeError(f"Command failed: {cmd}\nstdout:\n{out.decode()}\nstderr:\n{err.decode()}")

# async def generate_pptx_content(
#     md_content: str, report_title: str, report_id: str, user_id: str, user_name: str
# ) -> Dict[str, str]:
#     try:
#         import markdown
#         from weasyprint import HTML
#     except ImportError as e:
#         raise RuntimeError("WeasyPrint and markdown must be installed") from e

#     tmpdir = tempfile.mkdtemp()
#     s3_client = get_s3_instance()  # if this is sync, we’ll wrap calls
#     s3_key_prefix = f"{S3_REPORTS_BASE_PATH}/{user_name}_{user_id}/raw_files_{report_id}"

#     try:
#         base = f"{report_id}-{uuid.uuid4().hex[:8]}"
#         md_file   = os.path.join(tmpdir, f"{base}.md")
#         pdf_file  = os.path.join(tmpdir, f"{base}.pdf")
#         docx_file = os.path.join(tmpdir, f"{base}.docx")

#         # async write markdown
#         async with aiofiles.open(md_file, "w", encoding="utf-8") as f:
#             await f.write(md_content)

#         # Try WeasyPrint first (run in threadpool)
#         weasy_ok = True
#         try:
#             html_content = markdown.markdown(md_content, extensions=["tables", "fenced_code", "extra"])
#             await run_in_threadpool(lambda: HTML(string=html_content).write_pdf(target=pdf_file))
#         except Exception as e:
#             logger.warning("WeasyPrint failed for report_id=%s: %s", report_id, e)
#             weasy_ok = False

#         if not weasy_ok:
#             # pandoc md->docx
#             await _run(["pandoc", md_file, "-o", docx_file])
#             # libreoffice docx->pdf
#             try:
#                 await _run(["libreoffice", "--headless", "--convert-to", "pdf", "--outdir", tmpdir, docx_file])
#                 generated_pdf = os.path.join(tmpdir, os.path.basename(docx_file).replace(".docx", ".pdf"))
#                 shutil.move(generated_pdf, pdf_file)
#             except Exception:
#                 # fallback: pandoc direct->pdf
#                 await _run(["pandoc", md_file, "-o", pdf_file, "--pdf-engine=xelatex"])

#         # Uploads (wrap sync client in threadpool)
#         md_s3_path  = f"{s3_key_prefix}/{report_title}.md"
#         pdf_s3_path = f"{s3_key_prefix}/{report_title}.pdf"

#         await run_in_threadpool(s3_client.upload_file, md_file, md_s3_path)
#         await run_in_threadpool(s3_client.upload_file, pdf_file, pdf_s3_path)

#         return {"md": md_s3_path, "pdf": pdf_s3_path}

#     finally:
#         try:
#             shutil.rmtree(tmpdir)
#         except Exception as cleanup_err:
#             logger.warning("Failed to cleanup %s: %s", tmpdir, cleanup_err)

#             status_code=500,
#             content={"success": False, "error": "We're experiencing technical difficulties. Please try again later."}
#         )



async def create_new_version_internal(report_id: str, session: AsyncSession) -> Dict[str, Any]:
    """
    Internal function to create a new report version when active version is already generated.
    
    Args:
        report_id: The report ID
        session: Database session
        
    Returns:
        Dict with success status, new version details, and card counts
    """
    try:
        # First, get the poster_image_url from Version 1's ReportVersion to preserve it
        version1_stmt = select(ReportVersion).where(
            ReportVersion.report_id == report_id,
            ReportVersion.version == 1
        )
        version1_result = await session.execute(version1_stmt)
        version1 = version1_result.scalar_one_or_none()
        
        existing_poster_url = None
        if version1 and version1.poster_image_url:
            existing_poster_url = version1.poster_image_url
            logger.info(f"Preserving poster_image_url from Version 1: {existing_poster_url}")
        else:
            logger.warning(f"No poster_image_url found in Version 1 for report {report_id}")
        
        # Detect which cards were modified
        logger.info(f"Detecting modified cards for report {report_id}")
        detection_result = await detect_modified_cards(report_id=report_id, session=session)
        
        if not detection_result.get('success'):
            logger.error(f"Failed to detect modified cards: {detection_result.get('error')}")
            return {
                "success": False,
                "error": "Failed to analyze report cards"
            }
        
        modified_cards_data = detection_result.get('modified_cards', [])
        unchanged_cards_data = detection_result.get('unchanged_cards', [])
        modified_count = detection_result.get('modified_count', 0)
        unchanged_count = detection_result.get('unchanged_count', 0)
        
        logger.info(f"Detection complete: {modified_count} modified, {unchanged_count} unchanged")
        
        # Combine both lists for create_new_report_version
        all_cards_data = modified_cards_data + unchanged_cards_data
        
        # Sort by sequence to maintain order
        all_cards_data.sort(key=lambda x: x.get("sequence", 0))
        
        # Create the new report version
        logger.info(f"Creating new version for report {report_id}")
        version_response = await create_new_report_version(
            session=session,
            report_id=report_id,
            cards_data=all_cards_data
        )
        
        if not version_response.get('success'):
            logger.error(f"Failed to create new report version: {version_response.get('error')}")
            return {
                "success": False,
                "error": "Failed to create new report version"
            }
        
        new_version = version_response.get('version')
        version_id = version_response.get('version_id')
        
        # Reset Report table's denormalized fields for new version
        logger.info(f"Resetting Report table fields for new version {new_version}")
        reset_result = await update_report(
            session=session,
            report_id=report_id,
            update_data={
                "generated_at": None,  # Reset - new version not generated yet
                "s3_uri": {"md": None, "pdf": None, "html": None, "pptx": None, "info_pdf": None, "md_explicit": False, "html_explicit": False},
                "status": ReportStatus.ANALYSIS_COMPLETED.value,
                "poster_image_url": existing_poster_url  # Preserve poster URL across versions
            }
        )
        
        if not reset_result.get('success'):
            logger.warning(f"Failed to reset Report fields: {reset_result.get('error')}")
            # Continue anyway - version was created successfully
        
        logger.info(f"Successfully created version {new_version} with ID {version_id}")
        
        return {
            "success": True,
            "version": new_version,
            "version_id": version_id,
            "modified_count": modified_count,
            "unchanged_count": unchanged_count
        }
        
    except Exception as e:
        logger.error(f"Error in create_new_version_internal: {str(e)}", exc_info=True)
        return {
            "success": False,
            "error": str(e)
        }


@router.post("/generate-report", response_model=GenerateReportResponse, status_code=200, responses={404: {"model": ErrorResponse}, 401: {"model": ErrorResponse}, 422: {"model": ErrorResponse}, 500: {"model": ErrorResponse}})
async def generate_report(data: GenerateReportRequest, user_id: str = Depends(get_current_active_user)):
    """
    Generate report endpoint - handles both initial generation (v1) and regeneration (v2+).
    
    **Auto-Detection:**
    - If active version not generated → Generates v1
    - If active version already generated → Creates new version automatically, then generates
    
    Frontend always calls this single endpoint - no need to know about versions!
    """
    logger.info(f"Generate report request received for user_id: {user_id} and report_id: {data.report_id}")
    report_id = data.report_id
    output_type = (data.output_type or "pdf").lower().strip()
    if output_type not in ("pdf", "html", "md"):
        return JSONResponse(
            status_code=422,
            content={"success": False, "error": "Invalid output_type. Must be 'pdf', 'html', or 'md'."}
        )
    logger.info(f"Output type requested: {output_type} for report_id: {report_id}")
    _INTERNAL_S3_KEYS = {"md_explicit", "html_explicit"}
    def _clean_s3_uri(uri: dict, for_output_type: str = None) -> dict:
        cleaned = {k: v for k, v in (uri or {}).items() if k not in _INTERNAL_S3_KEYS}
        if for_output_type:
            result = {for_output_type: cleaned.get(for_output_type)}
            time_key = f"{for_output_type}_generation_time"
            if cleaned.get(time_key):
                result[time_key] = cleaned[time_key]
            return result
        return cleaned
    
    try:
        async with async_session_scope() as session:
            # Check if user exists and credentials are valid
            db_response = await check_user_by_id(user_id=user_id, session=session)
            if not db_response.get('success', False):
                logger.error(f"Failed to check user by id: {db_response.get('error')} for user_id: {user_id} and report_id: {data.report_id}")
                return JSONResponse(
                    status_code=500,
                    content={"success": False, "error": "Something unexpected happened. Please try again later."}
                )
            if not db_response.get('exists', False):
                logger.error(f"User with ID {user_id} not found or token is invalid for report_id: {data.report_id}")
                return JSONResponse(
                    status_code=401,
                    content={"success": False, "error": "Unauthorized request"}
                )
        
        async with async_session_scope() as session:
            user_plan_result = await get_active_subscription(user_id=user_id, session=session)
            user_tier = user_plan_result.get("subscription", {}).get("current_tier", SubscriptionTier.FREE.value) if user_plan_result.get("subscription") else SubscriptionTier.FREE.value

        if output_type in ("md", "html") and user_tier == SubscriptionTier.FREE.value:
            logger.warning(f"Free-tier user {user_id} attempted to generate {output_type.upper()} for report_id: {report_id}")
            return JSONResponse(
                status_code=403,
                content={"success": False, "error": f"{output_type.upper()} export is available on paid plans. Please upgrade to use this feature."}
            )

        async with async_session_scope() as session:
            db_response = await get_user_details(user_id=user_id, session=session)
            if not db_response.get('success', False):
                logger.error(f"Database error: Failed to get user details: {db_response.get('error')} for user_id: {user_id} and report_id: {data.report_id}")
                return JSONResponse(
                    status_code=500,
                    content={"success": False, "error": "Something unexpected happened. Please try again later."}
                )

        user_name = db_response.get("user", {}).get('user_name', None)
        user_email = db_response.get("user", {}).get('email', None)

        async with async_session_scope() as session:
            ownership = await verify_report_ownership(report_id=data.report_id, user_id=user_id, session=session)
            if not ownership["authorized"]:
                return JSONResponse(
                    status_code=ownership["status_code"],
                    content={"success": False, "error": ownership["error"]}
                )

        report_data = ownership["report"]

        # Sanity check: If specific version requested, check if PDF already exists
        target_version_for_generation = None  # Will be set if generating for specific old version
        use_specific_version = False
        
        if data.report_version_id:
            async with async_session_scope() as session:
                version_stmt = select(ReportVersion).where(
                    ReportVersion.id == data.report_version_id,
                    ReportVersion.report_id == report_id
                )
                version_result = await session.execute(version_stmt)
                target_version = version_result.scalar_one_or_none()
                
                if not target_version:
                    logger.warning(f"Version not found: {data.report_version_id} for report_id: {report_id}")
                    return JSONResponse(
                        status_code=404,
                        content={"success": False, "error": "The requested version was not found."}
                    )
                
                s3_uri = target_version.s3_uri or {}
                explicit_key = f"{output_type}_explicit"  # e.g. "md_explicit", "html_explicit"
                
                if s3_uri.get(output_type):
                    # File exists in S3. Check if it was explicitly generated or a byproduct.
                    # For PDF: always treat as explicit (no flag needed).
                    # For md/html: check the *_explicit flag. Key absent = old entry = treat as explicit.
                    is_explicit = output_type == "pdf" or explicit_key not in s3_uri or s3_uri.get(explicit_key) == True
                    
                    if is_explicit:
                        logger.info(f"{output_type.upper()} already explicitly generated for version {target_version.version}, returning existing")
                        return JSONResponse(
                            status_code=200,
                            content={
                                "success": True,
                                "message": "Your report is up to date.",
                                "report_id": report_id,
                                "report_version_id": target_version.id,
                                "version": target_version.version,
                                "output_type": output_type,
                                "s3_uri": _clean_s3_uri(s3_uri, for_output_type=output_type),
                                "already_exists": True
                            }
                        )
                    else:
                        # File exists as byproduct — flip flag to explicit and return success
                        logger.info(f"{output_type.upper()} exists as byproduct for version {target_version.version}, marking as explicit")
                        target_version.s3_uri = {**s3_uri, explicit_key: True}
                        await session.commit()
                        
                        return JSONResponse(
                            status_code=200,
                            content={
                                "success": True,
                                "message": f"Report {output_type.upper()} has been generated successfully.",
                                "report_id": report_id,
                                "report_version_id": target_version.id,
                                "version": target_version.version,
                                "output_type": output_type,
                                "s3_uri": _clean_s3_uri(target_version.s3_uri, for_output_type=output_type),
                                "already_exists": False
                            }
                        )
                
                # Use this specific version for generation
                target_version_for_generation = target_version
                use_specific_version = True
                logger.info(f"Will generate {output_type.upper()} for specific version {target_version.version}")

        async def send_cloudwatch_logs():
            """Send cloudwatch logs for report generation events"""
            try:
                # Get chat_id from report_data
                chat_id = report_data.get('chat_id')
                if not chat_id:
                    logger.warning(f"No chat_id found for report_id: {report_id}")
                    return
                
                # Get chat title from chat_id
                chat_title = None
                async with async_session_scope() as session:
                    chat_response = await get_user_chat(user_id=user_id, chat_id=chat_id, session=session)
                    if chat_response.get('success', False) and chat_response.get('message'):
                        chat_title = chat_response.get('message', {}).get('chat_title')
                
                # Log "Report generation started"
                start_data = {
                    "event": "Report generation started",
                    "event_success": True,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "chat_id": chat_id,
                    "chat_title": chat_title or "N/A"
                }
                await insert_cloudwatch_logs(data=start_data, user_id=user_id)
                logger.info(f"Logged 'Report generation started' for report_id: {report_id}")
                
            except Exception as e:
                logger.error(f"Error in send_cloudwatch_logs start: {e} for report_id: {report_id}")

        # Start the cloudwatch logging in background
        asyncio.create_task(send_cloudwatch_logs())
        
        # Set status to GENERATING_OUTPUT when report generation starts
        async with async_session_scope() as session:
            await update_report_status_by_chat_or_report_id(
                report_id=report_id,
                status=ReportStatus.GENERATING_OUTPUT.value,
                session=session
            )
            logger.info(f"Set report status to GENERATING_OUTPUT for report_id: {report_id}")
        
        # Check if the ACTIVE VERSION is already generated
        # If YES: Check for changes before auto-creating new version
        # If NO: Generate the current version (first time generation)
        # Skip this logic if generating for a specific old version (report_version_id provided)
        
        # Track if a new version was created (important for s3_uri handling)
        new_version_created = False
        
        if use_specific_version and target_version_for_generation:
            # Generating for a specific old version - use that version directly
            current_version = target_version_for_generation.version
            target_version_id = target_version_for_generation.id
            logger.info(f"Generating for specific version {current_version} (version_id: {target_version_id}) for report_id: {report_id}")
        else:
            target_version_id = None  # Will be set after we determine the version
            async with async_session_scope() as session:
                version_stmt = select(ReportVersion).where(
                    ReportVersion.report_id == report_id,
                    ReportVersion.is_active.is_(True)
                )
                version_result = await session.execute(version_stmt)
                active_version = version_result.scalar_one_or_none()
                
                # Check if ANY output exists (not just generated_at)
                # This includes md/html generation times, info_pdf_generation_time and pptx_generation_time
                s3_uri_check = active_version.s3_uri or {} if active_version else {}
                has_any_output = (
                    (active_version and active_version.generated_at) or
                    s3_uri_check.get('md_generation_time') or
                    s3_uri_check.get('html_generation_time') or
                    s3_uri_check.get('info_pdf_generation_time') or
                    s3_uri_check.get('pptx_generation_time')
                )
                
                if has_any_output:
                    # Version already has some output → Check if user made changes first
                    logger.info(f"Version {active_version.version} already has output(s). Checking for changes before regeneration for report_id: {report_id}")
                    
                    # Check if user has made any changes since last generation
                    detection_result = await detect_modified_cards(report_id=report_id, session=session)
                    
                    if detection_result.get('success'):
                        modified_count = detection_result.get('modified_count', 0)
                        unchanged_count = detection_result.get('unchanged_count', 0)
                        
                        logger.info(f"Report regeneration - Change detection: {modified_count} modified, {unchanged_count} unchanged")
                        
                        if modified_count == 0:
                            # No changes made - check if requested output_type exists for this version
                            current_s3_uri = active_version.s3_uri or {}
                            explicit_key = f"{output_type}_explicit"
                            
                            if current_s3_uri.get(output_type):
                                is_explicit = output_type == "pdf" or explicit_key not in current_s3_uri or current_s3_uri.get(explicit_key) == True
                                
                                if is_explicit:
                                    logger.warning(f"{output_type.upper()} already explicitly generated for version {active_version.version} and no changes detected")
                                    return JSONResponse(
                                        status_code=200,
                                        content={
                                            "success": True,
                                            "message": "Your report is up to date.",
                                            "report_id": report_id,
                                            "report_version_id": active_version.id,
                                            "version": active_version.version,
                                            "output_type": output_type,
                                            "s3_uri": _clean_s3_uri(current_s3_uri, for_output_type=output_type),
                                            "generated_at": active_version.generated_at.isoformat() if active_version.generated_at else None,
                                            "modified": False,
                                            "already_exists": True
                                        }
                                    )
                                else:
                                    # File exists as byproduct — flip flag to explicit and return
                                    logger.info(f"{output_type.upper()} exists as byproduct for version {active_version.version}, marking as explicit")
                                    active_version.s3_uri = {**current_s3_uri, explicit_key: True}
                                    await session.commit()
                                    
                                    return JSONResponse(
                                        status_code=200,
                                        content={
                                            "success": True,
                                            "message": f"Report {output_type.upper()} has been generated successfully.",
                                            "report_id": report_id,
                                            "report_version_id": active_version.id,
                                            "version": active_version.version,
                                            "output_type": output_type,
                                            "s3_uri": _clean_s3_uri(active_version.s3_uri, for_output_type=output_type),
                                            "generated_at": active_version.generated_at.isoformat() if active_version.generated_at else None,
                                            "modified": False,
                                            "already_exists": False
                                        }
                                    )
                            else:
                                # Other output exists but not the requested type - generate for same version
                                current_version = active_version.version
                                target_version_id = active_version.id
                                logger.info(f"Generating {output_type.upper()} for existing version {current_version} (other outputs exist but not {output_type})")
                        else:
                            # User made changes, proceed with creating new version
                            logger.info(f"User made {modified_count} changes, proceeding to create new version for report")
                            
                            # Enforce per-tier version limit before creating a new version
                            _is_paid = user_tier != SubscriptionTier.FREE.value
                            _max_versions = MAX_REPORT_VERSIONS_PAID if _is_paid else MAX_REPORT_VERSIONS_FREE
                            if active_version.version >= _max_versions:
                                _upgrade_hint = "" if _is_paid else " Upgrade to a paid plan to unlock up to 5 versions."
                                logger.warning(f"User {user_id} hit version limit ({_max_versions}) for report_id: {report_id}")
                                return JSONResponse(
                                    status_code=403,
                                    content={
                                        "success": False,
                                        "error": f"This report has reached the maximum of {_max_versions} versions.{_upgrade_hint}"
                                    }
                                )
                            
                            # Create new version
                            new_version_result = await create_new_version_internal(
                                report_id=report_id,
                                session=session
                            )
                            
                            if not new_version_result.get('success'):
                                logger.error(f"Failed to create new version: {new_version_result.get('error')}")
                                return JSONResponse(
                                    status_code=500,
                                    content={
                                        "success": False,
                                        "error": "Failed to create new version for regeneration. Please try again."
                                    }
                                )
                            
                            current_version = new_version_result.get('version')
                            target_version_id = new_version_result.get('version_id')
                            modified_count = new_version_result.get('modified_count', 0)
                            unchanged_count = new_version_result.get('unchanged_count', 0)
                            new_version_created = True  # Flag that new version was created
                            
                            logger.info(f"New version {current_version} created successfully. Modified: {modified_count}, Unchanged: {unchanged_count}")
                    else:
                        # If detection fails, proceed anyway (don't block user)
                        logger.warning(f"Could not detect changes for report regeneration: {detection_result.get('error')}, proceeding anyway")
                        
                        # Enforce per-tier version limit before creating a new version
                        _is_paid = user_tier != SubscriptionTier.FREE.value
                        _max_versions = MAX_REPORT_VERSIONS_PAID if _is_paid else MAX_REPORT_VERSIONS_FREE
                        if active_version.version >= _max_versions:
                            _upgrade_hint = "" if _is_paid else " Upgrade to a paid plan to unlock up to 5 versions."
                            logger.warning(f"User {user_id} hit version limit ({_max_versions}) for report_id: {report_id}")
                            return JSONResponse(
                                status_code=403,
                                content={
                                    "success": False,
                                    "error": f"This report has reached the maximum of {_max_versions} versions.{_upgrade_hint}"
                                }
                            )
                        
                        # Create new version anyway
                        new_version_result = await create_new_version_internal(
                            report_id=report_id,
                            session=session
                        )
                        
                        if not new_version_result.get('success'):
                            logger.error(f"Failed to create new version: {new_version_result.get('error')}")
                            return JSONResponse(
                                status_code=500,
                                content={
                                    "success": False,
                                    "error": "Failed to create new version for regeneration. Please try again."
                                }
                            )
                        
                        current_version = new_version_result.get('version')
                        target_version_id = new_version_result.get('version_id')
                        new_version_created = True  # Flag that new version was created
                else:
                    # First time generation for this version
                    current_version = active_version.version if active_version else 1
                    target_version_id = active_version.id if active_version else None
                    logger.info(f"First time generation for report_id: {report_id}, version: {current_version}")

        # Get cards - use version-specific cards if generating for old version
        async with async_session_scope() as session:
            if use_specific_version and target_version_for_generation:
                # Get cards linked to the specific version
                db_response = await get_cards_for_version(
                    report_id=report_id,
                    version_id=target_version_for_generation.id,
                    session=session
                )
                logger.info(f"Using cards from version snapshot for version {target_version_for_generation.version}")
            else:
                # Get current active cards
                db_response = await get_report_cards(report_id=report_id, session=session)
            
            if not db_response.get('success', False):
                logger.error(f"Failed to get report cards: {db_response.get('error')} for report_id: {report_id}")
                return JSONResponse(
                    status_code=500,
                    content={"success": False, "error": "Something unexpected happened. Please try again later."}
                )
        
        cards = db_response.get('cards', [])
        if not cards:
            logger.error(f"No report cards found for report_id: {report_id}")
            return JSONResponse(
                status_code=404,
                content={"success": False, "error": "Report not found"}
            )

        report_title = report_data.get('title', '')

        report_cards = []
        for card in cards:
            if card.get('type') == 'title':
                report_cards.append({
                    "section": [
                        {
                            "name": "title",
                            "content": extract_content(card, "title"),
                            "tables": [{"visualization": "", "table_id": "", "table_title": "", "s3_uri": ""}]
                        }
                    ],
                    "sub_sections": [],
                    "citations": {},
                    "summary": ""
                })

            elif card.get('type') == 'subtitle':
                report_cards.append({
                    "section": [
                        {
                            "name": "subtitle",
                            "content": extract_content(card, "content"),
                            "tables": [{"visualization": "", "table_id": "", "table_title": "", "s3_uri": ""}]
                        }
                    ],
                    "sub_sections": [],
                    "citations": {},
                    "summary": ""
                })

            elif card.get('type') == 'toc':
                report_cards.append({
                    "section": [
                        {
                            "name": "table_of_contents",
                            "content": extract_content(card, "content"),
                            "tables": [{"visualization": "", "table_id": "", "table_title": "", "s3_uri": ""}]
                        }
                    ],
                    "sub_sections": [],
                    "citations": {},
                    "summary": ""
                })

            elif card.get('type') == 'es':
                report_cards.append({
                    "section": [
                        {
                            "name": "executive_summary",
                            "content": extract_content(card, "content"),
                            "tables": [{"visualization": "", "table_id": "", "table_title": "", "s3_uri": ""}]
                        }
                    ],
                    "sub_sections": [],
                    "citations": {},
                    "summary": ""
                })

            elif card.get('type') == 'section':
                if card.get('section') and isinstance(card.get('section'), list):
                    report_cards.append({
                        "section": card.get("section", []),
                        "sub_sections": card.get("sub_sections", []),
                        "citations": card.get("citations", {}),
                        "summary": card.get("summary", "")
                    })
                else:
                    report_cards.append({
                        "section": [
                            {
                                "name": card.get("title", ""),
                                "content": extract_content(card, "content"),
                                "tables": [{"visualization": "", "table_id": "", "table_title": "", "s3_uri": ""}]
                            }
                        ],
                        "sub_sections": card.get("sub_sections", []),
                        "citations": card.get("citations", {}),
                        "summary": card.get("summary", "")
                    })
            else:
                logger.error(f"Unknown card type: {card.get('type')} for report_id: {report_id} at sequence: {card.get('sequence')}")
                continue
            
        # Use report title only in the file name (no report_id for better UX)
        base_filename = report_title.strip(".").strip().replace(":", " ").replace("-", " ").replace(" ", "_")
        base_filename = re.sub(r"_+", "_", base_filename)  # collapse multiple underscores

        logger.info(f"Getting table_id_markdown_map for report_id: {report_id}")
        async with async_session_scope() as session:
            table_id_map = await get_table_id_markdown_map(report_id=report_id, session=session)
            if not table_id_map.get('success', False):
                logger.error(f"Failed to get table_id_markdown_map: {table_id_map.get('error')} for report_id: {report_id}")
                return JSONResponse(
                    status_code=500,
                    content={"success": False, "error": "Internal server error"}
                )
            table_id_map = table_id_map.get('table_id_markdown_map', {})
            
        report_type = report_data.get("report_type") or "study"

        # Get existing poster_image_url if it exists (needed for PDF and HTML output)
        existing_poster_url = None
        
        if output_type in ("pdf", "html"):
            async with async_session_scope() as session:
                report_stmt = select(Report).where(Report.id == report_id)
                report_result = await session.execute(report_stmt)
                report = report_result.scalar_one_or_none()
                if report and report.report_type:
                    report_type = report.report_type
                if report and report.domain_name == "due_diligence" and report_type == "brief":
                    report_type = "study"
                    await update_report(
                        report_id=report_id,
                        update_data={"report_type": report_type},
                        session=session,
                    )
                    logger.warning(
                        "Corrected invalid persisted due_diligence report_type='brief' "
                        f"to 'study' before output generation for report_id: {report_id}"
                    )
                
                if report and report.poster_image_url:
                    existing_poster_url = report.poster_image_url
                    logger.info(f"Found existing poster URL in Report table: {existing_poster_url}")
                elif current_version > 1:
                    version1_stmt = select(ReportVersion).where(
                        ReportVersion.report_id == report_id,
                        ReportVersion.version == 1
                    )
                    version1_result = await session.execute(version1_stmt)
                    version1 = version1_result.scalar_one_or_none()
                    
                    if version1 and version1.poster_image_url:
                        existing_poster_url = version1.poster_image_url
                        logger.info(f"Found existing poster URL from Version 1's ReportVersion: {existing_poster_url}")
                    else:
                        logger.warning(f"No poster_image_url found in Version 1's ReportVersion for report_id {report_id}, will generate new poster")
                else:
                    logger.info(f"Version 1 generation - no existing poster found, will create new poster image")
            
            logger.info(f"Existing poster URL for report_id {report_id}: {existing_poster_url} (version: {current_version})")
        
        # Get existing s3_uri for the resolved version (for reuse logic in generate_report_output)
        _existing_s3 = {}
        if target_version_id:
            async with async_session_scope() as session:
                _ver_stmt = select(ReportVersion).where(ReportVersion.id == target_version_id)
                _ver_result = await session.execute(_ver_stmt)
                _ver_obj = _ver_result.scalar_one_or_none()
                if _ver_obj:
                    _existing_s3 = _ver_obj.s3_uri or {}
        
        _reuse_keys = [k for k in ("md", "html") if _existing_s3.get(k)]
        logger.info(
            f"Calling generate_report_output for report_id: {report_id}, version: {current_version}, "
            f"output_type: {output_type}, reusable_intermediates: {_reuse_keys or 'none'}, "
            f"existing_poster: {bool(existing_poster_url)}, report_type: {report_type}"
        )
        generation_result = await generate_report_output(
            report_cards=report_cards,
            table_id_map=table_id_map,
            user_id=user_id,
            user_name=user_name,
            report_id=report_id,
            base_filename=base_filename,
            report_title=report_title,
            output_type=output_type,
            existing_s3_uri=_existing_s3,
            chat_id=report_data.get('chat_id'),
            chat_title=report_title,
            version=current_version,
            existing_poster_url=existing_poster_url,
            report_type=report_type
        )
        logger.info(
            f"generate_report_output completed for report_id: {report_id}, version: {current_version}, "
            f"output_type: {output_type}, success: {generation_result.get('success')}"
        )
        
        if not generation_result.get("success"):
            logger.error(f"generate_report_output failed for user {user_id} and report_id: {report_id}: {generation_result}")
            return JSONResponse(
                status_code=500,
                content={"success": False, "error": "Something unexpected happened. Please try again later."}
            )
        
        result_data = generation_result["data"]
        s3_results = result_data["s3_paths"]
        attachment_data = result_data["attachment_data"]
        # Render-service JSON may leave this as an ISO string.
        raw_generation_time = result_data.get("report_generation_time")
        if isinstance(raw_generation_time, datetime):
            final_report_generation_time = raw_generation_time
        elif isinstance(raw_generation_time, str):
            final_report_generation_time = datetime.fromisoformat(
                raw_generation_time.replace("Z", "+00:00")
            )
        else:
            logger.warning(
                f"report_generation_time missing/unparseable ({raw_generation_time!r}) "
                f"for report_id: {report_id}; falling back to current time"
            )
            final_report_generation_time = datetime.now(timezone.utc)
        poster_image_url = result_data["poster_image_url"]

        uploaded_file_types = []
        s3_paths = {}
        file_types = ['pdf', 'md', 'html']
        for file_type, path in s3_results.items():
            if file_type in file_types and path:
                uploaded_file_types.append(file_type)
                s3_paths[file_type] = path

        if output_type not in uploaded_file_types:
            logger.error(f"Requested {output_type} not uploaded to s3 for user {user_id} and report_id: {report_id}")
            return JSONResponse(
                status_code=500,
                content={"success": False, "error": "Something unexpected happened. Please try again later."}
            )
        
        # Add generation time markers to s3_paths for md/html
        if s3_paths.get('md') and output_type in ('md', 'html'):
            s3_paths['md_generation_time'] = final_report_generation_time.isoformat()
        if s3_paths.get('html') and output_type in ('html',):
            s3_paths['html_generation_time'] = final_report_generation_time.isoformat()
        # For pdf output_type: process_report_cards generates all three so mark them all
        if output_type == 'pdf':
            if s3_paths.get('md'):
                s3_paths['md_generation_time'] = final_report_generation_time.isoformat()
            if s3_paths.get('html'):
                s3_paths['html_generation_time'] = final_report_generation_time.isoformat()
        
        # Set explicit flag for the requested output_type.
        # For byproducts: set *_explicit = False so old records (missing the key)
        # don't incorrectly appear as explicit in version history.
        # Guard: never downgrade an existing True to False (user already requested it before).
        if output_type == 'md':
            s3_paths['md_explicit'] = True
            if not _existing_s3.get('html') and _existing_s3.get('html_explicit') is not True:
                s3_paths['html_explicit'] = False
        elif output_type == 'html':
            s3_paths['html_explicit'] = True
            if s3_paths.get('md') and _existing_s3.get('md_explicit') is not True:
                s3_paths['md_explicit'] = False
        elif output_type == 'pdf':
            if s3_paths.get('md') and _existing_s3.get('md_explicit') is not True:
                s3_paths['md_explicit'] = False
            if s3_paths.get('html') and _existing_s3.get('html_explicit') is not True:
                s3_paths['html_explicit'] = False
        
        # Email notification only for PDF output
        if output_type == "pdf":
            if attachment_data:
                attachment_data = [data for data in attachment_data if data.get('mime_type') == 'application/pdf']
            else:
                logger.warning(f"No attachment data for report_id: {report_id} (PDF generated from cached intermediates) — skipping email")
        
        if output_type == "pdf" and attachment_data and report_title and user_email:
            logger.info(f"Preparing data for email notification for user {user_id} and report_id: {report_id}")
            async def send_email_background():
                try:
                    is_email_sent = await run_in_threadpool(lambda: send_report_notification_email(
                        recipient_email=user_email,
                        user_name=user_name,
                        report_title=report_title,
                        attachment_data=attachment_data,
                        report_generation_time=final_report_generation_time
                    ))
                    if is_email_sent:
                        # Get chat_id and chat_title for cloudwatch logging
                        chat_id = report_data.get('chat_id')
                        chat_title = None
                        if chat_id:
                            async with async_session_scope() as session:
                                chat_response = await get_user_chat(user_id=user_id, chat_id=chat_id, session=session)
                                if chat_response.get('success', False) and chat_response.get('message'):
                                    chat_title = chat_response.get('message', {}).get('chat_title')
                        
                        cloudwatch_data = {
                            "event": "Report sent via email",
                            "event_success": True,
                            "timestamp": final_report_generation_time.isoformat(),
                            "chat_id": chat_id or "N/A",
                            "chat_title": chat_title or "N/A"
                        }
                        try:
                            await insert_cloudwatch_logs(data=cloudwatch_data, user_id=user_id)
                        except Exception as e:
                            logger.error(f"Error in inserting cloudwatch logs: {e} for user_id: {user_id} and chat_id: {chat_id}")
                        logger.info(f"Email sent successfully to user {user_name} with user_id {user_id} and report_id: {report_id}")
                    else:
                        logger.error(f"Failed to send email to user {user_name} with user_id {user_id} and report_id: {report_id}")
                except Exception as e:
                    logger.error(f"Error in sending email to user {user_name} with user_id {user_id} and report_id: {report_id}: {e}")

            asyncio.create_task(send_email_background())

        # Update database with generated output paths
        # IMPORTANT: When generating for a specific OLD (non-active) version, only update that version's s3_uri
        # When generating for the ACTIVE version (whether report_version_id provided or not), update BOTH tables
        async with async_session_scope() as session:
            # Check if the provided version is the active version
            is_generating_for_old_version = (
                use_specific_version and 
                target_version_for_generation and 
                not target_version_for_generation.is_active
            )
            
            if is_generating_for_old_version:
                # ========================================
                # FLOW A: Generating for a SPECIFIC OLD (non-active) version
                # Update only the specific version's s3_uri, NOT the Report table
                # ========================================
                logger.info(f"Updating s3_uri for specific OLD version {current_version} (NOT updating Report table)")
                db_response = await update_specific_version_s3_uri(
                    report_id=report_id,
                    version_id=target_version_for_generation.id,
                    s3_uri_updates=s3_paths,
                    generated_at=final_report_generation_time if output_type == "pdf" else None,
                    session=session
                )
                if not db_response.get('success', False):
                    logger.error(f"Database error: Failed to update specific version s3_uri: {db_response.get('error')} for report_id: {report_id}")
                    return JSONResponse(
                        status_code=500,
                        content={"success": False, "error": "Something unexpected happened. Please try again later."}
                    )
                logger.info(f"Generated {output_type.upper()} for specific old version {current_version} - cards already linked from version snapshot")
            else:
                # ========================================
                # FLOW B: Generating for CURRENT/ACTIVE version
                # Update both Report table and active ReportVersion
                # ========================================
                updated_report_data = {
                    "s3_uri": s3_paths,
                    "status": ReportStatus.OUTPUT_GENERATED.value,
                }
                if output_type in ("pdf", "html"):
                    if output_type == "pdf":
                        updated_report_data["generated_at"] = final_report_generation_time
                    if poster_image_url:
                        updated_report_data["poster_image_url"] = poster_image_url
                db_response = await update_report(report_id=report_id, update_data=updated_report_data, session=session)
                if not db_response.get('success', False):
                    logger.error(f"Database error: Failed to update report: {db_response.get('error')} for user_id: {user_id} and report_id: {report_id}")
                    return JSONResponse(
                        status_code=500,
                        content={"success": False, "error": "Something unexpected happened. Please try again later."}
                    )
                
                # Finalize the report version by linking current cards
                # For version 1 (first time generation): Call finalize_report_version to populate report_version_cards
                # For version 2+: Already populated by create_new_report_version, skip finalization
                if current_version == 1:
                    logger.info(f"Finalizing version 1 for report_id: {report_id}")
                    finalize_result = await finalize_report_version(
                        session=session,
                        report_id=report_id
                    )
                    
                    if finalize_result.get('success'):
                        cards_linked = finalize_result.get('cards_linked', 0)
                        already_finalized = finalize_result.get('already_finalized', False)
                        if already_finalized:
                            logger.info(f"Version 1 already finalized for report_id: {report_id}")
                        else:
                            logger.info(f"Successfully finalized version 1 with {cards_linked} cards for report_id: {report_id}")
                    else:
                        logger.warning(f"Failed to finalize version 1: {finalize_result.get('error')} for report_id: {report_id}")
                        logger.warning("Report generation succeeded but version finalization failed - this is not critical")
                else:
                    logger.info(f"Version {current_version} already finalized during version creation - skipping finalize call")
                
        # if raw_md_content:
        #     try:
        #         async def publish_report_background():
        #             try:
        #                 publish_report_data = await publish_report(raw_md_content=raw_md_content, user_id=user_id, user_name=user_name, report_title=report_title)
        #                 publish_data = {
        #                     **publish_report_data,
        #                     "report_id": report_id,
        #                     "page_count": page_count,
        #                     "table_count": table_count,
        #                     "viz_count": viz_count,
        #                     "source_count": len(report_data.get('citations', []))
        #                 }
        #                 if publish_report_data:
        #                     async with async_session_scope() as session:
        #                         db_response = await insert_publish_details(publish_data=publish_data, session=session)
        #                         if not db_response.get('success', False):
        #                             logger.error(f"Database error: Failed to insert publish details: {db_response.get('error')} for user_id: {user_id} and report_id: {report_id}")
        #             except Exception as e:
                   
        #                 logger.error(f"Error in publishing report: {e} for user_id: {user_id} and report_id: {report_id}")
        #         # TODO: Currently disabled due to increased costing of heygen
        #         # asyncio.create_task(publish_report_background())
        #     except Exception as e:
        #         logger.error(f"Error in publishing report: {e} for user_id: {user_id} and report_id: {report_id}")

        # Log "Report generation completed" in background
        async def log_completion():
            try:
                # Get chat_id and chat_title
                chat_id = report_data.get('chat_id')
                chat_title = None
                if chat_id:
                    async with async_session_scope() as session:
                        chat_response = await get_user_chat(user_id=user_id, chat_id=chat_id, session=session)
                        if chat_response.get('success', False) and chat_response.get('message'):
                            chat_title = chat_response.get('message', {}).get('chat_title')
                
                completion_data = {
                    "event": "Report generation completed",
                    "event_success": True,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "chat_id": chat_id or "N/A",
                    "chat_title": chat_title or "N/A"
                }
                await insert_cloudwatch_logs(data=completion_data, user_id=user_id)
                logger.info(f"Logged 'Report generation completed' for report_id: {report_id}")
            except Exception as e:
                logger.error(f"Error logging completion: {e} for report_id: {report_id}")
        
        asyncio.create_task(log_completion())
        
        # Set status to OUTPUT_GENERATED when report generation completes successfully
        async with async_session_scope() as session:
            await update_report_status_by_chat_or_report_id(
                report_id=report_id,
                status=ReportStatus.OUTPUT_GENERATED.value,
                session=session
            )
            logger.info(f"Set report status to OUTPUT_GENERATED for report_id: {report_id}")

        # Get the final version ID if not already set
        final_version_id = target_version_id
        if not final_version_id and use_specific_version and target_version_for_generation:
            final_version_id = target_version_for_generation.id
        elif not final_version_id:
            # Need to get the version ID from the database
            async with async_session_scope() as session:
                version_stmt = select(ReportVersion).where(
                    ReportVersion.report_id == report_id,
                    ReportVersion.version == current_version
                )
                version_result = await session.execute(version_stmt)
                version_obj = version_result.scalar_one_or_none()
                if version_obj:
                    final_version_id = version_obj.id
        
        msg = f"Report {output_type.upper()} has been generated successfully."
        if output_type == "pdf":
            msg += " Please check your email for the report."
        return GenerateReportResponse(
            success=True,
            message=msg,
            report_id=report_id,
            report_version_id=final_version_id,
            version=current_version,
            output_type=output_type,
            s3_uri=_clean_s3_uri(s3_paths, for_output_type=output_type),
            modified=True,
            already_exists=False
        )
            
    except Exception as e:
        logger.error(f"Error in generate report endpoint: {e}")
        # Update report status to ERROR_GENERATION_REPORT on error
        try:
            async with async_session_scope() as session:
                await update_report_status_by_chat_or_report_id(
                    report_id=report_id,
                    status=ReportStatus.ERROR_GENERATION_REPORT.value,
                    session=session
                )
                logger.info(f"Set report status to ERROR_GENERATION_REPORT for report_id: {report_id}")
        except Exception as update_error:
            logger.error(f"Failed to update report status to ERROR_GENERATION_REPORT: {update_error}")
        
        return JSONResponse(
            status_code=500,
            content={"success": False, "error": "Something unexpected happened. Please try again later."}
        )

@router.get("/chat/{chat_id}", response_model=PreviousChatMessagesResponse, status_code=200, responses={404: {"model": ErrorResponse}, 401: {"model": ErrorResponse}, 422: {"model": ErrorResponse}, 500: {"model": ErrorResponse}})
async def get_chat_messages(chat_id: str, user_id: str = Depends(get_current_active_user)):
    """Get chat messages endpoint"""
    logger.info(f"Get chat messages request received for user_id: {user_id} and chat_id: {chat_id}")
    try:
        async with async_session_scope() as session:
            # Check if user exists and credentials are valid
            db_response = await check_user_by_id(user_id=user_id, session=session)
            if not db_response.get('success', False):
                logger.error(f"Failed to check user by id: {db_response.get('error')} for user_id: {user_id}")
                return JSONResponse(
                    status_code=500,
                    content={"success": False, "error": "I'm having an issue verifying your account for this chat. Please refresh."}
                )
            if not db_response.get('exists', False):
                logger.error(f"User with ID {user_id} not found or token is invalid")
                return JSONResponse(
                    status_code=401,
                    content={"success": False, "error": "I can't find an account associated with this chat. Please log in again to view your history."}
                )
            
            db_response = await get_user_chat(user_id=user_id, chat_id=chat_id, session=session)
            if not db_response.get('success', False):
                logger.error(f"Database error: Failed to get user chats: {db_response.get('error')} for user_id: {user_id}, chat_id: {chat_id}")
                return JSONResponse(
                    status_code=500,
                    content={"success": False, "error": "I'm unable to load your conversation due to a system error. Please try again."}
                )

            processing_user_message = await redis_instance.redis_client.get(f"chat:{chat_id}:processing_user_message")
            if processing_user_message:
                processing_user_message = [json.loads(processing_user_message)]
            else:
                processing_user_message = []
            
            chat_messages = db_response.get('message', {}).get('chat_messages', [])
            message_citations = db_response.get('message', {}).get('message_citations', {}) or {}
            if not chat_messages:
                logger.warning(f"No chat messages found for chat_id: {chat_id}, user_id: {user_id}")
                return PreviousChatMessagesResponse(
                    success=True,
                    messages=processing_user_message,
                    reports=[],
                    turn_in_progress=bool(processing_user_message),
                )
                # return JSONResponse(
                #     status_code=404,
                #     content={"success": False, "error": "No chat messages found"}
                # )
            
            formatted_chat_messages = []
            for i, msg in enumerate(chat_messages):
                msg_type = msg['type']
                msg_content = msg['content']

                if msg_type == 'human':
                    msg_object = {'type': msg_type, 'content': msg_content}
                    formatted_chat_messages.append(msg_object)
                elif msg_type == 'ai':
                    msg_metadata = msg.get('response_metadata', {})
                    # msg_stop = (
                    #     msg_metadata.get('stopReason', '')
                    #     or msg_metadata.get('stop_reason', '')
                    #     or msg_metadata.get('finish_reason', '')
                    # )
                    # if msg_stop in ('tool_use', 'tool_calls'):
                    #     called_names = [tc.get('name', '') for tc in msg.get('tool_calls', [])]
                    #     if 'retrieve' not in called_names:
                    #         continue
                    if i > 0 and chat_messages[i-1].get('type') == 'tool' and chat_messages[i-1].get('name') == 'retrieve':
                        continue
                    if isinstance(msg_content, str):
                        msg_object = {'type': msg_type, 'content': msg_content}
                    elif isinstance(msg_content, list) and isinstance(msg_content[0], dict) and 'text' in msg_content[0]:
                        msg_object = {'type': msg_type, 'content': msg_content[0]['text']}
                    else:
                        continue
                    msg_id = msg.get('id')
                    if msg_id and msg_id in message_citations:
                        msg_object['citations'] = message_citations[msg_id]
                    formatted_chat_messages.append(msg_object)
            
            db_response = await get_chat_reports_and_cards(chat_id=chat_id, session=session)
            if not db_response.get('success', False):
                logger.error(f"Database error: Failed to get chat report: {db_response.get('error')} for user_id: {user_id}, chat_id: {chat_id}")
                return JSONResponse(
                    status_code=500,
                    content={"success": False, "error": "something unexpected happened. Please try again later."}
                )
            
            reports_with_cards = db_response.get('reports', [])

            await replace_visualization_uris_in_reports(reports_with_cards, redis_instance.redis_client)

            return PreviousChatMessagesResponse(
                success=True,
                messages=formatted_chat_messages + processing_user_message,
                reports=reports_with_cards,
                turn_in_progress=bool(processing_user_message),
            )
    
    except RequestValidationError as e:
        # Validation errors (like missing fields) return 422
        logger.error(f"Validation error in get chat messages endpoint: {e} for user_id: {user_id}, chat_id: {chat_id}")
        return JSONResponse(
            status_code=422,
            content={"success": False, "error": "Your request couldn't be understood. Please check your input and try again."}
        )
    except HTTPException as e:
        # Re-raise HTTP exceptions to let the global handler handle them
        raise
    except Exception as e:
        # All other errors return 500
        logger.error(f"Error in get chat messages endpoint: {e} for user_id: {user_id}, chat_id: {chat_id}")
        return JSONResponse(
            status_code=500,
            content={"success": False, "error": "Something unexpected happened. Please try again later."}
        )

@router.get("/chat-list", response_model=UserChatsResponse, status_code=200, responses={404: {"model": ErrorResponse}, 401: {"model": ErrorResponse}, 422: {"model": ErrorResponse}, 500: {"model": ErrorResponse}})
async def chat_list(
    user_id: str = Depends(get_current_active_user),
    status: Optional[str] = Query(None, description="Comma-separated status values to filter by (e.g., 'draft,analysis-completed')"),
    search: Optional[str] = Query(None, description="Search term to filter chat titles (case-insensitive substring match)")
):
    """
    Get user chats endpoint with optional status filtering and search
    
    Query Parameters:
    - status: Optional comma-separated list of status values to filter by
    - search: Optional search term to filter chat titles (case-insensitive)
    
    Examples:
    - /chat-list (returns all chats)
    - /chat-list?status=draft (returns only draft chats)
    - /chat-list?search=AI (returns chats with "AI" in title)
    - /chat-list?search=wealth&status=draft,analysis-completed (returns chats matching both filters)
    
    Valid status values:
    - draft
    - awaiting-confirmation
    - analysis-in-progress
    - analysis-completed
    - generating-output
    - output-generated
    - updates-available
    - redo-analysis
    - error-generation-report
    """
    logger.info(f"Get user chats request received for user_id: {user_id}, status filter: {status}, search: {search}")
    try:        
        async with async_session_scope() as session:
            # Check if user exists and credentials are valid
            db_response = await check_user_by_id(user_id=user_id, session=session)
            if not db_response.get('success', False):
                logger.error(f"Failed to check user by id: {db_response.get('error')}")
                return JSONResponse(
                    status_code=500,
                    content={"success": False, "error": "I couldn't verify your account to load the chat list. Please refresh and try again."}
                )
            if not db_response.get('exists', False):
                logger.error(f"User with ID {user_id} not found or token is invalid")
                return JSONResponse(
                    status_code=401,
                    content={"success": False, "error": "I can't seem to find your account. Please log in to see your chat list."}
                )
            
            # Get all chats for the user
            db_response = await get_user_chats(user_id=user_id, session=session)
            if not db_response.get('success', False):
                logger.error(f"Database error: Failed to get user chats: {db_response.get('error')}")
                return JSONResponse(
                    status_code=500,
                    content={"success": False, "error": "I'm having trouble retrieving your chats right now. Please refresh and try again."}
                )
            
            chats = db_response.get('chats', [])
            if not chats:
                logger.info(f"No chats found for user_id: {user_id}")
                return UserChatsResponse(
                    success=True,
                    chats=None
                )

            # Parse status filter if provided
            status_filter = None
            if status:
                # Split comma-separated values and strip whitespace
                status_filter = [s.strip() for s in status.split(',') if s.strip()]
                logger.info(f"Filtering chats by status: {status_filter}")
            
            # Parse search query if provided
            search_query = search.strip().lower() if search else None
            if search_query:
                logger.info(f"Filtering chats by search query: {search_query}")

            chats_with_reports = []
            # Sort chats by created_at in descending order (newest first)
            sorted_chats = sorted(chats, key=lambda x: x['created_at'], reverse=True)
            
            async with async_session_scope() as session:
                for chat in sorted_chats:
                    session_id = await redis_instance.redis_client.get(f"chat_session:{chat['id']}")
                    if session_id and isinstance(session_id, str):
                        session_id = session_id.split(':')[-1]
                    
                    # Get report status for this chat
                    chat_status = ReportStatus.DRAFT.value  # Default for chats without reports
                    chat_image = None # TODO: Add chat image
                    
                    # Query for the latest report associated with this chat (order by created_at desc)
                    #This I have to do because before one chat can have multiple reports else I would have use scaler or none
                    report_stmt = (
                        select(Report)
                        .where(Report.chat_id == chat['id'])
                        .order_by(Report.created_at.desc())
                        .limit(1)
                    )
                    report_result = await session.execute(report_stmt)
                    report = report_result.scalar_one_or_none()
                    
                    if report:
                        chat_status = report.status
                        chat_image = report.poster_image_url
                    
                    # Apply status filter if provided
                    if status_filter and chat_status not in status_filter:
                        # Skip this chat if it doesn't match the filter
                        continue
                    
                    # Apply search filter (case-insensitive substring match)
                    if search_query and search_query not in chat['chat_title'].lower():
                        # Skip this chat if title doesn't match search query
                        continue
                    
                    chats_with_reports.append(
                        {
                            'chat_id': chat['id'],
                            'chat_title': chat['chat_title'],
                            'chat_image': chat_image,
                            'chat_status': chat_status,
                            'updated_at': chat['updated_at'],
                            'created_at': chat['created_at'],
                            'session_id': session_id,
                        }
                    )
            

            return UserChatsResponse(
                success=True,
                chats=chats_with_reports
            )
                
    except RequestValidationError as e:
        # Validation errors (like missing fields) return 422
        logger.error(f"Validation error in get user chats endpoint: {e}")
        return JSONResponse(
            status_code=422,
            content={"success": False, "error": "Your request couldn't be understood. Please check your input and try again."}
        )
    except HTTPException as e:
        # Re-raise HTTP exceptions to let the global handler handle them
        raise
    except Exception as e:
        # All other errors return 500
        logger.error(f"Error in get user chats endpoint: {e}")
        return JSONResponse(
            status_code=500,
            content={"success": False, "error": "An unexpected error occurred while loading your chat list. Our team has been alerted."}
        )

@router.post("/delete-chat", response_model=DeleteChatResponse, status_code=200, responses={404: {"model": ErrorResponse}, 401: {"model": ErrorResponse}, 422: {"model": ErrorResponse}, 500: {"model": ErrorResponse}})
async def delete_chat(data: DeleteChatRequest, user_id: str = Depends(get_current_active_user)):
    """Mark a chat as deleted"""
    logger.info(f"Delete chat request received for user_id: {user_id}, chat_id: {data.chat_id}")
    
    try:
        # Validate input data
        if not data.chat_id:
            logger.error("Missing required field: chat_id")
            return JSONResponse(
                status_code=422,
                content={"success": False, "error": "I'll need a chat ID to know which conversation to delete."}
            )
        
        async with async_session_scope() as session:
            # Check if user exists and credentials are valid
            db_response = await check_user_by_id(user_id=user_id, session=session)
            if not db_response.get('success', False):
                logger.error(f"Failed to check user by id: {db_response.get('error')}")
                return JSONResponse(
                    status_code=500,
                    content={"success": False, "error": "I've run into a technical problem on my end. Please try that again in a moment."}
                )
            if not db_response.get('exists', False):
                logger.error(f"User with ID {user_id} not found or token is invalid")
                return JSONResponse(
                    status_code=401,
                    content={"success": False, "error": "Your session seems to have expired. Please log in again to make changes."}
                )
            
            # Mark the chat as deleted
            db_response = await mark_chat_deleted(chat_id=data.chat_id, user_id=user_id, session=session)
            if not db_response.get('success', False):
                logger.error(f"Failed to mark chat as deleted: {db_response.get('error')}")
                return JSONResponse(
                    status_code=500,
                    content={"success": False, "error": "I've run into a technical problem on my end. Please try that again in a moment."}
                )
            
            return DeleteChatResponse(
                success=True,
                message=db_response.get('message', "Chat deleted successfully")
            )
            
    except RequestValidationError as e:
        # Validation errors (like missing fields) return 422
        logger.error(f"Validation error in delete chat endpoint: {e}")
        return JSONResponse(
            status_code=422,
            content={"success": False, "error": "Your request couldn't be understood. Please check your input and try again."}
        )
    except HTTPException as e:
        # Re-raise HTTP exceptions to let the global handler handle them
        raise
    except Exception as e:
        # All other errors return 500
        logger.error(f"Error in delete chat endpoint: {e}")
        return JSONResponse(
            status_code=500,
            content={"success": False, "error": "An unexpected error occurred while deleting the chat. Please try that again."}
        )

@router.post("/rename-chat-title", response_model=RenameChatResponse, status_code=200, responses={404: {"model": ErrorResponse}, 401: {"model": ErrorResponse}, 422: {"model": ErrorResponse}, 500: {"model": ErrorResponse}})
async def rename_chat_endpoint(data: RenameChatRequest, user_id: str = Depends(get_current_active_user)):
    """Rename a chat by updating its title"""
    logger.info(f"Rename chat request received for user_id: {user_id}, chat_id: {data.chat_id}")
    
    try:
        # Validate input data
        if not data.chat_id:
            logger.error("Missing required field: chat_id")
            return JSONResponse(
                status_code=422,
                content={"success": False, "error": "Missing required field: chat_id"}
            )
        
        if not data.new_title:
            logger.error("Missing required field: new_title")
            return JSONResponse(
                status_code=422,
                content={"success": False, "error": "Missing required field: new_title"}
            )
            
        # Trim whitespace from title
        new_title = data.new_title.strip()
        if not new_title:
            logger.error("New title cannot be empty")
            return JSONResponse(
                status_code=422,
                content={"success": False, "error": "New title cannot be empty"}
            )
        
        async with async_session_scope() as session:
            # Check if user exists and credentials are valid
            db_response = await check_user_by_id(user_id=user_id, session=session)
            if not db_response.get('success', False):
                logger.error(f"Failed to check user by id: {db_response.get('error')}")
                return JSONResponse(
                    status_code=500,
                    content={"success": False, "error": "An unexpected error stopped me from renaming that chat. Let's give it another try."}
                )
            if not db_response.get('exists', False):
                logger.error(f"User with ID {user_id} not found or token is invalid")
                return JSONResponse(
                    status_code=401,
                    content={"success": False, "error": "It looks like your session has expired. Please log in again to rename the chat."}
                )
            
            # Rename the chat
            db_response = await rename_chat(chat_id=data.chat_id, user_id=user_id, new_title=new_title, session=session)
            if not db_response.get('success', False):
                logger.error(f"Failed to rename chat: {db_response.get('error')}")
                return JSONResponse(
                    status_code=500,
                    content={"success": False, "error": "An unexpected error stopped me from renaming that chat. Let's give it another try."}
                )
            
            return RenameChatResponse(
                success=True,
                message=db_response.get('message', "Chat renamed successfully")
            )
            
    except RequestValidationError as e:
        # Validation errors (like missing fields) return 422
        logger.error(f"Validation error in rename chat endpoint: {e}")
        return JSONResponse(
            status_code=422,
            content={"success": False, "error": "Your request couldn't be understood. Please check your input and try again."}
        )
    except HTTPException as e:
        # Re-raise HTTP exceptions to let the global handler handle them
        raise
    except Exception as e:
        # All other errors return 500
        logger.error(f"Error in rename chat endpoint: {e}")
        return JSONResponse(
            status_code=500,
            content={"success": False, "error": "An unexpected error stopped me from renaming that chat. Let's give it another try."}
        )


# @router.get("/chats", response_model=UserChatsResponse, status_code=200, responses={404: {"model": ErrorResponse}, 401: {"model": ErrorResponse}, 422: {"model": ErrorResponse}, 500: {"model": ErrorResponse}})
# async def get_chats(user_id: str = Depends(get_current_active_user)):
#     """Get user chats endpoint"""
#     logger.info(f"Get user chats request received for user_id: {user_id}")
#     try:        
#         async with async_session_scope() as session:
#             # Check if user exists and credentials are valid
#             db_response = await check_user_by_id(user_id=user_id, session=session)
#             if not db_response.get('success', False):
#                 logger.error(f"Failed to check user by id: {db_response.get('error')} for user_id: {user_id}")
#                 return JSONResponse(
#                     status_code=500,
#                     content={"success": False, "error": "Internal server error"}
#                 )
#             if not db_response.get('exists', False):
#                 logger.error(f"User with ID {user_id} not found or token is invalid")
#                 return JSONResponse(
#                     status_code=401,
#                     content={"success": False, "error": "Unauthorized request"}
#                 )
            
#             # Get all chats for the user
#             db_response = await get_user_chats(user_id=user_id, session=session)
#             if not db_response.get('success', False):
#                 logger.error(f"Database error: Failed to get user chats: {db_response.get('error')} for user_id: {user_id}")
#                 return JSONResponse(
#                     status_code=500,
#                     content={"success": False, "error": "Internal server error"}
#                 )
            
#             chats = db_response.get('chats', [])
#             if not chats:
#                 logger.info(f"No chats found for user_id: {user_id}")
#                 return UserChatsResponse(
#                     success=True,
#                     chats=None
#                 )

#             chats_with_reports = []
#             # Sort chats by created_at in descending order (newest first)
#             sorted_chats = sorted(chats, key=lambda x: x['created_at'], reverse=True)
#             for chat in sorted_chats:
#                 chats_with_reports.append(
#                     {
#                         'chat_id': chat['id'],
#                         'chat_title': chat['chat_title'],
#                         'updated_at': chat['updated_at'],
#                         'created_at': chat['created_at']
#                     }
#                 )
            

#             return UserChatsResponse(
#                 success=True,
#                 chats=chats_with_reports
#             )
                
#     except RequestValidationError as e:
#         # Validation errors (like missing fields) return 422
#         logger.error(f"Validation error in get user chats endpoint: {e} for user_id: {user_id}")
#         return JSONResponse(
#             status_code=422,
#             content={"success": False, "error": "Invalid request"}
#         )
#     except HTTPException as e:
#         # Re-raise HTTP exceptions to let the global handler handle them
#         raise
#     except Exception as e:
#         # All other errors return 500
#         logger.error(f"Error in get user chats endpoint: {e} for user_id: {user_id}")
#         return JSONResponse(
#             status_code=500,
#             content={"success": False, "error": "Unexpected error"}
#         )
            
@router.get("/report", response_model=ReportPresignedUrlResponse, status_code=200, responses={404: {"model": ErrorResponse}, 401: {"model": ErrorResponse}, 422: {"model": ErrorResponse}, 500: {"model": ErrorResponse}})
async def get_report_presigned_url(id: str, file_type: str, user_id: str = Depends(get_current_active_user)):
    """Get report presigned url endpoint"""
    logger.info(f"Get report presigned url request received for report_id: {id}, file_type: {file_type}, user_id: {user_id}")
    try:
        async with async_session_scope() as session:
            # Check if user exists and credentials are valid
            db_response = await check_user_by_id(user_id=user_id, session=session)
            if not db_response.get('success', False):
                logger.error(f"Failed to check user by id: {db_response.get('error')} for user_id: {user_id}, report_id: {id}")
                return JSONResponse(
                    status_code=500,
                    content={"success": False, "error": "Something went wrong on our end while preparing your download. Please try that again."}
                )
            if not db_response.get('exists', False):
                logger.error(f"User with ID {user_id} not found or token is invalid")
                return JSONResponse(
                    status_code=401,
                    content={"success": False, "error": "Unauthorized request"}
                )
            
            # Verify report exists and user owns it
            ownership = await verify_report_ownership(report_id=id, user_id=user_id, session=session)
            if not ownership["authorized"]:
                return JSONResponse(
                    status_code=ownership["status_code"],
                    content={"success": False, "error": ownership["error"]}
                )
            
            db_response = await get_file_s3_path(id=id, file_type=file_type, session=session)
            if not db_response.get('success', False):
                logger.error(f"Database error: Failed to get file s3 path: {db_response.get('error')} for user_id: {user_id}, report_id: {id}")
                return JSONResponse(
                    status_code=500,
                    content={"success": False, "error": "Something went wrong on our end while preparing your download. Please try that again."}
                )
            
            s3_uri = db_response.get('s3_uri', None)
            if not s3_uri:
                logger.error(f"No s3 uri found for report_id: {id} with file_type: {file_type}, user_id: {user_id}")
                return JSONResponse(
                    status_code=404,
                    content={"success": False, "error": "I'm having trouble locating the report file. We're looking into it. Please try again shortly."}
                )
            
            if s3_uri and S3_REPORTS_BASE_PATH in s3_uri:
                s3_key = s3_uri[s3_uri.find(S3_REPORTS_BASE_PATH):]
            elif s3_uri and "casper_reports" in s3_uri: #This is been done for testing since sometime we do testing after putting prod records in the dev DB.
                s3_key = s3_uri[s3_uri.find("casper_reports"):]
            else:
                logger.error(f"Invalid s3 path: {s3_uri} of report_id: {id} with file_type: {file_type}, user_id: {user_id}")
                return JSONResponse(
                    status_code=404,
                    content={"success": False, "error": "I'm having trouble locating the report file. We're looking into it. Please try again shortly."}
                )
            s3_instance = get_s3_instance()
            presigned_url = await run_in_threadpool(s3_instance.get_download_presigned_url,
                s3_key=s3_key,
                timeout=3600  # 1 hour — reduced from 7 days for security
            )

            logger.info(f'Successfully generated presigned url of report_id: {id} with file_type: {file_type} for user_id: {user_id}')

            return JSONResponse(
                status_code=200,
                content={"success": True, "presigned_url": presigned_url}
            )

    except RequestValidationError as e:
        # Validation errors (like missing fields) return 422
        logger.error(f"Validation error in report presigned url endpoint: {e} for user_id: {user_id}, report_id: {id}")
        return JSONResponse(
            status_code=422,
            content={"success": False, "error": "Your request couldn't be understood. Please check your input and try again."}
        )
    except HTTPException as e:
        # Re-raise HTTP exceptions to let the global handler handle them
        raise
    except Exception as e:
        # All other errors return 500
        logger.error(f"Error in report presigned url endpoint: {e} for user_id: {user_id}, report_id: {id}")
        return JSONResponse(
            status_code=500,
            content={"success": False, "error": "Something went wrong on our end while preparing your download. Please try that again."}
        )


@router.post("/presigned-urls", response_model=BatchPresignedUrlResponse, status_code=200, responses={401: {"model": ErrorResponse}, 422: {"model": ErrorResponse}, 429: {"model": ErrorResponse}, 500: {"model": ErrorResponse}})
async def batch_presigned_urls(data: BatchPresignedUrlRequest, user_id: str = Depends(get_current_active_user)):
    """Generate presigned URLs for a batch of S3 visualization URIs.

    Intended for refreshing expired visualization URLs on long-lived pages.
    Rate-limited to 10 requests per minute per user, max 20 URIs per call.
    """
    logger.info(f"Batch presigned URL request from user_id: {user_id}, count: {len(data.s3_uris)}")
    try:
        if len(data.s3_uris) > 20:
            return JSONResponse(
                status_code=422,
                content={"success": False, "error": "Maximum 20 URIs per request."}
            )

        rate_key = f"rate_limit:presigned_urls:{user_id}"
        count = await redis_instance.redis_client.incr(rate_key)
        if count == 1:
            await redis_instance.redis_client.expire(rate_key, 60)
        if count > 10:
            logger.warning(f"Rate limit exceeded for presigned URLs by user_id: {user_id}")
            return JSONResponse(
                status_code=429,
                content={"success": False, "error": "Too many requests. Please try again shortly."}
            )

        async with async_session_scope() as session:
            db_response = await check_user_by_id(user_id=user_id, session=session)
            if not db_response.get('success', False) or not db_response.get('exists', False):
                return JSONResponse(
                    status_code=401,
                    content={"success": False, "error": "Unauthorized request"}
                )

        s3_instance = get_s3_instance()
        results: dict[str, str | None] = {}
        for s3_uri in data.s3_uris:
            if not s3_uri or not s3_uri.startswith("s3://"):
                results[s3_uri] = None
                continue
            s3_key = extract_s3_key(s3_uri)
            if not s3_key or S3_REPORTS_BASE_PATH not in s3_key:
                results[s3_uri] = None
                continue
            url = await get_or_create_presigned_url(s3_uri, redis_instance.redis_client, s3_instance)
            results[s3_uri] = url

        return BatchPresignedUrlResponse(success=True, urls=results)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in batch presigned URLs endpoint: {e}", exc_info=True)
        return JSONResponse(
            status_code=500,
            content={"success": False, "error": "Something went wrong. Please try again."}
        )


@router.get("/list-version-outputs", response_model=ListVersionOutputsResponse, status_code=200, responses={404: {"model": ErrorResponse}, 401: {"model": ErrorResponse}, 422: {"model": ErrorResponse}, 500: {"model": ErrorResponse}})
async def list_version_outputs(report_id: str, user_id: str = Depends(get_current_active_user)):
    """
    List all available output files (non-null) across all versions of a report.
    Returns a simple list for frontend to display all available downloads.
    
    Args:
        report_id: The report ID
        user_id: Authenticated user ID (from token)
    
    Returns:
        List of all available outputs with version, file_type, and generated_at
    """
    logger.info(f"List outputs request for report_id: {report_id}, user_id: {user_id}")
    
    try:
        async with async_session_scope() as session:
            # Verify the report exists and belongs to the user
            ownership = await verify_report_ownership(report_id=report_id, user_id=user_id, session=session)
            if not ownership["authorized"]:
                return JSONResponse(
                    status_code=ownership["status_code"],
                    content={"success": False, "error": ownership["error"]}
                )
            
            # Fetch all version outputs
            result = await get_all_version_outputs_list(report_id=report_id, session=session)
            
            if not result.get('success'):
                logger.error(f"Error fetching version outputs: {result.get('error')}")
                return JSONResponse(
                    status_code=500,
                    content={
                        "success": False,
                        "error": "Unable to fetch report outputs. Please try again later."
                    }
                )
            
            outputs = result.get('outputs', [])
            logger.info(f"Successfully fetched {len(outputs)} outputs for report {report_id}")
            
            return ListVersionOutputsResponse(
                success=True,
                report_id=report_id,
                outputs=outputs
            )
    
    except RequestValidationError as e:
        logger.error(f"Validation error in list-version-outputs endpoint: {e}")
        return JSONResponse(
            status_code=422,
            content={
                "success": False,
                "error": "Invalid request format. Please check your input and try again."
            }
        )
    except HTTPException as e:
        raise
    except Exception as e:
        logger.error(f"Unexpected error in list-version-outputs endpoint: {str(e)}", exc_info=True)
        return JSONResponse(
            status_code=500,
            content={
                "success": False,
                "error": "Something went wrong while fetching outputs. Please try again."
            }
        )


@router.get(
    "/report-version-history/{report_id}",
    response_model=ReportVersionHistoryResponse,
    status_code=200,
    responses={
        404: {"model": ErrorResponse},
        401: {"model": ErrorResponse},
        500: {"model": ErrorResponse}
    }
)
async def get_report_version_history(
    report_id: str,
    user_id: str = Depends(get_current_active_user)
):
    """
    Get version history for a report.
    Returns all versions with their generated outputs status.
    Used by frontend to populate the version history dropdown.
    """
    logger.info(f"Get version history request for report_id: {report_id}, user_id: {user_id}")
    
    try:
        # Verify user has access to this report
        async with async_session_scope() as session:
            db_response = await check_user_by_id(user_id=user_id, session=session)
            if not db_response.get('success', False):
                return JSONResponse(
                    status_code=500,
                    content={"success": False, "error": "Something unexpected happened. Please try again later."}
                )
            if not db_response.get('exists', False):
                return JSONResponse(
                    status_code=401,
                    content={"success": False, "error": "Unauthorized request"}
                )
        
        # Get version history (with ownership check)
        async with async_session_scope() as session:
            ownership = await verify_report_ownership(report_id=report_id, user_id=user_id, session=session)
            if not ownership["authorized"]:
                return JSONResponse(
                    status_code=ownership["status_code"],
                    content={"success": False, "error": ownership["error"]}
                )
            
            result = await get_report_version_history_data(
                report_id=report_id,
                session=session
            )
            
            if not result.get('success'):
                error_msg = result.get('error', 'Failed to get version history')
                return JSONResponse(
                    status_code=404 if "not found" in error_msg.lower() else 500,
                    content={"success": False, "error": error_msg}
                )
            
            return ReportVersionHistoryResponse(
                success=True,
                report_id=report_id,
                s3_base_path=result.get('s3_base_path', ''),
                versions=result.get('versions', [])
            )
    
    except Exception as e:
        logger.error(f"Error getting version history: {str(e)}", exc_info=True)
        return JSONResponse(
            status_code=500,
            content={"success": False, "error": "Something unexpected happened. Please try again later."}
        )


@router.get(
    "/report-info",
    response_model=ReportInfoResponse,
    status_code=200,
    responses={
        404: {"model": ErrorResponse},
        401: {"model": ErrorResponse},
        500: {"model": ErrorResponse}
    }
)
async def get_report_info(
    report_id: str,
    report_version_id: str,
    user_id: str = Depends(get_current_active_user)
):
    """
    Get report info for a specific version, including cards.
    Returns data in the same format as reports in previous chat messages endpoint.
    
    Args:
        report_id: The ID of the report
        report_version_id: The ID of the specific report version
        user_id: Authenticated user ID (from token)
    
    Returns:
        Report data with cards for the specified version
    """
    logger.info(f"Get report info request for report_id: {report_id}, version_id: {report_version_id}, user_id: {user_id}")
    
    try:
        async with async_session_scope() as session:
            db_response = await check_user_by_id(user_id=user_id, session=session)
            if not db_response.get('success', False):
                return JSONResponse(
                    status_code=500,
                    content={"success": False, "error": "Something unexpected happened. Please try again later."}
                )
            if not db_response.get('exists', False):
                return JSONResponse(
                    status_code=401,
                    content={"success": False, "error": "Unauthorized request"}
                )
            
            result = await get_report_info_by_version(
                report_id=report_id,
                report_version_id=report_version_id,
                session=session
            )
            
            if not result.get('success'):
                error_msg = result.get('error', 'Failed to get report info')
                return JSONResponse(
                    status_code=404 if "not found" in error_msg.lower() else 500,
                    content={"success": False, "error": error_msg}
                )

            version_reports = result.get('reports', [])
            await replace_visualization_uris_in_reports(version_reports, redis_instance.redis_client)

            return ReportInfoResponse(
                success=True,
                reports=version_reports
            )
    
    except Exception as e:
        logger.error(f"Error getting report info: {str(e)}", exc_info=True)
        return JSONResponse(
            status_code=500,
            content={"success": False, "error": "Something unexpected happened. Please try again later."}
        )


@router.get("/download-version", response_model=VersionDownloadResponse, status_code=200, responses={404: {"model": ErrorResponse}, 401: {"model": ErrorResponse}, 422: {"model": ErrorResponse}, 500: {"model": ErrorResponse}})
async def download_version_file(
    report_id: str,
    version: int,
    file_type: str,
    user_id: str = Depends(get_current_active_user)
):
    """
    Download a specific file type from a specific version of a report.
    Returns a presigned URL for direct download.
    
    Args:
        report_id: The ID of the report
        version: The version number to download
        file_type: The type of file to download (pdf, html, md, pptx, info_pdf)
        user_id: Authenticated user ID (from token)
    
    Returns:
        Presigned URL for downloading the file (valid for 1 hour)
    """
    logger.info(f"Download request for report_id: {report_id}, version: {version}, file_type: {file_type}, user_id: {user_id}")
    
    try:
        async with async_session_scope() as session:
            # Verify the report exists and belongs to the user
            ownership = await verify_report_ownership(report_id=report_id, user_id=user_id, session=session)
            if not ownership["authorized"]:
                return JSONResponse(
                    status_code=ownership["status_code"],
                    content={"success": False, "error": ownership["error"]}
                )
            report_data = ownership["report"]
            
            # Get the file path for the specified version and file type
            file_result = await get_version_file_for_download(
                report_id=report_id,
                version=version,
                file_type=file_type,
                session=session
            )
            
            if not file_result.get('success'):
                error_msg = file_result.get('error', 'File not found')
                logger.warning(f"File not found: {error_msg}")
                
                # Determine appropriate status code based on error
                if 'not found' in error_msg.lower():
                    status_code = 404
                elif 'Invalid file type' in error_msg:
                    status_code = 422
                else:
                    status_code = 500
                
                return JSONResponse(
                    status_code=status_code,
                    content={
                        "success": False,
                        "error": error_msg
                    }
                )
            
            # Generate presigned URL asynchronously
            s3_path = file_result.get('s3_path')
            
            # Extract S3 key from full S3 path (same logic as get_report_presigned_url)
            if s3_path and S3_REPORTS_BASE_PATH in s3_path:
                s3_key = s3_path[s3_path.find(S3_REPORTS_BASE_PATH):]
            elif s3_path and "casper_reports" in s3_path:  # For testing with prod records in dev DB
                s3_key = s3_path[s3_path.find("casper_reports"):]
            else:
                logger.error(f"Invalid s3 path: {s3_path} for report_id: {report_id}, version: {version}, file_type: {file_type}")
                return JSONResponse(
                    status_code=404,
                    content={
                        "success": False,
                        "error": "I'm having trouble locating the file. We're looking into it. Please try again shortly."
                    }
                )
            
            # Use global S3 instance and run in threadpool for async operation
            s3_instance = get_s3_instance()
            # presigned_url = await run_in_threadpool(
            #     s3_instance.get_download_presigned_url,
            #     s3_key=s3_key,
            #     timeout=604800
            # )
            # Generate custom filename with version number
            report_title = report_data.get('title', 'report')
            # Clean up the title for filename (keep spaces, remove problematic characters)
            safe_title = report_title.strip(".").strip().replace(":", " ").replace("_", " ")
            # Collapse multiple spaces into one
            safe_title = re.sub(r"\s+", " ", safe_title)
            
            # Construct filename based on file type
            if file_type == 'info_pdf':
                download_filename = f"{safe_title} v{version} visual brief.pdf"
            elif file_type == 'pdf':
                download_filename = f"{safe_title} v{version}.pdf"
            elif file_type == 'html':
                download_filename = f"{safe_title} v{version}.html"
            elif file_type == 'md':
                download_filename = f"{safe_title} v{version}.md"
            elif file_type == 'pptx':
                download_filename = f"{safe_title} v{version}.pptx"
            else:
                download_filename = f"{safe_title} v{version}.{file_type}"
            
            # Generate presigned URL with custom filename
            presigned_url = await run_in_threadpool(
                s3_instance.get_download_presigned_url_with_filename,
                s3_key=s3_key,
                filename=download_filename,
                timeout=3600  # 1 hour — reduced from 7 days for security
            )

            if not presigned_url:
                logger.error(f"Failed to generate presigned URL for s3_key: {s3_key}")
                return JSONResponse(
                    status_code=500,
                    content={
                        "success": False,
                        "error": "Failed to generate download link. Please try again."
                    }
                )
            
            logger.info(f"Successfully generated presigned URL for {file_type} version {version}")
            return VersionDownloadResponse(
                success=True,
                presigned_url=presigned_url,
                report_id=report_id,
                version=version,
                file_type=file_type,
                expires_in=3600
            )
    
    except RequestValidationError as e:
        logger.error(f"Validation error in download-version endpoint: {e}")
        return JSONResponse(
            status_code=422,
            content={
                "success": False,
                "error": "Invalid request format. Please check your input and try again."
            }
        )
    except HTTPException as e:
        raise
    except Exception as e:
        logger.error(f"Unexpected error in download-version endpoint: {str(e)}", exc_info=True)
        return JSONResponse(
            status_code=500,
            content={
                "success": False,
                "error": "Something went wrong while preparing your download. Please try again."
            }
        )

@router.post("/user-logs", status_code=200, responses={401: {"model": ErrorResponse}, 422: {"model": ErrorResponse}, 500: {"model": ErrorResponse}})
async def user_logs(data: UserLogsRequest, user_id: str = Depends(get_current_active_user)):
    """User logs endpoint"""
    logger.info(f"User logs request received for user_id: {user_id}")

    if data.type not in ['download-report']:
        return JSONResponse(
            status_code=400,
            content={"success": False, "error": "Invalid request"}
        )
    
    cloudwatch_data = {}


    if data.type == 'download-report':
        if not data.logs_data.get('chat_id') or not data.logs_data.get('chat_title'):
            return JSONResponse(
                status_code=400,
                content={"success": False, "error": "Invalid request"}
            )

        chat_id = data.logs_data.get('chat_id')
        chat_title = data.logs_data.get('chat_title')
        
        cloudwatch_data = {
            "event": "Report downloaded",
            "event_success": data.status,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "chat_id": chat_id,
            "chat_title": chat_title
        }


    if not cloudwatch_data:
        return JSONResponse(
            status_code=400,
            content={"success": False, "error": "Invalid request"}
        )

    try:
        response = await insert_cloudwatch_logs(data=cloudwatch_data, user_id=user_id)
    except Exception as e:
        logger.error(f"Error in inserting cloudwatch logs for user_id: {user_id}: {e}")
        return JSONResponse(
            status_code=500,
            content={"success": False, "error": "Internal server error"}
        )

    if not response.get('success', False):
        return JSONResponse(
            status_code=500,
            content={"success": False, "error": "Internal server error"}
        )
    
    return JSONResponse(
        status_code=200,
        content={"success": True}
    )

@router.post("/google-login", response_model=GoogleLoginResponse, responses={422: {"model": ErrorResponse}, 500: {"model": ErrorResponse}})
async def google_login(data: GoogleLoginRequest, chat_id: Optional[str] = None):
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
                content={"success": False, "error": f"Too many login attempts. Please try again in {ttl} seconds."}
            )
    except Exception as e:
        logger.error(f"Rate limit check failed for google-login: {e}")

    google_login_time = datetime.now(timezone.utc)
    login_or_signup = None
    # user_data_for_redis = {}
    async with async_session_scope() as session:
        try:

            # Generate random special character string for phone and phone_country_code
            special_chars = '!@#$%^&*()_+-=[]{}|;:,.<>?'
            random_phone = ''.join(random.choice(special_chars) for _ in range(10))
            random_country_code = ''.join(random.choice(special_chars) for _ in range(5))
            
            # Generate random password with special characters
            random_password = ''.join(random.choice(special_chars) for _ in range(16))
            password_hash = generate_password_hash(random_password)


            id_token_str = str(data.id_token)
            logger.info(f"id_token_str: {id_token_str[:10]}...")
            # Validate input data
            if not id_token_str:
                logger.error("Empty id_token received")
                return JSONResponse(
                    status_code=422,
                    content={"success": False, "error": "Your request couldn't be understood. Please check your input and try again."}
                )
            
            # Validate that GOOGLE_CLIENT_ID is configured (critical security check)
            if not GOOGLE_CLIENT_ID:
                logger.error("GOOGLE_CLIENT_ID is not configured - Google login is disabled for security reasons")
                return JSONResponse(
                    status_code=500,
                    content={"success": False, "error": "Google login is temporarily unavailable. We're on it. Please try again later."}
                )
            
            try:
                # Verify the ID token
                id_info = id_token.verify_oauth2_token(
                    id_token_str, 
                    google_requests.Request(), 
                    GOOGLE_CLIENT_ID
                )
                
                email = id_info.get('email')
                name = id_info.get('name')
                email_verified = id_info.get('email_verified', False)
                
                if not email or not name or not email_verified:
                    logger.error("Invalid Google ID token or email not verified")
                    return JSONResponse(
                        status_code=401,
                        content={"success": False, "error": "Invalid Google ID token or email not verified"}
                    )
            except ValueError as e:
                if "Token expired" in str(e):
                    logger.error(f"Error verifying Google ID token: {e}")
                    return JSONResponse(
                        status_code=401,
                        content={"success": False, "error": "Your Google session has expired. Please sign back in to Google, then connect here."}
                    )
                logger.error(f"Error verifying Google ID token: {e}")
                return JSONResponse(
                    status_code=401,
                    content={"success": False, "error": "Invalid Google ID token"}
                )
            except Exception as e:
                logger.error(f"Error verifying Google ID token: {e}")
                return JSONResponse(
                    status_code=401,
                    content={"success": False, "error": "Invalid Google ID token"}
                )
            
            # Check if user already exists
            db_response = await check_user_exists_by_email(email=email, session=session)
            if not db_response.get('success', False):
                logger.error(f"Database error: Failed to check if user exists: {db_response.get('error')}")
                return JSONResponse(
                    status_code=500,
                    content={"success": False, "error": "We've hit a system error during the Google sign-in. Our team is aware. Please try again."}
                )
            
            user_id = db_response.get('user_id')
            user_name = db_response.get('user_name')
            t_c_verified = False  # Default value for new users
            onboarding_completed = False  # Default for new users
            
            if db_response.get('exists', False):
                login_or_signup = 'login'
                t_c_verified = db_response.get('t_c_verified', False)
                onboarding_completed = db_response.get('onboarding_completed', False)
                if db_response.get('is_verified', False):
                    logger.info(f"Verified user with email {email} logged in with user_id: {user_id}")
                else:
                    db_response = await update_user_verification_status(email=email, verified_at=google_login_time, is_verified=True, is_google_verified=True, session=session)
                    if not db_response.get('success', False):
                        logger.error(f"Database error: Failed to update user verification status: {db_response.get('error')} for user_id: {user_id}")
                        return JSONResponse(
                            status_code=500,
                            content={"success": False, "error": "I couldn't update your account's verification status. Please try that again."}
                        )
                    logger.info(f"Unverified user with email {email} logged in with user_id: {user_id}")
                    
            else:
                # Create new user with random phone and phone_country_code
                # user_id = str(uuid7())
                login_or_signup = 'signup'
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
                    "verified_at": google_login_time
                }
                
                db_response = await create_user(user_data=new_user, session=session)
                if not db_response.get('success', False):
                    logger.error(f"Database error: Failed to create user: {db_response.get('error')}")
                    return JSONResponse(
                        status_code=500,
                        content={"success": False, "error": "I ran into a system error while creating your account. We're on it. Please try again."}
                    )
                
                user_id = db_response.get('id')
                t_c_verified = False  # New users have t_c_verified as False by default
                logger.info(f"Google user created successfully with ID: {user_id}")
                
                # Initialize wallet with signup bonus tokens
                wallet_response = await WalletService.initialize_wallet(user_id=user_id, session=session)
                if not wallet_response.get('success', False):
                    logger.error(f"Failed to initialize wallet for Google user_id: {user_id}: {wallet_response.get('error')}")
                    # Rollback user creation if wallet creation fails
                    await session.rollback()
                    return JSONResponse(
                        status_code=500,
                        content={"success": False, "error": "I ran into a system error while creating your account. We're on it. Please try again."}
                    )
                
                # Commit user and wallet creation
                await session.commit()
                logger.info(f"Google user and wallet created successfully for user_id: {user_id}")
                # TODO: mail send here
                logger.info(f"Wallet initialized for Google user_id: {user_id} with {wallet_response.get('tokens_credited', 0)} tokens")
            
            # Temp user login
            if chat_id:
                logger.info(f"Temporary user login with chat_id: {chat_id} and user_id: {user_id} and user_name: {user_name} and email: {email}")

                redis_response = await redis_instance.check_chat_exists(chat_id=chat_id)
                if not redis_response.get('success'):
                    logger.error(f"Failed to check if chat exists in redis with ID: {chat_id} for user_id: {user_id}: {redis_response.get('error')}")
                    return JSONResponse(
                        status_code=500,
                        content={"success": False, "error": "There was an issue accessing temporary chat data. Please try signing in again."}
                    )
                
                temp_chat_exists_in_redis = redis_response.get('exists', False)
                
                # when existing user logs in from temp chat without signing up first from temp chat
                if temp_chat_exists_in_redis:

                    redis_response = await redis_instance.get_chat(chat_id=chat_id)
                    if not redis_response.get('success'):
                        logger.error(f"Failed to get chat from Redis for chat_id: {chat_id}, user_id: {user_id}: {redis_response.get('error')}")
                        return JSONResponse(
                            status_code=500,
                            content={"success": False, "error": "I couldn't retrieve the temporary chat data. Please try the Google sign-in again."}
                        )
                
                    message_data = {
                        'user_id': user_id,
                        'chat_id': chat_id,
                        'chat_messages': redis_response.get('chat_data', {}).get('chat_messages'),
                        'updated_at': redis_response.get('chat_data', {}).get('updated_at'),
                        'created_at': redis_response.get('chat_data', {}).get('created_at'),
                        'chat_title': redis_response.get('chat_data', {}).get('chat_title')
                    }
                
                    db_response = await insert_chats(message_data=message_data, session=session)
                    if not db_response.get('success', False):
                        logger.error(f"Database error: Failed to insert chat for chat_id: {chat_id}, user_id: {user_id}: {db_response.get('error')}")
                        return JSONResponse(
                            status_code=500,
                            content={"success": False, "error": "There was a problem saving chat messages. Please try signing in again."}
                        )
                
                    logger.info(f"Temporary chat inserted successfully with ID: {chat_id} for user_id: {user_id} and user_name: {user_name} and email: {email}")
                
                    # update user_id in chat in redis
                    redis_response = await redis_instance.delete_chat(chat_id=chat_id)
                    if not redis_response.get('success'):
                        logger.error(f"Failed to delete chat from redis with ID: {chat_id} for user_id: {user_id}: {redis_response.get('error')}")
                        return JSONResponse(
                            status_code=500,
                            content={"success": False, "error": "We've hit a system error during the Google sign-in. Our team is aware. Please try again."}
                        )
                    logger.info(f"Temporary chat deleted successfully with ID: {chat_id} for user_id: {user_id} and user_name: {user_name} and email: {email}")
                
                    temp_chat_session = await redis_instance.redis_client.get(f"temp_chat_session:{chat_id}")
                    if temp_chat_session:
                        await redis_instance.redis_client.delete(temp_chat_session)
                    await redis_instance.redis_client.delete(f"temp_chat_session:{chat_id}")
                    logger.info(f"Temporary chat session deleted successfully with ID: {chat_id}")

                else:
                    logger.warning(f"Temporary chat with ID: {chat_id} not found in redis, temporary chat session expired for user_id: {user_id}")
            
            # Generate JWT tokens
            access_token = create_access_token(user_id)
            refresh_token = create_refresh_token(user_id)

            cloudwatch_data = {
                "event": f"Google {login_or_signup}",
                "event_success": True,
                "timestamp": datetime.now(timezone.utc).isoformat()
            }
            try:
                asyncio.create_task(insert_cloudwatch_logs(data=cloudwatch_data, user_id=user_id))
            except Exception as e:
                logger.error(f"Error in inserting cloudwatch logs for user_id: {user_id}: {e}")
            
            logger.info(f"User logged in successfully with ID: {user_id}")
            return GoogleLoginResponse(success=True, token=access_token, refresh_token=refresh_token, user_id=user_id, user_name=user_name, t_c_verified=t_c_verified, onboarding_completed=onboarding_completed)
            
        except Exception as e:
            logger.error(f"Error in google login: {e}")
            await session.rollback()
            return JSONResponse(
                status_code=500,
                content={"success": False, "error": "We've hit a system error during the Google sign-in. Our team is aware. Please try again."}
            )

@router.post("/forgot-password", response_model=ForgotPasswordResponse, responses={422: {"model": ErrorResponse}, 500: {"model": ErrorResponse}})
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
            logger.warning(f"Forgot-password rate limit exceeded for email: {data.email.strip().lower()}, attempts: {attempts}")
            return JSONResponse(
                status_code=429,
                content={"success": False, "message": f"Too many password reset requests. Please try again in {ttl} seconds."}
            )
    except Exception as e:
        logger.error(f"Rate limit check failed for forgot-password: {e}")

    try:
        forgot_password_time = datetime.now(timezone.utc)
        email = data.email.strip().lower()
        
        # Validate input data
        if len(email) == 0:
            logger.error("Empty email received after trimming whitespace")
            return JSONResponse(
                status_code=422,
                content={"success": False, "message": "Email cannot be empty"}
            )
        
        if len(email) > 50:
            logger.error(f"Email received is too long: {email}")
            return JSONResponse(
                status_code=422,
                content={"success": False, "message": "Email cannot be longer than 50 characters"}
            )
        
        # Check if password reset email has been sent recently
        forgot_password_email_key = f"forgot_password_email_sent:{email}"
        is_forgot_password_email_sent = await redis_instance.redis_client.exists(forgot_password_email_key)
        
        if is_forgot_password_email_sent:
            logger.info(f"Password reset email has already been sent recently to {email}")
            return ForgotPasswordResponse(
                success=False,
                message="A password reset link was recently sent. Please check your inbox or try again later."
            )
        
        async with async_session_scope() as session:
            # Check if user exists
            db_response = await check_user_exists_by_email(email=email, session=session)
            
            if not db_response.get('success', False):
                logger.error(f"Database error: {db_response.get('error')}")
                return JSONResponse(
                    status_code=500,
                    content={"success": False, "message": "A system error occurred while processing your request. Please try again."}
                )
            
            if not db_response.get('exists', False):
                logger.warning(f"User with email {email} not found")
                # For security reasons, don't reveal that the email doesn't exist
                return JSONResponse(
                    status_code=422,
                    content={"success": False, "message": "I don't recognize that email address. Perhaps you signed up with a different one?"}
                )
            if db_response.get('is_google_verified', False):
                logger.warning(f"User with email {email} is Google verified")
                return JSONResponse(
                    status_code=422,
                    content={"success": False, "message": "This account uses Google Sign-In. Please use the 'Sign in with Google' option to access your account."}
                )
            
            if not db_response.get('is_verified', False):
                logger.warning(f"User with email {email} is not verified")
                return JSONResponse(
                    status_code=422,
                    content={"success": False, "message": "This account is not verified. Please verify your account to reset your password."}
                )
            
            # User exists, generate reset token
            user_id = db_response.get('user_id')
            user_name = db_response.get('user_name')
            
            # Create reset token
            reset_token = create_reset_password_token(user_id=user_id, password_hash=db_response.get('password_hash'))
            
            # Generate reset link with token
            reset_link = f"{FRONTEND_RESET_PASSWORD_URL}?token={reset_token}"
            
            # Send reset email
            is_email_sent = await run_in_threadpool(lambda: send_password_reset_email(
                recipient_email=email,
                user_name=user_name,
                reset_link=reset_link
            ))
            
            if not is_email_sent:
                logger.error(f"Failed to send password reset email to {email} for user_id: {user_id}")
                return JSONResponse(
                    status_code=500,
                    content={"success": False, "message": "Failed to send password reset email. Please try again later."}
                )
            
            # Set reset email sent key in redis with cooldown period
            await redis_instance.redis_client.setex(
                forgot_password_email_key, 
                FORGOT_PASSWORD_EMAIL_COOLDOWN_SECONDS,  
                1
            )

            # Set token in redis with expiry time
            await redis_instance.redis_client.setex(
                f"reset_password_token_for_user:{user_id}",
                FORGOT_PASSWORD_EXPIRE_MINUTES * 60,
                reset_token
            )
            
            logger.info(f"Password reset email sent to {email} for user_id: {user_id}")
            
            # Log the event
            cloudwatch_data = {
                "event": "Forgot password",
                "event_success": True,
                "timestamp": forgot_password_time.isoformat()
            }
            try:
                asyncio.create_task(insert_cloudwatch_logs(data=cloudwatch_data, user_id=user_id))
            except Exception as e:
                logger.error(f"Error in inserting cloudwatch logs for user_id: {user_id}: {e}")
            
            return ForgotPasswordResponse(
                success=True,
                message="If your email is registered, you will receive a password reset link shortly."
            )
            
    except ValueError as e:
        # This catches email validation errors from Pydantic
        logger.error(f"Validation error in forgot password: {e}")
        return JSONResponse(
            status_code=422,
            content={"success": False, "message": "Invalid email format"}
        )
    except Exception as e:
        logger.error(f"Error in forgot password: {e}")
        return JSONResponse(
            status_code=500,
            content={"success": False, "message": "A system error occurred while processing your request. Please try again."}
        )

@router.get("/verify-reset-token", response_model=VerifyResetTokenResponse, responses={422: {"model": ErrorResponse}, 500: {"model": ErrorResponse}})
async def verify_reset_token(token: str):
    """Verify reset token endpoint"""
    logger.info("Verify reset token request received")
    
    try:
        token = token.strip()
        verify_reset_token_time = datetime.now(timezone.utc)
        
        # Validate input data
        if len(token) == 0:
            logger.error("Empty token received after trimming whitespace")
            return JSONResponse(
                status_code=422,
                content={
                    "success": False, 
                    "valid": False,
                    "message": "Token cannot be empty"
                }
            )
        
        # Verify token
        try:
            # Decode token
            payload = jwt.decode(token, JWT_SECRET_KEY, algorithms=[JWT_ALGORITHM])
            
            # Check token type
            if payload.get("type") != "reset_password":
                logger.error(f"Invalid token type for reset password for user_id: {payload.get('user_id')}")
                return JSONResponse(
                    status_code=401,
                    content={"success": False, "message": "That password reset link isn't valid. It might be incorrect or has been used before."}
                )
            
            # Get user_id from token
            user_id = payload.get("user_id")
            if not user_id:
                logger.error(f"User ID not found in token for reset password")
                return JSONResponse(
                    status_code=401,
                    content={"success": False, "message": "That password reset link isn't valid. It might be incorrect or has been used before."}
                )
            
            # Check if reset token of user is valid or not
            reset_token = await redis_instance.redis_client.get(f"reset_password_token_for_user:{user_id}")
            if not reset_token:
                logger.error(f"Reset token not found for user_id: {user_id}")
                return JSONResponse(
                    status_code=401,
                    content={"success": False, "message": "The link has expired. Please request a new one."}
                )
            if reset_token != token:
                logger.error(f"Reset token mismatch for user_id: {user_id}")
                return JSONResponse(
                    status_code=401,
                    content={"success": False, "message": "The link has expired. Please request a new one."}
                )
            
            # Add cloudwatch logs
            cloudwatch_data = {
                "event": "Reset password link clicked",
                "event_success": True,
                "timestamp": verify_reset_token_time.isoformat()
            }
            try:
                asyncio.create_task(insert_cloudwatch_logs(data=cloudwatch_data, user_id=user_id))
            except Exception as e:
                logger.error(f"Error in inserting cloudwatch logs for user_id: {user_id}: {e}")

            logger.info(f"Reset token verified successfully for user_id: {user_id}")
            # Token is valid
            return VerifyResetTokenResponse(
                success=True,
                valid=True,
                message="Token is valid"
            )
            
        except ExpiredSignatureError:
            logger.error("Token has expired for reset password")
            return VerifyResetTokenResponse(
                success=True,
                valid=False,
                message="For security, that password reset link has expired. You can request a new one."
            )
        except JWTError:
            logger.error("Invalid token for reset password")
            return VerifyResetTokenResponse(
                success=True,
                valid=False,
                message="That password reset link isn't valid. It might be incorrect or has been used before."
            )
            
    except Exception as e:
        logger.error(f"Error in verify reset token: {e}")
        return JSONResponse(
            status_code=500,
            content={
                "success": False,
                "valid": False,
                "message": "I couldn't verify that link due to a system error. Please try it again."
            }
        )

@router.post("/reset-password", response_model=ResetPasswordResponse, responses={422: {"model": ErrorResponse}, 500: {"model": ErrorResponse}})
async def reset_password(data: ResetPasswordRequest):
    """Reset password endpoint"""
    logger.info("Reset password request received")
    
    # Rate limit: max 5 reset-password attempts per token per 15 minutes
    token_hash = data.token.strip()[:32]  # Use first 32 chars as key prefix (avoid full token in Redis key)
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
                content={"success": False, "message": f"Too many password reset attempts. Please try again in {ttl} seconds."}
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
                status_code=422,
                content={"success": False, "message": "Token cannot be empty"}
            )
        
        if len(password) == 0:
            logger.error("Empty password received")
            return JSONResponse(
                status_code=422,
                content={"success": False, "message": "Password cannot be empty"}
            )
            
        if len(password) > 50:
            logger.error("Password is too long")
            return JSONResponse(
                status_code=422,
                content={"success": False, "message": "Password cannot be longer than 50 characters"}
            )
        
        # Verify token
        try:
            # Decode token
            payload = jwt.decode(token, JWT_SECRET_KEY, algorithms=[JWT_ALGORITHM])
            
            # Check token type
            if payload.get("type") != "reset_password":
                logger.error(f"Invalid token type for reset password for user_id: {payload.get('user_id')}")
                return JSONResponse(
                    status_code=401,
                    content={"success": False, "message": "That reset link is invalid. Please start the password reset process over."}
                )
            
            # Get user_id from token
            user_id = payload.get("user_id")
            if not user_id:
                logger.error(f"User ID not found in token for reset password")
                return JSONResponse(
                    status_code=401,
                    content={"success": False, "message": "That reset link is invalid. Please start the password reset process over."}
                )
            
            # Check if reset token of user is valid or not
            reset_token = await redis_instance.redis_client.get(f"reset_password_token_for_user:{user_id}")
            if not reset_token:
                logger.error(f"Reset token not found for user_id: {user_id}")
                return JSONResponse(
                    status_code=401,
                    content={"success": False, "message": "The link has expired. Please request a new one."}
                )
            if reset_token != token:
                logger.error(f"Reset token mismatch for user_id: {user_id}")
                return JSONResponse(
                    status_code=401,
                    content={"success": False, "message": "The link has expired. Please request a new one."}
                )
            
            old_password_hash = payload.get("password_hash")
            if not old_password_hash:
                logger.error(f"Old password hash not found in token for reset password for user_id: {user_id}")
                return JSONResponse(
                    status_code=401,
                    content={"success": False, "message": "That reset link is invalid. Please start the password reset process over."}
                )
            
            if verify_password(plain_password=password, hashed_password=old_password_hash):
                logger.error(f"New password cannot be the same as the old password for user_id: {user_id}")
                return JSONResponse(
                    status_code=401,
                    content={"success": False, "message": "Your new password needs to be different from your old one. Please choose a new one."}
                )
            
            # Update user password
            async with async_session_scope() as session:
                
                # Generate password hash
                password_hash = generate_password_hash(password)
                
                # Update user password
                db_response = await update_user_password(user_id=user_id, password_hash=password_hash, session=session)
                if not db_response.get('success', False):
                    logger.error(f"Database error: {db_response.get('error')} for user_id: {user_id}")
                    return JSONResponse(
                        status_code=500,
                        content={"success": False, "message": "I was unable to update your password due to a system error. Please try again."}
                    )
                
                # await session.commit()
                # logger.info(f"Password updated successfully for user_id: {user_id}")
                
                # Log the event
                cloudwatch_data = {
                    "event": "Password reset",
                    "event_success": True,
                    "timestamp": datetime.now(timezone.utc).isoformat()
                }
                try:
                    asyncio.create_task(insert_cloudwatch_logs(data=cloudwatch_data, user_id=user_id))
                except Exception as e:
                    logger.error(f"Error in inserting cloudwatch logs for user_id: {user_id}: {e}")

                # blacklist the token
                # await redis_instance.redis_client.setex(f"blacklisted_token:{token}", (FORGOT_PASSWORD_EXPIRE_MINUTES*60)+3600, 1)

                # delete the reset token from redis
                await redis_instance.redis_client.delete(f"reset_password_token_for_user:{user_id}")

                logger.info(f"Password reset successfully for user_id: {user_id}")
                return ResetPasswordResponse(
                    success=True,
                    message="Password has been reset successfully"
                )
            
        except ExpiredSignatureError:
            logger.error("Token has expired for reset password")
            return JSONResponse(
                status_code=401,
                content={"success": False, "message": "Reset link has expired. Please request a new one."}
            )
        except JWTError:
            logger.error("Invalid token for reset password")
            return JSONResponse(
                status_code=401 ,
                content={"success": False, "message": "That reset link is invalid. Please start the password reset process over."}
            )
            
    except Exception as e:
        logger.error(f"Error in reset password: {e}")
        return JSONResponse(
            status_code=500,
            content={"success": False, "message": "I was unable to update your password due to a system error. Please try again."}
        )

@router.post("/send-verification-link", response_model=SendVerificationLinkResponse, responses={422: {"model": ErrorResponse}, 500: {"model": ErrorResponse}})
async def send_verification_link(data: SendVerificationLinkRequest):
    """Send verification link endpoint"""
    logger.info(f"Send verification link request received for email: {data.email}")
    
    try:
        email = data.email.strip().lower()
        
        # Validate input data
        if len(email) == 0:
            logger.error("Empty email received after trimming whitespace")
            return JSONResponse(
                status_code=422,
                content={"success": False, "message": "Email cannot be empty"}
            )
        
        if len(email) > 50:
            logger.error(f"Email received is too long: {email}")
            return JSONResponse(
                status_code=422,
                content={"success": False, "message": "Email cannot be longer than 50 characters"}
            )
        
        async with async_session_scope() as session:
            # Check if user exists
            db_response = await check_user_exists_by_email(email=email, session=session)
            
            if not db_response.get('success', False):
                logger.error(f"Database error: {db_response.get('error')}")
                return JSONResponse(
                    status_code=500,
                    content={"success": False, "message": "An unexpected error occurred. Please try requesting a new verification link."}
                )
            
            if not db_response.get('exists', False):
                logger.warning(f"User with email {email} not found")
                return JSONResponse(
                    status_code=401,
                    content={"success": False, "message": "I can't find that email in our system. That email is not registered. Please sign up to create an account."}
                )
            
            # Check if user is already verified
            if db_response.get('is_verified', False):
                logger.info(f"User with email {email} is already verified")
                return JSONResponse(
                    status_code=401,
                    content={"success": False, "message": "Your email is already verified."}
                )
            
            # Check if verification email has already been sent
            user_id = db_response.get('user_id')
            user_name = db_response.get('user_name')
            
            verification_email_key = f"verification_email_sent:{email}"
            is_verification_email_sent = await redis_instance.redis_client.exists(verification_email_key)
            
            if is_verification_email_sent:
                logger.info(f"Verification email has already been sent to {email} for user_id: {user_id}")
                return SendVerificationLinkResponse(
                    success=False,
                    message="Verification email has already been sent. Please check your inbox."
                )
            
            # Create verification token
            verification_token = create_verification_token(email=email)
            
            # Generate verification link with token
            verification_link = f"{FRONTEND_VERIFICATION_URL}?token={verification_token}"
            
            # Send verification email
            is_email_sent = await run_in_threadpool(lambda: send_signup_verification_email(
                recipient_email=email,
                user_name=user_name,
                verification_link=verification_link
            ))
            
            if not is_email_sent:
                logger.error(f"Failed to send verification email to {email} for user_id: {user_id}")
                return JSONResponse(
                    status_code=500,
                    content={"success": False, "message": "The verification email could not be sent due to a system error. Please try again."}
                )
            
            # Set verification email sent key in redis
            await redis_instance.redis_client.setex(
                verification_email_key, 
                SIGNUP_VERIFICATION_EMAIL_COOLDOWN_SECONDS, 
                1
            )

            # set the verification token sent to user in redis
            await redis_instance.redis_client.setex(
                f"verification_token_for_user:{user_id}",
                SIGNUP_VERIFICATION_EXPIRE_MINUTES * 60,
                verification_token
            )
            
            logger.info(f"Verification email sent to {email} for user_id: {user_id}")
            
            # Log the event
            cloudwatch_data = {
                "event": "Signup verification",
                "event_success": True,
                "timestamp": datetime.now(timezone.utc).isoformat()
            }
            try:
                asyncio.create_task(insert_cloudwatch_logs(data=cloudwatch_data, user_id=user_id))
            except Exception as e:
                logger.error(f"Error in inserting cloudwatch logs for user_id: {user_id}: {e}")
            
            return SendVerificationLinkResponse(
                success=True,
                message="Verification email has been sent. Please check your inbox."
            )
    
    except ValueError as e:
        # This catches email validation errors from Pydantic
        logger.error(f"Validation error in send verification link: {e}")
        return JSONResponse(
            status_code=422,
            content={"success": False, "message": "Invalid email format"}
        )
    except Exception as e:
        logger.error(f"Error in send verification link: {e}")
        return JSONResponse(
            status_code=500,
            content={"success": False, "message": "An unexpected error occurred. Please try requesting a new verification link."}
        )

@router.post("/verify-account-token", response_model=VerifyAccountTokenResponse, responses={422: {"model": ErrorResponse}, 500: {"model": ErrorResponse}})
async def verify_account_token(data: VerifyAccountTokenRequest):
    """Verify account token endpoint"""
    logger.info("Verify account token request received")
    
    try:
        verification_time = datetime.now(timezone.utc)
        token = data.token.strip()
        
        # Validate input data
        if len(token) == 0:
            logger.error("Empty token received after trimming whitespace")
            return JSONResponse(
                status_code=422,
                content={"success": False, "message": "Token cannot be empty"}
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
                    content={"success": False, "message": "That verification link appears to be invalid. Please try requesting a new one."}
                )
            
            # Get email from token
            email = payload.get("email")
            if not email:
                logger.error("Email not found in token")
                return JSONResponse(
                    status_code=401,
                    content={"success": False, "message": "There seems to be an issue with that link. Please try generating a new one from your account."}
                )
            
            async with async_session_scope() as session:
                # Check if user exists
                db_response = await check_user_exists_by_email(email=email, session=session)
                
                if not db_response.get('success', False):
                    logger.error(f"Database error: {db_response.get('error')}")
                    return JSONResponse(
                        status_code=500,
                        content={"success": False, "message": "We've hit an unexpected error during verification. Our team is on it. Please try again."}
                    )
                
                if not db_response.get('exists', False):
                    logger.warning(f"User with email {email} not found")
                    return JSONResponse(
                        status_code=401,
                        content={"success": False, "message": "Your email is not registered with us."}
                    )
                
                user_id = db_response.get('user_id')
                user_name = db_response.get('user_name')
                t_c_verified = db_response.get('t_c_verified', False)
                
                if not user_id:
                    logger.error(f"User ID not found for email: {email}")
                    return JSONResponse(
                        status_code=401,
                        content={"success": False, "message": "This email isn't registered with us yet. Please sign up to create an account."}
                    )
                
                # check if the verification token is valid
                verification_token = await redis_instance.redis_client.get(f"verification_token_for_user:{user_id}")
                
                if verification_token is None:
                    # Redis key expired or was deleted
                    logger.error(f"Verification token expired or not found for user_id: {user_id}")
                    return JSONResponse(
                        status_code=401,
                        content={"success": False, "message": "That verification link has expired. Please request a fresh one."}
                    )
                
                if verification_token != token:
                    # Token exists but doesn't match - user requested a new link
                    logger.error(f"Verification token mismatch for user_id: {user_id} - a newer link was issued")
                    return JSONResponse(
                        status_code=401,
                        content={"success": False, "message": "This verification link is no longer valid. Please use the most recent link sent to your email."}
                    )
                
                # Check if user is already verified
                if db_response.get('is_verified', False):
                    logger.info(f"User with email {email} and user_id: {user_id} is already verified")

                    # Generate JWT tokens for auto-login
                    access_token = create_access_token(user_id)
                    refresh_token = create_refresh_token(user_id)
                    
                    logger.info(f"Auto-login successful for already verified user with ID: {user_id}")
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
                db_response = await update_user_verification_status(email=email, verified_at=verification_time, is_verified=True, is_google_verified=False, session=session)
                
                if not db_response.get('success', False):
                    logger.error(f"Database error: {db_response.get('error')} for user_id: {user_id}")
                    return JSONResponse(
                        status_code=500,
                        content={"success": False, "message": "We've hit an unexpected error during verification. Our team is on it. Please try again."}
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
                    "timestamp": verification_time.isoformat()
                }
                try:
                    asyncio.create_task(insert_cloudwatch_logs(data=cloudwatch_data, user_id=user_id))
                except Exception as e:
                    logger.error(f"Error in inserting cloudwatch logs for user_id: {user_id}: {e}")
                
                logger.info(f"Account verified and auto-login successful for email: {email} and user_id: {user_id}")
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
                content={"success": False, "message": "That verification link has expired. Please request a fresh one."}
            )
        except JWTError:
            logger.error("Invalid token for verification")
            return JSONResponse(
                status_code=401,
                content={"success": False, "message": "That verification link appears to be invalid. Please try requesting a new one."}
            )
            
    except Exception as e:
        logger.error(f"Error in verify account token: {e}")
        return JSONResponse(
            status_code=500,
            content={"success": False, "message": "We've hit an unexpected error during verification. Our team is on it. Please try again."}
        )
    

@router.post("/generate-presentation", response_model=GeneratePresentationResponse, status_code=200, responses={404: {"model": ErrorResponse}, 401: {"model": ErrorResponse}, 422: {"model": ErrorResponse}, 500: {"model": ErrorResponse}})
async def generate_pptx(
    data: GeneratePresentationRequest,
    user_id: str = Depends(get_current_active_user)
):
    """Generate PowerPoint presentation from report. If report_version_id is provided, generates for that specific version."""
    set_functionality(Functionality.PPTX_GENERATION)
    report_id = data.report_id
    report_version_id = data.report_version_id
    generation_mode = (data.generation_mode or "template").strip().lower()
    if generation_mode not in ("template", "scratch"):
        generation_mode = "template"
    logger.info(f"Generate presentation request received for user_id: {user_id}, report_id: {report_id}, version_id: {report_version_id}, mode: {generation_mode}")
    
    # Check if disclaimer template exists
    # from src.core.report_util.pptx_utils import TEMPLATE_PATH #TODO add the Template in a asset folder
    # if os.path.exists(TEMPLATE_PATH):
    #     logger.info(f"Disclaimer template found at {TEMPLATE_PATH}")
    # else:
    #     logger.warning(f"Disclaimer template not found at {TEMPLATE_PATH}")
    
    try:
        temp_files: list = []

        # 1. Verify user
        async with async_session_scope() as session:
            db_response = await check_user_by_id(user_id=user_id, session=session)
            if not db_response.get('success', False):
                logger.error(f"Failed to check user by id: {db_response.get('error')} for user_id: {user_id}")
                return JSONResponse(
                    status_code=500,
                    content={"success": False, "error": "I ran into a problem while creating your presentation. Please try generating it again."}
                )
            if not db_response.get('exists', False):
                logger.error(f"User with ID {user_id} not found or token is invalid")
                return JSONResponse(
                    status_code=401,
                    content={"success": False, "error": "Unauthorized request"}
                )

        async with async_session_scope() as session:
            user_plan_result = await get_active_subscription(user_id=user_id, session=session)
            pptx_user_tier = user_plan_result.get("subscription", {}).get("current_tier", SubscriptionTier.FREE.value) if user_plan_result.get("subscription") else SubscriptionTier.FREE.value

        # 2. Get user details and report details
        async with async_session_scope() as session:
            # Get user details
            db_response = await get_user_details(user_id=user_id, session=session)
            if not db_response.get('success', False):
                logger.error(f"Database error: Failed to get user details: {db_response.get('error')} for user_id: {user_id}")
                return JSONResponse(
                    status_code=500,
                    content={"success": False, "error": "I ran into a problem while creating your presentation. Please try generating it again."}
                )
            user_name = db_response.get("user", {}).get('user_name')
            
            # Get report details and verify ownership
            ownership = await verify_report_ownership(report_id=report_id, user_id=user_id, session=session)
            if not ownership["authorized"]:
                return JSONResponse(
                    status_code=ownership["status_code"],
                    content={"success": False, "error": ownership["error"]}
                )
            report_data = ownership["report"]

            report_title = report_data.get('title', '')
            report_length = report_data.get('length')
            report_type_field = report_data.get('report_type')
            
            # ============================================================================
            # FLOW A: Specific report_version_id provided
            # ============================================================================
            target_version_for_generation = None
            use_specific_version = False
            new_version_created = False  # Track if a new version was created (important for s3_uri handling)
            
            if report_version_id:
                # Step 1: Validate report_version_id belongs to this report
                version_stmt = select(ReportVersion).where(
                    ReportVersion.id == report_version_id,
                    ReportVersion.report_id == report_id
                )
                version_result = await session.execute(version_stmt)
                target_version = version_result.scalar_one_or_none()
                
                if not target_version:
                    logger.warning(f"Version not found: {report_version_id} for report_id: {report_id}")
                    return JSONResponse(
                        status_code=404,
                        content={"success": False, "error": "The requested version was not found."}
                    )
                
                # Step 2: Check if PPTX already exists for this version
                s3_uri = target_version.s3_uri or {}
                if s3_uri.get('pptx'):
                    # Already exists → Return "up to date"
                    logger.info(f"PPTX already exists for version {target_version.version}, returning existing")
                    return JSONResponse(
                        status_code=200,
                        content={
                            "success": True,
                            "message": "Your presentation is up to date. No new changes have been detected in your report since the last presentation was generated.",
                            "report_id": report_id,
                            "report_version_id": target_version.id,
                            "version": target_version.version,
                            "pptx_s3_uri": s3_uri.get('pptx'),
                            "pptx_generation_time": s3_uri.get('pptx_generation_time'),
                            "already_exists": True
                        }
                    )
                
                # Step 3: PPTX doesn't exist → Will generate for this specific version
                target_version_for_generation = target_version
                use_specific_version = True
                # Set version info from the specific version (NOT from active version)
                current_version = target_version.version
                target_version_id = target_version.id
                logger.info(f"Will generate PPTX for specific version {target_version.version}")
            
            # ============================================================================
            # FLOW B: No report_version_id provided - Use Active Version
            # Only run this logic when NOT using a specific version
            # ============================================================================
            if not use_specific_version:
                # Step 1: Get active version (single DB fetch - reuse data throughout)
                version_stmt = select(ReportVersion).where(
                    ReportVersion.report_id == report_id,
                    ReportVersion.is_active.is_(True)
                )
                version_result = await session.execute(version_stmt)
                active_version = version_result.scalar_one_or_none()
                
                if not active_version:
                    return JSONResponse(
                        status_code=404,
                        content={"success": False, "error": "No active version found for this report."}
                    )
                
                # Store version data for reuse (optimization: avoid extra DB calls)
                current_version = active_version.version
                current_s3_uri = active_version.s3_uri or {}
                target_version_id = active_version.id
                
                logger.info(f"PPTX generation for report_id: {report_id}, active version: {current_version}")
                
                # Step 2: Check if ANY output exists for this version
                # This determines if we need to check for card modifications
                has_any_output = (
                    active_version.generated_at or                      # PDF generated
                    current_s3_uri.get('md_generation_time') or         # MD generated
                    current_s3_uri.get('html_generation_time') or       # HTML generated
                    current_s3_uri.get('info_pdf_generation_time') or   # Infographic generated
                    current_s3_uri.get('pptx_generation_time')          # PPTX generated
                )
                
                # Step 3: Apply versioning logic based on has_any_output
                if has_any_output:
                    # ----------------------------------------------------------------
                    # Version has at least one output → Check for card modifications
                    # ----------------------------------------------------------------
                    logger.info(f"Version {current_version} has existing output(s). Checking for card modifications...")
                    
                    detection_result = await detect_modified_cards(
                        report_id=report_id, 
                        session=session, 
                        generation_type="presentation"
                    )
                    
                    if not detection_result.get('success'):
                        logger.error(f"Failed to detect card modifications: {detection_result.get('error')}")
                        return JSONResponse(
                            status_code=500,
                            content={"success": False, "error": "I ran into a problem while creating your presentation. Please try generating it again."}
                        )
                    
                    modified_count = detection_result.get('modified_count', 0)
                    logger.info(f"Card modification check: {modified_count} modified, {detection_result.get('unchanged_count', 0)} unchanged")
                    
                    if modified_count > 0:
                        # ----------------------------------------------------------------
                        # Cards MODIFIED → Create new version, then generate
                        # ----------------------------------------------------------------
                        logger.info(f"Cards modified since last output. Creating new version...")
                        
                        # Enforce per-tier version limit before creating a new version
                        _pptx_is_paid = pptx_user_tier != SubscriptionTier.FREE.value
                        _pptx_max_versions = MAX_REPORT_VERSIONS_PAID if _pptx_is_paid else MAX_REPORT_VERSIONS_FREE
                        if active_version.version >= _pptx_max_versions:
                            _upgrade_hint = "" if _pptx_is_paid else " Upgrade to a paid plan to unlock up to 5 versions."
                            logger.warning(f"User {user_id} hit version limit ({_pptx_max_versions}) for report_id: {report_id}")
                            return JSONResponse(
                                status_code=403,
                                content={
                                    "success": False,
                                    "error": f"This report has reached the maximum of {_pptx_max_versions} versions.{_upgrade_hint}"
                                }
                            )
                        
                        # Prepare cards data for new version
                        modified_cards_data = detection_result.get('modified_cards', [])
                        unchanged_cards_data = detection_result.get('unchanged_cards', [])
                        cards_data = modified_cards_data + unchanged_cards_data
                        cards_data.sort(key=lambda x: x.get("sequence", 0))
                        
                        # Create new version
                        version_response = await create_new_report_version(
                            session=session,
                            report_id=report_id,
                            cards_data=cards_data
                        )
                        
                        if not version_response.get('success'):
                            return JSONResponse(
                                status_code=500,
                                content={"success": False, "error": "I ran into a problem while creating your presentation. Please try generating it again."}
                            )
                        
                        # Update to new version
                        current_version = version_response.get('version')
                        target_version_id = version_response.get('version_id')
                        new_version_created = True  # Flag that new version was created
                        logger.info(f"Created new version {current_version} for PPTX generation")
                        
                        # Reset Report table fields for new version
                        await update_report(
                            report_id=report_id,
                            update_data={
                                "s3_uri": {"md": None, "html": None, "pdf": None, "pptx": None, "info_pdf": None},
                                "generated_at": None,
                                "status": ReportStatus.REDO_ANALYSIS.value,
                                "current_version": current_version
                            },
                            session=session
                        )
                    else:
                        # ----------------------------------------------------------------
                        # Cards NOT modified → Check if PPTX already exists
                        # ----------------------------------------------------------------
                        if current_s3_uri.get('pptx'):
                            # PPTX already exists for this version → Return "up to date"
                            logger.info(f"PPTX already exists for version {current_version} and no changes detected")
                            return JSONResponse(
                                status_code=200,
                                content={
                                    "success": True,
                                    "message": "Your presentation is up to date. No new changes have been detected in your report since the last presentation was generated.",
                                    "report_id": report_id,
                                    "report_version_id": active_version.id,
                                    "version": current_version,
                                    "pptx_s3_uri": current_s3_uri.get('pptx'),
                                    "pptx_generation_time": current_s3_uri.get('pptx_generation_time'),
                                    "already_exists": True
                                }
                            )
                        else:
                            # PPTX doesn't exist → Generate for current version (no new version needed)
                            logger.info(f"No modifications, but PPTX not yet generated. Generating for version {current_version}...")
                else:
                    # ----------------------------------------------------------------
                    # No outputs exist yet → Generate for current version (first output)
                    # This will "lock" the cards to this version
                    # ----------------------------------------------------------------
                    logger.info(f"No outputs exist for version {current_version}. Generating PPTX (first output)...")
            
        # Log "Presentation generation started" in background
        async def log_presentation_start():
            try:
                # Get chat_id and chat_title
                chat_id = report_data.get('chat_id')
                chat_title = None
                if chat_id:
                    async with async_session_scope() as session:
                        chat_response = await get_user_chat(user_id=user_id, chat_id=chat_id, session=session)
                        if chat_response.get('success', False) and chat_response.get('message'):
                            chat_title = chat_response.get('message', {}).get('chat_title')
                
                start_data = {
                    "event": "Presentation generation started",
                    "event_success": True,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "chat_id": chat_id or "N/A",
                    "chat_title": chat_title or "N/A"
                }
                await insert_cloudwatch_logs(data=start_data, user_id=user_id)
                logger.info(f"Logged 'Presentation generation started' for report_id: {report_id}")
            except Exception as e:
                logger.error(f"Error logging presentation start: {e} for report_id: {report_id}")
        
        asyncio.create_task(log_presentation_start())
        
        # Set status to GENERATING_OUTPUT when PPTX generation starts
        async with async_session_scope() as session:
            await update_report_status_by_chat_or_report_id(
                report_id=report_id,
                status=ReportStatus.GENERATING_OUTPUT.value,
                session=session
            )
            logger.info(f"Set report status to GENERATING_OUTPUT for PPTX generation, report_id: {report_id}")
        
        async with async_session_scope() as session:
            total_slides = content_slides_for_report(
                report_type=report_type_field,
                length=report_length,
            )
            logger.info(
                "report_type=%s length=%s → content slides: %d (plus 3 structural slides)",
                report_type_field,
                report_length,
                total_slides,
            )
            
            # Get report cards
            # If specific version requested (Flow A), use version's locked snapshot
            # Otherwise use current active cards
            if use_specific_version and target_version_for_generation:
                db_response = await get_cards_for_version(
                    report_id=report_id,
                    version_id=target_version_for_generation.id,
                    session=session
                )
                logger.info(f"Using cards from version snapshot for version {target_version_for_generation.version}")
            else:
                db_response = await get_report_cards(report_id=report_id, session=session)
            
            if not db_response.get('success', False):
                logger.error(f"Failed to get report cards: {db_response.get('error')} for report_id: {report_id}")
                return JSONResponse(
                    status_code=500,
                    content={"success": False, "error": "I ran into a problem while creating your presentation. Please try generating it again."}
                )
            cards = db_response.get('cards', [])

        # 4. Generate markdown from cards
        markdown_content = await generate_pptx_markdown_from_report(report_id, cards)
        if not markdown_content:
            logger.error(f"Failed to generate markdown content for report_id: {report_id}")
            return JSONResponse(
                status_code=500,
                content={"success": False, "error": "I ran into a problem while creating your presentation. Please try generating it again."}
            )

        # 5–7. Generate PPTX, append disclaimer slide, upload to S3 in one call
        base_filename = (report_title or "report").strip(".").strip().replace(":", " ").replace("-", " ").replace(" ", "_")
        base_filename = re.sub(r"_+", "_", base_filename)
        pptx_filename = f"{base_filename}.pptx"

        prefix = build_report_s3_prefix(
            user_id=user_id,
            user_name=user_name,
            chat_id=report_data.get('chat_id') or report_id,
            chat_title=report_title,
            report_id=report_id,
            version=current_version,
        )
        pptx_key = f"{prefix}/report/{pptx_filename}"
        logger.info(f"Generating and uploading PPTX to {pptx_key} for report_id={report_id}")

        try:
            pptx_s3_uri = await generate_pptx_and_upload_s3(
                md_content=markdown_content,
                total_slides=total_slides,
                s3_key=pptx_key,
                report_id=report_id,
                generation_mode=generation_mode,
                user_id=user_id,
                chat_id=report_data.get('chat_id'),
                report_version_id=(target_version_for_generation.id if use_specific_version and target_version_for_generation else None),
            )
        except Exception as e:
            logger.error(
                f"Failed to generate/upload PPTX for report_id={report_id}: {e}",
                exc_info=True,
            )
            return JSONResponse(
                status_code=500,
                content={"success": False, "error": "I ran into a problem while creating your presentation. Please try generating it again."}
            )

        # 8. Update s3_uri JSON field and status
        # IMPORTANT: When generating for a specific OLD (non-active) version, only update that version's s3_uri
        # When generating for the ACTIVE version (whether report_version_id provided or not), update BOTH tables
        # 
        # CRITICAL: When a NEW version was created, start with fresh s3_uri (don't carry over old version's outputs)
        # Otherwise, get the current s3_uri from the active version (NOT from stale report_data)
        if new_version_created:
            # New version created - start with fresh s3_uri
            updated_uris = {}
            logger.info(f"New version {current_version} created - starting PPTX update with fresh s3_uri")
        else:
            # Existing version - get current s3_uri from active version (fresh from DB)
            async with async_session_scope() as session:
                version_stmt = select(ReportVersion).where(
                    ReportVersion.report_id == report_id,
                    ReportVersion.is_active.is_(True)
                )
                version_result = await session.execute(version_stmt)
                active_ver = version_result.scalar_one_or_none()
                updated_uris = dict(active_ver.s3_uri or {}) if active_ver else {}
            logger.info(f"Existing version {current_version} - PPTX update will merge with current s3_uri")
        
        updated_uris["pptx"] = pptx_s3_uri
        updated_uris["pptx_generation_time"] = datetime.now(timezone.utc).isoformat()
        
        async with async_session_scope() as session:
            # Check if the provided version is the active version
            is_generating_for_old_version = (
                use_specific_version and 
                target_version_for_generation and 
                not target_version_for_generation.is_active
            )
            
            if is_generating_for_old_version:
                # ========================================
                # FLOW A: Generating for a SPECIFIC OLD (non-active) version
                # Update only the specific version's s3_uri, NOT the Report table
                # Report table should continue reflecting the current active version's data
                # ========================================
                logger.info(f"Updating PPTX s3_uri for specific OLD version {current_version} (NOT updating Report table)")
                db_response = await update_specific_version_s3_uri(
                    report_id=report_id,
                    version_id=target_version_for_generation.id,
                    s3_uri_updates={"pptx": pptx_s3_uri, "pptx_generation_time": updated_uris["pptx_generation_time"]},
                    session=session
                )
                if not db_response.get('success', False):
                    logger.error(f"Failed to update specific version PPTX s3_uri: {db_response.get('error')} for report_id: {report_id}")
                    return JSONResponse(
                        status_code=500,
                        content={"success": False, "error": "I ran into a problem while creating your presentation. Please try generating it again."}
                    )
                logger.info(f"Generated PPTX for specific old version {current_version} - cards already linked from version snapshot")
            else:
                # ========================================
                # FLOW B: Generating for CURRENT/ACTIVE version
                # Update both Report table and active ReportVersion
                # This covers:
                #   - No report_version_id provided (use_specific_version=False)
                #   - report_version_id provided but it's the active version
                # ========================================
                logger.info(f"Updating PPTX for active version {current_version}, preserving other files")
                db_response = await update_report(
                    report_id=report_id, 
                    update_data={
                        "s3_uri": updated_uris
                        # Status will be updated to OUTPUT_GENERATED after successful completion
                    }, 
                    session=session
                )
                if not db_response.get('success', False):
                    logger.error(f"Failed to update report: {db_response.get('error')} for report_id: {report_id}")
                    return JSONResponse(
                        status_code=500,
                        content={"success": False, "error": "I ran into a problem while creating your presentation. Please try generating it again."}
                    )
                
                # Finalize version by linking current cards (first output locks the cards)
                # For version 1 (first time generation): Call finalize_report_version to populate report_version_cards
                # For version 2+: Already populated by create_new_report_version, skip finalization
                if current_version == 1:
                    logger.info(f"Finalizing version 1 for PPTX generation, report_id: {report_id}")
                    finalize_result = await finalize_report_version(
                        session=session,
                        report_id=report_id
                    )
                    if finalize_result.get('success'):
                        cards_linked = finalize_result.get('cards_linked', 0)
                        already_finalized = finalize_result.get('already_finalized', False)
                        if already_finalized:
                            logger.info(f"Version 1 already finalized for PPTX, report_id: {report_id}")
                        else:
                            logger.info(f"Finalized version 1 with {cards_linked} cards for PPTX, report_id: {report_id}")
                    else:
                        logger.warning(f"Failed to finalize version 1 for PPTX: {finalize_result.get('error')}")

        logger.info(f"Presentation generated successfully for report_id: {report_id}")
        
        # Log "Presentation generation completed" in background
        async def log_presentation_completion():
            try:
                # Get chat_id and chat_title
                chat_id = report_data.get('chat_id')
                chat_title = None
                if chat_id:
                    async with async_session_scope() as session:
                        chat_response = await get_user_chat(user_id=user_id, chat_id=chat_id, session=session)
                        if chat_response.get('success', False) and chat_response.get('message'):
                            chat_title = chat_response.get('message', {}).get('chat_title')
                
                completion_data = {
                    "event": "Presentation generation completed",
                    "event_success": True,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "chat_id": chat_id or "N/A",
                    "chat_title": chat_title or "N/A"
                }
                await insert_cloudwatch_logs(data=completion_data, user_id=user_id)
                logger.info(f"Logged 'Presentation generation completed' for report_id: {report_id}")
            except Exception as e:
                logger.error(f"Error logging presentation completion: {e} for report_id: {report_id}")
        
        asyncio.create_task(log_presentation_completion())
        
        # Set status to OUTPUT_GENERATED when PPTX generation completes successfully
        async with async_session_scope() as session:
            await update_report_status_by_chat_or_report_id(
                report_id=report_id,
                status=ReportStatus.OUTPUT_GENERATED.value,
                session=session
            )
            logger.info(f"Set report status to OUTPUT_GENERATED for PPTX generation, report_id: {report_id}")

        # Get version ID for response
        final_version_id = None
        if use_specific_version and target_version_for_generation:
            final_version_id = target_version_for_generation.id
        else:
            async with async_session_scope() as session:
                version_stmt = select(ReportVersion).where(
                    ReportVersion.report_id == report_id,
                    ReportVersion.version == current_version
                )
                version_result = await session.execute(version_stmt)
                version_obj = version_result.scalar_one_or_none()
                if version_obj:
                    final_version_id = version_obj.id
        
        return GeneratePresentationResponse(
            success=True,
            message="Presentation has been generated successfully.",
            report_version_id=final_version_id,
            version=current_version,
            pptx_s3_uri=pptx_s3_uri,
            pptx_generation_time=updated_uris.get("pptx_generation_time"),
            report_id=report_id,
            modified=True,  # New PPTX was generated
            already_exists=False  # Newly generated, not pre-existing
        )

    except Exception as e:
        logger.error(f"Error generating presentation: {e}")
        # Update report status to ERROR_GENERATION_REPORT on error
        try:
            async with async_session_scope() as session:
                await update_report_status_by_chat_or_report_id(
                    report_id=report_id,
                    status=ReportStatus.ERROR_GENERATION_REPORT.value,
                    session=session
                )
                logger.info(f"Set report status to ERROR_GENERATION_REPORT for PPTX generation, report_id: {report_id}")
        except Exception as update_error:
            logger.error(f"Failed to update report status to ERROR_GENERATION_REPORT: {update_error}")
        
        # Clean up any temporary files
        for file_path in temp_files:
            try:
                if os.path.exists(file_path):
                    os.unlink(file_path)
            except:
                pass
        return JSONResponse(
            status_code=500,
            content={"success": False, "error": "I ran into a problem while creating your presentation. Please try generating it again."}
        )


@router.post("/generate-infographic", response_model=GenerateInfographicResponse, status_code=200, responses={404: {"model": ErrorResponse}, 401: {"model": ErrorResponse}, 422: {"model": ErrorResponse}, 500: {"model": ErrorResponse}})
async def generate_infographic(
    data: GenerateInfographicRequest,
    user_id: str = Depends(get_current_active_user)
):
    """
    Generate infographic PDF for a report.
    
    Logic:
    - Uses current timestamp for infographic generation (no correlation with Report.generated_at)
    - Stores info_pdf_generation_time in s3_uri JSONB only (like PPTX does with pptx_generation_time)
    - Follows same versioning logic as PPTX:
      - If infographic exists and no changes → return existing
      - If infographic exists and changes detected → create new version
      - If no infographic exists → generate for current version
    - Poster image: First generation (report OR infographic) creates it, subsequent reuse it
    - If report_version_id is provided, generates for that specific version
    """
    # Attribute all LLM spend in this request to the "infographic" functionality.
    set_functionality(Functionality.INFOGRAPHIC)
    report_id = data.report_id
    report_version_id = data.report_version_id
    logger.info(f"Generate infographic request received for user_id: {user_id}, report_id: {report_id}, version_id: {report_version_id}")
    
    try:
        # 1. Validate user
        async with async_session_scope() as session:
            db_response = await check_user_by_id(user_id=user_id, session=session)
            if not db_response.get('success', False):
                logger.error(f"Failed to check user by id: {db_response.get('error')} for user_id: {user_id} and report_id: {report_id}")
                return JSONResponse(
                    status_code=500,
                    content={"success": False, "error": "Something unexpected happened. Please try again later."}
                )
            if not db_response.get('exists', False):
                logger.error(f"User with ID {user_id} not found or token is invalid for report_id: {report_id}")
                return JSONResponse(
                    status_code=401,
                    content={"success": False, "error": "Unauthorized request"}
                )

        async with async_session_scope() as session:
            user_plan_result = await get_active_subscription(user_id=user_id, session=session)
            info_user_tier = user_plan_result.get("subscription", {}).get("current_tier", SubscriptionTier.FREE.value) if user_plan_result.get("subscription") else SubscriptionTier.FREE.value

        # 2. Get user details
        async with async_session_scope() as session:
            db_response = await get_user_details(user_id=user_id, session=session)
            if not db_response.get('success', False):
                logger.error(f"Database error: Failed to get user details: {db_response.get('error')} for user_id: {user_id} and report_id: {report_id}")
                return JSONResponse(
                    status_code=500,
                    content={"success": False, "error": "Something unexpected happened. Please try again later."}
                )
        
        user_name = db_response.get("user", {}).get('user_name', None)
        
        # 3. Get report details and verify ownership
        async with async_session_scope() as session:
            ownership = await verify_report_ownership(report_id=report_id, user_id=user_id, session=session)
            if not ownership["authorized"]:
                return JSONResponse(
                    status_code=ownership["status_code"],
                    content={"success": False, "error": ownership["error"]}
                )
        
        report_data = ownership["report"]
        report_title = report_data.get('title', '')
        
        # ============================================================================
        # FLOW A: Specific report_version_id provided
        # ============================================================================
        target_version_for_generation = None
        use_specific_version = False
        new_version_created = False  # Track if a new version was created (important for s3_uri handling)
        
        if report_version_id:
            async with async_session_scope() as session:
                # Step 1: Validate report_version_id belongs to this report
                version_stmt = select(ReportVersion).where(
                    ReportVersion.id == report_version_id,
                    ReportVersion.report_id == report_id
                )
                version_result = await session.execute(version_stmt)
                target_version = version_result.scalar_one_or_none()
                
                if not target_version:
                    logger.warning(f"Version not found: {report_version_id} for report_id: {report_id}")
                    return JSONResponse(
                        status_code=404,
                        content={"success": False, "error": "The requested version was not found."}
                    )
                
                # Step 2: Check if infographic already exists for this version
                s3_uri = target_version.s3_uri or {}
                if s3_uri.get('info_pdf'):
                    # Already exists → Return "up to date"
                    logger.info(f"Infographic already exists for version {target_version.version}, returning existing")
                    return JSONResponse(
                        status_code=200,
                        content={
                            "success": True,
                            "message": "Your infographic is up to date. No new changes have been detected in your report since the last infographic was generated.",
                            "report_id": report_id,
                            "report_version_id": target_version.id,
                            "version": target_version.version,
                            "info_pdf_s3_uri": s3_uri.get('info_pdf'),
                            "info_pdf_generation_time": s3_uri.get('info_pdf_generation_time'),
                            "already_exists": True
                        }
                    )
                
                # Step 3: Infographic doesn't exist → Will generate for this specific version
                target_version_for_generation = target_version
                use_specific_version = True
                # Set version info from the specific version (NOT from active version)
                current_version = target_version.version
                target_version_id = target_version.id
                logger.info(f"Will generate infographic for specific version {target_version.version}")
        
        # ============================================================================
        # FLOW B: No report_version_id provided - Use Active Version
        # Only run this logic when NOT using a specific version
        # ============================================================================
        if not use_specific_version:
            async with async_session_scope() as session:
                # Step 1: Get active version (single DB fetch - reuse data throughout)
                version_stmt = select(ReportVersion).where(
                    ReportVersion.report_id == report_id,
                    ReportVersion.is_active.is_(True)
                )
                version_result = await session.execute(version_stmt)
                active_version = version_result.scalar_one_or_none()
                
                if not active_version:
                    return JSONResponse(
                        status_code=404,
                        content={"success": False, "error": "No active version found for this report."}
                    )
                
                # Store version data for reuse (optimization: avoid extra DB calls)
                current_version = active_version.version
                current_s3_uri = active_version.s3_uri or {}
                target_version_id = active_version.id
                
                logger.info(f"Infographic generation for report_id: {report_id}, active version: {current_version}")
                
                # Step 2: Check if ANY output exists for this version
                # This determines if we need to check for card modifications
                has_any_output = (
                    active_version.generated_at or                      # PDF generated
                    current_s3_uri.get('md_generation_time') or         # MD generated
                    current_s3_uri.get('html_generation_time') or       # HTML generated
                    current_s3_uri.get('info_pdf_generation_time') or   # Infographic generated
                    current_s3_uri.get('pptx_generation_time')          # PPTX generated
                )
                
                # Step 3: Apply versioning logic based on has_any_output
                if has_any_output:
                    # ----------------------------------------------------------------
                    # Version has at least one output → Check for card modifications
                    # ----------------------------------------------------------------
                    logger.info(f"Version {current_version} has existing output(s). Checking for card modifications...")
                    
                    detection_result = await detect_modified_cards(
                        report_id=report_id, 
                        session=session, 
                        generation_type="infographic"
                    )
                    
                    if not detection_result.get('success'):
                        logger.error(f"Failed to detect card modifications: {detection_result.get('error')}")
                        return JSONResponse(
                            status_code=500,
                            content={"success": False, "error": "I ran into a problem while creating your infographic. Please try generating it again."}
                        )
                    
                    modified_count = detection_result.get('modified_count', 0)
                    logger.info(f"Card modification check: {modified_count} modified, {detection_result.get('unchanged_count', 0)} unchanged")
                    
                    if modified_count > 0:
                        # ----------------------------------------------------------------
                        # Cards MODIFIED → Create new version, then generate
                        # ----------------------------------------------------------------
                        logger.info(f"Cards modified since last output. Creating new version...")
                        
                        # Enforce per-tier version limit before creating a new version
                        _info_is_paid = info_user_tier != SubscriptionTier.FREE.value
                        _info_max_versions = MAX_REPORT_VERSIONS_PAID if _info_is_paid else MAX_REPORT_VERSIONS_FREE
                        if active_version.version >= _info_max_versions:
                            _upgrade_hint = "" if _info_is_paid else " Upgrade to a paid plan to unlock up to 5 versions."
                            logger.warning(f"User {user_id} hit version limit ({_info_max_versions}) for report_id: {report_id}")
                            return JSONResponse(
                                status_code=403,
                                content={
                                    "success": False,
                                    "error": f"This report has reached the maximum of {_info_max_versions} versions.{_upgrade_hint}"
                                }
                            )
                        
                        # Prepare cards data for new version
                        modified_cards_data = detection_result.get('modified_cards', [])
                        unchanged_cards_data = detection_result.get('unchanged_cards', [])
                        cards_data = modified_cards_data + unchanged_cards_data
                        cards_data.sort(key=lambda x: x.get("sequence", 0))
                        
                        # Create new version
                        version_response = await create_new_report_version(
                            session=session,
                            report_id=report_id,
                            cards_data=cards_data
                        )
                        
                        if not version_response.get('success'):
                            return JSONResponse(
                                status_code=500,
                                content={"success": False, "error": "I ran into a problem while creating your infographic. Please try generating it again."}
                            )
                        
                        # Update to new version
                        current_version = version_response.get('version')
                        target_version_id = version_response.get('version_id')
                        new_version_created = True  # Flag that new version was created
                        logger.info(f"Created new version {current_version} for infographic generation")
                        
                        # Reset Report table fields for new version
                        await update_report(
                            report_id=report_id,
                            update_data={
                                "s3_uri": {"md": None, "html": None, "pdf": None, "pptx": None, "info_pdf": None},
                                "generated_at": None,
                                "status": ReportStatus.REDO_ANALYSIS.value,
                                "current_version": current_version
                            },
                            session=session
                        )
                    else:
                        # ----------------------------------------------------------------
                        # Cards NOT modified → Check if Infographic already exists
                        # ----------------------------------------------------------------
                        if current_s3_uri.get('info_pdf'):
                            # Infographic already exists for this version → Return "up to date"
                            logger.info(f"Infographic already exists for version {current_version} and no changes detected")
                            return JSONResponse(
                                status_code=200,
                                content={
                                    "success": True,
                                    "message": "Your infographic is up to date. No new changes have been detected in your report since the last infographic was generated.",
                                    "report_id": report_id,
                                    "report_version_id": active_version.id,
                                    "version": current_version,
                                    "info_pdf_s3_uri": current_s3_uri.get('info_pdf'),
                                    "info_pdf_generation_time": current_s3_uri.get('info_pdf_generation_time'),
                                    "already_exists": True
                                }
                            )
                        else:
                            # Infographic doesn't exist → Generate for current version (no new version needed)
                            logger.info(f"No modifications, but infographic not yet generated. Generating for version {current_version}...")
                else:
                    # ----------------------------------------------------------------
                    # No outputs exist yet → Generate for current version (first output)
                    # This will "lock" the cards to this version
                    # ----------------------------------------------------------------
                    logger.info(f"No outputs exist for version {current_version}. Generating infographic (first output)...")
        
        # Set status to GENERATING_OUTPUT when infographic generation starts
        async with async_session_scope() as session:
            await update_report_status_by_chat_or_report_id(
                report_id=report_id,
                status=ReportStatus.GENERATING_OUTPUT.value,
                session=session
            )
            logger.info(f"Set report status to GENERATING_OUTPUT for infographic generation, report_id: {report_id}")
        
        # 5. Get report cards
        # If specific version requested (Flow A), use version's locked snapshot
        # Otherwise use current active cards
        async with async_session_scope() as session:
            if use_specific_version and target_version_for_generation:
                db_response = await get_cards_for_version(
                    report_id=report_id,
                    version_id=target_version_for_generation.id,
                    session=session
                )
                logger.info(f"Using cards from version snapshot for version {target_version_for_generation.version}")
            else:
                db_response = await get_report_cards(report_id=report_id, session=session)
            
            if not db_response.get('success', False):
                return JSONResponse(
                    status_code=500,
                    content={"success": False, "error": "Failed to get report cards."}
                )
        
        cards = db_response.get('cards', [])
        if not cards:
            return JSONResponse(
                status_code=404,
                content={"success": False, "error": "No report cards found."}
            )
        
        # 6. Generate markdown from cards (using same function as PPTX)
        
        raw_md_content = await generate_markdown_from_report(report_id, cards)
        
        if not raw_md_content:
            logger.error(f"Failed to generate markdown for report_id: {report_id}")
            return JSONResponse(
                status_code=500,
                content={"success": False, "error": "Failed to build report content."}
            )
        
        # 7. Use current timestamp for infographic generation (no correlation with Report.generated_at)
        infographic_generation_time = datetime.now(timezone.utc)
        logger.info(f"Infographic generation time: {infographic_generation_time}")
        
        # 8. Handle poster image (first generation creates it, subsequent reuse it)
        async with async_session_scope() as session:
            report_stmt = select(Report).where(Report.id == report_id)
            report_result = await session.execute(report_stmt)
            report = report_result.scalar_one_or_none()
            
            existing_poster_url = report.poster_image_url if report else None
        
        logger.info(f"Existing poster URL for infographic generation: {existing_poster_url}")
        
        # 9. Get chat info for S3 path
        chat_id = report_data.get('chat_id')
        chat_title = report_title  # Fallback
        if chat_id:
            async with async_session_scope() as session:
                chat_response = await get_user_chat(user_id=user_id, chat_id=chat_id, session=session)
                if chat_response.get('success', False) and chat_response.get('message'):
                    chat_title = chat_response.get('message', {}).get('chat_title', chat_title)
        
        # Get report subtitle from cards
        report_subtitle = ""
        for card in cards:
            if card.get('type') == 'subtitle':
                report_subtitle = extract_content(card, "content")
                break          

        # 10. Generate infographic PDF        
        infographic_s3_uri, newly_generated_poster_url = await run_in_threadpool(
            lambda: generate_one_pager(
                user_id=user_id,
                user_name=user_name,
                report_id=report_id,
                report_markdown=raw_md_content,
                report_title=report_title,
                report_subtitle=report_subtitle,
                poster_image_url=existing_poster_url,
                report_date=infographic_generation_time.strftime("%d %B %Y"),
                chat_id=chat_id,
                chat_title=chat_title,
                version=current_version,
                report_generation_time=infographic_generation_time
            )
        )
        
        if not infographic_s3_uri:
            # Update status to error
            async with async_session_scope() as session:
                await update_report_status_by_chat_or_report_id(
                    report_id=report_id,
                    status=ReportStatus.ERROR_GENERATION_REPORT.value,
                    session=session
                )
            return JSONResponse(
                status_code=500,
                content={"success": False, "error": "I ran into a problem while creating your infographic. Please try generating it again."}
            )
        
        logger.info(f"Infographic PDF generated successfully: {infographic_s3_uri}")
        
        # 11. Save newly generated poster URL
        # IMPORTANT: When generating for a specific OLD (non-active) version, only update that version's poster
        # When generating for the ACTIVE version (whether report_version_id provided or not), update BOTH tables
        
        # Check if the provided version is the active version (for poster update)
        is_generating_for_old_version_poster = (
            use_specific_version and 
            target_version_for_generation and 
            not target_version_for_generation.is_active
        )
        
        if newly_generated_poster_url:
            logger.info(f"New poster image was generated during infographic creation: {newly_generated_poster_url}")
            async with async_session_scope() as session:
                if is_generating_for_old_version_poster:
                    # Only update the specific old version's poster_image_url
                    version_stmt = select(ReportVersion).where(
                        ReportVersion.id == target_version_for_generation.id
                    )
                    version_result = await session.execute(version_stmt)
                    version = version_result.scalar_one_or_none()
                    
                    if version:
                        version.poster_image_url = newly_generated_poster_url
                        await session.commit()
                        logger.info(f"Saved new poster image URL to specific OLD ReportVersion (v{current_version}), NOT Report table: {newly_generated_poster_url}")
                else:
                    # Update both Report table and active ReportVersion
                    await update_report(
                        report_id=report_id,
                        update_data={"poster_image_url": newly_generated_poster_url},
                        session=session
                    )
                    logger.info(f"Saved new poster image URL to Report table: {newly_generated_poster_url}")
                    
                    # Also update ReportVersion table with the poster URL
                    version_stmt = select(ReportVersion).where(
                        ReportVersion.report_id == report_id,
                        ReportVersion.version == current_version
                    )
                    version_result = await session.execute(version_stmt)
                    version = version_result.scalar_one_or_none()
                    
                    if version:
                        version.poster_image_url = newly_generated_poster_url
                        await session.commit()
                        logger.info(f"Saved new poster image URL to ReportVersion table (version {current_version}): {newly_generated_poster_url}")
        
        # 12. Update s3_uri with infographic path (info_pdf_generation_time in s3_uri only - no Report.generated_at correlation)
        # IMPORTANT: When generating for a specific OLD (non-active) version, only update that version's s3_uri
        # When generating for the ACTIVE version (whether report_version_id provided or not), update BOTH tables
        
        # Check if the provided version is the active version
        is_generating_for_old_version = (
            use_specific_version and 
            target_version_for_generation and 
            not target_version_for_generation.is_active
        )
        
        # Get current s3_uri from the appropriate version
        # CRITICAL: When a NEW version was created, start with fresh s3_uri (don't carry over old version's outputs)
        if new_version_created:
            # New version created - start with fresh s3_uri
            current_s3_uri = {}
            logger.info(f"New version {current_version} created - starting infographic update with fresh s3_uri")
        elif is_generating_for_old_version:
            # Get s3_uri from the specific old version
            current_s3_uri = target_version_for_generation.s3_uri or {}
        else:
            # Get s3_uri from active version (fresh from DB)
            async with async_session_scope() as session:
                version_stmt = select(ReportVersion).where(
                    ReportVersion.report_id == report_id,
                    ReportVersion.is_active.is_(True)
                )
                version_result = await session.execute(version_stmt)
                active_version = version_result.scalar_one_or_none()
                current_s3_uri = active_version.s3_uri if active_version else {}
            logger.info(f"Existing version {current_version} - infographic update will merge with current s3_uri")
        
        updated_uris = dict(current_s3_uri)
        updated_uris["info_pdf"] = infographic_s3_uri
        updated_uris["info_pdf_generation_time"] = datetime.now(timezone.utc).isoformat()
        
        async with async_session_scope() as session:
            if is_generating_for_old_version:
                # ========================================
                # FLOW A: Generating for a SPECIFIC OLD (non-active) version
                # Update only the specific version's s3_uri, NOT the Report table
                # Report table should continue reflecting the current active version's data
                # ========================================
                logger.info(f"Updating infographic s3_uri for specific OLD version {current_version} (NOT updating Report table)")
                update_result = await update_specific_version_s3_uri(
                    report_id=report_id,
                    version_id=target_version_for_generation.id,
                    s3_uri_updates={"info_pdf": infographic_s3_uri, "info_pdf_generation_time": updated_uris["info_pdf_generation_time"]},
                    session=session
                )
                if not update_result.get('success'):
                    logger.error(f"Failed to update specific version infographic s3_uri: {update_result.get('error')} for report_id: {report_id}")
                logger.info(f"Generated infographic for specific old version {current_version} - cards already linked from version snapshot")
            else:
                # ========================================
                # FLOW B: Generating for CURRENT/ACTIVE version
                # Update both Report table and active ReportVersion
                # This covers:
                #   - No report_version_id provided (use_specific_version=False)
                #   - report_version_id provided but it's the active version
                # ========================================
                logger.info(f"Updating infographic for active version {current_version}, preserving other files")
                update_result = await update_report(
                    report_id=report_id,
                    update_data={"s3_uri": updated_uris},
                    session=session
                )
                
                if not update_result.get('success'):
                    logger.error(f"Failed to update s3_uri: {update_result.get('error')}")
                
                # Finalize version by linking current cards (first output locks the cards)
                # For version 1 (first time generation): Call finalize_report_version to populate report_version_cards
                # For version 2+: Already populated by create_new_report_version, skip finalization
                if current_version == 1:
                    logger.info(f"Finalizing version 1 for infographic generation, report_id: {report_id}")
                    finalize_result = await finalize_report_version(
                        session=session,
                        report_id=report_id
                    )
                    if finalize_result.get('success'):
                        cards_linked = finalize_result.get('cards_linked', 0)
                        already_finalized = finalize_result.get('already_finalized', False)
                        if already_finalized:
                            logger.info(f"Version 1 already finalized for infographic, report_id: {report_id}")
                        else:
                            logger.info(f"Finalized version 1 with {cards_linked} cards for infographic, report_id: {report_id}")
                    else:
                        logger.warning(f"Failed to finalize version 1 for infographic: {finalize_result.get('error')}")
                
                # Update status to OUTPUT_GENERATED for active version generation
                await update_report_status_by_chat_or_report_id(
                    report_id=report_id,
                    status=ReportStatus.OUTPUT_GENERATED.value,
                    session=session
                )
                logger.info(f"Set report status to OUTPUT_GENERATED for infographic generation, report_id: {report_id}")
        
        # Get version ID for response
        final_version_id = None
        if use_specific_version and target_version_for_generation:
            final_version_id = target_version_for_generation.id
        else:
            async with async_session_scope() as session:
                version_stmt = select(ReportVersion).where(
                    ReportVersion.report_id == report_id,
                    ReportVersion.version == current_version
                )
                version_result = await session.execute(version_stmt)
                version_obj = version_result.scalar_one_or_none()
                if version_obj:
                    final_version_id = version_obj.id
        
        return GenerateInfographicResponse(
            success=True,
            message="Infographic has been generated successfully.",
            report_version_id=final_version_id,
            version=current_version,
            info_pdf_s3_uri=infographic_s3_uri,
            info_pdf_generation_time=updated_uris.get("info_pdf_generation_time"),
            report_id=report_id,
            modified=True,
            already_exists=False  # Newly generated, not pre-existing
        )
    
    except Exception as e:
        logger.error(f"Error generating infographic: {e}")
        # Update report status to ERROR_GENERATION_REPORT on error
        try:
            async with async_session_scope() as session:
                await update_report_status_by_chat_or_report_id(
                    report_id=report_id,
                    status=ReportStatus.ERROR_GENERATION_REPORT.value,
                    session=session
                )
                logger.info(f"Set report status to ERROR_GENERATION_REPORT for infographic generation, report_id: {report_id}")
        except Exception as update_error:
            logger.error(f"Failed to update report status to ERROR_GENERATION_REPORT: {update_error}")
        
        return JSONResponse(
            status_code=500,
            content={"success": False, "error": "I ran into a problem while creating your infographic. Please try generating it again."}
        )


@router.post("/refine-card", response_model=RefineOrDeleteResponse, status_code=200, responses={404: {"model": ErrorResponse}, 401: {"model": ErrorResponse}, 422: {"model": ErrorResponse}, 500: {"model": ErrorResponse}})
async def refine_content(data: RefineOrDeleteRequest, user_id: str = Depends(get_current_active_user)):
    """Refine a card from the report"""
    set_functionality(Functionality.REFINE)
    logger.info(f"Refine card request received for user_id: {user_id}")
    
    try:
        # Validate input data
        if not data.report_id:
            logger.error("Missing required field: report_id")
            return JSONResponse(
                status_code=422,
                content={"success": False, "error": "Missing required field: report_id"}
            )
        
        if not data.card:
            logger.error("Missing required field: card")
            return JSONResponse(
                status_code=422,
                content={"success": False, "error": "Missing required field: card"}
            )
        
        report_id = data.report_id
        fe_input_data = data.card
        card_id = fe_input_data.get('id')

        async with async_session_scope() as session:
            db_response = await get_latest_card_version(session=session, card_id=card_id)
            if not db_response.get('success', False):
                logger.error(f"Failed to get latest card version: {db_response.get('error')} for card_id: {card_id}")
                return JSONResponse(
                    status_code=500,
                    content={"success": False, "error": "Something went wrong while I was refining your content. Let's give it another try."}
                )
            current_card_version = db_response.get('version', None)
            
            # Check if card exists
            if current_card_version is None:
                logger.error(f"Card with ID {card_id} not found in the database")
                return JSONResponse(
                    status_code=404,
                    content={"success": False, "error": f"Card with ID {card_id} not found"}
                )
        
        async with async_session_scope() as session:
            db_response = await get_report_details(report_id=report_id, session=session)
            if not db_response.get('success', False) or not db_response.get('report'):
                if not db_response.get('report'):
                    logger.warning(f"Report not found: {report_id}")
                    return JSONResponse(
                        status_code=404,
                        content={"success": False, "error": "Report not found. Please check the report ID and try again."}
                    )
                logger.error(f"Failed to get report details: {db_response.get('error')} for report_id: {report_id}")
                return JSONResponse(
                    status_code=500,
                    content={"success": False, "error": "Something went wrong while I was refining your content. Let's give it another try."}
                )
            report_data = db_response.get('report', {})
            chat_id = report_data.get('chat_id')
            report_type = report_data.get('report_type') or "study"
            initial_markdown_s3_path = report_data.get('initial_markdown')
            report_file_id = report_data.get('file_id')
            logger.info(f"Report data fetched for refine-card | report_id={report_id} | file_id={report_file_id} | has_s3_path={bool(initial_markdown_s3_path)}")

        # Resolve the OpenAI file_id for the report's initial markdown so the refiner always has
        # full-report context. grep only supplies the user's uploaded reference documents.
        report_file_id = await ensure_report_file_id(
            report_id=report_id,
            file_id=report_file_id,
            file_s3_path=initial_markdown_s3_path,
            log_prefix="REFINE_CARD",
        )
        if not report_file_id:
            # Degrade rather than fail: the refinement still uses the current card content plus
            # any grep-backed reference documents.
            logger.error(
                f"Refining without full report file context | report_id={report_id} "
                f"| has_s3_path={bool(initial_markdown_s3_path)}"
            )
        
        # Check if user exists and has access to this report
        async with async_session_scope() as session:
            db_response = await check_user_by_id(user_id=user_id, session=session)
            if not db_response.get('success', False):
                logger.error(f"Failed to check user by id: {db_response.get('error')}")
                return JSONResponse(
                    status_code=500,
                    content={"success": False, "error": "Something went wrong while I was refining your content. Let's give it another try."}
                )
            if not db_response.get('exists', False):
                logger.error(f"User with ID {user_id} not found or token is invalid")
                return JSONResponse(
                    status_code=401,
                    content={"success": False, "error": "Unauthorized request"}
                )
        
        # Get user details for refine_card function
        async with async_session_scope() as session:
            user_details_response = await get_user_details(user_id=user_id, session=session)
            if not user_details_response.get('success', False):
                logger.error(f"Failed to get user details: {user_details_response.get('error')}")
                return JSONResponse(
                    status_code=500,
                    content={"success": False, "error": "Something went wrong while I was refining your content. Let's give it another try."}
                )
            
            user_name = user_details_response.get("user", {}).get('user_name', 'User')
            
            # Get user subscription plan
            # user_plan = await get_active_subscription(user_id=user_id, session=session)
            # logger.info(f"User {user_id} subscription plan for card refinement: {user_plan}")

            # ── Build grep_session from chat reference files ──
            grep_session = None
            try:
                uploaded_file_ids = await get_chat_uploaded_file_ids(chat_id, session)
                if uploaded_file_ids:
                    grep_session = await get_or_create_grep_session(uploaded_file_ids, session)
                    logger.info(
                        f"Built grep_session for refine-card: "
                        f"chat_id={chat_id}, docs={len(grep_session.labels) if grep_session else 0}"
                    )
                else:
                    logger.info(f"No reference files found for chat_id={chat_id}")
            except Exception as gs_err:
                logger.warning(
                    f"Could not build grep_session for chat_id={chat_id}, "
                    f"proceeding without reference files: {gs_err}",
                    exc_info=True,
                )

            db_response = await get_report_in_cards_format(report_id=report_id, session=session)
            if not db_response.get('success', False):
                logger.error(f"Failed to get report in cards format: {db_response.get('error')} for report_id: {report_id}")
                return JSONResponse(
                    status_code=500,
                    content={"success": False, "error": "Something went wrong while I was refining your content. Let's give it another try."}
                )
            cards_for_db = db_response.get('cards', [])
            if not cards_for_db:
                logger.error(f"No cards found for report_id: {report_id}")
                return JSONResponse(
                    status_code=404,
                    content={"success": False, "error": "No cards found for this report"}
                )
            
            # Fetch existing refinement history from DB
            refinement_history_response = await get_refinement_history(report_id=report_id, session=session)
            existing_refinement_history = refinement_history_response.get('refine_history', []) if refinement_history_response.get('success') else []
            logger.info(f"Fetched {len(existing_refinement_history)} existing refinement history entries for report_id: {report_id}")
            
            # Check if we have refinement instructions
            user_instruction = fe_input_data.get('user_instruction')
            subsection_instruction = None if user_instruction else fe_input_data.get('subsection', {}).get('user_instruction')

            if user_instruction or subsection_instruction:
                # Create the format expected by refine.py
                refine_data = {
                    "id": fe_input_data.get('id'),  # Use the id from the card data
                    "refine_or_delete_prompt": user_instruction if user_instruction else None
                }
                
                if subsection_instruction:
                    # Add subsection data only if section-level instruction is not present
                    refine_data["subsection"] = {
                        "id": fe_input_data.get('subsection', {}).get('id'),
                        "refine_or_delete_prompt": subsection_instruction
                    }
                
                logger.info(
                    f"Calling refine_card for report_id={report_id}, chat_id={chat_id}, card_id={fe_input_data.get('id')}: "
                    f"grep_session={'present' if grep_session else 'None'}, "
                    f"docs={len(grep_session.labels) if grep_session else 0}, "
                    f"report_file={'present' if report_file_id else 'None'}"
                )
                # Web-search analytics: collected inside the sync refine_card call
                # (in-memory only, no I/O) and scheduled for a non-blocking DB
                # write via asyncio.create_task below, after the threadpool call
                # returns. Never scheduled from sync code.
                analytics_collector: List[Dict] = []
                analytics_operation_id = str(uuid7())
                updated_cards, updated_card, new_table_map, updated_refinement_history = await run_in_threadpool(
                    refine_card,
                    cards_for_db=cards_for_db,
                    fe_json_for_refine=refine_data,
                    user_name=user_name,
                    chat_id=chat_id,
                    latest_version=current_card_version,
                    refinement_history=existing_refinement_history,
                    file_id=report_file_id,
                    grep_session=grep_session,
                    report_type=report_type,
                    user_id=user_id,
                    analytics_collector=analytics_collector,
                    analytics_operation_id=analytics_operation_id,
                )

                # Save the refined card to database
                if updated_card:
                    # Get subsection_id if this is a subsection refinement
                    subsection_id = None if user_instruction else fe_input_data.get('subsection', {}).get('id')
                    section_id = fe_input_data.get('id')  # business card_id (section_id)

                    db_response = await persist_refined_card(
                        session=session,
                        report_id=report_id,
                        updated_card=updated_card,
                        user_instruction=user_instruction or subsection_instruction,
                        refinement_type=RefinementType.REFINE_SECTION.value if user_instruction else RefinementType.REFINE_SUBSECTION.value,
                        table_id_markdown_map=new_table_map,
                        subsection_id=subsection_id,
                        updated_refinement_history=updated_refinement_history,
                        section_id=section_id,
                        thread_policy=THREAD_POLICY_RESET,
                    )

                    if not db_response.get('success', False):
                        logger.error(f"Failed to save refined card to database: {db_response.get('error')}")
                        return JSONResponse(
                            status_code=500,
                            content={"success": False, "error": "Something went wrong while I was refining your content. Let's give it another try."}
                        )

                    # Persist web-search analytics collected during refine_card, if any.
                    # Enrichment scans the final refined card content for URLs actually
                    # retained in output, then schedules each collected entry for a
                    # non-blocking DB write via asyncio.create_task. All steps are
                    # wrapped so analytics can never affect the refine response.
                    try:
                        if analytics_collector:
                            enrich_terminal_search_analytics(analytics_collector, updated_card)
                            section_name = None
                            if updated_card.get("section"):
                                section_name = updated_card["section"][0].get("name")
                            if not section_name and updated_card.get("sub_sections"):
                                section_name = updated_card["sub_sections"][0].get("name")
                            log_scheduled_analytics_batch(
                                "card_refinement",
                                analytics_collector,
                                operation_id=analytics_operation_id,
                                section_name=section_name,
                                card_id=fe_input_data.get('id'),
                            )
                            for analytics_entry in analytics_collector:
                                try:
                                    event = search_analytics_schedule_kwargs(analytics_entry)
                                    event.update(
                                        {
                                            "trigger_source": "card_refinement",
                                            "user_query": user_instruction or subsection_instruction,
                                            "user_id": user_id,
                                            "chat_id": chat_id,
                                            "report_id": report_id,
                                            "section_name": section_name,
                                            "card_id": fe_input_data.get('id'),
                                        }
                                    )
                                    asyncio.create_task(log_web_search_event(**event))
                                except Exception:
                                    logger.exception(
                                        "[card_refinement] Failed to schedule non-fatal "
                                        "search analytics"
                                    )
                    except Exception:
                        logger.exception(
                            "[card_refinement] Failed to enrich non-fatal terminal "
                            "search analytics"
                        )
                
                def _normalize_table_title_for_response(value):
                    if value is None:
                        return ""
                    if isinstance(value, (tuple, list)):
                        return _normalize_table_title_for_response(value[0] if value else "")
                    if isinstance(value, str):
                        return value
                    return str(value)

                # Keep refine API contract stable: table_title must be a string.
                for section in updated_card.get("section", []) or []:
                    for table in section.get("tables", []) or []:
                        table["table_title"] = _normalize_table_title_for_response(table.get("table_title", ""))

                for subsection in updated_card.get("sub_sections", []) or []:
                    for table in subsection.get("tables", []) or []:
                        table["table_title"] = _normalize_table_title_for_response(table.get("table_title", ""))

                await replace_visualization_uris_in_card(updated_card, redis_instance.redis_client)
                return JSONResponse(
                    status_code=200,
                    content={
                        "success": True,
                        "message": "Card refined successfully",
                        "refine_card": updated_card
                    }
                )
            else:
                logger.error(f"No user instruction found for card_id: {fe_input_data.get('id')}")
                return JSONResponse(
                    status_code=422,
                    content={"success": False, "error": "No user instruction found for card"}
                )
    
    except Exception as e:
        logger.error(f"Error in refine_card: {e}")
        return JSONResponse(
            status_code=500,
            content={"success": False, "error": "Something went wrong while I was refining your content. Let's give it another try."}
        )

@router.post("/revert-card", response_model=RevertCardResponse, status_code=200, responses={404: {"model": ErrorResponse}, 401: {"model": ErrorResponse}, 422: {"model": ErrorResponse}, 500: {"model": ErrorResponse}})
async def revert_card(data: RevertCardRequest, user_id: str = Depends(get_current_active_user)):
    """
    Revert a card to its immediate previous version (undo last refinement).
    
    User can only undo to immediate previous version (v3 → v2, not v3 → v1).
    Current version is marked as deleted and previous version is reactivated.
    """
    logger.info(f"Revert card request received for user_id: {user_id}, card_id: {data.card_id}")
    
    try:
        # Validate input data
        if not data.report_id:
            logger.error("Missing required field: report_id")
            return JSONResponse(
                status_code=422,
                content={"success": False, "error": "Missing required field: report_id"}
            )
        
        if not data.card_id:
            logger.error("Missing required field: card_id")
            return JSONResponse(
                status_code=422,
                content={"success": False, "error": "Missing required field: card_id"}
            )
        
        # Check if user exists and has access to this report
        async with async_session_scope() as session:
            db_response = await check_user_by_id(user_id=user_id, session=session)
            if not db_response.get('success', False):
                logger.error(f"Failed to check user by id: {db_response.get('error')}")
                return JSONResponse(
                    status_code=500,
                    content={"success": False, "error": "Something went wrong while reverting your card. Please try again."}
                )
            if not db_response.get('exists', False):
                logger.error(f"User with ID {user_id} not found or token is invalid")
                return JSONResponse(
                    status_code=401,
                    content={"success": False, "error": "Unauthorized request"}
                )
            
            # Revert the card to immediate previous version
            revert_response = await revert_card_to_version(
                session=session,
                report_id=data.report_id,
                card_id=data.card_id
            )
            
            if not revert_response.get('success'):
                logger.error(f"Failed to revert card: {revert_response.get('error')}")
                return JSONResponse(
                    status_code=400,
                    content={"success": False, "error": revert_response.get('error', 'Failed to revert card')}
                )
            
            # Update report status to REDO_ANALYSIS since card was reverted
            await update_report_status_if_needed(
                report_id=data.report_id,
                new_status=ReportStatus.REDO_ANALYSIS.value,
                session=session
            )
            logger.info(f"Set report status to REDO_ANALYSIS after reverting card {data.card_id}")

            reverted_card_data = revert_response.get('reverted_card')
            if reverted_card_data:
                await replace_visualization_uris_in_card(reverted_card_data, redis_instance.redis_client)

            return RevertCardResponse(
                success=True,
                message=revert_response.get('message'),
                reverted_to_version=revert_response.get('reverted_to_version'),
                card_id=revert_response.get('card_id'),
                primary_card_id=revert_response.get('primary_card_id'),
                deleted_versions=revert_response.get('deleted_versions'),
                reverted_card=reverted_card_data
            )
    
    except Exception as e:
        logger.error(f"Unexpected error in revert_card: {str(e)}", exc_info=True)
        return JSONResponse(
            status_code=500,
            content={"success": False, "error": "Something unexpected happened while reverting your card. Please try again."}
        )

@router.post("/delete-card", response_model=RefineOrDeleteResponse, status_code=200, responses={404: {"model": ErrorResponse}, 401: {"model": ErrorResponse}, 422: {"model": ErrorResponse}, 500: {"model": ErrorResponse}})
async def delete_card(data: RefineOrDeleteRequest, user_id: str = Depends(get_current_active_user)):
    """Delete a card or subsection from the report"""
    logger.info(f"Delete card request received for user_id: {user_id}")
    
    try:
        # Validate input data
        if not data.report_id:
            logger.error("Missing required field: report_id")
            return JSONResponse(
                status_code=422,
                content={"success": False, "error": "Missing required field: report_id"}
            )
        
        if not data.card:
            logger.error("Missing required field: card")
            return JSONResponse(
                status_code=422,
                content={"success": False, "error": "Missing required field: card"}
            )
        
        report_id = data.report_id
        fe_input_data = data.card
        
        # Check if user exists and has access to this report
        async with async_session_scope() as session:
            db_response = await check_user_by_id(user_id=user_id, session=session)
            if not db_response.get('success', False):
                logger.error(f"Failed to check user by id: {db_response.get('error')}")
                return JSONResponse(
                    status_code=500,
                    content={"success": False, "error": "Something unexpected went wrong while deleting that. Please try it again."}
                )
            if not db_response.get('exists', False):
                logger.error(f"User with ID {user_id} not found or token is invalid")
                return JSONResponse(
                    status_code=401,
                    content={"success": False, "error": "Unauthorized request"}
                )
        
        async with async_session_scope() as session:
            db_response = await get_report_in_cards_format(report_id=report_id, session=session)
            if not db_response.get('success', False):
                logger.error(f"Failed to get report in cards format: {db_response.get('error')} for report_id: {report_id}")
                return JSONResponse(
                    status_code=500,
                    content={"success": False, "error": "Something unexpected went wrong while deleting that. Please try it again."}
                )
            cards_for_db = db_response.get('cards', [])
            if not cards_for_db:
                logger.error(f"No cards found for report_id: {report_id}")
                return JSONResponse(
                    status_code=404,
                    content={"success": False, "error": "I can't find the content you're trying to delete. It might have been removed already."}
                )
            
            # Check if we have deletion request
            section_id = fe_input_data.get('id')
            subsection_id = fe_input_data.get('subsection')
            
        updated_toc = await run_in_threadpool(extract_toc_after_delete, cards_for_db, fe_input_data)
   
        async with async_session_scope() as session:
            db_response = await delete_card_or_subsection(session=session, card_id=section_id, subsection_id=subsection_id, toc=updated_toc)
            if not db_response.get('success', False):
                logger.error(f"Failed to delete card or subsection: {db_response.get('error')} for report_id: {report_id}")
                return JSONResponse(
                    status_code=500,
                    content={"success": False, "error": "Something unexpected went wrong while deleting that. Please try it again."}
                )
            
            # Set status to REDO_ANALYSIS after deleting a card
            await update_report_status_if_needed(
                report_id=report_id,
                new_status=ReportStatus.REDO_ANALYSIS.value,
                session=session
            )
            logger.info(f"Set report status to REDO_ANALYSIS after card deletion for report_id: {report_id}")
        
        return JSONResponse(
            status_code=200,
            content={"success": True, "message": "Card or subsection deleted successfully"}
        )

            
    except Exception as e:
        logger.error(f"Error in delete_card: {e}")
        return JSONResponse(
            status_code=500,
            content={"success": False, "error": "Something unexpected went wrong while deleting that. Please try it again."}
        )

@router.post("/refine-visualization", response_model=RefineVisualizationResponse, status_code=200, responses={404: {"model": ErrorResponse}, 401: {"model": ErrorResponse}, 422: {"model": ErrorResponse}, 500: {"model": ErrorResponse}})
async def refine_visualization(data: RefineVisualizationRequest, user_id: str = Depends(get_current_active_user)):
    """Refine visualization for a section or subsection in a report"""
    set_functionality(Functionality.REFINE_VISUALIZATION)
    logger.info(f"Refine visualization request received for user_id: {user_id} and report_id: {data.report_id}")
    
    try:
        # Validate input data
        if not data.report_id:
            logger.error("Missing required field: report_id")
            return JSONResponse(
                status_code=422,
                content={"success": False, "error": "Missing required field: report_id"}
            )
        
        if not data.card:
            logger.error("Missing required field: card")
            return JSONResponse(
                status_code=422,
                content={"success": False, "error": "Missing required field: card"}
            )
        
        if not data.card.get('id'):
            logger.error("Missing required field: card.id")
            return JSONResponse(
                status_code=422,
                content={"success": False, "error": "Missing required field: card.id"}
            )
        
        report_id = data.report_id
        card_id = data.card.get('id')
        table_id = data.card.get('table_id')
        user_instruction = data.card.get('user_instruction')
        subsection_data = data.card.get('subsection')
        chat_id = data.card.get('chat_id')

        
        # Check if user exists and has access to this report
        async with async_session_scope() as session:
            db_response = await check_user_by_id(user_id=user_id, session=session)
            if not db_response.get('success', False):
                logger.error(f"Failed to check user by id: {db_response.get('error')}")
                return JSONResponse(
                    status_code=500,
                    content={"success": False, "error": "Something went wrong while refining your visualization. Please try again."}
                )
            if not db_response.get('exists', False):
                logger.error(f"User with ID {user_id} not found or token is invalid")
                return JSONResponse(
                    status_code=401,
                    content={"success": False, "error": "Unauthorized request"}
                )
            
            user_name = db_response.get('user_name')
            
            # Get user subscription plan
            # user_plan = await get_active_subscription(user_id=user_id, session=session)
            # logger.info(f"User {user_id} subscription plan for visualization refinement: {user_plan}")
            
            # Check if the card exists and belongs to the specified report
            card_stmt = select(Card).where(
                Card.card_id == card_id,
                Card.report_id == report_id,
                Card.is_active
            )
            card_result = await session.execute(card_stmt)
            existing_card = card_result.scalar_one_or_none()
            
            if not existing_card:
                logger.error(f"Card with ID {card_id} not found for report_id: {report_id}")
                return JSONResponse(
                    status_code=404,
                    content={"success": False, "error": "I can't find the visualization you're trying to refine. It might have been deleted already."}
                )
            
            parent_card_id = existing_card.id
            
            # Get table markdown content
            table_markdown = ""
            subsection_id = None
            
            # Determine if this is a section or subsection visualization
            if subsection_data:
                subsection_id = subsection_data.get('id')
                table_id = subsection_data.get('table_id')
                user_instruction = subsection_data.get('user_instruction')
                
                if not subsection_id:
                    logger.error("Missing subsection ID for subsection visualization")
                    return JSONResponse(
                        status_code=422,
                        content={"success": False, "error": "Missing subsection ID"}
                    )
                
                # Find the subsection and its table
                if existing_card.sub_sections:
                    for subsection in existing_card.sub_sections:
                        if isinstance(subsection, dict) and subsection.get('id') == subsection_id:
                            # Find table markdown in subsection
                            for table in subsection.get('tables', []):
                                if table.get('table_id') == table_id:
                                    # Get table markdown from database using report_id + table_id (stable identifiers)
                                    # Don't filter by parent_card_id as it may be stale after card refinement
                                    # Order by created_at DESC to get the latest entry if duplicates exist
                                    table_stmt = select(Table).where(
                                        Table.table_id == table_id,
                                        Table.report_id == report_id
                                    ).order_by(Table.created_at.desc()).limit(1)
                                    table_result = await session.execute(table_stmt)
                                    table_obj = table_result.scalar_one_or_none()
                                    
                                    if table_obj:
                                        # Self-healing: update stale parent_card_id if needed
                                        if table_obj.parent_card_id != parent_card_id:
                                            logger.info(f"Updating table {table_id} parent_card_id from {table_obj.parent_card_id} to {parent_card_id} (card was refined)")
                                            table_obj.parent_card_id = parent_card_id
                                            await session.flush()
                                        table_markdown = table_obj.table_markdown
                                    break
                            break
                    
                    if not table_markdown:
                        logger.error(f"Table with ID {table_id} not found in subsection {subsection_id}")
                        return JSONResponse(
                            status_code=404,
                            content={"success": False, "error": "I can't find the visualization you're trying to refine. It might have been deleted already."}
                        )
            else:
                # Section visualization
                # Find the table in the section
                if existing_card.content and isinstance(existing_card.content, dict):
                    for table in existing_card.content.get('tables', []):
                        if table.get('table_id') == table_id:
                            # Get table markdown from database using report_id + table_id (stable identifiers)
                            # Don't filter by parent_card_id as it may be stale after card refinement
                            # Order by created_at DESC to get the latest entry if duplicates exist
                            table_stmt = select(Table).where(
                                Table.table_id == table_id,
                                Table.report_id == report_id
                            ).order_by(Table.created_at.desc()).limit(1)
                            table_result = await session.execute(table_stmt)
                            table_obj = table_result.scalar_one_or_none()
                            
                            if table_obj:
                                # Self-healing: update stale parent_card_id if needed
                                if table_obj.parent_card_id != parent_card_id:
                                    logger.info(f"Updating table {table_id} parent_card_id from {table_obj.parent_card_id} to {parent_card_id} (card was refined)")
                                    table_obj.parent_card_id = parent_card_id
                                    await session.flush()
                                table_markdown = table_obj.table_markdown
                            break
                
                if not table_markdown:
                    logger.error(f"Table with ID {table_id} not found in section {card_id}")
                    return JSONResponse(
                        status_code=404,
                        content={"success": False, "error": "I can't find the visualization you're trying to refine. It might have been deleted already."}
                    )
            
            # Call viz_refine to generate new visualization
            input_json = {
                "user_ins": user_instruction,
                "table": table_markdown,
                "viz": table_obj.base64_s3_uri
            }
            
            new_viz_s3_path = await run_in_threadpool(refine_viz, input_json=input_json, user_name=user_name, chat_id=chat_id)
            
            if not new_viz_s3_path:
                logger.error(f"Failed to generate visualization for table {table_id}")
                return JSONResponse(
                    status_code=500,
                    content={"success": False, "error": "Something went wrong while refining your visualization. Please try again."}
                )
            
            # Update the base64_s3_uri field in the Table record
            table_stmt = select(Table).where(
                Table.table_id == table_id,
                Table.report_id == report_id,
                Table.parent_card_id == parent_card_id
            )
            table_result = await session.execute(table_stmt)
            table_obj = table_result.scalar_one_or_none()
            
            if table_obj:
                table_obj.base64_s3_uri = new_viz_s3_path
                logger.info(f"Updated base64_s3_uri for table {table_id} to {new_viz_s3_path}")
            
            # Update the card with new visualization S3 path
            if subsection_data:
                # Update subsection visualization
                for subsection in existing_card.sub_sections:
                    if isinstance(subsection, dict) and subsection.get('id') == subsection_id:
                        for table in subsection.get('tables', []):
                            if table.get('table_id') == table_id:
                                table['visualization'] = new_viz_s3_path
                                break
                        break
                
                # Mark the JSONB field as modified
                flag_modified(existing_card, 'sub_sections')
                logger.info(f"Updated visualization for subsection {subsection_id} in card {card_id}")
            else:
                # Update section visualization
                if existing_card.content and isinstance(existing_card.content, dict):
                    for table in existing_card.content.get('tables', []):
                        if table.get('table_id') == table_id:
                            table['visualization'] = new_viz_s3_path
                            break
                    
                    # Mark the JSONB field as modified
                    flag_modified(existing_card, 'content')
                    logger.info(f"Updated visualization for section {card_id}")
            
            existing_card.updated_at = datetime.now(timezone.utc)
            logger.info(f"Updated card {card_id} timestamp to mark visualization refinement")
            
            # Set status to REDO_ANALYSIS after refining visualization
            await update_report_status_if_needed(
                report_id=data.report_id,
                new_status=ReportStatus.REDO_ANALYSIS.value,
                session=session
            )
            logger.info(f"Set report status to REDO_ANALYSIS after refining visualization for report_id: {data.report_id}")
            
            # Commit the changes
            await session.commit()
            
            refined_viz_url = await get_or_create_presigned_url(
                new_viz_s3_path, redis_instance.redis_client
            ) or new_viz_s3_path

            return JSONResponse(
                status_code=200,
                content={
                    "success": True,
                    "message": "Visualization updated successfully",
                    "refined_viz": refined_viz_url
                }
            )
    
    except Exception as e:
        logger.error(f"Error in refine_visualization: {e}")
        return JSONResponse(
            status_code=500,
            content={"success": False, "error": "Something went wrong while refining your visualization. Please try again."}
        )

@router.post("/delete-visualization", response_model=RefineOrDeleteResponse, status_code=200, responses={404: {"model": ErrorResponse}, 401: {"model": ErrorResponse}, 422: {"model": ErrorResponse}, 500: {"model": ErrorResponse}})
async def delete_visualization_endpoint(data: RefineOrDeleteRequest, user_id: str = Depends(get_current_active_user)):
    """Delete visualization for a table in a report"""
    logger.info(f"Delete visualization request received for user_id: {user_id}")
    
    try:
        # Validate input data
        if not data.report_id:
            logger.error("Missing required field: report_id")
            return JSONResponse(
                status_code=422,
                content={"success": False, "error": "Missing required field: report_id"}
            )
        
        if not data.card:
            logger.error("Missing required field: card")
            return JSONResponse(
                status_code=422,
                content={"success": False, "error": "Missing required field: card"}
            )
        
        if not data.card.get('id'):
            logger.error("Missing required field: card.id")
            return JSONResponse(
                status_code=422,
                content={"success": False, "error": "It looks like some required information is missing. Please check your request and try again."}
            )
        
        report_id = data.report_id
        card_id = data.card.get('id')
        subsection_data = data.card.get('subsection')
        
        # Handle different input formats for section vs subsection table deletion
        if subsection_data:
            # Subsection table deletion format
            subsection_id = subsection_data.get('id')
            table_id = subsection_data.get('table_id')
            
            if not subsection_id:
                logger.error("Missing required field: card.subsection.id")
                return JSONResponse(
                    status_code=422,
                    content={"success": False, "error": "It looks like some required information is missing. Please check your request and try again."}
                )
            
            if not table_id:
                logger.error("Missing required field: card.subsection.table_id")
                return JSONResponse(
                    status_code=422,
                    content={"success": False, "error": "It looks like some required information is missing. Please check your request and try again."}
                )
        else:
            # Section table deletion format
            table_id = data.card.get('table_id')
            subsection_id = None
            
            if not table_id:
                logger.error("Missing required field: card.table_id")
                return JSONResponse(
                    status_code=422,
                    content={"success": False, "error": "Missing required field: card.table_id"}
                )
        
        # Check if user exists and has access to this report
        async with async_session_scope() as session:
            db_response = await check_user_by_id(user_id=user_id, session=session)
            if not db_response.get('success', False):
                logger.error(f"Failed to check user by id: {db_response.get('error')}")
                return JSONResponse(
                    status_code=500,
                    content={"success": False, "error": "Something went wrong while deleting your visualization. Please try again."}
                )
            if not db_response.get('exists', False):
                logger.error(f"User with ID {user_id} not found or token is invalid")
                return JSONResponse(
                    status_code=401,
                    content={"success": False, "error": "Unauthorized request"}
                )
            
            # Delete the visualization
            db_response = await delete_visualization(
                session=session,
                report_id=report_id,
                card_id=card_id,
                table_id=table_id,
                subsection_id=subsection_id
            )
            
            if not db_response.get('success', False):
                error_type = db_response.get('error_type', 'server_error')
                error_message = db_response.get('error', 'Something went wrong while deleting your visualization. Please try again.')
                
                # Determine appropriate status code based on error type
                if error_type == 'not_found':
                    status_code = 404
                    logger.warning(f"Resource not found: {error_message}")
                elif error_type == 'invalid_data':
                    status_code = 422
                    logger.warning(f"Invalid data: {error_message}")
                else:
                    status_code = 500
                    logger.error(f"Server error: {error_message}")
                
                return JSONResponse(
                    status_code=status_code,
                    content={"success": False, "error": error_message}
                )
            
            # Set status to REDO_ANALYSIS after deleting visualization
            await update_report_status_if_needed(
                report_id=report_id,
                new_status=ReportStatus.REDO_ANALYSIS.value,
                session=session
            )
            logger.info(f"Set report status to REDO_ANALYSIS after deleting visualization for report_id: {report_id}")
            
            return JSONResponse(
                status_code=200,
                content={
                    "success": True,
                    "message": "Visualization deleted successfully",
                    "card_id": card_id,
                    "table_id": table_id,
                    "subsection_id": subsection_id
                }
            )
            
    except Exception as e:
        logger.error(f"Error in delete_visualization_endpoint: {e}")
        return JSONResponse(
            status_code=500,
            content={"success": False, "error": "Something went wrong while deleting your visualization. Please try again."}
        )

def extract_table_markdown_from_card_content(content_dict: dict, sub_sections: list, target_table_id: str) -> Optional[str]:
    """
    Extract table markdown from card's content or sub_sections for a specific table_id.
    
    Args:
        content_dict: The card's content dictionary
        sub_sections: The card's sub_sections list
        target_table_id: The table_id to find
        
    Returns:
        The markdown table string if found, None otherwise
    """
    from src.core.cards.card_utils import extract_markdown_tables
    
    # Check section-level content
    if isinstance(content_dict, dict):
        section_content = content_dict.get('content', '')
        
        # Handle nested content structure
        if isinstance(section_content, dict):
            section_content = section_content.get('content', '')
        
        if isinstance(section_content, str) and section_content:
            # Check if this section has the target table_id
            section_tables = content_dict.get('tables', [])
            for idx, table_ref in enumerate(section_tables):
                if table_ref.get('table_id') == target_table_id:
                    # Extract all tables from content
                    extracted_tables = extract_markdown_tables(section_content)
                    if extracted_tables:
                        # Return table at matching position
                        if idx < len(extracted_tables):
                            return extracted_tables[idx]
                        else:
                            # Fallback if position mismatch
                            logger.warning(f"Position mismatch for table {target_table_id}: idx={idx}, extracted={len(extracted_tables)}")
                            return extracted_tables[0] if len(extracted_tables) > 0 else None
                    return None
    
    # Check subsection-level content
    if sub_sections:
        for subsection in sub_sections:
            if isinstance(subsection, dict):
                sub_content = subsection.get('content', '')
                
                # Handle nested content structure
                if isinstance(sub_content, dict):
                    sub_content = sub_content.get('content', '')
                
                if isinstance(sub_content, str) and sub_content:
                    # Check if this subsection has the target table_id
                    sub_tables = subsection.get('tables', [])
                    for idx, table_ref in enumerate(sub_tables):
                        if table_ref.get('table_id') == target_table_id:
                            # Extract all tables from subsection content
                            extracted_tables = extract_markdown_tables(sub_content)
                            if extracted_tables:
                                # Return table at matching position
                                if idx < len(extracted_tables):
                                    return extracted_tables[idx]
                                else:
                                    # Fallback if position mismatch
                                    logger.warning(f"Position mismatch for table {target_table_id} in subsection: idx={idx}, extracted={len(extracted_tables)}")
                                    return extracted_tables[0] if len(extracted_tables) > 0 else None
                            return None
    
    return None


@router.post("/edit-card-content", response_model=RefineOrDeleteResponse, status_code=200, responses={404: {"model": ErrorResponse}, 401: {"model": ErrorResponse}, 422: {"model": ErrorResponse}, 500: {"model": ErrorResponse}})
async def replace_card_content(data: RefineOrDeleteRequest, user_id: str = Depends(get_current_active_user)):
    """Replace section or subsection content for a particular active card"""
    logger.info(f"Replace card content request received for user_id: {user_id}")
    
    try:
        # Validate input data
        if not data.report_id:
            logger.error("Missing required field: report_id")
            return JSONResponse(
                status_code=422,
                content={"success": False, "error": "Please provide all required information for content editing."}
            )
        
        if not data.card:
            logger.error("Missing required field: card")
            return JSONResponse(
                status_code=422,
                content={"success": False, "error": "Please provide all required information for content editing."}
            )
        
        if not data.card.get('id'):
            logger.error("Missing required field: card.id")
            return JSONResponse(
                status_code=422,
                content={"success": False, "error": "Please provide all required information for content editing."}
            )
        
        
        report_id = data.report_id
        card_id = data.card.get('id')
        subsection_data = data.card.get('subsection')
        
        # Check if user exists and has access to this report
        async with async_session_scope() as session:
            db_response = await check_user_by_id(user_id=user_id, session=session)
            if not db_response.get('success', False):
                logger.error(f"Failed to check user by id: {db_response.get('error')}")
                return JSONResponse(
                    status_code=500,
                    content={"success": False, "error": "Something went wrong while editing your content. Please try again."}
                )
            if not db_response.get('exists', False):
                logger.error(f"User with ID {user_id} not found or token is invalid")
                return JSONResponse(
                    status_code=401,
                    content={"success": False, "error": "Unauthorized request"}
                )
            
            # Get all versions of the card to calculate next version number
            all_cards_stmt = select(Card).where(
                Card.card_id == card_id,
                Card.report_id == report_id
            ).order_by(Card.version.desc())
            all_cards_result = await session.execute(all_cards_stmt)
            all_cards = all_cards_result.scalars().all()
            
            if not all_cards:
                logger.error(f"Card with ID {card_id} not found for report_id: {report_id}")
                return JSONResponse(
                    status_code=404,
                    content={"success": False, "error": "The content you're trying to edit doesn't exist or may have been deleted."}
                )
            
            # Get the current active card
            existing_card = next((c for c in all_cards if c.is_active), None)
            if not existing_card:
                logger.error(f"No active card with ID {card_id} found for report_id: {report_id}")
                return JSONResponse(
                    status_code=404,
                    content={"success": False, "error": "The content you're trying to edit doesn't exist or may have been deleted."}
                )
            
            # Calculate next version number
            new_version = max(card.version for card in all_cards) + 1
            
            # Determine if this is a section or subsection edit
            is_subsection_edit = subsection_data is not None and subsection_data.get('id')
            
            # Initialize subsection_id (will be set if this is a subsection edit)
            subsection_id = None
            
            # Prepare new content by copying existing card data
            import copy
            new_content = copy.deepcopy(existing_card.content) if existing_card.content else {}
            new_sub_sections = copy.deepcopy(existing_card.sub_sections) if existing_card.sub_sections else []
            
            if is_subsection_edit:
                new_subsection_content = subsection_data.get('content')
                subsection_id = subsection_data.get('id')
                
                if not subsection_id:
                    logger.error("Missing subsection ID for subsection edit")
                    return JSONResponse(
                        status_code=422,
                        content={"success": False, "error": "The content you're trying to edit doesn't exist or may have been deleted."}
                    )
                
                # Update subsection content in the copied sub_sections
                subsection_updated = False
                for subsection in new_sub_sections:
                    if isinstance(subsection, dict) and subsection.get('id') == subsection_id:
                        subsection['content'] = new_subsection_content
                        subsection_updated = True
                        break
                
                if not subsection_updated:
                    logger.error(f"Subsection with ID {subsection_id} not found in card {card_id}")
                    return JSONResponse(
                        status_code=404,
                        content={"success": False, "error": "Subsection not found"}
                    )
                
                logger.info(f"Prepared updated subsection content for card {card_id}, subsection {subsection_id}")
                
            else:
                # Editing section content
                new_card_content = data.card.get('content')
                
                # Update the content in the copy
                if isinstance(new_content, dict):
                    if 'content' in new_content:
                        new_content['content'] = new_card_content
                    else:
                        new_content = {
                            "name": new_content.get('name', 'section'),
                            "content": new_card_content,
                            "tables": new_content.get('tables', [{"visualization": "", "table_id": "", "table_title": ""}]),
                            "id": new_content.get('id', card_id)  # Preserve original ID (same as card_id)
                        }
                else:
                    # Legacy string content - convert to new structure
                    new_content = {
                        "name": "section",
                        "content": new_card_content,
                        "tables": [{"visualization": "", "table_id": "", "table_title": ""}],
                        "id": card_id  # Use card_id, not a new UUID
                    }
                
                logger.info(f"Prepared updated section content for card {card_id}")
            
            # Generate summary for the new content
            try:
                logger.info(f"Generating summary for edited card {card_id}")
                 
                # Extract full section content for summary generation
                section_content = ""
                 
                # Get section name and content from NEW content
                if new_content and isinstance(new_content, dict):
                    section_content += new_content.get('name', '') + "\n\n"
                     
                    # Get section-level content
                    content_data = new_content.get('content', '')
                    if isinstance(content_data, dict):
                        section_content += content_data.get('content', '')
                    else:
                        section_content += str(content_data)
                    section_content += "\n\n"
                 
                # Get all subsection content from NEW sub_sections
                if new_sub_sections:
                    for subsection in new_sub_sections:
                        if isinstance(subsection, dict):
                            section_content += subsection.get('name', '') + "\n"
                            subsection_content = subsection.get('content', '')
                            if isinstance(subsection_content, dict):
                                section_content += subsection_content.get('content', '')
                            else:
                                section_content += str(subsection_content)
                            section_content += "\n\n"
                 
                # Generate new summary using generate_section_summary
                new_summary = await run_in_threadpool(generate_section_summary, section_content)
                logger.info(f"Successfully generated summary for edited card {card_id}")
                 
            except Exception as e:
                logger.error(f"Error generating summary for card {card_id}: {str(e)}")
                new_summary = existing_card.summary  # Fall back to old summary if generation fails
            
            # Deactivate ALL previous versions of this card (not just one)
            logger.info(f"Deactivating {len(all_cards)} previous versions of card_id: {card_id}")
            for card in all_cards:
                card.is_active = False
            
            # Create new card version with updated content
            new_card_id = str(uuid7())
            new_card = Card(
                id=new_card_id,
                card_id=card_id,  # Same business card_id
                report_id=report_id,
                title=existing_card.title,
                sequence=existing_card.sequence,
                content=new_content,
                sub_sections=new_sub_sections,
                citations=existing_card.citations if existing_card.citations else {},
                created_at=datetime.now(timezone.utc),
                summary=new_summary,
                type=existing_card.type,
                version=new_version,
                is_active=True,
                is_deleted=False,
                changed_since_es=True,  # Mark as changed to trigger ES regeneration
                last_es_version_used=None  # Not yet used in any ES
            )
            
            session.add(new_card)
            await session.flush()
            
            # Copy all table records from old card to new card version
            # Extract updated table markdown from new content if available
            logger.info(f"Copying table records from old card {existing_card.id} to new card {new_card_id}")
            
            old_tables_stmt = select(Table).where(Table.parent_card_id == existing_card.id)
            old_tables_result = await session.execute(old_tables_stmt)
            old_tables = old_tables_result.scalars().all()
            
            tables_created = 0
            tables_updated = 0
            
            for old_table in old_tables:
                # Try to extract updated table markdown from new content
                extracted_markdown = extract_table_markdown_from_card_content(
                    new_content, 
                    new_sub_sections, 
                    old_table.table_id
                )
                
                # Use extracted markdown if found, otherwise fallback to old markdown
                final_markdown = extracted_markdown if extracted_markdown else old_table.table_markdown
                
                if extracted_markdown and extracted_markdown != old_table.table_markdown:
                    logger.info(f"Table {old_table.table_id} content was updated")
                    tables_updated += 1
                
                new_table = Table(
                    id=str(uuid7()),
                    report_id=old_table.report_id,
                    parent_card_id=new_card_id,
                    table_id=old_table.table_id,
                    table_title=old_table.table_title,
                    table_markdown=final_markdown,
                    base64_s3_uri=old_table.base64_s3_uri
                )
                session.add(new_table)
                tables_created += 1
            
            logger.info(f"Successfully created {tables_created} table records for new card version ({tables_updated} with updated content)")
            
            # Create card version record for tracking
            from src.db.async_db_functions import insert_card_version
            version_result = await insert_card_version(
                session=session,
                card_id=new_card_id,  # Primary key (cards.id)
                section_id=card_id,   # Business card_id (cards.card_id)
                user_instruction="user edited manually",
                refinement_type=RefinementType.EDIT_SECTION.value,
                subsection_id=subsection_id if is_subsection_edit else None
            )
            
            if not version_result.get('success'):
                logger.warning(f"Failed to create card version record: {version_result.get('error')}")
            
            # Set status to REDO_ANALYSIS after editing card content
            await update_report_status_if_needed(
                report_id=report_id,
                new_status=ReportStatus.REDO_ANALYSIS.value,
                session=session
            )
            logger.info(f"Set report status to REDO_ANALYSIS after editing card content for report_id: {report_id}")
            
            # Commit the changes
            await session.commit()

            logger.info(f"Successfully created new version {new_version} for card {card_id} (primary key: {new_card_id})")
            
            # NOTE: We intentionally do NOT touch ask_caspr_chats on edit.
            # The existing chat entry keeps its original card_version, and the
            # historical query (card_version <= target) naturally includes it
            # for both the old and new report versions.  Bumping card_version
            # here would break historical lookups for versions between a prior
            # refine and this edit.  See edge_case_3_edit_after_refine.md.
            
            return JSONResponse(
                status_code=200,
                content={
                    "success": True,
                    "message": f"{'Subsection' if is_subsection_edit else 'Section'} content updated successfully",
                    "card_id": card_id,
                    "version": new_version,
                    "primary_card_id": new_card_id
                }
            )
    
    except Exception as e:
        logger.error(f"Error in replace_card_content: {e}")
        return JSONResponse(
            status_code=500,
            content={"success": False, "error": "Something went wrong while editing your content. Please try again."}
        )


# ========================================================================================================
# Regenerate Executive Summary Endpoint
# ========================================================================================================
@router.post("/regenerate-executive-summary", 
             response_model=RegenerateESResponse, 
             status_code=200,
             responses={404: {"model": ErrorResponse}, 401: {"model": ErrorResponse}, 422: {"model": ErrorResponse}, 500: {"model": ErrorResponse}})
async def regenerate_executive_summary(
    data: RegenerateESRequest, 
    user_id: str = Depends(get_current_active_user)
):
    """
    Regenerate executive summary based on refined cards.
    Only creates a new ES version if cards have been modified since last ES generation.
    """
    set_functionality(Functionality.EXECUTIVE_SUMMARY)
    try:
        logger.info(f"Regenerate ES request for report_id: {data.report_id}, user_id: {user_id}")
        
        async with async_session_scope() as session:
            # 1. Validate user token first
            db_response = await check_user_by_id(user_id=user_id, session=session)
            if not db_response.get('success', False):
                logger.error(f"Failed to check user by id: {db_response.get('error')}")
                return JSONResponse(
                    status_code=500,
                    content={"success": False, "error": "Something went wrong while regenerating the executive summary. Please try again."}
                )
            if not db_response.get('exists', False):
                logger.error(f"User with ID {user_id} not found or token is invalid")
                return JSONResponse(
                    status_code=401,
                    content={"success": False, "error": "Unauthorized request"}
                )
            
            # 2. Get report and verify ownership
            ownership = await verify_report_ownership(report_id=data.report_id, user_id=user_id, session=session)
            if not ownership["authorized"]:
                return JSONResponse(
                    status_code=ownership["status_code"],
                    content={"success": False, "error": ownership["error"]}
                )
            
            # 3. Get all relevant cards in one query:
            # - Active section cards (is_active=True, is_deleted=False)
            # - Deleted section cards that changed since last ES (is_deleted=True, changed_since_es=True)
            # - Active ES card (is_active=True, is_deleted=False)
            all_cards_result = await session.execute(
                select(Card).where(
                    and_(
                        Card.report_id == data.report_id,
                        or_(
                            # Active sections
                            and_(
                                Card.type == 'section',
                                Card.is_active == True,
                                Card.is_deleted == False
                            ),
                            # Deleted sections that changed since last ES
                            and_(
                                Card.type == 'section',
                                Card.is_deleted == True,
                                Card.changed_since_es == True
                            ),
                            # Active ES card
                            and_(
                                Card.type == 'es',
                                Card.is_active == True,
                                Card.is_deleted == False
                            )
                        )
                    )
                ).order_by(Card.sequence)
            )
            all_cards = all_cards_result.scalars().all()
            
            # Separate section cards (active + deleted with changes) and ES card
            section_cards = [card for card in all_cards if card.type == 'section']
            current_es_card = next((card for card in all_cards if card.type == 'es'), None)
            
            if not section_cards:
                return JSONResponse(
                    status_code=400,
                    content={"success": False, "error": "No sections found in this report."}
                )
            
            if not current_es_card:
                return JSONResponse(
                    status_code=404,
                    content={"success": False, "error": "Executive summary not found for this report."}
                )
            
            # 4. Check if any cards have changed since last ES
            any_changed = any(card.changed_since_es for card in section_cards)
            
            if not any_changed:
                return RegenerateESResponse(
                    success=False,
                    message="No changes have been made to any sections since the last executive summary was generated. Please refine some sections first to regenerate the summary."
                )
            
            # 5. Extract current ES content
            current_es_content = ""
            if current_es_card.content and isinstance(current_es_card.content, dict):
                content_data = current_es_card.content.get('content', '')
                current_es_content = content_data.get('content', '') if isinstance(content_data, dict) else str(content_data)
            
            current_es_version = current_es_card.version or 1
            
            # 6. Build cards_summary list for the update function
            cards_summary = []
            
            for card in section_cards:
                card_title = card.title or "Untitled Section"
                current_summary = card.summary or ""
                previous_summary = None
                is_deleted = card.is_deleted
                
                # If card changed, find the previous version used in ES
                if card.changed_since_es and card.card_id:
                    # Find the last version of this card that was used in ES
                    prev_card_result = await session.execute(
                        select(Card).where(
                            Card.card_id == card.card_id,
                            Card.report_id == data.report_id,
                            Card.type == 'section',
                            Card.last_es_version_used == current_es_version,
                            Card.is_deleted == False
                        ).order_by(Card.version.desc())
                    )
                    prev_card = prev_card_result.scalar_one_or_none()
                    
                    if prev_card:
                        previous_summary = prev_card.summary or ""
                
                cards_summary.append({
                    "card_title": card_title,
                    "current_summary": current_summary,
                    "previous_summary": previous_summary,
                    "is_deleted": is_deleted
                })
            
            # 7. Call update function
            logger.info(f"Calling update_executive_summary for report_id: {data.report_id}")            
            updated_es = await run_in_threadpool(
                update_executive_summary,
                cards=cards_summary,
                current_executive_summary=current_es_content,
                report_id=data.report_id
            )
            
            # Check if ES actually changed
            if updated_es == current_es_content:
                return RegenerateESResponse(
                    success=True,
                    message="Executive summary reviewed but no updates were necessary based on the changes made.",
                    new_es_version=current_es_version,
                    updated_summary=current_es_content
                )
            
            # 8. Create new ES version
            new_es_version = current_es_version + 1
            
            # Deactivate current ES
            current_es_card.is_active = False
            
            # Create new ES card
            new_es_card = Card(
                id=str(uuid7()),
                card_id=current_es_card.card_id,
                report_id=data.report_id,
                title="executive_summary",
                sequence=4,  # ES is always sequence 4
                content={
                    "name": "executive_summary",
                    "content": updated_es
                },
                sub_sections=[],
                citations={},
                summary="",
                type='es',
                version=new_es_version,
                is_active=True,
                is_deleted=False,
                changed_since_es=False,  # ES itself doesn't have this flag used meaningfully
                last_es_version_used=None
            )
            
            session.add(new_es_card)
            
            # 9. Update all section cards: reset flags and mark as used in new ES
            for card in section_cards:
                card.changed_since_es = False
                card.last_es_version_used = new_es_version
            
            # 10. Update report status
            await update_report_status_if_needed(
                report_id=data.report_id,
                new_status=ReportStatus.REDO_ANALYSIS.value,
                session=session
            )
            
            await session.commit()
            
            logger.info(f"Successfully regenerated ES for report_id: {data.report_id}, new version: {new_es_version}")
            
            return RegenerateESResponse(
                success=True,
                message=f"Executive summary successfully updated to version {new_es_version} based on your refined sections.",
                new_es_version=new_es_version,
                updated_summary=updated_es
            )
    
    except Exception as e:
        logger.error(f"Error regenerating executive summary: {str(e)}", exc_info=True)
        return JSONResponse(
            status_code=500,
            content={"success": False, "error": "An error occurred while regenerating the executive summary. Please try again."}
        )


# ========================================================================================================
# DEPRECATED: /regenerate-report endpoint
# ========================================================================================================
# This endpoint has been DEPRECATED and replaced by enhanced /generate-report functionality.
# The /generate-report endpoint now automatically detects if a new version is needed and creates it.
# 
# Migration Guide:
# - OLD: Frontend calls /regenerate-report, then /generate-report
# - NEW: Frontend only calls /generate-report (handles everything automatically)
#
# The logic from this endpoint has been extracted into create_new_version_internal() function
# and is now called internally by /generate-report when needed.
#
# Uncomment below if you need to temporarily re-enable for backward compatibility.
# ========================================================================================================

# @router.post(
#     "/regenerate-report",
#     response_model=RegenerateReportResponse,
#     status_code=200,
#     responses={
#         404: {"model": ErrorResponse},
#         401: {"model": ErrorResponse},
#         422: {"model": ErrorResponse},
#         500: {"model": ErrorResponse}
#     }
# )
# async def regenerate_report(
#     data: RegenerateReportRequest,
#     user_id: str = Depends(get_current_active_user)
# ):
#     """
#     DEPRECATED: Use /generate-report instead.
#     
#     Regenerate a report with modified cards - creates a new version.
#     
#     **🎯 AUTOMATIC DETECTION:** Backend automatically detects which cards were modified!
#     
#     This functionality is now part of /generate-report endpoint.
#     """
#     # Return deprecation notice
#     return JSONResponse(
#         status_code=410,  # 410 Gone - indicates resource is no longer available
#         content={
#             "success": False,
#             "error": "This endpoint has been deprecated. Please use /generate-report instead.",
#             "message": "The /generate-report endpoint now automatically handles version creation and regeneration."
#         }
#     )

# Original implementation commented out - kept for reference
# Authentication Required: Yes (JWT token)
#     logger.info(f"Regenerate report request received for user: {user_id}, report: {data.report_id}")
#     
#     try:
#         [... original code removed for brevity ...]
#     
#     except Exception as e:
#         logger.error(f"Error in regenerate_report: {str(e)}", exc_info=True)
#         return JSONResponse(
#             status_code=500,
#             content={
#                 "success": False,
#                 "error": "An error occurred while regenerating the report. Please try again."
#             }
#         )


@router.get(
    "/dashboard-stats",
    response_model=DashboardStatsResponse,
    status_code=200,
    responses={
        401: {"model": ErrorResponse},
        500: {"model": ErrorResponse}
    }
)
async def get_dashboard_stats_endpoint(
    user_id: str = Depends(get_current_active_user)
):
    """
    Get dashboard statistics for the current user.
    
    Returns counts of reports/chats in different states:
    - **drafts**: Number of draft chats (chats that exist in messages table but NOT in reports table)
    - **analysis_completed**: Number of reports with analysis completed status
    - **output_generated**: Number of reports with output generated status
    - **updated**: Number of updated reports (TODO: implement when update functionality is enabled)
    
    Authentication Required: Yes (JWT token)
    """
    logger.info(f"Dashboard stats request received for user: {user_id}")
    
    try:
        async with async_session_scope() as session:
            # Get dashboard statistics
            result = await get_dashboard_stats(user_id=user_id, session=session)
            
            if not result.get('success'):
                logger.error(f"Failed to get dashboard stats: {result.get('error')}")
                return JSONResponse(
                    status_code=500,
                    content={
                        "success": False,
                        "error": "Failed to retrieve dashboard statistics"
                    }
                )
            
            logger.info(f"Successfully retrieved dashboard stats for user {user_id}")
            return JSONResponse(
                status_code=200,
                content={
                    "drafts": result.get('drafts', 0),
                    "analysis_completed": result.get('analysis_completed', 0),
                    "output_generated": result.get('output_generated', 0),
                    "updates": result.get('updates', 0)  # TODO: implement when update functionality is enabled
                }
            )
    
    except Exception as e:
        logger.error(f"Error in get_dashboard_stats_endpoint: {e}", exc_info=True)
        return JSONResponse(
            status_code=500,
            content={"success": False, "error": "Internal server error"}
        )


@router.get(
    "/dashboard-info",
    response_model=DashboardInfoResponse,
    status_code=200,
    responses={
        401: {"model": ErrorResponse}
    }
)
async def get_dashboard_info_endpoint(
    user_id: str = Depends(get_current_active_user)
):
    """
    Get dashboard information including general facts and system info.
    
    Returns:
    - **general_facts**: List of helpful tips and facts about Caspr features
    - **caspr_info**: System information like live feeds data count
    
    **TODO:**
    - Replace static general_facts with dynamic content from database
    - Implement real live_feeds_data count from database
    - Add more system metrics as needed
    
    **Note:** Currently returns static dummy data for frontend development.
    
    Authentication Required: Yes (JWT token)
    """
    logger.info(f"Dashboard info request received for user: {user_id}")
    
    try:
        # TODO: Fetch this data from database instead of static values
        # For now, returning static data as requested
        dashboard_info = {
            "general_facts": [
                {
                    "title": "Caspr. can generate quick presentations on the reports you create",
                    "subtitle": "Generate presentation option is available to you as soon as a PDF report is generated. You even get a completely editable PPTX file to make changes on your own."
                }
            ],
            "caspr_info": [
                {
                    "live_feeds_data": 536831  # TODO: Get real count from database
                }
            ]
        }
        
        logger.info(f"Successfully retrieved dashboard info for user {user_id}")
        return JSONResponse(
            status_code=200,
            content=dashboard_info
        )
    
    except Exception as e:
        logger.error(f"Error in get_dashboard_info_endpoint: {e}", exc_info=True)
        return JSONResponse(
            status_code=500,
            content={"success": False, "error": "Internal server error"}
        )


@router.get(
    "/report-domains",
    response_model=ReportDomainsResponse,
    status_code=200,
    responses={
        401: {"model": ErrorResponse},
        500: {"model": ErrorResponse},
    },
)
async def get_report_domains(user_id: str = Depends(get_current_active_user)):
    """Return a summary of report domains for the authenticated user.

    Only domains that contain at least one non-draft report are included.
    Results are sorted by ``item_count`` descending.

    **Response fields per domain:**
    - ``domain_name``: Internal slug (e.g. ``"due_diligence"``)
    - ``category_name``: Human-readable label (e.g. ``"Due Diligence"``)
    - ``item_count``: Number of non-draft reports in that domain

    Authentication Required: Yes (JWT token)
    """
    logger.info(f"Get report-domains request received for user_id: {user_id}")

    try:
        async with async_session_scope() as session:
            result = await get_report_domain_summary(user_id=user_id, session=session)

            if not result.get("success"):
                logger.error(
                    f"Failed to fetch report domain summary for user_id: {user_id}: "
                    f"{result.get('error')}"
                )
                return JSONResponse(
                    status_code=500,
                    content={"success": False, "error": "Failed to retrieve report domains. Please try again."},
                )

            logger.info(
                f"Successfully retrieved report domains for user_id: {user_id}, "
                f"domain_count: {len(result.get('domains', []))}"
            )
            return ReportDomainsResponse(success=True, domains=result["domains"])

    except Exception as e:
        logger.error(f"Error in get_report_domains: {e}", exc_info=True)
        return JSONResponse(
            status_code=500,
            content={"success": False, "error": "Internal server error"},
        )



@router.get(
    "/report-domains/{domain_name}/reports",
    response_model=DomainReportsResponse,
    status_code=200,
    responses={
        400: {"model": ErrorResponse},
        401: {"model": ErrorResponse},
        500: {"model": ErrorResponse},
    },
)
async def get_domain_reports(
    domain_name: str,
    limit: int = Query(..., ge=1, le=100, description="Number of reports per page"),
    offset: int = Query(..., ge=0, description="Number of reports to skip"),
    user_id: str = Depends(get_current_active_user),
):
    """Return paginated non-draft reports for a specific domain for the authenticated user.

    Reports are sorted by ``last_activity_at`` descending (most recent first).

    **Path parameter:**
    - ``domain_name``: One of ``default``, ``primary_research``, ``due_diligence``,
      ``industry_benchmarking``, ``market_insight``, ``rfp``, ``business_plan``

    **Query parameters (required):**
    - ``limit`` (int, 1–100): Page size.
    - ``offset`` (int, ≥ 0): Pagination offset.

    **Response fields:**
    - ``total``: Total matching reports (use with ``limit``/``offset`` for client-side pagination)
    - ``reports``: Paginated list of report records

    Authentication Required: Yes (JWT token)
    """
    logger.info(
        f"Get domain-reports request received for user_id: {user_id}, "
        f"domain_name: {domain_name}, limit: {limit}, offset: {offset}"
    )

    if domain_name not in _VALID_DOMAIN_SLUGS:
        logger.warning(f"Invalid domain_name received: {domain_name}")
        return JSONResponse(
            status_code=400,
            content={
                "success": False,
                "error": (
                    f"Invalid domain '{domain_name}'. Must be one of: "
                    + ", ".join(sorted(_VALID_DOMAIN_SLUGS))
                ),
            },
        )

    try:
        async with async_session_scope() as session:
            result = await get_reports_by_domain(
                user_id=user_id,
                domain_name=domain_name,
                limit=limit,
                offset=offset,
                session=session,
            )

            if not result.get("success"):
                logger.error(
                    f"Failed to fetch reports for user_id: {user_id}, "
                    f"domain_name: {domain_name}: {result.get('error')}"
                )
                return JSONResponse(
                    status_code=500,
                    content={"success": False, "error": "Failed to retrieve reports. Please try again."},
                )

            logger.info(
                f"Successfully retrieved {len(result.get('reports', []))} of "
                f"{result.get('total', 0)} reports for user_id: {user_id}, "
                f"domain_name: {domain_name}"
            )
            return DomainReportsResponse(
                success=True,
                domain_name=result["domain_name"],
                category_name=result["category_name"],
                total=result["total"],
                limit=result["limit"],
                offset=result["offset"],
                reports=result["reports"],
            )

    except Exception as e:
        logger.error(f"Error in get_domain_reports: {e}", exc_info=True)
        return JSONResponse(
            status_code=500,
            content={"success": False, "error": "Internal server error"},
        )


# ============================================================================
# Homepage: categories, home-search, ongoing-chats
# ============================================================================


@router.get(
    "/categories",
    response_model=CategoriesResponse,
    status_code=200,
    responses={401: {"model": ErrorResponse}, 500: {"model": ErrorResponse}},
)
async def get_categories(user_id: str = Depends(get_current_active_user)):
    """Return the static catalog of all report categories.

    Returns the full set of categories regardless of whether the user has
    any reports in them. The internal ``default`` domain is surfaced as
    ``standard``; pass that slug back as-is on subsequent calls (the API
    will translate it internally where needed).

    Authentication Required: Yes (JWT token)
    """
    logger.info(f"Get categories request received for user_id: {user_id}")
    try:
        result = list_all_categories()
        if not result.get("success"):
            logger.error(f"Failed to build category catalog for user_id: {user_id}")
            return JSONResponse(
                status_code=500,
                content={"success": False, "error": "Failed to load categories. Please try again."},
            )
        return CategoriesResponse(
            success=True,
            categories=[CategoryItem(**c) for c in result["categories"]],
        )
    except Exception as e:
        logger.error(f"Error in get_categories: {e}", exc_info=True)
        return JSONResponse(
            status_code=500,
            content={"success": False, "error": "Internal server error"},
        )


def _parse_csv_param(value: Optional[str]) -> Optional[list]:
    """Split a comma-separated query param into a clean list; ``None`` if empty."""
    if not value:
        return None
    parts = [p.strip() for p in value.split(",") if p.strip()]
    return parts or None


def _parse_iso_datetime(
    raw: Optional[str], field_name: str
) -> Optional[datetime]:
    """Parse an ISO-8601 datetime; raise ValueError with a clean message on bad input."""
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(
            f"{field_name} must be an ISO-8601 datetime (e.g. 2026-01-15T00:00:00Z)"
        ) from exc


@router.get(
    "/home-search",
    response_model=HomeSearchResponse,
    status_code=200,
    responses={
        400: {"model": ErrorResponse},
        401: {"model": ErrorResponse},
        500: {"model": ErrorResponse},
    },
)
async def home_search(
    q: Optional[str] = Query(
        None,
        description="Case-insensitive substring matched against chat title and latest report title",
        max_length=200,
    ),
    domain: Optional[str] = Query(
        None,
        description="Comma-separated category slugs to filter by (e.g. 'standard,due_diligence')",
        max_length=200,
    ),
    status: Optional[str] = Query(
        None,
        description="Comma-separated report status values to filter by (e.g. 'draft,analysis-completed')",
        max_length=200,
    ),
    created_after: Optional[str] = Query(
        None,
        description="ISO-8601 datetime; only chats created at/after this time are returned",
    ),
    created_before: Optional[str] = Query(
        None,
        description="ISO-8601 datetime; only chats created at/before this time are returned",
    ),
    limit: int = Query(20, ge=1, le=100, description="Page size"),
    offset: int = Query(0, ge=0, description="Pagination offset"),
    user_id: str = Depends(get_current_active_user),
):
    """Homepage search across the authenticated user's chats and reports.

    Each result represents a chat (one row per chat); when the chat has a
    generated report, the latest report's title/poster are surfaced too.
    The search query (``q``) matches case-insensitively against both
    ``chat_title`` and the latest report's ``title``. The ``matched_on``
    field on each item indicates which field actually matched.

    All filters are additive (AND). Chats with no report are treated as
    having status ``draft`` and domain ``standard``.

    Authentication Required: Yes (JWT token)
    """
    logger.info(
        f"Home-search request received for user_id={user_id} q={q!r} "
        f"domain={domain} status={status} created_after={created_after} "
        f"created_before={created_before} limit={limit} offset={offset}"
    )

    try:
        domains_list = _parse_csv_param(domain)
        statuses_list = _parse_csv_param(status)
        created_after_dt = _parse_iso_datetime(created_after, "created_after")
        created_before_dt = _parse_iso_datetime(created_before, "created_before")
    except ValueError as exc:
        logger.warning(f"home-search bad date input: {exc}")
        return JSONResponse(
            status_code=400,
            content={"success": False, "error": str(exc)},
        )

    if (
        created_after_dt is not None
        and created_before_dt is not None
        and created_after_dt > created_before_dt
    ):
        return JSONResponse(
            status_code=400,
            content={
                "success": False,
                "error": "created_after must be earlier than or equal to created_before",
            },
        )

    try:
        async with async_session_scope() as session:
            result = await search_chats_and_reports(
                user_id=user_id,
                session=session,
                query=q,
                domains=domains_list,
                statuses=statuses_list,
                created_after=created_after_dt,
                created_before=created_before_dt,
                limit=limit,
                offset=offset,
            )

            if not result.get("success"):
                logger.error(
                    f"home-search DB failure for user_id={user_id}: {result.get('error')}"
                )
                return JSONResponse(
                    status_code=500,
                    content={"success": False, "error": "Failed to run search. Please try again."},
                )

            return HomeSearchResponse(
                success=True,
                total=result["total"],
                limit=result["limit"],
                offset=result["offset"],
                items=[HomeSearchItem(**item) for item in result["items"]],
            )

    except Exception as e:
        logger.error(f"Error in home_search: {e}", exc_info=True)
        return JSONResponse(
            status_code=500,
            content={"success": False, "error": "Internal server error"},
        )


@router.get(
    "/ongoing-chats",
    response_model=OngoingChatsResponse,
    status_code=200,
    responses={401: {"model": ErrorResponse}, 500: {"model": ErrorResponse}},
)
async def ongoing_chats(user_id: str = Depends(get_current_active_user)):
    """Return the user's ongoing (draft) chats, grouped by category.

    A chat is included here when its latest report is in ``draft`` status,
    or when no report has been generated yet. Because draft reports do not
    yet have a ``domain_name`` in the DB, every entry currently lands under
    the ``standard`` key. Additional category keys will start appearing on
    this response once drafts begin carrying a domain.

    Within each group, chats are ordered by ``updated_at`` descending
    (most-recent first).

    Authentication Required: Yes (JWT token)
    """
    logger.info(f"Ongoing-chats request received for user_id={user_id}")
    try:
        async with async_session_scope() as session:
            result = await get_draft_chats_grouped(user_id=user_id, session=session)

            if not result.get("success"):
                logger.error(
                    f"ongoing-chats DB failure for user_id={user_id}: {result.get('error')}"
                )
                return JSONResponse(
                    status_code=500,
                    content={
                        "success": False,
                        "error": "Failed to load ongoing chats. Please try again.",
                    },
                )

            groups = result.get("groups", {})
            standard_items = [
                OngoingChatItem(**item)
                for item in groups.get(STANDARD_CATEGORY_SLUG, [])
            ]

            # Surface any other category groups that drift in (forward
            # compatibility — today the DB always groups drafts under
            # 'standard', but we don't want to silently drop a real bucket).
            extras = {
                slug: items for slug, items in groups.items()
                if slug != STANDARD_CATEGORY_SLUG and items
            }
            if extras:
                logger.warning(
                    f"ongoing-chats: drafts found under non-standard groups "
                    f"for user_id={user_id}: {sorted(extras.keys())}"
                )

            return OngoingChatsResponse(success=True, standard=standard_items)

    except Exception as e:
        logger.error(f"Error in ongoing_chats: {e}", exc_info=True)
        return JSONResponse(
            status_code=500,
            content={"success": False, "error": "Internal server error"},
        )
