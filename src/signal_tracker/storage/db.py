"""Database engine / session helpers.

Accepts either a filesystem path (SQLite) or a full SQLAlchemy URL
(``postgresql+psycopg://...``) so the same code works in dev (SQLite file)
and in production (Postgres in Docker).
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import Engine, create_engine, inspect, text
from sqlalchemy.orm import Session, sessionmaker

from signal_tracker.storage.models import Base


def _is_database_url(value: str | Path) -> bool:
    text_value = str(value)
    return "://" in text_value and not text_value.startswith("/")


# Additive, idempotent column migrations. SQLAlchemy's create_all() only
# creates *missing tables*, never new columns on existing ones. For a
# single-maintainer app we keep a tiny hand-rolled list of "ensure column X
# exists on table Y" instead of pulling in Alembic.
_ADDITIVE_COLUMNS: tuple[tuple[str, str, str], ...] = (
    # (table, column, SQL type)
    ("signals", "search_run_id", "INTEGER"),
    ("user_cv", "profile_json", "JSON"),
    # Phase 8 — multi-tenant. user_id columns added retroactively to
    # tables that originally were single-user. Existing rows get NULL
    # and the bootstrap script claims them for the owner account.
    ("search_runs", "user_id", "INTEGER"),
    ("user_keywords", "user_id", "INTEGER"),
    ("watchlist", "user_id", "INTEGER"),
    ("user_cv", "user_id", "INTEGER"),
    ("preparations", "user_id", "INTEGER"),
    # Phase 9 — admin approval gate. New signups land unapproved until
    # an owner clicks "Approuver" on /admin/users. Existing rows are
    # back-filled to is_approved=1 by _backfill_user_approvals() below.
    ("users", "is_approved", "INTEGER NOT NULL DEFAULT 0"),
    ("users", "approved_at", "DATETIME"),
    ("users", "approved_by_id", "INTEGER"),
    # Phase 10 — country filter. The classifier extracts these on every
    # new signal; existing rows stay NULL and are treated as "unknown" by
    # the /results filter.
    ("signals", "hq_country", "VARCHAR(96)"),
    ("signals", "active_countries", "JSON"),
    # Phase 10 — record of what the dynamic source picker chose for the run.
    ("search_runs", "selected_sources", "JSON"),
    # Phase 11 — Jobs Agent semantic verdict. agent_processed_at also
    # serves as the "this row has been reviewed" marker so reruns don't
    # repeat the LLM work.
    ("job_offers", "agent_score", "FLOAT"),
    ("job_offers", "agent_fit_reasoning", "TEXT"),
    ("job_offers", "agent_killer_angle", "TEXT"),
    ("job_offers", "agent_why_now", "TEXT"),
    ("job_offers", "agent_processed_at", "DATETIME"),
)

# Same story for indexes: create_all() doesn't add new indexes to existing
# tables, so the (user_id, created_at) covering indexes used by the
# /preparations and sidebar /searches list pages need an explicit step.
_ADDITIVE_INDEXES: tuple[tuple[str, str, str], ...] = (
    # (index_name, table, columns)
    ("idx_preparations_user_created", "preparations", "user_id, created_at DESC"),
    ("idx_search_runs_user_created", "search_runs", "user_id, created_at DESC"),
    ("idx_signal_feedback_user_action", "signal_feedback", "user_id, action"),
)


class Database:
    """Thin wrapper around a SQLAlchemy engine + session factory."""

    def __init__(self, db_path_or_url: str | Path) -> None:
        if _is_database_url(db_path_or_url):
            url = str(db_path_or_url)
            self.db_path = None
            self.engine: Engine = create_engine(
                url,
                future=True,
                pool_pre_ping=True,  # mandatory for Postgres behind connection pools
            )
        else:
            self.db_path = Path(db_path_or_url)
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            self.engine = create_engine(
                f"sqlite:///{self.db_path}",
                future=True,
            )
        self._session_factory = sessionmaker(
            bind=self.engine, expire_on_commit=False, future=True
        )

    def create_all(self) -> None:
        Base.metadata.create_all(self.engine)
        added = self._apply_additive_migrations()
        self._apply_additive_indexes()
        # Only back-fill grandfathered approvals on the very deploy that
        # introduces the is_approved column — otherwise we'd auto-approve
        # every new signup at the next container restart.
        if ("users", "is_approved") in added:
            self._backfill_user_approvals()

    def _apply_additive_migrations(self) -> set[tuple[str, str]]:
        """Run pending ALTER TABLE ADD COLUMNs; return what was just added."""
        inspector = inspect(self.engine)
        existing_tables = set(inspector.get_table_names())
        just_added: set[tuple[str, str]] = set()
        for table, column, sql_type in _ADDITIVE_COLUMNS:
            if table not in existing_tables:
                continue  # create_all already made it with the column
            cols = {c["name"] for c in inspector.get_columns(table)}
            if column not in cols:
                with self.engine.begin() as conn:
                    conn.execute(
                        text(f"ALTER TABLE {table} ADD COLUMN {column} {sql_type}")
                    )
                just_added.add((table, column))
        return just_added

    def _backfill_user_approvals(self) -> None:
        """One-shot data migration for the Phase 9 approval gate.

        Without this, deploying the gate would lock every existing user
        out (is_approved defaults to 0 on the new column). We:
        - Mark every pre-existing user as approved so the upgrade is
          transparent for them.
        - Promote the earliest-created user to owner if no owner exists,
          so the instance always has at least one admin who can approve
          future signups.

        Called from create_all() only when the is_approved column was
        added in the same run, so it's a one-shot — subsequent restarts
        skip it and let unapproved post-gate signups stay unapproved.
        """
        with self.engine.begin() as conn:
            conn.execute(text(
                "UPDATE users SET is_approved = 1 WHERE is_approved = 0"
            ))
            owner_row = conn.execute(
                text("SELECT id FROM users WHERE is_owner = 1 LIMIT 1")
            ).first()
            if owner_row is None:
                earliest = conn.execute(
                    text("SELECT id FROM users ORDER BY created_at ASC LIMIT 1")
                ).first()
                if earliest is not None:
                    conn.execute(
                        text("UPDATE users SET is_owner = 1 WHERE id = :uid"),
                        {"uid": earliest[0]},
                    )

    def _apply_additive_indexes(self) -> None:
        inspector = inspect(self.engine)
        existing_tables = set(inspector.get_table_names())
        for index_name, table, columns in _ADDITIVE_INDEXES:
            if table not in existing_tables:
                continue
            existing = {idx["name"] for idx in inspector.get_indexes(table)}
            if index_name in existing:
                continue
            with self.engine.begin() as conn:
                conn.execute(
                    text(
                        f"CREATE INDEX IF NOT EXISTS {index_name} "
                        f"ON {table} ({columns})"
                    )
                )

    @contextmanager
    def session(self) -> Iterator[Session]:
        session = self._session_factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()


def init_db(db_path_or_url: str | Path) -> Database:
    """Create the tables (and SQLite file, if applicable), return a handle."""
    db = Database(db_path_or_url)
    db.create_all()
    return db


@contextmanager
def get_session(db_path_or_url: str | Path) -> Iterator[Session]:
    """Convenience context manager for one-off scripts."""
    db = init_db(db_path_or_url)
    with db.session() as session:
        yield session
