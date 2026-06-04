"""Generic agent loop: LLM picks tools, we execute, results feed back.

Wraps litellm.acompletion's tool-use mode. Per turn:
  1. Send messages + tools to the LLM.
  2. If the response has tool_use blocks, execute each tool, append a
     tool_result block to the conversation, loop.
  3. If the response has text only (no tool calls), the agent is done.
  4. If a "terminal" tool was called (finish / give_up), exit after
     executing it.

Anti-runaway:
- Hard cap on iterations (default 30).
- Hard cap on cost in USD (uses litellm.completion_cost(), best-effort).
- Per-turn timeout (litellm's network timeout).

The loop is provider-agnostic via litellm but Anthropic's tool-use
block shape is what we read/write — same encoding LiteLLM uses
internally for that provider.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

import litellm

from signal_tracker.agents.tools.base import Tool, ToolRegistry, ToolResult
from signal_tracker.classifier.llm import _resolve_fallbacks
from signal_tracker.utils.logging import get_logger

logger = get_logger(__name__)


class AgentBudgetExceeded(RuntimeError):
    """Raised internally and surfaced as an AgentResult.status='budget'."""


@dataclass(slots=True)
class AgentTrace:
    """Per-step trace of what the agent did — useful for the UI + debug."""

    step: int
    role: str          # "assistant" or "tool"
    text: str = ""     # assistant message text content
    tool_name: str = ""
    tool_input: dict[str, Any] = field(default_factory=dict)
    tool_result: str = ""
    is_error: bool = False
    cost_usd: float = 0.0


@dataclass(slots=True)
class AgentResult:
    """Final outcome of a run."""

    status: str  # "finished" | "budget" | "iterations" | "error" | "stuck"
    final_message: str = ""
    iterations: int = 0
    total_cost_usd: float = 0.0
    traces: list[AgentTrace] = field(default_factory=list)
    error: str | None = None


class AgentLoop:
    """One agent = one system prompt + one tool registry + one budget."""

    def __init__(
        self,
        *,
        model: str,
        system_prompt: str,
        tools: ToolRegistry,
        budget_usd: float = 0.5,
        max_iterations: int = 30,
        temperature: float = 0.2,
        fallback_model: str | None = None,
    ) -> None:
        self.model = model
        self.system_prompt = system_prompt
        self.tools = tools
        self.budget_usd = budget_usd
        self.max_iterations = max_iterations
        self.temperature = temperature
        self.fallback_model = fallback_model

    async def run(self, initial_user_message: str) -> AgentResult:
        messages: list[dict[str, Any]] = [
            {"role": "user", "content": initial_user_message},
        ]
        traces: list[AgentTrace] = []
        total_cost = 0.0
        iteration = 0
        fallbacks = _resolve_fallbacks(self.fallback_model)

        while iteration < self.max_iterations:
            iteration += 1
            if total_cost >= self.budget_usd:
                logger.info(
                    "agent.budget_exhausted iteration=%d cost=%.4f cap=%.4f",
                    iteration, total_cost, self.budget_usd,
                )
                return AgentResult(
                    status="budget", iterations=iteration,
                    total_cost_usd=total_cost, traces=traces,
                )

            kwargs: dict[str, Any] = {
                "model": self.model,
                "messages": [
                    {"role": "system", "content": self.system_prompt},
                    *messages,
                ],
                "tools": self.tools.anthropic_specs(),
                "temperature": self.temperature,
                "max_tokens": 1500,
            }
            if fallbacks:
                kwargs["fallbacks"] = fallbacks

            try:
                start = time.perf_counter()
                response = await litellm.acompletion(**kwargs)
                step_latency = time.perf_counter() - start
            except Exception as exc:
                logger.exception("agent.llm_call_failed iteration=%d", iteration)
                return AgentResult(
                    status="error", iterations=iteration,
                    total_cost_usd=total_cost, traces=traces,
                    error=f"LLM call failed: {exc}",
                )

            try:
                cost = float(litellm.completion_cost(completion_response=response))
            except Exception:
                cost = 0.0
            total_cost += cost

            choice = response.choices[0]
            content = choice.message.content
            tool_calls = getattr(choice.message, "tool_calls", None) or []

            assistant_text = self._extract_assistant_text(content)
            if assistant_text:
                traces.append(AgentTrace(
                    step=iteration, role="assistant",
                    text=assistant_text, cost_usd=cost,
                ))

            # No tool calls and a text answer → the agent stopped on its own.
            if not tool_calls:
                logger.info(
                    "agent.finished_no_tool iteration=%d latency=%.2f cost=%.4f",
                    iteration, step_latency, total_cost,
                )
                return AgentResult(
                    status="finished",
                    final_message=assistant_text,
                    iterations=iteration,
                    total_cost_usd=total_cost,
                    traces=traces,
                )

            # Append the assistant message verbatim so the tool_use ids
            # match in subsequent tool_result blocks.
            messages.append({
                "role": "assistant",
                "content": self._assistant_for_history(content, tool_calls),
            })

            tool_results: list[dict[str, Any]] = []
            stop_after = False
            for call in tool_calls:
                name, args, call_id = self._unpack_tool_call(call)
                tool = self.tools.by_name(name)
                if tool is None:
                    result = ToolResult(
                        content=f"Unknown tool: {name}", is_error=True,
                    )
                else:
                    result = await tool.execute(args)
                    if tool.terminal and not result.is_error:
                        stop_after = True
                traces.append(AgentTrace(
                    step=iteration, role="tool",
                    tool_name=name, tool_input=args,
                    tool_result=result.to_anthropic_content(),
                    is_error=result.is_error,
                ))
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": call_id,
                    "content": result.to_anthropic_content(),
                    "is_error": result.is_error,
                })

            messages.append({"role": "user", "content": tool_results})

            if stop_after:
                logger.info(
                    "agent.finished_terminal iteration=%d cost=%.4f",
                    iteration, total_cost,
                )
                return AgentResult(
                    status="finished",
                    final_message=assistant_text or "(terminal tool called)",
                    iterations=iteration,
                    total_cost_usd=total_cost,
                    traces=traces,
                )

        logger.info(
            "agent.max_iterations hit cap=%d cost=%.4f",
            self.max_iterations, total_cost,
        )
        return AgentResult(
            status="iterations", iterations=iteration,
            total_cost_usd=total_cost, traces=traces,
        )

    # -----------------------------------------------------------------
    # Anthropic / litellm content unpacking
    # -----------------------------------------------------------------

    @staticmethod
    def _extract_assistant_text(content: Any) -> str:
        """Pull readable text from the assistant message content.

        litellm normalizes Anthropic's blocks differently across versions
        — sometimes content is a plain string, sometimes a list of
        {type, text} blocks. We accept both.
        """
        if content is None:
            return ""
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            parts = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    parts.append(str(block.get("text", "")))
            return "\n".join(parts).strip()
        return str(content).strip()

    @staticmethod
    def _assistant_for_history(
        content: Any, tool_calls: list[Any],
    ) -> Any:
        """Build the assistant message we feed back into the conversation.

        Anthropic needs tool_use blocks in the assistant turn; OpenAI's
        format is similar but with a `tool_calls` array — litellm
        accepts both shapes on inputs, so the cleanest is to send back
        the same content we received (text + tool_use blocks).
        """
        if isinstance(content, list):
            return content
        # Synthesize a minimal list of blocks: any text, then one tool_use
        # block per call.
        blocks: list[dict[str, Any]] = []
        if isinstance(content, str) and content.strip():
            blocks.append({"type": "text", "text": content})
        for c in tool_calls:
            name, args, call_id = AgentLoop._unpack_tool_call(c)
            blocks.append({
                "type": "tool_use",
                "id": call_id,
                "name": name,
                "input": args,
            })
        return blocks

    @staticmethod
    def _unpack_tool_call(call: Any) -> tuple[str, dict[str, Any], str]:
        """Return (name, args, call_id) from a litellm tool_call object.

        litellm exposes tool calls in roughly the OpenAI shape:
          call.function.name + call.function.arguments (JSON string) + call.id.
        Anthropic-native dicts also show up as {type:'tool_use', id, name, input}.
        """
        if isinstance(call, dict):
            if call.get("type") == "tool_use":
                return (
                    str(call.get("name", "")),
                    dict(call.get("input") or {}),
                    str(call.get("id", "")),
                )
            fn = call.get("function") or {}
            args_raw = fn.get("arguments") or "{}"
            try:
                args = json.loads(args_raw) if isinstance(args_raw, str) else dict(args_raw)
            except json.JSONDecodeError:
                args = {}
            return (
                str(fn.get("name", "")),
                args,
                str(call.get("id", "")),
            )
        # SimpleNamespace-style object (litellm response).
        fn = getattr(call, "function", None)
        if fn is not None:
            args_raw = getattr(fn, "arguments", "") or "{}"
            try:
                args = json.loads(args_raw) if isinstance(args_raw, str) else dict(args_raw)
            except json.JSONDecodeError:
                args = {}
            return (
                str(getattr(fn, "name", "") or ""),
                args,
                str(getattr(call, "id", "") or ""),
            )
        return ("", {}, "")


__all__ = [
    "AgentBudgetExceeded",
    "AgentLoop",
    "AgentResult",
    "AgentTrace",
    "Tool",
]
