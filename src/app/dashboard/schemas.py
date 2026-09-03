"""Request and response schemas for the dashboard bounded context.

Moved verbatim from ``src/resources/schemas/chat.py`` during the R-STRUCT-1
migration. Field definitions and validators are unchanged.
"""

from typing import Any

from pydantic import BaseModel, Field

from app.core.errors import ErrorResponse  # noqa: F401


class UserLogsRequest(BaseModel):
    type: str
    logs_data: dict
    status: bool


# Merged from app.dashboard.schemas.py
"""Dashboard Schema Models"""

from pydantic import BaseModel


class ChatData(BaseModel):
    """Schema for individual chat data in dashboard - matches report format"""

    chat_id: str = Field(..., description="Unique chat identifier")
    chat_title: str = Field(..., description="Title of the chat/report")
    chat_image: str | None = Field(None, description="URL to poster/thumbnail image")
    chat_status: str = Field(
        ...,
        description="Status: draft, analysis-in-progress, analysis-completed, generating-output, output-generated, redo-analysis, error-generation-report",
    )
    updated_at: str = Field(..., description="ISO format timestamp of last update")
    created_at: str = Field(..., description="ISO format timestamp of creation")
    session_id: str | None = Field(None, description="Session identifier (if applicable)")


class DashboardChatsResponse(BaseModel):
    """Response model for dashboard chats endpoint - now same as reports"""

    success: bool = Field(..., description="Whether the request was successful")
    chats: list[ChatData] = Field(
        default_factory=list, description="List of chats with their status"
    )
    error: str | None = Field(None, description="Error message if success is False")


class ReportData(BaseModel):
    """Schema for individual report data in dashboard"""

    report_id: str = Field(..., description="Unique report identifier")
    report_title: str = Field(..., description="Title of the report")
    report_image: str | None = Field(None, description="URL to poster/thumbnail image")
    report_created_at: str = Field(..., description="ISO format timestamp of creation")
    report_updated_at: str = Field(..., description="ISO format timestamp of last update")
    report_status: str = Field(
        ..., description="Status: in_progress, failed, report_generated, presentation_generated"
    )
    report_type: str | None = Field(None, description="Report type: PDF, PPTX, HTML")
    report_progress_description: str = Field(..., description="Human-readable progress description")


class DashboardReportsResponse(BaseModel):
    """Response model for dashboard reports endpoint"""

    success: bool = Field(..., description="Whether the request was successful")
    reports: list[ReportData] = Field(
        default_factory=list, description="List of reports with their status"
    )
    error: str | None = Field(None, description="Error message if success is False")


class DashboardStatsResponse(BaseModel):
    """Response schema for dashboard statistics"""

    total_draft: int = Field(
        ..., description="Number of draft chats (chats in messages table but not in reports table)"
    )
    analysis_completed: int = Field(..., description="Number of reports with analysis completed")
    output_generated: int = Field(..., description="Number of reports with output generated")
    updated: int = Field(
        default=0,
        description="Number of updated reports (TODO: implement when update functionality is enabled)",
    )


class GeneralFact(BaseModel):
    """Individual general fact item"""

    title: str = Field(..., description="Title of the fact")
    subtitle: str = Field(..., description="Subtitle/description of the fact")


class CasprInfo(BaseModel):
    """Caspr information item"""

    live_feeds_data: int = Field(
        ..., description="Count of live feeds data (TODO: implement real count)"
    )


class DashboardInfoResponse(BaseModel):
    """Response schema for dashboard information"""

    general_facts: list[GeneralFact] = Field(..., description="List of general facts about Caspr")
    caspr_info: list[CasprInfo] = Field(
        ..., description="Caspr system information (TODO: implement real data)"
    )


# ---------------------------------------------------------------------------
# Report domains
# ---------------------------------------------------------------------------


class ReportDomainItem(BaseModel):
    """Summary of one report domain for the current user."""

    domain_name: str = Field(..., description="Internal domain slug, e.g. 'due_diligence'")
    category_name: str = Field(..., description="Human-readable label, e.g. 'Due Diligence'")
    item_count: int = Field(..., description="Number of non-draft reports in this domain")


class ReportDomainsResponse(BaseModel):
    """Response for GET /report-domains."""

    success: bool = True
    domains: list[ReportDomainItem] = Field(
        default_factory=list,
        description="Domains that have at least one non-draft report, sorted by item count desc",
    )


class DomainReportItem(BaseModel):
    """Lightweight report record returned by the domain-reports list endpoint."""

    report_id: str = Field(..., description="Unique report identifier")
    chat_id: str = Field(..., description="Associated chat/message ID")
    title: str | None = Field(None, description="Report title")
    status: str = Field(..., description="Report status")
    domain_name: str = Field(..., description="Domain slug")
    poster_image_url: str | None = Field(None, description="Poster/thumbnail CDN URL")
    current_version: int = Field(..., description="Current active version number")
    created_at: str | None = Field(None, description="ISO 8601 creation timestamp")
    last_activity_at: str | None = Field(None, description="ISO 8601 last-activity timestamp")
    generated_at: str | None = Field(None, description="ISO 8601 generation timestamp")
    s3_uri: dict[str, Any] | None = Field(None, description="S3 URIs for generated files")


class DomainReportsResponse(BaseModel):
    """Response for GET /report-domains/{domain_name}/reports."""

    success: bool = True
    domain_name: str = Field(..., description="Domain slug that was queried")
    category_name: str = Field(..., description="Human-readable domain label")
    total: int = Field(..., description="Total non-draft reports in this domain for the user")
    limit: int = Field(..., description="Page size applied")
    offset: int = Field(..., description="Offset applied")
    reports: list[DomainReportItem] = Field(
        default_factory=list, description="Paginated report list"
    )


# ---------------------------------------------------------------------------
# Categories (static catalog of report types)
# ---------------------------------------------------------------------------


class CategoryItem(BaseModel):
    """A single report-category card for the homepage catalog."""

    slug: str = Field(
        ...,
        description=(
            "Stable slug used in API params and DB writes. Note: the internal "
            "domain ``default`` is surfaced as ``standard`` in API responses."
        ),
    )
    display_name: str = Field(..., description="Human-readable label (e.g. 'Due Diligence')")


class CategoriesResponse(BaseModel):
    """Response for GET /categories."""

    success: bool = True
    categories: list[CategoryItem] = Field(
        default_factory=list,
        description="All available report categories, in fixed display order",
    )


# ---------------------------------------------------------------------------
# Home search (cross-domain search for chats + reports)
# ---------------------------------------------------------------------------


class HomeSearchItem(BaseModel):
    """One hit in the home search results.

    A hit represents a chat (which may or may not have an associated report).
    For drafts there is no report; for non-draft hits ``report_*`` fields are
    populated and ``domain`` reflects the report's domain (``default`` is
    surfaced as ``standard``).
    """

    chat_id: str = Field(..., description="Chat identifier")
    chat_title: str | None = Field(None, description="Chat title")
    report_id: str | None = Field(None, description="Latest report id, if any")
    report_title: str | None = Field(None, description="Latest report title, if any")
    status: str = Field(
        ...,
        description="Status of the latest report; 'draft' if no report exists",
    )
    domain: str = Field(
        ...,
        description="Report domain slug (default surfaced as 'standard')",
    )
    poster_image_url: str | None = Field(None, description="Poster/thumbnail CDN URL")
    created_at: str | None = Field(None, description="Chat created_at, ISO-8601")
    updated_at: str | None = Field(None, description="Chat updated_at, ISO-8601")
    matched_on: str = Field(
        ...,
        description="Where the search hit matched: 'chat_title', 'report_title', or 'both'",
    )


class HomeSearchResponse(BaseModel):
    """Response for GET /home-search."""

    success: bool = True
    total: int = Field(..., description="Total matching items (use with limit/offset)")
    limit: int = Field(..., description="Page size applied")
    offset: int = Field(..., description="Offset applied")
    items: list[HomeSearchItem] = Field(default_factory=list, description="Search hits page")


# ---------------------------------------------------------------------------
# Ongoing chats (drafts grouped by category)
# ---------------------------------------------------------------------------


class OngoingChatItem(BaseModel):
    """A single draft chat for the ongoing-chats endpoint."""

    chat_id: str = Field(..., description="Chat identifier")
    chat_title: str | None = Field(None, description="Chat title")
    status: str = Field(..., description="Report status; 'draft' for all entries here")
    poster_image_url: str | None = Field(None, description="Poster/thumbnail CDN URL")
    created_at: str | None = Field(None, description="Chat created_at, ISO-8601")
    updated_at: str | None = Field(None, description="Chat updated_at, ISO-8601")
    session_id: str | None = Field(None, description="Active chat session id (if any)")


class OngoingChatsResponse(BaseModel):
    """Response for GET /ongoing-chats.

    Draft reports do not yet have a ``domain_name`` in the DB, so all
    drafts are returned under the ``standard`` key. As soon as drafts
    start carrying a domain, additional category keys will appear here
    alongside ``standard``.
    """

    success: bool = True
    standard: list[OngoingChatItem] = Field(
        default_factory=list,
        description="Draft chats for the default/standard category",
    )
