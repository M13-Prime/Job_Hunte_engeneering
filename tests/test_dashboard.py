"""Tests for the FastAPI dashboard (Phase 5 + Phase 8 multi-tenant)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from signal_tracker.auth import hash_password
from signal_tracker.dashboard.app import build_app
from signal_tracker.storage import Database, init_db
from signal_tracker.storage.models import (
    RawItem,
    SearchRun,
    Signal,
    SignalFeedback,
    User,
    WatchlistEntry,
)


@pytest.fixture()
def db_with_signals(tmp_path: Path) -> Database:
    """A DB with one user, one SearchRun, and three signals attached to it."""
    db = init_db(tmp_path / "dash.db")
    with db.session() as s:
        user = User(
            email="test@example.com",
            password_hash=hash_password("password"),
            is_active=True,
            is_owner=True,
            is_approved=True,
        )
        s.add(user)
        s.flush()
        run = SearchRun(user_id=user.id, label="test run", status="done")
        s.add(run)
        s.flush()
        for i, score in enumerate([92.0, 70.0, 45.0]):
            raw = RawItem(
                source="rss:test",
                url=f"https://example.com/{i}",
                title=f"Article {i}",
                content="...",
                hash=f"h{i}",
                classified=True,
                published_at=datetime.now(tz=UTC),
            )
            s.add(raw)
            s.flush()
            s.add(
                Signal(
                    raw_item_id=raw.id,
                    signal_type="executive_change",
                    company_name=f"Company {i}",
                    company_normalized=f"company {i}",
                    key_persons=[],
                    relevance_score=score,
                    urgency_score=score,
                    fit_with_profile_score=score,
                    total_score=score,
                    summary_fr=f"Summary {i}",
                    suggested_angle=None,
                    recommended_action="contact_immediate",
                    target_contact=None,
                    dedup_key=f"k{i}",
                    search_run_id=run.id,
                )
            )
    return db


@pytest.fixture()
def client(db_with_signals: Database) -> TestClient:
    """Authenticated client (test@example.com signed in)."""
    c = TestClient(build_app(db=db_with_signals))
    resp = c.post(
        "/login",
        data={"email": "test@example.com", "password": "password"},
        follow_redirects=False,
    )
    assert resp.status_code == 303, resp.text
    return c


def test_healthz_is_public(db_with_signals: Database) -> None:
    """/healthz must NOT require auth."""
    c = TestClient(build_app(db=db_with_signals))
    response = c.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_unauthenticated_request_redirects_to_login(
    db_with_signals: Database,
) -> None:
    c = TestClient(build_app(db=db_with_signals))
    response = c.get("/", headers={"accept": "text/html"}, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_landing_renders(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert "Lancer la recherche" in response.text


def test_results_lists_user_signals(client: TestClient) -> None:
    response = client.get("/results")
    assert response.status_code == 200
    body = response.text
    assert "Company 0" in body
    assert "Company 1" in body
    assert "Company 2" in body


def test_results_filters_by_min_score(client: TestClient) -> None:
    response = client.get("/results?min_score=60")
    assert response.status_code == 200
    body = response.text
    assert "Company 0" in body  # 92
    assert "Company 1" in body  # 70
    assert "Company 2" not in body  # 45


def test_results_filters_by_feedback_pending(
    client: TestClient, db_with_signals: Database
) -> None:
    """A signal with feedback should be excluded from ?feedback=pending."""
    with db_with_signals.session() as s:
        user_id = s.execute(
            select(User.id).where(User.email == "test@example.com")
        ).scalar_one()
        sig0_id = s.execute(
            select(Signal.id).where(Signal.company_name == "Company 0")
        ).scalar_one()
        s.add(SignalFeedback(user_id=user_id, signal_id=sig0_id, action="contacted"))
    response = client.get("/results?feedback=pending")
    assert response.status_code == 200
    body = response.text
    assert "Company 0" not in body
    assert "Company 1" in body


def test_post_feedback_creates_signal_feedback(
    client: TestClient, db_with_signals: Database
) -> None:
    with db_with_signals.session() as s:
        sig_id = s.execute(
            select(Signal.id).where(Signal.company_name == "Company 0")
        ).scalar_one()
        user_id = s.execute(
            select(User.id).where(User.email == "test@example.com")
        ).scalar_one()
    response = client.post(
        f"/signals/{sig_id}/feedback",
        data={"action": "contacted"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    with db_with_signals.session() as s:
        fb = s.execute(
            select(SignalFeedback)
            .where(SignalFeedback.user_id == user_id)
            .where(SignalFeedback.signal_id == sig_id)
        ).scalar_one()
        assert fb.action == "contacted"


def test_post_feedback_rejects_unknown_action(client: TestClient) -> None:
    response = client.post(
        "/signals/1/feedback", data={"action": "garbage"}
    )
    assert response.status_code == 400


def test_get_contacted_link_marks_signal(
    client: TestClient, db_with_signals: Database
) -> None:
    with db_with_signals.session() as s:
        sig_id = s.execute(
            select(Signal.id).where(Signal.company_name == "Company 1")
        ).scalar_one()
        user_id = s.execute(
            select(User.id).where(User.email == "test@example.com")
        ).scalar_one()
    response = client.get(
        f"/signals/{sig_id}/contacted", follow_redirects=False
    )
    assert response.status_code == 303
    with db_with_signals.session() as s:
        fb = s.execute(
            select(SignalFeedback)
            .where(SignalFeedback.user_id == user_id)
            .where(SignalFeedback.signal_id == sig_id)
        ).scalar_one()
        assert fb.action == "contacted"


def test_post_watchlist_adds_entry(
    client: TestClient, db_with_signals: Database
) -> None:
    response = client.post(
        "/watchlist",
        data={"company_name": "Carbone 4 SAS", "notes": "Priorité haute"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    with db_with_signals.session() as s:
        entries = list(s.query(WatchlistEntry).all())
    assert len(entries) == 1
    assert entries[0].company_name == "Carbone 4 SAS"
    assert entries[0].normalized_name == "carbone 4"


def test_post_watchlist_duplicate_is_silent(
    client: TestClient, db_with_signals: Database
) -> None:
    client.post("/watchlist", data={"company_name": "Sweep"}, follow_redirects=False)
    response = client.post(
        "/watchlist", data={"company_name": "Sweep"}, follow_redirects=False
    )
    assert response.status_code == 303
    with db_with_signals.session() as s:
        assert s.query(WatchlistEntry).count() == 1


def test_delete_watchlist_entry(
    client: TestClient, db_with_signals: Database
) -> None:
    client.post("/watchlist", data={"company_name": "DeleteMe"})
    with db_with_signals.session() as s:
        entry_id = s.query(WatchlistEntry).one().id
    response = client.post(
        f"/watchlist/{entry_id}/delete", follow_redirects=False
    )
    assert response.status_code == 303
    with db_with_signals.session() as s:
        assert s.query(WatchlistEntry).count() == 0


def test_two_users_dont_see_each_others_data(db_with_signals: Database) -> None:
    """User isolation: watchlist created by user A is invisible to user B."""
    # Make a second user
    with db_with_signals.session() as s:
        s.add(User(
            email="bob@example.com",
            password_hash=hash_password("password"),
            is_active=True,
            is_approved=True,
        ))

    a = TestClient(build_app(db=db_with_signals))
    a.post("/login", data={"email": "test@example.com", "password": "password"})
    a.post("/watchlist", data={"company_name": "AliceCo"})

    b = TestClient(build_app(db=db_with_signals))
    b.post("/login", data={"email": "bob@example.com", "password": "password"})
    landing_b = b.get("/")
    assert landing_b.status_code == 200
    assert "AliceCo" not in landing_b.text
