"""@tool decorator and the registry types it produces.

Design notes:
- A @tool wraps an async function. Its signature + type hints + docstring
  produce a Tool object with an Anthropic-compatible JSON schema.
- The handler returns either:
    * a plain value (serialized to JSON for the tool result), or
    * a ToolResult (lets the tool flag is_error + attach structured data).
- Tools raise ToolError to signal a clean failure that the agent should
  see; uncaught exceptions are wrapped and reported as tool errors so
  the agent loop never crashes from a buggy tool.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import typing
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from types import UnionType
from typing import Any, Union, get_args, get_origin


class ToolError(RuntimeError):
    """Raised inside a tool handler to surface a controlled failure to
    the agent (vs. crashing the loop). The agent sees the message as the
    tool result with is_error=True."""


@dataclass(slots=True)
class ToolResult:
    """Structured result a tool can return when it wants more control
    over what the agent sees (vs. just returning a value)."""

    content: Any
    is_error: bool = False

    def to_anthropic_content(self) -> str:
        if isinstance(self.content, str):
            return self.content
        return json.dumps(self.content, ensure_ascii=False, default=str)


@dataclass(slots=True)
class Tool:
    """A single tool callable by an agent."""

    name: str
    description: str
    input_schema: dict[str, Any]
    handler: Callable[..., Awaitable[Any]]
    # Tools tagged terminal cause the agent loop to exit when called
    # successfully — used for finish() / give_up() style sentinels.
    terminal: bool = False

    def to_anthropic_spec(self) -> dict[str, Any]:
        """Anthropic tool-use schema: name, description, input_schema."""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }

    async def execute(self, raw_input: dict[str, Any]) -> ToolResult:
        """Run the handler safely.

        Always returns a ToolResult — wraps unexpected exceptions so a
        buggy tool can't crash the agent loop. The agent then sees the
        error and can decide whether to retry, try another tool, or
        give up.
        """
        try:
            value = await self.handler(**raw_input)
        except ToolError as exc:
            return ToolResult(content=f"ToolError: {exc}", is_error=True)
        except Exception as exc:  # pragma: no cover — defensive
            return ToolResult(
                content=f"Unhandled exception in tool {self.name}: {exc}",
                is_error=True,
            )
        if isinstance(value, ToolResult):
            return value
        return ToolResult(content=value, is_error=False)


# ---------------------------------------------------------------------------
# @tool decorator + schema generation
# ---------------------------------------------------------------------------


_PRIMITIVE_TYPE_MAP: dict[type, str] = {
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
}


def _type_to_schema(annotation: Any) -> dict[str, Any]:
    """Best-effort type → JSON schema mapping for tool params.

    Supports primitives, list[T], dict[str, V], Optional[T] / T | None,
    and unions (collapsed to {"anyOf": [...]}). Unknown types fall back
    to {"type": "string"} so the tool still works — the LLM just sees a
    looser contract.
    """
    if annotation is inspect.Parameter.empty:
        return {"type": "string"}
    if annotation in _PRIMITIVE_TYPE_MAP:
        return {"type": _PRIMITIVE_TYPE_MAP[annotation]}

    origin = get_origin(annotation)
    args = get_args(annotation)

    if origin in (Union, UnionType):
        non_none = [a for a in args if a is not type(None)]
        if len(non_none) == 1:
            return _type_to_schema(non_none[0])
        return {"anyOf": [_type_to_schema(a) for a in non_none]}

    if origin is list:
        items = _type_to_schema(args[0]) if args else {"type": "string"}
        return {"type": "array", "items": items}

    if origin is dict:
        return {"type": "object"}

    return {"type": "string"}


def _build_input_schema(handler: Callable[..., Any]) -> dict[str, Any]:
    sig = inspect.signature(handler)
    # Resolve string annotations into real types (closures inside the
    # decorated module use `from __future__ import annotations`, so each
    # param.annotation is a str unless we ask typing to resolve them).
    try:
        hints = typing.get_type_hints(handler, include_extras=False)
    except Exception:
        hints = {}
    properties: dict[str, Any] = {}
    required: list[str] = []
    for name, param in sig.parameters.items():
        if name in {"self", "cls"} or param.kind in (
            inspect.Parameter.VAR_POSITIONAL,
            inspect.Parameter.VAR_KEYWORD,
        ):
            continue
        annotation = hints.get(name, param.annotation)
        properties[name] = _type_to_schema(annotation)
        if param.default is inspect.Parameter.empty:
            required.append(name)
    schema: dict[str, Any] = {
        "type": "object",
        "properties": properties,
    }
    if required:
        schema["required"] = required
    return schema


def tool(
    name: str | None = None,
    *,
    description: str | None = None,
    terminal: bool = False,
) -> Callable[[Callable[..., Awaitable[Any]]], Tool]:
    """Wrap an async function into a Tool.

    The function name is used as the tool name unless explicitly given.
    The first paragraph of the docstring is used as the description
    unless explicitly given. Type hints power the input_schema.

    Example::

        @tool(description="Classify one article.")
        async def classify_article(article_id: int) -> dict:
            ...
    """
    def wrap(fn: Callable[..., Awaitable[Any]]) -> Tool:
        if not asyncio.iscoroutinefunction(fn):
            raise TypeError(
                f"@tool can only decorate async functions; got {fn.__name__}"
            )
        tool_name = name or fn.__name__
        doc = (fn.__doc__ or "").strip()
        head = doc.split("\n\n", 1)[0].strip() if doc else fn.__name__
        return Tool(
            name=tool_name,
            description=description or head or fn.__name__,
            input_schema=_build_input_schema(fn),
            handler=fn,
            terminal=terminal,
        )
    return wrap


# ---------------------------------------------------------------------------
# Tool registry (lightweight container)
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ToolRegistry:
    """Indexed container with collision detection."""

    tools: list[Tool] = field(default_factory=list)

    def add(self, t: Tool) -> None:
        if any(existing.name == t.name for existing in self.tools):
            raise ValueError(f"Tool name collision: {t.name}")
        self.tools.append(t)

    def extend(self, items: list[Tool]) -> None:
        for item in items:
            self.add(item)

    def by_name(self, name: str) -> Tool | None:
        return next((t for t in self.tools if t.name == name), None)

    def anthropic_specs(self) -> list[dict[str, Any]]:
        return [t.to_anthropic_spec() for t in self.tools]


__all__ = ["Tool", "ToolError", "ToolRegistry", "ToolResult", "tool"]
