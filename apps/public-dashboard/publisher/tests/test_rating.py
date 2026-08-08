import pytest
from blrec_dashboard_publisher import rating
from blrec_dashboard_publisher.rating import calculate_bayesian_rating


def test_rating_rewards_a_larger_sample_at_the_same_raw_win_rate() -> None:
    short = calculate_bayesian_rating(
        results=['W', 'W', 'W', 'W', 'L'], baseline=0.5, previous_ability=None
    )
    long = calculate_bayesian_rating(
        results=['W', 'W', 'W', 'W', 'L'] * 10, baseline=0.5, previous_ability=None
    )

    assert short is not None
    assert long is not None
    assert short.provisional is False
    assert long.score > short.score


def test_rating_uses_a_quarter_of_the_previous_season_ability() -> None:
    without_history = calculate_bayesian_rating(
        results=['W', 'L'] * 5, baseline=0.5, previous_ability=None
    )
    with_history = calculate_bayesian_rating(
        results=['W', 'L'] * 5, baseline=0.5, previous_ability=0.8
    )

    assert without_history is not None
    assert with_history is not None
    assert without_history.ability == pytest.approx(0.5)
    assert with_history.ability == pytest.approx(0.55)
    assert with_history.score > without_history.score


def test_rating_marks_fewer_than_five_matches_as_provisional() -> None:
    rating = calculate_bayesian_rating(
        results=['W', 'W', 'W', 'L'], baseline=0.5, previous_ability=None
    )

    assert rating is not None
    assert rating.provisional is True
    assert (
        calculate_bayesian_rating(results=[], baseline=0.5, previous_ability=None)
        is None
    )


def test_each_result_changes_the_visible_score_in_the_expected_direction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(rating, '_beta_quantile', lambda probability, alpha, beta: 0.5)

    first_win = calculate_bayesian_rating(
        results=['W'], baseline=0.5, previous_ability=None
    )
    second_win = calculate_bayesian_rating(
        results=['W', 'W'], baseline=0.5, previous_ability=None
    )
    following_loss = calculate_bayesian_rating(
        results=['W', 'W', 'L'], baseline=0.5, previous_ability=None
    )

    assert first_win is not None
    assert second_win is not None
    assert following_loss is not None
    assert first_win.score == 501
    assert second_win.score == 502
    assert following_loss.score <= second_win.score - 1


def test_rating_rejects_unknown_results() -> None:
    with pytest.raises(ValueError, match='result'):
        calculate_bayesian_rating(
            results=['W', 'unknown'], baseline=0.5, previous_ability=None
        )


@pytest.mark.parametrize(
    ('history', 'result', 'expected_delta'),
    (((['W'] * 155) + (['L'] * 17), 'W', 1), ((['W'] * 11) + (['L'] * 98), 'L', -1)),
)
def test_real_rounding_plateaus_receive_minimum_outcome_feedback(
    history: list, result: str, expected_delta: int
) -> None:
    before = calculate_bayesian_rating(
        results=history, baseline=0.5, previous_ability=None
    )
    after = calculate_bayesian_rating(
        results=history + [result], baseline=0.5, previous_ability=None
    )

    assert before is not None
    assert after is not None
    assert after.score - before.score == expected_delta
