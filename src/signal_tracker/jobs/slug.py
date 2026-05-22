"""Generate plausible ATS slugs from a company name."""

from __future__ import annotations

import re
import unicodedata

from signal_tracker.utils.normalize import normalize_company_name


def slug_candidates(company_name: str) -> list[str]:
    """Return slug variations to try across ATS APIs.

    Greenhouse / Lever / Workable / Ashby all expose a company slug in their
    board URL (e.g. boards.greenhouse.io/<slug>). The slug is usually
    lowercase, ASCII-only, and joins the words with either a hyphen, an
    underscore, or no separator. We generate the most likely variants.
    """
    if not company_name:
        return []
    base = normalize_company_name(company_name)
    nfkd = unicodedata.normalize("NFKD", base)
    ascii_only = "".join(c for c in nfkd if not unicodedata.combining(c))
    cleaned = re.sub(r"[^a-z0-9\s\-_]", "", ascii_only.lower())
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if not cleaned:
        return []

    parts = cleaned.split(" ")
    variants = [
        cleaned.replace(" ", "-"),
        cleaned.replace(" ", ""),
        cleaned.replace(" ", "_"),
    ]
    if len(parts) > 1:
        # Sometimes the slug is only the first word (e.g. "carbone" for "carbone 4")
        variants.append(parts[0])

    seen: set[str] = set()
    out: list[str] = []
    for v in variants:
        if v and v not in seen:
            seen.add(v)
            out.append(v)
    return out


__all__ = ["slug_candidates"]
