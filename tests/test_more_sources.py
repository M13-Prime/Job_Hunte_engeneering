"""Google News keyword feeds + collection languages per target country."""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

from signal_tracker.collectors.rss import RSSCollector
from signal_tracker.utils.geo import collection_languages
from signal_tracker.utils.keywords import google_news_feeds, keyword_collectors


def test_collection_keeps_english_next_to_local_language() -> None:
    assert collection_languages("France") == {"fr", "en"}
    assert collection_languages("Allemagne") == {"de", "en"}
    assert collection_languages("Monde") == set()   # open target: no filter


def test_google_news_feeds_per_keyword_and_language() -> None:
    feeds = google_news_feeds(
        {"job_title": ["Sustainability Manager"], "field": ["ESG", "AI"]},
        country="France", languages={"fr", "en"},
    )
    assert len(feeds) == 6                           # 3 keywords x (fr, en)
    first = feeds[0]
    assert first.language == "fr"
    q = parse_qs(urlparse(first.url).query)
    assert q["ceid"] == ["FR:fr"] and q["hl"] == ["fr"]
    assert '"Sustainability Manager"' in q["q"][0] and "when:7d" in q["q"][0]
    assert "recrute" in q["q"][0]
    assert any(f.language == "en" and "ceid=US%3Aen" in f.url for f in feeds)


def test_google_news_respects_max_keywords_and_language_filter() -> None:
    kws = {"field": [f"kw{i}" for i in range(15)]}
    assert len(google_news_feeds(kws, languages={"fr"})) == 10   # fr only, capped
    assert google_news_feeds({}, languages={"fr"}) == []


def test_keyword_collectors_include_google_news() -> None:
    cols = keyword_collectors(
        {"field": ["ESG"]}, languages={"fr", "en"}, gdelt_enabled=False, country="France",
    )
    assert any(isinstance(c, RSSCollector) for c in cols)
    assert keyword_collectors({}, languages={"fr"}) == []
