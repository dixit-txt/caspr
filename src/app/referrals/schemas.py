"""referral.py: Referral-related schemas"""
from typing import Optional
from pydantic import BaseModel, Field


class ReferralInfoResponse(BaseModel):
    """Response for getting referral information."""
    success: bool
    referral_code: str = Field(..., description="User's unique referral code")
    total_referrals: int = Field(..., description="Total successful referrals")
    total_tokens_earned: int = Field(..., description="Total tokens earned from referrals")
    referral_link: str = Field(..., description="Complete referral link to share")


class ReferralErrorResponse(BaseModel):
    """Error response for referral operations."""
    success: bool = False
    error: str

