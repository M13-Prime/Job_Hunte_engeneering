"""Small helpers shared by the ATS adapters."""

from __future__ import annotations

import html
import re
from datetime import UTC, datetime
from typing import Any

_TAG = re.compile(r"<[^>]+>")
_SPACES = re.compile(r"\s+")


def as_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def plain_text(value: Any, limit: int = 1500) -> str | None:
    """HTML -> plain text, truncated (descriptions feed the filters and the AI)."""
    if not value:
        return None
    text = _SPACES.sub(" ", html.unescape(_TAG.sub(" ", str(value)))).strip()
    if not text:
        return None
    return text[:limit] + "..." if len(text) > limit else text


def parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        try:
            parsed = datetime.strptime(raw[:10], "%Y-%m-%d")
        except ValueError:
            return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def join_location(*parts: Any) -> str | None:
    seen: list[str] = []
    for p in parts:
        s = as_str(p)
        if s and s not in seen:
            seen.append(s)
    return ", ".join(seen) or None
