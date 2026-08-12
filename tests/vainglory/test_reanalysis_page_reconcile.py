from pathlib import Path

import pytest

from blrec.bili_upload.database import BiliUploadDatabase
from blrec.bili_upload.remote_media import RemoteMediaCache
from blrec.vainglory.archive_backfill import (
    ArchiveBackfillService,
    ArchiveBackfillUnavailable,
)
from blrec.vainglory.repository import VaingloryConflict, VaingloryRepository
from blrec.vainglory.service import VaingloryIndexService


class UnusedRemoteDownloader:
    async def download(self, *_args: object, **_kwargs: object) -> None:
        raise AssertionError('the test only queues the remote download')


async def async_bundle(_account_id: int) -> object:
    return object()


@pytest.mark.asyncio
async def test_reanalysis_reconciles_remote_pages_before_queuing_scan(
    tmp_path: Path,
) -> None:
    class ThreePageArchiveReader:
        async def viewer_detail(self, _bundle: object, **kwargs: object):
            assert kwargs == {
                'account_id': 1,
                'credential_version': 1,
                'bvid': 'BV1abcdefgh',
            }
            return {
                'data': {
                    'aid': 303,
                    'bvid': 'BV1abcdefgh',
                    'title': '三分 P 历史稿件',
                    'pubtime': 1,
                    'pages': [
                        {
                            'page': page,
                            'cid': 400 + page,
                            'part': 'P{}'.format(page),
                            'duration': 600 if page < 3 else 472,
                        }
                        for page in range(1, 4)
                    ],
                }
            }

    database = BiliUploadDatabase(str(tmp_path / 'blrec.sqlite3'))
    await database.open()
    try:
        first = tmp_path / 'p1.mp4'
        second = tmp_path / 'p2.mp4'
        first.write_bytes(b'video')
        second.write_bytes(b'video')
        await database.execute(
            'INSERT INTO bili_accounts('
            'id,uid,display_name,credential_ciphertext,credential_version,key_id,'
            'state,created_at,updated_at) '
            "VALUES(1,42,'投稿账号',X'00',1,'key','active',1,1)"
        )
        await database.execute(
            'INSERT INTO recording_sessions('
            'id,room_id,broadcast_session_key,state,started_at,title,anchor_name) '
            "VALUES(1,100,'session:1','closed',1,'历史稿件','主播')"
        )
        await database.execute(
            'INSERT INTO recording_runs(id,session_id,state,started_at,ended_at) '
            "VALUES('run:1',1,'finished',1,1201)"
        )
        for part_id, path in ((1, first), (2, second)):
            await database.execute(
                'INSERT INTO recording_parts('
                'id,session_id,run_id,part_index,source_path,record_start_time,'
                'artifact_state,created_at,updated_at) '
                "VALUES(?,1,'run:1',?,?,1,'ready',1,1)",
                (part_id, part_id, str(path)),
            )
        await database.execute(
            'INSERT INTO upload_jobs('
            'id,session_id,account_id,policy_snapshot_json,state,submit_state,'
            'aid,bvid,created_at,updated_at) '
            "VALUES(1,1,1,'{}','approved','confirmed',303,'BV1abcdefgh',1,1)"
        )
        for part_index, path in ((1, first), (2, second)):
            await database.execute(
                'INSERT INTO upload_parts('
                'job_id,part_index,source_path,artifact_state,upload_state,'
                'remote_filename,cid) '
                "VALUES(1,?,?,'ready','confirmed',?,?)",
                (
                    part_index,
                    str(path),
                    'remote-p{}'.format(part_index),
                    400 + part_index,
                ),
            )
        remote_media = RemoteMediaCache(
            database,
            tmp_path / 'recordings',
            bundle_loader=async_bundle,
            downloader=UnusedRemoteDownloader(),
            clock=lambda: 100,
        )
        backfill = ArchiveBackfillService(
            database,
            ThreePageArchiveReader(),
            bundle_loader=async_bundle,
            remote_media_cache=remote_media,
            clock=lambda: 100,
        )
        repository = VaingloryRepository(database, clock=lambda: 100)
        service = VaingloryIndexService(
            repository,
            remote_media_cache=remote_media,
            archive_page_reconciler=backfill.reconcile_session_pages,
        )

        job = await service.request_scan(1)

        assert job.state == 'pending'
        assert (
            await database.scalar(
                'SELECT page_count FROM vainglory_archive_imports WHERE session_id=1'
            )
            == 3
        )
        assert (
            await database.scalar(
                'SELECT COUNT(*) FROM recording_parts WHERE session_id=1'
            )
            == 3
        )
        assert (
            await database.scalar(
                'SELECT COUNT(*) FROM vainglory_part_jobs '
                "WHERE session_id=1 AND state='pending'"
            )
            == 3
        )
        missing = await database.fetchone(
            'SELECT source.page,source.cid,source.state '
            'FROM vainglory_video_sources source '
            'JOIN recording_parts part ON part.id=source.part_id '
            'WHERE part.session_id=1 AND part.part_index=3'
        )
        assert missing is not None
        assert (int(missing['page']), int(missing['cid']), str(missing['state'])) == (
            3,
            403,
            'pending',
        )
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_reanalysis_stops_when_remote_pages_cannot_be_verified() -> None:
    class NeverRequestedRepository:
        async def request_scan(self, _session_id: int):
            raise AssertionError('scan must not be queued with unverified pages')

    async def unavailable(_session_id: int) -> int:
        raise ArchiveBackfillUnavailable('B 站暂时不可用')

    service = VaingloryIndexService(
        NeverRequestedRepository(),  # type: ignore[arg-type]
        archive_page_reconciler=unavailable,
    )

    with pytest.raises(VaingloryConflict, match='重新分析前核对 B 站分 P 失败'):
        await service.request_scan(1)
