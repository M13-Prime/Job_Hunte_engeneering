"""Tool definitions exposed to LLM agents (Phase 11).

Each tool is a Python coroutine decorated with @tool. The decorator
auto-generates the JSON schema expected by Anthropic's tool-use API
from the function signature + type hints + docstring. Tools are kept
small and side-effect-explicit so agents can reason about them.
"""

from __future__ import annotations

from signal_tracker.agents.tools.base import (
    Tool,
    ToolError,
    ToolResult,
    tool,
)

__all__ = ["Tool", "ToolError", "ToolResult", "tool"]
