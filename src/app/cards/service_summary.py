"""
Executive Summary Updater Module

This module provides functionality to update executive summaries based on changes
in report cards/sections. It intelligently updates only the portions corresponding
to refined cards while preserving information about unchanged cards.

The module uses a two-tier fallback approach:
1. OpenAI (gpt-4o) - Primary API
2. Gemini - Fallback with retry logic (Perplexity fallback disabled, see below)

All APIs use structured JSON output for consistent response formatting.
"""

import json
import time

from google import genai
from openai import OpenAI

from app.core.constants import (
    CARD_SUMMARY_MODEL,
    GEMINI_API_KEY,
    GEMINI_ES_MODEL_ID,
    OPENAI_API_KEY,
)
from app.core.logging import setup_logging
from app.observability.llm_response_logger import save_raw_llm_response, strip_json_code_fence
from app.research.prompts.prompt_utils import (
    # UPDATE_EXECUTIVE_SUMMARY_SCHEMA_PERPLEXITY,  # DISABLED: superseded by Gemini fallback
    CHECK_ES_UPDATE_REQUIRED_PROMPT,
    CHECK_ES_UPDATE_REQUIRED_SCHEMA_GEMINI,
    # CHECK_ES_UPDATE_REQUIRED_SCHEMA_PERPLEXITY,  # DISABLED: superseded by Gemini fallback
    CHECK_ES_UPDATE_REQUIRED_SCHEMA_OPENAI,
    UPDATE_EXECUTIVE_SUMMARY_PROMPT,
    UPDATE_EXECUTIVE_SUMMARY_SCHEMA_GEMINI,
    UPDATE_EXECUTIVE_SUMMARY_SCHEMA_OPENAI,
)

logger = setup_logging(__file__)


def check_if_update_required(
    cards_info: str,
    current_executive_summary: str,
    max_retries: int = 3,
    retry_delay: int = 5,
    chat_id: str | None = None,
    user_id: str | None = None,
) -> tuple[bool, str]:
    """
    Check if updating the executive summary is required based on changes in refined cards.

    Args:
        cards_info: Formatted string containing information about all cards and their changes
        current_executive_summary: The current executive summary
        max_retries: Number of retry attempts for fallback APIs
        retry_delay: Delay in seconds between retries

    Returns:
        tuple[bool, str]: (update_required, reasoning)
            - update_required: True if ES needs update, False otherwise
            - reasoning: Explanation for the decision
    """
    logger.info("Checking if executive summary update is required")

    formatted_prompt = CHECK_ES_UPDATE_REQUIRED_PROMPT.format(
        cards_info=cards_info, current_executive_summary=current_executive_summary
    )

    # Try OpenAI first
    try:
        logger.info("Using OpenAI API to check update requirement")
        client = OpenAI(api_key=OPENAI_API_KEY, timeout=120)

        response = client.chat.completions.create(
            model=CARD_SUMMARY_MODEL,
            messages=[{"role": "user", "content": formatted_prompt}],
            tools=[CHECK_ES_UPDATE_REQUIRED_SCHEMA_OPENAI],
            tool_choice={
                "type": "function",
                "function": {"name": "check_executive_summary_update_required"},
            },
        )
        save_raw_llm_response(
            response,
            CARD_SUMMARY_MODEL,
            "Checking whether the executive summary needs an update",
            chat_id=chat_id,
            user_id=user_id,
        )

        result = json.loads(response.choices[0].message.tool_calls[0].function.arguments)
        update_required = result["update_required"]
        reasoning = result["reasoning"]
        logger.info(f"OpenAI decision: update_required={update_required}, reasoning={reasoning}")
        return update_required, reasoning

    except Exception as e:
        logger.error(f"Error with OpenAI API: {e!s}")
        logger.info("Falling back to Gemini API")

    # Try Gemini as second fallback
    try:
        logger.info("Using Gemini API to check update requirement")
        gemini_client = genai.Client(api_key=GEMINI_API_KEY)

        interaction = gemini_client.interactions.create(
            model=GEMINI_ES_MODEL_ID,
            input=formatted_prompt,
            response_format={
                "type": "text",
                "mime_type": "application/json",
                "schema": CHECK_ES_UPDATE_REQUIRED_SCHEMA_GEMINI,
            },
            generation_config={
                "max_output_tokens": 2000,
                "thinking_config": {"thinking_budget": 0},
            },
        )
        save_raw_llm_response(
            interaction,
            GEMINI_ES_MODEL_ID,
            "Checking whether the executive summary needs an update (backup)",
            chat_id=chat_id,
            user_id=user_id,
        )

        result = json.loads(strip_json_code_fence(interaction.output_text))
        update_required = result["update_required"]
        reasoning = result["reasoning"]
        logger.info(f"Gemini decision: update_required={update_required}, reasoning={reasoning}")
        return update_required, reasoning

    except Exception as e:
        logger.error(f"Error with Gemini API: {e!s}")
        logger.info("Retrying with Gemini API")

    # DISABLED: Perplexity final fallback, superseded by the Gemini retry loop below
    # (kept for reference/rollback).
    # payload = {
    #     "model": "sonar-pro",
    #     "messages": [{"role": "user", "content": formatted_prompt}],
    #     "response_format": {
    #         "type": "json_schema",
    #         "json_schema": {"schema": CHECK_ES_UPDATE_REQUIRED_SCHEMA_PERPLEXITY}
    #     },
    #     "temperature": 0.1,
    #     "max_tokens": 2000
    # }
    # headers = {
    #     "Authorization": f"Bearer {PERPLEXITY_API_KEY}",
    #     "Content-Type": "application/json"
    # }
    # resp = requests.post("https://api.perplexity.ai/chat/completions", headers=headers, json=payload)
    # response_content = resp.json()["choices"][0]["message"]["content"]

    # Retry Gemini as the final fallback tier (with backoff)
    retry_count = 0
    while retry_count < max_retries:
        try:
            logger.info(
                f"Retrying Gemini API to check update requirement (attempt {retry_count + 1}/{max_retries})"
            )
            gemini_client = genai.Client(api_key=GEMINI_API_KEY)

            interaction = gemini_client.interactions.create(
                model=GEMINI_ES_MODEL_ID,
                input=formatted_prompt,
                response_format={
                    "type": "text",
                    "mime_type": "application/json",
                    "schema": CHECK_ES_UPDATE_REQUIRED_SCHEMA_GEMINI,
                },
                generation_config={
                    "temperature": 0.1,
                    "max_output_tokens": 2000,
                    "thinking_config": {"thinking_budget": 0},
                },
            )
            save_raw_llm_response(
                interaction,
                GEMINI_ES_MODEL_ID,
                "Checking whether the executive summary needs an update (backup)",
                chat_id=chat_id,
                user_id=user_id,
            )

            result = json.loads(strip_json_code_fence(interaction.output_text))
            update_required = result["update_required"]
            reasoning = result["reasoning"]
            logger.info(
                f"Gemini decision: update_required={update_required}, reasoning={reasoning}"
            )
            return update_required, reasoning

        except Exception as e:
            retry_count += 1
            logger.warning(f"Gemini API attempt {retry_count}/{max_retries} failed: {e!s}")
            if retry_count < max_retries:
                logger.info(f"Retrying in {retry_delay} seconds...")
                time.sleep(retry_delay)

    # All APIs failed
    logger.error("All API attempts failed for update check")
    logger.warning("Defaulting to update_required=True due to API failures")
    return True, "Unable to determine; defaulting to update for safety"


def update_executive_summary(
    cards: list[dict[str, str | None]],
    current_executive_summary: str,
    report_id: str,
    max_retries: int = 3,
    retry_delay: int = 5,
    user_id: str | None = None,
) -> str:
    """
    Update the executive summary based on changes in refined cards.

    This function intelligently updates only the portions of an executive summary
    that correspond to cards that have been refined, while preserving information
    about unchanged cards. It uses OpenAI as the primary API, with Gemini as
    the (retried) fallback.

    Args:
        cards: List of dictionaries with keys:
            - card_title: str (title of the card/section)
            - current_summary: str (current summary of the card)
            - previous_summary: Optional[str] (previous summary if card was refined, None otherwise)
            - is_deleted: bool (whether the card has been deleted since last ES)
        current_executive_summary: str (the current executive summary to update)
        report_id: str (report ID for logging purposes)
        max_retries: int (number of retry attempts for fallback APIs)
        retry_delay: int (delay in seconds between retries)

    Returns:
        str: Updated executive summary

    Example:
        >>> cards = [
        ...     {
        ...         "card_title": "Introduction",
        ...         "current_summary": "Introduction discusses AI basics...",
        ...         "previous_summary": None,  # Not refined
        ...         "is_deleted": False
        ...     },
        ...     {
        ...         "card_title": "Market Analysis",
        ...         "current_summary": "Market valued at $200B in 2024...",
        ...         "previous_summary": "Market valued at $150B in 2023...",  # Refined
        ...         "is_deleted": False
        ...     },
        ...     {
        ...         "card_title": "Deprecated Section",
        ...         "current_summary": "Old content...",
        ...         "previous_summary": "Old content...",
        ...         "is_deleted": True  # Deleted - should be removed from ES
        ...     }
        ... ]
        >>> current_exec = "Report covers AI and market growth..."
        >>> updated_exec = update_executive_summary(cards, current_exec, report_id="report_123")
    """
    try:
        logger.info(f"Updating executive summary for {len(cards)} cards for report_id: {report_id}")

        # Format cards information for the prompt
        cards_info = ""
        refined_count = 0
        deleted_count = 0

        for idx, card in enumerate(cards, 1):
            card_title = card.get("card_title", f"Card {idx}")
            current_summary = card.get("current_summary", "")
            previous_summary = card.get("previous_summary")
            is_deleted = card.get("is_deleted", False)

            if is_deleted:
                # Handle deleted cards
                deleted_count += 1
                refined_count += 1  # Count as a change
                cards_info += f"\n{'=' * 80}\n"
                cards_info += f"### ❌ DELETED CARD {idx}: {card_title}\n"
                cards_info += f"{'=' * 80}\n\n"
                cards_info += f"**Previous Summary (BEFORE deletion):**\n{previous_summary or current_summary}\n\n"
                cards_info += "**ACTION REQUIRED:** This section has been DELETED by the user.\n"
                cards_info += (
                    f"Remove ALL references to '{card_title}' from the executive summary.\n"
                )
                cards_info += (
                    "Ensure the executive summary flows naturally after removing this section.\n"
                )
                cards_info += f"{'=' * 80}\n\n"
            elif previous_summary:
                # Handle refined cards
                refined_count += 1
                cards_info += f"\n{'=' * 80}\n"
                cards_info += f"### ⚠️ REFINED CARD {idx}: {card_title}\n"
                cards_info += f"{'=' * 80}\n\n"
                cards_info += f"**PREVIOUS Summary (BEFORE refinement):**\n{previous_summary}\n\n"
                cards_info += f"**CURRENT Summary (AFTER refinement):**\n{current_summary}\n\n"
                cards_info += (
                    "**CHANGES:** Compare the above two summaries to identify what changed.\n"
                )
                cards_info += f"{'=' * 80}\n\n"
            else:
                # Handle unchanged cards
                cards_info += f"\n### Card {idx}: {card_title} (NOT REFINED)\n"
                cards_info += f"**Summary:**\n{current_summary}\n"
                cards_info += "**Status:** This card was NOT refined - keep its information unchanged in ES.\n\n"

        logger.info(
            f"Found {refined_count} changed cards ({deleted_count} deleted) out of {len(cards)} total cards for report_id: {report_id}"
        )

        # If no cards were refined or deleted, return the current executive summary unchanged
        if refined_count == 0:
            logger.info(
                f"No cards were refined or deleted, returning current executive summary unchanged for report_id: {report_id}"
            )
            return current_executive_summary

        # Check if update is actually required
        update_required, reasoning = check_if_update_required(
            cards_info,
            current_executive_summary,
            max_retries,
            retry_delay,
            chat_id=report_id,
            user_id=user_id,
        )

        if not update_required:
            logger.info(f"Update not required: {reasoning} for report_id: {report_id}")
            return current_executive_summary

        logger.info(
            f"Update required: {reasoning}. Proceeding with executive summary update... for report_id: {report_id}"
        )

        formatted_prompt = UPDATE_EXECUTIVE_SUMMARY_PROMPT.format(
            cards_info=cards_info,
            current_executive_summary=current_executive_summary,
            update_reasoning=reasoning,
        )

        # Try OpenAI first
        try:
            logger.info(
                f"Attempting to update executive summary using OpenAI API for report_id: {report_id}"
            )
            client = OpenAI(api_key=OPENAI_API_KEY, timeout=120)

            response = client.chat.completions.create(
                model=CARD_SUMMARY_MODEL,
                messages=[{"role": "user", "content": formatted_prompt}],
                tools=[UPDATE_EXECUTIVE_SUMMARY_SCHEMA_OPENAI],
                tool_choice={"type": "function", "function": {"name": "update_executive_summary"}},
            )
            save_raw_llm_response(
                response,
                CARD_SUMMARY_MODEL,
                "Updating the executive summary after report changes",
                chat_id=report_id,
                user_id=user_id,
            )

            result = json.loads(response.choices[0].message.tool_calls[0].function.arguments)
            updated_summary = result["updated_executive_summary"]
            logger.info(
                f"Successfully updated executive summary with OpenAI API for report_id: {report_id}"
            )
            return updated_summary

        except Exception as e:
            logger.error(f"Error with OpenAI API: {e!s} for report_id: {report_id}")
            logger.info(f"Falling back to Gemini API for report_id: {report_id}")

        # Try Gemini as second fallback
        try:
            logger.info(
                f"Attempting to update executive summary using Gemini API for report_id: {report_id}"
            )
            gemini_client = genai.Client(api_key=GEMINI_API_KEY)

            interaction = gemini_client.interactions.create(
                model=GEMINI_ES_MODEL_ID,
                input=formatted_prompt,
                response_format={
                    "type": "text",
                    "mime_type": "application/json",
                    "schema": UPDATE_EXECUTIVE_SUMMARY_SCHEMA_GEMINI,
                },
                generation_config={
                    "max_output_tokens": 16000,
                    "thinking_config": {"thinking_budget": 0},
                },
            )
            save_raw_llm_response(
                interaction,
                GEMINI_ES_MODEL_ID,
                "Updating the executive summary after report changes (backup)",
                chat_id=report_id,
                user_id=user_id,
            )

            result = json.loads(strip_json_code_fence(interaction.output_text))
            updated_summary = result["updated_executive_summary"]
            logger.info(
                f"Successfully updated executive summary with Gemini API for report_id: {report_id}"
            )
            return updated_summary

        except Exception as e:
            logger.error(f"Error with Gemini API: {e!s} for report_id: {report_id}")
            logger.info(f"Retrying with Gemini API for report_id: {report_id}")

        # DISABLED: Perplexity final fallback, superseded by the Gemini retry loop below
        # (kept for reference/rollback).
        # payload = {
        #     "model": "sonar-pro",
        #     "messages": [{"role": "user", "content": formatted_prompt}],
        #     "response_format": {
        #         "type": "json_schema",
        #         "json_schema": {"schema": UPDATE_EXECUTIVE_SUMMARY_SCHEMA_PERPLEXITY}
        #     },
        #     "temperature": 0.1,
        #     "max_tokens": 8000
        # }
        # headers = {
        #     "Authorization": f"Bearer {PERPLEXITY_API_KEY}",
        #     "Content-Type": "application/json"
        # }
        # resp = requests.post("https://api.perplexity.ai/chat/completions", headers=headers, json=payload)
        # response_content = resp.json()["choices"][0]["message"]["content"]

        # Retry Gemini as the final fallback tier (with backoff)
        retry_count = 0
        while retry_count < max_retries:
            try:
                logger.info(
                    f"Retrying Gemini API (attempt {retry_count + 1}/{max_retries}) for report_id: {report_id}"
                )
                gemini_client = genai.Client(api_key=GEMINI_API_KEY)

                interaction = gemini_client.interactions.create(
                    model=GEMINI_ES_MODEL_ID,
                    input=formatted_prompt,
                    response_format={
                        "type": "text",
                        "mime_type": "application/json",
                        "schema": UPDATE_EXECUTIVE_SUMMARY_SCHEMA_GEMINI,
                    },
                    generation_config={
                        "temperature": 0.1,
                        "max_output_tokens": 16000,
                        "thinking_config": {"thinking_budget": 0},
                    },
                )
                save_raw_llm_response(
                    interaction,
                    GEMINI_ES_MODEL_ID,
                    "Updating the executive summary after report changes (backup)",
                    chat_id=report_id,
                    user_id=user_id,
                )

                response_content = strip_json_code_fence(interaction.output_text)

                # Note: a JSONDecodeError here (e.g. truncated output) is intentionally
                # NOT swallowed - it's raised so the retry loop below treats it like any
                # other failed attempt, instead of silently returning corrupted raw text.
                result = json.loads(response_content)
                updated_summary = result["updated_executive_summary"]

                logger.info(
                    f"Successfully updated executive summary with Gemini API for report_id: {report_id}"
                )
                return updated_summary

            except Exception as e:
                retry_count += 1
                logger.warning(
                    f"Gemini API attempt {retry_count}/{max_retries} failed: {e!s} for report_id: {report_id}"
                )
                if retry_count < max_retries:
                    logger.info(f"Retrying in {retry_delay} seconds... for report_id: {report_id}")
                    time.sleep(retry_delay)

        # All APIs failed
        logger.critical(f"All API attempts failed for report_id: {report_id}")
        logger.info(
            f"Returning original executive summary unchanged due to all API failures for report_id: {report_id}"
        )
        return current_executive_summary

    except Exception as e:
        logger.critical(
            f"Unexpected error in update_executive_summary: {e!s} for report_id: {report_id}"
        )
        logger.info(
            f"Returning original executive summary unchanged due to unexpected error for report_id: {report_id}"
        )
        return current_executive_summary
