"""error_alerter.py: Collects 500 errors via Redis and sends batched digest emails."""

import asyncio
import json
import re
import sys
import time
import traceback as tb
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

from fastapi.concurrency import run_in_threadpool

from app.core.constants import ERROR_DIGEST_INTERVAL_SECONDS
from app.core.logging import setup_logging

logger = setup_logging(__file__)

REDIS_QUEUE_KEY = "error_alerts:queue"
REDIS_PROCESSING_PREFIX = "error_alerts:processing:"
MAX_QUEUE_SIZE = 5000

SENSITIVE_PARAM_PATTERN = re.compile(
    r"(token|password|passwd|secret|key|auth|credential|session_id|api_key)",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Per-request / per-task alert state lives in two ContextVars:
#
#   _alert_state_box   — mutable dict carrying both the captured short
#                        traceback and a "queued" sentinel.  Using a single
#                        mutable box (instead of two ContextVars) means
#                        background tasks spawned inside a request inherit
#                        the SAME box and stay in sync with the middleware.
#
#   _alert_request_meta — mutable dict with method/path/user_id/user_email
#                         so the logging filter can build a rich error entry
#                         even when no HTTP response is involved (e.g. an
#                         async background task or a model fallback inside a
#                         deeper retry loop that never propagates a 500).
#
# Flow:
#   1. The HTTP middleware (or a background task entry point) calls
#      ``set_alert_request_context(...)`` once it knows who the caller is.
#      That seeds both ContextVars for the rest of the async task tree.
#   2. Anywhere downstream, ``logger.error(...)`` / ``logger.exception(...)``
#      inside an ``except`` block triggers the logging filter which calls
#      ``capture_exception_for_alerter()`` -> short traceback is stored AND
#      the error is asynchronously queued to Redis exactly once.
#   3. The middleware, when it sees a 5xx response, falls back to queueing
#      ONLY if the filter hasn't already queued for this request (avoids
#      duplicate digest rows for the same crash).
# ---------------------------------------------------------------------------
_alert_state_box: ContextVar[dict[str, Any] | None] = ContextVar("_alert_state_box", default=None)
_alert_request_meta: ContextVar[dict[str, Any] | None] = ContextVar(
    "_alert_request_meta", default=None
)


def _init_request_context() -> None:
    """Create a fresh shared mutable state box for the current request/task."""
    _alert_state_box.set({"traceback": None, "queued": False})
    _alert_request_meta.set({})


def set_alert_request_context(
    *,
    method: str | None = None,
    path: str | None = None,
    user_id: str | None = None,
    user_email: str | None = None,
    query_params: str | None = None,
) -> None:
    """Stash request/task identity for the logging filter to consume.

    Safe to call multiple times — fields are merged into the existing meta
    dict.  Must be preceded by an ``_init_request_context()`` call (which the
    HTTP middleware and background-task helpers do automatically).
    """
    meta = _alert_request_meta.get()
    if meta is None:
        meta = {}
        _alert_request_meta.set(meta)
    if method is not None:
        meta["method"] = method
    if path is not None:
        meta["path"] = path
    if user_id is not None:
        meta["user_id"] = user_id
    if user_email is not None:
        meta["user_email"] = user_email
    if query_params is not None:
        meta["query_params"] = query_params


def get_alert_request_context() -> dict[str, Any]:
    """Return a shallow copy of the current alert meta (or an empty dict)."""
    meta = _alert_request_meta.get()
    return dict(meta) if meta else {}


def capture_exception_for_alerter(logger_name: str | None = None) -> None:
    """Capture the active exception and asynchronously enqueue an error event.

    Must be invoked from inside an ``except`` block (directly or via the
    logging filter) so ``sys.exc_info()`` returns the live exception.  No-op
    when there is no active exception.

    Idempotent per request/task: only the FIRST exception is captured and
    queued.  This keeps the digest readable and prevents a single bad request
    from producing dozens of rows when many ``logger.error`` calls fire in a
    retry loop.
    """
    exc_type, exc_val, _exc_tb_obj = sys.exc_info()
    if exc_type is None:
        return

    # Skip expected client errors (401/403/404, etc.) — not operator incidents.
    from starlette.exceptions import HTTPException as StarletteHTTPException

    if isinstance(exc_val, StarletteHTTPException) and exc_val.status_code < 500:
        return

    state = _alert_state_box.get()
    # Auto-init for background tasks that never went through the middleware.
    if state is None:
        state = {"traceback": None, "queued": False}
        _alert_state_box.set(state)

    if state.get("queued"):
        return

    short = "".join(tb.format_exception_only(exc_type, exc_val)).strip()
    state["traceback"] = short
    state["queued"] = True

    _schedule_alert_queue(short_tb=short, logger_name=logger_name)


def _consume_alert_traceback() -> str | None:
    """Return the captured traceback string for the current request."""
    state = _alert_state_box.get()
    if not state:
        return None
    return state.get("traceback")


def _was_alert_already_queued() -> bool:
    """True if the logging filter already queued an alert for this request."""
    state = _alert_state_box.get()
    return bool(state and state.get("queued"))


def _schedule_alert_queue(*, short_tb: str, logger_name: str | None) -> None:
    """Queue an error entry on the running event loop without awaiting it.

    Called synchronously from the logging filter, so it must never raise and
    must not block.  When no event loop is running (e.g. logger.error fires
    from a sync thread-pool worker) we fall back to a best-effort warning.
    """
    try:
        meta = get_alert_request_context()
        method = meta.get("method") or "BACKGROUND"
        path = meta.get("path") or (f"logger:{logger_name}" if logger_name else "logger:<unknown>")
        user_id = meta.get("user_id")
        user_email = meta.get("user_email")
        query_params = meta.get("query_params") or ""
        status_code = meta.get("status_code") or 500

        # Extract the short error message from the traceback ("TypeError: foo").
        error_message = short_tb.splitlines()[-1] if short_tb else ""

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop is None:
            # Best-effort: we cannot await here.  The next request inside an
            # event loop will still pick up its own errors; we just lose this
            # one row.  Log a breadcrumb so operators can spot the gap.
            logger.warning(
                "[ERROR_DIGEST] Could not auto-queue logger.error (no running loop) "
                "path=%s message=%s",
                path,
                error_message[:200],
            )
            return

        alerter = get_error_alerter()
        loop.create_task(
            alerter.queue_error(
                method=str(method),
                path=str(path),
                status_code=int(status_code) if isinstance(status_code, int) else 500,
                error_message=error_message,
                query_params=str(query_params),
                user_id=user_id,
                user_email=user_email,
                exc_traceback=short_tb,
            )
        )
    except Exception:
        # The alerter must never break user-visible flows.
        logger.warning("[ERROR_DIGEST] _schedule_alert_queue failed silently", exc_info=True)


def _redact_query_params(raw: str) -> str:
    """Replace values of sensitive query parameters with [REDACTED]."""
    if not raw:
        return ""
    parts = []
    for pair in raw.split("&"):
        if "=" in pair:
            name, _ = pair.split("=", 1)
            if SENSITIVE_PARAM_PATTERN.search(name):
                parts.append(f"{name}=[REDACTED]")
            else:
                parts.append(pair)
        else:
            parts.append(pair)
    return "&".join(parts)


class ErrorAlertManager:
    """Queues 500-level errors in Redis and drains them into a digest email on a fixed schedule."""

    def __init__(self, redis_client):
        self.redis_client = redis_client

    async def queue_error(
        self,
        *,
        method: str,
        path: str,
        status_code: int,
        error_message: str,
        query_params: str = "",
        user_id: str | None = None,
        user_email: str | None = None,
        exc: BaseException | None = None,
        exc_traceback: str | None = None,
    ) -> None:
        """Serialize one error event and push it to the Redis queue.

        Silently swallows any Redis errors so the alerter never breaks the API.
        The queue is capped at MAX_QUEUE_SIZE to prevent unbounded memory growth.

        exc_traceback: pre-formatted short traceback string captured by the
            logging filter (preferred). Falls back to formatting exc if provided.
        user_email: pre-resolved email for the user.  When omitted the digest
            sender does a batch lookup against the users table to fill it in.
        """
        try:
            traceback_str = exc_traceback
            if traceback_str is None and exc is not None:
                traceback_str = "".join(tb.format_exception(type(exc), exc, exc.__traceback__))

            entry = {
                "timestamp": datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S"),
                "method": method,
                "path": path,
                "status_code": status_code,
                "error_message": error_message[:500] if error_message else "",
                "query_params": _redact_query_params(query_params[:200] if query_params else ""),
                "user_id": user_id or "anonymous",
                "user_email": user_email or "",
                "traceback": traceback_str,
            }
            pipe = self.redis_client.pipeline()
            pipe.rpush(REDIS_QUEUE_KEY, json.dumps(entry))
            pipe.ltrim(REDIS_QUEUE_KEY, -MAX_QUEUE_SIZE, -1)
            await pipe.execute()
        except Exception:
            logger.warning("ErrorAlertManager.queue_error failed silently", exc_info=True)

    async def _cleanup_orphaned_processing_keys(self) -> None:
        """Delete any leftover processing keys from a previous hard crash."""
        try:
            cursor = b"0"
            orphans = []
            while True:
                cursor, keys = await self.redis_client.scan(
                    cursor=cursor, match=f"{REDIS_PROCESSING_PREFIX}*", count=100
                )
                orphans.extend(keys)
                if cursor == b"0" or cursor == 0:
                    break
            if orphans:
                await self.redis_client.delete(*orphans)
                logger.info(
                    f"[ERROR_DIGEST_LOOP] Cleaned up {len(orphans)} orphaned processing key(s)"
                )
        except Exception:
            logger.warning("Orphan cleanup failed", exc_info=True)

    async def flush_and_send_digest(self) -> None:
        """Atomically drain the queue via RENAME and send a digest email if there are errors."""
        from app.adapters.email import send_error_digest_email

        processing_key = f"{REDIS_PROCESSING_PREFIX}{int(time.time())}"

        try:
            await self.redis_client.rename(REDIS_QUEUE_KEY, processing_key)
        except Exception:
            return

        try:
            raw_entries = await self.redis_client.lrange(processing_key, 0, -1)
        except Exception:
            logger.warning("Failed to LRANGE processing queue", exc_info=True)
            return
        finally:
            try:
                await self.redis_client.delete(processing_key)
            except Exception:
                logger.warning(f"Failed to DEL {processing_key}", exc_info=True)

        if not raw_entries:
            return

        errors = []
        for raw in raw_entries:
            try:
                errors.append(json.loads(raw))
            except Exception:
                logger.warning(f"Could not parse queued error entry: {raw!r}")

        if not errors:
            return

        # Backfill ``user_email`` for any entry that only has a user_id (the
        # JWT-decoded path doesn't carry the email, so we resolve it once per
        # digest instead of once per error queue).
        await self._enrich_with_user_emails(errors)

        logger.info(f"[ERROR_DIGEST] Flushing {len(errors)} error(s) into digest email")

        try:
            await run_in_threadpool(send_error_digest_email, errors)
        except Exception:
            logger.error("Failed to send error digest email", exc_info=True)

    @staticmethod
    async def _enrich_with_user_emails(errors: list[dict[str, Any]]) -> None:
        """Fill in ``user_email`` for entries that only have a user_id.

        Performs ONE batch SELECT against ``users`` for the digest, regardless
        of how many error rows reference the same user.  Failures are swallowed
        — a missing email column should never block the digest itself.
        """
        try:
            uuid_re = re.compile(
                r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
            )
            unresolved_ids = {
                str(err.get("user_id"))
                for err in errors
                if not err.get("user_email")
                and err.get("user_id")
                and uuid_re.match(str(err.get("user_id")))
            }
            if not unresolved_ids:
                return

            # Local imports to avoid pulling DB at module load time.
            from sqlalchemy import select

            from app.core.db import async_session_scope
            from app.models import User

            id_to_email: dict[str, str] = {}
            async with async_session_scope() as session:
                result = await session.execute(
                    select(User.id, User.email).where(User.id.in_(unresolved_ids))
                )
                for row in result.all():
                    id_to_email[str(row[0])] = row[1]

            for err in errors:
                if err.get("user_email"):
                    continue
                uid = err.get("user_id")
                if uid and uid in id_to_email:
                    err["user_email"] = id_to_email[uid]
        except Exception:
            logger.warning(
                "[ERROR_DIGEST] Could not resolve user emails for digest",
                exc_info=True,
            )

    async def start_digest_loop(self) -> None:
        """Run flush_and_send_digest every ERROR_DIGEST_INTERVAL_SECONDS until cancelled."""
        logger.info(f"[ERROR_DIGEST_LOOP] Starting - interval={ERROR_DIGEST_INTERVAL_SECONDS}s")
        await self._cleanup_orphaned_processing_keys()

        while True:
            try:
                await asyncio.sleep(ERROR_DIGEST_INTERVAL_SECONDS)
                await self.flush_and_send_digest()
            except asyncio.CancelledError:
                logger.info("[ERROR_DIGEST_LOOP] Cancelled - shutting down")
                break
            except Exception:
                logger.error("[ERROR_DIGEST_LOOP] Unexpected error in digest loop", exc_info=True)


# ---------------------------------------------------------------------------
# Module-level singleton accessor.
#
# The HTTP middleware in ``main.py`` instantiates its own ``ErrorAlertManager``
# at startup and runs the digest loop from it.  Code that runs outside the
# HTTP request/response cycle (e.g. FastAPI ``BackgroundTasks`` like
# ``chat_producer`` / ``temp_chat_producer``, asyncio worker tasks, the
# subgraph pipeline) cannot rely on the 500-response middleware to surface
# its errors.  Those code paths should call :func:`get_error_alerter` and
# explicitly :meth:`queue_error` so failures still land in the digest email.
#
# The singleton is created lazily on first access to avoid pulling in the
# Redis client at module import time (and to sidestep import cycles with
# ``main.py``, which itself imports ``api`` which can import this module).
# ---------------------------------------------------------------------------
_error_alerter_singleton: ErrorAlertManager | None = None


def get_error_alerter() -> ErrorAlertManager:
    """Return a process-wide ``ErrorAlertManager`` for background tasks.

    Safe to call from anywhere; the underlying Redis client is itself a
    process-wide singleton so multiple ``ErrorAlertManager`` instances
    sharing it would be functionally equivalent — we still cache one for
    cheapness.
    """
    global _error_alerter_singleton
    if _error_alerter_singleton is None:
        # Local import to avoid a circular import at module load time.
        from app.core.redis import get_redis_instance

        _error_alerter_singleton = ErrorAlertManager(redis_client=get_redis_instance().redis_client)
    return _error_alerter_singleton


async def queue_background_error(
    *,
    path: str,
    error_message: str,
    user_id: str | None = None,
    user_email: str | None = None,
    exc: BaseException | None = None,
) -> None:
    """Convenience wrapper for non-HTTP code paths.

    Captures the active exception (if any) into the short-traceback format
    the digest email expects and queues a synthetic 500-style entry so the
    failure shows up in the next digest.

    Also flips the per-task ``queued`` flag so the inner logging filter does
    not race ahead and double-queue the same crash.

    All failures inside this helper are swallowed: the alerter should never
    be able to take down the background task that called it.
    """
    try:
        # Mark this task as "already queued" so the logger.error filter that
        # likely runs immediately before this call (in chat_producer's outer
        # except) does not produce a duplicate digest row.
        state = _alert_state_box.get()
        if state is None:
            state = {"traceback": None, "queued": True}
            _alert_state_box.set(state)
        else:
            state["queued"] = True

        short_tb: str | None = None
        if exc is not None:
            short_tb = "".join(tb.format_exception_only(type(exc), exc)).strip()
        await get_error_alerter().queue_error(
            method="BACKGROUND",
            path=path,
            status_code=500,
            error_message=error_message,
            user_id=user_id,
            user_email=user_email,
            exc=exc,
            exc_traceback=short_tb,
        )
    except Exception:
        logger.warning("queue_background_error failed silently", exc_info=True)
