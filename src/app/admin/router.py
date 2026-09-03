"""admin_api.py: CEO/cofounder dashboard APIs.

User tracking  → users / chats / reports / versions + current subscription only
Cost tracking  → costtracker only (tokens, spend, model, feature/context)
"""

from datetime import datetime

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse

from app.admin import repository as admin_db
from app.admin.dependencies import get_current_dashboard_admin
from app.core.db import async_session_scope
from app.core.logging import setup_logging

logger = setup_logging(__file__)

router = APIRouter(prefix="/admin", tags=["Admin Dashboard"])

_RANGE_DESC = "Preset: today | 2d | 7d | 30d | all (ignored if start_date/end_date set)"
_START_DESC = "Custom window start (YYYY-MM-DD or ISO datetime). Overrides range presets."
_END_DESC = "Custom window end (YYYY-MM-DD or ISO datetime). Defaults to now."


def _ok(data: dict) -> dict:
    return {"success": True, **data}


def _resolve_period(
    range: str | None,
    start_date: str | None,
    end_date: str | None,
) -> tuple[datetime | None, datetime, str] | JSONResponse:
    try:
        return admin_db.resolve_period(range, start_date, end_date)
    except ValueError as e:
        return JSONResponse(
            status_code=400,
            content={"success": False, "error": str(e)},
        )


def _period_payload(key: str, start: datetime | None, end: datetime) -> dict:
    return {
        "range": key,
        "start": start.isoformat() if start else None,
        "end": end.isoformat(),
    }


@router.get("/me")
async def admin_me(admin: dict = Depends(get_current_dashboard_admin)):
    """Who is logged into the dashboard."""
    return _ok({"admin": admin})


@router.get("/overview")
async def overview(
    range: str = Query("2d", description=_RANGE_DESC),
    start_date: str | None = Query(None, description=_START_DESC),
    end_date: str | None = Query(None, description=_END_DESC),
    user_id: str | None = Query(None, description="Optional: scope to one user"),
    admin: dict = Depends(get_current_dashboard_admin),
):
    """
    Executive overview:
    - total users (all registered)
    - active users (range)
    - spend today / all-time (+ range spend)
    - top features by spend
    - top users by spend (company mode only)
    """
    try:
        resolved = _resolve_period(range, start_date, end_date)
        if isinstance(resolved, JSONResponse):
            return resolved
        start, end, key = resolved
        async with async_session_scope() as session:
            costs = await admin_db.cost_summary(session, start=start, end=end, user_id=user_id)
            functionalities = await admin_db.cost_by_functionality(
                session, start=start, end=end, user_id=user_id
            )
            features = await admin_db.cost_by_feature(
                session, start=start, end=end, user_id=user_id, limit=10
            )
            models = await admin_db.cost_by_model(
                session, start=start, end=end, user_id=user_id, limit=8
            )
            payload = {
                **_period_payload(key, start, end),
                "user_id": user_id,
                "costs": costs,
                "cost_by_functionality": functionalities,
                "top_features": features,
                "top_models": models,
            }
            # Company-wide people KPIs are always real numbers, even when scoped
            # to one user (this section is for execs — the totals must be fair).
            payload["total_users"] = await admin_db.count_total_users(session)
            payload["active_users"] = await admin_db.count_active_users(
                session, start=start, end=end
            )
            payload["new_users"] = await admin_db.count_new_users(session, start=start, end=end)
            if user_id:
                summary = await admin_db.user_summary(
                    session, user_id=user_id, start=start, end=end
                )
                if not summary:
                    return JSONResponse(
                        status_code=404,
                        content={"success": False, "error": "User not found"},
                    )
                payload["user"] = summary
            else:
                payload["top_users"] = await admin_db.cost_by_user(
                    session, start=start, end=end, limit=10
                )
            return _ok(payload)
    except Exception as e:
        logger.error(f"[ADMIN_OVERVIEW] {e}", exc_info=True)
        return JSONResponse(
            status_code=500, content={"success": False, "error": "Internal server error"}
        )


# ---------------------------------------------------------------------------
# USER TRACKING
# ---------------------------------------------------------------------------


@router.get("/users/search")
async def users_search(
    q: str = Query(..., min_length=1),
    limit: int = Query(20, ge=1, le=50),
    admin: dict = Depends(get_current_dashboard_admin),
):
    """Typeahead search by email or name. Includes current subscription only."""
    try:
        async with async_session_scope() as session:
            users = await admin_db.search_users(session, q=q, limit=limit)
            return _ok({"users": users})
    except Exception as e:
        logger.error(f"[ADMIN_USERS_SEARCH] {e}", exc_info=True)
        return JSONResponse(
            status_code=500, content={"success": False, "error": "Internal server error"}
        )


@router.get("/users/active")
async def users_active(
    range: str = Query("30d", description=_RANGE_DESC),
    start_date: str | None = Query(None, description=_START_DESC),
    end_date: str | None = Query(None, description=_END_DESC),
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=200),
    include_list: bool = Query(True, description="Include paginated active-user rows"),
    admin: dict = Depends(get_current_dashboard_admin),
):
    """Active-user count for any custom period (day / days / month), plus daily series."""
    try:
        resolved = _resolve_period(range, start_date, end_date)
        if isinstance(resolved, JSONResponse):
            return resolved
        start, end, key = resolved
        async with async_session_scope() as session:
            report = await admin_db.active_users_report(
                session,
                start=start,
                end=end,
                page=page,
                page_size=page_size,
                include_list=include_list,
            )
            return _ok({**_period_payload(key, start, end), **report})
    except Exception as e:
        logger.error(f"[ADMIN_USERS_ACTIVE] {e}", exc_info=True)
        return JSONResponse(
            status_code=500, content={"success": False, "error": "Internal server error"}
        )


@router.get("/users")
async def users_list(
    range: str = Query("2d", description=_RANGE_DESC),
    start_date: str | None = Query(None, description=_START_DESC),
    end_date: str | None = Query(None, description=_END_DESC),
    q: str | None = Query(None, description="Search user name or email"),
    sort: str = Query("spend", description="spend | chats | last_active"),
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=200),
    active_only: bool = Query(False, description="If true, only users with activity in the period"),
    admin: dict = Depends(get_current_dashboard_admin),
):
    """All users (searchable, paginated), sorted by usage in the selected period."""
    try:
        resolved = _resolve_period(range, start_date, end_date)
        if isinstance(resolved, JSONResponse):
            return resolved
        start, end, key = resolved
        async with async_session_scope() as session:
            result = await admin_db.list_users_with_activity(
                session,
                start=start,
                end=end,
                q=q,
                page=page,
                page_size=page_size,
                sort=sort,
                active_only=active_only,
            )
            return _ok({**_period_payload(key, start, end), **result})
    except Exception as e:
        logger.error(f"[ADMIN_USERS_LIST] {e}", exc_info=True)
        return JSONResponse(
            status_code=500, content={"success": False, "error": "Internal server error"}
        )


@router.get("/users/{user_id}/summary")
async def users_summary(
    user_id: str,
    range: str = Query("2d", description=_RANGE_DESC),
    start_date: str | None = Query(None, description=_START_DESC),
    end_date: str | None = Query(None, description=_END_DESC),
    admin: dict = Depends(get_current_dashboard_admin),
):
    """One user: profile, subscription, activity counts, cost breakdown (costtracker)."""
    try:
        resolved = _resolve_period(range, start_date, end_date)
        if isinstance(resolved, JSONResponse):
            return resolved
        start, end, key = resolved
        async with async_session_scope() as session:
            summary = await admin_db.user_summary(session, user_id=user_id, start=start, end=end)
            if not summary:
                return JSONResponse(
                    status_code=404, content={"success": False, "error": "User not found"}
                )
            return _ok({**_period_payload(key, start, end), "user": summary})
    except Exception as e:
        logger.error(f"[ADMIN_USER_SUMMARY] {e}", exc_info=True)
        return JSONResponse(
            status_code=500, content={"success": False, "error": "Internal server error"}
        )


@router.get("/users/{user_id}/chats")
async def users_chats(
    user_id: str,
    range: str = Query("2d", description=_RANGE_DESC),
    start_date: str | None = Query(None, description=_START_DESC),
    end_date: str | None = Query(None, description=_END_DESC),
    limit: int = Query(50, ge=1, le=200),
    admin: dict = Depends(get_current_dashboard_admin),
):
    """Tree level: User → Chats (with spend from costtracker)."""
    try:
        resolved = _resolve_period(range, start_date, end_date)
        if isinstance(resolved, JSONResponse):
            return resolved
        start, end, key = resolved
        async with async_session_scope() as session:
            chats = await admin_db.user_chats(
                session, user_id=user_id, start=start, end=end, limit=limit
            )
            return _ok(
                {
                    **_period_payload(key, start, end),
                    "user_id": user_id,
                    "chats": chats,
                }
            )
    except Exception as e:
        logger.error(f"[ADMIN_USER_CHATS] {e}", exc_info=True)
        return JSONResponse(
            status_code=500, content={"success": False, "error": "Internal server error"}
        )


@router.get("/users/{user_id}/chats/{chat_id}/costs")
async def chat_costs(
    user_id: str,
    chat_id: str,
    range: str = Query("all", description=_RANGE_DESC),
    start_date: str | None = Query(None, description=_START_DESC),
    end_date: str | None = Query(None, description=_END_DESC),
    admin: dict = Depends(get_current_dashboard_admin),
):
    """Tree level: Chat → costtracker feature/model breakdown."""
    try:
        resolved = _resolve_period(range, start_date, end_date)
        if isinstance(resolved, JSONResponse):
            return resolved
        start, end, key = resolved
        async with async_session_scope() as session:
            costs = await admin_db.cost_for_chat(session, chat_id=chat_id, start=start, end=end)
            return _ok(
                {
                    **_period_payload(key, start, end),
                    "user_id": user_id,
                    **costs,
                }
            )
    except Exception as e:
        logger.error(f"[ADMIN_CHAT_COSTS] {e}", exc_info=True)
        return JSONResponse(
            status_code=500, content={"success": False, "error": "Internal server error"}
        )


@router.get("/users/{user_id}/chats/{chat_id}/reports")
async def chat_reports(
    user_id: str,
    chat_id: str,
    admin: dict = Depends(get_current_dashboard_admin),
):
    """Tree level: Chat → Reports."""
    try:
        async with async_session_scope() as session:
            reports = await admin_db.chat_reports(session, chat_id=chat_id)
            return _ok({"user_id": user_id, "chat_id": chat_id, "reports": reports})
    except Exception as e:
        logger.error(f"[ADMIN_CHAT_REPORTS] {e}", exc_info=True)
        return JSONResponse(
            status_code=500, content={"success": False, "error": "Internal server error"}
        )


@router.get("/reports/{report_id}/versions")
async def report_versions_list(
    report_id: str,
    admin: dict = Depends(get_current_dashboard_admin),
):
    """Tree level: Report → Versions."""
    try:
        async with async_session_scope() as session:
            versions = await admin_db.report_versions(session, report_id=report_id)
            return _ok({"report_id": report_id, "versions": versions})
    except Exception as e:
        logger.error(f"[ADMIN_REPORT_VERSIONS] {e}", exc_info=True)
        return JSONResponse(
            status_code=500, content={"success": False, "error": "Internal server error"}
        )


# ---------------------------------------------------------------------------
# COST TRACKING — costtracker only
# ---------------------------------------------------------------------------


@router.get("/costs/summary")
async def costs_summary(
    range: str = Query("2d", description=_RANGE_DESC),
    start_date: str | None = Query(None, description=_START_DESC),
    end_date: str | None = Query(None, description=_END_DESC),
    user_id: str | None = None,
    admin: dict = Depends(get_current_dashboard_admin),
):
    try:
        resolved = _resolve_period(range, start_date, end_date)
        if isinstance(resolved, JSONResponse):
            return resolved
        start, end, key = resolved
        async with async_session_scope() as session:
            summary = await admin_db.cost_summary(session, start=start, end=end, user_id=user_id)
            return _ok({**_period_payload(key, start, end), "user_id": user_id, **summary})
    except Exception as e:
        logger.error(f"[ADMIN_COSTS_SUMMARY] {e}", exc_info=True)
        return JSONResponse(
            status_code=500, content={"success": False, "error": "Internal server error"}
        )


@router.get("/costs/by-feature")
async def costs_by_feature(
    range: str = Query("2d", description=_RANGE_DESC),
    start_date: str | None = Query(None, description=_START_DESC),
    end_date: str | None = Query(None, description=_END_DESC),
    user_id: str | None = None,
    limit: int = Query(50, ge=1, le=200),
    admin: dict = Depends(get_current_dashboard_admin),
):
    try:
        resolved = _resolve_period(range, start_date, end_date)
        if isinstance(resolved, JSONResponse):
            return resolved
        start, end, key = resolved
        async with async_session_scope() as session:
            rows = await admin_db.cost_by_feature(
                session, start=start, end=end, user_id=user_id, limit=limit
            )
            return _ok(
                {
                    **_period_payload(key, start, end),
                    "user_id": user_id,
                    "features": rows,
                }
            )
    except Exception as e:
        logger.error(f"[ADMIN_COSTS_BY_FEATURE] {e}", exc_info=True)
        return JSONResponse(
            status_code=500, content={"success": False, "error": "Internal server error"}
        )


@router.get("/costs/by-functionality")
async def costs_by_functionality(
    range: str = Query("2d", description=_RANGE_DESC),
    start_date: str | None = Query(None, description=_START_DESC),
    end_date: str | None = Query(None, description=_END_DESC),
    user_id: str | None = None,
    admin: dict = Depends(get_current_dashboard_admin),
):
    """Spend grouped by user-facing functionality (chat, report generation,
    refine, refine visualization, executive summary, infographic, PPTX
    generation, Ask CASPR). The non-technical, feature-level cost view."""
    try:
        resolved = _resolve_period(range, start_date, end_date)
        if isinstance(resolved, JSONResponse):
            return resolved
        start, end, key = resolved
        async with async_session_scope() as session:
            rows = await admin_db.cost_by_functionality(
                session, start=start, end=end, user_id=user_id
            )
            return _ok(
                {
                    **_period_payload(key, start, end),
                    "user_id": user_id,
                    "functionalities": rows,
                }
            )
    except Exception as e:
        logger.error(f"[ADMIN_COSTS_BY_FUNCTIONALITY] {e}", exc_info=True)
        return JSONResponse(
            status_code=500, content={"success": False, "error": "Internal server error"}
        )


@router.get("/costs/by-model")
async def costs_by_model(
    range: str = Query("2d", description=_RANGE_DESC),
    start_date: str | None = Query(None, description=_START_DESC),
    end_date: str | None = Query(None, description=_END_DESC),
    user_id: str | None = None,
    limit: int = Query(30, ge=1, le=100),
    admin: dict = Depends(get_current_dashboard_admin),
):
    try:
        resolved = _resolve_period(range, start_date, end_date)
        if isinstance(resolved, JSONResponse):
            return resolved
        start, end, key = resolved
        async with async_session_scope() as session:
            rows = await admin_db.cost_by_model(
                session, start=start, end=end, user_id=user_id, limit=limit
            )
            return _ok(
                {
                    **_period_payload(key, start, end),
                    "user_id": user_id,
                    "models": rows,
                }
            )
    except Exception as e:
        logger.error(f"[ADMIN_COSTS_BY_MODEL] {e}", exc_info=True)
        return JSONResponse(
            status_code=500, content={"success": False, "error": "Internal server error"}
        )


@router.get("/costs/by-user")
async def costs_by_user(
    range: str = Query("2d", description=_RANGE_DESC),
    start_date: str | None = Query(None, description=_START_DESC),
    end_date: str | None = Query(None, description=_END_DESC),
    limit: int = Query(50, ge=1, le=200),
    admin: dict = Depends(get_current_dashboard_admin),
):
    try:
        resolved = _resolve_period(range, start_date, end_date)
        if isinstance(resolved, JSONResponse):
            return resolved
        start, end, key = resolved
        async with async_session_scope() as session:
            rows = await admin_db.cost_by_user(session, start=start, end=end, limit=limit)
            return _ok({**_period_payload(key, start, end), "users": rows})
    except Exception as e:
        logger.error(f"[ADMIN_COSTS_BY_USER] {e}", exc_info=True)
        return JSONResponse(
            status_code=500, content={"success": False, "error": "Internal server error"}
        )


@router.get("/costs/timeseries")
async def costs_timeseries(
    range: str = Query("30d", description=_RANGE_DESC),
    start_date: str | None = Query(None, description=_START_DESC),
    end_date: str | None = Query(None, description=_END_DESC),
    user_id: str | None = None,
    admin: dict = Depends(get_current_dashboard_admin),
):
    try:
        resolved = _resolve_period(range, start_date, end_date)
        if isinstance(resolved, JSONResponse):
            return resolved
        start, end, key = resolved
        # Default timeseries window if 'all' is huge — keep 30d unless custom dates
        if key == "all" and start is None and not start_date and not end_date:
            start, end, key = admin_db.parse_range("30d")
        async with async_session_scope() as session:
            rows = await admin_db.cost_timeseries(session, start=start, end=end, user_id=user_id)
            return _ok(
                {
                    **_period_payload(key, start, end),
                    "user_id": user_id,
                    "series": rows,
                }
            )
    except Exception as e:
        logger.error(f"[ADMIN_COSTS_TIMESERIES] {e}", exc_info=True)
        return JSONResponse(
            status_code=500, content={"success": False, "error": "Internal server error"}
        )
