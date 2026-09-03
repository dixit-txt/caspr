"""Database access for the auth bounded context.

Moved verbatim from ``src/db/async_db_functions.py`` during the R-STRUCT-1
migration. Function bodies are unchanged; only the import block was retargeted
at the new module paths.
"""

from datetime import datetime
from typing import Any, Dict

from sqlalchemy import or_, select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import setup_logging
from app.core.utils import verify_password
from app.models import (
    User,
)

logger = setup_logging(__file__)


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
