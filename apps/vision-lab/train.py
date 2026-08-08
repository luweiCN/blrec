"""虚荣画面分类模型训练脚本。

用法(在 apps/vision-lab 下):
    .venv/bin/pip install -r requirements-train.txt
    .venv/bin/python train.py --epochs 60 --imgsz 224

流程:
1. 从 data/labels.db 读取已标注帧
2. 按画面类型(frame_kind)组织为 ImageFolder 数据集(自动按类别比例划分 train/val)
3. 用 ultralytics YOLOv8n-cls 训练(本机自动选择 MPS/CPU)
4. 导出 ONNX 到 data/models/result-screen-cls.onnx

说明:
- 只使用 frame_kind 已标注的帧;game_mode / is_result 字段会写进 labels.json,
  供后续训练多任务模型时复用。
- 类别名与打标工具一致:gameplay / scoreboard / result / main_menu / other。
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path
from typing import Any, Dict, List

from labeler.config import WORK_DIR

DB_PATH = WORK_DIR / 'labels.db'
DATASET_DIR = WORK_DIR / 'dataset'
MODELS_DIR = WORK_DIR / 'models'

KINDS = ['gameplay', 'scoreboard', 'result', 'main_menu', 'shop',
         'out_of_game', 'other']


def load_labeled() -> List[Dict[str, Any]]:
    import sqlite3
    if not DB_PATH.is_file():
        raise SystemExit(f'未找到标注库 {DB_PATH},请先运行打标工具并完成标注')
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        'SELECT f.*, v.streamer FROM frames f JOIN videos v ON v.id = f.video_id '
        'WHERE f.labeled = 1 AND f.frame_kind IS NOT NULL ORDER BY f.id'
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def build_dataset(frames: List[Dict[str, Any]], val_ratio: float) -> Dict[str, Any]:
    """组织成 ultralytics 分类数据集目录结构:
    dataset/train/<kind>/*.jpg   dataset/val/<kind>/*.jpg
    """
    if shutil.os.path.exists(DATASET_DIR):
        shutil.rmtree(DATASET_DIR)
    counts: Dict[str, int] = {}
    import random
    random.seed(42)
    for f in frames:
        kind = f['frame_kind']
        counts[kind] = counts.get(kind, 0) + 1
        src = Path(f['frame_path'])
        if not src.exists():
            print(f'[跳过] 帧文件缺失: {src}')
            continue
        split = 'val' if random.random() < val_ratio else 'train'
        dst = DATASET_DIR / split / kind / f"f{f['id']:07d}.jpg"
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
    return counts


def train(args: argparse.Namespace, counts: Dict[str, int]) -> None:
    from ultralytics import YOLO

    model = YOLO(args.model)
    model.train(
        data=str(DATASET_DIR),
        epochs=args.epochs,
        imgsz=args.imgsz,
        batch=args.batch,
        device=args.device,
        project=str(WORK_DIR / 'runs'),
        name='cls',
        exist_ok=True,
        patience=args.patience,
        verbose=True,
    )
    # 导出 ONNX
    best = Path(WORK_DIR) / 'runs' / 'cls' / 'weights' / 'best.pt'
    if not best.is_file():
        raise SystemExit(f'训练产物缺失: {best}')
    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    exported = model.export(format='onnx', imgsz=args.imgsz)
    target = MODELS_DIR / 'result-screen-cls.onnx'
    if Path(exported) != target:
        shutil.copy2(exported, target)
    print(f'\n[完成] ONNX 模型已导出: {target}')
    print(f'[完成] 类别顺序: {model.names}')


def main() -> None:
    parser = argparse.ArgumentParser(description='虚荣画面分类模型训练')
    parser.add_argument('--epochs', type=int, default=60)
    parser.add_argument('--imgsz', type=int, default=224, help='训练输入尺寸')
    parser.add_argument('--batch', type=int, default=0, help='0=自动')
    parser.add_argument('--device', default='', help='留空自动选择(MPS/CPU)')
    parser.add_argument('--val-ratio', type=float, default=0.2)
    parser.add_argument('--patience', type=int, default=15)
    parser.add_argument(
        '--model',
        default=str(MODELS_DIR / 'base' / 'yolov8n-cls.pt'),
        help='预训练模型（默认读取外置工作目录）',
    )
    args = parser.parse_args()

    frames = load_labeled()
    if not frames:
        raise SystemExit('没有已标注且带 frame_kind 的帧,请先打标')
    counts = build_dataset(frames, args.val_ratio)
    print('数据集分布:')
    for kind in KINDS:
        print(f'  {kind:12s} {counts.get(kind, 0):5d}')
    train(args, counts)


if __name__ == '__main__':
    main()
