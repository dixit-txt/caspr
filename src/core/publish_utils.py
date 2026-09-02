import io
import os
from uuid_utils import uuid7
import aiofiles
import aiofiles.os
import aiofiles.tempfile
from google import genai
import httpx
from openai import AsyncOpenAI
import json 
from PIL import Image
import fitz
import asyncio
import markdown
import base64
import re
from weasyprint import HTML


from src.config.constants import (
    ENVIRONMENT,
    HEYGEN_API_KEY,
    HEYGEN_AVATAR_ID,
    HEYGEN_FOLDER_ID,
    HEYGEN_VOICE_ID,
    IMAGE_TO_TEXT_MODEL_ID,
    OPENAI_API_KEY,
    GEMINI_API_KEY,
    CASPR_INFO_HEYGEN,
    REPORT_TEXTUAL_ANALYSIS_MODEL,
    GEMINI_REPORT_TEXTUAL_ANALYSIS_MODEL,
    S3_REPORTS_BASE_PATH,
    TEXT_TO_IMAGE_MODEL_ID
)
from src.core.prompt_utils import (
    PUBLISH_QUES_ANS_PROMPT,
    PUBLISH_QUES_ANS_SCHEMA,
    PUBLISH_STUFF_PROMPT,
    PUBLISH_STUFF_SCHEMA,
    PUBLISH_OVERVIEW_PROMPT,
    PUBLISH_OVERVIEW_SCHEMA,
    PUBLISH_REPORT_DETAILS_PROMPT,
    PUBLISH_REPORT_DETAILS_SCHEMA,
    PUBLISH_INSIGHTS_PROMPT,
    PUBLISH_INSIGHTS_SCHEMA,
)
from src.config.log_helper import setup_logging
from src.core.s3_utils import get_s3_instance
from src.core.llm_response_logger import save_raw_llm_response, strip_json_code_fence

logger = setup_logging(__file__)

openai_client = AsyncOpenAI(api_key=OPENAI_API_KEY)
gemini_client = genai.Client(api_key=GEMINI_API_KEY)
s3_instance = get_s3_instance()


async def report_textual_analysis(prompt: str, schema: dict, pdf_name: str, pdf_b64: str, model: str = REPORT_TEXTUAL_ANALYSIS_MODEL, chat_id: str | None = None, user_id: str | None = None) -> str:
    logger.info(f"Analyzing report: {pdf_name}")
    try:
        response = await openai_client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": "You are a document analysis assistant."},
                {"role": "user", "content": [
                    {"type": "file", "file": {
                        "filename": pdf_name,
                        "file_data": f"data:application/pdf;base64,{pdf_b64}"
                    }},
                    {"type": "text", "text": prompt}
                ]}
            ],
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": "report_analysis",
                    "schema": schema, 
                    "strict": True
                }
            }
        )
        save_raw_llm_response(response, model, "Analyzing report text for publishing", chat_id, user_id=user_id)
        logger.info(f"Analysis complete for {pdf_name}")
        return json.loads(response.choices[0].message.content)
    except Exception as e:
        logger.error(f"OpenAI report_textual_analysis failed for {pdf_name} ({e}), falling back to Gemini API")
        interaction = await gemini_client.aio.interactions.create(
            model=GEMINI_REPORT_TEXTUAL_ANALYSIS_MODEL,
            input=[
                {"type": "document", "data": pdf_b64, "mime_type": "application/pdf"},
                {"type": "text", "text": prompt},
            ],
            system_instruction="You are a document analysis assistant.",
            response_format={
                "type": "text",
                "mime_type": "application/json",
                "schema": schema,
            },
            generation_config={
                "temperature": 0.1,
                "thinking_config": {"thinking_budget": 0},
            },
        )
        save_raw_llm_response(interaction, GEMINI_REPORT_TEXTUAL_ANALYSIS_MODEL, "Analyzing report text for publishing (backup)", chat_id, user_id=user_id)
        logger.info(f"Gemini fallback analysis complete for {pdf_name}")
        return json.loads(strip_json_code_fence(interaction.output_text))

async def extract_first_page_as_image(pdf_path: str) -> Image.Image:
    """Extract the first page of a PDF as an image asynchronously"""
    # This function performs I/O operations, so we'll run it in a thread pool
    def _extract_first_page():
        doc = fitz.open(pdf_path)
        page = doc.load_page(0)
        pix = page.get_pixmap(dpi=200)
        img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
        doc.close()
        return img
    
    return await asyncio.to_thread(_extract_first_page)

async def generate_poster_prompt_from_image(image: Image.Image, chat_id: str | None = None, user_id: str | None = None) -> str:
    image_buffer = io.BytesIO()
    image.save(image_buffer, format="PNG")
    image_b64 = base64.b64encode(image_buffer.getvalue()).decode("utf-8")

    interaction = await gemini_client.aio.interactions.create(
        model=IMAGE_TO_TEXT_MODEL_ID,
        input=[
            {"type": "image", "data": image_b64, "mime_type": "image/png"},
            {
                "type": "text",
                "text": (
                    "Based on the visual and the textual content of the image(the main title in the image), "
                    "generate a brief and imaginative poster prompt for an AI image generator. "
                    "In the prompt explicitly mention not to include any text or numbers."
                ),
            },
        ],
    )
    save_raw_llm_response(interaction, IMAGE_TO_TEXT_MODEL_ID, "Describing an image to create a matching poster", chat_id, user_id=user_id)
    return interaction.output_text.strip()

async def generate_poster_images(prompt: str, num_images: int = 1, chat_id: str | None = None, user_id: str | None = None):
    response = await gemini_client.aio.models.generate_images(
        model=TEXT_TO_IMAGE_MODEL_ID,
        prompt=prompt,
        config=genai.types.GenerateImagesConfig(number_of_images=num_images)
    )
    save_raw_llm_response(response, TEXT_TO_IMAGE_MODEL_ID, "Generating poster images for publishing", chat_id, user_id=user_id)
    return response.generated_images

async def safe_remove(path):
    """Safely remove a file asynchronously"""
    if path and os.path.exists(path):
        try:
            await aiofiles.os.remove(path)
            logger.info(f"Deleted temporary file: {path}")
        except Exception as e:
            logger.warning(f"Failed to delete temp file {path}: {e}")
    else:
        logger.info(f"File {path} does not exist")


async def generate_poster(user_id: str, user_name: str, file_contents: bytes):
    logger.info(f"Generating poster for {user_id}")
    tmp_pdf_path = None
    tmp_image_path = None
    
    try:
        async with aiofiles.tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
            await tmp.write(file_contents)
            tmp_pdf_path = tmp.name

        logger.info(f"Extracting first page as image for user: {user_id} and user_name: {user_name}")
        image = await extract_first_page_as_image(tmp_pdf_path)
        
        logger.info(f"Generating poster prompt for user: {user_id} and user_name: {user_name}")
        poster_prompt = await generate_poster_prompt_from_image(image, user_id=user_id)
        
        logger.info(f"Generating poster images for user: {user_id} and user_name: {user_name}")
        poster_images = await generate_poster_images(poster_prompt, num_images=1, user_id=user_id)
        
        if not poster_images:
            raise RuntimeError("No image returned by generator")
        
        first = poster_images[0]
        if not getattr(first, "image", None) or not getattr(first.image, "image_bytes", None):
            raise RuntimeError("Image bytes missing in response")
        poster_image_bytes = first.image.image_bytes

        logger.info(f"Saving poster image to temporary file for user: {user_id} and user_name: {user_name}")
        async with aiofiles.tempfile.NamedTemporaryFile(delete=False, suffix=".png") as tmp:
            await tmp.write(poster_image_bytes)
            tmp_image_path = tmp.name

        logger.info(f"Uploading poster image to S3 for user: {user_id} and user_name: {user_name}")
        image_id = str(uuid7())
        s3_key = S3_REPORTS_BASE_PATH + f"/published_reports/{user_name}_{user_id}/{image_id}.png"

        # Upload using sync S3 instance
        s3_uri = await asyncio.to_thread(lambda: s3_instance.upload_file(
            local_file_path=tmp_image_path,
            s3_key=s3_key
        ))

        return s3_uri, image_id

    except Exception as e:
        logger.error(f"Error generating poster: {str(e)} for user: {user_id} and user_name: {user_name}")
        return None, None
    
    finally:
        if tmp_pdf_path:
            await safe_remove(tmp_pdf_path)
        if tmp_image_path:
            await safe_remove(tmp_image_path)

async def generate_video(script: str, user_id: str, user_name: str):
    try:
        logger.info(f"Generating video for {user_id} and user_name: {user_name}")
        payload = {
            "caption": True,
            "title": f"{ENVIRONMENT} Video via API",
            "callback_id": "Caspr_Callback",
            "video_inputs": [
                {
                    "character": {
                        "type": "avatar",
                        "avatar_id": HEYGEN_AVATAR_ID,
                        "avatar_style": "normal",
                        "scale": 1.0
                    },
                    "voice": {
                        "type": "text",
                        "voice_id": HEYGEN_VOICE_ID,
                        "input_text": script,
                        "speed": 1.0,
                        "pitch": 10,
                        "emotion": "Excited",
                        "locale": "en-US"
                    },
                    # "background": {
                    #     # "type": "color",
                    #     # "value": "#eeece1"
                    #     "type": "image",
                    #     "url": "https://images.pexels.com/photos/380768/pexels-photo-380768.jpeg"
                    # }
                }
            ],
            "dimension": {
                "width": 1280,
                "height": 720
            },
            "folder_id": HEYGEN_FOLDER_ID
        }

        headers = {
            "accept": "application/json",
            "content-type": "application/json",
            "x-api-key": HEYGEN_API_KEY
        }

        url = "https://api.heygen.com/v2/video/generate"

        async with httpx.AsyncClient() as client:
            response = await client.post(url, json=payload, headers=headers)

        if response.status_code == 200 and not response.json().get("error"):
            logger.info(f"Video generation request sent successfully for {user_id} and user_name: {user_name}")
            return response.json().get("data", {}).get("video_id", "")
        else:
            logger.error(f"Error generating video: {response.json().get('error', 'Unknown error')}")
            raise Exception(f"Error generating video: {response.json().get('error', 'Unknown error')}")
    except Exception as e:
        logger.error(f"Error generating video: {str(e)} for user_id: {user_id} and user_name: {user_name}")
        return None
    
async def retrieve_video(video_id: str):
    url = f"https://api.heygen.com/v1/video_status.get?video_id={video_id}"

    headers = {
        "accept": "application/json",
        "x-api-key": HEYGEN_API_KEY
    }

    async with httpx.AsyncClient() as client:
        response = await client.get(url, headers=headers)

    if response.status_code == 200 and not response.json()['data']['error']:
        return response.json()
    else:
        logger.error(f"Error retrieving video: {response.json()}")
        raise Exception(f"Error retrieving video: {response.json()}")
        

async def analyze_report(pdf_base64: str, file_name: str, chat_id: str | None = None, user_id: str | None = None):
    logger.info(f"Calling textual analysis function for {file_name} by stuffing all information at once")
    try:
        analysis_result = await report_textual_analysis(
            prompt=PUBLISH_STUFF_PROMPT,
            schema=PUBLISH_STUFF_SCHEMA,
            pdf_name=file_name,
            pdf_b64=pdf_base64,
            chat_id=chat_id,
            user_id=user_id,
        )
        return analysis_result

    except Exception as e:
        logger.error(f"Error in textual analysis: {str(e)} when stuffing all information at once. Trying to analyze report by extracting information one by one for {file_name}")
        
        # Initialize analysis result structure with empty defaults
        analysis_result = {
            "overview": {},
            "report_title": {},
            "script_summary": {},
            "report_details": {},
            "insights": [],
            "ques_ans": []
        }

        # Define analysis components to extract
        analysis_components = [
            {
                "name": "overview",
                "prompt": PUBLISH_OVERVIEW_PROMPT,
                "schema": PUBLISH_OVERVIEW_SCHEMA,
                "fields": ["overview", "script_summary"]
            },
            {
                "name": "report_details",
                "prompt": PUBLISH_REPORT_DETAILS_PROMPT,
                "schema": PUBLISH_REPORT_DETAILS_SCHEMA,
                "fields": ["report_details"]
            },
            {
                "name": "insights",
                "prompt": PUBLISH_INSIGHTS_PROMPT,
                "schema": PUBLISH_INSIGHTS_SCHEMA,
                "fields": ["insights"]
            },
            {
                "name": "question-answer pairs",
                "prompt": PUBLISH_QUES_ANS_PROMPT,
                "schema": PUBLISH_QUES_ANS_SCHEMA,
                "fields": ["ques_ans"]
            }
        ]

        # Process each component independently
        for component in analysis_components:
            try:
                logger.info(f"Calling textual analysis function for {file_name} for extracting {component['name']}")
                result = await report_textual_analysis(
                    prompt=component["prompt"],
                    schema=component["schema"],
                    pdf_name=file_name,
                    pdf_b64=pdf_base64,
                    chat_id=chat_id,
                    user_id=user_id,
                )

                # Update analysis result with extracted fields
                for field in component["fields"]:
                    analysis_result[field] = result.get(field, {})

                logger.info(f"{component['name'].capitalize()} analysis complete for {file_name}")
                
            except Exception as e:
                logger.error(f"Error in {component['name']} analysis: {str(e)}")
                logger.info(f"Continuing with next component for {file_name}")

        return analysis_result


async def md_to_base64_pdf(raw_markdown: str):
    """
    Convert markdown to base64-encoded PDF asynchronously using weasyprint.
    
    Args:
        raw_markdown: The markdown content as a string
        
    Returns:
        tuple: (base64_encoded_pdf, pdf_bytes) - Base64-encoded PDF content and raw PDF bytes
    """
    def replace_section_id(match):
        """Used this function because {#3.2.-Leading-cloud-providers-andtheir-market-share} -> 3-2-leading-cloud-providers-and-their-market-share"""
        section_id = match.group(1)
        clean_id = section_id.replace('.', '-').replace(' ', '-')
        return f'<a id="{clean_id}"></a>'   
     
    # Fix markdown section IDs and convert to HTML
    fixed_markdown = re.sub(r'\{#(.*?)\}', replace_section_id, raw_markdown)
    html_content = markdown.markdown(fixed_markdown, extensions=["tables", "fenced_code", "extra"])
    
    # Add basic CSS styling for better PDF output
    styled_html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="utf-8">
        <style>
            body {{
                font-family: Arial, sans-serif;
                line-height: 1.6;
                margin: 20px;
                color: #333;
            }}
            h1, h2, h3, h4, h5, h6 {{
                color: #2c3e50;
                margin-top: 1.5em;
                margin-bottom: 0.5em;
            }}
            table {{
                border-collapse: collapse;
                width: 100%;
                margin: 1em 0;
            }}
            th, td {{
                border: 1px solid #ddd;
                padding: 8px;
                text-align: left;
            }}
            th {{
                background-color: #f2f2f2;
                font-weight: bold;
            }}
            code {{
                background-color: #f4f4f4;
                padding: 2px 4px;
                border-radius: 3px;
                font-family: monospace;
            }}
            pre {{
                background-color: #f4f4f4;
                padding: 10px;
                border-radius: 3px;
                overflow-x: auto;
            }}
            blockquote {{
                border-left: 4px solid #ddd;
                margin: 0;
                padding-left: 1em;
                color: #666;
            }}
        </style>
    </head>
    <body>
        {html_content}
    </body>
    </html>
    """
    
    # Run the PDF generation in a thread pool to avoid blocking
    def _generate_pdf():
        html_doc = HTML(string=styled_html)
        pdf_bytes = html_doc.write_pdf()
        pdf_base64 = base64.b64encode(pdf_bytes).decode('utf-8')
        return pdf_base64, pdf_bytes
    
    return await asyncio.to_thread(_generate_pdf)

async def publish_report(raw_md_content: str, user_id: str, user_name: str, report_title: str):
    try:
        logger.info(f"Publishing report for user_id: {user_id} user_name: {user_name} report_title: {report_title}")
        pdf_base64, pdf_bytes = await md_to_base64_pdf(raw_md_content)

        analysis_result = await analyze_report(pdf_base64, f"{report_title}.pdf", user_id=user_id)

        poster_s3_uri, poster_image_id = await generate_poster(user_id=user_id, user_name=user_name, file_contents=pdf_bytes)

        video_script = analysis_result['script_summary'] + "\n\n" + CASPR_INFO_HEYGEN

        if analysis_result.get("script_summary", ""):
            video_id = await generate_video(script=video_script, user_id=user_id, user_name=user_name)

        return {
            "faq": analysis_result.get("ques_ans", []),
            "insights": analysis_result.get("insights", []),
            "industries_jobs": analysis_result.get("report_details", {}).get("focus_areas", {}).get("industries_jobs", ""),
            "geographic_areas": analysis_result.get("report_details", {}).get("focus_areas", {}).get("geographic_areas", ""),
            "special_emphasis": analysis_result.get("report_details", {}).get("focus_areas", {}).get("special_emphasis", ""),
            "audience": analysis_result.get("report_details", {}).get("perspective", {}).get("audience", ""),
            "purpose": analysis_result.get("report_details", {}).get("perspective", {}).get("purpose", ""),
            "overview": analysis_result.get("overview", ""),
            "media_details": {
                "poster_s3_uri": poster_s3_uri, 
                "heygen_video_id": video_id,
                "heygen_folder_id": HEYGEN_FOLDER_ID
            }
        }
    
    except Exception as e:
        logger.error(f"Error in publishing report: {e} for user_id: {user_id} user_name: {user_name} report_title: {report_title}")
        return {}   
    

async def test_publish_report(file_path: str, user_id: str, user_name: str, report_title: str):
    """
    Read a markdown file and publish the report using its contents
    
    Args:
        file_path: Path to the markdown file
        user_id: User ID
        user_name: User name
        report_title: Title of the report
        
    Returns:
        The result from publish_report function
    """
    try:
        logger.info(f"Reading markdown file from {file_path} for user: {user_id}")
        
        # Read the markdown file asynchronously
        async with aiofiles.open(file_path, 'r', encoding='utf-8') as file:
            raw_md_content = await file.read()
            
        # Publish the report using the content
        return await publish_report(
            raw_md_content=raw_md_content,
            user_id=user_id,
            user_name=user_name,
            report_title=report_title
        )
    
    except Exception as e:
        logger.error(f"Error reading or publishing report from file {file_path}: {str(e)}")
        return {}
    

# print(asyncio.run(test_publish_report(
#         file_path="/home/namanbhatia/Documents/editor/consult_gpt/report_generation/caspr_backend/fork/caspr_backend/consolidated_raw_report.md",
#         user_id="1",
#         user_name="namanbhatia",
#         report_title="Test Report"
#     )))
