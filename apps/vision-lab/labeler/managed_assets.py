"""解析本地或 NAS media 管理的不可变 Vision Lab 资产。"""

from __future__ import annotations

import re
import shutil
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Tuple
from uuid import uuid4

from . import config

_SAFE_IDENTIFIER = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,191}$')


def frame_available(path: str | Path) -> bool:
    """本机存在，或已交由 NAS media 管理的帧都可进入远程快照。"""

    return Path(path).is_file() or bool(config.MEDIA_SERVER_URL)


def resolve_dataset_manifest(version_id: str, path: str | Path) -> Path:
    source = Path(path)
    if source.is_file():
        return source
    safe_id = _safe_identifier(version_id, '数据集版本')
    destination = (
        config.WORK_DIR / 'managed-assets' / 'datasets' / safe_id / 'samples.jsonl'
    )
    return _download_once(
        '/api/vision-workers/datasets/{}/manifest'.format(
            urllib.parse.quote(safe_id, safe='')
        ),
        destination,
    )


def resolve_model_run(run_id: str, artifact_path: str | Path) -> Tuple[Path, Path]:
    source = Path(artifact_path)
    metadata = source.with_suffix('.json')
    if source.is_file() and metadata.is_file():
        return source, metadata
    safe_id = _safe_identifier(run_id, '训练记录')
    root = config.WORK_DIR / 'managed-assets' / 'model-runs' / safe_id
    artifact = _download_once(
        '/api/vision-workers/model-runs/{}/artifact'.format(
            urllib.parse.quote(safe_id, safe='')
        ),
        root / 'model.onnx',
    )
    metadata = _download_once(
        '/api/vision-workers/model-runs/{}/metadata'.format(
            urllib.parse.quote(safe_id, safe='')
        ),
        root / 'model.json',
    )
    return artifact, metadata


def resolve_model_package_archive(package_id: str) -> Path:
    safe_id = _safe_identifier(package_id, '模型包')
    destination = (
        config.WORK_DIR / 'managed-assets' / 'model-packages' / f'{safe_id}.zip'
    )
    return _download_once(
        '/api/vision-workers/model-packages/{}/archive'.format(
            urllib.parse.quote(safe_id, safe='')
        ),
        destination,
    )


def _safe_identifier(value: str, name: str) -> str:
    normalized = str(value).strip()
    if not _SAFE_IDENTIFIER.fullmatch(normalized):
        raise ValueError(f'{name}标识无效')
    return normalized


def _download_once(path: str, destination: Path) -> Path:
    if destination.is_file() and destination.stat().st_size > 0:
        return destination
    if not config.MEDIA_SERVER_URL:
        raise FileNotFoundError(destination)
    if not config.VISION_WORKER_TOKEN:
        raise RuntimeError('尚未配置 Vision Worker token，无法读取 NAS 受管资产')
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f'.{destination.name}.{uuid4().hex}.part')
    request = urllib.request.Request(
        config.MEDIA_SERVER_URL + path,
        headers={'Authorization': f'Bearer {config.VISION_WORKER_TOKEN}'},
        method='GET',
    )
    try:
        with urllib.request.urlopen(request, timeout=300) as response:
            with temporary.open('wb') as output:
                shutil.copyfileobj(response, output, length=1024 * 1024)
        if temporary.stat().st_size <= 0:
            raise RuntimeError('NAS 返回了空文件')
        temporary.replace(destination)
    except urllib.error.HTTPError as error:
        raise FileNotFoundError(destination) from error
    except (OSError, urllib.error.URLError) as error:
        raise RuntimeError('NAS 受管资产暂时无法读取') from error
    finally:
        temporary.unlink(missing_ok=True)
    return destination
