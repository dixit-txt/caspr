"""Database access for the cards bounded context.

Moved verbatim from ``src/db/async_db_functions.py`` during the R-STRUCT-1
migration. Function bodies are unchanged; only the import block was retargeted
at the new module paths.
"""

import copy
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from fastapi.concurrency import run_in_threadpool
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from uuid_utils import uuid7

from app.core.enums import RefinementType
from app.core.logging import setup_logging
from app.core.sanitize import sanitize_card_data
from app.models import (
    Card, CardVersion, RefinementHistory, ReportVersionCard, Table,
)

logger = setup_logging(__file__)


def _normalize_table_title_value(value: Any) -> str:
    """Normalize table_title payloads to a plain string."""
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return _normalize_table_title_value(value[0] if value else "")
    if isinstance(value, str):
        return value
    return str(value)
def _normalize_tables_for_response(tables: Any) -> list[Dict[str, Any]]:
    """Normalize tables list so each table_title is always a string."""
    if not isinstance(tables, list):
        return []

    normalized_tables = []
    for table in tables:
        if not isinstance(table, dict):
            continue
        table_copy = dict(table)
        table_copy["table_title"] = _normalize_table_title_value(table_copy.get("table_title", ""))
        normalized_tables.append(table_copy)

    return normalized_tables
def _normalize_subsections_for_response(sub_sections: Any) -> list[Dict[str, Any]]:
    """Normalize subsection tables for API-safe output."""
    if not isinstance(sub_sections, list):
        return []

    normalized_sub_sections = []
    for sub in sub_sections:
        if not isinstance(sub, dict):
            continue
        sub_copy = dict(sub)
        sub_copy["tables"] = _normalize_tables_for_response(sub_copy.get("tables", []))
        normalized_sub_sections.append(sub_copy)

    return normalized_sub_sections
async def insert_table(session: AsyncSession, card_id: str, table_data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Insert a new table for a card.
    
    Args:
        session (AsyncSession): SQLAlchemy async session
        card_id (str): Business identifier for the card (card_id field from cards table)
        table_data (Dict[str, Any]): Dictionary containing table data with keys:
            - table_id: Business identifier for the table (required) - this can be reused across cards
            - table_title: Title of the table (optional)
            - table_markdown: Markdown representation of the table (optional)
            - visualization: Base64 representation of the visualization image (optional)
            - report_id: ID of the associated report (required)
            
    Returns:
        Dict[str, Any]: Dictionary with success status and table_id (the new primary key)
        
    Note:
        - The function generates a new unique UUID for the primary key 'id' field
        - The 'table_id' field stores the business identifier which can be reused
        - This prevents primary key conflicts when the same table_id is used in different cards
        - Uses parent_card_id to link to the card's primary key
    """
    logger.info(f"Creating new table for card: {card_id}")
    
    # Validate required fields
    if not card_id:
        logger.error("Missing required field: card_id")
        return {
            "success": False,
            "error": "Missing required field: card_id"
        }
    
    if not table_data.get('table_id'):
        logger.error("Missing required field: table_id")
        return {
            "success": False,
            "error": "Missing required field: table_id"
        }
    
    if not table_data.get('report_id'):
        logger.error("Missing required field: report_id")
        return {
            "success": False,
            "error": "Missing required field: report_id"
        }
    
    try:
        # Generate a new unique UUID for the primary key
        id = str(uuid7())
        table_id = table_data.get('table_id')
        
        logger.info(f"Creating table record - Primary Key ID: {id}, Business Table ID: {table_id}")
        
        # Get the active card's primary key ID using the business card_id
        parent_stmt = select(Card.id).where(
            Card.card_id == card_id,
            Card.is_active == True
        )
        parent_stmt_result = await session.execute(parent_stmt)
        parent_result = parent_stmt_result.scalar_one_or_none()
        
        if not parent_result:
            logger.error(f"No active card found with business card_id: {card_id}")
            return {
                "success": False,
                "error": f"No active card found with business card_id: {card_id}"
            }
        
        parent_card_id = parent_result
        
        table_title = table_data.get('table_title')
        if isinstance(table_title, tuple):
            # Defensive fallback for malformed payloads where (title, table_markdown) is passed.
            table_title = table_title[0] if table_title else ""
        elif table_title is None:
            table_title = ""
        elif not isinstance(table_title, str):
            table_title = str(table_title)
        
        table_markdown = table_data.get('table_markdown')
        if table_markdown is None:
            table_markdown = ""
        elif not isinstance(table_markdown, str):
            table_markdown = str(table_markdown)
        
        new_table = Table(
            id=id,
            report_id=table_data.get('report_id'),
            parent_card_id=parent_card_id,  # Use the primary key ID from the active card
            table_id=table_id,
            table_title=table_title,
            table_markdown=table_markdown,
            base64_s3_uri=table_data.get('visualization')  # Store base64 directly
        )
        
        session.add(new_table)
        await session.commit()
        
        logger.info(f"Successfully created table with ID: {id}")
        return {
            "success": True,
            "table_id": id,
            "base64_visualization": table_data.get('visualization')
        }
        
    except SQLAlchemyError as e:
        logger.error(f"Database error creating table: {str(e)}", exc_info=True)
        await session.rollback()
        return {
            "success": False,
            "error": f"Database error: {str(e)}"
        }
    except Exception as e:
        logger.error(f"Unexpected error creating table: {str(e)}", exc_info=True)
        await session.rollback()
        return {
            "success": False,
            "error": f"Unexpected error: {str(e)}"
        }
async def insert_card(session: AsyncSession, report_id: str, report_card: Dict[str, Any]) -> Dict[str, Any]:
    """
    Insert a new card for a report with versioning support.
    
    Args:
        session (AsyncSession): SQLAlchemy async session
        report_id (str): ID of the associated report
        report_card (Dict[str, Any]): Dictionary containing card data with keys:
            - id: Card identifier (required) - this links to the original card concept
            - title: Title of the card (required)
            - sequence: Sequence number to determine order (required)
            - content: Content of the card as text (required)
            - sub_sections: Sub-sections of the card in JSON format (optional)
            - citations: Citations or references for the card (optional)
            - created_at: Creation timestamp (optional, will use current time if not provided)
            - summary: Summary of the card content (optional)
            - type: Type of the card (title/subtitle/toc/section/viz) (required)
            - section: Array of section objects with name, content, and tables (new format)
            
    Returns:
        Dict[str, Any]: Dictionary with success status and card_id
    """
    logger.info(f"Creating new card for report: {report_id}")
    
    # Validate required fields
    required_fields = ['id','sequence', 'type']
    for field in required_fields:
        if field not in report_card or report_card[field] is None:
            logger.error(f"Missing required field: {field}")
            return {
                "success": False,
                "error": f"Missing required field: {field}"
            }
    
    # Sanitize JSONB fields to remove invalid Unicode characters (e.g., null bytes)
    # This prevents PostgreSQL encoding errors during JSONB to bytes conversion
    report_card = await run_in_threadpool(sanitize_card_data, report_card)
    logger.info(f"Sanitized JSONB fields for card_id: {report_card.get('id')}")
    
    # For backward compatibility, check if we have the new section format
    has_section_format = 'section' in report_card and isinstance(report_card['section'], list)
    
    # If using new format, extract title and content from section
    title = report_card.get('title')
    content = report_card.get('content')
    
    if has_section_format and report_card['section']:
        # Extract title and content from first section
        first_section = report_card['section'][0]
        if not title and 'name' in first_section:
            title = first_section.get('name')
        # For new JSONB structure, use the entire section as content
        if not content:
            content = first_section  # Use the entire section structure as content
    
    try:
        # Check if this card_id already exists to determine version
        # Use the 'id' field from report_card as the business card_id for versioning
        card_id = report_card['id']
        existing_cards_stmt = select(Card).where(Card.card_id == card_id)
        existing_cards_result = await session.execute(existing_cards_stmt)
        existing_cards = existing_cards_result.scalars().all()
        
        # Calculate version number using MAX(version) + 1 to avoid duplicate version numbers
        # This handles cases where user reverts and then refines (deleted versions don't cause duplicates)
        if existing_cards:
            version = max(card.version for card in existing_cards) + 1
        else:
            version = 1
        
        # Deactivate all previous versions of this card_id
        if existing_cards:
            logger.info(f"Deactivating {len(existing_cards)} previous versions of card_id: {card_id}")
            for existing_card in existing_cards:
                existing_card.is_active = False
            await session.flush()
        
        # Generate new primary key ID for this card instance
        new_card_id = str(uuid7())
        
        # Set created_at if not provided
        if 'created_at' not in report_card or not report_card['created_at']:
            report_card['created_at'] = datetime.now(timezone.utc)
        
        # Set empty citations if not provided
        if 'citations' not in report_card or report_card['citations'] is None:
            report_card['citations'] = []
            
        # Set empty sub_sections if not provided
        if 'sub_sections' not in report_card or report_card['sub_sections'] is None:
            report_card['sub_sections'] = []
            
        # Create a new card
        new_card = Card(
            id=new_card_id,  # New primary key for this card instance
            card_id=card_id,  # Business identifier linking to original card concept
            report_id=report_id,
            title=title,
            sequence=report_card['sequence'],
            content=content,
            sub_sections=report_card.get('sub_sections'),
            citations=report_card['citations'],
            created_at=report_card['created_at'],
            summary=report_card.get('summary'),
            type=report_card['type'],
            version=version,  # Set calculated version number
            is_active=True,   # This new version is active
            is_deleted=False,  # Not deleted
            changed_since_es=False,  # Initial cards haven't changed since ES (they're part of initial generation)
            last_es_version_used=None  # Will be set when ES is generated
        )
        
        session.add(new_card)
        await session.flush()
        
        # NOTE: Cards are NO LONGER automatically linked to report versions here
        # report_version_cards will be populated ONLY when:
        # 1. /generate-report is called for version 1 (first generation)
        # 2. /regenerate-report creates a new version (version 2+)
        # This ensures report_version_cards always reflects the actual cards used in generation
        
        await session.commit()
        
        logger.info(f"Successfully created card with ID: {new_card_id}, card_id: {card_id}, version: {version}")
        return {
            "success": True,
            "parent_card_id": new_card_id,
            "card_id": card_id,
            "version": version
        }
        
    except SQLAlchemyError as e:
        logger.error(f"Database error creating card: {str(e)}", exc_info=True)
        await session.rollback()
        return {
            "success": False,
            "error": f"Database error: {str(e)}"
        }
    except Exception as e:
        logger.error(f"Unexpected error creating card: {str(e)}", exc_info=True)
        await session.rollback()
        return {
            "success": False,
            "error": f"Unexpected error: {str(e)}"
        }
async def get_tables_for_card(card_id: str, session: AsyncSession) -> Dict[str, Any]:
    """
    Get all tables associated with a specific card.
    
    Args:
        card_id (str): Business identifier for the card (card_id field from cards table)
        session (AsyncSession): SQLAlchemy async session
            
    Returns:
        Dict[str, Any]: Dictionary with success status and list of tables
    """
    logger.info(f"Getting tables for card with business card_id: {card_id}")
    
    if not card_id:
        logger.error("Missing required field: card_id")
        return {
            "success": False,
            "error": "Missing required field: card_id"
        }
    
    try:
        # Single-query fetch: join Table -> Card on parent_card_id and filter by
        # the business card_id. Projects only the columns we actually return so
        # we don't pull entire Table rows across the wire. Replaces the previous
        # two round-trip pattern (card PK lookup + tables lookup).
        stmt = (
            select(
                Table.table_id,
                Table.table_title,
                Table.table_markdown,
                Table.base64_s3_uri,
            )
            .join(Card, Table.parent_card_id == Card.id)
            .where(Card.card_id == card_id, Card.is_active.is_(True))
        )
        result = await session.execute(stmt)
        rows = result.all()

        if not rows:
            logger.info(f"No tables found for card with business card_id: {card_id}")
            return {
                "success": True,
                "tables": []
            }

        tables_data = [
            {
                "table_id": row.table_id,
                "table_title": row.table_title,
                "table_markdown": row.table_markdown,
                "s3_uri": row.base64_s3_uri,
            }
            for row in rows
        ]

        logger.info(f"Successfully retrieved {len(tables_data)} tables for card with business card_id: {card_id}")
        return {
            "success": True,
            "tables": tables_data
        }
        
    except SQLAlchemyError as e:
        logger.error(f"Database error retrieving card tables: {str(e)}", exc_info=True)
        return {
            "success": False,
            "error": f"Database error: {str(e)}"
        }
    except Exception as e:
        logger.error(f"Unexpected error retrieving card tables: {str(e)}", exc_info=True)
        return {
            "success": False,
            "error": f"Unexpected error: {str(e)}"
        }
async def get_report_cards(report_id: str, session: AsyncSession, version_id: str = None) -> Dict[str, Any]:
    """
    Get all cards associated with a specific report.
    
    Args:
        report_id (str): ID of the report to retrieve cards for
        session (AsyncSession): SQLAlchemy async session
        version_id (str, optional): Not used currently, kept for backward compatibility
            
    Returns:
        Dict[str, Any]: Dictionary with success status and list of cards
    """
    logger.info(f"Getting cards for report with ID: {report_id}")
    
    if not report_id:
        logger.error("Missing required field: report_id")
        return {
            "success": False,
            "error": "Missing required field: report_id"
        }
    
    try:
        # Eager-load `tables` relationship so we don't issue one extra query per
        # card (previous N+1 pattern was: 1 cards query + 2 queries per card for
        # tables). `selectinload` issues exactly one extra query that fetches
        # all tables for the batch of cards in a single round trip.
        stmt = (
            select(Card)
            .options(selectinload(Card.tables))
            .where(
                Card.report_id == report_id,
                Card.is_active.is_(True),
                Card.is_deleted.is_(False),
            )
            .order_by(Card.sequence)
        )

        result = await session.execute(stmt)
        cards = result.scalars().all()

        if not cards:
            logger.info(f"No cards found for report with ID: {report_id}")
            return {
                "success": True,
                "cards": []
            }

        cards_data = []
        for card in cards:
            # Tables were eager-loaded — no extra DB call here.
            tables_data = [
                {
                    "table_id": t.table_id,
                    "table_title": _normalize_table_title_value(t.table_title),
                    "table_markdown": t.table_markdown,
                    "s3_uri": t.base64_s3_uri,
                }
                for t in card.tables
            ]

            sub_sections_data = _normalize_subsections_for_response(card.sub_sections)

            # Support both the new JSONB content structure and legacy strings.
            if isinstance(card.content, dict):
                content_text = card.content.get('content', '')
                content_name = card.content.get('name', card.title or '')
                content_tables = _normalize_tables_for_response(card.content.get('tables', tables_data))
            else:
                content_text = card.content or ''
                content_name = card.title or ''
                content_tables = tables_data

            card_data = {
                "report_id": card.report_id,
                "sequence": card.sequence,
                "citations": card.citations,
                "created_at": card.created_at.isoformat() if card.created_at else None,
                "summary": card.summary,
                "type": card.type,
                "section": [
                    {
                        "name": content_name,
                        "content": content_text,
                        "tables": content_tables
                    }
                ],
                "sub_sections": sub_sections_data
            }

            cards_data.append(card_data)

        logger.info(f"Successfully retrieved {len(cards_data)} cards for report with ID: {report_id}")
        return {
            "success": True,
            "cards": cards_data
        }
        
    except SQLAlchemyError as e:
        logger.error(f"Database error retrieving report cards: {str(e)}", exc_info=True)
        return {
            "success": False,
            "error": f"Database error: {str(e)}"
        }
    except Exception as e:
        logger.error(f"Unexpected error retrieving report cards: {str(e)}", exc_info=True)
        return {
            "success": False,
            "error": f"Unexpected error: {str(e)}"
        }
async def get_table_id_markdown_map(report_id: str, session: AsyncSession) -> Dict[str, Any]:
    """
    Get the table_id_markdown_map for a specific report.
    
    Args:
        report_id (str): ID of the report to retrieve table_id_markdown_map for
        session (AsyncSession): SQLAlchemy async session
            
    Returns:
        Dict[str, Any]: Dictionary with success status and table_id_markdown_map
    """
    logger.info(f"Getting table_id_markdown_map for report: {report_id}")
    
    if not report_id:
        logger.error("Missing required field: report_id")
        return {
            "success": False,
            "error": "Missing required field: report_id"
        }
    
    try:
        # Query to get all tables for active cards only
        stmt = select(Table).join(
            Card, Table.parent_card_id == Card.id
        ).where(
            Table.report_id == report_id,
            Card.is_active == True
        )
        result = await session.execute(stmt)
        tables = result.scalars().all()

        table_id_markdown_map = {table.table_id: table.table_markdown for table in tables}

        logger.info(f"Successfully retrieved {len(table_id_markdown_map)} tables from active cards for report: {report_id}")
        return {
            "success": True,
            "table_id_markdown_map": table_id_markdown_map
            }
    except Exception as e:
        logger.error(f"Unexpected error retrieving table_id_markdown_map: {str(e)}", exc_info=True)
        return {
            "success": False,
            "error": f"Unexpected error: {str(e)}"
        }
    except SQLAlchemyError as e:
        logger.error(f"Database error retrieving table_id_markdown_map: {str(e)}", exc_info=True)
        return {
            "success": False,
            "error": f"Database error: {str(e)}"
        }
async def get_report_in_cards_format(report_id: str, session: AsyncSession) -> Dict[str, Any]:
    """
    Get all active cards for a specific report in the format similar to cards_for_db.json.
    
    Args:
        report_id (str): ID of the report to retrieve cards for
        session (AsyncSession): SQLAlchemy async session
            
    Returns:
        Dict[str, Any]: Dictionary with success status and formatted cards data
    """
    logger.info(f"Getting active cards for report with ID: {report_id} in cards_for_db format")
    
    if not report_id:
        logger.error("Missing required field: report_id")
        return {
            "success": False,
            "error": "Missing required field: report_id"
        }
    
    try:
        # Query to get all active cards for the report, ordered by sequence
        stmt = select(Card).where(
            Card.report_id == report_id,
            Card.is_active == True,
            Card.is_deleted == False
        ).order_by(Card.sequence)
        result = await session.execute(stmt)
        cards = result.scalars().all()
        
        if not cards:
            logger.warning(f"No active cards found for report with ID: {report_id}")
            return {
                "success": True,
                "cards": []
            }
        
        # Convert cards to the required format
        formatted_cards = []
        for card in cards:
            # Parse content based on type
            content_data = card.content
            content_text = ""
            tables_data = []
            
            # Handle content based on its structure
            if isinstance(content_data, dict):
                content_text = content_data.get("content", "")
                # Use tables from content if available, otherwise empty list
                if "tables" in content_data:
                    tables_data = content_data.get("tables", [])
            elif isinstance(content_data, str):
                content_text = content_data
            
            # Create the card structure similar to cards_for_db.json
            formatted_card = {
                "section": [
                    {
                        "name": card.title or "",
                        "content": content_text,
                        "tables": tables_data,
                        "id": card.card_id
                    }
                ],
                "sub_sections": card.sub_sections if card.sub_sections else [],
                "citations": card.citations if card.citations else {},
                "summary": card.summary or ""
            }
            
            formatted_cards.append(formatted_card)
        
        logger.info(f"Successfully retrieved {len(formatted_cards)} active cards for report with ID: {report_id}")
        return {
            "success": True,
            "cards": formatted_cards
        }
        
    except SQLAlchemyError as e:
        logger.error(f"Database error retrieving report cards: {str(e)}", exc_info=True)
        return {
            "success": False,
            "error": f"Database error: {str(e)}"
        }
    except Exception as e:
        logger.error(f"Unexpected error retrieving report cards: {str(e)}", exc_info=True)
        return {
            "success": False,
            "error": f"Unexpected error: {str(e)}"
        }
async def insert_card_version(session: AsyncSession, card_id: str, section_id: str, user_instruction: str, refinement_type: str, subsection_id: str = None) -> Dict[str, Any]:
    """
    Insert a new card version record for tracking refinement history.
    
    Args:
        session (AsyncSession): SQLAlchemy async session
        card_id (str): Primary key ID of the card being refined (cards.id) - used for parent_card_id FK
        section_id (str): Business card_id (cards.card_id) - for tracking which logical card was modified
        user_instruction (str): User instruction for the refinement
        refinement_type (str): Type of refinement - use RefinementType enum values
            (e.g., RefinementType.DELETE_SECTION.value, RefinementType.REFINE_SUBSECTION.value, etc.)
        subsection_id (str, optional): ID of the subsection being modified (null for section-level changes)
        
    Returns:
        Dict[str, Any]: Dictionary with success status and version_id
    """
    logger.info(f"Creating card version record for card_id (PK): {card_id}, section_id: {section_id}, type: {refinement_type}, subsection_id: {subsection_id}")
    
    if not card_id or not section_id or not user_instruction or not refinement_type:
        logger.error("Missing required fields for card version")
        return {
            "success": False,
            "error": "Missing required fields: card_id, section_id, user_instruction, or refinement_type"
        }
    
    try:
        # Create new card version record
        new_version = CardVersion(
            id=str(uuid7()),
            parent_card_id=card_id,
            section_id=section_id,
            subsection_id=subsection_id,
            user_instruction=user_instruction,
            refinement_type=refinement_type
        )
        
        session.add(new_version)
        await session.commit()
        
        logger.info(f"Successfully created card version with ID: {new_version.id}")
        return {
            "success": True,
            "version_id": new_version.id
        }
        
    except SQLAlchemyError as e:
        logger.error(f"Database error creating card version: {str(e)}", exc_info=True)
        await session.rollback()
        return {
            "success": False,
            "error": f"Database error: {str(e)}"
        }
    except Exception as e:
        logger.error(f"Unexpected error creating card version: {str(e)}", exc_info=True)
        await session.rollback()
        return {
            "success": False,
            "error": f"Unexpected error: {str(e)}"
        }
async def refine_card_in_db(session: AsyncSession, report_id: str, card_data: Dict[str, Any], user_instruction: str, refinement_type: str, table_id_markdown_map: Dict[str, str], subsection_id: str = None) -> Dict[str, Any]:
    """
    Refine a card by creating a new version and storing the refinement details.
    
    Args:
        session (AsyncSession): SQLAlchemy async session
        report_id (str): ID of the report containing the card
        card_data (Dict[str, Any]): The refined card data
        user_instruction (str): User instruction for the refinement
        refinement_type (str): Type of refinement - use RefinementType enum values
        table_id_markdown_map (Dict[str, str]): Mapping of table IDs to markdown
        subsection_id (str, optional): ID of the subsection being modified (null for section-level changes)
        
    Returns:
        Dict[str, Any]: Dictionary with success status and new card details
    """
    logger.info(f"Refining card for report: {report_id}, type: {refinement_type}, subsection_id: {subsection_id}")
    
    try:
        # Get the business card_id from the card data
        business_card_id = None
        if card_data.get('section') and len(card_data['section']) > 0:
            business_card_id = card_data['section'][0].get('id')
        
        if not business_card_id:
            logger.error("No business card_id found in card data")
            return {
                "success": False,
                "error": "No business card_id found in card data"
            }
        
        # Deactivate all previous versions of this card
        existing_cards_stmt = select(Card).where(
            Card.card_id == business_card_id,
            Card.report_id == report_id
        )
        existing_cards_result = await session.execute(existing_cards_stmt)
        existing_cards = existing_cards_result.scalars().all()
        
        if existing_cards:
            sequence = existing_cards[-1].sequence #since sequence will be same
            type = existing_cards[-1].type
            logger.info(f"Deactivating {len(existing_cards)} previous versions of card_id: {business_card_id}")
            for existing_card in existing_cards:
                existing_card.is_active = False
            await session.flush()
        else:
            sequence = 1
            type = "section"
        
        
        # Calculate version number using MAX(version) + 1 to avoid duplicate version numbers
        # This handles cases where user reverts and then refines (deleted versions don't cause duplicates)
        if existing_cards:
            version = max(card.version for card in existing_cards) + 1
        else:
            version = 1
        
        # Generate new primary key ID for this card instance
        new_card_id = str(uuid7())
        
        
        # Create the new refined card
        new_card = Card(
            id=new_card_id,
            card_id=business_card_id,
            report_id=report_id,
            title=card_data['section'][0].get('name'),
            sequence=sequence,
            content=card_data['section'][0],  # Use entire section structure as content
            sub_sections=card_data.get('sub_sections'),
            citations=card_data.get('citations', {}),
            created_at=datetime.now(timezone.utc),
            summary=card_data.get('summary'),
            type=type,
            version=version,
            is_active=True,
            is_deleted=False,
            changed_since_es=True,  # Refined cards have changed since last ES
            last_es_version_used=None  # Not yet used in any ES
        )
        
        session.add(new_card)
        await session.commit()
        
        # Create card version record
        version_result = await insert_card_version(
            session=session,
            card_id=new_card_id,
            section_id=business_card_id,
            user_instruction=user_instruction,
            refinement_type=refinement_type,
            subsection_id=subsection_id
        )
        
        if not version_result.get('success'):
            logger.warning(f"Failed to create card version record: {version_result.get('error')}")
        
        # Get the table_id to markdown mapping for this report
        
        # Insert tables from the updated card - always call this function
        table_insert_result = await insert_tables_from_updated_card(
            session=session,
            report_id=report_id,
            business_card_id=business_card_id,
            updated_card=card_data,
            table_id_markdown_map=table_id_markdown_map or {}
        )
        
        if not table_insert_result.get('success'):
            logger.warning(f"Failed to insert tables from updated card: {table_insert_result.get('error')}")
        else:
            logger.info(f"Successfully inserted {table_insert_result.get('total_processed', 0)} tables from updated card")
        
        logger.info(f"Successfully refined card with ID: {new_card_id}, version: {version}")
        return {
            "success": True,
            "parentcard_id": new_card_id,
            "card_id": business_card_id,
            "version": version
        }
        
    except SQLAlchemyError as e:
        logger.error(f"Database error refining card: {str(e)}", exc_info=True)
        await session.rollback()
        return {
            "success": False,
            "error": f"Database error: {str(e)}"
        }
    except Exception as e:
        logger.error(f"Unexpected error refining card: {str(e)}", exc_info=True)
        await session.rollback()
        return {
            "success": False,
            "error": f"Unexpected error: {str(e)}"
        }
async def insert_tables_from_updated_card(
    session: AsyncSession, 
    report_id: str, 
    business_card_id: str, 
    updated_card: Dict[str, Any], 
    table_id_markdown_map: Dict[str, str]
) -> Dict[str, Any]:
    """
    Extract table data from updated card and insert new table entries.
    
    Args:
        session (AsyncSession): SQLAlchemy async session
        report_id (str): ID of the report
        business_card_id (str): normal card_id
        updated_card (Dict[str, Any]): The updated card data containing sections and subsections
        table_id_markdown_map (Dict[str, str]): Mapping of table_id to markdown content
        
    Returns:
        Dict[str, Any]: Dictionary with success status and list of inserted table IDs
    """
    logger.info(f"Extracting and inserting tables from updated card for business_card_id: {business_card_id}")
    
    if not updated_card:
        logger.warning("No updated card data provided")
        return {
            "success": True,
            "inserted_tables": []
        }
    
    inserted_table_ids = []
    
    def _safe_str(value: Any) -> str:
        """Normalize polymorphic payload fields into plain strings for DB columns."""
        if value is None:
            return ""
        if isinstance(value, tuple):
            return _safe_str(value[0] if value else "")
        if isinstance(value, str):
            return value
        return str(value)
    
    def _extract_markdowns_from_content(content: Any) -> list[str]:
        """
        Extract markdown tables from section/subsection content.
        Falls back to empty list when extraction fails or content is not a plain string.
        """
        if not isinstance(content, str) or not content.strip():
            return []
        try:
            from src.core.cards.card_utils import extract_markdown_tables
            return extract_markdown_tables(content) or []
        except Exception:
            return []
    
    try:
        # Process section tables
        if updated_card.get('section') and isinstance(updated_card['section'], list):
            for section in updated_card['section']:
                section_markdowns = _extract_markdowns_from_content(section.get('content'))
                if section.get('tables') and isinstance(section['tables'], list):
                    for idx, table in enumerate(section['tables']):
                        table_id = table.get('table_id')
                        if table_id:
                            # Prefer exact map lookup by table_id; fallback to content-order markdown extraction.
                            table_markdown = ""
                            if table_id_markdown_map:
                                table_markdown = table_id_markdown_map.get(table_id, "")
                            if not table_markdown and idx < len(section_markdowns):
                                table_markdown = section_markdowns[idx]
                            if not table_markdown:
                                table_markdown = _safe_str(table.get('table_markdown', ''))
                            
                            table_data = {
                                'table_id': table_id,
                                'table_title': _safe_str(table.get('table_title', '')),
                                'table_markdown': table_markdown,
                                'visualization': table.get('visualization', ''),
                                'report_id': report_id
                            }
                            
                            logger.info(f"Inserting section table with data: {table_data}")
                            
                            # Insert the table
                            insert_result = await insert_table(
                                session=session,
                                card_id=business_card_id,  # Use the business card_id
                                table_data=table_data
                            )
                            
                            if insert_result.get('success'):
                                inserted_table_ids.append(insert_result.get('table_id'))
                                logger.info(f"Successfully inserted section table with ID: {table_id}")
                            else:
                                logger.warning(f"Failed to insert section table {table_id}: {insert_result.get('error')}")
                        else:
                            logger.warning(f"Section table ID is empty or None")
        
        # Process subsection tables
        if updated_card.get('sub_sections') and isinstance(updated_card['sub_sections'], list):
            for subsection in updated_card['sub_sections']:
                subsection_markdowns = _extract_markdowns_from_content(subsection.get('content'))
                if subsection.get('tables') and isinstance(subsection['tables'], list):
                    for idx, table in enumerate(subsection['tables']):
                        table_id = table.get('table_id')
                        if table_id:
                            # Prefer exact map lookup by table_id; fallback to content-order markdown extraction.
                            table_markdown = ""
                            if table_id_markdown_map:
                                table_markdown = table_id_markdown_map.get(table_id, "")
                            if not table_markdown and idx < len(subsection_markdowns):
                                table_markdown = subsection_markdowns[idx]
                            if not table_markdown:
                                table_markdown = _safe_str(table.get('table_markdown', ''))
                            
                            table_data = {
                                'table_id': table_id,
                                'table_title': _safe_str(table.get('table_title', '')),
                                'table_markdown': table_markdown,
                                'visualization': table.get('visualization', ''),
                                'report_id': report_id
                            }
                            
                            logger.info(f"Inserting subsection table with data: {table_data}")
                            
                            # Insert the table
                            insert_result = await insert_table(
                                session=session,
                                card_id=business_card_id,  # Use the business card_id
                                table_data=table_data
                            )
                            
                            if insert_result.get('success'):
                                inserted_table_ids.append(insert_result.get('table_id'))
                                logger.info(f"Successfully inserted subsection table with table_ID: {table_id}")
                            else:
                                logger.warning(f"Failed to insert subsection table {table_id}: {insert_result.get('error')}")
                        else:
                            logger.warning(f"Subsection table ID is empty or None")
        
        logger.info(f"Successfully processed tables for card {business_card_id}, inserted {len(inserted_table_ids)} tables")
        return {
            "success": True,
            "inserted_tables": inserted_table_ids,
            "total_processed": len(inserted_table_ids)
        }
        
    except Exception as e:
        logger.error(f"Error processing tables from updated card: {str(e)}", exc_info=True)
        return {
            "success": False,
            "error": f"Error processing tables: {str(e)}",
            "inserted_tables": inserted_table_ids
        }
async def delete_card_or_subsection(session, card_id: str, subsection_id: Optional[str], toc: str):
    """
    Delete a card (section) or subsection from the database.
    
    Args:
        session: Database session
        card_id: The business card ID to delete or modify
        subsection_id: Optional subsection ID to delete. If None, deletes the whole section
        toc: Updated table of contents string
    
    Returns:
        Dict with success status and details
    """
    try:
        # Get the active version of the card
        card_query = select(Card).where(
            Card.card_id == card_id,
            Card.is_active == True,
            Card.is_deleted == False
        )
        result = await session.execute(card_query)
        active_card = result.scalar_one_or_none()
        
        if not active_card:
            return {
                'success': False,
                'error': f'No active card found with card_id: {card_id}'
            }
        
        if subsection_id is None:
            # SCENARIO 1: Delete whole section
            # Mark the active card as deleted and inactive, and flag for ES regeneration
            active_card.is_deleted = True
            active_card.is_active = False
            active_card.changed_since_es = True  # Mark as changed to trigger ES regeneration
            active_card.last_es_version_used = None  # Will be set when ES is regenerated
            
            # Update the TOC card (assuming it's at index 2 in the cards array)
            # Find the TOC card for this report
            toc_query = select(Card).where(
                Card.report_id == active_card.report_id,
                Card.type == "toc",  # TOC is typically at sequence 3
                Card.is_active == True,
                Card.is_deleted == False
            )
            toc_result = await session.execute(toc_query)
            toc_card = toc_result.scalar_one_or_none()
            
            if toc_card:
                # Create a new version of the TOC card with updated content
                new_toc_card = Card(
                    id=str(uuid7()),
                    card_id=toc_card.card_id,
                    report_id=toc_card.report_id,
                    title=toc_card.title,
                    sequence=toc_card.sequence,
                    sub_sections=toc_card.sub_sections,
                    content={
                        "name": "table_of_contents",
                        "content": toc,
                        "tables": [{"visualization": "", "table_id": "", "table_title": ""}],
                        "id": toc_card.card_id
                    },  # Updated TOC content in JSONB structure
                    citations=toc_card.citations,
                    created_at=datetime.now(timezone.utc),
                    summary=toc_card.summary,
                    type=toc_card.type,
                    version=toc_card.version + 1,
                    is_active=True,
                    is_deleted=False,
                    changed_since_es=False,  # TOC doesn't affect ES
                    last_es_version_used=None
                )
                
                # Mark old TOC card as inactive
                toc_card.is_active = False
                
                # Insert new TOC card
                session.add(new_toc_card)
                
                # Create card version record for TOC update
                await insert_card_version(
                    session=session,
                    card_id=new_toc_card.id,
                    section_id=toc_card.card_id,
                    user_instruction="Table of contents updated after section deletion",
                    refinement_type=RefinementType.REFINE_TOC.value
                )
            
            # Create card version record for section deletion
            await insert_card_version(
                session=session,
                card_id=active_card.id,
                section_id=active_card.card_id,
                user_instruction="Section deleted by user",
                refinement_type=RefinementType.DELETE_SECTION.value
            )
            
            await session.commit()
            
            return {
                'success': True,
                'message': f'Section {card_id} deleted successfully',
                'deleted_card_id': card_id,
                'deleted_subsection_id': None,
                'toc_updated': True
            }
            
        else:
            # SCENARIO 2: Delete subsection
            # Parse the current card content to find and remove the subsection
            # current_content = active_card.content
            current_sub_sections = active_card.sub_sections
            
            if not current_sub_sections:
                return {
                    'success': False,
                    'error': f'No subsections found in card {card_id}'
                }
            
            # Find and remove the subsection with the specified ID
            subsection_found = False
            updated_sub_sections = []
            
            for subsection in current_sub_sections:
                if subsection.get('id') == subsection_id:
                    subsection_found = True
                    # Skip this subsection (don't add it to updated list)
                    continue
                else:
                    updated_sub_sections.append(subsection)
            
            if not subsection_found:
                return {
                    'success': False,
                    'error': f'Subsection {subsection_id} not found in card {card_id}'
                }
            
            # Regenerate summary without the deleted subsection
            section_content = ""
            
            # Get section name and content
            if active_card.content and isinstance(active_card.content, dict):
                section_content += active_card.content.get('name', '') + "\n\n"
                
                # Get section-level content
                content_data = active_card.content.get('content', '')
                if isinstance(content_data, dict):
                    section_content += content_data.get('content', '')
                else:
                    section_content += str(content_data)
                section_content += "\n\n"
            
            # Get all remaining subsections (excluding deleted one)
            for subsection in updated_sub_sections:
                if isinstance(subsection, dict):
                    section_content += subsection.get('name', '') + "\n"
                    subsection_content = subsection.get('content', '')
                    if isinstance(subsection_content, dict):
                        section_content += subsection_content.get('content', '')
                    else:
                        section_content += str(subsection_content)
                    section_content += "\n\n"
            
            # Generate new summary using synchronous function
            from src.core.cards.card_utils import generate_section_summary
            try:
                new_summary = generate_section_summary(section_content)
                logger.info(f"Successfully regenerated summary after subsection deletion for card {card_id}")
            except Exception as e:
                logger.error(f"Error regenerating summary after subsection deletion: {str(e)}")
                new_summary = active_card.summary  # Fall back to old summary
            
            # Create a new version of the card with the subsection removed and updated summary
            new_card = Card(
                id=str(uuid7()),
                card_id=active_card.card_id,
                report_id=active_card.report_id,
                title=active_card.title,
                sequence=active_card.sequence,
                sub_sections=updated_sub_sections,  # Updated subsections
                content=active_card.content,
                citations=active_card.citations,
                created_at=datetime.now(timezone.utc),
                summary=new_summary,  # ✅ Regenerated summary without deleted subsection
                type=active_card.type,
                version=active_card.version + 1,
                is_active=True,
                is_deleted=False,
                changed_since_es=True,  # Card changed when subsection deleted
                last_es_version_used=None  # Not yet used in any ES
            )
            
            # Mark old card as inactive
            active_card.is_active = False
            
            # Insert new card
            session.add(new_card)
            
            # Update the TOC card with the new content
            toc_query = select(Card).where(
                Card.report_id == active_card.report_id,
                Card.type == "toc",  #TOC is typically at sequence 3
                Card.is_active == True,
                Card.is_deleted == False
            )
            toc_result = await session.execute(toc_query)
            toc_card = toc_result.scalar_one_or_none()
            
            if toc_card:
                # Create a new version of the TOC card with updated content
                new_toc_card = Card(
                    id=str(uuid7()),
                    card_id=toc_card.card_id,
                    report_id=toc_card.report_id,
                    title=toc_card.title,
                    sequence=toc_card.sequence,
                    sub_sections=toc_card.sub_sections,
                    content={
                        "name": "table_of_contents",
                        "content": toc,
                        "tables": [{"visualization": "", "table_id": "", "table_title": ""}],
                        "id": toc_card.card_id
                    },  # Updated TOC content in JSONB structure
                    citations=toc_card.citations,
                    created_at=datetime.now(timezone.utc),
                    summary=toc_card.summary,
                    type=toc_card.type,
                    version=toc_card.version + 1,
                    is_active=True,
                    is_deleted=False,
                    changed_since_es=False,  # TOC doesn't affect ES
                    last_es_version_used=toc_card.last_es_version_used  # Preserve ES version tracking
                )
                
                # Mark old TOC card as inactive
                toc_card.is_active = False
                
                # Insert new TOC card
                session.add(new_toc_card)
                
                # Create card version record for TOC update
                await insert_card_version(
                    session=session,
                    card_id=new_toc_card.id,
                    section_id=toc_card.card_id,
                    user_instruction="Table of contents updated after subsection deletion",
                    refinement_type=RefinementType.REFINE_TOC.value
                )
            
            # Create card version record for subsection deletion
            await insert_card_version(
                session=session,
                card_id=new_card.id,
                section_id=new_card.card_id,
                user_instruction=f"Subsection {subsection_id} deleted by user",
                refinement_type=RefinementType.DELETE_SUBSECTION.value
            )
            
            await session.commit()
            
            return {
                'success': True,
                'message': f'Subsection {subsection_id} deleted successfully from card {card_id}',
                'deleted_card_id': card_id,
                'deleted_subsection_id': subsection_id,
                'toc_updated': True,
                'new_card_id': new_card.id
            }
            
    except Exception as e:
        await session.rollback()
        logger.error(f"Error in delete_card_or_subsection: {e}")
        return {
            'success': False,
            'error': f'Failed to delete card/subsection: {str(e)}'
        }
async def delete_visualization(session: AsyncSession, report_id: str, card_id: str, table_id: str, subsection_id: Optional[str] = None) -> Dict[str, Any]:
    """
    Delete visualization for a table by setting base64_s3_uri to NULL in the database
    and updating the card's content to remove the visualization reference.
    
    Args:
        session: Database session
        report_id: The report ID
        card_id: The business card ID that contains the table
        table_id: The table ID to delete visualization for
        subsection_id: Optional subsection ID if the table is in a subsection
    
    Returns:
        Dict with success status and details
    """
    from sqlalchemy.orm.attributes import flag_modified    
    try:
        # Get the active version of the card
        card_query = select(Card).where(
            Card.card_id == card_id,
            Card.is_active == True,
            Card.is_deleted == False
        )
        result = await session.execute(card_query)
        active_card = result.scalar_one_or_none()
        
        if not active_card:
            return {
                'success': False,
                'error': f'No active card found with card_id: {card_id}',
                'error_type': 'not_found'
            }
        
        # Get the table record using table_id and report_id
        # We don't filter by parent_card_id because the table might reference an old card version
        # This happens when a card is refined - new card version is created but table still references old version
        # Order by created_at DESC to get the latest entry if duplicates exist
        table_query = select(Table).where(
            Table.report_id == report_id,
            Table.table_id == table_id
        ).order_by(Table.created_at.desc()).limit(1)
        table_result = await session.execute(table_query)
        table = table_result.scalar_one_or_none()
        
        if not table:
            return {
                'success': False,
                'error': f'No table found with table_id: {table_id} in report: {report_id}',
                'error_type': 'not_found'
            }
        
        # Update the table's parent_card_id to point to the current active card
        # This self-healing approach fixes stale references caused by card refinement
        if table.parent_card_id != active_card.id:
            logger.info(f"Updating table {table_id} parent_card_id from {table.parent_card_id} to {active_card.id} (card was refined)")
            table.parent_card_id = active_card.id
        
        # Clear the visualization in the table record
        table.base64_s3_uri = None
        logger.info(f"Cleared base64_s3_uri for table {table_id}")
        
        # Update the visualization reference directly in the existing card content
        # Important: We need to create deep copies to ensure SQLAlchemy detects the change
        visualization_found = False
        subsection_found = False
        
        if subsection_id:
            # Update in subsection
            if not active_card.sub_sections:
                logger.error(f"Card has no subsections, but subsection_id {subsection_id} was provided")
                await session.rollback()
                return {
                    'success': False,
                    'error': 'Card has no subsections',
                    'error_type': 'not_found'
                }
            
            # Create a deep copy of sub_sections to force SQLAlchemy to detect changes
            sub_sections_copy = copy.deepcopy(active_card.sub_sections)
            
            for subsection in sub_sections_copy:
                if isinstance(subsection, dict) and subsection.get('id') == subsection_id:
                    subsection_found = True
                    for table_entry in subsection.get('tables', []):
                        if table_entry.get('table_id') == table_id:
                            # Clear both visualization fields
                            table_entry['visualization'] = ""
                            if 'visualization_type' in table_entry:
                                table_entry['visualization_type'] = ""
                            visualization_found = True
                            logger.info(f"Cleared visualization in subsection {subsection_id} for table {table_id}")
                            break
                    break
            
            if not subsection_found:
                logger.error(f"Subsection with id {subsection_id} not found in card {card_id}")
                await session.rollback()
                return {
                    'success': False,
                    'error': f'Subsection with id {subsection_id} not found in card',
                    'error_type': 'not_found'
                }
            
            if not visualization_found:
                logger.error(f"Table {table_id} not found in subsection {subsection_id}")
                await session.rollback()
                return {
                    'success': False,
                    'error': f'Table not found in subsection with id {subsection_id}',
                    'error_type': 'not_found'
                }
            
            # Reassign to trigger SQLAlchemy change detection
            active_card.sub_sections = sub_sections_copy
            # Mark the JSONB field as modified
            flag_modified(active_card, 'sub_sections')
        else:
            # Update in section content
            if not isinstance(active_card.content, dict):
                logger.error("Card content is not a dict, cannot update visualization")
                await session.rollback()
                return {
                    'success': False,
                    'error': 'Invalid card content structure',
                    'error_type': 'invalid_data'
                }
            
            # Create a deep copy of content to force SQLAlchemy to detect changes
            content_copy = copy.deepcopy(active_card.content)
            
            for table_entry in content_copy.get('tables', []):
                if table_entry.get('table_id') == table_id:
                    # Clear both visualization fields
                    table_entry['visualization'] = ""
                    if 'visualization_type' in table_entry:
                        table_entry['visualization_type'] = ""
                    visualization_found = True
                    logger.info(f"Cleared visualization in section content for table {table_id}")
                    break
            
            if not visualization_found:
                logger.error(f"Table {table_id} not found in section content")
                await session.rollback()
                return {
                    'success': False,
                    'error': 'Table not found in section content',
                    'error_type': 'not_found'
                }
            
            # Reassign to trigger SQLAlchemy change detection
            active_card.content = content_copy
            # Mark the JSONB field as modified
            flag_modified(active_card, 'content')
        
        # Update timestamp to mark card as modified (for detect_modified_cards)
        active_card.updated_at = datetime.now(timezone.utc)
        logger.info(f"Updated card {card_id} timestamp to mark visualization deletion")
        
        # Flush to ensure changes are written to the database session
        # await session.flush()
        # logger.info(f"Flushed changes for table {table_id}")
        
        # Commit the transaction
        await session.commit()
        logger.info(f"Successfully deleted visualization for table {table_id}")
        
        return {
            'success': True,
            'message': f'Visualization deleted successfully for table {table_id}',
            'card_id': card_id,
            'table_id': table_id,
            'subsection_id': subsection_id
        }
        
    except Exception as e:
        await session.rollback()
        logger.error(f"Error in delete_visualization: {e}")
        return {
            'success': False,
            'error': f'Failed to delete visualization: {str(e)}',
            'error_type': 'server_error'
        }
async def revert_card_to_version(
    session: AsyncSession,
    report_id: str,
    card_id: str
) -> Dict[str, Any]:
    """
    Revert a card to its immediate previous version (undo last refinement).
    
    User can only undo to the immediate previous version (v3 → v2, not v3 → v1).
    Current version is marked as is_active=False AND is_deleted=True.
    Previous version is reactivated.
    
    Args:
        session: Database session
        report_id: ID of the report
        card_id: Business card_id (not primary key)
        
    Returns:
        Dict with success status and revert details
    """
    try:
        # Get all versions of this card ordered by version (including deleted ones for version calculation)
        cards_stmt = select(Card).where(
            Card.card_id == card_id,
            Card.report_id == report_id
        ).order_by(Card.version.desc())
        
        cards_result = await session.execute(cards_stmt)
        all_cards = cards_result.scalars().all()
        
        if not all_cards:
            return {
                "success": False,
                "error": f"No cards found with card_id: {card_id} in report: {report_id}"
            }
        
        # Find current active card
        active_card = next((c for c in all_cards if c.is_active and not c.is_deleted), None)
        if not active_card:
            return {
                "success": False,
                "error": f"No active card found with card_id: {card_id}"
            }
        
        current_version = active_card.version
        
        # Cannot revert version 1 (no previous version)
        if current_version == 1:
            return {
                "success": False,
                "error": "Cannot revert version 1 - it's the first version"
            }
        
        # Find the immediate previous non-deleted version
        # Sort by version descending, find the first one before current that's not deleted
        previous_card = None
        for card in all_cards:
            if card.version < current_version and not card.is_deleted:
                previous_card = card
                break  # Already sorted desc, so first match is the highest version before current
        
        if not previous_card:
            return {
                "success": False,
                "error": f"No previous version found to revert to (all versions before {current_version} are deleted)"
            }
        
        # Deactivate ALL versions first (to handle cases where multiple versions are incorrectly active)
        logger.info(f"Deactivating all versions of card_id: {card_id} before reverting")
        for card in all_cards:
            card.is_active = False
        
        # Mark current active card as deleted
        active_card.is_deleted = True
        
        # Activate previous version card
        previous_card.is_active = True
        
        # Set ES tracking flags - reverting a card means content has changed
        previous_card.changed_since_es = True
        previous_card.last_es_version_used = None
        
        await session.commit()
        
        # Refresh to get latest data
        await session.refresh(previous_card)
        
        logger.info(f"Reverted card {card_id} from version {current_version} to version {previous_card.version}")
        
        # Build card structure matching refine-card response format
        reverted_card_data = {
            "section": [previous_card.content] if previous_card.content else [],
            "sub_sections": previous_card.sub_sections if previous_card.sub_sections else [],
            "citations": previous_card.citations if previous_card.citations else {},
            "summary": previous_card.summary
        }
        
        return {
            "success": True,
            "message": f"Successfully reverted card from version {current_version} to version {previous_card.version}",
            "reverted_to_version": previous_card.version,
            "card_id": card_id,
            "primary_card_id": previous_card.id,
            "deleted_versions": [current_version],
            "reverted_card": reverted_card_data
        }
        
    except SQLAlchemyError as e:
        await session.rollback()
        logger.error(f"Database error reverting card: {str(e)}", exc_info=True)
        return {
            "success": False,
            "error": f"Database error: {str(e)}"
        }
    except Exception as e:
        await session.rollback()
        logger.error(f"Unexpected error reverting card: {str(e)}", exc_info=True)
        return {
            "success": False,
            "error": f"Unexpected error: {str(e)}"
        }
async def get_latest_card_version(session: AsyncSession, card_id: str) -> Dict[str, Any]:
    """
    Get the latest version of a card from the database.
    
    Args:
        session (AsyncSession): SQLAlchemy async session
        card_id (str): The ID of the card to get the latest version of
        
    Returns:
        Dict[str, Any]: Dictionary with success status and card data
    """
    logger.info(f"Getting latest version of card with ID: {card_id}")
    try:
        # Query to get the latest version of the card
        stmt = select(Card).where(Card.card_id == card_id, Card.is_active == True, Card.is_deleted == False)
        result = await session.execute(stmt)
        card = result.scalar_one_or_none()
        
        if not card:
            logger.warning(f"No card found with ID: {card_id}")
            return {
                "success": True,
                "version": None
            }
        
        return {
            "success": True,
            "version": card.version
        }
    except SQLAlchemyError as e:
        logger.error(f"Database error getting latest card version: {str(e)}", exc_info=True)
        return {
            "success": False,
            "error": f"Database error: {str(e)}"
        }
    except Exception as e:
        logger.error(f"Unexpected error getting latest card version: {str(e)}", exc_info=True)
        return {
            "success": False,
            "error": f"Unexpected error: {str(e)}"
        }
async def get_cards_for_version(
    report_id: str,
    version_id: str,
    session: AsyncSession
) -> Dict[str, Any]:
    """
    Get cards linked to a specific report version.
    Uses report_version_cards junction table to get the exact cards
    that were locked when the version was finalized.
    
    Args:
        report_id: ID of the report
        version_id: ID of the specific version
        session: Database session
        
    Returns:
        Dict with success status and list of cards
    """
    try:
        logger.info(f"Getting cards for version_id: {version_id}")

        # Single query: join through ReportVersionCard, order by the junction
        # table's sequence, and eager-load tables for each card. Previously this
        # path issued N extra queries (2 per card) via get_tables_for_card.
        cards_stmt = (
            select(Card)
            .options(selectinload(Card.tables))
            .join(ReportVersionCard, ReportVersionCard.parent_card_id == Card.id)
            .where(ReportVersionCard.report_version_id == version_id)
            .order_by(ReportVersionCard.sequence)
        )

        result = await session.execute(cards_stmt)
        cards = result.scalars().all()

        if not cards:
            logger.warning(f"No cards linked to version {version_id}, falling back to active cards")
            return await get_report_cards(report_id=report_id, session=session)

        cards_data = []
        for card in cards:
            tables_data = [
                {
                    "table_id": t.table_id,
                    "table_title": _normalize_table_title_value(t.table_title),
                    "table_markdown": t.table_markdown,
                    "s3_uri": t.base64_s3_uri,
                }
                for t in card.tables
            ]

            sub_sections_data = _normalize_subsections_for_response(card.sub_sections)

            if isinstance(card.content, dict):
                content_text = card.content.get('content', '')
                content_name = card.content.get('name', card.title or '')
                content_tables = _normalize_tables_for_response(card.content.get('tables', tables_data))
            else:
                content_text = card.content or ''
                content_name = card.title or ''
                content_tables = tables_data

            card_data = {
                "id": card.card_id,
                "report_id": card.report_id,
                "sequence": card.sequence,
                "citations": card.citations,
                "created_at": card.created_at.isoformat() if card.created_at else None,
                "summary": card.summary,
                "type": card.type,
                "section": [
                    {
                        "name": content_name,
                        "content": content_text,
                        "tables": content_tables
                    }
                ],
                "sub_sections": sub_sections_data
            }
            cards_data.append(card_data)

        logger.info(f"Successfully retrieved {len(cards_data)} cards for version {version_id}")
        return {
            "success": True,
            "cards": cards_data,
            "from_version_snapshot": True
        }
        
    except Exception as e:
        logger.error(f"Error getting cards for version: {str(e)}", exc_info=True)
        return {"success": False, "error": str(e)}
async def get_refinement_history(report_id: str, session: AsyncSession) -> Dict[str, Any]:
    """
    Fetch the current refinement history for a report from the refinement_history table.
    
    Args:
        report_id (str): ID of the report.
        session (AsyncSession): SQLAlchemy async session.
    
    Returns:
        Dict[str, Any]: Dictionary with success status and refine_history (list or None).
    """
    logger.info(f"Fetching refinement history for report_id: {report_id}")
    
    try:
        stmt = select(RefinementHistory).where(RefinementHistory.report_id == report_id)
        result = await session.execute(stmt)
        refinement_record = result.scalar_one_or_none()
        
        if refinement_record is None:
            logger.warning(f"No refinement history record found for report_id: {report_id}")
            return {
                "success": True,
                "refine_history": []
            }
        
        refine_history = refinement_record.refine_history or []
        logger.info(f"Found refinement history with {len(refine_history)} entries for report_id: {report_id}")
        return {
            "success": True,
            "refine_history": refine_history
        }
    
    except SQLAlchemyError as e:
        logger.error(f"Database error fetching refinement history: {str(e)}", exc_info=True)
        return {"success": False, "error": f"Database error: {str(e)}"}
    except Exception as e:
        logger.error(f"Unexpected error fetching refinement history: {str(e)}", exc_info=True)
        return {"success": False, "error": f"Unexpected error: {str(e)}"}
async def update_refinement_history(report_id: str, refine_history: list, session: AsyncSession) -> Dict[str, Any]:
    """
    Update the refinement history JSONB field for a report.
    
    Note: This function uses flush() instead of commit() to allow the caller 
    to control transaction boundaries. The caller MUST commit or rollback.
    
    Args:
        report_id (str): ID of the report.
        refine_history (list): Updated refinement history list to store.
        session (AsyncSession): SQLAlchemy async session.
    
    Returns:
        Dict[str, Any]: Dictionary with success status.
    """
    logger.info(f"Updating refinement history for report_id: {report_id} with {len(refine_history)} entries")
    
    try:
        stmt = select(RefinementHistory).where(RefinementHistory.report_id == report_id)
        result = await session.execute(stmt)
        refinement_record = result.scalar_one_or_none()
        
        if refinement_record is None:
            logger.warning(f"No refinement history record found for report_id: {report_id}, creating one")
            refinement_record = RefinementHistory(
                id=str(uuid7()),
                report_id=report_id,
                refine_history=refine_history
            )
            session.add(refinement_record)
        else:
            refinement_record.refine_history = refine_history
        
        await session.flush()
        logger.info(f"Successfully staged refinement history update for report_id: {report_id}")
        return {"success": True}
    
    except SQLAlchemyError as e:
        logger.error(f"Database error updating refinement history: {str(e)}", exc_info=True)
        return {"success": False, "error": f"Database error: {str(e)}"}
    except Exception as e:
        logger.error(f"Unexpected error updating refinement history: {str(e)}", exc_info=True)
        return {"success": False, "error": f"Unexpected error: {str(e)}"}
