"""Research Agent — autonomous classifier loop (Phase 11).

Replaces the linear `run_classification` step with an LLM-driven loop:
the agent picks unclassified articles, runs the classifier on them, and
decides when to stop (low yield → done early, budget hit → save and stop).

Wraps the existing classifier under the hood — the agent doesn't
re-implement scoring, it just orchestrates the work and reports
progress so the dashboard can show it live.

Feature-flagged via LLM_RESEARCH_AGENT_ENABLED (off by default).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from signal_tracker.agents.loop import AgentLoop, AgentResult
from signal_tracker.agents.tools.base import ToolError, ToolRegistry, tool
from signal_tracker.classifier.feedback import load_feedback_examples
from signal_tracker.classifier.llm import classify, prefilter
from signal_tracker.classifier.schemas import ClassifierInput
from signal_tracker.config import UserProfile, get_settings
from signal_tracker.pipeline import _store_signal
from signal_tracker.storage import Database
from signal_tracker.storage.models import RawItem, UserKeyword
from signal_tracker.utils.logging import get_logger
from signal_tracker.utils.normalize import normalize_company_name as _normalize  # noqa: F401

logger = get_logger(__name__)


SYSTEM_PROMPT = """\
You are a Research Agent for a job-seeker's signal pipeline. Your goal:
maximize the number of HIGH-QUALITY signals saved within the budget.

You have tools to:
- list_next_unclassified(limit) → returns IDs of articles waiting to be processed
- classify_and_save(article_id) → runs the classifier; saves a signal if relevant
- get_progress() → counters (processed, saved, errors)
- finish(reason) → stop the run

Strategy:
1. Start by calling list_next_unclassified with limit=10.
2. Process each ID one by one via classify_and_save.
3. Every ~10 articles, call get_progress. If yield < 5 % AND ≥ 30 already
   processed, call finish("low_yield") — don't burn budget on a noisy batch.
4. If list_next_unclassified returns empty, call finish("backlog_empty").
5. If you see repeated errors from classify_and_save, call finish("errors").

Be concise. No commentary between tool calls. Output JSON-strict tool inputs.
"""


@dataclass(slots=True)
class ResearchState:
    """Running counters the agent's tools read & mutate."""

    db: Database
    profile: UserProfile
    user_id: int | None
    search_run_id: int | None
    user_keywords: dict[str, list[str]]
    processed: int = 0
    saved: int = 0
    skipped_prefilter: int = 0
    errors: int = 0


def _build_tools(state: ResearchState) -> ToolRegistry:
    """Bind the research tools to a shared ResearchState."""
    settings = get_settings()
    prefilter_enabled = bool(
        settings.llm_prefilter_enabled and settings.llm_cheap_model
    )
    feedback_examples = load_feedback_examples(state.db)

    @tool(description="List up to `limit` raw_items still waiting to be "
                      "classified. Returns their IDs in insertion order.")
    async def list_next_unclassified(limit: int = 10) -> dict[str, Any]:
        capped = max(1, min(int(limit), 50))
        with state.db.session() as s:
            assert isinstance(s, Session)
            ids = list(s.execute(
                select(RawItem.id)
                .where(RawItem.classified.is_(False))
                .order_by(RawItem.id)
                .limit(capped)
            ).scalars())
        return {"article_ids": ids}

    @tool(description="Classify ONE article (by raw_item id) and persist a "
                      "Signal if the LLM judges it relevant. Returns the "
                      "decision + updated counters.")
    async def classify_and_save(article_id: int) -> dict[str, Any]:
        with state.db.session() as s:
            assert isinstance(s, Session)
            raw = s.get(RawItem, int(article_id))
            if raw is None:
                raise ToolError(f"article {article_id} not found")
            if raw.classified:
                return {"status": "already_classified", "saved": False}
            payload = ClassifierInput(
                source=raw.source, url=raw.url,
                title=raw.title, content=raw.content,
                published_at=raw.published_at,
            )

        # 1) Cheap prefilter (when enabled).
        if prefilter_enabled:
            verdict = await prefilter(payload, state.profile)
            if verdict == "no":
                with state.db.session() as s:
                    row = s.get(RawItem, int(article_id))
                    if row is not None:
                        row.classified = True
                state.processed += 1
                state.skipped_prefilter += 1
                return {
                    "status": "prefilter_skip",
                    "saved": False,
                    "verdict": "no",
                }

        # 2) Full classifier.
        try:
            result = await classify(
                payload,
                state.profile,
                extra_examples=feedback_examples,
                user_keywords=state.user_keywords or None,
            )
        except Exception as exc:
            state.errors += 1
            raise ToolError(f"classifier failed: {exc}") from exc

        # 3) Persist if relevant.
        saved = False
        deduped = False
        with state.db.session() as s:
            row = s.get(RawItem, int(article_id))
            if row is None:
                raise ToolError(f"article {article_id} vanished")
            row.classified = True
            if result.is_relevant:
                inserted = _store_signal(
                    s, row, result, search_run_id=state.search_run_id,
                )
                saved = inserted
                deduped = not inserted
        state.processed += 1
        if saved:
            state.saved += 1
        return {
            "status": "classified",
            "saved": saved,
            "deduped": deduped,
            "score": result.total_score,
            "company": result.company_name,
        }

    @tool(description="Return current counters (processed, saved, errors). "
                      "Use this to decide whether to keep going.")
    async def get_progress() -> dict[str, Any]:
        yield_pct = (
            round(100 * state.saved / state.processed, 1)
            if state.processed else 0.0
        )
        return {
            "processed": state.processed,
            "saved": state.saved,
            "skipped_prefilter": state.skipped_prefilter,
            "errors": state.errors,
            "yield_pct": yield_pct,
        }

    @tool(
        description="Stop the run. Pass a short reason "
                    "('backlog_empty', 'low_yield', 'errors', 'done').",
        terminal=True,
    )
    async def finish(reason: str) -> dict[str, Any]:
        return {"finished": True, "reason": reason}

    reg = ToolRegistry()
    reg.extend([list_next_unclassified, classify_and_save, get_progress, finish])
    return reg


async def run_research_agent(
    *,
    db: Database,
    profile: UserProfile,
    user_id: int | None,
    search_run_id: int | None,
    user_keywords: dict[str, list[str]] | None = None,
    budget_usd: float | None = None,
) -> tuple[AgentResult, ResearchState]:
    """Run the Research Agent end-to-end.

    Returns (AgentResult, ResearchState). The state carries the counters
    the dashboard reports as metrics (processed / saved / etc.).
    """
    settings = get_settings()
    user_keywords = user_keywords or {}
    state = ResearchState(
        db=db, profile=profile, user_id=user_id,
        search_run_id=search_run_id, user_keywords=user_keywords,
    )
    tools = _build_tools(state)
    model = settings.llm_model
    agent = AgentLoop(
        model=model,
        system_prompt=SYSTEM_PROMPT,
        tools=tools,
        budget_usd=budget_usd if budget_usd is not None else settings.llm_agent_budget_usd,
        max_iterations=settings.llm_agent_max_iterations,
        temperature=0.0,
        fallback_model=settings.llm_fallback_model,
    )
    user_msg = (
        "Process the backlog of unclassified articles for this user run.\n"
        f"Search run id: {search_run_id}\n"
        f"User keywords: {user_keywords}\n"
        "Begin by listing the next batch."
    )
    result = await agent.run(user_msg)
    logger.info(
        "research_agent.done status=%s processed=%d saved=%d cost=%.4f",
        result.status, state.processed, state.saved, result.total_cost_usd,
    )
    return result, state


__all__ = ["SYSTEM_PROMPT", "ResearchState", "run_research_agent"]


_ = UserKeyword  # keep import for forward use / IDE
