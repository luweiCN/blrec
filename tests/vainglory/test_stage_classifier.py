from blrec.vainglory.stage_classifier import (
    CONTENT_NOT_VAINGLORY,
    CONTENT_VAINGLORY,
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
    build_classified_windows,
)


def _observation(
    at_ms: int,
    stage: int = STAGE_GAMEPLAY,
    *,
    mode: int = MODE_3V3,
    content: int = CONTENT_VAINGLORY,
) -> ClassifiedObservation:
    return ClassifiedObservation(
        at_ms=at_ms, stage=stage, stage_conf=0.9, mode=mode, content=content
    )


def _seconds(value: int) -> int:
    return value * 1_000


def test_gameplay_run_produces_result_window() -> None:
    observations = tuple(_observation(_seconds(second)) for second in range(100, 600))
    windows = build_classified_windows(observations, duration_ms=_seconds(3_600))
    assert len(windows) == 1
    window = windows[0]
    assert window.start_ms <= _seconds(595)
    assert window.end_ms >= _seconds(624)
    assert window.mode == 'unknown'


def test_open_ended_gameplay_does_not_create_a_result_window() -> None:
    observations = (
        _observation(0, STAGE_GAMEPLAY),
        _observation(60_000, STAGE_GAMEPLAY),
    )

    assert (
        build_classified_windows(
            observations, duration_ms=120_000, run_gap_ms=75_000, skip_open_ended=True
        )
        == ()
    )


def test_quit_mid_game_still_generates_window() -> None:
    observations = tuple(
        _observation(_seconds(second), STAGE_GAMEPLAY) for second in range(100, 600)
    ) + (
        _observation(_seconds(600), STAGE_OUT_OF_MATCH, mode=MODE_3V3),
    )
    windows = build_classified_windows(observations, duration_ms=_seconds(3_600))
    assert len(windows) == 1
    assert windows[0].end_ms > _seconds(600)


def test_result_signal_pads_window() -> None:
    observations = tuple(
        _observation(_seconds(second)) for second in range(100, 600)
    ) + (
        _observation(_seconds(600), STAGE_RESULT_PAGE),
        _observation(_seconds(601), STAGE_VICTORY_DEFEAT),
        _observation(_seconds(602), STAGE_OUT_OF_MATCH),
    )
    windows = build_classified_windows(observations, duration_ms=_seconds(3_600))
    assert len(windows) == 1
    window = windows[0]
    assert window.start_ms <= _seconds(595)
    assert window.end_ms >= _seconds(609)
    assert window.focus_ms in (_seconds(600), _seconds(601))


def test_talent_select_votes_aram() -> None:
    observations = tuple(
        _observation(_seconds(second)) for second in range(100, 200)
    ) + (
        _observation(_seconds(200), STAGE_TALENT_SELECT, mode=MODE_ARAM),
        _observation(_seconds(201), STAGE_TALENT_SELECT, mode=MODE_ARAM),
        _observation(_seconds(202), STAGE_GAMEPLAY),
        _observation(_seconds(203), STAGE_GAMEPLAY),
    )
    windows = build_classified_windows(observations, duration_ms=_seconds(3_600))
    assert len(windows) == 1
    assert windows[0].mode == 'aram'


def test_single_talent_noise_keeps_mode_unknown() -> None:
    observations = tuple(
        _observation(_seconds(second)) for second in range(100, 300)
    ) + (
        _observation(_seconds(280), STAGE_TALENT_SELECT),
        _observation(_seconds(299), STAGE_GAMEPLAY),
    )
    windows = build_classified_windows(observations, duration_ms=_seconds(3_600))
    assert len(windows) == 1
    assert windows[0].mode == 'unknown'


def test_single_aram_mode_noise_keeps_mode_unknown() -> None:
    observations = tuple(
        _observation(_seconds(second)) for second in range(100, 300)
    ) + (
        _observation(_seconds(280), mode=MODE_ARAM),
        _observation(_seconds(299), STAGE_GAMEPLAY),
    )
    windows = build_classified_windows(observations, duration_ms=_seconds(3_600))
    assert len(windows) == 1
    assert windows[0].mode == 'unknown'


def test_five_v5_votes_on_interface_frames() -> None:
    observations = tuple(
        _observation(_seconds(second)) for second in range(100, 300)
    ) + (
        _observation(_seconds(280), STAGE_SCOREBOARD, mode=MODE_5V5),
        _observation(_seconds(285), STAGE_SCOREBOARD, mode=MODE_5V5),
        _observation(_seconds(290), STAGE_RESULT_PAGE, mode=MODE_5V5),
    )
    windows = build_classified_windows(observations, duration_ms=_seconds(3_600))
    assert len(windows) == 1
    assert windows[0].mode == '5v5'


def test_gameplay_mode_noise_ignored() -> None:
    observations = tuple(
        _observation(_seconds(second), mode=MODE_5V5) for second in range(100, 300)
    ) + (
        _observation(_seconds(280), STAGE_SCOREBOARD, mode=MODE_ARAM),
    )
    windows = build_classified_windows(observations, duration_ms=_seconds(3_600))
    assert windows[0].mode == 'unknown'


def test_sparse_five_v5_noise_keeps_mode_unknown() -> None:
    observations = tuple(_observation(_seconds(second)) for second in range(100, 300))
    observations = observations[:-2] + (
        _observation(_seconds(298), mode=MODE_5V5),
        _observation(_seconds(299), mode=MODE_5V5),
    )
    windows = build_classified_windows(observations, duration_ms=_seconds(3_600))
    assert windows[0].mode == 'unknown'


def test_pre_match_extends_run_forward() -> None:
    observations = tuple(
        _observation(_seconds(second), STAGE_PRE_MATCH) for second in range(90, 100)
    ) + tuple(_observation(_seconds(second)) for second in range(100, 200))
    windows = build_classified_windows(observations, duration_ms=_seconds(3_600))
    assert len(windows) == 1


def test_anchor_without_gameplay_is_abandoned() -> None:
    observations = tuple(
        _observation(_seconds(second), STAGE_PRE_MATCH) for second in range(90, 100)
    ) + (
        _observation(_seconds(120), STAGE_OUT_OF_MATCH),
        _observation(_seconds(140), STAGE_OUT_OF_MATCH),
    )
    windows = build_classified_windows(observations, duration_ms=_seconds(3_600))
    assert windows == ()


def test_second_anchor_abandons_first() -> None:
    observations = tuple(
        _observation(_seconds(second), STAGE_PRE_MATCH) for second in range(90, 100)
    ) + (
        _observation(_seconds(150), STAGE_PRE_MATCH),
        _observation(_seconds(160), STAGE_PRE_MATCH),
        _observation(_seconds(170), STAGE_GAMEPLAY),
        _observation(_seconds(171), STAGE_GAMEPLAY),
        _observation(_seconds(172), STAGE_GAMEPLAY),
    )
    windows = build_classified_windows(observations, duration_ms=_seconds(3_600))
    assert len(windows) == 1


def test_long_bp_still_confirms() -> None:
    observations = tuple(
        _observation(_seconds(second), STAGE_PRE_MATCH) for second in range(90, 240)
    ) + tuple(_observation(_seconds(second)) for second in range(240, 320))
    windows = build_classified_windows(observations, duration_ms=_seconds(3_600))
    assert len(windows) == 1


def test_video_starts_in_result_signal_only() -> None:
    observations = (
        _observation(_seconds(5), STAGE_RESULT_PAGE),
        _observation(_seconds(6), STAGE_VICTORY_DEFEAT),
        _observation(_seconds(7), STAGE_OUT_OF_MATCH),
    )
    windows = build_classified_windows(observations, duration_ms=_seconds(3_600))
    assert len(windows) == 1
    assert windows[0].start_ms <= _seconds(5)


def test_non_vainglory_produces_no_windows() -> None:
    observations = tuple(
        _observation(
            _seconds(second), STAGE_OUT_OF_MATCH, content=CONTENT_NOT_VAINGLORY
        )
        for second in range(0, 100)
    )
    windows = build_classified_windows(observations, duration_ms=_seconds(3_600))
    assert windows == ()


def test_nearby_windows_merge() -> None:
    observations = tuple(
        _observation(_seconds(second)) for second in range(100, 200)
    ) + (
        _observation(_seconds(210), STAGE_OUT_OF_MATCH),
        _observation(_seconds(215), STAGE_PRE_MATCH),
        _observation(_seconds(216), STAGE_GAMEPLAY),
        _observation(_seconds(217), STAGE_GAMEPLAY),
    )
    windows = build_classified_windows(observations, duration_ms=_seconds(3_600))
    assert len(windows) == 1


def test_transition_noise_smoothed_away() -> None:
    observations = tuple(_observation(_seconds(second)) for second in range(100, 200))
    noisy = tuple(
        _observation(obs.at_ms, STAGE_TRANSITION) if obs.at_ms == _seconds(150) else obs
        for obs in observations
    )
    windows = build_classified_windows(noisy, duration_ms=_seconds(3_600))
    assert len(windows) == 1


def test_two_matches_produce_two_windows() -> None:
    first = tuple(_observation(_seconds(second)) for second in range(100, 200))
    gap = (
        _observation(_seconds(440), STAGE_OUT_OF_MATCH),
        _observation(_seconds(460), STAGE_OUT_OF_MATCH),
        _observation(_seconds(490), STAGE_PRE_MATCH),
    )
    second = tuple(_observation(_seconds(second)) for second in range(500, 600))
    windows = build_classified_windows(
        first + gap + second, duration_ms=_seconds(3_600)
    )
    assert len(windows) == 2
