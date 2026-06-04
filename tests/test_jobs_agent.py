"""Tests for the Jobs Agent tools (Phase 11.1 per-user variant).

We exercise each tool standalone against a real SQLite DB so the SQL
queries are validated. The LLM call inside score_jobs_batch is patched
so the test doesn't depend on the network.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from signal_tracker.agents.jobs_agent import JobsState, _build_tools
from signal_tracker.config import UserProfile
from signal_tracker.storage import Database
from signal_tracker.storage.models import (
    JobAgentScore,
    JobOffer,
    RawItem,
    SearchRun,
    Signal,
    User,
)

_seq = {"n": 0}


def _make_user(db: Database, *, email: str = "u@x") -> int:
    _seq["n"] += 1
    with db.session() as s:
        u = User(
            email=f"{_seq['n']}-{email}", password_hash="x",
            is_active=True, is_approved=True,
        )
        s.add(u)
        s.flush()
        return u.id


def _make_signal(
    db: Database, *, company: str, score: float = 80.0,
    days_ago: int = 1, search_run_id: int | None = None,
) -> int:
    """Create a (RawItem, Signal) pair. If search_run_id is given, the
    signal is attributed to that run (used by user-scoping)."""
    _seq["n"] += 1
    nonce = _seq["n"]
    when = datetime.now(UTC) - timedelta(days=days_ago)
    with db.session() as s:
        raw = RawItem(
            source="rss", url=f"https://x/{company}/{nonce}",
            title=f"{company} news", content="...",
            published_at=when, classified=True,
            hash=f"hash-{company}-{nonce}",
        )
        s.add(raw)
        s.flush()
        sig = Signal(
            raw_item_id=raw.id, signal_type="hiring",
            company_name=company, company_normalized=company.lower(),
            relevance_score=80.0, urgency_score=70.0,
            fit_with_profile_score=70.0, total_score=score,
            summary_fr=f"{company} recrute.", recommended_action="contact",
            dedup_key=f"sig|{company}|{nonce}",
            created_at=when,
            search_run_id=search_run_id,
        )
        s.add(sig)
        s.flush()
        return sig.id


def _make_run(db: Database, *, user_id: int) -> int:
    with db.session() as s:
        r = SearchRun(user_id=user_id, label="t", status="done")
        s.add(r)
        s.flush()
        return r.id


def _make_job(
    db: Database, *, company: str, title: str = "Data Analyst",
    heuristic: float = 50.0, collected_at: datetime | None = None,
) -> int:
    _seq["n"] += 1
    nonce = _seq["n"]
    with db.session() as s:
        job = JobOffer(
            company_normalized=company.lower(),
            company_name=company, ats="greenhouse",
            ats_company_slug=company.lower(),
            external_id=f"{company}-{title}-{nonce}",
            title=title, url=f"https://x/{title}/{nonce}",
            location="Paris", department="Data",
            description="We need a strong analyst.",
            relevance_score=heuristic, matched_roles=["Data Analyst"],
            is_open=True,
            dedup_key=f"{company.lower()}|greenhouse|{nonce}",
        )
        s.add(job)
        s.flush()
        if collected_at is not None:
            job.collected_at = collected_at
        return job.id


@pytest.fixture()
def jobs_state(tmp_db: Database, sample_profile: UserProfile) -> JobsState:
    user_id = _make_user(tmp_db)
    return JobsState(
        db=tmp_db, profile=sample_profile, user_id=user_id, cv_text="my cv",
    )


async def test_list_companies_filters_to_users_signals(
    tmp_db: Database, sample_profile: UserProfile,
) -> None:
    """A user must only see companies from their OWN signals."""
    u1 = _make_user(tmp_db, email="a@x")
    u2 = _make_user(tmp_db, email="b@x")
    r1 = _make_run(tmp_db, user_id=u1)
    r2 = _make_run(tmp_db, user_id=u2)
    _make_signal(tmp_db, company="OnlyU1", score=80, search_run_id=r1)
    _make_signal(tmp_db, company="OnlyU2", score=90, search_run_id=r2)

    state_u1 = JobsState(
        db=tmp_db, profile=sample_profile, user_id=u1, cv_text="",
    )
    tools = _build_tools(state_u1)
    fn = tools.by_name("list_companies_with_recent_signals")
    assert fn is not None
    out = await fn.execute({"days": 14, "limit": 10})
    payload = json.loads(out.to_anthropic_content())
    names = [c["company_name"] for c in payload["companies"]]
    assert names == ["OnlyU1"]


async def test_list_unscored_jobs_filters_per_user(
    tmp_db: Database, sample_profile: UserProfile,
) -> None:
    """A user only sees jobs that ARE NOT scored FOR THEM yet — even
    if another user has scored them."""
    u1 = _make_user(tmp_db, email="a@x")
    u2 = _make_user(tmp_db, email="b@x")
    job_id = _make_job(tmp_db, company="Acme", title="A", heuristic=60)
    # u2 has already scored it.
    with tmp_db.session() as s:
        s.add(JobAgentScore(
            user_id=u2, job_offer_id=job_id, agent_score=80.0,
            agent_killer_angle="x",
        ))

    # u1 should still see the job.
    state_u1 = JobsState(
        db=tmp_db, profile=sample_profile, user_id=u1, cv_text="",
    )
    tools = _build_tools(state_u1)
    fn = tools.by_name("list_unscored_jobs")
    assert fn is not None
    out = await fn.execute({"limit": 10, "min_heuristic_score": 30})
    payload = json.loads(out.to_anthropic_content())
    assert [j["job_id"] for j in payload["jobs"]] == [job_id]

    # u2 should NOT see the job (already scored for them).
    state_u2 = JobsState(
        db=tmp_db, profile=sample_profile, user_id=u2, cv_text="",
    )
    tools = _build_tools(state_u2)
    fn = tools.by_name("list_unscored_jobs")
    assert fn is not None
    out = await fn.execute({"limit": 10, "min_heuristic_score": 30})
    payload = json.loads(out.to_anthropic_content())
    assert payload["jobs"] == []


async def test_score_jobs_batch_persists_per_user(
    jobs_state: JobsState, monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_id_a = _make_job(jobs_state.db, company="Acme", title="A")
    job_id_b = _make_job(jobs_state.db, company="Acme", title="B")

    async def fake_acompletion(**_: Any) -> SimpleNamespace:
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content=json.dumps({
                "fit_score": 82,
                "fit_reasoning": "OK.",
                "killer_angle": "Angle.",
                "why_now": "Now.",
            }))
        )])
    mock = AsyncMock(side_effect=fake_acompletion)
    import litellm
    monkeypatch.setattr(litellm, "acompletion", mock)

    tools = _build_tools(jobs_state)
    fn = tools.by_name("score_jobs_batch")
    assert fn is not None
    out = await fn.execute({"job_ids": [job_id_a, job_id_b]})
    payload = json.loads(out.to_anthropic_content())
    assert len(payload["scored"]) == 2
    assert all(r["status"] == "scored" for r in payload["scored"])

    # Each row was persisted to job_agent_scores for this user.
    with jobs_state.db.session() as s:
        rows = list(s.execute(
            JobAgentScore.__table__.select().where(
                JobAgentScore.user_id == jobs_state.user_id,
            )
        ))
    assert len(rows) == 2
    # LLM was called once per job — batched.
    assert mock.await_count == 2


async def test_mark_irrelevant_persists_zero_score(
    jobs_state: JobsState,
) -> None:
    job_id = _make_job(jobs_state.db, company="Acme", title="HR Coord")
    tools = _build_tools(jobs_state)
    fn = tools.by_name("mark_irrelevant")
    assert fn is not None
    out = await fn.execute({"job_id": job_id, "reason": "wrong function"})
    payload = json.loads(out.to_anthropic_content())
    assert payload["status"] == "skipped"

    with jobs_state.db.session() as s:
        score = s.execute(
            JobAgentScore.__table__.select().where(JobAgentScore.job_offer_id == job_id)
        ).first()
        assert score is not None
        assert score.agent_score == 0.0
        assert "wrong function" in (score.agent_fit_reasoning or "")


async def test_scrape_companies_skips_fresh_companies(
    jobs_state: JobsState, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A company scraped < 24h ago must be skipped (TTL cache)."""
    fresh_dt = datetime.now(UTC) - timedelta(hours=2)
    _make_job(
        jobs_state.db, company="FreshCo", title="X",
        heuristic=50, collected_at=fresh_dt,
    )

    # Patch the scraper so we know whether it was called.
    from signal_tracker.agents import jobs_agent as ja
    call_count = {"n": 0}

    class FakeScraper:
        def __init__(self) -> None:
            pass

        async def scrape_company(self, client: Any, name: str) -> Any:
            call_count["n"] += 1
            return SimpleNamespace(
                found=False, error="not used", company=name,
                company_normalized=name.lower(), ats=None,
            )

    monkeypatch.setattr(ja, "JobsScraper", FakeScraper)

    tools = _build_tools(jobs_state)
    fn = tools.by_name("scrape_companies_jobs")
    assert fn is not None
    out = await fn.execute({"company_names": ["FreshCo", "NewCo"]})
    payload = json.loads(out.to_anthropic_content())

    # FreshCo was skipped, NewCo was scraped (and returned not-found).
    skipped = [r for r in payload["results"] if r.get("skipped")]
    scraped = [r for r in payload["results"] if not r.get("skipped")]
    assert [r["company"] for r in skipped] == ["FreshCo"]
    assert [r["company"] for r in scraped] == ["NewCo"]
    assert call_count["n"] == 1
