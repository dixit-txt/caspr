"""Database access for the admin bounded context.

Moved verbatim from ``src/db/async_db_functions.py`` during the R-STRUCT-1
migration. Function bodies are unchanged; only the import block was retargeted
at the new module paths.
"""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import setup_logging
from app.models import (
    CostTracker,
)

logger = setup_logging(__file__)


async def insert_cost_tracker(
    *,
    timestamp: datetime,
    model_name: str,
    context: str | None = None,
    functionality: str | None = None,
    agent_name: str | None = None,
    chat_id: str | None = None,
    user_id: str | None = None,
    usage_metadata: dict[str, Any] | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    estimated_cost: float | None = None,
    cost_details: dict[str, Any] | None = None,
    session: AsyncSession,
) -> dict[str, Any]:
    """
    Insert one LLM cost/usage row into the ``costtracker`` table.

    Args:
        timestamp: When the LLM call occurred (UTC-aware datetime).
        model_name: Model id / label (e.g. ``gpt-4o``).
        context: Call-site label (e.g. ``card_utils.generate_drl``).
        functionality: Stable user-facing bucket (e.g. ``report_generation``).
        agent_name: Agent / stage that made the call.
        chat_id: Optional chat / session id.
        user_id: Optional user id.
        usage_metadata: Full provider usage blob to store as JSONB.
        input_tokens: Prompt / input token count (defaults to 0).
        output_tokens: Completion / output token count (defaults to 0).
        estimated_cost: Estimated USD cost (``payload.cost.estimated_cost_usd``).
        cost_details: Full cost breakdown JSON (``payload.cost``).
        session: SQLAlchemy async session.

    Returns:
        Dict with ``success`` and either ``data`` (row fields) or ``error``.
    """
    if not model_name:
        logger.error("model_name is required for costtracker insert")
        return {"success": False, "error": "model_name is required"}

    if timestamp is None:
        logger.error("timestamp is required for costtracker insert")
        return {"success": False, "error": "timestamp is required"}

    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=UTC)

    try:
        row = CostTracker(
            timestamp=timestamp,
            model_name=model_name,
            context=context,
            functionality=functionality,
            agent_name=agent_name,
            chat_id=chat_id,
            user_id=user_id,
            usage_metadata=usage_metadata,
            input_tokens=int(input_tokens or 0),
            output_tokens=int(output_tokens or 0),
            estimated_cost=float(estimated_cost) if estimated_cost is not None else None,
            cost_details=cost_details,
            created_at=datetime.now(UTC),
        )
        session.add(row)
        await session.commit()
        await session.refresh(row)

        logger.info(
            f"Inserted costtracker row id={row.id} model={model_name} "
            f"context={context} input_tokens={row.input_tokens} "
            f"output_tokens={row.output_tokens} estimated_cost={row.estimated_cost}"
        )
        return {
            "success": True,
            "data": {
                "id": row.id,
                "timestamp": row.timestamp,
                "model_name": row.model_name,
                "context": row.context,
                "functionality": row.functionality,
                "agent_name": row.agent_name,
                "chat_id": row.chat_id,
                "user_id": row.user_id,
                "usage_metadata": row.usage_metadata,
                "input_tokens": row.input_tokens,
                "output_tokens": row.output_tokens,
                "estimated_cost": row.estimated_cost,
                "cost_details": row.cost_details,
                "created_at": row.created_at,
            },
        }

    except SQLAlchemyError as e:
        await session.rollback()
        logger.error(f"Database error inserting costtracker row: {e!s}", exc_info=True)
        return {"success": False, "error": f"Database error: {e!s}"}

    except Exception as e:
        await session.rollback()
        logger.error(f"Unexpected error inserting costtracker row: {e!s}", exc_info=True)
        return {"success": False, "error": f"Unexpected error: {e!s}"}
