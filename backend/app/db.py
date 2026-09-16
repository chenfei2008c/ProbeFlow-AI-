from contextlib import contextmanager
import os
from threading import RLock

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

from app.config import ROOT


class Database:
    def __init__(self, settings):
        self.settings = settings
        settings.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(settings.data_dir, 0o700)
        self.path = settings.data_dir / "probeflow.sqlite3"
        self.lock = RLock()
        self.engine = create_engine(
            f"sqlite:///{self.path}", connect_args={"check_same_thread": False, "timeout": 20}
        )

        @event.listens_for(self.engine, "connect")
        def sqlite_setup(connection, _):
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA busy_timeout=20000")

    def initialize(self):
        from alembic import command
        from alembic.config import Config

        config = Config(str(ROOT / "alembic.ini"))
        with self.engine.begin() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "head")
        os.chmod(self.path, 0o600)

    @contextmanager
    def transaction(self):
        # Single process, SQLite writer serialization extends to file changes/deletion.
        with self.lock, Session(self.engine, expire_on_commit=False) as session:
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            try:
                yield session
                session.commit()
            except Exception:
                session.rollback()
                raise

    @contextmanager
    def read(self):
        with Session(self.engine, expire_on_commit=False) as session:
            yield session
