"""Tests for the FastAPI dashboard (Phase 5)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from signal_tracker.dashboard.app import build_app
from signal_tracker.storage import Database, init_db
from signal_tracker.storage.models import RawItem, Signal, WatchlistEntry


@pytest.fixture()
def db_with_signals(tmp_path: Path) -> Database:
    db = init_db(tmp_path / "dash.db")
    with db.session() as s:
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
                )
            )
    return db


@pytest.fixture()
def client(db_with_signals: Database) -> TestClient:
    return TestClient(build_app(db=db_with_signals))


def test_healthz(client: TestClient) -> None:
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_landing_renders(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert "Lancer la recherche" in response.text


def test_results_lists_all_signals(client: TestClient) -> None:
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
    with db_with_signals.session() as s:
        s.execute(
            __import__("sqlalchemy").update(Signal)
            .where(Signal.company_name == "Company 0")
            .values(user_feedback="contacted")
        )
    response = client.get("/results?feedback=pending")
    assert response.status_code == 200
    body = response.text
    assert "Company 0" not in body
    assert "Company 1" in body


def test_post_feedback_updates_signal(
    client: TestClient, db_with_signals: Database
) -> None:
    with db_with_signals.session() as s:
        sig_id = s.execute(
            __import__("sqlalchemy").select(Signal.id)
            .where(Signal.company_name == "Company 0")
        ).scalar_one()
    response = client.post(
        f"/signals/{sig_id}/feedback",
        data={"action": "contacted"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    with db_with_signals.session() as s:
        signal = s.get(Signal, sig_id)
        assert signal is not None
        assert signal.user_feedback == "contacted"


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
            __import__("sqlalchemy").select(Signal.id)
            .where(Signal.company_name == "Company 1")
        ).scalar_one()
    response = client.get(
        f"/signals/{sig_id}/contacted", follow_redirects=False
    )
    assert response.status_code == 303
    with db_with_signals.session() as s:
        assert s.get(Signal, sig_id).user_feedback == "contacted"  # type: ignore[union-attr]


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
