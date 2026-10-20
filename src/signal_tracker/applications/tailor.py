"""LLM call: tailor the user's CV to a posting + write the cover letter.

Hard rule enforced by the prompt (and surfaced in the UI): the model may
reorder, select, condense and reword what is in the CV, and mirror the
posting's vocabulary when the CV supports it — it must never add an
employer, title, date, degree, metric or skill that isn't in the CV.
Anything the posting asks for that the CV lacks goes in `gaps`.
"""

from __future__ import annotations

import json
import time
from typing import Any

import litellm
from pydantic import ValidationError
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from signal_tracker.applications.schemas import TailoredDocuments
from signal_tracker.classifier.llm import _extract_json, _resolve_fallbacks
from signal_tracker.config import get_settings, model_for
from signal_tracker.utils.llm_usage import record_usage
from signal_tracker.utils.logging import get_logger

logger = get_logger(__name__)


class TailoringError(RuntimeError):
    """Raised when the LLM cannot produce valid tailored documents."""


SYSTEM_PROMPT = """\
Tu es un expert en recrutement. Tu adaptes le CV d'un candidat à UNE offre
d'emploi précise et tu rédiges sa lettre de motivation.

RÈGLE ABSOLUE — zéro invention :
- Utilise UNIQUEMENT les faits présents dans le CV source : employeurs,
  intitulés, dates, diplômes, chiffres, outils, compétences.
- Tu peux : sélectionner, réordonner, condenser, reformuler, mettre en avant
  ce qui colle à l'offre, et reprendre le vocabulaire de l'offre QUAND le CV
  le justifie (ex. le CV dit "pipelines Databricks", l'offre dit "data
  engineering" → OK).
- Tu ne peux pas : ajouter une expérience, un outil, un chiffre, une
  certification ou une langue absents du CV, ni gonfler un niveau.
- Toute exigence de l'offre non couverte par le CV va dans "gaps", jamais
  dans le CV ni dans la lettre.

CV adapté (format ATS, une colonne, pas de tableau) :
- headline : intitulé visé, aligné sur l'offre si le parcours le justifie.
- summary : 3-4 lignes, les 2-3 arguments les plus forts pour CE poste.
- skills : 8-14 compétences du CV, les plus pertinentes pour l'offre d'abord.
- experiences : les expériences du CV (les plus pertinentes détaillées,
  les autres condensées), 2-5 bullets chacune, verbe d'action + résultat
  tel qu'écrit dans le CV.
- education / languages / certifications : repris du CV.

Lettre de motivation :
- 220-320 mots, ton direct et professionnel, pas de formules creuses.
- Structure : accroche liée à l'entreprise/au poste → 2 preuves concrètes
  tirées du CV → ce que le candidat apporte → appel à l'échange.
- Si un signal récent sur l'entreprise est fourni, il peut servir d'accroche.
- Pas d'adresse postale ni de date en en-tête ; commence par "Madame,
  Monsieur," (FR) ou "Dear Hiring Team," (EN) sauf si un nom est fourni.

Langue : celle de l'offre (fr ou en). Le CV et la lettre dans cette langue.

Sortie : JSON strict, rien d'autre :
{
  "language": "fr" | "en",
  "cv": {
    "name": str | null,
    "headline": str,
    "contact_line": str | null,
    "summary": str,
    "skills": [str],
    "experiences": [{"title": str, "company": str, "period": str | null,
                     "location": str | null, "bullets": [str]}],
    "education": [str],
    "languages": [str],
    "certifications": [str]
  },
  "cover_letter": str,
  "changes": ["3-6 phrases courtes : ce que tu as changé vs le CV source et pourquoi"],
  "gaps": ["exigences de l'offre non prouvées par le CV (vide si aucune)"]
}
"""


def render_user_prompt(
    *,
    company: str,
    title: str,
    location: str | None,
    description: str | None,
    cv_text: str,
    signal_summary: str | None = None,
) -> str:
    parts = [
        f"ENTREPRISE : {company}",
        f"POSTE : {title}",
        f"LIEU : {location or '(non précisé)'}",
    ]
    if signal_summary:
        parts.append(f"SIGNAL RÉCENT SUR L'ENTREPRISE : {signal_summary}")
    parts.append(f"\nOFFRE (texte) :\n{(description or '(pas de description)')[:6000]}")
    parts.append(f"\nCV SOURCE (seule source de faits autorisée) :\n{cv_text[:12000]}")
    parts.append("\nProduis le JSON.")
    return "\n".join(parts)


_RETRYABLE: tuple[type[BaseException], ...] = (
    json.JSONDecodeError,
    ValidationError,
    TimeoutError,
    ConnectionError,
    OSError,
)


async def generate_tailored_documents(
    *,
    company: str,
    title: str,
    location: str | None,
    description: str | None,
    cv_text: str,
    signal_summary: str | None = None,
) -> TailoredDocuments:
    """One LLM call (main model — this is writing, not extraction)."""
    if not cv_text.strip():
        raise TailoringError("Aucun CV enregistré : ajoute ton CV d'abord.")
    settings = get_settings()
    model = model_for("write", settings)
    fallbacks = _resolve_fallbacks(settings.llm_fallback_model)
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": render_user_prompt(
            company=company, title=title, location=location,
            description=description, cv_text=cv_text,
            signal_summary=signal_summary,
        )},
    ]

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=8),
        retry=retry_if_exception_type(_RETRYABLE),
        reraise=True,
    )
    async def _attempt() -> TailoredDocuments:
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "response_format": {"type": "json_object"},
            "temperature": 0.3,
            "max_tokens": 4000,
        }
        if fallbacks:
            kwargs["fallbacks"] = fallbacks
        start = time.perf_counter()
        response = await litellm.acompletion(**kwargs)
        latency = time.perf_counter() - start
        record_usage("write", model, response)
        text = response.choices[0].message.content
        if not isinstance(text, str) or not text.strip():
            raise TailoringError(
                f"LLM returned empty/non-string content (type={type(text).__name__})"
            )
        data = json.loads(_extract_json(text))
        docs = TailoredDocuments.model_validate(data)
        logger.info(
            "applications.tailored company=%s latency_ms=%.0f gaps=%d",
            company, latency * 1000, len(docs.gaps),
        )
        return docs

    try:
        return await _attempt()
    except TailoringError:
        raise
    except Exception as exc:
        raise TailoringError(f"La génération a échoué : {str(exc)[:300]}") from exc


__all__ = [
    "SYSTEM_PROMPT",
    "TailoringError",
    "generate_tailored_documents",
    "render_user_prompt",
]
