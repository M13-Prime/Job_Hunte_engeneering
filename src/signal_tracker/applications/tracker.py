"""Application pipeline rules: statuses, labels, follow-up dates."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from signal_tracker.storage.models import APPLICATION_STATUSES, Application

STATUS_LABELS: dict[str, str] = {
    "saved": "À postuler",
    "applied": "Postulée",
    "interview": "Entretien",
    "offer": "Offre",
    "rejected": "Refus / abandon",
}

# Statuses where a follow-up reminder makes sense.
FOLLOWUP_STATUSES = frozenset({"applied", "interview"})


def utcnow() -> datetime:
    """Naive UTC — the DB columns are timezone-less DateTime."""
    return datetime.now(UTC).replace(tzinfo=None)


def apply_status(app: Application, status: str, *, followup_days: int = 7) -> None:
    """Move an application to `status`, maintaining dates.

    - First move to "applied" stamps applied_at and schedules a follow-up
      `followup_days` later (unless one is already set).
    - Leaving the follow-up statuses (offer / rejected / back to saved)
      clears the reminder.
    """
    if status not in APPLICATION_STATUSES:
        raise ValueError(f"unknown status {status!r}")
    now = utcnow()
    if status == "applied":
        if app.applied_at is None:
            app.applied_at = now
        if app.next_followup_at is None:
            app.next_followup_at = now + timedelta(days=followup_days)
    elif status not in FOLLOWUP_STATUSES:
        app.next_followup_at = None
    app.status = status
    app.updated_at = now


def is_followup_due(app: Application, *, now: datetime | None = None) -> bool:
    if app.status not in FOLLOWUP_STATUSES or app.next_followup_at is None:
        return False
    return app.next_followup_at <= (now or utcnow())


__all__ = [
    "FOLLOWUP_STATUSES",
    "STATUS_LABELS",
    "apply_status",
    "is_followup_due",
    "utcnow",
]
