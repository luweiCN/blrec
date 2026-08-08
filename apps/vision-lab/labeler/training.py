"""不可变数据集快照上的本机训练任务与模型发布。"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
from uuid import uuid4

from . import config, db, export

PROGRESS_PREFIX = '@@BLREC_TRAIN_PROGRESS@@'
RESULT_PREFIX = '@@BLREC_TRAIN_RESULT@@'

TRAINING_TASKS: Dict[str, Dict[str, Any]] = {
    'bp_review': {
        'name': 'BP 模式分类',
        'kind': 'classify',
        'description': '识别 3V3、大乱斗、5V5 BP，并排除匹配确认等非 BP 画面。',
        'epochs': 60,
        'imgsz': 224,
        'base_model': 'yolov8n-cls.pt',
        'publish_name': 'bp-classifier-current.onnx',
        'recommended': '每种 BP 至少 100 张，非 BP 至少 200 张，并覆盖多个主播。',
    },
    'key_screen_review': {
        'name': '结算页／计分板分类',
        'kind': 'classify',
        'description': '区分赛后结算页、对局中计分板和其他易混淆画面。',
        'epochs': 60,
        'imgsz': 224,
        'base_model': 'yolov8n-cls.pt',
        'publish_name': 'key-screen-classifier-current.onnx',
        'recommended': '结算页和计分板各 100 张以上，其他 hard negative 300 张以上。',
    },
    'mode_gate': {
        'name': '大乱斗光栅检测',
        'kind': 'detect',
        'description': '检测大乱斗地图入口的黄色光栅；开放入口画面作为负样本。',
        'epochs': 100,
        'imgsz': 640,
        'base_model': 'yolov8n.pt',
        'publish_name': 'mode-gate-detector-current.onnx',
        'recommended': '至少 100 张有光栅、100 张同位置开放入口，覆盖不同设备。',
    },
    'result_detector': {
        'name': '结算面板检测',
        'kind': 'detect',
        'description': '定位真正结算面板，计分板和其他游戏画面作为 hard negative。',
        'epochs': 100,
        'imgsz': 640,
        'base_model': 'yolov8n.pt',
        'publish_name': 'result-detector-current.onnx',
        'recommended': '120～250 张有框结算页，800～1500 张负样本，重点覆盖计分板。',
    },
}


def _existing_bp_labels(conn: Any) -> Dict[int, str]:
    labels: Dict[int, str] = {}
    mode_labels = {'3v3': 'bp_3v3', 'aram': 'bp_aram', '5v5': 'bp_5v5'}
    rows = conn.execute(
        'SELECT a.frame_id, a.game_mode, a.screen_type, f.frame_path '
        'FROM annotations a JOIN frames f ON f.id = a.frame_id '
        "WHERE a.annotation_status = 'complete' "
        "AND a.screen_type IN ('hero_select_bp', 'hero_select_blind', "
        "'hero_select_aram', 'match_confirm')"
    ).fetchall()
    for row in rows:
        if not Path(row['frame_path']).is_file():
            continue
        label = (
            'not_bp'
            if row['screen_type'] == 'match_confirm'
            else mode_labels.get(row['game_mode'])
        )
        if label is None and row['screen_type'] == 'hero_select_aram':
            label = 'bp_aram'
        if label is not None:
            labels[int(row['frame_id'])] = label
    reviewed = conn.execute(
        'SELECT k.frame_id, k.confirmed_label, k.visual_condition, f.frame_path '
        'FROM bp_review_items k JOIN frames f ON f.id = k.frame_id '
        "WHERE k.review_status = 'confirmed' AND k.confirmed_label IS NOT NULL"
    ).fetchall()
    for row in reviewed:
        frame_id = int(row['frame_id'])
        if (
            row['visual_condition'] == 'unreadable'
            or not Path(row['frame_path']).is_file()
        ):
            labels.pop(frame_id, None)
        else:
            labels[frame_id] = str(row['confirmed_label'])
    return labels


def _existing_key_screen_labels(conn: Any) -> Dict[int, str]:
    labels: Dict[int, str] = {}
    rows = conn.execute(
        'SELECT a.frame_id, a.screen_type, f.frame_path '
        'FROM annotations a JOIN frames f ON f.id = a.frame_id '
        "WHERE a.annotation_status = 'complete'"
    ).fetchall()
    for row in rows:
        if not Path(row['frame_path']).is_file():
            continue
        screen_type = row['screen_type']
        if screen_type == 'result_page':
            label = 'result_page'
        elif screen_type in ('scoreboard', 'death_scoreboard'):
            label = 'scoreboard'
        else:
            label = 'other'
        labels[int(row['frame_id'])] = label
    reviewed = conn.execute(
        'SELECT k.frame_id, k.confirmed_label, k.visual_condition, f.frame_path '
        'FROM key_screen_review_items k JOIN frames f ON f.id = k.frame_id '
        "WHERE k.review_status = 'confirmed' AND k.confirmed_label IS NOT NULL"
    ).fetchall()
    for row in reviewed:
        frame_id = int(row['frame_id'])
        if (
            row['visual_condition'] == 'unreadable'
            or not Path(row['frame_path']).is_file()
        ):
            labels.pop(frame_id, None)
        else:
            labels[frame_id] = str(row['confirmed_label'])
    return labels


def _classification_summary(
    labels: Dict[int, str], required: List[str]
) -> Dict[str, Any]:
    counts = {
        label: sum(1 for value in labels.values() if value == label)
        for label in required
    }
    return {'total': len(labels), 'by_label': counts}


def _video_count_for_frames(conn: Any, frame_ids: List[int]) -> int:
    if not frame_ids:
        return 0
    selected = set(frame_ids)
    return len(
        {
            int(row['video_id'])
            for row in conn.execute('SELECT id, video_id FROM frames').fetchall()
            if int(row['id']) in selected
        }
    )


def _videos_by_label(
    conn: Any, labels: Dict[int, str], required: List[str]
) -> Dict[str, int]:
    selected = set(labels)
    frame_videos = {
        int(row['id']): int(row['video_id'])
        for row in conn.execute('SELECT id, video_id FROM frames').fetchall()
        if int(row['id']) in selected
    }
    return {
        label: len(
            {
                frame_videos[frame_id]
                for frame_id, value in labels.items()
                if value == label and frame_id in frame_videos
            }
        )
        for label in required
    }


def _task_counts(conn: Any, task_id: str) -> Dict[str, Any]:
    if task_id == 'bp_review':
        labels = _existing_bp_labels(conn)
        required = ['bp_3v3', 'bp_aram', 'bp_5v5', 'not_bp']
        counts = _classification_summary(labels, required)
        counts['videos_by_label'] = _videos_by_label(conn, labels, required)
        video_count = _video_count_for_frames(conn, list(labels))
    elif task_id == 'key_screen_review':
        labels = _existing_key_screen_labels(conn)
        required = ['result_page', 'scoreboard', 'other']
        counts = _classification_summary(labels, required)
        counts['videos_by_label'] = _videos_by_label(conn, labels, required)
        video_count = _video_count_for_frames(conn, list(labels))
    elif task_id == 'mode_gate':
        rows = conn.execute(
            'SELECT mga.evidence, COUNT(DISTINCT mga.frame_id) AS count '
            'FROM mode_gate_annotations mga JOIN frames f ON f.id = mga.frame_id '
            "WHERE mga.evidence IN ('blocked_gate', 'open_entrance') "
            "AND f.frame_path != '' GROUP BY mga.evidence"
        ).fetchall()
        by_evidence = {row['evidence']: row['count'] for row in rows}
        counts = {
            'total': sum(by_evidence.values()),
            'positive': int(by_evidence.get('blocked_gate', 0)),
            'negative': int(by_evidence.get('open_entrance', 0)),
        }
        video_count = int(
            conn.execute(
                'SELECT COUNT(DISTINCT f.video_id) '
                'FROM mode_gate_annotations mga '
                'JOIN frames f ON f.id = mga.frame_id '
                "WHERE mga.evidence IN ('blocked_gate', 'open_entrance') "
                "AND f.frame_path != ''"
            ).fetchone()[0]
        )
    elif task_id == 'result_detector':
        positive = int(
            conn.execute(
                'SELECT COUNT(DISTINCT a.frame_id) FROM annotations a '
                'JOIN boxes b ON b.frame_id = a.frame_id '
                'JOIN frames f ON f.id = a.frame_id '
                "WHERE a.annotation_status = 'complete' "
                "AND a.screen_type = 'result_page' "
                "AND b.box_type = 'result_panel' AND f.frame_path != ''"
            ).fetchone()[0]
        )
        negative = int(
            conn.execute(
                'SELECT COUNT(*) FROM annotations a '
                'JOIN frames f ON f.id = a.frame_id '
                "WHERE a.annotation_status = 'complete' "
                "AND COALESCE(a.screen_type, '') != 'result_page' "
                "AND f.frame_path != ''"
            ).fetchone()[0]
        )
        hard_negative = int(
            conn.execute(
                'SELECT COUNT(*) FROM annotations a '
                'JOIN frames f ON f.id = a.frame_id '
                "WHERE a.annotation_status = 'complete' "
                "AND a.screen_type IN ('scoreboard', 'death_scoreboard') "
                "AND f.frame_path != ''"
            ).fetchone()[0]
        )
        counts = {
            'total': positive + negative,
            'positive': positive,
            'negative': negative,
            'hard_negative': hard_negative,
        }
        video_count = int(
            conn.execute(
                'SELECT COUNT(DISTINCT f.video_id) FROM annotations a '
                'JOIN frames f ON f.id = a.frame_id '
                "WHERE a.annotation_status = 'complete' AND f.frame_path != ''"
            ).fetchone()[0]
        )
    else:
        raise ValueError(f'未知训练任务: {task_id}')
    counts['videos'] = video_count
    return counts


def _blocking_reasons(task_id: str, counts: Dict[str, Any]) -> List[str]:
    reasons = []
    if int(counts.get('videos', 0)) < 2:
        reasons.append('至少需要 2 个视频，才能把训练集和验证集按视频分开')
    if task_id == 'bp_review':
        labels = counts.get('by_label', {})
        names = {
            'bp_3v3': '3V3 BP',
            'bp_aram': '大乱斗 BP',
            'bp_5v5': '5V5 BP',
            'not_bp': '非 BP',
        }
        for label, name in names.items():
            if int(labels.get(label, 0)) < 2:
                reasons.append(f'{name} 至少需要 2 张有效图片')
            elif int(counts.get('videos_by_label', {}).get(label, 0)) < 2:
                reasons.append(f'{name} 至少需要来自 2 个不同视频')
    elif task_id == 'key_screen_review':
        labels = counts.get('by_label', {})
        names = {'result_page': '结算页', 'scoreboard': '计分板', 'other': '其他画面'}
        for label, name in names.items():
            if int(labels.get(label, 0)) < 2:
                reasons.append(f'{name} 至少需要 2 张有效图片')
            elif int(counts.get('videos_by_label', {}).get(label, 0)) < 2:
                reasons.append(f'{name} 至少需要来自 2 个不同视频')
    else:
        if int(counts.get('positive', 0)) < 2:
            reasons.append('正样本至少需要 2 张有效图片')
        if int(counts.get('negative', 0)) < 2:
            reasons.append('负样本至少需要 2 张有效图片')
    return reasons


def _quality_warnings(task_id: str, counts: Dict[str, Any]) -> List[str]:
    """已经能跑训练，但还没达到第一轮正式模型的建议量。"""
    if task_id == 'bp_review':
        values = counts.get('by_label', {})
        targets = {
            'bp_3v3': ('3V3 BP', 100),
            'bp_aram': ('大乱斗 BP', 100),
            'bp_5v5': ('5V5 BP', 100),
            'not_bp': ('非 BP', 200),
        }
    elif task_id == 'key_screen_review':
        values = counts.get('by_label', {})
        targets = {
            'result_page': ('结算页', 100),
            'scoreboard': ('计分板', 100),
            'other': ('其他画面', 300),
        }
    elif task_id == 'mode_gate':
        values = counts
        targets = {'positive': ('有光栅', 100), 'negative': ('开放入口', 100)}
    else:
        values = counts
        targets = {
            'positive': ('有结算面板', 120),
            'negative': ('负样本', 800),
            'hard_negative': ('计分板 hard negative', 100),
        }
    return [
        f'{name} {int(values.get(key, 0))}/{target}'
        for key, (name, target) in targets.items()
        if int(values.get(key, 0)) < target
    ]


def task_summaries(conn: Any) -> List[Dict[str, Any]]:
    summaries = []
    for task_id, definition in TRAINING_TASKS.items():
        counts = _task_counts(conn, task_id)
        reasons = _blocking_reasons(task_id, counts)
        warnings = _quality_warnings(task_id, counts)
        summaries.append(
            {
                'id': task_id,
                **definition,
                'counts': counts,
                'ready': not reasons,
                'blocking_reasons': reasons,
                'quality_warnings': warnings,
            }
        )
    return summaries


def export_snapshot(conn: Any, task_id: str) -> Dict[str, Any]:
    if task_id == 'bp_review':
        return export.export_bp_classifier(conn)
    if task_id == 'key_screen_review':
        return export.export_key_screen_classifier(conn)
    if task_id == 'mode_gate':
        return export.export_mode_gate_detector(conn)
    if task_id == 'result_detector':
        return export.export_result_detector(
            conn, include_negatives=True, max_negatives=1_500
        )
    raise ValueError(f'未知训练任务: {task_id}')


def new_run_id(task_id: str) -> str:
    stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
    return '{}-{}-{}'.format(task_id.replace('_', '-'), stamp, uuid4().hex[:6])


class TrainingManager:
    """一次只运行一个训练进程，避免多个任务争抢 MPS/内存。"""

    def __init__(self, db_path: Path = config.DB_PATH) -> None:
        self._db_path = Path(db_path)
        self._lock = threading.RLock()
        self._active_run_id: Optional[str] = None
        self._process: Optional[subprocess.Popen[str]] = None
        self._cancelled = set()

    def active_run_id(self) -> Optional[str]:
        with self._lock:
            return self._active_run_id

    def start(self, run_id: str) -> None:
        with self._lock:
            if self._active_run_id is not None:
                raise RuntimeError(f'已有训练正在运行: {self._active_run_id}')
            self._active_run_id = run_id
        threading.Thread(
            target=self._run, args=(run_id,), name=f'training-{run_id}', daemon=True
        ).start()

    def cancel(self, run_id: str) -> None:
        with self._lock:
            if self._active_run_id != run_id:
                raise KeyError(f'训练任务未在运行: {run_id}')
            self._cancelled.add(run_id)
            process = self._process
        if process is not None and process.poll() is None:
            process.terminate()

    def shutdown(self) -> None:
        """标注服务退出时停止训练子进程，避免留下孤儿进程。"""
        with self._lock:
            run_id = self._active_run_id
            process = self._process
            if run_id is not None:
                self._cancelled.add(run_id)
        if process is None or process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)

    def _update(self, run_id: str, **values: Any) -> None:
        conn = db.connect(self._db_path)
        try:
            db.update_training_run(conn, run_id, **values)
        finally:
            conn.close()

    def _run(self, run_id: str) -> None:
        conn = db.connect(self._db_path)
        try:
            run = db.get_training_run(conn, run_id)
            if run is None:
                raise KeyError(f'训练记录不存在: {run_id}')
            dataset = conn.execute(
                'SELECT manifest_path FROM dataset_versions WHERE id = ?',
                (run['dataset_version_id'],),
            ).fetchone()
            if dataset is None:
                raise KeyError(f'数据集版本不存在: {run["dataset_version_id"]}')
        finally:
            conn.close()
        definition = TRAINING_TASKS[run['task_id']]
        dataset_dir = Path(dataset['manifest_path']).parent
        run_dir = config.WORK_DIR / 'training-runs' / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        artifact_path = run_dir / 'model.onnx'
        log_path = Path(run['log_path'])
        log_path.parent.mkdir(parents=True, exist_ok=True)
        command = [
            sys.executable,
            '-m',
            'labeler.training_runner',
            '--task-id',
            run['task_id'],
            '--kind',
            definition['kind'],
            '--dataset-dir',
            str(dataset_dir),
            '--run-dir',
            str(run_dir),
            '--artifact',
            str(artifact_path),
            '--base-model',
            str(config.MODELS_DIR / 'base' / definition['base_model']),
            '--epochs',
            str(run['epochs']),
            '--imgsz',
            str(definition['imgsz']),
        ]
        metrics: Dict[str, Any] = {}
        try:
            if run_id in self._cancelled:
                self._update(
                    run_id, status='cancelled', finished_at=db.now(), error='用户取消'
                )
                return
            self._update(run_id, status='running', started_at=db.now(), error='')
            environment = dict(os.environ)
            environment['PYTHONUNBUFFERED'] = '1'
            with log_path.open('a', encoding='utf-8') as log_handle:
                process = subprocess.Popen(
                    command,
                    cwd=str(Path(__file__).resolve().parent.parent),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                    env=environment,
                )
                with self._lock:
                    self._process = process
                assert process.stdout is not None
                for line in process.stdout:
                    log_handle.write(line)
                    log_handle.flush()
                    stripped = line.strip()
                    if stripped.startswith(PROGRESS_PREFIX):
                        payload = json.loads(stripped[len(PROGRESS_PREFIX) :])
                        metrics = payload.get('metrics') or metrics
                        self._update(
                            run_id,
                            current_epoch=int(payload.get('epoch', 0)),
                            progress=float(payload.get('progress', 0)),
                            metrics=metrics,
                        )
                    elif stripped.startswith(RESULT_PREFIX):
                        payload = json.loads(stripped[len(RESULT_PREFIX) :])
                        metrics = payload.get('metrics') or metrics
                return_code = process.wait()
            if run_id in self._cancelled:
                self._update(
                    run_id, status='cancelled', finished_at=db.now(), error='用户取消'
                )
            elif return_code != 0:
                self._update(
                    run_id,
                    status='failed',
                    finished_at=db.now(),
                    error=f'训练进程退出码 {return_code}，请查看日志',
                )
            elif not artifact_path.is_file():
                self._update(
                    run_id,
                    status='failed',
                    finished_at=db.now(),
                    error='训练完成但没有生成 ONNX 模型',
                )
            else:
                self._update(
                    run_id,
                    status='succeeded',
                    current_epoch=int(run['epochs']),
                    progress=1.0,
                    metrics=metrics,
                    artifact_path=str(artifact_path),
                    finished_at=db.now(),
                )
        except Exception as error:  # noqa: BLE001
            try:
                self._update(
                    run_id,
                    status='failed',
                    finished_at=db.now(),
                    error=str(error)[:500],
                )
            except Exception:  # noqa: BLE001
                pass
        finally:
            with self._lock:
                self._process = None
                self._active_run_id = None
                self._cancelled.discard(run_id)


def publish_local_model(conn: Any, run_id: str) -> Dict[str, str]:
    """把成功模型设为标注工作台当前测试模型；不触碰 NAS 或 MacBook。"""
    run = db.get_training_run(conn, run_id)
    if run is None:
        raise KeyError(f'训练记录不存在: {run_id}')
    if run['status'] != 'succeeded':
        raise ValueError('只有训练成功的模型才能发布到本机测试区')
    source = Path(run['artifact_path'])
    if not source.is_file():
        raise FileNotFoundError(source)
    definition = TRAINING_TASKS[run['task_id']]
    config.MODELS_DIR.mkdir(parents=True, exist_ok=True)
    destination = config.MODELS_DIR / definition['publish_name']
    if destination.is_file():
        backup_dir = config.WORK_DIR / 'model-backups'
        backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime('%Y%m%d-%H%M%S-%f')
        shutil.copy2(
            destination, backup_dir / f'{destination.stem}-{stamp}{destination.suffix}'
        )
        current_metadata = destination.with_suffix('.json')
        if current_metadata.is_file():
            shutil.copy2(
                current_metadata, backup_dir / f'{destination.stem}-{stamp}.json'
            )
    shutil.copy2(source, destination)
    metadata_source = source.with_suffix('.json')
    if metadata_source.is_file():
        shutil.copy2(metadata_source, destination.with_suffix('.json'))
    db.update_training_run(conn, run_id, published_path=str(destination))
    return {'run_id': run_id, 'path': str(destination)}
