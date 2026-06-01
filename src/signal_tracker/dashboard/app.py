"""FastAPI dashboard (Phase 8 multi-tenant).

Email + password accounts, signed-cookie sessions, per-user scoping on:
keywords, watchlist, CV, search runs, preparations, signal feedback.
Signals + raw items stay shared (one news corpus).

Routes:
- GET  /signup | POST /signup
- GET  /login  | POST /login
- POST /logout
- GET  /                landing (auth)
- GET  /results         signals table (auth, per-user feedback)
- POST /search
- GET  /run/status      JSON, per-user
- POST /searches/{id}/delete
- POST /signals/{id}/feedback | GET /signals/{id}/contacted
- POST /watchlist | /watchlist/{id}/delete
- POST /keywords  | /keywords/{id}/delete
- GET  /signals/{id}/prepare | POST /signals/{id}/prepare
- GET  /preparations/{id}
- GET  /healthz
"""

from __future__ import annotations

import asyncio
import secrets
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import desc, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

from signal_tracker.auth import (
    SESSION_USER_ID_KEY,
    hash_password,
    login_user,
    logout_user,
    verify_password,
)
from signal_tracker.auth.session import touch_last_login
from signal_tracker.classifier.feedback import VALID_FEEDBACK
from signal_tracker.config import get_settings, load_user_profile, resolve_db_url
from signal_tracker.preparation.cv import CVExtractionError, extract_text
from signal_tracker.preparation.llm import (
    PreparationError,
    cv_profile_to_prompt_block,
    generate_cv_profile,
    generate_preparation,
)
from signal_tracker.preparation.schemas import CVProfile
from signal_tracker.storage import Database, init_db
from signal_tracker.storage.models import (
    Preparation,
    RawItem,
    SearchRun,
    Signal,
    SignalFeedback,
    User,
    UserCV,
    UserKeyword,
    WatchlistEntry,
)
from signal_tracker.utils.logging import get_logger
from signal_tracker.utils.normalize import normalize_company_name

logger = get_logger(__name__)

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
KEYWORD_CATEGORIES = ("field", "job_title", "other")

# Paths that do NOT require an authenticated user.
PUBLIC_PATHS = {"/healthz", "/login", "/signup", "/logout"}


def _idle_state() -> dict[str, Any]:
    return {
        "status": "idle",
        "step": None,
        "current_run_id": None,
        "metrics": {},
        "error": None,
    }


def build_app(db: Database | None = None) -> FastAPI:
    """Build the FastAPI app, optionally injecting a custom Database (tests)."""
    settings = get_settings()
    app_db = db or init_db(resolve_db_url(settings))
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

    def get_session_dep() -> Iterable[Session]:
        with app_db.session() as session:
            yield session

    app = FastAPI(title="Signal Tracker", version="0.4.0")

    # Per-user pipeline status. Keyed by user_id.
    task_states: dict[int, dict[str, Any]] = {}
    bg_tasks: set[asyncio.Task[None]] = set()

    def _state_for(uid: int) -> dict[str, Any]:
        return task_states.setdefault(uid, _idle_state())

    def _keyword_snapshot(session: Session, user_id: int) -> dict[str, list[str]]:
        snap: dict[str, list[str]] = {c: [] for c in KEYWORD_CATEGORIES}
        for kw in session.execute(
            select(UserKeyword).where(UserKeyword.user_id == user_id)
        ).scalars():
            snap.setdefault(kw.category, []).append(kw.value)
        return snap

    async def _run_search_task(run_id: int, user_id: int) -> None:
        from signal_tracker.pipeline import run_classification, run_collection

        state = _state_for(user_id)
        state.update(
            status="running", step="collect", current_run_id=run_id,
            metrics={}, error=None,
        )
        try:
            coll = await run_collection(db=app_db)
            state["metrics"]["collect"] = {
                "fetched": coll.fetched, "new": coll.new, "duplicates": coll.duplicates,
            }
            state["step"] = "classify"
            clf = await run_classification(
                profile=load_user_profile(),
                db=app_db,
                search_run_id=run_id,
                user_id=user_id,
            )
            metrics = {
                "collect": state["metrics"]["collect"],
                "classify": {
                    "processed": clf.processed,
                    "relevant": clf.relevant,
                    "signals_created": clf.signals_created,
                    "signals_deduped": clf.signals_deduped,
                    "errors": clf.errors,
                },
            }
            state["metrics"] = metrics
            state.update(status="done", step=None)
            with app_db.session() as session:
                run = session.get(SearchRun, run_id)
                if run is not None:
                    run.status = "done"
                    run.metrics = metrics
                    run.finished_at = datetime.now(tz=UTC)
        except Exception as exc:
            state.update(status="failed", step=None, error=str(exc)[:500])
            with app_db.session() as session:
                run = session.get(SearchRun, run_id)
                if run is not None:
                    run.status = "failed"
                    run.error = str(exc)[:500]
                    run.finished_at = datetime.now(tz=UTC)

    @app.middleware("http")
    async def auth_middleware(request: Request, call_next):  # type: ignore[no-untyped-def]
        # Resolve user from session cookie. Public paths skip the check.
        user: User | None = None
        uid = request.session.get(SESSION_USER_ID_KEY)
        if isinstance(uid, int):
            with app_db.session() as s:
                u = s.get(User, uid)
                if u is not None and u.is_active:
                    s.expunge(u)
                    user = u
        request.state.user = user

        if request.url.path in PUBLIC_PATHS:
            return await call_next(request)
        if user is None:
            # API clients (no Accept: text/html) get 401; browsers redirect.
            accepts_html = "text/html" in (request.headers.get("accept") or "")
            if accepts_html:
                return RedirectResponse(url="/login", status_code=303)
            return Response(status_code=401, content="Authentication required")
        return await call_next(request)

    # SessionMiddleware is added LAST so it ends up OUTERMOST in the
    # Starlette stack (add_middleware prepends). That way request.session
    # is populated before our auth_middleware reads it.
    session_secret = settings.session_secret_key or secrets.token_urlsafe(32)
    app.add_middleware(
        SessionMiddleware,
        secret_key=session_secret,
        max_age=14 * 24 * 3600,  # 14 days
        same_site="lax",
        https_only=False,  # behind Cloudflare tunnel — TLS terminates upstream
    )

    def require_user(request: Request) -> User:
        user: User | None = getattr(request.state, "user", None)
        if user is None:
            raise HTTPException(status_code=401, detail="Authentication required")
        return user

    # =========================================================================
    # Auth endpoints
    # =========================================================================
    @app.get("/login", response_class=HTMLResponse)
    def login_form(request: Request, next: Annotated[str, Query()] = "/") -> HTMLResponse:
        return templates.TemplateResponse(
            request, "login.html.j2",
            {"error": None, "next": next, "task_status": "idle", "current_run_id": None},
        )

    @app.post("/login")
    def login_submit(
        request: Request,
        email: Annotated[str, Form()],
        password: Annotated[str, Form()],
        next: Annotated[str, Form()] = "/",
        session: Session = Depends(get_session_dep),
    ) -> Response:
        email_clean = email.strip().lower()
        user = session.execute(
            select(User).where(User.email == email_clean)
        ).scalar_one_or_none()
        if user is None or not user.is_active or not verify_password(password, user.password_hash):
            return templates.TemplateResponse(
                request, "login.html.j2",
                {
                    "error": "Email ou mot de passe incorrect.",
                    "next": next, "task_status": "idle", "current_run_id": None,
                },
                status_code=401,
            )
        touch_last_login(session, user)
        login_user(request, user)
        return RedirectResponse(url=_safe_redirect(next), status_code=303)

    @app.get("/signup", response_class=HTMLResponse)
    def signup_form(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request, "signup.html.j2",
            {"error": None, "task_status": "idle", "current_run_id": None},
        )

    @app.post("/signup")
    def signup_submit(
        request: Request,
        email: Annotated[str, Form()],
        password: Annotated[str, Form()],
        password_confirm: Annotated[str, Form()],
        session: Session = Depends(get_session_dep),
    ) -> Response:
        email_clean = email.strip().lower()
        err: str | None = None
        if "@" not in email_clean or "." not in email_clean:
            err = "Email invalide."
        elif len(password) < 6:
            err = "Mot de passe trop court (6 caractères minimum)."
        elif password != password_confirm:
            err = "Les deux mots de passe ne correspondent pas."
        if err is None:
            existing = session.execute(
                select(User).where(User.email == email_clean)
            ).scalar_one_or_none()
            if existing is not None:
                err = "Un compte existe déjà avec cet email."
        if err is not None:
            return templates.TemplateResponse(
                request, "signup.html.j2",
                {"error": err, "task_status": "idle", "current_run_id": None},
                status_code=400,
            )
        user = User(
            email=email_clean,
            password_hash=hash_password(password),
            is_active=True,
            is_owner=False,
        )
        session.add(user)
        session.flush()
        touch_last_login(session, user)
        login_user(request, user)
        return RedirectResponse(url="/", status_code=303)

    @app.post("/logout")
    def logout(request: Request) -> RedirectResponse:
        logout_user(request)
        return RedirectResponse(url="/login", status_code=303)

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    # =========================================================================
    # Landing
    # =========================================================================
    @app.get("/", response_class=HTMLResponse)
    def landing(
        request: Request,
        user: User = Depends(require_user),
        session: Session = Depends(get_session_dep),
    ) -> HTMLResponse:
        kw_rows = list(
            session.execute(
                select(UserKeyword)
                .where(UserKeyword.user_id == user.id)
                .order_by(UserKeyword.category, UserKeyword.value)
            ).scalars()
        )
        keywords_by_cat: dict[str, list[UserKeyword]] = {c: [] for c in KEYWORD_CATEGORIES}
        for kw in kw_rows:
            keywords_by_cat.setdefault(kw.category, []).append(kw)

        runs = list(
            session.execute(
                select(SearchRun)
                .where(SearchRun.user_id == user.id)
                .order_by(desc(SearchRun.created_at)).limit(25)
            ).scalars()
        )
        watchlist = list(
            session.execute(
                select(WatchlistEntry)
                .where(WatchlistEntry.user_id == user.id)
                .order_by(WatchlistEntry.company_name)
            ).scalars()
        )
        # User's total signals = signals attached to one of their runs.
        run_ids = [r.id for r in runs] or [-1]
        total_signals = session.execute(
            select(func.count(Signal.id)).where(Signal.search_run_id.in_(run_ids))
        ).scalar_one()

        state = _state_for(user.id)
        return templates.TemplateResponse(
            request, "search.html.j2",
            {
                "keyword_categories": KEYWORD_CATEGORIES,
                "keywords_by_cat": keywords_by_cat,
                "runs": runs,
                "watchlist": watchlist,
                "total_signals": total_signals,
                "task_status": state["status"],
                "current_run_id": state["current_run_id"],
                "user": user,
            },
        )

    # =========================================================================
    # Results
    # =========================================================================
    @app.get("/results", response_class=HTMLResponse)
    def results(
        request: Request,
        run: Annotated[int | None, Query()] = None,
        feedback: Annotated[str | None, Query()] = None,
        min_score: Annotated[float, Query(ge=0, le=100)] = 0.0,
        limit: Annotated[int, Query(ge=1, le=500)] = 200,
        user: User = Depends(require_user),
        session: Session = Depends(get_session_dep),
    ) -> HTMLResponse:
        # User's runs only.
        user_run_ids = [
            r for (r,) in session.execute(
                select(SearchRun.id).where(SearchRun.user_id == user.id)
            ).all()
        ]
        if not user_run_ids:
            user_run_ids = [-1]

        # Per-user feedback lookup: signal_id -> action.
        fb_rows = session.execute(
            select(SignalFeedback.signal_id, SignalFeedback.action)
            .where(SignalFeedback.user_id == user.id)
        ).all()
        feedback_by_signal: dict[int, str] = {sid: a for sid, a in fb_rows}

        stmt = (
            select(Signal, RawItem)
            .join(RawItem, Signal.raw_item_id == RawItem.id)
            .where(Signal.total_score >= min_score)
            .where(Signal.search_run_id.in_(user_run_ids))
            .order_by(desc(Signal.total_score))
            .limit(limit)
        )
        if run is not None:
            stmt = stmt.where(Signal.search_run_id == run)
        if feedback == "pending":
            stmt = stmt.where(~Signal.id.in_(
                select(SignalFeedback.signal_id).where(SignalFeedback.user_id == user.id)
            ))
        elif feedback in VALID_FEEDBACK:
            stmt = stmt.where(Signal.id.in_(
                select(SignalFeedback.signal_id)
                .where(SignalFeedback.user_id == user.id)
                .where(SignalFeedback.action == feedback)
            ))

        rows = list(session.execute(stmt))

        focus_run_id = run
        if focus_run_id is None:
            latest = session.execute(
                select(SearchRun.id)
                .where(SearchRun.user_id == user.id)
                .order_by(desc(SearchRun.created_at)).limit(1)
            ).scalar_one_or_none()
            focus_run_id = latest
        focus_run = (
            session.get(SearchRun, focus_run_id)
            if focus_run_id and focus_run_id in user_run_ids else None
        )

        state = _state_for(user.id)
        return templates.TemplateResponse(
            request, "results.html.j2",
            {
                "rows": rows,
                "feedback_by_signal": feedback_by_signal,
                "filter_feedback": feedback or "",
                "min_score": min_score,
                "valid_feedback": VALID_FEEDBACK,
                "run_id": run,
                "focus_run_id": focus_run_id,
                "focus_run": focus_run,
                "task_status": state["status"],
                "current_run_id": state["current_run_id"],
                "user": user,
            },
        )

    @app.post("/search")
    async def launch_search(
        request: Request,
        label: Annotated[str | None, Form()] = None,
        user: User = Depends(require_user),
    ) -> RedirectResponse:
        state = _state_for(user.id)
        if state["status"] == "running":
            rid = state["current_run_id"]
            target = f"/results?run={rid}" if rid else "/results"
            return RedirectResponse(url=target, status_code=303)

        now = datetime.now(tz=UTC)
        with app_db.session() as session:
            snapshot = _keyword_snapshot(session, user.id)
            srun = SearchRun(
                user_id=user.id,
                label=(label or "").strip()
                or f"Recherche du {now.strftime('%d/%m %H:%M')}",
                status="running",
                keywords=snapshot,
            )
            session.add(srun)
            session.flush()
            run_id = srun.id

        task = asyncio.create_task(_run_search_task(run_id, user.id))
        bg_tasks.add(task)
        task.add_done_callback(bg_tasks.discard)
        return RedirectResponse(url=f"/results?run={run_id}", status_code=303)

    @app.get("/run/status")
    def run_status(user: User = Depends(require_user)) -> JSONResponse:
        return JSONResponse(_state_for(user.id))

    @app.post("/searches/{run_id}/delete")
    def delete_search(
        run_id: int,
        user: User = Depends(require_user),
        session: Session = Depends(get_session_dep),
    ) -> RedirectResponse:
        srun = session.get(SearchRun, run_id)
        if srun is not None and srun.user_id == user.id:
            session.execute(
                update(Signal)
                .where(Signal.search_run_id == run_id)
                .values(search_run_id=None)
            )
            session.delete(srun)
        return RedirectResponse(url="/", status_code=303)

    # =========================================================================
    # Feedback (per-user via SignalFeedback)
    # =========================================================================
    @app.post("/signals/{signal_id}/feedback")
    def set_feedback(
        signal_id: int,
        action: Annotated[str, Form()],
        redirect_to: Annotated[str, Form()] = "/results",
        user: User = Depends(require_user),
        session: Session = Depends(get_session_dep),
    ) -> RedirectResponse:
        if action not in VALID_FEEDBACK:
            raise HTTPException(status_code=400, detail=f"Invalid action: {action}")
        signal = session.get(Signal, signal_id)
        if signal is None:
            raise HTTPException(status_code=404, detail="Signal not found")
        existing = session.execute(
            select(SignalFeedback)
            .where(SignalFeedback.user_id == user.id)
            .where(SignalFeedback.signal_id == signal_id)
        ).scalar_one_or_none()
        if existing is None:
            session.add(SignalFeedback(
                user_id=user.id, signal_id=signal_id, action=action,
            ))
        else:
            existing.action = action
        return RedirectResponse(url=_safe_redirect(redirect_to), status_code=303)

    @app.get("/signals/{signal_id}/contacted")
    def mark_contacted(
        signal_id: int,
        user: User = Depends(require_user),
        session: Session = Depends(get_session_dep),
    ) -> RedirectResponse:
        signal = session.get(Signal, signal_id)
        if signal is None:
            raise HTTPException(status_code=404, detail="Signal not found")
        existing = session.execute(
            select(SignalFeedback)
            .where(SignalFeedback.user_id == user.id)
            .where(SignalFeedback.signal_id == signal_id)
        ).scalar_one_or_none()
        if existing is None:
            session.add(SignalFeedback(
                user_id=user.id, signal_id=signal_id, action="contacted",
            ))
        else:
            existing.action = "contacted"
        return RedirectResponse(url="/results", status_code=303)

    # =========================================================================
    # Watchlist
    # =========================================================================
    @app.post("/watchlist")
    def add_watchlist(
        company_name: Annotated[str, Form()],
        notes: Annotated[str | None, Form()] = None,
        user: User = Depends(require_user),
        session: Session = Depends(get_session_dep),
    ) -> RedirectResponse:
        normalized = normalize_company_name(company_name)
        if not normalized:
            raise HTTPException(status_code=400, detail="Empty company name")
        session.add(WatchlistEntry(
            user_id=user.id,
            company_name=company_name.strip(),
            normalized_name=normalized,
            notes=(notes or None),
        ))
        try:
            session.flush()
        except IntegrityError:
            session.rollback()
        return RedirectResponse(url="/", status_code=303)

    @app.post("/watchlist/{entry_id}/delete")
    def delete_watchlist(
        entry_id: int,
        user: User = Depends(require_user),
        session: Session = Depends(get_session_dep),
    ) -> RedirectResponse:
        entry = session.get(WatchlistEntry, entry_id)
        if entry is None or entry.user_id != user.id:
            raise HTTPException(status_code=404, detail="Watchlist entry not found")
        session.delete(entry)
        return RedirectResponse(url="/", status_code=303)

    # =========================================================================
    # Keywords
    # =========================================================================
    @app.post("/keywords")
    def add_keyword(
        category: Annotated[str, Form()],
        value: Annotated[str, Form()],
        user: User = Depends(require_user),
        session: Session = Depends(get_session_dep),
    ) -> RedirectResponse:
        if category not in KEYWORD_CATEGORIES:
            raise HTTPException(status_code=400, detail=f"Invalid category: {category}")
        value = value.strip()
        if not value:
            raise HTTPException(status_code=400, detail="Empty value")
        session.add(UserKeyword(user_id=user.id, category=category, value=value))
        try:
            session.flush()
        except IntegrityError:
            session.rollback()
        return RedirectResponse(url="/", status_code=303)

    @app.post("/keywords/{keyword_id}/delete")
    def delete_keyword(
        keyword_id: int,
        user: User = Depends(require_user),
        session: Session = Depends(get_session_dep),
    ) -> RedirectResponse:
        kw = session.get(UserKeyword, keyword_id)
        if kw is None or kw.user_id != user.id:
            raise HTTPException(status_code=404, detail="Keyword not found")
        session.delete(kw)
        return RedirectResponse(url="/", status_code=303)

    # =========================================================================
    # Preparation
    # =========================================================================
    def _user_owns_signal(session: Session, signal_id: int, user_id: int) -> bool:
        """A user 'owns' a signal if it came out of one of their search runs."""
        signal = session.get(Signal, signal_id)
        if signal is None or signal.search_run_id is None:
            return False
        srun = session.get(SearchRun, signal.search_run_id)
        return srun is not None and srun.user_id == user_id

    @app.get("/signals/{signal_id}/prepare", response_class=HTMLResponse)
    def prepare_form(
        request: Request,
        signal_id: int,
        user: User = Depends(require_user),
        session: Session = Depends(get_session_dep),
    ) -> HTMLResponse:
        signal = session.get(Signal, signal_id)
        if signal is None or not _user_owns_signal(session, signal_id, user.id):
            raise HTTPException(status_code=404, detail="Signal not found")
        raw = session.get(RawItem, signal.raw_item_id)
        cv = session.execute(
            select(UserCV).where(UserCV.user_id == user.id).limit(1)
        ).scalar_one_or_none()
        latest_prep = session.execute(
            select(Preparation)
            .where(Preparation.signal_id == signal_id)
            .where(Preparation.user_id == user.id)
            .order_by(desc(Preparation.created_at)).limit(1)
        ).scalar_one_or_none()
        state = _state_for(user.id)
        return templates.TemplateResponse(
            request, "prepare.html.j2",
            {
                "signal": signal, "raw": raw, "cv": cv,
                "latest_prep": latest_prep,
                "task_status": state["status"],
                "current_run_id": state["current_run_id"],
                "user": user,
            },
        )

    @app.post("/signals/{signal_id}/prepare")
    async def prepare_submit(
        signal_id: int,
        cv_file: Annotated[UploadFile | None, File()] = None,
        cv_text: Annotated[str, Form()] = "",
        save_cv: Annotated[str, Form()] = "",
        user: User = Depends(require_user),
    ) -> RedirectResponse:
        # 1. Resolve CV text. File upload > pasted text > stored CV.
        text_value = (cv_text or "").strip()
        filename: str | None = None
        cached_profile: dict[str, Any] | None = None
        uploaded_new_cv = False
        if cv_file is not None and cv_file.filename:
            data = await cv_file.read()
            filename = cv_file.filename
            try:
                extracted = extract_text(filename, data)
            except CVExtractionError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            if extracted.strip():
                text_value = extracted.strip()
                uploaded_new_cv = True

        if not text_value:
            with app_db.session() as session:
                stored = session.execute(
                    select(UserCV).where(UserCV.user_id == user.id).limit(1)
                ).scalar_one_or_none()
                if stored is not None:
                    text_value = stored.text
                    filename = filename or stored.filename
                    cached_profile = stored.profile_json
        elif not uploaded_new_cv:
            with app_db.session() as session:
                stored = session.execute(
                    select(UserCV).where(UserCV.user_id == user.id).limit(1)
                ).scalar_one_or_none()
                if stored is not None and stored.text == text_value:
                    cached_profile = stored.profile_json

        if not text_value:
            raise HTTPException(
                status_code=400,
                detail="No CV provided. Upload a PDF or paste your CV text.",
            )

        # 2. Save + distill into CVProfile if asked.
        new_profile_dict: dict[str, Any] | None = None
        if save_cv == "on":
            try:
                cv_profile = await generate_cv_profile(text_value)
                new_profile_dict = cv_profile.model_dump()
            except Exception as exc:
                logger.warning(
                    "preparation.cv_profile_failed_using_raw error=%s", str(exc)[:200],
                )
                new_profile_dict = None
            with app_db.session() as session:
                for old in session.execute(
                    select(UserCV).where(UserCV.user_id == user.id)
                ).scalars():
                    session.delete(old)
                session.flush()
                session.add(UserCV(
                    user_id=user.id,
                    filename=filename,
                    text=text_value,
                    char_count=len(text_value),
                    profile_json=new_profile_dict,
                ))
            cached_profile = new_profile_dict

        # 3. Load context, call LLM, persist report.
        with app_db.session() as session:
            signal = session.get(Signal, signal_id)
            if signal is None or not _user_owns_signal(session, signal_id, user.id):
                raise HTTPException(status_code=404, detail="Signal not found")
            raw = session.get(RawItem, signal.raw_item_id)
            company_name = signal.company_name
            signal_type = signal.signal_type
            recommended_action = signal.recommended_action
            total_score = signal.total_score
            summary_fr = signal.summary_fr
            suggested_angle = signal.suggested_angle
            source = raw.source if raw else "—"
            url = raw.url if raw else None
            title = raw.title if raw else None
            content = raw.content if raw else None

        if cached_profile is not None:
            try:
                cv_payload = cv_profile_to_prompt_block(CVProfile.model_validate(cached_profile))
            except Exception:
                cv_payload = text_value
        else:
            cv_payload = text_value

        try:
            report = await generate_preparation(
                company_name=company_name,
                signal_type=signal_type,
                recommended_action=recommended_action,
                total_score=total_score,
                summary_fr=summary_fr,
                suggested_angle=suggested_angle,
                source=source, url=url, title=title, content=content,
                profile=load_user_profile(),
                cv_text=cv_payload,
            )
            status = "done"
            error_msg = None
            report_dict: dict[str, Any] | None = report.model_dump()
        except (PreparationError, Exception) as exc:
            status = "failed"
            error_msg = str(exc)[:500]
            report_dict = None

        with app_db.session() as session:
            prep = Preparation(
                user_id=user.id,
                signal_id=signal_id,
                status=status,
                report=report_dict,
                cv_excerpt=text_value[:2000],
                error=error_msg,
            )
            session.add(prep)
            session.flush()
            prep_id = prep.id

        return RedirectResponse(url=f"/preparations/{prep_id}", status_code=303)

    @app.get("/preparations/{prep_id}", response_class=HTMLResponse)
    def view_preparation(
        request: Request,
        prep_id: int,
        user: User = Depends(require_user),
        session: Session = Depends(get_session_dep),
    ) -> HTMLResponse:
        prep = session.get(Preparation, prep_id)
        if prep is None or prep.user_id != user.id:
            raise HTTPException(status_code=404, detail="Preparation not found")
        signal = session.get(Signal, prep.signal_id)
        raw = session.get(RawItem, signal.raw_item_id) if signal else None
        state = _state_for(user.id)
        return templates.TemplateResponse(
            request, "preparation.html.j2",
            {
                "prep": prep, "report": prep.report,
                "signal": signal, "raw": raw,
                "task_status": state["status"],
                "current_run_id": state["current_run_id"],
                "user": user,
            },
        )

    return app


def _safe_redirect(target: str) -> str:
    """Only allow same-site relative redirects (defense against open redirect)."""
    if target.startswith("/") and not target.startswith("//"):
        return target
    return "/"


# Module-level app for `uvicorn signal_tracker.dashboard.app:app`.
app = build_app()

# func is referenced in module-level imports so ruff doesn't drop it; the
# landing route uses it for the per-user signal count.
_ = func

__all__ = ["app", "build_app"]
