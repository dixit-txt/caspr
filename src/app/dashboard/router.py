"""HTTP routes for the dashboard bounded context.

Handlers moved verbatim from ``src/resources/routers/api.py`` during the
R-STRUCT-1 migration, which split that 10,257-line module across seven
contexts. Handler bodies are unchanged; only the import block and the
``APIRouter`` they attach to are new.
"""

"""api.py: Authentication API for the Casper backend"""
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse

from app.auth.schemas import ErrorResponse
from app.auth.token import get_current_active_user
from app.core.db import async_session_scope
from app.core.logging import setup_logging
from app.dashboard.repository import (
    get_dashboard_stats,
    list_all_categories,
    search_chats_and_reports,
)
from app.dashboard.schemas import (
    CategoriesResponse,
    CategoryItem,
    DashboardInfoResponse,
    DashboardStatsResponse,
    HomeSearchItem,
    HomeSearchResponse,
    UserLogsRequest,
)
from app.observability.cloudwatch_utils import insert_cloudwatch_logs

router = APIRouter()
logger = setup_logging(__file__)


@router.post(
    "/user-logs",
    status_code=200,
    responses={
        401: {"model": ErrorResponse},
        422: {"model": ErrorResponse},
        500: {"model": ErrorResponse},
    },
)
async def user_logs(data: UserLogsRequest, user_id: str = Depends(get_current_active_user)):
    """User logs endpoint"""
    logger.info(f"User logs request received for user_id: {user_id}")

    if data.type not in ["download-report"]:
        return JSONResponse(status_code=400, content={"success": False, "error": "Invalid request"})

    cloudwatch_data = {}

    if data.type == "download-report":
        if not data.logs_data.get("chat_id") or not data.logs_data.get("chat_title"):
            return JSONResponse(
                status_code=400, content={"success": False, "error": "Invalid request"}
            )

        chat_id = data.logs_data.get("chat_id")
        chat_title = data.logs_data.get("chat_title")

        cloudwatch_data = {
            "event": "Report downloaded",
            "event_success": data.status,
            "timestamp": datetime.now(UTC).isoformat(),
            "chat_id": chat_id,
            "chat_title": chat_title,
        }

    if not cloudwatch_data:
        return JSONResponse(status_code=400, content={"success": False, "error": "Invalid request"})

    try:
        response = await insert_cloudwatch_logs(data=cloudwatch_data, user_id=user_id)
    except Exception as e:
        logger.error(f"Error in inserting cloudwatch logs for user_id: {user_id}: {e}")
        return JSONResponse(
            status_code=500, content={"success": False, "error": "Internal server error"}
        )

    if not response.get("success", False):
        return JSONResponse(
            status_code=500, content={"success": False, "error": "Internal server error"}
        )

    return JSONResponse(status_code=200, content={"success": True})


@router.get(
    "/dashboard-stats",
    response_model=DashboardStatsResponse,
    status_code=200,
    responses={401: {"model": ErrorResponse}, 500: {"model": ErrorResponse}},
)
async def get_dashboard_stats_endpoint(user_id: str = Depends(get_current_active_user)):
    """
    Get dashboard statistics for the current user.

    Returns counts of reports/chats in different states:
    - **drafts**: Number of draft chats (chats that exist in messages table but NOT in reports table)
    - **analysis_completed**: Number of reports with analysis completed status
    - **output_generated**: Number of reports with output generated status
    - **updated**: Number of updated reports (TODO: implement when update functionality is enabled)

    Authentication Required: Yes (JWT token)
    """
    logger.info(f"Dashboard stats request received for user: {user_id}")

    try:
        async with async_session_scope() as session:
            # Get dashboard statistics
            result = await get_dashboard_stats(user_id=user_id, session=session)

            if not result.get("success"):
                logger.error(f"Failed to get dashboard stats: {result.get('error')}")
                return JSONResponse(
                    status_code=500,
                    content={"success": False, "error": "Failed to retrieve dashboard statistics"},
                )

            logger.info(f"Successfully retrieved dashboard stats for user {user_id}")
            return JSONResponse(
                status_code=200,
                content={
                    "drafts": result.get("drafts", 0),
                    "analysis_completed": result.get("analysis_completed", 0),
                    "output_generated": result.get("output_generated", 0),
                    "updates": result.get(
                        "updates", 0
                    ),  # TODO: implement when update functionality is enabled
                },
            )

    except Exception as e:
        logger.error(f"Error in get_dashboard_stats_endpoint: {e}", exc_info=True)
        return JSONResponse(
            status_code=500, content={"success": False, "error": "Internal server error"}
        )


@router.get(
    "/dashboard-info",
    response_model=DashboardInfoResponse,
    status_code=200,
    responses={401: {"model": ErrorResponse}},
)
async def get_dashboard_info_endpoint(user_id: str = Depends(get_current_active_user)):
    """
    Get dashboard information including general facts and system info.

    Returns:
    - **general_facts**: List of helpful tips and facts about Caspr features
    - **caspr_info**: System information like live feeds data count

    **TODO:**
    - Replace static general_facts with dynamic content from database
    - Implement real live_feeds_data count from database
    - Add more system metrics as needed

    **Note:** Currently returns static dummy data for frontend development.

    Authentication Required: Yes (JWT token)
    """
    logger.info(f"Dashboard info request received for user: {user_id}")

    try:
        # TODO: Fetch this data from database instead of static values
        # For now, returning static data as requested
        dashboard_info = {
            "general_facts": [
                {
                    "title": "Caspr. can generate quick presentations on the reports you create",
                    "subtitle": "Generate presentation option is available to you as soon as a PDF report is generated. You even get a completely editable PPTX file to make changes on your own.",
                }
            ],
            "caspr_info": [
                {
                    "live_feeds_data": 536831  # TODO: Get real count from database
                }
            ],
        }

        logger.info(f"Successfully retrieved dashboard info for user {user_id}")
        return JSONResponse(status_code=200, content=dashboard_info)

    except Exception as e:
        logger.error(f"Error in get_dashboard_info_endpoint: {e}", exc_info=True)
        return JSONResponse(
            status_code=500, content={"success": False, "error": "Internal server error"}
        )


@router.get(
    "/categories",
    response_model=CategoriesResponse,
    status_code=200,
    responses={401: {"model": ErrorResponse}, 500: {"model": ErrorResponse}},
)
async def get_categories(user_id: str = Depends(get_current_active_user)):
    """Return the static catalog of all report categories.

    Returns the full set of categories regardless of whether the user has
    any reports in them. The internal ``default`` domain is surfaced as
    ``standard``; pass that slug back as-is on subsequent calls (the API
    will translate it internally where needed).

    Authentication Required: Yes (JWT token)
    """
    logger.info(f"Get categories request received for user_id: {user_id}")
    try:
        result = list_all_categories()
        if not result.get("success"):
            logger.error(f"Failed to build category catalog for user_id: {user_id}")
            return JSONResponse(
                status_code=500,
                content={"success": False, "error": "Failed to load categories. Please try again."},
            )
        return CategoriesResponse(
            success=True,
            categories=[CategoryItem(**c) for c in result["categories"]],
        )
    except Exception as e:
        logger.error(f"Error in get_categories: {e}", exc_info=True)
        return JSONResponse(
            status_code=500,
            content={"success": False, "error": "Internal server error"},
        )


def _parse_csv_param(value: str | None) -> list | None:
    """Split a comma-separated query param into a clean list; ``None`` if empty."""
    if not value:
        return None
    parts = [p.strip() for p in value.split(",") if p.strip()]
    return parts or None


def _parse_iso_datetime(raw: str | None, field_name: str) -> datetime | None:
    """Parse an ISO-8601 datetime; raise ValueError with a clean message on bad input."""
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(
            f"{field_name} must be an ISO-8601 datetime (e.g. 2026-01-15T00:00:00Z)"
        ) from exc


@router.get(
    "/home-search",
    response_model=HomeSearchResponse,
    status_code=200,
    responses={
        400: {"model": ErrorResponse},
        401: {"model": ErrorResponse},
        500: {"model": ErrorResponse},
    },
)
async def home_search(
    q: str | None = Query(
        None,
        description="Case-insensitive substring matched against chat title and latest report title",
        max_length=200,
    ),
    domain: str | None = Query(
        None,
        description="Comma-separated category slugs to filter by (e.g. 'standard,due_diligence')",
        max_length=200,
    ),
    status: str | None = Query(
        None,
        description="Comma-separated report status values to filter by (e.g. 'draft,analysis-completed')",
        max_length=200,
    ),
    created_after: str | None = Query(
        None,
        description="ISO-8601 datetime; only chats created at/after this time are returned",
    ),
    created_before: str | None = Query(
        None,
        description="ISO-8601 datetime; only chats created at/before this time are returned",
    ),
    limit: int = Query(20, ge=1, le=100, description="Page size"),
    offset: int = Query(0, ge=0, description="Pagination offset"),
    user_id: str = Depends(get_current_active_user),
):
    """Homepage search across the authenticated user's chats and reports.

    Each result represents a chat (one row per chat); when the chat has a
    generated report, the latest report's title/poster are surfaced too.
    The search query (``q``) matches case-insensitively against both
    ``chat_title`` and the latest report's ``title``. The ``matched_on``
    field on each item indicates which field actually matched.

    All filters are additive (AND). Chats with no report are treated as
    having status ``draft`` and domain ``standard``.

    Authentication Required: Yes (JWT token)
    """
    logger.info(
        f"Home-search request received for user_id={user_id} q={q!r} "
        f"domain={domain} status={status} created_after={created_after} "
        f"created_before={created_before} limit={limit} offset={offset}"
    )

    try:
        domains_list = _parse_csv_param(domain)
        statuses_list = _parse_csv_param(status)
        created_after_dt = _parse_iso_datetime(created_after, "created_after")
        created_before_dt = _parse_iso_datetime(created_before, "created_before")
    except ValueError as exc:
        logger.warning(f"home-search bad date input: {exc}")
        return JSONResponse(
            status_code=400,
            content={"success": False, "error": str(exc)},
        )

    if (
        created_after_dt is not None
        and created_before_dt is not None
        and created_after_dt > created_before_dt
    ):
        return JSONResponse(
            status_code=400,
            content={
                "success": False,
                "error": "created_after must be earlier than or equal to created_before",
            },
        )

    try:
        async with async_session_scope() as session:
            result = await search_chats_and_reports(
                user_id=user_id,
                session=session,
                query=q,
                domains=domains_list,
                statuses=statuses_list,
                created_after=created_after_dt,
                created_before=created_before_dt,
                limit=limit,
                offset=offset,
            )

            if not result.get("success"):
                logger.error(f"home-search DB failure for user_id={user_id}: {result.get('error')}")
                return JSONResponse(
                    status_code=500,
                    content={"success": False, "error": "Failed to run search. Please try again."},
                )

            return HomeSearchResponse(
                success=True,
                total=result["total"],
                limit=result["limit"],
                offset=result["offset"],
                items=[HomeSearchItem(**item) for item in result["items"]],
            )

    except Exception as e:
        logger.error(f"Error in home_search: {e}", exc_info=True)
        return JSONResponse(
            status_code=500,
            content={"success": False, "error": "Internal server error"},
        )
