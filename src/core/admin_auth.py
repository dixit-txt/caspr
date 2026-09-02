"""admin_auth.py: Guard for CEO/cofounder dashboard APIs."""
from fastapi import Depends, HTTPException
from sqlalchemy import select

from src.core.token_auth import get_current_active_user
from src.db.database import User
from src.db.db_utils import async_session_scope

ALLOWED_DASHBOARD_ROLES = frozenset({"ceo", "cofounder", "admin"})


async def get_current_dashboard_admin(
    user_id: str = Depends(get_current_active_user),
) -> dict:
    """Require a valid JWT user with dashboard_role in {ceo, cofounder, admin}."""
    async with async_session_scope() as session:
        result = await session.execute(select(User).where(User.id == user_id))
        user = result.scalar_one_or_none()
        if user is None:
            raise HTTPException(status_code=401, detail="User not found")
        role = (user.dashboard_role or "").strip().lower()
        if role not in ALLOWED_DASHBOARD_ROLES:
            raise HTTPException(status_code=403, detail="Dashboard access denied")
        return {
            "user_id": user.id,
            "email": user.email,
            "user_name": user.user_name,
            "dashboard_role": role,
        }
