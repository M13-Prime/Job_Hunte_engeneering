"""France Travail collector — hiring-surge detection by 30-day moving average.

OAuth2 client_credentials against https://entreprise.francetravail.fr, then
queries https://api.francetravail.io/partenaire/offresdemploi/v2/offres/search.

For each ROME code we count today's open postings per company, append the
counts to ``data/france_travail_history.json``, then emit a CollectedItem
for any company whose today_count exceeds ``max(min_count, mean + z*std)``
over the last 30 days.
"""

from __future__ import annotations

import asyncio
import json
import math
import statistics
from collections import defaultdict
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from signal_tracker.collectors.base import BaseCollector, CollectedItem
from signal_tracker.utils.http_cache import FileCache
from signal_tracker.utils.logging import get_logger

logger = get_logger(__name__)

TOKEN_URL = (
    "https://entreprise.francetravail.fr/connexion/oauth2/access_token?realm=/partenaire"
)
OFFERS_URL = (
    "https://api.francetravail.io/partenaire/offresdemploi/v2/offres/search"
)
DEFAULT_SCOPE = "api_offresdemploiv2 o2dsoffre"


@dataclass(slots=True)
class FranceTravailConfig:
    rome_codes: list[str]
    min_count: int = 3
    z_threshold: float = 1.5
    history_window_days: int = 30


class FranceTravailCollector(BaseCollector):
    """OAuth2 + offres-search + moving-average surge detector."""

    source_id = "france_travail"

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        config: FranceTravailConfig,
        history_path: str | Path = "data/france_travail_history.json",
        cache_dir: str | Path = "data/cache/france_travail",
        cache_ttl_seconds: int = 6 * 3600,
        rate_limit_seconds: float = 1.0,
        timeout_seconds: float = 30.0,
        client: httpx.AsyncClient | None = None,
        scope: str = DEFAULT_SCOPE,
    ) -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        self.config = config
        self.history_path = Path(history_path)
        self.history_path.parent.mkdir(parents=True, exist_ok=True)
        self.cache = FileCache(cache_dir, ttl_seconds=cache_ttl_seconds)
        self.rate_limit_seconds = rate_limit_seconds
        self.timeout_seconds = timeout_seconds
        self._client = client
        self._owns_client = client is None
        self.scope = scope
        self._token: str | None = None

    @classmethod
    def from_yaml(cls, raw: dict[str, Any]) -> FranceTravailConfig:
        return FranceTravailConfig(
            rome_codes=list(raw.get("rome_codes", []) or []),
            min_count=int(raw.get("surge_min_count", 3)),
            z_threshold=float(raw.get("surge_z_threshold", 1.5)),
            history_window_days=int(raw.get("history_window_days", 30)),
        )

    async def _get_token(self, client: httpx.AsyncClient) -> str | None:
        if self._token:
            return self._token
        try:
            response = await client.post(
                TOKEN_URL,
                data={
                    "grant_type": "client_credentials",
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    "scope": self.scope,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        except httpx.HTTPError as exc:
            logger.warning("france_travail.token error", extra={"error": str(exc)})
            return None
        if response.status_code != 200:
            logger.warning(
                "france_travail.token http_error",
                extra={"status": response.status_code},
            )
            return None
        try:
            payload = response.json()
        except ValueError:
            return None
        token = payload.get("access_token")
        if not isinstance(token, str):
            return None
        self._token = token
        return token

    async def _fetch_offers(
        self, client: httpx.AsyncClient, token: str, rome_code: str
    ) -> list[dict[str, Any]]:
        cache_key = f"rome:{rome_code}"
        cached = self.cache.get(cache_key)
        if cached is not None:
            try:
                return list(json.loads(cached).get("resultats") or [])
            except json.JSONDecodeError:
                pass
        try:
            response = await client.get(
                OFFERS_URL,
                params={"codeROME": rome_code, "range": "0-149"},
                headers={"Authorization": f"Bearer {token}"},
            )
        except httpx.HTTPError as exc:
            logger.warning(
                "france_travail.offers error",
                extra={"rome": rome_code, "error": str(exc)},
            )
            return []
        if response.status_code not in (200, 206):
            logger.warning(
                "france_travail.offers http_error",
                extra={"rome": rome_code, "status": response.status_code},
            )
            return []
        body = response.content
        self.cache.set(cache_key, body)
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            return []
        return list(payload.get("resultats") or [])

    @staticmethod
    def _company_name(offer: dict[str, Any]) -> str | None:
        entreprise = offer.get("entreprise") or {}
        if isinstance(entreprise, dict):
            name = entreprise.get("nom")
            if isinstance(name, str) and name.strip():
                return name.strip()
        return None

    def _load_history(self) -> dict[str, dict[str, int]]:
        if not self.history_path.exists():
            return {}
        try:
            data = json.loads(self.history_path.read_text("utf-8"))
        except json.JSONDecodeError:
            return {}
        if not isinstance(data, dict):
            return {}
        return data

    def _save_history(self, history: dict[str, dict[str, int]]) -> None:
        self.history_path.write_text(
            json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def _prune_history(
        self, history: dict[str, dict[str, int]]
    ) -> dict[str, dict[str, int]]:
        cutoff = (datetime.now(tz=UTC) - timedelta(days=self.config.history_window_days * 2)).date()
        kept: dict[str, dict[str, int]] = {}
        for day, counts in history.items():
            d = _safe_iso_date(day)
            if d is not None and d >= cutoff:
                kept[day] = counts
        return kept

    @staticmethod
    def _baseline(
        history: dict[str, dict[str, int]], company: str, today: str, window_days: int
    ) -> tuple[float, float]:
        end = _safe_iso_date(today)
        if end is None:
            return 0.0, 0.0
        start = end - timedelta(days=window_days)
        samples: list[int] = []
        for day, counts in history.items():
            d = _safe_iso_date(day)
            if d is None or d == end or d < start or d > end:
                continue
            samples.append(int(counts.get(company, 0)))
        if not samples:
            return 0.0, 0.0
        mean = statistics.fmean(samples)
        std = statistics.pstdev(samples) if len(samples) > 1 else 0.0
        return mean, std

    async def collect(self) -> AsyncIterator[CollectedItem]:
        client = self._client or httpx.AsyncClient(timeout=self.timeout_seconds)
        try:
            token = await self._get_token(client)
            if not token:
                return

            today = datetime.now(tz=UTC).strftime("%Y-%m-%d")
            today_counts: dict[str, int] = defaultdict(int)
            for index, rome in enumerate(self.config.rome_codes):
                if index > 0:
                    await asyncio.sleep(self.rate_limit_seconds)
                offers = await self._fetch_offers(client, token, rome)
                for offer in offers:
                    company = self._company_name(offer)
                    if not company:
                        continue
                    today_counts[company] += 1

            history = self._prune_history(self._load_history())
            history[today] = dict(today_counts)
            self._save_history(history)

            for company, count in today_counts.items():
                mean, std = self._baseline(
                    history, company, today, self.config.history_window_days
                )
                threshold = max(
                    float(self.config.min_count),
                    mean + self.config.z_threshold * std,
                )
                if count < threshold:
                    continue
                spike_ratio = count / mean if mean > 0 else math.inf
                day_anchor = datetime.now(tz=UTC).strftime("%Y-%m-%d")
                yield CollectedItem(
                    source=f"france_travail:{rome_summary(self.config.rome_codes)}",
                    url=(
                        "https://candidat.francetravail.fr/offres/recherche?"
                        f"motsCles={company.replace(' ', '+')}&date={day_anchor}"
                    ),
                    title=f"Pic de recrutement chez {company}",
                    content=(
                        f"{company} a publie {count} offres aujourd'hui sur les ROME "
                        f"{', '.join(self.config.rome_codes)} "
                        f"(baseline 30j: mean={mean:.1f}, std={std:.1f}, "
                        f"ratio x{spike_ratio:.1f})."
                    ),
                    published_at=datetime.now(tz=UTC),
                )
                logger.info(
                    "france_travail.surge_emitted",
                    extra={
                        "company": company,
                        "today_count": count,
                        "mean": round(mean, 2),
                        "std": round(std, 2),
                    },
                )
        finally:
            if self._owns_client:
                await client.aclose()


def rome_summary(rome_codes: Sequence[str]) -> str:
    return "+".join(rome_codes[:3])


def _safe_iso_date(value: str) -> Any:
    """Parse YYYY-MM-DD; return ``None`` on failure (typed as Any to keep mypy happy)."""
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


__all__ = [
    "DEFAULT_SCOPE",
    "FranceTravailCollector",
    "FranceTravailConfig",
    "rome_summary",
]
