"""auth_api.py: Authentication API for the Casper backend"""
from fastapi import APIRouter, HTTPException, Request, Depends
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError
from uuid_utils import uuid7
from datetime import datetime, timezone
from fastapi.security import HTTPAuthorizationCredentials
from src.config.constants import JWT_SECRET_KEY, JWT_ALGORITHM

from src.db.db_utils import async_session_scope
from src.config.log_helper import setup_logging
from src.db.async_db_functions import (
    create_user,
    check_user_exists_by_email,
    verify_user_credentials,
    update_user_token,
    check_user_by_id
)
from src.resources.schemas.auth import (
    SignupRequest,
    SignupResponse,
    LoginRequest,
    LoginResponse,
    LogoutRequest,
    LogoutResponse,
    ErrorResponse
)
from src.resources.exceptions import http_exception_handler, validation_exception_handler
from src.core.token_auth import (
    create_access_token,
    create_refresh_token,
    verify_token,
)

# Configure logging
logger = setup_logging(__file__)

# Create router
router = APIRouter()

# Exception handlers
router.exception_handler(HTTPException)(http_exception_handler)
router.exception_handler(RequestValidationError)(validation_exception_handler)

# API Endpoints
@router.post("/signup", response_model=SignupResponse, responses={422: {"model": ErrorResponse}, 500: {"model": ErrorResponse}})
async def signup(data: SignupRequest):
    """User signup endpoint"""
    try:
        async with async_session_scope() as session:
            # Validate input data
            if data.name.strip() == "":
                logger.error("Empty name received after trimming whitespace")
                return JSONResponse(
                    status_code=422,
                    content={"success": False, "error": "Name cannot be empty"}
                )
                
            if data.phone.strip() == "":
                logger.error("Empty phone received after trimming whitespace")
                return JSONResponse(
                    status_code=422,
                    content={"success": False, "error": "Phone number cannot be empty"}
                )
                
            if data.phone_country_code.strip() == "":
                logger.error("Empty phone_country_code received after trimming whitespace")
                return JSONResponse(
                    status_code=422,
                    content={"success": False, "error": "Phone country code cannot be empty"}
                )
                
            if data.password.strip() == "":
                logger.error("Empty password received after trimming whitespace")
                return JSONResponse(
                    status_code=422,
                    content={"success": False, "error": "Password cannot be empty"}
                )
            
            # Check if user already exists
            db_response = await check_user_exists_by_email(email=data.email, session=session)
            if not db_response.get('success', False):
                logger.error(f"Database error: Failed to check if user exists: {db_response.get('error')}")
                return JSONResponse(
                    status_code=500,
                    content={"success": False, "error": "Internal server error"}
                )
            
            if db_response.get('exists', False):
                logger.warning(f"User with email {data.email} already exists")
                return JSONResponse(
                    status_code=422,
                    content={"success": False, "error": "User with this email already exists"}
                )
            
            # Create new user
            user_id = str(uuid7())
            
            logger.info(f"Creating new user: {data.name}, {data.email}")
            
            # Create new user record
            new_user = {
                "user_id": user_id,
                "user_name": data.name,
                "email": data.email,
                "phone": data.phone,
                "phone_country_code": data.phone_country_code,
                "password": data.password
            }
            
            db_response = await create_user(user_data=new_user, session=session)
            if not db_response.get('success', False):
                logger.error(f"Database error: Failed to create user: {db_response.get('error')}")
                return JSONResponse(
                    status_code=500,
                    content={"success": False, "error": "Internal server error"}
                )
            
            logger.info(f"User created successfully with ID: {user_id}")
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
        try:
            await session.rollback()
        except Exception as e:
            logger.error(f"Error in rollback: {e}")
        return JSONResponse(
            status_code=500,
            content={"success": False, "error": "Internal server error"}
        )

@router.post("/login", response_model=LoginResponse, responses={422: {"model": ErrorResponse}, 500: {"model": ErrorResponse}})
async def login(data: LoginRequest):
    """User login endpoint"""
    try:
        async with async_session_scope() as session:
            # Check if user exists and credentials are valid
            db_response = await verify_user_credentials(email=data.email, password=data.password, session=session)
            
            if not db_response.get('success', False):
                logger.error(f"Database error: {db_response.get('error')}")
                return JSONResponse(
                    status_code=500,
                    content={"success": False, "error": "Internal server error"}
                )
            
            if not db_response.get('valid', False):
                logger.warning(f"Invalid login attempt for email {data.email}")
                return JSONResponse(
                    status_code=422,
                    content={"success": False, "error": "Invalid email or password"}
                )
            
            user_id = db_response.get('id')
            user_name = db_response.get('user_name')
            
            # Generate JWT tokens
            access_token = create_access_token(user_id)
            refresh_token = create_refresh_token(user_id)
            
            # Store token in database
            token_response = await update_user_token(user_id=user_id, token=access_token, session=session)
            
            if not token_response.get('success', False):
                logger.error(f"Failed to update user token: {token_response.get('error')}")
                return JSONResponse(
                    status_code=500,
                    content={"success": False, "error": "Internal server error"}
                )
            
            logger.info(f"User logged in successfully with ID: {user_id}")
            return LoginResponse(success=True, token=access_token, refresh_token=refresh_token, user_id=user_id, user_name=user_name)
            
    except Exception as e:
        logger.error(f"Error in login: {e}")
        try:
            await session.rollback()
        except Exception as e:
            logger.error(f"Error in rollback: {e}")
        return JSONResponse(
            status_code=500,
            content={"success": False, "error": "Internal server error"}
        )

@router.post("/logout", response_model=LogoutResponse, responses={401: {"model": ErrorResponse}, 422: {"model": ErrorResponse}, 500: {"model": ErrorResponse}})
async def logout(data: LogoutRequest, credentials: HTTPAuthorizationCredentials = Depends(security)):
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
            content={"success": False, "error": "Invalid request"}
        )
    
    try:
        async with async_session_scope() as session:
            # Clear the user's token
            user_id = verify_token(refresh_token, "refresh")

            db_response = await check_user_by_id(user_id=user_id, session=session)      
            if not db_response.get('success', False):
                logger.error(f"Failed to logout user: {db_response.get('error')}")
                return JSONResponse(
                    status_code=500,
                    content={"success": False, "error": "Internal server error"}
                )
                        
            logger.info(f"User logged out successfully with ID: {user_id}")
            return LogoutResponse(success=True)
            
    except Exception as e:
        logger.error(f"Error in logout: {e}")
        try:
            await session.rollback()
        except Exception as e:
            logger.error(f"Error in rollback: {e}")
        return JSONResponse(
            status_code=500,
            content={"success": False, "error": "Internal server error"}
        )

# Health check endpoint
@router.get("/health")
async def health_check():
    """Health check endpoint"""
    return {"status": "healthy", "timestamp": datetime.now(timezone.utc).isoformat()}
