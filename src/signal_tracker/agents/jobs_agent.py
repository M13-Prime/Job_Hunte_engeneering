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

import asyncio
import json
from collections.abc import Callable
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
from signal_tracker.storage.models import JobAgentScore, JobOffer, Signal, UserCV
from signal_tracker.utils.logging import get_logger

logger = get_logger(__name__)


SYSTEM_PROMPT = """\
You are a Jobs Agent. Given a backlog of recently-scraped open positions
at companies that just produced a hiring signal for the active user,
you decide which ones deserve their attention and rescore them
semantically against their CV.

Tools available:
- list_companies_with_recent_signals(days, limit) → company names worth
  scraping right now (companies the user already has signals for).
- scrape_companies_jobs(company_names) → BATCH scrape: runs the ATS
  scraper for ALL companies in parallel (asyncio.gather). Pass the full
  list at once — do NOT call this tool one company at a time.
- list_unscored_jobs(limit, min_heuristic_score) → offers waiting for
  semantic scoring (for THIS user — already-scored ones are filtered).
- score_jobs_batch(job_ids) → BATCH semantic scoring: runs the LLM
  scoring call for up to 10 jobs in parallel. Pass the IDs at once.
- mark_irrelevant(job_id, reason) → skip an offer that's clearly not
  a match without an LLM call.
- finish(reason) → stop the run.

Strategy (optimized for speed):
1. list_companies_with_recent_signals(days=14, limit=5).
2. scrape_companies_jobs with the FULL list returned above — single call.
3. list_unscored_jobs(limit=20, min_heuristic_score=10).
4. score_jobs_batch with the FULL list returned above — single call
   (the tool parallelizes internally).
5. finish when the unscored backlog is empty.

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


# Skip re-scraping a company whose offers were last refreshed less
# than this many hours ago. Tight enough that openings turnover stays
# visible; loose enough that two searches the same day don't redo the
# work.
_SCRAPE_TTL_HOURS = 24
# Cap on the batch sizes so we don't fan out 50 parallel ATS calls.
_SCRAPE_CONCURRENCY = 5
_SCORE_CONCURRENCY = 5


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


def _upsert_score(
    db: Database, *, user_id: int, job_offer_id: int,
    fit_score: float, fit_reasoning: str | None,
    killer_angle: str | None, why_now: str | None,
) -> None:
    """Insert-or-update one row in job_agent_scores."""
    with db.session() as s:
        existing = s.execute(
            select(JobAgentScore).where(and_(
                JobAgentScore.user_id == user_id,
                JobAgentScore.job_offer_id == job_offer_id,
            ))
        ).scalar_one_or_none()
        if existing is None:
            s.add(JobAgentScore(
                user_id=user_id, job_offer_id=job_offer_id,
                agent_score=fit_score, agent_fit_reasoning=fit_reasoning,
                agent_killer_angle=killer_angle, agent_why_now=why_now,
                processed_at=datetime.now(UTC),
            ))
        else:
            existing.agent_score = fit_score
            existing.agent_fit_reasoning = fit_reasoning
            existing.agent_killer_angle = killer_angle
            existing.agent_why_now = why_now
            existing.processed_at = datetime.now(UTC)


def _build_tools(state: JobsState) -> ToolRegistry:
    @tool(description="Return distinct companies that THIS USER produced "
                      "at least one signal for in the last `days` days. "
                      "Ordered by max signal score.")
    async def list_companies_with_recent_signals(
        days: int = 14, limit: int = 5,
    ) -> dict[str, Any]:
        cutoff = datetime.now(UTC) - timedelta(days=max(1, days))
        capped = max(1, min(int(limit), 20))
        # Scope: only signals from this user's search_runs.
        with state.db.session() as s:
            assert isinstance(s, Session)
            stmt = (
                select(
                    Signal.company_name,
                    Signal.company_normalized,
                    Signal.total_score,
                )
                .where(Signal.created_at >= cutoff)
                .order_by(Signal.total_score.desc())
            )
            if state.user_id is not None:
                from signal_tracker.storage.models import SearchRun as _SR
                user_run_ids = list(s.execute(
                    select(_SR.id).where(_SR.user_id == state.user_id)
                ).scalars())
                if not user_run_ids:
                    return {"companies": []}
                stmt = stmt.where(Signal.search_run_id.in_(user_run_ids))
            rows = s.execute(stmt).all()
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

    @tool(description="BATCH scrape: run ATS scraping for many companies "
                      "in parallel. Skips companies already scraped within "
                      "the last 24 h (TTL cache). Returns per-company "
                      "outcomes.")
    async def scrape_companies_jobs(
        company_names: list[str],
    ) -> dict[str, Any]:
        if not company_names:
            return {"results": []}
        # TTL filter: drop any company whose JobOffer rows were collected
        # in the last 24 h — saves the network round-trips on reruns.
        ttl_cutoff = datetime.now(UTC) - timedelta(hours=_SCRAPE_TTL_HOURS)
        with state.db.session() as s:
            assert isinstance(s, Session)
            from signal_tracker.utils.normalize import normalize_company_name
            normalized_map = {
                name: normalize_company_name(name) for name in company_names
            }
            fresh = set(s.execute(
                select(JobOffer.company_normalized)
                .where(and_(
                    JobOffer.company_normalized.in_(normalized_map.values()),
                    JobOffer.collected_at >= ttl_cutoff,
                ))
                .distinct()
            ).scalars())
        to_scrape = [
            name for name, norm in normalized_map.items() if norm not in fresh
        ]
        skipped = [
            name for name, norm in normalized_map.items() if norm in fresh
        ]

        scraper = JobsScraper()
        sem = asyncio.Semaphore(_SCRAPE_CONCURRENCY)

        async def _one(client: httpx.AsyncClient, name: str) -> dict[str, Any]:
            async with sem:
                try:
                    res = await scraper.scrape_company(client, name)
                except Exception as exc:
                    state.errors += 1
                    return {"company": name, "found": False, "error": str(exc)[:200]}
                if not res.found:
                    return {"company": name, "found": False, "error": res.error}
                report = ScrapingReport()
                persist_result(state.db, res, state.profile, report)
                state.companies_scraped += 1
                state.jobs_added += report.jobs_new
                return {
                    "company": name, "found": True, "ats": res.ats,
                    "added": report.jobs_new, "updated": report.jobs_updated,
                }

        async with httpx.AsyncClient(
            timeout=20.0, follow_redirects=True,
            headers={"User-Agent": "signal-tracker/0.1 (+jobs)"},
        ) as client:
            results = await asyncio.gather(*[_one(client, n) for n in to_scrape])

        for name in skipped:
            results.append({"company": name, "skipped": "fresh_cache"})
        return {"results": results}

    @tool(description="List open offers not yet scored by the agent FOR "
                      "THIS USER (already-scored offers for other users "
                      "are still listed if this user hasn't seen them yet). "
                      "Filtered by heuristic ≥ min_heuristic_score.")
    async def list_unscored_jobs(
        limit: int = 20, min_heuristic_score: int = 0,
    ) -> dict[str, Any]:
        capped = max(1, min(int(limit), 50))
        with state.db.session() as s:
            assert isinstance(s, Session)
            # Anti-join JobAgentScore on (user_id, job_offer_id).
            scored_for_user = (
                select(JobAgentScore.job_offer_id)
                .where(JobAgentScore.user_id == (state.user_id or -1))
            )
            rows = list(s.execute(
                select(
                    JobOffer.id, JobOffer.company_name, JobOffer.title,
                    JobOffer.relevance_score,
                )
                .where(and_(
                    JobOffer.is_open.is_(True),
                    JobOffer.id.not_in(scored_for_user),
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

    @tool(description="BATCH semantic scoring: runs the LLM scoring call "
                      "for up to 10 jobs in parallel. Persists each verdict "
                      "to job_agent_scores for THIS user.")
    async def score_jobs_batch(job_ids: list[int]) -> dict[str, Any]:
        if not job_ids:
            return {"scored": []}
        if state.user_id is None:
            raise ToolError("no user context; cannot persist scores")

        # Collect the (job, recent_signal) pairs in one session pass.
        targets: list[tuple[JobOffer, Signal | None]] = []
        with state.db.session() as s:
            assert isinstance(s, Session)
            for job_id in job_ids[:_SCORE_CONCURRENCY * 2]:
                job = s.get(JobOffer, int(job_id))
                if job is None:
                    continue
                signal = s.execute(
                    select(Signal)
                    .where(Signal.company_normalized == job.company_normalized)
                    .order_by(
                        Signal.total_score.desc(), Signal.created_at.desc(),
                    )
                    .limit(1)
                ).scalar_one_or_none()
                s.expunge(job)
                if signal is not None:
                    s.expunge(signal)
                targets.append((job, signal))

        sem = asyncio.Semaphore(_SCORE_CONCURRENCY)

        async def _one(job: JobOffer, sig: Signal | None) -> dict[str, Any]:
            async with sem:
                try:
                    verdict = await _semantic_score(
                        job=job, recent_signal=sig,
                        profile=state.profile, cv_text=state.cv_text,
                    )
                except ToolError as exc:
                    state.errors += 1
                    return {"job_id": job.id, "status": "error", "error": str(exc)[:200]}
                _upsert_score(
                    state.db,
                    user_id=state.user_id,  # type: ignore[arg-type]
                    job_offer_id=job.id,
                    fit_score=float(verdict["fit_score"]),
                    fit_reasoning=verdict["fit_reasoning"],
                    killer_angle=verdict["killer_angle"],
                    why_now=verdict["why_now"],
                )
                state.jobs_scored += 1
                return {"job_id": job.id, "status": "scored", **verdict}

        results = await asyncio.gather(*[_one(j, s) for j, s in targets])
        return {"scored": results}

    @tool(description="Mark an offer as 'reviewed but not worth scoring' "
                      "FOR THIS USER — persists a 0-score in "
                      "job_agent_scores. Use for obvious misses to save "
                      "tokens.")
    async def mark_irrelevant(job_id: int, reason: str) -> dict[str, Any]:
        if state.user_id is None:
            raise ToolError("no user context; cannot persist")
        with state.db.session() as s:
            row = s.get(JobOffer, int(job_id))
            if row is None:
                raise ToolError(f"job {job_id} not found")
        _upsert_score(
            state.db,
            user_id=state.user_id,
            job_offer_id=int(job_id),
            fit_score=0.0,
            fit_reasoning=f"(skipped) {reason}"[:500],
            killer_angle=None,
            why_now=None,
        )
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
        scrape_companies_jobs,
        list_unscored_jobs,
        score_jobs_batch,
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
    should_continue: Callable[[], bool] | None = None,
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
        should_continue=should_continue,
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
