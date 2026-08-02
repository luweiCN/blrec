from typing import Tuple

import pytest

import blrec.vainglory.analyzer as analyzer_module
from blrec.vainglory.analyzer import (
    AnalysisCancelled,
    AnalyzedMatch,
    ResultHit,
    VaingloryVideoAnalyzer,
    VideoPart,
    collapse_analyzed_matches,
    collapse_result_hits,
)
from blrec.vainglory.hero_recognition import HeroMatch
from blrec.vainglory.ocr import ResultHeader, ResultOcr
from blrec.vainglory.sampling import TimedFrame, VideoProfile
from blrec.vainglory.vision import HeroFrame, ResultLayout, RgbFrame, ViewportTransform


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


def test_result_confirmation_does_not_depend_on_the_client_language() -> None:
    analyzer = VaingloryVideoAnalyzer()
    header = ResultHeader('', 'unknown', 479, 9, 18, 40_900, 45_800)

    assert analyzer._is_result_header(header) is True
    assert analyzer._is_completed_match(header) is True


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
    monkeypatch.setattr(analyzer_module, 'detect_result_layout', lambda _frame: hit(0))

    nearby = analyzer._sample_nearby_result_frames(
        'unused', at_ms=1_000, duration_ms=2_000
    )

    assert sampler.calls == [0, 1_999]
    assert nearby == (frame, frame)


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
    monkeypatch.setattr(analyzer_module, 'detect_result_layout', lambda _frame: hit(0))

    nearby = analyzer._sample_nearby_result_frames(
        'unused', at_ms=1_000, duration_ms=2_000
    )

    assert sampler.calls == [0, 1_999]
    assert nearby == (frame,)


def test_aram_detection_samples_the_talent_selection_near_estimated_start() -> None:
    frame = RgbFrame(1, 1, b'\x00\x00\x00')

    class Sampler:
        calls = []

        def frame_at(self, _path: str, at_ms: int) -> RgbFrame:
            self.calls.append(at_ms)
            return frame

    class Detector:
        def is_visible(self, _frame: RgbFrame) -> bool:
            return len(Sampler.calls) == 2

    sampler = Sampler()
    analyzer = VaingloryVideoAnalyzer(
        sampler=sampler,  # type: ignore[arg-type]
        aram_detector=Detector(),  # type: ignore[arg-type]
    )

    mode = analyzer._detect_game_mode(
        'unused', result_at_ms=600_000, duration_seconds=590, video_duration_ms=700_000
    )

    assert mode == 'aram'
    assert sampler.calls == [11_000, 13_000]


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
        'unused', result_at_ms=600_000, duration_seconds=590, video_duration_ms=700_000
    )

    assert mode == 'unknown'
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


def test_hero_recognition_searches_one_shared_layout_offset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = []

    def extract(
        _frame: RgbFrame, *, viewport, center_shift: float
    ) -> Tuple[HeroFrame, ...]:
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
        _frame: RgbFrame, *, viewport, center_shift: float
    ) -> Tuple[HeroFrame, ...]:
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
