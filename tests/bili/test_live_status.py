from dataclasses import FrozenInstanceError

import pytest

from blrec.bili.live_status import ObservedStatus, StatusSnapshot, StatusSource


def test_status_snapshot_is_immutable_and_keeps_unknown_state() -> None:
    snapshot = StatusSnapshot(
        uid=10,
        room_id=20,
        status=ObservedStatus.UNKNOWN,
        observed_at=30.0,
        source=StatusSource.BATCH,
        live_time=0,
        observation_key=None,
    )

    assert snapshot.status is ObservedStatus.UNKNOWN
    with pytest.raises(FrozenInstanceError):
        snapshot.room_id = 21  # type: ignore[misc]
