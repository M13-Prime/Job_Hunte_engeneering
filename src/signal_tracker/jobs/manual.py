"""Ingest one job posting from an arbitrary URL ("score this link").

Borrowed from kevinpz/claude-job-search: anything the ATS backends don't
cover (LinkedIn, Welcome to the Jungle, an in-house careers page) can still
enter the pipeline by hand. The page is read through Jina Reader
(utils/jina.py), stored as a normal JobOffer with ats="manual", scored by
the deterministic prefilter, and then picked up by the usual AI scoring.

Title and company are guessed from the markdown; the heuristics are weak by
nature, so callers should pass ``title`` / ``company`` when they know them
(that is what scripts/add_job.py asks for).
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select

from signal_tracker.config import UserProfile
from signal_tracker.jobs.relevance import dealbreaker_hit, score_job
from signal_tracker.jobs.schemas import JobPosting
from signal_tracker.storage import Database
from signal_tracker.storage.models import JobOffer
from signal_tracker.utils.jina import read_url
from signal_tracker.utils.logging import get_logger
from signal_tracker.utils.normalize import normalize_company_name

logger = get_logger(__name__)

_TITLE_LINE = re.compile(r"^Title:\s*(.+)$", re.MULTILINE)
_H1 = re.compile(r"^#\s+(.+)$", re.MULTILINE)
# "Senior Data Engineer - Acme" / "... chez Acme" / "... at Acme"
_TITLE_SPLIT = re.compile(
    "\\s+(?:[-\u2013\u2014|\u00b7]|chez|at)\\s+", re.IGNORECASE
)


@dataclass(slots=True)
class ManualJob:
    job_offer_id: int
    title: str
    company: str
    relevance_score: float
    matched: list[str]
    dealbreaker: str | None
    created: bool


def guess_title_and_company(markdown: str) -> tuple[str | None, str | None]:
    """Best-effort (title, company) from a Jina Reader page."""
    headline = None
    for pattern in (_TITLE_LINE, _H1):
        match = pattern.search(markdown)
        if match:
            headline = match.group(1).strip()
            break
    if not headline:
        return None, None
    parts = [p.strip() for p in _TITLE_SPLIT.split(headline) if p.strip()]
    if len(parts) >= 2:
        return parts[0], parts[1]
    return headline, None


async def add_job_from_url(
    db: Database,
    url: str,
    profile: UserProfile,
    *,
    title: str | None = None,
    company: str | None = None,
    location: str | None = None,
) -> ManualJob:
    """Read ``url``, store it as a manual JobOffer and score it."""
    markdown = await read_url(url)
    guessed_title, guessed_company = guess_title_and_company(markdown)
    final_title = (title or guessed_title or "").strip()
    final_company = (company or guessed_company or "").strip()
    if not final_title or not final_company:
        raise ValueError(
            "could not determine title and company from the page — "
            "pass them explicitly"
        )

    normalized = normalize_company_name(final_company)
    external_id = hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]
    dedup = f"{normalized}|manual|{external_id}"

    posting = JobPosting(
        ats="manual",
        ats_company_slug="manual",
        external_id=external_id,
        title=final_title,
        url=url,
        location=location,
        description=markdown,
    )
    score, matched = score_job(posting, profile)
    blocker = dealbreaker_hit(posting, profile)

    with db.session() as session:
        existing = session.execute(
            select(JobOffer).where(JobOffer.dedup_key == dedup)
        ).scalar_one_or_none()
        if existing is not None:
            existing.description = markdown
            existing.relevance_score = score
            existing.matched_roles = matched
            existing.is_open = True
            session.flush()
            offer_id, created = existing.id, False
        else:
            offer = JobOffer(
                company_normalized=normalized,
                company_name=final_company,
                ats="manual",
                ats_company_slug="manual",
                external_id=external_id,
                title=final_title,
                url=url,
                location=location,
                description=markdown,
                posted_at=datetime.now(UTC),
                relevance_score=score,
                matched_roles=matched,
                dedup_key=dedup,
            )
            session.add(offer)
            session.flush()
            offer_id, created = offer.id, True

    logger.info(
        "manual.job_ingested url=%s score=%.1f created=%s dealbreaker=%r",
        url, score, created, blocker,
    )
    return ManualJob(
        job_offer_id=offer_id, title=final_title, company=final_company,
        relevance_score=score, matched=matched, dealbreaker=blocker,
        created=created,
    )


__all__ = ["ManualJob", "add_job_from_url", "guess_title_and_company"]
