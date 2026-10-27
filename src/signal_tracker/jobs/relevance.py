"""Score a job posting against the user profile (keyword-based, deterministic).

We deliberately keep this LLM-free: jobs scraping runs after the classifier,
fanning out to N companies / M jobs each. Running Claude on every posting
would explode the budget. The deterministic score is enough to surface the
right jobs in the digest; the user reads them.

Scoring scheme (capped at 100):
  dealbreaker match  -> 0, nothing else is considered
  +30 per full target_role match (all words of the role appear, any order)
  +15 per core skill
  +10 per domain keyword
  +5  per bonus skill
"""

from __future__ import annotations

from signal_tracker.config import UserProfile
from signal_tracker.jobs.filters import keyword_in_text
from signal_tracker.jobs.schemas import JobPosting
from signal_tracker.utils.logging import get_logger

logger = get_logger(__name__)

ROLE_POINTS = 30
CORE_SKILL_POINTS = 15
DOMAIN_POINTS = 10
BONUS_SKILL_POINTS = 5


def _haystack(job: JobPosting) -> str:
    parts = [job.title or "", job.department or "", job.description or ""]
    return " ".join(parts).lower()


def dealbreaker_hit(job: JobPosting, profile: UserProfile) -> str | None:
    """First dealbreaker found in the posting, or None."""
    if not profile.dealbreakers:
        return None
    haystack = _haystack(job)
    for term in profile.dealbreakers:
        if term and keyword_in_text(term, haystack):
            return term
    return None


def score_job(job: JobPosting, profile: UserProfile) -> tuple[float, list[str]]:
    """Return (score 0-100, list of matched role / skill labels).

    A dealbreaker short-circuits to (0.0, []) so the offer stays below the
    heuristic floor and is never sent to a paid scoring call.
    """
    haystack = _haystack(job)
    if not haystack.strip():
        return 0.0, []

    blocker = dealbreaker_hit(job, profile)
    if blocker is not None:
        logger.info("relevance.dealbreaker title=%r term=%r", job.title, blocker)
        return 0.0, []

    score = 0
    matched: list[str] = []
    for role in profile.target_roles:
        words = [w for w in role.lower().split() if len(w) > 1]
        if words and all(w in haystack for w in words):
            score += ROLE_POINTS
            matched.append(role)
    for skill in profile.core_skills:
        if skill and keyword_in_text(skill, haystack):
            score += CORE_SKILL_POINTS
            matched.append(skill)
    for domain in profile.domains:
        if domain.lower() in haystack:
            score += DOMAIN_POINTS
    for skill in profile.bonus_skills:
        if skill and keyword_in_text(skill, haystack):
            score += BONUS_SKILL_POINTS
    return float(min(score, 100)), matched


__all__ = ["dealbreaker_hit", "score_job"]
