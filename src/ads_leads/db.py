"""Подключение к БД (SQLite по умолчанию)."""

from collections.abc import Iterator
from functools import lru_cache

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from ads_leads.config import get_settings


class Base(DeclarativeBase):
    pass


@lru_cache
def get_engine(url: str | None = None) -> Engine:
    url = url or get_settings().db_url
    connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
    return create_engine(url, connect_args=connect_args)


def init_db(engine: Engine | None = None) -> None:
    from ads_leads import models  # noqa: F401  регистрирует таблицы

    Base.metadata.create_all(engine or get_engine())


def get_session() -> Iterator[Session]:
    """Зависимость FastAPI: сессия на запрос."""
    factory = sessionmaker(bind=get_engine(), expire_on_commit=False)
    with factory() as session:
        yield session
