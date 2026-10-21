"""Recruitee public careers API (common with French and Dutch scale-ups).

Endpoint: https://{slug}.recruitee.com/api/offers/
"""

from __future__ import annotations

import httpx

from signal_tracker.jobs.backends._common import as_str, join_location, parse_iso, plain_text
from signal_tracker.jobs.backends.base import JobsBackend
from signal_tracker.jobs.schemas import JobPosting


class RecruiteeBackend(JobsBackend):
    name = "recruitee"
    BASE_URL = "https://{slug}.recruitee.com/api/offers/"

    async def list_jobs(
        self, slug: str, client: httpx.AsyncClient
    ) -> list[JobPosting] | None:
        try:
            response = await client.get(self.BASE_URL.format(slug=slug))
        except httpx.HTTPError:
            return None
        if response.status_code >= 400:
            return None
        try:
            payload = response.json()
        except ValueError:
            return None
        offers = payload.get("offers") if isinstance(payload, dict) else None
        if not isinstance(offers, list):
            return None
        out: list[JobPosting] = []
        for offer in offers:
            if offer.get("status") not in (None, "published"):
                continue
            out.append(JobPosting(
                ats="recruitee",
                ats_company_slug=slug,
                external_id=str(offer.get("id") or offer.get("slug") or ""),
                title=as_str(offer.get("title")) or "(no title)",
                url=as_str(offer.get("careers_url"))
                or f"https://{slug}.recruitee.com/o/{offer.get('slug', '')}",
                location=as_str(offer.get("location"))
                or join_location(offer.get("city"), offer.get("country")),
                department=as_str(offer.get("department")),
                description=plain_text(
                    f"{offer.get('description') or ''} {offer.get('requirements') or ''}"
                ),
                posted_at=parse_iso(offer.get("published_at") or offer.get("created_at")),
            ))
        return out


__all__ = ["RecruiteeBackend"]
