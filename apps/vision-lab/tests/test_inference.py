"""inference.py 单元测试:类别顺序、概率归一化、检测输出解析。"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import numpy as np

from labeler import inference


class TestClasses(unittest.TestCase):
    def test_stage_classes_are_alpha_sorted(self):
        self.assertEqual(inference.STAGE_CLASSES,
                         sorted(inference.STAGE_CLASSES))

    def test_mode_classes_are_alpha_sorted(self):
        self.assertEqual(inference.MODE_CLASSES,
                         sorted(inference.MODE_CLASSES))

    def test_all_classes_have_labels(self):
        for c in inference.STAGE_CLASSES:
            self.assertIn(c, inference.STAGE_LABELS)
        for c in inference.MODE_CLASSES:
            self.assertIn(c, inference.MODE_LABELS)


class TestProbNormalization(unittest.TestCase):
    def test_already_normalized_not_softmaxed_again(self):
        # 已归一化分布(模拟 ultralytics ONNX 自带 softmax 的输出)
        logits = np.array([0.002, 0.002, 0.002, 0.9895, 0.002, 0.002])
        probs = inference._finalize_probs(logits)
        self.assertAlmostEqual(float(probs[3]), 0.9895, places=4)
        self.assertAlmostEqual(float(probs.sum()), 1.0, places=3)

    def test_raw_logits_get_softmax(self):
        logits = np.array([2.0, 1.0, 0.0, 0.0, 0.0, 0.0])
        probs = inference._finalize_probs(logits)
        self.assertAlmostEqual(float(probs.sum()), 1.0, places=5)
        self.assertGreater(float(probs[0]), 0.5)

    def test_top1_matches_argmax(self):
        logits = np.array([0.1, 0.2, 0.3, 0.4, 0.0, 0.0])
        probs = inference._finalize_probs(logits)
        self.assertEqual(int(np.argmax(probs)), 3)


class TestDetectParse(unittest.TestCase):
    def test_parse_detection_output(self):
        # 模拟 YOLOv8 检测输出 [1, 5, 8400]:cx, cy, w, h, cls_conf(640 坐标系像素)
        out = np.zeros((1, 5, 8400), dtype=np.float32)
        # 第 100 号候选:中心(320,320) 宽 320 高 320,置信度 0.9
        out[0, 0, 100] = 320.0
        out[0, 1, 100] = 320.0
        out[0, 2, 100] = 320.0
        out[0, 3, 100] = 320.0
        out[0, 4, 100] = 0.9
        dets = inference._parse_detect(out, orig_size=(640, 640))
        self.assertEqual(len(dets), 1)
        d = dets[0]
        self.assertAlmostEqual(d['conf'], 0.9, places=4)
        x1, y1, x2, y2 = d['xyxy_px']
        self.assertAlmostEqual(x1, 160.0, places=3)
        self.assertAlmostEqual(y1, 160.0, places=3)
        self.assertAlmostEqual(x2, 480.0, places=3)
        self.assertAlmostEqual(y2, 480.0, places=3)
        self.assertAlmostEqual(d['xywh_norm'][0], 0.25, places=4)
        self.assertAlmostEqual(d['xywh_norm'][1], 0.25, places=4)
        self.assertAlmostEqual(d['xywh_norm'][2], 0.5, places=4)
        self.assertAlmostEqual(d['xywh_norm'][3], 0.5, places=4)


if __name__ == '__main__':
    unittest.main()
