from io import BytesIO
from queue import Queue
from typing import Iterator, List, Optional, Tuple

import pytest

from blrec.vainglory.sampling import (
    CoarseObservation,
    FfmpegSampler,
    ScanWindow,
    TimedFrame,
    _read_showinfo_timestamps,
    adaptive_sampling_plan,
    fit_frame_dimensions,
    hud_lineup_similarity,
    result_search_windows,
    same_gameplay_run,
)
from blrec.vainglory.vision import RgbFrame


def observation(
    at_seconds: int,
    *,
    hud: bool = False,
    result: bool = False,
    signature: str = 'lineup',
    heroes: Tuple[str, ...] = (),
    view_context: str = 'played',
    scene: str = '',
) -> CoarseObservation:
    return CoarseObservation(
        at_ms=at_seconds * 1_000,
        hud_signature=signature if hud else None,
        result_visible=result,
        hero_lineup=heroes,
        team_size=len(heroes) // 2 if heroes else None,
        view_context=view_context if hud else 'unknown',
        scene_signature=scene,
    )


def test_default_hud_probe_interval_is_five_seconds() -> None:
    assert FfmpegSampler()._coarse_interval_seconds == 5


def test_sampler_accepts_only_configured_remote_media_origin() -> None:
    sampler = FfmpegSampler(trusted_remote_origin='http://nas:2234')

    assert (
        sampler._media_input('http://nas:2234/api/media?token=one')
        == 'http://nas:2234/api/media?token=one'
    )
    with pytest.raises(ValueError):
        sampler._media_input('http://attacker.invalid/video.flv')
    with pytest.raises(ValueError):
        sampler._media_input('http://user:password@nas:2234/video.flv')
    with pytest.raises(ValueError):
        sampler._media_input('http://nas:2234/video.flv#fragment')


def test_sampler_rejects_remote_media_when_no_origin_is_configured() -> None:
    with pytest.raises(ValueError):
        FfmpegSampler()._media_input('http://nas:2234/video.flv')


def test_classification_window_uses_five_second_local_keyframe_scan(
    monkeypatch,
) -> None:
    sampler = FfmpegSampler(coarse_interval_seconds=60)
    monkeypatch.setattr(
        sampler,
        'probe',
        lambda _path: type(
            'Profile', (), {'width': 1_920, 'height': 1_080, 'duration_ms': 180_000}
        )(),
    )
    observed = []

    def frames(_path, **kwargs):
        observed.append(kwargs)
        return iter(())

    monkeypatch.setattr(sampler, '_frames', frames)

    assert sampler.coarse_interval_seconds == 60
    assert (
        tuple(
            sampler.classify_window_frames(
                '/ignored', ScanWindow(60_000, 120_000), interval_seconds=5
            )
        )
        == ()
    )
    assert observed == [
        {
            'width': 480,
            'height': 270,
            'filter_value': 'fps=1/5,scale=480:270:flags=fast_bilinear',
            'frame_step_ms': 5_000,
            'skip_frame': 'nokey',
            'start_ms': 60_000,
            'duration_ms': 60_000,
            'sample_source': 'keyframe',
        }
    ]


def test_adaptive_sampling_reuses_nearby_keyframes_and_fills_large_gaps() -> None:
    plan = adaptive_sampling_plan(
        (0, 4_800, 12_200, 22_000),
        duration_ms=25_000,
        interval_ms=5_000,
        maximum_keyframe_distance_ms=2_500,
    )

    assert [point.target_ms for point in plan] == [0, 5_000, 10_000, 15_000, 20_000]
    assert [(point.at_ms, point.source) for point in plan] == [
        (0, 'keyframe'),
        (4_800, 'keyframe'),
        (12_200, 'keyframe'),
        (15_000, 'seek_fill'),
        (22_000, 'keyframe'),
    ]


def test_adaptive_sampling_does_not_reuse_one_keyframe_for_two_targets() -> None:
    plan = adaptive_sampling_plan(
        (2_500,),
        duration_ms=10_000,
        interval_ms=5_000,
        maximum_keyframe_distance_ms=2_500,
    )

    assert [(point.target_ms, point.at_ms, point.source) for point in plan] == [
        (0, 0, 'seek_fill'),
        (5_000, 2_500, 'keyframe'),
    ]


def test_classify_frames_preserves_actual_keyframe_pts_and_target_time(
    monkeypatch,
) -> None:
    frame = RgbFrame(2, 1, b'\x00\x00\x00' * 2)
    sampler = FfmpegSampler()
    monkeypatch.setattr(
        sampler,
        'probe',
        lambda _path: type(
            'Profile', (), {'width': 2, 'height': 1, 'duration_ms': 16_000}
        )(),
    )

    def keyframes(
        _path: str,
        *,
        width: int,
        height: int,
        interval_ms: int,
        maximum_keyframe_distance_ms: int,
    ) -> Iterator[TimedFrame]:
        assert (width, height) == (480, 240)
        assert interval_ms == 5_000
        assert maximum_keyframe_distance_ms == 2_500
        for target_ms, at_ms in ((0, 400), (5_000, 5_300), (10_000, 12_000)):
            yield TimedFrame(
                at_ms=at_ms, frame=frame, target_ms=target_ms, sample_source='keyframe'
            )

    monkeypatch.setattr(sampler, '_selected_keyframe_frames', keyframes)
    monkeypatch.setattr(
        sampler,
        '_seek_frame',
        lambda _path, at_ms, *, width, height: TimedFrame(
            at_ms=at_ms, frame=frame, target_ms=at_ms, sample_source='seek_fill'
        ),
    )

    frames = tuple(sampler.classify_frames('/ignored'))

    assert [(item.target_ms, item.at_ms, item.sample_source) for item in frames] == [
        (0, 400, 'keyframe'),
        (5_000, 5_300, 'keyframe'),
        (10_000, 12_000, 'keyframe'),
        (15_000, 15_000, 'seek_fill'),
    ]


def test_classify_frames_uses_configured_coarse_interval(monkeypatch) -> None:
    sampler = FfmpegSampler(coarse_interval_seconds=7)
    monkeypatch.setattr(
        sampler,
        'probe',
        lambda _path: type(
            'Profile', (), {'width': 2, 'height': 1, 'duration_ms': 15_000}
        )(),
    )
    observed = []

    def adaptive(_path, *, profile, width, height, interval_ms):
        observed.append((profile.duration_ms, width, height, interval_ms))
        return iter(())

    monkeypatch.setattr(sampler, '_adaptive_frames', adaptive)

    assert tuple(sampler.classify_frames('/ignored')) == ()
    assert observed == [(15_000, 480, 240, 7_000)]


def test_showinfo_reader_preserves_real_source_pts() -> None:
    timestamps: Queue[Optional[int]] = Queue()
    stderr_lines: List[bytes] = []

    _read_showinfo_timestamps(
        BytesIO(
            b'ffmpeg setup\n'
            b'[Parsed_showinfo_1] n:0 pts:123 pts_time:0.123\n'
            b'[Parsed_showinfo_1] n:1 pts:5678 pts_time:5.678\n'
        ),
        timestamps,
        stderr_lines,
    )

    assert timestamps.get_nowait() == 123
    assert timestamps.get_nowait() == 5_678
    assert timestamps.get_nowait() is None


def test_sampler_uses_modern_passthrough_frame_rate_option(monkeypatch) -> None:
    commands = []

    class EmptyProcess:
        def __init__(self) -> None:
            self.stdout = BytesIO()
            self.stderr = BytesIO()

        def wait(self, timeout=None) -> int:
            return 0

        def poll(self) -> int:
            return 0

    def popen(command, **_kwargs):
        commands.append(command)
        return EmptyProcess()

    sampler = FfmpegSampler()
    monkeypatch.setattr(sampler, '_media_input', lambda _path: '/video')
    monkeypatch.setattr('blrec.vainglory.sampling.subprocess.Popen', popen)
    monkeypatch.setattr(
        'blrec.vainglory.sampling._read_exact', lambda _stream, _size, **_kwargs: b''
    )

    assert (
        tuple(
            sampler._selected_keyframe_frames(
                '/ignored',
                width=2,
                height=1,
                interval_ms=5_000,
                maximum_keyframe_distance_ms=2_500,
            )
        )
        == ()
    )
    assert (
        tuple(
            sampler._frames(
                '/ignored',
                width=2,
                height=1,
                filter_value='null',
                frame_step_ms=1_000,
                variable_frame_rate=True,
            )
        )
        == ()
    )
    assert len(commands) == 2
    for command in commands:
        assert '-vsync' not in command
        option_index = command.index('-fps_mode')
        assert command[option_index + 1] == 'passthrough'


def test_sustained_hud_loss_scans_only_boundary_and_visual_changes() -> None:
    dark = '0' * 32
    changed = 'f' * 32

    windows = result_search_windows(
        (
            observation(0, hud=True, scene=dark),
            observation(5, hud=True, scene=dark),
            observation(10, scene=dark),
            observation(15, scene=dark),
            observation(20, scene=dark),
            observation(25, scene=changed),
            observation(30, scene=changed),
        ),
        duration_ms=35_000,
        hud_gap_ms=20_000,
        before_end_ms=5_000,
    )

    assert windows == (
        ScanWindow(0, 10_000, view_context='played', focus_ms=5_000),
        ScanWindow(20_000, 25_000, view_context='played', focus_ms=5_000),
        ScanWindow(30_000, 35_000, view_context='played', focus_ms=5_000),
    )


def test_hud_return_within_twenty_seconds_suppresses_scoreboard_gap() -> None:
    signature = '0' * 32
    windows = result_search_windows(
        (
            observation(0, hud=True, scene=signature),
            observation(5, hud=True, scene=signature),
            observation(10, scene='f' * 32),
            observation(15, scene='f' * 32),
            observation(20, hud=True, scene=signature),
            observation(25, hud=True, scene=signature),
            observation(30, scene=signature),
        ),
        duration_ms=35_000,
        hud_gap_ms=20_000,
        before_end_ms=5_000,
    )

    assert windows == (
        ScanWindow(20_000, 30_000, view_context='played', focus_ms=25_000),
        ScanWindow(30_000, 35_000, view_context='played', focus_ms=25_000),
    )


def test_searches_after_each_gameplay_run_and_at_video_end() -> None:
    windows = result_search_windows(
        (
            observation(0, hud=True),
            observation(5, hud=True),
            observation(10, hud=True),
            observation(15),
            observation(20),
            observation(90, hud=True),
            observation(95, hud=True),
        ),
        duration_ms=110_000,
    )

    assert windows == (
        ScanWindow(0, 70_000, view_context='played', focus_ms=10_000),
        ScanWindow(85_000, 110_000, view_context='played', focus_ms=95_000),
    )


def test_temporary_scoreboard_gap_merges_into_one_result_window() -> None:
    windows = result_search_windows(
        (
            observation(100, hud=True),
            observation(105, hud=True),
            observation(110),
            observation(115),
            observation(120),
            observation(125, hud=True),
            observation(130, hud=True),
            observation(135),
            observation(140),
            observation(145),
        ),
        duration_ms=180_000,
    )

    assert windows == (
        ScanWindow(120_000, 180_000, view_context='played', focus_ms=130_000),
    )


def test_a_keyframe_result_is_searched_even_without_detectable_hud() -> None:
    windows = result_search_windows(
        (observation(0), observation(5), observation(10, result=True), observation(15)),
        duration_ms=20_000,
    )

    assert windows == (ScanWindow(7_000, 13_000, focus_ms=10_000),)


def test_consecutive_huds_stay_in_one_game_despite_changed_portrait_hashes() -> None:
    windows = result_search_windows(
        (
            observation(100, hud=True, signature='00:00'),
            observation(130, hud=True, signature='ff:ff'),
            observation(160),
        ),
        duration_ms=220_000,
    )

    assert windows == (
        ScanWindow(120_000, 190_000, view_context='played', focus_ms=130_000),
    )


def test_recognized_lineup_reconnects_one_game_after_a_long_hud_gap() -> None:
    lineup = ('Alpha', 'Beta', 'Gamma', 'Delta', 'Epsilon', 'Zeta')
    windows = result_search_windows(
        (
            observation(100, hud=True, heroes=lineup),
            observation(130, hud=True, heroes=('Alpha', '', 'Gamma', '', '', 'Zeta')),
            observation(160),
            observation(190),
            observation(220, hud=True, heroes=('Alpha', 'Beta', '', '', '', 'Zeta')),
        ),
        duration_ms=300_000,
    )

    assert windows == (
        ScanWindow(
            210_000,
            280_000,
            view_context='played',
            hero_lineup=lineup,
            focus_ms=220_000,
        ),
    )


def test_conflicting_recognized_lineups_split_games_after_a_long_gap() -> None:
    first_lineup = ('Alpha', 'Beta', 'Gamma', 'Delta', 'Epsilon', 'Zeta')
    second_lineup = ('Kestrel', 'Lance', 'Lyra', 'Ringo', 'Skaarf', 'Vox')
    windows = result_search_windows(
        (
            observation(100, hud=True, heroes=first_lineup),
            observation(130),
            observation(160),
            observation(220, hud=True, heroes=second_lineup),
        ),
        duration_ms=300_000,
    )

    assert windows == (
        ScanWindow(
            90_000,
            160_000,
            view_context='played',
            hero_lineup=first_lineup,
            focus_ms=100_000,
        ),
        ScanWindow(
            210_000,
            280_000,
            view_context='played',
            hero_lineup=second_lineup,
            focus_ms=220_000,
        ),
    )


def test_conflicting_hero_read_splits_nearby_hud_samples() -> None:
    previous = observation(
        100, hud=True, heroes=('Alpha', 'Beta', 'Gamma', 'Delta', 'Epsilon', 'Zeta')
    )
    current = observation(
        130, hud=True, heroes=('Delta', 'Epsilon', 'Zeta', 'Alpha', 'Beta', 'Gamma')
    )

    assert same_gameplay_run(previous, current) is False


def test_same_heroes_on_opposite_sides_split_after_a_long_hud_gap() -> None:
    previous = observation(
        100, hud=True, heroes=('Alpha', 'Beta', 'Gamma', 'Delta', 'Epsilon', 'Zeta')
    )
    current = observation(
        190, hud=True, heroes=('Delta', 'Epsilon', 'Zeta', 'Alpha', 'Beta', 'Gamma')
    )

    assert same_gameplay_run(previous, current) is False


def test_changed_detected_team_size_does_not_split_consecutive_hud_samples() -> None:
    previous = observation(
        100, hud=True, heroes=('Alpha', 'Beta', 'Gamma', 'Delta', 'Epsilon', 'Zeta')
    )
    current = observation(
        130,
        hud=True,
        heroes=(
            'Alpha',
            'Beta',
            'Gamma',
            'Kestrel',
            'Lance',
            'Delta',
            'Epsilon',
            'Zeta',
            'Ringo',
            'Vox',
        ),
    )

    assert same_gameplay_run(previous, current) is True


def test_observer_gameplay_creates_an_observed_result_window() -> None:
    windows = result_search_windows(
        (
            observation(100, hud=True, signature='00:00', view_context='observed'),
            observation(130, hud=True, signature='00:00', view_context='observed'),
            observation(160),
        ),
        duration_ms=220_000,
    )

    assert windows == (
        ScanWindow(120_000, 190_000, view_context='observed', focus_ms=130_000),
    )


def test_lineup_similarity_allows_small_perceptual_hash_noise() -> None:
    assert (
        hud_lineup_similarity(
            '0000000000000000:ffffffffffffffff', '0000000000000001:fffffffffffffffe'
        )
        > 0.9
    )
    assert hud_lineup_similarity('0000000000000000', 'ffffffffffffffff') == 0


def test_frame_dimensions_preserve_common_device_aspect_ratios() -> None:
    assert fit_frame_dimensions(1920, 1080, 1920, 1080) == (1920, 1080)
    assert fit_frame_dimensions(2048, 1536, 1920, 1080) == (1440, 1080)
    assert fit_frame_dimensions(1920, 1280, 1920, 1080) == (1620, 1080)
    assert fit_frame_dimensions(2400, 1080, 1920, 1080) == (1920, 864)
