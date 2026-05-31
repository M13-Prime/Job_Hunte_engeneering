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
