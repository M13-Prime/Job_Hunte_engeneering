"""Five-criteria match rubric (A-F) + posting legitimacy checks.

Inspired by career-ops: instead of one opaque 0-100 number, the AI rates
the offer 0-5 on five named criteria; the overall score is a fixed weighted
average computed here (so it is reproducible and explainable), then mapped
to a letter grade. A legitimacy check flags postings that look stale,
generic or incomplete, with deterministic checks added on top of the AI's.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

# (key, label shown to the user, weight). Weights sum to 1.
CRITERIA: tuple[tuple[str, str, float], ...] = (
    ("role", "Poste visé", 0.30),
    ("skills", "Compétences", 0.30),
    ("seniority", "Séniorité", 0.20),
    ("location", "Lieu / pays", 0.10),
    ("timing", "Entreprise et timing", 0.10),
)
CRITERIA_LABELS = {k: label for k, label, _ in CRITERIA}

STALE_DAYS = 60
SHORT_DESCRIPTION = 300


def clean_criteria(raw: Any) -> dict[str, int] | None:
    """Keep the five known keys as ints in 0..5; None if the AI gave none."""
    if not isinstance(raw, dict):
        return None
    out: dict[str, int] = {}
    for key, _, _ in CRITERIA:
        try:
            out[key] = max(0, min(5, round(float(raw[key]))))
        except (KeyError, TypeError, ValueError):
            continue
    return out if len(out) >= 3 else None


def weighted_score(criteria: dict[str, int]) -> float:
    """0..100 from the 0..5 criteria; missing criteria are re-weighted away."""
    total_w = sum(w for k, _, w in CRITERIA if k in criteria)
    if total_w <= 0:
        return 0.0
    weighted = sum(criteria[k] / 5 * w for k, _, w in CRITERIA if k in criteria)
    return round(weighted / total_w * 100, 1)


def grade_for(score: float) -> str:
    if score >= 85:
        return "A"
    if score >= 70:
        return "B"
    if score >= 55:
        return "C"
    if score >= 40:
        return "D"
    return "F"


def legitimacy_flags(
    *, posted_at: datetime | None, description: str | None, location: str | None,
    ai_flags: Any = None, now: datetime | None = None,
) -> list[str]:
    """Short French warnings; empty list = nothing suspicious."""
    flags: list[str] = []
    now = now or datetime.now(UTC)
    if posted_at is not None:
        posted = posted_at if posted_at.tzinfo else posted_at.replace(tzinfo=UTC)
        age = (now - posted).days
        if age > STALE_DAYS:
            flags.append(f"Publiée il y a {age} jours : peut-être déjà pourvue")
    if not description or len(description.strip()) < SHORT_DESCRIPTION:
        flags.append("Description très courte ou absente")
    if not (location or "").strip():
        flags.append("Lieu non précisé")
    if isinstance(ai_flags, list):
        for f in ai_flags:
            text = str(f).strip()
            if text and text not in flags and len(flags) < 6:
                flags.append(text[:120])
    return flags


__all__ = [
    "CRITERIA", "CRITERIA_LABELS", "clean_criteria", "grade_for",
    "legitimacy_flags", "weighted_score",
]
