"""Greenhouse job board API adapter.

Endpoint: https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true
Public, no auth required.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import httpx

from signal_tracker.jobs.backends.base import JobsBackend
from signal_tracker.jobs.schemas import JobPosting


class GreenhouseBackend(JobsBackend):
    name = "greenhouse"
    BASE_URL = "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"

    async def list_jobs(
        self, slug: str, client: httpx.AsyncClient
    ) -> list[JobPosting] | None:
        url = self.BASE_URL.format(slug=slug)
        try:
            response = await client.get(url, params={"content": "true"})
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
            posted = _parse_iso(job.get("updated_at") or job.get("created_at"))
            location = (job.get("location") or {}).get("name")
            departments = job.get("departments") or []
            department = departments[0].get("name") if departments else None
            out.append(
                JobPosting(
                    ats="greenhouse",
                    ats_company_slug=slug,
                    external_id=str(job["id"]),
                    title=str(job.get("title", "")).strip() or "(no title)",
                    url=str(job.get("absolute_url") or ""),
                    location=location,
                    department=department,
                    description=_truncate(job.get("content")),
                    posted_at=posted,
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


__all__ = ["GreenhouseBackend"]
