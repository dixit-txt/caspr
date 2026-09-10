"""auth.py: Authentication-related schemas"""

from pydantic import BaseModel, EmailStr, Field


class SignupRequest(BaseModel):
    name: str
    email: EmailStr
    phone_country_code: str
    phone: str
    password: str
    referral_code: str | None = Field(None, max_length=12, description="Referral code (optional)")


class SignupResponse(BaseModel):
    success: bool = True
    message: str = "You need to verify your email address to continue."


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class LoginResponse(BaseModel):
    success: bool = True
    token: str
    refresh_token: str
    user_id: str
    user_name: str
    t_c_verified: bool
    onboarding_completed: bool


class GoogleLoginRequest(BaseModel):
    id_token: str


class GoogleLoginResponse(BaseModel):
    success: bool = True
    token: str
    refresh_token: str
    user_id: str
    user_name: str
    t_c_verified: bool
    onboarding_completed: bool


class LogoutRequest(BaseModel):
    refresh_token: str


class LogoutResponse(BaseModel):
    success: bool = True
    message: str = "You have been successfully logged out."


class ErrorResponse(BaseModel):
    success: bool = False
    error: str


class TokenData(BaseModel):
    user_id: str
    type: str


class ForgotPasswordRequest(BaseModel):
    email: EmailStr


class ForgotPasswordResponse(BaseModel):
    success: bool
    message: str


class VerifyResetTokenResponse(BaseModel):
    success: bool
    valid: bool
    message: str


class ResetPasswordRequest(BaseModel):
    token: str
    password: str


class ResetPasswordResponse(BaseModel):
    success: bool
    message: str


class SendVerificationLinkRequest(BaseModel):
    email: EmailStr


class SendVerificationLinkResponse(BaseModel):
    success: bool
    message: str


class VerifyAccountTokenRequest(BaseModel):
    token: str


class VerifyAccountTokenResponse(BaseModel):
    success: bool
    message: str
    token: str
    refresh_token: str
    user_id: str
    user_name: str
    t_c_verified: bool


class UpdateTCVerifiedRequest(BaseModel):
    t_c_verified: bool


class UpdateTCVerifiedResponse(BaseModel):
    success: bool
    message: str
    t_c_verified: bool


class GetTCVerifiedResponse(BaseModel):
    success: bool
    t_c_verified: bool


class GetUserTokensResponse(BaseModel):
    success: bool
    user_id: str
    user_name: str
    token: str
    refresh_token: str
    t_c_verified: bool


class GetWalkoverStatusResponse(BaseModel):
    success: bool = True
    walkover_completed: bool = Field(
        ...,
        description=(
            "False until the user has explicitly finished the in-app "
            "walkover/walkthrough. Frontend should show the walkover whenever "
            "this is False, and call POST /complete-walkover once the user "
            "finishes it."
        ),
    )


class CompleteWalkoverResponse(BaseModel):
    success: bool = True
    message: str = "Walkover marked as completed."
    walkover_completed: bool = True
