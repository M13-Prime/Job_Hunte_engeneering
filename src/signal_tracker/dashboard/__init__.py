"""Phase 5 — single-page FastAPI dashboard.

Run with::

    make dashboard            # uvicorn signal_tracker.dashboard.app:app

Or import ``app`` programmatically (used by the test suite).
"""

from signal_tracker.dashboard.app import app, build_app

__all__ = ["app", "build_app"]
