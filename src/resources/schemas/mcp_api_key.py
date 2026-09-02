"""Pydantic schemas for MCP API key management."""

from typing import Optional

from pydantic import BaseModel, Field


class McpApiKeyMeta(BaseModel):
    id: str
    key_prefix: str
    last_four: str
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    last_used_at: Optional[str] = None


class McpApiKeyStatusResponse(BaseModel):
    success: bool = True
    has_key: bool
    api_key: Optional[McpApiKeyMeta] = None


class McpApiKeyCreateResponse(BaseModel):
    success: bool = True
    api_key: str = Field(..., description="Plaintext key — shown only once")
    meta: McpApiKeyMeta
    message: str


class McpApiKeyDeleteResponse(BaseModel):
    success: bool = True
    message: str


class McpApiKeyErrorResponse(BaseModel):
    success: bool = False
    error: str
