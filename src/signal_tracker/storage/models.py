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
    """Companies the user wants to prioritize in scoring (Phase 5 dashboard)."""

    __tablename__ = "watchlist"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    company_name: Mapped[str] = mapped_column(String(512))
    normalized_name: Mapped[str] = mapped_column(String(512), unique=True, index=True)
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
