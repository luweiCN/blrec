import asyncio
import json
import os
import sqlite3
import threading
import uuid
from pathlib import Path
from typing import AsyncIterator, Optional, Sequence, Tuple

import pytest
import pytest_asyncio
from loguru import logger

from blrec.bili_upload.artifact_recovery import RecoveredArtifact
from blrec.bili_upload.database import BiliUploadDatabase
from blrec.bili_upload.deletion_worker import LocalDeletionWorker
from blrec.bili_upload.highlight_cut import (
    ClipInspection,
    ClipSource,
    CutArtifact,
    HighlightCutError,
    InspectedClipSource,
    LosslessClipper,
    MediaProfile,
)
from blrec.bili_upload.highlight_danmaku import DanmakuClipSource, DanmakuCutResult
from blrec.bili_upload.highlight_worker import HighlightWorker
from blrec.bili_upload.highlights import (
    HighlightConfirmationRequired,
    HighlightInspectionBusy,
    HighlightInspectionConflict,
    HighlightInspectionOperation,
    HighlightRangeUnavailable,
    HighlightService,
)


@pytest_asyncio.fixture
async def database(tmp_path: Path) -> AsyncIterator[BiliUploadDatabase]:
    value = BiliUploadDatabase(str(tmp_path / 'blrec.sqlite3'))
    await value.open()
    try:
        yield value
    finally:
        await value.close()


class FakeClipper:
    def __init__(self, *, extra_lead_ms: int = 2_000) -> None:
        self.extra_lead_ms = extra_lead_ms
        self.inspect_calls = []
        self.cut_calls = []

    def inspect(
        self,
        sources: Sequence[ClipSource],
        *,
        requested_start_ms: int,
        requested_end_ms: int,
        stable_end_ms: int,
        deadline_monotonic=None,
    ) -> ClipInspection:
        self.inspect_calls.append(
            (tuple(sources), requested_start_ms, requested_end_ms, stable_end_ms)
        )
        profile = MediaProfile('h264', 1920, 1080, '60/1', 42, 120_000, True)
        inspected = []
        output_offset_ms = 0
        for index, source in enumerate(sources):
            actual_start_ms = max(
                0, source.requested_start_ms - (self.extra_lead_ms if index == 0 else 0)
            )
            inspected.append(
                InspectedClipSource(
                    source.part_id,
                    source.path,
                    actual_start_ms,
                    source.requested_end_ms,
                    output_offset_ms,
                    profile,
                )
            )
            output_offset_ms += source.requested_end_ms - actual_start_ms
        return ClipInspection(
            tuple(inspected),
            requested_start_ms,
            requested_end_ms,
            requested_start_ms - self.extra_lead_ms,
            requested_end_ms,
            self.extra_lead_ms,
            self.extra_lead_ms > 10_000,
        )

    def cut(self, inspection: ClipInspection, output_path: str) -> CutArtifact:
        self.cut_calls.append((inspection, output_path))
        Path(output_path).write_bytes(b'clipped-video')
        return CutArtifact(
            output_path, len(b'clipped-video'), inspection.output_duration_ms
        )


class BlockingInspectionClipper(FakeClipper):
    def __init__(self) -> None:
        super().__init__()
        self.started = threading.Event()
        self.release = threading.Event()

    def inspect(self, *args, **kwargs) -> ClipInspection:
        self.started.set()
        self.release.wait(timeout=5)
        return super().inspect(*args, **kwargs)


class FlakyInspectionClipper(FakeClipper):
    def __init__(self) -> None:
        super().__init__()
        self.attempts = 0

    def inspect(self, *args, **kwargs) -> ClipInspection:
        self.attempts += 1
        if self.attempts == 1:
            self.inspect_calls.append((tuple(args[0]), None, None, None))
            raise HighlightCutError('temporary ffprobe failure')
        return super().inspect(*args, **kwargs)


class LegacyMultiClipper(LosslessClipper):
    def __init__(self) -> None:
        super().__init__(
            probe=lambda _path: (
                MediaProfile('h264', 1920, 1080, '60/1', 42, 120_000, True),
                (0, 18_000),
            )
        )
        self.cut_calls = []

    def cut(self, inspection: ClipInspection, output_path: str) -> CutArtifact:
        self.cut_calls.append((inspection, output_path))
        Path(output_path).write_bytes(b'legacy-multi-clip')
        return CutArtifact(
            output_path, len(b'legacy-multi-clip'), inspection.output_duration_ms
        )


class DurationAwareClipper(LosslessClipper):
    def __init__(self, duration_ms: int = 120_000) -> None:
        super().__init__()
        self.duration_ms = duration_ms
        self.probe_calls = []
        self.cut_calls = []

    def _probe_media(
        self,
        path: str,
        *,
        keyframe_at_ms: Optional[int] = None,
        known_duration_ms: Optional[int] = None,
        deadline_monotonic: Optional[float] = None,
    ) -> Tuple[MediaProfile, Tuple[int, ...]]:
        self.probe_calls.append((path, keyframe_at_ms, known_duration_ms))
        duration_ms = (
            self.duration_ms if known_duration_ms is None else int(known_duration_ms)
        )
        return (
            MediaProfile('h264', 1920, 1080, '60/1', 42, duration_ms, True),
            (0, 15_000),
        )

    def cut(self, inspection: ClipInspection, output_path: str) -> CutArtifact:
        self.cut_calls.append((inspection, output_path))
        Path(output_path).write_bytes(b'duration-aware-clip')
        return CutArtifact(
            output_path, len(b'duration-aware-clip'), inspection.output_duration_ms
        )


class FakeDanmakuClipper:
    def __init__(self) -> None:
        self.calls = []

    def cut(
        self, sources: Sequence[DanmakuClipSource], output_path: str
    ) -> DanmakuCutResult:
        self.calls.append((tuple(sources), output_path))
        if not sources:
            return DanmakuCutResult(None, 0, 0)
        Path(output_path).write_text('<i><d p="0">弹幕</d></i>', encoding='utf8')
        return DanmakuCutResult(output_path, len(sources), 1)


class BlockingClipper(FakeClipper):
    def __init__(self, *, fail: bool = False) -> None:
        super().__init__()
        self.fail = fail
        self.started = threading.Event()
        self.release = threading.Event()
        self.finished = threading.Event()

    def cut(self, inspection: ClipInspection, output_path: str) -> CutArtifact:
        self.started.set()
        self.release.wait()
        try:
            if self.fail:
                raise HighlightCutError('blocked FFmpeg failed')
            return super().cut(inspection, output_path)
        finally:
            self.finished.set()


async def seed_active_recording(database: BiliUploadDatabase, root: Path) -> Path:
    video = root / 'room-100.flv'
    xml = root / 'room-100.xml'
    video.write_bytes(b'live-video')
    xml.write_text('<i><d p="30,1,25,1,0,0,u,1">弹幕</d></i>', encoding='utf8')
    await database.execute(
        "INSERT INTO recording_sessions("
        "id,room_id,broadcast_session_key,state,started_at,title,anchor_name) "
        "VALUES(1,100,'100:900','open',900,'测试直播','主播')"
    )
    await database.execute(
        "INSERT INTO recording_runs(id,session_id,state,started_at) "
        "VALUES('run',1,'recording',900)"
    )
    await database.execute(
        'INSERT INTO recording_parts('
        'id,session_id,run_id,part_index,source_path,final_path,xml_path,'
        'record_start_time,timeline_start_at_ms,artifact_state,xml_completed,'
        'created_at,updated_at) '
        "VALUES(1,1,'run',1,?,NULL,?,900,900000,'recording',1,900,900)",
        (str(video), str(xml)),
    )
    return video


async def wait_for_inspection(
    service: HighlightService, operation_id: str, *, claim_key: str
) -> HighlightInspectionOperation:
    for _attempt in range(100):
        operation = await service.get_clip_inspection(operation_id, claim_key=claim_key)
        if operation.state in ('succeeded', 'failed'):
            return operation
        await asyncio.sleep(0.01)
    raise AssertionError('highlight inspection did not reach a terminal state')


@pytest.mark.asyncio
async def test_progress_returns_only_active_or_recent_clips(
    database: BiliUploadDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = 1_000
    cutoff = now - 300
    await database.execute(
        'INSERT INTO highlight_clips('
        'id,room_id,name,requested_start_ms,requested_end_ms,state,attempt,'
        'error_message,created_at,updated_at) VALUES'
        "(1,100,'过期成品',0,1000,'ready',0,NULL,1,699),"
        "(2,100,'排队中',0,1000,'queued',1,NULL,1,1),"
        "(3,100,'处理中',0,1000,'processing',2,NULL,1,2),"
        "(4,100,'边界成品',0,1000,'ready',0,NULL,1,700),"
        "(5,100,'最近取消',0,1000,'cancelled',3,'已取消',1,1000),"
        "(6,100,'过期失败',0,1000,'failed',4,'失败',1,699)"
    )
    fetchall = database.fetchall
    calls = []

    async def capturing_fetchall(sql, parameters=()):
        calls.append((sql, tuple(parameters)))
        return await fetchall(sql, parameters)

    monkeypatch.setattr(database, 'fetchall', capturing_fetchall)
    worker = HighlightWorker(
        database,
        FakeClipper(),
        FakeDanmakuClipper(),
        worker_id='worker',
        clock=lambda: now,
    )

    progress = await worker.progress()

    assert [item['id'] for item in progress] == [5, 4, 3, 2]
    assert len(calls) == 1
    sql = ' '.join(calls[0][0].split())
    assert "WHERE state IN ('queued','processing') OR updated_at>=?" in sql
    assert calls[0][1] == (cutoff,)


@pytest.mark.asyncio
async def test_create_clip_persists_ordered_sources_and_rejects_unsafe_tail(
    database: BiliUploadDatabase, tmp_path: Path
) -> None:
    root = tmp_path / 'records'
    root.mkdir()
    await seed_active_recording(database, root)
    clipper = FakeClipper()
    service = HighlightService(database, recording_root=root, clipper=clipper)

    clip = await service.create_clip(
        session_id=1,
        marker_id=None,
        name='第一段高光',
        requested_start_ms=20_000,
        requested_end_ms=70_000,
        confirm_keyframe=False,
        active_durations_ms={1: 120_000},
    )

    assert clip.state == 'queued'
    assert clip.output_video_path == str(
        root / 'highlights' / '100' / 'highlight-1.mp4'
    )
    rows = await database.fetchall(
        'SELECT ordinal,part_id,requested_start_ms,requested_end_ms,'
        'actual_start_ms,actual_end_ms FROM highlight_clip_sources '
        'WHERE clip_id=? ORDER BY ordinal',
        (clip.id,),
    )
    assert [dict(row) for row in rows] == [
        {
            'ordinal': 1,
            'part_id': 1,
            'requested_start_ms': 20_000,
            'requested_end_ms': 70_000,
            'actual_start_ms': 18_000,
            'actual_end_ms': 70_000,
        }
    ]

    with pytest.raises(HighlightRangeUnavailable, match='最后 10 秒'):
        await service.create_clip(
            session_id=1,
            marker_id=None,
            name='过近',
            requested_start_ms=100_000,
            requested_end_ms=119_000,
            confirm_keyframe=False,
            active_durations_ms={1: 120_000},
        )


@pytest.mark.asyncio
async def test_inspection_token_creates_idempotent_clip_and_worker_reuses_probe(
    database: BiliUploadDatabase, tmp_path: Path
) -> None:
    root = tmp_path / 'records'
    root.mkdir()
    await seed_active_recording(database, root)
    clipper = FakeClipper()
    service = HighlightService(
        database,
        recording_root=root,
        clipper=clipper,
        inspection_secret=b'test-inspection-secret',
    )
    idempotency_key = str(uuid.uuid4())
    claim_key = str(uuid.uuid4())
    try:
        accepted = await service.submit_clip_inspection(
            session_id=1,
            requested_start_ms=20_000,
            requested_end_ms=70_000,
            active_durations_ms={1: 120_000},
            idempotency_key=idempotency_key,
        )
        assert accepted.state == 'accepted'

        ready = None
        for _attempt in range(100):
            value = await service.get_clip_inspection(
                accepted.operation_id, claim_key=claim_key
            )
            if value.state == 'succeeded':
                ready = value
                break
            await asyncio.sleep(0.01)
        assert ready is not None
        assert ready.inspection_token
        assert ready.inspection is not None
        assert len(clipper.inspect_calls) == 1
        reclaimed = await service.get_clip_inspection(
            accepted.operation_id, claim_key=claim_key
        )
        assert reclaimed.inspection_token == ready.inspection_token

        stored = await database.fetchone(
            'SELECT claim_key_hash,token_hash FROM highlight_inspections '
            'WHERE operation_id=?',
            (accepted.operation_id,),
        )
        assert stored is not None
        assert claim_key not in tuple(str(value) for value in stored)
        assert ready.inspection_token not in tuple(str(value) for value in stored)

        with pytest.raises(HighlightInspectionConflict, match='绑定'):
            await service.create_clip(
                session_id=1,
                marker_id=None,
                name='错误重放',
                requested_start_ms=20_000,
                requested_end_ms=70_000,
                confirm_keyframe=False,
                active_durations_ms={1: 120_000},
                inspection_token=ready.inspection_token,
                idempotency_key=str(uuid.uuid4()),
            )

        clip = await service.create_clip(
            session_id=1,
            marker_id=None,
            name='第一段高光',
            requested_start_ms=20_000,
            requested_end_ms=70_000,
            confirm_keyframe=False,
            active_durations_ms={1: 120_000},
            inspection_token=ready.inspection_token,
            idempotency_key=idempotency_key,
        )
        repeated = await service.create_clip(
            session_id=1,
            marker_id=None,
            name='第一段高光',
            requested_start_ms=20_000,
            requested_end_ms=70_000,
            confirm_keyframe=False,
            active_durations_ms={1: 120_000},
            inspection_token=ready.inspection_token,
            idempotency_key=idempotency_key,
        )
        assert repeated.id == clip.id
        with pytest.raises(HighlightInspectionConflict, match='已经使用'):
            await service.get_clip_inspection(
                accepted.operation_id, claim_key=claim_key
            )

        worker = HighlightWorker(
            database, clipper, FakeDanmakuClipper(), worker_id='worker'
        )
        assert await worker.run_once() == clip.id
        assert len(clipper.inspect_calls) == 1
    finally:
        await service.shutdown()


@pytest.mark.asyncio
async def test_worker_reprobes_inspection_created_by_an_older_cut_algorithm(
    database: BiliUploadDatabase, tmp_path: Path
) -> None:
    root = tmp_path / 'records'
    root.mkdir()
    await seed_active_recording(database, root)
    clipper = FakeClipper()
    service = HighlightService(
        database,
        recording_root=root,
        clipper=clipper,
        inspection_secret=b'test-inspection-secret',
    )
    idempotency_key = str(uuid.uuid4())
    try:
        accepted = await service.submit_clip_inspection(
            session_id=1,
            requested_start_ms=20_000,
            requested_end_ms=70_000,
            active_durations_ms={1: 120_000},
            idempotency_key=idempotency_key,
        )
        ready = await wait_for_inspection(
            service, accepted.operation_id, claim_key=str(uuid.uuid4())
        )
        assert ready.inspection_token is not None
        clip = await service.create_clip(
            session_id=1,
            marker_id=None,
            name='旧算法预检',
            requested_start_ms=20_000,
            requested_end_ms=70_000,
            confirm_keyframe=False,
            active_durations_ms={1: 120_000},
            inspection_token=ready.inspection_token,
            idempotency_key=idempotency_key,
        )
        assert not isinstance(clip, HighlightInspectionOperation)
        stored = await database.scalar(
            'SELECT inspection_json FROM highlight_clips WHERE id=?', (clip.id,)
        )
        legacy = json.loads(str(stored))
        legacy.pop('algorithmVersion')
        await database.execute(
            'UPDATE highlight_clips SET inspection_json=? WHERE id=?',
            (json.dumps(legacy), clip.id),
        )

        worker = HighlightWorker(
            database, clipper, FakeDanmakuClipper(), worker_id='worker'
        )
        assert await worker.run_once() == clip.id

        assert len(clipper.inspect_calls) == 2
        refreshed = await database.scalar(
            'SELECT inspection_json FROM highlight_clips WHERE id=?', (clip.id,)
        )
        assert json.loads(str(refreshed))['algorithmVersion'] == 2
    finally:
        await service.shutdown()


@pytest.mark.asyncio
async def test_growing_recording_keeps_validated_inspection_for_create_and_worker(
    database: BiliUploadDatabase, tmp_path: Path
) -> None:
    root = tmp_path / 'records'
    root.mkdir()
    source = await seed_active_recording(database, root)
    clipper = FakeClipper()
    service = HighlightService(
        database,
        recording_root=root,
        clipper=clipper,
        inspection_secret=b'test-inspection-secret',
    )
    idempotency_key = str(uuid.uuid4())
    claim_key = str(uuid.uuid4())
    try:
        accepted = await service.submit_clip_inspection(
            session_id=1,
            requested_start_ms=20_000,
            requested_end_ms=70_000,
            active_durations_ms={1: 120_000},
            idempotency_key=idempotency_key,
        )
        ready = await wait_for_inspection(
            service, accepted.operation_id, claim_key=claim_key
        )
        assert ready.state == 'succeeded'
        assert ready.inspection_token is not None

        with source.open('ab') as stream:
            stream.write(b'-appended-after-inspection')
        clip = await service.create_clip(
            session_id=1,
            marker_id=None,
            name='录制中高光',
            requested_start_ms=20_000,
            requested_end_ms=70_000,
            confirm_keyframe=False,
            active_durations_ms={1: 130_000},
            inspection_token=ready.inspection_token,
            idempotency_key=idempotency_key,
        )

        assert not isinstance(clip, HighlightInspectionOperation)
        worker = HighlightWorker(
            database, clipper, FakeDanmakuClipper(), worker_id='worker'
        )
        assert await worker.run_once() == clip.id
        assert len(clipper.inspect_calls) == 1
    finally:
        await service.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize('mutation', ('replace', 'shrink'))
async def test_growing_recording_rejects_replaced_or_shrunk_inspection_source(
    database: BiliUploadDatabase, tmp_path: Path, mutation: str
) -> None:
    root = tmp_path / 'records'
    root.mkdir()
    source = await seed_active_recording(database, root)
    service = HighlightService(
        database,
        recording_root=root,
        clipper=FakeClipper(),
        inspection_secret=b'test-inspection-secret',
    )
    idempotency_key = str(uuid.uuid4())
    claim_key = str(uuid.uuid4())
    try:
        accepted = await service.submit_clip_inspection(
            session_id=1,
            requested_start_ms=20_000,
            requested_end_ms=70_000,
            active_durations_ms={1: 120_000},
            idempotency_key=idempotency_key,
        )
        ready = await wait_for_inspection(
            service, accepted.operation_id, claim_key=claim_key
        )
        assert ready.inspection_token is not None
        if mutation == 'replace':
            replacement = root / 'replacement.flv'
            replacement.write_bytes(b'replaced-video-source')
            os.replace(str(replacement), str(source))
        else:
            source.write_bytes(b'x')

        result = await service.create_clip(
            session_id=1,
            marker_id=None,
            name='变化的高光',
            requested_start_ms=20_000,
            requested_end_ms=70_000,
            confirm_keyframe=False,
            active_durations_ms={1: 130_000},
            inspection_token=ready.inspection_token,
            idempotency_key=idempotency_key,
        )

        assert isinstance(result, HighlightInspectionOperation)
        assert result.state == 'accepted'
    finally:
        await service.shutdown()


@pytest.mark.asyncio
async def test_claim_extends_terminal_retention_through_token_expiry(
    database: BiliUploadDatabase, tmp_path: Path
) -> None:
    root = tmp_path / 'records'
    root.mkdir()
    await seed_active_recording(database, root)
    now = [1_000]
    service = HighlightService(
        database,
        recording_root=root,
        clipper=FakeClipper(),
        clock=lambda: now[0],
        inspection_secret=b'test-inspection-secret',
    )
    idempotency_key = str(uuid.uuid4())
    claim_key = str(uuid.uuid4())
    try:
        accepted = await service.submit_clip_inspection(
            session_id=1,
            requested_start_ms=20_000,
            requested_end_ms=70_000,
            active_durations_ms={1: 120_000},
            idempotency_key=idempotency_key,
        )
        for _attempt in range(100):
            state = await database.scalar(
                'SELECT state FROM highlight_inspections WHERE operation_id=?',
                (accepted.operation_id,),
            )
            if state == 'succeeded':
                break
            await asyncio.sleep(0.01)
        assert state == 'succeeded'

        now[0] = 1_299
        ready = await service.get_clip_inspection(
            accepted.operation_id, claim_key=claim_key
        )
        assert ready.inspection_token is not None
        now[0] = 1_300
        await service.submit_clip_inspection(
            session_id=1,
            requested_start_ms=20_000,
            requested_end_ms=70_000,
            active_durations_ms={1: 120_000},
            idempotency_key=str(uuid.uuid4()),
        )

        assert (
            await database.scalar(
                'SELECT COUNT(*) FROM highlight_inspections WHERE operation_id=?',
                (accepted.operation_id,),
            )
            == 1
        )
        clip = await service.create_clip(
            session_id=1,
            marker_id=None,
            name='延长有效期',
            requested_start_ms=20_000,
            requested_end_ms=70_000,
            confirm_keyframe=False,
            active_durations_ms={1: 120_000},
            inspection_token=ready.inspection_token,
            idempotency_key=idempotency_key,
        )
        assert not isinstance(clip, HighlightInspectionOperation)
    finally:
        await service.shutdown()


@pytest.mark.asyncio
async def test_probe_singleflight_forgets_terminal_success_and_failure(
    database: BiliUploadDatabase, tmp_path: Path
) -> None:
    root = tmp_path / 'records'
    root.mkdir()
    await seed_active_recording(database, root)
    clipper = FlakyInspectionClipper()
    service = HighlightService(
        database,
        recording_root=root,
        clipper=clipper,
        inspection_secret=b'test-inspection-secret',
    )
    try:
        first = await service.submit_clip_inspection(
            session_id=1,
            requested_start_ms=20_000,
            requested_end_ms=70_000,
            active_durations_ms={1: 120_000},
            idempotency_key=str(uuid.uuid4()),
        )
        failed = await wait_for_inspection(
            service, first.operation_id, claim_key=str(uuid.uuid4())
        )
        assert failed.state == 'failed'
        assert service._probe_futures == {}

        second = await service.submit_clip_inspection(
            session_id=1,
            requested_start_ms=20_000,
            requested_end_ms=70_000,
            active_durations_ms={1: 120_000},
            idempotency_key=str(uuid.uuid4()),
        )
        succeeded = await wait_for_inspection(
            service, second.operation_id, claim_key=str(uuid.uuid4())
        )
        assert succeeded.state == 'succeeded'
        assert clipper.attempts == 2
        assert service._probe_futures == {}
    finally:
        await service.shutdown()


@pytest.mark.asyncio
async def test_successful_probe_remains_singleflight_until_result_is_durable(
    database: BiliUploadDatabase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / 'records'
    root.mkdir()
    await seed_active_recording(database, root)
    clipper = FakeClipper()
    service = HighlightService(
        database,
        recording_root=root,
        clipper=clipper,
        inspection_secret=b'test-inspection-secret',
    )
    original_execute = database.execute
    first_success_update_started = asyncio.Event()
    release_first_success_update = asyncio.Event()
    success_update_count = 0

    async def delay_first_success_update(sql, parameters=()):
        nonlocal success_update_count
        if "UPDATE highlight_inspections SET state='succeeded'" in sql:
            success_update_count += 1
            if success_update_count == 1:
                first_success_update_started.set()
                await release_first_success_update.wait()
        return await original_execute(sql, parameters)

    monkeypatch.setattr(database, 'execute', delay_first_success_update)
    try:
        first = await service.submit_clip_inspection(
            session_id=1,
            requested_start_ms=20_000,
            requested_end_ms=70_000,
            active_durations_ms={1: 120_000},
            idempotency_key=str(uuid.uuid4()),
        )
        await asyncio.wait_for(first_success_update_started.wait(), timeout=1)

        second = await service.submit_clip_inspection(
            session_id=1,
            requested_start_ms=20_000,
            requested_end_ms=70_000,
            active_durations_ms={1: 120_000},
            idempotency_key=str(uuid.uuid4()),
        )
        second_result = await wait_for_inspection(
            service, second.operation_id, claim_key=str(uuid.uuid4())
        )

        assert second_result.state == 'succeeded'
        assert len(clipper.inspect_calls) == 1
    finally:
        release_first_success_update.set()
        await service.shutdown()

    first_result = await service.get_clip_inspection(
        first.operation_id, claim_key=str(uuid.uuid4())
    )
    assert first_result.state == 'succeeded'
    assert service._probe_futures == {}


@pytest.mark.asyncio
async def test_inspection_admission_is_bounded_to_two_active_and_eight_waiting(
    database: BiliUploadDatabase, tmp_path: Path
) -> None:
    root = tmp_path / 'records'
    root.mkdir()
    await seed_active_recording(database, root)
    clipper = BlockingInspectionClipper()
    service = HighlightService(
        database,
        recording_root=root,
        clipper=clipper,
        inspection_secret=b'test-inspection-secret',
    )
    try:
        operations = []
        for _index in range(10):
            operations.append(
                await service.submit_clip_inspection(
                    session_id=1,
                    requested_start_ms=20_000,
                    requested_end_ms=70_000,
                    active_durations_ms={1: 120_000},
                    idempotency_key=str(uuid.uuid4()),
                )
            )
        assert all(operation.state == 'accepted' for operation in operations)
        assert await asyncio.get_running_loop().run_in_executor(
            None, clipper.started.wait, 1
        )

        with pytest.raises(HighlightInspectionBusy):
            await service.submit_clip_inspection(
                session_id=1,
                requested_start_ms=20_000,
                requested_end_ms=70_000,
                active_durations_ms={1: 120_000},
                idempotency_key=str(uuid.uuid4()),
            )
    finally:
        clipper.release.set()
        await service.shutdown()
    assert len(clipper.inspect_calls) == 1


@pytest.mark.asyncio
async def test_create_clip_requires_explicit_keyframe_confirmation(
    database: BiliUploadDatabase, tmp_path: Path
) -> None:
    root = tmp_path / 'records'
    root.mkdir()
    await seed_active_recording(database, root)
    clipper = FakeClipper(extra_lead_ms=12_000)
    service = HighlightService(database, recording_root=root, clipper=clipper)

    with pytest.raises(HighlightConfirmationRequired) as error:
        await service.create_clip(
            session_id=1,
            marker_id=None,
            name='关键帧过远',
            requested_start_ms=20_000,
            requested_end_ms=70_000,
            confirm_keyframe=False,
            active_durations_ms={1: 120_000},
        )
    assert error.value.extra_lead_ms == 12_000
    assert await database.scalar('SELECT COUNT(*) FROM highlight_clips') == 0

    clip = await service.create_clip(
        session_id=1,
        marker_id=None,
        name='已确认',
        requested_start_ms=20_000,
        requested_end_ms=70_000,
        confirm_keyframe=True,
        active_durations_ms={1: 120_000},
    )
    row = await database.fetchone(
        'SELECT keyframe_confirmation_required,keyframe_confirmed '
        'FROM highlight_clips WHERE id=?',
        (clip.id,),
    )
    assert row is not None
    assert dict(row) == {'keyframe_confirmation_required': 1, 'keyframe_confirmed': 1}


@pytest.mark.asyncio
async def test_worker_completes_video_and_danmaku_atomically(
    database: BiliUploadDatabase, tmp_path: Path
) -> None:
    root = tmp_path / 'records'
    root.mkdir()
    await seed_active_recording(database, root)
    clipper = FakeClipper()
    danmaku = FakeDanmakuClipper()
    service = HighlightService(database, recording_root=root, clipper=clipper)
    clip = await service.create_clip(
        session_id=1,
        marker_id=None,
        name='待处理',
        requested_start_ms=20_000,
        requested_end_ms=70_000,
        confirm_keyframe=False,
        active_durations_ms={1: 120_000},
    )
    worker = HighlightWorker(
        database, clipper, danmaku, worker_id='worker', clock=lambda: 1_000
    )

    assert await worker.run_once() == clip.id

    row = await database.fetchone(
        'SELECT state,actual_start_ms,actual_end_ms,output_video_path,'
        'output_xml_path,file_size_bytes,lease_owner,lease_until '
        'FROM highlight_clips WHERE id=?',
        (clip.id,),
    )
    assert row is not None
    assert row['state'] == 'ready'
    assert row['actual_start_ms'] == 18_000
    assert row['actual_end_ms'] == 70_000
    assert row['lease_owner'] is None
    assert row['lease_until'] is None
    assert row['file_size_bytes'] == len(b'clipped-video')
    assert Path(str(row['output_video_path'])).read_bytes() == b'clipped-video'
    assert Path(str(row['output_xml_path'])).exists()
    assert len(clipper.cut_calls) == 1
    assert len(danmaku.calls) == 1
    worker_sources = clipper.inspect_calls[-1][0]
    assert worker_sources[0].duration_ms is None
    assert worker_sources[0].keyframes_ms == ()


@pytest.mark.asyncio
async def test_worker_reprobes_profile_and_keyframes_after_source_replacement(
    database: BiliUploadDatabase, tmp_path: Path
) -> None:
    root = tmp_path / 'records'
    root.mkdir()
    source = await seed_active_recording(database, root)
    clipper = FakeClipper()
    service = HighlightService(
        database,
        recording_root=root,
        clipper=clipper,
        inspection_secret=b'test-inspection-secret',
    )
    idempotency_key = str(uuid.uuid4())
    try:
        accepted = await service.submit_clip_inspection(
            session_id=1,
            requested_start_ms=20_000,
            requested_end_ms=70_000,
            active_durations_ms={1: 120_000},
            idempotency_key=idempotency_key,
        )
        ready = await wait_for_inspection(
            service, accepted.operation_id, claim_key=str(uuid.uuid4())
        )
        assert ready.inspection_token is not None
        clip = await service.create_clip(
            session_id=1,
            marker_id=None,
            name='替换源文件',
            requested_start_ms=20_000,
            requested_end_ms=70_000,
            confirm_keyframe=False,
            active_durations_ms={1: 120_000},
            inspection_token=ready.inspection_token,
            idempotency_key=idempotency_key,
        )
        assert not isinstance(clip, HighlightInspectionOperation)
        stored_before = await database.scalar(
            'SELECT source_fingerprint_json FROM highlight_clips WHERE id=?', (clip.id,)
        )

        replacement = root / 'replacement.flv'
        replacement.write_bytes(b'replacement-source-with-new-inode')
        os.replace(str(replacement), str(source))
        clipper.extra_lead_ms = 5_000
        worker = HighlightWorker(
            database, clipper, FakeDanmakuClipper(), worker_id='worker'
        )
        assert await worker.run_once() == clip.id

        reprobe_sources = clipper.inspect_calls[-1][0]
        assert reprobe_sources[0].keyframes_ms == ()
        row = await database.fetchone(
            'SELECT actual_start_ms,inspection_json,source_fingerprint_json '
            'FROM highlight_clips WHERE id=?',
            (clip.id,),
        )
        assert row is not None
        assert int(row['actual_start_ms']) == 15_000
        assert json.loads(str(row['inspection_json']))['actualStartMs'] == 15_000
        assert str(row['source_fingerprint_json']) != str(stored_before)
    finally:
        await service.shutdown()


@pytest.mark.parametrize(
    ('replacement_duration_ms', 'expected_state', 'expected_cut_count'),
    ((60_000, 'queued', 0), (180_000, 'ready', 1)),
)
@pytest.mark.asyncio
async def test_worker_reprobes_replacement_with_its_real_duration(
    database: BiliUploadDatabase,
    tmp_path: Path,
    replacement_duration_ms: int,
    expected_state: str,
    expected_cut_count: int,
) -> None:
    root = tmp_path / 'records'
    root.mkdir()
    source = await seed_active_recording(database, root)
    clipper = DurationAwareClipper()
    service = HighlightService(
        database,
        recording_root=root,
        clipper=clipper,
        inspection_secret=b'test-inspection-secret',
    )
    idempotency_key = str(uuid.uuid4())
    try:
        accepted = await service.submit_clip_inspection(
            session_id=1,
            requested_start_ms=20_000,
            requested_end_ms=70_000,
            active_durations_ms={1: 120_000},
            idempotency_key=idempotency_key,
        )
        ready = await wait_for_inspection(
            service, accepted.operation_id, claim_key=str(uuid.uuid4())
        )
        assert ready.inspection_token is not None
        clip = await service.create_clip(
            session_id=1,
            marker_id=None,
            name='替换源文件时长',
            requested_start_ms=20_000,
            requested_end_ms=70_000,
            confirm_keyframe=False,
            active_durations_ms={1: 120_000},
            inspection_token=ready.inspection_token,
            idempotency_key=idempotency_key,
        )
        assert not isinstance(clip, HighlightInspectionOperation)

        replacement = root / 'replacement-duration.flv'
        replacement.write_bytes(b'replacement-source-with-different-duration')
        os.replace(str(replacement), str(source))
        clipper.duration_ms = replacement_duration_ms
        worker = HighlightWorker(
            database, clipper, FakeDanmakuClipper(), worker_id='worker'
        )

        assert await worker.run_once() == clip.id

        row = await database.fetchone(
            'SELECT state,inspection_json FROM highlight_clips WHERE id=?', (clip.id,)
        )
        assert row is not None
        assert str(row['state']) == expected_state
        assert clipper.probe_calls[-1][2] is None
        assert len(clipper.cut_calls) == expected_cut_count
        if expected_state == 'ready':
            inspection = json.loads(str(row['inspection_json']))
            assert inspection['sources'][0]['profile']['durationMs'] == 180_000
    finally:
        await service.shutdown()


@pytest.mark.asyncio
async def test_worker_keeps_migration_27_legacy_multi_part_clip_processable(
    database: BiliUploadDatabase, tmp_path: Path
) -> None:
    root = tmp_path / 'records'
    output_root = root / 'highlights' / '200'
    root.mkdir()
    output_root.mkdir(parents=True)
    first = root / 'legacy-p1.flv'
    second = root / 'legacy-p2.flv'
    first.write_bytes(b'legacy-first')
    second.write_bytes(b'legacy-second')
    await database.execute(
        'INSERT INTO recording_sessions('
        'id,room_id,broadcast_session_key,state,started_at,title,anchor_name) '
        "VALUES(9,200,'200:900','closed',900,'旧直播','主播')"
    )
    await database.execute(
        'INSERT INTO recording_runs(id,session_id,state,started_at,ended_at) '
        "VALUES('legacy-run',9,'finished',900,1020)"
    )
    await database.execute(
        'INSERT INTO recording_parts('
        'id,session_id,run_id,part_index,source_path,final_path,'
        'record_start_time,timeline_start_at_ms,record_duration_seconds,'
        'artifact_state,xml_completed,created_at,updated_at) '
        "VALUES(91,9,'legacy-run',1,?,?,900,900000,90,'ready',1,900,900),"
        "(92,9,'legacy-run',2,?,?,990,990000,30,'ready',1,990,990)",
        (str(first), str(first), str(second), str(second)),
    )
    output_video = output_root / 'highlight-1.mp4'
    output_xml = output_root / 'highlight-1.xml'
    await database.execute(
        'INSERT INTO highlight_clips('
        'id,room_id,source_session_id,name,requested_start_ms,requested_end_ms,'
        'actual_start_ms,actual_end_ms,output_video_path,output_xml_path,state,'
        'keyframe_confirmation_required,keyframe_confirmed,created_at,updated_at,'
        'inspection_json,source_fingerprint_json) '
        "VALUES(1,200,9,'迁移前多P片段',20000,120000,18000,120000,?,?,'queued',"
        '0,0,1,1,NULL,NULL)',
        (str(output_video), str(output_xml)),
    )
    await database.execute(
        'INSERT INTO highlight_clip_sources('
        'clip_id,part_id,ordinal,requested_start_ms,requested_end_ms,'
        'actual_start_ms,actual_end_ms) VALUES'
        '(1,91,1,20000,90000,18000,90000),'
        '(1,92,2,0,30000,0,30000)'
    )
    clipper = LegacyMultiClipper()
    worker = HighlightWorker(
        database, clipper, FakeDanmakuClipper(), worker_id='worker'
    )

    assert await worker.run_once() == 1

    row = await database.fetchone(
        'SELECT state,file_size_bytes FROM highlight_clips WHERE id=1'
    )
    assert row is not None
    assert dict(row) == {'state': 'ready', 'file_size_bytes': len(b'legacy-multi-clip')}
    assert output_video.read_bytes() == b'legacy-multi-clip'
    assert len(clipper.cut_calls[0][0].sources) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize('fail', (False, True))
async def test_worker_hands_off_ffmpeg_result_after_clip_deletion_intent(
    database: BiliUploadDatabase, tmp_path: Path, fail: bool
) -> None:
    root = tmp_path / 'records'
    root.mkdir()
    source = await seed_active_recording(database, root)
    clipper = BlockingClipper(fail=fail)
    service = HighlightService(database, recording_root=root, clipper=clipper)
    clip = await service.create_clip(
        session_id=1,
        marker_id=None,
        name='删除中的高光',
        requested_start_ms=20_000,
        requested_end_ms=70_000,
        confirm_keyframe=False,
        active_durations_ms={1: 120_000},
    )
    worker = HighlightWorker(
        database, clipper, FakeDanmakuClipper(), worker_id='worker', clock=lambda: 1_000
    )
    deletion = LocalDeletionWorker(
        database,
        recording_root=root,
        clip_root=root / 'highlights',
        clock=lambda: 1_001,
    )
    run_task = asyncio.create_task(worker.run_once())
    started = await asyncio.get_running_loop().run_in_executor(
        None, clipper.started.wait, 0.5
    )
    assert started
    try:
        await deletion.request_clip(clip.id)
        assert await deletion.run_once() == ('clip', clip.id)
        assert source.exists()

        clipper.release.set()
        assert await asyncio.wait_for(run_task, timeout=0.5) == clip.id

        row = await database.fetchone(
            'SELECT state,deletion_state,cancellation_generation,lease_owner '
            'FROM highlight_clips WHERE id=?',
            (clip.id,),
        )
        outcome = await database.fetchone(
            'SELECT owner_kind,owner_id,side_effect_key,source_generation,'
            'outcome_state FROM owner_handoff_outcomes '
            "WHERE owner_kind='highlight' AND owner_id=?",
            (clip.id,),
        )
        assert row is not None
        assert dict(row) == {
            'state': 'processing',
            'deletion_state': 'requested',
            'cancellation_generation': 1,
            'lease_owner': None,
        }
        assert outcome is not None
        assert dict(outcome) == {
            'owner_kind': 'highlight',
            'owner_id': clip.id,
            'side_effect_key': 'ffmpeg_cut',
            'source_generation': 0,
            'outcome_state': 'cancelled_local',
        }

        assert await deletion.run_once() == ('clip', clip.id)
        assert await database.scalar('SELECT COUNT(*) FROM highlight_clips') == 0
        assert source.exists()
        if clip.output_video_path is not None:
            assert not Path(clip.output_video_path).exists()
    finally:
        clipper.release.set()
        await asyncio.gather(run_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_worker_hands_off_when_source_session_is_deleted_during_ffmpeg(
    database: BiliUploadDatabase, tmp_path: Path
) -> None:
    root = tmp_path / 'records'
    root.mkdir()
    await seed_active_recording(database, root)
    clipper = BlockingClipper()
    service = HighlightService(database, recording_root=root, clipper=clipper)
    clip = await service.create_clip(
        session_id=1,
        marker_id=None,
        name='源场次删除中的高光',
        requested_start_ms=20_000,
        requested_end_ms=70_000,
        confirm_keyframe=False,
        active_durations_ms={1: 120_000},
    )
    worker = HighlightWorker(
        database, clipper, FakeDanmakuClipper(), worker_id='worker', clock=lambda: 1_000
    )
    deletion = LocalDeletionWorker(
        database,
        recording_root=root,
        clip_root=root / 'highlights',
        clock=lambda: 1_001,
    )
    run_task = asyncio.create_task(worker.run_once())
    started = await asyncio.get_running_loop().run_in_executor(
        None, clipper.started.wait, 0.5
    )
    assert started
    try:
        await deletion.request_session(1, manager_subject='manager')
        clipper.release.set()
        assert await asyncio.wait_for(run_task, timeout=0.5) == clip.id

        clip_row = await database.fetchone(
            'SELECT state,deletion_state,cancellation_generation,lease_owner '
            'FROM highlight_clips WHERE id=?',
            (clip.id,),
        )
        assert clip_row is not None
        assert dict(clip_row) == {
            'state': 'processing',
            'deletion_state': 'requested',
            'cancellation_generation': 1,
            'lease_owner': None,
        }
        assert (
            await database.scalar(
                "SELECT outcome_state FROM owner_handoff_outcomes "
                "WHERE owner_kind='highlight' AND owner_id=? "
                "AND source_generation=0",
                (clip.id,),
            )
            == 'cancelled_local'
        )
    finally:
        clipper.release.set()
        await asyncio.gather(run_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_worker_persists_claim_generation_until_ffmpeg_settles(
    database: BiliUploadDatabase, tmp_path: Path
) -> None:
    root = tmp_path / 'records'
    root.mkdir()
    await seed_active_recording(database, root)
    clipper = BlockingClipper()
    service = HighlightService(database, recording_root=root, clipper=clipper)
    clip = await service.create_clip(
        session_id=1,
        marker_id=None,
        name='持久化剪辑 owner',
        requested_start_ms=20_000,
        requested_end_ms=70_000,
        confirm_keyframe=False,
        active_durations_ms={1: 120_000},
    )
    worker = HighlightWorker(
        database, clipper, FakeDanmakuClipper(), worker_id='worker', clock=lambda: 1_000
    )
    run_task = asyncio.create_task(worker.run_once())
    started = await asyncio.get_running_loop().run_in_executor(
        None, clipper.started.wait, 0.5
    )
    assert started
    try:
        outcome = await database.fetchone(
            'SELECT source_generation,outcome_state,acknowledged_at '
            'FROM owner_handoff_outcomes '
            "WHERE owner_kind='highlight' AND owner_id=? "
            "AND side_effect_key='ffmpeg_cut'",
            (clip.id,),
        )
        assert outcome is not None
        assert dict(outcome) == {
            'source_generation': 0,
            'outcome_state': 'in_flight',
            'acknowledged_at': None,
        }

        clipper.release.set()
        assert await run_task == clip.id
        assert (
            await database.scalar(
                "SELECT COUNT(*) FROM owner_handoff_outcomes "
                "WHERE owner_kind='highlight' AND owner_id=?",
                (clip.id,),
            )
            == 0
        )
    finally:
        clipper.release.set()
        await asyncio.gather(run_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_cancelled_coroutine_drains_ffmpeg_before_recovery_releases_lease(
    database: BiliUploadDatabase, tmp_path: Path
) -> None:
    root = tmp_path / 'records'
    root.mkdir()
    await seed_active_recording(database, root)
    clipper = BlockingClipper()
    service = HighlightService(database, recording_root=root, clipper=clipper)
    clip = await service.create_clip(
        session_id=1,
        marker_id=None,
        name='取消后排空高光',
        requested_start_ms=20_000,
        requested_end_ms=70_000,
        confirm_keyframe=False,
        active_durations_ms={1: 120_000},
    )
    worker = HighlightWorker(
        database, clipper, FakeDanmakuClipper(), worker_id='worker', clock=lambda: 1_000
    )
    deletion = LocalDeletionWorker(
        database,
        recording_root=root,
        clip_root=root / 'highlights',
        clock=lambda: 1_001,
    )
    run_task = asyncio.create_task(worker.run_once())
    recovery_task = None
    started = await asyncio.get_running_loop().run_in_executor(
        None, clipper.started.wait, 0.5
    )
    assert started

    try:
        run_task.cancel()
        await asyncio.sleep(0)
        assert not run_task.done()
        await deletion.request_clip(clip.id)
        recovery_task = asyncio.create_task(worker.recover_interrupted())
        await asyncio.sleep(0)
        assert not recovery_task.done()
        assert await deletion.run_once() == ('clip', clip.id)
        assert (
            await database.scalar(
                'SELECT lease_owner FROM highlight_clips WHERE id=?', (clip.id,)
            )
            == 'worker'
        )
        clipper.release.set()
        with pytest.raises(asyncio.CancelledError):
            await run_task
        assert recovery_task is not None
        assert await recovery_task == 0

        row = await database.fetchone(
            'SELECT state,deletion_state,lease_owner FROM highlight_clips WHERE id=?',
            (clip.id,),
        )
        assert row is not None
        assert dict(row) == {
            'state': 'processing',
            'deletion_state': 'requested',
            'lease_owner': None,
        }
        assert await deletion.run_once() == ('clip', clip.id)
        assert await database.scalar('SELECT COUNT(*) FROM highlight_clips') == 0
        assert clip.output_video_path is not None
        assert not Path(clip.output_video_path).exists()
    finally:
        clipper.release.set()
        await asyncio.gather(run_task, return_exceptions=True)
        if recovery_task is not None:
            await asyncio.gather(recovery_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_recovery_uses_persisted_claim_generation_after_repeated_delete(
    database: BiliUploadDatabase, tmp_path: Path
) -> None:
    root = tmp_path / 'records'
    root.mkdir()
    await seed_active_recording(database, root)
    clipper = FakeClipper()
    service = HighlightService(database, recording_root=root, clipper=clipper)
    clip = await service.create_clip(
        session_id=1,
        marker_id=None,
        name='重复删除高光',
        requested_start_ms=20_000,
        requested_end_ms=70_000,
        confirm_keyframe=False,
        active_durations_ms={1: 120_000},
    )
    await database.execute(
        "UPDATE highlight_clips SET state='processing',lease_owner='dead-worker',"
        'lease_generation=1,lease_until=9999,attempt=1 WHERE id=?',
        (clip.id,),
    )
    await database.execute(
        'INSERT INTO owner_handoff_outcomes('
        'owner_kind,owner_id,side_effect_key,source_generation,outcome_state,'
        "outcome_json,acknowledged_at) VALUES('highlight',?,'ffmpeg_cut',0,"
        "'in_flight','{}',NULL)",
        (clip.id,),
    )
    deletion = LocalDeletionWorker(
        database,
        recording_root=root,
        clip_root=root / 'highlights',
        clock=lambda: 1_001,
    )
    assert await deletion.request_clip(clip.id) == 1
    assert await deletion.request_clip(clip.id) == 2
    restarted = HighlightWorker(
        database,
        clipper,
        FakeDanmakuClipper(),
        worker_id='new-worker',
        clock=lambda: 1_002,
    )

    assert await restarted.recover_interrupted() == 1

    outcomes = await database.fetchall(
        'SELECT source_generation,outcome_state FROM owner_handoff_outcomes '
        "WHERE owner_kind='highlight' AND owner_id=? ORDER BY source_generation",
        (clip.id,),
    )
    assert [dict(row) for row in outcomes] == [
        {'source_generation': 0, 'outcome_state': 'cancelled_local'}
    ]


@pytest.mark.asyncio
async def test_worker_cuts_the_same_final_file_used_by_preview(
    database: BiliUploadDatabase, tmp_path: Path
) -> None:
    root = tmp_path / 'records'
    root.mkdir()
    await seed_active_recording(database, root)
    clipper = FakeClipper()
    service = HighlightService(database, recording_root=root, clipper=clipper)
    clip = await service.create_clip(
        session_id=1,
        marker_id=None,
        name='成品文件',
        requested_start_ms=20_000,
        requested_end_ms=70_000,
        confirm_keyframe=False,
        active_durations_ms={1: 120_000},
    )
    final_path = root / 'room-100-final.mp4'
    final_path.write_bytes(b'final-video')
    await database.execute(
        "UPDATE recording_parts SET artifact_state='ready',final_path=? WHERE id=1",
        (str(final_path),),
    )
    worker = HighlightWorker(
        database, clipper, FakeDanmakuClipper(), worker_id='worker', clock=lambda: 1_000
    )

    assert await worker.run_once() == clip.id

    worker_sources = clipper.inspect_calls[-1][0]
    assert worker_sources[0].path == str(final_path)


@pytest.mark.asyncio
async def test_worker_retries_incomplete_ffprobe_metadata_for_growing_recording(
    database: BiliUploadDatabase, tmp_path: Path
) -> None:
    root = tmp_path / 'records'
    root.mkdir()
    await seed_active_recording(database, root)
    service = HighlightService(database, recording_root=root, clipper=FakeClipper())
    clip = await service.create_clip(
        session_id=1,
        marker_id=None,
        name='录制中片段',
        requested_start_ms=20_000,
        requested_end_ms=70_000,
        confirm_keyframe=False,
        active_durations_ms={1: 120_000},
    )
    await database.execute(
        'UPDATE highlight_clips SET file_size_bytes=999 WHERE id=?', (clip.id,)
    )
    failing = FakeClipper()
    failing.inspect = lambda *args, **kwargs: (_ for _ in ()).throw(
        HighlightCutError('ffprobe 返回了无效的视频流信息')
    )
    worker = HighlightWorker(
        database, failing, FakeDanmakuClipper(), worker_id='worker', clock=lambda: 1_000
    )

    assert await worker.run_once() == clip.id

    row = await database.fetchone(
        'SELECT state,next_attempt_at,error_message,file_size_bytes '
        'FROM highlight_clips WHERE id=?',
        (clip.id,),
    )
    assert row is not None
    assert row['state'] == 'queued'
    assert row['next_attempt_at'] > 1_000
    assert '无效的视频流信息' in row['error_message']
    assert row['file_size_bytes'] is None


@pytest.mark.asyncio
async def test_worker_keeps_retrying_incomplete_ffprobe_metadata_after_source_closes(
    database: BiliUploadDatabase, tmp_path: Path
) -> None:
    root = tmp_path / 'records'
    root.mkdir()
    await seed_active_recording(database, root)
    service = HighlightService(database, recording_root=root, clipper=FakeClipper())
    clip = await service.create_clip(
        session_id=1,
        marker_id=None,
        name='结束边界片段',
        requested_start_ms=20_000,
        requested_end_ms=70_000,
        confirm_keyframe=False,
        active_durations_ms={1: 120_000},
    )
    await database.execute(
        "UPDATE recording_parts SET artifact_state='ready',final_path=source_path "
        'WHERE id=1'
    )
    await database.execute(
        'UPDATE highlight_clips SET attempt=4 WHERE id=?', (clip.id,)
    )
    failing = FakeClipper()
    failing.inspect = lambda *args, **kwargs: (_ for _ in ()).throw(
        HighlightCutError('ffprobe 返回了无效的视频流信息')
    )
    worker = HighlightWorker(
        database, failing, FakeDanmakuClipper(), worker_id='worker', clock=lambda: 1_000
    )

    assert await worker.run_once() == clip.id

    row = await database.fetchone(
        'SELECT state,next_attempt_at FROM highlight_clips WHERE id=?', (clip.id,)
    )
    assert row is not None
    assert row['state'] == 'queued'
    assert row['next_attempt_at'] > 1_000


@pytest.mark.asyncio
async def test_worker_stops_retrying_invalid_metadata_after_finalization_grace(
    database: BiliUploadDatabase, tmp_path: Path
) -> None:
    root = tmp_path / 'records'
    root.mkdir()
    await seed_active_recording(database, root)
    service = HighlightService(database, recording_root=root, clipper=FakeClipper())
    clip = await service.create_clip(
        session_id=1,
        marker_id=None,
        name='损坏片段',
        requested_start_ms=20_000,
        requested_end_ms=70_000,
        confirm_keyframe=False,
        active_durations_ms={1: 120_000},
    )
    await database.execute(
        "UPDATE recording_parts SET artifact_state='ready',final_path=source_path,"
        'updated_at=1 WHERE id=1'
    )
    failing = FakeClipper()
    failing.inspect = lambda *args, **kwargs: (_ for _ in ()).throw(
        HighlightCutError('ffprobe 返回了无效的视频流信息')
    )
    worker = HighlightWorker(
        database, failing, FakeDanmakuClipper(), worker_id='worker', clock=lambda: 1_000
    )

    assert await worker.run_once() == clip.id

    assert (
        await database.scalar(
            'SELECT state FROM highlight_clips WHERE id=?', (clip.id,)
        )
        == 'failed'
    )


@pytest.mark.asyncio
async def test_worker_recovers_stale_partial_and_valid_final_output(
    database: BiliUploadDatabase, tmp_path: Path
) -> None:
    root = tmp_path / 'records'
    root.mkdir()
    await seed_active_recording(database, root)
    clipper = FakeClipper()
    service = HighlightService(database, recording_root=root, clipper=clipper)
    clip = await service.create_clip(
        session_id=1,
        marker_id=None,
        name='恢复任务',
        requested_start_ms=20_000,
        requested_end_ms=70_000,
        confirm_keyframe=False,
        active_durations_ms={1: 120_000},
    )
    assert clip.output_video_path is not None
    partial = Path(clip.output_video_path + '.partial')
    partial.parent.mkdir(parents=True, exist_ok=True)
    partial.write_bytes(b'incomplete')
    await database.execute(
        "UPDATE highlight_clips SET state='processing',lease_owner='old',"
        'lease_until=1,file_size_bytes=999 WHERE id=?',
        (clip.id,),
    )
    await database.execute(
        'INSERT INTO owner_handoff_outcomes('
        'owner_kind,owner_id,side_effect_key,source_generation,outcome_state,'
        "outcome_json,acknowledged_at) VALUES('highlight',?,'ffmpeg_cut',0,"
        "'in_flight','{}',NULL)",
        (clip.id,),
    )
    worker = HighlightWorker(
        database,
        clipper,
        FakeDanmakuClipper(),
        worker_id='worker',
        clock=lambda: 1_000,
        artifact_probe=lambda path: (
            RecoveredArtifact(path, Path(path).stat().st_size, 52)
            if Path(path).is_file()
            else None
        ),
    )

    assert await worker.recover_interrupted() == 1
    assert not partial.exists()
    reset = await database.fetchone(
        'SELECT state,file_size_bytes FROM highlight_clips WHERE id=?', (clip.id,)
    )
    assert reset is not None
    assert dict(reset) == {'state': 'queued', 'file_size_bytes': None}
    assert await database.scalar('SELECT COUNT(*) FROM owner_handoff_outcomes') == 0

    final = Path(clip.output_video_path)
    final.write_bytes(b'complete')
    await database.execute(
        "UPDATE highlight_clips SET state='processing',lease_owner='old',"
        'lease_until=1 WHERE id=?',
        (clip.id,),
    )
    await database.execute(
        'INSERT INTO owner_handoff_outcomes('
        'owner_kind,owner_id,side_effect_key,source_generation,outcome_state,'
        "outcome_json,acknowledged_at) VALUES('highlight',?,'ffmpeg_cut',0,"
        "'in_flight','{}',NULL)",
        (clip.id,),
    )
    assert await worker.recover_interrupted() == 1
    recovered = await database.fetchone(
        'SELECT state,file_size_bytes FROM highlight_clips WHERE id=?', (clip.id,)
    )
    assert recovered is not None
    assert dict(recovered) == {'state': 'ready', 'file_size_bytes': len(b'complete')}
    assert await database.scalar('SELECT COUNT(*) FROM owner_handoff_outcomes') == 0
    assert clipper.cut_calls == []


@pytest.mark.asyncio
async def test_backfill_file_sizes_is_bounded_and_runs_in_executor(
    database: BiliUploadDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    def seed(connection: sqlite3.Connection) -> None:
        for clip_id in range(1, 121):
            connection.execute(
                'INSERT INTO highlight_clips('
                'id,room_id,name,requested_start_ms,requested_end_ms,'
                'output_video_path,state,file_size_bytes,created_at,updated_at) '
                "VALUES(?,100,?,0,1000,?,'ready',?,?,?)",
                (
                    clip_id,
                    '片段 {}'.format(clip_id),
                    '/clips/clip-{}.mp4'.format(clip_id),
                    10 if clip_id == 1 else None,
                    clip_id,
                    clip_id,
                ),
            )

    await database.write(seed)
    caller_thread = threading.get_ident()
    checked_ids = []
    worker_threads = []

    def fake_getsize(path: str) -> int:
        clip_id = int(Path(path).stem.split('-')[-1])
        checked_ids.append(clip_id)
        worker_threads.append(threading.get_ident())
        if clip_id == 50:
            raise FileNotFoundError(path)
        if clip_id == 51:
            raise ValueError('invalid legacy path')
        return clip_id * 10

    monkeypatch.setattr(os.path, 'getsize', fake_getsize)
    worker = HighlightWorker(
        database, FakeClipper(), FakeDanmakuClipper(), worker_id='worker'
    )

    assert await worker.backfill_file_sizes(limit=0) == 0
    assert checked_ids == []

    messages = []
    sink = logger.add(messages.append, format='{message}')
    try:
        updated = await worker.backfill_file_sizes(limit=1_000)
    finally:
        logger.remove(sink)

    assert updated == 98
    assert checked_ids == list(range(2, 102))
    assert worker_threads and all(value != caller_thread for value in worker_threads)
    assert any('highlight_clip_size_backfill_skipped' in str(item) for item in messages)
    assert all('/clips/clip-50.mp4' not in str(item) for item in messages)
    assert (
        await database.scalar('SELECT file_size_bytes FROM highlight_clips WHERE id=2')
        == 20
    )
    assert (
        await database.scalar('SELECT file_size_bytes FROM highlight_clips WHERE id=50')
        is None
    )
    assert (
        await database.scalar('SELECT file_size_bytes FROM highlight_clips WHERE id=51')
        is None
    )
    assert (
        await database.scalar(
            'SELECT file_size_bytes FROM highlight_clips WHERE id=102'
        )
        is None
    )


@pytest.mark.asyncio
async def test_delete_clip_cancels_pending_and_removes_only_ready_outputs(
    database: BiliUploadDatabase, tmp_path: Path
) -> None:
    root = tmp_path / 'records'
    root.mkdir()
    source_video = await seed_active_recording(database, root)
    service = HighlightService(database, recording_root=root, clipper=FakeClipper())
    clip = await service.create_clip(
        session_id=1,
        marker_id=None,
        name='删除测试',
        requested_start_ms=20_000,
        requested_end_ms=70_000,
        confirm_keyframe=False,
        active_durations_ms={1: 120_000},
    )
    await database.execute(
        'UPDATE highlight_clips SET file_size_bytes=999 WHERE id=?', (clip.id,)
    )

    assert await service.delete_clip(clip.id) == 'cancelled'
    cancelled = await database.fetchone(
        'SELECT state,file_size_bytes FROM highlight_clips WHERE id=?', (clip.id,)
    )
    assert cancelled is not None
    assert dict(cancelled) == {'state': 'cancelled', 'file_size_bytes': None}

    assert clip.output_video_path is not None
    assert clip.output_xml_path is not None
    Path(clip.output_video_path).parent.mkdir(parents=True, exist_ok=True)
    Path(clip.output_video_path).write_bytes(b'output')
    Path(clip.output_xml_path).write_text('<i/>', encoding='utf8')
    await database.execute(
        "UPDATE highlight_clips SET state='ready',file_size_bytes=6 WHERE id=?",
        (clip.id,),
    )
    upload_session_id = await service.ensure_upload_session(clip.id)

    assert await service.delete_clip(clip.id) == 'deleted'
    assert (
        await database.scalar(
            'SELECT COUNT(*) FROM highlight_clips WHERE id=?', (clip.id,)
        )
        == 0
    )
    assert not Path(clip.output_video_path).exists()
    assert not Path(clip.output_xml_path).exists()
    assert (
        await database.scalar(
            'SELECT COUNT(*) FROM recording_sessions WHERE id=?', (upload_session_id,)
        )
        == 0
    )
    assert source_video.exists()


@pytest.mark.asyncio
async def test_delete_clip_cancels_local_upload_job_and_removes_files(
    database: BiliUploadDatabase, tmp_path: Path
) -> None:
    root = tmp_path / 'records'
    root.mkdir()
    await seed_active_recording(database, root)
    service = HighlightService(database, recording_root=root, clipper=FakeClipper())
    clip = await service.create_clip(
        session_id=1,
        marker_id=None,
        name='投稿中的片段',
        requested_start_ms=20_000,
        requested_end_ms=70_000,
        confirm_keyframe=False,
        active_durations_ms={1: 120_000},
    )
    assert clip.output_video_path is not None
    assert clip.output_xml_path is not None
    Path(clip.output_video_path).parent.mkdir(parents=True, exist_ok=True)
    Path(clip.output_video_path).write_bytes(b'output')
    Path(clip.output_xml_path).write_text('<i/>', encoding='utf8')
    await database.execute(
        "UPDATE highlight_clips SET state='ready',file_size_bytes=6 WHERE id=?",
        (clip.id,),
    )
    upload_session_id = await service.ensure_upload_session(clip.id)
    await database.execute(
        'INSERT INTO bili_accounts('
        'id,uid,display_name,credential_ciphertext,credential_version,key_id,'
        'state,created_at,updated_at) '
        "VALUES(1,1000,'投稿账号',X'00',1,'test','active',1,1)"
    )
    await database.execute(
        'INSERT INTO upload_jobs('
        'session_id,account_id,policy_snapshot_json,state,submit_state,'
        'created_at,updated_at) '
        "VALUES(?,1,'{}','ready','prepared',1,1)",
        (upload_session_id,),
    )

    result = await service.delete_clip(clip.id)

    assert result == 'deleted'
    assert not Path(clip.output_video_path).exists()
    assert not Path(clip.output_xml_path).exists()
    assert (
        await database.scalar(
            'SELECT COUNT(*) FROM highlight_clips WHERE id=?', (clip.id,)
        )
        == 0
    )
    assert await database.scalar('SELECT COUNT(*) FROM upload_jobs') == 0


@pytest.mark.asyncio
async def test_delete_clip_keeps_retryable_database_record_when_unlink_fails(
    database: BiliUploadDatabase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / 'records'
    root.mkdir()
    await seed_active_recording(database, root)
    service = HighlightService(database, recording_root=root, clipper=FakeClipper())
    clip = await service.create_clip(
        session_id=1,
        marker_id=None,
        name='待删除片段',
        requested_start_ms=20_000,
        requested_end_ms=70_000,
        confirm_keyframe=False,
        active_durations_ms={1: 120_000},
    )
    assert clip.output_video_path is not None
    Path(clip.output_video_path).parent.mkdir(parents=True, exist_ok=True)
    Path(clip.output_video_path).write_bytes(b'output')
    await database.execute(
        "UPDATE highlight_clips SET state='ready',file_size_bytes=6 WHERE id=?",
        (clip.id,),
    )
    upload_session_id = await service.ensure_upload_session(clip.id)

    async def fail_remove(*args, **kwargs) -> None:
        raise PermissionError('NAS temporarily refused deletion')

    monkeypatch.setattr(service, '_remove_clip_outputs', fail_remove)
    with pytest.raises(PermissionError, match='temporarily refused'):
        await service.delete_clip(clip.id)

    assert (
        await database.scalar(
            'SELECT file_size_bytes FROM highlight_clips WHERE id=?', (clip.id,)
        )
        is None
    )
    assert (
        await database.scalar(
            'SELECT COUNT(*) FROM recording_sessions WHERE id=?', (upload_session_id,)
        )
        == 1
    )

    monkeypatch.undo()
    assert await service.delete_clip(clip.id) == 'deleted'
    assert not Path(clip.output_video_path).exists()
    assert (
        await database.scalar(
            'SELECT COUNT(*) FROM recording_sessions WHERE id=?', (upload_session_id,)
        )
        == 0
    )
