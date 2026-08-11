"""用当前新模型给统一复核页生成可追溯建议；绝不写人工真值。"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from PIL import Image

from . import db, inference

CORE_PREFILL_TASKS = ('match_flow', 'hero_select', 'match_mode', 'result_detector')
HERO_PREFILL_TASKS = ('hero_avatar_detector', 'hero_identity', 'player_position')


def _latest_model_contexts(
    conn: Any, task_ids: Iterable[str]
) -> Dict[str, Dict[str, Any]]:
    requested = tuple(dict.fromkeys(str(value) for value in task_ids))
    if not requested:
        return {}
    placeholders = ','.join('?' for _ in requested)
    rows = conn.execute(
        'SELECT run.*, validation.status AS validation_status '
        'FROM training_runs run '
        'LEFT JOIN model_validations validation ON validation.run_id = run.id '
        f"WHERE run.status = 'succeeded' AND run.task_id IN ({placeholders}) "
        'ORDER BY run.created_at DESC, run.id DESC',
        requested,
    ).fetchall()
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for row in rows:
        value = dict(row)
        grouped.setdefault(str(value['task_id']), []).append(value)
    result: Dict[str, Dict[str, Any]] = {}
    for task_id in requested:
        candidates = grouped.get(task_id, [])
        selected = None
        for priority in range(3):
            for run in candidates:
                published = Path(str(run.get('published_path') or ''))
                if published.is_file() and published.with_suffix('.json').is_file():
                    run_priority = 0
                    artifact = published
                elif run.get('validation_status') == 'passed':
                    run_priority = 1
                    artifact = Path(str(run['artifact_path']))
                else:
                    run_priority = 2
                    artifact = Path(str(run['artifact_path']))
                if run_priority != priority:
                    continue
                metadata_path = artifact.with_suffix('.json')
                if not artifact.is_file() or not metadata_path.is_file():
                    continue
                try:
                    metadata = json.loads(metadata_path.read_text(encoding='utf-8'))
                except (json.JSONDecodeError, OSError):
                    continue
                selected = {
                    'run_id': str(run['id']),
                    'artifact': artifact,
                    'metadata': metadata,
                    'validation_status': run.get('validation_status') or 'pending',
                    'published': artifact == published,
                }
                break
            if selected is not None:
                break
        if selected is not None:
            result[task_id] = selected
    return result


def _classification_suggestion(
    task_id: str, context: Dict[str, Any], result: Dict[str, Any]
) -> Dict[str, Any]:
    top1 = result.get('top1')
    if not isinstance(top1, dict):
        raise ValueError(f'{task_id} 没有结构化 top1')
    return {
        'label': str(top1.get('class') or ''),
        'confidence': float(top1.get('prob') or 0),
        'origin': 'new_model_prefill',
        'model_run_id': context['run_id'],
        'reason': f'新模型 {context["run_id"]}',
    }


def _result_suggestion(
    context: Dict[str, Any], result: Dict[str, Any]
) -> Dict[str, Any]:
    detections = [
        value
        for value in result.get('detections') or []
        if isinstance(value, dict) and value.get('class') in {None, '', 'result_panel'}
    ]
    found = bool(result.get('found')) and bool(detections)
    top_confidence = max(
        (float(value.get('conf') or 0) for value in detections),
        default=float(result.get('raw_top_conf') or 0),
    )
    return {
        'label': 'result_panel' if found else 'no_result_panel',
        'confidence': top_confidence if found else max(0.0, 1.0 - top_confidence),
        'origin': 'new_model_prefill',
        'model_run_id': context['run_id'],
        'reason': f'新模型 {context["run_id"]}',
    }


def prefill_training_review_item(
    conn: Any,
    frame_id: int,
    *,
    task_ids: Iterable[str] = CORE_PREFILL_TASKS,
    force: bool = False,
) -> Dict[str, Any]:
    item = db.get_training_review_item(conn, int(frame_id))
    if item is None:
        raise KeyError(f'训练复核图片不存在: {frame_id}')
    frame_path = Path(str(item['frame_path']))
    if not frame_path.is_file():
        raise FileNotFoundError(frame_path)
    requested_tasks = tuple(dict.fromkeys((*task_ids, 'hero_avatar_detector')))
    contexts = _latest_model_contexts(conn, requested_tasks)
    if not contexts:
        return {'applied': False, 'cached': False, 'models': {}, 'item': item}
    model_runs = {task_id: context['run_id'] for task_id, context in contexts.items()}
    source_id = f'frame:{int(frame_id)}'
    previous = conn.execute(
        'SELECT metadata_json FROM training_review_sources '
        'WHERE source_type = ? AND source_id = ?',
        ('new_model_prefill', source_id),
    ).fetchone()
    if previous is not None and not force:
        metadata = json.loads(previous['metadata_json'] or '{}')
        if metadata.get('model_runs') == model_runs and not metadata.get('errors'):
            return {
                'applied': False,
                'cached': True,
                'models': model_runs,
                'item': item,
            }

    suggestions: Dict[str, Dict[str, Any]] = {}
    model_outputs = []
    suggested_boxes = []
    errors: Dict[str, str] = {}
    for task_id, context in contexts.items():
        if task_id == 'hero_avatar_detector':
            continue
        try:
            output = inference.run_artifact(
                context['artifact'], context['metadata'], frame_path, conf_thr=0.25
            )
            if task_id == 'result_detector':
                suggestion = _result_suggestion(context, output)
                suggestions['result_panel'] = suggestion
                if suggestion['label'] == 'result_panel':
                    for detection in output.get('detections') or []:
                        xywh = detection.get('xywh_norm')
                        if not isinstance(xywh, list) or len(xywh) != 4:
                            continue
                        suggested_boxes.append(
                            {
                                'type': 'result_panel',
                                'x': float(xywh[0]),
                                'y': float(xywh[1]),
                                'w': float(xywh[2]),
                                'h': float(xywh[3]),
                                'confidence': float(detection.get('conf') or 0),
                            }
                        )
                model_outputs.append(
                    {
                        'task_id': task_id,
                        'run_id': context['run_id'],
                        'found': bool(output.get('found')),
                        'raw_top_conf': float(output.get('raw_top_conf') or 0),
                    }
                )
            else:
                suggestion = _classification_suggestion(task_id, context, output)
                suggestions[task_id] = suggestion
                model_outputs.append(
                    {
                        'task_id': task_id,
                        'run_id': context['run_id'],
                        'top1': output.get('top1'),
                        'top5': output.get('top5'),
                    }
                )
        except Exception as exc:  # noqa: BLE001
            errors[task_id] = str(exc)[:300]
    hero_context_suggestion = None
    hero_detector = contexts.get('hero_avatar_detector')
    if hero_detector is not None:
        try:
            hero_output = inference.run_artifact(
                hero_detector['artifact'],
                hero_detector['metadata'],
                frame_path,
                conf_thr=0.25,
            )
            hero_context_suggestion = _infer_hero_context_suggestion(
                list(hero_output.get('detections') or []),
                result_found=(
                    suggestions.get('result_panel', {}).get('label') == 'result_panel'
                ),
            )
            model_outputs.append(
                {
                    'task_id': 'hero_avatar_detector',
                    'run_id': hero_detector['run_id'],
                    'found': bool(hero_output.get('found')),
                    'detected': len(hero_output.get('detections') or []),
                    'hero_context_suggestion': hero_context_suggestion,
                }
            )
        except Exception as exc:  # noqa: BLE001
            errors['hero_avatar_detector'] = str(exc)[:300]
    db.add_training_review_source(
        conn,
        frame_id=int(frame_id),
        source_type='new_model_prefill',
        source_id=source_id,
        suggestions=suggestions,
        metadata={
            'model_runs': model_runs,
            'model_outputs': model_outputs,
            'suggested_boxes': suggested_boxes,
            'hero_context_suggestion': hero_context_suggestion,
            'errors': errors,
        },
        image_path=str(frame_path),
    )
    return {
        'applied': bool(suggestions),
        'cached': False,
        'models': model_runs,
        'errors': errors,
        'item': db.get_training_review_item(conn, int(frame_id)),
    }


def _usable_avatar_detections(detections: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    usable = []
    for detection in detections:
        xywh = detection.get('xywh_norm')
        if not isinstance(xywh, list) or len(xywh) != 4:
            continue
        try:
            x, y, width, height = (float(value) for value in xywh)
        except (TypeError, ValueError):
            continue
        if not (
            0 <= x <= 1
            and 0 <= y <= 1
            and 0 < width <= 1
            and 0 < height <= 1
            and x + width <= 1.001
            and y + height <= 1.001
        ):
            continue
        usable.append(
            {
                'crop': {'x': x, 'y': y, 'w': width, 'h': height},
                'confidence': float(detection.get('conf') or 0),
                'center_x': x + width / 2,
                'center_y': y + height / 2,
            }
        )
    return usable


def _avatar_team_size(detected: int) -> int:
    return 5 if detected > 6 else 3


def _hero_context_payload(
    values: List[Dict[str, Any]], *, screen_type: str
) -> Dict[str, Any]:
    team_size = _avatar_team_size(len(values))
    expected = team_size * 2
    ranked = sorted(values, key=lambda value: value['confidence'], reverse=True)
    confidence = sum(
        value['confidence'] for value in ranked[: min(expected, len(ranked))]
    ) / min(expected, len(ranked))
    return {
        'screen_type': screen_type,
        'team_size': team_size,
        'confidence': round(confidence, 4),
        'detected': len(values),
        'complete_detection': len(values) >= expected,
    }


def _infer_hero_context_suggestion(
    detections: List[Dict[str, Any]], *, result_found: bool
) -> Optional[Dict[str, Any]]:
    """用头像排列推断 HUD／积分板；不确定时宁可不自动选择。"""
    usable = _usable_avatar_detections(detections)
    if len(usable) < 6:
        return None
    panel = [value for value in usable if value['center_y'] >= 0.10]
    top = [value for value in usable if value['center_y'] <= 0.22]
    if result_found and len(panel) >= 6:
        return _hero_context_payload(panel, screen_type='result_page')
    if len(panel) >= 6:
        panel_y = [value['center_y'] for value in panel]
        if max(panel_y) - min(panel_y) >= 0.12:
            return _hero_context_payload(panel, screen_type='scoreboard')
    if len(top) >= 6:
        top_y = [value['center_y'] for value in top]
        if max(top_y) - min(top_y) <= 0.10:
            return _hero_context_payload(top, screen_type='gameplay_hud')
    return None


def _ordered_avatar_slots(
    detections: List[Dict[str, Any]], *, screen_type: str, team_size: int
) -> List[Dict[str, Any]]:
    expected = team_size * 2
    usable = _usable_avatar_detections(detections)
    if screen_type == 'gameplay_hud':
        top = [value for value in usable if value['center_y'] <= 0.22]
        if len(top) >= expected:
            usable = top
    elif screen_type in {'scoreboard', 'result_page'}:
        panel = [value for value in usable if value['center_y'] >= 0.10]
        if len(panel) >= expected:
            usable = panel
    if len(usable) < expected:
        return []
    if len(usable) > expected:
        usable = sorted(usable, key=lambda value: value['confidence'], reverse=True)[
            :expected
        ]
    by_x = sorted(usable, key=lambda value: value['center_x'])
    left = by_x[:team_size]
    right = by_x[team_size:]
    if screen_type == 'gameplay_hud':
        left.sort(key=lambda value: value['center_x'], reverse=True)
        right.sort(key=lambda value: value['center_x'])
    else:
        left.sort(key=lambda value: value['center_y'])
        right.sort(key=lambda value: value['center_y'])
    slots = []
    for side, values in (('left', left), ('right', right)):
        for slot, value in enumerate(values, 1):
            slots.append(
                {
                    'side': side,
                    'slot': slot,
                    'crop': value['crop'],
                    'suggested_label': '',
                    'suggestion_confidence': 0.0,
                    'detection_confidence': value['confidence'],
                }
            )
    return slots


def _crop_to_path(
    image: Image.Image, crop: Dict[str, float], destination: Path
) -> None:
    left = max(0, min(image.width - 1, round(crop['x'] * image.width)))
    top = max(0, min(image.height - 1, round(crop['y'] * image.height)))
    right = max(
        left + 1, min(image.width, round((crop['x'] + crop['w']) * image.width))
    )
    bottom = max(
        top + 1, min(image.height, round((crop['y'] + crop['h']) * image.height))
    )
    image.crop((left, top, right, bottom)).save(destination, format='JPEG', quality=95)


def prefill_hero_lineup(
    conn: Any, frame_path: Path, *, screen_type: str, team_size: int
) -> Dict[str, Any]:
    """头像检测 → 槽位排序 → 单头像身份；结果仍只是待人工确认建议。"""
    if screen_type not in {'gameplay_hud', 'scoreboard', 'result_page'}:
        raise ValueError('英雄阵容画面类型无效')
    if team_size not in {3, 5}:
        raise ValueError('英雄阵容人数必须是 3 或 5')
    source = Path(frame_path)
    if not source.is_file():
        raise FileNotFoundError(source)
    contexts = _latest_model_contexts(conn, HERO_PREFILL_TASKS)
    detector = contexts.get('hero_avatar_detector')
    model_runs = {task_id: context['run_id'] for task_id, context in contexts.items()}
    if detector is None:
        return {
            'complete': False,
            'reason': '没有可用的英雄头像位置模型',
            'slots': [],
            'model_runs': model_runs,
        }
    detection = inference.run_artifact(
        detector['artifact'], detector['metadata'], source, conf_thr=0.25
    )
    slots = _ordered_avatar_slots(
        list(detection.get('detections') or []),
        screen_type=screen_type,
        team_size=team_size,
    )
    if len(slots) != team_size * 2:
        return {
            'complete': False,
            'reason': '头像位置模型没有找全 {} 个头像'.format(team_size * 2),
            'slots': [],
            'model_runs': model_runs,
            'detected': len(detection.get('detections') or []),
        }
    identity = contexts.get('hero_identity')
    if identity is not None:
        with Image.open(source) as opened:
            image = opened.convert('RGB')
        with tempfile.TemporaryDirectory(prefix='vision-lab-hero-prefill-') as tmp:
            temporary = Path(tmp)
            for index, slot in enumerate(slots):
                crop_path = temporary / f'{index}.jpg'
                _crop_to_path(image, slot['crop'], crop_path)
                prediction = inference.run_artifact(
                    identity['artifact'], identity['metadata'], crop_path
                )
                top1 = prediction.get('top1')
                if isinstance(top1, dict):
                    slot['suggested_label'] = str(top1.get('class') or '')
                    slot['suggestion_confidence'] = float(top1.get('prob') or 0)
    player_suggestion = None
    player = contexts.get('player_position')
    if player is not None and screen_type in {'scoreboard', 'result_page'}:
        prediction = inference.run_artifact(
            player['artifact'], player['metadata'], source
        )
        top1 = prediction.get('top1')
        if isinstance(top1, dict):
            label = str(top1.get('class') or '')
            side = (
                'left'
                if label.startswith('left')
                else 'right' if label.startswith('right') else ''
            )
            try:
                slot = int(label[len(side) :]) if side else 0
            except ValueError:
                slot = 0
            if side and 1 <= slot <= team_size:
                player_suggestion = {
                    'side': side,
                    'slot': slot,
                    'confidence': float(top1.get('prob') or 0),
                }
    return {
        'complete': True,
        'slots': slots,
        'player_suggestion': player_suggestion,
        'model_runs': model_runs,
        'detected': len(detection.get('detections') or []),
    }
