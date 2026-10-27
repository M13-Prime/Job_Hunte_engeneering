"""Manual job ingestion from a URL (Jina Reader) + title/company guessing."""

from __future__ import annotations

import pytest

from signal_tracker.config import UserProfile
from signal_tracker.jobs.manual import add_job_from_url, guess_title_and_company
from signal_tracker.storage import Database
from signal_tracker.storage.models import JobOffer
from signal_tracker.utils.jina import JinaError, read_url

PAGE = """Title: Senior Data Engineer - Acme Climate

Markdown Content:
Acme Climate recrute un Senior Data Engineer a Paris.
Stack: Databricks, PySpark, SQL. Sujet CSRD et empreinte carbone.
""" + ("Details du poste. " * 40)


def test_guess_from_title_line() -> None:
    assert guess_title_and_company(PAGE) == ("Senior Data Engineer", "Acme Climate")


def test_guess_from_h1_with_chez() -> None:
    assert guess_title_and_company("# Data Analyst chez EcoVadis\n\nblah") == (
        "Data Analyst", "EcoVadis",
    )


def test_guess_returns_none_without_heading() -> None:
    assert guess_title_and_company("just some text") == (None, None)


@pytest.mark.asyncio
async def test_add_job_from_url_persists_and_scores(
    tmp_db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_read(url: str, **kwargs: object) -> str:
        return PAGE

    monkeypatch.setattr("signal_tracker.jobs.manual.read_url", fake_read)
    profile = UserProfile(target_roles=["Data Engineer"], core_skills=["Databricks"])

    job = await add_job_from_url(tmp_db, "https://example.com/jobs/1", profile)
    assert job.created is True
    assert job.company == "Acme Climate"
    assert job.relevance_score >= 45
    assert job.dealbreaker is None

    with tmp_db.session() as session:
        rows = session.query(JobOffer).all()
        assert len(rows) == 1
        assert rows[0].ats == "manual"
        assert rows[0].url == "https://example.com/jobs/1"

    # Same URL again updates in place instead of duplicating.
    again = await add_job_from_url(tmp_db, "https://example.com/jobs/1", profile)
    assert again.created is False
    assert again.job_offer_id == job.job_offer_id
    with tmp_db.session() as session:
        assert session.query(JobOffer).count() == 1


@pytest.mark.asyncio
async def test_add_job_reports_dealbreaker(
    tmp_db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_read(url: str, **kwargs: object) -> str:
        return PAGE.replace("recrute un", "propose une alternance pour un")

    monkeypatch.setattr("signal_tracker.jobs.manual.read_url", fake_read)
    profile = UserProfile(target_roles=["Data Engineer"], dealbreakers=["alternance"])
    job = await add_job_from_url(tmp_db, "https://example.com/jobs/2", profile)
    assert job.dealbreaker == "alternance"
    assert job.relevance_score == 0.0


@pytest.mark.asyncio
async def test_add_job_requires_title_and_company(
    tmp_db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_read(url: str, **kwargs: object) -> str:
        return "no heading here, " * 40

    monkeypatch.setattr("signal_tracker.jobs.manual.read_url", fake_read)
    with pytest.raises(ValueError, match="title and company"):
        await add_job_from_url(tmp_db, "https://example.com/jobs/3", UserProfile())


@pytest.mark.asyncio
async def test_read_url_rejects_relative_url() -> None:
    with pytest.raises(JinaError):
        await read_url("/jobs/1")
