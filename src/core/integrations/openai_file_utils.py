import asyncio
import os
import time
from typing import Optional
from uuid_utils import uuid7
import json
import csv
import html
import markdown
import weasyprint
import chardet
from io import StringIO
from openai import OpenAI
from src.config.log_helper import setup_logging
from src.config.constants import SYNC_OPENAI_CLIENT
from src.core.integrations.s3_utils import download_file_from_s3_to_local
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Dict

logger = setup_logging(__file__)


PDF_INLINE_STYLES = """
    <style>
        @page {
            margin: 2cm;
            size: A4;
        }
        body {
            font-family: Arial, sans-serif;
            font-size: 11pt;
            line-height: 1.6;
            margin: 0;
            padding: 20px;
            word-wrap: break-word;
            overflow-wrap: break-word;
        }
        img {
            width: 525px;
            height: auto;
            max-width: 100%;
        }
        table {
            width: 100%;
            max-width: 100%;
            table-layout: fixed;
            border-collapse: collapse;
            word-wrap: break-word;
            overflow-wrap: break-word;
            margin: 10px 0;
        }
        th, td {
            padding: 8px;
            text-align: left;
            vertical-align: top;
            overflow-wrap: break-word;
            hyphens: auto;
            word-break: break-word;
            border: 1px solid #ddd;
        }
        pre {
            white-space: pre-wrap;
            word-wrap: break-word;
            background-color: #f5f5f5;
            padding: 10px;
            border-radius: 4px;
            overflow-wrap: break-word;
        }
        code {
            word-wrap: break-word;
            overflow-wrap: break-word;
        }
        p, div, span {
            word-wrap: break-word;
            overflow-wrap: break-word;
        }
    </style>
"""


def detect_encoding(file_path: str) -> str:
    """
    Detect the encoding of a file using chardet.
    Args:
        file_path: Path to the file
    Returns:
        Detected encoding as string
    """
    try:
        with open(file_path, "rb") as f:
            raw_data = f.read()
            result = chardet.detect(raw_data)
            print(result)
            encoding = result.get("encoding", "utf-8")
            confidence = result.get("confidence", 0)
            
            logger.info(f"Detected encoding: {encoding} (confidence: {confidence})")
            
            # If confidence is low or encoding is None, fallback to common encodings
            if confidence < 0.7 or encoding is None:
                logger.warning(f"Low confidence in encoding detection. Trying fallback encodings.")
                return "utf-8"

            return encoding
    except Exception as e:
        logger.error(f"Error detecting encoding: {e}. Defaulting to utf-8")
        return "utf-8"


def read_file_with_encoding(file_path: str) -> str:
    """
    Read file content with automatic encoding detection and fallback mechanisms.:
    Args:
        file_path: Path to the file
    Returns:
        File content as string
    """
    # Try detected encoding first
    detected_encoding = detect_encoding(file_path)
    encodings_to_try = [detected_encoding, "utf-8", "latin-1", "cp1252", "iso-8859-1", "ascii"]
    
    for encoding in encodings_to_try:
        try:
            with open(file_path, "r", encoding=encoding, errors="replace") as f:
                content = f.read()
                logger.info(f"Successfully read file with {encoding} encoding")
                return content
        except Exception as e:
            logger.warning(f"Failed to read with {encoding} encoding: {e}")
            continue
    
    # If all encodings fail, read as binary and decode with errors='replace'
    logger.warning("All encoding attempts failed. Reading with error replacement.")
    with open(file_path, "rb") as f:
        return f.read().decode("utf-8", errors="replace")


def create_html_document(content: str, title: str = "Document") -> str:
    """
    Wrap content in a proper HTML document structure with inline CSS.
    Args:
        content: HTML content to wrap
        title: Document title
    Returns:
        Complete HTML document as string
    """
    return f"""
<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <title>{html.escape(title)}</title>
    {PDF_INLINE_STYLES}
</head>
<body>
    {content}
</body>
</html>
"""


def convert_csv_to_html(file_path: str) -> str:
    """
    Convert CSV file to HTML table with proper encoding handling.
    Args:
        file_path: Path to CSV file
    Returns:
        HTML content as string
    """
    content = read_file_with_encoding(file_path)
    csv_reader = csv.reader(StringIO(content))
    
    rows = list(csv_reader)
    if not rows:
        return "<p>Empty CSV file</p>"
    
    html_content = '<table>\n'
    
    # First row as header
    if rows:
        html_content += '  <thead>\n    <tr>\n'
        for cell in rows[0]:
            html_content += f'      <th>{html.escape(str(cell))}</th>\n'
        html_content += '    </tr>\n  </thead>\n'
    
    # Remaining rows as body
    if len(rows) > 1:
        html_content += '  <tbody>\n'
        for row in rows[1:]:
            html_content += '    <tr>\n'
            for cell in row:
                html_content += f'      <td>{html.escape(str(cell))}</td>\n'
            html_content += '    </tr>\n'
        html_content += '  </tbody>\n'
    
    html_content += '</table>'
    return html_content


def convert_json_to_html(file_path: str) -> str:
    """
    Convert JSON file to formatted HTML with proper encoding handling.
    Args:
        file_path: Path to JSON file
    Returns:
        HTML content as string
    """
    content = read_file_with_encoding(file_path)
    
    try:
        json_data = json.loads(content)
        formatted_json = json.dumps(json_data, indent=2, ensure_ascii=False)
        return f'<pre><code>{html.escape(formatted_json)}</code></pre>'
    except json.JSONDecodeError as e:
        logger.error(f"Invalid JSON format: {e}")
        return f'<pre><code>{html.escape(content)}</code></pre>'


def convert_xml_to_html(file_path: str) -> str:
    """
    Convert XML file to formatted HTML with proper encoding handling.
    Args:
        file_path: Path to XML file
    Returns:
        HTML content as string
    """
    content = read_file_with_encoding(file_path)
    return f'<pre><code>{html.escape(content)}</code></pre>'


def convert_text_to_html(file_path: str) -> str:
    """
    Convert plain text file to HTML with proper encoding handling.
    Args:
        file_path: Path to text file
    Returns:
        HTML content as string
    """
    content = read_file_with_encoding(file_path)
    # Preserve line breaks and spaces
    paragraphs = content.split('\n\n')
    html_content = ''
    
    for para in paragraphs:
        if para.strip():
            # Replace single line breaks with <br>, escape HTML
            formatted_para = html.escape(para).replace('\n', '<br>\n')
            html_content += f'<p>{formatted_para}</p>\n'
    
    return html_content if html_content else f'<pre>{html.escape(content)}</pre>'


def convert_docx_to_html(file_path: str) -> str:
    """
    Convert DOCX file to HTML. Falls back to plain text extraction if needed.
    Args:
        file_path: Path to DOCX file
    Returns:
        HTML content as string
    """
    try:
        # Try using python-docx if available
        try:
            from docx import Document
            doc = Document(file_path)
            html_content = ''
            
            for para in doc.paragraphs:
                if para.text.strip():
                    html_content += f'<p>{html.escape(para.text)}</p>\n'
            
            # Handle tables
            for table in doc.tables:
                html_content += '<table>\n'
                for i, row in enumerate(table.rows):
                    html_content += '  <tr>\n'
                    tag = 'th' if i == 0 else 'td'
                    for cell in row.cells:
                        html_content += f'    <{tag}>{html.escape(cell.text)}</{tag}>\n'
                    html_content += '  </tr>\n'
                html_content += '</table>\n'
            
            return html_content if html_content else '<p>Empty document</p>'
        except ImportError:
            logger.warning("python-docx not available. Trying mammoth...")
            
            # Try mammoth if available
            try:
                import mammoth
                with open(file_path, "rb") as docx_file:
                    result = mammoth.convert_to_html(docx_file)
                    return result.value
            except ImportError:
                logger.error("Neither python-docx nor mammoth available for DOCX conversion")
                return None
    except Exception as e:
        logger.error(f"Error converting DOCX: {e}")
        return None


def convert_markdown_to_html(file_path: str) -> str:
    """
    Convert Markdown file to HTML with proper encoding handling.
    Args:
        file_path: Path to Markdown file
    Returns:
        HTML content as string
    """
    content = read_file_with_encoding(file_path)
    html_content = markdown.markdown(
        content, 
        extensions=["tables", "fenced_code", "extra", "nl2br", "sane_lists"]
    )
    return html_content


def convert_excel_to_html(file_path: str) -> str:
    """
    Convert Excel file (.xls or .xlsx) to HTML tables with proper formatting.
    Args:
        file_path: Path to Excel file
    Returns:
        HTML content as string
    """
    try:
        # Try using openpyxl/pandas for Excel files
        try:
            import pandas as pd
            
            # Read the Excel file (handles both .xls and .xlsx)
            excel_file = pd.ExcelFile(file_path)
            html_content = ''
            
            # Process each sheet
            for sheet_name in excel_file.sheet_names:
                df = pd.read_excel(excel_file, sheet_name=sheet_name)
                
                # Add sheet name as heading if multiple sheets
                if len(excel_file.sheet_names) > 1:
                    html_content += f'<h2>{html.escape(sheet_name)}</h2>\n'
                
                # Convert DataFrame to HTML table
                if not df.empty:
                    html_content += '<table>\n'
                    
                    # Table header
                    html_content += '  <thead>\n    <tr>\n'
                    for col in df.columns:
                        html_content += f'      <th>{html.escape(str(col))}</th>\n'
                    html_content += '    </tr>\n  </thead>\n'
                    
                    # Table body
                    html_content += '  <tbody>\n'
                    for _, row in df.iterrows():
                        html_content += '    <tr>\n'
                        for val in row:
                            # Handle NaN and None values
                            cell_value = '' if pd.isna(val) else str(val)
                            html_content += f'      <td>{html.escape(cell_value)}</td>\n'
                        html_content += '    </tr>\n'
                    html_content += '  </tbody>\n'
                    html_content += '</table>\n'
                else:
                    html_content += '<p>Empty sheet</p>\n'
            
            return html_content if html_content else '<p>Empty Excel file</p>'
            
        except ImportError:
            logger.error("pandas library not available for Excel conversion")
            return None
            
    except Exception as e:
        logger.error(f"Error converting Excel file: {e}")
        return None


def process_different_file_formats(file_path: str) -> str:
    """
    Process different file formats and return the path to the processed PDF file.
    Handles various encodings robustly and ensures proper PDF formatting.
    
    Args:
        file_path: The path to the file
    Returns:
        The path to the processed PDF file, or None if conversion fails
    Raises:
        Exception: If the file format is not supported or conversion fails
    Supported file formats: .md, .pdf, .docx, .doc, .txt, .json, .csv, .xml, .html, .xls, .xlsx
    """
    logger.info(f"[PROCESS_FILE] Starting file processing | file_path={file_path}")
    
    if file_path.lower().endswith(".pdf"):
        if not os.path.exists(file_path):
            logger.error(f"[PROCESS_FILE] PDF file does not exist | path={file_path}")
            return None
        logger.info(f"[PROCESS_FILE] File is already PDF | path={file_path}")
        return file_path
    # use the actual file name for the pdf file path
    pdf_file_path = f"{os.path.basename(file_path).replace('.', '_')}_{uuid7()}.pdf"
    file_extension = os.path.splitext(file_path)[1].lower()
    logger.info(f"[PROCESS_FILE] File details | extension={file_extension} | output_pdf={pdf_file_path}")

    try:
        html_content = None
                
        if file_extension == ".md":
            logger.info(f"[PROCESS_FILE] Converting Markdown to PDF")
            html_body = convert_markdown_to_html(file_path)
            html_content = create_html_document(html_body, "Markdown Document")
            logger.info(f"[PROCESS_FILE] Markdown converted | html_length={len(html_content)}")
            
        elif file_extension == ".docx":
            logger.info(f"[PROCESS_FILE] Converting DOCX to PDF")
            html_body = convert_docx_to_html(file_path)
            if html_body is None:
                logger.error(f"[PROCESS_FILE] DOCX conversion failed | file_path={file_path}")
                return None
            html_content = create_html_document(html_body, "Word Document")
            logger.info(f"[PROCESS_FILE] DOCX converted | html_length={len(html_content)}")
            
        elif file_extension == ".doc":
            logger.info("[PROCESS_FILE] Converting DOC to PDF")
            # DOC files are harder to parse, try basic text extraction
            try:
                import textract
                text = textract.process(file_path).decode('utf-8', errors='replace')
                html_body = convert_text_to_html_from_string(text)
                html_content = create_html_document(html_body, "Word Document")
                logger.info(f"[PROCESS_FILE] DOC converted using textract | text_length={len(text)}")
            except ImportError:
                logger.error("[PROCESS_FILE] textract not available for DOC conversion")
                return None
                
        elif file_extension == ".txt":
            logger.info(f"[PROCESS_FILE] Converting TXT to PDF")
            html_body = convert_text_to_html(file_path)
            html_content = create_html_document(html_body, "Text Document")
            logger.info(f"[PROCESS_FILE] TXT converted | html_length={len(html_content)}")
            
        elif file_extension == ".json":
            logger.info(f"[PROCESS_FILE] Converting JSON to PDF")
            html_body = convert_json_to_html(file_path)
            html_content = create_html_document(html_body, "JSON Document")
            logger.info(f"[PROCESS_FILE] JSON converted | html_length={len(html_content)}")
            
        elif file_extension == ".csv":
            logger.info(f"[PROCESS_FILE] Converting CSV to PDF")
            html_body = convert_csv_to_html(file_path)
            html_content = create_html_document(html_body, "CSV Document")
            logger.info(f"[PROCESS_FILE] CSV converted | html_length={len(html_content)}")
            
        elif file_extension == ".xml":
            logger.info(f"[PROCESS_FILE] Converting XML to PDF")
            html_body = convert_xml_to_html(file_path)
            html_content = create_html_document(html_body, "XML Document")
            logger.info(f"[PROCESS_FILE] XML converted | html_length={len(html_content)}")
            
        elif file_extension == ".html" or file_extension == ".htm":
            logger.info(f"[PROCESS_FILE] Converting HTML to PDF")
            content = read_file_with_encoding(file_path)
            # If already has html/body tags, use as-is but inject styles
            if '<html' in content.lower():
                # Inject styles into existing HTML
                if '<head>' in content.lower():
                    html_content = content.replace('<head>', f'<head>\n{PDF_INLINE_STYLES}', 1)
                else:
                    html_content = content.replace('<html>', f'<html>\n<head>\n{PDF_INLINE_STYLES}\n</head>', 1)
                logger.info(f"[PROCESS_FILE] Injected styles into existing HTML")
            else:
                # Wrap in HTML document
                html_content = create_html_document(content, "HTML Document")
                logger.info(f"[PROCESS_FILE] Wrapped HTML in document structure")
                
        elif file_extension == ".xls" or file_extension == ".xlsx":
            logger.info(f"[PROCESS_FILE] Converting Excel to PDF")
            html_body = convert_excel_to_html(file_path)
            if html_body is None:
                logger.error(f"[PROCESS_FILE] Excel conversion failed | file_path={file_path}")
                return None
            html_content = create_html_document(html_body, "Excel Document")
            logger.info(f"[PROCESS_FILE] Excel converted | html_length={len(html_content)}")
            
        else:
            logger.error(f"[PROCESS_FILE] Unsupported file format | extension={file_extension}")
            raise Exception(f"Unsupported file format: {file_extension}")
        
        if html_content:
            logger.info(f"[PROCESS_FILE] Generating PDF | output={pdf_file_path} | html_length={len(html_content)}")
            weasyprint.HTML(string=html_content).write_pdf(pdf_file_path)
            pdf_size = os.path.getsize(pdf_file_path)
            logger.info(f"[PROCESS_FILE] PDF generated successfully | path={pdf_file_path} | size={pdf_size} bytes")
            return pdf_file_path
        else:
            logger.error(f"[PROCESS_FILE] No HTML content generated | file_path={file_path}")
            return None
            
    except Exception as e:
        # Clean up failed PDF file
        logger.error(f"[PROCESS_FILE] Conversion failed | extension={file_extension} | file_path={file_path} | error={str(e)}", exc_info=True)
        if os.path.exists(pdf_file_path):
            try:
                os.remove(pdf_file_path)
                logger.info(f"[PROCESS_FILE] Cleaned up failed PDF | path={pdf_file_path}")
            except:
                pass
        return None
    
def convert_text_to_html_from_string(text: str) -> str:
    """
    Convert plain text string to HTML.
    Args:
        text: Plain text content
    Returns:
        HTML content as string
    """
    paragraphs = text.split('\n\n')
    html_content = ''
    
    for para in paragraphs:
        if para.strip():
            formatted_para = html.escape(para).replace('\n', '<br>\n')
            html_content += f'<p>{formatted_para}</p>\n'
    
    return html_content if html_content else f'<pre>{html.escape(text)}</pre>'

def upload_file_to_openai(file_path: str) -> str:
    """
    Upload file to OpenAI and return the file ID.
    This should be called once per session, not per question.
    Args:
        file_path: The path to the file
    Returns:
        The file object of the uploaded file
    Raises:
        Exception: If the file upload fails
    """
    logger.info(f"[UPLOAD_FILE_OPENAI] Starting upload | file_path={file_path}")
    if not file_path:
        raise ValueError("file_path is required for OpenAI upload")
    max_retries = 3
    retry_delay = 2
    
    file_size = os.path.getsize(file_path) if os.path.exists(file_path) else 0
    logger.info(f"[UPLOAD_FILE_OPENAI] File details | size={file_size} bytes | exists={os.path.exists(file_path)}")
    
    for attempt in range(max_retries):
        try:
            logger.info(f"[UPLOAD_FILE_OPENAI] Upload attempt {attempt + 1}/{max_retries}")
            file = SYNC_OPENAI_CLIENT.files.create(
                file=open(file_path, "rb"),
                purpose="assistants"
            )
            logger.info(f"[UPLOAD_FILE_OPENAI] Upload successful | file_id={file.id} | filename={file.filename} | attempt={attempt + 1}")
            return file
        except Exception as e:
            logger.error(f"[UPLOAD_FILE_OPENAI] Upload attempt {attempt + 1} failed | error={str(e)}")
            if attempt == max_retries - 1:
                logger.error(f"[UPLOAD_FILE_OPENAI] All upload attempts failed | max_retries={max_retries}")
                raise Exception(f"Error uploading file after {max_retries} attempts: {str(e)}")
            logger.info(f"[UPLOAD_FILE_OPENAI] Retrying in {retry_delay}s")
            time.sleep(retry_delay)


def check_if_user_vector_store_exists(vector_store_name: str) -> dict:
    """
    Checks if a vector store exists for a user and chat.
    Returns dict with vector_store_id and vector_store_exists flag.
    """
    try:
        vector_stores = SYNC_OPENAI_CLIENT.vector_stores.list()
        for vector_store in vector_stores.data:
            if vector_store.name == vector_store_name:
                logger.info(f"Vector store exists: {vector_store_name}")
                return {
                    "vector_store_id": vector_store.id,
                    "vector_store_name": vector_store_name,
                    "vector_store_exists": True,
                }
        # Only return "not found" after checking ALL vector stores
        logger.info(f"Vector store does not exist: {vector_store_name}")
        return {
            "vector_store_id": None,
            "vector_store_name": vector_store_name,
            "vector_store_exists": False,
        }
    except Exception as e:
        logger.error(f"Error while checking for vector store existence: {str(e)}")
        return {
            "vector_store_id": None,
            "vector_store_name": vector_store_name,
            "vector_store_exists": False,
        }

def create_user_vector_store(vector_store_name: str):
    """
    Creates a vector store from an existing uploaded file_id.
    Returns the vector store.
    """
    try:
        result = check_if_user_vector_store_exists(vector_store_name)
        if result["vector_store_exists"]:
            return result
        else:
            logger.info(f"Vector store does not exist: {vector_store_name}")
            logger.info(f"Creating empty vector store: {vector_store_name}")
            vector_store = SYNC_OPENAI_CLIENT.vector_stores.create(
                name=vector_store_name
            )
            logger.info(f"Empty vector store created successfully: {vector_store.id}")
            return {
                "vector_store_id": vector_store.id,
                "vector_store_name": vector_store_name,
                "vector_store_exists": True,
            }
    except Exception as e:
        logger.error(f"Error while creating vector store: {str(e)}")
        return {
            "vector_store_id": None,
            "vector_store_name": vector_store_name,
            "vector_store_exists": False,
        }

def attach_file_to_vector_store(
    vector_store_id: str,
    file_id: str,
    file_name: str,
    # chat_id: str,
):
    try:
        metadata = {
            # "chat_id": chat_id,
            "file_id": file_id,
            "file_name": file_name,
            "vector_store_id": vector_store_id,
        }

        # Attach single file
        file_obj = SYNC_OPENAI_CLIENT.vector_stores.files.create(
            vector_store_id=vector_store_id,
            file_id=file_id,
            attributes=metadata,
            timeout=300,
        )

        current_time = time.time()
        # Poll ingestion status
        while True:
            status = SYNC_OPENAI_CLIENT.vector_stores.files.retrieve(
                vector_store_id=vector_store_id,
                file_id=file_obj.id,
            ).status

            logger.info(f"Vector store ingestion status: {status}")

            if status == "completed":
                logger.info("Vector store ingestion completed successfully")
                break
            elif status == "failed":
                logger.error("Vector store ingestion failed")
                return False
            else:
                time.sleep(2)
            if time.time() - current_time > 600:
                logger.error("Vector store ingestion timed out")
                return False

        logger.info(f"File attached to vector store successfully: {file_id}")
        return True

    except Exception as e:
        logger.error(f"Error while attaching file to vector store: {str(e)}")
        return False

# def create_file_session_for_user_uploaded_file(file_path: str, chat_id: str, vector_store_name: str) -> str:
#     """
#     Upload file to OpenAI and return the file ID and vector store ID.
#     called once per session, not per question.
#     Args:
#         file_path: The path to the file
#         chat_id: The ID of the chat
#     Returns:
#         The file ID of the uploaded file and vector store ID.
#     Raises:
#         Exception: If the file upload fails
#     """
#     max_retries = 3
#     retry_delay = 2

#     for attempt in range(max_retries):
#         try:
#             processed_file_path = process_different_file_formats(file_path)
#             logger.info(f"Converted file to PDF: {processed_file_path}")
#             file = upload_file_to_openai(processed_file_path)
#             logger.info(f"Creating vector store.")
#             result = create_user_vector_store(vector_store_name)
#             attach_file_to_vector_store(result["vector_store_id"], file.id, file.filename, chat_id)
#             logger.info(f"File attached to vector store successfully: {file.filename}")
#             return {"file_id": str(file.id),        
#             "vector_store_id": result["vector_store_id"], 
#             "vector_store_name": vector_store_name,
#             }
#         except Exception as e:
#             if attempt == max_retries - 1:
#                 logger.error(f"Error uploading file after {max_retries} attempts: {str(e)}")
#                 raise Exception(f"Error uploading file: {str(e)}")
#             logger.warning(f"Attempt {attempt + 1} failed: {str(e)}. Retrying in {retry_delay} seconds...")
#             time.sleep(retry_delay)

#         # finally:
#         #     if os.path.exists(processed_file_path):
#         #         os.remove(processed_file_path)

def create_file_session_for_user_uploaded_files(
    s3_file_paths: List[str],
    # chat_id: str,
    vector_store_name: str,
    max_workers: int = 4
) -> Dict[str, any]:
    """
    Upload multiple user files to OpenAI and attach them to a single vector store.
    Called once per session, not per question.

    Args:
        s3_file_paths: List of S3 file paths of the files uploaded by the user
        # chat_id: The ID of the chat
        vector_store_name: Name of the vector store
        max_workers: Parallel workers for file processing

    Returns:
        Dict containing file IDs and vector store details

    Raises:
        Exception if processing or upload fails
    """

    max_retries = 3
    retry_delay = 2

    file_paths = [download_file_from_s3_to_local(path, os.path.basename(path.replace('s3://', '').split('/', 1)[1])) for path in s3_file_paths]
    logger.info(f"Files downloaded from S3 successfully: {file_paths}")

    # ---- STEP 1: Process files in parallel ----
    try:
        logger.info("Processing files in parallel...")

        processed_file_paths = []

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_path = {
                executor.submit(process_different_file_formats, path): path
                for path in file_paths
            }

            for future in as_completed(future_to_path):
                original_path = future_to_path[future]
                try:
                    processed_path = future.result()
                    if processed_path is None:
                        logger.error(f"File conversion returned None for {original_path}")
                        raise Exception(f"File conversion failed for {original_path}")
                    processed_file_paths.append(processed_path)
                    logger.info(f"Processed file: {original_path} → {processed_path}")
                except Exception as e:
                    logger.error(f"Failed to process file {original_path}: {e}")
                    raise Exception(f"File processing failed for {original_path}")

    except Exception:
        raise

    # ---- STEP 2: Create vector store (once per session) ----
    logger.info("Creating vector store...")
    vector_store = create_user_vector_store(vector_store_name)
    vector_store_id = vector_store["vector_store_id"]

    uploaded_files = []

    # ---- STEP 3: Upload & attach each file (retry-safe) ----
    for processed_path in processed_file_paths:
        try:
            for attempt in range(max_retries):
                try:
                    file = upload_file_to_openai(processed_path)
                    attach_file_to_vector_store(
                        vector_store_id,
                        file.id,
                        file.filename,
                        # chat_id
                    )
                    logger.info(f"File attached to vector store: {file.filename}")
                    uploaded_files.append(str(file.id))
                    break

                except Exception as e:
                    if attempt == max_retries - 1:
                        logger.error(
                            f"Error uploading file {processed_path} "
                            f"after {max_retries} attempts: {e}"
                        )
                        raise Exception(f"Error uploading file {processed_path}: {e}")

                    logger.warning(
                        f"Upload attempt {attempt + 1} failed for {processed_path}: {e}. "
                        f"Retrying in {retry_delay}s..."
                    )
                    time.sleep(retry_delay)
        finally:
            # Clean up temporary files after all retries (success or failure)
            if processed_path and os.path.exists(processed_path):
                os.remove(processed_path)
                logger.info(f"Cleaned up processed file: {processed_path}")
            if file_paths and any(os.path.exists(file_path) for file_path in file_paths):
                for file_path in file_paths:
                    os.remove(file_path)
                    logger.info(f"Cleaned up file path: {file_path}")

    # ---- STEP 4: Return session metadata ----
    return {
        "file_ids": uploaded_files,
        "vector_store_id": vector_store_id,
        "vector_store_name": vector_store_name,
        "total_files": len(uploaded_files)
    }

def create_file_session_openai_for_ask_caspr(markdown_text: Optional[str] = None, file_s3_path: Optional[str] = None) -> str:
    """
    Upload markdown text or S3 file as PDF to OpenAI and return the file ID.
    Called once per session, not per question.
    Args:
        markdown_text: The markdown content to convert to PDF and upload
        file_s3_path: The S3 path (s3://bucket/key format) of the file to download, convert to PDF, and upload
    Returns:
        The file ID of the uploaded file
    Raises:
        Exception: If the file upload fails
    """
    logger.info(f"[CREATE_FILE_OPENAI] Starting file session creation | has_markdown={markdown_text is not None} | has_s3_path={file_s3_path is not None}")
    max_retries = 3
    retry_delay = 2
    temp_file_path = None
    processed_file_path = None

    try:
        if markdown_text:
            logger.info(f"[CREATE_FILE_OPENAI] Creating temp file from markdown | text_length={len(markdown_text)}")
            temp_file_path = f"temp_{uuid7()}.md"    
            with open(temp_file_path, "w", encoding="utf-8") as f:
                f.write(markdown_text)
            logger.info(f"[CREATE_FILE_OPENAI] Temp markdown file created | path={temp_file_path}")
            
        elif file_s3_path:
            logger.info(f"[CREATE_FILE_OPENAI] Downloading file from S3 | s3_path={file_s3_path}")
            
            # Extract file extension from S3 path
            s3_path_cleaned = file_s3_path.replace("s3://", "")
            file_extension = os.path.splitext(s3_path_cleaned)[1]

            temp_file_path = os.path.basename(file_s3_path.replace('s3://', '').split('/', 1)[1])
            download_file_from_s3_to_local(file_s3_path, temp_file_path)
            logger.info(f"[CREATE_FILE_OPENAI] File downloaded from S3 | temp_path={temp_file_path} | extension={file_extension}")
            
        else:
            logger.error(f"[CREATE_FILE_OPENAI] Neither markdown_text nor file_s3_path provided")
            raise ValueError("Either markdown_text or file_s3_path must be provided")
        
        # Convert file to PDF (if not already PDF)
        logger.info(f"[CREATE_FILE_OPENAI] Converting file to PDF | source={temp_file_path}")
        processed_file_path = process_different_file_formats(temp_file_path)
        logger.info(f"[CREATE_FILE_OPENAI] File converted to PDF | pdf_path={processed_file_path}")
        if not processed_file_path:
            raise Exception(f"File conversion failed for {temp_file_path}")
        
        # Upload to OpenAI with retries
        for attempt in range(max_retries):
            try:
                logger.info(f"[CREATE_FILE_OPENAI] Upload attempt {attempt + 1}/{max_retries} | file={processed_file_path}")
                file = upload_file_to_openai(processed_file_path)
                logger.info(f"[CREATE_FILE_OPENAI] File uploaded successfully | file_id={file.id} | attempt={attempt + 1}")
                return str(file.id)
            except Exception as e:
                logger.error(f"[CREATE_FILE_OPENAI] Upload attempt {attempt + 1} failed | error={str(e)}")
                if attempt == max_retries - 1:
                    logger.error(f"[CREATE_FILE_OPENAI] All upload attempts failed | max_retries={max_retries}")
                    raise Exception(f"Error uploading file: {str(e)}")
                logger.info(f"[CREATE_FILE_OPENAI] Retrying in {retry_delay}s")
                time.sleep(retry_delay)
                
    finally:
        # Clean up temporary files
        if temp_file_path and os.path.exists(temp_file_path):
            try:
                os.remove(temp_file_path)
                logger.info(f"[CREATE_FILE_OPENAI] Cleaned up temp file | path={temp_file_path}")
            except Exception as e:
                logger.warning(f"[CREATE_FILE_OPENAI] Failed to remove temp file | path={temp_file_path} | error={str(e)}")
        
        if processed_file_path and os.path.exists(processed_file_path) and processed_file_path != temp_file_path:
            try:
                os.remove(processed_file_path)
                logger.info(f"[CREATE_FILE_OPENAI] Cleaned up processed file | path={processed_file_path}")
            except Exception as e:
                logger.warning(f"[CREATE_FILE_OPENAI] Failed to remove processed file | path={processed_file_path} | error={str(e)}")

# temporary function to delete all files
def delete_all_files_openai()->bool:
    """
    Delete all files associated with the session.
    Args:
        None
    Returns:
        True if the files are deleted, False otherwise
    Raises:
        Exception: If the files deletion fails
    Retries:
        3 times
    """
    max_retries = 3
    retry_delay = 2
    try:
        for attempt in range(max_retries):
            try:
                files = SYNC_OPENAI_CLIENT.files.list()
                for f in files.data:
                    SYNC_OPENAI_CLIENT.files.delete(f.id)
                    logger.info(f"File deleted successfully with ID: {f.id}")
            except Exception as e:
                logger.error(f"error in deleting files: {e}")
                logger.info(f"Attempt {attempt + 1} failed: {str(e)}. Retrying in {retry_delay} seconds...")
                time.sleep(retry_delay)
                if attempt == max_retries - 1:
                    logger.error(f"Error deleting files after {max_retries} attempts: {str(e)}")
                    return False
                return True
    except Exception as e:
        logger.error(f"error in deleting files: {e}")
        return False

def check_if_file_exists_openai(file_id: str) -> bool:
    """
    Check if the file exists in the OpenAI API.
    Args:
        file_id: The ID of the file to check
    Returns:
        True if the file exists, False otherwise
    """
    try:
        logger.info(f"[CHECK_FILE_OPENAI] Checking file existence | file_id={file_id}")
        file_obj = SYNC_OPENAI_CLIENT.files.retrieve(file_id=file_id)
        logger.info(f"[CHECK_FILE_OPENAI] File exists | file_id={file_id} | status={getattr(file_obj, 'status', 'N/A')}")
        return True
    except Exception as e:
        logger.warning(f"[CHECK_FILE_OPENAI] File does not exist | file_id={file_id} | error={str(e)}")
        return False

async def ensure_report_file_id(
    report_id: str,
    file_id: Optional[str],
    file_s3_path: Optional[str],
    log_prefix: str = "ENSURE_REPORT_FILE",
) -> Optional[str]:
    """
    Resolve a usable OpenAI file_id for a report's initial markdown, creating it when needed.

    Verifies an existing file_id against OpenAI and re-uploads from S3 when it is missing or
    stale (cleanup_openai_files.py nulls file_id while keeping initial_markdown, so re-creation
    on demand is expected). Persists any newly created file_id on the report.

    Args:
        report_id: The report whose file_id is being resolved
        file_id: The currently stored file_id, if any
        file_s3_path: S3 path of the report's initial markdown (s3://bucket/key)
        log_prefix: Caller tag used in log lines

    Returns:
        A usable file_id, or None when one could not be obtained. Callers degrade rather than
        fail, so this never raises.
    """
    from src.db.async_db_functions import update_report
    from src.db.db_utils import async_session_scope

    if file_id:
        try:
            if await asyncio.to_thread(check_if_file_exists_openai, file_id):
                logger.info(f"[{log_prefix}] file_id verified in OpenAI | report_id={report_id} | file_id={file_id}")
                return file_id
            logger.warning(
                f"[{log_prefix}] file_id present in DB but missing in OpenAI, re-uploading | "
                f"report_id={report_id} | file_id={file_id}"
            )
        except Exception as check_error:
            # Verification itself failed (network/API blip). Keep the existing id rather than
            # discarding a probably-valid file.
            logger.error(
                f"[{log_prefix}] Could not verify file_id, continuing with existing id | "
                f"report_id={report_id} | file_id={file_id} | error={str(check_error)}",
                exc_info=True,
            )
            return file_id

    if not file_s3_path:
        logger.error(
            f"[{log_prefix}] No initial markdown S3 path available, cannot create file_id | "
            f"report_id={report_id}"
        )
        return None

    try:
        logger.info(f"[{log_prefix}] Uploading initial markdown to OpenAI | report_id={report_id} | s3_path={file_s3_path}")
        new_file_id = await asyncio.to_thread(
            create_file_session_openai_for_ask_caspr,
            file_s3_path=file_s3_path,
        )
        logger.info(f"[{log_prefix}] Upload complete | report_id={report_id} | file_id={new_file_id}")
    except Exception as upload_error:
        logger.error(
            f"[{log_prefix}] Failed to upload initial markdown to OpenAI | "
            f"report_id={report_id} | s3_path={file_s3_path} | error={str(upload_error)}",
            exc_info=True,
        )
        return None

    try:
        async with async_session_scope() as session:
            await update_report(
                report_id=report_id,
                update_data={"file_id": new_file_id},
                session=session,
            )
        logger.info(f"[{log_prefix}] Persisted file_id on report | report_id={report_id} | file_id={new_file_id}")
    except Exception as db_error:
        # The file exists in OpenAI, so the caller can still use it for this request even though
        # the next request will have to re-upload.
        logger.error(
            f"[{log_prefix}] Failed to persist file_id | report_id={report_id} | "
            f"file_id={new_file_id} | error={str(db_error)}",
            exc_info=True,
        )

    return new_file_id

def check_if_file_id_is_attached_to_vector_store(file_id: str, vector_store_id: str) -> bool:
    """
    Check if the file is attached to the vector store.
    Args:
        file_id: The ID of the file to check
        vector_store_id: The ID of the vector store to check
    Returns:
        True if the file is attached to the vector store, False otherwise
    """
    try:
        logger.info(f"Checking if file_id {file_id} is attached to vector store {vector_store_id}")
        SYNC_OPENAI_CLIENT.vector_stores.files.retrieve(
        vector_store_id=vector_store_id,
        file_id=file_id
    )
        logger.info(f"File {file_id} is attached to vector store {vector_store_id}")
        return True
    except Exception as e:
        logger.error(f"error in checking if file is attached to vector store: {e}")
        logger.info(f"File {file_id} is not attached to vector store {vector_store_id}")
        return False

def delete_vector_store_openai(vector_store_id: str)->bool:
    """
    Delete a vector store associated with the session.
    Args:
        vector_store_id: The ID of the vector store to delete
    Returns:
        True if the vector store is deleted, False otherwise
    """
    try:
        logger.info(f"Deleting vector store with ID: {vector_store_id}")
        SYNC_OPENAI_CLIENT.vector_stores.delete(vector_store_id)
        logger.info(f"Vector store deleted successfully with ID: {vector_store_id}")
        return True
    except Exception as e:
        logger.error(f"error in deleting vector store: {e}")
        logger.info(f"Vector store with ID: {vector_store_id} does not exist")
        return False

def delete_all_vector_stores_openai()->bool: 
    """
    Delete all vector stores associated with the session.
    Args:
        None
    Returns:
        True if the vector stores are deleted, False otherwise
    """
    try:
        vector_stores = SYNC_OPENAI_CLIENT.vector_stores.list()
        for vector_store in vector_stores.data:
            delete_vector_store_openai(vector_store.id)
            logger.info(f"Vector store deleted successfully with ID: {vector_store.id}")
        return True
    except Exception as e:
        logger.error(f"error in deleting vector stores: {e}")
        return False

def delete_current_file_openai(file_id: str) -> bool:
    """
    Delete the current file.
    Args:
        file_id: The ID of the file to delete
    Returns:
        True if the file is deleted, False otherwise
    Raises:
        Exception: If the file deletion fails
    Retries:
        3 times
    """
    max_retries = 3
    retry_delay = 2
    try:
        for attempt in range(max_retries):
            try:
                SYNC_OPENAI_CLIENT.files.delete(file_id)
                return True
            except Exception as e:
                logger.error(f"error in deleting file: {e}")
                logger.info(f"Attempt {attempt + 1} failed: {str(e)}. Retrying in {retry_delay} seconds...")
                time.sleep(retry_delay)
                if attempt == max_retries - 1:
                    logger.error(f"Error deleting file after {max_retries} attempts: {str(e)}")
                    return False
    except Exception as e:
        logger.error(f"error in deleting file: {e}")
        return False
    
def check_if_file_id_exists_openai(file_id: str) -> bool:
    """
    Check if the file exists in the OpenAI API.
    Args:
        file_id: The ID of the file to check
    Returns:
        True if the file exists, False otherwise
    """
    try:
        logger.info(f"Checking if file_id {file_id} exists on openai: {file_id}")
        SYNC_OPENAI_CLIENT.files.retrieve(file_id=file_id)
        logger.info(f"File {file_id} exists on openai")
        return True
    except Exception as e:
        logger.error(f"error in checking if file exists: {e}")
        logger.info(f"File {file_id} does not exist on openai")
        return False
    
def check_if_vector_store_id_exists_openai(vector_store_id: str) -> bool:
    """
    Check if the vector store exists in the OpenAI API.
    Args:
        vector_store_id: The ID of the vector store to check
    Returns:
        True if the vector store exists, False otherwise
    """
    try:
        logger.info(f"Checking if vector_store_id {vector_store_id} exists on openai: {vector_store_id}")
        SYNC_OPENAI_CLIENT.vector_stores.retrieve(vector_store_id=vector_store_id)
        logger.info(f"Vector store {vector_store_id} exists on openai")
        return True
    except Exception as e:
        logger.error(f"error in checking if vector store exists: {e}")
        logger.info(f"Vector store {vector_store_id} does not exist on openai")
        return False
    