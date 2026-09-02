"""main.py: Main FastAPI application entry point"""
import asyncio
import json
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from src.resources.exceptions import http_exception_handler
from src.resources.routers import api
from src.resources.routers import wallet_api
# ask_caspr_api moved to ask-caspr-service — /api/v1/ask-caspr/* is served
# there now, not proxied through caspr-api yet. See caspr-api/README.md.
# upload_api moved to grep-service — /api/v1/upload/* is served there now,
# not proxied through caspr-api yet. See caspr-api/README.md → "Upload
# routes" for why, and what a reverse-proxy shim here would look like.
from src.resources.routers import onboarding_api
from src.resources.routers import admin_api
from src.resources.routers import mcp_api_key_api
from src.resources.routers import internal_db_api
from src.core.caspr_mcp.server import caspr_server, mcp_asgi_app
from src.config.log_helper import setup_logging
from src.config.constants import ALLOWED_ORIGINS, ENVIRONMENT
from src.core.cloudwatch_utils import CloudwatchInstance
from src.core.error_alerter import (
    ErrorAlertManager,
    _init_request_context,
    _consume_alert_traceback,
    _was_alert_already_queued,
    set_alert_request_context,
)
from src.core.redis_utils import get_redis_instance
from contextlib import asynccontextmanager

# Configure logging
logger = setup_logging(__file__)

# Initialise the error alerter (reuses the existing Redis singleton)
_redis = get_redis_instance()
error_alerter = ErrorAlertManager(redis_client=_redis.redis_client)


# Caspr MCP server (mounted in-process, served at /mcp). The streamable-http
# session manager must be run inside the parent app's lifespan.
mcp_app = mcp_asgi_app()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    logger.info("Starting up...")
    await CloudwatchInstance.connect()
    logger.info("Cloudwatch connected...")
    digest_task = asyncio.create_task(error_alerter.start_digest_loop())
    logger.info("Error digest loop started...")
    # Run the mounted MCP server's session manager alongside our lifespan.
    async with caspr_server.session_manager.run():
        logger.info("Caspr MCP server mounted at /mcp ...")
        yield
    # Shutdown
    logger.info("Shutting down...")
    digest_task.cancel()
    try:
        await digest_task
    except asyncio.CancelledError:
        pass
    await CloudwatchInstance.close()
    logger.info("Cloudwatch closed...")

# Create FastAPI app
app = FastAPI(
    title="Casper API",
    description="Casper - Report AI Search Assistant",
    version="1.0.0",
    lifespan=lifespan
)

@app.exception_handler(HTTPException)
async def app_http_exception_handler(request: Request, exc: HTTPException):
    """Return the intended HTTP status (403, 401, etc.) with a consistent JSON body."""
    return await http_exception_handler(request, exc)


# Global exception handler for validation errors
@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    """
    Global exception handler for validation errors.
    Formats the error response in a consistent way.
    """
    errors = exc.errors()
    error_messages = []
    
    for error in errors:
        # Extract field and error message
        field = ".".join(str(loc) for loc in error["loc"][1:]) if len(error["loc"]) > 1 else ""
        message = error["msg"]
        
        # Check for email validation errors
        if "email" in field.lower() and "not a valid email" in message.lower():
            logger.error(f"Invalid email format: {error}")
            return JSONResponse(
                status_code=422,
                content={"success": False, "error": "Invalid email format"}
            )
        
        error_messages.append(f"{field}: {message}" if field else message)
    
    # General validation error
    logger.error(f"Validation error: {error_messages}")
    return JSONResponse(
        status_code=422,
        content={"success": False, "error": "Validation error: " + "; ".join(error_messages)}
    )

# Add CORS middleware — restrict to known frontend origins per environment
logger.info(f"Configuring CORS for {ENVIRONMENT} environment with origins: {ALLOWED_ORIGINS}")
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "Accept", "Origin", "X-Requested-With", "X-API-Key"],
)


@app.middleware("http")
async def error_alert_middleware(request: Request, call_next):
    """Capture HTTP 500+ responses and push them to the error alert queue.

    The middleware seeds the alert context (method, path, user identity)
    BEFORE the handler runs so the logging filter can attribute any inner
    ``logger.error()`` calls to this request even when the handler swallows
    the exception and returns 200.

    If a 5xx response still escapes, we queue here as a safety net — but only
    when the filter hasn't already queued the same crash (see
    ``_was_alert_already_queued``).
    """
    # Set up a shared mutable box so the logging filter (running inside the
    # handler's except block) can pass the exception traceback back to us
    # AND auto-queue inner errors that don't propagate to a 500 response.
    _init_request_context()

    # --- User identity resolution (done up front so inner logger.error
    # captures carry the user_id even before the handler returns). ---
    user_id = None
    user_email = None
    try:
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            from jose import jwt as _jwt
            token = auth_header.split(" ", 1)[1]
            claims = _jwt.get_unverified_claims(token)
            user_id = claims.get("sub") or claims.get("user_id")
    except Exception:
        pass

    # For pre-auth endpoints (login / register / verify) the JWT is not yet
    # available.  Sniff the cached request body for an email / phone so the
    # digest shows *who* tried the failing operation.
    if not user_id:
        try:
            body_bytes_preview = await request.body()
            if body_bytes_preview:
                # Re-stash the cached body for the downstream handler to read.
                request._body = body_bytes_preview  # type: ignore[attr-defined]
                body_data = json.loads(body_bytes_preview)
                raw_email = body_data.get("email")
                raw_id = raw_email or body_data.get("phone_number") or body_data.get("username")
                if raw_id:
                    user_id = f"[pre-auth] {raw_id}"
                if raw_email:
                    user_email = raw_email
        except Exception:
            pass

    set_alert_request_context(
        method=request.method,
        path=request.url.path,
        user_id=user_id,
        user_email=user_email,
        query_params=str(request.query_params) if request.query_params else "",
    )

    response = await call_next(request)

    if response.status_code >= 500:
        try:
            # Collect the response body (streaming — must be consumed once).
            body_bytes = b""
            async for chunk in response.body_iterator:
                body_bytes += chunk

            error_message = ""
            try:
                body_json = json.loads(body_bytes)
                error_message = body_json.get("error") or body_json.get("detail") or str(body_json)
            except Exception:
                error_message = body_bytes.decode(errors="replace")[:500]

            # If the logging filter already queued this crash (via a
            # ``logger.error`` in the handler), skip — we'd otherwise produce
            # two digest rows for the same exception with different paths.
            if not _was_alert_already_queued():
                query_params = str(request.query_params) if request.query_params else ""
                short_tb = _consume_alert_traceback()

                await error_alerter.queue_error(
                    method=request.method,
                    path=request.url.path,
                    status_code=response.status_code,
                    error_message=error_message,
                    query_params=query_params,
                    user_id=user_id,
                    user_email=user_email,
                    exc_traceback=short_tb,
                )

            # Reconstruct the response with the already-consumed body.
            return Response(
                content=body_bytes,
                status_code=response.status_code,
                headers=dict[str, str](response.headers),
                media_type=response.media_type,
            )
        except Exception:
            logger.warning("error_alert_middleware failed silently", exc_info=True)

    return response

# Include routers
app.include_router(api.router, prefix="/api/v1", tags=["Chat"])
app.include_router(wallet_api.router, prefix="/api/v1", tags=["Wallet"])
app.include_router(onboarding_api.router, prefix="/api/v1", tags=["Onboarding"])
app.include_router(admin_api.router, prefix="/api/v1", tags=["Admin Dashboard"])
app.include_router(mcp_api_key_api.router, prefix="/api/v1", tags=["MCP API Keys"])
# internal_db_api's routes already carry the full /internal/db/... path in
# each decorator (no prefix here) — this is service-to-service only, not
# part of the public /api/v1 surface. See its module docstring for the
# access-control caveat.
app.include_router(internal_db_api.router, tags=["Internal DB (service-to-service)"])

# Mount the caspr MCP server (streamable-http) at /mcp
app.mount("/mcp", mcp_app)


# Health check endpoint
@app.get("/api/v1/health")
async def health_check():
    """Health check endpoint"""
    return {"status": "healthy"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True) 