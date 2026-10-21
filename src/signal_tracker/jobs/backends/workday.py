"""Workday career sites (large groups: TotalEnergies, Capgemini, Schneider...).

The slug cannot be guessed from the company name, so this backend is only
used through config/jobs.yaml overrides, with slug = "tenant|wdN|site", read
from the careers URL https://{tenant}.{wdN}.myworkdayjobs.com/{site}:

    - company: TotalEnergies
      ats: workday
      slug: totalenergies|wd3|TotalEnergies_careers
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx

from signal_tracker.jobs.backends._common import as_str
from signal_tracker.jobs.backends.base import JobsBackend
from signal_tracker.jobs.schemas import JobPosting


class WorkdayBackend(JobsBackend):
    name = "workday"
    guessable = False
    PAGE = 20
    MAX_PAGES = 10

    async def list_jobs(
        self, slug: str, client: httpx.AsyncClient
    ) -> list[JobPosting] | None:
        parts = slug.split("|")
        if len(parts) != 3 or not all(parts):
            return None
        tenant, wd, site = parts
        host = f"https://{tenant}.{wd}.myworkdayjobs.com"
        api = f"{host}/wday/cxs/{tenant}/{site}/jobs"
        out: list[JobPosting] = []
        for page in range(self.MAX_PAGES):
            try:
                response = await client.post(api, json={
                    "appliedFacets": {}, "limit": self.PAGE,
                    "offset": page * self.PAGE, "searchText": "",
                })
            except httpx.HTTPError:
                break
            if response.status_code >= 400:
                return out or None
            try:
                payload = response.json()
            except ValueError:
                break
            posts = payload.get("jobPostings") if isinstance(payload, dict) else None
            if not isinstance(posts, list):
                break
            for post in posts:
                path = as_str(post.get("externalPath")) or ""
                out.append(JobPosting(
                    ats="workday",
                    ats_company_slug=slug,
                    external_id=path or as_str(post.get("title")) or "",
                    title=as_str(post.get("title")) or "(no title)",
                    url=f"{host}/{site}{path}",
                    location=as_str(post.get("locationsText")),
                    department=None,
                    description=None,
                    posted_at=_posted_on(post.get("postedOn")),
                ))
            if len(posts) < self.PAGE:
                break
        return out


def _posted_on(value: object) -> datetime | None:
    """Workday says "Posted Today", "Posted 3 Days Ago", "Posted 30+ Days Ago"."""
    text = str(value or "").lower()
    now = datetime.now(UTC)
    if "today" in text:
        return now
    if "yesterday" in text:
        return now - timedelta(days=1)
    digits = "".join(c for c in text if c.isdigit())
    return now - timedelta(days=int(digits)) if digits else None


__all__ = ["WorkdayBackend"]
