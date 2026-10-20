"""Pydantic schemas for the CV-based preparation report."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class Executive(BaseModel):
    name: str
    role: str
    background: str | None = None


class ApproachStep(BaseModel):
    order: int = Field(ge=1)
    title: str
    detail: str


class ContactSuggestion(BaseModel):
    """A suggested person to reach out to. Empty list = no contact found."""

    name: str | None = None
    role: str | None = None
    rationale: str | None = None
    confidence: Literal["high", "medium", "low", "unknown"] = "unknown"


class ProfileFit(BaseModel):
    overall_fit_score: int = Field(ge=0, le=100)
    overall_rationale: str
    strong_points: list[str] = Field(default_factory=list)
    improvement_areas: list[str] = Field(default_factory=list)


class CompanyIntel(BaseModel):
    """What we know about the company. All optional — say so when unknown."""

    structure: str | None = None
    team_structure: str | None = None
    top_management: list[Executive] = Field(default_factory=list)
    notes: str | None = None
    # Career / jobs page URL. The LLM is instructed to leave these null
    # rather than invent — we render a Google-search fallback regardless.
    career_page_url: str | None = None
    career_page_confidence: Literal["high", "medium", "low", "unknown"] = "unknown"
    # Longer-form analysis (sector position, competitive landscape, hiring
    # patterns, risks). Rendered inside a collapsible <details> block.
    deep_dive: str | None = None


class ApproachPlan(BaseModel):
    optimal_approach: str
    steps: list[ApproachStep] = Field(default_factory=list)
    talking_points: list[str] = Field(default_factory=list)
    first_message_template: str


class PreparationReport(BaseModel):
    """Full structured plan rendered on the preparation page."""

    headline: str
    profile_fit: ProfileFit
    company_intel: CompanyIntel
    approach_plan: ApproachPlan
    contacts: list[ContactSuggestion] = Field(default_factory=list)


class CVRole(BaseModel):
    title: str
    company: str
    period: str | None = None
    achievements: list[str] = Field(default_factory=list)


class CVSuggestedKeywords(BaseModel):
    """Search-keyword suggestions derived from the CV (Phase 10).

    Each list is a short set of terms — at most 8 per bucket — the user
    can one-click add to their search keywords. Categories map directly
    to the existing UserKeyword.category column so insertion is trivial.
    """

    field: list[str] = Field(default_factory=list)       # sectors / domains
    job_title: list[str] = Field(default_factory=list)   # role variants
    other: list[str] = Field(default_factory=list)       # techs, methods, frameworks


class CVProfile(BaseModel):
    """Compact structured CV representation cached on UserCV.

    Generated once when the user saves a CV. Re-used as the `cv_text` payload
    on every subsequent preparation, so we never re-send the ~3k-token raw
    CV body to the model.
    """

    name: str | None = None
    headline: str | None = None
    years_experience: int | None = Field(default=None, ge=0, le=80)
    skills: list[str] = Field(default_factory=list)
    languages: list[str] = Field(default_factory=list)
    education: list[str] = Field(default_factory=list)
    top_roles: list[CVRole] = Field(default_factory=list)
    notable_achievements: list[str] = Field(default_factory=list)
    # Phase 10 — keyword suggestions the user can drop straight into their
    # search keyword editor without re-typing them.
    suggested_keywords: CVSuggestedKeywords = Field(default_factory=CVSuggestedKeywords)
