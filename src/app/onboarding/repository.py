"""
onboarding_db.py
================
Async database functions for the onboarding flow:
  - Fetch active user_roles / research_interests (for frontend dropdowns)
  - Save user's role selection
  - Save user's research interest selections
  - Validate university email domain
  - Save university verification state
  - Mark onboarding as complete
"""

from typing import Any

from sqlalchemy import delete, select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from uuid_utils import uuid7

from app.core.logging import setup_logging
from app.models import (
    ResearchInterest,
    University,
    User,
    UserResearchInterest,
    UserRole,
)

logger = setup_logging(__file__)

# Internal `name` values for the curated lookup rows that act as the "Other"
# placeholder cards in the UI. Kept in sync with the seed data in
# db_migrations/versions/f1a2b3c4d5e6_add_onboarding_tables.py.
OTHER_ROLE_NAME = "other"
OTHER_RESEARCH_INTEREST_NAME = "something_else"

# When a user picks one of the "Other" cards and types a description we don't
# link them to the shared curated row — that would overwrite everyone else's
# text. Instead we upsert a per-user row in the same lookup table, with
# is_active=False so it never appears in another user's option list. These
# prefixes form the stable, unique `name` of that per-user row.
CUSTOM_ROLE_NAME_PREFIX = "custom_user_"
CUSTOM_RESEARCH_NAME_PREFIX = "custom_user_"
# display_order for custom rows is pushed past anything curated to make any
# accidental admin listing put them at the end.
CUSTOM_ROW_DISPLAY_ORDER = 9999


def _custom_role_name(user_id: str) -> str:
    return f"{CUSTOM_ROLE_NAME_PREFIX}{user_id}"


def _custom_research_interest_name(user_id: str) -> str:
    return f"{CUSTOM_RESEARCH_NAME_PREFIX}{user_id}"


# ============================================================================
# Read operations — lookup tables
# ============================================================================


async def get_active_user_roles(session: AsyncSession) -> dict[str, Any]:
    """Return all active user roles ordered by display_order."""
    try:
        stmt = select(UserRole).where(UserRole.is_active == True).order_by(UserRole.display_order)
        result = await session.execute(stmt)
        roles = result.scalars().all()
        return {
            "success": True,
            "roles": [
                {
                    "id": r.id,
                    "name": r.name,
                    "display_name": r.display_name,
                    "description": r.description,
                    "discount_tag": r.discount_tag,
                    "display_order": r.display_order,
                }
                for r in roles
            ],
        }
    except SQLAlchemyError as e:
        logger.error(f"[GET_ACTIVE_USER_ROLES] DB error: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


async def get_active_research_interests(session: AsyncSession) -> dict[str, Any]:
    """Return all active research interests ordered by display_order."""
    try:
        stmt = (
            select(ResearchInterest)
            .where(ResearchInterest.is_active == True)
            .order_by(ResearchInterest.display_order)
        )
        result = await session.execute(stmt)
        interests = result.scalars().all()
        return {
            "success": True,
            "interests": [
                {
                    "id": i.id,
                    "name": i.name,
                    "display_name": i.display_name,
                    "description": i.description,
                    "display_order": i.display_order,
                }
                for i in interests
            ],
        }
    except SQLAlchemyError as e:
        logger.error(f"[GET_ACTIVE_RESEARCH_INTERESTS] DB error: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


# ============================================================================
# Write operations — onboarding steps
# ============================================================================


async def save_user_role(
    user_id: str,
    role_id: str,
    session: AsyncSession,
    custom_role_text: str | None = None,
) -> dict[str, Any]:
    """Set the user's selected role (Step 1 of onboarding).

    Curated role path:
        ``users.user_role_id`` is set to ``role_id`` directly.

    Custom ("Other") path:
        ``custom_role_text`` must be supplied and non-empty. We upsert a
        per-user row in ``user_roles`` (``name = f"custom_user_<user_id>"``,
        ``is_active = False``) carrying the typed text in ``display_name``,
        and link the user to that row. ``is_active = False`` keeps the row
        out of every other user's option list.
    """
    try:
        chosen_role = await session.get(UserRole, role_id)
        if not chosen_role:
            return {"success": False, "error": "Invalid role_id"}

        effective_role_id = role_id
        is_custom = chosen_role.name == OTHER_ROLE_NAME

        if is_custom:
            normalized_custom_text = (custom_role_text or "").strip() or None
            if not normalized_custom_text:
                return {
                    "success": False,
                    "error": "custom_role_text is required when selecting 'Other'",
                }

            custom_row = (
                await session.execute(
                    select(UserRole).where(UserRole.name == _custom_role_name(user_id))
                )
            ).scalar_one_or_none()

            if custom_row is None:
                custom_row = UserRole(
                    id=str(uuid7()),
                    name=_custom_role_name(user_id),
                    display_name=normalized_custom_text,
                    description=None,
                    discount_tag=None,
                    is_active=False,
                    display_order=CUSTOM_ROW_DISPLAY_ORDER,
                )
                session.add(custom_row)
                await session.flush()  # ensure custom_row.id is populated
            else:
                custom_row.display_name = normalized_custom_text
                custom_row.is_active = False  # stay hidden from option lists

            effective_role_id = custom_row.id

        await session.execute(
            update(User).where(User.id == user_id).values(user_role_id=effective_role_id)
        )
        await session.commit()
        logger.info(
            f"[SAVE_USER_ROLE] user={user_id} requested_role_id={role_id} "
            f"role_name={chosen_role.name} is_custom={is_custom} "
            f"stored_user_role_id={effective_role_id}"
        )
        return {"success": True}
    except SQLAlchemyError as e:
        logger.error(f"[SAVE_USER_ROLE] DB error: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


async def save_user_research_interests(
    user_id: str,
    interest_ids: list[str],
    session: AsyncSession,
    custom_research_text: str | None = None,
) -> dict[str, Any]:
    """
    Replace the user's research interest selections (Step 2 of onboarding).
    Deletes existing associations and inserts the new set.

    Curated interests are inserted into ``user_research_interests`` pointing
    at the curated row directly.

    If the user also ticks the ``something_else`` card,
    ``custom_research_text`` must be supplied. We upsert a per-user row in
    ``research_interests`` (``name = f"custom_user_<user_id>"``,
    ``is_active = False``) holding the typed text in ``display_name``, and
    the junction row for that selection points at the per-user row instead
    of the shared ``something_else`` row. The shared ``something_else`` row
    is never stored in the junction.
    """
    try:
        # Validate the incoming ids first so we don't half-mutate state on a
        # bad request. We also resolve whether any of them is the curated
        # "something_else" card so we know if we need a custom row.
        resolved_interests = []
        has_other_interest = False
        for interest_id in interest_ids:
            interest = await session.get(ResearchInterest, interest_id)
            if not interest:
                return {"success": False, "error": f"Invalid research_interest_id: {interest_id}"}
            if interest.name == OTHER_RESEARCH_INTEREST_NAME:
                has_other_interest = True
            resolved_interests.append(interest)

        normalized_custom_text: str | None = None
        if has_other_interest:
            normalized_custom_text = (custom_research_text or "").strip() or None
            if not normalized_custom_text:
                return {
                    "success": False,
                    "error": "custom_research_text is required when selecting 'Something else'",
                }

        # Upsert the per-user custom row if needed. We always fetch it (when
        # the user is moving away from "Something else") so we can leave it
        # in place but disconnected — `name` is stable per user, so a future
        # selection will simply update its display_name.
        custom_row = (
            await session.execute(
                select(ResearchInterest).where(
                    ResearchInterest.name == _custom_research_interest_name(user_id)
                )
            )
        ).scalar_one_or_none()

        if has_other_interest:
            if custom_row is None:
                custom_row = ResearchInterest(
                    id=str(uuid7()),
                    name=_custom_research_interest_name(user_id),
                    display_name=normalized_custom_text,
                    description=None,
                    is_active=False,
                    display_order=CUSTOM_ROW_DISPLAY_ORDER,
                )
                session.add(custom_row)
                await session.flush()  # populate custom_row.id
            else:
                custom_row.display_name = normalized_custom_text
                custom_row.is_active = False

        # Replace the user's existing junction rows. We issue an explicit
        # DELETE statement (and flush) so the deletes execute BEFORE the
        # inserts below — otherwise SQLAlchemy's default unit-of-work order
        # is INSERT, UPDATE, DELETE, which would collide with the
        # (user_id, research_interest_id) unique constraint when the user
        # re-selects an interest they already have.
        await session.execute(
            delete(UserResearchInterest).where(UserResearchInterest.user_id == user_id)
        )
        await session.flush()

        for interest in resolved_interests:
            target_interest_id = (
                custom_row.id if interest.name == OTHER_RESEARCH_INTEREST_NAME else interest.id
            )
            session.add(
                UserResearchInterest(
                    id=str(uuid7()),
                    user_id=user_id,
                    research_interest_id=target_interest_id,
                )
            )

        await session.commit()
        logger.info(
            f"[SAVE_USER_RESEARCH_INTERESTS] user={user_id} interests={interest_ids} "
            f"has_other_interest={has_other_interest} "
            f"has_custom_text={bool(normalized_custom_text)}"
        )
        return {"success": True}
    except SQLAlchemyError as e:
        logger.error(f"[SAVE_USER_RESEARCH_INTERESTS] DB error: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


# ============================================================================
# University domain validation & verification
# ============================================================================


async def validate_university_domain(email: str, session: AsyncSession) -> dict[str, Any]:
    """
    Check if the domain portion of *email* belongs to a registered, active
    partner university.

    Returns:
        success, matched (bool), university dict (if matched)
    """
    try:
        domain = email.rsplit("@", 1)[-1].lower()
        stmt = select(University).where(
            University.email_domain == domain, University.is_active == True
        )
        result = await session.execute(stmt)
        uni = result.scalar_one_or_none()

        if uni:
            return {
                "success": True,
                "matched": True,
                "university": {
                    "id": uni.id,
                    "name": uni.name,
                    "email_domain": uni.email_domain,
                    "discount_percentage": uni.discount_percentage,
                },
            }
        return {"success": True, "matched": False}
    except SQLAlchemyError as e:
        logger.error(f"[VALIDATE_UNIVERSITY_DOMAIN] DB error: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


async def save_university_email(
    user_id: str,
    university_email: str,
    university_id: str,
    session: AsyncSession,
) -> dict[str, Any]:
    """Persist the student's university email and link the university."""
    try:
        stmt = (
            update(User)
            .where(User.id == user_id)
            .values(
                university_email=university_email,
                university_id=university_id,
                is_university_verified=False,
            )
        )
        await session.execute(stmt)
        await session.commit()
        logger.info(
            f"[SAVE_UNIVERSITY_EMAIL] user={user_id} email={university_email} uni={university_id}"
        )
        return {"success": True}
    except SQLAlchemyError as e:
        logger.error(f"[SAVE_UNIVERSITY_EMAIL] DB error: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


async def mark_university_verified(user_id: str, session: AsyncSession) -> dict[str, Any]:
    """Flip is_university_verified to True after the user clicks the verification link."""
    try:
        stmt = update(User).where(User.id == user_id).values(is_university_verified=True)
        await session.execute(stmt)
        await session.commit()
        logger.info(f"[MARK_UNIVERSITY_VERIFIED] user={user_id}")
        return {"success": True}
    except SQLAlchemyError as e:
        logger.error(f"[MARK_UNIVERSITY_VERIFIED] DB error: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


# ============================================================================
# Complete onboarding
# ============================================================================


async def mark_onboarding_completed(user_id: str, session: AsyncSession) -> dict[str, Any]:
    """Set onboarding_completed = True."""
    try:
        stmt = update(User).where(User.id == user_id).values(onboarding_completed=True)
        await session.execute(stmt)
        await session.commit()
        logger.info(f"[MARK_ONBOARDING_COMPLETED] user={user_id}")
        return {"success": True}
    except SQLAlchemyError as e:
        logger.error(f"[MARK_ONBOARDING_COMPLETED] DB error: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


async def get_user_onboarding_status(user_id: str, session: AsyncSession) -> dict[str, Any]:
    """
    Return the current onboarding state for a user — useful for the frontend
    to know which step to show if the user refreshes mid-flow.

    Custom rows (``is_active = False``) are translated back to the curated
    ``other`` / ``something_else`` ids so the frontend's option grid can
    highlight the right card; the typed text is returned alongside in
    ``custom_role_text`` / ``custom_research_text``.
    """
    try:
        user = await session.get(User, user_id)
        if not user:
            return {"success": False, "error": "User not found"}

        # ---- Role ----
        reported_role_id: str | None = user.user_role_id
        custom_role_text: str | None = None
        if user.user_role_id:
            role = await session.get(UserRole, user.user_role_id)
            if (
                role is not None
                and not role.is_active
                and role.name.startswith(CUSTOM_ROLE_NAME_PREFIX)
            ):
                # Resolve the curated "other" row so the frontend selects it.
                other_role = (
                    await session.execute(select(UserRole).where(UserRole.name == OTHER_ROLE_NAME))
                ).scalar_one_or_none()
                reported_role_id = other_role.id if other_role else None
                custom_role_text = role.display_name

        # ---- Research interests ----
        interest_rows = await session.execute(
            select(ResearchInterest)
            .join(
                UserResearchInterest,
                UserResearchInterest.research_interest_id == ResearchInterest.id,
            )
            .where(UserResearchInterest.user_id == user_id)
        )
        interest_rows = interest_rows.scalars().all()

        custom_research_text: str | None = None
        other_interest_row = None
        reported_interest_ids: list[str] = []
        for interest in interest_rows:
            if not interest.is_active and interest.name.startswith(CUSTOM_RESEARCH_NAME_PREFIX):
                custom_research_text = interest.display_name
                if other_interest_row is None:
                    other_interest_row = (
                        await session.execute(
                            select(ResearchInterest).where(
                                ResearchInterest.name == OTHER_RESEARCH_INTEREST_NAME
                            )
                        )
                    ).scalar_one_or_none()
                if other_interest_row is not None:
                    reported_interest_ids.append(other_interest_row.id)
            else:
                reported_interest_ids.append(interest.id)

        return {
            "success": True,
            "onboarding": {
                "onboarding_completed": user.onboarding_completed,
                "user_role_id": reported_role_id,
                "custom_role_text": custom_role_text,
                "research_interest_ids": reported_interest_ids,
                "custom_research_text": custom_research_text,
                "university_email": user.university_email,
                "is_university_verified": user.is_university_verified,
                "university_id": user.university_id,
            },
        }
    except SQLAlchemyError as e:
        logger.error(f"[GET_USER_ONBOARDING_STATUS] DB error: {e}", exc_info=True)
        return {"success": False, "error": str(e)}
