"""Database access for the dashboard bounded context.

Moved verbatim from ``src/db/async_db_functions.py`` during the R-STRUCT-1
migration. Function bodies are unchanged; only the import block was retargeted
at the new module paths.
"""

from datetime import datetime
from typing import Any, Dict, Optional

from sqlalchemy import func, or_, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import ReportStatus
from app.core.logging import setup_logging
from app.models import (
    Message, Report,
)

from app.reports.constants import DEFAULT_DOMAIN_INTERNAL

logger = setup_logging(__file__)

#: UI ordering for report category cards. Dashboard-only.
_CATEGORY_DISPLAY_ORDER = [
    "default",
    "primary_research",
    "due_diligence",
    "industry_benchmarking",
    "market_insight",
    "rfp",
    "business_plan",
]


async def get_dashboard_stats(user_id: str, session: AsyncSession) -> dict:
    """
    Get dashboard statistics for a user.
    
    Returns counts of reports/chats in different states:
    - total_draft: Count of chats that exist in messages table but NOT in reports table
    - analysis_completed: Count of reports with status ANALYSIS_COMPLETED
    - output_generated: Count of reports with status OUTPUT_GENERATED
    - updated: Static value 0 (TODO: implement when update functionality is enabled)
    
    Args:
        user_id (str): User ID
        session (AsyncSession): Database session
    
    Returns:
        dict: Statistics data with counts for each status
    """
    try:
        logger.info(f"Getting dashboard stats for user {user_id}")
        
        # Get count of draft chats (chats in messages table but NOT in reports table)
        draft_stmt = (
            select(func.count(Message.id))
            .outerjoin(Report, Message.id == Report.chat_id)
            .where(
                Message.user_id == user_id,
                ~Message.is_deleted,
                Report.id.is_(None)  # No corresponding report exists
            )
        )
        result = await session.execute(draft_stmt)
        draft_count = result.scalar() or 0
        
        logger.info(f"Found {draft_count} draft chats for user {user_id}")
        
        # Get count of analysis completed reports
        analysis_completed_stmt = (
            select(func.count(Report.id))
            .join(Message, Report.chat_id == Message.id)
            .where(
                Message.user_id == user_id,
                ~Message.is_deleted,
                Report.status == ReportStatus.ANALYSIS_COMPLETED.value
            )
        )
        result = await session.execute(analysis_completed_stmt)
        analysis_completed_count = result.scalar() or 0
        
        logger.info(f"Found {analysis_completed_count} analysis-completed reports for user {user_id}")
        
        # Get count of output generated reports
        output_generated_stmt = (
            select(func.count(Report.id))
            .join(Message, Report.chat_id == Message.id)
            .where(
                Message.user_id == user_id,
                ~Message.is_deleted,
                Report.status == ReportStatus.OUTPUT_GENERATED.value
            )
        )
        result = await session.execute(output_generated_stmt)
        output_generated_count = result.scalar() or 0
        
        logger.info(f"Found {output_generated_count} output-generated reports for user {user_id}")
        
        # TODO: Implement logic for updated reports when update functionality is enabled
        return {
            "success": True,
            "drafts": draft_count,
            "analysis_completed": analysis_completed_count,
            "output_generated": output_generated_count,
            "updates": 0  # TODO: implement when update functionality is enabled
        }
        
    except Exception as e:
        logger.error(f"Error getting dashboard stats: {str(e)}", exc_info=True)
        return {
            "success": False,
            "error": f"Error getting dashboard stats: {str(e)}"
        }
def list_all_categories() -> Dict[str, Any]:
    """Return the static catalog of report categories.

    This is intentionally not user-scoped: every authenticated user sees the
    same set of categories on the homepage. ``default`` is surfaced as
    ``standard`` in the response.
    """
    return {
        "success": True,
        "categories": [
            {
                "slug": _to_external_domain(slug),
                "display_name": DOMAIN_DISPLAY_NAMES.get(
                    slug, slug.replace("_", " ").title()
                ),
            }
            for slug in _CATEGORY_DISPLAY_ORDER
        ],
    }
def _latest_report_per_chat_subq():
    """Build a subquery that selects the latest report per chat.

    Returned columns:
        chat_id, report_id, report_title, status, domain_name, poster_image_url

    Uses a window function so PostgreSQL can resolve it in a single pass.
    Defined here so both home-search and ongoing-chats stay aligned on what
    "the chat's latest report" means.
    """
    rn = func.row_number().over(
        partition_by=Report.chat_id,
        order_by=Report.created_at.desc(),
    ).label("rn")

    inner = (
        select(
            Report.chat_id.label("chat_id"),
            Report.id.label("report_id"),
            Report.title.label("report_title"),
            Report.status.label("status"),
            Report.domain_name.label("domain_name"),
            Report.poster_image_url.label("poster_image_url"),
            rn,
        )
    ).subquery()

    return (
        select(
            inner.c.chat_id,
            inner.c.report_id,
            inner.c.report_title,
            inner.c.status,
            inner.c.domain_name,
            inner.c.poster_image_url,
        )
        .where(inner.c.rn == 1)
        .subquery()
    )
async def search_chats_and_reports(
    user_id: str,
    session: AsyncSession,
    query: Optional[str] = None,
    domains: Optional[list] = None,
    statuses: Optional[list] = None,
    created_after: Optional[datetime] = None,
    created_before: Optional[datetime] = None,
    limit: int = 20,
    offset: int = 0,
) -> Dict[str, Any]:
    """Search the user's chats + latest reports for the homepage search bar.

    A hit is one row per chat. ``query`` matches case-insensitively against
    the chat title and (if present) the latest report's title. Chats without
    a report have an effective status of ``draft`` and an effective domain
    of ``default`` (returned as ``standard``).

    Args:
        user_id: Authenticated user's ID.
        session: SQLAlchemy async session.
        query: Optional case-insensitive substring to match titles against.
        domains: Optional list of external domain slugs to filter by; pass
            ``"standard"`` to match the internal ``default``/NULL domain.
        statuses: Optional list of report status values to filter by; pass
            ``"draft"`` to include chats with no report.
        created_after: Lower bound (inclusive) on chat ``created_at``.
        created_before: Upper bound (inclusive) on chat ``created_at``.
        limit: Page size (1–100).
        offset: Pagination offset.

    Returns:
        Dict with ``success``, ``total``, ``limit``, ``offset``, ``items``.
    """
    logger.info(
        f"Home-search for user_id={user_id} q={query!r} domains={domains} "
        f"statuses={statuses} after={created_after} before={created_before} "
        f"limit={limit} offset={offset}"
    )

    try:
        latest = _latest_report_per_chat_subq()

        # Effective status / domain treat a chat with no report as draft/default
        effective_status = func.coalesce(latest.c.status, ReportStatus.DRAFT.value)
        effective_domain = func.coalesce(latest.c.domain_name, DEFAULT_DOMAIN_INTERNAL)

        where_clauses = [
            Message.user_id == user_id,
            Message.is_deleted.is_(False),
        ]

        if query:
            q_like = f"%{query.strip().lower()}%"
            where_clauses.append(
                or_(
                    func.lower(Message.chat_title).like(q_like),
                    func.lower(latest.c.report_title).like(q_like),
                )
            )

        if domains:
            internal_domains = [_to_internal_domain(d) for d in domains]
            where_clauses.append(effective_domain.in_(internal_domains))

        if statuses:
            where_clauses.append(effective_status.in_(statuses))

        if created_after is not None:
            where_clauses.append(Message.created_at >= created_after)
        if created_before is not None:
            where_clauses.append(Message.created_at <= created_before)

        base_query = (
            select(
                Message.id.label("chat_id"),
                Message.chat_title.label("chat_title"),
                Message.created_at.label("created_at"),
                Message.updated_at.label("updated_at"),
                latest.c.report_id,
                latest.c.report_title,
                latest.c.poster_image_url,
                effective_status.label("eff_status"),
                effective_domain.label("eff_domain"),
            )
            .select_from(
                Message.__table__.outerjoin(latest, latest.c.chat_id == Message.id)
            )
            .where(*where_clauses)
        )

        # Total count (same filters, no limit/offset)
        count_stmt = select(func.count()).select_from(base_query.subquery())
        total = (await session.execute(count_stmt)).scalar() or 0

        # Paginated rows
        rows_stmt = (
            base_query.order_by(Message.updated_at.desc().nullslast())
            .limit(limit)
            .offset(offset)
        )
        rows = (await session.execute(rows_stmt)).all()

        q_lower = query.strip().lower() if query else None
        items = []
        for r in rows:
            matched_chat = bool(
                q_lower
                and r.chat_title
                and q_lower in r.chat_title.lower()
            )
            matched_report = bool(
                q_lower
                and r.report_title
                and q_lower in r.report_title.lower()
            )
            if matched_chat and matched_report:
                matched_on = "both"
            elif matched_report:
                matched_on = "report_title"
            else:
                matched_on = "chat_title"

            items.append(
                {
                    "chat_id": r.chat_id,
                    "chat_title": r.chat_title,
                    "report_id": r.report_id,
                    "report_title": r.report_title,
                    "status": r.eff_status,
                    "domain": _to_external_domain(r.eff_domain),
                    "poster_image_url": r.poster_image_url,
                    "created_at": r.created_at.isoformat() if r.created_at else None,
                    "updated_at": r.updated_at.isoformat() if r.updated_at else None,
                    "matched_on": matched_on,
                }
            )

        logger.info(
            f"Home-search returning {len(items)} of {total} hits for user_id={user_id}"
        )
        return {
            "success": True,
            "total": total,
            "limit": limit,
            "offset": offset,
            "items": items,
        }

    except SQLAlchemyError as e:
        logger.error(f"DB error in search_chats_and_reports: {str(e)}", exc_info=True)
        return {"success": False, "error": f"Database error: {str(e)}"}
    except Exception as e:
        logger.error(f"Unexpected error in search_chats_and_reports: {str(e)}", exc_info=True)
        return {"success": False, "error": f"Unexpected error: {str(e)}"}
