"""Deduplication helpers (SHA256 hashing of source+url)."""

from __future__ import annotations

import hashlib


def raw_item_hash(source: str, url: str) -> str:
    """SHA256 hash used as the dedup key for ``raw_items``."""
    payload = f"{source}\n{url}".encode()
    return hashlib.sha256(payload).hexdigest()


def signal_dedup_key(company_normalized: str, signal_type: str, week_bucket: str) -> str:
    """
    Coarse dedup key for signals: same company + same signal_type within a
    7-day window collapses to one entry.

    ``week_bucket`` should be an ISO week string like "2026-W19".
    """
    return f"{company_normalized}|{signal_type}|{week_bucket}"
