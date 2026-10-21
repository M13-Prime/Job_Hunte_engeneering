"""SmartRecruiters public postings API (widely used by large French groups).

Endpoint: https://api.smartrecruiters.com/v1/companies/{slug}/postings
Public, no auth. An unknown company id returns 200 with no postings, so an
empty result is treated as "not this ATS" to let the next backend try.
"""

from __future__ import annotations

from typing import Any

import httpx

from signal_tracker.jobs.backends._common import as_str, join_location, parse_iso
from signal_tracker.jobs.backends.base import JobsBackend
from signal_tracker.jobs.schemas import JobPosting


class SmartRecruitersBackend(JobsBackend):
    name = "smartrecruiters"
    BASE_URL = "https://api.smartrecruiters.com/v1/companies/{slug}/postings"
    PAGE = 100
    MAX_PAGES = 5

    async def list_jobs(
        self, slug: str, client: httpx.AsyncClient
    ) -> list[JobPosting] | None:
        out: list[JobPosting] = []
        for page in range(self.MAX_PAGES):
            try:
                response = await client.get(
                    self.BASE_URL.format(slug=slug),
                    params={"limit": self.PAGE, "offset": page * self.PAGE},
                )
            except httpx.HTTPError:
                return out if out else None
            if response.status_code >= 400:
                return out if out else None
            try:
                payload = response.json()
            except ValueError:
                return out if out else None
            content = payload.get("content") if isinstance(payload, dict) else None
            if not isinstance(content, list):
                return out if out else None
            for post in content:
                out.append(_posting(slug, post))
            total = payload.get("totalFound")
            if len(content) < self.PAGE or (isinstance(total, int) and len(out) >= total):
                break
        return out or None


def _posting(slug: str, post: dict[str, Any]) -> JobPosting:
    loc = post.get("location") or {}
    dept = post.get("department") or {}
    pid = as_str(post.get("id")) or ""
    return JobPosting(
        ats="smartrecruiters",
        ats_company_slug=slug,
        external_id=pid,
        title=as_str(post.get("name")) or "(no title)",
        url=f"https://jobs.smartrecruiters.com/{slug}/{pid}",
        location=join_location(loc.get("city"), loc.get("region"), loc.get("country"))
        if isinstance(loc, dict) else None,
        department=as_str(dept.get("label")) if isinstance(dept, dict) else None,
        description=None,  # the list endpoint carries no description
        posted_at=parse_iso(post.get("releasedDate")),
    )


__all__ = ["SmartRecruitersBackend"]
