"""String and JSONB sanitisation for values bound for PostgreSQL.

Moved verbatim from ``app.core.db.py`` during the R-STRUCT-1 migration.
These strip null bytes and control characters that PostgreSQL's UTF-8
encoding rejects during JSONB conversion; they are unrelated to session
management, which is why they live apart from ``app/core/db.py``.
"""

import re
from typing import Any

from app.core.logging import setup_logging

logger = setup_logging(__file__)


def sanitize_string(text: Any) -> Any:
    """
    Sanitize a string by removing invalid Unicode characters that PostgreSQL cannot handle.

    This function specifically handles:
    - Null bytes (0x00) which PostgreSQL UTF-8 encoding doesn't support
    - Other control characters that can cause database insertion issues
    - Preserves None values

    Args:
        text (Any): Input text to sanitize. Can be str, None, or other types.

    Returns:
        Any: Sanitized string if input was a string, otherwise returns the input unchanged.

    Examples:
        >>> sanitize_string("Hello\\x00World")
        'HelloWorld'
        >>> sanitize_string(None)
        None
        >>> sanitize_string(123)
        123
    """
    if text is None:
        return None

    if not isinstance(text, str):
        return text

    try:
        # Remove null bytes (0x00) - the most common issue with PostgreSQL
        sanitized = text.replace("\x00", "")

        # Remove other problematic control characters (except newline, tab, carriage return)
        # This pattern keeps printable characters, newlines, tabs, and carriage returns
        # but removes other control characters (0x01-0x08, 0x0B-0x0C, 0x0E-0x1F)
        sanitized = re.sub(r"[\x01-\x08\x0B-\x0C\x0E-\x1F\x7F]", "", sanitized)

        return sanitized
    except Exception as e:
        logger.warning(f"Error sanitizing string: {e!s}, returning original text")
        return text


def sanitize_json(data: Any) -> Any:
    """
    Recursively sanitize JSON data structures (dicts, lists) by removing invalid Unicode characters.

    This function traverses through nested JSON structures and applies string sanitization
    to all string values while preserving the structure and non-string values.

    Args:
        data (Any): Input data to sanitize. Can be dict, list, str, or any JSON-serializable type.

    Returns:
        Any: Sanitized data structure with the same type and structure as input.

    Examples:
        >>> sanitize_json({"name": "Test\\x00Data", "count": 5})
        {'name': 'TestData', 'count': 5}
        >>> sanitize_json([{"id": "abc\\x00def"}, {"id": "xyz"}])
        [{'id': 'abcdef'}, {'id': 'xyz'}]
        >>> sanitize_json(None)
        None
    """
    if data is None:
        return None

    try:
        if isinstance(data, dict):
            # Recursively sanitize dictionary values
            return {key: sanitize_json(value) for key, value in data.items()}

        elif isinstance(data, list):
            # Recursively sanitize list items
            return [sanitize_json(item) for item in data]

        elif isinstance(data, str):
            # Sanitize string values
            return sanitize_string(data)

        else:
            # Return other types unchanged (int, float, bool, etc.)
            return data

    except Exception as e:
        logger.warning(f"Error sanitizing JSON data: {e!s}, returning original data")
        return data


def sanitize_content_field(content_data: Any) -> Any:
    """
    Sanitize only the 'content' field within a content/section structure.

    Structure example:
    {
        "id": "...",
        "name": "...",
        "tables": [...],
        "content": "text to sanitize"
    }

    This function sanitizes ONLY the "content" key value, leaving other fields untouched.

    Args:
        content_data (Any): Content structure (dict) or any other type

    Returns:
        Any: Content structure with only the "content" field sanitized
    """
    if content_data is None:
        return None

    if not isinstance(content_data, dict):
        return content_data

    try:
        # Create a copy to avoid modifying the original
        sanitized = content_data.copy()

        # Only sanitize the 'content' field if it exists
        if "content" in sanitized:
            sanitized["content"] = sanitize_string(sanitized["content"])

        return sanitized

    except Exception as e:
        logger.warning(f"Error sanitizing content field: {e!s}, returning original data")
        return content_data


def sanitize_subsections_field(subsections_data: Any) -> Any:
    """
    Sanitize only the 'content' field within each subsection in the subsections list.

    Structure example:
    [
        {
            "id": "...",
            "name": "...",
            "tables": [...],
            "content": "text to sanitize"
        },
        ...
    ]

    This function sanitizes ONLY the "content" key value in each subsection dict.

    Args:
        subsections_data (Any): List of subsection dicts or any other type

    Returns:
        Any: List of subsections with only the "content" field sanitized in each
    """
    if subsections_data is None:
        return None

    if not isinstance(subsections_data, list):
        return subsections_data

    try:
        sanitized_subsections = []

        for subsection in subsections_data:
            if isinstance(subsection, dict):
                # Create a copy of the subsection
                sanitized_subsection = subsection.copy()

                # Only sanitize the 'content' field if it exists
                if "content" in sanitized_subsection:
                    sanitized_subsection["content"] = sanitize_string(
                        sanitized_subsection["content"]
                    )

                sanitized_subsections.append(sanitized_subsection)
            else:
                # If it's not a dict, keep it as is
                sanitized_subsections.append(subsection)

        return sanitized_subsections

    except Exception as e:
        logger.warning(f"Error sanitizing subsections: {e!s}, returning original data")
        return subsections_data


def sanitize_card_data(card_data: dict[str, Any]) -> dict[str, Any]:
    """
    Sanitize JSONB fields in a card data dictionary before database insertion.

    This function specifically handles the card data structure used in the application,
    sanitizing ONLY the JSONB fields to prevent PostgreSQL encoding errors during
    JSONB to bytes conversion.

    JSONB Fields sanitized:
    - title (String/Text column)
    - summary (Text column)
    - content (JSONB object - only the nested "content" field is sanitized)
    - sub_sections (JSONB array - only the nested "content" field in each subsection)
    - citations (JSONB object - all fields sanitized)

    For content and sub_sections, only the "content" field within the nested structure
    is sanitized, leaving other fields like "id", "name", "tables" untouched.

    Args:
        card_data (Dict[str, Any]): Card data dictionary with all fields

    Returns:
        Dict[str, Any]: Sanitized card data dictionary ready for database insertion

    Note:
        This function modifies a copy of the input dictionary, not the original.
    """
    try:
        # Create a shallow copy to avoid modifying the original
        sanitized = card_data.copy()

        # Sanitize string fields
        if "title" in sanitized:
            sanitized["title"] = sanitize_string(sanitized["title"])

        if "summary" in sanitized:
            sanitized["summary"] = sanitize_string(sanitized["summary"])

        # Sanitize only the 'content' field within the content structure
        if "content" in sanitized:
            sanitized["content"] = sanitize_content_field(sanitized["content"])

        # Sanitize only the 'content' field within each subsection
        if "sub_sections" in sanitized:
            sanitized["sub_sections"] = sanitize_subsections_field(sanitized["sub_sections"])

        # Sanitize all fields in citations (unchanged behavior)
        if "citations" in sanitized:
            sanitized["citations"] = sanitize_json(sanitized["citations"])

        logger.debug("Successfully sanitized JSONB fields in card data")
        return sanitized

    except Exception as e:
        logger.error(f"Error sanitizing card data: {e!s}, returning original data", exc_info=True)
        return card_data
