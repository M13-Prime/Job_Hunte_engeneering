"""Score a job posting against the user profile (keyword-based, deterministic).

We deliberately keep this LLM-free: jobs scraping runs after the classifier,
fanning out to N companies / M jobs each. Running Claude on every posting
would explode the budget. The deterministic score is enough to surface the
right jobs in the digest; the user reads them.
"""

from __future__ import annotations

from signal_tracker.config import UserProfile
from signal_tracker.jobs.schemas import JobPosting


def _haystack(job: JobPosting) -> str:
    parts = [job.title or "", job.department or "", job.description or ""]
    return " ".join(parts).lower()


def score_job(job: JobPosting, profile: UserProfile) -> tuple[float, list[str]]:
    """Return (score 0-100, list of matched role labels).

    Scoring scheme:
      +30 per full target_role match (all words from the role appear in the
            title / department / description, in any order)
      +10 per domain keyword present
      Capped at 100.
    """
    haystack = _haystack(job)
    if not haystack.strip():
        return 0.0, []

    score = 0
    matched: list[str] = []
    for role in profile.target_roles:
        words = [w for w in role.lower().split() if len(w) > 1]
        if words and all(w in haystack for w in words):
            score += 30
            matched.append(role)
    for domain in profile.domains:
        if domain.lower() in haystack:
            score += 10
    return float(min(score, 100)), matched


__all__ = ["score_job"]
