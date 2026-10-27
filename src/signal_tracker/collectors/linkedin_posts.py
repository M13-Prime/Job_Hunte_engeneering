"""LinkedIn company-posts collector (opt-in, see docs/LINKEDIN.md).

One item per watched company page per run, whose content is the raw posts
text returned by the LinkedIn MCP server; the LLM classifier reads it as
free text. The URL carries a content hash so that new posts create a new
RawItem while an unchanged page is deduplicated.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from signal_tracker.collectors.base import BaseCollector, CollectedItem
from signal_tracker.linkedin.client import LinkedInClient, LinkedInError, company_url
from signal_tracker.utils.logging import get_logger

logger = get_logger(__name__)

MAX_CONTENT_CHARS = 6000


@dataclass(slots=True)
class LinkedInWatch:
    name: str
    slug: str


class LinkedInPostsCollector(BaseCollector):
    source_id = "linkedin"

    def __init__(self, client: LinkedInClient, watchlist: Sequence[LinkedInWatch]) -> None:
        self.client = client
        self.watchlist = list(watchlist)

    @staticmethod
    def watchlist_from_yaml(cfg: dict[str, Any]) -> list[LinkedInWatch]:
        out: list[LinkedInWatch] = []
        for raw in cfg.get("companies") or []:
            name, slug = raw.get("name"), raw.get("slug")
            if name and slug:
                out.append(LinkedInWatch(name=str(name), slug=str(slug)))
        return out

    async def collect(self) -> AsyncIterator[CollectedItem]:
        for watch in self.watchlist:
            url = company_url(watch.slug)
            try:
                data = await self.client.get_company_posts(url)
            except LinkedInError as exc:
                logger.warning("linkedin.posts_failed company=%s err=%s", watch.name, exc)
                if "stopped" in str(exc) or "limit" in str(exc) or "blocked" in str(exc):
                    return
                continue
            text = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
            text = text[:MAX_CONTENT_CHARS]
            if len(text.strip()) < 40:
                continue
            digest = hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]
            yield CollectedItem(
                source=self.source_id,
                url=f"{url}posts/#h={digest}",
                title=f"LinkedIn — {watch.name} : publications récentes",
                content=text,
                published_at=datetime.now(UTC),
            )
