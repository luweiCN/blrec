from pathlib import Path
from typing import Any, Mapping, Tuple

import pytest

from blrec.bili_upload.database import BiliUploadDatabase
from blrec.bili_upload.errors import BiliApiError
from blrec.bili_upload.remote_media import RemoteMediaStatus
from blrec.vainglory.archive_backfill import (
    ArchiveBackfillService,
    ArchiveBackfillUnavailable,
)


class FakeArchiveReader:
    async def list_page(
        self,
        _bundle: object,
        *,
        account_id: int,
        credential_version: int,
        status: str,
        page_number: int,
        page_size: int,
    ) -> Tuple[Mapping[str, Any], ...]:
        assert account_id == 1
        assert credential_version == 1
        assert status == 'is_pubing,pubed,not_pubed'
        assert page_size == 50
        if page_number > 1:
            return ()
        return (
            {
                'Archive': {
                    'aid': 101,
                    'bvid': 'BV1abcdefgh',
                    'title': '早期虚荣录播',
                    'pubtime': 900,
                }
            },
        )

    async def detail(
        self, _bundle: object, *, account_id: int, credential_version: int, bvid: str
    ) -> Mapping[str, Any]:
        assert (account_id, credential_version, bvid) == (1, 1, 'BV1abcdefgh')
        return {
            'data': {
                'archive': {
                    'aid': 101,
                    'bvid': bvid,
                    'title': '早期虚荣录播',
                    'desc': '',
                    'pubtime': 900,
                },
                'videos': [
                    {'cid': 201, 'title': 'P1', 'duration': 600},
                    {'cid': 202, 'title': 'P2', 'duration': 720},
                ],
            }
        }

    async def viewer_detail(
        self, bundle: object, *, account_id: int, credential_version: int, bvid: str
    ) -> Mapping[str, Any]:
        return await self.detail(
            bundle,
            account_id=account_id,
            credential_version=credential_version,
            bvid=bvid,
        )


class FakeRemoteMediaCache:
    def __init__(self) -> None:
        self.requests = []
        self.force_remote_requests = []

    async def request(
        self, part_id: int, *, force_remote: bool = False
    ) -> RemoteMediaStatus:
        self.requests.append(part_id)
        self.force_remote_requests.append(force_remote)
        return RemoteMediaStatus(
            part_id=part_id, state='pending', progress=0, remote_available=True
        )


def test_missing_remote_source_requeues_pending_analysis() -> None:
    state = ArchiveBackfillService._derived_part_state(
        {
            'state': 'analyzing',
            'progress': 0.5,
            'error': None,
            'source_state': 'missing',
            'source_progress': 0,
            'source_error': None,
            'analysis_state': 'pending',
            'analysis_progress': 0,
            'analysis_error': None,
        }  # type: ignore[arg-type]
    )

    assert state == ('queued', 0, None)


@pytest.mark.parametrize(
    ('source_state', 'expected_stage'),
    (('pending', 'download_pending'), ('downloading', 'downloading')),
)
def test_archive_item_distinguishes_waiting_from_active_download(
    source_state: str, expected_stage: str
) -> None:
    stage = ArchiveBackfillService._item_stage(
        {
            'state': 'analyzing',
            'publication_state': None,
            'page_count': 1,
            'source_state': source_state,
            'analysis_state': None,
            'current_part_state': 'downloading',
        }  # type: ignore[arg-type]
    )

    assert stage == expected_stage


async def seed_account(database: BiliUploadDatabase) -> None:
    await database.execute(
        "INSERT INTO bili_accounts("
        "id,uid,display_name,credential_ciphertext,credential_version,key_id,"
        "state,created_at,updated_at) "
        "VALUES(1,42,'旧账号',X'00',1,'key','active',1,1)"
    )


@pytest.mark.asyncio
async def test_completed_archive_requires_publication_remote_verification(
    tmp_path: Path,
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'blrec.sqlite3'))
    await database.open()
    try:
        await seed_account(database)
        await database.execute(
            'INSERT INTO recording_sessions('
            'id,room_id,broadcast_session_key,state,started_at,title) '
            "VALUES(1,100,'archive:1','closed',1,'历史直播')"
        )
        await database.execute(
            'INSERT INTO vainglory_archive_syncs('
            'account_id,state,progress,discovered_count,completed_count,error,'
            'requested_at,completed_at,updated_at) '
            "VALUES(1,'ready',1,1,1,NULL,1,1,1)"
        )
        await database.execute(
            'INSERT INTO vainglory_archive_imports('
            'id,account_id,aid,bvid,title,session_id,state,progress,page_count,'
            'completed_page_count,created_at,updated_at) '
            "VALUES(1,1,101,'BV1abcdefgh','历史直播',1,'ready',1,1,1,1,1)"
        )
        await database.execute(
            "INSERT INTO recording_runs(id,session_id,state,started_at,ended_at) "
            "VALUES('archive-run',1,'finished',1,2)"
        )
        await database.execute(
            'INSERT INTO recording_parts('
            'id,session_id,run_id,part_index,source_path,record_start_time,'
            'artifact_state,created_at,updated_at) '
            "VALUES(1,1,'archive-run',1,?,1,'ready',1,1)",
            (str(tmp_path / 'archive.mp4'),),
        )
        await database.execute(
            'INSERT INTO vainglory_archive_parts('
            'id,import_id,page,cid,title,duration_seconds,recording_part_id,'
            'state,progress,created_at,updated_at) '
            "VALUES(1,1,1,201,'P1',600,1,'ready',1,1,1)"
        )
        await database.execute(
            'INSERT INTO vainglory_scan_jobs('
            'session_id,state,progress,algorithm_version,match_count,error,'
            'requested_at,started_at,completed_at,updated_at) '
            "VALUES(1,'ready',1,1,1,NULL,1,1,1,1)"
        )
        await database.execute(
            'INSERT INTO vainglory_matches('
            'id,session_id,result_part_id,started_at_ms,result_at_ms,game_mode,'
            'team_size,result_text,end_reason,left_color,right_color,winner_side,'
            'left_kills,right_kills,left_economy,right_economy,confidence,'
            'created_at) '
            "VALUES(1,1,1,0,1,'3v3',3,'Victory','normal','teal','orange',"
            "'left',1,0,1000,900,1,1)"
        )
        await database.execute(
            'INSERT INTO vainglory_publications('
            'id,account_id,session_id,aid,bvid,source_kind,payload_hash,'
            'description_block,state,description_state,pin_state,created_at,'
            'updated_at,needs_refresh,chapter_state,comment_cleanup_state,'
            'plan_state,match_count,public_visible_at) '
            "VALUES(1,1,1,101,'BV1abcdefgh','archive',?,'已回填','prepared',"
            "'confirmed','confirmed',1,1,0,'confirmed','confirmed','ready',1,1)",
            ('a' * 64,),
        )
        service = ArchiveBackfillService(
            database,
            FakeArchiveReader(),
            bundle_loader=lambda _account_id: async_value(object()),
            remote_media_cache=FakeRemoteMediaCache(),
            clock=lambda: 1_000,
        )

        pending = (await service.list_items(1))[0]
        assert pending.stage == 'publication_pending'
        assert pending.publication_progress == 0.99

        await database.execute(
            "UPDATE vainglory_publications SET state='confirmed',"
            'remote_verified_at=1000 WHERE id=1'
        )
        completed = (await service.list_items(1))[0]
        assert completed.stage == 'completed'
        assert completed.publication_progress == 1
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_manual_reanalysis_requeues_a_skipped_unmaterialized_import(
    tmp_path: Path,
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'blrec.sqlite3'))
    await database.open()
    try:
        await seed_account(database)
        await database.execute(
            'INSERT INTO vainglory_archive_syncs('
            'account_id,state,progress,discovered_count,completed_count,error,'
            'requested_at,started_at,completed_at,updated_at,discovery_complete,'
            "operator_paused) VALUES(1,'ready',1,1,1,NULL,1,1,1,1,1,1)"
        )
        await database.execute(
            'INSERT INTO vainglory_archive_imports('
            'id,account_id,aid,bvid,title,state,progress,page_count,'
            'completed_page_count,error,content_classification,'
            'classification_reason,retryable,next_retry_at,created_at,updated_at) '
            "VALUES(7,1,101,'BV1abcdefgh','短分 P','skipped',1,0,0,NULL,"
            "'unknown','稿件短于10分钟，未进行内容分析',0,NULL,1,1)"
        )
        service = ArchiveBackfillService(
            database,
            FakeArchiveReader(),
            bundle_loader=lambda _account_id: async_value(object()),
            remote_media_cache=FakeRemoteMediaCache(),
            clock=lambda: 1_000,
        )

        assert await service.request_import_reanalysis(7) is None
        imported = await database.fetchone(
            'SELECT state,progress,page_count,completed_page_count,error,'
            'content_classification,classification_reason,updated_at '
            'FROM vainglory_archive_imports WHERE id=7'
        )

        assert imported is not None
        assert dict(imported) == {
            'state': 'queued',
            'progress': 0.0,
            'page_count': 0,
            'completed_page_count': 0,
            'error': None,
            'content_classification': 'unknown',
            'classification_reason': '手动重新分析，等待核对 B 站分 P',
            'updated_at': 1_000,
        }
        sync = await database.fetchone(
            'SELECT state,progress,discovered_count,completed_count,'
            'operator_paused,completed_at,updated_at '
            'FROM vainglory_archive_syncs WHERE account_id=1'
        )
        assert sync is not None
        assert dict(sync) == {
            'state': 'running',
            'progress': 0.0,
            'discovered_count': 1,
            'completed_count': 0,
            'operator_paused': 1,
            'completed_at': None,
            'updated_at': 1_000,
        }
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_manual_archive_reanalysis_returns_an_existing_session(
    tmp_path: Path,
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'blrec.sqlite3'))
    await database.open()
    try:
        await seed_account(database)
        await database.execute(
            'INSERT INTO recording_sessions('
            'id,room_id,broadcast_session_key,state,started_at,title) '
            "VALUES(99,100,'archive:99','closed',900,'历史直播')"
        )
        await database.execute(
            'INSERT INTO vainglory_archive_imports('
            'id,account_id,aid,bvid,title,session_id,state,progress,page_count,'
            'completed_page_count,created_at,updated_at) '
            "VALUES(7,1,101,'BV1abcdefgh','历史直播',99,'ready',1,2,2,1,1)"
        )
        service = ArchiveBackfillService(
            database,
            FakeArchiveReader(),
            bundle_loader=lambda _account_id: async_value(object()),
            remote_media_cache=FakeRemoteMediaCache(),
            clock=lambda: 1_000,
        )

        assert await service.request_import_reanalysis(7) == 99
        imported = await database.fetchone(
            'SELECT state,page_count,completed_page_count,updated_at '
            'FROM vainglory_archive_imports WHERE id=7'
        )

        assert imported is not None
        assert dict(imported) == {
            'state': 'ready',
            'page_count': 2,
            'completed_page_count': 2,
            'updated_at': 1,
        }
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_archive_status_counts_only_analysis_completed_today(
    tmp_path: Path,
) -> None:
    now = 1_000_000
    database = BiliUploadDatabase(str(tmp_path / 'blrec.sqlite3'))
    await database.open()
    try:
        await seed_account(database)
        await database.execute(
            'INSERT INTO vainglory_archive_syncs('
            'account_id,state,progress,discovered_count,completed_count,error,'
            'requested_at,updated_at) '
            "VALUES(1,'running',0,2,0,NULL,1,1)"
        )
        for session_id, completed_at in ((1, now - 100), (2, now - 86_400)):
            await database.execute(
                'INSERT INTO recording_sessions('
                'id,room_id,broadcast_session_key,state,started_at,title) '
                "VALUES(?,100,?,'closed',1,'历史直播')",
                (session_id, 'archive:{}'.format(session_id)),
            )
            await database.execute(
                'INSERT INTO vainglory_archive_imports('
                'id,account_id,aid,bvid,title,session_id,state,progress,'
                'page_count,completed_page_count,created_at,updated_at) '
                "VALUES(?,?,?,?,'历史直播',?,'ready',1,1,1,1,1)",
                (
                    session_id,
                    1,
                    100 + session_id,
                    'BV1abcdefg{}'.format(session_id),
                    session_id,
                ),
            )
            await database.execute(
                'INSERT INTO vainglory_scan_jobs('
                'session_id,state,progress,algorithm_version,match_count,error,'
                'requested_at,started_at,completed_at,updated_at) '
                "VALUES(?,'ready',1,1,0,NULL,1,1,?,?)",
                (session_id, completed_at, completed_at),
            )
        service = ArchiveBackfillService(
            database,
            FakeArchiveReader(),
            bundle_loader=lambda _account_id: async_value(object()),
            remote_media_cache=FakeRemoteMediaCache(),
            clock=lambda: now,
        )

        status = await service.status(1)

        assert status.today_analyzed_count == 1
        assert await service.count_items(1) == 2
        page = await service.list_items(1, limit=1, offset=1)
        assert [item.id for item in page] == [1]
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_manual_archive_reanalysis_does_not_interrupt_active_import(
    tmp_path: Path,
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'blrec.sqlite3'))
    await database.open()
    try:
        await seed_account(database)
        await database.execute(
            'INSERT INTO vainglory_archive_imports('
            'id,account_id,aid,bvid,title,state,progress,page_count,'
            'completed_page_count,created_at,updated_at) '
            "VALUES(7,1,101,'BV1abcdefgh','处理中','analyzing',0.5,2,1,1,1)"
        )
        service = ArchiveBackfillService(
            database,
            FakeArchiveReader(),
            bundle_loader=lambda _account_id: async_value(object()),
            remote_media_cache=FakeRemoteMediaCache(),
            clock=lambda: 1_000,
        )

        with pytest.raises(ArchiveBackfillUnavailable, match='当前正在处理中'):
            await service.request_import_reanalysis(7)
        assert (
            await database.scalar(
                'SELECT state FROM vainglory_archive_imports WHERE id=7'
            )
            == 'analyzing'
        )
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_discovers_materializes_and_queues_each_archive_page(
    tmp_path: Path,
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'blrec.sqlite3'))
    await database.open()
    cache = FakeRemoteMediaCache()
    try:
        await seed_account(database)
        service = ArchiveBackfillService(
            database,
            FakeArchiveReader(),
            bundle_loader=lambda _account_id: async_value(object()),
            remote_media_cache=cache,
            clock=lambda: 1_000,
        )

        requested = await service.request(1)
        assert requested.state == 'discovering'
        assert await service.run_once() is True
        assert await service.run_once() is True
        assert await service.run_once() is True
        assert await service.run_once() is True

        sync = await service.status(1)
        assert sync.state == 'running'
        assert sync.discovered_count == 1
        imports = await database.fetchall(
            'SELECT state,page_count,session_id FROM vainglory_archive_imports'
        )
        assert len(imports) == 1
        assert str(imports[0]['state']) == 'analyzing'
        assert int(imports[0]['page_count']) == 2
        assert imports[0]['session_id'] is not None
        parts = await database.fetchall(
            'SELECT archive.page,archive.cid,archive.recording_part_id,'
            'source.origin,source.retention_kind,source.bvid '
            'FROM vainglory_archive_parts archive '
            'JOIN vainglory_video_sources source '
            'ON source.part_id=archive.recording_part_id '
            'ORDER BY archive.page'
        )
        assert [
            (
                int(row['page']),
                int(row['cid']),
                str(row['origin']),
                str(row['retention_kind']),
                str(row['bvid']),
            )
            for row in parts
        ] == [
            (1, 201, 'archive', 'analysis', 'BV1abcdefgh'),
            (2, 202, 'archive', 'analysis', 'BV1abcdefgh'),
        ]
        assert cache.requests == [
            int(parts[0]['recording_part_id']),
            int(parts[1]['recording_part_id']),
        ]
        waiting = (await service.list_items(1))[0]
        assert waiting.stage == 'download_pending'

        await database.execute(
            "UPDATE vainglory_video_sources SET state='downloading' WHERE part_id=?",
            (int(parts[1]['recording_part_id']),),
        )
        downloading = (await service.list_items(1))[0]
        assert downloading.stage == 'downloading'
        assert downloading.current_page == 2
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_reconcile_refreshes_only_changed_archive_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'blrec.sqlite3'))
    await database.open()
    cache = FakeRemoteMediaCache()
    refreshed = []
    try:
        await seed_account(database)
        service = ArchiveBackfillService(
            database,
            FakeArchiveReader(),
            bundle_loader=lambda _account_id: async_value(object()),
            remote_media_cache=cache,
            clock=lambda: 1_000,
        )
        await service.request(1)
        for _ in range(4):
            assert await service.run_once() is True
        session_id = int(
            await database.scalar('SELECT session_id FROM vainglory_archive_imports')
        )
        monkeypatch.setattr(
            'blrec.vainglory.archive_backfill.refresh_session_scan_job',
            lambda _connection, selected_session_id, _now: refreshed.append(
                selected_session_id
            ),
        )

        assert await service._reconcile() is False
        assert refreshed == []

        await database.execute(
            "UPDATE vainglory_video_sources SET state='downloading',progress=0.2 "
            'WHERE part_id=(SELECT recording_part_id '
            'FROM vainglory_archive_parts ORDER BY page LIMIT 1)'
        )
        assert await service._reconcile() is True
        assert refreshed == [session_id]
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_reads_public_viewer_pages_without_creator_metadata(
    tmp_path: Path,
) -> None:
    class ViewerReader(FakeArchiveReader):
        async def detail(self, *_args: Any, **_kwargs: Any) -> Mapping[str, Any]:
            raise AssertionError('creator detail must not be the primary source')

        async def viewer_detail(
            self,
            _bundle: object,
            *,
            account_id: int,
            credential_version: int,
            bvid: str,
        ) -> Mapping[str, Any]:
            assert (account_id, credential_version, bvid) == (1, 1, 'BV1abcdefgh')
            return {
                'data': {
                    'aid': 101,
                    'bvid': bvid,
                    'title': '公开虚荣录播',
                    'desc': '普通观看接口',
                    'pages': [
                        {'page': 1, 'cid': 301, 'part': '第一部分', 'duration': 700}
                    ],
                }
            }

    database = BiliUploadDatabase(str(tmp_path / 'blrec.sqlite3'))
    await database.open()
    try:
        await seed_account(database)
        service = ArchiveBackfillService(
            database,
            ViewerReader(),
            bundle_loader=lambda _account_id: async_value(object()),
            remote_media_cache=FakeRemoteMediaCache(),
            clock=lambda: 1_000,
        )
        await service.request(1)
        assert await service.run_once() is True
        assert await service.run_once() is True

        part = await database.fetchone(
            'SELECT cid,title,duration_seconds FROM vainglory_archive_parts'
        )
        assert part is not None
        assert (
            int(part['cid']),
            str(part['title']),
            int(part['duration_seconds']),
        ) == (301, '第一部分', 700)
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_retryable_metadata_failure_is_not_counted_as_complete(
    tmp_path: Path,
) -> None:
    class RetryReader(FakeArchiveReader):
        def __init__(self) -> None:
            self.failures = 1

        async def viewer_detail(
            self, bundle: object, *, account_id: int, credential_version: int, bvid: str
        ) -> Mapping[str, Any]:
            if self.failures:
                self.failures -= 1
                raise BiliApiError(-702, operation='archive_view')
            return await super().detail(
                bundle,
                account_id=account_id,
                credential_version=credential_version,
                bvid=bvid,
            )

    now = [1_000]
    database = BiliUploadDatabase(str(tmp_path / 'blrec.sqlite3'))
    await database.open()
    try:
        await seed_account(database)
        service = ArchiveBackfillService(
            database,
            RetryReader(),
            bundle_loader=lambda _account_id: async_value(object()),
            remote_media_cache=FakeRemoteMediaCache(),
            clock=lambda: now[0],
        )
        await service.request(1)
        assert await service.run_once() is True
        assert await service.run_once() is True

        imported = await database.fetchone(
            'SELECT state,retryable,next_retry_at FROM vainglory_archive_imports'
        )
        assert imported is not None
        assert str(imported['state']) == 'failed'
        assert int(imported['retryable']) == 1
        assert int(imported['next_retry_at']) > now[0]
        assert (await service.status(1)).completed_count == 0
        assert (await service.status(1)).daily_used == 0

        assert await service.run_once() is False
        now[0] = int(imported['next_retry_at'])
        assert await service.run_once() is True
        assert await service.run_once() is True
        assert (
            await database.scalar('SELECT state FROM vainglory_archive_imports')
            == 'analyzing'
        )
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_retryable_failed_download_is_requested_again(tmp_path: Path) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'blrec.sqlite3'))
    await database.open()
    cache = FakeRemoteMediaCache()
    try:
        await seed_account(database)
        service = ArchiveBackfillService(
            database,
            FakeArchiveReader(),
            bundle_loader=lambda _account_id: async_value(object()),
            remote_media_cache=cache,
            clock=lambda: 1_000,
        )
        await service.request(1)
        for _index in range(4):
            assert await service.run_once() is True
        part_id = int(
            await database.scalar(
                'SELECT recording_part_id FROM vainglory_archive_parts '
                'ORDER BY page LIMIT 1'
            )
        )
        await database.execute(
            "UPDATE vainglory_video_sources SET state='failed',"
            "error='下载中断' WHERE part_id=?",
            (part_id,),
        )
        await database.execute(
            "UPDATE vainglory_archive_parts SET state='failed',progress=1,"
            "error='下载中断' WHERE recording_part_id=?",
            (part_id,),
        )
        await database.execute(
            "UPDATE vainglory_archive_imports SET state='failed',retryable=1,"
            "next_retry_at=1000,error='下载中断'"
        )
        request_count = len(cache.requests)

        assert await service.run_once() is True
        assert await service.run_once() is True

        assert len(cache.requests) == request_count + 1
        assert cache.requests[-1] == part_id
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_existing_uploaded_archive_reuses_its_recording_parts(
    tmp_path: Path,
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'blrec.sqlite3'))
    await database.open()
    cache = FakeRemoteMediaCache()
    try:
        await seed_account(database)
        await database.execute(
            'INSERT INTO recording_sessions('
            'id,room_id,broadcast_session_key,state,started_at,title) '
            "VALUES(99,100,'existing:99','closed',900,'早期虚荣录播')"
        )
        await database.execute(
            "INSERT INTO recording_runs(id,session_id,state,started_at,ended_at) "
            "VALUES('existing-run',99,'finished',900,2300)"
        )
        for page, cid in ((1, 201), (2, 202)):
            await database.execute(
                'INSERT INTO recording_parts('
                'id,session_id,run_id,part_index,source_path,record_start_time,'
                'artifact_state,created_at,updated_at) '
                "VALUES(?,99,'existing-run',?,?,900,'ready',900,900)",
                (100 + page, page, '/recording/p{}.mp4'.format(page)),
            )
        await database.execute(
            'INSERT INTO upload_jobs('
            'id,session_id,account_id,policy_snapshot_json,state,submit_state,'
            'aid,bvid,created_at,updated_at) '
            "VALUES(50,99,1,'{}','approved','confirmed',101,"
            "'BV1abcdefgh',900,900)"
        )
        for page, cid in ((1, 201), (2, 202)):
            await database.execute(
                'INSERT INTO upload_parts('
                'job_id,part_index,source_path,artifact_state,upload_state,'
                'remote_filename,cid) '
                "VALUES(50,?,?,'ready','confirmed',?,?)",
                (
                    page,
                    '/recording/p{}.mp4'.format(page),
                    'remote-p{}'.format(page),
                    cid,
                ),
            )
            await database.execute(
                'INSERT INTO vainglory_part_jobs('
                'part_id,session_id,state,request_kind,progress,algorithm_version,'
                'match_count,error,requested_at,started_at,completed_at,updated_at) '
                "VALUES(?,99,'ready','automatic',1,13,0,NULL,900,900,900,900)",
                (100 + page,),
            )

        service = ArchiveBackfillService(
            database,
            FakeArchiveReader(),
            bundle_loader=lambda _account_id: async_value(object()),
            remote_media_cache=cache,
            clock=lambda: 1_000,
        )
        await service.request(1)
        assert await service.run_once() is True
        assert (
            await database.scalar('SELECT state FROM vainglory_archive_imports')
            == 'queued'
        )
        assert await service.run_once() is True
        assert await service.run_once() is True

        assert await database.scalar('SELECT COUNT(*) FROM recording_parts') == 2
        links = await database.fetchall(
            'SELECT page,recording_part_id,state '
            'FROM vainglory_archive_parts ORDER BY page'
        )
        assert [
            (int(row['page']), int(row['recording_part_id']), str(row['state']))
            for row in links
        ] == [(1, 101, 'ready'), (2, 102, 'ready')]
        assert (
            await database.scalar('SELECT state FROM vainglory_archive_imports')
            == 'ready'
        )
        assert cache.requests == []
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_migration_target_is_not_added_to_history_intake(tmp_path: Path) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'blrec.sqlite3'))
    await database.open()
    try:
        await seed_account(database)
        await database.execute(
            "INSERT INTO bili_accounts("
            "id,uid,display_name,credential_ciphertext,credential_version,key_id,"
            "state,created_at,updated_at) "
            "VALUES(2,84,'下载账号',X'00',1,'key','active',1,1)"
        )
        await database.execute(
            'INSERT INTO recording_sessions('
            'id,room_id,broadcast_session_key,state,started_at,title) '
            "VALUES(99,100,'bili-migration:42:1:BV1source001','closed',900,"
            "'早期虚荣录播')"
        )
        await database.execute(
            'INSERT INTO upload_jobs('
            'id,session_id,account_id,policy_snapshot_json,state,submit_state,'
            'aid,bvid,created_at,updated_at) '
            "VALUES(50,99,1,'{}','approved','confirmed',101,"
            "'BV1abcdefgh',900,900)"
        )
        await database.execute(
            'INSERT INTO archive_migration_jobs('
            'id,source_uid,download_account_id,target_account_id,state,progress,'
            'discovered_count,completed_count,failed_count,error,requested_at,'
            'started_at,completed_at,updated_at) '
            "VALUES(1,42,2,1,'completed',1,1,1,0,NULL,900,900,900,900)"
        )
        await database.execute(
            'INSERT INTO archive_migration_items('
            'id,migration_id,aid,bvid,title,published_at,state,progress,page_count,'
            'downloaded_page_count,session_id,upload_job_id,error,created_at,'
            'updated_at) '
            "VALUES(1,1,201,'BV1source001','源稿件',800,'task_created',1,1,1,"
            '99,50,NULL,900,900)'
        )
        service = ArchiveBackfillService(
            database,
            FakeArchiveReader(),
            bundle_loader=lambda _account_id: async_value(object()),
            remote_media_cache=FakeRemoteMediaCache(),
            clock=lambda: 1_000,
        )

        await service.request(1)
        assert await service.run_once() is True

        assert (
            await database.scalar('SELECT COUNT(*) FROM vainglory_archive_imports') == 0
        )
        status = await service.status(1)
        assert status.discovered_count == 0
        assert status.completed_count == 0
        assert status.daily_used == 0

        await database.execute(
            'INSERT INTO vainglory_archive_imports('
            'account_id,aid,bvid,title,published_at,session_id,state,progress,'
            'page_count,completed_page_count,created_at,updated_at) '
            "VALUES(1,101,'BV1abcdefgh','目标稿件',900,99,'queued',0,0,0,900,900)"
        )
        assert await service.recover_interrupted() == 1
        assert (
            await database.scalar(
                "SELECT COUNT(*) FROM vainglory_archive_imports "
                "WHERE state='skipped' AND retryable=0"
            )
            == 1
        )
        assert await service.list_items(1) == ()
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_marks_finished_archive_and_sync_without_duplicating_on_rescan(
    tmp_path: Path,
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'blrec.sqlite3'))
    await database.open()
    cache = FakeRemoteMediaCache()
    try:
        await seed_account(database)
        service = ArchiveBackfillService(
            database,
            FakeArchiveReader(),
            bundle_loader=lambda _account_id: async_value(object()),
            remote_media_cache=cache,
            clock=lambda: 1_000,
        )
        await service.request(1)
        for _index in range(4):
            assert await service.run_once() is True

        rows = await database.fetchall(
            'SELECT recording_part_id FROM vainglory_archive_parts ORDER BY page'
        )
        for row in rows:
            part_id = int(row['recording_part_id'])
            await database.execute(
                'INSERT INTO vainglory_part_jobs('
                'part_id,session_id,state,request_kind,progress,algorithm_version,'
                'match_count,error,requested_at,started_at,completed_at,updated_at) '
                'SELECT id,session_id,\'ready\',\'archive\',1,2,1,NULL,'
                '1000,1000,1000,1000 FROM recording_parts WHERE id=?',
                (part_id,),
            )
        await database.execute(
            "UPDATE vainglory_archive_parts SET state='queued',progress=0"
        )

        assert await service.run_once() is True
        sync = await service.status(1)
        assert sync.state == 'ready'
        assert sync.completed_count == 1
        imported = await database.fetchone(
            'SELECT content_classification,classification_reason '
            'FROM vainglory_archive_imports'
        )
        assert imported is not None
        assert str(imported['content_classification']) == ('suspected_non_vainglory')
        reviews = await service.list_suspected_non_vainglory()
        assert reviews.total == 1
        assert reviews.items[0].bvid == 'BV1abcdefgh'

        await service.request(1)
        assert await service.run_once() is True
        assert (
            await database.scalar('SELECT COUNT(*) FROM vainglory_archive_imports') == 1
        )
        assert await database.scalar('SELECT COUNT(*) FROM recording_sessions') == 1
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_does_not_treat_the_archive_owner_as_the_recorded_anchor(
    tmp_path: Path,
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'blrec.sqlite3'))
    await database.open()
    try:
        await seed_account(database)
        await database.execute(
            'INSERT INTO recording_sessions('
            'id,room_id,broadcast_session_key,state,started_at,title,'
            'anchor_uid,anchor_name) '
            "VALUES(99,777,'known:owner','closed',500,'已知直播',42,'旧账号')"
        )

        inferred = await database.write(
            lambda connection: ArchiveBackfillService._infer_anchor(
                connection,
                '旧账号的直播回放',
                '',
                excluded_anchor_uid=42,
                excluded_anchor_name='旧账号',
            )
        )

        assert inferred == (0, None, '')
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_infers_a_known_player_name_from_the_archive_description(
    tmp_path: Path,
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'blrec.sqlite3'))
    await database.open()
    try:
        await seed_account(database)
        await database.execute(
            "INSERT INTO vainglory_players(id,name,origin,created_at,updated_at) "
            "VALUES(1,'茉莉','manual',1,1)"
        )
        await database.execute(
            'INSERT INTO vainglory_player_aliases('
            'alias,player_id,created_at,updated_at) VALUES(\'茉莉\',1,1,1)'
        )

        inferred = await database.write(
            lambda connection: ArchiveBackfillService._infer_anchor(
                connection,
                '早期虚荣录播',
                '本期主播：茉莉',
                excluded_anchor_uid=42,
                excluded_anchor_name='旧账号',
            )
        )

        assert inferred == (0, None, '茉莉')
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_reconciles_existing_historical_identity_from_private_archive_detail(
    tmp_path: Path,
) -> None:
    class Reader(FakeArchiveReader):
        async def viewer_detail(
            self,
            _bundle: object,
            *,
            account_id: int,
            credential_version: int,
            bvid: str,
        ) -> Mapping[str, Any]:
            assert (account_id, credential_version, bvid) == (1, 1, 'BV1abcdefgh')
            return {
                'data': {
                    'archive': {
                        'aid': 101,
                        'bvid': bvid,
                        'title': '茉莉的直播回放',
                        'desc': '原直播间：https://live.bilibili.com/930376',
                        'is_only_self': 1,
                    },
                    'videos': [{'cid': 201, 'title': 'P1', 'duration': 600}],
                }
            }

    database = BiliUploadDatabase(str(tmp_path / 'blrec.sqlite3'))
    await database.open()
    try:
        await seed_account(database)
        await database.execute(
            "INSERT INTO vainglory_players(id,name,origin,created_at,updated_at) "
            "VALUES(1,'茉莉','manual',1,1)"
        )
        await database.execute(
            'INSERT INTO vainglory_player_rooms('
            'room_id,player_id,created_at,updated_at) VALUES(930376,1,1,1)'
        )
        await database.execute(
            'INSERT INTO recording_sessions('
            'id,room_id,broadcast_session_key,state,started_at,title,'
            'anchor_name) '
            "VALUES(1,0,'bili-archive:1:BV1abcdefgh','closed',1,"
            "'未归属旧稿件','-Akitsuki-')"
        )
        await database.execute(
            'INSERT INTO vainglory_archive_imports('
            'id,account_id,aid,bvid,title,session_id,state,progress,page_count,'
            'completed_page_count,created_at,updated_at) '
            "VALUES(1,1,101,'BV1abcdefgh','未归属旧稿件',1,'ready',1,1,1,1,1)"
        )
        service = ArchiveBackfillService(
            database,
            Reader(),
            bundle_loader=lambda _account_id: async_value(object()),
            remote_media_cache=FakeRemoteMediaCache(),
            clock=lambda: 1_000,
        )

        assert await service.reconcile_archive_identity_once() is True

        session = await database.fetchone(
            'SELECT room_id,anchor_name FROM recording_sessions WHERE id=1'
        )
        imported = await database.fetchone(
            'SELECT is_only_self,anchor_identity_checked_at,'
            'anchor_identity_error FROM vainglory_archive_imports WHERE id=1'
        )
        assert session is not None
        assert (int(session['room_id']), str(session['anchor_name'])) == (
            930376,
            '茉莉',
        )
        assert imported is not None
        assert int(imported['is_only_self']) == 1
        assert int(imported['anchor_identity_checked_at']) == 1_000
        assert imported['anchor_identity_error'] is None
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_identity_reconciliation_does_not_starve_archive_intake(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = [1_000]
    database = BiliUploadDatabase(str(tmp_path / 'blrec.sqlite3'))
    await database.open()
    try:
        service = ArchiveBackfillService(
            database,
            FakeArchiveReader(),
            bundle_loader=lambda _account_id: async_value(object()),
            remote_media_cache=FakeRemoteMediaCache(),
            clock=lambda: now[0],
        )
        service._identity_reconciliation_enabled = True
        calls = []

        async def reconcile() -> bool:
            return False

        async def reconcile_identity() -> bool:
            calls.append('identity')
            now[0] += 10
            return True

        async def claim_download_part() -> None:
            calls.append('download')
            return None

        async def claim_import() -> Mapping[str, int]:
            calls.append('import')
            return {'id': 1, 'page_count': 0}

        async def materialize(_imported: Mapping[str, int]) -> None:
            calls.append('materialize')

        monkeypatch.setattr(service, '_reconcile', reconcile)
        monkeypatch.setattr(
            service, 'reconcile_archive_identity_once', reconcile_identity
        )
        monkeypatch.setattr(service, '_claim_download_part', claim_download_part)
        monkeypatch.setattr(service, '_claim_import', claim_import)
        monkeypatch.setattr(service, '_materialize', materialize)

        assert await service.run_once() is True
        assert calls == ['identity', 'download', 'import', 'materialize']
        assert service._next_identity_reconcile_at == 1_015
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_keeps_short_archive_pages_for_result_scanning(tmp_path: Path) -> None:
    class Reader(FakeArchiveReader):
        async def detail(
            self,
            _bundle: object,
            *,
            account_id: int,
            credential_version: int,
            bvid: str,
        ) -> Mapping[str, Any]:
            detail = await super().detail(
                _bundle,
                account_id=account_id,
                credential_version=credential_version,
                bvid=bvid,
            )
            return {
                **detail,
                'data': {
                    **detail['data'],
                    'videos': [
                        {'cid': 201, 'title': '短片', 'duration': 599},
                        {'cid': 202, 'title': '完整录像', 'duration': 600},
                    ],
                },
            }

    database = BiliUploadDatabase(str(tmp_path / 'blrec.sqlite3'))
    await database.open()
    cache = FakeRemoteMediaCache()
    try:
        await seed_account(database)
        service = ArchiveBackfillService(
            database,
            Reader(),
            bundle_loader=lambda _account_id: async_value(object()),
            remote_media_cache=cache,
            clock=lambda: 1_000,
        )
        await service.request(1)
        assert await service.run_once() is True
        assert await service.run_once() is True

        pages = await database.fetchall(
            'SELECT page,cid,duration_seconds FROM vainglory_archive_parts'
        )

        assert [
            (int(row['page']), int(row['cid']), int(row['duration_seconds']))
            for row in pages
        ] == [(1, 201, 599), (2, 202, 600)]
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_discovers_and_materializes_pages_missing_from_an_existing_import(
    tmp_path: Path,
) -> None:
    class GrowingArchiveReader(FakeArchiveReader):
        def __init__(self) -> None:
            self.page_count = 2

        async def list_page(
            self,
            _bundle: object,
            *,
            account_id: int,
            credential_version: int,
            status: str,
            page_number: int,
            page_size: int,
        ) -> Tuple[Mapping[str, Any], ...]:
            del _bundle, page_size
            assert (account_id, credential_version, status) == (
                1,
                1,
                'is_pubing,pubed,not_pubed',
            )
            if page_number > 1:
                return ()
            return (
                {
                    'Archive': {
                        'aid': 101,
                        'bvid': 'BV1abcdefgh',
                        'title': '早期虚荣录播',
                        'pubtime': 900,
                    },
                    'cid_list': [201 + index for index in range(self.page_count)],
                },
            )

        async def viewer_detail(
            self,
            _bundle: object,
            *,
            account_id: int,
            credential_version: int,
            bvid: str,
        ) -> Mapping[str, Any]:
            assert (account_id, credential_version, bvid) == (1, 1, 'BV1abcdefgh')
            return {
                'data': {
                    'aid': 101,
                    'bvid': bvid,
                    'title': '早期虚荣录播',
                    'pages': [
                        {
                            'page': page,
                            'cid': 200 + page,
                            'part': 'P{}'.format(page),
                            'duration': 600 if page < 3 else 472,
                        }
                        for page in range(1, self.page_count + 1)
                    ],
                }
            }

    database = BiliUploadDatabase(str(tmp_path / 'blrec.sqlite3'))
    await database.open()
    reader = GrowingArchiveReader()
    try:
        await seed_account(database)
        service = ArchiveBackfillService(
            database,
            reader,
            bundle_loader=lambda _account_id: async_value(object()),
            remote_media_cache=FakeRemoteMediaCache(),
            clock=lambda: 1_000,
        )
        await service.request(1)
        assert await service.run_once() is True
        assert await service.run_once() is True

        original_parts = await database.fetchall(
            'SELECT page,recording_part_id FROM vainglory_archive_parts ORDER BY page'
        )
        for row in original_parts:
            await database.execute(
                'INSERT INTO vainglory_part_jobs('
                'part_id,session_id,state,request_kind,progress,algorithm_version,'
                'match_count,error,requested_at,started_at,completed_at,updated_at) '
                "SELECT id,session_id,'ready','archive',1,2,0,NULL,"
                '1000,1000,1000,1000 FROM recording_parts WHERE id=?',
                (int(row['recording_part_id']),),
            )

        reader.page_count = 3
        await service.request(1)
        assert await service.run_once() is True

        stale = await database.fetchone(
            'SELECT state,page_count,completed_page_count '
            'FROM vainglory_archive_imports'
        )
        assert stale is not None
        assert dict(stale) == {
            'state': 'queued',
            'page_count': 0,
            'completed_page_count': 0,
        }

        assert await service.run_once() is True
        refreshed = await database.fetchone(
            'SELECT state,page_count,completed_page_count '
            'FROM vainglory_archive_imports'
        )
        assert refreshed is not None
        assert dict(refreshed) == {
            'state': 'analyzing',
            'page_count': 3,
            'completed_page_count': 2,
        }
        refreshed_parts = await database.fetchall(
            'SELECT page,cid,recording_part_id,duration_seconds '
            'FROM vainglory_archive_parts ORDER BY page'
        )
        assert [
            (int(row['page']), int(row['cid']), int(row['duration_seconds']))
            for row in refreshed_parts
        ] == [(1, 201, 600), (2, 202, 600), (3, 203, 472)]
        assert [int(row['recording_part_id']) for row in refreshed_parts[:2]] == [
            int(row['recording_part_id']) for row in original_parts
        ]
        assert await database.scalar('SELECT COUNT(*) FROM recording_sessions') == 1
        session = await database.fetchone(
            'SELECT started_at,ended_at,live_end_time FROM recording_sessions'
        )
        assert session is not None
        assert (
            int(session['ended_at']) - int(session['started_at']),
            int(session['live_end_time']) - int(session['started_at']),
        ) == (1_672, 1_672)
        run = await database.fetchone('SELECT started_at,ended_at FROM recording_runs')
        assert run is not None
        assert int(run['ended_at']) - int(run['started_at']) == 1_672
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_infers_historical_anchor_from_live_room_link_and_known_sessions(
    tmp_path: Path,
) -> None:
    class Reader(FakeArchiveReader):
        async def detail(
            self,
            _bundle: object,
            *,
            account_id: int,
            credential_version: int,
            bvid: str,
        ) -> Mapping[str, Any]:
            detail = await super().detail(
                _bundle,
                account_id=account_id,
                credential_version=credential_version,
                bvid=bvid,
            )
            return {
                **detail,
                'data': {
                    **detail['data'],
                    'archive': {
                        **detail['data']['archive'],
                        'title': '主播甲的直播回放',
                        'desc': '直播间：https://live.bilibili.com/777',
                    },
                },
            }

    database = BiliUploadDatabase(str(tmp_path / 'blrec.sqlite3'))
    await database.open()
    try:
        await seed_account(database)
        await database.execute(
            'INSERT INTO recording_sessions('
            'id,room_id,broadcast_session_key,state,started_at,title,'
            'anchor_uid,anchor_name) '
            "VALUES(99,777,'known:777','closed',500,'已知直播',123,'主播甲')"
        )
        service = ArchiveBackfillService(
            database,
            Reader(),
            bundle_loader=lambda _account_id: async_value(object()),
            remote_media_cache=FakeRemoteMediaCache(),
            clock=lambda: 1_000,
        )
        await service.request(1)
        assert await service.run_once() is True
        assert await service.run_once() is True

        imported = await database.fetchone(
            'SELECT session.room_id,session.anchor_uid,session.anchor_name '
            'FROM vainglory_archive_imports imported '
            'JOIN recording_sessions session ON session.id=imported.session_id'
        )

        assert imported is not None
        assert (
            int(imported['room_id']),
            int(imported['anchor_uid']),
            str(imported['anchor_name']),
        ) == (777, 123, '主播甲')
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_historical_anchor_prefers_a_bound_room_from_multiple_description_links(
    tmp_path: Path,
) -> None:
    class Reader(FakeArchiveReader):
        async def detail(
            self,
            _bundle: object,
            *,
            account_id: int,
            credential_version: int,
            bvid: str,
        ) -> Mapping[str, Any]:
            detail = await super().detail(
                _bundle,
                account_id=account_id,
                credential_version=credential_version,
                bvid=bvid,
            )
            return {
                **detail,
                'data': {
                    **detail['data'],
                    'archive': {
                        **detail['data']['archive'],
                        'title': '旧直播录像',
                        'desc': (
                            '投稿账号：https://live.bilibili.com/111\n'
                            '原直播间号：930376'
                        ),
                    },
                },
            }

    database = BiliUploadDatabase(str(tmp_path / 'blrec.sqlite3'))
    await database.open()
    try:
        await seed_account(database)
        await database.execute(
            'INSERT INTO vainglory_players('
            "id,name,origin,created_at,updated_at) VALUES(9,'茉莉','manual',1,1)"
        )
        await database.execute(
            'INSERT INTO vainglory_player_rooms('
            'room_id,player_id,created_at,updated_at) VALUES(930376,9,1,1)'
        )
        service = ArchiveBackfillService(
            database,
            Reader(),
            bundle_loader=lambda _account_id: async_value(object()),
            remote_media_cache=FakeRemoteMediaCache(),
            clock=lambda: 1_000,
        )
        await service.request(1)
        assert await service.run_once() is True
        assert await service.run_once() is True

        imported = await database.fetchone(
            'SELECT session.room_id,session.anchor_name '
            'FROM vainglory_archive_imports imported '
            'JOIN recording_sessions session ON session.id=imported.session_id'
        )
        assert imported is not None
        assert int(imported['room_id']) == 930376
        assert str(imported['anchor_name']) == '茉莉'
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_history_control_accepts_operator_defined_daily_limit(
    tmp_path: Path,
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'blrec.sqlite3'))
    await database.open()
    try:
        await seed_account(database)
        service = ArchiveBackfillService(
            database,
            FakeArchiveReader(),
            bundle_loader=lambda _account_id: async_value(object()),
            remote_media_cache=FakeRemoteMediaCache(),
            clock=lambda: 1_000,
        )
        await service.request(1)

        updated = await service.update_control(1, daily_limit=50_000)

        assert updated.daily_limit == 50_000
        stored = await database.fetchone(
            'SELECT daily_limit,daily_limit_override,daily_limit_override_v2 '
            'FROM vainglory_archive_syncs WHERE account_id=1'
        )
        assert stored is not None and dict(stored) == {
            'daily_limit': 500,
            'daily_limit_override': 1_000,
            'daily_limit_override_v2': 50_000,
        }
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_requeues_an_import_interrupted_before_materialization(
    tmp_path: Path,
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'blrec.sqlite3'))
    await database.open()
    try:
        await seed_account(database)
        service = ArchiveBackfillService(
            database,
            FakeArchiveReader(),
            bundle_loader=lambda _account_id: async_value(object()),
            remote_media_cache=FakeRemoteMediaCache(),
            clock=lambda: 1_000,
        )
        await service.request(1)
        assert await service.run_once() is True
        await database.execute(
            "UPDATE vainglory_archive_imports SET state='downloading',"
            'page_count=0,progress=0.4'
        )

        assert await service.recover_interrupted() == 1
        assert (
            await database.scalar('SELECT state FROM vainglory_archive_imports LIMIT 1')
            == 'queued'
        )
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_history_backfill_pages_lazily_and_honors_pause_and_daily_limit(
    tmp_path: Path,
) -> None:
    class PagedReader(FakeArchiveReader):
        def __init__(self) -> None:
            self.page_calls = []

        async def list_page(
            self,
            _bundle: object,
            *,
            account_id: int,
            credential_version: int,
            status: str,
            page_number: int,
            page_size: int,
        ) -> Tuple[Mapping[str, Any], ...]:
            del _bundle, account_id, credential_version, status
            self.page_calls.append(page_number)
            if page_number == 1:
                return tuple(
                    {
                        'Archive': {
                            'aid': 10_000 + index,
                            'bvid': 'BV1{:08d}'.format(index),
                            'title': '历史稿件 {}'.format(index),
                            'pubtime': 900 + index,
                        }
                    }
                    for index in range(page_size)
                )
            return ()

        async def viewer_detail(
            self,
            _bundle: object,
            *,
            account_id: int,
            credential_version: int,
            bvid: str,
        ) -> Mapping[str, Any]:
            del _bundle, account_id, credential_version
            suffix = int(bvid[-8:])
            return {
                'data': {
                    'aid': 10_000 + suffix,
                    'bvid': bvid,
                    'title': '历史稿件 {}'.format(suffix),
                    'pages': [
                        {
                            'page': 1,
                            'cid': 20_000 + suffix,
                            'part': 'P1',
                            'duration': 600,
                        }
                    ],
                }
            }

    database = BiliUploadDatabase(str(tmp_path / 'blrec.sqlite3'))
    await database.open()
    try:
        await seed_account(database)
        reader = PagedReader()
        service = ArchiveBackfillService(
            database,
            reader,
            bundle_loader=lambda _account_id: async_value(object()),
            remote_media_cache=FakeRemoteMediaCache(),
            clock=lambda: 1_000,
        )
        await service.request(1)
        await service.update_control(1, daily_limit=1)
        assert await service.run_once() is True
        assert reader.page_calls == [1]
        assert (
            await database.scalar('SELECT COUNT(*) FROM vainglory_archive_imports')
            == 50
        )
        first_page = await service.status(1)
        assert first_page.next_page == 2
        assert first_page.discovery_complete is False

        paused = await service.update_control(1, paused=True)
        assert paused.operator_paused is True
        assert await service.run_once() is False

        await service.update_control(1, paused=False)
        assert await service.run_once() is True
        await service.run_once()
        assert await service.run_once() is False
        limited = await service.status(1)
        assert limited.daily_used == 1
        assert reader.page_calls == [1]
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_continues_discovery_before_the_priority_season(tmp_path: Path) -> None:
    now = [1_786_420_800]

    class HistoryReader:
        def __init__(self) -> None:
            self.page_calls = []

        async def list_page(
            self,
            _bundle: object,
            *,
            account_id: int,
            credential_version: int,
            status: str,
            page_number: int,
            page_size: int,
        ) -> Tuple[Mapping[str, Any], ...]:
            del _bundle, account_id, credential_version, status
            assert page_size == 1
            self.page_calls.append(page_number)
            if page_number == 1:
                return (
                    {
                        'Archive': {
                            'aid': 101,
                            'bvid': 'BV1current01',
                            'title': '夏季赛录播',
                            'pubtime': now[0] - 60,
                        }
                    },
                )
            if page_number == 2:
                return (
                    {
                        'Archive': {
                            'aid': 102,
                            'bvid': 'BV1history01',
                            'title': '更早历史录播',
                            'pubtime': 1_700_000_000,
                        }
                    },
                )
            return ()

        async def viewer_detail(
            self,
            _bundle: object,
            *,
            account_id: int,
            credential_version: int,
            bvid: str,
        ) -> Mapping[str, Any]:
            del _bundle, account_id, credential_version
            return {
                'data': {
                    'aid': 101 if bvid == 'BV1current01' else 102,
                    'bvid': bvid,
                    'title': '录播',
                    'pages': [
                        {
                            'page': 1,
                            'cid': 201 if bvid == 'BV1current01' else 202,
                            'part': 'P1',
                            'duration': 600,
                        }
                    ],
                }
            }

    database = BiliUploadDatabase(str(tmp_path / 'blrec.sqlite3'))
    await database.open()
    try:
        await seed_account(database)
        reader = HistoryReader()
        service = ArchiveBackfillService(
            database,
            reader,
            bundle_loader=lambda _account_id: async_value(object()),
            remote_media_cache=FakeRemoteMediaCache(),
            clock=lambda: now[0],
        )
        service.PAGE_SIZE = 1
        await service.request(1)

        for expected_page in (1, 2, 3):
            assert await service.run_once() is True
            assert reader.page_calls[-1] == expected_page
            now[0] += service.DISCOVERY_INTERVAL_SECONDS

        discovered = await database.fetchall(
            'SELECT bvid,state FROM vainglory_archive_imports '
            'ORDER BY recording_started_at DESC'
        )
        assert [(str(row['bvid']), str(row['state'])) for row in discovered] == [
            ('BV1current01', 'queued'),
            ('BV1history01', 'queued'),
        ]

        assert await service.run_once() is True
        prioritized = await database.fetchall(
            'SELECT bvid,state FROM vainglory_archive_imports '
            'ORDER BY recording_started_at DESC'
        )
        assert [(str(row['bvid']), str(row['state'])) for row in prioritized] == [
            ('BV1current01', 'analyzing'),
            ('BV1history01', 'queued'),
        ]
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_daily_limit_counts_archives_when_their_download_starts(
    tmp_path: Path,
) -> None:
    now = [1_786_420_800]

    class TwoArchiveReader:
        async def list_page(
            self,
            _bundle: object,
            *,
            account_id: int,
            credential_version: int,
            status: str,
            page_number: int,
            page_size: int,
        ) -> Tuple[Mapping[str, Any], ...]:
            del _bundle, account_id, credential_version, status, page_size
            if page_number > 1:
                return ()
            return tuple(
                {
                    'Archive': {
                        'aid': 101 + index,
                        'bvid': 'BV1daily{:04d}'.format(index),
                        'title': '夏季赛录播 {}'.format(index),
                        'pubtime': now[0] - index - 60,
                    }
                }
                for index in range(2)
            )

        async def viewer_detail(
            self,
            _bundle: object,
            *,
            account_id: int,
            credential_version: int,
            bvid: str,
        ) -> Mapping[str, Any]:
            del _bundle, account_id, credential_version
            suffix = int(bvid[-4:])
            return {
                'data': {
                    'aid': 101 + suffix,
                    'bvid': bvid,
                    'title': '夏季赛录播 {}'.format(suffix),
                    'pages': [
                        {
                            'page': page,
                            'cid': 1_000 + suffix * 10 + page,
                            'part': 'P{}'.format(page),
                            'duration': 600,
                        }
                        for page in (1, 2)
                    ],
                }
            }

    database = BiliUploadDatabase(str(tmp_path / 'blrec.sqlite3'))
    await database.open()
    cache = FakeRemoteMediaCache()
    try:
        await seed_account(database)
        service = ArchiveBackfillService(
            database,
            TwoArchiveReader(),
            bundle_loader=lambda _account_id: async_value(object()),
            remote_media_cache=cache,
            clock=lambda: now[0],
        )
        await service.request(1)
        await service.update_control(1, daily_limit=1)

        assert await service.run_once() is True
        assert await service.run_once() is True
        assert (await service.status(1)).daily_used == 0
        assert cache.requests == []

        assert await service.run_once() is True
        assert await service.run_once() is True
        first_day = await service.status(1)
        assert first_day.daily_used == 1
        assert len(cache.requests) == 2
        assert cache.force_remote_requests == [True, True]
        assert await service.run_once() is False

        imports = await database.fetchall(
            'SELECT state,page_count FROM vainglory_archive_imports '
            'ORDER BY recording_started_at DESC'
        )
        assert [(str(row['state']), int(row['page_count'])) for row in imports] == [
            ('analyzing', 2),
            ('queued', 0),
        ]

        now[0] += 24 * 60 * 60
        assert await service.run_once() is True
        assert await service.run_once() is True
        assert await service.run_once() is True
        second_day = await service.status(1)
        assert second_day.daily_used == 1
        assert second_day.quota_day != first_day.quota_day
        assert len(cache.requests) == 4
        assert cache.force_remote_requests == [True, True, True, True]
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_logically_deleted_local_residue_forces_archive_download(
    tmp_path: Path,
) -> None:
    class ResidueAwareCache(FakeRemoteMediaCache):
        async def request(
            self, part_id: int, *, force_remote: bool = False
        ) -> RemoteMediaStatus:
            self.requests.append(part_id)
            self.force_remote_requests.append(force_remote)
            return RemoteMediaStatus(
                part_id=part_id,
                state='pending' if force_remote else 'local',
                progress=0,
                remote_available=force_remote,
            )

    database = BiliUploadDatabase(str(tmp_path / 'blrec.sqlite3'))
    await database.open()
    cache = ResidueAwareCache()
    try:
        await seed_account(database)
        service = ArchiveBackfillService(
            database,
            FakeArchiveReader(),
            bundle_loader=lambda _account_id: async_value(object()),
            remote_media_cache=cache,
            clock=lambda: 1_000,
        )
        await service.request(1)
        assert await service.run_once() is True
        assert await service.run_once() is True
        part_id = int(
            await database.scalar(
                'SELECT recording_part_id FROM vainglory_archive_parts '
                'ORDER BY page LIMIT 1'
            )
        )
        await database.execute(
            "UPDATE recording_parts SET final_path='/rec/residue.mp4',"
            "artifact_state='ready',video_deleted_at=999 WHERE id=?",
            (part_id,),
        )
        await database.execute(
            'INSERT INTO vainglory_part_jobs('
            'part_id,session_id,state,request_kind,progress,algorithm_version,'
            'match_count,error,requested_at,started_at,completed_at,updated_at) '
            "SELECT id,session_id,'pending','archive',0,15,0,NULL,1000,NULL,NULL,1000 "
            'FROM recording_parts WHERE id=?',
            (part_id,),
        )
        await database.execute(
            "UPDATE vainglory_archive_syncs SET state='failed',"
            "error='历史发现暂时失败' WHERE account_id=1"
        )

        assert await service.run_once() is True

        assert cache.requests == [part_id]
        assert cache.force_remote_requests == [True]
        assert (
            await database.scalar(
                'SELECT state FROM vainglory_archive_parts '
                'WHERE recording_part_id=?',
                (part_id,),
            )
            == 'downloading'
        )
    finally:
        await database.close()


async def async_value(value: object) -> object:
    return value
