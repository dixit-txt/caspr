"""cards routes: summary.

Split out of ``app/cards/router.py`` to keep each router file under
the ~400-line ceiling in R-STRUCT-3. Handlers are unchanged.
"""

"""HTTP routes for the cards bounded context.

Handlers moved verbatim from ``src/resources/routers/api.py`` during the
R-STRUCT-1 migration, which split that 10,257-line module across seven
contexts. Handler bodies are unchanged; only the import block and the
``APIRouter`` they attach to are new.
"""

"""api.py: Authentication API for the Casper backend"""

from fastapi import APIRouter, Depends
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from sqlalchemy import and_, or_, select
from uuid_utils import uuid7

from app.auth.repository import (
    check_user_by_id,
)
from app.auth.schemas import ErrorResponse
from app.auth.token import get_current_active_user
from app.cards.schemas import (
    RegenerateESRequest,
    RegenerateESResponse,
)
from app.cards.service_summary import update_executive_summary
from app.core.db import async_session_scope
from app.core.enums import ReportStatus
from app.core.logging import setup_logging
from app.core.redis import get_redis_instance
from app.models import Card
from app.observability.functionality_context import Functionality, set_functionality
from app.reports.repository import (
    update_report_status_if_needed,
    verify_report_ownership,
)

router = APIRouter()
logger = setup_logging(__file__)
redis_instance = get_redis_instance()


# ========================================================================================================
# Regenerate Executive Summary Endpoint
# ========================================================================================================
@router.post(
    "/regenerate-executive-summary",
    response_model=RegenerateESResponse,
    status_code=200,
    responses={
        404: {"model": ErrorResponse},
        401: {"model": ErrorResponse},
        422: {"model": ErrorResponse},
        500: {"model": ErrorResponse},
    },
)
async def regenerate_executive_summary(
    data: RegenerateESRequest, user_id: str = Depends(get_current_active_user)
):
    """
    Regenerate executive summary based on refined cards.
    Only creates a new ES version if cards have been modified since last ES generation.
    """
    set_functionality(Functionality.EXECUTIVE_SUMMARY)
    try:
        logger.info(f"Regenerate ES request for report_id: {data.report_id}, user_id: {user_id}")

        async with async_session_scope() as session:
            # 1. Validate user token first
            db_response = await check_user_by_id(user_id=user_id, session=session)
            if not db_response.get("success", False):
                logger.error(f"Failed to check user by id: {db_response.get('error')}")
                return JSONResponse(
                    status_code=500,
                    content={
                        "success": False,
                        "error": "Something went wrong while regenerating the executive summary. Please try again.",
                    },
                )
            if not db_response.get("exists", False):
                logger.error(f"User with ID {user_id} not found or token is invalid")
                return JSONResponse(
                    status_code=401, content={"success": False, "error": "Unauthorized request"}
                )

            # 2. Get report and verify ownership
            ownership = await verify_report_ownership(
                report_id=data.report_id, user_id=user_id, session=session
            )
            if not ownership["authorized"]:
                return JSONResponse(
                    status_code=ownership["status_code"],
                    content={"success": False, "error": ownership["error"]},
                )

            # 3. Get all relevant cards in one query:
            # - Active section cards (is_active=True, is_deleted=False)
            # - Deleted section cards that changed since last ES (is_deleted=True, changed_since_es=True)
            # - Active ES card (is_active=True, is_deleted=False)
            all_cards_result = await session.execute(
                select(Card)
                .where(
                    and_(
                        Card.report_id == data.report_id,
                        or_(
                            # Active sections
                            and_(
                                Card.type == "section",
                                Card.is_active == True,
                                Card.is_deleted == False,
                            ),
                            # Deleted sections that changed since last ES
                            and_(
                                Card.type == "section",
                                Card.is_deleted == True,
                                Card.changed_since_es == True,
                            ),
                            # Active ES card
                            and_(
                                Card.type == "es", Card.is_active == True, Card.is_deleted == False
                            ),
                        ),
                    )
                )
                .order_by(Card.sequence)
            )
            all_cards = all_cards_result.scalars().all()

            # Separate section cards (active + deleted with changes) and ES card
            section_cards = [card for card in all_cards if card.type == "section"]
            current_es_card = next((card for card in all_cards if card.type == "es"), None)

            if not section_cards:
                return JSONResponse(
                    status_code=400,
                    content={"success": False, "error": "No sections found in this report."},
                )

            if not current_es_card:
                return JSONResponse(
                    status_code=404,
                    content={
                        "success": False,
                        "error": "Executive summary not found for this report.",
                    },
                )

            # 4. Check if any cards have changed since last ES
            any_changed = any(card.changed_since_es for card in section_cards)

            if not any_changed:
                return RegenerateESResponse(
                    success=False,
                    message="No changes have been made to any sections since the last executive summary was generated. Please refine some sections first to regenerate the summary.",
                )

            # 5. Extract current ES content
            current_es_content = ""
            if current_es_card.content and isinstance(current_es_card.content, dict):
                content_data = current_es_card.content.get("content", "")
                current_es_content = (
                    content_data.get("content", "")
                    if isinstance(content_data, dict)
                    else str(content_data)
                )

            current_es_version = current_es_card.version or 1

            # 6. Build cards_summary list for the update function
            cards_summary = []

            for card in section_cards:
                card_title = card.title or "Untitled Section"
                current_summary = card.summary or ""
                previous_summary = None
                is_deleted = card.is_deleted

                # If card changed, find the previous version used in ES
                if card.changed_since_es and card.card_id:
                    # Find the last version of this card that was used in ES
                    prev_card_result = await session.execute(
                        select(Card)
                        .where(
                            Card.card_id == card.card_id,
                            Card.report_id == data.report_id,
                            Card.type == "section",
                            Card.last_es_version_used == current_es_version,
                            Card.is_deleted == False,
                        )
                        .order_by(Card.version.desc())
                    )
                    prev_card = prev_card_result.scalar_one_or_none()

                    if prev_card:
                        previous_summary = prev_card.summary or ""

                cards_summary.append(
                    {
                        "card_title": card_title,
                        "current_summary": current_summary,
                        "previous_summary": previous_summary,
                        "is_deleted": is_deleted,
                    }
                )

            # 7. Call update function
            logger.info(f"Calling update_executive_summary for report_id: {data.report_id}")
            updated_es = await run_in_threadpool(
                update_executive_summary,
                cards=cards_summary,
                current_executive_summary=current_es_content,
                report_id=data.report_id,
            )

            # Check if ES actually changed
            if updated_es == current_es_content:
                return RegenerateESResponse(
                    success=True,
                    message="Executive summary reviewed but no updates were necessary based on the changes made.",
                    new_es_version=current_es_version,
                    updated_summary=current_es_content,
                )

            # 8. Create new ES version
            new_es_version = current_es_version + 1

            # Deactivate current ES
            current_es_card.is_active = False

            # Create new ES card
            new_es_card = Card(
                id=str(uuid7()),
                card_id=current_es_card.card_id,
                report_id=data.report_id,
                title="executive_summary",
                sequence=4,  # ES is always sequence 4
                content={"name": "executive_summary", "content": updated_es},
                sub_sections=[],
                citations={},
                summary="",
                type="es",
                version=new_es_version,
                is_active=True,
                is_deleted=False,
                changed_since_es=False,  # ES itself doesn't have this flag used meaningfully
                last_es_version_used=None,
            )

            session.add(new_es_card)

            # 9. Update all section cards: reset flags and mark as used in new ES
            for card in section_cards:
                card.changed_since_es = False
                card.last_es_version_used = new_es_version

            # 10. Update report status
            await update_report_status_if_needed(
                report_id=data.report_id,
                new_status=ReportStatus.REDO_ANALYSIS.value,
                session=session,
            )

            await session.commit()

            logger.info(
                f"Successfully regenerated ES for report_id: {data.report_id}, new version: {new_es_version}"
            )

            return RegenerateESResponse(
                success=True,
                message=f"Executive summary successfully updated to version {new_es_version} based on your refined sections.",
                new_es_version=new_es_version,
                updated_summary=updated_es,
            )

    except Exception as e:
        logger.error(f"Error regenerating executive summary: {e!s}", exc_info=True)
        return JSONResponse(
            status_code=500,
            content={
                "success": False,
                "error": "An error occurred while regenerating the executive summary. Please try again.",
            },
        )
