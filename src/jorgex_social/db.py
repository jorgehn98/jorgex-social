from datetime import UTC, datetime

from sqlalchemy import Engine, String, create_engine, event
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from sqlalchemy.types import TypeDecorator

from jorgex_social.config import Settings


class Base(DeclarativeBase):
    pass


class UTCDateTime(TypeDecorator[datetime]):
    """Stores aware datetimes as ISO 8601 text in UTC, so values sort and read the same anywhere."""

    impl = String(32)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect) -> str | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("naive datetime; pass an aware datetime")
        return value.astimezone(UTC).isoformat(timespec="microseconds")

    def process_result_value(self, value: str | None, dialect) -> datetime | None:
        return None if value is None else datetime.fromisoformat(value)


def utcnow() -> datetime:
    return datetime.now(UTC)


def make_engine(settings: Settings, *, write: bool = True) -> Engine:
    """`write=False` gives an engine whose transactions only read: a plain deferred BEGIN, which
    in WAL mode never waits for the write lock. Use it for endpoints that must answer while the
    worker writes, such as the public media URL Instagram downloads from."""
    settings.database_path.parent.mkdir(parents=True, exist_ok=True)
    # The API server and the worker are separate processes sharing one file.
    # hide_parameters: bound values (which may one day hold third-party data) stay out of errors.
    engine = create_engine(
        settings.database_url, connect_args={"timeout": 30}, hide_parameters=True
    )

    @event.listens_for(engine, "connect")
    def _on_connect(dbapi_connection, _record) -> None:
        # Take transaction control away from pysqlite, which only begins on the first write and
        # so leaves reads outside the transaction.
        dbapi_connection.isolation_level = None
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    @event.listens_for(engine, "begin")
    def _on_begin(connection) -> None:
        # IMMEDIATE takes the write lock up front: a read-then-write (pick a due post, mark it
        # publishing) cannot interleave with the other process. Keep transactions short and never
        # hold one across a call to Meta.
        connection.exec_driver_sql("BEGIN IMMEDIATE" if write else "BEGIN")

    return engine


def make_sessionmaker(engine: Engine) -> sessionmaker:
    return sessionmaker(engine, expire_on_commit=False)
