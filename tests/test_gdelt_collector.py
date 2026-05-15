"""GDELT collector tests using httpx MockTransport."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx

from signal_tracker.collectors.gdelt import GdeltCollector, GdeltQuery

SAMPLE_PAYLOAD: dict[str, Any] = {
    "articles": [
        {
            "url": "https://example.com/a",
            "title": "Carbone 4 nomme une nouvelle Directrice ESG",
            "seendate": "20250512T090000Z",
            "domain": "example.com",
            "language": "French",
            "sourcecountry": "France",
        },
        {
            "url": "https://example.com/b",
            "title": "Sweep raises EUR 22M Series B",
            "seendate": "20250512T103000Z",
        },
    ]
}


def _make_client(handler) -> httpx.AsyncClient:  # type: ignore[no-untyped-def]
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def _collect_all(collector: GdeltCollector) -> list[Any]:
    items: list[Any] = []
    async for item in collector.collect():
        items.append(item)
    return items


async def test_gdelt_parses_articles(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert "query=" in str(request.url)
        return httpx.Response(200, content=json.dumps(SAMPLE_PAYLOAD).encode())

    client = _make_client(handler)
    try:
        collector = GdeltCollector(
            queries=[GdeltQuery(id="t", query='"Carbone 4"')],
            cache_dir=tmp_path,
            client=client,
            rate_limit_seconds=0.0,
        )
        items = await _collect_all(collector)
    finally:
        await client.aclose()

    assert len(items) == 2
    assert items[0].source == "gdelt:t"
    assert items[0].title and "Carbone 4" in items[0].title
    assert items[0].published_at is not None
    assert items[0].published_at.year == 2025


async def test_gdelt_skips_bad_json(tmp_path: Path) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<html>oops</html>")

    client = _make_client(handler)
    try:
        collector = GdeltCollector(
            queries=[GdeltQuery(id="t", query="x")],
            cache_dir=tmp_path,
            client=client,
            rate_limit_seconds=0.0,
        )
        items = await _collect_all(collector)
    finally:
        await client.aclose()
    assert items == []


async def test_gdelt_uses_cache(tmp_path: Path) -> None:
    calls = {"n": 0}

    def handler(_: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, content=json.dumps(SAMPLE_PAYLOAD).encode())

    client = _make_client(handler)
    try:
        collector = GdeltCollector(
            queries=[GdeltQuery(id="t", query="x")],
            cache_dir=tmp_path,
            client=client,
            rate_limit_seconds=0.0,
        )
        await _collect_all(collector)
        await _collect_all(collector)
    finally:
        await client.aclose()
    assert calls["n"] == 1


def test_queries_from_yaml() -> None:
    raw = {
        "queries": [
            {"id": "x", "query": "Carbone 4", "timespan": "1d", "max_records": 50},
        ]
    }
    queries = GdeltCollector.queries_from_yaml(raw)
    assert len(queries) == 1
    assert queries[0].timespan == "1d"
    assert queries[0].max_records == 50
