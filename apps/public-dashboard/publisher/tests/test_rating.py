from typing import Optional

import pytest
from blrec_dashboard_publisher.rating import (
    CARRYOVER_MATCH_CAP,
    VirtualMatchRating,
    calculate_rating_forecast,
    calculate_virtual_match_rating,
    expected_win_probability,
)


def display_score(rating: Optional[VirtualMatchRating]) -> int:
    assert rating is not None
    return rating.score * 3


def test_virtual_average_curve_maps_2160_to_about_77_percent() -> None:
    assert expected_win_probability(1200) == pytest.approx(0.5)
    assert expected_win_probability(2160) == pytest.approx(0.773476, abs=0.000001)


def test_established_2160_rating_gains_six_or_loses_eighteen() -> None:
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

    assert display_score(win) == 2166
    assert display_score(loss) == 2142


def test_thirteen_wins_and_two_losses_from_2160_gain_thirty_points() -> None:
    rating = calculate_virtual_match_rating(
        results=(['W'] * 13) + (['L'] * 2),
        previous_ability=expected_win_probability(2160),
        previous_evidence=CARRYOVER_MATCH_CAP,
        reset_visible_score=False,
    )

    assert display_score(rating) == 2190


def test_forecast_projects_the_exact_next_result() -> None:
    history = ['W', 'W', 'L', 'W', 'L', 'W']
    rating = calculate_virtual_match_rating(results=history)
    after_win = calculate_virtual_match_rating(results=history + ['W'])
    after_loss = calculate_virtual_match_rating(results=history + ['L'])

    assert rating is not None
    forecast = calculate_rating_forecast(
        rating=rating,
        win_rate=history.count('W') / len(history),
        reset_visible_score=True,
    )

    assert after_win is not None
    assert after_loss is not None
    assert forecast.next_win_score == after_win.score
    assert forecast.next_loss_score == after_loss.score


def test_forecast_reports_promotion_targets_and_two_match_estimates() -> None:
    rating = VirtualMatchRating(
        ability=expected_win_probability(2160),
        evidence=CARRYOVER_MATCH_CAP,
        score=720,
        provisional=False,
    )

    forecast = calculate_rating_forecast(
        rating=rating, win_rate=0.774, reset_visible_score=True
    )

    assert forecast.next_win_score * 3 == 2166
    assert forecast.next_loss_score * 3 == 2142
    assert forecast.next_division is not None
    assert forecast.next_division.target_display_score == 2267
    assert forecast.next_division.all_win_matches == 18
    assert forecast.next_division.current_win_rate_matches == 186
    assert forecast.next_tier is not None
    assert forecast.next_tier.target_display_score == 2400
    assert forecast.next_tier.all_win_matches == 40
    assert forecast.next_tier.current_win_rate_matches == 417
    assert forecast.ultimate.target_display_score == 2800
    assert forecast.ultimate.all_win_matches == 108
    assert forecast.ultimate.current_win_rate_matches == 1112


def test_forecast_marks_a_non_positive_current_rate_as_unreachable() -> None:
    rating = VirtualMatchRating(
        ability=expected_win_probability(2160),
        evidence=CARRYOVER_MATCH_CAP,
        score=720,
        provisional=False,
    )

    forecast = calculate_rating_forecast(
        rating=rating, win_rate=0.75, reset_visible_score=True
    )

    assert forecast.next_division is not None
    assert forecast.next_division.current_win_rate_matches is None
    assert forecast.next_tier is not None
    assert forecast.next_tier.current_win_rate_matches is None
    assert forecast.ultimate.current_win_rate_matches is None


def test_forecast_marks_completed_vainglorious_gold_goals() -> None:
    rating = VirtualMatchRating(
        ability=expected_win_probability(2820),
        evidence=CARRYOVER_MATCH_CAP,
        score=940,
        provisional=False,
    )

    forecast = calculate_rating_forecast(
        rating=rating, win_rate=0.8, reset_visible_score=True
    )

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
            score=630,
            provisional=False,
        ),
        win_rate=0.8,
        reset_visible_score=True,
    )
    tier_ten = calculate_rating_forecast(
        rating=VirtualMatchRating(
            ability=expected_win_probability(2400),
            evidence=CARRYOVER_MATCH_CAP,
            score=800,
            provisional=False,
        ),
        win_rate=0.8,
        reset_visible_score=True,
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
