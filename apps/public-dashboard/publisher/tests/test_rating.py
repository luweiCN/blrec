from typing import Optional

import pytest
from blrec_dashboard_publisher.rating import (
    CARRYOVER_MATCH_CAP,
    VirtualMatchRating,
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
