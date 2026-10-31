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
# Hosted Postgres (Neon, ...) hands out postgresql:// URLs; the installed driver is psycopg 3.
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


def _add_missing_columns(existing: set[str]) -> None:
    """create_all never alters existing tables; add columns introduced since a database was created (all nullable).
    All tables' columns are read in one reflection call: per-table lookups against a remote database (Neon) took
    longer than a hosting platform's startup limit."""
    from sqlalchemy import inspect, text
    names = [t.name for t in Base.metadata.sorted_tables if t.name in existing]
    columns = {key[-1]: cols for key, cols in inspect(engine).get_multi_columns(filter_names=names).items()} if names else {}
    for table in Base.metadata.sorted_tables:
        if table.name not in columns:
            continue
        have = {c["name"] for c in columns[table.name]}
        for col in table.columns:
            if col.name not in have and not col.primary_key:
                ddl = col.type.compile(dialect=engine.dialect)
                with engine.begin() as conn:
                    conn.execute(text(f'ALTER TABLE {table.name} ADD COLUMN "{col.name}" {ddl}'))


def init_db() -> None:
    from sqlalchemy import inspect

    from .. import models  # noqa: F401  (register tables)
    existing = set(inspect(engine).get_table_names())
    missing = [t for t in Base.metadata.sorted_tables if t.name not in existing]
    if missing:
        Base.metadata.create_all(engine, tables=missing, checkfirst=False)
    _add_missing_columns(existing)
