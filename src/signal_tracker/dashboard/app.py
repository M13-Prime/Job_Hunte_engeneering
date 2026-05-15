"""Single-page FastAPI dashboard.

Endpoints:
- GET  /                              — list signals (filterable)
- POST /signals/{id}/feedback         — set Signal.user_feedback
- GET  /signals/{id}/contacted        — convenience GET for digest email link
- GET  /watchlist                     — list watchlist entries (HTML fragment)
- POST /watchlist                     — add a company
- POST /watchlist/{id}/delete         — remove a company
- GET  /healthz                       — liveness probe
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import desc, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from signal_tracker.classifier.feedback import VALID_FEEDBACK
from signal_tracker.config import get_settings
from signal_tracker.storage import Database, init_db
from signal_tracker.storage.models import RawItem, Signal, WatchlistEntry
from signal_tracker.utils.normalize import normalize_company_name

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"


def build_app(db: Database | None = None) -> FastAPI:
    """Build the FastAPI app, optionally injecting a custom Database (tests)."""
    app_db = db or init_db(get_settings().db_path)
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

    def get_session_dep() -> Iterable[Session]:
        with app_db.session() as session:
            yield session

    app = FastAPI(title="Signal Tracker Dashboard", version="0.1.0")

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/", response_class=HTMLResponse)
    def index(
        request: Request,
        feedback: Annotated[str | None, Query()] = None,
        min_score: Annotated[float, Query(ge=0, le=100)] = 0.0,
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
        session: Session = Depends(get_session_dep),
    ) -> HTMLResponse:
        stmt = (
            select(Signal, RawItem)
            .join(RawItem, Signal.raw_item_id == RawItem.id)
            .where(Signal.total_score >= min_score)
            .order_by(desc(Signal.total_score))
            .limit(limit)
        )
        if feedback == "pending":
            stmt = stmt.where(Signal.user_feedback.is_(None))
        elif feedback in VALID_FEEDBACK:
            stmt = stmt.where(Signal.user_feedback == feedback)

        rows = list(session.execute(stmt))
        watchlist = list(
            session.execute(select(WatchlistEntry).order_by(WatchlistEntry.company_name)).scalars()
        )

        return templates.TemplateResponse(
            request,
            "index.html.j2",
            {
                "rows": rows,
                "watchlist": watchlist,
                "filter_feedback": feedback or "",
                "min_score": min_score,
                "valid_feedback": VALID_FEEDBACK,
            },
        )

    @app.post("/signals/{signal_id}/feedback")
    def set_feedback(
        signal_id: int,
        action: Annotated[str, Form()],
        session: Session = Depends(get_session_dep),
    ) -> RedirectResponse:
        if action not in VALID_FEEDBACK:
            raise HTTPException(status_code=400, detail=f"Invalid action: {action}")
        signal = session.get(Signal, signal_id)
        if signal is None:
            raise HTTPException(status_code=404, detail="Signal not found")
        signal.user_feedback = action
        return RedirectResponse(url="/", status_code=303)

    @app.get("/signals/{signal_id}/contacted")
    def mark_contacted(
        signal_id: int,
        session: Session = Depends(get_session_dep),
    ) -> RedirectResponse:
        signal = session.get(Signal, signal_id)
        if signal is None:
            raise HTTPException(status_code=404, detail="Signal not found")
        signal.user_feedback = "contacted"
        return RedirectResponse(url="/", status_code=303)

    @app.post("/watchlist")
    def add_watchlist(
        company_name: Annotated[str, Form()],
        notes: Annotated[str | None, Form()] = None,
        session: Session = Depends(get_session_dep),
    ) -> RedirectResponse:
        normalized = normalize_company_name(company_name)
        if not normalized:
            raise HTTPException(status_code=400, detail="Empty company name")
        entry = WatchlistEntry(
            company_name=company_name.strip(),
            normalized_name=normalized,
            notes=(notes or None),
        )
        session.add(entry)
        try:
            session.flush()
        except IntegrityError:
            session.rollback()
            # Already exists; fall through silently.
        return RedirectResponse(url="/", status_code=303)

    @app.post("/watchlist/{entry_id}/delete")
    def delete_watchlist(
        entry_id: int,
        session: Session = Depends(get_session_dep),
    ) -> RedirectResponse:
        entry = session.get(WatchlistEntry, entry_id)
        if entry is None:
            raise HTTPException(status_code=404, detail="Watchlist entry not found")
        session.delete(entry)
        return RedirectResponse(url="/", status_code=303)

    return app


# Module-level app instance for `uvicorn signal_tracker.dashboard.app:app`.
app = build_app()


__all__ = ["app", "build_app"]
