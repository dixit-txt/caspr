"""Database engine, session factory, and the declarative ``Base``.

Two things differ from the pre-migration ``app.core.db.py`` this replaces,
and both are structural rather than behavioural:

* The engine is built by :func:`build_engine` and owned by the application
  lifespan (R-DB-3) instead of being constructed at module import. Import-time
  construction meant no module in the tree could be imported without a
  reachable database, which is what made an import smoke test impossible.
* ``Base`` carries a ``naming_convention`` (R-DB-7). Alembic autogenerate
  cannot see unnamed constraints, so without one, CHECK/UNIQUE/FK differences
  are silently skipped and a ``drop_constraint`` migration cannot be written
  because the generated name is unknown.

Engine keyword arguments are carried over verbatim from the module they
replace; none of the pool or timeout values changed.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy import MetaData
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.logging import setup_logging

logger = setup_logging(__file__)

#: R-DB-7. Must be in place before any further constraint is added; applying a
#: convention after tables exist requires a rename migration per constraint.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Declarative base for every ORM model in the service.

    Models are declared in ``app/<context>/models.py`` but must all inherit
    this one base, or relationships cannot span context packages. See
    ``app/models.py`` for the aggregator that guarantees registration.
    """

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def build_engine(dsn: str) -> AsyncEngine:
    """Create the application engine. Called once, from the lifespan."""
    return create_async_engine(
        dsn,
        echo=False,
        pool_size=10,
        max_overflow=20,
        pool_pre_ping=True,
        pool_recycle=1800,
        pool_timeout=60,
        connect_args={
            "command_timeout": 120,
            "timeout": 30,
        },
    )


SessionFactory = async_sessionmaker(
    expire_on_commit=False,  # R-DB-4
    class_=AsyncSession,
)


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    """Request-scoped session (R-DB-3). One session per request, never shared."""
    async with SessionFactory(bind=request.app.state.engine) as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            # R-DB-5: the bare re-raise is mandatory. Swallowing here means the
            # global handlers never fire and the client gets an uncorrelated 500.
            raise
        finally:
            await session.close()


SessionDep = Annotated[AsyncSession, Depends(get_session)]  # R-FA-2


@asynccontextmanager
async def async_session_scope() -> AsyncIterator[AsyncSession]:
    """Session scope for callers outside a request (tasks, scripts, CLI).

    Body carried over verbatim from ``app.core.db.py``, including its
    commit convention: this scope does not commit, callers do.
    """
    session = SessionFactory(bind=_require_engine())
    try:
        yield session
    except Exception as e:
        await session.rollback()
        # Expected HTTP auth/permission errors (403, 401, ...) are not DB failures.
        if not isinstance(e, StarletteHTTPException):
            logger.error(f"Database transaction rolled back due to error: {e!s}", exc_info=True)
        raise
    finally:
        await session.close()
        logger.info("Database session closed")


_ENGINE: AsyncEngine | None = None


def set_engine(engine: AsyncEngine) -> None:
    """Register the lifespan-owned engine for non-request callers.

    ``async_session_scope`` is used from Celery-less background work and from
    scripts that have no ``Request`` to read ``app.state`` off. The lifespan
    calls this once so those callers share the single engine (R-DB-3) rather
    than building their own.
    """
    global _ENGINE
    _ENGINE = engine


def _require_engine() -> AsyncEngine:
    """Return the process engine, building it on first use if needed.

    The lifespan calls :func:`set_engine` and that is the normal path. The
    lazy fallback exists for callers that run before or outside the
    application — scripts, and any pre-migration module still reaching for
    ``async_session_scope`` directly.

    Building lazily rather than at import is the whole point: the
    pre-migration ``app.core.db.py`` created its engine as a module-level
    side effect, so no module in the tree could be imported without a
    reachable database. Deferring to first *use* keeps behaviour identical
    while making the import smoke test possible.
    """
    global _ENGINE
    if _ENGINE is None:
        from app.core.constants import DB_CONNECTION_LINK

        _ENGINE = build_engine(DB_CONNECTION_LINK)
        logger.info("Async database engine initialized successfully")
    return _ENGINE
