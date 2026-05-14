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
