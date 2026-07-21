import asyncio
import struct
import threading
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping
from unittest.mock import AsyncMock

import pytest

from blrec.bili_upload.covers import CoverLibrary
from blrec.bili_upload.database import BiliUploadDatabase
from blrec.bili_upload.highlight_worker import HighlightWorker
from blrec.bili_upload.journal import RecordingJournalBridge
from blrec.bili_upload.policies import default_room_upload_policy
from blrec.bili_upload.recording_content import RecordingContentReader
from blrec.bili_upload.runtime import BiliAccountRuntime
from blrec.control.operations import ControlOperationJournal, ControlStepInput
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


def cover_png() -> bytes:
    return (
        b'\x89PNG\r\n\x1a\n'
        + struct.pack('>I', 13)
        + b'IHDR'
        + struct.pack('>II', 1600, 1000)
        + b'\x08\x02\x00\x00\x00'
        + b'\x00\x00\x00\x00'
    )


class IdentityProtocol:
    def __init__(self) -> None:
        self.oauth_calls = 0

    async def oauth_info(self, _bundle: Any) -> Mapping[str, Any]:
        self.oauth_calls += 1
        return {'code': 0, 'data': {'mid': 42, 'refresh': False}}

    async def web_nav(self, _bundle: Any) -> Mapping[str, Any]:
        return {'code': 0, 'data': {'isLogin': True, 'mid': 42, 'uname': 'fixture'}}


async def open_danmaku_stream(
    database: BiliUploadDatabase, reader: RecordingContentReader, tmp_path: Path
) -> Any:
    source = tmp_path / 'runtime-part.flv'
    source.write_bytes(b'video')
    xml = tmp_path / 'runtime-part.xml'
    xml.write_text('<i><d p="1,1,25,1">弹幕</d>', encoding='utf8')
    journal = RecordingJournalBridge(database, clock=lambda: 1_000)
    run_id = await journal.recording_started(100, live_start_time=900)
    await journal.video_created(run_id, str(source), record_start_time=901)
    part_id = (await journal.parts_for_run(run_id))[0].id
    await database.execute(
        'UPDATE recording_parts SET xml_path=?,xml_completed=0 WHERE id=?',
        (str(xml), part_id),
    )
    await reader.danmaku(part_id, cursor=0, limit=1)
    return next(iter(reader._danmaku_streams.values()))


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
async def test_runtime_backfills_highlight_sizes_after_recovery_before_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events = []

    async def recover(_worker: HighlightWorker) -> int:
        events.append(('recover', None))
        return 0

    async def backfill(_worker: HighlightWorker, limit: int = 100) -> int:
        events.append(('backfill', limit))
        return 0

    async def run_once(_worker: HighlightWorker):
        events.append(('run_once', None))
        return None

    monkeypatch.setattr(HighlightWorker, 'recover_interrupted', recover)
    monkeypatch.setattr(HighlightWorker, 'backfill_file_sizes', backfill)
    monkeypatch.setattr(HighlightWorker, 'run_once', run_once)
    runtime = BiliAccountRuntime(
        BiliUploadSettings(database_path=str(tmp_path / 'blrec.sqlite3')),
        api_key=None,
        credential_key=b'k' * 32,
        protocol=IdentityProtocol(),
    )

    try:
        assert await runtime.start()
        await asyncio.sleep(0)
        assert events[:3] == [('recover', None), ('backfill', 100), ('run_once', None)]
        assert events.count(('backfill', 100)) == 1
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
        assert runtime.deletion_worker is not None

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
    assert runtime.deletion_worker is None


@pytest.mark.asyncio
async def test_runtime_close_waits_for_and_closes_content_reader_files(
    tmp_path: Path,
) -> None:
    runtime = BiliAccountRuntime(
        BiliUploadSettings(database_path=str(tmp_path / 'blrec.sqlite3')),
        api_key='test-api-key',
        credential_key=b'k' * 32,
        protocol=IdentityProtocol(),
    )
    assert await runtime.start()
    assert runtime._database is not None
    assert runtime.content_reader is not None
    stream = await open_danmaku_stream(
        runtime._database, runtime.content_reader, tmp_path
    )
    locked = threading.Event()
    release = threading.Event()

    def hold_stream() -> None:
        with stream.lock:
            locked.set()
            assert release.wait(2)

    holder = threading.Thread(target=hold_stream)
    holder.start()
    assert locked.wait(2)
    timer = threading.Timer(0.05, release.set)
    timer.start()

    await runtime.close()
    holder.join(timeout=2)
    timer.cancel()

    assert not holder.is_alive()
    assert stream.file.closed is True


@pytest.mark.asyncio
async def test_runtime_partial_close_closes_content_reader_files(
    tmp_path: Path,
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'blrec.sqlite3'))
    await database.open()
    runtime = BiliAccountRuntime(
        BiliUploadSettings(database_path=str(tmp_path / 'blrec.sqlite3')),
        api_key='test-api-key',
        credential_key=b'k' * 32,
    )
    reader = RecordingContentReader(database)
    runtime._content_reader = reader
    stream = await open_danmaku_stream(database, reader, tmp_path)

    await runtime._close_partial(database)

    assert stream.file.closed is True


@pytest.mark.asyncio
async def test_runtime_close_stops_cover_admission_and_drains_admitted_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = BiliAccountRuntime(
        BiliUploadSettings(database_path=str(tmp_path / 'blrec.sqlite3')),
        api_key='test-api-key',
        credential_key=b'k' * 32,
        protocol=IdentityProtocol(),
    )
    loop = asyncio.get_running_loop()
    inspection_started = asyncio.Event()
    release_inspection = threading.Event()
    original_inspection = CoverLibrary._inspect_content

    def blocking_inspection(content: bytes) -> Any:
        loop.call_soon_threadsafe(inspection_started.set)
        assert release_inspection.wait(5)
        return original_inspection(content)

    monkeypatch.setattr(
        CoverLibrary, '_inspect_content', staticmethod(blocking_inspection)
    )
    assert await runtime.start()
    await runtime._stop_deletion_worker()
    await runtime._stop_media_index_worker()
    await runtime._stop_highlight_worker()
    await runtime._stop_upload_worker()
    library = runtime.cover_library
    assert library is not None
    addition = asyncio.create_task(library.add(cover_png(), 'cover.png'))
    await asyncio.wait_for(inspection_started.wait(), timeout=5)
    closing = asyncio.create_task(runtime.close())
    try:
        await asyncio.sleep(0)
        assert not closing.done()
    finally:
        release_inspection.set()

    asset = await addition
    await closing
    assert asset.filename == 'cover.png'
    with pytest.raises(RuntimeError, match='cover work coordinator is closed'):
        await library.add(cover_png(), 'other.png')


@pytest.mark.asyncio
async def test_concurrent_runtime_close_calls_share_cover_drain_before_database_close(
    tmp_path: Path,
) -> None:
    runtime = BiliAccountRuntime(
        BiliUploadSettings(database_path=str(tmp_path / 'blrec.sqlite3')),
        api_key='test-api-key',
        credential_key=b'k' * 32,
    )
    drain_started = asyncio.Event()
    release_drain = asyncio.Event()
    database_close_started = asyncio.Event()
    database_close_calls = 0

    class BlockingCoverLibrary:
        def close_admission(self) -> None:
            pass

        async def shutdown(self) -> None:
            drain_started.set()
            await release_drain.wait()

    class ObservedDatabase:
        async def close(self) -> None:
            nonlocal database_close_calls
            database_close_calls += 1
            database_close_started.set()

    runtime._cover_library = BlockingCoverLibrary()  # type: ignore[assignment]
    runtime._database = ObservedDatabase()  # type: ignore[assignment]
    first_close = asyncio.create_task(runtime.close())
    await asyncio.wait_for(drain_started.wait(), timeout=5)
    second_close = asyncio.create_task(runtime.close())
    await asyncio.sleep(0)

    assert not first_close.done()
    assert not second_close.done()
    assert not database_close_started.is_set()

    release_drain.set()
    await asyncio.gather(first_close, second_close)
    assert database_close_calls == 1


@pytest.mark.asyncio
async def test_runtime_awaits_cover_resolver_before_database_close(
    tmp_path: Path,
) -> None:
    runtime = BiliAccountRuntime(
        BiliUploadSettings(database_path=str(tmp_path / 'blrec.sqlite3')),
        api_key='test-api-key',
        credential_key=b'k' * 32,
    )
    events = []

    class ObservedResolver:
        async def close(self) -> None:
            events.append('resolver_started')
            await asyncio.sleep(0)
            events.append('resolver_closed')

    class ObservedDatabase:
        async def close(self) -> None:
            events.append('database_closed')

    runtime._cover_resolver = ObservedResolver()  # type: ignore[assignment]
    runtime._database = ObservedDatabase()  # type: ignore[assignment]

    await runtime.close()

    assert events == ['resolver_started', 'resolver_closed', 'database_closed']


@pytest.mark.asyncio
async def test_runtime_closes_each_successive_start_generation(tmp_path: Path) -> None:
    runtime = BiliAccountRuntime(
        BiliUploadSettings(database_path=str(tmp_path / 'blrec.sqlite3')),
        api_key='test-api-key',
        credential_key=b'k' * 32,
        protocol=IdentityProtocol(),
    )

    assert await runtime.start()
    first_library = runtime.cover_library
    assert first_library is not None
    await runtime.close()
    assert first_library._work._executor_closed is True

    assert await runtime.start()
    second_library = runtime.cover_library
    assert second_library is not None
    assert second_library is not first_library
    await asyncio.gather(runtime.close(), runtime.close())

    assert second_library._work._executor_closed is True
    assert runtime.manager is None
    assert runtime._database is None


@pytest.mark.asyncio
async def test_stopping_upload_worker_keeps_stop_event_visible_until_exit(
    tmp_path: Path,
) -> None:
    runtime = BiliAccountRuntime(
        BiliUploadSettings(database_path=str(tmp_path / 'blrec.sqlite3')),
        api_key='test-api-key',
        credential_key=b'k' * 32,
    )
    stop_event = asyncio.Event()
    observed = asyncio.Event()

    async def worker() -> None:
        await stop_event.wait()
        assert runtime._upload_stop_event is stop_event
        observed.set()

    runtime._upload_stop_event = stop_event
    runtime._upload_task = asyncio.create_task(worker())

    await runtime._stop_upload_worker()

    assert observed.is_set()
    assert runtime._upload_stop_event is None
    assert runtime._upload_task is None


@pytest.mark.asyncio
async def test_deleting_highlight_only_queues_local_deletion(tmp_path: Path) -> None:
    runtime = BiliAccountRuntime(
        BiliUploadSettings(database_path=str(tmp_path / 'blrec.sqlite3')),
        api_key='test-api-key',
        credential_key=b'k' * 32,
    )
    events = []
    runtime._highlight_service = SimpleNamespace()  # type: ignore[assignment]
    runtime._deletion_worker = SimpleNamespace(
        request_clip=AsyncMock(
            side_effect=lambda _clip_id: events.append('request') or 3
        )
    )
    runtime._stop_upload_worker = AsyncMock(  # type: ignore[method-assign]
        side_effect=lambda: events.append('stop_upload')
    )
    runtime._stop_highlight_worker = AsyncMock(  # type: ignore[method-assign]
        side_effect=lambda: events.append('stop_clip')
    )
    runtime._start_highlight_worker = AsyncMock(  # type: ignore[method-assign]
        side_effect=lambda: events.append('start_clip')
    )
    runtime._start_upload_worker = AsyncMock(  # type: ignore[method-assign]
        side_effect=lambda: events.append('start_upload')
    )

    result = await runtime.delete_highlight_clip(7)

    assert result == 'queued'
    assert events == ['request']
    runtime._stop_upload_worker.assert_not_awaited()
    runtime._stop_highlight_worker.assert_not_awaited()
    runtime._start_highlight_worker.assert_not_awaited()
    runtime._start_upload_worker.assert_not_awaited()


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
        sync_live_sessions=AsyncMock(return_value=[1]),
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
    coordinator.sync_live_sessions.assert_awaited_once_with()
    coordinator.prepare_waiting_jobs.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_upload_loop_runs_one_retry_quantum_per_iteration() -> None:
    runtime = BiliAccountRuntime(
        BiliUploadSettings(database_path='/unused.sqlite3'),
        api_key=None,
        credential_key=None,
    )
    journal = SimpleNamespace(finalize_cancelled_sessions=AsyncMock(return_value=0))
    coordinator = SimpleNamespace(
        sync_live_sessions=AsyncMock(return_value=[]),
        prepare_waiting_jobs=AsyncMock(return_value=[]),
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
    task_actions = SimpleNamespace(
        run_retry_batch_once=AsyncMock(return_value='retry-operation'),
        run_once=AsyncMock(return_value=None),
    )

    await runtime._run_uploads(
        journal,
        coordinator,
        review_watcher,
        comment_publisher,
        danmaku_importer,
        danmaku_publisher,
        stop_event,
        task_actions=task_actions,
    )

    task_actions.run_retry_batch_once.assert_awaited_once_with()
    task_actions.run_once.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_retry_admission_wakeup_interrupts_idle_upload_delay() -> None:
    runtime = BiliAccountRuntime(
        BiliUploadSettings(database_path='/unused.sqlite3'),
        api_key=None,
        credential_key=None,
    )
    stop_event = asyncio.Event()
    runtime._upload_stop_event = stop_event
    runtime._upload_wake_event = asyncio.Event()

    runtime._wake_upload_worker()
    await asyncio.wait_for(runtime._wait_for_upload_delay(stop_event, 60), timeout=0.1)

    assert not runtime._upload_wake_event.is_set()


@pytest.mark.asyncio
async def test_runtime_opens_shared_retry_journal_without_owning_its_close(
    tmp_path: Path,
) -> None:
    control_journal = ControlOperationJournal(tmp_path / 'control.sqlite3')
    runtime = BiliAccountRuntime(
        BiliUploadSettings(database_path=str(tmp_path / 'blrec.sqlite3')),
        api_key=None,
        credential_key=b'k' * 32,
        protocol=IdentityProtocol(),
        control_operation_journal=control_journal,
    )

    assert await runtime.start()
    try:
        assert runtime.task_actions is not None
        assert runtime.task_actions._control_journal is control_journal
    finally:
        await runtime.close()

    operation = await control_journal.admit(
        lane='test',
        kind='still-open',
        target_key='one',
        steps=(ControlStepInput(key='one'),),
    )
    assert operation.status == 'accepted'
    await control_journal.close()


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
        await runtime.report_primary_auth_failure('credential-fingerprint')
        runtime.manager.report_primary_auth_failure.assert_awaited_once_with(
            'credential-fingerprint'
        )
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_delete_open_session_only_queues_intent() -> None:
    calls = []
    runtime = object.__new__(BiliAccountRuntime)
    runtime._session_action_lock = asyncio.Lock()
    runtime._database = SimpleNamespace(
        fetchone=AsyncMock(
            return_value={'room_id': 100, 'state': 'open', 'job_id': None}
        )
    )
    runtime._task_actions = SimpleNamespace()
    runtime._deletion_worker = SimpleNamespace(
        request_session=AsyncMock(
            side_effect=lambda session_id, manager_subject: calls.append(
                ('request', session_id, manager_subject)
            )
            or 4
        )
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

    assert message == '已排队删除本地场次及文件'
    assert calls == [('request', 7, 'manager')]
    runtime._deletion_worker.request_session.assert_awaited_once_with(
        7, manager_subject='manager'
    )
    runtime._active_session_canceller.assert_not_awaited()
    runtime._stop_upload_worker.assert_not_awaited()
    runtime._start_upload_worker.assert_not_awaited()


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
async def test_session_batch_delegates_once_to_the_transactional_manager() -> None:
    runtime = object.__new__(BiliAccountRuntime)
    expected = (SimpleNamespace(target_id=7, accepted=True, message='已设置'),)
    runtime._task_actions = SimpleNamespace(
        run_session_batch=AsyncMock(return_value=expected)
    )

    result = await runtime.run_recording_session_batch(
        'set_upload', (7, 8), manager_subject='manager'
    )

    assert result == expected
    runtime._task_actions.run_session_batch.assert_awaited_once_with(
        'set_upload', (7, 8), manager_subject='manager'
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
async def test_create_highlight_upload_task_does_not_interrupt_active_upload() -> None:
    calls = []
    runtime = object.__new__(BiliAccountRuntime)
    runtime._session_action_lock = asyncio.Lock()
    runtime._highlight_service = SimpleNamespace(
        get_clip=AsyncMock(return_value=SimpleNamespace(room_id=100)),
        ensure_upload_session=AsyncMock(
            side_effect=lambda clip_id: calls.append(('session', clip_id)) or 12
        ),
    )
    runtime._coordinator = SimpleNamespace(
        create_highlight_job=AsyncMock(
            side_effect=lambda session_id: calls.append(('job', session_id)) or 9
        )
    )
    runtime._policy_manager = SimpleNamespace(validate=AsyncMock())
    runtime._category_catalog = SimpleNamespace(
        list=AsyncMock(
            return_value=SimpleNamespace(
                categories=(SimpleNamespace(children=(SimpleNamespace(id=21),)),),
                creation_statements=(SimpleNamespace(id=-2),),
            )
        )
    )
    runtime._session_submission_manager = SimpleNamespace(
        save_override=AsyncMock(
            side_effect=lambda session_id, settings, manager_subject: calls.append(
                ('settings', session_id)
            )
        )
    )
    runtime._stop_upload_worker = AsyncMock(
        side_effect=lambda: calls.append(('stop', None))
    )
    runtime._start_upload_worker = AsyncMock(
        side_effect=lambda: calls.append(('start', None))
    )

    settings = replace(
        default_room_upload_policy(),
        title_template='最终投稿标题',
        part_title_template='不应保留的分 P 模板',
        retention_mode='submitted',
        retention_days=5,
    )
    job_id = await runtime.create_highlight_upload_task(
        3, settings=settings, manager_subject='administrator'
    )

    assert job_id == 9
    assert calls == [('session', 3), ('settings', 12), ('job', 12)]
    normalized = runtime._session_submission_manager.save_override.await_args.args[1]
    assert normalized.title_template == '最终投稿标题'
    assert normalized.part_title_template == '最终投稿标题'
    assert normalized.retention_mode == 'never'
    assert normalized.retention_days == 0
    runtime._policy_manager.validate.assert_awaited_once_with(100, normalized)
    runtime._stop_upload_worker.assert_not_awaited()
    runtime._start_upload_worker.assert_not_awaited()
