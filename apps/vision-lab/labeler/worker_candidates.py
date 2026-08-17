"""把 MacBook worker 在 NAS 上留下的候选图导入本地专项复核队列。"""

from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Optional, Sequence

from PIL import Image

from . import config, db, hero_review
from .extract import phash_image
from .nas import NasClient

_LABELS_BY_TASK = {
    'screen_state': {
        'not_vainglory',
        'out_of_match',
        'pre_match',
        'in_match',
        'talent_select',
        'post_match',
        'transition',
    },
    'bp_review': {'bp_3v3', 'bp_aram', 'bp_5v5', 'not_bp'},
    'key_screen_review': {'result_page', 'scoreboard', 'other'},
    'result_detector': {'result_panel', 'no_result_panel'},
    'mode_gate': {'blocked_gate', 'open_entrance', 'no_evidence'},
}
_REMOTE_SOURCE_TYPES = {'worker', 'manual_correction'}


def _confidence(item: Mapping[str, Any], name: str) -> float:
    value = float(item.get(name, 0))
    if not 0 <= value <= 1:
        raise ValueError(f'{name} 必须在 0 到 1 之间')
    return value


def _validate(item: Mapping[str, Any]) -> Dict[str, Any]:
    schema_version = int(item.get('schema_version', 0))
    if schema_version not in (1, 2, 3):
        raise ValueError('不支持的 worker 候选格式')
    task = str(item.get('task', ''))
    if schema_version == 3:
        if task != 'unified_review':
            raise ValueError(f'不支持的 worker 候选任务: {task}')
        suggestions = item.get('suggestions')
        if not isinstance(suggestions, dict) or not suggestions:
            raise ValueError('统一 worker 候选缺少模型建议')
        normalized_suggestions = db.validate_training_suggestions(suggestions)
        sha256 = str(item.get('image_sha256', '')).lower()
        if len(sha256) != 64 or any(char not in '0123456789abcdef' for char in sha256):
            raise ValueError('worker 候选 SHA-256 无效')
        at_ms = int(item.get('at_ms', -1))
        if at_ms < 0:
            raise ValueError('worker 候选时间点无效')
        source_id = str(item.get('source_id') or '')
        if not source_id or len(source_id) > 300:
            raise ValueError('worker 候选 source_id 无效')
        confidence = max(
            float(value['confidence']) for value in normalized_suggestions.values()
        )
        return {
            **item,
            'schema_version': schema_version,
            'source_id': source_id,
            'task': task,
            'suggestions': normalized_suggestions,
            'image_sha256': sha256,
            'at_ms': at_ms,
            'part_id': int(item.get('part_id', 0)),
            'part_index': int(item.get('part_index', 0)),
            'suggestion_confidence': confidence,
            'stage_confidence': 0.0,
            'mode_confidence': 0.0,
            'suggested_boxes': list(item.get('suggested_boxes') or []),
        }
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
    source_id = str(item.get('source_id') or '')
    if not source_id or len(source_id) > 300:
        raise ValueError('worker 候选 source_id 无效')
    return {
        **item,
        'schema_version': schema_version,
        'source_id': source_id,
        'task': task,
        'suggested_label': label,
        'image_sha256': sha256,
        'at_ms': at_ms,
        'part_id': int(item.get('part_id', 0)),
        'part_index': int(item.get('part_index', 0)),
        'suggestion_confidence': _confidence(item, 'suggestion_confidence'),
        'stage_confidence': _confidence(item, 'stage_confidence'),
        'mode_confidence': _confidence(item, 'mode_confidence'),
        'suggested_boxes': list(item.get('suggested_boxes') or []),
    }


def _legacy_suggestions(item: Mapping[str, Any]) -> Dict[str, Dict[str, Any]]:
    task = str(item['task'])
    label = str(item['suggested_label'])
    confidence = float(item['suggestion_confidence'])
    mode = str(item.get('mode_class') or '')
    result: Dict[str, Dict[str, Any]] = {}
    if task == 'screen_state':
        flow = (
            'match_flow'
            if label in ('in_match', 'talent_select', 'post_match')
            else 'unreadable' if label == 'transition' else 'not_match_flow'
        )
        result['match_flow'] = {'label': flow, 'confidence': confidence}
        if flow == 'match_flow' and mode in ('3v3', 'aram', '5v5'):
            result['match_mode'] = {
                'label': mode,
                'confidence': float(item.get('mode_confidence', 0)),
            }
    elif task == 'bp_review':
        select = {
            'bp_3v3': 'select_3v3',
            'bp_aram': 'select_aram',
            'bp_5v5': 'select_5v5',
            'not_bp': 'not_select',
        }[label]
        result['hero_select'] = {'label': select, 'confidence': confidence}
        if label != 'not_bp':
            result['match_flow'] = {'label': 'not_match_flow', 'confidence': confidence}
            result['result_panel'] = {
                'label': 'no_result_panel',
                'confidence': confidence,
            }
    elif task == 'key_screen_review':
        if label == 'result_page':
            result['match_flow'] = {'label': 'match_flow', 'confidence': confidence}
            result['result_panel'] = {'label': 'result_panel', 'confidence': confidence}
        elif label == 'scoreboard':
            result['match_flow'] = {'label': 'match_flow', 'confidence': confidence}
            result['match_mode'] = {'label': 'unreadable', 'confidence': confidence}
            result['hero_select'] = {'label': 'not_select', 'confidence': confidence}
            result['result_panel'] = {
                'label': 'no_result_panel',
                'confidence': confidence,
            }
        else:
            result['result_panel'] = {
                'label': 'no_result_panel',
                'confidence': confidence,
            }
    elif task == 'result_detector':
        result['result_panel'] = {'label': label, 'confidence': confidence}
    elif task == 'mode_gate':
        if mode in ('3v3', 'aram', '5v5'):
            result['match_flow'] = {'label': 'match_flow', 'confidence': confidence}
            result['match_mode'] = {'label': mode, 'confidence': confidence}
            result['hero_select'] = {'label': 'not_select', 'confidence': confidence}
            result['result_panel'] = {
                'label': 'no_result_panel',
                'confidence': confidence,
            }
    return result


def _created_at(item: Mapping[str, Any]) -> int:
    try:
        return int(item.get('created_at', 0))
    except (TypeError, ValueError):
        return 0


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


def _reference_image(
    path: Path, sha256: str, *, width: int = 0, height: int = 0
) -> Dict[str, Any]:
    if path.stem.lower() != sha256:
        raise ValueError('worker 候选对象路径与说明中的 SHA-256 不一致')
    return {
        'width': max(0, int(width)),
        'height': max(0, int(height)),
        'phash': '',
        'frame_path': str(path),
        'thumb_path': '',
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
            _created_at(item),
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
                    local_resolver = getattr(nas, 'training_candidate_local_path', None)
                    local_path = (
                        local_resolver(str(item['image_path']))
                        if callable(local_resolver)
                        else None
                    )
                    if local_path is None:
                        image = nas.read_training_candidate(str(item['image_path']))
                        image_info = _store_image(image, item['image_sha256'])
                        result['downloaded'] += 1
                    else:
                        image_info = _reference_image(
                            local_path,
                            item['image_sha256'],
                            width=int(item.get('image_width', 0)),
                            height=int(item.get('image_height', 0)),
                        )
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
                                'model_source': str(
                                    item.get('model_version') or 'worker-unified-v3'
                                ),
                                'model_confidence': item['suggestion_confidence'],
                            }
                        ],
                    )
                    if not ids:
                        raise RuntimeError('worker 候选帧未能写入本地数据库')
                    frame_id = ids[0]
                else:
                    frame_id = int(frame['id'])
            else:
                frame_id = int(frame['id'])

            if item['schema_version'] == 3:
                source_type = str(item.get('source_type') or 'worker')
                if source_type not in _REMOTE_SOURCE_TYPES:
                    source_type = 'worker'
                was_inserted = db.add_training_review_source(
                    conn,
                    frame_id=frame_id,
                    source_type=source_type,
                    source_id=item['source_id'],
                    image_path=str(item['image_path']),
                    suggestions=dict(item['suggestions']),
                    metadata={**item, 'source': source_type},
                    source_created_at=_created_at(item),
                )
                result['inserted' if was_inserted else 'updated'] += 1
                task_counts = result['by_task'].setdefault(
                    item['task'], {'inserted': 0, 'updated': 0}
                )
                task_counts['inserted' if was_inserted else 'updated'] += 1
                result['processed'] += 1
                if progress is not None:
                    progress(dict(result))
                continue

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
                        item['stage_confidence'] if stage_class == 'pre_match' else 0.0
                    ),
                    mode_class=str(item.get('mode_class', 'unknown')),
                    mode_confidence=item['mode_confidence'],
                    mode_margin=0.0,
                    selection_reason=str(item.get('selection_reason', 'worker 候选')),
                    priority=100 + (1 - item['suggestion_confidence']) * 10,
                    raw_prediction={**item, 'source': 'macbook_worker'},
                )
            elif item['task'] == 'key_screen_review':
                was_inserted = db.upsert_key_screen_review_item(
                    conn,
                    frame_id=frame_id,
                    model_version=str(item.get('model_version', 'multi-v2')),
                    suggested_label=item['suggested_label'],
                    suggestion_confidence=item['suggestion_confidence'],
                    selection_reason=str(
                        item.get('selection_reason', 'worker 关键画面候选')
                    ),
                    priority=100 + (1 - item['suggestion_confidence']) * 10,
                    raw_prediction={**item, 'source': 'macbook_worker'},
                )
            else:
                was_inserted = (
                    conn.execute(
                        'SELECT 1 FROM worker_candidate_items WHERE source_id = ?',
                        (item['source_id'],),
                    ).fetchone()
                    is None
                )
            generic_inserted = db.upsert_worker_candidate(
                conn,
                source_id=item['source_id'],
                frame_id=frame_id,
                task=item['task'],
                schema_version=item['schema_version'],
                image_path=str(item['image_path']),
                image_sha256=item['image_sha256'],
                suggested_label=item['suggested_label'],
                suggestion_confidence=item['suggestion_confidence'],
                suggested_boxes=item['suggested_boxes'],
                raw_metadata={**item, 'source': 'macbook_worker'},
                candidate_created_at=_created_at(item),
            )
            was_inserted = generic_inserted
            db.add_training_review_source(
                conn,
                frame_id=frame_id,
                source_type='worker',
                source_id='{}:{}'.format(item['source_id'], item['task']),
                image_path=str(item['image_path']),
                suggestions=_legacy_suggestions(item),
                metadata={**item, 'source': 'macbook_worker'},
                source_created_at=_created_at(item),
            )
            if item['task'] in ('bp_review', 'key_screen_review'):
                table = (
                    'bp_review_items'
                    if item['task'] == 'bp_review'
                    else 'key_screen_review_items'
                )
                prior_review = conn.execute(
                    f'SELECT review_status, confirmed_label, visual_condition '
                    f'FROM {table} WHERE frame_id = ?',
                    (frame_id,),
                ).fetchone()
                candidate = db.get_worker_candidate_by_source(conn, item['source_id'])
                if (
                    generic_inserted
                    and prior_review is not None
                    and prior_review['review_status'] in ('confirmed', 'skipped')
                    and candidate is not None
                ):
                    db.review_worker_candidate(
                        conn,
                        candidate_id=int(candidate['id']),
                        label=prior_review['confirmed_label'],
                        visual_condition=str(
                            prior_review['visual_condition'] or 'clear'
                        ),
                    )
            result['inserted' if was_inserted else 'updated'] += 1
            task_counts = result['by_task'].setdefault(
                item['task'], {'inserted': 0, 'updated': 0}
            )
            task_counts['inserted' if was_inserted else 'updated'] += 1
        except Exception as error:  # noqa: BLE001
            result['failed'] += 1
            result['last_error'] = str(error)[:200]
        result['processed'] += 1
        if progress is not None:
            progress(dict(result))
    return result


def _review_hash(review: Mapping[str, Any]) -> str:
    payload = json.dumps(
        dict(review), ensure_ascii=False, separators=(',', ':'), sort_keys=True
    ).encode('utf-8')
    return hashlib.sha256(payload).hexdigest()


def _mirror_specialized_review(
    conn: Any, item: Mapping[str, Any], label: Optional[str], visual_condition: str
) -> None:
    task = str(item['task'])
    frame_id = int(item['frame_id'])
    if task == 'bp_review':
        db.review_bp_item(
            conn, frame_id=frame_id, label=label, visual_condition=visual_condition
        )
    elif task == 'key_screen_review':
        db.review_key_screen_item(
            conn, frame_id=frame_id, label=label, visual_condition=visual_condition
        )


def pull_worker_candidate_reviews(
    conn: Any, reviews: Sequence[Mapping[str, Any]]
) -> Dict[str, int]:
    """合并 NAS 复核；本地未回传改动永远不会被远端静默覆盖。"""
    result = {'reviews_pulled': 0, 'review_conflicts': 0, 'reviews_ignored': 0}
    for raw in reviews:
        source_id = str(raw.get('source_id') or '')
        item = db.get_worker_candidate_by_source(conn, source_id)
        if item is None:
            result['reviews_ignored'] += 1
            continue
        if int(raw.get('schema_version', 0)) != 1:
            result['reviews_ignored'] += 1
            continue
        task = str(raw.get('task') or '')
        if task != item['task']:
            result['reviews_ignored'] += 1
            continue
        status = str(raw.get('review_status') or '')
        label_value = raw.get('confirmed_label')
        label = None if status == 'skipped' else str(label_value or '')
        if status not in ('confirmed', 'skipped'):
            result['reviews_ignored'] += 1
            continue
        if label and label not in _LABELS_BY_TASK[task]:
            result['reviews_ignored'] += 1
            continue
        digest = _review_hash(raw)
        if digest == item['remote_review_hash']:
            continue
        if item['sync_state'] == 'dirty':
            conn.execute(
                "UPDATE worker_candidate_items SET sync_state = 'conflict' "
                'WHERE id = ?',
                (int(item['id']),),
            )
            conn.commit()
            result['review_conflicts'] += 1
            continue
        visual_condition = str(raw.get('visual_condition') or 'clear')
        boxes = list(raw.get('boxes') or [])
        reviewed_at = str(raw.get('reviewed_at') or db.now())
        _mirror_specialized_review(conn, item, label, visual_condition)
        db.review_worker_candidate(
            conn,
            candidate_id=int(item['id']),
            label=label,
            visual_condition=visual_condition,
            boxes=boxes,
            notes=str(raw.get('notes') or ''),
            reviewed_at=reviewed_at,
            sync_state='clean',
            remote_review_hash=digest,
        )
        result['reviews_pulled'] += 1
    return result


def push_worker_candidate_reviews(conn: Any, nas: NasClient) -> Dict[str, int]:
    result = {'reviews_pushed': 0, 'push_failed': 0}
    items = conn.execute(
        "SELECT id FROM worker_candidate_items WHERE sync_state = 'dirty' "
        "AND review_status IN ('confirmed', 'skipped') ORDER BY id"
    ).fetchall()
    for row in items:
        item = db.get_worker_candidate(conn, int(row['id']))
        assert item is not None
        review = {
            'schema_version': 1,
            'source_id': item['source_id'],
            'task': item['task'],
            'image_path': item['image_path'],
            'image_sha256': item['image_sha256'],
            'review_status': item['review_status'],
            'confirmed_label': item['confirmed_label'],
            'visual_condition': item['visual_condition'],
            'boxes': item['boxes'],
            'notes': item['notes'],
            'reviewed_at': item['reviewed_at'] or db.now(),
            'reviewer': 'vision_lab',
        }
        try:
            nas.write_training_candidate_review(item['image_path'], review)
            digest = _review_hash(review)
            conn.execute(
                "UPDATE worker_candidate_items SET sync_state = 'clean', "
                'remote_review_hash = ?, remote_reviewed_at = ? WHERE id = ?',
                (digest, review['reviewed_at'], int(item['id'])),
            )
            conn.commit()
            result['reviews_pushed'] += 1
        except Exception:  # noqa: BLE001
            result['push_failed'] += 1
    return result


def _unified_review_sources(
    conn: Any, source_ids: Sequence[str]
) -> Sequence[Mapping[str, Any]]:
    rows = []
    seen = set()
    for source_id in source_ids:
        normalized = str(source_id or '')
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        row = conn.execute(
            "SELECT * FROM training_review_sources WHERE source_type IN "
            "('worker', 'manual_correction') "
            'AND source_id = ?',
            (normalized,),
        ).fetchone()
        if row is not None:
            rows.append(row)
    return rows


def pull_training_review_reviews(
    conn: Any, reviews: Sequence[Mapping[str, Any]]
) -> Dict[str, int]:
    """合并一图多标签复核；本地脏数据只标冲突，不被远端覆盖。"""
    result = {'reviews_pulled': 0, 'review_conflicts': 0, 'reviews_ignored': 0}
    for raw in reviews:
        if int(raw.get('schema_version', 0)) != 2:
            continue
        source_ids = raw.get('source_ids')
        if not isinstance(source_ids, list):
            result['reviews_ignored'] += 1
            continue
        sources = _unified_review_sources(conn, source_ids)
        frame_ids = {int(source['frame_id']) for source in sources}
        if not sources or len(frame_ids) != 1:
            result['reviews_ignored'] += 1
            continue
        digest = _review_hash(raw)
        if all(str(source['remote_review_hash']) == digest for source in sources):
            continue
        if any(str(source['sync_state']) == 'dirty' for source in sources):
            conn.executemany(
                "UPDATE training_review_sources SET sync_state = 'conflict' "
                'WHERE id = ?',
                [(int(source['id']),) for source in sources],
            )
            conn.commit()
            result['review_conflicts'] += 1
            continue
        labels = raw.get('labels')
        status = str(raw.get('review_status') or '')
        if not isinstance(labels, dict) or status not in (
            'partial',
            'confirmed',
            'skipped',
        ):
            result['reviews_ignored'] += 1
            continue
        frame_id = frame_ids.pop()
        try:
            result_label = labels.get('result_panel_label')
            hero_layout_label = labels.get('hero_layout_label')
            result_quality = raw.get('result_quality')
            if not isinstance(result_quality, dict):
                result_quality = {}
            result_box = raw.get('result_box')
            if result_label == 'result_panel' and result_box is not None:
                boxes = db.normalize_candidate_boxes([result_box])
                box = boxes[0]
                db.save_box(
                    conn,
                    frame_id,
                    'result_panel',
                    box['x'],
                    box['y'],
                    box['w'],
                    box['h'],
                )
            if hero_layout_label in {'gameplay_hud', 'scoreboard', 'result_page'}:
                raw_lineup = raw.get('hero_lineup')
                if not isinstance(raw_lineup, dict):
                    if status == 'confirmed':
                        raise ValueError('远端英雄阵容无效')
                else:
                    screen_type = str(raw_lineup.get('screen_type') or '')
                    if screen_type != hero_layout_label:
                        raise ValueError('远端英雄阵容画面类型不一致')
                    team_size = int(raw_lineup.get('team_size'))
                    raw_slots = raw_lineup.get('slots')
                    if not isinstance(raw_slots, list):
                        raise ValueError('远端英雄阵容位置无效')
                    slots = [
                        {
                            'side': slot.get('side'),
                            'slot': slot.get('slot'),
                            'crop': slot.get('crop'),
                        }
                        for slot in raw_slots
                        if isinstance(slot, dict)
                    ]
                    db.replace_training_review_hero_layout(
                        conn,
                        frame_id=frame_id,
                        screen_type=screen_type,
                        team_size=team_size,
                        method='remote-human-v1',
                        slots=slots,
                    )
                    db.save_training_review_hero_lineup(
                        conn,
                        frame_id=frame_id,
                        labels=[
                            {
                                'side': slot.get('side'),
                                'slot': slot.get('slot'),
                                'hero_label': slot.get('hero_label'),
                            }
                            for slot in raw_slots
                            if isinstance(slot, dict)
                        ],
                        allowed_labels=hero_review.allowed_hero_labels(),
                        player_status=raw_lineup.get('player_status'),
                        player_side=raw_lineup.get('player_side'),
                        player_slot=raw_lineup.get('player_slot'),
                    )
            db.save_training_review(
                conn,
                frame_id=frame_id,
                match_flow_label=labels.get('match_flow_label'),
                match_mode_label=labels.get('match_mode_label'),
                hero_select_label=labels.get('hero_select_label'),
                hero_select_variant=labels.get('hero_select_variant'),
                hero_select_visibility=labels.get('hero_select_visibility'),
                result_panel_label=result_label,
                hero_layout_label=hero_layout_label,
                panel_render_state=str(
                    result_quality.get('panel_render_state') or 'clear'
                ),
                ocr_usable=str(result_quality.get('ocr_usable') or 'yes'),
                result_occlusion=str(result_quality.get('result_occlusion') or 'none'),
                occluder_types=result_quality.get('occluder_types') or [],
                status=status,
                notes=str(raw.get('notes') or ''),
            )
            if any(
                str(source['source_type']) == 'manual_correction' for source in sources
            ):
                db.audit(
                    conn,
                    'training_review',
                    frame_id=frame_id,
                    detail='imported manual correction from BLREC admin',
                )
        except (KeyError, TypeError, ValueError):
            result['reviews_ignored'] += 1
            continue
        reviewed_at = str(raw.get('reviewed_at') or db.now())
        conn.execute(
            "UPDATE training_review_sources SET sync_state = 'clean', "
            'remote_review_hash = ?, remote_reviewed_at = ?, updated_at = ? '
            "WHERE frame_id = ? AND source_type IN "
            "('worker', 'manual_correction')",
            (digest, reviewed_at, db.now(), frame_id),
        )
        conn.commit()
        result['reviews_pulled'] += 1
    return result


def push_training_review_reviews(conn: Any, nas: NasClient) -> Dict[str, int]:
    """把每张图的人工标签、英雄圆框和阵容作为一个 sidecar 回传 NAS。"""
    result = {'reviews_pushed': 0, 'push_failed': 0}
    rows = conn.execute(
        "SELECT DISTINCT frame_id FROM training_review_sources "
        "WHERE source_type IN ('worker', 'manual_correction') "
        "AND sync_state = 'dirty' "
        'ORDER BY frame_id'
    ).fetchall()
    for row in rows:
        frame_id = int(row['frame_id'])
        item = db.get_training_review_item(conn, frame_id)
        if item is None or item['review_status'] not in ('confirmed', 'skipped'):
            continue
        sources = conn.execute(
            "SELECT source_id, image_path FROM training_review_sources "
            "WHERE frame_id = ? AND source_type IN "
            "('worker', 'manual_correction') ORDER BY id",
            (frame_id,),
        ).fetchall()
        image_path = next(
            (str(source['image_path']) for source in sources if source['image_path']),
            '',
        )
        if not image_path:
            result['push_failed'] += 1
            continue
        result_box = item['boxes'].get('result_panel')
        if result_box is not None:
            result_box = {
                name: float(result_box[name]) for name in ('x', 'y', 'w', 'h')
            }
        lineup = db.get_training_review_hero_lineup(conn, frame_id)
        hero_lineup = None
        if (
            item.get('hero_layout_label')
            in {'gameplay_hud', 'scoreboard', 'result_page'}
            and lineup is not None
            and lineup['review_status'] == 'confirmed'
        ):
            hero_lineup = {
                'screen_type': lineup['screen_type'],
                'team_size': int(lineup['team_size']),
                'player_status': lineup['player_status'],
                'player_side': lineup['player_side'],
                'player_slot': lineup['player_slot'],
                'slots': [
                    {
                        'side': slot['side'],
                        'slot': int(slot['slot']),
                        'crop': {
                            name: float(slot['crop'][name])
                            for name in ('x', 'y', 'w', 'h')
                        },
                        'hero_label': slot['confirmed_label'],
                    }
                    for slot in lineup['slots']
                ],
            }
        review = {
            'schema_version': 2,
            'source_ids': [str(source['source_id']) for source in sources],
            'image_path': image_path,
            'image_sha256': item['sha256'],
            'review_status': item['review_status'],
            'labels': {
                name: item[name]
                for name in (
                    'match_flow_label',
                    'match_mode_label',
                    'hero_select_label',
                    'hero_select_variant',
                    'hero_select_visibility',
                    'result_panel_label',
                    'hero_layout_label',
                )
            },
            'result_box': result_box,
            'result_quality': {
                'panel_render_state': item['panel_render_state'],
                'ocr_usable': item['ocr_usable'],
                'result_occlusion': item['result_occlusion'],
                'occluder_types': item['occluder_types'],
            },
            'hero_lineup': hero_lineup,
            'notes': item['notes'],
            'reviewed_at': item['reviewed_at'] or db.now(),
            'reviewer': 'vision_lab',
        }
        try:
            nas.write_training_candidate_review(image_path, review)
            digest = _review_hash(review)
            conn.execute(
                "UPDATE training_review_sources SET sync_state = 'clean', "
                'remote_review_hash = ?, remote_reviewed_at = ?, updated_at = ? '
                "WHERE frame_id = ? AND source_type IN "
                "('worker', 'manual_correction')",
                (digest, review['reviewed_at'], db.now(), frame_id),
            )
            conn.commit()
            result['reviews_pushed'] += 1
        except Exception:  # noqa: BLE001
            result['push_failed'] += 1
    return result
