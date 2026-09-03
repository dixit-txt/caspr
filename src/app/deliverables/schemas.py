"""Request and response schemas for the deliverables bounded context.

Moved verbatim from ``src/resources/schemas/chat.py`` during the R-STRUCT-1
migration. Field definitions and validators are unchanged.
"""

from pydantic import BaseModel

from app.core.errors import ErrorResponse  # noqa: F401


class GeneratePresentationRequest(BaseModel):
    report_id: str
    report_version_id: str | None = None
    generation_mode: str = "template"  # "template" (Accenture master) or "scratch" (pptxgenjs)


class GeneratePresentationResponse(BaseModel):
    success: bool
    message: str
    report_version_id: str | None = None  # Version ID that was generated
    version: int | None = None  # Version number that was generated
    pptx_s3_uri: str | None = None
    pptx_generation_time: str | None = None
    report_id: str | None = None
    modified: bool | None = None  # True if new PPTX generated, False if no changes
    already_exists: bool | None = (
        None  # False if newly generated, True if output already existed for this version
    )


class DownloadPresentationResponse(BaseModel):
    success: bool
    download_url: str


class GenerateInfographicRequest(BaseModel):
    report_id: str
    report_version_id: str | None = None  # Specific version to generate for


class GenerateInfographicResponse(BaseModel):
    success: bool
    message: str
    report_version_id: str | None = None  # Version ID that was generated
    version: int | None = None  # Version number that was generated
    info_pdf_s3_uri: str | None = None
    info_pdf_generation_time: str | None = None
    report_id: str | None = None
    modified: bool | None = None  # True if new infographic generated, False if no changes
    already_exists: bool | None = (
        None  # False if newly generated, True if output already existed for this version
    )
