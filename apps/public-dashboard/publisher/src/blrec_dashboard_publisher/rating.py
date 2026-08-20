from __future__ import annotations

import math
from bisect import bisect_right
from dataclasses import dataclass
from typing import Optional, Sequence

__all__ = (
    'CARRYOVER_MATCH_CAP',
    'NEUTRAL_DISPLAY_SCORE',
    'PRIOR_MATCHES',
    'PROBABILITY_SCALE',
    'PROVISIONAL_MATCHES',
    'RATING_MODEL_VERSION',
    'RatingForecast',
    'RatingGoalForecast',
    'RatingTransition',
    'NEW_PLAYER_DISPLAY_SCORE',
    'SEASON_RESET_ANCHOR_DISPLAY_SCORE',
    'SEASON_RESET_CARRYOVER_RATE',
    'VirtualMatchRating',
    'calculate_rating_forecast',
    'calculate_virtual_match_rating',
    'calculate_virtual_match_rating_timeline',
    'expected_win_probability',
)


PRIOR_MATCHES = 20
CARRYOVER_MATCH_CAP = 200
PROVISIONAL_MATCHES = 5
NEUTRAL_DISPLAY_SCORE = 1200
NEW_PLAYER_DISPLAY_SCORE = 1000
SEASON_RESET_ANCHOR_DISPLAY_SCORE = 1200
SEASON_RESET_CARRYOVER_RATE = 0.5
PROBABILITY_SCALE = 1800
RATING_MODEL_VERSION = 7

_NEUTRAL_ABILITY = 0.5
_DISPLAY_SCORE_MULTIPLIER = 3
_DISPLAY_SCORE_MAXIMUM = 3000
_INTERNAL_SCORE_MAXIMUM = _DISPLAY_SCORE_MAXIMUM // _DISPLAY_SCORE_MULTIPLIER
_RATING_GAP_WIN_BONUS_RATE = 0.04
_RATING_GAP_OUTCOME_ADJUSTMENT_RATE = 0.02
_MAXIMUM_WIN_BONUS = 18
_MAXIMUM_WIN_PENALTY = 6
_MAXIMUM_LOSS_PENALTY = 6
_MAXIMUM_TIER_TEN_HIDDEN_BENEFIT = 1
_STANDARD_MINIMUM_WIN_DELTA = 9
_MINIMUM_LOSS_DELTA = 3
_MAXIMUM_LOSS_DELTA = 18
_FORECAST_STAGNATION_MATCHES = 200
_TIER_TEN_BRONZE_DISPLAY_SCORE = 2400
_TIER_TEN_SILVER_DISPLAY_SCORE = 2600
_TIER_TEN_GOLD_DISPLAY_SCORE = 2800
_SKILL_TIER_START_POINTS = (
    0,
    109,
    218,
    327,
    436,
    545,
    654,
    763,
    872,
    981,
    1090,
    1200,
    1250,
    1300,
    1350,
    1400,
    1467,
    1533,
    1600,
    1667,
    1733,
    1800,
    1867,
    1933,
    2000,
    2134,
    2267,
    2400,
    2600,
    2800,
)


@dataclass(frozen=True)
class VirtualMatchRating:
    ability: float
    evidence: float
    score: float
    provisional: bool


@dataclass(frozen=True)
class RatingTransition:
    result: str
    rating_before: VirtualMatchRating
    rating_after: VirtualMatchRating

    @property
    def score_before(self) -> int:
        return round(self.rating_before.score * _DISPLAY_SCORE_MULTIPLIER)

    @property
    def score_after(self) -> int:
        return round(self.rating_after.score * _DISPLAY_SCORE_MULTIPLIER)

    @property
    def score_delta(self) -> int:
        return self.score_after - self.score_before


@dataclass(frozen=True)
class RatingGoalForecast:
    target_display_score: int
    all_win_matches: int
    current_win_rate_matches: Optional[int]


@dataclass(frozen=True)
class RatingForecast:
    next_win_score: float
    next_loss_score: float
    next_division: Optional[RatingGoalForecast]
    next_tier: Optional[RatingGoalForecast]
    ultimate: RatingGoalForecast


def expected_win_probability(display_score: float) -> float:
    if not math.isfinite(display_score):
        raise ValueError('virtual match display score must be finite')
    return 1.0 / (
        1.0
        + math.pow(10.0, (NEUTRAL_DISPLAY_SCORE - display_score) / PROBABILITY_SCALE)
    )


def _display_score_for_ability(ability: float) -> float:
    score = NEUTRAL_DISPLAY_SCORE + PROBABILITY_SCALE * math.log10(
        ability / (1.0 - ability)
    )
    return max(0.0, min(float(_DISPLAY_SCORE_MAXIMUM), score))


def _initial_evidence(
    previous_ability: Optional[float], previous_evidence: Optional[float]
) -> tuple[float, float]:
    if previous_ability is None:
        if previous_evidence is not None:
            raise ValueError(
                'virtual match previous evidence requires a previous ability'
            )
        return _NEUTRAL_ABILITY, float(PRIOR_MATCHES)
    if not 0.0 < previous_ability < 1.0:
        raise ValueError('virtual match previous ability must be between zero and one')
    if previous_evidence is None or previous_evidence <= 0.0:
        raise ValueError('virtual match previous evidence must be positive')
    return previous_ability, min(previous_evidence, float(CARRYOVER_MATCH_CAP))


def _initial_display_score(previous_ability: Optional[float]) -> float:
    if previous_ability is None:
        return float(NEW_PLAYER_DISPLAY_SCORE)
    inherited_score = _display_score_for_ability(previous_ability)
    compressed_score = (
        SEASON_RESET_ANCHOR_DISPLAY_SCORE
        + (inherited_score - SEASON_RESET_ANCHOR_DISPLAY_SCORE)
        * SEASON_RESET_CARRYOVER_RATE
    )
    return min(inherited_score, compressed_score)


def _baseline_display_deltas(visible_score: int) -> tuple[int, int]:
    if visible_score < 981:
        return 24, 6
    if visible_score < 1400:
        return 21, 9
    if visible_score < 2000:
        return 18, 12
    return 12, 12


def _tier_ten_display_deltas(visible_score: int) -> tuple[int, int]:
    if visible_score < _TIER_TEN_SILVER_DISPLAY_SCORE:
        progress = visible_score - _TIER_TEN_BRONZE_DISPLAY_SCORE
        return 6 - _round_display_adjustment(progress / 100), 12
    if visible_score < _TIER_TEN_GOLD_DISPLAY_SCORE:
        progress = visible_score - _TIER_TEN_SILVER_DISPLAY_SCORE
        return (
            4 - _round_display_adjustment(progress / 100),
            12 - _round_display_adjustment(progress * 2 / 200),
        )
    progress = visible_score - _TIER_TEN_GOLD_DISPLAY_SCORE
    return (
        max(1, 2 - _round_display_adjustment(progress / 100)),
        max(6, 10 - _round_display_adjustment(progress * 4 / 200)),
    )


def _round_display_adjustment(value: float) -> int:
    return int(value + 0.5)


def _internal_outcome_delta(
    *, result: str, hidden_score_before: float, visible_score: float
) -> float:
    visible_display_score = round(visible_score * _DISPLAY_SCORE_MULTIPLIER)
    rating_gap = hidden_score_before - visible_display_score
    if visible_display_score >= _TIER_TEN_BRONZE_DISPLAY_SCORE:
        win_delta, loss_delta = _tier_ten_display_deltas(visible_display_score)
        hidden_benefit = 0
        if rating_gap > 0.0:
            hidden_benefit = min(
                _MAXIMUM_TIER_TEN_HIDDEN_BENEFIT,
                _round_display_adjustment(
                    rating_gap
                    * (
                        _RATING_GAP_WIN_BONUS_RATE
                        if result == 'W'
                        else _RATING_GAP_OUTCOME_ADJUSTMENT_RATE
                    )
                ),
            )
        display_delta = (
            win_delta + hidden_benefit
            if result == 'W'
            else -(loss_delta - hidden_benefit)
        )
        return display_delta / _DISPLAY_SCORE_MULTIPLIER

    win_delta, loss_delta = _baseline_display_deltas(visible_display_score)
    if result == 'W':
        if rating_gap > 0.0:
            win_delta += min(
                _MAXIMUM_WIN_BONUS,
                _round_display_adjustment(rating_gap * _RATING_GAP_WIN_BONUS_RATE),
            )
        else:
            win_delta -= min(
                _MAXIMUM_WIN_PENALTY,
                _round_display_adjustment(
                    -rating_gap * _RATING_GAP_OUTCOME_ADJUSTMENT_RATE
                ),
            )
        display_delta = max(_STANDARD_MINIMUM_WIN_DELTA, win_delta)
    else:
        if rating_gap > 0.0:
            loss_delta -= min(
                loss_delta - _MINIMUM_LOSS_DELTA,
                _round_display_adjustment(
                    rating_gap * _RATING_GAP_OUTCOME_ADJUSTMENT_RATE
                ),
            )
        else:
            loss_delta += min(
                _MAXIMUM_LOSS_PENALTY,
                _round_display_adjustment(
                    -rating_gap * _RATING_GAP_OUTCOME_ADJUSTMENT_RATE
                ),
            )
        display_delta = -min(_MAXIMUM_LOSS_DELTA, max(_MINIMUM_LOSS_DELTA, loss_delta))
    return display_delta / _DISPLAY_SCORE_MULTIPLIER


def _advance_evidence(
    alpha: float, beta: float, win_weight: float
) -> tuple[float, float]:
    alpha += win_weight
    beta += 1.0 - win_weight
    evidence = alpha + beta
    if evidence > CARRYOVER_MATCH_CAP:
        evidence_scale = CARRYOVER_MATCH_CAP / evidence
        alpha *= evidence_scale
        beta *= evidence_scale
    return alpha, beta


def _advance_rating(rating: VirtualMatchRating, result: str) -> VirtualMatchRating:
    if result not in ('W', 'L'):
        raise ValueError('virtual match rating result must be W or L')
    alpha = rating.ability * rating.evidence
    beta = (1.0 - rating.ability) * rating.evidence
    hidden_score_before = _display_score_for_ability(rating.ability)
    alpha, beta = _advance_evidence(alpha, beta, 1.0 if result == 'W' else 0.0)
    evidence = alpha + beta
    ability = alpha / evidence
    score = rating.score + _internal_outcome_delta(
        result=result,
        hidden_score_before=hidden_score_before,
        visible_score=rating.score,
    )
    score = max(0.0, min(float(_INTERNAL_SCORE_MAXIMUM), score))
    return VirtualMatchRating(
        ability=ability, evidence=evidence, score=score, provisional=rating.provisional
    )


def _advance_expected_rating(
    rating: VirtualMatchRating, win_rate: float
) -> VirtualMatchRating:
    alpha = rating.ability * rating.evidence
    beta = (1.0 - rating.ability) * rating.evidence
    hidden_score_before = _display_score_for_ability(rating.ability)
    win_delta = _internal_outcome_delta(
        result='W', hidden_score_before=hidden_score_before, visible_score=rating.score
    )
    loss_delta = _internal_outcome_delta(
        result='L', hidden_score_before=hidden_score_before, visible_score=rating.score
    )
    alpha, beta = _advance_evidence(alpha, beta, win_rate)
    evidence = alpha + beta
    score = rating.score + win_rate * win_delta + (1.0 - win_rate) * loss_delta
    score = max(0.0, min(float(_INTERNAL_SCORE_MAXIMUM), score))
    return VirtualMatchRating(
        ability=alpha / evidence,
        evidence=evidence,
        score=score,
        provisional=rating.provisional,
    )


def _goal_target_scores(display_score: int) -> tuple[Optional[int], Optional[int], int]:
    division_index = bisect_right(_SKILL_TIER_START_POINTS, display_score) - 1
    tier = division_index // 3 + 1
    next_division = (
        _SKILL_TIER_START_POINTS[division_index + 1]
        if division_index + 1 < len(_SKILL_TIER_START_POINTS)
        else None
    )
    next_tier = _SKILL_TIER_START_POINTS[tier * 3] if tier < 10 else None
    ultimate = 2400 if tier <= 8 else 2800
    return next_division, next_tier, ultimate


def _all_win_matches_for_targets(
    rating: VirtualMatchRating, targets: Sequence[int]
) -> dict[int, int]:
    matches_by_target = {
        target: 0
        for target in targets
        if round(rating.score * _DISPLAY_SCORE_MULTIPLIER) >= target
    }
    pending = sorted(set(targets) - matches_by_target.keys())
    projected = rating
    for matches in range(1, _DISPLAY_SCORE_MAXIMUM + 1):
        if not pending:
            break
        projected = _advance_rating(projected, 'W')
        reached = [
            target
            for target in pending
            if round(projected.score * _DISPLAY_SCORE_MULTIPLIER) >= target
        ]
        for target in reached:
            matches_by_target[target] = matches
            pending.remove(target)
    if pending:
        raise RuntimeError('all-win virtual match forecast did not reach its target')
    return matches_by_target


def _current_win_rate_matches_for_targets(
    rating: VirtualMatchRating,
    win_rate: float,
    targets: Sequence[int],
    all_win_matches: dict[int, int],
) -> dict[int, Optional[int]]:
    current_display_score = round(rating.score * _DISPLAY_SCORE_MULTIPLIER)
    matches_by_target: dict[int, Optional[int]] = {
        target: 0 for target in targets if current_display_score >= target
    }
    if win_rate >= 1.0:
        return {target: all_win_matches[target] for target in targets}

    pending = sorted(set(targets) - matches_by_target.keys())
    projected = rating
    highest_display_score = current_display_score
    stagnant_matches = 0
    for matches in range(1, _DISPLAY_SCORE_MAXIMUM + 1):
        if not pending:
            break
        projected = _advance_expected_rating(projected, win_rate)
        projected_display_score = round(projected.score * _DISPLAY_SCORE_MULTIPLIER)
        if projected_display_score > highest_display_score:
            highest_display_score = projected_display_score
            stagnant_matches = 0
        else:
            stagnant_matches += 1
        reached = [target for target in pending if projected_display_score >= target]
        for target in reached:
            matches_by_target[target] = matches
            pending.remove(target)
        if stagnant_matches >= _FORECAST_STAGNATION_MATCHES:
            break
    for target in pending:
        matches_by_target[target] = None
    return matches_by_target


def calculate_rating_forecast(
    *,
    rating: VirtualMatchRating,
    win_rate: float,
    achieved_display_score: Optional[int] = None,
) -> RatingForecast:
    if not math.isfinite(win_rate) or not 0.0 <= win_rate <= 1.0:
        raise ValueError('virtual match forecast win rate must be between zero and one')
    current_display_score = round(rating.score * _DISPLAY_SCORE_MULTIPLIER)
    if achieved_display_score is not None:
        if not 0 <= achieved_display_score <= _DISPLAY_SCORE_MAXIMUM:
            raise ValueError(
                'virtual match achieved display score must be between zero and maximum'
            )
        goal_progress_score = max(current_display_score, achieved_display_score)
    else:
        goal_progress_score = current_display_score
    next_division_target, next_tier_target, ultimate_target = _goal_target_scores(
        goal_progress_score
    )
    targets = tuple(
        target
        for target in (next_division_target, next_tier_target, ultimate_target)
        if target is not None
    )
    all_win_matches = _all_win_matches_for_targets(rating, targets)
    current_win_rate_matches = _current_win_rate_matches_for_targets(
        rating, win_rate, targets, all_win_matches
    )

    def goal(target: Optional[int]) -> Optional[RatingGoalForecast]:
        if target is None:
            return None
        return RatingGoalForecast(
            target_display_score=target,
            all_win_matches=all_win_matches[target],
            current_win_rate_matches=current_win_rate_matches[target],
        )

    ultimate = goal(ultimate_target)
    if ultimate is None:
        raise AssertionError('virtual match forecast ultimate goal is required')
    return RatingForecast(
        next_win_score=_advance_rating(rating, 'W').score,
        next_loss_score=_advance_rating(rating, 'L').score,
        next_division=goal(next_division_target),
        next_tier=goal(next_tier_target),
        ultimate=ultimate,
    )


def calculate_virtual_match_rating(
    *,
    results: Sequence[str],
    previous_ability: Optional[float] = None,
    previous_evidence: Optional[float] = None,
    reset_visible_score: bool = True,
) -> Optional[VirtualMatchRating]:
    timeline = calculate_virtual_match_rating_timeline(
        results=results,
        previous_ability=previous_ability,
        previous_evidence=previous_evidence,
        reset_visible_score=reset_visible_score,
    )
    return None if not timeline else timeline[-1].rating_after


def calculate_virtual_match_rating_timeline(
    *,
    results: Sequence[str],
    previous_ability: Optional[float] = None,
    previous_evidence: Optional[float] = None,
    reset_visible_score: bool = True,
) -> tuple[RatingTransition, ...]:
    if any(result not in ('W', 'L') for result in results):
        raise ValueError('virtual match rating result must be W or L')
    if not results:
        return ()

    prior_ability, prior_evidence = _initial_evidence(
        previous_ability, previous_evidence
    )
    rating = VirtualMatchRating(
        ability=prior_ability,
        evidence=prior_evidence,
        score=float(
            round(
                (
                    _initial_display_score(previous_ability)
                    if reset_visible_score
                    else _display_score_for_ability(prior_ability)
                )
                / _DISPLAY_SCORE_MULTIPLIER
            )
        ),
        provisional=True,
    )
    transitions = []
    for match_number, result in enumerate(results, start=1):
        after = _advance_rating(rating, result)
        after = VirtualMatchRating(
            ability=after.ability,
            evidence=after.evidence,
            score=after.score,
            provisional=match_number < PROVISIONAL_MATCHES,
        )
        transitions.append(
            RatingTransition(result=result, rating_before=rating, rating_after=after)
        )
        rating = after
    return tuple(transitions)
