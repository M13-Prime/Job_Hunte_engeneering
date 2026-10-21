"""Optional JobSpy collector: Indeed, LinkedIn, Glassdoor, Google Jobs.

Off by default (``JOBSPY_ENABLED=false``) and not installed by default:
build the image with ``INSTALL_JOBSPY=1``. JobSpy uses unofficial
endpoints of these sites, so it can break without notice, LinkedIn
rate-limits after a few pages, and the sites' terms may forbid scraping:
keep volumes small (a few searches a day).

Unlike the ATS backends, this searches by keyword and country, so it
finds companies that are not in the user's watchlist or signals.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from datetime import UTC, date, datetime
from typing import Any

from signal_tracker.jobs.backends._common import as_str, plain_text
from signal_tracker.jobs.schemas import CompanyJobsResult, JobPosting
from signal_tracker.utils.logging import get_logger
from signal_tracker.utils.normalize import normalize_company_name

logger = get_logger(__name__)

# Our French country names -> JobSpy's `country_indeed` names.
COUNTRY_FOR_JOBSPY: dict[str, str] = {
    "France": "France", "Belgique": "Belgium", "Suisse": "Switzerland",
    "Luxembourg": "Luxembourg", "Canada": "Canada", "Royaume-Uni": "UK",
    "États-Unis": "USA", "Allemagne": "Germany", "Espagne": "Spain",
    "Italie": "Italy", "Pays-Bas": "Netherlands", "Irlande": "Ireland",
}


def jobspy_available() -> bool:
    try:
        import jobspy  # noqa: F401
    except ImportError:
        return False
    return True


def _posted(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=UTC)
    return None


def rows_to_results(rows: list[dict[str, Any]]) -> list[CompanyJobsResult]:
    """Group JobSpy rows (one per posting) into one result per company."""
    by_company: dict[str, list[JobPosting]] = defaultdict(list)
    names: dict[str, str] = {}
    urls: dict[str, str] = {}
    for row in rows:
        company = as_str(row.get("company"))
        url = as_str(row.get("job_url"))
        if not company or not url:
            continue
        key = normalize_company_name(company)
        names.setdefault(key, company)
        site_url = as_str(row.get("company_url_direct")) or as_str(row.get("company_url"))
        if site_url and site_url.startswith("http") and "linkedin.com" not in site_url:
            urls.setdefault(key, site_url)
        by_company[key].append(JobPosting(
            ats="jobspy",
            ats_company_slug=as_str(row.get("site")) or "jobspy",
            external_id=as_str(row.get("id")) or url,
            title=as_str(row.get("title")) or "(no title)",
            url=url,
            location=as_str(row.get("location")),
            department=None,
            description=plain_text(row.get("description"), limit=2500),
            posted_at=_posted(row.get("date_posted")),
        ))
    out: list[CompanyJobsResult] = []
    for key, jobs in by_company.items():
        result = CompanyJobsResult(
            company=names[key], company_normalized=key,
            ats="jobspy", ats_company_slug="jobspy", jobs=jobs,
        )
        out.append(result)
    _COMPANY_URLS.update(urls)
    return out


# Company websites JobSpy gave us, consumed by the caller to fill Company.url.
_COMPANY_URLS: dict[str, str] = {}


def pop_company_urls() -> dict[str, str]:
    urls = dict(_COMPANY_URLS)
    _COMPANY_URLS.clear()
    return urls


async def search_jobspy(
    terms: list[str], *, country: str, sites: list[str],
    results_per_search: int, hours_old: int,
) -> list[CompanyJobsResult]:
    """One JobSpy search per term (max 3), in a worker thread."""
    if not terms or not jobspy_available():
        return []
    from jobspy import scrape_jobs

    country_name = COUNTRY_FOR_JOBSPY.get(country, "France")
    location = country if country in COUNTRY_FOR_JOBSPY else "France"
    rows: list[dict[str, Any]] = []
    for term in terms[:3]:
        try:
            frame = await asyncio.to_thread(
                scrape_jobs,
                site_name=sites,
                search_term=term,
                location=location,
                results_wanted=results_per_search,
                hours_old=hours_old,
                country_indeed=country_name,
                description_format="markdown",
            )
        except Exception as exc:  # unofficial endpoints: never break the refresh
            logger.warning("jobspy.search_failed term=%s error=%s", term, str(exc)[:200])
            continue
        rows.extend(frame.to_dict("records"))
    logger.info("jobspy.fetched terms=%d rows=%d", len(terms[:3]), len(rows))
    return rows_to_results(rows)


__all__ = [
    "COUNTRY_FOR_JOBSPY", "jobspy_available", "pop_company_urls",
    "rows_to_results", "search_jobspy",
]
