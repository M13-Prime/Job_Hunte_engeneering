"""More ATS backends, the A-F rubric, the CV review pass and JobSpy mapping."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from signal_tracker.applications.schemas import TailoredCV, TailoredDocuments
from signal_tracker.jobs.backends import (
    DEFAULT_BACKENDS,
    PersonioBackend,
    RecruiteeBackend,
    SmartRecruitersBackend,
    TeamtailorBackend,
    WorkdayBackend,
)
from signal_tracker.jobs.jobspy_source import rows_to_results
from signal_tracker.jobs.rubric import (
    clean_criteria,
    grade_for,
    legitimacy_flags,
    weighted_score,
)


def _client(handler: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


# ---------------------------------------------------------------------------
# Backends
# ---------------------------------------------------------------------------


async def test_smartrecruiters_parses_and_treats_empty_as_unknown() -> None:
    payload = {"totalFound": 1, "content": [{
        "id": "744", "name": "ESG Analyst", "releasedDate": "2026-10-01T10:00:00.000Z",
        "location": {"city": "Paris", "region": "IDF", "country": "fr"},
        "department": {"label": "Finance"},
    }]}

    def ok(request: httpx.Request) -> httpx.Response:
        assert "api.smartrecruiters.com/v1/companies/acme/postings" in str(request.url)
        return httpx.Response(200, json=payload)

    async with _client(ok) as c:
        jobs = await SmartRecruitersBackend().list_jobs("acme", c)
    assert jobs and jobs[0].title == "ESG Analyst"
    assert jobs[0].location == "Paris, IDF, fr"
    assert jobs[0].url == "https://jobs.smartrecruiters.com/acme/744"

    async with _client(lambda _: httpx.Response(200, json={"totalFound": 0, "content": []})) as c:
        assert await SmartRecruitersBackend().list_jobs("nobody", c) is None


async def test_recruitee_parses_offers() -> None:
    payload = {"offers": [{
        "id": 9, "slug": "esg-lead", "title": "ESG Lead", "status": "published",
        "location": "Lyon, France", "department": "Impact",
        "description": "<p>Pilot <b>CSRD</b></p>", "requirements": "<p>5 ans d'expérience</p>",
        "careers_url": "https://acme.recruitee.com/o/esg-lead",
        "published_at": "2026-10-02 08:00:00 UTC",
    }]}
    async with _client(lambda _: httpx.Response(200, json=payload)) as c:
        jobs = await RecruiteeBackend().list_jobs("acme", c)
    assert jobs and jobs[0].location == "Lyon, France"
    assert jobs[0].description and "CSRD" in jobs[0].description and "<p>" not in jobs[0].description
    async with _client(lambda _: httpx.Response(404)) as c:
        assert await RecruiteeBackend().list_jobs("nobody", c) is None


PERSONIO_XML = """<?xml version="1.0" encoding="UTF-8"?>
<workzag-jobs>
  <position>
    <id>123</id><office>Paris</office><department>Data</department>
    <name>Data Engineer ESG</name><createdAt>2026-09-30T09:00:00+00:00</createdAt>
    <jobDescriptions><jobDescription><name>Missions</name><value>&lt;p&gt;Build pipelines&lt;/p&gt;</value></jobDescription></jobDescriptions>
  </position>
</workzag-jobs>"""


async def test_personio_parses_xml_and_refuses_entities() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=PERSONIO_XML, headers={"content-type": "application/xml"})

    async with _client(handler) as c:
        jobs = await PersonioBackend().list_jobs("acme", c)
    assert jobs and jobs[0].title == "Data Engineer ESG"
    assert jobs[0].url == "https://acme.jobs.personio.de/job/123"
    assert jobs[0].description == "Build pipelines"

    evil = '<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><workzag-jobs/>'
    async with _client(lambda _: httpx.Response(200, text=evil, headers={"content-type": "text/xml"})) as c:
        assert await PersonioBackend().list_jobs("acme", c) is None


TEAMTAILOR_RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>Jobs</title>
<item><title>Sustainability Manager</title><link>https://acme.teamtailor.com/jobs/1-sm</link>
<guid>1</guid><description>&lt;p&gt;Lead our ESG roadmap&lt;/p&gt;</description>
<pubDate>Wed, 01 Oct 2026 08:00:00 +0000</pubDate></item>
</channel></rss>"""


async def test_teamtailor_parses_rss() -> None:
    async with _client(lambda _: httpx.Response(200, text=TEAMTAILOR_RSS)) as c:
        jobs = await TeamtailorBackend().list_jobs("acme", c)
    assert jobs and jobs[0].title == "Sustainability Manager"
    assert jobs[0].url.endswith("/jobs/1-sm")
    assert jobs[0].posted_at is not None
    async with _client(lambda _: httpx.Response(200, text="<html>not a feed</html>")) as c:
        assert await TeamtailorBackend().list_jobs("acme", c) is None


async def test_workday_uses_override_slug_only() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://acme.wd3.myworkdayjobs.com/wday/cxs/acme/Careers/jobs"
        return httpx.Response(200, json={"total": 1, "jobPostings": [{
            "title": "Head of ESG", "externalPath": "/job/Paris/Head-of-ESG_R1",
            "locationsText": "Paris, France", "postedOn": "Posted 3 Days Ago",
        }]})

    async with _client(handler) as c:
        jobs = await WorkdayBackend().list_jobs("acme|wd3|Careers", c)
    assert jobs and jobs[0].url == "https://acme.wd3.myworkdayjobs.com/Careers/job/Paris/Head-of-ESG_R1"
    assert jobs[0].posted_at is not None
    async with _client(handler) as c:
        assert await WorkdayBackend().list_jobs("acme", c) is None   # not a Workday slug
    assert not WorkdayBackend.guessable
    assert {b.name for b in DEFAULT_BACKENDS} >= {
        "smartrecruiters", "recruitee", "teamtailor", "personio", "workday",
    }


# ---------------------------------------------------------------------------
# Rubric
# ---------------------------------------------------------------------------


def test_rubric_weighted_score_and_grades() -> None:
    crit = clean_criteria({"role": 5, "skills": 4, "seniority": 4, "location": 5, "timing": 3})
    assert crit is not None
    score = weighted_score(crit)
    assert score == pytest.approx(86.0)
    assert grade_for(score) == "A"
    assert grade_for(72) == "B" and grade_for(56) == "C" and grade_for(41) == "D" and grade_for(10) == "F"
    assert clean_criteria({"role": 9, "skills": "x"}) is None          # too few valid keys
    assert clean_criteria({"role": 9, "skills": 3, "seniority": -2}) == {
        "role": 5, "skills": 3, "seniority": 0,
    }


def test_legitimacy_flags() -> None:
    now = datetime(2026, 10, 21, tzinfo=UTC)
    flags = legitimacy_flags(
        posted_at=now - timedelta(days=90), description="Short", location="",
        ai_flags=["Annonce très générique"], now=now,
    )
    assert any("90 jours" in f for f in flags)
    assert "Description très courte ou absente" in flags
    assert "Lieu non précisé" in flags
    assert "Annonce très générique" in flags
    assert legitimacy_flags(
        posted_at=now, description="x" * 400, location="Paris", now=now,
    ) == []


# ---------------------------------------------------------------------------
# CV review pass
# ---------------------------------------------------------------------------


def _docs() -> TailoredDocuments:
    return TailoredDocuments(
        language="fr",
        cv=TailoredCV(headline="Data Engineer", summary="Expert Kubernetes", skills=["SQL", "Kubernetes"]),
        cover_letter="Madame, Monsieur, j'ai 10 ans de Kubernetes.",
        changes=["Résumé recentré"],
    )


async def test_review_removes_inventions(monkeypatch: pytest.MonkeyPatch) -> None:
    import litellm

    from signal_tracker.applications.tailor import review_documents

    reviewed = {
        "issues": ["Retiré « Kubernetes » : absent du CV."],
        "cv": {"headline": "Data Engineer", "summary": "Data engineer SQL", "skills": ["SQL"]},
        "cover_letter": "Madame, Monsieur, je travaille en SQL.",
    }

    async def fake(**_: Any) -> SimpleNamespace:
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content=json.dumps(reviewed)))])

    monkeypatch.setattr(litellm, "acompletion", fake)
    out = await review_documents(_docs(), cv_text="Data engineer. SQL.")
    assert out.cv.skills == ["SQL"]
    assert "Kubernetes" not in out.cover_letter
    assert out.changes[0] == "Résumé recentré"
    assert any(c.startswith("Relecture : Retiré") for c in out.changes)


async def test_review_failure_keeps_draft(monkeypatch: pytest.MonkeyPatch) -> None:
    import litellm

    from signal_tracker.applications.tailor import review_documents

    async def boom(**_: Any) -> SimpleNamespace:
        raise RuntimeError("provider down")

    monkeypatch.setattr(litellm, "acompletion", boom)
    out = await review_documents(_docs(), cv_text="Data engineer. SQL.")
    assert out.cv.skills == ["SQL", "Kubernetes"]
    assert any("indisponible" in c for c in out.changes)


# ---------------------------------------------------------------------------
# JobSpy mapping (the library itself is optional and not installed in CI)
# ---------------------------------------------------------------------------


def test_jobspy_rows_grouped_by_company() -> None:
    from signal_tracker.jobs.jobspy_source import pop_company_urls

    rows: list[dict[str, Any]] = [
        {"id": "in-1", "site": "indeed", "job_url": "https://fr.indeed.com/1", "title": "ESG Analyst",
         "company": "Acme", "location": "Paris, FR", "description": "**ESG**",
         "company_url_direct": "https://acme.example"},
        {"id": "in-2", "site": "indeed", "job_url": "https://fr.indeed.com/2", "title": "CSR Lead",
         "company": "Acme", "location": "Lyon, FR", "description": None},
        {"id": "g-1", "site": "google", "job_url": "", "title": "No URL", "company": "Skip"},
    ]
    results = rows_to_results(rows)
    assert len(results) == 1 and len(results[0].jobs) == 2
    assert results[0].ats == "jobspy"
    assert pop_company_urls() == {"acme": "https://acme.example"}


async def test_jobspy_search_is_noop_when_library_missing() -> None:
    from signal_tracker.jobs.jobspy_source import jobspy_available, search_jobspy

    if jobspy_available():
        pytest.skip("jobspy installed")
    assert await search_jobspy(
        ["ESG"], country="France", sites=["indeed"], results_per_search=5, hours_old=24,
    ) == []
