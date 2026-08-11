"""把 BLREC 已识别对局的结算截图导入统一复核队列。"""

from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from PIL import Image

from . import config, db
from .extract import phash_image


def _validated(item: Mapping[str, Any]) -> Dict[str, Any]:
    match_id = int(item.get('match_id', 0))
    session_id = int(item.get('session_id', 0))
    part_id = int(item.get('part_id', 0))
    result_at_ms = int(item.get('result_at_ms', -1))
    relative_path = str(item.get('result_frame_path') or '')
    if match_id < 1 or session_id < 1 or part_id < 1 or result_at_ms < 0:
        raise ValueError('历史结算图元数据无效')
    if (
        not relative_path
        or relative_path.startswith('/')
        or '..' in relative_path.split('/')
        or not relative_path.lower().endswith('.png')
    ):
        raise ValueError('历史结算图路径无效')
    confidence = float(item.get('confidence', 0))
    if not 0 <= confidence <= 1:
        raise ValueError('历史结算识别置信度无效')
    try:
        hero_slot_count = int(item.get('hero_slot_count') or 0)
    except (TypeError, ValueError):
        hero_slot_count = 0
    if not 0 <= hero_slot_count <= 10:
        hero_slot_count = 0
    try:
        duration_seconds = int(item.get('duration_seconds') or 0)
    except (TypeError, ValueError):
        duration_seconds = 0
    if duration_seconds < 0:
        duration_seconds = 0
    try:
        started_at_ms = (
            None
            if item.get('started_at_ms') is None
            else max(0, int(item['started_at_ms']))
        )
    except (TypeError, ValueError):
        started_at_ms = None
    try:
        session_started_at = max(0, int(item.get('session_started_at') or 0))
    except (TypeError, ValueError):
        session_started_at = 0
    return {
        **item,
        'match_id': match_id,
        'session_id': session_id,
        'part_id': part_id,
        'part_index': int(item.get('part_index', 0)),
        'result_at_ms': result_at_ms,
        'result_frame_path': relative_path,
        'confidence': confidence,
        'hero_slot_count': hero_slot_count,
        'duration_seconds': duration_seconds,
        'started_at_ms': started_at_ms,
        'session_started_at': session_started_at,
    }


def _collapse_duplicate_items(
    conn: Any, items: Sequence[Dict[str, Any]]
) -> Tuple[List[Dict[str, Any]], int]:
    """同一局的重复 match 记录只导入一个结算代表图。"""
    existing = {
        str(row['source_id'])
        for row in conn.execute(
            "SELECT source_id FROM training_review_sources "
            "WHERE source_type = 'result_archive'"
        ).fetchall()
    }
    buckets: Dict[Tuple[int, int], List[Dict[str, Any]]] = {}
    for item in items:
        buckets.setdefault((int(item['session_id']), int(item['part_id'])), []).append(
            item
        )
    groups: List[List[Dict[str, Any]]] = []
    for candidates in buckets.values():
        local: List[Tuple[int, Optional[int], List[Dict[str, Any]]]] = []
        for item in sorted(candidates, key=lambda value: value['result_at_ms']):
            result_at = int(item['result_at_ms'])
            duration = int(item.get('duration_seconds') or 0)
            started_at = item.get('started_at_ms')
            estimated_start = (
                int(started_at)
                if started_at is not None
                else max(0, result_at - duration * 1_000) if duration > 0 else None
            )
            target = next(
                (
                    members
                    for anchor_result, anchor_start, members in local
                    if abs(anchor_result - result_at) <= 5_000
                    or (
                        anchor_start is not None
                        and estimated_start is not None
                        and abs(anchor_start - estimated_start) <= 90_000
                    )
                ),
                None,
            )
            if target is None:
                local.append((result_at, estimated_start, [item]))
            else:
                target.append(item)
        groups.extend(members for _result, _start, members in local)

    representatives = []
    for group in groups:
        representative = max(
            group,
            key=lambda item: (
                int('match:{}'.format(item['match_id']) in existing),
                float(item['confidence']),
                int(item['result_at_ms']),
                int(item['match_id']),
            ),
        )
        representatives.append(
            {
                **representative,
                'duplicate_match_ids': sorted(int(item['match_id']) for item in group),
            }
        )
    representatives.sort(
        key=lambda item: (item['session_id'], item['result_at_ms']), reverse=True
    )
    return representatives, len(items) - len(representatives)


def _mode_from_hero_slot_count(count: int) -> Optional[str]:
    if count == 6:
        return '3v3'
    if 7 <= count <= 10:
        return '5v5'
    return None


def _store_png_as_jpeg(content: bytes) -> Dict[str, Any]:
    with Image.open(io.BytesIO(content)) as source:
        source.load()
        image = source.convert('RGB')
    output = io.BytesIO()
    image.save(output, format='JPEG', quality=95)
    jpeg = output.getvalue()
    digest = hashlib.sha256(jpeg).hexdigest()
    fingerprint = phash_image(image)
    thumbnail = image.copy()
    config.FRAME_DIR.mkdir(parents=True, exist_ok=True)
    config.THUMB_DIR.mkdir(parents=True, exist_ok=True)
    frame_path = config.FRAME_DIR / f'{digest}.jpg'
    thumb_path = config.THUMB_DIR / f'{digest}.jpg'
    if not frame_path.is_file():
        frame_path.write_bytes(jpeg)
    if not thumb_path.is_file():
        thumbnail.thumbnail((config.THUMB_WIDTH, config.THUMB_WIDTH))
        thumbnail.save(thumb_path, quality=80)
    return {
        'width': image.width,
        'height': image.height,
        'sha256': digest,
        'phash': fingerprint,
        'frame_path': str(frame_path),
        'thumb_path': str(thumb_path),
    }


def detect_result_box(frame_path: Path) -> Optional[Dict[str, Any]]:
    """用当前旧结算检测器生成建议框；只作预标，不写人工真值。"""
    from . import inference

    prediction = inference.run_detect(
        'result-detector-v1', frame_path, conf_thr=0.25, imgsz=640
    )
    detections = prediction.get('detections') or []
    if not detections:
        return None
    detection = max(detections, key=lambda item: float(item.get('conf', 0)))
    x, y, w, h = (float(value) for value in detection['xywh_norm'])
    return {
        'type': 'result_panel',
        'x': x,
        'y': y,
        'w': w,
        'h': h,
        'confidence': float(detection.get('conf', 0)),
    }


def _sync_item(
    conn: Any,
    nas: Any,
    item: Mapping[str, Any],
    *,
    prefetched_content: Optional[bytes] = None,
    box_suggester: Optional[Callable[[Path], Optional[Dict[str, Any]]]] = None,
) -> Dict[str, bool]:
    source_id = 'match:{}'.format(item['match_id'])
    existing = conn.execute(
        'SELECT frame_id, metadata_json FROM training_review_sources '
        'WHERE source_type = ? AND source_id = ?',
        ('result_archive', source_id),
    ).fetchone()
    downloaded = False
    prior_metadata: Dict[str, Any] = {}
    if existing is not None:
        frame_id = int(existing['frame_id'])
        prior_metadata = json.loads(existing['metadata_json'] or '{}')
    else:
        content = (
            prefetched_content
            if prefetched_content is not None
            else nas.read_result_frame(item['result_frame_path'])
        )
        image = _store_png_as_jpeg(content)
        frame = conn.execute(
            'SELECT id FROM frames WHERE sha256 = ?', (image['sha256'],)
        ).fetchone()
        if frame is None:
            remote_path = 'result-archive://session/{}/part/{}'.format(
                item['session_id'], item['part_id']
            )
            video_id = db.upsert_video(
                conn,
                remote_path=remote_path,
                streamer=str(item.get('anchor_name') or ''),
                room_id=str(item.get('room_id') or ''),
                filename='session-{}-part-{}-results'.format(
                    item['session_id'], item['part_id']
                ),
                duration_seconds=0,
                size_bytes=0,
                part_index=item['part_index'] or None,
            )
            ids = db.add_frames(
                conn,
                video_id,
                [
                    {
                        'timestamp_ms': item['result_at_ms'],
                        'part_index': item['part_index'] or None,
                        'part_offset_ms': item['result_at_ms'],
                        'session_offset_ms': None,
                        'width': image['width'],
                        'height': image['height'],
                        'sha256': image['sha256'],
                        'phash': image['phash'],
                        'frame_path': image['frame_path'],
                        'thumb_path': image['thumb_path'],
                        'strategy': 'result_archive',
                        'model_source': 'blrec-recognized-match',
                        'model_confidence': item['confidence'],
                    }
                ],
            )
            if not ids:
                raise RuntimeError('历史结算图未能写入本地数据库')
            frame_id = ids[0]
        else:
            frame_id = int(frame['id'])
        downloaded = True
    metadata = {**item, 'source': 'blrec_recognized_match'}
    suggested_boxes = prior_metadata.get('suggested_boxes') or []
    attempted = bool(prior_metadata.get('box_suggestion_attempted'))
    if not attempted and box_suggester is not None:
        frame_row = conn.execute(
            'SELECT frame_path FROM frames WHERE id = ?', (frame_id,)
        ).fetchone()
        try:
            suggested_box = box_suggester(Path(frame_row['frame_path']))
            if suggested_box is not None:
                suggested_boxes = [suggested_box]
        except Exception as error:  # noqa: BLE001
            metadata['box_suggestion_error'] = str(error)[:200]
        attempted = True
    metadata['box_suggestion_attempted'] = attempted
    metadata['suggested_boxes'] = suggested_boxes
    confidence = item['confidence']
    suggestions = {
        'match_flow': {'label': 'match_flow', 'confidence': confidence},
        'hero_select': {'label': 'not_select', 'confidence': confidence},
        'result_panel': {'label': 'result_panel', 'confidence': confidence},
    }
    inferred_mode = _mode_from_hero_slot_count(item['hero_slot_count'])
    if inferred_mode is not None:
        suggestions['match_mode'] = {'label': inferred_mode, 'confidence': confidence}
    inserted = db.add_training_review_source(
        conn,
        frame_id=frame_id,
        source_type='result_archive',
        source_id=source_id,
        suggestions=suggestions,
        metadata=metadata,
        source_created_at=item['session_started_at'],
    )
    return {
        'inserted': inserted,
        'downloaded': downloaded,
        'box_suggested': bool(suggested_boxes),
    }


def sync_result_archive(
    conn: Any,
    nas: Any,
    items: Sequence[Mapping[str, Any]],
    *,
    maximum: int = 10_000,
    progress: Optional[Callable[[Dict[str, Any]], None]] = None,
    box_suggester: Optional[Callable[[Path], Optional[Dict[str, Any]]]] = None,
) -> Dict[str, Any]:
    """导入历史结算截图；系统识别只写预标，仍需人工确认与画框。"""
    ordered = sorted(
        items,
        key=lambda item: (
            int(item.get('session_id', 0)),
            int(item.get('result_at_ms', 0)),
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
        'box_suggested': 0,
        'duplicates_skipped': 0,
    }
    validated = []
    for raw in ordered:
        try:
            validated.append(_validated(raw))
        except Exception as error:  # noqa: BLE001
            result['failed'] += 1
            result['last_error'] = str(error)[:200]
            result['processed'] += 1
    validated, duplicates_skipped = _collapse_duplicate_items(conn, validated)
    result['duplicates_skipped'] = duplicates_skipped
    result['processed'] += duplicates_skipped
    for start in range(0, len(validated), 16):
        batch = validated[start : start + 16]
        missing_paths = []
        for item in batch:
            source_id = 'match:{}'.format(item['match_id'])
            exists = conn.execute(
                'SELECT 1 FROM training_review_sources '
                'WHERE source_type = ? AND source_id = ?',
                ('result_archive', source_id),
            ).fetchone()
            if exists is None:
                missing_paths.append(str(item['result_frame_path']))
        prefetched: Dict[str, bytes] = {}
        if missing_paths and hasattr(nas, 'read_result_frames'):
            try:
                prefetched = nas.read_result_frames(missing_paths)
            except Exception:  # noqa: BLE001
                # 批量通道失败时逐张回退，不能让一张坏图拖掉整批。
                prefetched = {}
        for item in batch:
            try:
                path = str(item['result_frame_path'])
                synced = _sync_item(
                    conn,
                    nas,
                    item,
                    prefetched_content=prefetched.get(path),
                    box_suggester=box_suggester,
                )
                result['inserted' if synced['inserted'] else 'updated'] += 1
                if synced['downloaded']:
                    result['downloaded'] += 1
                if synced['box_suggested']:
                    result['box_suggested'] += 1
            except Exception as error:  # noqa: BLE001
                result['failed'] += 1
                result['last_error'] = str(error)[:200]
            result['processed'] += 1
            if progress is not None:
                progress(dict(result))
    return result
