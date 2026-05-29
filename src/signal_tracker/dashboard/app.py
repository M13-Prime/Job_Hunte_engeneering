"""Single-page FastAPI dashboard.

Endpoints:
- GET  /healthz                       liveness probe
- GET  /                              main dashboard (signals + filters + keywords + watchlist)
- POST /signals/{id}/feedback         set Signal.user_feedback
- GET  /signals/{id}/contacted        convenience link for the digest email
- POST /watchlist                     add a company (silent on duplicate)
- POST /watchlist/{id}/delete         remove a company
- POST /keywords                      add a user-curated keyword
- POST /keywords/{id}/delete          remove a user keyword
- POST /run/pipeline                  launch a full collect+classify run in background
- GET  /run/status                    current background task state (polled by the UI)
"""

from __future__ import annotations

import asyncio
import base64
import secrets
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import desc, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from signal_tracker.classifier.feedback import VALID_FEEDBACK
from signal_tracker.config import get_settings, load_user_profile, resolve_db_url
from signal_tracker.storage import Database, init_db
from signal_tracker.storage.models import (
    RawItem,
    Signal,
    UserKeyword,
    WatchlistEntry,
)
from signal_tracker.utils.normalize import normalize_company_name

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
UNAUTH_PATHS = {"/healthz"}

KEYWORD_CATEGORIES = ("field", "job_title", "other")


def _check_basic_auth(header_value: str | None, expected_user: str, expected_pwd: str) -> bool:
    if not header_value or not header_value.startswith("Basic "):
        return False
    try:
        decoded = base64.b64decode(header_value.split(" ", 1)[1]).decode("utf-8")
        user, _, pwd = decoded.partition(":")
    except (ValueError, UnicodeDecodeError):
        return False
    return secrets.compare_digest(user, expected_user) and secrets.compare_digest(
        pwd, expected_pwd
    )


def build_app(db: Database | None = None) -> FastAPI:
    """Build the FastAPI app, optionally injecting a custom Database (tests)."""
    settings = get_settings()
    app_db = db or init_db(resolve_db_url(settings))
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

    def get_session_dep() -> Iterable[Session]:
        with app_db.session() as session:
            yield session

    app = FastAPI(title="Signal Tracker Dashboard", version="0.2.0")

    auth_user = settings.dashboard_auth_user
    auth_pwd = settings.dashboard_auth_password or ""

    # In-memory state for the background pipeline task. Keyed on a single
    # slot (one task at a time per dashboard process).
    task_state: dict[str, Any] = {
        "status": "idle",        # idle / running / done / failed
        "step": None,            # collect / classify / digest / None
        "started_at": None,
        "finished_at": None,
        "metrics": {},
        "error": None,
    }
    # Reference holder so the running asyncio task isn't garbage-collected
    # mid-flight (RUF006).
    bg_tasks: set[asyncio.Task[None]] = set()

    async def _run_pipeline_task() -> None:
        from signal_tracker.pipeline import run_classification, run_collection

        task_state.update(
            status="running",
            step="collect",
            started_at=datetime.now(tz=UTC).isoformat(),
            finished_at=None,
            metrics={},
            error=None,
        )
        try:
            coll = await run_collection(db=app_db)
            task_state["metrics"]["collect"] = {
                "fetched": coll.fetched,
                "new": coll.new,
                "duplicates": coll.duplicates,
            }
            task_state["step"] = "classify"
            profile = load_user_profile()
            clf = await run_classification(profile=profile, db=app_db)
            task_state["metrics"]["classify"] = {
                "processed": clf.processed,
                "relevant": clf.relevant,
                "signals_created": clf.signals_created,
                "signals_deduped": clf.signals_deduped,
                "errors": clf.errors,
            }
            task_state.update(
                status="done",
                step=None,
                finished_at=datetime.now(tz=UTC).isoformat(),
            )
        except Exception as exc:
            task_state.update(
                status="failed",
                step=None,
                finished_at=datetime.now(tz=UTC).isoformat(),
                error=str(exc)[:500],
            )

    @app.middleware("http")
    async def basic_auth_middleware(request: Request, call_next):  # type: ignore[no-untyped-def]
        if not auth_user:
            return await call_next(request)
        if request.url.path in UNAUTH_PATHS:
            return await call_next(request)
        if _check_basic_auth(request.headers.get("authorization"), auth_user, auth_pwd):
            return await call_next(request)
        return Response(
            status_code=401,
            content="Authentication required.",
            headers={"WWW-Authenticate": 'Basic realm="Signal Tracker"'},
        )

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

        kw_stmt = select(UserKeyword).order_by(
            UserKeyword.category, UserKeyword.value
        )
        kw_rows = list(session.execute(kw_stmt).scalars())
        keywords_by_cat: dict[str, list[UserKeyword]] = {c: [] for c in KEYWORD_CATEGORIES}
        for kw in kw_rows:
            keywords_by_cat.setdefault(kw.category, []).append(kw)

        # Top-line counts for the header.
        total_signals = session.query(Signal).count()
        pending_signals = session.query(Signal).filter(
            Signal.user_feedback.is_(None)
        ).count()
        total_companies = session.query(Signal.company_normalized).distinct().count()

        return templates.TemplateResponse(
            request,
            "index.html.j2",
            {
                "rows": rows,
                "watchlist": watchlist,
                "filter_feedback": feedback or "",
                "min_score": min_score,
                "valid_feedback": VALID_FEEDBACK,
                "keyword_categories": KEYWORD_CATEGORIES,
                "keywords_by_cat": keywords_by_cat,
                "totals": {
                    "signals": total_signals,
                    "pending": pending_signals,
                    "companies": total_companies,
                },
                "task_status": task_state["status"],
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

    @app.post("/keywords")
    def add_keyword(
        category: Annotated[str, Form()],
        value: Annotated[str, Form()],
        session: Session = Depends(get_session_dep),
    ) -> RedirectResponse:
        if category not in KEYWORD_CATEGORIES:
            raise HTTPException(status_code=400, detail=f"Invalid category: {category}")
        value = value.strip()
        if not value:
            raise HTTPException(status_code=400, detail="Empty value")
        entry = UserKeyword(category=category, value=value)
        session.add(entry)
        try:
            session.flush()
        except IntegrityError:
            session.rollback()
        return RedirectResponse(url="/", status_code=303)

    @app.post("/keywords/{keyword_id}/delete")
    def delete_keyword(
        keyword_id: int,
        session: Session = Depends(get_session_dep),
    ) -> RedirectResponse:
        kw = session.get(UserKeyword, keyword_id)
        if kw is None:
            raise HTTPException(status_code=404, detail="Keyword not found")
        session.delete(kw)
        return RedirectResponse(url="/", status_code=303)

    @app.post("/run/pipeline")
    async def launch_pipeline() -> RedirectResponse:
        if task_state["status"] == "running":
            return RedirectResponse(url="/", status_code=303)
        task = asyncio.create_task(_run_pipeline_task())
        bg_tasks.add(task)
        task.add_done_callback(bg_tasks.discard)
        return RedirectResponse(url="/", status_code=303)

    @app.get("/run/status")
    def run_status() -> JSONResponse:
        return JSONResponse(task_state)

    return app


# Module-level app instance for `uvicorn signal_tracker.dashboard.app:app`.
app = build_app()


__all__ = ["app", "build_app"]
