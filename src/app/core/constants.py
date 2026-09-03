"""constants.py: Constants for the Casper backend"""

import os
from datetime import timedelta
from pathlib import Path

import boto3
from dotenv import load_dotenv
from langchain_anthropic import ChatAnthropic

# from langchain_aws import ChatBedrock  # Bedrock disabled — using direct Anthropic API instead
# from langchain_groq import ChatGroq
from langchain_openai import ChatOpenAI
from openai import AsyncOpenAI, OpenAI
from pydantic import BaseModel, Field

from app.core.logging import setup_logging

# from langchain_anthropic import ChatAnthropic

logger = setup_logging(__name__)

if load_dotenv():
    logger.info("Loaded environment variables from .env file")
else:
    logger.info("No .env file found")

ENVIRONMENT = os.getenv("ENVIRONMENT").upper().strip()

os.environ["TOKENIZERS_PARALLELISM"] = "false"

# Get the root directory of the project (2 levels up from this file)
ROOT_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PPTX_GENERATOR_ROOT = Path(ROOT_DIR) / "pptx_generator"
PPTX_CASPR_TEMPLATE_PATH = PPTX_GENERATOR_ROOT / "masterslides" / "caspr" / "caspr.pptx"
PPTX_ACCENTURE_TEMPLATE_PATH = (
    PPTX_GENERATOR_ROOT / "masterslides" / "Template_Accenture Tech Acquisition Analysis.pptx"
)

# SlideSpeak API Configuration
SLIDESPEAK_API_URL = os.getenv("SLIDESPEAK_API_URL", "https://api.slidespeak.co/api/v1")
SLIDESPEAK_API_KEY = os.getenv("SLIDESPEAK_API_KEY")
SLIDESPEAK_TEMPLATE_ID = os.getenv("SLIDESPEAK_TEMPLATE_ID")
# Redis configuration
REDIS_HOST = os.getenv("REDIS_HOST")
# if not REDIS_HOST:
#     if ENVIRONMENT == "DEV":
#         REDIS_HOST = 'casprbackend.ezlab.in'
#     elif ENVIRONMENT == "PROD":
#         REDIS_HOST = 'caspr.ai'
# REDIS_HOST = 'localhost'
REDIS_PORT = int(os.getenv("REDIS_PORT", 6379))
REDIS_DB = int(os.getenv("REDIS_DB", 0))
# TTL for Redis keys (24 hours in seconds)
REDIS_TTL = int(os.getenv("REDIS_TTL", 86400))  # 24 hours = 86400 seconds
REDIS_PASSWORD = os.getenv("REDIS_PASSWORD")

# Image Generation Models
GEMINI_IMAGE_MODEL = os.getenv("GEMINI_IMAGE_MODEL")
IMAGE_GEN_MODEL = os.getenv("IMAGE_GEN_MODEL")
OPENAI_IMAGE_MODEL = os.getenv("OPENAI_IMAGE_MODEL")

# AWS Bedrock Constants
BEDROCK_ACCESS_KEY_ID = os.getenv("BEDROCK_ACCESS_KEY_ID")
BEDROCK_SECRET_ACCESS_KEY = os.getenv("BEDROCK_SECRET_ACCESS_KEY")
BEDROCK_REGION_NAME = os.getenv("BEDROCK_REGION_NAME")
BEDROCK_SESSION_TOKEN = os.getenv("BEDROCK_SESSION_TOKEN", "")
BEDROCK_LLM_ID = os.getenv("BEDROCK_LLM_ID")
BEDROCK_MODEL_SONNET = os.getenv("BEDROCK_MODEL_SONNET", "")
BEDROCK_MODEL_OPUS = os.getenv("BEDROCK_MODEL_OPUS", "")
BEDROCK_MODEL_HAIKU = os.getenv("BEDROCK_MODEL_HAIKU", "")
# bedrock_client = boto3.client(
#     service_name="bedrock-runtime",
#     aws_access_key_id=BEDROCK_ACCESS_KEY_ID,
#     aws_secret_access_key=BEDROCK_SECRET_ACCESS_KEY,
#     region_name=BEDROCK_REGION_NAME,
#     config=Config(read_timeout=3600)
# )  # Bedrock disabled — using direct Anthropic API instead

# LLM = ChatBedrock(
#     client=bedrock_client,
#     model_id=BEDROCK_LLM_ID,
#     streaming=True,
#     beta_use_converse_api=True,
#     max_tokens=100000
# )

# BEDROCK_REPORT_LLM = ChatBedrock(
#     client=bedrock_client,
#     model_id=BEDROCK_LLM_ID,
#     streaming=False,
#     max_tokens=100000,
#     model_kwargs={"temperature": 0.001},
#     disable_streaming=True
# )

# os.environ["ANTHROPIC_API_KEY"] = os.getenv('ANTHROPIC_API_KEY')
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
ANTHROPIC_MODEL_ID = os.getenv("ANTHROPIC_MODEL_ID")
ANTHROPIC_MODEL_FOR_HTML = os.getenv("ANTHROPIC_API_MODEL")


def get_anthropic_output_config(model_id: str | None) -> dict:
    """Return max_tokens and optional betas for the configured Anthropic model."""
    model = (model_id or "").lower()

    if "haiku" in model:
        return {"max_tokens": 64000}

    # Extended 128k output is supported on select Sonnet/Opus models only.
    if any(tag in model for tag in ("sonnet-4", "opus-4", "3-7-sonnet")):
        return {"max_tokens": 128000, "betas": ["output-128k-2025-02-19"]}

    return {"max_tokens": 64000}


ANTHROPIC_OUTPUT_CONFIG = get_anthropic_output_config(ANTHROPIC_MODEL_ID)
REPORT_LLM = ChatAnthropic(
    model=ANTHROPIC_MODEL_ID,
    temperature=0,
    timeout=None,
    max_retries=5,
    **ANTHROPIC_OUTPUT_CONFIG,
)

# Chat LLM (Anthropic) — used for the interactive chat / report_or_respond flow.
ANTHROPIC_LLM = ChatAnthropic(
    model=ANTHROPIC_MODEL_ID,
    timeout=None,
    streaming=True,
    max_retries=5,
    api_key=ANTHROPIC_API_KEY,
    **ANTHROPIC_OUTPUT_CONFIG,
)

logger.info("LLM initialized")


# HuggingFace Constants
# HUGGINGFACE_EMBEDDINGS_MODEL = os.getenv('HUGGINGFACE_EMBEDDINGS_MODEL')
# EMBEDDING_MODEL = HuggingFaceEmbeddings(
#     model_name=HUGGINGFACE_EMBEDDINGS_MODEL,
#     show_progress=False
# )
# logger.info(f"EMBEDDING_MODEL initialized")

# AWS OpenSearch Constants
# AWS_OPENSEARCH_ACCESS_KEY_ID = os.getenv('AWS_OPENSEARCH_ACCESS_KEY_ID')
# AWS_OPENSEARCH_SECRET_ACCESS_KEY = os.getenv('AWS_OPENSEARCH_SECRET_ACCESS_KEY')
# AWS_OPENSEARCH_REGION_NAME = os.getenv('AWS_OPENSEARCH_REGION_NAME')
# OPENSEARCH_URL = os.getenv('OPENSEARCH_URL')
# OPENSEARCH_INDEX = os.getenv('OPENSEARCH_INDEX')

# service = "es"  # must set the service as 'es'
# credentials = boto3.Session(
#     aws_access_key_id=AWS_OPENSEARCH_ACCESS_KEY_ID,
#     aws_secret_access_key=AWS_OPENSEARCH_SECRET_ACCESS_KEY
# ).get_credentials()

# awsauth = AWS4Auth(
#     AWS_OPENSEARCH_ACCESS_KEY_ID,
#     AWS_OPENSEARCH_SECRET_ACCESS_KEY,
#     AWS_OPENSEARCH_REGION_NAME,
#     service,
#     session_token=credentials.token
# )

# VECTOR_DB = OpenSearchVectorSearch(
#     embedding_function=EMBEDDING_MODEL,
#     opensearch_url=OPENSEARCH_URL,
#     http_auth=awsauth,
#     timeout=300,
#     use_ssl=True,
#     verify_certs=True,
#     connection_class=RequestsHttpConnection,
#     index_name=OPENSEARCH_INDEX,
#     engine="faiss"
# )
logger.info("VECTOR_DB initialized")

# # Tavily Constants
# TAVILY_API_KEY = os.getenv('TAVILY_API_KEY')

# # PAI Constants
# PAI_API_KEY = os.getenv('PAI_API_KEY')


# Secrets Manager Constants
SECRETS_REGION_NAME = os.getenv("SECRETS_REGION_NAME")
SECRETS_NAME = os.getenv("SECRETS_NAME")
SECRETS_ACCESS_KEY_ID = os.getenv("SECRETS_ACCESS_KEY_ID")
SECRETS_SECRET_ACCESS_KEY = os.getenv("SECRETS_SECRET_ACCESS_KEY")
session = boto3.session.Session(
    aws_access_key_id=SECRETS_ACCESS_KEY_ID,
    aws_secret_access_key=SECRETS_SECRET_ACCESS_KEY,
    region_name=SECRETS_REGION_NAME,
)
SECRETS_CLIENT = session.client("secretsmanager")
logger.info("SECRETS_CLIENT initialized")


# S3 Constants
LOCAL_S3 = None if os.getenv("LOCAL_S3") == "None" else os.getenv("LOCAL_S3", None)
S3_BUCKET_NAME = os.getenv("S3_BUCKET_NAME")
S3_ACCESS_KEY_ID = os.getenv("S3_ACCESS_KEY_ID")
S3_SECRET_ACCESS_KEY = os.getenv("S3_SECRET_ACCESS_KEY")
S3_REGION_NAME = os.getenv("S3_REGION_NAME")
S3_REPORTS_BASE_PATH = os.getenv("S3_REPORTS_BASE_PATH")
# logger.info(f"S3_BUCKET_NAME: {S3_BUCKET_NAME}")
# logger.info(f"S3_REGION_NAME: {S3_REGION_NAME}")
# logger.info(f"S3_ACCESS_KEY_ID: {S3_ACCESS_KEY_ID}")
# logger.info(f"S3_SECRET_ACCESS_KEY: {S3_SECRET_ACCESS_KEY}")
# logger.info(f"S3_REPORTS_BASE_PATH: {S3_REPORTS_BASE_PATH}")


# Document search backend: "openai" (vector store + file_search) or "grep" (local grep_agent)
# Default is "grep" — OpenAI vector store approach has been replaced by grep_agent_2.
# Set FILE_SEARCH_MODE=openai in the environment to revert to the OpenAI approach.
FILE_SEARCH_MODE = os.getenv("FILE_SEARCH_MODE", "grep").lower().strip()


def use_grep_file_search() -> bool:
    """True when FILE_SEARCH_MODE=grep (local grep_agent_2 instead of OpenAI file_search)."""
    return FILE_SEARCH_MODE == "grep"


# OpenAI Constants
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_CHAT_MODEL_ID = os.getenv("OPENAI_CHAT_MODEL_ID", "gpt-4o")
OPENAI_LLM_LANGCHAIN = ChatOpenAI(
    api_key=OPENAI_API_KEY, model=OPENAI_CHAT_MODEL_ID, temperature=0.0
)

# # Groq Constants
# GROQ_API_KEY = os.getenv('GROQ_API_KEY')
# GROQ_LLM_ID = os.getenv('GROQ_LLM_ID', 'openai/gpt-oss120b')
# GROQ_LLM = ChatGroq(
#     api_key=GROQ_API_KEY,
#     model=GROQ_LLM_ID,
#     temperature=0.0,
#     streaming=True,
# )
# logger.info(f"GROQ_LLM initialized with model: {GROQ_LLM_ID}")

ANTHROPIC_OPUS_4_MODEL_ID = os.getenv("ANTHROPIC_OPUS_4_MODEL_ID")

# Postgres Constants
DB_CONNECTION_LINK = "postgresql+asyncpg://{}:{}@{}/{}".format(  # async version
    # DB_CONNECTION_LINK = "postgresql+psycopg2://{}:{}@{}/{}".format(  # sync version
    os.getenv("STATIC_DATABASE_USER"),
    os.getenv("STATIC_DATABASE_PASS"),
    os.getenv("STATIC_DATABASE_URL"),
    os.getenv("STATIC_DATABASE_DB"),
)

# Cloudwatch Constants
LOG_STREAM = os.getenv("LOG_STREAM")
LOG_GROUP_NAME = os.getenv("LOG_GROUP_NAME")
CLOUDWATCH_AWS_REGION = os.getenv("CLOUDWATCH_AWS_REGION")
CLOUDWATCH_AWS_KEY_ID = os.getenv("CLOUDWATCH_AWS_KEY_ID")
CLOUDWATCH_AWS_SECRET_KEY = os.getenv("CLOUDWATCH_AWS_SECRET_KEY")
SUGGESTION_LOG_GROUP_NAME = os.getenv("SUGGESTION_LOG_GROUP_NAME")

# Perplexity API Key
PERPLEXITY_API_KEY = os.getenv("PERPLEXITY_API_KEY")

# Parallel API Key
PARALLEL_API_KEY = os.getenv("PARALLEL_API_KEY")

# Gemini API Key
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
IMAGE_TO_TEXT_MODEL_ID = os.getenv("IMAGE_TO_TEXT_MODEL_ID")
TEXT_TO_IMAGE_MODEL_ID = os.getenv("TEXT_TO_IMAGE_MODEL_ID")
GEMINI_ES_MODEL_ID = os.getenv("GEMINI_ES_MODEL_ID")

# HeyGen
HEYGEN_API_KEY = os.getenv("HEYGEN_API_KEY")
HEYGEN_AVATAR_ID = os.getenv("HEYGEN_AVATAR_ID")
HEYGEN_VOICE_ID = os.getenv("HEYGEN_VOICE_ID")
HEYGEN_FOLDER_ID = os.getenv("HEYGEN_FOLDER_ID")

REPORT_GEN_MESSAGE = "Your report has been generated successfully. You can download it."

# ── Microservices split — base URLs for the services this code now calls over
# HTTP instead of in-process. See app.adapters.grep_agent_2/__init__.py,
# app.deliverables.service_entry.py, app.research.infographics/one_pager.py,
# and app.deliverables.service_pptx.py for the shim call sites.
#
# The defaults are the LOCAL ones (`uv run uvicorn ...` on this laptop), not
# Docker DNS names. `http://grep-service:8010` and
# `http://report-render-service:8020` were the previous defaults and resolve
# nowhere outside a compose network that happens to use those aliases — and
# the folders are now named file-handling and render-report, so those names
# were wrong in compose too. Docker Compose sets these explicitly
# (docker-compose.yml at the workspace root) to http://file-handling:8010 and
# http://render-report:8020; anything else falls back to localhost, which is
# right for a developer running four uvicorns.
GREP_SERVICE_BASE_URL = os.getenv("GREP_SERVICE_BASE_URL", "http://localhost:8010")
REPORT_RENDER_SERVICE_BASE_URL = os.getenv(
    "REPORT_RENDER_SERVICE_BASE_URL", "http://localhost:8020"
)

# Shared secret proving to a sibling service that an /internal/* caller is us
# (render-report/src/resources/dependencies.py checks it as the
# `x-internal-secret` header). Empty in local development, where render-report
# skips the check so a plain curl works; render-report refuses to boot with it
# unset in production. The shims send the header only when this is set, so
# turning internal auth on is a matter of setting one variable on both sides —
# see app.deliverables.service_entry.py.
INTERNAL_API_SECRET = os.getenv("INTERNAL_API_SECRET", "")


# Chat Title Prompt
class ChatTitle(BaseModel):
    chat_title: str = Field(
        description="A concise chat title that captures the main topic or purpose of the conversation",
        # max_length=100
    )


# STRUCTURED_LLM = LLM.with_structured_output(ChatTitle)  # Bedrock disabled — using direct Anthropic API instead
# include_raw=True keeps the underlying AIMessage (with usage_metadata) alongside
# the parsed Pydantic object, instead of discarding it.
STRUCTURED_LLM = ANTHROPIC_LLM.with_structured_output(ChatTitle, include_raw=True)
# GROQ_STRUCTURED_LLM = GROQ_LLM.with_structured_output(ChatTitle, method="json_mode")

CASPR_INFO_HEYGEN = """Thank you for publishing this report.

Caspr Research is a full-stack AI market research firm that delivers real time, credible insights by analyzing diverse data sources with its proprietary AI. Our AI-generated insights: both quantitative and qualitative are continuously validated by top industry experts across various sectors, topics, and regions.
"""

# Include Domains for Tavily Search
# INCLUDE_DOMAINS = [
#     "https://www.reuters.com",
#     "https://franklintempleton.lu",
#     "https://www.bloomberg.com",
#     "https://www.cbsnews.com",
#     "https://www.iea.org",
#     "https://polymerupdate.com",
#     "https://www.opec.org",
#     "https://www.woodmac.com",
#     "https://www.eia.gov",
#     "https://rigcount.bakerhughes.com",
#     "https://www.financialsense.com",
#     "https://www.iata.org",
#     "https://www.spglobal.com",
#     "https://www.barrons.com",
#     "https://www.iaea.org",
#     "https://www.washingtonpost.com",
#     "https://www.abc.net.au",
#     "https://www.pipeline-journal.net",
#     "https://www.bankfab.com",
#     "https://4043042.fs1.hubspotusercontent-na1.net",
#     "https://guardian.ng",
#     "https://iea.blob.core.windows.net",
#     "https://initiatives.weforum.org",
#     "https://ourworldindata.org",
#     "https://pemedianetwork.com",
#     "https://uscode.house.gov",
#     "https://www.api.org",
#     "https://www.ft.com",
#     "https://www.gov.il",
#     "https://www.ief.org",
#     "https://www.imf.org",
#     "https://www.macrotrends.net",
#     "https://www.portcast.io",
#     "https://www.rudaw.net",
#     "https://www.vitol.com",
#     "https://www.voanews.com",
#     "https://www.zawya.com",
#     "https://worldgbc.org",
#     "https://core.ac.uk",
#     "https://www.meed.com",
#     "https://emiratesgbc.org",
#     "https://aesg.com",
#     "https://www.thenationalnews.com",
#     "https://www.bonafideresearch.com",
#     "https://papers.ssrn.com",
#     "https://fastcompanyme.com",
#     "https://bwpeople.businessworld.in",
#     "https://www.bclplaw.com",
#     "https://www.moei.gov.ae",
#     "https://planradar.com",
#     "https://www.centralbank.ae",
#     "https://www.moccae.gov.ae",
#     "https://www.eea.europa.eu",
#     "https://climatebonds.net",
#     "https://assets.kpmg.com",
#     "https://www.nature.org",
#     "https://www.tnfd.global",
#     "https://www.ifc.org",
#     "https://www.wam.ae",
#     "https://prosperitydata360.worldbank.org",
#     "https://mea-finance.com",
#     "https://www.nature.com",
#     "https://kpmg.com",
#     "https://gulfbusiness.com",
#     "https://www.majidalfuttaim.com",
#     "https://www.citigroup.com",
#     "https://gulfnews.com",
#     "https://www.se.com.sa",
#     "https://www.fundsglobalmena.com",
#     "https://www.ssga.com",
#     "https://www.fitchratings.com",
#     "https://www.forbesmiddleeast.com",
#     "https://www.moenergy.gov.sa",
#     "https://solarquarter.com",
#     "https://www.worldfutureenergysummit.com",
#     "https://www.arabnews.com",
#     "https://www.imarcgroup.com",
#     "https://www.spa.gov.sa",
#     "https://mediaoffice.ae",
#     "https://u.ae",
#     "https://english.alarabiya.net",
#     "https://www.dubaidet.gov.ae",
#     "https://unfccc.int",
#     "https://www.timeoutriyadh.com",
#     "https://www.agbi.com",
#     "https://aquila.is",
#     "https://ndmc.gov.sa",
#     "https://www.dfsa.ae",
#     "https://www.capitalmonitor.ai",
#     "https://www.weforum.org",
#     "https://paulsoninstitute.org",
#     "https://wedocs.unep.org",
#     "https://unicef.org",
#     "https://www.fao.org",
#     "https://iris.who.int",
#     "https://data.worldbank.org",
#     "https://www.cdproject.net",
#     "https://data.apps.fao.org",
#     "https://clientearth.org",
#     "https://www.greenqueen.com.hk",
#     "https://www.fastcompany.com",
#     "https://safcregistry.energyweb.org",
#     "https://www.offsetguide.org",
#     "https://www.iso.org",
#     "https://rmi.org",
#     "https://bezerocarbon.com",
#     "https://vcmintegrity.org",
#     "https://www.irecstandard.org",
#     "https://www.epa.gov",
#     "https://recs.org",
#     "https://www.aib-net.org",
#     "https://www.think-renewable.com",
#     "https://www.icao.int",
#     "https://www.transportenvironment.org",
#     "https://flysaba.org",
#     "https://www3.weforum.org",
#     "https://www.un.org",
#     "https://openknowledge.fao.org",
#     "https://unfoundation.org",
#     "https://futurity.org",
#     "https://www.rand.org",
#     "https://impact.economist.com",
#     "https://investmentmonitor.ai",
#     "https://ifarm.fi",
#     "https://sciencedirect.com",
#     "https://verdict.co.uk",
#     "https://economistimpact.com",
#     "https://www.alrajhi-capital.com/",
#     "https://www.nbk.com/",
#     "https://www.nbkwealth.com/",
#     "https://www.emiratesnbdresearch.com/",
# ]

# JWT Constants
JWT_SECRET_KEY = os.getenv("JWT_SECRET_KEY")
JWT_ALGORITHM = os.getenv("JWT_ALGORITHM")
JWT_ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("JWT_ACCESS_TOKEN_EXPIRE_MINUTES"))
JWT_REFRESH_TOKEN_EXPIRE_DAYS = int(os.getenv("JWT_REFRESH_TOKEN_EXPIRE_DAYS"))
FORGOT_PASSWORD_EXPIRE_MINUTES = int(os.getenv("FORGOT_PASSWORD_EXPIRE_MINUTES", "10"))
SIGNUP_VERIFICATION_EXPIRE_MINUTES = int(os.getenv("SIGNUP_VERIFICATION_EXPIRE_MINUTES", "15"))
SIGNUP_VERIFICATION_EMAIL_COOLDOWN_SECONDS = int(
    os.getenv("SIGNUP_VERIFICATION_EMAIL_COOLDOWN_SECONDS", "30")
)
FORGOT_PASSWORD_EMAIL_COOLDOWN_SECONDS = int(
    os.getenv("FORGOT_PASSWORD_EMAIL_COOLDOWN_SECONDS", "30")
)


if ENVIRONMENT == "DEV":
    FRONTEND_RESET_PASSWORD_URL = "https://dev.caspr.ai/reset-password"
    # FRONTEND_SIGNUP_VERIFICATION_URL = "https://caspr.ezlab.in/verify-email"
    # FRONTEND_VERIFICATION_URL = "https://dev.caspr.ai/verify-account"
    FRONTEND_VERIFICATION_URL = "https://dev.caspr.ai/get-started/thanks"
    FRONTEND_URL = "https://dev.caspr.ai"
    ALLOWED_ORIGINS = ["*"]
else:
    FRONTEND_RESET_PASSWORD_URL = "https://caspr.ai/reset-password"
    # FRONTEND_SIGNUP_VERIFICATION_URL = "https://caspr.ai/verify-email"
    # FRONTEND_VERIFICATION_URL = "https://caspr.ai/verify-account"
    FRONTEND_VERIFICATION_URL = "https://caspr.ai/get-started/thanks"
    FRONTEND_URL = "https://caspr.ai"
    ALLOWED_ORIGINS = ["https://caspr.ai", "https://www.caspr.ai", "https://wallet.caspr.ai"]

# JWT Settings
JWT_EXPIRATION_DELTA = timedelta(minutes=JWT_ACCESS_TOKEN_EXPIRE_MINUTES)
JWT_REFRESH_EXPIRATION_DELTA = timedelta(days=JWT_REFRESH_TOKEN_EXPIRE_DAYS)

# Google OAuth
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")

# Email Configuration
EMAIL_CONFIG = {
    "SMTP_SERVER": "smtp.gmail.com",
    "SMTP_PORT": 587,
    "SENDER_EMAIL": os.getenv("SENDER_EMAIL"),  # Replace with your email
    "APP_PASSWORD": os.getenv("EMAIL_PASS"),  # Replace with your app password
    "USE_TLS": True,
    "CC_EMAILS": os.getenv("EMAIL_CC").split(","),
    "BCC_EMAILS": os.getenv("EMAIL_BCC").split(","),
    # "BCC_EMAILS": ['naman.bhatia@ez.works']
}

# Subscriber Email Configuration
SUBSCRIBER_SENDER_EMAIL = os.getenv("SUBSCRIBER_SENDER_EMAIL")
SUBSCRIBER_SENDER_EMAIL_PASS = os.getenv("SUBSCRIBER_SENDER_EMAIL_PASS")
SUBSCRIBER_TO_EMAILS = [
    email.strip() for email in os.getenv("SUBSCRIBER_TO_EMAILS", "").split(",") if email.strip()
]
SUBSCRIBER_CC_EMAILS = [
    email.strip() for email in os.getenv("SUBSCRIBER_CC_EMAILS", "").split(",") if email.strip()
]
SUBSCRIBER_BCC_EMAILS = [
    email.strip() for email in os.getenv("SUBSCRIBER_BCC_EMAILS", "").split(",") if email.strip()
]

# Request Email Configuration
REQUEST_SENDER_EMAIL = os.getenv("REQUEST_SENDER_EMAIL")
REQUEST_SENDER_EMAIL_PASS = os.getenv("REQUEST_SENDER_EMAIL_PASS")
REQUEST_TO_EMAILS = [
    email.strip() for email in os.getenv("REQUEST_TO_EMAILS", "").split(",") if email.strip()
]
REQUEST_CC_EMAILS = [
    email.strip() for email in os.getenv("REQUEST_CC_EMAILS", "").split(",") if email.strip()
]
REQUEST_BCC_EMAILS = [
    email.strip() for email in os.getenv("REQUEST_BCC_EMAILS", "").split(",") if email.strip()
]


# Book a Call Email Configuration
BOOK_CALL_SENDER_EMAIL = os.getenv("BOOK_CALL_SENDER_EMAIL")
BOOK_CALL_SENDER_EMAIL_PASS = os.getenv("BOOK_CALL_SENDER_EMAIL_PASS")
BOOK_CALL_TO_EMAILS = [
    email.strip() for email in os.getenv("BOOK_CALL_TO_EMAILS", "").split(",") if email.strip()
]
BOOK_CALL_BCC_EMAILS = [
    email.strip() for email in os.getenv("BOOK_CALL_BCC_EMAILS", "").split(",") if email.strip()
]

# Error Alert Email Configuration
ERROR_ALERT_SENDER_EMAIL = os.getenv("ERROR_ALERT_SENDER_EMAIL")
ERROR_ALERT_SENDER_EMAIL_PASS = os.getenv("ERROR_ALERT_SENDER_EMAIL_PASS")
ERROR_ALERT_EMAILS = [
    email.strip() for email in os.getenv("ERROR_ALERT_EMAILS", "").split(",") if email.strip()
]
ERROR_DIGEST_INTERVAL_SECONDS = max(60, int(os.getenv("ERROR_DIGEST_INTERVAL_SECONDS", "300")))

# Payment Alert Email Configuration
PAYMENT_ALERT_SENDER_EMAIL = os.getenv("PAYMENT_ALERT_SENDER_EMAIL")
PAYMENT_ALERT_SENDER_EMAIL_PASS = os.getenv("PAYMENT_ALERT_SENDER_EMAIL_PASS")
PAYMENT_ALERT_EMAILS = [
    email.strip() for email in os.getenv("PAYMENT_ALERT_EMAILS", "").split(",") if email.strip()
]

# ============================================================================
# Wallet/Token System Constants
# ============================================================================

# Token amounts (with defaults if env vars not set)
SIGNUP_BONUS_TOKENS = int(os.getenv("SIGNUP_BONUS_TOKENS", "25000"))
REPORT_GENERATION_COST = int(os.getenv("REPORT_GENERATION_COST", "12500"))
TOKEN_EXPIRY_DAYS = int(os.getenv("TOKEN_EXPIRY_DAYS", "365"))

MAX_REPORT_VERSIONS_FREE = 2  # free-tier per-report version cap
MAX_REPORT_VERSIONS_PAID = 5  # plus/pro per-report version cap

# Subscription tier monthly costs in USD
TIER_COST_USD = {
    "plus": float(os.getenv("TIER_COST_PLUS_USD")),
    "pro": float(os.getenv("TIER_COST_PRO_USD")),
}

SUBSCRIPTION_INFO = {
    "yearly": {
        "free": {
            "default": {"cost": 0, "currency": "USD", "tokens_per_month": 0},
            "in": {"cost": 0, "currency": "INR", "tokens_per_month": 0},
        },
        "plus": {
            "in": {"cost": 47988, "currency": "INR", "tokens_per_month": 25000},
            "default": {"cost": 600, "currency": "USD", "tokens_per_month": 25000},
        },
        "pro": {
            "in": {"cost": 191988, "currency": "INR", "tokens_per_month": 150000},
            "default": {"cost": 2400, "currency": "USD", "tokens_per_month": 150000},
        },
    },
    "monthly": {
        "free": {
            "default": {"cost": 0, "currency": "USD", "tokens_per_month": 0},
            "in": {"cost": 0, "currency": "INR", "tokens_per_month": 0},
        },
        "plus": {
            "in": {"cost": 4999, "currency": "INR", "tokens_per_month": 25000},
            "default": {"cost": 60, "currency": "USD", "tokens_per_month": 25000},
        },
        "pro": {
            "in": {"cost": 19999, "currency": "INR", "tokens_per_month": 150000},
            "default": {"cost": 240, "currency": "USD", "tokens_per_month": 150000},
        },
    },
}

COUNTRY_TO_CURRENCY = {"in": "INR", "default": "USD"}


# Default total count for subscriptions (0 for unlimited)
DEFAULT_SUBSCRIPTION_TOTAL_COUNT = int(os.getenv("DEFAULT_SUBSCRIPTION_TOTAL_COUNT", "0"))

# Token pricing for top-up (tokens per USD)
TOKENS_PER_USD = int(os.getenv("TOKENS_PER_USD", "1250"))

TOKEN_PRICE_INFO = {
    "INR": float(os.getenv("TOKEN_PRICE_INFO_INR")),
    "USD": float(os.getenv("TOKEN_PRICE_INFO_USD")),
}

# Default currency for top-up
DEFAULT_TOPUP_CURRENCY = os.getenv("DEFAULT_TOPUP_CURRENCY", "USD")


CASPR_PAYMENT_BASE_URL = os.getenv(
    "CASPR_PAYMENT_BASE_URL", "https://72b54a898014.ngrok-free.app/api/v1"
)
CASPR_PAYMENT_API_KEY = os.getenv("CASPR_PAYMENT_API_KEY", "456")

X_API_KEY = os.getenv("X_API_KEY", "789")


TAX_INFO = {"in": [{"tax": "GST", "tax_percentage": 18}]}


# Live Sources Count
LIVE_SOURCES_COUNT = int(os.getenv("LIVE_SOURCES_COUNT"))

VISUALIZATION_MODEL = os.getenv("VISUALIZATION_MODEL")

NAPKIN_API_TOKEN = os.getenv("NAPKIN_API_TOKEN")

ASYNC_OPENAI_CLIENT = AsyncOpenAI(api_key=OPENAI_API_KEY)

SYNC_OPENAI_CLIENT = OpenAI(api_key=OPENAI_API_KEY)


# ============================================================================
# File Upload Limits
# ============================================================================

# Maximum number of files a user can upload in a single upload request
MAX_FILES_PER_UPLOAD = 3

# Maximum number of file references (new uploads + previously uploaded files) a user can attach to a single chat
MAX_FILE_REFERENCES_PER_CHAT = 3

# Per-turn limits for auxiliary tool calls (per single human message)
MAX_WEB_SEARCH_CALLS_PER_TURN = 3
MAX_QUERY_DOC_CALLS_PER_TURN = 2

# Maximum character length for user-submitted prompts (per flow)
CHAT_MAXIMUM_CHARACTER = 10_000
ASK_CASPR_MAXIMUM_CHARACTERS = 2_000
REFINE_MAXIMUM_CHARACTERS = 2_000

# Maximum file size in bytes (25 MB)
MAX_UPLOAD_FILE_SIZE_BYTES = 25 * 1024 * 1024

# Allowed file extensions for upload
ALLOWED_UPLOAD_EXTENSIONS = {
    ".md",
    ".pdf",
    ".docx",
    ".doc",
    ".txt",
    ".json",
    ".csv",
    ".xml",
    ".html",
    ".htm",
    ".xls",
    ".xlsx",
}


MCP_URL = os.getenv("MCP_URL")

# Valid domain slugs accepted by the domain-reports endpoint
_VALID_DOMAIN_SLUGS = frozenset(
    [
        "default",
        "primary_research",
        "due_diligence",
        "industry_benchmarking",
        "market_insight",
        "rfp",
        "business_plan",
    ]
)


# =============================================================================
# GREP AGENT 2 — Model Configuration
# =============================================================================
GREP_COORDINATOR_MODEL = os.getenv("GREP_COORDINATOR_MODEL", "gpt-5.4")
GREP_WORKER_MODEL = os.getenv("GREP_WORKER_MODEL", "gpt-4o")
GREP_SUMMARY_MODEL = os.getenv("GREP_SUMMARY_MODEL", "gpt-4o")

CARD_GEN_MODEL_OPENAI = os.getenv("CARD_GEN_MODEL_OPENAI", "gpt-5.4")
DRL_GEN_MODEL = os.getenv("DRL_GEN_MODEL", "gpt-4o")
COMPRESS_CONTEXT_SUMMARY_MODEL = os.getenv("COMPRESS_CONTEXT_SUMMARY_MODEL", "gpt-4o-mini")
QUERY_DOC_MODEL = os.getenv("QUERY_DOC_MODEL", "gpt-5.4")
RETRIEVE_LATEST_INFO_MODEL = os.getenv("RETRIEVE_LATEST_INFO_MODEL", "gpt-4o")
CARD_SUMMARY_MODEL = os.getenv("CARD_SUMMARY_MODEL", "gpt-4o")
CHAT_TITLE_MODEL = os.getenv("CHAT_TITLE_MODEL", "gpt-4o")
GENERATE_TITLE_FOR_TABLE_MODEL = os.getenv("GENERATE_TITLE_FOR_TABLE_MODEL", "gpt-4o")
PLOT_TYPE_MODEL = os.getenv("PLOT_TYPE_MODEL", "gpt-4o")
REFINE_REPORT_LAYOUT_MODEL = os.getenv("REFINE_REPORT_LAYOUT_MODEL", "gpt-4o")
TABLE_DICISION_MODEL = os.getenv("TABLE_DICISION_MODEL", "gpt-4o")
CARD_FIX_MODEL = os.getenv("CARD_FIX_MODEL", "gpt-5.4")
OPENAI_VALIDATION_MODEL = os.getenv("OPENAI_VALIDATION_MODEL", "gpt-4o")

# Additional model configuration
REFINER_MODEL = os.getenv("REFINER_MODEL", "gpt-5.4")
GEMINI_HTML_MODEL = os.getenv("GEMINI_HTML_MODEL", "gemini-3.1-pro-preview")
GEMINI_CARD_MODEL = os.getenv("GEMINI_CARD_MODEL", "gemini-3.6-flash")
TABLE_FIXER_MODEL = os.getenv("TABLE_FIXER_MODEL", "gpt-4o")
REFINE_TABLE_POLICY_MODEL = os.getenv("REFINE_TABLE_POLICY_MODEL", "gpt-4o-mini")
GREP_AGENT_MODEL = os.getenv("GREP_AGENT_MODEL", "gpt-5.5")
GREP_SYNTHESIS_MODEL = os.getenv("GREP_SYNTHESIS_MODEL", "gpt-4.1-mini")
PRIMARY_RESEARCH_MODEL = os.getenv("PRIMARY_RESEARCH_MODEL", "gpt-5.4")
DUE_DILIGENCE_MODEL = os.getenv("DUE_DILIGENCE_MODEL", "gpt-5.4")
REPORT_TEXTUAL_ANALYSIS_MODEL = os.getenv("REPORT_TEXTUAL_ANALYSIS_MODEL", "gpt-4o")
MD_OPTIMIZER_MODEL = os.getenv("MD_OPTIMIZER_MODEL", "gpt-4o")
FINDINGS_MODEL = os.getenv("FINDINGS_MODEL", "gpt-5.2")
SECTOR_NAME_MODEL = os.getenv("SECTOR_NAME_MODEL", "gpt-4o-mini")
DASHBOARD_MODEL = os.getenv("DASHBOARD_MODEL", "gpt-5.2")
PUBLISH_CSS_MODEL = os.getenv("PUBLISH_CSS_MODEL", "gpt-4o")
TITLE_SANITIZER_MODEL = os.getenv("TITLE_SANITIZER_MODEL", "gpt-4o")
POSTER_CLASSIFIER_MODEL = os.getenv("POSTER_CLASSIFIER_MODEL", "gpt-4o")
VIZ_PLOT_CODE_MODEL = os.getenv("VIZ_PLOT_CODE_MODEL", "gpt-5.1")
INFOGRAPHIC_CONTENT_MODEL = os.getenv("INFOGRAPHIC_CONTENT_MODEL", "gpt-5.2")
INFOGRAPHIC_DALLE2_MODEL = os.getenv("INFOGRAPHIC_DALLE2_MODEL", "dall-e-2")
INFOGRAPHIC_GEMINI_IMAGE_MODEL = os.getenv(
    "INFOGRAPHIC_GEMINI_IMAGE_MODEL", "nano-banana-pro-preview"
)
REPORT_ILLUSTRATION_MODEL = os.getenv("REPORT_ILLUSTRATION_MODEL", "dall-e-3")
TOKEN_CHECKER_MODEL = os.getenv("TOKEN_CHECKER_MODEL", "gpt-4o-mini")
ASK_CASPR_MODEL_MAIN = os.getenv("ASK_CASPR_MODEL_MAIN")
ASK_CASPR_MODEL_FALLBACK = os.getenv("ASK_CASPR_MODEL_FALLBACK")
SPECIALIZED_SEARCH_MODEL = os.getenv("SPECIALIZED_SEARCH_MODEL", "gpt-5.5")
GEMINI_DRL_GEN_MODEL = os.getenv("GEMINI_DRL_GEN_MODEL", "gemini-3.6-flash")
GEMINI_SUMMARY_MODEL = os.getenv("GEMINI_SUMMARY_MODEL", "gemini-3.6-flash")

# =============================================================================
# Gemini fallback models — mirror the OpenAI models above so every OpenAI call
# site has a same-purpose Gemini backup.
# =============================================================================
GEMINI_COMPRESS_CONTEXT_SUMMARY_MODEL = os.getenv(
    "GEMINI_COMPRESS_CONTEXT_SUMMARY_MODEL", "gemini-3.6-flash"
)
GEMINI_BRIEF_STREAM_MODEL = os.getenv("GEMINI_BRIEF_STREAM_MODEL", "gemini-3.1-pro-preview")
GEMINI_GENERATE_TITLE_FOR_TABLE_MODEL = os.getenv(
    "GEMINI_GENERATE_TITLE_FOR_TABLE_MODEL", "gemini-3.6-flash"
)
GEMINI_QUERY_DOC_MODEL = os.getenv("GEMINI_QUERY_DOC_MODEL", "gemini-3.1-pro-preview")
GEMINI_RETRIEVE_LATEST_INFO_MODEL = os.getenv(
    "GEMINI_RETRIEVE_LATEST_INFO_MODEL", "gemini-3.1-pro-preview"
)
GEMINI_CHAT_TITLE_MODEL = os.getenv("GEMINI_CHAT_TITLE_MODEL", "gemini-3.6-flash")
GEMINI_REPORT_TEXTUAL_ANALYSIS_MODEL = os.getenv(
    "GEMINI_REPORT_TEXTUAL_ANALYSIS_MODEL", "gemini-3.1-pro-preview"
)
GEMINI_POSTER_CLASSIFIER_MODEL = os.getenv("GEMINI_POSTER_CLASSIFIER_MODEL", "gemini-3.6-flash")
GEMINI_TITLE_SANITIZER_MODEL = os.getenv("GEMINI_TITLE_SANITIZER_MODEL", "gemini-3.6-flash")
GEMINI_INFOGRAPHIC_CONTENT_MODEL = os.getenv(
    "GEMINI_INFOGRAPHIC_CONTENT_MODEL", "gemini-3.1-pro-preview"
)
GEMINI_PUBLISH_CSS_MODEL = os.getenv("GEMINI_PUBLISH_CSS_MODEL", "gemini-3.6-flash")
GEMINI_VALIDATION_MODEL = os.getenv("GEMINI_VALIDATION_MODEL", "gemini-3.6-flash")
GEMINI_VIZ_PLOT_CODE_MODEL = os.getenv("GEMINI_VIZ_PLOT_CODE_MODEL", "gemini-3.1-pro-preview")
GEMINI_TABLE_DECISION_MODEL = os.getenv("GEMINI_TABLE_DECISION_MODEL", "gemini-3.6-flash")
GEMINI_PLOT_TYPE_MODEL = os.getenv("GEMINI_PLOT_TYPE_MODEL", "gemini-3.6-flash")
GEMINI_TABLE_FIXER_MODEL = os.getenv("GEMINI_TABLE_FIXER_MODEL", "gemini-3.6-flash")
GEMINI_REFINE_TABLE_POLICY_MODEL = os.getenv("GEMINI_REFINE_TABLE_POLICY_MODEL", "gemini-3.6-flash")
GEMINI_GREP_COORDINATOR_MODEL = os.getenv("GEMINI_GREP_COORDINATOR_MODEL", "gemini-3.6-flash")
GEMINI_GREP_WORKER_MODEL = os.getenv("GEMINI_GREP_WORKER_MODEL", "gemini-3.6-flash")
GEMINI_GREP_SUMMARY_MODEL = os.getenv("GEMINI_GREP_SUMMARY_MODEL", "gemini-3.6-flash")
GEMINI_GREP_AGENT_MODEL = os.getenv("GEMINI_GREP_AGENT_MODEL", "gemini-3.1-pro-preview")
GEMINI_GREP_SYNTHESIS_MODEL = os.getenv("GEMINI_GREP_SYNTHESIS_MODEL", "gemini-3.6-flash")
GEMINI_FINDINGS_MODEL = os.getenv("GEMINI_FINDINGS_MODEL", "gemini-3.1-pro-preview")
GEMINI_SECTOR_NAME_MODEL = os.getenv("GEMINI_SECTOR_NAME_MODEL", "gemini-3.6-flash")
GEMINI_DASHBOARD_MODEL = os.getenv("GEMINI_DASHBOARD_MODEL", "gemini-3.1-pro-preview")
GEMINI_PRIMARY_RESEARCH_MODEL = os.getenv("GEMINI_PRIMARY_RESEARCH_MODEL", "gemini-3.1-pro-preview")
GEMINI_DUE_DILIGENCE_MODEL = os.getenv("GEMINI_DUE_DILIGENCE_MODEL", "gemini-3.1-pro-preview")

PRIORITIZE_ARXIV = os.getenv("PRIORITIZE_ARXIV", "false").strip().lower() in ("true", "1", "yes")
