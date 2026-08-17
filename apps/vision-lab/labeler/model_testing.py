"""训练产物验收与不可变 Analysis Worker 模型包。"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from datetime import datetime
from pathlib import Path
from time import perf_counter
from typing import Any, Dict, List, Optional, Sequence
from uuid import uuid4

from PIL import Image

from . import config, db, inference

TASK_ROLES = {
    'match_flow': 'match_flow',
    'hero_select': 'hero_select',
    'match_mode': 'match_mode',
    'screen_state': 'screen_state',
    'bp_review': 'bp_classifier',
    'key_screen_review': 'key_screen',
    'result_detector': 'result_panel',
    'mode_gate': 'mode_gate',
    'hero_avatar_detector': 'hero_avatar',
    'hero_identity': 'hero_identity',
    'player_position': 'player_position',
}
REQUIRED_TASKS = (
    'match_flow',
    'hero_select',
    'match_mode',
    'result_detector',
    'hero_avatar_detector',
    'hero_identity',
    'player_position',
)
FIXED_DATASET_SPLITS = {'train', 'val', 'test'}
SCOREBOARD_CHALLENGE_SPLIT = 'scoreboard_challenge'
POST_RUN_CHALLENGE_SPLIT = 'post_run_challenge'
POST_RUN_CHALLENGE_TASKS = {
    'match_flow',
    'hero_select',
    'match_mode',
    'result_detector',
    'hero_avatar_detector',
    'hero_identity',
    'player_position',
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _run_context(conn: Any, run_id: str) -> Dict[str, Any]:
    run = db.get_training_run(conn, run_id)
    if run is None:
        raise KeyError(f'训练记录不存在: {run_id}')
    if run['status'] != 'succeeded':
        raise ValueError('只有训练成功的 run 才能测试')
    dataset = conn.execute(
        'SELECT * FROM dataset_versions WHERE id = ?', (run['dataset_version_id'],)
    ).fetchone()
    if dataset is None:
        raise KeyError(f'数据集版本不存在: {run["dataset_version_id"]}')
    artifact = Path(run['artifact_path'])
    metadata_path = artifact.with_suffix('.json')
    if not artifact.is_file() or not metadata_path.is_file():
        raise FileNotFoundError('训练产物或元数据不存在')
    metadata = json.loads(metadata_path.read_text(encoding='utf-8'))
    return {
        'run': run,
        'dataset': dict(dataset),
        'artifact': artifact,
        'metadata': metadata,
    }


def list_testable_runs(conn: Any) -> List[Dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT r.*, d.manifest_path, d.counts_json,
               v.status AS validation_status, v.notes AS validation_notes,
               v.tested_at
        FROM training_runs r
        JOIN dataset_versions d ON d.id = r.dataset_version_id
        LEFT JOIN model_validations v ON v.run_id = r.id
        WHERE r.status = 'succeeded'
        ORDER BY r.created_at DESC, r.id DESC
        """
    ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item['metrics_json'] = json.loads(item['metrics_json'] or '{}')
        item['config_json'] = json.loads(item['config_json'] or '{}')
        item['counts_json'] = json.loads(item['counts_json'] or '{}')
        item['validation_status'] = item['validation_status'] or 'pending'
        metadata_path = Path(item['artifact_path']).with_suffix('.json')
        try:
            metadata = json.loads(metadata_path.read_text(encoding='utf-8'))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            metadata = {}
        item['artifact_metadata'] = {
            key: metadata[key]
            for key in ('task_id', 'kind', 'imgsz', 'input', 'preprocessing', 'classes')
            if key in metadata
        }
        try:
            item['evaluation_gaps'] = _evaluation_gaps(
                _run_context(conn, str(item['id']))
            )
        except (FileNotFoundError, KeyError, ValueError):
            item['evaluation_gaps'] = ['无法读取固定测试集覆盖情况']
        result.append(item)
    return result


def _manifest_samples(path: Path) -> List[Dict[str, Any]]:
    if not path.is_file():
        raise FileNotFoundError(path)
    samples = []
    with path.open(encoding='utf-8') as handle:
        for line in handle:
            if line.strip():
                value = json.loads(line)
                if isinstance(value, dict):
                    samples.append(value)
    return samples


def _sample_image_path(context: Dict[str, Any], sample: Dict[str, Any]) -> Path:
    """返回训练 run 所绑定快照中的图片，而不是可变的原始帧路径。"""
    sample_id = str(sample.get('sample_id') or '')
    split = str(sample.get('split') or '')
    if (
        not sample_id
        or Path(sample_id).name != sample_id
        or split not in FIXED_DATASET_SPLITS
    ):
        raise ValueError('数据快照中的样本标识或切分无效')
    root = Path(context['dataset']['manifest_path']).parent.resolve()
    kind = str(context['metadata'].get('kind') or '')
    if kind == 'classify':
        label = str(sample.get('label') or '')
        if not label or Path(label).name != label:
            raise ValueError('分类快照中的标签无效')
        path = root / 'images' / split / label / f'{sample_id}.jpg'
    elif kind == 'detect':
        path = root / 'images' / split / f'{sample_id}.jpg'
    else:
        raise ValueError(f'未知训练产物类型: {kind}')
    resolved = path.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as error:
        raise ValueError('样本图片不在训练快照目录内') from error
    if not resolved.is_file():
        raise FileNotFoundError(resolved)
    return resolved


def _scoreboard_challenge_rows(
    conn: Any, *, frame_id: Optional[int] = None
) -> List[Dict[str, Any]]:
    where = """
        (
            r.review_status = 'confirmed'
            AND r.result_panel_label = 'no_result_panel'
            AND (
                r.hero_layout_label = 'scoreboard'
                OR a.screen_type IN ('scoreboard', 'death_scoreboard')
            )
        )
        OR (
            (r.frame_id IS NULL OR r.review_status != 'confirmed')
            AND a.annotation_status = 'complete'
            AND a.screen_type IN ('scoreboard', 'death_scoreboard')
        )
    """
    arguments: List[Any] = []
    if frame_id is not None:
        where = f'({where}) AND f.id = ?'
        arguments.append(int(frame_id))
    rows = conn.execute(
        f"""
        SELECT f.id AS frame_id, f.frame_path, f.timestamp_ms,
               f.video_id, v.streamer, r.match_mode_label,
               r.panel_render_state, r.hero_layout_label,
               a.screen_type, a.game_mode
        FROM frames f
        JOIN videos v ON v.id = f.video_id
        LEFT JOIN training_review_items r ON r.frame_id = f.id
        LEFT JOIN annotations a ON a.frame_id = f.id
        WHERE {where}
        ORDER BY
            CASE COALESCE(r.match_mode_label, a.game_mode)
                WHEN 'aram' THEN 0
                WHEN '5v5' THEN 1
                WHEN '3v3' THEN 2
                ELSE 3
            END,
            f.video_id, f.timestamp_ms, f.id
        """,
        arguments,
    ).fetchall()
    return [dict(row) for row in rows]


def _scoreboard_challenge_sample(row: Dict[str, Any]) -> Dict[str, Any]:
    raw_mode = row.get('match_mode_label')
    if raw_mode is None:
        raw_mode = row.get('game_mode')
    mode = str(raw_mode or '')
    if mode not in {'3v3', '5v5', 'aram'}:
        mode = 'unreadable'
    path = Path(str(row['frame_path'])).resolve()
    return {
        'sample_id': f'f{int(row["frame_id"]):08d}',
        'frame_id': int(row['frame_id']),
        'split': SCOREBOARD_CHALLENGE_SPLIT,
        'has_snapshot_image': path.is_file(),
        'expected': {'found': False, 'label': 'no_result_panel', 'boxes': []},
        'streamer': row.get('streamer') or '',
        'timestamp_ms': int(row.get('timestamp_ms') or 0),
        'visual_condition': row.get('panel_render_state') or 'clear',
        'evaluation_scenario': 'scoreboard',
        'evaluation_mode': mode,
        'evaluation_groups': [
            'scoreboard',
            *([f'scoreboard:{mode}'] if mode != 'unreadable' else []),
        ],
    }


def _run_manifest_members(context: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    return {
        str(sample.get('sample_id') or ''): sample
        for sample in _manifest_samples(Path(context['dataset']['manifest_path']))
        if str(sample.get('sample_id') or '')
    }


def _challenge_base(
    row: Dict[str, Any], *, sample_id: str, expected: Any
) -> Dict[str, Any]:
    path = Path(str(row['frame_path'])).resolve()
    return {
        'sample_id': sample_id,
        'frame_id': int(row['frame_id']),
        'video_id': int(row['video_id']),
        'split': POST_RUN_CHALLENGE_SPLIT,
        'has_snapshot_image': path.is_file(),
        'expected': expected,
        'streamer': row.get('streamer') or '',
        'timestamp_ms': int(row.get('timestamp_ms') or 0),
        'visual_condition': row.get('panel_render_state') or 'clear',
        'evaluation_scenario': row.get('evaluation_scenario') or '',
        'evaluation_mode': row.get('match_mode_label') or 'unreadable',
        'reviewed_at': row.get('reviewed_at') or '',
        '_frame_path': str(path),
    }


def _post_run_core_samples(
    conn: Any, *, task_id: str, cutoff: str
) -> List[Dict[str, Any]]:
    column = {
        'match_flow': 'match_flow_label',
        'hero_select': 'hero_select_label',
        'match_mode': 'match_mode_label',
        'result_detector': 'result_panel_label',
    }[task_id]
    allowed = {
        'match_flow': {'match_flow', 'not_match_flow'},
        'hero_select': {'not_select', 'select_3v3', 'select_aram', 'select_5v5'},
        'match_mode': {'3v3', 'aram', '5v5'},
        'result_detector': {'result_panel', 'no_result_panel'},
    }[task_id]
    rows = conn.execute(
        f'SELECT f.id AS frame_id, f.video_id, f.frame_path, '
        'f.timestamp_ms, v.streamer, r.reviewed_at, '
        'r.panel_render_state, r.hero_layout_label AS evaluation_scenario, '
        f'r.match_mode_label, r.{column} AS label '
        'FROM training_review_items r '
        'JOIN frames f ON f.id = r.frame_id '
        'JOIN videos v ON v.id = f.video_id '
        "WHERE r.review_status = 'confirmed' "
        'AND r.reviewed_at > ? '
        f'AND r.{column} IS NOT NULL '
        'ORDER BY r.reviewed_at, f.video_id, f.timestamp_ms, f.id',
        (cutoff,),
    ).fetchall()
    duplicate_results = db.training_review_duplicate_result_frame_ids(conn)
    samples = []
    for raw_row in rows:
        row = dict(raw_row)
        frame_id = int(row['frame_id'])
        label = str(row['label'])
        if label not in allowed or frame_id in duplicate_results:
            continue
        sample_id = f'f{frame_id:08d}'
        if task_id != 'result_detector':
            samples.append(_challenge_base(row, sample_id=sample_id, expected=label))
            continue
        raw_box = db.get_boxes(conn, frame_id).get('result_panel')
        if label == 'result_panel' and not isinstance(raw_box, dict):
            continue
        boxes = []
        if isinstance(raw_box, dict):
            boxes.append(
                {
                    'class': 'result_panel',
                    'xywh_norm': [
                        float(raw_box['x']),
                        float(raw_box['y']),
                        float(raw_box['w']),
                        float(raw_box['h']),
                    ],
                }
            )
        row['evaluation_scenario'] = (
            'result_panel'
            if label == 'result_panel'
            else (
                'scoreboard'
                if row.get('evaluation_scenario') == 'scoreboard'
                else 'other_negative'
            )
        )
        samples.append(
            _challenge_base(
                row,
                sample_id=sample_id,
                expected={
                    'found': label == 'result_panel',
                    'label': label,
                    'boxes': boxes if label == 'result_panel' else [],
                },
            )
        )
    return samples


def _post_run_hero_samples(
    conn: Any, *, task_id: str, cutoff: str
) -> List[Dict[str, Any]]:
    rows = conn.execute(
        'SELECT lineup.frame_id, lineup.screen_type, lineup.team_size, '
        'lineup.player_status, lineup.player_side, lineup.player_slot, '
        'lineup.reviewed_at, f.video_id, f.frame_path, f.timestamp_ms, '
        'v.streamer, review.panel_render_state, review.match_mode_label, '
        'slot.side, slot.slot, slot.crop_x, slot.crop_y, slot.crop_w, '
        'slot.crop_h, slot.confirmed_label '
        'FROM training_review_hero_lineups lineup '
        'JOIN training_review_hero_slots slot '
        'ON slot.frame_id = lineup.frame_id '
        'JOIN frames f ON f.id = lineup.frame_id '
        'JOIN videos v ON v.id = f.video_id '
        'LEFT JOIN training_review_items review ON review.frame_id = f.id '
        "WHERE lineup.review_status = 'confirmed' "
        'AND lineup.reviewed_at > ? '
        'ORDER BY lineup.reviewed_at, f.video_id, f.timestamp_ms, f.id, '
        "CASE slot.side WHEN 'left' THEN 0 ELSE 1 END, slot.slot",
        (cutoff,),
    ).fetchall()
    grouped: Dict[int, Dict[str, Any]] = {}
    for raw_row in rows:
        row = dict(raw_row)
        frame_id = int(row['frame_id'])
        group = grouped.setdefault(
            frame_id,
            {
                **row,
                'frame_id': frame_id,
                'evaluation_scenario': str(row['screen_type']),
                'slots': [],
            },
        )
        group['slots'].append(
            {
                'side': str(row['side']),
                'slot': int(row['slot']),
                'crop': {
                    'x': float(row['crop_x']),
                    'y': float(row['crop_y']),
                    'w': float(row['crop_w']),
                    'h': float(row['crop_h']),
                },
                'label': str(row['confirmed_label'] or ''),
            }
        )
    duplicate_results = db.training_review_duplicate_result_frame_ids(conn)
    samples = []
    for row in grouped.values():
        frame_id = int(row['frame_id'])
        team_size = int(row['team_size'])
        if len(row['slots']) != team_size * 2 or frame_id in duplicate_results:
            continue
        if task_id == 'hero_avatar_detector':
            sample = _challenge_base(
                row,
                sample_id=f'f{frame_id:08d}',
                expected={
                    'found': True,
                    'label': 'hero_avatar',
                    'boxes': [
                        {
                            'class': 'hero_avatar',
                            'xywh_norm': [
                                slot['crop']['x'],
                                slot['crop']['y'],
                                slot['crop']['w'],
                                slot['crop']['h'],
                            ],
                        }
                        for slot in row['slots']
                    ],
                },
            )
            samples.append(sample)
            continue
        if task_id == 'hero_identity':
            for slot in row['slots']:
                label = str(slot['label'])
                if not label or label == 'unreadable':
                    continue
                sample = _challenge_base(
                    row,
                    sample_id='f{:08d}-{}-{}'.format(
                        frame_id, slot['side'], slot['slot']
                    ),
                    expected=label,
                )
                sample['_crop'] = slot['crop']
                samples.append(sample)
            continue
        if (
            row['screen_type'] not in {'scoreboard', 'result_page'}
            or row['player_status'] != 'identified'
            or row['player_side'] not in {'left', 'right'}
            or not 1 <= int(row['player_slot'] or 0) <= team_size
        ):
            continue
        label = '{}{}'.format(row['player_side'], int(row['player_slot']))
        samples.append(
            _challenge_base(row, sample_id=f'f{frame_id:08d}', expected=label)
        )
    return samples


def _post_run_challenge_samples(
    conn: Any, context: Dict[str, Any]
) -> List[Dict[str, Any]]:
    task_id = str(context['run']['task_id'])
    if task_id not in POST_RUN_CHALLENGE_TASKS:
        raise ValueError('这个旧模型没有统一复核挑战集')
    cutoff = str(
        context['run'].get('finished_at') or context['run'].get('created_at') or ''
    )
    if task_id in {'match_flow', 'hero_select', 'match_mode', 'result_detector'}:
        samples = _post_run_core_samples(conn, task_id=task_id, cutoff=cutoff)
    else:
        samples = _post_run_hero_samples(conn, task_id=task_id, cutoff=cutoff)
    manifest_members = _run_manifest_members(context)
    manifest_videos = {
        int(sample['video_id'])
        for sample in manifest_members.values()
        if sample.get('video_id') is not None
    }
    result = []
    for sample in samples:
        if sample['sample_id'] in manifest_members:
            continue
        sample['is_new_video'] = int(sample['video_id']) not in manifest_videos
        result.append(sample)
    return result


def _public_challenge_sample(sample: Dict[str, Any]) -> Dict[str, Any]:
    return {key: value for key, value in sample.items() if not key.startswith('_')}


def _challenge_image_path(context: Dict[str, Any], sample: Dict[str, Any]) -> Path:
    source = Path(str(sample['_frame_path'])).resolve()
    if not source.is_file():
        raise FileNotFoundError(source)
    crop = sample.get('_crop')
    if not isinstance(crop, dict):
        return source
    cache_key = hashlib.sha256(
        json.dumps(
            {
                'source': str(source),
                'mtime_ns': source.stat().st_mtime_ns,
                'size': source.stat().st_size,
                'crop': crop,
            },
            sort_keys=True,
        ).encode('utf-8')
    ).hexdigest()[:16]
    cache_dir = (
        config.WORK_DIR / 'model-test-challenge-cache' / str(context['run']['id'])
    )
    cache_dir.mkdir(parents=True, exist_ok=True)
    destination = cache_dir / f'{sample["sample_id"]}-{cache_key}.jpg'
    if destination.is_file():
        return destination
    with Image.open(source) as image:
        image = image.convert('RGB')
        left = max(0, min(image.width - 1, round(float(crop['x']) * image.width)))
        top = max(0, min(image.height - 1, round(float(crop['y']) * image.height)))
        right = max(
            left + 1,
            min(
                image.width, round((float(crop['x']) + float(crop['w'])) * image.width)
            ),
        )
        bottom = max(
            top + 1,
            min(
                image.height,
                round((float(crop['y']) + float(crop['h'])) * image.height),
            ),
        )
        temporary = destination.with_name(f'.{destination.name}.{uuid4().hex}.tmp')
        image.crop((left, top, right, bottom)).save(
            temporary, format='JPEG', quality=95
        )
    os.replace(temporary, destination)
    return destination


def _sample_distribution(samples: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    result = {
        'result_panel': 0,
        'scoreboard': 0,
        'other_negative': 0,
        'scoreboard_by_mode': {'3v3': 0, '5v5': 0, 'aram': 0, 'unreadable': 0},
    }
    for sample in samples:
        groups = _sample_evaluation_groups(sample)
        expected = sample.get('expected')
        label = str(
            sample.get('label')
            or sample.get('detector_label')
            or (expected.get('label') if isinstance(expected, dict) else '')
            or ''
        )
        if label == 'result_panel' or 'result_panel' in groups:
            result['result_panel'] += 1
        elif 'scoreboard' in groups:
            result['scoreboard'] += 1
            mode = str(sample.get('evaluation_mode') or '')
            if mode not in {'3v3', '5v5', 'aram'}:
                mode = next(
                    (
                        group.split(':', 1)[1]
                        for group in groups
                        if group.startswith('scoreboard:')
                    ),
                    'unreadable',
                )
            if mode not in result['scoreboard_by_mode']:
                mode = 'unreadable'
            result['scoreboard_by_mode'][mode] += 1
        else:
            result['other_negative'] += 1
    return result


def run_sample_image_path(
    conn: Any, run_id: str, *, sample_id: str, split: str
) -> Path:
    context = _run_context(conn, run_id)
    if split == POST_RUN_CHALLENGE_SPLIT:
        sample = next(
            (
                value
                for value in _post_run_challenge_samples(conn, context)
                if value['sample_id'] == sample_id
            ),
            None,
        )
        if sample is None:
            raise KeyError(f'当前训练后挑战集中不存在样本: {sample_id}')
        return _challenge_image_path(context, sample)
    if split == SCOREBOARD_CHALLENGE_SPLIT:
        if str(context['run']['task_id']) != 'result_detector':
            raise ValueError('只有结算面板检测模型可以使用计分板难例库')
        if not sample_id.startswith('f') or not sample_id[1:].isdigit():
            raise ValueError('计分板难例样本标识无效')
        rows = _scoreboard_challenge_rows(conn, frame_id=int(sample_id[1:]))
        if not rows:
            raise KeyError(f'当前计分板难例中不存在样本: {sample_id}')
        path = Path(str(rows[0]['frame_path'])).resolve()
        if not path.is_file():
            raise FileNotFoundError(path)
        return path
    manifest = Path(context['dataset']['manifest_path'])
    sample = next(
        (
            value
            for value in _manifest_samples(manifest)
            if str(value.get('sample_id') or '') == sample_id
            and str(value.get('split') or '') == split
        ),
        None,
    )
    if sample is None:
        raise KeyError(f'训练快照中不存在样本: {sample_id}')
    try:
        return _sample_image_path(context, sample)
    except FileNotFoundError:
        frame_id = sample.get('frame_id')
        if frame_id is None and sample_id.startswith('f'):
            digits = sample_id[1:].split('-', 1)[0]
            if digits.isdigit():
                frame_id = int(digits)
        if frame_id is None:
            raise
        row = conn.execute(
            'SELECT frame_path FROM frames WHERE id = ?', (int(frame_id),)
        ).fetchone()
        if row is None:
            raise
        fallback = {
            '_frame_path': str(row['frame_path']),
            '_crop': sample.get('crop'),
            'sample_id': sample_id,
        }
        return _challenge_image_path(context, fallback)


def list_run_samples(
    conn: Any, run_id: str, *, split: str = 'test', limit: int = 500
) -> Dict[str, Any]:
    if split not in FIXED_DATASET_SPLITS | {
        SCOREBOARD_CHALLENGE_SPLIT,
        POST_RUN_CHALLENGE_SPLIT,
    }:
        raise ValueError('数据来源无效')
    context = _run_context(conn, run_id)
    if split == POST_RUN_CHALLENGE_SPLIT:
        selected = _post_run_challenge_samples(conn, context)
        capped = selected[: max(1, min(10_000, int(limit)))]
        by_label: Dict[str, int] = {}
        for sample in selected:
            expected = sample.get('expected')
            if isinstance(expected, dict):
                label = str(expected.get('label') or '')
            else:
                label = str(expected or '')
            by_label[label] = by_label.get(label, 0) + 1
        return {
            'run_id': run_id,
            'split': split,
            'total': len(selected),
            'items': [_public_challenge_sample(sample) for sample in capped],
            'distribution': {**_sample_distribution(selected), 'by_label': by_label},
            'is_fixed_snapshot': False,
            'cutoff_at': str(
                context['run'].get('finished_at')
                or context['run'].get('created_at')
                or ''
            ),
            'video_count': len({int(sample['video_id']) for sample in selected}),
            'new_video_count': len(
                {
                    int(sample['video_id'])
                    for sample in selected
                    if sample.get('is_new_video')
                }
            ),
        }
    if split == SCOREBOARD_CHALLENGE_SPLIT:
        if str(context['run']['task_id']) != 'result_detector':
            raise ValueError('只有结算面板检测模型可以使用计分板难例库')
        selected = [
            _scoreboard_challenge_sample(row)
            for row in _scoreboard_challenge_rows(conn)
        ]
        return {
            'run_id': run_id,
            'split': split,
            'total': len(selected),
            'items': selected[: max(1, min(10_000, int(limit)))],
            'distribution': _sample_distribution(selected),
            'is_fixed_snapshot': False,
        }
    manifest = Path(context['dataset']['manifest_path'])
    all_samples = _manifest_samples(manifest)
    selected = [sample for sample in all_samples if sample.get('split') == split]
    output = []
    for sample in selected[: max(1, min(10_000, int(limit)))]:
        sample_id = str(sample.get('sample_id') or '')
        frame_id: Optional[int] = None
        if sample_id.startswith('f'):
            digits = sample_id[1:].split('-', 1)[0]
            if digits.isdigit():
                frame_id = int(digits)
        if frame_id is None and sample.get('sha256'):
            row = conn.execute(
                'SELECT id FROM frames WHERE sha256 = ?', (str(sample['sha256']),)
            ).fetchone()
            frame_id = int(row['id']) if row else None
        expected: Any
        if context['metadata'].get('kind') == 'detect':
            role = TASK_ROLES.get(context['run']['task_id'])
            if role == 'mode_gate':
                box_key = 'mode_gate_boxes'
                raw_boxes = sample.get(box_key)
            elif role == 'hero_avatar':
                box_key = 'avatar_boxes'
                raw_boxes = sample.get(box_key)
            else:
                box_key = 'result_panel'
                raw_boxes = (sample.get('boxes') or {}).get(box_key)
            if isinstance(raw_boxes, dict):
                source_boxes = [raw_boxes]
            elif isinstance(raw_boxes, list):
                source_boxes = raw_boxes
            else:
                source_boxes = []
            expected_boxes = []
            for box in source_boxes:
                if not isinstance(box, dict):
                    continue
                try:
                    xywh = [
                        float(box['x']),
                        float(box['y']),
                        float(box['w']),
                        float(box['h']),
                    ]
                except (KeyError, TypeError, ValueError):
                    continue
                expected_boxes.append({'class': role or box_key, 'xywh_norm': xywh})
            expected = {
                'found': bool(expected_boxes),
                'label': sample.get('label') or sample.get('detector_label'),
                'boxes': expected_boxes,
            }
        else:
            expected = sample.get('label')
        try:
            _sample_image_path(context, sample)
            has_snapshot_image = True
        except (FileNotFoundError, ValueError):
            row = (
                conn.execute(
                    'SELECT frame_path FROM frames WHERE id = ?', (frame_id,)
                ).fetchone()
                if frame_id is not None
                else None
            )
            has_snapshot_image = bool(
                row is not None and Path(str(row['frame_path'])).is_file()
            )
        if isinstance(expected, dict):
            evaluation_scenario = sample.get('evaluation_scenario') or (
                sample.get('hero_screen_type')
                if role == 'hero_avatar'
                else (
                    'scoreboard'
                    if 'scoreboard' in _sample_evaluation_groups(sample)
                    else 'result_panel' if expected.get('found') else 'other_negative'
                )
            )
        else:
            evaluation_scenario = (
                sample.get('evaluation_scenario')
                or sample.get('hero_screen_type')
                or ''
            )
        output.append(
            {
                'sample_id': sample_id,
                'frame_id': frame_id,
                'split': split,
                'has_snapshot_image': has_snapshot_image,
                'expected': expected,
                'streamer': sample.get('streamer') or '',
                'timestamp_ms': int(sample.get('timestamp_ms') or 0),
                'visual_condition': sample.get('visual_condition') or 'clear',
                'evaluation_scenario': evaluation_scenario,
                'evaluation_mode': sample.get('evaluation_mode')
                or (sample.get('annotation') or {}).get('game_mode')
                or 'unreadable',
            }
        )
    return {
        'run_id': run_id,
        'split': split,
        'total': len(selected),
        'items': output,
        'distribution': _sample_distribution(selected),
        'is_fixed_snapshot': True,
    }


def worker_evaluation_plan(
    conn: Any, run_id: str, *, split: str = 'test'
) -> Dict[str, Any]:
    """生成轻量验收清单；原图和推理均由 Vision Worker 处理。"""
    context = _run_context(conn, run_id)
    listed = list_run_samples(conn, run_id, split=split, limit=10_000)
    private: Dict[str, Dict[str, Any]] = {}
    if split == POST_RUN_CHALLENGE_SPLIT:
        private = {
            str(value['sample_id']): value
            for value in _post_run_challenge_samples(conn, context)
        }
    elif split in FIXED_DATASET_SPLITS:
        private = {
            str(value.get('sample_id') or ''): value
            for value in _manifest_samples(Path(context['dataset']['manifest_path']))
            if value.get('split') == split
        }
    samples = []
    for public in listed['items']:
        sample_id = str(public.get('sample_id') or '')
        raw = private.get(sample_id) or {}
        frame_id = public.get('frame_id') or raw.get('frame_id')
        if frame_id is None and sample_id.startswith('f'):
            digits = sample_id[1:].split('-', 1)[0]
            if digits.isdigit():
                frame_id = int(digits)
        samples.append(
            {
                **public,
                'frame_id': int(frame_id) if frame_id is not None else None,
                'crop': raw.get('_crop') or raw.get('crop'),
            }
        )
    artifact = Path(context['artifact'])
    return {
        'run_id': run_id,
        'task_id': str(context['run']['task_id']),
        'kind': str(context['metadata'].get('kind') or ''),
        'split': split,
        'total': int(listed['total']),
        'samples': samples,
        'model': {
            'run_id': run_id,
            'metadata': context['metadata'],
            'artifact_size': int(artifact.stat().st_size),
        },
    }


def predict_run_sample(
    conn: Any, run_id: str, *, sample_id: str, split: str, conf_thr: float = 0.25
) -> Dict[str, Any]:
    context = _run_context(conn, run_id)
    frame_path = run_sample_image_path(conn, run_id, sample_id=sample_id, split=split)
    result = inference.run_artifact(
        context['artifact'],
        context['metadata'],
        frame_path,
        conf_thr=max(0.0, min(1.0, float(conf_thr))),
    )
    return {'run_id': run_id, 'sample_id': sample_id, 'split': split, **result}


def _normalized_box_iou(first: Dict[str, Any], second: Dict[str, Any]) -> float:
    left = first.get('xywh_norm')
    right = second.get('xywh_norm')
    if not (
        isinstance(left, list)
        and isinstance(right, list)
        and len(left) == 4
        and len(right) == 4
    ):
        return 0.0
    ax1, ay1, aw, ah = (float(value) for value in left)
    bx1, by1, bw, bh = (float(value) for value in right)
    ax2, ay2 = ax1 + aw, ay1 + ah
    bx2, by2 = bx1 + bw, by1 + bh
    intersection = max(0.0, min(ax2, bx2) - max(ax1, bx1)) * max(
        0.0, min(ay2, by2) - max(ay1, by1)
    )
    union = aw * ah + bw * bh - intersection
    return intersection / union if union > 0 else 0.0


def _best_detection_iou(
    expected: Sequence[Dict[str, Any]], predicted: Sequence[Dict[str, Any]]
) -> float:
    return max(
        (
            _normalized_box_iou(truth, guess)
            for truth in expected
            for guess in predicted
        ),
        default=0.0,
    )


def _detection_match_summary(
    expected: Sequence[Dict[str, Any]],
    predicted: Sequence[Dict[str, Any]],
    *,
    iou_threshold: float,
) -> Dict[str, Any]:
    """按 IoU 贪心一对一配对，避免多头像检测只命中一个也算正确。"""
    pairs = sorted(
        (
            (_normalized_box_iou(truth, guess), truth_index, guess_index)
            for truth_index, truth in enumerate(expected)
            for guess_index, guess in enumerate(predicted)
        ),
        reverse=True,
    )
    used_truth = set()
    used_guess = set()
    matched_ious = []
    for iou, truth_index, guess_index in pairs:
        if iou < iou_threshold:
            break
        if truth_index in used_truth or guess_index in used_guess:
            continue
        used_truth.add(truth_index)
        used_guess.add(guess_index)
        matched_ious.append(iou)
    return {
        'expected_count': len(expected),
        'predicted_count': len(predicted),
        'matched_count': len(matched_ious),
        'recall': len(matched_ious) / len(expected) if expected else 1.0,
        'precision': (
            len(matched_ious) / len(predicted)
            if predicted
            else (1.0 if not expected else 0.0)
        ),
        'mean_matched_iou': (
            sum(matched_ious) / len(matched_ious) if matched_ious else 0.0
        ),
    }


def _increment_report_group(
    groups: Dict[str, Dict[str, Any]], key: str, correct: bool
) -> None:
    value = groups.setdefault(key, {'total': 0, 'correct': 0, 'accuracy': 0.0})
    value['total'] += 1
    if correct:
        value['correct'] += 1


def _finalize_report_groups(groups: Dict[str, Dict[str, Any]]) -> None:
    for value in groups.values():
        total = int(value['total'])
        value['accuracy'] = round(int(value['correct']) / total, 6) if total else 0.0


def evaluation_report_from_predictions(
    *,
    run_id: str,
    task_id: str,
    kind: str,
    split: str,
    samples: Sequence[Dict[str, Any]],
    predictions: Dict[str, Dict[str, Any]],
    total: Optional[int] = None,
    conf_thr: float = 0.25,
    iou_threshold: float = 0.5,
    elapsed_seconds: float = 0.0,
) -> Dict[str, Any]:
    """根据 Worker 返回的逐样本结果生成与本机验收一致的报告。"""
    if kind not in {'classify', 'detect'}:
        raise ValueError(f'未知训练产物类型: {kind}')
    threshold = max(0.0, min(1.0, float(conf_thr)))
    required_iou = max(0.0, min(1.0, float(iou_threshold)))
    correct = 0
    evaluated = 0
    errors: List[Dict[str, Any]] = []
    failures: List[Dict[str, Any]] = []
    confusion: Dict[str, Dict[str, int]] = {}
    by_label: Dict[str, Dict[str, Any]] = {}
    by_scenario: Dict[str, Dict[str, Any]] = {}
    by_mode: Dict[str, Dict[str, Any]] = {}
    for sample in samples:
        sample_id = str(sample.get('sample_id') or '')
        outcome = predictions.get(sample_id) or {}
        result = outcome.get('result')
        if not isinstance(result, dict):
            failures.append(
                {
                    'sample_id': sample_id,
                    'frame_id': sample.get('frame_id'),
                    'error': str(outcome.get('error') or 'Worker 没有返回结果'),
                }
            )
            continue
        evaluated += 1
        if kind == 'classify':
            expected = str(sample.get('expected') or '')
            top1 = result.get('top1')
            if isinstance(top1, dict):
                predicted = str(top1.get('class') or '')
                confidence = float(top1.get('prob') or 0)
            else:
                predicted = str(top1 or '')
                confidence = 0.0
            matched = predicted == expected
            _increment_report_group(by_label, expected, matched)
            confusion.setdefault(expected, {})[predicted] = (
                confusion.setdefault(expected, {}).get(predicted, 0) + 1
            )
            error = {
                'sample_id': sample_id,
                'frame_id': sample.get('frame_id'),
                'expected': expected,
                'predicted': predicted,
                'confidence': confidence,
                'reason': '分类答案不一致',
            }
        else:
            expected_value = sample.get('expected') or {}
            expected_found = bool(expected_value.get('found'))
            predicted_found = bool(result.get('found'))
            expected_boxes = expected_value.get('boxes') or []
            predicted_boxes = result.get('detections') or []
            best_iou = _best_detection_iou(expected_boxes, predicted_boxes)
            avatar_summary: Optional[Dict[str, Any]] = None
            if task_id == 'hero_avatar_detector':
                avatar_summary = _detection_match_summary(
                    expected_boxes, predicted_boxes, iou_threshold=required_iou
                )
                matched = (
                    avatar_summary['expected_count']
                    == avatar_summary['predicted_count']
                    == avatar_summary['matched_count']
                )
            else:
                matched = expected_found == predicted_found
                if expected_found and predicted_found:
                    matched = matched and best_iou >= required_iou
            expected = 'found' if expected_found else 'not_found'
            predicted = 'found' if predicted_found else 'not_found'
            _increment_report_group(by_label, expected, matched)
            confusion.setdefault(expected, {})[predicted] = (
                confusion.setdefault(expected, {}).get(predicted, 0) + 1
            )
            scenario = str(sample.get('evaluation_scenario') or 'other_negative')
            _increment_report_group(by_scenario, scenario, matched)
            mode = str(sample.get('evaluation_mode') or 'unreadable')
            if scenario == 'scoreboard':
                _increment_report_group(by_mode, mode, matched)
            if avatar_summary is not None and not matched:
                reason = '头像数量或位置不正确'
            elif expected_found and not predicted_found:
                reason = '漏掉应有目标'
            elif not expected_found and predicted_found:
                reason = '负样本误报'
            elif expected_found and best_iou < required_iou:
                reason = '预测框位置不准'
            else:
                reason = '检测答案不一致'
            error = {
                'sample_id': sample_id,
                'frame_id': sample.get('frame_id'),
                'expected': expected,
                'predicted': predicted,
                'best_iou': round(best_iou, 6),
                'reason': reason,
                'evaluation_scenario': scenario,
                'evaluation_mode': mode,
                **(
                    {
                        key: (
                            round(float(value), 6)
                            if isinstance(value, float)
                            else value
                        )
                        for key, value in avatar_summary.items()
                    }
                    if avatar_summary is not None
                    else {}
                ),
            }
        if matched:
            correct += 1
        else:
            errors.append(error)
    _finalize_report_groups(by_label)
    _finalize_report_groups(by_scenario)
    _finalize_report_groups(by_mode)
    reported_total = len(samples) if total is None else int(total)
    return {
        'run_id': run_id,
        'task_id': task_id,
        'kind': kind,
        'split': split,
        'total': reported_total,
        'evaluated': evaluated,
        'correct': correct,
        'accuracy': round(correct / evaluated, 6) if evaluated else 0.0,
        'failed': len(failures),
        'truncated': reported_total > len(samples),
        'conf_thr': threshold,
        'iou_threshold': required_iou if kind == 'detect' else None,
        'elapsed_seconds': round(float(elapsed_seconds), 3),
        'by_label': by_label,
        'by_scenario': by_scenario,
        'scoreboard_by_mode': by_mode,
        'confusion': confusion,
        'errors': errors,
        'failures': failures,
    }


def evaluate_run_samples(
    conn: Any,
    run_id: str,
    *,
    split: str = 'test',
    conf_thr: float = 0.25,
    iou_threshold: float = 0.5,
) -> Dict[str, Any]:
    """对人工真值切分批量推理，并返回可定位错例的结构化报告。"""
    if split not in FIXED_DATASET_SPLITS | {
        SCOREBOARD_CHALLENGE_SPLIT,
        POST_RUN_CHALLENGE_SPLIT,
    }:
        raise ValueError('数据来源无效')
    context = _run_context(conn, run_id)
    maximum = 10_000
    listed = list_run_samples(conn, run_id, split=split, limit=maximum)
    samples = list(listed['items'])
    raw_by_id: Dict[str, Dict[str, Any]] = {}
    challenge_paths: Dict[str, Path] = {}
    if split == POST_RUN_CHALLENGE_SPLIT:
        challenge_paths = {
            sample['sample_id']: _challenge_image_path(context, sample)
            for sample in _post_run_challenge_samples(conn, context)
        }
    elif split == SCOREBOARD_CHALLENGE_SPLIT:
        for row in _scoreboard_challenge_rows(conn):
            sample_id = f'f{int(row["frame_id"]):08d}'
            challenge_paths[sample_id] = Path(str(row['frame_path'])).resolve()
    else:
        manifest = Path(context['dataset']['manifest_path'])
        raw_by_id = {
            str(sample.get('sample_id') or ''): sample
            for sample in _manifest_samples(manifest)
            if sample.get('split') == split
        }
    threshold = max(0.0, min(1.0, float(conf_thr)))
    kind = str(context['metadata'].get('kind') or '')
    if kind not in {'classify', 'detect'}:
        raise ValueError(f'未知训练产物类型: {kind}')
    predictions: Dict[str, Dict[str, Any]] = {}
    started = perf_counter()
    for sample in samples:
        sample_id = str(sample.get('sample_id') or '')
        try:
            if split in {SCOREBOARD_CHALLENGE_SPLIT, POST_RUN_CHALLENGE_SPLIT}:
                frame_path = challenge_paths[sample_id]
                if not frame_path.is_file():
                    raise FileNotFoundError(frame_path)
            else:
                raw = raw_by_id.get(sample_id)
                if raw is None:
                    raise KeyError(sample_id)
                frame_path = _sample_image_path(context, raw)
            result = inference.run_artifact(
                context['artifact'], context['metadata'], frame_path, conf_thr=threshold
            )
        except Exception as exc:  # noqa: BLE001
            predictions[sample_id] = {'error': str(exc)}
        else:
            predictions[sample_id] = {'result': result}
    return evaluation_report_from_predictions(
        run_id=run_id,
        task_id=str(context['run']['task_id']),
        kind=kind,
        split=split,
        samples=samples,
        predictions=predictions,
        total=int(listed['total']),
        conf_thr=threshold,
        iou_threshold=iou_threshold,
        elapsed_seconds=perf_counter() - started,
    )


def validate_run(
    conn: Any, run_id: str, *, status: str, notes: str = ''
) -> Dict[str, Any]:
    return db.set_model_validation(conn, run_id=run_id, status=status, notes=notes)


def _class_names(metadata: Dict[str, Any]) -> List[str]:
    value = metadata.get('classes') or {}
    if isinstance(value, dict):
        return [
            str(label)
            for _index, label in sorted(value.items(), key=lambda item: int(item[0]))
        ]
    if isinstance(value, list):
        return [str(label) for label in value]
    return []


def _sample_evaluation_groups(sample: Dict[str, Any]) -> set:
    groups = sample.get('evaluation_groups') or []
    if isinstance(groups, str):
        groups = [groups]
    result = {str(group) for group in groups if group}
    expected = sample.get('expected')
    label = str(
        sample.get('label')
        or sample.get('detector_label')
        or (expected.get('label') if isinstance(expected, dict) else '')
        or ''
    )
    if label == 'result_panel':
        result.add('result_panel')
    annotation = sample.get('annotation') or {}
    scenario = str(sample.get('evaluation_scenario') or '')
    screen_type = str(annotation.get('screen_type') or '')
    if scenario == 'scoreboard' or screen_type in {'scoreboard', 'death_scoreboard'}:
        result.add('scoreboard')
        mode = str(sample.get('evaluation_mode') or annotation.get('game_mode') or '')
        if mode in {'3v3', '5v5', 'aram'}:
            result.add(f'scoreboard:{mode}')
    return result


def _evaluation_gaps(context: Dict[str, Any]) -> List[str]:
    """检查固定测试集是否足以支持“可部署”结论。"""
    all_samples = _manifest_samples(Path(context['dataset']['manifest_path']))
    samples = [sample for sample in all_samples if sample.get('split') == 'test']
    if not samples:
        return ['固定测试集为空']
    kind = str(context['metadata'].get('kind') or '')
    if kind == 'classify':
        expected = set(_class_names(context['metadata']))
        present = {str(sample.get('label') or '') for sample in samples}
        missing = sorted(expected - present)
        return [f'固定测试集缺少类别 {label}' for label in missing]
    if kind == 'detect':
        task_id = str(context['run']['task_id'])
        positive_label = {
            'mode_gate': 'blocked_gate',
            'hero_avatar_detector': 'hero_avatar',
        }.get(task_id, 'result_panel')
        labels = {
            str(sample.get('label') or sample.get('detector_label') or '')
            for sample in samples
        }
        gaps = []
        if positive_label not in labels:
            gaps.append('固定测试集没有带框正样本')
        if task_id != 'hero_avatar_detector' and not any(
            label and label != positive_label for label in labels
        ):
            gaps.append('固定测试集没有无框负样本')
        if task_id == 'hero_avatar_detector':
            screen_types = {
                str(sample.get('hero_screen_type') or '') for sample in samples
            }
            names = {
                'gameplay_hud': 'HUD',
                'scoreboard': '积分板',
                'result_page': '结算界面',
            }
            for screen_type, name in names.items():
                if screen_type not in screen_types:
                    gaps.append(f'固定测试集缺少：{name}头像定位')
        if task_id == 'result_detector':
            available_groups = set().union(
                *(_sample_evaluation_groups(sample) for sample in all_samples)
            )
            test_groups = set().union(
                *(_sample_evaluation_groups(sample) for sample in samples)
            )
            if 'scoreboard' in available_groups and 'scoreboard' not in test_groups:
                gaps.append('固定测试集没有计分板难例')
            mode_labels = {'3v3': '3V3', '5v5': '5V5', 'aram': '大乱斗'}
            for mode, mode_label in mode_labels.items():
                group = f'scoreboard:{mode}'
                if group in available_groups and group not in test_groups:
                    suffix = '计分板' if mode == 'aram' else ' 计分板'
                    gaps.append(f'固定测试集缺少：{mode_label}{suffix}')
        return gaps
    return [f'未知训练产物类型 {kind}']


def prepare_model_package(
    conn: Any, run_ids: Sequence[str], *, package_id: str = ''
) -> Dict[str, Any]:
    """生成可序列化组包计划；模型复制、哈希和 ZIP 由 Worker 完成。"""
    if not run_ids:
        raise ValueError('至少选择一个已经验收通过的训练 run')
    contexts: Dict[str, Dict[str, Any]] = {}
    for run_id in run_ids:
        context = _run_context(conn, str(run_id))
        task_id = str(context['run']['task_id'])
        if task_id not in TASK_ROLES:
            raise ValueError(f'训练任务不能进入 Worker 模型包: {task_id}')
        validation = db.get_model_validation(conn, str(run_id))
        if validation is None or validation['status'] != 'passed':
            raise ValueError(f'模型尚未验收通过: {run_id}')
        if task_id in contexts:
            raise ValueError(f'同一个模型角色只能选择一个 run: {task_id}')
        contexts[task_id] = context
    missing = [task for task in REQUIRED_TASKS if task not in contexts]
    evaluation_gaps = {
        TASK_ROLES[task_id]: gaps
        for task_id, context in contexts.items()
        if (gaps := _evaluation_gaps(context))
    }
    status = 'ready' if not missing and not evaluation_gaps else 'incomplete'
    if not package_id:
        package_id = 'vg-vision-{}-{}'.format(
            datetime.now().strftime('%Y%m%d-%H%M%S'), uuid4().hex[:6]
        )
    if not package_id.replace('-', '').replace('_', '').isalnum():
        raise ValueError('模型包 ID 只能包含字母、数字、连字符和下划线')
    package_root = config.WORK_DIR / 'model-packages'
    destination = package_root / package_id
    archive = package_root / f'{package_id}.zip'
    if (
        destination.exists()
        or archive.exists()
        or conn.execute(
            'SELECT 1 FROM model_packages WHERE id = ?', (package_id,)
        ).fetchone()
    ):
        raise ValueError(f'模型包 ID 已存在: {package_id}')

    models: Dict[str, Any] = {}
    dataset_lock: Dict[str, Any] = {}
    metrics: Dict[str, Any] = {}
    for task_id, context in contexts.items():
        role = TASK_ROLES[task_id]
        metadata = context['metadata']
        classes = _class_names(metadata)
        kind = str(metadata.get('kind'))
        recorded_input = metadata.get('input') or {}
        preprocessing = metadata.get('preprocessing') or {}
        default_size = int(metadata.get('imgsz') or 224)
        artifact = Path(context['artifact'])
        models[role] = {
            'task_id': task_id,
            'run_id': str(context['run']['id']),
            'artifact_size': int(artifact.stat().st_size),
            'metadata': metadata,
            'manifest': {
                'file': f'models/{role}.onnx',
                'kind': 'classification' if kind == 'classify' else 'detection',
                'input': {
                    'width': int(recorded_input.get('width') or default_size),
                    'height': int(recorded_input.get('height') or default_size),
                    'color': str(preprocessing.get('color') or 'RGB'),
                    'resize': str(
                        preprocessing.get('resize')
                        or (
                            'shortest_edge_center_crop'
                            if kind == 'classify'
                            else 'letterbox'
                        )
                    ),
                    'pad_value': preprocessing.get('pad_value'),
                    'preserve_full_image': bool(
                        preprocessing.get('preserve_full_image', False)
                    ),
                    'scale': str(preprocessing.get('scale') or '0_to_1'),
                    'normalize': str(
                        preprocessing.get('normalize')
                        or ('imagenet' if kind == 'classify' else 'none')
                    ),
                },
                'training_augmentation': preprocessing.get('training_augmentation'),
                'classes': classes,
                'dataset_version': context['run']['dataset_version_id'],
                'training_run_id': context['run']['id'],
            },
        }
        dataset_manifest = Path(context['dataset']['manifest_path'])
        dataset_lock[role] = {
            'dataset_version': context['run']['dataset_version_id'],
            'manifest_sha256': _sha256(dataset_manifest),
            'counts': json.loads(context['dataset']['counts_json'] or '{}'),
        }
        metrics[role] = context['run']['metrics_json']
    manifest = {
        'schema_version': 2,
        'package_id': package_id,
        'pipeline_version': 'timeline-v2',
        'status': status,
        'missing_roles': [TASK_ROLES[task] for task in missing],
        'evaluation_gaps': evaluation_gaps,
        'models': {},
        'runtime': {
            'coarse_interval_ms': 60_000,
            'maximum_keyframe_distance_ms': 5_000,
            'result_scan_fps': 4,
            'result_window_before_ms': 40_000,
            'result_window_after_ms': 25_000,
            'thresholds': {
                'match_flow': 0.55,
                'hero_select': 0.55,
                'match_mode': 0.50,
                'result_panel': 0.55,
                'hero_avatar': 0.25,
                'hero_identity': 0.50,
                'player_position': 0.50,
            },
        },
        'compatibility': {
            'analysis_protocol_version': 2,
            'product': 'blrec-analysis-worker',
        },
    }
    return {
        'package_id': package_id,
        'status': status,
        'missing_tasks': missing,
        'evaluation_gaps': evaluation_gaps,
        'models': models,
        'manifest': manifest,
        'dataset_lock': dataset_lock,
        'metrics': metrics,
    }


def build_model_package(
    conn: Any, run_ids: Sequence[str], *, package_id: str = ''
) -> Dict[str, Any]:
    if not run_ids:
        raise ValueError('至少选择一个已经验收通过的训练 run')
    contexts: Dict[str, Dict[str, Any]] = {}
    for run_id in run_ids:
        context = _run_context(conn, str(run_id))
        task_id = str(context['run']['task_id'])
        if task_id not in TASK_ROLES:
            raise ValueError(f'训练任务不能进入 Worker 模型包: {task_id}')
        validation = db.get_model_validation(conn, str(run_id))
        if validation is None or validation['status'] != 'passed':
            raise ValueError(f'模型尚未验收通过: {run_id}')
        if task_id in contexts:
            raise ValueError(f'同一个模型角色只能选择一个 run: {task_id}')
        contexts[task_id] = context

    missing = [task for task in REQUIRED_TASKS if task not in contexts]
    evaluation_gaps = {
        TASK_ROLES[task_id]: gaps
        for task_id, context in contexts.items()
        if (gaps := _evaluation_gaps(context))
    }
    status = 'ready' if not missing and not evaluation_gaps else 'incomplete'
    if not package_id:
        package_id = 'vg-vision-{}-{}'.format(
            datetime.now().strftime('%Y%m%d-%H%M%S'), uuid4().hex[:6]
        )
    if not package_id.replace('-', '').replace('_', '').isalnum():
        raise ValueError('模型包 ID 只能包含字母、数字、连字符和下划线')
    package_root = config.WORK_DIR / 'model-packages'
    package_root.mkdir(parents=True, exist_ok=True)
    destination = package_root / package_id
    if (
        destination.exists()
        or conn.execute(
            'SELECT 1 FROM model_packages WHERE id = ?', (package_id,)
        ).fetchone()
    ):
        raise ValueError(f'模型包 ID 已存在: {package_id}')

    temporary = Path(tempfile.mkdtemp(prefix='.model-package-', dir=package_root))
    try:
        models_dir = temporary / 'models'
        models_dir.mkdir()
        manifest_models: Dict[str, Any] = {}
        dataset_lock: Dict[str, Any] = {}
        metrics: Dict[str, Any] = {}
        for task_id, context in contexts.items():
            role = TASK_ROLES[task_id]
            artifact: Path = context['artifact']
            target = models_dir / f'{role}.onnx'
            shutil.copy2(artifact, target)
            metadata = context['metadata']
            classes = _class_names(metadata)
            kind = str(metadata.get('kind'))
            recorded_input = metadata.get('input') or {}
            preprocessing = metadata.get('preprocessing') or {}
            default_size = int(metadata.get('imgsz') or 224)
            manifest_models[role] = {
                'file': f'models/{target.name}',
                'sha256': _sha256(target),
                'kind': 'classification' if kind == 'classify' else 'detection',
                'input': {
                    'width': int(recorded_input.get('width') or default_size),
                    'height': int(recorded_input.get('height') or default_size),
                    'color': str(preprocessing.get('color') or 'RGB'),
                    'resize': str(
                        preprocessing.get('resize')
                        or (
                            'shortest_edge_center_crop'
                            if kind == 'classify'
                            else 'letterbox'
                        )
                    ),
                    'pad_value': preprocessing.get('pad_value'),
                    'preserve_full_image': bool(
                        preprocessing.get('preserve_full_image', False)
                    ),
                    'scale': str(preprocessing.get('scale') or '0_to_1'),
                    'normalize': str(
                        preprocessing.get('normalize')
                        or ('imagenet' if kind == 'classify' else 'none')
                    ),
                },
                'training_augmentation': preprocessing.get('training_augmentation'),
                'classes': classes,
                'dataset_version': context['run']['dataset_version_id'],
                'training_run_id': context['run']['id'],
            }
            dataset_manifest = Path(context['dataset']['manifest_path'])
            dataset_lock[role] = {
                'dataset_version': context['run']['dataset_version_id'],
                'manifest_sha256': _sha256(dataset_manifest),
                'counts': json.loads(context['dataset']['counts_json'] or '{}'),
            }
            metrics[role] = context['run']['metrics_json']
        manifest = {
            'schema_version': 2,
            'package_id': package_id,
            'pipeline_version': 'timeline-v2',
            'status': status,
            'missing_roles': [TASK_ROLES[task] for task in missing],
            'evaluation_gaps': evaluation_gaps,
            'models': manifest_models,
            'runtime': {
                'coarse_interval_ms': 60_000,
                'maximum_keyframe_distance_ms': 5_000,
                'result_scan_fps': 4,
                'result_window_before_ms': 40_000,
                'result_window_after_ms': 25_000,
                'thresholds': {
                    'match_flow': 0.55,
                    'hero_select': 0.55,
                    'match_mode': 0.50,
                    'result_panel': 0.55,
                    'hero_avatar': 0.25,
                    'hero_identity': 0.50,
                    'player_position': 0.50,
                },
            },
            'compatibility': {
                'analysis_protocol_version': 2,
                'product': 'blrec-analysis-worker',
            },
        }
        (temporary / 'manifest.json').write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8'
        )
        (temporary / 'dataset-lock.json').write_text(
            json.dumps(dataset_lock, ensure_ascii=False, indent=2), encoding='utf-8'
        )
        (temporary / 'metrics.json').write_text(
            json.dumps(metrics, ensure_ascii=False, indent=2), encoding='utf-8'
        )
        os.replace(temporary, destination)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
    db.create_model_package(
        conn,
        package_id=package_id,
        status=status,
        path=str(destination),
        manifest=manifest,
    )
    return {
        'id': package_id,
        'status': status,
        'path': str(destination),
        'missing_tasks': missing,
        'evaluation_gaps': evaluation_gaps,
        'manifest': manifest,
    }


def model_package_archive(conn: Any, package_id: str) -> Path:
    row = conn.execute(
        'SELECT path FROM model_packages WHERE id = ?', (package_id,)
    ).fetchone()
    if row is None:
        raise KeyError(f'模型包不存在: {package_id}')
    path = Path(row['path']).resolve()
    root = (config.WORK_DIR / 'model-packages').resolve()
    try:
        path.relative_to(root)
    except ValueError as error:
        raise ValueError('模型包路径不在 Vision Lab 工作目录内') from error
    if path.is_file() and path.suffix == '.zip':
        return path
    if not path.is_dir():
        raise FileNotFoundError(path)
    archive = root / f'{package_id}.zip'
    if not archive.is_file():
        shutil.make_archive(
            str(archive.with_suffix('')),
            'zip',
            root_dir=path.parent,
            base_dir=path.name,
        )
    return archive
