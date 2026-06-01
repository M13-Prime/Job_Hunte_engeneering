"""Starlette SessionMiddleware-backed login state + FastAPI dependencies."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import cast

from fastapi import HTTPException, Request
from sqlalchemy.orm import Session

from signal_tracker.storage.models import User

SESSION_USER_ID_KEY = "uid"


def login_user(request: Request, user: User) -> None:
    """Mark this session as authenticated for the given user."""
    request.session[SESSION_USER_ID_KEY] = user.id


def logout_user(request: Request) -> None:
    request.session.pop(SESSION_USER_ID_KEY, None)


def _resolve_user(request: Request, session: Session) -> User | None:
    uid = request.session.get(SESSION_USER_ID_KEY)
    if not isinstance(uid, int):
        return None
    user: User | None = session.get(User, uid)
    if user is None or not user.is_active:
        return None
    return user


def current_user_optional(
    request: Request,
) -> User | None:
    """Return the logged-in User or None. Caller supplies the DB session.

    The session lookup needs the DB; rather than threading another Depends
    here (FastAPI can't chain easily with our generator dependency), the
    callers grab the session themselves and call `_resolve_user`. This
    helper is mostly so templates can read `request.state.user`.
    """
    return getattr(request.state, "user", None)


def current_user(request: Request) -> User:
    """Hard auth dependency — 401 if no session."""
    user = getattr(request.state, "user", None)
    if user is None:
        # Used as a Depends() guard; route handlers that want a redirect
        # to /login instead of 401 should check current_user_optional first.
        raise HTTPException(status_code=401, detail="Authentication required")
    return cast(User, user)


def touch_last_login(session: Session, user: User) -> None:
    user.last_login_at = datetime.now(tz=UTC)


__all__ = [
    "SESSION_USER_ID_KEY",
    "_resolve_user",
    "current_user",
    "current_user_optional",
    "login_user",
    "logout_user",
    "touch_last_login",
]
