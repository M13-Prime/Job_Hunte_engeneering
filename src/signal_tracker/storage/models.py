"""SQLAlchemy ORM models for the Signal Tracker DB."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    """Base class for all ORM models."""


class RawItem(Base):
    """Everything collected, before classification."""

    __tablename__ = "raw_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[str] = mapped_column(String(128), index=True)
    url: Mapped[str] = mapped_column(String(2048))
    title: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    content: Mapped[str | None] = mapped_column(Text, nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    collected_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    classified: Mapped[bool] = mapped_column(default=False, nullable=False)

    signals: Mapped[list[Signal]] = relationship(back_populates="raw_item")


class Company(Base):
    """Normalized company reference."""

    __tablename__ = "companies"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(512))
    normalized_name: Mapped[str] = mapped_column(String(512), unique=True, index=True)
    sector: Mapped[str | None] = mapped_column(String(256), nullable=True)
    size_estimate: Mapped[str | None] = mapped_column(String(64), nullable=True)
    url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )

    persons: Mapped[list[Person]] = relationship(back_populates="company")
    signals: Mapped[list[Signal]] = relationship(back_populates="company")


class Person(Base):
    """Executives / key persons detected in signals."""

    __tablename__ = "persons"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(256), index=True)
    role: Mapped[str | None] = mapped_column(String(256), nullable=True)
    company_id: Mapped[int | None] = mapped_column(
        ForeignKey("companies.id"), nullable=True
    )
    detected_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    source_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)

    company: Mapped[Company | None] = relationship(back_populates="persons")


class Signal(Base):
    """A classified, relevant signal."""

    __tablename__ = "signals"
    __table_args__ = (
        UniqueConstraint("dedup_key", name="uq_signals_dedup_key"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    raw_item_id: Mapped[int] = mapped_column(ForeignKey("raw_items.id"))
    company_id: Mapped[int | None] = mapped_column(
        ForeignKey("companies.id"), nullable=True
    )

    signal_type: Mapped[str] = mapped_column(String(64), index=True)
    company_name: Mapped[str] = mapped_column(String(512))
    company_normalized: Mapped[str] = mapped_column(String(512), index=True)
    # Phase 10: geographic footprint extracted by the classifier.
    # hq_country is the canonical French name ("France", "Belgique", ...).
    # active_countries is a JSON array always including hq_country if known.
    hq_country: Mapped[str | None] = mapped_column(String(96), nullable=True, index=True)
    active_countries: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)

    key_persons: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON, nullable=True)

    relevance_score: Mapped[float] = mapped_column(Float)
    urgency_score: Mapped[float] = mapped_column(Float)
    fit_with_profile_score: Mapped[float] = mapped_column(Float)
    total_score: Mapped[float] = mapped_column(Float, index=True)

    summary_fr: Mapped[str] = mapped_column(Text)
    suggested_angle: Mapped[str | None] = mapped_column(Text, nullable=True)
    recommended_action: Mapped[str] = mapped_column(String(64))
    target_contact: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)

    user_feedback: Mapped[str | None] = mapped_column(String(32), nullable=True)
    dedup_key: Mapped[str] = mapped_column(String(128), index=True)
    # The search run that first surfaced this signal (Phase 6 dashboard).
    search_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("search_runs.id"), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )

    raw_item: Mapped[RawItem] = relationship(back_populates="signals")
    company: Mapped[Company | None] = relationship(back_populates="signals")


class DigestSent(Base):
    """History of digests already sent to the user."""

    __tablename__ = "digests_sent"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    sent_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    recipient: Mapped[str] = mapped_column(String(256))
    signal_ids: Mapped[list[int]] = mapped_column(JSON)


class WatchlistEntry(Base):
    """Companies the user wants to prioritize in scoring (Phase 5 dashboard).

    Scoped per-user (Phase 8 multi-tenant). The legacy unique constraint on
    normalized_name was global; now it's per (user_id, normalized_name).
    """

    __tablename__ = "watchlist"
    __table_args__ = (
        UniqueConstraint(
            "user_id", "normalized_name", name="uq_watchlist_user_name"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id"), index=True, nullable=True
    )
    company_name: Mapped[str] = mapped_column(String(512))
    normalized_name: Mapped[str] = mapped_column(String(512), index=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    added_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )


class JobOffer(Base):
    """A single open position scraped from an ATS public API (Module 2)."""

    __tablename__ = "job_offers"
    __table_args__ = (
        UniqueConstraint("dedup_key", name="uq_job_offers_dedup_key"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    company_normalized: Mapped[str] = mapped_column(String(512), index=True)
    company_name: Mapped[str] = mapped_column(String(512))
    ats: Mapped[str] = mapped_column(String(32))
    ats_company_slug: Mapped[str] = mapped_column(String(256))
    external_id: Mapped[str] = mapped_column(String(128))

    title: Mapped[str] = mapped_column(String(512))
    url: Mapped[str] = mapped_column(String(2048))
    location: Mapped[str | None] = mapped_column(String(256), nullable=True)
    department: Mapped[str | None] = mapped_column(String(256), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    posted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    relevance_score: Mapped[float] = mapped_column(Float, default=0.0, index=True)
    matched_roles: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    is_open: Mapped[bool] = mapped_column(default=True, nullable=False, index=True)

    collected_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    dedup_key: Mapped[str] = mapped_column(String(256), index=True)


class SearchRun(Base):
    """One launch of the collect+classify pipeline from the dashboard.

    Lets the UI keep a history of past searches, each with the keywords used
    and the metrics it produced, and tag the signals it surfaced so new ones
    are distinguishable from older runs. Scoped per-user (Phase 8).
    """

    __tablename__ = "search_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id"), index=True, nullable=True
    )
    label: Mapped[str] = mapped_column(String(256))
    status: Mapped[str] = mapped_column(String(32), default="running", index=True)
    keywords: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    metrics: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False, index=True
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class UserKeyword(Base):
    """User-curated keywords injected into the classifier prompt at runtime.

    Scoped per-user (Phase 8). Each user maintains their own keyword set;
    uniqueness is (user_id, category, value).

    Categories:
    - field        : sectors / domains the user hunts (e.g. "AI for legal")
    - job_title    : roles to surface (e.g. "Sales Engineer")
    - other        : misc terms (e.g. "AI Act", "RAG", "vector DB")
    """

    __tablename__ = "user_keywords"
    __table_args__ = (
        UniqueConstraint(
            "user_id", "category", "value",
            name="uq_user_keywords_user_cat_value",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id"), index=True, nullable=True
    )
    category: Mapped[str] = mapped_column(String(32), index=True)
    value: Mapped[str] = mapped_column(String(256))
    added_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )


class UserCV(Base):
    """The current user CV — one row per user (Phase 8 multi-tenant).

    Stored as plain text so we can paste it into LLM prompts. We keep the
    original filename for display. To replace the CV the user uploads again
    and the dashboard deletes their previous row.

    `profile_json` is the distilled, structured CVProfile computed once
    when the CV is saved (Phase 7+). Future preparations send the compact
    profile to the LLM instead of the full text — ~80% token savings.
    """

    __tablename__ = "user_cv"
    __table_args__ = (
        UniqueConstraint("user_id", name="uq_user_cv_user"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id"), index=True, nullable=True
    )
    filename: Mapped[str | None] = mapped_column(String(256), nullable=True)
    text: Mapped[str] = mapped_column(Text)
    char_count: Mapped[int] = mapped_column(Integer, default=0)
    profile_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )


class Preparation(Base):
    """Generated preparation report for a (Signal, CV) pair (Phase 7+).

    Scoped per-user (Phase 8).
    """

    __tablename__ = "preparations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id"), index=True, nullable=True
    )
    signal_id: Mapped[int] = mapped_column(
        ForeignKey("signals.id"), index=True, nullable=False
    )
    status: Mapped[str] = mapped_column(String(16), default="done", index=True)
    report: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    cv_excerpt: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False, index=True
    )


# ----------------------------------------------------------------------------
# Phase 8: multi-tenant auth
# ----------------------------------------------------------------------------

class User(Base):
    """Email + bcrypt password account. Owns search runs, keywords, CV, etc.

    Approval gate: new signups land with ``is_approved=False`` and cannot
    log in until an owner approves them via the /admin/users page. The
    first user to sign up is auto-promoted to owner + approved so the
    instance is never left without an admin.
    """

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(default=True, nullable=False)
    is_owner: Mapped[bool] = mapped_column(default=False, nullable=False)
    is_approved: Mapped[bool] = mapped_column(default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    approved_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id"), nullable=True
    )


class SignalFeedback(Base):
    """Per-user feedback on a (shared) signal.

    Phase 8 replaces the global Signal.user_feedback column. One row per
    (user, signal). Older single-user feedback is migrated into this table
    against the owner account.
    """

    __tablename__ = "signal_feedback"
    __table_args__ = (
        UniqueConstraint("user_id", "signal_id", name="uq_signal_feedback_user_signal"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id"), index=True, nullable=False
    )
    signal_id: Mapped[int] = mapped_column(
        ForeignKey("signals.id"), index=True, nullable=False
    )
    action: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
