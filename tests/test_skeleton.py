"""Sanity tests for the Phase 0 skeleton."""

from __future__ import annotations

from pathlib import Path

from signal_tracker import __version__
from signal_tracker.config import load_user_profile
from signal_tracker.storage import init_db
from signal_tracker.storage.models import RawItem
from signal_tracker.utils.dedup import raw_item_hash
from signal_tracker.utils.normalize import normalize_company_name


def test_version_is_set() -> None:
    assert __version__ == "0.1.0"


def test_user_profile_loads() -> None:
    profile = load_user_profile()
    assert "Sustainability" in profile.domains
    assert "AI" in profile.domains
    assert any("ESG" in role for role in profile.target_roles)


def test_normalize_company_name() -> None:
    assert normalize_company_name("Carbone 4 SAS") == "carbone 4"
    assert normalize_company_name("Bénéfik SARL") == "benefik"
    assert normalize_company_name("OpenAI, Inc.") == "openai"


def test_raw_item_hash_is_stable() -> None:
    h1 = raw_item_hash("rss", "https://example.com/a")
    h2 = raw_item_hash("rss", "https://example.com/a")
    h3 = raw_item_hash("rss", "https://example.com/b")
    assert h1 == h2
    assert h1 != h3
    assert len(h1) == 64


def test_db_init_creates_schema(tmp_path: Path) -> None:
    db_file = tmp_path / "test.db"
    db = init_db(db_file)
    with db.session() as session:
        session.add(
            RawItem(
                source="test",
                url="https://example.com/x",
                title="t",
                content="c",
                hash=raw_item_hash("test", "https://example.com/x"),
            )
        )
    with db.session() as session:
        assert session.query(RawItem).count() == 1
