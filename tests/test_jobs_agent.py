"""Tests for the Jobs Agent tools (with the LLM patched).

We exercise each tool standalone against a real SQLite DB so the SQL
queries are validated. The LLM call inside score_job_semantically is
patched so the test doesn't depend on the network.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from signal_tracker.agents.jobs_agent import JobsState, _build_tools
from signal_tracker.config import UserProfile
from signal_tracker.storage import Database
from signal_tracker.storage.models import (
    JobOffer,
    RawItem,
    Signal,
)

_seq = {"n": 0}


def _make_signal(
    db: Database, *, company: str, score: float = 80.0,
    days_ago: int = 1,
) -> int:
    """Create a (RawItem, Signal) pair the agent's queries can find."""
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
        )
        s.add(sig)
        s.flush()
        return sig.id


def _make_job(
    db: Database, *, company: str, title: str = "Data Analyst",
    heuristic: float = 50.0, agent_processed: bool = False,
) -> int:
    with db.session() as s:
        job = JobOffer(
            company_normalized=company.lower(),
            company_name=company, ats="greenhouse",
            ats_company_slug=company.lower(),
            external_id=f"{company}-{title}",
            title=title, url=f"https://x/{title}",
            location="Paris", department="Data",
            description="We need a strong analyst.",
            relevance_score=heuristic, matched_roles=["Data Analyst"],
            is_open=True,
            dedup_key=f"{company.lower()}|greenhouse|{company}-{title}",
            agent_processed_at=(
                datetime.now(UTC) if agent_processed else None
            ),
        )
        s.add(job)
        s.flush()
        return job.id


@pytest.fixture()
def jobs_state(tmp_db: Database, sample_profile: UserProfile) -> JobsState:
    return JobsState(
        db=tmp_db, profile=sample_profile, user_id=None, cv_text="my cv",
    )


async def test_list_companies_with_recent_signals_dedups_and_sorts(
    jobs_state: JobsState,
) -> None:
    _make_signal(jobs_state.db, company="Acme", score=70)
    _make_signal(jobs_state.db, company="Acme", score=90)  # dup, higher
    _make_signal(jobs_state.db, company="Beta", score=60)
    _make_signal(jobs_state.db, company="OldCo", score=99, days_ago=30)

    tools = _build_tools(jobs_state)
    fn = tools.by_name("list_companies_with_recent_signals")
    assert fn is not None
    result = await fn.execute({"days": 14, "limit": 10})
    payload = json.loads(result.to_anthropic_content())
    names = [c["company_name"] for c in payload["companies"]]
    # OldCo excluded (out of window). Acme dedup'd. Acme first (higher score).
    assert "OldCo" not in names
    assert names[0] == "Acme"
    assert payload["companies"][0]["max_signal_score"] == 90


async def test_list_unscored_jobs_filters_processed_and_threshold(
    jobs_state: JobsState,
) -> None:
    _make_job(jobs_state.db, company="Acme", title="A", heuristic=60)
    _make_job(jobs_state.db, company="Acme", title="B", heuristic=20)
    _make_job(jobs_state.db, company="Acme", title="C", heuristic=80,
              agent_processed=True)

    tools = _build_tools(jobs_state)
    fn = tools.by_name("list_unscored_jobs")
    assert fn is not None
    out = await fn.execute({"limit": 10, "min_heuristic_score": 30})
    payload = json.loads(out.to_anthropic_content())
    titles = [j["title"] for j in payload["jobs"]]
    assert titles == ["A"]  # B below threshold, C already processed


async def test_score_job_semantically_persists_verdict(
    jobs_state: JobsState, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _make_signal(jobs_state.db, company="Acme", score=88)
    job_id = _make_job(jobs_state.db, company="Acme", title="Data Lead")

    # Patch litellm so the LLM call returns a deterministic verdict.
    async def fake_acompletion(**_: object) -> SimpleNamespace:
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content=json.dumps({
                "fit_score": 82,
                "fit_reasoning": "Couvre ESG + Python.",
                "killer_angle": "Tu as déjà fait ce projet exact chez X.",
                "why_now": "Signal récent: levée série B.",
            }))
        )])
    mock = AsyncMock(side_effect=fake_acompletion)
    # _semantic_score does `import litellm` locally, so we patch the
    # attribute on the actual module that gets bound.
    import litellm
    monkeypatch.setattr(litellm, "acompletion", mock)

    tools = _build_tools(jobs_state)
    fn = tools.by_name("score_job_semantically")
    assert fn is not None
    out = await fn.execute({"job_id": job_id})
    payload = json.loads(out.to_anthropic_content())
    assert payload["status"] == "scored"
    assert payload["fit_score"] == 82

    # The row was persisted.
    with jobs_state.db.session() as s:
        row = s.get(JobOffer, job_id)
        assert row is not None
        assert row.agent_score == 82.0
        assert row.agent_killer_angle is not None
        assert row.agent_processed_at is not None
    assert jobs_state.jobs_scored == 1


async def test_score_job_semantically_short_circuits_when_already_done(
    jobs_state: JobsState,
) -> None:
    job_id = _make_job(jobs_state.db, company="Acme", agent_processed=True)
    tools = _build_tools(jobs_state)
    fn = tools.by_name("score_job_semantically")
    assert fn is not None
    out = await fn.execute({"job_id": job_id})
    payload = json.loads(out.to_anthropic_content())
    assert payload["status"] == "already_scored"
    # No LLM call counted.
    assert jobs_state.jobs_scored == 0


async def test_mark_irrelevant_sets_processed_marker(
    jobs_state: JobsState,
) -> None:
    job_id = _make_job(jobs_state.db, company="Acme", title="HR Coordinator")
    tools = _build_tools(jobs_state)
    fn = tools.by_name("mark_irrelevant")
    assert fn is not None
    out = await fn.execute({"job_id": job_id, "reason": "wrong function"})
    payload = json.loads(out.to_anthropic_content())
    assert payload["status"] == "skipped"
    with jobs_state.db.session() as s:
        row = s.get(JobOffer, job_id)
        assert row is not None
        assert row.agent_processed_at is not None
        assert row.agent_score == 0.0
        assert "wrong function" in (row.agent_fit_reasoning or "")
    assert jobs_state.jobs_skipped == 1


async def test_unknown_job_id_raises_tool_error(
    jobs_state: JobsState,
) -> None:
    tools = _build_tools(jobs_state)
    fn = tools.by_name("mark_irrelevant")
    assert fn is not None
    out = await fn.execute({"job_id": 999_999, "reason": "x"})
    assert out.is_error is True
    assert "not found" in out.to_anthropic_content()
