import asyncio
import threading
from pathlib import Path
from typing import AsyncIterator, Callable

import pytest
import pytest_asyncio

from blrec.bili_upload.database import BiliUploadDatabase
from blrec.bili_upload.deletion_worker import LocalDeletionWorker
from blrec.bili_upload.media_index import MediaIndexResult, MediaIndexWorker


@pytest_asyncio.fixture
async def database(tmp_path: Path) -> AsyncIterator[BiliUploadDatabase]:
    value = BiliUploadDatabase(str(tmp_path / 'blrec.sqlite3'))
    await value.open()
    try:
        yield value
    finally:
        await value.close()


async def seed_part(
    database: BiliUploadDatabase,
    path: Path,
    *,
    session_state: str = 'closed',
    index_state: str = 'pending',
) -> int:
    path.write_bytes(b'FLV-incomplete-index')
    await database.execute(
        'INSERT INTO recording_sessions('
        'id,room_id,broadcast_session_key,state,started_at,ended_at) '
        'VALUES(1,100,\'100:1\',?,1,?)',
        (session_state, None if session_state == 'open' else 2),
    )
    await database.execute(
        'INSERT INTO recording_runs('
        'id,session_id,state,started_at,ended_at) VALUES(\'run\',1,?,1,?)',
        (
            'recording' if session_state == 'open' else 'finished',
            None if session_state == 'open' else 2,
        ),
    )
    await database.execute(
        'INSERT INTO recording_parts('
        'id,session_id,run_id,part_index,source_path,final_path,'
        'record_start_time,artifact_state,created_at,updated_at,media_index_state) '
        'VALUES(1,1,\'run\',1,?,?,1,\'ready\',1,1,?)',
        (str(path), str(path), index_state),
    )
    return 1


@pytest.mark.asyncio
async def test_worker_repairs_one_completed_flv_once(
    database: BiliUploadDatabase, tmp_path: Path
) -> None:
    path = tmp_path / 'recording.flv'
    part_id = await seed_part(database, path)
    calls = []

    def rebuild(value: str, progress: Callable[[float], None]) -> MediaIndexResult:
        calls.append(value)
        progress(0.5)
        path.write_bytes(b'FLV-repaired-index')
        return MediaIndexResult(
            duration_ms=12_000, file_size_bytes=path.stat().st_size, keyframe_count=3
        )

    worker = MediaIndexWorker(
        database,
        inspect=lambda _path: None,
        rebuild=rebuild,
        clock=lambda: 100,
        worker_id='test-indexer',
    )

    assert await worker.run_once() == part_id
    assert await worker.run_once() is None
    row = await database.fetchone(
        'SELECT media_index_state,media_index_progress,media_index_error,'
        'media_index_updated_at FROM recording_parts WHERE id=?',
        (part_id,),
    )
    assert row is not None
    assert dict(row) == {
        'media_index_state': 'ready',
        'media_index_progress': 1.0,
        'media_index_error': None,
        'media_index_updated_at': 100,
    }
    assert calls == [str(path)]


@pytest.mark.asyncio
async def test_worker_marks_valid_and_non_flv_parts_without_rewriting(
    database: BiliUploadDatabase, tmp_path: Path
) -> None:
    path = tmp_path / 'recording.flv'
    part_id = await seed_part(database, path)
    valid = MediaIndexResult(12_000, path.stat().st_size, 3)
    rebuild_calls = []
    worker = MediaIndexWorker(
        database,
        inspect=lambda _path: valid,
        rebuild=lambda value, _progress: rebuild_calls.append(value) or valid,
        clock=lambda: 100,
        worker_id='test-indexer',
    )

    assert await worker.run_once() == part_id
    assert rebuild_calls == []
    assert (
        await database.scalar(
            'SELECT media_index_state FROM recording_parts WHERE id=?', (part_id,)
        )
        == 'ready'
    )


@pytest.mark.asyncio
async def test_worker_never_claims_an_active_recording(
    database: BiliUploadDatabase, tmp_path: Path
) -> None:
    path = tmp_path / 'growing.flv'
    part_id = await seed_part(database, path, session_state='open')
    worker = MediaIndexWorker(
        database,
        inspect=lambda _path: None,
        rebuild=lambda _path, _progress: pytest.fail('must not rebuild active file'),
        clock=lambda: 100,
        worker_id='test-indexer',
    )

    assert await worker.run_once() is None
    assert (
        await database.scalar(
            'SELECT media_index_state FROM recording_parts WHERE id=?', (part_id,)
        )
        == 'pending'
    )


@pytest.mark.asyncio
async def test_worker_never_claims_a_session_pending_deletion(
    database: BiliUploadDatabase, tmp_path: Path
) -> None:
    path = tmp_path / 'deleting.flv'
    part_id = await seed_part(database, path)
    await database.execute(
        "UPDATE recording_sessions SET deletion_state='requested',"
        'cancellation_generation=1 WHERE id=1'
    )
    worker = MediaIndexWorker(
        database,
        inspect=lambda _path: pytest.fail('must not inspect deleting session'),
        rebuild=lambda _path, _progress: pytest.fail(
            'must not rebuild deleting session'
        ),
        clock=lambda: 100,
        worker_id='test-indexer',
    )

    assert await worker.run_once() is None
    assert (
        await database.scalar(
            'SELECT media_index_state FROM recording_parts WHERE id=?', (part_id,)
        )
        == 'pending'
    )


@pytest.mark.asyncio
async def test_generation_change_during_rebuild_acks_handoff_without_ready_commit(
    database: BiliUploadDatabase, tmp_path: Path
) -> None:
    path = tmp_path / 'race.flv'
    part_id = await seed_part(database, path)
    started = threading.Event()
    release = threading.Event()

    def rebuild(_path: str, _progress: Callable[[float], None]) -> MediaIndexResult:
        started.set()
        release.wait(timeout=5)
        path.write_bytes(b'rebuilt-after-delete-request')
        return MediaIndexResult(12_000, path.stat().st_size, 3)

    worker = MediaIndexWorker(
        database,
        inspect=lambda _path: None,
        rebuild=rebuild,
        clock=lambda: 100,
        worker_id='test-indexer',
    )
    deletion = LocalDeletionWorker(
        database,
        recording_root=tmp_path,
        clip_root=tmp_path / 'clips',
        clock=lambda: 100,
    )

    task = asyncio.create_task(worker.run_once())
    await asyncio.get_running_loop().run_in_executor(None, started.wait)
    await deletion.request_session(1, manager_subject='manager')
    release.set()
    assert await task == part_id

    row = await database.fetchone(
        'SELECT media_index_state,media_index_owner FROM recording_parts WHERE id=?',
        (part_id,),
    )
    assert row is not None
    assert dict(row) == {'media_index_state': 'failed', 'media_index_owner': None}
    outcome = await database.fetchone(
        'SELECT owner_kind,owner_id,side_effect_key,outcome_state '
        'FROM owner_handoff_outcomes'
    )
    assert outcome is not None
    assert dict(outcome) == {
        'owner_kind': 'media_index',
        'owner_id': part_id,
        'side_effect_key': 'rebuild',
        'outcome_state': 'cancelled_local',
    }


@pytest.mark.asyncio
async def test_worker_indexes_a_part_while_upload_job_waits_for_artifacts(
    database: BiliUploadDatabase, tmp_path: Path
) -> None:
    path = tmp_path / 'recording.flv'
    part_id = await seed_part(database, path)
    await database.execute(
        "INSERT INTO bili_accounts("
        "id,uid,display_name,credential_ciphertext,credential_version,key_id,"
        "state,created_at,updated_at) "
        "VALUES(1,42,'投稿账号',X'00',1,'k','active',1,1)"
    )
    await database.execute(
        'INSERT INTO upload_jobs('
        'id,session_id,account_id,policy_snapshot_json,state,submit_state,'
        'created_at,updated_at) '
        "VALUES(1,1,1,'{}','waiting_artifacts','prepared',1,1)"
    )
    result = MediaIndexResult(12_000, path.stat().st_size, 3)
    worker = MediaIndexWorker(
        database,
        inspect=lambda _path: result,
        rebuild=lambda _path, _progress: result,
        clock=lambda: 100,
        worker_id='test-indexer',
    )

    assert await worker.run_once() == part_id
    assert (
        await database.scalar(
            'SELECT media_index_state FROM recording_parts WHERE id=?', (part_id,)
        )
        == 'ready'
    )


@pytest.mark.asyncio
async def test_worker_recovers_an_interrupted_claim_after_restart(
    database: BiliUploadDatabase, tmp_path: Path
) -> None:
    path = tmp_path / 'recording.flv'
    part_id = await seed_part(database, path, index_state='indexing')
    await database.execute(
        'UPDATE recording_parts SET media_index_owner=\'dead-worker\','
        'media_index_lease_until=999 WHERE id=?',
        (part_id,),
    )
    result = MediaIndexResult(12_000, path.stat().st_size, 3)
    worker = MediaIndexWorker(
        database,
        inspect=lambda _path: result,
        rebuild=lambda _path, _progress: result,
        clock=lambda: 100,
        worker_id='new-worker',
    )

    assert await worker.recover_interrupted() == 1
    assert await worker.run_once() == part_id


@pytest.mark.asyncio
async def test_worker_records_a_bounded_failure(
    database: BiliUploadDatabase, tmp_path: Path
) -> None:
    path = tmp_path / 'broken.flv'
    part_id = await seed_part(database, path)

    def fail(_path: str, _progress: Callable[[float], None]) -> MediaIndexResult:
        raise RuntimeError('broken stream')

    worker = MediaIndexWorker(
        database,
        inspect=lambda _path: None,
        rebuild=fail,
        clock=lambda: 100,
        worker_id='test-indexer',
    )

    assert await worker.run_once() == part_id
    row = await database.fetchone(
        'SELECT media_index_state,media_index_error FROM recording_parts WHERE id=?',
        (part_id,),
    )
    assert row is not None
    assert row['media_index_state'] == 'failed'
    assert row['media_index_error'] == 'RuntimeError: broken stream'
