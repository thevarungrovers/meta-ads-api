"""MCP server exposing this project's Meta Ads tooling to an AI agent.

Reads are open. Writes go through a ``preview_*`` / ``apply_*`` pair, never a
single tool with a ``dry_run`` flag -- see :mod:`metaads_mcp.tools_write` for
why that distinction is the whole design.
"""

__version__ = "0.1.0"

__all__ = ["__version__"]
