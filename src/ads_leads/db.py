"""Подключение к БД (SQLite по умолчанию)."""

import sqlite3
from collections.abc import Iterator
from datetime import datetime
from functools import lru_cache
from pathlib import Path

from sqlalchemy import Engine, create_engine, event, inspect, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from ads_leads.config import get_settings


class Base(DeclarativeBase):
    pass


@lru_cache
def get_engine(url: str | None = None) -> Engine:
    url = url or get_settings().db_url
    if not url.startswith("sqlite"):
        return create_engine(url)
    engine = create_engine(url, connect_args={"check_same_thread": False, "timeout": 30})

    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_conn, _):
        # WAL: чтение не блокирует запись — несколько человек работают одновременно.
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    return engine


def init_db(engine: Engine | None = None) -> None:
    from ads_leads import models  # noqa: F401  регистрирует таблицы

    engine = engine or get_engine()
    Base.metadata.create_all(engine)
    _add_missing_columns(engine)
    with engine.begin() as conn:  # задачи, прерванные перезапуском сервера
        conn.execute(
            text("UPDATE jobs SET status='error', message='Прервано перезапуском сервера' "
                 "WHERE status IN ('queued','running')")
        )


def _add_missing_columns(engine: Engine) -> None:
    """Простая миграция для SQLite: добавляет новые nullable-колонки/колонки с default в старые таблицы."""
    insp = inspect(engine)
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            existing = {c["name"] for c in insp.get_columns(table.name)}
            for col in table.columns:
                if col.name in existing:
                    continue
                ddl = f'ALTER TABLE "{table.name}" ADD COLUMN "{col.name}" {col.type.compile(engine.dialect)}'
                if col.server_default is not None:
                    ddl += f" DEFAULT '{col.server_default.arg}'"
                conn.execute(text(ddl))


def backup_db(keep: int = 30) -> Path:
    """Согласованная копия SQLite (с учётом WAL) в data/backups; старые сверх keep удаляются."""
    settings = get_settings()
    src_path = Path(settings.db_url.removeprefix("sqlite:///"))
    target_dir = settings.data_dir / "backups"
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"ads_leads_{datetime.now():%Y-%m-%d_%H%M%S}.db"
    with sqlite3.connect(src_path) as src, sqlite3.connect(target) as dst:
        src.backup(dst)
    for old in sorted(target_dir.glob("ads_leads_*.db"))[:-keep]:
        old.unlink()
    return target


def get_session() -> Iterator[Session]:
    """Зависимость FastAPI: сессия на запрос."""
    factory = sessionmaker(bind=get_engine(), expire_on_commit=False)
    with factory() as session:
        yield session
