"""LinkedIn channel: disabled by default, rate/daily limits, block handling."""

from __future__ import annotations

import pytest

from signal_tracker.agents.tools.base import ToolRegistry
from signal_tracker.config import Settings
from signal_tracker.linkedin.client import LinkedInClient, LinkedInError, build_call
from signal_tracker.linkedin.tools import build_linkedin_tools


def test_disabled_by_default() -> None:
    assert Settings().linkedin_enabled is False
    assert LinkedInClient(Settings()).available() is False


def test_build_call_quotes_values() -> None:
    expr = build_call("search_people", keyword='Acme "CSO"', limit=5)
    assert expr == 'linkedin.search_people(keyword: "Acme \\"CSO\\"", limit: 5)'


@pytest.mark.asyncio
async def test_call_refused_when_disabled() -> None:
    with pytest.raises(LinkedInError):
        await LinkedInClient(Settings()).search_people("x")


@pytest.mark.asyncio
async def test_profile_url_validated() -> None:
    client = LinkedInClient(Settings(linkedin_enabled=True))
    with pytest.raises(LinkedInError):
        await client.get_person_profile("https://example.com/in/x")


@pytest.mark.asyncio
async def test_daily_limit_and_block(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = Settings(
        linkedin_enabled=True, linkedin_daily_limit=1, linkedin_min_interval_seconds=0
    )
    client = LinkedInClient(settings)
    monkeypatch.setattr("shutil.which", lambda _b: "/bin/echo")
    # /bin/echo prints the expression: not JSON -> returned as {"raw": ...}
    assert "raw" in await client.search_people("x")
    with pytest.raises(LinkedInError, match="daily"):
        await client.search_people("y")


def test_check_block_stops_channel() -> None:
    client = LinkedInClient(Settings(linkedin_enabled=True))
    with pytest.raises(LinkedInError):
        client._check_block("Redirected to authwall login")
    assert client._blocked is True


@pytest.mark.asyncio
async def test_keep_contact_validates_and_records() -> None:
    found: list[dict[str, object]] = []
    tools = build_linkedin_tools(LinkedInClient(Settings()), found)
    reg = ToolRegistry()
    reg.extend(tools)
    keep = reg.by_name("keep_contact")
    assert keep is not None
    bad = await keep.execute({
        "name": "A", "role": "CSO", "linkedin_url": "https://x.com/a", "rationale": "r"})
    assert bad.is_error
    ok = await keep.execute({
        "name": "A", "role": "CSO",
        "linkedin_url": "https://www.linkedin.com/in/a", "rationale": "r"})
    assert not ok.is_error and len(found) == 1
