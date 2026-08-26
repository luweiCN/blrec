import asyncio
from pathlib import Path
from typing import Awaitable, Callable, Optional

import pytest

from blrec.bili_upload.bili_download import BiliDownloadContractError
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
async def test_background_downloader_survives_transient_database_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache = RemoteMediaCache(
        object(),  # type: ignore[arg-type]
        tmp_path,
        bundle_loader=lambda _account_id: async_value('credential'),
        downloader=FakeDownloader(),
    )
    attempts = 0
    recoveries = 0

    async def run_once(**_kwargs: object) -> bool:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError('database tunnel restarted')
        raise asyncio.CancelledError

    async def recover_orphans() -> int:
        nonlocal recoveries
        recoveries += 1
        return 0

    async def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(cache, 'run_once', run_once)
    monkeypatch.setattr(cache, '_recover_orphaned_downloads', recover_orphans)
    monkeypatch.setattr('blrec.bili_upload.remote_media.asyncio.sleep', no_sleep)

    with pytest.raises(asyncio.CancelledError):
        await cache._run_worker(None, 0, 0)

    assert attempts == 2
    assert recoveries == 1


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
async def test_finishes_started_archive_before_downloading_newer_archive(
    tmp_path: Path,
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'blrec.sqlite3'))
    await database.open()
    try:
        first_path = tmp_path / 'archive-1-p1.mp4'
        first_path.write_bytes(b'ready')
        await seed_remote_part(database, first_path)
        await database.execute(
            'INSERT INTO recording_parts('
            'id,session_id,run_id,part_index,source_path,final_path,'
            'record_start_time,artifact_state,video_deleted_at,file_size_bytes,'
            'created_at,updated_at) '
            "VALUES(2,1,'run:1',2,?,NULL,2,'missing',50,0,2,2)",
            (str(tmp_path / 'archive-1-p2.mp4'),),
        )
        await database.execute(
            'INSERT INTO recording_sessions('
            'id,room_id,broadcast_session_key,state,started_at,title) '
            "VALUES(2,200,'session:2','closed',2,'更新但未开始的稿件')"
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
            "VALUES(3,2,'run:2',1,?,NULL,2,'missing',50,0,3,3)",
            (str(tmp_path / 'archive-2-p1.mp4'),),
        )
        await database.execute(
            'INSERT INTO vainglory_archive_imports('
            'id,account_id,aid,bvid,title,published_at,session_id,state,progress,'
            'page_count,completed_page_count,created_at,updated_at,'
            'recording_started_at) '
            "VALUES(1,1,101,'BV1abcdefgh','已开始稿件',1,1,'analyzing',0.5,"
            "2,0,1,1,1)"
        )
        await database.execute(
            'INSERT INTO vainglory_archive_imports('
            'id,account_id,aid,bvid,title,published_at,session_id,state,progress,'
            'page_count,completed_page_count,created_at,updated_at,'
            'recording_started_at) '
            "VALUES(2,1,102,'BV1ijklmnop','更新稿件',2,2,'analyzing',0,"
            "1,0,2,2,2)"
        )
        await database.execute(
            'INSERT INTO vainglory_archive_parts('
            'id,import_id,page,cid,title,duration_seconds,recording_part_id,'
            'state,progress,created_at,updated_at) VALUES'
            "(1,1,1,101,'P1',60,1,'analyzing',0.5,1,1),"
            "(2,1,2,102,'P2',60,2,'downloading',0,1,1),"
            "(3,2,1,201,'P1',60,3,'downloading',0,2,2)"
        )
        await database.execute(
            'INSERT INTO vainglory_video_sources('
            'part_id,account_id,bvid,cid,page,origin,state,retention_kind,'
            'progress,downloaded_bytes,total_bytes,cache_path,'
            'original_artifact_state,cached_at,expires_at,created_at,updated_at) '
            "VALUES(1,1,'BV1abcdefgh',101,1,'archive','ready','analysis',"
            "1,5,5,?,'missing',1,9999,1,1),"
            "(2,1,'BV1abcdefgh',102,2,'archive','pending','analysis',"
            "0,0,NULL,NULL,'missing',NULL,NULL,1,1),"
            "(3,1,'BV1ijklmnop',201,1,'archive','pending','analysis',"
            "0,0,NULL,NULL,'missing',NULL,NULL,2,2)",
            (str(first_path),),
        )
        cache = RemoteMediaCache(
            database,
            tmp_path,
            bundle_loader=lambda _account_id: async_value('credential'),
            downloader=FakeDownloader(),
            clock=lambda: 1_000,
        )

        claim = await cache._claim(None)

        assert claim is not None
        assert int(claim['part_id']) == 2
        assert (
            await database.scalar(
                'SELECT state FROM vainglory_video_sources WHERE part_id=3'
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
        assert initial.pending_download_archive_count == 1
        assert initial.active_download_count == 0
        assert initial.active_download_archive_count == 0
        assert initial.downloaded_waiting_analysis_count == 0
        assert initial.downloaded_waiting_analysis_archive_count == 0
        assert initial.active_analysis_count == 0
        assert initial.active_analysis_archive_count == 0
        assert initial.failed_download_count == 0
        assert initial.failed_download_archive_count == 0
        assert initial.downloads_per_interface == 3
        assert initial.interface_count == 2
        assert initial.total_concurrency == 6

        page = await cache.queue_items('pending')
        assert page.total == 1
        assert page.archive_count == 1
        assert page.items[0].part_id == 1
        assert page.items[0].archive_title == '已投稿录像'

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
async def test_queue_status_reports_distinct_livestreams_and_parts(
    tmp_path: Path,
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'blrec.sqlite3'))
    await database.open()
    try:
        missing = tmp_path / 'deleted.mp4'
        await seed_remote_part(database, missing)
        await database.execute(
            'INSERT INTO recording_parts('
            'id,session_id,run_id,part_index,source_path,final_path,'
            'record_start_time,artifact_state,video_deleted_at,file_size_bytes,'
            'created_at,updated_at) '
            "VALUES(2,1,'run:1',2,?,NULL,2,'missing',50,0,2,2)",
            (str(tmp_path / 'deleted-2.mp4'),),
        )
        await database.execute(
            'INSERT INTO vainglory_archive_imports('
            'id,account_id,aid,bvid,title,published_at,session_id,state,progress,'
            'page_count,completed_page_count,created_at,updated_at) '
            "VALUES(1,1,101,'BV1abcdefgh','两分批直播',1,1,'queued',0,2,0,1,1)"
        )
        await database.execute(
            'INSERT INTO vainglory_archive_parts('
            'id,import_id,page,cid,title,duration_seconds,recording_part_id,state,'
            'progress,created_at,updated_at) VALUES'
            "(1,1,1,123,'P1',60,1,'queued',0,1,1),"
            "(2,1,2,124,'P2',60,2,'queued',0,1,1)"
        )
        await database.execute(
            'INSERT INTO vainglory_video_sources('
            'part_id,account_id,bvid,cid,page,origin,state,retention_kind,'
            'progress,downloaded_bytes,original_artifact_state,created_at,'
            'updated_at) VALUES'
            "(1,1,'BV1abcdefgh',123,1,'archive','pending','analysis',"
            "0,0,'missing',1,1),"
            "(2,1,'BV1abcdefgh',124,2,'archive','pending','analysis',"
            "0,0,'missing',1,1)"
        )
        cache = RemoteMediaCache(
            database,
            tmp_path,
            bundle_loader=lambda _account_id: async_value('credential'),
            downloader=FakeDownloader(),
            clock=lambda: 1_000,
        )

        status = await cache.queue_status()

        assert status.pending_download_count == 2
        assert status.pending_download_archive_count == 1
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_keeps_active_download_order_stable_while_progress_changes(
    tmp_path: Path,
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'blrec.sqlite3'))
    await database.open()
    try:
        missing = tmp_path / 'deleted.mp4'
        await seed_remote_part(database, missing)
        await database.execute(
            'INSERT INTO recording_parts('
            'id,session_id,run_id,part_index,source_path,final_path,'
            'record_start_time,artifact_state,video_deleted_at,file_size_bytes,'
            'created_at,updated_at) '
            "VALUES(2,1,'run:1',2,?,NULL,2,'missing',50,0,2,2)",
            (str(tmp_path / 'deleted-2.mp4'),),
        )
        await database.execute(
            'INSERT INTO vainglory_video_sources('
            'part_id,account_id,bvid,cid,page,origin,state,retention_kind,'
            'progress,downloaded_bytes,original_artifact_state,created_at,'
            'updated_at) VALUES'
            "(1,1,'BV1abcdefgh',123,1,'archive','downloading','analysis',"
            "0.2,20,'missing',1,20),"
            "(2,1,'BV1abcdefgh',124,2,'archive','downloading','analysis',"
            "0.1,10,'missing',2,10)"
        )
        cache = RemoteMediaCache(
            database,
            tmp_path,
            bundle_loader=lambda _account_id: async_value('credential'),
            downloader=FakeDownloader(),
            clock=lambda: 1_000,
        )

        before = await cache.queue_items('downloading')
        await database.execute(
            'UPDATE vainglory_video_sources SET updated_at=30 WHERE part_id=2'
        )
        after = await cache.queue_items('downloading')

        assert [item.part_id for item in before.items] == [1, 2]
        assert [item.part_id for item in after.items] == [1, 2]
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


@pytest.mark.asyncio
async def test_transient_download_failure_retries_on_another_interface(
    tmp_path: Path,
) -> None:
    class RetryDownloader:
        def __init__(self) -> None:
            self.interfaces = []

        async def download_on_interface(
            self,
            bundle: object,
            *,
            bvid: str,
            cid: int,
            page: int,
            target: Path,
            progress: Callable[[int, Optional[int]], Awaitable[None]],
            interface_name: str,
            affinity_key: str,
        ) -> None:
            self.interfaces.append(interface_name)
            if len(self.interfaces) == 1:
                raise BiliDownloadContractError(
                    'yt-dlp 下载失败：source-bound DNS resolution failed'
                )
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b'video123')

    database = BiliUploadDatabase(str(tmp_path / 'blrec.sqlite3'))
    await database.open()
    now = [1_000]
    downloader = RetryDownloader()
    try:
        await seed_remote_part(database, tmp_path / 'deleted.mp4')
        cache = RemoteMediaCache(
            database,
            tmp_path,
            bundle_loader=lambda _account_id: async_value('credential'),
            downloader=downloader,
            download_interfaces=('wan-a', 'wan-b'),
            clock=lambda: now[0],
        )
        assert (await cache.request(1, force_remote=True)).state == 'pending'

        assert await cache.run_once(network_interface='wan-a') is True
        scheduled = await database.fetchone(
            'SELECT state,attempt_count,next_attempt_at,last_attempt_error '
            'FROM vainglory_video_sources WHERE part_id=1'
        )
        assert scheduled is not None
        assert dict(scheduled) == {
            'state': 'pending',
            'attempt_count': 1,
            'next_attempt_at': 1_030,
            'last_attempt_error': 'BiliDownloadContractError: yt-dlp 下载失败：'
            'source-bound DNS resolution failed',
        }
        assert await cache.run_once(network_interface='wan-b') is False

        now[0] = 1_030
        assert await cache.run_once(network_interface='wan-a') is False
        assert await cache.run_once(network_interface='wan-b') is True
        assert (await cache.status(1)).state == 'ready'
        assert downloader.interfaces == ['wan-a', 'wan-b']
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_transient_download_failure_stops_after_three_attempts(
    tmp_path: Path,
) -> None:
    class FailingDownloader:
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
            raise BiliDownloadContractError(
                'yt-dlp 下载失败：bytes read, more expected'
            )

    database = BiliUploadDatabase(str(tmp_path / 'blrec.sqlite3'))
    await database.open()
    now = [1_000]
    try:
        await seed_remote_part(database, tmp_path / 'deleted.mp4')
        cache = RemoteMediaCache(
            database,
            tmp_path,
            bundle_loader=lambda _account_id: async_value('credential'),
            downloader=FailingDownloader(),
            clock=lambda: now[0],
        )
        assert (await cache.request(1, force_remote=True)).state == 'pending'

        assert await cache.run_once() is True
        now[0] = 1_030
        assert await cache.run_once() is True
        now[0] = 1_150
        assert await cache.run_once() is True

        failed = await database.fetchone(
            'SELECT state,attempt_count,next_attempt_at,error '
            'FROM vainglory_video_sources WHERE part_id=1'
        )
        assert failed is not None
        assert str(failed['state']) == 'failed'
        assert int(failed['attempt_count']) == 3
        assert int(failed['next_attempt_at']) == 0
        assert 'bytes read, more expected' in str(failed['error'])
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_permanent_download_contract_error_does_not_retry(tmp_path: Path) -> None:
    class InvalidSourceDownloader:
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
            raise BiliDownloadContractError('B 站稿件分 P 信息无效')

    database = BiliUploadDatabase(str(tmp_path / 'blrec.sqlite3'))
    await database.open()
    try:
        await seed_remote_part(database, tmp_path / 'deleted.mp4')
        cache = RemoteMediaCache(
            database,
            tmp_path,
            bundle_loader=lambda _account_id: async_value('credential'),
            downloader=InvalidSourceDownloader(),
            clock=lambda: 1_000,
        )
        assert (await cache.request(1, force_remote=True)).state == 'pending'

        assert await cache.run_once() is True

        failed = await database.fetchone(
            'SELECT state,attempt_count,next_attempt_at '
            'FROM vainglory_video_sources WHERE part_id=1'
        )
        assert failed is not None
        assert dict(failed) == {
            'state': 'failed',
            'attempt_count': 1,
            'next_attempt_at': 0,
        }
    finally:
        await database.close()


async def async_value(value: object) -> object:
    return value
