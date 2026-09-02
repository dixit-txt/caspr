
from ast import Not
import base64
import datetime
import json
from uuid_utils import uuid7
import boto3
from langchain_anthropic import ChatAnthropic
# from langchain_aws import ChatBedrock  # Bedrock disabled — using direct Anthropic API instead
from langchain_core.prompts import ChatPromptTemplate
import markdown
import pandas as pd
from pandasai import SmartDataframe
from pydantic import BaseModel, Field
import requests
import os
from openai import OpenAI
from google import genai
from src.config.constants import ANTHROPIC_OPUS_4_MODEL_ID, ANTHROPIC_MODEL_ID, ANTHROPIC_LLM, ANTHROPIC_API_KEY, OPENAI_VALIDATION_MODEL, S3_REPORTS_BASE_PATH, TABLE_DICISION_MODEL, VIZ_PLOT_CODE_MODEL, GEMINI_API_KEY, GEMINI_TABLE_DECISION_MODEL, GEMINI_VALIDATION_MODEL, GEMINI_VIZ_PLOT_CODE_MODEL

from src.config.log_helper import setup_logging
from src.core.s3_utils import get_s3_instance
from src.core.llm_response_logger import save_raw_llm_response, strip_json_code_fence
from typing import Optional
import numpy as np
import plotly.graph_objects as go
import plotly.express as px

# from src.config.constants import BEDROCK_ACCESS_KEY_ID, BEDROCK_SECRET_ACCESS_KEY, BEDROCK_REGION_NAME
from botocore.config import Config

# BEDROCK_CLIENT = boto3.client(
#     service_name="bedrock-runtime",
#     aws_access_key_id=BEDROCK_ACCESS_KEY_ID,
#     aws_secret_access_key=BEDROCK_SECRET_ACCESS_KEY,
#     region_name=BEDROCK_REGION_NAME,
#     config=Config(read_timeout=3600)
# )

# BEDROCK_LLM = ChatBedrock(
#     client=BEDROCK_CLIENT,
#     model_id="eu.anthropic.claude-sonnet-4-5-20250929-v1:0",
#     streaming=True,
#     beta_use_converse_api=True,
# )

BEDROCK_LLM = ChatAnthropic(
    model=ANTHROPIC_MODEL_ID,
    max_tokens=16000,
    streaming=True,
    timeout=None,
    max_retries=5,
    api_key=ANTHROPIC_API_KEY,
)

logger = setup_logging(__name__)


class TableVisualizationDecision(BaseModel):
    """To decide if table needs to be visualized or not"""
    visualize_or_not: bool = Field(description="whether to visualize the table or not")
class GeneratePlotCode(BaseModel):
    code: str = Field(description="the code to generate the plot")

def decide_if_table_needs_visualization(table, chat_id: Optional[str] = None, user_id: Optional[str] = None) -> bool:
    """
    Decide if the table needs to be visualized or not.
    
    Args:
        table: The markdown table to evaluate.
    Returns:
        True if the table should be visualized, False otherwise.
    """
    try:
        logger.info(f"Deciding whether to visualize table")
        structured_llm_for_decision = ANTHROPIC_LLM.with_structured_output(TableVisualizationDecision, include_raw=True)
        decision_prompt = f"""
        YOU ARE A STRICT GATEKEEPER FOR DATA VISUALIZATION.

        Evaluate the following markdown table:
        {table}

        Return True ONLY if the table CLEARLY and UNAMBIGUOUSLY qualifies for meaningful quantitative visualization.

        ALL of the following MUST be satisfied to return True:
        1. QUANTITATIVE DOMINANCE:
        - The table MUST contain at least TWO numeric columns or ONE numeric column measured across an ordered dimension (e.g., time, rank, category).
        - Numeric values must represent measurable quantities (counts, percentages, amounts, rates).

        2. MEANINGFUL RELATIONSHIPS:
        - The data MUST exhibit at least one non-trivial relationship such as:
            • trends over time
            • comparisons across entities
            • proportional differences
            • correlations or growth/decline patterns
        - Relationships MUST NOT be obvious at a glance from the raw table.

        3. VISUALIZATION NECESSITY:
        - A chart MUST provide substantial additional insight that cannot be easily inferred by reading the table alone.
        - If the table already communicates the key message clearly, return False.

        4. PLOT FEASIBILITY:
        - Data MUST be structurally suitable for standard visualizations (bar, line, scatter, area, etc.).
        - Missing or NA values are acceptable ONLY if they do not obscure overall patterns.

        AUTOMATICALLY RETURN False IF ANY APPLY:
        - Numeric data is incidental, sparse, or decorative.
        - The table is primarily descriptive, categorical, or textual.
        - Values lack scale, comparison, or analytical relevance.
        - The dataset is too small, flat, or uniform to reveal patterns.
        - Visualization would be redundant or cosmetic.

        DEFAULT TO False UNLESS ALL CONDITIONS ARE CLEARLY MET.

        RETURN ONLY:
        True
        OR
        False
        """

        decision = structured_llm_for_decision.invoke(decision_prompt)
        save_raw_llm_response(decision["raw"], ANTHROPIC_MODEL_ID, "Deciding whether a table should become a chart", chat_id, user_id=user_id)
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
                    "description": "Decide whether the table should be visualized or not.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "visualize_or_not": {
                                "type": "boolean",
                                "description": "True if the table should be visualized, False otherwise."
                            }
                        },
                        "required": ["visualize_or_not"]
                    }
                }
            }
            table_validation_prompt_template = ChatPromptTemplate.from_template(template=decision_prompt)
            table_validation_prompt = table_validation_prompt_template.format(
                table=table
            )

            response = client.chat.completions.create(
                model=TABLE_DICISION_MODEL,
                messages=[{"role": "user", "content": table_validation_prompt}],
                tools=[table_validation_schema],
                tool_choice={"type": "function", "function": {"name": "table_decision"}}
            )
            save_raw_llm_response(response, TABLE_DICISION_MODEL, "Deciding whether a table should become a chart (backup)", chat_id, user_id=user_id)

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
                    input=decision_prompt,
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
                save_raw_llm_response(interaction, GEMINI_TABLE_DECISION_MODEL, "Deciding whether a table should become a chart (Gemini backup)", chat_id, user_id=user_id)
                result = json.loads(strip_json_code_fence(interaction.output_text))
                visualize_or_not = result.get("visualize_or_not")
                if visualize_or_not is None:
                    raise Exception("No visualize_or_not returned by Gemini")
                logger.info(f"Table viz validation result (Gemini):----> {visualize_or_not}")
                return visualize_or_not
            except Exception as gemini_e:
                logger.error(f"Error validating table with Gemini fallback: {gemini_e}")
                return True

def convert_md_table_to_df(md_table) -> pd.DataFrame:
    """
    Convert the markdown table to a pandas dataframe.
    
    Args:
        md_table: The markdown table to convert.
    Returns:
        A pandas dataframe.
    """
    try:
        logger.info(f"Converting markdown table to dataframe")
        html_content = markdown.markdown(md_table, extensions=['tables'])
        dfs = pd.read_html(html_content)
        df = dfs[0]
        return df
    except Exception as e:
        logger.error(f"Error converting markdown table to dataframe: {e}")
        return None

class ImageValidation(BaseModel):
    is_valid: bool = Field(description="whether the image is valid or not")

def validate_image(image_base64: str, md_table: str, chat_id: Optional[str] = None, user_id: Optional[str] = None) -> tuple[bool, str]:
    """
    Validate the image for the given markdown table.
    
    Args:
        image_base64: The base64 encoded image.
        md_table: The markdown table to validate.
    Returns:
        A tuple containing the validation result and the reasoning.
    """
    try:
        logger.info(f"Validating image")
        client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
        validation_prompt = f"""VALIDATE the image for the following markdown table:
        TABLE:
        {md_table}
        Analyze the table and the image and decide if the image is valid for the table.
        Return only True or False in JSON format with the reasoning.
        IMAGE is provided you need to validate it in base64 format.
        The image should be a valid image for the table.
        All the text in the image should be clearly readable.
        The Data representation in the image should be correct.
        Is the image utilizing the data in the table correctly?
        Return the reasoning for the validation decision.
        """
        image_validation_schema = {
            "type": "function",
            "function": {
                "name": "image_validation",
                "description": "Validate the image for the given markdown table",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "is_valid": {
                            "type": "boolean",
                            "description": "True if the image is valid, False otherwise."
                        },
                        "reasoning": {
                            "type": "string",
                            "description": "Reasoning for the validation decision."
                        }
                    },
                    "required": ["is_valid", "reasoning"]
                }
            }
        }
        image_validation_prompt_template = ChatPromptTemplate.from_template(template=validation_prompt)
        image_validation_prompt = image_validation_prompt_template.format(
            md_table=md_table
        )
        response = client.chat.completions.create(
            model=OPENAI_VALIDATION_MODEL,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": image_validation_prompt},
                        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{image_base64}"}}
                    ]
                }
            ],
            tools=[image_validation_schema],
            tool_choice={"type": "function", "function": {"name": "image_validation"}}
        )
        save_raw_llm_response(response, OPENAI_VALIDATION_MODEL, "Checking that a generated chart image looks correct", chat_id, user_id=user_id)
        tool_call = response.choices[0].message.tool_calls[0]
        structured_json = json.loads(tool_call.function.arguments)
        is_valid = structured_json.get('is_valid')
        reasoning = structured_json.get('reasoning')
        return is_valid, reasoning
    except Exception as e:
        logger.error(f"Error validating image with OpenAI: {e}")
        logger.info("Falling back to Gemini API for image validation")
        try:
            gemini_client = genai.Client(api_key=GEMINI_API_KEY)
            interaction = gemini_client.interactions.create(
                model=GEMINI_VALIDATION_MODEL,
                input=[
                    {"type": "text", "text": image_validation_prompt},
                    {"type": "image", "data": image_base64, "mime_type": "image/png"},
                ],
                response_format={
                    "type": "text",
                    "mime_type": "application/json",
                    "schema": {
                        "type": "object",
                        "properties": {
                            "is_valid": {"type": "boolean", "description": "True if the image is valid, False otherwise."},
                            "reasoning": {"type": "string", "description": "Reasoning for the validation decision."},
                        },
                        "required": ["is_valid", "reasoning"],
                    },
                },
                generation_config={
                    "temperature": 0.1,
                    "thinking_config": {"thinking_budget": 0},
                },
            )
            save_raw_llm_response(interaction, GEMINI_VALIDATION_MODEL, "Checking that a generated chart image looks correct (backup)", chat_id, user_id=user_id)
            result = json.loads(strip_json_code_fence(interaction.output_text))
            return result.get("is_valid", False), result.get("reasoning", "")
        except Exception as gemini_e:
            logger.error(f"Error validating image with Gemini fallback: {gemini_e}")
            return False, f"Error validating image: {gemini_e}"

def generate_plot_code(markdown_table: str, error_msg: str = None, chat_id: Optional[str] = None, user_id: Optional[str] = None) -> str:
    """
    Generate optimal plotly code for a specific dataset
    Strictly follow the instructions and generate the code.
    Args:
        markdown_table: The markdown table to visualize.
        error_msg: Optional error message from previous attempt to avoid repeating mistakes
        
    Returns:
        Python code string that creates a plotly figure
    """
    error_instruction = ""
    if error_msg:
        error_instruction = f"""
IMPORTANT - PREVIOUS ATTEMPT FAILED:
The previous code generation resulted in an error. DO NOT make the same mistake again.
Previous Error: {error_msg}
Please analyze this error carefully and generate code that avoids this issue.
"""
    
    prompt = f"""You are an expert in data visualization with plotly. Generate optimal Python code to create the BEST visualization for NON-TECHNICAL AUDIENCES.
{error_instruction}
STRICTLY FOLLOW THE FOLLOWING INSTRUCTIONS:
TABLE:
{markdown_table}

TABLE REQUIREMENTS:
1. Analyze the markdown table and determine the BEST plot type for this data (line, bar, scatter, pie, box, histogram, area, radar, etc.)
2. Generate clean, executable Python code that creates a plotly figure
3. PREFER SIMPLE, CLEAR VISUALIZATIONS over complex multi-subplot layouts

REQUIREMENTS:
- The markdown table is already loaded as variable 'df'
- Use plotly.express (as px) and plotly.graph_objects (as go) for plotting
- Code MUST create a plotly Figure object called 'fig'
- Use plotly.express for statistical plots (px.bar, px.line, px.scatter, etc.) and plotly.graph_objects for more customized or complex plots (go.Figure, go.Scatter, etc.)
- Include appropriate labels, titles, colors, and formatting
- Handle edge cases (missing values, date columns, categorical vs numeric)
- Make it visually appealing with good color schemes and styles.
- CRITICAL: Use MODERN Plotly syntax (v5.0+) with underscore properties (e.g., title_font, tick_font, title_text) NOT deprecated camelCase properties (titlefont, tickfont, titletext)

CRITICAL - HANDLING MULTIPLE METRICS:
If the table has multiple numeric columns with DIFFERENT SCALES (e.g., revenue in millions vs growth rate in percentage):
- PREFERRED APPROACH: Use a secondary y-axis with go.Figure() and fig.add_trace()
  Example: One metric on left y-axis, another on right y-axis
- ALTERNATIVE: Create separate simple plots for each metric (NO subplots unless absolutely necessary)
- DO NOT create subplot grids unless the data explicitly requires separate panels
- If using secondary y-axis, use the simple approach:
  * Create fig = go.Figure()
  * Add traces with yaxis='y' or yaxis='y2'
  * Configure layout with yaxis and yaxis2 properties
  * DO NOT use make_subplots() unless you have a specific need for multiple panels

AVOID THESE COMMON ERRORS:
1. DO NOT use fig.add_trace(..., row=X, col=Y) without first creating subplots with make_subplots()
2. DO NOT use make_subplots() with secondary_y parameter unless you understand the configuration
3. DO NOT overcomplicate visualizations - simple dual-axis plots are better than complex subplots
4. If you get an error about "row and column", switch to a simple dual-axis approach instead
5. PANDAS STRING OPERATIONS: Use df['column'].str.method() NOT df.str.method() - the .str accessor only works on Series, not DataFrames
6. Always check column data types before applying string operations: use df['column'].astype(str) if needed

CRITICAL - NON-TECHNICAL AUDIENCE REQUIREMENTS:
This plot is for NON-TECHNICAL users who need clear, self-explanatory visualizations:

1. **CLEAR AND DESCRIPTIVE TITLES**: Use plain language titles that explain WHAT the data shows, not just what it is
   - Good: "Monthly Sales Performance: Revenue Increased by 25% in Q4"
   - Bad: "Sales Data" or "Revenue vs Month"

2. **PREVENT OVERLAPPING TEXT** (MANDATORY):
   - Rotate x-axis labels if there are many categories: xaxis=dict(tickangle=-45)
   - Use appropriate font sizes (not too large): Keep axis labels at 10-12px, titles at 14-16px
   - Add margins to prevent text cutoff: margin=dict(l=80, r=80, t=100, b=120)
   - If category names are long, consider truncating or abbreviating them
   - Ensure adequate spacing between tick labels
   - Use fig.update_xaxes(tickmode='linear') or tickmode='auto' to control density

3. **INFORMATIVE LABELS**: 
   - Axis labels should explain the units and what is being measured
   - Use labels parameter to rename columns to human-readable names
   - Example: labels={{"rev": "Revenue (in USD)", "month": "Month of 2024"}}

4. **ADD CONTEXT WITH ANNOTATIONS** (when helpful):
   - Highlight key insights, peaks, or important data points
   - Add text annotations to explain trends or outliers
   - Example: fig.add_annotation(x="March", y=50000, text="Highest Sales", showarrow=True)

5. **CLEAR LEGENDS**:
   - Position legends where they don't obscure data: legend=dict(x=1.02, y=1, xanchor='left')
   - Use descriptive legend titles and labels
   - Ensure legend text is readable

6. **READABLE FORMATTING**:
   - Use appropriate number formatting for large numbers (e.g., "1.2M" instead of "1200000")
   - Add hover templates with clear descriptions: hovertemplate="<b>%{{x}}</b><br>Sales: $%{{y:,.0f}}<extra></extra>"
   - Use color schemes that are colorblind-friendly and professional

7. **LAYOUT OPTIMIZATION**:
   - Set adequate height and width if needed: fig.update_layout(height=500, width=800)
   - Add proper padding/margins to prevent text cutoff
   - Use template='plotly_white' or 'plotly' for clean professional look

CRITICAL - AVAILABLE VARIABLES:
You may ONLY use these pre-defined variables in your code:
- df: pandas DataFrame containing the data
- px: plotly.express module
- go: plotly.graph_objects module
- pd: pandas module
- np: numpy module

DO NOT reference any other variables like user_name, chat_id, markdown_table, etc. They are NOT available in the execution context.

PLOTLY SYNTAX WARNING - AVOID DEPRECATED PROPERTIES:
The following OLD properties will cause errors. Use the NEW properties instead:
- OLD: titlefont → NEW: title_font
- OLD: tickfont → NEW: tick_font  
- OLD: titletext → NEW: title_text
- OLD: rangeselector → NEW: rangeselector (still valid but use dict() format)
Always use underscore_case properties, NOT camelCase properties in Plotly v5.0+

OUTPUT FORMAT:
Return ONLY executable Python code, no explanations. The code should STRICTLY follow the following format:
1. Start with necessary imports if needed (assume px and go are already imported)
2. Create a plotly figure object and assign it to variable 'fig'
3. Add proper labels, title, legend, and formatting
4. The final result must be a plotly Figure object assigned to 'fig'
5. Do NOT use fig.show() or any display/output code
6. ONLY use the variables listed above (df, px, go, pd, np)

Example structure for plotly express (NON-TECHNICAL AUDIENCE):
```python
# Create plot with descriptive labels for non-technical users
fig = px.bar(
    df, 
    x="column1", 
    y="column2", 
    title="Clear Descriptive Title Explaining the Insight", 
    color="optional_column", 
    labels={{"column1": "Descriptive X Label (with units)", "column2": "Descriptive Y Label (with units)"}},
    template='plotly_white'
)
# Prevent overlapping text and improve readability
fig.update_layout(
    font=dict(size=12), 
    title_font_size=16,
    title_x=0.5,  # Center the title
    legend=dict(title_text="Legend Title", x=1.02, y=1),
    xaxis=dict(
        title_text="X Axis Label", 
        title_font=dict(size=13),
        tickangle=-45,  # Rotate labels to prevent overlap
        tickfont=dict(size=10)
    ),
    yaxis=dict(
        title_text="Y Axis Label", 
        title_font=dict(size=13),
        tickfont=dict(size=10)
    ),
    margin=dict(l=80, r=80, t=100, b=120),  # Prevent text cutoff
    hovermode='x unified'
)
# Add hover information for clarity
fig.update_traces(hovertemplate="<b>%{{x}}</b><br>Value: %{{y:,.2f}}<extra></extra>")
```

Example structure for plotly graph_objects (NON-TECHNICAL AUDIENCE):
```python
fig = go.Figure()
fig.add_trace(go.Scatter(
    x=df["x"], 
    y=df["y"], 
    mode="lines+markers", 
    name="Descriptive Series Name",
    hovertemplate="<b>%{{x}}</b><br>Value: %{{y:,.2f}}<extra></extra>"
))
fig.update_layout(
    title="Clear Descriptive Title Explaining What the Data Shows", 
    title_x=0.5,  # Center title
    title_font_size=16,
    xaxis=dict(
        title_text="X Axis Label (with units)", 
        title_font=dict(size=13),
        tickangle=-45,  # Prevent overlap
        tickfont=dict(size=10)
    ),
    yaxis=dict(
        title_text="Y Axis Label (with units)", 
        title_font=dict(size=13),
        tickfont=dict(size=10)
    ),
    font=dict(size=12), 
    template='plotly_white',
    margin=dict(l=80, r=80, t=100, b=120),  # Prevent text cutoff
    hovermode='x unified'
)
```

Example structure for DUAL Y-AXIS plots (for metrics with different scales):
```python
# Simple dual-axis approach - NO make_subplots needed
fig = go.Figure()

# First metric on left y-axis
fig.add_trace(go.Bar(
    x=df["year"], 
    y=df["metric1"],
    name="First Metric (e.g., Revenue in USD Billion)",
    marker_color='blue',
    yaxis='y',
    hovertemplate="<b>%{{x}}</b><br>Revenue: $%{{y:.1f}}B<extra></extra>"
))

# Second metric on right y-axis (different scale)
fig.add_trace(go.Scatter(
    x=df["year"], 
    y=df["metric2"],
    mode="lines+markers",
    name="Second Metric (e.g., Growth Rate %)",
    marker_color='red',
    line=dict(color='red', width=2),
    yaxis='y2',
    hovertemplate="<b>%{{x}}</b><br>Growth: %{{y:.1f}}%<extra></extra>"
))

# Configure layout with dual y-axes
fig.update_layout(
    title="Clear Title Showing Both Metrics", 
    title_x=0.5,
    title_font_size=16,
    xaxis=dict(
        title_text="Time Period",
        title_font=dict(size=13),
        tickfont=dict(size=10)
    ),
    yaxis=dict(
        title_text="Primary Metric (Left Axis)",
        title_font=dict(size=13),
        tickfont=dict(size=10),
        side='left'
    ),
    yaxis2=dict(
        title_text="Secondary Metric (Right Axis)",
        title_font=dict(size=13),
        tickfont=dict(size=10),
        overlaying='y',
        side='right'
    ),
    legend=dict(x=0.5, y=-0.15, xanchor='center', yanchor='top', orientation='h'),
    margin=dict(l=80, r=80, t=100, b=120),
    template='plotly_white',
    hovermode='x unified'
)
```

IMPORTANT: Always ensure 'fig' is a plotly Figure object. ALWAYS implement measures to prevent overlapping text. For multi-metric data, prefer simple dual-axis approach over complex subplots.

Generate the code now:"""

    try:
        structured_llm = ANTHROPIC_LLM.with_structured_output(GeneratePlotCode, include_raw=True)
        response = structured_llm.invoke(prompt)
        save_raw_llm_response(response["raw"], ANTHROPIC_MODEL_ID, "Writing code to draw a chart", chat_id, user_id=user_id)
        if response["parsing_error"] is not None:
            raise response["parsing_error"]
        code = response["parsed"].code
        return code
    except Exception as e:
        logger.info(f"Error with Claude: {e}")
        logger.info(f"Falling back to OpenAI")
        try:
            client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
            generate_plot_code_schema_openai = {
                "type": "function",
                "function": {
                    "name": "generate_plot_code",
                    "description": "Generates optimal plotly code for a specific dataset",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "code": {"type": "string", "description": "Optimal plotly code for a specific dataset"}
                        }
                    }
                }
            }
            response = client.chat.completions.create(
                model=VIZ_PLOT_CODE_MODEL,
                messages=[
                    {"role": "system", "content": "You are an expert data visualization engineer. You generate optimal, clean seaborn code that creates beautiful and insightful visualizations."},
                    {"role": "user", "content": prompt}
                ],
                temperature=0.5,
                tools=[generate_plot_code_schema_openai],
                tool_choice={"type": "function", "function": {"name": "generate_plot_code"}},
            )
            save_raw_llm_response(response, VIZ_PLOT_CODE_MODEL, "Writing code to draw a chart (backup)", chat_id, user_id=user_id)
            tool_call = response.choices[0].message.tool_calls[0]
            structured_json = json.loads(tool_call.function.arguments)
            code = structured_json.get("code", "")
            return code
        except Exception as openai_e:
            logger.error(f"Error with OpenAI generating plot code: {openai_e}")
            logger.info("Falling back to Gemini API for plot code generation")
            try:
                gemini_client = genai.Client(api_key=GEMINI_API_KEY)
                interaction = gemini_client.interactions.create(
                    model=GEMINI_VIZ_PLOT_CODE_MODEL,
                    input=prompt,
                    system_instruction="You are an expert data visualization engineer. You generate optimal, clean seaborn code that creates beautiful and insightful visualizations.",
                    response_format={
                        "type": "text",
                        "mime_type": "application/json",
                        "schema": GeneratePlotCode.model_json_schema(),
                    },
                    generation_config={
                        "temperature": 0.5,
                        "thinking_config": {"thinking_budget": 0},
                    },
                )
                save_raw_llm_response(interaction, GEMINI_VIZ_PLOT_CODE_MODEL, "Writing code to draw a chart (Gemini backup)", chat_id, user_id=user_id)
                result = json.loads(strip_json_code_fence(interaction.output_text))
                return result.get("code", "")
            except Exception as gemini_e:
                logger.error(f"Error with Gemini fallback generating plot code: {gemini_e}")
                return ""

def execute_plot_code(plot_code: str, df, markdown_table: str, user_name: str, chat_id: str, user_id: Optional[str] = None):
    """Render dynamic plots from plotly code, evaluate, and upload to S3 if valid"""
    exec_globals = {
        'df': df,
        'go': go,
        'px': px,
        'pd': pd,
        'np': np,
        'plotly': __import__('plotly'),
    }
    
    image_path = None
    html_path = None
    try:
        # The generated code should assign the plotly Figure to 'fig'
        exec(plot_code, exec_globals)
        fig = exec_globals.get('fig')
        if fig is None or not hasattr(fig, "to_image"):
            logger.error(
                "Generated code did not create a plotly 'fig' variable for user %s, chat %s",
                user_name, chat_id
            )
            raise ValueError("Generated code did not create a 'fig' variable as a plotly Figure")
    except NameError as ne:
        error_msg = f"NameError in generated code: {str(ne)}. Available variables: df, px, go, pd, np. Do not use user_name, chat_id, or other undefined variables."
        logger.error(f"NameError in generated plot code for user {user_name}, chat {chat_id}: {error_msg}")
        return None, None, False, error_msg
    except AttributeError as ae:
        error_msg = f"AttributeError in generated code: {str(ae)}. Common fix: Use df['column'].str.method() NOT df.str.method() - the .str accessor only works on Series."
        logger.error(f"AttributeError in generated plot code for user {user_name}, chat {chat_id}: {error_msg}")
        return None, None, False, error_msg
    except Exception as e:
        error_msg = f"Execution error: {str(e)}. Check data types and ensure proper pandas/plotly syntax."
        logger.error(f"Failed to render plotly plot for user {user_name}, chat {chat_id}: {error_msg}")
        return None, None, False, error_msg
    
    try:
        
        uuid_str = str(uuid7())
        date_str = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
        image_path = f"{date_str}_{uuid_str}.png"
        html_path = f"{date_str}_{uuid_str}.html"
        # Requires kaleido for to_image/to_file
        # pip install kaleido==1.2.0
        # pip install plotly==6.5.0
        # sudo apt update
        # sudo apt install -y \
        # libx11-6 \
        # libxcomposite1 \
        # libxcursor1 \
        # libxdamage1 \
        # libxext6 \
        # libxi6 \
        # libxrandr2 \
        # libxrender1 \
        # libxtst6 \
        # libxss1 \
        # libxcb1 \
        # libatk1.0-0 \
        # libatk-bridge2.0-0 \
        # libcairo2 \
        # libpango-1.0-0 \
        # libpangocairo-1.0-0 \
        # libasound2 \
        # libcups2 \
        # libdbus-glib-1-2 \
        # libnss3 \
        # libnspr4 \
        # fontconfig \
        # freetype2-demos
        try:
            fig.write_image(image_path, format='png', scale=2)
            fig.write_html(html_path)
        except Exception as img_exc:
            logger.error(f"Failed to write plotly image: {img_exc}")
            raise
        
        logger.info(f"Saved plotly plot to temporary file: {image_path} and {html_path}")
        
        logger.info(f"Evaluating generated image before S3 upload")
        with open(image_path, 'rb') as img_file:
            image_base64 = base64.b64encode(img_file.read()).decode('utf-8')
        
        is_valid, reasoning = validate_image(image_base64, markdown_table, chat_id=chat_id, user_id=user_id)
        
        if not is_valid:
            logger.warning(f"Image validation failed: {reasoning}")
            return None, None, False, reasoning
        
        logger.info(f"Image validation passed: {reasoning}")
        
        # Upload to S3 only if validation passed
        s3_controls = get_s3_instance()
        now = datetime.datetime.now()
        year = now.strftime('%Y')
        month = now.strftime('%m')
        day = now.strftime('%d')
        
        s3_key = f"{S3_REPORTS_BASE_PATH}/visualizations/{year}/{month}/{day}/{user_name}/chat_{chat_id}/{image_path}"
        image_s3_path = s3_controls.upload_file(image_path, s3_key)
        s3_key_html = f"{S3_REPORTS_BASE_PATH}/visualizations/{year}/{month}/{day}/{user_name}/chat_{chat_id}/{html_path}"
        html_s3_path = s3_controls.upload_file(html_path, s3_key_html)
        
        if image_s3_path:
            logger.info(f"Uploaded plotly plot to S3: {image_s3_path}")
            logger.info(f"Uploaded plotly plot to S3: {html_s3_path}")
            return image_s3_path, html_s3_path, True, reasoning
        else:
            logger.error(f"Failed to upload plotly plot to S3")
            return None, None, False, "Failed to upload to S3"
    except Exception as e:
        logger.error(f"Failed to render plotly plot: {e}")
        return None, None, False, str(e)
    finally:
        if image_path and os.path.exists(image_path):
            os.remove(image_path)
            logger.info(f"Cleaned up temporary file: {image_path}")
        if html_path and os.path.exists(html_path):
            os.remove(html_path)
            logger.info(f"Cleaned up temporary file: {html_path}")


def run_viz_pipeline(markdown_table: str, user_name: str, chat_id: str, user_id: Optional[str] = None)-> tuple[str, str]:
    """
    Run the visualization pipeline for the given markdown table.
    
    Args:
        markdown_table: The markdown table to visualize.
        user_name: The name of the user.
        chat_id: The ID of the chat.
    Returns:
        A tuple containing the image path and the HTML path.
    """
    return_config = {
        "image_path": None,
        "html_path": None,
        "is_valid": False,
        "reasoning": None,
        "error_msg": None,
        "NANO_BANANA": False,
    }
    try:
        logger.info(f"Running visualization pipeline for user {user_name} and chat {chat_id}")
        should_visualize = decide_if_table_needs_visualization(markdown_table, chat_id=chat_id, user_id=user_id)
        
        if not should_visualize:
            logger.info(f"Table does not need visualization")
            return_config["NANO_BANANA"] = True
            return return_config
        
        df = convert_md_table_to_df(markdown_table)
        if df is None:
            logger.error(f"Failed to convert markdown table to dataframe")
            return return_config
        
        max_retries = 2
        error_msg = return_config["error_msg"]
        
        for attempt in range(max_retries):
            logger.info(f"Visualization attempt {attempt + 1}/{max_retries}")
            
            if attempt == 0:
                code = generate_plot_code(markdown_table, chat_id=chat_id, user_id=user_id)
            else:
                logger.info(f"Regenerating code with error feedback: {error_msg}")
                code = generate_plot_code(markdown_table, error_msg=error_msg, chat_id=chat_id, user_id=user_id)
        
            try:
                image_s3_path, html_s3_path, is_valid, reasoning = execute_plot_code(
                    code, df, markdown_table, user_name, chat_id, user_id=user_id
                )
                if is_valid and image_s3_path and html_s3_path:
                    logger.info(f"Successfully generated and validated visualization on attempt {attempt + 1}")
                    return_config["is_valid"] = True
                    return_config["reasoning"] = reasoning
                    return_config["error_msg"] = None
                    return_config["image_path"] = image_s3_path
                    return_config["html_path"] = html_s3_path
                    return return_config
                else:
                    error_msg = f"Validation failed - {reasoning}. Please generate code that addresses this issue."
                    logger.warning(f"Attempt {attempt + 1} failed: {error_msg}")
                    
                    if attempt < max_retries - 1:
                        logger.info(f"Retrying with error feedback...")
                        continue
                    else:
                        logger.error(f"All {max_retries} attempts failed. Last error: {error_msg}")
                        return_config["error_msg"] = error_msg
                        return return_config
            except Exception as exec_error:
                error_msg = f"Code execution error - {str(exec_error)}. Please fix the code to avoid this error."
                logger.error(f"Attempt {attempt + 1} execution error: {error_msg}")
                return_config["error_msg"] = error_msg
                if attempt < max_retries - 1:
                    logger.info(f"Retrying with error feedback...")
                    continue
                else:
                    logger.error(f"All {max_retries} attempts failed. Last error: {error_msg}")
                    return return_config
        return return_config
    except Exception as e:
        logger.error(f"Error running visualization pipeline: {e}")
        return return_config


# from src.config.constants import bedrock_client  # Bedrock disabled — using direct Anthropic API instead


class MermaidVisualization(BaseModel):
    """Structured output for mermaid diagram generation from a table."""
    data_classification: str = Field(
        description="Classification of the table data: 'quantitative', 'qualitative', or 'mixed'"
    )
    chart_type: str = Field(
        description="The mermaid chart type chosen. Must be one of: 'pie', 'flowchart LR', 'flowchart TD', or 'timeline'"
    )
    mermaid_code: str = Field(
        description="The complete, valid mermaid diagram code that visualizes the table data. No custom styling or init blocks."
    )
    reasoning: str = Field(
        description="Brief explanation of why this chart type and color scheme were chosen for the given data"
    )


# OPUS_LLM = ChatBedrock(
#     client=BEDROCK_CLIENT,
#     model_id=ANTHROPIC_OPUS_4_MODEL_ID,
#     streaming=False,
#     beta_use_converse_api=True,
# )

OPUS_LLM = ChatAnthropic(
    model=ANTHROPIC_OPUS_4_MODEL_ID,
    max_tokens=16000,
    timeout=None,
    max_retries=5,
    api_key=ANTHROPIC_API_KEY,
)


def generate_mermaid_from_table(table: str, chart_hint: Optional[str] = None, chat_id: Optional[str] = None, user_id: Optional[str] = None) -> MermaidVisualization:
    """
    Takes a markdown table and generates mermaid diagram code using
    Claude Opus 4 via Bedrock with structured output.

    Args:
        table: A markdown-formatted table string.
        chart_hint: Optional hint for preferred chart type
                    (e.g., "pie", "bar", "timeline").

    Returns:
        MermaidVisualization with data_classification, chart_type, mermaid_code, and reasoning.

    Raises:
        Exception: If both Opus and fallback LLM fail.
    """
    hint_instruction = ""
    if chart_hint:
        hint_instruction = f"\nPREFERRED CHART TYPE: {chart_hint} (use this if the data supports it)\n"

    prompt = f"""You are an expert data visualization engineer specializing in Mermaid.js diagrams.

Analyze the following markdown table and generate the BEST mermaid diagram code to visualize it.
{hint_instruction}
TABLE:
{table}

STEP 1 — CLASSIFY THE DATA:
Before choosing a chart type, classify the table content:

A) QUANTITATIVE — numeric-heavy data with measurable values (revenue, percentages, counts, scores).
B) QUALITATIVE/WORKFLOW — process steps, stages, categories, relationships, comparisons described in text, status tracking, feature lists, pros/cons, decision criteria.
C) MIXED — contains both numeric metrics and descriptive/categorical information.

STEP 2 — CHOOSE THE BEST DIAGRAM TYPE based on classification:

IMPORTANT: Use ONLY stable, simple Mermaid diagram types that render reliably in PDF.
DO NOT use experimental or complex chart types such as `xychart-beta`, `sankey-beta`, `block-beta`, `journey`, or `quadrantChart`.

Allowed chart types:
- `pie` (for proportional numeric data)
- `flowchart LR` or `flowchart TD` (for process/category relationships)
- `timeline` (only when chronology is the main message)

For QUANTITATIVE data:
  - Prefer `pie` when showing shares/proportions with <=8 categories.
  - If data does not fit a pie chart, use a simple `flowchart` with concise labels and values.

For QUALITATIVE/WORKFLOW data:
  - Use `flowchart LR` or `flowchart TD`.
  - Use `timeline` only for clearly chronological sequences.

For MIXED data:
  - Prioritize the dominant pattern and choose the simplest allowed chart type.
  - If unsure, default to `flowchart TD`.

STEP 3 — STYLING:
Do NOT include any %%{{init}}%% block, classDef, or style directives.
Do NOT set any custom colors, themes, or themeVariables.
Let mermaid use its DEFAULT theme with default colors.
Keep the diagram clean and simple — content over decoration.

STEP 4 — A4 PDF LAYOUT & SIZING (CRITICAL):
This diagram MUST render cleanly on an A4 page (210mm x 297mm, ~794 x 1123px at 96dpi).
Follow these rules strictly to avoid overlap and ugly output:

GENERAL SIZING:
- Limit the diagram to a MAXIMUM of 8-10 top-level nodes/branches
- If the data has more than 10 items, group them into logical clusters or summarize

LABEL LENGTH RULES (MANDATORY):
- Node labels: MAX 20 characters. Abbreviate or use line breaks for longer text
- Pie labels: MAX 18 characters
- If a value has units, keep them short: $5.2B, 12.3%, 4K, 1.2M

FLOWCHART SIZING:
- Use `flowchart LR` (left-to-right) for processes with 4-6 steps — fits A4 landscape width
- Use `flowchart TD` (top-down) for hierarchies with 3-5 levels — fits A4 portrait height
- NEVER exceed 5 nodes in a single row/column — add line breaks in the flow with subgraphs
- Keep subgraph labels short (max 15 chars)

PIE CHART SIZING:
- MAX 8 slices. If more, combine smallest into "Others"
- Labels must be short enough to not overlap the chart

PREVENTING OVERLAP:
- NEVER place long text next to other long text without spacing
- For flowcharts with many nodes, use subgraphs to create visual grouping and whitespace
- Prefer wider/shorter diagrams over tall/narrow ones for A4 landscape compatibility
- When in doubt, SIMPLIFY — fewer nodes with clear labels beats many nodes that overlap

STEP 5 — GENERATE VALID MERMAID CODE:

CRITICAL SYNTAX RULES (violations WILL cause rendering failures):

RULE 1 — SAFE NODE IDs:
  - Node IDs must be simple alphanumeric identifiers: letters, digits, underscores ONLY
  - NO spaces, hyphens, dots, or special characters in node IDs
  - GOOD: node1, salesData, Q1_revenue
  - BAD: node-1, sales.data, Q1 revenue

RULE 2 — SAFE LABELS (MOST IMPORTANT FOR ROBUST RENDERING):
  - ALL labels MUST be wrapped in double quotes inside square brackets: ["Label text here"]
  - NEVER use parentheses () in labels — they break the parser. Use square brackets [] only.
  - NEVER use special characters in labels: no &, #, <, >, {{}}, ||
  - Replace & with "and", replace < > with comparisons in words
  - If you need line breaks, use <br/> inside the quoted label
  - GOOD: A["Revenue: $5.2B"]
  - GOOD: B["Growth Rate<br/>12.3%"]
  - BAD: A(Revenue: $5.2B)
  - BAD: B["Revenue & Profit"]
  - BAD: C["Sales <$1M>"]

RULE 3 — PIE CHART SYNTAX:
  - Title line: `title Chart Title`
  - Each slice: `"Label" : numericValue`
  - Values must be plain numbers (no $, %, or units in the value part)
  - Labels must be in double quotes
  - GOOD:
    ```
    pie title Market Share
        "Company A" : 45
        "Company B" : 30
        "Others" : 25
    ```

RULE 4 — FLOWCHART SYNTAX:
  - Start with `flowchart TD` or `flowchart LR`
  - All node definitions: ID["Label text"]
  - Edges: A --> B or A -->|"edge label"| B
  - Edge labels must also be in quotes: -->|"label"|
  - NEVER use --> without a valid target node on the same line
  - Each edge on its own line
  - GOOD:
    ```
    flowchart TD
        A["Start"] --> B["Process"]
        B --> C["End"]
    ```

RULE 5 — SUBGRAPH RULES:
  - Subgraph declaration: `subgraph sgID["Subgraph Title"]`
  - Use a UNIQUE ID for subgraphs that is NEVER used as a node ID elsewhere
  - Always suffix subgraph IDs with _sg: `subgraph sales_sg["Sales"]`
  - NEVER draw an edge TO a subgraph ID
  - NEVER use the same identifier as both a node and a subgraph
  - Close every subgraph with `end` on its own line

RULE 6 — FORBIDDEN PATTERNS:
  - NEVER use `title` as a node ID (conflicts with Mermaid internals)
  - NEVER use backtick-wrapped markdown in labels: ["`**bold**`"] is INVALID
  - NEVER use %%{{init}}%% or classDef or style directives
  - NEVER use colons in node labels for flowcharts — rephrase to avoid them
    - BAD: A["Key: Value"]  (colon can cause parse issues in some renderers)
    - GOOD: A["Key - Value"] or A["Key = Value"]
  - NEVER start a label with a number or special character
  - NEVER use empty labels: A[""] is invalid

RULE 7 — TIMELINE SYNTAX:
  - Start with `timeline`
  - Then `title Timeline Title`
  - Sections: `section Section Name`
  - Events: `    Event description : date or detail`
  - Keep it simple — no nesting, no complex formatting

STEP 6 — SELF-VALIDATION CHECKLIST (verify ALL before outputting):
1. Does the code start with exactly one of: `pie`, `flowchart TD`, `flowchart LR`, or `timeline`?
2. Are ALL node labels wrapped in ["double quotes inside brackets"]?
3. Are there any parentheses () used as node shapes? If yes, REPLACE with ["..."]
4. Are ALL node IDs simple alphanumeric (no spaces, hyphens, or dots)?
5. Are there any special characters (&, #, <, >) in labels? If yes, REMOVE or REPHRASE
6. Is every subgraph properly closed with `end`?
7. Is any subgraph ID also used as a node target? If yes, RENAME the subgraph ID
8. Are there fewer than 10 top-level elements?
9. Are all labels under 20 characters?
10. Is the diagram free of %%{{init}}%%, classDef, and style directives?

If ANY check fails, FIX IT before outputting the mermaid_code."""

    structured_llm = OPUS_LLM.with_structured_output(MermaidVisualization, include_raw=True)

    try:
        logger.info("Generating mermaid visualization with Opus 4 via Bedrock")
        result = structured_llm.invoke(prompt)
        save_raw_llm_response(result["raw"], ANTHROPIC_OPUS_4_MODEL_ID, "Creating a flowchart or diagram", chat_id, user_id=user_id)
        if result["parsing_error"] is not None:
            raise result["parsing_error"]
        response: MermaidVisualization = result["parsed"]
        response.mermaid_code = _sanitize_mermaid(response.mermaid_code)
        logger.info(f"Generated {response.chart_type} mermaid diagram (classification: {response.data_classification})")
        return response
    except Exception as e:
        logger.error(f"Opus 4 failed: {e}, falling back to default LLM")
        fallback_structured = ANTHROPIC_LLM.with_structured_output(MermaidVisualization, include_raw=True)
        fallback_result = fallback_structured.invoke(prompt)
        save_raw_llm_response(fallback_result["raw"], ANTHROPIC_MODEL_ID, "Creating a flowchart or diagram (backup)", chat_id, user_id=user_id)
        if fallback_result["parsing_error"] is not None:
            raise fallback_result["parsing_error"]
        response: MermaidVisualization = fallback_result["parsed"]
        response.mermaid_code = _sanitize_mermaid(response.mermaid_code)
        logger.info(f"Fallback generated {response.chart_type} mermaid diagram (classification: {response.data_classification})")
        return response

def _sanitize_mermaid(code: str) -> str:
    """Clean generated mermaid code: unescape, strip fences, remove init/style blocks,
    and fix common structural issues that prevent rendering."""
    import re

    code = code.replace("\\n", "\n").replace("\\t", "    ")
    code = code.strip()
    if code.startswith("```mermaid"):
        code = code[len("```mermaid"):]
    if code.startswith("```"):
        code = code[3:]
    if code.endswith("```"):
        code = code[:-3]
    code = re.sub(r'%%\{init:.*?\}%%', '', code, flags=re.DOTALL)
    code = re.sub(r'^[ \t]*(classDef|style)\b.*$', '', code, flags=re.MULTILINE)

    # Remove backtick-wrapped markdown in node labels: ["`**text**`"] -> ["text"]
    code = re.sub(r'\["`\*\*(.*?)\*\*`"\]', r'["\1"]', code)
    code = re.sub(r'\["`(.*?)`"\]', r'["\1"]', code)

    # Replace & with "and" in labels (between quotes inside brackets)
    def _fix_ampersand(m):
        return m.group(0).replace('&', 'and')
    code = re.sub(r'\["[^"]*&[^"]*"\]', _fix_ampersand, code)

    # Replace < and > in labels with safe alternatives
    def _fix_angle_brackets(m):
        content = m.group(0)
        content = content.replace('<br/>', '\x00BR\x00')  # preserve <br/>
        content = content.replace('<', 'lt ').replace('>', ' gt')
        content = content.replace('\x00BR\x00', '<br/>')
        return content
    code = re.sub(r'\["[^"]*[<>][^"]*"\]', _fix_angle_brackets, code)

    # Fix parentheses used as node shapes: A(Label) -> A["Label"]
    # Match: ID(Label) but NOT subgraph or keywords
    code = re.sub(r'(\b[A-Za-z_]\w*)\(([^)]{1,50})\)(\s*-->|\s*$|\s*\n)', r'\1["\2"]\3', code, flags=re.MULTILINE)

    # Fix subgraph ID collision: if a node ID is used as edge target AND as subgraph ID,
    # remove the edges that point TO the subgraph IDs (subgraphs render without them).
    subgraph_ids = set(re.findall(r'^\s*subgraph\s+(\w+)', code, re.MULTILINE))
    if subgraph_ids:
        lines = code.split('\n')
        cleaned_lines = []
        for line in lines:
            edge_match = re.search(r'-->\s*(?:\|[^|]*\|\s*)?(\w+)', line)
            if edge_match:
                target_id = edge_match.group(1)
                if target_id in subgraph_ids:
                    continue
            cleaned_lines.append(line)
        code = '\n'.join(cleaned_lines)

    # Fix 'title' used as node ID — rename to 'Header'
    if re.search(r'(?:^|\s)title\s*[\[\({]', code, re.MULTILINE):
        code = re.sub(r'\btitle\b(?=\s*[\[\({])', 'Header', code)
        code = re.sub(r'\btitle\b(?=\s*-->)', 'Header', code)

    # Remove any lines with just whitespace or empty node definitions
    code = re.sub(r'\n{3,}', '\n\n', code)

    # Ensure no trailing edge without target (dangling -->)
    code = re.sub(r'-->\s*$', '', code, flags=re.MULTILINE)

    # Strip colons from flowchart node labels (replace with dash) to avoid parser confusion
    # Only do this for flowchart diagrams, not pie charts where colons are required
    if code.strip().startswith('flowchart'):
        def _fix_colons_in_labels(m):
            return m.group(0).replace(':', ' -')
        code = re.sub(r'\["[^"]*:[^"]*"\]', _fix_colons_in_labels, code)

    return code.strip()


def mermaid_to_markdown(mermaid_code: str) -> str:
    """Wrap raw mermaid code in a markdown fenced code block."""
    code = _sanitize_mermaid(mermaid_code)
    return f"```mermaid\n{code}\n```"

def generate_mermaid_viz(table: str, chart_hint: Optional[str] = None, chat_id: Optional[str] = None, user_id: Optional[str] = None) -> Optional[str]:
    """
    Main entry point for mermaid-based visualization.
    
    Takes a markdown table, generates a mermaid diagram, and returns the
    raw mermaid code string (not wrapped in markdown fences).
    Returns None if generation fails.
    """
    try:
        logger.info("Attempting mermaid visualization generation")
        result: MermaidVisualization = generate_mermaid_from_table(table, chart_hint=chart_hint, chat_id=chat_id, user_id=user_id)
        if result and result.mermaid_code:
            logger.info(f"Mermaid viz generated successfully — chart_type={result.chart_type}, "
                        f"classification={result.data_classification}")
            return result.mermaid_code
        logger.warning("Mermaid generation returned empty code")
        return None
    except Exception as e:
        logger.error(f"Mermaid visualization generation failed: {e}")
        return None
