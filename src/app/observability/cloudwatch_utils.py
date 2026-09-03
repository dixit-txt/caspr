import asyncio
from datetime import datetime, timezone
from app.adapters.cloudwatch import CloudwatchInstance
# from src.db.async_db_functions import 
import json
from src.db.async_db_functions import get_user_details
from app.core.logging import setup_logging
from app.core.db import async_session_scope

logger = setup_logging(__file__)

event_types = [
    "Signup",
    "Login",
    "Logout",
    "Chat started",
    "User input",
    "Caspr response",
    "Card generation started",
    "Card generation completed",
    "Report generation started",
    "Report generation completed",
    "Presentation generation started",
    "Presentation generation completed",
    # "Raw report generated",
    # "Final report generated",
    "Report downloaded",
    "Report sent via email",
    "Google login",
    "Google signup",
    "Forgot password",
    "Reset password link clicked",
    "Password reset",
    "Signup verification",
    "Account verified"
]


async def check_only_special(string):
    return all(not c.isalnum() for c in string) and bool(string)


async def insert_cloudwatch_logs(data: dict, user_id: str = None):
    try:
        logger.info(f"Inserting cloudwatch logs for user_id: {user_id}, event: {data.get('event')}")

        user_email = None
        phone_country_code = None
        chat_id = data.get('chat_id')
        chat_title = data.get('chat_title')
        if not user_id:
            raise Exception("User ID not found")

        event = data.get("event")
        if event not in event_types:
            raise Exception("Invalid event type")
            
        # get the user email and phone country code using user_id from the database
        async with async_session_scope() as session:
            db_response = await get_user_details(user_id=user_id, session=session)
            if not db_response.get('success', False):
                    raise Exception("Failed to get user details")
        
            if not db_response.get('user'):
                raise Exception("User not found")
            
            user_email = db_response.get('user', {}).get('email')
            user_name = db_response.get('user', {}).get('user_name')
            phone = db_response.get('user', {}).get('phone')
            is_google_verified = db_response.get('user', {}).get('is_google_verified')
            if is_google_verified:
                phone_country_code = "N/A"
            else:
                phone_country_code = db_response.get('user', {}).get('phone_country_code')


        if not user_email:
            raise Exception("User email not found")
        if not phone_country_code:
            raise Exception("Phone country code not found")
        if not data.get('timestamp'):
            raise Exception("Timestamp not found")

        request_data = {
            "email": user_email,
            "user_name": user_name,
            "phone_country_code": phone_country_code,
            "phone": phone,
            "event": event,
            "event_success": data.get('event_success'),
            "timestamp": data.get('timestamp', datetime.now(timezone.utc).isoformat()),
            "chat_id": chat_id if chat_id else "N/A",
            "chat_title": chat_title if chat_title else "N/A"
        }

        await CloudwatchInstance.add_log_messages(
            log_messages=[json.dumps(request_data)]
        )

        logger.info(f"Cloudwatch logs inserted for user id: {user_id}, event: {event}")
        
        return {"success": True}
    except Exception as e:
        logger.error(f"Error inserting cloudwatch logs for user_id: {user_id}, event: {data.get('event')}, error: {e}")
        return {"success": False, "error": str(e)}


async def insert_cloudwatch_logs_for_caspr_page(request_data: dict):
    try:
        logger.info(f"Inserting cloudwatch logs for event: {request_data.get('event')} and email: {request_data.get('email')}")

        if not request_data.get('event'):
            raise Exception("Event is required!")
        
        # request_data = {
        #     "name": name,
        #     "email": email,
        #     "phone": phone,
        #     "phone_country_code": phone_country_code,
        #     "report_title": report_title,
        #     "timestamp": datetime.now(timezone.utc).isoformat()
        # }
        
        await CloudwatchInstance.add_log_messages(
            log_messages=[json.dumps(request_data)]
        )
            
        logger.info(f"Successfully inserted cloudwatch logs for event: {request_data.get('event')} and email: {request_data.get('email')}")

    except Exception as e:
        logger.error(f"Error inserting cloudwatch logs: {e}")
        raise e