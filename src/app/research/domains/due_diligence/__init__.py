"""Due Diligence domain package."""

try:
    from app.research.domains.due_diligence.graph import (
        DD_RESEARCH_TOOL_DEFINITION,
        build_due_diligence_subgraph,
    )
except ImportError:
    build_due_diligence_subgraph = None
    DD_RESEARCH_TOOL_DEFINITION = None

__all__ = [
    "DD_RESEARCH_TOOL_DEFINITION",
    "build_due_diligence_subgraph",
]
