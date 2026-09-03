"""HTTP routes for the leads bounded context.

Handlers moved verbatim from ``src/resources/routers/api.py`` during the
R-STRUCT-1 migration, which split that 10,257-line module across seven
contexts. Handler bodies are unchanged; only the import block and the
``APIRouter`` they attach to are new.
"""

"""api.py: Authentication API for the Casper backend"""
import asyncio

from fastapi import APIRouter
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from app.adapters.email import (
    send_book_call_email,
    send_request_confirmation_email,
    send_subscribe_confirmation_email,
)
from app.billing.schemas import (
    BookCallRequest,
    BookCallResponse,
    LiveSourcesResponse,
    RequestRequest,
    RequestResponse,
    SubscribeRequest,
    SubscribeResponse,
)
from app.core.constants import (
    BOOK_CALL_BCC_EMAILS,
    BOOK_CALL_SENDER_EMAIL,
    BOOK_CALL_SENDER_EMAIL_PASS,
    BOOK_CALL_TO_EMAILS,
    LIVE_SOURCES_COUNT,
    REQUEST_BCC_EMAILS,
    REQUEST_SENDER_EMAIL,
    REQUEST_SENDER_EMAIL_PASS,
    REQUEST_TO_EMAILS,
    SUBSCRIBER_BCC_EMAILS,
    SUBSCRIBER_CC_EMAILS,
    SUBSCRIBER_SENDER_EMAIL,
    SUBSCRIBER_SENDER_EMAIL_PASS,
    SUBSCRIBER_TO_EMAILS,
)
from app.core.db import async_session_scope
from app.core.logging import setup_logging
from app.core.redis import get_redis_instance
from app.leads.repository import (
    create_call_booking,
    create_request,
    create_subscriber,
)
from app.observability.cloudwatch_utils import insert_cloudwatch_logs_for_caspr_page

router = APIRouter()
logger = setup_logging(__file__)
redis_instance = get_redis_instance()


@router.post(
    "/subscribe",
    response_model=SubscribeResponse,
    status_code=200,
    summary="Subscribe to newsletter",
    description="Subscribe to newsletter with email address",
)
async def subscribe(data: SubscribeRequest):
    try:
        logger.info(f"Received subscription request for email: {data.email_id}")

        if not data.email_id:
            logger.error("Email address is required for subscription!")
            return JSONResponse(
                content={
                    "success": False,
                    "message": "Please provide a valid email address to subscribe.",
                },
                status_code=400,
            )

        # Insert subscriber into database
        async with async_session_scope() as session:
            db_response = await create_subscriber(email=data.email_id, session=session)

        if not db_response.get("success"):
            logger.error(f"Failed to insert subscriber into database: {db_response.get('error')}")
            return JSONResponse(
                content={
                    "success": False,
                    "message": "We encountered an issue while processing your subscription.",
                },
                status_code=500,
            )

        logger.info(
            f"Successfully inserted subscriber with ID: {db_response['data']['id']} for email: {data.email_id}"
        )

        # Send subscription confirmation email
        email_sent = await run_in_threadpool(
            send_subscribe_confirmation_email,
            subscriber_email=data.email_id,
            sender_email=SUBSCRIBER_SENDER_EMAIL,
            sender_password=SUBSCRIBER_SENDER_EMAIL_PASS,
            to_emails=SUBSCRIBER_TO_EMAILS,
            cc_emails=SUBSCRIBER_CC_EMAILS,
            bcc_emails=SUBSCRIBER_BCC_EMAILS,
        )

        # Use database timestamp for CloudWatch logs
        db_timestamp = db_response["data"]["created_at"].isoformat()

        try:
            cloudwatch_request_data = {
                "event": "Subscribe",
                "email": data.email_id,
                "subscriber_id": db_response["data"]["id"],
                "timestamp": db_timestamp,
            }
            asyncio.create_task(
                insert_cloudwatch_logs_for_caspr_page(request_data=cloudwatch_request_data)
            )
        except Exception as e:
            logger.error(f"Error inserting cloudwatch logs: {e!s} for {data.email_id}")

        if email_sent:
            logger.info(f"Subscription email sent successfully to {data.email_id}")
        else:
            logger.error(f"Failed to send subscription email to {data.email_id}")

        return SubscribeResponse(success=True, message="Successfully subscribed!")

    except Exception as e:
        logger.error(f"Error processing subscription: {e!s} for {data.email_id}")
        return JSONResponse(
            content={
                "success": False,
                "message": "We encountered an issue while processing your subscription.",
            },
            status_code=500,
        )


@router.post(
    "/request",
    response_model=RequestResponse,
    status_code=200,
    summary="Submit a request for Caspr.",
    description="Submit a request for Caspr. with client details",
)
async def submit_request(data: RequestRequest):
    try:
        logger.info(f"Received request from {data.name} ({data.email})")

        if not data.name:
            logger.error("Name is required for request!")
            return JSONResponse(
                content={"success": False, "message": "Please provide your name."}, status_code=400
            )

        if not data.email:
            logger.error("Email address is required for request!")
            return JSONResponse(
                content={"success": False, "message": "Please provide a valid email address."},
                status_code=400,
            )

        # Insert request into database
        async with async_session_scope() as session:
            db_response = await create_request(
                name=data.name,
                email=data.email,
                website=data.website,
                description=data.description,
                session=session,
            )

        if not db_response.get("success"):
            logger.error(f"Failed to insert request into database: {db_response.get('error')}")
            return JSONResponse(
                content={
                    "success": False,
                    "message": "We encountered an issue while processing your request.",
                },
                status_code=500,
            )

        logger.info(
            f"Successfully inserted request with ID: {db_response['data']['id']} for {data.name} ({data.email})"
        )

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
            bcc_emails=REQUEST_BCC_EMAILS,
        )

        # Use database timestamp for CloudWatch logs
        db_timestamp = db_response["data"]["created_at"].isoformat()

        try:
            cloudwatch_request_data = {
                "event": "Request",
                "name": data.name,
                "email": data.email,
                "website": data.website,
                "description": data.description,
                "request_id": db_response["data"]["id"],
                "timestamp": db_timestamp,
            }
            asyncio.create_task(
                insert_cloudwatch_logs_for_caspr_page(request_data=cloudwatch_request_data)
            )
        except Exception as e:
            logger.error(f"Error inserting cloudwatch logs: {e!s} for {data.email}")

        if email_sent:
            logger.info(
                f"Request confirmation email sent successfully for {data.name} ({data.email})"
            )
        else:
            logger.error(
                f"Failed to send request confirmation email for {data.name} ({data.email})"
            )

        return RequestResponse(success=True, message="Request submitted successfully!")

    except Exception as e:
        logger.error(f"Error processing request: {e!s} for {data.name} ({data.email})")
        return JSONResponse(
            content={
                "success": False,
                "message": "We encountered an issue while processing your request.",
            },
            status_code=500,
        )


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
                content={
                    "success": False,
                    "message": "Please provide your phone number (country code and number).",
                },
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
                content={
                    "success": False,
                    "message": "We encountered an issue while processing your request.",
                },
                status_code=500,
            )

        logger.info(
            f"Successfully inserted call booking with ID: {db_response['data']['id']} for {data.name} ({data.email})"
        )

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

        return BookCallResponse(
            success=True,
            message="Call booking submitted successfully. We will be in touch shortly.",
        )

    except Exception as e:
        logger.error(f"Error processing call booking: {e!s}")
        return JSONResponse(
            content={
                "success": False,
                "message": "We encountered an issue while processing your request.",
            },
            status_code=500,
        )


@router.get(
    "/live-sources",
    response_model=LiveSourcesResponse,
    status_code=200,
    summary="Get live sources count",
    description="Get the current count of live sources",
)
async def live_sources():
    try:
        logger.info("Received request to get live sources count")

        return LiveSourcesResponse(success=True, count=LIVE_SOURCES_COUNT)
    except Exception as e:
        logger.error(f"Error getting live sources count: {e!s}")
        return JSONResponse(
            content={
                "success": False,
                "message": "We're having trouble loading the live sources information.",
            },
            status_code=500,
        )
