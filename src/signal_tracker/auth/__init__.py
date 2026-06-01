"""Email + password authentication (Phase 8 multi-tenant)."""

from signal_tracker.auth.password import hash_password, verify_password
from signal_tracker.auth.session import (
    SESSION_USER_ID_KEY,
    current_user,
    current_user_optional,
    login_user,
    logout_user,
)

__all__ = [
    "SESSION_USER_ID_KEY",
    "current_user",
    "current_user_optional",
    "hash_password",
    "login_user",
    "logout_user",
    "verify_password",
]
