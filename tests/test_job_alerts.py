"""Phase 12 — job alerts + follow-ups in the digest, daily refresh/score."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from signal_tracker.config import UserProfile
from signal_tracker.jobs import matching
from signal_tracker.jobs.scraper import ScrapingReport
from signal_tracker.notifier.digest import (
    DigestBuilder,
    record_digest_sent,
    resolve_digest_user_id,
)
from signal_tracker.notifier.render import render_digest
from signal_tracker.storage import Database
from signal_tracker.storage.models import (
    Application,
    JobAgentScore,
    JobOffer,
    User,
    UserCV,
    WatchlistEntry,
)
from signal_tracker.utils.normalize import normalize_company_name

_seq = {"n": 0}


def _user(db: Database, email: str, *, owner: bool = False) -> int:
    with db.session() as s:
        u = User(
            email=email, password_hash="x", is_active=True,
            is_approved=True, is_owner=owner,
        )
        s.add(u)
        s.flush()
        return u.id


def _job(db: Database, company: str = "Acme", heuristic: float = 50.0) -> int:
    _seq["n"] += 1
    n = _seq["n"]
    with db.session() as s:
        job = JobOffer(
            company_normalized=normalize_company_name(company), company_name=company,
            ats="lever", ats_company_slug="acme", external_id=f"x{n}",
            title=f"Role {n}", url=f"https://jobs/{n}", relevance_score=heuristic,
            is_open=True, dedup_key=f"k{n}",
        )
        s.add(job)
        s.flush()
        return job.id


def _score(db: Database, user_id: int, job_id: int, score: float) -> None:
    with db.session() as s:
        s.add(JobAgentScore(
            user_id=user_id, job_offer_id=job_id, agent_score=score,
            agent_killer_angle="Angle fort.",
        ))


def test_digest_includes_new_high_scoring_jobs_for_that_user(tmp_db: Database) -> None:
    me = _user(tmp_db, "me@x", owner=True)
    other = _user(tmp_db, "other@x")
    good, weak, others = _job(tmp_db), _job(tmp_db), _job(tmp_db)
    _score(tmp_db, me, good, 82)
    _score(tmp_db, me, weak, 40)
    _score(tmp_db, other, others, 95)

    data = DigestBuilder(tmp_db, user_id=me, job_alert_threshold=60).build()
    assert [j.id for j in data.new_jobs] == [good]
    assert not data.is_empty

    rendered = render_digest(data)
    assert "Nouvelles offres" in rendered.html
    assert "offre(s)" in rendered.subject
    assert "Angle fort." in rendered.text

    record_digest_sent(tmp_db, "me@x", data)
    again = DigestBuilder(tmp_db, user_id=me, job_alert_threshold=60).build()
    assert again.new_jobs == []


def test_digest_without_user_has_no_job_sections(tmp_db: Database) -> None:
    me = _user(tmp_db, "me@x")
    _score(tmp_db, me, _job(tmp_db), 90)
    data = DigestBuilder(tmp_db).build()
    assert data.new_jobs == []
    assert data.is_empty


def test_digest_lists_due_followups(tmp_db: Database) -> None:
    me = _user(tmp_db, "me@x")
    now = datetime.now(UTC).replace(tzinfo=None)
    with tmp_db.session() as s:
        s.add(Application(
            user_id=me, company_name="Due Co", title="PM", status="applied",
            applied_at=now - timedelta(days=8), next_followup_at=now - timedelta(days=1),
        ))
        s.add(Application(
            user_id=me, company_name="Later Co", title="PM", status="applied",
            next_followup_at=now + timedelta(days=3),
        ))
        s.add(Application(
            user_id=me, company_name="Closed Co", title="PM", status="rejected",
            next_followup_at=now - timedelta(days=1),
        ))
    data = DigestBuilder(tmp_db, user_id=me).build()
    assert [f.company_name for f in data.followups] == ["Due Co"]
    rendered = render_digest(data)
    assert "Relances" in rendered.html
    assert "relance(s)" in rendered.subject


def test_resolve_digest_user_id(tmp_db: Database) -> None:
    assert resolve_digest_user_id(tmp_db, "x@x") is None
    owner = _user(tmp_db, "owner@x", owner=True)
    me = _user(tmp_db, "me@x")
    assert resolve_digest_user_id(tmp_db, "me@x") == me
    assert resolve_digest_user_id(tmp_db, "nobody@x") == owner
    assert resolve_digest_user_id(tmp_db, None) == owner


async def test_refresh_and_score_respects_limit_and_scope(
    tmp_db: Database, sample_profile: UserProfile, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import litellm

    me = _user(tmp_db, "me@x")
    with tmp_db.session() as s:
        s.add(WatchlistEntry(user_id=me, company_name="Acme", normalized_name="acme"))
        s.add(UserCV(user_id=me, text="CV text", char_count=7))
    acme_jobs = [_job(tmp_db, "Acme", heuristic=h) for h in (60, 50, 40)]
    _job(tmp_db, "Elsewhere", heuristic=90)  # not one of the user's companies
    _job(tmp_db, "Acme", heuristic=0)  # below the keyword floor

    scraped: list[list[str]] = []

    async def fake_refresh(
        _db: Database, _profile: UserProfile, companies: list[str],
    ) -> ScrapingReport:
        scraped.append(companies)
        return ScrapingReport(companies_attempted=len(companies), jobs_new=3)

    calls = {"n": 0}

    async def fake_acompletion(**_: Any) -> SimpleNamespace:
        calls["n"] += 1
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
            content=json.dumps({"fit_score": 70, "fit_reasoning": "ok"})
        ))])

    monkeypatch.setattr(matching, "refresh_jobs", fake_refresh)
    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)

    res = await matching.refresh_and_score_for_user(
        tmp_db, sample_profile, me, score_limit=2, min_heuristic=10,
    )
    assert scraped == [["Acme"]]
    assert res.jobs_new == 3
    assert res.jobs_scored == 2
    assert calls["n"] == 2
    with tmp_db.session() as s:
        from sqlalchemy import select

        scored = set(s.execute(select(JobAgentScore.job_offer_id)).scalars())
    assert scored == set(acme_jobs[:2])  # best keyword scores first


async def test_refresh_without_cv_scrapes_but_does_not_score(
    tmp_db: Database, sample_profile: UserProfile, monkeypatch: pytest.MonkeyPatch,
) -> None:
    me = _user(tmp_db, "me@x")
    with tmp_db.session() as s:
        s.add(WatchlistEntry(user_id=me, company_name="Acme", normalized_name="acme"))
    _job(tmp_db, "Acme", heuristic=60)

    async def fake_refresh(*_: Any) -> ScrapingReport:
        return ScrapingReport()

    async def no_llm(**_: Any) -> Any:
        raise AssertionError("LLM must not be called without a CV")

    import litellm

    monkeypatch.setattr(matching, "refresh_jobs", fake_refresh)
    monkeypatch.setattr(litellm, "acompletion", no_llm)
    res = await matching.refresh_and_score_for_user(tmp_db, sample_profile, me)
    assert res.jobs_scored == 0
