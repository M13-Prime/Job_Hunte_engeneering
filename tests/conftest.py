"""Shared pytest fixtures."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from signal_tracker.config import UserProfile, get_settings, load_user_profile
from signal_tracker.storage import Database, init_db


@pytest.fixture(autouse=True)
def _reset_settings_cache() -> Iterator[None]:
    """Clear lru_cache on settings/profile loaders between tests."""
    get_settings.cache_clear()
    load_user_profile.cache_clear()
    yield
    get_settings.cache_clear()
    load_user_profile.cache_clear()


@pytest.fixture()
def tmp_db(tmp_path: Path) -> Database:
    return init_db(tmp_path / "test.db")


@pytest.fixture()
def sample_profile() -> UserProfile:
    return UserProfile(
        domains=["Sustainability", "AI", "ESG"],
        target_roles=["Data Analyst ESG", "Climate Data Scientist"],
        geographies=["France", "Europe"],
        target_company_types=["Climate tech", "ESG SaaS"],
        notes="Test profile.",
    )
