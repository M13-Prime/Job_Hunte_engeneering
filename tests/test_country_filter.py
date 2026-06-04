"""Tests for the Phase 10 country filter on /results."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from signal_tracker.auth import hash_password
from signal_tracker.dashboard.app import build_app
from signal_tracker.storage import Database, init_db
from signal_tracker.storage.models import (
    RawItem,
    SearchRun,
    Signal,
    User,
)


@pytest.fixture()
def db_with_country_signals(tmp_path: Path) -> Database:
    """One user, one run, four signals with distinct country footprints."""
    db = init_db(tmp_path / "country.db")
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
        run = SearchRun(user_id=user.id, label="r", status="done")
        s.add(run)
        s.flush()

        fixtures = [
            # (hq, actives, name, score)
            ("France", ["France"], "Greenly", 90.0),
            ("Belgique", ["Belgique", "France"], "BeCo", 85.0),
            ("Allemagne", ["Allemagne"], "Munich Co", 80.0),
            (None, None, "Unknown HQ", 70.0),
        ]
        for i, (hq, actives, name, score) in enumerate(fixtures):
            raw = RawItem(
                source="rss:test", url=f"https://example.com/{i}",
                title=name, content="...", hash=f"h{i}",
                classified=True, published_at=datetime.now(tz=UTC),
            )
            s.add(raw)
            s.flush()
            s.add(Signal(
                raw_item_id=raw.id,
                signal_type="executive_change",
                company_name=name,
                company_normalized=name.lower(),
                hq_country=hq,
                active_countries=actives,
                key_persons=[],
                relevance_score=score, urgency_score=score,
                fit_with_profile_score=score, total_score=score,
                summary_fr=f"Summary {i}",
                suggested_angle=None,
                recommended_action="contact_immediate",
                target_contact=None,
                dedup_key=f"k{i}",
                search_run_id=run.id,
            ))
    return db


@pytest.fixture()
def client(db_with_country_signals: Database) -> TestClient:
    c = TestClient(build_app(db=db_with_country_signals))
    c.post("/login", data={"email": "test@example.com", "password": "password"})
    return c


def test_no_filter_returns_all(client: TestClient) -> None:
    """Without country params the filter is a no-op."""
    resp = client.get("/results")
    assert resp.status_code == 200
    body = resp.text
    assert "Greenly" in body
    assert "BeCo" in body
    assert "Munich Co" in body
    assert "Unknown HQ" in body


def test_filter_hq_country_only(client: TestClient) -> None:
    """country_scope=hq matches against Signal.hq_country exclusively."""
    resp = client.get("/results?country=France&country_scope=hq")
    body = resp.text
    assert "Greenly" in body          # HQ France ✓
    assert "BeCo" not in body         # HQ Belgique, even with France active
    assert "Munich Co" not in body
    assert "Unknown HQ" not in body   # NULL never matches


def test_filter_any_includes_active(client: TestClient) -> None:
    """country_scope=any matches HQ OR active_countries."""
    resp = client.get("/results?country=France&country_scope=any")
    body = resp.text
    assert "Greenly" in body  # HQ France
    assert "BeCo" in body     # France in active_countries
    assert "Munich Co" not in body
    assert "Unknown HQ" not in body


def test_filter_multi_country(client: TestClient) -> None:
    """Multiple country values are OR'd together."""
    resp = client.get("/results?country=France&country=Allemagne&country_scope=hq")
    body = resp.text
    assert "Greenly" in body
    assert "Munich Co" in body
    assert "BeCo" not in body


def test_country_chips_render_in_ui(client: TestClient) -> None:
    """The available countries from the corpus + starter list appear as chips."""
    resp = client.get("/results")
    body = resp.text
    # Starter list common countries
    assert "France" in body
    assert "Belgique" in body
    # And signals' actual HQ countries
    assert "Allemagne" in body


def test_country_tag_renders_on_each_row(client: TestClient) -> None:
    """Each signal row that has hq_country shows it as a country-tag."""
    resp = client.get("/results")
    body = resp.text
    assert 'class="country-tag"' in body
    # Verify the +N badge appears for the BeCo row (HQ Belgique, +France).
    assert "+1" in body
