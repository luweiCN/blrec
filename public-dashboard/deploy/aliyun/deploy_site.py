#!/usr/bin/env python3
from __future__ import annotations

import argparse
import mimetypes
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, List, Mapping, Optional

IMMUTABLE_NAME = re.compile(r'\.[0-9a-f]{8,}\.', re.IGNORECASE)
DATA_PREFIX = 'data/'


class SiteDeploymentError(RuntimeError):
    pass


@dataclass(frozen=True)
class SiteUpload:
    source: Path
    object_key: str
    headers: Mapping[str, str]


def _content_type(path: Path) -> str:
    overrides = {
        '.css': 'text/css; charset=utf-8',
        '.html': 'text/html; charset=utf-8',
        '.js': 'application/javascript; charset=utf-8',
        '.json': 'application/json; charset=utf-8',
        '.svg': 'image/svg+xml',
    }
    if path.suffix.casefold() in overrides:
        return overrides[path.suffix.casefold()]
    guessed, _encoding = mimetypes.guess_type(path.name)
    return guessed or 'application/octet-stream'


def _cache_control(object_key: str) -> str:
    if object_key == 'index.html':
        return 'no-cache, no-store, must-revalidate'
    if IMMUTABLE_NAME.search(Path(object_key).name):
        return 'public, max-age=31536000, immutable'
    return 'public, max-age=86400'


def build_upload_plan(distribution: Path) -> List[SiteUpload]:
    root = distribution.expanduser().resolve(strict=True)
    if not root.is_dir():
        raise SiteDeploymentError('站点构建产物不是目录：{}'.format(root))
    index = root / 'index.html'
    if not index.is_file():
        raise SiteDeploymentError('站点构建产物缺少 index.html')

    uploads: List[SiteUpload] = []
    for source in sorted(root.rglob('*')):
        if source.is_symlink():
            raise SiteDeploymentError('站点构建产物不能包含符号链接：{}'.format(source))
        if not source.is_file():
            continue
        object_key = source.relative_to(root).as_posix()
        if object_key == 'data/.gitignore':
            continue
        if object_key == 'data' or object_key.startswith(DATA_PREFIX):
            raise SiteDeploymentError('页面部署禁止包含 data/**：{}'.format(object_key))
        uploads.append(
            SiteUpload(
                source=source,
                object_key=object_key,
                headers={
                    'Cache-Control': _cache_control(object_key),
                    'Content-Type': _content_type(source),
                },
            )
        )

    return sorted(
        uploads,
        key=lambda upload: (upload.object_key == 'index.html', upload.object_key),
    )


def upload_site(bucket: Any, uploads: Iterable[SiteUpload]) -> int:
    uploaded_bytes = 0
    for upload in uploads:
        result = bucket.put_object_from_file(
            upload.object_key, str(upload.source), headers=dict(upload.headers)
        )
        status = int(getattr(result, 'status', 0))
        if not 200 <= status < 300:
            raise SiteDeploymentError(
                'OSS 上传失败：{}（HTTP {}）'.format(upload.object_key, status)
            )
        uploaded_bytes += upload.source.stat().st_size
    return uploaded_bytes


def _required_environment(name: str) -> str:
    value = os.environ.get(name, '').strip()
    if not value:
        raise SiteDeploymentError('缺少环境变量 {}'.format(name))
    return value


def _create_bucket(endpoint: str, bucket_name: str) -> Any:
    try:
        import oss2
    except ImportError as exc:
        raise SiteDeploymentError('缺少 oss2，请先安装部署依赖') from exc

    access_key_id = _required_environment('ALIBABA_CLOUD_ACCESS_KEY_ID')
    access_key_secret = _required_environment('ALIBABA_CLOUD_ACCESS_KEY_SECRET')
    security_token = os.environ.get('ALIBABA_CLOUD_SECURITY_TOKEN', '').strip()
    auth: Any
    if security_token:
        auth = oss2.StsAuth(access_key_id, access_key_secret, security_token)
    else:
        auth = oss2.Auth(access_key_id, access_key_secret)
    return oss2.Bucket(auth, endpoint, bucket_name)


def _parse_args(arguments: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='发布虚荣排行榜静态页面到 OSS')
    parser.add_argument('--dist', required=True, type=Path)
    parser.add_argument('--endpoint', required=True)
    parser.add_argument('--bucket', required=True)
    return parser.parse_args(arguments)


def main() -> None:
    arguments = _parse_args()
    uploads = build_upload_plan(arguments.dist)
    bucket = _create_bucket(arguments.endpoint, arguments.bucket)
    uploaded_bytes = upload_site(bucket, uploads)
    print(
        'site deployed: {} objects, {} bytes, index.html uploaded last'.format(
            len(uploads), uploaded_bytes
        )
    )


if __name__ == '__main__':
    main()
