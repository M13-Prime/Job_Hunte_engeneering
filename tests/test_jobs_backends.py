"""Tests for ATS backend adapters using httpx MockTransport."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from signal_tracker.jobs.backends import (
    AshbyBackend,
    GreenhouseBackend,
    LeverBackend,
    WorkableBackend,
)

# ---------------------------------------------------------------------------
# Greenhouse
# ---------------------------------------------------------------------------

GREENHOUSE_OK: dict[str, Any] = {
    "jobs": [
        {
            "id": 101,
            "title": "Senior ML Engineer",
            "absolute_url": "https://boards.greenhouse.io/sweep/101",
            "location": {"name": "Paris, France"},
            "departments": [{"name": "Engineering"}],
            "updated_at": "2025-05-12T09:00:00Z",
            "content": "<p>Hello</p>",
        }
    ]
}


async def test_greenhouse_parses_jobs() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert "boards-api.greenhouse.io" in str(request.url)
        return httpx.Response(200, json=GREENHOUSE_OK)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        jobs = await GreenhouseBackend().list_jobs("sweep", client)
    finally:
        await client.aclose()
    assert jobs is not None
    assert len(jobs) == 1
    assert jobs[0].title == "Senior ML Engineer"
    assert jobs[0].location == "Paris, France"
    assert jobs[0].department == "Engineering"
    assert jobs[0].posted_at is not None


async def test_greenhouse_404_returns_none() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": "not found"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        jobs = await GreenhouseBackend().list_jobs("nope", client)
    finally:
        await client.aclose()
    assert jobs is None


# ---------------------------------------------------------------------------
# Lever
# ---------------------------------------------------------------------------

LEVER_OK = [
    {
        "id": "abc-123",
        "text": "Climate Data Scientist",
        "hostedUrl": "https://jobs.lever.co/sweep/abc-123",
        "createdAt": 1715500000000,
        "categories": {
            "department": "Data",
            "allLocations": ["Paris", "Remote"],
        },
        "descriptionPlain": "Some description here.",
    }
]


async def test_lever_parses_jobs() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=LEVER_OK)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        jobs = await LeverBackend().list_jobs("sweep", client)
    finally:
        await client.aclose()
    assert jobs is not None
    assert jobs[0].title == "Climate Data Scientist"
    assert jobs[0].department == "Data"
    assert jobs[0].location == "Paris"


async def test_lever_404_returns_none() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(404, content=b"")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        jobs = await LeverBackend().list_jobs("nope", client)
    finally:
        await client.aclose()
    assert jobs is None


# ---------------------------------------------------------------------------
# Workable
# ---------------------------------------------------------------------------

WORKABLE_OK: dict[str, Any] = {
    "results": [
        {
            "shortcode": "JOB123",
            "title": "Sustainability Analyst",
            "url": "https://apply.workable.com/sweep/j/JOB123",
            "city": "Lyon",
            "department": "Sustainability",
            "description": "Help our ESG team",
            "published_on": "2025-05-12",
        }
    ]
}


async def test_workable_parses_jobs() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json=WORKABLE_OK)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        jobs = await WorkableBackend().list_jobs("sweep", client)
    finally:
        await client.aclose()
    assert captured["method"] == "POST"
    assert captured["body"] == {"query": "", "limit": 100}
    assert jobs is not None
    assert jobs[0].title == "Sustainability Analyst"
    assert jobs[0].location == "Lyon"


# ---------------------------------------------------------------------------
# Ashby
# ---------------------------------------------------------------------------

ASHBY_OK: dict[str, Any] = {
    "jobs": [
        {
            "id": "ashby-xyz",
            "title": "AI Engineer",
            "jobUrl": "https://jobs.ashbyhq.com/sweep/ashby-xyz",
            "location": "Remote",
            "department": "AI",
            "descriptionPlain": "Build agents",
            "publishedAt": "2025-05-12T10:00:00Z",
        }
    ]
}


async def test_ashby_parses_jobs() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=ASHBY_OK)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        jobs = await AshbyBackend().list_jobs("sweep", client)
    finally:
        await client.aclose()
    assert jobs is not None
    assert jobs[0].title == "AI Engineer"
    assert jobs[0].department == "AI"


@pytest.mark.live
async def test_live_greenhouse_smoke() -> None:
    """Hit a real, well-known Greenhouse board to confirm the parser still
    works against the live API. Skipped in CI."""
    async with httpx.AsyncClient(timeout=20.0) as client:
        # Greenhouse uses Stripe; ridiculously stable slug.
        jobs = await GreenhouseBackend().list_jobs("stripe", client)
    assert jobs is not None
    assert len(jobs) > 0
