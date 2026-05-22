"""Tests for the orchestrator that tries slug x backend combinations."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx

from signal_tracker.config import UserProfile
from signal_tracker.jobs.backends import (
    AshbyBackend,
    GreenhouseBackend,
    LeverBackend,
    WorkableBackend,
)
from signal_tracker.jobs.scraper import (
    JobsOverride,
    JobsScraper,
    ScrapingReport,
    parse_overrides,
    persist_result,
)
from signal_tracker.storage import init_db
from signal_tracker.storage.models import JobOffer

GREENHOUSE_BODY = {
    "jobs": [
        {
            "id": 1,
            "title": "Senior ML Engineer",
            "absolute_url": "https://boards.greenhouse.io/x/1",
            "location": {"name": "Paris"},
            "departments": [{"name": "Engineering"}],
        }
    ]
}


def _make_router(routes: dict[str, dict[str, Any]]) -> httpx.AsyncClient:
    """Build a client that maps URL substrings to (status, body) responses."""

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        for key, response in routes.items():
            if key in url:
                return httpx.Response(response["status"], json=response.get("body", {}))
        return httpx.Response(404, json={})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_scraper_picks_first_backend_that_matches() -> None:
    client = _make_router({
        "boards-api.greenhouse.io/v1/boards/sweep/jobs": {
            "status": 200,
            "body": GREENHOUSE_BODY,
        },
    })
    try:
        scraper = JobsScraper(
            backends=[GreenhouseBackend(), LeverBackend(), WorkableBackend(), AshbyBackend()],
            rate_limit_seconds=0.0,
            client=client,
        )
        results = await scraper.scrape_companies(["Sweep"])
    finally:
        await client.aclose()
    assert len(results) == 1
    assert results[0].found
    assert results[0].ats == "greenhouse"
    assert results[0].ats_company_slug == "sweep"
    assert len(results[0].jobs) == 1


async def test_scraper_returns_not_found_when_no_backend_matches() -> None:
    client = _make_router({})  # everything 404
    try:
        scraper = JobsScraper(
            backends=[GreenhouseBackend(), LeverBackend()],
            rate_limit_seconds=0.0,
            client=client,
        )
        results = await scraper.scrape_companies(["NotARealCompany"])
    finally:
        await client.aclose()
    assert results[0].found is False
    assert results[0].ats is None


async def test_scraper_uses_override() -> None:
    client = _make_router({
        "boards-api.greenhouse.io/v1/boards/custom-slug/jobs": {
            "status": 200,
            "body": GREENHOUSE_BODY,
        },
    })
    try:
        scraper = JobsScraper(
            backends=[GreenhouseBackend()],
            overrides=[
                JobsOverride(
                    company_normalized="surprising name",
                    ats="greenhouse",
                    slug="custom-slug",
                )
            ],
            rate_limit_seconds=0.0,
            client=client,
        )
        results = await scraper.scrape_companies(["Surprising Name"])
    finally:
        await client.aclose()
    assert results[0].found
    assert results[0].ats_company_slug == "custom-slug"


def test_parse_overrides_handles_both_shapes() -> None:
    raw = [
        {"company": "Sweep", "ats": "greenhouse", "slug": "sweep"},
        {"normalized": "carbone 4", "ats": "greenhouse", "slug": "carbone4"},
        {"company": "", "ats": "x", "slug": "y"},  # invalid -> dropped
    ]
    out = parse_overrides(raw)
    assert len(out) == 2
    assert out[0].company_normalized == "sweep"
    assert out[1].company_normalized == "carbone 4"


async def test_persist_result_inserts_and_closes(tmp_path: Path) -> None:
    db = init_db(tmp_path / "jobs.db")

    # First scrape: 2 jobs
    client = _make_router({
        "boards-api.greenhouse.io/v1/boards/sweep/jobs": {
            "status": 200,
            "body": {
                "jobs": [
                    {"id": 1, "title": "ML Engineer", "absolute_url": "u1"},
                    {"id": 2, "title": "Data Scientist", "absolute_url": "u2"},
                ]
            },
        },
    })
    try:
        scraper = JobsScraper(
            backends=[GreenhouseBackend()],
            rate_limit_seconds=0.0,
            client=client,
        )
        results = await scraper.scrape_companies(["Sweep"])
    finally:
        await client.aclose()
    report = ScrapingReport(companies_attempted=1)
    profile = UserProfile(domains=["AI"], target_roles=["ML Engineer"])
    persist_result(db, results[0], profile, report)
    assert report.jobs_new == 2

    with db.session() as s:
        assert s.query(JobOffer).filter_by(is_open=True).count() == 2

    # Second scrape: job #1 disappeared, #2 still there
    client = _make_router({
        "boards-api.greenhouse.io/v1/boards/sweep/jobs": {
            "status": 200,
            "body": {
                "jobs": [
                    {"id": 2, "title": "Data Scientist", "absolute_url": "u2"},
                ]
            },
        },
    })
    try:
        scraper = JobsScraper(
            backends=[GreenhouseBackend()],
            rate_limit_seconds=0.0,
            client=client,
        )
        results = await scraper.scrape_companies(["Sweep"])
    finally:
        await client.aclose()
    report2 = ScrapingReport(companies_attempted=1)
    persist_result(db, results[0], profile, report2)
    assert report2.jobs_updated == 1
    assert report2.jobs_new == 0

    with db.session() as s:
        open_count = s.query(JobOffer).filter_by(is_open=True).count()
        closed_count = s.query(JobOffer).filter_by(is_open=False).count()
    assert open_count == 1
    assert closed_count == 1
