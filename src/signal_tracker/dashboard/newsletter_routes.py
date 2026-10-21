"""Phase 13 — newsletter routes.

- GET  /newsletter/preview          the current user's newsletter, as the email
- POST /newsletter/toggle           opt in / out (current user)
- GET  /newsletter/unsubscribe?t=   one-click opt-out from the email (public)
- GET  /admin/newsletter            recipients + send buttons (owner)
- POST /admin/newsletter/send       scope=me (test to myself) | all
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from signal_tracker.config import get_settings
from signal_tracker.notifier.email import (
    DryRunSender,
    EmailSenderProtocol,
    build_sender_from_settings,
)
from signal_tracker.notifier.newsletter import (
    NewsletterBuilder,
    read_unsubscribe_token,
    recipients,
    render_newsletter,
    send_newsletters,
)
from signal_tracker.storage import Database
from signal_tracker.storage.models import User

NEWSLETTER_PUBLIC_PATHS = {"/newsletter/unsubscribe"}

_FLASH = {
    "sent": "Newsletter envoyée.",
    "dry": "Aucun envoi d'email configuré (SMTP ou Resend) : rien n'est parti. Voir l'aperçu.",
    "no_from": "DIGEST_FROM_EMAIL est vide : configure l'adresse d'expédition dans .env.",
}


def _base_url(request: Request) -> str:
    configured = get_settings().dashboard_base_url
    return (configured or str(request.base_url)).rstrip("/")


def register_newsletter_routes(
    app: FastAPI,
    *,
    app_db: Database,
    templates: Jinja2Templates,
    require_user: Callable[[Request], User],
    require_admin: Callable[[Request], User],
    get_session_dep: Callable[[], Iterable[Session]],
    state_for: Callable[[int], dict[str, Any]],
    secret: str,
) -> None:
    def _builder(request: Request) -> NewsletterBuilder:
        settings = get_settings()
        return NewsletterBuilder(
            app_db, base_url=_base_url(request), secret=secret,
            period_days=settings.newsletter_period_days,
        )

    @app.get("/newsletter/preview", response_class=HTMLResponse)
    def preview(request: Request, user: User = Depends(require_user)) -> HTMLResponse:
        rendered = render_newsletter(_builder(request).build(user.id))
        return HTMLResponse(rendered.html)

    @app.post("/newsletter/toggle")
    def toggle(
        next: Annotated[str, Form()] = "/",
        user: User = Depends(require_user),
        session: Session = Depends(get_session_dep),
    ) -> RedirectResponse:
        row = session.get(User, user.id)
        if row is not None:
            row.newsletter_opt_in = not row.newsletter_opt_in
        target = next if next.startswith("/") and not next.startswith("//") else "/"
        return RedirectResponse(url=target, status_code=303)

    @app.get("/newsletter/unsubscribe", response_class=HTMLResponse)
    def unsubscribe(
        request: Request,
        t: Annotated[str, Query(max_length=512)] = "",
        session: Session = Depends(get_session_dep),
    ) -> HTMLResponse:
        uid = read_unsubscribe_token(t, secret) if t else None
        row = session.get(User, uid) if uid is not None else None
        if row is None:
            raise HTTPException(
                status_code=400, detail="Lien de désabonnement invalide ou expiré.",
            )
        row.newsletter_opt_in = False
        return templates.TemplateResponse(
            request, "newsletter_unsubscribed.html.j2",
            {"email": row.email, "user": None, "task_status": "idle", "current_run_id": None},
        )

    @app.get("/admin/newsletter", response_class=HTMLResponse)
    def admin_newsletter(
        request: Request,
        msg: Annotated[str, Query()] = "",
        user: User = Depends(require_admin),
    ) -> HTMLResponse:
        settings = get_settings()
        state = state_for(user.id)
        return templates.TemplateResponse(
            request, "admin_newsletter.html.j2",
            {
                "recipients": recipients(app_db),
                "weekday": settings.newsletter_weekday,
                "hour": settings.newsletter_hour,
                "minute": settings.newsletter_minute,
                "period_days": settings.newsletter_period_days,
                "from_email": settings.digest_from_email,
                "base_url": settings.dashboard_base_url,
                "flash": _FLASH.get(msg),
                "stats": None,
                "user": user,
                "task_status": state["status"],
                "current_run_id": state["current_run_id"],
            },
        )

    @app.post("/admin/newsletter/send")
    async def admin_send(
        request: Request,
        scope: Annotated[str, Form()] = "me",
        user: User = Depends(require_admin),
    ) -> RedirectResponse:
        settings = get_settings()
        if not settings.digest_from_email:
            return RedirectResponse(url="/admin/newsletter?msg=no_from", status_code=303)
        try:
            sender: EmailSenderProtocol = build_sender_from_settings(settings)
            dry = False
        except Exception:
            sender, dry = DryRunSender(), True
        await send_newsletters(
            app_db,
            sender=sender,
            from_email=settings.digest_from_email,
            base_url=_base_url(request),
            secret=secret,
            period_days=settings.newsletter_period_days,
            only_user_id=user.id if scope != "all" else None,
            skip_empty=scope == "all",
        )
        return RedirectResponse(
            url=f"/admin/newsletter?msg={'dry' if dry else 'sent'}", status_code=303,
        )


__all__ = ["NEWSLETTER_PUBLIC_PATHS", "register_newsletter_routes"]
