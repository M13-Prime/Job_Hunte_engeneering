"""Per-company scraping orchestrator.

For each company name:
1. Try the manual override (config/jobs.yaml) — single (ats, slug) pair.
2. Otherwise generate slug candidates from the name.
3. For each (slug, backend) combination, fetch the board. The first backend
   that returns a non-None list wins, and we record (ats, slug) so the next
   run skips the trial-and-error.
4. Persist the resulting CompanyJobsResult to the DB via ``persist_result``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass

import httpx
from sqlalchemy import select

from signal_tracker.config import UserProfile
from signal_tracker.jobs.backends import DEFAULT_BACKENDS
from signal_tracker.jobs.backends.base import JobsBackend
from signal_tracker.jobs.relevance import score_job
from signal_tracker.jobs.schemas import ATS, CompanyJobsResult
from signal_tracker.jobs.slug import slug_candidates
from signal_tracker.storage import Database
from signal_tracker.storage.models import JobOffer
from signal_tracker.utils.logging import get_logger
from signal_tracker.utils.normalize import normalize_company_name

logger = get_logger(__name__)


@dataclass(slots=True)
class JobsOverride:
    """One entry from config/jobs.yaml::overrides."""

    company_normalized: str
    ats: str
    slug: str


@dataclass(slots=True)
class ScrapingReport:
    companies_attempted: int = 0
    companies_with_ats: int = 0
    jobs_collected: int = 0
    jobs_new: int = 0
    jobs_updated: int = 0
    errors: int = 0


def parse_overrides(raw: list[dict[str, str]] | None) -> list[JobsOverride]:
    out: list[JobsOverride] = []
    for entry in raw or []:
        normalized = normalize_company_name(
            entry.get("normalized") or entry.get("company") or ""
        )
        ats = entry.get("ats") or ""
        slug = entry.get("slug") or ""
        if normalized and ats and slug:
            out.append(JobsOverride(normalized, ats, slug))
    return out


class JobsScraper:
    def __init__(
        self,
        backends: Sequence[JobsBackend] | None = None,
        overrides: Sequence[JobsOverride] | None = None,
        rate_limit_seconds: float = 1.0,
        timeout_seconds: float = 20.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.backends = list(backends or DEFAULT_BACKENDS)
        self.overrides_by_name: dict[str, JobsOverride] = {
            o.company_normalized: o for o in (overrides or [])
        }
        self.rate_limit_seconds = rate_limit_seconds
        self.timeout_seconds = timeout_seconds
        self._client = client
        self._owns_client = client is None

    def _backend_by_name(self, name: str) -> JobsBackend | None:
        for backend in self.backends:
            if backend.name == name:
                return backend
        return None

    async def scrape_company(
        self, client: httpx.AsyncClient, company_name: str
    ) -> CompanyJobsResult:
        normalized = normalize_company_name(company_name)
        override = self.overrides_by_name.get(normalized)

        if override is not None:
            backend = self._backend_by_name(override.ats)
            if backend is None:
                return CompanyJobsResult(
                    company=company_name,
                    company_normalized=normalized,
                    error=f"unknown ATS in override: {override.ats}",
                )
            jobs = await backend.list_jobs(override.slug, client)
            if jobs is None:
                return CompanyJobsResult(
                    company=company_name,
                    company_normalized=normalized,
                    error=f"override pointed at {override.ats}:{override.slug} but board not found",
                )
            return CompanyJobsResult(
                company=company_name,
                company_normalized=normalized,
                ats=_as_ats_literal(backend.name),
                ats_company_slug=override.slug,
                jobs=jobs,
            )

        # Brute-force slug x backend, hit-rate is usually high for tech.
        for slug in slug_candidates(company_name):
            for backend in self.backends:
                try:
                    jobs = await backend.list_jobs(slug, client)
                except Exception as exc:  # backend bug -> log + keep trying
                    logger.warning(
                        "jobs.backend_error",
                        extra={
                            "company": company_name,
                            "backend": backend.name,
                            "slug": slug,
                            "error": str(exc)[:200],
                        },
                    )
                    continue
                if jobs is None:
                    continue
                logger.info(
                    "jobs.found",
                    extra={
                        "company": company_name,
                        "ats": backend.name,
                        "slug": slug,
                        "count": len(jobs),
                    },
                )
                return CompanyJobsResult(
                    company=company_name,
                    company_normalized=normalized,
                    ats=_as_ats_literal(backend.name),
                    ats_company_slug=slug,
                    jobs=jobs,
                )
        return CompanyJobsResult(
            company=company_name, company_normalized=normalized
        )

    async def scrape_companies(self, company_names: Sequence[str]) -> list[CompanyJobsResult]:
        client = self._client or httpx.AsyncClient(
            timeout=self.timeout_seconds,
            headers={"User-Agent": "signal-tracker/0.1 (+jobs scraper)"},
            follow_redirects=True,
        )
        try:
            results: list[CompanyJobsResult] = []
            for index, name in enumerate(company_names):
                if index > 0:
                    await asyncio.sleep(self.rate_limit_seconds)
                result = await self.scrape_company(client, name)
                results.append(result)
            return results
        finally:
            if self._owns_client:
                await client.aclose()


def _as_ats_literal(name: str) -> ATS:
    """Narrow the runtime backend name to the Literal type without lying."""
    if name in {"greenhouse", "lever", "workable", "ashby", "manual", "unknown"}:
        return name  # type: ignore[return-value]
    return "unknown"


def persist_result(
    db: Database,
    result: CompanyJobsResult,
    profile_for_scoring: UserProfile,
    report: ScrapingReport,
) -> None:
    """Insert/refresh JobOffer rows. Marks disappeared jobs as is_open=False."""
    if not result.found:
        return

    seen_dedup_keys: set[str] = set()
    for job in result.jobs:
        score, matched_roles = score_job(job, profile_for_scoring)
        dedup = f"{result.company_normalized}|{job.ats}|{job.external_id}"
        seen_dedup_keys.add(dedup)
        with db.session() as session:
            existing = session.execute(
                select(JobOffer).where(JobOffer.dedup_key == dedup)
            ).scalar_one_or_none()
            if existing is None:
                session.add(
                    JobOffer(
                        company_normalized=result.company_normalized,
                        company_name=result.company,
                        ats=job.ats,
                        ats_company_slug=job.ats_company_slug,
                        external_id=job.external_id,
                        title=job.title,
                        url=job.url,
                        location=job.location,
                        department=job.department,
                        description=job.description,
                        posted_at=job.posted_at,
                        relevance_score=score,
                        matched_roles=matched_roles,
                        is_open=True,
                        dedup_key=dedup,
                    )
                )
                report.jobs_new += 1
            else:
                existing.title = job.title
                existing.url = job.url
                existing.location = job.location
                existing.department = job.department
                existing.description = job.description
                existing.posted_at = job.posted_at
                existing.relevance_score = score
                existing.matched_roles = matched_roles
                existing.is_open = True
                report.jobs_updated += 1
        report.jobs_collected += 1

    # Mark previously-open jobs that disappeared as closed.
    with db.session() as session:
        previously_open = list(
            session.execute(
                select(JobOffer).where(
                    JobOffer.company_normalized == result.company_normalized,
                    JobOffer.is_open.is_(True),
                )
            ).scalars()
        )
        for row in previously_open:
            if row.dedup_key not in seen_dedup_keys:
                row.is_open = False


__all__ = [
    "JobsOverride",
    "JobsScraper",
    "ScrapingReport",
    "parse_overrides",
    "persist_result",
]
