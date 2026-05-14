"""Tests for the LiteLLM wrapper (mocked, no network calls).

Includes 10 canned scenarios (5 positive, 5 negative) — the brief requires the
classifier to be exercised with that many cases. The LLM call is mocked, so
these tests pin down the *schema contract* of the wrapper, not the quality
of any real model.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from signal_tracker.classifier.llm import ClassifierError, classify
from signal_tracker.classifier.schemas import ClassifierInput
from signal_tracker.config import UserProfile

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fake_response(payload: dict[str, Any], *, model: str = "test/model") -> Any:
    """Build an object that quacks like a LiteLLM response."""
    return SimpleNamespace(
        choices=[
            SimpleNamespace(message=SimpleNamespace(content=json.dumps(payload)))
        ],
        usage=SimpleNamespace(prompt_tokens=100, completion_tokens=50),
        model=model,
    )


def _make_input(title: str, content: str = "") -> ClassifierInput:
    return ClassifierInput(
        source="rss:test",
        url=f"https://example.com/{title.lower().replace(' ', '-')}",
        title=title,
        content=content,
    )


@pytest.fixture()
def patched_litellm(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """Patch ``litellm.acompletion`` and ``litellm.completion_cost``."""
    mock_call = AsyncMock()
    monkeypatch.setattr("signal_tracker.classifier.llm.litellm.acompletion", mock_call)
    monkeypatch.setattr(
        "signal_tracker.classifier.llm.litellm.completion_cost",
        lambda completion_response=None: 0.0001,
    )
    return mock_call


# ---------------------------------------------------------------------------
# Canned scenarios (5 positive, 5 negative)
# ---------------------------------------------------------------------------

POSITIVE_CASES: list[tuple[str, dict[str, Any]]] = [
    (
        "Carbone 4 nomme une nouvelle Directrice ESG",
        {
            "is_relevant": True,
            "signal_type": "executive_change",
            "company_name": "Carbone 4",
            "company_normalized": "carbone 4",
            "key_persons": [
                {"name": "Camille Dupont", "role": "Directrice ESG", "is_new_hire": True}
            ],
            "relevance_score": 95,
            "urgency_score": 90,
            "fit_with_profile_score": 92,
            "summary_fr": "Carbone 4 cree un poste de Directrice ESG.",
            "suggested_angle": "Feliciter pour la nomination et proposer un profil data ESG.",
            "recommended_action": "contact_immediate",
            "target_contact": {
                "name": "Camille Dupont",
                "role": "Directrice ESG",
                "rationale": "Constitue son equipe.",
            },
        },
    ),
    (
        "Sweep leve 22M EUR en Serie B",
        {
            "is_relevant": True,
            "signal_type": "funding",
            "company_name": "Sweep",
            "company_normalized": "sweep",
            "key_persons": [],
            "relevance_score": 88,
            "urgency_score": 78,
            "fit_with_profile_score": 90,
            "summary_fr": "Sweep leve 22M EUR pour doubler ses equipes data.",
            "suggested_angle": "Mentionner la levee pour pousser un profil data ESG.",
            "recommended_action": "research_first",
            "target_contact": {
                "name": None,
                "role": "Head of Data",
                "rationale": "Recrutement post-levee.",
            },
        },
    ),
    (
        "Le groupe Bertin publie son premier rapport CSRD",
        {
            "is_relevant": True,
            "signal_type": "csrd_publication",
            "company_name": "Groupe Bertin",
            "company_normalized": "bertin",
            "key_persons": [],
            "relevance_score": 75,
            "urgency_score": 60,
            "fit_with_profile_score": 70,
            "summary_fr": "Premier rapport CSRD, besoin de structurer l'equipe ESG.",
            "suggested_angle": "Proposer un appui methodologique pour les futurs exercices.",
            "recommended_action": "research_first",
            "target_contact": None,
        },
    ),
    (
        "EcoVadis lance une practice IA appliquee a l'ESG",
        {
            "is_relevant": True,
            "signal_type": "strategic_announcement",
            "company_name": "EcoVadis",
            "company_normalized": "ecovadis",
            "key_persons": [],
            "relevance_score": 82,
            "urgency_score": 70,
            "fit_with_profile_score": 88,
            "summary_fr": "EcoVadis lance une practice AI for Sustainability.",
            "suggested_angle": "Postuler comme premier data scientist de la practice.",
            "recommended_action": "research_first",
            "target_contact": None,
        },
    ),
    (
        "Plan A rachete Climatiq pour consolider sa plateforme carbone",
        {
            "is_relevant": True,
            "signal_type": "acquisition",
            "company_name": "Plan A",
            "company_normalized": "plan a",
            "key_persons": [],
            "relevance_score": 80,
            "urgency_score": 65,
            "fit_with_profile_score": 85,
            "summary_fr": "Plan A acquiert Climatiq, integration data en perspective.",
            "suggested_angle": "Cibler l'integration des plateformes en proposant un profil data.",
            "recommended_action": "monitor",
            "target_contact": None,
        },
    ),
]

NEGATIVE_CASES: list[tuple[str, dict[str, Any]]] = [
    (
        "Apple sort un nouvel iPhone",
        {
            "is_relevant": False,
            "signal_type": "other",
            "company_name": "",
            "company_normalized": "",
            "key_persons": [],
            "relevance_score": 0,
            "urgency_score": 0,
            "fit_with_profile_score": 0,
            "summary_fr": "Lancement produit, sans lien.",
            "suggested_angle": None,
            "recommended_action": "ignore",
            "target_contact": None,
        },
    ),
    (
        "Resultats sportifs du week-end",
        {
            "is_relevant": False,
            "signal_type": "other",
            "company_name": "",
            "company_normalized": "",
            "key_persons": [],
            "relevance_score": 0,
            "urgency_score": 0,
            "fit_with_profile_score": 0,
            "summary_fr": "Sport - hors perimetre.",
            "suggested_angle": None,
            "recommended_action": "ignore",
            "target_contact": None,
        },
    ),
    (
        "Recette de cuisine veggie",
        {
            "is_relevant": False,
            "signal_type": "other",
            "company_name": "",
            "company_normalized": "",
            "key_persons": [],
            "relevance_score": 0,
            "urgency_score": 0,
            "fit_with_profile_score": 0,
            "summary_fr": "Contenu lifestyle.",
            "suggested_angle": None,
            "recommended_action": "ignore",
            "target_contact": None,
        },
    ),
    (
        "Petite annonce immobiliere a Bordeaux",
        {
            "is_relevant": False,
            "signal_type": "other",
            "company_name": "",
            "company_normalized": "",
            "key_persons": [],
            "relevance_score": 0,
            "urgency_score": 0,
            "fit_with_profile_score": 0,
            "summary_fr": "Immobilier - hors perimetre.",
            "suggested_angle": None,
            "recommended_action": "ignore",
            "target_contact": None,
        },
    ),
    (
        "Cinema : sortie d'un nouveau blockbuster",
        {
            "is_relevant": False,
            "signal_type": "other",
            "company_name": "",
            "company_normalized": "",
            "key_persons": [],
            "relevance_score": 0,
            "urgency_score": 0,
            "fit_with_profile_score": 0,
            "summary_fr": "Culture - hors perimetre.",
            "suggested_angle": None,
            "recommended_action": "ignore",
            "target_contact": None,
        },
    ),
]


@pytest.mark.parametrize(("title", "payload"), POSITIVE_CASES)
async def test_positive_cases_parse(
    patched_litellm: AsyncMock,
    sample_profile: UserProfile,
    title: str,
    payload: dict[str, Any],
) -> None:
    patched_litellm.return_value = _fake_response(payload)
    result = await classify(_make_input(title), sample_profile)
    assert result.is_relevant is True
    assert result.signal_type != "other" or result.company_name != ""
    assert 0 <= result.total_score <= 100
    assert result.recommended_action in {
        "contact_immediate",
        "research_first",
        "monitor",
    }


@pytest.mark.parametrize(("title", "payload"), NEGATIVE_CASES)
async def test_negative_cases_parse(
    patched_litellm: AsyncMock,
    sample_profile: UserProfile,
    title: str,
    payload: dict[str, Any],
) -> None:
    patched_litellm.return_value = _fake_response(payload)
    result = await classify(_make_input(title), sample_profile)
    assert result.is_relevant is False
    assert result.recommended_action == "ignore"
    assert result.total_score == 0


# ---------------------------------------------------------------------------
# Retry / error behavior
# ---------------------------------------------------------------------------


async def test_retries_on_malformed_json_then_succeeds(
    patched_litellm: AsyncMock,
    sample_profile: UserProfile,
) -> None:
    bad = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="not json {"))],
        usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
        model="test/model",
    )
    good = _fake_response(POSITIVE_CASES[0][1])
    patched_litellm.side_effect = [bad, good]
    result = await classify(_make_input("Carbone 4 nomme"), sample_profile)
    assert result.is_relevant is True
    assert patched_litellm.await_count == 2


async def test_retries_on_validation_error_then_succeeds(
    patched_litellm: AsyncMock,
    sample_profile: UserProfile,
) -> None:
    invalid = _fake_response({"is_relevant": True})  # missing required keys
    good = _fake_response(POSITIVE_CASES[1][1])
    patched_litellm.side_effect = [invalid, good]
    result = await classify(_make_input("Sweep leve"), sample_profile)
    assert result.signal_type == "funding"
    assert patched_litellm.await_count == 2


async def test_gives_up_after_three_failures(
    patched_litellm: AsyncMock,
    sample_profile: UserProfile,
) -> None:
    bad = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="{"))],
        usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
        model="test/model",
    )
    patched_litellm.return_value = bad
    with pytest.raises(json.JSONDecodeError):
        await classify(_make_input("x"), sample_profile)
    assert patched_litellm.await_count == 3


async def test_empty_response_raises_classifier_error(
    patched_litellm: AsyncMock,
    sample_profile: UserProfile,
) -> None:
    empty = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=""))],
        usage=SimpleNamespace(prompt_tokens=1, completion_tokens=0),
        model="test/model",
    )
    patched_litellm.return_value = empty
    with pytest.raises(ClassifierError):
        await classify(_make_input("x"), sample_profile)


async def test_fallback_model_passed_to_litellm(
    monkeypatch: pytest.MonkeyPatch,
    patched_litellm: AsyncMock,
    sample_profile: UserProfile,
) -> None:
    monkeypatch.setenv("LLM_MODEL", "anthropic/claude-sonnet-4-5")
    monkeypatch.setenv("LLM_FALLBACK_MODEL", "openai/gpt-4o-mini")
    from signal_tracker.config import get_settings

    get_settings.cache_clear()

    patched_litellm.return_value = _fake_response(POSITIVE_CASES[0][1])
    await classify(_make_input("x"), sample_profile)
    await_args = patched_litellm.await_args
    assert await_args is not None
    assert await_args.kwargs["fallbacks"] == ["openai/gpt-4o-mini"]
    assert await_args.kwargs["response_format"] == {"type": "json_object"}
    assert await_args.kwargs["model"] == "anthropic/claude-sonnet-4-5"
