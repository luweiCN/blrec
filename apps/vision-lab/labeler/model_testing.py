"""训练产物验收与不可变 Analysis Worker 模型包。"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence
from uuid import uuid4

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
}
REQUIRED_TASKS = ('match_flow', 'hero_select', 'match_mode', 'result_detector')


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
        or split not in {'train', 'val', 'test'}
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


def run_sample_image_path(
    conn: Any, run_id: str, *, sample_id: str, split: str
) -> Path:
    context = _run_context(conn, run_id)
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
    return _sample_image_path(context, sample)


def list_run_samples(
    conn: Any, run_id: str, *, split: str = 'test', limit: int = 500
) -> Dict[str, Any]:
    if split not in {'train', 'val', 'test'}:
        raise ValueError('split 必须是 train、val 或 test')
    context = _run_context(conn, run_id)
    manifest = Path(context['dataset']['manifest_path'])
    all_samples = _manifest_samples(manifest)
    selected = [sample for sample in all_samples if sample.get('split') == split]
    output = []
    for sample in selected[: max(1, min(2_000, int(limit)))]:
        sample_id = str(sample.get('sample_id') or '')
        frame_id: Optional[int] = None
        if sample_id.startswith('f') and sample_id[1:].isdigit():
            frame_id = int(sample_id[1:])
        if frame_id is None and sample.get('sha256'):
            row = conn.execute(
                'SELECT id FROM frames WHERE sha256 = ?', (str(sample['sha256']),)
            ).fetchone()
            frame_id = int(row['id']) if row else None
        expected: Any
        if context['metadata'].get('kind') == 'detect':
            role = TASK_ROLES.get(context['run']['task_id'])
            box_key = 'mode_gate_boxes' if role == 'mode_gate' else 'result_panel'
            expected = {
                'found': bool(
                    sample.get(box_key)
                    if role == 'mode_gate'
                    else (sample.get('boxes') or {}).get(box_key)
                ),
                'label': sample.get('label') or sample.get('detector_label'),
            }
        else:
            expected = sample.get('label')
        try:
            _sample_image_path(context, sample)
            has_snapshot_image = True
        except (FileNotFoundError, ValueError):
            has_snapshot_image = False
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
            }
        )
    return {'run_id': run_id, 'split': split, 'total': len(selected), 'items': output}


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


def _evaluation_gaps(context: Dict[str, Any]) -> List[str]:
    """检查固定测试集是否足以支持“可部署”结论。"""
    samples = [
        sample
        for sample in _manifest_samples(Path(context['dataset']['manifest_path']))
        if sample.get('split') == 'test'
    ]
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
        positive_label = 'blocked_gate' if task_id == 'mode_gate' else 'result_panel'
        labels = {
            str(sample.get('label') or sample.get('detector_label') or '')
            for sample in samples
        }
        gaps = []
        if positive_label not in labels:
            gaps.append('固定测试集没有带框正样本')
        if not any(label and label != positive_label for label in labels):
            gaps.append('固定测试集没有无框负样本')
        return gaps
    return [f'未知训练产物类型 {kind}']


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
            manifest_models[role] = {
                'file': f'models/{target.name}',
                'sha256': _sha256(target),
                'kind': 'classification' if kind == 'classify' else 'detection',
                'input': {
                    'width': int(metadata.get('imgsz') or 224),
                    'height': int(metadata.get('imgsz') or 224),
                    'color': 'RGB',
                    'resize': (
                        'shortest_edge_center_crop'
                        if kind == 'classify'
                        else 'letterbox'
                    ),
                    'pad_value': None if kind == 'classify' else 114,
                    'scale': '0_to_1',
                    'normalize': ('imagenet' if kind == 'classify' else 'none'),
                },
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
            'schema_version': 1,
            'package_id': package_id,
            'pipeline_version': 'timeline-v1',
            'status': status,
            'missing_roles': [TASK_ROLES[task] for task in missing],
            'evaluation_gaps': evaluation_gaps,
            'models': manifest_models,
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
