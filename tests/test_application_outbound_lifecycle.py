import asyncio
from typing import Any, List

import pytest

from blrec.application import Application


class _Emitter:
    def __init__(self, calls: List[str]) -> None:
        self._calls = calls

    async def start(self) -> None:
        self._calls.append('webhook.start')

    def enable(self) -> None:
        self._calls.append('webhook.enable')

    def disable(self) -> None:
        self._calls.append('webhook.disable')

    async def close(self, *, drain_timeout_seconds: float = 5) -> None:
        assert drain_timeout_seconds == 5
        self._calls.append('webhook.close')


class _TaskManager:
    def __init__(self, calls: List[str]) -> None:
        self._calls = calls

    async def stop_all_tasks(self, force: bool = False) -> None:
        assert not force
        self._calls.append('tasks.stop')

    async def destroy_all_tasks(self) -> None:
        self._calls.append('tasks.destroy')


class _FailingJournal:
    async def open(self) -> None:
        raise RuntimeError('control journal failed')


@pytest.mark.asyncio
async def test_launch_starts_webhook_session_before_subscribing() -> None:
    calls: List[str] = []
    app = object.__new__(Application)
    app._setup_logger = lambda: None
    app._setup_live_status_monitor = _noop
    app._setup = lambda: setattr(app, '_webhook_emitter', _Emitter(calls))
    app._control_operation_journal = None

    async def load() -> None:
        calls.append('tasks.load')

    app._load_tasks_and_controls = load

    await app.launch()
    await asyncio.sleep(0)

    assert calls[:2] == ['webhook.start', 'webhook.enable']


@pytest.mark.asyncio
async def test_exit_disables_webhooks_before_drain_and_deletes_emitter() -> None:
    calls: List[str] = []
    app = object.__new__(Application)
    app._task_manager = _TaskManager(calls)
    app._webhook_emitter = _Emitter(calls)
    app._live_status_coordinator = None
    app._live_status_session = None
    app._network_session_pool = None
    app._destroy = lambda: calls.append('application.destroy')

    await app._exit()

    assert calls == [
        'webhook.disable',
        'webhook.close',
        'tasks.stop',
        'tasks.destroy',
        'application.destroy',
    ]
    assert not hasattr(app, '_webhook_emitter')


@pytest.mark.asyncio
async def test_launch_failure_after_webhook_start_still_closes_session() -> None:
    calls: List[str] = []
    app = object.__new__(Application)
    app._setup_logger = lambda: None
    app._setup_live_status_monitor = _noop
    app._setup = lambda: setattr(app, '_webhook_emitter', _Emitter(calls))
    app._control_operation_journal = _FailingJournal()
    app._teardown_live_status_monitor_after_failure = _noop

    with pytest.raises(RuntimeError, match='control journal failed'):
        await app.launch()

    assert calls == [
        'webhook.start',
        'webhook.enable',
        'webhook.disable',
        'webhook.close',
    ]


async def _noop(*_args: Any, **_kwargs: Any) -> None:
    return None
