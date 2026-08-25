from __future__ import annotations

import argparse
import os
import re
import sqlite3
import time
from pathlib import Path
from typing import Iterator, Sequence, Tuple

import psycopg
from psycopg.conninfo import conninfo_to_dict

from blrec.bili_upload import database as database_module
from blrec.bili_upload.database import BiliUploadDatabase
from blrec.bili_upload.postgres_database import postgres_schema_sql

_IDENTIFIER = re.compile(r'^[a-z][a-z0-9_]*$')
_SUPPORTED_MIGRATIONS = (77, 78, 79, 80, 81, 82)


def _migration_path(version: int) -> Path:
    module_path = Path(str(database_module.__file__)).resolve()
    return module_path.with_name('migrations') / '{:04d}_initial.sql'.format(version)


def _migration_statements(version: int) -> Iterator[str]:
    statement = ''
    for line in (
        _migration_path(version).read_text(encoding='utf8').splitlines(keepends=True)
    ):
        statement += line
        if sqlite3.complete_statement(statement):
            sql = statement.strip()
            if sql:
                yield postgres_schema_sql(sql)
            statement = ''
    if statement.strip():
        raise RuntimeError('migration {} has an incomplete statement'.format(version))


def migrate_postgres_schema(
    database_url: str, *, expected_database: str, expected_schema: str
) -> Tuple[int, ...]:
    normalized_url = database_url.replace('postgresql+psycopg://', 'postgresql://', 1)
    connection_info = conninfo_to_dict(normalized_url)
    database_name = str(connection_info.get('dbname', ''))
    if not expected_database or database_name != expected_database:
        raise RuntimeError(
            'refusing PostgreSQL database {!r}; expected {!r}'.format(
                database_name, expected_database
            )
        )
    if _IDENTIFIER.fullmatch(expected_schema) is None:
        raise ValueError('expected PostgreSQL schema is invalid')

    with psycopg.connect(normalized_url) as connection:
        schema_row = connection.execute('SELECT current_schema()').fetchone()
        current_schema = None if schema_row is None else schema_row[0]
        if current_schema != expected_schema:
            raise RuntimeError(
                'refusing PostgreSQL schema {!r}; expected {!r}'.format(
                    current_schema, expected_schema
                )
            )
        locked = connection.execute(
            "SELECT pg_try_advisory_lock(hashtext('blrec-main-database'))"
        ).fetchone()
        if locked is None or not bool(locked[0]):
            raise RuntimeError('main database is still owned by a running service')
        version_row = connection.execute(
            'SELECT COALESCE(MAX(version),0) FROM schema_migrations'
        ).fetchone()
        current_version = 0 if version_row is None else int(version_row[0])
        target_version = BiliUploadDatabase.LATEST_SCHEMA_VERSION
        if current_version > target_version:
            raise RuntimeError('PostgreSQL schema is newer than this application')
        pending = tuple(range(current_version + 1, target_version + 1))
        unsupported = tuple(
            version for version in pending if version not in _SUPPORTED_MIGRATIONS
        )
        if unsupported:
            raise RuntimeError(
                'PostgreSQL migrations are not implemented for {}'.format(unsupported)
            )
        for version in pending:
            for statement in _migration_statements(version):
                connection.execute(statement)
            connection.execute(
                'INSERT INTO schema_migrations(version,applied_at) VALUES(%s,%s)',
                (version, int(time.time())),
            )
        connection.commit()
        return pending


def main(values: Sequence[str] = ()) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--database-url-env', default='BLREC_DATABASE_URL')
    parser.add_argument(
        '--expected-database',
        default=os.environ.get('BLREC_DATABASE_NAME', 'blrec_dashboard'),
    )
    parser.add_argument(
        '--expected-schema', default=os.environ.get('BLREC_DATABASE_SCHEMA', 'core')
    )
    args = parser.parse_args(None if not values else values)
    if not args.apply:
        parser.error('--apply is required because the migration changes PostgreSQL')
    database_url = os.environ.get(args.database_url_env, '').strip()
    if not database_url.startswith(('postgresql://', 'postgresql+psycopg://')):
        raise ValueError('database URL must use PostgreSQL')
    applied = migrate_postgres_schema(
        database_url,
        expected_database=args.expected_database,
        expected_schema=args.expected_schema,
    )
    print(
        'backend=postgresql schema={} migrations={} integrity=ok'.format(
            args.expected_schema,
            ','.join(str(version) for version in applied) if applied else 'none',
        )
    )


if __name__ == '__main__':
    main()
