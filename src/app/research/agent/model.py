"""model.py: Casper backend — composition root.

The ``Casper`` implementation is split across mixins by concern:
``_chat`` (planning/chat), ``_retrieve`` (retrieval + read tools),
``_report`` (report generation), ``_graph`` (LangGraph wiring). Shared state
lives on ``self`` and is initialised here in ``__init__`` /
``_initialize_components``.
"""

import asyncio
import datetime
import os
from typing import Any

from langchain_anthropic import ChatAnthropic

# from langchain_aws import ChatBedrock  # Bedrock disabled — using direct Anthropic API instead
from langchain_core.messages import (
    SystemMessage,
)

# from langchain_core.messages.base import BaseMessage
# from langchain_tavily import TavilySearch
from uuid_utils import uuid7

from app.adapters.grep_agent_2 import PdfSession
from app.adapters.s3 import get_s3_instance

# from app.research.infographics import process_infographics
# from app.deliverables.helper_functions import generate_image_with_Google, generate_image_with_openai
from app.core.constants import (
    ANTHROPIC_API_KEY,
    # ANTHROPIC_MODEL_ID,
    # BEDROCK_LLM_ID,
    # BEDROCK_REPORT_LLM,
    # REPORT_LLM,
    ANTHROPIC_MODEL_ID,
    ANTHROPIC_OUTPUT_CONFIG,
    MCP_URL,
    PRIORITIZE_ARXIV,
    use_grep_file_search,
)
from app.core.logging import setup_logging
from app.observability.llm_response_logger import save_raw_llm_response
from app.research.agent._chat import ChatMixin
from app.research.agent._graph import GraphMixin
from app.research.agent._helpers import (
    _keep_finished_chat_history,
    _normalize_report_config,
    _sibling_tier,
)
from app.research.agent._report import ReportMixin
from app.research.agent._retrieve import RetrieveMixin
from app.research.agent._state import (
    CARD_STYLES,
    DEFAULT_CARD_STYLE,
    GENERIC_TOOL_ERROR_MSG,
    REPORT_TIERS,
    CasperState,
    DocumentQueryResponse,
    SearchQueries,
    UpdatedLayoutSection,
    UpdatedProposedReportLayout,
)
from app.research.prompts.prompt_utils import (
    SYSTEM_MESSAGE,
    get_system_message_with_documents,
)

logger = setup_logging(__name__)

__all__ = [
    "CARD_STYLES",
    "DEFAULT_CARD_STYLE",
    "GENERIC_TOOL_ERROR_MSG",
    "REPORT_TIERS",
    "Casper",
    "CasperState",
    "DocumentQueryResponse",
    "SearchQueries",
    "UpdatedLayoutSection",
    "UpdatedProposedReportLayout",
    "_keep_finished_chat_history",
    "_normalize_report_config",
    "_sibling_tier",
    "save_raw_llm_response",
]


class Casper(ChatMixin, RetrieveMixin, ReportMixin, GraphMixin):
    """A class for the RAG chatbot."""

    def __init__(self, config: dict[str, Any]):
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
        logger.info(
            f"Initializing Casper with user: {config['user_name']} - chat_id: {config['chat_id']} with {len(config['user_previous_messages'])} previous messages"
        )

        # Initialize instance variables
        self.user_name = config["user_name"]
        self.chat_id = config["chat_id"]
        # Set when the caller has already been *charged* for a specific tier
        # (caspr-backend prices `depth` before this ever runs). No longer decides
        # anything: the tier confirmed on the report-config popup is the sole
        # authority (R10), because a selection made with both layouts in view
        # beats a tier priced before either layout existed.
        self.forced_report_type = config.get("forced_report_type")
        # Set by the caller when a report for this chat is still being generated
        # (status ANALYSIS_IN_PROGRESS). When True the graph enters
        # `respond_during_report` instead of `report_or_respond`, so the user can
        # keep chatting while the report is built. `in_progress_report_id` /
        # `in_progress_report_title` give that node something to reference.
        self.report_in_progress = bool(config.get("report_in_progress", False))
        self.in_progress_report_id = config.get("in_progress_report_id")
        self.in_progress_report_title = config.get("in_progress_report_title")
        self.openai_upload_file_config = config.get("upload_file_config")
        persisted_mode = (config.get("file_search_mode") or "").lower().strip()

        # Persisted "openai" skips grep for the rest of the chat (API passes this after a prior fallback).
        if persisted_mode == "openai":
            self.file_search_mode = "openai"
            self.upload_file_config = self.openai_upload_file_config
            self.grep_session: PdfSession | None = None
        elif use_grep_file_search():
            self.file_search_mode = "grep"
            self.upload_file_config = None
            self.grep_session: PdfSession | None = config.get("grep_session")
            logger.info(
                f"[grep_agent_2] Received PdfSession from config: {bool(self.grep_session)} | "
                f"user: {config['user_name']} - chat_id: {config['chat_id']}"
            )
        else:
            self.file_search_mode = "openai"
            self.upload_file_config = self.openai_upload_file_config
            self.grep_session: PdfSession | None = None
        self.web_search = config.get(
            "web_search", True
        )  # Optional: web_search flag to force live info retrieval
        self.domain_name = config.get(
            "domain_name", "default"
        )  # Set dynamically by retrieve() tool, can be overridden in config
        self.domain_id = config.get(
            "domain_id", "default"
        )  # Set dynamically by retrieve() tool, can be overridden in config
        self.mcp_server_url = config.get("mcp_server_url", os.environ.get("DD_MCP_SERVER_URL", ""))
        # self.chat_messages = []
        self.retrieve_config = {}
        self.user_id = config.get("user_id")  # User ID for balance checks
        # Messages of the turn currently being processed, kept so tools that need the
        # user's intent (e.g. the layout refresh) can read it without taking state.
        self._current_turn_messages: list = []
        # Strong references to fire-and-forget tasks so they are not garbage collected
        # mid-flight (asyncio only holds weak references to running tasks).
        self._background_tasks: set = set()
        # Latest web-refreshed proposed layout (structured, as the search returned it),
        # set by update_proposed_report_layout.
        self.updated_proposed_report_layout: UpdatedProposedReportLayout | None = None
        self._layout_refresh_task: asyncio.Task | None = None

        # --- Report-config pause (R1-R4) -----------------------------------
        # The checkpointer thread is scoped to ONE turn, not to the chat. A
        # chat-scoped thread would collide with the history `get_processing_state`
        # rebuilds every turn: the rebuilt list would append on top of the
        # checkpointed one and the `messages` reducer would duplicate everything.
        # A resume passes the paused turn's id back in through config.
        self.turn_thread_id: str = str(config.get("thread_id") or uuid7())
        # Both tiers' layouts, keyed by tier: {"study": {...}, "brief": {...}}.
        # Produced by `layout_pair`, checkpointed, and read back by `report_config`.
        self.report_layout_pair: dict[str, Any] = {}
        # The confirmed submission, once the pause has been resumed (R12).
        self.report_config_submission: dict[str, Any] = {}
        # Who the cards are written for. Investor is the only value v1 accepts,
        # and the default, so nothing has to be confirmed to get the tone (R14).
        self.card_style: str = DEFAULT_CARD_STYLE
        # True when this Casper was built to resume a pause rather than to run a
        # fresh turn. `report_config` re-executes from its first line on resume,
        # so this is what stops it emitting a second pause event.
        self._resuming_report_config: bool = False

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
        self.user_previous_messages = config["user_previous_messages"]
        # self.graph = self._build_graph()

        logger.info(
            f"Initialized Casper | user: {self.user_name} - chat_id: {self.chat_id} | "
            f"previous_messages={len(self.user_previous_messages)} | "
            f"domain_name='{self.domain_name}' | web_search={self.web_search} | "
            f"has_upload_config={bool(self.upload_file_config)} | "
            f"file_search_mode={self.file_search_mode}"
        )

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
            anthropic_key = os.getenv("ANTHROPIC_API_KEY")
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
                logger.info(
                    f"ANTHROPIC_API_KEY configured successfully: ...{anthropic_key[-10:]} for user: {self.user_name} - chat_id: {self.chat_id}"
                )
            else:
                logger.warning(
                    f"ANTHROPIC_API_KEY not found in secrets for user: {self.user_name} - chat_id: {self.chat_id}"
                )

        except Exception as e:
            logger.error(
                f"Error configuring APIs: {e} for user: {self.user_name} - chat_id: {self.chat_id}"
            )

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
                date_today=datetime.datetime.now(datetime.UTC).strftime("%B %d, %Y"),
            )

            # `retrieve_latest_info` is disabled — current-information lookups now happen in
            # the background layout refresh (update_proposed_report_layout), so the model is
            # no longer told it can search. Restore the block below together with the tool
            # binding in report_or_respond / _build_graph to bring the tool back.
            # if self.web_search:
            #     logger.info(f"Live information retrieval mode enabled for user: {self.user_name} - chat_id: {self.chat_id}")
            #     system_msg_content += "\n\n**IMPORTANT - LEARNING BRAIN ENABLED:**\nInternal capability is ACTIVE: use the 'retrieve_latest_info' tool, which performs web search/current-source retrieval, for user questions that benefit from current, real-time, or up-to-date information. Always prioritize the most current and accurate information available.\n\n**USER-FACING FRAMING:**\nTo the user, this capability is Caspr's Learning Brain. Treat all retrieved information, whether from uploaded documents or current external sources, as information surfaced by the Learning Brain.\n\n**CRITICAL - NEVER REVEAL TOOLS OR INTERNAL PROCESSES TO THE USER:**\n- NEVER mention tool names like 'retrieve_latest_info', 'query_document', or any internal tool/process.\n- NEVER say 'web search', 'searching the web', 'I'll use my retrieval tool', 'Let me search', 'I'll query the document', 'Using my information retrieval', etc.\n- If you need a user-visible phrase, say 'I'll tap into the Learning Brain' or 'The Learning Brain found...' without referencing tools, searches, or mechanisms.\n\n**REQUIRED TOOL ARGUMENTS - status_message and progress_updates:**\nEvery call to 'retrieve_latest_info' MUST include:\n- status_message: A short, user-facing sentence describing what is being checked RIGHT NOW, e.g. 'Checking Nvidia\\'s latest AI chip updates' or 'Reading analyst views on GPU demand'. NEVER mention tool names, APIs, web_search, or internal details.\n- progress_updates: An ordered list of 3-4 SHORT, specific lines describing how THIS particular search unfolds, e.g. ['Looking for Nvidia\\'s most recent earnings and product news', 'Reading analyst takes on data-center GPU demand', 'Cross-checking figures across multiple sources', 'Pulling the key numbers together']. Each search can take 10-30 seconds; these lines are shown one at a time while the user waits, so make them concrete to THIS query — never generic filler, never mention tools/APIs/internal details.\n\n**CRITICAL - NEVER INCLUDE SOURCES:**\nDo NOT include any source URLs, 'Sources:' section, or 'References:' section in your response. Never list or mention URLs or source links to the user."

            # Kept from the block above: these two rules are about how Caspr talks, not about
            # the search tool, so they still apply while the tool is disabled.
            system_msg_content += "\n\n**CRITICAL - NEVER REVEAL TOOLS OR INTERNAL PROCESSES TO THE USER:**\n- NEVER mention tool names like 'query_document', or any internal tool/process.\n- NEVER say 'web search', 'searching the web', 'I'll use my retrieval tool', 'Let me search', 'I'll query the document', 'Using my information retrieval', etc.\n- If you need a user-visible phrase, say 'I'll tap into the Learning Brain' or 'The Learning Brain found...' without referencing tools, searches, or mechanisms.\n\n**CRITICAL - NEVER INCLUDE SOURCES:**\nDo NOT include any source URLs, 'Sources:' section, or 'References:' section in your response. Never list or mention URLs or source links to the user."

            if self._has_uploaded_documents():
                file_metadata = (self.upload_file_config or {}).get("file_metadata", [])
                if not file_metadata and self.openai_upload_file_config:
                    file_metadata = self.openai_upload_file_config.get("file_metadata", [])
                if self._uses_grep_file_search():
                    doc_ref = (
                        [s.labels for s in self.grep_session.worker_sessions]
                        if self.grep_session
                        else []
                    )
                    # In grep mode upload_file_config is None so file_metadata is empty above.
                    # Build it from grep_session.labels so the system prompt correctly names
                    # every uploaded document (e.g. "2 documents: 'file1.pdf', 'file2.html'").
                    if not file_metadata and self.grep_session:
                        file_metadata = [{"filename": label} for label in self.grep_session.labels]
                else:
                    doc_ref = (self.upload_file_config or {}).get("file_ids")
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
                logger.info(
                    f"No file_id found, not adding file upload info to system message for user: {self.user_name} - chat_id: {self.chat_id}"
                )
                system_msg_content += "\n\n**IMPORTANT - NO DOCUMENT UPLOADED:**\nThe user has not uploaded any document. Do NOT proactively ask or suggest the user to upload a document — UNLESS the user selects 'Primary Research' as their report type. Primary Research REQUIRES an uploaded document (surveys, interviews, datasets, etc.). If the user chooses Primary Research without uploading a document, you MUST ask them to upload their research data before proceeding. You MUST NOT generate a Primary Research report without an uploaded document. For all other report types, only mention document upload if the user explicitly asks about uploading or referencing a document."

            self.system_message = SystemMessage(content=system_msg_content)

        except Exception as e:
            logger.error(
                f"Error initializing components: {e} for user: {self.user_name} - chat_id: {self.chat_id}"
            )
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
