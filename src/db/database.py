from sqlalchemy import (
    Column, String, DateTime, Integer, Float, ForeignKey, CHAR, func, Boolean, Text, Numeric, Enum,
    UniqueConstraint, CheckConstraint
)
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import relationship
from sqlalchemy import Index
from uuid_utils import uuid7

# Import enums from separate module for better organization
from src.db.enums import (
    ReportStatus, FileType, MessageType,  # noqa: F401
    SubscriptionTier, TransactionType, TransactionSource, TransactionStatus, SubscriptionStatus,  # noqa: F401
    UploadedFileStatus, FileUsageType, FileUploadContext  # noqa: F401
)

# Base class for all models
Base = declarative_base()

# ============================================================================
# Onboarding Lookup Tables (defined before User to allow FK references)
# ============================================================================

class UserRole(Base):
    """
    Lookup table for "What best describes you?" onboarding options.

    Rows are managed dynamically — add/remove via the DB or an admin panel
    without code deploys.  The ``is_active`` flag allows soft-disabling an
    option while preserving historical associations.
    """
    __tablename__ = 'user_roles'

    id = Column(CHAR(36), primary_key=True, default=lambda: str(uuid7()), unique=True, nullable=False)
    name = Column(String(100), unique=True, nullable=False, comment="Internal key, e.g. founder_entrepreneur")
    display_name = Column(String(255), nullable=False, comment="UI label, e.g. Founder / Entrepreneur")
    description = Column(String(500), nullable=True, comment="Subtitle shown in the UI card")
    discount_tag = Column(String(50), nullable=True, comment="Badge text, e.g. 50% OFF! — NULL for most roles")
    is_active = Column(Boolean, default=True, nullable=False, comment="Soft-disable without deleting")
    display_order = Column(Integer, nullable=False, default=0, comment="Controls UI grid ordering")
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    users = relationship("User", back_populates="role", lazy="dynamic")

    def __repr__(self):
        return f"<UserRole(id='{self.id}', name='{self.name}')>"


class ResearchInterest(Base):
    """
    Lookup table for "What do you want to research?" onboarding options (multi-select).
    """
    __tablename__ = 'research_interests'

    id = Column(CHAR(36), primary_key=True, default=lambda: str(uuid7()), unique=True, nullable=False)
    name = Column(String(100), unique=True, nullable=False, comment="Internal key, e.g. market_research")
    display_name = Column(String(255), nullable=False, comment="UI label, e.g. Market Research")
    description = Column(String(500), nullable=True, comment="Subtitle shown in the UI card")
    is_active = Column(Boolean, default=True, nullable=False, comment="Soft-disable without deleting")
    display_order = Column(Integer, nullable=False, default=0, comment="Controls UI grid ordering")
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    users = relationship("UserResearchInterest", back_populates="research_interest", lazy="dynamic")

    def __repr__(self):
        return f"<ResearchInterest(id='{self.id}', name='{self.name}')>"


class University(Base):
    """
    Partner universities whose students get a discount.

    The ``email_domain`` is matched against the domain portion of the
    university email the student enters during onboarding.
    """
    __tablename__ = 'universities'

    id = Column(CHAR(36), primary_key=True, default=lambda: str(uuid7()), unique=True, nullable=False)
    name = Column(String(255), nullable=False, comment="e.g. IIT Bombay, Stanford University")
    email_domain = Column(String(255), unique=True, nullable=False, comment="e.g. iitb.ac.in, stanford.edu")
    discount_percentage = Column(Integer, nullable=False, default=50, comment="Discount offered to verified students")
    is_active = Column(Boolean, default=True, nullable=False, comment="Can disable a partnership without deleting")
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    users = relationship("User", back_populates="university", lazy="dynamic")

    def __repr__(self):
        return f"<University(id='{self.id}', name='{self.name}', domain='{self.email_domain}')>"


# ============================================================================
# Core User Model
# ============================================================================

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
    __tablename__ = 'users'
    
    id = Column(CHAR(36), primary_key=True, default=lambda: str(uuid7()), unique=True, nullable=False)
    email = Column(String(255), unique=True, nullable=False)
    password_hash = Column(String(255), nullable=False)
    phone = Column(String(20), unique=True, nullable=False)
    phone_country_code = Column(String(8), nullable=True)
    user_name = Column(String(255), nullable=True)
    is_google_verified = Column(Boolean, default=False, nullable=False)
    is_verified = Column(Boolean, default=False, nullable=False)
    verified_at = Column(DateTime(timezone=True), nullable=True)
    t_c_verified = Column(Boolean, default=False, nullable=False, comment="Whether the user has accepted Terms and Conditions")
    referral_code = Column(String(12), unique=True, nullable=False, index=True, comment="Unique referral code for this user")

    # Onboarding fields
    user_role_id = Column(CHAR(36), ForeignKey('user_roles.id', ondelete='SET NULL'), nullable=True, comment="Selected role from onboarding")
    university_email = Column(String(255), nullable=True, comment="University email for student verification")
    is_university_verified = Column(Boolean, default=False, nullable=False, comment="Whether the university email has been verified")
    university_id = Column(CHAR(36), ForeignKey('universities.id', ondelete='SET NULL'), nullable=True, comment="Partner university after domain validation")
    onboarding_completed = Column(Boolean, default=False, nullable=False, comment="Whether the user finished the onboarding flow")
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
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    # Relationships
    messages = relationship("Message", back_populates="user", cascade="all, delete-orphan", lazy="dynamic")
    wallet = relationship("Wallet", back_populates="user", uselist=False, cascade="all, delete-orphan")
    subscription = relationship("Subscription", back_populates="user", uselist=False, cascade="all, delete-orphan")
    referrals_given = relationship("Referral", foreign_keys="Referral.referrer_id", back_populates="referrer", lazy="dynamic")
    referral_received = relationship("Referral", foreign_keys="Referral.referee_id", back_populates="referee", uselist=False)
    role = relationship("UserRole", back_populates="users", uselist=False)
    university = relationship("University", back_populates="users", uselist=False)
    research_interests = relationship("UserResearchInterest", back_populates="user", cascade="all, delete-orphan", lazy="dynamic")

    def __repr__(self):
        return f"<User(id='{self.id}', user_name='{self.user_name}', email='{self.email}')>"

    def __str__(self):
        return f"{self.user_name} ({self.email})"


class UserResearchInterest(Base):
    """
    Junction table for the many-to-many relationship between users and
    research interests (the "What do you want to research?" multi-select).
    """
    __tablename__ = 'user_research_interests'

    id = Column(CHAR(36), primary_key=True, default=lambda: str(uuid7()), unique=True, nullable=False)
    user_id = Column(CHAR(36), ForeignKey('users.id', ondelete='CASCADE'), nullable=False)
    research_interest_id = Column(CHAR(36), ForeignKey('research_interests.id', ondelete='CASCADE'), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (
        UniqueConstraint('user_id', 'research_interest_id', name='uq_user_research_interest'),
    )

    user = relationship("User", back_populates="research_interests")
    research_interest = relationship("ResearchInterest", back_populates="users")

    def __repr__(self):
        return f"<UserResearchInterest(user_id='{self.user_id}', research_interest_id='{self.research_interest_id}')>"


class Message(Base):
    """
    Message table to store chat messages with their associated metadata.
    
    Attributes:
        id (str): Unique identifier for each message (primary key).
        user_id (str): Foreign key reference to the user that sent the message.
        chat_title (str): Title of the chat.
        chat_messages (JSONB): Complete message data stored as JSONB.
        created_at (datetime): The timestamp when the message was created.
        updated_at (datetime): The timestamp when the message was last updated.
        is_deleted (bool): Whether the chat has been deleted by the user.
        deleted_at (datetime): The timestamp when the chat was deleted.

    Relationships:
        user (User): Many-to-one relationship with the User table (the sender of the message).
        reports (Report): One-to-many relationship with the Report table.
    """
    __tablename__ = 'messages'
    
    id = Column(CHAR(36), primary_key=True, default=lambda: str(uuid7()), unique=True, nullable=False, comment="Chat identifier")
    user_id = Column(CHAR(36), ForeignKey('users.id', ondelete='CASCADE'), nullable=False, comment="Reference to the user")
    chat_title = Column(String(255), nullable=True, comment="Title of the chat")
    chat_messages = Column(JSONB, nullable=True, comment="Complete message data stored as JSONB")
    message_citations = Column(JSONB, nullable=True, comment="Citations keyed by LangChain AIMessage id: {ai_msg_id: [url, ...]}")
    created_at = Column(DateTime(timezone=True), nullable=False, comment="Timestamp when message was created")
    updated_at = Column(DateTime(timezone=True), nullable=False, comment="Timestamp when message was last updated")
    is_deleted = Column(Boolean, default=False, nullable=False, comment="Whether the chat has been deleted by the user")
    deleted_at = Column(DateTime(timezone=True), nullable=True, comment="Timestamp when the chat was deleted")
    
    # Relationships
    user = relationship("User", back_populates="messages")
    reports = relationship("Report", back_populates="message")

    def __repr__(self):
        return f"<Message(id='{self.id}', user_id='{self.user_id}', created_at='{self.created_at}')>"

class Report(Base):
    """
    Report table to store base report information (acts as a container for versions).
    
    Attributes:
        id (str): Unique identifier for the report (primary key).
        chat_id (str): Foreign key reference to Message table.
        created_at (datetime): Timestamp when report was first created.
        title (str): Title of the report.
        layout (JSONB): Layout configuration for the report.
        length (str): Length description of the report (e.g. "comprehensive", "overview").
        summary (str): Summary of the report.
        citations (JSONB): Citations or references used in the report.
        poster_image_url (str): CDN URL for the poster/thumbnail image.
        s3_uri (JSONB): S3 URIs of current active version (denormalized for quick access).
        generated_at (datetime): When current version was generated (denormalized for quick access).
        current_version (int): Current active version number.
        status (str): Overall status (in_progress, report_generated, presentation_generated).
        last_activity_at (datetime): Last activity timestamp.

    Relationships:
        message (Message): Many-to-one relationship with the Message table.
        versions (ReportVersion): One-to-many relationship with ReportVersion table.
        cards (Card): One-to-many relationship with the Card table (for backward compatibility).
        publish (Publish): One-to-many relationship with the Publish table.
        tables (Table): One-to-many relationship with the Table table.
    """
    __tablename__ = 'reports'
    
    id = Column(CHAR(36), primary_key=True, default=lambda: str(uuid7()), unique=True, nullable=False)
    chat_id = Column(CHAR(36), ForeignKey('messages.id', ondelete='CASCADE'), nullable=False, comment="Reference to the chat/message")
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False, comment="Timestamp when report was first created")
    title = Column(String(255), nullable=True, comment="Title of the report")
    layout = Column(JSONB, nullable=True, comment="Layout configuration for the report")
    length = Column(String(50), nullable=True, comment="Length description of the report (comprehensive/overview)")
    summary = Column(Text, nullable=True, comment="Summary of the report")
    citations = Column(JSONB, nullable=True, comment="Citations or references used in the report")
    poster_image_url = Column(String(500), nullable=True, comment="CDN URL for the poster/thumbnail image")
    s3_uri = Column(JSONB, nullable=False, server_default='{"md": null, "pdf": null, "html": null, "pptx": null, "info_pdf": null}', comment="S3 URIs of current version (denormalized for quick access)")
    generated_at = Column(DateTime(timezone=True), nullable=True, comment="When current version was generated (denormalized for quick access)")
    current_version = Column(Integer, nullable=False, default=1, comment="Current active version number")
    status = Column(String(50), nullable=False, default=ReportStatus.DRAFT.value, comment="Overall status of the report")
    last_activity_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False, comment="Last activity timestamp")
    file_id = Column(String(200), nullable=True, comment="OpenAI file ID for the report")
    initial_markdown = Column(String(500), nullable=True, comment="S3 path of the initial markdown file for Ask Caspr")
    domain_name = Column(String(50), nullable=True, comment="Report domain: default, primary_research, due_diligence, industry_benchmarking, market_insight, rfp, business_plan")
    report_type = Column(String(10), nullable=True, default='study', comment="Report generation type: study or brief")
    
    # Relationships
    message = relationship("Message", back_populates="reports")
    versions = relationship("ReportVersion", back_populates="report", cascade="all, delete-orphan")
    cards = relationship("Card", back_populates="report", cascade="all, delete-orphan")
    publish = relationship("Publish", back_populates="report", cascade="all, delete-orphan")
    tables = relationship("Table", back_populates="report", cascade="all, delete-orphan")
    
    def __repr__(self):
        return f"<Report(id='{self.id}', title='{self.title}', current_version={self.current_version})>"

class ReportVersion(Base):
    """
    Table representing different versions of a report.
    Each version tracks which cards belong to it without duplicating unchanged cards.
    
    Attributes:
        id (str): Unique identifier for each report version (primary key).
        report_id (str): Foreign key reference to the base report.
        version (int): Version number (1, 2, 3, etc.).
        s3_uri (JSONB): S3 URIs specific to this version {file_type: s3_path}.
        poster_image_url (str): S3 URL for the poster/thumbnail image for this version.
        generated_at (datetime): When this version was generated.
        is_active (bool): Whether this is the current active version.
        status (str): Status of this version (in_progress, report_generated, presentation_generated).
        last_activity_at (datetime): Last activity timestamp for this version.
        created_at (datetime): When this version record was created.
        
    Relationships:
        report (Report): Many-to-one relationship with Report table.
        version_cards (ReportVersionCard): One-to-many with ReportVersionCard table.
    """
    __tablename__ = 'report_versions'
    
    id = Column(CHAR(36), primary_key=True, default=lambda: str(uuid7()), unique=True, nullable=False)
    report_id = Column(CHAR(36), ForeignKey('reports.id', ondelete='CASCADE'), nullable=False, comment="Reference to the base report")
    version = Column(Integer, nullable=False, comment="Version number")
    s3_uri = Column(JSONB, nullable=True, comment="S3 URIs for this version {file_type: s3_path}")
    poster_image_url = Column(String(500), nullable=True, comment="S3 URL for the poster/thumbnail image")
    generated_at = Column(DateTime(timezone=True), nullable=True, comment="When this version was generated")
    is_active = Column(Boolean, nullable=False, default=True, comment="Whether this is the active version")
    status = Column(String(50), nullable=False, default=ReportStatus.DRAFT.value, comment="Status of this version")
    last_activity_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False, comment="Last activity timestamp")
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    
    # Relationships
    report = relationship("Report", back_populates="versions")
    version_cards = relationship("ReportVersionCard", back_populates="report_version", cascade="all, delete-orphan")
    
    def __repr__(self):
        return f"<ReportVersion(id='{self.id}', report_id='{self.report_id}', version={self.version}, is_active={self.is_active})>"

class ReportVersionCard(Base):
    """
    Junction table mapping cards to report versions.
    This allows cards to be reused across versions without duplication.
    
    Attributes:
        id (str): Unique identifier (primary key).
        report_version_id (str): Foreign key to ReportVersion.
        parent_card_id (str): Foreign key to Card table's primary key (cards.id) - NOT the business card_id.
        sequence (int): Position of this card in this specific version.
        is_modified (bool): Whether this card was modified in this version.
        created_at (datetime): When this mapping was created.
        
    Relationships:
        report_version (ReportVersion): Many-to-one with ReportVersion.
        card (Card): Many-to-one with Card.
    """
    __tablename__ = 'report_version_cards'
    
    id = Column(CHAR(36), primary_key=True, default=lambda: str(uuid7()), unique=True, nullable=False)
    report_version_id = Column(CHAR(36), ForeignKey('report_versions.id', ondelete='CASCADE'), nullable=False, comment="Reference to report version")
    parent_card_id = Column(CHAR(36), ForeignKey('cards.id', ondelete='CASCADE'), nullable=False, comment="Reference to card table primary key (cards.id)")
    sequence = Column(Integer, nullable=False, comment="Position in this version")
    is_modified = Column(Boolean, nullable=False, default=False, comment="Whether card was modified in this version")
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    
    # Relationships
    report_version = relationship("ReportVersion", back_populates="version_cards")
    card = relationship("Card", foreign_keys=[parent_card_id])
    
    def __repr__(self):
        return f"<ReportVersionCard(report_version_id='{self.report_version_id}', parent_card_id='{self.parent_card_id}', sequence={self.sequence}, is_modified={self.is_modified})>"

class Card(Base):
    """
    Table representing individual sections of a report. Each card is a part of a larger report.
    Cards can be reused across report versions via ReportVersionCard junction table.

    Attributes:
        id (str): Unique identifier for each card (primary key).
        card_id (str): Business identifier for the card (links versions of same logical card).
        report_id (str): Foreign key reference to the base report (for quick filtering).
        title (str): Title of the card.
        sequence (int): The sequence number of the card (used to preserve order in the report).
        content (JSONB): The content of the card, stored in JSON format.
        citations (JSONB): Citations or references specific to this card, stored as JSON.
        created_at (datetime): The timestamp when the card was created.
        updated_at (datetime): The timestamp when the card was last modified.
        summary (str): Summary of the card content.
        type (str): Type of the card (title/subtitle/toc/section/viz).
        version (int): Version of the card content.
        is_active (bool): Whether the card is active.
        is_deleted (bool): Whether the card is deleted.
        changed_since_es (bool): Whether card changed since last ES generation.
        last_es_version_used (int): ES version number this card summary was used in.
    Relationships:
        report (Report): Many-to-one relationship with the Report table (the base report).
        tables (Tables): One-to-many relationship with the Tables table (tables in this card).
        versions (CardVersion): One-to-many relationship with the CardVersion table (versions of the card).
    """
    __tablename__ = 'cards'
    
    id = Column(CHAR(36), primary_key=True, default=lambda: str(uuid7()), unique=True, nullable=False,comment="Primary key - unique for each card instance")
    card_id = Column(CHAR(36), nullable=False,comment="Business card identifier - links versions of same logical card")
    report_id = Column(CHAR(36), ForeignKey('reports.id', ondelete='CASCADE'), nullable=False, comment="Reference to base report for filtering")
    title = Column(Text, nullable=True, comment="Title of the card")
    sequence = Column(Integer, nullable=False, comment="Sequence to preserve order in the report")
    sub_sections = Column(JSONB, nullable=True, comment="Sub section of the card")
    content = Column(JSONB, nullable=True, comment="Content of the card")
    citations = Column(JSONB, nullable=True, comment="Citations or references specific to this card")
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False, comment="Timestamp when the card was last modified")
    summary = Column(Text, nullable=True, comment="Summary of the card content")
    type = Column(String(50), nullable=True, comment="Type of the card (title/subtitle/toc/section/viz)")
    version = Column(Integer, nullable=True, comment="Version of the card")
    is_active = Column(Boolean, nullable=False, default=True, comment="Whether the card is active")
    is_deleted = Column(Boolean, nullable=False, default=False, comment="Whether the card is deleted")
    changed_since_es = Column(Boolean, nullable=False, default=False, comment="Whether card changed since last ES generation")
    last_es_version_used = Column(Integer, nullable=True, comment="ES version number this card summary was used in")
    report = relationship("Report", back_populates="cards")
    tables = relationship("Table", back_populates="card", cascade="all, delete-orphan")
    versions = relationship("CardVersion", back_populates="card", cascade="all, delete-orphan")
    def __repr__(self):
        return f"<Card(id='{self.id}', report_id='{self.report_id}', sequence={self.sequence})>"

class CardVersion(Base):
    """
    Table representing version of the card.
    Attributes:
        id (str): Unique identifier for each card version (primary key).
        parent_card_id (str): Foreign key to cards.id (primary key) - maintains referential integrity.
        section_id (str): Business card_id from cards.card_id - tracks which logical card was modified.
        subsection_id (str): ID of the subsection that was modified (null for section-level changes).
        user_instruction (str): User instruction for the card.
        refinement_type (str): Type of refinement - uses RefinementType enum values
            (e.g., 're_section', 're_subsection', 'del_section', 'manual_edit', 'edit_section', etc.).
        created_at (datetime): The timestamp when the card version was created.
        
    Relationships:
        card (Card): Many-to-one relationship with the Card table.
    """
    __tablename__ = 'card_versions'
    
    id = Column(CHAR(36), primary_key=True, default=lambda: str(uuid7()), unique=True, nullable=False)
    parent_card_id = Column(CHAR(36), ForeignKey('cards.id', ondelete='CASCADE'), nullable=False, comment="Foreign key to cards.id (primary key)")
    section_id = Column(CHAR(36), nullable=True, comment="Business card_id from cards.card_id - tracks which logical card was modified")
    subsection_id = Column(CHAR(36), nullable=True, comment="ID of the subsection that was modified (null for section-level changes)")
    user_instruction = Column(Text, nullable=True, comment="User instruction for the card")
    refinement_type = Column(String(50), nullable=True, comment="Refinement type section,subsection or visualization")
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    card = relationship("Card", back_populates="versions", foreign_keys=[parent_card_id])
    def __repr__(self):
        return f"<CardVersion(id='{self.id}', parent_card_id='{self.parent_card_id}', section_id='{self.section_id}', subsection_id='{self.subsection_id}')>"

class Publish(Base):
    """
    Table representing publishing metadata for a report.
    
    Attributes:
        id (str): Unique identifier for each publish record (primary key).
        report_id (str): Foreign key reference to the associated report.
        faq (JSONB): Frequently asked questions related to the report.
        insights (JSONB): Key insights from the report.
        industries_jobs (str): Industries or job sectors the report is relevant to.
        geographic_areas (str): Geographic areas covered in the report.
        special_emphasis (str): Special areas of emphasis in the report.
        audience (str): Target audience for the report.
        purpose (str): Purpose of the report.
        overview (str): Overview or summary of the report.
        media_details (JSONB): Details about media elements in the report.
        page_count (int): Number of pages in the report.
        source_count (int): Number of sources cited in the report.
        table_count (int): Number of tables in the report.
        viz_count (int): Number of visualizations in the report.
        created_at (datetime): The timestamp when the publish record was created.
        
    Relationships:
        report (Report): Many-to-one relationship with the Report table.
    """
    __tablename__ = 'publish'
    
    id = Column(CHAR(36), primary_key=True, default=lambda: str(uuid7()), unique=True, nullable=False)
    # card_id = Column(CHAR(36), ForeignKey('cards.id', ondelete='CASCADE'), nullable=False)
    report_id = Column(CHAR(36), ForeignKey('reports.id', ondelete='CASCADE'), nullable=False)
    faq = Column(JSONB, nullable=True, comment="Frequently asked questions related to the report")
    insights = Column(JSONB, nullable=True, comment="Key insights from the report")
    industries_jobs = Column(Text, nullable=True, comment="Industries or job sectors the report is relevant to")
    geographic_areas = Column(Text, nullable=True, comment="Geographic areas covered in the report")
    special_emphasis = Column(Text, nullable=True, comment="Special areas of emphasis in the report")
    audience = Column(Text, nullable=True, comment="Target audience for the report")
    purpose = Column(Text, nullable=True, comment="Purpose of the report")
    overview = Column(Text, nullable=True, comment="Overview or summary of the report")
    media_details = Column(JSONB, nullable=True, comment="Details about media elements in the report (poster_id, heygen_video_id, heygen_folder_id)")
    page_count = Column(Integer, nullable=True, comment="Number of pages in the report")
    source_count = Column(Integer, nullable=True, comment="Number of sources cited in the report")
    table_count = Column(Integer, nullable=True, comment="Number of tables in the report")
    viz_count = Column(Integer, nullable=True, comment="Number of visualizations in the report")
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    
    # Relationships
    report = relationship("Report", back_populates="publish")
    
    def __repr__(self):
        return f"<Publish(id='{self.id}', report_id='{self.report_id}')>"

class Table(Base):
    """
    Table representing visualizations and tables data for report cards.
    
    Attributes:
        id (str): Unique identifier for each table/visualization (primary key).
        report_id (str): Foreign key reference to the associated report.
        parent_card_id (str): Foreign key reference to the associated card.
        table_id (str): Identifier for the specific table within a card.
        table_title (str): Title of the table.
        table_markdown (Text): Markdown representation of the table.
        base64_s3_uri (str): S3 URI for the visualization image converted from base64.
        created_at (datetime): The timestamp when the record was created.
        
    Relationships:
        card (Card): Many-to-one relationship with the Card table.
        report (Report): Many-to-one relationship with the Report table.
    """
    __tablename__ = 'tables'
    
    id = Column(CHAR(36), primary_key=True, default=lambda: str(uuid7()), unique=True, nullable=False)
    report_id = Column(CHAR(36), ForeignKey('reports.id', ondelete='CASCADE'), nullable=False)
    parent_card_id = Column(CHAR(36), ForeignKey('cards.id', ondelete='CASCADE'), nullable=False)
    table_id = Column(String(255), nullable=False)
    table_title = Column(Text, nullable=True)
    table_markdown = Column(Text, nullable=True)
    base64_s3_uri = Column(Text, nullable=True, comment="S3 URI for the visualization image converted from base64")
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    
    card = relationship("Card", back_populates="tables")
    report = relationship("Report", back_populates="tables")
    def __repr__(self):
        return f"<Table(id='{self.id}', parent_card_id='{self.parent_card_id}', table_id='{self.table_id}')>"

class Subscriber(Base):
    """
    Table representing subscribers with email information.
    
    Attributes:
        id (int): Unique identifier for each subscriber (primary key, auto-increment).
        email (str): Email address of the subscriber (non-unique).
        created_at (datetime): The timestamp when the subscriber was created (UTC).
    """
    __tablename__ = 'subscribers'
    
    id = Column(Integer, primary_key=True, autoincrement=True, nullable=False)
    email = Column(String(255), nullable=False, comment="Email address of the subscriber")
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False, comment="Timestamp when subscriber was created in UTC")
    
    def __repr__(self):
        return f"<Subscriber(id={self.id}, email='{self.email}')>"

class Request(Base):
    """
    Table representing requests from users/visitors.
    
    Attributes:
        id (int): Unique identifier for each request (primary key, auto-increment).
        name (str): Name of the person making the request (non-nullable, non-unique).
        email (str): Email address of the requester (non-nullable, non-unique).
        website (str): Website URL of the requester (nullable, non-unique).
        description (str): Description of the request (non-nullable, non-unique).
    """
    __tablename__ = 'requests'
    
    id = Column(Integer, primary_key=True, autoincrement=True, nullable=False)
    name = Column(String(255), nullable=False, comment="Name of the person making the request")
    email = Column(String(255), nullable=False, comment="Email address of the requester")
    website = Column(String(500), nullable=True, comment="Website URL of the requester")
    description = Column(Text, nullable=True, comment="Description of the request")
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False, comment="Timestamp when request was created")
    
    def __repr__(self):
        return f"<Request(id={self.id}, name='{self.name}', email='{self.email}')>"


class CallBooking(Base):
    """
    Table representing call booking requests from users/visitors.

    Attributes:
        id (int): Unique identifier for each call booking (primary key, auto-increment).
        name (str): Name of the person booking the call (non-nullable).
        email (str): Email address of the requester (non-nullable).
        phone_country_code (str): Country code of the phone number (e.g. +1, +91).
        phone_number (str): Phone number without country code.
        brief (str): Optional brief/description from the user.
        created_at (datetime): Timestamp when the booking was created.
    """
    __tablename__ = 'call_bookings'

    id = Column(Integer, primary_key=True, autoincrement=True, nullable=False)
    name = Column(String(255), nullable=False, comment="Name of the person booking the call")
    email = Column(String(255), nullable=False, comment="Email address of the requester")
    phone_country_code = Column(String(20), nullable=False, comment="Country code of the phone number")
    phone_number = Column(String(50), nullable=False, comment="Phone number without country code")
    brief = Column(Text, nullable=True, comment="Optional brief or description from the user")
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False, comment="Timestamp when the booking was created")

    def __repr__(self):
        return f"<CallBooking(id={self.id}, name='{self.name}', email='{self.email}')>"


class CostTracker(Base):
    """
    Append-only log of per-LLM-call token usage for cost tracking.

    Maps to the cost-tracker JSON payload produced by
    ``save_raw_llm_response`` in ``src/core/llm_response_logger.py``.

    Attributes:
        id (str): Unique row id (UUID7 primary key).
        timestamp (datetime): When the LLM call occurred (UTC).
        model_name (str): Model id, e.g. gpt-4o.
        context (str): Call context label, e.g. normal_flow_fallback.
        functionality (str): Stable user-facing functionality bucket (e.g. report_generation).
        agent_name (str): Agent / stage name that made the call.
        chat_id (str): Chat identifier associated with the call.
        user_id (str): User identifier associated with the call.
        usage_metadata (dict): Full provider usage blob (JSONB).
        input_tokens (int): Prompt / input token count.
        output_tokens (int): Completion / output token count.
        estimated_cost (float): Estimated USD cost from llm_cost_calculator.
        cost_details (dict): Full cost breakdown JSON (payload["cost"]).
        created_at (datetime): When this row was inserted (UTC).
    """
    __tablename__ = 'costtracker'

    id = Column(CHAR(36), primary_key=True, default=lambda: str(uuid7()), unique=True, nullable=False)

    # --- payload fields (+ id) ---
    timestamp = Column(DateTime(timezone=True), nullable=False, index=True, comment="When the LLM call occurred (UTC)")
    model_name = Column(String(100), nullable=False, index=True, comment="Model id, e.g. gpt-4o")
    context = Column(String(255), nullable=True, comment="Call context label, e.g. normal_flow_fallback")
    functionality = Column(
        String(50),
        nullable=True,
        index=True,
        comment="Stable user-facing functionality bucket (see src.core.observability.functionality_context.Functionality), e.g. report_generation",
    )
    agent_name = Column(String(255), nullable=True, comment="Agent / stage that made the call")
    chat_id = Column(String(255), nullable=True, index=True, comment="Chat id associated with the call")
    user_id = Column(String(255), nullable=True, index=True, comment="User id associated with the call")
    usage_metadata = Column(JSONB, nullable=True, comment="Full provider usage blob from the LLM response")
    input_tokens = Column(Integer, nullable=False, default=0, comment="Prompt / input token count")
    output_tokens = Column(Integer, nullable=False, default=0, comment="Completion / output token count")
    estimated_cost = Column(Float, nullable=True, comment="Estimated USD cost for this LLM call")
    cost_details = Column(JSONB, nullable=True, comment="Full cost breakdown from llm_cost_calculator (payload cost block)")
    created_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        comment="When this costtracker row was inserted (UTC)",
    )

    def __repr__(self):
        return (
            f"<CostTracker(id='{self.id}', model_name='{self.model_name}', "
            f"agent_name='{self.agent_name}', input_tokens={self.input_tokens}, "
            f"output_tokens={self.output_tokens}, estimated_cost={self.estimated_cost})>"
        )


# ============================================================================
# Wallet/Token System Models
# ============================================================================

class Wallet(Base):
    """
    Wallet table - stores token balances and transaction history for each user.
    
    Each user has exactly one wallet that tracks their token balance. The wallet maintains
    both available tokens (ready to use) and reserved tokens (locked for pending operations).
    This table uses denormalized balance fields for performance, while TokenBatch remains
    the source of truth for detailed token tracking with expiry management.
    
    Attributes:
        id (str): Unique identifier for the wallet (UUID, primary key).
        user_id (str): Foreign key reference to users.id (unique constraint - one wallet per user).
        available_balance (int): Tokens currently available for use. This is a denormalized field
            updated on each transaction for fast reads. Defaults to 0.
        reserved_balance (int): Tokens temporarily locked for pending operations (e.g., report
            generation in progress). This is a denormalized field. Defaults to 0.
        created_at (datetime): Timestamp when the wallet was created (UTC, auto-generated).
        updated_at (datetime): Timestamp when the wallet was last modified (UTC, auto-updated).
    
    Relationships:
        user (User): One-to-one relationship with User table. Each user has exactly one wallet.
        token_batches (TokenBatch): One-to-many relationship with TokenBatch table. Stores all
            token batches with their expiry dates (source of truth for balance calculation).
        transactions (TokenTransaction): One-to-many relationship with TokenTransaction table.
            Complete audit trail of all token movements, ordered by created_at descending.
    
    Balance Management:
        - available_balance and reserved_balance are denormalized for performance.
        - TokenBatch table is the source of truth for detailed token tracking.
        - All balance updates must be atomic with TokenBatch changes.
        - Use database transactions to ensure consistency between Wallet and TokenBatch.
    
    Example:
        User signs up and receives 50,000 signup bonus tokens:
        - Wallet: available_balance=50000, reserved_balance=0
        - TokenBatch: 1 batch with 50000 tokens, expires in 90 days
        
        User starts generating a report (costs 5000 tokens):
        - Wallet: available_balance=45000, reserved_balance=5000
        - TokenBatch: remaining_tokens=45000, reserved_tokens=5000
        
        Report generation completes:
        - Wallet: available_balance=45000, reserved_balance=0
        - TokenBatch: remaining_tokens=45000, reserved_tokens=0
    """
    __tablename__ = 'wallets'
    
    id = Column(CHAR(36), primary_key=True, default=lambda: str(uuid7()), unique=True, nullable=False)
    user_id = Column(CHAR(36), ForeignKey('users.id', ondelete='CASCADE'), unique=True, nullable=False, comment="Reference to user (one wallet per user)")
    
    # Denormalized balances for fast reads (updated on each transaction)
    available_balance = Column(Integer, nullable=False, default=0, comment="Tokens available for use")
    reserved_balance = Column(Integer, nullable=False, default=0, comment="Tokens locked for pending operations")
    
    # Timestamps
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
    
    # Relationships
    user = relationship("User", back_populates="wallet")
    token_batches = relationship("TokenBatch", back_populates="wallet", cascade="all, delete-orphan")
    transactions = relationship("TokenTransaction", back_populates="wallet", cascade="all, delete-orphan", order_by="desc(TokenTransaction.created_at)")
    
    def __repr__(self):
        return f"<Wallet(id='{self.id}', user_id='{self.user_id}', balance={self.available_balance})>"


class Subscription(Base):
    """
    Subscription table (Parent) - manages user subscription status and tier.
    
    This is the parent table in the subscription system, maintaining the current state
    of each user's subscription. Each user has exactly one subscription record that tracks
    their current tier (free, plus, pro) and overall subscription status. The detailed
    billing history and payment intervals are stored in the child SubscriptionInterval table.
    
    Subscription Tiers:
        - FREE: Default tier with limited tokens (50,000 signup bonus)
        - PLUS: Paid tier with 25,000 tokens/month
        - PRO: Premium tier with 150,000 tokens/month
    
    Attributes:
        id (str): Unique identifier for the subscription record (UUID, primary key).
        user_id (str): Foreign key reference to users.id. Links this subscription to a user.
            Note: Not unique to support subscription history (user can have multiple subscriptions
            over time, though typically one active subscription).
        subscription_id (str): External subscription identifier from payment gateway
            (e.g., Razorpay subscription ID). Used for webhook processing and payment gateway
            integration. Nullable for free tier users. Indexed for quick payment gateway lookups.
        current_tier (str): Current active subscription tier. Uses SubscriptionTier enum:
            - FREE: No payment required, limited features
            - PLUS: Monthly subscription, moderate token allocation
            - PRO: Monthly subscription, high token allocation
            Defaults to FREE for new users.
        status (str): Overall subscription status. Uses SubscriptionStatus enum:
            - active: Subscription is currently active and valid
            - expired: Subscription period has ended, no auto-renewal
            - cancelled: User cancelled, may still be active until period ends
            - refunded: Payment was refunded, subscription terminated
            - paused: Temporarily paused (future feature)
            Defaults to ACTIVE.
        meta_data (JSONB): Flexible JSON storage for additional subscription metadata:
            - cancellation_reason: Why user cancelled
            - cancellation_date: When cancellation was requested
            - scheduled_tier_change: Upcoming tier change details
            - promotional_details: Special offers or discounts applied
            - payment_method: Preferred payment method
        created_at (datetime): Timestamp when subscription record was created (UTC, auto-generated).
        updated_at (datetime): Timestamp when subscription was last modified (UTC, auto-updated).
            Updates on tier changes, status changes, or metadata modifications.
    
    Relationships:
        user (User): One-to-one relationship with User table. Each user has one subscription
            record tracking their current subscription state.
        intervals (SubscriptionInterval): One-to-many relationship with SubscriptionInterval table.
            Contains complete billing and payment history, ordered by start_date descending.
            Each interval represents one billing cycle (typically monthly).
    
    Subscription Lifecycle:
        1. User signs up → Subscription created with tier=FREE, status=ACTIVE
        2. User subscribes to PLUS → tier=PLUS, status=ACTIVE, new SubscriptionInterval created
        3. Monthly renewal → New SubscriptionInterval created, status remains ACTIVE
        4. User upgrades to PRO → tier=PRO, new SubscriptionInterval created
        5. User cancels → status=CANCELLED (remains active until period ends)
        6. Period ends without renewal → status=EXPIRED, tier may revert to FREE
    
    Indexes:
        - idx_subscriptions_user_id: Fast lookup of user's subscription
        - idx_subscriptions_current_tier: Filter users by subscription tier
        - idx_subscriptions_status: Find subscriptions by status
        - idx_subscriptions_external_id: Quick lookup by payment gateway subscription ID
    
    Example:
        New user signup:
        - Subscription: user_id=<uuid>, tier=FREE, status=ACTIVE, subscription_id=NULL
        
        User subscribes to Plus:
        - Subscription: tier=PLUS, status=ACTIVE, subscription_id="sub_razorpay_123"
        - SubscriptionInterval: tier=PLUS, start_date=now, end_date=now+30days, status=ACTIVE
        
        User upgrades to Pro mid-cycle:
        - Subscription: tier=PRO, status=ACTIVE
        - Old SubscriptionInterval: status=EXPIRED (prorated)
        - New SubscriptionInterval: tier=PRO, start_date=now, end_date=now+30days
    """
    __tablename__ = 'subscriptions'
    
    id = Column(CHAR(36), primary_key=True, default=lambda: str(uuid7()), unique=True, nullable=False)
    user_id = Column(CHAR(36), ForeignKey('users.id', ondelete='CASCADE'), unique=False, nullable=False, comment="Reference to user (one subscription per user)")
    subscription_id = Column(String(100), nullable=True, comment="Reference to external subscription id (Razorpay subscription ID)")
    
    # Current subscription tier
    current_tier = Column(
        Enum(SubscriptionTier, name="subscription_tier_enum", native_enum=True),
        nullable=False,
        default=SubscriptionTier.FREE,
        comment="Current tier: free, plus, pro"
    )
    
    # Status
    status = Column(
        Enum(SubscriptionStatus, name="subscription_status_enum", native_enum=True),
        nullable=False,
        default=SubscriptionStatus.ACTIVE,
        comment="Status: active, expired, cancelled, refunded, paused"
    )
    meta_data = Column(JSONB, nullable=True, comment="Additional metadata")
    
    # Timestamps
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
    
    # Relationships
    user = relationship("User", back_populates="subscription")
    intervals = relationship("SubscriptionInterval", back_populates="subscription", cascade="all, delete-orphan", order_by="desc(SubscriptionInterval.start_date)")
    
    def __repr__(self):
        return f"<Subscription(id='{self.id}', user_id='{self.user_id}', tier='{self.current_tier}', status='{self.status}')>"


class SubscriptionInterval(Base):
    """
    SubscriptionInterval table (Child) - tracks individual billing cycles and payments.
    
    This is the child table in the subscription system, storing detailed records of each
    billing interval (typically monthly). A new row is created every time a subscription
    payment is processed, providing a complete audit trail of all subscription payments,
    token allocations, and billing periods.
    
    This table enables:
    - Complete payment history and financial reconciliation
    - Token allocation tracking per billing cycle
    - Subscription lifecycle management (renewals, upgrades, cancellations)
    - Prorated billing calculations for mid-cycle changes
    - Revenue analytics and reporting
    
    Attributes:
        id (str): Unique identifier for this billing interval (UUID, primary key).
        subscription_id (str): Foreign key reference to subscriptions.id (parent subscription).
            Links this interval to the user's subscription record. Indexed for fast lookup
            of all intervals for a subscription.
        tier (str): Subscription tier for this specific billing interval. Uses SubscriptionTier enum:
            - PLUS: 25,000 tokens/month tier
            - PRO: 150,000 tokens/month tier
            Note: FREE tier doesn't create intervals (no billing).
            This can differ from parent subscription's current_tier during upgrades/downgrades.
        start_date (datetime): Start timestamp of this billing interval (UTC). Typically the
            payment date or subscription start date. Indexed for time-based queries.
        end_date (datetime): End timestamp of this billing interval (UTC). Typically 30 days
            after start_date for monthly subscriptions. Indexed for finding active intervals
            and expiry checks.
        payment_id (str): External payment service reference ID (e.g., Razorpay payment_id).
            Used for payment reconciliation, refund processing, and webhook handling.
            Indexed for quick payment gateway lookups. Required for all paid intervals.
        amount (decimal): Amount charged for this billing interval in the specified currency.
            Precision: 10 digits total, 2 decimal places (e.g., 999.99).
            Nullable for promotional/free intervals.
        currency (str): ISO 4217 currency code (USD, INR, EUR, etc.). Should match the
            payment gateway currency. Nullable if amount is null.
        tokens_credited (int): Number of tokens allocated to the user for this billing interval.
            - PLUS tier: typically 25,000 tokens
            - PRO tier: typically 150,000 tokens
            These tokens are added to the user's wallet via a TokenBatch entry.
            Defaults to 0 (should be set based on tier).
        token_batch_id (str): Foreign key reference to token_batches.id. Links to the specific
            token batch created for this interval's token allocation. Used to track which tokens
            came from which subscription payment. Can be null if token crediting failed.
        status (str): Current status of this billing interval. Uses SubscriptionStatus enum:
            - active: Current active billing period
            - expired: Billing period has ended (normal completion)
            - cancelled: Subscription cancelled during this period
            - refunded: Payment was refunded, tokens may be revoked
            - paused: Billing paused (future feature)
            Defaults to ACTIVE. Indexed for finding active/expired intervals.
        meta_data (JSONB): Flexible JSON storage for additional billing details:
            - next_billing_date: Scheduled date for next auto-renewal
            - payment_method: Card/UPI/wallet details (masked)
            - payment_gateway_response: Full payment gateway webhook data
            - proration_details: Calculations for mid-cycle upgrades/downgrades
            - discount_applied: Promotional discount information
            - invoice_id: Reference to generated invoice
            - refund_reason: Why payment was refunded
            - autopay_enabled: Whether auto-renewal is enabled
        created_at (datetime): Timestamp when this interval record was created (UTC, auto-generated).
    
    Relationships:
        subscription (Subscription): Many-to-one relationship with Subscription table.
            Multiple intervals belong to one subscription (billing history).
        token_batch (TokenBatch): Many-to-one relationship with TokenBatch table.
            Links to the token batch created for this interval's token allocation.
    
    Billing Lifecycle Examples:
        
        1. Initial subscription to Plus tier:
           - subscription.tier = PLUS, subscription.status = ACTIVE
           - Interval: tier=PLUS, start=2026-01-01, end=2026-02-01, amount=9.99,
                      tokens_credited=25000, status=ACTIVE
        
        2. Auto-renewal after 30 days:
           - Previous interval: status changes from ACTIVE to EXPIRED
           - New interval: tier=PLUS, start=2026-02-01, end=2026-03-01, amount=9.99,
                          tokens_credited=25000, status=ACTIVE
        
        3. Mid-cycle upgrade from Plus to Pro:
           - subscription.tier = PRO
           - Old interval: status=EXPIRED (prorated refund calculated)
           - New interval: tier=PRO, start=2026-02-15, end=2026-03-15, amount=24.99,
                          tokens_credited=150000, status=ACTIVE
           - meta_data includes proration details
        
        4. User cancels subscription:
           - subscription.status = CANCELLED
           - Current interval: status=CANCELLED (remains until end_date)
           - No new interval created at end_date
        
        5. Payment fails during renewal:
           - Previous interval: status=EXPIRED
           - New interval: status=FAILED (created but payment unsuccessful)
           - subscription.status may change to EXPIRED
           - No tokens credited
        
        6. Refund processed:
           - Interval: status=REFUNDED
           - meta_data includes refund_reason and refund_date
           - Associated token_batch may be revoked/expired
    
    Indexes:
        - idx_subscription_intervals_subscription_id: Fast lookup of all intervals for a subscription
        - idx_subscription_intervals_tier: Filter intervals by subscription tier
        - idx_subscription_intervals_start_date: Time-based queries and sorting
        - idx_subscription_intervals_end_date: Find expiring/expired intervals
        - idx_subscription_intervals_payment_id: Quick payment gateway reference lookup
        - idx_subscription_intervals_status: Filter by interval status
        - idx_subscription_intervals_active_lookup: Composite index (subscription_id, status, end_date)
          for efficiently finding the current active interval
    
    Queries:
        Find current active interval:
        SELECT * FROM subscription_intervals 
        WHERE subscription_id = ? AND status = 'active' AND end_date > NOW()
        ORDER BY end_date DESC LIMIT 1;
        
        Calculate total revenue for a user:
        SELECT SUM(amount) FROM subscription_intervals 
        WHERE subscription_id = ? AND status IN ('active', 'expired');
        
        Get billing history:
        SELECT * FROM subscription_intervals 
        WHERE subscription_id = ? 
        ORDER BY start_date DESC;
    """
    __tablename__ = 'subscription_intervals'
    
    id = Column(CHAR(36), primary_key=True, default=lambda: str(uuid7()), unique=True, nullable=False)
    subscription_id = Column(CHAR(36), ForeignKey('subscriptions.id', ondelete='CASCADE'), nullable=False, comment="Reference to parent subscription")
    
    # Billing interval details
    tier = Column(
        Enum(SubscriptionTier, name="subscription_tier_enum", native_enum=True, create_constraint=False),
        nullable=False,
        comment="Subscription tier for this interval: plus, pro"
    )
    start_date = Column(DateTime(timezone=True), nullable=False, comment="Start of billing interval")
    end_date = Column(DateTime(timezone=True), nullable=False, comment="End of billing interval")
    
    # Payment tracking
    payment_id = Column(String(50), nullable=False, index=True, comment="Reference to payment_id in payment service")
    amount = Column(Numeric(10, 2), nullable=True, comment="Amount charged")
    currency = Column(String(10), nullable=True, comment="Currency of payment")
    
    # Token tracking
    tokens_credited = Column(Integer, nullable=False, default=0, comment="Tokens credited for this interval")
    token_batch_id = Column(CHAR(36), ForeignKey('token_batches.id', ondelete='SET NULL'), nullable=True, comment="Reference to token batch created")
    
    # Status tracking
    status = Column(
        Enum(SubscriptionStatus, name="subscription_status_enum", native_enum=True, create_constraint=False),
        nullable=False,
        default=SubscriptionStatus.ACTIVE,
        comment="Status: active, expired, cancelled, refunded"
    )
    meta_data = Column(JSONB, nullable=True, comment="Additional details like next autopay schedule date, payment method and other payment related data")
    
    # Timestamps
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    
    # Relationships
    subscription = relationship("Subscription", back_populates="intervals")
    token_batch = relationship("TokenBatch", foreign_keys=[token_batch_id])
    
    def __repr__(self):
        return f"<SubscriptionInterval(id='{self.id}', tier='{self.tier}', start='{self.start_date}', end='{self.end_date}', status='{self.status}')>"


class TokenBatch(Base):
    """
    Token batches with FIFO expiry tracking - SOURCE OF TRUTH for token balances.
    
    This table tracks individual batches of tokens with their expiry dates, enabling
    FIFO (First-In-First-Out) consumption and automatic expiry management. Each batch
    represents tokens acquired from a specific source (signup bonus, subscription,
    top-up purchase, refund, etc.) and maintains its own lifecycle.
    
    Token Consumption Strategy:
        - Always consume from the oldest non-expired batch first (FIFO).
        - Check expiry using: expires_at > NOW() (no separate is_expired flag).
        - When a batch is fully consumed (remaining_tokens = 0), it can be archived.
        - Multi-batch operations are supported when a single batch has insufficient tokens.
    
    Attributes:
        id (str): Unique identifier for the token batch (UUID, primary key).
        wallet_id (str): Foreign key reference to wallets.id. Links this batch to a user's wallet.
        idempotency_key (str): Optional idempotency key for payment operations to prevent
            duplicate token credits from the same payment.
        initial_tokens (int): Original number of tokens when the batch was created (immutable).
        remaining_tokens (int): Tokens currently available for use in this batch. This is the
            source of truth for available balance calculation. Updated on each transaction.
        reserved_tokens (int): Tokens temporarily locked for pending operations (e.g., report
            generation in progress). Defaults to 0.
        amount (decimal): Amount paid for this batch in the specified currency. Null for
            free token sources like signup bonuses or referrals.
        currency (str): ISO currency code (USD, INR, EUR, etc.). Null for free token sources.
        source_type (str): How tokens were obtained. Uses TransactionSource enum values:
            - signup_bonus: Free tokens on account creation
            - subscription: Monthly subscription tokens
            - topup: One-time token purchase
            - referral: Bonus from referral program
            - refund: Tokens returned from failed operations
            - expiry: Negative batch for expired tokens (if tracked)
        payment_id (str): External payment service reference ID (e.g., Razorpay payment_id).
            Indexed for quick payment lookup. Null for free token sources.
        status (str): Current status of the batch. Uses TransactionStatus enum:
            - pending: Payment initiated but not confirmed
            - completed: Tokens successfully credited
            - failed: Payment or credit operation failed
        meta_data (JSONB): Additional flexible data storage for:
            - Error messages for failed batches
            - Token batch IDs involved in multi-batch operations
            - Payment gateway specific details
            - Promotional campaign information
        start_at (datetime): When this batch becomes active/usable (UTC).
        expires_at (datetime): When this batch expires and tokens become unusable (UTC).
            Used for expiry checks: WHERE expires_at > NOW().
        created_at (datetime): When this batch record was created (UTC, auto-generated).
    
    Relationships:
        wallet (Wallet): Many-to-one relationship with Wallet table. Multiple batches can
            belong to the same wallet.
    
    Balance Calculation:
        The wallet's denormalized balances are calculated from active batches:
        - available_balance = SUM(remaining_tokens) WHERE expires_at > NOW()
        - reserved_balance = SUM(reserved_tokens) WHERE expires_at > NOW()
        
        These calculations should be performed atomically during transactions to ensure
        consistency between TokenBatch (source of truth) and Wallet (denormalized cache).
    
    Indexes:
        - idx_token_batches_wallet_id: Fast lookup of all batches for a wallet
        - idx_token_batches_source_type: Filter by token source
        - idx_token_batches_expires_at: Efficient expiry checks
        - idx_token_batches_payment_id: Quick payment reference lookup
        - idx_token_batches_balance_calc: Composite index for balance calculation queries
    
    Example:
        User signs up and gets 50,000 tokens (90-day expiry):
        - Batch 1: initial=50000, remaining=50000, source=signup_bonus, expires in 90 days
        
        User subscribes to Plus tier (25,000 tokens, 30-day expiry):
        - Batch 2: initial=25000, remaining=25000, source=subscription, expires in 30 days
        
        User consumes 60,000 tokens (FIFO - oldest batch first):
        - Batch 1: remaining=15000 (50000 consumed)
        - Batch 2: remaining=15000 (10000 consumed)
    """
    __tablename__ = 'token_batches'
    
    id = Column(CHAR(36), primary_key=True, default=lambda: str(uuid7()), unique=True, nullable=False)
    wallet_id = Column(CHAR(36), ForeignKey('wallets.id', ondelete='CASCADE'), nullable=False, comment="Reference to wallet")
    idempotency_key = Column(String(100), nullable=True, comment="Idempotency key for payment")    
    # Token tracking - SOURCE OF TRUTH
    initial_tokens = Column(Integer, nullable=False, comment="Original tokens in batch")
    remaining_tokens = Column(Integer, nullable=False, comment="Available tokens (source of truth for balance)")
    reserved_tokens = Column(Integer, nullable=False, default=0, comment="Tokens locked for pending operations")
    
    # Payment tracking
    amount = Column(Numeric(10, 2), nullable=True, comment="Amount paid for this batch")
    currency = Column(String(10), nullable=True, comment="Currency: USD, INR, EUR, etc.")
    
    # Source tracking
    source_type = Column(
        Enum(TransactionSource, name="transaction_source_enum", native_enum=True),
        nullable=False,
        comment="Source: signup_bonus, subscription, topup, report_generation, refund, expiry"
    )
    payment_id = Column(String(50), nullable=True, index=True, comment="Reference to payment_id in payment service")
    
    # Status
    status = Column(
        Enum(TransactionStatus, name="transaction_status_enum", native_enum=True),
        nullable=False,
        default=TransactionStatus.PENDING,
        comment="Status: completed, pending, failed"
    )

    meta_data = Column(JSONB, nullable=True, comment="Additional data (error messages, token_batch_ids for multi-batch operations, etc.)")
    
    # Expiry (no is_expired flag - use expires_at > NOW() for expiry check)
    start_at = Column(DateTime(timezone=True), nullable=False, comment="When this batch expires")
    expires_at = Column(DateTime(timezone=True), nullable=False, comment="When this batch expires")
    
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    
    # Relationships
    wallet = relationship("Wallet", back_populates="token_batches")
    
    def __repr__(self):
        return f"<TokenBatch(id='{self.id}', remaining={self.remaining_tokens}, reserved={self.reserved_tokens}, expires='{self.expires_at}')>"


class TokenTransaction(Base):
    """
    Transaction ledger providing complete audit trail of all token movements.
    
    This table records every token operation (credits, debits, reservations, releases)
    with balance snapshots for historical tracking and auditing. It serves as an
    immutable log of all wallet activity, enabling transaction history, dispute
    resolution, and financial reconciliation.
    
    Important: The balance_after and reserved_after fields are SNAPSHOTS captured at
    transaction time for audit purposes. They are NOT the source of truth for current
    balances - always use TokenBatch for real-time balance calculations.
    
    Attributes:
        id (str): Unique identifier for the transaction (UUID, primary key).
        wallet_id (str): Foreign key reference to wallets.id. Links this transaction to
            a user's wallet.
        transaction_type (str): Type of token movement. Uses TransactionType enum:
            - credit: Adding tokens to wallet (signup, purchase, refund)
            - debit: Removing tokens from wallet (report generation, expiry)
            - reserve: Locking tokens for pending operations
            - release: Unlocking previously reserved tokens
        source_type (str): Business reason for the transaction. Uses TransactionSource enum:
            - signup_bonus: Free tokens on account creation
            - subscription: Monthly subscription token allocation
            - topup: One-time token purchase
            - referral: Bonus from referral program (referrer or referee)
            - report_generation: Tokens consumed for creating reports
            - refund: Tokens returned from failed/cancelled operations
            - expiry: Tokens removed due to batch expiration
        tokens (int): Number of tokens involved in this transaction:
            - Positive values: Credits (adding tokens)
            - Negative values: Debits (removing tokens)
            - Zero: Status changes without token movement
        balance_after (int): Snapshot of available balance immediately after this transaction
            completed. Used for audit trail and historical balance reconstruction.
        reserved_after (int): Snapshot of reserved balance immediately after this transaction
            completed. Used for tracking locked tokens over time.
        report_id (str): Foreign key reference to reports.id. Set when transaction is related
            to report generation (reserve, release, or debit for report). Indexed for quick
            report-related transaction lookup. Null for non-report transactions.
        token_batch_id (str): Foreign key reference to token_batches.id. Points to the primary
            token batch affected by this transaction. For multi-batch operations, this is the
            first batch, with others listed in meta_data. Can be null for failed transactions.
        status (str): Current status of the transaction. Uses TransactionStatus enum:
            - pending: Transaction initiated but not yet completed
            - completed: Transaction successfully processed
            - failed: Transaction failed (error details in meta_data)
            - reversed: Transaction was reversed/rolled back
        description (str): Human-readable description of the transaction for display in
            transaction history UI (e.g., "Report generation: AI Market Analysis").
        meta_data (JSONB): Flexible JSON storage for additional transaction details:
            - error_messages: Failure reasons for failed transactions
            - token_batch_ids: Array of batch IDs for multi-batch operations
            - report_title: Title of associated report
            - payment_gateway_response: External payment service data
            - reversal_reason: Why a transaction was reversed
            - original_transaction_id: Link to original transaction for reversals
        created_at (datetime): Timestamp when the transaction was created (UTC, auto-generated).
            Indexed for chronological queries and transaction history.
    
    Relationships:
        wallet (Wallet): Many-to-one relationship with Wallet table. All transactions for
            a wallet are accessible via wallet.transactions (ordered by created_at desc).
        token_batch (TokenBatch): Many-to-one relationship with TokenBatch table. Links to
            the primary batch affected by this transaction.
    
    Transaction Flow Examples:
        
        1. User signs up (50,000 token bonus):
           - type=credit, source=signup_bonus, tokens=+50000
           - balance_after=50000, reserved_after=0
           - status=completed
        
        2. User starts report generation (costs 5,000 tokens):
           - type=reserve, source=report_generation, tokens=0
           - balance_after=45000, reserved_after=5000
           - status=pending, report_id=<report_uuid>
        
        3. Report generation completes successfully:
           - type=debit, source=report_generation, tokens=-5000
           - balance_after=45000, reserved_after=0
           - status=completed, report_id=<report_uuid>
        
        4. Report generation fails:
           - type=release, source=report_generation, tokens=0
           - balance_after=50000, reserved_after=0
           - status=completed, report_id=<report_uuid>
           - meta_data={"error": "API timeout", "refund_reason": "generation_failed"}
    
    Indexes:
        - idx_token_transactions_wallet_id: Fast lookup of all transactions for a wallet
        - idx_token_transactions_transaction_type: Filter by transaction type
        - idx_token_transactions_source_type: Filter by source/reason
        - idx_token_transactions_status: Find pending or failed transactions
        - idx_token_transactions_report_id: Quick lookup of report-related transactions
        - idx_token_transactions_token_batch_id: Find all transactions affecting a batch
        - idx_token_transactions_created_at: Chronological ordering and time-based queries
    
    Audit Trail Usage:
        - Transaction history: SELECT * FROM token_transactions WHERE wallet_id=? ORDER BY created_at DESC
        - Balance at specific time: Calculate from snapshots up to that timestamp
        - Dispute resolution: Full history of all token movements with reasons
        - Financial reconciliation: Match transactions with payment gateway records
    """
    __tablename__ = 'token_transactions'
    
    id = Column(CHAR(36), primary_key=True, default=lambda: str(uuid7()), unique=True, nullable=False)
    wallet_id = Column(CHAR(36), ForeignKey('wallets.id', ondelete='CASCADE'), nullable=False, comment="Reference to wallet")
    
    # Transaction details
    transaction_type = Column(
        Enum(TransactionType, name="transaction_type_enum", native_enum=True),
        nullable=False,
        comment="Type: credit, debit, reserve, release"
    )
    source_type = Column(
        Enum(TransactionSource, name="transaction_source_enum", native_enum=True, create_constraint=False),
        nullable=False,
        comment="Source: signup_bonus, subscription, topup, report_generation, refund, expiry"
    )
    
    # Amounts
    tokens = Column(Integer, nullable=False, comment="Token amount (+ve for credit, -ve for debit)")
    
    # Balance snapshots (for audit trail, NOT source of truth)
    balance_after = Column(Integer, nullable=False, comment="Available balance snapshot after transaction")
    reserved_after = Column(Integer, nullable=False, comment="Reserved balance snapshot after transaction")
    
    # References
    report_id = Column(CHAR(36), nullable=True, index=True, comment="Reference to report (if applicable)")
    token_batch_id = Column(CHAR(36), ForeignKey('token_batches.id', ondelete='SET NULL'), nullable=True, comment="Reference to primary affected token batch")
    
    # Status and metadata
    status = Column(
        Enum(TransactionStatus, name="transaction_status_enum", native_enum=True),
        nullable=False,
        default=TransactionStatus.PENDING,
        comment="Status: completed, pending, failed, reversed"
    )
    description = Column(String(255), nullable=True, comment="Human-readable description")
    meta_data = Column(JSONB, nullable=True, comment="Additional data (error messages, token_batch_ids for multi-batch operations, etc.)")
    
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    
    # Relationships
    wallet = relationship("Wallet", back_populates="transactions")
    token_batch = relationship("TokenBatch", foreign_keys=[token_batch_id])
    
    def __repr__(self):
        return f"<TokenTransaction(id='{self.id}', type='{self.transaction_type}', tokens={self.tokens}, status='{self.status}')>"


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


class RefinementHistory(Base):
    """
    RefinementHistory table to track refinement history for reports.
    
    This table maintains a one-to-one relationship with the Report table.
    Every report has exactly one refinement history record that stores
    all refinement operations performed on that report.
    
    Attributes:
        id (str): Unique identifier for the refinement history record (primary key).
        report_id (str): Foreign key reference to the associated report (unique - one history per report).
        refine_history (JSONB): JSON array storing the history of refinements made to the report.
            Each entry can contain:
            - timestamp: When the refinement was made
            - refinement_type: Type of refinement (section, subsection, visualization, etc.)
            - user_instruction: What the user asked for
            - affected_cards: Which cards were modified
            - version_created: Version number after refinement
        created_at (datetime): When this refinement history record was created.
        updated_at (datetime): When this refinement history was last updated.
    
    Relationships:
        report (Report): One-to-one relationship with the Report table.
    
    Note:
        A RefinementHistory record is automatically created when a Report is created,
        with refine_history initially set to null.
    """
    __tablename__ = 'refinement_history'
    
    id = Column(CHAR(36), primary_key=True, default=lambda: str(uuid7()), unique=True, nullable=False)
    report_id = Column(CHAR(36), ForeignKey('reports.id', ondelete='CASCADE'), unique=True, nullable=False, comment="Reference to the report (one history per report)")
    refine_history = Column(JSONB, nullable=True, comment="JSON array storing refinement history")
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
    
    # Relationships
    report = relationship("Report", backref="refinement_history", uselist=False)
    
    def __repr__(self):
        return f"<RefinementHistory(id='{self.id}', report_id='{self.report_id}')>"


class AskCasprChat(Base):
    """
    Table storing Ask Caspr chat conversations tied to report sections/subsections.
    
    Design rationale:
        - Uses report_id (not cards.id) because cards.id changes with every card version,
          while the chat is logically tied to a section within a report.
        - section_id and subsection_id never change across refinements — only the card's
          primary key (cards.id) changes when content is versioned.
        - Uses created_at ordering to determine the "current" chat entry. When content is
          refined, a new row is inserted with chat=NULL, effectively invalidating the
          previous chat since the frontend always picks the latest created_at.
    
    Workflow:
        1. Report cards generated → one row per section + one row per subsection, chat=NULL.
        2. User chats on a section/subsection → chat JSONB is updated with conversation.
        3. User refines a section/subsection → new row inserted with chat=NULL and fresh
           created_at. Old chat is no longer returned (superseded by latest created_at).
        4. Frontend query → SELECT ... WHERE report_id=? AND section_id=? AND subsection_id=?
           ORDER BY created_at DESC LIMIT 1.
    
    Attributes:
        id (str): Unique identifier (primary key).
        report_id (str): FK to reports.id — stable anchor for the chat.
        section_id (str): Business card_id from cards.card_id — identifies the logical section.
            Does NOT change across card versions.
        subsection_id (str): Subsection id from sub_sections JSONB. NULL for section-level chats.
            Does NOT change across card versions.
        chat (JSONB): Ask Caspr conversation stored as JSON array of messages:
            [{"role": "user", "content": "..."}, {"role": "assistant", "content": "..."}, ...]
            NULL when no chat has started yet (initial state or after refinement invalidation).
        created_at (datetime): When this entry was created. Used to determine the current/latest
            chat entry for a given (report_id, section_id, subsection_id) combination.
        updated_at (datetime): When the chat was last updated (e.g., new message appended).
    
    Relationships:
        report (Report): Many-to-one relationship with the Report table.
    """
    __tablename__ = 'ask_caspr_chats'
    
    id = Column(CHAR(36), primary_key=True, default=lambda: str(uuid7()), unique=True, nullable=False)
    report_id = Column(CHAR(36), ForeignKey('reports.id', ondelete='CASCADE'), nullable=False, comment="Reference to the report (stable anchor)")
    section_id = Column(CHAR(36), nullable=False, comment="Business card_id from cards.card_id — does not change across versions")
    subsection_id = Column(CHAR(36), nullable=True, comment="Subsection id — NULL for section-level chats, does not change across versions")
    chat = Column(JSONB, nullable=True, comment="Ask Caspr conversation as JSON array of {role, content} messages")
    version = Column(Integer, nullable=False, default=1, comment="Version number of the section/subsection content when this chat entry was created")
    card_version = Column(Integer, nullable=False, default=1, comment="cards.version at the time this entry was created/updated — links to card timeline for version navigation")
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False, comment="Used to determine the current/latest chat entry")
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False, comment="When chat was last updated")
    
    # Relationships
    report = relationship("Report", backref="ask_caspr_chats")
    
    def __repr__(self):
        return f"<AskCasprChat(id='{self.id}', report_id='{self.report_id}', section_id='{self.section_id}', subsection_id='{self.subsection_id}')>"


# ============================================================================
# File Upload System Models
# ============================================================================

class UserVectorStore(Base):
    """
    Stores the OpenAI vector store information for each user.

    Each user has exactly one vector store (one-to-one with users).
    The vector_store_id can be NULL if not yet created or if deleted by
    OpenAI / cron cleanup. A new vector store is created lazily on next upload.

    Attributes:
        id (str): Unique identifier (primary key).
        user_id (str): FK -> users.id (UNIQUE - one vector store per user).
        vector_store_id (str): OpenAI vector store ID (nullable if not created / deleted).
        last_accessed_at (datetime): Last time this vector store was accessed/used.
        created_at (datetime): When vector store record was created.
        updated_at (datetime): Last update timestamp.
    """
    __tablename__ = 'user_vector_stores'

    id               = Column(CHAR(36), primary_key=True, default=lambda: str(uuid7()), unique=True, nullable=False)
    user_id          = Column(CHAR(36), ForeignKey('users.id', ondelete='CASCADE'), unique=True, nullable=False, comment="Reference to user (one vector store per user)")
    vector_store_id  = Column(String(200), nullable=True, comment="OpenAI vector store ID (null if not created yet or deleted)")
    last_accessed_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False, comment="Last time this vector store was accessed/used")
    created_at       = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at       = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    # Relationships
    user               = relationship("User", backref="user_vector_store", uselist=False)
    vector_store_files = relationship("VectorStoreFile", back_populates="user_vector_store", cascade="all, delete-orphan")

    def __repr__(self):
        return f"<UserVectorStore(id='{self.id}', user_id='{self.user_id}', vector_store_id='{self.vector_store_id}')>"


class UploadedFile(Base):
    """
    Stores metadata for every file uploaded by users.
    
    Keeps the S3 URI so files can be re-uploaded to OpenAI if they get deleted.
    Supports soft-delete (is_deleted_by_user) and content-hash deduplication.
    
    Attributes:
        id (str): Unique identifier (primary key).
        user_id (str): FK -> users.id.
        s3_uri (str): Full S3 path of the uploaded file.
        original_filename (str): Original filename when uploaded.
        file_size (int): File size in bytes.
        file_type (str): File extension (pdf, docx, csv, etc.).
        content_hash (str): SHA-256 hash for duplicate detection.
        upload_context (str): 'in_chat' or 'standalone'.
        status (str): Upload pipeline status.
        is_deleted_by_user (bool): Soft-delete flag.
        deleted_by_user_at (datetime): When user deleted the file.
        last_accessed_at (datetime): Last time file was accessed/used.
        created_at (datetime): When file was first uploaded.
        updated_at (datetime): Last update timestamp.
    """
    __tablename__ = 'uploaded_files'
    
    id = Column(CHAR(36), primary_key=True, default=lambda: str(uuid7()), unique=True, nullable=False)
    user_id = Column(CHAR(36), ForeignKey('users.id', ondelete='CASCADE'), nullable=False, comment="Reference to user who uploaded the file")
    s3_uri = Column(String(500), nullable=False, comment="S3 path of the uploaded file (for re-upload)")
    original_filename = Column(String(255), nullable=True, comment="Original filename when uploaded")
    file_size = Column(Integer, nullable=True, comment="File size in bytes")
    file_type = Column(String(50), nullable=True, comment="File type/extension (pdf, docx, txt, etc.)")
    content_hash = Column(String(64), nullable=True, comment="SHA-256 hash for duplicate detection")
    upload_context = Column(Enum(FileUploadContext, name="file_upload_context_enum", native_enum=True), nullable=False, default=FileUploadContext.IN_CHAT, comment="Context: IN_CHAT or STANDALONE")
    status = Column(Enum(UploadedFileStatus, name="uploaded_file_status_enum", native_enum=True), nullable=False, default=UploadedFileStatus.PENDING_OPENAI_UPLOAD, comment="Upload pipeline status")
    is_deleted_by_user = Column(Boolean, default=False, nullable=False, comment="Soft-delete flag set by user")
    deleted_by_user_at = Column(DateTime(timezone=True), nullable=True, comment="When user deleted the file")
    last_accessed_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False, comment="Last time this file was accessed/used")
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
    # grep_agent_2 parse-time columns (populated after PDF parsing at upload)
    total_pages  = Column(Integer, nullable=True, comment="Total pages in the document (set after parsing)")
    doc_map      = Column(Text, nullable=True, comment="TOC-like structure string for worker system prompt")
    sections     = Column(JSONB, nullable=True, comment="Ordered list of detected heading strings")
    llm_summary  = Column(Text, nullable=True, comment="60-100 word LLM summary used by coordinator for routing")
    token_index  = Column(JSONB, nullable=True, comment="Inverted index: {token: {chunk_id_str: count}}")
    bigram_index = Column(JSONB, nullable=True, comment="Bigram index: {bigram: {chunk_id_str: count}}")
    is_parsed    = Column(Boolean, nullable=False, default=False, comment="True once chunks and indexes are stored in DB")
    parsed_at    = Column(DateTime(timezone=True), nullable=True, comment="When parsing completed")

    # Relationships
    user          = relationship("User", backref="uploaded_files")
    file_versions = relationship("FileVersion", back_populates="uploaded_file", cascade="all, delete-orphan")
    chunks        = relationship("UploadedFileChunk", back_populates="uploaded_file", cascade="all, delete-orphan")
    chat_files    = relationship("ChatFile", back_populates="uploaded_file", cascade="all, delete-orphan")

    def __repr__(self):
        return f"<UploadedFile(id='{self.id}', user_id='{self.user_id}', filename='{self.original_filename}', status='{self.status}')>"


class UploadedFileChunk(Base):
    """
    Stores individual text chunks extracted from an uploaded PDF at upload time.

    One row per ~800-char chunk. Enables lazy or full chunk loading at query time
    without re-parsing or re-downloading the original file from S3.

    Attributes:
        id (str): Unique identifier (primary key).
        uploaded_file_id (str): FK -> uploaded_files.id.
        chunk_id (int): 0-based sequential index within this document.
        text (str): ~800 char chunk text extracted from the PDF.
        page_num (int): 1-based page number this chunk came from.
        section (str): Most recent detected heading before this chunk.
        start_offset (int): Character offset from start of full document text.
        end_offset (int): Character offset end.
        created_at (datetime): When this chunk was stored.
    """
    __tablename__ = 'uploaded_file_chunks'

    id               = Column(CHAR(36), primary_key=True, default=lambda: str(uuid7()), unique=True, nullable=False)
    uploaded_file_id = Column(CHAR(36), ForeignKey('uploaded_files.id', ondelete='CASCADE'), nullable=False, comment="Reference to uploaded file")
    chunk_id         = Column(Integer, nullable=False, comment="0-based sequential chunk index within this document")
    text             = Column(Text, nullable=False, comment="~800 char chunk text extracted from the PDF")
    page_num         = Column(Integer, nullable=False, comment="1-based page number this chunk came from")
    section          = Column(String(500), nullable=True, comment="Most recent detected heading before this chunk")
    start_offset     = Column(Integer, nullable=False, comment="Character offset from start of full document text")
    end_offset       = Column(Integer, nullable=False, comment="Character offset end")
    created_at       = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    uploaded_file = relationship("UploadedFile", back_populates="chunks")

    __table_args__ = (
        UniqueConstraint('uploaded_file_id', 'chunk_id', name='uq_ufc_file_chunk'),
    )

    def __repr__(self):
        return f"<UploadedFileChunk(id='{self.id}', uploaded_file_id='{self.uploaded_file_id}', chunk_id={self.chunk_id}, page_num={self.page_num})>"


class FileVersion(Base):
    """
    Tracks OpenAI file IDs for uploaded files over time.

    One physical file can have multiple OpenAI file IDs over its lifetime
    (when OpenAI deletes a file and we re-upload from S3). Only one version
    is active at a time per uploaded_file.

    Attributes:
        id (str): Unique identifier (primary key).
        uploaded_file_id (str): FK -> uploaded_files.id.
        openai_file_id (str): Current OpenAI file ID.
        is_active (bool): Whether this is the current/active version.
        inactive_at (datetime): When this version became inactive.
        created_at (datetime): When this OpenAI file ID was created.
        last_verified_at (datetime): Last time we verified file exists in OpenAI.
    """
    __tablename__ = 'file_versions'

    id               = Column(CHAR(36), primary_key=True, default=lambda: str(uuid7()), unique=True, nullable=False)
    uploaded_file_id = Column(CHAR(36), ForeignKey('uploaded_files.id', ondelete='CASCADE'), nullable=False, comment="Reference to the uploaded file")
    openai_file_id   = Column(String(200), nullable=False, comment="OpenAI file ID for this version")
    is_active        = Column(Boolean, default=True, nullable=False, comment="Whether this file ID is currently active/valid")
    inactive_at      = Column(DateTime(timezone=True), nullable=True, comment="When this file version became inactive")
    created_at       = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    last_verified_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=True, comment="Last time we verified this file ID exists in OpenAI")

    # Relationships
    uploaded_file    = relationship("UploadedFile", back_populates="file_versions")
    vector_store_files = relationship("VectorStoreFile", back_populates="file_version", cascade="all, delete-orphan")
    chat_files       = relationship("ChatFile", back_populates="file_version")

    def __repr__(self):
        return f"<FileVersion(id='{self.id}', uploaded_file_id='{self.uploaded_file_id}', openai_file_id='{self.openai_file_id}', is_active={self.is_active})>"


class VectorStoreFile(Base):
    """
    Junction table linking vector stores to file versions.

    Attributes:
        id (str): Unique identifier (primary key).
        user_vector_store_id (str): FK -> user_vector_stores.id.
        file_version_id (str): FK -> file_versions.id.
        created_at (datetime): When the file was attached to the vector store.
        is_deleted (bool): Soft-delete flag (set when vector store is recreated).
        deleted_at (datetime): When this junction record was soft-deleted.
    """
    __tablename__ = 'vector_store_files'

    id                   = Column(CHAR(36), primary_key=True, default=lambda: str(uuid7()), unique=True, nullable=False)
    user_vector_store_id = Column(CHAR(36), ForeignKey('user_vector_stores.id', ondelete='CASCADE'), nullable=False, comment="Reference to user's vector store")
    file_version_id      = Column(CHAR(36), ForeignKey('file_versions.id', ondelete='CASCADE'), nullable=False, comment="Reference to file version (OpenAI file ID)")
    created_at           = Column(DateTime(timezone=True), server_default=func.now(), nullable=False, comment="When file was attached to vector store")
    is_deleted           = Column(Boolean, default=False, nullable=False, comment="Soft-delete flag (set when VS is recreated or file re-uploaded)")
    deleted_at           = Column(DateTime(timezone=True), nullable=True, comment="When this junction record was soft-deleted")

    # Relationships
    user_vector_store = relationship("UserVectorStore", back_populates="vector_store_files")
    file_version      = relationship("FileVersion", back_populates="vector_store_files")

    __table_args__ = (
        UniqueConstraint('user_vector_store_id', 'file_version_id', name='uq_vector_store_file_version'),
    )

    def __repr__(self):
        return f"<VectorStoreFile(id='{self.id}', vector_store_id='{self.user_vector_store_id}', file_version_id='{self.file_version_id}', is_deleted={self.is_deleted})>"


class ChatFile(Base):
    """
    Tracks which files are used in which chats (new uploads vs references).

    Attributes:
        id (str): Unique identifier (primary key).
        chat_id (str): FK -> messages.id (the chat this file is used in).
        uploaded_file_id (str): FK -> uploaded_files.id.
        file_version_id (str): FK -> file_versions.id (specific version used).
        usage_type (str): 'new_upload' or 'reference' (reused from previous upload).
        upload_context (str): Historical context copied from uploaded_files at association time.
        created_at (datetime): When this file was associated with this chat.
    """
    __tablename__ = 'chat_files'

    id               = Column(CHAR(36), primary_key=True, default=lambda: str(uuid7()), unique=True, nullable=False)
    chat_id          = Column(CHAR(36), ForeignKey('messages.id', ondelete='CASCADE'), nullable=False, comment="Reference to chat/message")
    uploaded_file_id = Column(CHAR(36), ForeignKey('uploaded_files.id', ondelete='CASCADE'), nullable=False, comment="Reference to uploaded file")
    file_version_id  = Column(CHAR(36), ForeignKey('file_versions.id', ondelete='SET NULL'), nullable=True, comment="Reference to specific file version used")
    usage_type       = Column(Enum(FileUsageType, name="file_usage_type_enum", native_enum=True), nullable=False, comment="NEW_UPLOAD or REFERENCE (reused from previous upload)")
    upload_context   = Column(Enum(FileUploadContext, name="file_upload_context_enum", native_enum=True, create_constraint=False), nullable=True, comment="Historical context: IN_CHAT or STANDALONE")
    created_at       = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    # Relationships
    chat          = relationship("Message", backref="chat_files")
    uploaded_file = relationship("UploadedFile", back_populates="chat_files")
    file_version  = relationship("FileVersion", back_populates="chat_files")

    def __repr__(self):
        return f"<ChatFile(id='{self.id}', chat_id='{self.chat_id}', uploaded_file_id='{self.uploaded_file_id}', usage_type='{self.usage_type}')>"


# ============================================================================
# Web Search Tracking
# ============================================================================

class WebSearchEvent(Base):
    """
    One row per web-search invocation — from chat (retrieve_latest_info), card
    generation (generate_cards / brief stream), card refinement (refine-card),
    or Ask Caspr.

    Attributes:
        trigger_source: 'chat', 'card_generation', 'brief', 'card_refinement', or 'ask_caspr'
        user_query:     The exact query string sent to the search engine.
        section_name:   For card_generation/brief/card_refinement/ask_caspr — which report section triggered it.
        model_used:     OpenAI model that issued the search (e.g. 'gpt-4o', 'gpt-5.4').
        total_results_count: Number of URLs returned by the search.
        cited_count:    Number of WebSearchCitation rows with was_cited_in_output=True
                         for this event. For events created before the dedup fix in
                         ``_normalize_links`` (see WebSearchCitation docstring), this can
                         overcount distinct cited sources — treat as an upper bound for
                         historical rows, not an exact count.
    """
    __tablename__ = 'web_search_events'

    id                   = Column(CHAR(36), primary_key=True, default=lambda: str(uuid7()), unique=True, nullable=False)
    operation_id         = Column(CHAR(36), default=lambda: str(uuid7()), nullable=False, comment="Correlation ID shared by attempts in one search operation")
    attempt_number       = Column(Integer, default=0, server_default="0", nullable=False)
    user_id              = Column(CHAR(36), ForeignKey('users.id', ondelete='SET NULL'), nullable=True, comment="Reference to user")
    chat_id              = Column(String(255), nullable=True, index=True, comment="Client-generated chat session ID (e.g. 'chat-67b79e2c') — not an FK")
    report_id            = Column(CHAR(36), ForeignKey('reports.id', ondelete='SET NULL'), nullable=True, comment="Reference to report (nullable — not always available at search time)")
    card_id              = Column(CHAR(36), nullable=True, comment="Logical card identifier; intentionally not an FK")
    trigger_source       = Column(String(30), nullable=False, comment="'chat', 'card_generation', 'brief', 'card_refinement', or 'ask_caspr'")
    section_name         = Column(Text, nullable=True, comment="Report section being generated (card_generation/brief only)")
    user_query           = Column(Text, nullable=True, comment="Query string sent to the web search")
    model_used           = Column(String(50), nullable=True, comment="Model used, e.g. gpt-4o, gpt-5.4")
    provider             = Column(String(50), nullable=True)
    provider_response_id = Column(String(255), nullable=True)
    provider_queries     = Column(JSONB, nullable=True)
    status               = Column(String(30), nullable=True)
    error_type           = Column(String(255), nullable=True)
    duration_ms          = Column(Integer, nullable=True)
    search_call_count    = Column(Integer, default=0, server_default="0", nullable=True)
    candidate_count      = Column(Integer, default=0, server_default="0", nullable=True)
    cited_count          = Column(Integer, default=0, server_default="0", nullable=True, comment="Upper bound for historical rows — see WebSearchCitation docstring for a pre-fix duplicate-URL caveat")
    total_results_count  = Column(Integer, nullable=True, comment="Compatibility count; equals candidate_count for new rows")
    usage_metadata       = Column(JSONB, nullable=True)
    created_at           = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    citations = relationship("WebSearchCitation", back_populates="search_event", cascade="all, delete-orphan")
    raw_response = relationship("WebSearchRawResponse", back_populates="search_event", uselist=False, cascade="all, delete-orphan")

    def __repr__(self):
        return f"<WebSearchEvent(id='{self.id}', trigger='{self.trigger_source}', model='{self.model_used}', results={self.total_results_count})>"


class WebSearchCitation(Base):
    """
    One row per URL returned by a single WebSearchEvent.

    Attributes:
        domain:             Extracted hostname (e.g. 'who.int') — indexed for domain analytics.
        title:              Page title from the OpenAI search result (may be null for chat path).
        snippet:            Text snippet from the OpenAI search result.
        was_cited_in_output: True if this URL was actually cited inline in the final output;
                             None when not yet determined.

    NOTE FOR ANALYSIS / DASHBOARDS (including LLM-driven ones): a bug present
    until the fix in ``src/db/web_search_db.py::_normalize_links`` (search for
    "cited_row_index_by_url") could persist the SAME cited URL as TWO separate
    rows per ``search_event_id`` — one sourced from OpenAI's own
    ``url_citation`` annotations, one from a plain-text URL regex fallback
    over the final rendered card/answer — both with ``was_cited_in_output``
    True. This does NOT affect candidate rows (``was_cited_in_output`` False),
    which can legitimately repeat a URL across different
    ``search_call_index``/``result_rank`` values by design.
    For rows created before that fix, a naive ``COUNT(*)`` of cited rows per
    ``search_event_id``/report/section can overstate the number of distinct
    sources actually cited. When counting or listing cited sources, prefer
    ``COUNT(DISTINCT url)`` / ``GROUP BY url`` per ``search_event_id`` rather
    than raw row counts, and treat ``web_search_events.cited_count`` (which
    predates the fix and was NOT backfilled) as an upper bound rather than an
    exact count for historical events.
    """
    __tablename__ = 'web_search_citations'

    id              = Column(CHAR(36), primary_key=True, default=lambda: str(uuid7()), unique=True, nullable=False)
    search_event_id = Column(CHAR(36), ForeignKey('web_search_events.id', ondelete='CASCADE'), nullable=False, comment="Parent search event")
    user_id         = Column(CHAR(36), ForeignKey('users.id', ondelete='SET NULL'), nullable=True, comment="Denormalized for fast per-user queries")
    report_id       = Column(CHAR(36), ForeignKey('reports.id', ondelete='SET NULL'), nullable=True, comment="Denormalized for fast per-report queries")
    url             = Column(Text, nullable=False, comment="Canonical HTTP(S) URL")
    raw_url         = Column(Text, nullable=True, comment="URL exactly as supplied by the provider or output")
    domain          = Column(String(255), nullable=True, comment="Extracted hostname, e.g. reuters.com — indexed")
    title           = Column(Text, nullable=True, comment="Page title from search result")
    snippet         = Column(Text, nullable=True, comment="Text snippet from search result")
    search_call_id  = Column(String(255), nullable=True)
    search_call_index = Column(Integer, nullable=True)
    result_rank     = Column(Integer, nullable=True)
    citation_order  = Column(Integer, nullable=True)
    was_cited_in_output = Column(Boolean, nullable=True, comment="True if URL was cited inline in final output; None = unknown")
    created_at      = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    search_event = relationship("WebSearchEvent", back_populates="citations")

    def __repr__(self):
        return f"<WebSearchCitation(id='{self.id}', domain='{self.domain}', cited={self.was_cited_in_output})>"


class WebSearchRawResponse(Base):
    """
    One row per raw OpenAI Responses-API payload for a web-search-enabled call
    made during card generation, card refinement, or Ask Caspr. 1:1 with
    WebSearchEvent; kept in its own table so the full JSON body doesn't bloat
    the hot event/citation tables used for routine analytics queries.

    Attributes:
        provider_response_id: OpenAI's own response.id — lets you fetch/replay
                               the exact call via the API/dashboard.
        raw_response:          Full response.model_dump(mode='json') payload.
        is_truncated:          True if raw_response was replaced with a small
                                placeholder because it exceeded the size guard.
    """
    __tablename__ = 'web_search_raw_responses'

    id                    = Column(CHAR(36), primary_key=True, default=lambda: str(uuid7()), unique=True, nullable=False)
    search_event_id       = Column(CHAR(36), ForeignKey('web_search_events.id', ondelete='CASCADE'), nullable=False, unique=True, comment="Parent WebSearchEvent (1:1)")
    operation_id          = Column(CHAR(36), nullable=False, comment="Denormalized from parent event — correlate rounds without a join")
    attempt_number        = Column(Integer, nullable=True)

    user_id               = Column(CHAR(36), ForeignKey('users.id', ondelete='SET NULL'), nullable=True, comment="Denormalized for fast per-user queries")
    report_id             = Column(CHAR(36), ForeignKey('reports.id', ondelete='SET NULL'), nullable=True, comment="Denormalized for fast per-report queries")
    chat_id               = Column(String(255), nullable=True, comment="Client-generated chat session ID — not an FK")
    card_id               = Column(CHAR(36), nullable=True, comment="Logical card identifier; intentionally not an FK")
    trigger_source        = Column(String(30), nullable=False, comment="'card_generation', 'brief', 'card_refinement', or 'ask_caspr'")

    provider              = Column(String(50), nullable=True, comment="'openai'")
    provider_response_id  = Column(String(255), nullable=True, comment="OpenAI response.id")
    previous_response_id  = Column(String(255), nullable=True, comment="response.previous_response_id, if OpenAI-side chaining is used")
    model_used            = Column(String(50), nullable=True, comment="response.model, e.g. gpt-5.4")
    response_status       = Column(String(30), nullable=True, comment="response.status — 'completed', 'incomplete', 'failed', etc.")

    raw_response          = Column(JSONB, nullable=False, comment="Full response.model_dump(mode='json') from the OpenAI SDK")
    payload_size_bytes    = Column(Integer, nullable=True, comment="Size in bytes of the serialized raw response, before any truncation")
    is_truncated          = Column(Boolean, nullable=False, default=False, server_default="false", comment="True if raw_response was truncated due to the size guard")

    response_created_at   = Column(DateTime(timezone=True), nullable=True, comment="OpenAI's response.created_at (epoch converted to UTC)")
    created_at            = Column(DateTime(timezone=True), server_default=func.now(), nullable=False, comment="When this row was inserted (UTC)")

    search_event = relationship("WebSearchEvent", back_populates="raw_response", uselist=False)

    def __repr__(self):
        return f"<WebSearchRawResponse(id='{self.id}', search_event_id='{self.search_event_id}', provider_response_id='{self.provider_response_id}')>"


# ============================================================================
# Onboarding System Indexes
# ============================================================================

Index('idx_user_roles_name', UserRole.name)
Index('idx_user_roles_is_active', UserRole.is_active)
Index('idx_research_interests_name', ResearchInterest.name)
Index('idx_research_interests_is_active', ResearchInterest.is_active)
Index('idx_user_research_interests_user_id', UserResearchInterest.user_id)
Index('idx_user_research_interests_interest_id', UserResearchInterest.research_interest_id)
Index('idx_universities_email_domain', University.email_domain)
Index('idx_universities_is_active', University.is_active)
Index('idx_users_user_role_id', User.user_role_id)
Index('idx_users_university_id', User.university_id)
Index('idx_users_onboarding_completed', User.onboarding_completed)

# Index for reports table
Index('idx_reports_domain_name', Report.domain_name)

# Index for referrals table
Index('idx_referrals_referrer_id', Referral.referrer_id)
Index('idx_referrals_referee_id', Referral.referee_id)
Index('idx_referrals_code_used', Referral.referral_code_used)

# Index for refinement_history table
Index('idx_refinement_history_report_id', RefinementHistory.report_id)

# Indexes for ask_caspr_chats table
Index('idx_ask_caspr_chats_report_id', AskCasprChat.report_id)
Index('idx_ask_caspr_chats_section_id', AskCasprChat.section_id)
Index('idx_ask_caspr_chats_subsection_id', AskCasprChat.subsection_id)
Index('idx_ask_caspr_chats_created_at', AskCasprChat.created_at)
# Composite index for the primary query pattern: get latest chat for a (report, section, subsection) tuple
Index('idx_ask_caspr_chats_latest_lookup', AskCasprChat.report_id, AskCasprChat.section_id, AskCasprChat.subsection_id, AskCasprChat.created_at)

# Index for tables table
Index('idx_tables_report_id', Table.report_id)

# Indexes for user-related queries
Index('idx_users_email', User.email)
Index('idx_users_phone', User.phone)
Index('idx_users_t_c_verified', User.t_c_verified)
Index('idx_users_dashboard_role', User.dashboard_role)

# Indexes for messages table
Index('idx_messages_user_id', Message.user_id)
Index('idx_messages_is_deleted', Message.is_deleted)

# Index for reports table
Index('idx_reports_chat_id', Report.chat_id)
Index('idx_reports_current_version', Report.current_version)
Index('idx_reports_status', Report.status)

# Indexes for report_versions table
Index('idx_report_versions_report_id', ReportVersion.report_id)
Index('idx_report_versions_version', ReportVersion.version)
Index('idx_report_versions_is_active', ReportVersion.is_active)
Index('idx_report_versions_status', ReportVersion.status)

# Indexes for report_version_cards table
Index('idx_report_version_cards_version_id', ReportVersionCard.report_version_id)
Index('idx_report_version_cards_parent_card_id', ReportVersionCard.parent_card_id)
Index('idx_report_version_cards_sequence', ReportVersionCard.sequence)

# Index for cards table
Index('idx_cards_id', Card.id)
Index('idx_cards_card_id', Card.card_id)
Index('idx_cards_report_id', Card.report_id)
Index('idx_cards_sequence', Card.sequence)
Index('idx_tables_table_id', Table.table_id)

#Index for card_versions table
Index('idx_card_versions_parent_card_id', CardVersion.parent_card_id)
Index('idx_card_versions_section_id', CardVersion.section_id)
Index('idx_card_versions_subsection_id', CardVersion.subsection_id)
Index('idx_card_versions_refinement_type', CardVersion.refinement_type)

# Index for publish table
Index('idx_publish_report_id', Publish.report_id)

# Indexes for grep_agent_2 file upload system
Index('idx_uploaded_files_is_parsed', UploadedFile.is_parsed)
Index('idx_ufc_uploaded_file_id', UploadedFileChunk.uploaded_file_id)
Index('idx_ufc_file_page', UploadedFileChunk.uploaded_file_id, UploadedFileChunk.page_num)
Index('idx_chat_files_chat_id', ChatFile.chat_id)
Index('idx_chat_files_uploaded_file_id', ChatFile.uploaded_file_id)

# Indexes for web_search_events and web_search_citations tables
Index('idx_web_search_events_user_id', WebSearchEvent.user_id)
Index('idx_web_search_events_chat_id', WebSearchEvent.chat_id)
Index('idx_web_search_events_report_id', WebSearchEvent.report_id)
Index('idx_web_search_events_trigger_source', WebSearchEvent.trigger_source)
Index('idx_web_search_events_created_at', WebSearchEvent.created_at)
Index('idx_web_search_events_operation_id', WebSearchEvent.operation_id)
Index('idx_web_search_events_provider', WebSearchEvent.provider)
Index('idx_web_search_events_status', WebSearchEvent.status)
Index('idx_web_search_events_card_id', WebSearchEvent.card_id)
Index('idx_web_search_citations_search_event_id', WebSearchCitation.search_event_id)
Index('idx_web_search_citations_user_id', WebSearchCitation.user_id)
Index('idx_web_search_citations_report_id', WebSearchCitation.report_id)
Index('idx_web_search_citations_domain', WebSearchCitation.domain)
Index('idx_web_search_citations_was_cited', WebSearchCitation.was_cited_in_output)
Index(
    'idx_web_search_citations_event_role_rank',
    WebSearchCitation.search_event_id,
    WebSearchCitation.was_cited_in_output,
    WebSearchCitation.search_call_index,
    WebSearchCitation.result_rank,
)

# Indexes for web_search_raw_responses table
Index('idx_web_search_raw_responses_search_event_id', WebSearchRawResponse.search_event_id, unique=True)
Index('idx_web_search_raw_responses_operation_id', WebSearchRawResponse.operation_id)
Index('idx_web_search_raw_responses_provider_response_id', WebSearchRawResponse.provider_response_id)
Index('idx_web_search_raw_responses_user_id', WebSearchRawResponse.user_id)
Index('idx_web_search_raw_responses_report_id', WebSearchRawResponse.report_id)
Index('idx_web_search_raw_responses_trigger_source', WebSearchRawResponse.trigger_source)
Index('idx_web_search_raw_responses_created_at', WebSearchRawResponse.created_at)

# Indexes for subscribers table
Index('idx_subscribers_email', Subscriber.email)

# Indexes for requests table
Index('idx_requests_email', Request.email)

# Indexes for call_bookings table
Index('idx_call_bookings_email', CallBooking.email)
Index('idx_call_bookings_created_at', CallBooking.created_at)

# Indexes for costtracker table
Index('idx_costtracker_timestamp', CostTracker.timestamp)
Index('idx_costtracker_user_id', CostTracker.user_id)
Index('idx_costtracker_chat_id', CostTracker.chat_id)
Index('idx_costtracker_model_name', CostTracker.model_name)
Index('idx_costtracker_created_at', CostTracker.created_at)
Index('idx_costtracker_estimated_cost', CostTracker.estimated_cost)
Index('idx_costtracker_functionality', CostTracker.functionality)

# Indexes for wallets table
Index('idx_wallets_user_id', Wallet.user_id)

# Indexes for subscriptions table (parent)
Index('idx_subscriptions_user_id', Subscription.user_id)
Index('idx_subscriptions_current_tier', Subscription.current_tier)
Index('idx_subscriptions_status', Subscription.status)
Index('idx_subscriptions_external_id', Subscription.subscription_id)

# Indexes for subscription_intervals table (child - payment history)
Index('idx_subscription_intervals_subscription_id', SubscriptionInterval.subscription_id)
Index('idx_subscription_intervals_tier', SubscriptionInterval.tier)
Index('idx_subscription_intervals_start_date', SubscriptionInterval.start_date)
Index('idx_subscription_intervals_end_date', SubscriptionInterval.end_date)
Index('idx_subscription_intervals_payment_id', SubscriptionInterval.payment_id)
Index('idx_subscription_intervals_status', SubscriptionInterval.status)
# Composite index for finding active interval
Index('idx_subscription_intervals_active_lookup', SubscriptionInterval.subscription_id, SubscriptionInterval.status, SubscriptionInterval.end_date)

# Indexes for token_batches table
Index('idx_token_batches_wallet_id', TokenBatch.wallet_id)
Index('idx_token_batches_source_type', TokenBatch.source_type)
Index('idx_token_batches_expires_at', TokenBatch.expires_at)
Index('idx_token_batches_payment_id', TokenBatch.payment_id)
# Composite index for balance calculation query (uses expires_at for expiry check)
Index('idx_token_batches_balance_calc', TokenBatch.wallet_id, TokenBatch.expires_at)

# Indexes for token_transactions table
Index('idx_token_transactions_wallet_id', TokenTransaction.wallet_id)
Index('idx_token_transactions_transaction_type', TokenTransaction.transaction_type)
Index('idx_token_transactions_source_type', TokenTransaction.source_type)
Index('idx_token_transactions_status', TokenTransaction.status)
Index('idx_token_transactions_report_id', TokenTransaction.report_id)
Index('idx_token_transactions_token_batch_id', TokenTransaction.token_batch_id)
Index('idx_token_transactions_created_at', TokenTransaction.created_at)

# ============================================================================
# File Upload System Indexes
# ============================================================================

# Indexes for user_vector_stores table
Index('idx_user_vector_stores_user_id', UserVectorStore.user_id)
Index('idx_user_vector_stores_last_accessed', UserVectorStore.last_accessed_at)

# Indexes for uploaded_files table
Index('idx_uploaded_files_user_id', UploadedFile.user_id)
Index('idx_uploaded_files_last_accessed', UploadedFile.last_accessed_at)
Index('idx_uploaded_files_s3_uri', UploadedFile.s3_uri)
Index('idx_uploaded_files_content_hash', UploadedFile.content_hash)
Index('idx_uploaded_files_status', UploadedFile.status)
Index('idx_uploaded_files_is_deleted', UploadedFile.is_deleted_by_user)

# Indexes for file_versions table
Index('idx_file_versions_uploaded_file_id', FileVersion.uploaded_file_id)
Index('idx_file_versions_openai_file_id', FileVersion.openai_file_id)
Index('idx_file_versions_is_active', FileVersion.is_active)
# Partial unique index: only one active version per uploaded file
# NOTE: This needs to be created via raw SQL in the migration because
# SQLAlchemy Index doesn't directly support partial (WHERE) unique indexes.
# CREATE UNIQUE INDEX idx_file_versions_active_unique ON file_versions (uploaded_file_id) WHERE is_active = True;

# Indexes for vector_store_files table
Index('idx_vector_store_files_vector_store_id', VectorStoreFile.user_vector_store_id)
Index('idx_vector_store_files_file_version_id', VectorStoreFile.file_version_id)

# Indexes for chat_files table
Index('idx_chat_files_chat_id', ChatFile.chat_id)
Index('idx_chat_files_uploaded_file_id', ChatFile.uploaded_file_id)
Index('idx_chat_files_file_version_id', ChatFile.file_version_id)
Index('idx_chat_files_usage_type', ChatFile.usage_type)

# Index('idx_source_documents_gin', Report.source_documents, postgresql_using='gin')
# Index('idx_s3_uri_gin', Report.s3_uri, postgresql_using='gin')

# Database utility functions
# def create_database_engine(database_url: str):
#     """
#     Create and return a SQLAlchemy engine instance.
    
#     Args:
#         database_url (str): Database connection URL to an existing database
#                            Examples:
#                            - PostgreSQL: "postgresql://user:pass@localhost/dbname"
#                            - MySQL: "mysql://user:pass@localhost/dbname" 
#                            - SQLite: "sqlite:///path/to/database.db"
        
#     Returns:
#         Engine: SQLAlchemy engine instance
        
#     Raises:
#         sqlalchemy.exc.OperationalError: If database doesn't exist or connection fails
#     """
#     try:
#         logger.info(f"Attempting to connect to database")
#         # Create engine with minimal logging
#         engine = create_engine(
#             database_url,
#             echo=False,  # Disable SQL echoing
#             pool_pre_ping=True  # Enable connection health checks
#         )
#         # Test connection
#         engine.connect()
#         logger.debug("Database connection established successfully")
#         logger.info("Successfully connected to database")
#         return engine
        
#     except Exception as e:
#         logger.error(f"Error connecting to database: {str(e)}", exc_info=True)
#         logger.error("Make sure the database exists and connection details are correct")
#         raise

# def create_database_tables(engine):
#     """
#     Create all tables (users, messages, reports) in the database.
    
#     Args:
#         engine: SQLAlchemy engine instance
        
#     Raises:
#         sqlalchemy.exc.SQLAlchemyError: If table creation fails
#     """
#     try:
#         logger.info("Creating database tables")
#         # Create all tables if they don't exist
#         Base.metadata.create_all(engine)
#         logger.info("Successfully created all database tables")
        
#     except Exception as e:
#         logger.error(f"Error creating database tables: {str(e)}", exc_info=True)
#         raise

# def setup_database_tables(database_url: str):
#     """
#     Connect to existing database and create all tables.
    
#     Note: This function assumes the database already exists.
#     It only creates the tables (users, messages, reports) inside the database.
    
#     Args:
#         database_url (str): Database connection URL to an existing database
#                            Examples:
#                            - PostgreSQL: "postgresql://user:pass@localhost/dbname"
#                            - MySQL: "mysql://user:pass@localhost/dbname" 
#                            - SQLite: "sqlite:///path/to/database.db"
        
#     Returns:
#         Engine: SQLAlchemy engine instance
        
#     Raises:
#         sqlalchemy.exc.OperationalError: If database doesn't exist or connection fails
#     """
#     engine = create_database_engine(database_url)
#     create_database_tables(engine)
#     return engine

# def create_database_if_not_exists(database_url: str, database_name: str):
#     """
#     Create database if it doesn't exist (PostgreSQL/MySQL only).
    
#     Args:
#         database_url (str): Connection URL without database name
#         database_name (str): Name of database to create
        
#     Returns:
#         str: Full database URL with database name
#     """
#     logger.info(f"Checking if database '{database_name}' exists")
    
#     # Connect without specifying database
#     temp_engine = create_engine(database_url)
    
#     try:
#         with temp_engine.connect() as conn:
#             # Check if database exists and create if not
#             if 'postgresql' in database_url:
#                 result = conn.execute(text(f"SELECT 1 FROM pg_database WHERE datname='{database_name}'"))
#                 if not result.fetchone():
#                     conn.execute(text(f"CREATE DATABASE {database_name}"))
#                     logger.info(f"Created PostgreSQL database: {database_name}")
#                 else:
#                     logger.debug(f"Database '{database_name}' already exists")
#             elif 'mysql' in database_url:
#                 conn.execute(text(f"CREATE DATABASE IF NOT EXISTS {database_name}"))
#                 logger.info(f"Ensured MySQL database '{database_name}' exists")
#     except Exception as e:
#         logger.error(f"Error creating database: {str(e)}", exc_info=True)
#         raise
    
#     # Return full URL with database name
#     return f"{database_url.rstrip('/')}/{database_name}"

# def get_session(engine):
#     """
#     Create a database session.
    
#     Args:
#         engine: SQLAlchemy engine instance
        
#     Returns:
#         Session: SQLAlchemy session instance
#     """
#     logger.debug("Creating new database session")
#     Session = sessionmaker(bind=engine)
#     return Session()

# Example usage and sample data creation
# if __name__ == "__main__":
#     # Example database setup
#     # DATABASE_URL = "postgresql://postgres:Amit%401234@127.0.0.1/casper"
    
#     # Create engine and tables
    # engine = create_database_engine(DB_CONNECTION_LINK)
    # create_database_tables(engine)
    # session = get_session(engine)   
    # session.flush()
    # session.close()
    
    # Sample data creation (for testing)
    # try:
    #     # Use a single session for all operations
    #     session = get_session(engine)
        
    #     # Create a sample user with provided UUID string
    #     sample_user_id = str(uuid7())
    #     sample_user = User(
    #         user_id=sample_user_id,
    #         email="amit.doe@example.com",
    #         password="hashed_password_here",
    #         phone="9343451290",
    #         phone_country_code="+91",
    #         user_name="Amit Doe"
    #     )
    #     session.add(sample_user)
    #     session.commit()
        
    #     # Create a sample message/chat
    #     sample_chat_id = str(uuid7())
    #     sample_message = Message(
    #         chat_id=sample_chat_id,
    #         user_id=sample_user_id,
    #         chat_title="Sample Chat",
    #         chat_messages={
    #             "messages": [
    #                 {"role": "user", "content": "Hello, this is a test message!"},
    #                 {"role": "assistant", "content": "Hello! How can I help you today?"}
    #             ]
    #         }
    #     )
    #     session.add(sample_message)
    #     session.commit()
        
    #     # Create a sample report
    #     sample_report_id = str(uuid7())
    #     sample_report = Report(
    #         chat_id=sample_chat_id,
    #         report_id=sample_report_id,
    #         s3_uri={
    #             "pdf": "s3://my-bucket/reports/pdf/report-123.pdf",
    #             "html": "s3://my-bucket/reports/html/report-123.html"
    #         },
    #         source_documents=[
    #             {"title": "Sample Document", "content": "Sample content for testing"}
    #         ]
    #     )
    #     session.add(sample_report)
    #     session.commit()
        
    #     print("Sample data created successfully!")
        
    # except Exception as e:
    #     print(f"Error creating sample data: {e}")
    #     if 'session' in locals():
    #         session.rollback()
    # finally:
    #     # Ensure session is closed
    #     if 'session' in locals():
    #         session.close()
