from __future__ import annotations

import asyncio
import sqlite3
import stat
import threading
from pathlib import Path

import pytest

from blrec.bili_upload import (
    BiliUploadDatabase,
    DatabaseLocked,
    LeaseLost,
    UnsupportedDatabaseFilesystem,
)

REQUIRED_TABLES = {
    'schema_migrations',
    'event_journal',
    'bili_accounts',
    'qr_sessions',
    'room_upload_policies',
    'recording_sessions',
    'recording_runs',
    'upload_jobs',
    'upload_parts',
    'upload_chunks',
    'comment_items',
    'danmaku_items',
    'management_audit',
}


async def seed_ready_job(database: BiliUploadDatabase) -> None:
    await database.execute(
        "INSERT INTO bili_accounts("
        "id,uid,display_name,credential_ciphertext,credential_version,key_id,"
        "state,created_at,updated_at) "
        "VALUES(1,42,'u',X'00',1,'k','active',1,1)"
    )
    await database.execute(
        "INSERT INTO recording_sessions("
        "id,room_id,broadcast_session_key,state,started_at) "
        "VALUES(1,100,'100:1','closed',1)"
    )
    await database.execute(
        "INSERT INTO upload_jobs("
        "id,session_id,account_id,policy_snapshot_json,state,submit_state,"
        "created_at,updated_at) "
        "VALUES(1,1,1,'{}','ready','prepared',1,1)"
    )


@pytest.mark.asyncio
async def test_migration_enables_wal_constraints_and_claim_indexes(
    tmp_path: Path,
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'blrec.sqlite3'))
    await database.open()
    try:
        assert await database.scalar('PRAGMA journal_mode') == 'wal'
        assert await database.scalar('PRAGMA foreign_keys') == 1
        assert await database.scalar('PRAGMA busy_timeout') == 5000
        assert await database.scalar('PRAGMA quick_check') == 'ok'
        assert await database.scalar('SELECT MAX(version) FROM schema_migrations') == 1
        assert REQUIRED_TABLES == await database.table_names()

        indexes = {
            row['name']
            for row in await database.fetchall(
                "SELECT name FROM sqlite_master WHERE type='index'"
            )
        }
        assert {
            'upload_jobs_claim_idx',
            'comment_items_claim_idx',
            'danmaku_items_claim_idx',
        } <= indexes

        await database.execute(
            "INSERT INTO bili_accounts("
            "id,uid,display_name,credential_ciphertext,credential_version,key_id,"
            "state,created_at,updated_at) "
            "VALUES(1,42,'u',X'00',1,'k','active',1,1)"
        )
        await database.execute(
            "INSERT INTO recording_sessions("
            "id,room_id,broadcast_session_key,state,started_at) "
            "VALUES(1,100,'100:1','closed',1)"
        )
        with pytest.raises(sqlite3.IntegrityError):
            await database.execute(
                "INSERT INTO upload_jobs("
                "session_id,account_id,policy_snapshot_json,state,submit_state,"
                "created_at,updated_at) "
                "VALUES(1,1,'{}','invalid','prepared',1,1)"
            )
        await database.execute(
            "INSERT INTO upload_jobs("
            "id,session_id,account_id,policy_snapshot_json,state,submit_state,"
            "created_at,updated_at) "
            "VALUES(1,1,1,'{}','ready','prepared',1,1)"
        )
        with pytest.raises(sqlite3.IntegrityError):
            await database.execute(
                "INSERT INTO comment_items("
                "job_id,ordinal,kind,content,request_fingerprint,state,attempt) "
                "VALUES(1,0,'root','content','fingerprint','prepared',-1)"
            )
        with pytest.raises(sqlite3.IntegrityError):
            await database.execute(
                'UPDATE upload_jobs SET lease_generation=-1 WHERE id=1'
            )
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_database_secures_directory_database_wal_shm_and_lock(
    tmp_path: Path,
) -> None:
    directory = tmp_path / 'private'
    path = directory / 'blrec.sqlite3'
    database = BiliUploadDatabase(str(path))

    await database.open()
    try:
        await database.execute(
            "INSERT INTO recording_sessions("
            "room_id,broadcast_session_key,state,started_at) "
            "VALUES(1,'1:1','closed',1)"
        )
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700
        for protected_path in (
            path,
            Path(str(path) + '-wal'),
            Path(str(path) + '-shm'),
            Path(str(path) + '.lock'),
        ):
            assert protected_path.exists(), protected_path
            assert stat.S_IMODE(protected_path.stat().st_mode) == 0o600
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_second_database_owner_is_rejected(tmp_path: Path) -> None:
    path = str(tmp_path / 'blrec.sqlite3')
    first = BiliUploadDatabase(path)
    second = BiliUploadDatabase(path)

    await first.open()
    try:
        with pytest.raises(DatabaseLocked):
            await second.open()
    finally:
        await second.close()
        await first.close()


@pytest.mark.asyncio
async def test_unsupported_shared_filesystem_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / 'blrec.sqlite3'
    database = BiliUploadDatabase(str(path))
    monkeypatch.setattr(database, '_filesystem_type', lambda _: 'nfs')

    with pytest.raises(UnsupportedDatabaseFilesystem, match='nfs'):
        await database.open()

    assert not path.exists()
    await database.close()


@pytest.mark.asyncio
async def test_reads_and_writes_share_one_database_actor(tmp_path: Path) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'blrec.sqlite3'))
    await database.open()
    try:
        caller_thread = threading.get_ident()
        read_thread = await database.read(lambda _: threading.get_ident())
        write_thread = await database.write(lambda _: threading.get_ident())

        assert read_thread == write_thread
        assert read_thread != caller_thread
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_claim_is_unique_and_stale_generation_cannot_update(
    tmp_path: Path,
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'blrec.sqlite3'))
    await database.open()
    try:
        await seed_ready_job(database)

        claims = await asyncio.gather(
            database.claim('upload_jobs', ('ready',), 'worker-a', now=1000),
            database.claim('upload_jobs', ('ready',), 'worker-b', now=1000),
        )
        claimed = [claim for claim in claims if claim is not None]
        assert len(claimed) == 1
        first = claimed[0]
        assert first.id == 1
        assert first.lease_generation == 1
        assert first.lease_until == 1120
        assert first.attempt == 1

        with pytest.raises(LeaseLost):
            await database.fenced_update(
                'upload_jobs',
                first.id,
                'wrong-worker',
                first.lease_generation,
                {'state': 'uploading'},
            )
        assert await database.scalar('SELECT state FROM upload_jobs WHERE id=1') == (
            'ready'
        )
        assert (
            await database.fenced_update(
                'upload_jobs',
                first.id,
                first.lease_owner,
                first.lease_generation,
                {'state': 'uploading'},
            )
            is None
        )

        second = await database.claim(
            'upload_jobs', ('uploading',), 'worker-b', now=1121
        )
        assert second is not None
        assert second.lease_generation == 2
        with pytest.raises(LeaseLost):
            await database.fenced_update(
                'upload_jobs',
                first.id,
                first.lease_owner,
                first.lease_generation,
                {'state': 'submitting'},
            )
        assert await database.scalar('SELECT state FROM upload_jobs WHERE id=1') == (
            'uploading'
        )
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_lease_renews_only_in_the_second_half_of_ttl(tmp_path: Path) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'blrec.sqlite3'))
    await database.open()
    try:
        await seed_ready_job(database)
        claim = await database.claim('upload_jobs', ('ready',), 'worker', now=1000)
        assert claim is not None

        assert await database.renew(claim, now=1059) == 0
        assert await database.renew(claim, now=1060) == 1
        assert (
            await database.scalar(
                'SELECT lease_until FROM upload_jobs WHERE id=?', (claim.id,)
            )
            == 1180
        )
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_dynamic_table_and_column_names_are_schema_whitelisted(
    tmp_path: Path,
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'blrec.sqlite3'))
    await database.open()
    try:
        with pytest.raises(ValueError, match='claim table'):
            await database.claim(
                'upload_jobs; DROP TABLE upload_jobs', ('ready',), 'worker', now=1
            )
        with pytest.raises(ValueError, match='update column'):
            await database.fenced_update(
                'upload_jobs', 1, 'worker', 1, {'state = NULL': 'ready'}
            )
        assert 'upload_jobs' in await database.table_names()
    finally:
        await database.close()
