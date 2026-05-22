"""Database engine / session helpers.

Accepts either a filesystem path (SQLite) or a full SQLAlchemy URL
(``postgresql+psycopg://...``) so the same code works in dev (SQLite file)
and in production (Postgres in Docker).
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from signal_tracker.storage.models import Base


def _is_database_url(value: str | Path) -> bool:
    text = str(value)
    return "://" in text and not text.startswith("/")


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
