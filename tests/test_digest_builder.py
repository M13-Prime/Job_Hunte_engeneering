"""Tests for ``DigestBuilder``: sectioning + dedup against digests_sent."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from signal_tracker.config import UserProfile
from signal_tracker.notifier.digest import DigestBuilder, record_digest_sent
from signal_tracker.storage import init_db
from signal_tracker.storage.models import DigestSent, RawItem, Signal


def _seed_raw(session, url: str, source: str = "rss:test"):  # type: ignore[no-untyped-def]
    raw = RawItem(
        source=source,
        url=url,
        title=url,
        content="...",
        hash=f"hash-{url}",
        classified=True,
        published_at=datetime.now(tz=UTC),
    )
    session.add(raw)
    session.flush()
    return raw


def _seed_signal(  # type: ignore[no-untyped-def]
    session,
    raw_id: int,
    *,
    score: float,
    signal_type: str = "executive_change",
    company: str = "Carbone 4",
    normalized: str = "carbone 4",
    created_at: datetime | None = None,
) -> Signal:
    signal = Signal(
        raw_item_id=raw_id,
        signal_type=signal_type,
        company_name=company,
        company_normalized=normalized,
        key_persons=[],
        relevance_score=score,
        urgency_score=score,
        fit_with_profile_score=score,
        total_score=score,
        summary_fr=f"Summary for {company} ({signal_type})",
        suggested_angle="Angle test",
        recommended_action="contact_immediate" if score >= 80 else "research_first",
        target_contact={"name": None, "role": "Head of Data", "rationale": "r"},
        dedup_key=f"{normalized}|{signal_type}|{raw_id}",
    )
    if created_at:
        signal.created_at = created_at
    session.add(signal)
    session.flush()
    return signal


def test_builder_partitions_into_three_sections(tmp_path: Path) -> None:
    db = init_db(tmp_path / "d.db")
    with db.session() as session:
        r1 = _seed_raw(session, "https://example.com/hot")
        r2 = _seed_raw(session, "https://example.com/mid")
        r3 = _seed_raw(session, "https://example.com/weak1")
        r4 = _seed_raw(session, "https://example.com/weak2")
        _seed_signal(session, r1.id, score=92)
        _seed_signal(session, r2.id, score=68, company="Sweep", normalized="sweep")
        # Two weak signals on the same (third) company -> shows up in watch.
        _seed_signal(
            session,
            r3.id,
            score=45,
            signal_type="funding",
            company="Plan A",
            normalized="plan a",
        )
        _seed_signal(
            session,
            r4.id,
            score=40,
            signal_type="strategic_announcement",
            company="Plan A",
            normalized="plan a",
        )

    data = DigestBuilder(db=db, profile=UserProfile()).build()
    assert len(data.hot) == 1
    assert data.hot[0].company_name == "Carbone 4"
    assert len(data.investigate) == 1
    assert data.investigate[0].company_name == "Sweep"
    assert len(data.watch) == 1
    assert data.watch[0].company == "Plan A"
    assert data.watch[0].count == 2
    assert "funding" in data.watch[0].signal_types
    assert "strategic_announcement" in data.watch[0].signal_types


def test_builder_excludes_already_sent_signals(tmp_path: Path) -> None:
    db = init_db(tmp_path / "d.db")
    with db.session() as session:
        r1 = _seed_raw(session, "https://example.com/a")
        r2 = _seed_raw(session, "https://example.com/b")
        s1 = _seed_signal(session, r1.id, score=85)
        s2 = _seed_signal(session, r2.id, score=87)
        # Record that s1 has already been sent.
        session.add(DigestSent(recipient="me@example.com", signal_ids=[s1.id]))
        kept_id = s2.id

    data = DigestBuilder(db=db, profile=UserProfile()).build()
    assert len(data.hot) == 1
    assert data.hot[0].id == kept_id


def test_builder_ignores_old_signals(tmp_path: Path) -> None:
    db = init_db(tmp_path / "d.db")
    with db.session() as session:
        r1 = _seed_raw(session, "https://example.com/old")
        _seed_signal(
            session,
            r1.id,
            score=92,
            created_at=datetime.now(tz=UTC) - timedelta(days=30),
        )

    data = DigestBuilder(db=db, profile=UserProfile()).build(recent_window_days=7)
    assert data.is_empty


def test_record_digest_sent_persists_signal_ids(tmp_path: Path) -> None:
    db = init_db(tmp_path / "d.db")
    with db.session() as session:
        r1 = _seed_raw(session, "https://example.com/a")
        r2 = _seed_raw(session, "https://example.com/b")
        _seed_signal(session, r1.id, score=85)
        _seed_signal(session, r2.id, score=82)

    data = DigestBuilder(db=db, profile=UserProfile()).build()
    record_digest_sent(db, recipient="me@example.com", data=data)

    # A subsequent build sees no signals.
    again = DigestBuilder(db=db, profile=UserProfile()).build()
    assert again.is_empty
