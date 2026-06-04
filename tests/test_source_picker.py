"""Tests for the Phase 10 dynamic source picker."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from signal_tracker.agents.source_picker import (
    DomainSpec,
    PickerSelection,
    load_source_registry,
    materialize_sources,
    pick_sources,
)
from signal_tracker.config import UserProfile

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fake_response(payload: dict[str, Any]) -> Any:
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(payload)))],
        usage=SimpleNamespace(prompt_tokens=10, completion_tokens=10),
        model="anthropic/claude-haiku-4-5",
    )


@pytest.fixture()
def patched_litellm(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    mock = AsyncMock()
    monkeypatch.setattr(
        "signal_tracker.agents.source_picker.litellm.acompletion", mock,
    )
    return mock


@pytest.fixture()
def sample_registry() -> dict[str, DomainSpec]:
    return {
        "design_public": DomainSpec(
            domain_id="design_public", label="Design",
            description="Service design + public sector design.",
            keywords=["design", "service designer"],
            rss=[{"id": "r1", "name": "R1", "url": "https://r1.example/feed"}],
            gdelt_queries=[{"id": "g1", "query": '"design lead" hires'}],
        ),
        "business_intelligence": DomainSpec(
            domain_id="business_intelligence", label="BI",
            description="Business intelligence + analytics.",
            keywords=["BI", "data analyst"],
            rss=[{"id": "r2", "name": "R2", "url": "https://r2.example/feed"}],
            gdelt_queries=[{"id": "g2", "query": '"Head of Data" appointment'}],
        ),
        "fr_tech_general": DomainSpec(
            domain_id="fr_tech_general", label="Tech FR",
            description="Generic FR/EU tech and startup news.",
            keywords=["startup", "levée"],
            rss=[{"id": "r3", "name": "R3", "url": "https://r3.example/feed"}],
            gdelt_queries=[],
        ),
    }


@pytest.fixture()
def design_profile() -> UserProfile:
    return UserProfile(
        domains=["Design public", "Service design"],
        target_roles=["Service Designer", "Design Lead"],
        geographies=["France"],
    )


# ---------------------------------------------------------------------------
# Registry loader
# ---------------------------------------------------------------------------


def test_load_registry_reads_real_yaml() -> None:
    """The shipped config/source_registry.yaml must parse cleanly into
    DomainSpecs every CI run."""
    registry = load_source_registry()
    assert registry, "shipped registry is empty"
    # Every domain we know about is present.
    for required in ("design_public", "business_intelligence", "fr_tech_general"):
        assert required in registry, f"missing domain: {required}"
        d = registry[required]
        assert d.label
        assert d.description
        assert d.feed_configs()  # not empty
    # GDELT queries are optional per domain (fr_tech_general has none).
    assert registry["fr_tech_general"].gdelt_query_objects() == []


def test_load_registry_missing_file_returns_empty(tmp_path: Path) -> None:
    out = load_source_registry(tmp_path / "does_not_exist.yaml")
    assert out == {}


# ---------------------------------------------------------------------------
# Picker fail-open + opt-in
# ---------------------------------------------------------------------------


async def test_picker_disabled_returns_all_domains(
    monkeypatch: pytest.MonkeyPatch,
    sample_registry: dict[str, DomainSpec],
    design_profile: UserProfile,
    patched_litellm: AsyncMock,
) -> None:
    monkeypatch.delenv("LLM_SOURCE_PICKER_ENABLED", raising=False)
    from signal_tracker.config import get_settings
    get_settings.cache_clear()

    selection = await pick_sources(
        design_profile, {"field": ["design"]}, registry=sample_registry,
    )
    assert set(selection.selected_domain_ids) == set(sample_registry.keys())
    # And no LLM call was made.
    assert patched_litellm.await_count == 0


async def test_picker_failure_falls_back_open(
    monkeypatch: pytest.MonkeyPatch,
    sample_registry: dict[str, DomainSpec],
    design_profile: UserProfile,
    patched_litellm: AsyncMock,
) -> None:
    monkeypatch.setenv("LLM_SOURCE_PICKER_ENABLED", "true")
    monkeypatch.setenv("LLM_CHEAP_MODEL", "anthropic/claude-haiku-4-5")
    from signal_tracker.config import get_settings
    get_settings.cache_clear()

    patched_litellm.side_effect = RuntimeError("provider down")
    selection = await pick_sources(
        design_profile, {"field": ["design"]}, registry=sample_registry,
    )
    # Fail open = activate every domain so no relevant source is lost.
    assert set(selection.selected_domain_ids) == set(sample_registry.keys())


# ---------------------------------------------------------------------------
# Picker happy path
# ---------------------------------------------------------------------------


async def test_picker_returns_llm_selection(
    monkeypatch: pytest.MonkeyPatch,
    sample_registry: dict[str, DomainSpec],
    design_profile: UserProfile,
    patched_litellm: AsyncMock,
) -> None:
    monkeypatch.setenv("LLM_SOURCE_PICKER_ENABLED", "true")
    monkeypatch.setenv("LLM_CHEAP_MODEL", "anthropic/claude-haiku-4-5")
    from signal_tracker.config import get_settings
    get_settings.cache_clear()

    patched_litellm.return_value = _fake_response({
        "selected_domain_ids": ["design_public", "fr_tech_general"],
        "extra_gdelt_queries": ['"service design" recrute'],
        "rationale": "Tu cibles le design public et tu veux un filet général.",
    })

    selection = await pick_sources(
        design_profile,
        {"field": ["design public", "service design"], "job_title": ["Service Designer"]},
        registry=sample_registry,
    )
    assert selection.selected_domain_ids == ["design_public", "fr_tech_general"]
    assert selection.extra_gdelt_queries == ['"service design" recrute']
    assert "design public" in selection.rationale.lower()


async def test_picker_drops_hallucinated_domain_ids(
    monkeypatch: pytest.MonkeyPatch,
    sample_registry: dict[str, DomainSpec],
    design_profile: UserProfile,
    patched_litellm: AsyncMock,
) -> None:
    """LLM may invent domain IDs — those must be filtered out."""
    monkeypatch.setenv("LLM_SOURCE_PICKER_ENABLED", "true")
    monkeypatch.setenv("LLM_CHEAP_MODEL", "anthropic/claude-haiku-4-5")
    from signal_tracker.config import get_settings
    get_settings.cache_clear()

    patched_litellm.return_value = _fake_response({
        "selected_domain_ids": ["design_public", "bogus_domain", "another_fake"],
        "extra_gdelt_queries": [],
        "rationale": "OK",
    })
    selection = await pick_sources(
        design_profile, {"field": ["design"]}, registry=sample_registry,
    )
    assert selection.selected_domain_ids == ["design_public"]


async def test_picker_empty_after_hallucination_filter_falls_back(
    monkeypatch: pytest.MonkeyPatch,
    sample_registry: dict[str, DomainSpec],
    design_profile: UserProfile,
    patched_litellm: AsyncMock,
) -> None:
    """If the LLM picked nothing valid, fall back to all domains so we
    don't run a search with zero sources."""
    monkeypatch.setenv("LLM_SOURCE_PICKER_ENABLED", "true")
    monkeypatch.setenv("LLM_CHEAP_MODEL", "anthropic/claude-haiku-4-5")
    from signal_tracker.config import get_settings
    get_settings.cache_clear()

    patched_litellm.return_value = _fake_response({
        "selected_domain_ids": ["totally_made_up"],
        "extra_gdelt_queries": [],
        "rationale": "x",
    })
    selection = await pick_sources(
        design_profile, {}, registry=sample_registry,
    )
    assert set(selection.selected_domain_ids) == set(sample_registry.keys())


# ---------------------------------------------------------------------------
# Materialization
# ---------------------------------------------------------------------------


def test_materialize_dedupes_feeds_and_queries(
    sample_registry: dict[str, DomainSpec],
) -> None:
    # Duplicate the design_public RSS URL into business_intelligence to
    # confirm we collapse it.
    sample_registry["business_intelligence"].rss.append(
        {"id": "r1_dup", "name": "Dup", "url": "https://r1.example/feed"}
    )
    selection = PickerSelection(
        selected_domain_ids=["design_public", "business_intelligence"],
        extra_gdelt_queries=['"AI Act" compliance'],
    )
    feeds, queries = materialize_sources(selection, sample_registry)
    feed_urls = [f.url for f in feeds]
    assert feed_urls.count("https://r1.example/feed") == 1
    assert "https://r2.example/feed" in feed_urls
    # Queries: 2 from domains + 1 extra.
    queries_str = [q.query for q in queries]
    assert '"design lead" hires' in queries_str
    assert '"Head of Data" appointment' in queries_str
    assert '"AI Act" compliance' in queries_str
    # Extra query gets the conservative 60-record cap.
    extra = next(q for q in queries if q.query == '"AI Act" compliance')
    assert extra.max_records == 60


def test_materialize_skips_unknown_domain_id(
    sample_registry: dict[str, DomainSpec],
) -> None:
    selection = PickerSelection(selected_domain_ids=["design_public", "bogus"])
    feeds, _queries = materialize_sources(selection, sample_registry)
    # Only design_public sources land in the feeds; the bogus id is ignored.
    assert {f.url for f in feeds} == {"https://r1.example/feed"}
