from dataclasses import replace
from types import SimpleNamespace
from typing import Tuple

import pytest
from loguru import logger

import blrec.vainglory.analyzer as analyzer_module
from blrec.vainglory.analyzer import (
    AnalysisCancelled,
    AnalyzedHero,
    AnalyzedMatch,
    ResultHit,
    TrainingCandidate,
    VaingloryVideoAnalyzer,
    VideoPart,
    _apply_hud_team_size_evidence,
    _avatar_screen_context,
    _candidate_segment_start,
    _cap_training_candidate_timestamps,
    _ModeConflict,
    _model_package_mode_conflicts,
    _model_package_run_modes,
    _remember_model_package_prediction_candidates,
    _remember_training_candidate,
    _segments_with_gameplay,
    _selected_model_package_candidates,
    _timeline_refinement_windows,
    classify_match_kind,
    collapse_analyzed_matches,
    collapse_result_hits,
    exclude_content_duplicates,
    stats_eligibility,
)
from blrec.vainglory.hero_recognition import HeroMatch
from blrec.vainglory.ocr import OcrPlayer, PlayerStats, ResultHeader, ResultOcr
from blrec.vainglory.sampling import ScanWindow, TimedFrame, VideoProfile
from blrec.vainglory.stage_classifier import (
    CONTENT_VAINGLORY,
    MODE_3V3,
    MODE_5V5,
    MODE_ARAM,
    STAGE_GAMEPLAY,
    STAGE_OUT_OF_MATCH,
    STAGE_PRE_MATCH,
    STAGE_TRANSITION,
    ClassifiedObservation,
    StagePrediction,
)
from blrec.vainglory.vision import (
    GameplayHud,
    HeroAvatarDetection,
    HeroFrame,
    PixelRect,
    ResultLayout,
    RgbFrame,
    ViewportTransform,
)


def hit(at_ms: int, confidence: float = 1.0) -> ResultHit:
    return ResultHit(
        at_ms=at_ms,
        layout=ResultLayout(
            left_color='teal',
            right_color='orange',
            winner_color='teal',
            winner_side='left',
            confidence=confidence,
        ),
    )


def analyzed_match(at_ms: int, duration_seconds: int) -> AnalyzedMatch:
    return AnalyzedMatch(
        part_id=1,
        part_index=1,
        result_at_ms=at_ms,
        layout=hit(at_ms).layout,
        ocr=ResultOcr(
            header=ResultHeader(
                '', 'unknown', duration_seconds, None, None, None, None
            ),
            players=(),
        ),
        heroes=(),
        confidence=1,
    )


def test_live_point_uses_snapshot_timestamp_and_new_model_outputs(monkeypatch) -> None:
    frame = RgbFrame(2, 1, b'\x00\x00\x00' * 2)

    class Sampler:
        def probe(self, _path: str) -> VideoProfile:
            return VideoProfile(width=2, height=1, duration_ms=60_000)

        def frame_at(self, _path: str, at_ms: int) -> RgbFrame:
            assert at_ms == 59_999
            return frame

    class Classifier:
        def classify(self, _frame: RgbFrame) -> StagePrediction:
            return StagePrediction(
                content=CONTENT_VAINGLORY,
                content_conf=0.99,
                stage=STAGE_OUT_OF_MATCH,
                stage_conf=0.96,
                mode=MODE_3V3,
                mode_conf=0.8,
                model_version='package-v1',
                match_flow_label='not_match_flow',
                match_flow_conf=0.94,
                hero_select_label='not_select',
                hero_select_conf=0.93,
                match_mode_label='3v3',
                match_mode_conf=0.8,
                result_conf=0.03,
            )

    monkeypatch.setattr(analyzer_module, '_high_quality_training_jpeg', lambda _f: b'j')
    analyzer = VaingloryVideoAnalyzer(sampler=Sampler(), stage_classifier=Classifier())

    result = analyzer.classify_live_point(
        VideoPart(id=1, index=1, path='snapshot'), at_ms=90_000
    )

    assert result.observed_at_ms == 59_999
    assert result.match_flow_label == 'not_match_flow'
    assert result.model_version == 'package-v1'
    assert result.image_jpeg == b'j'


def test_result_hits_collapse_repeated_frames_and_overlapping_windows() -> None:
    collapsed = collapse_result_hits(
        (
            hit(10_000),
            hit(10_250),
            hit(10_500),
            hit(10_250),
            hit(10_500),
            hit(10_750),
            hit(40_000),
            hit(40_250),
        )
    )

    assert [item.at_ms for item in collapsed] == [10_500, 40_250]


def test_model_package_run_mode_prefers_hero_selection_evidence() -> None:
    observations = (
        ClassifiedObservation(0, STAGE_PRE_MATCH, 0.9, MODE_ARAM, CONTENT_VAINGLORY),
        ClassifiedObservation(
            5_000, STAGE_PRE_MATCH, 0.9, MODE_ARAM, CONTENT_VAINGLORY
        ),
        ClassifiedObservation(10_000, STAGE_GAMEPLAY, 0.9, MODE_3V3, CONTENT_VAINGLORY),
        ClassifiedObservation(15_000, STAGE_GAMEPLAY, 0.9, MODE_3V3, CONTENT_VAINGLORY),
    )

    assert _model_package_run_modes(observations) == {0: 'aram'}


def test_model_package_mode_jitter_is_saved_without_changing_locked_mode() -> None:
    observations = (
        ClassifiedObservation(0, STAGE_PRE_MATCH, 0.99, MODE_5V5, CONTENT_VAINGLORY),
        ClassifiedObservation(
            60_000, STAGE_GAMEPLAY, 0.99, MODE_5V5, CONTENT_VAINGLORY, 0.98
        ),
        ClassifiedObservation(
            120_000, STAGE_GAMEPLAY, 0.99, MODE_3V3, CONTENT_VAINGLORY, 0.97
        ),
        ClassifiedObservation(
            180_000, STAGE_GAMEPLAY, 0.99, MODE_5V5, CONTENT_VAINGLORY, 0.99
        ),
    )
    segments = ((0, 180_000),)

    modes = _model_package_run_modes(observations, run_gap_ms=75_000)
    conflicts = _model_package_mode_conflicts(observations, segments, modes)

    assert modes == {0: '5v5'}
    assert [
        (item.segment_start_ms, item.at_ms, item.predicted_mode, item.stable_mode)
        for item in conflicts
    ] == [(0, 120_000, '3v3', '5v5')]


def test_ten_hud_positions_disprove_three_player_mode() -> None:
    assert _apply_hud_team_size_evidence(
        {0: '3v3'}, {0: tuple('hero-{}'.format(index) for index in range(10))}
    ) == {0: '5v5'}
    assert _apply_hud_team_size_evidence(
        {0: '5v5'}, {0: tuple('hero-{}'.format(index) for index in range(6))}
    ) == {0: '5v5'}


def test_avatar_screen_context_separates_top_hud_and_vertical_scoreboard() -> None:
    frame = RgbFrame(100, 50, b'\x00\x00\x00' * 5_000)
    hud = tuple(
        HeroAvatarDetection(PixelRect(1 + index * 9, 3, 8 + index * 9, 8), 0.9)
        for index in range(10)
    )
    scoreboard = tuple(
        HeroAvatarDetection(
            PixelRect(
                30 if index < 3 else 65,
                9 + (index % 3) * 10,
                35 if index < 3 else 70,
                14 + (index % 3) * 10,
            ),
            0.85,
        )
        for index in range(6)
    )

    assert _avatar_screen_context(frame, hud) == ('gameplay_hud', 5, 0.9)
    assert _avatar_screen_context(frame, scoreboard) == ('scoreboard', 3, 0.85)
    assert _avatar_screen_context(frame, scoreboard[:5]) is None


def test_avatar_screen_probe_keeps_spread_clear_hud_and_best_scoreboard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frames = {
        at_ms: RgbFrame(100, 50, bytes([index, 0, 0]) * 5_000)
        for index, at_ms in enumerate((10_000, 30_000, 60_000, 80_000), 1)
    }

    class Sampler:
        def classify_window_frames(
            self, _path: str, _window: ScanWindow, *, interval_seconds: int
        ):
            assert interval_seconds == 15
            return tuple(TimedFrame(at_ms, frame) for at_ms, frame in frames.items())

    class Detector:
        def detect(self, frame: RgbFrame):
            marker = frame.pixels[0]
            if marker in {1, 2, 4}:
                confidence = {1: 0.7, 2: 0.95, 4: 0.9}[marker]
                return tuple(
                    HeroAvatarDetection(
                        PixelRect(1 + index * 15, 3, 8 + index * 15, 8), confidence
                    )
                    for index in range(6)
                )
            return tuple(
                HeroAvatarDetection(
                    PixelRect(
                        30 if index < 3 else 65,
                        9 + (index % 3) * 10,
                        35 if index < 3 else 70,
                        14 + (index % 3) * 10,
                    ),
                    0.88,
                )
                for index in range(6)
            )

    monkeypatch.setattr(
        analyzer_module, 'jpeg_bytes', lambda frame: b'jpeg-' + frame.pixels
    )
    analyzer = VaingloryVideoAnalyzer(
        sampler=Sampler(), hero_avatar_detector=Detector()  # type: ignore[arg-type]
    )

    candidates = analyzer._probe_avatar_screen_candidates(
        'unused', ((0, 90_000),), {0: '3v3'}, cancelled=None, interval_seconds=15
    )

    assert [
        (item.at_ms, item.stage_class, item.suggestion_confidence)
        for item in candidates
    ] == [
        (30_000, 'gameplay_hud', 0.95),
        (60_000, 'scoreboard', 0.88),
        (80_000, 'gameplay_hud', 0.9),
    ]
    assert all('清晰' in item.selection_reason for item in candidates)


def test_mode_conflict_candidates_are_selected_before_regular_samples() -> None:
    def candidate(at_ms: int, *, reason: str) -> TrainingCandidate:
        return TrainingCandidate(
            at_ms=at_ms,
            segment_start_ms=0,
            image_jpeg=b'image',
            model_version='vision-package-v1',
            suggested_label='3v3',
            suggestion_confidence=0.9,
            stage_class='gameplay',
            stage_confidence=0.9,
            mode_class='3v3',
            mode_confidence=0.9,
            selection_reason=reason,
            task='match_mode',
        )

    conflict = candidate(120_000, reason='模式冲突')
    regular = tuple(
        candidate(index * 60_000, reason='正常代表帧') for index in range(1, 40)
    )

    selected = _selected_model_package_candidates((conflict,), regular, (), (), (), ())
    capped = _cap_training_candidate_timestamps(selected, maximum_timestamps=24)

    assert conflict in selected
    assert conflict in capped


def test_refined_boundary_frames_feed_the_same_training_candidate_pools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        analyzer_module, 'jpeg_bytes', lambda _frame: b'\xff\xd8candidate\xff\xd9'
    )
    frame = RgbFrame(4, 4, b'\x00\x00\x00' * 16)
    representative = []
    borderline = []
    result_candidates = []
    prediction = StagePrediction(
        CONTENT_VAINGLORY,
        0.9,
        STAGE_PRE_MATCH,
        0.88,
        MODE_ARAM,
        0.87,
        model_version='vision-package-v1',
        match_flow_label='not_match_flow',
        match_flow_conf=0.81,
        hero_select_label='select_aram',
        hero_select_conf=0.88,
        match_mode_label='aram',
        match_mode_conf=0.87,
    )

    _remember_model_package_prediction_candidates(
        representative,
        borderline,
        result_candidates,
        prediction=prediction,
        timed=TimedFrame(45_000, frame, sample_source='keyframe'),
        model_package_id='vision-package-v1',
        result_model_version='result-v1',
        selection_context='新模型边界局部复核',
    )

    assert any(
        item.task == 'hero_select' and item.suggested_label == 'select_aram'
        for item in representative
    )
    assert any('边界局部复核' in item.selection_reason for item in borderline)
    assert [item.suggested_label for item in result_candidates] == ['no_result_panel']


def test_timeline_refinement_only_targets_state_change_intervals() -> None:
    observations = (
        ClassifiedObservation(0, STAGE_OUT_OF_MATCH, 0.99, MODE_3V3, CONTENT_VAINGLORY),
        ClassifiedObservation(
            60_000, STAGE_OUT_OF_MATCH, 0.99, MODE_3V3, CONTENT_VAINGLORY
        ),
        ClassifiedObservation(
            120_000, STAGE_GAMEPLAY, 0.99, MODE_5V5, CONTENT_VAINGLORY
        ),
        ClassifiedObservation(
            180_000, STAGE_GAMEPLAY, 0.99, MODE_5V5, CONTENT_VAINGLORY
        ),
        ClassifiedObservation(
            240_000, STAGE_OUT_OF_MATCH, 0.99, MODE_3V3, CONTENT_VAINGLORY
        ),
    )
    assert _timeline_refinement_windows(observations, duration_ms=300_000) == (
        ScanWindow(60_000, 120_000),
        ScanWindow(180_000, 240_000),
    )


def test_cancelled_hero_selection_segment_is_not_scanned_for_results() -> None:
    observations = (
        ClassifiedObservation(0, STAGE_PRE_MATCH, 0.9, MODE_3V3, CONTENT_VAINGLORY),
        ClassifiedObservation(
            60_000, STAGE_PRE_MATCH, 0.9, MODE_3V3, CONTENT_VAINGLORY
        ),
        ClassifiedObservation(
            120_000, STAGE_OUT_OF_MATCH, 0.9, MODE_3V3, CONTENT_VAINGLORY
        ),
        ClassifiedObservation(
            180_000, STAGE_GAMEPLAY, 0.9, MODE_3V3, CONTENT_VAINGLORY
        ),
    )

    assert _segments_with_gameplay(((0, 60_000), (180_000, 180_000)), observations) == (
        (180_000, 180_000),
    )


def test_model_package_cascade_keeps_source_timeline_and_direct_candidates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        analyzer_module, 'jpeg_bytes', lambda _frame: b'\xff\xd8candidate\xff\xd9'
    )
    frame = RgbFrame(4, 4, b'\x00\x00\x00' * 16)

    class Sampler:
        def probe(self, _path: str) -> VideoProfile:
            return VideoProfile(width=4, height=4, duration_ms=25_000)

        def classify_frames(self, _path: str):
            for target_ms, at_ms, source in (
                (0, 400, 'keyframe'),
                (5_000, 5_200, 'keyframe'),
                (10_000, 10_000, 'seek_fill'),
                (15_000, 15_100, 'keyframe'),
                (20_000, 20_000, 'seek_fill'),
            ):
                yield TimedFrame(
                    at_ms=at_ms, frame=frame, target_ms=target_ms, sample_source=source
                )

        def fine_frames(self, *_args, **_kwargs):
            return iter(())

    predictions = iter(
        (
            StagePrediction(
                0,
                0.9,
                STAGE_PRE_MATCH,
                0.92,
                MODE_3V3,
                0.92,
                model_version='vision-package-v1',
                match_flow_label='match_flow',
                match_flow_conf=0.9,
                hero_select_label='select_3v3',
                hero_select_conf=0.92,
                match_mode_label='3v3',
                match_mode_conf=0.92,
            ),
            StagePrediction(
                0,
                0.91,
                STAGE_PRE_MATCH,
                0.93,
                MODE_3V3,
                0.93,
                model_version='vision-package-v1',
                match_flow_label='match_flow',
                match_flow_conf=0.91,
                hero_select_label='select_3v3',
                hero_select_conf=0.93,
                match_mode_label='3v3',
                match_mode_conf=0.93,
            ),
            StagePrediction(
                0,
                0.95,
                STAGE_GAMEPLAY,
                0.9,
                MODE_3V3,
                0.88,
                model_version='vision-package-v1',
                match_flow_label='match_flow',
                match_flow_conf=0.95,
                hero_select_label='not_select',
                hero_select_conf=0.9,
                match_mode_label='3v3',
                match_mode_conf=0.88,
            ),
            StagePrediction(
                0,
                0.96,
                STAGE_GAMEPLAY,
                0.91,
                MODE_3V3,
                0.89,
                model_version='vision-package-v1',
                match_flow_label='match_flow',
                match_flow_conf=0.96,
                hero_select_label='not_select',
                hero_select_conf=0.91,
                match_mode_label='3v3',
                match_mode_conf=0.89,
            ),
            StagePrediction(
                0,
                0.94,
                STAGE_OUT_OF_MATCH,
                0.94,
                MODE_3V3,
                0,
                model_version='vision-package-v1',
                match_flow_label='not_match_flow',
                match_flow_conf=0.94,
            ),
        )
    )

    class Classifier:
        model_version = 'vision-package-v1'

        def classify(self, _frame: RgbFrame) -> StagePrediction:
            return next(predictions)

    analyzer = VaingloryVideoAnalyzer(
        sampler=Sampler(), stage_classifier=Classifier(), result_panel_detector=object()
    )
    monkeypatch.setattr(analyzer, '_detect_result_layout', lambda _frame: None)
    monkeypatch.setattr(analyzer, '_tail_regression', lambda *_args, **_kwargs: 0)
    monkeypatch.setattr(analyzer, '_exit_regression', lambda *_args, **_kwargs: 0)
    monkeypatch.setattr(
        analyzer,
        '_probe_run_modes',
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError('新模型不应再跑旧开局模式探测')
        ),
    )
    statuses = []

    dense = analyzer.scan_part_cascade(
        VideoPart(id=1, index=1, path='unused'), status_callback=statuses.append
    )

    assert dense.model_package_id == 'vision-package-v1'
    assert [
        (item.target_ms, item.at_ms, item.sample_source)
        for item in dense.timeline_points
    ] == [
        (0, 400, 'keyframe'),
        (5_000, 5_200, 'keyframe'),
        (10_000, 10_000, 'seek_fill'),
        (15_000, 15_100, 'keyframe'),
        (20_000, 20_000, 'seek_fill'),
    ]
    assert {'match_flow', 'hero_select', 'match_mode'} <= {
        item.task for item in dense.training_candidates
    }
    stage_transitions = []
    for status in statuses:
        if not stage_transitions or stage_transitions[-1] != status.stage:
            stage_transitions.append(status.stage)
    assert stage_transitions == [
        'timeline_scan',
        'timeline_analysis',
        'result_scan',
        'candidate_upload',
    ]
    assert statuses[-1].keyframe_frames == 3
    assert statuses[-1].seek_fill_frames == 2


def test_avatar_detector_orders_result_heroes_before_identity_recognition() -> None:
    frame = RgbFrame(100, 100, b'\x00\x00\x00' * 10_000)
    detections = tuple(
        HeroAvatarDetection(PixelRect(x, y, x + 8, y + 8), 0.95)
        for x, y in ((60, 50), (30, 70), (60, 30), (30, 30), (60, 70), (30, 50))
    )

    class Detector:
        def detect(self, _frame: RgbFrame):
            return detections

    labels = iter(('Alpha', 'Beta', 'Gamma', 'Delta', 'Epsilon', 'Zeta'))

    class Recognizer:
        def recognize(self, _frame: RgbFrame):
            return HeroMatch(next(labels), 0.9, 10, 5)

    analyzer = VaingloryVideoAnalyzer(
        hero_avatar_detector=Detector(), hero_recognizer=Recognizer()
    )

    heroes = analyzer._recognize_detected_result_heroes(frame, hit(0).layout)

    assert heroes is not None
    assert [(hero.side, hero.slot, hero.label) for hero in heroes] == [
        ('left', 1, 'Alpha'),
        ('left', 2, 'Beta'),
        ('left', 3, 'Gamma'),
        ('right', 1, 'Delta'),
        ('right', 2, 'Epsilon'),
        ('right', 3, 'Zeta'),
    ]


def test_segment_hud_lineup_uses_full_frame_and_retries_after_one_minute() -> None:
    frame = RgbFrame(100, 50, b'\x00\x00\x00' * 5_000)

    class Sampler:
        def __init__(self) -> None:
            self.requested = []

        def frame_at(self, _path: str, at_ms: int) -> RgbFrame:
            self.requested.append(at_ms)
            return frame

    class Detector:
        def __init__(self) -> None:
            self.calls = 0

        def detect(self, _frame: RgbFrame):
            self.calls += 1
            count = 5 if self.calls == 1 else 6
            return tuple(
                HeroAvatarDetection(PixelRect(index * 10, 0, index * 10 + 8, 8), 0.9)
                for index in range(count)
            )

    class Recognizer:
        def __init__(self) -> None:
            self.index = 0

        def recognize(self, _frame: RgbFrame):
            self.index += 1
            return HeroMatch('hero-{}'.format(self.index), 0.9, 10, 5)

    sampler = Sampler()
    analyzer = VaingloryVideoAnalyzer(
        sampler=sampler, hero_avatar_detector=Detector(), hero_recognizer=Recognizer()
    )
    observations = (
        ClassifiedObservation(10_000, STAGE_GAMEPLAY, 0.9, MODE_3V3, CONTENT_VAINGLORY),
        ClassifiedObservation(40_000, STAGE_GAMEPLAY, 0.9, MODE_3V3, CONTENT_VAINGLORY),
        ClassifiedObservation(70_000, STAGE_GAMEPLAY, 0.9, MODE_3V3, CONTENT_VAINGLORY),
    )

    lineups = analyzer._recognize_segment_hud_lineups(
        'unused',
        observations,
        ((10_000, 70_000),),
        {10_000: '3v3'},
        cancelled=None,
        frame_cache={},
    )

    assert sampler.requested == [10_000, 70_000]
    assert lineups == {10_000: tuple('hero-{}'.format(index) for index in range(1, 7))}


def test_mode_conflict_probes_nearby_full_frames_for_ten_player_hud() -> None:
    frame = RgbFrame(100, 50, b'\x00\x00\x00' * 5_000)

    class Sampler:
        def __init__(self) -> None:
            self.requested = []

        def frame_at(self, _path: str, at_ms: int) -> RgbFrame:
            self.requested.append(at_ms)
            return frame

    class Detector:
        def detect(self, _frame: RgbFrame):
            return tuple(
                HeroAvatarDetection(PixelRect(index * 9, 0, index * 9 + 8, 8), 0.9)
                for index in range(10)
            )

    class Recognizer:
        def __init__(self) -> None:
            self.index = 0

        def recognize(self, _frame: RgbFrame):
            self.index += 1
            return HeroMatch('hero-{}'.format(self.index), 0.9, 10, 5)

    sampler = Sampler()
    analyzer = VaingloryVideoAnalyzer(
        sampler=sampler, hero_avatar_detector=Detector(), hero_recognizer=Recognizer()
    )
    observations = tuple(
        ClassifiedObservation(at_ms, STAGE_GAMEPLAY, 0.9, MODE_3V3, CONTENT_VAINGLORY)
        for at_ms in (10_000, 40_000, 70_000)
    )

    lineups = analyzer._recognize_segment_hud_lineups(
        'unused',
        observations,
        ((10_000, 70_000),),
        {10_000: '3v3'},
        cancelled=None,
        frame_cache={},
        mode_conflicts=(_ModeConflict(10_000, 40_000, '5v5', '3v3', 0.95),),
    )

    assert sampler.requested == [40_000]
    assert len(lineups[10_000]) == 10
    assert _apply_hud_team_size_evidence({10_000: '3v3'}, lineups) == {10_000: '5v5'}


def test_training_candidate_materialization_reuses_full_resolution_frame(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    low = b'\xff\xd8low\xff\xd9'
    high = b'\xff\xd8high\xff\xd9'
    frame = RgbFrame(1_920, 1_080, b'\x00\x00\x00' * (1_920 * 1_080))

    class Sampler:
        def __init__(self) -> None:
            self.requested = []

        def frame_at(self, _path: str, at_ms: int) -> RgbFrame:
            self.requested.append(at_ms)
            return frame

    def candidate(task: str) -> TrainingCandidate:
        return TrainingCandidate(
            at_ms=10_000,
            segment_start_ms=0,
            image_jpeg=low,
            model_version='vision-v1',
            suggested_label='match_flow',
            suggestion_confidence=0.9,
            stage_class='gameplay',
            stage_confidence=0.9,
            mode_class='3v3',
            mode_confidence=0.9,
            selection_reason='test',
            task=task,
        )

    sampler = Sampler()
    analyzer = VaingloryVideoAnalyzer(sampler=sampler)
    monkeypatch.setattr(analyzer_module, '_high_quality_training_jpeg', lambda _: high)

    refreshed = analyzer._refresh_training_candidate_images(
        'unused',
        (candidate('match_flow'), candidate('match_flow')),
        cancelled=None,
        frame_cache={},
    )

    assert sampler.requested == [10_000]
    assert [item.image_jpeg for item in refreshed] == [high, high]


def test_training_candidate_limit_keeps_all_tasks_for_selected_timestamps() -> None:
    candidates = tuple(
        TrainingCandidate(
            at_ms=at_ms,
            segment_start_ms=0,
            image_jpeg=b'\xff\xd8x\xff\xd9',
            model_version='vision-v1',
            suggested_label='match_flow',
            suggestion_confidence=0.9,
            stage_class='gameplay',
            stage_confidence=0.9,
            mode_class='3v3',
            mode_confidence=0.9,
            selection_reason='test',
            task='match_flow',
        )
        for at_ms in (10_000, 10_000, 20_000, 30_000)
    )

    selected = _cap_training_candidate_timestamps(candidates, maximum_timestamps=2)

    assert [item.at_ms for item in selected] == [10_000, 10_000, 20_000]


def test_result_candidate_uses_segment_nearest_to_window_focus() -> None:
    assert (
        _candidate_segment_start(
            205_000, focus_ms=200_000, segments=((0, 100_000), (110_000, 200_000))
        )
        == 110_000
    )


def test_result_regression_scans_backwards_in_chunks_and_stops_after_hit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    empty = RgbFrame(2, 2, b'\x00\x00\x00' * 4)
    result = RgbFrame(2, 2, b'\x01\x01\x01' * 4)

    class Sampler:
        def __init__(self) -> None:
            self.windows = []

        def fine_frames(self, _path: str, window: ScanWindow):
            self.windows.append((window.start_ms, window.end_ms))
            yield TimedFrame(
                at_ms=window.start_ms + 1_000,
                frame=result if window.start_ms == 30_000 else empty,
            )

    sampler = Sampler()
    analyzer = VaingloryVideoAnalyzer(sampler=sampler, result_panel_detector=object())
    monkeypatch.setattr(
        analyzer,
        '_detect_result_layout',
        lambda frame: hit(0).layout if frame.pixels[0] else None,
    )

    hits = []
    found = analyzer._scan_result_backwards(
        'unused',
        start_ms=0,
        end_ms=120_000,
        hits=hits,
        cancelled=None,
        training_candidates=None,
        key_screen_reason='test',
        detector_reason='test',
    )

    assert found.hit_count == 1
    assert found.decoded_frames == 2
    assert found.scanned_ms == 90_000
    assert sampler.windows == [(75_000, 120_000), (30_000, 75_000)]
    assert [item.at_ms for item in hits] == [31_000]


def test_tail_regression_skips_segment_that_already_has_a_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    analyzer = VaingloryVideoAnalyzer(result_panel_detector=object())
    monkeypatch.setattr(
        analyzer,
        '_scan_result_backwards',
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError('已有结算的对局不应再回扫')
        ),
    )

    assert (
        analyzer._tail_regression('unused', [hit(500_000)], ((100_000, 1_000_000),), ())
        == 0
    )


def test_strict_exit_regression_ignores_cancelled_bp_and_transition() -> None:
    analyzer = VaingloryVideoAnalyzer(result_panel_detector=object())
    observations = (
        ClassifiedObservation(0, STAGE_PRE_MATCH, 0.9, MODE_3V3, CONTENT_VAINGLORY),
        ClassifiedObservation(
            60_000, STAGE_OUT_OF_MATCH, 0.9, MODE_3V3, CONTENT_VAINGLORY
        ),
        ClassifiedObservation(
            120_000, STAGE_GAMEPLAY, 0.9, MODE_3V3, CONTENT_VAINGLORY
        ),
        ClassifiedObservation(
            180_000, STAGE_TRANSITION, 0.9, MODE_3V3, CONTENT_VAINGLORY
        ),
    )

    assert (
        analyzer._exit_regression('unused', observations, [], strict_gameplay_exit=True)
        == 0
    )


def test_opening_probe_keeps_bp_and_hard_negative_training_frames(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frame = RgbFrame(1, 1, b'\x00\x00\x00')
    observations = (
        ClassifiedObservation(0, STAGE_PRE_MATCH, 0.9, MODE_ARAM, CONTENT_VAINGLORY),
        ClassifiedObservation(
            5_000, STAGE_PRE_MATCH, 0.9, MODE_ARAM, CONTENT_VAINGLORY
        ),
        ClassifiedObservation(10_000, STAGE_GAMEPLAY, 0.9, MODE_3V3, CONTENT_VAINGLORY),
        ClassifiedObservation(30_000, STAGE_GAMEPLAY, 0.9, MODE_3V3, CONTENT_VAINGLORY),
    )

    class Sampler:
        def fine_frames(self, _path: str, _window: ScanWindow):
            for at_ms in (0, 2_500, 7_500):
                yield TimedFrame(at_ms=at_ms, frame=frame)

    predictions = iter(
        (
            StagePrediction(0, 1, STAGE_PRE_MATCH, 0.9, MODE_ARAM, 0.8),
            StagePrediction(0, 1, STAGE_PRE_MATCH, 0.95, MODE_ARAM, 0.85),
            StagePrediction(0, 1, STAGE_GAMEPLAY, 0.9, MODE_3V3, 0.7),
        )
    )

    class Classifier:
        def classify(self, _frame: RgbFrame) -> StagePrediction:
            return next(predictions)

    monkeypatch.setattr(analyzer_module, 'smooth_stages', lambda _items: observations)
    monkeypatch.setattr(analyzer_module, '_pre_match_anchors', lambda _items: ())
    monkeypatch.setattr(
        analyzer_module, '_confirmed_anchors', lambda _anchors, _items: ()
    )
    monkeypatch.setattr(
        analyzer_module,
        'gameplay_runs',
        lambda _items: ((observations[0], observations[-1]),),
    )
    monkeypatch.setattr(
        analyzer_module, '_segment_ranges', lambda _runs, _anchors: ((0, 30_000),)
    )
    monkeypatch.setattr(
        analyzer_module, 'jpeg_bytes', lambda _frame: b'\xff\xd8candidate\xff\xd9'
    )
    analyzer = VaingloryVideoAnalyzer(
        sampler=Sampler(), stage_classifier=Classifier()  # type: ignore[arg-type]
    )

    _modes, candidates = analyzer._probe_run_modes('unused', observations)

    assert len(candidates) == 3
    assert [item.suggested_label for item in candidates] == [
        'bp_aram',
        'bp_aram',
        'not_bp',
    ]
    assert all(item.image_jpeg.startswith(b'\xff\xd8') for item in candidates)


def test_key_screen_training_candidates_are_spaced_and_keep_strongest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frame = RgbFrame(1, 1, b'\x00\x00\x00')
    candidates = []
    monkeypatch.setattr(
        analyzer_module, 'jpeg_bytes', lambda _frame: b'\xff\xd8candidate\xff\xd9'
    )

    assert _remember_training_candidate(
        candidates,
        task='key_screen_review',
        suggested_label='scoreboard',
        at_ms=10_000,
        segment_start_ms=0,
        frame=frame,
        model_version='multi-v2',
        suggestion_confidence=0.6,
        stage_class='scoreboard',
        stage_confidence=0.6,
        selection_reason='粗扫候选',
        minimum_gap_ms=20_000,
        maximum_per_label=2,
    )
    assert not _remember_training_candidate(
        candidates,
        task='key_screen_review',
        suggested_label='scoreboard',
        at_ms=15_000,
        segment_start_ms=0,
        frame=frame,
        model_version='multi-v2',
        suggestion_confidence=0.5,
        stage_class='scoreboard',
        stage_confidence=0.5,
        selection_reason='较弱候选',
        minimum_gap_ms=20_000,
        maximum_per_label=2,
    )
    assert _remember_training_candidate(
        candidates,
        task='key_screen_review',
        suggested_label='scoreboard',
        at_ms=16_000,
        segment_start_ms=0,
        frame=frame,
        model_version='multi-v2',
        suggestion_confidence=0.9,
        stage_class='scoreboard',
        stage_confidence=0.9,
        selection_reason='较强候选',
        minimum_gap_ms=20_000,
        maximum_per_label=2,
    )

    assert len(candidates) == 1
    assert candidates[0].task == 'key_screen_review'
    assert candidates[0].at_ms == 16_000
    assert candidates[0].suggestion_confidence == 0.9


def test_in_match_training_candidates_keep_each_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frame = RgbFrame(1, 1, b'\x00\x00\x00')
    candidates = []
    monkeypatch.setattr(
        analyzer_module, 'jpeg_bytes', lambda _frame: b'\xff\xd8candidate\xff\xd9'
    )

    for at_ms, mode in ((10_000, '3v3'), (20_000, '5v5')):
        assert _remember_training_candidate(
            candidates,
            task='screen_state',
            suggested_label='in_match',
            at_ms=at_ms,
            segment_start_ms=0,
            frame=frame,
            model_version='multi-v2',
            suggestion_confidence=0.9,
            stage_class='in_match',
            stage_confidence=0.9,
            mode_class=mode,
            mode_confidence=0.9,
            selection_reason='对局画面候选',
            minimum_gap_ms=60_000,
            maximum_per_label=1,
            separate_modes=True,
        )

    assert [candidate.mode_class for candidate in candidates] == ['3v3', '5v5']


def test_borderline_training_candidates_keep_lowest_accepted_confidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frame = RgbFrame(1, 1, b'\x00\x00\x00')
    candidates = []
    monkeypatch.setattr(
        analyzer_module, 'jpeg_bytes', lambda _frame: b'\xff\xd8candidate\xff\xd9'
    )

    for confidence in (0.72, 0.61, 0.68):
        _remember_training_candidate(
            candidates,
            task='result_detector',
            suggested_label='result_panel',
            at_ms=int(confidence * 1_000),
            segment_start_ms=0,
            frame=frame,
            model_version='result-detector-v1',
            suggestion_confidence=confidence,
            stage_class='result_page',
            stage_confidence=confidence,
            selection_reason='结算模型边界样本',
            minimum_gap_ms=15_000,
            maximum_per_label=1,
            prefer_lower_confidence=True,
        )

    assert len(candidates) == 1
    assert candidates[0].suggestion_confidence == 0.61


def test_result_hit_keeps_layout_from_the_strongest_frame() -> None:
    collapsed = collapse_result_hits(
        (hit(10_000, 0.8), hit(10_250, 1.0), hit(10_500, 0.9))
    )

    assert len(collapsed) == 1
    assert collapsed[0].layout.confidence == 1.0


def test_completed_matches_collapse_by_their_estimated_game_start() -> None:
    collapsed = collapse_analyzed_matches(
        (
            analyzed_match(600_000, 590),
            analyzed_match(610_000, 600),
            analyzed_match(1_200_000, 500),
        )
    )

    assert [item.result_at_ms for item in collapsed] == [610_000, 1_200_000]


def test_replayed_result_with_identical_content_is_kept_but_excluded() -> None:
    players = tuple(
        OcrPlayer(
            side=side,
            slot=slot,
            name='{}{}'.format(side, slot),
            normalized_name='{}{}'.format(side, slot),
            stats=PlayerStats(slot, slot, slot, 10_000 + slot),
            confidence=1,
        )
        for side in ('left', 'right')
        for slot in range(1, 4)
    )
    first = replace(
        analyzed_match(600_000, 590),
        ocr=replace(analyzed_match(600_000, 590).ocr, players=players),
    )
    replay = replace(first, result_at_ms=1_200_000)

    deduplicated = exclude_content_duplicates((first, replay))

    assert len(deduplicated) == 2
    assert deduplicated[0].stats_eligible is True
    assert deduplicated[1].stats_eligible is False
    assert deduplicated[1].stats_exclusion_reason == 'duplicate'


def test_observed_result_does_not_exclude_a_later_played_copy() -> None:
    players = tuple(
        OcrPlayer(
            side=side,
            slot=slot,
            name='{}{}'.format(side, slot),
            normalized_name='{}{}'.format(side, slot),
            stats=PlayerStats(slot, slot, slot, 10_000 + slot),
            confidence=1,
        )
        for side in ('left', 'right')
        for slot in range(1, 4)
    )
    played = replace(
        analyzed_match(1_200_000, 590),
        ocr=replace(analyzed_match(1_200_000, 590).ocr, players=players),
    )
    observed = replace(
        played,
        result_at_ms=600_000,
        view_context='observed',
        stats_eligible=False,
        stats_exclusion_reason='observed',
    )

    deduplicated = exclude_content_duplicates((observed, played))

    assert deduplicated[0].stats_exclusion_reason == 'observed'
    assert deduplicated[1].stats_eligible is True


def test_result_confirmation_does_not_depend_on_the_client_language() -> None:
    analyzer = VaingloryVideoAnalyzer()
    header = ResultHeader('', 'unknown', 479, 9, 18, 40_900, 45_800)

    assert analyzer._is_result_header(header) is True
    assert analyzer._is_completed_match(header) is True


def test_short_3v3_is_retained_but_excluded_from_statistics() -> None:
    assert stats_eligibility(
        game_mode='3v3', duration_seconds=179, match_kind='pvp', view_context='played'
    ) == (False, 'too_short_3v3')
    assert stats_eligibility(
        game_mode='aram', duration_seconds=179, match_kind='pvp', view_context='played'
    ) == (True, '')


def test_observed_bot_and_practice_matches_are_excluded_from_statistics() -> None:
    assert stats_eligibility(
        game_mode='3v3', duration_seconds=900, match_kind='pvp', view_context='observed'
    ) == (False, 'observed')
    assert stats_eligibility(
        game_mode='3v3', duration_seconds=900, match_kind='bot', view_context='played'
    ) == (False, 'bot')
    assert stats_eligibility(
        game_mode='3v3',
        duration_seconds=900,
        match_kind='practice',
        view_context='played',
    ) == (False, 'practice')


def test_three_player_bot_names_preserve_spaces_for_classification() -> None:
    players = tuple(
        OcrPlayer(
            side='left',
            slot=slot,
            name=name.replace(' ', ''),
            normalized_name=name.replace(' ', '').casefold(),
            stats=PlayerStats(1, 1, 1, 1_000),
            confidence=1,
            raw_name=name,
        )
        for slot, name in enumerate(('主播', 'Alpha Bot', 'Beta Bot'), 1)
    )

    assert (
        classify_match_kind(
            ResultOcr(ResultHeader('', 'normal', 900, 1, 1, 1, 1), players),
            (),
            team_size=3,
        )
        == 'bot'
    )


def test_five_player_bot_names_must_match_multiple_recognized_heroes() -> None:
    players = tuple(
        OcrPlayer(
            side='left',
            slot=slot,
            name=raw_name.replace(' ', ''),
            normalized_name=raw_name.replace(' ', '').casefold(),
            stats=PlayerStats(1, 1, 1, 1_000),
            confidence=1,
            raw_name=raw_name,
        )
        for slot, raw_name in enumerate(('主播', '无情 凯恩', '盲暴 格雷'), 1)
    )
    heroes = (
        AnalyzedHero('left', 1, '0' * 16, b'', label='Caine'),
        AnalyzedHero('left', 2, '1' * 16, b'', label='Glaive'),
    )

    assert (
        classify_match_kind(
            ResultOcr(ResultHeader('', 'normal', 900, 1, 1, 1, 1), players),
            heroes,
            team_size=5,
        )
        == 'bot'
    )


def test_one_visible_player_is_classified_as_practice() -> None:
    player = OcrPlayer(
        side='left',
        slot=1,
        name='主播',
        normalized_name='主播',
        stats=PlayerStats(1, 0, 0, 1_000),
        confidence=1,
    )
    hero = AnalyzedHero(
        side='left', slot=1, fingerprint='0' * 16, thumbnail_png=b'', label='Caine'
    )

    assert (
        classify_match_kind(
            ResultOcr(ResultHeader('', 'normal', 120, 1, 0, 1, 0), (player,)),
            (hero,),
            team_size=3,
        )
        == 'practice'
    )


def test_player_ocr_keeps_the_primary_visual_layout_when_header_fallback_wins() -> None:
    primary = ResultLayout(
        left_color='teal',
        right_color='orange',
        winner_color='orange',
        winner_side='right',
        confidence=1,
        viewport=ViewportTransform('responsive', 0.05, 0.05, 0.9, 0.9, 'wide'),
    )
    fallback = hit(0).layout
    incomplete = ResultHeader('战败', 'normal', None, 16, 10, 52_900, 53_600)
    completed = ResultHeader('战败', 'normal', 1277, 16, 10, None, 53_600)
    analyzer = VaingloryVideoAnalyzer()

    layout, header = analyzer._select_ocr_context(
        (primary, fallback), ((primary, incomplete), (fallback, completed))
    )

    assert layout is primary
    assert header is not None
    assert header.duration_seconds == 1277
    assert header.left_economy == 52_900


def test_name_ocr_samples_the_same_result_slot_from_nearby_frames(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frame = RgbFrame(1, 1, b'\x00\x00\x00')

    class Sampler:
        calls = []

        def frame_at(self, _path: str, at_ms: int) -> RgbFrame:
            self.calls.append(at_ms)
            return frame

    sampler = Sampler()
    analyzer = VaingloryVideoAnalyzer(sampler=sampler)  # type: ignore[arg-type]
    layout = hit(0).layout
    monkeypatch.setattr(analyzer, '_detect_result_layout', lambda _frame: layout)

    nearby = analyzer._sample_nearby_result_frames(
        'unused', at_ms=1_000, duration_ms=2_000
    )

    assert sampler.calls == [0, 500, 1_500, 1_999]
    assert nearby == tuple((frame, layout) for _index in range(4))


def test_name_ocr_skips_an_unreadable_optional_nearby_frame(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frame = RgbFrame(1, 1, b'\x00\x00\x00')

    class Sampler:
        calls = []

        def frame_at(self, _path: str, at_ms: int) -> RgbFrame:
            self.calls.append(at_ms)
            if at_ms == 0:
                raise RuntimeError('unreadable tail frame')
            return frame

    sampler = Sampler()
    analyzer = VaingloryVideoAnalyzer(sampler=sampler)  # type: ignore[arg-type]
    layout = hit(0).layout
    monkeypatch.setattr(analyzer, '_detect_result_layout', lambda _frame: layout)

    nearby = analyzer._sample_nearby_result_frames(
        'unused', at_ms=1_000, duration_ms=2_000
    )

    assert sampler.calls == [0, 500, 1_500, 1_999]
    assert nearby == tuple((frame, layout) for _index in range(3))


def test_aram_detection_samples_the_talent_selection_near_estimated_start() -> None:
    frame = RgbFrame(1, 1, b'\x00\x00\x00')

    class Sampler:
        calls = []

        def frame_at(self, _path: str, at_ms: int) -> RgbFrame:
            self.calls.append(at_ms)
            return frame

    class Detector:
        def is_visible(self, _frame: RgbFrame) -> bool:
            return len(Sampler.calls) >= 2

    sampler = Sampler()
    analyzer = VaingloryVideoAnalyzer(
        sampler=sampler,  # type: ignore[arg-type]
        aram_detector=Detector(),  # type: ignore[arg-type]
    )

    mode = analyzer._detect_game_mode(
        'unused',
        result_at_ms=600_000,
        duration_seconds=590,
        video_duration_ms=700_000,
        team_size=3,
    )

    assert mode == 'aram'
    assert sampler.calls == [11_000, 13_000, 15_000]


def test_result_frame_mode_skips_opening_rescan_when_confident() -> None:
    frame = RgbFrame(1, 1, b'\x00\x00\x00')

    class Sampler:
        def fine_frames(self, _path: str, _window: ScanWindow):
            raise AssertionError('confident result mode must not rescan the opening')

    class ModeClassifier:
        def predict(self, predicted_frame: RgbFrame):
            assert predicted_frame is frame
            return SimpleNamespace(label='aram', confidence=0.98)

    analyzer = VaingloryVideoAnalyzer(
        sampler=Sampler(),  # type: ignore[arg-type]
        match_mode_classifier=ModeClassifier(),  # type: ignore[arg-type]
        minimum_result_mode_confidence=0.75,
    )

    mode = analyzer._detect_game_mode(
        'unused',
        result_at_ms=600_000,
        duration_seconds=590,
        video_duration_ms=700_000,
        team_size=3,
        result_frame=frame,
    )

    assert mode == 'aram'


def test_result_frame_mode_conflict_keeps_existing_fallback() -> None:
    frame = RgbFrame(1, 1, b'\x00\x00\x00')

    class ModeClassifier:
        def predict(self, _frame: RgbFrame):
            return SimpleNamespace(label='aram', confidence=0.99)

    analyzer = VaingloryVideoAnalyzer(
        match_mode_classifier=ModeClassifier(),  # type: ignore[arg-type]
        minimum_result_mode_confidence=0.75,
    )

    mode = analyzer._detect_game_mode(
        'unused',
        result_at_ms=600_000,
        duration_seconds=None,
        video_duration_ms=700_000,
        team_size=3,
        hint='3v3',
        result_frame=frame,
    )

    assert mode == '3v3'


def test_aram_detection_rejects_one_noisy_circle_match() -> None:
    frame = RgbFrame(1, 1, b'\x00\x00\x00')

    class Sampler:
        calls = []

        def frame_at(self, _path: str, at_ms: int) -> RgbFrame:
            self.calls.append(at_ms)
            return frame

    class Detector:
        def is_visible(self, _frame: RgbFrame) -> bool:
            return len(Sampler.calls) == 3

    sampler = Sampler()
    analyzer = VaingloryVideoAnalyzer(
        sampler=sampler,  # type: ignore[arg-type]
        aram_detector=Detector(),  # type: ignore[arg-type]
    )

    mode = analyzer._detect_game_mode(
        'unused',
        result_at_ms=600_000,
        duration_seconds=590,
        video_duration_ms=700_000,
        team_size=3,
    )

    assert mode == '3v3'
    assert sampler.calls == [11_000, 13_000, 15_000]


def test_aram_detection_fails_safe_when_match_start_is_outside_the_part() -> None:
    class Sampler:
        def frame_at(self, _path: str, _at_ms: int) -> RgbFrame:
            raise AssertionError('must not sample before the current video part')

    class Detector:
        def is_visible(self, _frame: RgbFrame) -> bool:
            return True

    analyzer = VaingloryVideoAnalyzer(
        sampler=Sampler(),  # type: ignore[arg-type]
        aram_detector=Detector(),  # type: ignore[arg-type]
    )

    assert (
        analyzer._detect_game_mode(
            'unused',
            result_at_ms=200_000,
            duration_seconds=590,
            video_duration_ms=700_000,
            team_size=3,
        )
        == 'unknown'
    )


def test_aram_detection_ignores_an_unreadable_optional_frame() -> None:
    frame = RgbFrame(1, 1, b'\x00\x00\x00')

    class Sampler:
        calls = []

        def frame_at(self, _path: str, at_ms: int) -> RgbFrame:
            self.calls.append(at_ms)
            if len(self.calls) == 1:
                raise RuntimeError('unreadable')
            return frame

    class Detector:
        def is_visible(self, _frame: RgbFrame) -> bool:
            return False

    sampler = Sampler()
    analyzer = VaingloryVideoAnalyzer(
        sampler=sampler,  # type: ignore[arg-type]
        aram_detector=Detector(),  # type: ignore[arg-type]
    )

    mode = analyzer._detect_game_mode(
        'unused',
        result_at_ms=600_000,
        duration_seconds=590,
        video_duration_ms=700_000,
        team_size=3,
    )

    assert mode == '3v3'
    assert sampler.calls == [11_000, 13_000, 15_000]


def test_video_analysis_stops_between_sampled_frames() -> None:
    frame = RgbFrame(1, 1, b'\x00\x00\x00')

    class Sampler:
        def probe(self, _path: str) -> VideoProfile:
            return VideoProfile(width=1, height=1, duration_ms=1_000)

        def coarse_frames(self, _path: str):
            yield TimedFrame(at_ms=0, frame=frame)

    analyzer = VaingloryVideoAnalyzer(sampler=Sampler())  # type: ignore[arg-type]

    with pytest.raises(AnalysisCancelled):
        analyzer.analyze_part(
            VideoPart(id=1, index=1, path='unused'), cancelled=lambda: True
        )


def test_coarse_scan_runs_expensive_result_fallback_only_every_two_minutes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frame = RgbFrame(1, 1, b'\x00\x00\x00')

    class Sampler:
        def probe(self, _path: str) -> VideoProfile:
            return VideoProfile(width=1, height=1, duration_ms=180_000)

        def coarse_frames(self, _path: str):
            for at_ms in range(0, 180_000, 30_000):
                yield TimedFrame(at_ms=at_ms, frame=frame)

    analyzer = VaingloryVideoAnalyzer(sampler=Sampler())  # type: ignore[arg-type]
    result_probes = []
    monkeypatch.setattr(
        analyzer_module, 'detect_gameplay_hud_details', lambda _frame: None
    )
    monkeypatch.setattr(analyzer_module, 'detect_observer_hud', lambda _frame: None)
    monkeypatch.setattr(
        analyzer,
        '_detect_result_layout',
        lambda _frame: result_probes.append(True) and None,
    )

    scanned = analyzer.scan_part(VideoPart(id=1, index=1, path='unused'))

    assert scanned.candidate_times_ms == ()
    assert len(result_probes) == 2


def test_scan_logs_coarse_progress_and_each_fine_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frame = RgbFrame(1, 1, b'\x00\x00\x00')

    class Sampler:
        def probe(self, _path: str) -> VideoProfile:
            return VideoProfile(width=1, height=1, duration_ms=600_000)

        def coarse_frames(self, _path: str):
            for at_ms in range(0, 600_000, 60_000):
                yield TimedFrame(at_ms=at_ms, frame=frame)

    windows = (ScanWindow(100_000, 170_000), ScanWindow(300_000, 370_000))
    scan_results = iter(
        (
            analyzer_module._WindowScanResult((hit(120_000),), 3, 4, 5, 1),
            analyzer_module._WindowScanResult((), 6, 7, 0, 0),
        )
    )
    analyzer = VaingloryVideoAnalyzer(sampler=Sampler())  # type: ignore[arg-type]
    monkeypatch.setattr(
        analyzer_module, 'detect_gameplay_hud_details', lambda _frame: None
    )
    monkeypatch.setattr(analyzer_module, 'detect_observer_hud', lambda _frame: None)
    monkeypatch.setattr(analyzer, '_detect_result_layout', lambda _frame: None)
    monkeypatch.setattr(
        analyzer_module, 'result_search_windows', lambda *_args, **_kwargs: windows
    )
    monkeypatch.setattr(
        analyzer, '_scan_window', lambda *_args, **_kwargs: next(scan_results)
    )
    messages = []
    statuses = []
    sink = logger.add(messages.append, format='{message}')
    try:
        scanned = analyzer.scan_part(
            VideoPart(id=1, index=2, path='unused'), status_callback=statuses.append
        )
    finally:
        logger.remove(sink)

    message = ''.join(str(item) for item in messages)
    assert scanned.candidate_times_ms == (120_000,)
    assert 'Vainglory coarse scan progress: part_id=1' in message
    assert 'Vainglory fine scan started: part_id=1 windows=2' in message
    assert 'Vainglory fine scan window started: part_id=1 window=1/2' in message
    assert 'Vainglory fine scan window completed: part_id=1 window=2/2' in message
    assert 'candidates_so_far=1' in message
    assert statuses[-1].stage == 'fine_scan'
    assert statuses[-1].candidate_count == 1
    assert statuses[-1].current_window == 2


def test_fine_scan_checks_the_narrow_boundary_window_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    preview = RgbFrame(4, 4, b'\x00\x00\x00' * 16)
    result = RgbFrame(4, 4, b'\xff\xff\xff' * 16)

    class Sampler:
        def __init__(self) -> None:
            self.fine_windows = []

        def result_preview_frames(
            self, _path: str, window: ScanWindow, *, keyframes_only: bool
        ):
            yield TimedFrame(at_ms=window.start_ms, frame=preview)

        def fine_frames(self, _path: str, window: ScanWindow):
            self.fine_windows.append((window.start_ms, window.end_ms))
            for at_ms in range(window.start_ms, window.end_ms, 250):
                yield TimedFrame(
                    at_ms=at_ms, frame=result if at_ms == 101_250 else preview
                )

    sampler = Sampler()
    analyzer = VaingloryVideoAnalyzer(sampler=sampler)  # type: ignore[arg-type]

    def detect(frame: RgbFrame):
        return hit(0).layout if frame is result else None

    monkeypatch.setattr(analyzer, '_detect_result_layout', detect)
    statuses = []

    scanned = analyzer._scan_window(
        'unused',
        ScanWindow(90_000, 160_000, focus_ms=100_000),
        window_index=1,
        window_count=1,
        status=statuses.append,
    )

    assert [item.at_ms for item in scanned.hits] == [101_250]
    assert sampler.fine_windows == [(95_000, 125_000)]
    assert scanned.expanded_fallback is False
    assert statuses[-1] == '第 1/1 个区间：窄区间预览未命中，正在高帧率兜底'


def test_small_transition_window_runs_only_one_high_rate_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    preview = RgbFrame(4, 4, b'\x00\x00\x00' * 16)
    result = RgbFrame(4, 4, b'\xff\xff\xff' * 16)

    class Sampler:
        def __init__(self) -> None:
            self.fine_windows = []

        def result_preview_frames(self, *_args, **_kwargs):
            raise AssertionError('小变化区间不应重复预览扫描')

        def fine_frames(self, _path: str, window: ScanWindow):
            self.fine_windows.append((window.start_ms, window.end_ms))
            for at_ms in range(window.start_ms, window.end_ms, 250):
                yield TimedFrame(
                    at_ms=at_ms, frame=result if at_ms == 6_250 else preview
                )

    sampler = Sampler()
    analyzer = VaingloryVideoAnalyzer(sampler=sampler)  # type: ignore[arg-type]
    monkeypatch.setattr(
        analyzer,
        '_detect_result_layout',
        lambda frame: hit(0).layout if frame is result else None,
    )

    scanned = analyzer._scan_window(
        'unused', ScanWindow(0, 10_000, focus_ms=5_000), window_index=1, window_count=1
    )

    assert [item.at_ms for item in scanned.hits] == [6_250]
    assert sampler.fine_windows == [(0, 10_000)]
    assert scanned.keyframe_preview_frames == 0
    assert scanned.fallback_preview_frames == 0
    assert scanned.refinement_frames == 40


def test_fine_scan_expands_to_the_full_beta36_window_after_a_narrow_miss(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    preview = RgbFrame(4, 4, b'\x00\x00\x00' * 16)
    result = RgbFrame(4, 4, b'\xff\xff\xff' * 16)

    class Sampler:
        def __init__(self) -> None:
            self.fine_windows = []

        def result_preview_frames(
            self, _path: str, window: ScanWindow, *, keyframes_only: bool
        ):
            yield TimedFrame(at_ms=window.start_ms, frame=preview)

        def fine_frames(self, _path: str, window: ScanWindow):
            self.fine_windows.append((window.start_ms, window.end_ms))
            for at_ms in range(window.start_ms, window.end_ms, 250):
                yield TimedFrame(
                    at_ms=at_ms, frame=result if at_ms == 145_000 else preview
                )

    sampler = Sampler()
    analyzer = VaingloryVideoAnalyzer(sampler=sampler)  # type: ignore[arg-type]

    def detect(frame: RgbFrame):
        return hit(0).layout if frame is result else None

    monkeypatch.setattr(analyzer, '_detect_result_layout', detect)

    scanned = analyzer._scan_window(
        'unused',
        ScanWindow(90_000, 160_000, focus_ms=100_000),
        window_index=1,
        window_count=1,
    )

    assert [item.at_ms for item in scanned.hits] == [145_000]
    assert sampler.fine_windows == [
        (95_000, 125_000),
        (90_000, 95_000),
        (125_000, 160_000),
    ]
    assert scanned.expanded_fallback is True


def test_recognition_logs_each_candidate_rejection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frame = RgbFrame(1, 1, b'\x00\x00\x00')

    class Sampler:
        def frame_at(self, _path: str, _at_ms: int) -> RgbFrame:
            return frame

    analyzer = VaingloryVideoAnalyzer(sampler=Sampler())  # type: ignore[arg-type]
    monkeypatch.setattr(analyzer, '_detect_result_layouts', lambda _frame: ())
    messages = []
    statuses = []
    sink = logger.add(messages.append, format='{message}')
    try:
        matches = analyzer.recognize_scanned_part(
            VideoPart(id=1, index=2, path='unused'),
            analyzer_module.ScannedPart(600_000, (120_000, 360_000)),
            status_callback=statuses.append,
        )
    finally:
        logger.remove(sink)

    message = ''.join(str(item) for item in messages)
    assert matches == ()
    assert 'Vainglory recognition started: part_id=1 candidates=2' in message
    assert 'Vainglory candidate recognition started: part_id=1 candidate=1/2' in message
    assert 'reason=layout' in message
    assert (
        'Vainglory candidate recognition completed: part_id=1 candidate=2/2' in message
    )
    assert statuses[-1].rejected_candidates == 2
    assert statuses[-1].recognized_matches == 0


def test_coarse_scan_never_calls_the_game_timer_reader(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frame = RgbFrame(1, 1, b'\x00\x00\x00')

    class Sampler:
        def probe(self, _path: str) -> VideoProfile:
            return VideoProfile(width=1, height=1, duration_ms=60_000)

        def coarse_frames(self, _path: str):
            yield TimedFrame(at_ms=0, frame=frame)
            yield TimedFrame(at_ms=30_000, frame=frame)

        def result_preview_frames(self, *_args, **_kwargs):
            return iter(())

        def fine_frames(self, *_args, **_kwargs):
            return iter(())

    class Reader:
        timer_calls = 0

        def read_game_timer(self, _frame: RgbFrame):
            self.timer_calls += 1
            raise AssertionError('粗扫不得调用计时器 OCR')

    reader = Reader()
    analyzer = VaingloryVideoAnalyzer(
        sampler=Sampler(),  # type: ignore[arg-type]
        result_reader=reader,  # type: ignore[arg-type]
    )
    monkeypatch.setattr(
        analyzer_module,
        'detect_gameplay_hud_details',
        lambda _frame: GameplayHud('lineup', 3, 6),
    )

    analyzer.scan_part(VideoPart(id=1, index=1, path='unused'))

    assert reader.timer_calls == 0


def test_coarse_scan_recognizes_heroes_only_at_gameplay_anchors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hud_frame = RgbFrame(1, 1, b'\x01\x00\x00')
    blank_frame = RgbFrame(1, 1, b'\x00\x00\x00')

    class Sampler:
        def probe(self, _path: str) -> VideoProfile:
            return VideoProfile(width=1, height=1, duration_ms=210_000)

        def coarse_frames(self, _path: str):
            for at_ms, frame in (
                (0, hud_frame),
                (30_000, hud_frame),
                (60_000, hud_frame),
                (90_000, blank_frame),
                (120_000, blank_frame),
                (150_000, hud_frame),
                (180_000, hud_frame),
            ):
                yield TimedFrame(at_ms=at_ms, frame=frame)

        def result_preview_frames(self, *_args, **_kwargs):
            return iter(())

        def fine_frames(self, *_args, **_kwargs):
            return iter(())

    analyzer = VaingloryVideoAnalyzer(
        sampler=Sampler(),  # type: ignore[arg-type]
        hero_recognizer=object(),  # type: ignore[arg-type]
    )
    monkeypatch.setattr(
        analyzer_module,
        'detect_gameplay_hud_details',
        lambda frame: GameplayHud('lineup', 3, 6) if frame is hud_frame else None,
    )
    monkeypatch.setattr(analyzer_module, 'detect_observer_hud', lambda _frame: None)
    recognized_at = []
    monkeypatch.setattr(
        analyzer,
        '_recognize_coarse_hud_lineup',
        lambda _path, at_ms, **_kwargs: recognized_at.append(at_ms)
        or ('Alpha', 'Beta', 'Gamma', 'Delta', 'Epsilon', 'Zeta'),
        raising=False,
    )

    analyzer.scan_part(VideoPart(id=1, index=1, path='unused'))

    assert recognized_at == [0, 150_000]


def test_final_result_evidence_rejects_the_in_game_scoreboard_pattern() -> None:
    complete_players = tuple(
        OcrPlayer(
            side=side,
            slot=slot,
            name='{}{}'.format(side, slot),
            normalized_name='{}{}'.format(side, slot),
            stats=PlayerStats(slot, slot, slot, 10_000 + slot),
            confidence=1,
        )
        for side in ('left', 'right')
        for slot in range(1, 4)
    )
    valid = ResultOcr(
        ResultHeader('投降', 'surrender', 929, 3, 20, 29_600, 42_000), complete_players
    )
    scoreboard_players = tuple(
        replace(
            player,
            stats=replace(
                player.stats,
                kills=None if player.side == 'left' else player.stats.kills,
                deaths=None if player.side == 'right' else player.stats.deaths,
            ),
            confidence=0.45,
        )
        for player in complete_players
    )
    scoreboard = ResultOcr(
        ResultHeader('投降', 'surrender', 552, 100, None, 5_300, None),
        scoreboard_players,
    )

    assert VaingloryVideoAnalyzer._is_credible_result(valid, team_size=3) is True
    assert VaingloryVideoAnalyzer._is_credible_result(scoreboard, team_size=3) is False


def test_incomplete_result_evidence_stops_before_hero_recognition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frame = RgbFrame(1, 1, b'\x00\x00\x00')
    scoreboard = ResultOcr(
        ResultHeader('投降', 'surrender', 552, 100, None, 5_300, None), ()
    )

    class Reader:
        def read(self, *_args, **_kwargs) -> ResultOcr:
            return scoreboard

    analyzer = VaingloryVideoAnalyzer(result_reader=Reader())  # type: ignore[arg-type]
    monkeypatch.setattr(
        analyzer,
        '_recognize_heroes',
        lambda *_args, **_kwargs: pytest.fail('不完整候选不得进入英雄识别'),
    )
    monkeypatch.setattr(
        analyzer,
        '_classify_afk_statuses',
        lambda *_args, **_kwargs: pytest.fail('非结算页不得进入挂机识别'),
    )

    with pytest.raises(analyzer_module._ResultEvidenceRejected):
        analyzer._recognize_frame(
            frame,
            part=VideoPart(id=1, index=1, path='unused'),
            at_ms=552_000,
            layout=hit(0).layout,
            header=scoreboard.header,
            video_duration_ms=600_000,
        )


def _complete_afk_result(team_size: int = 3) -> ResultOcr:
    return ResultOcr(
        ResultHeader('胜利', 'normal', 900, 12, 8, 30_000, 25_000),
        tuple(
            OcrPlayer(
                side=side,
                slot=slot,
                name='{}{}'.format(side, slot),
                normalized_name='{}{}'.format(side, slot),
                raw_name='{}{}'.format(side, slot),
                stats=PlayerStats(slot, slot, slot, 10_000),
                confidence=0.9,
            )
            for side in ('left', 'right')
            for slot in range(1, team_size + 1)
        ),
    )


def test_afk_classifier_batches_every_result_slot_after_quality_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frame = RgbFrame(320, 180, b'\x00\x00\x00' * 320 * 180)

    class Classifier:
        model_version = 'afk-run-1'

        def __init__(self) -> None:
            self.batch_sizes = []

        def predict_many(self, frames):
            self.batch_sizes.append(len(frames))
            return tuple(
                SimpleNamespace(
                    label='afk' if index == 1 else 'active',
                    confidence=0.9,
                    scores=(
                        (('active', 0.1), ('afk', 0.9))
                        if index == 1
                        else (('active', 0.9), ('afk', 0.1))
                    ),
                )
                for index, _frame in enumerate(frames)
            )

    classifier = Classifier()
    analyzer = VaingloryVideoAnalyzer(afk_status_classifier=classifier)
    monkeypatch.setattr(
        analyzer_module, 'visible_result_portrait_count', lambda *_args: 6
    )
    monkeypatch.setattr(
        analyzer_module, 'result_action_min_contrast', lambda *_args: 100
    )

    statuses = analyzer._classify_afk_statuses(
        frame, hit(0).layout, _complete_afk_result()
    )

    assert classifier.batch_sizes == [6]
    assert len(statuses) == 6
    assert statuses[1].status == 'afk'
    assert statuses[1].probability == pytest.approx(0.9)
    assert {status.model_version for status in statuses} == {'afk-run-1'}
    assert all(not status.gate_reason for status in statuses)


@pytest.mark.parametrize(
    ('visible_portraits', 'action_contrast', 'reason'),
    ((5, 100, 'avatars_not_all_visible'), (6, 36, 'panel_low_contrast')),
)
def test_afk_classifier_abstains_on_low_quality_result(
    monkeypatch: pytest.MonkeyPatch,
    visible_portraits: int,
    action_contrast: int,
    reason: str,
) -> None:
    frame = RgbFrame(320, 180, b'\x00\x00\x00' * 320 * 180)

    class Classifier:
        model_version = 'afk-run-1'

        def predict_many(self, _frames):
            raise AssertionError('低质结算页不得进入模型')

    analyzer = VaingloryVideoAnalyzer(afk_status_classifier=Classifier())
    monkeypatch.setattr(
        analyzer_module,
        'visible_result_portrait_count',
        lambda *_args: visible_portraits,
    )
    monkeypatch.setattr(
        analyzer_module, 'result_action_min_contrast', lambda *_args: action_contrast
    )

    statuses = analyzer._classify_afk_statuses(
        frame, hit(0).layout, _complete_afk_result()
    )

    assert len(statuses) == 6
    assert {status.status for status in statuses} == {'unknown'}
    assert {status.gate_reason for status in statuses} == {reason}
    assert all(status.probability is None for status in statuses)


def test_afk_classifier_error_abstains_instead_of_marking_players_active(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frame = RgbFrame(320, 180, b'\x00\x00\x00' * 320 * 180)

    class Classifier:
        model_version = 'afk-run-1'

        def predict_many(self, _frames):
            raise RuntimeError('onnx failed')

    analyzer = VaingloryVideoAnalyzer(afk_status_classifier=Classifier())
    monkeypatch.setattr(
        analyzer_module, 'visible_result_portrait_count', lambda *_args: 6
    )
    monkeypatch.setattr(
        analyzer_module, 'result_action_min_contrast', lambda *_args: 100
    )

    statuses = analyzer._classify_afk_statuses(
        frame, hit(0).layout, _complete_afk_result()
    )

    assert len(statuses) == 6
    assert {status.status for status in statuses} == {'unknown'}
    assert {status.gate_reason for status in statuses} == {'model_error:RuntimeError'}
    assert all(status.probability is None for status in statuses)


def test_result_hero_lineup_distinguishes_mismatch_from_missing_evidence() -> None:
    heroes = tuple(
        AnalyzedHero(
            side=side,
            slot=slot,
            fingerprint='',
            thumbnail_png=b'',
            label=label,
            confidence=0.9,
        )
        for side, labels in (
            ('left', ('Alpha', 'Beta', 'Gamma')),
            ('right', ('Delta', 'Epsilon', 'Zeta')),
        )
        for slot, label in enumerate(labels, 1)
    )
    frame = HeroFrame(side='left', slot=1, frame=RgbFrame(1, 1, b'\x00\x00\x00'))

    def hud(labels: Tuple[str, ...]):
        return {
            (side, slot): (
                replace(frame, side=side, slot=slot),
                HeroMatch(label, 0.9, 12, 6),
            )
            for side, side_labels in (('left', labels[:3]), ('right', labels[3:]))
            for slot, label in enumerate(side_labels, 1)
            if label
        }

    assert (
        VaingloryVideoAnalyzer._result_hud_lineup_evidence(
            heroes, hud(('Alpha', 'Beta', '', 'Delta', 'Epsilon', '')), team_size=3
        )
        == 'matched'
    )
    assert (
        VaingloryVideoAnalyzer._result_hud_lineup_evidence(
            heroes,
            hud(('Kestrel', 'Lance', 'Lyra', 'Ringo', 'Skaarf', 'Vox')),
            team_size=3,
        )
        == 'mismatched'
    )
    assert (
        VaingloryVideoAnalyzer._result_hud_lineup_evidence(
            heroes, hud(('Alpha', '', '', '', '', '')), team_size=3
        )
        == 'unknown'
    )


def test_five_player_hud_fallback_uses_left_slot_nearest_center(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frame = HeroFrame('left', 1, RgbFrame(1, 1, b'\x00\x00\x00'))
    result_labels = {
        'left': ('Alpha', 'Beta', 'Gamma', 'Delta', 'Epsilon'),
        'right': ('Kestrel', 'Lance', 'Lyra', 'Ringo', 'Vox'),
    }
    heroes = tuple(
        AnalyzedHero(side, slot, '', b'', label, 0.9)
        for side, labels in result_labels.items()
        for slot, label in enumerate(labels, 1)
    )
    hud = {
        (hud_side, slot): (
            replace(frame, side=hud_side, slot=slot),
            HeroMatch(label, 0.9, 12, 6),
        )
        for hud_side, labels in (
            ('left', result_labels['right']),
            ('right', result_labels['left']),
        )
        for slot, label in enumerate(labels, 1)
    }
    analyzer = VaingloryVideoAnalyzer()
    monkeypatch.setattr(
        analyzer, '_recognize_gameplay_hud_heroes', lambda *_args, **_kwargs: hud
    )

    _, player = analyzer._apply_gameplay_hud_fallback(
        heroes,
        layout=ResultLayout(
            left_color='teal',
            right_color='orange',
            winner_color='teal',
            winner_side='left',
            confidence=1,
            team_size=5,
        ),
        frames=(),
        team_size=5,
    )

    assert player is not None
    assert (player.side, player.slot) == ('right', 5)


def test_three_player_hud_fallback_uses_local_side_slot_nearest_center(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frame = HeroFrame('left', 1, RgbFrame(1, 1, b'\x00\x00\x00'))
    result_labels = {
        'left': ('Alpha', 'Beta', 'Gamma'),
        'right': ('Kestrel', 'Lance', 'Lyra'),
    }
    heroes = tuple(
        AnalyzedHero(side, slot, '', b'', label, 0.9)
        for side, labels in result_labels.items()
        for slot, label in enumerate(labels, 1)
    )
    hud = {
        (side, slot): (
            replace(frame, side=side, slot=slot),
            HeroMatch(label, 0.9, 12, 6),
        )
        for side, labels in result_labels.items()
        for slot, label in enumerate(labels, 1)
    }
    analyzer = VaingloryVideoAnalyzer()
    monkeypatch.setattr(
        analyzer, '_recognize_gameplay_hud_heroes', lambda *_args, **_kwargs: hud
    )

    _, player = analyzer._apply_gameplay_hud_fallback(
        heroes,
        layout=ResultLayout(
            left_color='orange',
            right_color='teal',
            winner_color='teal',
            winner_side='right',
            confidence=1,
            team_size=3,
        ),
        frames=(),
        team_size=3,
    )

    assert player is not None
    assert (player.side, player.slot) == ('right', 1)


def test_hero_recognition_searches_one_shared_layout_offset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = []

    def extract(
        _frame: RgbFrame, *, viewport, team_size: int, center_shift: float
    ) -> Tuple[HeroFrame, ...]:
        assert team_size == 3
        calls.append(center_shift)
        variant = 1 if center_shift == 0.01 else 0
        return tuple(
            HeroFrame(
                side=side, slot=slot, frame=RgbFrame(1, 1, bytes((variant, slot, 0)))
            )
            for side in ('left', 'right')
            for slot in range(1, 4)
        )

    class Recognizer:
        def recognize(self, frame: RgbFrame):
            variant, slot, _ = frame.pixels
            if variant == 0 and slot == 3:
                return None
            return HeroMatch(
                label='{}-{}'.format(variant, slot),
                confidence=0.9,
                inliers=12,
                margin=6,
            )

    monkeypatch.setattr(analyzer_module, 'extract_result_heroes', extract)
    analyzer = VaingloryVideoAnalyzer(
        hero_recognizer=Recognizer()  # type: ignore[arg-type]
    )

    heroes = analyzer._recognize_heroes(RgbFrame(1, 1, b'\x00\x00\x00'), hit(0).layout)

    assert calls == [0.0, -0.01, 0.01, -0.02, 0.02]
    assert [hero.label for hero in heroes] == ['1-1', '1-2', '1-3', '1-1', '1-2', '1-3']


def test_hero_recognition_keeps_the_center_layout_when_all_six_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = []

    def extract(
        _frame: RgbFrame, *, viewport, team_size: int, center_shift: float
    ) -> Tuple[HeroFrame, ...]:
        assert team_size == 3
        calls.append(center_shift)
        return tuple(
            HeroFrame(side=side, slot=slot, frame=RgbFrame(1, 1, bytes((0, slot, 0))))
            for side in ('left', 'right')
            for slot in range(1, 4)
        )

    class Recognizer:
        def recognize(self, frame: RgbFrame):
            return HeroMatch(
                label=str(frame.pixels[1]), confidence=0.9, inliers=12, margin=6
            )

    monkeypatch.setattr(analyzer_module, 'extract_result_heroes', extract)
    analyzer = VaingloryVideoAnalyzer(
        hero_recognizer=Recognizer()  # type: ignore[arg-type]
    )

    heroes = analyzer._recognize_heroes(RgbFrame(1, 1, b'\x00\x00\x00'), hit(0).layout)

    assert calls == [0.0]
    assert len(heroes) == 6
