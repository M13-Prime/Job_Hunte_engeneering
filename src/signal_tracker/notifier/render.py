"""Jinja2 rendering for the daily digest."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from signal_tracker.notifier.digest import DigestData

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"


@dataclass(slots=True)
class RenderedDigest:
    subject: str
    html: str
    text: str


def _build_subject(data: DigestData) -> str:
    if data.is_empty:
        return f"Signal Tracker — {data.generated_at.strftime('%d %b')} — RAS"
    return (
        f"Signal Tracker — {data.generated_at.strftime('%d %b')} — "
        f"{len(data.hot)} chaud · {len(data.investigate)} a investiguer"
    )


def _build_environment(templates_dir: Path = TEMPLATES_DIR) -> Environment:
    return Environment(
        loader=FileSystemLoader(str(templates_dir)),
        autoescape=select_autoescape(["html", "htm", "j2"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )


def render_digest(
    data: DigestData,
    *,
    templates_dir: Path = TEMPLATES_DIR,
) -> RenderedDigest:
    """Render a ``DigestData`` to (subject, html, text)."""
    env = _build_environment(templates_dir)
    subject = _build_subject(data)
    html = env.get_template("digest.html.j2").render(
        data=data,
        subject=subject,
        dashboard_base_url=data.dashboard_base_url,
    )
    text = env.get_template("digest.txt.j2").render(
        data=data,
        subject=subject,
    )
    return RenderedDigest(subject=subject, html=html, text=text)


__all__ = ["RenderedDigest", "render_digest"]
