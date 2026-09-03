"""Request and response schemas for the cards bounded context.

Moved verbatim from ``src/resources/schemas/chat.py`` during the R-STRUCT-1
migration. Field definitions and validators are unchanged.
"""

from typing import Any

from pydantic import BaseModel, field_validator

from app.core.constants import REFINE_MAXIMUM_CHARACTERS
from app.core.errors import ErrorResponse  # noqa: F401


class RefineOrDeleteRequest(BaseModel):
    report_id: str
    card: dict[str, Any]

    @field_validator("card")
    @classmethod
    def validate_user_instruction_length(cls, card: dict[str, Any]) -> dict[str, Any]:
        instruction = card.get("user_instruction") or ""
        if len(instruction) > REFINE_MAXIMUM_CHARACTERS:
            raise ValueError(
                f"user_instruction must be at most {REFINE_MAXIMUM_CHARACTERS} characters"
            )
        subsection = card.get("subsection") or {}
        sub_instruction = subsection.get("user_instruction") or ""
        if len(sub_instruction) > REFINE_MAXIMUM_CHARACTERS:
            raise ValueError(
                f"subsection.user_instruction must be at most {REFINE_MAXIMUM_CHARACTERS} characters"
            )
        return card


class RefineOrDeleteResponse(BaseModel):
    success: bool
    message: str
    refine_card: dict[str, Any] | None = None


class RefineVisualizationRequest(BaseModel):
    report_id: str
    card: dict[str, Any]

    @field_validator("card")
    @classmethod
    def validate_user_instruction_length(cls, card: dict[str, Any]) -> dict[str, Any]:
        instruction = card.get("user_instruction") or ""
        if len(instruction) > REFINE_MAXIMUM_CHARACTERS:
            raise ValueError(
                f"user_instruction must be at most {REFINE_MAXIMUM_CHARACTERS} characters"
            )
        return card


class RefineVisualizationResponse(BaseModel):
    success: bool
    message: str
    updated_card: dict[str, Any] | None = None


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
    message: str | None = None
    reverted_to_version: int | None = None
    card_id: str | None = None
    primary_card_id: str | None = None  # New active card's primary key
    deleted_versions: list[int] | None = None
    reverted_card: dict[str, Any] | None = None  # Full card structure (same format as refine_card)


class RegenerateESRequest(BaseModel):
    report_id: str


class RegenerateESResponse(BaseModel):
    success: bool
    message: str
    new_es_version: int | None = None
    updated_summary: str | None = None
