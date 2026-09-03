"""Database access for the referrals bounded context.

Moved verbatim from ``src/db/async_db_functions.py`` during the R-STRUCT-1
migration. Function bodies are unchanged; only the import block was retargeted
at the new module paths.
"""

import secrets
import string

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import setup_logging
from app.models import (
    User,
)

logger = setup_logging(__file__)


async def generate_unique_referral_code(session: AsyncSession) -> str:
    """
    Generate unique 8-character referral code.
    
    Args:
        session: Database session
        
    Returns:
        Unique referral code (uppercase alphanumeric)
        
    Raises:
        ValueError: If unable to generate unique code after max attempts
    """
    chars = string.ascii_uppercase + string.digits
    max_attempts = 10
    
    for attempt in range(max_attempts):
        code = ''.join(secrets.choice(chars) for _ in range(8))
        
        # Check if code already exists
        result = await session.execute(
            select(User.id).where(User.referral_code == code)
        )
        if not result.scalar_one_or_none():
            logger.info(f"[REFERRAL_CODE_GENERATED] code={code}, attempts={attempt + 1}")
            return code
    
    logger.error(f"[REFERRAL_CODE_GENERATION_FAILED] max_attempts={max_attempts}")
    raise ValueError("Failed to generate unique referral code after maximum attempts")
