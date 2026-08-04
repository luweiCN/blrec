from typing import Tuple

from blrec.vainglory.sampling import (
    CoarseObservation,
    FfmpegSampler,
    ScanWindow,
    fit_frame_dimensions,
    hud_lineup_similarity,
    result_search_windows,
    same_gameplay_run,
)


def observation(
    at_seconds: int,
    *,
    hud: bool = False,
    result: bool = False,
    signature: str = 'lineup',
    heroes: Tuple[str, ...] = (),
    view_context: str = 'played',
) -> CoarseObservation:
    return CoarseObservation(
        at_ms=at_seconds * 1_000,
        hud_signature=signature if hud else None,
        result_visible=result,
        hero_lineup=heroes,
        team_size=len(heroes) // 2 if heroes else None,
        view_context=view_context if hud else 'unknown',
    )


def test_default_hud_probe_interval_is_thirty_seconds() -> None:
    assert FfmpegSampler()._coarse_interval_seconds == 30


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
        ScanWindow(0, 70_000, view_context='played'),
        ScanWindow(85_000, 110_000, view_context='played'),
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

    assert windows == (ScanWindow(120_000, 180_000, view_context='played'),)


def test_a_keyframe_result_is_searched_even_without_detectable_hud() -> None:
    windows = result_search_windows(
        (observation(0), observation(5), observation(10, result=True), observation(15)),
        duration_ms=20_000,
    )

    assert windows == (ScanWindow(7_000, 13_000),)


def test_consecutive_huds_stay_in_one_game_despite_changed_portrait_hashes() -> None:
    windows = result_search_windows(
        (
            observation(100, hud=True, signature='00:00'),
            observation(130, hud=True, signature='ff:ff'),
            observation(160),
        ),
        duration_ms=220_000,
    )

    assert windows == (ScanWindow(120_000, 190_000, view_context='played'),)


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
        ScanWindow(210_000, 280_000, view_context='played', hero_lineup=lineup),
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
        ScanWindow(90_000, 160_000, view_context='played', hero_lineup=first_lineup),
        ScanWindow(210_000, 280_000, view_context='played', hero_lineup=second_lineup),
    )


def test_same_heroes_on_opposite_sides_are_a_conflicting_hud() -> None:
    previous = observation(
        100, hud=True, heroes=('Alpha', 'Beta', 'Gamma', 'Delta', 'Epsilon', 'Zeta')
    )
    current = observation(
        130, hud=True, heroes=('Delta', 'Epsilon', 'Zeta', 'Alpha', 'Beta', 'Gamma')
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

    assert windows == (ScanWindow(120_000, 190_000, view_context='observed'),)


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
