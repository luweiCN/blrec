import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping
from unittest.mock import AsyncMock

import pytest

from blrec.bili_upload.database import BiliUploadDatabase
from blrec.bili_upload.journal import RecordingJournalBridge
from blrec.bili_upload.runtime import BiliAccountRuntime
from blrec.setting.models import BiliUploadSettings


class FakeClock:
    def __init__(self, value: int = 1_000_000) -> None:
        self.value = value

    def __call__(self) -> float:
        return float(self.value)


def confirmed_response() -> Mapping[str, Any]:
    return {
        'code': 0,
        'data': {
            'token_info': {
                'access_token': 'access-new',
                'refresh_token': 'refresh-new',
                'mid': 42,
                'expires_in': 180 * 24 * 3600,
            },
            'cookie_info': {
                'cookies': [
                    {'name': 'DedeUserID', 'value': '42'},
                    {'name': 'SESSDATA', 'value': 'sess-secret', 'http_only': 1},
                    {'name': 'bili_jct', 'value': 'csrf-secret'},
                ]
            },
        },
    }


class IdentityProtocol:
    def __init__(self) -> None:
        self.oauth_calls = 0

    async def oauth_info(self, _bundle: Any) -> Mapping[str, Any]:
        self.oauth_calls += 1
        return {'code': 0, 'data': {'mid': 42, 'refresh': False}}

    async def web_nav(self, _bundle: Any) -> Mapping[str, Any]:
        return {'code': 0, 'data': {'isLogin': True, 'mid': 42, 'uname': 'fixture'}}


@pytest.mark.asyncio
async def test_runtime_requires_credential_key_without_creating_database(
    tmp_path: Path,
) -> None:
    runtime = BiliAccountRuntime(
        BiliUploadSettings(database_path=str(tmp_path / 'unused.sqlite3')),
        api_key=None,
        credential_key=None,
    )

    assert not await runtime.start()
    assert runtime.manager is None
    assert runtime.journal is None
    assert runtime.unavailable_reason == 'credential key is required'
    assert not (tmp_path / 'unused.sqlite3').exists()
    assert await runtime.primary_cookie_header('https://api.bilibili.com/') is None


@pytest.mark.asyncio
async def test_runtime_starts_without_api_key(tmp_path: Path) -> None:
    runtime = BiliAccountRuntime(
        BiliUploadSettings(database_path=str(tmp_path / 'blrec.sqlite3')),
        api_key=None,
        credential_key=b'k' * 32,
        protocol=IdentityProtocol(),
    )

    try:
        assert await runtime.start()
        assert runtime.manager is not None
        assert runtime.unavailable_reason is None
        assert (tmp_path / 'blrec.sqlite3').exists()
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_enabled_runtime_starts_manager_and_periodic_health_check(
    tmp_path: Path,
) -> None:
    protocol = IdentityProtocol()
    clock = FakeClock()
    settings = BiliUploadSettings(database_path=str(tmp_path / 'blrec.sqlite3'))
    seed_runtime = BiliAccountRuntime(
        settings,
        api_key='test-api-key',
        credential_key=b'k' * 32,
        protocol=protocol,
        clock=clock,
    )
    assert await seed_runtime.start()
    assert seed_runtime.manager is not None
    await seed_runtime.manager.finish_confirmed_login(confirmed_response())
    await seed_runtime.close()
    protocol.oauth_calls = 0

    runtime = BiliAccountRuntime(
        settings,
        api_key='test-api-key',
        credential_key=b'k' * 32,
        protocol=protocol,
        clock=clock,
        refresh_interval_seconds=0.01,
    )
    try:
        assert await runtime.start()
        assert runtime.manager is not None
        assert runtime.journal is not None
        assert runtime.coordinator is not None
        assert runtime.policy_manager is not None
        assert runtime.category_catalog is not None
        assert runtime.cover_library is not None
        assert runtime.cover_resolver is not None
        assert runtime.collection_manager is not None
        assert runtime.collection_publisher is not None
        assert runtime.review_watcher is not None
        assert runtime.comment_planner is not None
        assert runtime.comment_publisher is not None
        assert runtime.danmaku_importer is not None
        assert runtime.danmaku_publisher is not None
        assert runtime.task_actions is not None
        assert runtime.highlight_service is not None
        assert runtime.highlight_worker is not None
        assert runtime.media_index_worker is not None

        for _ in range(100):
            if protocol.oauth_calls:
                break
            await asyncio.sleep(0.01)

        assert protocol.oauth_calls == 1
        assert runtime.unavailable_reason is None
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_runtime_close_is_idempotent(tmp_path: Path) -> None:
    runtime = BiliAccountRuntime(
        BiliUploadSettings(database_path=str(tmp_path / 'blrec.sqlite3')),
        api_key='test-api-key',
        credential_key=b'k' * 32,
        protocol=IdentityProtocol(),
    )

    assert await runtime.start()
    await runtime.close()
    await runtime.close()

    assert runtime.manager is None
    assert runtime.coordinator is None
    assert runtime.policy_manager is None
    assert runtime.category_catalog is None
    assert runtime.cover_library is None
    assert runtime.cover_resolver is None
    assert runtime.collection_manager is None
    assert runtime.collection_publisher is None
    assert runtime.review_watcher is None
    assert runtime.comment_planner is None
    assert runtime.comment_publisher is None
    assert runtime.danmaku_importer is None
    assert runtime.danmaku_publisher is None
    assert runtime.task_actions is None
    assert runtime.highlight_service is None
    assert runtime.highlight_worker is None
    assert runtime.media_index_worker is None


@pytest.mark.asyncio
async def test_runtime_reconciles_crash_interrupted_recording_before_use(
    tmp_path: Path,
) -> None:
    source = tmp_path / 'interrupted.flv'
    source.write_bytes(b'partial recording')
    database_path = str(tmp_path / 'blrec.sqlite3')
    database = BiliUploadDatabase(database_path)
    await database.open()
    journal = RecordingJournalBridge(database, clock=lambda: 1_000)
    run_id = await journal.recording_started(100, live_start_time=900)
    await journal.video_created(run_id, str(source), record_start_time=901)
    await database.close()

    runtime = BiliAccountRuntime(
        BiliUploadSettings(database_path=database_path),
        api_key='test-api-key',
        credential_key=b'k' * 32,
        protocol=IdentityProtocol(),
        clock=lambda: 2_000,
    )
    try:
        assert await runtime.start()
        assert runtime.journal is not None
        session = await runtime.journal.session_for_run(run_id)
        assert session.state == 'cancelled'
        assert session.parts[0].artifact_state == 'failed'
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_upload_loop_finalizes_cancelled_sessions_before_job_creation(
    tmp_path: Path,
) -> None:
    runtime = BiliAccountRuntime(
        BiliUploadSettings(database_path=str(tmp_path / 'unused.sqlite3')),
        api_key=None,
        credential_key=None,
    )
    journal = SimpleNamespace(finalize_cancelled_sessions=AsyncMock(return_value=1))
    coordinator = SimpleNamespace(
        resolve_finished_sessions=AsyncMock(return_value=[1]),
        prepare_waiting_jobs=AsyncMock(return_value=[1]),
        run_once=AsyncMock(return_value=None),
    )
    review_watcher = SimpleNamespace(run_once=AsyncMock(return_value=None))
    comment_publisher = SimpleNamespace(run_once=AsyncMock(return_value=None))
    danmaku_importer = SimpleNamespace(run_once=AsyncMock(return_value=None))
    stop_event = asyncio.Event()

    async def stop_after_iteration() -> None:
        stop_event.set()

    danmaku_publisher = SimpleNamespace(
        run_once=AsyncMock(side_effect=stop_after_iteration)
    )

    await runtime._run_uploads(
        journal,
        coordinator,
        review_watcher,
        comment_publisher,
        danmaku_importer,
        danmaku_publisher,
        stop_event,
    )

    journal.finalize_cancelled_sessions.assert_awaited_once_with()
    coordinator.resolve_finished_sessions.assert_awaited_once_with()
    coordinator.prepare_waiting_jobs.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_runtime_exposes_primary_cookie_and_forwards_auth_failures(
    tmp_path: Path,
) -> None:
    changed = AsyncMock()
    runtime = BiliAccountRuntime(
        BiliUploadSettings(database_path=str(tmp_path / 'blrec.sqlite3')),
        api_key='test-api-key',
        credential_key=b'k' * 32,
        protocol=IdentityProtocol(),
        on_primary_credential_changed=changed,
    )
    try:
        assert await runtime.start()
        assert runtime.manager is not None
        await runtime.manager.finish_confirmed_login(confirmed_response())

        header = await runtime.primary_cookie_header(
            'https://api.live.bilibili.com/x/test'
        )
        assert 'SESSDATA=sess-secret' in header
        changed.assert_awaited_once_with()

        runtime.manager.report_primary_auth_failure = AsyncMock()
        await runtime.report_primary_auth_failure()
        runtime.manager.report_primary_auth_failure.assert_awaited_once_with()
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_delete_open_session_stops_workers_and_current_recording() -> None:
    calls = []
    runtime = object.__new__(BiliAccountRuntime)
    runtime._session_action_lock = asyncio.Lock()
    runtime._database = SimpleNamespace(
        fetchone=AsyncMock(
            return_value={'room_id': 100, 'state': 'open', 'job_id': None}
        )
    )
    runtime._task_actions = SimpleNamespace(
        delete_session=AsyncMock(return_value='已删除')
    )
    runtime._session_submission_manager = SimpleNamespace()
    runtime._active_session_canceller = AsyncMock(
        side_effect=lambda room_id: calls.append(('cancel', room_id))
    )
    runtime._stop_upload_worker = AsyncMock(
        side_effect=lambda: calls.append(('stop_worker', None))
    )
    runtime._start_upload_worker = AsyncMock(
        side_effect=lambda: calls.append(('start_worker', None))
    )

    message = await runtime.run_recording_session_action(
        'delete_local', 7, manager_subject='manager'
    )

    assert message == '已删除'
    assert calls == [('stop_worker', None), ('cancel', 100), ('start_worker', None)]
    runtime._task_actions.delete_session.assert_awaited_once_with(
        7, manager_subject='manager'
    )


@pytest.mark.asyncio
async def test_session_action_maps_job_capability_to_job_id() -> None:
    runtime = object.__new__(BiliAccountRuntime)
    runtime._session_action_lock = asyncio.Lock()
    runtime._database = SimpleNamespace(
        fetchone=AsyncMock(
            return_value={'room_id': 100, 'state': 'closed', 'job_id': 9}
        )
    )
    runtime._task_actions = SimpleNamespace(
        retry_failed=AsyncMock(return_value='已重新排队')
    )
    runtime._session_submission_manager = SimpleNamespace()

    message = await runtime.run_recording_session_action(
        'retry_failed', 7, manager_subject='manager'
    )

    assert message == '已重新排队'
    runtime._task_actions.retry_failed.assert_awaited_once_with(
        9, manager_subject='manager'
    )


@pytest.mark.asyncio
async def test_session_action_maps_manual_danmaku_backfill_to_job_id() -> None:
    runtime = object.__new__(BiliAccountRuntime)
    runtime._session_action_lock = asyncio.Lock()
    runtime._database = SimpleNamespace(
        fetchone=AsyncMock(
            return_value={'room_id': 100, 'state': 'closed', 'job_id': 9}
        )
    )
    runtime._task_actions = SimpleNamespace(
        request_danmaku_backfill=AsyncMock(return_value='已排队回灌弹幕')
    )
    runtime._session_submission_manager = SimpleNamespace()

    message = await runtime.run_recording_session_action(
        'backfill_danmaku', 7, manager_subject='manager'
    )

    assert message == '已排队回灌弹幕'
    runtime._task_actions.request_danmaku_backfill.assert_awaited_once_with(
        9, manager_subject='manager'
    )


@pytest.mark.asyncio
async def test_create_highlight_upload_task_pauses_worker_around_draft_creation() -> (
    None
):
    calls = []
    runtime = object.__new__(BiliAccountRuntime)
    runtime._session_action_lock = asyncio.Lock()
    runtime._highlight_service = SimpleNamespace(
        ensure_upload_session=AsyncMock(
            side_effect=lambda clip_id: calls.append(('session', clip_id)) or 12
        )
    )
    runtime._coordinator = SimpleNamespace(
        create_highlight_job=AsyncMock(
            side_effect=lambda session_id: calls.append(('job', session_id)) or 9
        )
    )
    runtime._stop_upload_worker = AsyncMock(
        side_effect=lambda: calls.append(('stop', None))
    )
    runtime._start_upload_worker = AsyncMock(
        side_effect=lambda: calls.append(('start', None))
    )

    job_id = await runtime.create_highlight_upload_task(
        3, manager_subject='administrator'
    )

    assert job_id == 9
    assert calls == [('stop', None), ('session', 3), ('job', 12), ('start', None)]
