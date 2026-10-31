from __future__ import annotations

import uuid
from contextlib import contextmanager
from datetime import datetime, timezone

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from .config import get_settings


def new_id() -> str:
    return str(uuid.uuid4())


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


_url = get_settings().database_url
# Hosted Postgres (Neon, Render, ...) hands out postgresql:// URLs; the installed driver is psycopg 3.
for _prefix in ("postgres://", "postgresql://"):
    if _url.startswith(_prefix):
        _url = "postgresql+psycopg://" + _url[len(_prefix):]
_is_sqlite = _url.startswith("sqlite")
engine = create_engine(
    _url,
    # prepare_threshold=None: no server-side prepared statements, which transaction-mode poolers (Neon -pooler) can't share.
    connect_args={"check_same_thread": False, "timeout": 30} if _is_sqlite else {"prepare_threshold": None},
    pool_pre_ping=True,
    # Neon closes idle connections when the compute scales to zero; recycle before that happens.
    pool_recycle=300,
)

if _is_sqlite:
    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(conn, _record):
        cur = conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def session_scope():
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _add_missing_columns() -> None:
    """create_all never alters existing tables; add columns introduced since a database was created (all nullable)."""
    from sqlalchemy import inspect, text
    insp = inspect(engine)
    for table in Base.metadata.sorted_tables:
        if not insp.has_table(table.name):
            continue
        have = {c["name"] for c in insp.get_columns(table.name)}
        for col in table.columns:
            if col.name not in have and not col.primary_key:
                ddl = col.type.compile(dialect=engine.dialect)
                with engine.begin() as conn:
                    conn.execute(text(f'ALTER TABLE {table.name} ADD COLUMN "{col.name}" {ddl}'))


def init_db() -> None:
    from .. import models  # noqa: F401  (register tables)
    Base.metadata.create_all(engine)
    _add_missing_columns()
