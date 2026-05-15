"""NewsAPI.org collector (v2 /everything endpoint).

Docs: https://newsapi.org/docs/endpoints/everything
Free tier: 100 requests/day, 30-day historical window.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

from signal_tracker.collectors.base import BaseCollector, CollectedItem
from signal_tracker.utils.http_cache import FileCache
from signal_tracker.utils.logging import get_logger

logger = get_logger(__name__)


@dataclass(slots=True)
class NewsApiQuery:
    id: str
    query: str
    language: str | None = None
    page_size: int = 50
    sort_by: str = "publishedAt"


class NewsApiCollector(BaseCollector):
    """Async NewsAPI v2 collector."""

    source_id = "newsapi"
    BASE_URL = "https://newsapi.org/v2/everything"

    def __init__(
        self,
        api_key: str,
        queries: Sequence[NewsApiQuery],
        cache_dir: str | Path = "data/cache/newsapi",
        cache_ttl_seconds: int = 6 * 3600,
        rate_limit_seconds: float = 1.0,
        timeout_seconds: float = 30.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.api_key = api_key
        self.queries = list(queries)
        self.cache = FileCache(cache_dir, ttl_seconds=cache_ttl_seconds)
        self.rate_limit_seconds = rate_limit_seconds
        self.timeout_seconds = timeout_seconds
        self._client = client
        self._owns_client = client is None

    @classmethod
    def queries_from_yaml(cls, raw: dict[str, Any]) -> list[NewsApiQuery]:
        out: list[NewsApiQuery] = []
        for q in raw.get("queries", []) or []:
            out.append(
                NewsApiQuery(
                    id=str(q["id"]),
                    query=str(q["query"]),
                    language=q.get("language"),
                    page_size=int(q.get("page_size", 50)),
                    sort_by=str(q.get("sort_by", "publishedAt")),
                )
            )
        return out

    async def _fetch(
        self, client: httpx.AsyncClient, query: NewsApiQuery
    ) -> bytes | None:
        params: dict[str, str] = {
            "q": query.query,
            "sortBy": query.sort_by,
            "pageSize": str(query.page_size),
        }
        if query.language:
            params["language"] = query.language
        cache_key = f"{query.id}|{query.query}|{query.language}|{query.page_size}"
        cached = self.cache.get(cache_key)
        if cached is not None:
            return cached
        try:
            response = await client.get(
                self.BASE_URL,
                params=params,
                headers={"X-Api-Key": self.api_key},
            )
        except httpx.HTTPError as exc:
            logger.warning(
                "newsapi.fetch error",
                extra={"query_id": query.id, "error": str(exc)},
            )
            return None
        if response.status_code != 200:
            logger.warning(
                "newsapi.fetch http_error",
                extra={"query_id": query.id, "status": response.status_code},
            )
            return None
        body = response.content
        self.cache.set(cache_key, body)
        return body

    @staticmethod
    def _parse_published(value: Any) -> datetime | None:
        if not isinstance(value, str):
            return None
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None

    async def collect(self) -> AsyncIterator[CollectedItem]:
        client = self._client or httpx.AsyncClient(timeout=self.timeout_seconds)
        try:
            for index, query in enumerate(self.queries):
                if index > 0:
                    await asyncio.sleep(self.rate_limit_seconds)
                body = await self._fetch(client, query)
                if body is None:
                    continue
                import json

                try:
                    payload = json.loads(body)
                except json.JSONDecodeError:
                    continue
                if payload.get("status") != "ok":
                    logger.warning(
                        "newsapi.api_error",
                        extra={
                            "query_id": query.id,
                            "api_code": payload.get("code"),
                            "api_message": payload.get("message"),
                        },
                    )
                    continue
                count = 0
                for article in payload.get("articles", []) or []:
                    url = article.get("url")
                    if not url:
                        continue
                    yield CollectedItem(
                        source=f"newsapi:{query.id}",
                        url=str(url),
                        title=article.get("title") or None,
                        content=(
                            article.get("content")
                            or article.get("description")
                            or None
                        ),
                        published_at=self._parse_published(article.get("publishedAt")),
                    )
                    count += 1
                logger.info(
                    "newsapi.collect query_done",
                    extra={"query_id": query.id, "articles": count},
                )
        finally:
            if self._owns_client:
                await client.aclose()


__all__ = ["NewsApiCollector", "NewsApiQuery"]
