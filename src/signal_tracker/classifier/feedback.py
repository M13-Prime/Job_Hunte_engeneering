"""Dynamic few-shot examples extracted from past user feedback.

When the user marks a signal ``relevant`` / ``contacted`` / ``not_relevant``
via ``scripts/feedback.py``, we use those judgments as additional few-shot
examples in subsequent classifier calls.

Allowed values for ``Signal.user_feedback``: ``relevant``, ``contacted``,
``not_relevant``. The first two count as positive, the last as negative.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import desc, select

from signal_tracker.storage import Database
from signal_tracker.storage.models import RawItem, Signal

VALID_FEEDBACK = ("relevant", "contacted", "not_relevant")
POSITIVE_FEEDBACK = ("relevant", "contacted")
NEGATIVE_FEEDBACK = ("not_relevant",)


@dataclass(slots=True)
class FeedbackExample:
    """One labeled example to splice into the classifier prompt."""

    title: str | None
    content: str | None
    company_name: str
    signal_type: str
    summary_fr: str
    feedback: str  # one of VALID_FEEDBACK


def is_positive(example: FeedbackExample) -> bool:
    return example.feedback in POSITIVE_FEEDBACK


def load_feedback_examples(
    db: Database,
    *,
    window_days: int = 60,
    max_per_class: int = 3,
) -> list[FeedbackExample]:
    """Load up to ``max_per_class`` positive + ``max_per_class`` negative
    examples from the last ``window_days`` days.
    """
    cutoff = datetime.now(tz=UTC) - timedelta(days=window_days)
    positives: list[FeedbackExample] = []
    negatives: list[FeedbackExample] = []

    with db.session() as session:
        rows = list(
            session.execute(
                select(Signal, RawItem)
                .join(RawItem, Signal.raw_item_id == RawItem.id)
                .where(Signal.user_feedback.in_(VALID_FEEDBACK))
                .where(Signal.created_at >= cutoff)
                .order_by(desc(Signal.created_at))
            )
        )

    for signal, raw in rows:
        ex = FeedbackExample(
            title=raw.title,
            content=raw.content,
            company_name=signal.company_name,
            signal_type=signal.signal_type,
            summary_fr=signal.summary_fr,
            feedback=signal.user_feedback or "",
        )
        if is_positive(ex) and len(positives) < max_per_class:
            positives.append(ex)
        elif not is_positive(ex) and len(negatives) < max_per_class:
            negatives.append(ex)
        if len(positives) >= max_per_class and len(negatives) >= max_per_class:
            break

    return positives + negatives


def render_examples_block(examples: Iterable[FeedbackExample]) -> str:
    """Render a list of examples as plain-text shots appended to the prompt."""
    lines: list[str] = []
    for ex in examples:
        verdict = "POSITIVE (highly relevant)" if is_positive(ex) else "NEGATIVE (not relevant)"
        lines.append(
            f"- [{verdict} - prior feedback: {ex.feedback}]\n"
            f"  Title: {ex.title or '(no title)'}\n"
            f"  Company: {ex.company_name or '(unknown)'}\n"
            f"  Signal type: {ex.signal_type}\n"
            f"  Summary: {ex.summary_fr or '(none)'}"
        )
    return "\n".join(lines)


__all__ = [
    "NEGATIVE_FEEDBACK",
    "POSITIVE_FEEDBACK",
    "VALID_FEEDBACK",
    "FeedbackExample",
    "is_positive",
    "load_feedback_examples",
    "render_examples_block",
]
