"""Pydantic schemas for classifier I/O."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, computed_field

SignalType = Literal[
    "executive_change",
    "funding",
    "csrd_publication",
    "hiring_surge",
    "strategic_announcement",
    "acquisition",
    "regulatory",
    "other",
]

RecommendedAction = Literal[
    "contact_immediate",
    "research_first",
    "monitor",
    "ignore",
]


class KeyPerson(BaseModel):
    """Executive / key person referenced in the signal."""

    name: str
    role: str
    is_new_hire: bool = False


class TargetContact(BaseModel):
    """Suggested contact to reach out to for a spontaneous application."""

    name: str | None = None
    role: str | None = None
    rationale: str | None = None


class ClassifierInput(BaseModel):
    """Lightweight DTO passed to the classifier (decoupled from the ORM)."""

    source: str
    url: str
    title: str | None = None
    content: str | None = None
    published_at: datetime | None = None


class ClassificationResult(BaseModel):
    """The strict shape the LLM must return.

    The score weights are documented in the brief:
        total_score = 0.4 * relevance + 0.3 * urgency + 0.3 * fit_with_profile.

    The digest notification threshold is total_score >= 60.
    """

    is_relevant: bool
    signal_type: SignalType
    company_name: str = ""
    company_normalized: str = ""
    key_persons: list[KeyPerson] = Field(default_factory=list)
    relevance_score: float = Field(ge=0, le=100)
    urgency_score: float = Field(ge=0, le=100)
    fit_with_profile_score: float = Field(ge=0, le=100)
    summary_fr: str = ""
    suggested_angle: str | None = None
    recommended_action: RecommendedAction
    target_contact: TargetContact | None = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def total_score(self) -> float:
        return round(
            0.4 * self.relevance_score
            + 0.3 * self.urgency_score
            + 0.3 * self.fit_with_profile_score,
            2,
        )


__all__ = [
    "ClassificationResult",
    "ClassifierInput",
    "KeyPerson",
    "RecommendedAction",
    "SignalType",
    "TargetContact",
]
