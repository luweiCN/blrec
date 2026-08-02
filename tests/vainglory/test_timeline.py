from typing import Optional

from blrec.vainglory.timeline import HudObservation, MatchTimeline


def observation(
    at_seconds: int,
    *,
    timer_seconds: Optional[int],
    lineup: Optional[str] = 'same-lineup',
) -> HudObservation:
    return HudObservation(
        part_index=1,
        at_ms=at_seconds * 1_000,
        timer_seconds=timer_seconds,
        lineup_fingerprint=lineup,
    )


def test_timeline_keeps_one_match_through_short_hud_gaps() -> None:
    timeline = MatchTimeline()

    assert timeline.observe(observation(5, timer_seconds=3)) is None
    assert timeline.observe(observation(20, timer_seconds=18)) is None
    assert timeline.observe(observation(25, timer_seconds=None, lineup=None)) is None
    assert timeline.observe(observation(35, timer_seconds=33)) is None


def test_timeline_detects_reset_even_when_lineup_is_unchanged() -> None:
    timeline = MatchTimeline()
    timeline.observe(observation(5, timer_seconds=3))
    timeline.observe(observation(35, timer_seconds=33))
    timeline.observe(observation(65, timer_seconds=63))
    timeline.observe(observation(95, timer_seconds=None, lineup=None))

    boundary = timeline.observe(observation(125, timer_seconds=8))

    assert boundary is not None
    assert boundary.previous_last_seen_ms == 65_000
    assert boundary.next_first_seen_ms == 125_000


def test_timeline_uses_changed_lineup_after_a_long_gap() -> None:
    timeline = MatchTimeline()
    timeline.observe(observation(5, timer_seconds=3, lineup='lineup-a'))
    timeline.observe(observation(35, timer_seconds=33, lineup='lineup-a'))
    timeline.observe(observation(65, timer_seconds=None, lineup=None))

    boundary = timeline.observe(observation(100, timer_seconds=None, lineup='lineup-b'))

    assert boundary is not None
    assert boundary.previous_last_seen_ms == 35_000
    assert boundary.next_first_seen_ms == 100_000


def test_timeline_does_not_split_on_one_bad_timer_reading() -> None:
    timeline = MatchTimeline(confirmations=2)
    timeline.observe(observation(5, timer_seconds=3))
    timeline.observe(observation(35, timer_seconds=33))

    assert timeline.observe(observation(40, timer_seconds=4)) is None
    assert timeline.observe(observation(45, timer_seconds=43)) is None
