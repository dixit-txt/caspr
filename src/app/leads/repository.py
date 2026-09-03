"""Database access for the leads bounded context.

Moved verbatim from ``src/db/async_db_functions.py`` during the R-STRUCT-1
migration. Function bodies are unchanged; only the import block was retargeted
at the new module paths.
"""

from typing import Any, Dict, Optional

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import setup_logging
from app.models import (
    CallBooking, Request,
    Subscriber,
)

logger = setup_logging(__file__)


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
