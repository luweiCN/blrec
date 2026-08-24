"""Ultralytics 子进程入口；用 JSON 标记把 epoch 进度回传给工作台。"""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path
from typing import Any, Dict

from . import classification_preprocessing
from .training import PROGRESS_PREFIX, RESULT_PREFIX


def _numbers(values: Any) -> Dict[str, float]:
    if not isinstance(values, dict):
        return {}
    result = {}
    for key, value in values.items():
        try:
            result[str(key)] = float(value)
        except (TypeError, ValueError):
            continue
    return result


def _artifact_input_metadata(
    kind: str, *, input_width: int, input_height: int
) -> Dict[str, Any]:
    if kind == 'classify':
        return classification_preprocessing.preprocessing_metadata(
            width=input_width, height=input_height
        )
    return {
        'preprocessing': {
            'color': 'RGB',
            'resize': 'letterbox',
            'pad_value': 114,
            'scale': '0_to_1',
            'normalize': 'none',
        }
    }


def _onnx_export_options(task_id: str, export_size: Any) -> Dict[str, Any]:
    options: Dict[str, Any] = {'format': 'onnx', 'imgsz': export_size}
    if task_id == 'afk_status':
        options['dynamic'] = True
    return options


def main() -> None:
    parser = argparse.ArgumentParser(description='虚荣视觉模型训练子进程')
    parser.add_argument('--task-id', required=True)
    parser.add_argument('--kind', choices=('classify', 'detect'), required=True)
    parser.add_argument('--dataset-dir', type=Path, required=True)
    parser.add_argument('--run-dir', type=Path, required=True)
    parser.add_argument('--artifact', type=Path, required=True)
    parser.add_argument('--base-model', type=Path, required=True)
    parser.add_argument('--epochs', type=int, required=True)
    parser.add_argument('--imgsz', type=int, required=True)
    parser.add_argument('--input-width', type=int, default=0)
    parser.add_argument('--input-height', type=int, default=0)
    parser.add_argument('--device', default='')
    parser.add_argument('--resume-checkpoint', type=Path)
    args = parser.parse_args()

    if not args.base_model.is_file():
        raise FileNotFoundError(args.base_model)
    if args.resume_checkpoint is not None and not args.resume_checkpoint.is_file():
        raise FileNotFoundError(args.resume_checkpoint)
    if args.epochs <= 0 or args.imgsz <= 0:
        raise ValueError('epochs 和 imgsz 必须为正数')
    if args.kind == 'classify' and (args.input_width <= 0 or args.input_height <= 0):
        raise ValueError('分类模型必须提供有效的 input-width 和 input-height')
    if args.kind == 'detect':
        data_path = args.dataset_dir / 'data.yaml'
    else:
        data_path = args.dataset_dir / 'images'
    if not data_path.exists():
        raise FileNotFoundError(data_path)

    device = args.device
    if not device:
        import torch

        device = 'mps' if torch.backends.mps.is_available() else 'cpu'

    from ultralytics import YOLO

    model_path = args.resume_checkpoint or args.base_model
    model = YOLO(str(model_path))

    def report_epoch(trainer: Any) -> None:
        epoch = int(trainer.epoch) + 1
        epochs = max(1, int(trainer.epochs))
        payload = {
            'epoch': epoch,
            'epochs': epochs,
            'progress': min(1.0, epoch / epochs),
            'metrics': _numbers(getattr(trainer, 'metrics', {})),
        }
        print(PROGRESS_PREFIX + json.dumps(payload, ensure_ascii=False), flush=True)

    model.add_callback('on_train_epoch_end', report_epoch)
    trainer_class = None
    if args.kind == 'classify':
        from .classification_training import FullFrameClassificationTrainer

        FullFrameClassificationTrainer.input_width = args.input_width
        FullFrameClassificationTrainer.input_height = args.input_height
        trainer_class = FullFrameClassificationTrainer
    train_options = {
        'trainer': trainer_class,
        'data': str(data_path),
        'epochs': args.epochs,
        'imgsz': args.imgsz,
        'batch': -1,
        'device': device,
        'project': str(args.run_dir),
        'name': 'ultralytics',
        'exist_ok': False,
        'patience': max(10, min(30, args.epochs // 3)),
        'verbose': True,
    }
    if args.resume_checkpoint is not None:
        train_options['resume'] = str(args.resume_checkpoint)
    train_result = model.train(**train_options)
    trainer = model.trainer
    best_path = Path(str(trainer.best))
    if not best_path.is_file():
        raise FileNotFoundError(f'最佳权重不存在: {best_path}')
    best_model = YOLO(str(best_path))
    export_size = (
        [args.input_height, args.input_width] if args.kind == 'classify' else args.imgsz
    )
    exported = Path(
        str(best_model.export(**_onnx_export_options(args.task_id, export_size)))
    )
    args.artifact.parent.mkdir(parents=True, exist_ok=True)
    if exported.resolve() != args.artifact.resolve():
        shutil.copy2(exported, args.artifact)
    names = {
        str(index): str(name)
        for index, name in getattr(best_model, 'names', {}).items()
    }
    metrics = _numbers(getattr(train_result, 'results_dict', {}))
    input_metadata = _artifact_input_metadata(
        args.kind, input_width=args.input_width, input_height=args.input_height
    )
    metadata = {
        'task_id': args.task_id,
        'kind': args.kind,
        'imgsz': args.imgsz,
        'epochs': args.epochs,
        'device': device,
        'classes': names,
        'metrics': metrics,
        **input_metadata,
    }
    args.artifact.with_suffix('.json').write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2), encoding='utf-8'
    )
    print(
        RESULT_PREFIX
        + json.dumps(
            {'artifact': str(args.artifact), 'metrics': metrics}, ensure_ascii=False
        ),
        flush=True,
    )


if __name__ == '__main__':
    main()
