"""Vision Lab PostgreSQL 可验证备份与 NAS 流式存储。"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
import re
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Dict, Mapping, Optional, Sequence, Tuple
from urllib.parse import quote, urlparse

from . import config, postgres

_BACKUP_NAME = re.compile(r'^vision-lab-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{12}\.dump$')


def validate_backup_name(value: str) -> str:
    name = str(value or '').strip()
    if _BACKUP_NAME.fullmatch(name) is None:
        raise ValueError('Vision Lab 备份文件名无效')
    return name


def list_backups(directory: Path) -> list[Dict[str, Any]]:
    if not directory.is_dir():
        return []
    result = []
    for path in directory.iterdir():
        if not path.is_file() or _BACKUP_NAME.fullmatch(path.name) is None:
            continue
        stat = path.stat()
        result.append(
            {
                'name': path.name,
                'size_bytes': int(stat.st_size),
                'modified_at': int(stat.st_mtime),
            }
        )
    return sorted(
        result, key=lambda value: (value['modified_at'], value['name']), reverse=True
    )


def prune_backups(directory: Path, *, keep: int) -> None:
    for item in list_backups(directory)[max(2, int(keep)) :]:
        path = directory / str(item['name'])
        path.unlink(missing_ok=True)
        path.with_suffix(path.suffix + '.sha256').unlink(missing_ok=True)


async def store_backup_stream(
    chunks: AsyncIterator[bytes],
    *,
    directory: Path,
    filename: str,
    expected_length: int,
    maximum_bytes: int,
    keep: int,
) -> Dict[str, Any]:
    name = validate_backup_name(filename)
    if not 1 <= int(expected_length) <= int(maximum_bytes):
        raise ValueError('Vision Lab 备份大小无效')
    if directory.is_symlink():
        raise ValueError('Vision Lab 备份目录不能是符号链接')
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / name
    if destination.exists():
        raise FileExistsError(name)
    temporary = directory / f'.{name}.{os.getpid()}.tmp'
    digest = hashlib.sha256()
    size = 0
    try:
        with temporary.open('xb') as output:
            async for chunk in chunks:
                if not chunk:
                    continue
                size += len(chunk)
                if size > int(maximum_bytes) or size > int(expected_length):
                    raise ValueError('Vision Lab 备份超过声明大小')
                output.write(chunk)
                digest.update(chunk)
            output.flush()
            os.fsync(output.fileno())
        if size != int(expected_length):
            raise ValueError('Vision Lab 备份未完整上传')
        temporary.chmod(0o600)
        temporary.replace(destination)
        checksum = digest.hexdigest()
        checksum_path = destination.with_suffix(destination.suffix + '.sha256')
        checksum_path.write_text(f'{checksum}  {name}\n', encoding='ascii')
        checksum_path.chmod(0o600)
        prune_backups(directory, keep=keep)
        return {'name': name, 'size_bytes': size, 'sha256': checksum}
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _postgres_connection_info(database_url: str) -> Dict[str, str]:
    from psycopg.conninfo import conninfo_to_dict

    return {
        str(key): str(value)
        for key, value in conninfo_to_dict(database_url).items()
        if value is not None
    }


def _postgres_command(
    executable: str, connection_info: Mapping[str, str], *arguments: str
) -> Tuple[str, ...]:
    command = [executable, *arguments]
    for key, option in (
        ('host', '--host'),
        ('port', '--port'),
        ('user', '--username'),
        ('dbname', '--dbname'),
    ):
        value = str(connection_info.get(key) or '')
        if value:
            command.extend((option, value))
    return tuple(command)


def _postgres_environment(connection_info: Mapping[str, str]) -> Dict[str, str]:
    environment = dict(os.environ)
    for name in (
        'VISION_LAB_DATABASE_URL',
        'BLREC_DATABASE_URL',
        'DASHBOARD_DATABASE_URL',
    ):
        environment.pop(name, None)
    password = str(connection_info.get('password') or '')
    if password:
        environment['PGPASSWORD'] = password
    environment['PGCONNECT_TIMEOUT'] = str(
        connection_info.get('connect_timeout') or '5'
    )
    return environment


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as source:
        while True:
            chunk = source.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def create_backup(database_url: str, *, schema: str, directory: Path) -> Path:
    import psycopg
    from psycopg import sql

    normalized_schema = postgres.validate_schema_name(schema)
    pg_dump = os.environ.get('VISION_LAB_PG_DUMP', '').strip() or shutil.which(
        'pg_dump'
    )
    pg_restore = os.environ.get('VISION_LAB_PG_RESTORE', '').strip() or shutil.which(
        'pg_restore'
    )
    if not pg_dump or not pg_restore:
        raise RuntimeError('本机缺少 pg_dump 或 pg_restore')
    with psycopg.connect(database_url, autocommit=True) as connection:
        row = connection.execute(
            'SELECT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname=%s)',
            (normalized_schema,),
        ).fetchone()
        if row is None or not bool(row[0]):
            raise RuntimeError('Vision Lab schema 不存在')
        connection.execute(
            sql.SQL('SET search_path TO {}').format(sql.Identifier(normalized_schema))
        )
        version = connection.execute(
            'SELECT COALESCE(MAX(version),0) FROM vision_schema_migrations'
        ).fetchone()
        if version is None or int(version[0]) != postgres.POSTGRES_SCHEMA_VERSION:
            raise RuntimeError('Vision Lab schema 版本不符合当前程序')
    connection_info = _postgres_connection_info(database_url)
    directory.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        prefix='vision-lab-', suffix='.dump', dir=directory, delete=False
    )
    handle.close()
    destination = Path(handle.name)
    environment = _postgres_environment(connection_info)
    try:
        dump = subprocess.run(
            _postgres_command(
                pg_dump,
                connection_info,
                '--format=custom',
                '--compress=6',
                '--no-owner',
                '--no-privileges',
                '--schema',
                normalized_schema,
                '--file',
                str(destination),
            ),
            env=environment,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        if (
            dump.returncode != 0
            or not destination.is_file()
            or destination.stat().st_size == 0
        ):
            raise RuntimeError('pg_dump 未能生成 Vision Lab 备份')
        listing = subprocess.run(
            (pg_restore, '--list', str(destination)),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        if (
            listing.returncode != 0
            or normalized_schema not in listing.stdout
            or 'TABLE' not in listing.stdout
        ):
            raise RuntimeError('pg_restore 未能验证 Vision Lab 备份')
        digest = _file_sha256(destination)
        timestamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        final = directory / f'vision-lab-{timestamp}-{digest[:12]}.dump'
        destination.chmod(0o600)
        destination.replace(final)
        return final
    except BaseException:
        destination.unlink(missing_ok=True)
        raise


def upload_backup(path: Path, *, base_url: str, token: str) -> Dict[str, Any]:
    parsed = urlparse(base_url.rstrip('/'))
    if parsed.scheme != 'http' or not parsed.hostname:
        raise ValueError('Vision Lab NAS 图片服务必须使用内网 HTTP 地址')
    name = validate_backup_name(path.name)
    size = path.stat().st_size
    connection = http.client.HTTPConnection(
        parsed.hostname, parsed.port or 80, timeout=120
    )
    target = (
        f'{parsed.path.rstrip("/")}/api/vision-workers/database-backups/{quote(name)}'
    )
    try:
        connection.putrequest('PUT', target)
        connection.putheader('Authorization', f'Bearer {token}')
        connection.putheader('Content-Type', 'application/octet-stream')
        connection.putheader('Content-Length', str(size))
        connection.endheaders()
        with path.open('rb') as source:
            while True:
                chunk = source.read(1024 * 1024)
                if not chunk:
                    break
                connection.send(chunk)
        response = connection.getresponse()
        payload = response.read()
        if response.status < 200 or response.status >= 300:
            raise RuntimeError(f'NAS 备份上传失败：HTTP {response.status}')
        decoded = json.loads(payload.decode('utf-8'))
        if not isinstance(decoded, dict):
            raise RuntimeError('NAS 备份响应无效')
        return decoded
    finally:
        connection.close()


def main(argv: Optional[Sequence[str]] = None) -> None:
    parser = argparse.ArgumentParser(description='备份 Vision Lab PostgreSQL 到 NAS')
    parser.add_argument('--keep-local', action='store_true')
    args = parser.parse_args(argv)
    if not config.DATABASE_URL:
        raise RuntimeError('必须配置 VISION_LAB_DATABASE_URL')
    if not config.MEDIA_SERVER_URL:
        raise RuntimeError('必须配置 VISION_LAB_MEDIA_SERVER_URL')
    if not config.VISION_WORKER_TOKEN:
        raise RuntimeError('必须配置 VISION_LAB_WORKER_TOKEN')
    spool = config.WORK_DIR / 'database-backup-spool'
    backup = create_backup(
        config.DATABASE_URL, schema=config.DATABASE_SCHEMA, directory=spool
    )
    try:
        result = upload_backup(
            backup, base_url=config.MEDIA_SERVER_URL, token=config.VISION_WORKER_TOKEN
        )
        print(
            'backup={} bytes={} sha256={} uploaded=ok'.format(
                result.get('name'), result.get('size_bytes'), result.get('sha256')
            )
        )
    finally:
        if not args.keep_local:
            backup.unlink(missing_ok=True)


if __name__ == '__main__':
    main()
