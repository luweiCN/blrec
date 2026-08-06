from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any, Optional, Protocol, Sequence

from .vision import PixelRect, ResultPanelDetection, RgbFrame


class ResultPanelDetector(Protocol):
    def detect(  # noqa: E704
        self, frame: RgbFrame
    ) -> Optional[ResultPanelDetection]: ...  # noqa: E704


def result_panel_model_path() -> Path:
    return (
        Path(__file__).resolve().parent.parent
        / 'data'
        / 'vainglory'
        / 'result-panel.onnx'
    )


class OnnxResultPanelDetector:
    def __init__(
        self,
        model_path: Path,
        *,
        confidence_threshold: float = 0.55,
        input_size: int = 640,
        providers: Optional[Sequence[str]] = None,
    ) -> None:
        if not 0 < confidence_threshold < 1:
            raise ValueError('结算页检测置信度阈值必须在 0 和 1 之间')
        if input_size <= 0:
            raise ValueError('结算页检测输入尺寸必须为正数')
        path = Path(model_path)
        if not path.is_file():
            raise FileNotFoundError(path)
        numpy: Any = importlib.import_module('numpy')
        onnxruntime: Any = importlib.import_module('onnxruntime')
        options = onnxruntime.SessionOptions()
        options.intra_op_num_threads = 1
        options.inter_op_num_threads = 1
        self._numpy = numpy
        self._session = onnxruntime.InferenceSession(
            str(path),
            sess_options=options,
            providers=tuple(providers or ('CPUExecutionProvider',)),
        )
        self._input_name = self._session.get_inputs()[0].name
        self._confidence_threshold = confidence_threshold
        self._input_size = input_size

    def detect(self, frame: RgbFrame) -> Optional[ResultPanelDetection]:
        cv2: Any = importlib.import_module('cv2')
        numpy = self._numpy
        image = numpy.frombuffer(frame.pixels, dtype=numpy.uint8).reshape(
            frame.height, frame.width, 3
        )
        scale = min(self._input_size / frame.width, self._input_size / frame.height)
        width = max(1, min(self._input_size, round(frame.width * scale)))
        height = max(1, min(self._input_size, round(frame.height * scale)))
        resized = cv2.resize(image, (width, height), interpolation=cv2.INTER_LINEAR)
        left = (self._input_size - width) // 2
        top = (self._input_size - height) // 2
        canvas = numpy.full(
            (self._input_size, self._input_size, 3), 114, dtype=numpy.uint8
        )
        canvas[top : top + height, left : left + width] = resized
        tensor = numpy.ascontiguousarray(
            canvas.transpose(2, 0, 1)[None].astype(numpy.float32) / 255.0
        )
        output = self._session.run(None, {self._input_name: tensor})[0]
        prediction = numpy.squeeze(output)
        if prediction.ndim != 2:
            return None
        if prediction.shape[0] <= 16 and prediction.shape[1] > prediction.shape[0]:
            prediction = prediction.transpose()
        if prediction.shape[1] < 5 or prediction.shape[0] == 0:
            return None
        scores = (
            prediction[:, 4]
            if prediction.shape[1] == 5
            else numpy.max(prediction[:, 4:], axis=1)
        )
        index = int(numpy.argmax(scores))
        confidence = float(scores[index])
        if confidence < self._confidence_threshold:
            return None
        center_x, center_y, box_width, box_height = (
            float(value) for value in prediction[index, :4]
        )
        source_left = (center_x - box_width / 2 - left) / scale
        source_top = (center_y - box_height / 2 - top) / scale
        source_right = (center_x + box_width / 2 - left) / scale
        source_bottom = (center_y + box_height / 2 - top) / scale
        pixel_left = max(0, min(frame.width - 1, round(source_left)))
        pixel_top = max(0, min(frame.height - 1, round(source_top)))
        pixel_right = max(pixel_left + 1, min(frame.width, round(source_right)))
        pixel_bottom = max(pixel_top + 1, min(frame.height, round(source_bottom)))
        if (
            pixel_right - pixel_left < frame.width * 0.45
            or pixel_bottom - pixel_top < frame.height * 0.3
        ):
            return None
        return ResultPanelDetection(
            rect=PixelRect(pixel_left, pixel_top, pixel_right, pixel_bottom),
            confidence=confidence,
        )


def load_result_panel_detector(
    *, providers: Optional[Sequence[str]] = None
) -> Optional[OnnxResultPanelDetector]:
    path = result_panel_model_path()
    if not path.is_file():
        return None
    return OnnxResultPanelDetector(path, providers=providers)
