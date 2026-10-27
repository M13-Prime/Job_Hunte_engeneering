"""Tests for the keyword-based relevance scorer."""

from __future__ import annotations

from signal_tracker.config import UserProfile
from signal_tracker.jobs.relevance import score_job
from signal_tracker.jobs.schemas import JobPosting


def _job(title: str, department: str | None = None, description: str | None = None) -> JobPosting:
    return JobPosting(
        ats="greenhouse",
        ats_company_slug="x",
        external_id="1",
        title=title,
        url="https://example.com",
        department=department,
        description=description,
    )


def test_full_role_match_scores_30() -> None:
    profile = UserProfile(
        domains=["AI"],
        target_roles=["ML Engineer"],
    )
    score, matched = score_job(_job("Senior ML Engineer"), profile)
    assert score >= 30
    assert "ML Engineer" in matched


def test_domain_bonus_adds_10() -> None:
    profile = UserProfile(
        domains=["sustainability"],
        target_roles=["ML Engineer"],
    )
    score, _ = score_job(
        _job("Senior ML Engineer", description="Work on sustainability metrics"),
        profile,
    )
    assert score == 40  # 30 (role) + 10 (domain)


def test_unrelated_job_scores_zero() -> None:
    profile = UserProfile(
        domains=["AI"],
        target_roles=["ML Engineer"],
    )
    score, matched = score_job(_job("Office Manager"), profile)
    assert score == 0
    assert matched == []


def test_score_capped_at_100() -> None:
    profile = UserProfile(
        domains=["A", "B", "C", "D", "E", "F", "G", "H", "I", "J"],
        target_roles=["Engineer", "ML Engineer", "AI Engineer", "Data Engineer"],
    )
    score, _ = score_job(
        _job(
            "AI ML Data Engineer for everything",
            description="A B C D E F G H I J",
        ),
        profile,
    )
    assert score == 100


def test_core_and_bonus_skills_add_points() -> None:
    profile = UserProfile(
        target_roles=["Data Engineer"],
        core_skills=["Databricks"],
        bonus_skills=["Airflow"],
    )
    score, matched = score_job(
        _job("Data Engineer", description="Stack Databricks et Airflow."), profile
    )
    assert score == 50  # 30 role + 15 core + 5 bonus
    assert "Databricks" in matched


def test_dealbreaker_forces_zero() -> None:
    profile = UserProfile(
        target_roles=["Data Engineer"],
        core_skills=["Databricks"],
        dealbreakers=["alternance"],
    )
    job = _job("Data Engineer", description="Poste en alternance, stack Databricks.")
    score, matched = score_job(job, profile)
    assert score == 0.0
    assert matched == []


def test_dealbreaker_matches_whole_words_only() -> None:
    from signal_tracker.jobs.relevance import dealbreaker_hit

    profile = UserProfile(dealbreakers=["stage"])
    assert dealbreaker_hit(_job("Stage data engineer"), profile) == "stage"
    # "stagiaire" contains "stage" as a prefix but is a different word
    assert dealbreaker_hit(_job("Data engineer", description="Stagnation"), profile) is None


def test_no_dealbreakers_configured_is_a_noop() -> None:
    from signal_tracker.jobs.relevance import dealbreaker_hit

    assert dealbreaker_hit(_job("Anything", description="stage"), UserProfile()) is None
