"""ORM models for the internal bounded context.

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
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
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

    __tablename__ = "user_vector_stores"

    id = Column(
        CHAR(36), primary_key=True, default=lambda: str(uuid7()), unique=True, nullable=False
    )
    user_id = Column(
        CHAR(36),
        ForeignKey("users.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
        comment="Reference to user (one vector store per user)",
    )
    vector_store_id = Column(
        String(200),
        nullable=True,
        comment="OpenAI vector store ID (null if not created yet or deleted)",
    )
    last_accessed_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        comment="Last time this vector store was accessed/used",
    )
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    # Relationships
    user = relationship("User", backref="user_vector_store", uselist=False)
    vector_store_files = relationship(
        "VectorStoreFile", back_populates="user_vector_store", cascade="all, delete-orphan"
    )

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

    __tablename__ = "uploaded_files"

    id = Column(
        CHAR(36), primary_key=True, default=lambda: str(uuid7()), unique=True, nullable=False
    )
    user_id = Column(
        CHAR(36),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        comment="Reference to user who uploaded the file",
    )
    s3_uri = Column(
        String(500), nullable=False, comment="S3 path of the uploaded file (for re-upload)"
    )
    original_filename = Column(
        String(255), nullable=True, comment="Original filename when uploaded"
    )
    file_size = Column(Integer, nullable=True, comment="File size in bytes")
    file_type = Column(
        String(50), nullable=True, comment="File type/extension (pdf, docx, txt, etc.)"
    )
    content_hash = Column(String(64), nullable=True, comment="SHA-256 hash for duplicate detection")
    upload_context = Column(
        Enum(FileUploadContext, name="file_upload_context_enum", native_enum=True),
        nullable=False,
        default=FileUploadContext.IN_CHAT,
        comment="Context: IN_CHAT or STANDALONE",
    )
    status = Column(
        Enum(UploadedFileStatus, name="uploaded_file_status_enum", native_enum=True),
        nullable=False,
        default=UploadedFileStatus.PENDING_OPENAI_UPLOAD,
        comment="Upload pipeline status",
    )
    is_deleted_by_user = Column(
        Boolean, default=False, nullable=False, comment="Soft-delete flag set by user"
    )
    deleted_by_user_at = Column(
        DateTime(timezone=True), nullable=True, comment="When user deleted the file"
    )
    last_accessed_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        comment="Last time this file was accessed/used",
    )
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
    # grep_agent_2 parse-time columns (populated after PDF parsing at upload)
    total_pages = Column(
        Integer, nullable=True, comment="Total pages in the document (set after parsing)"
    )
    doc_map = Column(
        Text, nullable=True, comment="TOC-like structure string for worker system prompt"
    )
    sections = Column(JSONB, nullable=True, comment="Ordered list of detected heading strings")
    llm_summary = Column(
        Text, nullable=True, comment="60-100 word LLM summary used by coordinator for routing"
    )
    token_index = Column(
        JSONB, nullable=True, comment="Inverted index: {token: {chunk_id_str: count}}"
    )
    bigram_index = Column(
        JSONB, nullable=True, comment="Bigram index: {bigram: {chunk_id_str: count}}"
    )
    is_parsed = Column(
        Boolean,
        nullable=False,
        default=False,
        comment="True once chunks and indexes are stored in DB",
    )
    parsed_at = Column(DateTime(timezone=True), nullable=True, comment="When parsing completed")

    # Relationships
    user = relationship("User", backref="uploaded_files")
    file_versions = relationship(
        "FileVersion", back_populates="uploaded_file", cascade="all, delete-orphan"
    )
    chunks = relationship(
        "UploadedFileChunk", back_populates="uploaded_file", cascade="all, delete-orphan"
    )
    chat_files = relationship(
        "ChatFile", back_populates="uploaded_file", cascade="all, delete-orphan"
    )

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

    __tablename__ = "uploaded_file_chunks"

    id = Column(
        CHAR(36), primary_key=True, default=lambda: str(uuid7()), unique=True, nullable=False
    )
    uploaded_file_id = Column(
        CHAR(36),
        ForeignKey("uploaded_files.id", ondelete="CASCADE"),
        nullable=False,
        comment="Reference to uploaded file",
    )
    chunk_id = Column(
        Integer, nullable=False, comment="0-based sequential chunk index within this document"
    )
    text = Column(Text, nullable=False, comment="~800 char chunk text extracted from the PDF")
    page_num = Column(Integer, nullable=False, comment="1-based page number this chunk came from")
    section = Column(
        String(500), nullable=True, comment="Most recent detected heading before this chunk"
    )
    start_offset = Column(
        Integer, nullable=False, comment="Character offset from start of full document text"
    )
    end_offset = Column(Integer, nullable=False, comment="Character offset end")
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    uploaded_file = relationship("UploadedFile", back_populates="chunks")

    __table_args__ = (UniqueConstraint("uploaded_file_id", "chunk_id", name="uq_ufc_file_chunk"),)

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

    __tablename__ = "file_versions"

    id = Column(
        CHAR(36), primary_key=True, default=lambda: str(uuid7()), unique=True, nullable=False
    )
    uploaded_file_id = Column(
        CHAR(36),
        ForeignKey("uploaded_files.id", ondelete="CASCADE"),
        nullable=False,
        comment="Reference to the uploaded file",
    )
    openai_file_id = Column(String(200), nullable=False, comment="OpenAI file ID for this version")
    is_active = Column(
        Boolean,
        default=True,
        nullable=False,
        comment="Whether this file ID is currently active/valid",
    )
    inactive_at = Column(
        DateTime(timezone=True), nullable=True, comment="When this file version became inactive"
    )
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    last_verified_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=True,
        comment="Last time we verified this file ID exists in OpenAI",
    )

    # Relationships
    uploaded_file = relationship("UploadedFile", back_populates="file_versions")
    vector_store_files = relationship(
        "VectorStoreFile", back_populates="file_version", cascade="all, delete-orphan"
    )
    chat_files = relationship("ChatFile", back_populates="file_version")

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

    __tablename__ = "vector_store_files"

    id = Column(
        CHAR(36), primary_key=True, default=lambda: str(uuid7()), unique=True, nullable=False
    )
    user_vector_store_id = Column(
        CHAR(36),
        ForeignKey("user_vector_stores.id", ondelete="CASCADE"),
        nullable=False,
        comment="Reference to user's vector store",
    )
    file_version_id = Column(
        CHAR(36),
        ForeignKey("file_versions.id", ondelete="CASCADE"),
        nullable=False,
        comment="Reference to file version (OpenAI file ID)",
    )
    created_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        comment="When file was attached to vector store",
    )
    is_deleted = Column(
        Boolean,
        default=False,
        nullable=False,
        comment="Soft-delete flag (set when VS is recreated or file re-uploaded)",
    )
    deleted_at = Column(
        DateTime(timezone=True), nullable=True, comment="When this junction record was soft-deleted"
    )

    # Relationships
    user_vector_store = relationship("UserVectorStore", back_populates="vector_store_files")
    file_version = relationship("FileVersion", back_populates="vector_store_files")

    __table_args__ = (
        UniqueConstraint(
            "user_vector_store_id", "file_version_id", name="uq_vector_store_file_version"
        ),
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

    __tablename__ = "chat_files"

    id = Column(
        CHAR(36), primary_key=True, default=lambda: str(uuid7()), unique=True, nullable=False
    )
    chat_id = Column(
        CHAR(36),
        ForeignKey("messages.id", ondelete="CASCADE"),
        nullable=False,
        comment="Reference to chat/message",
    )
    uploaded_file_id = Column(
        CHAR(36),
        ForeignKey("uploaded_files.id", ondelete="CASCADE"),
        nullable=False,
        comment="Reference to uploaded file",
    )
    file_version_id = Column(
        CHAR(36),
        ForeignKey("file_versions.id", ondelete="SET NULL"),
        nullable=True,
        comment="Reference to specific file version used",
    )
    usage_type = Column(
        Enum(FileUsageType, name="file_usage_type_enum", native_enum=True),
        nullable=False,
        comment="NEW_UPLOAD or REFERENCE (reused from previous upload)",
    )
    upload_context = Column(
        Enum(
            FileUploadContext,
            name="file_upload_context_enum",
            native_enum=True,
            create_constraint=False,
        ),
        nullable=True,
        comment="Historical context: IN_CHAT or STANDALONE",
    )
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    # Relationships
    chat = relationship("Message", backref="chat_files")
    uploaded_file = relationship("UploadedFile", back_populates="chat_files")
    file_version = relationship("FileVersion", back_populates="chat_files")

    def __repr__(self):
        return f"<ChatFile(id='{self.id}', chat_id='{self.chat_id}', uploaded_file_id='{self.uploaded_file_id}', usage_type='{self.usage_type}')>"


# Standalone indexes, moved with the models they index.
Index("idx_uploaded_files_is_parsed", UploadedFile.is_parsed)
Index("idx_ufc_uploaded_file_id", UploadedFileChunk.uploaded_file_id)
Index("idx_ufc_file_page", UploadedFileChunk.uploaded_file_id, UploadedFileChunk.page_num)
Index("idx_chat_files_chat_id", ChatFile.chat_id)
Index("idx_chat_files_uploaded_file_id", ChatFile.uploaded_file_id)
Index("idx_user_vector_stores_user_id", UserVectorStore.user_id)
Index("idx_user_vector_stores_last_accessed", UserVectorStore.last_accessed_at)
Index("idx_uploaded_files_user_id", UploadedFile.user_id)
Index("idx_uploaded_files_last_accessed", UploadedFile.last_accessed_at)
Index("idx_uploaded_files_s3_uri", UploadedFile.s3_uri)
Index("idx_uploaded_files_content_hash", UploadedFile.content_hash)
Index("idx_uploaded_files_status", UploadedFile.status)
Index("idx_uploaded_files_is_deleted", UploadedFile.is_deleted_by_user)
Index("idx_file_versions_uploaded_file_id", FileVersion.uploaded_file_id)
Index("idx_file_versions_openai_file_id", FileVersion.openai_file_id)
Index("idx_file_versions_is_active", FileVersion.is_active)
Index("idx_vector_store_files_vector_store_id", VectorStoreFile.user_vector_store_id)
Index("idx_vector_store_files_file_version_id", VectorStoreFile.file_version_id)
Index("idx_chat_files_chat_id", ChatFile.chat_id)
Index("idx_chat_files_uploaded_file_id", ChatFile.uploaded_file_id)
Index("idx_chat_files_file_version_id", ChatFile.file_version_id)
Index("idx_chat_files_usage_type", ChatFile.usage_type)
