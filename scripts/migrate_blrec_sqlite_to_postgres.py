from __future__ import annotations

import argparse
import fcntl
import os
import re
import sqlite3
import time
from contextlib import closing, contextmanager
from pathlib import Path
from typing import BinaryIO, Dict, Iterable, Iterator, List, Sequence, Set, Tuple

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict

from blrec.bili_upload.database import BiliUploadDatabase
from blrec.bili_upload.postgres_database import (
    POSTGRES_COMPATIBILITY_SQL,
    postgres_schema_sql,
)

_IDENTIFIER = re.compile(r'^[a-z][a-z0-9_]*$')


@contextmanager
def _exclusive_source_lock(source_path: Path) -> Iterator[BinaryIO]:
    lock_path = Path(str(source_path) + '.lock')
    lock_file = open(lock_path, 'a+b', buffering=0)
    os.fchmod(lock_file.fileno(), 0o600)
    try:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError) as error:
            raise RuntimeError(
                'source SQLite database is still owned by a running service'
            ) from error
        yield lock_file
    finally:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        finally:
            lock_file.close()


def _backup_sqlite(source_path: Path, backup_directory: Path) -> Path:
    backup_directory.mkdir(parents=True, exist_ok=True)
    backup_path = backup_directory / 'blrec-before-postgres-{}.sqlite3'.format(
        int(time.time())
    )
    source = sqlite3.connect(
        'file:{}?mode=ro'.format(source_path.resolve()), uri=True, timeout=60
    )
    backup = sqlite3.connect(str(backup_path), timeout=60)
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


def _table_names(connection: sqlite3.Connection) -> Tuple[str, ...]:
    return tuple(
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
    )


def _table_order(
    connection: sqlite3.Connection, tables: Sequence[str]
) -> Tuple[str, ...]:
    remaining: Dict[str, Set[str]] = {}
    known = set(tables)
    for table in tables:
        if _IDENTIFIER.fullmatch(table) is None:
            raise RuntimeError('invalid SQLite table name: {!r}'.format(table))
        dependencies = {
            str(row[2])
            for row in connection.execute(
                'PRAGMA foreign_key_list({})'.format(table)
            ).fetchall()
            if str(row[2]) in known and str(row[2]) != table
        }
        remaining[table] = dependencies
    ordered: List[str] = []
    while remaining:
        ready = sorted(
            table
            for table, dependencies in remaining.items()
            if dependencies.isdisjoint(remaining)
        )
        if not ready:
            raise RuntimeError(
                'cross-table foreign key cycle prevents PostgreSQL migration: '
                '{}'.format(sorted(remaining))
            )
        ordered.extend(ready)
        for table in ready:
            remaining.pop(table)
    return tuple(ordered)


def _source_sql(connection: sqlite3.Connection, object_type: str, name: str) -> str:
    row = connection.execute(
        'SELECT sql FROM sqlite_master WHERE type=? AND name=?', (object_type, name)
    ).fetchone()
    if row is None or not row[0]:
        raise RuntimeError('SQLite {} {} has no schema SQL'.format(object_type, name))
    return str(row[0])


def _columns(connection: sqlite3.Connection, table: str) -> Tuple[str, ...]:
    return tuple(
        str(row[1])
        for row in connection.execute('PRAGMA table_info({})'.format(table)).fetchall()
    )


def _copy_table(
    source: sqlite3.Connection, target: psycopg.Connection, table: str
) -> int:
    columns = _columns(source, table)
    select_statement = 'SELECT {} FROM {}'.format(
        ','.join('"{}"'.format(column) for column in columns), table
    )
    copy_statement = sql.SQL('COPY {} ({}) FROM STDIN').format(
        sql.Identifier(table), sql.SQL(',').join(map(sql.Identifier, columns))
    )
    count = 0
    with target.cursor().copy(copy_statement) as copy:
        for row in source.execute(select_statement):
            copy.write_row(tuple(row))
            count += 1
    return count


def _explicit_indexes(connection: sqlite3.Connection) -> Iterable[Tuple[str, str]]:
    rows = connection.execute(
        "SELECT name,sql FROM sqlite_master WHERE type='index' "
        "AND sql IS NOT NULL ORDER BY name"
    ).fetchall()
    for name, statement in rows:
        yield str(name), str(statement)


def _dashboard_triggers(
    connection: sqlite3.Connection,
) -> Iterable[Tuple[str, str, str]]:
    rows = connection.execute(
        "SELECT name,sql FROM sqlite_master WHERE type='trigger' ORDER BY name"
    ).fetchall()
    for name, statement in rows:
        match = re.search(
            r'\bAFTER\s+(INSERT|DELETE|UPDATE(?:\s+OF\s+'
            r'[A-Za-z_][A-Za-z0-9_]*(?:\s*,\s*'
            r'[A-Za-z_][A-Za-z0-9_]*)*)?)\s+ON\s+'
            r'([A-Za-z_][A-Za-z0-9_]*)',
            str(statement),
            re.I,
        )
        if match is None or not str(name).startswith('dashboard_source_'):
            raise RuntimeError('unsupported SQLite trigger: {}'.format(name))
        yield str(name), match.group(1).upper(), match.group(2)


def _create_dashboard_triggers(
    source: sqlite3.Connection, target: psycopg.Connection
) -> None:
    triggers = tuple(_dashboard_triggers(source))
    if not triggers:
        return
    target.execute(
        "CREATE FUNCTION blrec_touch_dashboard_source_state() "
        "RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN "
        "UPDATE dashboard_source_state SET revision=revision+1,"
        "changed_at=EXTRACT(EPOCH FROM clock_timestamp())::bigint "
        "WHERE singleton_id=1; RETURN NULL; END $$"
    )
    for name, event, table in triggers:
        target.execute(
            sql.SQL('CREATE TRIGGER {} AFTER {} ON {} FOR EACH ROW ').format(
                sql.Identifier(name), sql.SQL(event), sql.Identifier(table)
            )
            + sql.SQL('EXECUTE FUNCTION blrec_touch_dashboard_source_state()')
        )


def _reset_identity(target: psycopg.Connection, table: str) -> None:
    sequence = target.execute(
        'SELECT pg_get_serial_sequence(%s,%s)', (table, 'id')
    ).fetchone()
    if sequence is None or sequence[0] is None:
        return
    maximum = target.execute(
        sql.SQL('SELECT MAX(id) FROM {}').format(sql.Identifier(table))
    ).fetchone()
    value = None if maximum is None else maximum[0]
    if value is None:
        target.execute('SELECT setval(%s,1,false)', (sequence[0],))
    else:
        target.execute('SELECT setval(%s,%s,true)', (sequence[0], int(value)))


def migrate(
    source_path: Path,
    database_url: str,
    *,
    expected_database: str,
    expected_schema: str,
    backup_directory: Path,
) -> Dict[str, int]:
    if not source_path.is_file() or source_path.is_symlink():
        raise RuntimeError('source SQLite database is not a regular file')
    connection_info = conninfo_to_dict(database_url)
    database_name = str(connection_info.get('dbname', ''))
    if not expected_database or database_name != expected_database:
        raise RuntimeError(
            'refusing PostgreSQL target {!r}; expected {!r}'.format(
                database_name, expected_database
            )
        )
    copied: Dict[str, int] = {}
    with _exclusive_source_lock(source_path):
        backup_path = _backup_sqlite(source_path, backup_directory)
        with closing(
            sqlite3.connect(
                'file:{}?mode=ro'.format(backup_path.resolve()), uri=True, timeout=60
            )
        ) as source, closing(psycopg.connect(database_url, autocommit=True)) as target:
            schema_row = target.execute('SELECT current_schema()').fetchone()
            if schema_row is None or schema_row[0] is None:
                raise RuntimeError('PostgreSQL current schema is unavailable')
            current_schema = str(schema_row[0])
            if current_schema != expected_schema:
                raise RuntimeError(
                    'refusing PostgreSQL schema {!r}; expected {!r}'.format(
                        current_schema, expected_schema
                    )
                )
            source_version = source.execute(
                'SELECT COALESCE(MAX(version),0) FROM schema_migrations'
            ).fetchone()
            current_version = 0 if source_version is None else int(source_version[0])
            if current_version != BiliUploadDatabase.LATEST_SCHEMA_VERSION:
                raise RuntimeError(
                    'source schema version {} does not match required {}'.format(
                        current_version, BiliUploadDatabase.LATEST_SCHEMA_VERSION
                    )
                )
            tables = _table_order(source, _table_names(source))
            with target.transaction():
                target.execute('SELECT pg_advisory_xact_lock(8675309073)')
                existing = target.execute(
                    'SELECT table_name FROM information_schema.tables '
                    "WHERE table_schema=current_schema() AND table_type='BASE TABLE'"
                ).fetchall()
                if existing:
                    raise RuntimeError(
                        'target PostgreSQL database is not empty: {}'.format(
                            sorted(str(row[0]) for row in existing)
                        )
                    )
                target.execute(POSTGRES_COMPATIBILITY_SQL)
                for table in tables:
                    target.execute(
                        postgres_schema_sql(_source_sql(source, 'table', table))
                    )
                for table in tables:
                    copied[table] = _copy_table(source, target, table)
                for _name, statement in _explicit_indexes(source):
                    target.execute(postgres_schema_sql(statement))
                _create_dashboard_triggers(source, target)
                for table in tables:
                    if 'id' in _columns(source, table):
                        _reset_identity(target, table)
                for table, expected in copied.items():
                    actual = int(
                        target.execute(
                            sql.SQL('SELECT COUNT(*) FROM {}').format(
                                sql.Identifier(table)
                            )
                        ).fetchone()[0]
                    )
                    if actual != expected:
                        raise RuntimeError(
                            '{} row count differs after migration: {} != {}'.format(
                                table, actual, expected
                            )
                        )
    print('SQLite backup: {}'.format(backup_path))
    print('PostgreSQL tables: {}'.format(len(copied)))
    print('PostgreSQL rows: {}'.format(sum(copied.values())))
    return copied


def _arguments(values: Sequence[str] = ()) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description='Copy the BLREC main SQLite database into empty PostgreSQL.'
    )
    parser.add_argument('--sqlite', required=True, type=Path)
    parser.add_argument('--backup-directory', required=True, type=Path)
    parser.add_argument('--expected-database', required=True)
    parser.add_argument('--expected-schema', required=True)
    parser.add_argument(
        '--database-url-env', default='BLREC_DATABASE_URL', metavar='NAME'
    )
    parser.add_argument('--apply', action='store_true')
    arguments = parser.parse_args(None if not values else values)
    if not arguments.apply:
        parser.error('--apply is required because the migration changes PostgreSQL')
    return arguments


def main(values: Sequence[str] = ()) -> None:
    arguments = _arguments(values)
    database_url = os.environ.get(arguments.database_url_env, '').strip()
    if not database_url.startswith(('postgresql://', 'postgresql+psycopg://')):
        raise RuntimeError('PostgreSQL database URL environment variable is missing')
    migrate(
        arguments.sqlite,
        database_url.replace('postgresql+psycopg://', 'postgresql://', 1),
        expected_database=arguments.expected_database,
        expected_schema=arguments.expected_schema,
        backup_directory=arguments.backup_directory,
    )


if __name__ == '__main__':
    main()
