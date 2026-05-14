"""Normalize company names so two spellings collapse to one entity."""

from __future__ import annotations

import re
import unicodedata

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
