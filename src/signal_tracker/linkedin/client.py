"""Thin, rate-limited wrapper around ``mcporter call 'linkedin.<fn>(...)'``.

agent-reach (https://github.com/EdisonChenAI/agent-reach) exposes LinkedIn
through the ``mcporter`` CLI and a logged-in session. This client:

- never runs unless explicitly enabled in settings;
- spaces calls (``linkedin_min_interval_seconds``) and caps them per day;
- stops for the rest of the run after a block / login error;
- passes arguments as a list (no shell), with values quoted for mcporter.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import time
from datetime import date
from typing import Any

from signal_tracker.config import Settings, get_settings
from signal_tracker.utils.logging import get_logger

logger = get_logger(__name__)

_BLOCK_MARKERS = ("login", "authwall", "captcha", "challenge", "429", "rate limit", "restricted")


class LinkedInError(RuntimeError):
    """Any LinkedIn channel failure (disabled, limit reached, blocked, bad output)."""


def _quote(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def build_call(function: str, **kwargs: Any) -> str:
    """Build the mcporter call expression, e.g. linkedin.search_people(keyword: "x", limit: 5)."""
    parts: list[str] = []
    for key, value in kwargs.items():
        if value is None:
            continue
        rendered = _quote(value) if isinstance(value, str) else json.dumps(value)
        parts.append(f"{key}: {rendered}")
    return f"linkedin.{function}({', '.join(parts)})"


class LinkedInClient:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._lock = asyncio.Lock()
        self._last_call = 0.0
        self._day = date.today()
        self._calls_today = 0
        self._blocked = False

    @property
    def enabled(self) -> bool:
        return bool(self.settings.linkedin_enabled)

    def available(self) -> bool:
        return self.enabled and shutil.which(self.settings.linkedin_mcporter_bin) is not None

    async def _call(self, function: str, **kwargs: Any) -> Any:
        if not self.enabled:
            raise LinkedInError("LinkedIn channel is disabled (LINKEDIN_ENABLED=false)")
        if self._blocked:
            raise LinkedInError("LinkedIn channel stopped after a block/login error")
        binary = shutil.which(self.settings.linkedin_mcporter_bin)
        if binary is None:
            raise LinkedInError(f"{self.settings.linkedin_mcporter_bin!r} not found in PATH")

        async with self._lock:
            today = date.today()
            if today != self._day:
                self._day, self._calls_today = today, 0
            if self._calls_today >= self.settings.linkedin_daily_limit:
                raise LinkedInError("daily LinkedIn call limit reached")
            elapsed = time.monotonic() - self._last_call
            wait = self.settings.linkedin_min_interval_seconds - elapsed
            if wait > 0:
                await asyncio.sleep(wait)
            self._calls_today += 1
            self._last_call = time.monotonic()

            expr = build_call(function, **kwargs)
            logger.info("linkedin call", extra={"function": function})
            proc = await asyncio.create_subprocess_exec(
                binary, "call", expr,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            try:
                out, err = await asyncio.wait_for(
                    proc.communicate(), timeout=self.settings.linkedin_timeout_seconds
                )
            except TimeoutError as exc:
                proc.kill()
                raise LinkedInError("mcporter call timed out") from exc

        text = out.decode("utf-8", "replace").strip()
        errtext = err.decode("utf-8", "replace").strip()
        if proc.returncode != 0:
            self._check_block(errtext or text)
            raise LinkedInError(f"mcporter failed ({proc.returncode}): {(errtext or text)[:300]}")
        self._check_block(text[:500] if len(text) < 500 else "")
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return {"raw": text[:4000]}

    def _check_block(self, message: str) -> None:
        low = message.lower()
        if any(marker in low for marker in _BLOCK_MARKERS):
            self._blocked = True
            raise LinkedInError(f"LinkedIn blocked or requires login: {message[:200]}")

    async def search_people(self, keyword: str, limit: int = 5) -> Any:
        return await self._call("search_people", keyword=keyword, limit=max(1, min(limit, 10)))

    async def get_person_profile(self, linkedin_url: str) -> Any:
        if "linkedin.com/in/" not in linkedin_url:
            raise LinkedInError("not a LinkedIn profile URL")
        return await self._call("get_person_profile", linkedin_url=linkedin_url)
