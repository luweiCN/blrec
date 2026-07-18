from pathlib import Path
from types import SimpleNamespace
from typing import AsyncIterator, List

import pytest
import pytest_asyncio

from blrec.bili_upload.artifact_recovery import RecoveredArtifact
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


async def seed_upload_policy(
    database: BiliUploadDatabase, *, room_id: int = 100, enabled: bool = True
) -> None:
    await database.execute(
        "INSERT INTO bili_accounts("
        "id,uid,display_name,credential_ciphertext,credential_version,key_id,"
        "state,created_at,updated_at) "
        "VALUES(1,42,'投稿账号',X'00',1,'k','active',1,1)"
    )
    await database.execute(
        'INSERT INTO bili_account_selection(id,primary_account_id) VALUES(1,1)'
    )
    await database.execute(
        'INSERT INTO room_upload_policies('
        'room_id,account_mode,account_id,enabled,title_template,'
        'description_template,part_title_template,dynamic_template,tid,tags,'
        'creation_statement_id,original_authorization,copyright,source,'
        'is_only_self,publish_dynamic,no_reprint,up_selection_reply,'
        'up_close_reply,up_close_danmu,auto_comment,danmaku_backfill,'
        'filter_json,created_at,updated_at) '
        "VALUES(?,'primary',NULL,?,'{{ title }} 录播','',"
        "'P{{ part_index }}','',17,'直播,录播',-1,1,1,'',0,0,1,0,0,0,0,0,"
        "'{}',1,1)",
        (room_id, int(enabled)),
    )


@pytest.mark.asyncio
async def test_recording_start_does_not_freeze_room_upload_policy(database) -> None:
    await seed_upload_policy(database)
    journal = RecordingJournalBridge(database, clock=lambda: 1_000)

    await journal.recording_started(100, live_start_time=900)
    await database.execute(
        'UPDATE room_upload_policies SET enabled=0 WHERE room_id=100'
    )
    await journal.recording_started(100, live_start_time=901)

    rows = await database.fetchall(
        'SELECT upload_intent,upload_decision FROM recording_sessions ORDER BY id'
    )
    assert [tuple(row) for row in rows] == [
        ('none', 'follow_room'),
        ('none', 'follow_room'),
    ]


@pytest.mark.asyncio
async def test_list_sessions_derives_current_upload_intent(database) -> None:
    await seed_upload_policy(database)
    journal = RecordingJournalBridge(database, clock=lambda: 1_000)
    run_id = await journal.recording_started(100, live_start_time=900)
    session = await journal.session_for_run(run_id)

    assert (await journal.list_sessions())[0].upload_intent == 'auto'

    await database.execute(
        "UPDATE recording_sessions SET upload_decision='upload' WHERE id=?",
        (session.id,),
    )
    assert (await journal.list_sessions())[0].upload_intent == 'upload'

    await database.execute(
        "UPDATE recording_sessions SET upload_decision='skip' WHERE id=?",
        (session.id,),
    )
    assert (await journal.list_sessions())[0].upload_intent == 'skip'

    await database.execute(
        "UPDATE recording_sessions SET upload_decision='follow_room' WHERE id=?",
        (session.id,),
    )
    await database.execute(
        'UPDATE room_upload_policies SET enabled=0 WHERE room_id=100'
    )
    assert (await journal.list_sessions())[0].upload_intent == 'none'

    await database.execute(
        'UPDATE room_upload_policies SET enabled=1 WHERE room_id=100'
    )
    await database.execute(
        'INSERT INTO upload_suppressions('
        'session_id,reason,manager_subject,created_at) VALUES(?,?,?,?)',
        (session.id, 'operator', 'owner', 1_001),
    )
    assert (await journal.list_sessions())[0].upload_intent == 'skip'


@pytest.mark.asyncio
async def test_video_created_records_local_media_timeline_anchor(database) -> None:
    journal = RecordingJournalBridge(database, clock=lambda: 1_000.250)
    run_id = await journal.recording_started(100, live_start_time=900)

    await journal.video_created(run_id, '/rec/p1.flv', record_start_time=990)

    row = await database.fetchone(
        'SELECT record_start_time,timeline_start_at_ms '
        'FROM recording_parts WHERE run_id=?',
        (run_id,),
    )
    assert row is not None
    assert dict(row) == {'record_start_time': 990, 'timeline_start_at_ms': 1_000_250}


@pytest.mark.asyncio
async def test_part_order_is_creation_order_not_completion_order(
    database, monkeypatch: pytest.MonkeyPatch
) -> None:
    events = []
    monkeypatch.setattr(
        'blrec.bili_upload.journal.audit',
        lambda event, **fields: events.append((event, fields)),
    )
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
    names = [event for event, _fields in events]
    assert names == [
        'recording_started',
        'recording_part_created',
        'recording_part_created',
        'recording_part_completed',
        'recording_part_completed',
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
async def test_restart_of_frozen_live_creates_continuation_session(database) -> None:
    journal = RecordingJournalBridge(database, clock=lambda: 1_000)
    first_run = await journal.recording_started(100, live_start_time=900)
    await journal.video_created(first_run, '/rec/p1.flv', record_start_time=901)
    await journal.video_completed(first_run, '/rec/p1.flv')
    await journal.video_postprocessed(first_run, '/rec/p1.flv', '/rec/p1.flv')
    await journal.recording_finished(first_run)

    restarted_run = await journal.recording_started(100, live_start_time=900)

    first_session = await journal.session_for_run(first_run)
    restarted_session = await journal.session_for_run(restarted_run)
    assert restarted_session.id != first_session.id
    assert restarted_session.broadcast_session_key.startswith('100:900:continuation:')
    assert first_session.state == 'closed'


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
async def test_list_sessions_identifies_derived_highlight_media(database) -> None:
    await database.execute(
        "INSERT INTO recording_sessions("
        "id,room_id,broadcast_session_key,state,started_at,source_kind) "
        "VALUES(1,100,'100:live','closed',1,'live'),"
        "(2,100,'highlight:7','closed',2,'highlight')"
    )
    await database.execute(
        'INSERT INTO highlight_clips('
        'id,room_id,source_session_id,upload_session_id,name,requested_start_ms,'
        'requested_end_ms,state,created_at,updated_at) '
        "VALUES(7,100,1,2,'高光',0,1000,'ready',1,1)"
    )

    sessions = await RecordingJournalBridge(database).list_sessions(sort_order='oldest')

    assert [(item.source_kind, item.highlight_clip_id) for item in sessions] == [
        ('live', None),
        ('highlight', 7),
    ]

    recordings = await RecordingJournalBridge(database).list_sessions(
        scope='recordings'
    )
    assert [item.id for item in recordings] == [1]

    await database.execute(
        'INSERT INTO bili_accounts('
        'id,uid,display_name,credential_ciphertext,credential_version,key_id,'
        'state,created_at,updated_at) '
        "VALUES(1,42,'账号',X'00',1,'key','active',1,1)"
    )
    await database.execute(
        'INSERT INTO upload_jobs('
        'session_id,account_id,policy_snapshot_json,state,submit_state,'
        'created_at,updated_at) '
        "VALUES(2,1,'{}','paused','prepared',1,1)"
    )
    uploads = await RecordingJournalBridge(database).list_sessions(scope='uploads')
    assert [item.id for item in uploads] == [2]


@pytest.mark.asyncio
async def test_list_sessions_rejects_negative_offset(database) -> None:
    journal = RecordingJournalBridge(database, clock=lambda: 1_000)

    with pytest.raises(ValueError, match='offset must not be negative'):
        await journal.list_sessions(offset=-1)


@pytest.mark.asyncio
async def test_list_sessions_filters_upload_state_time_and_fuzzy_text(database) -> None:
    now = [1_000]
    journal = RecordingJournalBridge(database, clock=lambda: now[0])
    first_run = await journal.recording_started(
        100,
        live_start_time=900,
        metadata=SimpleNamespace(
            title='深夜游戏直播',
            cover_url='',
            anchor_uid=10,
            anchor_name='甲主播',
            area_id=1,
            area_name='单机游戏',
            parent_area_id=2,
            parent_area_name='游戏',
        ),
    )
    now[0] = 2_000
    second_run = await journal.recording_started(
        200,
        live_start_time=1_900,
        metadata=SimpleNamespace(
            title='白天学习直播',
            cover_url='',
            anchor_uid=20,
            anchor_name='乙主播',
            area_id=3,
            area_name='教育学习',
            parent_area_id=4,
            parent_area_name='知识',
        ),
    )
    first = await journal.session_for_run(first_run)
    second = await journal.session_for_run(second_run)
    await database.execute(
        'INSERT INTO bili_accounts('
        'id,uid,display_name,credential_ciphertext,credential_version,key_id,state,'
        'created_at,updated_at) '
        "VALUES(1,10,'游戏投稿账号',X'00',1,'k','active',1,1),"
        "(2,20,'学习投稿账号',X'00',1,'k','active',1,1)"
    )
    await database.execute(
        'INSERT INTO upload_jobs('
        'session_id,account_id,policy_snapshot_json,state,submit_state,'
        'created_at,updated_at) VALUES(?,?,?,?,?,?,?)',
        (first.id, 1, '{}', 'paused', 'prepared', 1_000, 1_000),
    )
    await database.execute(
        'INSERT INTO upload_jobs('
        'session_id,account_id,policy_snapshot_json,state,submit_state,aid,bvid,'
        'created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)',
        (second.id, 2, '{}', 'approved', 'confirmed', 123, 'BV1approved', 2_000, 2_000),
    )

    sessions = await journal.list_sessions(
        query='学习投稿',
        upload_state='approved',
        started_from=1_500,
        started_to=2_500,
        sort_order='oldest',
    )

    assert [session.id for session in sessions] == [second.id]
    assert (
        await journal.count_sessions(
            query='学习投稿',
            upload_state='approved',
            started_from=1_500,
            started_to=2_500,
        )
        == 1
    )


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
async def test_reconcile_recovers_crash_interrupted_file_without_manual_review(
    database, tmp_path: Path
) -> None:
    source = tmp_path / 'interrupted.flv'
    source.write_bytes(b'partial recording')
    journal = RecordingJournalBridge(
        database,
        clock=lambda: 1_000,
        artifact_probe=lambda path: RecoveredArtifact(path, 17, 12),
    )
    run_id = await journal.recording_started(100, live_start_time=900)
    await journal.video_created(run_id, str(source), record_start_time=901)

    await journal.reconcile_open_sessions()

    session = await journal.session_for_run(run_id)
    part = (await journal.parts_for_run(run_id))[0]
    assert session.state == 'cancelled'
    assert part.artifact_state == 'ready'
    assert part.final_path == str(source)
    assert part.file_size_bytes == 17
    assert part.record_duration_seconds == 12
    assert part.record_end_time == 913
    assert part.error_message == '录制异常中断，已自动恢复原始文件'
    assert (
        await database.scalar(
            'SELECT COUNT(*) FROM recording_runs '
            "WHERE id=? AND state='cancelled' AND ended_at IS NOT NULL",
            (run_id,),
        )
        == 1
    )


@pytest.mark.asyncio
async def test_reconcile_excludes_unreadable_interrupted_file(
    database, tmp_path: Path
) -> None:
    source = tmp_path / 'broken.flv'
    source.write_bytes(b'broken')
    journal = RecordingJournalBridge(
        database, clock=lambda: 1_000, artifact_probe=lambda _path: None
    )
    run_id = await journal.recording_started(100, live_start_time=900)
    await journal.video_created(run_id, str(source), record_start_time=901)

    await journal.reconcile_open_sessions()

    part = (await journal.parts_for_run(run_id))[0]
    assert part.artifact_state == 'failed'
    assert part.final_path is None
    assert part.error_message == '录制异常中断，文件无法解析，已自动排除'


@pytest.mark.asyncio
async def test_reconcile_falls_back_when_existing_ready_artifact_is_unreadable(
    database, tmp_path: Path
) -> None:
    source = tmp_path / 'source.flv'
    final = tmp_path / 'broken.mp4'
    source.write_bytes(b'video')
    final.write_bytes(b'broken')

    def probe(path: str):
        if path == str(source):
            return RecoveredArtifact(path, 5, 20)
        return None

    journal = RecordingJournalBridge(
        database, clock=lambda: 1_000, artifact_probe=probe
    )
    run_id = await journal.recording_started(100, live_start_time=900)
    await journal.video_created(run_id, str(source), record_start_time=901)
    await journal.video_completed(run_id, str(source))
    await journal.video_postprocessed(run_id, str(source), str(final))
    await database.execute(
        "UPDATE recording_sessions SET state='manual_review' "
        'WHERE id=(SELECT session_id FROM recording_runs WHERE id=?)',
        (run_id,),
    )

    await journal.reconcile_open_sessions()

    part = (await journal.parts_for_run(run_id))[0]
    assert part.artifact_state == 'ready'
    assert part.final_path == str(source)
    assert part.error_message == '录制异常中断，已自动恢复原始文件'


@pytest.mark.asyncio
async def test_cancelled_session_is_finalized_after_resume_grace(
    database, tmp_path: Path
) -> None:
    now = [1_000]
    source = tmp_path / 'resumable.flv'
    source.write_bytes(b'video')
    journal = RecordingJournalBridge(database, clock=lambda: now[0])
    run_id = await journal.recording_started(100, live_start_time=900)
    await journal.video_created(run_id, str(source), record_start_time=901)
    await journal.video_completed(run_id, str(source))
    await journal.video_postprocessed(run_id, str(source), str(source))
    await journal.recording_cancelled(run_id)

    now[0] = 1_599
    assert await journal.finalize_cancelled_sessions(grace_seconds=600) == 0
    assert (await journal.session_for_run(run_id)).state == 'cancelled'

    now[0] = 1_600
    assert await journal.finalize_cancelled_sessions(grace_seconds=600) == 1
    assert (await journal.session_for_run(run_id)).state == 'closed'


@pytest.mark.asyncio
async def test_cancelled_session_with_only_broken_parts_is_skipped(database) -> None:
    now = [1_000]
    journal = RecordingJournalBridge(
        database, clock=lambda: now[0], artifact_probe=lambda _path: None
    )
    run_id = await journal.recording_started(100, live_start_time=900)
    await journal.video_created(run_id, '/rec/broken.flv', record_start_time=901)
    await journal.video_completed(run_id, '/rec/broken.flv')
    await journal.video_postprocessing_failed(
        run_id, '/rec/broken.flv', RuntimeError('invalid FLV')
    )
    await journal.recording_cancelled(run_id)

    now[0] = 1_600
    assert await journal.finalize_cancelled_sessions(grace_seconds=600) == 1
    assert (await journal.session_for_run(run_id)).state == 'skipped'


@pytest.mark.asyncio
async def test_reconcile_consumes_legacy_manual_review_state(
    database, tmp_path: Path
) -> None:
    source = tmp_path / 'legacy.flv'
    source.write_bytes(b'video')
    journal = RecordingJournalBridge(
        database,
        clock=lambda: 1_000,
        artifact_probe=lambda path: RecoveredArtifact(path, 5, 9),
    )
    run_id = await journal.recording_started(100, live_start_time=900)
    await journal.video_created(run_id, str(source), record_start_time=901)
    await database.execute(
        "UPDATE recording_runs SET state='finished',ended_at=910 WHERE id=?", (run_id,)
    )
    await database.execute(
        "UPDATE recording_parts SET artifact_state='manual_review' WHERE run_id=?",
        (run_id,),
    )
    await database.execute(
        "UPDATE recording_sessions SET state='manual_review' "
        'WHERE id=(SELECT session_id FROM recording_runs WHERE id=?)',
        (run_id,),
    )

    await journal.reconcile_open_sessions()

    session = await journal.session_for_run(run_id)
    part = (await journal.parts_for_run(run_id))[0]
    assert session.state == 'closed'
    assert part.artifact_state == 'ready'


@pytest.mark.asyncio
async def test_postprocessing_failure_uses_valid_source_as_upload_artifact(
    database, tmp_path: Path
) -> None:
    source = tmp_path / 'source.flv'
    source.write_bytes(b'video')
    journal = RecordingJournalBridge(
        database,
        clock=lambda: 1_000,
        artifact_probe=lambda path: RecoveredArtifact(path, 5, 20),
    )
    run_id = await journal.recording_started(100, live_start_time=900)
    await journal.video_created(run_id, str(source), record_start_time=901)
    await journal.video_completed(run_id, str(source))

    await journal.video_postprocessing_failed(
        run_id, str(source), RuntimeError('remux failed')
    )

    part = (await journal.parts_for_run(run_id))[0]
    assert part.artifact_state == 'ready'
    assert part.final_path == str(source)
    assert part.file_size_bytes == 5
    assert (
        part.error_message
        == '后处理失败，已自动使用原始录制文件：RuntimeError: remux failed'
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
async def test_unrecoverable_postprocessing_failure_skips_empty_session(
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
    assert session.state == 'skipped'
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
    now = [1_000.0]
    journal = RecordingJournalBridge(database, clock=lambda: now[0])
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
        "UPDATE upload_jobs SET submission_verification_state='partial',"
        "submission_verified_at=1040,submission_verification_json='"
        '{"state":"partial","checked":["title"],'
        '"missing":["up_selection_reply"],"mismatches":[]}'
        "' WHERE id=9"
    )
    await database.execute(
        'INSERT INTO upload_parts('
        'id,job_id,part_index,source_path,final_path,xml_path,artifact_state,'
        'upload_state,danmaku_import_state,remote_filename,cid) '
        "VALUES(10,9,1,'/rec/p1.flv','/rec/p1.mp4','/rec/p1.xml','ready',"
        "'confirmed','pending','remote-p1',NULL)"
    )
    await database.execute(
        'INSERT INTO upload_chunks('
        'part_id,chunk_no,offset,size,state,attempt) VALUES'
        "(10,0,0,4,'confirmed',1),(10,1,4,4,'prepared',0)"
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
    assert job.parts[0].confirmed_bytes == 4
    assert job.parts[0].total_bytes == 8
    assert job.confirmed_bytes == 4
    assert job.total_bytes == 8
    assert job.percent == 50.0
    assert job.current_part_index == 1
    assert job.bytes_per_second is None
    assert job.eta_seconds is None
    assert job.can_repair is False
    assert job.submission_verification_state == 'partial'
    assert job.submission_verified_at == 1040
    assert job.submission_verification == {
        'state': 'partial',
        'checked': ['title'],
        'missing': ['up_selection_reply'],
        'mismatches': [],
    }
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

    now[0] = 1_002.0
    await database.execute(
        "UPDATE upload_chunks SET state='confirmed' " 'WHERE part_id=10 AND chunk_no=1'
    )
    progressed = (await journal.upload_jobs_for_sessions((session.id,)))[session.id]
    assert progressed.confirmed_bytes == 8
    assert progressed.percent == 100.0
    assert progressed.bytes_per_second == 2.0
    assert progressed.eta_seconds == 0

    await database.execute(
        "UPDATE upload_parts SET transcode_state='failed' WHERE id=10"
    )
    failed_jobs = await journal.upload_jobs_for_sessions((session.id,))
    assert failed_jobs[session.id].can_repair is True


@pytest.mark.asyncio
async def test_realtime_upload_progress_returns_active_job_bytes(database) -> None:
    now = [1_000.0]
    journal = RecordingJournalBridge(database, clock=lambda: now[0])
    run_id = await journal.recording_started(100, live_start_time=900)
    session = await journal.session_for_run(run_id)
    await database.execute(
        "INSERT INTO bili_accounts("
        "id,uid,display_name,credential_ciphertext,credential_version,key_id,"
        "state,created_at,updated_at) VALUES(1,42,'账号',X'00',1,'k','active',1,1)"
    )
    await database.execute(
        'INSERT INTO upload_jobs('
        'id,session_id,account_id,policy_snapshot_json,state,submit_state,'
        'created_at,updated_at) '
        "VALUES(3,?,1,'{}','uploading','prepared',1,1000)",
        (session.id,),
    )
    await database.execute(
        'INSERT INTO upload_parts('
        'id,job_id,part_index,source_path,artifact_state,upload_state) '
        "VALUES(4,3,1,'/rec/p1.flv','ready','uploading')"
    )
    await database.execute(
        'INSERT INTO upload_chunks('
        'part_id,chunk_no,offset,size,state,attempt) '
        "VALUES(4,0,0,4,'confirmed',1),(4,1,4,4,'prepared',0)"
    )

    progress = await journal.realtime_upload_progress()

    assert progress == [
        {
            'jobId': 3,
            'sessionId': session.id,
            'state': 'uploading',
            'submitState': 'prepared',
            'aid': None,
            'bvid': None,
            'confirmedBytes': 4,
            'totalBytes': 8,
            'percent': 50.0,
            'bytesPerSecond': None,
            'etaSeconds': None,
            'currentPartIndex': 1,
        }
    ]


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
