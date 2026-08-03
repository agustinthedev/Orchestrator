from __future__ import annotations

from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from orchestrator.database.models import Base


def _sqlite_path(url: str) -> Path | None:
    if not url.startswith("sqlite:///") or url == "sqlite:///:memory:":
        return None
    return Path(url.removeprefix("sqlite:///"))


class Database:
    def __init__(self, url: str) -> None:
        path = _sqlite_path(url)
        if path and str(path) != ":memory:":
            path.parent.mkdir(parents=True, exist_ok=True)
        self.engine: Engine = create_engine(url, future=True, connect_args={"check_same_thread": False})
        if url.startswith("sqlite"):
            event.listen(self.engine, "connect", self._configure_sqlite)
        self.session_factory = sessionmaker(self.engine, class_=Session, expire_on_commit=False)

    @staticmethod
    def _configure_sqlite(dbapi_connection: Any, _connection_record: Any) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()

    def create_all(self) -> None:
        Base.metadata.create_all(self.engine)

    def session(self) -> Session:
        return self.session_factory()

    def healthcheck(self) -> bool:
        with self.session() as session:
            session.execute(text("SELECT 1"))
        return True

    def dispose(self) -> None:
        self.engine.dispose()


def create_database(url: str) -> Database:
    database = Database(url)
    database.create_all()
    return database

