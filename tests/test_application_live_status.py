from typing import List

import aiohttp
import pytest
from pydantic import ValidationError

from blrec.application import Application
from blrec.exception import ForbiddenError
from blrec.setting.models import LiveMonitorSettings, Settings, SettingsIn
from blrec.setting.setting_manager import SettingsManager


class OrderedTaskManager:
    def __init__(self, calls: List[str]) -> None:
        self._calls = calls

    async def stop_all_tasks(self, force: bool = False) -> None:
        self._calls.append('tasks.stop')

    async def destroy_all_tasks(self) -> None:
        self._calls.append('tasks.destroy')


class OrderedCoordinator:
    def __init__(self, calls: List[str]) -> None:
        self._calls = calls

    async def stop(self) -> None:
        self._calls.append('coordinator.stop')


class SettingsApplication:
    def __init__(self, recording: bool) -> None:
        self.recording = recording
        self.restart_count = 0

    def has_recording_task(self) -> bool:
        return self.recording

    async def restart(self) -> None:
        self.restart_count += 1


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
