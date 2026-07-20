import os
import sqlite3
import threading
from pathlib import Path
from typing import AsyncIterator, Sequence

import pytest
import pytest_asyncio
from loguru import logger

from blrec.bili_upload.artifact_recovery import RecoveredArtifact
from blrec.bili_upload.database import BiliUploadDatabase
from blrec.bili_upload.highlight_cut import (
    ClipInspection,
    ClipSource,
    CutArtifact,
    HighlightCutError,
    InspectedClipSource,
    MediaProfile,
)
from blrec.bili_upload.highlight_danmaku import DanmakuClipSource, DanmakuCutResult
from blrec.bili_upload.highlight_worker import HighlightWorker
from blrec.bili_upload.highlights import (
    HighlightConfirmationRequired,
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
    assert worker_sources[0].duration_ms == 70_000
    assert worker_sources[0].keyframes_ms == (18_000,)


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

    final = Path(clip.output_video_path)
    final.write_bytes(b'complete')
    await database.execute(
        "UPDATE highlight_clips SET state='processing',lease_owner='old',"
        'lease_until=1 WHERE id=?',
        (clip.id,),
    )
    assert await worker.recover_interrupted() == 1
    recovered = await database.fetchone(
        'SELECT state,file_size_bytes FROM highlight_clips WHERE id=?', (clip.id,)
    )
    assert recovered is not None
    assert dict(recovered) == {'state': 'ready', 'file_size_bytes': len(b'complete')}
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
