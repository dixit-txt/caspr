"""token_auth.py: Token authentication functionality"""
from datetime import datetime, timedelta, timezone
from uuid_utils import uuid7
from jose import jwt, JWTError
from fastapi import HTTPException, Depends
from fastapi.security import OAuth2PasswordBearer
from pydantic import ValidationError
from src.config.constants import (
    JWT_SECRET_KEY,
    JWT_ALGORITHM,
    JWT_EXPIRATION_DELTA,
    JWT_REFRESH_EXPIRATION_DELTA,
    FORGOT_PASSWORD_EXPIRE_MINUTES,
    SIGNUP_VERIFICATION_EXPIRE_MINUTES
)

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="token")


def create_access_token(user_id: int) -> str:
    """Create a new JWT access token"""
    payload = {
        "user_id": user_id,
        "type": "access",
        "iat": datetime.now(timezone.utc),
        "exp": datetime.now(timezone.utc) + JWT_EXPIRATION_DELTA
    }
    return jwt.encode(payload, JWT_SECRET_KEY, algorithm=JWT_ALGORITHM)

def create_reset_password_token(user_id: str, password_hash: str) -> str:
    """Create a new JWT reset password token"""
    payload = {
        "user_id": user_id,
        "type": "reset_password",
        "password_hash": password_hash,
        "iat": datetime.now(timezone.utc),
        "exp": datetime.now(timezone.utc) + timedelta(minutes=FORGOT_PASSWORD_EXPIRE_MINUTES)
    }
    return jwt.encode(payload, JWT_SECRET_KEY, algorithm=JWT_ALGORITHM)

def create_verification_token(email: str) -> str:
    """Create a new JWT verification token"""
    payload = {
        "email": email,
        "type": "verification",
        "iat": datetime.now(timezone.utc),
        "exp": datetime.now(timezone.utc) + timedelta(minutes=SIGNUP_VERIFICATION_EXPIRE_MINUTES)
    }
    return jwt.encode(payload, JWT_SECRET_KEY, algorithm=JWT_ALGORITHM)

def create_refresh_token(user_id: str) -> str:
    """Create a new JWT refresh token"""
    jti = str(uuid7())
    payload = {
        "jti": jti,
        "user_id": user_id,
        "type": "refresh",
        "iat": datetime.now(timezone.utc),
        "exp": datetime.now(timezone.utc) + JWT_REFRESH_EXPIRATION_DELTA
    }
    return jwt.encode(payload, JWT_SECRET_KEY, algorithm=JWT_ALGORITHM)

def verify_token(token: str, expected_type: str) -> bool:
    try:
        payload = jwt.decode(token, JWT_SECRET_KEY, algorithms=[JWT_ALGORITHM])
        if payload.get("type") == expected_type:
            return payload.get("user_id")
        else:
            raise HTTPException(status_code=401, detail="Invalid token")
    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid token")


async def get_current_active_user(token: str = Depends(oauth2_scheme)) -> str:
    """Resolve the caller to a ``user_id`` from a JWT access token or MCP API key.

    MCP hosts send ``Authorization: Bearer caspr_mcp_...``. That key maps to the
    owning account, so wallet/credits and ownership checks match the web app.
    """
    from src.core.mcp_api_key_auth import is_mcp_api_key, normalize_credential

    token = normalize_credential(token)

    if is_mcp_api_key(token):
        from src.db.db_utils import async_session_scope
        from src.db.mcp_api_key_functions import resolve_user_id_from_mcp_api_key

        try:
            async with async_session_scope() as session:
                user_id = await resolve_user_id_from_mcp_api_key(token, session)
        except Exception:
            raise HTTPException(status_code=401, detail="Invalid token")
        if not user_id:
            raise HTTPException(status_code=401, detail="Invalid token")
        return user_id

    try:
        payload = jwt.decode(token, JWT_SECRET_KEY, algorithms=[JWT_ALGORITHM])
        if payload.get("type") == "access":
            user_id = payload.get("user_id")
            if user_id is None:
                raise HTTPException(status_code=401, detail="Invalid token")
            return user_id
        else:
            raise HTTPException(status_code=401, detail="Invalid token")

    except (JWTError, ValidationError):
        # If the token is invalid or expired, raise a credential error
        raise HTTPException(status_code=401, detail="Invalid token")


async def get_current_user_context(token: str = Depends(oauth2_scheme)) -> tuple:
    """Resolve the caller to ``(user_id, is_mcp)`` from a JWT or MCP API key.

    Returns a tuple where ``is_mcp`` is ``True`` when the request was
    authenticated with a ``caspr_mcp_...`` API key and ``False`` for normal
    JWT-authenticated web/app sessions.
    """
    from src.core.mcp_api_key_auth import is_mcp_api_key, normalize_credential

    token = normalize_credential(token)

    if is_mcp_api_key(token):
        from src.db.db_utils import async_session_scope
        from src.db.mcp_api_key_functions import resolve_user_id_from_mcp_api_key

        try:
            async with async_session_scope() as session:
                user_id = await resolve_user_id_from_mcp_api_key(token, session)
        except Exception:
            raise HTTPException(status_code=401, detail="Invalid token")
        if not user_id:
            raise HTTPException(status_code=401, detail="Invalid token")
        return user_id, True

    try:
        payload = jwt.decode(token, JWT_SECRET_KEY, algorithms=[JWT_ALGORITHM])
        if payload.get("type") == "access":
            user_id = payload.get("user_id")
            if user_id is None:
                raise HTTPException(status_code=401, detail="Invalid token")
            return user_id, False
        else:
            raise HTTPException(status_code=401, detail="Invalid token")

    except (JWTError, ValidationError):
        raise HTTPException(status_code=401, detail="Invalid token")
