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


def build_scheduler(
    job: Callable[[], Awaitable[Any]],
    *,
    hour: int | None = None,
    minute: int = 0,
    timezone: str | None = None,
) -> AsyncIOScheduler:
    """Build (but do not start) a scheduler with one daily cron job."""
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
    return scheduler


async def run_forever(
    job: Callable[[], Awaitable[Any]],
    *,
    hour: int | None = None,
    minute: int = 0,
    timezone: str | None = None,
) -> None:
    """Start the scheduler and block until cancelled."""
    scheduler = build_scheduler(job, hour=hour, minute=minute, timezone=timezone)
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
