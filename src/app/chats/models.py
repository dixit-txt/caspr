"""ORM models for the chats bounded context.

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


# Standalone indexes, moved with the models they index.
Index('idx_ask_caspr_chats_report_id', AskCasprChat.report_id)
Index('idx_ask_caspr_chats_section_id', AskCasprChat.section_id)
Index('idx_ask_caspr_chats_subsection_id', AskCasprChat.subsection_id)
Index('idx_ask_caspr_chats_created_at', AskCasprChat.created_at)
Index('idx_ask_caspr_chats_latest_lookup', AskCasprChat.report_id, AskCasprChat.section_id, AskCasprChat.subsection_id, AskCasprChat.created_at)
Index('idx_messages_user_id', Message.user_id)
Index('idx_messages_is_deleted', Message.is_deleted)
