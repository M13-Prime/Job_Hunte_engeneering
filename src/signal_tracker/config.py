"""Configuration loader.

Two layers:
- Runtime config / secrets: env vars (.env) parsed via pydantic-settings.
- User-tunable knobs: YAML files under ``config/`` (profile, keywords, sources).
"""

from __future__ import annotations

from functools import cache
from pathlib import Path
from typing import Any

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


__all__ = [
    "CONFIG_DIR",
    "REPO_ROOT",
    "Settings",
    "UserProfile",
    "get_settings",
    "load_jobs_config",
    "load_keywords",
    "load_sources",
    "load_user_profile",
]


# Silence the unused import warning at strict-mypy
_ = EmailStr
