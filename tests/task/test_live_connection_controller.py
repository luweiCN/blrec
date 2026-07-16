import asyncio
import importlib
import sys
import types
from types import SimpleNamespace
from typing import List
from unittest.mock import AsyncMock, Mock, call

import pytest

from blrec.bili.live_connection_controller import LiveConnectionController
from blrec.bili.live_status import (
    BatchStatusResult,
    ObservedStatus,
    StatusSnapshot,
    StatusSource,
)
from blrec.bili.models import LiveStatus, RoomInfo


def load_task_module(monkeypatch: pytest.MonkeyPatch) -> types.ModuleType:
    pkg_resources = types.ModuleType('pkg_resources')
    pkg_resources.resource_string = Mock(return_value=b'')
    monkeypatch.setitem(sys.modules, 'pkg_resources', pkg_resources)
    importlib.import_module('blrec.setting')
    return importlib.import_module('blrec.task.task')


class FakeLive:
    room_id = 1001
    room_info: object

    def replace_room_info(self, room_info: object) -> None:
        self.room_info = room_info


class FakeMonitor:
    def __init__(self) -> None:
        self.confirmed: List[ObservedStatus] = []
        self.enabled = False

    def enable(self) -> None:
        self.enabled = True

    def disable(self) -> None:
        self.enabled = False

    async def apply_confirmed_status(self, status: ObservedStatus) -> None:
        self.confirmed.append(status)


def mocked_danmaku() -> AsyncMock:
    danmaku = AsyncMock()
    danmaku.set_room_id = Mock()
    return danmaku


def live_snapshot() -> StatusSnapshot:
    return StatusSnapshot(
        1, 1001, ObservedStatus.LIVE, 100.0, StatusSource.CONFIRMATION, 10, '1:10'
    )


def preparing_snapshot() -> StatusSnapshot:
    return StatusSnapshot(
        1, 1001, ObservedStatus.PREPARING, 130.0, StatusSource.BATCH, 10, '1:10'
    )


def room_info(
    uid: int = 1, room_id: int = 1001, live_status: LiveStatus = LiveStatus.LIVE
) -> RoomInfo:
    return RoomInfo(
        uid=uid,
        room_id=room_id,
        short_room_id=0,
        area_id=0,
        area_name='',
        parent_area_id=0,
        parent_area_name='',
        live_status=live_status,
        live_start_time=10,
        online=0,
        title='',
        cover='',
        tags='',
        description='',
    )


@pytest.mark.asyncio
async def test_offline_registration_does_not_start_websocket() -> None:
    danmaku = mocked_danmaku()
    monitor = FakeMonitor()
    controller = LiveConnectionController(
        FakeLive(), danmaku, monitor, AsyncMock(return_value=object())
    )

    assert controller.active is False
    danmaku.start.assert_not_awaited()


@pytest.mark.asyncio
async def test_confirmed_live_starts_once_and_offline_releases_wss() -> None:
    danmaku = mocked_danmaku()
    monitor = FakeMonitor()
    validated_room_info = room_info()
    room_info_loader = AsyncMock(return_value=validated_room_info)
    live = FakeLive()
    controller = LiveConnectionController(live, danmaku, monitor, room_info_loader)

    await controller.on_confirmed_status(live_snapshot())
    await controller.on_confirmed_status(live_snapshot())
    await controller.on_confirmed_status(preparing_snapshot())

    danmaku.start.assert_awaited_once()
    danmaku.stop.assert_awaited_once()
    room_info_loader.assert_awaited_once_with(1001)
    assert live.room_info.live_status is LiveStatus.PREPARING  # type: ignore
    assert monitor.confirmed == [ObservedStatus.LIVE, ObservedStatus.PREPARING]
    assert controller.active is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('status', 'expected'),
    [
        (ObservedStatus.PREPARING, LiveStatus.PREPARING),
        (ObservedStatus.ROUND, LiveStatus.ROUND),
    ],
)
async def test_confirmed_end_updates_room_info_before_monitor_event(
    status: ObservedStatus, expected: LiveStatus
) -> None:
    live = FakeLive()
    observed_room_statuses: List[LiveStatus] = []
    monitor = FakeMonitor()

    async def inspect_status(received: ObservedStatus) -> None:
        observed_room_statuses.append(live.room_info.live_status)  # type: ignore
        monitor.confirmed.append(received)

    monitor.apply_confirmed_status = inspect_status  # type: ignore
    controller = LiveConnectionController(
        live, mocked_danmaku(), monitor, AsyncMock(return_value=room_info())
    )
    await controller.on_confirmed_status(live_snapshot())

    await controller.on_confirmed_status(
        StatusSnapshot(1, 1001, status, 130.0, StatusSource.BATCH, 10, '1:10')
    )

    assert live.room_info.live_status is expected  # type: ignore
    assert observed_room_statuses[-1] is expected


@pytest.mark.asyncio
async def test_failed_confirmed_end_rolls_back_room_info() -> None:
    live = FakeLive()
    monitor = FakeMonitor()
    controller = LiveConnectionController(
        live, mocked_danmaku(), monitor, AsyncMock(return_value=room_info())
    )
    await controller.on_confirmed_status(live_snapshot())
    original_room_info = live.room_info
    observed_room_statuses: List[LiveStatus] = []

    async def fail_event(status: ObservedStatus) -> None:
        observed_room_statuses.append(live.room_info.live_status)  # type: ignore
        raise RuntimeError('event failed')

    monitor.apply_confirmed_status = fail_event  # type: ignore

    with pytest.raises(RuntimeError, match='event failed'):
        await controller.on_confirmed_status(preparing_snapshot())

    assert live.room_info is original_room_info
    assert observed_room_statuses == [LiveStatus.PREPARING]
    assert controller.active is True


@pytest.mark.asyncio
@pytest.mark.parametrize('status', [ObservedStatus.UNKNOWN, ObservedStatus.STALE])
async def test_unknown_status_keeps_active_connection(status: ObservedStatus) -> None:
    danmaku = mocked_danmaku()
    monitor = FakeMonitor()
    controller = LiveConnectionController(
        FakeLive(), danmaku, monitor, AsyncMock(return_value=room_info())
    )
    await controller.on_confirmed_status(live_snapshot())
    danmaku.reset_mock()

    await controller.on_confirmed_status(
        StatusSnapshot(1, 1001, status, 130.0, StatusSource.BATCH, 10, '1:10')
    )

    danmaku.stop.assert_not_awaited()
    assert monitor.confirmed == [ObservedStatus.LIVE]
    assert monitor.enabled is True
    assert controller.active is True


@pytest.mark.asyncio
async def test_close_releases_an_active_connection_once() -> None:
    danmaku = mocked_danmaku()
    monitor = FakeMonitor()
    controller = LiveConnectionController(
        FakeLive(), danmaku, monitor, AsyncMock(return_value=room_info())
    )
    await controller.on_confirmed_status(live_snapshot())

    await controller.close()
    await controller.close()

    danmaku.stop.assert_awaited_once()
    assert monitor.enabled is False
    assert controller.active is False


@pytest.mark.asyncio
async def test_wss_hint_is_forwarded_with_room_id() -> None:
    status_sink = AsyncMock()
    controller = LiveConnectionController(
        FakeLive(),
        AsyncMock(),
        FakeMonitor(),
        AsyncMock(return_value=object()),
        status_sink=status_sink,
    )

    await controller.on_wss_hint(ObservedStatus.PREPARING)

    status_sink.assert_awaited_once_with(1001, ObservedStatus.PREPARING)


@pytest.mark.asyncio
async def test_exhausted_wss_rebuilds_without_duplicate_live_events() -> None:
    from blrec.bili.live_monitor import LiveMonitor

    class EventDanmaku:
        def __init__(self) -> None:
            self.stopped = True
            self.start_count = 0
            self.stop_count = 0
            self.listeners: List[object] = []

        def add_listener(self, listener: object) -> None:
            if listener not in self.listeners:
                self.listeners.append(listener)

        def remove_listener(self, listener: object) -> None:
            self.listeners.remove(listener)

        def set_room_id(self, room_id: int) -> None:
            assert self.stopped is True

        async def start(self) -> None:
            self.stopped = False
            self.start_count += 1

        async def stop(self) -> None:
            self.stopped = True
            self.stop_count += 1

    live = FakeLive()
    live.room_info = room_info()
    live.get_live_streams = AsyncMock(return_value=[])  # type: ignore
    danmaku = EventDanmaku()
    monitor = LiveMonitor(danmaku, live, status_sink=AsyncMock())  # type: ignore
    listener = AsyncMock()
    monitor.add_listener(listener)
    status_sink = AsyncMock()
    controller = LiveConnectionController(
        live,
        danmaku,  # type: ignore
        monitor,
        AsyncMock(return_value=room_info()),
        status_sink=status_sink,
    )
    monitor._status_sink = controller.on_wss_hint

    await controller.on_confirmed_status(live_snapshot())
    await monitor.on_client_retries_exhausted(
        RuntimeError('websocket retries exhausted')
    )

    assert controller.active is False
    assert danmaku.stop_count == 0
    status_sink.assert_awaited_with(1001, ObservedStatus.STALE)

    await controller.on_confirmed_status(live_snapshot())

    assert controller.active is True
    assert danmaku.start_count == 2
    assert listener.on_live_began.await_count == 1
    assert listener.on_live_ended.await_count == 0
    assert listener.on_live_stream_reset.await_count == 0

    await monitor.on_client_retries_exhausted(
        RuntimeError('websocket retries exhausted again')
    )
    await controller.on_confirmed_status(live_snapshot())

    assert danmaku.start_count == 3
    assert listener.on_live_began.await_count == 1
    assert listener.on_live_ended.await_count == 0
    assert listener.on_live_stream_reset.await_count == 0

    await controller.on_confirmed_status(preparing_snapshot())
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_pending_retry_receives_confirmed_end_once() -> None:
    from blrec.bili.live_monitor import LiveMonitor

    danmaku = mocked_danmaku()
    danmaku.stopped = True
    danmaku.add_listener = Mock()
    danmaku.remove_listener = Mock()
    live = FakeLive()
    live.room_info = room_info()
    live.get_live_streams = AsyncMock(return_value=[])  # type: ignore
    monitor = LiveMonitor(danmaku, live, status_sink=AsyncMock())  # type: ignore
    listener = AsyncMock()
    monitor.add_listener(listener)
    controller = LiveConnectionController(
        live,
        danmaku,
        monitor,
        AsyncMock(return_value=room_info()),
        status_sink=AsyncMock(),
    )
    monitor._status_sink = controller.on_wss_hint

    await controller.on_confirmed_status(live_snapshot())
    await monitor.on_client_retries_exhausted(RuntimeError('exhausted'))
    await controller.on_confirmed_status(preparing_snapshot())
    await controller.on_confirmed_status(preparing_snapshot())

    assert listener.on_live_began.await_count == 1
    assert listener.on_live_ended.await_count == 1
    assert controller.active is False
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_failed_websocket_start_rolls_back_monitor() -> None:
    failure = RuntimeError('websocket failed')
    danmaku = mocked_danmaku()
    danmaku.start.side_effect = failure
    monitor = FakeMonitor()
    controller = LiveConnectionController(
        FakeLive(), danmaku, monitor, AsyncMock(return_value=room_info())
    )

    with pytest.raises(RuntimeError, match='websocket failed'):
        await controller.on_confirmed_status(live_snapshot())

    danmaku.stop.assert_awaited_once()
    assert monitor.enabled is False
    assert controller.active is False


@pytest.mark.asyncio
async def test_websocket_activation_timeout_is_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered_tasks: List[asyncio.Task[None]] = []
    never_connect = asyncio.Event()

    async def block_start() -> None:
        task = asyncio.current_task()
        assert task is not None
        entered_tasks.append(task)
        await never_connect.wait()

    danmaku = mocked_danmaku()
    danmaku.start.side_effect = block_start
    monitor = FakeMonitor()
    controller = LiveConnectionController(
        FakeLive(), danmaku, monitor, AsyncMock(return_value=room_info())
    )
    monkeypatch.setattr(
        LiveConnectionController, '_ACTIVATION_TIMEOUT_SECONDS', 0.01, raising=False
    )

    activation = asyncio.create_task(controller.on_confirmed_status(live_snapshot()))
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(activation, timeout=0.1)

    assert activation.cancelled() is False
    assert entered_tasks[0] is not activation
    danmaku.stop.assert_awaited_once()
    assert monitor.enabled is False
    assert controller.active is False


@pytest.mark.asyncio
async def test_partially_started_danmaku_client_can_stop() -> None:
    from blrec.bili.danmaku_client import DanmakuClient

    danmaku = object.__new__(DanmakuClient)
    danmaku._stopped = False
    danmaku._stopped_lock = asyncio.Lock()
    danmaku._logger = Mock()
    danmaku._listeners = []

    await danmaku.stop()

    assert danmaku.stopped is True


@pytest.mark.asyncio
async def test_danmaku_receive_retry_exhaustion_emits_terminal_signal() -> None:
    from blrec.bili.danmaku_client import DanmakuClient

    danmaku = object.__new__(DanmakuClient)
    danmaku._retry_count = 0
    danmaku._retry_delay = 0
    danmaku._MAX_RETRIES = 0
    danmaku._listeners = [AsyncMock()]

    with pytest.raises(Exception, match='maximum of retries'):
        await danmaku._retry()

    listener = danmaku._listeners[0]
    listener.on_client_retries_exhausted.assert_awaited_once()


@pytest.mark.asyncio
async def test_terminal_message_loop_error_does_not_block_disconnect() -> None:
    import aiohttp

    from blrec.bili.danmaku_client import DanmakuClient

    async def fail_message_loop() -> None:
        raise aiohttp.WebSocketError(1006, 'terminal receive error')

    message_loop = asyncio.create_task(fail_message_loop())
    await asyncio.sleep(0)
    assert isinstance(message_loop.exception(), aiohttp.WebSocketError)
    danmaku = object.__new__(DanmakuClient)
    danmaku._message_loop_task = message_loop
    danmaku._disconnect = AsyncMock()
    danmaku._logger = Mock()

    await danmaku._do_stop()

    danmaku._disconnect.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_failed_stale_cleanup_does_not_stick_recovery_future() -> None:
    danmaku = mocked_danmaku()
    danmaku.stop.side_effect = RuntimeError('cleanup failed')
    controller = LiveConnectionController(
        FakeLive(),
        danmaku,
        FakeMonitor(),
        AsyncMock(return_value=room_info()),
        status_sink=AsyncMock(),
    )
    await controller.on_wss_hint(ObservedStatus.STALE)

    with pytest.raises(RuntimeError, match='cleanup failed'):
        await controller.on_confirmed_status(live_snapshot())

    danmaku.stop.side_effect = None
    await controller.on_confirmed_status(live_snapshot())

    danmaku.start.assert_awaited_once_with()
    assert controller.active is True


@pytest.mark.asyncio
async def test_external_monitor_enable_skips_legacy_polling() -> None:
    from blrec.bili.live_monitor import LiveMonitor
    from blrec.bili.models import LiveStatus

    danmaku = Mock()
    live = Mock()
    live.room_id = 1001
    live.room_info = SimpleNamespace(live_status=LiveStatus.LIVE)
    monitor = LiveMonitor(danmaku, live, status_sink=AsyncMock())

    monitor.enable()

    danmaku.add_listener.assert_called_once_with(monitor)
    assert hasattr(monitor, '_polling_task') is False


@pytest.mark.asyncio
async def test_external_monitor_forwards_wss_status_hints() -> None:
    from blrec.bili.danmaku_client import DanmakuCommand
    from blrec.bili.live_monitor import LiveMonitor
    from blrec.bili.models import LiveStatus

    live = Mock()
    live.room_id = 1001
    live.room_info = SimpleNamespace(live_status=LiveStatus.PREPARING)
    live.update_room_info = AsyncMock()
    live.get_live_streams = AsyncMock(return_value=[])
    status_sink = AsyncMock()
    monitor = LiveMonitor(Mock(), live, status_sink=status_sink)
    monitor.enable()

    await monitor.on_danmaku_received({'cmd': DanmakuCommand.LIVE.value})
    await monitor.on_danmaku_received({'cmd': DanmakuCommand.PREPARING.value})
    await monitor.on_danmaku_received(
        {'cmd': DanmakuCommand.PREPARING.value, 'round': 1}
    )

    assert status_sink.await_args_list == [
        call(ObservedStatus.LIVE),
        call(ObservedStatus.PREPARING),
        call(ObservedStatus.ROUND),
    ]
    live.update_room_info.assert_not_awaited()


@pytest.mark.asyncio
async def test_external_monitor_ignores_room_change_legacy_read() -> None:
    from blrec.bili.danmaku_client import DanmakuCommand
    from blrec.bili.live_monitor import LiveMonitor

    live = Mock()
    live.room_id = 1001
    live.room_info = SimpleNamespace(live_status=LiveStatus.LIVE)
    live.update_room_info = AsyncMock()
    status_sink = AsyncMock()
    monitor = LiveMonitor(Mock(), live, status_sink=status_sink)
    monitor.enable()

    await monitor.on_danmaku_received({'cmd': DanmakuCommand.ROOM_CHANGE.value})

    live.update_room_info.assert_not_awaited()
    status_sink.assert_not_awaited()


@pytest.mark.asyncio
async def test_external_monitor_applies_confirmed_status_to_existing_events() -> None:
    from blrec.bili.live_monitor import LiveMonitor
    from blrec.bili.models import LiveStatus

    live = Mock()
    live.room_id = 1001
    live.room_info = SimpleNamespace(live_status=LiveStatus.LIVE)
    live.get_live_streams = AsyncMock(return_value=[])
    listener = AsyncMock()
    monitor = LiveMonitor(Mock(), live, status_sink=AsyncMock())
    monitor.add_listener(listener)
    monitor.enable()

    await monitor.apply_confirmed_status(ObservedStatus.LIVE)
    await monitor.apply_confirmed_status(ObservedStatus.PREPARING)

    assert listener.on_live_status_changed.await_args_list == [
        call(LiveStatus.LIVE, LiveStatus.PREPARING),
        call(LiveStatus.PREPARING, LiveStatus.LIVE),
    ]
    listener.on_live_began.assert_awaited_once_with(live)
    listener.on_live_ended.assert_awaited_once_with(live)


@pytest.mark.asyncio
async def test_external_monitor_disable_skips_legacy_polling_cleanup() -> None:
    from blrec.bili.live_monitor import LiveMonitor
    from blrec.bili.models import LiveStatus

    danmaku = Mock()
    live = Mock()
    live.room_id = 1001
    live.room_info = SimpleNamespace(live_status=LiveStatus.PREPARING)
    monitor = LiveMonitor(danmaku, live, status_sink=AsyncMock())
    stop_polling = AsyncMock()
    monitor._stop_polling = stop_polling
    monitor.enable()

    monitor.disable()
    await asyncio.sleep(0)

    stop_polling.assert_not_awaited()
    danmaku.remove_listener.assert_called_once_with(monitor)


@pytest.mark.asyncio
async def test_external_monitor_skips_legacy_status_checks() -> None:
    from blrec.bili.live_monitor import LiveMonitor

    live = Mock()
    live.room_id = 1001
    live.room_info = SimpleNamespace(live_status=LiveStatus.PREPARING)
    live.update_room_info = AsyncMock()
    monitor = LiveMonitor(Mock(), live, status_sink=AsyncMock())
    monitor.enable()

    await monitor.on_client_reconnected()
    await monitor.check_live_status()

    live.update_room_info.assert_not_awaited()


@pytest.mark.asyncio
async def test_live_replaces_validated_room_info_and_real_room_id() -> None:
    from blrec.bili.live import Live

    live = object.__new__(Live)
    live._room_id = 1001
    live._room_info = room_info(room_id=1001)
    redirected = room_info(room_id=2002)

    live.replace_room_info(redirected)

    assert live.room_info is redirected
    assert live.room_id == 2002


@pytest.mark.asyncio
async def test_danmaku_room_id_can_only_change_while_stopped() -> None:
    from blrec.bili.danmaku_client import DanmakuClient

    danmaku = object.__new__(DanmakuClient)
    danmaku._stopped = True
    danmaku._room_id = 1001
    danmaku._logger_context = {'room_id': 1001}
    danmaku._logger = Mock()

    danmaku.set_room_id(2002)

    assert danmaku.room_id == 2002
    danmaku._stopped = False
    with pytest.raises(RuntimeError, match='while stopped'):
        danmaku.set_room_id(3003)
    assert danmaku.room_id == 2002


@pytest.mark.asyncio
async def test_batch_task_registers_without_starting_websocket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task_module = load_task_module(monkeypatch)
    RecordTask = task_module.RecordTask

    live = Mock()
    live.room_id = 1001
    live.room_info = room_info(uid=99, room_id=2002)
    live.user_info = SimpleNamespace(uid=1)
    monkeypatch.setattr(task_module, 'Live', Mock(return_value=live))
    coordinator = Mock()
    coordinator.observe_wss = AsyncMock()
    anonymous = Mock()
    anonymous.confirm_status = AsyncMock()
    task = RecordTask(
        1001, live_status_coordinator=coordinator, anonymous_room_client=anonymous
    )
    danmaku = AsyncMock()
    controller = AsyncMock()
    recorder = AsyncMock()
    task._danmaku_client = danmaku
    task._connection_controller = controller
    task._recorder = recorder

    await task.enable_monitor()

    coordinator.register.assert_called_once()
    assert coordinator.register.call_args.kwargs['uid'] == 1
    assert coordinator.register.call_args.kwargs['room_id'] == 2002
    assert coordinator.register.call_args.kwargs['listener'] == (
        controller.on_confirmed_status
    )
    assert coordinator.register.call_args.kwargs['confirmer'] == (
        anonymous.confirm_status
    )
    assert coordinator.register.call_args.kwargs['requested_room_id'] == 1001
    assert coordinator.register.call_args.kwargs['mapping_loader'] == (
        anonymous.fetch_uid_mappings
    )
    assert coordinator.register.call_args.kwargs['confirmer_uses_room_id'] is True
    danmaku.start.assert_not_awaited()

    await task.disable_monitor()

    coordinator.unregister.assert_called_once_with(1001)
    controller.close.assert_awaited_once()
    recorder.stop.assert_not_awaited()


@pytest.mark.asyncio
async def test_redirect_uses_canonical_room_with_stable_task_ownership(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from blrec.bili.live_status_coordinator import LiveStatusCoordinator

    task_module = load_task_module(monkeypatch)

    class CanonicalLive:
        def __init__(self) -> None:
            self.room_id = 123
            self.room_info = room_info(uid=0, room_id=123)
            self.user_info = SimpleNamespace(uid=0)

        def replace_room_info(self, value: RoomInfo) -> None:
            self.room_info = value
            self.room_id = value.room_id

    class CanonicalDanmaku:
        def __init__(self, live: CanonicalLive) -> None:
            self.live = live
            self.room_id = 123
            self.stopped = True
            self.started = 0
            self.stopped_count = 0

        def set_room_id(self, value: int) -> None:
            assert self.stopped is True
            self.room_id = value

        async def start(self) -> None:
            assert self.live.room_id == 2002
            assert self.room_id == 2002
            self.stopped = False
            self.started += 1

        async def stop(self) -> None:
            self.stopped = True
            self.stopped_count += 1

    canonical_live = CanonicalLive()
    monkeypatch.setattr(task_module, 'Live', Mock(return_value=canonical_live))
    batch_snapshot = StatusSnapshot(
        7, 2002, ObservedStatus.LIVE, 100.0, StatusSource.BATCH, 10, '7:10'
    )
    batch_client = Mock()
    batch_client.fetch = AsyncMock(
        return_value=BatchStatusResult({7: batch_snapshot}, frozenset())
    )
    coordinator = LiveStatusCoordinator(batch_client, clock=lambda: 100.0)
    observe_wss = AsyncMock(wraps=coordinator.observe_wss)
    coordinator.observe_wss = observe_wss
    anonymous = Mock()
    anonymous.fetch_uid_mappings = AsyncMock(return_value={123: (2002, 7)})
    anonymous.confirm_status = AsyncMock(
        return_value=StatusSnapshot(
            7, 2002, ObservedStatus.LIVE, 100.0, StatusSource.CONFIRMATION, 10, '7:10'
        )
    )
    canonical_room_info = room_info(uid=7, room_id=2002)
    anonymous.load_room_info = AsyncMock(return_value=canonical_room_info)
    danmaku = CanonicalDanmaku(canonical_live)
    monitor = FakeMonitor()
    controller = LiveConnectionController(
        canonical_live,
        danmaku,
        monitor,
        anonymous.load_room_info,
        status_sink=coordinator.observe_wss,
        registration_key=123,
    )
    task = task_module.RecordTask(
        123, live_status_coordinator=coordinator, anonymous_room_client=anonymous
    )
    task._danmaku_client = danmaku
    task._live_monitor = monitor
    task._connection_controller = controller

    await task.enable_monitor()
    await coordinator.poll_once()
    for _ in range(10):
        if danmaku.started:
            break
        await asyncio.sleep(0)

    anonymous.fetch_uid_mappings.assert_awaited_once_with((123,))
    anonymous.confirm_status.assert_awaited_once_with(2002)
    anonymous.load_room_info.assert_awaited_once_with(2002)
    assert canonical_live.room_info is canonical_room_info
    assert danmaku.started == 1
    assert coordinator.metrics(100.0).registered_rooms == 1

    await controller.on_wss_hint(ObservedStatus.PREPARING)
    observe_wss.assert_awaited_once_with(123, ObservedStatus.PREPARING)
    original_unregister = coordinator.unregister
    unregister = Mock(side_effect=original_unregister)
    coordinator.unregister = unregister
    await task.disable_monitor()

    unregister.assert_called_once_with(123)
    assert coordinator.metrics(100.0).registered_rooms == 0
    assert danmaku.stopped_count == 1


@pytest.mark.asyncio
async def test_batch_task_requires_both_shared_dependencies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task_module = load_task_module(monkeypatch)
    monkeypatch.setattr(task_module, 'Live', Mock())

    with pytest.raises(ValueError, match='must be provided together'):
        task_module.RecordTask(1001, live_status_coordinator=Mock())
    with pytest.raises(ValueError, match='must be provided together'):
        task_module.RecordTask(1001, anonymous_room_client=Mock())


@pytest.mark.asyncio
async def test_legacy_task_keeps_original_monitor_lifecycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task_module = load_task_module(monkeypatch)
    monkeypatch.setattr(task_module, 'Live', Mock())
    task = task_module.RecordTask(1001)
    task._danmaku_client = AsyncMock()
    task._live_monitor = Mock()

    await task.enable_monitor()
    await task.disable_monitor()

    task._danmaku_client.start.assert_awaited_once_with()
    task._live_monitor.enable.assert_called_once_with()
    task._live_monitor.disable.assert_called_once_with()
    task._danmaku_client.stop.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_batch_task_builds_external_monitor_for_real_room(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task_module = load_task_module(monkeypatch)
    live = Mock()
    live.room_id = 1001
    live.room_info = room_info(uid=7, room_id=2002)
    monkeypatch.setattr(task_module, 'Live', Mock(return_value=live))
    danmaku = Mock()
    danmaku_factory = Mock(return_value=danmaku)
    monitor = Mock()
    monitor_factory = Mock(return_value=monitor)
    controller = Mock()
    controller.on_wss_hint = AsyncMock()
    controller_factory = Mock(return_value=controller)
    monkeypatch.setattr(task_module, 'DanmakuClient', danmaku_factory)
    monkeypatch.setattr(task_module, 'LiveMonitor', monitor_factory)
    monkeypatch.setattr(task_module, 'LiveConnectionController', controller_factory)
    coordinator = Mock()
    coordinator.observe_wss = AsyncMock()
    anonymous = Mock()
    anonymous.load_room_info = AsyncMock(return_value=live.room_info)
    task = task_module.RecordTask(
        1001, live_status_coordinator=coordinator, anonymous_room_client=anonymous
    )

    task._setup_danmaku_client()
    task._setup_live_monitor()

    assert danmaku_factory.call_args.args[3] == 2002
    assert monitor_factory.call_args.args == (danmaku, live)
    status_sink = monitor_factory.call_args.kwargs['status_sink']
    await status_sink(ObservedStatus.PREPARING)
    controller.on_wss_hint.assert_awaited_once_with(ObservedStatus.PREPARING)
    controller_factory.assert_called_once()
    room_info_loader = controller_factory.call_args.args[3]
    assert await room_info_loader(2002) is live.room_info
    anonymous.load_room_info.assert_awaited_once_with(2002)
    assert controller_factory.call_args.kwargs['status_sink'] == (
        coordinator.observe_wss
    )
    assert controller_factory.call_args.kwargs['registration_key'] == 1001


def test_batch_task_uses_anonymous_sticky_websocket_route_without_cookie(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task_module = load_task_module(monkeypatch)
    live = Mock()
    live.room_id = 1001
    live.room_info = room_info(uid=7, room_id=2002)
    live.session = Mock()
    live.appapi = Mock()
    live.webapi = Mock()
    live.base_api_urls = ['https://api.example']
    live.base_live_api_urls = ['https://live.example']
    live.base_play_info_api_urls = ['https://play.example']
    live.stream_headers = {'User-Agent': 'fixture'}
    live.headers = {'User-Agent': 'fixture', 'Cookie': 'DedeUserID=7;'}
    live.cookie = 'DedeUserID=7;'
    monkeypatch.setattr(task_module, 'Live', Mock(return_value=live))
    pool = Mock()
    websocket_session = Mock()
    pool.client.return_value = websocket_session
    danmaku_factory = Mock()
    monkeypatch.setattr(task_module, 'DanmakuClient', danmaku_factory)
    anonymous_appapi = Mock()
    anonymous_webapi = Mock()
    appapi_factory = Mock(return_value=anonymous_appapi)
    webapi_factory = Mock(return_value=anonymous_webapi)
    monkeypatch.setattr(task_module, 'AppApi', appapi_factory, raising=False)
    monkeypatch.setattr(task_module, 'WebApi', webapi_factory, raising=False)
    task = task_module.RecordTask(1001, network_session_pool=pool)

    task._setup_danmaku_client()

    pool.client.assert_any_call('danmaku', anonymous=True, affinity_key='danmaku:2002')
    assert danmaku_factory.call_args.args[0] is websocket_session
    assert danmaku_factory.call_args.args[1] is anonymous_appapi
    assert danmaku_factory.call_args.args[2] is anonymous_webapi
    assert danmaku_factory.call_args.kwargs['headers'] == {'User-Agent': 'fixture'}


@pytest.mark.asyncio
async def test_batch_task_cookie_fallback_configures_one_coherent_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task_module = load_task_module(monkeypatch)
    live = Mock()
    live.room_id = 1001
    live.room_info = room_info(uid=7, room_id=2002)
    live.base_api_urls = ['https://api.example']
    live.base_live_api_urls = ['https://live.example']
    live.base_play_info_api_urls = ['https://play.example']
    live.stream_headers = {'User-Agent': 'fixture'}
    live.headers = {'User-Agent': 'fixture', 'Cookie': 'DedeUserID=7; buvid3=device;'}
    live.cookie = 'DedeUserID=7; buvid3=device;'
    monkeypatch.setattr(task_module, 'Live', Mock(return_value=live))
    anonymous_session = Mock(name='anonymous_session')
    authenticated_session = Mock(name='authenticated_session')
    pool = Mock()
    pool.client.side_effect = lambda _purpose, **options: (
        anonymous_session if options.get('anonymous') else authenticated_session
    )
    danmaku = Mock()
    danmaku.room_id = 2002
    danmaku.start = AsyncMock(
        side_effect=[OSError('anonymous connection failed'), None]
    )
    danmaku.stop = AsyncMock()
    danmaku.configure = Mock()
    monkeypatch.setattr(task_module, 'DanmakuClient', Mock(return_value=danmaku))
    appapis = [
        Mock(name='initial_anonymous_appapi'),
        Mock(name='anonymous_appapi'),
        Mock(name='authenticated_appapi'),
    ]
    webapis = [
        Mock(name='initial_anonymous_webapi'),
        Mock(name='anonymous_webapi'),
        Mock(name='authenticated_webapi'),
    ]
    monkeypatch.setattr(task_module, 'AppApi', Mock(side_effect=appapis), raising=False)
    monkeypatch.setattr(task_module, 'WebApi', Mock(side_effect=webapis), raising=False)
    task = task_module.RecordTask(1001, network_session_pool=pool)
    task._setup_danmaku_client()

    await task._danmaku_connection.start()

    assert danmaku.configure.call_args_list[0].args == (
        anonymous_session,
        appapis[1],
        webapis[1],
        live.stream_headers,
    )
    assert danmaku.configure.call_args_list[1].args == (
        authenticated_session,
        appapis[2],
        webapis[2],
        live.headers,
    )
    assert task._danmaku_connection.mode == 'authenticated'


def test_batch_task_cookie_refresh_does_not_change_an_active_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task_module = load_task_module(monkeypatch)
    live = Mock()
    live.room_id = 1001
    live.room_info = room_info(uid=7, room_id=2002)
    live.cookie = 'old-cookie'
    live.headers = {'Cookie': 'new-cookie'}
    monkeypatch.setattr(task_module, 'Live', Mock(return_value=live))
    task = task_module.RecordTask(
        1001, live_status_coordinator=Mock(), anonymous_room_client=Mock()
    )
    danmaku = Mock()
    danmaku.headers = {'Cookie': 'frozen-cookie'}
    task._danmaku_client = danmaku

    task.cookie = 'new-cookie'

    assert danmaku.headers == {'Cookie': 'frozen-cookie'}


@pytest.mark.asyncio
async def test_batch_task_does_not_restart_inactive_websocket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task_module = load_task_module(monkeypatch)
    monkeypatch.setattr(task_module, 'Live', Mock())
    task = task_module.RecordTask(
        1001, live_status_coordinator=Mock(), anonymous_room_client=Mock()
    )
    task._danmaku_client = AsyncMock()
    task._connection_controller = Mock(active=False)

    await task.restart_danmaku_client()

    task._danmaku_client.restart.assert_not_awaited()


@pytest.mark.asyncio
async def test_task_manager_injects_shared_batch_dependencies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    load_task_module(monkeypatch)
    manager_module = importlib.import_module('blrec.task.task_manager')
    task = Mock()
    task.setup = AsyncMock()
    task_factory = Mock(return_value=task)
    monkeypatch.setattr(manager_module, 'RecordTask', task_factory)
    settings_manager = Mock()
    settings_manager.get_settings.return_value = SimpleNamespace(bili_api=object())
    settings_manager.apply_task_header_settings = AsyncMock()
    coordinator = Mock()
    anonymous = Mock()
    with pytest.raises(ValueError, match='must be provided together'):
        manager_module.RecordTaskManager(settings_manager, coordinator)
    manager = manager_module.RecordTaskManager(settings_manager, coordinator, anonymous)
    manager.apply_task_bili_api_settings = Mock()
    settings = SimpleNamespace(
        room_id=1001,
        header=object(),
        output=object(),
        danmaku=object(),
        recorder=object(),
        postprocessing=object(),
        enable_monitor=False,
        enable_recorder=False,
    )

    await manager.add_task(settings)

    task_factory.assert_called_once_with(
        1001, live_status_coordinator=coordinator, anonymous_room_client=anonymous
    )


@pytest.mark.asyncio
async def test_recorder_waits_until_external_monitor_is_active(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    load_task_module(monkeypatch)
    recorder_module = importlib.import_module('blrec.core.recorder')
    recorder = object.__new__(recorder_module.Recorder)
    recorder._live_monitor = Mock(enabled=False)
    recorder._danmaku_dumper = Mock()
    recorder._raw_danmaku_dumper = Mock()
    recorder._cover_downloader = Mock()
    recorder._logger = Mock()
    recorder._live = Mock()
    recorder._live.is_living.return_value = True
    recorder._stream_available = False
    recorder._print_live_info = Mock()
    recorder._print_waiting_message = Mock()
    recorder._start_recording = AsyncMock()

    await recorder._do_start()

    recorder._start_recording.assert_not_awaited()
    recorder._print_waiting_message.assert_called_once_with()
    assert recorder._stream_available is False


@pytest.mark.asyncio
async def test_recorder_starts_when_monitor_is_active_and_live(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    load_task_module(monkeypatch)
    recorder_module = importlib.import_module('blrec.core.recorder')
    recorder = object.__new__(recorder_module.Recorder)
    recorder._live_monitor = Mock(enabled=True)
    recorder._danmaku_dumper = Mock()
    recorder._raw_danmaku_dumper = Mock()
    recorder._cover_downloader = Mock()
    recorder._logger = Mock()
    recorder._live = Mock()
    recorder._live.is_living.return_value = True
    recorder._stream_available = False
    recorder._print_live_info = Mock()
    recorder._print_waiting_message = Mock()
    recorder._start_recording = AsyncMock()

    await recorder._do_start()

    recorder._start_recording.assert_awaited_once_with()
    recorder._print_waiting_message.assert_not_called()
    assert recorder._stream_available is True
