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
        self._apply_additive_migrations()

    def _apply_additive_migrations(self) -> None:
        inspector = inspect(self.engine)
        existing_tables = set(inspector.get_table_names())
        for table, column, sql_type in _ADDITIVE_COLUMNS:
            if table not in existing_tables:
                continue  # create_all already made it with the column
            cols = {c["name"] for c in inspector.get_columns(table)}
            if column not in cols:
                with self.engine.begin() as conn:
                    conn.execute(
                        text(f"ALTER TABLE {table} ADD COLUMN {column} {sql_type}")
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
