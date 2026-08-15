from __future__ import annotations

import math
from bisect import bisect_right
from dataclasses import dataclass
from typing import Optional, Sequence

__all__ = (
    'CARRYOVER_MATCH_CAP',
    'CATCHUP_ELITE_DISPLAY_SCORE',
    'CATCHUP_ELITE_LIMIT',
    'CATCHUP_HIGH_DISPLAY_SCORE',
    'CATCHUP_HIGH_LIMIT',
    'CATCHUP_LIMIT',
    'CATCHUP_LOSS_MULTIPLIER',
    'CATCHUP_PROTECTION_MATCHES',
    'CATCHUP_PROTECTION_GAP',
    'CATCHUP_RATE',
    'MINIMUM_OUTCOME_DELTA',
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
MINIMUM_OUTCOME_DELTA = 1
CATCHUP_RATE = 0.08
CATCHUP_LIMIT = 18
CATCHUP_HIGH_DISPLAY_SCORE = 2500
CATCHUP_HIGH_LIMIT = 12
CATCHUP_ELITE_DISPLAY_SCORE = 2700
CATCHUP_ELITE_LIMIT = 10
CATCHUP_PROTECTION_GAP = 150
CATCHUP_PROTECTION_MATCHES = 50
CATCHUP_LOSS_MULTIPLIER = 0.5
RATING_MODEL_VERSION = 4

_NEUTRAL_ABILITY = 0.5
_DISPLAY_SCORE_MULTIPLIER = 3
_DISPLAY_SCORE_MAXIMUM = 3000
_INTERNAL_SCORE_MAXIMUM = _DISPLAY_SCORE_MAXIMUM // _DISPLAY_SCORE_MULTIPLIER
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
    score: int
    provisional: bool
    season_matches: int = 0


@dataclass(frozen=True)
class RatingTransition:
    result: str
    rating_before: VirtualMatchRating
    rating_after: VirtualMatchRating

    @property
    def score_before(self) -> int:
        return self.rating_before.score * _DISPLAY_SCORE_MULTIPLIER

    @property
    def score_after(self) -> int:
        return self.rating_after.score * _DISPLAY_SCORE_MULTIPLIER

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
    next_win_score: int
    next_loss_score: int
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


def _catchup_limit(hidden_score: float) -> int:
    if hidden_score >= CATCHUP_ELITE_DISPLAY_SCORE:
        return CATCHUP_ELITE_LIMIT
    if hidden_score >= CATCHUP_HIGH_DISPLAY_SCORE:
        return CATCHUP_HIGH_LIMIT
    return CATCHUP_LIMIT


def _internal_outcome_delta(
    *,
    result: str,
    hidden_score_before: float,
    hidden_score_after: float,
    visible_score: float,
    season_match_number: int,
) -> int:
    display_delta = hidden_score_after - hidden_score_before
    placement_gap = hidden_score_after - (visible_score * _DISPLAY_SCORE_MULTIPLIER)
    if result == 'W' and placement_gap > 0.0:
        display_delta += min(
            _catchup_limit(hidden_score_after), placement_gap * CATCHUP_RATE
        )
    elif (
        result == 'L'
        and season_match_number <= CATCHUP_PROTECTION_MATCHES
        and placement_gap >= CATCHUP_PROTECTION_GAP
    ):
        display_delta *= CATCHUP_LOSS_MULTIPLIER

    internal_delta = round(display_delta / _DISPLAY_SCORE_MULTIPLIER)
    if result == 'W':
        return max(MINIMUM_OUTCOME_DELTA, internal_delta)
    return min(-MINIMUM_OUTCOME_DELTA, internal_delta)


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


def _advance_rating(
    rating: VirtualMatchRating, result: str, *, reset_visible_score: bool
) -> VirtualMatchRating:
    if result not in ('W', 'L'):
        raise ValueError('virtual match rating result must be W or L')
    alpha = rating.ability * rating.evidence
    beta = (1.0 - rating.ability) * rating.evidence
    hidden_score_before = _display_score_for_ability(rating.ability)
    alpha, beta = _advance_evidence(alpha, beta, 1.0 if result == 'W' else 0.0)
    evidence = alpha + beta
    ability = alpha / evidence
    hidden_score_after = _display_score_for_ability(ability)
    season_match_number = rating.season_matches + 1
    if reset_visible_score:
        score = rating.score + _internal_outcome_delta(
            result=result,
            hidden_score_before=hidden_score_before,
            hidden_score_after=hidden_score_after,
            visible_score=rating.score,
            season_match_number=season_match_number,
        )
        score = max(0, min(_INTERNAL_SCORE_MAXIMUM, score))
    else:
        score = round(hidden_score_after / _DISPLAY_SCORE_MULTIPLIER)
    return VirtualMatchRating(
        ability=ability,
        evidence=evidence,
        score=score,
        provisional=rating.provisional,
        season_matches=season_match_number,
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
    rating: VirtualMatchRating, targets: Sequence[int], *, reset_visible_score: bool
) -> dict[int, int]:
    matches_by_target = {target: 0 for target in targets if rating.score * 3 >= target}
    pending = sorted(set(targets) - matches_by_target.keys())
    projected = rating
    for matches in range(1, _DISPLAY_SCORE_MAXIMUM + 1):
        if not pending:
            break
        projected = _advance_rating(
            projected, 'W', reset_visible_score=reset_visible_score
        )
        reached = [target for target in pending if projected.score * 3 >= target]
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
    *,
    reset_visible_score: bool,
) -> dict[int, Optional[int]]:
    current_display_score = rating.score * _DISPLAY_SCORE_MULTIPLIER
    matches_by_target: dict[int, Optional[int]] = {}
    if win_rate >= 1.0:
        return {target: all_win_matches[target] for target in targets}

    next_win_score = _advance_rating(
        rating, 'W', reset_visible_score=reset_visible_score
    ).score
    next_loss_score = _advance_rating(
        rating, 'L', reset_visible_score=reset_visible_score
    ).score
    expected_display_delta = _DISPLAY_SCORE_MULTIPLIER * (
        win_rate * (next_win_score - rating.score)
        + (1.0 - win_rate) * (next_loss_score - rating.score)
    )
    for target in targets:
        remaining_score = max(0, target - current_display_score)
        matches_by_target[target] = (
            0
            if remaining_score == 0
            else (
                math.ceil(remaining_score / expected_display_delta)
                if expected_display_delta > 0.0
                else None
            )
        )
    return matches_by_target


def calculate_rating_forecast(
    *, rating: VirtualMatchRating, win_rate: float, reset_visible_score: bool
) -> RatingForecast:
    if not math.isfinite(win_rate) or not 0.0 <= win_rate <= 1.0:
        raise ValueError('virtual match forecast win rate must be between zero and one')
    next_division_target, next_tier_target, ultimate_target = _goal_target_scores(
        rating.score * _DISPLAY_SCORE_MULTIPLIER
    )
    targets = tuple(
        target
        for target in (next_division_target, next_tier_target, ultimate_target)
        if target is not None
    )
    all_win_matches = _all_win_matches_for_targets(
        rating, targets, reset_visible_score=reset_visible_score
    )
    current_win_rate_matches = _current_win_rate_matches_for_targets(
        rating,
        win_rate,
        targets,
        all_win_matches,
        reset_visible_score=reset_visible_score,
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
        next_win_score=_advance_rating(
            rating, 'W', reset_visible_score=reset_visible_score
        ).score,
        next_loss_score=_advance_rating(
            rating, 'L', reset_visible_score=reset_visible_score
        ).score,
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
        score=round(
            (
                _initial_display_score(previous_ability)
                if reset_visible_score
                else _display_score_for_ability(prior_ability)
            )
            / _DISPLAY_SCORE_MULTIPLIER
        ),
        provisional=True,
    )
    transitions = []
    for match_number, result in enumerate(results, start=1):
        after = _advance_rating(rating, result, reset_visible_score=reset_visible_score)
        after = VirtualMatchRating(
            ability=after.ability,
            evidence=after.evidence,
            score=after.score,
            provisional=match_number < PROVISIONAL_MATCHES,
            season_matches=after.season_matches,
        )
        transitions.append(
            RatingTransition(result=result, rating_before=rating, rating_after=after)
        )
        rating = after
    return tuple(transitions)
