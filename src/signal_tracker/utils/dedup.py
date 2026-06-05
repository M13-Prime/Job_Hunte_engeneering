"""Deduplication helpers (SHA256 hashing of source+url)."""

from __future__ import annotations

import hashlib

from signal_tracker.utils.normalize import normalize_url


def raw_item_hash(source: str, url: str) -> str:
    """SHA256 hash used as the dedup key for ``raw_items``.

    Phase 11.2 — the URL is canonicalized first (lowercase host, strip
    tracking params, drop fragment) so two visits to the same article
    from different campaigns collapse to one row instead of being
    classified twice. Existing rows in the DB keep their old hash; new
    ingests use the canonical form, so any future re-ingest of an
    already-known article (under any variant URL) will dedup against
    the canonical hash.
    """
    canonical = normalize_url(url)
    payload = f"{source}\n{canonical}".encode()
    return hashlib.sha256(payload).hexdigest()


def signal_dedup_key(company_normalized: str, signal_type: str, week_bucket: str) -> str:
    """
    Coarse dedup key for signals: same company + same signal_type within a
    7-day window collapses to one entry.

    ``week_bucket`` should be an ISO week string like "2026-W19".
    """
    return f"{company_normalized}|{signal_type}|{week_bucket}"
