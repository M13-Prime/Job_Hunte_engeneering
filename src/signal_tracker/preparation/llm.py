"""LiteLLM wrapper for the preparation generator.

Mirrors signal_tracker.classifier.llm: JSON-only output, retry on transient
errors, fallback model support, fence-stripping for chatty providers.
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

from signal_tracker.classifier.llm import _extract_json, _resolve_fallbacks
from signal_tracker.config import UserProfile, get_settings
from signal_tracker.preparation.prompts import SYSTEM_PROMPT, render_user_prompt
from signal_tracker.preparation.schemas import PreparationReport
from signal_tracker.utils.logging import get_logger

logger = get_logger(__name__)


class PreparationError(RuntimeError):
    """Raised when the LLM cannot produce a valid PreparationReport."""


_RETRYABLE: tuple[type[BaseException], ...] = (
    json.JSONDecodeError,
    ValidationError,
    TimeoutError,
    ConnectionError,
    OSError,
)


async def generate_preparation(
    *,
    company_name: str,
    signal_type: str,
    recommended_action: str,
    total_score: float,
    summary_fr: str,
    suggested_angle: str | None,
    source: str,
    url: str | None,
    title: str | None,
    content: str | None,
    profile: UserProfile,
    cv_text: str,
) -> PreparationReport:
    """Run the LLM to produce a structured preparation report."""
    settings = get_settings()
    model = settings.llm_model
    fallbacks = _resolve_fallbacks(settings.llm_fallback_model)

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": render_user_prompt(
                company=company_name,
                signal_type=signal_type,
                recommended_action=recommended_action,
                score=total_score,
                summary=summary_fr,
                angle=suggested_angle,
                source=source,
                url=url,
                title=title,
                content=content,
                domains=list(profile.domains),
                target_roles=list(profile.target_roles),
                geographies=list(profile.geographies),
                cv_text=cv_text,
            ),
        },
    ]

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=8),
        retry=retry_if_exception_type(_RETRYABLE),
        reraise=True,
    )
    async def _attempt() -> PreparationReport:
        start = time.perf_counter()
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "response_format": {"type": "json_object"},
            "temperature": 0.2,
        }
        if fallbacks:
            kwargs["fallbacks"] = fallbacks
        response = await litellm.acompletion(**kwargs)
        latency = time.perf_counter() - start

        text = response.choices[0].message.content
        if not isinstance(text, str) or not text.strip():
            raise PreparationError(
                f"LLM returned empty/non-string content (type={type(text).__name__})"
            )
        cleaned = _extract_json(text)
        try:
            data = json.loads(cleaned)
        except json.JSONDecodeError as exc:
            preview = text[:300].replace("\n", "\\n")
            raise json.JSONDecodeError(
                f"{exc.msg} | preview: {preview!r}", exc.doc, exc.pos
            ) from exc
        report = PreparationReport.model_validate(data)
        logger.info(
            "preparation.generated company=%s latency_ms=%.0f score=%d",
            company_name, latency * 1000, report.profile_fit.overall_fit_score,
        )
        return report

    return await _attempt()
