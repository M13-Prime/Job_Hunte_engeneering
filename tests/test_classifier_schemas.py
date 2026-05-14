"""Schema-level tests for classifier I/O."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from signal_tracker.classifier.schemas import (
    ClassificationResult,
    KeyPerson,
    TargetContact,
)


def test_total_score_weighted_correctly() -> None:
    result = ClassificationResult(
        is_relevant=True,
        signal_type="executive_change",
        company_name="Carbone 4",
        company_normalized="carbone 4",
        relevance_score=80,
        urgency_score=70,
        fit_with_profile_score=90,
        summary_fr="...",
        recommended_action="contact_immediate",
    )
    # 0.4*80 + 0.3*70 + 0.3*90 = 32 + 21 + 27 = 80.0
    assert result.total_score == pytest.approx(80.0)


def test_irrelevant_payload_validates() -> None:
    result = ClassificationResult(
        is_relevant=False,
        signal_type="other",
        relevance_score=0,
        urgency_score=0,
        fit_with_profile_score=0,
        recommended_action="ignore",
    )
    assert result.total_score == 0
    assert result.company_name == ""


def test_score_bounds_enforced() -> None:
    with pytest.raises(ValidationError):
        ClassificationResult(
            is_relevant=True,
            signal_type="funding",
            relevance_score=120,
            urgency_score=10,
            fit_with_profile_score=10,
            recommended_action="monitor",
        )


def test_invalid_signal_type_rejected() -> None:
    with pytest.raises(ValidationError):
        ClassificationResult(
            is_relevant=True,
            signal_type="not_a_real_type",
            relevance_score=10,
            urgency_score=10,
            fit_with_profile_score=10,
            recommended_action="monitor",
        )


def test_nested_models_dump_correctly() -> None:
    result = ClassificationResult(
        is_relevant=True,
        signal_type="executive_change",
        company_name="Sweep",
        company_normalized="sweep",
        key_persons=[KeyPerson(name="A B", role="CSO", is_new_hire=True)],
        target_contact=TargetContact(name="A B", role="CSO", rationale="r"),
        relevance_score=90,
        urgency_score=80,
        fit_with_profile_score=85,
        summary_fr="...",
        recommended_action="contact_immediate",
    )
    dumped = result.model_dump()
    assert dumped["key_persons"][0]["is_new_hire"] is True
    assert dumped["target_contact"]["name"] == "A B"
