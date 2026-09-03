"""Request and response schemas for the reports bounded context.

Moved verbatim from ``src/resources/schemas/chat.py`` during the R-STRUCT-1
migration. Field definitions and validators are unchanged.
"""

from typing import Any

from pydantic import BaseModel

from app.core.errors import ErrorResponse  # noqa: F401


class S3Paths(BaseModel):
    pdf: str | None = None
    md: str | None = None
    html: str | None = None
    pptx: str | None = None


class ReportPresignedUrlRequest(BaseModel):
    id: str
    file_type: str


class ReportPresignedUrlResponse(BaseModel):
    success: bool
    presigned_url: str | None


class VersionDownloadResponse(BaseModel):
    success: bool
    presigned_url: str | None = None
    report_id: str | None = None
    version: int | None = None
    file_type: str | None = None
    expires_in: int | None = None  # seconds


class VersionOutputItem(BaseModel):
    version: int
    file_type: str
    s3_path: str
    generated_at: str | None = None


class ListVersionOutputsResponse(BaseModel):
    success: bool
    report_id: str | None = None
    outputs: list[VersionOutputItem] | None = None


class ReportRequest(BaseModel):
    user_id: str
    report_id: str


class ReportResponse(BaseModel):
    success: bool = True
    report_url: str


class UserReportsResponse(BaseModel):
    success: bool = True
    reports: list[dict]


class GenerateReportRequest(BaseModel):
    report_id: str
    report_version_id: str | None = None  # Specific version to generate for (from dropdown)
    output_type: str | None = "pdf"  # "pdf" | "html" | "md"


class GenerateReportResponse(BaseModel):
    success: bool
    message: str
    report_id: str
    report_version_id: str | None = None  # Version ID that was generated
    version: int | None = None  # Version number that was generated
    output_type: str | None = None  # "pdf" | "html" | "md"
    s3_uri: dict | None = None  # Full s3_uri dict for this version
    modified: bool | None = None  # True if new report generated, False if no changes
    already_exists: bool | None = (
        None  # False if newly generated, True if output already existed for this version
    )


class ModifiedCard(BaseModel):
    """Schema for a modified card in version regeneration"""

    id: str  # Business card_id
    is_modified: bool = True
    # Include all card fields for modified cards
    section: list[dict[str, Any]]
    sub_sections: list[dict[str, Any]] | None = None
    citations: dict[str, Any] | None = None
    summary: str | None = None
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
    version: int | None = None
    version_id: str | None = None
    report_id: str | None = None
    modified_count: int | None = None
    unchanged_count: int | None = None


class GeneratedOutputStatus(BaseModel):
    """Status of a generated output type for a version"""

    generated: bool
    s3_path: str | None = None  # Relative path from s3_base_path (e.g., "v1/report.pdf")
    generated_at: str | None = None


class VersionInfo(BaseModel):
    """Information about a single report version"""

    report_version_id: str
    version: int
    is_current: bool
    is_modified: bool
    label: str  # Human-readable label (e.g., "3rd Nov 2025, 2:30 PM")
    created_at: str | None = None
    generated_outputs: dict[str, GeneratedOutputStatus]


class ReportVersionHistoryResponse(BaseModel):
    """Response for the version history dropdown API"""

    success: bool
    report_id: str
    s3_base_path: str  # Base S3 path for the report (e.g., "s3://bucket/reports/rpt-abc-123")
    versions: list[VersionInfo]


class ReportInfoResponse(BaseModel):
    """Response for getting report info by report_id and report_version_id"""

    success: bool
    reports: list[dict] | None


class BatchPresignedUrlRequest(BaseModel):
    s3_uris: list[str]


class BatchPresignedUrlResponse(BaseModel):
    success: bool
    urls: dict[str, str | None]
