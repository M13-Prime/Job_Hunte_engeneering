"""Run one collection pass and report new raw_items."""

from __future__ import annotations

import asyncio

from rich.console import Console
from rich.table import Table

from signal_tracker.config import get_settings
from signal_tracker.pipeline import run_collection
from signal_tracker.utils.logging import setup_logging


async def _main() -> None:
    setup_logging(get_settings().log_level)
    console = Console()
    console.rule("[bold cyan]Signal Tracker — collect")
    report = await run_collection()
    table = Table(show_lines=False)
    table.add_column("metric", style="bold")
    table.add_column("value", justify="right")
    table.add_row("fetched", str(report.fetched))
    table.add_row("new", str(report.new))
    table.add_row("duplicates", str(report.duplicates))
    console.print(table)


def main() -> None:
    asyncio.run(_main())


if __name__ == "__main__":
    main()
