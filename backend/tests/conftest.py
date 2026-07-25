from __future__ import annotations

import os

import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app import models  # noqa: F401  (registers the mappings)
from app.database import Base

#: In-memory SQLite keeps the suite fast. Point TEST_DATABASE_URL at a scratch PostgreSQL to run
#: the identical suite against the engine the Azure deployment uses:
#:   $env:TEST_DATABASE_URL="postgresql+asyncpg://user:pw@127.0.0.1:5432/db"
TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", "sqlite+aiosqlite:///:memory:")


@pytest_asyncio.fixture
async def session():
    engine = create_async_engine(TEST_DATABASE_URL)
    async with engine.begin() as connection:
        # A real database is reused across tests, so each one starts from a clean schema rather
        # than inheriting the previous test's rows. In-memory SQLite is already fresh.
        if engine.dialect.name != "sqlite":
            await connection.run_sync(Base.metadata.drop_all)
        await connection.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as db:
        yield db
    await engine.dispose()
