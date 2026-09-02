"""
async_db_functions.py
Database operation functions for message and report operations with comprehensive error handling
and logging, adapted for FastAPI's asynchronous architecture.

This module provides a set of functions for handling user messages and reports,
including retrieving user details, checking user existence, and inserting messages and reports.
"""

from typing import Dict, Any, Optional
import copy
from uuid_utils import uuid7
import string
import secrets
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import selectinload
from datetime import datetime, timezone

from src.core.common.utils import prune_report_layout_async, verify_password
from src.db.database import Card, Publish, User, Message, Report, Table, CardVersion, Subscriber, Request, CallBooking, ReportVersion, ReportVersionCard, RefinementHistory, AskCasprChat, CostTracker
from src.db.enums import ReportStatus, RefinementType
from src.db.db_utils import  sanitize_card_data
from src.config.log_helper import setup_logging
from fastapi.concurrency import run_in_threadpool


# Configure logging
logger = setup_logging(__file__)


def _normalize_table_title_value(value: Any) -> str:
    """Normalize table_title payloads to a plain string."""
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return _normalize_table_title_value(value[0] if value else "")
    if isinstance(value, str):
        return value
    return str(value)


def _normalize_tables_for_response(tables: Any) -> list[Dict[str, Any]]:
    """Normalize tables list so each table_title is always a string."""
    if not isinstance(tables, list):
        return []

    normalized_tables = []
    for table in tables:
        if not isinstance(table, dict):
            continue
        table_copy = dict(table)
        table_copy["table_title"] = _normalize_table_title_value(table_copy.get("table_title", ""))
        normalized_tables.append(table_copy)

    return normalized_tables


def _normalize_subsections_for_response(sub_sections: Any) -> list[Dict[str, Any]]:
    """Normalize subsection tables for API-safe output."""
    if not isinstance(sub_sections, list):
        return []

    normalized_sub_sections = []
    for sub in sub_sections:
        if not isinstance(sub, dict):
            continue
        sub_copy = dict(sub)
        sub_copy["tables"] = _normalize_tables_for_response(sub_copy.get("tables", []))
        normalized_sub_sections.append(sub_copy)

    return normalized_sub_sections


async def generate_unique_referral_code(session: AsyncSession) -> str:
    """
    Generate unique 8-character referral code.
    
    Args:
        session: Database session
        
    Returns:
        Unique referral code (uppercase alphanumeric)
        
    Raises:
        ValueError: If unable to generate unique code after max attempts
    """
    chars = string.ascii_uppercase + string.digits
    max_attempts = 10
    
    for attempt in range(max_attempts):
        code = ''.join(secrets.choice(chars) for _ in range(8))
        
        # Check if code already exists
        result = await session.execute(
            select(User.id).where(User.referral_code == code)
        )
        if not result.scalar_one_or_none():
            logger.info(f"[REFERRAL_CODE_GENERATED] code={code}, attempts={attempt + 1}")
            return code
    
    logger.error(f"[REFERRAL_CODE_GENERATION_FAILED] max_attempts={max_attempts}")
    raise ValueError("Failed to generate unique referral code after maximum attempts")

async def get_user_details(user_id: str, session: AsyncSession) -> Dict[str, Any]:
    """
    Get user details by user_id.
    
    Args:
        user_id (str): User ID to retrieve
        session (AsyncSession): SQLAlchemy async session
            
    Returns:
        Dict[str, Any]: Dictionary with success status and user details if found
    """
    logger.info(f"Getting user details for user_id: {user_id}")
    
    if not user_id:
        logger.error("Invalid user_id: empty value provided")
        return {
            "success": False,
            "error": "Invalid user ID: empty value provided"
        }
    
    # redis_instance = get_redis_instance()

    # # Get user details from redis
    # logger.info(f"Checking redis for user with ID: {user_id}")
    # redis_response = await redis_instance.get_user(user_id, ttl=3600)
    # if redis_response.get('success'):
    #     logger.info(f"User with ID {user_id} found in redis")
    #     return {
    #         "success": True,
    #         "user": redis_response.get('user_data')
    #     }
    try:
        # Query to get user details
        stmt = select(User).where(User.id == user_id)
        result = await session.execute(stmt)
        user = result.scalar_one_or_none()
        
        if user:
            logger.info(f"User found with ID: {user_id}")
            return {
                "success": True,
                "user": {
                    "user_id": user.id,
                    "user_name": user.user_name,
                    "email": user.email,
                    "phone": user.phone,
                    "phone_country_code": user.phone_country_code,
                    "created_at": user.created_at,
                    "is_google_verified": user.is_google_verified,
                    "onboarding_completed": user.onboarding_completed,
                    "user_role_id": user.user_role_id,
                }
            }
        else:
            logger.warning(f"No user found with ID: {user_id}")
            return {
                "success": True,
                "user": None
            }
            
    except SQLAlchemyError as e:
        logger.error(f"Database error retrieving user details: {str(e)}", exc_info=True)
        return {
            "success": False,
            "error": str(e)
        }
    except Exception as e:
        logger.error(f"Unexpected error retrieving user details: {str(e)}", exc_info=True)
        return {
            "success": False,
            "error": str(e)
        }


async def get_user_chat(user_id: str, chat_id: str, session: AsyncSession) -> Dict[str, Any]:
    """
    Get message details for a specific chat_id.
    
    Args:
        chat_id (str): Chat ID to retrieve message for
        session (AsyncSession): SQLAlchemy async session
            
    Returns:
        Dict[str, Any]: Dictionary with success status and message if found
    """
    logger.info(f"Getting messages for chat_id: {chat_id}")
    
    if not chat_id:
        logger.error("Invalid chat_id: empty value provided")
        return {
            "success": False,
            "error": "Invalid chat ID: empty value provided"
        }
    
    if not user_id:
        logger.error("Invalid user_id: empty value provided")
        return {
            "success": False,
            "error": "Invalid user_id: empty value provided"
        }
    
    # redis_instance = get_redis_instance()
    # logger.info(f"Checking redis for chat_id: {chat_id}")
    # redis_response = await redis_instance.get_chat(chat_id, ttl=3600)
    # if redis_response.get('success'):
    #     logger.info(f"Chat with ID {chat_id} found in redis")
    #     return {
    #         "success": True,
    #         "message": {
    #             "id": redis_response.get('chat_data').get('chat_id'),
    #             "user_id": redis_response.get('chat_data').get('user_id'),
    #             "chat_title": redis_response.get('chat_data').get('chat_title'),
    #             "chat_messages": redis_response.get('chat_data').get('chat_messages'),
    #             "created_at": redis_response.get('chat_data').get('created_at'),
    #             "updated_at": redis_response.get('chat_data').get('updated_at')
    #         }
    #     }
    
    try:
        # Query for specific message by chat_id
        stmt = select(Message).where(Message.id == chat_id, Message.user_id == user_id)
        result = await session.execute(stmt)
        message = result.scalar_one_or_none()
        
        if message:
            logger.info(f"Found message with chat_id: {chat_id}")
            message_data = {
                "id": message.id,
                "user_id": message.user_id,
                "chat_title": message.chat_title,
                "chat_messages": message.chat_messages,
                "message_citations": message.message_citations or {},
                "created_at": message.created_at,
                "updated_at": message.updated_at,
            }
            return {
                "success": True,
                "message": message_data
            }
        else:
            # logger.warning(f"No message found with chat_id: {chat_id}")
            return {
                "success": True,
                "message": {}
            }
            
    except SQLAlchemyError as e:
        logger.error(f"Database error retrieving chat message: {str(e)}", exc_info=True)
        return {
            "success": False,
            "error": str(e)
        }
    except Exception as e:
        logger.error(f"Unexpected error retrieving chat message: {str(e)}", exc_info=True)
        return {
            "success": False,
            "error": str(e)
        }

async def insert_chats(message_data: Dict[str, Any], session: AsyncSession) -> Dict[str, Any]:
    """
    Insert a new message or update an existing message based on the presence of created_at.
    If created_at is present, it's a new chat; otherwise, it's an update to an existing chat.
    
    Args:
        message_data (Dict[str, Any]): Dictionary containing message data with keys:
            - chat_id: Chat identifier (required)
            - user_id: User ID (required)
            - created_at: Creation timestamp (required for new chats)
            - chat_title: Title of the chat (optional)
            - chat_messages: Complete message data stored as JSONB (optional)
            - updated_at: Update timestamp (optional, will be set if not provided)
        session (AsyncSession): SQLAlchemy async session
            
    Returns:
        Dict[str, Any]: Dictionary with success status and operation performed
    """
    logger.info(f"Processing chat for user_id: {message_data.get('user_id')} with chat_id: {message_data.get('chat_id')}")
    
    if 'user_id' not in message_data or not message_data['user_id']:
        logger.error("Missing required field: user_id")
        return {
            "success": False,
            "error": "Missing required field: user_id"
        }       

    # redis_instance = get_redis_instance()  

    try:
        # First, check if the chat already exists in the database
        chat_id = message_data.get('chat_id')
        stmt = select(Message).where(Message.id == chat_id)
        result = await session.execute(stmt)
        existing_chat = result.scalar_one_or_none()
        
        if existing_chat:
            # Chat exists - UPDATE it
            logger.info(f"Updating existing chat with chat_id: {chat_id}")
            
            # Validate ownership before updating
            if existing_chat.user_id != message_data.get('user_id'):
                logger.error(f"Ownership validation failed: User {message_data.get('user_id')} attempted to update chat {chat_id} owned by user {existing_chat.user_id}")
                return {
                    "success": False,
                    "error": "Unauthorized: Cannot update chat owned by another user"
                }
            
            # Build update values dynamically
            update_values = {}
            if message_data.get('chat_messages') is not None:
                update_values['chat_messages'] = message_data['chat_messages']
            if message_data.get('chat_title') is not None:
                update_values['chat_title'] = message_data['chat_title']
            if message_data.get('updated_at') is not None:
                update_values['updated_at'] = message_data['updated_at']
            if message_data.get('message_citations') is not None:
                # Merge new citations into the existing dict (preserves prior turns)
                merged = {**(existing_chat.message_citations or {}), **message_data['message_citations']}
                update_values['message_citations'] = merged
            
            if update_values:
                stmt = update(Message).values(**update_values).where(Message.id == chat_id)
                await session.execute(stmt)
                await session.commit()
                logger.info(f"Chat updated with chat_id: {chat_id}")
            else:
                logger.warning(f"No fields to update for chat_id: {chat_id}")

            # Update redis
            # logger.info(f"Updating chat in redis with chat_id: {chat_id}")
            # redis_response = await redis_instance.update_chat(
            #     chat_data={
            #         'chat_id': chat_id,
            #         'chat_messages': message_data.get('chat_messages'),
            #         'chat_title': message_data.get('chat_title'),
            #         'updated_at': message_data.get('updated_at')
            #     },
            #     ttl=3600
            # )
            # if redis_response.get('success'):
            #     logger.info(f"Chat updated in redis with chat_id: {chat_id}")
            # else:
            #     logger.error(f"Failed to update chat in redis with chat_id: {chat_id}")
            
            return {
                "success": True,
                "chat_id": chat_id,
                "is_new_chat": False
            }
        else:
            # Chat doesn't exist - CREATE it
            logger.info(f"Creating new chat with chat_id: {chat_id}")
            
            if not message_data.get('created_at'):
                logger.error(f"Cannot create new chat without created_at for chat_id: {chat_id}")
                return {
                    "success": False,
                    "error": "Cannot create new chat without created_at timestamp"
                }
            
            # Create message record
            new_message = Message(
                user_id=message_data['user_id'],
                created_at=message_data['created_at'],
                chat_title=message_data.get('chat_title'),
                chat_messages=message_data.get('chat_messages'),
                message_citations=message_data.get('message_citations'),
                updated_at=message_data.get('updated_at'),
                id=chat_id
            )
            
            session.add(new_message)
            await session.commit()
            logger.info(f"New chat created with chat_id {new_message.id}")

            # logger.info(f"Creating chat in redis with chat_id: {new_message.id}")
            # redis_response = await redis_instance.check_chat_exists(new_message.id, ttl=3600)
            # if redis_response.get('success') and not redis_response.get('exists'):
                # redis_response = await redis_instance.create_chat(message_data, ttl=3600)
                # if redis_response.get('success'):   
                    # logger.info(f"Chat created in redis with chat_id: {new_message.id}")
            # redis_response = await redis_instance.create_chat(message_data, ttl=3600)
            # if redis_response.get('success'):   
                # logger.info(f"Chat created in redis with chat_id: {new_message.id}")
            
            return {
                "success": True,
                "chat_id": new_message.id,
                "is_new_chat": True
            }
            
    except SQLAlchemyError as e:
        logger.error(f"Database error inserting/updating message: {str(e)}", exc_info=True)
        return {
            "success": False,
            "error": str(e)
        }
    except Exception as e:
        logger.error(f"Unexpected error inserting/updating message: {str(e)}", exc_info=True)
        return {
            "success": False,
            "error": str(e)
        }
    
async def check_user_by_id(user_id: str, session: AsyncSession) -> Dict[str, Any]:
    """
    Check if a user exists in the database by their user_id.
    
    Args:
        user_id (str): The user_id to check
        session (AsyncSession): The SQLAlchemy async session
            
    Returns:
        Dict[str, Any]: Dictionary with success status and user details if found
    """
    logger.info(f"Checking if user with ID {user_id} exists")

    if not user_id:
        logger.error("Invalid user_id: empty value provided")
        return {
            "success": False,
            "error": "Invalid user ID: empty value provided"
        }
    
    # redis_instance = get_redis_instance()
    # logger.info(f"Checking redis for user_id: {user_id}")
    # redis_response = await redis_instance.check_user_exists(user_id, ttl=3600)
    # if redis_response.get('success') and redis_response.get('exists'):
    #     logger.info(f"User with ID {user_id} exists in redis")
    #     return {
    #         "success": True,
    #         "exists": True
    #     }
    
    try:
        # Query to get user details
        stmt = select(User).where(User.id == user_id)
        result = await session.execute(stmt)
        user = result.scalar_one_or_none()
        
        if user:
            logger.info(f"User found with ID: {user_id}")
            return {
                "success": True,
                "exists": True,
                "t_c_verified": user.t_c_verified,
                "user_name": user.user_name
            }
        
        logger.warning(f"User with ID {user_id} not found")
        return {
            "success": True,
            "exists": False,
            "error": f"User with ID {user_id} not found"
        }
    
    except SQLAlchemyError as e:
        logger.error(f"Database error checking user by ID: {str(e)}", exc_info=True)
        return {
            "success": False,
            "exists": False,
            "error": f"Database error: {str(e)}"
        }
        
    except Exception as e:
        logger.error(f"Unexpected error checking user by ID: {str(e)}", exc_info=True)
        return {
            "success": False,
            "exists": False,
            "error": f"Unexpected error: {str(e)}"
        }

async def verify_user_credentials(email: str, password: str, session: AsyncSession) -> Dict[str, Any]:
    """
    Verify user credentials by checking if the user exists and has a valid token.
    
    Args:
        email (str): The email to check
        password (str): The password to check
        session (AsyncSession): The SQLAlchemy async session
            
    Returns:
        Dict[str, Any]: Dictionary with success status and user details if found
    """
    try:
        logger.info(f"Verifying user credentials for email: {email}")

        statement = select(User).where(User.email == email)
        result = await session.execute(statement)
        user = result.scalar_one_or_none()

        if not user:
            logger.error(f"User with email {email} not found")
            return {
                "success": True,
                "valid": False,
                "error": f"User with email {email} not found"
            }
        
        if user.is_google_verified: 
            logger.info(f"User with email {email} is google verified")
            return {
                "success": True,
                "valid": False,
                "id": user.id,
                "error": "This email is registered via Google login. Please continue with Google to sign in.",
                "is_google_verified": True,
            }
        
        if not user.is_verified:
            logger.info(f"User with email {email} is not verified")
            return {
                "success": True,
                "valid": False,
                "error": "Your account is waiting for verification. Just click the link in your email to get started.",
                "is_verified": False,
            }

        if not verify_password(password, user.password_hash):
            logger.error(f"Invalid password for user with email {email}")
            return {
                "success": True,
                "valid": False,
                "error": "That password doesn't quite match. Feel free to try again, or reset it if you'd like."
            }
        
        # response = await update_user_in_redis(user_data={
        #     'user_id': user.id,
        #     'email': user.email,
        #     'phone': user.phone,
        #     'created_at': user.created_at,
        #     'user_name': user.user_name,
        #     'phone_country_code': user.phone_country_code,
        #     'password_hash': user.password_hash,
        #     'is_google_verified': user.is_google_verified
        # }, session=session)
        
        # if not response.get('success', False):
        #     logger.error(f"Failed to update user in redis: {response.get('error')}")
        
        # redis_instance = get_redis_instance()
        # logger.info(f"Checking redis for user_id: {user.id}")
        # redis_response = await redis_instance.check_user_exists(user.id, ttl=3600)
        # if redis_response.get('success') and not redis_response.get('exists'):
        #     redis_response = await redis_instance.create_user(
        #         user_data={
        #             'user_id': user.id,
        #             'email': user.email,
        #             'phone': user.phone,
        #             'created_at': user.created_at,
        #             'user_name': user.user_name,
        #             'phone_country_code': user.phone_country_code,
        #             'password_hash': user.password_hash
        #         }
        #     )
        
        logger.info(f"User with email {email} verified successfully")
        
        return {
            "success": True,
            "valid": True,
            "id": user.id,
            "user_name": user.user_name,
            "email": user.email,
            "phone": user.phone,
            "phone_country_code": user.phone_country_code,
            "is_verified": user.is_verified,
            "verified_at": user.verified_at,
            "t_c_verified": user.t_c_verified,
            "onboarding_completed": user.onboarding_completed,
        }
    
    except SQLAlchemyError as e:
        logger.error(f"Database error verifying user credentials: {str(e)}", exc_info=True)
        return {
            "success": False,
            "valid": False,
            "error": f"Database error: {str(e)}"
        }
    
    except Exception as e:
        logger.error(f"Unexpected error verifying user credentials: {str(e)}", exc_info=True)
        return {
            "success": False,
            "valid": False,
            "error": f"Unexpected error: {str(e)}"
        }
    
async def create_user(user_data: Dict[str, Any], session: AsyncSession) -> Dict[str, Any]:
    """
    Create a new user in the database.
    
    Args:
        user_data (dict): Dictionary containing user data
        session (AsyncSession): The SQLAlchemy async session
            
    Returns:
        Dict[str, Any]: Dictionary with success status and user details if found
    """
    logger.info(f"Creating new user with data: {user_data.get('email')}")    
    
    try:
        # Generate unique referral code for the new user
        referral_code = await generate_unique_referral_code(session)
        user_data['referral_code'] = referral_code
        
        # Create a new user instance
        new_user = User(**user_data)
        # user_id = str(uuid7())
        # new_user.id = user_id
        # Add the new user to the session
        session.add(new_user)   
        await session.flush()  # Use flush instead of commit to keep transaction open
        
        logger.info(f"User created successfully with ID: {new_user.id}, referral_code: {referral_code}")

        # Add user to redis
        # user_data['user_id'] = new_user.id
        # user_data['created_at'] = new_user.created_at
        # redis_instance = get_redis_instance()
        # logger.info(f"Adding user to redis with ID: {new_user.id}")
        # redis_response = await redis_instance.create_user(user_data, ttl=3600)
        # if redis_response.get('success'):
        #     logger.info(f"User added to redis successfully with ID: {new_user.id}")
        # else:
        #     logger.error(f"Failed to add user to redis with ID: {new_user.id}")

        return {
            "success": True,
            "id": new_user.id,
        }
    
    except SQLAlchemyError as e:
        logger.error(f"Database error creating user: {str(e)}", exc_info=True)
        return {
            "success": False,
            "error": f"Database error: {str(e)}"
        }

async def check_user_exists_by_email_or_phone(email: str = None, phone: str = None, session: AsyncSession = None) -> Dict[str, Any]:
    """
    Check if a user exists in the database by their email or phone number using a single database call.
    
    Args:
        email (str, optional): The email to check
        phone (str, optional): The phone number to check
        session (AsyncSession): The SQLAlchemy async session
            
    Returns:
        Dict[str, Any]: Dictionary with success status and detailed match information
    """
    logger.info(f"Checking if user exists with email: {email} or phone: {phone}")

    if not email or not phone:
        logger.error("Either email or phone is not provided")
        return {
            "success": False,
            "error": "Provide both email and phone"
        }

    try:
        # Build conditions for the query
        conditions = [
            User.email == email,
            User.phone == phone
        ]
        
        # Single query to get all matching users
        stmt = select(User).where(or_(*conditions))
        result = await session.execute(stmt)
        users = result.scalars().all()

        if users:
            response = {
                "success": True,
                "exists": True,
            }
        else:
            response = {
                "success": True,
                "exists": False,
            }

        logger.info(f"User existence check completed: {response}")
        return response
    
    except SQLAlchemyError as e:
        logger.error(f"Database error checking user existence: {str(e)}", exc_info=True)
        return {
            "success": False,
            "error": f"Database error: {str(e)}"
        }
    
    except Exception as e:
        logger.error(f"Unexpected error checking user existence: {str(e)}", exc_info=True)
        return {
            "success": False,
            "error": f"Unexpected error: {str(e)}"
        }


async def get_user_chats(
    user_id: int,
    session: AsyncSession,
    limit: Optional[int] = None,
    offset: int = 0,
) -> Dict[str, Any]:
    """
    Get chat list for a specific user_id.

    Performance notes:
    - Projects only the columns needed to render the sidebar/chat-list.
      Previously this SELECT pulled the entire `Message` row, including the
      large ``chat_messages`` JSONB column (can be many MB per row on active
      users). That dominates RDS "ClientWrite" time and wastes bandwidth.
    - Adds ``ORDER BY updated_at DESC`` so results are deterministic and the
      most-recent chats come first (what every UI expects).
    - Accepts optional ``limit``/``offset`` so callers can paginate. The
      default is unlimited for backwards compatibility with existing callers.

    Args:
        user_id (int): The user_id to get chats for.
        session (AsyncSession): SQLAlchemy async session.
        limit (Optional[int]): Max rows to return. ``None`` = no limit.
        offset (int): Rows to skip for pagination.

    Returns:
        Dict[str, Any]: {"success": bool, "chats": [...]} or error dict.
    """
    logger.info(f"Getting chat messages for user_id: {user_id} (limit={limit}, offset={offset})")

    if not user_id:
        logger.error("Invalid user_id: empty value provided")
        return {
            "success": False,
            "error": "Invalid user ID: empty value provided"
        }

    try:
        stmt = (
            select(
                Message.id,
                Message.chat_title,
                Message.created_at,
                Message.updated_at,
                Message.is_deleted,
                Message.deleted_at,
            )
            .where(Message.user_id == user_id, Message.is_deleted.is_(False))
            .order_by(Message.updated_at.desc().nullslast())
        )
        if offset:
            stmt = stmt.offset(offset)
        if limit is not None:
            stmt = stmt.limit(limit)

        result = await session.execute(stmt)
        rows = result.all()

        if not rows:
            logger.info(f"No chat messages found for user_id: {user_id}")
            return {"success": True, "chats": []}

        chat_list = [
            {
                "id": row.id,
                "chat_title": row.chat_title,
                "created_at": row.created_at.isoformat() if row.created_at else None,
                "updated_at": row.updated_at.isoformat() if row.updated_at else None,
                "is_deleted": row.is_deleted,
                "deleted_at": row.deleted_at.isoformat() if row.deleted_at else None,
            }
            for row in rows
        ]

        logger.info(f"Successfully retrieved {len(chat_list)} chats for user_id: {user_id}")
        return {"success": True, "chats": chat_list}
    
    except SQLAlchemyError as e:
        logger.error(f"Database error retrieving chat messages: {str(e)}", exc_info=True)
        return {
            "success": False,
            "error": f"Database error: {str(e)}"
        }
    
    except Exception as e:
        logger.error(f"Unexpected error retrieving chat messages: {str(e)}", exc_info=True)
        return {
            "success": False,
            "error": f"Unexpected error: {str(e)}"
        }
    
async def get_chat_reports(chat_id: int, session: AsyncSession) -> Dict[str, Any]:
    """
    Get all report files for a specific chat_id.
    
    Args:
        chat_id (int): The chat_id to get reports for
        session (AsyncSession): The SQLAlchemy async session
            
    Returns:
        Dict[str, Any]: Dictionary with success status and user details if found
    """
    logger.info(f"Getting report files for chat_id: {chat_id}")

    if not chat_id:
        logger.error("Invalid chat_id: empty value provided")
        return {
            "success": False,
            "error": "Invalid chat ID: empty value provided"
        }
    
    report_list = []

    # redis_instance = get_redis_instance()
    # logger.info(f"Checking redis for chat_id: {chat_id}")
    # redis_response = await redis_instance.get_chat_report(chat_id, ttl=3600)
    # if redis_response.get('success') and redis_response.get('reports'):
    #     logger.info(f"Reports retrieved from redis for chat_id: {chat_id}")
    #     for report in redis_response.get('reports'):
    #         report_list.append({
    #             "id": report.get('report_id'),
    #             "source_documents": report.get('source_documents'),
    #             "s3_uri": report.get('s3_uri'),
    #             "created_at": report.get('created_at').isoformat() if report.get('created_at') else None
    #         })
    #     return {
    #         "success": True,
    #         "reports": report_list
    #     }

    try:
        # Query to get all report files for the chat
        stmt = select(Report).where(Report.chat_id == chat_id)  
        result = await session.execute(stmt)
        reports = result.scalars().all()
        
        if not reports:
            logger.warning(f"No report files found for chat_id: {chat_id}")
            return {
                "success": True,
                "reports": []
            }
        
        for report in reports:
            report_list.append({
                "id": report.id,
                "citations": report.citations,
                "s3_uri": report.s3_uri,
                "created_at": report.created_at.isoformat() if report.created_at else None
            })

        response = {
            "success": True,    
            "reports": report_list
        }

        logger.info(f"Successfully retrieved {len(report_list)} reports for chat_id: {chat_id}")
        return response
    
    except SQLAlchemyError as e:
        logger.error(f"Database error retrieving report files: {str(e)}", exc_info=True)
        return {
            "success": False,
            "error": f"Database error: {str(e)}"
        }

    except Exception as e:
        logger.error(f"Unexpected error retrieving report files: {str(e)}", exc_info=True)
        return {
            "success": False,
            "error": f"Unexpected error: {str(e)}"
        }   
    

async def get_file_s3_path(id: int, file_type: str, session: AsyncSession) -> Dict[str, Any]:
    """
    Get the S3 path for a specific file based on its ID and file type.
    
    Args:
        id (int): The ID of the file
        file_type (str): The type of the file (e.g., 'report', 'chat_message')
        session (AsyncSession): The SQLAlchemy async session
            
    Returns:
        Dict[str, Any]: Dictionary with success status and file details if found
    """
    logger.info(f"Getting S3 path for file with ID: {id} and file_type: {file_type}")

    if not id:
        logger.error("Invalid file ID: empty value provided")
        return {
            "success": False,
            "error": "Invalid file ID: empty value provided"
        }
    
    if not file_type:
        logger.error("Invalid file type: empty value provided")
        return {
            "success": False,
            "error": "Invalid file type: empty value provided"
        }
    
    try:
        # Query to get the S3 path for the file
        stmt = select(Report.s3_uri).where(Report.id == id)
        result = await session.execute(stmt)
        s3_paths = result.scalar_one_or_none()
        
        if not s3_paths:
            logger.error(f"Report with ID {id} not found")
            return {
                "success": False,
                "error": f"Report with ID {id} not found"
            }
        
        s3_path = s3_paths.get(file_type)
        logger.info(f"S3 path for file with ID {id} and file_type {file_type}: {s3_path}")
        return {
            "success": True,
            "s3_uri": s3_path
        }
        
    except SQLAlchemyError as e:
        logger.error(f"Database error retrieving S3 path: {str(e)}", exc_info=True)
        return {
            "success": False,
            "error": f"Database error: {str(e)}"
        }
    
    except Exception as e:
        logger.error(f"Unexpected error retrieving S3 path: {str(e)}", exc_info=True)
        return {
            "success": False,
            "error": f"Unexpected error: {str(e)}"
        }
    

    
async def check_user_exists_by_email(email: str, session: AsyncSession) -> Dict[str, Any]:
    """
    Check if a user exists in the database by their email.
    
    Args:
        email (str): The email to check
        session (AsyncSession): The SQLAlchemy async session
            
    Returns:
        Dict[str, Any]: Dictionary with success status and user details if found
    """
    logger.info(f"Checking if user exists with email: {email}")

    if not email:
        logger.error("Email is not provided")
        return {
            "success": False,
            "error": "Email is required"
        }

    try:
        # Query to get user details
        stmt = select(User).where(User.email == email)
        result = await session.execute(stmt)
        user = result.scalar_one_or_none()
        
        if user:
            logger.info(f"User found with email: {email}")
            return {
                "success": True,
                "exists": True,
                "is_google_verified": user.is_google_verified,
                "user_id": user.id,
                "user_name": user.user_name,
                "email": user.email,
                "phone": user.phone,
                "phone_country_code": user.phone_country_code,
                "created_at": user.created_at if user.created_at else None,
                "password_hash": user.password_hash if user.password_hash else None,
                "is_verified": user.is_verified,
                "verified_at": user.verified_at if user.verified_at else None,
                "t_c_verified": user.t_c_verified,
                "onboarding_completed": user.onboarding_completed,
            }
        
        logger.warning(f"User with email {email} not found")
        return {
            "success": True,
            "exists": False
        }
    
    except SQLAlchemyError as e:
        logger.error(f"Database error checking user existence: {str(e)}", exc_info=True)
        return {
            "success": False,
            "error": f"Database error: {str(e)}"
        }
    
    except Exception as e:
        logger.error(f"Unexpected error checking user existence: {str(e)}", exc_info=True)
        return {
            "success": False,
            "error": f"Unexpected error: {str(e)}"
        }

async def update_user_password(user_id: str, password_hash: str, session: AsyncSession) -> Dict[str, Any]:
    """
    Update user password in the database.
    
    Args:
        user_id (str): The user ID to update
        password_hash (str): The new hashed password
        session (AsyncSession): The SQLAlchemy async session
            
    Returns:
        Dict[str, Any]: Dictionary with success status and error if any
    """
    logger.info(f"Updating password for user_id: {user_id}")
    
    if not user_id:
        logger.error("Invalid user_id: empty value provided")
        return {
            "success": False,
            "error": "Invalid user ID: empty value provided"
        }
    
    if not password_hash:
        logger.error("Invalid password_hash: empty value provided")
        return {
            "success": False,
            "error": "Invalid password hash: empty value provided"
        }
    
    try:
        # Update user password
        stmt = update(User).where(User.id == user_id).values(password_hash=password_hash)
        result = await session.execute(stmt)
        
        if result.rowcount == 0:
            logger.error(f"User with ID {user_id} not found")
            return {
                "success": False,
                "error": f"User with ID {user_id} not found"
            }
        
        await session.commit()
        logger.info(f"Password updated successfully for user_id: {user_id}")
        return {
            "success": True
        }
    
    except SQLAlchemyError as e:
        logger.error(f"Database error updating user password: {str(e)}", exc_info=True)
        await session.rollback()
        return {
            "success": False,
            "error": f"Database error: {str(e)}"
        }
    
    except Exception as e:
        logger.error(f"Unexpected error updating user password: {str(e)}", exc_info=True)
        await session.rollback()
        return {
            "success": False,
            "error": f"Unexpected error: {str(e)}"
        }

async def update_user_verification_status(email: str, verified_at: datetime, is_verified:bool, is_google_verified:bool, session: AsyncSession) -> Dict[str, Any]:
    """
    Update user verification status in the database.
    
    Args:
        email (str): The email of the user to update
        verified_at (datetime): The timestamp when the user was verified
        session (AsyncSession): The SQLAlchemy async session
            
    Returns:
        Dict[str, Any]: Dictionary with success status and error if any
    """
    logger.info(f"Updating verification status for email: {email}")
    
    if not email:
        logger.error("Invalid email: empty value provided")
        return {
            "success": False,
            "error": "Invalid email: empty value provided"
        }
    
    try:
        # First get the user to retrieve the user_id
        stmt_select = select(User).where(User.email == email)
        result_select = await session.execute(stmt_select)
        user = result_select.scalar_one_or_none()
        
        if not user:
            logger.error(f"User with email {email} not found")
            return {
                "success": False,
                "error": f"User with email {email} not found"
            }
        
        # Update user verification status
        stmt = update(User).where(User.email == email).values(is_verified=is_verified, verified_at=verified_at, is_google_verified=is_google_verified)
        result = await session.execute(stmt)
        
        if result.rowcount == 0:
            logger.error(f"User with email {email} not found or couldn't be updated")
            return {
                "success": False,
                "error": f"User with email {email} not found or couldn't be updated"
            }
        
        await session.commit()
        logger.info(f"Verification status updated successfully for email: {email}")

        return {
            "success": True,
            "user_id": user.id
        }
    
    except SQLAlchemyError as e:
        logger.error(f"Database error updating user verification status: {str(e)}", exc_info=True)
        await session.rollback()
        return {
            "success": False,
            "error": f"Database error: {str(e)}"
        }

async def update_user_tc_verified(user_id: str, t_c_verified: bool, session: AsyncSession) -> Dict[str, Any]:
    """
    Update user Terms and Conditions verification status in the database.
    
    Args:
        user_id (str): The ID of the user to update
        t_c_verified (bool): The T&C verification status
        session (AsyncSession): The SQLAlchemy async session
            
    Returns:
        Dict[str, Any]: Dictionary with success status and error if any
    """
    logger.info(f"Updating T&C verification status for user_id: {user_id} to {t_c_verified}")
    
    if not user_id:
        logger.error("Invalid user_id: empty value provided")
        return {
            "success": False,
            "error": "Invalid user_id: empty value provided"
        }
    
    try:
        # Check if user exists
        stmt_select = select(User).where(User.id == user_id)
        result_select = await session.execute(stmt_select)
        user = result_select.scalar_one_or_none()
        
        if not user:
            logger.error(f"User with ID {user_id} not found")
            return {
                "success": False,
                "error": f"User not found"
            }
        
        # Update t_c_verified status
        stmt = update(User).where(User.id == user_id).values(t_c_verified=t_c_verified)
        result = await session.execute(stmt)
        
        if result.rowcount == 0:
            logger.error(f"User with ID {user_id} not found or couldn't be updated")
            return {
                "success": False,
                "error": f"User not found or couldn't be updated"
            }
        
        await session.commit()
        logger.info(f"T&C verification status updated successfully for user_id: {user_id}")

        return {
            "success": True,
            "t_c_verified": t_c_verified
        }
    
    except SQLAlchemyError as e:
        logger.error(f"Database error updating T&C verification status: {str(e)}", exc_info=True)
        await session.rollback()
        return {
            "success": False,
            "error": f"Database error: {str(e)}"
        }
    except Exception as e:
        logger.error(f"Unexpected error updating T&C verification status: {str(e)}", exc_info=True)
        await session.rollback()
        return {
            "success": False,
            "error": f"Unexpected error: {str(e)}"
        }
    
    except Exception as e:
        logger.error(f"Unexpected error updating user verification status: {str(e)}", exc_info=True)
        await session.rollback()
        return {
            "success": False,
            "error": f"Unexpected error: {str(e)}"
        }


async def get_user_walkover_status(user_id: str, session: AsyncSession) -> Dict[str, Any]:
    """Fetch the ``walkover_completed`` flag for a user.

    Args:
        user_id: ID of the user whose flag should be read.
        session: SQLAlchemy async session.

    Returns:
        A dict with the keys:
            - ``success`` (bool): Whether the read completed without error.
            - ``exists`` (bool): Whether the user was found.
            - ``walkover_completed`` (bool, optional): Current value of the flag.
            - ``error`` (str, optional): Error message when ``success`` is False
              or the user was not found.
    """
    if not user_id:
        logger.error("Invalid user_id: empty value provided")
        return {
            "success": False,
            "exists": False,
            "error": "Invalid user ID: empty value provided",
        }

    logger.info(f"Fetching walkover_completed for user_id: {user_id}")

    try:
        stmt = select(User.walkover_completed).where(User.id == user_id)
        result = await session.execute(stmt)
        row = result.first()

        if row is None:
            logger.warning(f"User with ID {user_id} not found while fetching walkover_completed")
            return {
                "success": True,
                "exists": False,
                "error": "User not found",
            }

        walkover_completed = bool(row[0])
        logger.info(f"walkover_completed={walkover_completed} for user_id: {user_id}")
        return {
            "success": True,
            "exists": True,
            "walkover_completed": walkover_completed,
        }

    except SQLAlchemyError as e:
        logger.error(f"Database error fetching walkover_completed: {str(e)}", exc_info=True)
        return {
            "success": False,
            "exists": False,
            "error": f"Database error: {str(e)}",
        }
    except Exception as e:
        logger.error(f"Unexpected error fetching walkover_completed: {str(e)}", exc_info=True)
        return {
            "success": False,
            "exists": False,
            "error": f"Unexpected error: {str(e)}",
        }


async def mark_walkover_completed(
    user_id: str,
    session: AsyncSession,
) -> Dict[str, Any]:
    """Mark the user's walkover as completed.

    Idempotent: always writes ``walkover_completed = True`` and commits within
    the active session. Safe to call repeatedly.

    Args:
        user_id: ID of the user to update.
        session: SQLAlchemy async session.

    Returns:
        A dict with the keys:
            - ``success`` (bool): Whether the update succeeded.
            - ``walkover_completed`` (bool, optional): The persisted value.
            - ``error`` (str, optional): Error message when ``success`` is False.
    """
    if not user_id:
        logger.error("Invalid user_id: empty value provided")
        return {
            "success": False,
            "error": "Invalid user ID: empty value provided",
        }

    logger.info(f"Marking walkover_completed=True for user_id: {user_id}")

    try:
        stmt = (
            update(User)
            .where(User.id == user_id)
            .values(walkover_completed=True)
        )
        result = await session.execute(stmt)

        if result.rowcount == 0:
            logger.error(f"User with ID {user_id} not found while updating walkover_completed")
            return {
                "success": False,
                "error": "User not found",
            }

        await session.commit()
        logger.info(f"walkover_completed marked True for user_id: {user_id}")
        return {
            "success": True,
            "walkover_completed": True,
        }

    except SQLAlchemyError as e:
        logger.error(f"Database error updating walkover_completed: {str(e)}", exc_info=True)
        await session.rollback()
        return {
            "success": False,
            "error": f"Database error: {str(e)}",
        }
    except Exception as e:
        logger.error(f"Unexpected error updating walkover_completed: {str(e)}", exc_info=True)
        await session.rollback()
        return {
            "success": False,
            "error": f"Unexpected error: {str(e)}",
        }


async def create_report(
    report_id: str,
    chat_id: str,
    created_at: datetime,
    s3_uri: dict,
    session: AsyncSession
) -> Dict[str, Any]:
    """
    Create a new report with minimal required fields and initial version.

    Args:
        report_id (str): Unique identifier for the report.
        chat_id (str): Chat identifier.
        created_at (datetime): Creation timestamp.
        s3_uri (Dict, optional): S3 URIs to the report files in JSON format. Defaults to {}.
        session (AsyncSession, optional): SQLAlchemy async session.

    Returns:
        Dict[str, Any]: Dictionary with success status, report_id, and version_id.
    """
    logger.info("Creating new report with ID: %s", report_id)

    # --- Validation ---
    missing_fields = [
        field for field, value in {
            "report_id": report_id,
            "chat_id": chat_id,
            "created_at": created_at
        }.items() if not value
    ]
    if missing_fields:
        error_msg = f"Missing required fields: {', '.join(missing_fields)}"
        logger.error(error_msg)
        return {"success": False, "error": error_msg}


    try:
        # Create base report
        new_report = Report(
            id=report_id,
            chat_id=chat_id,
            created_at=created_at,
            current_version=1,
            status=ReportStatus.DRAFT.value,
            last_activity_at=created_at,
            s3_uri={"md": None, "pdf": None, "html": None, "pptx": None, "info_pdf": None, "md_explicit": False, "html_explicit": False}
        )
        session.add(new_report)
        await session.flush()  # Flush to get the report_id
        
        # Create initial version (version 1)
        initial_version = ReportVersion(
            id=str(uuid7()),
            report_id=report_id,
            version=1,
            s3_uri={**(s3_uri or {}), "md_explicit": False, "html_explicit": False},
            is_active=True,
            status=ReportStatus.DRAFT.value,
            last_activity_at=created_at,
            created_at=created_at
        )
        session.add(initial_version)
        
        # Create refinement history entry with null refine_history
        refinement_history = RefinementHistory(
            id=str(uuid7()),
            report_id=report_id,
            refine_history=None
        )
        session.add(refinement_history)
        
        await session.commit()

        logger.info("Successfully created report with ID: %s, initial version, and refinement history", report_id)
        return {
            "success": True, 
            "report_id": report_id,
            "version_id": initial_version.id,
            "version": 1
        }

    except SQLAlchemyError as e:
        await session.rollback()
        error_msg = f"Database error: {str(e)}"
        logger.error(error_msg, exc_info=True)
        return {"success": False, "error": error_msg}

    except Exception as e:
        await session.rollback()
        error_msg = f"Unexpected error: {str(e)}"
        logger.error(error_msg, exc_info=True)
        return {"success": False, "error": error_msg}
    
async def update_report(report_id: str, update_data: Dict[str, Any], session: AsyncSession) -> Dict[str, Any]:
    """
    Update an existing report with the provided fields.
    
    Args:
        report_id (str): ID of the report to update
        update_data (Dict[str, Any]): Dictionary containing fields to update
        session (AsyncSession): SQLAlchemy async session
            
    Returns:
        Dict[str, Any]: Dictionary with success status
    """
    # logger.info(f"Updating report with ID: {report_id}")
    
    # if not report_id:
    #     logger.error("Missing required field: report_id")
    #     return {
    #         "success": False,
    #         "error": "Missing required field: report_id"
    #     }
    
    # if not update_data:
    #     logger.warning(f"No update data provided for report: {report_id}")
    #     return {
    #         "success": True,
    #         "message": "No changes to apply"
    #     }
    
    # try:
    #     # First check if report exists
    #     stmt = select(Report).where(Report.id == report_id)
    #     result = await session.execute(stmt)
    #     report = result.scalar_one_or_none()
        
    #     if not report:
    #         logger.error(f"Report with ID {report_id} not found")
    #         return {
    #             "success": False,
    #             "error": f"Report with ID {report_id} not found"
    #         }
        
    #     # Update only the fields provided in update_data
    #     for field, value in update_data.items():
    #         if hasattr(report, field):
    #             setattr(report, field, value)
    #         else:
    #             logger.warning(f"Ignoring unknown field: {field}")
        
    #     await session.commit()
        
    #     logger.info(f"Successfully updated report with ID: {report_id}")
    #     return {
    #         "success": True,
    #         "report_id": report_id
    #     }

    logger.info(f"Updating report with ID: {report_id}")
    
    if not report_id:
        logger.error("Missing required field: report_id")
        return {
            "success": False,
            "error": "Missing required field: report_id"
        }
    
    if not update_data:
        logger.warning(f"No update data provided for report: {report_id}")
        return {
            "success": True,
            "message": "No changes to apply"
        }
    
    try:
        # First check if report exists
        stmt = select(Report).where(Report.id == report_id)
        result = await session.execute(stmt)
        report = result.scalar_one_or_none()
        
        if not report:
            logger.error(f"Report with ID {report_id} not found")
            return {
                "success": False,
                "error": f"Report with ID {report_id} not found"
            }
        
        # Get active version
        active_version_stmt = select(ReportVersion).where(
            ReportVersion.report_id == report_id,
            ReportVersion.is_active == True
        )
        active_version_result = await session.execute(active_version_stmt)
        active_version = active_version_result.scalar_one_or_none()
        
        if not active_version:
            logger.error(f"No active version found for report {report_id}")
            return {
                "success": False,
                "error": f"No active version found for report {report_id}"
            }
        
        # Handle s3_uri - update in active version, not report
        if "s3_uri" in update_data:
            incoming = update_data.pop("s3_uri") or {}
            existing = active_version.s3_uri or {}

            # Always apply incoming values
            merged = {**existing, **incoming}

            # Ensure all required keys exist
            for required_key in ("md", "html", "pdf", "pptx", "info_pdf"):
                if required_key not in merged:
                    merged[required_key] = None

            active_version.s3_uri = dict(merged)
            report.s3_uri = dict(merged)
        
        # Handle status update - update both report and active version
        if "status" in update_data:
            status = update_data.pop("status")
            report.status = status
            active_version.status = status
        
        # Handle generated_at - update in active version
        if "generated_at" in update_data:
            generated_at = update_data.pop("generated_at")
            active_version.generated_at = generated_at
            report.generated_at = generated_at
        
        # Handle poster_image_url - update in active version
        if "poster_image_url" in update_data:
            poster_url = update_data.pop("poster_image_url")
            active_version.poster_image_url = poster_url
            report.poster_image_url = poster_url
        
        # Update last_activity_at in both
        if "last_activity_at" in update_data or "status" in locals():
            timestamp = update_data.pop("last_activity_at", datetime.now(timezone.utc))
            report.last_activity_at = timestamp
            active_version.last_activity_at = timestamp
        
        # Update other Report-level fields (title, layout, etc.)
        report_fields = ['title', 'layout', 'length', 'summary', 'citations', 'current_version', 'file_id', 'initial_markdown', 'domain_name', 'report_type']
        title_updated = False
        new_title = None
        
        for field, value in update_data.items():
            if field in report_fields and hasattr(report, field):
                setattr(report, field, value)
                # Track if title was updated
                if field == 'title':
                    title_updated = True
                    new_title = value
            else:
                logger.warning(f"Ignoring unknown field: {field}")
        
        # If title was updated, also update the chat title
        if title_updated and new_title:
            try:
                chat_stmt = select(Message).where(Message.id == report.chat_id)
                chat_result = await session.execute(chat_stmt)
                chat = chat_result.scalar_one_or_none()
                
                if chat:
                    chat.chat_title = new_title
                    chat.updated_at = datetime.now(timezone.utc)
                    logger.info(f"Updated chat title to '{new_title}' for chat_id: {report.chat_id}")
                else:
                    logger.warning(f"Chat not found with id: {report.chat_id}, could not update chat title")
            except Exception as e:
                logger.error(f"Error updating chat title: {e}", exc_info=True)
                # Don't fail the entire update if chat title update fails
        
        await session.commit()
        
        logger.info(f"Successfully updated report with ID: {report_id}")
        return {
            "success": True,
            "report_id": report_id
        }
        
    except SQLAlchemyError as e:
        logger.error(f"Database error updating report: {str(e)}", exc_info=True)
        await session.rollback()
        return {
            "success": False,
            "error": f"Database error: {str(e)}"
        }
    except Exception as e:
        logger.error(f"Unexpected error updating report: {str(e)}", exc_info=True)
        await session.rollback()
        return {
            "success": False,
            "error": f"Unexpected error: {str(e)}"
        }


async def update_specific_version_s3_uri(
    report_id: str,
    version_id: str,
    s3_uri_updates: Dict[str, Any],
    generated_at: datetime = None,
    session: AsyncSession = None
) -> Dict[str, Any]:
    """
    Update s3_uri for a SPECIFIC version (not necessarily the active one).
    Use this when generating outputs for old versions to avoid updating the Report table
    which should always reflect the current version's data.
    
    Args:
        report_id: Report ID for validation
        version_id: Specific version ID to update
        s3_uri_updates: Dict with s3_uri fields to update (e.g., {"pdf": "s3://...", "md": "s3://..."})
        generated_at: Optional timestamp for when PDF/HTML/MD was generated
        session: Database session
    
    Returns:
        Dict with success status and updated s3_uri
    """
    try:
        # Get the specific version
        stmt = select(ReportVersion).where(
            ReportVersion.id == version_id,
            ReportVersion.report_id == report_id
        )
        result = await session.execute(stmt)
        version = result.scalar_one_or_none()
        
        if not version:
            logger.error(f"Version {version_id} not found for report {report_id}")
            return {"success": False, "error": f"Version {version_id} not found for report {report_id}"}
        
        # Merge with existing s3_uri
        existing = version.s3_uri or {}
        merged = {**existing, **s3_uri_updates}
        
        # Ensure all required keys exist
        for required_key in ("md", "html", "pdf", "pptx", "info_pdf"):
            if required_key not in merged:
                merged[required_key] = None
        
        version.s3_uri = dict(merged)
        version.last_activity_at = datetime.now(timezone.utc)
        
        # Update generated_at if provided (for PDF/HTML/MD generation)
        if generated_at:
            version.generated_at = generated_at
        
        await session.commit()
        
        logger.info(f"Updated s3_uri for specific version {version_id} (v{version.version}) - NOT updating Report table")
        return {"success": True, "s3_uri": merged, "version": version.version}
        
    except SQLAlchemyError as e:
        logger.error(f"Database error updating specific version s3_uri: {str(e)}", exc_info=True)
        await session.rollback()
        return {"success": False, "error": f"Database error: {str(e)}"}
    except Exception as e:
        logger.error(f"Unexpected error updating specific version s3_uri: {str(e)}", exc_info=True)
        await session.rollback()
        return {"success": False, "error": str(e)}


async def update_report_status_by_chat_or_report_id(
    chat_id: str = None,
    report_id: str = None,
    status: str = None,
    session: AsyncSession = None
) -> Dict[str, Any]:
    """
    Update report status using either chat_id or report_id.
    
    Args:
        chat_id: Chat ID to find the report
        report_id: Report ID (if known)
        status: New status value
        session: Database session
        
    Returns:
        Dict with success status
    """
    if not status:
        return {"success": False, "error": "Status is required"}
    
    if not chat_id and not report_id:
        return {"success": False, "error": "Either chat_id or report_id is required"}
    
    try:
        # If report_id is provided, use it directly
        if report_id:
            target_report_id = report_id
        else:
            # Find report by chat_id
            stmt = select(Report).where(Report.chat_id == chat_id)
            result = await session.execute(stmt)
            report = result.scalar_one_or_none()
            
            if not report:
                logger.warning(f"No report found for chat_id: {chat_id}")
                return {"success": False, "error": f"No report found for chat_id: {chat_id}"}
            
            target_report_id = report.id
        
        # Update the report status
        update_data = {
            "status": status,
            "last_activity_at": datetime.now(timezone.utc)
        }
        
        return await update_report(report_id=target_report_id, update_data=update_data, session=session)
        
    except Exception as e:
        logger.error(f"Error updating report status: {str(e)}", exc_info=True)
        return {"success": False, "error": str(e)}


async def update_report_status_if_needed(
    report_id: str,
    new_status: str,
    session: AsyncSession
) -> Dict[str, Any]:
    """
    Update report status only if it's different from the current status.
    This optimization avoids unnecessary database writes.
    
    Args:
        report_id: Report ID
        new_status: New status value
        session: Database session
        
    Returns:
        Dict with success status and whether update was performed
    """
    try:
        # First, fetch the current status
        stmt = select(Report.status).where(Report.id == report_id)
        result = await session.execute(stmt)
        current_status = result.scalar_one_or_none()
        
        if current_status is None:
            logger.warning(f"Report not found: {report_id}")
            return {"success": False, "error": "Report not found"}
        
        # Always update last_activity_at to reflect user activity,
        # but only update status if it's actually different.
        now = datetime.now(timezone.utc)
        
        if current_status == new_status:
            # Status unchanged — still update last_activity_at to track activity
            logger.debug(f"Status already {new_status} for report {report_id}, updating last_activity_at only")
            update_data = {"last_activity_at": now}
            result = await update_report(report_id=report_id, update_data=update_data, session=session)
            if result.get("success"):
                result["updated"] = False
                result["message"] = "Status already set, last_activity_at updated"
            return result
        
        # Status is different, perform full update
        update_data = {
            "status": new_status,
            "last_activity_at": now
        }
        
        result = await update_report(report_id=report_id, update_data=update_data, session=session)
        if result.get("success"):
            result["updated"] = True
        return result
        
    except Exception as e:
        logger.error(f"Error checking/updating report status: {str(e)}", exc_info=True)
        return {"success": False, "error": str(e)}

    
async def get_report_details(report_id: str, session: AsyncSession) -> Dict[str, Any]:
    """
    Get detailed information about a specific report.
    
    Args:
        report_id (str): ID of the report to retrieve
        session (AsyncSession): SQLAlchemy async session
            
    Returns:
        Dict[str, Any]: Dictionary with success status and report details
    """
    logger.info(f"Getting details for report with ID: {report_id}")
    
    if not report_id:
        logger.error("Missing required field: report_id")
        return {
            "success": False,
            "error": "Missing required field: report_id"
        }
    
    try:
        # Query to get report details
        stmt = select(Report).where(Report.id == report_id)
        result = await session.execute(stmt)
        report = result.scalar_one_or_none()
        
        if not report:
            logger.warning(f"Report with ID {report_id} not found")
            return {
                "success": True,
                "report": None
            }
        
        # Convert report to dictionary
        report_data = {
            "id": report.id,
            "chat_id": report.chat_id,
            "s3_uri": report.s3_uri,
            "created_at": report.created_at.isoformat() if report.created_at else None,
            "title": report.title,
            "layout": report.layout,
            "length": report.length,
            "summary": report.summary,
            "citations": report.citations,
            "file_id": report.file_id,
            "initial_markdown": report.initial_markdown,
            "report_type": report.report_type,
        }
        
        logger.info(f"Successfully retrieved details for report with ID: {report_id}")
        return {
            "success": True,
            "report": report_data
        }
        
    except SQLAlchemyError as e:
        logger.error(f"Database error retrieving report details: {str(e)}", exc_info=True)
        return {
            "success": False,
            "error": f"Database error: {str(e)}"
        }
    except Exception as e:
        logger.error(f"Unexpected error retrieving report details: {str(e)}", exc_info=True)
        return {
            "success": False,
            "error": f"Unexpected error: {str(e)}"
        }


async def verify_report_ownership(report_id: str, user_id: str, session: AsyncSession) -> Dict[str, Any]:
    """
    Verify that a report exists and the requesting user owns it.

    Fetches report details, then checks ownership by verifying the report's
    chat_id corresponds to a Message owned by the given user_id.

    Args:
        report_id (str): ID of the report to verify.
        user_id (str): ID of the user requesting access.
        session (AsyncSession): SQLAlchemy async session.

    Returns:
        Dict[str, Any]: {
            "authorized": True,  "report": <report_dict>
        } on success, or {
            "authorized": False, "status_code": 404|401|500,
            "error": "<message>"
        } on failure.
    """
    try:
        report_result = await get_report_details(report_id=report_id, session=session)

        if not report_result or not report_result.get("success", False):
            error_msg = report_result.get("error", "Failed to retrieve report details.") if report_result else "Failed to retrieve report details."
            logger.error(f"Failed to get report details for report_id: {report_id}, error: {error_msg}")
            return {"authorized": False, "status_code": 500, "error": "Something unexpected happened. Please try again later."}

        if not report_result.get("report"):
            logger.warning(f"Report not found: {report_id} for user: {user_id}")
            return {"authorized": False, "status_code": 404, "error": "Report not found. Please check the report ID and try again."}

        report_data = report_result["report"]
        chat_id = report_data.get("chat_id")

        if not chat_id:
            logger.error(f"Report {report_id} has no associated chat_id")
            return {"authorized": False, "status_code": 500, "error": "Something unexpected happened. Please try again later."}

        chat_stmt = select(Message).where(
            Message.id == chat_id,
            Message.user_id == user_id,
            Message.is_deleted == False  # noqa: E712  — SQLAlchemy filter expression
        )
        chat_result = await session.execute(chat_stmt)
        chat = chat_result.scalar_one_or_none()

        if not chat:
            logger.warning(f"Unauthorized access attempt to report {report_id} by user {user_id}")
            return {"authorized": False, "status_code": 401, "error": "You don't have permission to access this report."}

        return {"authorized": True, "report": report_data}

    except SQLAlchemyError as e:
        logger.error(f"Database error verifying report ownership for report_id: {report_id}: {str(e)}", exc_info=True)
        return {"authorized": False, "status_code": 500, "error": "Something unexpected happened. Please try again later."}
    except Exception as e:
        logger.error(f"Unexpected error verifying report ownership for report_id: {report_id}: {str(e)}", exc_info=True)
        return {"authorized": False, "status_code": 500, "error": "Something unexpected happened. Please try again later."}

    
async def insert_table(session: AsyncSession, card_id: str, table_data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Insert a new table for a card.
    
    Args:
        session (AsyncSession): SQLAlchemy async session
        card_id (str): Business identifier for the card (card_id field from cards table)
        table_data (Dict[str, Any]): Dictionary containing table data with keys:
            - table_id: Business identifier for the table (required) - this can be reused across cards
            - table_title: Title of the table (optional)
            - table_markdown: Markdown representation of the table (optional)
            - visualization: Base64 representation of the visualization image (optional)
            - report_id: ID of the associated report (required)
            
    Returns:
        Dict[str, Any]: Dictionary with success status and table_id (the new primary key)
        
    Note:
        - The function generates a new unique UUID for the primary key 'id' field
        - The 'table_id' field stores the business identifier which can be reused
        - This prevents primary key conflicts when the same table_id is used in different cards
        - Uses parent_card_id to link to the card's primary key
    """
    logger.info(f"Creating new table for card: {card_id}")
    
    # Validate required fields
    if not card_id:
        logger.error("Missing required field: card_id")
        return {
            "success": False,
            "error": "Missing required field: card_id"
        }
    
    if not table_data.get('table_id'):
        logger.error("Missing required field: table_id")
        return {
            "success": False,
            "error": "Missing required field: table_id"
        }
    
    if not table_data.get('report_id'):
        logger.error("Missing required field: report_id")
        return {
            "success": False,
            "error": "Missing required field: report_id"
        }
    
    try:
        # Generate a new unique UUID for the primary key
        id = str(uuid7())
        table_id = table_data.get('table_id')
        
        logger.info(f"Creating table record - Primary Key ID: {id}, Business Table ID: {table_id}")
        
        # Get the active card's primary key ID using the business card_id
        parent_stmt = select(Card.id).where(
            Card.card_id == card_id,
            Card.is_active == True
        )
        parent_stmt_result = await session.execute(parent_stmt)
        parent_result = parent_stmt_result.scalar_one_or_none()
        
        if not parent_result:
            logger.error(f"No active card found with business card_id: {card_id}")
            return {
                "success": False,
                "error": f"No active card found with business card_id: {card_id}"
            }
        
        parent_card_id = parent_result
        
        table_title = table_data.get('table_title')
        if isinstance(table_title, tuple):
            # Defensive fallback for malformed payloads where (title, table_markdown) is passed.
            table_title = table_title[0] if table_title else ""
        elif table_title is None:
            table_title = ""
        elif not isinstance(table_title, str):
            table_title = str(table_title)
        
        table_markdown = table_data.get('table_markdown')
        if table_markdown is None:
            table_markdown = ""
        elif not isinstance(table_markdown, str):
            table_markdown = str(table_markdown)
        
        new_table = Table(
            id=id,
            report_id=table_data.get('report_id'),
            parent_card_id=parent_card_id,  # Use the primary key ID from the active card
            table_id=table_id,
            table_title=table_title,
            table_markdown=table_markdown,
            base64_s3_uri=table_data.get('visualization')  # Store base64 directly
        )
        
        session.add(new_table)
        await session.commit()
        
        logger.info(f"Successfully created table with ID: {id}")
        return {
            "success": True,
            "table_id": id,
            "base64_visualization": table_data.get('visualization')
        }
        
    except SQLAlchemyError as e:
        logger.error(f"Database error creating table: {str(e)}", exc_info=True)
        await session.rollback()
        return {
            "success": False,
            "error": f"Database error: {str(e)}"
        }
    except Exception as e:
        logger.error(f"Unexpected error creating table: {str(e)}", exc_info=True)
        await session.rollback()
        return {
            "success": False,
            "error": f"Unexpected error: {str(e)}"
        }

async def insert_card(session: AsyncSession, report_id: str, report_card: Dict[str, Any]) -> Dict[str, Any]:
    """
    Insert a new card for a report with versioning support.
    
    Args:
        session (AsyncSession): SQLAlchemy async session
        report_id (str): ID of the associated report
        report_card (Dict[str, Any]): Dictionary containing card data with keys:
            - id: Card identifier (required) - this links to the original card concept
            - title: Title of the card (required)
            - sequence: Sequence number to determine order (required)
            - content: Content of the card as text (required)
            - sub_sections: Sub-sections of the card in JSON format (optional)
            - citations: Citations or references for the card (optional)
            - created_at: Creation timestamp (optional, will use current time if not provided)
            - summary: Summary of the card content (optional)
            - type: Type of the card (title/subtitle/toc/section/viz) (required)
            - section: Array of section objects with name, content, and tables (new format)
            
    Returns:
        Dict[str, Any]: Dictionary with success status and card_id
    """
    logger.info(f"Creating new card for report: {report_id}")
    
    # Validate required fields
    required_fields = ['id','sequence', 'type']
    for field in required_fields:
        if field not in report_card or report_card[field] is None:
            logger.error(f"Missing required field: {field}")
            return {
                "success": False,
                "error": f"Missing required field: {field}"
            }
    
    # Sanitize JSONB fields to remove invalid Unicode characters (e.g., null bytes)
    # This prevents PostgreSQL encoding errors during JSONB to bytes conversion
    report_card = await run_in_threadpool(sanitize_card_data, report_card)
    logger.info(f"Sanitized JSONB fields for card_id: {report_card.get('id')}")
    
    # For backward compatibility, check if we have the new section format
    has_section_format = 'section' in report_card and isinstance(report_card['section'], list)
    
    # If using new format, extract title and content from section
    title = report_card.get('title')
    content = report_card.get('content')
    
    if has_section_format and report_card['section']:
        # Extract title and content from first section
        first_section = report_card['section'][0]
        if not title and 'name' in first_section:
            title = first_section.get('name')
        # For new JSONB structure, use the entire section as content
        if not content:
            content = first_section  # Use the entire section structure as content
    
    try:
        # Check if this card_id already exists to determine version
        # Use the 'id' field from report_card as the business card_id for versioning
        card_id = report_card['id']
        existing_cards_stmt = select(Card).where(Card.card_id == card_id)
        existing_cards_result = await session.execute(existing_cards_stmt)
        existing_cards = existing_cards_result.scalars().all()
        
        # Calculate version number using MAX(version) + 1 to avoid duplicate version numbers
        # This handles cases where user reverts and then refines (deleted versions don't cause duplicates)
        if existing_cards:
            version = max(card.version for card in existing_cards) + 1
        else:
            version = 1
        
        # Deactivate all previous versions of this card_id
        if existing_cards:
            logger.info(f"Deactivating {len(existing_cards)} previous versions of card_id: {card_id}")
            for existing_card in existing_cards:
                existing_card.is_active = False
            await session.flush()
        
        # Generate new primary key ID for this card instance
        new_card_id = str(uuid7())
        
        # Set created_at if not provided
        if 'created_at' not in report_card or not report_card['created_at']:
            report_card['created_at'] = datetime.now(timezone.utc)
        
        # Set empty citations if not provided
        if 'citations' not in report_card or report_card['citations'] is None:
            report_card['citations'] = []
            
        # Set empty sub_sections if not provided
        if 'sub_sections' not in report_card or report_card['sub_sections'] is None:
            report_card['sub_sections'] = []
            
        # Create a new card
        new_card = Card(
            id=new_card_id,  # New primary key for this card instance
            card_id=card_id,  # Business identifier linking to original card concept
            report_id=report_id,
            title=title,
            sequence=report_card['sequence'],
            content=content,
            sub_sections=report_card.get('sub_sections'),
            citations=report_card['citations'],
            created_at=report_card['created_at'],
            summary=report_card.get('summary'),
            type=report_card['type'],
            version=version,  # Set calculated version number
            is_active=True,   # This new version is active
            is_deleted=False,  # Not deleted
            changed_since_es=False,  # Initial cards haven't changed since ES (they're part of initial generation)
            last_es_version_used=None  # Will be set when ES is generated
        )
        
        session.add(new_card)
        await session.flush()
        
        # NOTE: Cards are NO LONGER automatically linked to report versions here
        # report_version_cards will be populated ONLY when:
        # 1. /generate-report is called for version 1 (first generation)
        # 2. /regenerate-report creates a new version (version 2+)
        # This ensures report_version_cards always reflects the actual cards used in generation
        
        await session.commit()
        
        logger.info(f"Successfully created card with ID: {new_card_id}, card_id: {card_id}, version: {version}")
        return {
            "success": True,
            "parent_card_id": new_card_id,
            "card_id": card_id,
            "version": version
        }
        
    except SQLAlchemyError as e:
        logger.error(f"Database error creating card: {str(e)}", exc_info=True)
        await session.rollback()
        return {
            "success": False,
            "error": f"Database error: {str(e)}"
        }
    except Exception as e:
        logger.error(f"Unexpected error creating card: {str(e)}", exc_info=True)
        await session.rollback()
        return {
            "success": False,
            "error": f"Unexpected error: {str(e)}"
        }

async def link_card_to_report_version(
    session: AsyncSession,
    report_version_id: str,
    card_primary_id: str,
    sequence: int,
    is_modified: bool = False
) -> Dict[str, Any]:
    """
    Link a card to a report version via ReportVersionCard junction table.
    
    Args:
        session (AsyncSession): SQLAlchemy async session
        report_version_id (str): ID of the report version
        card_primary_id (str): Primary key ID of the card (Card.id, not the business card_id)
        sequence (int): Position of card in this version
        is_modified (bool): Whether this card was modified in this version
        
    Returns:
        Dict[str, Any]: Dictionary with success status
    """
    try:
        mapping = ReportVersionCard(
            id=str(uuid7()),
            report_version_id=report_version_id,
            parent_card_id=card_primary_id,
            sequence=sequence,
            is_modified=is_modified,
            created_at=datetime.now(timezone.utc)
        )
        session.add(mapping)
        # Note: Do NOT flush here - let the parent transaction handle it
        # This ensures proper rollback if any subsequent operation fails
        
        logger.info(f"Linked card {card_primary_id} to report version {report_version_id} at sequence {sequence}")
        return {"success": True, "mapping_id": mapping.id}
        
    except Exception as e:
        logger.error(f"Error linking card to report version: {str(e)}", exc_info=True)
        return {"success": False, "error": str(e)}

async def finalize_report_version(
    session: AsyncSession,
    report_id: str,
    version_id: str = None
) -> Dict[str, Any]:
    """
    Finalize a report version by linking all active cards to it.
    Should ONLY be called when report is generated (PDF/HTML/PPTX created) for VERSION 1.
    For version 2+, use /regenerate-report which calls create_new_report_version.
    
    This ensures report_version_cards always reflects the ACTUAL cards used in generation.
    
    Args:
        session (AsyncSession): SQLAlchemy async session
        report_id (str): ID of the report
        version_id (str, optional): ID of the version to finalize. If None, uses active version.
        
    Returns:
        Dict[str, Any]: Dictionary with success status and number of cards linked
    """
    try:
        # Get the version to finalize
        if version_id:
            version_stmt = select(ReportVersion).where(ReportVersion.id == version_id)
        else:
            # Get active version
            version_stmt = select(ReportVersion).where(
                ReportVersion.report_id == report_id,
                ReportVersion.is_active == True
            )
        
        version_result = await session.execute(version_stmt)
        version = version_result.scalar_one_or_none()
        
        if not version:
            return {"success": False, "error": "Version not found"}
        
        # IMPORTANT: Only finalize version 1 here
        # Version 2+ should be finalized by create_new_report_version in /regenerate-report
        if version.version != 1:
            logger.warning(f"Attempted to finalize version {version.version} for report {report_id}. Only version 1 should be finalized via this function.")
            return {
                "success": False,
                "error": f"Version {version.version} should not be finalized here. Use /regenerate-report for version 2+"
            }
        
        # Check if this version is already finalized (has entries in report_version_cards)
        existing_mappings_stmt = select(ReportVersionCard).where(
            ReportVersionCard.report_version_id == version.id
        ).limit(1)
        existing_result = await session.execute(existing_mappings_stmt)
        existing_mapping = existing_result.scalar_one_or_none()
        
        if existing_mapping:
            logger.info(f"Version {version.id} already finalized, skipping")
            return {
                "success": True,
                "message": "Version already finalized",
                "cards_linked": 0,
                "already_finalized": True
            }
        
        # Get all active cards for this report (latest versions only)
        # Since is_active is already set to False for old versions, we just filter by is_active
        cards_stmt = (
            select(Card)
            .where(
                Card.report_id == report_id,
                Card.is_active == True,
                Card.is_deleted == False
            )
            .order_by(Card.sequence)
        )
        
        cards_result = await session.execute(cards_stmt)
        cards = cards_result.scalars().all()
        
        if not cards:
            logger.warning(f"No active cards found for report {report_id}")
            return {
                "success": False,
                "error": "No active cards found for this report"
            }
        
        # Link each card to the version
        for card in cards:
            mapping = ReportVersionCard(
                id=str(uuid7()),
                report_version_id=version.id,
                parent_card_id=card.id,  # Use the card's primary key
                sequence=card.sequence,
                is_modified=False,  # All cards in first generation are not "modified"
                created_at=datetime.now(timezone.utc)
            )
            session.add(mapping)
        
        # Update version's generated_at timestamp and status
        version.generated_at = datetime.now(timezone.utc)
        version.status = ReportStatus.OUTPUT_GENERATED.value
        
        await session.commit()
        
        logger.info(f"Finalized version {version.id} (v{version.version}) with {len(cards)} cards for report {report_id}")
        return {
            "success": True,
            "version_id": version.id,
            "version": version.version,
            "cards_linked": len(cards),
            "already_finalized": False
        }
        
    except Exception as e:
        logger.error(f"Error finalizing report version: {str(e)}", exc_info=True)
        return {"success": False, "error": str(e)}

async def create_new_report_version(
    session: AsyncSession,
    report_id: str,
    cards_data: list,
    previous_version_id: str = None
) -> Dict[str, Any]:
    """
    Create a new report version, reusing unchanged cards and creating new ones for modified cards.
    
    Args:
        session (AsyncSession): SQLAlchemy async session
        report_id (str): ID of the base report
        cards_data (list): List of card data dictionaries with structure:
            - For unchanged cards: {"card_id": "...", "sequence": N, "is_modified": False, "existing_card_pk_id": "..."}
            - For new/modified cards: complete card data with is_modified=True
        previous_version_id (str, optional): ID of previous version to copy from
        
    Returns:
        Dict[str, Any]: Dictionary with success status, new version details
    """
    logger.info(f"Creating new version for report {report_id}")
    
    try:
        # Get current report to determine next version number
        report_stmt = select(Report).where(Report.id == report_id)
        report_result = await session.execute(report_stmt)
        report = report_result.scalar_one_or_none()
        
        if not report:
            return {"success": False, "error": f"Report {report_id} not found"}
        
        next_version = report.current_version + 1
        
        # Deactivate previous active version
        if previous_version_id:
            prev_version_stmt = select(ReportVersion).where(ReportVersion.id == previous_version_id)
            prev_version_result = await session.execute(prev_version_stmt)
            prev_version = prev_version_result.scalar_one_or_none()
            if prev_version:
                prev_version.is_active = False
        else:
            # Deactivate all previous versions
            deactivate_stmt = (
                update(ReportVersion)
                .where(ReportVersion.report_id == report_id)
                .values(is_active=False)
            )
            await session.execute(deactivate_stmt)
        
        await session.flush()
        
        # Create new version
        new_version = ReportVersion(
            id=str(uuid7()),
            report_id=report_id,
            version=next_version,
            s3_uri={"md_explicit": False, "html_explicit": False},
            is_active=True,
            status=ReportStatus.ANALYSIS_COMPLETED.value,
            last_activity_at=datetime.now(timezone.utc),
            created_at=datetime.now(timezone.utc)
        )
        session.add(new_version)
        await session.flush()
        
        # Link cards to this version
        for card_info in cards_data:
            if card_info.get('is_modified', False):
                # Check if this modified card already exists (has pk_id from detect_modified_cards)
                if card_info.get('pk_id'):
                    # Modified card already exists - just reuse it (don't create duplicate!)
                    card_pk_id = card_info.get('pk_id')
                    logger.info(f"Reusing existing modified card with pk_id: {card_pk_id}")
                else:
                    # This is a truly new card - create it
                    card_result = await insert_card(
                        session=session,
                        report_id=report_id,
                        report_card=card_info
                    )
                    if not card_result.get('success'):
                        raise Exception(f"Failed to insert modified card: {card_result.get('error')}")
                    
                    card_pk_id = card_result.get('parent_card_id')
                    logger.info(f"Created new card with pk_id: {card_pk_id}")
            else:
                # Reuse existing unchanged card
                card_pk_id = card_info.get('existing_card_pk_id')
                if not card_pk_id:
                    logger.error(f"Missing existing_card_pk_id for unchanged card")
                    continue
            
            # Link card to version
            link_result = await link_card_to_report_version(
                session=session,
                report_version_id=new_version.id,
                card_primary_id=card_pk_id,
                sequence=card_info.get('sequence', 0),
                is_modified=card_info.get('is_modified', False)
            )
            
            if not link_result.get('success'):
                logger.warning(f"Failed to link card to version: {link_result.get('error')}")
        
        # Update report's current_version and last_activity_at
        report.current_version = next_version
        report.last_activity_at = datetime.now(timezone.utc)
        
        await session.commit()
        
        logger.info(f"Successfully created version {next_version} for report {report_id}")
        return {
            "success": True,
            "version_id": new_version.id,
            "version": next_version,
            "report_id": report_id
        }
        
    except Exception as e:
        await session.rollback()
        logger.error(f"Error creating new report version: {str(e)}", exc_info=True)
        return {"success": False, "error": str(e)}

async def detect_modified_cards(report_id: str, session: AsyncSession, generation_type: str = "report") -> Dict[str, Any]:
    """
    Automatically detect which cards have been modified since the last generation of ANY type.
    
    This function compares card updated_at timestamps with the MOST RECENT generation timestamp
    (from report, infographic, or presentation) to determine which cards are new/modified.
    
    Logic:
    - Find the most recent generation timestamp from: generated_at, info_pdf_generation_time, pptx_generation_time
    - Compare card timestamps against this most recent generation
    - If any card was modified after the most recent generation → new version needed
    
    If no generation has occurred yet (first time for this version), all cards are treated as "unchanged"
    since there's nothing to compare against.
    
    Note: updated_at/created_at is set when:
    - Card is created (created_at defaults to now, updated_at = server_default)
    - Card content is refined/edited (NEW card record created with created_at = now)
    - Visualization is refined or deleted (updated_at manually set)
    
    Args:
        report_id (str): ID of the report
        session (AsyncSession): SQLAlchemy async session
        generation_type (str): Type of generation requesting detection (for logging)
        
    Returns:
        Dict[str, Any]: Dictionary with:
            - success (bool): Operation status
            - modified_cards (list): Full card data for modified cards
            - unchanged_cards (list): Card info (id, pk_id, sequence) for unchanged cards
            - comparison_timestamp (datetime): The timestamp used for comparison
            - is_first_generation (bool): True if no generation has occurred for this version
    """
    logger.info(f"Detecting modified cards for report: {report_id}, requested by: {generation_type}")
    
    try:
        # Get the active report version
        version_stmt = select(ReportVersion).where(
            ReportVersion.report_id == report_id,
            ReportVersion.is_active == True
        )
        version_result = await session.execute(version_stmt)
        active_version = version_result.scalar_one_or_none()
        
        if not active_version:
            logger.error(f"No active version found for report {report_id}")
            return {
                "success": False,
                "error": "No active version found for this report"
            }
        
        # Get all active cards for this report first
        cards_stmt = select(Card).where(
            Card.report_id == report_id,
            Card.is_active == True
        ).order_by(Card.sequence)
        
        cards_result = await session.execute(cards_stmt)
        all_cards = cards_result.scalars().all()
        
        # Find the MOST RECENT generation timestamp from any output type
        # This ensures we detect modifications since the last time ANY output was generated
        timestamps = []
        s3_uri = active_version.s3_uri or {}
        
        # 1. Report PDF generation time (generated_at column)
        if active_version.generated_at:
            timestamps.append(('report_pdf', active_version.generated_at))
            logger.debug(f"Found generated_at: {active_version.generated_at}")
        
        # 2. MD generation time (md_generation_time in s3_uri)
        md_gen_time_str = s3_uri.get('md_generation_time')
        if md_gen_time_str:
            try:
                md_ts = datetime.fromisoformat(md_gen_time_str.replace('Z', '+00:00'))
                timestamps.append(('report_md', md_ts))
                logger.debug(f"Found md_generation_time: {md_ts}")
            except (ValueError, AttributeError):
                logger.warning(f"Could not parse md_generation_time: {md_gen_time_str}")
        
        # 3. HTML generation time (html_generation_time in s3_uri)
        html_gen_time_str = s3_uri.get('html_generation_time')
        if html_gen_time_str:
            try:
                html_ts = datetime.fromisoformat(html_gen_time_str.replace('Z', '+00:00'))
                timestamps.append(('report_html', html_ts))
                logger.debug(f"Found html_generation_time: {html_ts}")
            except (ValueError, AttributeError):
                logger.warning(f"Could not parse html_generation_time: {html_gen_time_str}")
        
        # 4. Infographic generation time (info_pdf_generation_time)
        info_pdf_gen_time_str = s3_uri.get('info_pdf_generation_time')
        if info_pdf_gen_time_str:
            try:
                info_ts = datetime.fromisoformat(info_pdf_gen_time_str.replace('Z', '+00:00'))
                timestamps.append(('infographic', info_ts))
                logger.debug(f"Found info_pdf_generation_time: {info_ts}")
            except (ValueError, AttributeError):
                logger.warning(f"Could not parse info_pdf_generation_time: {info_pdf_gen_time_str}")
        
        # 5. Presentation generation time (pptx_generation_time)
        pptx_gen_time_str = s3_uri.get('pptx_generation_time')
        if pptx_gen_time_str:
            try:
                pptx_ts = datetime.fromisoformat(pptx_gen_time_str.replace('Z', '+00:00'))
                timestamps.append(('presentation', pptx_ts))
                logger.debug(f"Found pptx_generation_time: {pptx_ts}")
            except (ValueError, AttributeError):
                logger.warning(f"Could not parse pptx_generation_time: {pptx_gen_time_str}")
        
        # Use the most recent timestamp for comparison
        comparison_timestamp = None
        comparison_source = None
        is_first_generation = False
        
        if timestamps:
            # Sort by timestamp descending and get the most recent
            timestamps.sort(key=lambda x: x[1], reverse=True)
            comparison_source, comparison_timestamp = timestamps[0]
            logger.info(f"Using most recent generation timestamp from {comparison_source}: {comparison_timestamp}")
        
        # If no comparison timestamp found, this is the first generation for this version
        if not comparison_timestamp:
            is_first_generation = True
            logger.info(f"No previous generation found for this version - first generation, treating all cards as unchanged")
            
            # All cards are "unchanged" for first generation
            unchanged_cards = []
            for card in all_cards:
                unchanged_cards.append({
                    "card_id": card.card_id,
                    "existing_card_pk_id": card.id,
                    "sequence": card.sequence,
                    "is_modified": False
                })
            
            return {
                "success": True,
                "modified_cards": [],
                "unchanged_cards": unchanged_cards,
                "comparison_timestamp": None,
                "total_cards": len(all_cards),
                "modified_count": 0,
                "unchanged_count": len(all_cards),
                "is_first_generation": True
            }
        
        modified_cards = []
        unchanged_cards = []
        
        for card in all_cards:
            # Use updated_at for comparison (falls back to created_at if not available for backward compatibility)
            last_modified = card.updated_at if hasattr(card, 'updated_at') and card.updated_at else card.created_at
            
            # Compare card last modification time with comparison timestamp
            if last_modified > comparison_timestamp:
                # Card was created/edited AFTER last generation → MODIFIED
                modified_cards.append({
                    "id": card.card_id,  # Business card_id
                    "pk_id": card.id,     # Primary key
                    "section": [card.content] if isinstance(card.content, dict) else card.content,
                    "sub_sections": card.sub_sections or [],
                    "citations": card.citations or {},
                    "summary": card.summary or "",
                    "type": card.type,
                    "sequence": card.sequence,
                    "is_modified": True
                })
                logger.info(f"Card {card.card_id} is MODIFIED (updated: {last_modified}, comparison: {comparison_timestamp})")
            else:
                # Card existed when last generation occurred → UNCHANGED
                unchanged_cards.append({
                    "card_id": card.card_id,
                    "existing_card_pk_id": card.id,
                    "sequence": card.sequence,
                    "is_modified": False
                })
                logger.debug(f"Card {card.card_id} is UNCHANGED (updated: {last_modified}, comparison: {comparison_timestamp})")
        
        logger.info(f"Detection complete for {generation_type}: {len(modified_cards)} modified, {len(unchanged_cards)} unchanged")
        
        return {
            "success": True,
            "modified_cards": modified_cards,
            "unchanged_cards": unchanged_cards,
            "comparison_timestamp": comparison_timestamp.isoformat() if comparison_timestamp else None,
            "total_cards": len(all_cards),
            "modified_count": len(modified_cards),
            "unchanged_count": len(unchanged_cards),
            "is_first_generation": False
        }
        
    except Exception as e:
        logger.error(f"Error detecting modified cards: {str(e)}", exc_info=True)
        return {"success": False, "error": str(e)}


async def get_active_report_version(report_id: str, session: AsyncSession) -> Dict[str, Any]:
    """
    Get the active version of a report with all its cards.
    
    Args:
        report_id (str): ID of the report
        session (AsyncSession): SQLAlchemy async session
        
    Returns:
        Dict[str, Any]: Dictionary with version info and cards
    """
    try:
        # Get active version
        version_stmt = select(ReportVersion).where(
            ReportVersion.report_id == report_id,
            ReportVersion.is_active == True
        )
        version_result = await session.execute(version_stmt)
        version = version_result.scalar_one_or_none()
        
        if not version:
            return {"success": True, "version": None, "cards": []}
        
        # Get cards for this version
        cards_stmt = (
            select(Card, ReportVersionCard.sequence, ReportVersionCard.is_modified)
            .join(ReportVersionCard, Card.id == ReportVersionCard.parent_card_id)
            .where(ReportVersionCard.report_version_id == version.id)
            .order_by(ReportVersionCard.sequence)
        )
        cards_result = await session.execute(cards_stmt)
        cards_data = cards_result.all()
        
        cards_list = []
        for card, sequence, is_modified in cards_data:
            cards_list.append({
                "id": card.id,
                "card_id": card.card_id,
                "title": card.title,
                "sequence": sequence,
                "content": card.content,
                "sub_sections": card.sub_sections,
                "citations": card.citations,
                "summary": card.summary,
                "type": card.type,
                "is_modified": is_modified,
                "version": card.version
            })
        
        return {
            "success": True,
            "version": {
                "id": version.id,
                "version": version.version,
                "status": version.status,
                "s3_uri": version.s3_uri,
                "generated_at": version.generated_at.isoformat() if version.generated_at else None,
                "last_activity_at": version.last_activity_at.isoformat() if version.last_activity_at else None
            },
            "cards": cards_list
        }
        
    except Exception as e:
        logger.error(f"Error getting active report version: {str(e)}", exc_info=True)
        return {"success": False, "error": str(e)}


async def get_tables_for_card(card_id: str, session: AsyncSession) -> Dict[str, Any]:
    """
    Get all tables associated with a specific card.
    
    Args:
        card_id (str): Business identifier for the card (card_id field from cards table)
        session (AsyncSession): SQLAlchemy async session
            
    Returns:
        Dict[str, Any]: Dictionary with success status and list of tables
    """
    logger.info(f"Getting tables for card with business card_id: {card_id}")
    
    if not card_id:
        logger.error("Missing required field: card_id")
        return {
            "success": False,
            "error": "Missing required field: card_id"
        }
    
    try:
        # Single-query fetch: join Table -> Card on parent_card_id and filter by
        # the business card_id. Projects only the columns we actually return so
        # we don't pull entire Table rows across the wire. Replaces the previous
        # two round-trip pattern (card PK lookup + tables lookup).
        stmt = (
            select(
                Table.table_id,
                Table.table_title,
                Table.table_markdown,
                Table.base64_s3_uri,
            )
            .join(Card, Table.parent_card_id == Card.id)
            .where(Card.card_id == card_id, Card.is_active.is_(True))
        )
        result = await session.execute(stmt)
        rows = result.all()

        if not rows:
            logger.info(f"No tables found for card with business card_id: {card_id}")
            return {
                "success": True,
                "tables": []
            }

        tables_data = [
            {
                "table_id": row.table_id,
                "table_title": row.table_title,
                "table_markdown": row.table_markdown,
                "s3_uri": row.base64_s3_uri,
            }
            for row in rows
        ]

        logger.info(f"Successfully retrieved {len(tables_data)} tables for card with business card_id: {card_id}")
        return {
            "success": True,
            "tables": tables_data
        }
        
    except SQLAlchemyError as e:
        logger.error(f"Database error retrieving card tables: {str(e)}", exc_info=True)
        return {
            "success": False,
            "error": f"Database error: {str(e)}"
        }
    except Exception as e:
        logger.error(f"Unexpected error retrieving card tables: {str(e)}", exc_info=True)
        return {
            "success": False,
            "error": f"Unexpected error: {str(e)}"
        }

async def get_report_cards(report_id: str, session: AsyncSession, version_id: str = None) -> Dict[str, Any]:
    """
    Get all cards associated with a specific report.
    
    Args:
        report_id (str): ID of the report to retrieve cards for
        session (AsyncSession): SQLAlchemy async session
        version_id (str, optional): Not used currently, kept for backward compatibility
            
    Returns:
        Dict[str, Any]: Dictionary with success status and list of cards
    """
    logger.info(f"Getting cards for report with ID: {report_id}")
    
    if not report_id:
        logger.error("Missing required field: report_id")
        return {
            "success": False,
            "error": "Missing required field: report_id"
        }
    
    try:
        # Eager-load `tables` relationship so we don't issue one extra query per
        # card (previous N+1 pattern was: 1 cards query + 2 queries per card for
        # tables). `selectinload` issues exactly one extra query that fetches
        # all tables for the batch of cards in a single round trip.
        stmt = (
            select(Card)
            .options(selectinload(Card.tables))
            .where(
                Card.report_id == report_id,
                Card.is_active.is_(True),
                Card.is_deleted.is_(False),
            )
            .order_by(Card.sequence)
        )

        result = await session.execute(stmt)
        cards = result.scalars().all()

        if not cards:
            logger.info(f"No cards found for report with ID: {report_id}")
            return {
                "success": True,
                "cards": []
            }

        cards_data = []
        for card in cards:
            # Tables were eager-loaded — no extra DB call here.
            tables_data = [
                {
                    "table_id": t.table_id,
                    "table_title": _normalize_table_title_value(t.table_title),
                    "table_markdown": t.table_markdown,
                    "s3_uri": t.base64_s3_uri,
                }
                for t in card.tables
            ]

            sub_sections_data = _normalize_subsections_for_response(card.sub_sections)

            # Support both the new JSONB content structure and legacy strings.
            if isinstance(card.content, dict):
                content_text = card.content.get('content', '')
                content_name = card.content.get('name', card.title or '')
                content_tables = _normalize_tables_for_response(card.content.get('tables', tables_data))
            else:
                content_text = card.content or ''
                content_name = card.title or ''
                content_tables = tables_data

            card_data = {
                "report_id": card.report_id,
                "sequence": card.sequence,
                "citations": card.citations,
                "created_at": card.created_at.isoformat() if card.created_at else None,
                "summary": card.summary,
                "type": card.type,
                "section": [
                    {
                        "name": content_name,
                        "content": content_text,
                        "tables": content_tables
                    }
                ],
                "sub_sections": sub_sections_data
            }

            cards_data.append(card_data)

        logger.info(f"Successfully retrieved {len(cards_data)} cards for report with ID: {report_id}")
        return {
            "success": True,
            "cards": cards_data
        }
        
    except SQLAlchemyError as e:
        logger.error(f"Database error retrieving report cards: {str(e)}", exc_info=True)
        return {
            "success": False,
            "error": f"Database error: {str(e)}"
        }
    except Exception as e:
        logger.error(f"Unexpected error retrieving report cards: {str(e)}", exc_info=True)
        return {
            "success": False,
            "error": f"Unexpected error: {str(e)}"
        }
    
async def get_chat_reports_and_cards(chat_id: int, session: AsyncSession) -> Dict[str, Any]:
    """
    Get all report files for a specific chat_id, including their associated cards.
    
    Args:
        chat_id (int): The chat_id to get reports for
        session (AsyncSession): The SQLAlchemy async session
            
    Returns:
        Dict[str, Any]: Dictionary with success status and reports with their cards
    """
    logger.info(f"Getting report files for chat_id: {chat_id}")

    if not chat_id:
        logger.error("Invalid chat_id: empty value provided")
        return {
            "success": False,
            "error": "Invalid chat ID: empty value provided"
        }
    
    report_list = []

    try:
        # A chat can have multiple reports (retries, reruns) — fetch all, newest first
        stmt = select(Report).where(Report.chat_id == chat_id).order_by(Report.created_at.desc())
        result = await session.execute(stmt)
        reports = result.scalars().all()

        if not reports:
            logger.warning(f"No report found for chat_id: {chat_id}")
            return {
                "success": True,
                "reports": []
            }

        for report in reports:
            # Get all active cards directly from Card table
            cards_stmt = select(Card).where(
                Card.report_id == report.id,
                Card.is_active == True,
                Card.is_deleted == False
            ).order_by(Card.sequence)

            cards_result = await session.execute(cards_stmt)
            cards = cards_result.scalars().all()

            # Convert cards to list of dictionaries
            cards_data = []
            for card in cards:
                content_obj = card.content if isinstance(card.content, dict) else {}
                cards_data.append({
                    "id": card.card_id,
                    "title": card.title,
                    "sequence": card.sequence,
                    "content": content_obj.get('content', ''),
                    "tables": _normalize_tables_for_response(content_obj.get('tables', [])),
                    "sub_sections": _normalize_subsections_for_response(card.sub_sections),
                    "citations": card.citations,
                    "summary": content_obj.get('summary', ''),
                    "type": card.type
                })

            filtered_layout = await prune_report_layout_async(report.layout, cards_data)

            # Build report response
            report_data = {
                "id": report.id,
                "version": report.current_version,
                "current_version": report.current_version,
                "poster_image_url": report.poster_image_url,
                "s3_uri": report.s3_uri,
                "status": report.status,
                "last_activity_at": report.last_activity_at.isoformat() if report.last_activity_at else None,
                "created_at": report.created_at.isoformat() if report.created_at else None,
                "generated_at": report.generated_at.isoformat() if report.generated_at else None,
                "title": report.title,
                "report_layout": filtered_layout,
                "summary": report.summary,
                "length": report.length,
                "domain_name": report.domain_name,
                "report_type": report.report_type,
                "citations": report.citations,
                "cards": cards_data
            }

            report_list.append(report_data)

        logger.info(f"Successfully retrieved {len(report_list)} report(s) for chat_id: {chat_id}")
        return {
            "success": True,
            "reports": report_list
        }

    except SQLAlchemyError as e:
        logger.error(f"Database error retrieving report files: {str(e)}", exc_info=True)
        return {
            "success": False,
            "error": f"Database error: {str(e)}"
        }

    except Exception as e:
        logger.error(f"Unexpected error retrieving report files: {str(e)}", exc_info=True)
        return {
            "success": False,
            "error": f"Unexpected error: {str(e)}"
        }
    
async def insert_publish_details(publish_data: Dict[str, Any], session: AsyncSession) -> Dict[str, Any]:
    """
    Insert publishing details for a report.
    
    Args:
        publish_data (Dict[str, Any]): Dictionary containing publish data with keys:
            - report_id (str): ID of the associated report (required)
            - faq (Dict/List, optional): Frequently asked questions related to the report
            - insights (Dict/List, optional): Key insights from the report
            - industries_jobs (str, optional): Industries or job sectors the report is relevant to
            - geographic_areas (str, optional): Geographic areas covered in the report
            - special_emphasis (str, optional): Special areas of emphasis in the report
            - audience (str, optional): Target audience for the report
            - purpose (str, optional): Purpose of the report
            - overview (str, optional): Overview or summary of the report
            - media_details (Dict, optional): Details about media elements in the report
            - page_count (int, optional): Number of pages in the report
            - source_count (int, optional): Number of sources cited in the report
            - table_count (int, optional): Number of tables in the report
            - viz_count (int, optional): Number of visualizations in the report
        session (AsyncSession): SQLAlchemy async session
            
    Returns:
        Dict[str, Any]: Dictionary with success status and publish_id
    """
    logger.info(f"Creating publish details for report: {publish_data.get('report_id')}")
    
    # Validate required fields
    if 'report_id' not in publish_data or not publish_data['report_id']:
        error_msg = "Missing required field: report_id"
        logger.error(error_msg)
        return {
            "success": False,
            "error": error_msg
        }
    
    try:
        # Check if report exists
        report_stmt = select(Report).where(Report.id == publish_data['report_id'])
        report_result = await session.execute(report_stmt)
        report = report_result.scalar_one_or_none()
        
        if not report:
            error_msg = f"Report with ID {publish_data['report_id']} not found"
            logger.error(error_msg)
            return {
                "success": False,
                "error": error_msg
            }
        
        # Generate a new UUID for the publish record
        publish_id = str(uuid7())
        
        # Create a new publish record
        new_publish = Publish(
            id=publish_id,
            report_id=publish_data['report_id'],
            faq=publish_data.get('faq'),
            insights=publish_data.get('insights'),
            industries_jobs=publish_data.get('industries_jobs'),
            geographic_areas=publish_data.get('geographic_areas'),
            special_emphasis=publish_data.get('special_emphasis'),
            audience=publish_data.get('audience'),
            purpose=publish_data.get('purpose'),
            overview=publish_data.get('overview'),
            media_details=publish_data.get('media_details'),
            page_count=publish_data.get('page_count'),
            source_count=publish_data.get('source_count'),
            table_count=publish_data.get('table_count'),
            viz_count=publish_data.get('viz_count')
        )
        
        session.add(new_publish)
        await session.commit()
        
        logger.info(f"Successfully created publish details with ID: {publish_id}")
        return {
            "success": True,
            "publish_id": publish_id
        }
        
    except SQLAlchemyError as e:
        await session.rollback()
        error_msg = f"Database error: {str(e)}"
        logger.error(error_msg, exc_info=True)
        return {
            "success": False,
            "error": error_msg
        }
    except Exception as e:
        await session.rollback()
        error_msg = f"Unexpected error: {str(e)}"
        logger.error(error_msg, exc_info=True)
        return {
            "success": False,
            "error": error_msg
        }

async def get_table_id_markdown_map(report_id: str, session: AsyncSession) -> Dict[str, Any]:
    """
    Get the table_id_markdown_map for a specific report.
    
    Args:
        report_id (str): ID of the report to retrieve table_id_markdown_map for
        session (AsyncSession): SQLAlchemy async session
            
    Returns:
        Dict[str, Any]: Dictionary with success status and table_id_markdown_map
    """
    logger.info(f"Getting table_id_markdown_map for report: {report_id}")
    
    if not report_id:
        logger.error("Missing required field: report_id")
        return {
            "success": False,
            "error": "Missing required field: report_id"
        }
    
    try:
        # Query to get all tables for active cards only
        stmt = select(Table).join(
            Card, Table.parent_card_id == Card.id
        ).where(
            Table.report_id == report_id,
            Card.is_active == True
        )
        result = await session.execute(stmt)
        tables = result.scalars().all()

        table_id_markdown_map = {table.table_id: table.table_markdown for table in tables}

        logger.info(f"Successfully retrieved {len(table_id_markdown_map)} tables from active cards for report: {report_id}")
        return {
            "success": True,
            "table_id_markdown_map": table_id_markdown_map
            }
    except Exception as e:
        logger.error(f"Unexpected error retrieving table_id_markdown_map: {str(e)}", exc_info=True)
        return {
            "success": False,
            "error": f"Unexpected error: {str(e)}"
        }
    except SQLAlchemyError as e:
        logger.error(f"Database error retrieving table_id_markdown_map: {str(e)}", exc_info=True)
        return {
            "success": False,
            "error": f"Database error: {str(e)}"
        }

# async def get_report_in_cards_format(report_id: str, session: AsyncSession) -> Dict[str, Any]:
#     """
#     Get all active cards for a specific report in the format similar to cards_for_db.json.
    
#     Args:
#         report_id (str): ID of the report to retrieve cards for
#         session (AsyncSession): SQLAlchemy async session
            
#     Returns:
#         Dict[str, Any]: Dictionary with success status and formatted cards data
#     """
#     logger.info(f"Getting active cards for report with ID: {report_id} in cards_for_db format")
    
#     if not report_id:
#         logger.error("Missing required field: report_id")
#         return {
#             "success": False,
#             "error": "Missing required field: report_id"
#         }
    
#     try:
#         # Query to get all active cards for the report, ordered by sequence
#         stmt = select(Card).where(
#             Card.report_id == report_id,
#             Card.is_active == True
#         ).order_by(Card.sequence)
#         result = await session.execute(stmt)
#         cards = result.scalars().all()
        
#         if not cards:
#             logger.warning(f"No active cards found for report with ID: {report_id}")
#             return {
#                 "success": True,
#                 "cards": []
#             }
        
#         # Convert cards to the required format
#         formatted_cards = []
#         for card in cards:
#             # Get tables for this card using parent_card_id (card's primary key id)
#             tables_stmt = select(Table).where(Table.parent_card_id == card.id)
#             tables_result = await session.execute(tables_stmt)
#             tables = tables_result.scalars().all()
            
#             # Format tables for the section
#             formatted_tables = []
#             for table in tables:
#                 formatted_tables.append({
#                     "visualization": table.base64_s3_uri or "",  # base64_s3_uri field represents visualization
#                     "table_id": table.table_id or "",
#                     "table_title": table.table_title or ""
#                 })
            
#             # Create the card structure similar to cards_for_db.json
#             formatted_card = {
#                 "section": [
#                     {
#                         "name": card.title or "",
#                         "content": card.content or "",
#                         "tables": formatted_tables,
#                         "id": card.card_id  # Add card_id in each section
#                     }
#                 ],
#                 "sub_sections": card.sub_sections if card.sub_sections else [],
#                 "citations": card.citations if card.citations else {},
#                 "summary": card.summary or ""
#             }
            
#             formatted_cards.append(formatted_card)
        
#         logger.info(f"Successfully retrieved {len(formatted_cards)} active cards for report with ID: {report_id}")
#         return {
#             "success": True,
#             "cards": formatted_cards
#         }
        
#     except SQLAlchemyError as e:
#         logger.error(f"Database error retrieving report cards: {str(e)}", exc_info=True)
#         return {
#             "success": False,
#             "error": f"Database error: {str(e)}"
#         }
#     except Exception as e:
#         logger.error(f"Unexpected error retrieving report cards: {str(e)}", exc_info=True)
#         return {
#             "success": False,
#             "error": f"Unexpected error: {str(e)}"
#         }

async def get_report_in_cards_format(report_id: str, session: AsyncSession) -> Dict[str, Any]:
    """
    Get all active cards for a specific report in the format similar to cards_for_db.json.
    
    Args:
        report_id (str): ID of the report to retrieve cards for
        session (AsyncSession): SQLAlchemy async session
            
    Returns:
        Dict[str, Any]: Dictionary with success status and formatted cards data
    """
    logger.info(f"Getting active cards for report with ID: {report_id} in cards_for_db format")
    
    if not report_id:
        logger.error("Missing required field: report_id")
        return {
            "success": False,
            "error": "Missing required field: report_id"
        }
    
    try:
        # Query to get all active cards for the report, ordered by sequence
        stmt = select(Card).where(
            Card.report_id == report_id,
            Card.is_active == True,
            Card.is_deleted == False
        ).order_by(Card.sequence)
        result = await session.execute(stmt)
        cards = result.scalars().all()
        
        if not cards:
            logger.warning(f"No active cards found for report with ID: {report_id}")
            return {
                "success": True,
                "cards": []
            }
        
        # Convert cards to the required format
        formatted_cards = []
        for card in cards:
            # Parse content based on type
            content_data = card.content
            content_text = ""
            tables_data = []
            
            # Handle content based on its structure
            if isinstance(content_data, dict):
                content_text = content_data.get("content", "")
                # Use tables from content if available, otherwise empty list
                if "tables" in content_data:
                    tables_data = content_data.get("tables", [])
            elif isinstance(content_data, str):
                content_text = content_data
            
            # Create the card structure similar to cards_for_db.json
            formatted_card = {
                "section": [
                    {
                        "name": card.title or "",
                        "content": content_text,
                        "tables": tables_data,
                        "id": card.card_id
                    }
                ],
                "sub_sections": card.sub_sections if card.sub_sections else [],
                "citations": card.citations if card.citations else {},
                "summary": card.summary or ""
            }
            
            formatted_cards.append(formatted_card)
        
        logger.info(f"Successfully retrieved {len(formatted_cards)} active cards for report with ID: {report_id}")
        return {
            "success": True,
            "cards": formatted_cards
        }
        
    except SQLAlchemyError as e:
        logger.error(f"Database error retrieving report cards: {str(e)}", exc_info=True)
        return {
            "success": False,
            "error": f"Database error: {str(e)}"
        }
    except Exception as e:
        logger.error(f"Unexpected error retrieving report cards: {str(e)}", exc_info=True)
        return {
            "success": False,
            "error": f"Unexpected error: {str(e)}"
        }

async def insert_card_version(session: AsyncSession, card_id: str, section_id: str, user_instruction: str, refinement_type: str, subsection_id: str = None) -> Dict[str, Any]:
    """
    Insert a new card version record for tracking refinement history.
    
    Args:
        session (AsyncSession): SQLAlchemy async session
        card_id (str): Primary key ID of the card being refined (cards.id) - used for parent_card_id FK
        section_id (str): Business card_id (cards.card_id) - for tracking which logical card was modified
        user_instruction (str): User instruction for the refinement
        refinement_type (str): Type of refinement - use RefinementType enum values
            (e.g., RefinementType.DELETE_SECTION.value, RefinementType.REFINE_SUBSECTION.value, etc.)
        subsection_id (str, optional): ID of the subsection being modified (null for section-level changes)
        
    Returns:
        Dict[str, Any]: Dictionary with success status and version_id
    """
    logger.info(f"Creating card version record for card_id (PK): {card_id}, section_id: {section_id}, type: {refinement_type}, subsection_id: {subsection_id}")
    
    if not card_id or not section_id or not user_instruction or not refinement_type:
        logger.error("Missing required fields for card version")
        return {
            "success": False,
            "error": "Missing required fields: card_id, section_id, user_instruction, or refinement_type"
        }
    
    try:
        # Create new card version record
        new_version = CardVersion(
            id=str(uuid7()),
            parent_card_id=card_id,
            section_id=section_id,
            subsection_id=subsection_id,
            user_instruction=user_instruction,
            refinement_type=refinement_type
        )
        
        session.add(new_version)
        await session.commit()
        
        logger.info(f"Successfully created card version with ID: {new_version.id}")
        return {
            "success": True,
            "version_id": new_version.id
        }
        
    except SQLAlchemyError as e:
        logger.error(f"Database error creating card version: {str(e)}", exc_info=True)
        await session.rollback()
        return {
            "success": False,
            "error": f"Database error: {str(e)}"
        }
    except Exception as e:
        logger.error(f"Unexpected error creating card version: {str(e)}", exc_info=True)
        await session.rollback()
        return {
            "success": False,
            "error": f"Unexpected error: {str(e)}"
        }

async def refine_card_in_db(session: AsyncSession, report_id: str, card_data: Dict[str, Any], user_instruction: str, refinement_type: str, table_id_markdown_map: Dict[str, str], subsection_id: str = None) -> Dict[str, Any]:
    """
    Refine a card by creating a new version and storing the refinement details.
    
    Args:
        session (AsyncSession): SQLAlchemy async session
        report_id (str): ID of the report containing the card
        card_data (Dict[str, Any]): The refined card data
        user_instruction (str): User instruction for the refinement
        refinement_type (str): Type of refinement - use RefinementType enum values
        table_id_markdown_map (Dict[str, str]): Mapping of table IDs to markdown
        subsection_id (str, optional): ID of the subsection being modified (null for section-level changes)
        
    Returns:
        Dict[str, Any]: Dictionary with success status and new card details
    """
    logger.info(f"Refining card for report: {report_id}, type: {refinement_type}, subsection_id: {subsection_id}")
    
    try:
        # Get the business card_id from the card data
        business_card_id = None
        if card_data.get('section') and len(card_data['section']) > 0:
            business_card_id = card_data['section'][0].get('id')
        
        if not business_card_id:
            logger.error("No business card_id found in card data")
            return {
                "success": False,
                "error": "No business card_id found in card data"
            }
        
        # Deactivate all previous versions of this card
        existing_cards_stmt = select(Card).where(
            Card.card_id == business_card_id,
            Card.report_id == report_id
        )
        existing_cards_result = await session.execute(existing_cards_stmt)
        existing_cards = existing_cards_result.scalars().all()
        
        if existing_cards:
            sequence = existing_cards[-1].sequence #since sequence will be same
            type = existing_cards[-1].type
            logger.info(f"Deactivating {len(existing_cards)} previous versions of card_id: {business_card_id}")
            for existing_card in existing_cards:
                existing_card.is_active = False
            await session.flush()
        else:
            sequence = 1
            type = "section"
        
        
        # Calculate version number using MAX(version) + 1 to avoid duplicate version numbers
        # This handles cases where user reverts and then refines (deleted versions don't cause duplicates)
        if existing_cards:
            version = max(card.version for card in existing_cards) + 1
        else:
            version = 1
        
        # Generate new primary key ID for this card instance
        new_card_id = str(uuid7())
        
        
        # Create the new refined card
        new_card = Card(
            id=new_card_id,
            card_id=business_card_id,
            report_id=report_id,
            title=card_data['section'][0].get('name'),
            sequence=sequence,
            content=card_data['section'][0],  # Use entire section structure as content
            sub_sections=card_data.get('sub_sections'),
            citations=card_data.get('citations', {}),
            created_at=datetime.now(timezone.utc),
            summary=card_data.get('summary'),
            type=type,
            version=version,
            is_active=True,
            is_deleted=False,
            changed_since_es=True,  # Refined cards have changed since last ES
            last_es_version_used=None  # Not yet used in any ES
        )
        
        session.add(new_card)
        await session.commit()
        
        # Create card version record
        version_result = await insert_card_version(
            session=session,
            card_id=new_card_id,
            section_id=business_card_id,
            user_instruction=user_instruction,
            refinement_type=refinement_type,
            subsection_id=subsection_id
        )
        
        if not version_result.get('success'):
            logger.warning(f"Failed to create card version record: {version_result.get('error')}")
        
        # Get the table_id to markdown mapping for this report
        
        # Insert tables from the updated card - always call this function
        table_insert_result = await insert_tables_from_updated_card(
            session=session,
            report_id=report_id,
            business_card_id=business_card_id,
            updated_card=card_data,
            table_id_markdown_map=table_id_markdown_map or {}
        )
        
        if not table_insert_result.get('success'):
            logger.warning(f"Failed to insert tables from updated card: {table_insert_result.get('error')}")
        else:
            logger.info(f"Successfully inserted {table_insert_result.get('total_processed', 0)} tables from updated card")
        
        logger.info(f"Successfully refined card with ID: {new_card_id}, version: {version}")
        return {
            "success": True,
            "parentcard_id": new_card_id,
            "card_id": business_card_id,
            "version": version
        }
        
    except SQLAlchemyError as e:
        logger.error(f"Database error refining card: {str(e)}", exc_info=True)
        await session.rollback()
        return {
            "success": False,
            "error": f"Database error: {str(e)}"
        }
    except Exception as e:
        logger.error(f"Unexpected error refining card: {str(e)}", exc_info=True)
        await session.rollback()
        return {
            "success": False,
            "error": f"Unexpected error: {str(e)}"
        }



async def insert_tables_from_updated_card(
    session: AsyncSession, 
    report_id: str, 
    business_card_id: str, 
    updated_card: Dict[str, Any], 
    table_id_markdown_map: Dict[str, str]
) -> Dict[str, Any]:
    """
    Extract table data from updated card and insert new table entries.
    
    Args:
        session (AsyncSession): SQLAlchemy async session
        report_id (str): ID of the report
        business_card_id (str): normal card_id
        updated_card (Dict[str, Any]): The updated card data containing sections and subsections
        table_id_markdown_map (Dict[str, str]): Mapping of table_id to markdown content
        
    Returns:
        Dict[str, Any]: Dictionary with success status and list of inserted table IDs
    """
    logger.info(f"Extracting and inserting tables from updated card for business_card_id: {business_card_id}")
    
    if not updated_card:
        logger.warning("No updated card data provided")
        return {
            "success": True,
            "inserted_tables": []
        }
    
    inserted_table_ids = []
    
    def _safe_str(value: Any) -> str:
        """Normalize polymorphic payload fields into plain strings for DB columns."""
        if value is None:
            return ""
        if isinstance(value, tuple):
            return _safe_str(value[0] if value else "")
        if isinstance(value, str):
            return value
        return str(value)
    
    def _extract_markdowns_from_content(content: Any) -> list[str]:
        """
        Extract markdown tables from section/subsection content.
        Falls back to empty list when extraction fails or content is not a plain string.
        """
        if not isinstance(content, str) or not content.strip():
            return []
        try:
            from src.core.cards.card_utils import extract_markdown_tables
            return extract_markdown_tables(content) or []
        except Exception:
            return []
    
    try:
        # Process section tables
        if updated_card.get('section') and isinstance(updated_card['section'], list):
            for section in updated_card['section']:
                section_markdowns = _extract_markdowns_from_content(section.get('content'))
                if section.get('tables') and isinstance(section['tables'], list):
                    for idx, table in enumerate(section['tables']):
                        table_id = table.get('table_id')
                        if table_id:
                            # Prefer exact map lookup by table_id; fallback to content-order markdown extraction.
                            table_markdown = ""
                            if table_id_markdown_map:
                                table_markdown = table_id_markdown_map.get(table_id, "")
                            if not table_markdown and idx < len(section_markdowns):
                                table_markdown = section_markdowns[idx]
                            if not table_markdown:
                                table_markdown = _safe_str(table.get('table_markdown', ''))
                            
                            table_data = {
                                'table_id': table_id,
                                'table_title': _safe_str(table.get('table_title', '')),
                                'table_markdown': table_markdown,
                                'visualization': table.get('visualization', ''),
                                'report_id': report_id
                            }
                            
                            logger.info(f"Inserting section table with data: {table_data}")
                            
                            # Insert the table
                            insert_result = await insert_table(
                                session=session,
                                card_id=business_card_id,  # Use the business card_id
                                table_data=table_data
                            )
                            
                            if insert_result.get('success'):
                                inserted_table_ids.append(insert_result.get('table_id'))
                                logger.info(f"Successfully inserted section table with ID: {table_id}")
                            else:
                                logger.warning(f"Failed to insert section table {table_id}: {insert_result.get('error')}")
                        else:
                            logger.warning(f"Section table ID is empty or None")
        
        # Process subsection tables
        if updated_card.get('sub_sections') and isinstance(updated_card['sub_sections'], list):
            for subsection in updated_card['sub_sections']:
                subsection_markdowns = _extract_markdowns_from_content(subsection.get('content'))
                if subsection.get('tables') and isinstance(subsection['tables'], list):
                    for idx, table in enumerate(subsection['tables']):
                        table_id = table.get('table_id')
                        if table_id:
                            # Prefer exact map lookup by table_id; fallback to content-order markdown extraction.
                            table_markdown = ""
                            if table_id_markdown_map:
                                table_markdown = table_id_markdown_map.get(table_id, "")
                            if not table_markdown and idx < len(subsection_markdowns):
                                table_markdown = subsection_markdowns[idx]
                            if not table_markdown:
                                table_markdown = _safe_str(table.get('table_markdown', ''))
                            
                            table_data = {
                                'table_id': table_id,
                                'table_title': _safe_str(table.get('table_title', '')),
                                'table_markdown': table_markdown,
                                'visualization': table.get('visualization', ''),
                                'report_id': report_id
                            }
                            
                            logger.info(f"Inserting subsection table with data: {table_data}")
                            
                            # Insert the table
                            insert_result = await insert_table(
                                session=session,
                                card_id=business_card_id,  # Use the business card_id
                                table_data=table_data
                            )
                            
                            if insert_result.get('success'):
                                inserted_table_ids.append(insert_result.get('table_id'))
                                logger.info(f"Successfully inserted subsection table with table_ID: {table_id}")
                            else:
                                logger.warning(f"Failed to insert subsection table {table_id}: {insert_result.get('error')}")
                        else:
                            logger.warning(f"Subsection table ID is empty or None")
        
        logger.info(f"Successfully processed tables for card {business_card_id}, inserted {len(inserted_table_ids)} tables")
        return {
            "success": True,
            "inserted_tables": inserted_table_ids,
            "total_processed": len(inserted_table_ids)
        }
        
    except Exception as e:
        logger.error(f"Error processing tables from updated card: {str(e)}", exc_info=True)
        return {
            "success": False,
            "error": f"Error processing tables: {str(e)}",
            "inserted_tables": inserted_table_ids
        }

async def delete_card_or_subsection(session, card_id: str, subsection_id: Optional[str], toc: str):
    """
    Delete a card (section) or subsection from the database.
    
    Args:
        session: Database session
        card_id: The business card ID to delete or modify
        subsection_id: Optional subsection ID to delete. If None, deletes the whole section
        toc: Updated table of contents string
    
    Returns:
        Dict with success status and details
    """
    try:
        # Get the active version of the card
        card_query = select(Card).where(
            Card.card_id == card_id,
            Card.is_active == True,
            Card.is_deleted == False
        )
        result = await session.execute(card_query)
        active_card = result.scalar_one_or_none()
        
        if not active_card:
            return {
                'success': False,
                'error': f'No active card found with card_id: {card_id}'
            }
        
        if subsection_id is None:
            # SCENARIO 1: Delete whole section
            # Mark the active card as deleted and inactive, and flag for ES regeneration
            active_card.is_deleted = True
            active_card.is_active = False
            active_card.changed_since_es = True  # Mark as changed to trigger ES regeneration
            active_card.last_es_version_used = None  # Will be set when ES is regenerated
            
            # Update the TOC card (assuming it's at index 2 in the cards array)
            # Find the TOC card for this report
            toc_query = select(Card).where(
                Card.report_id == active_card.report_id,
                Card.type == "toc",  # TOC is typically at sequence 3
                Card.is_active == True,
                Card.is_deleted == False
            )
            toc_result = await session.execute(toc_query)
            toc_card = toc_result.scalar_one_or_none()
            
            if toc_card:
                # Create a new version of the TOC card with updated content
                new_toc_card = Card(
                    id=str(uuid7()),
                    card_id=toc_card.card_id,
                    report_id=toc_card.report_id,
                    title=toc_card.title,
                    sequence=toc_card.sequence,
                    sub_sections=toc_card.sub_sections,
                    content={
                        "name": "table_of_contents",
                        "content": toc,
                        "tables": [{"visualization": "", "table_id": "", "table_title": ""}],
                        "id": toc_card.card_id
                    },  # Updated TOC content in JSONB structure
                    citations=toc_card.citations,
                    created_at=datetime.now(timezone.utc),
                    summary=toc_card.summary,
                    type=toc_card.type,
                    version=toc_card.version + 1,
                    is_active=True,
                    is_deleted=False,
                    changed_since_es=False,  # TOC doesn't affect ES
                    last_es_version_used=None
                )
                
                # Mark old TOC card as inactive
                toc_card.is_active = False
                
                # Insert new TOC card
                session.add(new_toc_card)
                
                # Create card version record for TOC update
                await insert_card_version(
                    session=session,
                    card_id=new_toc_card.id,
                    section_id=toc_card.card_id,
                    user_instruction="Table of contents updated after section deletion",
                    refinement_type=RefinementType.REFINE_TOC.value
                )
            
            # Create card version record for section deletion
            await insert_card_version(
                session=session,
                card_id=active_card.id,
                section_id=active_card.card_id,
                user_instruction="Section deleted by user",
                refinement_type=RefinementType.DELETE_SECTION.value
            )
            
            await session.commit()
            
            return {
                'success': True,
                'message': f'Section {card_id} deleted successfully',
                'deleted_card_id': card_id,
                'deleted_subsection_id': None,
                'toc_updated': True
            }
            
        else:
            # SCENARIO 2: Delete subsection
            # Parse the current card content to find and remove the subsection
            # current_content = active_card.content
            current_sub_sections = active_card.sub_sections
            
            if not current_sub_sections:
                return {
                    'success': False,
                    'error': f'No subsections found in card {card_id}'
                }
            
            # Find and remove the subsection with the specified ID
            subsection_found = False
            updated_sub_sections = []
            
            for subsection in current_sub_sections:
                if subsection.get('id') == subsection_id:
                    subsection_found = True
                    # Skip this subsection (don't add it to updated list)
                    continue
                else:
                    updated_sub_sections.append(subsection)
            
            if not subsection_found:
                return {
                    'success': False,
                    'error': f'Subsection {subsection_id} not found in card {card_id}'
                }
            
            # Regenerate summary without the deleted subsection
            section_content = ""
            
            # Get section name and content
            if active_card.content and isinstance(active_card.content, dict):
                section_content += active_card.content.get('name', '') + "\n\n"
                
                # Get section-level content
                content_data = active_card.content.get('content', '')
                if isinstance(content_data, dict):
                    section_content += content_data.get('content', '')
                else:
                    section_content += str(content_data)
                section_content += "\n\n"
            
            # Get all remaining subsections (excluding deleted one)
            for subsection in updated_sub_sections:
                if isinstance(subsection, dict):
                    section_content += subsection.get('name', '') + "\n"
                    subsection_content = subsection.get('content', '')
                    if isinstance(subsection_content, dict):
                        section_content += subsection_content.get('content', '')
                    else:
                        section_content += str(subsection_content)
                    section_content += "\n\n"
            
            # Generate new summary using synchronous function
            from src.core.cards.card_utils import generate_section_summary
            try:
                new_summary = generate_section_summary(section_content)
                logger.info(f"Successfully regenerated summary after subsection deletion for card {card_id}")
            except Exception as e:
                logger.error(f"Error regenerating summary after subsection deletion: {str(e)}")
                new_summary = active_card.summary  # Fall back to old summary
            
            # Create a new version of the card with the subsection removed and updated summary
            new_card = Card(
                id=str(uuid7()),
                card_id=active_card.card_id,
                report_id=active_card.report_id,
                title=active_card.title,
                sequence=active_card.sequence,
                sub_sections=updated_sub_sections,  # Updated subsections
                content=active_card.content,
                citations=active_card.citations,
                created_at=datetime.now(timezone.utc),
                summary=new_summary,  # ✅ Regenerated summary without deleted subsection
                type=active_card.type,
                version=active_card.version + 1,
                is_active=True,
                is_deleted=False,
                changed_since_es=True,  # Card changed when subsection deleted
                last_es_version_used=None  # Not yet used in any ES
            )
            
            # Mark old card as inactive
            active_card.is_active = False
            
            # Insert new card
            session.add(new_card)
            
            # Update the TOC card with the new content
            toc_query = select(Card).where(
                Card.report_id == active_card.report_id,
                Card.type == "toc",  #TOC is typically at sequence 3
                Card.is_active == True,
                Card.is_deleted == False
            )
            toc_result = await session.execute(toc_query)
            toc_card = toc_result.scalar_one_or_none()
            
            if toc_card:
                # Create a new version of the TOC card with updated content
                new_toc_card = Card(
                    id=str(uuid7()),
                    card_id=toc_card.card_id,
                    report_id=toc_card.report_id,
                    title=toc_card.title,
                    sequence=toc_card.sequence,
                    sub_sections=toc_card.sub_sections,
                    content={
                        "name": "table_of_contents",
                        "content": toc,
                        "tables": [{"visualization": "", "table_id": "", "table_title": ""}],
                        "id": toc_card.card_id
                    },  # Updated TOC content in JSONB structure
                    citations=toc_card.citations,
                    created_at=datetime.now(timezone.utc),
                    summary=toc_card.summary,
                    type=toc_card.type,
                    version=toc_card.version + 1,
                    is_active=True,
                    is_deleted=False,
                    changed_since_es=False,  # TOC doesn't affect ES
                    last_es_version_used=toc_card.last_es_version_used  # Preserve ES version tracking
                )
                
                # Mark old TOC card as inactive
                toc_card.is_active = False
                
                # Insert new TOC card
                session.add(new_toc_card)
                
                # Create card version record for TOC update
                await insert_card_version(
                    session=session,
                    card_id=new_toc_card.id,
                    section_id=toc_card.card_id,
                    user_instruction="Table of contents updated after subsection deletion",
                    refinement_type=RefinementType.REFINE_TOC.value
                )
            
            # Create card version record for subsection deletion
            await insert_card_version(
                session=session,
                card_id=new_card.id,
                section_id=new_card.card_id,
                user_instruction=f"Subsection {subsection_id} deleted by user",
                refinement_type=RefinementType.DELETE_SUBSECTION.value
            )
            
            await session.commit()
            
            return {
                'success': True,
                'message': f'Subsection {subsection_id} deleted successfully from card {card_id}',
                'deleted_card_id': card_id,
                'deleted_subsection_id': subsection_id,
                'toc_updated': True,
                'new_card_id': new_card.id
            }
            
    except Exception as e:
        await session.rollback()
        logger.error(f"Error in delete_card_or_subsection: {e}")
        return {
            'success': False,
            'error': f'Failed to delete card/subsection: {str(e)}'
        }

async def rename_chat(chat_id: str, user_id: str, new_title: str, session: AsyncSession) -> Dict[str, Any]:
    """
    Rename a chat by updating its title.
    
    Args:
        chat_id (str): The ID of the chat to rename
        user_id (str): The ID of the user who owns the chat
        new_title (str): The new title for the chat
        session (AsyncSession): SQLAlchemy async session
        
    Returns:
        Dict[str, Any]: Dictionary with success status and operation details
    """
    logger.info(f"Renaming chat with ID: {chat_id} for user_id: {user_id}")
    
    try:
        # Check if the chat exists and belongs to the user
        stmt = select(Message).where(
            Message.id == chat_id,
            Message.user_id == user_id
        )
        result = await session.execute(stmt)
        message = result.scalar_one_or_none()
        
        if not message:
            logger.error(f"Chat with ID: {chat_id} not found or does not belong to user: {user_id}")
            return {
                "success": False,
                "error": "Chat not found or does not belong to the user"
            }
        
        # Update the chat title
        message.chat_title = new_title
        message.updated_at = datetime.now(timezone.utc)
        await session.commit()
        
        logger.info(f"Chat with ID: {chat_id} renamed successfully to '{new_title}' for user_id: {user_id}")
        return {
            "success": True,
            "message": "Chat renamed successfully"
        }
        
    except SQLAlchemyError as e:
        logger.error(f"Database error renaming chat: {str(e)}", exc_info=True)
        return {
            "success": False,
            "error": f"Database error: {str(e)}"
        }
        
    except Exception as e:
        logger.error(f"Unexpected error renaming chat: {str(e)}", exc_info=True)
        return {
            "success": False,
            "error": f"Unexpected error: {str(e)}"
        }

async def mark_chat_deleted(chat_id: str, user_id: str, session: AsyncSession) -> Dict[str, Any]:
    """
    Mark a chat as deleted without physically removing it from the database.
    
    Args:
        chat_id (str): The ID of the chat to mark as deleted
        user_id (str): The ID of the user who owns the chat
        session (AsyncSession): SQLAlchemy async session
        
    Returns:
        Dict[str, Any]: Dictionary with success status and operation details
    """
    logger.info(f"Marking chat with ID: {chat_id} as deleted for user_id: {user_id}")
    
    try:
        # Check if the chat exists and belongs to the user
        stmt = select(Message).where(
            Message.id == chat_id,
            Message.user_id == user_id
        )
        result = await session.execute(stmt)
        message = result.scalar_one_or_none()
        
        if not message:
            logger.error(f"Chat with ID: {chat_id} not found or does not belong to user: {user_id}")
            return {
                "success": False,
                "error": "Chat not found or does not belong to the user"
            }
            
        # Check if the chat is already deleted
        if message.is_deleted:
            logger.warning(f"Chat with ID: {chat_id} is already marked as deleted")
            return {
                "success": True,
                "message": "Chat was already marked as deleted"
            }
        
        # Mark the chat as deleted
        message.is_deleted = True
        message.deleted_at = datetime.now(timezone.utc)
        await session.commit()
        
        logger.info(f"Chat with ID: {chat_id} marked as deleted successfully for user_id: {user_id}")
        return {
            "success": True,
            "message": "Chat marked as deleted successfully"
        }
        
    except SQLAlchemyError as e:
        logger.error(f"Database error marking chat as deleted: {str(e)}", exc_info=True)
        return {
            "success": False,
            "error": f"Database error: {str(e)}"
        }
        
    except Exception as e:
        logger.error(f"Unexpected error marking chat as deleted: {str(e)}", exc_info=True)
        return {
            "success": False,
            "error": f"Unexpected error: {str(e)}"
        }
        
        
async def delete_visualization(session: AsyncSession, report_id: str, card_id: str, table_id: str, subsection_id: Optional[str] = None) -> Dict[str, Any]:
    """
    Delete visualization for a table by setting base64_s3_uri to NULL in the database
    and updating the card's content to remove the visualization reference.
    
    Args:
        session: Database session
        report_id: The report ID
        card_id: The business card ID that contains the table
        table_id: The table ID to delete visualization for
        subsection_id: Optional subsection ID if the table is in a subsection
    
    Returns:
        Dict with success status and details
    """
    from sqlalchemy.orm.attributes import flag_modified    
    try:
        # Get the active version of the card
        card_query = select(Card).where(
            Card.card_id == card_id,
            Card.is_active == True,
            Card.is_deleted == False
        )
        result = await session.execute(card_query)
        active_card = result.scalar_one_or_none()
        
        if not active_card:
            return {
                'success': False,
                'error': f'No active card found with card_id: {card_id}',
                'error_type': 'not_found'
            }
        
        # Get the table record using table_id and report_id
        # We don't filter by parent_card_id because the table might reference an old card version
        # This happens when a card is refined - new card version is created but table still references old version
        # Order by created_at DESC to get the latest entry if duplicates exist
        table_query = select(Table).where(
            Table.report_id == report_id,
            Table.table_id == table_id
        ).order_by(Table.created_at.desc()).limit(1)
        table_result = await session.execute(table_query)
        table = table_result.scalar_one_or_none()
        
        if not table:
            return {
                'success': False,
                'error': f'No table found with table_id: {table_id} in report: {report_id}',
                'error_type': 'not_found'
            }
        
        # Update the table's parent_card_id to point to the current active card
        # This self-healing approach fixes stale references caused by card refinement
        if table.parent_card_id != active_card.id:
            logger.info(f"Updating table {table_id} parent_card_id from {table.parent_card_id} to {active_card.id} (card was refined)")
            table.parent_card_id = active_card.id
        
        # Clear the visualization in the table record
        table.base64_s3_uri = None
        logger.info(f"Cleared base64_s3_uri for table {table_id}")
        
        # Update the visualization reference directly in the existing card content
        # Important: We need to create deep copies to ensure SQLAlchemy detects the change
        visualization_found = False
        subsection_found = False
        
        if subsection_id:
            # Update in subsection
            if not active_card.sub_sections:
                logger.error(f"Card has no subsections, but subsection_id {subsection_id} was provided")
                await session.rollback()
                return {
                    'success': False,
                    'error': 'Card has no subsections',
                    'error_type': 'not_found'
                }
            
            # Create a deep copy of sub_sections to force SQLAlchemy to detect changes
            sub_sections_copy = copy.deepcopy(active_card.sub_sections)
            
            for subsection in sub_sections_copy:
                if isinstance(subsection, dict) and subsection.get('id') == subsection_id:
                    subsection_found = True
                    for table_entry in subsection.get('tables', []):
                        if table_entry.get('table_id') == table_id:
                            # Clear both visualization fields
                            table_entry['visualization'] = ""
                            if 'visualization_type' in table_entry:
                                table_entry['visualization_type'] = ""
                            visualization_found = True
                            logger.info(f"Cleared visualization in subsection {subsection_id} for table {table_id}")
                            break
                    break
            
            if not subsection_found:
                logger.error(f"Subsection with id {subsection_id} not found in card {card_id}")
                await session.rollback()
                return {
                    'success': False,
                    'error': f'Subsection with id {subsection_id} not found in card',
                    'error_type': 'not_found'
                }
            
            if not visualization_found:
                logger.error(f"Table {table_id} not found in subsection {subsection_id}")
                await session.rollback()
                return {
                    'success': False,
                    'error': f'Table not found in subsection with id {subsection_id}',
                    'error_type': 'not_found'
                }
            
            # Reassign to trigger SQLAlchemy change detection
            active_card.sub_sections = sub_sections_copy
            # Mark the JSONB field as modified
            flag_modified(active_card, 'sub_sections')
        else:
            # Update in section content
            if not isinstance(active_card.content, dict):
                logger.error("Card content is not a dict, cannot update visualization")
                await session.rollback()
                return {
                    'success': False,
                    'error': 'Invalid card content structure',
                    'error_type': 'invalid_data'
                }
            
            # Create a deep copy of content to force SQLAlchemy to detect changes
            content_copy = copy.deepcopy(active_card.content)
            
            for table_entry in content_copy.get('tables', []):
                if table_entry.get('table_id') == table_id:
                    # Clear both visualization fields
                    table_entry['visualization'] = ""
                    if 'visualization_type' in table_entry:
                        table_entry['visualization_type'] = ""
                    visualization_found = True
                    logger.info(f"Cleared visualization in section content for table {table_id}")
                    break
            
            if not visualization_found:
                logger.error(f"Table {table_id} not found in section content")
                await session.rollback()
                return {
                    'success': False,
                    'error': 'Table not found in section content',
                    'error_type': 'not_found'
                }
            
            # Reassign to trigger SQLAlchemy change detection
            active_card.content = content_copy
            # Mark the JSONB field as modified
            flag_modified(active_card, 'content')
        
        # Update timestamp to mark card as modified (for detect_modified_cards)
        active_card.updated_at = datetime.now(timezone.utc)
        logger.info(f"Updated card {card_id} timestamp to mark visualization deletion")
        
        # Flush to ensure changes are written to the database session
        # await session.flush()
        # logger.info(f"Flushed changes for table {table_id}")
        
        # Commit the transaction
        await session.commit()
        logger.info(f"Successfully deleted visualization for table {table_id}")
        
        return {
            'success': True,
            'message': f'Visualization deleted successfully for table {table_id}',
            'card_id': card_id,
            'table_id': table_id,
            'subsection_id': subsection_id
        }
        
    except Exception as e:
        await session.rollback()
        logger.error(f"Error in delete_visualization: {e}")
        return {
            'success': False,
            'error': f'Failed to delete visualization: {str(e)}',
            'error_type': 'server_error'
        }

async def revert_card_to_version(
    session: AsyncSession,
    report_id: str,
    card_id: str
) -> Dict[str, Any]:
    """
    Revert a card to its immediate previous version (undo last refinement).
    
    User can only undo to the immediate previous version (v3 → v2, not v3 → v1).
    Current version is marked as is_active=False AND is_deleted=True.
    Previous version is reactivated.
    
    Args:
        session: Database session
        report_id: ID of the report
        card_id: Business card_id (not primary key)
        
    Returns:
        Dict with success status and revert details
    """
    try:
        # Get all versions of this card ordered by version (including deleted ones for version calculation)
        cards_stmt = select(Card).where(
            Card.card_id == card_id,
            Card.report_id == report_id
        ).order_by(Card.version.desc())
        
        cards_result = await session.execute(cards_stmt)
        all_cards = cards_result.scalars().all()
        
        if not all_cards:
            return {
                "success": False,
                "error": f"No cards found with card_id: {card_id} in report: {report_id}"
            }
        
        # Find current active card
        active_card = next((c for c in all_cards if c.is_active and not c.is_deleted), None)
        if not active_card:
            return {
                "success": False,
                "error": f"No active card found with card_id: {card_id}"
            }
        
        current_version = active_card.version
        
        # Cannot revert version 1 (no previous version)
        if current_version == 1:
            return {
                "success": False,
                "error": "Cannot revert version 1 - it's the first version"
            }
        
        # Find the immediate previous non-deleted version
        # Sort by version descending, find the first one before current that's not deleted
        previous_card = None
        for card in all_cards:
            if card.version < current_version and not card.is_deleted:
                previous_card = card
                break  # Already sorted desc, so first match is the highest version before current
        
        if not previous_card:
            return {
                "success": False,
                "error": f"No previous version found to revert to (all versions before {current_version} are deleted)"
            }
        
        # Deactivate ALL versions first (to handle cases where multiple versions are incorrectly active)
        logger.info(f"Deactivating all versions of card_id: {card_id} before reverting")
        for card in all_cards:
            card.is_active = False
        
        # Mark current active card as deleted
        active_card.is_deleted = True
        
        # Activate previous version card
        previous_card.is_active = True
        
        # Set ES tracking flags - reverting a card means content has changed
        previous_card.changed_since_es = True
        previous_card.last_es_version_used = None
        
        await session.commit()
        
        # Refresh to get latest data
        await session.refresh(previous_card)
        
        logger.info(f"Reverted card {card_id} from version {current_version} to version {previous_card.version}")
        
        # Build card structure matching refine-card response format
        reverted_card_data = {
            "section": [previous_card.content] if previous_card.content else [],
            "sub_sections": previous_card.sub_sections if previous_card.sub_sections else [],
            "citations": previous_card.citations if previous_card.citations else {},
            "summary": previous_card.summary
        }
        
        return {
            "success": True,
            "message": f"Successfully reverted card from version {current_version} to version {previous_card.version}",
            "reverted_to_version": previous_card.version,
            "card_id": card_id,
            "primary_card_id": previous_card.id,
            "deleted_versions": [current_version],
            "reverted_card": reverted_card_data
        }
        
    except SQLAlchemyError as e:
        await session.rollback()
        logger.error(f"Database error reverting card: {str(e)}", exc_info=True)
        return {
            "success": False,
            "error": f"Database error: {str(e)}"
        }
    except Exception as e:
        await session.rollback()
        logger.error(f"Unexpected error reverting card: {str(e)}", exc_info=True)
        return {
            "success": False,
            "error": f"Unexpected error: {str(e)}"
        }


async def create_subscriber(email: str, session: AsyncSession) -> Dict[str, Any]:
    """
    Insert a new subscriber into the database.
    
    Args:
        email (str): Email address of the subscriber
        session (AsyncSession): SQLAlchemy async session
        
    Returns:
        Dict[str, Any]: Dictionary with success status and subscriber data
    """
    logger.info(f"Creating new subscriber with email: {email}")
    
    if not email:
        logger.error("Email address is required for creating subscriber")
        return {
            "success": False,
            "error": "Email address is required"
        }
    
    try:
        # Create new subscriber
        new_subscriber = Subscriber(email=email)
        session.add(new_subscriber)
        await session.commit()
        
        # Refresh to get the auto-generated ID and timestamp
        await session.refresh(new_subscriber)
        
        logger.info(f"Successfully created subscriber with ID: {new_subscriber.id} for email: {email}")
        
        return {
            "success": True,
            "data": {
                "id": new_subscriber.id,
                "email": new_subscriber.email,
                "created_at": new_subscriber.created_at
            }
        }
        
    except SQLAlchemyError as e:
        await session.rollback()
        logger.error(f"Database error creating subscriber: {str(e)}", exc_info=True)
        return {
            "success": False,
            "error": f"Database error: {str(e)}"
        }
    
    except Exception as e:
        await session.rollback()
        logger.error(f"Unexpected error creating subscriber: {str(e)}", exc_info=True)
        return {
            "success": False,
            "error": f"Unexpected error: {str(e)}"
        }

async def create_request(name: str, email: str, website: str = None, description: str = None, session: AsyncSession = None) -> Dict[str, Any]:
    """
    Insert a new request into the database.
    
    Args:
        name (str): Name of the person making the request
        email (str): Email address of the requester
        website (str, optional): Website URL of the requester
        description (str, optional): Description of the request
        session (AsyncSession): SQLAlchemy async session
        
    Returns:
        Dict[str, Any]: Dictionary with success status and request data
    """
    logger.info(f"Creating new request from {name} ({email})")
    
    if not name:
        logger.error("Name is required for creating request")
        return {
            "success": False,
            "error": "Name is required"
        }
    
    if not email:
        logger.error("Email address is required for creating request")
        return {
            "success": False,
            "error": "Email address is required"
        }
    
    try:
        # Create new request
        new_request = Request(
            name=name,
            email=email,
            website=website,
            description=description
        )
        session.add(new_request)
        await session.commit()
        
        # Refresh to get the auto-generated ID and timestamp
        await session.refresh(new_request)
        
        logger.info(f"Successfully created request with ID: {new_request.id} for {name} ({email})")
        
        return {
            "success": True,
            "data": {
                "id": new_request.id,
                "name": new_request.name,
                "email": new_request.email,
                "website": new_request.website,
                "description": new_request.description,
                "created_at": new_request.created_at
            }
        }
        
    except SQLAlchemyError as e:
        await session.rollback()
        logger.error(f"Database error creating request: {str(e)}", exc_info=True)
        return {
            "success": False,
            "error": f"Database error: {str(e)}"
        }
    
    except Exception as e:
        await session.rollback()
        logger.error(f"Unexpected error creating request: {str(e)}", exc_info=True)
        return {
            "success": False,
            "error": f"Unexpected error: {str(e)}"
        }


async def create_call_booking(
    name: str,
    email: str,
    phone_country_code: str,
    phone_number: str,
    brief: Optional[str] = None,
    session: AsyncSession = None,
) -> Dict[str, Any]:
    """
    Insert a new call booking into the database.

    Args:
        name (str): Name of the person booking the call
        email (str): Email address of the requester
        phone_country_code (str): Country code of the phone number
        phone_number (str): Phone number without country code
        brief (str, optional): Optional brief or description from the user
        session (AsyncSession): SQLAlchemy async session

    Returns:
        Dict[str, Any]: Dictionary with success status and booking data
    """
    logger.info(f"Creating call booking from {name} ({email})")

    if not name:
        logger.error("Name is required for call booking")
        return {"success": False, "error": "Name is required"}

    if not email:
        logger.error("Email is required for call booking")
        return {"success": False, "error": "Email is required"}

    if not phone_country_code or not phone_number:
        logger.error("Phone (country_code and number) is required for call booking")
        return {"success": False, "error": "Phone is required"}

    try:
        new_booking = CallBooking(
            name=name,
            email=email,
            phone_country_code=phone_country_code,
            phone_number=phone_number,
            brief=brief,
        )
        session.add(new_booking)
        await session.commit()
        await session.refresh(new_booking)

        logger.info(f"Successfully created call booking with ID: {new_booking.id} for {name} ({email})")

        return {
            "success": True,
            "data": {
                "id": new_booking.id,
                "name": new_booking.name,
                "email": new_booking.email,
                "phone_country_code": new_booking.phone_country_code,
                "phone_number": new_booking.phone_number,
                "brief": new_booking.brief,
                "created_at": new_booking.created_at,
            },
        }

    except SQLAlchemyError as e:
        await session.rollback()
        logger.error(f"Database error creating call booking: {str(e)}", exc_info=True)
        return {"success": False, "error": f"Database error: {str(e)}"}

    except Exception as e:
        await session.rollback()
        logger.error(f"Unexpected error creating call booking: {str(e)}", exc_info=True)
        return {"success": False, "error": f"Unexpected error: {str(e)}"}


async def insert_cost_tracker(
    *,
    timestamp: datetime,
    model_name: str,
    context: Optional[str] = None,
    functionality: Optional[str] = None,
    agent_name: Optional[str] = None,
    chat_id: Optional[str] = None,
    user_id: Optional[str] = None,
    usage_metadata: Optional[Dict[str, Any]] = None,
    input_tokens: Optional[int] = None,
    output_tokens: Optional[int] = None,
    estimated_cost: Optional[float] = None,
    cost_details: Optional[Dict[str, Any]] = None,
    session: AsyncSession,
) -> Dict[str, Any]:
    """
    Insert one LLM cost/usage row into the ``costtracker`` table.

    Args:
        timestamp: When the LLM call occurred (UTC-aware datetime).
        model_name: Model id / label (e.g. ``gpt-4o``).
        context: Call-site label (e.g. ``card_utils.generate_drl``).
        functionality: Stable user-facing bucket (e.g. ``report_generation``).
        agent_name: Agent / stage that made the call.
        chat_id: Optional chat / session id.
        user_id: Optional user id.
        usage_metadata: Full provider usage blob to store as JSONB.
        input_tokens: Prompt / input token count (defaults to 0).
        output_tokens: Completion / output token count (defaults to 0).
        estimated_cost: Estimated USD cost (``payload.cost.estimated_cost_usd``).
        cost_details: Full cost breakdown JSON (``payload.cost``).
        session: SQLAlchemy async session.

    Returns:
        Dict with ``success`` and either ``data`` (row fields) or ``error``.
    """
    if not model_name:
        logger.error("model_name is required for costtracker insert")
        return {"success": False, "error": "model_name is required"}

    if timestamp is None:
        logger.error("timestamp is required for costtracker insert")
        return {"success": False, "error": "timestamp is required"}

    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)

    try:
        row = CostTracker(
            timestamp=timestamp,
            model_name=model_name,
            context=context,
            functionality=functionality,
            agent_name=agent_name,
            chat_id=chat_id,
            user_id=user_id,
            usage_metadata=usage_metadata,
            input_tokens=int(input_tokens or 0),
            output_tokens=int(output_tokens or 0),
            estimated_cost=float(estimated_cost) if estimated_cost is not None else None,
            cost_details=cost_details,
            created_at=datetime.now(timezone.utc),
        )
        session.add(row)
        await session.commit()
        await session.refresh(row)

        logger.info(
            f"Inserted costtracker row id={row.id} model={model_name} "
            f"context={context} input_tokens={row.input_tokens} "
            f"output_tokens={row.output_tokens} estimated_cost={row.estimated_cost}"
        )
        return {
            "success": True,
            "data": {
                "id": row.id,
                "timestamp": row.timestamp,
                "model_name": row.model_name,
                "context": row.context,
                "functionality": row.functionality,
                "agent_name": row.agent_name,
                "chat_id": row.chat_id,
                "user_id": row.user_id,
                "usage_metadata": row.usage_metadata,
                "input_tokens": row.input_tokens,
                "output_tokens": row.output_tokens,
                "estimated_cost": row.estimated_cost,
                "cost_details": row.cost_details,
                "created_at": row.created_at,
            },
        }

    except SQLAlchemyError as e:
        await session.rollback()
        logger.error(f"Database error inserting costtracker row: {str(e)}", exc_info=True)
        return {"success": False, "error": f"Database error: {str(e)}"}

    except Exception as e:
        await session.rollback()
        logger.error(f"Unexpected error inserting costtracker row: {str(e)}", exc_info=True)
        return {"success": False, "error": f"Unexpected error: {str(e)}"}


async def get_latest_card_version(session: AsyncSession, card_id: str) -> Dict[str, Any]:
    """
    Get the latest version of a card from the database.
    
    Args:
        session (AsyncSession): SQLAlchemy async session
        card_id (str): The ID of the card to get the latest version of
        
    Returns:
        Dict[str, Any]: Dictionary with success status and card data
    """
    logger.info(f"Getting latest version of card with ID: {card_id}")
    try:
        # Query to get the latest version of the card
        stmt = select(Card).where(Card.card_id == card_id, Card.is_active == True, Card.is_deleted == False)
        result = await session.execute(stmt)
        card = result.scalar_one_or_none()
        
        if not card:
            logger.warning(f"No card found with ID: {card_id}")
            return {
                "success": True,
                "version": None
            }
        
        return {
            "success": True,
            "version": card.version
        }
    except SQLAlchemyError as e:
        logger.error(f"Database error getting latest card version: {str(e)}", exc_info=True)
        return {
            "success": False,
            "error": f"Database error: {str(e)}"
        }
    except Exception as e:
        logger.error(f"Unexpected error getting latest card version: {str(e)}", exc_info=True)
        return {
            "success": False,
            "error": f"Unexpected error: {str(e)}"
        }


async def get_dashboard_stats(user_id: str, session: AsyncSession) -> dict:
    """
    Get dashboard statistics for a user.
    
    Returns counts of reports/chats in different states:
    - total_draft: Count of chats that exist in messages table but NOT in reports table
    - analysis_completed: Count of reports with status ANALYSIS_COMPLETED
    - output_generated: Count of reports with status OUTPUT_GENERATED
    - updated: Static value 0 (TODO: implement when update functionality is enabled)
    
    Args:
        user_id (str): User ID
        session (AsyncSession): Database session
    
    Returns:
        dict: Statistics data with counts for each status
    """
    try:
        logger.info(f"Getting dashboard stats for user {user_id}")
        
        # Get count of draft chats (chats in messages table but NOT in reports table)
        draft_stmt = (
            select(func.count(Message.id))
            .outerjoin(Report, Message.id == Report.chat_id)
            .where(
                Message.user_id == user_id,
                ~Message.is_deleted,
                Report.id.is_(None)  # No corresponding report exists
            )
        )
        result = await session.execute(draft_stmt)
        draft_count = result.scalar() or 0
        
        logger.info(f"Found {draft_count} draft chats for user {user_id}")
        
        # Get count of analysis completed reports
        analysis_completed_stmt = (
            select(func.count(Report.id))
            .join(Message, Report.chat_id == Message.id)
            .where(
                Message.user_id == user_id,
                ~Message.is_deleted,
                Report.status == ReportStatus.ANALYSIS_COMPLETED.value
            )
        )
        result = await session.execute(analysis_completed_stmt)
        analysis_completed_count = result.scalar() or 0
        
        logger.info(f"Found {analysis_completed_count} analysis-completed reports for user {user_id}")
        
        # Get count of output generated reports
        output_generated_stmt = (
            select(func.count(Report.id))
            .join(Message, Report.chat_id == Message.id)
            .where(
                Message.user_id == user_id,
                ~Message.is_deleted,
                Report.status == ReportStatus.OUTPUT_GENERATED.value
            )
        )
        result = await session.execute(output_generated_stmt)
        output_generated_count = result.scalar() or 0
        
        logger.info(f"Found {output_generated_count} output-generated reports for user {user_id}")
        
        # TODO: Implement logic for updated reports when update functionality is enabled
        return {
            "success": True,
            "drafts": draft_count,
            "analysis_completed": analysis_completed_count,
            "output_generated": output_generated_count,
            "updates": 0  # TODO: implement when update functionality is enabled
        }
        
    except Exception as e:
        logger.error(f"Error getting dashboard stats: {str(e)}", exc_info=True)
        return {
            "success": False,
            "error": f"Error getting dashboard stats: {str(e)}"
        }


# Domain slug → human-readable display name
DOMAIN_DISPLAY_NAMES: Dict[str, str] = {
    "default": "General",
    "primary_research": "Primary Research",
    "due_diligence": "Due Diligence",
    "industry_benchmarking": "Industry Benchmarking",
    "market_insight": "Market Insight",
    "rfp": "RFP",
    "business_plan": "Business Plan",
}

# Statuses considered "not draft" (i.e. a real report has been started)
_NON_DRAFT_STATUSES = [s.value for s in ReportStatus if s != ReportStatus.DRAFT]


async def get_report_domain_summary(user_id: str, session: AsyncSession) -> Dict[str, Any]:
    """Return a per-domain summary of non-draft reports for a user.

    Only domains that have at least one non-draft report are included.

    Args:
        user_id: Authenticated user's ID.
        session: SQLAlchemy async session.

    Returns:
        Dict with keys:
            - ``success`` (bool)
            - ``domains`` (list[dict]): Each item has ``domain_name``,
              ``category_name`` (display label), and ``item_count``.
            - ``error`` (str, optional)
    """
    logger.info(f"Fetching report domain summary for user_id: {user_id}")

    try:
        stmt = (
            select(
                Report.domain_name,
                func.count(Report.id).label("item_count"),
            )
            .join(Message, Report.chat_id == Message.id)
            .where(
                Message.user_id == user_id,
                ~Message.is_deleted,
                Report.status.in_(_NON_DRAFT_STATUSES),
            )
            .group_by(Report.domain_name)
            .order_by(func.count(Report.id).desc())
        )

        result = await session.execute(stmt)
        rows = result.all()

        domains = []
        for row in rows:
            slug = row.domain_name or "default"
            domains.append(
                {
                    "domain_name": slug,
                    "category_name": DOMAIN_DISPLAY_NAMES.get(slug, slug.replace("_", " ").title()),
                    "item_count": row.item_count,
                }
            )

        logger.info(
            f"Domain summary for user_id: {user_id} — {len(domains)} domain(s) with non-draft reports"
        )
        return {"success": True, "domains": domains}

    except SQLAlchemyError as e:
        logger.error(f"DB error in get_report_domain_summary: {str(e)}", exc_info=True)
        return {"success": False, "error": f"Database error: {str(e)}"}
    except Exception as e:
        logger.error(f"Unexpected error in get_report_domain_summary: {str(e)}", exc_info=True)
        return {"success": False, "error": f"Unexpected error: {str(e)}"}


async def get_reports_by_domain(
    user_id: str,
    domain_name: str,
    limit: int,
    offset: int,
    session: AsyncSession,
) -> Dict[str, Any]:
    """Return paginated non-draft reports for a user filtered by domain.

    Results are ordered by ``last_activity_at`` descending (most recent first).

    Args:
        user_id: Authenticated user's ID.
        domain_name: Domain slug to filter by (e.g. ``"due_diligence"``).
            Pass ``"default"`` for reports with no specific domain.
        limit: Maximum number of records to return.
        offset: Number of records to skip (for pagination).
        session: SQLAlchemy async session.

    Returns:
        Dict with keys:
            - ``success`` (bool)
            - ``domain_name`` (str): Slug echoed back.
            - ``category_name`` (str): Human-readable display label.
            - ``total`` (int): Total non-draft reports in this domain (for the user).
            - ``limit`` (int): Applied limit.
            - ``offset`` (int): Applied offset.
            - ``reports`` (list[dict]): Paginated report records.
            - ``error`` (str, optional)
    """
    logger.info(
        f"Fetching reports for user_id: {user_id}, domain_name: {domain_name}, "
        f"limit: {limit}, offset: {offset}"
    )

    try:
        # Normalise: treat NULL domain_name in DB as "default"
        domain_filter = (
            Report.domain_name.is_(None)
            if domain_name == "default"
            else Report.domain_name == domain_name
        )

        base_where = [
            Message.user_id == user_id,
            ~Message.is_deleted,
            Report.status.in_(_NON_DRAFT_STATUSES),
            domain_filter,
        ]

        # Total count for pagination metadata
        count_stmt = (
            select(func.count(Report.id))
            .join(Message, Report.chat_id == Message.id)
            .where(*base_where)
        )
        total_result = await session.execute(count_stmt)
        total = total_result.scalar() or 0

        # Paginated records
        reports_stmt = (
            select(Report)
            .join(Message, Report.chat_id == Message.id)
            .where(*base_where)
            .order_by(Report.last_activity_at.desc())
            .limit(limit)
            .offset(offset)
        )
        reports_result = await session.execute(reports_stmt)
        reports = reports_result.scalars().all()

        reports_data = [
            {
                "report_id": r.id,
                "chat_id": r.chat_id,
                "title": r.title,
                "status": r.status,
                "domain_name": r.domain_name or "default",
                "poster_image_url": r.poster_image_url,
                "current_version": r.current_version,
                "created_at": r.created_at.isoformat() if r.created_at else None,
                "last_activity_at": r.last_activity_at.isoformat() if r.last_activity_at else None,
                "generated_at": r.generated_at.isoformat() if r.generated_at else None,
                "s3_uri": r.s3_uri,
            }
            for r in reports
        ]

        logger.info(
            f"Returning {len(reports_data)} of {total} reports for "
            f"user_id: {user_id}, domain_name: {domain_name}"
        )
        return {
            "success": True,
            "domain_name": domain_name,
            "category_name": DOMAIN_DISPLAY_NAMES.get(domain_name, domain_name.replace("_", " ").title()),
            "total": total,
            "limit": limit,
            "offset": offset,
            "reports": reports_data,
        }

    except SQLAlchemyError as e:
        logger.error(f"DB error in get_reports_by_domain: {str(e)}", exc_info=True)
        return {"success": False, "error": f"Database error: {str(e)}"}
    except Exception as e:
        logger.error(f"Unexpected error in get_reports_by_domain: {str(e)}", exc_info=True)
        return {"success": False, "error": f"Unexpected error: {str(e)}"}


# ============================================================================
# Categories / Home search / Ongoing chats
# ============================================================================

# Fixed display order for the homepage category catalog. Internal slugs are
# preserved here; the route layer renames ``default`` to ``standard`` when
# returning to clients.
_CATEGORY_DISPLAY_ORDER = [
    "default",
    "primary_research",
    "due_diligence",
    "industry_benchmarking",
    "market_insight",
    "rfp",
    "business_plan",
]

# External-facing alias for the internal ``default`` domain.
STANDARD_CATEGORY_SLUG = "standard"
DEFAULT_DOMAIN_INTERNAL = "default"


def _to_external_domain(internal_slug: Optional[str]) -> str:
    """Translate the internal ``default`` slug (or NULL) to ``standard``.

    Every other slug is returned unchanged.
    """
    if not internal_slug or internal_slug == DEFAULT_DOMAIN_INTERNAL:
        return STANDARD_CATEGORY_SLUG
    return internal_slug


def _to_internal_domain(external_slug: str) -> str:
    """Reverse of :func:`_to_external_domain` — used for filter parameters."""
    if external_slug == STANDARD_CATEGORY_SLUG:
        return DEFAULT_DOMAIN_INTERNAL
    return external_slug


def list_all_categories() -> Dict[str, Any]:
    """Return the static catalog of report categories.

    This is intentionally not user-scoped: every authenticated user sees the
    same set of categories on the homepage. ``default`` is surfaced as
    ``standard`` in the response.
    """
    return {
        "success": True,
        "categories": [
            {
                "slug": _to_external_domain(slug),
                "display_name": DOMAIN_DISPLAY_NAMES.get(
                    slug, slug.replace("_", " ").title()
                ),
            }
            for slug in _CATEGORY_DISPLAY_ORDER
        ],
    }


def _latest_report_per_chat_subq():
    """Build a subquery that selects the latest report per chat.

    Returned columns:
        chat_id, report_id, report_title, status, domain_name, poster_image_url

    Uses a window function so PostgreSQL can resolve it in a single pass.
    Defined here so both home-search and ongoing-chats stay aligned on what
    "the chat's latest report" means.
    """
    rn = func.row_number().over(
        partition_by=Report.chat_id,
        order_by=Report.created_at.desc(),
    ).label("rn")

    inner = (
        select(
            Report.chat_id.label("chat_id"),
            Report.id.label("report_id"),
            Report.title.label("report_title"),
            Report.status.label("status"),
            Report.domain_name.label("domain_name"),
            Report.poster_image_url.label("poster_image_url"),
            rn,
        )
    ).subquery()

    return (
        select(
            inner.c.chat_id,
            inner.c.report_id,
            inner.c.report_title,
            inner.c.status,
            inner.c.domain_name,
            inner.c.poster_image_url,
        )
        .where(inner.c.rn == 1)
        .subquery()
    )


async def search_chats_and_reports(
    user_id: str,
    session: AsyncSession,
    query: Optional[str] = None,
    domains: Optional[list] = None,
    statuses: Optional[list] = None,
    created_after: Optional[datetime] = None,
    created_before: Optional[datetime] = None,
    limit: int = 20,
    offset: int = 0,
) -> Dict[str, Any]:
    """Search the user's chats + latest reports for the homepage search bar.

    A hit is one row per chat. ``query`` matches case-insensitively against
    the chat title and (if present) the latest report's title. Chats without
    a report have an effective status of ``draft`` and an effective domain
    of ``default`` (returned as ``standard``).

    Args:
        user_id: Authenticated user's ID.
        session: SQLAlchemy async session.
        query: Optional case-insensitive substring to match titles against.
        domains: Optional list of external domain slugs to filter by; pass
            ``"standard"`` to match the internal ``default``/NULL domain.
        statuses: Optional list of report status values to filter by; pass
            ``"draft"`` to include chats with no report.
        created_after: Lower bound (inclusive) on chat ``created_at``.
        created_before: Upper bound (inclusive) on chat ``created_at``.
        limit: Page size (1–100).
        offset: Pagination offset.

    Returns:
        Dict with ``success``, ``total``, ``limit``, ``offset``, ``items``.
    """
    logger.info(
        f"Home-search for user_id={user_id} q={query!r} domains={domains} "
        f"statuses={statuses} after={created_after} before={created_before} "
        f"limit={limit} offset={offset}"
    )

    try:
        latest = _latest_report_per_chat_subq()

        # Effective status / domain treat a chat with no report as draft/default
        effective_status = func.coalesce(latest.c.status, ReportStatus.DRAFT.value)
        effective_domain = func.coalesce(latest.c.domain_name, DEFAULT_DOMAIN_INTERNAL)

        where_clauses = [
            Message.user_id == user_id,
            Message.is_deleted.is_(False),
        ]

        if query:
            q_like = f"%{query.strip().lower()}%"
            where_clauses.append(
                or_(
                    func.lower(Message.chat_title).like(q_like),
                    func.lower(latest.c.report_title).like(q_like),
                )
            )

        if domains:
            internal_domains = [_to_internal_domain(d) for d in domains]
            where_clauses.append(effective_domain.in_(internal_domains))

        if statuses:
            where_clauses.append(effective_status.in_(statuses))

        if created_after is not None:
            where_clauses.append(Message.created_at >= created_after)
        if created_before is not None:
            where_clauses.append(Message.created_at <= created_before)

        base_query = (
            select(
                Message.id.label("chat_id"),
                Message.chat_title.label("chat_title"),
                Message.created_at.label("created_at"),
                Message.updated_at.label("updated_at"),
                latest.c.report_id,
                latest.c.report_title,
                latest.c.poster_image_url,
                effective_status.label("eff_status"),
                effective_domain.label("eff_domain"),
            )
            .select_from(
                Message.__table__.outerjoin(latest, latest.c.chat_id == Message.id)
            )
            .where(*where_clauses)
        )

        # Total count (same filters, no limit/offset)
        count_stmt = select(func.count()).select_from(base_query.subquery())
        total = (await session.execute(count_stmt)).scalar() or 0

        # Paginated rows
        rows_stmt = (
            base_query.order_by(Message.updated_at.desc().nullslast())
            .limit(limit)
            .offset(offset)
        )
        rows = (await session.execute(rows_stmt)).all()

        q_lower = query.strip().lower() if query else None
        items = []
        for r in rows:
            matched_chat = bool(
                q_lower
                and r.chat_title
                and q_lower in r.chat_title.lower()
            )
            matched_report = bool(
                q_lower
                and r.report_title
                and q_lower in r.report_title.lower()
            )
            if matched_chat and matched_report:
                matched_on = "both"
            elif matched_report:
                matched_on = "report_title"
            else:
                matched_on = "chat_title"

            items.append(
                {
                    "chat_id": r.chat_id,
                    "chat_title": r.chat_title,
                    "report_id": r.report_id,
                    "report_title": r.report_title,
                    "status": r.eff_status,
                    "domain": _to_external_domain(r.eff_domain),
                    "poster_image_url": r.poster_image_url,
                    "created_at": r.created_at.isoformat() if r.created_at else None,
                    "updated_at": r.updated_at.isoformat() if r.updated_at else None,
                    "matched_on": matched_on,
                }
            )

        logger.info(
            f"Home-search returning {len(items)} of {total} hits for user_id={user_id}"
        )
        return {
            "success": True,
            "total": total,
            "limit": limit,
            "offset": offset,
            "items": items,
        }

    except SQLAlchemyError as e:
        logger.error(f"DB error in search_chats_and_reports: {str(e)}", exc_info=True)
        return {"success": False, "error": f"Database error: {str(e)}"}
    except Exception as e:
        logger.error(f"Unexpected error in search_chats_and_reports: {str(e)}", exc_info=True)
        return {"success": False, "error": f"Unexpected error: {str(e)}"}


async def get_draft_chats_grouped(
    user_id: str,
    session: AsyncSession,
) -> Dict[str, Any]:
    """Return the user's ongoing/draft chats, grouped by external category.

    A chat is "ongoing" when:
      - It has at least one report and that latest report's status is
        :pyattr:`ReportStatus.DRAFT`, OR
      - It has no report yet (the chat exists but no report has been
        generated). These are treated as drafts too.

    Drafts currently never carry a ``domain_name`` (the domain is written
    only after the report-generation tool call resolves), so today every
    item lands under the ``standard`` key. The grouping shape future-proofs
    the response so additional category keys can appear once drafts start
    carrying a domain.

    Returns:
        Dict with ``success`` and a ``groups`` dict keyed by external
        category slug, each value being a list of chat dicts ordered by
        ``updated_at`` desc.
    """
    logger.info(f"Fetching ongoing/draft chats for user_id={user_id}")

    try:
        latest = _latest_report_per_chat_subq()

        effective_status = func.coalesce(latest.c.status, ReportStatus.DRAFT.value)
        effective_domain = func.coalesce(latest.c.domain_name, DEFAULT_DOMAIN_INTERNAL)

        stmt = (
            select(
                Message.id.label("chat_id"),
                Message.chat_title.label("chat_title"),
                Message.created_at.label("created_at"),
                Message.updated_at.label("updated_at"),
                latest.c.poster_image_url,
                effective_status.label("eff_status"),
                effective_domain.label("eff_domain"),
            )
            .select_from(
                Message.__table__.outerjoin(latest, latest.c.chat_id == Message.id)
            )
            .where(
                Message.user_id == user_id,
                Message.is_deleted.is_(False),
                effective_status == ReportStatus.DRAFT.value,
            )
            .order_by(Message.updated_at.desc().nullslast())
        )

        rows = (await session.execute(stmt)).all()

        groups: Dict[str, list] = {}
        for r in rows:
            external_slug = _to_external_domain(r.eff_domain)
            groups.setdefault(external_slug, []).append(
                {
                    "chat_id": r.chat_id,
                    "chat_title": r.chat_title,
                    "status": r.eff_status,
                    "poster_image_url": r.poster_image_url,
                    "created_at": r.created_at.isoformat() if r.created_at else None,
                    "updated_at": r.updated_at.isoformat() if r.updated_at else None,
                }
            )

        logger.info(
            f"Found {len(rows)} draft chat(s) across {len(groups)} group(s) for "
            f"user_id={user_id}"
        )
        return {"success": True, "groups": groups}

    except SQLAlchemyError as e:
        logger.error(f"DB error in get_draft_chats_grouped: {str(e)}", exc_info=True)
        return {"success": False, "error": f"Database error: {str(e)}"}
    except Exception as e:
        logger.error(f"Unexpected error in get_draft_chats_grouped: {str(e)}", exc_info=True)
        return {"success": False, "error": f"Unexpected error: {str(e)}"}


async def get_all_version_outputs_list(report_id: str, session: AsyncSession) -> Dict[str, Any]:
    """
    Get all available outputs (non-null files) across all versions for a report.
    Returns a simple list of {version, file_type, generated_at} for frontend display.
    
    Args:
        report_id (str): The report ID
        session (AsyncSession): Database session
        
    Returns:
        Dict containing list of all available outputs
    """
    try:
        logger.info(f"Fetching all version outputs list for report_id: {report_id}")
        
        # Fetch all versions for the report, ordered by version descending
        versions_stmt = (
            select(ReportVersion)
            .where(ReportVersion.report_id == report_id)
            .order_by(ReportVersion.version.desc())
        )
        result = await session.execute(versions_stmt)
        versions = result.scalars().all()
        
        if not versions:
            logger.warning(f"No versions found for report_id: {report_id}")
            return {
                "success": False,
                "error": f"No versions found for report {report_id}"
            }
        
        # Build list of all available outputs
        outputs = []
        
        for version in versions:
            s3_uri = version.s3_uri or {}
            version_num = version.version
            generated_at = version.generated_at.isoformat() if version.generated_at else None
            
            # Check each file type and add if not null
            if s3_uri.get('pdf'):
                outputs.append({
                    "version": version_num,
                    "file_type": "pdf",
                    "s3_path": s3_uri.get('pdf'),
                    "generated_at": generated_at
                })
            
            if s3_uri.get('html') and ('html_explicit' not in s3_uri or s3_uri.get('html_explicit') == True):
                html_generated_at = s3_uri.get('html_generation_time') or generated_at
                outputs.append({
                    "version": version_num,
                    "file_type": "html",
                    "s3_path": s3_uri.get('html'),
                    "generated_at": html_generated_at
                })
            
            if s3_uri.get('md') and ('md_explicit' not in s3_uri or s3_uri.get('md_explicit') == True):
                md_generated_at = s3_uri.get('md_generation_time') or generated_at
                outputs.append({
                    "version": version_num,
                    "file_type": "md",
                    "s3_path": s3_uri.get('md'),
                    "generated_at": md_generated_at
                })
            
            if s3_uri.get('pptx'):
                # Use pptx_generation_time if available
                pptx_generated_at = s3_uri.get('pptx_generation_time') or generated_at
                outputs.append({
                    "version": version_num,
                    "file_type": "pptx",
                    "s3_path": s3_uri.get('pptx'),
                    "generated_at": pptx_generated_at
                })
            
            if s3_uri.get('info_pdf'):
                # Use info_pdf_generation_time if available
                info_pdf_generated_at = s3_uri.get('info_pdf_generation_time') or generated_at
                outputs.append({
                    "version": version_num,
                    "file_type": "info_pdf",
                    "s3_path": s3_uri.get('info_pdf'),
                    "generated_at": info_pdf_generated_at
                })
        
        logger.info(f"Successfully fetched {len(outputs)} output files for report_id: {report_id}")
        
        return {
            "success": True,
            "report_id": report_id,
            "outputs": outputs
        }
        
    except Exception as e:
        logger.error(f"Error fetching version outputs list for report {report_id}: {str(e)}", exc_info=True)
        return {
            "success": False,
            "error": str(e)
        }


async def get_version_file_for_download(report_id: str, version: int, file_type: str, session: AsyncSession) -> Dict[str, Any]:
    """
    Get S3 path for a specific file type in a specific version for download.
    
    Args:
        report_id (str): The report ID
        version (int): The version number
        file_type (str): The file type (pdf, html, md, pptx, info_pdf)
        session (AsyncSession): Database session
        
    Returns:
        Dict with s3_path or error
    """
    try:
        logger.info(f"Fetching {file_type} for report_id: {report_id}, version: {version}")
        
        # Validate file_type
        if file_type not in ['pdf', 'html', 'md', 'pptx', 'info_pdf']:
            return {
                "success": False,
                "error": f"Invalid file type: {file_type}. Must be one of: pdf, html, md, pptx, info_pdf"
            }
        
        # Fetch the specific version
        version_stmt = (
            select(ReportVersion)
            .where(
                ReportVersion.report_id == report_id,
                ReportVersion.version == version
            )
        )
        result = await session.execute(version_stmt)
        report_version = result.scalar_one_or_none()
        
        if not report_version:
            logger.warning(f"Version {version} not found for report_id: {report_id}")
            return {
                "success": False,
                "error": f"Version {version} not found for this report"
            }
        
        # Get s3_uri
        s3_uri = report_version.s3_uri or {}
        s3_path = s3_uri.get(file_type)
        
        if not s3_path:
            logger.warning(f"{file_type} not found for report_id: {report_id}, version: {version}")
            return {
                "success": False,
                "error": f"{file_type.upper()} file not found for version {version}"
            }
        
        logger.info(f"Successfully fetched {file_type} path for version {version}")
        return {
            "success": True,
            "report_id": report_id,
            "version": version,
            "file_type": file_type,
            "s3_path": s3_path
        }
        
    except Exception as e:
        logger.error(f"Error fetching {file_type} for report {report_id} version {version}: {str(e)}", exc_info=True)
        return {
            "success": False,
            "error": str(e)
        }


async def get_report_version_history_data(
    report_id: str,
    session: AsyncSession
) -> Dict[str, Any]:
    """
    Get version history data for a report.
    Returns all versions with their output status for the dropdown.
    
    Args:
        report_id: ID of the report
        session: Database session
        
    Returns:
        Dict with success status, s3_base_path, and versions list
    """
    try:
        logger.info(f"Getting version history for report_id: {report_id}")
        
        # Verify report exists
        report_stmt = select(Report).where(Report.id == report_id)
        report_result = await session.execute(report_stmt)
        report = report_result.scalar_one_or_none()
        
        if not report:
            return {"success": False, "error": "Report not found"}
        
        # Get all versions ordered by version descending (newest first)
        versions_stmt = (
            select(ReportVersion)
            .where(ReportVersion.report_id == report_id)
            .order_by(ReportVersion.version.desc())
        )
        versions_result = await session.execute(versions_stmt)
        versions = versions_result.scalars().all()
        
        if not versions:
            return {"success": False, "error": "No versions found for this report"}
        
        # Get the active version for is_modified check
        active_version = next((v for v in versions if v.is_active), None)
        
        # Check if cards have been modified since last output (for active version only)
        is_modified = False
        if active_version:
            # Check if any output exists
            s3_uri = active_version.s3_uri or {}
            has_any_output = (
                active_version.generated_at or
                s3_uri.get('md_generation_time') or
                s3_uri.get('html_generation_time') or
                s3_uri.get('info_pdf_generation_time') or
                s3_uri.get('pptx_generation_time')
            )
            
            if has_any_output:
                # Check for modifications
                detection_result = await detect_modified_cards(report_id=report_id, session=session)
                if detection_result.get('success'):
                    is_modified = detection_result.get('modified_count', 0) > 0
        
        # Build s3_base_path by extracting from any existing s3_uri
        # S3 path structure: s3://bucket/{base_path}/report_{report_id}/v{version}/...
        # We need to find the base path up to and including report_{report_id}/
        report_marker = f"report_{report_id}/"
        s3_base_path = None
        
        # Try to extract base path from any existing s3_uri in versions
        for version in versions:
            version_s3_uri = version.s3_uri or {}
            for key in ['pdf', 'html', 'md', 'pptx', 'info_pdf']:
                uri = version_s3_uri.get(key)
                if uri and report_marker in uri:
                    # Extract path up to and including report_{report_id}/
                    idx = uri.find(report_marker)
                    s3_base_path = uri[:idx + len(report_marker)].rstrip('/')
                    break
            if s3_base_path:
                break
        
        # Fallback if no existing URIs found
        if not s3_base_path:
            from src.config.constants import S3_BUCKET_NAME
            s3_base_path = f"s3://{S3_BUCKET_NAME}/report_{report_id}"
        
        # Helper to extract relative path from full S3 URI
        def get_relative_path(full_uri: str) -> str:
            if not full_uri:
                return None
            # Extract path after report_{report_id}/
            # e.g., "s3://bucket/.../report_rpt-123/v1/report/file.pdf" -> "v1/report/file.pdf"
            if report_marker in full_uri:
                return full_uri.split(report_marker)[-1]
            return None
        
        # Build version list
        versions_data = []
        for version in versions:
            s3_uri = version.s3_uri or {}
            
            # Format created_at as human-readable label
            created_at = version.created_at
            if created_at:
                # Format: "3rd Nov 2025, 2:30 PM"
                day = created_at.day
                suffix = 'th' if 11 <= day <= 13 else {1: 'st', 2: 'nd', 3: 'rd'}.get(day % 10, 'th')
                label = created_at.strftime(f"%-d{suffix} %b %Y, %-I:%M %p")
            else:
                label = "Unknown"
            
            version_data = {
                "report_version_id": version.id,
                "version": version.version,
                "is_current": version.is_active,
                "is_modified": is_modified if version.is_active else False,
                "label": label,
                "created_at": created_at.isoformat() if created_at else None,
                "generated_outputs": {
                    "pdf": {
                        "generated": bool(s3_uri.get('pdf')),
                        "s3_path": get_relative_path(s3_uri.get('pdf')),
                        "generated_at": version.generated_at.isoformat() if version.generated_at else None
                    },
                    "html": {
                        "generated": bool(s3_uri.get('html')) and ('html_explicit' not in s3_uri or s3_uri.get('html_explicit') == True),
                        "s3_path": get_relative_path(s3_uri.get('html')) if ('html_explicit' not in s3_uri or s3_uri.get('html_explicit') == True) else None,
                        "generated_at": (s3_uri.get('html_generation_time') or (version.generated_at.isoformat() if version.generated_at else None)) if ('html_explicit' not in s3_uri or s3_uri.get('html_explicit') == True) else None
                    },
                    "md": {
                        "generated": bool(s3_uri.get('md')) and ('md_explicit' not in s3_uri or s3_uri.get('md_explicit') == True),
                        "s3_path": get_relative_path(s3_uri.get('md')) if ('md_explicit' not in s3_uri or s3_uri.get('md_explicit') == True) else None,
                        "generated_at": (s3_uri.get('md_generation_time') or (version.generated_at.isoformat() if version.generated_at else None)) if ('md_explicit' not in s3_uri or s3_uri.get('md_explicit') == True) else None
                    },
                    "info_pdf": {
                        "generated": bool(s3_uri.get('info_pdf')),
                        "s3_path": get_relative_path(s3_uri.get('info_pdf')),
                        "generated_at": s3_uri.get('info_pdf_generation_time')
                    },
                    "pptx": {
                        "generated": bool(s3_uri.get('pptx')),
                        "s3_path": get_relative_path(s3_uri.get('pptx')),
                        "generated_at": s3_uri.get('pptx_generation_time')
                    }
                }
            }
            versions_data.append(version_data)
        
        logger.info(f"Successfully retrieved {len(versions_data)} versions for report_id: {report_id}")
        return {
            "success": True,
            "report_id": report_id,
            "s3_base_path": s3_base_path,
            "versions": versions_data
        }
        
    except Exception as e:
        logger.error(f"Error getting version history: {str(e)}", exc_info=True)
        return {"success": False, "error": str(e)}


async def get_cards_for_version(
    report_id: str,
    version_id: str,
    session: AsyncSession
) -> Dict[str, Any]:
    """
    Get cards linked to a specific report version.
    Uses report_version_cards junction table to get the exact cards
    that were locked when the version was finalized.
    
    Args:
        report_id: ID of the report
        version_id: ID of the specific version
        session: Database session
        
    Returns:
        Dict with success status and list of cards
    """
    try:
        logger.info(f"Getting cards for version_id: {version_id}")

        # Single query: join through ReportVersionCard, order by the junction
        # table's sequence, and eager-load tables for each card. Previously this
        # path issued N extra queries (2 per card) via get_tables_for_card.
        cards_stmt = (
            select(Card)
            .options(selectinload(Card.tables))
            .join(ReportVersionCard, ReportVersionCard.parent_card_id == Card.id)
            .where(ReportVersionCard.report_version_id == version_id)
            .order_by(ReportVersionCard.sequence)
        )

        result = await session.execute(cards_stmt)
        cards = result.scalars().all()

        if not cards:
            logger.warning(f"No cards linked to version {version_id}, falling back to active cards")
            return await get_report_cards(report_id=report_id, session=session)

        cards_data = []
        for card in cards:
            tables_data = [
                {
                    "table_id": t.table_id,
                    "table_title": _normalize_table_title_value(t.table_title),
                    "table_markdown": t.table_markdown,
                    "s3_uri": t.base64_s3_uri,
                }
                for t in card.tables
            ]

            sub_sections_data = _normalize_subsections_for_response(card.sub_sections)

            if isinstance(card.content, dict):
                content_text = card.content.get('content', '')
                content_name = card.content.get('name', card.title or '')
                content_tables = _normalize_tables_for_response(card.content.get('tables', tables_data))
            else:
                content_text = card.content or ''
                content_name = card.title or ''
                content_tables = tables_data

            card_data = {
                "id": card.card_id,
                "report_id": card.report_id,
                "sequence": card.sequence,
                "citations": card.citations,
                "created_at": card.created_at.isoformat() if card.created_at else None,
                "summary": card.summary,
                "type": card.type,
                "section": [
                    {
                        "name": content_name,
                        "content": content_text,
                        "tables": content_tables
                    }
                ],
                "sub_sections": sub_sections_data
            }
            cards_data.append(card_data)

        logger.info(f"Successfully retrieved {len(cards_data)} cards for version {version_id}")
        return {
            "success": True,
            "cards": cards_data,
            "from_version_snapshot": True
        }
        
    except Exception as e:
        logger.error(f"Error getting cards for version: {str(e)}", exc_info=True)
        return {"success": False, "error": str(e)}


async def get_report_info_by_version(
    report_id: str,
    report_version_id: str,
    session: AsyncSession
) -> Dict[str, Any]:
    """
    Get report info for a specific version, including cards linked to that version.
    Returns data in the same format as get_chat_reports_and_cards.
    
    Args:
        report_id: ID of the report
        report_version_id: ID of the specific report version
        session: Database session
        
    Returns:
        Dict with success status and report data with cards
    """
    logger.info(f"Getting report info for report_id: {report_id}, version_id: {report_version_id}")
    
    if not report_id or not report_version_id:
        logger.error("Invalid report_id or report_version_id: empty value provided")
        return {
            "success": False,
            "error": "Invalid report ID or version ID: empty value provided"
        }
    
    try:
        # Get the report
        report_stmt = select(Report).where(Report.id == report_id)
        report_result = await session.execute(report_stmt)
        report = report_result.scalar_one_or_none()
        
        if not report:
            logger.warning(f"Report with ID {report_id} not found")
            return {
                "success": False,
                "error": f"Report with ID {report_id} not found"
            }
        
        # Get the specific version
        version_stmt = select(ReportVersion).where(
            ReportVersion.id == report_version_id,
            ReportVersion.report_id == report_id
        )
        version_result = await session.execute(version_stmt)
        version = version_result.scalar_one_or_none()
        
        if not version:
            logger.warning(f"Version with ID {report_version_id} not found for report {report_id}")
            return {
                "success": False,
                "error": f"Version with ID {report_version_id} not found"
            }
        
        # Get cards linked to this version via junction table
        cards_stmt = (
            select(Card)
            .join(ReportVersionCard, ReportVersionCard.parent_card_id == Card.id)
            .where(ReportVersionCard.report_version_id == report_version_id)
            .order_by(ReportVersionCard.sequence)
        )
        cards_result = await session.execute(cards_stmt)
        cards = cards_result.scalars().all()
        
        # If no cards in version snapshot, fall back to active cards
        if not cards:
            logger.info(f"No cards in version snapshot, falling back to active cards for report {report_id}")
            cards_stmt = select(Card).where(
                Card.report_id == report_id,
                Card.is_active == True,
                Card.is_deleted == False
            ).order_by(Card.sequence)
            cards_result = await session.execute(cards_stmt)
            cards = cards_result.scalars().all()
        
        # Convert cards to list of dictionaries (same format as get_chat_reports_and_cards)
        cards_data = []
        for card in cards:
            cards_data.append({
                "id": card.card_id,
                "title": card.title,
                "sequence": card.sequence,
                "content": card.content.get('content', '') if isinstance(card.content, dict) else card.content or '',
                "tables": card.content.get('tables', []) if isinstance(card.content, dict) else [],
                "sub_sections": card.sub_sections,
                "citations": card.citations,
                "summary": card.summary,
                "type": card.type
            })
        
        filtered_layout = await prune_report_layout_async(report.layout, cards_data)
        
        # Build report response (same format as get_chat_reports_and_cards)
        report_data = {
            "id": report.id,
            "version": version.version,
            "current_version": report.current_version,
            "poster_image_url": report.poster_image_url,
            "s3_uri": report.s3_uri,
            "status": report.status,
            "last_activity_at": report.last_activity_at.isoformat() if report.last_activity_at else None,
            "created_at": report.created_at.isoformat() if report.created_at else None,
            "generated_at": report.generated_at.isoformat() if report.generated_at else None,
            "title": report.title,
            "report_layout": filtered_layout,
            "summary": report.summary,
            "length": report.length,
            "domain_name": report.domain_name,
            "citations": report.citations,
            "cards": cards_data
        }
        
        logger.info(f"Successfully retrieved report info for report_id: {report_id}, version_id: {report_version_id}")
        return {
            "success": True,
            "reports": [report_data]
        }
    
    except SQLAlchemyError as e:
        logger.error(f"Database error retrieving report info: {str(e)}", exc_info=True)
        return {
            "success": False,
            "error": f"Database error: {str(e)}"
        }
    
    except Exception as e:
        logger.error(f"Unexpected error retrieving report info: {str(e)}", exc_info=True)
        return {
            "success": False,
            "error": f"Unexpected error: {str(e)}"
        }


async def get_refinement_history(report_id: str, session: AsyncSession) -> Dict[str, Any]:
    """
    Fetch the current refinement history for a report from the refinement_history table.
    
    Args:
        report_id (str): ID of the report.
        session (AsyncSession): SQLAlchemy async session.
    
    Returns:
        Dict[str, Any]: Dictionary with success status and refine_history (list or None).
    """
    logger.info(f"Fetching refinement history for report_id: {report_id}")
    
    try:
        stmt = select(RefinementHistory).where(RefinementHistory.report_id == report_id)
        result = await session.execute(stmt)
        refinement_record = result.scalar_one_or_none()
        
        if refinement_record is None:
            logger.warning(f"No refinement history record found for report_id: {report_id}")
            return {
                "success": True,
                "refine_history": []
            }
        
        refine_history = refinement_record.refine_history or []
        logger.info(f"Found refinement history with {len(refine_history)} entries for report_id: {report_id}")
        return {
            "success": True,
            "refine_history": refine_history
        }
    
    except SQLAlchemyError as e:
        logger.error(f"Database error fetching refinement history: {str(e)}", exc_info=True)
        return {"success": False, "error": f"Database error: {str(e)}"}
    except Exception as e:
        logger.error(f"Unexpected error fetching refinement history: {str(e)}", exc_info=True)
        return {"success": False, "error": f"Unexpected error: {str(e)}"}


async def update_refinement_history(report_id: str, refine_history: list, session: AsyncSession) -> Dict[str, Any]:
    """
    Update the refinement history JSONB field for a report.
    
    Note: This function uses flush() instead of commit() to allow the caller 
    to control transaction boundaries. The caller MUST commit or rollback.
    
    Args:
        report_id (str): ID of the report.
        refine_history (list): Updated refinement history list to store.
        session (AsyncSession): SQLAlchemy async session.
    
    Returns:
        Dict[str, Any]: Dictionary with success status.
    """
    logger.info(f"Updating refinement history for report_id: {report_id} with {len(refine_history)} entries")
    
    try:
        stmt = select(RefinementHistory).where(RefinementHistory.report_id == report_id)
        result = await session.execute(stmt)
        refinement_record = result.scalar_one_or_none()
        
        if refinement_record is None:
            logger.warning(f"No refinement history record found for report_id: {report_id}, creating one")
            refinement_record = RefinementHistory(
                id=str(uuid7()),
                report_id=report_id,
                refine_history=refine_history
            )
            session.add(refinement_record)
        else:
            refinement_record.refine_history = refine_history
        
        await session.flush()
        logger.info(f"Successfully staged refinement history update for report_id: {report_id}")
        return {"success": True}
    
    except SQLAlchemyError as e:
        logger.error(f"Database error updating refinement history: {str(e)}", exc_info=True)
        return {"success": False, "error": f"Database error: {str(e)}"}
    except Exception as e:
        logger.error(f"Unexpected error updating refinement history: {str(e)}", exc_info=True)
        return {"success": False, "error": f"Unexpected error: {str(e)}"}


async def get_latest_ask_caspr_version(
    report_id: str,
    section_id: str,
    session: AsyncSession,
    subsection_id: str = None
) -> int:
    """
    Get the latest version number from ask_caspr_chats for a specific section or subsection.
    
    - For section refinement (subsection_id=None): fetches MAX(version) where section_id matches
      AND subsection_id IS NULL, so subsection records don't affect section versioning.
    - For subsection refinement: fetches MAX(version) where subsection_id matches.
    
    Args:
        report_id (str): ID of the report.
        section_id (str): Business card_id identifying the section.
        session (AsyncSession): SQLAlchemy async session.
        subsection_id (str, optional): Subsection ID. None for section-level lookup.
    
    Returns:
        int: The latest version found, or 0 if no records exist.
    """
    try:
        stmt = select(func.max(AskCasprChat.version)).where(
            AskCasprChat.report_id == report_id,
            AskCasprChat.section_id == section_id
        )
        
        if subsection_id:
            # Subsection refinement: match specific subsection
            stmt = stmt.where(AskCasprChat.subsection_id == subsection_id)
        else:
            # Section refinement: only look at section-level rows (subsection_id IS NULL)
            stmt = stmt.where(AskCasprChat.subsection_id.is_(None))
        
        result = await session.execute(stmt)
        max_version = result.scalar_one_or_none()
        
        version = max_version if max_version is not None else 0
        logger.info(f"Latest Ask Caspr version for section_id: {section_id}, subsection_id: {subsection_id} = {version}")
        return version
    
    except Exception as e:
        logger.error(f"Error fetching latest Ask Caspr version: {str(e)}", exc_info=True)
        return 0


async def insert_ask_caspr_chat_entry(
    report_id: str,
    section_id: str,
    session: AsyncSession,
    subsection_id: str = None,
    version: int = 1,
    card_version: int = 1
) -> Dict[str, Any]:
    """
    Insert a new Ask Caspr chat entry with chat=NULL for a section or subsection.
    
    This is called after a successful refinement to invalidate old chats.
    The frontend always picks the latest created_at entry, so inserting a new row 
    with NULL chat effectively resets the conversation for that section/subsection.
    
    Note: This function uses flush() instead of commit() to allow the caller 
    to control transaction boundaries. The caller MUST commit or rollback.
    
    Args:
        report_id (str): ID of the report.
        section_id (str): Business card_id (cards.card_id) identifying the logical section.
        session (AsyncSession): SQLAlchemy async session.
        subsection_id (str, optional): Subsection ID. NULL for section-level invalidation.
        version (int): Version number of the section/subsection content. Defaults to 1.
        card_version (int): cards.version at the time of creation — links to the card timeline. Defaults to 1.
    
    Returns:
        Dict[str, Any]: Dictionary with success status and entry_id.
    """
    logger.info(f"Inserting Ask Caspr chat entry for report_id: {report_id}, section_id: {section_id}, subsection_id: {subsection_id}, version: {version}, card_version: {card_version}")
    
    try:
        new_entry = AskCasprChat(
            id=str(uuid7()),
            report_id=report_id,
            section_id=section_id,
            subsection_id=subsection_id,
            chat=None,
            version=version,
            card_version=card_version
        )
        session.add(new_entry)
        await session.flush()
        
        logger.info(f"Successfully staged Ask Caspr chat entry with id: {new_entry.id}")
        return {"success": True, "entry_id": new_entry.id}
    
    except SQLAlchemyError as e:
        logger.error(f"Database error inserting Ask Caspr chat entry: {str(e)}", exc_info=True)
        return {"success": False, "error": f"Database error: {str(e)}"}
    except Exception as e:
        logger.error(f"Unexpected error inserting Ask Caspr chat entry: {str(e)}", exc_info=True)
        return {"success": False, "error": f"Unexpected error: {str(e)}"}