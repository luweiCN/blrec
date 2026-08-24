"""在 Vision Worker 本地按 NAS 清单物化一次训练数据集。"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from statistics import median
from typing import Any, Callable, Dict, Iterable, List, Tuple

from PIL import Image

ImageFetcher = Callable[[int, Path], None]
ProgressCallback = Callable[[int, int], None]

CLASSIFICATION_TASKS = {
    'match_flow',
    'match_mode',
    'hero_select',
    'hero_identity',
    'player_position',
    'afk_status',
}
DETECTION_TASKS = {'result_detector', 'hero_avatar_detector'}


def load_manifest(path: Path) -> List[Dict[str, Any]]:
    samples = []
    with path.open(encoding='utf-8') as handle:
        for line in handle:
            if not line.strip():
                continue
            sample = json.loads(line)
            if not isinstance(sample, dict):
                raise ValueError('数据集清单行必须是 JSON 对象')
            samples.append(sample)
    if not samples:
        raise ValueError('数据集清单为空')
    return samples


def materialize_dataset(
    *,
    task_id: str,
    manifest_path: Path,
    output_dir: Path,
    fetch_image: ImageFetcher,
    progress: ProgressCallback | None = None,
    frame_cache_dir: Path | None = None,
    download_workers: int = 1,
) -> Dict[str, Any]:
    if task_id not in CLASSIFICATION_TASKS | DETECTION_TASKS:
        raise ValueError(f'尚不支持远程物化的训练任务: {task_id}')
    samples = load_manifest(manifest_path)
    sources = output_dir / 'sources'
    sources.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)

    unique_samples: Dict[int, Dict[str, Any]] = {}
    for sample in samples:
        frame_id = int(sample['frame_id'])
        unique_samples.setdefault(frame_id, sample)
    total = len(unique_samples)

    def prepare_source(sample: Dict[str, Any]) -> Tuple[int, Path]:
        frame_id = int(sample['frame_id'])
        sha256 = str(sample.get('sha256') or '')
        source = sources / f'{frame_id}-{sha256[:16]}.jpg'
        if source.is_file() and source.stat().st_size > 0:
            if frame_cache_dir is not None:
                cached = _cached_frame_path(frame_cache_dir, frame_id, sha256)
                if not cached.is_file():
                    _reuse_file(source, cached)
            return frame_id, source

        if frame_cache_dir is not None:
            cached = _cached_frame_path(frame_cache_dir, frame_id, sha256)
            if not cached.is_file() or cached.stat().st_size <= 0:
                _download_frame(fetch_image, frame_id, sha256, cached)
            _reuse_file(cached, source)
        else:
            _download_frame(fetch_image, frame_id, sha256, source)
        return frame_id, source

    frame_sources: Dict[int, Path] = {}
    worker_count = max(1, min(int(download_workers), total))
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        futures = [
            executor.submit(prepare_source, sample)
            for sample in unique_samples.values()
        ]
        for future in as_completed(futures):
            frame_id, source = future.result()
            frame_sources[frame_id] = source
            if progress is not None:
                progress(len(frame_sources), total)

    if task_id in CLASSIFICATION_TASKS:
        replicas = _materialize_classification(
            task_id=task_id,
            samples=samples,
            frame_sources=frame_sources,
            output_dir=output_dir,
        )
    else:
        replicas = 0
        _materialize_detection(
            task_id=task_id,
            samples=samples,
            frame_sources=frame_sources,
            output_dir=output_dir,
        )
    shutil.copy2(manifest_path, output_dir / 'samples.jsonl')
    (output_dir / '.materialized.json').write_text(
        json.dumps(
            {
                'task_id': task_id,
                'samples': len(samples),
                'frames': len(frame_sources),
                'training_balance_replicas': replicas,
                'manifest_sha256': _sha256(manifest_path),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding='utf-8',
    )
    return {
        'samples': len(samples),
        'frames': len(frame_sources),
        'training_balance_replicas': replicas,
    }


def _materialize_classification(
    *,
    task_id: str,
    samples: List[Dict[str, Any]],
    frame_sources: Dict[int, Path],
    output_dir: Path,
) -> int:
    destinations: Dict[str, List[Path]] = {}
    opened: Dict[int, Image.Image] = {}
    try:
        for sample in samples:
            split = _safe_part(sample.get('split'), {'train', 'val', 'test'})
            label = _safe_label(sample.get('label'))
            destination_dir = output_dir / 'images' / split / label
            destination_dir.mkdir(parents=True, exist_ok=True)
            destination = destination_dir / f"{_safe_sample_id(sample)}.jpg"
            source = frame_sources[int(sample['frame_id'])]
            if task_id in {'hero_identity', 'afk_status'}:
                frame_id = int(sample['frame_id'])
                image = opened.get(frame_id)
                if image is None:
                    image = Image.open(source).convert('RGB')
                    opened[frame_id] = image
                image.crop(_pixel_crop(image, sample.get('crop'))).save(
                    destination, format='JPEG', quality=95
                )
            elif not destination.exists():
                shutil.copy2(source, destination)
            if split == 'train':
                destinations.setdefault(label, []).append(destination)
    finally:
        for image in opened.values():
            image.close()
    if task_id not in {'hero_identity', 'player_position', 'afk_status'}:
        return 0
    return _balance_training_classes(
        destinations, target_limit=None if task_id == 'afk_status' else 100
    )


def _materialize_detection(
    *,
    task_id: str,
    samples: List[Dict[str, Any]],
    frame_sources: Dict[int, Path],
    output_dir: Path,
) -> None:
    class_name = 'result_panel' if task_id == 'result_detector' else 'hero_avatar'
    for split in ('train', 'val', 'test'):
        (output_dir / 'images' / split).mkdir(parents=True, exist_ok=True)
        (output_dir / 'labels' / split).mkdir(parents=True, exist_ok=True)
    for sample in samples:
        split = _safe_part(sample.get('split'), {'train', 'val', 'test'})
        sample_id = _safe_sample_id(sample)
        source = frame_sources[int(sample['frame_id'])]
        destination = output_dir / 'images' / split / f'{sample_id}.jpg'
        if not destination.exists():
            shutil.copy2(source, destination)
        boxes: Iterable[Dict[str, Any]]
        if task_id == 'result_detector':
            result_box = (sample.get('boxes') or {}).get('result_panel')
            boxes = [] if result_box is None else [result_box]
        else:
            boxes = sample.get('avatar_boxes') or []
        labels = []
        for box in boxes:
            x = float(box['x']) + float(box['w']) / 2
            y = float(box['y']) + float(box['h']) / 2
            labels.append(
                '0 {:.6f} {:.6f} {:.6f} {:.6f}'.format(
                    x, y, float(box['w']), float(box['h'])
                )
            )
        (output_dir / 'labels' / split / f'{sample_id}.txt').write_text(
            ('\n'.join(labels) + '\n') if labels else '', encoding='utf-8'
        )
    (output_dir / 'data.yaml').write_text(
        f'path: {output_dir}\ntrain: images/train\nval: images/val\n'
        f"test: images/test\nnc: 1\nnames: ['{class_name}']\n",
        encoding='utf-8',
    )


def _pixel_crop(image: Image.Image, value: Any) -> Tuple[int, int, int, int]:
    if not isinstance(value, dict):
        raise ValueError('英雄头像样本缺少 crop')
    left = max(0, min(image.width - 1, round(float(value['x']) * image.width)))
    top = max(0, min(image.height - 1, round(float(value['y']) * image.height)))
    right = max(
        left + 1,
        min(image.width, round((float(value['x']) + float(value['w'])) * image.width)),
    )
    bottom = max(
        top + 1,
        min(
            image.height, round((float(value['y']) + float(value['h'])) * image.height)
        ),
    )
    return left, top, right, bottom


def _balance_training_classes(
    paths_by_label: Dict[str, List[Path]], *, target_limit: int | None = 100
) -> int:
    populated = [len(paths) for paths in paths_by_label.values() if paths]
    target = max(1, round(median(populated))) if populated else 0
    if target_limit is not None:
        target = min(target_limit, target)
    replicas = 0
    for paths in paths_by_label.values():
        if not paths:
            continue
        original_count = len(paths)
        for index in range(original_count, target):
            source = paths[index % original_count]
            destination = source.with_name(
                '{}-balance-{:04d}.jpg'.format(source.stem, index - original_count + 1)
            )
            shutil.copy2(source, destination)
            replicas += 1
    return replicas


def _safe_sample_id(sample: Dict[str, Any]) -> str:
    return _safe_part(sample.get('sample_id'), None)


def _safe_label(value: Any) -> str:
    return _safe_part(value, None)


def _safe_part(value: Any, allowed: set[str] | None) -> str:
    normalized = str(value or '').strip()
    if (
        not normalized
        or normalized in {'.', '..'}
        or '/' in normalized
        or '\\' in normalized
    ):
        raise ValueError(f'不安全的数据集路径片段: {normalized!r}')
    if allowed is not None and normalized not in allowed:
        raise ValueError(f'未知数据集路径片段: {normalized!r}')
    return normalized


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _cached_frame_path(cache_dir: Path, frame_id: int, sha256: str) -> Path:
    return cache_dir / f'{frame_id}-{sha256[:16]}.jpg'


def _download_frame(
    fetch_image: ImageFetcher, frame_id: int, sha256: str, destination: Path
) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(
        f'{destination.name}.download-{os.getpid()}-{threading.get_ident()}'
    )
    temporary.unlink(missing_ok=True)
    try:
        fetch_image(frame_id, temporary)
        if not temporary.is_file() or temporary.stat().st_size <= 0:
            raise RuntimeError(f'帧 {frame_id} 下载结果为空')
        if len(sha256) == 64 and _sha256(temporary) != sha256:
            raise RuntimeError(f'帧 {frame_id} 的 SHA-256 校验失败')
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)


def _reuse_file(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.is_file() and destination.stat().st_size > 0:
        return
    temporary = destination.with_name(
        f'{destination.name}.link-{os.getpid()}-{threading.get_ident()}'
    )
    temporary.unlink(missing_ok=True)
    try:
        try:
            os.link(source, temporary)
        except OSError:
            shutil.copy2(source, temporary)
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
