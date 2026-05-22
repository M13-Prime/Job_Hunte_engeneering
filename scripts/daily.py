"""Daily orchestrator: collect -> classify -> digest, in one shot."""

from __future__ import annotations

import argparse
import asyncio

from rich.console import Console

from signal_tracker.config import get_settings, load_user_profile, resolve_db_url
from signal_tracker.notifier.digest import DigestBuilder, record_digest_sent
from signal_tracker.notifier.email import (
    DryRunSender,
    EmailSenderProtocol,
    build_sender_from_settings,
)
from signal_tracker.notifier.render import render_digest
from signal_tracker.pipeline import run_classification, run_collection
from signal_tracker.storage import init_db
from signal_tracker.utils.logging import setup_logging


async def _main(dry_run: bool) -> None:
    settings = get_settings()
    setup_logging(settings.log_level)
    console = Console()
    console.rule("[bold cyan]Signal Tracker — daily run")

    db = init_db(resolve_db_url(settings))
    profile = load_user_profile()

    console.print("[bold]1/3 collect[/bold]")
    coll = await run_collection(db=db)
    console.print(
        f"  fetched={coll.fetched}  new={coll.new}  duplicates={coll.duplicates}"
    )

    console.print("[bold]2/3 classify[/bold]")
    clf = await run_classification(profile=profile, db=db)
    console.print(
        f"  processed={clf.processed}  relevant={clf.relevant}  "
        f"signals={clf.signals_created}  errors={clf.errors}"
    )

    console.print("[bold]3/3 digest[/bold]")
    builder = DigestBuilder(
        db=db,
        profile=profile,
        dashboard_base_url=settings.dashboard_base_url,
    )
    data = builder.build()
    rendered = render_digest(data)

    if dry_run or not settings.digest_to_email or not settings.digest_from_email:
        if not dry_run:
            console.print(
                "[yellow]No recipient/sender configured — running digest in dry-run.[/yellow]"
            )
        dry_sender: EmailSenderProtocol = DryRunSender()
        await dry_sender.send(
            to=settings.digest_to_email or "preview@local",
            subject=rendered.subject,
            html=rendered.html,
            text=rendered.text,
            sender=settings.digest_from_email or "preview@local",
        )
        console.print(f"  (dry-run) subject: {rendered.subject}")
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
    console.print(f"[bold green]Digest sent to {settings.digest_to_email}.[/bold green]")


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the daily orchestrator.")
    parser.add_argument(
        "--dry-run", action="store_true", help="Skip the actual email send."
    )
    args = parser.parse_args()
    asyncio.run(_main(dry_run=args.dry_run))


if __name__ == "__main__":
    main()
