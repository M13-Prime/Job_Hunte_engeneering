"""Digest data model + builder.

Three sections per the brief:

1. 🔥 "A contacter aujourd'hui"        — score >= 80
2. 📊 "A investiguer cette semaine"   — 60 <= score < 80
3. 👀 "Veille en cours"                — companies accumulating >=2 weak
                                          signals (score < 60) in the last
                                          ``watch_window_days`` days.

Signals that have already been included in a previous digest are filtered
out using the ``digests_sent`` table.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from signal_tracker.config import UserProfile
from signal_tracker.storage import Database
from signal_tracker.storage.models import DigestSent, RawItem, Signal

HOT_THRESHOLD = 80.0
INVESTIGATE_THRESHOLD = 60.0


@dataclass(slots=True)
class DigestSignal:
    """View-model for a single signal in the digest."""

    id: int
    score: float
    signal_type: str
    company_name: str
    summary_fr: str
    suggested_angle: str | None
    recommended_action: str
    target_contact_name: str | None
    target_contact_role: str | None
    target_contact_rationale: str | None
    source: str
    url: str
    published_at: datetime | None


@dataclass(slots=True)
class WatchEntry:
    """A company that has accumulated multiple weak signals."""

    company: str
    count: int
    signal_types: list[str]
    latest_summary: str


@dataclass(slots=True)
class DigestData:
    """Everything the template needs to render one digest email."""

    generated_at: datetime
    user_name: str | None
    hot: list[DigestSignal] = field(default_factory=list)
    investigate: list[DigestSignal] = field(default_factory=list)
    watch: list[WatchEntry] = field(default_factory=list)
    dashboard_base_url: str | None = None

    @property
    def total_signal_count(self) -> int:
        return len(self.hot) + len(self.investigate)

    @property
    def is_empty(self) -> bool:
        return not (self.hot or self.investigate or self.watch)


def _signal_to_view(signal: Signal, raw: RawItem | None) -> DigestSignal:
    contact = signal.target_contact or {}
    return DigestSignal(
        id=signal.id,
        score=float(signal.total_score),
        signal_type=signal.signal_type,
        company_name=signal.company_name or "(unknown)",
        summary_fr=signal.summary_fr,
        suggested_angle=signal.suggested_angle,
        recommended_action=signal.recommended_action,
        target_contact_name=contact.get("name"),
        target_contact_role=contact.get("role"),
        target_contact_rationale=contact.get("rationale"),
        source=raw.source if raw else "(unknown)",
        url=raw.url if raw else "",
        published_at=raw.published_at if raw else None,
    )


def _load_already_sent_ids(session: Session) -> set[int]:
    sent: set[int] = set()
    for row in session.execute(select(DigestSent.signal_ids)).scalars():
        for sid in row or []:
            sent.add(int(sid))
    return sent


class DigestBuilder:
    """Read signals from the DB and structure them into ``DigestData``."""

    def __init__(
        self,
        db: Database,
        profile: UserProfile | None = None,
        dashboard_base_url: str | None = None,
    ) -> None:
        self.db = db
        self.profile = profile
        self.dashboard_base_url = dashboard_base_url

    def build(
        self,
        *,
        watch_window_days: int = 30,
        recent_window_days: int = 7,
        watch_min_count: int = 2,
    ) -> DigestData:
        now = datetime.now(tz=UTC)
        cutoff_recent = now - timedelta(days=recent_window_days)
        cutoff_watch = now - timedelta(days=watch_window_days)

        with self.db.session() as session:
            sent_ids = _load_already_sent_ids(session)

            # Sections 1 + 2 — recent, unsent, score >= 60
            recent_rows = list(
                session.execute(
                    select(Signal, RawItem)
                    .join(RawItem, Signal.raw_item_id == RawItem.id)
                    .where(Signal.total_score >= INVESTIGATE_THRESHOLD)
                    .where(Signal.created_at >= cutoff_recent)
                )
            )

            # Section 3 — cumulative weak signals
            watch_rows = list(
                session.execute(
                    select(Signal)
                    .where(Signal.total_score < INVESTIGATE_THRESHOLD)
                    .where(Signal.created_at >= cutoff_watch)
                )
            )

        hot: list[DigestSignal] = []
        investigate: list[DigestSignal] = []
        for signal, raw in recent_rows:
            if signal.id in sent_ids:
                continue
            view = _signal_to_view(signal, raw)
            if view.score >= HOT_THRESHOLD:
                hot.append(view)
            else:
                investigate.append(view)

        hot.sort(key=lambda s: s.score, reverse=True)
        investigate.sort(key=lambda s: s.score, reverse=True)

        # Group watch signals by normalized company name.
        per_company: dict[str, list[Signal]] = defaultdict(list)
        for signal in (row[0] for row in watch_rows):
            if not signal.company_normalized:
                continue
            per_company[signal.company_normalized].append(signal)

        watch: list[WatchEntry] = []
        for normalized, signals in per_company.items():
            if len(signals) < watch_min_count:
                continue
            latest = max(signals, key=lambda s: s.created_at)
            watch.append(
                WatchEntry(
                    company=latest.company_name or normalized,
                    count=len(signals),
                    signal_types=sorted({s.signal_type for s in signals}),
                    latest_summary=latest.summary_fr,
                )
            )
        watch.sort(key=lambda w: w.count, reverse=True)

        return DigestData(
            generated_at=now,
            user_name=(self.profile.name if self.profile else None),
            hot=hot,
            investigate=investigate,
            watch=watch,
            dashboard_base_url=self.dashboard_base_url,
        )


def record_digest_sent(db: Database, recipient: str, data: DigestData) -> None:
    """Persist a row in ``digests_sent`` so signals don't reappear later."""
    signal_ids = [s.id for s in data.hot] + [s.id for s in data.investigate]
    if not signal_ids:
        return
    with db.session() as session:
        session.add(DigestSent(recipient=recipient, signal_ids=signal_ids))


__all__ = [
    "HOT_THRESHOLD",
    "INVESTIGATE_THRESHOLD",
    "DigestBuilder",
    "DigestData",
    "DigestSignal",
    "WatchEntry",
    "record_digest_sent",
]
