from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from importlib import resources
from pathlib import Path
from typing import Iterator

LATEST_SCHEMA_VERSION = 1


def connect_database(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(str(path), timeout=30.0)
    connection.row_factory = sqlite3.Row
    connection.execute('PRAGMA foreign_keys=ON')
    connection.execute('PRAGMA busy_timeout=30000')
    return connection


def initialize_database(path: Path) -> None:
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
            migration_name = f'{version:04d}_initial.sql'
            migration = (
                resources.files('blrec_dashboard_api')
                .joinpath('migrations', migration_name)
                .read_text(encoding='utf-8')
            )
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


@contextmanager
def database_session(path: Path) -> Iterator[sqlite3.Connection]:
    connection = connect_database(path)
    try:
        yield connection
    finally:
        connection.close()
