"""Caspr MCP server package.

Wraps the entire caspr agent (chat + report generation + downloads) as MCP
tools/prompts so Claude or any MCP host can drive it. See ``server.py``.
"""

from src.core.caspr_mcp.caspr_client import CasprAPIError, CasprClient

__all__ = ["CasprClient", "CasprAPIError"]
