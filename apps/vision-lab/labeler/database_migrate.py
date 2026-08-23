"""把 Vision Lab schema 从远程 PostgreSQL 一次性迁到本机独立数据库。"""

from __future__ import annotations

import argparse
import os
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path
from typing import Dict, Optional, Sequence

from . import config, database_backup, postgres


def _read_secret(path: Path) -> str:
    resolved = path.expanduser().resolve()
    if stat.S_IMODE(resolved.stat().st_mode) & 0o077:
        raise RuntimeError(f'{resolved} 权限必须为 600')
    value = resolved.read_text(encoding='utf-8').strip()
    if not value:
        raise RuntimeError(f'{resolved} 不能为空')
    return value


def _require_local_target(database_url: str) -> None:
    info = database_backup._postgres_connection_info(database_url)
    host = str(info.get('host') or '')
    if host not in {'', 'localhost', '127.0.0.1', '::1'} and not host.startswith('/'):
        raise ValueError('Vision Lab 目标数据库必须位于本机')


def _schema_table_counts(database_url: str, schema: str) -> Dict[str, int]:
    import psycopg
    from psycopg import sql

    normalized = postgres.validate_schema_name(schema)
    with psycopg.connect(database_url, autocommit=True) as connection:
        tables = [
            str(row[0])
            for row in connection.execute(
                'SELECT table_name FROM information_schema.tables '
                "WHERE table_schema=%s AND table_type='BASE TABLE' "
                'ORDER BY table_name',
                (normalized,),
            ).fetchall()
        ]
        return {
            table: int(
                connection.execute(
                    sql.SQL('SELECT COUNT(*) FROM {}.{}').format(
                        sql.Identifier(normalized), sql.Identifier(table)
                    )
                ).fetchone()[0]
            )
            for table in tables
        }


def restore_archive(archive: Path, target_url: str, *, schema: str) -> None:
    import psycopg
    from psycopg import sql

    normalized = postgres.validate_schema_name(schema)
    _require_local_target(target_url)
    with psycopg.connect(target_url, autocommit=True) as target:
        exists = target.execute(
            'SELECT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname=%s)', (normalized,)
        ).fetchone()
        if exists is not None and bool(exists[0]):
            raise RuntimeError('本机目标数据库已有 Vision Lab schema，拒绝覆盖')
    pg_restore = os.environ.get('VISION_LAB_PG_RESTORE', '').strip() or shutil.which(
        'pg_restore'
    )
    if not pg_restore:
        raise RuntimeError('本机缺少 pg_restore')
    listing = subprocess.run(
        (pg_restore, '--list', str(archive)),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if listing.returncode != 0 or normalized not in listing.stdout:
        raise RuntimeError('Vision Lab 备份归档校验失败')
    target_info = database_backup._postgres_connection_info(target_url)
    restored = subprocess.run(
        database_backup._postgres_command(
            pg_restore,
            target_info,
            '--exit-on-error',
            '--single-transaction',
            '--no-owner',
            '--no-privileges',
            str(archive),
        ),
        env=database_backup._postgres_environment(target_info),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if restored.returncode != 0:
        raise RuntimeError('pg_restore 未能恢复本机 Vision Lab 数据库')
    with psycopg.connect(target_url, autocommit=True) as target:
        target.execute(
            sql.SQL('SET search_path TO {}').format(sql.Identifier(normalized))
        )
        version = target.execute(
            'SELECT COALESCE(MAX(version),0) FROM vision_schema_migrations'
        ).fetchone()
        if version is None or int(version[0]) != postgres.POSTGRES_SCHEMA_VERSION:
            raise RuntimeError('恢复后的 Vision Lab schema 版本无效')


def migrate_postgres_database(
    source_url: str, target_url: str, *, schema: str, working_directory: Path
) -> Dict[str, int]:
    normalized = postgres.validate_schema_name(schema)
    _require_local_target(target_url)
    source_counts = _schema_table_counts(source_url, normalized)
    if not source_counts:
        raise RuntimeError('源 Vision Lab schema 没有数据表')
    archive = database_backup.create_backup(
        source_url, schema=normalized, directory=working_directory
    )
    restore_archive(archive, target_url, schema=normalized)
    target_counts = _schema_table_counts(target_url, normalized)
    if target_counts != source_counts:
        differences = sorted(
            table
            for table in set(source_counts) | set(target_counts)
            if source_counts.get(table) != target_counts.get(table)
        )
        raise RuntimeError('Vision Lab 迁移行数校验失败：' + ','.join(differences[:10]))
    return {
        'tables': len(source_counts),
        'rows': sum(source_counts.values()),
        'archive_bytes': archive.stat().st_size,
    }


def main(argv: Optional[Sequence[str]] = None) -> None:
    parser = argparse.ArgumentParser(description='迁移 Vision Lab PostgreSQL 到本机')
    parser.add_argument('--source-url-file', required=True, type=Path)
    parser.add_argument('--target-url-file', required=True, type=Path)
    parser.add_argument('--schema', default='vision_lab')
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args(argv)
    if not args.apply:
        raise RuntimeError('迁移会写入本机独立数据库，确认后必须显式传入 --apply')
    source_url = _read_secret(args.source_url_file)
    target_url = _read_secret(args.target_url_file)
    with tempfile.TemporaryDirectory(
        prefix='vision-lab-migrate-', dir=config.WORK_DIR
    ) as temporary:
        result = migrate_postgres_database(
            source_url,
            target_url,
            schema=args.schema,
            working_directory=Path(temporary),
        )
    print(
        'tables={} rows={} archive_bytes={} verified=ok'.format(
            result['tables'], result['rows'], result['archive_bytes']
        )
    )


if __name__ == '__main__':
    main()
