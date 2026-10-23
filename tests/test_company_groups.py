"""Signals page: one card per company with merged key people."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from signal_tracker.auth import hash_password
from signal_tracker.dashboard.app import build_app
from signal_tracker.dashboard.company_groups import group_by_company
from signal_tracker.storage import Database, init_db
from signal_tracker.storage.models import RawItem, SearchRun, Signal, User


def _sig(name: str, score: float, **kw: Any) -> Any:
    base: dict[str, Any] = {
        "company_normalized": name.lower(), "company_name": name,
        "total_score": score, "signal_type": "hiring", "hq_country": "France",
        "active_countries": ["France"], "key_persons": [], "target_contact": None,
        "search_run_id": 1,
    }
    base.update(kw)
    return SimpleNamespace(**base)


def test_groups_sorted_and_signals_merged() -> None:
    raw = SimpleNamespace(url="https://a.example/1")
    rows = [
        (_sig("Acme", 55), raw), (_sig("Beta", 70), raw),
        (_sig("Acme", 90, signal_type="funding"), raw),
    ]
    groups = group_by_company(rows)
    assert [g.name for g in groups] == ["Acme", "Beta"]
    acme = groups[0]
    assert acme.count == 2 and acme.best_score == 90 and acme.tier == "hot"
    assert [s.total_score for s, _ in acme.signals] == [90, 55]
    assert acme.types == ["funding", "hiring"]


def test_people_deduplicated_and_new_hire_first() -> None:
    raw = SimpleNamespace(url="https://a.example/1")
    s1 = _sig("Acme", 80, key_persons=[{"name": "Jeanne Roy", "role": "", "is_new_hire": False}])
    s2 = _sig(
        "Acme", 60,
        key_persons=[
            {"name": "jeanne roy", "role": "CTO", "is_new_hire": False},
            {"name": "Paul Martin", "role": "CFO", "is_new_hire": True},
        ],
        target_contact={"name": "Zoé Lin", "role": "DRH"},
    )
    people = group_by_company([(s1, raw), (s2, raw)])[0].people
    assert people[0].name == "Paul Martin"
    jeanne = next(p for p in people if p.name.lower() == "jeanne roy")
    assert jeanne.role == "CTO"
    assert len(people) == 3


@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    db: Database = init_db(tmp_path / "g.db")
    with db.session() as s:
        user = User(
            email="t@example.com", password_hash=hash_password("password"),
            is_active=True, is_owner=True, is_approved=True,
        )
        s.add(user)
        s.flush()
        run = SearchRun(user_id=user.id, label="r", status="done")
        s.add(run)
        s.flush()
        for i, score in enumerate((90.0, 70.0)):
            raw = RawItem(
                source="rss:test", url=f"https://example.com/{i}", title="t",
                content="c", hash=f"h{i}", classified=True,
                published_at=datetime.now(tz=UTC),
            )
            s.add(raw)
            s.flush()
            s.add(Signal(
                raw_item_id=raw.id, signal_type="hiring", company_name="Acme",
                company_normalized="acme", hq_country="France",
                active_countries=["France"],
                key_persons=[{"name": "Jeanne Roy", "role": "CTO", "is_new_hire": True}],
                relevance_score=score, urgency_score=score,
                fit_with_profile_score=score, total_score=score,
                summary_fr=f"Résumé {i}", suggested_angle=None,
                recommended_action="watch", target_contact=None,
                dedup_key=f"k{i}", search_run_id=run.id,
            ))
    c = TestClient(build_app(db=db))
    c.post("/login", data={"email": "t@example.com", "password": "password"})
    return c


def test_page_shows_one_card_with_people_and_no_prepare(client: TestClient) -> None:
    html = client.get("/results", params={"country": "France", "country_scope": "any"}).text
    assert html.count('class="co-card') == 1
    assert "Jeanne Roy" in html and "CTO" in html
    assert "Préparer" not in html
    assert "/prepare" not in html
