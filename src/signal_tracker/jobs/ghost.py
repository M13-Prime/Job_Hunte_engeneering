"""Ghost-job detection from the offer history (no AI call, no extra request).

A "ghost job" is an ad that stays online without a real intent to hire
soon: evergreen pipeline ads, ads recycled again and again, or ads kept up
while the company is cutting jobs. We can spot that from what the tool
already stores:

- open for a long time: first seen by the tool long ago and still open;
- recycled: the same title at the same company was published, removed,
  then published again;
- duplicated: several identical ads open at once (CV collection);
- contradicted: a recent signal on the company mentions layoffs or a
  hiring freeze.

These are hints, not proof: the UI says "à vérifier", never "fausse offre".
"""

from __future__ import annotations

import re
import unicodedata
from collections import defaultdict
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from signal_tracker.storage.models import JobOffer, Signal

LONG_OPEN_DAYS = 45
DUPLICATE_MIN = 3
LAYOFF_LOOKBACK_DAYS = 120
_LAYOFF_PATTERNS = (
    "%licenci%", "%plan social%", "%suppression%poste%", "%suppressions d'emploi%",
    "%layoff%", "%job cuts%", "%gel des embauches%", "%hiring freeze%",
    "%restructuration%", "%plan de départs%", "%rupture conventionnelle collective%",
)


def title_key(title: str | None) -> str:
    """Normalise a title so reposts match: accents, case, (H/F), punctuation."""
    raw = unicodedata.normalize("NFKD", (title or "").lower())
    text = "".join(c for c in raw if not unicodedata.combining(c))
    text = re.sub(r"\((?:h|f|m|w|x|d)\s*/\s*(?:h|f|m|w|x|d)(?:\s*/\s*\w)?\)", " ", text)
    text = re.sub(r"\b(?:h|f)\s*/\s*(?:h|f)\b|\bm/f/d\b|\bm/w/d\b", " ", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


def _naive(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt.astimezone(UTC).replace(tzinfo=None) if dt.tzinfo else dt


def ghost_flags(
    session: Session, jobs: Iterable[JobOffer], *, now: datetime | None = None,
) -> dict[int, list[str]]:
    """job id -> French warnings (empty list = nothing suspicious)."""
    job_list = list(jobs)
    out: dict[int, list[str]] = {j.id: [] for j in job_list}
    if not job_list:
        return out
    now_naive = _naive(now or datetime.now(UTC)) or datetime.now(UTC).replace(tzinfo=None)
    companies = sorted({j.company_normalized for j in job_list})

    # History of every offer of these companies (open and closed).
    history: dict[tuple[str, str], list[tuple[int, bool]]] = defaultdict(list)
    for jid, company, title, is_open in session.execute(
        select(JobOffer.id, JobOffer.company_normalized, JobOffer.title, JobOffer.is_open)
        .where(JobOffer.company_normalized.in_(companies))
    ).all():
        history[(company, title_key(title))].append((int(jid), bool(is_open)))

    cutoff = now_naive - timedelta(days=LAYOFF_LOOKBACK_DAYS)
    layoff_companies = {
        c for (c,) in session.execute(
            select(Signal.company_normalized)
            .where(Signal.company_normalized.in_(companies))
            .where(Signal.created_at >= cutoff)
            .where(or_(*[Signal.summary_fr.ilike(p) for p in _LAYOFF_PATTERNS]))
            .distinct()
        ).all()
    }

    for job in job_list:
        flags = out[job.id]
        first_seen = _naive(job.collected_at)
        if job.is_open and first_seen is not None:
            days = (now_naive - first_seen).days
            if days >= LONG_OPEN_DAYS:
                flags.append(f"Toujours en ligne {days} jours après sa première détection")
        same = history.get((job.company_normalized, title_key(job.title)), [])
        closed_before = sum(1 for jid, is_open in same if not is_open and jid != job.id)
        if closed_before:
            flags.append(
                f"Même intitulé déjà publié puis retiré {closed_before} fois (annonce recyclée)"
            )
        open_copies = sum(1 for _, is_open in same if is_open)
        if open_copies >= DUPLICATE_MIN:
            flags.append(
                f"{open_copies} annonces identiques ouvertes en même temps (vivier de CV ?)"
            )
        if job.company_normalized in layoff_companies:
            flags.append("Licenciements ou gel des embauches évoqués récemment dans les signaux")
    return out


__all__ = ["DUPLICATE_MIN", "LONG_OPEN_DAYS", "ghost_flags", "title_key"]
