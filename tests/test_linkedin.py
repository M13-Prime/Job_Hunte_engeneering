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


@pytest.mark.asyncio
async def test_all_tools_registered() -> None:
    reg = ToolRegistry()
    reg.extend(build_linkedin_tools(LinkedInClient(Settings()), []))
    names = {t.name for t in reg.tools}
    assert names == {
        "linkedin_search_people", "linkedin_get_profile", "linkedin_company_profile",
        "linkedin_company_posts", "linkedin_search_jobs", "linkedin_job_details",
        "keep_contact",
    }


@pytest.mark.asyncio
async def test_url_guards_on_company_and_job_tools() -> None:
    client = LinkedInClient(Settings(linkedin_enabled=True))
    for coro in (
        client.get_company_profile("https://example.com/x"),
        client.get_company_posts("https://linkedin.com/in/x"),
        client.get_job_details("https://example.com/jobs/1"),
    ):
        with pytest.raises(LinkedInError):
            await coro


def test_company_url_builder() -> None:
    from signal_tracker.linkedin.client import company_url

    assert company_url("acme") == "https://www.linkedin.com/company/acme/"
    assert company_url("https://www.linkedin.com/company/acme") == (
        "https://www.linkedin.com/company/acme/"
    )


@pytest.mark.asyncio
async def test_posts_collector_yields_hashed_items() -> None:
    from signal_tracker.collectors.linkedin_posts import LinkedInPostsCollector, LinkedInWatch

    class FakeClient:
        async def get_company_posts(self, url: str) -> str:
            return "Acme is hiring a head of sustainability for its new ESG team. " * 3

    collector = LinkedInPostsCollector(
        FakeClient(),  # type: ignore[arg-type]
        [LinkedInWatch(name="Acme", slug="acme")],
    )
    items = [item async for item in collector.collect()]
    assert len(items) == 1
    assert items[0].source == "linkedin"
    assert "linkedin.com/company/acme/posts/#h=" in items[0].url
    assert "sustainability" in (items[0].content or "")


@pytest.mark.asyncio
async def test_posts_collector_stops_when_blocked() -> None:
    from signal_tracker.collectors.linkedin_posts import LinkedInPostsCollector, LinkedInWatch

    class BlockedClient:
        async def get_company_posts(self, url: str) -> str:
            raise LinkedInError("LinkedIn blocked or requires login: authwall")

    collector = LinkedInPostsCollector(
        BlockedClient(),  # type: ignore[arg-type]
        [LinkedInWatch(name="A", slug="a"), LinkedInWatch(name="B", slug="b")],
    )
    assert [item async for item in collector.collect()] == []


def test_watchlist_from_yaml_skips_incomplete() -> None:
    from signal_tracker.collectors.linkedin_posts import LinkedInPostsCollector

    watch = LinkedInPostsCollector.watchlist_from_yaml(
        {"companies": [{"name": "A", "slug": "a"}, {"name": "B"}, {"slug": "c"}]}
    )
    assert [(w.name, w.slug) for w in watch] == [("A", "a")]
