"""multi-v2 三头分类器与基于分类时间线的结算窗口推断。

multi-v2 一帧同时输出三个头：content(是否虚荣)、stage(8 类阶段)、mode(3 类模式)。
单帧的 result_page/victory_defeat 分类并不可靠(实测 60%/14%)，因此窗口推断
不依赖结算帧本身，而是依赖 gameplay(100% 可靠) 的连续性来切分对局段，
再对每个对局段的结束点推断结算画面出现的时间窗口。
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .vision import RgbFrame

CONTENT_VAINGLORY = 0
CONTENT_NOT_VAINGLORY = 1

STAGE_GAMEPLAY = 0
STAGE_SCOREBOARD = 1
STAGE_RESULT_PAGE = 2
STAGE_VICTORY_DEFEAT = 3
STAGE_PRE_MATCH = 4
STAGE_OUT_OF_MATCH = 5
STAGE_TRANSITION = 6
STAGE_TALENT_SELECT = 7

MODE_3V3 = 0
MODE_ARAM = 1
MODE_5V5 = 2

_STAGE_RESULT_SIGNALS = (STAGE_RESULT_PAGE, STAGE_VICTORY_DEFEAT)
_STAGE_IN_MATCH = (STAGE_GAMEPLAY, STAGE_PRE_MATCH, STAGE_TALENT_SELECT)


@dataclass(frozen=True)
class StagePrediction:
    content: int
    content_conf: float
    stage: int
    stage_conf: float
    mode: int
    mode_conf: float


@dataclass(frozen=True)
class ClassifiedObservation:
    at_ms: int
    stage: int
    stage_conf: float
    mode: int
    content: int


@dataclass(frozen=True)
class ClassifiedResultWindow:
    start_ms: int
    end_ms: int
    mode: str
    focus_ms: Optional[int] = None


def stage_classifier_model_path() -> Path:
    return (
        Path(__file__).resolve().parent.parent / 'data' / 'vainglory' / 'multi-v2.onnx'
    )


def _softmax(values: Any) -> Tuple[float, ...]:
    import numpy

    shifted = values - values.max()
    exponentials = numpy.exp(shifted)
    return tuple(float(value) for value in exponentials / exponentials.sum())


class StageClassifier:
    def __init__(
        self,
        model_path: Path,
        *,
        input_size: int = 224,
        providers: Optional[Sequence[str]] = None,
    ) -> None:
        if input_size <= 0:
            raise ValueError('分类模型输入尺寸必须为正数')
        path = Path(model_path)
        if not path.is_file():
            raise FileNotFoundError(path)
        onnxruntime: Any = importlib.import_module('onnxruntime')
        options = onnxruntime.SessionOptions()
        options.intra_op_num_threads = 1
        options.inter_op_num_threads = 1
        self._session = onnxruntime.InferenceSession(
            str(path),
            sess_options=options,
            providers=tuple(providers or ('CPUExecutionProvider',)),
        )
        self._input_name = self._session.get_inputs()[0].name
        self._input_size = input_size

    def classify(self, frame: RgbFrame) -> StagePrediction:
        cv2: Any = importlib.import_module('cv2')
        numpy: Any = importlib.import_module('numpy')
        image = numpy.frombuffer(frame.pixels, dtype=numpy.uint8).reshape(
            frame.height, frame.width, 3
        )
        resized = cv2.resize(
            image, (self._input_size, self._input_size), interpolation=cv2.INTER_LINEAR
        )
        tensor = numpy.ascontiguousarray(resized.transpose(2, 0, 1)[None]).astype(
            numpy.float32
        )
        tensor /= 255.0
        tensor -= numpy.asarray([0.485, 0.456, 0.406], dtype=numpy.float32).reshape(
            1, 3, 1, 1
        )
        tensor /= numpy.asarray([0.229, 0.224, 0.225], dtype=numpy.float32).reshape(
            1, 3, 1, 1
        )
        content, stage, mode = self._session.run(None, {self._input_name: tensor})
        content_confidences = _softmax(numpy.squeeze(content))
        stage_confidences = _softmax(numpy.squeeze(stage))
        mode_confidences = _softmax(numpy.squeeze(mode))
        return StagePrediction(
            content=int(content_confidences.index(max(content_confidences))),
            content_conf=max(content_confidences),
            stage=int(stage_confidences.index(max(stage_confidences))),
            stage_conf=max(stage_confidences),
            mode=int(mode_confidences.index(max(mode_confidences))),
            mode_conf=max(mode_confidences),
        )


def load_stage_classifier(
    *, model_path: Optional[Path] = None, providers: Optional[Sequence[str]] = None
) -> Optional[StageClassifier]:
    path = stage_classifier_model_path() if model_path is None else Path(model_path)
    if not path.is_file():
        return None
    return StageClassifier(path, providers=providers)


def smooth_stages(
    observations: Sequence[ClassifiedObservation], *, window: int = 3
) -> Tuple[ClassifiedObservation, ...]:
    if window < 1:
        raise ValueError('smoothing window must be positive')
    if len(observations) <= window:
        return tuple(observations)
    result: List[ClassifiedObservation] = []
    radius = window // 2
    for index, observation in enumerate(observations):
        if observation.stage in _STAGE_RESULT_SIGNALS:
            result.append(observation)
            continue
        neighbors = observations[
            max(0, index - radius) : min(len(observations), index + radius + 1)
        ]
        counts = {stage: 0 for stage in range(8)}
        for neighbor in neighbors:
            counts[neighbor.stage] += 1
        stage = max(counts, key=counts.__getitem__)
        if stage == observation.stage:
            result.append(observation)
            continue
        result.append(
            ClassifiedObservation(
                at_ms=observation.at_ms,
                stage=stage,
                stage_conf=min(observation.stage_conf, counts[stage] / len(neighbors)),
                mode=observation.mode,
                content=observation.content,
            )
        )
    return tuple(result)


def gameplay_runs(
    observations: Sequence[ClassifiedObservation], *, maximum_gap_ms: int = 20_000
) -> Tuple[Tuple[ClassifiedObservation, ClassifiedObservation], ...]:
    """返回对局段列表，每段为 (段内最早对局观测, 段内最后 gameplay 观测)。"""
    if maximum_gap_ms < 0:
        raise ValueError('gameplay gap must not be negative')
    runs: List[List[ClassifiedObservation]] = []
    for observation in observations:
        if observation.content != CONTENT_VAINGLORY:
            continue
        if observation.stage not in _STAGE_IN_MATCH:
            continue
        if runs and observation.at_ms - runs[-1][-1].at_ms <= maximum_gap_ms:
            runs[-1].append(observation)
            continue
        runs.append([observation])
    result: List[Tuple[ClassifiedObservation, ClassifiedObservation]] = []
    for run in runs:
        gameplay = tuple(item for item in run if item.stage == STAGE_GAMEPLAY) or run
        result.append((run[0], gameplay[-1]))
    return tuple(result)


def _mode_for_run(run: Sequence[ClassifiedObservation]) -> str:
    # 模式判定只信"明确界面"的信号：
    # - 大乱斗：天赋选择界面帧（大乱斗特有，且该界面帧 mode 头实测 100% 为 aram）
    # - 5v5：积分板/结算页帧的 mode 头（这些界面与 3v3/aram 画面差异大，mode 可信）
    # gameplay 帧的 mode 头在 3v3/aram 同地图下不可信，一律不参与投票
    talent_frames = sum(
        item.stage == STAGE_TALENT_SELECT and item.mode == MODE_ARAM for item in run
    )
    interface_five_frames = sum(
        item.stage in (STAGE_SCOREBOARD, STAGE_RESULT_PAGE) and item.mode == MODE_5V5
        for item in run
    )
    if talent_frames >= 2:
        return 'aram'
    if interface_five_frames >= 2:
        return '5v5'
    return 'unknown'


def _exit_signal_windows(
    observations: Sequence[ClassifiedObservation],
    *,
    duration_ms: int,
    exit_before_ms: int = 150_000,
    exit_after_ms: int = 25_000,
) -> Tuple[ClassifiedResultWindow, ...]:
    """退出信号回扫窗口。

    对局中→游戏外/转场的切换点意味着前面必然出现过结算画面（对局结束
    才能回大厅），但结算帧本身可能被误判为 gameplay（长时间空白）。
    因此在退出信号帧前回扫一段距离生成窗口，覆盖"结算被误判"的空白。
    """
    if min(exit_before_ms, exit_after_ms) < 0:
        raise ValueError('exit window paddings must not be negative')
    windows: List[ClassifiedResultWindow] = []
    for index, item in enumerate(observations):
        if item.stage not in (STAGE_OUT_OF_MATCH, STAGE_TRANSITION):
            continue
        if index > 0 and observations[index - 1].stage not in _STAGE_IN_MATCH:
            continue
        windows.append(
            ClassifiedResultWindow(
                start_ms=max(0, min(duration_ms, item.at_ms - exit_before_ms)),
                end_ms=min(duration_ms, item.at_ms + exit_after_ms),
                mode='unknown',
                focus_ms=item.at_ms,
            )
        )
    return tuple(windows)


def _pre_match_anchors(
    observations: Sequence[ClassifiedObservation],
    *,
    minimum_frames: int = 2,
    gap_ms: int = 15_000,
) -> Tuple[Tuple[ClassifiedObservation, ClassifiedObservation], ...]:
    """选英雄界面连续段（对局开始信号节点）。

    选英雄界面是每局必有的信号（持续 1 到数分钟），但选完可能秒退/取消
    （对局未开始），因此锚点只是"对局可能开始"的信号，需要后续 gameplay
    连续性确认。
    """
    if minimum_frames < 1:
        raise ValueError('anchor minimum frames must be positive')
    anchors: List[Tuple[ClassifiedObservation, ClassifiedObservation]] = []
    current: List[ClassifiedObservation] = []
    for item in observations:
        if item.stage == STAGE_PRE_MATCH:
            if current and item.at_ms - current[-1].at_ms > gap_ms:
                if len(current) >= minimum_frames:
                    anchors.append((current[0], current[-1]))
                current = []
            current.append(item)
            continue
        if len(current) >= minimum_frames:
            anchors.append((current[0], current[-1]))
        current = []
    if len(current) >= minimum_frames:
        anchors.append((current[0], current[-1]))
    return tuple(anchors)


def _confirmed_anchors(
    anchors: Sequence[Tuple[ClassifiedObservation, ClassifiedObservation]],
    observations: Sequence[ClassifiedObservation],
    *,
    confirmation_window_ms: int = 300_000,
) -> Tuple[Tuple[ClassifiedObservation, ClassifiedObservation], ...]:
    """锚点（选英雄界面）对局确认。

    锚点后确认窗口内出现 gameplay 才算对局真正开始；如果窗口内先出现
    新的选英雄界面（另一锚点），说明前一局的选英雄没有开始（秒退/取消/
    选完退出），前一锚点报废。5v5 的 BP 本身可能持续数分钟，因此确认
    窗口放宽到 5 分钟。
    """
    if confirmation_window_ms < 0:
        raise ValueError('confirmation window must not be negative')
    confirmed = []
    for index, (anchor_start, anchor_end) in enumerate(anchors):
        window_end = anchor_end.at_ms + confirmation_window_ms
        first_gameplay = next(
            (
                item
                for item in observations
                if anchor_end.at_ms < item.at_ms <= window_end
                and item.stage == STAGE_GAMEPLAY
            ),
            None,
        )
        if first_gameplay is None:
            continue
        next_anchor = anchors[index + 1] if index + 1 < len(anchors) else None
        if next_anchor is not None and next_anchor[0].at_ms < first_gameplay.at_ms:
            continue
        confirmed.append((anchor_start, anchor_end))
    return tuple(confirmed)


def build_classified_windows(
    observations: Sequence[ClassifiedObservation],
    *,
    duration_ms: int,
    result_before_ms: int = 40_000,
    result_after_ms: int = 25_000,
    result_signal_pad_ms: int = 8_000,
    run_gap_ms: int = 20_000,
    run_modes: Optional[Dict[int, str]] = None,
) -> Tuple[ClassifiedResultWindow, ...]:
    if duration_ms <= 0:
        raise ValueError('video duration must be positive')
    if min(result_before_ms, result_after_ms, result_signal_pad_ms) < 0:
        raise ValueError('window paddings must not be negative')
    smoothed = smooth_stages(observations)
    runs = gameplay_runs(smoothed, maximum_gap_ms=run_gap_ms)
    anchors = _confirmed_anchors(_pre_match_anchors(smoothed), smoothed)
    segments = _segment_ranges(runs, anchors)

    def bounded(start_ms: int, end_ms: int) -> Tuple[int, int]:
        return (max(0, min(duration_ms, start_ms)), max(0, min(duration_ms, end_ms)))

    windows: List[ClassifiedResultWindow] = []
    for segment_start_ms, segment_end_ms in segments:
        gameplay_frames = tuple(
            item
            for item in smoothed
            if segment_start_ms <= item.at_ms <= segment_end_ms
            and item.stage == STAGE_GAMEPLAY
        )
        if not gameplay_frames:
            continue
        end_ms = gameplay_frames[-1].at_ms
        segment_observations = tuple(
            item for item in observations if segment_start_ms <= item.at_ms <= end_ms
        )
        run_mode = run_modes.get(segment_start_ms) if run_modes is not None else None
        if run_mode is None:
            run_mode = _mode_for_run(segment_observations)
        following = tuple(
            item
            for item in smoothed
            if item.at_ms > end_ms and item.at_ms <= end_ms + result_after_ms
        )
        result_signals = tuple(
            item for item in following if item.stage in _STAGE_RESULT_SIGNALS
        )
        if result_signals:
            first_signal = result_signals[0].at_ms
            last_signal = result_signals[-1].at_ms
            start_ms, window_end = bounded(
                end_ms - result_before_ms, last_signal + result_signal_pad_ms
            )
            windows.append(
                ClassifiedResultWindow(
                    start_ms=start_ms,
                    end_ms=window_end,
                    mode=run_mode,
                    focus_ms=first_signal,
                )
            )
        else:
            window_end = end_ms + result_after_ms
            after_states = tuple(
                item for item in following if item.stage != STAGE_TRANSITION
            )
            if after_states:
                window_end = min(window_end, after_states[0].at_ms + result_after_ms)
            start_ms, bounded_end = bounded(end_ms - result_before_ms, window_end)
            if bounded_end > start_ms:
                windows.append(
                    ClassifiedResultWindow(
                        start_ms=start_ms,
                        end_ms=bounded_end,
                        mode=run_mode,
                        focus_ms=end_ms,
                    )
                )
        windows.extend(
            _run_interior_windows(
                segment_observations,
                duration_ms=duration_ms,
                result_before_ms=result_before_ms,
                result_after_ms=result_after_ms,
                run_mode=run_mode,
            )
        )
    signal_only = tuple(
        item
        for item in observations
        if item.stage in _STAGE_RESULT_SIGNALS
        and not any(window.start_ms <= item.at_ms < window.end_ms for window in windows)
    )
    if signal_only:
        start_ms, end_ms = bounded(
            signal_only[0].at_ms - result_signal_pad_ms,
            signal_only[-1].at_ms + result_signal_pad_ms,
        )
        signal_run = next(
            (
                run
                for run in runs
                if run[0].at_ms <= signal_only[0].at_ms <= run[1].at_ms
            ),
            None,
        )
        signal_mode = 'unknown'
        if signal_run is not None:
            if run_modes is not None:
                signal_mode = run_modes.get(signal_run[0].at_ms, 'unknown')
            if signal_mode == 'unknown':
                signal_mode = _mode_for_run(
                    tuple(
                        item
                        for item in smoothed
                        if signal_run[0].at_ms <= item.at_ms <= signal_run[1].at_ms
                    )
                )
        windows.append(
            ClassifiedResultWindow(
                start_ms=start_ms,
                end_ms=end_ms,
                mode=signal_mode,
                focus_ms=signal_only[0].at_ms,
            )
        )
    return _merge_classified_windows(windows)


def _segment_ranges(
    runs: Sequence[Tuple[ClassifiedObservation, ClassifiedObservation]],
    anchors: Sequence[Tuple[ClassifiedObservation, ClassifiedObservation]],
) -> Tuple[Tuple[int, int], ...]:
    """对局段时间范围，锚点（选英雄界面）处切分。

    锚点属于新一局的开始，因此被锚点穿过的 gameplay 段从锚点处一分为二；
    没有锚点的段（视频开头的进行中局）保持原样作为兜底。
    """
    anchor_times = tuple(anchor_start.at_ms for anchor_start, _ in anchors)
    if not anchor_times:
        return tuple((run_start.at_ms, run_end.at_ms) for run_start, run_end in runs)
    ranges: List[Tuple[int, int]] = []
    for run_start, run_end in runs:
        cuts = tuple(
            at_ms for at_ms in anchor_times if run_start.at_ms < at_ms < run_end.at_ms
        )
        boundaries = (run_start.at_ms, *cuts, run_end.at_ms)
        for index in range(len(boundaries) - 1):
            ranges.append((boundaries[index], boundaries[index + 1]))
    merged: List[Tuple[int, int]] = []
    for low, high in sorted(ranges):
        if merged and low < merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], high))
            continue
        merged.append((low, high))
    return tuple(merged)


def _run_interior_windows(
    run_observations: Sequence[ClassifiedObservation],
    *,
    duration_ms: int,
    result_before_ms: int,
    result_after_ms: int,
    run_mode: str,
) -> Tuple[ClassifiedResultWindow, ...]:
    """对局段内部的结算信号帧生成独立窗口。

    巨型对局段（结算帧被误判为 gameplay 导致段不结束）中，被识别为
    结算页/胜负动画的帧不能只靠段尾窗口覆盖，这里每个信号帧单独出窗。
    """
    if not run_observations:
        return ()
    windows = []
    for signal in run_observations:
        if signal.stage not in _STAGE_RESULT_SIGNALS:
            continue
        windows.append(
            ClassifiedResultWindow(
                start_ms=max(0, min(duration_ms, signal.at_ms - result_before_ms)),
                end_ms=min(duration_ms, signal.at_ms + result_after_ms),
                mode=run_mode,
                focus_ms=signal.at_ms,
            )
        )
    return tuple(windows)


def _merge_classified_windows(
    windows: Sequence[ClassifiedResultWindow], *, merge_gap_ms: int = 5_000
) -> Tuple[ClassifiedResultWindow, ...]:
    ordered = sorted(
        (window for window in windows if window.end_ms > window.start_ms),
        key=lambda window: (window.start_ms, window.end_ms),
    )
    merged: List[ClassifiedResultWindow] = []
    for window in ordered:
        if not merged or window.start_ms > merged[-1].end_ms + merge_gap_ms:
            merged.append(window)
            continue
        previous = merged[-1]
        merged[-1] = ClassifiedResultWindow(
            start_ms=previous.start_ms,
            end_ms=max(previous.end_ms, window.end_ms),
            mode=previous.mode if previous.mode != 'unknown' else window.mode,
            focus_ms=(
                window.focus_ms if window.focus_ms is not None else previous.focus_ms
            ),
        )
    return tuple(merged)
