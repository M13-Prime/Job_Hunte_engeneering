"""Abstract base class for ATS backends."""

from __future__ import annotations

import abc

import httpx

from signal_tracker.jobs.schemas import JobPosting


class JobsBackend(abc.ABC):
    """Talk to one ATS public job board API.

    A concrete backend returns ``None`` from ``list_jobs`` when the slug is
    not known to that provider (typically a 404). It returns an empty list
    when the slug exists but the company currently has no open postings.
    Any non-recoverable error should be raised; the caller handles it.
    """

    #: Short identifier of the provider, e.g. "greenhouse".
    name: str = ""

    @abc.abstractmethod
    async def list_jobs(
        self, slug: str, client: httpx.AsyncClient
    ) -> list[JobPosting] | None:
        raise NotImplementedError


__all__ = ["JobsBackend"]
