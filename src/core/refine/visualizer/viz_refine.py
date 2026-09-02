"""
Input_json:
{
    "user_ins": ""
    "table":
    "viz": ""
}

Flow:
1. Decision (is table quantitative?)
2. If True -> html_graph_maker_refine (table + user_ins only, NOT existing viz)
3. If False -> html_table_visualizer_refine (table + user_ins only, NOT existing viz)
4. Fallback -> Gemini image gen (table + user_ins only, NOT existing viz)
"""
import base64
import datetime
import os
import time
from typing import Optional
import pandas as pd
from uuid_utils import uuid7
import shutil
import markdown
from google import genai
from src.config.constants import S3_REPORTS_BASE_PATH
from src.core.refine.visualizer.html_graph_maker_refine import generate_html_visualization_refine
from src.core.refine.visualizer.html_table_visualizer_refine import generate_info_visualization_refine
from src.core.prompts.dynamic_prompting import decide_if_table_needs_visualization
from src.core.integrations.s3_utils import get_s3_instance

from src.core.prompts.prompt_utils import TABLE_TO_VIZ_PROMPT
from src.config.constants import VISUALIZATION_MODEL, ENVIRONMENT
from src.config.log_helper import setup_logging
from src.core.observability.llm_response_logger import save_raw_llm_response
logger = setup_logging(__file__)

def generate_refine_table_visualization(table: str, 
    user_prompt: str, 
    chat_id: str, 
    existing_viz: str = None,
    aspect_ratio: Optional[str] = "4:3", 
    max_retries: int = 6,
    user_id: Optional[str] = None)->str:
    """Generate refined table visualization with decision-based HTML first, then a Gemini fallback.
    
    Flow:
        1. Run decision check (quantitative vs qualitative)
        2. If quantitative (True) -> html_graph_maker_refine (table + user_ins + existing_viz)
        3. If qualitative (False) -> html_table_visualizer_refine (table + user_ins + existing_viz)
        4. If HTML fails -> Gemini image generation fallback (table + user_ins only, NOT viz)

    Args:
        table: The table to visualize.
        user_prompt: The user's refinement instruction.
        chat_id: The id of the chat.
        existing_viz: The existing HTML visualization to refine (passed to HTML generators only).
        aspect_ratio: The aspect ratio of the image.
        max_retries: The maximum number of retries.
    Returns:
        HTML string for HTML visualizations, or image path for image-based ones.
    """
    
    if not table or not table.strip():
        logger.error("Table content is empty or None")
        return None
    
    if table.strip() in ['<table>', '</table>', '<table></table>']:
        logger.error(f"Table contains only empty HTML tags: {table.strip()}")
        return None
        
    logger.info(f"Generating refine table visualization for chat_id: {chat_id}")

    # Step 1: Decision — is this table quantitative (suitable for charts) or not?
    is_quantitative = decide_if_table_needs_visualization(table, chat_id=chat_id, user_id=user_id)
    logger.info(f"Table visualization decision: is_quantitative={is_quantitative}")

    # Step 2: HTML visualization based on decision (table + user_ins + existing_viz)
    if is_quantitative:
        try:
            # raise Exception("test")
            logger.info("Decision=True: Trying HTML graph visualization refine (charts)")
            image_html = generate_html_visualization_refine(table, user_refine_prompt=user_prompt, existing_viz=existing_viz, chat_id=chat_id, user_id=user_id)
            if image_html is not None:
                logger.info(f"HTML graph visualization refine succeeded: {image_html.visualization_types}")
                return image_html.html_code
        except Exception as e:
            logger.error(f"HTML graph visualization refine failed: {str(e)}")
    else:
        try:
            # raise Exception("test")
            logger.info("Decision=False: Trying infographic visualization refine")
            info_viz = generate_info_visualization_refine(table, user_refine_prompt=user_prompt, existing_viz=existing_viz, chat_id=chat_id, user_id=user_id)
            if info_viz is not None:
                logger.info(f"Infographic visualization refine succeeded: {info_viz.visualization_type}")
                return info_viz.html_code
        except Exception as e:
            logger.error(f"Infographic visualization refine failed: {str(e)}")

    # Step 3: Fallback to Gemini image generation (table + user_ins only, NOT existing viz)
    REFINE_VIZ_PROMPT = TABLE_TO_VIZ_PROMPT.format(table=table) + f" Make it in English only {user_prompt}(ENGLISH ONLY)" if user_prompt else TABLE_TO_VIZ_PROMPT.format(table=table)
    last_error = None
    try:
        # raise Exception("test")
        logger.info(f"Using Gemini for visualization (table + user_ins only)")
        for attempt in range(1, max_retries + 1):
            api_key_num = ((attempt - 1) % 6) + 1
            Google_client = genai.Client(api_key=os.getenv(f"GEMINI_API_KEY_{api_key_num}"))
            try:
                logger.info(f"Generating table visualization (attempt {attempt}/{max_retries} using GEMINI_API_KEY_{api_key_num})")
                interaction = Google_client.interactions.create(
                    model=VISUALIZATION_MODEL,
                    input=REFINE_VIZ_PROMPT,
                    response_format={
                        "type": "image",
                        "aspect_ratio": aspect_ratio,
                    },
                )
                save_raw_llm_response(interaction, VISUALIZATION_MODEL, "Regenerating a chart image after edits", chat_id, user_id=user_id)

                if interaction.output_image is not None:
                    image_path = f"{uuid7()}.png"
                    with open(image_path, "wb") as f:
                        f.write(base64.b64decode(interaction.output_image.data))
                    logger.info(f"Generated image: {image_path}")
                    return image_path

            except Exception as e:
                logger.error(f"Attempt {attempt}/{max_retries} (GEMINI_API_KEY_{api_key_num}) failed: {e}")
                last_error = e
                if attempt == max_retries:
                    logger.warning(f"All {max_retries} Gemini API attempts failed")
                    raise last_error
                continue

        logger.error(f"All {max_retries} Gemini API attempts failed, no fallback available")
        return None

    except Exception as e:
        logger.error(f"Error generating visualization with Gemini: {str(e)}")
        # PandasAI fallback removed: pandasai has no build for Python 3.14 (see
        # pyproject requires-python). html_graph_maker_refine + Gemini remain as
        # the visualization path; if both fail there is no chart for this table.
        return None


def generate_visualization(table: str, user_prompt: str, user_name: str, chat_id: str, existing_viz: str = None, user_id: Optional[str] = None)->str:
    """
    Generate a refined visualization for a given table.
    Args:
        table: The table to generate a visualization for.
        user_prompt: The user's refinement instruction.
        user_name: The name of the user.
        chat_id: The id of the chat.
        existing_viz: The existing HTML visualization to refine (passed to HTML generators only).
    Returns:
        The S3 path to the generated visualization (.png or .html), or empty string on failure.
    """
    
    if not table or not table.strip():
        logger.error("Table content is empty or None")
        return None
    
    image_path = None
    html_temp_path = None
    try:
        image_path = generate_refine_table_visualization(table, user_prompt, chat_id, existing_viz=existing_viz, user_id=user_id)
        if image_path and isinstance(image_path, str) and not image_path.endswith(".png"):
            s3_controls = get_s3_instance()
            now = datetime.datetime.now()
            year = now.strftime('%Y')
            month = now.strftime('%m')
            day = now.strftime('%d')

            html_filename = f"{uuid7()}.html"
            html_temp_path = html_filename
            with open(html_temp_path, "w", encoding="utf-8") as f:
                f.write(image_path)

            s3_key = f"{S3_REPORTS_BASE_PATH}/visualizations/{year}/{month}/{day}/{user_name}/chat_{chat_id}/{html_filename}"
            s3_path = s3_controls.upload_file(html_temp_path, s3_key)
            if s3_path:
                logger.info(f"Uploaded HTML visualization to S3: {s3_path}")
                return s3_path
            else:
                logger.error(f"Failed to upload HTML visualization to S3")
                return image_path
        if image_path:
            s3_controls = get_s3_instance()
            now = datetime.datetime.now()
            year = now.strftime('%Y')
            month = now.strftime('%m')
            day = now.strftime('%d')
            s3_key = f"{S3_REPORTS_BASE_PATH}/visualizations/{year}/{month}/{day}/{user_name}/chat_{chat_id}/{image_path}"
            s3_path = s3_controls.upload_file(image_path, s3_key)
            if s3_path:
                logger.info(f"Uploaded image to S3: {s3_path}")
                return s3_path
            else:
                logger.error(f"Failed to upload image to S3: {s3_path}")
                return ""
    finally:
        if html_temp_path and os.path.exists(html_temp_path):
            os.remove(html_temp_path)
            logger.info(f"Deleted temp HTML file: {html_temp_path}")
        if image_path and os.path.exists(str(image_path)):
            os.remove(image_path)
            logger.info(f"Deleted image from local directory: {image_path}")
        if os.path.exists(f"temp_viz_dir_{chat_id}"):
            shutil.rmtree(f"temp_viz_dir_{chat_id}")
            logger.info(f"Deleted temporary directory: temp_viz_dir_{chat_id}")
    return ""

def refine_viz(input_json: dict, user_name: str, chat_id: str, user_id: Optional[str] = None)->str:
    """
    Refine a visualization for a given table.

    Input JSON format:
    {
        "user_ins": "user's refinement instruction",
        "table": "the table data (markdown/text)",
        "viz": "existing visualization html (NOT used for generation)"
    }

    The 'viz' key holds the existing HTML but is NOT passed into any generator.
    Only 'table' and 'user_ins' are used for regeneration.

    Args:
        input_json: The input JSON with user_ins, table, and viz keys.
        user_name: The name of the user.
        chat_id: The id of the chat.
    Returns:
        The path/html of the refined visualization.
    """
    try:
        user_prompt = input_json.get("user_ins", "")
        table = input_json.get("table", "")
        existing_viz = input_json.get("viz", "")
        Refined_viz_path = generate_visualization(table, user_prompt, user_name, chat_id, existing_viz=existing_viz, user_id=user_id)
        if not Refined_viz_path:
            logger.error(f"No Refined visualization is generated")
            return ""
        return Refined_viz_path
    except Exception as e:
        logger.error(f"Error refining viz: {str(e)}")
        return ""
