"""Read any web page as markdown through Jina Reader (r.jina.ai).

Borrowed from agent-reach: prefixing a URL with ``https://r.jina.ai/``
returns a rendered, markdown-ish version of the page. No API key, no
browser, no login — which makes it the cheap way to read a job posting or
a company page that our ATS backends don't cover (LinkedIn, Welcome to the
Jungle, an in-house careers page...).

Limits worth knowing: it is a third-party service (the URL you ask for is
sent to Jina), it is rate-limited when unauthenticated, and it returns
whatever the page shows a logged-out visitor — so a members-only posting
comes back truncated or empty.
"""

from __future__ import annotations

import httpx

from signal_tracker.utils.logging import get_logger

logger = get_logger(__name__)

READER_PREFIX = "https://r.jina.ai/"
DEFAULT_TIMEOUT = 45.0
MAX_CHARS = 20000


class JinaError(RuntimeError):
    """The reader could not return usable text for that URL."""


async def read_url(
    url: str, *, client: httpx.AsyncClient | None = None, timeout: float = DEFAULT_TIMEOUT
) -> str:
    """Return the page as markdown text. Raises JinaError on failure."""
    if not url.startswith(("http://", "https://")):
        raise JinaError(f"not an absolute URL: {url!r}")

    own_client = client is None
    client = client or httpx.AsyncClient(follow_redirects=True, timeout=timeout)
    try:
        response = await client.get(
            f"{READER_PREFIX}{url}",
            headers={"Accept": "text/plain", "User-Agent": "signal-tracker/1.0"},
            timeout=timeout,
        )
    except httpx.HTTPError as exc:
        raise JinaError(f"reader request failed: {exc}") from exc
    finally:
        if own_client:
            await client.aclose()

    if response.status_code != 200:
        raise JinaError(f"reader returned HTTP {response.status_code}")
    text = response.text.strip()
    if len(text) < 200:
        raise JinaError("reader returned almost nothing (login wall or empty page?)")
    return text[:MAX_CHARS]


__all__ = ["READER_PREFIX", "JinaError", "read_url"]
