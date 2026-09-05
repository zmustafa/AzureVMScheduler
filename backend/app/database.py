from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from .config import get_settings


class Base(DeclarativeBase):
    pass


settings = get_settings()
DATABASE_URL = settings.resolved_database_url
#: SQLite is the local default; PostgreSQL is what the Azure deployment uses. Everything below that
#: differs between them keys off this flag rather than assuming one engine.
IS_SQLITE = DATABASE_URL.startswith("sqlite")

# SQLite needs a generous busy timeout so the scheduler's background writes wait for the
# single-writer lock instead of failing. Postgres has no such limitation.
_connect_args = {"timeout": 30} if IS_SQLITE else {}
engine = create_async_engine(
    DATABASE_URL,
    future=True,
    # Container Apps can idle a connection until the platform drops it; pre-ping reconnects
    # transparently instead of surfacing a dead-connection error on the next request.
    pool_pre_ping=not IS_SQLITE,
    connect_args=_connect_args,
)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


if IS_SQLITE:
    @event.listens_for(engine.sync_engine, "connect")
    def configure_sqlite(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.close()


async def get_db() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as session:
        yield session


async def ping_database() -> None:
    """Raise if the database is unreachable. Backs the container readiness probe."""
    async with engine.connect() as connection:
        await connection.execute(text("SELECT 1"))


async def initialize_database() -> None:
    from . import models  # noqa: F401

    async with engine.begin() as connection:
        await connection.run_sync(_upgrade_legacy_sqlite_schema)
        await connection.run_sync(Base.metadata.create_all)
        await connection.run_sync(_add_missing_columns)
        await connection.run_sync(_backfill_legacy_sqlite_data)
        await connection.run_sync(_backfill_notification_read_receipts)


def _add_missing_columns(connection) -> None:
    """On anything but SQLite, bring an existing database up to the current model shape.

    SQLite is handled by `_upgrade_legacy_sqlite_schema`, which also carries the one-off table
    rewrites that dialect needed.
    """
    if connection.dialect.name == "sqlite":
        return
    from .schema_ops import add_missing_model_columns

    add_missing_model_columns(connection)


def _upgrade_legacy_sqlite_schema(connection) -> None:
    if connection.dialect.name != "sqlite":
        return
    from .schema_ops import add_missing_columns, rename_start_attempts, set_aside_legacy_attempts

    # Rename first: create_all would otherwise build an empty vm_attempts and orphan the history.
    rename_start_attempts(connection)
    add_missing_columns(connection)
    set_aside_legacy_attempts(connection)


def _backfill_legacy_sqlite_data(connection) -> None:
    if connection.dialect.name != "sqlite":
        return
    from .schema_ops import backfill_hierarchy, copy_legacy_attempts

    copy_legacy_attempts(connection)
    backfill_hierarchy(connection)


def _backfill_notification_read_receipts(connection) -> None:
    """Convert the retired global read bit when startup, rather than Alembic, upgrades a database."""
    if connection.dialect.name == "postgresql":
        connection.exec_driver_sql(
            "INSERT INTO notification_event_reads (event_id, user_id, read_at) "
            "SELECT notification_events.id, users.id, CURRENT_TIMESTAMP "
            "FROM notification_events CROSS JOIN users WHERE notification_events.read = true "
            "ON CONFLICT (event_id, user_id) DO NOTHING"
        )
    else:
        connection.exec_driver_sql(
            "INSERT OR IGNORE INTO notification_event_reads (event_id, user_id, read_at) "
            "SELECT notification_events.id, users.id, CURRENT_TIMESTAMP "
            "FROM notification_events CROSS JOIN users WHERE notification_events.read = 1"
        )
    # Makes the conversion one-shot. A user created later must not inherit somebody else's old
    # acknowledgement merely because the application restarted.
    connection.exec_driver_sql("UPDATE notification_events SET read = false WHERE read = true")
