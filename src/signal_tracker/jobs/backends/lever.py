"""Lever job board API adapter.

Endpoint: https://api.lever.co/v0/postings/{slug}?mode=json
Public, no auth required for the postings list.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx

from signal_tracker.jobs.backends.base import JobsBackend
from signal_tracker.jobs.schemas import JobPosting


class LeverBackend(JobsBackend):
    name = "lever"
    BASE_URL = "https://api.lever.co/v0/postings/{slug}"

    async def list_jobs(
        self, slug: str, client: httpx.AsyncClient
    ) -> list[JobPosting] | None:
        url = self.BASE_URL.format(slug=slug)
        try:
            response = await client.get(url, params={"mode": "json"})
        except httpx.HTTPError:
            return None
        if response.status_code == 404:
            return None
        if response.status_code >= 400:
            return None
        try:
            postings = response.json()
        except ValueError:
            return None
        if not isinstance(postings, list):
            return None
        out: list[JobPosting] = []
        for post in postings:
            posted_ms = post.get("createdAt") or post.get("updatedAt")
            posted: datetime | None = None
            if isinstance(posted_ms, int | float):
                posted = datetime.fromtimestamp(float(posted_ms) / 1000.0, tz=UTC)
            categories = post.get("categories") or {}
            description = post.get("descriptionPlain") or post.get("description")
            out.append(
                JobPosting(
                    ats="lever",
                    ats_company_slug=slug,
                    external_id=str(post.get("id") or post.get("lever_id") or ""),
                    title=str(post.get("text", "")).strip() or "(no title)",
                    url=str(post.get("hostedUrl") or post.get("applyUrl") or ""),
                    location=_first(categories.get("location") or categories.get("allLocations")),
                    department=categories.get("department") or categories.get("team"),
                    description=_truncate(description),
                    posted_at=posted,
                )
            )
        return out


def _first(value: Any) -> str | None:
    if isinstance(value, list):
        return str(value[0]) if value else None
    if isinstance(value, str):
        return value
    return None


def _truncate(value: Any, limit: int = 1000) -> str | None:
    if not value:
        return None
    text = str(value)
    if len(text) > limit:
        return text[:limit] + "..."
    return text


__all__ = ["LeverBackend"]
