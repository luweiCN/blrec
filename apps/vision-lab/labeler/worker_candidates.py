"""把 MacBook worker 在 NAS 上留下的候选图导入本地专项复核队列。"""

from __future__ import annotations

import hashlib
import io
from typing import Any, Callable, Dict, Mapping, Optional, Sequence

from PIL import Image

from . import config, db
from .extract import phash_image
from .nas import NasClient

_LABELS_BY_TASK = {
    'bp_review': {'bp_3v3', 'bp_aram', 'bp_5v5', 'not_bp'},
    'key_screen_review': {'result_page', 'scoreboard', 'other'},
}


def _confidence(item: Mapping[str, Any], name: str) -> float:
    value = float(item[name])
    if not 0 <= value <= 1:
        raise ValueError(f'{name} 必须在 0 到 1 之间')
    return value


def _validate(item: Mapping[str, Any]) -> Dict[str, Any]:
    if int(item.get('schema_version', 0)) != 1:
        raise ValueError('不支持的 worker 候选格式')
    task = str(item.get('task', ''))
    if task not in _LABELS_BY_TASK:
        raise ValueError(f'不支持的 worker 候选任务: {task}')
    label = str(item.get('suggested_label', ''))
    if label not in _LABELS_BY_TASK[task]:
        raise ValueError(f'未知 worker 建议标签: {label}')
    sha256 = str(item.get('image_sha256', '')).lower()
    if len(sha256) != 64 or any(c not in '0123456789abcdef' for c in sha256):
        raise ValueError('worker 候选 SHA-256 无效')
    at_ms = int(item.get('at_ms', -1))
    if at_ms < 0:
        raise ValueError('worker 候选时间点无效')
    return {
        **item,
        'task': task,
        'suggested_label': label,
        'image_sha256': sha256,
        'at_ms': at_ms,
        'part_id': int(item.get('part_id', 0)),
        'part_index': int(item.get('part_index', 0)),
        'suggestion_confidence': _confidence(item, 'suggestion_confidence'),
        'stage_confidence': _confidence(item, 'stage_confidence'),
        'mode_confidence': _confidence(item, 'mode_confidence'),
    }


def _store_image(content: bytes, sha256: str) -> Dict[str, Any]:
    if hashlib.sha256(content).hexdigest() != sha256:
        raise ValueError('worker 候选图片与说明中的 SHA-256 不一致')
    with Image.open(io.BytesIO(content)) as image:
        image.load()
        width, height = image.size
        fingerprint = phash_image(image)
        thumbnail = image.copy()
    config.FRAME_DIR.mkdir(parents=True, exist_ok=True)
    config.THUMB_DIR.mkdir(parents=True, exist_ok=True)
    frame_path = config.FRAME_DIR / f'{sha256}.jpg'
    thumb_path = config.THUMB_DIR / f'{sha256}.jpg'
    if not frame_path.is_file():
        frame_path.write_bytes(content)
    if not thumb_path.is_file():
        thumbnail.thumbnail((config.THUMB_WIDTH, config.THUMB_WIDTH))
        thumbnail.convert('RGB').save(thumb_path, quality=80)
    return {
        'width': width,
        'height': height,
        'phash': fingerprint,
        'frame_path': str(frame_path),
        'thumb_path': str(thumb_path),
    }


def sync_worker_candidates(
    conn: Any,
    nas: NasClient,
    items: Sequence[Mapping[str, Any]],
    *,
    maximum: int = 10_000,
    progress: Optional[Callable[[Dict[str, Any]], None]] = None,
) -> Dict[str, Any]:
    """导入候选并写入模型预标；已人工确认的项目不会退回待确认。"""
    ordered = sorted(
        items,
        key=lambda item: (
            int(item.get('created_at', 0)),
            int(item.get('part_id', 0)),
            int(item.get('at_ms', 0)),
        ),
        reverse=True,
    )[:maximum]
    result = {
        'total': len(ordered),
        'processed': 0,
        'inserted': 0,
        'updated': 0,
        'downloaded': 0,
        'failed': 0,
        'last_error': '',
        'by_task': {},
    }
    for raw_item in ordered:
        try:
            item = _validate(raw_item)
            frame = conn.execute(
                'SELECT id FROM frames WHERE sha256 = ?', (item['image_sha256'],)
            ).fetchone()
            if frame is None:
                remote_path = 'worker-candidate://session/{}/part/{}'.format(
                    int(item.get('session_id', 0)), item['part_id']
                )
                video_id = db.upsert_video(
                    conn,
                    remote_path=remote_path,
                    streamer=str(
                        item.get('streamer') or item.get('session_title') or ''
                    ),
                    room_id=str(item.get('room_id') or ''),
                    filename=str(item.get('filename') or f"part-{item['part_id']}"),
                    duration_seconds=0,
                    size_bytes=0,
                    part_index=item['part_index'] or None,
                )
                frame = conn.execute(
                    'SELECT id FROM frames WHERE video_id = ? AND timestamp_ms = ?',
                    (video_id, item['at_ms']),
                ).fetchone()
                if frame is None:
                    image = nas.read_training_candidate(str(item['image_path']))
                    image_info = _store_image(image, item['image_sha256'])
                    ids = db.add_frames(
                        conn,
                        video_id,
                        [
                            {
                                'timestamp_ms': item['at_ms'],
                                'part_index': item['part_index'] or None,
                                'part_offset_ms': item['at_ms'],
                                'session_offset_ms': None,
                                'width': image_info['width'],
                                'height': image_info['height'],
                                'sha256': item['image_sha256'],
                                'phash': image_info['phash'],
                                'frame_path': image_info['frame_path'],
                                'thumb_path': image_info['thumb_path'],
                                'strategy': 'worker_candidate',
                                'model_source': str(item.get('model_version', '')),
                                'model_confidence': item['suggestion_confidence'],
                            }
                        ],
                    )
                    if not ids:
                        raise RuntimeError('worker 候选帧未能写入本地数据库')
                    frame_id = ids[0]
                    result['downloaded'] += 1
                else:
                    frame_id = int(frame['id'])
            else:
                frame_id = int(frame['id'])

            if item['task'] == 'bp_review':
                stage_class = str(item.get('stage_class', 'unknown'))
                was_inserted = db.upsert_bp_review_item(
                    conn,
                    frame_id=frame_id,
                    model_version=str(item.get('model_version', 'multi-v2')),
                    suggested_label=item['suggested_label'],
                    suggestion_confidence=item['suggestion_confidence'],
                    stage_class=stage_class,
                    stage_confidence=item['stage_confidence'],
                    pre_match_confidence=(
                        item['stage_confidence']
                        if stage_class == 'pre_match' else 0.0
                    ),
                    mode_class=str(item.get('mode_class', 'unknown')),
                    mode_confidence=item['mode_confidence'],
                    mode_margin=0.0,
                    selection_reason=str(
                        item.get('selection_reason', 'worker 候选')),
                    priority=100 + (1 - item['suggestion_confidence']) * 10,
                    raw_prediction={**item, 'source': 'macbook_worker'},
                )
            else:
                was_inserted = db.upsert_key_screen_review_item(
                    conn,
                    frame_id=frame_id,
                    model_version=str(item.get('model_version', 'multi-v2')),
                    suggested_label=item['suggested_label'],
                    suggestion_confidence=item['suggestion_confidence'],
                    selection_reason=str(
                        item.get('selection_reason', 'worker 关键画面候选')),
                    priority=100 + (1 - item['suggestion_confidence']) * 10,
                    raw_prediction={**item, 'source': 'macbook_worker'},
                )
            result['inserted' if was_inserted else 'updated'] += 1
            task_counts = result['by_task'].setdefault(
                item['task'], {'inserted': 0, 'updated': 0})
            task_counts['inserted' if was_inserted else 'updated'] += 1
        except Exception as error:  # noqa: BLE001
            result['failed'] += 1
            result['last_error'] = str(error)[:200]
        result['processed'] += 1
        if progress is not None:
            progress(dict(result))
    return result
