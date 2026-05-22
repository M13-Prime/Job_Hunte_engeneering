"""Pydantic models for the jobs module."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

ATS = Literal[
    "greenhouse",
    "lever",
    "workable",
    "ashby",
    "manual",
    "unknown",
]


class JobPosting(BaseModel):
    """A single open position fetched from an ATS public API."""

    ats: ATS
    ats_company_slug: str
    external_id: str
    title: str
    url: str
    location: str | None = None
    department: str | None = None
    description: str | None = None
    posted_at: datetime | None = None


class CompanyJobsResult(BaseModel):
    """Outcome of scraping one company across all configured backends."""

    company: str
    company_normalized: str
    ats: ATS | None = None
    ats_company_slug: str | None = None
    jobs: list[JobPosting] = Field(default_factory=list)
    error: str | None = None

    @property
    def found(self) -> bool:
        return self.ats is not None


__all__ = ["ATS", "CompanyJobsResult", "JobPosting"]
