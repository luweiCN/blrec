from dataclasses import replace
from pathlib import Path
from typing import Optional, Tuple

import pytest

from blrec.bili_upload.database import BiliUploadDatabase
from blrec.bili_upload.policies import default_room_upload_policy
from blrec.bili_upload.retention import RetentionManager
from blrec.bili_upload.session_submission import encode_submission_settings


async def seed_account(database: BiliUploadDatabase) -> None:
    await database.execute(
        'INSERT INTO bili_accounts('
        'id,uid,display_name,credential_ciphertext,credential_version,key_id,'
        'state,created_at,updated_at) '
        "VALUES(1,42,'账号',X'00',1,'k','active',1,1)"
    )


async def seed_recording(
    database: BiliUploadDatabase,
    root: Path,
    *,
    identifier: int,
    room_id: int,
    retention_mode: str,
    retention_days: int,
    submitted_at: Optional[int],
    approved_at: Optional[int] = None,
    content: bytes = b'video',
) -> Tuple[Path, Path]:
    now = 100 + identifier
    video = root / '{}.flv'.format(identifier)
    xml = root / '{}.xml'.format(identifier)
    video.write_bytes(content)
    xml.write_text('<i/>', encoding='utf8')
    await database.execute(
        'INSERT INTO room_upload_policies('
        'room_id,account_mode,account_id,enabled,title_template,'
        'description_template,tid,tags,copyright,source,auto_comment,'
        'danmaku_backfill,filter_json,created_at,updated_at,part_title_template,'
        'dynamic_template,is_only_self,publish_dynamic,no_reprint,'
        'up_selection_reply,up_close_reply,up_close_danmu,creation_statement_id,'
        'original_authorization,retention_mode,retention_days) '
        "VALUES(?,'fixed',1,1,'title','',17,'tag',3,'',0,0,'{}',?,?,"
        "'P{{ part_index }}','',0,1,0,0,0,0,-1,0,?,?)",
        (room_id, now, now, retention_mode, retention_days),
    )
    await database.execute(
        'INSERT INTO recording_sessions('
        'id,room_id,broadcast_session_key,state,started_at,ended_at) '
        "VALUES(?,?,?,'closed',?,?)",
        (identifier, room_id, '{}:{}'.format(room_id, identifier), now, now),
    )
    await database.execute(
        'INSERT INTO recording_runs(id,session_id,state,started_at,ended_at) '
        "VALUES(?,?,'finished',?,?)",
        ('run-{}'.format(identifier), identifier, now, now),
    )
    await database.execute(
        'INSERT INTO recording_parts('
        'id,session_id,run_id,part_index,source_path,final_path,xml_path,'
        'record_start_time,artifact_state,xml_completed,file_size_bytes,'
        'created_at,updated_at) '
        "VALUES(?,?,?,1,?,?,?,?,'ready',1,?,?,?)",
        (
            identifier,
            identifier,
            'run-{}'.format(identifier),
            str(video),
            str(video),
            str(xml),
            now,
            len(content),
            now,
            now,
        ),
    )
    await database.execute(
        'INSERT INTO upload_jobs('
        'id,session_id,account_id,policy_snapshot_json,state,submit_state,aid,bvid,'
        'upload_completed_at,submitted_at,approved_at,created_at,updated_at) '
        'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
        (
            identifier,
            identifier,
            1,
            '{}',
            'approved' if approved_at is not None else 'waiting_review',
            'confirmed' if submitted_at is not None else 'prepared',
            identifier if submitted_at is not None else None,
            'BV{}'.format(identifier) if submitted_at is not None else None,
            now,
            submitted_at,
            approved_at,
            now,
            now,
        ),
    )
    return video, xml


@pytest.mark.asyncio
async def test_event_retention_deletes_only_video_and_preserves_danmaku(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events = []
    monkeypatch.setattr(
        'blrec.bili_upload.retention.audit',
        lambda event, **fields: events.append((event, fields)),
    )
    root = tmp_path / 'records'
    root.mkdir()
    database = BiliUploadDatabase(str(tmp_path / 'upload.sqlite3'))
    await database.open()
    try:
        await seed_account(database)
        video, xml = await seed_recording(
            database,
            root,
            identifier=1,
            room_id=100,
            retention_mode='submitted',
            retention_days=5,
            submitted_at=1_000,
        )
        manager = RetentionManager(database, root, clock=lambda: 1_000 + 5 * 86400)

        deleted = await manager.run_once()

        assert deleted == 1
        assert not video.exists()
        assert xml.exists()
        row = await database.fetchone(
            'SELECT video_deleted_at,video_delete_reason,video_delete_error,'
            'xml_path FROM recording_parts WHERE id=1'
        )
        assert row is not None
        assert row['video_deleted_at'] == 1_000 + 5 * 86400
        assert row['video_delete_reason'] == 'submitted'
        assert row['video_delete_error'] is None
        assert row['xml_path'] == str(xml)
        assert any(
            event == 'recording_video_deleted'
            and fields['part_id'] == 1
            and fields['reason'] == 'submitted'
            for event, fields in events
        )
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_clip_library_files_do_not_count_toward_recording_capacity(
    tmp_path: Path,
) -> None:
    recording_root = tmp_path / 'rec'
    clip_root = tmp_path / 'clips'
    recording_root.mkdir()
    clip_root.mkdir()
    recording_video = recording_root / 'recording.flv'
    clip_video = clip_root / 'highlight.mp4'
    recording_video.write_bytes(b'r' * 10)
    clip_video.write_bytes(b'c' * 40)
    database = BiliUploadDatabase(str(tmp_path / 'upload.sqlite3'))
    await database.open()
    try:
        await database.execute(
            'INSERT INTO recording_sessions('
            'id,room_id,broadcast_session_key,state,started_at,source_kind) '
            "VALUES(1,100,'100:1','closed',1,'live'),"
            "(2,100,'highlight:1','closed',2,'highlight')"
        )
        await database.execute(
            'INSERT INTO recording_runs(id,session_id,state,started_at,ended_at) '
            "VALUES('live',1,'finished',1,1),('clip',2,'finished',2,2)"
        )
        await database.execute(
            'INSERT INTO recording_parts('
            'session_id,run_id,part_index,source_path,final_path,'
            'record_start_time,file_size_bytes,artifact_state,created_at,updated_at) '
            "VALUES(1,'live',1,?,?,1,10,'ready',1,1),"
            "(2,'clip',1,?,?,2,40,'ready',2,2)",
            (
                str(recording_video),
                str(recording_video),
                str(clip_video),
                str(clip_video),
            ),
        )
        manager = RetentionManager(database, recording_root, capacity_bytes=lambda: 100)

        status = await manager.status()

        assert status.managed_video_bytes == 10
        assert status.remaining_bytes == 90
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_status_aggregates_persisted_live_sizes_without_file_io(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / 'records'
    root.mkdir()
    database = BiliUploadDatabase(str(tmp_path / 'upload.sqlite3'))
    await database.open()
    try:

        def seed(connection) -> None:
            connection.executemany(
                'INSERT INTO recording_sessions('
                'id,room_id,broadcast_session_key,state,started_at,ended_at,'
                'source_kind) VALUES(?,?,?,\'closed\',1,2,?)',
                (
                    (1, 100, '100:many', 'live'),
                    (2, 101, '101:deleted', 'live'),
                    (3, 102, 'highlight:1', 'highlight'),
                    (4, 103, '103:protected', 'live'),
                ),
            )
            connection.executemany(
                'INSERT INTO recording_runs('
                'id,session_id,state,started_at,ended_at) '
                "VALUES(?,?,'finished',1,2)",
                (('many', 1), ('deleted', 2), ('highlight', 3), ('protected', 4)),
            )
            connection.executemany(
                'INSERT INTO recording_parts('
                'id,session_id,run_id,part_index,source_path,final_path,'
                'record_start_time,artifact_state,file_size_bytes,created_at,'
                'updated_at) VALUES(?,1,\'many\',?,?,?,1,\'ready\',1,1,1)',
                tuple(
                    (
                        identifier,
                        identifier,
                        str(root / '{}.flv'.format(identifier)),
                        str(root / '{}.flv'.format(identifier)),
                    )
                    for identifier in range(1, 101)
                ),
            )
            connection.execute(
                'INSERT INTO recording_parts('
                'id,session_id,run_id,part_index,source_path,final_path,'
                'record_start_time,artifact_state,file_size_bytes,video_deleted_at,'
                'created_at,updated_at) '
                "VALUES(101,2,'deleted',1,?,?,1,'ready',50,2,1,1)",
                (str(root / 'deleted.flv'), str(root / 'deleted.flv')),
            )
            connection.execute(
                'INSERT INTO recording_parts('
                'id,session_id,run_id,part_index,source_path,final_path,'
                'record_start_time,artifact_state,file_size_bytes,created_at,'
                'updated_at) '
                "VALUES(102,3,'highlight',1,?,?,1,'ready',70,1,1)",
                (str(root / 'highlight.mp4'), str(root / 'highlight.mp4')),
            )
            connection.execute(
                'INSERT INTO recording_parts('
                'id,session_id,run_id,part_index,source_path,final_path,'
                'record_start_time,artifact_state,file_size_bytes,created_at,'
                'updated_at) '
                "VALUES(103,4,'protected',1,?,?,1,'ready',11,1,1)",
                (str(root / 'protected.flv'), str(root / 'protected.flv')),
            )
            connection.execute(
                'INSERT INTO highlight_clips('
                'id,room_id,source_session_id,name,requested_start_ms,'
                'requested_end_ms,state,created_at,updated_at) '
                "VALUES(1,103,4,'高光',0,1000,'queued',1,1)"
            )
            connection.execute(
                'INSERT INTO highlight_clip_sources('
                'clip_id,part_id,ordinal,requested_start_ms,requested_end_ms) '
                'VALUES(1,103,1,0,1000)'
            )

        await database.write(seed)
        manager = RetentionManager(database, root, capacity_bytes=lambda: 1_000)
        database_calls = []
        filesystem_calls = []
        original_run = database._run

        async def counting_run(operation, *args):
            database_calls.append(operation.__name__)
            return await original_run(operation, *args)

        def forbidden_paths_size(paths) -> int:
            filesystem_calls.append(tuple(paths))
            raise AssertionError('retention status must not inspect recording paths')

        monkeypatch.setattr(database, '_run', counting_run)
        monkeypatch.setattr(manager, '_paths_size', forbidden_paths_size)

        status = await manager.status()

        assert status.managed_video_bytes == 111
        assert len(database_calls) == 1
        assert filesystem_calls == []
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_null_size_capacity_uses_real_active_file_before_cleanup(
    tmp_path: Path,
) -> None:
    root = tmp_path / 'records'
    root.mkdir()
    database = BiliUploadDatabase(str(tmp_path / 'upload.sqlite3'))
    await database.open()
    try:
        await seed_account(database)
        reclaimable, _xml = await seed_recording(
            database,
            root,
            identifier=1,
            room_id=101,
            retention_mode='capacity',
            retention_days=5,
            submitted_at=1_000,
            content=b'old!',
        )
        active = root / 'active.flv'
        active.write_bytes(b'recording')
        await database.execute(
            'INSERT INTO recording_sessions('
            'id,room_id,broadcast_session_key,state,started_at) '
            "VALUES(2,102,'102:active','open',2)"
        )
        await database.execute(
            'INSERT INTO recording_runs(id,session_id,state,started_at) '
            "VALUES('active',2,'recording',2)"
        )
        await database.execute(
            'INSERT INTO recording_parts('
            'id,session_id,run_id,part_index,source_path,record_start_time,'
            'artifact_state,file_size_bytes,created_at,updated_at) '
            "VALUES(2,2,'active',1,?,2,'recording',NULL,2,2)",
            (str(active),),
        )
        manager = RetentionManager(
            database, root, capacity_bytes=lambda: 8, clock=lambda: 10_000
        )

        assert await manager.run_once() == 1
        assert not reclaimable.exists()
        assert active.exists()
        assert (
            await database.scalar(
                'SELECT video_deleted_at FROM recording_parts WHERE id=1'
            )
            == 10_000
        )
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_session_retention_override_wins_over_room_policy(tmp_path: Path) -> None:
    root = tmp_path / 'records'
    root.mkdir()
    database = BiliUploadDatabase(str(tmp_path / 'upload.sqlite3'))
    await database.open()
    try:
        await seed_account(database)
        video, _xml = await seed_recording(
            database,
            root,
            identifier=1,
            room_id=100,
            retention_mode='never',
            retention_days=30,
            submitted_at=1_000,
        )
        override = replace(
            default_room_upload_policy(), retention_mode='submitted', retention_days=0
        )
        await database.execute(
            'UPDATE recording_sessions SET upload_override_json=? WHERE id=1',
            (encode_submission_settings(override),),
        )
        manager = RetentionManager(database, root, clock=lambda: 1_001)

        assert await manager.run_once() == 1
        assert not video.exists()
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_capacity_retention_deletes_oldest_eligible_video_only(
    tmp_path: Path,
) -> None:
    root = tmp_path / 'records'
    root.mkdir()
    database = BiliUploadDatabase(str(tmp_path / 'upload.sqlite3'))
    await database.open()
    try:
        await seed_account(database)
        oldest, oldest_xml = await seed_recording(
            database,
            root,
            identifier=1,
            room_id=101,
            retention_mode='capacity',
            retention_days=5,
            submitted_at=1_000,
            content=b'1111',
        )
        newer, _ = await seed_recording(
            database,
            root,
            identifier=2,
            room_id=102,
            retention_mode='capacity',
            retention_days=5,
            submitted_at=2_000,
            content=b'2222',
        )
        protected, _ = await seed_recording(
            database,
            root,
            identifier=3,
            room_id=103,
            retention_mode='never',
            retention_days=5,
            submitted_at=500,
            content=b'3333',
        )
        manager = RetentionManager(
            database,
            root,
            capacity_bytes=lambda: 8,
            warning_threshold_bytes=lambda: 2,
            clock=lambda: 10_000,
        )

        deleted = await manager.run_once()
        status = await manager.status()

        assert deleted == 1
        assert not oldest.exists()
        assert oldest_xml.exists()
        assert newer.exists()
        assert protected.exists()
        assert status.managed_video_bytes == 8
        assert status.capacity_bytes == 8
        assert status.remaining_bytes == 0
        assert status.warning is True
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_capacity_retention_skips_parts_used_by_pending_highlight(
    tmp_path: Path,
) -> None:
    root = tmp_path / 'records'
    root.mkdir()
    database = BiliUploadDatabase(str(tmp_path / 'upload.sqlite3'))
    await database.open()
    try:
        await seed_account(database)
        video, _xml = await seed_recording(
            database,
            root,
            identifier=1,
            room_id=101,
            retention_mode='capacity',
            retention_days=5,
            submitted_at=1_000,
            content=b'video',
        )
        await database.execute(
            'INSERT INTO highlight_clips('
            'id,room_id,source_session_id,name,requested_start_ms,'
            'requested_end_ms,state,created_at,updated_at) '
            "VALUES(1,101,1,'高光',0,1000,'queued',1,1)"
        )
        await database.execute(
            'INSERT INTO highlight_clip_sources('
            'clip_id,part_id,ordinal,requested_start_ms,requested_end_ms) '
            'VALUES(1,1,1,0,1000)'
        )
        manager = RetentionManager(
            database, root, capacity_bytes=lambda: 1, clock=lambda: 10_000
        )

        assert await manager.run_once() == 0
        assert video.exists()

        await database.execute("UPDATE highlight_clips SET state='failed' WHERE id=1")
        assert await manager.run_once() == 1
        assert not video.exists()
    finally:
        await database.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('upload_intent', ('none', 'skip'))
async def test_capacity_retention_reclaims_safe_no_job_sessions_only(
    tmp_path: Path, upload_intent: str
) -> None:
    root = tmp_path / 'records'
    root.mkdir()
    database = BiliUploadDatabase(str(tmp_path / 'upload.sqlite3'))
    await database.open()
    try:
        await seed_account(database)
        reclaimable, _ = await seed_recording(
            database,
            root,
            identifier=1,
            room_id=101,
            retention_mode='capacity',
            retention_days=5,
            submitted_at=1_000,
            content=b'1111',
        )
        pending_upload, _ = await seed_recording(
            database,
            root,
            identifier=2,
            room_id=102,
            retention_mode='capacity',
            retention_days=5,
            submitted_at=None,
            content=b'2222',
        )
        await database.execute('DELETE FROM upload_jobs WHERE id=1')
        await database.execute(
            'UPDATE recording_sessions SET upload_intent=? WHERE id=1', (upload_intent,)
        )
        manager = RetentionManager(
            database, root, capacity_bytes=lambda: 4, clock=lambda: 10_000
        )

        assert await manager.run_once() == 1
        assert not reclaimable.exists()
        assert pending_upload.exists()
        assert (
            await database.scalar(
                'SELECT video_deleted_at FROM recording_parts WHERE id=2'
            )
            is None
        )
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_retention_rejects_video_path_outside_recording_root(
    tmp_path: Path,
) -> None:
    root = tmp_path / 'records'
    root.mkdir()
    outside = tmp_path / 'outside.flv'
    outside.write_bytes(b'outside')
    database = BiliUploadDatabase(str(tmp_path / 'upload.sqlite3'))
    await database.open()
    try:
        await seed_account(database)
        video, _ = await seed_recording(
            database,
            root,
            identifier=1,
            room_id=100,
            retention_mode='submitted',
            retention_days=0,
            submitted_at=1_000,
        )
        video.unlink()
        await database.execute(
            'UPDATE recording_parts SET source_path=?,final_path=? WHERE id=1',
            (str(outside), str(outside)),
        )
        manager = RetentionManager(database, root, clock=lambda: 2_000)

        assert await manager.run_once() == 0
        assert outside.exists()
        error = await database.scalar(
            'SELECT video_delete_error FROM recording_parts WHERE id=1'
        )
        assert 'outside' in str(error).lower()
    finally:
        await database.close()


@pytest.mark.parametrize(
    ('retention_mode', 'submitted_at', 'approved_at'),
    (
        ('upload_completed', None, None),
        ('submitted', 1_000, None),
        ('approved', 900, 1_000),
        ('capacity', 1_000, None),
    ),
)
@pytest.mark.asyncio
async def test_media_library_session_is_excluded_from_all_retention(
    tmp_path: Path,
    retention_mode: str,
    submitted_at: Optional[int],
    approved_at: Optional[int],
) -> None:
    root = tmp_path / 'records'
    root.mkdir()
    database = BiliUploadDatabase(str(tmp_path / 'upload.sqlite3'))
    await database.open()
    try:
        await seed_account(database)
        video, _xml = await seed_recording(
            database,
            root,
            identifier=1,
            room_id=100,
            retention_mode=retention_mode,
            retention_days=0,
            submitted_at=submitted_at,
            approved_at=approved_at,
            content=b'permanent',
        )
        await database.execute(
            'INSERT INTO media_library_items('
            'session_id,kind,origin,storage_key,display_name,state,created_at,'
            'updated_at) VALUES(1,\'broadcast\',\'recording\',?,\'永久直播\','
            "'ready',1,1)",
            ('f' * 32,),
        )
        manager = RetentionManager(
            database, root, capacity_bytes=lambda: 1, clock=lambda: 10_000
        )

        assert await manager.run_once() == 0
        assert video.exists()
        assert (await manager.status()).managed_video_bytes == 0
        assert await manager._event_candidates(10_000) == []
        assert await manager._capacity_candidates() == []
    finally:
        await database.close()
