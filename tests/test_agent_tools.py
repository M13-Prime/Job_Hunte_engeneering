"""Tests for the @tool decorator + ToolRegistry + schema generation."""

from __future__ import annotations

from typing import Any

import pytest

from signal_tracker.agents.tools.base import (
    ToolError,
    ToolRegistry,
    ToolResult,
    tool,
)


def test_tool_decorator_builds_schema_from_signature() -> None:
    @tool(description="Add two numbers.")
    async def add(a: int, b: int, label: str | None = None) -> dict[str, Any]:
        return {"sum": a + b, "label": label}

    spec = add.to_anthropic_spec()
    assert spec["name"] == "add"
    assert spec["description"] == "Add two numbers."
    assert spec["input_schema"]["type"] == "object"
    props = spec["input_schema"]["properties"]
    assert props["a"] == {"type": "integer"}
    assert props["b"] == {"type": "integer"}
    # Optional param (default None) → not required, type is union-collapsed to "string".
    assert props["label"] == {"type": "string"}
    assert set(spec["input_schema"]["required"]) == {"a", "b"}


def test_tool_decorator_supports_lists() -> None:
    @tool()
    async def take_list(items: list[str]) -> int:
        return len(items)

    spec = take_list.to_anthropic_spec()
    assert spec["input_schema"]["properties"]["items"] == {
        "type": "array", "items": {"type": "string"},
    }


def test_tool_decorator_rejects_sync_functions() -> None:
    def sync_handler(x: int) -> int:
        return x

    with pytest.raises(TypeError):
        tool()(sync_handler)  # type: ignore[arg-type]


async def test_tool_execute_returns_tool_result_for_plain_value() -> None:
    @tool()
    async def echo(text: str) -> str:
        return text.upper()

    out = await echo.execute({"text": "hi"})
    assert isinstance(out, ToolResult)
    assert out.content == "HI"
    assert out.is_error is False


async def test_tool_execute_wraps_tool_error() -> None:
    @tool()
    async def boom(x: int) -> int:
        raise ToolError("nope")

    out = await boom.execute({"x": 1})
    assert out.is_error is True
    assert "nope" in out.content


async def test_tool_execute_catches_unhandled_exceptions() -> None:
    @tool()
    async def crash(x: int) -> int:
        raise RuntimeError("kaboom")

    out = await crash.execute({"x": 1})
    assert out.is_error is True
    assert "kaboom" in out.content


def test_registry_rejects_name_collision() -> None:
    @tool(name="dup")
    async def a() -> int:
        return 1

    @tool(name="dup")
    async def b() -> int:
        return 2

    reg = ToolRegistry()
    reg.add(a)
    with pytest.raises(ValueError, match="collision"):
        reg.add(b)


def test_registry_by_name_lookup() -> None:
    @tool()
    async def foo() -> int:
        return 1

    reg = ToolRegistry()
    reg.add(foo)
    assert reg.by_name("foo") is foo
    assert reg.by_name("unknown") is None


def test_terminal_flag_is_propagated() -> None:
    @tool(terminal=True)
    async def stop(reason: str) -> dict[str, Any]:
        return {"reason": reason}

    assert stop.terminal is True
