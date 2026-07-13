import asyncio
import importlib
from typing import TYPE_CHECKING, Callable, Dict, List, Sequence, Set, Tuple

import pytest

from blrec.bili.batch_status_client import BatchProtocolError, BatchStatusClient
from blrec.bili.live import Live
from blrec.bili.live_connection_controller import LiveConnectionController
from blrec.bili.live_monitor import LiveEventListener, LiveMonitor
from blrec.bili.live_status import (
    BatchStatusResult,
    BreakerState,
    ObservedStatus,
    StatusSnapshot,
    StatusSource,
)
from blrec.bili.live_status_coordinator import LiveStatusCoordinator
from blrec.bili.models import LiveStatus, RoomInfo, UserInfo

if TYPE_CHECKING:
    from blrec.task.task import RecordTask

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

    async def fetch_uid_mappings(
        self, room_ids: Sequence[int]
    ) -> Dict[int, Tuple[int, int]]:
        return {room_id: (room_id, room_id - 1000) for room_id in room_ids}

    async def confirm_status(self, room_id: int) -> StatusSnapshot:
        uid = room_id - 1000
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
        self.connections: List['FakeDanmakuConnection'] = []

    def connection(self) -> 'FakeDanmakuConnection':
        connection = FakeDanmakuConnection(self)
        self.connections.append(connection)
        return connection


class FakeDanmakuConnection:
    def __init__(self, fleet: FakeDanmaku) -> None:
        self.fleet = fleet
        self.active = False
        self.room_id = 0
        self.listeners: List[object] = []

    def add_listener(self, listener: object) -> None:
        if listener not in self.listeners:
            self.listeners.append(listener)

    def remove_listener(self, listener: object) -> None:
        if listener in self.listeners:
            self.listeners.remove(listener)

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


class FakeRecorder(LiveEventListener):
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

    async def on_live_began(self, live: Live) -> None:
        uid = live.room_info.uid
        assert uid not in self.active_uids
        self.active_uids.add(uid)
        self.started_broadcasts += 1

    async def on_live_ended(self, live: Live) -> None:
        uid = live.room_info.uid
        assert uid in self.active_uids
        self.active_uids.remove(uid)
        self.stopped_broadcasts += 1
        self.stop_breaker_states.append(self.breaker_state())


class FakeLive:
    def __init__(self, room_id: int, user_agent: str = '', cookie: str = '') -> None:
        uid = room_id - 1000
        self.room_id = room_id
        self.room_info = room_info(uid, LiveStatus.PREPARING)
        self.user_info = UserInfo('', '', '', uid)

    def replace_room_info(self, value: RoomInfo) -> None:
        self.room_info = value

    async def get_live_streams(self) -> List[Dict[str, object]]:
        return [{'format': [{'format_name': 'flv'}]}]


@pytest.mark.asyncio
async def test_58_room_batch_live_monitor_rollout_scenario(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    importlib.import_module('blrec.setting')
    task_module = importlib.import_module('blrec.task.task')
    monkeypatch.setattr(task_module, 'Live', FakeLive)
    clock = MutableClock()
    fake_batch = FakeBatchClient(clock)
    coordinator = LiveStatusCoordinator(fake_batch, clock=clock)
    fake_danmaku = FakeDanmaku()
    fake_single_room = FakeSingleRoom(clock)
    fake_recorder = FakeRecorder(lambda: coordinator.metrics(clock()).breaker_state)
    tasks: List['RecordTask'] = []
    controllers: Dict[int, LiveConnectionController] = {}

    for uid in range(1, 59):
        task = task_module.RecordTask(
            uid + 1000,
            live_status_coordinator=coordinator,
            anonymous_room_client=fake_single_room,  # type: ignore[arg-type]
        )
        task._danmaku_client = fake_danmaku.connection()
        task._setup_live_monitor()
        assert isinstance(task._live_monitor, LiveMonitor)
        assert isinstance(task._connection_controller, LiveConnectionController)
        task._live_monitor.add_listener(fake_recorder)
        tasks.append(task)
        await task.enable_monitor()
        controllers[uid] = task._connection_controller

    async def drain_notifications() -> None:
        while coordinator._notification_tasks:
            await asyncio.gather(*tuple(coordinator._notification_tasks))

    async def poll(phase: str) -> None:
        fake_batch.begin_poll(phase)
        try:
            await coordinator.poll_once()
            await drain_notifications()
        finally:
            fake_batch.end_poll()

    try:
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
        await drain_notifications()
        assert fake_recorder.stops_during_breaker == 0
        await poll('rate_limit')

        clock.advance(30)
        await poll('live')
        assert coordinator.metrics(clock()).breaker_state is BreakerState.HALF_OPEN
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
        assert final_metrics.missing_results == 1
        assert final_metrics.fallback_requests == 5
    finally:
        for task in tasks:
            await task._live_monitor._stop_checking()
        for task in tasks:
            await task.disable_monitor()
            task._live_monitor.remove_listener(fake_recorder)
        await coordinator.stop()

    assert coordinator.metrics(clock()).registered_rooms == 0
    assert all(not connection.active for connection in fake_danmaku.connections)
    assert all(not connection.listeners for connection in fake_danmaku.connections)
