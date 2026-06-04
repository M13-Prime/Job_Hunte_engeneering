"""Per-search dynamic source picker (Phase 10 feature 3).

At search-launch time, an LLM picks which curated domain(s) match the
launching user's keywords + profile, and optionally generates a small
set of extra GDELT queries from their specific keywords. The collectors
then run on the *union* of selected domains' sources, keeping the
corpus tight and relevant.

Hybrid design:
- Curated registry (config/source_registry.yaml) holds vetted RSS feeds
  + GDELT queries tagged by domain.
- LLM agent picks domains + drafts extra queries — never invents URLs.

The agent is opt-in via env (LLM_SOURCE_PICKER_ENABLED). When disabled,
or when the LLM call fails, the picker falls back to all registered
domains — same behavior as the legacy static config.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import litellm
import yaml
from pydantic import BaseModel, Field, ValidationError

from signal_tracker.classifier.llm import (
    _build_system_content,
    _extract_json,
    _resolve_fallbacks,
)
from signal_tracker.collectors.gdelt import GdeltQuery
from signal_tracker.collectors.rss import FeedConfig
from signal_tracker.config import CONFIG_DIR, UserProfile, get_settings
from signal_tracker.utils.logging import get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Registry data classes
# ---------------------------------------------------------------------------


class DomainSpec(BaseModel):
    """One curated domain block from source_registry.yaml."""

    domain_id: str
    label: str
    description: str
    keywords: list[str] = Field(default_factory=list)
    rss: list[dict[str, Any]] = Field(default_factory=list)
    gdelt_queries: list[dict[str, Any]] = Field(default_factory=list)

    def feed_configs(self) -> list[FeedConfig]:
        return [FeedConfig.from_dict(raw) for raw in self.rss]

    def gdelt_query_objects(self) -> list[GdeltQuery]:
        out: list[GdeltQuery] = []
        for raw in self.gdelt_queries:
            out.append(GdeltQuery(
                id=str(raw["id"]),
                query=str(raw["query"]),
                timespan=str(raw.get("timespan", "24h")),
                max_records=int(raw.get("max_records", 100)),
            ))
        return out


class PickerSelection(BaseModel):
    """What the picker decided for one search run."""

    selected_domain_ids: list[str] = Field(default_factory=list)
    extra_gdelt_queries: list[str] = Field(default_factory=list)
    rationale: str = ""

    def to_metadata(self) -> dict[str, Any]:
        return {
            "selected_domain_ids": self.selected_domain_ids,
            "extra_gdelt_queries": self.extra_gdelt_queries,
            "rationale": self.rationale,
        }


# ---------------------------------------------------------------------------
# Registry loader
# ---------------------------------------------------------------------------


def load_source_registry(
    path: str | Path | None = None,
) -> dict[str, DomainSpec]:
    """Load the curated domain registry from YAML.

    Missing file → empty registry (caller should fall back to the
    legacy static config in that case).
    """
    registry_path = Path(path) if path else CONFIG_DIR / "source_registry.yaml"
    if not registry_path.exists():
        logger.warning(
            "source_picker.registry_missing path=%s", registry_path,
        )
        return {}
    with registry_path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    raw_domains = data.get("domains") or {}
    if not isinstance(raw_domains, dict):
        raise ValueError(
            f"Expected 'domains' mapping in {registry_path}, got {type(raw_domains)}"
        )
    out: dict[str, DomainSpec] = {}
    for domain_id, raw in raw_domains.items():
        if not isinstance(raw, dict):
            continue
        out[str(domain_id)] = DomainSpec(domain_id=str(domain_id), **raw)
    return out


# ---------------------------------------------------------------------------
# LLM picker
# ---------------------------------------------------------------------------


_SYSTEM_PROMPT = """\
You decide which curated news-source DOMAINS to activate for a job-signal
search run, based on the user's profile + their saved keywords.

You are given:
- The user profile (domains of interest, target roles, geographies).
- The user's saved keywords (fields / job titles / other).
- A list of available DOMAINS with a short description + example keywords.

Your output picks the SMALLEST set of domain_ids that covers the user's
keywords, plus an optional handful of extra GDELT queries you'd generate
from their specific keywords.

HARD RULES
1. Output strict JSON. No prose.
2. Always include "fr_tech_general" if it exists — it's a safety-net
   domain. Never pick it as the ONLY domain unless nothing else fits.
3. Prefer fewer domains: 1-3 specialized + the safety net is ideal,
   not all of them.
4. extra_gdelt_queries: 0-3 items, each a single line GDELT-syntax string
   built from the user's *specific* keywords (e.g. company names, tool
   names, role variants that don't appear in any domain's queries).
   Empty list when nothing useful to add.
5. rationale: 1 short French sentence ("Tutoiement", informal "tu"),
   explaining why you picked those domains.

OUTPUT
{
  "selected_domain_ids": ["string", ...],
  "extra_gdelt_queries": ["string", ...],
  "rationale": "1 phrase FR"
}
"""


def _render_user_prompt(
    profile: UserProfile,
    user_keywords: dict[str, list[str]],
    registry: dict[str, DomainSpec],
) -> str:
    domains_block = []
    for d in registry.values():
        kws = ", ".join(d.keywords[:8]) if d.keywords else "(none)"
        domains_block.append(
            f"- {d.domain_id} ({d.label}) — {d.description.strip()}\n"
            f"  keywords: {kws}"
        )
    kw_lines = []
    for cat, label in (
        ("field", "Fields"),
        ("job_title", "Job titles"),
        ("other", "Other"),
    ):
        items = user_keywords.get(cat) or []
        kw_lines.append(f"  {label}: {', '.join(items) or '(none)'}")
    return (
        "USER PROFILE\n"
        f"  Domains: {', '.join(profile.domains) or '(none)'}\n"
        f"  Target roles: {', '.join(profile.target_roles) or '(none)'}\n"
        f"  Geographies: {', '.join(profile.geographies) or '(none)'}\n\n"
        "USER KEYWORDS\n"
        + "\n".join(kw_lines)
        + "\n\nAVAILABLE DOMAINS\n"
        + "\n".join(domains_block)
        + "\n\nDecide."
    )


def _fallback_selection(registry: dict[str, DomainSpec]) -> PickerSelection:
    """When the picker is disabled or the LLM fails, activate everything
    — same effective behavior as the legacy static config."""
    return PickerSelection(
        selected_domain_ids=list(registry.keys()),
        extra_gdelt_queries=[],
        rationale="Tous les domaines activés (sélecteur LLM désactivé ou échec).",
    )


async def pick_sources(
    profile: UserProfile,
    user_keywords: dict[str, list[str]],
    registry: dict[str, DomainSpec] | None = None,
) -> PickerSelection:
    """Ask the LLM which domains + extra queries to use for this search."""
    registry = registry if registry is not None else load_source_registry()
    if not registry:
        return PickerSelection(
            rationale="Aucun registre de sources — fallback config statique.",
        )

    settings = get_settings()
    if not settings.llm_source_picker_enabled:
        return _fallback_selection(registry)

    model = settings.llm_cheap_model or settings.llm_model
    fallbacks = _resolve_fallbacks(settings.llm_fallback_model)
    messages = [
        {
            "role": "system",
            "content": _build_system_content(
                model, _SYSTEM_PROMPT,
                cache=settings.llm_prompt_cache_enabled,
            ),
        },
        {"role": "user", "content": _render_user_prompt(profile, user_keywords, registry)},
    ]
    kwargs: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "response_format": {"type": "json_object"},
        "temperature": 0.0,
        "max_tokens": 400,
    }
    if fallbacks:
        kwargs["fallbacks"] = fallbacks

    try:
        start = time.perf_counter()
        response = await litellm.acompletion(**kwargs)
        latency = time.perf_counter() - start
        text = response.choices[0].message.content
        if not isinstance(text, str) or not text.strip():
            raise ValueError("empty response")
        data = json.loads(_extract_json(text))
        result = PickerSelection.model_validate(data)
    except (json.JSONDecodeError, ValidationError, ValueError, Exception) as exc:
        logger.warning("source_picker.failed_open error=%s", str(exc)[:160])
        return _fallback_selection(registry)

    # Drop any domain IDs the LLM hallucinated.
    known = set(registry.keys())
    result.selected_domain_ids = [d for d in result.selected_domain_ids if d in known]
    if not result.selected_domain_ids:
        return _fallback_selection(registry)

    logger.info(
        "source_picker.selected domains=%s extra_queries=%d latency_sec=%.2f",
        result.selected_domain_ids, len(result.extra_gdelt_queries), latency,
    )
    return result


# ---------------------------------------------------------------------------
# Materialization
# ---------------------------------------------------------------------------


def materialize_sources(
    selection: PickerSelection,
    registry: dict[str, DomainSpec],
) -> tuple[list[FeedConfig], list[GdeltQuery]]:
    """Turn a PickerSelection into concrete collector inputs.

    De-duplicates RSS feeds by URL and GDELT queries by query string so
    a feed/query appearing under multiple domains is fetched exactly once.
    Adds the LLM-drafted extra GDELT queries.
    """
    seen_urls: set[str] = set()
    feeds: list[FeedConfig] = []
    seen_queries: set[str] = set()
    queries: list[GdeltQuery] = []

    for domain_id in selection.selected_domain_ids:
        domain = registry.get(domain_id)
        if domain is None:
            continue
        for fc in domain.feed_configs():
            if fc.url in seen_urls:
                continue
            seen_urls.add(fc.url)
            feeds.append(fc)
        for q in domain.gdelt_query_objects():
            if q.query in seen_queries:
                continue
            seen_queries.add(q.query)
            queries.append(q)

    # LLM-drafted extras — short timespan, conservative cap.
    for i, raw_query in enumerate(selection.extra_gdelt_queries):
        q = (raw_query or "").strip()
        if not q or q in seen_queries:
            continue
        seen_queries.add(q)
        queries.append(GdeltQuery(
            id=f"picker_extra_{i}",
            query=q,
            timespan="24h",
            max_records=60,
        ))

    return feeds, queries


__all__ = [
    "DomainSpec",
    "PickerSelection",
    "load_source_registry",
    "materialize_sources",
    "pick_sources",
]
