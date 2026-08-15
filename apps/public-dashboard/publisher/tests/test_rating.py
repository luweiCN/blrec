from typing import Optional

import pytest
from blrec_dashboard_publisher.rating import (
    CARRYOVER_MATCH_CAP,
    VirtualMatchRating,
    _advance_rating,
    calculate_rating_forecast,
    calculate_virtual_match_rating,
    calculate_virtual_match_rating_timeline,
    expected_win_probability,
)


def display_score(rating: Optional[VirtualMatchRating]) -> int:
    assert rating is not None
    return round(rating.score * 3)


def test_virtual_average_curve_maps_2160_to_about_77_percent() -> None:
    assert expected_win_probability(1200) == pytest.approx(0.5)
    assert expected_win_probability(2160) == pytest.approx(0.773476, abs=0.000001)


def test_established_2160_rating_uses_tier_nine_baseline_deltas() -> None:
    ability = expected_win_probability(2160)
    win = calculate_virtual_match_rating(
        results=['W'],
        previous_ability=ability,
        previous_evidence=CARRYOVER_MATCH_CAP,
        reset_visible_score=False,
    )
    loss = calculate_virtual_match_rating(
        results=['L'],
        previous_ability=ability,
        previous_evidence=CARRYOVER_MATCH_CAP,
        reset_visible_score=False,
    )

    assert display_score(win) == 2172
    assert display_score(loss) == 2148


def test_thirteen_wins_and_two_losses_from_2160_gain_one_hundred_eighteen() -> None:
    rating = calculate_virtual_match_rating(
        results=(['W'] * 13) + (['L'] * 2),
        previous_ability=expected_win_probability(2160),
        previous_evidence=CARRYOVER_MATCH_CAP,
        reset_visible_score=False,
    )

    assert display_score(rating) == 2278


@pytest.mark.parametrize(
    ('visible_score', 'win_delta', 'loss_delta'),
    ((2160, 12, -12), (2499, 9, -12), (2700, 2, -12), (2901, 1, -15)),
)
def test_established_rating_uses_visible_tier_baselines(
    visible_score: int, win_delta: int, loss_delta: int
) -> None:
    rating = VirtualMatchRating(
        ability=expected_win_probability(visible_score),
        evidence=CARRYOVER_MATCH_CAP,
        score=visible_score / 3,
        provisional=False,
    )

    win = _advance_rating(rating, 'W')
    loss = _advance_rating(rating, 'L')

    assert display_score(win) - visible_score == win_delta
    assert display_score(loss) - visible_score == loss_delta


@pytest.mark.parametrize(
    ('visible_score', 'hidden_score', 'win_delta'),
    (
        (2700, 2400, 1),
        (2700, 2700, 2),
        (2700, 3000, 3),
        (2901, 2601, 1),
        (2901, 3000, 3),
    ),
)
def test_tier_ten_silver_and_gold_wins_gain_one_to_three_points(
    visible_score: int, hidden_score: int, win_delta: int
) -> None:
    rating = VirtualMatchRating(
        ability=expected_win_probability(hidden_score),
        evidence=CARRYOVER_MATCH_CAP,
        score=visible_score / 3,
        provisional=False,
    )

    win = _advance_rating(rating, 'W')

    assert display_score(win) - visible_score == win_delta


@pytest.mark.parametrize(
    ('hidden_score', 'win_delta', 'loss_delta'), ((2460, 24, -6), (1860, 9, -18))
)
def test_hidden_strength_continuously_adjusts_tier_nine_deltas(
    hidden_score: int, win_delta: int, loss_delta: int
) -> None:
    rating = VirtualMatchRating(
        ability=expected_win_probability(hidden_score),
        evidence=CARRYOVER_MATCH_CAP,
        score=720.0,
        provisional=False,
    )

    win = _advance_rating(rating, 'W')
    loss = _advance_rating(rating, 'L')

    assert display_score(win) - 2160 == win_delta
    assert display_score(loss) - 2160 == loss_delta


def test_first_loss_without_history_receives_provisional_protection() -> None:
    rating = calculate_virtual_match_rating(results=['L'])

    assert display_score(rating) == 994


def test_rating_timeline_exposes_each_exact_score_change() -> None:
    timeline = calculate_virtual_match_rating_timeline(results=['W', 'L', 'W'])
    final = calculate_virtual_match_rating(results=['W', 'L', 'W'])

    assert final is not None
    assert len(timeline) == 3
    assert timeline[0].result == 'W'
    assert timeline[0].score_before == 999
    assert timeline[0].score_after > timeline[0].score_before
    assert timeline[0].score_delta == (
        timeline[0].score_after - timeline[0].score_before
    )
    assert timeline[1].score_before == timeline[0].score_after
    assert timeline[1].score_delta < 0
    assert timeline[2].score_before == timeline[1].score_after
    assert timeline[-1].score_after == display_score(final)


def test_rating_timeline_replays_an_inserted_historical_result() -> None:
    original = calculate_virtual_match_rating_timeline(results=['W', 'W'])
    backfilled = calculate_virtual_match_rating_timeline(results=['W', 'L', 'W'])

    assert backfilled[0] == original[0]
    assert backfilled[2].score_before == backfilled[1].score_after
    assert backfilled[2].score_after != original[1].score_after


def test_forecast_projects_the_exact_next_result() -> None:
    history = ['W', 'W', 'L', 'W', 'L', 'W']
    rating = calculate_virtual_match_rating(results=history)
    after_win = calculate_virtual_match_rating(results=history + ['W'])
    after_loss = calculate_virtual_match_rating(results=history + ['L'])

    assert rating is not None
    forecast = calculate_rating_forecast(
        rating=rating, win_rate=history.count('W') / len(history)
    )

    assert after_win is not None
    assert after_loss is not None
    assert forecast.next_win_score == after_win.score
    assert forecast.next_loss_score == after_loss.score


def test_forecast_reports_promotion_targets_and_two_match_estimates() -> None:
    rating = VirtualMatchRating(
        ability=expected_win_probability(2160),
        evidence=CARRYOVER_MATCH_CAP,
        score=720.0,
        provisional=False,
    )

    forecast = calculate_rating_forecast(rating=rating, win_rate=0.774)

    assert forecast.next_win_score * 3 == 2172
    assert forecast.next_loss_score * 3 == 2148
    assert forecast.next_division is not None
    assert forecast.next_division.target_display_score == 2267
    assert forecast.next_division.all_win_matches == 10
    assert forecast.next_division.current_win_rate_matches == 20
    assert forecast.next_tier is not None
    assert forecast.next_tier.target_display_score == 2400
    assert forecast.next_tier.all_win_matches == 23
    assert forecast.next_tier.current_win_rate_matches == 57
    assert forecast.ultimate.target_display_score == 2800
    assert forecast.ultimate.all_win_matches == 153
    assert forecast.ultimate.current_win_rate_matches is None


def test_forecast_marks_a_non_positive_current_rate_as_unreachable() -> None:
    rating = VirtualMatchRating(
        ability=expected_win_probability(2160),
        evidence=CARRYOVER_MATCH_CAP,
        score=720,
        provisional=False,
    )

    forecast = calculate_rating_forecast(rating=rating, win_rate=0.5)

    assert forecast.next_division is not None
    assert forecast.next_division.current_win_rate_matches is None
    assert forecast.next_tier is not None
    assert forecast.next_tier.current_win_rate_matches is None
    assert forecast.ultimate.current_win_rate_matches is None


def test_forecast_marks_completed_vainglorious_gold_goals() -> None:
    rating = VirtualMatchRating(
        ability=expected_win_probability(2820),
        evidence=CARRYOVER_MATCH_CAP,
        score=940.0,
        provisional=False,
    )

    forecast = calculate_rating_forecast(rating=rating, win_rate=0.8)

    assert forecast.next_division is None
    assert forecast.next_tier is None
    assert forecast.ultimate.target_display_score == 2800
    assert forecast.ultimate.all_win_matches == 0
    assert forecast.ultimate.current_win_rate_matches == 0


def test_forecast_switches_the_ultimate_goal_after_entering_tier_nine() -> None:
    tier_eight = calculate_rating_forecast(
        rating=VirtualMatchRating(
            ability=expected_win_probability(1890),
            evidence=CARRYOVER_MATCH_CAP,
            score=630.0,
            provisional=False,
        ),
        win_rate=0.8,
    )
    tier_ten = calculate_rating_forecast(
        rating=VirtualMatchRating(
            ability=expected_win_probability(2400),
            evidence=CARRYOVER_MATCH_CAP,
            score=800.0,
            provisional=False,
        ),
        win_rate=0.8,
    )

    assert tier_eight.next_division is not None
    assert tier_eight.next_division.target_display_score == 1933
    assert tier_eight.next_tier is not None
    assert tier_eight.next_tier.target_display_score == 2000
    assert tier_eight.ultimate.target_display_score == 2400
    assert tier_ten.next_division is not None
    assert tier_ten.next_division.target_display_score == 2600
    assert tier_ten.next_tier is None
    assert tier_ten.ultimate.target_display_score == 2800


def test_more_evidence_beats_a_small_sample_at_the_same_win_rate() -> None:
    short = calculate_virtual_match_rating(
        results=['W', 'W', 'W', 'W', 'L'], reset_visible_score=False
    )
    long = calculate_virtual_match_rating(
        results=['W', 'W', 'W', 'W', 'L'] * 10, reset_visible_score=False
    )

    assert short is not None
    assert long is not None
    assert short.provisional is False
    assert long.score > short.score


def test_previous_hidden_strength_accelerates_the_visible_season_reset() -> None:
    strong = calculate_virtual_match_rating(
        results=['W'],
        previous_ability=expected_win_probability(2160),
        previous_evidence=CARRYOVER_MATCH_CAP,
    )
    neutral = calculate_virtual_match_rating(
        results=['W'], previous_ability=0.5, previous_evidence=CARRYOVER_MATCH_CAP
    )

    assert strong is not None
    assert neutral is not None
    assert display_score(strong) > display_score(neutral)
    assert display_score(strong) >= 1047


@pytest.mark.parametrize(
    ('previous_score', 'expected_reset_score'),
    ((2400, 1800), (2600, 1899), (2800, 2001)),
)
def test_established_players_receive_a_soft_season_reset(
    previous_score: int, expected_reset_score: int
) -> None:
    timeline = calculate_virtual_match_rating_timeline(
        results=['W'],
        previous_ability=expected_win_probability(previous_score),
        previous_evidence=CARRYOVER_MATCH_CAP,
    )

    assert timeline[0].score_before == expected_reset_score


@pytest.mark.parametrize(
    ('previous_score', 'minimum_matches', 'maximum_matches'),
    ((2400, 25, 35), (2600, 30, 40), (2800, 125, 135)),
)
def test_high_rank_soft_reset_recovery_matches_the_calibrated_curve(
    previous_score: int, minimum_matches: int, maximum_matches: int
) -> None:
    results = ['L' if match % 10 == 5 else 'W' for match in range(1, 151)]
    timeline = calculate_virtual_match_rating_timeline(
        results=results,
        previous_ability=expected_win_probability(previous_score),
        previous_evidence=CARRYOVER_MATCH_CAP,
    )
    recovery_match = next(
        match
        for match, transition in enumerate(timeline, start=1)
        if transition.score_after >= previous_score
    )

    assert minimum_matches <= recovery_match <= maximum_matches


@pytest.mark.parametrize(
    'history', (['W'], ['W', 'L'] * 10, (['W'] * 30) + (['L'] * 8))
)
def test_next_season_result_always_moves_the_visible_score(history: list[str]) -> None:
    before = calculate_virtual_match_rating(results=history)
    after_win = calculate_virtual_match_rating(results=history + ['W'])
    after_loss = calculate_virtual_match_rating(results=history + ['L'])

    assert before is not None
    assert after_win is not None
    assert after_loss is not None
    assert after_win.score >= before.score + 1
    assert after_loss.score <= before.score - 1


def test_carryover_evidence_is_capped() -> None:
    capped = calculate_virtual_match_rating(
        results=['W'],
        previous_ability=0.75,
        previous_evidence=CARRYOVER_MATCH_CAP,
        reset_visible_score=False,
    )
    oversized = calculate_virtual_match_rating(
        results=['W'],
        previous_ability=0.75,
        previous_evidence=CARRYOVER_MATCH_CAP * 10,
        reset_visible_score=False,
    )

    assert oversized == capped
    assert oversized is not None
    assert oversized.evidence == CARRYOVER_MATCH_CAP


def test_rating_marks_fewer_than_five_matches_as_provisional() -> None:
    rating = calculate_virtual_match_rating(results=['W', 'W', 'W', 'L'])

    assert rating is not None
    assert rating.provisional is True
    assert calculate_virtual_match_rating(results=[]) is None


def test_rating_rejects_unknown_results() -> None:
    with pytest.raises(ValueError, match='result'):
        calculate_virtual_match_rating(results=['W', 'unknown'])


def test_previous_evidence_requires_a_previous_ability() -> None:
    with pytest.raises(ValueError, match='evidence'):
        calculate_virtual_match_rating(
            results=['W'], previous_evidence=CARRYOVER_MATCH_CAP
        )
