"""Workable job board API adapter.

Endpoint: https://apply.workable.com/api/v3/accounts/{slug}/jobs
Public, no auth required.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import httpx

from signal_tracker.jobs.backends.base import JobsBackend
from signal_tracker.jobs.schemas import JobPosting


class WorkableBackend(JobsBackend):
    name = "workable"
    BASE_URL = "https://apply.workable.com/api/v3/accounts/{slug}/jobs"

    async def list_jobs(
        self, slug: str, client: httpx.AsyncClient
    ) -> list[JobPosting] | None:
        url = self.BASE_URL.format(slug=slug)
        try:
            response = await client.post(url, json={"query": "", "limit": 100})
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
        results = payload.get("results") or []
        out: list[JobPosting] = []
        for job in results:
            loc_obj = job.get("location")
            nested_city = loc_obj.get("city") if isinstance(loc_obj, dict) else None
            location = _str(job.get("city")) or _str(job.get("country")) or _str(nested_city)
            out.append(
                JobPosting(
                    ats="workable",
                    ats_company_slug=slug,
                    external_id=str(job.get("shortcode") or job.get("id") or ""),
                    title=str(job.get("title", "")).strip() or "(no title)",
                    url=str(job.get("url") or job.get("apply_url") or ""),
                    location=location,
                    department=_str(job.get("department")),
                    description=_truncate(job.get("description")),
                    posted_at=_parse_iso(job.get("published_on") or job.get("created_at")),
                )
            )
        return out


def _str(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


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


__all__ = ["WorkableBackend"]
