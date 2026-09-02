"""DD domain configuration."""

from src.core.domains.planner import DomainConfig
from src.core.domains.due_diligence.prompts import (
    DD_DRL_ADDENDUM,
    DD_SOURCE_RULES,
    DD_TABLE_RULES,
    DD_FORMATTING_RULES,
    DD_CITATION_RULES,
)

DUE_DILIGENCE_CONFIG = DomainConfig(
    domain_name="due_diligence",
    display_name="Due Diligence Analysis",
    web_search_enabled=True,
    require_uploaded_file=False,
    max_sections_overview=12,
    max_sections_comprehensive=12,
    table_ratio_target="50-60%",
    tone="Formal analytical, evidence-based",
    citation_style="web_and_tool",
    source_rules_block=DD_SOURCE_RULES,
    table_rules_block=DD_TABLE_RULES,
    formatting_rules_block=DD_FORMATTING_RULES,
    citation_rules_block=DD_CITATION_RULES,
    drl_addendum=DD_DRL_ADDENDUM,
)
