"""End-to-end pipeline tests (collectors + classifier mocked)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from signal_tracker.classifier.schemas import ClassificationResult
from signal_tracker.collectors.base import BaseCollector, CollectedItem
from signal_tracker.config import UserProfile
from signal_tracker.pipeline import run_classification, run_collection
from signal_tracker.storage import Database, init_db
from signal_tracker.storage.models import RawItem, Signal


class _StubCollector(BaseCollector):
    source_id = "stub"

    def __init__(self, items: list[CollectedItem]) -> None:
        self._items = items

    async def collect(self) -> AsyncIterator[CollectedItem]:
        for item in self._items:
            yield item


def _item(url: str, title: str) -> CollectedItem:
    return CollectedItem(
        source="rss:test",
        url=url,
        title=title,
        content=f"Body of {title}",
        published_at=datetime(2025, 5, 12, tzinfo=UTC),
    )


async def test_run_collection_inserts_and_deduplicates(
    tmp_path: Path,
) -> None:
    db = init_db(tmp_path / "p.db")
    items = [
        _item("https://example.com/a", "A"),
        _item("https://example.com/b", "B"),
        _item("https://example.com/a", "A duplicate"),  # same URL -> dedup
    ]
    report = await run_collection(collectors=[_StubCollector(items)], db=db)
    assert report.fetched == 3
    assert report.new == 2
    assert report.duplicates == 1

    with db.session() as session:
        assert session.query(RawItem).count() == 2

    # Running again on the same backlog produces only duplicates.
    report2 = await run_collection(collectors=[_StubCollector(items)], db=db)
    assert report2.new == 0
    assert report2.duplicates == 3


async def test_run_classification_creates_signals_and_dedups(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    sample_profile: UserProfile,
) -> None:
    db = init_db(tmp_path / "c.db")
    items = [
        _item("https://example.com/sig1", "Carbone 4 nomme une CSO"),
        _item("https://example.com/sig2", "Carbone 4 confirme la nomination"),
        _item("https://example.com/sig3", "Apple sort un iPhone"),
    ]
    await run_collection(collectors=[_StubCollector(items)], db=db)

    relevant_payload = ClassificationResult(
        is_relevant=True,
        signal_type="executive_change",
        company_name="Carbone 4",
        company_normalized="carbone 4",
        relevance_score=95,
        urgency_score=90,
        fit_with_profile_score=92,
        summary_fr="Nomination CSO.",
        recommended_action="contact_immediate",
    )
    irrelevant_payload = ClassificationResult(
        is_relevant=False,
        signal_type="other",
        relevance_score=0,
        urgency_score=0,
        fit_with_profile_score=0,
        recommended_action="ignore",
    )

    call_results = {
        "https://example.com/sig1": relevant_payload,
        "https://example.com/sig2": relevant_payload,
        "https://example.com/sig3": irrelevant_payload,
    }

    async def fake_classify(item, _profile, **_kwargs):  # type: ignore[no-untyped-def]
        return call_results[item.url]

    monkeypatch.setattr("signal_tracker.pipeline.classify", fake_classify)

    report = await run_classification(profile=sample_profile, db=db)
    assert report.processed == 3
    assert report.relevant == 2
    # Both Carbone 4 entries fall in the same ISO week, so the second one is
    # deduped by signal_dedup_key.
    assert report.signals_created == 1
    assert report.signals_deduped == 1
    assert report.errors == 0

    with db.session() as session:
        assert session.query(Signal).count() == 1
        assert session.query(RawItem).filter_by(classified=True).count() == 3


async def test_run_classification_survives_errors(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    sample_profile: UserProfile,
    tmp_db: Database,
) -> None:
    db = tmp_db
    items = [
        _item("https://example.com/x1", "x1"),
        _item("https://example.com/x2", "x2"),
    ]
    await run_collection(collectors=[_StubCollector(items)], db=db)

    async def boom(*_a, **_k):  # type: ignore[no-untyped-def]
        raise RuntimeError("LLM exploded")

    monkeypatch.setattr("signal_tracker.pipeline.classify", boom)
    report = await run_classification(profile=sample_profile, db=db)
    assert report.errors == 2
    assert report.processed == 0
    assert report.signals_created == 0
