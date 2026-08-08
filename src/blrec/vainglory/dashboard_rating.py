from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional, Sequence

__all__ = (
    'CARRYOVER_RATE',
    'CREDIBLE_LEVEL',
    'MINIMUM_OUTCOME_DELTA',
    'PRIOR_MATCHES',
    'PROVISIONAL_MATCHES',
    'RATING_MODEL_VERSION',
    'BayesianRating',
    'calculate_bayesian_rating',
)


PRIOR_MATCHES = 20
CARRYOVER_RATE = 0.25
CREDIBLE_LEVEL = 0.9
PROVISIONAL_MATCHES = 5
MINIMUM_OUTCOME_DELTA = 1
RATING_MODEL_VERSION = 2
_LOWER_QUANTILE = 1.0 - CREDIBLE_LEVEL
_SCORE_SCALE = 1000


@dataclass(frozen=True)
class BayesianRating:
    ability: float
    score: int
    provisional: bool


def _beta_continued_fraction(alpha: float, beta: float, value: float) -> float:
    maximum_iterations = 240
    epsilon = 3e-14
    minimum = 1e-300
    combined = alpha + beta
    alpha_plus_one = alpha + 1.0
    alpha_minus_one = alpha - 1.0
    denominator = 1.0 - combined * value / alpha_plus_one
    if abs(denominator) < minimum:
        denominator = minimum
    denominator = 1.0 / denominator
    numerator = 1.0
    result = denominator
    for iteration in range(1, maximum_iterations + 1):
        doubled = 2 * iteration
        coefficient = (
            iteration
            * (beta - iteration)
            * value
            / ((alpha_minus_one + doubled) * (alpha + doubled))
        )
        denominator = 1.0 + coefficient * denominator
        if abs(denominator) < minimum:
            denominator = minimum
        numerator = 1.0 + coefficient / numerator
        if abs(numerator) < minimum:
            numerator = minimum
        denominator = 1.0 / denominator
        result *= denominator * numerator

        coefficient = -(
            (alpha + iteration)
            * (combined + iteration)
            * value
            / ((alpha + doubled) * (alpha_plus_one + doubled))
        )
        denominator = 1.0 + coefficient * denominator
        if abs(denominator) < minimum:
            denominator = minimum
        numerator = 1.0 + coefficient / numerator
        if abs(numerator) < minimum:
            numerator = minimum
        denominator = 1.0 / denominator
        change = denominator * numerator
        result *= change
        if abs(change - 1.0) <= epsilon:
            return result
    raise ArithmeticError('Bayesian rating beta fraction did not converge')


def _regularized_incomplete_beta(value: float, alpha: float, beta: float) -> float:
    if value <= 0.0:
        return 0.0
    if value >= 1.0:
        return 1.0
    scale = math.exp(
        math.lgamma(alpha + beta)
        - math.lgamma(alpha)
        - math.lgamma(beta)
        + alpha * math.log(value)
        + beta * math.log1p(-value)
    )
    if value < (alpha + 1.0) / (alpha + beta + 2.0):
        return scale * _beta_continued_fraction(alpha, beta, value) / alpha
    return 1.0 - (scale * _beta_continued_fraction(beta, alpha, 1.0 - value) / beta)


def _beta_quantile(probability: float, alpha: float, beta: float) -> float:
    lower = 0.0
    upper = 1.0
    for _iteration in range(64):
        midpoint = (lower + upper) / 2.0
        if _regularized_incomplete_beta(midpoint, alpha, beta) < probability:
            lower = midpoint
        else:
            upper = midpoint
    return (lower + upper) / 2.0


def calculate_bayesian_rating(
    *,
    results: Sequence[str],
    baseline: float,
    previous_ability: Optional[float],
    carryover_rate: float = CARRYOVER_RATE,
) -> Optional[BayesianRating]:
    if not 0.0 < baseline < 1.0:
        raise ValueError('Bayesian rating baseline must be between zero and one')
    if previous_ability is not None and not 0.0 <= previous_ability <= 1.0:
        raise ValueError('Bayesian previous ability must be between zero and one')
    if not 0.0 <= carryover_rate <= 1.0:
        raise ValueError('Bayesian carryover rate must be between zero and one')
    if any(result not in ('W', 'L') for result in results):
        raise ValueError('Bayesian rating result must be W or L')
    matches = len(results)
    if matches == 0:
        return None

    prior_mean = baseline
    if previous_ability is not None:
        prior_mean = (
            baseline * (1.0 - carryover_rate) + previous_ability * carryover_rate
        )
    alpha = prior_mean * PRIOR_MATCHES
    beta = (1.0 - prior_mean) * PRIOR_MATCHES
    score = round(_beta_quantile(_LOWER_QUANTILE, alpha, beta) * _SCORE_SCALE)
    for result in results:
        if result == 'W':
            alpha += 1.0
        else:
            beta += 1.0
        target_score = round(
            _beta_quantile(_LOWER_QUANTILE, alpha, beta) * _SCORE_SCALE
        )
        if result == 'W':
            score = max(target_score, score + MINIMUM_OUTCOME_DELTA)
        else:
            score = min(target_score, score - MINIMUM_OUTCOME_DELTA)
        score = max(0, min(_SCORE_SCALE, score))
    ability = alpha / (alpha + beta)
    return BayesianRating(
        ability=ability, score=score, provisional=matches < PROVISIONAL_MATCHES
    )
