"""Async DB helpers for per-user MCP API keys (one key per account)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Optional

from sqlalchemy import delete, select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from uuid_utils import uuid7

from src.config.log_helper import setup_logging
from src.core.mcp_api_key_auth import (
    generate_mcp_api_key,
    hash_mcp_api_key,
    key_display_parts,
    normalize_credential,
)
from src.db.database import McpApiKey

logger = setup_logging(__name__)


def _serialize_key_meta(row: McpApiKey) -> Dict[str, Any]:
    return {
        "id": row.id,
        "key_prefix": row.key_prefix,
        "last_four": row.last_four,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        "last_used_at": row.last_used_at.isoformat() if row.last_used_at else None,
    }


async def get_mcp_api_key_for_user(user_id: str, session: AsyncSession) -> Dict[str, Any]:
    """Return metadata for the user's MCP API key (never the plaintext)."""
    try:
        stmt = select(McpApiKey).where(McpApiKey.user_id == user_id)
        result = await session.execute(stmt)
        row = result.scalar_one_or_none()
        if not row:
            return {"success": True, "has_key": False, "api_key": None}
        return {"success": True, "has_key": True, "api_key": _serialize_key_meta(row)}
    except SQLAlchemyError as e:
        logger.error(f"[MCP_API_KEY] get failed user_id={user_id}: {e}", exc_info=True)
        return {"success": False, "error": "Database error while fetching MCP API key."}


async def create_mcp_api_key(user_id: str, session: AsyncSession) -> Dict[str, Any]:
    """Create the user's sole MCP API key. Fails if one already exists."""
    try:
        existing = await session.execute(select(McpApiKey).where(McpApiKey.user_id == user_id))
        if existing.scalar_one_or_none() is not None:
            return {
                "success": False,
                "error": "An MCP API key already exists. Delete or regenerate it instead.",
                "code": "KEY_EXISTS",
            }

        plaintext = generate_mcp_api_key()
        prefix, last_four = key_display_parts(plaintext)
        row = McpApiKey(
            id=str(uuid7()),
            user_id=user_id,
            key_prefix=prefix,
            key_hash=hash_mcp_api_key(plaintext),
            last_four=last_four,
        )
        session.add(row)
        await session.commit()
        await session.refresh(row)
        logger.info(f"[MCP_API_KEY] created user_id={user_id} key_id={row.id}")
        return {
            "success": True,
            "api_key": plaintext,
            "meta": _serialize_key_meta(row),
            "message": "Store this API key now — it will not be shown again.",
        }
    except SQLAlchemyError as e:
        await session.rollback()
        logger.error(f"[MCP_API_KEY] create failed user_id={user_id}: {e}", exc_info=True)
        return {"success": False, "error": "Database error while creating MCP API key."}


async def regenerate_mcp_api_key(user_id: str, session: AsyncSession) -> Dict[str, Any]:
    """Replace the user's MCP API key (or create one if missing)."""
    try:
        plaintext = generate_mcp_api_key()
        prefix, last_four = key_display_parts(plaintext)
        key_hash = hash_mcp_api_key(plaintext)
        now = datetime.now(timezone.utc)

        existing = await session.execute(select(McpApiKey).where(McpApiKey.user_id == user_id))
        row = existing.scalar_one_or_none()
        if row is None:
            row = McpApiKey(
                id=str(uuid7()),
                user_id=user_id,
                key_prefix=prefix,
                key_hash=key_hash,
                last_four=last_four,
            )
            session.add(row)
        else:
            row.key_prefix = prefix
            row.key_hash = key_hash
            row.last_four = last_four
            row.last_used_at = None
            row.updated_at = now

        await session.commit()
        await session.refresh(row)
        logger.info(f"[MCP_API_KEY] regenerated user_id={user_id} key_id={row.id}")
        return {
            "success": True,
            "api_key": plaintext,
            "meta": _serialize_key_meta(row),
            "message": "Store this API key now — it will not be shown again. The previous key (if any) no longer works.",
        }
    except SQLAlchemyError as e:
        await session.rollback()
        logger.error(f"[MCP_API_KEY] regenerate failed user_id={user_id}: {e}", exc_info=True)
        return {"success": False, "error": "Database error while regenerating MCP API key."}


async def delete_mcp_api_key(user_id: str, session: AsyncSession) -> Dict[str, Any]:
    """Delete the user's MCP API key."""
    try:
        result = await session.execute(
            delete(McpApiKey).where(McpApiKey.user_id == user_id)
        )
        await session.commit()
        if result.rowcount == 0:
            return {"success": False, "error": "No MCP API key found for this account.", "code": "NOT_FOUND"}
        logger.info(f"[MCP_API_KEY] deleted user_id={user_id}")
        return {"success": True, "message": "MCP API key deleted."}
    except SQLAlchemyError as e:
        await session.rollback()
        logger.error(f"[MCP_API_KEY] delete failed user_id={user_id}: {e}", exc_info=True)
        return {"success": False, "error": "Database error while deleting MCP API key."}


async def resolve_user_id_from_mcp_api_key(
    raw_key: str,
    session: AsyncSession,
    *,
    touch_last_used: bool = True,
) -> Optional[str]:
    """Resolve a plaintext MCP API key to ``user_id``, or ``None`` if invalid."""
    try:
        raw_key = normalize_credential(raw_key)
        key_hash = hash_mcp_api_key(raw_key)
        result = await session.execute(select(McpApiKey).where(McpApiKey.key_hash == key_hash))
        row = result.scalar_one_or_none()
        if row is None:
            return None
        if touch_last_used:
            await session.execute(
                update(McpApiKey)
                .where(McpApiKey.id == row.id)
                .values(last_used_at=datetime.now(timezone.utc))
            )
            await session.commit()
        return row.user_id
    except Exception as e:
        logger.error(f"[MCP_API_KEY] resolve failed: {e}", exc_info=True)
        try:
            await session.rollback()
        except Exception:
            pass
        return None
