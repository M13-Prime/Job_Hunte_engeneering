"""Build, render, and (optionally) send the daily digest email.

Usage:
    uv run python scripts/digest.py                 # send via configured backend
    uv run python scripts/digest.py --dry-run       # render to console + ./preview.html
    uv run python scripts/digest.py --preview-out preview.html
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from rich.console import Console
from rich.table import Table

from signal_tracker.config import get_settings, load_user_profile
from signal_tracker.notifier.digest import DigestBuilder, record_digest_sent
from signal_tracker.notifier.email import (
    DryRunSender,
    EmailSenderProtocol,
    build_sender_from_settings,
)
from signal_tracker.notifier.render import render_digest
from signal_tracker.storage import init_db
from signal_tracker.utils.logging import setup_logging


async def _main(dry_run: bool, preview_out: Path | None) -> None:
    settings = get_settings()
    setup_logging(settings.log_level)
    console = Console()
    console.rule("[bold cyan]Signal Tracker — digest")

    db = init_db(settings.db_path)
    profile = load_user_profile()

    builder = DigestBuilder(
        db=db,
        profile=profile,
        dashboard_base_url=settings.dashboard_base_url,
    )
    data = builder.build()
    rendered = render_digest(data)

    summary = Table(show_lines=False, title="Digest contents")
    summary.add_column("section", style="bold")
    summary.add_column("count", justify="right")
    summary.add_row("hot (>=80)", str(len(data.hot)))
    summary.add_row("investigate (60-79)", str(len(data.investigate)))
    summary.add_row("watch", str(len(data.watch)))
    summary.add_row("subject", rendered.subject)
    console.print(summary)

    if preview_out:
        preview_out.write_text(rendered.html, encoding="utf-8")
        console.print(f"[green]HTML preview written to[/green] {preview_out}")

    if dry_run:
        sender: EmailSenderProtocol = DryRunSender()
        console.print("[yellow]Dry-run mode — no email will leave the machine.[/yellow]")
    else:
        if not settings.digest_to_email or not settings.digest_from_email:
            console.print(
                "[red]DIGEST_TO_EMAIL or DIGEST_FROM_EMAIL is empty — "
                "switching to dry-run.[/red]"
            )
            sender = DryRunSender()
            dry_run = True
        else:
            sender = build_sender_from_settings(settings)

    if dry_run:
        await sender.send(
            to=settings.digest_to_email or "preview@local",
            subject=rendered.subject,
            html=rendered.html,
            text=rendered.text,
            sender=settings.digest_from_email or "preview@local",
        )
        return

    assert settings.digest_to_email and settings.digest_from_email
    await sender.send(
        to=settings.digest_to_email,
        subject=rendered.subject,
        html=rendered.html,
        text=rendered.text,
        sender=settings.digest_from_email,
    )
    record_digest_sent(db, recipient=settings.digest_to_email, data=data)
    console.print(f"[bold green]Digest envoye a {settings.digest_to_email}.[/bold green]")


def main() -> None:
    parser = argparse.ArgumentParser(description="Send the daily digest.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Render the digest but do not send any email.",
    )
    parser.add_argument(
        "--preview-out",
        type=Path,
        default=None,
        help="Optional path to write the rendered HTML preview (default: none).",
    )
    args = parser.parse_args()
    asyncio.run(_main(dry_run=args.dry_run, preview_out=args.preview_out))


if __name__ == "__main__":
    main()
