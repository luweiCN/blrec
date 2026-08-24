"""数据集导出与版本管理。

- 结算检测数据集:单类 result_panel,正样本必须有 result_panel_bbox,
  负样本含随机背景与积分板 hard negative;支持 JSONL / YOLO / COCO。
- 切分以整段视频为单位(同一视频/同一事件的帧只属于一个集合),防泄漏。
- 每次导出生成不可变版本 result-detector-vN,禁止覆盖。
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from statistics import median
from typing import Any, Dict, List, Optional, Tuple

from PIL import Image

from . import config, db, managed_assets

PLAYER_POSITION_LABELS = (
    'left1',
    'left2',
    'left3',
    'left4',
    'left5',
    'right1',
    'right2',
    'right3',
)


def _git_commit() -> str:
    try:
        out = subprocess.run(
            [
                'git',
                '-C',
                str(Path(__file__).resolve().parent.parent.parent.parent),
                'rev-parse',
                '--short',
                'HEAD',
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return out.stdout.strip() if out.returncode == 0 else ''
    except Exception:  # noqa: BLE001
        return ''


def next_version_id(conn: Any, task_id: str) -> str:
    prefix = {
        'result_detector': 'result-detector',
        'game_state': 'game-state',
        'game_mode': 'game-mode',
        'match_flow': 'match-flow-classifier',
        'match_mode': 'match-mode-classifier',
        'hero_select': 'hero-select-classifier',
        'hero_avatar_detector': 'hero-avatar-detector',
        'hero_identity': 'hero-identity-classifier',
        'player_position': 'player-position-classifier',
        'screen_state': 'screen-state',
        'bp_review': 'bp-classifier',
        'key_screen_review': 'key-screen-classifier',
        'mode_gate': 'mode-gate-detector',
        'viewport': 'viewport',
        'same_match': 'same-match',
    }.get(task_id, task_id)
    existing = [
        r['id'] for r in conn.execute('SELECT id FROM dataset_versions').fetchall()
    ]
    if config.EXPORT_DIR.is_dir():
        existing.extend(path.name for path in config.EXPORT_DIR.iterdir())
    nums = []
    for eid in existing:
        m = re.match(rf'^{re.escape(prefix)}-v(\d+)$', eid)
        if m:
            nums.append(int(m.group(1)))
    n = max(nums, default=0) + 1
    return f'{prefix}-v{n}'


# ---------- 样本收集 ----------


def _frame_sample_metadata(
    conn: Any,
) -> Tuple[Dict[int, Dict[str, Any]], Dict[int, Dict[str, Dict[str, float]]]]:
    """批量读取帧标注与框，避免远程数据库逐帧往返。"""
    annotations: Dict[int, Dict[str, Any]] = {}
    for row in conn.execute('SELECT * FROM annotations').fetchall():
        annotation = dict(row)
        annotation['quality_flags'] = json.loads(annotation['quality_flags'] or '[]')
        annotation['occluder_types'] = json.loads(annotation['occluder_types'] or '[]')
        annotations[int(annotation['frame_id'])] = annotation

    boxes: Dict[int, Dict[str, Dict[str, float]]] = {}
    for row in conn.execute(
        'SELECT frame_id, box_type, x, y, w, h FROM boxes'
    ).fetchall():
        box = dict(row)
        frame_id = int(box.pop('frame_id'))
        boxes.setdefault(frame_id, {})[str(box['box_type'])] = box
    return annotations, boxes


def _frame_sample(
    conn: Any,
    f: Dict[str, Any],
    *,
    annotations: Optional[Dict[int, Dict[str, Any]]] = None,
    boxes: Optional[Dict[int, Dict[str, Dict[str, float]]]] = None,
) -> Optional[Dict[str, Any]]:
    """组装单帧的 JSONL 样本;缺原始文件返回 None。"""
    if not managed_assets.frame_available(f['frame_path']):
        return None
    frame_id = int(f['id'])
    ann = (
        annotations.get(frame_id, {})
        if annotations is not None
        else (db.get_annotation(conn, frame_id) or {})
    )
    frame_boxes = (
        boxes.get(frame_id, {}) if boxes is not None else db.get_boxes(conn, frame_id)
    )
    return {
        'sample_id': f'f{f["id"]:08d}',
        'frame_id': int(f['id']),
        'video_id': f['video_id'],
        'streamer': f['streamer'],
        'remote_path': f['remote_path'],
        'timestamp_ms': f['timestamp_ms'],
        'part_index': f['part_index'],
        'part_offset_ms': f['part_offset_ms'],
        'session_offset_ms': f['session_offset_ms'],
        'width': f['width'],
        'height': f['height'],
        'sha256': f['sha256'],
        'phash': f['phash'],
        'event_id': f['event_id'],
        'strategy': f['strategy'],
        'model_source': f['model_source'],
        'model_confidence': f['model_confidence'],
        'is_representative': bool(f['is_representative']),
        'annotation': {
            'content_family': ann.get('content_family'),
            'non_vainglory_type': ann.get('non_vainglory_type'),
            'game_context': ann.get('game_context'),
            'screen_type': ann.get('screen_type'),
            'game_mode': ann.get('game_mode'),
            'match_kind': ann.get('match_kind', 'unknown'),
            'view_context': ann.get('view_context', 'unknown'),
            'quality_flags': ann.get('quality_flags', []),
            'black_bars': ann.get('black_bars', 'none'),
            'ocr_usable': ann.get('ocr_usable'),
            'notes': ann.get('notes', ''),
        },
        'boxes': frame_boxes,
        'label_version': ann.get('label_version', 'v1'),
    }


def _labeled_frames(conn: Any) -> List[Dict[str, Any]]:
    rows = conn.execute(
        'SELECT f.*, v.streamer, v.remote_path FROM frames f '
        'JOIN videos v ON v.id = f.video_id WHERE f.labeled = 1 '
        'ORDER BY f.video_id, f.timestamp_ms'
    ).fetchall()
    return [dict(r) for r in rows]


# ---------- 切分(按视频,防泄漏) ----------


def split_by_video(
    video_ids: List[int], ratio: Tuple[float, float, float] = (0.8, 0.1, 0.1)
) -> Dict[str, List[int]]:
    """按视频分配集合;同一视频的全部帧/事件进入同一集合。"""
    import random

    rng = random.Random(42)
    ids = list(video_ids)
    rng.shuffle(ids)
    n = len(ids)
    if n == 1:
        return {'train': ids, 'val': [], 'test': []}
    if n == 2:
        return {'train': ids[:1], 'val': ids[1:], 'test': []}
    n_val = max(1, int(n * ratio[1]))
    n_test = max(1, int(n * ratio[2]))
    n_train = max(1, n - n_val - n_test)
    if n_train + n_val + n_test > n:
        n_test = max(0, n - n_train - n_val)
    return {
        'train': ids[:n_train],
        'val': ids[n_train : n_train + n_val],
        'test': ids[n_train + n_val : n_train + n_val + n_test],
    }


def split_classification_by_video(
    samples: List[Dict[str, Any]], labels: Tuple[str, ...]
) -> Dict[str, List[int]]:
    """按视频切分，并尽量让每个类别都出现在 train/val。

    仍以整段视频为最小单位，只会把整个视频从 train 移到
    val/test；不会为了补类别而造成同视频泄漏。
    """
    video_labels: Dict[int, set] = {}
    video_sizes: Dict[int, int] = {}
    for sample in samples:
        video_id = int(sample['video_id'])
        video_labels.setdefault(video_id, set()).add(str(sample['label']))
        video_sizes[video_id] = video_sizes.get(video_id, 0) + 1
    split = split_by_video(sorted(video_labels))

    # 先确保训练集本身有所有类别；极小数据下可能因此缩小验证集。
    for label in labels:
        if any(label in video_labels[video_id] for video_id in split['train']):
            continue
        source_name = next(
            (
                name
                for name in ('val', 'test')
                if any(label in video_labels[video_id] for video_id in split[name])
            ),
            None,
        )
        if source_name is None:
            continue
        video_id = min(
            (
                video_id
                for video_id in split[source_name]
                if label in video_labels[video_id]
            ),
            key=lambda item: (video_sizes[item], item),
        )
        split[source_name].remove(video_id)
        split['train'].append(video_id)

    # 验证集优先覆盖所有类别；测试集只在训练集仍有同类
    # 其他视频时补齐，防止稀有类从训练集消失。
    for target_name in ('val', 'test'):
        while True:
            missing = {
                label
                for label in labels
                if not any(
                    label in video_labels[video_id] for video_id in split[target_name]
                )
            }
            if not missing:
                break
            eligible = []
            source_names = ('test', 'train') if target_name == 'val' else ('train',)
            for source_name in source_names:
                for video_id in split[source_name]:
                    provided = video_labels[video_id] & missing
                    if not provided:
                        continue
                    if source_name == 'train':
                        remaining = [
                            other for other in split['train'] if other != video_id
                        ]
                        if not all(
                            any(label in video_labels[other] for other in remaining)
                            for label in video_labels[video_id]
                        ):
                            continue
                    eligible.append(
                        (
                            len(provided),
                            int(source_name != 'train'),
                            -video_sizes[video_id],
                            -video_id,
                            source_name,
                            video_id,
                        )
                    )
            if not eligible:
                break
            *_, source_name, video_id = max(eligible)
            split[source_name].remove(video_id)
            split[target_name].append(video_id)
    return {name: sorted(video_ids) for name, video_ids in split.items()}


def split_detection_by_video(
    samples: List[Dict[str, Any]],
    *,
    label_field: str,
    positive_label: str,
    evaluation_group_field: str = '',
    required_test_groups: Tuple[str, ...] = (),
    required_val_groups: Tuple[str, ...] = (),
) -> Dict[str, List[int]]:
    """按视频切分，并保证检测正负样本与指定难例组的覆盖。"""
    video_labels: Dict[int, set] = {}
    video_counts: Dict[int, Dict[str, int]] = {}
    video_groups: Dict[int, set] = {}
    video_sizes: Dict[int, int] = {}
    for sample in samples:
        video_id = int(sample['video_id'])
        label = str(sample[label_field])
        video_labels.setdefault(video_id, set()).add(label)
        video_sizes[video_id] = video_sizes.get(video_id, 0) + 1
        groups = (
            sample.get(evaluation_group_field, []) if evaluation_group_field else []
        )
        if isinstance(groups, str):
            groups = [groups]
        video_groups.setdefault(video_id, set()).update(
            str(group) for group in groups if group
        )
        kind = positive_label if label == positive_label else '__negative__'
        counts = video_counts.setdefault(
            video_id, {positive_label: 0, '__negative__': 0}
        )
        counts[kind] += 1
    split = split_by_video(sorted(video_labels))
    required = {positive_label, '__negative__'}

    def evidence(video_id: int) -> set:
        labels = video_labels[video_id]
        values = {positive_label} if positive_label in labels else set()
        if any(label != positive_label for label in labels):
            values.add('__negative__')
        return values

    while True:
        present = set().union(*(evidence(video_id) for video_id in split['train']))
        missing = required - present
        if not missing:
            break
        choice = next(
            (
                (source, video_id)
                for source in ('val', 'test')
                for video_id in split[source]
                if evidence(video_id) & missing
            ),
            None,
        )
        if choice is None:
            break
        source, video_id = choice
        split[source].remove(video_id)
        split['train'].append(video_id)
    totals = {
        kind: sum(counts[kind] for counts in video_counts.values()) for kind in required
    }
    minimums = {
        kind: min(100, max(2, round(total * 0.2))) for kind, total in totals.items()
    }
    for kind in (positive_label, '__negative__'):
        while (
            sum(video_counts[video_id][kind] for video_id in split['train'])
            < minimums[kind]
        ):
            current = sum(video_counts[video_id][kind] for video_id in split['train'])
            deficit = minimums[kind] - current
            choices = [
                (video_counts[video_id][kind], source, video_id)
                for source in ('val', 'test')
                for video_id in split[source]
                if video_counts[video_id][kind] > 0
            ]
            if not choices:
                break
            enough = [choice for choice in choices if choice[0] >= deficit]
            if enough:
                _count, source, video_id = min(
                    enough,
                    key=lambda choice: (choice[0], choice[1] != 'test', choice[2]),
                )
            else:
                _count, source, video_id = max(
                    choices,
                    key=lambda choice: (choice[0], choice[1] == 'test', -choice[2]),
                )
            split[source].remove(video_id)
            split['train'].append(video_id)

    requested_groups = set(required_test_groups) | set(required_val_groups)
    group_videos = {
        group: {
            video_id for video_id, groups in video_groups.items() if group in groups
        }
        for group in requested_groups
    }
    train_required = {
        group for group, video_ids in group_videos.items() if len(video_ids) >= 2
    }

    def target_groups(name: str) -> set:
        return (
            set().union(
                *(video_groups.get(video_id, set()) for video_id in split[name])
            )
            if split[name]
            else set()
        )

    def train_can_release(video_id: int) -> bool:
        remaining = [other for other in split['train'] if other != video_id]
        remaining_evidence = (
            set().union(*(evidence(other) for other in remaining))
            if remaining
            else set()
        )
        if not required <= remaining_evidence:
            return False
        remaining_groups = (
            set().union(*(video_groups.get(other, set()) for other in remaining))
            if remaining
            else set()
        )
        return train_required <= remaining_groups

    # 如果某个难例组有至少两个独立视频，训练集和测试集必须各留一个。
    for group in sorted(train_required - target_groups('train')):
        choices = [
            (video_sizes[video_id], source, video_id)
            for source in ('val', 'test')
            for video_id in split[source]
            if group in video_groups.get(video_id, set())
        ]
        if not choices:
            continue
        _size, source, video_id = min(choices)
        split[source].remove(video_id)
        split['train'].append(video_id)

    def fill_target(name: str, required_groups: Tuple[str, ...]) -> None:
        while True:
            missing = set(required_groups) - target_groups(name)
            if not missing:
                return
            sources = ('val', 'train') if name == 'test' else ('train',)
            choices = []
            for source in sources:
                for video_id in split[source]:
                    provided = video_groups.get(video_id, set()) & missing
                    if not provided:
                        continue
                    if source == 'train' and not train_can_release(video_id):
                        continue
                    choices.append(
                        (
                            -len(provided),
                            video_sizes[video_id],
                            source == 'train',
                            video_id,
                            source,
                        )
                    )
            if not choices:
                return
            *_rank, video_id, source = min(choices)
            split[source].remove(video_id)
            split[name].append(video_id)

    fill_target('test', required_test_groups)
    fill_target('val', required_val_groups)
    return {name: sorted(video_ids) for name, video_ids in split.items()}


# ---------- 结算检测导出 ----------

_SCOREBOARD_SCREEN_TYPES = {'scoreboard', 'death_scoreboard'}
_KNOWN_MATCH_MODES = {'3v3', '5v5', 'aram'}


def _result_detector_evaluation_groups(
    sample: Dict[str, Any],
    *,
    hero_layout_label: Any = None,
    match_mode_label: Any = None,
) -> None:
    """记录结算检测需要单独验收的场景，不改变检测训练标签。"""
    label = str(sample.get('detector_label') or '')
    annotation = sample.get('annotation') or {}
    if label == 'result_panel':
        scenario = 'result_panel'
        groups = ['result_panel']
    elif (
        str(hero_layout_label or '') == 'scoreboard'
        or str(annotation.get('screen_type') or '') in _SCOREBOARD_SCREEN_TYPES
    ):
        scenario = 'scoreboard'
        groups = ['scoreboard']
    else:
        scenario = 'other_negative'
        groups = ['other_negative']
    mode = str(match_mode_label or annotation.get('game_mode') or '')
    if mode not in _KNOWN_MATCH_MODES:
        mode = 'unreadable'
    if scenario == 'scoreboard' and mode in _KNOWN_MATCH_MODES:
        groups.append(f'scoreboard:{mode}')
    sample['evaluation_scenario'] = scenario
    sample['evaluation_mode'] = mode
    sample['evaluation_groups'] = groups


def _evaluation_group_counts(samples: List[Dict[str, Any]]) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for sample in samples:
        for group in sample.get('evaluation_groups') or ():
            counts[str(group)] = counts.get(str(group), 0) + 1
    return counts


def export_result_detector(
    conn: Any,
    *,
    include_negatives: bool = True,
    max_negatives: Optional[int] = None,
    version: Optional[str] = None,
    materialize: bool = True,
) -> Dict[str, Any]:
    """导出 result_detector 数据集(JSONL + YOLO + COCO),创建不可变版本。"""
    annotations, boxes = _frame_sample_metadata(conn)
    frames = _labeled_frames(conn)
    samples_by_frame: Dict[int, Dict[str, Any]] = {}
    result_without_bbox = 0
    for frame in frames:
        annotation = annotations.get(int(frame['id']), {})
        sample = _frame_sample(conn, frame, annotations=annotations, boxes=boxes)
        if sample is None:
            continue
        if annotation.get('screen_type') == 'result_page':
            if not sample['boxes'].get('result_panel'):
                result_without_bbox += 1
                continue
            sample['detector_label'] = 'result_panel'
        elif include_negatives:
            sample['detector_label'] = 'no_result_panel'
        else:
            continue
        sample['label_source'] = 'existing_human_annotation'
        samples_by_frame[int(frame['id'])] = sample

    reviewed = conn.execute(
        'SELECT c.*, f.*, v.streamer, v.remote_path '
        'FROM worker_candidate_items c '
        'JOIN frames f ON f.id = c.frame_id '
        'JOIN videos v ON v.id = f.video_id '
        "WHERE c.task = 'result_detector' AND c.review_status = 'confirmed' "
        'AND c.confirmed_label IS NOT NULL '
        "AND c.visual_condition != 'unreadable'"
    ).fetchall()
    for row in reviewed:
        frame = dict(row)
        frame['id'] = int(row['frame_id'])
        sample = _frame_sample(conn, frame, annotations=annotations, boxes=boxes)
        label = str(frame['confirmed_label'])
        if sample is None or (label == 'no_result_panel' and not include_negatives):
            continue
        candidate_boxes = json.loads(frame['boxes_json'] or '[]')
        if label == 'result_panel':
            result_boxes = [
                box
                for box in candidate_boxes
                if not box.get('type') or box.get('type') == 'result_panel'
            ]
            if not result_boxes:
                continue
            sample['boxes']['result_panel'] = result_boxes[0]
        else:
            sample['boxes'].pop('result_panel', None)
        sample['detector_label'] = label
        sample['label_source'] = 'worker_candidate_confirmed'
        sample['visual_condition'] = str(frame['visual_condition'])
        samples_by_frame[int(frame['frame_id'])] = sample

    unified_rows = conn.execute(
        'SELECT f.*, v.streamer, v.remote_path, r.result_panel_label, '
        'r.panel_render_state, r.hero_layout_label, r.match_mode_label '
        'FROM training_review_items r '
        'JOIN frames f ON f.id = r.frame_id '
        'JOIN videos v ON v.id = f.video_id '
        "WHERE r.review_status = 'confirmed' "
        'AND r.result_panel_label IS NOT NULL '
        'ORDER BY f.video_id, f.timestamp_ms, f.id'
    ).fetchall()
    for row in unified_rows:
        frame = dict(row)
        frame_id = int(frame['id'])
        label = str(frame['result_panel_label'])
        if label == 'unreadable':
            samples_by_frame.pop(frame_id, None)
            continue
        if label == 'no_result_panel' and not include_negatives:
            samples_by_frame.pop(frame_id, None)
            continue
        sample = _frame_sample(conn, frame, annotations=annotations, boxes=boxes)
        if sample is None:
            samples_by_frame.pop(frame_id, None)
            continue
        if label == 'result_panel' and not sample['boxes'].get('result_panel'):
            result_without_bbox += 1
            samples_by_frame.pop(frame_id, None)
            continue
        if label == 'no_result_panel':
            sample['boxes'].pop('result_panel', None)
        sample['detector_label'] = label
        sample['label_source'] = 'training_review_confirmed'
        sample['panel_render_state'] = str(frame['panel_render_state'])
        sample['review_hero_layout_label'] = frame['hero_layout_label']
        sample['review_match_mode_label'] = frame['match_mode_label']
        samples_by_frame[frame_id] = sample

    duplicate_results = db.training_review_duplicate_result_frame_ids(conn)
    for frame_id in duplicate_results:
        sample = samples_by_frame.get(frame_id)
        if sample is not None and sample['detector_label'] == 'result_panel':
            samples_by_frame.pop(frame_id, None)

    for sample in samples_by_frame.values():
        _result_detector_evaluation_groups(
            sample,
            hero_layout_label=sample.pop('review_hero_layout_label', None),
            match_mode_label=sample.pop('review_match_mode_label', None),
        )

    positives = [
        sample
        for sample in samples_by_frame.values()
        if sample['detector_label'] == 'result_panel'
    ]
    negatives = [
        sample
        for sample in samples_by_frame.values()
        if sample['detector_label'] == 'no_result_panel'
    ]
    if max_negatives and len(negatives) > max_negatives:

        def _negative_rank(sample: Dict[str, Any]) -> Tuple[int, str]:
            if sample.get('evaluation_scenario') == 'scoreboard':
                priority = 0
            elif sample.get('label_source') in (
                'training_review_confirmed',
                'worker_candidate_confirmed',
            ):
                priority = 1
            else:
                priority = 2
            return priority, str(sample.get('sha256') or sample['sample_id'])

        negatives = sorted(negatives, key=_negative_rank)[:max_negatives]
    samples = positives + negatives

    if not samples:
        raise RuntimeError('没有可导出的已标注样本')

    # 切分:按视频
    video_ids = sorted({s['video_id'] for s in samples})
    group_videos: Dict[str, set] = {}
    for sample in samples:
        for group in sample.get('evaluation_groups') or ():
            group_videos.setdefault(str(group), set()).add(int(sample['video_id']))
    required_test_groups = tuple(
        sorted(group for group, videos in group_videos.items() if len(videos) >= 2)
    )
    required_val_groups = tuple(
        sorted(group for group, videos in group_videos.items() if len(videos) >= 3)
    )
    split = split_detection_by_video(
        samples,
        label_field='detector_label',
        positive_label='result_panel',
        evaluation_group_field='evaluation_groups',
        required_test_groups=required_test_groups,
        required_val_groups=required_val_groups,
    )
    v2split = {vid: k for k, vids in split.items() for vid in vids}
    for s in samples:
        s['split'] = v2split[s['video_id']]

    version_id = version or next_version_id(conn, 'result_detector')
    out_dir = config.EXPORT_DIR / version_id
    if out_dir.exists():
        raise RuntimeError(f'数据集版本已存在,禁止覆盖: {version_id}')
    out_dir.mkdir(parents=True, exist_ok=False)
    images_dir = out_dir / 'images'
    labels_dir = out_dir / 'labels'
    if materialize:
        for split_name in ('train', 'val', 'test'):
            (images_dir / split_name).mkdir(parents=True, exist_ok=True)
            (labels_dir / split_name).mkdir(parents=True, exist_ok=True)

    coco_images = []
    coco_annotations = []
    coco_id = 0
    for s in samples:
        split_name = s['split']
        img_name = f"{s['sample_id']}.jpg"
        box = s['boxes'].get('result_panel')
        if materialize:
            src = _path_for(conn, s)
            if src is None:
                continue
            dst = images_dir / split_name / img_name
            if not dst.exists():
                shutil.copy2(src, dst)
            txt = labels_dir / split_name / f"{s['sample_id']}.txt"
            if box:
                x = box['x'] + box['w'] / 2
                y = box['y'] + box['h'] / 2
                txt.write_text(f"0 {x:.6f} {y:.6f} {box['w']:.6f} {box['h']:.6f}\n")
            else:
                txt.write_text('')
        coco_id += 1
        coco_images.append(
            {
                'id': coco_id,
                'file_name': f'{split_name}/{img_name}',
                'width': s['width'],
                'height': s['height'],
            }
        )
        if box:
            coco_annotations.append(
                {
                    'id': coco_id,
                    'image_id': coco_id,
                    'category_id': 1,
                    'bbox': [
                        box['x'] * s['width'],
                        box['y'] * s['height'],
                        box['w'] * s['width'],
                        box['h'] * s['height'],
                    ],
                    'area': box['w'] * s['width'] * box['h'] * s['height'],
                    'iscrowd': 0,
                }
            )

    if materialize:
        (out_dir / 'coco_annotations.json').write_text(
            json.dumps(
                {
                    'images': coco_images,
                    'annotations': coco_annotations,
                    'categories': [{'id': 1, 'name': 'result_panel'}],
                },
                ensure_ascii=False,
            ),
            encoding='utf-8',
        )
        (out_dir / 'data.yaml').write_text(
            f"path: {out_dir}\ntrain: images/train\nval: images/val\n"
            f"test: images/test\nnc: 1\nnames: ['result_panel']\n",
            encoding='utf-8',
        )
    # JSONL 清单
    jsonl_path = out_dir / 'samples.jsonl'
    with jsonl_path.open('w', encoding='utf-8') as fh:
        for s in samples:
            fh.write(json.dumps(s, ensure_ascii=False) + '\n')

    # 版本记录
    by_evaluation_group = _evaluation_group_counts(samples)
    counts = {
        'total': len(samples),
        'positive': len(positives),
        'negative': len(negatives),
        'result_with_bbox': sum(1 for s in samples if s['boxes'].get('result_panel')),
        'excluded_result_without_bbox': result_without_bbox,
        'by_split': {
            k: sum(1 for s in samples if s['split'] == k)
            for k in ('train', 'val', 'test')
        },
        'by_evaluation_group': by_evaluation_group,
        'by_split_evaluation_group': {
            split_name: _evaluation_group_counts(
                [sample for sample in samples if sample['split'] == split_name]
            )
            for split_name in ('train', 'val', 'test')
        },
        'videos': len(video_ids),
    }
    db.create_dataset_version(
        conn,
        version_id=version_id,
        task_id='result_detector',
        filter_json={
            'include_negatives': include_negatives,
            'max_negatives': max_negatives,
            'negative_priority': ['scoreboard', 'death_scoreboard', 'frame_id'],
            'required_test_evaluation_groups': list(required_test_groups),
            'required_val_evaluation_groups': list(required_val_groups),
            'materialized_by': 'vision_worker' if not materialize else 'vision_lab',
        },
        counts=counts,
        manifest_path=str(jsonl_path),
        git_commit=_git_commit(),
    )
    return {'version': version_id, 'dir': str(out_dir), **counts}


def _path_for(conn: Any, s: Dict[str, Any]) -> Optional[Path]:
    row = conn.execute(
        'SELECT frame_path FROM frames WHERE sha256 = ?', (s['sha256'],)
    ).fetchone()
    if not row:
        return None
    p = Path(row['frame_path'])
    return p if p.exists() else None


def _write_classification_images(
    conn: Any, out_dir: Path, samples: List[Dict[str, Any]], labels: Tuple[str, ...]
) -> None:
    """把分类快照组织为 Ultralytics ImageFolder 目录。"""
    for split_name in ('train', 'val', 'test'):
        for label in labels:
            (out_dir / 'images' / split_name / label).mkdir(parents=True, exist_ok=True)
    for sample in samples:
        source = _path_for(conn, sample)
        if source is None:
            continue
        destination = (
            out_dir
            / 'images'
            / sample['split']
            / sample['label']
            / f"{sample['sample_id']}.jpg"
        )
        if not destination.exists():
            shutil.copy2(source, destination)


def _confirmed_hero_lineup_samples(conn: Any) -> List[Dict[str, Any]]:
    """返回完整人工确认阵容；头像位置与英雄真值仍分别保留。"""
    annotations, boxes = _frame_sample_metadata(conn)
    rows = conn.execute(
        'SELECT f.*, v.streamer, v.remote_path, lineup.screen_type, '
        'lineup.team_size FROM training_review_hero_lineups lineup '
        'JOIN frames f ON f.id = lineup.frame_id '
        'JOIN videos v ON v.id = f.video_id '
        'JOIN training_review_items review ON review.frame_id = lineup.frame_id '
        "WHERE lineup.review_status = 'confirmed' "
        "AND COALESCE(review.view_context_label, 'played') = 'played' "
        "AND COALESCE(review.match_kind_label, 'pvp') != 'practice' "
        'ORDER BY f.video_id, f.timestamp_ms, f.id'
    ).fetchall()
    slots_by_frame: Dict[int, List[Dict[str, Any]]] = {}
    for slot_row in conn.execute(
        'SELECT frame_id, side, slot, crop_x, crop_y, crop_w, crop_h, '
        'confirmed_label FROM training_review_hero_slots '
        "ORDER BY frame_id, CASE side WHEN 'left' THEN 0 ELSE 1 END, slot"
    ).fetchall():
        slot = dict(slot_row)
        slots_by_frame.setdefault(int(slot.pop('frame_id')), []).append(slot)
    samples = []
    for row in rows:
        frame = dict(row)
        sample = _frame_sample(conn, frame, annotations=annotations, boxes=boxes)
        if sample is None:
            continue
        slots = slots_by_frame.get(int(frame['id']), [])
        team_size = int(frame['team_size'])
        if len(slots) != team_size * 2:
            continue
        sample['frame_id'] = int(frame['id'])
        sample['hero_screen_type'] = str(frame['screen_type'])
        sample['team_size'] = team_size
        sample['hero_slots'] = [
            {
                'side': str(slot['side']),
                'slot': int(slot['slot']),
                'crop': {
                    'x': float(slot['crop_x']),
                    'y': float(slot['crop_y']),
                    'w': float(slot['crop_w']),
                    'h': float(slot['crop_h']),
                },
                'confirmed_label': str(slot['confirmed_label'] or ''),
            }
            for slot in slots
        ]
        samples.append(sample)
    return samples


def confirmed_player_position_samples(conn: Any) -> List[Dict[str, Any]]:
    """返回积分板／结算图中人工明确确认的主播位置整图样本。"""
    annotations, boxes = _frame_sample_metadata(conn)
    rows = conn.execute(
        'SELECT f.*, v.streamer, v.remote_path, lineup.screen_type, '
        'lineup.team_size, lineup.player_side, lineup.player_slot, '
        'review.panel_render_state '
        'FROM training_review_hero_lineups lineup '
        'JOIN frames f ON f.id = lineup.frame_id '
        'JOIN videos v ON v.id = f.video_id '
        'LEFT JOIN training_review_items review ON review.frame_id = f.id '
        "WHERE lineup.review_status = 'confirmed' "
        "AND lineup.player_status = 'identified' "
        "AND COALESCE(review.view_context_label, 'played') = 'played' "
        "AND lineup.screen_type IN ('scoreboard', 'result_page') "
        "AND lineup.player_side IN ('left', 'right') "
        'AND lineup.player_slot BETWEEN 1 AND lineup.team_size '
        'AND EXISTS ('
        'SELECT 1 FROM training_review_hero_slots slot '
        'WHERE slot.frame_id = lineup.frame_id '
        'AND slot.side = lineup.player_side '
        'AND slot.slot = lineup.player_slot) '
        'ORDER BY f.video_id, f.timestamp_ms, f.id'
    ).fetchall()
    duplicate_results = db.training_review_duplicate_result_frame_ids(conn)
    samples = []
    for row in rows:
        frame = dict(row)
        if int(frame['id']) in duplicate_results:
            continue
        label = '{}{}'.format(frame['player_side'], int(frame['player_slot']))
        if label not in PLAYER_POSITION_LABELS:
            continue
        sample = _frame_sample(conn, frame, annotations=annotations, boxes=boxes)
        if sample is None:
            continue
        sample.update(
            {
                'frame_id': int(frame['id']),
                'label': label,
                'hero_screen_type': str(frame['screen_type']),
                'evaluation_scenario': str(frame['screen_type']),
                'team_size': int(frame['team_size']),
                'player_side': str(frame['player_side']),
                'player_slot': int(frame['player_slot']),
                'visual_condition': str(frame['panel_render_state'] or 'clear'),
                'label_source': 'training_review_player_position_confirmed',
            }
        )
        samples.append(sample)
    return samples


def export_hero_avatar_detector(
    conn: Any, *, materialize: bool = True
) -> Dict[str, Any]:
    """把 HUD／积分板／结算页上的人工头像圆框导出为单类检测数据。"""
    samples = _confirmed_hero_lineup_samples(conn)
    if not samples:
        raise RuntimeError('没有可导出的人工确认英雄头像框')
    split = split_classification_by_video(
        [{**sample, 'label': str(sample['hero_screen_type'])} for sample in samples],
        ('gameplay_hud', 'scoreboard', 'result_page'),
    )
    video_split = {
        video_id: name for name, video_ids in split.items() for video_id in video_ids
    }
    for sample in samples:
        sample['split'] = video_split[int(sample['video_id'])]
        sample['detector_label'] = 'hero_avatar'
        sample['avatar_boxes'] = [slot['crop'] for slot in sample['hero_slots']]

    version_id = next_version_id(conn, 'hero_avatar_detector')
    out_dir = config.EXPORT_DIR / version_id
    if out_dir.exists():
        raise RuntimeError(f'数据集版本已存在: {version_id}')
    out_dir.mkdir(parents=True, exist_ok=False)
    if materialize:
        for split_name in ('train', 'val', 'test'):
            (out_dir / 'images' / split_name).mkdir(parents=True, exist_ok=True)
            (out_dir / 'labels' / split_name).mkdir(parents=True, exist_ok=True)
        for sample in samples:
            source = _path_for(conn, sample)
            if source is None:
                continue
            split_name = str(sample['split'])
            destination = out_dir / 'images' / split_name / f"{sample['sample_id']}.jpg"
            if not destination.exists():
                shutil.copy2(source, destination)
            labels = []
            for box in sample['avatar_boxes']:
                center_x = float(box['x']) + float(box['w']) / 2
                center_y = float(box['y']) + float(box['h']) / 2
                labels.append(
                    '0 {:.6f} {:.6f} {:.6f} {:.6f}'.format(
                        center_x, center_y, float(box['w']), float(box['h'])
                    )
                )
            (out_dir / 'labels' / split_name / f"{sample['sample_id']}.txt").write_text(
                '\n'.join(labels) + '\n', encoding='utf-8'
            )
        (out_dir / 'data.yaml').write_text(
            f'path: {out_dir}\ntrain: images/train\nval: images/val\n'
            "test: images/test\nnc: 1\nnames: ['hero_avatar']\n",
            encoding='utf-8',
        )
    jsonl_path = out_dir / 'samples.jsonl'
    with jsonl_path.open('w', encoding='utf-8') as handle:
        for sample in samples:
            handle.write(json.dumps(sample, ensure_ascii=False) + '\n')
    screen_types = ('gameplay_hud', 'scoreboard', 'result_page')
    counts = {
        'total': len(samples),
        'positive': len(samples),
        'boxes': sum(len(sample['avatar_boxes']) for sample in samples),
        'videos': len({int(sample['video_id']) for sample in samples}),
        'by_screen_type': {
            screen_type: sum(
                sample['hero_screen_type'] == screen_type for sample in samples
            )
            for screen_type in screen_types
        },
        'by_team_size': {
            str(team_size): sum(
                int(sample['team_size']) == team_size for sample in samples
            )
            for team_size in (3, 5)
        },
        'by_split': {
            name: sum(sample['split'] == name for sample in samples)
            for name in ('train', 'val', 'test')
        },
    }
    db.create_dataset_version(
        conn,
        version_id=version_id,
        task_id='hero_avatar_detector',
        filter_json={
            'source': 'training_review_hero_lineups',
            'confirmed_only': True,
            'classes': ['hero_avatar'],
            'split_unit': 'video',
            'materialized_by': 'vision_worker' if not materialize else 'vision_lab',
        },
        counts=counts,
        manifest_path=str(jsonl_path),
        git_commit=_git_commit(),
    )
    return {'version': version_id, 'dir': str(out_dir), **counts}


def _write_hero_identity_images(
    conn: Any, out_dir: Path, samples: List[Dict[str, Any]], labels: Tuple[str, ...]
) -> int:
    for split_name in ('train', 'val', 'test'):
        for label in labels:
            (out_dir / 'images' / split_name / label).mkdir(parents=True, exist_ok=True)
    opened: Dict[str, Image.Image] = {}
    written: Dict[str, List[Path]] = {label: [] for label in labels}
    try:
        for sample in samples:
            source = _path_for(conn, sample)
            if source is None:
                continue
            key = str(source)
            image = opened.get(key)
            if image is None:
                image = Image.open(source).convert('RGB')
                opened[key] = image
            crop = sample['crop']
            left = max(0, min(image.width - 1, round(float(crop['x']) * image.width)))
            top = max(0, min(image.height - 1, round(float(crop['y']) * image.height)))
            right = max(
                left + 1,
                min(
                    image.width,
                    round((float(crop['x']) + float(crop['w'])) * image.width),
                ),
            )
            bottom = max(
                top + 1,
                min(
                    image.height,
                    round((float(crop['y']) + float(crop['h'])) * image.height),
                ),
            )
            destination = (
                out_dir
                / 'images'
                / str(sample['split'])
                / str(sample['label'])
                / f"{sample['sample_id']}.jpg"
            )
            image.crop((left, top, right, bottom)).save(
                destination, format='JPEG', quality=95
            )
            if sample['split'] == 'train':
                written[str(sample['label'])].append(destination)
    finally:
        for image in opened.values():
            image.close()

    populated = [len(paths) for paths in written.values() if paths]
    target = min(100, max(1, round(median(populated)))) if populated else 0
    replicas = 0
    for label, paths in written.items():
        if not paths:
            continue
        for index in range(len(paths), target):
            source = paths[index % len(paths)]
            destination = source.with_name(
                '{}-balance-{:04d}.jpg'.format(source.stem, index - len(paths) + 1)
            )
            shutil.copy2(source, destination)
            replicas += 1
    return replicas


def _classification_balance_replicas(
    samples: List[Dict[str, Any]], labels: Tuple[str, ...]
) -> int:
    counts = {
        label: sum(
            sample['split'] == 'train' and sample['label'] == label
            for sample in samples
        )
        for label in labels
    }
    populated = [count for count in counts.values() if count]
    target = min(100, max(1, round(median(populated)))) if populated else 0
    return sum(max(0, target - count) for count in counts.values() if count)


def export_hero_identity_classifier(
    conn: Any, *, materialize: bool = True
) -> Dict[str, Any]:
    """把人工确认且可读的头像裁剪导出为英雄身份分类数据。"""
    lineups = _confirmed_hero_lineup_samples(conn)
    samples = []
    excluded_unreadable = 0
    for lineup in lineups:
        for slot in lineup['hero_slots']:
            label = str(slot['confirmed_label'] or '')
            if not label or label == 'unreadable':
                excluded_unreadable += 1
                continue
            sample = {
                key: value
                for key, value in lineup.items()
                if key not in {'hero_slots', 'sample_id'}
            }
            sample.update(
                {
                    'sample_id': 'f{:08d}-{}-{}'.format(
                        int(lineup['frame_id']), slot['side'], slot['slot']
                    ),
                    'label': label,
                    'side': slot['side'],
                    'slot': int(slot['slot']),
                    'crop': slot['crop'],
                    'label_source': 'training_review_hero_lineup_confirmed',
                }
            )
            samples.append(sample)
    if not samples:
        raise RuntimeError('没有可导出的人工确认英雄头像')
    labels = tuple(
        sorted({str(sample['label']) for sample in samples}, key=str.casefold)
    )
    split = split_classification_by_video(samples, labels)
    video_split = {
        video_id: name for name, video_ids in split.items() for video_id in video_ids
    }
    for sample in samples:
        sample['split'] = video_split[int(sample['video_id'])]

    version_id = next_version_id(conn, 'hero_identity')
    out_dir = config.EXPORT_DIR / version_id
    if out_dir.exists():
        raise RuntimeError(f'数据集版本已存在: {version_id}')
    out_dir.mkdir(parents=True, exist_ok=False)
    replicas = (
        _write_hero_identity_images(conn, out_dir, samples, labels)
        if materialize
        else _classification_balance_replicas(samples, labels)
    )
    jsonl_path = out_dir / 'samples.jsonl'
    with jsonl_path.open('w', encoding='utf-8') as handle:
        for sample in samples:
            handle.write(json.dumps(sample, ensure_ascii=False) + '\n')
    counts = {
        'total': len(samples),
        'classes': len(labels),
        'videos': len({int(sample['video_id']) for sample in samples}),
        'excluded_unreadable': excluded_unreadable,
        'training_balance_replicas': replicas,
        'by_label': {
            label: sum(sample['label'] == label for sample in samples)
            for label in labels
        },
        'by_screen_type': {
            screen_type: sum(
                sample['hero_screen_type'] == screen_type for sample in samples
            )
            for screen_type in ('gameplay_hud', 'scoreboard', 'result_page')
        },
        'by_split': {
            name: sum(sample['split'] == name for sample in samples)
            for name in ('train', 'val', 'test')
        },
    }
    db.create_dataset_version(
        conn,
        version_id=version_id,
        task_id='hero_identity',
        filter_json={
            'source': 'training_review_hero_slots',
            'confirmed_only': True,
            'excluded_labels': ['unreadable', ''],
            'split_unit': 'video',
            'train_balance': 'repeat_to_class_median_capped_at_100',
            'materialized_by': 'vision_worker' if not materialize else 'vision_lab',
        },
        counts=counts,
        manifest_path=str(jsonl_path),
        git_commit=_git_commit(),
    )
    return {'version': version_id, 'dir': str(out_dir), **counts}


AFK_STATUS_LABELS = ('active', 'afk')


def afk_status_context_crop(slot: Dict[str, Any]) -> Dict[str, float]:
    """从头像向玩家名字一侧扩展，保留灰头像与名字删除线。"""
    avatar = slot['crop']
    x = float(avatar['x'])
    y = float(avatar['y'])
    width = float(avatar['w'])
    height = float(avatar['h'])
    top = max(0.0, y - height * 0.3)
    bottom = min(1.0, y + height * 1.3)
    if slot['side'] == 'left':
        left = max(0.0, x - width * 6.0)
        right = min(1.0, x + width * 1.2)
    else:
        left = max(0.0, x - width * 0.2)
        right = min(1.0, x + width * 7.0)
    return {
        'x': left,
        'y': top,
        'w': max(0.001, right - left),
        'h': max(0.001, bottom - top),
    }


def confirmed_afk_status_samples(conn: Any) -> List[Dict[str, Any]]:
    """只返回真正结算图上人工确认的最终挂机状态。"""
    annotations, boxes = _frame_sample_metadata(conn)
    rows = conn.execute(
        'SELECT f.*, v.streamer, v.remote_path, lineup.team_size, '
        'slot.side, slot.slot, slot.crop_x, slot.crop_y, slot.crop_w, '
        'slot.crop_h, slot.is_afk '
        'FROM training_review_hero_slots slot '
        'JOIN training_review_hero_lineups lineup '
        'ON lineup.frame_id = slot.frame_id '
        'JOIN frames f ON f.id = slot.frame_id '
        'JOIN videos v ON v.id = f.video_id '
        "WHERE lineup.review_status = 'confirmed' "
        "AND lineup.screen_type = 'result_page' "
        'AND slot.is_afk IS NOT NULL '
        'ORDER BY f.video_id, f.timestamp_ms, f.id, '
        "CASE slot.side WHEN 'left' THEN 0 ELSE 1 END, slot.slot"
    ).fetchall()
    duplicate_results = db.training_review_duplicate_result_frame_ids(conn)
    samples = []
    for row in rows:
        frame = dict(row)
        if int(frame['id']) in duplicate_results:
            continue
        sample = _frame_sample(conn, frame, annotations=annotations, boxes=boxes)
        if sample is None:
            continue
        slot = {
            'side': str(frame['side']),
            'slot': int(frame['slot']),
            'crop': {
                'x': float(frame['crop_x']),
                'y': float(frame['crop_y']),
                'w': float(frame['crop_w']),
                'h': float(frame['crop_h']),
            },
        }
        sample.update(
            {
                'sample_id': 'f{:08d}-{}-{}'.format(
                    int(frame['id']), slot['side'], slot['slot']
                ),
                'frame_id': int(frame['id']),
                'label': 'afk' if bool(int(frame['is_afk'])) else 'active',
                'hero_screen_type': 'result_page',
                'team_size': int(frame['team_size']),
                'side': slot['side'],
                'slot': slot['slot'],
                'avatar_crop': slot['crop'],
                'crop': afk_status_context_crop(slot),
                'label_source': 'result_page_final_afk_confirmed',
            }
        )
        samples.append(sample)
    return samples


def export_afk_status_classifier(
    conn: Any, *, materialize: bool = True
) -> Dict[str, Any]:
    """导出真正结算图上的最终挂机二分类数据。"""
    samples = confirmed_afk_status_samples(conn)
    if not samples:
        raise RuntimeError('没有可导出的结算图挂机标注')
    split = split_classification_by_video(samples, AFK_STATUS_LABELS)
    video_split = {
        video_id: name for name, video_ids in split.items() for video_id in video_ids
    }
    for sample in samples:
        sample['split'] = video_split[int(sample['video_id'])]

    version_id = next_version_id(conn, 'afk_status')
    out_dir = config.EXPORT_DIR / version_id
    if out_dir.exists():
        raise RuntimeError(f'数据集版本已存在: {version_id}')
    out_dir.mkdir(parents=True, exist_ok=False)
    replicas = (
        _write_hero_identity_images(conn, out_dir, samples, AFK_STATUS_LABELS)
        if materialize
        else _classification_balance_replicas(samples, AFK_STATUS_LABELS)
    )
    jsonl_path = out_dir / 'samples.jsonl'
    with jsonl_path.open('w', encoding='utf-8') as handle:
        for sample in samples:
            handle.write(json.dumps(sample, ensure_ascii=False) + '\n')
    counts = {
        'total': len(samples),
        'videos': len({int(sample['video_id']) for sample in samples}),
        'training_balance_replicas': replicas,
        'by_label': {
            label: sum(sample['label'] == label for sample in samples)
            for label in AFK_STATUS_LABELS
        },
        'videos_by_label': {
            label: len(
                {
                    int(sample['video_id'])
                    for sample in samples
                    if sample['label'] == label
                }
            )
            for label in AFK_STATUS_LABELS
        },
        'by_split': {
            name: sum(sample['split'] == name for sample in samples)
            for name in ('train', 'val', 'test')
        },
    }
    db.create_dataset_version(
        conn,
        version_id=version_id,
        task_id='afk_status',
        filter_json={
            'source': 'training_review_hero_slots',
            'screen_type': 'result_page',
            'labels': list(AFK_STATUS_LABELS),
            'requires_explicit_is_afk': True,
            'split_unit': 'video',
            'train_balance': 'repeat_to_class_median_capped_at_100',
            'materialized_by': 'vision_worker' if not materialize else 'vision_lab',
        },
        counts=counts,
        manifest_path=str(jsonl_path),
        git_commit=_git_commit(),
    )
    return {'version': version_id, 'dir': str(out_dir), **counts}


def export_player_position_classifier(
    conn: Any, *, materialize: bool = True
) -> Dict[str, Any]:
    """按完整积分板／结算图训练主播本人所在阵容位置。"""
    samples = confirmed_player_position_samples(conn)
    if not samples:
        raise RuntimeError('没有可导出的主播本人位置标注')
    labels = tuple(
        label
        for label in PLAYER_POSITION_LABELS
        if any(sample['label'] == label for sample in samples)
    )
    if len(labels) < 2:
        raise RuntimeError('主播本人位置至少需要两个不同类别')
    split = split_classification_by_video(samples, labels)
    video_split = {
        video_id: name for name, video_ids in split.items() for video_id in video_ids
    }
    for sample in samples:
        sample['split'] = video_split[int(sample['video_id'])]

    version_id = next_version_id(conn, 'player_position')
    out_dir = config.EXPORT_DIR / version_id
    if out_dir.exists():
        raise RuntimeError(f'数据集版本已存在: {version_id}')
    out_dir.mkdir(parents=True, exist_ok=False)
    jsonl_path = out_dir / 'samples.jsonl'
    with jsonl_path.open('w', encoding='utf-8') as handle:
        for sample in samples:
            handle.write(json.dumps(sample, ensure_ascii=False) + '\n')
    if materialize:
        _write_classification_images(conn, out_dir, samples, labels)
        train_images = {
            label: sorted((out_dir / 'images' / 'train' / label).glob('*.jpg'))
            for label in labels
        }
        populated = [len(paths) for paths in train_images.values() if paths]
        balance_target = min(100, max(1, round(median(populated)))) if populated else 0
        replicas = 0
        for label, paths in train_images.items():
            if not paths:
                continue
            for index in range(len(paths), balance_target):
                source = paths[index % len(paths)]
                destination = source.with_name(
                    '{}-balance-{:04d}.jpg'.format(source.stem, index - len(paths) + 1)
                )
                shutil.copy2(source, destination)
                replicas += 1
    else:
        replicas = _classification_balance_replicas(samples, labels)

    excluded_unreadable = int(
        conn.execute(
            'SELECT COUNT(*) FROM training_review_hero_lineups '
            "WHERE review_status = 'confirmed' "
            "AND screen_type IN ('scoreboard', 'result_page') "
            "AND player_status = 'unreadable'"
        ).fetchone()[0]
    )
    excluded_hud = int(
        conn.execute(
            'SELECT COUNT(*) FROM training_review_hero_lineups '
            "WHERE review_status = 'confirmed' "
            "AND screen_type = 'gameplay_hud'"
        ).fetchone()[0]
    )
    counts = {
        'total': len(samples),
        'classes': len(labels),
        'videos': len({int(sample['video_id']) for sample in samples}),
        'excluded_unreadable': excluded_unreadable,
        'excluded_hud': excluded_hud,
        'training_balance_replicas': replicas,
        'by_label': {
            label: sum(sample['label'] == label for sample in samples)
            for label in labels
        },
        'videos_by_label': {
            label: len(
                {
                    int(sample['video_id'])
                    for sample in samples
                    if sample['label'] == label
                }
            )
            for label in labels
        },
        'by_screen_type': {
            screen_type: sum(
                sample['hero_screen_type'] == screen_type for sample in samples
            )
            for screen_type in ('scoreboard', 'result_page')
        },
        'by_team_size': {
            str(team_size): sum(
                int(sample['team_size']) == team_size for sample in samples
            )
            for team_size in (3, 5)
        },
        'by_split': {
            name: sum(sample['split'] == name for sample in samples)
            for name in ('train', 'val', 'test')
        },
    }
    db.create_dataset_version(
        conn,
        version_id=version_id,
        task_id='player_position',
        filter_json={
            'source': 'training_review_hero_lineups',
            'screen_types': ['scoreboard', 'result_page'],
            'player_status': 'identified',
            'labels': list(labels),
            'confirmed_only': True,
            'excluded_screen_types': ['gameplay_hud'],
            'excluded_labels': ['unreadable', 'pending'],
            'split_unit': 'video',
            'train_balance': 'repeat_to_class_median_capped_at_100',
            'preserve_full_image': True,
            'materialized_by': 'vision_worker' if not materialize else 'vision_lab',
        },
        counts=counts,
        manifest_path=str(jsonl_path),
        git_commit=_git_commit(),
    )
    return {'version': version_id, 'dir': str(out_dir), **counts}


UNIFIED_CLASSIFICATION_LABELS = {
    'match_flow': ('match_flow', 'not_match_flow'),
    'match_mode': ('3v3', 'aram', '5v5'),
    'hero_select': ('not_select', 'select_3v3', 'select_aram', 'select_5v5'),
}


def export_training_review_classifier(
    conn: Any, task_id: str, *, materialize: bool = True
) -> Dict[str, Any]:
    """从一图多标签人工复核中冻结一个分类任务的数据快照。"""
    annotations, boxes = _frame_sample_metadata(conn)
    labels = UNIFIED_CLASSIFICATION_LABELS.get(task_id)
    if labels is None:
        raise ValueError(f'未知统一分类任务: {task_id}')
    column = {
        'match_flow': 'match_flow_label',
        'match_mode': 'match_mode_label',
        'hero_select': 'hero_select_label',
    }[task_id]
    rows = conn.execute(
        f'SELECT f.*, v.streamer, v.remote_path, r.{column} AS label, '
        'r.review_status AS review_status, '
        'r.hero_select_variant AS hero_select_variant, '
        'r.hero_select_visibility AS hero_select_visibility, '
        'r.panel_render_state AS panel_render_state '
        'FROM training_review_items r '
        'JOIN frames f ON f.id = r.frame_id '
        'JOIN videos v ON v.id = f.video_id '
        "WHERE (r.review_status = 'confirmed' OR ("
        "r.review_status = 'partial' AND EXISTS ("
        'SELECT 1 FROM training_review_sources source '
        'WHERE source.frame_id = r.frame_id '
        "AND source.source_type = 'manual_correction'))) "
        f'AND r.{column} IS NOT NULL '
        'ORDER BY f.video_id, f.timestamp_ms, f.id'
    ).fetchall()
    duplicate_results = db.training_review_duplicate_result_frame_ids(conn)
    samples = []
    for row in rows:
        frame = dict(row)
        if int(frame['id']) in duplicate_results:
            continue
        label = str(frame['label'])
        if label not in labels:
            continue
        sample = _frame_sample(conn, frame, annotations=annotations, boxes=boxes)
        if sample is None:
            continue
        sample['label'] = label
        sample['label_source'] = (
            'manual_correction'
            if frame['review_status'] == 'partial'
            else 'training_review_confirmed'
        )
        sample['panel_render_state'] = str(frame['panel_render_state'])
        if task_id == 'hero_select' and frame['hero_select_variant'] is not None:
            sample['hero_select_variant'] = str(frame['hero_select_variant'])
        if task_id == 'hero_select':
            sample['hero_select_visibility'] = str(
                frame['hero_select_visibility'] or 'unknown'
            )
        samples.append(sample)
    if not samples:
        raise RuntimeError(f'没有可导出的 {task_id} 人工确认样本')
    split = split_classification_by_video(samples, labels)
    video_split = {
        video_id: name for name, video_ids in split.items() for video_id in video_ids
    }
    for sample in samples:
        sample['split'] = video_split[int(sample['video_id'])]

    version_id = next_version_id(conn, task_id)
    out_dir = config.EXPORT_DIR / version_id
    if out_dir.exists():
        raise RuntimeError(f'数据集版本已存在: {version_id}')
    out_dir.mkdir(parents=True, exist_ok=False)
    jsonl_path = out_dir / 'samples.jsonl'
    with jsonl_path.open('w', encoding='utf-8') as handle:
        for sample in samples:
            handle.write(json.dumps(sample, ensure_ascii=False) + '\n')
    if materialize:
        _write_classification_images(conn, out_dir, samples, labels)
    excluded_unreadable = int(
        conn.execute(
            f'SELECT COUNT(*) FROM training_review_items '
            "WHERE review_status = 'confirmed' "
            f"AND {column} = 'unreadable'"
        ).fetchone()[0]
    )
    counts = {
        'total': len(samples),
        'videos': len({int(sample['video_id']) for sample in samples}),
        'excluded_unreadable': excluded_unreadable,
        'by_label': {
            label: sum(1 for sample in samples if sample['label'] == label)
            for label in labels
        },
        'by_split': {
            name: sum(1 for sample in samples if sample['split'] == name)
            for name in ('train', 'val', 'test')
        },
    }
    db.create_dataset_version(
        conn,
        version_id=version_id,
        task_id=task_id,
        filter_json={
            'source': 'training_review_items',
            'label_column': column,
            'labels': list(labels),
            'confirmed_only': True,
            'excluded_labels': ['unreadable'],
            'split_unit': 'video',
            'materialized_by': 'vision_worker' if not materialize else 'vision_lab',
        },
        counts=counts,
        manifest_path=str(jsonl_path),
        git_commit=_git_commit(),
    )
    return {'version': version_id, 'dir': str(out_dir), **counts}


# ---------- 通用导出(游戏状态/模式/窗口,JSONL) ----------


def export_generic(conn: Any, task_id: str) -> Dict[str, Any]:
    """按训练目标导出 JSONL(分类/窗口等任务)。"""
    frames = _labeled_frames(conn)
    samples = [s for s in (_frame_sample(conn, f) for f in frames) if s]
    if not samples:
        raise RuntimeError('没有可导出的已标注样本')
    video_ids = sorted({s['video_id'] for s in samples})
    split = split_by_video(video_ids)
    v2split = {vid: k for k, vids in split.items() for vid in vids}
    for s in samples:
        s['split'] = v2split[s['video_id']]
    version_id = next_version_id(conn, task_id)
    out_dir = config.EXPORT_DIR / version_id
    if out_dir.exists():
        raise RuntimeError(f'数据集版本已存在: {version_id}')
    out_dir.mkdir(parents=True, exist_ok=True)
    jsonl_path = out_dir / 'samples.jsonl'
    with jsonl_path.open('w', encoding='utf-8') as fh:
        for s in samples:
            fh.write(json.dumps(s, ensure_ascii=False) + '\n')
    counts = {
        'total': len(samples),
        'videos': len(video_ids),
        'by_split': {
            k: sum(1 for s in samples if s['split'] == k)
            for k in ('train', 'val', 'test')
        },
    }
    db.create_dataset_version(
        conn,
        version_id=version_id,
        task_id=task_id,
        filter_json={},
        counts=counts,
        manifest_path=str(jsonl_path),
        git_commit=_git_commit(),
    )
    return {'version': version_id, 'dir': str(out_dir), **counts}


# ---------- BP 专用分类导出 ----------


def export_bp_classifier(conn: Any) -> Dict[str, Any]:
    """导出人工确认的 BP 四分类数据集。

    原有完整英雄选择标注作为基础正样本；BP 复核页的人工确认结果覆盖同帧
    的旧标签，并补充 not_bp 难负样本。模型建议本身绝不进入数据集。
    """
    samples_by_frame: Dict[int, Dict[str, Any]] = {}
    mode_labels = {'3v3': 'bp_3v3', 'aram': 'bp_aram', '5v5': 'bp_5v5'}
    rows = conn.execute(
        'SELECT f.*, v.streamer, v.remote_path, a.screen_type, a.game_mode '
        'FROM annotations a JOIN frames f ON f.id = a.frame_id '
        'JOIN videos v ON v.id = f.video_id '
        "WHERE a.annotation_status = 'complete' "
        "AND a.screen_type IN ('hero_select_bp', 'hero_select_blind', "
        "'hero_select_aram', 'match_confirm') "
        'ORDER BY f.video_id, f.timestamp_ms'
    ).fetchall()
    for row in rows:
        frame = dict(row)
        label = (
            'not_bp'
            if frame['screen_type'] == 'match_confirm'
            else mode_labels.get(frame['game_mode'])
        )
        if label is None and frame['screen_type'] == 'hero_select_aram':
            label = 'bp_aram'
        if label is None:
            continue
        sample = _frame_sample(conn, frame)
        if sample is not None:
            sample['label'] = label
            sample['label_source'] = 'existing_human_annotation'
            sample['visual_condition'] = 'clear'
            samples_by_frame[frame['id']] = sample

    reviewed = conn.execute(
        'SELECT f.*, v.streamer, v.remote_path, b.confirmed_label, '
        'b.model_version, b.visual_condition FROM bp_review_items b '
        'JOIN frames f ON f.id = b.frame_id '
        'JOIN videos v ON v.id = f.video_id '
        "WHERE b.review_status = 'confirmed' "
        'AND b.confirmed_label IS NOT NULL '
        "AND b.visual_condition != 'unreadable' "
        'ORDER BY f.video_id, f.timestamp_ms'
    ).fetchall()
    excluded_unreadable = int(
        conn.execute(
            "SELECT COUNT(*) FROM bp_review_items WHERE review_status = 'confirmed' "
            "AND confirmed_label IS NOT NULL AND visual_condition = 'unreadable'"
        ).fetchone()[0]
    )
    for row in reviewed:
        frame = dict(row)
        sample = _frame_sample(conn, frame)
        if sample is not None:
            sample['label'] = frame['confirmed_label']
            sample['label_source'] = 'bp_review_confirmed'
            sample['prelabel_model'] = frame['model_version']
            sample['visual_condition'] = frame['visual_condition']
            samples_by_frame[frame['id']] = sample

    samples = list(samples_by_frame.values())
    if not samples:
        raise RuntimeError('没有可导出的 BP 人工确认样本')

    video_ids = sorted({s['video_id'] for s in samples})
    split = split_classification_by_video(
        samples, ('bp_3v3', 'bp_aram', 'bp_5v5', 'not_bp')
    )
    v2split = {vid: name for name, vids in split.items() for vid in vids}
    for sample in samples:
        sample['split'] = v2split[sample['video_id']]

    version_id = next_version_id(conn, 'bp_review')
    out_dir = config.EXPORT_DIR / version_id
    if out_dir.exists():
        raise RuntimeError(f'数据集版本已存在: {version_id}')
    out_dir.mkdir(parents=True, exist_ok=False)
    jsonl_path = out_dir / 'samples.jsonl'
    with jsonl_path.open('w', encoding='utf-8') as fh:
        for sample in samples:
            fh.write(json.dumps(sample, ensure_ascii=False) + '\n')

    labels = ('bp_3v3', 'bp_aram', 'bp_5v5', 'not_bp')
    _write_classification_images(conn, out_dir, samples, labels)
    counts = {
        'total': len(samples),
        'videos': len(video_ids),
        'excluded_unreadable': excluded_unreadable,
        'by_label': {
            label: sum(1 for s in samples if s['label'] == label) for label in labels
        },
        'by_source': {
            source: sum(1 for s in samples if s['label_source'] == source)
            for source in ('existing_human_annotation', 'bp_review_confirmed')
        },
        'by_split': {
            name: sum(1 for s in samples if s['split'] == name)
            for name in ('train', 'val', 'test')
        },
    }
    db.create_dataset_version(
        conn,
        version_id=version_id,
        task_id='bp_review',
        filter_json={
            'labels': list(labels),
            'confirmed_only': True,
            'excluded_visual_conditions': ['unreadable'],
            'split_unit': 'video',
        },
        counts=counts,
        manifest_path=str(jsonl_path),
        git_commit=_git_commit(),
    )
    return {'version': version_id, 'dir': str(out_dir), **counts}


# ---------- 结算页 / 计分板三分类导出 ----------


def _key_screen_other_rank(sample: Dict[str, Any]) -> Tuple[int, str]:
    """优先保留人工确认和最容易与结算页混淆的负样本。"""
    if sample.get('label_source') == 'key_screen_review_confirmed':
        priority = 0
    elif sample.get('annotation', {}).get('screen_type') in {
        'victory_defeat_animation',
        'other_post',
    }:
        priority = 1
    else:
        priority = 2
    return priority, str(sample.get('sha256') or sample['sample_id'])


def export_key_screen_classifier(conn: Any) -> Dict[str, Any]:
    """导出 result_page / scoreboard / other 三分类不可变快照。"""
    samples_by_frame: Dict[int, Dict[str, Any]] = {}
    rows = conn.execute(
        'SELECT f.*, v.streamer, v.remote_path, a.screen_type '
        'FROM annotations a JOIN frames f ON f.id = a.frame_id '
        'JOIN videos v ON v.id = f.video_id '
        "WHERE a.annotation_status = 'complete' "
        'ORDER BY f.video_id, f.timestamp_ms'
    ).fetchall()
    for row in rows:
        frame = dict(row)
        screen_type = frame['screen_type']
        if screen_type == 'result_page':
            label = 'result_page'
        elif screen_type in ('scoreboard', 'death_scoreboard'):
            label = 'scoreboard'
        else:
            label = 'other'
        sample = _frame_sample(conn, frame)
        if sample is None:
            continue
        sample['label'] = label
        sample['label_source'] = 'existing_human_annotation'
        sample['visual_condition'] = 'clear'
        samples_by_frame[frame['id']] = sample

    reviewed = conn.execute(
        'SELECT f.*, v.streamer, v.remote_path, k.confirmed_label, '
        'k.model_version, k.visual_condition '
        'FROM key_screen_review_items k '
        'JOIN frames f ON f.id = k.frame_id '
        'JOIN videos v ON v.id = f.video_id '
        "WHERE k.review_status = 'confirmed' "
        'AND k.confirmed_label IS NOT NULL '
        "AND k.visual_condition != 'unreadable' "
        'ORDER BY f.video_id, f.timestamp_ms'
    ).fetchall()
    excluded_unreadable = int(
        conn.execute(
            'SELECT COUNT(*) FROM key_screen_review_items '
            "WHERE review_status = 'confirmed' AND confirmed_label IS NOT NULL "
            "AND visual_condition = 'unreadable'"
        ).fetchone()[0]
    )
    for row in reviewed:
        frame = dict(row)
        sample = _frame_sample(conn, frame)
        if sample is None:
            continue
        sample['label'] = frame['confirmed_label']
        sample['label_source'] = 'key_screen_review_confirmed'
        sample['prelabel_model'] = frame['model_version']
        sample['visual_condition'] = frame['visual_condition']
        samples_by_frame[frame['id']] = sample

    available_samples = list(samples_by_frame.values())
    non_other = [sample for sample in available_samples if sample['label'] != 'other']
    other = sorted(
        (sample for sample in available_samples if sample['label'] == 'other'),
        key=_key_screen_other_rank,
    )
    # 大量普通游戏帧会淹没结算页/计分板。保留至少 300 张且最多为
    # 两个目标类总量的 3 倍，并用固定哈希排序使每次快照可复现。
    maximum_other = max(300, len(non_other) * 3)
    samples = non_other + other[:maximum_other]
    samples.sort(
        key=lambda sample: (
            int(sample['video_id']),
            int(sample['timestamp_ms']),
            sample['sample_id'],
        )
    )
    if not samples:
        raise RuntimeError('没有可导出的关键画面人工确认样本')
    video_ids = sorted({sample['video_id'] for sample in samples})
    split = split_classification_by_video(
        samples, ('result_page', 'scoreboard', 'other')
    )
    video_split = {video_id: name for name, ids in split.items() for video_id in ids}
    for sample in samples:
        sample['split'] = video_split[sample['video_id']]

    version_id = next_version_id(conn, 'key_screen_review')
    out_dir = config.EXPORT_DIR / version_id
    if out_dir.exists():
        raise RuntimeError(f'数据集版本已存在: {version_id}')
    out_dir.mkdir(parents=True, exist_ok=False)
    jsonl_path = out_dir / 'samples.jsonl'
    with jsonl_path.open('w', encoding='utf-8') as handle:
        for sample in samples:
            handle.write(json.dumps(sample, ensure_ascii=False) + '\n')
    labels = ('result_page', 'scoreboard', 'other')
    _write_classification_images(conn, out_dir, samples, labels)
    counts = {
        'total': len(samples),
        'videos': len(video_ids),
        'excluded_unreadable': excluded_unreadable,
        'available_other': len(other),
        'excluded_other_balance': max(0, len(other) - maximum_other),
        'by_label': {
            label: sum(1 for sample in samples if sample['label'] == label)
            for label in labels
        },
        'by_split': {
            name: sum(1 for sample in samples if sample['split'] == name)
            for name in ('train', 'val', 'test')
        },
    }
    db.create_dataset_version(
        conn,
        version_id=version_id,
        task_id='key_screen_review',
        filter_json={
            'labels': list(labels),
            'confirmed_only': True,
            'excluded_visual_conditions': ['unreadable'],
            'other_sampling': {
                'policy': 'confirmed_and_post_match_first_then_stable_hash',
                'maximum': maximum_other,
            },
            'split_unit': 'video',
        },
        counts=counts,
        manifest_path=str(jsonl_path),
        git_commit=_git_commit(),
    )
    return {'version': version_id, 'dir': str(out_dir), **counts}


# ---------- 七类画面状态分类导出 ----------

SCREEN_STATE_LABELS = (
    'not_vainglory',
    'out_of_match',
    'pre_match',
    'in_match',
    'talent_select',
    'post_match',
    'transition',
)


def _screen_state_label(annotation: Dict[str, Any]) -> Optional[str]:
    if annotation.get('content_family') == 'not_vainglory':
        return 'not_vainglory'
    if annotation.get('content_family') != 'vainglory':
        return None
    context = annotation.get('game_context')
    if context == 'in_match' and annotation.get('screen_type') == 'talent_select':
        return 'talent_select'
    if context in {'out_of_match', 'pre_match', 'in_match', 'post_match', 'transition'}:
        return str(context)
    return None


def export_screen_state_classifier(conn: Any) -> Dict[str, Any]:
    """把旧通用标注和新 worker 人工复核合并为七分类不可变快照。"""
    samples_by_frame: Dict[int, Dict[str, Any]] = {}
    rows = conn.execute(
        'SELECT f.*, v.streamer, v.remote_path '
        'FROM annotations a JOIN frames f ON f.id = a.frame_id '
        'JOIN videos v ON v.id = f.video_id '
        "WHERE a.annotation_status = 'complete' "
        'ORDER BY f.video_id, f.timestamp_ms'
    ).fetchall()
    for row in rows:
        frame = dict(row)
        sample = _frame_sample(conn, frame)
        if sample is None:
            continue
        label = _screen_state_label(sample['annotation'])
        if label is None:
            continue
        sample['label'] = label
        sample['label_source'] = 'existing_human_annotation'
        sample['visual_condition'] = 'clear'
        samples_by_frame[int(frame['id'])] = sample

    reviewed = conn.execute(
        'SELECT f.*, v.streamer, v.remote_path, c.confirmed_label, '
        'c.visual_condition FROM worker_candidate_items c '
        'JOIN frames f ON f.id = c.frame_id '
        'JOIN videos v ON v.id = f.video_id '
        "WHERE c.task = 'screen_state' AND c.review_status = 'confirmed' "
        'AND c.confirmed_label IS NOT NULL '
        "AND c.visual_condition != 'unreadable' "
        'ORDER BY f.video_id, f.timestamp_ms'
    ).fetchall()
    for row in reviewed:
        frame = dict(row)
        sample = _frame_sample(conn, frame)
        if sample is None:
            continue
        sample['label'] = str(frame['confirmed_label'])
        sample['label_source'] = 'worker_candidate_confirmed'
        sample['visual_condition'] = str(frame['visual_condition'])
        samples_by_frame[int(frame['id'])] = sample

    available = list(samples_by_frame.values())
    if not available:
        raise RuntimeError('没有可导出的画面状态人工确认样本')
    non_in_match = [sample for sample in available if sample['label'] != 'in_match']
    in_match = sorted(
        (sample for sample in available if sample['label'] == 'in_match'),
        key=lambda sample: (
            sample['label_source'] != 'worker_candidate_confirmed',
            str(sample.get('sha256') or sample['sample_id']),
        ),
    )
    largest_other = max(
        (
            sum(1 for sample in non_in_match if sample['label'] == label)
            for label in SCREEN_STATE_LABELS
            if label != 'in_match'
        ),
        default=0,
    )
    maximum_in_match = max(500, largest_other * 3)
    samples = non_in_match + in_match[:maximum_in_match]
    samples.sort(
        key=lambda sample: (
            int(sample['video_id']),
            int(sample['timestamp_ms']),
            sample['sample_id'],
        )
    )
    split = split_classification_by_video(samples, SCREEN_STATE_LABELS)
    video_split = {
        video_id: name for name, video_ids in split.items() for video_id in video_ids
    }
    for sample in samples:
        sample['split'] = video_split[sample['video_id']]

    version_id = next_version_id(conn, 'screen_state')
    out_dir = config.EXPORT_DIR / version_id
    if out_dir.exists():
        raise RuntimeError(f'数据集版本已存在: {version_id}')
    out_dir.mkdir(parents=True, exist_ok=False)
    jsonl_path = out_dir / 'samples.jsonl'
    with jsonl_path.open('w', encoding='utf-8') as handle:
        for sample in samples:
            handle.write(json.dumps(sample, ensure_ascii=False) + '\n')
    _write_classification_images(conn, out_dir, samples, SCREEN_STATE_LABELS)
    counts = {
        'total': len(samples),
        'videos': len({sample['video_id'] for sample in samples}),
        'available_in_match': len(in_match),
        'excluded_in_match_balance': max(0, len(in_match) - maximum_in_match),
        'by_label': {
            label: sum(1 for sample in samples if sample['label'] == label)
            for label in SCREEN_STATE_LABELS
        },
        'by_split': {
            name: sum(1 for sample in samples if sample['split'] == name)
            for name in ('train', 'val', 'test')
        },
    }
    db.create_dataset_version(
        conn,
        version_id=version_id,
        task_id='screen_state',
        filter_json={
            'labels': list(SCREEN_STATE_LABELS),
            'confirmed_only': True,
            'excluded_visual_conditions': ['unreadable'],
            'in_match_maximum': maximum_in_match,
            'split_unit': 'video',
        },
        counts=counts,
        manifest_path=str(jsonl_path),
        git_commit=_git_commit(),
    )
    return {'version': version_id, 'dir': str(out_dir), **counts}


# ---------- 3V3 / 大乱斗光栅检测导出 ----------


def export_mode_gate_detector(conn: Any) -> Dict[str, Any]:
    """导出单类光栅检测数据集；开放入口帧是无框 hard negative。"""
    rows = conn.execute(
        'SELECT mga.round_id, mga.frame_id, mga.evidence, mga.updated_at, '
        'f.*, v.streamer, v.remote_path '
        'FROM mode_gate_annotations mga '
        'JOIN frames f ON f.id = mga.frame_id '
        'JOIN videos v ON v.id = f.video_id '
        "WHERE mga.evidence IN ('blocked_gate', 'open_entrance') "
        'ORDER BY mga.updated_at, mga.round_id, mga.frame_id'
    ).fetchall()
    samples_by_frame: Dict[int, Dict[str, Any]] = {}
    for row in rows:
        frame = dict(row)
        sample = _frame_sample(conn, frame)
        if sample is None:
            continue
        annotation = db.get_mode_gate_annotation(
            conn, round_id=str(frame['round_id']), frame_id=int(frame['frame_id'])
        )
        if annotation is None:
            continue
        sample['label'] = frame['evidence']
        sample['label_source'] = 'mode_gate_human_annotation'
        sample['round_id'] = frame['round_id']
        sample['mode_gate_boxes'] = (
            annotation['boxes'] if frame['evidence'] == 'blocked_gate' else []
        )
        sample['open_entrance_boxes'] = (
            annotation['boxes'] if frame['evidence'] == 'open_entrance' else []
        )
        samples_by_frame[int(frame['frame_id'])] = sample
    worker_rows = conn.execute(
        'SELECT c.*, f.*, v.streamer, v.remote_path '
        'FROM worker_candidate_items c '
        'JOIN frames f ON f.id = c.frame_id '
        'JOIN videos v ON v.id = f.video_id '
        "WHERE c.task = 'mode_gate' AND c.review_status = 'confirmed' "
        "AND c.confirmed_label IN ('blocked_gate', 'open_entrance') "
        "AND c.visual_condition != 'unreadable'"
    ).fetchall()
    for row in worker_rows:
        frame = dict(row)
        frame['id'] = int(row['frame_id'])
        sample = _frame_sample(conn, frame)
        if sample is None:
            continue
        label = str(frame['confirmed_label'])
        candidate_boxes = json.loads(frame['boxes_json'] or '[]')
        sample['label'] = label
        sample['label_source'] = 'worker_candidate_confirmed'
        sample['round_id'] = 'worker_candidates'
        sample['mode_gate_boxes'] = candidate_boxes if label == 'blocked_gate' else []
        sample['open_entrance_boxes'] = (
            candidate_boxes if label == 'open_entrance' else []
        )
        samples_by_frame[int(frame['frame_id'])] = sample
    samples = list(samples_by_frame.values())
    if not samples:
        raise RuntimeError('没有可导出的光栅或开放入口人工标注')

    video_ids = sorted({sample['video_id'] for sample in samples})
    split = split_detection_by_video(
        samples, label_field='label', positive_label='blocked_gate'
    )
    video_split = {video_id: name for name, ids in split.items() for video_id in ids}
    for sample in samples:
        sample['split'] = video_split[sample['video_id']]

    version_id = next_version_id(conn, 'mode_gate')
    out_dir = config.EXPORT_DIR / version_id
    if out_dir.exists():
        raise RuntimeError(f'数据集版本已存在: {version_id}')
    for split_name in ('train', 'val', 'test'):
        (out_dir / 'images' / split_name).mkdir(parents=True, exist_ok=True)
        (out_dir / 'labels' / split_name).mkdir(parents=True, exist_ok=True)
    for sample in samples:
        source = _path_for(conn, sample)
        if source is None:
            continue
        image_name = f"{sample['sample_id']}.jpg"
        destination = out_dir / 'images' / sample['split'] / image_name
        shutil.copy2(source, destination)
        label_path = out_dir / 'labels' / sample['split'] / f"{sample['sample_id']}.txt"
        lines = []
        for box in sample['mode_gate_boxes']:
            center_x = float(box['x']) + float(box['w']) / 2
            center_y = float(box['y']) + float(box['h']) / 2
            lines.append(
                '0 {:.6f} {:.6f} {:.6f} {:.6f}'.format(
                    center_x, center_y, float(box['w']), float(box['h'])
                )
            )
        label_path.write_text(
            ('\n'.join(lines) + '\n') if lines else '', encoding='utf-8'
        )
    (out_dir / 'data.yaml').write_text(
        f'path: {out_dir}\ntrain: images/train\nval: images/val\n'
        "test: images/test\nnc: 1\nnames: ['mode_gate']\n",
        encoding='utf-8',
    )
    jsonl_path = out_dir / 'samples.jsonl'
    with jsonl_path.open('w', encoding='utf-8') as handle:
        for sample in samples:
            handle.write(json.dumps(sample, ensure_ascii=False) + '\n')
    counts = {
        'total': len(samples),
        'positive': sum(1 for sample in samples if sample['label'] == 'blocked_gate'),
        'negative': sum(1 for sample in samples if sample['label'] == 'open_entrance'),
        'boxes': sum(len(sample['mode_gate_boxes']) for sample in samples),
        'videos': len(video_ids),
        'by_split': {
            name: sum(1 for sample in samples if sample['split'] == name)
            for name in ('train', 'val', 'test')
        },
    }
    db.create_dataset_version(
        conn,
        version_id=version_id,
        task_id='mode_gate',
        filter_json={
            'positive_evidence': 'blocked_gate',
            'negative_evidence': 'open_entrance',
            'excluded_evidence': ['no_evidence'],
            'split_unit': 'video',
        },
        counts=counts,
        manifest_path=str(jsonl_path),
        git_commit=_git_commit(),
    )
    return {'version': version_id, 'dir': str(out_dir), **counts}
