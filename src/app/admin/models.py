"""ORM models for the admin bounded context.

Moved verbatim from ``app.models.py`` during the R-STRUCT-1 migration.
Column definitions, comments, and relationships are unchanged; only the
declarative base moved, from the module-local ``declarative_base()`` to the
single shared ``app.core.db.Base`` (spec §3.2).

Relationships that point at another context resolve through the shared
registry, which ``app/models.py`` guarantees is fully populated.
"""

from sqlalchemy import (
    CHAR,
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import relationship
from uuid_utils import uuid7

from app.core.db import Base
from app.core.enums import (  # noqa: F401
    FileType,
    FileUploadContext,
    FileUsageType,
    MessageType,
    ReportStatus,
    SubscriptionStatus,
    SubscriptionTier,
    TransactionSource,
    TransactionStatus,
    TransactionType,
    UploadedFileStatus,
)


class CostTracker(Base):
    """
    Append-only log of per-LLM-call token usage for cost tracking.

    Maps to the cost-tracker JSON payload produced by
    ``save_raw_llm_response`` in ``src/core/llm_response_logger.py``.

    Attributes:
        id (str): Unique row id (UUID7 primary key).
        timestamp (datetime): When the LLM call occurred (UTC).
        model_name (str): Model id, e.g. gpt-4o.
        context (str): Call context label, e.g. normal_flow_fallback.
        functionality (str): Stable user-facing functionality bucket (e.g. report_generation).
        agent_name (str): Agent / stage name that made the call.
        chat_id (str): Chat identifier associated with the call.
        user_id (str): User identifier associated with the call.
        usage_metadata (dict): Full provider usage blob (JSONB).
        input_tokens (int): Prompt / input token count.
        output_tokens (int): Completion / output token count.
        estimated_cost (float): Estimated USD cost from llm_cost_calculator.
        cost_details (dict): Full cost breakdown JSON (payload["cost"]).
        created_at (datetime): When this row was inserted (UTC).
    """

    __tablename__ = "costtracker"

    id = Column(
        CHAR(36), primary_key=True, default=lambda: str(uuid7()), unique=True, nullable=False
    )

    # --- payload fields (+ id) ---
    timestamp = Column(
        DateTime(timezone=True),
        nullable=False,
        index=True,
        comment="When the LLM call occurred (UTC)",
    )
    model_name = Column(String(100), nullable=False, index=True, comment="Model id, e.g. gpt-4o")
    context = Column(
        String(255), nullable=True, comment="Call context label, e.g. normal_flow_fallback"
    )
    functionality = Column(
        String(50),
        nullable=True,
        index=True,
        comment="Stable user-facing functionality bucket (see app.observability.functionality_context.Functionality), e.g. report_generation",
    )
    agent_name = Column(String(255), nullable=True, comment="Agent / stage that made the call")
    chat_id = Column(
        String(255), nullable=True, index=True, comment="Chat id associated with the call"
    )
    user_id = Column(
        String(255), nullable=True, index=True, comment="User id associated with the call"
    )
    usage_metadata = Column(
        JSONB, nullable=True, comment="Full provider usage blob from the LLM response"
    )
    input_tokens = Column(Integer, nullable=False, default=0, comment="Prompt / input token count")
    output_tokens = Column(
        Integer, nullable=False, default=0, comment="Completion / output token count"
    )
    estimated_cost = Column(Float, nullable=True, comment="Estimated USD cost for this LLM call")
    cost_details = Column(
        JSONB,
        nullable=True,
        comment="Full cost breakdown from llm_cost_calculator (payload cost block)",
    )
    created_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        comment="When this costtracker row was inserted (UTC)",
    )

    def __repr__(self):
        return (
            f"<CostTracker(id='{self.id}', model_name='{self.model_name}', "
            f"agent_name='{self.agent_name}', input_tokens={self.input_tokens}, "
            f"output_tokens={self.output_tokens}, estimated_cost={self.estimated_cost})>"
        )


class WebSearchEvent(Base):
    """
    One row per web-search invocation — from chat (retrieve_latest_info), card
    generation (generate_cards / brief stream), card refinement (refine-card),
    or Ask Caspr.

    Attributes:
        trigger_source: 'chat', 'card_generation', 'brief', 'card_refinement', or 'ask_caspr'
        user_query:     The exact query string sent to the search engine.
        section_name:   For card_generation/brief/card_refinement/ask_caspr — which report section triggered it.
        model_used:     OpenAI model that issued the search (e.g. 'gpt-4o', 'gpt-5.4').
        total_results_count: Number of URLs returned by the search.
        cited_count:    Number of WebSearchCitation rows with was_cited_in_output=True
                         for this event. For events created before the dedup fix in
                         ``_normalize_links`` (see WebSearchCitation docstring), this can
                         overcount distinct cited sources — treat as an upper bound for
                         historical rows, not an exact count.
    """

    __tablename__ = "web_search_events"

    id = Column(
        CHAR(36), primary_key=True, default=lambda: str(uuid7()), unique=True, nullable=False
    )
    operation_id = Column(
        CHAR(36),
        default=lambda: str(uuid7()),
        nullable=False,
        comment="Correlation ID shared by attempts in one search operation",
    )
    attempt_number = Column(Integer, default=0, server_default="0", nullable=False)
    user_id = Column(
        CHAR(36),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        comment="Reference to user",
    )
    chat_id = Column(
        String(255),
        nullable=True,
        index=True,
        comment="Client-generated chat session ID (e.g. 'chat-67b79e2c') — not an FK",
    )
    report_id = Column(
        CHAR(36),
        ForeignKey("reports.id", ondelete="SET NULL"),
        nullable=True,
        comment="Reference to report (nullable — not always available at search time)",
    )
    card_id = Column(
        CHAR(36), nullable=True, comment="Logical card identifier; intentionally not an FK"
    )
    trigger_source = Column(
        String(30),
        nullable=False,
        comment="'chat', 'card_generation', 'brief', 'card_refinement', or 'ask_caspr'",
    )
    section_name = Column(
        Text, nullable=True, comment="Report section being generated (card_generation/brief only)"
    )
    user_query = Column(Text, nullable=True, comment="Query string sent to the web search")
    model_used = Column(String(50), nullable=True, comment="Model used, e.g. gpt-4o, gpt-5.4")
    provider = Column(String(50), nullable=True)
    provider_response_id = Column(String(255), nullable=True)
    provider_queries = Column(JSONB, nullable=True)
    status = Column(String(30), nullable=True)
    error_type = Column(String(255), nullable=True)
    duration_ms = Column(Integer, nullable=True)
    search_call_count = Column(Integer, default=0, server_default="0", nullable=True)
    candidate_count = Column(Integer, default=0, server_default="0", nullable=True)
    cited_count = Column(
        Integer,
        default=0,
        server_default="0",
        nullable=True,
        comment="Upper bound for historical rows — see WebSearchCitation docstring for a pre-fix duplicate-URL caveat",
    )
    total_results_count = Column(
        Integer, nullable=True, comment="Compatibility count; equals candidate_count for new rows"
    )
    usage_metadata = Column(JSONB, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    citations = relationship(
        "WebSearchCitation", back_populates="search_event", cascade="all, delete-orphan"
    )
    raw_response = relationship(
        "WebSearchRawResponse",
        back_populates="search_event",
        uselist=False,
        cascade="all, delete-orphan",
    )

    def __repr__(self):
        return f"<WebSearchEvent(id='{self.id}', trigger='{self.trigger_source}', model='{self.model_used}', results={self.total_results_count})>"


class WebSearchCitation(Base):
    """
    One row per URL returned by a single WebSearchEvent.

    Attributes:
        domain:             Extracted hostname (e.g. 'who.int') — indexed for domain analytics.
        title:              Page title from the OpenAI search result (may be null for chat path).
        snippet:            Text snippet from the OpenAI search result.
        was_cited_in_output: True if this URL was actually cited inline in the final output;
                             None when not yet determined.

    NOTE FOR ANALYSIS / DASHBOARDS (including LLM-driven ones): a bug present
    until the fix in ``app.admin.repository_web_search.py::_normalize_links`` (search for
    "cited_row_index_by_url") could persist the SAME cited URL as TWO separate
    rows per ``search_event_id`` — one sourced from OpenAI's own
    ``url_citation`` annotations, one from a plain-text URL regex fallback
    over the final rendered card/answer — both with ``was_cited_in_output``
    True. This does NOT affect candidate rows (``was_cited_in_output`` False),
    which can legitimately repeat a URL across different
    ``search_call_index``/``result_rank`` values by design.
    For rows created before that fix, a naive ``COUNT(*)`` of cited rows per
    ``search_event_id``/report/section can overstate the number of distinct
    sources actually cited. When counting or listing cited sources, prefer
    ``COUNT(DISTINCT url)`` / ``GROUP BY url`` per ``search_event_id`` rather
    than raw row counts, and treat ``web_search_events.cited_count`` (which
    predates the fix and was NOT backfilled) as an upper bound rather than an
    exact count for historical events.
    """

    __tablename__ = "web_search_citations"

    id = Column(
        CHAR(36), primary_key=True, default=lambda: str(uuid7()), unique=True, nullable=False
    )
    search_event_id = Column(
        CHAR(36),
        ForeignKey("web_search_events.id", ondelete="CASCADE"),
        nullable=False,
        comment="Parent search event",
    )
    user_id = Column(
        CHAR(36),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        comment="Denormalized for fast per-user queries",
    )
    report_id = Column(
        CHAR(36),
        ForeignKey("reports.id", ondelete="SET NULL"),
        nullable=True,
        comment="Denormalized for fast per-report queries",
    )
    url = Column(Text, nullable=False, comment="Canonical HTTP(S) URL")
    raw_url = Column(
        Text, nullable=True, comment="URL exactly as supplied by the provider or output"
    )
    domain = Column(
        String(255), nullable=True, comment="Extracted hostname, e.g. reuters.com — indexed"
    )
    title = Column(Text, nullable=True, comment="Page title from search result")
    snippet = Column(Text, nullable=True, comment="Text snippet from search result")
    search_call_id = Column(String(255), nullable=True)
    search_call_index = Column(Integer, nullable=True)
    result_rank = Column(Integer, nullable=True)
    citation_order = Column(Integer, nullable=True)
    was_cited_in_output = Column(
        Boolean,
        nullable=True,
        comment="True if URL was cited inline in final output; None = unknown",
    )
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    search_event = relationship("WebSearchEvent", back_populates="citations")

    def __repr__(self):
        return f"<WebSearchCitation(id='{self.id}', domain='{self.domain}', cited={self.was_cited_in_output})>"


class WebSearchRawResponse(Base):
    """
    One row per raw OpenAI Responses-API payload for a web-search-enabled call
    made during card generation, card refinement, or Ask Caspr. 1:1 with
    WebSearchEvent; kept in its own table so the full JSON body doesn't bloat
    the hot event/citation tables used for routine analytics queries.

    Attributes:
        provider_response_id: OpenAI's own response.id — lets you fetch/replay
                               the exact call via the API/dashboard.
        raw_response:          Full response.model_dump(mode='json') payload.
        is_truncated:          True if raw_response was replaced with a small
                                placeholder because it exceeded the size guard.
    """

    __tablename__ = "web_search_raw_responses"

    id = Column(
        CHAR(36), primary_key=True, default=lambda: str(uuid7()), unique=True, nullable=False
    )
    search_event_id = Column(
        CHAR(36),
        ForeignKey("web_search_events.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        comment="Parent WebSearchEvent (1:1)",
    )
    operation_id = Column(
        CHAR(36),
        nullable=False,
        comment="Denormalized from parent event — correlate rounds without a join",
    )
    attempt_number = Column(Integer, nullable=True)

    user_id = Column(
        CHAR(36),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        comment="Denormalized for fast per-user queries",
    )
    report_id = Column(
        CHAR(36),
        ForeignKey("reports.id", ondelete="SET NULL"),
        nullable=True,
        comment="Denormalized for fast per-report queries",
    )
    chat_id = Column(
        String(255), nullable=True, comment="Client-generated chat session ID — not an FK"
    )
    card_id = Column(
        CHAR(36), nullable=True, comment="Logical card identifier; intentionally not an FK"
    )
    trigger_source = Column(
        String(30),
        nullable=False,
        comment="'card_generation', 'brief', 'card_refinement', or 'ask_caspr'",
    )

    provider = Column(String(50), nullable=True, comment="'openai'")
    provider_response_id = Column(String(255), nullable=True, comment="OpenAI response.id")
    previous_response_id = Column(
        String(255),
        nullable=True,
        comment="response.previous_response_id, if OpenAI-side chaining is used",
    )
    model_used = Column(String(50), nullable=True, comment="response.model, e.g. gpt-5.4")
    response_status = Column(
        String(30),
        nullable=True,
        comment="response.status — 'completed', 'incomplete', 'failed', etc.",
    )

    raw_response = Column(
        JSONB, nullable=False, comment="Full response.model_dump(mode='json') from the OpenAI SDK"
    )
    payload_size_bytes = Column(
        Integer,
        nullable=True,
        comment="Size in bytes of the serialized raw response, before any truncation",
    )
    is_truncated = Column(
        Boolean,
        nullable=False,
        default=False,
        server_default="false",
        comment="True if raw_response was truncated due to the size guard",
    )

    response_created_at = Column(
        DateTime(timezone=True),
        nullable=True,
        comment="OpenAI's response.created_at (epoch converted to UTC)",
    )
    created_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        comment="When this row was inserted (UTC)",
    )

    search_event = relationship("WebSearchEvent", back_populates="raw_response", uselist=False)

    def __repr__(self):
        return f"<WebSearchRawResponse(id='{self.id}', search_event_id='{self.search_event_id}', provider_response_id='{self.provider_response_id}')>"


# Standalone indexes, moved with the models they index.
Index("idx_web_search_events_user_id", WebSearchEvent.user_id)
Index("idx_web_search_events_chat_id", WebSearchEvent.chat_id)
Index("idx_web_search_events_report_id", WebSearchEvent.report_id)
Index("idx_web_search_events_trigger_source", WebSearchEvent.trigger_source)
Index("idx_web_search_events_created_at", WebSearchEvent.created_at)
Index("idx_web_search_events_operation_id", WebSearchEvent.operation_id)
Index("idx_web_search_events_provider", WebSearchEvent.provider)
Index("idx_web_search_events_status", WebSearchEvent.status)
Index("idx_web_search_events_card_id", WebSearchEvent.card_id)
Index("idx_web_search_citations_search_event_id", WebSearchCitation.search_event_id)
Index("idx_web_search_citations_user_id", WebSearchCitation.user_id)
Index("idx_web_search_citations_report_id", WebSearchCitation.report_id)
Index("idx_web_search_citations_domain", WebSearchCitation.domain)
Index("idx_web_search_citations_was_cited", WebSearchCitation.was_cited_in_output)
Index(
    "idx_web_search_citations_event_role_rank",
    WebSearchCitation.search_event_id,
    WebSearchCitation.was_cited_in_output,
    WebSearchCitation.search_call_index,
    WebSearchCitation.result_rank,
)
Index(
    "idx_web_search_raw_responses_search_event_id",
    WebSearchRawResponse.search_event_id,
    unique=True,
)
Index("idx_web_search_raw_responses_operation_id", WebSearchRawResponse.operation_id)
Index(
    "idx_web_search_raw_responses_provider_response_id", WebSearchRawResponse.provider_response_id
)
Index("idx_web_search_raw_responses_user_id", WebSearchRawResponse.user_id)
Index("idx_web_search_raw_responses_report_id", WebSearchRawResponse.report_id)
Index("idx_web_search_raw_responses_trigger_source", WebSearchRawResponse.trigger_source)
Index("idx_web_search_raw_responses_created_at", WebSearchRawResponse.created_at)
Index("idx_costtracker_timestamp", CostTracker.timestamp)
Index("idx_costtracker_user_id", CostTracker.user_id)
Index("idx_costtracker_chat_id", CostTracker.chat_id)
Index("idx_costtracker_model_name", CostTracker.model_name)
Index("idx_costtracker_created_at", CostTracker.created_at)
Index("idx_costtracker_estimated_cost", CostTracker.estimated_cost)
Index("idx_costtracker_functionality", CostTracker.functionality)
