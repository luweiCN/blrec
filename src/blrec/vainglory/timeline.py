from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class HudObservation:
    part_index: int
    at_ms: int
    timer_seconds: Optional[int]
    lineup_fingerprint: Optional[str]


@dataclass(frozen=True)
class MatchBoundary:
    previous_part_index: int
    previous_last_seen_ms: int
    next_part_index: int
    next_first_seen_ms: int


@dataclass
class _PendingReset:
    observation: HudObservation
    previous: HudObservation
    confirmations: int = 1


class MatchTimeline:
    def __init__(
        self,
        *,
        confirmations: int = 1,
        long_gap_ms: int = 30_000,
        reset_ceiling_seconds: int = 20,
        reset_drop_seconds: int = 20,
    ) -> None:
        if confirmations < 1:
            raise ValueError('confirmations must be positive')
        if long_gap_ms < 0:
            raise ValueError('long gap must not be negative')
        self._confirmations = confirmations
        self._long_gap_ms = long_gap_ms
        self._reset_ceiling_seconds = reset_ceiling_seconds
        self._reset_drop_seconds = reset_drop_seconds
        self._last: Optional[HudObservation] = None
        self._lineup: Optional[str] = None
        self._pending: Optional[_PendingReset] = None

    def observe(self, value: HudObservation) -> Optional[MatchBoundary]:
        if value.timer_seconds is None and value.lineup_fingerprint is None:
            return None
        previous = self._last
        if previous is None:
            self._accept(value)
            return None

        lineup_changed = (
            self._lineup is not None
            and value.lineup_fingerprint is not None
            and value.lineup_fingerprint != self._lineup
        )
        long_gap = self._elapsed(previous, value) >= self._long_gap_ms
        if lineup_changed and long_gap:
            return self._finish_boundary(previous, value)

        if self._looks_like_timer_reset(previous, value):
            pending = self._pending
            if pending is None:
                pending = _PendingReset(value, previous)
                self._pending = pending
            else:
                pending.observation = value
                pending.confirmations += 1
            if pending.confirmations >= self._confirmations:
                return self._finish_boundary(pending.previous, pending.observation)
            return None

        pending = self._pending
        if pending is not None:
            if self._confirms_pending_reset(pending.observation, value):
                pending.confirmations += 1
                if pending.confirmations >= self._confirmations:
                    return self._finish_boundary(pending.previous, value)
                return None
            self._pending = None

        self._accept(value)
        return None

    def _accept(self, value: HudObservation) -> None:
        self._last = value
        if value.lineup_fingerprint is not None:
            self._lineup = value.lineup_fingerprint

    def _finish_boundary(
        self, previous: HudObservation, value: HudObservation
    ) -> MatchBoundary:
        boundary = MatchBoundary(
            previous_part_index=previous.part_index,
            previous_last_seen_ms=previous.at_ms,
            next_part_index=value.part_index,
            next_first_seen_ms=value.at_ms,
        )
        self._last = value
        self._lineup = value.lineup_fingerprint
        self._pending = None
        return boundary

    def _looks_like_timer_reset(
        self, previous: HudObservation, value: HudObservation
    ) -> bool:
        if previous.timer_seconds is None or value.timer_seconds is None:
            return False
        return (
            value.timer_seconds <= self._reset_ceiling_seconds
            and previous.timer_seconds - value.timer_seconds >= self._reset_drop_seconds
        )

    @staticmethod
    def _confirms_pending_reset(
        candidate: HudObservation, value: HudObservation
    ) -> bool:
        if candidate.timer_seconds is None or value.timer_seconds is None:
            return False
        elapsed_seconds = max(0, value.at_ms - candidate.at_ms) // 1_000
        expected = candidate.timer_seconds + elapsed_seconds
        return candidate.timer_seconds <= value.timer_seconds <= expected + 5

    @staticmethod
    def _elapsed(previous: HudObservation, value: HudObservation) -> int:
        if value.part_index != previous.part_index:
            return 2**31 - 1
        return max(0, value.at_ms - previous.at_ms)
