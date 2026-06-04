"""Tests for Phase 10 CV-suggested keywords."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from signal_tracker.auth import hash_password
from signal_tracker.dashboard.app import build_app
from signal_tracker.storage import Database, init_db
from signal_tracker.storage.models import User, UserCV, UserKeyword


def _seed_cv(db: Database, *, profile_json: dict[str, Any] | None) -> int:
    with db.session() as s:
        s.add(User(
            email="test@example.com",
            password_hash=hash_password("password"),
            is_active=True, is_owner=True, is_approved=True,
        ))
        s.flush()
        user_id: int = s.execute(select(User.id)).scalar_one()
        s.add(UserCV(
            user_id=user_id, filename="cv.pdf",
            text="x" * 100, char_count=100,
            profile_json=profile_json,
        ))
    return user_id


@pytest.fixture()
def db_with_cv(tmp_path: Path) -> Database:
    db = init_db(tmp_path / "cv.db")
    _seed_cv(db, profile_json={
        "name": "Test", "headline": "Designer", "skills": ["Figma"],
        "languages": [], "education": [], "top_roles": [],
        "notable_achievements": [],
        "suggested_keywords": {
            "field": ["design de service", "design public"],
            "job_title": ["Service Designer", "Design Lead"],
            "other": ["Figma", "double diamond"],
        },
    })
    return db


@pytest.fixture()
def client(db_with_cv: Database) -> TestClient:
    c = TestClient(build_app(db=db_with_cv))
    c.post("/login", data={"email": "test@example.com", "password": "password"})
    return c


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def test_landing_renders_suggested_chips(client: TestClient) -> None:
    resp = client.get("/")
    assert resp.status_code == 200
    body = resp.text
    assert "Suggestions du CV" in body
    assert "design de service" in body
    assert "Service Designer" in body
    assert "double diamond" in body
    # Form posts to the accept endpoint.
    assert 'action="/keywords/suggested/accept"' in body


def test_no_suggestions_panel_when_no_cv(tmp_path: Path) -> None:
    """Without a CV, no suggestions panel is rendered."""
    db = init_db(tmp_path / "no_cv.db")
    with db.session() as s:
        s.add(User(
            email="test@example.com",
            password_hash=hash_password("password"),
            is_active=True, is_owner=True, is_approved=True,
        ))
    c = TestClient(build_app(db=db))
    c.post("/login", data={"email": "test@example.com", "password": "password"})
    resp = c.get("/")
    assert "Suggestions du CV" not in resp.text


def test_already_added_keywords_are_filtered_out(
    db_with_cv: Database,
) -> None:
    """If the user has already added a suggested keyword manually, it stops
    showing up in the panel — avoids the same chip appearing twice."""
    with db_with_cv.session() as s:
        user_id = s.execute(select(User.id)).scalar_one()
        s.add(UserKeyword(
            user_id=user_id, category="field", value="design public",
        ))
    c = TestClient(build_app(db=db_with_cv))
    c.post("/login", data={"email": "test@example.com", "password": "password"})
    body = c.get("/").text
    # "design public" — already in user keywords — shouldn't appear in the
    # suggestions panel. But "design de service" should still be there.
    sug_section = body.split("Suggestions du CV", 1)[1] if "Suggestions du CV" in body else ""
    assert "design de service" in sug_section
    # Ensure it's NOT in the suggested-chip part (it may still appear in the
    # active-keyword chips list higher in the page).
    sug_chips_block = sug_section.split("Watchlist", 1)[0]
    assert "design public" not in sug_chips_block


# ---------------------------------------------------------------------------
# Accept endpoint
# ---------------------------------------------------------------------------


def test_accept_adds_keyword(client: TestClient, db_with_cv: Database) -> None:
    resp = client.post(
        "/keywords/suggested/accept",
        data={"category": "field", "value": "design de service"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "/#suggested-keywords" in resp.headers["location"]
    with db_with_cv.session() as s:
        kws = list(s.execute(select(UserKeyword)).scalars())
        assert any(k.value == "design de service" and k.category == "field" for k in kws)


def test_accept_is_idempotent(client: TestClient, db_with_cv: Database) -> None:
    """Clicking the same suggestion twice doesn't error — the unique
    constraint just rolls back the second insert."""
    for _ in range(2):
        resp = client.post(
            "/keywords/suggested/accept",
            data={"category": "job_title", "value": "Service Designer"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
    with db_with_cv.session() as s:
        count = s.execute(
            select(func.count(UserKeyword.id))
            .where(UserKeyword.value == "Service Designer")
        ).scalar_one()
        assert count == 1


def test_accept_rejects_invalid_category(client: TestClient) -> None:
    resp = client.post(
        "/keywords/suggested/accept",
        data={"category": "bogus", "value": "x"},
        follow_redirects=False,
    )
    assert resp.status_code == 400


def test_accept_rejects_empty_value(client: TestClient) -> None:
    resp = client.post(
        "/keywords/suggested/accept",
        data={"category": "field", "value": "   "},
        follow_redirects=False,
    )
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Schema defaults
# ---------------------------------------------------------------------------


def test_cv_profile_without_suggestions_still_validates() -> None:
    """A CVProfile JSON without suggested_keywords must validate — old CVs
    pre-Phase-10 don't have the field. Pydantic default kicks in."""
    from signal_tracker.preparation.schemas import CVProfile
    profile = CVProfile.model_validate({
        "name": "Test", "headline": None, "years_experience": None,
        "skills": [], "languages": [], "education": [],
        "top_roles": [], "notable_achievements": [],
    })
    assert profile.suggested_keywords.field == []
    assert profile.suggested_keywords.job_title == []
    assert profile.suggested_keywords.other == []
