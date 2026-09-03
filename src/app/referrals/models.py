"""ORM models for the referrals bounded context.

Moved verbatim from ``src/db/database.py`` during the R-STRUCT-1 migration.
Column definitions, comments, and relationships are unchanged; only the
declarative base moved, from the module-local ``declarative_base()`` to the
single shared ``app.core.db.Base`` (spec §3.2).

Relationships that point at another context resolve through the shared
registry, which ``app/models.py`` guarantees is fully populated.
"""

from sqlalchemy import (
    CHAR, Boolean, CheckConstraint, Column, DateTime, Enum, Float, ForeignKey, Index,
    Integer, Numeric, String, Text, UniqueConstraint, func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import relationship
from uuid_utils import uuid7

from app.core.db import Base
from app.core.enums import (  # noqa: F401
    FileType, FileUploadContext, FileUsageType, MessageType, ReportStatus,
    SubscriptionStatus, SubscriptionTier, TransactionSource, TransactionStatus,
    TransactionType, UploadedFileStatus,
)


class Referral(Base):
    """
    Referral table to track referral relationships between users.
    
    When a user signs up with a referral code:
    - The referee (new user) gets 75,000 tokens (50k base + 25k bonus)
    - The referrer (existing user) gets 25,000 bonus tokens
    
    Attributes:
        id (str): Unique identifier for the referral (primary key).
        referrer_id (str): Foreign key to the user who referred (gave the code).
        referee_id (str): Foreign key to the user who was referred (used the code).
        referral_code_used (str): The referral code that was used.
        tokens_credited_to_referrer (int): Tokens credited to referrer (default 25,000).
        tokens_credited_to_referee (int): Tokens credited to referee (default 25,000).
        status (str): Status of the referral (completed, pending, failed).
        created_at (datetime): When the referral was created.
    
    Constraints:
        - referee_id must be unique (user can only be referred once)
        - referrer_id != referee_id (no self-referral)
        - Both foreign keys cascade on delete
    
    Relationships:
        referrer (User): The user who gave the referral code.
        referee (User): The user who used the referral code.
    """
    __tablename__ = 'referrals'
    
    id = Column(CHAR(36), primary_key=True, default=lambda: str(uuid7()), unique=True, nullable=False)
    referrer_id = Column(CHAR(36), ForeignKey('users.id', ondelete='CASCADE'), nullable=False, comment="User who gave the referral code")
    referee_id = Column(CHAR(36), ForeignKey('users.id', ondelete='CASCADE'), nullable=False, comment="User who used the referral code")
    referral_code_used = Column(String(12), nullable=False, index=True, comment="The referral code that was used")
    tokens_credited_to_referrer = Column(Integer, nullable=False, default=25000, comment="Bonus tokens credited to referrer")
    tokens_credited_to_referee = Column(Integer, nullable=False, default=25000, comment="Bonus tokens credited to referee")
    status = Column(String(20), nullable=False, default='completed', comment="Status: completed, pending, failed")
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    
    # Relationships
    referrer = relationship("User", foreign_keys=[referrer_id], back_populates="referrals_given")
    referee = relationship("User", foreign_keys=[referee_id], back_populates="referral_received")
    
    __table_args__ = (
        UniqueConstraint('referee_id', name='uq_referee_one_referral'),
        CheckConstraint('referrer_id != referee_id', name='ck_no_self_referral'),
    )
    
    def __repr__(self):
        return f"<Referral(id='{self.id}', referrer_id='{self.referrer_id}', referee_id='{self.referee_id}', code='{self.referral_code_used}')>"


# Standalone indexes, moved with the models they index.
Index('idx_referrals_referrer_id', Referral.referrer_id)
Index('idx_referrals_referee_id', Referral.referee_id)
Index('idx_referrals_code_used', Referral.referral_code_used)
