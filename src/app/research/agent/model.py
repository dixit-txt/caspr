"""new_model.py: Model for the Casper backend"""
import datetime
from email import generator
import re
import shutil
from typing import List, Dict, Any, Tuple, Literal, Generator, AsyncGenerator, TypedDict, Optional
from uuid_utils import uuid7
# from langchain_aws import ChatBedrock  # Bedrock disabled — using direct Anthropic API instead
from langchain_core.documents.base import Document
from langchain_core.messages import SystemMessage, HumanMessage, AIMessage, AIMessageChunk, ToolMessage
# from langchain_core.messages.base import BaseMessage
# from langchain_tavily import TavilySearch
from langgraph.graph import MessagesState, StateGraph, END
from langchain_core.tools import tool
from langgraph.prebuilt import ToolNode
from langchain_anthropic import ChatAnthropic

# from langgraph.checkpoint.memory import MemorySaver
import json
import os
import asyncio
import time
from langchain_core.prompts import ChatPromptTemplate
from app.core.logging import setup_logging
from pydantic import BaseModel, Field
from langgraph.config import get_stream_writer

# from app.research.infographics import process_infographics
# from app.deliverables.helper_functions import generate_image_with_Google, generate_image_with_openai
from src.core.cards.card_fixer import fix_card
from app.adapters.s3 import get_s3_instance
import tempfile
import json
from app.core.constants import (
    # ANTHROPIC_MODEL_ID,
    # BEDROCK_LLM_ID,
    # BEDROCK_REPORT_LLM,
    # REPORT_LLM,
    ANTHROPIC_MODEL_ID,
    ANTHROPIC_API_KEY,
    ANTHROPIC_OUTPUT_CONFIG,
    OPENAI_CHAT_MODEL_ID,
    OPENAI_LLM_LANGCHAIN,
    MAX_QUERY_DOC_CALLS_PER_TURN,
    MAX_WEB_SEARCH_CALLS_PER_TURN,
    PRIORITIZE_ARXIV,
    REPORT_GENERATION_COST,
    S3_REPORTS_BASE_PATH,
    SECRETS_CLIENT,
    SECRETS_NAME,
    # LLM,  # Bedrock disabled — using direct Anthropic API instead
    # INCLUDE_DOMAINS,
    # VECTOR_DB,
    STRUCTURED_LLM,
    ASYNC_OPENAI_CLIENT,
    MCP_URL,
    use_grep_file_search,
    QUERY_DOC_MODEL,
    RETRIEVE_LATEST_INFO_MODEL,
    CHAT_TITLE_MODEL,
    GEMINI_API_KEY,
    GEMINI_QUERY_DOC_MODEL,
    GEMINI_RETRIEVE_LATEST_INFO_MODEL,
    GEMINI_CHAT_TITLE_MODEL,
)
from google import genai
from app.adapters.grep_agent_2 import ask_pdfs, PdfSession
from app.core.db import async_session_scope
from src.services.wallet_service import WalletService

from app.research.prompts.prompt_utils import SYSTEM_MESSAGE, CHAT_TITLE_PROMPT, SYSTEM_MESSAGE_WITH_DOCUMENT, get_system_message_with_documents
from src.core.cards.card_utils import add_viz_to_card, clean_drl, clean_drl_to_clean_rl, clean_url, convert_json_to_md, extract_title_and_toc, gather_context_from_uploaded_file, generate_brief_cards, generate_cards, generate_cumulative_summary, generate_drl, generate_section_summary, modify_card, modify_report_layout, refine_cumulative_summary, refine_report_layout, remove_citations_from_DRL, remove_citations_from_RL, replace_brief_citations, replace_citations
from app.observability.web_search_analytics import (
    enrich_terminal_search_analytics,
    extract_openai_search_analytics,
    extract_gemini_search_analytics,
    logical_card_id,
    log_scheduled_analytics_batch,
    search_analytics_schedule_kwargs,
)
from src.db.web_search_db import log_web_search_event
from src.core.cards.card_utils import update_summaries_for_ask_caspr
from app.research.domains.primary_research import build_primary_research_subgraph
from app.research.domains.due_diligence import build_due_diligence_subgraph
from app.research.domains.planner import _drl_heartbeat_lines, _run_card_heartbeat
from app.observability.llm_response_logger import save_raw_llm_response, strip_json_code_fence

logger = setup_logging(__name__)

GENERIC_TOOL_ERROR_MSG = "Something went wrong while processing your request. Please try again."

class DocumentQueryResponse(BaseModel):
    """Response model for document queries"""
    answer: str = Field(description="The answer to the user's question about the document")
    citations: List[str] = Field(
        default_factory=list,
        description="List of citation URLs from retrieved sources"
    )

class SearchQueries(BaseModel):
    """Model for generating search queries based on user instructions and report layout"""
    queries: List[str] = Field(
        description="List of search queries generated for each topic",
        min_items=1
    )


# ---------------------------------------------------------------------------
# Heartbeat helpers for retrieve_latest_info
# ---------------------------------------------------------------------------

_HEARTBEAT_STOP_WORDS = {
    "the", "a", "an", "of", "in", "on", "and", "for", "to", "with",
    "is", "are", "was", "latest", "current", "recent", "about",
}


def _topic_from_query(query: str, max_words: int = 7) -> str:
    words = [w for w in query.split() if w.lower() not in _HEARTBEAT_STOP_WORDS]
    return " ".join(words[:max_words]).rstrip(".,;") or query[:50]


def _heartbeat_messages(search_query: str, progress_updates: List[str]) -> List[str]:
    """Build ordered heartbeat lines for a search from the model's own progress_updates."""
    cleaned: List[str] = []
    seen: set = set()
    for msg in progress_updates or []:
        text = (msg or "").strip()
        key = text.lower()
        if text and key not in seen:
            seen.add(key)
            cleaned.append(text)
    if cleaned:
        return cleaned
    return [f"Still gathering sources on {_topic_from_query(search_query)}"]


async def _run_heartbeat(
    search_query: str,
    progress_updates: List[str],
    event_writer,
    started: float,
    interval: float = 4.0,
    tool_name: str = "retrieve_latest_info",
) -> None:
    """
    Emit heartbeat events via event_writer while a search/lookup is in flight.

    Paces the model's own progress_updates lines out over time, one per tick.
    If the lookup outlasts the supplied lines, the LAST line is held until the
    work completes (the caller cancels this task as soon as it finishes).  Falls
    back to a topic-derived line if the model supplied none.

    `tool_name` must match the name the API layer uses to map to the correct
    SSE event type (e.g. "retrieve_latest_info" → learning_brain_latest_*,
    "query_document" → learning_brain_document_*).
    """
    messages = _heartbeat_messages(search_query, progress_updates)
    tick = 0
    while True:
        await asyncio.sleep(interval)
        elapsed = time.time() - started
        # Play the model's lines in order; once exhausted, hold the last line
        # until the lookup completes.
        msg = messages[min(tick, len(messages) - 1)]
        event_writer({
            "name": tool_name,
            "status": "heartbeat",
            "message": msg,
            "elapsed": f"{elapsed:.0f}s",
        })
        tick += 1


# ---------------------------------------------------------------------------
# Gate node helpers — enforce one tool call per step
# ---------------------------------------------------------------------------

_TRANSIENT_FLAG = "_caspr_transient"

_GATE_REJECTION_TEMPLATE = (
    "Please call only ONE tool at a time. "
    "You just requested {n} tool calls in a single step ({names}). "
    "Pick the single most important action right now and call only that one tool."
)


def _is_transient(message) -> bool:
    """Return True if this message is a transient gate-correction artifact."""
    meta = getattr(message, "additional_kwargs", None) or {}
    return bool(meta.get(_TRANSIENT_FLAG))


def _compact_history(messages: list) -> list:
    """Drop transient gate-correction messages while preserving all real tool-call/result pairs."""
    return [m for m in messages if not _is_transient(m)]


def _normalize_tool_call(call: Dict[str, Any]) -> Dict[str, Any]:
    """Ensure tool-call args are a dict (some providers serialise them as a JSON string)."""
    normalised = dict(call)
    args = normalised.get("args")
    if isinstance(args, str):
        try:
            normalised["args"] = json.loads(args)
        except json.JSONDecodeError:
            normalised["args"] = {}
    elif args is None:
        normalised["args"] = {}
    return normalised

class Casper:
    """A class for the RAG chatbot."""
    
    def __init__(self, config: Dict[str, Any]):
        """Initialize the RAG chatbot with configuration parameters.
        
        Args:
            config (Dict[str, Any]): Configuration dictionary containing:
                - user_name (str): Name of the user
                - chat_id (str): Unique chat identifier
                - user_plan (str): User's subscription plan
                - upload_file_config (dict, optional): OpenAI file search config (file_ids, vector_store_id, etc.).
                  Required as fallback when FILE_SEARCH_MODE=grep and grep_agent_2 fails.
                - grep_session (PdfSession, optional): Pre-built grep_agent_2 PdfSession for document search
                  (FILE_SEARCH_MODE=grep only). Created once externally and reused across calls.
                - file_search_mode (str, optional): "grep" or "openai". Pass "openai" on later messages in the
                  same chat after a grep fallback so grep is not retried (new Casper is created per message).
                - web_search (bool, optional): If True, forces live information retrieval for all queries.
                                               If False (default), LLM intelligently decides when to retrieve latest info.
                - user_previous_messages (List): List of previous conversation messages
        
        Example:
            # Force live info retrieval mode (always retrieve latest info):
            config = {
                'user_name': 'John',
                'chat_id': '123',
                'user_plan': 'premium',
                'web_search': True,  # Forces live info retrieval for all queries
                'user_previous_messages': []
            }
        
            # Intelligent mode (LLM decides when to retrieve latest info):
            config = {
                'user_name': 'John',
                'chat_id': '123',
                'user_plan': 'premium',
                'web_search': True,  # or omit this parameter
                'user_previous_messages': []
            }
        """
        logger.info(f"Initializing Casper with user: {config['user_name']} - chat_id: {config['chat_id']} with {len(config['user_previous_messages'])} previous messages")
        
        # Initialize instance variables
        self.user_name = config['user_name']
        self.chat_id = config['chat_id']
        self.openai_upload_file_config = config.get("upload_file_config")
        persisted_mode = (config.get("file_search_mode") or "").lower().strip()

        # Persisted "openai" skips grep for the rest of the chat (API passes this after a prior fallback).
        if persisted_mode == "openai":
            self.file_search_mode = "openai"
            self.upload_file_config = self.openai_upload_file_config
            self.grep_session: Optional[PdfSession] = None
        elif use_grep_file_search():
            self.file_search_mode = "grep"
            self.upload_file_config = None
            self.grep_session: Optional[PdfSession] = config.get("grep_session")
            logger.info(
                f"[grep_agent_2] Received PdfSession from config: {bool(self.grep_session)} | "
                f"user: {config['user_name']} - chat_id: {config['chat_id']}"
            )
        else:
            self.file_search_mode = "openai"
            self.upload_file_config = self.openai_upload_file_config
            self.grep_session: Optional[PdfSession] = None
        self.web_search = config.get('web_search', True)  # Optional: web_search flag to force live info retrieval
        self.domain_name = config.get('domain_name', 'default')  # Set dynamically by retrieve() tool, can be overridden in config
        self.domain_id = config.get('domain_id', 'default')  # Set dynamically by retrieve() tool, can be overridden in config
        self.mcp_server_url = config.get('mcp_server_url', os.environ.get('DD_MCP_SERVER_URL', ''))
        # self.chat_messages = []
        self.retrieve_config = {}
        self.user_id = config.get('user_id')  # User ID for balance checks

        self.mcp_server_url = MCP_URL

        # Get secrets
        # self.secret = self._get_secret()
        
        # Configure APIs
        self._configure_apis()
        
        # Initialize components
        self._initialize_components()

        # Initialize S3 instance
        self.s3_instance = get_s3_instance()

        self.prioritize_arxiv = PRIORITIZE_ARXIV
        
        # Setup conversation
        self.user_previous_messages = config['user_previous_messages']
        # self.graph = self._build_graph()
        
        logger.info(
            f"Initialized Casper | user: {self.user_name} - chat_id: {self.chat_id} | "
            f"previous_messages={len(self.user_previous_messages)} | "
            f"domain_name='{self.domain_name}' | web_search={self.web_search} | "
            f"has_upload_config={bool(self.upload_file_config)} | "
            f"file_search_mode={self.file_search_mode}"
        )

    def _uses_grep_file_search(self) -> bool:
        return self.file_search_mode == "grep"

    def _fallback_to_openai_file_search(self) -> bool:
        """Switch from grep to OpenAI file search after a grep_agent failure."""
        openai_config = self.openai_upload_file_config
        file_ids = (openai_config or {}).get("file_ids")
        if not openai_config or not file_ids:
            logger.error(
                f"Cannot fall back to OpenAI file search: missing upload_file_config "
                f"for user: {self.user_name} - chat_id: {self.chat_id}"
            )
            return False
        self.file_search_mode = "openai"
        self.upload_file_config = openai_config
        logger.warning(
            f"grep_agent failed; switched file_search_mode to openai for "
            f"user: {self.user_name} - chat_id: {self.chat_id}"
        )
        return True

    @staticmethod
    def _grep_result_failed(result: dict, answer: str) -> bool:
        if not answer or not answer.strip():
            return True
        lowered = answer.strip().lower()
        return (
            lowered.startswith("error processing documents")
            or lowered.startswith("no pdf paths provided")
            or "error processing documents:" in lowered
        )

    def _has_uploaded_documents(self) -> bool:
        """True when the session has documents for query_document (mode-specific)."""
        if self._uses_grep_file_search():
            return self.grep_session is not None
        config = self.upload_file_config
        if not config or not isinstance(config, dict):
            return False
        file_ids = config.get("file_ids")
        return bool(file_ids) and file_ids not in ("", "null", "None")
    
    async def async_init(self):
        self.graph = await self._build_graph()

    # def _get_secret(self) -> Dict[str, Any]:
    #     """Get secrets from AWS Secrets Manager"""        
    #     try:
    #         get_secret_value_response = SECRETS_CLIENT.get_secret_value(
    #             SecretId=SECRETS_NAME
    #         )
    #         secret = get_secret_value_response['SecretString']
    #         json_secret = json.loads(secret)
    #         return json_secret
    #     except Exception as e:
    #         logger.error(f"Error getting secret: {e} for user: {self.user_name} - chat_id: {self.chat_id}")
    #         return {}
    
    def _configure_apis(self) -> None:  # Added: New method to configure APIs
        """Configure external APIs"""
        try:
            # pai_key = self.secret.get('PAI_API_KEY')
            # tavily_key = self.secret.get('TAVILY_API_KEY')
            # anthropic_key = self.secret('ANTHROPIC_API_KEY')
            anthropic_key = os.getenv('ANTHROPIC_API_KEY')
            # anthropic_key = os.getenv('ANTHROPIC_API_KEY')
            # if pai_key:
            #     pai.api_key.set(pai_key)
            #     logger.info(f"PAI_API_KEY configured successfully: {pai_key}")
            # else:
            #     logger.warning("PAI_API_KEY not found in secrets")
                
            # if tavily_key:
            #     os.environ["TAVILY_API_KEY"] = tavily_key
            #     logger.info(f"TAVILY_API_KEY configured successfully: ...{tavily_key[-10:]} for user: {self.user_name} - chat_id: {self.chat_id}")
            # else:
            #     logger.warning(f"TAVILY_API_KEY not found in secrets for user: {self.user_name} - chat_id: {self.chat_id}")
                                
            if anthropic_key:
                os.environ["ANTHROPIC_API_KEY"] = anthropic_key
                logger.info(f"ANTHROPIC_API_KEY configured successfully: ...{anthropic_key[-10:]} for user: {self.user_name} - chat_id: {self.chat_id}")
            else:
                logger.warning(f"ANTHROPIC_API_KEY not found in secrets for user: {self.user_name} - chat_id: {self.chat_id}")
                
        except Exception as e:
            logger.error(f"Error configuring APIs: {e} for user: {self.user_name} - chat_id: {self.chat_id}")
    
    def _initialize_components(self) -> None:  # Added: New method to initialize components
        """Initialize Tavily search, vector DB, LLM and system message"""
        try:
            # Initialize Tavily search
            # self.tavily_search_client = TavilySearch(
            #     max_results=10,
            #     topic="general",
            #     include_answer=True,
            #     include_raw_content=True,
            #     search_depth="advanced",
            #     # include_domains=INCLUDE_DOMAINS,
            # )
            
            # Initialize other components
            # self.vector_db = VECTOR_DB
            # self.anthropic_report_llm = ChatAnthropic(
            #     model=ANTHROPIC_MODEL_ID,
            #     temperature=0,
            #     max_tokens=128000,
            #     timeout=None,
            #     streaming=False,
            #     max_retries=5,
            #     betas=["output-128k-2025-02-19"],
            #     disable_streaming=True
            # )
            self.anthropic_llm = ChatAnthropic(
                api_key=ANTHROPIC_API_KEY,
                model=ANTHROPIC_MODEL_ID,
                timeout=None,
                streaming=True,
                max_retries=5,
                **ANTHROPIC_OUTPUT_CONFIG,
            )

            # Prepare system message with file_id info if available
            system_msg_content = SYSTEM_MESSAGE.format(
                user_name=self.user_name,
                date_today=datetime.datetime.now(datetime.timezone.utc).strftime('%B %d, %Y'),
                report_generation_cost=REPORT_GENERATION_COST
            )

            if self.web_search:
                logger.info(f"Live information retrieval mode enabled for user: {self.user_name} - chat_id: {self.chat_id}")
                system_msg_content += "\n\n**IMPORTANT - LEARNING BRAIN ENABLED:**\nInternal capability is ACTIVE: use the 'retrieve_latest_info' tool, which performs web search/current-source retrieval, for user questions that benefit from current, real-time, or up-to-date information. Always prioritize the most current and accurate information available.\n\n**USER-FACING FRAMING:**\nTo the user, this capability is Caspr's Learning Brain. Treat all retrieved information, whether from uploaded documents or current external sources, as information surfaced by the Learning Brain.\n\n**CRITICAL - NEVER REVEAL TOOLS OR INTERNAL PROCESSES TO THE USER:**\n- NEVER mention tool names like 'retrieve_latest_info', 'query_document', or any internal tool/process.\n- NEVER say 'web search', 'searching the web', 'I'll use my retrieval tool', 'Let me search', 'I'll query the document', 'Using my information retrieval', etc.\n- If you need a user-visible phrase, say 'I'll tap into the Learning Brain' or 'The Learning Brain found...' without referencing tools, searches, or mechanisms.\n\n**REQUIRED TOOL ARGUMENTS - status_message and progress_updates:**\nEvery call to 'retrieve_latest_info' MUST include:\n- status_message: A short, user-facing sentence describing what is being checked RIGHT NOW, e.g. 'Checking Nvidia\\'s latest AI chip updates' or 'Reading analyst views on GPU demand'. NEVER mention tool names, APIs, web_search, or internal details.\n- progress_updates: An ordered list of 3-4 SHORT, specific lines describing how THIS particular search unfolds, e.g. ['Looking for Nvidia\\'s most recent earnings and product news', 'Reading analyst takes on data-center GPU demand', 'Cross-checking figures across multiple sources', 'Pulling the key numbers together']. Each search can take 10-30 seconds; these lines are shown one at a time while the user waits, so make them concrete to THIS query — never generic filler, never mention tools/APIs/internal details.\n\n**CRITICAL - NEVER INCLUDE SOURCES:**\nDo NOT include any source URLs, 'Sources:' section, or 'References:' section in your response. Never list or mention URLs or source links to the user."
            
            if self._has_uploaded_documents():
                file_metadata = (self.upload_file_config or {}).get('file_metadata', [])
                if not file_metadata and self.openai_upload_file_config:
                    file_metadata = self.openai_upload_file_config.get('file_metadata', [])
                if self._uses_grep_file_search():
                    doc_ref = [s.labels for s in self.grep_session.worker_sessions] if self.grep_session else []
                    # In grep mode upload_file_config is None so file_metadata is empty above.
                    # Build it from grep_session.labels so the system prompt correctly names
                    # every uploaded document (e.g. "2 documents: 'file1.pdf', 'file2.html'").
                    if not file_metadata and self.grep_session:
                        file_metadata = [{"filename": label} for label in self.grep_session.labels]
                else:
                    doc_ref = (self.upload_file_config or {}).get('file_ids')
                logger.info(
                    f"Adding file upload info to system message | "
                    f"file_search_mode={self.file_search_mode} | "
                    f"doc_ref={doc_ref}, "
                    f"file_count: {len(file_metadata)}, filenames: {[f.get('filename') for f in file_metadata]} "
                    f"for user: {self.user_name} - chat_id: {self.chat_id}"
                )
                system_msg_content += f"{get_system_message_with_documents(file_metadata)}"
                system_msg_content += (
                    "\n\n**REQUIRED TOOL ARGUMENTS FOR query_document - status_message and progress_updates:**\n"
                    "Every call to 'query_document' MUST include:\n"
                    "- status_message: A short, user-facing sentence describing what is being read RIGHT NOW, "
                    "e.g. 'Reading through your uploaded document' or 'Looking up the relevant section in your file'. "
                    "NEVER mention tool names or internal details.\n"
                    "- progress_updates: An ordered list of EXACTLY 5 SHORT, specific lines describing how this document "
                    "lookup unfolds, e.g. ['Scanning the document for relevant sections', "
                    "'Reading the matching passages carefully', "
                    "'Cross-checking across sections', 'Connecting the findings together', "
                    "'Pulling the key details together']. Always provide all 5 so a longer read stays well-narrated. "
                    "Make them concrete to THIS query — never generic filler, never mention tools or internal details."
                )
            else:
                logger.info(f"No file_id found, not adding file upload info to system message for user: {self.user_name} - chat_id: {self.chat_id}")
                system_msg_content += "\n\n**IMPORTANT - NO DOCUMENT UPLOADED:**\nThe user has not uploaded any document. Do NOT proactively ask or suggest the user to upload a document — UNLESS the user selects 'Primary Research' as their report type. Primary Research REQUIRES an uploaded document (surveys, interviews, datasets, etc.). If the user chooses Primary Research without uploading a document, you MUST ask them to upload their research data before proceeding. You MUST NOT generate a Primary Research report without an uploaded document. For all other report types, only mention document upload if the user explicitly asks about uploading or referencing a document."
            
            self.system_message = SystemMessage(content=system_msg_content)
            
        except Exception as e:
            logger.error(f"Error initializing components: {e} for user: {self.user_name} - chat_id: {self.chat_id}")
            raise
        
    # async def _get_search_queries(self) -> None:
    #     """Generate search queries based on user instructions, report layout and report language."""
    #     logger.info(f"Generating search queries for user: {self.user_name} - chat_id: {self.chat_id}")
    #     report_layout = self.retrieve_config.get('report_layout', '')
    #     user_instructions = self.retrieve_config.get('user_instructions', '')
        
    #     try:
    #         subquery_prompt = f"""Given the main query and report layout below, generate focused search queries.
    #         Each query should be specific and maintain context from the main query.

    #         Main Query: {user_instructions}

    #         Report Layout:
    #         {report_layout}

    #         Report Length: {self.retrieve_config.get('report_length', 'brief')}
    #         Word Count Guidelines:
    #         - overview: create maximum 10 queries only for the entire report
    #         - brief: create atleast 10-20 queries only for the entire report
    #         - comprehensive: create atleast 20 or more queries only for the entire report

    #         Generate a list of search queries, one per topic.
    #         Each query should:
    #         1. Be specific to the topic
    #         2. Maintain context from the main query
    #         3. Include relevant keywords
    #         4. Be optimized for search
    #         5. Consider depth based on report length - more detailed queries for comprehensive reports

    #         Return only the list of queries, one per line."""

    #         structured_llm = LLM.with_structured_output(SearchQueries)
    #         response = await structured_llm.ainvoke(subquery_prompt)

    #         subqueries = response.queries
            
    #         self.retrieve_config['search_queries'] = subqueries

    #         logger.info(f"Generated {len(subqueries)} search queries for user: {self.user_name} - chat_id: {self.chat_id}")
    #         # logger.info(f"Search queries: {subqueries}")
    #         return subqueries
    
    #     except Exception as e:
    #         logger.error(f"Going to exception for generating search queries: {e} for user: {self.user_name} - chat_id: {self.chat_id}")
    #         # # Get the report layout from retrieve_config
    #         # report_layout = self.retrieve_config.get('report_layout', '')
    #         # user_instructions = self.retrieve_config.get('user_instructions', '')
    #         try:
    #             if not report_layout or not user_instructions:  # Added: Input validation
    #                 logger.warning(f"Missing report_layout or user_instructions for user: {self.user_name} - chat_id: {self.chat_id}")
    #                 self.retrieve_config['search_queries'] = []
    #                 return
                
    #             topics = []
    #             for line in report_layout.split('\n'):
    #                 line = line.strip()
    #                 if line.startswith('# '):
    #                     topics.append(line.replace('# ', '').strip())
    #                 elif line.startswith('## '):  # Main sections
    #                     topics.append(line.replace('## ', '').strip())
    #                 elif line.startswith('### '):  # Subsections
    #                     topics.append(line.replace('### ', '').strip())
                
    #             if not topics:  # Added: Handle case where no topics found
    #                 logger.warning(f"No topics extracted from report layout for user: {self.user_name} - chat_id: {self.chat_id}")
    #                 self.retrieve_config['search_queries'] = [user_instructions]
    #                 return
                
    #             # Create contextual subqueries for each topic
    #             subquery_prompt = f"""
    #             Given the main query: "{user_instructions}"
    #             And the following topics:
    #             {topics}
                
    #             Create a detailed search query for each topic that:
    #             1. Maintains context from the main query
    #             2. Is specific to the topic
    #             3. Includes relevant keywords
    #             4. Is optimized for search
                
    #             Format each query to be specific and detailed.
    #             Return only the list of queries, one per line.
    #             """
            
    #             response = await LLM.ainvoke(subquery_prompt)
    #             subqueries = []
                
    #             # Extract content from response
    #             if hasattr(response, 'content'):
    #                 if isinstance(response.content, list) and response.content and isinstance(response.content[0], dict):
    #                     content = response.content[0].get('text', '')
    #                 else:
    #                     content = response.content
    #             else:
    #                 content = str(response)
                    
    #             subqueries = [line.strip() for line in content.split('\n') if line.strip()]
                
    #             # Validation and fallback
    #             if not subqueries:  # Added: Fallback if no subqueries generated
    #                 logger.warning(f"No subqueries generated, using original user instructions for user: {self.user_name} - chat_id: {self.chat_id}")
    #                 subqueries = [user_instructions]
                
    #             self.retrieve_config['search_queries'] = subqueries
    #             logger.info(f'Generated {len(subqueries)} search queries for user: {self.user_name} - chat_id: {self.chat_id}')
                
    #         except Exception as e:
    #             logger.error(f"Error creating search queries: {e} for user: {self.user_name} - chat_id: {self.chat_id}")
    #             # Fallback to user instructions
    #             self.retrieve_config['search_queries'] = [self.retrieve_config.get('user_instructions', '')]


    # async def _tavily_internet_search(self) -> None:
    #         """Search the internet using Tavily for relevant information."""
    #         try:
    #             logger.info(f"Searching the internet using Tavily for relevant information for user: {self.user_name} - chat_id: {self.chat_id}")
    #             serialized = "----- Web Search Results -----\n\n"
    #             retrieved_docs = []
    #             tavily_results = []
                
    #             tavily_threshold = 0.7

    #             search_queries = self.retrieve_config.get('search_queries', [])
    #             if not search_queries:
    #                 logger.warning(f"No search queries found for user: {self.user_name} - chat_id: {self.chat_id}")
    #                 self.retrieve_config['web_serialized'] = "No relevant documents found on the internet.\n\n"
    #                 self.retrieve_config['web_retrieved_docs'] = []
    #                 return
                
    #             tavily_result_tasks = await asyncio.gather(*[
    #                 self.tavily_search_client.ainvoke(search_query)
    #                 for search_query in search_queries
    #             ], return_exceptions=True)

    #             tavily_results = []

    #             # logg errors
    #             for search_query, tavily_result in zip(search_queries, tavily_result_tasks):
    #                 if isinstance(tavily_result, dict) and "error" in tavily_result or isinstance(tavily_result, Exception):  # Added: Type check
    #                     logger.error(f"Error searching internet with Tavily for {search_query}: {tavily_result['error'] if isinstance(tavily_result, dict) else tavily_result} for user: {self.user_name} - chat_id: {self.chat_id}")
    #                 else:
    #                     tavily_results.append(tavily_result)

    #             # for search_query in search_queries:
    #             #     try:
    #             #         logger.info(f"Searching internet with Tavily for: '{search_query}'")
    #             #         tavily_result = self.tavily_search_client.invoke(search_query)
                        
    #             #         if isinstance(tavily_result, dict) and "error" in tavily_result:  # Added: Type check
    #             #             logger.error(f"Error searching internet with Tavily: {tavily_result['error']}")
    #             #             continue
                            
    #             #         tavily_results.append(tavily_result)

    #             #     except Exception as e:
    #             #         logger.error(f"Error searching internet with Tavily: {e}")
    #             #         continue

    #             if not tavily_results:
    #                 self.retrieve_config['web_retrieved_docs'] = []
    #                 self.retrieve_config['web_serialized'] = "No relevant documents found on the internet.\n\n"
    #                 return

    #             # Process Tavily results
    #             for tavily_result in tavily_results:
    #                 if not isinstance(tavily_result, dict):  # Added: Type validation
    #                     logger.warning(f"Invalid tavily result format: {type(tavily_result)} - {tavily_result} for user: {self.user_name} - chat_id: {self.chat_id}")
    #                     continue

    #                 if tavily_result.get('query'):
    #                     serialized += f"Search query: {tavily_result['query']}\n"

    #                 if tavily_result.get("answer"):
    #                     serialized += f"Web Search Answer: {tavily_result['answer']}\n"

    #                 if tavily_result.get("results"):
    #                     # Filter results based on score threshold
    #                     filtered_results = [result for result in tavily_result['results'] if result.get('score', 0) > tavily_threshold]
                        
    #                     if not filtered_results:
    #                         logger.warning(f"No results above score threshold {tavily_threshold} for query: {tavily_result.get('query', 'Unknown')} for user: {self.user_name} - chat_id: {self.chat_id}")
    #                         continue
                        
    #                     for idx, result in enumerate(filtered_results):
    #                         # Create document with safe access to result fields
    #                         doc = Document(
    #                             page_content=result.get("content", ""),
    #                             metadata={
    #                                 "url": result.get("url", ""),
    #                                 "title": result.get("title", ""),
    #                                 "source": "Web Search",
    #                             }
    #                         )
    #                         retrieved_docs.append(doc)
                            
    #                         serialized += (f"Source {idx+1}: {result.get('url', '')}\n"
    #                                         f"Title: {result.get('title', '')}\n"
    #                                         f"Content: {result.get('content', '')}\n")
                            
    #                 else:
    #                     logger.warning(f"No results found from Tavily for query: {tavily_result.get('query', 'Unknown')} for user: {self.user_name} - chat_id: {self.chat_id}")
                    
    #                 serialized += "--------------\n\n"

    #             self.retrieve_config['web_serialized'] = serialized
    #             self.retrieve_config['web_retrieved_docs'] = retrieved_docs
    #             logger.info(f'Retrieved {len(retrieved_docs)} web docs for user: {self.user_name} - chat_id: {self.chat_id}')

    #         except Exception as e:
    #             logger.error(f"Error searching the internet using Tavily: {e} for user: {self.user_name} - chat_id: {self.chat_id}")
    #             self.retrieve_config['web_serialized'] = "Error searching the internet using Tavily.\n\n"
    #             self.retrieve_config['web_retrieved_docs'] = []

    # async def _knowledge_base_search(self, k_docs: int = 4, similarity_threshold: float = 0.6) -> None:
    #     """Search the knowledge base for relevant documents."""
    #     try:
    #         logger.info(f"Searching the knowledge base for relevant documents for user: {self.user_name} - chat_id: {self.chat_id}")

    #         search_queries = self.retrieve_config.get('search_queries', [])
    #         if not search_queries:
    #             logger.warning(f"No search queries found for user: {self.user_name} - chat_id: {self.chat_id}")
    #             self.retrieve_config['kb_serialized'] = "No relevant documents found in the knowledge base.\n\n"
    #             self.retrieve_config['kb_retrieved_docs'] = []
    #             return
            
    #         retrieved_docs = []

    #         docs_and_scores_tasks = await asyncio.gather(*[
    #             self.vector_db.asimilarity_search_with_score(search_query, k=k_docs)
    #             for search_query in search_queries
    #         ], return_exceptions=True)

    #         for search_query, docs_and_scores in zip(search_queries, docs_and_scores_tasks):
    #             if isinstance(docs_and_scores, Exception):
    #                 logger.error(f"Error searching knowledge base for {search_query}: {docs_and_scores} for user: {self.user_name} - chat_id: {self.chat_id}")
    #                 continue
    #             for doc, score in docs_and_scores:
    #                 if score >= similarity_threshold:
    #                     retrieved_docs.append(doc)

    #         # for search_query in search_queries:
    #         #     try:
    #         #         logger.info(f"Searching knowledge base for: {search_query}")
    #         #         docs_and_scores = self.vector_db.similarity_search_with_score(search_query, k=k_docs)

    #         #         for doc, score in docs_and_scores:
    #         #             if score >= similarity_threshold:
    #         #                 retrieved_docs.append(doc)
                
    #         #     except Exception as e:
    #         #         logger.error(f"Error searching knowledge base: {e}")
    #         #         continue

    #         serialized = "\n\n----- Knowledge Base Results -----\n\n"

    #         if not retrieved_docs:
    #             logger.warning(f"No relevant documents found in the knowledge base for user: {self.user_name} - chat_id: {self.chat_id}")
    #             self.retrieve_config['kb_serialized'] = serialized + "No relevant documents found in the knowledge base.\n\n"
    #             self.retrieve_config['kb_retrieved_docs'] = []
    #             return
            
    #         logger.info(f"Found {len(retrieved_docs)} relevant documents in knowledge base for user: {self.user_name} - chat_id: {self.chat_id}")

    #         # Remove duplicates based on chunk_id
    #         unique_article_chunk_ids = set()
    #         unique_pdf_chunk_ids = set()
    #         unique_article_chunks = []
    #         unique_pdf_chunks = []

    #         for doc in retrieved_docs:
    #             metadata = doc.metadata   # Added: Safety check for metadata
    #             file_type = metadata.get('file_type', '')
    #             chunk_id = metadata.get('chunk_id')
                
    #             if file_type == 'article':
    #                 if chunk_id not in unique_article_chunk_ids:
    #                     unique_article_chunk_ids.add(chunk_id)
    #                     unique_article_chunks.append(doc)
    #             elif file_type == 'pdf':
    #                 if chunk_id not in unique_pdf_chunk_ids:
    #                     unique_pdf_chunk_ids.add(chunk_id)
    #                     unique_pdf_chunks.append(doc)
                
    #         unique_docs = unique_article_chunks + unique_pdf_chunks

    #         if not unique_docs:
    #             logger.warning(f"No unique documents found in the knowledge base for user: {self.user_name} - chat_id: {self.chat_id}")
    #             self.retrieve_config['kb_serialized'] = serialized + "No relevant documents found in the knowledge base.\n\n"
    #             self.retrieve_config['kb_retrieved_docs'] = []
    #             return
            
    #         logger.info(f"Found {len(unique_docs)} unique relevant documents in knowledge base for user: {self.user_name} - chat_id: {self.chat_id}")
            
    #         # Serialize documents
    #         try:
    #             for idx, doc in enumerate(unique_docs):
    #                 serialized += f"Document {idx+1}:\n"
    #                 metadata = doc.metadata
                    
    #                 if metadata.get('file_type') == 'article':
    #                     serialized += (f"Source: {metadata.get('title', 'Unknown')}\n" 
    #                                     f"Content: {doc.page_content}\n\n")
    #                 elif metadata.get('file_type') == 'pdf':
    #                     serialized += (f"Source: {metadata.get('source', 'Unknown')}\n" 
    #                                     f"Content: {doc.page_content}\n\n")
    #                 else:
    #                     # Handle other file types
    #                     serialized += (f"Source: {metadata.get('source', metadata.get('title', 'Unknown'))}\n" 
    #                                     f"Content: {doc.page_content}\n\n")
    #         except Exception as e:
    #             logger.error(f"Error serializing knowledge base documents: {e} for user: {self.user_name} - chat_id: {self.chat_id}")
    #             self.retrieve_config['kb_serialized'] = serialized + "Error retrieving knowledge base documents.\n\n"
    #             self.retrieve_config['kb_retrieved_docs'] = []
    #             return

    #         self.retrieve_config['kb_serialized'] = serialized
    #         self.retrieve_config['kb_retrieved_docs'] = unique_docs
    #         logger.info(f'Retrieved {len(unique_docs)} kb docs for user: {self.user_name} - chat_id: {self.chat_id}')

    #     except Exception as e:
    #         logger.error(f"Error searching the knowledge base: {e} for user: {self.user_name} - chat_id: {self.chat_id}")
    #         self.retrieve_config['kb_serialized'] = "Error searching the knowledge base.\n\n"
    #         self.retrieve_config['kb_retrieved_docs'] = []
    async def _upload_report_layout_to_s3_background(self, report_layout: str, filename: str) -> None:
        """Background task to upload report layout to S3.
        
        Args:
            report_layout (str): The report layout content to upload
            filename (str): The filename to use for the upload
        """
        try:
            now = datetime.datetime.now()
            year = now.strftime('%Y')
            month = now.strftime('%m')
            day = now.strftime('%d')
            
            # Create temporary file
            with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False, encoding='utf-8') as tmp_file:
                tmp_file.write(report_layout)
                temp_path = tmp_file.name
            
            # Build S3 key
            s3_key = f"{S3_REPORTS_BASE_PATH}/caspr_metadata_reports/{year}/{month}/{day}/{self.user_name}/chat_{self.chat_id}/Report_layout/{filename}"
            
            try:
                logger.info(f"Uploading report layout to S3: {s3_key}")
                s3_path = await asyncio.to_thread(self.s3_instance.upload_file, temp_path, s3_key)
                if s3_path:
                    logger.info(f"Uploaded report layout to S3: {s3_path}")
                else:
                    logger.error(f"Failed to upload report layout to S3")
            except Exception as e:
                logger.error(f"Error uploading report layout to S3: {e}")
            finally:
                try:
                    os.unlink(temp_path)
                except Exception as e:
                    logger.warning(f"Failed to cleanup temporary report layout file: {e}")
        except Exception as e:
            logger.error(f"Error in background task for uploading report layout: {e}")

    async def _upload_descriptive_layout_to_s3_background(self, descriptive_layout: dict, filename: str) -> None:
        """Background task to upload descriptive report layout to S3.
        
        Args:
            descriptive_layout (dict): The descriptive layout content to upload
            filename (str): The filename to use for the upload
        """
        try:
            now = datetime.datetime.now()
            year = now.strftime('%Y')
            month = now.strftime('%m')
            day = now.strftime('%d')
            
            # Create temporary file
            with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False, encoding='utf-8') as tmp_file:
                json.dump(descriptive_layout, tmp_file, indent=2, ensure_ascii=False)
                temp_path = tmp_file.name
            
            # Build S3 key
            s3_key = f"{S3_REPORTS_BASE_PATH}/caspr_metadata_reports/{year}/{month}/{day}/{self.user_name}/chat_{self.chat_id}/Descriptive_report_layout/{filename}"
            
            try:
                logger.info(f"Uploading descriptive layout to S3: {s3_key}")
                s3_path = await asyncio.to_thread(self.s3_instance.upload_file, temp_path, s3_key)
                if s3_path:
                    logger.info(f"Uploaded descriptive layout to S3: {s3_path}")
                else:
                    logger.error(f"Failed to upload descriptive layout to S3")
            except Exception as e:
                logger.error(f"Error uploading descriptive layout to S3: {e}")
            finally:
                try:
                    os.unlink(temp_path)
                except Exception as e:
                    logger.warning(f"Failed to cleanup temporary descriptive layout file: {e}")
        except Exception as e:
            logger.error(f"Error in background task for uploading descriptive layout: {e}")

    async def _upload_card_to_s3_background(self, card: dict, card_name: str) -> None:
        """Background task to upload individual card to S3.
        
        Args:
            card (dict): The card content to upload
            card_name (str): The name of the card section
        """
        try:
            with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False, encoding='utf-8') as tmp_file:
                json.dump(card, tmp_file, indent=2, ensure_ascii=False)
                temp_path = tmp_file.name
            
            now = datetime.datetime.now()
            year = now.strftime('%Y')
            month = now.strftime('%m')
            day = now.strftime('%d')
            s3_key = f"{S3_REPORTS_BASE_PATH}/caspr_metadata_reports/{year}/{month}/{day}/{self.user_name}/chat_{self.chat_id}/Cards/{card_name}.json"
            
            try:
                await asyncio.to_thread(self.s3_instance.upload_file, temp_path, s3_key)
                logger.info(f"Uploaded card to S3: {s3_key}")
            except Exception as e:
                logger.error(f"Error uploading card to S3: {e}")
            finally:
                try:
                    os.unlink(temp_path)
                except Exception as e:
                    logger.warning(f"Failed to cleanup temporary card file: {e}")
        except Exception as e:
            logger.error(f"Error in background task for uploading card: {e}")

    async def _upload_fixed_card_to_s3_background(self, card: dict, card_name: str) -> None:
        """Background task to upload LLM-fixed card to S3."""
        try:
            with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False, encoding='utf-8') as tmp_file:
                json.dump(card, tmp_file, indent=2, ensure_ascii=False)
                temp_path = tmp_file.name

            now = datetime.datetime.now()
            year = now.strftime('%Y')
            month = now.strftime('%m')
            day = now.strftime('%d')
            s3_key = f"{S3_REPORTS_BASE_PATH}/caspr_metadata_reports/{year}/{month}/{day}/{self.user_name}/chat_{self.chat_id}/Cards/LLM_Fixed_Cards/{card_name}.json"

            try:
                await asyncio.to_thread(self.s3_instance.upload_file, temp_path, s3_key)
                logger.info(f"Uploaded fixed card to S3: {s3_key}")
            except Exception as e:
                logger.error(f"Error uploading fixed card to S3: {e}")
            finally:
                try:
                    os.unlink(temp_path)
                except Exception as e:
                    logger.warning(f"Failed to cleanup temporary fixed card file: {e}")
        except Exception as e:
            logger.error(f"Error in background task for uploading fixed card: {e}")

    async def _upload_cards_for_db_to_s3_background(self, cards_for_db: list, table_and_table_id_map: dict) -> None:
        """Background task to upload cards_for_db to S3.
        
        Args:
            cards_for_db (list): The cards for database
            table_and_table_id_map (dict): Table and table ID mapping
        """
        cards_for_db_file_path = None
        try:
            now = datetime.datetime.now()
            year = now.strftime('%Y')
            month = now.strftime('%m')
            day = now.strftime('%d')
            
            cards_for_db_file_name = f"{self.user_name}_chat_{self.chat_id}_cards_for_db.json"
            cards_for_db_file_path = os.path.join(os.getcwd(), "temp", cards_for_db_file_name)
            cards_for_db_with_table_and_table_id_map = cards_for_db + [table_and_table_id_map]
            os.makedirs(os.path.dirname(cards_for_db_file_path), exist_ok=True)
            with open(cards_for_db_file_path, "w") as f:
                json.dump(cards_for_db_with_table_and_table_id_map, f, indent=2, ensure_ascii=False)
            s3_key = f"{S3_REPORTS_BASE_PATH}/caspr_metadata_reports/{year}/{month}/{day}/{self.user_name}/chat_{self.chat_id}/Cards/Report_Card_Json/{cards_for_db_file_name}"
            await asyncio.to_thread(self.s3_instance.upload_file, cards_for_db_file_path, s3_key)
            logger.info(f"Cards_for_db uploaded successfully to S3: {s3_key} for user: {self.user_name} - chat_id: {self.chat_id}")
        except Exception as e:
            logger.error(f"Error uploading cards_for_db to S3: {e} for user: {self.user_name} - chat_id: {self.chat_id}")
        finally:
            if cards_for_db_file_path and os.path.exists(cards_for_db_file_path):
                try:
                    os.remove(cards_for_db_file_path)
                    logger.info(f"Cards_for_db file removed successfully for user: {self.user_name} - chat_id: {self.chat_id}")
                except Exception as e:
                    logger.warning(f"Failed to cleanup cards_for_db file: {e}")

    async def _query_document_openai(self, user_query: str) -> str:
        """Query documents via OpenAI vector store file_search."""
        config = self.upload_file_config
        if not config:
            logger.error(
                f"No OpenAI document configuration available for user: {self.user_name} - "
                f"chat_id: {self.chat_id}"
            )
            return GENERIC_TOOL_ERROR_MSG

        file_ids = config.get('file_ids')
        vector_store_id = config.get('vector_store_id')

        if not file_ids:
            logger.error(
                f"No document file_ids available for user: {self.user_name} - chat_id: {self.chat_id}"
            )
            return GENERIC_TOOL_ERROR_MSG

        try:
            for file_id in config['file_ids']:
                await ASYNC_OPENAI_CLIENT.files.retrieve(file_id)
                logger.info(
                    f"File {file_id} verified for user: {self.user_name} - chat_id: {self.chat_id}"
                )
        except Exception as file_check_error:
            logger.error(
                f"OpenAI file check failed: {file_check_error} for user: {self.user_name} - "
                f"chat_id: {self.chat_id}"
            )
            return GENERIC_TOOL_ERROR_MSG

        if not vector_store_id:
            logger.error(
                f"No vector_store_id for OpenAI file search | user: {self.user_name} - "
                f"chat_id: {self.chat_id}"
            )
            return GENERIC_TOOL_ERROR_MSG

        current_date = datetime.datetime.now().strftime('%B %d, %Y')
        logger.info(
            f"Querying document via OpenAI file_search | vector_store={vector_store_id} | "
            f"file_ids={file_ids} | user: {self.user_name} - chat_id: {self.chat_id}"
        )

        prompt = f"""Current Date: {current_date}

User Question: {user_query}

Please analyze the uploaded document to answer the question based on the document content.
Provide specific references to sections or pages if available."""

        filters = {
            "type": "and",
            "filters": [
                {
                    "type": "in",
                    "key": "file_id",
                    "value": config['file_ids'],
                },
            ],
        }

        try:
            response = await ASYNC_OPENAI_CLIENT.responses.create(
                model=QUERY_DOC_MODEL,
                input=prompt,
                tools=[
                    {
                        "type": "file_search",
                        "vector_store_ids": [vector_store_id],
                        "filters": filters,
                    }
                ],
                tool_choice="auto",
            )
            save_raw_llm_response(response, QUERY_DOC_MODEL, "Answering a question from uploaded documents", self.chat_id, user_id=self.user_id or self.user_name)

            answer = response.output_text
            logger.info(
                f"OpenAI file_search query complete for user: {self.user_name} - chat_id: {self.chat_id}"
            )
            return answer
        except Exception as e:
            logger.error(
                f"OpenAI file_search query failed ({e}), falling back to Gemini API | "
                f"user: {self.user_name} - chat_id: {self.chat_id}"
            )
            # Best-effort fallback: Gemini's file_search tool expects a Gemini-native
            # vector store, so this only succeeds if the OpenAI vector_store_id is
            # also reachable from Gemini (e.g. shared/mirrored store). If not, this
            # raises and the caller's own except handles the GENERIC_TOOL_ERROR_MSG.
            gemini_client = genai.Client(api_key=GEMINI_API_KEY)
            interaction = gemini_client.interactions.create(
                model=GEMINI_QUERY_DOC_MODEL,
                input=prompt,
                tools=[
                    {
                        "type": "file_search",
                        "vector_store_ids": [vector_store_id],
                        "filters": filters,
                    }
                ],
            )
            save_raw_llm_response(interaction, GEMINI_QUERY_DOC_MODEL, "Answering a question from uploaded documents (backup)", self.chat_id, user_id=self.user_id or self.user_name)

            answer = interaction.output_text
            logger.info(
                f"Gemini file_search fallback query complete for user: {self.user_name} - chat_id: {self.chat_id}"
            )
            return answer

    async def _query_document_grep(self, user_query: str) -> str:
        """Query documents via grep_agent_2 using the pre-built PdfSession; raises on failure so caller can fall back to OpenAI."""
        if not self.grep_session:
            raise RuntimeError("No PdfSession available for grep search")

        logger.info(
            f"Querying document via grep_agent_2 | "
            f"user: {self.user_name} - chat_id: {self.chat_id}"
        )
        result = await asyncio.to_thread(
            ask_pdfs,
            self.grep_session,
            user_query,
            chat_id=self.chat_id,
            user_id=self.user_id or self.user_name,
        )
        answer = result.get("answer", "")
        if self._grep_result_failed(result, answer):
            raise RuntimeError(f"grep_agent_2 failed: {answer}")

        citations = result.get("citations") or []
        if citations:
            cite_lines = []
            for c in citations:
                doc = c.get("document", "")
                page = c.get("page", "")
                section = c.get("section", "")
                cite_lines.append(f"- {doc} (page {page}, {section})")
            answer = f"{answer}\n\nSources from documents:\n" + "\n".join(cite_lines)

        logger.info(
            f"grep_agent_2 query complete | workers_used={result.get('workers_used')} | "
            f"user: {self.user_name} - chat_id: {self.chat_id}"
        )
        return answer

    async def query_document(
        self,
        user_query: str,
        status_message: str = "",
        progress_updates: Optional[List[str]] = None,
    ) -> str:
        """Query an uploaded document to extract information and answer questions.

        Use this tool when the user has uploaded a document and wants to ask questions about it.
        This tool analyzes only the document content.

        Args:
            user_query (str): The user's question about the document.
            status_message (str): Short user-facing message shown before the query starts,
                e.g. "Reading through your uploaded document".  Never mention tool names or
                internal details.
            progress_updates (List[str]): Ordered list of EXACTLY 5 short, query-specific
                lines describing how this document lookup unfolds.  Shown one at a time
                (about every 4 seconds) while the query is in flight.  Always provide all 5 —
                they should narrate the lookup from start to finish so a longer read stays
                well-narrated.
                Example: ["Scanning the document for relevant sections",
                          "Reading the matching passages carefully",
                          "Checking the supporting details",
                          "Connecting the findings together",
                          "Pulling the key details together"].

        Returns:
            str: The answer to the user's question based on the document content
        """
        try:
            event_writer = get_stream_writer()
            event_writer({
                "name": "query_document",
                "status": "start",
                "message": status_message,
                "progress_updates": progress_updates or [],
            })

            if not self._has_uploaded_documents():
                logger.error(
                    f"No document configuration available for user: {self.user_name} - "
                    f"chat_id: {self.chat_id}"
                )
                return GENERIC_TOOL_ERROR_MSG

            if self._uses_grep_file_search():
                try:
                    started = time.time()
                    heartbeat_task = asyncio.create_task(
                        _run_heartbeat(user_query, progress_updates or [], event_writer, started, tool_name="query_document")
                    )
                    try:
                        answer = await self._query_document_grep(user_query)
                    finally:
                        heartbeat_task.cancel()
                        try:
                            await heartbeat_task
                        except asyncio.CancelledError:
                            pass
                except Exception as grep_err:
                    logger.error(
                        f"grep_agent_2 error: {grep_err} for user: {self.user_name} - "
                        f"chat_id: {self.chat_id}",
                        exc_info=True,
                    )
                    if self._fallback_to_openai_file_search():
                        started = time.time()
                        heartbeat_task = asyncio.create_task(
                            _run_heartbeat(user_query, progress_updates or [], event_writer, started, tool_name="query_document")
                        )
                        try:
                            answer = await self._query_document_openai(user_query)
                        finally:
                            heartbeat_task.cancel()
                            try:
                                await heartbeat_task
                            except asyncio.CancelledError:
                                pass
                    else:
                        return GENERIC_TOOL_ERROR_MSG
            else:
                logger.info(
                    f"Using OpenAI file_search (file_search_mode=openai) for user: "
                    f"{self.user_name} - chat_id: {self.chat_id}"
                )
                started = time.time()
                heartbeat_task = asyncio.create_task(
                    _run_heartbeat(user_query, progress_updates or [], event_writer, started, tool_name="query_document")
                )
                try:
                    answer = await self._query_document_openai(user_query)
                finally:
                    heartbeat_task.cancel()
                    try:
                        await heartbeat_task
                    except asyncio.CancelledError:
                        pass

            event_writer({
                "name": "query_document",
                "status": "end",
                "elapsed": f"{time.time() - started:.1f}s",
            })
            return answer
            
#             else:
#                 # Use traditional file-based approach
#                 logger.info(f"Querying document {file_id} using traditional approach for user: {self.user_name} - chat_id: {self.chat_id}")
                
#                 prompt = f"""Current Date: {current_date}
                
# User Question: {user_query}

# Please analyze the uploaded document to answer the question based on the document content.
# Provide specific references to sections or pages if available."""

                
#                 user_content = [
#                     {
#                         "type": "input_text",
#                         "text": prompt,
#                     },
#                     {
#                         "type": "input_file",
#                         "file_id": file_id
#                     }
#                 ]
#                 response = await ASYNC_OPENAI_CLIENT.responses.parse(
#                     model="gpt-5.2",
#                     input=[
#                         {"role": "system", "content": "You are a helpful assistant that answers questions based on the provided document content."},
#                         {"role": "user", "content": user_content}
#                     ],
#                     text_format=DocumentQueryResponse,
#                     temperature=0.1,
#                 )
                
#                 output = response.model_dump()['output'][-1]
#                 json_output = output['content'][0]['parsed']
#                 answer = json_output['answer']
                
#                 logger.info(f"Successfully queried document using traditional approach for user: {self.user_name} - chat_id: {self.chat_id}")
#                 return answer
            
        except Exception as e:
            logger.error(f"Error querying document: {e} for user: {self.user_name} - chat_id: {self.chat_id}")
            event_writer({"name": "query_document", "status": "end", "elapsed": ""})
            return GENERIC_TOOL_ERROR_MSG
    
    async def retrieve_latest_info(
        self,
        user_query: str,
        status_message: str = "",
        progress_updates: Optional[List[str]] = None,
    ) -> str:
        """
        Retrieve the latest up-to-date information to answer questions accurately.

        Use this tool when the user asks questions that require current, real-time, or up-to-date information.
        This is useful for:
        - Current events and news
        - Latest data, statistics, or trends
        - Real-time information (stock prices, weather, etc.)
        - Questions about recent developments
        - Any information that needs to be current and verified

        Args:
            user_query (str): The user's question that requires up-to-date information.
            status_message (str): Short user-facing message shown before the search starts,
                e.g. "Checking Nvidia's latest AI chip updates".  Never mention tool names,
                APIs, web_search, or internal details.
            progress_updates (List[str]): Ordered list of EXACTLY 5 short, query-specific
                lines describing how this search unfolds.  Shown one at a time (about every
                4 seconds) while the search is in flight so the user sees the assistant's own
                evolving narration.  Always provide all 5 — they should narrate the search
                from start to finish so a longer search stays well-narrated.
                Example: ["Looking for Nvidia's most recent earnings",
                          "Reading analyst takes on GPU demand",
                          "Cross-checking the latest figures",
                          "Comparing against competitor trends",
                          "Pulling the key numbers together"].
                Never generic filler; never mention tools/APIs/internal details.

        Returns:
            str: The answer to the user's question with citations from verified sources
        """
        try:
            event_writer = get_stream_writer()
            event_writer({
                "name": "retrieve_latest_info",
                "status": "start",
                "message": status_message,
                "progress_updates": progress_updates or [],
            })

            search_mode = "forced" if self.web_search else "intelligent"
            logger.info(f"Retrieving latest info ({search_mode} mode) for query: {user_query[:100]}... for user: {self.user_name} - chat_id: {self.chat_id}")
            
            current_date = datetime.datetime.now().strftime('%B %d, %Y')
            
            prompt = f"""Current Date: {current_date}

User Question: {user_query}

Please search the web for the latest and most accurate information to answer this question comprehensively.
Make sure to cite your sources appropriately."""

            started = time.time()
            operation_id = str(uuid7())
            heartbeat_task = asyncio.create_task(
                _run_heartbeat(user_query, progress_updates or [], event_writer, started)
            )

            try:
                request_started = time.perf_counter()
                try:
                    response = await ASYNC_OPENAI_CLIENT.responses.parse(
                        model=RETRIEVE_LATEST_INFO_MODEL,
                        input=[
                            {"role": "system", "content": "You are a helpful assistant that searches the web to provide accurate, up-to-date information with proper citations."},
                            {"role": "user", "content": prompt}
                        ],
                        tools=[{"type": "web_search"}],
                        tool_choice={"type": "web_search"},
                        text_format=DocumentQueryResponse,
                        temperature=0.1,
                        include=["web_search_call.results"],
                    )
                except Exception as provider_exc:
                    try:
                        asyncio.create_task(
                            log_web_search_event(
                                trigger_source="chat",
                                user_query=user_query,
                                candidate_links=[],
                                cited_links=[],
                                operation_id=operation_id,
                                attempt_number=1,
                                provider="openai",
                                model_used=RETRIEVE_LATEST_INFO_MODEL,
                                status="failed",
                                error_type=type(provider_exc).__name__,
                                duration_ms=round((time.perf_counter() - request_started) * 1000),
                                search_call_count=0,
                                user_id=self.user_id,
                                chat_id=self.chat_id,
                            )
                        )
                    except Exception:
                        logger.exception(
                            "[retrieve_latest_info] Failed to schedule non-fatal "
                            "provider failure analytics"
                        )

                    logger.info(f"[retrieve_latest_info] OpenAI web search failed ({provider_exc}), falling back to Gemini | chat_id={self.chat_id}")
                    gemini_request_started = time.perf_counter()
                    try:
                        gemini_client = genai.Client(api_key=GEMINI_API_KEY)
                        interaction = gemini_client.interactions.create(
                            model=GEMINI_RETRIEVE_LATEST_INFO_MODEL,
                            input=prompt,
                            system_instruction="You are a helpful assistant that searches the web to provide accurate, up-to-date information with proper citations.",
                            tools=[{"type": "google_search"}],
                            response_format={
                                "type": "text",
                                "mime_type": "application/json",
                                "schema": DocumentQueryResponse.model_json_schema(),
                            },
                            generation_config={
                                "temperature": 0.1,
                                "thinking_config": {"thinking_budget": 0},
                            },
                        )
                    except Exception as gemini_exc:
                        try:
                            asyncio.create_task(
                                log_web_search_event(
                                    trigger_source="chat",
                                    user_query=user_query,
                                    candidate_links=[],
                                    cited_links=[],
                                    operation_id=operation_id,
                                    attempt_number=2,
                                    provider="gemini",
                                    model_used=GEMINI_RETRIEVE_LATEST_INFO_MODEL,
                                    status="failed",
                                    error_type=type(gemini_exc).__name__,
                                    duration_ms=round((time.perf_counter() - gemini_request_started) * 1000),
                                    search_call_count=0,
                                    user_id=self.user_id,
                                    chat_id=self.chat_id,
                                )
                            )
                        except Exception:
                            logger.exception(
                                "[retrieve_latest_info] Failed to schedule non-fatal "
                                "gemini failure analytics"
                            )
                        # Both providers failed — surface the original OpenAI error
                        # so the outer except returns GENERIC_TOOL_ERROR_MSG.
                        raise provider_exc

                    gemini_request_duration_ms = round((time.perf_counter() - gemini_request_started) * 1000)
                    save_raw_llm_response(interaction, GEMINI_RETRIEVE_LATEST_INFO_MODEL, "retrieve_latest_info (backup)", self.chat_id, user_id=self.user_id)

                    try:
                        gemini_analytics_event = extract_gemini_search_analytics(interaction, model_used=GEMINI_RETRIEVE_LATEST_INFO_MODEL)
                    except Exception:
                        logger.exception(
                            "[retrieve_latest_info] Failed to extract Gemini analytics; "
                            "scheduling minimal successful event"
                        )
                        gemini_analytics_event = {
                            "provider": "gemini",
                            "provider_response_id": getattr(interaction, "id", None),
                            "provider_queries": [],
                            "model_used": GEMINI_RETRIEVE_LATEST_INFO_MODEL,
                            "status": "succeeded",
                            "usage_metadata": None,
                            "search_call_count": 0,
                            "candidate_links": [],
                            "cited_links": [],
                        }
                    gemini_analytics_event.update(
                        {
                            "trigger_source": "chat",
                            "user_query": user_query,
                            "operation_id": operation_id,
                            "attempt_number": 2,
                            "model_used": gemini_analytics_event.get("model_used") or GEMINI_RETRIEVE_LATEST_INFO_MODEL,
                            "status": "succeeded",
                            "duration_ms": gemini_request_duration_ms,
                            "user_id": self.user_id,
                            "chat_id": self.chat_id,
                        }
                    )
                    try:
                        asyncio.create_task(log_web_search_event(**gemini_analytics_event))
                    except Exception:
                        logger.exception(
                            "[retrieve_latest_info] Failed to schedule non-fatal "
                            "gemini success analytics"
                        )

                    gemini_parsed = json.loads(strip_json_code_fence(interaction.output_text))
                    gemini_answer = gemini_parsed.get("answer", "")
                    gemini_citations = [clean_url(u) for u in (gemini_parsed.get("citations") or []) if clean_url(u)]

                    logger.info(
                        f"[retrieve_latest_info] Gemini fallback succeeded with {len(gemini_citations)} "
                        f"citations for user: {self.user_name} - chat_id: {self.chat_id}"
                    )
                    event_writer({
                        "name": "retrieve_latest_info",
                        "status": "end",
                        "citations": gemini_citations,
                        "elapsed": f"{time.time() - started:.1f}s",
                    })
                    if not hasattr(self, '_latest_info_context'):
                        self._latest_info_context = []
                    self._latest_info_context.append(gemini_answer)
                    return gemini_answer
                request_duration_ms = round(
                    (time.perf_counter() - request_started) * 1000
                )
                try:
                    analytics_event = extract_openai_search_analytics(response)
                except Exception:
                    logger.exception(
                        "[retrieve_latest_info] Failed to extract analytics; "
                        "scheduling minimal successful event"
                    )
                    analytics_event = {
                        "provider": "openai",
                        "provider_response_id": getattr(response, "id", None),
                        "provider_queries": [],
                        "model_used": RETRIEVE_LATEST_INFO_MODEL,
                        "status": "succeeded",
                        "usage_metadata": None,
                        "search_call_count": 0,
                        "candidate_links": [],
                        "cited_links": [],
                    }
                analytics_event.update(
                    {
                        "trigger_source": "chat",
                        "user_query": user_query,
                        "operation_id": operation_id,
                        "attempt_number": 1,
                        "model_used": analytics_event.get("model_used") or RETRIEVE_LATEST_INFO_MODEL,
                        "status": "succeeded",
                        "duration_ms": request_duration_ms,
                        "user_id": self.user_id,
                        "chat_id": self.chat_id,
                    }
                )
                try:
                    asyncio.create_task(log_web_search_event(**analytics_event))
                except Exception:
                    logger.exception(
                        "[retrieve_latest_info] Failed to schedule non-fatal "
                        "provider success analytics"
                    )
                save_raw_llm_response(response, RETRIEVE_LATEST_INFO_MODEL, "retrieve_latest_info", self.chat_id, user_id=self.user_id)
            finally:
                heartbeat_task.cancel()
                try:
                    await heartbeat_task
                except asyncio.CancelledError:
                    pass
            
            # Parallel search path kept for reference; OpenAI web_search is the active provider.
            # from parallel import AsyncParallel
            # client = AsyncParallel(api_key=PARALLEL_API_KEY)
            # search = await client.search(
            #     objective="Find latest information about the user's question",
            #     search_queries=[user_query],
            # )
            # results = []
            # results.extend(search.results)
            # return str(results)

            output_items = response.model_dump().get('output', [])

            # Collect citations from two places in the response:
            #   1. web_search_call.results  — rich objects with url/title/snippet
            #   2. message content annotations — inline URL references added by
            #      the model inside its final answer text
            # Both sources are de-duplicated via _seen_urls so no URL is counted twice.
            # raw_search_results retains title + snippet for the existing completion
            # log; citations holds only the cleaned URLs returned to the caller.
            _seen_urls: set = set()
            citations: list = []
            raw_search_results: list = []

            def _add_citation(url: str, title: str = None, snippet: str = None) -> None:
                cleaned = clean_url(url)
                if cleaned and cleaned not in _seen_urls:
                    _seen_urls.add(cleaned)
                    citations.append(cleaned)
                    raw_search_results.append({
                        "url": cleaned,
                        "title": title or None,
                        "snippet": snippet or None,
                    })

            for _item in output_items:
                if _item.get('type') == 'web_search_call':
                    for _result in (_item.get('results') or []):
                        if _result.get('url'):
                            _add_citation(
                                _result['url'],
                                title=_result.get('title'),
                                snippet=_result.get('snippet'),
                            )
                if _item.get('type') == 'message':
                    for _block in (_item.get('content') or []):
                        for _ann in (_block.get('annotations') or []):
                            if 'url' in _ann:
                                _add_citation(_ann['url'])

            logger.info(
                f"[retrieve_latest_info] Web search complete | "
                f"unique_citations={len(raw_search_results)} chat_id={self.chat_id}"
            )

            # Extract the answer from the last message-type output item.
            answer = ""
            for _item in reversed(output_items):
                if _item.get('type') != 'message':
                    continue
                for _block in (_item.get('content') or []):
                    _parsed = _block.get('parsed')
                    if isinstance(_parsed, dict) and _parsed.get('answer'):
                        answer = _parsed['answer']
                        break
                    _text = _block.get('text') or _block.get('output_text')
                    if _text:
                        answer = _text
                        break
                if answer:
                    break

            # Format response with citations
            # if citations:
            #     answer += "\n\n**Sources:**\n"
            #     for idx, url in enumerate(citations, 1):
            #         answer += f"{idx}. {clean_url(url)}\n"
            
            logger.info(f"Successfully retrieved latest info with {len(citations)} citations for user: {self.user_name} - chat_id: {self.chat_id}")
            event_writer({
                "name": "retrieve_latest_info",
                "status": "end",
                "citations": [clean_url(url) for url in citations],
                "elapsed": f"{time.time() - started:.1f}s",
            })
            # Store the retrieved info for use in DRL generation
            if not hasattr(self, '_latest_info_context'):
                self._latest_info_context = []
            self._latest_info_context.append(answer)
            return answer

        except Exception as e:
            logger.error(f"Error retrieving latest info: {e} for user: {self.user_name} - chat_id: {self.chat_id}")
            event_writer({"name": "retrieve_latest_info", "status": "end"})
            return GENERIC_TOOL_ERROR_MSG


    async def retrieve(self, user_instructions: str, 
        report_layout: str, 
        report_language: str,
        report_title: str, 
        domain_name: Literal[
            'default', 
            'primary_research', 
            'due_diligence', 
            'industry_benchmarking', 
            'market_insight', 
            'rfp', 
            'business_plan'
            ],
        report_type: Literal['study', 'brief'] = 'study'
        ) -> Tuple[str, List[Document]]:
        """Retrieve information as per user_instructions from the vector store and verified sources and generate report according to the report_layout
        
        This method is used as a tool by the LLM.

        Args:
            user_instructions (str): Detailed instructions captured from the user about report requirements. Do mention in the user_instructions that the report should be generated in the English only never user any other language.
            report_layout (str): The proposed markdown structure/layout of the report that has been approved by the user. Make sure to always generate report layout in ENglish only no other language.
            report_language (str): The report should be generated in the English only never user any other language.
            report_title (str): The title of the report in 7 words or less.
            domain_name (Literal['default', 'primary_research', 'due_diligence', 'industry_benchmarking', 'market_insight', 'rfp', 'business_plan']): **REQUIRED**. The report domain type. You MUST pass this parameter explicitly on every call — do NOT omit it. Pick the value that matches the report type the user confirmed in the feedback phase. Use 'primary_research' when the user wants a report that analyses their own uploaded research data (surveys, interviews, experiments, datasets) using ONLY the uploaded document as the source. Use 'due_diligence' when the user wants a due diligence investigation report that uses the Learning Brain and external data feeds (financial databases, regulatory filings, news, court records) to investigate and assess an entity. Use 'industry_benchmarking' when the user wants to benchmark companies/industries against competitors or industry standards. Use 'market_insight' when the user wants market intelligence, trends analysis, or market opportunity reports. Use 'rfp' when the user wants to create or respond to a Request for Proposal. Use 'business_plan' when the user wants to generate a business plan or business strategy document. Use 'default' ONLY when the user explicitly selected 'Standard Report' option 7, or when the request genuinely does not fit any specialized domain.
            report_type (Literal['study', 'brief']): The type of report to generate. 'study' is the default full-length detailed research report with comprehensive analysis, citations, and deep coverage of 8-10+ sections. 'brief' generates a shorter, concise research brief with a maximum of 5 sections that summarizes key findings quickly. Default is 'study'. Use 'brief' ONLY when the user explicitly asks for a brief or short summary report.
        """
    # async def retrieve(self, user_instructions: str, report_layout: str, report_language:str ,report_title: str) -> Tuple[str, List[Document]]:
    #     """Retrieve information as per user_instructions from the vector store and web search and generate report according to the report_layout
        
    #     This method is used as a tool by the LLM.

    #     Args:
    #         user_instructions (str): Detailed instructions captured from the user about report requirements.
    #         report_layout (str): The proposed markdown structure/layout of the report that has been approved by the user.
    #         report_language (str): The language in which the report should be generated.
    #         report_title (str): The title of the report in 7 words or less.
    #     """
        try:
            event_writer = get_stream_writer()

            event_writer({"name": "retrieve", "report_title": report_title})

            # Input validation - Added
            if not user_instructions or not report_layout:
                logger.error(f"Missing required parameters: user_instructions or report_layout for user: {self.user_name} - chat_id: {self.chat_id}")
                return GENERIC_TOOL_ERROR_MSG, []

            # Validating report layout
            report_layout_title = report_layout.strip().split()[0]
            now = datetime.datetime.now()
            year = now.strftime('%Y')
            month = now.strftime('%m')
            day = now.strftime('%d')
            
            # Upload original report layout to S3 as a background task
            original_report_layout_filename = f"report_layout_original_{self.user_name}_chat_{self.chat_id}.txt"
            asyncio.create_task(self._upload_report_layout_to_s3_background(report_layout, original_report_layout_filename))
            
            _SPECIALIZED_DOMAINS = {"primary_research", "due_diligence"}

            if report_layout_title.count("#") != 1:
                logger.warning(f"Report layout title must start with exactly one '#' heading for user: {self.user_name} - chat_id: {self.chat_id}")

                report_layout = await asyncio.to_thread(remove_citations_from_RL, report_layout)
                logger.info(f"Removed citations from report layout")
                if domain_name not in _SPECIALIZED_DOMAINS:
                    cleaned_report_layout_filename = f"report_layout_cleaned_{self.user_name}__chat_{self.chat_id}.txt"
                    asyncio.create_task(self._upload_report_layout_to_s3_background(report_layout, cleaned_report_layout_filename))
                report_layout = f"# {report_title}\n\n" + report_layout

                logger.info(f"Refined report layout for user: {self.user_name} - chat_id: {self.chat_id}")
            else:
                logger.info(f"Refining report layout for user: {self.user_name} - chat_id: {self.chat_id}")
                report_layout = await asyncio.to_thread(remove_citations_from_RL, report_layout)
                if domain_name not in _SPECIALIZED_DOMAINS:
                    cleaned_report_layout_filename = f"report_layout_cleaned_{self.user_name}__chat_{self.chat_id}.txt"
                    asyncio.create_task(self._upload_report_layout_to_s3_background(report_layout, cleaned_report_layout_filename))                


            # Store configuration
            self.retrieve_config['user_instructions'] = user_instructions
            self.retrieve_config['report_layout'] = report_layout
            self.retrieve_config['report_length'] = report_type
            # self.retrieve_config['report_language'] = report_language
            self.retrieve_config['report_language'] = "English"
            self.retrieve_config['domain_name'] = domain_name
            self.retrieve_config['report_title'] = report_title
            self.retrieve_config['report_type'] = report_type
            self.domain_name = domain_name

            logger.info(
                f"[retrieve] Domain routing decision: domain_name='{domain_name}' | "
                f"report_title='{report_title}' | "
                f"report_type='{report_type}' | "
                f"report_language='{report_language}' | "
                f"has_files={self._has_uploaded_documents()} | "
                f"web_search={self.web_search} | "
                f"user: {self.user_name} - chat_id: {self.chat_id}"
            )
            logger.info(
                f"[retrieve] Config stored: user_instructions_len={len(user_instructions)} | "
                f"report_layout_len={len(report_layout)} | "
                f"user: {self.user_name} - chat_id: {self.chat_id}"
            )

            event_writer({"name": "retrieve", "domain_name": domain_name})
            event_writer({"name": "retrieve", "report_type": report_type})

            if domain_name in _SPECIALIZED_DOMAINS:
                logger.info(
                    f"[retrieve] Specialized domain detected: domain_name='{domain_name}' — "
                    f"delegating DRL generation and report pipeline to '{domain_name}' subgraph | "
                    f"user: {self.user_name} - chat_id: {self.chat_id}"
                )
                return "", {
                    "report_layout": report_layout,
                    "domain_name": domain_name,
                }
            logger.info(
                f"[retrieve] Domain '{domain_name}' — proceeding with standard DRL generation pipeline | "
                f"user: {self.user_name} - chat_id: {self.chat_id}"
            )

            event_writer({"name": "retrieve", "status": "card_stream_start", "title": report_title})

            logger.info(f"Generating descriptive report layout for user: {self.user_name} - chat_id: {self.chat_id}")
            # Pass any previously retrieved latest info as context for DRL generation
            latest_info_context = "\n\n".join(getattr(self, '_latest_info_context', [])) or None
            if latest_info_context:
                logger.info(f"Passing {len(self._latest_info_context)} retrieve_latest_info results as context to DRL generation for user: {self.user_name} - chat_id: {self.chat_id}")
            descriptive_report_layout = await asyncio.to_thread(
                generate_drl,
                report_layout,
                user_instructions,
                report_type,
                latest_info_context=latest_info_context,
                chat_id=self.chat_id,
                user_id=self.user_id or self.user_name,
            )
            if not descriptive_report_layout:
                logger.error(f"Error generating descriptive report layout for user: {self.user_name} - chat_id: {self.chat_id}")
                return GENERIC_TOOL_ERROR_MSG, {}
            logger.info(f"Loaded {len(descriptive_report_layout)} sections from layout for user: {self.user_name} - chat_id: {self.chat_id}")
            
            original_descriptive_layout_filename = f"descriptive_report_layout_original_{self.user_name}__chat_{self.chat_id}.json"
            asyncio.create_task(self._upload_descriptive_layout_to_s3_background(descriptive_report_layout, original_descriptive_layout_filename))

            logger.info(f"Cleaning descriptive report layout for user: {self.user_name} - chat_id: {self.chat_id}")
            descriptive_report_layout = await asyncio.to_thread(clean_drl, descriptive_report_layout)
            descriptive_report_layout = await asyncio.to_thread(remove_citations_from_DRL, descriptive_report_layout)
            cleaned_descriptive_layout_filename = f"descriptive_report_layout_cleaned_{self.user_name}__chat_{self.chat_id}.json"
            asyncio.create_task(self._upload_descriptive_layout_to_s3_background(descriptive_report_layout, cleaned_descriptive_layout_filename))
            if not descriptive_report_layout:
                logger.error(f"Error cleaning descriptive report layout for user: {self.user_name} - chat_id: {self.chat_id}")
                return GENERIC_TOOL_ERROR_MSG, {}
            logger.info(f"Cleaned descriptive report layout for user: {self.user_name} - chat_id: {self.chat_id}")

            cleaned_report_layout = await asyncio.to_thread(clean_drl_to_clean_rl, descriptive_report_layout)
            if not cleaned_report_layout:
                logger.error(f"Error cleaning descriptive report layout for user: {self.user_name} - chat_id: {self.chat_id}")
                return GENERIC_TOOL_ERROR_MSG, {}
            logger.info(f"Cleaned descriptive report layout for user: {self.user_name} - chat_id: {self.chat_id}")
            cleaned_report_layout = await asyncio.to_thread(modify_report_layout, cleaned_report_layout)
            event_writer({"name": "retrieve", "report_layout": cleaned_report_layout})

            return "", {
                "descriptive_report_layout": descriptive_report_layout,
                "report_layout": report_layout,
                "domain_name": domain_name
            }
        
        except Exception as e:
            event_writer({"name": "retrieve", "error": e})
            logger.error(f"Error in retrieve: {e}")
            return GENERIC_TOOL_ERROR_MSG, {}
        

        # Generate search queries based on user instructions and report layout
        # event_writer({"name": "retrieve", "status": "generating_search_queries"})
        # await self._get_search_queries()

        # # Search the internet using Tavily 
        # event_writer({"name": "retrieve", "status": "searching_internet"})
        # await self._tavily_internet_search()

        # # Search the knowledge base
        # event_writer({"name": "retrieve", "status": "searching_knowledge_base"})
        # await self._knowledge_base_search()

        # Combine the web and knowledge base search results
        # web_serialized = self.retrieve_config.get('web_serialized', '')
        # kb_serialized = self.retrieve_config.get('kb_serialized', '')
        # retrieved_content = web_serialized + kb_serialized
        
        # web_docs = self.retrieve_config.get('web_retrieved_docs', [])
        # kb_docs = self.retrieve_config.get('kb_retrieved_docs', [])
        # all_retrieved_docs = web_docs + kb_docs

        # if not all_retrieved_docs:
        #     logger.error(f"No retrieved documents found for user: {self.user_name} - chat_id: {self.chat_id}")
        #     event_writer({"name": "retrieve", "status": "error"})
        
        # # Store all retrieved docs for later use
        # self.retrieve_config['all_retrieved_docs'] = all_retrieved_docs
        
        # logger.info(f'Total retrieved documents: {len(all_retrieved_docs)} for user: {self.user_name} - chat_id: {self.chat_id}')
        # return retrieved_content, all_retrieved_docs
    
    # def _prepare_messages(self, messages: List, system_msg: SystemMessage) -> List:
    #     """Prepare messages by ensuring proper system message placement."""
    #     prepared_messages = []
    #     has_system = False
        
    #     for msg in messages:
    #         if msg.type == "system":
    #             has_system = True
    #             continue
    #         prepared_messages.append(msg)
        
    #     # Add system message at the beginning
    #     if has_system:
    #         prepared_messages = [system_msg] + prepared_messages
    #     else:
    #         prepared_messages.insert(0, system_msg)
            
    #     return prepared_messages
    
    async def report_or_respond(self, state: MessagesState) -> Dict[str, List]:
        """Generate tool call to retrieve relevant information for generating a report in markdown according to the user's instructions or respond to the user's query directly."""
        event_writer = get_stream_writer()
        try:
            # Process messages
            messages = list(state["messages"])  # Make a copy to avoid modifying original

            # Create the retrieve tool with proper decoration
            retrieve_tool = tool(response_format="content_and_artifact")(self.retrieve)

            # --- BALANCE CHECK LOGIC (PRE-LMM INVOCATION) ---
            balance_info = None
            has_sufficient_balance = True
            if getattr(self, "user_id", None):
                try:
                    async with async_session_scope() as session:
                        balance_info = await WalletService.has_sufficient_balance(
                            user_id=self.user_id,
                            tokens_needed=REPORT_GENERATION_COST,
                            session=session
                        )
                        if balance_info.get("success"):
                            has_sufficient_balance = balance_info.get("has_balance", True)
                            if not has_sufficient_balance:
                                logger.info(
                                    f"Insufficient balance detected before LLM call for user: {self.user_name} - chat_id: {self.chat_id}. "
                                    f"Available: {balance_info.get('available')}, Required: {balance_info.get('required')}"
                                )
                        else:
                            logger.warning(
                                f"Balance check failed for user: {self.user_name} - chat_id: {self.chat_id}: {balance_info.get('error')}. "
                                f"Proceeding with tool binding."
                            )
                            has_sufficient_balance = True  # Allow tool binding if check fails, downstream will handle
                except Exception as balance_error:
                    logger.error(
                        f"Error checking balance before LLM call for user: {self.user_name} - chat_id: {self.chat_id}: {balance_error}"
                    )
                    has_sufficient_balance = True  # Allow tool binding, fail-safe

            event_writer({"name": "report_or_respond", "status": "message_stream_start"})
            response = None

            # --- Per-tool call counting (shared by both balance paths) ---
            # Walk backwards to find the last human message, then count how many
            # times each auxiliary tool has already been called in this turn so we
            # can enforce per-tool rate limits consistently regardless of balance.
            last_human_idx = -1
            for idx, m in enumerate(messages):
                if getattr(m, 'type', None) == 'human':
                    last_human_idx = idx

            current_turn_msgs = messages[last_human_idx + 1:]

            web_search_count = 0
            query_doc_count = 0
            for m in current_turn_msgs:
                if getattr(m, 'type', None) != 'tool':
                    continue
                name = getattr(m, 'name', '')
                if name == 'retrieve_latest_info':
                    web_search_count += 1
                if name == 'query_document':
                    query_doc_count += 1

            has_file_ids = self._has_uploaded_documents()

            # ── INSUFFICIENT BALANCE PATH ──
            # Do not bind any tools — report generation and Learning Brain lookups
            # are unavailable until the user tops up their wallet.
            if not has_sufficient_balance and balance_info:
                available = balance_info.get("available", 0)
                required = balance_info.get("required", REPORT_GENERATION_COST)
                shortfall = balance_info.get("shortfall", required - available)

                insufficient_balance_context = (
                    f"\n\nIMPORTANT - INSUFFICIENT TOKEN BALANCE:\n"
                    f"The user currently has insufficient token balance to generate a report.\n"
                    f"- Available: {available:,} tokens\n"
                    f"- Required: {required:,} tokens\n"
                    f"- Shortfall: {shortfall:,} tokens\n\n"
                    f"CRITICAL: You have NO tools available right now.\n"
                    f"- Do NOT call retrieve, retrieve_latest_info, or query_document\n"
                    f"- Do NOT consult the Learning Brain, uploaded documents, or external sources\n"
                    f"- Do NOT produce report-style output, tables, structured findings, or mini-reports as substitutes\n"
                    f"- Answer only with brief general guidance from your own knowledge\n\n"
                    f"NEVER MENTION TOOLS OR INTERNAL PROCESSES TO THE USER:\n"
                    f"- NEVER mention tool names like 'retrieve tool', 'query_document tool', or any internal mechanisms\n"
                    f"- NEVER say 'I'll use my tool', 'Let me call a function', 'Using my retrieval system', "
                    f"'I'll tap into the Learning Brain', etc.\n"
                    f"- Just provide information naturally without referencing any internal processes\n\n"
                    f"YOU MUST CONTINUE TO INTERACT WITH THE USER NORMALLY:\n"
                    f"- Chat with the user as usual and answer their questions at a high level\n"
                    f"- If they ask about tokens/wallet, explain: 'Tokens are used to generate reports. Each report costs {required:,} tokens. "
                    f"You can add tokens from the Wallet page to enable report generation.'\n"
                    f"- If they request a report, follow your standard process:\n"
                    f"  1. Create and present the proposed report layout\n"
                    f"  2. Ask focused questions to refine requirements\n"
                    f"  3. Display the final requirements summary\n"
                    f"  4. Ask for confirmation as usual\n"
                    f"- ONLY AFTER receiving final confirmation, inform them:\n"
                    f"  'I have captured all the requirements for your report. However, you currently need {shortfall:,} more tokens "
                    f"to generate this report. Report generation will become available once you top up your balance. "
                    f"Please visit your Wallet page to add more tokens, then we can proceed with generating the report.'\n"
                    f"- Do NOT mention the token balance issue until after you've captured all requirements and received final confirmation\n"
                    f"- Do NOT attempt to generate the report or substitute report content in chat\n"
                    f"- Be helpful, friendly, and professional throughout the interaction"
                )

                original_system_msg = messages[0]
                updated_system_msg = SystemMessage(content=original_system_msg.content + insufficient_balance_context)
                messages = [updated_system_msg] + messages[1:]

                logger.info(
                    f"[insufficient_balance] Invoking LLM with no tools bound for user: {self.user_name} - chat_id: {self.chat_id}. "
                    f"Available: {available}, Required: {required}, Shortfall: {shortfall}."
                )

                try:
                    logger.info(f"[insufficient_balance] Using anthropic llm for user: {self.user_name} - chat_id: {self.chat_id}")
                    response = await self.anthropic_llm.ainvoke(messages)
                    save_raw_llm_response(response, ANTHROPIC_MODEL_ID, "Handling a low-balance research request", self.chat_id, user_id=self.user_id or self.user_name)
                    # logger.info(f"[insufficient_balance] Using bedrock llm for user: {self.user_name} - chat_id: {self.chat_id}")
                    # response = await LLM.ainvoke(messages)
                except Exception as e:
                    logger.error(f"[insufficient_balance] Anthropic failed: {e}, falling back to openai for user: {self.user_name} - chat_id: {self.chat_id}")
                    try:
                        logger.info(f"[insufficient_balance] Using openai llm for user: {self.user_name} - chat_id: {self.chat_id}")
                        response = await OPENAI_LLM_LANGCHAIN.ainvoke(messages)
                        save_raw_llm_response(response, OPENAI_CHAT_MODEL_ID, "Handling a low-balance research request (backup)", self.chat_id, user_id=self.user_id or self.user_name)
                    except Exception as e:
                        logger.error(f"[insufficient_balance] All LLM providers failed for user: {self.user_name} - chat_id: {self.chat_id}. Last error: {e}")
                        raise Exception(f"All LLM providers failed. Last error: {e}")
            else:
                # ── NORMAL TOOL-BINDING FLOW (all tools including retrieve) ──
                bound_tools = [retrieve_tool]
                withheld_tools = []

                # retrieve_latest_info tool binding
                if web_search_count < MAX_WEB_SEARCH_CALLS_PER_TURN:
                    bound_tools.append(tool(self.retrieve_latest_info))
                else:
                    withheld_tools.append("retrieve_latest_info")
                    logger.info(f"retrieve_latest_info limit reached ({web_search_count}/{MAX_WEB_SEARCH_CALLS_PER_TURN}) for user: {self.user_name} - chat_id: {self.chat_id}")

                if has_file_ids and query_doc_count < MAX_QUERY_DOC_CALLS_PER_TURN:
                    bound_tools.append(tool(self.query_document))
                elif has_file_ids:
                    withheld_tools.append("query_document")
                    logger.info(f"query_document limit reached ({query_doc_count}/{MAX_QUERY_DOC_CALLS_PER_TURN}) for user: {self.user_name} - chat_id: {self.chat_id}")

                if withheld_tools:
                    limit_note = (
                        f"\n\nIMPORTANT — TOOL USAGE LIMIT REACHED:\n"
                        f"The following tools are NO LONGER AVAILABLE for this message "
                        f"because you have already used them the maximum number of times: "
                        f"{', '.join(withheld_tools)}.\n"
                        f"Do NOT attempt to call these tools. Use the information you have "
                        f"already gathered from previous tool calls to formulate your response."
                    )
                    original_sys = messages[0]
                    messages[0] = SystemMessage(content=original_sys.content + limit_note)

                logger.info(
                    f"[normal_flow] Invoking LLM with all tools for user: {self.user_name} - chat_id: {self.chat_id}. "
                    f"Bound tools: {[t.name for t in bound_tools]}, Withheld tools: {withheld_tools}"
                )

                try:
                    # raise Exception("test")
                    logger.info(f"[normal_flow] Using anthropic llm for user: {self.user_name} - chat_id: {self.chat_id}")
                    llm_with_tools = self.anthropic_llm.bind_tools(bound_tools)
                    response = await llm_with_tools.ainvoke(messages)
                    save_raw_llm_response(response, ANTHROPIC_MODEL_ID, "Planning and answering the main research request", self.chat_id, user_id=self.user_id or self.user_name)
                    # logger.info(f"[normal_flow] Using bedrock llm for user: {self.user_name} - chat_id: {self.chat_id}")
                    # llm_with_tools = LLM.bind_tools(bound_tools)
                    # response = await llm_with_tools.ainvoke(messages)
                except Exception as e:
                    logger.error(f"[normal_flow] Anthropic failed: {e}, falling back to openai for user: {self.user_name} - chat_id: {self.chat_id}")
                    try:
                        logger.info(f"[normal_flow] Using openai llm for user: {self.user_name} - chat_id: {self.chat_id}")
                        llm_with_tools = OPENAI_LLM_LANGCHAIN.bind_tools(bound_tools)
                        response = await llm_with_tools.ainvoke(messages)
                        save_raw_llm_response(response, OPENAI_CHAT_MODEL_ID, "Planning and answering the main research request (backup)", self.chat_id, user_id=self.user_id or self.user_name)
                    except Exception as e:
                        logger.error(f"[normal_flow] All LLM providers failed for user: {self.user_name} - chat_id: {self.chat_id}. Last error: {e}")
                        raise Exception(f"All LLM providers failed. Last error: {e}")

            event_writer({"name": "report_or_respond", "status": "message_stream_complete"})

            return {"messages": [response]}
        except Exception as e:
            logger.error(f"Error in report_or_respond: {e} for user: {self.user_name} - chat_id: {self.chat_id}")
            # Return error message
            error_msg = AIMessage(content=f"I apologize, but I encountered an error while processing your request.")
            event_writer({"name": "report_or_respond", "status": "error"})
            return {"messages": [error_msg]}
            
    def _get_recent_tool_messages(self, messages: List) -> List:
        """Extract the most recent tool message from the message history."""
        for message in reversed(messages):
            if hasattr(message, 'type') and message.type == "tool":  # Added: hasattr check
                return [message]
        return []


    # async def _raw_report_generation(self, final_prompt: str) -> str:
    #     """Generate the raw report using the LLM"""
    #     try:
    #         response = await self.anthropic_report_llm.ainvoke(final_prompt)
    #     except Exception as e:
    #         logger.error(f"Error generating raw markdown report using anthropic api: {e} for user: {self.user_name} - chat_id: {self.chat_id}")
    #         # try:
    #         logger.info(f"Now using bedrock api to generate the report for user: {self.user_name} - chat_id: {self.chat_id}")
    #         response = await BEDROCK_REPORT_LLM.ainvoke(final_prompt)
            # except Exception as e:
                # logger.error(f"Error generating raw markdown report using bedrock api: {e} for user: {self.user_name} - chat_id: {self.chat_id}")
                # raise Exception(f"Error generating raw markdown report using bedrock api: {e}")
                # response = AIMessage(content="Error: There was an error while generating the report. Please try again.")

        # return response


    def _emit_card_step(self, event_writer, section_name: str, step: str) -> None:
        """Emit a post-processing progress beat for the card currently being finalized.

        Mirrors the ``card_generation_heartbeat`` events sent during generation so
        the frontend can keep showing concrete progress ("Summarizing section…",
        "Building charts…", …) after the LLM response has been received.
        """
        if event_writer is None or not section_name:
            return
        event_writer({
            "name": "generate_report",
            "status": "card_generation_heartbeat",
            "section": section_name,
            "step": step,
        })

    async def _prepare_card_through_pipeline(
        self,
        card: dict,
        report_type: str = "study",
        event_writer=None,
        section_name: str = "",
    ) -> Tuple[dict, dict]:
        """Run the expensive card processing steps without emitting or appending.
        
        This is intentionally side-effect-light so multiple cards can be prepared
        concurrently and then stored/streamed in their original order.

        When ``event_writer`` and ``section_name`` are provided, post-processing
        progress beats are emitted for each stage (summary, charts) so the
        frontend can surface what is happening after the card's LLM response.
        """
        self._emit_card_step(event_writer, section_name, "Summarizing section…")
        try:
            card_summary = await asyncio.to_thread(
                generate_section_summary,
                card,
                chat_id=self.chat_id,
                user_id=self.user_id or self.user_name,
            )
        except Exception as e:
            logger.error(f"Error generating summary: {str(e)} for user: {self.user_name} - chat_id: {self.chat_id}")
            card_summary = ""

        card["summary"] = card_summary
        self._emit_card_step(event_writer, section_name, "Building charts…")
        modified_card = await asyncio.to_thread(modify_card, card)
        card, table_and_table_id_map_for_current_card = await asyncio.to_thread(
            add_viz_to_card,
            modified_card,
            self.user_name,
            self.chat_id,
            report_type,
            user_id=self.user_id or self.user_name,
        )
        logger.info(f"Table and table id map for current card: {table_and_table_id_map_for_current_card}")
        card = await asyncio.to_thread(
            update_summaries_for_ask_caspr,
            card,
            chat_id=self.chat_id,
            user_id=self.user_id or self.user_name,
        )
        card['section'][0]['content'] = card['section'][0]['content'].replace("?utm_source=openai", "").replace("&utm_source=openai", "")
        for sub_section in card['sub_sections']:
            sub_section['content'] = sub_section['content'].replace("?utm_source=openai", "").replace("&utm_source=openai", "")
        return card, table_and_table_id_map_for_current_card

    async def _process_card_through_pipeline(self, card: dict, cards_for_db: list, table_and_table_id_map: dict, event_writer, report_type: str = "study", section_name: str = "") -> dict:
        """Common card processing pipeline shared by study and brief flows.
        
        Takes a raw card (after fix_card), generates summary, modifies structure,
        adds visualizations, updates summaries, cleans URLs, appends to cards_for_db,
        and emits via event_writer.
        
        Returns the processed card.
        """
        card, table_and_table_id_map_for_current_card = await self._prepare_card_through_pipeline(
            card, report_type=report_type, event_writer=event_writer, section_name=section_name,
        )
        table_and_table_id_map.update(table_and_table_id_map_for_current_card)
        cards_for_db.append(card)
        event_writer({"name": "generate_report", "card_db": card, "card_type": "section"})
        return card

    async def _process_brief_card_task(
        self,
        idx: int,
        card: dict,
        event_writer=None,
        section_name: str = "",
    ) -> Tuple[int, dict, dict]:
        """Prepare a brief card concurrently while preserving its original index."""
        try:
            logger.info(f"[brief] Processing card {idx}: {card.get('section', 'unknown')} for user: {self.user_name} - chat_id: {self.chat_id}")
            processed_card, table_map = await self._prepare_card_through_pipeline(
                card, report_type="brief", event_writer=event_writer, section_name=section_name,
            )
            return idx, processed_card, table_map
        except Exception as e:
            logger.error(f"[brief] Error processing card {idx}: {str(e)} for user: {self.user_name} - chat_id: {self.chat_id}")
            fallback_card = {
                "section": [{"name": card.get('section', f'Section {idx}'), "content": "", "tables": [{"visualization": "", "table_id": "", "table_title": "", "visualization_type": ""}], "id": str(uuid7()), "summary": ""}],
                "sub_sections": [{"name": "", "content": "", "tables": [{"visualization": "", "table_id": "", "table_title": "", "visualization_type": ""}], "id": str(uuid7()), "summary": ""}],
                "citations": {},
                "summary": ""
            }
            return idx, fallback_card, {}

    async def generate_report(self, state: MessagesState) -> Dict[str, List]:
        """Generate the final report using retrieved information.
        
        Note: This method should only be called after the retrieve tool has been executed.
        Document query tools should not route here.
        """
        try:
            event_writer = get_stream_writer()
            # Extract tool messages (retrieval results)
            tool_messages = self._get_recent_tool_messages(state["messages"])
            
            if not tool_messages:
                error_msg = "No tool messages found. The generate_report method should only be called after the retrieve tool."
                logger.error(f"{error_msg} for user: {self.user_name} - chat_id: {self.chat_id}")
                raise Exception(error_msg)

            if not hasattr(tool_messages[0], 'artifact') or not tool_messages[0].artifact:
                error_msg = f"No artifact found in tool message. Tool called was: {tool_messages[0].name if hasattr(tool_messages[0], 'name') else 'unknown'}. The generate_report method requires the retrieve tool's artifact."
                logger.error(f"{error_msg} for user: {self.user_name} - chat_id: {self.chat_id}")
                raise Exception(error_msg)
            
            artifact = tool_messages[0].artifact
            if not artifact:
                logger.error(f"No artifact found in tool_messages for user: {self.user_name} - chat_id: {self.chat_id}")
                raise
            
            descriptive_report_layout = artifact.get('descriptive_report_layout', '')
            # modified_report_layout = artifact.get('modified_report_layout', '')
            report_layout = artifact.get('report_layout', '')

            if not descriptive_report_layout:
                logger.error(f"No descriptive report layout found in artifact for user: {self.user_name} - chat_id: {self.chat_id}")
                raise Exception(f"No descriptive report layout found in artifact for user: {self.user_name} - chat_id: {self.chat_id}")

            # if not modified_report_layout:
            #     logger.error(f"No modified report layout found in artifact for user: {self.user_name} - chat_id: {self.chat_id}")
            #     raise Exception(f"No modified report layout found in artifact for user: {self.user_name} - chat_id: {self.chat_id}")
            
            if not report_layout:
                logger.error(f"No report layout found in artifact for user: {self.user_name} - chat_id: {self.chat_id}")
                raise Exception(f"No report layout found in artifact for user: {self.user_name} - chat_id: {self.chat_id}")
            
            # Get configuration from retrieve_config
            user_instructions = self.retrieve_config.get('user_instructions', '')
            report_layout = self.retrieve_config.get('report_layout', '')
            report_type = self.retrieve_config.get('report_type', 'study')
            # report_language = self.retrieve_config.get('report_language', 'English')
            # Validation - Added
            if not user_instructions or not report_layout:
                logger.error(f"Missing user_instructions or report_layout in retrieve_config for user: {self.user_name} - chat_id: {self.chat_id}")
                raise Exception(f"Missing user_instructions or report_layout in retrieve_config for user: {self.user_name} - chat_id: {self.chat_id}")
            table_of_contents, title, subtitle = await asyncio.to_thread(extract_title_and_toc, descriptive_report_layout)
            title = self.retrieve_config['report_title']
            logger.info(f"Title from LG: {title}")
            if not table_of_contents or not title or not subtitle:
                logger.error(f"Error extracting title and toc for user: {self.user_name} - chat_id: {self.chat_id}")
                raise Exception(f"Error extracting title and toc for user: {self.user_name} - chat_id: {self.chat_id}")
            
            cards_for_db = []
            title_section_id = str(uuid7())
            title_sub_section_id = str(uuid7())
            cards_for_db.append({
                "section": [
                    {
                        "name": "title",
                        "content": title,
                        "tables": [
                            {
                                "visualization": "",
                                "table_id": "",
                                "table_title": "",
                                "visualization_type": ""
                            }
                        ],
                        "id": title_section_id,
                        "summary": ""
                    }
                ],
                "sub_sections": [
                    {
                        "name": "",
                        "content": "",
                        "tables": [
                            {
                                "visualization": "",
                                "table_id": "",
                                "table_title": "",
                                "visualization_type": ""
                            }
                        ],
                        "id": title_sub_section_id,
                        "summary": ""
                    }
                ],
                "citations": {},
                "summary": ""
            })
            event_writer({"name": "generate_report", "card_db": {"section": [{"name": "title", "content": title,"tables": [{"visualization": "", "table_id": "", "table_title": "", "visualization_type": ""}],"id": title_section_id,"summary": ""}],"sub_sections": [{"name": "", "content": "", "tables": [{"visualization": "", "table_id": "", "table_title": "", "visualization_type": ""}],"id": title_sub_section_id,"summary": ""}],"citations": {},"summary": ""}, "card_type":"title"})

            subtitle_section_id = str(uuid7())
            subtitle_sub_section_id = str(uuid7())
            cards_for_db.append({"section": [
                {
                    "name": "subtitle",
                    "content": subtitle,
                    "tables": [
                        {
                            "visualization": "",
                            "table_id": "",
                            "table_title": "",
                            "visualization_type": ""
                        }
                    ],
                    "id": subtitle_section_id,
                    "summary": ""

                }
            ],
            "sub_sections": [
                {
                    "name": "",
                    "content": "",
                    "tables": [
                        {
                            "visualization": "",
                            "table_id": "",
                            "table_title": "",
                            "visualization_type": ""
                        }
                    ],
                    "id": subtitle_sub_section_id,
                    "summary": ""
                }
            ],
            "citations": {},
            "summary": ""
            })
            event_writer({"name": "generate_report", "card_db": {"section": [{"name": "subtitle", "content": subtitle,"tables": [{"visualization": "", "table_id": "", "table_title": "", "visualization_type": ""}],"id": subtitle_section_id,"summary": ""}],"sub_sections": [{"name": "", "content": "", "tables": [{"visualization": "", "table_id": "", "table_title": "", "visualization_type": ""}],"id": subtitle_sub_section_id,"summary": ""}],"citations": {},"summary": ""}, "card_type":"subtitle"})

            # cards_for_db.append({"table_of_contents": f"Table of Contents\n\n{table_of_contents}"})
            toc_section_id = str(uuid7())
            toc_sub_section_id = str(uuid7())
            cards_for_db.append({
                "section": [
                    {
                        "name": "table_of_contents",
                        "content": table_of_contents,
                        "tables": [
                            {
                                "visualization": "",
                                "table_id": "",
                                "table_title": "",
                                "visualization_type": ""
                            }
                        ],
                        "id": toc_section_id,
                        "summary": ""
                    }
                ],
                "sub_sections": [
                    {
                        "name": "",
                        "content": "",
                        "tables": [
                            {
                                "visualization": "",
                                "table_id": "",
                                "table_title": "",
                                "visualization_type": ""
                            }
                        ],
                        "id": toc_sub_section_id,
                        "summary": ""
                    }
                ],
                "citations": {},
                "summary": ""
            })
            event_writer({"name": "generate_report", "card_db": {"section": [{"name": "table_of_contents", "content": table_of_contents,"tables": [{"visualization": "", "table_id": "", "table_title": "", "visualization_type": ""}],"id": toc_section_id,"summary": ""}],"sub_sections": [{"name": "", "content": "", "tables": [{"visualization": "", "table_id": "", "table_title": "", "visualization_type": ""}],"id": toc_sub_section_id,"summary": ""}],"citations": {},"summary": ""}, "card_type":"toc"})
            
            all_report_citations = [] # Here we store all the citations from all the sections with right sequence
            openai_citations = [] # Here we store all the citations from openai in raw json format 
            citation_url_map = {} # Here we store url-citation mapping (key: url, value: citation)
            descriptive_report_layout = descriptive_report_layout[1:] # Removing the first section (title)
            # cumulative_summary = ""  # PAUSED: Using full previous cards as context instead
            table_and_table_id_map = {}

            if report_type == 'brief':
                logger.info(f"[generate_report] Brief report mode — using generate_brief_cards | user: {self.user_name} - chat_id: {self.chat_id}")
                # Only enrich DRL with document context when a grep session is available
                if self.grep_session:
                    descriptive_report_layout = gather_context_from_uploaded_file(
                        descriptive_report_layout,
                        self.grep_session,
                        user_instructions,
                        chat_id=self.chat_id,
                        user_id=self.user_id or self.user_name,
                    )

                # Brief card streaming has two stages running together:
                # 1. `brief_gen` yields raw cards as soon as the brief LLM finishes each JSON section.
                #    Example raw_card:
                #    {
                #        "section": "External Shock Risks",
                #        "content": "The main near-term crisis triggers are...",
                #        "sub_sections": [{"name": "Impact of US tariffs", "content": "..."}],
                #    }
                # 2. `_process_brief_card_task` performs slower post-processing for that card:
                #    summaries, markdown cleanup, table extraction, and visualization upload.
                #
                # We process cards concurrently for speed, but emit them in report order.
                # Example:
                # - Card 1 and card 2 are processing.
                # - Card 2 finishes first, so we store it in `brief_card_results`.
                # - Card 1 finishes later; `flush_ready_brief_cards()` emits card 1, then
                #   immediately emits already-ready card 2.

                # Web-search analytics for brief reports: a single `generate_brief_cards`
                # call covers the *entire* report (all sections in one LLM call), unlike
                # the study path below which runs one `generate_cards` call per section.
                # `analytics_collector` therefore ends up holding at most one entry, and
                # persistence is scheduled once after all brief cards are emitted (see
                # `_schedule_brief_analytics` further down), instead of once per section.
                analytics_collector: List[Dict] = []
                analytics_operation_id = str(uuid7())

                brief_gen = generate_brief_cards(
                    drl=descriptive_report_layout,
                    user_instructions=user_instructions,
                    chat_id=self.chat_id,
                    user_id=self.user_id or self.user_name,
                    analytics_collector=analytics_collector,
                    analytics_operation_id=analytics_operation_id,
                    prioritize_arxiv=self.prioritize_arxiv,
                )
                idx = 0  # Last raw card number received from `brief_gen`; example: 0 -> 1 -> 2.
                next_emit_idx = 1  # Next card number allowed to stream to UI; example: waits for card 1 before card 2.
                brief_card_tasks = {}  # Maps active asyncio tasks to card indexes; example: {<Task pending>: 2}.
                brief_card_results = {}  # Holds completed cards waiting for order; example: {2: (card_db, table_map)}.
                brief_stream_finished = False  # True only after `brief_gen` returns None, meaning no more raw cards.
                # Per-card heartbeat tasks: each card keeps its OWN heartbeat running
                # from the moment it starts streaming, through its (concurrent)
                # post-processing, until its `card_db` event is actually sent. Multiple
                # cards' heartbeats therefore run at the same time (e.g. card A finishing
                # post-processing while card B is still being generated).
                card_hb_tasks = {}  # {card_idx: hb_task}
                hb_reap = []  # cancelled heartbeat tasks awaiting a final await to avoid warnings

                def flush_ready_brief_cards():
                    """Emit every completed card that is ready in strict report order.

                    Example before flushing:
                    - next_emit_idx = 1
                    - brief_card_results = {2: (card2, table_map2)}
                    Nothing emits because card 1 is still missing.

                    Example after card 1 arrives:
                    - next_emit_idx = 1
                    - brief_card_results = {1: (card1, table_map1), 2: (card2, table_map2)}
                    This emits card 1, increments next_emit_idx to 2, then emits card 2.
                    """
                    nonlocal next_emit_idx
                    while next_emit_idx in brief_card_results:
                        card, table_map = brief_card_results.pop(next_emit_idx)
                        table_and_table_id_map.update(table_map)
                        cards_for_db.append(card)
                        logger.info(f"[brief] Emitting card {next_emit_idx} for user: {self.user_name} - chat_id: {self.chat_id}")
                        event_writer({"name": "generate_report", "card_db": card, "card_type": "section"})
                        # This card's event has now been sent, so its heartbeat is no
                        # longer needed — stop it (later cards keep their own running).
                        emitted_hb = card_hb_tasks.pop(next_emit_idx, None)
                        if emitted_hb is not None:
                            emitted_hb.cancel()
                            hb_reap.append(emitted_hb)
                        next_emit_idx += 1

                # Brief cards stream in DRL order, so while the LLM is producing the
                # next card we surface that section's own DRL heartbeat lines. Each
                # raw-card fetch gets a paired heartbeat task keyed off the DRL section
                # about to arrive (descriptive_report_layout[<cards received so far>]).
                def _start_brief_heartbeat(card_number: int):
                    # Heartbeat for the section currently being streamed by the LLM.
                    # Out of range (no more sections to stream) yields an empty no-op:
                    # the per-card heartbeats in `card_hb_tasks` cover post-processing.
                    section = (
                        descriptive_report_layout[card_number]
                        if card_number < len(descriptive_report_layout) else {}
                    )
                    return asyncio.create_task(
                        _run_card_heartbeat(
                            section.get("section", f"Section {card_number + 1}"),
                            event_writer,
                            heartbeat_lines=_drl_heartbeat_lines(section),
                        )
                    )

                async def _stop_brief_heartbeat(hb_task):
                    if hb_task is None:
                        return
                    hb_task.cancel()
                    try:
                        await hb_task
                    except asyncio.CancelledError:
                        pass

                # Pulling from the sync generator can block while the LLM streams the next JSON object,
                # so we run `next(brief_gen, None)` in a thread and race it against card processing.
                # Example result: raw_card dict for card 3, or None when the brief stream is complete.
                next_raw_card_task = asyncio.create_task(asyncio.to_thread(lambda: next(brief_gen, None)))
                next_raw_card_hb = _start_brief_heartbeat(idx)
                await asyncio.sleep(0)  # yield once so the heartbeat task can emit its first line
                while not brief_stream_finished or brief_card_tasks:
                    # Wait for either:
                    # - the next raw card to arrive from the LLM stream, or
                    # - any already-started card processing task to finish.
                    # This is what lets card 1 emit as soon as it finishes, without waiting for all cards.
                    wait_tasks = set(brief_card_tasks.keys())
                    if next_raw_card_task is not None:
                        wait_tasks.add(next_raw_card_task)

                    done, _ = await asyncio.wait(wait_tasks, return_when=asyncio.FIRST_COMPLETED)
                    raw_card_task_done = next_raw_card_task if next_raw_card_task in done else None

                    if raw_card_task_done is not None:
                        raw_card = raw_card_task_done.result()
                        if raw_card is None:
                            # LLM stream is done. This heartbeat was anticipating a
                            # section that will never stream, so stop it. Cards already
                            # in flight keep their own heartbeats (card_hb_tasks) running
                            # through post-processing until their card events are sent.
                            brief_stream_finished = True
                            next_raw_card_task = None
                            await _stop_brief_heartbeat(next_raw_card_hb)
                            next_raw_card_hb = None
                        else:
                            # A real card arrived. Its section has finished streaming,
                            # but we DON'T stop its heartbeat — we hand it off to
                            # card_hb_tasks so it keeps running through this card's
                            # post-processing until its card event is sent. A fresh
                            # heartbeat is started below for the next streaming section.
                            idx += 1
                            card_hb_tasks[idx] = next_raw_card_hb
                            next_raw_card_hb = None
                            try:
                                logger.info(f"[brief] Queueing card {idx}: {raw_card.get('section', 'unknown')} for user: {self.user_name} - chat_id: {self.chat_id}")
                                card = replace_brief_citations(raw_card, citation_url_map, all_report_citations)
                                # Start expensive card processing but do not await it here.
                                # Example task result shape: (2, processed_card_db, {"table-id": "| table markdown |"}).
                                task = asyncio.create_task(self._process_brief_card_task(
                                    idx, card,
                                    event_writer=event_writer,
                                    section_name=raw_card.get("section", f"Section {idx}"),
                                ))
                                brief_card_tasks[task] = idx

                            except Exception as e:
                                logger.error(f"[brief] Error queueing card {idx}: {str(e)} for user: {self.user_name} - chat_id: {self.chat_id}")
                                fallback_card = {
                                    "section": [{"name": raw_card.get('section', f'Section {idx}'), "content": "", "tables": [{"visualization": "", "table_id": "", "table_title": "", "visualization_type": ""}], "id": str(uuid7()), "summary": ""}],
                                    "sub_sections": [{"name": "", "content": "", "tables": [{"visualization": "", "table_id": "", "table_title": "", "visualization_type": ""}], "id": str(uuid7()), "summary": ""}],
                                    "citations": {},
                                    "summary": ""
                                }
                                brief_card_results[idx] = (fallback_card, {})
                                flush_ready_brief_cards()

                            # Keep reading the next raw card while existing cards continue processing.
                            next_raw_card_task = asyncio.create_task(asyncio.to_thread(lambda: next(brief_gen, None)))
                            next_raw_card_hb = _start_brief_heartbeat(idx)
                            await asyncio.sleep(0)  # yield once so the heartbeat task can emit its first line

                    for completed_task in done:
                        if completed_task not in brief_card_tasks:
                            continue

                        task_idx = brief_card_tasks.pop(completed_task)
                        try:
                            result_idx, card, table_map = completed_task.result()
                        except Exception as e:
                            logger.error(f"[brief] Error processing card {task_idx}: {str(e)} for user: {self.user_name} - chat_id: {self.chat_id}")
                            result_idx = task_idx
                            card = {
                                "section": [{"name": f"Section {task_idx}", "content": "", "tables": [{"visualization": "", "table_id": "", "table_title": "", "visualization_type": ""}], "id": str(uuid7()), "summary": ""}],
                                "sub_sections": [{"name": "", "content": "", "tables": [{"visualization": "", "table_id": "", "table_title": "", "visualization_type": ""}], "id": str(uuid7()), "summary": ""}],
                                "citations": {},
                                "summary": ""
                            }
                            table_map = {}

                        # Store the completed result by its original card index. It may emit now
                        # or wait for earlier cards. Example: result_idx=2 waits if card 1 is missing.
                        brief_card_results[result_idx] = (card, table_map)
                        flush_ready_brief_cards()

                # Every card has been post-processed and streamed in order. Reap any
                # heartbeats: the (possibly None) streaming heartbeat plus any per-card
                # heartbeats that flush already cancelled, and any leftovers as a
                # safety net.
                await _stop_brief_heartbeat(next_raw_card_hb)
                next_raw_card_hb = None
                for leftover_hb in list(card_hb_tasks.values()):
                    leftover_hb.cancel()
                    hb_reap.append(leftover_hb)
                card_hb_tasks.clear()
                if hb_reap:
                    await asyncio.gather(*hb_reap, return_exceptions=True)
                    hb_reap.clear()

                # Persist web-search analytics for the whole brief report now that
                # every section card has been emitted into `cards_for_db` (title,
                # subtitle, TOC, and all brief sections). Enrichment scans that full,
                # final content for URLs actually retained in output, then schedules
                # the single collected entry (if any) for a non-blocking DB write via
                # `asyncio.create_task`, mirroring the study-report path below. All
                # steps are wrapped so analytics can never affect report generation.
                try:
                    if analytics_collector:
                        enrich_terminal_search_analytics(analytics_collector, cards_for_db)
                        log_scheduled_analytics_batch(
                            "card_generation",
                            analytics_collector,
                            operation_id=analytics_operation_id,
                            section_name="brief_report",
                        )
                        for analytics_entry in analytics_collector:
                            try:
                                event = search_analytics_schedule_kwargs(analytics_entry)
                                event.update(
                                    {
                                        "trigger_source": "card_generation",
                                        "user_query": user_instructions,
                                        "user_id": self.user_id,
                                        "chat_id": self.chat_id,
                                        "section_name": "brief_report",
                                    }
                                )
                                asyncio.create_task(log_web_search_event(**event))
                            except Exception:
                                logger.exception(
                                    "[brief] Failed to schedule non-fatal "
                                    "search analytics"
                                )
                except Exception:
                    logger.exception(
                        "[brief] Failed to enrich non-fatal terminal search analytics"
                    )

            else:
                for idx, section in enumerate(descriptive_report_layout):
                    section_name = section.get("section", f"Section {idx + 1}")
                    analytics_collector: List[Dict] = []
                    analytics_operation_id = str(uuid7())
                    analytics_scheduled = False
                    card = None

                    def _schedule_collected_analytics(rendered_card=None):
                        nonlocal analytics_scheduled
                        if analytics_scheduled:
                            return
                        analytics_scheduled = True
                        if rendered_card is not None:
                            try:
                                enrich_terminal_search_analytics(
                                    analytics_collector,
                                    rendered_card,
                                )
                            except Exception:
                                logger.exception(
                                    "[card_generation] Failed to enrich non-fatal "
                                    "terminal search analytics"
                                )
                        card_id = logical_card_id(rendered_card)
                        log_scheduled_analytics_batch(
                            "card_generation",
                            analytics_collector,
                            operation_id=analytics_operation_id,
                            section_name=section_name,
                            card_id=card_id,
                        )
                        for analytics_entry in analytics_collector:
                            try:
                                event = search_analytics_schedule_kwargs(
                                    analytics_entry
                                )
                                event.update(
                                    {
                                        "trigger_source": "card_generation",
                                        "user_query": section.get("section", section_name),
                                        "user_id": self.user_id,
                                        "chat_id": self.chat_id,
                                        "section_name": section_name,
                                        "card_id": card_id,
                                    }
                                )
                                asyncio.create_task(log_web_search_event(**event))
                            except Exception:
                                logger.exception(
                                    "[card_generation] Failed to schedule "
                                    "non-fatal search analytics"
                                )

                    try:
                        logger.info(f"Processing section {idx+1}/{len(descriptive_report_layout)}: {section['section']} for user: {self.user_name} - chat_id: {self.chat_id}")

                        # Surface this section's own DRL-authored heartbeat lines while
                        # its card is generated (a single long, blocking LLM call).
                        _hb_task = asyncio.create_task(
                            _run_card_heartbeat(
                                section_name, event_writer,
                                heartbeat_lines=_drl_heartbeat_lines(section),
                            )
                        )
                        try:
                            card, card_citations = await asyncio.to_thread(generate_cards,
                                    section,
                                    "",
                                    is_first_section=idx==0,
                                    descriptive_report_layout=descriptive_report_layout,
                                    user_instructions=user_instructions,
                                    report_length=report_type.upper(),
                                    upload_file_config=self.upload_file_config,
                                    web_search=self.web_search,
                                    previous_cards=cards_for_db,
                                    grep_session=self.grep_session,
                                    chat_id=self.chat_id,
                                    user_id=self.user_id,
                                    analytics_collector=analytics_collector,
                                    analytics_operation_id=analytics_operation_id,
                                    prioritize_arxiv=self.prioritize_arxiv,
                                    )
                        finally:
                            _hb_task.cancel()
                            try:
                                await _hb_task
                            except asyncio.CancelledError:
                                pass
                        # Note: generate_cards already falls back to Gemini internally
                        # (src.core.cards.card_utils.generate_cards) when OpenAI fails, so
                        # `card`/`card_citations` are only None here if OpenAI AND the
                        # Gemini fallback both failed for this section.
                        card_with_citations = [card, card_citations]
                        card_name = f"{card['section']}"
                        asyncio.create_task(self._upload_card_to_s3_background(card_with_citations, card_name))

                        self._emit_card_step(event_writer, section_name, "Linking sources…")
                        logger.info(f"Running card fixer for section: {card.get('section', 'unknown')}")
                        card = fix_card(
                            card,
                            chat_id=self.chat_id,
                            user_id=self.user_id or self.user_name,
                        )
                        logger.info(f"Card fixer completed for section: {card.get('section', 'unknown')}")
                        asyncio.create_task(self._upload_fixed_card_to_s3_background(card, card_name))
                        
                        if card_citations and isinstance(card_citations[0], dict):
                            logger.info(f"Openai citations detected for section {idx+1}: {section['section']} for user: {self.user_name} - chat_id: {self.chat_id}")
                            openai_citations.extend(card_citations)

                            web_urls = []
                            file_citation_count = 0
                            url_to_snippet = {}
                            for citation in card_citations:
                                if citation.get('url'):
                                    web_urls.append(citation['url'])
                                    snippet = citation.get('snippet', '')
                                    if snippet:
                                        cleaned_url = clean_url(citation['url'])
                                        if cleaned_url not in url_to_snippet:
                                            url_to_snippet[cleaned_url] = snippet
                                elif citation.get('type') == 'file_citation' or citation.get('file_id'):
                                    file_citation_count += 1

                            if web_urls:
                                try:
                                    card_citations = [await asyncio.to_thread(clean_url, url) for url in web_urls]
                                    logger.info(f"Processed {len(card_citations)} web citations for user: {self.user_name} - chat_id: {self.chat_id}")
                                except Exception as e:
                                    logger.error(f"Error cleaning web citation URLs for user: {self.user_name} - chat_id: {self.chat_id}: {e}")
                                    card_citations = [url.replace("?utm_source=openai", "").replace("&utm_source=openai", "") for url in web_urls]
                            else:
                                card_citations = []

                            if file_citation_count > 0:
                                logger.info(f"Skipped {file_citation_count} file_citation annotations (no URL) for section {idx+1}: {section['section']} for user: {self.user_name} - chat_id: {self.chat_id}")

                        elif card_citations:
                            logger.info(f"Perplexity citations detected for section {idx+1}: {section['section']} for user: {self.user_name} - chat_id: {self.chat_id}")
                            url_to_snippet = {}
                        else:
                            logger.warning(f"No citations found for section {idx+1}: {section['section']} for user: {self.user_name} - chat_id: {self.chat_id}")
                            card_citations = []
                            url_to_snippet = {}

                        if card is None or card_citations is None:
                            logger.error(f"Failed to generate content for section {idx+1} for user: {self.user_name} - chat_id: {self.chat_id}")
                            raise Exception(f"Card generation failed for section {section['section']}")

                        processed_citations = []
                        citation_indices = []
                        
                        for url in card_citations:
                            if url in citation_url_map:
                                citation_indices.append(citation_url_map[url])
                            else:
                                all_report_citations.append(url)
                                citation_url_map[url] = len(all_report_citations)
                                citation_indices.append(len(all_report_citations))
                            processed_citations.append(url)

                        logger.info(f"Total unique citations count: {len(all_report_citations)} for section {idx+1}: {section['section']} for user: {self.user_name} - chat_id: {self.chat_id}")
                        logger.info(f"Generated content for section {idx+1} for user: {self.user_name} - chat_id: {self.chat_id}")
                        logger.info(f"Retrieved {len(card_citations)} citations, {len(processed_citations)} after processing for user: {self.user_name} - chat_id: {self.chat_id}")
                        
                        try:
                            section_content_with_citations = await asyncio.to_thread(replace_citations,
                                card['content'],
                                citation_indices,
                                processed_citations,
                                citation_url_map,
                                all_report_citations,
                                url_to_snippet
                            )
                            if not section_content_with_citations:
                                logger.error(f"Failed to replace citations in section {idx+1} for user: {self.user_name} - chat_id: {self.chat_id}")
                                section_content_with_citations = card['content']

                            card['content'] = section_content_with_citations
                        except Exception as e:
                            logger.error(f"Error replacing citations in section {idx+1}: {str(e)}")
                            section_content_with_citations = card['content']
                            card['content'] = section_content_with_citations

                        sub_section_content = [card['sub_sections'][i]['content'] for i in range(len(card['sub_sections']))]
                        for i, sentence in enumerate(sub_section_content):
                            try:
                                sentence_with_citations = await asyncio.to_thread(replace_citations,
                                    sentence, 
                                    citation_indices, 
                                    processed_citations,
                                    citation_url_map,
                                    all_report_citations,
                                    url_to_snippet
                                )
                                if not sentence_with_citations:
                                    logger.error(f"Failed to replace citations in subsection {i} for user: {self.user_name} - chat_id: {self.chat_id}")
                                    sentence_with_citations = sentence

                                card['sub_sections'][i]['content'] = sentence_with_citations
                            except Exception as e:
                                logger.error(f"Error replacing citations in subsection {i}: {str(e)}")
                                card['sub_sections'][i]['content'] = sentence

                        logger.info(f"Added section {idx+1} to cards_for_db for user: {self.user_name} - chat_id: {self.chat_id}")

                        card["citations"] = {url: citation_url_map.get(url, 0) for url in processed_citations if url in citation_url_map}
                        card = await self._process_card_through_pipeline(card, cards_for_db, table_and_table_id_map, event_writer, report_type=report_type, section_name=section_name)
                        _schedule_collected_analytics(card)

                        logger.info(f"Updating cumulative summary after section {idx+1} for user: {self.user_name} - chat_id: {self.chat_id}")
                
                    except Exception as e:
                        _schedule_collected_analytics()
                        logger.error(f"Error processing section {idx+1}: {str(e)} for user: {self.user_name} - chat_id: {self.chat_id}")
                        card = {
                            "section": [
                                {
                                    "name": section['section'],
                                    "content": "",
                                    "tables": [
                                        {
                                            "visualization": "",
                                            "table_id": "",
                                            "table_title": "",
                                            "visualization_type": ""
                                        }
                                    ],
                                    "id": str(uuid7()),
                                    "summary": ""
                                }
                            ],
                            "sub_sections": [
                                {
                                    "name": "",
                                    "content": "",
                                    "tables": [
                                        {
                                            'visualization': "",
                                            'table_id': "",
                                            'table_title': "",
                                            'visualization_type': ""
                                        }
                                    ],
                                    "id": str(uuid7()),
                                    "summary": ""
                                }
                            ],
                            "citations": {},
                            "summary": ""
                        }
                        cards_for_db.append(card)
                        card_citations = []
                        event_writer({"name": "generate_report", "card_db": card, "card_type":"section"})

            event_writer({"name":"generate_report", "report_citations": citation_url_map})
            # Build cumulative_summary from individual card summaries for executive summary generation
            cumulative_summary = "\n".join(
                card.get("summary", "") for card in cards_for_db if card.get("summary")
            )
            event_writer({"name":"generate_report", "report_summary": cumulative_summary})

            try:
                if not cumulative_summary:
                    logger.warning(f"Invalid cumulative summary for executive summary generation for user: {self.user_name} - chat_id: {self.chat_id}")
                else:
                    # Old approach: refine_cumulative_summary(cumulative_summary) - took a string
                    # New approach: pass cards_for_db directly, function extracts summaries internally
                    executive_summary = await asyncio.to_thread(
                        refine_cumulative_summary,
                        cards_for_db,
                        chat_id=self.chat_id,
                        user_id=self.user_id or self.user_name,
                    )
                
                logger.info(f"Executive summary generated successfully for user: {self.user_name} - chat_id: {self.chat_id}")
            except Exception as e:
                logger.error(f"Error generating executive summary: {str(e)} for user: {self.user_name} - chat_id: {self.chat_id}")
                executive_summary = None
            # cards_for_db.insert(3, {'executive_summary': f"Executive Summary\n\n{executive_summary}"})
            if executive_summary:
                cards_for_db.insert(3, {'section': [
                    {
                        "name": "executive_summary",
                        "content": executive_summary,
                        "tables": [
                            {
                                "visualization": "",
                                "table_id": "",
                                "table_title": "",
                                "visualization_type": ""
                            }
                        ],
                        "id": str(uuid7()),
                        "summary": ""
                    }
                ],
                "sub_sections": [
                    {
                        "name": "",
                        "content": "",
                        "tables": [
                            {
                                "visualization": "",
                                "table_id": "",
                                "table_title": "",
                                "visualization_type": ""
                            }
                        ],
                        "id": str(uuid7()),
                        "summary": ""
                    }
                ],
                "citations": {},
                "summary": ""
                })
                event_writer({"name": "generate_report", "card_db": {"section": [{"name": "executive_summary", "content": executive_summary}]}, "card_type":"es"})
            event_writer({"name": "generate_report", "table_and_table_id_map": table_and_table_id_map})
            event_writer({"name": "generate_report", "status": "card_stream_complete"})
            logger.info(f"Converting cards_for_db to markdown for user: {self.user_name} - chat_id: {self.chat_id}")
            md_content = await asyncio.to_thread(convert_json_to_md, cards_for_db, table_and_table_id_map)
            # logger.info(f"Markdown content generated successfully for user: {self.user_name} - chat_id: {self.chat_id}")
            # Upload cards_for_db to S3 as background task
            asyncio.create_task(self._upload_cards_for_db_to_s3_background(cards_for_db, table_and_table_id_map))
            event_writer({"name": "generate_report", "status": "md_content", "md_content": md_content})
            return {"messages": [AIMessage(content=md_content)]}
            # return {"messages": [AIMessage(content=cumulative_summary)]}
            # try:
            #     image_url, image_data = generate_image_with_Google(cards_for_db['section'][0]['content'], self.user_name, self.user_id, self.report_id)
            #     image_name = f"{self.report_title}_{self.report_generation_time.strftime('%Y%m%d_%H%M%S')}.png"
            #     image_data.save(os.path.join(os.getcwd(), "temp", image_name))
            #     prefix = build_report_s3_prefix(user_id=self.user_id, user_name=self.user_name, chat_id=self.chat_id, chat_title=self.chat_title, report_id=self.report_id, version=self.version, when=self.report_generation_time)
            #     image_s3_key = f"{prefix}/report/{image_name}"
            #     poster_image_s3_path = self.s3_instance.upload_file(os.path.join(os.getcwd(), "temp", image_name), image_s3_key)
            #     event_writer({"name": "generate_report", "poster_image_s3_path": poster_image_s3_path})
            #     logger.info(f"Poster image uploaded successfully to S3: {image_s3_key} for user: {self.user_name} - chat_id: {self.chat_id}")
            # except Exception as e:
            #     image_url, image_data = generate_image_with_openai(cards_for_db['section'][0]['content'], self.user_name, self.user_id, self.report_id)
            #     image_name = f"{self.report_title}_{self.report_generation_time.strftime('%Y%m%d_%H%M%S')}.png"
            #     image_data.save(os.path.join(os.getcwd(), "temp", image_name))
            #     prefix = build_report_s3_prefix(user_id=self.user_id, user_name=self.user_name, chat_id=self.chat_id, chat_title=self.chat_title, report_id=self.report_id, version=self.version, when=self.report_generation_time)
            #     image_s3_key = f"{prefix}/report/{image_name}"
            #     poster_image_s3_path = self.s3_instance.upload_file(os.path.join(os.getcwd(), "temp", image_name), image_s3_key)
            #     event_writer({"name": "generate_report", "poster_image_s3_path": poster_image_s3_path})
            #     logger.info(f"Poster image uploaded successfully to S3: {image_s3_key} for user: {self.user_name} - chat_id: {self.chat_id}")
            # finally:
            #     os.remove(os.path.join(os.getcwd(), "temp", image_name))            
            # return {"messages": [AIMessage(content=final_report, usage_metadata=response.usage_metadata)]}
            
        except Exception as e:
            logger.error(f"Error in generate_report: {e} for user: {self.user_name} - chat_id: {self.chat_id}")
            error_msg = AIMessage(content=f"I apologize, but I encountered an error while generating the report. Please try again.")
            event_writer({"name": "generate_report", "error": e})
            return {"messages": [error_msg]}
        
    def _count_turn_tool_calls(self, messages: List) -> Tuple[int, int]:
        """Count how many web-search / document-query ToolMessages have already
        been produced in the CURRENT turn (since the last human message).

        Mirrors the counting logic used in ``report_or_respond`` so the
        sequential dispatcher can enforce the same per-turn caps as it iterates
        over a multi-call AIMessage.
        """
        last_human_idx = -1
        for idx, m in enumerate(messages):
            if getattr(m, "type", None) == "human":
                last_human_idx = idx

        web_used = 0
        doc_used = 0
        for m in messages[last_human_idx + 1:]:
            if getattr(m, "type", None) != "tool":
                continue
            name = getattr(m, "name", "")
            if name == "retrieve_latest_info":
                web_used += 1
            elif name == "query_document":
                doc_used += 1
        return web_used, doc_used

    def _gate_node(self, state: MessagesState) -> Dict[str, list]:
        """
        Pre-execution gate — classifies the assistant's tool step and enforces
        the ONE exclusivity rule we still care about: report generation
        (``retrieve``) must run alone.

        Behaviour:
          - 0 or 1 tool calls: pass through unchanged.
          - 2+ tool calls, NONE of which is ``retrieve``: pass through unchanged
            so the sequential dispatcher can honour every call one-at-a-time
            (each tool_call_id still gets a matching ToolMessage downstream, so
            provider history stays valid).
          - 2+ tool calls that INCLUDE ``retrieve``: report generation is
            exclusive — rewrite the AIMessage (same id so add_messages overwrites
            in place) to keep ONLY the first ``retrieve`` call and drop the rest.

        Routing (web/doc sequential vs retrieve) is decided in ``_route_after_gate``.
        """
        messages = state["messages"]
        last_ai: Optional[AIMessage] = next(
            (m for m in reversed(messages) if isinstance(m, AIMessage)), None
        )

        if last_ai is None:
            return {"messages": []}

        tool_calls = [
            _normalize_tool_call(c)
            for c in (getattr(last_ai, "tool_calls", []) or [])
        ]

        if len(tool_calls) <= 1:
            return {"messages": []}

        retrieve_calls = [c for c in tool_calls if c.get("name") == "retrieve"]
        if not retrieve_calls:
            # All read tools — honour them all, run sequentially downstream.
            names = ", ".join(c.get("name", "?") for c in tool_calls)
            logger.info(
                f"[gate] Model requested {len(tool_calls)} parallel read-tool calls "
                f"({names}) — passing through to sequential dispatcher | "
                f"user: {self.user_name} - chat_id: {self.chat_id}"
            )
            return {"messages": []}

        # retrieve is exclusive: keep only the first retrieve call, drop the rest.
        names = ", ".join(c.get("name", "?") for c in tool_calls)
        logger.warning(
            f"[gate] retrieve is exclusive — collapsing {len(tool_calls)}-tool step "
            f"({names}) to a single retrieve call | "
            f"user: {self.user_name} - chat_id: {self.chat_id}"
        )
        collapsed_ai = AIMessage(
            content=last_ai.content,
            id=last_ai.id,
            tool_calls=[retrieve_calls[0]],
        )
        return {"messages": [collapsed_ai]}

    def _route_after_report_or_respond(self, state: MessagesState) -> str:
        """
        Conditional edge leaving report_or_respond — two outcomes:
          - "gate"             : last AI message has tool calls → classify the step.
          - END                : no tool calls → final answer, graph ends here.
        """
        messages = state["messages"]
        last_ai: Optional[AIMessage] = next(
            (m for m in reversed(messages) if isinstance(m, AIMessage)), None
        )
        if last_ai is not None and getattr(last_ai, "tool_calls", []):
            return "gate"
        return END

    def _route_after_gate(self, state: MessagesState) -> str:
        """
        Conditional edge after gate — three outcomes based on the (possibly
        collapsed) last AIMessage:
          - "tools"            : the step contains ``retrieve`` → prebuilt ToolNode
                                 (handles the content_and_artifact tuple).
          - "sequential_tools" : one or more read tools (retrieve_latest_info /
                                 query_document) → run them sequentially with
                                 per-call heartbeats.
          - "report_or_respond": no tool calls remain → loop back to the LLM.
        """
        messages = state["messages"]
        last_ai: Optional[AIMessage] = next(
            (m for m in reversed(messages) if isinstance(m, AIMessage)), None
        )
        if last_ai is None:
            return "report_or_respond"

        tool_calls = getattr(last_ai, "tool_calls", []) or []
        if not tool_calls:
            return "report_or_respond"

        if any((c.get("name") if isinstance(c, dict) else getattr(c, "name", None)) == "retrieve"
               for c in tool_calls):
            return "tools"
        return "sequential_tools"

    async def _dispatch_read_tool(self, name: str, args: Dict[str, Any]) -> str:
        """Invoke a single read tool (retrieve_latest_info / query_document) and
        return its string result.  Called from the sequential dispatcher so each
        tool's own start/heartbeat/end events stream fully before the next begins.
        """
        if name == "retrieve_latest_info":
            return await self.retrieve_latest_info(
                user_query=args.get("user_query", ""),
                status_message=args.get("status_message", ""),
                progress_updates=args.get("progress_updates"),
            )
        if name == "query_document":
            return await self.query_document(
                user_query=args.get("user_query", ""),
                status_message=args.get("status_message", ""),
                progress_updates=args.get("progress_updates"),
            )
        logger.warning(
            f"[sequential_tools] Unknown read tool '{name}' — returning error stub | "
            f"user: {self.user_name} - chat_id: {self.chat_id}"
        )
        return GENERIC_TOOL_ERROR_MSG

    async def _sequential_tools_node(self, state: MessagesState) -> Dict[str, list]:
        """
        Execute the read-tool calls in the last AIMessage STRICTLY one at a time,
        in the order the model emitted them.  Because each call is awaited before
        the next begins, its start/heartbeat/end events stream as a clean,
        non-interleaved block — the user sees tool A's narration fully, then tool
        B's.  Every tool_call_id receives exactly one matching ToolMessage
        (skipped calls get a stub) so provider history stays valid.

        Per-turn caps (MAX_WEB_SEARCH_CALLS_PER_TURN / MAX_QUERY_DOC_CALLS_PER_TURN)
        are enforced as we iterate, honouring all calls up to the limit and
        stubbing the rest.
        """
        messages = state["messages"]
        last_ai: Optional[AIMessage] = next(
            (m for m in reversed(messages) if isinstance(m, AIMessage)), None
        )
        if last_ai is None:
            logger.warning(
                f"[sequential_tools] No AIMessage found in state — nothing to dispatch | "
                f"user: {self.user_name} - chat_id: {self.chat_id}"
            )
            return {"messages": []}

        calls = [
            _normalize_tool_call(c)
            for c in (getattr(last_ai, "tool_calls", []) or [])
        ]

        web_used, doc_used = self._count_turn_tool_calls(messages)

        call_names = [c.get("name", "?") for c in calls]
        is_parallel = len(calls) > 1
        logger.info(
            f"[sequential_tools] Dispatching {len(calls)} read-tool call(s) "
            f"sequentially{' (PARALLEL batch from model)' if is_parallel else ''}: "
            f"{call_names} | prior_turn_usage: web={web_used}/{MAX_WEB_SEARCH_CALLS_PER_TURN}, "
            f"doc={doc_used}/{MAX_QUERY_DOC_CALLS_PER_TURN} | "
            f"user: {self.user_name} - chat_id: {self.chat_id}"
        )

        batch_started = time.time()
        ran = 0
        skipped = 0
        errored = 0

        out_messages: List[ToolMessage] = []
        for position, call in enumerate(calls, start=1):
            name = call.get("name", "")
            call_id = call.get("id", "")
            args = call.get("args", {}) or {}
            query_preview = str(args.get("user_query", ""))[:120]

            if name == "retrieve_latest_info" and web_used >= MAX_WEB_SEARCH_CALLS_PER_TURN:
                skipped += 1
                logger.warning(
                    f"[sequential_tools] Skipping call {position}/{len(calls)} "
                    f"retrieve_latest_info — per-turn limit reached "
                    f"({web_used}/{MAX_WEB_SEARCH_CALLS_PER_TURN}) | query='{query_preview}' | "
                    f"user: {self.user_name} - chat_id: {self.chat_id}"
                )
                out_messages.append(ToolMessage(
                    content="Note: the latest-information lookup limit for this turn has been "
                            "reached. Use the information already gathered to answer.",
                    tool_call_id=call_id,
                    name=name,
                ))
                continue

            if name == "query_document" and doc_used >= MAX_QUERY_DOC_CALLS_PER_TURN:
                skipped += 1
                logger.warning(
                    f"[sequential_tools] Skipping call {position}/{len(calls)} "
                    f"query_document — per-turn limit reached "
                    f"({doc_used}/{MAX_QUERY_DOC_CALLS_PER_TURN}) | query='{query_preview}' | "
                    f"user: {self.user_name} - chat_id: {self.chat_id}"
                )
                out_messages.append(ToolMessage(
                    content="Note: the document-lookup limit for this turn has been reached. "
                            "Use the information already gathered to answer.",
                    tool_call_id=call_id,
                    name=name,
                ))
                continue

            logger.info(
                f"[sequential_tools] Running call {position}/{len(calls)} '{name}' | "
                f"query='{query_preview}' | "
                f"user: {self.user_name} - chat_id: {self.chat_id}"
            )
            call_started = time.time()
            try:
                result = await self._dispatch_read_tool(name, args)
                elapsed = time.time() - call_started
                result_len = len(result) if isinstance(result, str) else len(str(result))
                ran += 1
                logger.info(
                    f"[sequential_tools] Completed call {position}/{len(calls)} '{name}' in "
                    f"{elapsed:.1f}s | result_chars={result_len} | "
                    f"user: {self.user_name} - chat_id: {self.chat_id}"
                )
            except Exception as e:
                elapsed = time.time() - call_started
                errored += 1
                logger.error(
                    f"[sequential_tools] Call {position}/{len(calls)} '{name}' raised after "
                    f"{elapsed:.1f}s: {e} | query='{query_preview}' | "
                    f"user: {self.user_name} - chat_id: {self.chat_id}",
                    exc_info=True,
                )
                result = GENERIC_TOOL_ERROR_MSG

            out_messages.append(ToolMessage(
                content=result if isinstance(result, str) else str(result),
                tool_call_id=call_id,
                name=name,
            ))

            if name == "retrieve_latest_info":
                web_used += 1
            elif name == "query_document":
                doc_used += 1

        batch_elapsed = time.time() - batch_started
        logger.info(
            f"[sequential_tools] Batch complete in {batch_elapsed:.1f}s | "
            f"requested={len(calls)}, ran={ran}, skipped={skipped}, errored={errored} | "
            f"turn_usage_after: web={web_used}/{MAX_WEB_SEARCH_CALLS_PER_TURN}, "
            f"doc={doc_used}/{MAX_QUERY_DOC_CALLS_PER_TURN} | "
            f"user: {self.user_name} - chat_id: {self.chat_id}"
        )
        return {"messages": out_messages}

    def _route_after_tools(self, state: MessagesState) -> str:
        """Route after tools based on which tool was called.
        
        If retrieve tool was called, go to domain_router.
        If document query tools or info retrieval tool were called, go back to report_or_respond.
        """
        messages = state["messages"]
        last_message = messages[-1]
        
        if hasattr(last_message, 'name'):
            tool_name = last_message.name
            logger.info(
                f"[route_after_tools] Tool completed: tool_name='{tool_name}' | "
                f"domain_name='{self.domain_name}' | "
                f"user: {self.user_name} - chat_id: {self.chat_id}"
            )
            if tool_name == "retrieve":
                logger.info(
                    f"[route_after_tools] Routing to domain_router (domain_name='{self.domain_name}') | "
                    f"user: {self.user_name} - chat_id: {self.chat_id}"
                )
                return "domain_router"
            elif tool_name in ["query_document"]:
                logger.info(f"[route_after_tools] Routing back to report_or_respond after '{tool_name}' | user: {self.user_name} - chat_id: {self.chat_id}")
                return "report_or_respond"
            elif tool_name == "retrieve_latest_info":
                logger.info(f"Routing back to report_or_respond after {tool_name} tool for user: {self.user_name} - chat_id: {self.chat_id}")
                return "report_or_respond"
        
        logger.warning(f"[route_after_tools] No tool name found on last message — defaulting to report_or_respond | user: {self.user_name} - chat_id: {self.chat_id}")
        return "report_or_respond"

    async def domain_router(self, state: MessagesState) -> MessagesState:
        """Pass-through node that sits between tools and the domain-specific
        subgraph.  The actual routing decision is made by ``_route_by_domain``
        on the conditional edge leaving this node.

        Note: This node intentionally performs no DB writes. The ``domain_name``
        is emitted via the ``retrieve`` event in ``retrieve()`` and the API
        layer is responsible for persisting it to the ``reports`` table."""
        logger.info(
            f"[domain_router] Entered domain router | domain_name='{self.domain_name}' | "
            f"available_domains=['default', 'primary_research', 'due_diligence', "
            f"'industry_benchmarking', 'market_insight', 'rfp', 'business_plan'] | "
            f"retrieve_config_keys={list(self.retrieve_config.keys())} | "
            f"user: {self.user_name} - chat_id: {self.chat_id}"
        )
        return state

    def _route_by_domain(self, state: MessagesState) -> str:
        """Conditional edge function: dispatch to the correct domain subgraph
        based on ``self.domain_name``.
        
        Specialized domains (primary_research, due_diligence) have their own
        subgraphs. All other domains (industry_benchmarking, market_insight,
        rfp, business_plan, default) use the standard report pipeline."""
        domain = self.domain_name
        if domain == "primary_research":
            logger.info(
                f"[domain_router] Dispatching to PRIMARY_RESEARCH subgraph | "
                f"has_upload_files={self._has_uploaded_documents()} | "
                f"user: {self.user_name} - chat_id: {self.chat_id}"
            )
            return "primary_research"
        if domain == "due_diligence":
            logger.info(
                f"[domain_router] Dispatching to DUE_DILIGENCE subgraph | "
                f"entity='{self.retrieve_config.get('report_title', 'N/A')}' | "
                f"user: {self.user_name} - chat_id: {self.chat_id}"
            )
            return "due_diligence"
        logger.info(
            f"[domain_router] Dispatching to DEFAULT report generation pipeline "
            f"(domain_name='{domain}') | "
            f"user: {self.user_name} - chat_id: {self.chat_id}"
        )
        return "default"

    async def run_primary_research(self, state: MessagesState) -> Dict[str, List]:
        """Wrapper that bridges MessagesState -> PrimaryResearchState, runs the
        PR subgraph, and returns the result as MessagesState."""
        try:
            event_writer = get_stream_writer()

            tool_messages = self._get_recent_tool_messages(state["messages"])
            if not tool_messages:
                logger.error(f"[primary_research] No retrieve tool message found for user: {self.user_name} - chat_id: {self.chat_id}")
                error_msg = AIMessage(content="I apologize, but I encountered an error while generating the report. Please try again.")
                event_writer({"name": "generate_report", "error": "No retrieve tool message"})
                return {"messages": [error_msg]}

            pr_initial_state = {
                "upload_file_config": self.upload_file_config,
                "grep_session": self.grep_session,
                "user_instructions": self.retrieve_config.get('user_instructions', ''),
                "report_layout": self.retrieve_config.get('report_layout', ''),
                "report_length": self.retrieve_config.get('report_type', 'study').upper(),
                "report_type": self.retrieve_config.get('report_type', 'study'),
                "report_title": self.retrieve_config.get('report_title', ''),
                "report_language": self.retrieve_config.get('report_language', 'English'),
                "user_name": self.user_name,
                "chat_id": self.chat_id,
                "user_id": self.user_id or self.user_name,
                "web_search": False,
                "s3_instance": self.s3_instance,
            }

            logger.info(
                f"[primary_research] Invoking PR subgraph | "
                f"report_title='{pr_initial_state['report_title']}' | "
                f"report_type='{pr_initial_state['report_length']}' | "
                f"report_language='{pr_initial_state['report_language']}' | "
                f"has_upload_config={bool(self.upload_file_config)} | "
                f"file_ids={self.upload_file_config.get('file_ids') if self.upload_file_config else None} | "
                f"web_search=False | "
                f"user: {self.user_name} - chat_id: {self.chat_id}"
            )

            # Stream the subgraph instead of invoking it so that custom events
            # emitted via get_stream_writer() inside subgraph nodes are forwarded
            # to the parent graph's stream. Without this, LangGraph isolates the
            # subgraph's writer and the frontend would see no progress events
            # between `domain_name` and the final report.
            final_state: Dict = {}
            async for sub_mode, sub_data in self._pr_subgraph.astream(
                pr_initial_state,
                stream_mode=["values", "custom"],
            ):
                if sub_mode == "custom":
                    event_writer(sub_data)
                elif sub_mode == "values":
                    final_state = sub_data

            if final_state.get("error") and not final_state.get("final_message"):
                error_text = final_state["error"]
                logger.info(
                    f"[primary_research] Subgraph completed with error: {error_text} | "
                    f"user: {self.user_name} - chat_id: {self.chat_id}"
                )
                event_writer({"name": "generate_report", "status": "card_stream_complete"})
                event_writer({"name": "generate_report", "status": "md_content", "md_content": error_text})
                return {"messages": [AIMessage(content=error_text)]}

            ai_message = final_state.get("final_message")
            if ai_message:
                logger.info(
                    f"[primary_research] Subgraph completed successfully | "
                    f"output_length={len(ai_message.content)} chars | "
                    f"user: {self.user_name} - chat_id: {self.chat_id}"
                )
                return {"messages": [ai_message]}

            logger.info(
                f"[primary_research] Subgraph returned no final_message and no error | "
                f"user: {self.user_name} - chat_id: {self.chat_id}"
            )
            error_msg = AIMessage(content="I apologize, but I encountered an error while generating the report. Please try again.")
            event_writer({"name": "generate_report", "error": "No final message from PR subgraph"})
            event_writer({"name": "generate_report", "status": "card_stream_complete"})
            event_writer({"name": "generate_report", "status": "md_content", "md_content": error_msg.content})
            return {"messages": [error_msg]}

        except Exception as e:
            logger.error(f"[primary_research] Error in run_primary_research: {e} for user: {self.user_name} - chat_id: {self.chat_id}")
            event_writer = get_stream_writer()
            event_writer({"name": "generate_report", "error": str(e)})
            event_writer({"name": "generate_report", "status": "card_stream_complete"})
            error_msg = AIMessage(content="I apologize, but I encountered an error while generating the report. Please try again.")
            event_writer({"name": "generate_report", "status": "md_content", "md_content": error_msg.content})
            return {"messages": [error_msg]}
    
    async def run_due_diligence(self, state: MessagesState) -> Dict[str, List]:
        """Wrapper that bridges MessagesState -> DueDiligenceState, runs the
        DD subgraph, and returns the result as MessagesState."""
        try:
            event_writer = get_stream_writer()

            tool_messages = self._get_recent_tool_messages(state["messages"])
            if not tool_messages:
                logger.error(f"[due_diligence] No retrieve tool message found for user: {self.user_name} - chat_id: {self.chat_id}")
                error_msg = AIMessage(content="I apologize, but I encountered an error while generating the report. Please try again.")
                event_writer({"name": "generate_report", "error": "No retrieve tool message"})
                return {"messages": [error_msg]}

            dd_initial_state = {
                "upload_file_config": self.upload_file_config,
                "grep_session": self.grep_session,
                "user_instructions": self.retrieve_config.get('user_instructions', ''),
                "report_layout": self.retrieve_config.get('report_layout', ''),
                "report_length": self.retrieve_config.get('report_type', 'study').upper(),
                "report_type": self.retrieve_config.get('report_type', 'study'),
                "report_title": self.retrieve_config.get('report_title', ''),
                "report_language": self.retrieve_config.get('report_language', 'English'),
                "user_name": self.user_name,
                "chat_id": self.chat_id,
                "user_id": self.user_id or self.user_name,
                "web_search": True,
                "entity_name": self.retrieve_config.get('report_title', ''),
                "s3_instance": self.s3_instance,
                "mcp_server_url": self.mcp_server_url,
            }

            logger.info(
                f"[due_diligence] Invoking DD subgraph | "
                f"entity_name='{dd_initial_state['entity_name']}' | "
                f"report_title='{dd_initial_state['report_title']}' | "
                f"report_type='{dd_initial_state['report_length']}' | "
                f"report_language='{dd_initial_state['report_language']}' | "
                f"has_upload_config={bool(self.upload_file_config)} | "
                f"file_ids={self.upload_file_config.get('file_ids') if self.upload_file_config else None} | "
                f"web_search=True | "
                f"user: {self.user_name} - chat_id: {self.chat_id}"
            )

            # Stream the subgraph instead of invoking it so that custom events
            # emitted via get_stream_writer() inside subgraph nodes are forwarded
            # to the parent graph's stream. Without this, LangGraph isolates the
            # subgraph's writer and the frontend would see no progress events
            # between `domain_name` and the final report.
            final_state: Dict = {}
            async for sub_mode, sub_data in self._dd_subgraph.astream(
                dd_initial_state,
                stream_mode=["values", "custom"],
            ):
                if sub_mode == "custom":
                    event_writer(sub_data)
                elif sub_mode == "values":
                    final_state = sub_data

            if final_state.get("error") and not final_state.get("final_message"):
                error_text = final_state["error"]
                logger.info(
                    f"[due_diligence] Subgraph completed with error: {error_text} | "
                    f"user: {self.user_name} - chat_id: {self.chat_id}"
                )
                event_writer({"name": "generate_report", "status": "card_stream_complete"})
                event_writer({"name": "generate_report", "status": "md_content", "md_content": error_text})
                return {"messages": [AIMessage(content=error_text)]}

            ai_message = final_state.get("final_message")
            if ai_message:
                logger.info(
                    f"[due_diligence] Subgraph completed successfully | "
                    f"output_length={len(ai_message.content)} chars | "
                    f"user: {self.user_name} - chat_id: {self.chat_id}"
                )
                return {"messages": [ai_message]}

            logger.info(
                f"[due_diligence] Subgraph returned no final_message and no error | "
                f"user: {self.user_name} - chat_id: {self.chat_id}"
            )
            error_msg = AIMessage(content="I apologize, but I encountered an error while generating the report. Please try again.")
            event_writer({"name": "generate_report", "error": "No final message from DD subgraph"})
            event_writer({"name": "generate_report", "status": "card_stream_complete"})
            event_writer({"name": "generate_report", "status": "md_content", "md_content": error_msg.content})
            return {"messages": [error_msg]}

        except Exception as e:
            logger.error(f"[due_diligence] Error in run_due_diligence: {e} for user: {self.user_name} - chat_id: {self.chat_id}")
            event_writer = get_stream_writer()
            event_writer({"name": "generate_report", "error": str(e)})
            event_writer({"name": "generate_report", "status": "card_stream_complete"})
            error_msg = AIMessage(content="I apologize, but I encountered an error while generating the report. Please try again.")
            event_writer({"name": "generate_report", "status": "md_content", "md_content": error_msg.content})
            return {"messages": [error_msg]}

    async def _build_graph(self):
        """Build the LangGraph workflow."""
        try:
            graph_builder = StateGraph(MessagesState)

            retrieve_tool = tool(response_format="content_and_artifact")(self.retrieve)
            query_doc_tool = tool(self.query_document)
            web_search_tool = tool(self.retrieve_latest_info)

            graph_builder.add_node("report_or_respond", self.report_or_respond)
            if not self._has_uploaded_documents():
                tools = [retrieve_tool, web_search_tool]
            else:
                tools = [retrieve_tool, query_doc_tool, web_search_tool]
            graph_builder.add_node("tools", ToolNode(tools))
            graph_builder.add_node("sequential_tools", self._sequential_tools_node)
            graph_builder.add_node("generate_report", self.generate_report)
            graph_builder.add_node("domain_router", self.domain_router)

            self._pr_subgraph = build_primary_research_subgraph()
            graph_builder.add_node("pr_subgraph", self.run_primary_research)

            self._dd_subgraph = build_due_diligence_subgraph()
            graph_builder.add_node("dd_subgraph", self.run_due_diligence)

            logger.info(
                f"[build_graph] Domain subgraphs registered: "
                f"['default/industry_benchmarking/market_insight/rfp/business_plan' -> generate_report, "
                f"'primary_research' -> pr_subgraph, 'due_diligence' -> dd_subgraph] | "
                f"tools_registered={['retrieve', 'query_document', 'retrieve_latest_info'] if self._has_uploaded_documents() else ['retrieve', 'retrieve_latest_info']} | "
                f"user: {self.user_name} - chat_id: {self.chat_id}"
            )
            
            graph_builder.set_entry_point("report_or_respond")

            # report_or_respond routes to gate when tool calls are present,
            # or directly to END when the LLM produces a final answer.
            # gate classifies the step: it collapses a retrieve step to a single
            # call (report generation is exclusive) and otherwise leaves read-tool
            # calls intact.  _route_after_gate then dispatches:
            #   - retrieve  -> "tools" (prebuilt ToolNode, handles the artifact)
            #   - read tools-> "sequential_tools" (run one-at-a-time with heartbeats)
            # The graph therefore always ends on report_or_respond, never on gate.
            graph_builder.add_node("gate", self._gate_node)
            graph_builder.add_conditional_edges(
                "report_or_respond",
                self._route_after_report_or_respond,
                {"gate": "gate", END: END},
            )
            graph_builder.add_conditional_edges(
                "gate",
                self._route_after_gate,
                {
                    "tools": "tools",
                    "sequential_tools": "sequential_tools",
                    "report_or_respond": "report_or_respond",
                },
            )

            # Read tools always loop back to report_or_respond so the model can
            # reason over the gathered results (and optionally search again).
            graph_builder.add_edge("sequential_tools", "report_or_respond")
            
            # Route after tools: retrieve -> domain_router, others -> loop
            graph_builder.add_conditional_edges(
                "tools",
                self._route_after_tools,
                {
                    "domain_router": "domain_router",
                    "report_or_respond": "report_or_respond"
                }
            )

            # Domain router dispatches to the correct subgraph
            graph_builder.add_conditional_edges(
                "domain_router",
                self._route_by_domain,
                {
                    "default": "generate_report",
                    "primary_research": "pr_subgraph",
                    "due_diligence": "dd_subgraph",
                }
            )
            
            graph_builder.add_edge("generate_report", END)
            graph_builder.add_edge("pr_subgraph", END)
            graph_builder.add_edge("dd_subgraph", END)
            
            # Compile graph
            return graph_builder.compile()
            
        except Exception as e:
            logger.error(f"Error building graph: {e} for user: {self.user_name} - chat_id: {self.chat_id}")
            raise
        
    async def generate_chat_title(self, user_query: str, ai_response: str) -> str:
        """Generate a chat title based on the first exchange between a user and Caspr."""
        logger.info(f"Generating chat title for user: {self.user_name} - chat_id: {self.chat_id}")
        chat_title_schema = {
                "type": "function",
                "function": {
                    "name": "create_chat_title",
                    "description": "Generates a short and meaningful chat title (max 40 characters) based on a conversation in English only",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "title": {
                                "type": "string",
                                "description": "Concise title summarizing the conversation (max 40 characters) in English only"
                            }
                        },
                        "required": ["title"]
                    }
                }
            }
        try:
            # raise Exception("test")
            title_prompt = ChatPromptTemplate.from_template(template=CHAT_TITLE_PROMPT)
            title_prompt = title_prompt.format(user_query=user_query, ai_response=ai_response[:500]+'...')

            response = await ASYNC_OPENAI_CLIENT.chat.completions.create(
                model=CHAT_TITLE_MODEL,
                messages=[{"role": "user", "content": title_prompt}],
                tools=[chat_title_schema],
                tool_choice={"type": "function", "function": {"name": "create_chat_title"}}
            )
            save_raw_llm_response(response, CHAT_TITLE_MODEL, "create_chat_title", self.chat_id, user_id=self.user_id or self.user_name)

            tool_call = response.choices[0].message.tool_calls[0]
            structured_json = json.loads(tool_call.function.arguments)
            title = structured_json.get('title', '')
            if not title:
                raise Exception("No title generated")

            return title
        
        except Exception as e:
            logger.error(f"Error generating chat title using openai strict json: {e}")
            logger.info("Falling back to pydantic structured output.")

            try:
                title_prompt = ChatPromptTemplate.from_template(template=CHAT_TITLE_PROMPT)
                title_prompt = title_prompt.format(user_query=user_query, ai_response=ai_response[:50]+'...')
                title_response = await STRUCTURED_LLM.ainvoke(title_prompt)
                save_raw_llm_response(title_response["raw"], ANTHROPIC_MODEL_ID, "Creating a title for the chat", self.chat_id, user_id=self.user_id or self.user_name)
                if title_response["parsing_error"] is not None:
                    raise title_response["parsing_error"]
                return title_response["parsed"].chat_title
            except Exception as e:
                logger.error(f"Error generating chat title: {e}")
                logger.info("Falling back to simple naive title generation prompt.")
                try:
                    fallback_response = await ASYNC_OPENAI_CLIENT.chat.completions.create(
                        model=CHAT_TITLE_MODEL,
                        messages=[
                            {"role": "system", "content": "Answer in 5 words or less."},
                            {"role": "user", "content": f"Create a chat title for this chat conversation between a user and a report generator AI assitant \nUser: {user_query}\nAssistant: {ai_response[:50]+'...'}"}
                        ],
                        tools=[chat_title_schema],
                        tool_choice={"type": "function", "function": {"name": "create_chat_title"}}
                    )
                    save_raw_llm_response(fallback_response, CHAT_TITLE_MODEL, "create_chat_title_backup", self.chat_id, user_id=self.user_id or self.user_name)

                    tool_call = fallback_response.choices[0].message.tool_calls[0]
                    structured_json = json.loads(tool_call.function.arguments)
                    title_fallback_response = structured_json.get('title', '').replace("'", "").replace('"', '').strip()
                    if not title_fallback_response:
                        raise Exception("No title generated")
                    return title_fallback_response

                    # fallback_prompt_messages = [SystemMessage(content="Answer in 5 words or less.")]
                    # title_fallback_response = await LLM.ainvoke(fallback_prompt_messages + [
                    #     HumanMessage(content=f"Create a chat title for this chat conversation between a user and a report generator AI assitant \nUser: {user_query}\nAssistant: {ai_response[:50]+'...'}")
                    # ]
                    # )
                    # title_fallback_response = title_fallback_response.content[0]['text'].replace("'", "").replace('"','').strip()
                    # return title_fallback_response
                except Exception as e:
                    logger.error(f"Error generating chat title in fallback: {e}")
                    logger.info("Falling back to Gemini API for chat title.")
                    try:
                        chat_title_schema_gemini = {
                            "type": "object",
                            "properties": {
                                "title": {
                                    "type": "string",
                                    "description": "Concise title summarizing the conversation (max 40 characters) in English only"
                                }
                            },
                            "required": ["title"]
                        }
                        gemini_title_prompt = f"Answer in 5 words or less.\nCreate a chat title for this chat conversation between a user and a report generator AI assitant \nUser: {user_query}\nAssistant: {ai_response[:50]+'...'}"
                        gemini_client = genai.Client(api_key=GEMINI_API_KEY)
                        interaction = gemini_client.interactions.create(
                            model=GEMINI_CHAT_TITLE_MODEL,
                            input=gemini_title_prompt,
                            response_format={
                                "type": "text",
                                "mime_type": "application/json",
                                "schema": chat_title_schema_gemini,
                            },
                            generation_config={
                                "temperature": 0.1,
                                "thinking_config": {"thinking_budget": 0},
                            },
                        )
                        save_raw_llm_response(interaction, GEMINI_CHAT_TITLE_MODEL, "create_chat_title_backup_gemini", self.chat_id, user_id=self.user_id or self.user_name)
                        result = json.loads(strip_json_code_fence(interaction.output_text))
                        gemini_title = (result.get("title") or "").replace("'", "").replace('"', '').strip()
                        if not gemini_title:
                            raise Exception("No title generated by Gemini")
                        return gemini_title
                    except Exception as gemini_e:
                        logger.error(f"Error generating chat title with Gemini fallback: {gemini_e}")
                        return ai_response[:30]
            

    async def get_processing_state(self, query: str) -> AsyncGenerator[Tuple[str, Dict], None]:
        """Returns the state to be processed"""
        try:

            chat_messages = []

            if not query or not isinstance(query, str):
                logger.error("Invalid query provided")
                return {"error": "Invalid query provided"}
            
            input_message = {
                "type": "human",
                "content": query.strip()
            }

            if not self.user_previous_messages:
                # is_first_query = True
                chat_messages = [self.system_message.model_dump()] + [input_message]
                logger.info(f"Starting new chat for user: {self.user_name} - chat_id: {self.chat_id}")
            else:
                # is_first_query = False

                should_include = [True]*len(self.user_previous_messages)

                # removing all tool call messages and their corresponding human messages where report was not generated
                for idx, msg in enumerate(self.user_previous_messages):
                    _meta = msg.get('response_metadata', {})
                    stop_reason = _meta.get('stopReason', '') or _meta.get('stop_reason', '') or _meta.get('finish_reason', '')
                    if msg['type'] == 'ai' and stop_reason in ('tool_use', 'tool_calls'):

                        if idx+1 < len(self.user_previous_messages) and self.user_previous_messages[idx+1]['type'] == 'tool':

                            # Advance past all consecutive tool messages (handles parallel tool calls)
                            end = idx + 1
                            while end < len(self.user_previous_messages) and self.user_previous_messages[end]['type'] == 'tool':
                                end += 1

                            if end < len(self.user_previous_messages) and self.user_previous_messages[end]['type'] == 'ai':
                                continue
                            else:
                                logger.info(f"tool_use message did not have a corresponding end_turn message, removing it from the chat history for user: {self.user_name} - chat_id: {self.chat_id}")
                                should_include[idx-1:end] = [False] * (end - (idx - 1))

                        else:
                            logger.info(f"tool_use message did not have a corresponding tool type message, removing it from the chat history for user: {self.user_name} - chat_id: {self.chat_id}")
                            # del self.user_previous_messages[idx-1:idx+1]
                            should_include[idx-1:idx+1] = [False]*2

                    elif msg['type'] == 'human':
                        if idx+1 < len(self.user_previous_messages) and self.user_previous_messages[idx+1]['type'] != 'ai':
                            logger.info(f"human message did not have a corresponding ai message, removing it from the chat history for user: {self.user_name} - chat_id: {self.chat_id}")
                            should_include[idx] = False

                self.user_previous_messages = [msg for msg, include in zip(self.user_previous_messages, should_include) if include]

                # Strip any leading tool messages — orphaned from a previously corrupted history
                while self.user_previous_messages and self.user_previous_messages[0]['type'] == 'tool':
                    logger.warning(f"Removing orphaned leading tool message from chat history for user: {self.user_name} - chat_id: {self.chat_id}")
                    self.user_previous_messages.pop(0)

                # Check if the first message is a system message
                if self.user_previous_messages and self.user_previous_messages[0]['type'] == "system":
                    self.user_previous_messages = self.user_previous_messages[1:]

                chat_messages = [self.system_message.model_dump()] + self.user_previous_messages + [input_message]
                logger.info(f"Continuing chat with {len(self.user_previous_messages)} previous messages for user: {self.user_name} - chat_id: {self.chat_id}")

            initial_state = {"messages": chat_messages}
            
            return self.graph.astream(
                initial_state,
                stream_mode=["messages","values","updates","custom"]
            )
        except Exception as e:
            # import traceback
            # tb_str = traceback.format_exc()
            logger.error(f"Error in get_processing_state: {e}")
            return {"error": "Error in get_processing_state"}