"""Request and response schemas for the chats bounded context.

Moved verbatim from ``src/resources/schemas/chat.py`` during the R-STRUCT-1
migration. Field definitions and validators are unchanged.
"""

from pydantic import BaseModel, Field

from app.core.constants import CHAT_MAXIMUM_CHARACTER
from app.core.errors import ErrorResponse  # noqa: F401
from app.reports.schemas import S3Paths


class CreateSessionRequest(BaseModel):
    chat_id: str | None = None


class CreateTempSessionRequest(BaseModel):
    chat_id: str | None = None


class ChatRequest(BaseModel):
    message: str = Field(max_length=CHAT_MAXIMUM_CHARACTER)
    chat_id: str
    session_id: str
    reference_ids: list[str] | None = None  # uploaded_files.id list — sent on first message only


class ReportConfigRequest(BaseModel):
    """The Confirm submission that resumes a graph paused for configuration.

    ``thread_id`` echoes the id the pause event carried, so a Confirm can only
    resume the turn it was issued for. Every other field is stored on the
    report configuration; only ``report_tier`` and ``style`` act in v1.
    """

    chat_id: str
    session_id: str
    thread_id: str
    report_tier: str
    style: str = "investor"
    output_formats: list[str] = Field(default_factory=list)
    language: str = "English"
    data_sources: list[str] = Field(default_factory=list)


class TempChatRequest(BaseModel):
    message: str = Field(max_length=CHAT_MAXIMUM_CHARACTER)
    chat_id: str
    session_id: str


class ChatResponse(BaseModel):
    success: bool
    is_report_generated: bool
    tool_call: bool
    source_documents: list[str] | None
    ai_answer: str | None
    s3_paths: S3Paths
    report_created_at: str | None
    chat_title: str | None
    chat_id: str | None
    report_id: str | None = None


class PreviousChatMessagesRequest(BaseModel):
    user_id: str
    chat_id: str


class PreviousChatMessagesResponse(BaseModel):
    success: bool
    messages: list[dict] | None
    reports: list[dict] | None
    turn_in_progress: bool = False


class UserChatsRequest(BaseModel):
    user_id: str


class UserChatsResponse(BaseModel):
    success: bool
    chats: list[dict] | None


class DeleteChatRequest(BaseModel):
    chat_id: str


class DeleteChatResponse(BaseModel):
    success: bool
    message: str | None = None


class RenameChatRequest(BaseModel):
    chat_id: str
    new_title: str


class RenameChatResponse(BaseModel):
    success: bool
    message: str | None = None


class TempChatMessagesResponse(BaseModel):
    success: bool
    messages: list[dict] | None


class ChatReferenceFileItem(BaseModel):
    reference_id: str
    original_filename: str | None = None
    file_size: int | None = None
    file_type: str | None = None
    status: str | None = None
    created_at: str | None = None


class ChatReferenceFilesResponse(BaseModel):
    """Response for GET /upload/chat-files/{chat_id}"""

    success: bool
    source: str
    files: list[ChatReferenceFileItem]
