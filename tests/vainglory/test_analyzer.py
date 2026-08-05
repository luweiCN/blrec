from dataclasses import replace
from typing import Tuple

import pytest
from loguru import logger

import blrec.vainglory.analyzer as analyzer_module
from blrec.vainglory.analyzer import (
    AnalysisCancelled,
    AnalyzedHero,
    AnalyzedMatch,
    ResultHit,
    VaingloryVideoAnalyzer,
    VideoPart,
    classify_match_kind,
    collapse_analyzed_matches,
    collapse_result_hits,
    exclude_content_duplicates,
    stats_eligibility,
)
from blrec.vainglory.hero_recognition import HeroMatch
from blrec.vainglory.ocr import OcrPlayer, PlayerStats, ResultHeader, ResultOcr
from blrec.vainglory.sampling import ScanWindow, TimedFrame, VideoProfile
from blrec.vainglory.vision import (
    GameplayHud,
    HeroFrame,
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

    with pytest.raises(analyzer_module._ResultEvidenceRejected):
        analyzer._recognize_frame(
            frame,
            part=VideoPart(id=1, index=1, path='unused'),
            at_ms=552_000,
            layout=hit(0).layout,
            header=scoreboard.header,
            video_duration_ms=600_000,
        )


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
