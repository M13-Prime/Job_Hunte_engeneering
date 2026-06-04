"""Tests for the AgentLoop framework (mocked LiteLLM)."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from signal_tracker.agents.loop import AgentLoop
from signal_tracker.agents.tools.base import ToolRegistry, tool


def _llm_response(
    *, text: str = "", tool_calls: list[dict[str, Any]] | None = None,
) -> SimpleNamespace:
    """Mimic the litellm response shape used by AgentLoop."""
    tcs = []
    for i, tc in enumerate(tool_calls or []):
        tcs.append(SimpleNamespace(
            id=tc.get("id", f"tc_{i}"),
            type="function",
            function=SimpleNamespace(
                name=tc["name"],
                arguments=json.dumps(tc.get("input", {})),
            ),
        ))
    msg = SimpleNamespace(content=text, tool_calls=tcs)
    return SimpleNamespace(
        choices=[SimpleNamespace(message=msg)],
        usage=SimpleNamespace(prompt_tokens=10, completion_tokens=10),
        model="anthropic/claude-haiku-4-5",
    )


@pytest.fixture()
def patched_litellm(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    mock = AsyncMock()
    monkeypatch.setattr(
        "signal_tracker.agents.loop.litellm.acompletion", mock,
    )
    monkeypatch.setattr(
        "signal_tracker.agents.loop.litellm.completion_cost",
        lambda completion_response=None: 0.01,
    )
    return mock


async def test_agent_stops_when_llm_returns_only_text(
    patched_litellm: AsyncMock,
) -> None:
    @tool()
    async def noop() -> dict[str, Any]:
        return {"ok": True}

    reg = ToolRegistry()
    reg.add(noop)
    patched_litellm.return_value = _llm_response(text="Done.")
    agent = AgentLoop(
        model="anthropic/claude-haiku-4-5",
        system_prompt="sys", tools=reg,
        budget_usd=1.0, max_iterations=5,
    )
    result = await agent.run("go")
    assert result.status == "finished"
    assert result.final_message == "Done."
    assert result.iterations == 1


async def test_agent_executes_tool_then_finishes(
    patched_litellm: AsyncMock,
) -> None:
    @tool()
    async def shout(text: str) -> str:
        return text.upper()

    reg = ToolRegistry()
    reg.add(shout)

    patched_litellm.side_effect = [
        _llm_response(tool_calls=[{"name": "shout", "input": {"text": "hi"}}]),
        _llm_response(text="HI was shouted."),
    ]
    agent = AgentLoop(
        model="anthropic/claude-haiku-4-5",
        system_prompt="sys", tools=reg,
        budget_usd=1.0, max_iterations=5,
    )
    result = await agent.run("go")
    assert result.status == "finished"
    assert result.iterations == 2
    # Trace records the tool call.
    tool_traces = [t for t in result.traces if t.role == "tool"]
    assert len(tool_traces) == 1
    assert tool_traces[0].tool_name == "shout"
    assert tool_traces[0].tool_result == "HI"


async def test_agent_terminal_tool_short_circuits(
    patched_litellm: AsyncMock,
) -> None:
    @tool(terminal=True)
    async def finish(reason: str) -> dict[str, Any]:
        return {"reason": reason}

    reg = ToolRegistry()
    reg.add(finish)
    patched_litellm.return_value = _llm_response(
        text="OK",
        tool_calls=[{"name": "finish", "input": {"reason": "done"}}],
    )
    agent = AgentLoop(
        model="anthropic/claude-haiku-4-5",
        system_prompt="sys", tools=reg,
        budget_usd=1.0, max_iterations=10,
    )
    result = await agent.run("go")
    assert result.status == "finished"
    # Even though the loop has budget left, terminal tool stops it.
    assert result.iterations == 1


async def test_agent_stops_on_budget(
    patched_litellm: AsyncMock,
) -> None:
    @tool()
    async def x() -> int:
        return 1

    reg = ToolRegistry()
    reg.add(x)
    patched_litellm.return_value = _llm_response(
        tool_calls=[{"name": "x", "input": {}}],
    )
    agent = AgentLoop(
        model="anthropic/claude-haiku-4-5",
        system_prompt="sys", tools=reg,
        budget_usd=0.005,  # one call (0.01) already busts it
        max_iterations=20,
    )
    result = await agent.run("go")
    assert result.status == "budget"
    assert result.iterations >= 1
    assert result.total_cost_usd >= 0.005


async def test_agent_stops_on_max_iterations(
    patched_litellm: AsyncMock,
) -> None:
    @tool()
    async def x() -> int:
        return 1

    reg = ToolRegistry()
    reg.add(x)
    # LLM keeps calling the tool forever — only iteration cap stops us.
    patched_litellm.return_value = _llm_response(
        tool_calls=[{"name": "x", "input": {}}],
    )
    agent = AgentLoop(
        model="anthropic/claude-haiku-4-5",
        system_prompt="sys", tools=reg,
        budget_usd=10.0, max_iterations=3,
    )
    result = await agent.run("go")
    assert result.status == "iterations"
    assert result.iterations == 3


async def test_agent_surfaces_unknown_tool_to_llm(
    patched_litellm: AsyncMock,
) -> None:
    """If the LLM hallucinates a tool name, the agent feeds back an
    error tool_result instead of crashing."""
    @tool()
    async def real() -> str:
        return "ok"

    reg = ToolRegistry()
    reg.add(real)

    patched_litellm.side_effect = [
        _llm_response(tool_calls=[{"name": "fake", "input": {}}]),
        _llm_response(text="Gave up."),
    ]
    agent = AgentLoop(
        model="anthropic/claude-haiku-4-5",
        system_prompt="sys", tools=reg,
        budget_usd=1.0, max_iterations=5,
    )
    result = await agent.run("go")
    assert result.status == "finished"
    error_traces = [t for t in result.traces if t.role == "tool" and t.is_error]
    assert len(error_traces) == 1
    assert "Unknown tool" in error_traces[0].tool_result
