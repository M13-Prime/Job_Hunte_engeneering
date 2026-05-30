"""Single-page-app-ish FastAPI dashboard (Phase 6 redesign).

Flow:
- GET  /                  search landing: keyword editor + launch + history
- POST /search           create a SearchRun, kick collect+classify in bg,
                         redirect to the results table for that run
- GET  /results          signals table (filter by run / score / feedback);
                         signals from the focused run are flagged "new"
- GET  /run/status       JSON poll for the live status pill
- POST /signals/{id}/feedback   set Signal.user_feedback
- GET  /signals/{id}/contacted  convenience link for the digest email
- POST /watchlist | /watchlist/{id}/delete
- POST /keywords  | /keywords/{id}/delete
- POST /searches/{id}/delete    drop a past search from history
- GET  /healthz
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
from sqlalchemy import desc, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from signal_tracker.classifier.feedback import VALID_FEEDBACK
from signal_tracker.config import get_settings, load_user_profile, resolve_db_url
from signal_tracker.storage import Database, init_db
from signal_tracker.storage.models import (
    RawItem,
    SearchRun,
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

    app = FastAPI(title="Signal Tracker", version="0.3.0")

    auth_user = settings.dashboard_auth_user
    auth_pwd = settings.dashboard_auth_password or ""

    # Live status for the header pill. current_run_id links to the SearchRun.
    task_state: dict[str, Any] = {
        "status": "idle",  # idle / running / done / failed
        "step": None,
        "current_run_id": None,
        "metrics": {},
        "error": None,
    }
    bg_tasks: set[asyncio.Task[None]] = set()

    def _keyword_snapshot(session: Session) -> dict[str, list[str]]:
        snap: dict[str, list[str]] = {c: [] for c in KEYWORD_CATEGORIES}
        for kw in session.execute(select(UserKeyword)).scalars():
            snap.setdefault(kw.category, []).append(kw.value)
        return snap

    async def _run_search_task(run_id: int) -> None:
        from signal_tracker.pipeline import run_classification, run_collection

        task_state.update(
            status="running", step="collect", current_run_id=run_id,
            metrics={}, error=None,
        )
        try:
            coll = await run_collection(db=app_db)
            task_state["metrics"]["collect"] = {
                "fetched": coll.fetched, "new": coll.new, "duplicates": coll.duplicates,
            }
            task_state["step"] = "classify"
            clf = await run_classification(
                profile=load_user_profile(), db=app_db, search_run_id=run_id
            )
            metrics = {
                "collect": task_state["metrics"]["collect"],
                "classify": {
                    "processed": clf.processed,
                    "relevant": clf.relevant,
                    "signals_created": clf.signals_created,
                    "signals_deduped": clf.signals_deduped,
                    "errors": clf.errors,
                },
            }
            task_state["metrics"] = metrics
            task_state.update(status="done", step=None)
            with app_db.session() as session:
                run = session.get(SearchRun, run_id)
                if run is not None:
                    run.status = "done"
                    run.metrics = metrics
                    run.finished_at = datetime.now(tz=UTC)
        except Exception as exc:
            task_state.update(status="failed", step=None, error=str(exc)[:500])
            with app_db.session() as session:
                run = session.get(SearchRun, run_id)
                if run is not None:
                    run.status = "failed"
                    run.error = str(exc)[:500]
                    run.finished_at = datetime.now(tz=UTC)

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

    # ---------------------------------------------------------------- landing
    @app.get("/", response_class=HTMLResponse)
    def landing(
        request: Request,
        session: Session = Depends(get_session_dep),
    ) -> HTMLResponse:
        kw_rows = list(
            session.execute(
                select(UserKeyword).order_by(UserKeyword.category, UserKeyword.value)
            ).scalars()
        )
        keywords_by_cat: dict[str, list[UserKeyword]] = {c: [] for c in KEYWORD_CATEGORIES}
        for kw in kw_rows:
            keywords_by_cat.setdefault(kw.category, []).append(kw)

        runs = list(
            session.execute(
                select(SearchRun).order_by(desc(SearchRun.created_at)).limit(25)
            ).scalars()
        )
        watchlist = list(
            session.execute(
                select(WatchlistEntry).order_by(WatchlistEntry.company_name)
            ).scalars()
        )
        total_signals = session.query(Signal).count()

        return templates.TemplateResponse(
            request,
            "search.html.j2",
            {
                "keyword_categories": KEYWORD_CATEGORIES,
                "keywords_by_cat": keywords_by_cat,
                "runs": runs,
                "watchlist": watchlist,
                "total_signals": total_signals,
                "task_status": task_state["status"],
                "current_run_id": task_state["current_run_id"],
            },
        )

    # ---------------------------------------------------------------- results
    @app.get("/results", response_class=HTMLResponse)
    def results(
        request: Request,
        run: Annotated[int | None, Query()] = None,
        feedback: Annotated[str | None, Query()] = None,
        min_score: Annotated[float, Query(ge=0, le=100)] = 0.0,
        limit: Annotated[int, Query(ge=1, le=500)] = 200,
        session: Session = Depends(get_session_dep),
    ) -> HTMLResponse:
        stmt = (
            select(Signal, RawItem)
            .join(RawItem, Signal.raw_item_id == RawItem.id)
            .where(Signal.total_score >= min_score)
            .order_by(desc(Signal.total_score))
            .limit(limit)
        )
        if run is not None:
            stmt = stmt.where(Signal.search_run_id == run)
        if feedback == "pending":
            stmt = stmt.where(Signal.user_feedback.is_(None))
        elif feedback in VALID_FEEDBACK:
            stmt = stmt.where(Signal.user_feedback == feedback)

        rows = list(session.execute(stmt))

        # The "focus" run whose signals get the NEW badge: the explicit ?run=,
        # else the most recent run.
        focus_run_id = run
        if focus_run_id is None:
            latest = session.execute(
                select(SearchRun.id).order_by(desc(SearchRun.created_at)).limit(1)
            ).scalar_one_or_none()
            focus_run_id = latest

        focus_run = session.get(SearchRun, focus_run_id) if focus_run_id else None

        return templates.TemplateResponse(
            request,
            "results.html.j2",
            {
                "rows": rows,
                "filter_feedback": feedback or "",
                "min_score": min_score,
                "valid_feedback": VALID_FEEDBACK,
                "run_id": run,
                "focus_run_id": focus_run_id,
                "focus_run": focus_run,
                "task_status": task_state["status"],
                "current_run_id": task_state["current_run_id"],
            },
        )

    @app.post("/search")
    async def launch_search(
        label: Annotated[str | None, Form()] = None,
    ) -> RedirectResponse:
        if task_state["status"] == "running":
            rid = task_state["current_run_id"]
            target = f"/results?run={rid}" if rid else "/results"
            return RedirectResponse(url=target, status_code=303)

        # Create + commit the SearchRun in its own session so the background
        # task (separate connection) can see it immediately — SQLite hides
        # uncommitted rows from other connections.
        now = datetime.now(tz=UTC)
        with app_db.session() as session:
            snapshot = _keyword_snapshot(session)
            run = SearchRun(
                label=(label or "").strip()
                or f"Recherche du {now.strftime('%d/%m %H:%M')}",
                status="running",
                keywords=snapshot,
            )
            session.add(run)
            session.flush()
            run_id = run.id

        task = asyncio.create_task(_run_search_task(run_id))
        bg_tasks.add(task)
        task.add_done_callback(bg_tasks.discard)
        return RedirectResponse(url=f"/results?run={run_id}", status_code=303)

    @app.get("/run/status")
    def run_status() -> JSONResponse:
        return JSONResponse(task_state)

    @app.post("/searches/{run_id}/delete")
    def delete_search(
        run_id: int,
        session: Session = Depends(get_session_dep),
    ) -> RedirectResponse:
        run = session.get(SearchRun, run_id)
        if run is not None:
            # Detach signals (keep them) then drop the run from history.
            session.execute(
                update(Signal)
                .where(Signal.search_run_id == run_id)
                .values(search_run_id=None)
            )
            session.delete(run)
        return RedirectResponse(url="/", status_code=303)

    # --------------------------------------------------------------- feedback
    @app.post("/signals/{signal_id}/feedback")
    def set_feedback(
        signal_id: int,
        action: Annotated[str, Form()],
        redirect_to: Annotated[str, Form()] = "/results",
        session: Session = Depends(get_session_dep),
    ) -> RedirectResponse:
        if action not in VALID_FEEDBACK:
            raise HTTPException(status_code=400, detail=f"Invalid action: {action}")
        signal = session.get(Signal, signal_id)
        if signal is None:
            raise HTTPException(status_code=404, detail="Signal not found")
        signal.user_feedback = action
        return RedirectResponse(url=_safe_redirect(redirect_to), status_code=303)

    @app.get("/signals/{signal_id}/contacted")
    def mark_contacted(
        signal_id: int,
        session: Session = Depends(get_session_dep),
    ) -> RedirectResponse:
        signal = session.get(Signal, signal_id)
        if signal is None:
            raise HTTPException(status_code=404, detail="Signal not found")
        signal.user_feedback = "contacted"
        return RedirectResponse(url="/results", status_code=303)

    # -------------------------------------------------------------- watchlist
    @app.post("/watchlist")
    def add_watchlist(
        company_name: Annotated[str, Form()],
        notes: Annotated[str | None, Form()] = None,
        session: Session = Depends(get_session_dep),
    ) -> RedirectResponse:
        normalized = normalize_company_name(company_name)
        if not normalized:
            raise HTTPException(status_code=400, detail="Empty company name")
        session.add(
            WatchlistEntry(
                company_name=company_name.strip(),
                normalized_name=normalized,
                notes=(notes or None),
            )
        )
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

    # --------------------------------------------------------------- keywords
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
        session.add(UserKeyword(category=category, value=value))
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

    return app


def _safe_redirect(target: str) -> str:
    """Only allow same-site relative redirects (defense against open redirect)."""
    if target.startswith("/") and not target.startswith("//"):
        return target
    return "/results"


# Module-level app for `uvicorn signal_tracker.dashboard.app:app`.
app = build_app()

# func is imported for potential aggregate queries in future endpoints.
_ = func

__all__ = ["app", "build_app"]
