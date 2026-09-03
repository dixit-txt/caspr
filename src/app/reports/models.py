"""ORM models for the reports bounded context.

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
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
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

    __tablename__ = "reports"

    id = Column(
        CHAR(36), primary_key=True, default=lambda: str(uuid7()), unique=True, nullable=False
    )
    chat_id = Column(
        CHAR(36),
        ForeignKey("messages.id", ondelete="CASCADE"),
        nullable=False,
        comment="Reference to the chat/message",
    )
    created_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        comment="Timestamp when report was first created",
    )
    title = Column(String(255), nullable=True, comment="Title of the report")
    layout = Column(JSONB, nullable=True, comment="Layout configuration for the report")
    length = Column(
        String(50),
        nullable=True,
        comment="Length description of the report (comprehensive/overview)",
    )
    summary = Column(Text, nullable=True, comment="Summary of the report")
    citations = Column(JSONB, nullable=True, comment="Citations or references used in the report")
    poster_image_url = Column(
        String(500), nullable=True, comment="CDN URL for the poster/thumbnail image"
    )
    s3_uri = Column(
        JSONB,
        nullable=False,
        server_default='{"md": null, "pdf": null, "html": null, "pptx": null, "info_pdf": null}',
        comment="S3 URIs of current version (denormalized for quick access)",
    )
    generated_at = Column(
        DateTime(timezone=True),
        nullable=True,
        comment="When current version was generated (denormalized for quick access)",
    )
    current_version = Column(
        Integer, nullable=False, default=1, comment="Current active version number"
    )
    status = Column(
        String(50),
        nullable=False,
        default=ReportStatus.DRAFT.value,
        comment="Overall status of the report",
    )
    last_activity_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        comment="Last activity timestamp",
    )
    file_id = Column(String(200), nullable=True, comment="OpenAI file ID for the report")
    initial_markdown = Column(
        String(500), nullable=True, comment="S3 path of the initial markdown file for Ask Caspr"
    )
    domain_name = Column(
        String(50),
        nullable=True,
        comment="Report domain: default, primary_research, due_diligence, industry_benchmarking, market_insight, rfp, business_plan",
    )
    report_type = Column(
        String(10), nullable=True, default="study", comment="Report generation type: study or brief"
    )

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

    __tablename__ = "report_versions"

    id = Column(
        CHAR(36), primary_key=True, default=lambda: str(uuid7()), unique=True, nullable=False
    )
    report_id = Column(
        CHAR(36),
        ForeignKey("reports.id", ondelete="CASCADE"),
        nullable=False,
        comment="Reference to the base report",
    )
    version = Column(Integer, nullable=False, comment="Version number")
    s3_uri = Column(JSONB, nullable=True, comment="S3 URIs for this version {file_type: s3_path}")
    poster_image_url = Column(
        String(500), nullable=True, comment="S3 URL for the poster/thumbnail image"
    )
    generated_at = Column(
        DateTime(timezone=True), nullable=True, comment="When this version was generated"
    )
    is_active = Column(
        Boolean, nullable=False, default=True, comment="Whether this is the active version"
    )
    status = Column(
        String(50),
        nullable=False,
        default=ReportStatus.DRAFT.value,
        comment="Status of this version",
    )
    last_activity_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        comment="Last activity timestamp",
    )
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    # Relationships
    report = relationship("Report", back_populates="versions")
    version_cards = relationship(
        "ReportVersionCard", back_populates="report_version", cascade="all, delete-orphan"
    )

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

    __tablename__ = "report_version_cards"

    id = Column(
        CHAR(36), primary_key=True, default=lambda: str(uuid7()), unique=True, nullable=False
    )
    report_version_id = Column(
        CHAR(36),
        ForeignKey("report_versions.id", ondelete="CASCADE"),
        nullable=False,
        comment="Reference to report version",
    )
    parent_card_id = Column(
        CHAR(36),
        ForeignKey("cards.id", ondelete="CASCADE"),
        nullable=False,
        comment="Reference to card table primary key (cards.id)",
    )
    sequence = Column(Integer, nullable=False, comment="Position in this version")
    is_modified = Column(
        Boolean, nullable=False, default=False, comment="Whether card was modified in this version"
    )
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    # Relationships
    report_version = relationship("ReportVersion", back_populates="version_cards")
    card = relationship("Card", foreign_keys=[parent_card_id])

    def __repr__(self):
        return f"<ReportVersionCard(report_version_id='{self.report_version_id}', parent_card_id='{self.parent_card_id}', sequence={self.sequence}, is_modified={self.is_modified})>"


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

    __tablename__ = "publish"

    id = Column(
        CHAR(36), primary_key=True, default=lambda: str(uuid7()), unique=True, nullable=False
    )
    # card_id = Column(CHAR(36), ForeignKey('cards.id', ondelete='CASCADE'), nullable=False)
    report_id = Column(CHAR(36), ForeignKey("reports.id", ondelete="CASCADE"), nullable=False)
    faq = Column(JSONB, nullable=True, comment="Frequently asked questions related to the report")
    insights = Column(JSONB, nullable=True, comment="Key insights from the report")
    industries_jobs = Column(
        Text, nullable=True, comment="Industries or job sectors the report is relevant to"
    )
    geographic_areas = Column(Text, nullable=True, comment="Geographic areas covered in the report")
    special_emphasis = Column(
        Text, nullable=True, comment="Special areas of emphasis in the report"
    )
    audience = Column(Text, nullable=True, comment="Target audience for the report")
    purpose = Column(Text, nullable=True, comment="Purpose of the report")
    overview = Column(Text, nullable=True, comment="Overview or summary of the report")
    media_details = Column(
        JSONB,
        nullable=True,
        comment="Details about media elements in the report (poster_id, heygen_video_id, heygen_folder_id)",
    )
    page_count = Column(Integer, nullable=True, comment="Number of pages in the report")
    source_count = Column(Integer, nullable=True, comment="Number of sources cited in the report")
    table_count = Column(Integer, nullable=True, comment="Number of tables in the report")
    viz_count = Column(Integer, nullable=True, comment="Number of visualizations in the report")
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    # Relationships
    report = relationship("Report", back_populates="publish")

    def __repr__(self):
        return f"<Publish(id='{self.id}', report_id='{self.report_id}')>"


# Standalone indexes, moved with the models they index.
Index("idx_reports_domain_name", Report.domain_name)
Index("idx_reports_chat_id", Report.chat_id)
Index("idx_reports_current_version", Report.current_version)
Index("idx_reports_status", Report.status)
Index("idx_report_versions_report_id", ReportVersion.report_id)
Index("idx_report_versions_version", ReportVersion.version)
Index("idx_report_versions_is_active", ReportVersion.is_active)
Index("idx_report_versions_status", ReportVersion.status)
Index("idx_report_version_cards_version_id", ReportVersionCard.report_version_id)
Index("idx_report_version_cards_parent_card_id", ReportVersionCard.parent_card_id)
Index("idx_report_version_cards_sequence", ReportVersionCard.sequence)
Index("idx_publish_report_id", Publish.report_id)
