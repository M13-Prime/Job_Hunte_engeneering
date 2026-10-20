"""Offer filters: country, keywords (AND / OR) and experience range."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from signal_tracker.auth import hash_password
from signal_tracker.dashboard.app import build_app
from signal_tracker.jobs.filters import (
    JobFilter,
    is_exec_title,
    is_junior_title,
    keywords_match,
    location_matches_country,
    parse_years_required,
    rejection_reason,
)
from signal_tracker.storage import Database, init_db
from signal_tracker.storage.models import JobOffer, User, UserKeyword, WatchlistEntry


def test_location_matching() -> None:
    assert location_matches_country("Paris, France", "France") is True
    assert location_matches_country("Lyon", "France") is True
    assert location_matches_country("Île-de-France", "France") is True
    assert location_matches_country("London, UK", "France") is False
    assert location_matches_country("Berlin", "France") is False
    assert location_matches_country("Remote", "France") is None
    assert location_matches_country("", "France") is None
    assert location_matches_country("Berlin", "Monde") is None


def test_keywords_and_vs_or_with_synonyms() -> None:
    text = "Responsable RSE: pilotage ESG et projets d'intelligence artificielle"
    assert keywords_match(["sustainability", "AI"], text, mode="all")
    assert not keywords_match(["sustainability", "AI"], "Responsable RSE", mode="all")
    assert keywords_match(["sustainability", "AI"], "Responsable RSE", mode="any")
    assert keywords_match([], "anything")
    # "ai" must not match inside another word
    assert not keywords_match(["AI"], "maintain the mailing chain")


def test_parse_years_required() -> None:
    assert parse_years_required("5-10 years of experience in ESG") == (5, 10)
    assert parse_years_required("Minimum 7 ans d'expérience") == (7, None)
    assert parse_years_required("3+ years' experience") == (3, None)
    assert parse_years_required("Founded 10 years ago, we grow fast") is None
    assert parse_years_required(None) is None


def test_title_seniority() -> None:
    assert is_exec_title("Directeur RSE")
    assert is_exec_title("VP Sustainability")
    assert not is_exec_title("Sustainability Manager")
    assert is_junior_title("Stage - Analyste ESG")
    assert not is_junior_title("Senior ESG Analyst")


def _reason(title: str, location: str, desc: str, flt: JobFilter) -> str | None:
    return rejection_reason(
        title=title, location=location, department=None, description=desc, flt=flt,
    )


def test_experience_range_hides_directors_and_juniors() -> None:
    flt = JobFilter(country="France", exp_min=5, exp_max=10)
    assert _reason("Director of Sustainability", "Paris", "", flt) == "too_senior"
    assert _reason("ESG Analyst", "Paris", "2 years of experience", flt) == "too_junior"
    assert _reason("Stagiaire ESG", "Paris", "", flt) == "too_junior"
    assert _reason("ESG Lead", "Paris", "12 ans d'expérience minimum", flt) == "too_senior"
    assert _reason("ESG Lead", "Paris", "5-8 ans d'expérience", flt) is None
    assert _reason("ESG Lead", "Berlin", "5-8 ans d'expérience", flt) == "country"


def test_unknown_location_toggle() -> None:
    assert _reason("ESG Lead", "Remote", "", JobFilter(country="France")) is None
    strict = JobFilter(country="France", include_unknown=False)
    assert _reason("ESG Lead", "Remote", "", strict) == "country"


@pytest.fixture()
def db_jobs(tmp_path: Path) -> Database:
    db = init_db(tmp_path / "jobs.db")
    with db.session() as s:
        u = User(
            email="t@example.com", password_hash=hash_password("password"),
            is_active=True, is_owner=True, is_approved=True,
        )
        s.add(u)
        s.flush()
        s.add(WatchlistEntry(user_id=u.id, company_name="Acme", normalized_name="acme"))
        s.add(UserKeyword(user_id=u.id, category="field", value="sustainability"))
        offers = [
            (
                "Sustainability and AI Manager", "Paris, France",
                "Build AI for ESG. 5-8 years of experience.",
            ),
            ("Sustainability Manager", "Paris, France", "ESG reporting. 6 years of experience."),
            ("Director of Sustainability", "Paris, France", "Lead everything."),
            ("Sustainability and AI Lead", "London, UK", "AI and ESG. 6 years of experience."),
        ]
        for i, (title, loc, desc) in enumerate(offers):
            s.add(JobOffer(
                company_normalized="acme", company_name="Acme", ats="greenhouse",
                ats_company_slug="acme", external_id=str(i), title=title,
                url=f"https://example.com/{i}", location=loc, description=desc,
                relevance_score=50.0, is_open=True, dedup_key=f"k{i}",
                collected_at=datetime.now(tz=UTC),
            ))
    return db


@pytest.fixture()
def client(db_jobs: Database) -> TestClient:
    c = TestClient(build_app(db=db_jobs))
    c.post("/login", data={"email": "t@example.com", "password": "password"})
    return c


def test_jobs_default_view_uses_country_france(client: TestClient) -> None:
    body = client.get("/jobs").text
    assert "Sustainability and AI Manager" in body
    assert "London, UK" not in body


def test_jobs_keywords_all_requires_both(client: TestClient) -> None:
    body = client.get("/jobs?f=1&country=all&kw=sustainability&kw=AI&mode=all").text
    assert "Sustainability and AI Manager" in body
    assert "Sustainability and AI Lead" in body        # country=all keeps London
    assert 'href="/jobs/2"' not in body                    # no AI -> hidden under AND
    assert "Director of Sustainability" not in body


def test_jobs_keywords_any_widens(client: TestClient) -> None:
    body = client.get("/jobs?f=1&country=France&kw=sustainability&kw=AI&mode=any").text
    assert "Sustainability Manager" in body


def test_save_prefs_applies_experience_everywhere(
    client: TestClient, db_jobs: Database,
) -> None:
    r = client.post(
        "/jobs/prefs",
        data={"country": "France", "exp_min": "5", "exp_max": "10"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    with db_jobs.session() as s:
        u = s.query(User).one()
        assert (u.exp_min, u.exp_max) == (5, 10)
    body = client.get("/jobs").text
    assert "Director of Sustainability" not in body
    assert "masquée" in body
