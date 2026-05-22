"""Scrape current job offers from companies discovered in signals.

Examples:
    uv run python scripts/jobs.py --from-signals
    uv run python scripts/jobs.py --from-watchlist
    uv run python scripts/jobs.py --company "Sweep"
    uv run python scripts/jobs.py --from-signals --from-watchlist --top 20
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime, timedelta

from rich.console import Console
from rich.table import Table
from sqlalchemy import desc, select

from signal_tracker.config import (
    get_settings,
    load_jobs_config,
    load_user_profile,
)
from signal_tracker.jobs.scraper import (
    JobsScraper,
    ScrapingReport,
    parse_overrides,
    persist_result,
)
from signal_tracker.storage import init_db
from signal_tracker.storage.models import JobOffer, Signal, WatchlistEntry
from signal_tracker.utils.logging import setup_logging


def _companies_from_signals(db, days: int, min_score: float) -> list[str]:  # type: ignore[no-untyped-def]
    cutoff = datetime.now(tz=UTC) - timedelta(days=days)
    with db.session() as session:
        rows = list(
            session.execute(
                select(Signal.company_name)
                .where(Signal.created_at >= cutoff)
                .where(Signal.total_score >= min_score)
                .where(Signal.company_name.isnot(None))
                .where(Signal.company_name != "")
                .order_by(desc(Signal.total_score))
            ).scalars()
        )
    # Dedup preserving order (highest score first).
    seen: set[str] = set()
    out: list[str] = []
    for name in rows:
        if name not in seen:
            seen.add(name)
            out.append(name)
    return out


def _companies_from_watchlist(db) -> list[str]:  # type: ignore[no-untyped-def]
    with db.session() as session:
        return list(
            session.execute(
                select(WatchlistEntry.company_name).order_by(WatchlistEntry.company_name)
            ).scalars()
        )


async def _main(args: argparse.Namespace) -> None:
    settings = get_settings()
    setup_logging(settings.log_level)
    console = Console()
    console.rule("[bold cyan]Signal Tracker — jobs scraper")

    profile = load_user_profile()
    jobs_cfg = load_jobs_config()
    overrides = parse_overrides(jobs_cfg.get("overrides"))

    db = init_db(settings.db_path)

    sources_cfg = jobs_cfg.get("sources") or {}
    use_watchlist = bool(args.from_watchlist) or bool(sources_cfg.get("use_watchlist"))
    use_signals = bool(args.from_signals) or bool(sources_cfg.get("use_recent_signals"))
    days = int(sources_cfg.get("recent_signals_days", 14))
    min_score = float(sources_cfg.get("recent_signals_min_score", 50))

    companies: list[str] = list(args.company or [])
    if use_watchlist:
        companies.extend(_companies_from_watchlist(db))
    if use_signals:
        companies.extend(_companies_from_signals(db, days=days, min_score=min_score))

    seen: set[str] = set()
    unique_companies: list[str] = []
    for name in companies:
        key = name.strip().lower()
        if key and key not in seen:
            seen.add(key)
            unique_companies.append(name)

    if args.top is not None:
        unique_companies = unique_companies[: args.top]

    if not unique_companies:
        console.print(
            "[yellow]No companies to scrape. Pass --company / --from-signals "
            "/ --from-watchlist or enable sources in config/jobs.yaml.[/yellow]"
        )
        return

    console.print(f"Scraping {len(unique_companies)} companies...")
    scraper = JobsScraper(
        overrides=overrides,
        rate_limit_seconds=float(jobs_cfg.get("rate_limit_seconds", 1.0)),
        timeout_seconds=float(jobs_cfg.get("timeout_seconds", 20.0)),
    )
    results = await scraper.scrape_companies(unique_companies)

    report = ScrapingReport(companies_attempted=len(unique_companies))
    for result in results:
        if result.found:
            report.companies_with_ats += 1
        persist_result(db, result, profile, report)

    metrics = Table(title="Scraping metrics", show_lines=False)
    metrics.add_column("metric", style="bold")
    metrics.add_column("value", justify="right")
    metrics.add_row("companies attempted", str(report.companies_attempted))
    metrics.add_row("companies with ATS found", str(report.companies_with_ats))
    metrics.add_row("jobs new", str(report.jobs_new))
    metrics.add_row("jobs updated", str(report.jobs_updated))
    metrics.add_row("jobs total collected", str(report.jobs_collected))
    console.print(metrics)

    with db.session() as session:
        top_jobs = list(
            session.execute(
                select(JobOffer)
                .where(JobOffer.is_open.is_(True))
                .order_by(desc(JobOffer.relevance_score), desc(JobOffer.posted_at))
                .limit(15)
            ).scalars()
        )

    if top_jobs:
        top = Table(title="Top open jobs (by relevance)", show_lines=False)
        top.add_column("score", justify="right")
        top.add_column("company", style="bold")
        top.add_column("title")
        top.add_column("location")
        top.add_column("ats")
        for job in top_jobs:
            top.add_row(
                f"{job.relevance_score:.0f}",
                job.company_name,
                job.title,
                job.location or "-",
                job.ats,
            )
        console.print(top)


def main() -> None:
    parser = argparse.ArgumentParser(description="Scrape job offers from ATS APIs.")
    parser.add_argument(
        "--company",
        action="append",
        default=[],
        help="Company name to scrape (repeatable).",
    )
    parser.add_argument(
        "--from-signals",
        action="store_true",
        help="Pull companies from recent signals (DB-driven).",
    )
    parser.add_argument(
        "--from-watchlist",
        action="store_true",
        help="Pull companies from the watchlist table.",
    )
    parser.add_argument(
        "--top",
        type=int,
        default=None,
        help="Limit to the top N companies after deduplication.",
    )
    args = parser.parse_args()
    asyncio.run(_main(args))


if __name__ == "__main__":
    main()
