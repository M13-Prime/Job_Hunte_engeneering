"""AI match score for job offers, per user (Phase 12, Jobright-style).

The Jobs Agent (agents/jobs_agent.py) is an autonomous loop that only
looks at companies with fresh signals. This module exposes the same
semantic scoring as plain functions so the dashboard can score any
offer on demand ("Scorer" button) and the daily run can refresh the
user's companies and auto-score the new offers, within a hard cap.

Cost model: one cheap-model call (~600 output tokens) per scored offer.
Nothing here is called in a loop without an explicit `limit`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import and_, desc, select

from signal_tracker.agents.jobs_agent import _semantic_score, _upsert_score
from signal_tracker.agents.tools.base import ToolError
from signal_tracker.config import UserProfile, get_settings, load_jobs_config
from signal_tracker.jobs.scraper import (
    JobsScraper,
    ScrapingReport,
    parse_overrides,
    persist_result,
)
from signal_tracker.storage import Database
from signal_tracker.storage.models import (
    JobAgentScore,
    JobOffer,
    SearchRun,
    Signal,
    UserCV,
    WatchlistEntry,
)
from signal_tracker.utils.logging import get_logger

logger = get_logger(__name__)


class MatchingError(RuntimeError):
    """Raised when an offer cannot be scored (no CV, unknown offer, LLM error)."""


def load_cv_text(db: Database, user_id: int) -> str:
    with db.session() as s:
        cv = s.execute(
            select(UserCV).where(UserCV.user_id == user_id).limit(1)
        ).scalar_one_or_none()
        return (cv.text or "") if cv is not None else ""


async def score_job_for_user(
    db: Database,
    *,
    user_id: int,
    job_offer_id: int,
    profile: UserProfile,
    cv_text: str | None = None,
) -> dict[str, Any]:
    """Score one offer against the user's CV and persist the verdict."""
    cv = cv_text if cv_text is not None else load_cv_text(db, user_id)
    if not cv.strip():
        raise MatchingError("Ajoute ton CV pour obtenir un score de match.")
    with db.session() as s:
        job = s.get(JobOffer, job_offer_id)
        if job is None:
            raise MatchingError(f"Offre {job_offer_id} introuvable.")
        signal = s.execute(
            select(Signal)
            .where(Signal.company_normalized == job.company_normalized)
            .order_by(desc(Signal.total_score), desc(Signal.created_at))
            .limit(1)
        ).scalar_one_or_none()
        s.expunge(job)
        if signal is not None:
            s.expunge(signal)
    try:
        verdict = await _semantic_score(
            job=job, recent_signal=signal, profile=profile, cv_text=cv,
        )
    except ToolError as exc:
        raise MatchingError(str(exc)) from exc
    except Exception as exc:  # network / provider / malformed JSON values
        logger.warning("jobs.score_failed job=%s error=%s", job_offer_id, str(exc)[:200])
        raise MatchingError(f"Le scoring IA a échoué : {str(exc)[:200]}") from exc
    _upsert_score(
        db,
        user_id=user_id,
        job_offer_id=job_offer_id,
        fit_score=float(verdict["fit_score"]),
        fit_reasoning=verdict["fit_reasoning"],
        killer_angle=verdict["killer_angle"],
        why_now=verdict["why_now"] or None,
        matched_skills=verdict["matched_skills"],
        missing_skills=verdict["missing_skills"],
    )
    return verdict


def companies_for_user(
    db: Database, user_id: int, *, signal_days: int = 14, min_signal_score: float = 50.0,
) -> list[str]:
    """The user's watchlist first, then companies from their recent signals."""
    cutoff = datetime.now(UTC) - timedelta(days=signal_days)
    with db.session() as s:
        watch = list(s.execute(
            select(WatchlistEntry.company_name)
            .where(WatchlistEntry.user_id == user_id)
            .order_by(WatchlistEntry.company_name)
        ).scalars())
        run_ids = select(SearchRun.id).where(SearchRun.user_id == user_id)
        from_signals = list(s.execute(
            select(Signal.company_name)
            .where(and_(
                Signal.search_run_id.in_(run_ids),
                Signal.created_at >= cutoff,
                Signal.total_score >= min_signal_score,
            ))
            .order_by(desc(Signal.total_score))
        ).scalars())
    seen: set[str] = set()
    out: list[str] = []
    for name in [*watch, *from_signals]:
        key = (name or "").strip().lower()
        if key and key not in seen:
            seen.add(key)
            out.append(name)
    return out


async def refresh_jobs(
    db: Database, profile: UserProfile, companies: list[str],
) -> ScrapingReport:
    """Scrape the ATS boards of `companies` and upsert the offers."""
    report = ScrapingReport(companies_attempted=len(companies))
    if not companies:
        return report
    jobs_cfg = load_jobs_config()
    scraper = JobsScraper(
        overrides=parse_overrides(jobs_cfg.get("overrides")),
        rate_limit_seconds=float(jobs_cfg.get("rate_limit_seconds", 1.0)),
        timeout_seconds=float(jobs_cfg.get("timeout_seconds", 20.0)),
    )
    for result in await scraper.scrape_companies(companies):
        if result.found:
            report.companies_with_ats += 1
        persist_result(db, result, profile, report)
    return report


def unscored_job_ids(
    db: Database, user_id: int, *, min_heuristic: float, limit: int,
    companies: list[str] | None = None,
) -> list[int]:
    """Open offers this user hasn't scored yet, best keyword score first."""
    if limit <= 0:
        return []
    from signal_tracker.utils.normalize import normalize_company_name

    scored = select(JobAgentScore.job_offer_id).where(JobAgentScore.user_id == user_id)
    stmt = (
        select(JobOffer.id)
        .where(and_(
            JobOffer.is_open.is_(True),
            JobOffer.id.not_in(scored),
            JobOffer.relevance_score >= min_heuristic,
        ))
        .order_by(desc(JobOffer.relevance_score), desc(JobOffer.collected_at))
        .limit(limit)
    )
    if companies is not None:
        stmt = stmt.where(
            JobOffer.company_normalized.in_([normalize_company_name(c) for c in companies])
        )
    with db.session() as s:
        return [int(i) for i in s.execute(stmt).scalars()]


@dataclass(slots=True)
class JobsRefreshResult:
    companies: int = 0
    companies_with_ats: int = 0
    jobs_new: int = 0
    jobs_scored: int = 0
    errors: list[str] = field(default_factory=list)


async def refresh_and_score_for_user(
    db: Database,
    profile: UserProfile,
    user_id: int,
    *,
    score_limit: int | None = None,
    min_heuristic: float | None = None,
) -> JobsRefreshResult:
    """Daily job-alert step: refresh the user's companies, score new offers.

    Scoring is skipped (scrape still runs) when the user has no CV.
    """
    settings = get_settings()
    limit = settings.jobs_auto_score_limit if score_limit is None else score_limit
    floor = (
        settings.jobs_auto_score_min_heuristic if min_heuristic is None else min_heuristic
    )
    out = JobsRefreshResult()
    companies = companies_for_user(db, user_id)
    out.companies = len(companies)
    report = await refresh_jobs(db, profile, companies)
    out.companies_with_ats = report.companies_with_ats
    out.jobs_new = report.jobs_new

    cv_text = load_cv_text(db, user_id)
    if not cv_text.strip():
        return out
    for job_id in unscored_job_ids(
        db, user_id, min_heuristic=floor, limit=limit, companies=companies,
    ):
        try:
            await score_job_for_user(
                db, user_id=user_id, job_offer_id=job_id,
                profile=profile, cv_text=cv_text,
            )
            out.jobs_scored += 1
        except MatchingError as exc:
            out.errors.append(str(exc)[:200])
    logger.info(
        "jobs.refresh_and_score user=%s companies=%d new=%d scored=%d errors=%d",
        user_id, out.companies, out.jobs_new, out.jobs_scored, len(out.errors),
    )
    return out


__all__ = [
    "JobsRefreshResult",
    "MatchingError",
    "companies_for_user",
    "load_cv_text",
    "refresh_and_score_for_user",
    "refresh_jobs",
    "score_job_for_user",
    "unscored_job_ids",
]
