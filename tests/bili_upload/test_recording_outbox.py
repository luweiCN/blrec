import asyncio
import os
import sqlite3
import stat
import threading
from pathlib import Path
from typing import AsyncIterator, Iterator, List, Optional

import pytest
import pytest_asyncio

from blrec.bili_upload.artifact_recovery import RecoveredArtifact
from blrec.bili_upload.database import BiliUploadDatabase
from blrec.bili_upload.journal import RecordingJournalBridge, RecordingSessionMetadata
from blrec.bili_upload.recording_outbox import (
    LocalRecordingOutbox,
    RecordingOutboxConsistencyError,
    RecordingOutboxError,
    RecordingOutboxEvent,
    RecordingOutboxRuntime,
    RecordingOutboxSynchronizer,
)


@pytest_asyncio.fixture
async def outbox(tmp_path: Path) -> AsyncIterator[LocalRecordingOutbox]:
    value = LocalRecordingOutbox(tmp_path / 'recording-journal.sqlite3')
    await value.open()
    try:
        yield value
    finally:
        await value.close()


@pytest_asyncio.fixture
async def database(tmp_path: Path) -> AsyncIterator[BiliUploadDatabase]:
    value = BiliUploadDatabase(str(tmp_path / 'blrec.sqlite3'))
    await value.open()
    try:
        yield value
    finally:
        await value.close()


@pytest.mark.asyncio
async def test_open_configures_durable_private_sqlite(tmp_path: Path) -> None:
    path = tmp_path / 'recording-journal.sqlite3'
    outbox = LocalRecordingOutbox(path)

    await outbox.open()
    try:
        assert await outbox.pragma('journal_mode') == 'wal'
        assert await outbox.pragma('synchronous') == 2
        assert await outbox.pragma('foreign_keys') == 1
        assert await outbox.pragma('quick_check') == 'ok'
        assert await outbox.pragma('application_id') == outbox.APPLICATION_ID
        assert await outbox.pragma('user_version') == outbox.SCHEMA_VERSION
        assert stat.S_IMODE(path.stat().st_mode) == 0o600

        await outbox.append_event(
            RecordingOutboxEvent(
                event_id='event-1',
                event_type='recording_started',
                run_id='run-1',
                room_id=100,
                path=None,
                payload={'live_start_time': 900, 'metadata': None},
                occurred_at=1_000.25,
            )
        )
        for suffix in ('-wal', '-shm'):
            sidecar = Path(str(path) + suffix)
            if sidecar.exists():
                assert stat.S_IMODE(sidecar.stat().st_mode) == 0o600
    finally:
        await outbox.close()


@pytest.mark.asyncio
async def test_append_is_ordered_idempotent_and_rejects_conflict(
    outbox: LocalRecordingOutbox,
) -> None:
    started = RecordingOutboxEvent(
        event_id='event-started',
        event_type='recording_started',
        run_id='run-1',
        room_id=100,
        path=None,
        payload={'live_start_time': 900, 'metadata': None},
        occurred_at=1_000.25,
    )
    created = RecordingOutboxEvent(
        event_id='event-created',
        event_type='video_created',
        run_id='run-1',
        room_id=100,
        path='/rec/p1.flv',
        payload={'record_start_time': 901, 'timeline_start_at_ms': 1_001_500},
        occurred_at=1_001.5,
    )

    persisted_started = await outbox.append_event(started)
    replayed_started = await outbox.append_event(started)
    persisted_created = await outbox.append_event(created)

    assert persisted_started.sequence == 1
    assert replayed_started == persisted_started
    assert persisted_created.sequence == 2
    assert [event.event_id for event in await outbox.pending_events(limit=10)] == [
        'event-started',
        'event-created',
    ]

    with pytest.raises(RecordingOutboxConsistencyError, match='conflicting content'):
        await outbox.append_event(
            RecordingOutboxEvent(
                event_id='event-started',
                event_type='recording_finished',
                run_id='run-1',
                room_id=100,
                path=None,
                payload={},
                occurred_at=1_002,
            )
        )


@pytest.mark.asyncio
async def test_lifecycle_payload_and_source_binding_survive_restart(
    tmp_path: Path,
) -> None:
    path = tmp_path / 'recording-journal.sqlite3'
    values: Iterator[str] = iter(('run-1', 'event-started', 'event-created'))
    outbox = LocalRecordingOutbox(
        path, clock=lambda: 1_000.25, uuid_factory=lambda: next(values)
    )
    await outbox.open()
    run_id = await outbox.recording_started(
        100,
        live_start_time=900,
        metadata=RecordingSessionMetadata(
            title='测试直播',
            cover_url='https://example.invalid/cover.jpg',
            anchor_uid=42,
            anchor_name='主播',
            area_id=1,
            area_name='分区',
            parent_area_id=2,
            parent_area_name='父分区',
        ),
    )
    await outbox.video_created(run_id, '/rec/p1.flv', record_start_time=901)
    await outbox.close()

    reopened = LocalRecordingOutbox(path)
    await reopened.open()
    try:
        events = await reopened.pending_events(limit=10)
        assert run_id == 'run-1'
        assert events[0].payload == {
            'live_start_time': 900,
            'metadata': {
                'anchor_name': '主播',
                'anchor_uid': 42,
                'area_id': 1,
                'area_name': '分区',
                'cover_url': 'https://example.invalid/cover.jpg',
                'parent_area_id': 2,
                'parent_area_name': '父分区',
                'title': '测试直播',
            },
        }
        assert events[1].payload == {
            'record_start_time': 901,
            'timeline_start_at_ms': 1_000_250,
        }
        assert await reopened.run_id_for_source('/rec/p1.flv') == 'run-1'
    finally:
        await reopened.close()


@pytest.mark.asyncio
async def test_attempt_and_ack_status_only_ack_exact_event(
    outbox: LocalRecordingOutbox,
) -> None:
    first = await outbox.append_event(
        RecordingOutboxEvent(
            event_id='event-1',
            event_type='recording_started',
            run_id='run-1',
            room_id=100,
            path=None,
            payload={'live_start_time': 900, 'metadata': None},
            occurred_at=1_000,
        )
    )
    await outbox.append_event(
        RecordingOutboxEvent(
            event_id='event-2',
            event_type='recording_finished',
            run_id='run-1',
            room_id=100,
            path=None,
            payload={},
            occurred_at=1_100,
        )
    )

    await outbox.mark_attempt(first.sequence, 'PostgreSQL unavailable')
    failed_status = await outbox.status()
    assert failed_status.pending_count == 2
    assert failed_status.oldest_pending_at == 1_000
    assert failed_status.last_error == 'PostgreSQL unavailable'
    assert failed_status.attempt_count == 1

    await outbox.mark_synced(first.sequence, synced_at=1_200)
    status = await outbox.status()
    assert status.pending_count == 1
    assert status.oldest_pending_at == 1_100
    assert status.last_synced_at == 1_200
    assert status.last_error is None
    assert [event.event_id for event in await outbox.pending_events(limit=10)] == [
        'event-2'
    ]


@pytest.mark.asyncio
async def test_open_rejects_symlink(tmp_path: Path) -> None:
    target = tmp_path / 'target.sqlite3'
    target.touch()
    path = tmp_path / 'recording-journal.sqlite3'
    os.symlink(target, path)
    outbox = LocalRecordingOutbox(path)

    with pytest.raises(ValueError, match='must not be a symlink'):
        await outbox.open()


@pytest.mark.asyncio
async def test_slow_artifact_probe_does_not_occupy_sqlite_writer(
    tmp_path: Path,
) -> None:
    probe_entered = threading.Event()
    release_probe = threading.Event()

    def slow_probe(_path: str):  # type: ignore[no-untyped-def]
        probe_entered.set()
        release_probe.wait(timeout=2)
        return None

    values: Iterator[str] = iter(
        ('run-1', 'event-started', 'event-created', 'event-failed', 'event-finished')
    )
    outbox = LocalRecordingOutbox(
        tmp_path / 'recording-journal.sqlite3',
        uuid_factory=lambda: next(values),
        artifact_probe=slow_probe,
    )
    await outbox.open()
    failed = None
    try:
        run_id = await outbox.recording_started(100, live_start_time=900)
        await outbox.video_created(run_id, '/rec/p1.flv', record_start_time=901)
        failed = asyncio.create_task(
            outbox.video_postprocessing_failed(
                run_id, '/rec/p1.flv', RuntimeError('boom')
            )
        )
        loop = asyncio.get_running_loop()
        assert await loop.run_in_executor(None, probe_entered.wait, 1)

        await asyncio.wait_for(outbox.recording_finished(run_id), timeout=0.2)
        release_probe.set()
        await failed
    finally:
        release_probe.set()
        if failed is not None:
            await failed
        await outbox.close()


@pytest.mark.asyncio
async def test_file_probe_does_not_shift_event_occurrence_time(tmp_path: Path) -> None:
    now = [1_000.25]

    def probe(path: str) -> RecoveredArtifact:
        now[0] = 2_000.5
        return RecoveredArtifact(path=path, size_bytes=8, duration_seconds=60)

    values: Iterator[str] = iter(
        ('run-1', 'event-started', 'event-created', 'event-failed')
    )
    outbox = LocalRecordingOutbox(
        tmp_path / 'recording-journal.sqlite3',
        clock=lambda: now[0],
        uuid_factory=lambda: next(values),
        artifact_probe=probe,
    )
    await outbox.open()
    try:
        run_id = await outbox.recording_started(100, live_start_time=900)
        await outbox.video_created(run_id, '/rec/p1.flv', record_start_time=901)
        await outbox.video_postprocessing_failed(
            run_id, '/rec/p1.flv', RuntimeError('boom')
        )

        event = (await outbox.pending_events())[-1]
        assert event.event_type == 'video_postprocessing_failed'
        assert event.occurred_at == 1_000.25
    finally:
        await outbox.close()


@pytest.mark.asyncio
async def test_file_derived_facts_are_captured_before_remote_sync(
    tmp_path: Path,
) -> None:
    source = tmp_path / 'p1.flv'
    final = tmp_path / 'p1.mp4'
    xml = tmp_path / 'p1.xml'
    source.write_bytes(b'original')
    final.write_bytes(b'final-video')
    xml.write_text('<i><d>one</d><d>two</d></i>', encoding='utf8')
    values: Iterator[str] = iter(
        (
            'run-1',
            'event-started',
            'event-created',
            'event-postprocessed',
            'event-danmaku',
            'event-failed',
        )
    )
    outbox = LocalRecordingOutbox(
        tmp_path / 'recording-journal.sqlite3',
        clock=lambda: 1_000.25,
        uuid_factory=lambda: next(values),
        artifact_probe=lambda path: RecoveredArtifact(
            path=path, size_bytes=8, duration_seconds=60
        ),
    )
    await outbox.open()
    try:
        run_id = await outbox.recording_started(100, live_start_time=900)
        await outbox.video_created(run_id, str(source), record_start_time=901)
        await outbox.video_postprocessed(run_id, str(source), str(final))
        await outbox.danmaku_completed(run_id, str(xml))
        await outbox.video_postprocessing_failed(
            run_id, str(source), RuntimeError('boom')
        )

        events = {event.event_type: event for event in await outbox.pending_events()}
        assert events['video_postprocessed'].payload['file_size_bytes'] == 11
        assert events['danmaku_completed'].payload['danmaku_count'] == 2
        assert events['video_postprocessing_failed'].payload == {
            'error': 'RuntimeError: boom',
            'recovered_artifact': {
                'duration_seconds': 60,
                'path': str(source.resolve()),
                'size_bytes': 8,
            },
        }
    finally:
        await outbox.close()


@pytest.mark.asyncio
async def test_open_rejects_newer_schema_version(tmp_path: Path) -> None:
    path = tmp_path / 'recording-journal.sqlite3'
    connection = sqlite3.connect(str(path))
    connection.execute(
        'PRAGMA application_id={}'.format(LocalRecordingOutbox.APPLICATION_ID)
    )
    connection.execute(
        'PRAGMA user_version={}'.format(LocalRecordingOutbox.SCHEMA_VERSION + 1)
    )
    connection.close()
    outbox = LocalRecordingOutbox(path)

    with pytest.raises(RecordingOutboxError) as raised:
        await outbox.open()

    assert isinstance(raised.value.__cause__, sqlite3.DatabaseError)
    assert 'unsupported recording outbox schema version' in str(raised.value.__cause__)
    await outbox.close()


@pytest.mark.asyncio
async def test_remote_projector_replays_complete_lifecycle_with_captured_facts(
    database: BiliUploadDatabase,
) -> None:
    journal = RecordingJournalBridge(database, clock=lambda: 9_999)
    events = (
        RecordingOutboxEvent(
            event_id='event-started',
            event_type='recording_started',
            run_id='local-run-1',
            room_id=100,
            path=None,
            payload={
                'live_start_time': 900,
                'metadata': {
                    'title': '测试直播',
                    'cover_url': 'https://example.invalid/cover.jpg',
                    'anchor_uid': 42,
                    'anchor_name': '主播',
                    'area_id': 1,
                    'area_name': '分区',
                    'parent_area_id': 2,
                    'parent_area_name': '父分区',
                },
            },
            occurred_at=1_000.25,
            sequence=1,
        ),
        RecordingOutboxEvent(
            event_id='event-created',
            event_type='video_created',
            run_id='local-run-1',
            room_id=100,
            path='/rec/p1.flv',
            payload={'record_start_time': 901, 'timeline_start_at_ms': 1_001_500},
            occurred_at=1_001.5,
            sequence=2,
        ),
        RecordingOutboxEvent(
            event_id='event-completed',
            event_type='video_completed',
            run_id='local-run-1',
            room_id=100,
            path='/rec/p1.flv',
            payload={},
            occurred_at=1_060,
            sequence=3,
        ),
        RecordingOutboxEvent(
            event_id='event-postprocessed',
            event_type='video_postprocessed',
            run_id='local-run-1',
            room_id=100,
            path='/rec/p1.mp4',
            payload={'source_path': '/rec/p1.flv', 'file_size_bytes': 1234},
            occurred_at=1_065,
            sequence=4,
        ),
        RecordingOutboxEvent(
            event_id='event-danmaku',
            event_type='danmaku_completed',
            run_id='local-run-1',
            room_id=100,
            path='/rec/p1.xml',
            payload={'danmaku_count': 7},
            occurred_at=1_066,
            sequence=5,
        ),
        RecordingOutboxEvent(
            event_id='event-finished',
            event_type='recording_finished',
            run_id='local-run-1',
            room_id=100,
            path=None,
            payload={},
            occurred_at=1_070,
            sequence=6,
        ),
    )

    for event in events:
        await journal.apply_recording_event(event)
    await journal.apply_recording_event(events[0])
    await journal.apply_recording_event(events[1])

    session = await database.fetchone(
        'SELECT id,state,started_at,ended_at,live_end_time,title,anchor_uid '
        'FROM recording_sessions WHERE room_id=?',
        (100,),
    )
    run = await database.fetchone(
        'SELECT id,state,started_at,ended_at FROM recording_runs'
    )
    part = await database.fetchone(
        'SELECT source_path,final_path,record_start_time,timeline_start_at_ms,'
        'record_end_time,record_duration_seconds,file_size_bytes,danmaku_count,'
        'artifact_state,xml_completed,source_completed_at,postprocessed_at '
        'FROM recording_parts'
    )
    event_count = await database.fetchone('SELECT COUNT(*) AS count FROM event_journal')

    assert session is not None
    assert dict(session) == {
        'id': 1,
        'state': 'closed',
        'started_at': 1000,
        'ended_at': 1070,
        'live_end_time': 1070,
        'title': '测试直播',
        'anchor_uid': 42,
    }
    assert run is not None
    assert dict(run) == {
        'id': 'local-run-1',
        'state': 'finished',
        'started_at': 1000,
        'ended_at': 1070,
    }
    assert part is not None
    assert dict(part) == {
        'source_path': '/rec/p1.flv',
        'final_path': '/rec/p1.mp4',
        'record_start_time': 901,
        'timeline_start_at_ms': 1001500,
        'record_end_time': 1060,
        'record_duration_seconds': 159,
        'file_size_bytes': 1234,
        'danmaku_count': 7,
        'artifact_state': 'ready',
        'xml_completed': 1,
        'source_completed_at': 1060,
        'postprocessed_at': 1065,
    }
    assert event_count is not None
    assert int(event_count['count']) == len(events)


@pytest.mark.asyncio
async def test_remote_projector_uses_captured_recovery_when_source_is_gone(
    database: BiliUploadDatabase,
) -> None:
    journal = RecordingJournalBridge(
        database,
        clock=lambda: 9_999,
        artifact_probe=lambda _path: pytest.fail('projector must not probe files'),
    )
    events = (
        RecordingOutboxEvent(
            event_id='event-started',
            event_type='recording_started',
            run_id='local-run-1',
            room_id=100,
            path=None,
            payload={'live_start_time': 900, 'metadata': None},
            occurred_at=1_000,
        ),
        RecordingOutboxEvent(
            event_id='event-created',
            event_type='video_created',
            run_id='local-run-1',
            room_id=100,
            path='/rec/gone.flv',
            payload={'record_start_time': 901, 'timeline_start_at_ms': 1_001_000},
            occurred_at=1_001,
        ),
        RecordingOutboxEvent(
            event_id='event-completed',
            event_type='video_completed',
            run_id='local-run-1',
            room_id=100,
            path='/rec/gone.flv',
            payload={},
            occurred_at=1_060,
        ),
        RecordingOutboxEvent(
            event_id='event-failed',
            event_type='video_postprocessing_failed',
            run_id='local-run-1',
            room_id=100,
            path='/rec/gone.flv',
            payload={
                'error': 'RuntimeError: remux failed',
                'recovered_artifact': {
                    'path': '/rec/gone.flv',
                    'size_bytes': 2048,
                    'duration_seconds': 59,
                },
            },
            occurred_at=1_065,
        ),
    )

    for event in events:
        await journal.apply_recording_event(event)

    part = await database.fetchone(
        'SELECT artifact_state,final_path,file_size_bytes,record_duration_seconds,'
        'error_message,postprocessed_at FROM recording_parts'
    )
    assert part is not None
    assert dict(part) == {
        'artifact_state': 'ready',
        'final_path': '/rec/gone.flv',
        'file_size_bytes': 2048,
        'record_duration_seconds': 159,
        'error_message': '后处理失败，已自动使用原始录制文件：RuntimeError: remux failed',
        'postprocessed_at': 1065,
    }


class _FakeProjector:
    def __init__(self) -> None:
        self.calls: List[str] = []
        self.failure: Optional[BaseException] = None

    async def apply_recording_event(self, event: RecordingOutboxEvent) -> None:
        self.calls.append(event.event_id)
        if self.failure is not None:
            raise self.failure


@pytest.mark.asyncio
async def test_synchronizer_retries_head_event_and_acks_only_after_success(
    outbox: LocalRecordingOutbox,
) -> None:
    for index, event_type in enumerate(('recording_started', 'recording_finished')):
        await outbox.append_event(
            RecordingOutboxEvent(
                event_id='event-{}'.format(index + 1),
                event_type=event_type,
                run_id='run-1',
                room_id=100,
                path=None,
                payload=(
                    {'live_start_time': 900, 'metadata': None}
                    if event_type == 'recording_started'
                    else {}
                ),
                occurred_at=1_000 + index,
            )
        )
    projector = _FakeProjector()
    synchronizer = RecordingOutboxSynchronizer(outbox, lambda: projector)

    projector.failure = RuntimeError('PostgreSQL unavailable')
    assert not await synchronizer.sync_one()
    failed_status = await outbox.status()
    assert failed_status.pending_count == 2
    assert failed_status.attempt_count == 1
    assert projector.calls == ['event-1']

    projector.failure = None
    assert await synchronizer.sync_one()
    assert [event.event_id for event in await outbox.pending_events()] == ['event-2']
    assert await synchronizer.sync_one()
    assert await outbox.pending_events() == []
    assert projector.calls == ['event-1', 'event-1', 'event-2']


@pytest.mark.asyncio
async def test_bootstrap_drain_projects_all_backlog_before_workers_start(
    outbox: LocalRecordingOutbox,
) -> None:
    for index, event_type in enumerate(('recording_started', 'recording_finished')):
        await outbox.append_event(
            RecordingOutboxEvent(
                event_id='event-{}'.format(index + 1),
                event_type=event_type,
                run_id='run-1',
                room_id=100,
                path=None,
                payload=(
                    {'live_start_time': 900, 'metadata': None}
                    if event_type == 'recording_started'
                    else {}
                ),
                occurred_at=1_000 + index,
            )
        )
    projector = _FakeProjector()
    synchronizer = RecordingOutboxSynchronizer(outbox, lambda: None)

    assert await synchronizer.drain(projector) == 2

    assert projector.calls == ['event-1', 'event-2']
    assert await outbox.pending_events() == []


@pytest.mark.asyncio
async def test_background_synchronizer_waits_for_projector_then_drains(
    outbox: LocalRecordingOutbox,
) -> None:
    await outbox.append_event(
        RecordingOutboxEvent(
            event_id='event-1',
            event_type='recording_started',
            run_id='run-1',
            room_id=100,
            path=None,
            payload={'live_start_time': 900, 'metadata': None},
            occurred_at=1_000,
        )
    )
    projector: Optional[_FakeProjector] = None
    synchronizer = RecordingOutboxSynchronizer(
        outbox, lambda: projector, retry_interval_seconds=0.01
    )
    synchronizer.start()
    try:
        await asyncio.sleep(0.03)
        assert (await outbox.status()).attempt_count == 0

        projector = _FakeProjector()
        synchronizer.wake()
        for _ in range(50):
            if not await outbox.pending_events():
                break
            await asyncio.sleep(0.01)

        assert await outbox.pending_events() == []
        assert projector.calls == ['event-1']
    finally:
        await synchronizer.close()


class _DelayedRemoteRuntime:
    def __init__(self, projector: _FakeProjector) -> None:
        self.journal: Optional[_FakeProjector] = None
        self._projector = projector
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def start(self) -> bool:
        self.started.set()
        await self.release.wait()
        self.journal = self._projector
        return True


@pytest.mark.asyncio
async def test_runtime_opens_local_store_without_waiting_for_remote_database(
    tmp_path: Path,
) -> None:
    outbox = LocalRecordingOutbox(tmp_path / 'recording-journal.sqlite3')
    projector = _FakeProjector()
    remote = _DelayedRemoteRuntime(projector)
    ready = asyncio.Event()

    async def on_remote_ready() -> None:
        ready.set()

    runtime = RecordingOutboxRuntime(
        outbox,
        lambda: remote,
        on_remote_ready=on_remote_ready,
        retry_interval_seconds=0.01,
    )

    await asyncio.wait_for(runtime.start(), timeout=0.2)
    await asyncio.wait_for(remote.started.wait(), timeout=0.2)
    assert await outbox.pragma('quick_check') == 'ok'
    await outbox.append_event(
        RecordingOutboxEvent(
            event_id='event-1',
            event_type='recording_started',
            run_id='run-1',
            room_id=100,
            path=None,
            payload={'live_start_time': 900, 'metadata': None},
            occurred_at=1_000,
        )
    )
    assert not ready.is_set()
    assert (await outbox.status()).pending_count == 1

    remote.release.set()
    await asyncio.wait_for(ready.wait(), timeout=0.2)
    for _ in range(50):
        if not await outbox.pending_events():
            break
        await asyncio.sleep(0.01)

    assert projector.calls == ['event-1']
    assert await outbox.pending_events() == []
    await runtime.close()


@pytest.mark.asyncio
async def test_runtime_retries_remote_start_without_reopening_local_store(
    tmp_path: Path,
) -> None:
    outbox = LocalRecordingOutbox(tmp_path / 'recording-journal.sqlite3')
    projector = _FakeProjector()

    class RetryRuntime:
        journal: Optional[_FakeProjector] = None

        def __init__(self) -> None:
            self.attempts = 0

        async def start(self) -> bool:
            self.attempts += 1
            if self.attempts == 1:
                return False
            self.journal = projector
            return True

    remote = RetryRuntime()
    ready = asyncio.Event()

    async def on_remote_ready() -> None:
        ready.set()

    runtime = RecordingOutboxRuntime(
        outbox,
        lambda: remote,
        on_remote_ready=on_remote_ready,
        retry_interval_seconds=0.01,
    )
    await runtime.start()
    try:
        await asyncio.wait_for(ready.wait(), timeout=0.2)
        assert remote.attempts == 2
        assert await outbox.pragma('quick_check') == 'ok'
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_local_journal_failure_does_not_prevent_remote_or_file_recording_start(
    tmp_path: Path,
) -> None:
    invalid_path = tmp_path / 'recording-journal.sqlite3'
    invalid_path.mkdir()
    outbox = LocalRecordingOutbox(invalid_path)
    projector = _FakeProjector()

    class ReadyRuntime:
        journal = projector

        async def start(self) -> bool:
            return True

    ready = asyncio.Event()

    async def on_remote_ready() -> None:
        ready.set()

    runtime = RecordingOutboxRuntime(
        outbox,
        lambda: ReadyRuntime(),
        on_remote_ready=on_remote_ready,
        retry_interval_seconds=0.01,
    )

    await runtime.start()
    try:
        await asyncio.wait_for(ready.wait(), timeout=0.2)
        assert outbox.degraded_reason is not None
        assert 'must be a regular file' in outbox.degraded_reason

        invalid_path.rmdir()
        await runtime.start()
        assert runtime.local_ready is True
        assert outbox.degraded_reason is None
    finally:
        await runtime.close()
