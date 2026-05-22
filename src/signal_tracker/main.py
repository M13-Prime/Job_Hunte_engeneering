"""CLI entry point — Phase 0 demo: validate the skeleton end-to-end."""

from __future__ import annotations

from rich.console import Console
from rich.table import Table

from signal_tracker import __version__
from signal_tracker.config import get_settings, load_user_profile, resolve_db_url
from signal_tracker.storage import init_db
from signal_tracker.storage.models import Company, Person, RawItem, Signal
from signal_tracker.utils.dedup import raw_item_hash
from signal_tracker.utils.normalize import normalize_company_name


def demo() -> None:
    """
    Wire the skeleton together:
    - load settings (.env) and user profile (config/user_profile.yaml)
    - create the SQLite DB and tables
    - insert + read back one dummy raw_item
    - print a summary table

    Used by ``make demo`` to confirm the project boots end-to-end.
    """
    console = Console()
    settings = get_settings()
    profile = load_user_profile()

    console.rule(f"[bold cyan]Signal Tracker v{__version__} — Phase 0 demo")

    profile_table = Table(title="User profile (config/user_profile.yaml)", show_lines=False)
    profile_table.add_column("field", style="bold")
    profile_table.add_column("value")
    profile_table.add_row("domains", ", ".join(profile.domains) or "(none)")
    profile_table.add_row("target_roles", ", ".join(profile.target_roles) or "(none)")
    profile_table.add_row("geographies", ", ".join(profile.geographies) or "(none)")
    profile_table.add_row(
        "target_company_types", ", ".join(profile.target_company_types) or "(none)"
    )
    console.print(profile_table)

    runtime_table = Table(title="Runtime settings (.env)", show_lines=False)
    runtime_table.add_column("field", style="bold")
    runtime_table.add_column("value")
    runtime_table.add_row("llm_model", settings.llm_model)
    runtime_table.add_row("llm_fallback_model", settings.llm_fallback_model or "(none)")
    runtime_table.add_row("digest_send_hour", str(settings.digest_send_hour))
    runtime_table.add_row("db_path", settings.db_path)
    runtime_table.add_row("log_level", settings.log_level)
    console.print(runtime_table)

    db = init_db(resolve_db_url(settings))
    console.print(f"[green]OK[/green] DB initialised at [bold]{settings.db_path}[/bold]")

    with db.session() as session:
        dummy_url = "https://example.com/signal-tracker/demo"
        dummy_hash = raw_item_hash("demo", dummy_url)
        existing = (
            session.query(RawItem).filter(RawItem.hash == dummy_hash).one_or_none()
        )
        if existing is None:
            session.add(
                RawItem(
                    source="demo",
                    url=dummy_url,
                    title="Phase 0 skeleton boot test",
                    content="If you can read this, the DB write path works.",
                    hash=dummy_hash,
                    classified=False,
                )
            )

        counts = {
            "raw_items": session.query(RawItem).count(),
            "companies": session.query(Company).count(),
            "persons": session.query(Person).count(),
            "signals": session.query(Signal).count(),
        }

    counts_table = Table(title="DB row counts", show_lines=False)
    counts_table.add_column("table", style="bold")
    counts_table.add_column("rows", justify="right")
    for table_name, n in counts.items():
        counts_table.add_row(table_name, str(n))
    console.print(counts_table)

    sample = normalize_company_name("Carbone 4 SAS")
    console.print(f"[dim]normalize_company_name('Carbone 4 SAS') -> '{sample}'[/dim]")
    console.print("[bold green]Phase 0 skeleton OK.[/bold green]")


if __name__ == "__main__":
    demo()
