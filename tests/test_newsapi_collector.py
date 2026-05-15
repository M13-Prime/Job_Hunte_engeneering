"""NewsAPI collector tests."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx

from signal_tracker.collectors.newsapi import NewsApiCollector, NewsApiQuery

OK_PAYLOAD: dict[str, Any] = {
    "status": "ok",
    "totalResults": 2,
    "articles": [
        {
            "source": {"name": "Le Monde"},
            "title": "Carbone 4 publie son rapport CSRD",
            "url": "https://lemonde.fr/c4",
            "description": "Premier rapport.",
            "content": "Contenu detaille.",
            "publishedAt": "2025-05-12T09:00:00Z",
        },
        {
            "source": {"name": "Reuters"},
            "title": "Sweep funding round",
            "url": "https://reuters.com/sweep",
            "publishedAt": "2025-05-12T10:00:00Z",
        },
    ],
}

ERROR_PAYLOAD: dict[str, Any] = {
    "status": "error",
    "code": "apiKeyInvalid",
    "message": "Bad key",
}


async def _collect_all(collector: NewsApiCollector) -> list[Any]:
    items: list[Any] = []
    async for item in collector.collect():
        items.append(item)
    return items


async def test_newsapi_parses_articles(tmp_path: Path) -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["headers"] = dict(request.headers)
        captured["url"] = str(request.url)
        return httpx.Response(200, content=json.dumps(OK_PAYLOAD).encode())

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        collector = NewsApiCollector(
            api_key="test-key",
            queries=[NewsApiQuery(id="csrd", query="CSRD", language="fr")],
            cache_dir=tmp_path,
            client=client,
            rate_limit_seconds=0.0,
        )
        items = await _collect_all(collector)
    finally:
        await client.aclose()

    assert len(items) == 2
    assert items[0].source == "newsapi:csrd"
    assert captured["headers"]["x-api-key"] == "test-key"
    assert "language=fr" in captured["url"]
    assert items[0].published_at is not None


async def test_newsapi_handles_error_payload(tmp_path: Path) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=json.dumps(ERROR_PAYLOAD).encode())

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        collector = NewsApiCollector(
            api_key="bad",
            queries=[NewsApiQuery(id="x", query="y")],
            cache_dir=tmp_path,
            client=client,
            rate_limit_seconds=0.0,
        )
        items = await _collect_all(collector)
    finally:
        await client.aclose()
    assert items == []


def test_newsapi_queries_from_yaml() -> None:
    raw = {"queries": [{"id": "x", "query": "y", "language": "fr", "page_size": 10}]}
    queries = NewsApiCollector.queries_from_yaml(raw)
    assert queries[0].language == "fr"
    assert queries[0].page_size == 10
