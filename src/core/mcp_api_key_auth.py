"""MCP API key generation and hashing helpers.

Keys are high-entropy secrets (``caspr_mcp_`` + urlsafe token). We store an
HMAC-SHA256 digest (peppered with ``JWT_SECRET_KEY``) for O(1) lookup.
Plaintext is returned only at create/regenerate time.
"""

from __future__ import annotations

import hmac
import hashlib
import secrets

from src.config.constants import JWT_SECRET_KEY

MCP_API_KEY_PREFIX = "caspr_mcp_"

# Display-friendly prefix length (includes the ``caspr_mcp_`` literal).
_PREFIX_DISPLAY_LEN = 16


def is_mcp_api_key(token: str) -> bool:
    """Return True if *token* looks like a Caspr MCP API key (not a JWT)."""
    token = (token or "").strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    return token.startswith(MCP_API_KEY_PREFIX)


def normalize_credential(token: str) -> str:
    """Strip optional ``Bearer `` prefix and whitespace."""
    token = (token or "").strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    return token


def generate_mcp_api_key() -> str:
    """Generate a new plaintext MCP API key."""
    return f"{MCP_API_KEY_PREFIX}{secrets.token_urlsafe(32)}"


def hash_mcp_api_key(raw_key: str) -> str:
    """HMAC-SHA256 hex digest of the plaintext key."""
    raw_key = normalize_credential(raw_key)
    if not JWT_SECRET_KEY:
        raise RuntimeError("JWT_SECRET_KEY is not configured; cannot hash MCP API keys.")
    return hmac.new(
        JWT_SECRET_KEY.encode("utf-8"),
        raw_key.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def key_display_parts(raw_key: str) -> tuple[str, str]:
    """Return ``(key_prefix, last_four)`` for UI display."""
    raw_key = normalize_credential(raw_key)
    prefix = raw_key[:_PREFIX_DISPLAY_LEN]
    last_four = raw_key[-4:] if len(raw_key) >= 4 else raw_key
    return prefix, last_four
