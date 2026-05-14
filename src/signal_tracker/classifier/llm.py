"""LiteLLM wrapper.

The whole project goes through ``classify()`` for any LLM call so we can swap
providers from .env without touching code. NEVER import a provider-specific
SDK here.
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

from signal_tracker.classifier.prompts import (
    CLASSIFIER_PROMPT_VERSION,
    render_system_prompt,
    render_user_prompt,
)
from signal_tracker.classifier.schemas import ClassificationResult, ClassifierInput
from signal_tracker.config import UserProfile, get_settings
from signal_tracker.utils.logging import get_logger

logger = get_logger(__name__)


class ClassifierError(RuntimeError):
    """Raised when the LLM cannot produce a valid ClassificationResult."""


_RETRYABLE: tuple[type[BaseException], ...] = (
    json.JSONDecodeError,
    ValidationError,
    # Network / provider issues that LiteLLM can raise.
    # We catch broadly because litellm exception types vary across versions.
    TimeoutError,
    ConnectionError,
    OSError,
)


async def classify(
    item: ClassifierInput,
    profile: UserProfile,
) -> ClassificationResult:
    """Classify a collected item via the LLM configured in LLM_MODEL.

    - Uses ``litellm.acompletion`` (never a provider-specific SDK).
    - Forces JSON output via ``response_format={"type": "json_object"}``.
    - Retries 3x with exponential backoff on network errors and on JSON /
      Pydantic validation failures.
    - Uses LiteLLM's native ``fallbacks=[...]`` if ``LLM_FALLBACK_MODEL`` is set.
    - Logs per call: model, latency, estimated cost, tokens in/out.
    """
    settings = get_settings()
    model = settings.llm_model
    fallbacks = (
        [settings.llm_fallback_model] if settings.llm_fallback_model else None
    )

    messages = [
        {"role": "system", "content": render_system_prompt(profile)},
        {"role": "user", "content": render_user_prompt(item)},
    ]

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=8),
        retry=retry_if_exception_type(_RETRYABLE),
        reraise=True,
    )
    async def _attempt() -> ClassificationResult:
        start = time.perf_counter()
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "response_format": {"type": "json_object"},
            "temperature": 0.0,
        }
        if fallbacks:
            kwargs["fallbacks"] = fallbacks

        response = await litellm.acompletion(**kwargs)
        latency = time.perf_counter() - start

        choices = response.choices
        text = choices[0].message.content
        if not text:
            raise ClassifierError("Empty response from LLM")

        data = json.loads(text)
        result = ClassificationResult.model_validate(data)

        try:
            cost = float(litellm.completion_cost(completion_response=response))
        except Exception:  # cost calc is best-effort
            cost = 0.0

        usage = getattr(response, "usage", None)
        tokens_in = getattr(usage, "prompt_tokens", None) if usage else None
        tokens_out = getattr(usage, "completion_tokens", None) if usage else None

        logger.info(
            "llm.classify ok",
            extra={
                "prompt_version": CLASSIFIER_PROMPT_VERSION,
                "model": getattr(response, "model", model),
                "latency_sec": round(latency, 3),
                "cost_usd": round(cost, 6),
                "tokens_in": tokens_in,
                "tokens_out": tokens_out,
                "signal_type": result.signal_type,
                "is_relevant": result.is_relevant,
                "total_score": result.total_score,
            },
        )
        return result

    return await _attempt()


__all__ = ["ClassifierError", "classify"]
