"""Request middleware.

``error_alert_middleware`` is moved verbatim from the pre-migration
``main.py``. It remains a ``BaseHTTPMiddleware``-style function registered
with ``app.middleware("http")``.

R-FA-7 prohibits that form for middleware touching contextvars, and this one
should eventually be pure ASGI. Converting it is a rewrite of its response
buffering, not a move, so it is deferred per spec §5 — and it is a
prerequisite for the correlation ID work (R-OBS-1), because contextvars set
inside BaseHTTPMiddleware do not reliably propagate to the handler.
"""

import json

from fastapi import Request

from app.core.logging import setup_logging
from app.observability.error_alerter import (
    _consume_alert_traceback,
    _init_request_context,
    _was_alert_already_queued,
    set_alert_request_context,
)

logger = setup_logging(__file__)


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

                # The alerter is owned by the lifespan and lives on app.state
                # (R-FA-1); pre-migration it was a module-level singleton built
                # at import, which opened a Redis client just by importing main.
                await request.app.state.error_alerter.queue_error(
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
