import asyncio
from collections import deque
from typing import Deque, List, Optional, Sequence, Union
from unittest.mock import AsyncMock

import pytest

from blrec.bili.batch_status_client import (
    BatchApiError,
    BatchProtocolError,
    BatchStatusClient,
)
from blrec.bili.live_status import (
    BatchStatusResult,
    BreakerState,
    ObservedStatus,
    StatusSnapshot,
    StatusSource,
)
from blrec.bili.live_status_coordinator import (
    LiveStatusCoordinator,
    StatusCircuitBreaker,
)


def snapshot(
    uid: int,
    status: ObservedStatus,
    *,
    live_time: int = 1,
    source: StatusSource = StatusSource.BATCH,
) -> StatusSnapshot:
    return StatusSnapshot(
        uid=uid,
        room_id=uid + 1000,
        status=status,
        observed_at=100.0,
        source=source,
        live_time=live_time,
        observation_key='{}:{}'.format(uid, live_time) if live_time else None,
    )


def batch_result(
    uid: int, status: Optional[ObservedStatus], live_time: int = 1
) -> BatchStatusResult:
    if status is None:
        return BatchStatusResult({}, frozenset({uid}))
    return BatchStatusResult(
        {uid: snapshot(uid, status, live_time=live_time)}, frozenset()
    )


def batch_for(
    uids: Sequence[int],
    status: ObservedStatus,
    *,
    missing: Sequence[int] = (),
    live_time: int = 1,
) -> BatchStatusResult:
    missing_uids = frozenset(missing)
    snapshots = {
        uid: snapshot(uid, status, live_time=live_time)
        for uid in uids
        if uid not in missing_uids
    }
    return BatchStatusResult(snapshots, missing_uids)


def confirmation(
    uid: int, status: ObservedStatus, live_time: int = 1
) -> StatusSnapshot:
    return snapshot(uid, status, live_time=live_time, source=StatusSource.CONFIRMATION)


ScriptedOutcome = Union[BatchStatusResult, BaseException]


class ScriptedBatchClient(BatchStatusClient):
    def __init__(
        self,
        results: Optional[Sequence[ScriptedOutcome]] = None,
        error: Optional[BaseException] = None,
    ) -> None:
        self.calls: List[List[int]] = []
        self.results: Deque[ScriptedOutcome] = deque(results or [])
        self.error = error

    async def fetch(
        self, uids: Sequence[int], *, observed_at: float
    ) -> BatchStatusResult:
        self.calls.append(list(uids))
        if self.error is not None:
            raise self.error
        if self.results:
            result = self.results.popleft()
            if isinstance(result, BaseException):
                raise result
            return result
        return BatchStatusResult({}, frozenset(uids))


class BlockingBatchClient(BatchStatusClient):
    def __init__(self) -> None:
        self.calls: List[List[int]] = []
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.active = 0
        self.max_active = 0

    async def fetch(
        self, uids: Sequence[int], *, observed_at: float
    ) -> BatchStatusResult:
        self.calls.append(list(uids))
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            if len(self.calls) == 1:
                self.entered.set()
                await self.release.wait()
            return batch_for(uids, ObservedStatus.PREPARING)
        finally:
            self.active -= 1


class MutableClock:
    def __init__(self, value: float = 100.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


async def wait_for_calls(client: ScriptedBatchClient, count: int) -> None:
    for _ in range(50):
        if len(client.calls) >= count:
            return
        await asyncio.sleep(0)
    raise AssertionError('timed out waiting for batch calls')


@pytest.mark.asyncio
async def test_58_rooms_are_deduplicated_into_two_batches() -> None:
    client = ScriptedBatchClient()
    coordinator = LiveStatusCoordinator(client, batch_size=29, clock=lambda: 100.0)
    for uid in range(1, 59):
        coordinator.register(uid, uid + 1000, AsyncMock(), AsyncMock())
    coordinator.register(1, 2001, AsyncMock(), AsyncMock())

    await coordinator.poll_once()

    assert [len(call) for call in client.calls] == [29, 29]
    assert sorted(uid for call in client.calls for uid in call) == list(range(1, 59))


@pytest.mark.asyncio
async def test_batches_are_released_sequentially() -> None:
    client = BlockingBatchClient()
    coordinator = LiveStatusCoordinator(client, batch_size=29, clock=lambda: 100.0)
    for uid in range(1, 59):
        coordinator.register(uid, uid + 1000, AsyncMock(), AsyncMock())

    polling = asyncio.create_task(coordinator.poll_once())
    await client.entered.wait()

    assert client.calls == [list(range(1, 30))]
    assert client.max_active == 1

    client.release.set()
    await polling

    assert client.calls == [list(range(1, 30)), list(range(30, 59))]
    assert client.max_active == 1


@pytest.mark.asyncio
async def test_missing_result_does_not_emit_offline() -> None:
    listener = AsyncMock()
    confirmer = AsyncMock(return_value=confirmation(1, ObservedStatus.LIVE))
    client = ScriptedBatchClient(
        results=[batch_result(1, ObservedStatus.LIVE), batch_result(1, None)]
    )
    coordinator = LiveStatusCoordinator(client, clock=lambda: 100.0)
    coordinator.register(1, 1001, listener, confirmer)

    await coordinator.poll_once()
    await coordinator.poll_once()

    assert [call.args[0].status for call in listener.await_args_list] == [
        ObservedStatus.LIVE
    ]


@pytest.mark.asyncio
async def test_contradictory_missing_snapshot_does_not_emit_offline() -> None:
    listener = AsyncMock()
    confirmer = AsyncMock(return_value=confirmation(1, ObservedStatus.LIVE))
    contradictory = BatchStatusResult(
        {
            1: snapshot(1, ObservedStatus.PREPARING),
            2: snapshot(2, ObservedStatus.PREPARING),
        },
        frozenset({1}),
    )
    client = ScriptedBatchClient(
        results=[
            BatchStatusResult(
                {
                    1: snapshot(1, ObservedStatus.LIVE),
                    2: snapshot(2, ObservedStatus.PREPARING),
                },
                frozenset(),
            ),
            contradictory,
            contradictory,
        ]
    )
    coordinator = LiveStatusCoordinator(client, clock=lambda: 100.0)
    coordinator.register(1, 1001, listener, confirmer)
    coordinator.register(2, 1002, AsyncMock(), AsyncMock())

    await coordinator.poll_once()
    await coordinator.poll_once()
    await coordinator.poll_once()

    assert [call.args[0].status for call in listener.await_args_list] == [
        ObservedStatus.LIVE
    ]


@pytest.mark.asyncio
async def test_unknown_and_stale_results_do_not_emit_offline() -> None:
    listener = AsyncMock()
    confirmer = AsyncMock(return_value=confirmation(1, ObservedStatus.LIVE))
    client = ScriptedBatchClient(
        results=[
            batch_result(1, ObservedStatus.LIVE),
            batch_result(1, ObservedStatus.UNKNOWN),
            batch_result(1, ObservedStatus.STALE),
        ]
    )
    coordinator = LiveStatusCoordinator(client, clock=lambda: 100.0)
    coordinator.register(1, 1001, listener, confirmer)

    await coordinator.poll_once()
    await coordinator.poll_once()
    await coordinator.poll_once()

    assert [call.args[0].status for call in listener.await_args_list] == [
        ObservedStatus.LIVE
    ]


@pytest.mark.asyncio
async def test_first_live_requires_anonymous_confirmation() -> None:
    listener = AsyncMock()
    confirmer = AsyncMock(return_value=confirmation(1, ObservedStatus.PREPARING))
    client = ScriptedBatchClient(results=[batch_result(1, ObservedStatus.LIVE)])
    coordinator = LiveStatusCoordinator(client, clock=lambda: 100.0)
    registration = coordinator.register(1, 1001, listener, confirmer)

    await coordinator.poll_once()

    assert confirmer.await_count == 1
    assert listener.await_count == 0
    assert registration.current is ObservedStatus.UNKNOWN


@pytest.mark.asyncio
async def test_repeated_broadcast_is_suppressed() -> None:
    listener = AsyncMock()
    confirmer = AsyncMock(return_value=confirmation(1, ObservedStatus.LIVE))
    client = ScriptedBatchClient(
        results=[
            batch_result(1, ObservedStatus.LIVE),
            batch_result(1, ObservedStatus.LIVE),
        ]
    )
    coordinator = LiveStatusCoordinator(client, clock=lambda: 100.0)
    coordinator.register(1, 1001, listener, confirmer)

    await coordinator.poll_once()
    await coordinator.poll_once()

    assert listener.await_count == 1
    assert confirmer.await_count == 1


@pytest.mark.asyncio
async def test_offline_requires_two_batch_observations() -> None:
    listener = AsyncMock()
    confirmer = AsyncMock(return_value=confirmation(1, ObservedStatus.LIVE))
    client = ScriptedBatchClient(
        results=[
            batch_result(1, ObservedStatus.LIVE),
            batch_result(1, ObservedStatus.PREPARING),
            batch_result(1, ObservedStatus.PREPARING),
        ]
    )
    coordinator = LiveStatusCoordinator(client, clock=lambda: 100.0)
    coordinator.register(1, 1001, listener, confirmer)

    await coordinator.poll_once()
    await coordinator.poll_once()
    await coordinator.poll_once()

    assert [call.args[0].status for call in listener.await_args_list] == [
        ObservedStatus.LIVE,
        ObservedStatus.PREPARING,
    ]


@pytest.mark.asyncio
async def test_wss_negative_uses_one_shared_http_confirmation() -> None:
    entered = asyncio.Event()
    release = asyncio.Event()

    async def confirm_once() -> StatusSnapshot:
        entered.set()
        await release.wait()
        return confirmation(1, ObservedStatus.PREPARING)

    listener = AsyncMock()
    confirmer = AsyncMock(side_effect=confirm_once)
    coordinator = LiveStatusCoordinator(ScriptedBatchClient(), clock=lambda: 100.0)
    registration = coordinator.register(1, 1001, listener, confirmer)
    registration.current = ObservedStatus.LIVE
    registration.observation_key = '1:1'

    first = asyncio.create_task(coordinator.observe_wss(1001, ObservedStatus.PREPARING))
    await entered.wait()
    second = asyncio.create_task(coordinator.observe_wss(1001, ObservedStatus.ROUND))
    await asyncio.sleep(0)
    release.set()
    await asyncio.gather(first, second)

    assert confirmer.await_count == 1
    assert coordinator.fallback_count == 1
    assert listener.await_count == 1
    assert registration.current is ObservedStatus.PREPARING


@pytest.mark.asyncio
async def test_live_wss_confirmation_clears_negative_hint_without_emitting() -> None:
    listener = AsyncMock()
    confirmer = AsyncMock(return_value=confirmation(1, ObservedStatus.LIVE))
    coordinator = LiveStatusCoordinator(ScriptedBatchClient(), clock=lambda: 100.0)
    registration = coordinator.register(1, 1001, listener, confirmer)
    registration.current = ObservedStatus.LIVE
    registration.observation_key = '1:1'

    await coordinator.observe_wss(1001, ObservedStatus.PREPARING)

    assert registration.current is ObservedStatus.LIVE
    assert registration.wss_negative is False
    assert listener.await_count == 0


@pytest.mark.asyncio
async def test_later_batch_negative_confirms_failed_wss_hint() -> None:
    listener = AsyncMock()
    confirmer = AsyncMock(return_value=confirmation(1, ObservedStatus.LIVE))
    client = ScriptedBatchClient(
        results=[
            batch_result(1, ObservedStatus.LIVE),
            batch_result(1, ObservedStatus.PREPARING),
        ]
    )
    coordinator = LiveStatusCoordinator(client, clock=lambda: 100.0)
    coordinator.register(1, 1001, listener, confirmer)

    await coordinator.poll_once()
    await coordinator.observe_wss(1001, ObservedStatus.PREPARING)
    await coordinator.poll_once()

    assert confirmer.await_count == 1
    assert [call.args[0].status for call in listener.await_args_list] == [
        ObservedStatus.LIVE,
        ObservedStatus.PREPARING,
    ]


@pytest.mark.asyncio
async def test_confirmation_cooldown_limits_physical_requests() -> None:
    clock = MutableClock()
    listener = AsyncMock()
    confirmer = AsyncMock(
        side_effect=[
            confirmation(1, ObservedStatus.LIVE, live_time=1),
            confirmation(1, ObservedStatus.LIVE, live_time=2),
        ]
    )
    client = ScriptedBatchClient(
        results=[
            batch_result(1, ObservedStatus.LIVE, live_time=1),
            batch_result(1, ObservedStatus.LIVE, live_time=2),
            batch_result(1, ObservedStatus.LIVE, live_time=2),
        ]
    )
    coordinator = LiveStatusCoordinator(client, clock=clock)
    coordinator.register(1, 1001, listener, confirmer)

    await coordinator.poll_once()
    await coordinator.poll_once()

    assert coordinator.fallback_count == 1
    assert confirmer.await_count == 1
    assert listener.await_count == 1

    clock.advance(600)
    await coordinator.poll_once()

    assert coordinator.fallback_count == 2
    assert confirmer.await_count == 2
    assert listener.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'error',
    [
        BatchProtocolError('HTTP 429'),
        BatchApiError(-352),
        BatchApiError(-412),
        asyncio.TimeoutError(),
        BatchProtocolError('unexpected response envelope'),
        OSError('transport failed'),
    ],
)
async def test_batch_failures_open_breaker_without_stopping_live(
    error: BaseException,
) -> None:
    client = ScriptedBatchClient(results=[error])
    coordinator = LiveStatusCoordinator(client, clock=lambda: 100.0)
    registration = coordinator.register(1, 1001, AsyncMock(), AsyncMock())
    registration.current = ObservedStatus.LIVE

    await coordinator.poll_once()

    assert coordinator.metrics(100.0).breaker_state is BreakerState.OPEN
    assert registration.current is ObservedStatus.LIVE


@pytest.mark.asyncio
async def test_large_missing_result_opens_breaker_after_planned_batches() -> None:
    first = list(range(1, 30))
    second = list(range(30, 59))
    client = ScriptedBatchClient(
        results=[
            batch_for(first, ObservedStatus.PREPARING, missing=list(range(15, 30))),
            batch_for(second, ObservedStatus.PREPARING),
        ]
    )
    coordinator = LiveStatusCoordinator(client, batch_size=29, clock=lambda: 100.0)
    for uid in range(1, 59):
        coordinator.register(uid, uid + 1000, AsyncMock(), AsyncMock())

    await coordinator.poll_once()

    assert client.calls == [first, second]
    assert coordinator.metrics(100.0).breaker_state is BreakerState.OPEN
    assert coordinator.metrics(100.0).missing_results == 15


def test_breaker_uses_exponential_canary_delay() -> None:
    clock = MutableClock(0.0)
    breaker = StatusCircuitBreaker(clock=clock)

    breaker.record_failure('HTTP 429')
    clock.advance(29)
    assert breaker.allow_canary(clock()) is False
    clock.advance(1)
    assert breaker.allow_canary(clock()) is True

    breaker.record_failure('timeout')
    clock.advance(59)
    assert breaker.allow_canary(clock()) is False
    clock.advance(1)
    assert breaker.allow_canary(clock()) is True


@pytest.mark.asyncio
async def test_breaker_recovers_one_then_five_then_full() -> None:
    clock = MutableClock(0.0)
    client = ScriptedBatchClient(
        results=[
            asyncio.TimeoutError(),
            batch_for([1], ObservedStatus.PREPARING),
            batch_for(list(range(1, 6)), ObservedStatus.PREPARING),
            batch_for(list(range(1, 11)), ObservedStatus.PREPARING),
        ]
    )
    coordinator = LiveStatusCoordinator(client, batch_size=10, clock=clock)
    for uid in range(1, 11):
        coordinator.register(uid, uid + 1000, AsyncMock(), AsyncMock())

    await coordinator.poll_once()
    await coordinator.poll_once()
    assert client.calls == [list(range(1, 11))]

    clock.advance(30)
    await coordinator.poll_once()
    assert client.calls[-1] == [1]
    assert coordinator.metrics(clock()).breaker_state is BreakerState.HALF_OPEN

    await coordinator.poll_once()
    assert client.calls[-1] == list(range(1, 6))
    assert coordinator.metrics(clock()).breaker_state is BreakerState.CLOSED

    await coordinator.poll_once()
    assert client.calls[-1] == list(range(1, 11))


@pytest.mark.asyncio
async def test_five_failed_canaries_pause_until_resume() -> None:
    clock = MutableClock(0.0)
    failures: List[ScriptedOutcome] = [
        BatchProtocolError('unexpected response envelope') for _ in range(6)
    ]
    client = ScriptedBatchClient(
        results=failures + [batch_for([1], ObservedStatus.PREPARING)]
    )
    coordinator = LiveStatusCoordinator(client, clock=clock)
    coordinator.register(1, 1001, AsyncMock(), AsyncMock())

    await coordinator.poll_once()
    for delay in (30, 60, 120, 240, 480):
        clock.advance(delay)
        await coordinator.poll_once()

    assert coordinator.metrics(clock()).breaker_state is BreakerState.PAUSED
    paused_calls = len(client.calls)
    clock.advance(10_000)
    await coordinator.poll_once()
    assert len(client.calls) == paused_calls

    coordinator.resume()
    await coordinator.poll_once()

    assert client.calls[-1] == [1]
    assert coordinator.metrics(clock()).breaker_state is BreakerState.HALF_OPEN


@pytest.mark.asyncio
async def test_start_runs_lowest_uid_canary_before_full_poll() -> None:
    client = ScriptedBatchClient(
        results=[
            batch_for([1], ObservedStatus.PREPARING),
            batch_for([1, 2, 3], ObservedStatus.PREPARING),
        ]
    )
    coordinator = LiveStatusCoordinator(client, clock=lambda: 100.0)
    for uid in (3, 1, 2):
        coordinator.register(uid, uid + 1000, AsyncMock(), AsyncMock())

    await coordinator.start()
    await coordinator.start()
    await wait_for_calls(client, 2)
    await coordinator.stop()
    await coordinator.stop()

    assert client.calls == [[1], [1, 2, 3]]


@pytest.mark.asyncio
async def test_rooms_registered_after_start_are_canaried_before_full_poll(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = ScriptedBatchClient(
        results=[
            batch_for([1], ObservedStatus.PREPARING),
            batch_for([1, 2], ObservedStatus.PREPARING),
        ]
    )
    coordinator = LiveStatusCoordinator(client, clock=lambda: 100.0)
    waiting_for_next_cycle = asyncio.Event()
    keep_waiting = asyncio.Event()
    sleep_count = 0

    async def controlled_sleep(delay: float) -> None:
        nonlocal sleep_count
        sleep_count += 1
        if sleep_count == 1:
            coordinator.register(2, 1002, AsyncMock(), AsyncMock())
            coordinator.register(1, 1001, AsyncMock(), AsyncMock())
            return
        waiting_for_next_cycle.set()
        await keep_waiting.wait()

    monkeypatch.setattr(asyncio, 'sleep', controlled_sleep)

    await coordinator.start()
    await waiting_for_next_cycle.wait()
    await coordinator.stop()

    assert client.calls == [[1], [1, 2]]


@pytest.mark.asyncio
async def test_polling_cycles_do_not_overlap() -> None:
    client = BlockingBatchClient()
    coordinator = LiveStatusCoordinator(client, clock=lambda: 100.0)
    coordinator.register(1, 1001, AsyncMock(), AsyncMock())

    first = asyncio.create_task(coordinator.poll_once())
    await client.entered.wait()
    second = asyncio.create_task(coordinator.poll_once())
    await asyncio.sleep(0)

    assert client.max_active == 1
    assert len(client.calls) == 1

    client.release.set()
    await asyncio.gather(first, second)

    assert client.max_active == 1


@pytest.mark.asyncio
async def test_listener_failure_rolls_back_and_does_not_block_other_rooms() -> None:
    clock = MutableClock()
    first_result = batch_for([1, 2], ObservedStatus.LIVE)
    client = ScriptedBatchClient(results=[first_result, first_result])
    first_listener = AsyncMock(side_effect=RuntimeError('listener failed'))
    second_listener = AsyncMock()
    first_confirmer = AsyncMock(return_value=confirmation(1, ObservedStatus.LIVE))
    second_confirmer = AsyncMock(return_value=confirmation(2, ObservedStatus.LIVE))
    coordinator = LiveStatusCoordinator(client, clock=clock)
    first_registration = coordinator.register(1, 1001, first_listener, first_confirmer)
    second_registration = coordinator.register(
        2, 1002, second_listener, second_confirmer
    )

    await coordinator.poll_once()

    assert first_registration.current is ObservedStatus.UNKNOWN
    assert second_registration.current is ObservedStatus.LIVE
    assert second_listener.await_count == 1

    first_listener.side_effect = None
    clock.advance(600)
    await coordinator.poll_once()

    assert first_registration.current is ObservedStatus.LIVE
    assert first_listener.await_count == 2
    assert second_listener.await_count == 1


@pytest.mark.asyncio
async def test_unregister_removes_room_from_later_polls() -> None:
    client = ScriptedBatchClient()
    coordinator = LiveStatusCoordinator(client, clock=lambda: 100.0)
    coordinator.register(1, 1001, AsyncMock(), AsyncMock())
    coordinator.unregister(1001)

    await coordinator.poll_once()

    assert client.calls == []
