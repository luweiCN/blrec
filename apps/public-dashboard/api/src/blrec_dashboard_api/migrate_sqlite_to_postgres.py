from __future__ import annotations

import argparse
import os
import re
import sqlite3
import time
from pathlib import Path
from typing import Sequence

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict

from .database import initialize_database

_TABLES = (
    'dashboard_publications',
    'dashboard_publication_standings',
    'match_assets',
    'asset_batches',
)
_IDENTIFIER_PATTERN = re.compile(r'^[a-z][a-z0-9_]*$')


def _sqlite_backup(source_path: Path, backup_directory: Path) -> Path:
    backup_directory.mkdir(parents=True, exist_ok=True)
    backup_path = backup_directory / 'dashboard-before-postgres-{}.sqlite3'.format(
        int(time.time())
    )
    source = sqlite3.connect(
        'file:{}?mode=ro'.format(source_path.resolve()), uri=True, timeout=30
    )
    backup = sqlite3.connect(backup_path, timeout=30)
    try:
        source_check = source.execute('PRAGMA quick_check').fetchone()
        if source_check != ('ok',):
            raise RuntimeError(
                'source SQLite database quick_check failed: {!r}'.format(source_check)
            )
        source.backup(backup)
        backup_check = backup.execute('PRAGMA quick_check').fetchone()
        if backup_check != ('ok',):
            raise RuntimeError(
                'SQLite backup quick_check failed: {!r}'.format(backup_check)
            )
    finally:
        backup.close()
        source.close()
    if not backup_path.is_file() or backup_path.stat().st_size == 0:
        raise RuntimeError('SQLite backup is empty')
    return backup_path


def _sqlite_columns(connection: sqlite3.Connection, table: str) -> tuple[str, ...]:
    if _IDENTIFIER_PATTERN.fullmatch(table) is None:
        raise ValueError('invalid table name')
    return tuple(
        str(row['name'])
        for row in connection.execute('PRAGMA table_info({})'.format(table)).fetchall()
    )


def _postgres_columns(connection: psycopg.Connection, table: str) -> tuple[str, ...]:
    return tuple(
        str(row[0])
        for row in connection.execute(
            'SELECT column_name FROM information_schema.columns '
            'WHERE table_schema=current_schema() AND table_name=%s '
            'ORDER BY ordinal_position',
            (table,),
        ).fetchall()
    )


def _copy_table(
    source: sqlite3.Connection, target: psycopg.Connection, table: str
) -> int:
    source_columns = _sqlite_columns(source, table)
    target_columns = set(_postgres_columns(target, table))
    columns = tuple(column for column in source_columns if column in target_columns)
    if not columns:
        raise RuntimeError('table {} has no shared columns'.format(table))
    select_sql = 'SELECT {} FROM {}'.format(','.join(columns), table)
    copy_sql = sql.SQL('COPY {} ({}) FROM STDIN').format(
        sql.Identifier(table), sql.SQL(',').join(map(sql.Identifier, columns))
    )
    count = 0
    with target.cursor().copy(copy_sql) as copy:
        for row in source.execute(select_sql):
            copy.write_row(tuple(row))
            count += 1
    return count


def migrate(
    source_path: Path, database_url: str, *, backup_directory: Path
) -> dict[str, int]:
    if not source_path.is_file() or source_path.is_symlink():
        raise RuntimeError('source SQLite database is not a regular file')
    connection_info = conninfo_to_dict(database_url)
    database_name = connection_info.get('dbname', '')
    if database_name not in {'blrec_dashboard', 'blrec_dashboard_test'}:
        raise RuntimeError('refusing to migrate into an unexpected PostgreSQL database')

    backup_path = _sqlite_backup(source_path, backup_directory)
    initialize_database(database_url)
    source = sqlite3.connect(
        'file:{}?mode=ro'.format(source_path.resolve()), uri=True, timeout=30
    )
    source.row_factory = sqlite3.Row
    target = psycopg.connect(database_url)
    copied: dict[str, int] = {}
    try:
        with target.transaction():
            target.execute('SELECT pg_advisory_xact_lock(8675309002)')
            nonempty = {
                table: int(
                    target.execute(
                        sql.SQL('SELECT COUNT(*) FROM {}').format(sql.Identifier(table))
                    ).fetchone()[0]
                )
                for table in _TABLES
            }
            nonempty = {table: count for table, count in nonempty.items() if count}
            if nonempty:
                raise RuntimeError(
                    'target PostgreSQL database is not empty: {}'.format(nonempty)
                )
            for table in _TABLES:
                copied[table] = _copy_table(source, target, table)
            for table, expected in copied.items():
                actual = int(
                    target.execute(
                        sql.SQL('SELECT COUNT(*) FROM {}').format(sql.Identifier(table))
                    ).fetchone()[0]
                )
                if actual != expected:
                    raise RuntimeError(
                        '{} row count differs after migration: {} != {}'.format(
                            table, actual, expected
                        )
                    )
    finally:
        target.close()
        source.close()
    print('SQLite backup: {}'.format(backup_path))
    for table in _TABLES:
        print('{}: {}'.format(table, copied[table]))
    return copied


def _arguments(values: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description='Copy the public dashboard SQLite database into empty PostgreSQL.'
    )
    parser.add_argument('--sqlite', required=True, type=Path)
    parser.add_argument('--backup-directory', required=True, type=Path)
    parser.add_argument(
        '--database-url-env', default='DASHBOARD_API_DATABASE_URL', metavar='NAME'
    )
    parser.add_argument('--apply', action='store_true')
    arguments = parser.parse_args(values)
    if not arguments.apply:
        parser.error('--apply is required because the migration changes PostgreSQL')
    return arguments


def main(values: Sequence[str] | None = None) -> None:
    arguments = _arguments(values)
    database_url = os.environ.get(arguments.database_url_env, '').strip()
    if not database_url.startswith(('postgresql://', 'postgresql+psycopg://')):
        raise RuntimeError('PostgreSQL database URL environment variable is missing')
    migrate(
        arguments.sqlite,
        database_url.replace('postgresql+psycopg://', 'postgresql://', 1),
        backup_directory=arguments.backup_directory,
    )


if __name__ == '__main__':
    main()
