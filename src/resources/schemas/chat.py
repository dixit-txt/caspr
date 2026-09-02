"""chat.py: Chat-related schemas"""
from pydantic import BaseModel, Field, field_validator
from typing import Any, List, Optional, Union, Dict
from datetime import datetime
from src.config.constants import CHAT_MAXIMUM_CHARACTER, REFINE_MAXIMUM_CHARACTERS

class S3Paths(BaseModel):
    pdf: Union[str, None] = None
    md: Union[str, None] = None
    html: Union[str, None] = None
    pptx: Union[str, None] = None

class CreateSessionRequest(BaseModel):
    chat_id: Optional[str] = None

class CreateTempSessionRequest(BaseModel):
    chat_id: Optional[str] = None

class ChatRequest(BaseModel):
    message: str = Field(max_length=CHAT_MAXIMUM_CHARACTER)
    chat_id: str
    session_id: str
    reference_ids: Optional[List[str]] = None  # uploaded_files.id list — sent on first message only

class TempChatRequest(BaseModel):
    message: str = Field(max_length=CHAT_MAXIMUM_CHARACTER)
    chat_id: str
    session_id: str

# class TempChatRequest(BaseModel):
#     message: str
#     chat_id: Optional[str] = None

# class ChatRequest(BaseModel):
#     message: str
#     chat_id: Optional[str] = None

class ChatResponse(BaseModel):
    success: bool
    is_report_generated: bool
    tool_call: bool
    source_documents: Union[List[str], None]
    ai_answer: Union[str, None]
    s3_paths: S3Paths
    report_created_at: Union[str, None]
    chat_title: Union[str, None]
    chat_id: Union[str, None]
    report_id: Union[str, None] = None

class PreviousChatMessagesRequest(BaseModel):
    user_id: str
    chat_id: str

class PreviousChatMessagesResponse(BaseModel):
    success: bool
    messages: Union[List[Dict], None]
    reports: Union[List[Dict], None]
    turn_in_progress: bool = False
    
class UserChatsRequest(BaseModel):
    user_id: str

class UserChatsResponse(BaseModel):
    success: bool
    chats: Union[List[Dict], None]

class ReportPresignedUrlRequest(BaseModel):
    id: str
    file_type: str

class ReportPresignedUrlResponse(BaseModel):
    success: bool
    presigned_url: Union[str, None]

class VersionDownloadResponse(BaseModel):
    success: bool
    presigned_url: Optional[str] = None
    report_id: Optional[str] = None
    version: Optional[int] = None
    file_type: Optional[str] = None
    expires_in: Optional[int] = None  # seconds

class VersionOutputItem(BaseModel):
    version: int
    file_type: str
    s3_path: str
    generated_at: Optional[str] = None

class ListVersionOutputsResponse(BaseModel):
    success: bool
    report_id: Optional[str] = None
    outputs: Optional[List[VersionOutputItem]] = None

class ReportRequest(BaseModel):
    user_id: str
    report_id: str

class ReportResponse(BaseModel):
    success: bool = True
    report_url: str

class UserReportsResponse(BaseModel):
    success: bool = True
    reports: List[dict] 

class UserLogsRequest(BaseModel):
    type: str
    logs_data: dict
    status: bool

class GenerateReportRequest(BaseModel):
    report_id: str
    report_version_id: Optional[str] = None  # Specific version to generate for (from dropdown)
    output_type: Optional[str] = "pdf"  # "pdf" | "html" | "md"

class GenerateReportResponse(BaseModel):
    success: bool
    message: str
    report_id: str
    report_version_id: Optional[str] = None  # Version ID that was generated
    version: Optional[int] = None  # Version number that was generated
    output_type: Optional[str] = None  # "pdf" | "html" | "md"
    s3_uri: Optional[Dict] = None  # Full s3_uri dict for this version
    modified: Optional[bool] = None  # True if new report generated, False if no changes
    already_exists: Optional[bool] = None  # False if newly generated, True if output already existed for this version

class GeneratePresentationRequest(BaseModel):
    report_id: str
    report_version_id: Optional[str] = None
    generation_mode: str = "template"  # "template" (Accenture master) or "scratch" (pptxgenjs)

class GeneratePresentationResponse(BaseModel):
    success: bool
    message: str
    report_version_id: Optional[str] = None  # Version ID that was generated
    version: Optional[int] = None  # Version number that was generated
    pptx_s3_uri: Optional[str] = None
    pptx_generation_time: Optional[str] = None
    report_id: Optional[str] = None
    modified: Optional[bool] = None  # True if new PPTX generated, False if no changes
    already_exists: Optional[bool] = None  # False if newly generated, True if output already existed for this version

class DownloadPresentationResponse(BaseModel):
    success: bool
    download_url: str

class GenerateInfographicRequest(BaseModel):
    report_id: str
    report_version_id: Optional[str] = None  # Specific version to generate for

class GenerateInfographicResponse(BaseModel):
    success: bool
    message: str
    report_version_id: Optional[str] = None  # Version ID that was generated
    version: Optional[int] = None  # Version number that was generated
    info_pdf_s3_uri: Optional[str] = None
    info_pdf_generation_time: Optional[str] = None
    report_id: Optional[str] = None
    modified: Optional[bool] = None  # True if new infographic generated, False if no changes
    already_exists: Optional[bool] = None  # False if newly generated, True if output already existed for this version

class RefineOrDeleteRequest(BaseModel):
    report_id: str
    card: Dict[str, Any]

    @field_validator("card")
    @classmethod
    def validate_user_instruction_length(cls, card: Dict[str, Any]) -> Dict[str, Any]:
        instruction = card.get("user_instruction") or ""
        if len(instruction) > REFINE_MAXIMUM_CHARACTERS:
            raise ValueError(f"user_instruction must be at most {REFINE_MAXIMUM_CHARACTERS} characters")
        subsection = card.get("subsection") or {}
        sub_instruction = subsection.get("user_instruction") or ""
        if len(sub_instruction) > REFINE_MAXIMUM_CHARACTERS:
            raise ValueError(f"subsection.user_instruction must be at most {REFINE_MAXIMUM_CHARACTERS} characters")
        return card

class RefineOrDeleteResponse(BaseModel):
    success: bool
    message: str
    refine_card: Optional[Dict[str, Any]] = None

class RefineVisualizationRequest(BaseModel):
    report_id: str
    card: Dict[str, Any]

    @field_validator("card")
    @classmethod
    def validate_user_instruction_length(cls, card: Dict[str, Any]) -> Dict[str, Any]:
        instruction = card.get("user_instruction") or ""
        if len(instruction) > REFINE_MAXIMUM_CHARACTERS:
            raise ValueError(f"user_instruction must be at most {REFINE_MAXIMUM_CHARACTERS} characters")
        return card

class RefineVisualizationResponse(BaseModel):
    success: bool
    message: str
    updated_card: Optional[Dict[str, Any]] = None
    
class DeleteChatRequest(BaseModel):
    chat_id: str

class DeleteChatResponse(BaseModel):
    success: bool
    message: Optional[str] = None
    
class RenameChatRequest(BaseModel):
    chat_id: str
    new_title: str

class RenameChatResponse(BaseModel):
    success: bool
    message: Optional[str] = None

class ModifiedCard(BaseModel):
    """Schema for a modified card in version regeneration"""
    id: str  # Business card_id
    is_modified: bool = True
    # Include all card fields for modified cards
    section: List[Dict[str, Any]]
    sub_sections: Optional[List[Dict[str, Any]]] = None
    citations: Optional[Dict[str, Any]] = None
    summary: Optional[str] = None
    type: str
    sequence: int

class UnchangedCard(BaseModel):
    """Schema for an unchanged card (to be reused from previous version)"""
    id: str  # Business card_id
    is_modified: bool = False
    sequence: int
    existing_card_pk_id: str  # Primary key ID of the card to reuse

class RegenerateReportRequest(BaseModel):
    """
    Request schema for regenerating a report with modified cards (creates new version).
    
    Backend automatically detects which cards were modified by comparing:
    - card.created_at > report_version.generated_at → Modified
    - card.created_at <= report_version.generated_at → Unchanged
    
    Frontend only needs to send the report_id!
    """
    report_id: str

class RegenerateReportResponse(BaseModel):
    success: bool
    message: str
    version: Optional[int] = None
    version_id: Optional[str] = None
    report_id: Optional[str] = None
    modified_count: Optional[int] = None
    unchanged_count: Optional[int] = None

class RevertCardRequest(BaseModel):
    """
    Request to revert a card to its immediate previous version (undo last refinement).
    
    Only reverts to immediate previous version (v3 → v2, not v3 → v1).
    Current version is deleted and previous version is reactivated.
    """
    report_id: str
    card_id: str  # Business card_id (not primary key)

class RevertCardResponse(BaseModel):
    success: bool
    message: Optional[str] = None
    reverted_to_version: Optional[int] = None
    card_id: Optional[str] = None
    primary_card_id: Optional[str] = None  # New active card's primary key
    deleted_versions: Optional[List[int]] = None
    reverted_card: Optional[Dict[str, Any]] = None  # Full card structure (same format as refine_card)

class RegenerateESRequest(BaseModel):
    report_id: str

class RegenerateESResponse(BaseModel):
    success: bool
    message: str
    new_es_version: Optional[int] = None
    updated_summary: Optional[str] = None

class TempChatMessagesResponse(BaseModel):
    success: bool
    messages: Union[List[Dict], None]


# Report Version History Schemas
class GeneratedOutputStatus(BaseModel):
    """Status of a generated output type for a version"""
    generated: bool
    s3_path: Optional[str] = None  # Relative path from s3_base_path (e.g., "v1/report.pdf")
    generated_at: Optional[str] = None


class VersionInfo(BaseModel):
    """Information about a single report version"""
    report_version_id: str
    version: int
    is_current: bool
    is_modified: bool
    label: str  # Human-readable label (e.g., "3rd Nov 2025, 2:30 PM")
    created_at: Optional[str] = None
    generated_outputs: Dict[str, GeneratedOutputStatus]


class ReportVersionHistoryResponse(BaseModel):
    """Response for the version history dropdown API"""
    success: bool
    report_id: str
    s3_base_path: str  # Base S3 path for the report (e.g., "s3://bucket/reports/rpt-abc-123")
    versions: List[VersionInfo]


class ReportInfoResponse(BaseModel):
    """Response for getting report info by report_id and report_version_id"""
    success: bool
    reports: Union[List[Dict], None]

class BatchPresignedUrlRequest(BaseModel):
    s3_uris: List[str]

class BatchPresignedUrlResponse(BaseModel):
    success: bool
    urls: Dict[str, Optional[str]]

class ChatReferenceFileItem(BaseModel):
    reference_id: str
    original_filename: Optional[str] = None
    file_size: Optional[int] = None
    file_type: Optional[str] = None
    status: Optional[str] = None
    created_at: Optional[str] = None


class ChatReferenceFilesResponse(BaseModel):
    """Response for GET /upload/chat-files/{chat_id}"""
    success: bool
    source: str
    files: List[ChatReferenceFileItem]
