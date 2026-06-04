"""Country → language mapping for the pre-collect filter (Phase 11.2).

The classifier already extracts hq_country / active_countries on the
output side, and /results filters by country. That's too late: by the
time we filter, we've already scraped GDELT and ~20 RSS feeds and run
the LLM classifier on the lot. This module lets us drop entire feeds
and tighten GDELT queries BEFORE the network round-trips happen.

The mapping is deliberately coarse — we don't need geolinguistic
precision, just enough to skip an English-only feed when the user is
hunting strictly in France.
"""

from __future__ import annotations

# Canonical FR labels used by UserProfile.geographies → set of ISO-639
# language codes that dominate news output for those regions. Multi-
# language regions list both languages.
_COUNTRY_TO_LANGUAGES: dict[str, set[str]] = {
    # French-speaking
    "france": {"fr"},
    "belgique": {"fr", "nl"},
    "belgium": {"fr", "nl"},
    "suisse": {"fr", "de", "it"},
    "switzerland": {"fr", "de", "it"},
    "luxembourg": {"fr", "de"},
    "quebec": {"fr", "en"},
    "québec": {"fr", "en"},
    "canada": {"en", "fr"},
    "monaco": {"fr"},
    # English-speaking
    "uk": {"en"},
    "united kingdom": {"en"},
    "royaume-uni": {"en"},
    "united states": {"en"},
    "etats-unis": {"en"},
    "états-unis": {"en"},
    "usa": {"en"},
    "us": {"en"},
    "ireland": {"en"},
    "irlande": {"en"},
    "australia": {"en"},
    "australie": {"en"},
    "new zealand": {"en"},
    # German
    "germany": {"de"},
    "allemagne": {"de"},
    "austria": {"de"},
    "autriche": {"de"},
    # Other big markets — minimal coverage, mostly here so the matcher
    # doesn't accidentally treat them as "unknown" and fall back to "no
    # filter".
    "spain": {"es"},
    "espagne": {"es"},
    "italy": {"it"},
    "italie": {"it"},
    "portugal": {"pt"},
    "netherlands": {"nl"},
    "pays-bas": {"nl"},
}

# When the user says "Europe" or "global" we deliberately do NOT
# restrict by language — these are catch-all geographies that should
# behave like "no filter".
_OPEN_LABELS: set[str] = {
    "europe", "global", "international", "monde", "world",
    "remote", "any", "tous", "toutes",
}

# GDELT 2.0 takes language as the long English name ("french",
# "english"…), not an ISO code.
_GDELT_LANGUAGE_FULL = {
    "fr": "french", "en": "english", "de": "german",
    "es": "spanish", "it": "italian", "pt": "portuguese",
    "nl": "dutch",
}


def geographies_to_languages(geographies: list[str]) -> set[str]:
    """Map a UserProfile.geographies list to the ISO-639 languages we
    keep at collect time.

    Behavior:
    - Unknown labels are silently ignored (don't constrain the filter).
    - An open label (Europe / Global / Monde / ...) returns an empty
      set, which the caller interprets as "no filter at all" — better
      to over-collect than to drop a relevant article on a vague intent.
    - The result is a UNION across all labels: someone targeting
      "France" + "UK" gets {fr, en}.
    """
    if not geographies:
        return set()
    langs: set[str] = set()
    saw_known = False
    for raw in geographies:
        key = raw.strip().lower()
        if not key:
            continue
        if key in _OPEN_LABELS:
            return set()  # explicit open intent → no filter
        if key in _COUNTRY_TO_LANGUAGES:
            langs |= _COUNTRY_TO_LANGUAGES[key]
            saw_known = True
    # If the user only listed unknown labels we're conservative and
    # don't restrict (they probably typed a city or a sector we don't
    # have in the table — over-collecting beats missing signals).
    return langs if saw_known else set()


def languages_to_gdelt_clause(languages: set[str]) -> str:
    """Build a GDELT 2.0 `sourcelang:` clause for a set of languages.

    Returns an empty string when there's nothing to filter on (so the
    caller can `f"{base} {clause}".strip()` without thinking).

    One language → `sourcelang:french`. Multiple → wrapped in OR:
    `(sourcelang:french OR sourcelang:english)`.
    """
    names = sorted({
        _GDELT_LANGUAGE_FULL[lang]
        for lang in languages if lang in _GDELT_LANGUAGE_FULL
    })
    if not names:
        return ""
    if len(names) == 1:
        return f"sourcelang:{names[0]}"
    inner = " OR ".join(f"sourcelang:{n}" for n in names)
    return f"({inner})"


__all__ = ["geographies_to_languages", "languages_to_gdelt_clause"]
