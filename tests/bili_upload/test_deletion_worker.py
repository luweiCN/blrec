from __future__ import annotations

import asyncio
from pathlib import Path
from typing import List

import pytest

from blrec.bili_upload.database import BiliUploadDatabase
from blrec.bili_upload.deletion_worker import LocalDeletionWorker


async def _seed_session(
    database: BiliUploadDatabase,
    root: Path,
    *,
    path_count: int = 1,
    recording: bool = False,
) -> List[Path]:
    await database.execute(
        "INSERT INTO recording_sessions("
        "id,room_id,broadcast_session_key,state,started_at) "
        "VALUES(1,100,'100:1',?,1)",
        ('open' if recording else 'closed',),
    )
    await database.execute(
        'INSERT INTO recording_runs(id,session_id,state,started_at,ended_at) '
        'VALUES(?,?,?,?,?)',
        (
            'run-1',
            1,
            'recording' if recording else 'finished',
            1,
            None if recording else 2,
        ),
    )
    paths = []
    for index in range(1, path_count + 1):
        path = root / 'part-{}.flv'.format(index)
        path.write_bytes(b'video')
        paths.append(path)
        await database.execute(
            'INSERT INTO recording_parts('
            'session_id,run_id,part_index,source_path,final_path,'
            'record_start_time,artifact_state,created_at,updated_at) '
            "VALUES(?,?,?,?,?,1,'ready',1,1)",
            (1, 'run-1', index, str(path), str(path)),
        )
    return paths


@pytest.mark.asyncio
async def test_request_session_only_persists_generation_and_wakes(
    tmp_path: Path,
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'db.sqlite3'))
    await database.open()
    try:
        await _seed_session(database, tmp_path)
        canceller_started = asyncio.Event()

        async def cancel(_room_id: int) -> None:
            canceller_started.set()

        worker = LocalDeletionWorker(
            database,
            recording_root=tmp_path,
            clip_root=tmp_path / 'clips',
            active_session_canceller=cancel,
        )

        generation = await worker.request_session(1, manager_subject='manager')

        assert generation == 1
        assert not canceller_started.is_set()
        row = await database.fetchone(
            'SELECT deletion_state,cancellation_generation FROM '
            'recording_sessions WHERE id=1'
        )
        assert row is not None
        assert dict(row) == {
            'deletion_state': 'requested',
            'cancellation_generation': 1,
        }
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_worker_processes_at_most_128_owned_paths_and_resumes(
    tmp_path: Path,
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'db.sqlite3'))
    await database.open()
    try:
        paths = await _seed_session(database, tmp_path, path_count=129)
        worker = LocalDeletionWorker(
            database,
            recording_root=tmp_path,
            clip_root=tmp_path / 'clips',
            clock=lambda: 100,
        )
        await worker.request_session(1, manager_subject='manager')

        assert await worker.run_once() == ('session', 1)
        assert sum(path.exists() for path in paths) == 1
        assert (
            await database.scalar(
                "SELECT COUNT(*) FROM local_deletion_items WHERE state='pending'"
            )
            == 1
        )
        assert (
            await database.scalar(
                "SELECT COUNT(*) FROM local_deletion_items WHERE state='done'"
            )
            == 128
        )
        assert (
            await database.scalar('SELECT COUNT(*) FROM recording_sessions WHERE id=1')
            == 1
        )

        restarted = LocalDeletionWorker(
            database, recording_root=tmp_path, clip_root=tmp_path / 'clips'
        )
        await restarted.recover_interrupted()
        assert await restarted.run_once() == ('session', 1)

        assert not any(path.exists() for path in paths)
        assert await database.scalar('SELECT COUNT(*) FROM local_deletion_items') == 0
        assert await database.scalar('SELECT COUNT(*) FROM recording_sessions') == 0
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_worker_waits_for_active_recorder_then_deletes(tmp_path: Path) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'db.sqlite3'))
    await database.open()
    try:
        paths = await _seed_session(database, tmp_path, recording=True)
        cancelled = []

        async def cancel(room_id: int) -> None:
            cancelled.append(room_id)

        worker = LocalDeletionWorker(
            database,
            recording_root=tmp_path,
            clip_root=tmp_path / 'clips',
            active_session_canceller=cancel,
        )
        await worker.request_session(1, manager_subject='manager')

        assert await worker.run_once() == ('session', 1)
        assert cancelled == [100]
        assert paths[0].exists()
        assert await database.scalar('SELECT COUNT(*) FROM local_deletion_items') == 0

        await database.execute(
            "UPDATE recording_runs SET state='cancelled',ended_at=2 WHERE id='run-1'"
        )
        await database.execute(
            "UPDATE recording_sessions SET state='cancelled',ended_at=2 WHERE id=1"
        )
        assert await worker.run_once() == ('session', 1)
        assert not paths[0].exists()
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_worker_rejects_path_outside_owned_root_without_unlinking(
    tmp_path: Path,
) -> None:
    root = tmp_path / 'recordings'
    root.mkdir()
    outside = tmp_path / 'outside.flv'
    outside.write_bytes(b'keep')
    database = BiliUploadDatabase(str(tmp_path / 'db.sqlite3'))
    await database.open()
    try:
        await database.execute(
            "INSERT INTO recording_sessions("
            "id,room_id,broadcast_session_key,state,started_at) "
            "VALUES(1,100,'100:1','closed',1)"
        )
        await database.execute(
            "INSERT INTO recording_runs(id,session_id,state,started_at,ended_at) "
            "VALUES('run-1',1,'finished',1,2)"
        )
        await database.execute(
            'INSERT INTO recording_parts('
            'session_id,run_id,part_index,source_path,final_path,'
            'record_start_time,artifact_state,created_at,updated_at) '
            "VALUES(1,'run-1',1,?,?,1,'ready',1,1)",
            (str(outside), str(outside)),
        )
        worker = LocalDeletionWorker(
            database, recording_root=root, clip_root=tmp_path / 'clips'
        )
        await worker.request_session(1, manager_subject='manager')

        assert await worker.run_once() == ('session', 1)

        assert outside.exists()
        row = await database.fetchone(
            'SELECT deletion_state,deletion_error FROM recording_sessions WHERE id=1'
        )
        assert row is not None
        assert row['deletion_state'] == 'failed'
        assert 'ownership' in str(row['deletion_error'])
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_clip_deletion_keeps_source_recording_and_removes_local_upload(
    tmp_path: Path,
) -> None:
    recording_root = tmp_path / 'rec'
    clip_root = tmp_path / 'clips'
    recording_root.mkdir()
    clip_root.mkdir()
    source = recording_root / 'source.flv'
    output = clip_root / 'clip.mp4'
    xml = clip_root / 'clip.xml'
    source.write_bytes(b'source')
    output.write_bytes(b'clip')
    xml.write_text('<i/>', encoding='utf8')
    database = BiliUploadDatabase(str(tmp_path / 'db.sqlite3'))
    await database.open()
    try:
        await database.execute(
            'INSERT INTO bili_accounts('
            'id,uid,display_name,credential_ciphertext,credential_version,key_id,'
            'state,created_at,updated_at) '
            "VALUES(1,42,'account',X'00',1,'k','active',1,1)"
        )
        await database.execute(
            'INSERT INTO recording_sessions('
            'id,room_id,broadcast_session_key,state,started_at,source_kind) '
            "VALUES(1,100,'live:1','closed',1,'live'),"
            "(2,100,'highlight:7','closed',2,'highlight')"
        )
        await database.execute(
            'INSERT INTO recording_runs(id,session_id,state,started_at,ended_at) '
            "VALUES('live-run',1,'finished',1,2),"
            "('clip-run',2,'finished',2,3)"
        )
        await database.execute(
            'INSERT INTO recording_parts('
            'id,session_id,run_id,part_index,source_path,final_path,xml_path,'
            'record_start_time,artifact_state,created_at,updated_at) '
            "VALUES(1,1,'live-run',1,?,?,NULL,1,'ready',1,1),"
            "(2,2,'clip-run',1,?,?,?,2,'ready',2,2)",
            (str(source), str(source), str(output), str(output), str(xml)),
        )
        await database.execute(
            'INSERT INTO highlight_clips('
            'id,room_id,source_session_id,upload_session_id,name,'
            'requested_start_ms,requested_end_ms,output_video_path,'
            'output_xml_path,state,created_at,updated_at) '
            "VALUES(7,100,1,2,'clip',0,1000,?,?,'ready',1,1)",
            (str(output), str(xml)),
        )
        await database.execute(
            'INSERT INTO highlight_clip_sources('
            'clip_id,part_id,ordinal,requested_start_ms,requested_end_ms) '
            'VALUES(7,1,1,0,1000)'
        )
        await database.execute(
            'INSERT INTO upload_jobs('
            'id,session_id,account_id,policy_snapshot_json,state,submit_state,'
            'created_at,updated_at) '
            "VALUES(9,2,1,'{}','paused','prepared',2,2)"
        )
        await database.execute(
            'INSERT INTO upload_parts('
            'id,job_id,part_index,source_path,final_path,xml_path,artifact_state) '
            "VALUES(9,9,1,?,?,?,'ready')",
            (str(output), str(output), str(xml)),
        )
        worker = LocalDeletionWorker(
            database, recording_root=recording_root, clip_root=clip_root
        )

        await worker.request_clip(7)
        assert await worker.run_once() == ('clip', 7)

        assert source.exists()
        assert not output.exists()
        assert not xml.exists()
        assert await database.scalar('SELECT COUNT(*) FROM highlight_clips') == 0
        assert await database.scalar('SELECT COUNT(*) FROM upload_jobs') == 0
        assert (
            await database.scalar(
                "SELECT COUNT(*) FROM recording_sessions WHERE source_kind='highlight'"
            )
            == 0
        )
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_deletion_fence_prevents_new_upload_and_highlight_claims(
    tmp_path: Path,
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'db.sqlite3'))
    await database.open()
    try:
        await database.execute(
            'INSERT INTO bili_accounts('
            'id,uid,display_name,credential_ciphertext,credential_version,key_id,'
            'state,created_at,updated_at) '
            "VALUES(1,42,'account',X'00',1,'k','active',1,1)"
        )
        await database.execute(
            'INSERT INTO recording_sessions('
            'id,room_id,broadcast_session_key,state,started_at,deletion_state,'
            'cancellation_generation) '
            "VALUES(1,100,'live:1','closed',1,'requested',1)"
        )
        await database.execute(
            'INSERT INTO upload_jobs('
            'id,session_id,account_id,policy_snapshot_json,state,submit_state,'
            'created_at,updated_at) '
            "VALUES(9,1,1,'{}','ready','prepared',1,1)"
        )
        await database.execute(
            'INSERT INTO highlight_clips('
            'id,room_id,source_session_id,name,requested_start_ms,'
            'requested_end_ms,state,created_at,updated_at) '
            "VALUES(7,100,1,'clip',0,1000,'queued',1,1)"
        )

        upload_claim = await database.claim(
            'upload_jobs', ('ready',), 'upload-worker', now=10
        )
        clip_claim = await database.claim(
            'highlight_clips', ('queued',), 'clip-worker', now=10
        )

        assert upload_claim is None
        assert clip_claim is None
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_restart_finishes_when_unlink_happened_before_item_commit(
    tmp_path: Path,
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'db.sqlite3'))
    await database.open()
    try:
        paths = await _seed_session(database, tmp_path)

        def unlink_then_lose_result(path: Path) -> None:
            path.unlink()
            raise OSError('simulated crash after unlink')

        worker = LocalDeletionWorker(
            database,
            recording_root=tmp_path,
            clip_root=tmp_path / 'clips',
            unlink=unlink_then_lose_result,
        )
        await worker.request_session(1, manager_subject='manager')

        assert await worker.run_once() == ('session', 1)
        assert not paths[0].exists()
        assert (
            await database.scalar(
                'SELECT deletion_state FROM recording_sessions WHERE id=1'
            )
            == 'failed'
        )

        restarted = LocalDeletionWorker(
            database, recording_root=tmp_path, clip_root=tmp_path / 'clips'
        )
        await restarted.recover_interrupted()
        assert await restarted.run_once() == ('session', 1)

        assert await database.scalar('SELECT COUNT(*) FROM recording_sessions') == 0
        assert await database.scalar('SELECT COUNT(*) FROM local_deletion_items') == 0
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_worker_does_not_clear_an_owned_upload_lease(tmp_path: Path) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'db.sqlite3'))
    await database.open()
    try:
        paths = await _seed_session(database, tmp_path)
        await database.execute(
            'INSERT INTO bili_accounts('
            'id,uid,display_name,credential_ciphertext,credential_version,key_id,'
            'state,created_at,updated_at) '
            "VALUES(1,42,'account',X'00',1,'k','active',1,1)"
        )
        await database.execute(
            'INSERT INTO upload_jobs('
            'id,session_id,account_id,policy_snapshot_json,state,submit_state,'
            'lease_owner,lease_until,created_at,updated_at) '
            "VALUES(9,1,1,'{}','uploading','prepared','owner',999,1,1)"
        )
        worker = LocalDeletionWorker(
            database,
            recording_root=tmp_path,
            clip_root=tmp_path / 'clips',
            clock=lambda: 2_000,
        )
        await worker.request_session(1, manager_subject='manager')

        assert await worker.run_once() == ('session', 1)

        assert paths[0].exists()
        assert (
            await database.scalar('SELECT lease_owner FROM upload_jobs WHERE id=9')
            == 'owner'
        )
        assert await database.scalar('SELECT COUNT(*) FROM local_deletion_items') == 0
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_unknown_remote_result_is_acknowledged_before_local_rows_are_deleted(
    tmp_path: Path,
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'db.sqlite3'))
    await database.open()
    try:
        await database.execute(
            'INSERT INTO bili_accounts('
            'id,uid,display_name,credential_ciphertext,credential_version,key_id,'
            'state,created_at,updated_at) '
            "VALUES(1,42,'account',X'00',1,'k','active',1,1)"
        )
        await database.execute(
            'INSERT INTO recording_sessions('
            'id,room_id,broadcast_session_key,state,started_at) '
            "VALUES(1,100,'live:1','closed',1)"
        )
        await database.execute(
            'INSERT INTO upload_jobs('
            'id,session_id,account_id,policy_snapshot_json,state,submit_state,'
            'created_at,updated_at) '
            "VALUES(9,1,1,'{}','paused','unknown_outcome',1,1)"
        )
        worker = LocalDeletionWorker(
            database,
            recording_root=tmp_path,
            clip_root=tmp_path / 'clips',
            clock=lambda: 100,
        )

        await worker.request_session(1, manager_subject='manager')
        await worker.run_once()

        outcome = await database.fetchone(
            'SELECT owner_kind,owner_id,side_effect_key,outcome_state,'
            'acknowledged_at FROM owner_handoff_outcomes'
        )
        assert outcome is not None
        assert dict(outcome) == {
            'owner_kind': 'upload',
            'owner_id': 9,
            'side_effect_key': 'archive_submit',
            'outcome_state': 'unknown_terminal',
            'acknowledged_at': 100,
        }
        assert await database.scalar('SELECT COUNT(*) FROM upload_jobs') == 0
    finally:
        await database.close()
