"""Keyword-driven search (Phase 13).

Three jobs, none of which costs a token:

1. ``KeywordMatcher`` — accent- and case-insensitive whole-word matching.
   Used as a free gate before the LLM classifier (an article that mentions
   none of the user's keywords, profile terms or config/keywords.yaml
   triggers is skipped) and for the free-text search on /results.
2. ``gdelt_queries_from_keywords`` / ``newsapi_queries_from_keywords`` —
   turn the user's saved keywords into collector queries, so a search
   actually *fetches* news about them instead of only re-ranking a fixed
   corpus.
3. ``keyword_collectors`` — the collectors above, ready to append to a run.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from urllib.parse import quote_plus

from signal_tracker.collectors.base import BaseCollector
from signal_tracker.collectors.gdelt import GdeltCollector, GdeltQuery
from signal_tracker.collectors.newsapi import NewsApiCollector, NewsApiQuery
from signal_tracker.collectors.rss import FeedConfig, RSSCollector

# Event words appended to a keyword group so GDELT returns hiring signals
# about it, not every article that mentions it.
_TITLE_TRIGGERS = (
    '(hiring OR recrute OR recrutement OR appoints OR appointed OR nomme OR "rejoint")'
)
_FIELD_TRIGGERS = (
    '(raises OR funding OR "levée de fonds" OR acquisition OR expansion OR appoints OR nomme)'
)
_MIN_TERM_LEN = 3  # GDELT rejects shorter phrases
_GROUP_SIZE = 4


def fold(text: str) -> str:
    """Casefold and strip accents: "Levée" → "levee"."""
    decomposed = unicodedata.normalize("NFKD", text or "")
    return "".join(c for c in decomposed if not unicodedata.combining(c)).casefold()


class KeywordMatcher:
    """Whole-word, accent/case-insensitive matcher over a set of terms."""

    def __init__(self, terms: Iterable[str]) -> None:
        cleaned = sorted(
            {fold(t).strip() for t in terms if t and fold(t).strip()},
            key=len, reverse=True,
        )
        self.terms = cleaned
        self._regex = (
            re.compile(
                r"(?<!\w)(" + "|".join(re.escape(t) for t in cleaned) + r")(?!\w)"
            )
            if cleaned else None
        )

    def __bool__(self) -> bool:
        return self._regex is not None

    def matches(self, text: str | None) -> bool:
        if self._regex is None:
            return True  # no terms → no filtering
        return bool(self._regex.search(fold(text or "")))

    def found(self, text: str | None) -> list[str]:
        if self._regex is None:
            return []
        return sorted({m.group(1) for m in self._regex.finditer(fold(text or ""))})


def prefilter_terms(
    user_keywords: Mapping[str, Sequence[str]],
    profile_terms: Iterable[str] = (),
    config_keywords: Mapping[str, Sequence[str]] | None = None,
) -> list[str]:
    """Every term that makes an article worth an LLM look."""
    terms: list[str] = []
    for values in user_keywords.values():
        terms.extend(values)
    terms.extend(profile_terms)
    for values in (config_keywords or {}).values():
        terms.extend(values)
    return terms


def _clean(value: str) -> str:
    return " ".join(value.replace('"', " ").split())


def _groups(values: Sequence[str]) -> list[list[str]]:
    seen: set[str] = set()
    unique: list[str] = []
    for raw in values:
        value = _clean(raw)
        key = fold(value)
        if len(value) >= _MIN_TERM_LEN and key not in seen:
            seen.add(key)
            unique.append(value)
    return [unique[i:i + _GROUP_SIZE] for i in range(0, len(unique), _GROUP_SIZE)]


def _or_clause(values: Sequence[str]) -> str:
    quoted = [f'"{v}"' for v in values]
    return quoted[0] if len(quoted) == 1 else "(" + " OR ".join(quoted) + ")"


def gdelt_queries_from_keywords(
    user_keywords: Mapping[str, Sequence[str]],
    *,
    timespan: str = "72h",
    max_records: int = 75,
    max_queries: int = 8,
) -> list[GdeltQuery]:
    """One GDELT query per group of <= 4 keywords, with event words."""
    queries: list[GdeltQuery] = []
    plan = (
        ("job_title", _TITLE_TRIGGERS),
        ("field", _FIELD_TRIGGERS),
        ("other", ""),
    )
    for category, triggers in plan:
        for i, group in enumerate(_groups(user_keywords.get(category) or [])):
            query = f"{_or_clause(group)} {triggers}".strip()
            queries.append(GdeltQuery(
                id=f"kw_{category}_{i}", query=query,
                timespan=timespan, max_records=max_records,
            ))
    return queries[:max_queries]


def newsapi_queries_from_keywords(
    user_keywords: Mapping[str, Sequence[str]],
    *,
    max_queries: int = 3,
) -> list[NewsApiQuery]:
    """NewsAPI's free tier is 100 requests/day: a few broad OR queries."""
    values: list[str] = []
    for category in ("job_title", "field", "other"):
        values.extend(user_keywords.get(category) or [])
    queries = []
    for i, group in enumerate(_groups(values)):
        queries.append(NewsApiQuery(
            id=f"kw_{i}", query=" OR ".join(f'"{v}"' for v in group), page_size=30,
        ))
    return queries[:max_queries]


# Google News editions per target country: (hl, gl, ceid) for the local
# language; English always uses the US edition (largest coverage).
_GNEWS_LOCAL: dict[str, tuple[str, str, str]] = {
    "France": ("fr", "FR", "FR:fr"), "Belgique": ("fr", "BE", "BE:fr"),
    "Suisse": ("fr", "CH", "CH:fr"), "Luxembourg": ("fr", "FR", "FR:fr"),
    "Canada": ("fr", "CA", "CA:fr"), "Allemagne": ("de", "DE", "DE:de"),
    "Espagne": ("es", "ES", "ES:es"), "Italie": ("it", "IT", "IT:it"),
    "Pays-Bas": ("nl", "NL", "NL:nl"),
}
_GNEWS_EVENTS = {
    "fr": "(recrute OR recrutement OR nomme OR nomination OR lève OR levée OR rachète "
          "OR acquisition OR lance OR implantation)",
    "en": "(hiring OR appoints OR appointed OR raises OR funding OR acquires "
          "OR acquisition OR launches OR expands)",
}


def google_news_feeds(
    user_keywords: Mapping[str, Sequence[str]],
    *,
    country: str = "France",
    languages: set[str] | None = None,
    max_keywords: int = 10,
    days: int = 7,
) -> list[FeedConfig]:
    """Google News RSS search feeds for the user's keywords (free, no key).

    One feed per keyword and language, restricted to the last `days` days
    and to hiring / leadership / funding events so the classifier gets
    relevant articles instead of every mention of the keyword.
    """
    ordered: list[str] = []
    for cat in ("job_title", "field", "other"):
        for kw in user_keywords.get(cat) or []:
            if kw and kw.strip() and kw.strip() not in ordered:
                ordered.append(kw.strip())
    editions: list[tuple[str, str, str]] = []
    local = _GNEWS_LOCAL.get(country)
    if local and (not languages or local[0] in languages):
        editions.append(local)
    if not languages or "en" in languages:
        editions.append(("en", "US", "US:en"))
    feeds: list[FeedConfig] = []
    for kw in ordered[:max_keywords]:
        for hl, gl, ceid in editions:
            events = _GNEWS_EVENTS.get(hl, _GNEWS_EVENTS["en"])
            q = quote_plus(f'"{kw}" {events} when:{days}d')
            feeds.append(FeedConfig(
                id=f"gnews_{hl}_{fold(kw).replace(' ', '_')[:40]}",
                name=f"Google News ({hl}) : {kw}",
                url=f"https://news.google.com/rss/search?q={q}&hl={hl}&gl={gl}&ceid={ceid}",
                language=hl,
            ))
    return feeds


def keyword_collectors(
    user_keywords: Mapping[str, Sequence[str]],
    *,
    languages: set[str] | None = None,
    gdelt_enabled: bool = True,
    newsapi_key: str | None = None,
    country: str = "France",
    google_news: bool = True,
) -> list[BaseCollector]:
    """Collectors that fetch news about the user's own keywords."""
    if not any(user_keywords.values()):
        return []
    collectors: list[BaseCollector] = []
    if google_news:
        feeds = google_news_feeds(user_keywords, country=country, languages=languages)
        if feeds:
            collectors.append(RSSCollector(feeds, cache_ttl_seconds=3 * 3600))
    if gdelt_enabled:
        from signal_tracker.pipeline import _augment_gdelt_queries

        gdelt = _augment_gdelt_queries(
            gdelt_queries_from_keywords(user_keywords), languages or set(),
        )
        if gdelt:
            collectors.append(GdeltCollector(gdelt))
    if newsapi_key:
        news = newsapi_queries_from_keywords(user_keywords)
        if news:
            collectors.append(NewsApiCollector(newsapi_key, news))
    return collectors


__all__ = [
    "KeywordMatcher",
    "fold",
    "gdelt_queries_from_keywords",
    "google_news_feeds",
    "keyword_collectors",
    "newsapi_queries_from_keywords",
    "prefilter_terms",
]
