"""RSS collector tests using httpx's built-in MockTransport (no network)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from signal_tracker.collectors.rss import FeedConfig, RSSCollector

SAMPLE_FEED = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
<channel>
  <title>Test Feed</title>
  <link>https://example.com</link>
  <description>Test</description>
  <item>
    <title>Carbone 4 nomme une nouvelle Directrice ESG</title>
    <link>https://example.com/articles/carbone4</link>
    <description>Carbone 4 cree un poste de direction ESG.</description>
    <pubDate>Mon, 12 May 2025 09:00:00 +0000</pubDate>
  </item>
  <item>
    <title>Sweep leve 22M EUR en Serie B</title>
    <link>https://example.com/articles/sweep</link>
    <description>Levee pour doubler l'equipe data.</description>
    <pubDate>Tue, 13 May 2025 10:30:00 +0000</pubDate>
  </item>
</channel>
</rss>
"""


def _client_returning(body: bytes, status: int = 200) -> httpx.AsyncClient:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(status, content=body)

    transport = httpx.MockTransport(handler)
    return httpx.AsyncClient(transport=transport)


async def _collect_all(collector: RSSCollector) -> list[Any]:
    items = []
    async for item in collector.collect():
        items.append(item)
    return items


async def test_rss_parses_entries(tmp_path: Path) -> None:
    feeds = [FeedConfig(id="test", name="Test", url="https://example.com/feed.xml")]
    client = _client_returning(SAMPLE_FEED)
    try:
        collector = RSSCollector(
            feeds=feeds,
            cache_dir=tmp_path,
            client=client,
            rate_limit_seconds=0.0,
        )
        items = await _collect_all(collector)
    finally:
        await client.aclose()

    assert len(items) == 2
    assert items[0].title == "Carbone 4 nomme une nouvelle Directrice ESG"
    assert items[0].url == "https://example.com/articles/carbone4"
    assert items[0].source == "rss:test"
    assert items[0].content is not None
    assert items[0].published_at is not None
    assert items[0].published_at.year == 2025


async def test_rss_uses_cache_on_second_run(tmp_path: Path) -> None:
    feeds = [FeedConfig(id="test", name="Test", url="https://example.com/feed.xml")]
    calls = {"count": 0}

    def handler(_: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        return httpx.Response(200, content=SAMPLE_FEED)

    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    try:
        collector = RSSCollector(
            feeds=feeds,
            cache_dir=tmp_path,
            client=client,
            rate_limit_seconds=0.0,
        )
        items1 = await _collect_all(collector)
        items2 = await _collect_all(collector)
    finally:
        await client.aclose()

    assert len(items1) == 2
    assert len(items2) == 2
    assert calls["count"] == 1, "Second collect should hit the local cache"


async def test_rss_skips_failing_feed(tmp_path: Path) -> None:
    feeds = [
        FeedConfig(id="bad", name="Bad", url="https://example.com/404.xml"),
        FeedConfig(id="ok", name="OK", url="https://example.com/good.xml"),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        if "404" in str(request.url):
            return httpx.Response(404, content=b"not found")
        return httpx.Response(200, content=SAMPLE_FEED)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        collector = RSSCollector(
            feeds=feeds,
            cache_dir=tmp_path,
            client=client,
            rate_limit_seconds=0.0,
        )
        items = await _collect_all(collector)
    finally:
        await client.aclose()

    assert len(items) == 2  # only the "ok" feed produces items
    assert all(item.source == "rss:ok" for item in items)


def test_feedconfig_from_dict() -> None:
    cfg = FeedConfig.from_dict(
        {"id": "x", "name": "X", "url": "https://x.example/feed", "language": "fr"}
    )
    assert cfg.id == "x"
    assert cfg.language == "fr"


@pytest.mark.live
async def test_live_real_feed_smoke() -> None:
    """Sanity: a known-good feed actually parses. Skipped in CI."""
    feeds = [FeedConfig(id="techcrunch", name="TechCrunch", url="https://techcrunch.com/feed/")]
    collector = RSSCollector(feeds=feeds, cache_dir="data/cache/_live_test")
    items = await _collect_all(collector)
    assert items, "Expected at least one entry from TechCrunch"
