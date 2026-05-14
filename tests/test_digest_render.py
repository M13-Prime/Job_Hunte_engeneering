"""Tests for the Jinja2 digest renderer."""

from __future__ import annotations

from datetime import UTC, datetime

from signal_tracker.notifier.digest import DigestData, DigestSignal, WatchEntry
from signal_tracker.notifier.render import render_digest


def _signal(score: float, company: str = "Carbone 4", url: str = "https://x") -> DigestSignal:
    return DigestSignal(
        id=1,
        score=score,
        signal_type="executive_change",
        company_name=company,
        summary_fr=f"Resume pour {company}",
        suggested_angle="Angle X",
        recommended_action="contact_immediate",
        target_contact_name="A B",
        target_contact_role="CSO",
        target_contact_rationale="motif",
        source="rss:test",
        url=url,
        published_at=datetime(2025, 5, 12, tzinfo=UTC),
    )


def test_render_empty_digest_mentions_ras() -> None:
    data = DigestData(generated_at=datetime(2025, 5, 12, 7, 0, tzinfo=UTC), user_name=None)
    rendered = render_digest(data)
    assert "RAS" in rendered.subject
    assert "Pas de nouveau signal" in rendered.html
    assert "Pas de nouveau signal" in rendered.text


def test_render_includes_three_sections() -> None:
    data = DigestData(
        generated_at=datetime(2025, 5, 12, 7, 0, tzinfo=UTC),
        user_name="Malek",
        hot=[_signal(92, "Carbone 4", "https://example.com/c4")],
        investigate=[_signal(68, "Sweep", "https://example.com/sweep")],
        watch=[
            WatchEntry(
                company="Plan A",
                count=3,
                signal_types=["funding", "strategic_announcement"],
                latest_summary="Cumulatif weak signals",
            )
        ],
    )
    rendered = render_digest(data)
    assert "Malek" in rendered.html
    assert "Carbone 4" in rendered.html
    assert "Sweep" in rendered.html
    assert "Plan A" in rendered.html
    assert "92.0" in rendered.html
    assert "A contacter aujourd&#39;hui" in rendered.html or "A contacter" in rendered.html
    assert "A investiguer" in rendered.html
    assert "Veille en cours" in rendered.html
    # Text fallback covers the same content.
    assert "Carbone 4" in rendered.text
    assert "Sweep" in rendered.text
    assert "Plan A" in rendered.text


def test_render_omits_dashboard_button_when_no_url() -> None:
    data = DigestData(
        generated_at=datetime(2025, 5, 12, 7, 0, tzinfo=UTC),
        user_name=None,
        hot=[_signal(92)],
        dashboard_base_url=None,
    )
    rendered = render_digest(data)
    assert "Marquer comme contacte" not in rendered.html


def test_render_includes_dashboard_button_when_url_set() -> None:
    data = DigestData(
        generated_at=datetime(2025, 5, 12, 7, 0, tzinfo=UTC),
        user_name=None,
        hot=[_signal(92)],
        dashboard_base_url="https://dash.example.com",
    )
    rendered = render_digest(data)
    assert "Marquer comme contacte" in rendered.html
    assert "https://dash.example.com/signals/1/contacted" in rendered.html


def test_render_escapes_user_content() -> None:
    nasty = _signal(92, "<script>alert(1)</script>")
    data = DigestData(
        generated_at=datetime(2025, 5, 12, 7, 0, tzinfo=UTC),
        user_name=None,
        hot=[nasty],
    )
    rendered = render_digest(data)
    assert "<script>alert(1)</script>" not in rendered.html
    assert "&lt;script&gt;" in rendered.html
