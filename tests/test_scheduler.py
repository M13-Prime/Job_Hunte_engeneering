"""Smoke tests for the APScheduler wrapper."""

from __future__ import annotations

from signal_tracker.notifier.scheduler import build_scheduler


async def _noop() -> None:
    return None


def test_build_scheduler_registers_daily_job() -> None:
    scheduler = build_scheduler(_noop, hour=7, minute=0, timezone="Europe/Paris")
    jobs = scheduler.get_jobs()
    assert len(jobs) == 1
    job = jobs[0]
    assert job.id == "daily_pipeline"
    assert "cron" in str(job.trigger).lower()


def test_build_scheduler_adds_weekly_newsletter_sunday_9am() -> None:
    scheduler = build_scheduler(
        _noop, hour=7, timezone="Europe/Paris",
        weekly_job=_noop, weekly_day=6, weekly_hour=9, weekly_minute=0,
    )
    jobs = {j.id: j for j in scheduler.get_jobs()}
    assert set(jobs) == {"daily_pipeline", "weekly_newsletter"}
    trigger = str(jobs["weekly_newsletter"].trigger)
    assert "day_of_week='sun'" in trigger
    assert "hour='9'" in trigger
