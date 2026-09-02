"""admin_db.py: Query helpers for the CEO/cofounder admin dashboard.

Separation of concerns (important):
- USER TRACKING  → users, messages, reports, report_versions + subscription
                   (current_tier / status only from Subscription — no wallet
                   balances / token transactions)
- COST TRACKING  → costtracker only (tokens, estimated_cost, model, context)
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.observability.functionality_context import Functionality, label_for, normalize_functionality, remap_feature_label
from src.db.database import CostTracker, Message, Report, ReportVersion, User
from src.db.wallet_functions import get_active_subscription


def _functionality_bucket_expr():
    """SQL expression: coalesce + remap websearch aliases → learning_brain.

    Admins must never see ``websearch`` / ``web_search`` as a functionality —
    those are shown as Learning Brain in every dashboard response.
    """
    raw = func.lower(func.coalesce(CostTracker.functionality, Functionality.OTHER.value))
    return case(
        (
            raw.in_(("websearch", "web_search", "web search")),
            Functionality.LEARNING_BRAIN.value,
        ),
        else_=func.coalesce(CostTracker.functionality, Functionality.OTHER.value),
    )


def _parse_datetime(value: Optional[str], *, end_of_day: bool = False) -> Optional[datetime]:
    """Parse ISO date (YYYY-MM-DD) or datetime string into UTC-aware datetime."""
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    # Allow trailing Z
    raw = raw.replace("Z", "+00:00")
    try:
        if len(raw) == 10 and raw[4] == "-" and raw[7] == "-":
            dt = datetime.fromisoformat(raw)
            if end_of_day:
                dt = dt.replace(hour=23, minute=59, second=59, microsecond=999999)
            else:
                dt = dt.replace(hour=0, minute=0, second=0, microsecond=0)
        else:
            dt = datetime.fromisoformat(raw)
    except ValueError as e:
        raise ValueError(f"Invalid date/datetime: {value}") from e
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt


def resolve_period(
    range_key: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> Tuple[Optional[datetime], datetime, str]:
    """Resolve a flexible time window.

    Priority:
      1. Explicit ``start_date`` / ``end_date`` (any day or datetime the admin picks)
      2. Preset ``range``: today | 2d | 7d | 30d | all
      3. Default: last 2 days

    Returns (start_utc|None, end_utc, label).
    ``start`` is None only for ``all`` (no lower bound).
    """
    now = datetime.now(timezone.utc)

    if start_date or end_date:
        start = _parse_datetime(start_date, end_of_day=False) if start_date else None
        end = _parse_datetime(end_date, end_of_day=True) if end_date else now
        if start is not None and end < start:
            raise ValueError("end_date must be >= start_date")
        if start is None and end_date and not start_date:
            # Only end provided → treat as single-day window ending that day
            start = end.replace(hour=0, minute=0, second=0, microsecond=0)
        if start is None and start_date is None and end_date is None:
            start = now - timedelta(days=2)
        label_start = start.date().isoformat() if start else "…"
        label_end = end.date().isoformat()
        return start, end, f"custom:{label_start}:{label_end}"

    return parse_range(range_key or "2d")


def parse_range(range_key: str) -> Tuple[Optional[datetime], datetime, str]:
    """Return (start_utc|None, end_utc, normalized_key) for preset ranges.

    Presets: today | 2d | 7d | 30d | all
    Prefer :func:`resolve_period` when callers also accept custom dates.
    """
    now = datetime.now(timezone.utc)
    key = (range_key or "2d").strip().lower()
    if key in ("today", "1d", "day"):
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return start, now, "today"
    if key in ("2d", "2day", "2days"):
        return now - timedelta(days=2), now, "2d"
    if key in ("7d", "7day", "7days", "week"):
        return now - timedelta(days=7), now, "7d"
    if key in ("30d", "30day", "30days", "month"):
        return now - timedelta(days=30), now, "30d"
    if key in ("all", "alltime", "all-time"):
        return None, now, "all"
    return now - timedelta(days=2), now, "2d"


def _cost_time_filter(start: Optional[datetime], end: datetime):
    clauses = [CostTracker.timestamp <= end]
    if start is not None:
        clauses.append(CostTracker.timestamp >= start)
    return and_(*clauses)


async def get_subscription_snapshot(session: AsyncSession, user_id: str) -> Dict[str, Any]:
    """Current plan only — tier + status. No wallet balances.

    ``subscriptions.user_id`` is NOT unique (payment history / pending rows).
    Use the same resolution as the wallet layer: prefer a row with an active
    charged interval, else the newest subscription row.
    """
    result = await get_active_subscription(session, user_id=user_id)
    if not result.get("success"):
        return {"current_tier": "free", "status": None}
    sub = result.get("subscription")
    if not sub:
        return {"current_tier": "free", "status": None}
    tier = sub.get("current_tier") or "free"
    if hasattr(tier, "value"):
        tier = tier.value
    status = sub.get("status")
    if hasattr(status, "value"):
        status = status.value
    return {"current_tier": str(tier).lower(), "status": status}


async def get_subscription_snapshots(
    session: AsyncSession,
    user_ids: List[str],
) -> Dict[str, Dict[str, Any]]:
    """Batch subscription snapshots keyed by user_id."""
    out: Dict[str, Dict[str, Any]] = {}
    for uid in user_ids:
        if not uid or uid in out:
            continue
        out[uid] = await get_subscription_snapshot(session, uid)
    return out


# ---------------------------------------------------------------------------
# COST TRACKING — costtracker only
# ---------------------------------------------------------------------------

async def cost_summary(
    session: AsyncSession,
    *,
    start: Optional[datetime],
    end: datetime,
    user_id: Optional[str] = None,
) -> Dict[str, Any]:
    filters = [_cost_time_filter(start, end)]
    if user_id:
        filters.append(CostTracker.user_id == user_id)

    stmt = select(
        func.coalesce(func.sum(CostTracker.estimated_cost), 0.0).label("spend"),
        func.coalesce(func.sum(CostTracker.input_tokens), 0).label("input_tokens"),
        func.coalesce(func.sum(CostTracker.output_tokens), 0).label("output_tokens"),
        func.count(CostTracker.id).label("calls"),
        func.count(func.distinct(CostTracker.user_id)).label("users"),
        func.count(func.distinct(CostTracker.chat_id)).label("chats"),
    ).where(and_(*filters))
    row = (await session.execute(stmt)).one()

    # Today + all-time always useful on overview cards
    today_start = end.replace(hour=0, minute=0, second=0, microsecond=0)
    today_filters = [CostTracker.timestamp >= today_start, CostTracker.timestamp <= end]
    all_filters: List[Any] = []
    if user_id:
        today_filters.append(CostTracker.user_id == user_id)
        all_filters.append(CostTracker.user_id == user_id)

    today_stmt = select(
        func.coalesce(func.sum(CostTracker.estimated_cost), 0.0)
    ).where(and_(*today_filters))
    all_stmt = select(func.coalesce(func.sum(CostTracker.estimated_cost), 0.0))
    if all_filters:
        all_stmt = all_stmt.where(and_(*all_filters))

    spend_today = float((await session.execute(today_stmt)).scalar() or 0)
    spend_all_time = float((await session.execute(all_stmt)).scalar() or 0)

    return {
        "spend_usd": float(row.spend or 0),
        "spend_today_usd": spend_today,
        "spend_all_time_usd": spend_all_time,
        "input_tokens": int(row.input_tokens or 0),
        "output_tokens": int(row.output_tokens or 0),
        "calls": int(row.calls or 0),
        "distinct_users": int(row.users or 0),
        "distinct_chats": int(row.chats or 0),
    }


async def cost_by_feature(
    session: AsyncSession,
    *,
    start: Optional[datetime],
    end: datetime,
    user_id: Optional[str] = None,
    limit: int = 50,
) -> List[Dict[str, Any]]:
    """Spend breakdown by costtracker.context (+ agent_name). Raw table labels only."""
    filters = [_cost_time_filter(start, end)]
    if user_id:
        filters.append(CostTracker.user_id == user_id)

    stmt = (
        select(
            CostTracker.context,
            CostTracker.agent_name,
            func.coalesce(func.sum(CostTracker.estimated_cost), 0.0).label("spend"),
            func.coalesce(func.sum(CostTracker.input_tokens), 0).label("input_tokens"),
            func.coalesce(func.sum(CostTracker.output_tokens), 0).label("output_tokens"),
            func.count(CostTracker.id).label("calls"),
            func.count(func.distinct(CostTracker.user_id)).label("users"),
        )
        .where(and_(*filters))
        .group_by(CostTracker.context, CostTracker.agent_name)
        .order_by(func.coalesce(func.sum(CostTracker.estimated_cost), 0.0).desc())
        .limit(limit)
    )
    rows = (await session.execute(stmt)).all()
    return [
        {
            "context": remap_feature_label(r.context),
            "agent_name": remap_feature_label(r.agent_name),
            "spend_usd": float(r.spend or 0),
            "input_tokens": int(r.input_tokens or 0),
            "output_tokens": int(r.output_tokens or 0),
            "calls": int(r.calls or 0),
            "distinct_users": int(r.users or 0),
        }
        for r in rows
    ]


async def cost_by_functionality(
    session: AsyncSession,
    *,
    start: Optional[datetime],
    end: datetime,
    user_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Spend grouped by the stable, user-facing ``functionality`` bucket.

    This is the primary "what are we spending on?" view for non-technical
    readers (chat / report generation / refine / refine visualization /
    executive summary / infographic / PPTX generation / Ask CASPR /
    Learning Brain). Rows with a NULL functionality are surfaced as ``other``.
    ``websearch`` / ``web_search`` are remapped to ``learning_brain``.
    """
    filters = [_cost_time_filter(start, end)]
    if user_id:
        filters.append(CostTracker.user_id == user_id)

    bucket = _functionality_bucket_expr()
    stmt = (
        select(
            bucket.label("functionality"),
            func.coalesce(func.sum(CostTracker.estimated_cost), 0.0).label("spend"),
            func.coalesce(func.sum(CostTracker.input_tokens), 0).label("input_tokens"),
            func.coalesce(func.sum(CostTracker.output_tokens), 0).label("output_tokens"),
            func.count(CostTracker.id).label("calls"),
            func.count(func.distinct(CostTracker.user_id)).label("users"),
            func.count(func.distinct(CostTracker.chat_id)).label("chats"),
        )
        .where(and_(*filters))
        .group_by(bucket)
        .order_by(func.coalesce(func.sum(CostTracker.estimated_cost), 0.0).desc())
    )
    rows = (await session.execute(stmt)).all()
    return [
        {
            "functionality": normalize_functionality(r.functionality),
            "label": label_for(r.functionality),
            "spend_usd": float(r.spend or 0),
            "input_tokens": int(r.input_tokens or 0),
            "output_tokens": int(r.output_tokens or 0),
            "calls": int(r.calls or 0),
            "distinct_users": int(r.users or 0),
            "distinct_chats": int(r.chats or 0),
        }
        for r in rows
    ]


async def cost_by_model(
    session: AsyncSession,
    *,
    start: Optional[datetime],
    end: datetime,
    user_id: Optional[str] = None,
    limit: int = 30,
) -> List[Dict[str, Any]]:
    filters = [_cost_time_filter(start, end)]
    if user_id:
        filters.append(CostTracker.user_id == user_id)

    stmt = (
        select(
            CostTracker.model_name,
            func.coalesce(func.sum(CostTracker.estimated_cost), 0.0).label("spend"),
            func.coalesce(func.sum(CostTracker.input_tokens), 0).label("input_tokens"),
            func.coalesce(func.sum(CostTracker.output_tokens), 0).label("output_tokens"),
            func.count(CostTracker.id).label("calls"),
        )
        .where(and_(*filters))
        .group_by(CostTracker.model_name)
        .order_by(func.coalesce(func.sum(CostTracker.estimated_cost), 0.0).desc())
        .limit(limit)
    )
    rows = (await session.execute(stmt)).all()
    return [
        {
            "model_name": r.model_name,
            "spend_usd": float(r.spend or 0),
            "input_tokens": int(r.input_tokens or 0),
            "output_tokens": int(r.output_tokens or 0),
            "calls": int(r.calls or 0),
        }
        for r in rows
    ]


async def cost_by_user(
    session: AsyncSession,
    *,
    start: Optional[datetime],
    end: datetime,
    limit: int = 50,
) -> List[Dict[str, Any]]:
    filters = [_cost_time_filter(start, end)]
    stmt = (
        select(
            CostTracker.user_id,
            func.coalesce(func.sum(CostTracker.estimated_cost), 0.0).label("spend"),
            func.coalesce(func.sum(CostTracker.input_tokens), 0).label("input_tokens"),
            func.coalesce(func.sum(CostTracker.output_tokens), 0).label("output_tokens"),
            func.count(CostTracker.id).label("calls"),
            func.count(func.distinct(CostTracker.chat_id)).label("chats"),
        )
        .where(and_(*filters))
        .group_by(CostTracker.user_id)
        .order_by(func.coalesce(func.sum(CostTracker.estimated_cost), 0.0).desc())
        .limit(limit)
    )
    rows = (await session.execute(stmt)).all()
    user_ids = [r.user_id for r in rows if r.user_id]
    users_map: Dict[str, User] = {}
    if user_ids:
        urows = (
            await session.execute(select(User).where(User.id.in_(user_ids)))
        ).scalars().all()
        users_map = {u.id: u for u in urows}

    out = []
    for r in rows:
        u = users_map.get(r.user_id)
        out.append({
            "user_id": r.user_id,
            "email": u.email if u else None,
            "user_name": u.user_name if u else None,
            "spend_usd": float(r.spend or 0),
            "input_tokens": int(r.input_tokens or 0),
            "output_tokens": int(r.output_tokens or 0),
            "calls": int(r.calls or 0),
            "distinct_chats": int(r.chats or 0),
        })
    return out


async def cost_timeseries(
    session: AsyncSession,
    *,
    start: Optional[datetime],
    end: datetime,
    user_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    filters = [_cost_time_filter(start, end)]
    if user_id:
        filters.append(CostTracker.user_id == user_id)

    day = func.date_trunc("day", CostTracker.timestamp)
    stmt = (
        select(
            day.label("day"),
            func.coalesce(func.sum(CostTracker.estimated_cost), 0.0).label("spend"),
            func.coalesce(func.sum(CostTracker.input_tokens), 0).label("input_tokens"),
            func.coalesce(func.sum(CostTracker.output_tokens), 0).label("output_tokens"),
            func.count(CostTracker.id).label("calls"),
            func.count(func.distinct(CostTracker.user_id)).label("active_users"),
        )
        .where(and_(*filters))
        .group_by(day)
        .order_by(day.asc())
    )
    rows = (await session.execute(stmt)).all()
    return [
        {
            "day": r.day.isoformat() if r.day else None,
            "spend_usd": float(r.spend or 0),
            "input_tokens": int(r.input_tokens or 0),
            "output_tokens": int(r.output_tokens or 0),
            "calls": int(r.calls or 0),
            "active_users": int(r.active_users or 0),
        }
        for r in rows
    ]


async def cost_for_chat(
    session: AsyncSession,
    *,
    chat_id: str,
    start: Optional[datetime] = None,
    end: Optional[datetime] = None,
) -> Dict[str, Any]:
    end = end or datetime.now(timezone.utc)
    filters = [CostTracker.chat_id == chat_id, CostTracker.timestamp <= end]
    if start is not None:
        filters.append(CostTracker.timestamp >= start)

    summary_stmt = select(
        func.coalesce(func.sum(CostTracker.estimated_cost), 0.0).label("spend"),
        func.coalesce(func.sum(CostTracker.input_tokens), 0).label("input_tokens"),
        func.coalesce(func.sum(CostTracker.output_tokens), 0).label("output_tokens"),
        func.count(CostTracker.id).label("calls"),
    ).where(and_(*filters))
    s = (await session.execute(summary_stmt)).one()

    by_feat = (
        await session.execute(
            select(
                CostTracker.context,
                CostTracker.agent_name,
                CostTracker.model_name,
                func.coalesce(func.sum(CostTracker.estimated_cost), 0.0).label("spend"),
                func.coalesce(func.sum(CostTracker.input_tokens), 0).label("input_tokens"),
                func.coalesce(func.sum(CostTracker.output_tokens), 0).label("output_tokens"),
                func.count(CostTracker.id).label("calls"),
            )
            .where(and_(*filters))
            .group_by(CostTracker.context, CostTracker.agent_name, CostTracker.model_name)
            .order_by(func.coalesce(func.sum(CostTracker.estimated_cost), 0.0).desc())
        )
    ).all()

    features = [
        {
            "context": remap_feature_label(r.context),
            "agent_name": remap_feature_label(r.agent_name),
            "model_name": r.model_name,
            "spend_usd": float(r.spend or 0),
            "input_tokens": int(r.input_tokens or 0),
            "output_tokens": int(r.output_tokens or 0),
            "calls": int(r.calls or 0),
        }
        for r in by_feat
    ]

    bucket = _functionality_bucket_expr()
    by_func_rows = (
        await session.execute(
            select(
                bucket.label("functionality"),
                func.coalesce(func.sum(CostTracker.estimated_cost), 0.0).label("spend"),
                func.coalesce(func.sum(CostTracker.input_tokens), 0).label("input_tokens"),
                func.coalesce(func.sum(CostTracker.output_tokens), 0).label("output_tokens"),
                func.count(CostTracker.id).label("calls"),
            )
            .where(and_(*filters))
            .group_by(bucket)
            .order_by(func.coalesce(func.sum(CostTracker.estimated_cost), 0.0).desc())
        )
    ).all()
    by_functionality = [
        {
            "functionality": normalize_functionality(r.functionality),
            "label": label_for(r.functionality),
            "spend_usd": float(r.spend or 0),
            "input_tokens": int(r.input_tokens or 0),
            "output_tokens": int(r.output_tokens or 0),
            "calls": int(r.calls or 0),
        }
        for r in by_func_rows
    ]

    return {
        "chat_id": chat_id,
        "spend_usd": float(s.spend or 0),
        "input_tokens": int(s.input_tokens or 0),
        "output_tokens": int(s.output_tokens or 0),
        "calls": int(s.calls or 0),
        "by_functionality": by_functionality,
        "by_feature": features,
    }


# ---------------------------------------------------------------------------
# USER TRACKING — users / messages / reports / versions + subscription only
# ---------------------------------------------------------------------------

async def count_total_users(session: AsyncSession) -> int:
    """All registered users in the DB (not period-scoped)."""
    result = await session.execute(select(func.count(User.id)))
    return int(result.scalar() or 0)


async def count_active_users(
    session: AsyncSession,
    *,
    start: Optional[datetime],
    end: datetime,
) -> int:
    """Active = distinct users with a chat message OR a costtracker row in range."""
    msg_filters = [Message.updated_at <= end, Message.is_deleted.is_(False)]
    if start is not None:
        msg_filters.append(Message.updated_at >= start)
    msg_ids = select(Message.user_id).where(and_(*msg_filters))

    cost_filters = [_cost_time_filter(start, end), CostTracker.user_id.isnot(None)]
    cost_ids = select(CostTracker.user_id).where(and_(*cost_filters))

    union_subq = msg_ids.union(cost_ids).subquery()
    result = await session.execute(select(func.count()).select_from(union_subq))
    return int(result.scalar() or 0)


async def count_new_users(
    session: AsyncSession,
    *,
    start: Optional[datetime],
    end: datetime,
) -> int:
    filters = [User.created_at <= end]
    if start is not None:
        filters.append(User.created_at >= start)
    result = await session.execute(select(func.count(User.id)).where(and_(*filters)))
    return int(result.scalar() or 0)


async def search_users(
    session: AsyncSession,
    *,
    q: str,
    limit: int = 20,
) -> List[Dict[str, Any]]:
    q = (q or "").strip()
    if not q:
        return []
    pattern = f"%{q}%"
    stmt = (
        select(User)
        .where(or_(User.email.ilike(pattern), User.user_name.ilike(pattern)))
        .order_by(User.email.asc())
        .limit(limit)
    )
    users = (await session.execute(stmt)).scalars().all()
    snapshots = await get_subscription_snapshots(session, [u.id for u in users])
    out = []
    for u in users:
        out.append({
            "user_id": u.id,
            "email": u.email,
            "user_name": u.user_name,
            "created_at": u.created_at.isoformat() if u.created_at else None,
            "subscription": snapshots.get(u.id, {"current_tier": "free", "status": None}),
        })
    return out


async def list_users_with_activity(
    session: AsyncSession,
    *,
    start: Optional[datetime],
    end: datetime,
    q: Optional[str] = None,
    page: int = 1,
    page_size: int = 25,
    sort: str = "spend",
    active_only: bool = False,
) -> Dict[str, Any]:
    """Paginated users sorted by usage, with optional name/email search.

    Metrics (spend/chats/reports) are scoped to ``start``–``end``.
    By default lists all matching users (zero-usage last); set ``active_only``
    to only users with a chat or costtracker row in the period.
    """
    page = max(1, int(page or 1))
    page_size = max(1, min(int(page_size or 25), 200))
    offset = (page - 1) * page_size
    q = (q or "").strip() or None

    spend_subq = (
        select(
            CostTracker.user_id.label("user_id"),
            func.coalesce(func.sum(CostTracker.estimated_cost), 0.0).label("spend"),
            func.coalesce(func.sum(CostTracker.input_tokens), 0).label("input_tokens"),
            func.coalesce(func.sum(CostTracker.output_tokens), 0).label("output_tokens"),
            func.count(CostTracker.id).label("calls"),
        )
        .where(_cost_time_filter(start, end))
        .group_by(CostTracker.user_id)
    ).subquery()

    msg_filters = [Message.is_deleted.is_(False), Message.updated_at <= end]
    if start is not None:
        msg_filters.append(Message.updated_at >= start)
    chat_subq = (
        select(
            Message.user_id.label("user_id"),
            func.count(Message.id).label("chats"),
            func.max(Message.updated_at).label("last_active"),
        )
        .where(and_(*msg_filters))
        .group_by(Message.user_id)
    ).subquery()

    user_filters = []
    if q:
        pattern = f"%{q}%"
        user_filters.append(or_(User.email.ilike(pattern), User.user_name.ilike(pattern)))
    if active_only:
        user_filters.append(
            or_(spend_subq.c.user_id.isnot(None), chat_subq.c.user_id.isnot(None))
        )

    from_joins = (
        select(User.id)
        .outerjoin(spend_subq, User.id == spend_subq.c.user_id)
        .outerjoin(chat_subq, User.id == chat_subq.c.user_id)
    )
    if user_filters:
        from_joins = from_joins.where(and_(*user_filters))
    total = int(
        (await session.execute(select(func.count()).select_from(from_joins.subquery()))).scalar()
        or 0
    )

    base = (
        select(
            User,
            spend_subq.c.spend,
            spend_subq.c.input_tokens,
            spend_subq.c.output_tokens,
            spend_subq.c.calls,
            chat_subq.c.chats,
            chat_subq.c.last_active,
        )
        .outerjoin(spend_subq, User.id == spend_subq.c.user_id)
        .outerjoin(chat_subq, User.id == chat_subq.c.user_id)
    )
    if user_filters:
        base = base.where(and_(*user_filters))

    spend_col = func.coalesce(spend_subq.c.spend, 0.0)
    chats_col = func.coalesce(chat_subq.c.chats, 0)
    if sort == "chats":
        order = chats_col.desc(), spend_col.desc(), User.email.asc()
    elif sort == "last_active":
        order = chat_subq.c.last_active.desc().nullslast(), spend_col.desc()
    else:
        order = spend_col.desc(), chats_col.desc(), User.email.asc()

    rows = (
        await session.execute(base.order_by(*order).offset(offset).limit(page_size))
    ).all()

    page_ids = [u.id for u, *_rest in rows]
    report_rows: Dict[str, int] = {}
    if page_ids:
        report_stmt = (
            select(Message.user_id, func.count(Report.id).label("reports"))
            .join(Report, Report.chat_id == Message.id)
            .where(Message.user_id.in_(page_ids), Message.is_deleted.is_(False))
            .group_by(Message.user_id)
        )
        report_rows = {
            r.user_id: int(r.reports or 0)
            for r in (await session.execute(report_stmt)).all()
        }

    items: List[Dict[str, Any]] = []
    snapshots = await get_subscription_snapshots(session, page_ids)
    for u, spend, input_tokens, output_tokens, calls, chats, last_active in rows:
        items.append({
            "user_id": u.id,
            "email": u.email,
            "user_name": u.user_name,
            "created_at": u.created_at.isoformat() if u.created_at else None,
            "last_active": last_active.isoformat() if last_active else None,
            "chats": int(chats or 0),
            "reports": report_rows.get(u.id, 0),
            "spend_usd": float(spend or 0),
            "input_tokens": int(input_tokens or 0),
            "output_tokens": int(output_tokens or 0),
            "calls": int(calls or 0),
            "subscription": snapshots.get(u.id, {"current_tier": "free", "status": None}),
        })

    return {
        "users": items,
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": (total + page_size - 1) // page_size if page_size else 0,
    }


async def active_users_report(
    session: AsyncSession,
    *,
    start: Optional[datetime],
    end: datetime,
    page: int = 1,
    page_size: int = 25,
    include_list: bool = True,
) -> Dict[str, Any]:
    """How many users were active in a custom period (day / days / month / …).

    Active = distinct users with a chat message OR a costtracker row in range.
    Also returns new signups and a per-day active-user series for charts.
    """
    active_count = await count_active_users(session, start=start, end=end)
    new_users = await count_new_users(session, start=start, end=end)

    # Daily distinct active users (messages ∪ costtracker)
    msg_filters = [Message.is_deleted.is_(False), Message.updated_at <= end]
    if start is not None:
        msg_filters.append(Message.updated_at >= start)
    msg_days = select(
        func.date_trunc("day", Message.updated_at).label("day"),
        Message.user_id.label("user_id"),
    ).where(and_(*msg_filters))

    cost_days = select(
        func.date_trunc("day", CostTracker.timestamp).label("day"),
        CostTracker.user_id.label("user_id"),
    ).where(
        and_(
            _cost_time_filter(start, end),
            CostTracker.user_id.isnot(None),
        )
    )

    daily_union = msg_days.union(cost_days).subquery()
    daily_stmt = (
        select(
            daily_union.c.day,
            func.count(func.distinct(daily_union.c.user_id)).label("active_users"),
        )
        .group_by(daily_union.c.day)
        .order_by(daily_union.c.day.asc())
    )
    daily = [
        {
            "day": r.day.date().isoformat() if hasattr(r.day, "date") else str(r.day)[:10],
            "active_users": int(r.active_users or 0),
        }
        for r in (await session.execute(daily_stmt)).all()
        if r.day is not None
    ]

    payload: Dict[str, Any] = {
        "active_users": active_count,
        "new_users": new_users,
        "daily": daily,
    }

    if include_list:
        listed = await list_users_with_activity(
            session,
            start=start,
            end=end,
            page=page,
            page_size=page_size,
            sort="spend",
            active_only=True,
        )
        payload.update(listed)

    return payload


async def user_summary(
    session: AsyncSession,
    *,
    user_id: str,
    start: Optional[datetime],
    end: datetime,
) -> Optional[Dict[str, Any]]:
    result = await session.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        return None

    sub = await get_subscription_snapshot(session, user_id)
    costs = await cost_summary(session, start=start, end=end, user_id=user_id)
    functionalities = await cost_by_functionality(
        session, start=start, end=end, user_id=user_id
    )
    features = await cost_by_feature(session, start=start, end=end, user_id=user_id, limit=20)
    models = await cost_by_model(session, start=start, end=end, user_id=user_id, limit=15)

    chat_filters = [Message.user_id == user_id, Message.is_deleted.is_(False)]
    chats_total = int(
        (await session.execute(select(func.count(Message.id)).where(and_(*chat_filters)))).scalar()
        or 0
    )
    reports_total = int(
        (
            await session.execute(
                select(func.count(Report.id))
                .join(Message, Report.chat_id == Message.id)
                .where(Message.user_id == user_id, Message.is_deleted.is_(False))
            )
        ).scalar()
        or 0
    )
    last_active = (
        await session.execute(
            select(func.max(Message.updated_at)).where(and_(*chat_filters))
        )
    ).scalar()

    return {
        "user_id": user.id,
        "email": user.email,
        "user_name": user.user_name,
        "created_at": user.created_at.isoformat() if user.created_at else None,
        "last_active": last_active.isoformat() if last_active else None,
        "subscription": sub,
        "chats_total": chats_total,
        "reports_total": reports_total,
        "costs": costs,
        "costs_by_functionality": functionalities,
        "costs_by_feature": features,
        "costs_by_model": models,
    }


async def user_chats(
    session: AsyncSession,
    *,
    user_id: str,
    start: Optional[datetime],
    end: datetime,
    limit: int = 50,
) -> List[Dict[str, Any]]:
    filters = [Message.user_id == user_id, Message.is_deleted.is_(False)]
    # Include chats touched in range OR with cost in range — still list all if range=all
    if start is not None:
        filters.append(Message.updated_at >= start)
    filters.append(Message.updated_at <= end)

    msgs = (
        await session.execute(
            select(Message)
            .where(and_(*filters))
            .order_by(Message.updated_at.desc())
            .limit(limit)
        )
    ).scalars().all()

    out = []
    for m in msgs:
        cost = await cost_for_chat(session, chat_id=m.id, start=start, end=end)
        report_count = int(
            (
                await session.execute(
                    select(func.count(Report.id)).where(Report.chat_id == m.id)
                )
            ).scalar()
            or 0
        )
        out.append({
            "chat_id": m.id,
            "chat_title": m.chat_title or "Untitled chat",
            "created_at": m.created_at.isoformat() if m.created_at else None,
            "updated_at": m.updated_at.isoformat() if m.updated_at else None,
            "reports_count": report_count,
            "spend_usd": cost["spend_usd"],
            "input_tokens": cost["input_tokens"],
            "output_tokens": cost["output_tokens"],
            "calls": cost["calls"],
        })
    return out


async def chat_reports(
    session: AsyncSession,
    *,
    chat_id: str,
) -> List[Dict[str, Any]]:
    reports = (
        await session.execute(
            select(Report)
            .where(Report.chat_id == chat_id)
            .order_by(Report.created_at.desc())
        )
    ).scalars().all()
    out = []
    for r in reports:
        version_count = int(
            (
                await session.execute(
                    select(func.count(ReportVersion.id)).where(
                        ReportVersion.report_id == r.id
                    )
                )
            ).scalar()
            or 0
        )
        out.append({
            "report_id": r.id,
            "title": r.title or "Untitled report",
            "status": r.status,
            "current_version": r.current_version,
            "versions_count": version_count,
            "report_type": r.report_type,
            "domain_name": r.domain_name,
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "last_activity_at": r.last_activity_at.isoformat() if r.last_activity_at else None,
        })
    return out


async def report_versions(
    session: AsyncSession,
    *,
    report_id: str,
) -> List[Dict[str, Any]]:
    versions = (
        await session.execute(
            select(ReportVersion)
            .where(ReportVersion.report_id == report_id)
            .order_by(ReportVersion.version.desc())
        )
    ).scalars().all()
    return [
        {
            "version_id": v.id,
            "report_id": v.report_id,
            "version": v.version,
            "is_active": v.is_active,
            "status": v.status,
            "generated_at": v.generated_at.isoformat() if v.generated_at else None,
            "created_at": v.created_at.isoformat() if v.created_at else None,
            "last_activity_at": v.last_activity_at.isoformat() if v.last_activity_at else None,
        }
        for v in versions
    ]
