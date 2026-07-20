from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from typing import List

import pytest

from blrec.bili_upload.database import BiliUploadDatabase
from blrec.bili_upload.deletion_worker import LocalDeletionWorker
from blrec.bili_upload.highlights import HighlightService


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
async def test_v25_requested_session_keeps_its_file_in_the_deletion_cursor(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / 'db.sqlite3'
    video_path = tmp_path / 'legacy.flv'
    video_path.write_bytes(b'video')
    migration_directory = (
        Path(__file__).parents[2] / 'src' / 'blrec' / 'bili_upload' / 'migrations'
    )
    connection = sqlite3.connect(str(database_path))
    try:
        for version in range(1, 26):
            connection.executescript(
                (migration_directory / '{:04d}_initial.sql'.format(version)).read_text(
                    encoding='utf8'
                )
            )
            connection.execute(
                'INSERT INTO schema_migrations(version,applied_at) VALUES(?,1)',
                (version,),
            )
        connection.execute(
            'INSERT INTO recording_sessions('
            'id,room_id,broadcast_session_key,state,started_at,deletion_state,'
            'deletion_requested_at) '
            "VALUES(1,100,'legacy','closed',1,'requested',1)"
        )
        connection.execute(
            'INSERT INTO recording_runs(id,session_id,state,started_at,ended_at) '
            "VALUES('run-1',1,'finished',1,2)"
        )
        connection.execute(
            'INSERT INTO recording_parts('
            'session_id,run_id,part_index,source_path,final_path,'
            'record_start_time,artifact_state,created_at,updated_at) '
            "VALUES(1,'run-1',1,?,?,1,'ready',1,1)",
            (str(video_path), str(video_path)),
        )
        connection.commit()
    finally:
        connection.close()

    database = BiliUploadDatabase(str(database_path))
    await database.open()
    try:
        worker = LocalDeletionWorker(
            database, recording_root=tmp_path, clip_root=tmp_path / 'clips'
        )

        assert await worker.run_once() == ('session', 1)

        assert not video_path.exists()
        assert await database.scalar('SELECT COUNT(*) FROM recording_sessions') == 0
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_run_rechecks_after_a_wake_between_empty_scan_and_wait(
    tmp_path: Path,
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'db.sqlite3'))
    await database.open()
    stop_event = asyncio.Event()
    first_scan = asyncio.Event()
    release_first_scan = asyncio.Event()
    second_scan = asyncio.Event()
    try:
        worker = LocalDeletionWorker(
            database, recording_root=tmp_path, clip_root=tmp_path / 'clips'
        )
        calls = 0

        async def controlled_run_once(*, stop_event: asyncio.Event) -> object:
            nonlocal calls
            calls += 1
            if calls == 1:
                first_scan.set()
                await release_first_scan.wait()
                return None
            second_scan.set()
            stop_event.set()
            return None

        worker.run_once = controlled_run_once  # type: ignore[method-assign]
        run_task = asyncio.create_task(worker.run(stop_event))
        await first_scan.wait()

        worker.wake()
        release_first_scan.set()
        await asyncio.wait_for(second_scan.wait(), timeout=0.5)
        await asyncio.wait_for(run_task, timeout=0.5)

        assert calls == 2
    finally:
        stop_event.set()
        if 'worker' in locals():
            worker.wake()
        if 'run_task' in locals():
            await asyncio.gather(run_task, return_exceptions=True)
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
async def test_clip_deletion_waits_for_bound_upload_owners_and_branches(
    tmp_path: Path,
) -> None:
    clip_root = tmp_path / 'clips'
    clip_root.mkdir()
    output = clip_root / 'clip.mp4'
    output.write_bytes(b'clip')
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
            "VALUES(2,100,'highlight:7','closed',2,'highlight')"
        )
        await database.execute(
            'INSERT INTO upload_jobs('
            'id,session_id,account_id,policy_snapshot_json,state,submit_state,'
            'collection_branch_state,created_at,updated_at) '
            "VALUES(9,2,1,'{}','ready','prepared','running',2,2)"
        )
        await database.execute(
            'INSERT INTO highlight_clips('
            'id,room_id,upload_session_id,name,requested_start_ms,'
            'requested_end_ms,output_video_path,state,created_at,updated_at) '
            "VALUES(7,100,2,'clip',0,1000,?,'ready',1,1)",
            (str(output),),
        )
        claim = await database.claim('upload_jobs', ('ready',), 'upload-owner', now=10)
        assert claim is not None
        worker = LocalDeletionWorker(
            database, recording_root=tmp_path / 'rec', clip_root=clip_root
        )

        await worker.request_clip(7)
        blocked_claim = await database.claim(
            'upload_jobs', ('ready',), 'second-owner', now=200
        )
        assert blocked_claim is None
        assert await worker.run_once() == ('clip', 7)
        assert output.exists()
        assert await database.scalar('SELECT COUNT(*) FROM upload_jobs') == 1
        assert (
            await database.scalar('SELECT lease_owner FROM upload_jobs WHERE id=9')
            == 'upload-owner'
        )

        await database.execute(
            'UPDATE upload_jobs SET lease_owner=NULL,lease_until=NULL '
            'WHERE id=? AND lease_owner=? AND lease_generation=?',
            (claim.id, claim.lease_owner, claim.lease_generation),
        )
        assert await worker.run_once() == ('clip', 7)
        assert output.exists()
        assert await database.scalar('SELECT COUNT(*) FROM upload_jobs') == 1

        await database.execute(
            "UPDATE upload_jobs SET collection_branch_state='completed' WHERE id=9"
        )
        assert await worker.run_once() == ('clip', 7)
        assert not output.exists()
        assert await database.scalar('SELECT COUNT(*) FROM upload_jobs') == 0
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_failed_clip_deletion_stays_visible_with_its_error(
    tmp_path: Path,
) -> None:
    clip_root = tmp_path / 'clips'
    clip_root.mkdir()
    output = clip_root / 'clip.mp4'
    output.write_bytes(b'clip')
    database = BiliUploadDatabase(str(tmp_path / 'db.sqlite3'))
    await database.open()
    try:
        await database.execute(
            'INSERT INTO highlight_clips('
            'id,room_id,name,requested_start_ms,requested_end_ms,'
            'output_video_path,state,created_at,updated_at) '
            "VALUES(7,100,'clip',0,1000,?,'ready',1,1)",
            (str(output),),
        )

        def refuse_unlink(_path: Path) -> None:
            raise PermissionError('NAS refused deletion')

        worker = LocalDeletionWorker(
            database,
            recording_root=tmp_path / 'rec',
            clip_root=clip_root,
            unlink=refuse_unlink,
        )
        await worker.request_clip(7)
        await worker.run_once()

        total, summaries = await HighlightService(database).list_clip_summaries(
            limit=20, offset=0
        )
        detail = await HighlightService(database).get_clip(7)

        assert total == 1
        assert summaries[0].deletion_state == 'failed'
        assert summaries[0].deletion_error == 'unlink_PermissionError'
        assert detail.deletion_state == 'failed'
        assert detail.deletion_error == 'unlink_PermissionError'
        assert output.exists()
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_late_upload_session_cannot_cross_clip_quiesce_barrier(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clip_root = tmp_path / 'clips'
    clip_root.mkdir()
    output = clip_root / 'clip.mp4'
    output.write_bytes(b'clip')
    database = BiliUploadDatabase(str(tmp_path / 'db.sqlite3'))
    await database.open()
    create_waiting = asyncio.Event()
    release_create = asyncio.Event()
    unlinked: List[Path] = []
    try:
        await database.execute(
            'INSERT INTO recording_sessions('
            'id,room_id,broadcast_session_key,state,started_at,title,anchor_name) '
            "VALUES(1,100,'live:1','closed',1,'直播','主播')"
        )
        await database.execute(
            'INSERT INTO highlight_clips('
            'id,room_id,source_session_id,name,requested_start_ms,'
            'requested_end_ms,actual_start_ms,actual_end_ms,output_video_path,'
            'state,created_at,updated_at) '
            "VALUES(7,100,1,'clip',0,1000,0,1000,?,'ready',1,1)",
            (str(output),),
        )
        service = HighlightService(database, clip_root=clip_root)
        worker = LocalDeletionWorker(
            database,
            recording_root=tmp_path / 'rec',
            clip_root=clip_root,
            unlink=lambda path: unlinked.append(path),
        )
        original_write = database.write

        async def gated_write(operation):
            if getattr(operation, '__name__', '') == 'create':
                create_waiting.set()
                await release_create.wait()
            return await original_write(operation)

        monkeypatch.setattr(database, 'write', gated_write)
        ensure_task = asyncio.create_task(service.ensure_upload_session(7))
        await asyncio.wait_for(create_waiting.wait(), timeout=0.5)

        generation = await worker.request_clip(7)
        assert await worker._quiesce_clip(7, generation)  # noqa: SLF001
        release_create.set()
        with pytest.raises(ValueError, match='not ready for upload'):
            await ensure_task

        assert (
            await database.scalar(
                "SELECT COUNT(*) FROM recording_sessions WHERE source_kind='highlight'"
            )
            == 0
        )
        assert (
            await database.scalar(
                'SELECT upload_session_id FROM highlight_clips WHERE id=7'
            )
            is None
        )
        assert unlinked == []

        def unexpected_path_check(_raw_path: str) -> Path:
            raise AssertionError('deleting clip must fail before local file I/O')

        monkeypatch.setattr(service, '_owned_highlight_path', unexpected_path_check)
        with pytest.raises(ValueError, match='not ready for upload'):
            await service.ensure_upload_session(7)
    finally:
        release_create.set()
        if 'ensure_task' in locals():
            await asyncio.gather(ensure_task, return_exceptions=True)
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
