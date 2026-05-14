"""Abstract base class for source collectors."""

from __future__ import annotations

import abc
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime


@dataclass(slots=True)
class CollectedItem:
    """A single item returned by a collector, before persistence / classification."""

    source: str
    url: str
    title: str | None
    content: str | None
    published_at: datetime | None


class BaseCollector(abc.ABC):
    """All concrete collectors implement ``collect`` as an async iterator."""

    #: Unique short identifier ("rss", "gdelt", "pappers", ...)
    source_id: str = "base"

    @abc.abstractmethod
    def collect(self) -> AsyncIterator[CollectedItem]:
        """Yield collected items. Concrete classes use ``async def`` with ``yield``."""
        raise NotImplementedError
