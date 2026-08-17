from pathlib import Path
from typing import Awaitable, Callable, Optional

import pytest

from blrec.bili_upload.database import BiliUploadDatabase
from blrec.bili_upload.remote_media import RemoteMediaCache


class FakeDownloader:
    def __init__(self) -> None:
        self.calls = []

    async def download(
        self,
        bundle: object,
        *,
        bvid: str,
        cid: int,
        page: int,
        target: Path,
        progress: Callable[[int, Optional[int]], Awaitable[None]],
    ) -> None:
        self.calls.append((bundle, bvid, cid, page, target))
        await progress(4, 8)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b'video123')
        await progress(8, 8)


async def seed_remote_part(database: BiliUploadDatabase, source_path: Path) -> None:
    await database.execute(
        "INSERT INTO bili_accounts("
        "id,uid,display_name,credential_ciphertext,credential_version,key_id,"
        "state,created_at,updated_at) "
        "VALUES(1,42,'账号',X'00',1,'key','active',1,1)"
    )
    await database.execute(
        'INSERT INTO recording_sessions('
        'id,room_id,broadcast_session_key,state,started_at,title) '
        "VALUES(1,100,'session:1','closed',1,'已投稿录像')"
    )
    await database.execute(
        "INSERT INTO recording_runs(id,session_id,state,started_at,ended_at) "
        "VALUES('run:1',1,'finished',1,2)"
    )
    await database.execute(
        'INSERT INTO recording_parts('
        'id,session_id,run_id,part_index,source_path,final_path,'
        'record_start_time,artifact_state,video_deleted_at,file_size_bytes,'
        'created_at,updated_at) '
        "VALUES(1,1,'run:1',1,?,? ,1,'missing',50,99,1,1)",
        (str(source_path), str(source_path)),
    )
    await database.execute(
        'INSERT INTO upload_jobs('
        'id,session_id,account_id,policy_snapshot_json,state,submit_state,'
        'bvid,created_at,updated_at) '
        "VALUES(1,1,1,'{}','approved','confirmed','BV1abcdefgh',1,1)"
    )
    await database.execute(
        'INSERT INTO upload_parts('
        'id,job_id,part_index,source_path,artifact_state,upload_state,cid) '
        "VALUES(1,1,1,?,'missing','confirmed',123)",
        (str(source_path),),
    )


async def seed_migration_target_archive(database: BiliUploadDatabase) -> None:
    await database.execute(
        "INSERT INTO bili_accounts("
        "id,uid,display_name,credential_ciphertext,credential_version,key_id,"
        "state,created_at,updated_at) "
        "VALUES(2,84,'下载账号',X'00',1,'key','active',1,1)"
    )
    await database.execute(
        'INSERT INTO archive_migration_jobs('
        'id,source_uid,download_account_id,target_account_id,state,progress,'
        'discovered_count,completed_count,failed_count,error,requested_at,'
        'started_at,completed_at,updated_at) '
        "VALUES(1,42,2,1,'completed',1,1,1,0,NULL,1,1,1,1)"
    )
    await database.execute(
        'INSERT INTO archive_migration_items('
        'id,migration_id,aid,bvid,title,published_at,state,progress,page_count,'
        'downloaded_page_count,session_id,upload_job_id,error,created_at,updated_at) '
        "VALUES(1,1,101,'BV1source001','源稿件',1,'task_created',1,1,1,"
        '1,1,NULL,1,1)'
    )
    await database.execute(
        'INSERT INTO vainglory_archive_syncs('
        'account_id,state,progress,discovered_count,completed_count,error,'
        'requested_at,started_at,completed_at,updated_at) '
        "VALUES(1,'running',0,1,0,NULL,1,1,NULL,1)"
    )
    await database.execute(
        'INSERT INTO vainglory_archive_imports('
        'id,account_id,aid,bvid,title,published_at,session_id,state,progress,'
        'page_count,completed_page_count,created_at,updated_at) '
        "VALUES(1,1,201,'BV1abcdefgh','目标稿件',1,1,'queued',0,1,0,1,1)"
    )
    await database.execute(
        'INSERT INTO vainglory_archive_parts('
        'id,import_id,page,cid,title,duration_seconds,recording_part_id,state,'
        'progress,created_at,updated_at) '
        "VALUES(1,1,1,123,'P1',60,1,'queued',0,1,1)"
    )


@pytest.mark.asyncio
async def test_downloads_missing_submitted_part_and_expires_after_ten_days(
    tmp_path: Path,
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'blrec.sqlite3'))
    await database.open()
    now = [1_000]
    downloader = FakeDownloader()
    try:
        missing = tmp_path / 'deleted.mp4'
        await seed_remote_part(database, missing)
        cache = RemoteMediaCache(
            database,
            tmp_path,
            bundle_loader=lambda _account_id: async_value('credential'),
            downloader=downloader,
            clock=lambda: now[0],
        )

        requested = await cache.request(1)
        assert requested.state == 'pending'
        assert requested.progress == 0

        assert await cache.run_once() is True
        ready = await cache.status(1)
        assert ready.state == 'ready'
        assert ready.progress == 1
        assert ready.expires_at == 1_000 + 10 * 24 * 60 * 60
        assert downloader.calls[0][1:4] == ('BV1abcdefgh', 123, 1)
        assert ready.cache_path is not None
        assert Path(ready.cache_path).read_bytes() == b'video123'
        part = await database.fetchone(
            'SELECT final_path,artifact_state,video_deleted_at,file_size_bytes '
            'FROM recording_parts WHERE id=1'
        )
        assert part is not None
        assert str(part['final_path']) == ready.cache_path
        assert str(part['artifact_state']) == 'ready'
        assert part['video_deleted_at'] is None
        assert int(part['file_size_bytes']) == 8

        now[0] = ready.expires_at or 0
        assert await cache.cleanup_expired() == 1
        assert not Path(ready.cache_path).exists()
        restored = await database.fetchone(
            'SELECT final_path,artifact_state,video_deleted_at,file_size_bytes '
            'FROM recording_parts WHERE id=1'
        )
        assert restored is not None
        assert str(restored['final_path']) == str(missing)
        assert str(restored['artifact_state']) == 'missing'
        assert int(restored['video_deleted_at']) == 50
        assert int(restored['file_size_bytes']) == 99
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_uses_existing_local_video_without_queuing_download(
    tmp_path: Path,
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'blrec.sqlite3'))
    await database.open()
    downloader = FakeDownloader()
    try:
        local = tmp_path / 'local.mp4'
        local.write_bytes(b'local')
        await seed_remote_part(database, local)
        await database.execute(
            "UPDATE recording_parts SET artifact_state='ready',"
            'video_deleted_at=NULL,file_size_bytes=5 WHERE id=1'
        )
        cache = RemoteMediaCache(
            database,
            tmp_path,
            bundle_loader=lambda _account_id: async_value('credential'),
            downloader=downloader,
            clock=lambda: 1_000,
        )

        status = await cache.request(1)

        assert status.state == 'local'
        assert await cache.run_once() is False
        assert downloader.calls == []
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_force_remote_queues_download_for_logically_deleted_local_video(
    tmp_path: Path,
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'blrec.sqlite3'))
    await database.open()
    downloader = FakeDownloader()
    try:
        local = tmp_path / 'deleted-but-present.mp4'
        local.write_bytes(b'local')
        await seed_remote_part(database, local)
        cache = RemoteMediaCache(
            database,
            tmp_path,
            bundle_loader=lambda _account_id: async_value('credential'),
            downloader=downloader,
            clock=lambda: 1_000,
        )

        assert (await cache.request(1)).state == 'local'
        requested = await cache.request(1, force_remote=True)

        assert requested.state == 'pending'
        assert await cache.run_once() is True
        assert len(downloader.calls) == 1
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_manual_reanalysis_downloads_before_regular_remote_media(
    tmp_path: Path,
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'blrec.sqlite3'))
    await database.open()
    downloader = FakeDownloader()
    try:
        first_path = tmp_path / 'manual.mp4'
        second_path = tmp_path / 'regular.mp4'
        await seed_remote_part(database, first_path)
        await database.execute(
            'INSERT INTO recording_sessions('
            'id,room_id,broadcast_session_key,state,started_at,title) '
            "VALUES(2,200,'session:2','closed',2,'普通下载')"
        )
        await database.execute(
            "INSERT INTO recording_runs(id,session_id,state,started_at,ended_at) "
            "VALUES('run:2',2,'finished',2,3)"
        )
        await database.execute(
            'INSERT INTO recording_parts('
            'id,session_id,run_id,part_index,source_path,final_path,'
            'record_start_time,artifact_state,video_deleted_at,file_size_bytes,'
            'created_at,updated_at) '
            "VALUES(2,2,'run:2',1,?,? ,2,'missing',50,99,2,2)",
            (str(second_path), str(second_path)),
        )
        await database.execute(
            'INSERT INTO upload_jobs('
            'id,session_id,account_id,policy_snapshot_json,state,submit_state,'
            'bvid,created_at,updated_at) '
            "VALUES(2,2,1,'{}','approved','confirmed','BV1ijklmnop',2,2)"
        )
        await database.execute(
            'INSERT INTO upload_parts('
            'id,job_id,part_index,source_path,artifact_state,upload_state,cid) '
            "VALUES(2,2,1,?,'missing','confirmed',456)",
            (str(second_path),),
        )
        cache = RemoteMediaCache(
            database,
            tmp_path,
            bundle_loader=lambda _account_id: async_value('credential'),
            downloader=downloader,
            clock=lambda: 1_000,
        )
        assert (await cache.request(1, force_remote=True)).state == 'pending'
        assert (await cache.request(2, force_remote=True)).state == 'pending'
        await database.execute(
            "UPDATE vainglory_video_sources SET retention_kind='analysis' "
            'WHERE part_id=1'
        )
        await database.execute(
            'INSERT INTO vainglory_part_jobs('
            'part_id,session_id,state,request_kind,progress,algorithm_version,'
            'match_count,error,requested_at,started_at,completed_at,updated_at) '
            "VALUES(1,1,'pending','manual',0,1,0,NULL,1000,NULL,NULL,1000)"
        )

        assert await cache.run_once() is True

        assert downloader.calls[0][1:4] == ('BV1abcdefgh', 123, 1)
        assert (
            await database.scalar(
                'SELECT state FROM vainglory_video_sources WHERE part_id=2'
            )
            == 'pending'
        )
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_explicit_user_download_promotes_analysis_cache_to_ten_days(
    tmp_path: Path,
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'blrec.sqlite3'))
    await database.open()
    try:
        missing = tmp_path / 'deleted.mp4'
        await seed_remote_part(database, missing)
        await database.execute(
            'INSERT INTO vainglory_video_sources('
            'part_id,account_id,bvid,cid,page,origin,state,retention_kind,'
            'progress,downloaded_bytes,original_artifact_state,created_at,'
            'updated_at) '
            "VALUES(1,1,'BV1abcdefgh',123,1,'archive','missing','analysis',"
            "0,0,'missing',1,1)"
        )
        cache = RemoteMediaCache(
            database,
            tmp_path,
            bundle_loader=lambda _account_id: async_value('credential'),
            downloader=FakeDownloader(),
            clock=lambda: 1_000,
        )

        requested = await cache.request(1, retain_for_playback=True)

        assert requested.state == 'pending'
        assert (
            await database.scalar(
                'SELECT retention_kind FROM vainglory_video_sources WHERE part_id=1'
            )
            == 'ten_day'
        )
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_does_not_download_migration_target_discovered_by_history_intake(
    tmp_path: Path,
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'blrec.sqlite3'))
    await database.open()
    downloader = FakeDownloader()
    try:
        missing = tmp_path / 'deleted.mp4'
        await seed_remote_part(database, missing)
        await seed_migration_target_archive(database)
        await database.execute(
            'INSERT INTO vainglory_video_sources('
            'part_id,account_id,bvid,cid,page,origin,state,retention_kind,'
            'progress,downloaded_bytes,original_artifact_state,created_at,'
            'updated_at) '
            "VALUES(1,1,'BV1abcdefgh',123,1,'archive','pending','analysis',"
            "0,0,'missing',1,1)"
        )
        cache = RemoteMediaCache(
            database,
            tmp_path,
            bundle_loader=lambda _account_id: async_value('credential'),
            downloader=downloader,
            clock=lambda: 1_000,
        )

        assert await cache.run_once() is False
        assert downloader.calls == []
        assert (
            await database.scalar(
                'SELECT state FROM vainglory_video_sources WHERE part_id=1'
            )
            == 'pending'
        )
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_reports_and_persists_remote_download_queue_concurrency(
    tmp_path: Path,
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'blrec.sqlite3'))
    await database.open()
    try:
        missing = tmp_path / 'deleted.mp4'
        await seed_remote_part(database, missing)
        cache = RemoteMediaCache(
            database,
            tmp_path,
            bundle_loader=lambda _account_id: async_value('credential'),
            downloader=FakeDownloader(),
            download_interfaces=('wan-a', 'wan-b'),
            clock=lambda: 1_000,
        )
        assert (await cache.request(1, force_remote=True)).state == 'pending'

        initial = await cache.queue_status()

        assert initial.pending_download_count == 1
        assert initial.active_download_count == 0
        assert initial.downloaded_waiting_analysis_count == 0
        assert initial.active_analysis_count == 0
        assert initial.failed_download_count == 0
        assert initial.downloads_per_interface == 3
        assert initial.interface_count == 2
        assert initial.total_concurrency == 6

        updated = await cache.update_downloads_per_interface(5)

        assert updated.downloads_per_interface == 5
        assert updated.total_concurrency == 10
        restarted = RemoteMediaCache(
            database,
            tmp_path,
            bundle_loader=lambda _account_id: async_value('credential'),
            downloader=FakeDownloader(),
            download_interfaces=('wan-a', 'wan-b'),
            clock=lambda: 1_001,
        )
        assert (await restarted.queue_status()).downloads_per_interface == 5
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_rejects_unsafe_remote_download_concurrency(tmp_path: Path) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'blrec.sqlite3'))
    await database.open()
    try:
        cache = RemoteMediaCache(
            database,
            tmp_path,
            bundle_loader=lambda _account_id: async_value('credential'),
            downloader=FakeDownloader(),
            clock=lambda: 1_000,
        )

        with pytest.raises(ValueError, match='1 到 8'):
            await cache.update_downloads_per_interface(0)
        with pytest.raises(ValueError, match='1 到 8'):
            await cache.update_downloads_per_interface(9)
    finally:
        await database.close()


async def async_value(value: object) -> object:
    return value
