"""Ghost-job hints computed from the offer history and the signals."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from signal_tracker.jobs.ghost import ghost_flags, title_key
from signal_tracker.storage import Database, init_db
from signal_tracker.storage.models import JobOffer, RawItem, Signal

NOW = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)


def _offer(i: int, title: str, *, is_open: bool = True, days_ago: int = 1,
           company: str = "acme") -> JobOffer:
    return JobOffer(
        company_normalized=company, company_name=company.title(), ats="greenhouse",
        ats_company_slug=company, external_id=str(i), title=title,
        url=f"https://example.com/{i}", location="Paris", description="x",
        relevance_score=10.0, is_open=is_open, dedup_key=f"{company}{i}",
        collected_at=(NOW - timedelta(days=days_ago)).replace(tzinfo=None),
    )


@pytest.fixture()
def db(tmp_path: Path) -> Database:
    return init_db(tmp_path / "ghost.db")


def test_title_key_matches_reposts() -> None:
    assert title_key("Chargé RSE (H/F)") == title_key("chargé rse - h/f")
    assert title_key("ESG Analyst (m/f/d)") == title_key("ESG analyst")


def test_fresh_unique_offer_is_clean(db: Database) -> None:
    with db.session() as s:
        s.add(_offer(1, "ESG Analyst"))
    with db.session() as s:
        jobs = list(s.query(JobOffer))
        assert ghost_flags(s, jobs, now=NOW) == {jobs[0].id: []}


def test_long_open_recycled_and_duplicated(db: Database) -> None:
    with db.session() as s:
        s.add(_offer(1, "Chargé RSE (H/F)", days_ago=80))
        s.add(_offer(2, "Chargé RSE", is_open=False, days_ago=200))
        s.add(_offer(3, "chargé rse h/f", days_ago=2))
        s.add(_offer(4, "Chargé RSE (F/H)", days_ago=2))
    with db.session() as s:
        target = s.query(JobOffer).filter_by(external_id="1").one()
        flags = ghost_flags(s, [target], now=NOW)[target.id]
    assert any("80 jours" in f for f in flags)
    assert any("recyclée" in f for f in flags)
    assert any("3 annonces identiques" in f for f in flags)


def test_layoff_signal_flags_company(db: Database) -> None:
    with db.session() as s:
        s.add(_offer(1, "Data Engineer"))
        raw = RawItem(source="rss:t", url="https://n.example/1", title="t", content="c",
                      hash="h1", classified=True, published_at=NOW)
        s.add(raw)
        s.flush()
        s.add(Signal(
            raw_item_id=raw.id, signal_type="strategic_announcement", company_name="Acme",
            company_normalized="acme", key_persons=[], relevance_score=50, urgency_score=50,
            fit_with_profile_score=50, total_score=50,
            summary_fr="Acme annonce un plan social et 300 suppressions de postes.",
            recommended_action="monitor", dedup_key="d1",
            created_at=(NOW - timedelta(days=10)).replace(tzinfo=None),
        ))
    with db.session() as s:
        job = s.query(JobOffer).one()
        flags = ghost_flags(s, [job], now=NOW)[job.id]
    assert any("Licenciements" in f for f in flags)
