"""Тесты идут против настоящего PostgreSQL (docker compose up -d db), в отдельной базе payments_test.
Схема создаётся миграциями Alembic — так заодно проверяются и они."""

import asyncio
import os

TEST_URL = os.environ.get("TEST_DATABASE_URL", "postgresql+asyncpg://payments:payments@localhost:5432/payments_test")
os.environ["DATABASE_URL"] = TEST_URL
os.environ["WEBHOOK_SECRET"] = "test-secret"

import asyncpg  # noqa: E402
import pytest  # noqa: E402
from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402
from sqlalchemy.pool import NullPool  # noqa: E402

import app.db  # noqa: E402
import app.tasks  # noqa: E402
from app.main import app as fastapi_app  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(__file__))


def alembic_cfg() -> Config:
    cfg = Config(os.path.join(ROOT, "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(ROOT, "alembic"))
    return cfg


async def _recreate_db() -> None:
    admin_dsn = TEST_URL.replace("+asyncpg", "").rsplit("/", 1)[0] + "/postgres"
    name = TEST_URL.rsplit("/", 1)[1]
    con = await asyncpg.connect(admin_dsn)
    try:
        await con.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        await con.execute(f'CREATE DATABASE "{name}"')
    finally:
        await con.close()


@pytest.fixture(scope="session", autouse=True)
def database():
    asyncio.run(_recreate_db())
    command.upgrade(alembic_cfg(), "head")
    yield


# NullPool: каждый тест в своём event loop, держать соединения между ними нельзя.
engine = create_async_engine(TEST_URL, poolclass=NullPool)
app.db.SessionLocal = async_sessionmaker(engine, expire_on_commit=False)
app.tasks.SessionLocal = app.db.SessionLocal


@pytest.fixture(autouse=True)
async def clean():
    async with engine.begin() as con:
        await con.execute(text("TRUNCATE notifications, webhook_events, refunds, payments CASCADE"))
    yield


@pytest.fixture(autouse=True)
def queued(monkeypatch):
    """Очередь Celery без Redis: запоминаем, что API поставило в очередь. Сами задачи тесты зовут напрямую."""
    ids: list[str] = []
    monkeypatch.setattr(app.tasks.deliver, "delay", lambda nid: ids.append(nid))
    return ids


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=fastapi_app), base_url="http://test") as c:
        yield c


async def count(table: str) -> int:
    async with engine.connect() as con:
        return (await con.execute(text(f"SELECT count(*) FROM {table}"))).scalar_one()
