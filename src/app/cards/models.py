"""ORM models for the cards bounded context.

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


# Standalone indexes, moved with the models they index.
Index('idx_refinement_history_report_id', RefinementHistory.report_id)
Index('idx_tables_report_id', Table.report_id)
Index('idx_cards_id', Card.id)
Index('idx_cards_card_id', Card.card_id)
Index('idx_cards_report_id', Card.report_id)
Index('idx_cards_sequence', Card.sequence)
Index('idx_tables_table_id', Table.table_id)
Index('idx_card_versions_parent_card_id', CardVersion.parent_card_id)
Index('idx_card_versions_section_id', CardVersion.section_id)
Index('idx_card_versions_subsection_id', CardVersion.subsection_id)
Index('idx_card_versions_refinement_type', CardVersion.refinement_type)
