"""
MCP API key management for authenticated Caspr users.

One long-lived key per account. Usage of the key authenticates as that user, so
wallet balance, subscriptions, and report billing follow the normal Caspr path.

Endpoints (JWT required for management):
    GET    /mcp/api-key              – status / metadata (never plaintext)
    POST   /mcp/api-key              – create (fails if one already exists)
    POST   /mcp/api-key/regenerate   – replace (or create if missing)
    DELETE /mcp/api-key              – delete
"""

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from src.config.log_helper import setup_logging
from src.core.token_auth import get_current_active_user
from src.db.db_utils import async_session_scope
from src.db.mcp_api_key_functions import (
    create_mcp_api_key,
    delete_mcp_api_key,
    get_mcp_api_key_for_user,
    regenerate_mcp_api_key,
)
from src.resources.schemas.mcp_api_key import (
    McpApiKeyCreateResponse,
    McpApiKeyDeleteResponse,
    McpApiKeyErrorResponse,
    McpApiKeyStatusResponse,
)

logger = setup_logging(__name__)

router = APIRouter(prefix="/mcp", tags=["MCP API Keys"])


@router.get(
    "/api-key",
    response_model=McpApiKeyStatusResponse,
    responses={401: {"model": McpApiKeyErrorResponse}, 500: {"model": McpApiKeyErrorResponse}},
)
async def get_api_key_status(user_id: str = Depends(get_current_active_user)):
    """Return whether the user has an MCP API key and non-secret metadata."""
    try:
        async with async_session_scope() as session:
            result = await get_mcp_api_key_for_user(user_id, session)
            if not result.get("success"):
                return JSONResponse(
                    status_code=500,
                    content={"success": False, "error": result.get("error") or "Failed to fetch API key status."},
                )
            return McpApiKeyStatusResponse(
                success=True,
                has_key=bool(result.get("has_key")),
                api_key=result.get("api_key"),
            )
    except Exception as e:
        logger.error(f"[MCP_API_KEY_STATUS] user_id={user_id} error={e}", exc_info=True)
        return JSONResponse(
            status_code=500,
            content={"success": False, "error": "Internal server error"},
        )


@router.post(
    "/api-key",
    response_model=McpApiKeyCreateResponse,
    responses={
        401: {"model": McpApiKeyErrorResponse},
        409: {"model": McpApiKeyErrorResponse},
        500: {"model": McpApiKeyErrorResponse},
    },
)
async def create_api_key(user_id: str = Depends(get_current_active_user)):
    """Create the user's MCP API key. Returns plaintext once."""
    try:
        async with async_session_scope() as session:
            result = await create_mcp_api_key(user_id, session)
            if not result.get("success"):
                status = 409 if result.get("code") == "KEY_EXISTS" else 500
                return JSONResponse(
                    status_code=status,
                    content={"success": False, "error": result.get("error") or "Failed to create API key."},
                )
            return McpApiKeyCreateResponse(
                success=True,
                api_key=result["api_key"],
                meta=result["meta"],
                message=result["message"],
            )
    except Exception as e:
        logger.error(f"[MCP_API_KEY_CREATE] user_id={user_id} error={e}", exc_info=True)
        return JSONResponse(
            status_code=500,
            content={"success": False, "error": "Internal server error"},
        )


@router.post(
    "/api-key/regenerate",
    response_model=McpApiKeyCreateResponse,
    responses={401: {"model": McpApiKeyErrorResponse}, 500: {"model": McpApiKeyErrorResponse}},
)
async def regenerate_api_key(user_id: str = Depends(get_current_active_user)):
    """Regenerate the user's MCP API key (invalidates the previous one)."""
    try:
        async with async_session_scope() as session:
            result = await regenerate_mcp_api_key(user_id, session)
            if not result.get("success"):
                return JSONResponse(
                    status_code=500,
                    content={"success": False, "error": result.get("error") or "Failed to regenerate API key."},
                )
            return McpApiKeyCreateResponse(
                success=True,
                api_key=result["api_key"],
                meta=result["meta"],
                message=result["message"],
            )
    except Exception as e:
        logger.error(f"[MCP_API_KEY_REGENERATE] user_id={user_id} error={e}", exc_info=True)
        return JSONResponse(
            status_code=500,
            content={"success": False, "error": "Internal server error"},
        )


@router.delete(
    "/api-key",
    response_model=McpApiKeyDeleteResponse,
    responses={
        401: {"model": McpApiKeyErrorResponse},
        404: {"model": McpApiKeyErrorResponse},
        500: {"model": McpApiKeyErrorResponse},
    },
)
async def delete_api_key(user_id: str = Depends(get_current_active_user)):
    """Delete the user's MCP API key."""
    try:
        async with async_session_scope() as session:
            result = await delete_mcp_api_key(user_id, session)
            if not result.get("success"):
                status = 404 if result.get("code") == "NOT_FOUND" else 500
                return JSONResponse(
                    status_code=status,
                    content={"success": False, "error": result.get("error") or "Failed to delete API key."},
                )
            return McpApiKeyDeleteResponse(success=True, message=result["message"])
    except Exception as e:
        logger.error(f"[MCP_API_KEY_DELETE] user_id={user_id} error={e}", exc_info=True)
        return JSONResponse(
            status_code=500,
            content={"success": False, "error": "Internal server error"},
        )
