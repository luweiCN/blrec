from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import blrec.web.main as web_main


@pytest.mark.asyncio
async def test_credential_callback_waits_until_application_is_started(
    monkeypatch,
) -> None:
    refresh = AsyncMock()
    monkeypatch.setattr(
        web_main, 'app', SimpleNamespace(refresh_managed_cookie=refresh)
    )
    monkeypatch.setattr(web_main, '_application_started', False, raising=False)

    await web_main._apply_primary_credential()

    refresh.assert_not_awaited()

    web_main._application_started = True
    await web_main._apply_primary_credential()

    refresh.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_realtime_highlight_snapshot_uses_runtime_worker(monkeypatch) -> None:
    progress = AsyncMock(return_value=({'id': 3, 'state': 'processing'},))
    monkeypatch.setattr(
        web_main,
        '_bili_account_runtime',
        SimpleNamespace(highlight_worker=SimpleNamespace(progress=progress)),
    )

    assert await web_main._realtime_highlight_snapshot() == [
        {'id': 3, 'state': 'processing'}
    ]
    progress.assert_awaited_once_with()


class PasswordWorkProbe:
    def __init__(self, events) -> None:
        self.events = events

    def close_admission(self) -> None:
        self.events.append('password.close_admission')

    async def shutdown(self) -> None:
        self.events.append('password.shutdown')


class ActiveMediaProbe:
    def __init__(self, events) -> None:
        self.events = events

    def close_admission(self) -> None:
        self.events.append('active_media.close_admission')

    async def shutdown(self) -> None:
        self.events.append('active_media.shutdown')


class NotificationDispatcherProbe:
    def __init__(self, events) -> None:
        self.events = events
        self.close_calls = 0

    async def start(self) -> None:
        self.events.append('notifications.start')

    async def close(self, *, drain_timeout_seconds=15) -> None:
        assert drain_timeout_seconds == 15
        self.close_calls += 1
        self.events.append('notifications.close')


class RuntimeProbe:
    manager = None
    unavailable_reason = None
    journal = None
    content_reader = None
    task_actions = None
    run_recording_session_action = None
    session_submission_manager = None
    retention_manager = None
    policy_manager = None
    category_catalog = None
    cover_library = None
    collection_manager = None
    highlight_service = None
    highlight_worker = None
    create_highlight_upload_task = None
    delete_highlight_clip = None

    def __init__(self, events) -> None:
        self.events = events

    async def start(self) -> None:
        self.events.append('runtime.start')

    async def close(self) -> None:
        self.events.append('runtime.close')


class FailingApplicationProbe:
    def __init__(self, events) -> None:
        self.events = events

    async def launch(self) -> None:
        self.events.append('app.launch')
        raise RuntimeError('application launch failed')

    async def exit(self) -> None:
        self.events.append('app.exit')


class JournalProbe:
    def __init__(self, events) -> None:
        self.events = events

    async def close(self) -> None:
        self.events.append('journal.close')


@pytest.mark.asyncio
async def test_startup_failure_always_closes_password_work_and_auth_store(
    monkeypatch,
) -> None:
    events = []
    password_work = PasswordWorkProbe(events)
    active_media = ActiveMediaProbe(events)
    dispatcher = NotificationDispatcherProbe(events)

    async def fail_start() -> None:
        events.append('runtime.start')
        raise RuntimeError('startup failed')

    async def fail_close() -> None:
        events.append('runtime.close')
        raise RuntimeError('runtime close failed')

    monkeypatch.setattr(
        web_main,
        '_admin_auth_store',
        SimpleNamespace(
            open=lambda: events.append('store.open'),
            close=lambda: events.append('store.close'),
        ),
    )
    monkeypatch.setattr(web_main, 'PasswordWorkCoordinator', lambda: password_work)
    monkeypatch.setattr(web_main, 'ActiveMediaService', lambda: active_media)
    monkeypatch.setattr(web_main, '_notification_dispatcher', dispatcher)
    monkeypatch.setattr(
        web_main,
        '_bili_account_runtime',
        SimpleNamespace(start=fail_start, close=fail_close),
    )
    monkeypatch.setattr(
        web_main._realtime_sampler,
        'stop',
        AsyncMock(side_effect=lambda: events.append('realtime.stop')),
    )
    monkeypatch.setattr(web_main.security, 'configure', lambda *args, **kwargs: None)
    monkeypatch.setattr(web_main.auth, 'configure', lambda *args, **kwargs: None)
    monkeypatch.setattr(
        web_main.security, 'reset', lambda: events.append('security.reset')
    )
    monkeypatch.setattr(web_main.auth, 'reset', lambda: events.append('auth.reset'))
    monkeypatch.setattr(web_main.browser_extension, 'reset', lambda: None)

    with pytest.raises(RuntimeError, match='runtime close failed'):
        await web_main.on_startup()

    assert 'password.close_admission' in events
    assert 'active_media.close_admission' in events
    assert 'auth.reset' in events
    assert 'security.reset' in events
    assert events.index('notifications.start') < events.index('runtime.start')
    assert events.index('notifications.close') > events.index('runtime.close')
    assert dispatcher.close_calls == 1
    assert events.index('password.shutdown') > events.index('runtime.close')
    assert events.index('active_media.shutdown') > events.index('runtime.close')
    assert events.index('store.close') > events.index('password.shutdown')


@pytest.mark.asyncio
async def test_startup_failure_after_application_launch_entered_calls_exit(
    monkeypatch,
) -> None:
    events = []
    password_work = PasswordWorkProbe(events)
    active_media = ActiveMediaProbe(events)
    dispatcher = NotificationDispatcherProbe(events)

    monkeypatch.setattr(
        web_main,
        '_admin_auth_store',
        SimpleNamespace(
            open=lambda: events.append('store.open'),
            close=lambda: events.append('store.close'),
        ),
    )
    monkeypatch.setattr(web_main, 'PasswordWorkCoordinator', lambda: password_work)
    monkeypatch.setattr(web_main, 'ActiveMediaService', lambda: active_media)
    monkeypatch.setattr(web_main, '_notification_dispatcher', dispatcher)
    monkeypatch.setattr(web_main, '_bili_account_runtime', RuntimeProbe(events))
    monkeypatch.setattr(web_main, 'app', FailingApplicationProbe(events))
    monkeypatch.setattr(web_main, '_control_operation_journal', JournalProbe(events))
    monkeypatch.setattr(
        web_main._realtime_sampler,
        'stop',
        AsyncMock(side_effect=lambda: events.append('realtime.stop')),
    )
    monkeypatch.setattr(web_main.security, 'configure', lambda *args, **kwargs: None)
    monkeypatch.setattr(web_main.auth, 'configure', lambda *args, **kwargs: None)
    monkeypatch.setattr(web_main.security, 'reset', lambda: None)
    monkeypatch.setattr(web_main.auth, 'reset', lambda: None)
    monkeypatch.setattr(web_main.browser_extension, 'reset', lambda: None)

    with pytest.raises(RuntimeError, match='application launch failed'):
        await web_main.on_startup()

    assert events.index('app.launch') < events.index('app.exit')
    assert events.index('app.exit') < events.index('notifications.close')


@pytest.mark.asyncio
async def test_shutdown_stops_password_admission_before_application_cleanup(
    monkeypatch,
) -> None:
    events = []
    password_work = PasswordWorkProbe(events)
    active_media = ActiveMediaProbe(events)
    dispatcher = NotificationDispatcherProbe(events)

    async def realtime_stop() -> None:
        events.append('realtime.stop')

    async def app_exit() -> None:
        events.append('app.exit')

    async def runtime_close() -> None:
        events.append('runtime.close')

    monkeypatch.setattr(web_main, '_password_work_coordinator', password_work)
    monkeypatch.setattr(web_main, '_active_media_service', active_media)
    monkeypatch.setattr(web_main, '_notification_dispatcher', dispatcher)
    monkeypatch.setattr(
        web_main,
        '_admin_auth_store',
        SimpleNamespace(close=lambda: events.append('store.close')),
    )
    monkeypatch.setattr(web_main._realtime_sampler, 'stop', realtime_stop)
    monkeypatch.setattr(web_main, 'app', SimpleNamespace(exit=app_exit))
    monkeypatch.setattr(
        web_main,
        '_settings',
        SimpleNamespace(dump=lambda: events.append('settings.dump')),
    )
    monkeypatch.setattr(
        web_main, '_bili_account_runtime', SimpleNamespace(close=runtime_close)
    )
    monkeypatch.setattr(web_main.browser_extension, 'reset', lambda: None)
    monkeypatch.setattr(
        web_main.security, 'reset', lambda: events.append('security.reset')
    )
    monkeypatch.setattr(web_main.auth, 'reset', lambda: events.append('auth.reset'))

    await web_main.on_shuntdown()

    assert events.index('password.close_admission') < events.index('realtime.stop')
    assert events.index('active_media.close_admission') < events.index('realtime.stop')
    assert events.index('auth.reset') < events.index('realtime.stop')
    assert events.index('security.reset') < events.index('realtime.stop')
    assert events.index('password.shutdown') > events.index('runtime.close')
    assert events.index('active_media.shutdown') > events.index('runtime.close')
    assert events.index('notifications.close') > events.index('runtime.close')
    assert events.index('store.close') > events.index('password.shutdown')
