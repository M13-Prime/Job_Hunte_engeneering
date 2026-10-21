"""Build and send the weekly newsletter (Phase 13).

Usage:
    uv run python scripts/newsletter.py --dry-run          # render, send nothing
    uv run python scripts/newsletter.py --preview-out nl.html --user me@example.com
    uv run python scripts/newsletter.py                    # send to every opted-in user
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from rich.console import Console
from sqlalchemy import select

from signal_tracker.config import get_settings, resolve_db_url
from signal_tracker.notifier.email import (
    DryRunSender,
    EmailSenderProtocol,
    build_sender_from_settings,
)
from signal_tracker.notifier.newsletter import (
    NewsletterBuilder,
    render_newsletter,
    send_newsletters,
)
from signal_tracker.storage import init_db
from signal_tracker.storage.models import User
from signal_tracker.utils.logging import setup_logging


async def _main(dry_run: bool, preview_out: Path | None, user_email: str | None) -> None:
    settings = get_settings()
    setup_logging(settings.log_level)
    console = Console()
    db = init_db(resolve_db_url(settings))

    only_user_id: int | None = None
    if user_email:
        with db.session() as s:
            only_user_id = s.execute(
                select(User.id).where(User.email == user_email.strip().lower())
            ).scalar_one_or_none()
        if only_user_id is None:
            console.print(f"[red]No user {user_email}[/red]")
            return

    if preview_out:
        uid = only_user_id
        if uid is None:
            with db.session() as s:
                uid = s.execute(select(User.id).order_by(User.id).limit(1)).scalar_one_or_none()
        if uid is None:
            console.print("[red]No user in the database.[/red]")
            return
        data = NewsletterBuilder(
            db, base_url=settings.dashboard_base_url, secret=settings.session_secret_key,
            period_days=settings.newsletter_period_days,
        ).build(uid)
        preview_out.write_text(render_newsletter(data).html, encoding="utf-8")
        console.print(f"[green]Preview written to[/green] {preview_out}")

    sender: EmailSenderProtocol
    if dry_run or not settings.digest_from_email:
        sender = DryRunSender()
    else:
        try:
            sender = build_sender_from_settings(settings)
        except RuntimeError as exc:
            console.print(f"[yellow]{exc} — dry-run.[/yellow]")
            sender = DryRunSender()
    stats = await send_newsletters(
        db,
        sender=sender,
        from_email=settings.digest_from_email or "preview@local",
        base_url=settings.dashboard_base_url,
        secret=settings.session_secret_key,
        period_days=settings.newsletter_period_days,
        only_user_id=only_user_id,
    )
    console.print(f"newsletter: {stats}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Send the weekly newsletter.")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--preview-out", type=Path, default=None)
    parser.add_argument("--user", default=None, help="Only this user's email")
    args = parser.parse_args()
    asyncio.run(_main(args.dry_run, args.preview_out, args.user))


if __name__ == "__main__":
    main()
