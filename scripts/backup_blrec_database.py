from __future__ import annotations

import argparse
import os
import re
import sqlite3
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Mapping, Sequence, Tuple

from blrec.bili_upload.database import BiliUploadDatabase

_IDENTIFIER = re.compile(r'^[a-z][a-z0-9_]*$')


def _backup_path(backup_dir: Path, label: str, suffix: str) -> Path:
    if backup_dir.is_symlink():
        raise ValueError('backup directory must not be a symlink')
    backup_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime('%Y%m%d-%H%M%S')
    path = backup_dir / 'blrec-before-{}-{}{}'.format(label, timestamp, suffix)
    if path.exists():
        raise FileExistsError(path)
    return path


def _backup_sqlite(database_path: Path, backup_path: Path) -> None:
    if not database_path.is_file() or database_path.is_symlink():
        raise ValueError('SQLite database must be a regular file')
    source = sqlite3.connect(database_path)
    try:
        source_integrity = source.execute('PRAGMA quick_check').fetchone()
        if source_integrity is None or source_integrity[0] != 'ok':
            raise RuntimeError(
                'source quick_check failed: {!r}'.format(source_integrity)
            )
        backup = sqlite3.connect(backup_path)
        try:
            source.backup(backup)
            integrity = backup.execute('PRAGMA quick_check').fetchone()
            if integrity is None or integrity[0] != 'ok':
                raise RuntimeError('backup quick_check failed: {!r}'.format(integrity))
        finally:
            backup.close()
    finally:
        source.close()
    backup_path.chmod(0o600)


def _postgres_dump_command(
    connection_info: Mapping[str, str], backup_path: Path, schema: str
) -> Tuple[str, ...]:
    command = [
        'pg_dump',
        '--format=custom',
        '--compress=6',
        '--no-owner',
        '--no-privileges',
        '--schema={}'.format(schema),
        '--file={}'.format(backup_path),
    ]
    options = (
        ('host', '--host'),
        ('port', '--port'),
        ('user', '--username'),
        ('dbname', '--dbname'),
    )
    for key, option in options:
        value = connection_info.get(key, '')
        if value:
            command.append('{}={}'.format(option, value))
    return tuple(command)


def _backup_postgres(
    database_url: str,
    backup_path: Path,
    *,
    expected_database: str,
    expected_schema: str,
) -> None:
    import psycopg
    from psycopg.conninfo import conninfo_to_dict

    connection_info = conninfo_to_dict(database_url)
    database_name = str(connection_info.get('dbname', ''))
    if not expected_database or database_name != expected_database:
        raise RuntimeError(
            'refusing PostgreSQL database {!r}; expected {!r}'.format(
                database_name, expected_database
            )
        )
    if _IDENTIFIER.fullmatch(expected_schema) is None:
        raise ValueError('expected PostgreSQL schema is invalid')
    with psycopg.connect(database_url, autocommit=True) as connection:
        schema_row = connection.execute('SELECT current_schema()').fetchone()
        if schema_row is None or schema_row[0] is None:
            raise RuntimeError('PostgreSQL current schema is unavailable')
        current_schema = str(schema_row[0])
        if current_schema != expected_schema:
            raise RuntimeError(
                'refusing PostgreSQL schema {!r}; expected {!r}'.format(
                    current_schema, expected_schema
                )
            )
        version = connection.execute(
            'SELECT COALESCE(MAX(version),0) FROM schema_migrations'
        ).fetchone()
        current_version = 0 if version is None else int(version[0])
        if current_version != BiliUploadDatabase.LATEST_SCHEMA_VERSION:
            raise RuntimeError(
                'PostgreSQL schema version {} does not match required {}'.format(
                    current_version, BiliUploadDatabase.LATEST_SCHEMA_VERSION
                )
            )

    environment = dict(os.environ)
    environment.pop('BLREC_DATABASE_URL', None)
    environment.pop('DASHBOARD_DATABASE_URL', None)
    password = connection_info.get('password', '')
    if password:
        environment['PGPASSWORD'] = password
    environment['PGCONNECT_TIMEOUT'] = str(connection_info.get('connect_timeout', '5'))
    try:
        result = subprocess.run(
            _postgres_dump_command(connection_info, backup_path, expected_schema),
            env=environment,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(
                'pg_dump failed with exit code {}'.format(result.returncode)
            )
        backup_path.chmod(0o600)
        listing = subprocess.run(
            ('pg_restore', '--list', str(backup_path)),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        if listing.returncode != 0 or not listing.stdout.strip():
            raise RuntimeError('pg_restore could not validate PostgreSQL backup')
        if expected_schema not in listing.stdout or 'TABLE' not in listing.stdout:
            raise RuntimeError('PostgreSQL backup does not contain the expected schema')
    except BaseException:
        if backup_path.exists():
            backup_path.unlink()
        raise


def main(values: Sequence[str] = ()) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--database', default='/cfg/blrec.sqlite3')
    parser.add_argument('--backup-dir', default='/cfg/backups')
    parser.add_argument('--label', required=True)
    parser.add_argument('--database-url-env', default='BLREC_DATABASE_URL')
    parser.add_argument(
        '--expected-database',
        default=os.environ.get('BLREC_DATABASE_NAME', 'blrec_dashboard'),
    )
    parser.add_argument(
        '--expected-schema', default=os.environ.get('BLREC_DATABASE_SCHEMA', 'core')
    )
    args = parser.parse_args(None if not values else values)

    label = args.label.strip()
    if not re.fullmatch(r'[A-Za-z0-9._-]+', label):
        raise ValueError('label contains unsupported characters')

    backup_dir = Path(os.path.abspath(os.path.expanduser(args.backup_dir)))
    database_url = os.environ.get(args.database_url_env, '').strip()
    if database_url:
        if not database_url.startswith(('postgresql://', 'postgresql+psycopg://')):
            raise ValueError('database URL must use PostgreSQL')
        backup_path = _backup_path(backup_dir, label, '.dump')
        _backup_postgres(
            database_url.replace('postgresql+psycopg://', 'postgresql://', 1),
            backup_path,
            expected_database=args.expected_database,
            expected_schema=args.expected_schema,
        )
        backend = 'postgresql'
    else:
        database_path = Path(os.path.abspath(os.path.expanduser(args.database)))
        backup_path = _backup_path(backup_dir, label, '.sqlite3')
        _backup_sqlite(database_path, backup_path)
        backend = 'sqlite'

    if not backup_path.is_file() or backup_path.stat().st_size == 0:
        raise RuntimeError('database backup is empty')
    print(
        'backend={} backup={} bytes={} integrity=ok'.format(
            backend, backup_path.name, backup_path.stat().st_size
        )
    )


if __name__ == '__main__':
    main()
