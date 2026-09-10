"""Process-wide LangGraph checkpointer (R4).

The agent graph pauses for report configuration and is resumed by a *later*
HTTP request, so the paused state has to outlive the request that created it.
That rules out :class:`InMemorySaver`, which the repo uses elsewhere: it is
per-object and per-process, so a paused report would vanish on a restart and
would be invisible to a second worker.

The saver is built once, in the application lifespan, and shared by every
``Casper`` graph in the process. When Postgres is unreachable — local runs,
tests, a database blip at boot — construction degrades to an in-memory saver
rather than refusing to start: an interrupt that survives only within one
process is still better than an application that cannot serve at all.
"""

from __future__ import annotations

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg import AsyncConnection
from psycopg.rows import DictRow, dict_row
from psycopg_pool import AsyncConnectionPool

from app.core.logging import setup_logging

logger = setup_logging(__file__)

#: The saver holds one connection open per concurrent graph step, and graph
#: steps around the pause are short. A small pool is enough and keeps the
#: checkpointer from competing with the application engine for connections.
_POOL_MIN_SIZE = 1
_POOL_MAX_SIZE = 5

_CHECKPOINTER: BaseCheckpointSaver[str] | None = None
_POOL: AsyncConnectionPool[AsyncConnection[DictRow]] | None = None


async def setup_checkpointer(dsn: str) -> BaseCheckpointSaver[str]:
    """Build the process checkpointer and run its one-time table setup.

    Idempotent: repeated calls return the saver built by the first one without
    re-running ``setup()``. Called from the lifespan, never per request —
    ``setup()`` issues DDL, and doing that on every turn would put a schema
    round-trip in front of each report.
    """
    global _CHECKPOINTER, _POOL

    if _CHECKPOINTER is not None:
        return _CHECKPOINTER

    try:
        pool: AsyncConnectionPool[AsyncConnection[DictRow]] = AsyncConnectionPool(
            conninfo=dsn,
            min_size=_POOL_MIN_SIZE,
            max_size=_POOL_MAX_SIZE,
            open=False,
            # Both are required by AsyncPostgresSaver: it issues its own
            # transactions and reads rows by column name.
            kwargs={"autocommit": True, "row_factory": dict_row},
        )
        await pool.open(wait=True, timeout=10.0)
        saver = AsyncPostgresSaver(pool)
        await saver.setup()
    except Exception as exc:
        logger.warning(
            f"[checkpointer] Postgres checkpointer unavailable ({exc}) — falling back to "
            f"an in-memory saver. Paused reports will not survive a restart and will not "
            f"be resumable from another worker."
        )
        _CHECKPOINTER = InMemorySaver()
        return _CHECKPOINTER

    _POOL = pool
    _CHECKPOINTER = saver
    logger.info("[checkpointer] Postgres checkpointer ready")
    return _CHECKPOINTER


def get_checkpointer() -> BaseCheckpointSaver[str]:
    """Return the process checkpointer, building an in-memory one on first use.

    The lifespan normally calls :func:`setup_checkpointer` long before any
    graph is compiled. The lazy fallback covers callers that run outside the
    application — tests and scripts — so building a graph never depends on a
    reachable database.
    """
    global _CHECKPOINTER

    if _CHECKPOINTER is None:
        logger.info(
            "[checkpointer] No checkpointer configured for this process — using an in-memory saver"
        )
        _CHECKPOINTER = InMemorySaver()
    return _CHECKPOINTER


def set_checkpointer(checkpointer: BaseCheckpointSaver[str] | None) -> None:
    """Install a specific saver for the process. For the lifespan and tests."""
    global _CHECKPOINTER
    _CHECKPOINTER = checkpointer


async def close_checkpointer() -> None:
    """Release the connection pool on shutdown."""
    global _CHECKPOINTER, _POOL

    pool, _POOL = _POOL, None
    _CHECKPOINTER = None
    if pool is not None:
        await pool.close()
        logger.info("[checkpointer] Postgres checkpointer pool closed")
