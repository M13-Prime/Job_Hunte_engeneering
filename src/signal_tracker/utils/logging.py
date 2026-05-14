"""Lightweight structured logging setup.

- Console handler via ``rich`` for interactive use.
- All log records carry ``extra={...}`` payloads that are flattened into the
  message; this keeps grep-friendly key=value pairs in the output.
"""

from __future__ import annotations

import logging
from typing import Any

from rich.logging import RichHandler

_CONFIGURED = False


class _KVFormatter(logging.Formatter):
    """Append `extra` kwargs to the message as `key=value` pairs."""

    _STANDARD_ATTRS = frozenset(
        logging.LogRecord(
            name="", level=0, pathname="", lineno=0, msg="", args=None, exc_info=None
        ).__dict__.keys()
        | {"message", "asctime"}
    )

    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        extras: dict[str, Any] = {
            k: v for k, v in record.__dict__.items() if k not in self._STANDARD_ATTRS
        }
        if extras:
            kv = " ".join(f"{k}={v!r}" for k, v in extras.items())
            return f"{base}  {kv}"
        return base


def setup_logging(level: str = "INFO") -> None:
    """Configure root logging once. Idempotent."""
    global _CONFIGURED
    if _CONFIGURED:
        return
    handler = RichHandler(
        rich_tracebacks=True,
        markup=False,
        show_path=False,
        show_time=True,
        log_time_format="%H:%M:%S",
    )
    handler.setFormatter(_KVFormatter("%(message)s"))
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level.upper())
    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
