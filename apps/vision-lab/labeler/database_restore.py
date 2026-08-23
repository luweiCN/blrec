"""从 NAS 最近一次可验证备份恢复本机 Vision Lab 数据库。"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import tempfile
from pathlib import Path
from typing import Optional, Sequence
from urllib.parse import urlparse

from . import config, database_backup, database_migrate


def download_latest_backup(*, base_url: str, token: str, directory: Path) -> Path:
    parsed = urlparse(base_url.rstrip('/'))
    if parsed.scheme != 'http' or not parsed.hostname:
        raise ValueError('Vision Lab NAS 图片服务必须使用内网 HTTP 地址')
    connection = http.client.HTTPConnection(
        parsed.hostname, parsed.port or 80, timeout=120
    )
    target = f'{parsed.path.rstrip("/")}/api/vision-workers/database-backups/latest'
    try:
        connection.request('GET', target, headers={'Authorization': f'Bearer {token}'})
        response = connection.getresponse()
        if response.status < 200 or response.status >= 300:
            response.read()
            raise RuntimeError(f'读取 NAS Vision Lab 备份失败：HTTP {response.status}')
        name = database_backup.validate_backup_name(
            response.getheader('X-Backup-Filename') or ''
        )
        expected_sha256 = str(response.getheader('X-Checksum-Sha256') or '')
        if len(expected_sha256) != 64:
            raise RuntimeError('NAS Vision Lab 备份缺少校验值')
        directory.mkdir(parents=True, exist_ok=True)
        destination = directory / name
        digest = hashlib.sha256()
        with destination.open('xb') as output:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                output.write(chunk)
                digest.update(chunk)
        if digest.hexdigest() != expected_sha256:
            destination.unlink(missing_ok=True)
            raise RuntimeError('NAS Vision Lab 备份 SHA-256 校验失败')
        destination.chmod(0o600)
        return destination
    finally:
        connection.close()


def main(argv: Optional[Sequence[str]] = None) -> None:
    parser = argparse.ArgumentParser(description='从 NAS 恢复本机 Vision Lab 数据库')
    parser.add_argument('--target-url-file', required=True, type=Path)
    parser.add_argument('--schema', default='vision_lab')
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args(argv)
    if not args.apply:
        raise RuntimeError('恢复会写入本机独立数据库，确认后必须显式传入 --apply')
    if not config.MEDIA_SERVER_URL or not config.VISION_WORKER_TOKEN:
        raise RuntimeError('必须配置 NAS 图片服务与 Vision Worker token')
    target_url = database_migrate._read_secret(args.target_url_file)
    with tempfile.TemporaryDirectory(
        prefix='vision-lab-restore-', dir=config.WORK_DIR
    ) as temporary:
        archive = download_latest_backup(
            base_url=config.MEDIA_SERVER_URL,
            token=config.VISION_WORKER_TOKEN,
            directory=Path(temporary),
        )
        database_migrate.restore_archive(archive, target_url, schema=str(args.schema))
        counts = database_migrate._schema_table_counts(target_url, str(args.schema))
    print('tables={} rows={} restored=ok'.format(len(counts), sum(counts.values())))


if __name__ == '__main__':
    main()
