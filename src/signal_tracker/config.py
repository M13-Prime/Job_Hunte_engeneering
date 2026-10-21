"""Configuration loader.

Two layers:
- Runtime config / secrets: env vars (.env) parsed via pydantic-settings.
- User-tunable knobs: YAML files under ``config/`` (profile, keywords, sources).
"""

from __future__ import annotations

from functools import cache
from pathlib import Path
from typing import Any, Literal

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, EmailStr, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = REPO_ROOT / "config"

# Eagerly load .env into os.environ so libraries that read env vars directly
# (LiteLLM, OpenAI SDK, ...) see the keys without us having to thread them
# through manually. override=False so Codespaces Secrets / shell exports win.
load_dotenv(REPO_ROOT / ".env", override=False)


class Settings(BaseSettings):
    """Runtime settings sourced from env / .env file."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # LLM
    llm_model: str = "anthropic/claude-sonnet-4-5"
    llm_fallback_model: str | None = None
    # Seconds to sleep between consecutive classify() calls in the pipeline.
    # Set to ~7 on Anthropic Tier 1 (30k TPM) to avoid rate-limit retries.
    llm_rate_limit_seconds: float = 0.0
    # Phase 11.2 — parallelism cap on run_classification. The pipeline
    # processes up to this many articles concurrently (asyncio.gather +
    # Semaphore). Keep it under your Anthropic tier's RPM cap; 8 is safe
    # for paid tier, raise to 16-24 if you have headroom. Setting it to
    # 1 reverts to the old strictly-sequential loop.
    llm_concurrency: int = 8
    # Cheap model used for the two-stage classifier prefilter (perf #2) and
    # the CV → CVProfile distillation (perf #7). Falls back to llm_model
    # when unset. Recommended: anthropic/claude-haiku-4-5.
    llm_cheap_model: str | None = None
    # Opt-in: if true, every article goes through a Haiku yes/no/maybe
    # prefilter before the full Sonnet classification. Cuts cost and time
    # ~60-70% when the news corpus is mostly noise. Requires llm_cheap_model.
    llm_prefilter_enabled: bool = False
    # Opt-in: send Anthropic prompt caching headers on the classifier
    # system prompt. No-op for non-Anthropic providers.
    llm_prompt_cache_enabled: bool = True
    # Opt-in: at search-launch time, an LLM picks which curated domains
    # from config/source_registry.yaml to activate, instead of fetching
    # every configured source. Requires llm_cheap_model.
    llm_source_picker_enabled: bool = False
    # Phase 11 — agentic loops (LLM + tools). Each agent is opt-in, both
    # off by default so the existing linear pipeline keeps working.
    llm_research_agent_enabled: bool = False
    llm_prep_agent_enabled: bool = False
    # Phase 11 — Jobs Agent: post-classify pass that scrapes ATS boards
    # for signal-bearing companies and semantically rescores the offers.
    llm_jobs_agent_enabled: bool = False
    # Per-run safety caps. Budget is best-effort (uses
    # litellm.completion_cost() to estimate per turn).
    llm_agent_budget_usd: float = 0.5
    llm_agent_max_iterations: int = 30
    # Phase 13 — one model per task. Each is optional; unset falls back to
    # LLM_CHEAP_MODEL (prefilter / extract) or LLM_MODEL (the rest), so an
    # existing .env keeps working unchanged. See model_for() and
    # docs/LLM_COSTS.md for the recommended cheap-but-good routing.
    #   classify : bulk news classification (hundreds of calls per search)
    #   prefilter: yes/no/maybe gate before classify (tiny output)
    #   extract  : structured extraction — CV profile, job match score,
    #              source picker, prep research steps
    #   write    : long-form writing — preparation report, tailored CV +
    #              cover letter (quality-sensitive, few calls)
    #   agent    : tool-using agent loops (research / jobs / prep agents)
    llm_model_classify: str | None = None
    llm_model_prefilter: str | None = None
    llm_model_extract: str | None = None
    llm_model_write: str | None = None
    llm_model_agent: str | None = None
    # Free, deterministic gate before any LLM call: an article that matches
    # none of the user's keywords / profile terms / config/keywords.yaml is
    # skipped without spending a token. Off = classify everything.
    keyword_prefilter_enabled: bool = True
    # Phase 12 — job alerts. The daily run refreshes the ATS boards of the
    # user's watchlist + signal companies, then AI-scores at most
    # `jobs_auto_score_limit` new offers (cheap model, ~1 call each) whose
    # keyword score is >= `jobs_auto_score_min_heuristic`. Offers scoring
    # >= `job_alert_threshold` land in the morning digest. Set the limit
    # to 0 to keep scoring 100% manual (button on /jobs).
    jobs_daily_refresh_enabled: bool = True
    jobs_auto_score_limit: int = 10
    jobs_auto_score_min_heuristic: float = 10.0
    job_alert_threshold: float = 60.0
    # Days after "postulée" before the tracker suggests a follow-up.
    application_followup_days: int = 7
    # Phase 13 — weekly newsletter to every approved, opted-in user.
    # Sent by the scheduler on this weekday (0 = Monday ... 6 = Sunday),
    # -1 disables the automatic send (the admin page can still send it).
    newsletter_weekday: int = 6
    # Own trigger, independent from the daily digest (Europe/Paris time).
    newsletter_hour: int = 9
    newsletter_minute: int = 0
    newsletter_period_days: int = 7
    # Top N of the week in each section.
    newsletter_max_signals: int = 10
    newsletter_max_jobs: int = 10
    # Minimum AI match score for an offer to be listed, and whether offers
    # with ghost-job hints are left out.
    newsletter_job_min_score: float = 50.0
    newsletter_hide_ghost: bool = True
    anthropic_api_key: str | None = None
    openai_api_key: str | None = None
    gemini_api_key: str | None = None
    mistral_api_key: str | None = None
    ollama_base_url: str | None = None

    # Source API keys
    newsapi_key: str | None = None
    pappers_api_key: str | None = None
    france_travail_client_id: str | None = None
    france_travail_client_secret: str | None = None

    # Notification
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_user: str | None = None
    smtp_password: str | None = None
    digest_to_email: str | None = None
    digest_from_email: str | None = None
    resend_api_key: str | None = None

    # Runtime
    digest_send_hour: int = 7
    digest_timezone: str = "Europe/Paris"
    dashboard_base_url: str | None = None
    log_level: str = "INFO"
    db_path: str = "data/signals.db"
    # When set, takes precedence over ``db_path``. Supports
    # ``sqlite:///...`` or ``postgresql+psycopg://user:pwd@host:5432/db``.
    database_url: str | None = None

    # Dashboard Basic Auth (legacy single-user gate; superseded by the
    # Phase 8 email+password accounts but kept as an outer firewall when
    # both env vars are set).
    dashboard_auth_user: str | None = None
    dashboard_auth_password: str | None = None

    # Phase 8 — session signing key for the email+password auth cookies.
    # When unset we generate a random 32-byte key at boot. Set this in
    # prod (.env) so user sessions survive container restarts.
    session_secret_key: str | None = None

    # Second, cheap AI pass that removes anything the source CV doesn't
    # support from a tailored CV / cover letter (~0.01 $ per document).
    # False = only when the user clicks "Relecture" on the documents page.
    tailor_review_enabled: bool = False
    # Optional JobSpy collector (Indeed, LinkedIn, Glassdoor, Google Jobs).
    # Needs the image built with INSTALL_JOBSPY=1. Unofficial endpoints:
    # they break without notice and LinkedIn rate-limits quickly.
    jobspy_enabled: bool = False
    jobspy_sites: str = "indeed,google"
    jobspy_results_per_search: int = 30
    jobspy_hours_old: int = 168


class UserProfile(BaseModel):
    """The profile used to score relevance of signals to the user."""

    name: str | None = None
    domains: list[str] = Field(default_factory=list)
    target_roles: list[str] = Field(default_factory=list)
    geographies: list[str] = Field(default_factory=list)
    target_company_types: list[str] = Field(default_factory=list)
    notes: str | None = None


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        raise ValueError(f"Expected mapping at top level of {path}, got {type(data)}")
    return data


@cache
def get_settings() -> Settings:
    return Settings()


@cache
def load_user_profile(path: str | Path | None = None) -> UserProfile:
    profile_path = Path(path) if path else CONFIG_DIR / "user_profile.yaml"
    return UserProfile.model_validate(_read_yaml(profile_path))


@cache
def load_keywords(path: str | Path | None = None) -> dict[str, list[str]]:
    keywords_path = Path(path) if path else CONFIG_DIR / "keywords.yaml"
    raw = _read_yaml(keywords_path)
    return {k: list(v or []) for k, v in raw.items()}


@cache
def load_sources(path: str | Path | None = None) -> dict[str, Any]:
    sources_path = Path(path) if path else CONFIG_DIR / "sources.yaml"
    return _read_yaml(sources_path)


@cache
def load_jobs_config(path: str | Path | None = None) -> dict[str, Any]:
    jobs_path = Path(path) if path else CONFIG_DIR / "jobs.yaml"
    return _read_yaml(jobs_path)


LLMTask = Literal["classify", "prefilter", "extract", "write", "agent"]
LLM_TASKS: tuple[LLMTask, ...] = ("classify", "prefilter", "extract", "write", "agent")


def model_for(task: LLMTask, settings: Settings | None = None) -> str:
    """The model to use for `task`.

    Priority: the task's own LLM_MODEL_<TASK> > LLM_CHEAP_MODEL for the
    cheap tasks (prefilter, extract) > LLM_MODEL.
    """
    settings = settings or get_settings()
    override = getattr(settings, f"llm_model_{task}", None)
    if override:
        return str(override)
    if task in ("prefilter", "extract") and settings.llm_cheap_model:
        return settings.llm_cheap_model
    return settings.llm_model


def resolve_db_url(settings: Settings | None = None) -> str:
    """Return the URL/path to hand to ``init_db``.

    Priority: explicit ``DATABASE_URL`` env var > the local SQLite ``db_path``.
    Centralised so every script and the dashboard agree.
    """
    settings = settings or get_settings()
    return settings.database_url or settings.db_path


__all__ = [
    "CONFIG_DIR",
    "LLM_TASKS",
    "REPO_ROOT",
    "LLMTask",
    "Settings",
    "UserProfile",
    "get_settings",
    "load_jobs_config",
    "load_keywords",
    "load_sources",
    "load_user_profile",
    "model_for",
    "resolve_db_url",
]


# Silence the unused import warning at strict-mypy
_ = EmailStr
