"""Application factory and lifespan (R-FA-1).

Three module-scope side effects from the pre-migration ``main.py`` now live
inside ``lifespan``: the Redis client, the ``ErrorAlertManager``, and — via
``app.core.db`` — the database engine. Building them at import time meant the
application could not be imported without a reachable Redis and PostgreSQL,
which is why an import smoke test was impossible before this change. It also
breaks fork-based workers and test isolation, which is what R-FA-1 is about.

Nothing else in the startup or shutdown sequence changed: CloudWatch still
connects on startup and closes on shutdown, and the error digest loop is still
a task cancelled on the way out.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware

import app.models  # noqa: F401  # registers every ORM mapper — spec §3.2
from app.adapters.cloudwatch import CloudwatchInstance
from app.api import api_router
from app.core.checkpointer import close_checkpointer, setup_checkpointer
from app.core.constants import (
    ALLOWED_ORIGINS,
    DB_CHECKPOINTER_DSN,
    DB_CONNECTION_LINK,
    ENVIRONMENT,
)
from app.core.db import build_engine, set_engine
from app.core.errors import http_exception_handler, validation_error_handler
from app.core.logging import setup_logging
from app.core.middleware import error_alert_middleware
from app.core.redis import get_redis_instance
from app.observability.error_alerter import ErrorAlertManager

logger = setup_logging(__file__)


@asynccontextmanager
async def lifespan(fastapi_app: FastAPI) -> AsyncIterator[None]:
    logger.info("Starting up...")

    engine = build_engine(DB_CONNECTION_LINK)
    fastapi_app.state.engine = engine
    # Share the one engine with callers that have no Request to read
    # app.state off — scripts and background work (R-DB-3).
    set_engine(engine)

    # The agent graph's pause survives between requests only if its state is
    # checkpointed. Setup issues DDL, so it belongs here and not per request.
    fastapi_app.state.checkpointer = await setup_checkpointer(DB_CHECKPOINTER_DSN)

    fastapi_app.state.redis = get_redis_instance()
    fastapi_app.state.error_alerter = ErrorAlertManager(
        redis_client=fastapi_app.state.redis.redis_client
    )

    await CloudwatchInstance.connect()
    logger.info("Cloudwatch connected...")
    digest_task = asyncio.create_task(fastapi_app.state.error_alerter.start_digest_loop())
    logger.info("Error digest loop started...")

    try:
        yield
    finally:
        logger.info("Shutting down...")
        digest_task.cancel()
        try:
            await digest_task
        except asyncio.CancelledError:
            pass
        await CloudwatchInstance.close()
        logger.info("Cloudwatch closed...")
        await close_checkpointer()
        await engine.dispose()


def _register_health(fastapi_app: FastAPI) -> None:
    """R-FA-10: liveness and readiness answer different questions.

    Liveness answers "should you kill me" and must check nothing external. If
    it touched the database, a 30-second blip would make the orchestrator
    restart-loop every healthy pod, turning partial degradation into a total
    outage. Readiness answers "should you route to me" and does check.
    """

    @fastapi_app.get("/health/live", tags=["Health"])
    async def health_live() -> dict[str, str]:
        return {"status": "healthy"}

    @fastapi_app.get("/health/ready", tags=["Health"])
    async def health_ready(request: Request) -> dict[str, object]:
        checks: dict[str, str] = {}
        try:
            async with request.app.state.engine.connect() as conn:
                await asyncio.wait_for(conn.exec_driver_sql("SELECT 1"), timeout=3)
            checks["database"] = "ok"
        except Exception:
            checks["database"] = "unreachable"
        try:
            await asyncio.wait_for(request.app.state.redis.redis_client.ping(), timeout=3)
            checks["redis"] = "ok"
        except Exception:
            checks["redis"] = "unreachable"
        ready = all(value == "ok" for value in checks.values())
        return {"status": "ready" if ready else "degraded", "checks": checks}

    # Deprecated alias. render-report probes this path for its own readiness
    # and has a test asserting it, so removing it would break a sibling
    # service. Retire only after render-report migrates. Spec §4.4.
    @fastapi_app.get("/api/v1/health", tags=["Health"])
    async def health_check() -> dict[str, str]:
        """Health check endpoint"""
        return {"status": "healthy"}


def create_app() -> FastAPI:
    is_production = ENVIRONMENT == "PROD"

    fastapi_app = FastAPI(
        title="Casper API",
        description="Casper - Report AI Search Assistant",
        version="1.0.0",
        lifespan=lifespan,
        # R-FA-9: /openapi.json is a complete map of the attack surface,
        # including internal endpoints and field names.
        docs_url=None if is_production else "/docs",
        redoc_url=None if is_production else "/redoc",
        openapi_url=None if is_production else "/openapi.json",
    )

    fastapi_app.add_exception_handler(HTTPException, http_exception_handler)
    fastapi_app.add_exception_handler(RequestValidationError, validation_error_handler)

    logger.info(f"Configuring CORS for {ENVIRONMENT} environment with origins: {ALLOWED_ORIGINS}")
    fastapi_app.add_middleware(
        CORSMiddleware,
        allow_origins=ALLOWED_ORIGINS,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=[
            "Authorization",
            "Content-Type",
            "Accept",
            "Origin",
            "X-Requested-With",
            "X-API-Key",
        ],
    )
    fastapi_app.middleware("http")(error_alert_middleware)

    fastapi_app.include_router(api_router)
    _register_health(fastapi_app)
    return fastapi_app
