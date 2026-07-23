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
from blrec.request_metrics import request_metrics_scope

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
    'highlight_markers',
    'highlight_clips',
    'highlight_clip_sources',
    'highlight_inspections',
    'local_deletion_items',
    'owner_handoff_outcomes',
    'upload_retry_batches',
    'upload_retry_batch_items',
    'media_library_items',
    'media_library_tags',
    'media_library_item_tags',
    'media_library_parts',
    'media_library_file_moves',
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
        assert await database.scalar('SELECT MAX(version) FROM schema_migrations') == 30
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
        recording_part_columns = {
            row['name']
            for row in await database.fetchall('PRAGMA table_info(recording_parts)')
        }
        session_columns = {
            row['name']
            for row in await database.fetchall('PRAGMA table_info(recording_sessions)')
        }
        clip_columns = {
            row['name']
            for row in await database.fetchall('PRAGMA table_info(highlight_clips)')
        }
        assert 'cancellation_generation' in session_columns
        assert {
            'cancellation_generation',
            'deletion_state',
            'deletion_error',
            'deletion_requested_at',
        } <= clip_columns
        assert {'upload_excluded_reason', 'upload_probe_attempt'} <= (
            recording_part_columns
        )
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
            'repair_reupload_snapshot_json',
            'operator_paused',
            'operator_resume_state',
            'submission_verification_state',
            'submission_verified_at',
            'submission_verification_json',
            'preupload_finalized',
        } <= job_columns
        await database.execute(
            "INSERT INTO bili_accounts("
            "id,uid,display_name,credential_ciphertext,credential_version,key_id,"
            "state,created_at,updated_at) "
            "VALUES(99,99,'迁移测试账号',X'00',1,'k','active',1,1)"
        )
        await database.execute(
            "INSERT INTO recording_sessions("
            "id,room_id,broadcast_session_key,state,started_at) "
            "VALUES(99,99,'migration:99','closed',1)"
        )
        await database.execute(
            "INSERT INTO upload_jobs("
            "id,session_id,account_id,policy_snapshot_json,state,submit_state,"
            "created_at,updated_at) "
            "VALUES(99,99,99,'{}','waiting_artifacts','prepared',1,1)"
        )
        assert (
            await database.scalar(
                'SELECT preupload_finalized FROM upload_jobs WHERE id=99'
            )
            == 1
        )
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
            'source_kind',
            'upload_decision',
            'upload_override_json',
            'upload_resolution_state',
            'upload_resolution_error',
            'upload_resolved_at',
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
            'timeline_start_at_ms',
            'media_index_state',
            'media_index_error',
            'media_index_progress',
            'media_index_updated_at',
        } <= part_columns
        marker_columns = {
            row['name']
            for row in await database.fetchall('PRAGMA table_info(highlight_markers)')
        }
        assert {
            'recording_part_id',
            'part_anchor_at_ms',
            'current_time_ms',
            'seekable_end_ms',
            'raw_delay_ms',
            'baseline_delay_ms',
            'effective_rewind_ms',
        } <= marker_columns
        clip_columns = {
            row['name']
            for row in await database.fetchall('PRAGMA table_info(highlight_clips)')
        }
        assert 'file_size_bytes' in clip_columns
        await database.execute(
            'INSERT INTO highlight_clips('
            'id,room_id,name,requested_start_ms,requested_end_ms,state,'
            'file_size_bytes,created_at,updated_at) '
            "VALUES(99,99,'size check',0,1000,'ready',NULL,1,1)"
        )
        assert (
            await database.scalar(
                'SELECT file_size_bytes FROM highlight_clips WHERE id=99'
            )
            is None
        )
        with pytest.raises(sqlite3.IntegrityError):
            await database.execute(
                'UPDATE highlight_clips SET file_size_bytes=-1 WHERE id=99'
            )

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
            'highlight_clips_claim_idx',
            'recording_sessions_source_started_idx',
            'upload_jobs_state_session_idx',
            'highlight_clips_library_idx',
            'upload_retry_batch_items_state_idx',
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
async def test_media_library_schema_constraints_and_list_index(tmp_path: Path) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'blrec.sqlite3'))
    await database.open()
    try:
        await database.execute(
            "INSERT INTO recording_sessions("
            "id,room_id,broadcast_session_key,state,started_at) "
            "VALUES(1,100,'100:1','closed',1)"
        )
        await database.execute(
            'INSERT INTO media_library_items('
            'id,session_id,kind,origin,storage_key,display_name,state,'
            'created_at,updated_at) '
            "VALUES(1,1,'broadcast','recording','0123456789abcdef0123456789abcdef',"
            "'第一场直播','ready',1,1)"
        )
        with pytest.raises(sqlite3.IntegrityError):
            await database.execute(
                'INSERT INTO media_library_items('
                'session_id,kind,origin,storage_key,display_name,state,'
                'created_at,updated_at) '
                "VALUES(1,'broadcast','recording',"
                "'fedcba9876543210fedcba9876543210','重复收藏','ready',1,1)"
            )
        with pytest.raises(sqlite3.IntegrityError):
            await database.execute(
                'UPDATE media_library_items SET state=?,error=NULL WHERE id=1',
                ('failed',),
            )
        with pytest.raises(sqlite3.IntegrityError):
            await database.execute(
                'UPDATE media_library_items SET display_name=? WHERE id=1', ('   ',)
            )

        await database.execute(
            "INSERT INTO media_library_tags(id,name) VALUES(1,'Keep')"
        )
        with pytest.raises(sqlite3.IntegrityError):
            await database.execute(
                "INSERT INTO media_library_tags(id,name) VALUES(2,'keep')"
            )
        await database.execute(
            'INSERT INTO media_library_item_tags(item_id,tag_id) VALUES(1,1)'
        )

        indexes = {
            row['name']
            for row in await database.fetchall(
                "SELECT name FROM sqlite_master WHERE type='index'"
            )
        }
        assert {
            'media_library_items_list_idx',
            'media_library_parts_recording_part_idx',
            'media_library_item_tags_tag_idx',
            'media_library_file_moves_state_idx',
            'highlight_clips_source_library_idx',
        } <= indexes
        plan = await database.fetchall(
            'EXPLAIN QUERY PLAN SELECT id FROM media_library_items '
            'WHERE kind=? '
            'ORDER BY created_at DESC,id DESC LIMIT 20',
            ('broadcast',),
        )
        assert any('media_library_items_list_idx' in str(row['detail']) for row in plan)
        assert not any('TEMP B-TREE' in str(row['detail']) for row in plan)

        await database.execute('DELETE FROM recording_sessions WHERE id=1')
        assert await database.scalar('SELECT COUNT(*) FROM media_library_items') == 0
        assert (
            await database.scalar('SELECT COUNT(*) FROM media_library_item_tags') == 0
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
        assert await database.scalar('SELECT MAX(version) FROM schema_migrations') == 30
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_twenty_fourth_migration_preserves_legacy_highlight_clip(
    tmp_path: Path,
) -> None:
    path = tmp_path / 'blrec.sqlite3'
    migration_directory = (
        Path(__file__).parents[2] / 'src' / 'blrec' / 'bili_upload' / 'migrations'
    )
    connection = sqlite3.connect(str(path))
    try:
        for version in range(1, 24):
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
            'INSERT INTO highlight_clips('
            'id,room_id,name,requested_start_ms,requested_end_ms,'
            'output_video_path,state,created_at,updated_at) '
            "VALUES(7,100,'旧片段',0,1000,'/clips/legacy.mp4','ready',1,2)"
        )
        connection.commit()
    finally:
        connection.close()

    database = BiliUploadDatabase(str(path))
    await database.open()
    try:
        row = await database.fetchone(
            'SELECT name,output_video_path,file_size_bytes '
            'FROM highlight_clips WHERE id=7'
        )
        assert row is not None
        assert dict(row) == {
            'name': '旧片段',
            'output_video_path': '/clips/legacy.mp4',
            'file_size_bytes': None,
        }
        assert await database.scalar('SELECT MAX(version) FROM schema_migrations') == 30
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_twenty_fifth_migration_adds_only_hot_read_indexes(
    tmp_path: Path,
) -> None:
    path = tmp_path / 'blrec.sqlite3'
    migration_directory = (
        Path(__file__).parents[2] / 'src' / 'blrec' / 'bili_upload' / 'migrations'
    )
    connection = sqlite3.connect(str(path))
    try:
        for version in range(1, 25):
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
            'INSERT INTO highlight_clips('
            'id,room_id,name,requested_start_ms,requested_end_ms,state,'
            'file_size_bytes,created_at,updated_at) '
            "VALUES(8,100,'version 24 clip',0,1000,'ready',123,1,2)"
        )
        connection.commit()
    finally:
        connection.close()

    database = BiliUploadDatabase(str(path))
    await database.open()
    try:
        assert await database.scalar('SELECT MAX(version) FROM schema_migrations') == 30
        assert (
            await database.scalar(
                'SELECT file_size_bytes FROM highlight_clips WHERE id=8'
            )
            == 123
        )
        indexes = {
            str(row['name'])
            for row in await database.fetchall(
                "SELECT name FROM sqlite_master WHERE type='index'"
            )
        }
        assert {
            'recording_sessions_source_started_idx',
            'upload_jobs_state_session_idx',
            'highlight_clips_library_idx',
        } <= indexes
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_twenty_sixth_migration_adds_recoverable_deletion_state(
    tmp_path: Path,
) -> None:
    path = tmp_path / 'blrec.sqlite3'
    migration_directory = (
        Path(__file__).parents[2] / 'src' / 'blrec' / 'bili_upload' / 'migrations'
    )
    connection = sqlite3.connect(str(path))
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
            "VALUES(1,100,'legacy:1','closed',1,'none',NULL),"
            "(2,100,'legacy:2','closed',1,'requested',1),"
            "(3,100,'legacy:3','closed',1,'deleting',1),"
            "(4,100,'legacy:4','closed',1,'failed',1)"
        )
        connection.execute(
            'INSERT INTO highlight_clips('
            'id,room_id,name,requested_start_ms,requested_end_ms,state,'
            'created_at,updated_at) '
            "VALUES(1,100,'legacy clip',0,1000,'ready',1,1)"
        )
        connection.commit()
    finally:
        connection.close()

    database = BiliUploadDatabase(str(path))
    await database.open()
    try:
        assert await database.scalar('SELECT MAX(version) FROM schema_migrations') == 30
        sessions = await database.fetchall(
            'SELECT id,cancellation_generation FROM recording_sessions ORDER BY id'
        )
        clip = await database.fetchone(
            'SELECT cancellation_generation,deletion_state,deletion_error '
            'FROM highlight_clips WHERE id=1'
        )
        assert [dict(session) for session in sessions] == [
            {'id': 1, 'cancellation_generation': 0},
            {'id': 2, 'cancellation_generation': 1},
            {'id': 3, 'cancellation_generation': 1},
            {'id': 4, 'cancellation_generation': 1},
        ]
        assert clip is not None and dict(clip) == {
            'cancellation_generation': 0,
            'deletion_state': 'none',
            'deletion_error': None,
        }
        with pytest.raises(sqlite3.IntegrityError):
            await database.execute(
                'INSERT INTO owner_handoff_outcomes('
                'owner_kind,owner_id,side_effect_key,source_generation,'
                'outcome_state,outcome_json,acknowledged_at) '
                "VALUES('upload',1,'submit',0,'in_flight','{}',1)"
            )
        with pytest.raises(sqlite3.IntegrityError):
            await database.execute(
                'INSERT INTO owner_handoff_outcomes('
                'owner_kind,owner_id,side_effect_key,source_generation,'
                'outcome_state,outcome_json) '
                "VALUES('upload',1,'submit',0,'unknown_terminal','{}')"
            )
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_twenty_seventh_migration_persists_safe_highlight_inspections(
    tmp_path: Path,
) -> None:
    path = tmp_path / 'blrec.sqlite3'
    migration_directory = (
        Path(__file__).parents[2] / 'src' / 'blrec' / 'bili_upload' / 'migrations'
    )
    connection = sqlite3.connect(str(path))
    try:
        for version in range(1, 27):
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
            'INSERT INTO highlight_clips('
            'id,room_id,name,requested_start_ms,requested_end_ms,state,'
            'created_at,updated_at) '
            "VALUES(7,100,'旧片段',0,1000,'ready',1,1)"
        )
        connection.commit()
    finally:
        connection.close()

    database = BiliUploadDatabase(str(path))
    await database.open()
    try:
        assert await database.scalar('SELECT MAX(version) FROM schema_migrations') == 30
        legacy = await database.fetchone(
            'SELECT inspection_json,source_fingerprint_json,idempotency_key '
            'FROM highlight_clips WHERE id=7'
        )
        assert legacy is not None and dict(legacy) == {
            'inspection_json': None,
            'source_fingerprint_json': None,
            'idempotency_key': None,
        }
        await database.execute(
            'INSERT INTO highlight_inspections('
            'operation_id,session_id,requested_start_ms,requested_end_ms,'
            "idempotency_key,state,active_durations_json,created_at,updated_at) "
            "VALUES('op',1,0,1000,'idem','accepted','{}',1,1)"
        )
        with pytest.raises(sqlite3.IntegrityError):
            await database.execute(
                'INSERT INTO highlight_inspections('
                'operation_id,session_id,requested_start_ms,requested_end_ms,'
                "idempotency_key,state,active_durations_json,created_at,updated_at) "
                "VALUES('other',1,0,1000,'idem','accepted','{}',1,1)"
            )
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_twenty_eighth_migration_adds_repair_reupload_snapshot(
    tmp_path: Path,
) -> None:
    path = tmp_path / 'blrec.sqlite3'
    migration_directory = (
        Path(__file__).parents[2] / 'src' / 'blrec' / 'bili_upload' / 'migrations'
    )
    connection = sqlite3.connect(str(path))
    try:
        for version in range(1, 28):
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
            "INSERT INTO bili_accounts("
            "id,uid,display_name,credential_ciphertext,credential_version,key_id,"
            "state,created_at,updated_at) "
            "VALUES(1,42,'u',X'00',1,'k','active',1,1)"
        )
        connection.execute(
            "INSERT INTO recording_sessions("
            "id,room_id,broadcast_session_key,state,started_at) "
            "VALUES(1,100,'100:1','closed',1)"
        )
        connection.execute(
            'INSERT INTO upload_jobs('
            'id,session_id,account_id,policy_snapshot_json,state,submit_state,'
            "created_at,updated_at) VALUES(1,1,1,'{}','waiting_review',"
            "'confirmed',1,1)"
        )
        connection.commit()
    finally:
        connection.close()

    database = BiliUploadDatabase(str(path))
    await database.open()
    try:
        assert await database.scalar('SELECT MAX(version) FROM schema_migrations') == 30
        assert (
            await database.scalar(
                'SELECT repair_reupload_snapshot_json FROM upload_jobs WHERE id=1'
            )
            is None
        )
        await database.execute(
            'UPDATE upload_jobs SET repair_reupload_snapshot_json=? WHERE id=1',
            ('{"format_version":1}',),
        )
        assert (
            await database.scalar(
                'SELECT repair_reupload_snapshot_json FROM upload_jobs WHERE id=1'
            )
            == '{"format_version":1}'
        )
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_nineteenth_migration_preserves_jobs_and_resets_open_auto_sessions(
    tmp_path: Path,
) -> None:
    path = tmp_path / 'blrec.sqlite3'
    migration_directory = (
        Path(__file__).parents[2] / 'src' / 'blrec' / 'bili_upload' / 'migrations'
    )
    connection = sqlite3.connect(str(path))
    try:
        for version in range(1, 19):
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
            "VALUES(1,42,'existing',X'00',1,'key','active',1,1)"
        )
        for session_id, state, intent in (
            (1, 'open', 'auto'),
            (2, 'open', 'upload'),
            (3, 'open', 'skip'),
            (4, 'closed', 'none'),
            (5, 'closed', 'auto'),
        ):
            connection.execute(
                'INSERT INTO recording_sessions('
                'id,room_id,broadcast_session_key,state,started_at,upload_intent) '
                'VALUES(?,?,?,?,1,?)',
                (
                    session_id,
                    100 + session_id,
                    'session-{}'.format(session_id),
                    state,
                    intent,
                ),
            )
        connection.execute(
            'INSERT INTO upload_jobs('
            'session_id,account_id,policy_snapshot_json,state,submit_state,'
            'created_at,updated_at) '
            "VALUES(5,1,'{}','ready','prepared',1,1)"
        )
        connection.commit()
    finally:
        connection.close()

    database = BiliUploadDatabase(str(path))
    await database.open()
    try:
        rows = await database.fetchall(
            'SELECT id,upload_decision,upload_resolution_state '
            'FROM recording_sessions ORDER BY id'
        )
        assert [tuple(row) for row in rows] == [
            (1, 'follow_room', 'pending'),
            (2, 'upload', 'pending'),
            (3, 'skip', 'pending'),
            (4, 'follow_room', 'not_requested'),
            (5, 'follow_room', 'job_created'),
        ]
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_highlight_markers_are_independent_and_clips_are_claimable(
    tmp_path: Path,
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'blrec.sqlite3'))
    await database.open()
    try:
        await database.execute(
            "INSERT INTO highlight_markers("
            "id,room_id,observed_at_ms,player_delay_ms,content_at_ms,title,"
            "anchor_name,name,note,source,created_at,updated_at) "
            "VALUES(1,100,20000,1500,18500,'直播标题','主播','高光 00:18','',"
            "'browser_extension',20,20)"
        )
        marker = await database.fetchone(
            'SELECT room_id,content_at_ms,name FROM highlight_markers WHERE id=1'
        )
        assert marker is not None
        assert dict(marker) == {
            'room_id': 100,
            'content_at_ms': 18500,
            'name': '高光 00:18',
        }

        with pytest.raises(sqlite3.IntegrityError):
            await database.execute(
                "INSERT INTO recording_sessions("
                "room_id,broadcast_session_key,state,started_at,source_kind) "
                "VALUES(100,'100:invalid','closed',1,'invalid')"
            )
        with pytest.raises(sqlite3.IntegrityError):
            await database.execute(
                "INSERT INTO highlight_clips("
                "room_id,name,requested_start_ms,requested_end_ms,state,"
                "created_at,updated_at) "
                "VALUES(100,'错误区间',10000,10000,'queued',20,20)"
            )

        await database.execute(
            "INSERT INTO highlight_clips("
            "id,marker_id,room_id,name,requested_start_ms,requested_end_ms,state,"
            "created_at,updated_at) "
            "VALUES(1,1,100,'高光片段',10000,20000,'queued',20,20)"
        )
        claim = await database.claim(
            'highlight_clips', ('queued',), 'highlight-worker', now=20
        )
        assert claim is not None
        assert claim.id == 1
        assert claim.lease_owner == 'highlight-worker'

        await database.execute('DELETE FROM highlight_markers WHERE id=1')
        assert (
            await database.scalar('SELECT marker_id FROM highlight_clips WHERE id=1')
            is None
        )
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
async def test_database_executor_records_request_metrics(tmp_path: Path) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'blrec.sqlite3'))
    try:
        with request_metrics_scope() as metrics:
            await database.open()

        assert metrics.database_calls == 1
        assert metrics.database_ms > 0.0
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
