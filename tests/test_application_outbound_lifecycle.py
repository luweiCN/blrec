import asyncio
from typing import Any, List, Optional

import pytest

import blrec.application as application_module
from blrec.application import Application
from blrec.setting import Settings


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


class _FailingCloseEmitter(_Emitter):
    def __init__(self, calls: List[str]) -> None:
        super().__init__(calls)
        self._close_calls = 0

    async def close(self, *, drain_timeout_seconds: float = 5) -> None:
        await super().close(drain_timeout_seconds=drain_timeout_seconds)
        self._close_calls += 1
        if self._close_calls == 1:
            raise OSError('session close failed')


class _UpdateClient:
    def __init__(self, calls: List[str], value: Optional[str] = '4.0.0') -> None:
        self._calls = calls
        self._value = value

    async def start(self) -> None:
        self._calls.append('update.start')

    async def close(self) -> None:
        self._calls.append('update.close')

    async def get_latest_version_string(self, _project_name: str) -> Optional[str]:
        self._calls.append('update.get')
        return self._value


class _TaskManager:
    def __init__(self, calls: List[str]) -> None:
        self._calls = calls

    async def stop_all_tasks(self, force: bool = False) -> None:
        assert not force
        self._calls.append('tasks.stop')

    async def destroy_all_tasks(self) -> None:
        self._calls.append('tasks.destroy')


class _DisableProbe:
    def __init__(self, calls: List[str], name: str) -> None:
        self._calls = calls
        self._name = name

    def disable(self) -> None:
        self._calls.append('{}.disable'.format(self._name))


class _ValidationSession:
    def __init__(self) -> None:
        self.closed = False
        self.close_calls = 0

    async def close(self) -> None:
        self.close_calls += 1
        self.closed = True


class _ValidationPool:
    def __init__(self, client: object) -> None:
        self._client = client
        self.calls = []

    def client(
        self, purpose: str, *, anonymous: bool = False, affinity_key: object = None
    ) -> object:
        self.calls.append((purpose, anonymous, affinity_key))
        return self._client


class _FailingJournal:
    async def open(self) -> None:
        raise RuntimeError('control journal failed')


class _SecretFailingCloseEmitter(_Emitter):
    async def close(self, *, drain_timeout_seconds: float = 5) -> None:
        await super().close(drain_timeout_seconds=drain_timeout_seconds)
        raise OSError('https://secret.invalid/hook payload-secret')


class _RecordingLogger:
    def __init__(self) -> None:
        self.errors: List[str] = []

    def info(self, _message: str, *_args: object) -> None:
        return None

    def debug(self, _message: str, *_args: object) -> None:
        return None

    def error(self, message: str, *args: object) -> None:
        self.errors.append(message.format(*args))


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
async def test_launch_and_exit_own_update_metadata_client() -> None:
    calls: List[str] = []
    app = object.__new__(Application)
    app._setup_logger = lambda: None
    app._setup_live_status_monitor = _noop

    def setup() -> None:
        app._webhook_emitter = _Emitter(calls)
        app._update_metadata_client = _UpdateClient(calls)

    app._setup = setup
    app._control_operation_journal = None

    async def load() -> None:
        return None

    app._load_tasks_and_controls = load
    await app.launch()
    await asyncio.sleep(0)

    assert calls[:3] == ['webhook.start', 'webhook.enable', 'update.start']
    assert await app.get_latest_version_string('blrec') == '4.0.0'

    await app._teardown_update_metadata()

    assert calls[-2:] == ['update.get', 'update.close']
    assert not hasattr(app, '_update_metadata_client')


@pytest.mark.asyncio
async def test_cookie_validation_fallback_session_is_closed_and_forgotten() -> None:
    app = object.__new__(Application)
    session = _ValidationSession()
    app._bili_validation_session = session

    await app._teardown_bili_validation_session()
    await app._teardown_bili_validation_session()

    assert session.close_calls == 1
    assert app._bili_validation_session is None


def test_cookie_validation_routed_client_is_pool_owned() -> None:
    client = object()
    pool = _ValidationPool(client)
    app = object.__new__(Application)
    app._bili_validation_session = None
    app._ensure_network_session_pool = lambda: pool

    assert app._get_bili_validation_session() is client
    assert app._bili_validation_session is None
    assert pool.calls == [('bili_api', True, None)]


def test_application_notifiers_share_injected_dispatcher() -> None:
    dispatcher = object()
    app = Application(Settings(), notification_dispatcher=dispatcher)

    app._setup_notifiers()
    try:
        assert app._email_notifier._dispatcher is dispatcher
        assert app._serverchan_notifier._dispatcher is dispatcher
        assert app._pushdeer_notifier._dispatcher is dispatcher
        assert app._pushplus_notifier._dispatcher is dispatcher
        assert app._telegram_notifier._dispatcher is dispatcher
        assert app._bark_notifier._dispatcher is dispatcher
    finally:
        app._destroy_notifiers()


def test_partial_notifier_teardown_is_idempotent() -> None:
    calls: List[str] = []
    app = object.__new__(Application)
    app._email_notifier = _DisableProbe(calls, 'email')
    app._serverchan_notifier = _DisableProbe(calls, 'serverchan')

    app._destroy_notifiers()
    app._destroy_notifiers()

    assert calls == ['email.disable', 'serverchan.disable']
    assert not hasattr(app, '_email_notifier')
    assert not hasattr(app, '_serverchan_notifier')


@pytest.mark.asyncio
async def test_exit_disables_webhooks_before_drain_and_deletes_emitter() -> None:
    calls: List[str] = []
    app = object.__new__(Application)
    app._task_manager = _TaskManager(calls)
    app._webhook_emitter = _Emitter(calls)
    app._update_metadata_client = _UpdateClient(calls)
    app._live_status_coordinator = None
    app._live_status_session = None
    app._network_session_pool = None
    app._destroy = lambda: calls.append('application.destroy')

    await app._exit()

    assert calls == [
        'update.close',
        'webhook.disable',
        'webhook.close',
        'tasks.stop',
        'tasks.destroy',
        'application.destroy',
    ]
    assert not hasattr(app, '_webhook_emitter')
    assert not hasattr(app, '_update_metadata_client')


@pytest.mark.asyncio
async def test_failed_webhook_close_keeps_emitter_for_retry() -> None:
    calls: List[str] = []
    app = object.__new__(Application)
    emitter = _FailingCloseEmitter(calls)
    app._webhook_emitter = emitter

    with pytest.raises(OSError, match='session close failed'):
        await app._teardown_webhooks()

    assert app._webhook_emitter is emitter

    await app._teardown_webhooks()

    assert not hasattr(app, '_webhook_emitter')
    assert calls == [
        'webhook.disable',
        'webhook.close',
        'webhook.disable',
        'webhook.close',
    ]


@pytest.mark.asyncio
async def test_exit_keeps_failed_webhook_close_for_later_retry() -> None:
    calls: List[str] = []
    app = object.__new__(Application)
    emitter = _FailingCloseEmitter(calls)
    app._task_manager = _TaskManager(calls)
    app._webhook_emitter = emitter
    app._live_status_coordinator = None
    app._live_status_session = None
    app._network_session_pool = None
    app._destroy_notifiers = lambda: calls.append('application.destroy_notifiers')
    app._destroy_exception_handler = lambda: calls.append(
        'application.destroy_exception_handler'
    )

    with pytest.raises(OSError, match='session close failed'):
        await app._exit()

    assert app._webhook_emitter is emitter
    assert calls == [
        'webhook.disable',
        'webhook.close',
        'tasks.stop',
        'tasks.destroy',
        'application.destroy_notifiers',
        'application.destroy_exception_handler',
    ]

    await app._teardown_webhooks()

    assert not hasattr(app, '_webhook_emitter')
    assert emitter._close_calls == 2


@pytest.mark.asyncio
async def test_launch_failure_after_webhook_start_still_closes_session() -> None:
    calls: List[str] = []
    app = object.__new__(Application)
    app._setup_logger = lambda: None
    app._setup_live_status_monitor = _noop

    def setup() -> None:
        app._webhook_emitter = _Emitter(calls)
        app._email_notifier = _DisableProbe(calls, 'email')
        app._exception_handler = _DisableProbe(calls, 'exceptions')

    app._setup = setup
    app._control_operation_journal = _FailingJournal()
    app._teardown_live_status_monitor_after_failure = _noop

    with pytest.raises(RuntimeError, match='control journal failed'):
        await app.launch()

    assert calls == [
        'webhook.start',
        'webhook.enable',
        'webhook.disable',
        'webhook.close',
        'email.disable',
        'exceptions.disable',
    ]
    assert not hasattr(app, '_email_notifier')
    assert not hasattr(app, '_exception_handler')


@pytest.mark.asyncio
async def test_launch_cleanup_log_redacts_webhook_error_details(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: List[str] = []
    recording_logger = _RecordingLogger()
    monkeypatch.setattr(application_module, 'logger', recording_logger)
    app = object.__new__(Application)
    app._setup_logger = lambda: None
    app._setup_live_status_monitor = _noop
    app._setup = lambda: setattr(
        app, '_webhook_emitter', _SecretFailingCloseEmitter(calls)
    )
    app._control_operation_journal = _FailingJournal()
    app._teardown_live_status_monitor_after_failure = _noop

    with pytest.raises(RuntimeError, match='control journal failed'):
        await app.launch()

    assert recording_logger.errors == ['Webhook teardown after launch failure: OSError']
    assert 'secret.invalid' not in recording_logger.errors[0]
    assert 'payload-secret' not in recording_logger.errors[0]


async def _noop(*_args: Any, **_kwargs: Any) -> None:
    return None
