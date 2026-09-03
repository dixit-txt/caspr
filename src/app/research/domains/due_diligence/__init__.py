"""Due Diligence domain package."""

try:
    from app.research.domains.due_diligence.graph import (
        build_due_diligence_subgraph,
        DD_RESEARCH_TOOL_DEFINITION,
    )
except ImportError:
    build_due_diligence_subgraph = None
    DD_RESEARCH_TOOL_DEFINITION = None

__all__ = [
    "build_due_diligence_subgraph",
    "DD_RESEARCH_TOOL_DEFINITION",
]
