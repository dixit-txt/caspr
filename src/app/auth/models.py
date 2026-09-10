"""ORM models for the auth bounded context.

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
    String,
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


class User(Base):
    """
    Table representing a user in the system.

    Attributes:
        id (str): Unique identifier for each user (primary key).
        email (str): Email address of the user (must be unique).
        password_hash (str): The hashed password of the user.
        phone (str): The phone number of the user (must be unique).
        phone_country_code (str): The country code of the user's phone number.
        user_name (str): Full name of the user.
        is_google_verified (bool): Whether the user is verified via Google OAuth.
        is_verified (bool): Whether the user has verified their email.
        verified_at (datetime): The timestamp when the user was verified.
        T_C_verified (bool): Whether the user has accepted Terms and Conditions.
        user_role_id (str): FK to user_roles — selected during onboarding.
        university_email (str): University email for student verification.
        is_university_verified (bool): Whether the university email is verified.
        university_id (str): FK to universities — set after domain validation.
        onboarding_completed (bool): Whether the onboarding flow is finished.
        walkover_completed (bool): True once the user has explicitly finished the
            in-app walkover/walkthrough. Frontend GETs this on every authenticated
            session and POSTs to set it True when the user finishes the tour.
        created_at (datetime): The timestamp when the user record was created.
        updated_at (datetime): The timestamp when the user record was last updated.

    Relationships:
        messages (Message): One-to-many relationship with the Message table.
    """

    __tablename__ = "users"

    id = Column(
        CHAR(36), primary_key=True, default=lambda: str(uuid7()), unique=True, nullable=False
    )
    email = Column(String(255), unique=True, nullable=False)
    password_hash = Column(String(255), nullable=False)
    phone = Column(String(20), unique=True, nullable=False)
    phone_country_code = Column(String(8), nullable=True)
    user_name = Column(String(255), nullable=True)
    is_google_verified = Column(Boolean, default=False, nullable=False)
    is_verified = Column(Boolean, default=False, nullable=False)
    verified_at = Column(DateTime(timezone=True), nullable=True)
    t_c_verified = Column(
        Boolean,
        default=False,
        nullable=False,
        comment="Whether the user has accepted Terms and Conditions",
    )
    referral_code = Column(
        String(12),
        unique=True,
        nullable=False,
        index=True,
        comment="Unique referral code for this user",
    )

    # Onboarding fields
    user_role_id = Column(
        CHAR(36),
        ForeignKey("user_roles.id", ondelete="SET NULL"),
        nullable=True,
        comment="Selected role from onboarding",
    )
    university_email = Column(
        String(255), nullable=True, comment="University email for student verification"
    )
    is_university_verified = Column(
        Boolean,
        default=False,
        nullable=False,
        comment="Whether the university email has been verified",
    )
    university_id = Column(
        CHAR(36),
        ForeignKey("universities.id", ondelete="SET NULL"),
        nullable=True,
        comment="Partner university after domain validation",
    )
    onboarding_completed = Column(
        Boolean,
        default=False,
        nullable=False,
        comment="Whether the user finished the onboarding flow",
    )
    walkover_completed = Column(
        Boolean,
        default=False,
        nullable=False,
        comment=(
            "False until the user explicitly finishes the in-app walkover/walkthrough. "
            "Frontend reads this via GET /get-walkover-status and flips it to True via "
            "POST /complete-walkover once the tour is finished."
        ),
    )

    # Admin dashboard access (CEO / cofounder). Separate from onboarding user_role_id.
    dashboard_role = Column(
        String(50),
        nullable=True,
        index=True,
        comment="Admin dashboard role: ceo | cofounder | admin. NULL = no dashboard access.",
    )

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    # Relationships
    messages = relationship(
        "Message", back_populates="user", cascade="all, delete-orphan", lazy="dynamic"
    )
    wallet = relationship(
        "Wallet", back_populates="user", uselist=False, cascade="all, delete-orphan"
    )
    subscription = relationship(
        "Subscription", back_populates="user", uselist=False, cascade="all, delete-orphan"
    )
    referrals_given = relationship(
        "Referral", foreign_keys="Referral.referrer_id", back_populates="referrer", lazy="dynamic"
    )
    referral_received = relationship(
        "Referral", foreign_keys="Referral.referee_id", back_populates="referee", uselist=False
    )
    role = relationship("UserRole", back_populates="users", uselist=False)
    university = relationship("University", back_populates="users", uselist=False)
    research_interests = relationship(
        "UserResearchInterest", back_populates="user", cascade="all, delete-orphan", lazy="dynamic"
    )

    def __repr__(self):
        return f"<User(id='{self.id}', user_name='{self.user_name}', email='{self.email}')>"

    def __str__(self):
        return f"{self.user_name} ({self.email})"


# Standalone indexes, moved with the models they index.
Index("idx_users_user_role_id", User.user_role_id)
Index("idx_users_university_id", User.university_id)
Index("idx_users_onboarding_completed", User.onboarding_completed)
Index("idx_users_email", User.email)
Index("idx_users_phone", User.phone)
Index("idx_users_t_c_verified", User.t_c_verified)
Index("idx_users_dashboard_role", User.dashboard_role)
