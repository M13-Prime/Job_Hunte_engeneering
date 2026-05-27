"""LiteLLM wrapper.

The whole project goes through ``classify()`` for any LLM call so we can swap
providers from .env without touching code. NEVER import a provider-specific
SDK here.
"""

from __future__ import annotations

import json
import os
import re
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

from signal_tracker.classifier.feedback import FeedbackExample
from signal_tracker.classifier.prompts import (
    CLASSIFIER_PROMPT_VERSION,
    render_system_prompt,
    render_user_prompt,
)
from signal_tracker.classifier.schemas import ClassificationResult, ClassifierInput
from signal_tracker.config import UserProfile, get_settings
from signal_tracker.utils.logging import get_logger

# LiteLLM is chatty by default; one INFO line per call buries our own
# structured log. Keep it to warnings / errors.
litellm.suppress_debug_info = True

logger = get_logger(__name__)


class ClassifierError(RuntimeError):
    """Raised when the LLM cannot produce a valid ClassificationResult."""


_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)
_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)


def _extract_json(raw: str) -> str:
    """Pull a JSON object out of a possibly-fenced or preamble-laden response.

    Claude (and a few other models) sometimes ignore ``response_format`` and
    wrap the JSON in markdown fences or prepend a short preamble. We accept
    those forms and fish out the actual object.
    """
    text = raw.strip()
    fence_match = _FENCE_RE.search(text)
    if fence_match:
        text = fence_match.group(1).strip()
    elif not text.startswith("{"):
        obj_match = _JSON_OBJECT_RE.search(text)
        if obj_match:
            text = obj_match.group(0)
    return text


_RETRYABLE: tuple[type[BaseException], ...] = (
    json.JSONDecodeError,
    ValidationError,
    # Network / provider issues that LiteLLM can raise.
    # We catch broadly because litellm exception types vary across versions.
    TimeoutError,
    ConnectionError,
    OSError,
)


_PROVIDER_KEY_ENV: dict[str, str] = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "mistral": "MISTRAL_API_KEY",
}


def _provider_has_credentials(model: str) -> bool:
    """True if the env var required by the provider behind ``model`` is set."""
    if "/" not in model:
        return True
    provider = model.split("/", 1)[0].lower()
    required = _PROVIDER_KEY_ENV.get(provider)
    if required is None:
        return True  # unknown provider (e.g. ollama) — let LiteLLM decide.
    return bool(os.environ.get(required))


def _resolve_fallbacks(model_name: str | None) -> list[str] | None:
    """Build the fallbacks=... arg, dropping any that lack credentials.

    Otherwise LiteLLM happily tries the fallback when the primary fails, hits
    'Missing credentials' on the fallback, and that masking error is what we
    surface — drowning the real reason the primary call failed.
    """
    if not model_name:
        return None
    if _provider_has_credentials(model_name):
        return [model_name]
    logger.warning(
        "llm.fallback_disabled missing_key model=%s",
        model_name,
    )
    return None


async def classify(
    item: ClassifierInput,
    profile: UserProfile,
    *,
    extra_examples: list[FeedbackExample] | None = None,
    user_keywords: dict[str, list[str]] | None = None,
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
    fallbacks = _resolve_fallbacks(settings.llm_fallback_model)

    messages = [
        {
            "role": "system",
            "content": render_system_prompt(
                profile,
                extra_examples=extra_examples,
                user_keywords=user_keywords,
            ),
        },
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
        if not isinstance(text, str):
            raise ClassifierError(
                f"LLM returned non-string content (type={type(text).__name__}, "
                f"repr={text!r:.200})"
            )
        if not text.strip():
            raise ClassifierError(
                f"LLM returned empty/whitespace content (len={len(text)})"
            )

        cleaned = _extract_json(text)
        if not cleaned.strip():
            preview = text[:300].replace("\n", "\\n")
            raise ClassifierError(
                f"LLM returned no JSON-like block. Raw preview: {preview!r}"
            )
        try:
            data = json.loads(cleaned)
        except json.JSONDecodeError as exc:
            preview = text[:300].replace("\n", "\\n")
            logger.warning(
                "llm.classify json_decode_error raw_preview=%s", preview
            )
            # Re-raise as JSONDecodeError (keeps retry behaviour) with the
            # preview embedded so downstream callers can see what the model
            # returned without the warning log being suppressed.
            raise json.JSONDecodeError(
                f"{exc.msg} | Raw preview: {preview!r}", exc.doc, exc.pos
            ) from exc
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
