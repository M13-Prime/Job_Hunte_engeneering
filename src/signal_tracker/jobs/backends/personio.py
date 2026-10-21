"""Personio public XML job feed (very common with European SMEs).

Endpoint: https://{slug}.jobs.personio.de/xml (falls back to .com).
"""

from __future__ import annotations

import xml.etree.ElementTree as ET

import httpx

from signal_tracker.jobs.backends._common import as_str, join_location, parse_iso, plain_text
from signal_tracker.jobs.backends.base import JobsBackend
from signal_tracker.jobs.schemas import JobPosting


class PersonioBackend(JobsBackend):
    name = "personio"
    HOSTS = ("https://{slug}.jobs.personio.de", "https://{slug}.jobs.personio.com")

    async def list_jobs(
        self, slug: str, client: httpx.AsyncClient
    ) -> list[JobPosting] | None:
        for host in self.HOSTS:
            base = host.format(slug=slug)
            try:
                response = await client.get(f"{base}/xml", params={"language": "fr"})
            except httpx.HTTPError:
                continue
            if response.status_code >= 400 or "xml" not in response.headers.get(
                "content-type", "xml"
            ):
                continue
            parsed = parse_personio_xml(response.text, slug, base)
            if parsed is not None:
                return parsed
        return None


def parse_personio_xml(text: str, slug: str, base: str) -> list[JobPosting] | None:
    if "<!DOCTYPE" in text[:500] or "<!ENTITY" in text:
        return None  # never expand entities from a remote document
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return None
    if root.tag != "workzag-jobs":
        return None
    out: list[JobPosting] = []
    for pos in root.findall("position"):
        pid = (pos.findtext("id") or "").strip()
        descs = " ".join(
            (d.findtext("value") or "") for d in pos.findall("jobDescriptions/jobDescription")
        )
        out.append(JobPosting(
            ats="personio",
            ats_company_slug=slug,
            external_id=pid,
            title=as_str(pos.findtext("name")) or "(no title)",
            url=f"{base}/job/{pid}",
            location=join_location(
                pos.findtext("office"), pos.findtext("additionalOffices/office"),
            ),
            department=as_str(pos.findtext("department")),
            description=plain_text(descs),
            posted_at=parse_iso(pos.findtext("createdAt")),
        ))
    return out


__all__ = ["PersonioBackend", "parse_personio_xml"]
