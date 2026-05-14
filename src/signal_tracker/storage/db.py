"""Database engine / session helpers."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from signal_tracker.storage.models import Base


class Database:
    """Thin wrapper around a SQLAlchemy engine + session factory."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.engine: Engine = create_engine(
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


def init_db(db_path: str | Path) -> Database:
    """Create the DB file and tables if missing, return a Database handle."""
    db = Database(db_path)
    db.create_all()
    return db


@contextmanager
def get_session(db_path: str | Path) -> Iterator[Session]:
    """Convenience context manager for one-off scripts."""
    db = init_db(db_path)
    with db.session() as session:
        yield session
