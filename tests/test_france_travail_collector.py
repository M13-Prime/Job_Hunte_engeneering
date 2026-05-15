"""France Travail collector tests — OAuth2 + moving-average surge detection."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from signal_tracker.collectors.france_travail import (
    FranceTravailCollector,
    FranceTravailConfig,
)


def _offer(company: str) -> dict[str, Any]:
    return {
        "intitule": "Sustainability data analyst",
        "entreprise": {"nom": company},
        "lieuTravail": {"libelle": "Paris"},
    }


def _make_handler(token_payload: dict[str, Any], offers: list[dict[str, Any]]):  # type: ignore[no-untyped-def]
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host.startswith("entreprise"):
            return httpx.Response(200, json=token_payload)
        return httpx.Response(200, json={"resultats": offers})

    return handler


async def _collect_all(collector: FranceTravailCollector) -> list[Any]:
    items: list[Any] = []
    async for item in collector.collect():
        items.append(item)
    return items


async def test_fresh_history_emits_when_min_count_reached(tmp_path: Path) -> None:
    handler = _make_handler(
        {"access_token": "tok", "expires_in": 1500},
        [_offer("Carbone 4"), _offer("Carbone 4"), _offer("Carbone 4")],
    )
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        collector = FranceTravailCollector(
            client_id="cid",
            client_secret="cs",
            config=FranceTravailConfig(
                rome_codes=["M1403"], min_count=3, z_threshold=1.5
            ),
            history_path=tmp_path / "hist.json",
            cache_dir=tmp_path / "cache",
            client=client,
            rate_limit_seconds=0.0,
        )
        items = await _collect_all(collector)
    finally:
        await client.aclose()

    assert len(items) == 1
    assert "Carbone 4" in (items[0].title or "")
    assert (tmp_path / "hist.json").exists()


async def test_below_min_count_does_not_emit(tmp_path: Path) -> None:
    handler = _make_handler(
        {"access_token": "tok", "expires_in": 1500},
        [_offer("Carbone 4")],  # only 1 offer
    )
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        collector = FranceTravailCollector(
            client_id="cid",
            client_secret="cs",
            config=FranceTravailConfig(rome_codes=["M1403"], min_count=3),
            history_path=tmp_path / "hist.json",
            cache_dir=tmp_path / "cache",
            client=client,
            rate_limit_seconds=0.0,
        )
        items = await _collect_all(collector)
    finally:
        await client.aclose()
    assert items == []


async def test_baseline_with_history_uses_z_threshold(tmp_path: Path) -> None:
    # Pre-seed a calm 30-day history (1 offer/day) for "Plan A".
    history: dict[str, dict[str, int]] = {}
    today = datetime.now(tz=UTC).date()
    for i in range(1, 31):
        day = (today - timedelta(days=i)).isoformat()
        history[day] = {"Plan A": 1}
    (tmp_path / "hist.json").write_text(json.dumps(history), encoding="utf-8")

    # Today: 5 offers — well above mean=1, std=0; threshold = max(min_count=3, 1).
    handler = _make_handler(
        {"access_token": "tok", "expires_in": 1500},
        [_offer("Plan A")] * 5,
    )
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        collector = FranceTravailCollector(
            client_id="cid",
            client_secret="cs",
            config=FranceTravailConfig(
                rome_codes=["M1403"], min_count=3, z_threshold=1.5
            ),
            history_path=tmp_path / "hist.json",
            cache_dir=tmp_path / "cache",
            client=client,
            rate_limit_seconds=0.0,
        )
        items = await _collect_all(collector)
    finally:
        await client.aclose()

    assert len(items) == 1
    assert (items[0].content or "").startswith("Plan A a publie 5 offres")


async def test_token_failure_short_circuits(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host.startswith("entreprise"):
            return httpx.Response(401, json={"error": "bad creds"})
        return httpx.Response(200, json={"resultats": []})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        collector = FranceTravailCollector(
            client_id="cid",
            client_secret="cs",
            config=FranceTravailConfig(rome_codes=["M1403"]),
            history_path=tmp_path / "hist.json",
            cache_dir=tmp_path / "cache",
            client=client,
            rate_limit_seconds=0.0,
        )
        items = await _collect_all(collector)
    finally:
        await client.aclose()
    assert items == []
