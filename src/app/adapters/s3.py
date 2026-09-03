"""s3_utils.py - This file contains the functions for the S3 bucket operations"""
import os 
import re
from urllib.parse import quote
from datetime import datetime, timezone
from dotenv import load_dotenv
import boto3
from botocore.exceptions import ClientError
from app.core.logging import setup_logging
from dataclasses import dataclass
from botocore.client import BaseClient
import time
from functools import wraps, lru_cache

from app.core.constants import (
    LOCAL_S3,
    S3_BUCKET_NAME,
    S3_ACCESS_KEY_ID,
    S3_SECRET_ACCESS_KEY,
    S3_REGION_NAME,
    S3_REPORTS_BASE_PATH,
)

s3_logger = setup_logging(__file__)


def retry_with_backoff(retries=3, backoff_in_seconds=1):
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            x = 0
            while True:
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    if x == retries:
                        raise e
                    else:
                        x += 1
                        sleep = (backoff_in_seconds * 2 ** x)
                        s3_logger.warning(
                            f"Retrying {func.__name__} - attempt {x} of {retries} after {sleep} seconds. Error: {str(e)}"
                        )
                        time.sleep(sleep)
        return wrapper
    return decorator

@dataclass
class S3Controls():
    bucket_name: str
    aws_key_id: str
    aws_secret_key: str
    aws_region: str
    s3_client: BaseClient = None
    
    def __post_init__(self):
        self.s3_client = boto3.client(
            aws_access_key_id = self.aws_key_id,
            aws_secret_access_key = self.aws_secret_key,
            region_name = self.aws_region,
            service_name="s3",
            endpoint_url = LOCAL_S3
        )
        
        
    def check_if_key_exists(self, key: str):
        try:
            self.s3_client.get_object(Key = key, Bucket = self.bucket_name)
            return True
            
        except ClientError as err:
            if err.response['Error']['Code'] == 'NoSuchKey':
                return False
            
            s3_logger.error('Error while checking whether key exists in S3 Bucket with key: %s', exc_info = True)
            raise Exception(f"Error while checking whether key exists, reason: {err.response['Error']['Message']}")
        
        
    def generate_presigned_url_fast(self, s3_key: str, timeout: int = 3600) -> str:
        """Generate a presigned URL *without* checking key existence first.

        Use for visualization images written by our own pipeline where
        existence is virtually guaranteed.  Falls back gracefully: the
        URL itself will 404 on the consumer side if the object is gone.
        """
        try:
            return self.s3_client.generate_presigned_url(
                "get_object",
                Params={"Bucket": self.bucket_name, "Key": s3_key},
                ExpiresIn=timeout,
            )
        except Exception:
            s3_logger.error("Presigned URL generation failed for s3_key=%s", s3_key, exc_info=True)
            raise

    def get_download_presigned_url(self, s3_key, timeout=3600):
        """
        Get the presigned URL to download the file
        :param s3_key: str
        :param timeout: int
        :return: str
        """
        try:
            if not self.check_if_key_exists(key = s3_key):
                raise Exception("No resource found with given key.")

            url = self.s3_client.generate_presigned_url(
                "get_object",
                Params={"Bucket": self.bucket_name, "Key": s3_key},
                ExpiresIn=timeout,
            )
            return url
        
        except:  
            s3_logger.error('Link creation failed for s3_key = %s', exc_info=True)
            raise Exception("Link creation failed please try again.")

    def get_download_presigned_url_with_filename(self, s3_key: str, filename: str, timeout: int = 3600) -> str:
        """
        Get the presigned URL to download the file with a custom filename
        :param s3_key: str - S3 key of the file
        :param filename: str - Custom filename for download
        :param timeout: int - URL expiration time in seconds
        :return: str - Presigned URL
        """
        try:
            if not self.check_if_key_exists(key=s3_key):
                raise Exception("No resource found with given key.")

            url = self.s3_client.generate_presigned_url(
                "get_object",
                Params={
                    "Bucket": self.bucket_name,
                    "Key": s3_key,
                    "ResponseContentDisposition": f"attachment; filename*=UTF-8''{quote(filename)}"
                },
                ExpiresIn=timeout,
            )
            return url
        
        except Exception as e:
            s3_logger.error('Link creation failed for s3_key = %s, filename = %s: %s', s3_key, filename, str(e), exc_info=True)
            raise Exception("Link creation failed please try again.")
    
    @retry_with_backoff(retries=3, backoff_in_seconds=1)
    def _upload_file_with_retry(self, local_file_path: str, s3_key: str) -> None:
        """Internal method to handle the actual S3 upload with retry mechanism"""
        self.s3_client.upload_file(
            Filename=local_file_path,
            Bucket=self.bucket_name,
            Key=s3_key
        )

    def upload_file(self, local_file_path: str, s3_key: str = None) -> str:
        """
        Upload a file to S3 bucket with retry mechanism
        # :param local_file_path: str - Path to the local file
        # :param s3_key: str - Optional custom S3 key/path. If not provided, uses filename
        # :return: str - The S3 path of the uploaded file
        # """
        try:
            if not os.path.exists(local_file_path):
                raise FileNotFoundError(f"Local file not found: {local_file_path}")

            if s3_key is None:
                s3_key = os.path.basename(local_file_path)

            self._upload_file_with_retry(local_file_path, s3_key)

            s3_path = f"s3://{self.bucket_name}/{s3_key}"
            s3_logger.info(f"Successfully uploaded file to {s3_path}")
            return s3_path

        except Exception as e:
            s3_logger.error(f"Failed to upload file {local_file_path} after all retries", exc_info=True)
            raise Exception(f"Upload failed after all retries: {str(e)}")

    @retry_with_backoff(retries=3, backoff_in_seconds=1)
    def _upload_content_with_retry(self, content: bytes, s3_key: str, content_type: str = "text/markdown") -> None:
        """Internal method to upload bytes/content directly to S3 with retry."""
        self.s3_client.put_object(
            Bucket=self.bucket_name,
            Key=s3_key,
            Body=content,
            ContentType=content_type
        )

    def upload_content(self, content: str, s3_key: str, content_type: str = "text/markdown") -> str:
        """
        Upload string content directly to S3 without writing a local file.
        
        :param content: str - The string content to upload
        :param s3_key: str - S3 key/path for the uploaded content
        :param content_type: str - MIME type (default: text/markdown)
        :return: str - The full S3 path (s3://bucket/key)
        """
        try:
            content_bytes = content.encode("utf-8")
            self._upload_content_with_retry(content_bytes, s3_key, content_type)
            s3_path = f"s3://{self.bucket_name}/{s3_key}"
            s3_logger.info(f"Successfully uploaded content to {s3_path}")
            return s3_path
        except Exception as e:
            s3_logger.error(f"Failed to upload content to {s3_key} after all retries", exc_info=True)
            raise Exception(f"Content upload failed after all retries: {str(e)}")

    @retry_with_backoff(retries=3, backoff_in_seconds=1)
    def _download_file_with_retry(self, s3_key: str) -> bytes:
        """Internal method to handle the actual S3 download with retry mechanism"""
        response = self.s3_client.get_object(Bucket=self.bucket_name, Key=s3_key)
        return response['Body'].read()

    def download_file(self, s3_key: str) -> bytes:
        """
        Download a file from S3 bucket and return its content as bytes
        :param s3_key: str - S3 key/path of the file to download
        :return: bytes - The file content as bytes
        """
        try:
            if not self.check_if_key_exists(key=s3_key):
                raise FileNotFoundError(f"File not found in S3: {s3_key}")

            file_content = self._download_file_with_retry(s3_key)
            
            s3_path = f"s3://{self.bucket_name}/{s3_key}"
            s3_logger.info(f"Successfully downloaded file from {s3_path}")
            return file_content

        except Exception as e:
            s3_logger.error(f"Failed to download file {s3_key} after all retries", exc_info=True)
            raise Exception(f"Download failed after all retries: {str(e)}")

PRESIGNED_URL_EXPIRY = 86400       # 24 hours
PRESIGNED_URL_CACHE_TTL = 82800    # 23 hours (slightly less than URL expiry)


def extract_s3_key(s3_uri: str) -> str | None:
    """Extract the object key from an S3 URI (s3://bucket/path/to/file) or return None."""
    if not s3_uri or not isinstance(s3_uri, str):
        return None
    if s3_uri.startswith("s3://"):
        parts = s3_uri.replace("s3://", "").split("/", 1)
        return parts[1] if len(parts) > 1 else None
    if S3_REPORTS_BASE_PATH and S3_REPORTS_BASE_PATH in s3_uri:
        return s3_uri[s3_uri.find(S3_REPORTS_BASE_PATH):]
    return None


async def get_or_create_presigned_url(
    s3_uri: str,
    redis_client,
    s3_instance: "S3Controls | None" = None,
) -> str | None:
    """Return a cached presigned URL for the S3 URI, generating one on cache miss.

    Returns None if the URI is empty or unparseable.
    """
    s3_key = extract_s3_key(s3_uri)
    if not s3_key:
        return None

    cache_key = f"presigned_url:{s3_key}"
    try:
        cached = await redis_client.get(cache_key)
        if cached:
            return cached.decode() if isinstance(cached, bytes) else cached
    except Exception as exc:
        s3_logger.warning(f"Redis GET failed for {cache_key}: {exc}")

    if s3_instance is None:
        s3_instance = get_s3_instance()

    from fastapi.concurrency import run_in_threadpool
    presigned_url = await run_in_threadpool(
        s3_instance.generate_presigned_url_fast,
        s3_key=s3_key,
        timeout=PRESIGNED_URL_EXPIRY,
    )

    try:
        await redis_client.setex(cache_key, PRESIGNED_URL_CACHE_TTL, presigned_url)
    except Exception as exc:
        s3_logger.warning(f"Redis SETEX failed for {cache_key}: {exc}")

    return presigned_url


async def replace_visualization_uris_in_reports(reports: list[dict], redis_client) -> list[dict]:
    """Walk through every card/subsection in *reports* and swap S3 visualization URIs for presigned URLs."""
    if not reports:
        return reports

    s3_instance = get_s3_instance()
    tasks: list[tuple] = []

    for report in reports:
        for card in report.get("cards", []):
            for table in card.get("tables", []):
                viz = table.get("visualization", "")
                if viz and isinstance(viz, str) and viz.startswith("s3://"):
                    tasks.append((table, "visualization", viz))

            for sub in card.get("sub_sections") or []:
                for table in sub.get("tables", []):
                    viz = table.get("visualization", "")
                    if viz and isinstance(viz, str) and viz.startswith("s3://"):
                        tasks.append((table, "visualization", viz))

    if not tasks:
        return reports

    import asyncio
    urls = await asyncio.gather(
        *(get_or_create_presigned_url(uri, redis_client, s3_instance) for _, _, uri in tasks)
    )
    for (obj, key, _original_uri), url in zip(tasks, urls):
        if url:
            obj[key] = url

    return reports


async def replace_visualization_uris_in_card(card: dict, redis_client) -> dict:
    """Replace S3 visualization URIs in a single card dict.

    Handles both formats:
    - Chat API format:   {"tables": [...], "sub_sections": [...]}
    - Refine/revert format: {"section": [{"tables": [...]}], "sub_sections": [...]}
    """
    if not card:
        return card

    s3_instance = get_s3_instance()
    tasks: list[tuple] = []

    def _collect(tables_list: list | None):
        for table in (tables_list or []):
            viz = table.get("visualization", "")
            if viz and isinstance(viz, str) and viz.startswith("s3://"):
                tasks.append((table, "visualization", viz))

    _collect(card.get("tables"))

    for section in card.get("section") or []:
        if isinstance(section, dict):
            _collect(section.get("tables"))

    for sub in card.get("sub_sections") or []:
        _collect(sub.get("tables"))

    if not tasks:
        return card

    import asyncio
    urls = await asyncio.gather(
        *(get_or_create_presigned_url(uri, redis_client, s3_instance) for _, _, uri in tasks)
    )
    for (obj, key, _), url in zip(tasks, urls):
        if url:
            obj[key] = url

    return card


@lru_cache(maxsize=1)
def get_s3_instance(aws_key_id = S3_ACCESS_KEY_ID, bucket_name = S3_BUCKET_NAME, aws_secret_key = S3_SECRET_ACCESS_KEY, aws_region = S3_REGION_NAME) -> S3Controls:
    # s3_logger.info(f"aws_key_id: {aws_key_id}")
    # s3_logger.info(f"bucket_name: {bucket_name}")
    # s3_logger.info(f"aws_secret_key: {aws_secret_key}")
    # s3_logger.info(f"aws_region: {aws_region}")
    S3Instance = S3Controls(
        aws_key_id = aws_key_id,
        bucket_name = bucket_name,
        aws_secret_key = aws_secret_key,
        aws_region = aws_region
    )
    
    return S3Instance

S3_boto3_client = boto3.client('s3',
    aws_access_key_id=S3_ACCESS_KEY_ID,
    aws_secret_access_key=S3_SECRET_ACCESS_KEY,
    region_name=S3_REGION_NAME
)


def _slugify_for_s3(value: str, max_length: int = 80) -> str:
    """Return a safe, concise slug for S3 keys from titles/names.
    - Lowercase, replace spaces with underscores
    - Remove non-alphanumeric/_/-
    - Collapse repeats and trim length
    """
    if not value:
        return "untitled"
    value = value.strip().lower().replace(" ", "_")
    value = re.sub(r"[^a-z0-9_-]+", "_", value)
    value = re.sub(r"_+", "_", value)
    return value[:max_length].strip("_") or "untitled"


def download_file_from_s3_to_local(s3_path: list[str], local_file_path: str) -> str:
    """
    Download a file from S3 to local file path.
    
    Args:
        s3_path: S3 path in format s3://bucket/key
        local_file_path: Local file path where file will be saved
    
    Returns:
        str: The local file path where the file was saved
    
    Raises:
        ValueError: If S3 path format is invalid
        Exception: If download fails
    """
    try:
        if not s3_path.startswith("s3://"):
            raise ValueError("S3 path must start with s3://")
        
        # Parse bucket and key from S3 path
        s3_path_cleaned = s3_path.replace("s3://", "")
        bucket_name, object_key = s3_path_cleaned.split("/", 1)
        
        s3_logger.info(f"Downloading file from S3: bucket={bucket_name}, key={object_key}")
        
        # Download file from S3
        response = S3_boto3_client.get_object(Bucket=bucket_name, Key=object_key)
        file_bytes = response['Body'].read()
        
        # Write to local file
        with open(local_file_path, 'wb') as f:
            f.write(file_bytes)
        
        s3_logger.info(f"Successfully downloaded file from S3 to: {local_file_path}")
        return local_file_path
        
    except Exception as e:
        s3_logger.error(f"Failed to download file from S3: {str(e)}")
        raise Exception(f"Error downloading file from S3: {str(e)}")


def build_report_s3_prefix(*, user_id: str, user_name: str, chat_id: str, chat_title: str, report_id: str, version: int = 1, when: datetime | None = None) -> str:
    """Build base S3 folder for a given report instance with version support:
    {BASE}/YYYY/MM/DD/{user_id}_{user_name}/{chat_id}_{chat_title}/report_{report_id}/v{version}
    
    Args:
        user_id: User ID
        user_name: User name (will be slugified)
        chat_id: Chat ID
        chat_title: Chat title (will be slugified)
        report_id: Report ID
        version: Report version number (default: 1)
        when: Timestamp for date folder (default: current UTC time)
    
    Returns:
        S3 prefix path including version folder
    """
    when = when or datetime.now(timezone.utc)
    chat_id = (chat_id or "").strip()
    title_slug = _slugify_for_s3(chat_title)
    user_slug = _slugify_for_s3(user_name)
    date_path = when.strftime("%Y/%m/%d")
    return f"{S3_REPORTS_BASE_PATH}/{date_path}/{user_id}_{user_slug}/{chat_id}_{title_slug}/report_{report_id}/v{version}"


def upload_initial_markdown_to_s3(
    *,
    md_content: str,
    user_id: str,
    user_name: str,
    chat_id: str,
    chat_title: str,
    report_id: str,
    version: int = 0,
    when: datetime | None = None
) -> str:
    """
    Upload the initial markdown content to S3 for Ask Caspr.
    
    Path: {S3_REPORTS_BASE_PATH}/{date_path}/{user_id}_{user_slug}/{chat_id}_{title_slug}/report_{report_id}/{title_slug_30}_initial_v{version}.md
    
    Args:
        md_content: The markdown string to upload
        user_id: User ID
        user_name: User name
        chat_id: Chat ID
        chat_title: Chat title (first 30 chars used in filename)
        report_id: Report ID
        version: Version number for the filename suffix (default: 0)
        when: Timestamp for date folder (default: current UTC time)
    
    Returns:
        Full S3 path (s3://bucket/key) of the uploaded file
    """
    when = when or datetime.now(timezone.utc)
    chat_id = (chat_id or "").strip()
    title_slug = _slugify_for_s3(chat_title)
    user_slug = _slugify_for_s3(user_name)
    date_path = when.strftime("%Y/%m/%d")

    # Build filename: first 30 chars of title slug + _initial_v{version}.md
    filename_slug = _slugify_for_s3(chat_title, max_length=30)
    filename = f"{filename_slug}_initial_v{version}.md"

    s3_key = (
        f"{S3_REPORTS_BASE_PATH}/{date_path}/{user_id}_{user_slug}/"
        f"{chat_id}_{title_slug}/report_{report_id}/{filename}"
    )

    s3_instance = get_s3_instance()
    s3_path = s3_instance.upload_content(content=md_content, s3_key=s3_key)
    s3_logger.info(f"Uploaded initial markdown v{version} for report {report_id} to {s3_path}")
    return s3_path
