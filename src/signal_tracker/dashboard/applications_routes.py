"""Phase 12 dashboard routes — Jobright-style features.

- /jobs                      open offers for the user's companies, AI score
- POST /jobs/refresh         background: scrape watchlist + signal companies,
                             auto-score new offers (capped)
- GET  /jobs/refresh/status  JSON
- GET  /jobs/{id}            offer detail + match breakdown + actions
- POST /jobs/{id}/score      AI match score on demand
- POST /jobs/{id}/track      add the offer to the application tracker
- POST /jobs/{id}/tailor     tailored CV + cover letter (creates/updates the
                             tracked application)
- POST /signals/{id}/track   spontaneous application from a signal
- GET  /applications         kanban
- POST /applications         manual entry
- POST /applications/{id}/status | /update | /delete | /tailor
- GET  /applications/{id}/documents       tailored CV + letter (print → PDF)
- GET  /applications/{id}/cv.md | /letter.txt   downloads

Registered from build_app() so it shares the app's auth, DB and templates.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable
from datetime import datetime, timedelta
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import ColumnElement, and_, desc, or_, select
from sqlalchemy.orm import Session

from signal_tracker.applications.render import cv_to_markdown, safe_filename, section_labels
from signal_tracker.applications.schemas import TailoredCV
from signal_tracker.applications.tailor import TailoringError, generate_tailored_documents
from signal_tracker.applications.tracker import (
    STATUS_LABELS,
    apply_status,
    is_followup_due,
    utcnow,
)
from signal_tracker.config import get_settings, load_user_profile
from signal_tracker.jobs.filters import (
    REASON_LABELS,
    profile_title_terms,
    rejection_reason,
    user_default_filter,
)
from signal_tracker.jobs.ghost import ghost_flags
from signal_tracker.jobs.matching import (
    MatchingError,
    companies_for_user,
    load_cv_text,
    refresh_and_score_for_user,
    score_job_for_user,
)
from signal_tracker.jobs.rubric import CRITERIA
from signal_tracker.storage import Database
from signal_tracker.storage.models import (
    APPLICATION_STATUSES,
    Application,
    JobAgentScore,
    JobOffer,
    SearchRun,
    Signal,
    User,
    UserCV,
    UserKeyword,
)
from signal_tracker.utils.geo import COUNTRY_CHOICES, normalize_country
from signal_tracker.utils.logging import get_logger
from signal_tracker.utils.normalize import normalize_company_name

logger = get_logger(__name__)

# Messages shown via ?msg=<key> after a redirect (no user text in URLs).
FLASH_MESSAGES: dict[str, str] = {
    "no_cv": "Ajoute ton CV (page Recherche) pour utiliser le score IA et le CV adapté.",
    "score_failed": "Le scoring IA a échoué. Réessaie dans un instant.",
    "tailor_failed": "La génération du CV adapté a échoué. Réessaie dans un instant.",
    "tracked": "Offre ajoutée à ton suivi de candidatures.",
    "refresh_started": "Actualisation lancée : scraping des pages carrières puis score IA.",
    "refresh_running": "Une actualisation est déjà en cours.",
    "prefs_saved": (
        "Préférences enregistrées : pays et expérience s'appliquent aussi "
        "au digest et à la newsletter."
    ),
    "no_companies": "Ajoute des entreprises à ta watchlist ou lance une recherche d'abord.",
}


def _parse_date(value: str) -> datetime | None:
    value = (value or "").strip()
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Date invalide (AAAA-MM-JJ).") from exc


def _to_int(value: str | None) -> int | None:
    try:
        n = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return n if 0 <= n <= 60 else None


def _profile_terms_and_years(session: Session, user_id: int) -> tuple[list[str], int | None]:
    """Distinctive words of the user's keywords + CV headline/roles, and CV years."""
    phrases = [
        v for (v,) in session.execute(
            select(UserKeyword.value).where(UserKeyword.user_id == user_id)
        ).all() if v
    ]
    cv = session.execute(
        select(UserCV).where(UserCV.user_id == user_id).limit(1)
    ).scalar_one_or_none()
    years: int | None = None
    profile = (cv.profile_json or {}) if cv is not None else {}
    if isinstance(profile, dict):
        if isinstance(profile.get("headline"), str):
            phrases.append(profile["headline"])
        for role in profile.get("top_roles") or []:
            if isinstance(role, dict) and isinstance(role.get("title"), str):
                phrases.append(role["title"])
        raw_years = profile.get("years_experience")
        if isinstance(raw_years, int) and 0 <= raw_years <= 60:
            years = raw_years
    return profile_title_terms(phrases), years


def register_application_routes(
    app: FastAPI,
    *,
    app_db: Database,
    templates: Jinja2Templates,
    require_user: Callable[[Request], User],
    get_session_dep: Callable[[], Iterable[Session]],
    state_for: Callable[[int], dict[str, Any]],
) -> None:
    refresh_states: dict[int, dict[str, Any]] = {}
    bg_tasks: set[asyncio.Task[None]] = set()

    def _ctx(user: User, **extra: Any) -> dict[str, Any]:
        state = state_for(user.id)
        return {
            "user": user,
            "task_status": state["status"],
            "current_run_id": state["current_run_id"],
            **extra,
        }

    def _flash(msg: str | None) -> str | None:
        return FLASH_MESSAGES.get(msg or "")

    def _has_cv(session: Session, user_id: int) -> bool:
        return session.execute(
            select(UserCV.id).where(UserCV.user_id == user_id).limit(1)
        ).scalar_one_or_none() is not None

    def _own_application(session: Session, app_id: int, user_id: int) -> Application:
        row = session.get(Application, app_id)
        if row is None or row.user_id != user_id:
            raise HTTPException(status_code=404, detail="Candidature introuvable")
        return row

    def _application_for_job(
        session: Session, user_id: int, job: JobOffer,
    ) -> Application:
        existing = session.execute(
            select(Application).where(and_(
                Application.user_id == user_id,
                Application.job_offer_id == job.id,
            )).limit(1)
        ).scalar_one_or_none()
        if existing is not None:
            return existing
        row = Application(
            user_id=user_id, job_offer_id=job.id,
            company_name=job.company_name, title=job.title, url=job.url,
            status="saved", created_at=utcnow(), updated_at=utcnow(),
        )
        session.add(row)
        session.flush()
        return row

    # =====================================================================
    # Offers
    # =====================================================================
    @app.get("/jobs", response_class=HTMLResponse)
    def list_jobs(
        request: Request,
        msg: Annotated[str, Query()] = "",
        min_score: Annotated[int, Query(ge=0, le=100)] = 0,
        f: Annotated[int, Query()] = 0,
        country: Annotated[str, Query(max_length=96)] = "",
        unknown: Annotated[int, Query()] = 0,
        kw: Annotated[list[str] | None, Query()] = None,
        extra: Annotated[str, Query(max_length=200)] = "",
        mode: Annotated[str, Query()] = "all",
        exp_min: Annotated[str, Query(max_length=3)] = "",
        exp_max: Annotated[str, Query(max_length=3)] = "",
        rel: Annotated[int, Query()] = 0,
        hide_ghost: Annotated[int, Query()] = 0,
        user: User = Depends(require_user),
        session: Session = Depends(get_session_dep),
    ) -> HTMLResponse:
        """Open offers relevant to THIS user.

        An offer is listed iff the user has an AI score for it, or it
        belongs to one of the user's companies (watchlist + companies of
        their recent signals) with a non-zero keyword score. On top of
        that: country, keyword (AND / OR) and experience filters.
        """
        me = session.get(User, user.id)
        flt = user_default_filter(me)
        if f:  # the filter form was submitted: its values replace the saved ones
            flt.country = "" if country in ("", "all") else country
            flt.include_unknown = bool(unknown)
            flt.exp_min = _to_int(exp_min)
            flt.exp_max = _to_int(exp_max)
        terms, cv_years = _profile_terms_and_years(session, user.id)
        exp_from_cv = False
        if not f and flt.exp_min is None and flt.exp_max is None and cv_years is not None:
            # Nothing saved yet: start from the experience read in the CV.
            flt.exp_min, flt.exp_max = max(0, cv_years - 3), cv_years + 3
            exp_from_cv = True
        # Checkboxes send nothing when unticked: only trust them on a submitted form.
        title_gate = bool(rel) if f else True
        if title_gate:
            flt.title_terms = terms
        flt.keyword_mode = "any" if mode == "any" else "all"
        flt.keywords = [k.strip() for k in (kw or []) if k.strip()] + [
            t.strip() for t in extra.split(",") if t.strip()
        ]

        companies = companies_for_user(app_db, user.id)
        normalized = [normalize_company_name(c) for c in companies]
        score_join = and_(
            JobAgentScore.job_offer_id == JobOffer.id,
            JobAgentScore.user_id == user.id,
        )
        conditions: list[ColumnElement[bool]] = [JobAgentScore.id.is_not(None)]
        if normalized:
            conditions.append(and_(
                JobOffer.company_normalized.in_(normalized),
                JobOffer.relevance_score > 0,
            ))
        stmt = (
            select(JobOffer, JobAgentScore)
            .outerjoin(JobAgentScore, score_join)
            .where(and_(JobOffer.is_open.is_(True), or_(*conditions)))
            .order_by(
                desc(JobAgentScore.agent_score),
                desc(JobOffer.relevance_score),
                desc(JobOffer.collected_at),
            )
            .limit(600)
        )
        all_rows = list(session.execute(stmt).all())
        if min_score:
            all_rows = [
                r for r in all_rows
                if r[1] is not None and r[1].agent_score >= min_score
            ]
        rows = []
        hidden: dict[str, int] = {}
        for job, score_row in all_rows:
            reason = rejection_reason(
                title=job.title, location=job.location, department=job.department,
                description=job.description, flt=flt,
                ai_score=score_row.agent_score if score_row is not None else None,
            )
            if reason is None:
                rows.append((job, score_row))
            else:
                hidden[reason] = hidden.get(reason, 0) + 1
        ghost = ghost_flags(session, [j for j, _ in rows])
        if hide_ghost:
            kept = [(j, sc) for j, sc in rows if not ghost.get(j.id)]
            if len(kept) < len(rows):
                hidden["ghost"] = len(rows) - len(kept)
            rows = kept
        keyword_choices = sorted({
            v for (v,) in session.execute(
                select(UserKeyword.value).where(UserKeyword.user_id == user.id)
            ).all() if v
        }, key=str.lower)
        tracked = {
            job_id: status
            for job_id, status in session.execute(
                select(Application.job_offer_id, Application.status).where(and_(
                    Application.user_id == user.id,
                    Application.job_offer_id.is_not(None),
                ))
            ).all()
        }
        return templates.TemplateResponse(
            request, "jobs_list.html.j2",
            _ctx(
                user,
                rows=rows,
                tracked=tracked,
                status_labels=STATUS_LABELS,
                companies_count=len(companies),
                has_cv=_has_cv(session, user.id),
                refresh=refresh_states.get(user.id, {"status": "idle"}),
                flash=_flash(msg),
                min_score=min_score,
                flt=flt,
                keyword_choices=keyword_choices,
                extra_terms=extra,
                country_choices=COUNTRY_CHOICES,
                hidden=[(REASON_LABELS[k], n) for k, n in hidden.items()],
                hidden_total=sum(hidden.values()),
                saved_country=normalize_country(me.country if me else None),
                title_gate=title_gate,
                ghost=ghost,
                hide_ghost=bool(hide_ghost),
                title_terms=terms,
                exp_from_cv=exp_from_cv,
                cv_years=cv_years,
            ),
        )

    @app.post("/jobs/prefs")
    def save_job_prefs(
        country: Annotated[str, Form()] = "France",
        exp_min: Annotated[str, Form()] = "",
        exp_max: Annotated[str, Form()] = "",
        user: User = Depends(require_user),
        session: Session = Depends(get_session_dep),
    ) -> RedirectResponse:
        row = session.get(User, user.id)
        if row is not None:
            row.country = normalize_country(country)
            lo, hi = _to_int(exp_min), _to_int(exp_max)
            if lo is not None and hi is not None and hi < lo:
                lo, hi = hi, lo
            row.exp_min, row.exp_max = lo, hi
        return RedirectResponse(url="/jobs?msg=prefs_saved", status_code=303)

    @app.post("/jobs/refresh")
    async def refresh_jobs(user: User = Depends(require_user)) -> RedirectResponse:
        current = refresh_states.get(user.id, {})
        if current.get("status") == "running":
            return RedirectResponse(url="/jobs?msg=refresh_running", status_code=303)
        if not companies_for_user(app_db, user.id):
            return RedirectResponse(url="/jobs?msg=no_companies", status_code=303)
        refresh_states[user.id] = {"status": "running", "started_at": utcnow().isoformat()}
        uid = user.id

        async def _run() -> None:
            try:
                res = await refresh_and_score_for_user(app_db, load_user_profile(), uid)
                refresh_states[uid] = {
                    "status": "done",
                    "companies": res.companies,
                    "companies_with_ats": res.companies_with_ats,
                    "jobs_new": res.jobs_new,
                    "jobs_scored": res.jobs_scored,
                    "errors": len(res.errors),
                }
            except Exception as exc:
                logger.warning("jobs.refresh_failed user=%s error=%s", uid, str(exc)[:200])
                refresh_states[uid] = {"status": "failed", "error": str(exc)[:300]}

        task = asyncio.create_task(_run())
        bg_tasks.add(task)
        task.add_done_callback(bg_tasks.discard)
        return RedirectResponse(url="/jobs?msg=refresh_started", status_code=303)

    @app.get("/jobs/refresh/status")
    def refresh_status(user: User = Depends(require_user)) -> JSONResponse:
        return JSONResponse(refresh_states.get(user.id, {"status": "idle"}))

    def _job_or_404(session: Session, job_id: int) -> JobOffer:
        job = session.get(JobOffer, job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Offre introuvable")
        return job

    @app.get("/jobs/{job_id}", response_class=HTMLResponse)
    def job_detail(
        request: Request,
        job_id: int,
        msg: Annotated[str, Query()] = "",
        user: User = Depends(require_user),
        session: Session = Depends(get_session_dep),
    ) -> HTMLResponse:
        job = _job_or_404(session, job_id)
        score = session.execute(
            select(JobAgentScore).where(and_(
                JobAgentScore.user_id == user.id,
                JobAgentScore.job_offer_id == job_id,
            ))
        ).scalar_one_or_none()
        application = session.execute(
            select(Application).where(and_(
                Application.user_id == user.id,
                Application.job_offer_id == job_id,
            )).limit(1)
        ).scalar_one_or_none()
        return templates.TemplateResponse(
            request, "job_detail.html.j2",
            _ctx(
                user, job=job, score=score, application=application,
                status_labels=STATUS_LABELS, has_cv=_has_cv(session, user.id),
                flash=_flash(msg), criteria_defs=CRITERIA,
                ghost=ghost_flags(session, [job]).get(job.id, []),
            ),
        )

    @app.post("/jobs/{job_id}/score")
    async def score_job(
        job_id: int,
        next: Annotated[str, Form()] = "",
        user: User = Depends(require_user),
    ) -> RedirectResponse:
        back = next if next.startswith("/") and not next.startswith("//") else f"/jobs/{job_id}"
        path, _, fragment = back.partition("#")

        def _with_msg(key: str) -> str:
            sep = "&" if "?" in path else "?"
            return f"{path}{sep}msg={key}" + (f"#{fragment}" if fragment else "")

        with app_db.session() as session:
            _job_or_404(session, job_id)
            if not _has_cv(session, user.id):
                return RedirectResponse(url=_with_msg("no_cv"), status_code=303)
        try:
            await score_job_for_user(
                app_db, user_id=user.id, job_offer_id=job_id, profile=load_user_profile(),
            )
        except MatchingError:
            return RedirectResponse(url=_with_msg("score_failed"), status_code=303)
        return RedirectResponse(url=back, status_code=303)

    @app.post("/jobs/{job_id}/track")
    def track_job(
        job_id: int,
        user: User = Depends(require_user),
        session: Session = Depends(get_session_dep),
    ) -> RedirectResponse:
        job = _job_or_404(session, job_id)
        _application_for_job(session, user.id, job)
        return RedirectResponse(url=f"/jobs/{job_id}?msg=tracked", status_code=303)

    async def _tailor_into(app_id: int, user_id: int) -> bool:
        """Generate the documents for one tracked application. True on success."""
        with app_db.session() as session:
            row = _own_application(session, app_id, user_id)
            company, title, url = row.company_name, row.title, row.url
            description: str | None = None
            location: str | None = None
            signal_summary: str | None = None
            if row.job_offer_id is not None:
                job = session.get(JobOffer, row.job_offer_id)
                if job is not None:
                    description, location = job.description, job.location
                    signal = session.execute(
                        select(Signal)
                        .where(Signal.company_normalized == job.company_normalized)
                        .order_by(desc(Signal.total_score)).limit(1)
                    ).scalar_one_or_none()
                    signal_summary = signal.summary_fr if signal else None
            elif row.signal_id is not None:
                signal = session.get(Signal, row.signal_id)
                if signal is not None:
                    signal_summary = signal.summary_fr
                    description = (
                        "Candidature spontanée — pas d'offre publiée. "
                        f"Contexte : {signal.summary_fr} "
                        f"Angle suggéré : {signal.suggested_angle or '—'}"
                    )
            if not description and row.notes:
                description = row.notes
        try:
            docs = await generate_tailored_documents(
                company=company, title=title, location=location,
                description=description or f"Poste : {title}. Lien : {url or '—'}",
                cv_text=load_cv_text(app_db, user_id),
                signal_summary=signal_summary,
            )
        except TailoringError as exc:
            logger.warning("applications.tailor_failed app=%s error=%s", app_id, str(exc)[:200])
            return False
        with app_db.session() as session:
            row = _own_application(session, app_id, user_id)
            row.tailored_cv = {
                **docs.cv.model_dump(),
                "_language": docs.language,
                "_changes": docs.changes,
                "_gaps": docs.gaps,
            }
            row.cover_letter = docs.cover_letter
            row.tailored_at = utcnow()
            row.updated_at = utcnow()
        return True

    @app.post("/jobs/{job_id}/tailor")
    async def tailor_job(
        job_id: int,
        user: User = Depends(require_user),
    ) -> RedirectResponse:
        with app_db.session() as session:
            job = _job_or_404(session, job_id)
            if not _has_cv(session, user.id):
                return RedirectResponse(url=f"/jobs/{job_id}?msg=no_cv", status_code=303)
            app_id = _application_for_job(session, user.id, job).id
        if not await _tailor_into(app_id, user.id):
            return RedirectResponse(url=f"/jobs/{job_id}?msg=tailor_failed", status_code=303)
        return RedirectResponse(url=f"/applications/{app_id}/documents", status_code=303)

    @app.post("/signals/{signal_id}/track")
    def track_signal(
        signal_id: int,
        user: User = Depends(require_user),
        session: Session = Depends(get_session_dep),
    ) -> RedirectResponse:
        signal = session.get(Signal, signal_id)
        run = session.get(SearchRun, signal.search_run_id) if (
            signal is not None and signal.search_run_id is not None
        ) else None
        if signal is None or run is None or run.user_id != user.id:
            raise HTTPException(status_code=404, detail="Signal not found")
        existing = session.execute(
            select(Application.id).where(and_(
                Application.user_id == user.id, Application.signal_id == signal_id,
            )).limit(1)
        ).scalar_one_or_none()
        if existing is None:
            session.add(Application(
                user_id=user.id, signal_id=signal_id,
                company_name=signal.company_name or "(entreprise)",
                title="Candidature spontanée",
                status="saved", notes=signal.suggested_angle,
                created_at=utcnow(), updated_at=utcnow(),
            ))
        return RedirectResponse(url="/applications", status_code=303)

    # =====================================================================
    # Application tracker
    # =====================================================================
    @app.get("/applications", response_class=HTMLResponse)
    def list_applications(
        request: Request,
        msg: Annotated[str, Query()] = "",
        user: User = Depends(require_user),
        session: Session = Depends(get_session_dep),
    ) -> HTMLResponse:
        rows = list(session.execute(
            select(Application)
            .where(Application.user_id == user.id)
            .order_by(desc(Application.updated_at))
        ).scalars())
        columns: dict[str, list[Application]] = {s: [] for s in APPLICATION_STATUSES}
        for row in rows:
            columns.setdefault(row.status, []).append(row)
        now = utcnow()
        due = {r.id for r in rows if is_followup_due(r, now=now)}
        return templates.TemplateResponse(
            request, "applications.html.j2",
            _ctx(
                user, columns=columns, statuses=APPLICATION_STATUSES,
                status_labels=STATUS_LABELS, due=due, total=len(rows),
                flash=_flash(msg),
            ),
        )

    @app.post("/applications")
    def add_application(
        company_name: Annotated[str, Form()],
        title: Annotated[str, Form()],
        url: Annotated[str, Form()] = "",
        status: Annotated[str, Form()] = "saved",
        notes: Annotated[str, Form()] = "",
        user: User = Depends(require_user),
        session: Session = Depends(get_session_dep),
    ) -> RedirectResponse:
        company_name, title = company_name.strip(), title.strip()
        if not company_name or not title:
            raise HTTPException(status_code=400, detail="Entreprise et poste requis.")
        if status not in APPLICATION_STATUSES:
            raise HTTPException(status_code=400, detail="Statut invalide.")
        clean_url = url.strip()
        if clean_url and not clean_url.startswith(("http://", "https://")):
            raise HTTPException(status_code=400, detail="Lien invalide (http/https).")
        row = Application(
            user_id=user.id, company_name=company_name[:512], title=title[:512],
            url=clean_url or None, notes=notes.strip() or None,
            status="saved", created_at=utcnow(), updated_at=utcnow(),
        )
        apply_status(row, status, followup_days=get_settings().application_followup_days)
        session.add(row)
        return RedirectResponse(url="/applications", status_code=303)

    @app.post("/applications/{app_id}/status")
    def set_application_status(
        app_id: int,
        status: Annotated[str, Form()],
        user: User = Depends(require_user),
        session: Session = Depends(get_session_dep),
    ) -> RedirectResponse:
        row = _own_application(session, app_id, user.id)
        try:
            apply_status(row, status, followup_days=get_settings().application_followup_days)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Statut invalide.") from exc
        return RedirectResponse(url="/applications", status_code=303)

    @app.post("/applications/{app_id}/update")
    def update_application(
        app_id: int,
        notes: Annotated[str, Form()] = "",
        next_followup_at: Annotated[str, Form()] = "",
        followup_done: Annotated[str, Form()] = "",
        user: User = Depends(require_user),
        session: Session = Depends(get_session_dep),
    ) -> RedirectResponse:
        row = _own_application(session, app_id, user.id)
        row.notes = notes.strip() or None
        if followup_done:
            # "Relancé" — push the next reminder out by the default delay.
            row.next_followup_at = utcnow() + timedelta(
                days=get_settings().application_followup_days
            )
        else:
            row.next_followup_at = _parse_date(next_followup_at)
        row.updated_at = utcnow()
        return RedirectResponse(url="/applications", status_code=303)

    @app.post("/applications/{app_id}/delete")
    def delete_application(
        app_id: int,
        user: User = Depends(require_user),
        session: Session = Depends(get_session_dep),
    ) -> RedirectResponse:
        session.delete(_own_application(session, app_id, user.id))
        return RedirectResponse(url="/applications", status_code=303)

    @app.post("/applications/{app_id}/tailor")
    async def tailor_application(
        app_id: int,
        user: User = Depends(require_user),
    ) -> RedirectResponse:
        with app_db.session() as session:
            _own_application(session, app_id, user.id)
            if not _has_cv(session, user.id):
                return RedirectResponse(url="/applications?msg=no_cv", status_code=303)
        if not await _tailor_into(app_id, user.id):
            return RedirectResponse(url="/applications?msg=tailor_failed", status_code=303)
        return RedirectResponse(url=f"/applications/{app_id}/documents", status_code=303)

    def _docs(row: Application) -> tuple[TailoredCV, str, list[str], list[str]]:
        if not row.tailored_cv:
            raise HTTPException(status_code=404, detail="Pas encore de CV adapté.")
        data = dict(row.tailored_cv)
        language = str(data.pop("_language", "fr"))
        changes = [str(c) for c in data.pop("_changes", []) or []]
        gaps = [str(g) for g in data.pop("_gaps", []) or []]
        return TailoredCV.model_validate(data), language, changes, gaps

    @app.get("/applications/{app_id}/documents", response_class=HTMLResponse)
    def application_documents(
        request: Request,
        app_id: int,
        user: User = Depends(require_user),
        session: Session = Depends(get_session_dep),
    ) -> HTMLResponse:
        row = _own_application(session, app_id, user.id)
        cv, language, changes, gaps = _docs(row)
        return templates.TemplateResponse(
            request, "application_docs.html.j2",
            _ctx(
                user, application=row, cv=cv, language=language,
                labels=section_labels(language), changes=changes, gaps=gaps,
                letter=row.cover_letter or "", status_labels=STATUS_LABELS,
            ),
        )

    @app.get("/applications/{app_id}/cv.md")
    def download_cv(
        app_id: int,
        user: User = Depends(require_user),
        session: Session = Depends(get_session_dep),
    ) -> Response:
        row = _own_application(session, app_id, user.id)
        cv, language, _changes, _gaps = _docs(row)
        name = safe_filename("cv", row.company_name, row.title)
        return Response(
            content=cv_to_markdown(cv, language),
            media_type="text/markdown; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{name}.md"'},
        )

    @app.get("/applications/{app_id}/letter.txt")
    def download_letter(
        app_id: int,
        user: User = Depends(require_user),
        session: Session = Depends(get_session_dep),
    ) -> Response:
        row = _own_application(session, app_id, user.id)
        if not row.cover_letter:
            raise HTTPException(status_code=404, detail="Pas encore de lettre.")
        name = safe_filename("lettre", row.company_name, row.title)
        return Response(
            content=row.cover_letter,
            media_type="text/plain; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{name}.txt"'},
        )


__all__ = ["FLASH_MESSAGES", "register_application_routes"]
