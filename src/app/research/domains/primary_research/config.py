"""
Primary Research domain -- configuration.

Instantiates a ``DomainConfig`` with all the PR-specific overrides (no web
search, document-only citations, heavier table ratio, etc.) and wires in the
prompt blocks from ``prompts.py``.
"""

from app.research.domains.planner import DomainConfig
from app.research.domains.primary_research.prompts import (
    PR_CITATION_RULES,
    PR_DRL_ADDENDUM,
    PR_FORMATTING_RULES,
    PR_SOURCE_RULES,
    PR_TABLE_RULES,
)

PRIMARY_RESEARCH_CONFIG = DomainConfig(
    domain_name="primary_research",
    display_name="Primary Research Analysis",

    web_search_enabled=False,
    require_uploaded_file=True,

    max_sections_overview=12,
    max_sections_comprehensive=12,

    table_ratio_target="60-70%",
    tone="Strict analytical / data-centric",
    citation_style="Document refs only",

    source_rules_block=PR_SOURCE_RULES,
    table_rules_block=PR_TABLE_RULES,
    formatting_rules_block=PR_FORMATTING_RULES,
    citation_rules_block=PR_CITATION_RULES,
    drl_addendum=PR_DRL_ADDENDUM,
)
