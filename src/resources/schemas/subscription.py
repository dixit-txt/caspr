"""subscription.py: Subscription and request-related schemas"""
from typing import Optional
from pydantic import BaseModel, EmailStr


class PhoneNumberModel(BaseModel):
    country_code: str
    number: str


class SubscribeRequest(BaseModel):
    email_id: EmailStr


class SubscribeResponse(BaseModel):
    success: bool = True
    message: str = "Successfully subscribed!"


class RequestRequest(BaseModel):
    name: str
    email: EmailStr
    website: Optional[str] = None
    description: Optional[str] = None


class RequestResponse(BaseModel):
    success: bool = True
    message: str = "Request submitted successfully!"


class BookCallRequest(BaseModel):
    name: str
    email: EmailStr
    phone: PhoneNumberModel
    brief: Optional[str] = None


class BookCallResponse(BaseModel):
    success: bool = True
    message: str = "Call booking submitted successfully. We will be in touch shortly."


class LiveSourcesResponse(BaseModel):
    success: bool
    count: int