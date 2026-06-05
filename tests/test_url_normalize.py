"""Tests for normalize_url (Phase 11.2 URL canonicalization)."""

from __future__ import annotations

from signal_tracker.utils.dedup import raw_item_hash
from signal_tracker.utils.normalize import normalize_url


def test_utm_params_stripped() -> None:
    a = normalize_url("https://example.com/article?utm_source=newsletter&utm_medium=email")
    b = normalize_url("https://example.com/article?utm_source=twitter")
    assert a == b == "https://example.com/article"


def test_facebook_and_google_click_ids_stripped() -> None:
    assert normalize_url("https://x.com/a?fbclid=ABC") == "https://x.com/a"
    assert normalize_url("https://x.com/a?gclid=XYZ") == "https://x.com/a"


def test_real_params_preserved_and_sorted() -> None:
    # Article id should stay; param order should be normalized.
    a = normalize_url("https://news.fr/article?id=42&page=3")
    b = normalize_url("https://news.fr/article?page=3&id=42")
    assert a == b == "https://news.fr/article?id=42&page=3"


def test_fragment_dropped() -> None:
    assert normalize_url("https://x.com/a#section") == "https://x.com/a"


def test_host_lowercased() -> None:
    assert normalize_url("https://EXAMPLE.com/a") == "https://example.com/a"
    assert normalize_url("https://Example.COM/a") == "https://example.com/a"


def test_trailing_slash_removed_except_root() -> None:
    assert normalize_url("https://x.com/a/") == "https://x.com/a"
    # Root path is kept as-is.
    assert normalize_url("https://x.com/") == "https://x.com/"


def test_default_ports_stripped() -> None:
    assert normalize_url("https://x.com:443/a") == "https://x.com/a"
    assert normalize_url("http://x.com:80/a") == "http://x.com/a"
    # Non-default ports stay.
    assert normalize_url("http://x.com:8080/a") == "http://x.com:8080/a"


def test_empty_or_malformed_returns_empty_or_input() -> None:
    assert normalize_url("") == ""
    # Malformed URL must not raise — keeps ingest robust.
    out = normalize_url("not a url")
    assert isinstance(out, str)


def test_raw_item_hash_collapses_utm_variants() -> None:
    """Two visits to the same article with different UTM tags must
    produce the same dedup hash — that's the whole point of the
    Phase 11.2 normalization."""
    h1 = raw_item_hash("rss", "https://news.fr/a?utm_source=tw")
    h2 = raw_item_hash("rss", "https://news.fr/a?utm_source=fb")
    h3 = raw_item_hash("rss", "https://news.fr/a")
    assert h1 == h2 == h3


def test_raw_item_hash_differs_for_different_sources() -> None:
    # Same URL from a different source is still a distinct row
    # (source-tagged dedup, intentional).
    h_rss = raw_item_hash("rss", "https://news.fr/a")
    h_gdelt = raw_item_hash("gdelt", "https://news.fr/a")
    assert h_rss != h_gdelt
