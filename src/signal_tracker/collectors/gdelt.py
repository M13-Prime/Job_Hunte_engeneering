"""GDELT 2.0 collector — global news search via the DOC 2.0 ArtList API.

Docs: https://blog.gdeltproject.org/gdelt-doc-2-0-api-debuts/
No authentication required.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from signal_tracker.collectors.base import BaseCollector, CollectedItem
from signal_tracker.utils.http_cache import FileCache
from signal_tracker.utils.logging import get_logger

logger = get_logger(__name__)


@dataclass(slots=True)
class GdeltQuery:
    id: str
    query: str
    timespan: str = "24h"
    max_records: int = 100


class GdeltCollector(BaseCollector):
    """Async GDELT 2.0 collector for one or more queries."""

    source_id = "gdelt"
    BASE_URL = "https://api.gdeltproject.org/api/v2/doc/doc"

    def __init__(
        self,
        queries: Sequence[GdeltQuery],
        cache_dir: str | Path = "data/cache/gdelt",
        cache_ttl_seconds: int = 6 * 3600,
        rate_limit_seconds: float = 1.0,
        timeout_seconds: float = 30.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.queries = list(queries)
        self.cache = FileCache(cache_dir, ttl_seconds=cache_ttl_seconds)
        self.rate_limit_seconds = rate_limit_seconds
        self.timeout_seconds = timeout_seconds
        self._client = client
        self._owns_client = client is None

    async def _fetch(
        self, client: httpx.AsyncClient, query: GdeltQuery
    ) -> bytes | None:
        params = {
            "query": query.query,
            "mode": "ArtList",
            "format": "json",
            "maxrecords": str(query.max_records),
            "timespan": query.timespan,
        }
        cache_key = f"{query.id}|{query.query}|{query.timespan}|{query.max_records}"
        cached = self.cache.get(cache_key)
        if cached is not None:
            return cached
        try:
            response = await client.get(self.BASE_URL, params=params)
        except httpx.HTTPError as exc:
            logger.warning(
                "gdelt.fetch error",
                extra={"query_id": query.id, "error": str(exc)},
            )
            return None
        if response.status_code != 200:
            logger.warning(
                "gdelt.fetch http_error",
                extra={"query_id": query.id, "status": response.status_code},
            )
            return None
        body = response.content
        self.cache.set(cache_key, body)
        return body

    @staticmethod
    def _parse_seendate(value: Any) -> datetime | None:
        if not isinstance(value, str) or len(value) < 15:
            return None
        try:
            return datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
        except ValueError:
            return None

    @classmethod
    def queries_from_yaml(cls, raw: dict[str, Any]) -> list[GdeltQuery]:
        out: list[GdeltQuery] = []
        for q in raw.get("queries", []) or []:
            out.append(
                GdeltQuery(
                    id=str(q["id"]),
                    query=str(q["query"]),
                    timespan=str(q.get("timespan", "24h")),
                    max_records=int(q.get("max_records", 100)),
                )
            )
        return out

    async def collect(self) -> AsyncIterator[CollectedItem]:
        client = self._client or httpx.AsyncClient(timeout=self.timeout_seconds)
        try:
            for index, query in enumerate(self.queries):
                if index > 0:
                    await asyncio.sleep(self.rate_limit_seconds)
                body = await self._fetch(client, query)
                if body is None:
                    continue
                try:
                    payload = json.loads(body)
                except json.JSONDecodeError as exc:
                    logger.warning(
                        "gdelt.parse bad_json",
                        extra={"query_id": query.id, "error": str(exc)[:200]},
                    )
                    continue
                count = 0
                for article in payload.get("articles", []) or []:
                    url = article.get("url")
                    if not url:
                        continue
                    yield CollectedItem(
                        source=f"gdelt:{query.id}",
                        url=str(url),
                        title=article.get("title") or None,
                        content=article.get("title") or None,
                        published_at=self._parse_seendate(article.get("seendate")),
                    )
                    count += 1
                logger.info(
                    "gdelt.collect query_done",
                    extra={"query_id": query.id, "articles": count},
                )
        finally:
            if self._owns_client:
                await client.aclose()


__all__ = ["GdeltCollector", "GdeltQuery"]
