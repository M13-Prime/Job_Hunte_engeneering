"""Pappers v2 collector — track dirigeant changes for a watchlist of SIREN.

We snapshot the current ``representants`` set per SIREN to disk; on each run
we diff against the previous snapshot and emit a CollectedItem when the set
changes (a new appointment / departure is the "executive_change" signal).

Docs: https://www.pappers.fr/api/documentation
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from signal_tracker.collectors.base import BaseCollector, CollectedItem
from signal_tracker.utils.http_cache import FileCache
from signal_tracker.utils.logging import get_logger

logger = get_logger(__name__)


@dataclass(slots=True)
class PappersWatch:
    siren: str
    label: str | None = None  # human-readable hint, ignored by the diff logic


class PappersCollector(BaseCollector):
    """Diff-based Pappers collector."""

    source_id = "pappers"
    BASE_URL = "https://api.pappers.fr/v2/entreprise"

    def __init__(
        self,
        api_key: str,
        watchlist: Sequence[PappersWatch],
        snapshot_dir: str | Path = "data/pappers_snapshots",
        cache_dir: str | Path = "data/cache/pappers",
        cache_ttl_seconds: int = 6 * 3600,
        rate_limit_seconds: float = 1.0,
        timeout_seconds: float = 30.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.api_key = api_key
        self.watchlist = list(watchlist)
        self.snapshot_dir = Path(snapshot_dir)
        self.snapshot_dir.mkdir(parents=True, exist_ok=True)
        self.cache = FileCache(cache_dir, ttl_seconds=cache_ttl_seconds)
        self.rate_limit_seconds = rate_limit_seconds
        self.timeout_seconds = timeout_seconds
        self._client = client
        self._owns_client = client is None

    @classmethod
    def watchlist_from_yaml(cls, raw: dict[str, Any]) -> list[PappersWatch]:
        out: list[PappersWatch] = []
        for entry in raw.get("watchlist", []) or []:
            if isinstance(entry, str):
                out.append(PappersWatch(siren=entry))
            elif isinstance(entry, dict) and "siren" in entry:
                out.append(
                    PappersWatch(siren=str(entry["siren"]), label=entry.get("label"))
                )
        return out

    def _snapshot_path(self, siren: str) -> Path:
        return self.snapshot_dir / f"{siren}.json"

    def _load_snapshot(self, siren: str) -> dict[str, Any] | None:
        path = self._snapshot_path(siren)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text("utf-8"))
        except json.JSONDecodeError:
            return None
        if not isinstance(data, dict):
            return None
        return data

    def _save_snapshot(self, siren: str, snapshot: dict[str, Any]) -> None:
        self._snapshot_path(siren).write_text(
            json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    @staticmethod
    def _representants(payload: dict[str, Any]) -> list[dict[str, str]]:
        raw = payload.get("representants") or []
        people: list[dict[str, str]] = []
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            people.append(
                {
                    "name": str(
                        entry.get("nom_complet")
                        or f"{entry.get('prenom', '')} {entry.get('nom', '')}".strip()
                    ),
                    "role": str(entry.get("qualite") or ""),
                }
            )
        return people

    async def _fetch(
        self, client: httpx.AsyncClient, watch: PappersWatch
    ) -> dict[str, Any] | None:
        params = {"api_token": self.api_key, "siren": watch.siren}
        cache_key = f"siren:{watch.siren}"
        cached = self.cache.get(cache_key)
        if cached is not None:
            try:
                return dict(json.loads(cached))
            except json.JSONDecodeError:
                pass
        try:
            response = await client.get(self.BASE_URL, params=params)
        except httpx.HTTPError as exc:
            logger.warning(
                "pappers.fetch error",
                extra={"siren": watch.siren, "error": str(exc)},
            )
            return None
        if response.status_code != 200:
            logger.warning(
                "pappers.fetch http_error",
                extra={"siren": watch.siren, "status": response.status_code},
            )
            return None
        body = response.content
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            return None
        self.cache.set(cache_key, body)
        if not isinstance(payload, dict):
            return None
        return payload

    @staticmethod
    def _diff(
        previous: list[dict[str, str]], current: list[dict[str, str]]
    ) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
        prev_set = {(p["name"], p["role"]) for p in previous}
        curr_set = {(p["name"], p["role"]) for p in current}
        added = [
            {"name": name, "role": role}
            for (name, role) in (curr_set - prev_set)
        ]
        removed = [
            {"name": name, "role": role}
            for (name, role) in (prev_set - curr_set)
        ]
        return added, removed

    async def collect(self) -> AsyncIterator[CollectedItem]:
        client = self._client or httpx.AsyncClient(timeout=self.timeout_seconds)
        try:
            for index, watch in enumerate(self.watchlist):
                if index > 0:
                    await asyncio.sleep(self.rate_limit_seconds)
                payload = await self._fetch(client, watch)
                if payload is None:
                    continue
                company_name = (
                    payload.get("nom_entreprise")
                    or payload.get("denomination")
                    or watch.label
                    or watch.siren
                )
                current = self._representants(payload)
                snapshot_now = {
                    "company": company_name,
                    "representants": current,
                    "snapshot_at": datetime.now(tz=UTC).isoformat(),
                }
                previous_snapshot = self._load_snapshot(watch.siren)
                self._save_snapshot(watch.siren, snapshot_now)

                if previous_snapshot is None:
                    # First time we see this SIREN — no diff to emit.
                    logger.info(
                        "pappers.snapshot_initialised",
                        extra={"siren": watch.siren, "representants": len(current)},
                    )
                    continue

                added, removed = self._diff(
                    list(previous_snapshot.get("representants") or []),
                    current,
                )
                if not added and not removed:
                    continue

                # Emit one synthetic item per change set; URL is unique per
                # (siren, ISO day) so re-runs the same day dedup naturally.
                day = datetime.now(tz=UTC).strftime("%Y-%m-%d")
                url = f"https://www.pappers.fr/entreprise/{watch.siren}#diff-{day}"
                lines: list[str] = [
                    f"Changement de dirigeants chez {company_name} (SIREN {watch.siren}).",
                ]
                for person in added:
                    lines.append(
                        f"NOMINATION : {person['name']} - {person['role']}"
                    )
                for person in removed:
                    lines.append(
                        f"DEPART : {person['name']} - {person['role']}"
                    )
                yield CollectedItem(
                    source=f"pappers:{watch.siren}",
                    url=url,
                    title=f"Pappers diff dirigeants - {company_name}",
                    content="\n".join(lines),
                    published_at=datetime.now(tz=UTC),
                )
                logger.info(
                    "pappers.diff_emitted",
                    extra={
                        "siren": watch.siren,
                        "added": len(added),
                        "removed": len(removed),
                    },
                )
        finally:
            if self._owns_client:
                await client.aclose()


__all__ = ["PappersCollector", "PappersWatch"]
