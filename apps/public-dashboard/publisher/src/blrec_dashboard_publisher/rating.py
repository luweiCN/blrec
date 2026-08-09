from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional, Sequence

__all__ = (
    'CARRYOVER_MATCH_CAP',
    'CATCHUP_LIMIT',
    'CATCHUP_LOSS_MULTIPLIER',
    'CATCHUP_PROTECTION_GAP',
    'CATCHUP_RATE',
    'MINIMUM_OUTCOME_DELTA',
    'NEUTRAL_DISPLAY_SCORE',
    'PRIOR_MATCHES',
    'PROBABILITY_SCALE',
    'PROVISIONAL_MATCHES',
    'RATING_MODEL_VERSION',
    'SEASON_RESET_DISPLAY_SCORE',
    'VirtualMatchRating',
    'calculate_virtual_match_rating',
    'expected_win_probability',
)


PRIOR_MATCHES = 20
CARRYOVER_MATCH_CAP = 200
PROVISIONAL_MATCHES = 5
NEUTRAL_DISPLAY_SCORE = 1200
SEASON_RESET_DISPLAY_SCORE = 1000
PROBABILITY_SCALE = 1800
MINIMUM_OUTCOME_DELTA = 1
CATCHUP_RATE = 0.08
CATCHUP_LIMIT = 45
CATCHUP_PROTECTION_GAP = 150
CATCHUP_LOSS_MULTIPLIER = 0.5
RATING_MODEL_VERSION = 3

_NEUTRAL_ABILITY = 0.5
_DISPLAY_SCORE_MULTIPLIER = 3
_DISPLAY_SCORE_MAXIMUM = 3000
_INTERNAL_SCORE_MAXIMUM = _DISPLAY_SCORE_MAXIMUM // _DISPLAY_SCORE_MULTIPLIER


@dataclass(frozen=True)
class VirtualMatchRating:
    ability: float
    evidence: float
    score: int
    provisional: bool


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


def _internal_outcome_delta(
    *,
    result: str,
    hidden_score_before: float,
    hidden_score_after: float,
    visible_score: int,
) -> int:
    display_delta = hidden_score_after - hidden_score_before
    placement_gap = hidden_score_after - (visible_score * _DISPLAY_SCORE_MULTIPLIER)
    if result == 'W' and placement_gap > 0.0:
        display_delta += min(CATCHUP_LIMIT, placement_gap * CATCHUP_RATE)
    elif result == 'L' and placement_gap >= CATCHUP_PROTECTION_GAP:
        display_delta *= CATCHUP_LOSS_MULTIPLIER

    internal_delta = round(display_delta / _DISPLAY_SCORE_MULTIPLIER)
    if result == 'W':
        return max(MINIMUM_OUTCOME_DELTA, internal_delta)
    return min(-MINIMUM_OUTCOME_DELTA, internal_delta)


def calculate_virtual_match_rating(
    *,
    results: Sequence[str],
    previous_ability: Optional[float] = None,
    previous_evidence: Optional[float] = None,
    reset_visible_score: bool = True,
) -> Optional[VirtualMatchRating]:
    if any(result not in ('W', 'L') for result in results):
        raise ValueError('virtual match rating result must be W or L')
    if not results:
        return None

    prior_ability, prior_evidence = _initial_evidence(
        previous_ability, previous_evidence
    )
    alpha = prior_ability * prior_evidence
    beta = (1.0 - prior_ability) * prior_evidence
    visible_score = round(
        (
            SEASON_RESET_DISPLAY_SCORE
            if reset_visible_score
            else _display_score_for_ability(prior_ability)
        )
        / _DISPLAY_SCORE_MULTIPLIER
    )

    for result in results:
        hidden_score_before = _display_score_for_ability(alpha / (alpha + beta))
        if result == 'W':
            alpha += 1.0
        else:
            beta += 1.0
        evidence = alpha + beta
        if evidence > CARRYOVER_MATCH_CAP:
            evidence_scale = CARRYOVER_MATCH_CAP / evidence
            alpha *= evidence_scale
            beta *= evidence_scale
        hidden_score_after = _display_score_for_ability(alpha / (alpha + beta))
        if reset_visible_score:
            visible_score += _internal_outcome_delta(
                result=result,
                hidden_score_before=hidden_score_before,
                hidden_score_after=hidden_score_after,
                visible_score=visible_score,
            )
            visible_score = max(0, min(_INTERNAL_SCORE_MAXIMUM, visible_score))

    ability = alpha / (alpha + beta)
    if not reset_visible_score:
        visible_score = round(
            _display_score_for_ability(ability) / _DISPLAY_SCORE_MULTIPLIER
        )
    return VirtualMatchRating(
        ability=ability,
        evidence=alpha + beta,
        score=visible_score,
        provisional=len(results) < PROVISIONAL_MATCHES,
    )
