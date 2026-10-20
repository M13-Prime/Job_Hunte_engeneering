"""Per-task LLM usage + cost accounting (Phase 13).

Every LLM call site calls ``record_usage(task, model, response)`` right
after ``litellm.acompletion``. Rows land in the ``llm_usage`` table of the
database registered with ``set_usage_db`` (the dashboard and the daily
scripts register theirs; tests that don't register one record nothing).

Cost: LiteLLM's own price map first; for models it doesn't know yet, the
small ``FALLBACK_PRICES`` table below (USD per 1M tokens, checked October
2026 — update it when prices move). Unknown model → cost left NULL.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import case, func, select

from signal_tracker.storage import Database
from signal_tracker.storage.models import LlmUsage
from signal_tracker.utils.logging import get_logger

logger = get_logger(__name__)

# (input, output, cached input) USD per 1M tokens. Keyed by a substring of
# the model id so "anthropic/claude-haiku-5-5" and "claude-haiku-5-5-2026…"
# both match. First match wins: keep more specific keys first.
FALLBACK_PRICES: tuple[tuple[str, float, float, float], ...] = (
    ("claude-haiku-5-5", 0.10, 0.50, 0.01),
    ("claude-sonnet-5-5", 2.00, 10.00, 0.10),
    ("claude-opus-5-5", 4.00, 20.00, 0.20),
    ("claude-haiku-4-5", 1.00, 5.00, 0.10),
    ("claude-sonnet-4-5", 3.00, 15.00, 0.30),
    ("gemini-2.5-flash-lite", 0.10, 0.40, 0.01),
    ("gemini-3.5-flash-lite", 0.30, 2.50, 0.03),
    ("mistral-small", 0.15, 0.60, 0.15),
    ("deepseek", 0.30, 1.20, 0.01),
)

_usage_db: Database | None = None


def set_usage_db(db: Database | None) -> None:
    """Register (or clear) the database usage rows are written to."""
    global _usage_db
    _usage_db = db


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _tokens(response: Any) -> tuple[int, int, int]:
    usage = getattr(response, "usage", None)
    if usage is None and isinstance(response, dict):
        usage = response.get("usage")
    if usage is None:
        return 0, 0, 0

    def _get(obj: Any, name: str) -> Any:
        return obj.get(name) if isinstance(obj, dict) else getattr(obj, name, None)

    prompt = _int(_get(usage, "prompt_tokens"))
    completion = _int(_get(usage, "completion_tokens"))
    cached = _int(_get(usage, "cache_read_input_tokens"))
    details = _get(usage, "prompt_tokens_details")
    if not cached and details is not None:
        cached = _int(_get(details, "cached_tokens"))
    return prompt, completion, cached


def estimate_cost(
    model: str, input_tokens: int, output_tokens: int, cached: int = 0,
) -> float | None:
    """Cost from FALLBACK_PRICES, or None for an unknown model."""
    name = model.lower()
    for key, p_in, p_out, p_cached in FALLBACK_PRICES:
        if key in name:
            fresh = max(0, input_tokens - cached)
            return (fresh * p_in + cached * p_cached + output_tokens * p_out) / 1_000_000
    return None


def record_usage(task: str, model: str, response: Any) -> None:
    """Persist one call's usage. Never raises: accounting must not break a run."""
    if _usage_db is None:
        return
    try:
        input_tokens, output_tokens, cached = _tokens(response)
        if not (input_tokens or output_tokens):
            return  # mocked / streaming response without usage
        actual_model = str(getattr(response, "model", None) or model)
        cost: float | None = None
        try:
            import litellm

            cost = float(litellm.completion_cost(completion_response=response)) or None
        except Exception:
            cost = None
        if cost is None:
            cost = estimate_cost(actual_model, input_tokens, output_tokens, cached)
        with _usage_db.session() as s:
            s.add(LlmUsage(
                task=task[:32], model=actual_model[:128],
                input_tokens=input_tokens, output_tokens=output_tokens,
                cached_tokens=cached, cost_usd=cost,
            ))
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("llm_usage.record_failed error=%s", str(exc)[:200])


@dataclass(slots=True)
class UsageRow:
    task: str
    model: str
    calls: int
    input_tokens: int
    output_tokens: int
    cost_usd: float
    unknown_cost_calls: int


def usage_summary(db: Database, *, days: int = 30) -> list[UsageRow]:
    """Totals per (task, model) over the last `days` days, costliest first."""
    since = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=days)
    with db.session() as s:
        rows = s.execute(
            select(
                LlmUsage.task,
                LlmUsage.model,
                func.count(LlmUsage.id),
                func.coalesce(func.sum(LlmUsage.input_tokens), 0),
                func.coalesce(func.sum(LlmUsage.output_tokens), 0),
                func.coalesce(func.sum(LlmUsage.cost_usd), 0.0),
                func.sum(case((LlmUsage.cost_usd.is_(None), 1), else_=0)),
            )
            .where(LlmUsage.created_at >= since)
            .group_by(LlmUsage.task, LlmUsage.model)
        ).all()
    out = [
        UsageRow(
            task=str(r[0]), model=str(r[1]), calls=int(r[2]),
            input_tokens=int(r[3]), output_tokens=int(r[4]),
            cost_usd=float(r[5]), unknown_cost_calls=int(r[6] or 0),
        )
        for r in rows
    ]
    out.sort(key=lambda u: u.cost_usd, reverse=True)
    return out


__all__ = [
    "FALLBACK_PRICES",
    "UsageRow",
    "estimate_cost",
    "record_usage",
    "set_usage_db",
    "usage_summary",
]
