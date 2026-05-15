"""Pappers collector tests — diff detection across snapshots."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx

from signal_tracker.collectors.pappers import PappersCollector, PappersWatch


def _payload(reps: list[dict[str, str]], name: str = "Carbone 4") -> dict[str, Any]:
    return {
        "siren": "552120222",
        "nom_entreprise": name,
        "representants": [
            {"nom_complet": p["name"], "qualite": p["role"]} for p in reps
        ],
    }


async def _collect_all(collector: PappersCollector) -> list[Any]:
    items: list[Any] = []
    async for item in collector.collect():
        items.append(item)
    return items


async def test_first_run_initialises_snapshot_no_emit(tmp_path: Path) -> None:
    body = json.dumps(
        _payload([{"name": "Alice Dupont", "role": "President"}])
    ).encode()

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        collector = PappersCollector(
            api_key="test",
            watchlist=[PappersWatch(siren="552120222", label="Carbone 4")],
            snapshot_dir=tmp_path / "snap",
            cache_dir=tmp_path / "cache",
            client=client,
            rate_limit_seconds=0.0,
        )
        items = await _collect_all(collector)
    finally:
        await client.aclose()

    assert items == []
    assert (tmp_path / "snap" / "552120222.json").exists()


async def test_second_run_with_diff_emits_signal(tmp_path: Path) -> None:
    snap_dir = tmp_path / "snap"
    snap_dir.mkdir()
    (snap_dir / "552120222.json").write_text(
        json.dumps(
            {
                "company": "Carbone 4",
                "representants": [{"name": "Alice Dupont", "role": "President"}],
            }
        ),
        encoding="utf-8",
    )
    new_payload = _payload(
        [
            {"name": "Alice Dupont", "role": "President"},
            {"name": "Bob Martin", "role": "Directeur ESG"},
        ]
    )

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=json.dumps(new_payload).encode())

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        collector = PappersCollector(
            api_key="test",
            watchlist=[PappersWatch(siren="552120222")],
            snapshot_dir=snap_dir,
            cache_dir=tmp_path / "cache",
            client=client,
            rate_limit_seconds=0.0,
        )
        items = await _collect_all(collector)
    finally:
        await client.aclose()

    assert len(items) == 1
    item = items[0]
    assert item.title and "Carbone 4" in item.title
    assert item.content and "NOMINATION" in item.content
    assert "Bob Martin" in (item.content or "")


async def test_no_diff_means_no_emit(tmp_path: Path) -> None:
    snap_dir = tmp_path / "snap"
    snap_dir.mkdir()
    (snap_dir / "552120222.json").write_text(
        json.dumps(
            {
                "company": "Carbone 4",
                "representants": [{"name": "Alice Dupont", "role": "President"}],
            }
        ),
        encoding="utf-8",
    )
    same_payload = _payload([{"name": "Alice Dupont", "role": "President"}])

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=json.dumps(same_payload).encode())

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        collector = PappersCollector(
            api_key="test",
            watchlist=[PappersWatch(siren="552120222")],
            snapshot_dir=snap_dir,
            cache_dir=tmp_path / "cache",
            client=client,
            rate_limit_seconds=0.0,
        )
        items = await _collect_all(collector)
    finally:
        await client.aclose()
    assert items == []


def test_watchlist_from_yaml_supports_both_shapes() -> None:
    watch = PappersCollector.watchlist_from_yaml(
        {"watchlist": ["123", {"siren": "456", "label": "X"}]}
    )
    assert [w.siren for w in watch] == ["123", "456"]
    assert watch[1].label == "X"
