"""Generic RSS / Atom collector.

Reads a list of feeds (see ``config/sources.yaml`` -> ``rss``) and yields
one ``CollectedItem`` per entry. HTTP responses are cached on disk for 6h.
"""

from __future__ import annotations

import asyncio
import calendar
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import feedparser
import httpx

from signal_tracker.collectors.base import BaseCollector, CollectedItem
from signal_tracker.utils.http_cache import FileCache
from signal_tracker.utils.logging import get_logger

logger = get_logger(__name__)


@dataclass(slots=True)
class FeedConfig:
    """One feed entry from config/sources.yaml."""

    id: str
    name: str
    url: str
    language: str | None = None

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> FeedConfig:
        return cls(
            id=str(raw["id"]),
            name=str(raw.get("name", raw["id"])),
            url=str(raw["url"]),
            language=raw.get("language"),
        )


class RSSCollector(BaseCollector):
    """Async generic RSS collector with file cache + rate limiting."""

    source_id = "rss"

    def __init__(
        self,
        feeds: Sequence[FeedConfig],
        cache_dir: str | Path = "data/cache/rss",
        cache_ttl_seconds: int = 6 * 3600,
        rate_limit_seconds: float = 1.0,
        timeout_seconds: float = 30.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.feeds = list(feeds)
        self.cache = FileCache(cache_dir, ttl_seconds=cache_ttl_seconds)
        self.rate_limit_seconds = rate_limit_seconds
        self.timeout_seconds = timeout_seconds
        self._client = client  # injectable for tests
        self._owns_client = client is None

    async def _fetch(self, client: httpx.AsyncClient, feed: FeedConfig) -> bytes | None:
        cached = self.cache.get(feed.url)
        if cached is not None:
            logger.debug("rss.fetch cache_hit", extra={"feed_id": feed.id})
            return cached
        try:
            response = await client.get(
                feed.url,
                headers={"User-Agent": "signal-tracker/0.1 (+rss collector)"},
                follow_redirects=True,
            )
        except httpx.HTTPError as exc:
            logger.warning(
                "rss.fetch network_error",
                extra={"feed_id": feed.id, "error": str(exc)},
            )
            return None
        if response.status_code >= 400:
            logger.warning(
                "rss.fetch http_error",
                extra={"feed_id": feed.id, "status": response.status_code},
            )
            return None
        body = response.content
        self.cache.set(feed.url, body)
        return body

    @staticmethod
    def _parse_published(entry: Any) -> datetime | None:
        struct = getattr(entry, "published_parsed", None) or getattr(
            entry, "updated_parsed", None
        )
        if not struct:
            return None
        try:
            return datetime.fromtimestamp(calendar.timegm(struct), tz=UTC)
        except (OverflowError, OSError, TypeError):
            return None

    @staticmethod
    def _entry_content(entry: Any) -> str | None:
        # feedparser exposes summary / description, sometimes content[]
        content_list = getattr(entry, "content", None)
        if content_list:
            try:
                return str(content_list[0].get("value") or "").strip() or None
            except (AttributeError, IndexError, TypeError):
                pass
        for attr in ("summary", "description", "subtitle"):
            value = getattr(entry, attr, None)
            if value:
                return str(value).strip() or None
        return None

    async def collect(self) -> AsyncIterator[CollectedItem]:
        client = self._client or httpx.AsyncClient(timeout=self.timeout_seconds)
        try:
            for index, feed in enumerate(self.feeds):
                if index > 0:
                    await asyncio.sleep(self.rate_limit_seconds)
                body = await self._fetch(client, feed)
                if body is None:
                    continue
                parsed = feedparser.parse(body)
                if parsed.bozo:
                    logger.warning(
                        "rss.parse bozo",
                        extra={
                            "feed_id": feed.id,
                            "reason": str(parsed.bozo_exception)[:200],
                        },
                    )
                count = 0
                for entry in parsed.entries:
                    url = getattr(entry, "link", None)
                    if not url:
                        continue
                    yield CollectedItem(
                        source=f"rss:{feed.id}",
                        url=str(url),
                        title=(getattr(entry, "title", None) or None),
                        content=self._entry_content(entry),
                        published_at=self._parse_published(entry),
                    )
                    count += 1
                logger.info(
                    "rss.collect feed_done",
                    extra={"feed_id": feed.id, "entries": count},
                )
        finally:
            if self._owns_client:
                await client.aclose()


__all__ = ["FeedConfig", "RSSCollector"]
