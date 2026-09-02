
from ast import Not
import base64
import datetime
import json
import logging
import re
from typing import List, Optional
from langchain_core.prompts import ChatPromptTemplate
import markdown
import pandas as pd
from pandasai import SmartDataframe
from pydantic import BaseModel, Field
import requests
import os
import time
from openai import OpenAI
from google import genai
from src.config.constants import ANTHROPIC_LLM, ANTHROPIC_MODEL_ID, TABLE_DICISION_MODEL, PLOT_TYPE_MODEL, GEMINI_API_KEY, GEMINI_TABLE_DECISION_MODEL, GEMINI_PLOT_TYPE_MODEL
from src.config.log_helper import setup_logging
from src.core.llm_response_logger import save_raw_llm_response, strip_json_code_fence

logger = setup_logging(__name__)


class TableVisualizationDecision(BaseModel):
    """To decide if table needs to be visualized or not"""
    visualize_or_not: bool = Field(description="whether to visualize the table or not")

class TableSpecificPrompt(BaseModel):
    """To create a specific prompt for a table"""
    prompt: str = Field(description="a specific prompt for the table for a perfect visualization")


def decide_if_table_needs_visualization(table, chat_id: Optional[str] = None, user_id: Optional[str] = None):
    try:
        logger.info(f"Deciding whether to visualize table")
        structured_llm_for_decision = ANTHROPIC_LLM.with_structured_output(TableVisualizationDecision, include_raw=True)
        decision_prompt = f"""Analyze this markdown table and determine if it is QUANTITATIVE (suitable for chart/graph visualization) or QUALITATIVE/TEXT-HEAVY (not suitable for visualization):

{table}

Return TRUE if the table is QUANTITATIVE and can be visualized as a chart/graph:
- Contains meaningful numeric data (numbers, percentages, amounts, scores, etc.)
- Has data that can be plotted (trends, comparisons, distributions, proportions)
- A chart would add value beyond reading the raw numbers

Return FALSE if the table is QUALITATIVE or TEXT-HEAVY:
- Contains mostly text descriptions, names, categories without numeric relationships
- Contains lists of features, pros/cons, or descriptive comparisons
- Data is primarily textual with no meaningful numeric patterns to plot
- Table has very few numeric values that don't form plottable relationships

RETURN ONLY True OR False.
"""
        decision = structured_llm_for_decision.invoke(decision_prompt)
        save_raw_llm_response(decision["raw"], ANTHROPIC_MODEL_ID, "Deciding if a table needs a visual chart", chat_id, user_id=user_id)
        if decision["parsing_error"] is not None:
            raise decision["parsing_error"]
        visualize_or_not = decision["parsed"].visualize_or_not
        should_visualize = visualize_or_not
        return should_visualize
    except Exception as e:
        try:
            logger.info(f"Falling back to OpenAI structured output for table validation")
            client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

            table_validation_schema = {
                "type": "function",
                "function": {
                    "name": "table_decision",
                    "description": "Decide whether the table is quantitative (True) or qualitative/text-heavy (False).",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "visualize_or_not": {
                                "type": "boolean",
                                "description": "True if the table is quantitative and can be visualized as a chart/graph, False if it is qualitative or text-heavy."
                            }
                        },
                        "required": ["visualize_or_not"]
                    }
                }
            }
            table_validation_prompt = f"""Analyze this markdown table and determine if it is QUANTITATIVE (suitable for chart/graph visualization) or QUALITATIVE/TEXT-HEAVY (not suitable for visualization):

{table}

Return TRUE if the table is QUANTITATIVE and can be visualized as a chart/graph:
- Contains meaningful numeric data (numbers, percentages, amounts, scores, etc.)
- Has data that can be plotted (trends, comparisons, distributions, proportions)
- A chart would add value beyond reading the raw numbers

Return FALSE if the table is QUALITATIVE or TEXT-HEAVY:
- Contains mostly text descriptions, names, categories without numeric relationships
- Contains lists of features, pros/cons, or descriptive comparisons
- Data is primarily textual with no meaningful numeric patterns to plot
- Table has very few numeric values that don't form plottable relationships

RETURN ONLY True OR False.
"""

            response = client.chat.completions.create(
                model=TABLE_DICISION_MODEL,
                messages=[{"role": "user", "content": table_validation_prompt}],
                tools=[table_validation_schema],
                tool_choice={"type": "function", "function": {"name": "table_decision"}}
            )
            save_raw_llm_response(response, TABLE_DICISION_MODEL, "Deciding if a table needs a visual chart (backup)", chat_id, user_id=user_id)

            tool_call = response.choices[0].message.tool_calls[0]
            structured_json = json.loads(tool_call.function.arguments)
            visualize_or_not = structured_json.get('visualize_or_not')

            if visualize_or_not is None:
                raise Exception("No visualize_or_not returned by model")
            logger.info(f"Table viz validation result:----> {visualize_or_not}")
            should_visualize = visualize_or_not
            return should_visualize
        except Exception as e:
            logger.error(f"Error validating table: {e}")
            logger.info("Falling back to Gemini API for table validation")
            try:
                gemini_client = genai.Client(api_key=GEMINI_API_KEY)
                interaction = gemini_client.interactions.create(
                    model=GEMINI_TABLE_DECISION_MODEL,
                    input=table_validation_prompt,
                    response_format={
                        "type": "text",
                        "mime_type": "application/json",
                        "schema": TableVisualizationDecision.model_json_schema(),
                    },
                    generation_config={
                        "temperature": 0.1,
                        "thinking_config": {"thinking_budget": 0},
                    },
                )
                save_raw_llm_response(interaction, GEMINI_TABLE_DECISION_MODEL, "Deciding if a table needs a visual chart (Gemini backup)", chat_id, user_id=user_id)
                result = json.loads(strip_json_code_fence(interaction.output_text))
                visualize_or_not = result.get("visualize_or_not")
                if visualize_or_not is None:
                    raise Exception("No visualize_or_not returned by Gemini")
                logger.info(f"Table viz validation result (Gemini):----> {visualize_or_not}")
                return visualize_or_not
            except Exception as gemini_e:
                logger.error(f"Error validating table with Gemini fallback: {gemini_e}")
                return True

def generate_dynamic_prompt(table, chat_id: Optional[str] = None, user_id: Optional[str] = None):
    try:
        logger.info(f"Generating dynamic prompt for table")
        structured_llm_for_prompt = ANTHROPIC_LLM.with_structured_output(TableSpecificPrompt, include_raw=True)
        prompt_prompt = f"""Analyze {table} and create visualization prompt to visualize the data in the best possible way:
        you must always pass the {{table}} and {{plot_type}} in the prompt(VERY IMPORTANT).
        also make sure to save the visualization as .png file always(VERY IMPORTANT).
        Always save the visualization as .png file(VERY IMPORTANT).
        The plot is for the non-technical audience, so it should be in a way that is easy to understand and interpret.
        1. Visualization must be accurate, complete, robust and easy to understand for non-technical audiences(IMPORTANT).
        2. You can try using different types of plots to make the visualization more informative and easy to understand.
        NO overlapping elements allowed - must be fully readable.
        Write visualization title. No need of using subtitles. Just use the title.(IMPORTANT)
        There may be some missing values in the table. So write prompt that can visualize the data even if there are missing values.(THIS IS VERY IMPORTANT)
        ALWAYS RETURN THE PROMPT ONLY, NO OTHER TEXT OR COMMENTARY.
        """
        table_specific_prompt_result = structured_llm_for_prompt.invoke(prompt_prompt)
        save_raw_llm_response(table_specific_prompt_result["raw"], ANTHROPIC_MODEL_ID, "Writing chart instructions tailored to the table", chat_id, user_id=user_id)
        if table_specific_prompt_result["parsing_error"] is not None:
            raise table_specific_prompt_result["parsing_error"]
        dynamic_prompt = table_specific_prompt_result["parsed"].prompt
        if dynamic_prompt:
            return dynamic_prompt
    except Exception as e:
        try:
            logger.info("Falling back to OpenAI structured output for new Table Specific Prompt")
            client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

            table_specific_prompt_schema = {
                "type": "function",
                "function": {
                    "name": "table_specific_prompt",
                    "description": "Generate a new table specific prompt based on the context(table, plot_type).",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "table_specific_prompt": {
                                "type": "string",
                                "description": "The new table specific prompt"
                            }
                        },
                        "required": ["table_specific_prompt"]
                    }
                }
            }
            table_specific_prompt_text = f"""Analyze {table} and create visualization prompt to visualize the data in the best possible way:
            you must always pass the {{table}} and {{plot_type}} in the prompt(VERY IMPORTANT).
            also make sure to save the visualization as .png file always(VERY IMPORTANT).
            Always save the visualization as .png file(VERY IMPORTANT).
            The plot is for the non-technical audience, so it should be in a way that is easy to understand and interpret.
            1. Visualization must be accurate, complete, robust and easy to understand for non-technical audiences(IMPORTANT).
            2. You can try using different types of plots to make the visualization more informative and easy to understand.
            NO overlapping elements allowed - must be fully readable.
            Write visualization title. No need of using subtitles. Just use the title.(IMPORTANT)
            There may be some missing values in the table. So write prompt that can visualize the data even if there are missing values.(THIS IS VERY IMPORTANT)
            ALWAYS RETURN THE PROMPT ONLY, NO OTHER TEXT OR COMMENTARY.
            """
            response = client.chat.completions.create(
                model=PLOT_TYPE_MODEL,
                messages=[{"role": "user", "content": table_specific_prompt_text}],
                tools=[table_specific_prompt_schema],
                tool_choice={"type": "function", "function": {"name": "table_specific_prompt"}}
            )
            save_raw_llm_response(response, PLOT_TYPE_MODEL, "Writing chart instructions tailored to the table (backup)", chat_id, user_id=user_id)

            tool_call = response.choices[0].message.tool_calls[0]
            structured_json = json.loads(tool_call.function.arguments)
            dynamic_prompt = structured_json.get('table_specific_prompt')
            logger.info(f"Dynamic prompt:----> {dynamic_prompt}")

            if dynamic_prompt:
                return dynamic_prompt
        except Exception as e:
            logger.error(f"Error generating dynamic prompt: {e}")
            logger.info("Falling back to Gemini API for dynamic prompt generation")
            try:
                table_specific_prompt_schema_gemini = {
                    "type": "object",
                    "properties": {
                        "table_specific_prompt": {
                            "type": "string",
                            "description": "The new table specific prompt"
                        }
                    },
                    "required": ["table_specific_prompt"]
                }
                gemini_client = genai.Client(api_key=GEMINI_API_KEY)
                interaction = gemini_client.interactions.create(
                    model=GEMINI_PLOT_TYPE_MODEL,
                    input=table_specific_prompt_text,
                    response_format={
                        "type": "text",
                        "mime_type": "application/json",
                        "schema": table_specific_prompt_schema_gemini,
                    },
                    generation_config={
                        "temperature": 0.1,
                        "thinking_config": {"thinking_budget": 0},
                    },
                )
                save_raw_llm_response(interaction, GEMINI_PLOT_TYPE_MODEL, "Writing chart instructions tailored to the table (Gemini backup)", chat_id, user_id=user_id)
                result = json.loads(strip_json_code_fence(interaction.output_text))
                dynamic_prompt = result.get('table_specific_prompt')
                logger.info(f"Dynamic prompt (Gemini):----> {dynamic_prompt}")
                if dynamic_prompt:
                    return dynamic_prompt
                return None
            except Exception as gemini_e:
                logger.error(f"Error generating dynamic prompt with Gemini fallback: {gemini_e}")
                return None

class PlotType(BaseModel):
    """To choose the best plot type"""
    plot_type: str = Field(description="the best plot type for the table")


def choosse_the_best_plot_type(table, chat_id: Optional[str] = None, user_id: Optional[str] = None):
    try:
        logger.info(f"Deciding whether to visualize table")
        structured_llm_for_decision = ANTHROPIC_LLM.with_structured_output(PlotType, include_raw=True)
        choose_the_best_plot_type_prompt = f"""Choose the best plot type for the following table:
        {table}
        Only return the plot type, no other text or commentary.
        """
        decision = structured_llm_for_decision.invoke(choose_the_best_plot_type_prompt)
        save_raw_llm_response(decision["raw"], ANTHROPIC_MODEL_ID, "Choosing the best chart type for the data", chat_id, user_id=user_id)
        if decision["parsing_error"] is not None:
            raise decision["parsing_error"]
        plot_type = decision["parsed"].plot_type
        should_visualize = plot_type
        return should_visualize
    except Exception as e:
        try:
            logger.info(f"Falling back to OpenAI structured output for choose the best plot type")
            client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

            choose_the_best_plot_type_prompt_schema = {
                "type": "function",
                "function": {
                    "name": "plot_type_decision",
                    "description": "Choose the best plot type for the table.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "plot_type": {
                                "type": "string",
                                "description": "The best plot type for the table."
                            }
                        },
                        "required": ["plot_type"]
                    }
                }
            }
            choose_the_best_plot_type_prompt_template = ChatPromptTemplate.from_template(template=choose_the_best_plot_type_prompt)
            choose_the_best_plot_type_prompt = choose_the_best_plot_type_prompt_template.format(
                table=table
            )

            response = client.chat.completions.create(
                model=PLOT_TYPE_MODEL,
                messages=[{"role": "user", "content": choose_the_best_plot_type_prompt}],
                tools=[choose_the_best_plot_type_prompt_schema],
                tool_choice={"type": "function", "function": {"name": "plot_type_decision"}}
            )
            save_raw_llm_response(response, PLOT_TYPE_MODEL, "Choosing the best chart type for the data (backup)", chat_id, user_id=user_id)

            tool_call = response.choices[0].message.tool_calls[0]
            structured_json = json.loads(tool_call.function.arguments)
            plot_type = structured_json.get('plot_type')

            if plot_type is None:
                raise Exception("No visualize_or_not returned by model")
            logger.info(f"Plot type result:----> {plot_type}")
            should_visualize = plot_type
            return should_visualize
        except Exception as e:
            logger.error(f"Error choosing the best plot type: {e}")
            logger.info("Falling back to Gemini API for choose the best plot type")
            try:
                gemini_client = genai.Client(api_key=GEMINI_API_KEY)
                interaction = gemini_client.interactions.create(
                    model=GEMINI_PLOT_TYPE_MODEL,
                    input=choose_the_best_plot_type_prompt,
                    response_format={
                        "type": "text",
                        "mime_type": "application/json",
                        "schema": PlotType.model_json_schema(),
                    },
                    generation_config={
                        "temperature": 0.1,
                        "thinking_config": {"thinking_budget": 0},
                    },
                )
                save_raw_llm_response(interaction, GEMINI_PLOT_TYPE_MODEL, "Choosing the best chart type for the data (Gemini backup)", chat_id, user_id=user_id)
                result = json.loads(strip_json_code_fence(interaction.output_text))
                plot_type = result.get('plot_type')
                if plot_type is None:
                    raise Exception("No plot_type returned by Gemini")
                logger.info(f"Plot type result (Gemini):----> {plot_type}")
                return plot_type
            except Exception as gemini_e:
                logger.error(f"Error choosing the best plot type with Gemini fallback: {gemini_e}")
                return None