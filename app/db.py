from collections.abc import AsyncIterator, Sequence
from typing import Any

from sqlalchemy import event
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.config import get_settings


class Base(DeclarativeBase):
    pass


_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def _enable_sqlite_foreign_keys(dbapi_connection, _record) -> None:
    """SQLite не проверяет внешние ключи, пока не попросить: включаем на каждом соединении.

    Иначе тесты на SQLite пропускали бы то, что PostgreSQL отвергает (висячие ссылки, каскады).
    """
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        _engine = create_async_engine(get_settings().database_url, pool_pre_ping=True)
        if _engine.dialect.name == "sqlite":
            event.listen(_engine.sync_engine, "connect", _enable_sqlite_foreign_keys)
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    global _sessionmaker
    if _sessionmaker is None:
        _sessionmaker = async_sessionmaker(get_engine(), expire_on_commit=False)
    return _sessionmaker


def reset_db_state() -> None:
    """Используется тестами для подмены DATABASE_URL."""
    global _engine, _sessionmaker
    _engine = None
    _sessionmaker = None


async def get_session() -> AsyncIterator[AsyncSession]:
    async with get_sessionmaker()() as session:
        yield session


def dialect_insert(session: AsyncSession, table: Any):
    """INSERT с поддержкой ON CONFLICT под диалект сессии.

    Синтаксис ON CONFLICT у PostgreSQL и SQLite совпадает, но в SQLAlchemy это разные конструкции
    (postgresql.insert и sqlite.insert), общей нет. Другие диалекты проект не поддерживает.
    """
    name = session.bind.dialect.name
    if name == "postgresql":
        return postgresql.insert(table)
    if name == "sqlite":
        return sqlite.insert(table)
    raise NotImplementedError(f"ON CONFLICT не реализован для диалекта {name}")


def insert_ignore(
    session: AsyncSession,
    table: Any,
    values: dict[str, Any],
    conflict_columns: Sequence[str] | None = None,
):
    """INSERT ... ON CONFLICT DO NOTHING. rowcount результата 0 означает, что строка уже была.

    conflict_columns ограничивает конфликт конкретным уникальным ключом; без них подавляется любой
    конфликт уникальности строки.
    """
    stmt = dialect_insert(session, table).values(**values)
    return stmt.on_conflict_do_nothing(
        index_elements=list(conflict_columns) if conflict_columns else None
    )
