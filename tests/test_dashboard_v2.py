"""Tests for the v2 dashboard endpoints: keywords + pipeline runner."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from signal_tracker.dashboard.app import build_app
from signal_tracker.storage import Database, init_db
from signal_tracker.storage.models import UserKeyword


@pytest.fixture()
def db(tmp_path: Path) -> Database:
    return init_db(tmp_path / "v2.db")


@pytest.fixture()
def client(db: Database) -> TestClient:
    return TestClient(build_app(db=db))


# ---------------------------------------------------------------------------
# Keywords
# ---------------------------------------------------------------------------

def test_add_keyword_persists(client: TestClient, db: Database) -> None:
    response = client.post(
        "/keywords",
        data={"category": "field", "value": "AI for legal"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    with db.session() as s:
        rows = list(s.execute(select(UserKeyword)).scalars())
    assert len(rows) == 1
    assert rows[0].category == "field"
    assert rows[0].value == "AI for legal"


def test_add_keyword_rejects_invalid_category(client: TestClient) -> None:
    response = client.post(
        "/keywords",
        data={"category": "nope", "value": "x"},
        follow_redirects=False,
    )
    assert response.status_code == 400


def test_add_keyword_rejects_empty_value(client: TestClient) -> None:
    response = client.post(
        "/keywords",
        data={"category": "field", "value": "   "},
        follow_redirects=False,
    )
    assert response.status_code == 400


def test_add_keyword_duplicate_is_silent(client: TestClient, db: Database) -> None:
    client.post("/keywords", data={"category": "field", "value": "AI"}, follow_redirects=False)
    response = client.post(
        "/keywords", data={"category": "field", "value": "AI"}, follow_redirects=False
    )
    assert response.status_code == 303
    with db.session() as s:
        assert s.query(UserKeyword).count() == 1


def test_delete_keyword(client: TestClient, db: Database) -> None:
    client.post(
        "/keywords",
        data={"category": "job_title", "value": "Sales Engineer"},
        follow_redirects=False,
    )
    with db.session() as s:
        kw_id = s.query(UserKeyword).one().id
    response = client.post(f"/keywords/{kw_id}/delete", follow_redirects=False)
    assert response.status_code == 303
    with db.session() as s:
        assert s.query(UserKeyword).count() == 0


def test_delete_unknown_keyword_returns_404(client: TestClient) -> None:
    response = client.post("/keywords/9999/delete", follow_redirects=False)
    assert response.status_code == 404


def test_index_renders_keyword_chips(client: TestClient) -> None:
    client.post("/keywords", data={"category": "field", "value": "AI for legal"})
    client.post("/keywords", data={"category": "job_title", "value": "Sales Engineer"})
    response = client.get("/")
    body = response.text
    assert "AI for legal" in body
    assert "Sales Engineer" in body
    assert "Mots-cl" in body  # the panel heading
    assert "Lancer la recherche" in body


# ---------------------------------------------------------------------------
# Pipeline launcher
# ---------------------------------------------------------------------------

def test_run_status_starts_idle(client: TestClient) -> None:
    response = client.get("/run/status")
    assert response.status_code == 200
    payload: dict[str, Any] = response.json()
    assert payload["status"] == "idle"
    assert payload["step"] is None


def test_launch_pipeline_kicks_background_task(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Stub the pipeline imports so the test doesn't hit the LLM.
    async def fake_collect(*_args: Any, **_kwargs: Any) -> Any:
        class R:
            fetched, new, duplicates = 12, 5, 7
        return R()

    async def fake_classify(*_args: Any, **_kwargs: Any) -> Any:
        class R:
            processed, relevant, signals_created, signals_deduped, errors = 5, 2, 2, 0, 0
        return R()

    monkeypatch.setattr("signal_tracker.pipeline.run_collection", fake_collect)
    monkeypatch.setattr("signal_tracker.pipeline.run_classification", fake_classify)

    response = client.post("/run/pipeline", follow_redirects=False)
    assert response.status_code == 303
    # The task runs in the background; the status endpoint should report it.
    # Polling is fine since the task is async — TestClient waits for the loop.
    import time
    for _ in range(20):
        status = client.get("/run/status").json()
        if status["status"] == "done":
            break
        time.sleep(0.1)
    final = client.get("/run/status").json()
    assert final["status"] == "done", final
    assert final["metrics"]["collect"]["new"] == 5
    assert final["metrics"]["classify"]["signals_created"] == 2


def test_launch_while_running_is_ignored(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    async def slow_collect(*_args: Any, **_kwargs: Any) -> Any:
        await asyncio.sleep(0.3)
        class R:
            fetched, new, duplicates = 0, 0, 0
        return R()

    async def fake_classify(*_args: Any, **_kwargs: Any) -> Any:
        class R:
            processed, relevant, signals_created, signals_deduped, errors = 0, 0, 0, 0, 0
        return R()

    monkeypatch.setattr("signal_tracker.pipeline.run_collection", slow_collect)
    monkeypatch.setattr("signal_tracker.pipeline.run_classification", fake_classify)

    client.post("/run/pipeline", follow_redirects=False)
    # Immediately re-launch — should be ignored, no error.
    r2 = client.post("/run/pipeline", follow_redirects=False)
    assert r2.status_code == 303
