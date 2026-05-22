"""Ashby job board API adapter.

Endpoint: https://api.ashbyhq.com/posting-api/job-board/{slug}
Public, no auth required for published boards.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import httpx

from signal_tracker.jobs.backends.base import JobsBackend
from signal_tracker.jobs.schemas import JobPosting


class AshbyBackend(JobsBackend):
    name = "ashby"
    BASE_URL = "https://api.ashbyhq.com/posting-api/job-board/{slug}"

    async def list_jobs(
        self, slug: str, client: httpx.AsyncClient
    ) -> list[JobPosting] | None:
        url = self.BASE_URL.format(slug=slug)
        try:
            response = await client.get(url)
        except httpx.HTTPError:
            return None
        if response.status_code == 404:
            return None
        if response.status_code >= 400:
            return None
        try:
            payload = response.json()
        except ValueError:
            return None
        jobs_raw = payload.get("jobs") or []
        out: list[JobPosting] = []
        for job in jobs_raw:
            location = job.get("location") or job.get("locationName")
            out.append(
                JobPosting(
                    ats="ashby",
                    ats_company_slug=slug,
                    external_id=str(job.get("id") or job.get("jobId") or ""),
                    title=str(job.get("title", "")).strip() or "(no title)",
                    url=str(job.get("jobUrl") or job.get("applyUrl") or ""),
                    location=str(location) if location else None,
                    department=job.get("department") or job.get("team"),
                    description=_truncate(
                        job.get("descriptionPlain") or job.get("descriptionHtml")
                    ),
                    posted_at=_parse_iso(job.get("publishedAt")),
                )
            )
        return out


def _parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _truncate(value: Any, limit: int = 1000) -> str | None:
    if not value:
        return None
    text = str(value)
    if len(text) > limit:
        return text[:limit] + "..."
    return text


__all__ = ["AshbyBackend"]
