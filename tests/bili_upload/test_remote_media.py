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


async def async_value(value: object) -> object:
    return value
