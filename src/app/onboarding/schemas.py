"""Pydantic schemas for the onboarding API."""

from pydantic import BaseModel, EmailStr, Field

# -- Request schemas --


class SaveUserRoleRequest(BaseModel):
    role_id: str
    custom_role_text: str | None = Field(
        default=None,
        max_length=100,
        description=(
            "Free-text description the user typed when selecting the 'Other' "
            "role. Required only when role_id maps to the 'other' row in "
            "user_roles; ignored otherwise."
        ),
    )


class SaveResearchInterestsRequest(BaseModel):
    interest_ids: list[str]
    custom_research_text: str | None = Field(
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
    description: str | None = None
    discount_tag: str | None = None
    display_order: int


class ResearchInterestItem(BaseModel):
    id: str
    name: str
    display_name: str
    description: str | None = None
    display_order: int


class UserRolesResponse(BaseModel):
    success: bool
    roles: list[UserRoleItem] = []


class ResearchInterestsResponse(BaseModel):
    success: bool
    interests: list[ResearchInterestItem] = []


class ValidateUniversityEmailResponse(BaseModel):
    success: bool
    matched: bool = False
    university: dict | None = None
    message: str | None = None


class OnboardingStatusResponse(BaseModel):
    success: bool
    onboarding: dict | None = None


class OnboardingStepResponse(BaseModel):
    success: bool
    message: str = ""
    error: str | None = None
