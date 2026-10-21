"""APScheduler wrapper: run ``daily_pipeline`` every day at ``DIGEST_SEND_HOUR``."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from signal_tracker.config import get_settings
from signal_tracker.utils.logging import get_logger

logger = get_logger(__name__)


_WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def build_scheduler(
    job: Callable[[], Awaitable[Any]],
    *,
    hour: int | None = None,
    minute: int = 0,
    timezone: str | None = None,
    weekly_job: Callable[[], Awaitable[Any]] | None = None,
    weekly_day: int = 6,
    weekly_hour: int = 9,
    weekly_minute: int = 0,
) -> AsyncIOScheduler:
    """Build (but do not start) a scheduler with one daily cron job, plus an
    optional weekly one (0 = Monday ... 6 = Sunday)."""
    settings = get_settings()
    scheduler = AsyncIOScheduler(timezone=timezone or "UTC")
    scheduler.add_job(
        job,
        CronTrigger(
            hour=hour if hour is not None else settings.digest_send_hour,
            minute=minute,
            timezone=timezone or "UTC",
        ),
        id="daily_pipeline",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    if weekly_job is not None and 0 <= weekly_day <= 6:
        scheduler.add_job(
            weekly_job,
            CronTrigger(
                day_of_week=_WEEKDAYS[weekly_day], hour=weekly_hour,
                minute=weekly_minute, timezone=timezone or "UTC",
            ),
            id="weekly_newsletter",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
            # If the VM was asleep at 9:00, still send within 3 hours.
            misfire_grace_time=3 * 3600,
        )
    return scheduler


async def run_forever(
    job: Callable[[], Awaitable[Any]],
    *,
    hour: int | None = None,
    minute: int = 0,
    timezone: str | None = None,
    weekly_job: Callable[[], Awaitable[Any]] | None = None,
    weekly_day: int = 6,
    weekly_hour: int = 9,
    weekly_minute: int = 0,
) -> None:
    """Start the scheduler and block until cancelled."""
    scheduler = build_scheduler(
        job, hour=hour, minute=minute, timezone=timezone,
        weekly_job=weekly_job, weekly_day=weekly_day,
        weekly_hour=weekly_hour, weekly_minute=weekly_minute,
    )
    scheduler.start()
    logger.info(
        "scheduler.started",
        extra={
            "hour": hour if hour is not None else get_settings().digest_send_hour,
            "minute": minute,
            "timezone": timezone or "UTC",
        },
    )
    try:
        await asyncio.Event().wait()  # block forever
    finally:
        scheduler.shutdown(wait=False)


__all__ = ["build_scheduler", "run_forever"]
