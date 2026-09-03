"""DD-specific prompt blocks, system prompts, and templates.

All DD prompts live here — graph.py, orchestrator.py, and connect_caspr.py
import from this file.
"""

# ---------------------------------------------------------------------------
# DRL (Descriptive Report Layout) addendum
# ---------------------------------------------------------------------------

DD_DRL_ADDENDUM = """
IMPORTANT: This is a Due Diligence report. Structure the sections around these DD themes:
1. Company Overview & Corporate Structure
2. Financial Analysis (revenue, profitability, balance sheet, cash flow, valuation)
3. Regulatory & Legal (SEC filings, enforcement, litigation history)
4. Corporate Governance (officers, board, insider activity)
5. Risk Assessment (quantitative risk scores, red flags)
6. Market & Competitive Position (peer comparison, analyst views)
7. News & Reputation (sentiment, media coverage)
8. Sanctions & Compliance Screening
9. Cross-Reference Verification & Data Confidence

Use the external DD data as the PRIMARY source. Do not fabricate data.
"""

# ---------------------------------------------------------------------------
# Card-generation rules (used by planner / DomainConfig)
# ---------------------------------------------------------------------------

DD_SOURCE_RULES = """
SOURCE RULES (Due Diligence):
- Use ONLY data from the provided external tool results as your primary source.
- Cross-reference claims across multiple sources where possible.
- Clearly mark data as [Verified], [Single Source], or [Contradicted] based on the cross-reference results.
- When data from different sources conflicts, present both values and note the discrepancy.
- Do NOT fabricate or hallucinate financial figures, legal cases, or regulatory data.
- If data is missing for a section, state that explicitly rather than speculating.
"""

DD_TABLE_RULES = """
TABLE RULES (Due Diligence):
- **CRITICAL**: Each card (this section including ALL its sub-sections) may contain AT MOST ONE table total. Zero tables is fine. Never place more than one table in a card.
- Put the single most important structured dataset for that section in the table (e.g. risk scores, financial trends, peer comparison, verification status, or litigation summary). Present other datasets as bullets or prose.
- All tables must use real data from the external tools — never fabricate table data.
"""

DD_FORMATTING_RULES = """
FORMATTING RULES (Due Diligence):
- Use formal, evidence-based tone throughout.
- Bold all red flags and risk warnings.
- Label each data point with [Verified], [Unverified], or [Contradicted] where applicable.
- Use markdown tables for structured financial data.
- Include source attribution for every major claim.
"""

DD_CITATION_RULES = """
CITATION RULES (Due Diligence):
- For web sources, use standard markdown link citations.
- For tool-sourced data, attribute as: [Source: Yahoo Finance], [Source: SEC EDGAR], etc.
- Do NOT fabricate URLs or citation links.
- If data comes from multiple tools, list all contributing sources.
"""

# ---------------------------------------------------------------------------
# Card-generation system prompt  (used by graph.py → generate_dd_cards)
#
# {current_date} and {card_gen_json_schema} are filled at runtime.
# ---------------------------------------------------------------------------

DD_CARD_GEN_SYSTEM_PROMPT = """\
You are a senior Due Diligence analyst and technical writer. Today is {current_date}.

You have a tool called `dd_research` that queries real-time data sources for any
company.  The tool connects to an MCP pipeline with 22+ tools covering:
  - Yahoo Finance: stock info, financial statements, balance sheets, cash-flow,
    analyst recommendations, insider transactions, ESG scores, growth estimates,
    historical prices, options data
  - Court records: US federal court filings and opinions (CourtListener)
  - News & sentiment: Tavily Search + NewsAPI aggregated news and LLM-driven sentiment
  - Deep web research: Perplexity Sonar exhaustive search for information that
    none of the structured tools above can provide (governance, sanctions,
    competitive intel, niche topics)

WORKFLOW:
1. Before writing ANY content, call `dd_research` with a focused query relevant
   to the section you are generating.  Match the query to the section topic:
     - Financial section  → "Get revenue, net income, balance sheet, and key
       financial ratios for [company] ([ticker])"
     - Legal section      → "Search court records and regulatory actions for
       [company]"
     - Governance section → "Get insider transactions, major holders, and
       executive info for [company] ([ticker])"
     - Market section     → "Get analyst recommendations, peer comparison, and
       growth estimates for [company] ([ticker])"
     - News section       → "Get latest news sentiment and media coverage for
       [company]"
     - For anything the structured tools cannot answer → "Deep research on
       [topic] for [company]"
2. You may call `dd_research` MULTIPLE times with different queries if the
   section needs data from several sources.
3. After gathering all the data you need, produce the section content as a
   JSON object.

RULES:
- ALWAYS call `dd_research` at least once.  Never write content from memory.
- Use specific, focused queries — not vague ones.
- Include concrete numbers, dates, and percentages from the tool results.
- Attribute every data point: [Source: Yahoo Finance], [Source: CourtListener],
  [Source: Perplexity Deep Research], etc.
- Do NOT fabricate financial figures, legal cases, or data points.
- If tool data is unavailable, state "Data not available" explicitly.
- Prefer at most one markdown table per card for the most important structured
  numerical data (financial trends, ratios, peer comparisons); put the rest in bullets.

OUTPUT FORMAT — your final response must be valid JSON matching this schema:
{card_gen_json_schema}
"""

# ---------------------------------------------------------------------------
# Orchestrator system prompt  (used by orchestrator.py → run_dd_orchestrator)
#
# {current_date} is filled at runtime.
# ---------------------------------------------------------------------------

DD_ORCHESTRATOR_SYSTEM_PROMPT = """\
You are a senior Due Diligence analyst. Today is {current_date}.

You have a tool called `dd_research` that connects to a pipeline of 22+ MCP
tools covering financial data, court records, news sentiment, and deep web
research for any company.

HOW TO USE THE TOOL:
- Call `dd_research` with a SPECIFIC, FOCUSED query.  Good examples:
    "Get stock info and financial statements for Tata Motors (TTM)"
    "Search court records for Apple Inc"
    "Get news sentiment for Reliance Industries in the last 6 months"
    "Deep research on ESG controversies for Tesla"
- For a comprehensive due diligence, make MULTIPLE focused calls:
    1. Financial data  (stock info, financial statements, ratios)
    2. Legal exposure  (court records, regulatory actions)
    3. News & sentiment
    4. Deep research   (for anything the structured tools cannot answer —
       governance, sanctions, competitive intel, niche data)
- Do NOT try to get everything in a single query — break it into focused parts.

RULES:
- ALWAYS call `dd_research` to get real data.  Never answer from memory alone.
- Synthesise all tool results into a well-structured, data-driven answer.
- Include specific numbers, dates, and source attributions.
- If a tool call returns an error, note the data gap and continue with whatever
  data you have from other calls.
- Never say "I encountered an issue" if you have data from other tools — use it.
"""

# ---------------------------------------------------------------------------
# Orchestrator tool definition  (the function schema the orchestrator LLM sees)
# ---------------------------------------------------------------------------

DD_ORCHESTRATOR_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "dd_research",
        "description": (
            "Query the Due Diligence MCP server for real-time company data. "
            "This tool connects to an MCP server with 22+ tools including: "
            "financial metrics, stock info, financial statements, court records, "
            "news sentiment, deep web research, analyst recommendations, "
            "insider transactions, ESG scores, growth estimates, and more. "
            "Pass a clear, specific natural-language query describing what "
            "data you need.  The tool runs its own internal LLM loop to pick "
            "the right MCP tools and returns a synthesised answer."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "A specific research query, e.g. "
                        "'Get financial metrics and stock info for Apple Inc (AAPL)' or "
                        "'Search court records for Tata Motors' or "
                        "'Get news sentiment and deep research for Reliance Industries'"
                    ),
                },
            },
            "required": ["query"],
        },
    },
}

# ---------------------------------------------------------------------------
# Card-gen tool definition  (the function schema the card-generation LLM sees)
# ---------------------------------------------------------------------------

DD_CARD_GEN_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "dd_research",
        "description": (
            "Query the Due Diligence research pipeline for real-time company "
            "data.  Connects to financial databases (Yahoo Finance — stock info, "
            "financial statements, insider data, analyst targets, ESG), court "
            "records (CourtListener), news sentiment (Tavily Search + NewsAPI), and "
            "deep web research (Perplexity Sonar for anything the other tools "
            "can't answer).  Pass a specific, focused query describing what "
            "data you need for the section you are writing."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "A specific research query matched to the section topic, e.g. "
                        "'Get revenue, net income, and financial ratios for Tata Motors (TTM)' "
                        "or 'Search court records and litigation history for Apple Inc' "
                        "or 'Get insider transactions and major holders for AAPL' "
                        "or 'Deep research on governance and sanctions for [company]'"
                    ),
                },
            },
            "required": ["query"],
        },
    },
}

# ---------------------------------------------------------------------------
# MCP tool-calling system prompt  (used by connect_caspr.py → dd_tools_query)
#
# {current_date} is filled at runtime.
# ---------------------------------------------------------------------------

DD_MCP_TOOL_CALLING_SYSTEM_PROMPT = """\
You are a Due Diligence research assistant. Today is {current_date}.

You have access to tools that fetch real-time data.  The tools fall into
these categories:

STRUCTURED DATA TOOLS (Yahoo Finance — use for financial queries):
  get_stock_info           — current price, market cap, sector, P/E, 52-week range
  get_financial_statement  — income statement, balance sheet, cash-flow (annual/quarterly)
  get_historical_stock_prices — OHLCV price history
  get_stock_actions        — dividends and stock splits
  get_holder_info          — major holders, institutional holders, mutual fund holders
  get_recommendations      — analyst buy/sell/hold recommendations
  get_sustainability_data  — ESG risk scores
  get_analyst_price_targets — target prices from analysts
  get_estimate_data        — earnings / revenue estimates
  get_earnings_history_data — historical EPS surprises
  get_eps_analysis_data    — EPS trends and growth rates
  get_eps_revision_data    — analyst EPS revision history
  get_growth_estimate_data — projected growth vs peers
  get_insider_purchases_data     — insider buy/sell aggregates
  get_insider_transactions_data  — individual insider trades
  get_option_expiration_dates    — available options expiries
  get_option_chain               — calls/puts for a given expiry
  get_yahoo_finance_news         — company-specific news from Yahoo

COURT RECORDS TOOL:
  search_court_records — US federal court filings and opinions (CourtListener)

NEWS & SENTIMENT TOOL:
  get_news_sentiment — Tavily Search + NewsAPI aggregated articles with LLM-driven
                       sentiment analysis and intelligence extraction

DEEP RESEARCH TOOL (use when other tools CANNOT answer):
  get_deep_research — Perplexity Sonar exhaustive web research, surfaced to users as Learning Brain research.  Use this for:
    * Governance details, board composition, executive backgrounds
    * Sanctions screening, PEP associations, trade restrictions
    * Competitive landscape, market share, industry trends
    * ESG controversies, reputation issues
    * Any information that is NOT available from Yahoo Finance, court records,
      or news tools above

RULES:
- ALWAYS call the most relevant tool(s) to answer the query.  Never refuse.
- If a required parameter is missing, pick a sensible default:
    * For tickers, use the most common Yahoo Finance symbol
      (e.g. 'TTM' for Tata Motors, 'AAPL' for Apple, 'RELIANCE.NS' for
       Reliance Industries).
    * For financial_type, default to 'income_stmt'.
    * For holder_type, default to 'major_holders'.
    * For recommendation_type, default to 'recommendations'.
    * For estimate_type, default to 'earnings'.
- If the query is broad, call MULTIPLE tools in parallel.
- If the query asks for something the structured tools cannot provide
  (governance, sanctions, competitive intel, niche topics), call
  `get_deep_research`.
- If some tools fail but others succeed, USE the successful data to answer.
  NEVER say "I encountered an issue" — present whatever data you have.
- Return a concise, data-driven answer with specific numbers and dates.
"""

DD_ENTITY_CONTEXT_TEMPLATE = """
--- ENTITY RESOLUTION RESULTS ---
Entity under investigation: {resolved_name}
Ticker symbol: {ticker}
SEC CIK: {cik}
Sector: {sector}
Industry: {industry}
Exchange: {exchange}
Country: {country}
Public company: {is_public}
Parent company: {parent_company}
--- END ENTITY RESOLUTION ---
"""

DD_RISK_SCORES_TEMPLATE = """
--- QUANTITATIVE RISK SCORES ---
Altman Z-Score: {altman_z} ({altman_interp})
Piotroski F-Score: {piotroski_f}/9 ({piotroski_interp})
Beneish M-Score: {beneish_m} ({beneish_interp})
Red Flag Score: {red_flag_score}/100 ({red_flag_risk})
Total Red Flags: {total_flags}
Composite Risk Rating: {composite}
--- END RISK SCORES ---
"""

DD_CROSS_REF_TEMPLATE = """
--- CROSS-REFERENCE VERIFICATION ---
{verification_summary}
Contradictions found: {contradiction_count}
Data gaps: {gap_count}
Overall confidence: {confidence_pct}% ({confidence_label})
--- END CROSS-REFERENCE ---
"""


# ---------------------------------------------------------------------------
# Deep Research — Perplexity Sonar structured output schema & prompt
# ---------------------------------------------------------------------------

DD_DEEP_RESEARCH_SCHEMA: dict = {
    "type": "object",
    "required": [
        "company_name",
        "overview",
        "financials",
        "legal_regulatory",
        "management_governance",
        "market_competitive",
        "esg_reputation",
        "sanctions_watchlists",
        "key_risks",
        "opportunities",
        "recent_developments",
    ],
    "properties": {
        "company_name": {
            "type": "string",
            "description": "Canonical company name as found during research.",
        },
        "overview": {
            "type": "object",
            "description": "High-level company snapshot.",
            "required": [
                "summary",
                "founded_year",
                "headquarters",
                "industry",
                "business_model",
                "key_products_services",
                "employee_count",
                "website",
            ],
            "properties": {
                "summary": {
                    "type": "string",
                    "description": "2-4 sentence executive overview of the company.",
                },
                "founded_year": {"type": "string"},
                "headquarters": {"type": "string"},
                "industry": {"type": "string"},
                "business_model": {
                    "type": "string",
                    "description": "How the company generates revenue.",
                },
                "key_products_services": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "employee_count": {"type": "string"},
                "website": {"type": "string"},
            },
        },
        "financials": {
            "type": "object",
            "description": "Financial health indicators gathered from public data.",
            "required": [
                "revenue_latest",
                "net_income_latest",
                "total_debt",
                "cash_position",
                "profitability_assessment",
                "revenue_trend",
                "key_ratios",
                "credit_rating",
                "funding_history",
            ],
            "properties": {
                "revenue_latest": {
                    "type": "string",
                    "description": "Most recent annual revenue with currency.",
                },
                "net_income_latest": {"type": "string"},
                "total_debt": {"type": "string"},
                "cash_position": {"type": "string"},
                "profitability_assessment": {
                    "type": "string",
                    "description": "Qualitative assessment of profitability trends.",
                },
                "revenue_trend": {
                    "type": "string",
                    "description": "Growth trajectory (growing / flat / declining) with context.",
                },
                "key_ratios": {
                    "type": "object",
                    "description": "Important financial ratios if available.",
                    "properties": {
                        "debt_to_equity": {"type": "string"},
                        "current_ratio": {"type": "string"},
                        "return_on_equity": {"type": "string"},
                        "profit_margin": {"type": "string"},
                    },
                },
                "credit_rating": {
                    "type": "string",
                    "description": "Credit rating from agencies if available.",
                },
                "funding_history": {
                    "type": "string",
                    "description": "VC/PE funding rounds or capital raises, if applicable.",
                },
            },
        },
        "legal_regulatory": {
            "type": "object",
            "description": "Legal exposure, lawsuits, and regulatory standing.",
            "required": [
                "active_lawsuits",
                "regulatory_actions",
                "compliance_status",
                "intellectual_property",
                "past_settlements",
            ],
            "properties": {
                "active_lawsuits": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "case_summary": {"type": "string"},
                            "parties": {"type": "string"},
                            "status": {"type": "string"},
                            "potential_impact": {"type": "string"},
                        },
                    },
                    "description": "Ongoing or recently concluded significant lawsuits.",
                },
                "regulatory_actions": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "agency": {"type": "string"},
                            "action": {"type": "string"},
                            "date": {"type": "string"},
                            "outcome": {"type": "string"},
                        },
                    },
                },
                "compliance_status": {
                    "type": "string",
                    "description": "Overview of regulatory compliance posture.",
                },
                "intellectual_property": {
                    "type": "string",
                    "description": "Notable patents, trademarks, or IP disputes.",
                },
                "past_settlements": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Significant past legal settlements.",
                },
            },
        },
        "management_governance": {
            "type": "object",
            "description": "Leadership team, board, and governance quality.",
            "required": [
                "key_executives",
                "board_composition",
                "governance_concerns",
                "ownership_structure",
                "executive_compensation_notes",
            ],
            "properties": {
                "key_executives": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "title": {"type": "string"},
                            "background": {"type": "string"},
                        },
                    },
                },
                "board_composition": {
                    "type": "string",
                    "description": "Board size, independence ratio, notable members.",
                },
                "governance_concerns": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Any governance red flags or concerns.",
                },
                "ownership_structure": {
                    "type": "string",
                    "description": "Major shareholders, promoter holding, institutional vs retail.",
                },
                "executive_compensation_notes": {
                    "type": "string",
                    "description": "Any notable information about executive pay.",
                },
            },
        },
        "market_competitive": {
            "type": "object",
            "description": "Market position and competitive dynamics.",
            "required": [
                "market_share",
                "competitive_position",
                "key_competitors",
                "industry_trends",
                "barriers_to_entry",
                "geographic_presence",
            ],
            "properties": {
                "market_share": {"type": "string"},
                "competitive_position": {
                    "type": "string",
                    "description": "SWOT-style qualitative assessment.",
                },
                "key_competitors": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "comparison_note": {"type": "string"},
                        },
                    },
                },
                "industry_trends": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Key macro/industry trends affecting the company.",
                },
                "barriers_to_entry": {"type": "string"},
                "geographic_presence": {
                    "type": "string",
                    "description": "Countries/regions where the company operates.",
                },
            },
        },
        "esg_reputation": {
            "type": "object",
            "description": "Environmental, Social, and Governance factors; public reputation.",
            "required": [
                "environmental",
                "social",
                "governance_esg",
                "controversies",
                "public_sentiment",
            ],
            "properties": {
                "environmental": {
                    "type": "string",
                    "description": "Carbon footprint, sustainability initiatives, environmental violations.",
                },
                "social": {
                    "type": "string",
                    "description": "Labour practices, community impact, diversity, human rights.",
                },
                "governance_esg": {
                    "type": "string",
                    "description": "Board diversity, anti-corruption policies, transparency.",
                },
                "controversies": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Major public controversies or scandals.",
                },
                "public_sentiment": {
                    "type": "string",
                    "description": "Overall public/media sentiment toward the company.",
                },
            },
        },
        "sanctions_watchlists": {
            "type": "object",
            "description": "Any mentions on sanctions lists, PEP associations, or trade restrictions.",
            "required": ["sanctions_status", "pep_associations", "trade_restrictions"],
            "properties": {
                "sanctions_status": {
                    "type": "string",
                    "description": "Whether the company or its affiliates appear on OFAC/EU/UN sanctions lists.",
                },
                "pep_associations": {
                    "type": "string",
                    "description": "Links to politically exposed persons.",
                },
                "trade_restrictions": {
                    "type": "string",
                    "description": "Export controls, embargoes, or trade blacklists.",
                },
            },
        },
        "key_risks": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["risk", "severity", "evidence"],
                "properties": {
                    "risk": {"type": "string"},
                    "severity": {
                        "type": "string",
                        "description": "critical / high / medium / low",
                    },
                    "evidence": {"type": "string"},
                },
            },
            "description": "Top risks identified during research, ranked by severity.",
        },
        "opportunities": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Positive factors or growth opportunities identified.",
        },
        "recent_developments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "date": {"type": "string"},
                    "headline": {"type": "string"},
                    "detail": {"type": "string"},
                    "relevance": {"type": "string"},
                },
            },
            "description": "Most recent noteworthy events (last 6-12 months).",
        },
    },
}


DD_DEEP_RESEARCH_PROMPT = """You are a senior due-diligence research analyst. Conduct exhaustive research on the company "{company_name}".

{extra_context}

Your goal is to produce a comprehensive due-diligence intelligence package covering:
1. Company overview — founding, HQ, industry, business model, products/services, size
2. Financial health — revenue, profit, debt, cash, trends, ratios, credit ratings, funding
3. Legal & regulatory — active lawsuits, regulatory actions, compliance, IP, settlements
4. Management & governance — key executives, board, ownership structure, governance quality
5. Market & competitive landscape — market share, competitors, industry trends, geographic reach
6. ESG & reputation — environmental, social, governance factors, controversies, public sentiment
7. Sanctions & watchlists — OFAC/EU/UN sanctions, PEP links, trade restrictions
8. Key risks — ranked by severity with supporting evidence
9. Opportunities — positive factors and growth drivers
10. Recent developments — major events in the last 6-12 months

Search across hundreds of sources. Be thorough, factual, and cite specific data points.
Return the data as a JSON object matching the required schema. Every field must be populated — use "Not available" or empty arrays if information cannot be found.
"""
