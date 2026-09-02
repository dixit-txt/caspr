"""Pydantic schemas for the onboarding API."""

from typing import List, Optional
from pydantic import BaseModel, EmailStr, Field


# -- Request schemas --

class SaveUserRoleRequest(BaseModel):
    role_id: str
    custom_role_text: Optional[str] = Field(
        default=None,
        max_length=100,
        description=(
            "Free-text description the user typed when selecting the 'Other' "
            "role. Required only when role_id maps to the 'other' row in "
            "user_roles; ignored otherwise."
        ),
    )


class SaveResearchInterestsRequest(BaseModel):
    interest_ids: List[str]
    custom_research_text: Optional[str] = Field(
        default=None,
        max_length=100,
        description=(
            "Free-text description the user typed when selecting 'Something "
            "else' in the research-interests step. Required only when one of "
            "the selected interest_ids maps to the 'something_else' row in "
            "research_interests; ignored otherwise."
        ),
    )


class ValidateUniversityEmailRequest(BaseModel):
    email: EmailStr


class SaveUniversityEmailRequest(BaseModel):
    university_email: EmailStr
    university_id: str


# -- Response schemas --

class UserRoleItem(BaseModel):
    id: str
    name: str
    display_name: str
    description: Optional[str] = None
    discount_tag: Optional[str] = None
    display_order: int


class ResearchInterestItem(BaseModel):
    id: str
    name: str
    display_name: str
    description: Optional[str] = None
    display_order: int


class UserRolesResponse(BaseModel):
    success: bool
    roles: List[UserRoleItem] = []


class ResearchInterestsResponse(BaseModel):
    success: bool
    interests: List[ResearchInterestItem] = []


class ValidateUniversityEmailResponse(BaseModel):
    success: bool
    matched: bool = False
    university: Optional[dict] = None
    message: Optional[str] = None


class OnboardingStatusResponse(BaseModel):
    success: bool
    onboarding: Optional[dict] = None


class OnboardingStepResponse(BaseModel):
    success: bool
    message: str = ""
    error: Optional[str] = None
