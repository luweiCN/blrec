from __future__ import annotations

import hashlib
import importlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

from blrec.vainglory.hero_recognition import HeroMatch
from blrec.vainglory.result_detection import OnnxResultPanelDetector
from blrec.vainglory.stage_classifier import (
    CONTENT_VAINGLORY,
    MODE_3V3,
    MODE_5V5,
    MODE_ARAM,
    STAGE_GAMEPLAY,
    STAGE_OUT_OF_MATCH,
    STAGE_PRE_MATCH,
    STAGE_TRANSITION,
    StagePrediction,
)
from blrec.vainglory.vision import (
    HeroAvatarDetection,
    PixelRect,
    RecordedPlayer,
    ResultLayout,
    RgbFrame,
)

REQUIRED_MODEL_ROLES = (
    'match_flow',
    'hero_select',
    'match_mode',
    'result_panel',
    'hero_avatar',
    'hero_identity',
    'player_position',
)
SUPPORTED_SCHEMA_VERSIONS = (1, 2)


@dataclass(frozen=True)
class ModelSpec:
    role: str
    path: Path
    sha256: str
    kind: str
    input_width: int
    input_height: int
    color: str
    resize: str
    pad_value: int
    preserve_full_image: bool
    scale: str
    normalize: str
    classes: Tuple[str, ...]
    dataset_version: str
    training_run_id: str


@dataclass(frozen=True)
class RuntimeConfig:
    coarse_interval_ms: int
    maximum_keyframe_distance_ms: int
    result_scan_fps: int
    thresholds: Mapping[str, float]


@dataclass(frozen=True)
class ModelPackage:
    root: Path
    package_id: str
    pipeline_version: str
    schema_version: int
    models: Mapping[str, ModelSpec]
    runtime: RuntimeConfig

    def model(self, role: str) -> ModelSpec:
        try:
            return self.models[role]
        except KeyError as error:
            raise KeyError('模型包缺少角色: {}'.format(role)) from error


@dataclass(frozen=True)
class ClassificationPrediction:
    label: str
    confidence: float
    scores: Tuple[Tuple[str, float], ...]


def _classification_tensor(frame: RgbFrame, spec: ModelSpec) -> Any:
    cv2: Any = importlib.import_module('cv2')
    numpy: Any = importlib.import_module('numpy')
    image = numpy.frombuffer(frame.pixels, dtype=numpy.uint8).reshape(
        frame.height, frame.width, 3
    )
    if spec.resize == 'aspect_fit_letterbox':
        resize_scale = min(
            spec.input_width / frame.width, spec.input_height / frame.height
        )
        resized_width = max(1, min(spec.input_width, round(frame.width * resize_scale)))
        resized_height = max(
            1, min(spec.input_height, round(frame.height * resize_scale))
        )
        resized = cv2.resize(
            image, (resized_width, resized_height), interpolation=cv2.INTER_LINEAR
        )
        left = (spec.input_width - resized_width) // 2
        top = (spec.input_height - resized_height) // 2
        prepared = numpy.full(
            (spec.input_height, spec.input_width, 3), spec.pad_value, dtype=numpy.uint8
        )
        prepared[top : top + resized_height, left : left + resized_width] = resized
    elif spec.resize == 'shortest_edge_center_crop':
        resize_scale = max(
            spec.input_width / frame.width, spec.input_height / frame.height
        )
        resized_width = max(spec.input_width, round(frame.width * resize_scale))
        resized_height = max(spec.input_height, round(frame.height * resize_scale))
        resized = cv2.resize(
            image, (resized_width, resized_height), interpolation=cv2.INTER_LINEAR
        )
        left = (resized_width - spec.input_width) // 2
        top = (resized_height - spec.input_height) // 2
        prepared = resized[
            top : top + spec.input_height, left : left + spec.input_width
        ]
    else:
        raise ValueError(
            '{} 模型使用了不支持的 resize: {}'.format(spec.role, spec.resize)
        )
    tensor = prepared.astype(numpy.float32)
    if spec.scale == '0_to_1':
        tensor /= 255.0
    elif spec.scale != 'none':
        raise ValueError(
            '{} 模型使用了不支持的 scale: {}'.format(spec.role, spec.scale)
        )
    if spec.normalize == 'imagenet':
        tensor -= numpy.asarray([0.485, 0.456, 0.406], dtype=numpy.float32)
        tensor /= numpy.asarray([0.229, 0.224, 0.225], dtype=numpy.float32)
    elif spec.normalize != 'none':
        raise ValueError(
            '{} 模型使用了不支持的 normalize: {}'.format(spec.role, spec.normalize)
        )
    return numpy.ascontiguousarray(tensor.transpose(2, 0, 1)[None])


class OnnxClassificationModel:
    def __init__(
        self, spec: ModelSpec, *, providers: Optional[Sequence[str]] = None
    ) -> None:
        if spec.kind != 'classification':
            raise ValueError('{} 不是分类模型'.format(spec.role))
        if spec.color != 'RGB':
            raise ValueError('{} 模型只支持 RGB 输入'.format(spec.role))
        onnxruntime: Any = importlib.import_module('onnxruntime')
        options = onnxruntime.SessionOptions()
        options.intra_op_num_threads = 1
        options.inter_op_num_threads = 1
        self._spec = spec
        self._session = onnxruntime.InferenceSession(
            str(spec.path),
            sess_options=options,
            providers=tuple(providers or ('CPUExecutionProvider',)),
        )
        self._input_name = self._session.get_inputs()[0].name

    def predict(self, frame: RgbFrame) -> ClassificationPrediction:
        numpy: Any = importlib.import_module('numpy')
        tensor = _classification_tensor(frame, self._spec)
        output = self._session.run(None, {self._input_name: tensor})[0]
        values = numpy.asarray(output, dtype=numpy.float32).reshape(-1)
        if len(values) != len(self._spec.classes):
            raise RuntimeError(
                '{} 模型输出类别数与 manifest 不一致'.format(self._spec.role)
            )
        if (
            len(values)
            and float(values.min()) >= 0
            and float(values.max()) <= 1
            and abs(float(values.sum()) - 1.0) < 0.01
        ):
            probabilities = values
        else:
            shifted = values - values.max()
            exponentials = numpy.exp(shifted)
            probabilities = exponentials / exponentials.sum()
        scores = tuple(
            sorted(
                (
                    (label, float(probabilities[index]))
                    for index, label in enumerate(self._spec.classes)
                ),
                key=lambda item: item[1],
                reverse=True,
            )
        )
        return ClassificationPrediction(scores[0][0], scores[0][1], scores)


class OnnxHeroAvatarDetector:
    def __init__(
        self,
        spec: ModelSpec,
        *,
        confidence_threshold: float,
        providers: Optional[Sequence[str]] = None,
    ) -> None:
        if spec.kind != 'detection' or spec.resize != 'letterbox':
            raise ValueError('{} 不是受支持的头像检测模型'.format(spec.role))
        if spec.input_width != spec.input_height:
            raise ValueError('{} 当前只支持正方形检测输入'.format(spec.role))
        onnxruntime: Any = importlib.import_module('onnxruntime')
        options = onnxruntime.SessionOptions()
        options.intra_op_num_threads = 1
        options.inter_op_num_threads = 1
        self._spec = spec
        self._threshold = confidence_threshold
        self._session = onnxruntime.InferenceSession(
            str(spec.path),
            sess_options=options,
            providers=tuple(providers or ('CPUExecutionProvider',)),
        )
        self._input_name = self._session.get_inputs()[0].name

    def detect(self, frame: RgbFrame) -> Tuple[HeroAvatarDetection, ...]:
        cv2: Any = importlib.import_module('cv2')
        numpy: Any = importlib.import_module('numpy')
        size = self._spec.input_width
        image = numpy.frombuffer(frame.pixels, dtype=numpy.uint8).reshape(
            frame.height, frame.width, 3
        )
        scale = min(size / frame.width, size / frame.height)
        width = max(1, min(size, round(frame.width * scale)))
        height = max(1, min(size, round(frame.height * scale)))
        resized = cv2.resize(image, (width, height), interpolation=cv2.INTER_LINEAR)
        left = (size - width) // 2
        top = (size - height) // 2
        canvas = numpy.full((size, size, 3), self._spec.pad_value, dtype=numpy.uint8)
        canvas[top : top + height, left : left + width] = resized
        tensor = numpy.ascontiguousarray(
            canvas.transpose(2, 0, 1)[None].astype(numpy.float32) / 255.0
        )
        output = self._session.run(None, {self._input_name: tensor})[0]
        prediction = numpy.squeeze(output)
        if prediction.ndim != 2:
            return ()
        if prediction.shape[0] <= 32 and prediction.shape[1] > prediction.shape[0]:
            prediction = prediction.transpose()
        if prediction.shape[1] < 5:
            return ()
        scores = (
            prediction[:, 4]
            if prediction.shape[1] == 5
            else numpy.max(prediction[:, 4:], axis=1)
        )
        valid_indexes = numpy.where(scores >= self._threshold)[0]
        if not len(valid_indexes):
            return ()
        boxes = numpy.stack(
            (
                prediction[:, 0] - prediction[:, 2] / 2,
                prediction[:, 1] - prediction[:, 3] / 2,
                prediction[:, 0] + prediction[:, 2] / 2,
                prediction[:, 1] + prediction[:, 3] / 2,
            ),
            axis=1,
        )
        selected = _nms_indexes(
            boxes[valid_indexes], scores[valid_indexes], iou_threshold=0.45
        )
        detections = []
        for selected_index in selected:
            index = int(valid_indexes[selected_index])
            x1, y1, x2, y2 = boxes[index]
            source_left = max(0, min(frame.width - 1, round((x1 - left) / scale)))
            source_top = max(0, min(frame.height - 1, round((y1 - top) / scale)))
            source_right = max(
                source_left + 1, min(frame.width, round((x2 - left) / scale))
            )
            source_bottom = max(
                source_top + 1, min(frame.height, round((y2 - top) / scale))
            )
            detections.append(
                HeroAvatarDetection(
                    PixelRect(source_left, source_top, source_right, source_bottom),
                    float(scores[index]),
                )
            )
        return tuple(sorted(detections, key=lambda item: item.confidence, reverse=True))


def _nms_indexes(boxes: Any, scores: Any, *, iou_threshold: float) -> Tuple[int, ...]:
    numpy: Any = importlib.import_module('numpy')
    order = scores.argsort()[::-1]
    kept = []
    while order.size:
        index = int(order[0])
        kept.append(index)
        if order.size == 1:
            break
        remaining = order[1:]
        xx1 = numpy.maximum(boxes[index, 0], boxes[remaining, 0])
        yy1 = numpy.maximum(boxes[index, 1], boxes[remaining, 1])
        xx2 = numpy.minimum(boxes[index, 2], boxes[remaining, 2])
        yy2 = numpy.minimum(boxes[index, 3], boxes[remaining, 3])
        intersection = numpy.maximum(0, xx2 - xx1) * numpy.maximum(0, yy2 - yy1)
        area = (boxes[:, 2] - boxes[:, 0]) * (boxes[:, 3] - boxes[:, 1])
        union = area[index] + area[remaining] - intersection
        order = remaining[intersection / (union + 1e-9) <= iou_threshold]
    return tuple(kept)


class PackageHeroRecognizer:
    def __init__(self, classifier: Any, *, threshold: float) -> None:
        self._classifier = classifier
        self._threshold = threshold

    def recognize(self, frame: RgbFrame) -> Optional[HeroMatch]:
        prediction = self._classifier.predict(frame)
        if prediction.confidence < self._threshold:
            return None
        second = 0.0 if len(prediction.scores) < 2 else prediction.scores[1][1]
        margin = max(1, round((prediction.confidence - second) * 100))
        return HeroMatch(
            label=prediction.label,
            confidence=prediction.confidence,
            inliers=max(5, round(prediction.confidence * 20)),
            margin=margin,
        )


class PackageRecordedPlayerDetector:
    def __init__(self, classifier: Any, *, threshold: float) -> None:
        self._classifier = classifier
        self._threshold = threshold

    def detect(self, frame: RgbFrame, layout: ResultLayout) -> Optional[RecordedPlayer]:
        prediction = self._classifier.predict(frame)
        if prediction.confidence < self._threshold:
            return None
        label = prediction.label
        if not label.startswith(('left', 'right')):
            return None
        try:
            slot = int(label[-1])
        except ValueError:
            return None
        if slot < 1 or slot > layout.team_size:
            return None
        return RecordedPlayer(
            side='left' if label.startswith('left') else 'right',
            slot=slot,
            confidence=prediction.confidence,
        )


def _mode_value(label: str) -> int:
    return {'3v3': MODE_3V3, 'aram': MODE_ARAM, '5v5': MODE_5V5}.get(label, MODE_3V3)


def _selection_mode(label: str) -> str:
    return {'select_3v3': '3v3', 'select_aram': 'aram', 'select_5v5': '5v5'}.get(
        label, ''
    )


class PackageStageClassifier:
    def __init__(
        self,
        *,
        package_id: str,
        match_flow: Any,
        hero_select: Any,
        match_mode: Any,
        thresholds: Mapping[str, float],
    ) -> None:
        self.model_version = package_id
        self._match_flow = match_flow
        self._hero_select = hero_select
        self._match_mode = match_mode
        self._thresholds = thresholds

    def classify(self, frame: RgbFrame) -> StagePrediction:
        flow = self._match_flow.predict(frame)
        selection = self._hero_select.predict(frame)
        selected_mode = _selection_mode(selection.label)
        if selected_mode and selection.confidence >= float(
            self._thresholds.get('hero_select', 0.55)
        ):
            return StagePrediction(
                content=CONTENT_VAINGLORY,
                content_conf=max(flow.confidence, selection.confidence),
                stage=STAGE_PRE_MATCH,
                stage_conf=selection.confidence,
                mode=_mode_value(selected_mode),
                mode_conf=selection.confidence,
                model_version=self.model_version,
                match_flow_label=flow.label,
                match_flow_conf=flow.confidence,
                hero_select_label=selection.label,
                hero_select_conf=selection.confidence,
                match_mode_label=selected_mode,
                match_mode_conf=selection.confidence,
            )
        flow_threshold = float(self._thresholds.get('match_flow', 0.55))
        if flow.confidence < flow_threshold:
            return StagePrediction(
                content=CONTENT_VAINGLORY,
                content_conf=flow.confidence,
                stage=STAGE_TRANSITION,
                stage_conf=flow.confidence,
                mode=MODE_3V3,
                mode_conf=0.0,
                model_version=self.model_version,
                match_flow_label=flow.label,
                match_flow_conf=flow.confidence,
                hero_select_label=selection.label,
                hero_select_conf=selection.confidence,
            )
        if flow.label != 'match_flow':
            return StagePrediction(
                content=CONTENT_VAINGLORY,
                content_conf=flow.confidence,
                stage=STAGE_OUT_OF_MATCH,
                stage_conf=flow.confidence,
                mode=MODE_3V3,
                mode_conf=0.0,
                model_version=self.model_version,
                match_flow_label=flow.label,
                match_flow_conf=flow.confidence,
                hero_select_label=selection.label,
                hero_select_conf=selection.confidence,
            )
        mode = self._match_mode.predict(frame)
        return StagePrediction(
            content=CONTENT_VAINGLORY,
            content_conf=flow.confidence,
            stage=STAGE_GAMEPLAY,
            stage_conf=min(flow.confidence, selection.confidence),
            mode=_mode_value(mode.label),
            mode_conf=mode.confidence,
            model_version=self.model_version,
            match_flow_label=flow.label,
            match_flow_conf=flow.confidence,
            hero_select_label=selection.label,
            hero_select_conf=selection.confidence,
            match_mode_label=mode.label,
            match_mode_conf=mode.confidence,
        )


@dataclass(frozen=True)
class PackageRuntime:
    package: ModelPackage
    stage_classifier: PackageStageClassifier
    result_panel_detector: OnnxResultPanelDetector
    classifiers: Mapping[str, OnnxClassificationModel]
    hero_avatar_detector: OnnxHeroAvatarDetector
    hero_recognizer: PackageHeroRecognizer
    recorded_player_detector: PackageRecordedPlayerDetector


def build_package_runtime(
    package: ModelPackage, *, providers: Optional[Sequence[str]] = None
) -> PackageRuntime:
    classifiers: Dict[str, OnnxClassificationModel] = {
        role: OnnxClassificationModel(package.model(role), providers=providers)
        for role in ('match_flow', 'hero_select', 'match_mode')
    }
    result_spec = package.model('result_panel')
    if result_spec.kind != 'detection':
        raise ValueError('result_panel 必须是检测模型')
    if result_spec.input_width != result_spec.input_height:
        raise ValueError('result_panel 当前只支持正方形输入')
    result_panel = OnnxResultPanelDetector(
        result_spec.path,
        confidence_threshold=package.runtime.thresholds['result_panel'],
        input_size=result_spec.input_width,
        providers=providers,
        model_version=package.package_id,
    )
    stage_classifier = PackageStageClassifier(
        package_id=package.package_id,
        match_flow=classifiers['match_flow'],
        hero_select=classifiers['hero_select'],
        match_mode=classifiers['match_mode'],
        thresholds=package.runtime.thresholds,
    )
    hero_avatar_detector = OnnxHeroAvatarDetector(
        package.model('hero_avatar'),
        confidence_threshold=package.runtime.thresholds['hero_avatar'],
        providers=providers,
    )
    identity = OnnxClassificationModel(
        package.model('hero_identity'), providers=providers
    )
    classifiers['hero_identity'] = identity
    hero_recognizer = PackageHeroRecognizer(
        identity, threshold=package.runtime.thresholds['hero_identity']
    )
    player = OnnxClassificationModel(
        package.model('player_position'), providers=providers
    )
    classifiers['player_position'] = player
    recorded_player_detector = PackageRecordedPlayerDetector(
        player, threshold=package.runtime.thresholds['player_position']
    )
    return PackageRuntime(
        package,
        stage_classifier,
        result_panel,
        classifiers,
        hero_avatar_detector,
        hero_recognizer,
        recorded_player_detector,
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _model_path(root: Path, value: Any) -> Path:
    relative = Path(str(value or ''))
    if not str(relative) or relative.is_absolute():
        raise ValueError('模型文件必须位于模型包目录内')
    resolved = (root / relative).resolve()
    try:
        resolved.relative_to(root)
    except ValueError as error:
        raise ValueError('模型文件不在模型包目录内') from error
    if not resolved.is_file():
        raise FileNotFoundError(resolved)
    return resolved


def _positive_int(value: Any, *, name: str, default: int = 0) -> int:
    try:
        result = int(default if value is None else value)
    except (TypeError, ValueError) as error:
        raise ValueError('{} 必须是正整数'.format(name)) from error
    if result <= 0:
        raise ValueError('{} 必须是正整数'.format(name))
    return result


def _runtime_config(value: Any) -> RuntimeConfig:
    raw = value if isinstance(value, dict) else {}
    thresholds_raw = raw.get('thresholds')
    thresholds: Dict[str, float] = {}
    for role, default in {
        'match_flow': 0.55,
        'hero_select': 0.55,
        'match_mode': 0.50,
        'result_panel': 0.55,
        'hero_avatar': 0.25,
        'hero_identity': 0.50,
        'player_position': 0.50,
    }.items():
        source = thresholds_raw if isinstance(thresholds_raw, dict) else {}
        try:
            threshold = float(source.get(role, default))
        except (TypeError, ValueError) as error:
            raise ValueError('{} 阈值无效'.format(role)) from error
        if not 0 < threshold < 1:
            raise ValueError('{} 阈值必须在 0 和 1 之间'.format(role))
        thresholds[role] = threshold
    interval_ms = _positive_int(
        raw.get('coarse_interval_ms'), name='一级扫描间隔', default=5_000
    )
    maximum_distance_ms = _positive_int(
        raw.get('maximum_keyframe_distance_ms'),
        name='关键帧最大距离',
        default=interval_ms // 2,
    )
    return RuntimeConfig(
        coarse_interval_ms=interval_ms,
        maximum_keyframe_distance_ms=maximum_distance_ms,
        result_scan_fps=_positive_int(
            raw.get('result_scan_fps'), name='结算精扫帧率', default=4
        ),
        thresholds=thresholds,
    )


def load_model_package(path: Path) -> ModelPackage:
    root = Path(path).expanduser().resolve()
    if not root.is_dir():
        raise FileNotFoundError(root)
    manifest_path = root / 'manifest.json'
    try:
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    except json.JSONDecodeError as error:
        raise ValueError('模型包 manifest.json 不是有效 JSON') from error
    if not isinstance(manifest, dict):
        raise ValueError('模型包 manifest.json 必须是对象')
    schema_version = int(manifest.get('schema_version') or 0)
    if schema_version not in SUPPORTED_SCHEMA_VERSIONS:
        raise ValueError('不支持的模型包 schema_version: {}'.format(schema_version))
    package_id = str(manifest.get('package_id') or '').strip()
    pipeline_version = str(manifest.get('pipeline_version') or '').strip()
    if not package_id or not pipeline_version:
        raise ValueError('模型包缺少 package_id 或 pipeline_version')
    raw_models = manifest.get('models')
    if not isinstance(raw_models, dict):
        raise ValueError('模型包 models 必须是对象')
    missing = [role for role in REQUIRED_MODEL_ROLES if role not in raw_models]
    if missing:
        raise ValueError('模型包缺少核心角色: {}'.format(', '.join(missing)))
    if manifest.get('status') != 'ready':
        raise ValueError('只有 ready 模型包可以由 Worker 加载')

    models: Dict[str, ModelSpec] = {}
    for role, raw_value in raw_models.items():
        if not isinstance(raw_value, dict):
            raise ValueError('{} 模型配置无效'.format(role))
        model_path = _model_path(root, raw_value.get('file'))
        expected_hash = str(raw_value.get('sha256') or '').lower()
        if len(expected_hash) != 64 or _sha256(model_path) != expected_hash:
            raise ValueError('{} 模型 SHA-256 校验失败'.format(role))
        kind = str(raw_value.get('kind') or '')
        if kind not in {'classification', 'detection'}:
            raise ValueError('{} 模型 kind 无效'.format(role))
        input_config = raw_value.get('input')
        if not isinstance(input_config, dict):
            raise ValueError('{} 模型缺少输入契约'.format(role))
        classes_value = raw_value.get('classes')
        if not isinstance(classes_value, list) or not classes_value:
            raise ValueError('{} 模型缺少类别顺序'.format(role))
        models[str(role)] = ModelSpec(
            role=str(role),
            path=model_path,
            sha256=expected_hash,
            kind=kind,
            input_width=_positive_int(
                input_config.get('width'), name='{} 输入宽度'.format(role)
            ),
            input_height=_positive_int(
                input_config.get('height'), name='{} 输入高度'.format(role)
            ),
            color=str(input_config.get('color') or ''),
            resize=str(input_config.get('resize') or ''),
            pad_value=int(input_config.get('pad_value') or 0),
            preserve_full_image=bool(input_config.get('preserve_full_image', False)),
            scale=str(input_config.get('scale') or ''),
            normalize=str(input_config.get('normalize') or ''),
            classes=tuple(str(value) for value in classes_value),
            dataset_version=str(raw_value.get('dataset_version') or ''),
            training_run_id=str(raw_value.get('training_run_id') or ''),
        )
    return ModelPackage(
        root=root,
        package_id=package_id,
        pipeline_version=pipeline_version,
        schema_version=schema_version,
        models=models,
        runtime=_runtime_config(manifest.get('runtime')),
    )
