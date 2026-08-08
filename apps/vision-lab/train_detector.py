"""训练第一版结算页检测器(YOLOv8n detection)。

流程:导出 result-detector-v1 数据集(防泄漏 8:1:1)→ ultralytics 训练 →
导出 ONNX 到 models/result-detector-v1.onnx。
用法: .venv/bin/python train_detector.py [--epochs 150] [--imgsz 640] [--max-neg 2000]
"""
import argparse
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import labeler.db as db
import labeler.export as export_mod
from labeler.config import DB_PATH, EXPORT_DIR, MODELS_DIR

MAX_NEG_DEFAULT = 2000  # 检测训练负样本上限(积分板优先),6800 全量太慢


def main() -> None:
    parser = argparse.ArgumentParser(description='结算页检测器训练')
    parser.add_argument('--epochs', type=int, default=150)
    parser.add_argument('--imgsz', type=int, default=640)
    parser.add_argument('--max-neg', type=int, default=MAX_NEG_DEFAULT)
    parser.add_argument('--device', default='', help='留空自动(MPS/CPU)')
    parser.add_argument('--version', default='result-detector-v1')
    parser.add_argument(
        '--model',
        default=str(MODELS_DIR / 'base' / 'yolov8n.pt'),
        help='预训练模型（默认读取外置工作目录）',
    )
    args = parser.parse_args()

    # 1) 导出数据集(不可变版本,防泄漏切分;已存在则复用)
    conn = db.connect(DB_PATH)
    try:
        if (EXPORT_DIR / args.version).exists():
            print(f'[导出] 复用已有数据集 {args.version}')
            result = {'dir': str(EXPORT_DIR / args.version)}
        else:
            result = export_mod.export_result_detector(
                conn, include_negatives=True, max_negatives=args.max_neg,
                version=args.version)
    finally:
        conn.close()
    print(f'\n[数据集] {args.version} -> {result["dir"]}')

    # 2) 训练
    from ultralytics import YOLO
    yaml_path = Path(result['dir']) / 'data.yaml'
    model = YOLO(args.model)
    model.train(
        data=str(yaml_path),
        epochs=args.epochs,
        imgsz=args.imgsz,
        batch=-1,
        device=args.device,
        project=str(Path(result['dir']).parent / 'runs'),
        name='det',
        exist_ok=True,
        patience=30,
        verbose=True,
    )

    # 3) 导出 ONNX
    best = Path(result['dir']).parent / 'runs' / 'det' / 'weights' / 'best.pt'
    if not best.is_file():
        raise SystemExit(f'训练产物缺失: {best}')
    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    exported = model.export(format='onnx', imgsz=args.imgsz)
    target = MODELS_DIR / f'{args.version}.onnx'
    if Path(exported) != target:
        shutil.copy2(exported, target)
    print(f'\n[完成] ONNX 模型: {target}')
    print(f'[完成] 测试集评估见 ultralytics runs/det 输出')


if __name__ == '__main__':
    main()
