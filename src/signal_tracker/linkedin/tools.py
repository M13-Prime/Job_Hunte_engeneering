"""Agent tools exposing the LinkedIn channel. Only built when enabled."""

from __future__ import annotations

from typing import Any

from signal_tracker.agents.tools.base import Tool, ToolError, tool
from signal_tracker.linkedin.client import LinkedInClient, LinkedInError


def build_linkedin_tools(
    client: LinkedInClient, found: list[dict[str, Any]]
) -> list[Tool]:
    """Return tools bound to ``client``. Contacts the agent keeps are appended to ``found``."""

    @tool(description="Search LinkedIn people by free text (e.g. 'Acme head of "
                      "sustainability'). Max 10 results. Rate-limited: use sparingly.")
    async def linkedin_search_people(keyword: str, limit: int = 5) -> Any:
        try:
            return await client.search_people(keyword, limit)
        except LinkedInError as exc:
            raise ToolError(str(exc)) from exc

    @tool(description="Read ONE LinkedIn profile by URL (linkedin.com/in/...). "
                      "Use only for the best candidate contact.")
    async def linkedin_get_profile(linkedin_url: str) -> Any:
        try:
            return await client.get_person_profile(linkedin_url)
        except LinkedInError as exc:
            raise ToolError(str(exc)) from exc

    @tool(description="Record a verified contact to include in the report. "
                      "Only names/roles actually seen on LinkedIn; never invent.")
    async def keep_contact(
        name: str, role: str, linkedin_url: str, rationale: str
    ) -> dict[str, Any]:
        if "linkedin.com/in/" not in linkedin_url:
            raise ToolError("linkedin_url must be a linkedin.com/in/ URL")
        found.append({
            "name": name, "role": role, "linkedin_url": linkedin_url,
            "rationale": rationale, "confidence": "medium",
        })
        return {"kept": len(found)}

    return [linkedin_search_people, linkedin_get_profile, keep_contact]
