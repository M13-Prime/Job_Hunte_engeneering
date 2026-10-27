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

    @tool(description="Read a company's LinkedIn page (about, size, sector). "
                      "Pass https://www.linkedin.com/company/<slug>/.")
    async def linkedin_company_profile(company_url: str) -> Any:
        try:
            return await client.get_company_profile(company_url)
        except LinkedInError as exc:
            raise ToolError(str(exc)) from exc

    @tool(description="Recent posts of a company's LinkedIn page: its news "
                      "(hiring pushes, launches, leadership changes).")
    async def linkedin_company_posts(company_url: str) -> Any:
        try:
            return await client.get_company_posts(company_url)
        except LinkedInError as exc:
            raise ToolError(str(exc)) from exc

    @tool(description="Search LinkedIn job offers by keywords and optional "
                      "location (e.g. 'sustainability manager', 'Paris').")
    async def linkedin_search_jobs(keywords: str, location: str | None = None) -> Any:
        try:
            return await client.search_jobs(keywords, location)
        except LinkedInError as exc:
            raise ToolError(str(exc)) from exc

    @tool(description="Read ONE LinkedIn job offer by URL (linkedin.com/jobs/...): "
                      "team, department, requirements, recruiter if shown.")
    async def linkedin_job_details(job_url: str) -> Any:
        try:
            return await client.get_job_details(job_url)
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

    return [
        linkedin_search_people, linkedin_get_profile, linkedin_company_profile,
        linkedin_company_posts, linkedin_search_jobs, linkedin_job_details,
        keep_contact,
    ]
