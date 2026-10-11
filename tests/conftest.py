"""Общие фикстуры: база на каждый тест.

По умолчанию SQLite в tmp_path (схема через create_all). С переменной TEST_DATABASE_URL
(postgresql+asyncpg://...) тесты идут на PostgreSQL: схема накатывается миграциями (alembic upgrade
head) один раз на сессию, между тестами таблицы очищаются TRUNCATE ... RESTART IDENTITY, потому что
сценарии опираются на идентификаторы с единицы. Тесты с маркером pg нужны только на PostgreSQL:
SQLite не держит блокировки между операторами и гонку не воспроизводит.
"""

import asyncio
import os
from pathlib import Path

import pytest
from sqlalchemy import text

ROOT = Path(__file__).resolve().parent.parent
TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", "")

os.environ.update(
    {
        "DATABASE_URL": TEST_DATABASE_URL or "sqlite+aiosqlite:///./tests/placeholder.db",
        "SESSION_SECRET": "test-session-secret",
        "FAKE_CALLS": "true",
        "WORKER_ENABLED": "false",
        "FAKE_CALL_SECONDS": "0.4",
        "FAKE_ANSWER_RATE": "1.0",
        "TWILIO_VALIDATE_SIGNATURE": "false",
        "OPENAI_API_KEY": "",
        # тесты не должны ходить во внешние API, даже если в .env есть реальные ключи
        "VOICE_ENGINE": "openai",
        "YANDEX_API_KEY": "",
        "YANDEX_FOLDER_ID": "",
        "POST_CALL_ANALYSIS": "false",
    }
)

from app import models  # noqa: E402, F401
from app.config import get_settings  # noqa: E402
from app.db import Base, get_engine, reset_db_state  # noqa: E402


def pytest_collection_modifyitems(config, items) -> None:
    if TEST_DATABASE_URL:
        return
    skip = pytest.mark.skip(reason="нужен PostgreSQL: задайте TEST_DATABASE_URL")
    for item in items:
        if "pg" in item.keywords:
            item.add_marker(skip)


@pytest.fixture(scope="session")
def event_loop_policy():
    return asyncio.DefaultEventLoopPolicy()


@pytest.fixture(scope="session")
def pg_schema() -> None:
    """Схема PostgreSQL миграциями, как в проде. На SQLite ничего не делает."""
    if not TEST_DATABASE_URL:
        return
    from alembic.config import Config

    from alembic import command

    get_settings.cache_clear()
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    command.upgrade(config, "head")


async def _truncate_all(conn) -> None:
    tables = ", ".join(t.name for t in reversed(Base.metadata.sorted_tables))
    await conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))


@pytest.fixture(autouse=True)
async def fresh_db(tmp_path, pg_schema):
    if TEST_DATABASE_URL:
        os.environ["DATABASE_URL"] = TEST_DATABASE_URL
    else:
        db_file = tmp_path / "test.db"
        os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{db_file.as_posix()}"
    get_settings.cache_clear()
    reset_db_state()
    engine = get_engine()
    async with engine.begin() as conn:
        if TEST_DATABASE_URL:
            await _truncate_all(conn)
        else:
            await conn.run_sync(Base.metadata.create_all)
    yield
    await engine.dispose()
    reset_db_state()
