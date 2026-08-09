from __future__ import annotations

import json
import re
import time
from collections import Counter
from dataclasses import dataclass, replace
from pathlib import Path
from statistics import mean
from typing import (
    Any,
    Callable,
    Dict,
    List,
    Literal,
    Optional,
    Protocol,
    Sequence,
    Set,
    Tuple,
)

from loguru import logger

from .catalog import hero_chinese_name
from .hero_recognition import HeroMatch, SiftHeroRecognizer
from .mode_recognition import AramDetector, AramTalentSelectionDetector
from .ocr import ResultHeader, ResultOcr, TesseractResultReader, merge_result_headers
from .result_detection import ResultPanelDetector
from .sampling import (
    CoarseObservation,
    FfmpegSampler,
    ScanWindow,
    TimedFrame,
    hero_lineup_evidence,
    hud_lineup_similarity,
    result_search_windows,
    same_gameplay_run,
)
from .stage_classifier import (
    CONTENT_NOT_VAINGLORY,
    MODE_3V3,
    MODE_5V5,
    MODE_ARAM,
    STAGE_GAMEPLAY,
    STAGE_OUT_OF_MATCH,
    STAGE_PRE_MATCH,
    STAGE_RESULT_PAGE,
    STAGE_SCOREBOARD,
    STAGE_TALENT_SELECT,
    STAGE_TRANSITION,
    STAGE_VICTORY_DEFEAT,
    ClassifiedObservation,
    StageClassifier,
    StagePrediction,
    _confirmed_anchors,
    _pre_match_anchors,
    _segment_ranges,
    build_classified_windows,
    gameplay_runs,
    smooth_stages,
)
from .vision import (
    GameplayHud,
    HeroFrame,
    PixelRect,
    RecordedPlayer,
    ResultLayout,
    RgbFrame,
    TeamSide,
    TeamSize,
    ViewportTransform,
    detect_active_content_rect,
    detect_gameplay_hud,
    detect_gameplay_hud_details,
    detect_observer_hud,
    detect_recorded_player,
    detect_result_layouts,
    extract_gameplay_hud_heroes,
    extract_result_heroes,
    hero_fingerprint,
    jpeg_bytes,
    normalize_gameplay_frame,
    png_bytes,
    result_frame_quality,
    select_gameplay_hud_centers,
)


class AnalysisCancelled(RuntimeError):
    pass


class _ResultEvidenceRejected(RuntimeError):
    def __init__(self, result: ResultOcr) -> None:
        super().__init__('结算画面证据不完整')
        self.result = result


def _detect_hud_context(
    frame: RgbFrame,
) -> Tuple[Optional[GameplayHud], Literal['played', 'observed', 'unknown']]:
    detected = detect_gameplay_hud_details(frame)
    if detected is not None:
        return detected, 'played'
    detected = detect_observer_hud(frame)
    return detected, 'observed' if detected is not None else 'unknown'


class ResultReader(Protocol):
    def read_header(  # noqa: E704
        self,
        frame: RgbFrame,
        *,
        viewport: ViewportTransform = ...,
        team_size: int = ...,
    ) -> ResultHeader: ...  # noqa: E704

    def read(  # noqa: E704
        self,
        frame: RgbFrame,
        *,
        header: Optional[ResultHeader] = ...,
        viewport: ViewportTransform = ...,
        name_frames: Sequence[RgbFrame] = ...,
        team_size: int = ...,
    ) -> ResultOcr: ...  # noqa: E704

    def read_wide_screenshot(  # noqa: E704
        self,
        frame: RgbFrame,
        *,
        header: Optional[ResultHeader] = ...,
        viewport: ViewportTransform = ...,
        name_frames: Sequence[RgbFrame] = ...,
        team_size: int = ...,
    ) -> ResultOcr: ...  # noqa: E704


@dataclass(frozen=True)
class VideoPart:
    id: int
    index: int
    path: str
    title: str = ''
    manual_candidate_times_ms: Tuple[int, ...] = ()


@dataclass(frozen=True)
class ResultHit:
    at_ms: int
    layout: ResultLayout
    view_context: Literal['played', 'observed', 'unknown'] = 'unknown'
    hero_lineup: Tuple[str, ...] = ()


@dataclass(frozen=True)
class _WindowScanResult:
    hits: Tuple[ResultHit, ...]
    keyframe_preview_frames: int
    fallback_preview_frames: int
    refinement_frames: int
    refinement_windows: int
    expanded_fallback: bool = False


@dataclass(frozen=True)
class AnalyzedHero:
    side: TeamSide
    slot: int
    fingerprint: str
    thumbnail_png: bytes
    label: str = ''
    confidence: float = 0


@dataclass(frozen=True)
class AnalyzedMatch:
    part_id: int
    part_index: int
    result_at_ms: int
    layout: ResultLayout
    ocr: ResultOcr
    heroes: Tuple[AnalyzedHero, ...]
    confidence: float
    result_frame_png: bytes = b''
    game_mode: str = 'unknown'
    recorded_player: Optional[RecordedPlayer] = None
    match_kind: Literal['pvp', 'bot', 'practice', 'unknown'] = 'unknown'
    view_context: Literal['played', 'observed', 'unknown'] = 'unknown'
    stats_eligible: bool = True
    stats_exclusion_reason: str = ''


@dataclass(frozen=True)
class ScannedPart:
    video_duration_ms: int
    candidate_times_ms: Tuple[int, ...]
    candidate_view_contexts: Tuple[Literal['played', 'observed', 'unknown'], ...] = ()
    candidate_hero_lineups: Tuple[Tuple[str, ...], ...] = ()
    candidate_modes: Tuple[str, ...] = ()


TrainingCandidateTask = Literal[
    'screen_state', 'bp_review', 'key_screen_review', 'result_detector', 'mode_gate'
]
TrainingCandidateLabel = Literal[
    'not_vainglory',
    'out_of_match',
    'pre_match',
    'in_match',
    'talent_select',
    'post_match',
    'transition',
    'bp_3v3',
    'bp_aram',
    'bp_5v5',
    'not_bp',
    'result_page',
    'scoreboard',
    'other',
    'result_panel',
    'no_result_panel',
    'blocked_gate',
    'open_entrance',
    'no_evidence',
]


@dataclass(frozen=True)
class TrainingCandidateBox:
    box_type: str
    x: float
    y: float
    w: float
    h: float

    def __post_init__(self) -> None:
        if not (
            0 <= self.x <= 1
            and 0 <= self.y <= 1
            and 0 < self.w <= 1
            and 0 < self.h <= 1
            and self.x + self.w <= 1.001
            and self.y + self.h <= 1.001
        ):
            raise ValueError('training candidate box must be normalized')


@dataclass(frozen=True)
class TrainingCandidate:
    at_ms: int
    segment_start_ms: int
    image_jpeg: bytes
    model_version: str
    suggested_label: TrainingCandidateLabel
    suggestion_confidence: float
    stage_class: str
    stage_confidence: float
    mode_class: str
    mode_confidence: float
    selection_reason: str
    task: TrainingCandidateTask = 'bp_review'
    suggested_boxes: Tuple[TrainingCandidateBox, ...] = ()


def _remember_training_candidate(
    candidates: List[TrainingCandidate],
    *,
    task: TrainingCandidateTask,
    suggested_label: TrainingCandidateLabel,
    at_ms: int,
    segment_start_ms: int,
    frame: RgbFrame,
    model_version: str,
    suggestion_confidence: float,
    stage_class: str,
    stage_confidence: float,
    selection_reason: str,
    minimum_gap_ms: int,
    maximum_per_label: int,
    mode_class: str = 'unknown',
    mode_confidence: float = 0,
    suggested_boxes: Sequence[TrainingCandidateBox] = (),
    prefer_lower_confidence: bool = False,
    separate_modes: bool = False,
) -> bool:
    """保留少量间隔开的高价值帧；候选失败绝不影响主分析。"""
    same_label = [
        (index, candidate)
        for index, candidate in enumerate(candidates)
        if candidate.task == task
        and candidate.suggested_label == suggested_label
        and (not separate_modes or candidate.mode_class == mode_class)
    ]
    replacement_index: Optional[int] = None
    nearby = [
        item for item in same_label if abs(item[1].at_ms - at_ms) < minimum_gap_ms
    ]
    if nearby:
        replacement_index, previous = min(
            nearby, key=lambda item: abs(item[1].at_ms - at_ms)
        )
        if (
            suggestion_confidence >= previous.suggestion_confidence
            if prefer_lower_confidence
            else suggestion_confidence <= previous.suggestion_confidence
        ):
            return False
    elif len(same_label) >= maximum_per_label:
        replacement_index, replaceable = (
            max(same_label, key=lambda item: item[1].suggestion_confidence)
            if prefer_lower_confidence
            else min(same_label, key=lambda item: item[1].suggestion_confidence)
        )
        if (
            suggestion_confidence >= replaceable.suggestion_confidence
            if prefer_lower_confidence
            else suggestion_confidence <= replaceable.suggestion_confidence
        ):
            return False
    try:
        image_jpeg = jpeg_bytes(frame)
    except Exception as error:  # noqa: BLE001
        logger.warning(
            'Vainglory training candidate skipped: task={} at_ms={} error={!r}',
            task,
            at_ms,
            error,
        )
        return False
    candidate = TrainingCandidate(
        at_ms=int(at_ms),
        segment_start_ms=max(0, int(segment_start_ms)),
        image_jpeg=image_jpeg,
        model_version=model_version,
        suggested_label=suggested_label,
        suggestion_confidence=max(0, min(1, float(suggestion_confidence))),
        stage_class=stage_class,
        stage_confidence=max(0, min(1, float(stage_confidence))),
        mode_class=mode_class,
        mode_confidence=max(0, min(1, float(mode_confidence))),
        selection_reason=selection_reason,
        task=task,
        suggested_boxes=tuple(suggested_boxes),
    )
    if replacement_index is None:
        candidates.append(candidate)
    else:
        candidates[replacement_index] = candidate
    return True


def _result_panel_training_box(
    frame: RgbFrame, layout: ResultLayout
) -> TrainingCandidateBox:
    reference = (
        (0.01, 0.09, 0.99, 0.905) if layout.team_size == 5 else (0.09, 0.22, 0.91, 0.78)
    )
    rect = layout.viewport.source_rect(frame, *reference)
    return TrainingCandidateBox(
        box_type='result_panel',
        x=rect.left / frame.width,
        y=rect.top / frame.height,
        w=(rect.right - rect.left) / frame.width,
        h=(rect.bottom - rect.top) / frame.height,
    )


def _screen_state_candidate_label(
    prediction: StagePrediction,
) -> TrainingCandidateLabel:
    if prediction.content == CONTENT_NOT_VAINGLORY:
        return 'not_vainglory'
    labels: Dict[int, TrainingCandidateLabel] = {
        STAGE_PRE_MATCH: 'pre_match',
        STAGE_OUT_OF_MATCH: 'out_of_match',
        STAGE_TRANSITION: 'transition',
        STAGE_TALENT_SELECT: 'talent_select',
        STAGE_RESULT_PAGE: 'post_match',
        STAGE_VICTORY_DEFEAT: 'post_match',
        STAGE_GAMEPLAY: 'in_match',
        STAGE_SCOREBOARD: 'in_match',
    }
    return labels.get(prediction.stage, 'transition')


_SCREEN_STATE_CANDIDATE_LIMITS = {
    'not_vainglory': 2,
    'out_of_match': 3,
    'pre_match': 4,
    'in_match': 4,
    'talent_select': 3,
    'post_match': 3,
    'transition': 3,
}


def _selected_screen_state_candidates(
    candidates: Sequence[TrainingCandidate],
) -> List[TrainingCandidate]:
    selected = [
        candidate
        for label, maximum in _SCREEN_STATE_CANDIDATE_LIMITS.items()
        if label != 'in_match'
        for candidate in [item for item in candidates if item.suggested_label == label][
            :maximum
        ]
    ]
    known_modes = ('3v3', 'aram', '5v5')
    selected.extend(
        candidate
        for mode in known_modes
        for candidate in [
            item
            for item in candidates
            if item.suggested_label == 'in_match' and item.mode_class == mode
        ][: _SCREEN_STATE_CANDIDATE_LIMITS['in_match']]
    )
    selected.extend(
        item
        for item in candidates
        if item.suggested_label == 'in_match' and item.mode_class not in known_modes
    )
    return selected[:32]


@dataclass(frozen=True)
class DenseScanResult:
    scanned_part: ScannedPart
    decoded_frames: int
    result_frames: int
    probe_seconds: float
    decode_seconds: float
    detection_seconds: float
    total_seconds: float
    training_candidates: Tuple[TrainingCandidate, ...] = ()


@dataclass(frozen=True)
class AnalysisStatus:
    stage: Literal[
        'probing', 'coarse_scan', 'fine_scan', 'ocr_waiting', 'ocr_recognition'
    ]
    detail: str
    elapsed_seconds: float
    coarse_frames: int = 0
    gameplay_runs: int = 0
    result_windows: int = 0
    current_window: int = 0
    total_windows: int = 0
    candidate_count: int = 0
    current_candidate: int = 0
    total_candidates: int = 0
    rejected_candidates: int = 0
    recognized_matches: int = 0


def classify_match_kind(
    ocr: ResultOcr, heroes: Sequence[AnalyzedHero], *, team_size: int
) -> Literal['pvp', 'bot', 'practice', 'unknown']:
    populated_players = tuple(
        player
        for player in ocr.players
        if player.name
        or player.raw_name
        or any(
            value is not None
            for value in (
                player.stats.kills,
                player.stats.deaths,
                player.stats.assists,
                player.stats.economy,
            )
        )
    )
    recognized_heroes = tuple(hero for hero in heroes if hero.label)
    if len(populated_players) <= 1 and len(recognized_heroes) <= 1:
        return 'practice'

    evidence = '\n'.join(
        value
        for value in (ocr.raw_text, *(player.raw_name for player in ocr.players))
        if value
    )
    if team_size == 3:
        bot_names = {
            '{} Bot'.format(prefix.casefold())
            for prefix in ('Alpha', 'Beta', 'Gamma', 'Delta', 'Epsilon')
            if re.search(
                r'(?<![A-Za-z]){}\s+Bot(?![A-Za-z])'.format(prefix),
                evidence,
                re.IGNORECASE,
            )
        }
        if len(bot_names) >= 2:
            return 'bot'
    elif team_size == 5:
        matched_hero_names = 0
        for label in {hero.label for hero in recognized_heroes}:
            names = {label, hero_chinese_name(label)}
            if any(
                re.search(
                    r'[^\s\n]+\s+{}(?:\s|$)'.format(re.escape(name)),
                    evidence,
                    re.IGNORECASE,
                )
                for name in names
                if name
            ):
                matched_hero_names += 1
        if matched_hero_names >= 2:
            return 'bot'
    return 'pvp' if len(populated_players) >= 2 else 'unknown'


def stats_eligibility(
    *,
    game_mode: str,
    duration_seconds: Optional[int],
    match_kind: str,
    view_context: str,
) -> Tuple[bool, str]:
    if view_context == 'observed':
        return False, 'observed'
    if match_kind == 'bot':
        return False, 'bot'
    if match_kind == 'practice':
        return False, 'practice'
    if (
        game_mode == '3v3'
        and duration_seconds is not None
        and duration_seconds < 5 * 60
    ):
        return False, 'too_short_3v3'
    return True, ''


def exclude_content_duplicates(
    matches: Sequence[AnalyzedMatch],
) -> Tuple[AnalyzedMatch, ...]:
    seen: Set[Tuple[Any, ...]] = set()
    result: List[AnalyzedMatch] = []
    for match in sorted(matches, key=lambda item: item.result_at_ms):
        populated = tuple(
            player
            for player in match.ocr.players
            if player.normalized_name
            or any(
                value is not None
                for value in (
                    player.stats.kills,
                    player.stats.deaths,
                    player.stats.assists,
                    player.stats.economy,
                )
            )
        )
        duration = match.ocr.header.duration_seconds
        if duration is None or len(populated) < match.layout.team_size:
            result.append(match)
            continue
        fingerprint: Tuple[Any, ...] = (
            duration,
            match.ocr.header.end_reason,
            match.ocr.header.left_kills,
            match.ocr.header.right_kills,
            match.ocr.header.left_economy,
            match.ocr.header.right_economy,
            match.layout.left_color,
            match.layout.right_color,
            match.layout.winner_side,
            tuple(
                (
                    player.side,
                    player.slot,
                    player.normalized_name,
                    player.stats.kills,
                    player.stats.deaths,
                    player.stats.assists,
                    player.stats.economy,
                )
                for player in populated
            ),
            tuple((hero.side, hero.slot, hero.label) for hero in match.heroes),
        )
        if fingerprint in seen and match.stats_eligible:
            match = replace(
                match, stats_eligible=False, stats_exclusion_reason='duplicate'
            )
        elif match.stats_eligible:
            seen.add(fingerprint)
        result.append(match)
    return tuple(result)


def collapse_result_hits(
    hits: Sequence[ResultHit], *, maximum_gap_ms: int = 1_000
) -> Tuple[ResultHit, ...]:
    if maximum_gap_ms < 0:
        raise ValueError('maximum result gap must not be negative')
    strongest_by_time: Dict[int, ResultHit] = {}
    for hit in hits:
        previous = strongest_by_time.get(hit.at_ms)
        if previous is None or hit.layout.confidence > previous.layout.confidence:
            strongest_by_time[hit.at_ms] = hit
    ordered = sorted(strongest_by_time.values(), key=lambda hit: hit.at_ms)
    groups: List[List[ResultHit]] = []
    for hit in ordered:
        if not groups or hit.at_ms - groups[-1][-1].at_ms > maximum_gap_ms:
            groups.append([hit])
        else:
            groups[-1].append(hit)
    result: List[ResultHit] = []
    for group in groups:
        middle = group[len(group) // 2]
        strongest = max(group, key=lambda hit: hit.layout.confidence)
        result.append(
            ResultHit(
                at_ms=middle.at_ms,
                layout=strongest.layout,
                view_context=strongest.view_context,
                hero_lineup=strongest.hero_lineup,
            )
        )
    return tuple(result)


def _result_refinement_windows(
    hits: Sequence[ResultHit], *, outer_window: ScanWindow, padding_ms: int = 2_000
) -> Tuple[ScanWindow, ...]:
    candidates = collapse_result_hits(hits, maximum_gap_ms=5_000)
    windows = [
        ScanWindow(
            start_ms=max(outer_window.start_ms, hit.at_ms - padding_ms),
            end_ms=min(outer_window.end_ms, hit.at_ms + padding_ms),
        )
        for hit in candidates
    ]
    merged: List[ScanWindow] = []
    for window in windows:
        if window.end_ms <= window.start_ms:
            continue
        if not merged or window.start_ms > merged[-1].end_ms:
            merged.append(window)
            continue
        previous = merged[-1]
        merged[-1] = ScanWindow(
            start_ms=previous.start_ms, end_ms=max(previous.end_ms, window.end_ms)
        )
    return tuple(merged)


def _focused_result_window(
    window: ScanWindow, *, before_ms: int = 5_000, after_ms: int = 25_000
) -> ScanWindow:
    if window.focus_ms is None:
        return window
    start_ms = max(window.start_ms, window.focus_ms - before_ms)
    end_ms = min(window.end_ms, window.focus_ms + after_ms)
    if end_ms <= start_ms:
        return window
    return replace(window, start_ms=start_ms, end_ms=end_ms)


def _remaining_result_windows(
    outer: ScanWindow, focused: ScanWindow
) -> Tuple[ScanWindow, ...]:
    windows: List[ScanWindow] = []
    if outer.start_ms < focused.start_ms:
        windows.append(replace(outer, end_ms=focused.start_ms))
    if focused.end_ms < outer.end_ms:
        windows.append(replace(outer, start_ms=focused.end_ms))
    return tuple(windows)


def _visual_state_signature(frame: RgbFrame) -> str:
    return _visual_region_hash(frame, 0.05, 0.15, 0.95, 0.90) + _visual_region_hash(
        frame, 0.0, 0.62, 1.0, 0.95
    )


def _visual_region_hash(
    frame: RgbFrame, left: float, top: float, right: float, bottom: float
) -> str:
    rect = frame.relative_rect(left, top, right, bottom)
    width = rect.right - rect.left
    height = rect.bottom - rect.top
    rows: List[Tuple[int, ...]] = []
    for row in range(8):
        y = rect.top + min(height - 1, (row * 2 + 1) * height // 16)
        grayscale_values: List[int] = []
        for column in range(9):
            x = rect.left + min(width - 1, (column * 2 + 1) * width // 18)
            offset = (y * frame.width + x) * 3
            red, green, blue = frame.pixels[offset : offset + 3]
            grayscale_values.append((red * 3 + green * 6 + blue) // 10)
        rows.append(tuple(grayscale_values))
    bits = 0
    for row_values in rows:
        for column in range(8):
            bits <<= 1
            if row_values[column] < row_values[column + 1]:
                bits |= 1
    return '{:016x}'.format(bits)


def collapse_analyzed_matches(
    matches: Sequence[AnalyzedMatch], *, maximum_start_gap_ms: int = 90_000
) -> Tuple[AnalyzedMatch, ...]:
    if maximum_start_gap_ms < 0:
        raise ValueError('maximum game start gap must not be negative')
    groups: List[Tuple[Optional[int], AnalyzedMatch]] = []
    for match in sorted(matches, key=lambda item: item.result_at_ms):
        duration = match.ocr.header.duration_seconds
        if duration is None:
            groups.append((None, match))
            continue
        estimated_start = max(0, match.result_at_ms - duration * 1_000)
        duplicate_index = next(
            (
                index
                for index, (group_start, _) in enumerate(groups)
                if group_start is not None
                and abs(group_start - estimated_start) <= maximum_start_gap_ms
            ),
            None,
        )
        if duplicate_index is None:
            groups.append((estimated_start, match))
            continue
        group_start, previous = groups[duplicate_index]
        if match.result_at_ms >= previous.result_at_ms:
            groups[duplicate_index] = (group_start, match)
    return tuple(
        match for _, match in sorted(groups, key=lambda item: item[1].result_at_ms)
    )


class VaingloryVideoAnalyzer:
    _RESULT_FALLBACK_INTERVAL_MS = 120_000
    _HUD_CONTINUITY_MS = 20_000
    _HUD_CONTINUITY_LEVELS_MS = (20_000, 75_000, 150_000, 300_000)
    _MAX_RESULT_WINDOWS_PER_HOUR = 60
    _MIN_RESULT_WINDOWS_BEFORE_RELAXING = 30
    _COMPACT_FINE_WINDOW_MS = 12_000

    def __init__(
        self,
        *,
        sampler: Optional[FfmpegSampler] = None,
        result_reader: Optional[ResultReader] = None,
        hero_recognizer: Optional[SiftHeroRecognizer] = None,
        aram_detector: Optional[AramDetector] = None,
        result_panel_detector: Optional[ResultPanelDetector] = None,
        stage_classifier: Optional[StageClassifier] = None,
        minimum_match_seconds: int = 60,
    ) -> None:
        if minimum_match_seconds < 0:
            raise ValueError('minimum match duration must not be negative')
        self._sampler = sampler or FfmpegSampler()
        self._result_reader = result_reader or TesseractResultReader()
        self._hero_recognizer = hero_recognizer
        self._aram_detector = aram_detector or AramTalentSelectionDetector()
        self._result_panel_detector = result_panel_detector
        self._stage_classifier = stage_classifier
        self._minimum_match_seconds = minimum_match_seconds

    def analyze_part(
        self,
        part: VideoPart,
        *,
        progress: Optional[Callable[[float], None]] = None,
        status_callback: Optional[Callable[[AnalysisStatus], None]] = None,
        cancelled: Optional[Callable[[], bool]] = None,
    ) -> Tuple[AnalyzedMatch, ...]:
        def scan_progress(value: float) -> None:
            if progress is not None:
                progress(value * 0.7)

        def recognition_progress(value: float) -> None:
            if progress is not None:
                progress(0.7 + value * 0.3)

        scanned = self.scan_part(
            part,
            progress=scan_progress,
            status_callback=status_callback,
            cancelled=cancelled,
        )
        return self.recognize_scanned_part(
            part,
            scanned,
            progress=recognition_progress,
            status_callback=status_callback,
            cancelled=cancelled,
        )

    def recognize_candidate(
        self,
        part: VideoPart,
        *,
        at_ms: int,
        view_context: Literal['played', 'observed', 'unknown'] = 'unknown',
        cancelled: Optional[Callable[[], bool]] = None,
    ) -> Tuple[AnalyzedMatch, ...]:
        profile = self._sampler.probe(part.path)
        candidate_at_ms = max(0, min(int(at_ms), max(0, profile.duration_ms - 1)))
        return self.recognize_scanned_part(
            part,
            ScannedPart(
                video_duration_ms=profile.duration_ms,
                candidate_times_ms=(candidate_at_ms,),
                candidate_view_contexts=(view_context,),
                candidate_hero_lineups=((),),
            ),
            cancelled=cancelled,
        )

    def scan_part(
        self,
        part: VideoPart,
        *,
        progress: Optional[Callable[[float], None]] = None,
        status_callback: Optional[Callable[[AnalysisStatus], None]] = None,
        cancelled: Optional[Callable[[], bool]] = None,
    ) -> ScannedPart:
        scan_started = time.monotonic()
        probe_started = time.monotonic()
        profile = self._sampler.probe(part.path)
        probe_seconds = time.monotonic() - probe_started
        logger.info(
            'Vainglory analysis started: part_id={} part_index={} '
            'size={}x{} aspect={:.4f} duration_ms={}',
            part.id,
            part.index,
            profile.width,
            profile.height,
            profile.width / profile.height,
            profile.duration_ms,
        )
        self._emit_status(
            status_callback,
            AnalysisStatus(
                stage='probing',
                detail='视频信息读取完成，准备开始粗扫',
                elapsed_seconds=time.monotonic() - scan_started,
            ),
        )
        coarse_started = time.monotonic()
        observations: List[CoarseObservation] = []
        previous_gameplay: Optional[CoarseObservation] = None
        previous_probe_had_hud = False
        segment_lineup: Tuple[str, ...] = ()
        lineup_probe_attempts = 0
        last_result_fallback_ms = -self._RESULT_FALLBACK_INTERVAL_MS
        result_fallback_probes = 0
        lineup_probes = 0
        lineup_recognized_slots = 0
        lineup_seconds = 0.0
        hud_detection_seconds = 0.0
        result_fallback_seconds = 0.0
        played_hud_hits = 0
        observer_hud_hits = 0
        gameplay_runs = 0
        coarse_result_hits = 0
        transition_result_probes = 0
        gameplay_viewport: Optional[Tuple[float, float, float, float]] = None
        next_viewport_probe_ms = 0
        next_coarse_report = 0.1
        logger.info(
            'Vainglory coarse scan started: part_id={} duration_ms={}',
            part.id,
            profile.duration_ms,
        )
        self._emit_status(
            status_callback,
            AnalysisStatus(
                stage='coarse_scan',
                detail='开始粗扫整段视频',
                elapsed_seconds=time.monotonic() - scan_started,
            ),
        )
        for timed in self._sampler.coarse_frames(part.path):
            self._raise_if_cancelled(cancelled)
            hud_started = time.monotonic()
            hud_frame = timed.frame
            if gameplay_viewport is not None and gameplay_viewport != (
                0.0,
                0.0,
                1.0,
                1.0,
            ):
                hud_frame = timed.frame.crop(
                    timed.frame.relative_rect(*gameplay_viewport)
                )
            hud, view_context = _detect_hud_context(hud_frame)
            if hud is None and gameplay_viewport not in (None, (0.0, 0.0, 1.0, 1.0)):
                hud, view_context = _detect_hud_context(timed.frame)
                if hud is not None:
                    gameplay_viewport = (0.0, 0.0, 1.0, 1.0)
                    hud_frame = timed.frame
            if hud is None and timed.at_ms >= next_viewport_probe_ms:
                next_viewport_probe_ms = timed.at_ms + 30_000
                rect = detect_active_content_rect(timed.frame)
                if rect != PixelRect(0, 0, timed.frame.width, timed.frame.height):
                    candidate = timed.frame.crop(rect)
                    hud, view_context = _detect_hud_context(candidate)
                    if hud is not None:
                        gameplay_viewport = (
                            rect.left / timed.frame.width,
                            rect.top / timed.frame.height,
                            rect.right / timed.frame.width,
                            rect.bottom / timed.frame.height,
                        )
                        hud_frame = candidate
                        logger.info(
                            'Vainglory gameplay viewport detected: part_id={} '
                            'at_ms={} source={}x{} rect=({}, {}, {}, {})',
                            part.id,
                            timed.at_ms,
                            timed.frame.width,
                            timed.frame.height,
                            rect.left,
                            rect.top,
                            rect.right,
                            rect.bottom,
                        )
            if hud is not None and gameplay_viewport is None:
                gameplay_viewport = (0.0, 0.0, 1.0, 1.0)
            hud_detection_seconds += time.monotonic() - hud_started

            result_visible = False
            suspicious_hud_return = (
                hud is not None
                and not previous_probe_had_hud
                and previous_gameplay is not None
                and previous_gameplay.team_size is not None
                and previous_gameplay.team_size != hud.team_size
            )
            if suspicious_hud_return:
                transition_result_probes += 1
                result_fallback_started = time.monotonic()
                result_visible = self._detect_result_layout(timed.frame) is not None
                result_fallback_seconds += time.monotonic() - result_fallback_started
                if result_visible:
                    hud = None
                    view_context = 'unknown'
            new_hud_segment = hud is not None and (
                previous_gameplay is None
                or previous_gameplay.view_context != view_context
                or (
                    not previous_probe_had_hud
                    and timed.at_ms - previous_gameplay.at_ms > self._HUD_CONTINUITY_MS
                )
            )
            if new_hud_segment:
                segment_lineup = ()
                lineup_probe_attempts = 0
            if (
                hud is not None
                and view_context == 'played'
                and self._hero_recognizer is not None
                and lineup_probe_attempts < 2
                and sum(bool(label) for label in segment_lineup) < hud.team_size * 2
            ):
                lineup_started = time.monotonic()
                lineup_probes += 1
                lineup_probe_attempts += 1
                recognized_lineup = self._recognize_coarse_hud_lineup(
                    part.path,
                    timed.at_ms,
                    team_size=hud.team_size,
                    viewport=gameplay_viewport,
                )
                lineup_seconds += time.monotonic() - lineup_started
                lineup_recognized_slots += sum(
                    bool(label) for label in recognized_lineup
                )
                segment_lineup = self._merge_hud_lineups(
                    segment_lineup, recognized_lineup
                )
            if (
                not result_visible
                and hud is None
                and timed.at_ms - last_result_fallback_ms
                >= self._RESULT_FALLBACK_INTERVAL_MS
            ):
                last_result_fallback_ms = timed.at_ms
                result_fallback_probes += 1
                result_fallback_started = time.monotonic()
                result_visible = self._detect_result_layout(timed.frame) is not None
                result_fallback_seconds += time.monotonic() - result_fallback_started
            observation = CoarseObservation(
                at_ms=timed.at_ms,
                hud_signature=None if hud is None else hud.signature,
                result_visible=result_visible,
                team_size=None if hud is None else hud.team_size,
                visible_portraits=0 if hud is None else hud.visible_portraits,
                view_context=view_context,
                hero_lineup=segment_lineup if hud is not None else (),
                scene_signature=_visual_state_signature(timed.frame),
            )
            similarity = (
                None
                if previous_gameplay is None or hud is None
                else hud_lineup_similarity(
                    previous_gameplay.hud_signature or '', hud.signature
                )
            )
            logger.debug(
                'Vainglory HUD probe: part_id={} at_ms={} context={} '
                'team_size={} visible_portraits={} recognized_heroes={} '
                'lineup_similarity={} result_fallback={} result_visible={}',
                part.id,
                timed.at_ms,
                view_context,
                observation.team_size,
                observation.visible_portraits,
                tuple(label for label in observation.hero_lineup if label),
                None if similarity is None else round(similarity, 4),
                hud is None and timed.at_ms == last_result_fallback_ms,
                result_visible,
            )
            gameplay_run_started = hud is not None and (
                previous_gameplay is None
                or not same_gameplay_run(
                    previous_gameplay,
                    observation,
                    maximum_gap_ms=self._HUD_CONTINUITY_MS,
                )
            )
            if gameplay_run_started:
                gameplay_runs += 1
                logger.info(
                    'Vainglory gameplay run started: part_id={} at_ms={} '
                    'context={} team_size={} recognized_heroes={} '
                    'previous_at_ms={} lineup_similarity={}',
                    part.id,
                    timed.at_ms,
                    view_context,
                    observation.team_size,
                    tuple(label for label in observation.hero_lineup if label),
                    None if previous_gameplay is None else previous_gameplay.at_ms,
                    None if similarity is None else round(similarity, 4),
                )
            observations.append(observation)
            if hud is not None:
                previous_gameplay = observation
                if view_context == 'played':
                    played_hud_hits += 1
                else:
                    observer_hud_hits += 1
            if result_visible:
                coarse_result_hits += 1
            previous_probe_had_hud = hud is not None
            coarse_ratio = min(1.0, timed.at_ms / max(1, profile.duration_ms))
            if coarse_ratio >= next_coarse_report:
                logger.info(
                    'Vainglory coarse scan progress: part_id={} progress={:.0%} '
                    'media_at_ms={} frames={} hud_hits={} observer_hits={} '
                    'gameplay_runs={} result_hits={} elapsed_seconds={:.3f}',
                    part.id,
                    coarse_ratio,
                    timed.at_ms,
                    len(observations),
                    played_hud_hits,
                    observer_hud_hits,
                    gameplay_runs,
                    coarse_result_hits,
                    time.monotonic() - coarse_started,
                )
                self._emit_status(
                    status_callback,
                    AnalysisStatus(
                        stage='coarse_scan',
                        detail='粗扫已完成约 {:.0%}'.format(coarse_ratio),
                        elapsed_seconds=time.monotonic() - scan_started,
                        coarse_frames=len(observations),
                        gameplay_runs=gameplay_runs,
                    ),
                )
                while next_coarse_report <= coarse_ratio:
                    next_coarse_report += 0.1
            if progress is not None:
                progress(min(0.6, timed.at_ms / profile.duration_ms * 0.6))

        coarse_seconds = time.monotonic() - coarse_started
        windows = result_search_windows(
            observations,
            duration_ms=profile.duration_ms,
            hud_gap_ms=self._HUD_CONTINUITY_MS,
            before_end_ms=5_000,
        )
        primary_window_count = len(windows)
        maximum_expected_windows = max(
            self._MIN_RESULT_WINDOWS_BEFORE_RELAXING,
            (profile.duration_ms * self._MAX_RESULT_WINDOWS_PER_HOUR + 3_600_000 - 1)
            // 3_600_000,
        )
        selected_hud_gap_ms = self._HUD_CONTINUITY_MS
        window_attempts = [(selected_hud_gap_ms, primary_window_count)]
        if primary_window_count > maximum_expected_windows:
            for hud_gap_ms in self._HUD_CONTINUITY_LEVELS_MS[1:]:
                candidate_windows = result_search_windows(
                    observations,
                    duration_ms=profile.duration_ms,
                    hud_gap_ms=hud_gap_ms,
                    before_end_ms=5_000,
                )
                window_attempts.append((hud_gap_ms, len(candidate_windows)))
                logger.warning(
                    'Vainglory noisy HUD transition level evaluated: part_id={} '
                    'duration_ms={} gap_ms={} windows={} threshold={}',
                    part.id,
                    profile.duration_ms,
                    hud_gap_ms,
                    len(candidate_windows),
                    maximum_expected_windows,
                )
                if len(candidate_windows) < len(windows):
                    windows = candidate_windows
                    selected_hud_gap_ms = hud_gap_ms
                if len(windows) <= maximum_expected_windows:
                    break
        relaxed_hud_continuity = selected_hud_gap_ms != self._HUD_CONTINUITY_MS
        if relaxed_hud_continuity:
            logger.warning(
                'Vainglory noisy HUD transitions compacted: part_id={} '
                'duration_ms={} primary_windows={} threshold={} '
                'selected_gap_ms={} selected_windows={} attempts={}',
                part.id,
                profile.duration_ms,
                primary_window_count,
                maximum_expected_windows,
                selected_hud_gap_ms,
                len(windows),
                tuple(window_attempts),
            )
        logger.info(
            'Vainglory coarse scan completed: part_id={} frames={} hud_hits={} '
            'observer_hits={} lineup_probes={} lineup_recognized_slots={} '
            'hud_detection_seconds={:.3f} lineup_seconds={:.3f} '
            'result_fallback_probes={} result_fallback_seconds={:.3f} '
            'transition_result_probes={} result_hits={} primary_windows={} '
            'windows={} selected_hud_gap_ms={} window_attempts={} '
            'elapsed_seconds={:.3f}',
            part.id,
            len(observations),
            sum(
                item.hud_signature is not None and item.view_context == 'played'
                for item in observations
            ),
            sum(item.view_context == 'observed' for item in observations),
            lineup_probes,
            lineup_recognized_slots,
            hud_detection_seconds,
            lineup_seconds,
            result_fallback_probes,
            result_fallback_seconds,
            transition_result_probes,
            sum(item.result_visible for item in observations),
            primary_window_count,
            len(windows),
            selected_hud_gap_ms,
            tuple(window_attempts),
            coarse_seconds,
        )
        self._emit_status(
            status_callback,
            AnalysisStatus(
                stage='coarse_scan',
                detail=(
                    '粗扫完成：发现 {} 个游戏片段，生成 {} 个疑似结算区间'.format(
                        gameplay_runs, len(windows)
                    )
                    if not relaxed_hud_continuity
                    else '粗扫完成：噪声区间已从 {} 个合并为 {} 个'.format(
                        primary_window_count, len(windows)
                    )
                ),
                elapsed_seconds=time.monotonic() - scan_started,
                coarse_frames=len(observations),
                gameplay_runs=gameplay_runs,
                result_windows=len(windows),
                total_windows=len(windows),
            ),
        )
        logger.debug(
            'Vainglory result search windows: part_id={} windows={}',
            part.id,
            tuple(
                (window.start_ms, window.end_ms, window.view_context)
                for window in windows
            ),
        )
        hits: List[ResultHit] = []
        keyframe_preview_frames = 0
        fallback_preview_frames = 0
        refinement_frames = 0
        refinement_windows = 0
        expanded_fallbacks = 0
        total_window_ms = sum(window.end_ms - window.start_ms for window in windows)
        scanned_window_ms = 0
        fine_started = time.monotonic()
        logger.info(
            'Vainglory fine scan started: part_id={} windows={} '
            'search_duration_ms={} elapsed_seconds={:.3f}',
            part.id,
            len(windows),
            total_window_ms,
            time.monotonic() - scan_started,
        )
        self._emit_status(
            status_callback,
            AnalysisStatus(
                stage='fine_scan',
                detail='开始精扫 {} 个疑似结算区间'.format(len(windows)),
                elapsed_seconds=time.monotonic() - scan_started,
                coarse_frames=len(observations),
                gameplay_runs=gameplay_runs,
                result_windows=len(windows),
                total_windows=len(windows),
            ),
        )
        for window_index, window in enumerate(windows, 1):
            self._raise_if_cancelled(cancelled)
            window_started = time.monotonic()
            logger.info(
                'Vainglory fine scan window started: part_id={} window={}/{} '
                'start_ms={} end_ms={} duration_ms={} candidates_so_far={} '
                'elapsed_seconds={:.3f}',
                part.id,
                window_index,
                len(windows),
                window.start_ms,
                window.end_ms,
                window.end_ms - window.start_ms,
                len(collapse_result_hits(hits)),
                time.monotonic() - scan_started,
            )

            def report_window_step(detail: str) -> None:
                self._emit_status(
                    status_callback,
                    AnalysisStatus(
                        stage='fine_scan',
                        detail=detail,
                        elapsed_seconds=time.monotonic() - scan_started,
                        coarse_frames=len(observations),
                        gameplay_runs=gameplay_runs,
                        result_windows=len(windows),
                        current_window=window_index,
                        total_windows=len(windows),
                        candidate_count=len(collapse_result_hits(hits)),
                    ),
                )

            report_window_step(
                '正在精扫第 {}/{} 个疑似结算区间'.format(window_index, len(windows))
            )
            scanned = self._scan_window(
                part.path,
                window,
                part_id=part.id,
                window_index=window_index,
                window_count=len(windows),
                status=report_window_step,
                cancelled=cancelled,
            )
            hits.extend(
                replace(
                    hit,
                    view_context=window.view_context,
                    hero_lineup=window.hero_lineup,
                )
                for hit in scanned.hits
            )
            keyframe_preview_frames += scanned.keyframe_preview_frames
            fallback_preview_frames += scanned.fallback_preview_frames
            refinement_frames += scanned.refinement_frames
            refinement_windows += scanned.refinement_windows
            expanded_fallbacks += int(scanned.expanded_fallback)
            scanned_window_ms += window.end_ms - window.start_ms
            candidates_so_far = len(collapse_result_hits(hits))
            logger.info(
                'Vainglory fine scan window completed: part_id={} window={}/{} '
                'hits={} candidates_so_far={} keyframe_preview_frames={} '
                'fallback_preview_frames={} refinement_windows={} '
                'refinement_frames={} expanded_fallback={} '
                'window_seconds={:.3f} '
                'elapsed_seconds={:.3f}',
                part.id,
                window_index,
                len(windows),
                len(scanned.hits),
                candidates_so_far,
                scanned.keyframe_preview_frames,
                scanned.fallback_preview_frames,
                scanned.refinement_windows,
                scanned.refinement_frames,
                scanned.expanded_fallback,
                time.monotonic() - window_started,
                time.monotonic() - scan_started,
            )
            self._emit_status(
                status_callback,
                AnalysisStatus(
                    stage='fine_scan',
                    detail='第 {}/{} 个区间完成，累计发现 {} 个结算候选'.format(
                        window_index, len(windows), candidates_so_far
                    ),
                    elapsed_seconds=time.monotonic() - scan_started,
                    coarse_frames=len(observations),
                    gameplay_runs=gameplay_runs,
                    result_windows=len(windows),
                    current_window=window_index,
                    total_windows=len(windows),
                    candidate_count=candidates_so_far,
                ),
            )
            if progress is not None:
                fine_progress = scanned_window_ms / max(1, total_window_ms)
                progress(0.6 + fine_progress * 0.4)

        fine_seconds = time.monotonic() - fine_started
        candidates = collapse_result_hits(hits)
        candidate_entries: List[
            Tuple[int, Literal['played', 'observed', 'unknown'], Tuple[str, ...]]
        ] = [
            (candidate.at_ms, candidate.view_context, candidate.hero_lineup)
            for candidate in candidates
        ]
        for manual_at_ms in part.manual_candidate_times_ms:
            if manual_at_ms < 0 or manual_at_ms >= profile.duration_ms:
                logger.warning(
                    'Ignored out-of-range manual match marker: part_id={} '
                    'at_ms={} duration_ms={}',
                    part.id,
                    manual_at_ms,
                    profile.duration_ms,
                )
                continue
            nearby = min(
                (
                    (abs(at_ms - manual_at_ms), index)
                    for index, (at_ms, _context, _lineup) in enumerate(
                        candidate_entries
                    )
                    if abs(at_ms - manual_at_ms) <= 5_000
                ),
                default=None,
            )
            if nearby is None:
                candidate_entries.append((manual_at_ms, 'unknown', ()))
            else:
                _distance, index = nearby
                _at_ms, context, lineup = candidate_entries[index]
                candidate_entries[index] = (manual_at_ms, context, lineup)
        candidate_entries = sorted(
            {
                at_ms: (at_ms, context, lineup)
                for at_ms, context, lineup in candidate_entries
            }.values(),
            key=lambda item: item[0],
        )
        if part.manual_candidate_times_ms:
            logger.info(
                'Vainglory manual match markers merged: part_id={} markers={} '
                'candidates_before={} candidates_after={}',
                part.id,
                part.manual_candidate_times_ms,
                len(candidates),
                len(candidate_entries),
            )
        logger.info(
            'Vainglory fine scan completed: part_id={} windows={} hits={} '
            'candidates={} keyframe_preview_frames={} '
            'fallback_preview_frames={} refinement_windows={} '
            'refinement_frames={} expanded_fallbacks={} elapsed_seconds={:.3f}',
            part.id,
            len(windows),
            len(hits),
            len(candidates),
            keyframe_preview_frames,
            fallback_preview_frames,
            refinement_windows,
            refinement_frames,
            expanded_fallbacks,
            fine_seconds,
        )
        self._emit_status(
            status_callback,
            AnalysisStatus(
                stage='fine_scan',
                detail='精扫完成：{} 个区间得到 {} 个结算候选'.format(
                    len(windows), len(candidates)
                ),
                elapsed_seconds=time.monotonic() - scan_started,
                coarse_frames=len(observations),
                gameplay_runs=gameplay_runs,
                result_windows=len(windows),
                current_window=len(windows),
                total_windows=len(windows),
                candidate_count=len(candidates),
            ),
        )
        if progress is not None:
            progress(1.0)
        logger.info(
            'Vainglory video scan completed: part_id={} candidates={} '
            'probe_seconds={:.3f} coarse_seconds={:.3f} fine_seconds={:.3f} '
            'total_seconds={:.3f}',
            part.id,
            len(candidates),
            probe_seconds,
            coarse_seconds,
            fine_seconds,
            time.monotonic() - scan_started,
        )
        return ScannedPart(
            video_duration_ms=profile.duration_ms,
            candidate_times_ms=tuple(item[0] for item in candidate_entries),
            candidate_view_contexts=tuple(item[1] for item in candidate_entries),
            candidate_hero_lineups=tuple(item[2] for item in candidate_entries),
        )

    def scan_part_dense(
        self,
        part: VideoPart,
        *,
        progress: Optional[Callable[[float], None]] = None,
        cancelled: Optional[Callable[[], bool]] = None,
    ) -> DenseScanResult:
        scan_started = time.monotonic()
        probe_started = time.monotonic()
        profile = self._sampler.probe(part.path)
        probe_seconds = time.monotonic() - probe_started
        window = ScanWindow(start_ms=0, end_ms=profile.duration_ms)
        frames = iter(self._sampler.fine_frames(part.path, window, threads=6))
        hits: List[ResultHit] = []
        training_candidates: List[TrainingCandidate] = []
        borderline_result_candidates: List[TrainingCandidate] = []
        decoded_frames = 0
        decode_seconds = 0.0
        detection_seconds = 0.0
        next_progress = 0.01
        logger.info(
            'Vainglory dense scan started: part_id={} duration_ms={}',
            part.id,
            profile.duration_ms,
        )
        while True:
            self._raise_if_cancelled(cancelled)
            decode_started = time.monotonic()
            try:
                timed = next(frames)
            except StopIteration:
                decode_seconds += time.monotonic() - decode_started
                break
            decode_seconds += time.monotonic() - decode_started
            decoded_frames += 1
            detection_started = time.monotonic()
            layout = self._detect_result_layout(timed.frame)
            detection_seconds += time.monotonic() - detection_started
            if layout is not None:
                hits.append(ResultHit(at_ms=timed.at_ms, layout=layout))
                _remember_training_candidate(
                    training_candidates,
                    task='key_screen_review',
                    suggested_label='result_page',
                    at_ms=timed.at_ms,
                    segment_start_ms=max(0, timed.at_ms - 30_000),
                    frame=timed.frame,
                    model_version='result-detector-v1',
                    suggestion_confidence=layout.confidence,
                    stage_class='result_page',
                    stage_confidence=layout.confidence,
                    selection_reason=('worker 结算检测模型命中，保留为结算页复核候选'),
                    minimum_gap_ms=15_000,
                    maximum_per_label=12,
                )
                _remember_training_candidate(
                    training_candidates,
                    task='result_detector',
                    suggested_label='result_panel',
                    at_ms=timed.at_ms,
                    segment_start_ms=max(0, timed.at_ms - 30_000),
                    frame=timed.frame,
                    model_version='result-detector-v1',
                    suggestion_confidence=layout.confidence,
                    stage_class='result_page',
                    stage_confidence=layout.confidence,
                    selection_reason='worker 结算检测命中，预填结算面板框',
                    minimum_gap_ms=15_000,
                    maximum_per_label=12,
                    suggested_boxes=(_result_panel_training_box(timed.frame, layout),),
                )
                _remember_training_candidate(
                    borderline_result_candidates,
                    task='result_detector',
                    suggested_label='result_panel',
                    at_ms=timed.at_ms,
                    segment_start_ms=max(0, timed.at_ms - 30_000),
                    frame=timed.frame,
                    model_version='result-detector-v1',
                    suggestion_confidence=layout.confidence,
                    stage_class='result_page',
                    stage_confidence=layout.confidence,
                    selection_reason='worker 结算检测低置信边界命中，供人工复核',
                    minimum_gap_ms=15_000,
                    maximum_per_label=6,
                    suggested_boxes=(_result_panel_training_box(timed.frame, layout),),
                    prefer_lower_confidence=True,
                )
            ratio = min(1.0, timed.at_ms / max(1, profile.duration_ms))
            if ratio >= next_progress:
                if progress is not None:
                    progress(ratio)
                while next_progress <= ratio:
                    next_progress += 0.01
        if progress is not None:
            progress(1.0)

        candidates = list(collapse_result_hits(hits, maximum_gap_ms=5_000))
        candidate_times_ms = [candidate.at_ms for candidate in candidates]
        for manual_at_ms in part.manual_candidate_times_ms:
            if 0 <= manual_at_ms < profile.duration_ms and not any(
                abs(candidate_at_ms - manual_at_ms) <= 5_000
                for candidate_at_ms in candidate_times_ms
            ):
                candidate_times_ms.append(manual_at_ms)
        candidate_times_ms.sort()
        total_seconds = time.monotonic() - scan_started
        logger.info(
            'Vainglory dense scan completed: part_id={} frames={} '
            'result_frames={} candidates={} probe_seconds={:.3f} '
            'decode_seconds={:.3f} detection_seconds={:.3f} '
            'total_seconds={:.3f}',
            part.id,
            decoded_frames,
            len(hits),
            len(candidate_times_ms),
            probe_seconds,
            decode_seconds,
            detection_seconds,
            total_seconds,
        )
        scanned_part = ScannedPart(
            video_duration_ms=profile.duration_ms,
            candidate_times_ms=tuple(candidate_times_ms),
            candidate_view_contexts=tuple('unknown' for _ in candidate_times_ms),
            candidate_hero_lineups=tuple(() for _ in candidate_times_ms),
        )
        return DenseScanResult(
            scanned_part=scanned_part,
            decoded_frames=decoded_frames,
            result_frames=len(hits),
            probe_seconds=probe_seconds,
            decode_seconds=decode_seconds,
            detection_seconds=detection_seconds,
            total_seconds=total_seconds,
            training_candidates=tuple(
                (training_candidates + borderline_result_candidates)[:30]
            ),
        )

    def scan_part_cascade(
        self,
        part: VideoPart,
        *,
        progress: Optional[Callable[[float], None]] = None,
        status_callback: Optional[Callable[[AnalysisStatus], None]] = None,
        cancelled: Optional[Callable[[], bool]] = None,
        debug_dir: Optional[Path] = None,
    ) -> DenseScanResult:
        """分类粗扫 + 结算窗口推断 + 窗口内精扫的级联扫描。

        第一阶段以固定间隔全片跑 multi-v2 分类模型，构建阶段时间线；第二阶段
        基于对局段结束点推断结算画面窗口（不依赖结算帧本身，覆盖用户中途
        退出等无结算场景——空窗口在精扫后自然丢弃）；第三阶段仅在窗口内
        以 4 FPS 跑结算面板检测。

        传入 debug_dir 时会把逐帧预测记录为 JSONL，并把被判定为
        天赋选择(大乱斗信号)或模式为大乱斗的可疑帧截图保存，用于排查误判。
        """
        if self._stage_classifier is None:
            raise RuntimeError('级联扫描需要阶段分类模型')
        debug_writer: Optional[Any] = None
        if debug_dir is not None:
            debug_dir = Path(debug_dir).expanduser().resolve()
            debug_dir.mkdir(parents=True, exist_ok=True)
            debug_writer = (debug_dir / 'classify-observations.jsonl').open(
                'a', encoding='utf8'
            )
        try:
            return self._scan_part_cascade(
                part,
                progress=progress,
                status_callback=status_callback,
                cancelled=cancelled,
                debug_dir=debug_dir,
                debug_writer=debug_writer,
            )
        finally:
            if debug_writer is not None:
                debug_writer.close()

    def _probe_run_modes(
        self,
        path: str,
        observations: Sequence[ClassifiedObservation],
        *,
        cancelled: Optional[Callable[[], bool]] = None,
    ) -> Tuple[Dict[int, str], Tuple[TrainingCandidate, ...]]:
        """对每个对局段的开局窗口以 4 FPS 高帧率采样做模式探测。

        窗口覆盖"最后一张英雄卡片 ~ 进游戏后 20 秒"：大乱斗的天赋选择
        界面必然出现在该窗口（3v3/5v5 没有），多帧投票确认；选英雄界面
        大乱斗版与普通版画面不同，mode 头也是辅助信号。窗口内的零星
        噪声帧（单帧 talent/aram）达不到阈值，不会误判。
        """
        if self._stage_classifier is None:
            return {}, ()
        smoothed = smooth_stages(observations)
        anchors = _confirmed_anchors(_pre_match_anchors(smoothed), smoothed)
        segments = _segment_ranges(gameplay_runs(smoothed), anchors)
        result: Dict[int, str] = {}
        training_candidates: List[TrainingCandidate] = []
        stage_names = {
            STAGE_GAMEPLAY: 'gameplay',
            STAGE_SCOREBOARD: 'scoreboard',
            STAGE_RESULT_PAGE: 'result_page',
            STAGE_VICTORY_DEFEAT: 'victory_defeat',
            STAGE_PRE_MATCH: 'pre_match',
            STAGE_OUT_OF_MATCH: 'out_of_match',
            STAGE_TRANSITION: 'transition',
            STAGE_TALENT_SELECT: 'talent_select',
        }
        mode_names = {MODE_3V3: '3v3', MODE_ARAM: 'aram', MODE_5V5: '5v5'}
        mode_labels: Dict[int, TrainingCandidateLabel] = {
            MODE_3V3: 'bp_3v3',
            MODE_ARAM: 'bp_aram',
            MODE_5V5: 'bp_5v5',
        }
        for segment_start_ms, segment_end_ms in segments:
            pre_match_frames = tuple(
                item
                for item in smoothed
                if item.stage == STAGE_PRE_MATCH
                and segment_start_ms <= item.at_ms < segment_end_ms
            )
            if pre_match_frames:
                window_start = max(segment_start_ms, pre_match_frames[-1].at_ms - 5_000)
            else:
                window_start = segment_start_ms
            window_end = min(segment_end_ms, window_start + 25_000)
            if window_end <= window_start:
                continue
            talent_frames = 0
            five_frames = 0
            pre_match_modes: Counter[int] = Counter()
            probed_frames = 0
            pre_match_evidence: List[Tuple[TimedFrame, StagePrediction]] = []
            non_bp_evidence: List[Tuple[TimedFrame, StagePrediction]] = []

            def remember(
                bucket: List[Tuple[TimedFrame, StagePrediction]],
                timed: TimedFrame,
                prediction: StagePrediction,
                *,
                minimum_gap_ms: int,
                maximum: int,
            ) -> None:
                score = prediction.stage_conf + prediction.mode_conf
                if bucket and timed.at_ms - bucket[-1][0].at_ms < minimum_gap_ms:
                    previous = bucket[-1][1]
                    if score > previous.stage_conf + previous.mode_conf:
                        bucket[-1] = (timed, prediction)
                    return
                bucket.append((timed, prediction))
                del bucket[:-maximum]

            try:
                for timed in self._sampler.fine_frames(
                    path, ScanWindow(start_ms=window_start, end_ms=window_end)
                ):
                    self._raise_if_cancelled(cancelled)
                    prediction = self._stage_classifier.classify(timed.frame)
                    probed_frames += 1
                    if (
                        prediction.stage == STAGE_TALENT_SELECT
                        and prediction.mode == MODE_ARAM
                    ):
                        talent_frames += 1
                    if (
                        prediction.stage in (STAGE_SCOREBOARD, STAGE_RESULT_PAGE)
                        and prediction.mode == MODE_5V5
                    ):
                        five_frames += 1
                    if prediction.stage == STAGE_PRE_MATCH:
                        pre_match_modes[prediction.mode] += 1
                        remember(
                            pre_match_evidence,
                            timed,
                            prediction,
                            minimum_gap_ms=2_000,
                            maximum=2,
                        )
                    else:
                        remember(
                            non_bp_evidence,
                            timed,
                            prediction,
                            minimum_gap_ms=5_000,
                            maximum=1,
                        )
            except RuntimeError as error:
                logger.warning(
                    'Vainglory run opening probe skipped: start_ms={} error={!r}',
                    segment_start_ms,
                    error,
                )
                continue
            selected_evidence = pre_match_evidence + non_bp_evidence
            for timed, prediction in selected_evidence[:3]:
                likely_bp = prediction.stage == STAGE_PRE_MATCH
                suggested_label: TrainingCandidateLabel = (
                    mode_labels.get(prediction.mode, 'not_bp')
                    if likely_bp
                    else 'not_bp'
                )
                try:
                    image_jpeg = jpeg_bytes(timed.frame)
                except Exception as error:  # noqa: BLE001
                    logger.warning(
                        'Vainglory training candidate skipped: at_ms={} ' 'error={!r}',
                        timed.at_ms,
                        error,
                    )
                    continue
                training_candidates.append(
                    TrainingCandidate(
                        at_ms=int(timed.at_ms),
                        segment_start_ms=int(segment_start_ms),
                        image_jpeg=image_jpeg,
                        model_version='multi-v2',
                        suggested_label=suggested_label,
                        suggestion_confidence=(
                            min(prediction.stage_conf, prediction.mode_conf)
                            if likely_bp
                            else prediction.stage_conf
                        ),
                        stage_class=stage_names.get(
                            prediction.stage, str(prediction.stage)
                        ),
                        stage_confidence=float(prediction.stage_conf),
                        mode_class=mode_names.get(
                            prediction.mode, str(prediction.mode)
                        ),
                        mode_confidence=float(prediction.mode_conf),
                        selection_reason=(
                            'worker 开局探测识别为 pre_match，保留为 BP 复核候选'
                            if likely_bp
                            else 'worker 开局探测中的非 BP／可能漏检画面'
                        ),
                    )
                )
            if talent_frames >= 2:
                mode = 'aram'
            elif five_frames >= 2:
                mode = '5v5'
            else:
                mode = 'unknown'
                total_pre_match = sum(pre_match_modes.values())
                if total_pre_match >= 8:
                    dominant_mode, dominant_count = pre_match_modes.most_common(1)[0]
                    if dominant_count >= total_pre_match * 0.5:
                        mode = {
                            MODE_3V3: '3v3',
                            MODE_ARAM: 'aram',
                            MODE_5V5: '5v5',
                        }.get(dominant_mode, 'unknown')
            if mode != 'unknown':
                result[segment_start_ms] = mode
            logger.info(
                'Vainglory run opening mode probe: segment_start_ms={} '
                'window_start_ms={} window_end_ms={} frames={} '
                'talent_frames={} five_frames={} pre_match_modes={} mode={}',
                segment_start_ms,
                window_start,
                window_end,
                probed_frames,
                talent_frames,
                five_frames,
                dict(pre_match_modes),
                mode,
            )
        return result, tuple(training_candidates[:60])

    def _exit_regression(
        self,
        path: str,
        observations: Sequence[ClassifiedObservation],
        hits: List[ResultHit],
        *,
        cancelled: Optional[Callable[[], bool]] = None,
        training_candidates: Optional[List[TrainingCandidate]] = None,
    ) -> int:
        """退出信号回归扫描。

        对局中→游戏外/转场/非虚荣的切换意味着前面必然出现过结算画面
        （对局结束才能退出），但结算帧可能被误判为 gameplay（长时间空白，
        如 840 案例的 109 秒）。因此从退出信号帧一直往前扫到该局最近的
        选英雄界面锚点（无锚点则扫到对局段开始），以 4 FPS 跑结算面板
        检测，命中帧直接加入 hits。回归区间上限 30 分钟作为保险。
        """
        if self._result_panel_detector is None:
            return 0
        smoothed = smooth_stages(observations)
        anchors = _confirmed_anchors(_pre_match_anchors(smoothed), smoothed)
        runs = gameplay_runs(smoothed)
        in_match_stages = (STAGE_GAMEPLAY, STAGE_PRE_MATCH, STAGE_TALENT_SELECT)
        exit_frames = []
        for index, item in enumerate(observations):
            is_exit = (
                item.stage in (STAGE_OUT_OF_MATCH, STAGE_TRANSITION)
                or item.content == CONTENT_NOT_VAINGLORY
            )
            if not is_exit or index == 0:
                continue
            previous = observations[index - 1]
            if (
                previous.stage not in in_match_stages
                or previous.content == CONTENT_NOT_VAINGLORY
            ):
                continue
            exit_frames.append(item)
        if not exit_frames:
            return 0
        added = 0
        for exit_item in exit_frames:
            anchor_start = next(
                (
                    anchor[0].at_ms
                    for anchor in reversed(anchors)
                    if anchor[0].at_ms < exit_item.at_ms
                ),
                None,
            )
            has_confirmed_anchor = anchor_start is not None
            if anchor_start is None:
                run = next(
                    (run for run in reversed(runs) if run[0].at_ms <= exit_item.at_ms),
                    None,
                )
                anchor_start = run[0].at_ms if run is not None else 0
            scan_start = max(0, anchor_start, exit_item.at_ms - 30 * 60_000)
            scan_end = exit_item.at_ms
            if scan_end <= scan_start:
                continue
            if has_confirmed_anchor and any(
                scan_start <= hit.at_ms <= scan_end for hit in hits
            ):
                continue
            regression_hits = 0
            for timed in self._sampler.fine_frames(
                path, ScanWindow(start_ms=scan_start, end_ms=scan_end)
            ):
                self._raise_if_cancelled(cancelled)
                layout = self._detect_result_layout(timed.frame)
                if layout is not None:
                    hits.append(ResultHit(at_ms=timed.at_ms, layout=layout))
                    regression_hits += 1
                    if training_candidates is not None:
                        _remember_training_candidate(
                            training_candidates,
                            task='key_screen_review',
                            suggested_label='result_page',
                            at_ms=timed.at_ms,
                            segment_start_ms=scan_start,
                            frame=timed.frame,
                            model_version='result-detector-v1',
                            suggestion_confidence=layout.confidence,
                            stage_class='result_page',
                            stage_confidence=layout.confidence,
                            selection_reason=(
                                'worker 退出回归扫描命中，保留为结算页复核候选'
                            ),
                            minimum_gap_ms=15_000,
                            maximum_per_label=12,
                        )
            added += regression_hits
            logger.info(
                'Vainglory regression scan: exit_at_ms={} scan_start_ms={} '
                'scan_end_ms={} hits={}',
                exit_item.at_ms,
                scan_start,
                scan_end,
                regression_hits,
            )
        return added

    def _tail_regression(
        self,
        path: str,
        hits: List[ResultHit],
        segments: Sequence[Tuple[int, int]],
        anchors: Sequence[Tuple[ClassifiedObservation, ClassifiedObservation]],
        *,
        cancelled: Optional[Callable[[], bool]] = None,
        training_candidates: Optional[List[TrainingCandidate]] = None,
    ) -> int:
        """段尾窗口无命中时的自动回归。

        结算画面可能被误判为 gameplay（段尾位置被高估），段尾窗口
        （前推 40s）覆盖不到真实结算。此时从该段最近的锚点（无锚点则
        段起点）到段尾窗口起点做 4 FPS 结算面板检测，命中帧直接加入
        hits（由后续 collapse 统一处理）。
        """
        if self._result_panel_detector is None:
            return 0
        added = 0
        for seg_start, seg_end in segments:
            tail_start = seg_end - 40_000
            tail_end = seg_end + 25_000
            if any(tail_start <= hit.at_ms <= tail_end for hit in hits):
                continue
            anchor_ms = next(
                (
                    anchor[0].at_ms
                    for anchor in reversed(anchors)
                    if anchor[0].at_ms <= seg_start
                ),
                seg_start,
            )
            scan_start = max(seg_start, anchor_ms)
            scan_end = tail_start
            if scan_end <= scan_start:
                continue
            regression_hits = 0
            for timed in self._sampler.fine_frames(
                path, ScanWindow(start_ms=scan_start, end_ms=scan_end)
            ):
                self._raise_if_cancelled(cancelled)
                layout = self._detect_result_layout(timed.frame)
                if layout is not None:
                    hits.append(ResultHit(at_ms=timed.at_ms, layout=layout))
                    regression_hits += 1
                    if training_candidates is not None:
                        _remember_training_candidate(
                            training_candidates,
                            task='key_screen_review',
                            suggested_label='result_page',
                            at_ms=timed.at_ms,
                            segment_start_ms=scan_start,
                            frame=timed.frame,
                            model_version='result-detector-v1',
                            suggestion_confidence=layout.confidence,
                            stage_class='result_page',
                            stage_confidence=layout.confidence,
                            selection_reason=(
                                'worker 段尾回归扫描命中，保留为结算页复核候选'
                            ),
                            minimum_gap_ms=15_000,
                            maximum_per_label=12,
                        )
            added += regression_hits
            logger.info(
                'Vainglory tail regression scan: seg_start_ms={} seg_end_ms={} '
                'anchor_ms={} scan_start_ms={} scan_end_ms={} hits={}',
                seg_start,
                seg_end,
                anchor_ms,
                scan_start,
                scan_end,
                regression_hits,
            )
        return added

    def _scan_part_cascade(
        self,
        part: VideoPart,
        *,
        progress: Optional[Callable[[float], None]] = None,
        status_callback: Optional[Callable[[AnalysisStatus], None]] = None,
        cancelled: Optional[Callable[[], bool]] = None,
        debug_dir: Optional[Path] = None,
        debug_writer: Optional[Any] = None,
    ) -> DenseScanResult:
        assert self._stage_classifier is not None
        classifier = self._stage_classifier
        scan_started = time.monotonic()
        probe_started = time.monotonic()
        profile = self._sampler.probe(part.path)
        probe_seconds = time.monotonic() - probe_started
        classify_started = time.monotonic()
        observations: List[ClassifiedObservation] = []
        screen_state_candidates: List[TrainingCandidate] = []
        key_screen_candidates: List[TrainingCandidate] = []
        result_detector_candidates: List[TrainingCandidate] = []
        borderline_result_candidates: List[TrainingCandidate] = []
        mode_gate_candidates: List[TrainingCandidate] = []
        next_progress = 0.01
        next_status_progress = 0.1
        logger.info(
            'Vainglory cascade scan started: part_id={} duration_ms={}',
            part.id,
            profile.duration_ms,
        )
        self._emit_status(
            status_callback,
            AnalysisStatus(
                stage='coarse_scan',
                detail='开始每 5 秒取一帧分类粗扫整段视频',
                elapsed_seconds=time.monotonic() - scan_started,
            ),
        )
        for timed in self._sampler.classify_frames(part.path):
            self._raise_if_cancelled(cancelled)
            prediction = classifier.classify(timed.frame)
            observations.append(
                ClassifiedObservation(
                    at_ms=timed.at_ms,
                    stage=prediction.stage,
                    stage_conf=prediction.stage_conf,
                    mode=prediction.mode,
                    content=prediction.content,
                )
            )
            screen_state_label = _screen_state_candidate_label(prediction)
            _remember_training_candidate(
                screen_state_candidates,
                task='screen_state',
                suggested_label=screen_state_label,
                at_ms=timed.at_ms,
                segment_start_ms=max(0, timed.at_ms - 30_000),
                frame=timed.frame,
                model_version='multi-v2',
                suggestion_confidence=(
                    prediction.content_conf
                    if screen_state_label == 'not_vainglory'
                    else min(prediction.content_conf, prediction.stage_conf)
                ),
                stage_class=screen_state_label,
                stage_confidence=prediction.stage_conf,
                mode_class={MODE_3V3: '3v3', MODE_ARAM: 'aram', MODE_5V5: '5v5'}.get(
                    prediction.mode, str(prediction.mode)
                ),
                mode_confidence=prediction.mode_conf,
                selection_reason='worker 低频粗扫代表帧，供画面状态人工复核',
                minimum_gap_ms=60_000,
                maximum_per_label=_SCREEN_STATE_CANDIDATE_LIMITS[screen_state_label],
                separate_modes=screen_state_label == 'in_match',
            )
            key_screen_label: Optional[TrainingCandidateLabel] = None
            key_screen_reason = ''
            key_screen_limit = 0
            key_screen_gap_ms = 0
            if prediction.stage == STAGE_RESULT_PAGE:
                key_screen_label = 'result_page'
                key_screen_reason = (
                    'worker 分类粗扫识别为结算页，保留为关键画面复核候选'
                )
                key_screen_limit = 12
                key_screen_gap_ms = 20_000
            elif prediction.stage == STAGE_SCOREBOARD:
                key_screen_label = 'scoreboard'
                key_screen_reason = (
                    'worker 分类粗扫识别为计分板，保留为关键画面复核候选'
                )
                key_screen_limit = 12
                key_screen_gap_ms = 20_000
            elif prediction.stage == STAGE_VICTORY_DEFEAT:
                key_screen_label = 'other'
                key_screen_reason = (
                    '胜负动画容易与结算页混淆，保留为关键画面 hard negative'
                )
                key_screen_limit = 6
                key_screen_gap_ms = 60_000
            if key_screen_label is not None:
                _remember_training_candidate(
                    key_screen_candidates,
                    task='key_screen_review',
                    suggested_label=key_screen_label,
                    at_ms=timed.at_ms,
                    segment_start_ms=max(0, timed.at_ms - 30_000),
                    frame=timed.frame,
                    model_version='multi-v2',
                    suggestion_confidence=prediction.stage_conf,
                    stage_class={
                        STAGE_RESULT_PAGE: 'result_page',
                        STAGE_SCOREBOARD: 'scoreboard',
                        STAGE_VICTORY_DEFEAT: 'victory_defeat',
                    }.get(prediction.stage, str(prediction.stage)),
                    stage_confidence=prediction.stage_conf,
                    mode_class={
                        MODE_3V3: '3v3',
                        MODE_ARAM: 'aram',
                        MODE_5V5: '5v5',
                    }.get(prediction.mode, str(prediction.mode)),
                    mode_confidence=prediction.mode_conf,
                    selection_reason=key_screen_reason,
                    minimum_gap_ms=key_screen_gap_ms,
                    maximum_per_label=key_screen_limit,
                )
            if prediction.stage in (STAGE_SCOREBOARD, STAGE_VICTORY_DEFEAT):
                _remember_training_candidate(
                    result_detector_candidates,
                    task='result_detector',
                    suggested_label='no_result_panel',
                    at_ms=timed.at_ms,
                    segment_start_ms=max(0, timed.at_ms - 30_000),
                    frame=timed.frame,
                    model_version='multi-v2',
                    suggestion_confidence=prediction.stage_conf,
                    stage_class=(
                        'scoreboard'
                        if prediction.stage == STAGE_SCOREBOARD
                        else 'victory_defeat'
                    ),
                    stage_confidence=prediction.stage_conf,
                    mode_class={
                        MODE_3V3: '3v3',
                        MODE_ARAM: 'aram',
                        MODE_5V5: '5v5',
                    }.get(prediction.mode, str(prediction.mode)),
                    mode_confidence=prediction.mode_conf,
                    selection_reason=(
                        '计分板／胜负动画是结算检测器的重要 hard negative'
                    ),
                    minimum_gap_ms=60_000,
                    maximum_per_label=6,
                )
            if prediction.stage == STAGE_GAMEPLAY and prediction.mode in (
                MODE_3V3,
                MODE_ARAM,
            ):
                _remember_training_candidate(
                    mode_gate_candidates,
                    task='mode_gate',
                    suggested_label='no_evidence',
                    at_ms=timed.at_ms,
                    segment_start_ms=max(0, timed.at_ms - 30_000),
                    frame=timed.frame,
                    model_version='multi-v2',
                    suggestion_confidence=prediction.mode_conf,
                    stage_class='gameplay',
                    stage_confidence=prediction.stage_conf,
                    mode_class=('aram' if prediction.mode == MODE_ARAM else '3v3'),
                    mode_confidence=prediction.mode_conf,
                    selection_reason=(
                        '3V3／大乱斗游戏画面，检查是否拍到黄色光栅或开放入口'
                    ),
                    minimum_gap_ms=120_000,
                    maximum_per_label=6,
                )
            if debug_writer is not None:
                debug_writer.write(
                    json.dumps(
                        {
                            'at_ms': timed.at_ms,
                            'content': prediction.content,
                            'content_conf': round(prediction.content_conf, 4),
                            'stage': prediction.stage,
                            'stage_conf': round(prediction.stage_conf, 4),
                            'mode': prediction.mode,
                            'mode_conf': round(prediction.mode_conf, 4),
                        },
                        ensure_ascii=False,
                    )
                    + '\n'
                )
                debug_writer.flush()
                if (
                    prediction.stage == STAGE_TALENT_SELECT
                    or prediction.mode == MODE_ARAM
                ) and debug_dir is not None:
                    frame_path = debug_dir / 'suspect-{:09d}ms.png'.format(timed.at_ms)
                    frame_path.write_bytes(png_bytes(timed.frame))
                    logger.info(
                        'Vainglory cascade debug suspect frame saved: part_id={} '
                        'at_ms={} stage={} stage_conf={:.3f} mode={} mode_conf={:.3f} '
                        'path={}',
                        part.id,
                        timed.at_ms,
                        prediction.stage,
                        prediction.stage_conf,
                        prediction.mode,
                        prediction.mode_conf,
                        frame_path,
                    )
            ratio = min(1.0, timed.at_ms / max(1, profile.duration_ms))
            if ratio >= next_progress:
                if progress is not None:
                    progress(ratio * 0.6)
                while next_progress <= ratio:
                    next_progress += 0.01
            if ratio >= next_status_progress:
                self._emit_status(
                    status_callback,
                    AnalysisStatus(
                        stage='coarse_scan',
                        detail='分类粗扫 {}% · 已采样 {} 帧'.format(
                            min(100, round(ratio * 100)), len(observations)
                        ),
                        elapsed_seconds=time.monotonic() - scan_started,
                        coarse_frames=len(observations),
                    ),
                )
                while next_status_progress <= ratio:
                    next_status_progress += 0.1
        classify_seconds = time.monotonic() - classify_started
        self._emit_status(
            status_callback,
            AnalysisStatus(
                stage='coarse_scan',
                detail='分类采样完成，正在确认对局段与模式',
                elapsed_seconds=time.monotonic() - scan_started,
                coarse_frames=len(observations),
            ),
        )
        run_modes, training_candidates = self._probe_run_modes(
            part.path, observations, cancelled=cancelled
        )
        windows = list(
            build_classified_windows(
                observations, duration_ms=profile.duration_ms, run_modes=run_modes
            )
        )
        smoothed = smooth_stages(observations)
        anchor_segments = _segment_ranges(
            gameplay_runs(smoothed),
            _confirmed_anchors(_pre_match_anchors(smoothed), smoothed),
        )
        segment_anchors = _confirmed_anchors(_pre_match_anchors(smoothed), smoothed)
        logger.info(
            'Vainglory classify pass completed: part_id={} frames={} '
            'gameplay_frames={} result_signal_frames={} windows={} '
            'classify_seconds={:.3f}',
            part.id,
            len(observations),
            sum(item.stage == STAGE_GAMEPLAY for item in observations),
            sum(
                item.stage in (STAGE_RESULT_PAGE, STAGE_VICTORY_DEFEAT)
                for item in observations
            ),
            len(windows),
            classify_seconds,
        )
        self._emit_status(
            status_callback,
            AnalysisStatus(
                stage='coarse_scan',
                detail='分类完成：识别 {} 个对局段，生成 {} 个精扫区间'.format(
                    len(anchor_segments), len(windows)
                ),
                elapsed_seconds=time.monotonic() - scan_started,
                coarse_frames=len(observations),
                gameplay_runs=len(anchor_segments),
                result_windows=len(windows),
                total_windows=len(windows),
            ),
        )
        hits: List[ResultHit] = []
        decoded_frames = 0
        detection_seconds = 0.0
        scanned_window_ms = 0
        total_window_ms = sum(window.end_ms - window.start_ms for window in windows)
        fine_started = time.monotonic()
        logger.info(
            'Vainglory guided fine scan started: part_id={} windows={} '
            'search_duration_ms={}',
            part.id,
            len(windows),
            total_window_ms,
        )
        self._emit_status(
            status_callback,
            AnalysisStatus(
                stage='fine_scan',
                detail='开始用 4 FPS 精扫 {} 个结算区间'.format(len(windows)),
                elapsed_seconds=time.monotonic() - scan_started,
                coarse_frames=len(observations),
                gameplay_runs=len(anchor_segments),
                result_windows=len(windows),
                total_windows=len(windows),
            ),
        )
        for window_index, window in enumerate(windows, 1):
            self._raise_if_cancelled(cancelled)
            scan_window = ScanWindow(
                start_ms=window.start_ms, end_ms=window.end_ms, view_context='unknown'
            )
            for timed in self._sampler.fine_frames(part.path, scan_window):
                self._raise_if_cancelled(cancelled)
                decoded_frames += 1
                detection_started = time.monotonic()
                layout = self._detect_result_layout(timed.frame)
                detection_seconds += time.monotonic() - detection_started
                if layout is not None:
                    hits.append(ResultHit(at_ms=timed.at_ms, layout=layout))
                    _remember_training_candidate(
                        key_screen_candidates,
                        task='key_screen_review',
                        suggested_label='result_page',
                        at_ms=timed.at_ms,
                        segment_start_ms=window.start_ms,
                        frame=timed.frame,
                        model_version='result-detector-v1',
                        suggestion_confidence=layout.confidence,
                        stage_class='result_page',
                        stage_confidence=layout.confidence,
                        selection_reason=(
                            'worker 结算检测模型命中，保留为结算页复核候选'
                        ),
                        minimum_gap_ms=15_000,
                        maximum_per_label=12,
                    )
                    _remember_training_candidate(
                        result_detector_candidates,
                        task='result_detector',
                        suggested_label='result_panel',
                        at_ms=timed.at_ms,
                        segment_start_ms=window.start_ms,
                        frame=timed.frame,
                        model_version='result-detector-v1',
                        suggestion_confidence=layout.confidence,
                        stage_class='result_page',
                        stage_confidence=layout.confidence,
                        selection_reason='worker 检测命中，预填结算面板框供人工修正',
                        minimum_gap_ms=15_000,
                        maximum_per_label=8,
                        suggested_boxes=(
                            _result_panel_training_box(timed.frame, layout),
                        ),
                    )
                    _remember_training_candidate(
                        borderline_result_candidates,
                        task='result_detector',
                        suggested_label='result_panel',
                        at_ms=timed.at_ms,
                        segment_start_ms=window.start_ms,
                        frame=timed.frame,
                        model_version='result-detector-v1',
                        suggestion_confidence=layout.confidence,
                        stage_class='result_page',
                        stage_confidence=layout.confidence,
                        selection_reason=(
                            'worker 结算检测低置信边界命中，供人工复核'
                        ),
                        minimum_gap_ms=15_000,
                        maximum_per_label=6,
                        suggested_boxes=(
                            _result_panel_training_box(timed.frame, layout),
                        ),
                        prefer_lower_confidence=True,
                    )
            scanned_window_ms += window.end_ms - window.start_ms
            logger.info(
                'Vainglory guided fine scan window completed: part_id={} '
                'window={}/{} start_ms={} end_ms={} mode={} '
                'hits_so_far={}',
                part.id,
                window_index,
                len(windows),
                window.start_ms,
                window.end_ms,
                window.mode,
                len(collapse_result_hits(hits)),
            )
            if progress is not None:
                progress(0.6 + scanned_window_ms / max(1, total_window_ms) * 0.4)
            self._emit_status(
                status_callback,
                AnalysisStatus(
                    stage='fine_scan',
                    detail='精扫 {}/{} · 累计找到 {} 个结算画面'.format(
                        window_index, len(windows), len(collapse_result_hits(hits))
                    ),
                    elapsed_seconds=time.monotonic() - scan_started,
                    coarse_frames=len(observations),
                    gameplay_runs=len(anchor_segments),
                    result_windows=len(windows),
                    current_window=window_index,
                    total_windows=len(windows),
                    candidate_count=len(collapse_result_hits(hits)),
                ),
            )
        self._emit_status(
            status_callback,
            AnalysisStatus(
                stage='fine_scan',
                detail='区间精扫完成，正在做段尾与退出回归补漏',
                elapsed_seconds=time.monotonic() - scan_started,
                coarse_frames=len(observations),
                gameplay_runs=len(anchor_segments),
                result_windows=len(windows),
                current_window=len(windows),
                total_windows=len(windows),
                candidate_count=len(collapse_result_hits(hits)),
            ),
        )
        tail_regression_added = self._tail_regression(
            part.path,
            hits,
            anchor_segments,
            segment_anchors,
            cancelled=cancelled,
            training_candidates=key_screen_candidates,
        )
        if tail_regression_added:
            logger.info(
                'Vainglory tail regression added hits: part_id={} extra={}',
                part.id,
                tail_regression_added,
            )
        exit_regression_added = self._exit_regression(
            part.path,
            observations,
            hits,
            cancelled=cancelled,
            training_candidates=key_screen_candidates,
        )
        if exit_regression_added:
            logger.info(
                'Vainglory exit regression added hits: part_id={} extra={}',
                part.id,
                exit_regression_added,
            )
        fine_seconds = time.monotonic() - fine_started
        candidates = list(collapse_result_hits(hits, maximum_gap_ms=5_000))
        candidate_entries: List[Tuple[int, str]] = []
        for candidate in candidates:
            matching_windows = tuple(
                item
                for item in windows
                if item.start_ms <= candidate.at_ms < item.end_ms
            )
            matched_window = matching_windows[0] if matching_windows else None
            candidate_entries.append(
                (
                    candidate.at_ms,
                    'unknown' if matched_window is None else matched_window.mode,
                )
            )
        for manual_at_ms in part.manual_candidate_times_ms:
            if 0 <= manual_at_ms < profile.duration_ms and not any(
                abs(candidate_at_ms - manual_at_ms) <= 5_000
                for candidate_at_ms, _mode in candidate_entries
            ):
                candidate_entries.append((manual_at_ms, 'unknown'))
        candidate_entries.sort(key=lambda item: item[0])
        candidate_times_ms = [item[0] for item in candidate_entries]
        candidate_modes = [item[1] for item in candidate_entries]
        total_seconds = time.monotonic() - scan_started
        logger.info(
            'Vainglory cascade scan completed: part_id={} classify_frames={} '
            'decoded_frames={} result_frames={} windows={} candidates={} '
            'probe_seconds={:.3f} classify_seconds={:.3f} '
            'detection_seconds={:.3f} total_seconds={:.3f}',
            part.id,
            len(observations),
            decoded_frames,
            len(hits),
            len(windows),
            len(candidate_times_ms),
            probe_seconds,
            classify_seconds,
            detection_seconds,
            total_seconds,
        )
        self._emit_status(
            status_callback,
            AnalysisStatus(
                stage='fine_scan',
                detail='级联扫描完成：{} 个区间得到 {} 个结算候选'.format(
                    len(windows), len(candidate_times_ms)
                ),
                elapsed_seconds=total_seconds,
                coarse_frames=len(observations),
                gameplay_runs=len(anchor_segments),
                result_windows=len(windows),
                current_window=len(windows),
                total_windows=len(windows),
                candidate_count=len(candidate_times_ms),
            ),
        )
        scanned_part = ScannedPart(
            video_duration_ms=profile.duration_ms,
            candidate_times_ms=tuple(candidate_times_ms),
            candidate_view_contexts=tuple('unknown' for _ in candidate_times_ms),
            candidate_hero_lineups=tuple(() for _ in candidate_times_ms),
            candidate_modes=tuple(candidate_modes),
        )
        return DenseScanResult(
            scanned_part=scanned_part,
            decoded_frames=decoded_frames,
            result_frames=len(hits),
            probe_seconds=probe_seconds,
            decode_seconds=classify_seconds + fine_seconds,
            detection_seconds=detection_seconds,
            total_seconds=total_seconds,
            training_candidates=tuple(
                _selected_screen_state_candidates(screen_state_candidates)
                + [
                    candidate
                    for label in ('bp_3v3', 'bp_aram', 'bp_5v5', 'not_bp')
                    for candidate in [
                        item
                        for item in training_candidates
                        if item.suggested_label == label
                    ][:5]
                ]
                + [
                    candidate
                    for label, maximum in (
                        ('result_page', 3),
                        ('scoreboard', 3),
                        ('other', 2),
                    )
                    for candidate in [
                        item
                        for item in key_screen_candidates
                        if item.suggested_label == label
                    ][:maximum]
                ]
                + [
                    candidate
                    for label, maximum in (('result_panel', 4), ('no_result_panel', 3))
                    for candidate in [
                        item
                        for item in result_detector_candidates
                        if item.suggested_label == label
                    ][:maximum]
                ]
                + list(borderline_result_candidates[:3])
                + list(mode_gate_candidates[:6])
            )[:80],
        )

    def recognize_scanned_part(
        self,
        part: VideoPart,
        scanned: ScannedPart,
        *,
        progress: Optional[Callable[[float], None]] = None,
        status_callback: Optional[Callable[[AnalysisStatus], None]] = None,
        cancelled: Optional[Callable[[], bool]] = None,
        debug_dir: Optional[Path] = None,
    ) -> Tuple[AnalyzedMatch, ...]:
        recognition_run_started = time.monotonic()
        candidates = scanned.candidate_times_ms
        candidate_contexts = (
            scanned.candidate_view_contexts
            if len(scanned.candidate_view_contexts) == len(candidates)
            else tuple('unknown' for _ in candidates)
        )
        candidate_lineups = (
            scanned.candidate_hero_lineups
            if len(scanned.candidate_hero_lineups) == len(candidates)
            else tuple(() for _ in candidates)
        )
        candidate_modes = (
            scanned.candidate_modes
            if len(scanned.candidate_modes) == len(candidates)
            else tuple('' for _ in candidates)
        )
        matches: List[AnalyzedMatch] = []
        rejected_layout = 0
        rejected_header = 0
        rejected_short = 0
        rejected_evidence = 0
        rejected_lineup = 0
        candidate_started = time.monotonic()
        candidate_frame_seconds = 0.0
        header_ocr_seconds = 0.0
        nearby_frame_seconds = 0.0
        match_recognition_seconds = 0.0
        logger.info(
            'Vainglory recognition started: part_id={} candidates={} '
            'video_duration_ms={}',
            part.id,
            len(candidates),
            scanned.video_duration_ms,
        )
        self._emit_status(
            status_callback,
            AnalysisStatus(
                stage='ocr_recognition',
                detail='开始逐个验证 {} 个结算候选'.format(len(candidates)),
                elapsed_seconds=0.0,
                candidate_count=len(candidates),
                total_candidates=len(candidates),
            ),
        )
        for index, (
            candidate_at_ms,
            candidate_view_context,
            candidate_lineup,
            candidate_mode,
        ) in enumerate(
            zip(candidates, candidate_contexts, candidate_lineups, candidate_modes)
        ):
            self._raise_if_cancelled(cancelled)
            item_started = time.monotonic()
            logger.info(
                'Vainglory candidate recognition started: part_id={} '
                'candidate={}/{} at_ms={} elapsed_seconds={:.3f}',
                part.id,
                index + 1,
                len(candidates),
                candidate_at_ms,
                time.monotonic() - recognition_run_started,
            )
            self._emit_status(
                status_callback,
                AnalysisStatus(
                    stage='ocr_recognition',
                    detail='正在验证第 {}/{} 个结算候选'.format(
                        index + 1, len(candidates)
                    ),
                    elapsed_seconds=time.monotonic() - recognition_run_started,
                    candidate_count=len(candidates),
                    current_candidate=index + 1,
                    total_candidates=len(candidates),
                    rejected_candidates=(
                        rejected_layout
                        + rejected_header
                        + rejected_short
                        + rejected_evidence
                        + rejected_lineup
                    ),
                    recognized_matches=len(matches),
                ),
            )
            if progress is not None:
                progress(index / max(1, len(candidates)))
            frame_started = time.monotonic()
            frame = self._sampler.frame_at(part.path, candidate_at_ms)
            if debug_dir is not None:
                frame_path = Path(debug_dir) / 'candidate-{:09d}ms.png'.format(
                    candidate_at_ms
                )
                frame_path.write_bytes(png_bytes(frame))
            layouts = self._detect_result_layouts(frame)
            candidate_frame_seconds += time.monotonic() - frame_started
            if not layouts:
                rejected_layout += 1
                self._log_candidate_completed(
                    part_id=part.id,
                    index=index,
                    total=len(candidates),
                    at_ms=candidate_at_ms,
                    outcome='rejected',
                    reason='layout',
                    item_started=item_started,
                    run_started=recognition_run_started,
                    status_callback=status_callback,
                    rejected_candidates=(
                        rejected_layout
                        + rejected_header
                        + rejected_short
                        + rejected_evidence
                        + rejected_lineup
                    ),
                    recognized_matches=len(matches),
                )
                continue
            header_started = time.monotonic()
            attempts = self._read_layout_headers(
                frame,
                layouts,
                require_completed=True,
                part_id=part.id,
                at_ms=candidate_at_ms,
            )
            header_ocr_seconds += time.monotonic() - header_started
            layout, header = self._select_ocr_context(layouts, attempts)
            header_team_size = layout.team_size
            nearby_started = time.monotonic()
            nearby_contexts = self._sample_nearby_result_frames(
                part.path, at_ms=candidate_at_ms, duration_ms=scanned.video_duration_ms
            )
            nearby_frame_seconds += time.monotonic() - nearby_started
            ranked_contexts = sorted(
                ((frame, layout), *nearby_contexts),
                key=lambda item: result_frame_quality(item[0], item[1]),
                reverse=True,
            )
            frame, layout = ranked_contexts[0]
            name_frames = tuple(item[0] for item in ranked_contexts[1:])
            best_layouts = {layout.team_size: layout}
            for detected_layout in self._detect_result_layouts(frame):
                previous = best_layouts.get(detected_layout.team_size)
                if previous is None or detected_layout.confidence > previous.confidence:
                    best_layouts[detected_layout.team_size] = detected_layout
            recognition_layouts = sorted(
                best_layouts.values(), key=lambda item: item.confidence, reverse=True
            )
            match_started = time.monotonic()
            try:
                recognized: Optional[AnalyzedMatch] = None
                last_rejection: Optional[_ResultEvidenceRejected] = None
                for recognition_layout in recognition_layouts:
                    try:
                        recognized = self._recognize_frame(
                            frame,
                            part=part,
                            at_ms=candidate_at_ms,
                            layout=recognition_layout,
                            header=(
                                header
                                if recognition_layout.team_size == header_team_size
                                else None
                            ),
                            name_frames=name_frames,
                            hero_frames=name_frames,
                            video_duration_ms=scanned.video_duration_ms,
                            view_context=candidate_view_context,
                            game_mode_hint=candidate_mode,
                        )
                    except _ResultEvidenceRejected as rejected:
                        last_rejection = rejected
                        logger.info(
                            'Vainglory result OCR layout retry: part_id={} '
                            'at_ms={} rejected_team_size={} observed_players={}',
                            part.id,
                            candidate_at_ms,
                            recognition_layout.team_size,
                            rejected.result.observed_player_count,
                        )
                        continue
                    layout = recognition_layout
                    break
                if recognized is None:
                    if last_rejection is None:
                        raise RuntimeError('结算候选没有可用的版式')
                    raise last_rejection
            except _ResultEvidenceRejected as rejected:
                match_recognition_seconds += time.monotonic() - match_started
                rejected_evidence += 1
                evidence_header = rejected.result.header
                complete_kda = sum(
                    player.stats.kills is not None
                    and player.stats.deaths is not None
                    and player.stats.assists is not None
                    for player in rejected.result.players
                )
                logger.info(
                    'Vainglory result candidate rejected after full OCR: '
                    'part_id={} at_ms={} reason=evidence left_kills={} '
                    'right_kills={} left_economy={} right_economy={} '
                    'complete_kda={}',
                    part.id,
                    candidate_at_ms,
                    evidence_header.left_kills,
                    evidence_header.right_kills,
                    evidence_header.left_economy,
                    evidence_header.right_economy,
                    complete_kda,
                )
                self._log_candidate_completed(
                    part_id=part.id,
                    index=index,
                    total=len(candidates),
                    at_ms=candidate_at_ms,
                    outcome='rejected',
                    reason='evidence',
                    item_started=item_started,
                    run_started=recognition_run_started,
                    status_callback=status_callback,
                    rejected_candidates=(
                        rejected_layout
                        + rejected_header
                        + rejected_short
                        + rejected_evidence
                        + rejected_lineup
                    ),
                    recognized_matches=len(matches),
                )
                continue
            match_recognition_seconds += time.monotonic() - match_started
            recognized_header = recognized.ocr.header
            if not self._is_completed_match(recognized_header):
                if self._is_result_header(recognized_header):
                    rejected_short += 1
                    reason = 'short'
                else:
                    rejected_header += 1
                    reason = 'header'
                logger.info(
                    'Vainglory result candidate rejected after full OCR: '
                    'part_id={} at_ms={} reason={} result_text={!r} duration={}',
                    part.id,
                    candidate_at_ms,
                    reason,
                    recognized_header.result_text,
                    recognized_header.duration_seconds,
                )
                self._log_candidate_completed(
                    part_id=part.id,
                    index=index,
                    total=len(candidates),
                    at_ms=candidate_at_ms,
                    outcome='rejected',
                    reason=reason,
                    item_started=item_started,
                    run_started=recognition_run_started,
                    status_callback=status_callback,
                    rejected_candidates=(
                        rejected_layout
                        + rejected_header
                        + rejected_short
                        + rejected_evidence
                        + rejected_lineup
                    ),
                    recognized_matches=len(matches),
                )
                continue
            lineup_evidence = self._result_hero_lineup_evidence(
                recognized.heroes, candidate_lineup, team_size=layout.team_size
            )
            logger.info(
                'Vainglory result/HUD hero comparison: part_id={} at_ms={} '
                'evidence={} result_heroes={} hud_heroes={}',
                part.id,
                candidate_at_ms,
                lineup_evidence,
                tuple(hero.label for hero in recognized.heroes if hero.label),
                tuple(label for label in candidate_lineup if label),
            )
            if lineup_evidence == 'mismatched':
                rejected_lineup += 1
                logger.info(
                    'Vainglory result candidate rejected after hero comparison: '
                    'part_id={} at_ms={} reason=lineup',
                    part.id,
                    candidate_at_ms,
                )
                self._log_candidate_completed(
                    part_id=part.id,
                    index=index,
                    total=len(candidates),
                    at_ms=candidate_at_ms,
                    outcome='rejected',
                    reason='lineup',
                    item_started=item_started,
                    run_started=recognition_run_started,
                    status_callback=status_callback,
                    rejected_candidates=(
                        rejected_layout
                        + rejected_header
                        + rejected_short
                        + rejected_evidence
                        + rejected_lineup
                    ),
                    recognized_matches=len(matches),
                )
                continue
            matches.append(recognized)
            logger.info(
                'Vainglory match recognized: part_id={} at_ms={} viewport={} '
                'winner_color={} winner_side={} layout_confidence={:.4f} '
                'result_text={!r} duration={} mode={} match_kind={} '
                'view_context={} stats_eligible={} exclusion_reason={}',
                part.id,
                candidate_at_ms,
                layout.viewport.name,
                layout.winner_color,
                layout.winner_side,
                layout.confidence,
                recognized_header.result_text,
                recognized_header.duration_seconds,
                recognized.game_mode,
                recognized.match_kind,
                recognized.view_context,
                recognized.stats_eligible,
                recognized.stats_exclusion_reason,
            )
            self._log_candidate_completed(
                part_id=part.id,
                index=index,
                total=len(candidates),
                at_ms=candidate_at_ms,
                outcome='accepted',
                reason='',
                item_started=item_started,
                run_started=recognition_run_started,
                status_callback=status_callback,
                rejected_candidates=(
                    rejected_layout
                    + rejected_header
                    + rejected_short
                    + rejected_evidence
                    + rejected_lineup
                ),
                recognized_matches=len(matches),
            )
        recognized_match_count = len(matches)
        matches = list(collapse_analyzed_matches(matches))
        before_content_deduplication = sum(match.stats_eligible for match in matches)
        matches = list(exclude_content_duplicates(matches))
        content_duplicates = before_content_deduplication - sum(
            match.stats_eligible for match in matches
        )
        if progress is not None:
            progress(1.0)
        candidate_seconds = time.monotonic() - candidate_started
        logger.info(
            'Vainglory recognition completed: part_id={} candidates={} '
            'matches={} timeline_duplicates={} content_duplicates={} '
            'rejected_layout={} '
            'rejected_header={} rejected_short={} rejected_evidence={} '
            'rejected_lineup={} '
            'candidate_seconds={:.3f} candidate_frame_seconds={:.3f} '
            'header_ocr_seconds={:.3f} nearby_frame_seconds={:.3f} '
            'match_recognition_seconds={:.3f} total_seconds={:.3f}',
            part.id,
            len(candidates),
            len(matches),
            recognized_match_count - len(matches),
            content_duplicates,
            rejected_layout,
            rejected_header,
            rejected_short,
            rejected_evidence,
            rejected_lineup,
            candidate_seconds,
            candidate_frame_seconds,
            header_ocr_seconds,
            nearby_frame_seconds,
            match_recognition_seconds,
            time.monotonic() - recognition_run_started,
        )
        self._emit_status(
            status_callback,
            AnalysisStatus(
                stage='ocr_recognition',
                detail='候选验证完成：确认 {} 局，排除 {} 个候选'.format(
                    len(matches),
                    rejected_layout
                    + rejected_header
                    + rejected_short
                    + rejected_evidence
                    + rejected_lineup,
                ),
                elapsed_seconds=time.monotonic() - recognition_run_started,
                candidate_count=len(candidates),
                current_candidate=len(candidates),
                total_candidates=len(candidates),
                rejected_candidates=(
                    rejected_layout
                    + rejected_header
                    + rejected_short
                    + rejected_evidence
                    + rejected_lineup
                ),
                recognized_matches=len(matches),
            ),
        )
        return tuple(matches)

    def _log_candidate_completed(
        self,
        *,
        part_id: int,
        index: int,
        total: int,
        at_ms: int,
        outcome: Literal['accepted', 'rejected'],
        reason: str,
        item_started: float,
        run_started: float,
        status_callback: Optional[Callable[[AnalysisStatus], None]],
        rejected_candidates: int,
        recognized_matches: int,
    ) -> None:
        logger.info(
            'Vainglory candidate recognition completed: part_id={} '
            'candidate={}/{} at_ms={} outcome={} reason={} '
            'candidate_seconds={:.3f} elapsed_seconds={:.3f}',
            part_id,
            index + 1,
            total,
            at_ms,
            outcome,
            reason,
            time.monotonic() - item_started,
            time.monotonic() - run_started,
        )
        reason_labels = {
            'layout': '画面结构不是完整结算页',
            'evidence': '结算数据证据不完整',
            'header': '未读到有效结算时长',
            'short': '对局时长不足最低要求',
            'lineup': '结算英雄与 HUD 阵容不一致',
        }
        detail = (
            '第 {}/{} 个候选已确认，当前通过 {} 局'.format(
                index + 1, total, recognized_matches
            )
            if outcome == 'accepted'
            else '第 {}/{} 个候选已排除：{}'.format(
                index + 1, total, reason_labels.get(reason, reason or '证据不足')
            )
        )
        self._emit_status(
            status_callback,
            AnalysisStatus(
                stage='ocr_recognition',
                detail=detail,
                elapsed_seconds=time.monotonic() - run_started,
                candidate_count=total,
                current_candidate=index + 1,
                total_candidates=total,
                rejected_candidates=rejected_candidates,
                recognized_matches=recognized_matches,
            ),
        )

    def _scan_window(
        self,
        path: str,
        window: ScanWindow,
        *,
        part_id: int = 0,
        window_index: int = 0,
        window_count: int = 0,
        status: Optional[Callable[[str], None]] = None,
        cancelled: Optional[Callable[[], bool]] = None,
    ) -> _WindowScanResult:
        if window.end_ms - window.start_ms <= self._COMPACT_FINE_WINDOW_MS:
            return self._scan_compact_window(
                path,
                window,
                part_id=part_id,
                window_index=window_index,
                window_count=window_count,
                status=status,
                cancelled=cancelled,
            )
        focused = _focused_result_window(window)
        logger.info(
            'Vainglory fine scan focus: part_id={} window={}/{} '
            'outer_start_ms={} outer_end_ms={} focus_ms={} '
            'focused_start_ms={} focused_end_ms={}',
            part_id,
            window_index,
            window_count,
            window.start_ms,
            window.end_ms,
            window.focus_ms,
            focused.start_ms,
            focused.end_ms,
        )
        focused_scan = self._scan_window_regions(
            path,
            (focused,),
            part_id=part_id,
            window_index=window_index,
            window_count=window_count,
            scope='focused' if focused != window else 'full',
            scope_label='窄区间' if focused != window else '完整区间',
            status=status,
            cancelled=cancelled,
        )
        if focused_scan.hits or focused == window:
            return focused_scan

        remaining = _remaining_result_windows(window, focused)
        if not remaining:
            return focused_scan
        if status is not None:
            status(
                '第 {}/{} 个区间：窄区间未命中，正在扩展完整兜底范围'.format(
                    window_index, window_count
                )
            )
        logger.info(
            'Vainglory fine scan expanded fallback: part_id={} window={}/{} '
            'regions={}',
            part_id,
            window_index,
            window_count,
            tuple((item.start_ms, item.end_ms) for item in remaining),
        )
        expanded = self._scan_window_regions(
            path,
            remaining,
            part_id=part_id,
            window_index=window_index,
            window_count=window_count,
            scope='expanded',
            scope_label='完整兜底区间',
            status=status,
            cancelled=cancelled,
        )
        return _WindowScanResult(
            hits=collapse_result_hits(
                (*focused_scan.hits, *expanded.hits), maximum_gap_ms=5_000
            ),
            keyframe_preview_frames=(
                focused_scan.keyframe_preview_frames + expanded.keyframe_preview_frames
            ),
            fallback_preview_frames=(
                focused_scan.fallback_preview_frames + expanded.fallback_preview_frames
            ),
            refinement_frames=(
                focused_scan.refinement_frames + expanded.refinement_frames
            ),
            refinement_windows=(
                focused_scan.refinement_windows + expanded.refinement_windows
            ),
            expanded_fallback=True,
        )

    def _scan_compact_window(
        self,
        path: str,
        window: ScanWindow,
        *,
        part_id: int,
        window_index: int,
        window_count: int,
        status: Optional[Callable[[str], None]],
        cancelled: Optional[Callable[[], bool]],
    ) -> _WindowScanResult:
        pass_started = time.monotonic()
        if status is not None:
            status(
                '第 {}/{} 个区间：正在高帧率核验画面变化'.format(
                    window_index, window_count
                )
            )
        logger.info(
            'Vainglory fine scan pass started: part_id={} window={}/{} '
            'pass=transition_fine scope=compact',
            part_id,
            window_index,
            window_count,
        )
        hits: List[ResultHit] = []
        frame_count = 0
        for timed in self._sampler.fine_frames(path, window):
            self._raise_if_cancelled(cancelled)
            frame_count += 1
            layout = self._detect_result_layout(timed.frame)
            if layout is not None:
                hits.append(ResultHit(at_ms=timed.at_ms, layout=layout))
        collapsed = collapse_result_hits(hits, maximum_gap_ms=5_000)
        logger.info(
            'Vainglory fine scan pass completed: part_id={} window={}/{} '
            'pass=transition_fine scope=compact frames={} hits={} '
            'elapsed_seconds={:.3f}',
            part_id,
            window_index,
            window_count,
            frame_count,
            len(collapsed),
            time.monotonic() - pass_started,
        )
        return _WindowScanResult(
            hits=collapsed,
            keyframe_preview_frames=0,
            fallback_preview_frames=0,
            refinement_frames=frame_count,
            refinement_windows=1,
        )

    def _scan_window_regions(
        self,
        path: str,
        regions: Sequence[ScanWindow],
        *,
        part_id: int,
        window_index: int,
        window_count: int,
        scope: str,
        scope_label: str,
        status: Optional[Callable[[str], None]],
        cancelled: Optional[Callable[[], bool]],
    ) -> _WindowScanResult:
        pass_started = time.monotonic()
        if status is not None:
            status(
                '第 {}/{} 个区间：正在快速检查{}关键帧'.format(
                    window_index, window_count, scope_label
                )
            )
        logger.info(
            'Vainglory fine scan pass started: part_id={} window={}/{} '
            'pass=keyframe_preview scope={}',
            part_id,
            window_index,
            window_count,
            scope,
        )
        keyframe_hits, keyframe_frames = self._scan_preview_regions(
            path, regions, keyframes_only=True, cancelled=cancelled
        )
        logger.info(
            'Vainglory fine scan pass completed: part_id={} window={}/{} '
            'pass=keyframe_preview scope={} frames={} hits={} '
            'elapsed_seconds={:.3f}',
            part_id,
            window_index,
            window_count,
            scope,
            keyframe_frames,
            len(keyframe_hits),
            time.monotonic() - pass_started,
        )
        refinement_hits: Tuple[ResultHit, ...] = ()
        refinement_frames = 0
        refinement_windows = 0
        if keyframe_hits:
            if status is not None:
                status(
                    '第 {}/{} 个区间：{}关键帧命中，正在加密确认'.format(
                        window_index, window_count, scope_label
                    )
                )
            refinement_hits, frame_count, window_count_value = (
                self._refine_preview_hits_in_regions(
                    path, regions, keyframe_hits, cancelled=cancelled
                )
            )
            refinement_frames += frame_count
            refinement_windows += window_count_value
        if refinement_hits:
            return _WindowScanResult(
                hits=refinement_hits,
                keyframe_preview_frames=keyframe_frames,
                fallback_preview_frames=0,
                refinement_frames=refinement_frames,
                refinement_windows=refinement_windows,
            )

        pass_started = time.monotonic()
        if status is not None:
            status(
                '第 {}/{} 个区间：正在逐秒补扫{}'.format(
                    window_index, window_count, scope_label
                )
            )
        logger.info(
            'Vainglory fine scan pass started: part_id={} window={}/{} '
            'pass=fallback_preview scope={}',
            part_id,
            window_index,
            window_count,
            scope,
        )
        fallback_hits, fallback_frames = self._scan_preview_regions(
            path, regions, keyframes_only=False, cancelled=cancelled
        )
        logger.info(
            'Vainglory fine scan pass completed: part_id={} window={}/{} '
            'pass=fallback_preview scope={} frames={} hits={} '
            'elapsed_seconds={:.3f}',
            part_id,
            window_index,
            window_count,
            scope,
            fallback_frames,
            len(fallback_hits),
            time.monotonic() - pass_started,
        )
        if fallback_hits:
            if status is not None:
                status(
                    '第 {}/{} 个区间：{}逐秒补扫命中，正在加密确认'.format(
                        window_index, window_count, scope_label
                    )
                )
            refinement_hits, frame_count, window_count_value = (
                self._refine_preview_hits_in_regions(
                    path, regions, fallback_hits, cancelled=cancelled
                )
            )
            refinement_frames += frame_count
            refinement_windows += window_count_value
        if refinement_hits:
            return _WindowScanResult(
                hits=refinement_hits,
                keyframe_preview_frames=keyframe_frames,
                fallback_preview_frames=fallback_frames,
                refinement_frames=refinement_frames,
                refinement_windows=refinement_windows,
            )

        pass_started = time.monotonic()
        if status is not None:
            status(
                '第 {}/{} 个区间：{}预览未命中，正在高帧率兜底'.format(
                    window_index, window_count, scope_label
                )
            )
        logger.info(
            'Vainglory fine scan pass started: part_id={} window={}/{} '
            'pass=fine_fallback scope={}',
            part_id,
            window_index,
            window_count,
            scope,
        )
        fine_fallback_hits: List[ResultHit] = []
        frame_count = 0
        scanned_regions = 0
        for region in regions:
            if region.end_ms <= region.start_ms:
                continue
            scanned_regions += 1
            for timed in self._sampler.fine_frames(path, region):
                self._raise_if_cancelled(cancelled)
                frame_count += 1
                layout = self._detect_result_layout(timed.frame)
                if layout is not None:
                    fine_fallback_hits.append(
                        ResultHit(at_ms=timed.at_ms, layout=layout)
                    )
        refinement_frames += frame_count
        refinement_windows += scanned_regions
        logger.info(
            'Vainglory fine scan pass completed: part_id={} window={}/{} '
            'pass=fine_fallback scope={} frames={} hits={} '
            'elapsed_seconds={:.3f}',
            part_id,
            window_index,
            window_count,
            scope,
            frame_count,
            len(fine_fallback_hits),
            time.monotonic() - pass_started,
        )
        return _WindowScanResult(
            hits=collapse_result_hits(
                fine_fallback_hits or fallback_hits or keyframe_hits,
                maximum_gap_ms=5_000,
            ),
            keyframe_preview_frames=keyframe_frames,
            fallback_preview_frames=fallback_frames,
            refinement_frames=refinement_frames,
            refinement_windows=refinement_windows,
        )

    def _scan_preview_regions(
        self,
        path: str,
        regions: Sequence[ScanWindow],
        *,
        keyframes_only: bool,
        cancelled: Optional[Callable[[], bool]],
    ) -> Tuple[Tuple[ResultHit, ...], int]:
        hits: List[ResultHit] = []
        frame_count = 0
        for region in regions:
            region_hits, region_frames = self._scan_preview(
                path, region, keyframes_only=keyframes_only, cancelled=cancelled
            )
            hits.extend(region_hits)
            frame_count += region_frames
        return tuple(hits), frame_count

    def _scan_preview(
        self,
        path: str,
        window: ScanWindow,
        *,
        keyframes_only: bool,
        cancelled: Optional[Callable[[], bool]],
    ) -> Tuple[Tuple[ResultHit, ...], int]:
        hits: List[ResultHit] = []
        frame_count = 0
        for timed in self._sampler.result_preview_frames(
            path, window, keyframes_only=keyframes_only
        ):
            self._raise_if_cancelled(cancelled)
            frame_count += 1
            layout = self._detect_result_layout(timed.frame)
            if layout is not None:
                hits.append(ResultHit(at_ms=timed.at_ms, layout=layout))
        return tuple(hits), frame_count

    def _refine_preview_hits_in_regions(
        self,
        path: str,
        regions: Sequence[ScanWindow],
        preview_hits: Sequence[ResultHit],
        *,
        cancelled: Optional[Callable[[], bool]],
    ) -> Tuple[Tuple[ResultHit, ...], int, int]:
        hits: List[ResultHit] = []
        frame_count = 0
        window_count = 0
        for region in regions:
            region_hits = tuple(
                hit
                for hit in preview_hits
                if region.start_ms <= hit.at_ms <= region.end_ms
            )
            if not region_hits:
                continue
            refined, frames, windows = self._refine_preview_hits(
                path, region, region_hits, cancelled=cancelled
            )
            hits.extend(refined)
            frame_count += frames
            window_count += windows
        return collapse_result_hits(hits), frame_count, window_count

    def _refine_preview_hits(
        self,
        path: str,
        outer_window: ScanWindow,
        preview_hits: Sequence[ResultHit],
        *,
        cancelled: Optional[Callable[[], bool]],
    ) -> Tuple[Tuple[ResultHit, ...], int, int]:
        windows = _result_refinement_windows(preview_hits, outer_window=outer_window)
        hits: List[ResultHit] = []
        frame_count = 0
        for window in windows:
            for timed in self._sampler.fine_frames(path, window):
                self._raise_if_cancelled(cancelled)
                frame_count += 1
                layout = self._detect_result_layout(timed.frame)
                if layout is not None:
                    hits.append(ResultHit(at_ms=timed.at_ms, layout=layout))
        return collapse_result_hits(hits), frame_count, len(windows)

    def _recognize_frame(
        self,
        frame: RgbFrame,
        *,
        part: VideoPart,
        at_ms: int,
        layout: ResultLayout,
        header: Optional[ResultHeader],
        name_frames: Sequence[RgbFrame] = (),
        hero_frames: Sequence[RgbFrame] = (),
        video_duration_ms: int,
        view_context: Literal['played', 'observed', 'unknown'] = 'unknown',
        game_mode_hint: str = '',
    ) -> AnalyzedMatch:
        ocr_started = time.monotonic()
        if layout.viewport.ocr_profile == 'wide':
            recognized = self._result_reader.read_wide_screenshot(
                frame,
                header=header,
                viewport=layout.viewport,
                name_frames=name_frames,
                team_size=layout.team_size,
            )
        else:
            recognized = self._result_reader.read(
                frame,
                header=header,
                viewport=layout.viewport,
                name_frames=name_frames,
                team_size=layout.team_size,
            )
        ocr_seconds = time.monotonic() - ocr_started
        if not self._is_credible_result(recognized, team_size=layout.team_size):
            raise _ResultEvidenceRejected(recognized)
        heroes_started = time.monotonic()
        heroes = self._recognize_heroes(frame, layout, nearby_frames=hero_frames)
        recorded_player = detect_recorded_player(frame, layout)
        if self._hero_recognizer is not None and (
            any(not hero.label for hero in heroes) or recorded_player is None
        ):
            gameplay_frames = self._sample_gameplay_hud_frames(
                part.path,
                result_at_ms=at_ms,
                duration_seconds=recognized.header.duration_seconds,
                video_duration_ms=video_duration_ms,
            )
            if gameplay_frames:
                heroes, fallback_player = self._apply_gameplay_hud_fallback(
                    heroes,
                    layout=layout,
                    frames=gameplay_frames,
                    team_size=layout.team_size,
                )
                if recorded_player is None:
                    recorded_player = fallback_player
        hero_seconds = time.monotonic() - heroes_started
        frame_encode_started = time.monotonic()
        result_frame_png = png_bytes(frame)
        frame_encode_seconds = time.monotonic() - frame_encode_started
        player_confidence = mean(player.confidence for player in recognized.players)
        confidence = min(1.0, layout.confidence * 0.6 + player_confidence * 0.4)
        game_mode = self._detect_game_mode(
            part.path,
            result_at_ms=at_ms,
            duration_seconds=recognized.header.duration_seconds,
            video_duration_ms=video_duration_ms,
            team_size=layout.team_size,
            hint=game_mode_hint,
        )
        populated_players = sum(
            bool(player.name or player.raw_name) for player in recognized.players
        )
        if populated_players >= 10:
            game_mode = '5v5'
        resolved_view_context: Literal['played', 'observed', 'unknown'] = (
            'played'
            if view_context == 'unknown' and recorded_player is not None
            else view_context
        )
        match_kind = classify_match_kind(recognized, heroes, team_size=layout.team_size)
        eligible, exclusion_reason = stats_eligibility(
            game_mode=game_mode,
            duration_seconds=recognized.header.duration_seconds,
            match_kind=match_kind,
            view_context=resolved_view_context,
        )
        short_suspect = (
            game_mode == '3v3'
            and recognized.header.duration_seconds is not None
            and 5 * 60 <= recognized.header.duration_seconds < 8 * 60
        )
        logger.info(
            'Vainglory match extraction timings: part_id={} at_ms={} '
            'name_stats_ocr_seconds={:.3f} hero_seconds={:.3f} '
            'result_frame_encode_seconds={:.3f} result_frame_bytes={}',
            part.id,
            at_ms,
            ocr_seconds,
            hero_seconds,
            frame_encode_seconds,
            len(result_frame_png),
        )
        logger.info(
            'Vainglory match classification: part_id={} at_ms={} mode={} '
            'match_kind={} view_context={} stats_eligible={} '
            'exclusion_reason={} short_suspect={}',
            part.id,
            at_ms,
            game_mode,
            match_kind,
            resolved_view_context,
            eligible,
            exclusion_reason,
            short_suspect,
        )
        return AnalyzedMatch(
            part_id=part.id,
            part_index=part.index,
            result_at_ms=at_ms,
            layout=layout,
            ocr=recognized,
            heroes=heroes,
            confidence=confidence,
            result_frame_png=result_frame_png,
            game_mode=game_mode,
            recorded_player=recorded_player,
            match_kind=match_kind,
            view_context=resolved_view_context,
            stats_eligible=eligible,
            stats_exclusion_reason=exclusion_reason,
        )

    def _detect_game_mode(
        self,
        path: str,
        *,
        result_at_ms: int,
        duration_seconds: Optional[int],
        video_duration_ms: int,
        team_size: TeamSize,
        hint: str = '',
    ) -> str:
        if team_size == 5:
            return '5v5'
        if hint in ('5v5', 'aram'):
            return hint
        if duration_seconds is None:
            return '3v3' if hint == '3v3' else 'unknown'
        estimated_start_ms = result_at_ms - duration_seconds * 1_000
        if estimated_start_ms < 0:
            return '3v3' if hint == '3v3' else 'unknown'
        if self._stage_classifier is None:
            return self._legacy_game_mode(
                path,
                estimated_start_ms=estimated_start_ms,
                video_duration_ms=video_duration_ms,
            )
        classifier = self._stage_classifier
        window_start = max(0, estimated_start_ms - 180_000)
        window_end = min(video_duration_ms, estimated_start_ms)
        if window_end <= window_start:
            return '3v3'
        pre_match_modes: Counter[int] = Counter()
        talent_frames = 0
        for timed in self._sampler.fine_frames(
            path, ScanWindow(start_ms=window_start, end_ms=window_end)
        ):
            prediction = classifier.classify(timed.frame)
            if prediction.stage == STAGE_PRE_MATCH:
                pre_match_modes[prediction.mode] += 1
            elif (
                prediction.stage == STAGE_TALENT_SELECT and prediction.mode == MODE_ARAM
            ):
                talent_frames += 1
        logger.info(
            'Vainglory game mode opening window: result_at_ms={} '
            'estimated_start_ms={} window_start_ms={} window_end_ms={} '
            'pre_match_modes={} talent_frames={}',
            result_at_ms,
            estimated_start_ms,
            window_start,
            window_end,
            dict(pre_match_modes),
            talent_frames,
        )
        if talent_frames >= 2:
            return 'aram'
        if pre_match_modes:
            total = sum(pre_match_modes.values())
            dominant_mode, dominant_count = pre_match_modes.most_common(1)[0]
            if total >= 8 and dominant_count >= total * 0.5:
                if dominant_mode == MODE_5V5:
                    return '5v5'
                if dominant_mode == MODE_ARAM:
                    return 'aram'
        return '3v3'

    def _legacy_game_mode(
        self, path: str, *, estimated_start_ms: int, video_duration_ms: int
    ) -> str:
        sampled_at: List[int] = []
        talent_hits = 0
        for offset_ms in (1_000, 3_000, 5_000):
            at_ms = estimated_start_ms + offset_ms
            if at_ms < 0 or at_ms >= video_duration_ms or at_ms in sampled_at:
                continue
            sampled_at.append(at_ms)
            try:
                frame = self._sampler.frame_at(path, at_ms)
            except RuntimeError as error:
                logger.warning(
                    'Skipped unreadable optional Vainglory mode frame: '
                    'at_ms={} error={}',
                    at_ms,
                    error,
                )
                continue
            visible = self._aram_detector.is_visible(frame)
            logger.debug(
                'Vainglory game mode probe: at_ms={} talent_visible={}', at_ms, visible
            )
            if visible:
                talent_hits += 1
            if talent_hits >= 2:
                logger.info(
                    'Vainglory ARAM talent selector recognized: '
                    'sampled_at={} talent_hits={}',
                    tuple(sampled_at),
                    talent_hits,
                )
                return 'aram'
        return '3v3'

    def _recognize_heroes(
        self,
        frame: RgbFrame,
        layout: ResultLayout,
        *,
        nearby_frames: Sequence[RgbFrame] = (),
    ) -> Tuple[AnalyzedHero, ...]:
        variants = [
            (
                0.0,
                self._recognize_hero_variant(
                    frame,
                    viewport=layout.viewport,
                    team_size=layout.team_size,
                    center_shift=0.0,
                ),
            )
        ]
        if self._hero_recognizer is not None and any(
            match is None for _, match in variants[0][1]
        ):
            for center_shift in (-0.01, 0.01, -0.02, 0.02):
                variants.append(
                    (
                        center_shift,
                        self._recognize_hero_variant(
                            frame,
                            viewport=layout.viewport,
                            team_size=layout.team_size,
                            center_shift=center_shift,
                        ),
                    )
                )
        center_shift, selected = max(
            variants,
            key=lambda variant: self._hero_variant_score(variant[0], variant[1]),
        )
        logger.debug(
            'Vainglory hero layout selected: viewport={} center_shift={:.3f} '
            'recognized={}/{}',
            layout.viewport.name,
            center_shift,
            sum(match is not None for _, match in selected),
            layout.team_size * 2,
        )
        unresolved = {
            (hero.side, hero.slot) for hero, match in selected if match is None
        }
        nearby_matches = self._recognize_nearby_heroes(
            nearby_frames, unresolved, team_size=layout.team_size
        )
        if nearby_matches:
            logger.info(
                'Vainglory nearby frames filled hero positions: filled={} '
                'remaining={}',
                len(nearby_matches),
                len(unresolved) - len(nearby_matches),
            )
        resolved = tuple(
            (
                nearby_matches.get((hero.side, hero.slot), (hero, match))
                if match is None
                else (hero, match)
            )
            for hero, match in selected
        )
        return tuple(
            AnalyzedHero(
                side=hero.side,
                slot=hero.slot,
                fingerprint=hero_fingerprint(hero.frame),
                thumbnail_png=png_bytes(hero.frame),
                label='' if match is None else match.label,
                confidence=0 if match is None else match.confidence,
            )
            for hero, match in resolved
        )

    def _recognize_nearby_heroes(
        self,
        frames: Sequence[RgbFrame],
        positions: Set[Tuple[TeamSide, int]],
        *,
        team_size: TeamSize,
    ) -> Dict[Tuple[TeamSide, int], Tuple[HeroFrame, HeroMatch]]:
        if self._hero_recognizer is None or not frames or not positions:
            return {}
        candidates: Dict[Tuple[TeamSide, int], List[Tuple[HeroFrame, HeroMatch]]] = {
            position: [] for position in positions
        }
        for frame in frames:
            layout = self._detect_result_layout(frame)
            if layout is None or layout.team_size != team_size:
                continue
            best_in_frame: Dict[
                Tuple[TeamSide, int], Tuple[HeroFrame, HeroMatch, float]
            ] = {}
            for center_shift in (0.0, -0.01, 0.01, -0.02, 0.02):
                for hero in extract_result_heroes(
                    frame,
                    viewport=layout.viewport,
                    team_size=layout.team_size,
                    center_shift=center_shift,
                ):
                    position = (hero.side, hero.slot)
                    if position not in positions:
                        continue
                    match = self._hero_recognizer.recognize(hero.frame)
                    if match is None:
                        continue
                    current = best_in_frame.get(position)
                    score = (
                        match.confidence,
                        match.inliers,
                        match.margin,
                        -abs(center_shift),
                    )
                    if current is None:
                        best_in_frame[position] = (hero, match, center_shift)
                        continue
                    current_match = current[1]
                    current_score = (
                        current_match.confidence,
                        current_match.inliers,
                        current_match.margin,
                        -abs(current[2]),
                    )
                    if score > current_score:
                        best_in_frame[position] = (hero, match, center_shift)
            for position, (hero, match, _) in best_in_frame.items():
                candidates[position].append((hero, match))

        resolved: Dict[Tuple[TeamSide, int], Tuple[HeroFrame, HeroMatch]] = {}
        for position, position_candidates in candidates.items():
            by_label: Dict[str, List[Tuple[HeroFrame, HeroMatch]]] = {}
            for candidate in position_candidates:
                by_label.setdefault(candidate[1].label, []).append(candidate)
            ordered = sorted(
                by_label.values(),
                key=lambda group: (
                    len(group),
                    max(item[1].confidence for item in group),
                    sum(item[1].inliers for item in group),
                    sum(item[1].margin for item in group),
                ),
                reverse=True,
            )
            if not ordered:
                continue
            winner = ordered[0]
            strongest = max(
                winner,
                key=lambda item: (item[1].confidence, item[1].inliers, item[1].margin),
            )
            stable_consensus = (
                len(winner) >= 2
                and (len(ordered) == 1 or len(winner) > len(ordered[1]))
                and strongest[1].confidence >= 0.70
                and strongest[1].inliers >= 8
            )
            single_strong_match = (
                len(by_label) == 1
                and strongest[1].confidence >= 0.85
                and strongest[1].inliers >= 10
            )
            if stable_consensus or single_strong_match:
                resolved[position] = strongest
        return resolved

    def _sample_gameplay_hud_frames(
        self,
        path: str,
        *,
        result_at_ms: int,
        duration_seconds: Optional[int],
        video_duration_ms: int,
    ) -> Tuple[RgbFrame, ...]:
        if duration_seconds is None:
            return ()
        estimated_start_ms = result_at_ms - duration_seconds * 1_000
        if estimated_start_ms < 0:
            return ()
        frames: List[RgbFrame] = []
        accepted_at: List[int] = []
        for offset_ms in (
            5_000,
            10_000,
            15_000,
            20_000,
            25_000,
            30_000,
            35_000,
            40_000,
        ):
            at_ms = estimated_start_ms + offset_ms
            if at_ms < 0 or at_ms >= video_duration_ms:
                continue
            try:
                frame = self._sampler.frame_at(path, at_ms)
            except RuntimeError as error:
                logger.warning(
                    'Skipped unreadable Vainglory gameplay HUD frame: '
                    'at_ms={} error={}',
                    at_ms,
                    error,
                )
                continue
            frame = normalize_gameplay_frame(frame)
            if detect_gameplay_hud(frame) is None:
                continue
            frames.append(frame)
            accepted_at.append(at_ms)
        logger.debug(
            'Vainglory gameplay HUD fallback frames: result_at_ms={} accepted={}',
            result_at_ms,
            tuple(accepted_at),
        )
        return tuple(frames)

    def _apply_gameplay_hud_fallback(
        self,
        heroes: Sequence[AnalyzedHero],
        *,
        layout: ResultLayout,
        frames: Sequence[RgbFrame],
        team_size: TeamSize,
    ) -> Tuple[Tuple[AnalyzedHero, ...], Optional[RecordedPlayer]]:
        hud = self._recognize_gameplay_hud_heroes(frames, team_size=team_size)
        side_map = self._map_gameplay_hud_sides(heroes, hud)
        if not side_map:
            return tuple(heroes), None
        updated = list(heroes)
        for hud_side, result_side in side_map.items():
            resolved_labels = Counter(
                hero.label
                for hero in updated
                if hero.side == result_side and hero.label
            )
            available = []
            for slot in range(1, team_size + 1):
                candidate = hud.get((hud_side, slot))
                if candidate is None:
                    continue
                label = candidate[1].label
                if resolved_labels[label] > 0:
                    resolved_labels[label] -= 1
                else:
                    available.append(candidate)
            unresolved = [
                index
                for index, hero in enumerate(updated)
                if hero.side == result_side and not hero.label
            ]
            if len(unresolved) != 1 or len(available) != 1:
                continue
            index = unresolved[0]
            previous = updated[index]
            match = available[0][1]
            updated[index] = AnalyzedHero(
                side=previous.side,
                slot=previous.slot,
                fingerprint=previous.fingerprint,
                thumbnail_png=previous.thumbnail_png,
                label=match.label,
                confidence=min(0.89, match.confidence * 0.9),
            )
            logger.info(
                'Vainglory gameplay HUD filled result hero: side={} slot={} '
                'label={} confidence={:.3f}',
                previous.side,
                previous.slot,
                match.label,
                match.confidence,
            )

        teal_side: TeamSide = 'left' if layout.left_color == 'teal' else 'right'
        hud_teal_side = next(
            (hud_side for hud_side, side in side_map.items() if side == teal_side), None
        )
        if hud_teal_side is None:
            return tuple(updated), None
        local_slot = team_size if hud_teal_side == 'left' else 1
        local = hud.get((hud_teal_side, local_slot))
        if local is None:
            return tuple(updated), None
        matching = [
            hero
            for hero in updated
            if hero.side == teal_side and hero.label == local[1].label
        ]
        if len(matching) != 1:
            return tuple(updated), None
        player = RecordedPlayer(
            side=matching[0].side,
            slot=matching[0].slot,
            confidence=min(0.89, 0.68 + local[1].confidence * 0.2),
        )
        logger.info(
            'Vainglory gameplay HUD identified recorded player: side={} slot={} '
            'hero={} confidence={:.3f}',
            player.side,
            player.slot,
            local[1].label,
            player.confidence,
        )
        return tuple(updated), player

    def _recognize_gameplay_hud_heroes(
        self, frames: Sequence[RgbFrame], *, team_size: TeamSize
    ) -> Dict[Tuple[TeamSide, int], Tuple[HeroFrame, HeroMatch]]:
        if self._hero_recognizer is None:
            return {}
        candidates: Dict[Tuple[TeamSide, int], List[Tuple[HeroFrame, HeroMatch]]] = {}
        for frame in frames:
            centers = select_gameplay_hud_centers(frame, team_size=team_size)
            if centers is None:
                continue
            selected = tuple(
                (hero, self._hero_recognizer.recognize(hero.frame))
                for hero in extract_gameplay_hud_heroes(
                    frame, team_size=team_size, centers=centers
                )
            )
            for hero, match in selected:
                if match is None:
                    continue
                candidates.setdefault((hero.side, hero.slot), []).append((hero, match))
        result: Dict[Tuple[TeamSide, int], Tuple[HeroFrame, HeroMatch]] = {}
        for position, values in candidates.items():
            grouped: Dict[str, List[Tuple[HeroFrame, HeroMatch]]] = {}
            for value in values:
                grouped.setdefault(value[1].label, []).append(value)
            ordered = sorted(
                grouped.values(),
                key=lambda group: (
                    len(group),
                    max(item[1].confidence for item in group),
                    sum(item[1].inliers for item in group),
                ),
                reverse=True,
            )
            if not ordered:
                continue
            winner = ordered[0]
            strongest = max(
                winner, key=lambda item: (item[1].confidence, item[1].inliers)
            )
            stable = len(winner) >= 2 and (
                len(ordered) == 1 or len(winner) > len(ordered[1])
            )
            strong = (
                len(grouped) == 1
                and strongest[1].confidence >= 0.85
                and strongest[1].inliers >= 10
            )
            if stable or strong:
                result[position] = strongest
        return result

    @staticmethod
    def _map_gameplay_hud_sides(
        heroes: Sequence[AnalyzedHero],
        hud: Dict[Tuple[TeamSide, int], Tuple[HeroFrame, HeroMatch]],
    ) -> Dict[TeamSide, TeamSide]:
        result_labels = {
            side: Counter(
                hero.label for hero in heroes if hero.side == side and hero.label
            )
            for side in ('left', 'right')
        }
        hud_labels = {
            side: Counter(
                match.label
                for (candidate_side, _), (_, match) in hud.items()
                if candidate_side == side
            )
            for side in ('left', 'right')
        }

        def overlap(first: Counter[str], second: Counter[str]) -> int:
            return sum((first & second).values())

        same = overlap(hud_labels['left'], result_labels['left']) + overlap(
            hud_labels['right'], result_labels['right']
        )
        swapped = overlap(hud_labels['left'], result_labels['right']) + overlap(
            hud_labels['right'], result_labels['left']
        )
        if max(same, swapped) < 2 or same == swapped:
            return {}
        if same > swapped:
            return {'left': 'left', 'right': 'right'}
        return {'left': 'right', 'right': 'left'}

    @staticmethod
    def _result_hero_lineup_evidence(
        heroes: Sequence[AnalyzedHero], lineup: Sequence[str], *, team_size: TeamSize
    ) -> Literal['matched', 'mismatched', 'unknown']:
        if len(lineup) != team_size * 2:
            return 'unknown'
        labels = {(hero.side, hero.slot): hero.label for hero in heroes if hero.label}
        sides: Tuple[TeamSide, TeamSide] = ('left', 'right')
        result_lineup = tuple(
            labels.get((side, slot), '')
            for side in sides
            for slot in range(1, team_size + 1)
        )
        return hero_lineup_evidence(result_lineup, lineup)

    @staticmethod
    def _result_hud_lineup_evidence(
        heroes: Sequence[AnalyzedHero],
        hud: Dict[Tuple[TeamSide, int], Tuple[HeroFrame, HeroMatch]],
        *,
        team_size: TeamSize,
    ) -> Literal['matched', 'mismatched', 'unknown']:
        sides: Tuple[TeamSide, TeamSide] = ('left', 'right')
        lineup = tuple(
            ('' if (side, slot) not in hud else hud[(side, slot)][1].label)
            for side in sides
            for slot in range(1, team_size + 1)
        )
        return VaingloryVideoAnalyzer._result_hero_lineup_evidence(
            heroes, lineup, team_size=team_size
        )

    def recognize_saved_heroes(self, content: bytes) -> Tuple[AnalyzedHero, ...]:
        frame = self._sampler.decode_image(content)
        layout = self._detect_result_layout(frame)
        if layout is None:
            raise ValueError('保存的图片不是可识别的结算画面')
        return self._recognize_heroes(frame, layout)

    def detect_saved_recorded_player(self, content: bytes) -> Optional[RecordedPlayer]:
        frame = self._sampler.decode_image(content)
        layout = self._detect_result_layout(frame)
        if layout is None:
            raise ValueError('保存的图片不是可识别的结算画面')
        return detect_recorded_player(frame, layout)

    def _recognize_hero_variant(
        self,
        frame: RgbFrame,
        *,
        viewport: ViewportTransform,
        team_size: TeamSize,
        center_shift: float,
    ) -> Tuple[Tuple[HeroFrame, Optional[HeroMatch]], ...]:
        return tuple(
            (
                hero,
                (
                    None
                    if self._hero_recognizer is None
                    else self._hero_recognizer.recognize(hero.frame)
                ),
            )
            for hero in extract_result_heroes(
                frame, viewport=viewport, team_size=team_size, center_shift=center_shift
            )
        )

    @staticmethod
    def _hero_variant_score(
        center_shift: float, recognized: Sequence[Tuple[HeroFrame, Optional[HeroMatch]]]
    ) -> Tuple[int, float, int, int]:
        matches = tuple(match for _, match in recognized if match is not None)
        return (
            len(matches),
            -abs(center_shift),
            sum(match.inliers for match in matches),
            sum(match.margin for match in matches),
        )

    def _sample_nearby_result_frames(
        self, path: str, *, at_ms: int, duration_ms: int
    ) -> Tuple[Tuple[RgbFrame, ResultLayout], ...]:
        frames: List[Tuple[RgbFrame, ResultLayout]] = []
        attempted_at: List[int] = []
        accepted_at: List[int] = []
        offsets_ms = (
            -5_000,
            -3_000,
            -2_000,
            -1_000,
            -500,
            500,
            1_000,
            2_000,
            3_000,
            5_000,
        )
        for offset_ms in offsets_ms:
            candidate_at = max(0, min(duration_ms - 1, at_ms + offset_ms))
            if candidate_at == at_ms or candidate_at in attempted_at:
                continue
            attempted_at.append(candidate_at)
            try:
                frame = self._sampler.frame_at(path, candidate_at)
            except RuntimeError as error:
                logger.warning(
                    'Skipped unreadable optional Vainglory nearby frame: '
                    'at_ms={} error={}',
                    candidate_at,
                    error,
                )
                continue
            layout = self._detect_result_layout(frame)
            if layout is None:
                continue
            frames.append((frame, layout))
            accepted_at.append(candidate_at)
        logger.debug(
            'Vainglory nearby OCR frames: at_ms={} sampled_at={} accepted={}',
            at_ms,
            tuple(at_ms + offset for offset in offsets_ms),
            tuple(accepted_at),
        )
        return tuple(frames)

    def _recognize_coarse_hud_lineup(
        self,
        path: str,
        at_ms: int,
        *,
        team_size: TeamSize,
        viewport: Optional[Tuple[float, float, float, float]] = None,
    ) -> Tuple[str, ...]:
        try:
            frame = self._sampler.frame_at(path, at_ms)
        except RuntimeError as error:
            logger.warning(
                'Skipped unreadable Vainglory HUD lineup frame: ' 'at_ms={} error={}',
                at_ms,
                error,
            )
            return ()
        if viewport is None:
            frame = normalize_gameplay_frame(frame)
        elif viewport != (0.0, 0.0, 1.0, 1.0):
            frame = frame.crop(frame.relative_rect(*viewport))
        recognized = self._recognize_gameplay_hud_heroes((frame,), team_size=team_size)
        sides: Tuple[TeamSide, TeamSide] = ('left', 'right')
        lineup = tuple(
            (
                ''
                if (side, slot) not in recognized
                else recognized[(side, slot)][1].label
            )
            for side in sides
            for slot in range(1, team_size + 1)
        )
        logger.debug(
            'Vainglory coarse HUD heroes: at_ms={} team_size={} labels={}',
            at_ms,
            team_size,
            lineup,
        )
        return lineup

    @staticmethod
    def _merge_hud_lineups(
        previous: Sequence[str], current: Sequence[str]
    ) -> Tuple[str, ...]:
        if not previous:
            return tuple(current)
        if not current or len(previous) != len(current):
            return tuple(previous)
        return tuple(left or right for left, right in zip(previous, current))

    def _detect_result_layout(self, frame: RgbFrame) -> Optional[ResultLayout]:
        layouts = self._detect_result_layouts(frame)
        if not layouts:
            return None
        return max(layouts, key=lambda layout: layout.confidence)

    def _detect_result_layouts(self, frame: RgbFrame) -> Tuple[ResultLayout, ...]:
        detector = self._result_panel_detector
        if detector is None:
            return detect_result_layouts(frame)
        try:
            detection = detector.detect(frame)
        except (ImportError, OSError, RuntimeError, ValueError) as error:
            logger.error(
                'Vainglory result panel detector disabled after failure: {!r}', error
            )
            self._result_panel_detector = None
            return detect_result_layouts(frame)
        if detection is None:
            return ()
        return detect_result_layouts(frame, panel_detection=detection)

    def _read_layout_headers(
        self,
        frame: RgbFrame,
        layouts: Sequence[ResultLayout],
        *,
        require_completed: bool,
        part_id: Optional[int] = None,
        at_ms: Optional[int] = None,
    ) -> Tuple[Tuple[ResultLayout, ResultHeader], ...]:
        attempts: List[Tuple[ResultLayout, ResultHeader]] = []
        for layout in layouts:
            header = self._result_reader.read_header(
                frame, viewport=layout.viewport, team_size=layout.team_size
            )
            attempts.append((layout, header))
            logger.debug(
                'Vainglory result OCR attempt: part_id={} at_ms={} viewport={} '
                'team_size={} result_text={!r} duration={}',
                part_id,
                at_ms,
                layout.viewport.name,
                layout.team_size,
                header.result_text,
                header.duration_seconds,
            )
            accepted = (
                self._is_completed_match(header)
                if require_completed
                else self._is_result_header(header)
            )
            if accepted:
                break
        return tuple(attempts)

    def _is_completed_match(self, header: ResultHeader) -> bool:
        return (
            self._is_result_header(header)
            and header.duration_seconds is not None
            and header.duration_seconds >= self._minimum_match_seconds
        )

    @staticmethod
    def _is_credible_result(result: ResultOcr, *, team_size: int) -> bool:
        if team_size not in (3, 5):
            return False
        header = result.header
        if not all(
            value is not None
            for value in (
                header.left_kills,
                header.right_kills,
                header.left_economy,
                header.right_economy,
            )
        ):
            return False
        observed_players = result.observed_player_count
        if observed_players is None:
            return True
        return observed_players <= 6 if team_size == 3 else observed_players > 6

    def _select_ocr_context(
        self,
        layouts: Sequence[ResultLayout],
        attempts: Sequence[Tuple[ResultLayout, ResultHeader]],
    ) -> Tuple[ResultLayout, Optional[ResultHeader]]:
        primary_layout = max(layouts, key=lambda item: item.confidence)
        accepted = tuple(
            (attempt_layout, header)
            for attempt_layout, header in attempts
            if attempt_layout.team_size == primary_layout.team_size
            and self._is_completed_match(header)
        )
        if not accepted:
            return primary_layout, None
        header = merge_result_headers(
            tuple(
                (attempt_header, attempt_layout.confidence)
                for attempt_layout, attempt_header in attempts
                if attempt_layout.team_size == primary_layout.team_size
            )
        )
        return primary_layout, header

    @staticmethod
    def _is_result_header(header: ResultHeader) -> bool:
        return header.duration_seconds is not None

    @staticmethod
    def _emit_status(
        callback: Optional[Callable[[AnalysisStatus], None]], status: AnalysisStatus
    ) -> None:
        if callback is not None:
            callback(status)

    @staticmethod
    def _raise_if_cancelled(cancelled: Optional[Callable[[], bool]]) -> None:
        if cancelled is not None and cancelled():
            raise AnalysisCancelled('对局分析已停止')
