"""Mark a stored signal with user feedback.

Examples:
    uv run python scripts/feedback.py --list
    uv run python scripts/feedback.py 42 --action contacted
    uv run python scripts/feedback.py 42 --action relevant
    uv run python scripts/feedback.py 42 --action not_relevant
"""

from __future__ import annotations

import argparse
import sys

from rich.console import Console
from rich.table import Table
from sqlalchemy import desc, select

from signal_tracker.classifier.feedback import VALID_FEEDBACK
from signal_tracker.config import get_settings
from signal_tracker.storage import init_db
from signal_tracker.storage.models import RawItem, Signal


def _list_pending(limit: int) -> int:
    db = init_db(get_settings().db_path)
    console = Console()
    with db.session() as session:
        rows = list(
            session.execute(
                select(Signal, RawItem)
                .join(RawItem, Signal.raw_item_id == RawItem.id)
                .where(Signal.user_feedback.is_(None))
                .order_by(desc(Signal.total_score))
                .limit(limit)
            )
        )

    if not rows:
        console.print("[dim]No signals waiting for feedback.[/dim]")
        return 0

    table = Table(title=f"Top {len(rows)} signals without feedback")
    table.add_column("id", justify="right")
    table.add_column("score", justify="right")
    table.add_column("type")
    table.add_column("company")
    table.add_column("source")
    for signal, raw in rows:
        table.add_row(
            str(signal.id),
            f"{signal.total_score:.1f}",
            signal.signal_type,
            signal.company_name or "-",
            raw.source,
        )
    console.print(table)
    return 0


def _record(signal_id: int, action: str) -> int:
    if action not in VALID_FEEDBACK:
        print(f"Invalid action {action!r}. Must be one of {VALID_FEEDBACK}.")
        return 2
    db = init_db(get_settings().db_path)
    console = Console()
    with db.session() as session:
        signal = session.get(Signal, signal_id)
        if signal is None:
            console.print(f"[red]Signal #{signal_id} not found.[/red]")
            return 1
        signal.user_feedback = action
    console.print(
        f"[green]Signal #{signal_id} marked as[/green] [bold]{action}[/bold]."
    )
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Record user feedback on a signal.")
    parser.add_argument(
        "signal_id",
        type=int,
        nargs="?",
        help="ID of the signal to mark (omit with --list).",
    )
    parser.add_argument(
        "--action",
        choices=VALID_FEEDBACK,
        help="Verdict to record on the signal.",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="Print pending signals (no feedback yet).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=20,
        help="Max rows to display with --list (default: 20).",
    )
    args = parser.parse_args()

    if args.list:
        sys.exit(_list_pending(args.limit))
    if args.signal_id is None or args.action is None:
        parser.print_help()
        sys.exit(2)
    sys.exit(_record(args.signal_id, args.action))


if __name__ == "__main__":
    main()
