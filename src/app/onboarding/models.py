"""ORM models for the onboarding bounded context.

Moved verbatim from ``app.models.py`` during the R-STRUCT-1 migration.
Column definitions, comments, and relationships are unchanged; only the
declarative base moved, from the module-local ``declarative_base()`` to the
single shared ``app.core.db.Base`` (spec §3.2).

Relationships that point at another context resolve through the shared
registry, which ``app/models.py`` guarantees is fully populated.
"""

from sqlalchemy import (
    CHAR,
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import relationship
from uuid_utils import uuid7

from app.core.db import Base
from app.core.enums import (  # noqa: F401
    FileType,
    FileUploadContext,
    FileUsageType,
    MessageType,
    ReportStatus,
    SubscriptionStatus,
    SubscriptionTier,
    TransactionSource,
    TransactionStatus,
    TransactionType,
    UploadedFileStatus,
)


class UserRole(Base):
    """
    Lookup table for "What best describes you?" onboarding options.

    Rows are managed dynamically — add/remove via the DB or an admin panel
    without code deploys.  The ``is_active`` flag allows soft-disabling an
    option while preserving historical associations.
    """

    __tablename__ = "user_roles"

    id = Column(
        CHAR(36), primary_key=True, default=lambda: str(uuid7()), unique=True, nullable=False
    )
    name = Column(
        String(100), unique=True, nullable=False, comment="Internal key, e.g. founder_entrepreneur"
    )
    display_name = Column(
        String(255), nullable=False, comment="UI label, e.g. Founder / Entrepreneur"
    )
    description = Column(String(500), nullable=True, comment="Subtitle shown in the UI card")
    discount_tag = Column(
        String(50), nullable=True, comment="Badge text, e.g. 50% OFF! — NULL for most roles"
    )
    is_active = Column(
        Boolean, default=True, nullable=False, comment="Soft-disable without deleting"
    )
    display_order = Column(Integer, nullable=False, default=0, comment="Controls UI grid ordering")
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    users = relationship("User", back_populates="role", lazy="dynamic")

    def __repr__(self):
        return f"<UserRole(id='{self.id}', name='{self.name}')>"


class ResearchInterest(Base):
    """
    Lookup table for "What do you want to research?" onboarding options (multi-select).
    """

    __tablename__ = "research_interests"

    id = Column(
        CHAR(36), primary_key=True, default=lambda: str(uuid7()), unique=True, nullable=False
    )
    name = Column(
        String(100), unique=True, nullable=False, comment="Internal key, e.g. market_research"
    )
    display_name = Column(String(255), nullable=False, comment="UI label, e.g. Market Research")
    description = Column(String(500), nullable=True, comment="Subtitle shown in the UI card")
    is_active = Column(
        Boolean, default=True, nullable=False, comment="Soft-disable without deleting"
    )
    display_order = Column(Integer, nullable=False, default=0, comment="Controls UI grid ordering")
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    users = relationship("UserResearchInterest", back_populates="research_interest", lazy="dynamic")

    def __repr__(self):
        return f"<ResearchInterest(id='{self.id}', name='{self.name}')>"


class University(Base):
    """
    Partner universities whose students get a discount.

    The ``email_domain`` is matched against the domain portion of the
    university email the student enters during onboarding.
    """

    __tablename__ = "universities"

    id = Column(
        CHAR(36), primary_key=True, default=lambda: str(uuid7()), unique=True, nullable=False
    )
    name = Column(String(255), nullable=False, comment="e.g. IIT Bombay, Stanford University")
    email_domain = Column(
        String(255), unique=True, nullable=False, comment="e.g. iitb.ac.in, stanford.edu"
    )
    discount_percentage = Column(
        Integer, nullable=False, default=50, comment="Discount offered to verified students"
    )
    is_active = Column(
        Boolean, default=True, nullable=False, comment="Can disable a partnership without deleting"
    )
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    users = relationship("User", back_populates="university", lazy="dynamic")

    def __repr__(self):
        return f"<University(id='{self.id}', name='{self.name}', domain='{self.email_domain}')>"


class UserResearchInterest(Base):
    """
    Junction table for the many-to-many relationship between users and
    research interests (the "What do you want to research?" multi-select).
    """

    __tablename__ = "user_research_interests"

    id = Column(
        CHAR(36), primary_key=True, default=lambda: str(uuid7()), unique=True, nullable=False
    )
    user_id = Column(CHAR(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    research_interest_id = Column(
        CHAR(36), ForeignKey("research_interests.id", ondelete="CASCADE"), nullable=False
    )
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (
        UniqueConstraint("user_id", "research_interest_id", name="uq_user_research_interest"),
    )

    user = relationship("User", back_populates="research_interests")
    research_interest = relationship("ResearchInterest", back_populates="users")

    def __repr__(self):
        return f"<UserResearchInterest(user_id='{self.user_id}', research_interest_id='{self.research_interest_id}')>"


# Standalone indexes, moved with the models they index.
Index("idx_user_roles_name", UserRole.name)
Index("idx_user_roles_is_active", UserRole.is_active)
Index("idx_research_interests_name", ResearchInterest.name)
Index("idx_research_interests_is_active", ResearchInterest.is_active)
Index("idx_user_research_interests_user_id", UserResearchInterest.user_id)
Index("idx_user_research_interests_interest_id", UserResearchInterest.research_interest_id)
Index("idx_universities_email_domain", University.email_domain)
Index("idx_universities_is_active", University.is_active)
