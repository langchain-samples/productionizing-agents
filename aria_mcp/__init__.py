"""ARIA's application layer.

This package is deliberately free of any LLM, agent, or prompt code. It is a plain
Python library over the plant's reference data, with a narrow, validated, well-tested
API surface. `server.py` exposes that same API over MCP.

The separation is the point. See ../aria/README.md and Module 1.
"""

from aria_mcp.repository import (
    ARIALookupError,
    Repository,
    get_repository,
)

__all__ = ["ARIALookupError", "Repository", "get_repository"]
