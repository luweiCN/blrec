import asyncio
from types import SimpleNamespace
from typing import Iterator, List, Optional, Sequence
from unittest.mock import AsyncMock

import aiohttp
import pytest
from pydantic import ValidationError

from blrec.application import Application
from blrec.bili.batch_status_client import BatchStatusClient
from blrec.bili.live_status import (
    BatchStatusResult,
    ObservedStatus,
    StatusSnapshot,
    StatusSource,
)
from blrec.bili.live_status_coordinator import LiveStatusCoordinator
from blrec.exception import ForbiddenError
from blrec.setting.models import LiveMonitorSettings, Settings, SettingsIn
from blrec.setting.setting_manager import SettingsManager


class OrderedTaskManager:
    def __init__(self, calls: List[str], failure_stage: Optional[str] = None) -> None:
        self._calls = calls
        self._failure_stage = failure_stage

    async def stop_all_tasks(self, force: bool = False) -> None:
        self._calls.append('tasks.stop')
        if self._failure_stage == 'tasks.stop':
            raise RuntimeError('tasks.stop')

    async def destroy_all_tasks(self) -> None:
        self._calls.append('tasks.destroy')
        if self._failure_stage == 'tasks.destroy':
            raise RuntimeError('tasks.destroy')


class OrderedCoordinator:
    def __init__(
        self, calls: List[str], failure: Optional[BaseException] = None
    ) -> None:
        self._calls = calls
        self._failure = failure

    async def stop(self) -> None:
        self._calls.append('coordinator.stop')
        if self._failure is not None:
            raise self._failure


class OrderedSession:
    def __init__(self, calls: List[str]) -> None:
        self._calls = calls
        self.closed = False

    async def close(self) -> None:
        self._calls.append('session.close')
        self.closed = True


class SettingsApplication:
    def __init__(self, recording: bool) -> None:
        self.recording = recording
        self.restart_count = 0

    def has_recording_task(self) -> bool:
        return self.recording

    async def restart(self) -> None:
        self.restart_count += 1


class BlockingStatusClient(BatchStatusClient):
    def __init__(self) -> None:
        self.calls: List[List[int]] = []
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def fetch(
        self, uids: Sequence[int], *, observed_at: float
    ) -> BatchStatusResult:
        self.calls.append(list(uids))
        if len(self.calls) == 1:
            self.entered.set()
            await self.release.wait()
        return BatchStatusResult(
            {
                uid: StatusSnapshot(
                    uid=uid,
                    room_id=uid + 1000,
                    status=ObservedStatus.PREPARING,
                    observed_at=observed_at,
                    source=StatusSource.BATCH,
                    live_time=0,
                    observation_key=None,
                )
                for uid in uids
            },
            frozenset(),
        )


class LegacyTaskManager:
    def __init__(self, monitor_states: Sequence[bool]) -> None:
        self._monitor_states = monitor_states
        self.iterations = 0

    def get_all_task_data(self) -> Iterator[SimpleNamespace]:
        self.iterations += 1
        for monitor_enabled in self._monitor_states:
            yield SimpleNamespace(
                task_status=SimpleNamespace(monitor_enabled=monitor_enabled)
            )


def test_live_monitor_settings_reject_unsafe_interval() -> None:
    with pytest.raises(ValidationError):
        LiveMonitorSettings(interval_seconds=10)


@pytest.mark.asyncio
async def test_settings_manager_rejects_mode_change_while_recording() -> None:
    application = SettingsApplication(recording=True)
    current = Settings(live_monitor=LiveMonitorSettings(mode='batch'))
    manager = SettingsManager(application, current)  # type: ignore[arg-type]

    with pytest.raises(ForbiddenError, match='recording'):
        await manager.change_settings(
            SettingsIn(live_monitor=LiveMonitorSettings(mode='legacy'))
        )

    assert current.live_monitor.mode == 'batch'
    assert application.restart_count == 0


@pytest.mark.asyncio
async def test_settings_manager_restarts_for_safe_mode_change() -> None:
    application = SettingsApplication(recording=False)
    current = Settings(live_monitor=LiveMonitorSettings(mode='batch'))
    manager = SettingsManager(application, current)  # type: ignore[arg-type]

    async def skip_dump() -> None:
        return None

    manager.dump_settings = skip_dump  # type: ignore[assignment]
    await manager.change_settings(
        SettingsIn(live_monitor=LiveMonitorSettings(mode='legacy'))
    )

    assert current.live_monitor.mode == 'legacy'
    assert application.restart_count == 1


@pytest.mark.asyncio
async def test_partial_live_monitor_update_preserves_legacy_mode() -> None:
    application = SettingsApplication(recording=True)
    current = Settings(
        live_monitor=LiveMonitorSettings(
            mode='legacy',
            interval_seconds=45,
            batch_size=20,
            fallback_cooldown_seconds=1200,
        )
    )
    manager = SettingsManager(application, current)  # type: ignore[arg-type]
    dump_count = 0

    async def record_dump() -> None:
        nonlocal dump_count
        dump_count += 1

    manager.dump_settings = record_dump  # type: ignore[assignment]
    update = SettingsIn.parse_obj({'liveMonitor': {'batchSize': 10}})

    assert update.live_monitor is not None
    assert update.live_monitor.__fields_set__ == {'batch_size'}

    result = await manager.change_settings(update)

    assert current.live_monitor == LiveMonitorSettings(
        mode='legacy',
        interval_seconds=45,
        batch_size=10,
        fallback_cooldown_seconds=1200,
    )
    assert result.live_monitor == current.live_monitor
    assert application.restart_count == 0
    assert dump_count == 1


@pytest.mark.asyncio
async def test_partial_batch_settings_reconfigure_running_coordinator() -> None:
    current = Settings(
        live_monitor=LiveMonitorSettings(
            mode='batch',
            interval_seconds=30,
            batch_size=29,
            fallback_cooldown_seconds=600,
        )
    )
    client = BlockingStatusClient()
    coordinator = LiveStatusCoordinator(client)
    for uid in range(1, 31):
        coordinator.register(uid, uid + 1000, AsyncMock(), AsyncMock())
    application = object.__new__(Application)
    application._settings = current
    application._live_status_coordinator = coordinator
    manager = SettingsManager(application, current)

    async def skip_dump() -> None:
        return None

    manager.dump_settings = skip_dump  # type: ignore[assignment]
    polling = asyncio.create_task(coordinator.poll_once())
    await client.entered.wait()
    update = SettingsIn.parse_obj(
        {
            'liveMonitor': {
                'intervalSeconds': 45,
                'batchSize': 10,
                'fallbackCooldownSeconds': 1200,
            }
        }
    )
    changing = asyncio.create_task(manager.change_settings(update))
    await asyncio.sleep(0)
    changed_during_poll = changing.done()

    client.release.set()
    await polling
    result = await changing
    client.calls.clear()
    await coordinator.poll_once()

    assert changed_during_poll is False
    assert result.live_monitor == current.live_monitor
    assert coordinator.metrics(100.0).interval_seconds == 45
    assert coordinator.metrics(100.0).batch_size == 10
    assert coordinator._fallback_cooldown_seconds == 1200
    assert [len(call) for call in client.calls] == [10, 10, 10]


def test_legacy_metrics_count_monitor_enabled_tasks_with_one_iteration() -> None:
    application = Application(Settings(live_monitor=LiveMonitorSettings(mode='legacy')))
    task_manager = LegacyTaskManager([True, False])
    application._task_manager = task_manager  # type: ignore[assignment]

    metrics = application.get_live_status_metrics()

    assert metrics.registered_rooms == 2
    assert metrics.active_websockets == 1
    assert task_manager.iterations == 1


@pytest.mark.asyncio
async def test_application_forwards_current_live_suppression() -> None:
    app = object.__new__(Application)
    app._task_manager = SimpleNamespace(suppress_current_live=AsyncMock())

    await app.suppress_current_live(100)

    app._task_manager.suppress_current_live.assert_awaited_once_with(100)


@pytest.mark.asyncio
async def test_application_stops_coordinator_after_tasks() -> None:
    calls: List[str] = []
    app = object.__new__(Application)
    app._task_manager = OrderedTaskManager(calls)
    app._live_status_coordinator = OrderedCoordinator(calls)
    app._destroy = lambda: calls.append('application.destroy')

    await app._exit()

    assert calls == [
        'tasks.stop',
        'tasks.destroy',
        'coordinator.stop',
        'application.destroy',
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'failure_stage', ['tasks.stop', 'tasks.destroy', 'coordinator.stop']
)
async def test_application_exit_continues_cleanup_after_failure(
    failure_stage: str,
) -> None:
    calls: List[str] = []
    app = object.__new__(Application)
    app._task_manager = OrderedTaskManager(calls, failure_stage)
    coordinator_failure = (
        RuntimeError('coordinator.stop')
        if failure_stage == 'coordinator.stop'
        else None
    )
    app._live_status_coordinator = OrderedCoordinator(calls, coordinator_failure)
    app._live_status_session = OrderedSession(calls)
    app._destroy = lambda: calls.append('application.destroy')

    with pytest.raises(RuntimeError, match=failure_stage):
        await app._exit()

    assert calls == [
        'tasks.stop',
        'tasks.destroy',
        'coordinator.stop',
        'session.close',
        'application.destroy',
    ]
    assert app._live_status_coordinator is None
    assert app._live_status_session is None


@pytest.mark.asyncio
async def test_application_exit_does_not_swallow_cancellation() -> None:
    calls: List[str] = []
    app = object.__new__(Application)
    app._task_manager = OrderedTaskManager(calls)
    app._live_status_coordinator = OrderedCoordinator(calls, asyncio.CancelledError())
    app._live_status_session = OrderedSession(calls)
    app._destroy = lambda: calls.append('application.destroy')

    with pytest.raises(asyncio.CancelledError):
        await app._exit()

    assert calls == [
        'tasks.stop',
        'tasks.destroy',
        'coordinator.stop',
        'session.close',
        'application.destroy',
    ]


@pytest.mark.asyncio
async def test_application_launch_cleans_batch_monitor_when_setup_fails() -> None:
    calls: List[str] = []
    app = Application(Settings())
    coordinator = OrderedCoordinator(calls)
    session = OrderedSession(calls)
    app._setup_logger = lambda: None
    app._destroy = lambda: None

    async def setup_live_status_monitor() -> None:
        calls.append('coordinator.start')
        app._live_status_coordinator = coordinator  # type: ignore[assignment]
        app._live_status_session = session  # type: ignore[assignment]

    def fail_setup() -> None:
        raise RuntimeError('application.setup')

    app._setup_live_status_monitor = setup_live_status_monitor  # type: ignore
    app._setup = fail_setup

    try:
        with pytest.raises(RuntimeError, match='application.setup'):
            await app.launch()

        assert calls == ['coordinator.start', 'coordinator.stop', 'session.close']
        assert session.closed
        assert app._live_status_coordinator is None
        assert app._live_status_session is None
    finally:
        if app._live_status_coordinator is not None:
            await app._live_status_coordinator.stop()
        if app._live_status_session is not None:
            await app._live_status_session.close()


@pytest.mark.asyncio
async def test_application_closes_session_when_coordinator_start_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from blrec.bili.live_status_coordinator import LiveStatusCoordinator

    calls: List[str] = []
    session = OrderedSession(calls)
    session.cookie_jar = aiohttp.DummyCookieJar()  # type: ignore[attr-defined]
    session.headers = {}  # type: ignore[attr-defined]
    session.auth = None  # type: ignore[attr-defined]
    session.trust_env = False  # type: ignore[attr-defined]
    monkeypatch.setattr(aiohttp, 'ClientSession', lambda **kwargs: session)

    async def fail_start(coordinator: LiveStatusCoordinator) -> None:
        raise RuntimeError('coordinator.start')

    monkeypatch.setattr(LiveStatusCoordinator, 'start', fail_start)
    app = Application(Settings())

    try:
        with pytest.raises(RuntimeError, match='coordinator.start'):
            await app._setup_live_status_monitor()

        assert session.closed
        assert app._live_status_coordinator is None
        assert app._live_status_session is None
    finally:
        if not session.closed:
            await session.close()


@pytest.mark.asyncio
async def test_application_launches_and_closes_isolated_batch_session() -> None:
    from blrec.bili.net import connector, timeout

    app = Application(Settings())
    app._setup_logger = lambda: None
    app._setup = lambda: None
    app._destroy = lambda: None

    await app.launch()

    session = app._live_status_session
    assert session is not None
    assert isinstance(session.cookie_jar, aiohttp.DummyCookieJar)
    assert not session.headers
    assert session.connector is connector
    assert session.timeout is timeout
    assert app._live_status_coordinator is not None
    assert app._live_status_coordinator._polling_task is not None
    metrics = app.get_live_status_metrics()
    with pytest.raises(TypeError):
        metrics.mode = 'legacy'  # type: ignore[misc]
    assert not hasattr(metrics, 'headers')
    assert not hasattr(metrics, 'raw_payload')

    await app._exit()

    assert session.closed


@pytest.mark.asyncio
async def test_application_legacy_mode_does_not_start_batch_monitor() -> None:
    app = Application(Settings(live_monitor=LiveMonitorSettings(mode='legacy')))
    app._setup_logger = lambda: None
    app._setup = lambda: None
    app._destroy = lambda: None

    await app.launch()

    assert app._live_status_session is None
    assert app._live_status_coordinator is None
    assert app.get_live_status_metrics().mode == 'legacy'

    await app._exit()


@pytest.mark.asyncio
async def test_application_refreshes_managed_cookie_on_loaded_tasks() -> None:
    app = object.__new__(Application)
    app._task_manager = AsyncMock()

    await app.refresh_managed_cookie()

    app._task_manager.refresh_managed_cookie.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_application_wires_managed_account_into_legacy_tasks() -> None:
    provider = AsyncMock(return_value='SESSDATA=managed')
    reporter = AsyncMock()
    app = Application(
        Settings(live_monitor=LiveMonitorSettings(mode='legacy')),
        managed_cookie_provider=provider,
        auth_failure_reporter=reporter,
    )

    await app._setup_live_status_monitor()

    assert app._task_manager._managed_cookie_provider is provider
    assert app._task_manager._auth_failure_reporter is reporter
