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
from typing import Any, Dict, List, Optional, Tuple

from . import config, db


def _git_commit() -> str:
    try:
        out = subprocess.run(
            ['git', '-C', str(Path(__file__).resolve().parent.parent.parent.parent),
             'rev-parse', '--short', 'HEAD'],
            capture_output=True, text=True, timeout=5,
        )
        return out.stdout.strip() if out.returncode == 0 else ''
    except Exception:  # noqa: BLE001
        return ''


def next_version_id(conn: Any, task_id: str) -> str:
    prefix = {'result_detector': 'result-detector',
              'game_state': 'game-state',
              'game_mode': 'game-mode',
              'bp_review': 'bp-classifier',
              'key_screen_review': 'key-screen-classifier',
              'mode_gate': 'mode-gate-detector',
              'viewport': 'viewport',
              'same_match': 'same-match'}.get(task_id, task_id)
    existing = [r['id'] for r in conn.execute(
        'SELECT id FROM dataset_versions').fetchall()]
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

def _frame_sample(conn: Any, f: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """组装单帧的 JSONL 样本;缺原始文件返回 None。"""
    path = Path(f['frame_path'])
    if not path.exists():
        return None
    ann = db.get_annotation(conn, f['id']) or {}
    boxes = db.get_boxes(conn, f['id'])
    return {
        'sample_id': f'f{f["id"]:08d}',
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
        'boxes': boxes,
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

def split_by_video(video_ids: List[int], ratio: Tuple[float, float, float] = (0.8, 0.1, 0.1)
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
        'val': ids[n_train:n_train + n_val],
        'test': ids[n_train + n_val:n_train + n_val + n_test],
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
        source_name = next((
            name for name in ('val', 'test')
            if any(label in video_labels[video_id] for video_id in split[name])
        ), None)
        if source_name is None:
            continue
        video_id = min(
            (video_id for video_id in split[source_name]
             if label in video_labels[video_id]),
            key=lambda item: (video_sizes[item], item),
        )
        split[source_name].remove(video_id)
        split['train'].append(video_id)

    # 验证集优先覆盖所有类别；测试集只在训练集仍有同类
    # 其他视频时补齐，防止稀有类从训练集消失。
    for target_name in ('val', 'test'):
        while True:
            missing = {
                label for label in labels
                if not any(
                    label in video_labels[video_id]
                    for video_id in split[target_name]
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
                        if not all(any(
                                label in video_labels[other]
                                for other in remaining)
                                for label in video_labels[video_id]):
                            continue
                    eligible.append((
                        len(provided), int(source_name != 'train'),
                        -video_sizes[video_id], -video_id,
                        source_name, video_id))
            if not eligible:
                break
            *_, source_name, video_id = max(eligible)
            split[source_name].remove(video_id)
            split[target_name].append(video_id)
    return {name: sorted(video_ids) for name, video_ids in split.items()}


# ---------- 结算检测导出 ----------

def export_result_detector(conn: Any, *, include_negatives: bool = True,
                           max_negatives: Optional[int] = None,
                           version: Optional[str] = None) -> Dict[str, Any]:
    """导出 result_detector 数据集(JSONL + YOLO + COCO),创建不可变版本。"""
    frames = _labeled_frames(conn)
    result_frames = [
        f for f in frames
        if (db.get_annotation(conn, f['id']) or {}).get('screen_type')
        == 'result_page'
    ]
    positives = [
        f for f in result_frames if db.get_boxes(conn, f['id']).get('result_panel')
    ]
    result_frame_ids = {f['id'] for f in result_frames}
    # 负样本:非 result_page 的已标帧(含积分板 hard negative、随机负样本、其他)
    negatives = [f for f in frames if f['id'] not in result_frame_ids] \
        if include_negatives else []
    if max_negatives and len(negatives) > max_negatives:
        # 积分板(硬负样本)优先保留,其余按帧 id 截断(可复现)
        def _is_hard(f: Dict[str, Any]) -> bool:
            st = (db.get_annotation(conn, f['id']) or {}).get('screen_type')
            return st in ('scoreboard', 'death_scoreboard')
        hard = [f for f in negatives if _is_hard(f)]
        rest = sorted((f for f in negatives if not _is_hard(f)),
                      key=lambda f: f['id'])
        negatives = hard + rest[:max(0, max_negatives - len(hard))]
    samples: List[Dict[str, Any]] = []
    for f in positives + negatives:
        s = _frame_sample(conn, f)
        if s is not None:
            samples.append(s)

    if not samples:
        raise RuntimeError('没有可导出的已标注样本')

    # 切分:按视频
    video_ids = sorted({s['video_id'] for s in samples})
    split = split_by_video(video_ids)
    v2split = {vid: k for k, vids in split.items() for vid in vids}
    for s in samples:
        s['split'] = v2split[s['video_id']]

    version_id = version or next_version_id(conn, 'result_detector')
    out_dir = config.EXPORT_DIR / version_id
    if out_dir.exists():
        raise RuntimeError(f'数据集版本已存在,禁止覆盖: {version_id}')
    images_dir = out_dir / 'images'
    labels_dir = out_dir / 'labels'
    for split_name in ('train', 'val', 'test'):
        (images_dir / split_name).mkdir(parents=True, exist_ok=True)
        (labels_dir / split_name).mkdir(parents=True, exist_ok=True)

    coco_images = []
    coco_annotations = []
    coco_id = 0
    for s in samples:
        src = _path_for(conn, s)
        if src is None:
            continue
        split_name = s['split']
        img_name = f"{s['sample_id']}.jpg"
        dst = images_dir / split_name / img_name
        if not dst.exists():
            shutil.copy2(src, dst)
        # YOLO 标签:正样本写 result_panel 框,负样本空
        box = s['boxes'].get('result_panel')
        txt = labels_dir / split_name / f"{s['sample_id']}.txt"
        if box:
            x = box['x'] + box['w'] / 2
            y = box['y'] + box['h'] / 2
            txt.write_text(f"0 {x:.6f} {y:.6f} {box['w']:.6f} {box['h']:.6f}\n")
        else:
            txt.write_text('')
        # COCO
        coco_id += 1
        coco_images.append({
            'id': coco_id, 'file_name': f'{split_name}/{img_name}',
            'width': s['width'], 'height': s['height'],
        })
        if box:
            coco_annotations.append({
                'id': coco_id, 'image_id': coco_id,
                'category_id': 1,
                'bbox': [box['x'] * s['width'], box['y'] * s['height'],
                         box['w'] * s['width'], box['h'] * s['height']],
                'area': box['w'] * s['width'] * box['h'] * s['height'],
                'iscrowd': 0,
            })

    # COCO JSON
    (out_dir / 'coco_annotations.json').write_text(json.dumps({
        'images': coco_images,
        'annotations': coco_annotations,
        'categories': [{'id': 1, 'name': 'result_panel'}],
    }, ensure_ascii=False), encoding='utf-8')
    # YOLO data.yaml
    (out_dir / 'data.yaml').write_text(
        f"path: {out_dir}\ntrain: images/train\nval: images/val\n"
        f"test: images/test\nnc: 1\nnames: ['result_panel']\n",
        encoding='utf-8')
    # JSONL 清单
    jsonl_path = out_dir / 'samples.jsonl'
    with jsonl_path.open('w', encoding='utf-8') as fh:
        for s in samples:
            fh.write(json.dumps(s, ensure_ascii=False) + '\n')

    # 版本记录
    counts = {
        'total': len(samples), 'positive': len(positives),
        'negative': len(negatives),
        'result_with_bbox': sum(1 for s in samples if s['boxes'].get('result_panel')),
        'excluded_result_without_bbox': len(result_frames) - len(positives),
        'by_split': {k: sum(1 for s in samples if s['split'] == k)
                     for k in ('train', 'val', 'test')},
        'videos': len(video_ids),
    }
    db.create_dataset_version(
        conn, version_id=version_id, task_id='result_detector',
        filter_json={
            'include_negatives': include_negatives,
            'max_negatives': max_negatives,
            'negative_priority': ['scoreboard', 'death_scoreboard', 'frame_id'],
        },
        counts=counts, manifest_path=str(jsonl_path),
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
        conn: Any, out_dir: Path, samples: List[Dict[str, Any]],
        labels: Tuple[str, ...]) -> None:
    """把分类快照组织为 Ultralytics ImageFolder 目录。"""
    for split_name in ('train', 'val', 'test'):
        for label in labels:
            (out_dir / 'images' / split_name / label).mkdir(
                parents=True, exist_ok=True)
    for sample in samples:
        source = _path_for(conn, sample)
        if source is None:
            continue
        destination = (
            out_dir / 'images' / sample['split'] / sample['label'] /
            f"{sample['sample_id']}.jpg"
        )
        if not destination.exists():
            shutil.copy2(source, destination)


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
    counts = {'total': len(samples),
              'videos': len(video_ids),
              'by_split': {k: sum(1 for s in samples if s['split'] == k)
                           for k in ('train', 'val', 'test')}}
    db.create_dataset_version(
        conn, version_id=version_id, task_id=task_id,
        filter_json={}, counts=counts, manifest_path=str(jsonl_path),
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
    mode_labels = {
        '3v3': 'bp_3v3',
        'aram': 'bp_aram',
        '5v5': 'bp_5v5',
    }
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
            'not_bp' if frame['screen_type'] == 'match_confirm'
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
    excluded_unreadable = int(conn.execute(
        "SELECT COUNT(*) FROM bp_review_items WHERE review_status = 'confirmed' "
        "AND confirmed_label IS NOT NULL AND visual_condition = 'unreadable'"
    ).fetchone()[0])
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
        samples, ('bp_3v3', 'bp_aram', 'bp_5v5', 'not_bp'))
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
            label: sum(1 for s in samples if s['label'] == label)
            for label in labels
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
        conn, version_id=version_id, task_id='bp_review',
        filter_json={
            'labels': list(labels),
            'confirmed_only': True,
            'excluded_visual_conditions': ['unreadable'],
            'split_unit': 'video',
        },
        counts=counts, manifest_path=str(jsonl_path),
        git_commit=_git_commit(),
    )
    return {'version': version_id, 'dir': str(out_dir), **counts}


# ---------- 结算页 / 计分板三分类导出 ----------

def _key_screen_other_rank(sample: Dict[str, Any]) -> Tuple[int, str]:
    """优先保留人工确认和最容易与结算页混淆的负样本。"""
    if sample.get('label_source') == 'key_screen_review_confirmed':
        priority = 0
    elif sample.get('annotation', {}).get('screen_type') in {
            'victory_defeat_animation', 'other_post'}:
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
    excluded_unreadable = int(conn.execute(
        'SELECT COUNT(*) FROM key_screen_review_items '
        "WHERE review_status = 'confirmed' AND confirmed_label IS NOT NULL "
        "AND visual_condition = 'unreadable'"
    ).fetchone()[0])
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
    non_other = [
        sample for sample in available_samples if sample['label'] != 'other'
    ]
    other = sorted(
        (sample for sample in available_samples if sample['label'] == 'other'),
        key=_key_screen_other_rank,
    )
    # 大量普通游戏帧会淹没结算页/计分板。保留至少 300 张且最多为
    # 两个目标类总量的 3 倍，并用固定哈希排序使每次快照可复现。
    maximum_other = max(300, len(non_other) * 3)
    samples = non_other + other[:maximum_other]
    samples.sort(key=lambda sample: (
        int(sample['video_id']), int(sample['timestamp_ms']), sample['sample_id']))
    if not samples:
        raise RuntimeError('没有可导出的关键画面人工确认样本')
    video_ids = sorted({sample['video_id'] for sample in samples})
    split = split_classification_by_video(
        samples, ('result_page', 'scoreboard', 'other'))
    video_split = {
        video_id: name for name, ids in split.items() for video_id in ids
    }
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
            conn,
            round_id=str(frame['round_id']),
            frame_id=int(frame['frame_id']),
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
    samples = list(samples_by_frame.values())
    if not samples:
        raise RuntimeError('没有可导出的光栅或开放入口人工标注')

    video_ids = sorted({sample['video_id'] for sample in samples})
    split = split_by_video(video_ids)
    video_split = {
        video_id: name for name, ids in split.items() for video_id in ids
    }
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
        label_path = (
            out_dir / 'labels' / sample['split'] /
            f"{sample['sample_id']}.txt"
        )
        lines = []
        for box in sample['mode_gate_boxes']:
            center_x = float(box['x']) + float(box['w']) / 2
            center_y = float(box['y']) + float(box['h']) / 2
            lines.append(
                '0 {:.6f} {:.6f} {:.6f} {:.6f}'.format(
                    center_x, center_y, float(box['w']), float(box['h'])))
        label_path.write_text(
            ('\n'.join(lines) + '\n') if lines else '', encoding='utf-8')
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
        'positive': sum(
            1 for sample in samples if sample['label'] == 'blocked_gate'),
        'negative': sum(
            1 for sample in samples if sample['label'] == 'open_entrance'),
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
