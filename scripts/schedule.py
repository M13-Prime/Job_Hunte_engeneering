"""Start the APScheduler daemon that runs the daily orchestrator on cron."""

from __future__ import annotations

import asyncio

from rich.console import Console

from signal_tracker.config import get_settings, load_user_profile, resolve_db_url
from signal_tracker.notifier.digest import (
    DigestBuilder,
    record_digest_sent,
    resolve_digest_user_id,
)
from signal_tracker.notifier.email import (
    DryRunSender,
    EmailSenderProtocol,
    build_sender_from_settings,
)
from signal_tracker.notifier.render import render_digest
from signal_tracker.notifier.scheduler import run_forever
from signal_tracker.pipeline import (
    collectors_for_user,
    run_classification,
    run_collection,
)
from signal_tracker.storage import init_db
from signal_tracker.utils.logging import setup_logging


async def _job() -> None:
    settings = get_settings()
    db = init_db(resolve_db_url(settings))
    from signal_tracker.utils.llm_usage import set_usage_db

    set_usage_db(db)
    profile = load_user_profile()
    digest_user_id = resolve_digest_user_id(db, settings.digest_to_email)
    await run_collection(collectors=collectors_for_user(db, digest_user_id), db=db)
    await run_classification(profile=profile, db=db)
    if settings.jobs_daily_refresh_enabled and digest_user_id is not None:
        from signal_tracker.jobs.matching import refresh_and_score_for_user

        try:
            await refresh_and_score_for_user(db, profile, digest_user_id)
        except Exception as exc:  # never block the digest on the jobs step
            Console().print(f"[red]jobs step failed: {exc}[/red]")
    data = DigestBuilder(
        db=db,
        profile=profile,
        dashboard_base_url=settings.dashboard_base_url,
        user_id=digest_user_id,
        job_alert_threshold=settings.job_alert_threshold,
    ).build()
    rendered = render_digest(data)

    if not settings.digest_to_email or not settings.digest_from_email:
        dry_sender: EmailSenderProtocol = DryRunSender()
        await dry_sender.send(
            to="preview@local",
            subject=rendered.subject,
            html=rendered.html,
            text=rendered.text,
            sender="preview@local",
        )
        return

    sender: EmailSenderProtocol = build_sender_from_settings(settings)
    await sender.send(
        to=settings.digest_to_email,
        subject=rendered.subject,
        html=rendered.html,
        text=rendered.text,
        sender=settings.digest_from_email,
    )
    record_digest_sent(db, recipient=settings.digest_to_email, data=data)


async def _newsletter_job() -> None:
    """Weekly newsletter, own trigger (NEWSLETTER_WEEKDAY at NEWSLETTER_HOUR)."""
    from signal_tracker.notifier.newsletter import send_newsletters

    settings = get_settings()
    console = Console()
    if not settings.digest_from_email:
        console.print("[yellow]newsletter: DIGEST_FROM_EMAIL is empty, nothing sent[/yellow]")
        return
    db = init_db(resolve_db_url(settings))
    try:
        sender: EmailSenderProtocol = build_sender_from_settings(settings)
    except RuntimeError as exc:
        console.print(f"[yellow]newsletter: {exc}[/yellow]")
        return
    stats = await send_newsletters(
        db,
        sender=sender,
        from_email=settings.digest_from_email,
        base_url=settings.dashboard_base_url,
        secret=settings.session_secret_key,
        period_days=settings.newsletter_period_days,
    )
    console.print(f"newsletter: {stats}")


def main() -> None:
    settings = get_settings()
    setup_logging(settings.log_level)
    console = Console()
    console.rule("[bold cyan]Signal Tracker — scheduler")
    console.print(
        f"Will run daily at [bold]{settings.digest_send_hour:02d}:00[/bold] "
        f"({settings.digest_timezone}). Ctrl+C to stop."
    )
    days = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
    weekly = 0 <= settings.newsletter_weekday <= 6
    if weekly:
        console.print(
            f"Newsletter: every [bold]{days[settings.newsletter_weekday]}[/bold] at "
            f"[bold]{settings.newsletter_hour:02d}:{settings.newsletter_minute:02d}[/bold]."
        )
    asyncio.run(run_forever(
        _job, timezone=settings.digest_timezone,
        weekly_job=_newsletter_job if weekly else None,
        weekly_day=settings.newsletter_weekday,
        weekly_hour=settings.newsletter_hour,
        weekly_minute=settings.newsletter_minute,
    ))


if __name__ == "__main__":
    main()
