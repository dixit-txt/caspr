"""
onboarding_api.py
=================
FastAPI router for the post-login onboarding flow.

Endpoints:
    GET   /onboarding/roles              – List active user roles
    GET   /onboarding/research-interests – List active research interests
    POST  /onboarding/save-role          – Save selected role
    POST  /onboarding/save-interests     – Save selected research interests
    POST  /onboarding/validate-university-email – Check if university domain is registered
    POST  /onboarding/save-university-email     – Persist university email + link university
    POST  /onboarding/complete           – Mark onboarding as done
    GET   /onboarding/status             – Get current onboarding state (for page-refresh resume)
"""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from src.config.log_helper import setup_logging
from src.core.token_auth import get_current_active_user
from src.db.db_utils import async_session_scope
from src.db.onboarding_db import (
    get_active_research_interests,
    get_active_user_roles,
    get_user_onboarding_status,
    mark_onboarding_completed,
    save_university_email,
    save_user_research_interests,
    save_user_role,
    validate_university_domain,
)
from src.resources.schemas.onboarding import (
    OnboardingStatusResponse,
    OnboardingStepResponse,
    ResearchInterestsResponse,
    SaveResearchInterestsRequest,
    SaveUniversityEmailRequest,
    SaveUserRoleRequest,
    UserRolesResponse,
    ValidateUniversityEmailRequest,
    ValidateUniversityEmailResponse,
)

logger = setup_logging(__file__)

router = APIRouter(prefix="/onboarding", tags=["Onboarding"])


# ============================================================================
# GET — lookup data for dropdowns
# ============================================================================

@router.get("/roles", response_model=UserRolesResponse)
async def list_roles(request: Request, user=Depends(get_current_active_user)):
    """Return all active user-role options for the onboarding UI."""
    try:
        async with async_session_scope() as session:
            result = await get_active_user_roles(session)
            if not result.get("success"):
                return JSONResponse(status_code=500, content={"success": False, "error": "Failed to fetch roles"})
            return UserRolesResponse(success=True, roles=result["roles"])
    except Exception as e:
        logger.error(f"[LIST_ROLES] error: {e}", exc_info=True)
        return JSONResponse(status_code=500, content={"success": False, "error": "Internal server error"})


@router.get("/research-interests", response_model=ResearchInterestsResponse)
async def list_research_interests(request: Request, user=Depends(get_current_active_user)):
    """Return all active research-interest options for the onboarding UI."""
    try:
        async with async_session_scope() as session:
            result = await get_active_research_interests(session)
            if not result.get("success"):
                return JSONResponse(status_code=500, content={"success": False, "error": "Failed to fetch interests"})
            return ResearchInterestsResponse(success=True, interests=result["interests"])
    except Exception as e:
        logger.error(f"[LIST_RESEARCH_INTERESTS] error: {e}", exc_info=True)
        return JSONResponse(status_code=500, content={"success": False, "error": "Internal server error"})


# ============================================================================
# POST — onboarding steps
# ============================================================================

@router.post("/save-role", response_model=OnboardingStepResponse)
async def save_role(data: SaveUserRoleRequest, request: Request, user=Depends(get_current_active_user)):
    """Save the user's role selection (Step 1).

    When the chosen role is the ``Other`` row, ``custom_role_text`` must be
    supplied in the request body and is persisted alongside the role.
    """
    try:
        async with async_session_scope() as session:
            result = await save_user_role(
                user,
                data.role_id,
                session,
                custom_role_text=data.custom_role_text,
            )
            if not result.get("success"):
                return JSONResponse(status_code=400, content={"success": False, "error": result.get("error", "Failed to save role")})
            return OnboardingStepResponse(success=True, message="Role saved")
    except Exception as e:
        logger.error(f"[SAVE_ROLE] error: {e}", exc_info=True)
        return JSONResponse(status_code=500, content={"success": False, "error": "Internal server error"})


@router.post("/save-interests", response_model=OnboardingStepResponse)
async def save_interests(data: SaveResearchInterestsRequest, request: Request, user=Depends(get_current_active_user)):
    """Save the user's research interest selections (Step 2).

    When one of the chosen interests is the ``Something else`` row,
    ``custom_research_text`` must be supplied in the request body and is
    persisted on the user.
    """
    if not data.interest_ids:
        return JSONResponse(status_code=422, content={"success": False, "error": "At least one interest must be selected"})
    try:
        async with async_session_scope() as session:
            result = await save_user_research_interests(
                user,
                data.interest_ids,
                session,
                custom_research_text=data.custom_research_text,
            )
            if not result.get("success"):
                return JSONResponse(status_code=400, content={"success": False, "error": result.get("error", "Failed to save interests")})
            return OnboardingStepResponse(success=True, message="Research interests saved")
    except Exception as e:
        logger.error(f"[SAVE_INTERESTS] error: {e}", exc_info=True)
        return JSONResponse(status_code=500, content={"success": False, "error": "Internal server error"})


@router.post("/validate-university-email", response_model=ValidateUniversityEmailResponse)
async def validate_university_email(data: ValidateUniversityEmailRequest, request: Request, user=Depends(get_current_active_user)):
    """Check whether the university email domain is registered with Caspr."""
    try:
        async with async_session_scope() as session:
            result = await validate_university_domain(data.email, session)
            if not result.get("success"):
                return JSONResponse(status_code=500, content={"success": False, "error": "Validation failed"})

            if result["matched"]:
                return ValidateUniversityEmailResponse(
                    success=True,
                    matched=True,
                    university=result["university"],
                    message="University is registered with us",
                )
            return ValidateUniversityEmailResponse(
                success=True,
                matched=False,
                message="Sorry! This university is not registered with us yet",
            )
    except Exception as e:
        logger.error(f"[VALIDATE_UNIVERSITY_EMAIL] error: {e}", exc_info=True)
        return JSONResponse(status_code=500, content={"success": False, "error": "Internal server error"})


@router.post("/save-university-email", response_model=OnboardingStepResponse)
async def save_uni_email(data: SaveUniversityEmailRequest, request: Request, user=Depends(get_current_active_user)):
    """
    Persist the student's university email and link the university.
    The frontend should call /validate-university-email first to get the
    university_id, then pass both here.
    """
    try:
        async with async_session_scope() as session:
            result = await save_university_email(
                user, str(data.university_email), data.university_id, session
            )
            if not result.get("success"):
                return JSONResponse(status_code=400, content={"success": False, "error": result.get("error", "Failed to save university email")})
            return OnboardingStepResponse(success=True, message="University email saved. A verification link has been sent.")
    except Exception as e:
        logger.error(f"[SAVE_UNIVERSITY_EMAIL] error: {e}", exc_info=True)
        return JSONResponse(status_code=500, content={"success": False, "error": "Internal server error"})


@router.post("/complete", response_model=OnboardingStepResponse)
async def complete_onboarding(request: Request, user=Depends(get_current_active_user)):
    """Mark the user's onboarding as complete."""
    try:
        async with async_session_scope() as session:
            result = await mark_onboarding_completed(user, session)
            if not result.get("success"):
                return JSONResponse(status_code=500, content={"success": False, "error": "Failed to complete onboarding"})
            return OnboardingStepResponse(success=True, message="Onboarding completed")
    except Exception as e:
        logger.error(f"[COMPLETE_ONBOARDING] error: {e}", exc_info=True)
        return JSONResponse(status_code=500, content={"success": False, "error": "Internal server error"})


# ============================================================================
# GET — resume state
# ============================================================================

@router.get("/status", response_model=OnboardingStatusResponse)
async def onboarding_status(request: Request, user=Depends(get_current_active_user)):
    """Return the user's current onboarding progress so the frontend can resume from the right step."""
    try:
        async with async_session_scope() as session:
            result = await get_user_onboarding_status(user, session)
            if not result.get("success"):
                return JSONResponse(status_code=500, content={"success": False, "error": result.get("error", "Failed to fetch status")})
            return OnboardingStatusResponse(success=True, onboarding=result["onboarding"])
    except Exception as e:
        logger.error(f"[ONBOARDING_STATUS] error: {e}", exc_info=True)
        return JSONResponse(status_code=500, content={"success": False, "error": "Internal server error"})
