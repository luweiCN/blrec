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
    'bili_account_selection',
    'qr_sessions',
    'room_upload_policies',
    'upload_category_cache',
    'cover_assets',
    'cover_asset_uploads',
    'recording_sessions',
    'recording_runs',
    'recording_parts',
    'upload_jobs',
    'upload_parts',
    'upload_chunks',
    'comment_items',
    'danmaku_items',
    'management_audit',
    'upload_suppressions',
    'upload_job_archives',
    'operational_notification_states',
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
        assert await database.scalar('SELECT MAX(version) FROM schema_migrations') == 16
        assert REQUIRED_TABLES == await database.table_names()

        account_columns = {
            row['name']
            for row in await database.fetchall('PRAGMA table_info(bili_accounts)')
        }
        assert {'avatar_url', 'credential_expires_at'} <= account_columns
        policy_columns = {
            row['name']
            for row in await database.fetchall(
                'PRAGMA table_info(room_upload_policies)'
            )
        }
        assert {
            'account_mode',
            'part_title_template',
            'dynamic_template',
            'is_only_self',
            'publish_dynamic',
            'no_reprint',
            'creation_statement_id',
            'original_authorization',
            'up_selection_reply',
            'up_close_reply',
            'up_close_danmu',
            'collection_season_id',
            'collection_section_id',
            'cover_mode',
            'cover_asset_id',
            'publish_delay_seconds',
            'retention_mode',
            'retention_days',
        } <= policy_columns
        job_columns = {
            row['name']
            for row in await database.fetchall('PRAGMA table_info(upload_jobs)')
        }
        assert {
            'scheduled_publish_at',
            'collection_branch_state',
            'collection_error',
            'upload_completed_at',
            'submitted_at',
            'approved_at',
            'repair_state',
            'repair_message',
            'repair_error',
            'repair_attempt',
            'repair_requested_at',
            'repair_completed_at',
            'operator_paused',
            'operator_resume_state',
        } <= job_columns
        upload_part_columns = {
            row['name']
            for row in await database.fetchall('PRAGMA table_info(upload_parts)')
        }
        assert {
            'transcode_state',
            'transcode_fail_code',
            'transcode_fail_desc',
            'repair_stage',
            'repair_original_attempts',
            'repair_remux_attempts',
            'repair_diagnostic',
            'repair_temp_path',
            'repair_original_path',
            'repair_original_identity',
        } <= upload_part_columns
        session_columns = {
            row['name']
            for row in await database.fetchall('PRAGMA table_info(recording_sessions)')
        }
        assert {
            'title',
            'cover_url',
            'cover_path',
            'anchor_uid',
            'anchor_name',
            'area_id',
            'area_name',
            'parent_area_id',
            'parent_area_name',
            'live_end_time',
            'deletion_state',
            'deletion_error',
            'deletion_requested_at',
        } <= session_columns
        part_columns = {
            row['name']
            for row in await database.fetchall('PRAGMA table_info(recording_parts)')
        }
        assert {
            'record_end_time',
            'record_duration_seconds',
            'file_size_bytes',
            'danmaku_count',
            'video_deleted_at',
            'video_delete_reason',
            'video_delete_error',
        } <= part_columns

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
        with pytest.raises(sqlite3.IntegrityError):
            await database.execute(
                "INSERT INTO room_upload_policies("
                "room_id,account_mode,account_id,enabled,title_template,"
                "description_template,tid,tags,copyright,source,auto_comment,"
                "danmaku_backfill,filter_json,created_at,updated_at) "
                "VALUES(100,'primary',1,1,'title','description',17,'tag',1,'',"
                "0,0,'{}',1,1)"
            )
        with pytest.raises(sqlite3.IntegrityError):
            await database.execute(
                "INSERT INTO room_upload_policies("
                "room_id,account_mode,account_id,enabled,title_template,"
                "description_template,tid,tags,copyright,source,auto_comment,"
                "danmaku_backfill,filter_json,created_at,updated_at) "
                "VALUES(100,'fixed',NULL,1,'title','description',17,'tag',1,'',"
                "0,0,'{}',1,1)"
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
async def test_second_migration_preserves_existing_accounts(tmp_path: Path) -> None:
    path = tmp_path / 'blrec.sqlite3'
    migration = (
        Path(__file__).parents[2]
        / 'src'
        / 'blrec'
        / 'bili_upload'
        / 'migrations'
        / '0001_initial.sql'
    ).read_text(encoding='utf8')
    connection = sqlite3.connect(str(path))
    try:
        connection.executescript(migration)
        connection.execute(
            'INSERT INTO schema_migrations(version,applied_at) VALUES(1,1)'
        )
        connection.execute(
            'INSERT INTO bili_accounts('
            'id,uid,display_name,credential_ciphertext,credential_version,key_id,'
            'state,created_at,updated_at) '
            "VALUES(1,42,'existing',X'00',1,'key','active',10,20)"
        )
        connection.execute(
            'INSERT INTO room_upload_policies('
            'room_id,account_id,enabled,title_template,description_template,tid,'
            'tags,copyright,source,auto_comment,danmaku_backfill,filter_json,'
            'created_at,updated_at) '
            "VALUES(100,1,1,'title','description',17,'tag',1,'',0,0,'{}',10,20)"
        )
        connection.execute(
            'INSERT INTO recording_sessions('
            'id,room_id,broadcast_session_key,live_start_time,state,started_at) '
            "VALUES(1,100,'100:900',900,'closed',900)"
        )
        connection.commit()
    finally:
        connection.close()

    database = BiliUploadDatabase(str(path))
    await database.open()
    try:
        row = await database.fetchone(
            'SELECT display_name,avatar_url,credential_expires_at,created_at '
            'FROM bili_accounts WHERE id=1'
        )
        assert row is not None
        assert dict(row) == {
            'display_name': 'existing',
            'avatar_url': '',
            'credential_expires_at': 0,
            'created_at': 10,
        }
        assert (
            await database.scalar(
                'SELECT primary_account_id FROM bili_account_selection WHERE id=1'
            )
            == 1
        )
        policy = await database.fetchone(
            'SELECT account_mode,account_id,part_title_template,dynamic_template,'
            'is_only_self,publish_dynamic,no_reprint,up_selection_reply,'
            'up_close_reply,up_close_danmu,creation_statement_id,'
            'original_authorization FROM room_upload_policies '
            'WHERE room_id=100'
        )
        assert policy is not None
        assert dict(policy) == {
            'account_mode': 'fixed',
            'account_id': 1,
            'part_title_template': 'P{{ part_index }}',
            'dynamic_template': '',
            'is_only_self': 0,
            'publish_dynamic': 1,
            'no_reprint': 1,
            'up_selection_reply': 0,
            'up_close_reply': 0,
            'up_close_danmu': 0,
            'creation_statement_id': -1,
            'original_authorization': 1,
        }
        session = await database.fetchone(
            'SELECT room_id,title,cover_url,anchor_name,area_name '
            'FROM recording_sessions WHERE id=1'
        )
        assert session is not None
        assert dict(session) == {
            'room_id': 100,
            'title': '',
            'cover_url': '',
            'anchor_name': '',
            'area_name': '',
        }
        assert await database.scalar('SELECT MAX(version) FROM schema_migrations') == 16
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_eighth_migration_derives_current_creation_statement_fields(
    tmp_path: Path,
) -> None:
    path = tmp_path / 'blrec.sqlite3'
    migration_directory = (
        Path(__file__).parents[2] / 'src' / 'blrec' / 'bili_upload' / 'migrations'
    )
    connection = sqlite3.connect(str(path))
    try:
        for version in range(1, 8):
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
            'INSERT INTO bili_accounts('
            'id,uid,display_name,credential_ciphertext,credential_version,key_id,'
            'state,created_at,updated_at) '
            "VALUES(1,42,'existing',X'00',1,'key','active',10,20)"
        )
        for room_id, copyright_value, no_reprint in ((100, 1, 0), (101, 2, 1)):
            connection.execute(
                'INSERT INTO room_upload_policies('
                'room_id,account_mode,account_id,enabled,title_template,'
                'description_template,tid,tags,copyright,source,auto_comment,'
                'danmaku_backfill,filter_json,created_at,updated_at,no_reprint) '
                "VALUES(?,'fixed',1,1,'title','description',17,'tag',?,?,0,0,"
                "'{}',10,20,?)",
                (
                    room_id,
                    copyright_value,
                    'https://example.com/source' if copyright_value == 2 else '',
                    no_reprint,
                ),
            )
        connection.commit()
    finally:
        connection.close()

    database = BiliUploadDatabase(str(path))
    await database.open()
    try:
        rows = await database.fetchall(
            'SELECT room_id,copyright,no_reprint,creation_statement_id,'
            'original_authorization FROM room_upload_policies ORDER BY room_id'
        )
        assert [dict(row) for row in rows] == [
            {
                'room_id': 100,
                'copyright': 3,
                'no_reprint': 0,
                'creation_statement_id': -1,
                'original_authorization': 0,
            },
            {
                'room_id': 101,
                'copyright': 2,
                'no_reprint': 0,
                'creation_statement_id': -2,
                'original_authorization': 0,
            },
        ]
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
