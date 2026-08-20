from __future__ import annotations

import sqlite3
import time
from atexit import register
from contextlib import contextmanager
from importlib import resources
from pathlib import Path
from threading import Lock
from typing import Any, Iterator, Mapping, Sequence, Union, overload

LATEST_SCHEMA_VERSION = 10
DatabaseTarget = Union[Path, str]
_POSTGRES_POOLS: dict[str, Any] = {}
_POSTGRES_POOLS_LOCK = Lock()


class DatabaseRow(Mapping[str, Any]):
    def __init__(self, names: Sequence[str], values: Sequence[Any]) -> None:
        self._names = tuple(names)
        self._values = tuple(values)
        self._mapping = dict(zip(self._names, self._values))

    @overload
    def __getitem__(self, key: str) -> Any: ...  # noqa: E704

    @overload
    def __getitem__(self, key: int) -> Any: ...  # noqa: E704

    def __getitem__(self, key: object) -> Any:
        if isinstance(key, int):
            return self._values[key]
        if isinstance(key, str):
            return self._mapping[key]
        raise TypeError('database row indexes must be strings or integers')

    def __iter__(self) -> Iterator[str]:
        return iter(self._names)

    def __len__(self) -> int:
        return len(self._names)


def _postgres_row_factory(cursor: Any) -> Any:
    names = tuple(column.name for column in (cursor.description or ()))
    return lambda values: DatabaseRow(names, values)


def _postgres_sql(sql: str) -> str:
    return sql.replace('BEGIN IMMEDIATE', 'BEGIN').replace('?', '%s')


class PostgresConnection:
    dialect = 'postgresql'

    def __init__(self, connection: Any, pool: Any) -> None:
        self._connection = connection
        self._pool = pool
        self._closed = False

    def execute(self, sql: str, parameters: Sequence[Any] = ()) -> Any:
        statement = _postgres_sql(sql)
        if parameters:
            return self._connection.execute(statement, tuple(parameters))
        return self._connection.execute(statement)

    def executemany(self, sql: str, parameters: Any) -> Any:
        cursor = self._connection.cursor()
        cursor.executemany(_postgres_sql(sql), parameters)
        return cursor

    def copy_rows(self, sql: str, rows: Any) -> Any:
        cursor = self._connection.cursor()
        with cursor.copy(sql) as copy:
            for row in rows:
                copy.write_row(row)
        return cursor

    def commit(self) -> None:
        self._connection.commit()

    def rollback(self) -> None:
        self._connection.rollback()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._pool.putconn(self._connection)


def is_postgres(target: DatabaseTarget) -> bool:
    return isinstance(target, str) and target.startswith(
        ('postgresql://', 'postgresql+psycopg://')
    )


def connect_database(target: DatabaseTarget) -> Any:
    if is_postgres(target):
        database_url = str(target).replace('postgresql+psycopg://', 'postgresql://', 1)
        pool = _postgres_pool(database_url)
        return PostgresConnection(pool.getconn(), pool)
    path = Path(target)
    connection = sqlite3.connect(str(path), timeout=30.0)
    connection.row_factory = sqlite3.Row
    connection.execute('PRAGMA foreign_keys=ON')
    connection.execute('PRAGMA busy_timeout=30000')
    return connection


def _postgres_pool(database_url: str) -> Any:
    with _POSTGRES_POOLS_LOCK:
        pool = _POSTGRES_POOLS.get(database_url)
        if pool is None:
            from psycopg_pool import ConnectionPool

            pool = ConnectionPool(
                database_url,
                min_size=1,
                max_size=8,
                kwargs={'autocommit': True, 'row_factory': _postgres_row_factory},
                check=ConnectionPool.check_connection,
                open=True,
            )
            _POSTGRES_POOLS[database_url] = pool
        return pool


def close_database_pools() -> None:
    with _POSTGRES_POOLS_LOCK:
        pools = tuple(_POSTGRES_POOLS.values())
        _POSTGRES_POOLS.clear()
    for pool in pools:
        pool.close()


register(close_database_pools)


def initialize_database(target: DatabaseTarget) -> None:
    if is_postgres(target):
        _initialize_postgres(target)
        return
    path = Path(target)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = connect_database(path)
    try:
        connection.execute('PRAGMA journal_mode=WAL')
        connection.execute('PRAGMA synchronous=NORMAL')
        has_migrations = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='schema_migrations'"
        ).fetchone()
        current_version = 0
        if has_migrations is not None:
            row = connection.execute(
                'SELECT COALESCE(MAX(version),0) FROM schema_migrations'
            ).fetchone()
            current_version = 0 if row is None else int(row[0])
        for version in range(current_version + 1, LATEST_SCHEMA_VERSION + 1):
            migration = _migration_text('migrations', version)
            connection.executescript(
                'BEGIN IMMEDIATE;\n'
                + migration
                + '\nINSERT INTO schema_migrations(version,applied_at) VALUES('
                + str(version)
                + ','
                + str(int(time.time()))
                + ');\nCOMMIT;'
            )
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _initialize_postgres(target: DatabaseTarget) -> None:
    connection = connect_database(target)
    try:
        connection.execute('BEGIN')
        connection.execute('SELECT pg_advisory_xact_lock(8675309001)')
        connection.execute(
            'CREATE TABLE IF NOT EXISTS schema_migrations('
            'version INTEGER PRIMARY KEY,applied_at BIGINT NOT NULL)'
        )
        row = connection.execute(
            'SELECT COALESCE(MAX(version),0) AS version FROM schema_migrations'
        ).fetchone()
        current_version = 0 if row is None else int(row['version'])
        for version in range(current_version + 1, LATEST_SCHEMA_VERSION + 1):
            connection.execute(_migration_text('postgres_migrations', version))
            connection.execute(
                'INSERT INTO schema_migrations(version,applied_at) VALUES(?,?)',
                (version, int(time.time())),
            )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _migration_text(directory: str, version: int) -> str:
    return (
        resources.files('blrec_dashboard_api')
        .joinpath(directory)
        .joinpath('{:04d}_initial.sql'.format(version))
        .read_text(encoding='utf-8')
    )


@contextmanager
def database_session(target: DatabaseTarget) -> Iterator[Any]:
    connection = connect_database(target)
    try:
        yield connection
    finally:
        connection.close()
