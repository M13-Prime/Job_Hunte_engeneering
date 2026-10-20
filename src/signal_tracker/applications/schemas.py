"""Pydantic schemas for the tailored CV + cover letter."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class TailoredExperience(BaseModel):
    title: str
    company: str
    period: str | None = None
    location: str | None = None
    bullets: list[str] = Field(default_factory=list)


class TailoredCV(BaseModel):
    """A CV rewritten for one posting. Every fact must come from the source CV."""

    name: str | None = None
    headline: str
    contact_line: str | None = None
    summary: str
    skills: list[str] = Field(default_factory=list)
    experiences: list[TailoredExperience] = Field(default_factory=list)
    education: list[str] = Field(default_factory=list)
    languages: list[str] = Field(default_factory=list)
    certifications: list[str] = Field(default_factory=list)


class TailoredDocuments(BaseModel):
    """LLM output: tailored CV + cover letter + transparency notes."""

    language: Literal["fr", "en"] = "fr"
    cv: TailoredCV
    cover_letter: str
    # What was changed vs the source CV and why (shown to the user).
    changes: list[str] = Field(default_factory=list)
    # Requirements of the posting the CV doesn't evidence — the model
    # must list them here instead of inventing experience.
    gaps: list[str] = Field(default_factory=list)


__all__ = ["TailoredCV", "TailoredDocuments", "TailoredExperience"]
