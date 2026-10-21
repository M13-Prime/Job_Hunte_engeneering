"""Phase 12 — /jobs AI match, tailored CV + letter, application tracker."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from signal_tracker.applications.render import cv_to_markdown, safe_filename
from signal_tracker.applications.schemas import TailoredCV, TailoredExperience
from signal_tracker.applications.tracker import apply_status, is_followup_due
from signal_tracker.auth import hash_password
from signal_tracker.dashboard.app import build_app
from signal_tracker.storage import Database, init_db
from signal_tracker.storage.models import (
    Application,
    JobAgentScore,
    JobOffer,
    RawItem,
    SearchRun,
    Signal,
    User,
    UserCV,
    WatchlistEntry,
)
from signal_tracker.utils.normalize import normalize_company_name

_seq = {"n": 0}


@pytest.fixture()
def db(tmp_path: Path) -> Database:
    db = init_db(tmp_path / "apps.db")
    with db.session() as s:
        s.add(User(
            email="me@example.com", password_hash=hash_password("password"),
            is_active=True, is_owner=True, is_approved=True,
        ))
        s.add(User(
            email="other@example.com", password_hash=hash_password("password"),
            is_active=True, is_approved=True,
        ))
    return db


def _uid(db: Database, email: str = "me@example.com") -> int:
    with db.session() as s:
        return int(s.execute(select(User.id).where(User.email == email)).scalar_one())


def _login(db: Database, email: str = "me@example.com") -> TestClient:
    c = TestClient(build_app(db=db))
    resp = c.post(
        "/login", data={"email": email, "password": "password"}, follow_redirects=False,
    )
    assert resp.status_code == 303, resp.text
    return c


@pytest.fixture()
def client(db: Database) -> TestClient:
    return _login(db)


def _make_job(
    db: Database, *, company: str = "Acme", title: str = "Data Analyst ESG",
    heuristic: float = 40.0,
) -> int:
    _seq["n"] += 1
    n = _seq["n"]
    with db.session() as s:
        job = JobOffer(
            company_normalized=normalize_company_name(company),
            company_name=company, ats="greenhouse", ats_company_slug=company.lower(),
            external_id=f"{company}-{n}", title=title, url=f"https://jobs.example/{n}",
            location="Paris", department="Data",
            description="We need SQL, Python and CSRD reporting experience.",
            relevance_score=heuristic, matched_roles=["Data Analyst ESG"],
            is_open=True, dedup_key=f"{company}|greenhouse|{n}",
        )
        s.add(job)
        s.flush()
        return job.id


def _add_cv(db: Database, user_id: int, text: str = "Malek, data engineer. SQL, Python.") -> None:
    with db.session() as s:
        s.add(UserCV(user_id=user_id, filename="cv.pdf", text=text, char_count=len(text)))


def _watch(db: Database, user_id: int, company: str = "Acme") -> None:
    with db.session() as s:
        s.add(WatchlistEntry(
            user_id=user_id, company_name=company,
            normalized_name=normalize_company_name(company),
        ))


def _fake_llm(payload: dict[str, Any]) -> Any:
    async def fake_acompletion(**_: Any) -> SimpleNamespace:
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content=json.dumps(payload))
        )])
    return fake_acompletion


SCORE_PAYLOAD = {
    "fit_score": 78,
    "fit_reasoning": "Bon match data + ESG.",
    "killer_angle": "Pipelines CSRD chez EcoVadis.",
    "why_now": "",
    "matched_skills": ["SQL", "Python"],
    "missing_skills": ["Power BI"],
}

TAILOR_PAYLOAD = {
    "language": "fr",
    "cv": {
        "name": "Malek",
        "headline": "Data Engineer ESG",
        "contact_line": None,
        "summary": "Data engineer orienté reporting extra-financier.",
        "skills": ["SQL", "Python"],
        "experiences": [{
            "title": "Data Engineer", "company": "EcoVadis", "period": "2023-",
            "location": "Paris", "bullets": ["Pipelines Databricks pour le scoring RSE."],
        }],
        "education": ["Master data"],
        "languages": ["Français", "Anglais"],
        "certifications": [],
    },
    "cover_letter": "Madame, Monsieur,\n\nJe candidate...",
    "changes": ["Mise en avant du reporting RSE."],
    "gaps": ["Power BI"],
}


# ---------------------------------------------------------------------------
# /jobs — listing + AI score
# ---------------------------------------------------------------------------

def test_jobs_page_lists_offers_from_watchlist_only(client: TestClient, db: Database) -> None:
    uid = _uid(db)
    _watch(db, uid, "Acme")
    _make_job(db, company="Acme", title="Climate Data Scientist")
    _make_job(db, company="Elsewhere", title="Unrelated Role")
    resp = client.get("/jobs")
    assert resp.status_code == 200
    assert "Climate Data Scientist" in resp.text
    assert "Unrelated Role" not in resp.text
    assert "Scorer" in resp.text


def test_score_without_cv_redirects_with_message(client: TestClient, db: Database) -> None:
    job_id = _make_job(db)
    resp = client.post(
        f"/jobs/{job_id}/score", data={"next": f"/jobs#job-{job_id}"}, follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == f"/jobs?msg=no_cv#job-{job_id}"


def test_score_persists_match_breakdown(
    client: TestClient, db: Database, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import litellm

    uid = _uid(db)
    _add_cv(db, uid)
    job_id = _make_job(db)
    monkeypatch.setattr(litellm, "acompletion", _fake_llm(SCORE_PAYLOAD))
    resp = client.post(f"/jobs/{job_id}/score", follow_redirects=False)
    assert resp.status_code == 303
    with db.session() as s:
        row = s.execute(select(JobAgentScore)).scalar_one()
        assert row.user_id == uid
        assert row.agent_score == 78
        assert row.matched_skills == ["SQL", "Python"]
        assert row.missing_skills == ["Power BI"]
    page = client.get(f"/jobs/{job_id}")
    assert page.status_code == 200
    assert "Power BI" in page.text
    assert "Pipelines CSRD" in page.text
    # Scored offers show on /jobs even without a watchlist entry.
    assert "Data Analyst ESG" in client.get("/jobs").text


def test_score_llm_failure_redirects_with_message(
    client: TestClient, db: Database, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import litellm

    async def boom(**_: Any) -> Any:
        raise RuntimeError("provider down")

    _add_cv(db, _uid(db))
    job_id = _make_job(db)
    monkeypatch.setattr(litellm, "acompletion", boom)
    resp = client.post(f"/jobs/{job_id}/score", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == f"/jobs/{job_id}?msg=score_failed"


def test_refresh_without_companies_is_refused(client: TestClient) -> None:
    resp = client.post("/jobs/refresh", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/jobs?msg=no_companies"


# ---------------------------------------------------------------------------
# Tracker
# ---------------------------------------------------------------------------

def test_track_job_creates_a_single_application(client: TestClient, db: Database) -> None:
    job_id = _make_job(db)
    for _ in range(2):
        resp = client.post(f"/jobs/{job_id}/track", follow_redirects=False)
        assert resp.status_code == 303
    with db.session() as s:
        rows = list(s.execute(select(Application)).scalars())
    assert len(rows) == 1
    assert rows[0].status == "saved"
    assert rows[0].job_offer_id == job_id
    board = client.get("/applications")
    assert board.status_code == 200
    assert "Data Analyst ESG" in board.text


def test_status_changes_manage_followup(client: TestClient, db: Database) -> None:
    resp = client.post(
        "/applications",
        data={"company_name": "Acme", "title": "PM", "url": "https://acme.example/job"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    with db.session() as s:
        app_id = s.execute(select(Application.id)).scalar_one()

    client.post(f"/applications/{app_id}/status", data={"status": "applied"})
    with db.session() as s:
        row = s.get(Application, app_id)
        assert row is not None
        assert row.status == "applied"
        assert row.applied_at is not None
        assert row.next_followup_at is not None
        assert row.next_followup_at - row.applied_at >= timedelta(days=6)

    client.post(f"/applications/{app_id}/status", data={"status": "rejected"})
    with db.session() as s:
        row = s.get(Application, app_id)
        assert row is not None
        assert row.next_followup_at is None

    bad = client.post(
        f"/applications/{app_id}/status", data={"status": "nope"}, follow_redirects=False,
    )
    assert bad.status_code == 400


def test_manual_application_validation(client: TestClient) -> None:
    resp = client.post(
        "/applications",
        data={"company_name": "Acme", "title": "PM", "url": "javascript:alert(1)"},
        follow_redirects=False,
    )
    assert resp.status_code == 400


def test_update_notes_and_followup_date(client: TestClient, db: Database) -> None:
    client.post("/applications", data={"company_name": "Acme", "title": "PM"})
    with db.session() as s:
        app_id = s.execute(select(Application.id)).scalar_one()
    resp = client.post(
        f"/applications/{app_id}/update",
        data={"notes": "Relancer Julie", "next_followup_at": "2030-01-15"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    with db.session() as s:
        row = s.get(Application, app_id)
        assert row is not None
        assert row.notes == "Relancer Julie"
        assert row.next_followup_at == datetime(2030, 1, 15)


def test_applications_are_private(db: Database) -> None:
    me = _login(db)
    me.post("/applications", data={"company_name": "Acme", "title": "PM"})
    with db.session() as s:
        app_id = s.execute(select(Application.id)).scalar_one()
    other = _login(db, "other@example.com")
    assert "Acme" not in other.get("/applications").text
    resp = other.post(
        f"/applications/{app_id}/status", data={"status": "applied"}, follow_redirects=False,
    )
    assert resp.status_code == 404
    resp = other.post(f"/applications/{app_id}/delete", follow_redirects=False)
    assert resp.status_code == 404


def test_track_signal_creates_spontaneous_application(client: TestClient, db: Database) -> None:
    uid = _uid(db)
    with db.session() as s:
        run = SearchRun(user_id=uid, label="t", status="done")
        s.add(run)
        s.flush()
        raw = RawItem(source="rss", url="https://news/1", title="Acme lève 10M", hash="h1")
        s.add(raw)
        s.flush()
        sig = Signal(
            raw_item_id=raw.id, signal_type="funding", company_name="Acme",
            company_normalized="acme", relevance_score=80, urgency_score=70,
            fit_with_profile_score=70, total_score=80, summary_fr="Acme lève 10M.",
            suggested_angle="Proposer la data RSE.", recommended_action="contact",
            dedup_key="sig-1", search_run_id=run.id,
        )
        s.add(sig)
        s.flush()
        signal_id = sig.id
    resp = client.post(f"/signals/{signal_id}/track", follow_redirects=False)
    assert resp.status_code == 303
    with db.session() as s:
        row = s.execute(select(Application)).scalar_one()
        assert row.signal_id == signal_id
        assert row.title == "Candidature spontanée"
    # Someone else's signal → 404.
    other = _login(db, "other@example.com")
    assert other.post(f"/signals/{signal_id}/track", follow_redirects=False).status_code == 404


# ---------------------------------------------------------------------------
# Tailored CV + letter
# ---------------------------------------------------------------------------

def test_tailor_without_cv_redirects(client: TestClient, db: Database) -> None:
    job_id = _make_job(db)
    resp = client.post(f"/jobs/{job_id}/tailor", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == f"/jobs/{job_id}?msg=no_cv"


def test_tailor_generates_documents(
    client: TestClient, db: Database, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import litellm

    _add_cv(db, _uid(db))
    job_id = _make_job(db)
    monkeypatch.setattr(litellm, "acompletion", _fake_llm(TAILOR_PAYLOAD))
    resp = client.post(f"/jobs/{job_id}/tailor", follow_redirects=False)
    assert resp.status_code == 303
    with db.session() as s:
        row = s.execute(select(Application)).scalar_one()
        app_id = row.id
        assert row.cover_letter and row.cover_letter.startswith("Madame")
        assert row.tailored_cv is not None
        assert row.tailored_cv["headline"] == "Data Engineer ESG"
    assert resp.headers["location"] == f"/applications/{app_id}/documents"

    page = client.get(f"/applications/{app_id}/documents")
    assert page.status_code == 200
    assert "Data Engineer ESG" in page.text
    assert "Power BI" in page.text  # gaps are shown, not hidden in the CV

    md = client.get(f"/applications/{app_id}/cv.md")
    assert md.status_code == 200
    assert "attachment" in md.headers["content-disposition"]
    assert "## Expérience" in md.text
    letter = client.get(f"/applications/{app_id}/letter.txt")
    assert letter.status_code == 200
    assert letter.text.startswith("Madame")

    other = _login(db, "other@example.com")
    assert other.get(f"/applications/{app_id}/documents").status_code == 404


def test_documents_404_before_generation(client: TestClient) -> None:
    client.post("/applications", data={"company_name": "Acme", "title": "PM"})
    page = client.get("/applications/1/documents")
    assert page.status_code == 404


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

def test_apply_status_and_followup_due() -> None:
    app = Application(user_id=1, company_name="A", title="B", status="saved")
    apply_status(app, "applied", followup_days=7)
    assert app.next_followup_at is not None
    assert not is_followup_due(app)
    later = datetime.now(UTC).replace(tzinfo=None) + timedelta(days=8)
    assert is_followup_due(app, now=later)
    apply_status(app, "offer")
    assert app.next_followup_at is None
    with pytest.raises(ValueError):
        apply_status(app, "unknown")


def test_cv_markdown_and_filename() -> None:
    cv = TailoredCV(
        name="Malek", headline="Data Engineer", summary="Résumé.",
        skills=["SQL"],
        experiences=[TailoredExperience(
            title="DE", company="EcoVadis", period="2023-", bullets=["Pipelines"],
        )],
    )
    md = cv_to_markdown(cv, "en")
    assert md.startswith("# Malek")
    assert "## Experience" in md
    assert "- Pipelines" in md
    assert safe_filename("cv", "L'Oréal", "Chef de projet") == "cv-l-or-al-chef-de-projet"


def test_review_runs_only_on_button(
    client: TestClient, db: Database, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import litellm

    calls: list[int] = []
    fake = _fake_llm(TAILOR_PAYLOAD)

    async def counting(**kw: Any) -> SimpleNamespace:
        calls.append(1)
        return await fake(**kw)  # type: ignore[no-any-return]

    _add_cv(db, _uid(db))
    job_id = _make_job(db)
    monkeypatch.setattr(litellm, "acompletion", counting)
    client.post(f"/jobs/{job_id}/tailor", follow_redirects=False)
    assert len(calls) == 1                      # generation only, no automatic review
    with db.session() as s:
        app_id = s.execute(select(Application)).scalar_one().id
    page = client.get(f"/applications/{app_id}/documents").text
    assert "Relecture anti-invention" in page and "pas encore relu" in page

    resp = client.post(f"/applications/{app_id}/review", follow_redirects=False)
    assert resp.status_code == 303 and len(calls) == 2
    with db.session() as s:
        row = s.execute(select(Application)).scalar_one()
        assert row.tailored_cv and row.tailored_cv.get("_reviewed_at")
    assert "relu par le 2e agent" in client.get(f"/applications/{app_id}/documents").text
