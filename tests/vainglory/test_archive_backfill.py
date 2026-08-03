from pathlib import Path
from typing import Any, Mapping, Tuple

import pytest

from blrec.bili_upload.database import BiliUploadDatabase
from blrec.bili_upload.errors import BiliApiError
from blrec.bili_upload.remote_media import RemoteMediaStatus
from blrec.vainglory.archive_backfill import ArchiveBackfillService


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
        assert status == 'pubed'
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

    async def request(self, part_id: int) -> RemoteMediaStatus:
        self.requests.append(part_id)
        return RemoteMediaStatus(
            part_id=part_id, state='pending', progress=0, remote_available=True
        )


async def seed_account(database: BiliUploadDatabase) -> None:
    await database.execute(
        "INSERT INTO bili_accounts("
        "id,uid,display_name,credential_ciphertext,credential_version,key_id,"
        "state,created_at,updated_at) "
        "VALUES(1,42,'旧账号',X'00',1,'key','active',1,1)"
    )


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
async def test_skips_archive_pages_shorter_than_ten_minutes(tmp_path: Path) -> None:
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
        ] == [(2, 202, 600)]
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


async def async_value(value: object) -> object:
    return value
