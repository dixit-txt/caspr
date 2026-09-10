"""reports routes: domains.

Split out of ``app/reports/router.py`` to keep each router file under
the ~400-line ceiling in R-STRUCT-3. Handlers are unchanged.
"""

"""HTTP routes for the reports bounded context.

Handlers moved verbatim from ``src/resources/routers/api.py`` during the
R-STRUCT-1 migration, which split that 10,257-line module across seven
contexts. Handler bodies are unchanged; only the import block and the
``APIRouter`` they attach to are new.
"""

"""api.py: Authentication API for the Casper backend"""

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse

from app.auth.schemas import ErrorResponse
from app.auth.token import get_current_active_user
from app.core.constants import (
    _VALID_DOMAIN_SLUGS,
)
from app.core.db import async_session_scope
from app.core.logging import setup_logging
from app.core.redis import get_redis_instance
from app.dashboard.schemas import (
    DomainReportsResponse,
    ReportDomainsResponse,
)
from app.reports.repository import (
    get_report_domain_summary,
    get_reports_by_domain,
)

router = APIRouter()
logger = setup_logging(__file__)
redis_instance = get_redis_instance()


@router.get(
    "/report-domains",
    response_model=ReportDomainsResponse,
    status_code=200,
    responses={
        401: {"model": ErrorResponse},
        500: {"model": ErrorResponse},
    },
)
async def get_report_domains(user_id: str = Depends(get_current_active_user)):
    """Return a summary of report domains for the authenticated user.

    Only domains that contain at least one non-draft report are included.
    Results are sorted by ``item_count`` descending.

    **Response fields per domain:**
    - ``domain_name``: Internal slug (e.g. ``"due_diligence"``)
    - ``category_name``: Human-readable label (e.g. ``"Due Diligence"``)
    - ``item_count``: Number of non-draft reports in that domain

    Authentication Required: Yes (JWT token)
    """
    logger.info(f"Get report-domains request received for user_id: {user_id}")

    try:
        async with async_session_scope() as session:
            result = await get_report_domain_summary(user_id=user_id, session=session)

            if not result.get("success"):
                logger.error(
                    f"Failed to fetch report domain summary for user_id: {user_id}: "
                    f"{result.get('error')}"
                )
                return JSONResponse(
                    status_code=500,
                    content={
                        "success": False,
                        "error": "Failed to retrieve report domains. Please try again.",
                    },
                )

            logger.info(
                f"Successfully retrieved report domains for user_id: {user_id}, "
                f"domain_count: {len(result.get('domains', []))}"
            )
            return ReportDomainsResponse(success=True, domains=result["domains"])

    except Exception as e:
        logger.error(f"Error in get_report_domains: {e}", exc_info=True)
        return JSONResponse(
            status_code=500,
            content={"success": False, "error": "Internal server error"},
        )


@router.get(
    "/report-domains/{domain_name}/reports",
    response_model=DomainReportsResponse,
    status_code=200,
    responses={
        400: {"model": ErrorResponse},
        401: {"model": ErrorResponse},
        500: {"model": ErrorResponse},
    },
)
async def get_domain_reports(
    domain_name: str,
    limit: int = Query(..., ge=1, le=100, description="Number of reports per page"),
    offset: int = Query(..., ge=0, description="Number of reports to skip"),
    user_id: str = Depends(get_current_active_user),
):
    """Return paginated non-draft reports for a specific domain for the authenticated user.

    Reports are sorted by ``last_activity_at`` descending (most recent first).

    **Path parameter:**
    - ``domain_name``: One of ``default``, ``primary_research``, ``due_diligence``,
      ``industry_benchmarking``, ``market_insight``, ``rfp``, ``business_plan``

    **Query parameters (required):**
    - ``limit`` (int, 1–100): Page size.
    - ``offset`` (int, ≥ 0): Pagination offset.

    **Response fields:**
    - ``total``: Total matching reports (use with ``limit``/``offset`` for client-side pagination)
    - ``reports``: Paginated list of report records

    Authentication Required: Yes (JWT token)
    """
    logger.info(
        f"Get domain-reports request received for user_id: {user_id}, "
        f"domain_name: {domain_name}, limit: {limit}, offset: {offset}"
    )

    if domain_name not in _VALID_DOMAIN_SLUGS:
        logger.warning(f"Invalid domain_name received: {domain_name}")
        return JSONResponse(
            status_code=400,
            content={
                "success": False,
                "error": (
                    f"Invalid domain '{domain_name}'. Must be one of: "
                    + ", ".join(sorted(_VALID_DOMAIN_SLUGS))
                ),
            },
        )

    try:
        async with async_session_scope() as session:
            result = await get_reports_by_domain(
                user_id=user_id,
                domain_name=domain_name,
                limit=limit,
                offset=offset,
                session=session,
            )

            if not result.get("success"):
                logger.error(
                    f"Failed to fetch reports for user_id: {user_id}, "
                    f"domain_name: {domain_name}: {result.get('error')}"
                )
                return JSONResponse(
                    status_code=500,
                    content={
                        "success": False,
                        "error": "Failed to retrieve reports. Please try again.",
                    },
                )

            logger.info(
                f"Successfully retrieved {len(result.get('reports', []))} of "
                f"{result.get('total', 0)} reports for user_id: {user_id}, "
                f"domain_name: {domain_name}"
            )
            return DomainReportsResponse(
                success=True,
                domain_name=result["domain_name"],
                category_name=result["category_name"],
                total=result["total"],
                limit=result["limit"],
                offset=result["offset"],
                reports=result["reports"],
            )

    except Exception as e:
        logger.error(f"Error in get_domain_reports: {e}", exc_info=True)
        return JSONResponse(
            status_code=500,
            content={"success": False, "error": "Internal server error"},
        )
