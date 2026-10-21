"""Offer filters: country, keywords (AND / OR) and years of experience.

All deterministic and free (no LLM call): they run on the offer title,
location and description that are already stored.

- Country: the offer's location is matched against known names and big
  cities. A clear mismatch is hidden; an unknown or remote location is
  kept (and can be hidden with ``include_unknown=False``).
- Keywords: every selected keyword must appear (AND) or at least one
  (OR). A few synonyms are built in ("AI" also matches "intelligence
  artificielle", "sustainability" also matches "RSE", "ESG"...).
- Experience: the years asked by the posting are parsed ("5-10 ans",
  "3+ years of experience") and compared to the range the user targets.
  Executive titles (director, VP, head of...) are hidden for profiles
  under 12 years, junior titles (stage, junior...) for profiles of 4+.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass, field


def _norm(text: str | None) -> str:
    """Lowercase + strip accents so "Île-de-France" matches "ile-de-france"."""
    raw = unicodedata.normalize("NFKD", (text or "").lower())
    return "".join(c for c in raw if not unicodedata.combining(c))


# ---------------------------------------------------------------------------
# Country
# ---------------------------------------------------------------------------

COUNTRY_ALIASES: dict[str, tuple[str, ...]] = {
    "France": (
        "france", "paris", "lyon", "marseille", "toulouse", "nantes", "bordeaux",
        "lille", "rennes", "strasbourg", "montpellier", "nice", "grenoble",
        "ile-de-france", "sophia antipolis", "boulogne-billancourt", "la defense",
        "puteaux", "levallois", "saint-denis", "nanterre", "issy-les-moulineaux",
    ),
    "Belgique": ("belgique", "belgium", "belgie", "bruxelles", "brussels", "anvers", "antwerp", "gand", "ghent", "liege"),
    "Suisse": ("suisse", "switzerland", "schweiz", "zurich", "geneve", "geneva", "lausanne", "bale", "basel", "zug"),
    "Luxembourg": ("luxembourg",),
    "Canada": ("canada", "toronto", "montreal", "vancouver", "ottawa", "calgary", "quebec"),
    "Royaume-Uni": ("royaume-uni", "united kingdom", "uk", "england", "scotland", "london", "londres", "manchester", "edinburgh", "birmingham"),
    "États-Unis": (
        "etats-unis", "united states", "usa", "u.s.", "new york", "san francisco",
        "austin", "boston", "seattle", "chicago", "los angeles", "washington",
        "california", "texas", "denver", "atlanta", "miami",
    ),
    "Allemagne": ("allemagne", "germany", "deutschland", "berlin", "munich", "munchen", "hamburg", "frankfurt", "cologne", "koln", "stuttgart"),
    "Espagne": ("espagne", "spain", "espana", "madrid", "barcelona", "barcelone", "valencia", "seville"),
    "Italie": ("italie", "italy", "italia", "milan", "milano", "rome", "roma", "turin", "torino"),
    "Pays-Bas": ("pays-bas", "netherlands", "nederland", "amsterdam", "rotterdam", "utrecht", "eindhoven", "the hague"),
    "Irlande": ("irlande", "ireland", "dublin", "cork"),
    "Portugal": ("portugal", "lisbon", "lisbonne", "porto"),
    "Pologne": ("pologne", "poland", "warsaw", "varsovie", "krakow", "cracovie"),
    "Inde": ("inde", "india", "bangalore", "bengaluru", "mumbai", "delhi", "hyderabad", "pune"),
    "Singapour": ("singapour", "singapore",),
}

# Locations that say nothing about a country.
_OPEN_LOCATION = re.compile(r"\b(remote|teletravail|anywhere|worldwide|global|emea|europe|hybrid)\b")


def _word_in(alias: str, text: str) -> bool:
    return re.search(r"(?<![a-z])" + re.escape(alias) + r"(?![a-z])", text) is not None


_US_STATES = ["AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY", "DC"]
_CA_PROVINCES = ["AB", "BC", "MB", "NB", "NL", "NS", "NT", "NU", "ON", "PE", "QC", "SK", "YT"]
_STATE_SUFFIX = re.compile(r",\s*([A-Z]{2})(?:\s|,|$|\))")


def _country_from_code(location: str) -> str | None:
    """'Pittsburgh, PA' -> États-Unis, 'Toronto, ON' -> Canada (uppercase codes only)."""
    for code in _STATE_SUFFIX.findall(location or ""):
        if code in _US_STATES:
            return "États-Unis"
        if code in _CA_PROVINCES:
            return "Canada"
    return None


def location_matches_country(location: str | None, country: str) -> bool | None:
    """True = in that country, False = clearly elsewhere, None = unknown/open."""
    if country in ("", "Monde", "Europe"):
        return None
    loc = _norm(location)
    if not loc.strip():
        return None
    coded = _country_from_code(location or "")
    if coded is not None:
        return coded == country
    target = COUNTRY_ALIASES.get(country, (_norm(country),))
    if any(_word_in(a, loc) for a in target):
        return True
    for other, aliases in COUNTRY_ALIASES.items():
        if other != country and any(_word_in(a, loc) for a in aliases):
            return False
    return None


# ---------------------------------------------------------------------------
# Keywords
# ---------------------------------------------------------------------------

_SYNONYMS: dict[str, tuple[str, ...]] = {
    "ai": ("ai", "ia", "artificial intelligence", "intelligence artificielle", "machine learning", "genai", "llm", "generative"),
    "ia": ("ai", "ia", "artificial intelligence", "intelligence artificielle", "machine learning", "genai", "llm"),
    "sustainability": ("sustainability", "sustainable", "durabilite", "durable", "rse", "csr", "esg", "developpement durable", "climate", "climat"),
    "durabilite": ("sustainability", "durabilite", "rse", "csr", "esg", "developpement durable"),
    "rse": ("rse", "csr", "esg", "sustainability", "durabilite"),
    "csr": ("rse", "csr", "esg", "sustainability", "durabilite"),
    "esg": ("esg", "rse", "csr", "sustainability", "durabilite"),
    "climate": ("climate", "climat", "carbon", "carbone", "decarbonation", "net zero"),
    "data": ("data", "donnees", "analytics"),
}


def _term_regex(term: str) -> re.Pattern[str]:
    # Longer terms accept plural/derived endings ("sustainab" -> "sustainability").
    suffix = r"[a-z]*" if len(term) > 3 else ""
    return re.compile(r"(?<![a-z])" + re.escape(term) + suffix + r"(?![a-z])")


def keyword_in_text(keyword: str, text_norm: str) -> bool:
    key = _norm(keyword).strip()
    if not key:
        return True
    variants = {key, *_SYNONYMS.get(key, ())}
    return any(_term_regex(v).search(text_norm) for v in variants)


def keywords_match(keywords: Iterable[str], text: str, *, mode: str = "all") -> bool:
    kws = [k for k in keywords if k and k.strip()]
    if not kws:
        return True
    norm = _norm(text)
    hits = [keyword_in_text(k, norm) for k in kws]
    return any(hits) if mode == "any" else all(hits)


# ---------------------------------------------------------------------------
# Title relevance (is this job title related to the user's profile at all?)
# ---------------------------------------------------------------------------

# Words that say nothing about the field: "Manager" alone must not make
# "IT Desktop Support Manager" look relevant to a sustainability profile.
_GENERIC_TITLE_WORDS = {
    "manager", "management", "lead", "leader", "senior", "junior", "head", "director",
    "directeur", "directrice", "chief", "officer", "specialist", "specialiste",
    "associate", "principal", "staff", "intern", "stagiaire", "consultant",
    "consultante", "responsable", "charge", "chargee", "chef", "expert", "experte",
    "analyst", "analyste", "engineer", "ingenieur", "ingenieure", "coordinator",
    "coordinateur", "coordinatrice", "assistant", "assistante", "project", "projet",
    "projets", "program", "programme", "business", "global", "international",
    "team", "equipe", "group", "groupe", "de", "des", "du", "la", "le", "les", "et",
    "and", "of", "the", "in", "en", "pour", "for", "to", "a", "au", "aux", "sur",
    "with", "avec", "h", "f", "hf", "fh", "m", "w", "remote", "hybrid", "cdi", "cdd",
}


def profile_title_terms(phrases: Iterable[str]) -> list[str]:
    """Distinctive words of the user's keywords / target roles."""
    out: list[str] = []
    seen: set[str] = set()
    for phrase in phrases:
        for tok in re.split(r"[^a-z0-9+#]+", _norm(phrase)):
            if not tok or tok in _GENERIC_TITLE_WORDS or tok in seen:
                continue
            if len(tok) < 3 and tok not in ("ai", "ia", "bi", "ml", "rh", "hr", "it"):
                continue
            seen.add(tok)
            out.append(tok)
    return out


def title_relevant(title: str | None, terms: Iterable[str]) -> bool:
    """True when the title contains at least one profile term (synonyms included)."""
    term_list = list(terms)
    if not term_list:
        return True
    norm = _norm(title)
    return any(keyword_in_text(t, norm) for t in term_list)


# ---------------------------------------------------------------------------
# Experience
# ---------------------------------------------------------------------------

_YEARS = re.compile(
    r"(\d{1,2})\s*(?:(?:\+)|(?:[-–]|à|a|to|et)\s*(\d{1,2}))?\s*\+?\s*"
    r"(?:years?|yrs?|ans|annees)\b"
)
_EXEC_TITLE = re.compile(
    r"(?<![a-z])(director|directeur|directrice|vp|vice[- ]president|head of|chief|"
    r"cto|ceo|coo|cfo|cso|c-level|managing director|general manager|directeur general)(?![a-z])"
)
_JUNIOR_TITLE = re.compile(
    r"(?<![a-z])(junior|jr|intern|internship|stagiaire|stage|alternance|apprenti|"
    r"apprentice|graduate|trainee|entry[- ]level|debutant)(?![a-z])"
)


def parse_years_required(text: str | None) -> tuple[int, int | None] | None:
    """Years of experience asked by a posting: (min, max-or-None), or None."""
    norm = _norm(text)[:8000]
    for m in _YEARS.finditer(norm):
        window = norm[max(0, m.start() - 70): m.end() + 70]
        if "exp" not in window:
            continue  # "founded 10 years ago", "a 5-year plan"...
        lo = int(m.group(1))
        hi = int(m.group(2)) if m.group(2) else None
        if hi is not None and hi < lo:
            lo, hi = hi, lo
        if lo > 30:
            continue
        return lo, hi
    return None


def is_exec_title(title: str | None) -> bool:
    return _EXEC_TITLE.search(_norm(title)) is not None


def is_junior_title(title: str | None) -> bool:
    return _JUNIOR_TITLE.search(_norm(title)) is not None


# ---------------------------------------------------------------------------
# Combined verdict
# ---------------------------------------------------------------------------


@dataclass
class JobFilter:
    country: str = ""                 # "" = no country filter
    include_unknown: bool = True      # keep offers whose location is unknown/remote
    keywords: list[str] = field(default_factory=list)
    keyword_mode: str = "all"         # "all" (AND) | "any" (OR)
    exp_min: int | None = None
    exp_max: int | None = None
    # Title must contain one of these profile terms (empty = no check).
    title_terms: list[str] = field(default_factory=list)
    # An AI match score at or above this keeps the offer even if the title
    # does not contain a profile term (the AI read the whole posting).
    title_bypass_score: float = 60.0


def rejection_reason(
    *, title: str, location: str | None, department: str | None,
    description: str | None, flt: JobFilter, ai_score: float | None = None,
) -> str | None:
    """None when the offer passes; otherwise a short code for why it was hidden."""
    if flt.country:
        verdict = location_matches_country(location, flt.country)
        if verdict is False or (verdict is None and not flt.include_unknown):
            return "country"
    if flt.title_terms and not title_relevant(title, flt.title_terms) and (
        ai_score is None or ai_score < flt.title_bypass_score
    ):
        return "off_profile"
    if flt.keywords:
        blob = " ".join([title or "", department or "", description or ""])
        if not keywords_match(flt.keywords, blob, mode=flt.keyword_mode):
            return "keywords"
    if flt.exp_max is not None and is_exec_title(title) and flt.exp_max < 12:
        return "too_senior"
    if flt.exp_min is not None and flt.exp_min >= 4 and is_junior_title(title):
        return "too_junior"
    req = parse_years_required(description)
    if req is not None:
        lo, hi = req
        if flt.exp_max is not None and lo > flt.exp_max:
            return "too_senior"
        if flt.exp_min is not None:
            ceiling = hi if hi is not None else lo + 2
            if ceiling < flt.exp_min:
                return "too_junior"
    return None


def user_default_filter(user: object | None) -> JobFilter:
    """The saved preferences of a User row (country + experience range)."""
    country = getattr(user, "country", "") or ""
    return JobFilter(
        country="" if country in ("Europe", "Monde") else country,
        exp_min=getattr(user, "exp_min", None),
        exp_max=getattr(user, "exp_max", None),
    )


def offer_passes(job: object, flt: JobFilter) -> bool:
    return rejection_reason(
        title=str(getattr(job, "title", "") or ""),
        location=getattr(job, "location", None),
        department=getattr(job, "department", None),
        description=getattr(job, "description", None),
        flt=flt,
    ) is None


REASON_LABELS = {
    "country": "hors du pays choisi",
    "keywords": "ne contiennent pas tes mots-clés",
    "too_senior": "trop seniors pour ton expérience",
    "too_junior": "trop juniors pour ton expérience",
    "off_profile": "sans rapport avec ton profil",
    "ghost": "possiblement fantômes",
}

__all__ = [
    "COUNTRY_ALIASES",
    "REASON_LABELS",
    "JobFilter",
    "is_exec_title",
    "is_junior_title",
    "keyword_in_text",
    "keywords_match",
    "location_matches_country",
    "offer_passes",
    "parse_years_required",
    "rejection_reason",
    "user_default_filter",
]
