"""Sanity-check the local setup: .env, DB, templates, optional LLM ping."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from rich.console import Console
from rich.table import Table

from signal_tracker.config import (
    CONFIG_DIR,
    REPO_ROOT,
    get_settings,
    load_user_profile,
)
from signal_tracker.notifier.render import TEMPLATES_DIR as NOTIFIER_TEMPLATES_DIR
from signal_tracker.storage import init_db
from signal_tracker.storage.models import RawItem


def _row(table: Table, name: str, status: bool, detail: str = "") -> None:
    icon = "[green]OK[/green]" if status else "[red]FAIL[/red]"
    table.add_row(icon, name, detail)


async def _ping_llm() -> tuple[bool, str]:
    """Send a minimal classification call. Returns (ok, message)."""
    try:
        from signal_tracker.classifier.llm import classify
        from signal_tracker.classifier.schemas import ClassifierInput

        item = ClassifierInput(
            source="doctor",
            url="https://example.com/healthcheck",
            title="signal-tracker doctor healthcheck",
            content="(ignore me)",
        )
        await classify(item, load_user_profile())
        return True, "LLM round-trip succeeded"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {str(exc)[:500]}"


def main() -> None:
    console = Console()
    console.rule("[bold cyan]Signal Tracker — doctor")

    settings = get_settings()
    table = Table(show_lines=False)
    table.add_column("status", width=8)
    table.add_column("check", style="bold")
    table.add_column("detail")

    env_path = REPO_ROOT / ".env"
    _row(table, ".env file", env_path.exists(), str(env_path))

    profile_path = CONFIG_DIR / "user_profile.yaml"
    _row(table, "user_profile.yaml", profile_path.exists(), str(profile_path))

    sources_path = CONFIG_DIR / "sources.yaml"
    _row(table, "sources.yaml", sources_path.exists(), str(sources_path))

    _row(
        table,
        "LLM_MODEL set",
        bool(settings.llm_model),
        settings.llm_model or "(empty)",
    )

    # Map provider prefix -> required env var.
    provider_key_map: dict[str, str] = {
        "anthropic": "ANTHROPIC_API_KEY",
        "openai": "OPENAI_API_KEY",
        "gemini": "GEMINI_API_KEY",
        "mistral": "MISTRAL_API_KEY",
    }
    provider = settings.llm_model.split("/", 1)[0] if "/" in settings.llm_model else ""
    required_key = provider_key_map.get(provider)
    if required_key:
        present = bool(os.environ.get(required_key))
        _row(
            table,
            f"{required_key} set",
            present,
            "(redacted)" if present else f"set {required_key} in .env",
        )
    elif provider == "ollama":
        _row(
            table,
            "OLLAMA_BASE_URL set",
            bool(settings.ollama_base_url),
            settings.ollama_base_url or "(empty)",
        )

    db_path = Path(settings.db_path)
    db = init_db(db_path)
    with db.session() as session:
        raw_count = session.query(RawItem).count()
    _row(table, "SQLite DB", db_path.exists(), f"{db_path} · {raw_count} raw_items")

    _row(
        table,
        "Digest templates",
        (NOTIFIER_TEMPLATES_DIR / "digest.html.j2").exists()
        and (NOTIFIER_TEMPLATES_DIR / "digest.txt.j2").exists(),
        str(NOTIFIER_TEMPLATES_DIR),
    )

    email_ok = (
        bool(settings.resend_api_key)
        or bool(settings.smtp_host and settings.digest_to_email)
    )
    _row(
        table,
        "Email backend",
        email_ok,
        "Resend"
        if settings.resend_api_key
        else (f"SMTP {settings.smtp_host}" if settings.smtp_host else "dry-run only"),
    )

    console.print(table)

    if os.environ.get("DOCTOR_SKIP_LLM"):
        console.print("[dim]Skipping LLM round-trip (DOCTOR_SKIP_LLM is set).[/dim]")
        return
    if not required_key or not os.environ.get(required_key):
        console.print(
            "[yellow]Skipping LLM round-trip — no API key set for "
            f"{settings.llm_model}.[/yellow]"
        )
        return

    console.print("[bold]Pinging LLM…[/bold]")
    ok, detail = asyncio.run(_ping_llm())
    if ok:
        console.print(f"[green]LLM OK[/green] · {detail}")
    else:
        console.print(f"[red]LLM KO[/red] · {detail}")


if __name__ == "__main__":
    main()
