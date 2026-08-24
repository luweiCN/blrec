"""模型推理服务:加载 ONNX 模型,对帧做检测/分类推理,输出原始与格式化结果。

模型注册表约定(models/*.onnx 文件名前缀):
- result-detector-*  检测(结算面板),imgsz 640
- stage-cls-*        阶段分类(6 类),imgsz 224
- mode-cls-*         模式分类(3 类),imgsz 224
其他文件按通用条目列出(无任务信息)。
"""

from __future__ import annotations

import os
import platform
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np
from PIL import Image

from . import classification_preprocessing
from .config import MODELS_DIR

# 类别名(按 ultralytics 训练的字母序:与 ONNX 输出索引一致)
STAGE_CLASSES = [
    'in_match',
    'not_vainglory',
    'out_of_match',
    'post_match',
    'pre_match',
    'transition',
]
MODE_CLASSES = ['3v3', '5v5', 'aram']

STAGE_LABELS = {
    'gameplay': '对局中',
    'scoreboard': '积分板',
    'result_page': '结算页',
    'victory_defeat': '胜负动画',
    'pre_match': '赛前(排队/选英雄)',
    'out_of_match': '游戏外(大厅等)',
    'transition': '转场(切APP/黑屏/重连)',
    'talent_select': '天赋选择(必大乱斗)',
    'in_match': '对局中',
    'not_vainglory': '非虚荣画面',
    'post_match': '赛后(结算/胜负动画)',
}
MODE_LABELS = {'3v3': '3v3', 'aram': '大乱斗', '5v5': '5v5'}
RESULT_MODE_LABELS = {
    '3v3': '3V3',
    'aram': '大乱斗',
    '5v5': '5V5',
    'blitz': '闪电战',
}
CONTENT_LABELS = {'vainglory': '虚荣', 'not_vainglory': '非虚荣'}
BP_CLASSES = ['bp_3v3', 'bp_5v5', 'bp_aram', 'not_bp']
BP_LABELS = {
    'bp_3v3': '3V3 BP',
    'bp_aram': '大乱斗 BP',
    'bp_5v5': '5V5 BP',
    'not_bp': '非 BP',
}
KEY_SCREEN_CLASSES = ['other', 'result_page', 'scoreboard']
KEY_SCREEN_LABELS = {
    'other': '其他画面',
    'result_page': '赛后结算页',
    'scoreboard': '对局中计分板',
}
MATCH_FLOW_LABELS = {'match_flow': '对局流程中', 'not_match_flow': '非对局画面'}
HERO_SELECT_LABELS = {
    'not_select': '不是英雄选择',
    'select_3v3': '3V3 英雄选择',
    'select_aram': '大乱斗英雄选择',
    'select_5v5': '5V5 英雄选择',
}
PLAYER_POSITION_CLASSES = [
    'left1',
    'left2',
    'left3',
    'left4',
    'left5',
    'right1',
    'right2',
    'right3',
]
PLAYER_POSITION_LABELS = {
    label: '{}队第 {} 位'.format('左' if label.startswith('left') else '右', label[-1])
    for label in PLAYER_POSITION_CLASSES
}
AFK_STATUS_LABELS = {'active': '正常', 'afk': '挂机'}

# 内置注册表:文件名关键字 → 任务与类别(文件名以 -cls- 或 -detector- 区分)
TASK_HINTS = [
    ('afk-status-classifier', 'classify', ['active', 'afk'], AFK_STATUS_LABELS, 224),
    (
        'result-mode-classifier',
        'classify',
        ['3v3', '5v5', 'aram', 'blitz'],
        RESULT_MODE_LABELS,
        512,
    ),
    ('hero-avatar-detector', 'detect', [], {'hero_avatar': '英雄头像'}, 960),
    (
        'player-position-classifier',
        'classify',
        PLAYER_POSITION_CLASSES,
        PLAYER_POSITION_LABELS,
        512,
    ),
    ('mode-gate-detector', 'detect', [], {'mode_gate': '黄色光栅'}, 640),
    ('result-detector', 'detect', STAGE_CLASSES, {'result_panel': '结算面板'}, 640),
    ('result-panel', 'detect', STAGE_CLASSES, {'result_panel': '结算面板'}, 640),
    ('stage-cls', 'classify', STAGE_CLASSES, STAGE_LABELS, 224),
    ('mode-cls', 'classify', MODE_CLASSES, MODE_LABELS, 224),
    ('bp-classifier', 'classify', BP_CLASSES, BP_LABELS, 224),
    ('key-screen-classifier', 'classify', KEY_SCREEN_CLASSES, KEY_SCREEN_LABELS, 224),
    (
        'multi-v2',
        'multi',
        ['content', 'stage', 'mode'],
        {'content': CONTENT_LABELS, 'stage': STAGE_LABELS, 'mode': MODE_LABELS},
        224,
    ),
]

_sessions: Dict[str, Any] = {}  # name -> onnxruntime.InferenceSession
_sessions_lock = threading.RLock()
_torch_models: Dict[str, Any] = {}  # name -> (nn.Module, device)

MULTI_CLASSES = {
    'content': ['vainglory', 'not_vainglory'],
    # 顺序必须与 train_multi.py 的 STAGE_CLS 一致
    'stage': [
        'gameplay',
        'scoreboard',
        'result_page',
        'victory_defeat',
        'pre_match',
        'out_of_match',
        'transition',
        'talent_select',
    ],
    'mode': ['3v3', 'aram', '5v5'],
}

# 对局中/胜负动画的同地图界面:mode 预测 3v3 时实际可能是大乱斗(待确认)
MODE_AMBIGUOUS_STAGES = {'gameplay', 'victory_defeat'}


def _load_session(name: str) -> Any:
    path = MODELS_DIR / f'{name}.onnx'
    return _load_session_path(path)


def _preferred_execution_providers(
    available: Sequence[str],
    *,
    preference: str = 'auto',
    system_name: Optional[str] = None,
) -> Tuple[str, ...]:
    normalized = str(preference or 'auto').strip().lower()
    if normalized not in {'auto', 'coreml', 'cpu'}:
        raise ValueError('VISION_LAB_EXECUTION_PROVIDER 只能是 auto、coreml 或 cpu')
    available_set = set(available)
    wants_coreml = normalized == 'coreml' or (
        normalized == 'auto' and (system_name or platform.system()) == 'Darwin'
    )
    if wants_coreml and 'CoreMLExecutionProvider' in available_set:
        providers = ['CoreMLExecutionProvider']
        if 'CPUExecutionProvider' in available_set:
            providers.append('CPUExecutionProvider')
        return tuple(providers)
    if 'CPUExecutionProvider' not in available_set:
        raise RuntimeError('ONNX Runtime 缺少 CPUExecutionProvider')
    return ('CPUExecutionProvider',)


def _load_session_path(path: Path) -> Any:
    cache_key = str(path.resolve())
    import onnxruntime as ort

    with _sessions_lock:
        if cache_key in _sessions:
            return _sessions[cache_key]
        if not path.exists():
            raise FileNotFoundError(f'模型不存在: {path}')
        providers = _preferred_execution_providers(
            ort.get_available_providers(),
            preference=os.environ.get('VISION_LAB_EXECUTION_PROVIDER', 'auto'),
        )
        so = ort.SessionOptions()
        so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        so.intra_op_num_threads = 1 if providers[0] == 'CoreMLExecutionProvider' else 4
        _sessions[cache_key] = ort.InferenceSession(
            str(path), so, providers=list(providers)
        )
        return _sessions[cache_key]


def clear_model_cache() -> None:
    """发布新测试模型后让下一次推理重新加载文件。"""
    with _sessions_lock:
        _sessions.clear()
        _torch_models.clear()


def list_models() -> List[Dict[str, Any]]:
    if not MODELS_DIR.exists():
        return []
    out = []
    for p in sorted(list(MODELS_DIR.glob('*.onnx')) + list(MODELS_DIR.glob('*.pt'))):
        name = p.stem
        task, classes, labels, imgsz = 'unknown', [], {}, 640
        for kw, t, cls, lb, sz in TASK_HINTS:
            if name.startswith(kw):
                task, classes, labels, imgsz = t, cls, lb, sz
                break
        out.append(
            {
                'name': name,
                'file': p.name,
                'size_mb': round(p.stat().st_size / 1e6, 1),
                'task': task,
                'imgsz': imgsz,
                'classes': classes,
                'labels': labels,
            }
        )
    return out


def _letterbox(img: Image.Image, size: int) -> np.ndarray:
    """等比缩放 + 灰边填充到 size×size,返回 CHW float32 [0,1] 及缩放信息。"""
    w, h = img.size
    scale = min(size / w, size / h)
    nw, nh = int(round(w * scale)), int(round(h * scale))
    img2 = img.resize((nw, nh), Image.BILINEAR)
    canvas = Image.new('RGB', (size, size), (114, 114, 114))
    canvas.paste(img2, ((size - nw) // 2, (size - nh) // 2))
    arr = np.asarray(canvas, dtype=np.float32) / 255.0
    return arr.transpose(2, 0, 1)[None], scale, (size - nw) // 2, (size - nh) // 2


def _softmax(x: np.ndarray) -> np.ndarray:
    e = np.exp(x - x.max())
    return e / e.sum()


def _finalize_probs(logits: np.ndarray) -> np.ndarray:
    """分类输出归一化:已是概率分布(和≈1)则直接用,否则做 softmax。

    ultralytics 导出的 ONNX 分类模型自带 softmax;对已归一化的输出
    再 softmax 会把分布拉平,必须检测。
    """
    if abs(float(logits.sum()) - 1.0) < 0.01:
        return logits
    return _softmax(logits)


def _parse_detect(
    outputs: np.ndarray,
    conf_thr: float = 0.25,
    imgsz: int = 640,
    orig_size=(0, 0),
    class_name: str = 'result_panel',
    class_label: str = '结算面板',
):
    """解析 YOLOv8 检测输出 [1,4+nc,8400] → 归一化框列表。

    outputs: [1, 5, 8400] = [cx, cy, w, h, conf](单类)。
    返回 dets 列表(xywh_norm + xyxy_px + conf),供测试与推理共用。
    """
    pred = outputs[0]  # (5, 8400)
    cx, cy, bw, bh, confs = pred[0], pred[1], pred[2], pred[3], pred[4]
    boxes = np.stack([cx - bw / 2, cy - bh / 2, cx + bw / 2, cy + bh / 2], axis=1)
    valid = confs >= conf_thr
    boxes, confs = boxes[valid], confs[valid]
    keep = _nms(boxes, confs)
    w, h = orig_size
    scale = min(imgsz / w, imgsz / h) if w and h else 1.0
    nw, nh = int(w * scale), int(h * scale)
    pad_x, pad_y = (imgsz - nw) // 2, (imgsz - nh) // 2
    dets = []
    for i in keep:
        x1, y1, x2, y2 = boxes[i]
        x1 = (x1 - pad_x) / scale
        y1 = (y1 - pad_y) / scale
        x2 = (x2 - pad_x) / scale
        y2 = (y2 - pad_y) / scale
        x1, x2 = max(0, x1), min(w, x2)
        y1, y2 = max(0, y1), min(h, y2)
        dets.append(
            {
                'class': class_name,
                'label': class_label,
                'conf': round(float(confs[i]), 4),
                'xyxy_px': [round(float(v), 1) for v in (x1, y1, x2, y2)],
                'xywh_norm': [
                    round(float(v), 4)
                    for v in (x1 / w, y1 / h, (x2 - x1) / w, (y2 - y1) / h)
                ],
            }
        )
    return dets


def _nms(boxes: np.ndarray, scores: np.ndarray, iou_thr: float = 0.45) -> List[int]:
    order = scores.argsort()[::-1]
    keep = []
    while order.size > 0:
        i = order[0]
        keep.append(int(i))
        if order.size == 1:
            break
        xx1 = np.maximum(boxes[i, 0], boxes[order[1:], 0])
        yy1 = np.maximum(boxes[i, 1], boxes[order[1:], 1])
        xx2 = np.minimum(boxes[i, 2], boxes[order[1:], 2])
        yy2 = np.minimum(boxes[i, 3], boxes[order[1:], 3])
        inter = np.maximum(0, xx2 - xx1) * np.maximum(0, yy2 - yy1)
        area_i = (boxes[i, 2] - boxes[i, 0]) * (boxes[i, 3] - boxes[i, 1])
        area_o = (boxes[order[1:], 2] - boxes[order[1:], 0]) * (
            boxes[order[1:], 3] - boxes[order[1:], 1]
        )
        iou = inter / (area_i + area_o - inter + 1e-9)
        order = order[1:][iou <= iou_thr]
    return keep


def run_detect(
    name: str, frame_path: Path, conf_thr: float = 0.25, imgsz: int = 640
) -> Dict[str, Any]:
    """检测推理(单类,结算面板)。"""
    sess = _load_session(name)
    is_gate = name.startswith('mode-gate-detector')
    return _run_detect_session(
        sess,
        frame_path,
        conf_thr=conf_thr,
        imgsz=imgsz,
        class_name='mode_gate' if is_gate else 'result_panel',
        class_label='黄色光栅' if is_gate else '结算面板',
    )


def _run_detect_session(
    sess: Any,
    frame_path: Path,
    *,
    conf_thr: float,
    imgsz: int,
    class_name: str,
    class_label: str,
) -> Dict[str, Any]:
    img = Image.open(frame_path).convert('RGB')
    x, _scale, _pad_x, _pad_y = _letterbox(img, imgsz)
    outputs = sess.run(None, {sess.get_inputs()[0].name: x})[0]  # (1,4+nc,8400)
    dets = _parse_detect(
        outputs,
        conf_thr,
        imgsz,
        img.size,
        class_name=class_name,
        class_label=class_label,
    )
    confs_all = outputs[0][4]
    return {
        'task': 'detect',
        'found': len(dets) > 0,
        'detections': dets,
        'raw_shape': list(outputs.shape),
        'raw_top_conf': round(float(confs_all.max()), 4) if len(confs_all) else 0.0,
    }


def run_classify(
    name: str,
    frame_path: Path,
    classes: List[str],
    labels: Dict[str, str],
    imgsz: int = 224,
) -> Dict[str, Any]:
    """分类推理,返回 top-5 概率 + 原始 logits。"""
    sess = _load_session(name)
    return _run_classify_session(
        sess, frame_path, classes=classes, labels=labels, imgsz=imgsz
    )


def _classification_tensor(
    img: Image.Image,
    imgsz: int,
    *,
    input_width: Optional[int] = None,
    input_height: Optional[int] = None,
    resize: str = 'shortest_edge_center_crop',
    pad_value: int = 114,
) -> np.ndarray:
    """按训练产物记录的规则生成分类输入；旧模型继续兼容中心裁剪。"""
    if resize == 'aspect_fit_letterbox':
        return classification_preprocessing.classification_tensor(
            img,
            width=int(input_width or imgsz),
            height=int(input_height or imgsz),
            pad_value=pad_value,
        )
    width, height = img.size
    scale = imgsz / min(width, height)
    resized = img.resize(
        (max(imgsz, round(width * scale)), max(imgsz, round(height * scale))),
        Image.BILINEAR,
    )
    left = max(0, (resized.width - imgsz) // 2)
    top = max(0, (resized.height - imgsz) // 2)
    cropped = resized.crop((left, top, left + imgsz, top + imgsz))
    arr = np.asarray(cropped, dtype=np.float32) / 255.0
    mean = np.asarray([0.485, 0.456, 0.406], dtype=np.float32)
    std = np.asarray([0.229, 0.224, 0.225], dtype=np.float32)
    arr = (arr - mean) / std
    return arr.transpose(2, 0, 1)[None]


def _run_classify_session(
    sess: Any,
    frame_path: Path,
    *,
    classes: List[str],
    labels: Dict[str, str],
    imgsz: int,
    input_config: Optional[Dict[str, Any]] = None,
    preprocessing: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    img = Image.open(frame_path).convert('RGB')
    input_values = input_config or {}
    preprocessing_values = preprocessing or {}
    recorded_pad_value = preprocessing_values.get('pad_value')
    x = _classification_tensor(
        img,
        imgsz,
        input_width=input_values.get('width'),
        input_height=input_values.get('height'),
        resize=str(preprocessing_values.get('resize') or 'shortest_edge_center_crop'),
        pad_value=int(114 if recorded_pad_value is None else recorded_pad_value),
    )
    logits = sess.run(None, {sess.get_inputs()[0].name: x})[0][0]
    probs = _finalize_probs(logits)
    order = probs.argsort()[::-1]
    scores = [
        {
            'class': classes[i],
            'label': labels.get(classes[i], classes[i]),
            'prob': round(float(probs[i]), 4),
        }
        for i in order
    ]
    return {
        'task': 'classify',
        'top1': scores[0],
        'top5': scores[:5],
        'scores': scores,
        'raw_logits': [round(float(v), 4) for v in logits.tolist()],
        'raw_probs': [round(float(v), 4) for v in probs.tolist()],
    }


def _classification_classes(metadata: Dict[str, Any]) -> List[str]:
    raw_classes = metadata.get('classes') or {}
    if isinstance(raw_classes, dict):
        return [
            str(value)
            for _key, value in sorted(
                raw_classes.items(), key=lambda item: int(item[0])
            )
        ]
    if isinstance(raw_classes, list):
        return [str(value) for value in raw_classes]
    raise ValueError('训练产物缺少分类标签顺序')


def run_artifact_batch(
    artifact_path: Path, metadata: Dict[str, Any], images: Sequence[Image.Image]
) -> List[Dict[str, Any]]:
    """一次 ``session.run`` 分类同一帧的多个裁剪，避免逐槽推理。"""
    if str(metadata.get('kind') or '') != 'classify':
        raise ValueError('批量推理只支持分类模型')
    if not images:
        return []
    artifact = Path(artifact_path)
    session = _load_session_path(artifact)
    classes = _classification_classes(metadata)
    task_id = str(metadata.get('task_id') or '')
    labels = {'afk_status': AFK_STATUS_LABELS}.get(task_id, {})
    imgsz = int(metadata.get('imgsz') or 224)
    input_values = metadata.get('input') or {}
    preprocessing_values = metadata.get('preprocessing') or {}
    recorded_pad_value = preprocessing_values.get('pad_value')
    tensors = [
        _classification_tensor(
            image.convert('RGB'),
            imgsz,
            input_width=input_values.get('width'),
            input_height=input_values.get('height'),
            resize=str(
                preprocessing_values.get('resize') or 'shortest_edge_center_crop'
            ),
            pad_value=int(114 if recorded_pad_value is None else recorded_pad_value),
        )
        for image in images
    ]
    input_info = session.get_inputs()[0]
    input_shape = getattr(input_info, 'shape', None)
    fixed_single_batch = bool(
        input_shape and isinstance(input_shape[0], int) and int(input_shape[0]) == 1
    )
    if fixed_single_batch and len(tensors) > 1:
        raw_batch = np.concatenate(
            [session.run(None, {input_info.name: tensor})[0] for tensor in tensors],
            axis=0,
        )
    else:
        batch = np.concatenate(tensors, axis=0)
        raw_batch = session.run(None, {input_info.name: batch})[0]
    if len(raw_batch) != len(images):
        raise RuntimeError('批量分类模型返回数量与输入裁剪不一致')
    results = []
    for raw_logits in raw_batch:
        logits = np.asarray(raw_logits, dtype=np.float32)
        probs = _finalize_probs(logits)
        order = probs.argsort()[::-1]
        scores = [
            {
                'class': classes[index],
                'label': labels.get(classes[index], classes[index]),
                'prob': round(float(probs[index]), 4),
            }
            for index in order
        ]
        results.append(
            {
                'model': artifact.name,
                'task': 'classify',
                'top1': scores[0],
                'top5': scores[:5],
                'scores': scores,
                'raw_logits': [round(float(value), 4) for value in logits.tolist()],
                'raw_probs': [round(float(value), 4) for value in probs.tolist()],
            }
        )
    return results


def run_artifact(
    artifact_path: Path,
    metadata: Dict[str, Any],
    frame_path: Path,
    conf_thr: float = 0.25,
) -> Dict[str, Any]:
    """直接测试某次训练 run 的 ONNX，不先覆盖本机 current 模型。"""
    artifact = Path(artifact_path)
    session = _load_session_path(artifact)
    kind = str(metadata.get('kind') or '')
    task_id = str(metadata.get('task_id') or '')
    imgsz = int(metadata.get('imgsz') or (640 if kind == 'detect' else 224))
    if kind == 'classify':
        classes = _classification_classes(metadata)
        label_maps = {
            'match_flow': MATCH_FLOW_LABELS,
            'hero_select': HERO_SELECT_LABELS,
            'match_mode': MODE_LABELS,
            'result_mode': RESULT_MODE_LABELS,
            'screen_state': STAGE_LABELS,
            'bp_review': BP_LABELS,
            'key_screen_review': KEY_SCREEN_LABELS,
            'player_position': PLAYER_POSITION_LABELS,
            'afk_status': AFK_STATUS_LABELS,
        }
        return {
            'model': artifact.name,
            **_run_classify_session(
                session,
                frame_path,
                classes=classes,
                labels=label_maps.get(task_id, {}),
                imgsz=imgsz,
                input_config=metadata.get('input'),
                preprocessing=metadata.get('preprocessing'),
            ),
        }
    if kind == 'detect':
        is_gate = task_id == 'mode_gate'
        is_hero_avatar = task_id == 'hero_avatar_detector'
        class_name = (
            'mode_gate'
            if is_gate
            else 'hero_avatar' if is_hero_avatar else 'result_panel'
        )
        class_label = (
            '黄色光栅' if is_gate else '英雄头像' if is_hero_avatar else '结算面板'
        )
        return {
            'model': artifact.name,
            **_run_detect_session(
                session,
                frame_path,
                conf_thr=conf_thr,
                imgsz=imgsz,
                class_name=class_name,
                class_label=class_label,
            ),
        }
    raise ValueError(f'未知训练产物类型: {kind}')


def _load_torch_model(name: str):
    """加载 multi 任务模型(torch .pt),返回 (model, device)。"""
    if name in _torch_models:
        return _torch_models[name]
    import torch

    path = MODELS_DIR / f'{name}.pt'
    if not path.exists():
        raise FileNotFoundError(f'模型不存在: {path}')
    device = 'mps' if torch.backends.mps.is_available() else 'cpu'
    # 与 train_multi.py 的 MultiHeadModel 结构一致
    import torch.nn as nn
    import torchvision.models

    # 与 train_multi.py 的 MultiHeadModel 结构一致
    backbone = torchvision.models.resnet18(weights=None)
    feat = backbone.fc.in_features
    backbone.fc = nn.Identity()

    class _MultiHead(nn.Module):
        def __init__(self):
            super().__init__()
            self.backbone = backbone
            self.content_head = nn.Linear(feat, len(MULTI_CLASSES['content']))
            self.stage_head = nn.Linear(feat, len(MULTI_CLASSES['stage']))
            self.mode_head = nn.Linear(feat, len(MULTI_CLASSES['mode']))

        def forward(self, x):
            f = self.backbone(x)
            return (self.content_head(f), self.stage_head(f), self.mode_head(f))

    model = _MultiHead()
    model.load_state_dict(torch.load(path, map_location=device))
    model.eval().to(device)
    _torch_models[name] = (model, device)
    return model, device


def run_multi(name: str, frame_path: Path, imgsz: int = 224) -> Dict[str, Any]:
    """多输出头推理:content(虚荣/非虚荣) + stage(6 类) + mode(3 类)。"""
    import torch
    from torchvision import transforms

    model, device = _load_torch_model(name)
    tf = transforms.Compose(
        [
            transforms.Resize((imgsz, imgsz)),
            transforms.ToTensor(),
            transforms.Normalize([0.485, 0.456, 0.406], [0.229, 0.224, 0.225]),
        ]
    )
    from PIL import Image

    img = Image.open(frame_path).convert('RGB')
    x = tf(img)[None].to(device)
    with torch.no_grad():
        out_c, out_s, out_m = model(x)
    heads = {
        'content': (out_c, MULTI_CLASSES['content'], CONTENT_LABELS),
        'stage': (out_s, MULTI_CLASSES['stage'], STAGE_LABELS),
        'mode': (out_m, MULTI_CLASSES['mode'], MODE_LABELS),
    }
    result: Dict[str, Any] = {'task': 'multi'}
    for hname, (logits, classes, labels) in heads.items():
        probs = _finalize_probs(logits[0].cpu().numpy())
        order = probs.argsort()[::-1][:5]
        top = [
            {
                'class': classes[i],
                'label': labels.get(classes[i], classes[i]),
                'prob': round(float(probs[i]), 4),
            }
            for i in order
        ]
        result[hname] = {
            'top1': top[0],
            'top5': top,
            'raw_probs': [round(float(v), 4) for v in probs.tolist()],
        }
    # 分层模式输出:对局中/胜负动画(同地图)判 3v3 时,实际可能是大乱斗
    st = result['stage']['top1']['class']
    if st in MODE_AMBIGUOUS_STAGES and result['mode']['top1']['class'] == '3v3':
        result['mode']['ambiguous'] = True
        result['mode']['note'] = (
            '对局中画面:可能是 3v3 或大乱斗(同地图),' '需用积分板/结算页/天赋选择确认'
        )
    # 确定性规则:识别出天赋选择界面 → 模式必为大乱斗(覆盖 mode 头)
    if st == 'talent_select':
        result['mode'] = {
            'top1': {'class': 'aram', 'label': '大乱斗', 'prob': 1.0},
            'top5': [{'class': 'aram', 'label': '大乱斗', 'prob': 1.0}],
            'raw_probs': [],
            'rule_override': True,
            'note': '天赋选择界面是大乱斗特有(规则锁定)',
        }
    return result


def run_model(name: str, frame_path: Path, conf_thr: float = 0.25) -> Dict[str, Any]:
    """按模型注册表自动选择任务并推理。"""
    meta = next((m for m in list_models() if m['name'] == name), None)
    if meta is None:
        raise FileNotFoundError(f'未知模型: {name}')
    if meta['task'] == 'detect':
        return {'model': name, **run_detect(name, frame_path, conf_thr, meta['imgsz'])}
    if meta['task'] == 'classify':
        return {
            'model': name,
            **run_classify(
                name, frame_path, meta['classes'], meta['labels'], meta['imgsz']
            ),
        }
    if meta['task'] == 'multi':
        return {'model': name, **run_multi(name, frame_path, meta['imgsz'])}
    raise RuntimeError(f'模型 {name} 任务类型未知,无法推理')
