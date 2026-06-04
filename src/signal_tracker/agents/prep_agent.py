"""Preparation Agent — agentic prep report generator (Phase 11).

Instead of one monolithic LLM call that produces the whole PreparationReport
in one shot, the agent decomposes the work:
  1. Quick fit assessment — is there enough overlap to bother?
  2. If yes: company intel section, approach plan section, message draft.
  3. Assemble + save.

The agent decides whether to do the deep analysis or bail early with a
"low fit" report. Saves cost when the signal clearly doesn't match the CV.

Feature-flagged via LLM_PREP_AGENT_ENABLED (off by default).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from signal_tracker.agents.loop import AgentLoop, AgentResult
from signal_tracker.agents.tools.base import ToolError, ToolRegistry, tool
from signal_tracker.classifier.llm import _extract_json
from signal_tracker.config import UserProfile, get_settings
from signal_tracker.preparation.llm import generate_preparation
from signal_tracker.preparation.schemas import PreparationReport
from signal_tracker.utils.logging import get_logger

logger = get_logger(__name__)


SYSTEM_PROMPT = """\
You are a Preparation Agent. Given a hiring signal + the user's CV, you
produce a tight preparation plan: how to approach the company, who to
contact, what to say in the first message.

Tools available:
- get_signal_context() → the signal + the article + the user's CV.
- assess_fit() → quick fit verdict + score 0-100.
- generate_full_report() → run the full LLM pipeline that produces a
  validated PreparationReport JSON. Use this when the fit looks decent
  (≥ 35) — it does the company intel, approach plan, and first-message
  draft in one structured pass.
- save_report(report_json) → persist + emit terminal signal. Pass the
  exact JSON returned by generate_full_report.
- give_up(reason) → bail with a graceful "no fit" verdict. Use when
  assess_fit returned a score < 35 and there's no compelling angle.

Flow:
1. Call get_signal_context to see what you're working with.
2. Call assess_fit. If score < 35, call give_up.
3. Otherwise call generate_full_report, then save_report with its output.

Be concise. No commentary. Output JSON-strict tool inputs.
"""


_FIT_SYSTEM_PROMPT = """\
You assess whether a hiring signal and a candidate's CV are a plausible
match. Output strict JSON only:
{"fit_score": int 0..100, "reasoning": "1 phrase FR"}

Score guide:
  0-30  : almost no overlap (different sector, different role, location off)
  31-60 : partial overlap (some skills relevant, but key gaps)
  61-90 : strong overlap (most criteria met)
  91-100: textbook match
"""


@dataclass(slots=True)
class PrepContext:
    """Inputs the agent sees + outputs it produces."""

    # Signal context
    company_name: str
    signal_type: str
    recommended_action: str
    total_score: float
    summary_fr: str
    suggested_angle: str | None
    source: str
    url: str | None
    title: str | None
    content: str | None
    # User context
    profile: UserProfile
    cv_text: str
    # Captured outputs
    fit_score: int | None = None
    fit_reasoning: str | None = None
    final_report: dict[str, Any] | None = None
    final_status: str = "pending"  # 'done' | 'no_fit' | 'pending'
    bailed_reason: str | None = None
    history: list[str] = field(default_factory=list)


async def _quick_fit_assessment(ctx: PrepContext) -> tuple[int, str]:
    """One cheap LLM call: rough fit score 0-100 + reasoning."""
    import litellm

    from signal_tracker.classifier.llm import _resolve_fallbacks
    settings = get_settings()
    model = settings.llm_cheap_model or settings.llm_model
    fallbacks = _resolve_fallbacks(settings.llm_fallback_model)
    user = (
        f"COMPANY: {ctx.company_name}\n"
        f"SIGNAL TYPE: {ctx.signal_type}\n"
        f"SIGNAL SUMMARY (FR): {ctx.summary_fr}\n"
        f"SUGGESTED ANGLE: {ctx.suggested_angle or '(none)'}\n\n"
        f"USER DOMAINS: {', '.join(ctx.profile.domains) or '(none)'}\n"
        f"USER ROLES: {', '.join(ctx.profile.target_roles) or '(none)'}\n\n"
        f"USER CV (excerpt):\n{ctx.cv_text[:3000]}\n\n"
        "Score the fit."
    )
    kwargs: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": _FIT_SYSTEM_PROMPT},
            {"role": "user", "content": user},
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0.0,
        "max_tokens": 200,
    }
    if fallbacks:
        kwargs["fallbacks"] = fallbacks
    response = await litellm.acompletion(**kwargs)
    text = response.choices[0].message.content
    if not isinstance(text, str) or not text.strip():
        raise ToolError("empty fit-assessment response")
    try:
        data = json.loads(_extract_json(text))
    except json.JSONDecodeError as exc:
        raise ToolError(f"fit assessment returned invalid JSON: {exc}") from exc
    score = int(data.get("fit_score", 0))
    reasoning = str(data.get("reasoning", ""))
    return max(0, min(100, score)), reasoning


def _build_tools(ctx: PrepContext) -> ToolRegistry:
    @tool(description="Return the signal context + the user's CV snippet.")
    async def get_signal_context() -> dict[str, Any]:
        return {
            "company_name": ctx.company_name,
            "signal_type": ctx.signal_type,
            "recommended_action": ctx.recommended_action,
            "signal_score": ctx.total_score,
            "summary_fr": ctx.summary_fr,
            "suggested_angle": ctx.suggested_angle,
            "article_title": ctx.title,
            "article_url": ctx.url,
            "user_domains": list(ctx.profile.domains),
            "user_target_roles": list(ctx.profile.target_roles),
            "user_geographies": list(ctx.profile.geographies),
            "cv_excerpt": ctx.cv_text[:1500],
        }

    @tool(description="Quick fit assessment between signal and CV. "
                      "Returns {fit_score, reasoning}.")
    async def assess_fit() -> dict[str, Any]:
        score, reasoning = await _quick_fit_assessment(ctx)
        ctx.fit_score = score
        ctx.fit_reasoning = reasoning
        ctx.history.append(f"assess_fit → {score}")
        return {"fit_score": score, "reasoning": reasoning}

    @tool(description="Run the full preparation LLM pipeline (company "
                      "intel + approach plan + first message). Returns "
                      "a validated PreparationReport JSON ready to be "
                      "saved.")
    async def generate_full_report() -> dict[str, Any]:
        try:
            report = await generate_preparation(
                company_name=ctx.company_name,
                signal_type=ctx.signal_type,
                recommended_action=ctx.recommended_action,
                total_score=ctx.total_score,
                summary_fr=ctx.summary_fr,
                suggested_angle=ctx.suggested_angle,
                source=ctx.source, url=ctx.url, title=ctx.title,
                content=ctx.content,
                profile=ctx.profile,
                cv_text=ctx.cv_text,
            )
        except Exception as exc:
            raise ToolError(f"preparation pipeline failed: {exc}") from exc
        return report.model_dump()

    @tool(
        description="Persist the final PreparationReport. Pass the exact "
                    "JSON returned by generate_full_report.",
        terminal=True,
    )
    async def save_report(report_json: dict[str, Any]) -> dict[str, Any]:
        try:
            validated = PreparationReport.model_validate(report_json)
        except Exception as exc:
            raise ToolError(f"report JSON does not validate: {exc}") from exc
        ctx.final_report = validated.model_dump()
        ctx.final_status = "done"
        ctx.history.append("save_report → done")
        return {"saved": True}

    @tool(
        description="Bail out of the run with a brief reason. Use when "
                    "fit_score is too low to justify a full report.",
        terminal=True,
    )
    async def give_up(reason: str) -> dict[str, Any]:
        ctx.bailed_reason = reason
        ctx.final_status = "no_fit"
        ctx.history.append(f"give_up → {reason}")
        return {"bailed": True, "reason": reason}

    reg = ToolRegistry()
    reg.extend([
        get_signal_context, assess_fit,
        generate_full_report, save_report, give_up,
    ])
    return reg


async def run_prep_agent(
    *,
    company_name: str,
    signal_type: str,
    recommended_action: str,
    total_score: float,
    summary_fr: str,
    suggested_angle: str | None,
    source: str,
    url: str | None,
    title: str | None,
    content: str | None,
    profile: UserProfile,
    cv_text: str,
    budget_usd: float | None = None,
) -> tuple[AgentResult, PrepContext]:
    """Run the Prep Agent end-to-end. Returns (AgentResult, PrepContext)."""
    settings = get_settings()
    ctx = PrepContext(
        company_name=company_name,
        signal_type=signal_type,
        recommended_action=recommended_action,
        total_score=total_score,
        summary_fr=summary_fr,
        suggested_angle=suggested_angle,
        source=source, url=url, title=title, content=content,
        profile=profile, cv_text=cv_text,
    )
    tools = _build_tools(ctx)
    agent = AgentLoop(
        model=settings.llm_model,
        system_prompt=SYSTEM_PROMPT,
        tools=tools,
        budget_usd=budget_usd if budget_usd is not None else settings.llm_agent_budget_usd,
        max_iterations=settings.llm_agent_max_iterations,
        temperature=0.0,
        fallback_model=settings.llm_fallback_model,
    )
    user_msg = (
        f"Build a preparation plan for the signal about \"{company_name}\".\n"
        "Start by calling get_signal_context."
    )
    result = await agent.run(user_msg)
    logger.info(
        "prep_agent.done status=%s final=%s cost=%.4f",
        result.status, ctx.final_status, result.total_cost_usd,
    )
    return result, ctx


__all__ = ["SYSTEM_PROMPT", "PrepContext", "run_prep_agent"]
