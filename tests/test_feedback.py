"""Tests for the feedback loop (DB extraction + prompt injection)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from signal_tracker.classifier.feedback import (
    FeedbackExample,
    load_feedback_examples,
    render_examples_block,
)
from signal_tracker.classifier.prompts import render_system_prompt
from signal_tracker.config import UserProfile
from signal_tracker.storage import init_db
from signal_tracker.storage.models import RawItem, Signal


def _seed(  # type: ignore[no-untyped-def]
    session, url: str, *, feedback: str | None, score: float = 80.0,
    company: str = "Carbone 4", signal_type: str = "executive_change",
):
    raw = RawItem(
        source="rss:test",
        url=url,
        title=f"Title {url}",
        content=f"Body {url}",
        hash=f"h-{url}",
        classified=True,
        published_at=datetime.now(tz=UTC),
    )
    session.add(raw)
    session.flush()
    signal = Signal(
        raw_item_id=raw.id,
        signal_type=signal_type,
        company_name=company,
        company_normalized=company.lower(),
        key_persons=[],
        relevance_score=score,
        urgency_score=score,
        fit_with_profile_score=score,
        total_score=score,
        summary_fr=f"Summary for {url}",
        suggested_angle=None,
        recommended_action="contact_immediate",
        target_contact=None,
        dedup_key=f"key-{url}",
        user_feedback=feedback,
    )
    session.add(signal)
    session.flush()
    return raw, signal


def test_load_feedback_balances_positive_and_negative(tmp_path: Path) -> None:
    db = init_db(tmp_path / "fb.db")
    with db.session() as s:
        for i in range(5):
            _seed(s, f"https://x/pos{i}", feedback="contacted")
        for i in range(5):
            _seed(s, f"https://x/neg{i}", feedback="not_relevant")
        _seed(s, "https://x/none", feedback=None)  # no feedback -> ignored

    examples = load_feedback_examples(db, max_per_class=2)
    pos = [e for e in examples if e.feedback in ("relevant", "contacted")]
    neg = [e for e in examples if e.feedback == "not_relevant"]
    assert len(pos) == 2
    assert len(neg) == 2


def test_render_examples_block_marks_polarity() -> None:
    examples = [
        FeedbackExample(
            title="t1", content="c1", company_name="A",
            signal_type="executive_change", summary_fr="s1", feedback="contacted",
        ),
        FeedbackExample(
            title="t2", content="c2", company_name="B",
            signal_type="other", summary_fr="s2", feedback="not_relevant",
        ),
    ]
    block = render_examples_block(examples)
    assert "POSITIVE" in block
    assert "NEGATIVE" in block
    assert "executive_change" in block


def test_render_system_prompt_appends_feedback_section() -> None:
    profile = UserProfile(domains=["AI"], target_roles=["X"])
    base = render_system_prompt(profile)
    with_extra = render_system_prompt(
        profile,
        extra_examples=[
            FeedbackExample(
                title="t", content="c", company_name="A",
                signal_type="funding", summary_fr="s", feedback="contacted",
            )
        ],
    )
    assert "DYNAMIC EXAMPLES" in with_extra
    assert "DYNAMIC EXAMPLES" not in base
    assert len(with_extra) > len(base)


def test_pipeline_passes_feedback_to_classify(
    monkeypatch: object, tmp_path: Path
) -> None:
    """Smoke check that run_classification injects extra_examples into classify()."""
    import asyncio

    from signal_tracker.classifier.schemas import ClassificationResult
    from signal_tracker.pipeline import run_classification

    db = init_db(tmp_path / "fb2.db")
    with db.session() as s:
        # seed a positive feedback example
        _seed(s, "https://x/pos", feedback="contacted")
        # seed an unclassified item to trigger a classify() call
        s.add(
            RawItem(
                source="rss:t", url="https://x/new", title="new", content="body",
                hash="hnew", classified=False,
                published_at=datetime.now(tz=UTC),
            )
        )

    captured: dict[str, object] = {}

    async def fake_classify(item, profile, *, extra_examples=None):  # type: ignore[no-untyped-def]
        captured["extra_examples"] = extra_examples
        return ClassificationResult(
            is_relevant=False, signal_type="other",
            relevance_score=0, urgency_score=0, fit_with_profile_score=0,
            recommended_action="ignore",
        )

    monkeypatch.setattr("signal_tracker.pipeline.classify", fake_classify)  # type: ignore[attr-defined]
    asyncio.run(run_classification(db=db, profile=UserProfile()))
    examples = captured["extra_examples"]
    assert isinstance(examples, list)
    assert len(examples) == 1
