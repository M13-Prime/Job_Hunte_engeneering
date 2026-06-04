"""FastAPI dashboard (Phase 8 multi-tenant).

Email + password accounts, signed-cookie sessions, per-user scoping on:
keywords, watchlist, CV, search runs, preparations, signal feedback.
Signals + raw items stay shared (one news corpus).

Routes:
- GET  /signup | POST /signup
- GET  /login  | POST /login
- POST /logout
- GET  /                landing (auth) — CV card in sidebar
- POST /cv | POST /cv/delete
- GET  /results         signals table (auth, per-user feedback)
- POST /search
- GET  /run/status      JSON, per-user
- POST /searches/{id}/delete
- POST /signals/{id}/feedback | GET /signals/{id}/contacted
- POST /watchlist | /watchlist/{id}/delete
- POST /keywords  | /keywords/{id}/delete
- GET  /signals/{id}/prepare | POST /signals/{id}/prepare
- GET  /preparations              list (auth)
- GET  /preparations/{id}         report
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
from starlette.responses import StreamingResponse

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
PUBLIC_PATHS = {
    "/healthz", "/login", "/signup", "/logout", "/pending",
    "/manifest.webmanifest",
}

# PWA manifest. Inline (no static files needed) — served verbatim by /manifest.webmanifest.
# Icons use the same green/S motif as the apple-touch-icon defined in base.html.j2.
_ICON_SVG = (
    "data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 512 512'>"
    "<rect width='512' height='512' rx='112' fill='%2334c759'/>"
    "<text x='256' y='362' font-family='-apple-system,BlinkMacSystemFont,sans-serif' "
    "font-size='312' font-weight='800' text-anchor='middle' fill='white'>S</text></svg>"
)
PWA_MANIFEST: dict[str, Any] = {
    "name": "Signal Tracker",
    "short_name": "Signals",
    "description": "Détecte les signaux d'embauche avant les offres publiques.",
    "start_url": "/",
    "scope": "/",
    "display": "standalone",
    "orientation": "any",
    "background_color": "#f5f5f7",
    "theme_color": "#34c759",
    "lang": "fr",
    "icons": [
        {"src": _ICON_SVG, "sizes": "any", "type": "image/svg+xml", "purpose": "any"},
        {"src": _ICON_SVG, "sizes": "192x192", "type": "image/svg+xml", "purpose": "maskable"},
        {"src": _ICON_SVG, "sizes": "512x512", "type": "image/svg+xml", "purpose": "maskable"},
    ],
}


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
        from signal_tracker.agents.source_picker import (
            load_source_registry,
            pick_sources,
        )
        from signal_tracker.pipeline import (
            build_collectors_for_selection,
            run_classification,
            run_collection,
        )

        state = _state_for(user_id)
        state.update(
            status="running", step="collect", current_run_id=run_id,
            metrics={}, error=None,
        )
        try:
            # Phase 10 feature 3 — let the LLM pick which curated domains
            # to activate for this user. If the registry is empty or the
            # picker is disabled, this falls back to all domains, then
            # the collectors fall back to the legacy static config when
            # there's nothing to materialize.
            profile = load_user_profile()
            with app_db.session() as session:
                user_keywords = _keyword_snapshot(session, user_id)
            selection = await pick_sources(profile, user_keywords)
            registry = load_source_registry()
            picker_collectors = (
                build_collectors_for_selection(selection) if registry else []
            )
            # Persist the picker decision on the SearchRun so the UI can
            # show which domains powered the run later.
            with app_db.session() as session:
                run = session.get(SearchRun, run_id)
                if run is not None:
                    run.selected_sources = selection.to_metadata()
            coll = await run_collection(
                collectors=picker_collectors or None,
                db=app_db,
            )
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
                    "prefiltered_out": clf.prefiltered_out,
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
        # Phase 9: unapproved users get a holding page; nothing else is
        # reachable until an admin approves them.
        if not user.is_approved:
            accepts_html = "text/html" in (request.headers.get("accept") or "")
            if accepts_html:
                return RedirectResponse(url="/pending", status_code=303)
            return Response(status_code=403, content="Account pending approval")
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

    def require_admin(request: Request) -> User:
        """Owner-only routes. is_owner is the admin bit (Phase 9)."""
        user = require_user(request)
        if not user.is_owner:
            raise HTTPException(status_code=403, detail="Admin access required")
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
        # Phase 9: still let the cookie be set so the holding page recognizes
        # them, but the middleware will keep them off everything else.
        touch_last_login(session, user)
        login_user(request, user)
        if not user.is_approved:
            return RedirectResponse(url="/pending", status_code=303)
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
        # Phase 9: the FIRST user to sign up on a fresh instance becomes
        # owner + auto-approved so the deployment is never admin-less.
        # Everyone after that lands in /pending until an admin approves.
        total_users = session.execute(select(func.count(User.id))).scalar_one()
        is_first = total_users == 0
        user = User(
            email=email_clean,
            password_hash=hash_password(password),
            is_active=True,
            is_owner=is_first,
            is_approved=is_first,
            approved_at=datetime.now(tz=UTC) if is_first else None,
        )
        session.add(user)
        session.flush()
        if is_first:
            user.approved_by_id = user.id  # self-approved
        touch_last_login(session, user)
        login_user(request, user)
        if not user.is_approved:
            return RedirectResponse(url="/pending", status_code=303)
        return RedirectResponse(url="/", status_code=303)

    @app.post("/logout")
    def logout(request: Request) -> RedirectResponse:
        logout_user(request)
        return RedirectResponse(url="/login", status_code=303)

    @app.get("/pending", response_class=HTMLResponse)
    def pending(request: Request) -> HTMLResponse:
        """Holding page for accounts waiting on admin approval."""
        user: User | None = getattr(request.state, "user", None)
        return templates.TemplateResponse(
            request, "pending.html.j2",
            {
                "user": user, "task_status": "idle", "current_run_id": None,
            },
        )

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/manifest.webmanifest")
    def pwa_manifest() -> JSONResponse:
        """PWA manifest — lets iOS / Android "Add to Home Screen" produce a
        standalone-launching shortcut with the right name + icon + theme."""
        return JSONResponse(
            PWA_MANIFEST,
            media_type="application/manifest+json",
            headers={"Cache-Control": "public, max-age=3600"},
        )

    # =========================================================================
    # Admin: account validation (Phase 9)
    # =========================================================================
    @app.get("/admin/users", response_class=HTMLResponse)
    def admin_users(
        request: Request,
        user: User = Depends(require_admin),
        session: Session = Depends(get_session_dep),
    ) -> HTMLResponse:
        rows = list(session.execute(
            select(User).order_by(User.is_approved.asc(), desc(User.created_at))
        ).scalars())
        pending_count = sum(1 for u in rows if not u.is_approved)
        return templates.TemplateResponse(
            request, "admin_users.html.j2",
            {
                "users": rows,
                "pending_count": pending_count,
                "user": user,
                "task_status": "idle",
                "current_run_id": None,
            },
        )

    def _act_on_user(
        session: Session, admin: User, target_id: int,
    ) -> User:
        target = session.get(User, target_id)
        if target is None:
            raise HTTPException(status_code=404, detail="User not found")
        if target.id == admin.id:
            raise HTTPException(
                status_code=400,
                detail="Tu ne peux pas modifier ton propre compte ici.",
            )
        return target

    @app.post("/admin/users/{user_id}/approve")
    def admin_approve(
        user_id: int,
        admin: User = Depends(require_admin),
        session: Session = Depends(get_session_dep),
    ) -> RedirectResponse:
        target = _act_on_user(session, admin, user_id)
        if not target.is_approved:
            target.is_approved = True
            target.is_active = True
            target.approved_at = datetime.now(tz=UTC)
            target.approved_by_id = admin.id
            logger.info(
                "admin.user_approved by=%s target=%s", admin.email, target.email,
            )
        return RedirectResponse(url="/admin/users", status_code=303)

    @app.post("/admin/users/{user_id}/revoke")
    def admin_revoke(
        user_id: int,
        admin: User = Depends(require_admin),
        session: Session = Depends(get_session_dep),
    ) -> RedirectResponse:
        target = _act_on_user(session, admin, user_id)
        if target.is_owner:
            raise HTTPException(
                status_code=400,
                detail="Retire d'abord le rôle admin avant de révoquer.",
            )
        target.is_approved = False
        logger.info(
            "admin.user_revoked by=%s target=%s", admin.email, target.email,
        )
        return RedirectResponse(url="/admin/users", status_code=303)

    @app.post("/admin/users/{user_id}/promote")
    def admin_promote(
        user_id: int,
        admin: User = Depends(require_admin),
        session: Session = Depends(get_session_dep),
    ) -> RedirectResponse:
        target = _act_on_user(session, admin, user_id)
        if not target.is_approved:
            raise HTTPException(
                status_code=400,
                detail="Approuve d'abord le compte avant de le promouvoir.",
            )
        target.is_owner = True
        logger.info(
            "admin.user_promoted by=%s target=%s", admin.email, target.email,
        )
        return RedirectResponse(url="/admin/users", status_code=303)

    @app.post("/admin/users/{user_id}/demote")
    def admin_demote(
        user_id: int,
        admin: User = Depends(require_admin),
        session: Session = Depends(get_session_dep),
    ) -> RedirectResponse:
        target = _act_on_user(session, admin, user_id)
        # Don't let the last admin demote — would lock everyone out of /admin.
        other_owners = session.execute(
            select(func.count(User.id))
            .where(User.is_owner.is_(True))
            .where(User.id != target.id)
        ).scalar_one()
        if other_owners == 0:
            raise HTTPException(
                status_code=400,
                detail="Au moins un admin doit rester. Promeus quelqu'un d'autre d'abord.",
            )
        target.is_owner = False
        logger.info(
            "admin.user_demoted by=%s target=%s", admin.email, target.email,
        )
        return RedirectResponse(url="/admin/users", status_code=303)

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
        # Current CV (or None) for the sidebar card.
        cv = session.execute(
            select(UserCV).where(UserCV.user_id == user.id).limit(1)
        ).scalar_one_or_none()

        # CV-suggested keywords (Phase 10): pull from profile_json, filter out
        # any already present in the user's UserKeyword table so we never
        # suggest something they've already added.
        suggested_keywords: dict[str, list[str]] = {c: [] for c in KEYWORD_CATEGORIES}
        if cv is not None and cv.profile_json:
            raw_sug = (cv.profile_json or {}).get("suggested_keywords") or {}
            active_by_cat: dict[str, set[str]] = {
                c: {k.value.lower() for k in keywords_by_cat.get(c, [])}
                for c in KEYWORD_CATEGORIES
            }
            for cat in KEYWORD_CATEGORIES:
                items = raw_sug.get(cat) or []
                if not isinstance(items, list):
                    continue
                # De-dupe, drop empties, drop anything already added.
                seen: set[str] = set()
                deduped: list[str] = []
                for item in items:
                    if not isinstance(item, str):
                        continue
                    cleaned = item.strip()
                    key = cleaned.lower()
                    if not cleaned or key in seen or key in active_by_cat[cat]:
                        continue
                    seen.add(key)
                    deduped.append(cleaned)
                suggested_keywords[cat] = deduped[:8]

        state = _state_for(user.id)
        return templates.TemplateResponse(
            request, "search.html.j2",
            {
                "keyword_categories": KEYWORD_CATEGORIES,
                "keywords_by_cat": keywords_by_cat,
                "suggested_keywords": suggested_keywords,
                "runs": runs,
                "watchlist": watchlist,
                "cv": cv,
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
        country: Annotated[list[str] | None, Query()] = None,
        country_scope: Annotated[str, Query()] = "any",
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

        # Phase 10 — country filter.
        # country_scope=hq  → match against Signal.hq_country only
        # country_scope=any → match against hq_country OR active_countries
        #                     (= companies present in that country, HQ or not)
        selected_countries = [c.strip() for c in (country or []) if c.strip()]
        valid_scope = country_scope if country_scope in {"hq", "any"} else "any"
        if selected_countries:
            if valid_scope == "hq":
                stmt = stmt.where(Signal.hq_country.in_(selected_countries))
            else:
                # SQLite can't index JSON-contains, so fall back to LIKE on the
                # serialized array. Pre-filter on hq_country to keep planning
                # decent — most matching rows match through HQ anyway.
                from sqlalchemy import or_
                conds = [Signal.hq_country.in_(selected_countries)]
                for c in selected_countries:
                    # Quoted exact-token match within the JSON array dump.
                    conds.append(Signal.active_countries.like(f'%"{c}"%'))
                stmt = stmt.where(or_(*conds))

        rows = list(session.execute(stmt))

        # Build the list of countries present in this user's data — drives
        # the multi-select in the UI.
        country_rows = session.execute(
            select(Signal.hq_country)
            .where(Signal.search_run_id.in_(user_run_ids))
            .where(Signal.hq_country.is_not(None))
            .distinct()
        ).all()
        available_countries = sorted({c for (c,) in country_rows if c})
        # Always expose a starter list of common ones so an empty corpus
        # still has something selectable.
        STARTER_COUNTRIES = (
            "France", "Belgique", "Luxembourg", "Suisse", "Allemagne",
            "Pays-Bas", "Espagne", "Italie", "Royaume-Uni",
            "Maroc", "Tunisie", "Algérie", "Sénégal",
            "États-Unis", "Canada",
        )
        available_countries = sorted(set(available_countries) | set(STARTER_COUNTRIES))

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
                "selected_countries": selected_countries,
                "country_scope": valid_scope,
                "available_countries": available_countries,
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

    @app.get("/run/events")
    async def run_events(user: User = Depends(require_user)) -> StreamingResponse:
        """Server-sent events stream for the current user's pipeline state.

        Replaces the 2-3s ``/run/status`` polling with a single long-lived
        connection that pushes deltas as they happen. Wakes up roughly
        every 500ms server-side to check the in-memory state dict; only
        emits an event when the state changed (or after a 15s heartbeat
        so proxies / Cloudflare don't drop the idle connection).
        """
        import json as _json

        async def gen() -> Any:
            last_serialized: str | None = None
            heartbeat_every = 15.0
            last_heartbeat = 0.0
            elapsed = 0.0
            poll_interval = 0.5
            # Emit an initial snapshot so the client renders something
            # immediately without waiting for a state change.
            initial = _state_for(user.id)
            last_serialized = _json.dumps(initial, sort_keys=True, default=str)
            yield f"data: {last_serialized}\n\n"
            # Cap the stream lifetime to 10 minutes — clients reconnect
            # transparently. Avoids leaking connections if the browser
            # forgets to close.
            while elapsed < 600.0:
                await asyncio.sleep(poll_interval)
                elapsed += poll_interval
                last_heartbeat += poll_interval
                state = _state_for(user.id)
                serialized = _json.dumps(state, sort_keys=True, default=str)
                if serialized != last_serialized:
                    yield f"data: {serialized}\n\n"
                    last_serialized = serialized
                    last_heartbeat = 0.0
                elif last_heartbeat >= heartbeat_every:
                    yield ": heartbeat\n\n"
                    last_heartbeat = 0.0
                # Once the run reaches a terminal state and the client has
                # seen it, close the stream — no point in holding a
                # connection while idle.
                if (
                    state.get("status") in {"done", "failed", "idle"}
                    and elapsed > 2.0
                    and serialized == last_serialized
                ):
                    break

        return StreamingResponse(
            gen(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "X-Accel-Buffering": "no",
                "Connection": "keep-alive",
            },
        )

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

    @app.post("/keywords/suggested/accept")
    def accept_suggested_keyword(
        category: Annotated[str, Form()],
        value: Annotated[str, Form()],
        user: User = Depends(require_user),
        session: Session = Depends(get_session_dep),
    ) -> RedirectResponse:
        """One-click add of a CV-suggested keyword (Phase 10).

        Same insert as POST /keywords but redirects back to / with an
        anchor so the user lands on the keyword editor and sees the new
        chip immediately. Silently no-ops on the (user, category, value)
        unique conflict, same as the regular add route.
        """
        if category not in KEYWORD_CATEGORIES:
            raise HTTPException(status_code=400, detail=f"Invalid category: {category}")
        clean_value = value.strip()
        if not clean_value:
            raise HTTPException(status_code=400, detail="Empty value")
        session.add(UserKeyword(user_id=user.id, category=category, value=clean_value))
        try:
            session.flush()
        except IntegrityError:
            session.rollback()
        return RedirectResponse(url="/#suggested-keywords", status_code=303)

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
        force: Annotated[int, Query()] = 0,
        user: User = Depends(require_user),
        session: Session = Depends(get_session_dep),
    ) -> Response:
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
            .where(Preparation.status == "done")
            .order_by(desc(Preparation.created_at)).limit(1)
        ).scalar_one_or_none()
        # Skip the form (and the LLM call) when a done prep already exists
        # for this (user, signal). User can explicitly re-generate via the
        # report page's "Régénérer" link (which sends ?force=1).
        if latest_prep is not None and not force:
            return RedirectResponse(
                url=f"/preparations/{latest_prep.id}", status_code=303
            )
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

    # =========================================================================
    # CV management (Phase 8)
    # =========================================================================
    @app.post("/cv")
    async def upload_cv(
        cv_file: Annotated[UploadFile | None, File()] = None,
        cv_text: Annotated[str, Form()] = "",
        user: User = Depends(require_user),
    ) -> RedirectResponse:
        """Upload / replace the user's CV from the landing page.

        Always persists + generates the compact CVProfile (the landing
        page form has no "save" toggle — pressing the button is the
        explicit intent to save).
        """
        text_value = (cv_text or "").strip()
        filename: str | None = None
        if cv_file is not None and cv_file.filename:
            data = await cv_file.read()
            filename = cv_file.filename
            try:
                extracted = extract_text(filename, data)
            except CVExtractionError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            if extracted.strip():
                text_value = extracted.strip()

        if not text_value:
            raise HTTPException(
                status_code=400,
                detail="No CV provided. Upload a PDF or paste your CV text.",
            )

        try:
            cv_profile = await generate_cv_profile(text_value)
            profile_dict: dict[str, Any] | None = cv_profile.model_dump()
        except Exception as exc:
            logger.warning(
                "preparation.cv_profile_failed_using_raw error=%s", str(exc)[:200],
            )
            profile_dict = None

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
                profile_json=profile_dict,
            ))
        return RedirectResponse(url="/", status_code=303)


    @app.post("/cv/delete")
    def delete_cv(
        user: User = Depends(require_user),
        session: Session = Depends(get_session_dep),
    ) -> RedirectResponse:
        for cv in session.execute(
            select(UserCV).where(UserCV.user_id == user.id)
        ).scalars():
            session.delete(cv)
        return RedirectResponse(url="/", status_code=303)


    @app.get("/preparations", response_class=HTMLResponse)
    def list_preparations(
        request: Request,
        user: User = Depends(require_user),
        session: Session = Depends(get_session_dep),
    ) -> HTMLResponse:
        """All preps the user has generated. Newest first."""
        rows = list(session.execute(
            select(Preparation, Signal, RawItem)
            .join(Signal, Preparation.signal_id == Signal.id)
            .join(RawItem, Signal.raw_item_id == RawItem.id)
            .where(Preparation.user_id == user.id)
            .order_by(desc(Preparation.created_at))
            .limit(200)
        ).all())
        state = _state_for(user.id)
        return templates.TemplateResponse(
            request, "preparations_list.html.j2",
            {
                "rows": rows,
                "task_status": state["status"],
                "current_run_id": state["current_run_id"],
                "user": user,
            },
        )


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
