"""Teamtailor career sites expose a public RSS feed of open jobs.

Endpoint: https://{slug}.teamtailor.com/jobs.rss
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import feedparser
import httpx

from signal_tracker.jobs.backends._common import as_str, plain_text
from signal_tracker.jobs.backends.base import JobsBackend
from signal_tracker.jobs.schemas import JobPosting


class TeamtailorBackend(JobsBackend):
    name = "teamtailor"
    BASE_URL = "https://{slug}.teamtailor.com/jobs.rss"

    async def list_jobs(
        self, slug: str, client: httpx.AsyncClient
    ) -> list[JobPosting] | None:
        try:
            response = await client.get(self.BASE_URL.format(slug=slug))
        except httpx.HTTPError:
            return None
        if response.status_code >= 400 or "<rss" not in response.text[:2000]:
            return None
        return parse_teamtailor_rss(response.text, slug)


def parse_teamtailor_rss(text: str, slug: str) -> list[JobPosting]:
    feed: Any = feedparser.parse(text)
    out: list[JobPosting] = []
    for entry in feed.entries:
        link = as_str(entry.get("link")) or ""
        locations = entry.get("tt_locations") or entry.get("location")
        loc = None
        if isinstance(locations, str):
            loc = locations
        elif isinstance(entry.get("tags"), list) and entry["tags"]:
            loc = as_str(entry["tags"][0].get("term"))
        published = entry.get("published_parsed")
        posted = datetime(*published[:6]).replace(tzinfo=UTC) if published else None
        out.append(JobPosting(
            ats="teamtailor",
            ats_company_slug=slug,
            external_id=as_str(entry.get("id")) or link,
            title=as_str(entry.get("title")) or "(no title)",
            url=link,
            location=loc,
            department=as_str(entry.get("tt_department")),
            description=plain_text(entry.get("summary") or entry.get("description")),
            posted_at=posted,
        ))
    return out


__all__ = ["TeamtailorBackend", "parse_teamtailor_rss"]
