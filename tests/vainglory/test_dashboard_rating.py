import pytest

from blrec.vainglory.dashboard_rating import calculate_bayesian_rating


def test_rating_rewards_a_larger_sample_at_the_same_raw_win_rate() -> None:
    short = calculate_bayesian_rating(
        wins=4, matches=5, baseline=0.5, previous_ability=None
    )
    long = calculate_bayesian_rating(
        wins=40, matches=50, baseline=0.5, previous_ability=None
    )

    assert short is not None
    assert long is not None
    assert short.provisional is False
    assert long.score > short.score


def test_rating_uses_a_quarter_of_the_previous_season_ability() -> None:
    without_history = calculate_bayesian_rating(
        wins=5, matches=10, baseline=0.5, previous_ability=None
    )
    with_history = calculate_bayesian_rating(
        wins=5, matches=10, baseline=0.5, previous_ability=0.8
    )

    assert without_history is not None
    assert with_history is not None
    assert without_history.ability == pytest.approx(0.5)
    assert with_history.ability == pytest.approx(0.55)
    assert with_history.score > without_history.score


def test_rating_marks_fewer_than_five_matches_as_provisional() -> None:
    rating = calculate_bayesian_rating(
        wins=3, matches=4, baseline=0.5, previous_ability=None
    )

    assert rating is not None
    assert rating.provisional is True
    assert (
        calculate_bayesian_rating(
            wins=0, matches=0, baseline=0.5, previous_ability=None
        )
        is None
    )
