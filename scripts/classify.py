"""Classify the backlog of unclassified raw_items and show the top signals."""

from __future__ import annotations

import argparse
import asyncio

from rich.console import Console
from rich.table import Table
from sqlalchemy import desc, select

from signal_tracker.config import get_settings
from signal_tracker.pipeline import run_classification
from signal_tracker.storage import init_db
from signal_tracker.storage.models import RawItem, Signal
from signal_tracker.utils.logging import setup_logging


async def _main(limit: int | None) -> None:
    setup_logging(get_settings().log_level)
    console = Console()
    console.rule("[bold cyan]Signal Tracker — classify")
    report = await run_classification(limit=limit)

    metrics = Table(show_lines=False, title="Run metrics")
    metrics.add_column("metric", style="bold")
    metrics.add_column("value", justify="right")
    metrics.add_row("processed", str(report.processed))
    metrics.add_row("relevant", str(report.relevant))
    metrics.add_row("signals_created", str(report.signals_created))
    metrics.add_row("signals_deduped", str(report.signals_deduped))
    metrics.add_row("errors", str(report.errors))
    console.print(metrics)

    settings = get_settings()
    db = init_db(settings.db_path)
    with db.session() as session:
        rows = list(
            session.execute(
                select(Signal, RawItem)
                .join(RawItem, Signal.raw_item_id == RawItem.id)
                .order_by(desc(Signal.total_score))
                .limit(10)
            )
        )

    if not rows:
        console.print("[dim]No signals stored yet.[/dim]")
        return

    top = Table(title="Top signals (by total_score)", show_lines=False)
    top.add_column("score", justify="right")
    top.add_column("type")
    top.add_column("company")
    top.add_column("action")
    top.add_column("source")
    for signal, raw in rows:
        top.add_row(
            f"{signal.total_score:.1f}",
            signal.signal_type,
            signal.company_name or "-",
            signal.recommended_action,
            raw.source,
        )
    console.print(top)


def main() -> None:
    parser = argparse.ArgumentParser(description="Classify the unclassified backlog.")
    parser.add_argument(
        "--limit", type=int, default=None, help="Max raw_items to classify in this run."
    )
    args = parser.parse_args()
    asyncio.run(_main(args.limit))


if __name__ == "__main__":
    main()
