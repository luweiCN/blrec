from typing import Callable, Dict, List, Sequence, Set

import pytest

from blrec.bili.batch_status_client import BatchProtocolError, BatchStatusClient
from blrec.bili.live_connection_controller import LiveConnectionController
from blrec.bili.live_status import (
    BatchStatusResult,
    BreakerState,
    ObservedStatus,
    StatusSnapshot,
    StatusSource,
)
from blrec.bili.live_status_coordinator import LiveStatusCoordinator
from blrec.bili.models import LiveStatus, RoomInfo

LIVE_UIDS = frozenset(range(1, 6))
LIVE_TIMES = {uid: 1_700_000_000 + uid for uid in LIVE_UIDS}


class MutableClock:
    def __init__(self, value: float = 0.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def status_snapshot(
    uid: int,
    status: ObservedStatus,
    clock: MutableClock,
    source: StatusSource = StatusSource.BATCH,
) -> StatusSnapshot:
    live_time = LIVE_TIMES[uid] if status is ObservedStatus.LIVE else 0
    return StatusSnapshot(
        uid=uid,
        room_id=uid + 1000,
        status=status,
        observed_at=clock(),
        source=source,
        live_time=live_time,
        observation_key='{}:{}'.format(uid, live_time) if live_time else None,
    )


def room_info(uid: int, status: LiveStatus = LiveStatus.LIVE) -> RoomInfo:
    return RoomInfo(
        uid=uid,
        room_id=uid + 1000,
        short_room_id=0,
        area_id=0,
        area_name='',
        parent_area_id=0,
        parent_area_name='',
        live_status=status,
        live_start_time=LIVE_TIMES.get(uid, 0),
        online=0,
        title='',
        cover='',
        tags='',
        description='',
    )


class FakeBatchClient(BatchStatusClient):
    def __init__(self, clock: MutableClock) -> None:
        self.clock = clock
        self.phase = 'offline'
        self.requests_this_poll = 0
        self.requests_per_poll: List[int] = []
        self.missing_items = 0
        self.rate_limits = 0

    @property
    def max_requests_per_poll(self) -> int:
        return max(self.requests_per_poll, default=0)

    def begin_poll(self, phase: str) -> None:
        self.phase = phase
        self.requests_this_poll = 0

    def end_poll(self) -> None:
        self.requests_per_poll.append(self.requests_this_poll)

    async def fetch(
        self, uids: Sequence[int], *, observed_at: float
    ) -> BatchStatusResult:
        self.requests_this_poll += 1
        if self.phase == 'rate_limit' and self.rate_limits == 0:
            self.rate_limits += 1
            raise BatchProtocolError('HTTP 429')

        missing = {1} if self.phase == 'missing' and 1 in uids else set()
        self.missing_items += len(missing)
        live_phase = self.phase in ('live', 'missing')
        snapshots = {
            uid: status_snapshot(
                uid,
                (
                    ObservedStatus.LIVE
                    if live_phase and uid in LIVE_UIDS
                    else ObservedStatus.PREPARING
                ),
                self.clock,
            )
            for uid in uids
            if uid not in missing
        }
        return BatchStatusResult(snapshots, frozenset(missing))


class FakeSingleRoom:
    def __init__(self, clock: MutableClock) -> None:
        self.clock = clock
        self.live_uids: Set[int] = set()
        self.confirmation_times: List[float] = []

    async def confirm_status(self, uid: int) -> StatusSnapshot:
        self.confirmation_times.append(self.clock())
        status = (
            ObservedStatus.LIVE if uid in self.live_uids else ObservedStatus.PREPARING
        )
        return status_snapshot(uid, status, self.clock, StatusSource.CONFIRMATION)

    async def load_room_info(self, room_id: int) -> RoomInfo:
        return room_info(room_id - 1000)

    def max_requests_in_window(self, seconds: float) -> int:
        return max(
            (
                sum(start <= item < start + seconds for item in self.confirmation_times)
                for start in self.confirmation_times
            ),
            default=0,
        )


class FakeDanmaku:
    def __init__(self) -> None:
        self.total_start_calls = 0
        self.max_concurrent_connections = 0
        self.active_connections = 0

    def connection(self) -> 'FakeDanmakuConnection':
        return FakeDanmakuConnection(self)


class FakeDanmakuConnection:
    def __init__(self, fleet: FakeDanmaku) -> None:
        self.fleet = fleet
        self.active = False
        self.room_id = 0

    def set_room_id(self, room_id: int) -> None:
        assert not self.active
        self.room_id = room_id

    async def start(self) -> None:
        assert not self.active
        self.active = True
        self.fleet.total_start_calls += 1
        self.fleet.active_connections += 1
        self.fleet.max_concurrent_connections = max(
            self.fleet.max_concurrent_connections, self.fleet.active_connections
        )

    async def stop(self) -> None:
        assert self.active
        self.active = False
        self.fleet.active_connections -= 1


class FakeRecorder:
    def __init__(self, breaker_state: Callable[[], BreakerState]) -> None:
        self.breaker_state = breaker_state
        self.active_uids: Set[int] = set()
        self.started_broadcasts = 0
        self.stopped_broadcasts = 0
        self.stop_breaker_states: List[BreakerState] = []

    @property
    def stops_during_breaker(self) -> int:
        return sum(
            state is not BreakerState.CLOSED for state in self.stop_breaker_states
        )

    def start(self, uid: int) -> None:
        assert uid not in self.active_uids
        self.active_uids.add(uid)
        self.started_broadcasts += 1

    def stop(self, uid: int) -> None:
        assert uid in self.active_uids
        self.active_uids.remove(uid)
        self.stopped_broadcasts += 1
        self.stop_breaker_states.append(self.breaker_state())


class FakeMonitor:
    def __init__(self, uid: int, recorder: FakeRecorder) -> None:
        self.uid = uid
        self.recorder = recorder
        self.enabled = False

    def enable(self) -> None:
        self.enabled = True

    def disable(self) -> None:
        self.enabled = False

    async def apply_confirmed_status(self, status: ObservedStatus) -> None:
        if status is ObservedStatus.LIVE:
            self.recorder.start(self.uid)
        elif status in (ObservedStatus.PREPARING, ObservedStatus.ROUND):
            self.recorder.stop(self.uid)


class FakeLive:
    def __init__(self, uid: int) -> None:
        self.room_id = uid + 1000
        self.room_info = room_info(uid, LiveStatus.PREPARING)

    def replace_room_info(self, value: RoomInfo) -> None:
        self.room_info = value


@pytest.mark.asyncio
async def test_58_room_batch_live_monitor_rollout_scenario() -> None:
    clock = MutableClock()
    fake_batch = FakeBatchClient(clock)
    coordinator = LiveStatusCoordinator(fake_batch, clock=clock)
    fake_danmaku = FakeDanmaku()
    fake_single_room = FakeSingleRoom(clock)
    fake_recorder = FakeRecorder(lambda: coordinator.metrics(clock()).breaker_state)
    controllers: Dict[int, LiveConnectionController] = {}

    for uid in range(1, 59):
        live = FakeLive(uid)
        controller = LiveConnectionController(
            live,  # type: ignore[arg-type]
            fake_danmaku.connection(),  # type: ignore[arg-type]
            FakeMonitor(uid, fake_recorder),  # type: ignore[arg-type]
            fake_single_room.load_room_info,
            status_sink=coordinator.observe_wss,
            registration_key=uid + 1000,
        )
        coordinator.register(
            uid,
            uid + 1000,
            controller.on_confirmed_status,
            lambda uid=uid: fake_single_room.confirm_status(uid),
        )
        controllers[uid] = controller

    async def poll(phase: str) -> None:
        fake_batch.begin_poll(phase)
        try:
            await coordinator.poll_once()
        finally:
            fake_batch.end_poll()

    await poll('offline')
    assert coordinator.metrics(clock()).active_websockets == 0

    fake_single_room.live_uids.update(LIVE_UIDS)
    await poll('live')
    assert coordinator.metrics(clock()).active_websockets == 5

    await poll('missing')
    assert coordinator.metrics(clock()).active_websockets == 5

    await poll('rate_limit')
    assert coordinator.metrics(clock()).breaker_state is BreakerState.OPEN

    await controllers[1].on_wss_hint(ObservedStatus.PREPARING)
    assert fake_recorder.stops_during_breaker == 0
    await poll('rate_limit')

    clock.advance(30)
    await poll('live')
    assert coordinator.metrics(clock()).breaker_state is BreakerState.HALF_OPEN
    await poll('live')
    assert coordinator.metrics(clock()).breaker_state is BreakerState.CLOSED

    fake_single_room.live_uids.clear()
    await poll('offline')
    await poll('offline')

    final_metrics = coordinator.metrics(clock())

    assert fake_danmaku.total_start_calls == 5
    assert fake_danmaku.max_concurrent_connections == 5
    assert fake_danmaku.active_connections == 0
    assert fake_recorder.started_broadcasts == 5
    assert fake_recorder.stopped_broadcasts == 5
    assert fake_recorder.stops_during_breaker == 0
    assert fake_batch.max_requests_per_poll == 2
    assert fake_single_room.max_requests_in_window(600) <= 5
    assert fake_batch.missing_items == 1
    assert fake_batch.rate_limits == 1
    assert final_metrics.registered_rooms == 58
    assert final_metrics.active_websockets == 0
