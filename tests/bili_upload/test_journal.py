from pathlib import Path
from types import SimpleNamespace
from typing import AsyncIterator, List

import pytest
import pytest_asyncio

from blrec.bili_upload.database import BiliUploadDatabase
from blrec.bili_upload.journal import RecordingJournalBridge, RecordingJournalListener


@pytest_asyncio.fixture
async def database(tmp_path: Path) -> AsyncIterator[BiliUploadDatabase]:
    value = BiliUploadDatabase(str(tmp_path / 'blrec.sqlite3'))
    await value.open()
    try:
        yield value
    finally:
        await value.close()


@pytest.mark.asyncio
async def test_part_order_is_creation_order_not_completion_order(database) -> None:
    journal = RecordingJournalBridge(database, clock=lambda: 1_000)
    run_id = await journal.recording_started(100, live_start_time=900)
    await journal.video_created(run_id, '/rec/p1.flv', record_start_time=901)
    await journal.video_created(run_id, '/rec/p2.flv', record_start_time=902)

    await journal.video_completed(run_id, '/rec/p2.flv')
    await journal.video_completed(run_id, '/rec/p1.flv')

    parts = await journal.parts_for_run(run_id)
    assert [(part.part_index, part.source_path) for part in parts] == [
        (1, '/rec/p1.flv'),
        (2, '/rec/p2.flv'),
    ]


@pytest.mark.asyncio
async def test_restart_of_same_live_reuses_session_and_continues_part_numbers(
    database,
) -> None:
    journal = RecordingJournalBridge(database, clock=lambda: 1_000)
    first_run = await journal.recording_started(100, live_start_time=900)
    await journal.video_created(first_run, '/rec/p1.flv', record_start_time=901)
    await journal.video_completed(first_run, '/rec/p1.flv')
    await journal.video_postprocessed(first_run, '/rec/p1.flv', '/rec/p1.flv')
    await journal.recording_cancelled(first_run)

    restarted_run = await journal.recording_started(100, live_start_time=900)
    await journal.video_created(restarted_run, '/rec/p2.flv', record_start_time=902)

    first_session = await journal.session_for_run(first_run)
    restarted_session = await journal.session_for_run(restarted_run)
    restarted_parts = await journal.parts_for_run(restarted_run)
    assert restarted_session.id == first_session.id
    assert restarted_session.state == 'open'
    assert [(part.part_index, part.source_path) for part in restarted_parts] == [
        (2, '/rec/p2.flv')
    ]


@pytest.mark.asyncio
async def test_list_sessions_supports_offset_and_total(database) -> None:
    journal = RecordingJournalBridge(database, clock=lambda: 1_000)
    for index in range(3):
        await journal.recording_started(100 + index, live_start_time=900 + index)

    sessions = await journal.list_sessions(limit=1, offset=1)

    assert await journal.count_sessions() == 3
    assert len(sessions) == 1
    assert sessions[0].room_id == 101


@pytest.mark.asyncio
async def test_list_sessions_rejects_negative_offset(database) -> None:
    journal = RecordingJournalBridge(database, clock=lambda: 1_000)

    with pytest.raises(ValueError, match='offset must not be negative'):
        await journal.list_sessions(offset=-1)


@pytest.mark.asyncio
async def test_missing_live_start_time_reuses_open_surrogate_session(database) -> None:
    journal = RecordingJournalBridge(database, clock=lambda: 1_000)

    first_run = await journal.recording_started(100, live_start_time=0)
    restarted_run = await journal.recording_started(100, live_start_time=0)

    first_session = await journal.session_for_run(first_run)
    restarted_session = await journal.session_for_run(restarted_run)
    assert restarted_session.id == first_session.id
    assert (
        restarted_session.broadcast_session_key == first_session.broadcast_session_key
    )
    assert restarted_session.broadcast_session_key.startswith('100:local:')


@pytest.mark.asyncio
async def test_reconcile_marks_crash_interrupted_file_for_manual_review(
    database, tmp_path: Path
) -> None:
    source = tmp_path / 'interrupted.flv'
    source.write_bytes(b'partial recording')
    journal = RecordingJournalBridge(database, clock=lambda: 1_000)
    run_id = await journal.recording_started(100, live_start_time=900)
    await journal.video_created(run_id, str(source), record_start_time=901)

    await journal.reconcile_open_sessions()

    session = await journal.session_for_run(run_id)
    part = (await journal.parts_for_run(run_id))[0]
    assert session.state == 'manual_review'
    assert part.artifact_state == 'manual_review'
    assert (
        await database.scalar(
            'SELECT COUNT(*) FROM recording_runs '
            "WHERE id=? AND state='cancelled' AND ended_at IS NOT NULL",
            (run_id,),
        )
        == 1
    )


@pytest.mark.asyncio
async def test_remux_path_becomes_final_only_after_postprocess(
    database, tmp_path: Path
) -> None:
    source = tmp_path / 'part.flv'
    final = tmp_path / 'part.mp4'
    source.write_bytes(b'source')
    final.write_bytes(b'final')
    journal = RecordingJournalBridge(database, clock=lambda: 1_000)
    run_id = await journal.recording_started(100, live_start_time=900)
    await journal.video_created(run_id, str(source), record_start_time=901)
    await journal.video_completed(run_id, str(source))

    part = (await journal.parts_for_run(run_id))[0]
    assert part.final_path is None
    assert part.artifact_state == 'postprocessing'

    source.unlink()
    await journal.video_postprocessed(run_id, str(source), str(final))

    part = (await journal.parts_for_run(run_id))[0]
    assert part.final_path == str(final)
    assert part.artifact_state == 'ready'
    assert part.source_exists is False


@pytest.mark.asyncio
async def test_session_closes_only_after_recording_and_postprocessing_finish(
    database,
) -> None:
    journal = RecordingJournalBridge(database, clock=lambda: 1_000)
    run_id = await journal.recording_started(100, live_start_time=900)
    await journal.video_created(run_id, '/rec/p1.flv', record_start_time=901)
    await journal.video_completed(run_id, '/rec/p1.flv')

    await journal.recording_finished(run_id)
    assert (await journal.session_for_run(run_id)).state == 'open'

    await journal.video_postprocessed(run_id, '/rec/p1.flv', '/rec/p1.flv')
    assert (await journal.session_for_run(run_id)).state == 'closed'


@pytest.mark.asyncio
async def test_postprocessing_failure_is_a_terminal_visible_part_state(
    database,
) -> None:
    journal = RecordingJournalBridge(database, clock=lambda: 1_000)
    run_id = await journal.recording_started(100, live_start_time=900)
    await journal.video_created(run_id, '/rec/p1.flv', record_start_time=901)
    await journal.video_completed(run_id, '/rec/p1.flv')
    await journal.recording_finished(run_id)

    await journal.video_postprocessing_failed(
        run_id, '/rec/p1.flv', RuntimeError('invalid FLV')
    )

    session = await journal.session_for_run(run_id)
    assert session.state == 'closed'
    assert session.parts[0].artifact_state == 'failed'
    assert session.parts[0].error_message == 'RuntimeError: invalid FLV'


@pytest.mark.asyncio
async def test_replayed_event_is_idempotent(database) -> None:
    journal = RecordingJournalBridge(database, clock=lambda: 1_000)
    run_id = await journal.recording_started(
        100, live_start_time=900, event_id='recording-started'
    )

    await journal.video_created(
        run_id, '/rec/p1.flv', record_start_time=901, event_id='video-created'
    )
    await journal.video_created(
        run_id, '/rec/p1.flv', record_start_time=901, event_id='video-created'
    )

    assert len(await journal.parts_for_run(run_id)) == 1
    assert (
        await database.scalar(
            "SELECT COUNT(*) FROM event_journal WHERE id='video-created'"
        )
        == 1
    )


@pytest.mark.asyncio
async def test_completed_danmaku_is_bound_to_matching_part(
    database, tmp_path: Path
) -> None:
    source = tmp_path / 'p1.flv'
    xml = tmp_path / 'p1.xml'
    source.write_bytes(b'source')
    xml.write_text('<i><d>one</d></i>')
    journal = RecordingJournalBridge(database, clock=lambda: 1_000)
    run_id = await journal.recording_started(100, live_start_time=900)
    await journal.video_created(run_id, str(source), record_start_time=901)

    await journal.danmaku_completed(run_id, str(xml))

    part = (await journal.parts_for_run(run_id))[0]
    assert part.xml_path == str(xml)
    assert part.xml_completed is True
    assert part.danmaku_count == 1


@pytest.mark.asyncio
async def test_session_snapshot_and_part_metrics_are_persisted(
    database, tmp_path: Path
) -> None:
    now = [1_000]
    source = tmp_path / 'part.flv'
    final = tmp_path / 'part.mp4'
    xml = tmp_path / 'part.xml'
    cover = tmp_path / 'cover.jpg'
    source.write_bytes(b'source')
    final.write_bytes(b'final-video')
    xml.write_text('<i><d>one</d><gift>ignore</gift><d>two</d></i>')
    cover.write_bytes(b'cover')
    journal = RecordingJournalBridge(database, clock=lambda: now[0])

    run_id = await journal.recording_started(
        100,
        live_start_time=900,
        metadata=SimpleNamespace(
            title='开播标题',
            cover_url='https://example.invalid/cover.jpg',
            anchor_uid=42,
            anchor_name='主播',
            area_id=1,
            area_name='单机游戏',
            parent_area_id=2,
            parent_area_name='游戏',
        ),
    )
    await journal.cover_downloaded(run_id, str(cover))
    await journal.video_created(run_id, str(source), record_start_time=901)
    now[0] = 911
    await journal.video_completed(run_id, str(source))
    await journal.video_postprocessed(run_id, str(source), str(final))
    await journal.danmaku_completed(run_id, str(xml))
    now[0] = 912
    await journal.recording_finished(run_id)

    session = await journal.session_for_run(run_id)
    part = session.parts[0]
    assert session.title == '开播标题'
    assert session.cover_url == 'https://example.invalid/cover.jpg'
    assert session.cover_path == str(cover)
    assert session.anchor_uid == 42
    assert session.anchor_name == '主播'
    assert session.area_name == '单机游戏'
    assert session.parent_area_name == '游戏'
    assert session.live_end_time == 912
    assert session.part_count == 1
    assert session.danmaku_count == 2
    assert session.total_file_size_bytes == len(b'final-video')
    assert session.record_duration_seconds == 10
    assert part.record_end_time == 911
    assert part.record_duration_seconds == 10
    assert part.file_size_bytes == len(b'final-video')
    assert part.danmaku_count == 2


@pytest.mark.asyncio
async def test_upload_progress_is_joined_to_its_recording_session(database) -> None:
    journal = RecordingJournalBridge(database, clock=lambda: 1_000)
    run_id = await journal.recording_started(100, live_start_time=900)
    await journal.video_created(run_id, '/rec/p1.flv', record_start_time=901)
    session = await journal.session_for_run(run_id)
    await database.execute(
        'INSERT INTO bili_accounts('
        'id,uid,display_name,credential_ciphertext,credential_version,key_id,state,'
        'pause_reason,created_at,updated_at,avatar_url,credential_expires_at) '
        "VALUES(7,42,'投稿账号',?,1,'key','active',NULL,900,900,'',0)",
        (b'encrypted',),
    )
    await database.execute(
        'INSERT INTO upload_jobs('
        'id,session_id,account_id,policy_snapshot_json,state,submit_state,'
        'comment_branch_state,danmaku_branch_state,aid,bvid,review_reason,'
        'attempt,next_attempt_at,created_at,updated_at) '
        "VALUES(9,?,7,'{}','waiting_review','confirmed','pending','pending',"
        "123,'BV1test','等待 B 站审核',2,1100,1001,1050)",
        (session.id,),
    )
    await database.execute(
        'INSERT INTO upload_parts('
        'id,job_id,part_index,source_path,final_path,xml_path,artifact_state,'
        'upload_state,danmaku_import_state,remote_filename,cid) '
        "VALUES(10,9,1,'/rec/p1.flv','/rec/p1.mp4','/rec/p1.xml','ready',"
        "'confirmed','pending','remote-p1',NULL)"
    )
    for index, state in enumerate(
        ('confirmed', 'prepared', 'in_flight', 'unknown_outcome', 'failed_permanent')
    ):
        await database.execute(
            'INSERT INTO danmaku_items('
            'part_id,xml_identity,original_index,progress_ms,mode,fontsize,color,'
            'content,priority,request_fingerprint,state,error_message) '
            'VALUES(10,?,?,?,?,?,?,?,?,?,?,?)',
            (
                'xml-1',
                index,
                index * 1000,
                1,
                25,
                16_777_215,
                '弹幕 {}'.format(index),
                0,
                'fingerprint-{}'.format(index),
                state,
                '远端结果未知' if state == 'unknown_outcome' else None,
            ),
        )

    jobs = await journal.upload_jobs_for_sessions((session.id, 999))

    assert set(jobs) == {session.id}
    job = jobs[session.id]
    assert (job.state, job.submit_state, job.account_display_name) == (
        'waiting_review',
        'confirmed',
        '投稿账号',
    )
    assert (job.parts[0].part_index, job.parts[0].upload_state) == (1, 'confirmed')
    assert job.parts[0].remote_filename == 'remote-p1'
    assert job.parts[0].cid is None
    assert (
        job.danmaku_total,
        job.danmaku_confirmed,
        job.danmaku_pending,
        job.danmaku_unknown,
        job.danmaku_failed,
    ) == (5, 1, 2, 1, 1)
    assert len(job.unknown_danmaku_items) == 1
    assert job.unknown_danmaku_items[0].content == '弹幕 3'
    assert job.unknown_danmaku_items[0].part_index == 1


class FakeEmitter:
    def __init__(self) -> None:
        self.listeners: List[object] = []

    def add_listener(self, listener: object) -> None:
        self.listeners.append(listener)

    def remove_listener(self, listener: object) -> None:
        self.listeners.remove(listener)


@pytest.mark.asyncio
async def test_listener_persists_recorder_and_postprocessor_lifecycle(
    database, tmp_path: Path
) -> None:
    journal = RecordingJournalBridge(database, clock=lambda: 1_000)
    source = tmp_path / 'p1.flv'
    final = tmp_path / 'p1.mp4'
    xml = tmp_path / 'p1.xml'
    cover = tmp_path / 'cover.jpg'
    source.write_bytes(b'source')
    final.write_bytes(b'final')
    xml.write_text('<i><d>one</d></i>')
    cover.write_bytes(b'cover')
    recorder = FakeEmitter()
    recorder.live = SimpleNamespace(
        room_id=100,
        room_info=SimpleNamespace(
            room_id=100,
            live_start_time=900,
            title='直播标题',
            cover='https://example.invalid/cover.jpg',
            area_id=1,
            area_name='单机游戏',
            parent_area_id=2,
            parent_area_name='游戏',
        ),
        user_info=SimpleNamespace(uid=42, name='主播'),
    )
    recorder.record_start_time = 901
    postprocessor = FakeEmitter()
    listener = RecordingJournalListener(
        journal,
        recorder,  # type: ignore[arg-type]
        postprocessor,  # type: ignore[arg-type]
    )

    await listener.on_recording_started(recorder)  # type: ignore[arg-type]
    await listener.on_video_file_created(  # type: ignore[arg-type]
        recorder, str(source)
    )
    await listener.on_video_file_completed(  # type: ignore[arg-type]
        recorder, str(source)
    )
    await listener.on_danmaku_file_completed(  # type: ignore[arg-type]
        recorder, str(xml)
    )
    await listener.on_cover_image_downloaded(  # type: ignore[arg-type]
        recorder, str(cover)
    )
    await listener.on_video_postprocessing_result(  # type: ignore[arg-type]
        postprocessor, str(source), str(final)
    )
    assert listener._source_runs == {}
    await listener.on_recording_finished(recorder)  # type: ignore[arg-type]

    sessions = await journal.list_sessions()
    assert len(sessions) == 1
    assert sessions[0].state == 'closed'
    assert sessions[0].title == '直播标题'
    assert sessions[0].anchor_name == '主播'
    assert sessions[0].cover_path == str(cover)
    assert sessions[0].parts[0].final_path == str(final)
    assert sessions[0].parts[0].xml_path == str(xml)
    assert sessions[0].parts[0].danmaku_count == 1

    listener.close()
    assert recorder.listeners == []
    assert postprocessor.listeners == []
