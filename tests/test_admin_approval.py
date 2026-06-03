"""Tests for the Phase 9 admin-approval gate."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from signal_tracker.auth import hash_password
from signal_tracker.dashboard.app import build_app
from signal_tracker.storage import Database, init_db
from signal_tracker.storage.models import User


@pytest.fixture()
def db(tmp_path: Path) -> Database:
    """Fresh DB with one approved owner."""
    db = init_db(tmp_path / "admin.db")
    with db.session() as s:
        s.add(User(
            email="admin@example.com",
            password_hash=hash_password("password"),
            is_active=True,
            is_owner=True,
            is_approved=True,
        ))
    return db


@pytest.fixture()
def admin_client(db: Database) -> TestClient:
    c = TestClient(build_app(db=db))
    resp = c.post(
        "/login",
        data={"email": "admin@example.com", "password": "password"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    return c


# ---------------------------------------------------------------------------
# Signup / login gating
# ---------------------------------------------------------------------------


def test_first_signup_becomes_admin(tmp_path: Path) -> None:
    """On a fresh DB, the very first signup is auto-promoted and approved."""
    fresh_db = init_db(tmp_path / "fresh.db")
    c = TestClient(build_app(db=fresh_db))
    resp = c.post("/signup", data={
        "email": "first@example.com",
        "password": "supersecret",
        "password_confirm": "supersecret",
    }, follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/"  # straight into the app, not /pending
    with fresh_db.session() as s:
        u = s.execute(select(User).where(User.email == "first@example.com")).scalar_one()
        assert u.is_owner is True
        assert u.is_approved is True


def test_second_signup_lands_in_pending(db: Database) -> None:
    """With an admin already in the DB, the next signup is gated."""
    c = TestClient(build_app(db=db))
    resp = c.post("/signup", data={
        "email": "newbie@example.com",
        "password": "supersecret",
        "password_confirm": "supersecret",
    }, follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/pending"
    with db.session() as s:
        u = s.execute(select(User).where(User.email == "newbie@example.com")).scalar_one()
        assert u.is_owner is False
        assert u.is_approved is False


def test_unapproved_login_redirects_to_pending(db: Database) -> None:
    with db.session() as s:
        s.add(User(
            email="waiting@example.com",
            password_hash=hash_password("password"),
            is_active=True,
            is_approved=False,
        ))
    c = TestClient(build_app(db=db))
    resp = c.post(
        "/login",
        data={"email": "waiting@example.com", "password": "password"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/pending"


def test_unapproved_user_cannot_reach_protected_pages(db: Database) -> None:
    """Even with a valid session cookie, /results 303s to /pending."""
    with db.session() as s:
        s.add(User(
            email="waiting@example.com",
            password_hash=hash_password("password"),
            is_active=True,
            is_approved=False,
        ))
    c = TestClient(build_app(db=db))
    c.post("/login", data={"email": "waiting@example.com", "password": "password"})
    resp = c.get("/results", follow_redirects=False, headers={"accept": "text/html"})
    assert resp.status_code == 303
    assert resp.headers["location"] == "/pending"


# ---------------------------------------------------------------------------
# /admin/users surface
# ---------------------------------------------------------------------------


def test_admin_users_page_lists_accounts(admin_client: TestClient, db: Database) -> None:
    with db.session() as s:
        s.add(User(
            email="pending@example.com",
            password_hash=hash_password("password"),
            is_active=True,
            is_approved=False,
        ))
    resp = admin_client.get("/admin/users")
    assert resp.status_code == 200
    body = resp.text
    assert "admin@example.com" in body
    assert "pending@example.com" in body
    assert "1 en attente" in body


def test_non_admin_cannot_open_admin_page(db: Database) -> None:
    with db.session() as s:
        s.add(User(
            email="user@example.com",
            password_hash=hash_password("password"),
            is_active=True,
            is_approved=True,
            is_owner=False,
        ))
    c = TestClient(build_app(db=db))
    c.post("/login", data={"email": "user@example.com", "password": "password"})
    resp = c.get("/admin/users", follow_redirects=False)
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Admin actions
# ---------------------------------------------------------------------------


def test_approve_grants_access(admin_client: TestClient, db: Database) -> None:
    """Approving a user lets them log in and reach /results."""
    with db.session() as s:
        s.add(User(
            email="pending@example.com",
            password_hash=hash_password("password"),
            is_active=True,
            is_approved=False,
        ))
        s.flush()
        target_id = s.execute(
            select(User.id).where(User.email == "pending@example.com")
        ).scalar_one()

    resp = admin_client.post(f"/admin/users/{target_id}/approve", follow_redirects=False)
    assert resp.status_code == 303

    with db.session() as s:
        u = s.get(User, target_id)
        assert u is not None
        assert u.is_approved is True
        assert u.approved_at is not None
        assert u.approved_by_id is not None

    # Now they can log in and reach a protected page.
    them = TestClient(build_app(db=db))
    them.post("/login", data={"email": "pending@example.com", "password": "password"})
    resp = them.get("/results", follow_redirects=False, headers={"accept": "text/html"})
    assert resp.status_code == 200


def test_revoke_blocks_existing_user(admin_client: TestClient, db: Database) -> None:
    """Revoking an approved non-admin user kicks them back to /pending."""
    with db.session() as s:
        s.add(User(
            email="bob@example.com",
            password_hash=hash_password("password"),
            is_active=True,
            is_approved=True,
        ))
        s.flush()
        target_id = s.execute(
            select(User.id).where(User.email == "bob@example.com")
        ).scalar_one()

    resp = admin_client.post(f"/admin/users/{target_id}/revoke", follow_redirects=False)
    assert resp.status_code == 303

    bob = TestClient(build_app(db=db))
    login = bob.post(
        "/login",
        data={"email": "bob@example.com", "password": "password"},
        follow_redirects=False,
    )
    assert login.headers["location"] == "/pending"


def test_cannot_revoke_an_admin(admin_client: TestClient, db: Database) -> None:
    """Revoking is blocked for admins so we don't lock the instance out."""
    with db.session() as s:
        s.add(User(
            email="other_admin@example.com",
            password_hash=hash_password("password"),
            is_active=True,
            is_owner=True,
            is_approved=True,
        ))
        s.flush()
        target_id = s.execute(
            select(User.id).where(User.email == "other_admin@example.com")
        ).scalar_one()
    resp = admin_client.post(f"/admin/users/{target_id}/revoke", follow_redirects=False)
    assert resp.status_code == 400


def test_cannot_demote_the_last_admin(admin_client: TestClient, db: Database) -> None:
    """The instance must always have at least one admin."""
    with db.session() as s:
        admin_id = s.execute(
            select(User.id).where(User.email == "admin@example.com")
        ).scalar_one()
    # Set up a second admin to make demotion legal, then remove them and
    # try again to confirm the last-admin guard kicks in.
    with db.session() as s:
        s.add(User(
            email="second_admin@example.com",
            password_hash=hash_password("password"),
            is_active=True,
            is_owner=True,
            is_approved=True,
        ))
        s.flush()
        second_id = s.execute(
            select(User.id).where(User.email == "second_admin@example.com")
        ).scalar_one()
    # Demote the second admin — should work.
    resp = admin_client.post(f"/admin/users/{second_id}/demote", follow_redirects=False)
    assert resp.status_code == 303
    # Now only admin@ is owner. Try to demote them (themselves) — refused
    # because it's the same user (self-edit guard).
    resp = admin_client.post(f"/admin/users/{admin_id}/demote", follow_redirects=False)
    assert resp.status_code == 400


def test_promote_unapproved_is_refused(admin_client: TestClient, db: Database) -> None:
    with db.session() as s:
        s.add(User(
            email="pending@example.com",
            password_hash=hash_password("password"),
            is_active=True,
            is_approved=False,
        ))
        s.flush()
        target_id = s.execute(
            select(User.id).where(User.email == "pending@example.com")
        ).scalar_one()
    resp = admin_client.post(f"/admin/users/{target_id}/promote", follow_redirects=False)
    assert resp.status_code == 400


def test_admin_cannot_modify_their_own_account(admin_client: TestClient, db: Database) -> None:
    with db.session() as s:
        admin_id = s.execute(
            select(User.id).where(User.email == "admin@example.com")
        ).scalar_one()
    for action in ("approve", "revoke", "promote", "demote"):
        resp = admin_client.post(f"/admin/users/{admin_id}/{action}", follow_redirects=False)
        assert resp.status_code == 400, action


# ---------------------------------------------------------------------------
# Migration back-fill
# ---------------------------------------------------------------------------


def test_backfill_grandfathers_existing_users(tmp_path: Path) -> None:
    """When the is_approved column is freshly added, existing rows are kept
    valid (is_approved=1) and the earliest user is promoted to owner."""
    import sqlite3
    p = tmp_path / "legacy.db"
    # Pre-build a "legacy" DB with the v8 schema (no is_approved column).
    conn = sqlite3.connect(p)
    conn.executescript(
        """
        CREATE TABLE users (
          id INTEGER PRIMARY KEY,
          email VARCHAR(320) UNIQUE NOT NULL,
          password_hash VARCHAR(255) NOT NULL,
          is_active INTEGER NOT NULL DEFAULT 1,
          is_owner INTEGER NOT NULL DEFAULT 0,
          created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
          last_login_at DATETIME
        );
        INSERT INTO users (email, password_hash, created_at)
          VALUES ('first@example.com', 'x', '2025-01-01 00:00:00'),
                 ('second@example.com', 'x', '2025-06-01 00:00:00');
        """
    )
    conn.commit()
    conn.close()

    # Now run create_all — the additive migration should add the column,
    # back-fill everyone to approved, and promote 'first@' to owner.
    init_db(p)
    conn = sqlite3.connect(p)
    rows = list(conn.execute(
        "SELECT email, is_approved, is_owner FROM users ORDER BY created_at"
    ))
    conn.close()
    assert rows[0] == ("first@example.com", 1, 1)  # earliest -> owner
    assert rows[1] == ("second@example.com", 1, 0)
