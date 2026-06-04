"""Jobs Agent — semantic scoring of job offers against the user (Phase 11).

The existing heuristic `score_job` is a deterministic keyword check —
fast, cheap, but blind to semantics ("Salesforce admin" on the CV vs
"CRM Implementation Lead" on the offer = no keyword hit, but a clear
fit). The Jobs Agent adds a semantic layer on top, only for offers
that already cleared the heuristic floor.

Per company-with-recent-signals:
  1. Trigger the existing scraper (greenhouse / lever / ashby / etc.).
  2. For each offer that just got a non-zero heuristic score, do an
     LLM pass that:
       - rescores the fit vs the actual CV (0-100),
       - extracts a 1-line "killer angle" (the strongest CV → offer link),
       - explains "why now" given the recent signal.
  3. Persist the verdict on JobOffer (agent_score / agent_*) so the UI
     can sort by it and the digest can surface the top picks.

The agent decides which offers warrant the full semantic pass and
which it skips. The budget cap stops it dead before it costs more
than what it'll save.

Feature-flagged via LLM_JOBS_AGENT_ENABLED (off by default).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from signal_tracker.agents.loop import AgentLoop, AgentResult
from signal_tracker.agents.tools.base import ToolError, ToolRegistry, tool
from signal_tracker.classifier.llm import _extract_json, _resolve_fallbacks
from signal_tracker.config import UserProfile, get_settings
from signal_tracker.jobs.scraper import JobsScraper, ScrapingReport, persist_result
from signal_tracker.storage import Database
from signal_tracker.storage.models import JobOffer, Signal, UserCV
from signal_tracker.utils.logging import get_logger

logger = get_logger(__name__)


SYSTEM_PROMPT = """\
You are a Jobs Agent. Given a backlog of recently-scraped open positions
at companies that just produced a hiring signal, you decide which ones
deserve the user's attention and rescore them semantically against
their CV.

Tools available:
- list_companies_with_recent_signals(days, limit) → company names worth
  scraping right now (recent strong signal, no jobs cached yet).
- scrape_company_jobs(company_name) → run the ATS scraper for ONE
  company. Returns the number of offers added.
- list_unscored_jobs(limit, min_heuristic_score) → offers waiting for
  semantic scoring.
- get_job_with_signal_context(job_id) → the full posting + recent
  signal context for that company + a CV excerpt.
- score_job_semantically(job_id, agent_score, fit_reasoning,
  killer_angle, why_now) → persist the LLM verdict.
- mark_irrelevant(job_id, reason) → skip an offer that's clearly not
  a match (saves a full scoring pass).
- finish(reason) → stop the run.

Strategy:
1. list_companies_with_recent_signals(days=14, limit=5).
2. For each company, scrape_company_jobs.
3. list_unscored_jobs(limit=20, min_heuristic_score=10).
4. For each job: get_job_with_signal_context, then either
   score_job_semantically (if the fit is non-trivial) or
   mark_irrelevant (saves tokens on clear misses).
5. finish when the unscored backlog is empty or you've processed 20+
   jobs.

Be concise. No commentary. Output JSON-strict tool inputs.
"""


_SCORE_SYSTEM_PROMPT = """\
You rescore a job posting against a candidate's CV given a recent
hiring signal at the company. Output strict JSON only:
{
  "fit_score": int 0..100,
  "fit_reasoning": "1 short FR sentence",
  "killer_angle": "1 FR sentence: the single strongest CV → role link",
  "why_now": "1 FR sentence: why the recent signal makes the timing right"
}

Score guide:
  0-30  : not a match (sector or seniority mismatch)
  31-60 : partial match (some skills relevant, key gaps)
  61-85 : strong match
  86-100: textbook match
"""


@dataclass(slots=True)
class JobsState:
    """Running counters + shared context for the agent's tools."""

    db: Database
    profile: UserProfile
    user_id: int | None
    cv_text: str
    companies_scraped: int = 0
    jobs_added: int = 0
    jobs_scored: int = 0
    jobs_skipped: int = 0
    errors: int = 0
    history: list[str] = field(default_factory=list)


def _load_cv_text(db: Database, user_id: int | None) -> str:
    if user_id is None:
        return ""
    with db.session() as s:
        cv = s.execute(
            select(UserCV).where(UserCV.user_id == user_id).limit(1)
        ).scalar_one_or_none()
        if cv is None:
            return ""
        return cv.text or ""


async def _semantic_score(
    *, job: JobOffer, recent_signal: Signal | None,
    profile: UserProfile, cv_text: str,
) -> dict[str, Any]:
    """One cheap LLM call: 0-100 score + reasoning + killer angle + why_now."""
    import litellm
    settings = get_settings()
    model = settings.llm_cheap_model or settings.llm_model
    fallbacks = _resolve_fallbacks(settings.llm_fallback_model)
    signal_block = (
        f"RECENT SIGNAL: {recent_signal.signal_type} — {recent_signal.summary_fr}\n"
        f"  recommended_action={recent_signal.recommended_action}\n"
        f"  total_score={recent_signal.total_score}\n"
        if recent_signal else "RECENT SIGNAL: (none)\n"
    )
    user = (
        f"COMPANY: {job.company_name}\n"
        f"JOB TITLE: {job.title}\n"
        f"LOCATION: {job.location or '(unspecified)'}\n"
        f"DEPARTMENT: {job.department or '(unspecified)'}\n"
        f"DESCRIPTION (excerpt):\n{(job.description or '')[:2500]}\n\n"
        f"{signal_block}\n"
        f"USER TARGET ROLES: {', '.join(profile.target_roles) or '(none)'}\n"
        f"USER DOMAINS: {', '.join(profile.domains) or '(none)'}\n"
        f"USER GEOGRAPHIES: {', '.join(profile.geographies) or '(none)'}\n\n"
        f"USER CV (excerpt):\n{cv_text[:3000]}\n\n"
        "Rescore + extract the angle + explain the timing."
    )
    kwargs: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": _SCORE_SYSTEM_PROMPT},
            {"role": "user", "content": user},
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0.0,
        "max_tokens": 400,
    }
    if fallbacks:
        kwargs["fallbacks"] = fallbacks
    response = await litellm.acompletion(**kwargs)
    text = response.choices[0].message.content
    if not isinstance(text, str) or not text.strip():
        raise ToolError("empty semantic-score response")
    try:
        data = json.loads(_extract_json(text))
    except json.JSONDecodeError as exc:
        raise ToolError(f"semantic-score returned invalid JSON: {exc}") from exc
    return {
        "fit_score": max(0, min(100, int(data.get("fit_score", 0)))),
        "fit_reasoning": str(data.get("fit_reasoning", ""))[:500],
        "killer_angle": str(data.get("killer_angle", ""))[:500],
        "why_now": str(data.get("why_now", ""))[:500],
    }


def _build_tools(state: JobsState) -> ToolRegistry:
    @tool(description="Return distinct companies that produced at least one "
                      "signal in the last `days` days and don't have any open "
                      "scraped offer yet. Ordered by max signal score.")
    async def list_companies_with_recent_signals(
        days: int = 14, limit: int = 5,
    ) -> dict[str, Any]:
        cutoff = datetime.now(UTC) - timedelta(days=max(1, days))
        capped = max(1, min(int(limit), 20))
        with state.db.session() as s:
            assert isinstance(s, Session)
            rows = s.execute(
                select(
                    Signal.company_name,
                    Signal.company_normalized,
                    Signal.total_score,
                )
                .where(Signal.created_at >= cutoff)
                .order_by(Signal.total_score.desc())
            ).all()
        seen: set[str] = set()
        out: list[dict[str, Any]] = []
        for name, normalized, score in rows:
            if normalized in seen:
                continue
            seen.add(normalized)
            out.append({
                "company_name": name,
                "company_normalized": normalized,
                "max_signal_score": float(score),
            })
            if len(out) >= capped:
                break
        return {"companies": out}

    @tool(description="Run the ATS scraper for ONE company. Returns the "
                      "number of offers found / added.")
    async def scrape_company_jobs(company_name: str) -> dict[str, Any]:
        scraper = JobsScraper()
        report = ScrapingReport()
        try:
            async with httpx.AsyncClient(
                timeout=20.0, follow_redirects=True,
                headers={"User-Agent": "signal-tracker/0.1 (+jobs)"},
            ) as client:
                result = await scraper.scrape_company(client, company_name)
        except Exception as exc:
            state.errors += 1
            raise ToolError(f"scraper failed: {exc}") from exc
        if not result.found:
            return {
                "found": False, "ats": None, "added": 0,
                "error": result.error,
            }
        persist_result(state.db, result, state.profile, report)
        state.companies_scraped += 1
        state.jobs_added += report.jobs_new
        return {
            "found": True, "ats": result.ats,
            "added": report.jobs_new, "updated": report.jobs_updated,
            "total": report.jobs_collected,
        }

    @tool(description="List open offers not yet semantically scored by "
                      "the agent, with heuristic score ≥ min_heuristic_score. "
                      "Ordered by heuristic score desc.")
    async def list_unscored_jobs(
        limit: int = 20, min_heuristic_score: int = 0,
    ) -> dict[str, Any]:
        capped = max(1, min(int(limit), 50))
        with state.db.session() as s:
            assert isinstance(s, Session)
            rows = list(s.execute(
                select(
                    JobOffer.id, JobOffer.company_name, JobOffer.title,
                    JobOffer.relevance_score,
                )
                .where(and_(
                    JobOffer.is_open.is_(True),
                    JobOffer.agent_processed_at.is_(None),
                    JobOffer.relevance_score >= float(min_heuristic_score),
                ))
                .order_by(JobOffer.relevance_score.desc())
                .limit(capped)
            ).all())
        return {"jobs": [
            {
                "job_id": r.id, "company": r.company_name,
                "title": r.title, "heuristic_score": r.relevance_score,
            }
            for r in rows
        ]}

    @tool(description="Run the semantic scoring LLM call on one job, then "
                      "persist the verdict. The fit_score / killer_angle / "
                      "why_now / fit_reasoning are computed by the model and "
                      "you do NOT pass them — the tool computes them.")
    async def score_job_semantically(job_id: int) -> dict[str, Any]:
        with state.db.session() as s:
            assert isinstance(s, Session)
            job = s.get(JobOffer, int(job_id))
            if job is None:
                raise ToolError(f"job {job_id} not found")
            if job.agent_processed_at is not None:
                return {"status": "already_scored", "job_id": job.id}
            # Pick the most recent / highest signal for the company.
            recent_signal = s.execute(
                select(Signal)
                .where(Signal.company_normalized == job.company_normalized)
                .order_by(Signal.total_score.desc(), Signal.created_at.desc())
                .limit(1)
            ).scalar_one_or_none()
            # Detach: we use these values outside the session.
            scoring_input = (job, recent_signal)

        verdict = await _semantic_score(
            job=scoring_input[0], recent_signal=scoring_input[1],
            profile=state.profile, cv_text=state.cv_text,
        )

        with state.db.session() as s:
            row = s.get(JobOffer, int(job_id))
            if row is None:
                raise ToolError(f"job {job_id} vanished")
            row.agent_score = float(verdict["fit_score"])
            row.agent_fit_reasoning = verdict["fit_reasoning"]
            row.agent_killer_angle = verdict["killer_angle"]
            row.agent_why_now = verdict["why_now"]
            row.agent_processed_at = datetime.now(UTC)
        state.jobs_scored += 1
        state.history.append(f"scored job {job_id} → {verdict['fit_score']}")
        return {"status": "scored", "job_id": int(job_id), **verdict}

    @tool(description="Mark an offer as 'reviewed but not worth scoring' — "
                      "sets agent_processed_at without an agent_score. Use "
                      "this for obvious misses (wrong seniority, wrong "
                      "geography) to save tokens.")
    async def mark_irrelevant(job_id: int, reason: str) -> dict[str, Any]:
        with state.db.session() as s:
            row = s.get(JobOffer, int(job_id))
            if row is None:
                raise ToolError(f"job {job_id} not found")
            row.agent_score = 0.0
            row.agent_fit_reasoning = f"(skipped) {reason}"[:500]
            row.agent_processed_at = datetime.now(UTC)
        state.jobs_skipped += 1
        return {"status": "skipped", "job_id": int(job_id), "reason": reason}

    @tool(
        description="Stop the run. Pass a short reason "
                    "('backlog_empty', 'budget_aware', 'errors', 'done').",
        terminal=True,
    )
    async def finish(reason: str) -> dict[str, Any]:
        return {"finished": True, "reason": reason}

    reg = ToolRegistry()
    reg.extend([
        list_companies_with_recent_signals,
        scrape_company_jobs,
        list_unscored_jobs,
        score_job_semantically,
        mark_irrelevant,
        finish,
    ])
    return reg


async def run_jobs_agent(
    *,
    db: Database,
    profile: UserProfile,
    user_id: int | None,
    budget_usd: float | None = None,
) -> tuple[AgentResult, JobsState]:
    """Run the Jobs Agent end-to-end. Returns (AgentResult, JobsState)."""
    settings = get_settings()
    cv_text = _load_cv_text(db, user_id)
    state = JobsState(
        db=db, profile=profile, user_id=user_id, cv_text=cv_text,
    )
    tools = _build_tools(state)
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
        "Process the jobs backlog for the recent signal-bearing companies.\n"
        "Start by listing the most recent signal companies."
    )
    result = await agent.run(user_msg)
    logger.info(
        "jobs_agent.done status=%s scraped=%d added=%d scored=%d skipped=%d cost=%.4f",
        result.status, state.companies_scraped, state.jobs_added,
        state.jobs_scored, state.jobs_skipped, result.total_cost_usd,
    )
    return result, state


__all__ = ["SYSTEM_PROMPT", "JobsState", "run_jobs_agent"]
