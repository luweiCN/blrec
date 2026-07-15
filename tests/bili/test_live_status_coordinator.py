import asyncio
from collections import deque
from typing import Deque, List, Optional, Sequence, Union
from unittest.mock import AsyncMock, Mock, call

import pytest

from blrec.bili import live_status_coordinator as coordinator_module
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
async def test_repeated_small_missing_live_result_uses_cooled_safe_confirmation() -> (
    None
):
    clock = MutableClock()
    listener = AsyncMock()
    confirmer = AsyncMock(
        side_effect=[
            confirmation(1, ObservedStatus.PREPARING),
            confirmation(1, ObservedStatus.PREPARING),
        ]
    )
    missing_live = BatchStatusResult(
        {2: snapshot(2, ObservedStatus.PREPARING)}, frozenset({1})
    )
    client = ScriptedBatchClient(results=[missing_live, missing_live, missing_live])
    coordinator = LiveStatusCoordinator(client, clock=clock)
    registration = coordinator.register(1, 1001, listener, confirmer)
    coordinator.register(2, 1002, AsyncMock(), AsyncMock())
    registration.current = ObservedStatus.LIVE
    registration.observation_key = '1:1'

    await coordinator.poll_once()
    await coordinator.poll_once()

    assert registration.current is ObservedStatus.LIVE
    assert registration.negative_count == 1
    assert confirmer.await_count == 1
    assert listener.await_count == 0

    clock.advance(600)
    await coordinator.poll_once()

    assert registration.current is ObservedStatus.PREPARING
    assert confirmer.await_count == 2
    assert [item.args[0].status for item in listener.await_args_list] == [
        ObservedStatus.PREPARING
    ]


@pytest.mark.asyncio
async def test_small_missing_non_live_result_does_not_fan_out_confirmation() -> None:
    confirmer = AsyncMock()
    client = ScriptedBatchClient(
        results=[
            BatchStatusResult(
                {2: snapshot(2, ObservedStatus.PREPARING)}, frozenset({1})
            )
        ]
    )
    coordinator = LiveStatusCoordinator(client, clock=lambda: 100.0)
    coordinator.register(1, 1001, AsyncMock(), confirmer)
    coordinator.register(2, 1002, AsyncMock(), AsyncMock())

    await coordinator.poll_once()

    confirmer.assert_not_awaited()
    assert coordinator.fallback_count == 0


@pytest.mark.asyncio
async def test_one_cycle_confirms_at_most_one_missing_live_room() -> None:
    uids = list(range(1, 7))
    result = batch_for(uids, ObservedStatus.LIVE, missing=[1, 2], live_time=1)
    client = ScriptedBatchClient(results=[result])
    coordinator = LiveStatusCoordinator(client, clock=lambda: 100.0)
    confirmers: List[AsyncMock] = []
    for uid in uids:
        confirmer = AsyncMock(return_value=confirmation(uid, ObservedStatus.LIVE))
        confirmers.append(confirmer)
        registration = coordinator.register(uid, uid + 1000, AsyncMock(), confirmer)
        registration.current = ObservedStatus.LIVE
        registration.observation_key = '{}:1'.format(uid)

    await coordinator.poll_once()

    assert sum(item.await_count for item in confirmers) == 1
    assert coordinator.fallback_count == 1


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
async def test_confirmation_failure_is_reported_and_isolated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    failure = RuntimeError('confirmation failed')
    reported = Mock()
    monkeypatch.setattr(coordinator_module, 'submit_exception', reported, raising=False)
    failed_listener = AsyncMock()
    healthy_listener = AsyncMock()
    client = ScriptedBatchClient(results=[batch_for([1, 2], ObservedStatus.LIVE)])
    coordinator = LiveStatusCoordinator(client, clock=lambda: 100.0)
    failed_registration = coordinator.register(
        1, 1001, failed_listener, AsyncMock(side_effect=failure)
    )
    healthy_registration = coordinator.register(
        2,
        1002,
        healthy_listener,
        AsyncMock(return_value=confirmation(2, ObservedStatus.LIVE)),
    )

    await coordinator.poll_once()

    reported.assert_called_once_with(failure)
    assert failed_registration.current is ObservedStatus.UNKNOWN
    assert failed_listener.await_count == 0
    assert healthy_registration.current is ObservedStatus.LIVE
    assert healthy_listener.await_count == 1
    assert coordinator.metrics(100.0).breaker_state is BreakerState.CLOSED


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
async def test_cancelled_waiter_does_not_cancel_shared_confirmation() -> None:
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def confirm_once() -> StatusSnapshot:
        nonlocal calls
        calls += 1
        entered.set()
        await release.wait()
        return confirmation(1, ObservedStatus.PREPARING)

    coordinator = LiveStatusCoordinator(ScriptedBatchClient(), clock=lambda: 100.0)
    registration = coordinator.register(1, 1001, AsyncMock(), confirm_once)
    first = asyncio.create_task(coordinator._confirm(registration))
    await entered.wait()
    second = asyncio.create_task(coordinator._confirm(registration))
    await asyncio.sleep(0)

    try:
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        release.set()
        result = await asyncio.gather(second, return_exceptions=True)

        assert result == [confirmation(1, ObservedStatus.PREPARING)]
        assert calls == 1
        assert coordinator.fallback_count == 1
    finally:
        release.set()
        await coordinator.stop()
        await asyncio.gather(first, second, return_exceptions=True)


@pytest.mark.asyncio
async def test_cancelled_cached_confirmation_is_rebuilt() -> None:
    entered = asyncio.Event()
    never_release = asyncio.Event()
    calls = 0

    async def confirm_with_cancelled_first_call() -> StatusSnapshot:
        nonlocal calls
        calls += 1
        if calls == 1:
            entered.set()
            await never_release.wait()
        return confirmation(1, ObservedStatus.ROUND)

    coordinator = LiveStatusCoordinator(ScriptedBatchClient(), clock=lambda: 100.0)
    registration = coordinator.register(
        1, 1001, AsyncMock(), confirm_with_cancelled_first_call
    )
    first = asyncio.create_task(coordinator._confirm(registration))
    await entered.wait()
    coordinator._fallback_tasks[1001].task.cancel()

    try:
        with pytest.raises(asyncio.CancelledError):
            await first
        retry = await asyncio.gather(
            coordinator._confirm(registration), return_exceptions=True
        )

        assert retry == [confirmation(1, ObservedStatus.ROUND)]
        assert calls == 2
        assert coordinator.fallback_count == 2
    finally:
        never_release.set()
        await coordinator.stop()


@pytest.mark.asyncio
async def test_stop_cancels_unregistered_confirmation_task() -> None:
    entered = asyncio.Event()
    cancelled = asyncio.Event()
    never_release = asyncio.Event()

    async def confirm_until_cancelled() -> StatusSnapshot:
        entered.set()
        try:
            await never_release.wait()
        finally:
            cancelled.set()
        return confirmation(1, ObservedStatus.LIVE)

    coordinator = LiveStatusCoordinator(ScriptedBatchClient(), clock=lambda: 100.0)
    registration = coordinator.register(1, 1001, AsyncMock(), confirm_until_cancelled)
    waiter = asyncio.create_task(coordinator._confirm(registration))
    await entered.wait()
    entry = coordinator._fallback_tasks[1001]
    coordinator.unregister(1001)
    assert 1001 not in coordinator._fallback_tasks

    try:
        await coordinator.stop()
        await asyncio.sleep(0)

        assert cancelled.is_set()
        assert entry.task.cancelled()
        assert waiter.done()
        result = await asyncio.gather(waiter, return_exceptions=True)
        assert isinstance(result[0], asyncio.CancelledError)
        assert coordinator._fallback_tasks == {}
    finally:
        entry.task.cancel()
        waiter.cancel()
        await asyncio.gather(entry.task, waiter, return_exceptions=True)


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
async def test_exhausted_wss_is_not_active_and_same_broadcast_retries_once() -> None:
    client = ScriptedBatchClient(
        results=[
            batch_result(1, ObservedStatus.LIVE),
            batch_result(1, ObservedStatus.LIVE),
        ]
    )
    coordinator = LiveStatusCoordinator(client, clock=lambda: 100.0)

    listener = AsyncMock()
    registration = coordinator.register(1, 1001, listener, AsyncMock())
    registration.current = ObservedStatus.LIVE
    registration.observation_key = '1:1'
    await coordinator.observe_wss(1001, ObservedStatus.LIVE)
    listener.reset_mock()

    await coordinator.observe_wss(1001, ObservedStatus.STALE)

    assert coordinator.metrics(100.0).active_websockets == 0
    assert registration.current is ObservedStatus.LIVE

    await coordinator.poll_once()
    await coordinator.poll_once()

    assert listener.await_count == 1
    assert listener.await_args.args[0].status is ObservedStatus.LIVE
    assert registration.current is ObservedStatus.LIVE
    assert coordinator.metrics(100.0).active_websockets == 1


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
async def test_unregister_during_confirmation_does_not_notify() -> None:
    entered = asyncio.Event()
    release = asyncio.Event()

    async def confirm_after_release() -> StatusSnapshot:
        entered.set()
        await release.wait()
        return confirmation(1, ObservedStatus.LIVE)

    listener = AsyncMock()
    confirmer = AsyncMock(side_effect=confirm_after_release)
    client = ScriptedBatchClient(results=[batch_result(1, ObservedStatus.LIVE)])
    coordinator = LiveStatusCoordinator(client, clock=lambda: 100.0)
    registration = coordinator.register(1, 1001, listener, confirmer)

    polling = asyncio.create_task(coordinator.poll_once())
    await entered.wait()
    fallback_entry = coordinator._fallback_tasks[1001]
    coordinator.unregister(1001)
    assert 1001 not in coordinator._fallback_tasks
    assert fallback_entry.task.cancelled() is False
    release.set()
    await polling

    assert fallback_entry.task.cancelled() is False
    assert listener.await_count == 0
    assert registration.current is ObservedStatus.UNKNOWN


@pytest.mark.asyncio
async def test_reregistered_room_does_not_reuse_previous_confirmation() -> None:
    old_entered = asyncio.Event()
    old_release = asyncio.Event()

    async def old_confirmation() -> StatusSnapshot:
        old_entered.set()
        await old_release.wait()
        return confirmation(1, ObservedStatus.PREPARING)

    old_listener = AsyncMock()
    old_confirmer = AsyncMock(side_effect=old_confirmation)
    coordinator = LiveStatusCoordinator(ScriptedBatchClient(), clock=lambda: 100.0)
    old_registration = coordinator.register(1, 1001, old_listener, old_confirmer)
    old_registration.current = ObservedStatus.LIVE

    old_observation = asyncio.create_task(
        coordinator.observe_wss(1001, ObservedStatus.PREPARING)
    )
    await old_entered.wait()

    new_listener = AsyncMock()
    new_confirmer = AsyncMock(return_value=confirmation(1, ObservedStatus.ROUND))
    new_registration = coordinator.register(1, 1001, new_listener, new_confirmer)
    new_registration.current = ObservedStatus.LIVE
    new_observation = asyncio.create_task(
        coordinator.observe_wss(1001, ObservedStatus.ROUND)
    )

    old_release.set()
    await asyncio.gather(old_observation, new_observation)

    assert old_confirmer.await_count == 1
    assert new_confirmer.await_count == 1
    assert coordinator.fallback_count == 2
    assert old_listener.await_count == 0
    assert [call.args[0].status for call in new_listener.await_args_list] == [
        ObservedStatus.ROUND
    ]


@pytest.mark.asyncio
async def test_blocked_listener_does_not_block_other_rooms_or_polling() -> None:
    first_listener_entered = asyncio.Event()
    first_listener_cancelled = asyncio.Event()
    never_release_first_listener = asyncio.Event()

    async def block_first_listener(snapshot: StatusSnapshot) -> None:
        first_listener_entered.set()
        try:
            await never_release_first_listener.wait()
        finally:
            first_listener_cancelled.set()

    first_listener = AsyncMock(side_effect=block_first_listener)
    second_listener = AsyncMock()
    client = ScriptedBatchClient(
        results=[
            BatchStatusResult(
                {
                    1: snapshot(1, ObservedStatus.LIVE),
                    2: snapshot(2, ObservedStatus.LIVE),
                },
                frozenset(),
            )
        ]
    )
    coordinator = LiveStatusCoordinator(client, clock=lambda: 100.0)
    coordinator.register(
        1,
        1001,
        first_listener,
        AsyncMock(return_value=confirmation(1, ObservedStatus.LIVE)),
    )
    coordinator.register(
        2,
        1002,
        second_listener,
        AsyncMock(return_value=confirmation(2, ObservedStatus.LIVE)),
    )

    await asyncio.wait_for(coordinator.poll_once(), timeout=0.1)
    await first_listener_entered.wait()
    await asyncio.wait_for(coordinator.stop(), timeout=0.1)

    assert second_listener.await_count == 1
    assert first_listener_cancelled.is_set()


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
    first_listener = AsyncMock()
    second_listener = AsyncMock()
    client = ScriptedBatchClient(
        results=[
            batch_for(first, ObservedStatus.PREPARING, missing=list(range(15, 30))),
            batch_for(second, ObservedStatus.PREPARING),
        ]
    )
    coordinator = LiveStatusCoordinator(client, batch_size=29, clock=lambda: 100.0)
    for uid in range(1, 59):
        listener = (
            first_listener
            if uid == 1
            else second_listener if uid == 30 else AsyncMock()
        )
        registration = coordinator.register(uid, uid + 1000, listener, AsyncMock())
        if uid in (1, 30):
            registration.current = ObservedStatus.LIVE
            registration.negative_count = 1

    await coordinator.poll_once()

    assert client.calls == [first, second]
    assert coordinator.metrics(100.0).breaker_state is BreakerState.OPEN
    assert coordinator.metrics(100.0).missing_results == 15
    assert coordinator._registrations[1001].current is ObservedStatus.LIVE
    assert coordinator._registrations[1001].negative_count == 1
    assert coordinator._registrations[1030].current is ObservedStatus.LIVE
    assert coordinator._registrations[1030].negative_count == 1
    assert first_listener.await_count == 0
    assert second_listener.await_count == 0


@pytest.mark.asyncio
async def test_later_batch_failure_discards_earlier_status_updates() -> None:
    first = list(range(1, 30))
    second = list(range(30, 59))
    listener = AsyncMock()
    client = ScriptedBatchClient(
        results=[
            batch_for(first, ObservedStatus.PREPARING),
            BatchProtocolError('HTTP 429'),
        ]
    )
    coordinator = LiveStatusCoordinator(client, batch_size=29, clock=lambda: 100.0)
    for uid in range(1, 59):
        registration = coordinator.register(
            uid, uid + 1000, listener if uid == 1 else AsyncMock(), AsyncMock()
        )
        if uid == 1:
            registration.current = ObservedStatus.LIVE
            registration.negative_count = 1

    await coordinator.poll_once()

    assert client.calls == [first, second]
    assert coordinator.metrics(100.0).breaker_state is BreakerState.OPEN
    assert coordinator._registrations[1001].current is ObservedStatus.LIVE
    assert coordinator._registrations[1001].negative_count == 1
    assert listener.await_count == 0


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
    assert coordinator.metrics(clock()).breaker_state is BreakerState.HALF_OPEN

    await coordinator.poll_once()
    assert client.calls[-1] == list(range(1, 11))
    assert coordinator.metrics(clock()).breaker_state is BreakerState.CLOSED


@pytest.mark.asyncio
async def test_full_recovery_probe_failure_reopens_breaker() -> None:
    clock = MutableClock(0.0)
    client = ScriptedBatchClient(
        results=[
            asyncio.TimeoutError(),
            batch_for([1], ObservedStatus.PREPARING),
            batch_for(list(range(1, 6)), ObservedStatus.PREPARING),
            BatchProtocolError('HTTP 429'),
        ]
    )
    coordinator = LiveStatusCoordinator(client, batch_size=10, clock=clock)
    for uid in range(1, 11):
        coordinator.register(uid, uid + 1000, AsyncMock(), AsyncMock())

    await coordinator.poll_once()
    clock.advance(30)
    await coordinator.poll_once()
    await coordinator.poll_once()
    await coordinator.poll_once()

    assert [len(call) for call in client.calls] == [10, 1, 5, 10]
    assert coordinator.metrics(clock()).breaker_state is BreakerState.OPEN


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
async def test_start_attaches_exception_callback_to_polling_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    failure = RuntimeError('polling loop failed')
    reported: List[object] = []
    real_sleep = asyncio.sleep

    async def fail_sleep(delay: float) -> None:
        raise failure

    def capture(future: object) -> None:
        reported.append(future)

    monkeypatch.setattr(
        coordinator_module, 'exception_callback', capture, raising=False
    )
    monkeypatch.setattr(asyncio, 'sleep', fail_sleep)
    coordinator = LiveStatusCoordinator(ScriptedBatchClient())

    await coordinator.start()
    task = coordinator._polling_task
    assert task is not None
    for _ in range(10):
        if reported:
            break
        await real_sleep(0)
    assert task.done()
    assert task.exception() is failure

    assert reported == [task]


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
async def test_listener_failure_rolls_back_and_does_not_block_other_rooms(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = MutableClock()
    first_result = batch_for([1, 2], ObservedStatus.LIVE)
    client = ScriptedBatchClient(results=[first_result, first_result])
    failure = RuntimeError('listener failed')
    reported = Mock()
    monkeypatch.setattr(coordinator_module, 'submit_exception', reported, raising=False)
    first_listener = AsyncMock(side_effect=failure)
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
    reported.assert_called_once_with(failure)

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


@pytest.mark.asyncio
async def test_room_aware_confirmer_receives_real_room_id() -> None:
    client = ScriptedBatchClient(results=[batch_result(1, ObservedStatus.LIVE)])
    confirmer = AsyncMock(return_value=confirmation(1, ObservedStatus.LIVE))
    coordinator = LiveStatusCoordinator(client, clock=lambda: 100.0)
    coordinator.register(1, 1001, AsyncMock(), confirmer, confirmer_uses_room_id=True)

    await coordinator.poll_once()

    confirmer.assert_awaited_once_with(1001)


@pytest.mark.asyncio
async def test_redirected_missing_uid_mappings_preserve_registration_owners() -> None:
    client = ScriptedBatchClient()
    mapping_loader = AsyncMock(return_value={123: (2002, 7), 456: (2002, 7)})
    coordinator = LiveStatusCoordinator(client, clock=lambda: 100.0)
    for requested_room_id in (123, 456):
        coordinator.register(
            0,
            requested_room_id,
            AsyncMock(),
            AsyncMock(),
            requested_room_id=requested_room_id,
            mapping_loader=mapping_loader,
        )

    await coordinator.poll_once()

    mapping_loader.assert_awaited_once_with((123, 456))
    assert client.calls == [[7]]
    assert coordinator.metrics(100.0).registered_rooms == 2


@pytest.mark.asyncio
async def test_alias_mappings_fan_out_one_batch_and_one_confirmation() -> None:
    live_snapshot = StatusSnapshot(
        uid=7,
        room_id=2002,
        status=ObservedStatus.LIVE,
        observed_at=100.0,
        source=StatusSource.BATCH,
        live_time=10,
        observation_key='7:10',
    )
    confirmation_snapshot = StatusSnapshot(
        uid=7,
        room_id=2002,
        status=ObservedStatus.LIVE,
        observed_at=100.0,
        source=StatusSource.CONFIRMATION,
        live_time=10,
        observation_key='7:10',
    )
    client = ScriptedBatchClient(
        results=[BatchStatusResult({7: live_snapshot}, frozenset())]
    )
    mapping_loader = AsyncMock(return_value={123: (2002, 7), 456: (2002, 7)})
    first_listener = AsyncMock()
    second_listener = AsyncMock()
    first_confirmer = AsyncMock(return_value=confirmation_snapshot)
    second_confirmer = AsyncMock(return_value=confirmation_snapshot)
    coordinator = LiveStatusCoordinator(client, clock=lambda: 100.0)
    coordinator.register(
        0,
        123,
        first_listener,
        first_confirmer,
        requested_room_id=123,
        mapping_loader=mapping_loader,
        confirmer_uses_room_id=True,
    )
    coordinator.register(
        0,
        456,
        second_listener,
        second_confirmer,
        requested_room_id=456,
        mapping_loader=mapping_loader,
        confirmer_uses_room_id=True,
    )

    await coordinator.poll_once()

    mapping_loader.assert_awaited_once_with((123, 456))
    assert client.calls == [[7]]
    assert first_confirmer.await_args_list + second_confirmer.await_args_list == [
        call(2002)
    ]
    assert coordinator.fallback_count == 1
    assert [item.args[0].status for item in first_listener.await_args_list] == [
        ObservedStatus.LIVE
    ]
    assert [item.args[0].status for item in second_listener.await_args_list] == [
        ObservedStatus.LIVE
    ]
    assert coordinator.metrics(100.0).registered_rooms == 2
    entry = coordinator._fallback_tasks[2002]
    assert set(entry.generations) == {123, 456}
    assert set(entry.consumed) == {123, 456}

    coordinator.unregister(123)
    assert coordinator.metrics(100.0).registered_rooms == 1
    assert coordinator._fallback_tasks[2002] is entry
    assert set(entry.generations) == {456}
    assert set(entry.consumed) == {456}
    coordinator.unregister(456)
    assert coordinator.metrics(100.0).registered_rooms == 0
    assert 2002 not in coordinator._fallback_tasks


@pytest.mark.asyncio
async def test_reregister_releases_completed_fallback_generation() -> None:
    client = ScriptedBatchClient(results=[batch_result(1, ObservedStatus.LIVE)])
    coordinator = LiveStatusCoordinator(client, clock=lambda: 100.0)
    old_registration = coordinator.register(
        1,
        1001,
        AsyncMock(),
        AsyncMock(return_value=confirmation(1, ObservedStatus.LIVE)),
    )
    await coordinator.poll_once()
    old_entry = coordinator._fallback_tasks[1001]
    assert old_entry.generations[1001] is old_registration
    assert old_entry.consumed[1001] is old_registration

    new_registration = coordinator.register(1, 1001, AsyncMock(), AsyncMock())

    assert 1001 not in coordinator._fallback_tasks
    assert coordinator._registrations[1001] is new_registration


@pytest.mark.asyncio
async def test_unresolved_mapping_uses_fallback_cooldown() -> None:
    clock = MutableClock()
    client = ScriptedBatchClient()
    mapping_loader = AsyncMock(return_value={})
    coordinator = LiveStatusCoordinator(client, clock=clock)
    coordinator.register(
        0,
        123,
        AsyncMock(),
        AsyncMock(),
        requested_room_id=123,
        mapping_loader=mapping_loader,
    )

    await coordinator.poll_once()
    await coordinator.poll_once()
    clock.advance(599)
    await coordinator.poll_once()

    mapping_loader.assert_awaited_once_with((123,))
    assert client.calls == []

    clock.advance(1)
    await coordinator.poll_once()

    assert mapping_loader.await_count == 2
