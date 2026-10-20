"""Normalize company names so two spellings collapse to one entity."""

from __future__ import annotations

import re
import unicodedata
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

_SUFFIXES = (
    " sas",
    " sasu",
    " sa",
    " sarl",
    " eurl",
    " gmbh",
    " ltd",
    " llc",
    " inc",
    " plc",
    " bv",
    " ag",
    " spa",
    " srl",
)


def normalize_company_name(name: str) -> str:
    """Return a lowercase, accent-stripped, suffix-stripped form."""
    if not name:
        return ""
    # Strip accents
    nfkd = unicodedata.normalize("NFKD", name)
    no_accents = "".join(c for c in nfkd if not unicodedata.combining(c))
    cleaned = no_accents.lower().strip()
    # Collapse whitespace
    cleaned = re.sub(r"\s+", " ", cleaned)
    # Trim trailing punctuation/whitespace before *and* after suffix-stripping,
    # so "OpenAI, Inc." -> "openai, inc." -> "openai, inc" -> "openai," -> "openai".
    trailing = ".,-"
    cleaned = cleaned.rstrip(trailing + " ")
    for suffix in _SUFFIXES:
        if cleaned.endswith(suffix):
            cleaned = cleaned[: -len(suffix)].rstrip(trailing + " ")
            break
    return cleaned


# Marketing / tracking params that change between visits but point at
# the same article. Stripping them lets our dedup hash collapse
# "?utm_source=newsletter" and "?utm_source=twitter" to the same row.
_URL_TRACKING_PARAMS: frozenset[str] = frozenset({
    # Standard UTM (Google Analytics)
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "utm_id", "utm_name", "utm_brand",
    # Facebook / Instagram
    "fbclid", "fb_action_ids", "fb_action_types", "fb_source",
    # Google Ads
    "gclid", "gclsrc", "dclid", "wbraid", "gbraid",
    # Mailchimp / mail clients
    "mc_eid", "mc_cid", "mkt_tok",
    # MS Ads
    "msclkid",
    # Generic "where did the click come from" trackers
    "ref", "ref_src", "ref_url", "source", "src", "from", "trk", "trk_contact",
    # Yandex
    "yclid", "_openstat",
    # X (formerly Twitter) deeplinks
    "s",  # ?s=20 from web shares
})


def normalize_url(url: str) -> str:
    """Canonicalize a URL so two equivalent forms collapse to one hash.

    Specifically: lowercase scheme + host, strip default ports, drop
    tracking params (utm_*, fbclid, gclid, mc_eid, …), drop the
    fragment, and remove a single trailing slash on the path (unless
    the path IS just "/"). The query string is kept for non-tracking
    params, with keys sorted for stability.

    Falls back to the raw URL if parsing fails — never blocks ingest.
    """
    if not url:
        return ""
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return url

    scheme = (parts.scheme or "").lower()
    host = (parts.hostname or "").lower()
    # Strip default ports — keep non-default ones.
    port = parts.port
    if port is not None and not (
        (scheme == "http" and port == 80) or (scheme == "https" and port == 443)
    ):
        netloc = f"{host}:{port}"
    else:
        netloc = host
    # Strip user:pass@ from the netloc (rare in practice but it'd break dedup).

    # Path: drop a single trailing slash unless it's the only character.
    path = parts.path or ""
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")

    # Query: drop tracking params, sort the rest for stability.
    if parts.query:
        kept = [
            (k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
            if k.lower() not in _URL_TRACKING_PARAMS
        ]
        kept.sort()
        query = urlencode(kept, doseq=True)
    else:
        query = ""

    # Fragment is always client-side state, never identifies the resource.
    return urlunsplit((scheme, netloc, path, query, ""))


__all__ = ["normalize_company_name", "normalize_url"]
