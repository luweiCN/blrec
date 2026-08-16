from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any, AsyncIterator, List

import pytest

from blrec.bili_upload.accounts import AccountWriteGate
from blrec.bili_upload.archive_migration import (
    ArchiveDetail,
    ArchiveListing,
    ArchiveMigrationService,
    ArchivePage,
    BiliPublicArchiveReader,
    YtDlpSpaceArchiveCatalog,
)
from blrec.bili_upload.database import BiliUploadDatabase
from blrec.bili_upload.policies import default_room_upload_policy
from blrec.bili_upload.session_submission import (
    decode_submission_settings,
    encode_submission_settings,
)
from blrec.bili_upload.upload import UploadCoordinator


class FakeCatalog:
    def __init__(self, entries: tuple[ArchiveListing, ...]) -> None:
        self.entries = entries
        self.calls: List[int] = []

    async def iter_archives(
        self, _bundle: Any, *, source_uid: int
    ) -> AsyncIterator[ArchiveListing]:
        self.calls.append(source_uid)
        for entry in self.entries:
            yield entry


class FakeDetailReader:
    def __init__(
        self, detail: ArchiveDetail, *, match_requested_bvid: bool = False
    ) -> None:
        self.detail_value = detail
        self.match_requested_bvid = match_requested_bvid
        self.calls: List[str] = []

    async def detail(self, _bundle: Any, *, bvid: str) -> ArchiveDetail:
        self.calls.append(bvid)
        if self.match_requested_bvid:
            return replace(self.detail_value, bvid=bvid)
        return self.detail_value


class FakeDownloader:
    def __init__(self) -> None:
        self.calls: List[tuple[str, int, Path]] = []
        self.danmaku_calls: List[tuple[str, int, Path]] = []

    async def download(
        self,
        _bundle: Any,
        *,
        bvid: str,
        cid: int,
        page: int,
        target: Path,
        danmaku_target: Path | None,
        progress: Any,
    ) -> None:
        self.calls.append((bvid, page, target))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(('page-{}'.format(page)).encode())
        assert danmaku_target is not None
        danmaku_target.write_text('<i></i>')
        await progress(6, 6)

    async def download_danmaku(
        self, _bundle: Any, *, bvid: str, cid: int, page: int, target: Path
    ) -> None:
        del cid
        self.danmaku_calls.append((bvid, page, target))
        target.write_text('<i></i>')


class FakeTaskCreator:
    def __init__(self, database: BiliUploadDatabase) -> None:
        self.database = database
        self.calls: List[tuple[int, str, str, tuple[str, ...]]] = []

    async def create_archive_migration_job(
        self,
        session_id: int,
        *,
        description: str,
        tags: str,
        part_titles: tuple[str, ...],
    ) -> int:
        self.calls.append((session_id, description, tags, part_titles))
        account_id = int(
            await self.database.scalar(
                'SELECT target_account_id FROM archive_migration_jobs LIMIT 1'
            )
        )
        parts = await self.database.fetchall(
            'SELECT part_index,source_path,final_path,xml_path '
            'FROM recording_parts WHERE session_id=? ORDER BY part_index',
            (session_id,),
        )
        snapshot = json.dumps(
            {
                'description': description,
                'danmaku_backfill': True,
                'part_titles': list(part_titles),
                'recording_part_indexes': [int(part['part_index']) for part in parts],
            },
            ensure_ascii=False,
        )
        now = 1000

        def create(connection) -> int:
            job_id = int(
                connection.execute(
                    'INSERT INTO upload_jobs('
                    'session_id,account_id,policy_snapshot_json,state,submit_state,'
                    'operator_paused,operator_resume_state,review_reason,'
                    'created_at,updated_at) '
                    "VALUES(?,?,?,'paused','prepared',1,'ready',?,?,?)",
                    (session_id, account_id, snapshot, '迁移一致性校验中', now, now),
                ).lastrowid
            )
            for part in parts:
                connection.execute(
                    'INSERT INTO upload_parts('
                    'job_id,part_index,source_path,final_path,xml_path,'
                    'artifact_state,upload_state,danmaku_import_state) '
                    "VALUES(?,?,?,?,?,'ready','prepared','pending')",
                    (
                        job_id,
                        int(part['part_index']),
                        str(part['source_path']),
                        str(part['final_path']),
                        str(part['xml_path']),
                    ),
                )
            return job_id

        return await self.database.write(create)


async def seed_accounts(database: BiliUploadDatabase) -> None:
    await database.execute(
        'INSERT INTO bili_accounts('
        'id,uid,display_name,credential_ciphertext,credential_version,key_id,'
        'state,created_at,updated_at) VALUES'
        "(1,100,'下载账号',X'00',1,'key','active',1,1),"
        "(2,200,'目标账号',X'00',1,'key','active',1,1)"
    )


def archive_detail() -> ArchiveDetail:
    return ArchiveDetail(
        aid=101,
        bvid='BV1abcdefgh',
        owner_uid=300,
        owner_name='源账号',
        title='旧稿件',
        description='原简介',
        tags=('虚荣', '直播回放'),
        tid=172,
        cover_url='https://i0.hdslb.com/cover.jpg',
        published_at=900,
        pages=(
            ArchivePage(page=1, cid=201, title='第一局', duration_seconds=600),
            ArchivePage(page=2, cid=202, title='第二局', duration_seconds=720),
        ),
    )


@pytest.mark.asyncio
async def test_migration_downloads_each_page_and_creates_one_upload_task(
    tmp_path: Path,
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'blrec.sqlite3'))
    await database.open()
    try:
        await seed_accounts(database)
        catalog = FakeCatalog(
            (ArchiveListing('BV1abcdefgh', '旧稿件', published_at=900),)
        )
        details = FakeDetailReader(archive_detail())
        downloader = FakeDownloader()
        creator = FakeTaskCreator(database)
        service = ArchiveMigrationService(
            database,
            recording_root=tmp_path / 'rec',
            catalog=catalog,
            detail_reader=details,
            downloader=downloader,
            bundle_loader=lambda _account_id: async_value(object()),
            task_creator=creator,
            clock=lambda: 1000,
        )

        requested = await service.request(
            source_uid=300, download_account_id=1, target_account_id=2
        )
        assert requested.state == 'discovering'
        assert requested.daily_limit == 60
        assert await service.run_once() is True
        assert await service.run_once() is True

        status = await service.status(requested.id)
        assert status.state == 'completed'
        assert status.source_name is None
        assert status.discovered_count == 1
        assert status.completed_count == 1
        assert status.failed_count == 0
        assert [call[:2] for call in downloader.calls] == [
            ('BV1abcdefgh', 1),
            ('BV1abcdefgh', 2),
        ]
        assert creator.calls[0][1:] == ('原简介', '虚荣,直播回放', ('第一局', '第二局'))

        item = await database.fetchone(
            'SELECT state,page_count,downloaded_page_count,attempt_count,'
            'session_id,upload_job_id '
            'FROM archive_migration_items'
        )
        assert item is not None
        assert dict(item) == {
            'state': 'task_created',
            'page_count': 2,
            'downloaded_page_count': 2,
            'attempt_count': 1,
            'session_id': creator.calls[0][0],
            'upload_job_id': int(item['upload_job_id']),
        }
        parts = await database.fetchall(
            'SELECT part_index,artifact_state,final_path,xml_path '
            'FROM recording_parts ORDER BY part_index'
        )
        assert [
            (int(row['part_index']), str(row['artifact_state'])) for row in parts
        ] == [(1, 'ready'), (2, 'ready')]
        assert all(Path(str(row['final_path'])).is_file() for row in parts)
        assert all(Path(str(row['xml_path'])).is_file() for row in parts)
        session = await database.fetchone(
            'SELECT room_id,anchor_uid,anchor_name,upload_override_json '
            'FROM recording_sessions'
        )
        assert session is not None
        assert (
            int(session['room_id']),
            session['anchor_uid'],
            str(session['anchor_name']),
        ) == (0, None, '')
        migration_policy = decode_submission_settings(
            str(session['upload_override_json'])
        )
        assert migration_policy.retention_mode == 'approved'
        assert migration_policy.retention_days == 0
        await database.execute(
            "UPDATE recording_sessions SET room_id=300,anchor_uid=300,"
            "anchor_name='源账号'"
        )
        assert await service.repair_inferred_anchors() == 1
        cleared = await database.fetchone(
            'SELECT room_id,anchor_uid,anchor_name FROM recording_sessions'
        )
        assert cleared is not None
        assert (
            int(cleared['room_id']),
            cleared['anchor_uid'],
            str(cleared['anchor_name']),
        ) == (0, None, '')
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_migration_uses_a_known_recorded_anchor_named_in_the_title(
    tmp_path: Path,
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'blrec.sqlite3'))
    await database.open()
    try:
        await seed_accounts(database)
        await database.execute(
            'INSERT INTO recording_sessions('
            'id,room_id,broadcast_session_key,state,started_at,title,'
            'anchor_uid,anchor_name) '
            "VALUES(99,21045351,'known:anchor','closed',500,'直播',"
            "342828919,'-玩不明白-')"
        )
        detail = replace(
            archive_detail(), title='[2023-01-06][-玩不明白-][虚荣直播回放]'
        )
        service = ArchiveMigrationService(
            database,
            recording_root=tmp_path / 'rec',
            catalog=FakeCatalog(
                (ArchiveListing(detail.bvid, detail.title, published_at=900),)
            ),
            detail_reader=FakeDetailReader(detail),
            downloader=FakeDownloader(),
            bundle_loader=lambda _account_id: async_value(object()),
            task_creator=FakeTaskCreator(database),
            clock=lambda: 1000,
        )

        await service.request(
            source_uid=300, download_account_id=1, target_account_id=2
        )
        assert await service.run_once()
        assert await service.run_once()
        migrated = await database.fetchone(
            "SELECT room_id,anchor_uid,anchor_name FROM recording_sessions "
            "WHERE broadcast_session_key LIKE 'bili-migration:%'"
        )

        assert migrated is not None
        assert (
            int(migrated['room_id']),
            int(migrated['anchor_uid']),
            str(migrated['anchor_name']),
        ) == (21045351, 342828919, '-玩不明白-')
        await database.execute(
            'UPDATE recording_sessions SET room_id=300,anchor_uid=300,'
            "anchor_name='源账号' WHERE broadcast_session_key LIKE "
            "'bili-migration:%'"
        )

        assert await service.repair_inferred_anchors() == 1
        repaired = await database.fetchone(
            "SELECT room_id,anchor_uid,anchor_name FROM recording_sessions "
            "WHERE broadcast_session_key LIKE 'bili-migration:%'"
        )
        assert repaired is not None
        assert (
            int(repaired['room_id']),
            int(repaired['anchor_uid']),
            str(repaired['anchor_name']),
        ) == (21045351, 342828919, '-玩不明白-')
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_rescan_is_idempotent_and_reuses_created_task(tmp_path: Path) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'blrec.sqlite3'))
    await database.open()
    try:
        await seed_accounts(database)
        catalog = FakeCatalog(
            (ArchiveListing('BV1abcdefgh', '旧稿件', published_at=900),)
        )
        downloader = FakeDownloader()
        creator = FakeTaskCreator(database)
        service = ArchiveMigrationService(
            database,
            recording_root=tmp_path / 'rec',
            catalog=catalog,
            detail_reader=FakeDetailReader(archive_detail()),
            downloader=downloader,
            bundle_loader=lambda _account_id: async_value(object()),
            task_creator=creator,
            clock=lambda: 1000,
        )
        first = await service.request(
            source_uid=300, download_account_id=1, target_account_id=2
        )
        assert await service.run_once()
        assert await service.run_once()

        second = await service.request(
            source_uid=300, download_account_id=1, target_account_id=2
        )
        assert second.id == first.id
        assert await service.run_once()
        status = await service.status(first.id)

        assert status.state == 'completed'
        assert (
            await database.scalar('SELECT COUNT(*) FROM archive_migration_items') == 1
        )
        assert await database.scalar('SELECT COUNT(*) FROM recording_sessions') == 1
        assert await database.scalar('SELECT COUNT(*) FROM upload_jobs') == 1
        assert len(downloader.calls) == 2
        assert len(creator.calls) == 1
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_requeues_migration_item_interrupted_during_a_side_effect(
    tmp_path: Path,
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'blrec.sqlite3'))
    await database.open()
    try:
        await seed_accounts(database)
        service = ArchiveMigrationService(
            database,
            recording_root=tmp_path / 'rec',
            catalog=FakeCatalog(
                (ArchiveListing('BV1abcdefgh', '旧稿件', published_at=900),)
            ),
            detail_reader=FakeDetailReader(archive_detail()),
            downloader=FakeDownloader(),
            bundle_loader=lambda _account_id: async_value(object()),
            task_creator=FakeTaskCreator(database),
            clock=lambda: 1000,
        )
        requested = await service.request(
            source_uid=300, download_account_id=1, target_account_id=2
        )
        assert await service.run_once() is True
        await database.execute(
            "UPDATE archive_migration_items SET state='creating_task',"
            'progress=0.95 WHERE migration_id=?',
            (requested.id,),
        )

        assert await service.recover_interrupted() == 1
        assert (
            await database.scalar(
                'SELECT state FROM archive_migration_items WHERE migration_id=?',
                (requested.id,),
            )
            == 'queued'
        )
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_migration_pause_and_daily_quota_survive_worker_iterations(
    tmp_path: Path,
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'blrec.sqlite3'))
    await database.open()
    try:
        await seed_accounts(database)
        now = [1_000]
        details = FakeDetailReader(archive_detail(), match_requested_bvid=True)
        service = ArchiveMigrationService(
            database,
            recording_root=tmp_path / 'rec',
            catalog=FakeCatalog(
                (
                    ArchiveListing('BV1abcdefgh', '旧稿件一', published_at=900),
                    ArchiveListing('BV1bcdefghi', '旧稿件二', published_at=901),
                )
            ),
            detail_reader=details,
            downloader=FakeDownloader(),
            bundle_loader=lambda _account_id: async_value(object()),
            task_creator=FakeTaskCreator(database),
            clock=lambda: now[0],
        )
        requested = await service.request(
            source_uid=300, download_account_id=1, target_account_id=2
        )
        assert await service.run_once() is True

        paused = await service.update_control(requested.id, paused=True, daily_limit=1)
        assert paused.operator_paused is True
        assert paused.daily_limit == 1
        assert await service.run_once() is False

        resumed = await service.update_control(requested.id, paused=False)
        assert resumed.operator_paused is False
        assert await service.run_once() is True
        assert await service.run_once() is False
        limited = await service.status(requested.id)
        assert limited.daily_used == 1
        assert limited.completed_count == 1

        now[0] += 86_400
        assert await service.run_once() is True
        next_day = await service.status(requested.id)
        assert next_day.daily_used == 1
        assert len(details.calls) == 2
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_rejects_copying_an_account_back_to_itself(tmp_path: Path) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'blrec.sqlite3'))
    await database.open()
    try:
        await seed_accounts(database)
        service = ArchiveMigrationService(
            database,
            recording_root=tmp_path / 'rec',
            catalog=FakeCatalog(()),
            detail_reader=FakeDetailReader(archive_detail()),
            downloader=FakeDownloader(),
            bundle_loader=lambda _account_id: async_value(object()),
            task_creator=FakeTaskCreator(database),
            clock=lambda: 1000,
        )

        with pytest.raises(ValueError, match='源账号和目标账号不能相同'):
            await service.request(
                source_uid=200, download_account_id=1, target_account_id=2
            )
    finally:
        await database.close()


def test_ytdlp_catalog_uses_cookie_and_streams_flat_entries() -> None:
    command = YtDlpSpaceArchiveCatalog.build_command(
        executable='yt-dlp',
        cookie_path=Path('/tmp/cookies.txt'),
        source_uid=1409676,
        source_address='192.0.2.10',
    )

    assert '--cookies' in command
    assert '--flat-playlist' in command
    assert '--lazy-playlist' in command
    assert '--dump-json' in command
    assert '--source-address' in command
    assert command[-1] == 'https://space.bilibili.com/1409676/video'
    parsed = YtDlpSpaceArchiveCatalog.parse_entry(
        '{"id":"BV1Jx411E7yp","title":"虚荣合集","timestamp":1509504930}'
    )
    assert parsed == ArchiveListing(
        bvid='BV1Jx411E7yp', title='虚荣合集', published_at=1509504930
    )


@pytest.mark.asyncio
async def test_public_reader_preserves_source_metadata_and_page_order() -> None:
    class Protocol:
        async def public_archive_view(self, _bundle: Any, *, bvid: str) -> Any:
            return {
                'data': {
                    'aid': 101,
                    'bvid': bvid,
                    'owner': {'mid': 300, 'name': '源账号'},
                    'title': '旧稿件',
                    'desc': '  原简介\n第二行  ',
                    'tid': 172,
                    'pic': 'http://i0.hdslb.com/cover.jpg',
                    'pubdate': 900,
                    'pages': [
                        {'page': 2, 'cid': 202, 'part': '第二局', 'duration': 720},
                        {'page': 1, 'cid': 201, 'part': '第一局', 'duration': 600},
                    ],
                }
            }

        async def public_archive_tags(self, _bundle: Any, *, bvid: str) -> Any:
            del bvid
            return {
                'data': [
                    {'tag_name': '虚荣'},
                    {'tag_name': '直播回放'},
                    {'tag_name': '虚荣'},
                ]
            }

    detail = await BiliPublicArchiveReader(Protocol()).detail(
        object(), bvid='BV1abcdefgh'
    )

    assert detail == replace(archive_detail(), description='  原简介\n第二行  ')


@pytest.mark.asyncio
async def test_upload_coordinator_creates_idempotent_migration_task(
    tmp_path: Path,
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'blrec.sqlite3'))
    await database.open()
    try:
        await seed_accounts(database)
        command = replace(
            default_room_upload_policy(),
            account_mode='fixed',
            account_id=2,
            title_template='{{ title }}',
            description_template='{{ archive_description }}',
            part_title_template='P{{ part_index }}',
            dynamic_template='{{ title }}',
            tid=172,
            tags='{{ archive_tags }}',
            creation_statement_id=-1,
            original_authorization=False,
            source='',
            auto_comment=False,
            danmaku_backfill=True,
            retention_days=0,
        )
        await database.execute(
            'INSERT INTO recording_sessions('
            'id,room_id,broadcast_session_key,live_start_time,state,started_at,'
            'ended_at,title,cover_url,anchor_uid,anchor_name,live_end_time,'
            'upload_intent,source_kind,upload_decision,upload_override_json,'
            'upload_resolution_state,upload_resolved_at) '
            "VALUES(10,300,'bili-migration:fixture',900,'closed',900,1000,'旧稿件',"
            "'https://i0.hdslb.com/cover.jpg',300,'源账号',1000,'upload','live',"
            "'upload',?,'not_requested',1000)",
            (encode_submission_settings(command),),
        )
        await database.execute(
            "INSERT INTO recording_runs(id,session_id,state,started_at,ended_at) "
            "VALUES('migration-run',10,'finished',900,1000)"
        )
        for page in (1, 2):
            path = tmp_path / 'p{}.mp4'.format(page)
            path.write_bytes(('page-{}'.format(page)).encode())
            xml_path = path.with_suffix('.xml')
            xml_path.write_text('<i></i>')
            await database.execute(
                'INSERT INTO recording_parts('
                'session_id,run_id,part_index,source_path,final_path,xml_path,'
                'record_start_time,artifact_state,xml_completed,'
                'record_duration_seconds,file_size_bytes,created_at,updated_at) '
                "VALUES(10,'migration-run',?,?,?,?,?,'ready',1,60,6,1000,1000)",
                (page, str(path), str(path), str(xml_path), 900 + page),
            )
        coordinator = UploadCoordinator(
            database,
            object(),
            object(),  # type: ignore[arg-type]
            bundle_loader=lambda _account_id: async_value(object()),
            account_gates=AccountWriteGate(database),
            cover_resolver=object(),  # type: ignore[arg-type]
            stability_seconds=30,
            clock=lambda: 1000,
        )

        first = await coordinator.create_archive_migration_job(
            10,
            description='  原简介\n第二行  ',
            tags='虚荣,直播回放',
            part_titles=('第一局', '第二局'),
        )
        second = await coordinator.create_archive_migration_job(
            10,
            description='  原简介\n第二行  ',
            tags='虚荣,直播回放',
            part_titles=('第一局', '第二局'),
        )

        assert second == first
        job = await database.fetchone(
            'SELECT account_id,state,operator_paused,priority,policy_snapshot_json '
            'FROM upload_jobs WHERE id=?',
            (first,),
        )
        assert job is not None
        assert (
            int(job['account_id']),
            str(job['state']),
            int(job['operator_paused']),
            int(job['priority']),
        ) == (2, 'paused', 1, -100)
        snapshot = json.loads(str(job['policy_snapshot_json']))
        assert snapshot['title'] == '旧稿件'
        assert snapshot['description'] == '  原简介\n第二行  '
        assert snapshot['tags'] == '虚荣,直播回放'
        assert snapshot['source'] == ''
        assert snapshot['copyright'] == 3
        assert snapshot['danmaku_backfill'] is True
        assert snapshot['part_titles'] == ['第一局', '第二局']
        assert (
            await database.scalar(
                'SELECT COUNT(*) FROM upload_parts WHERE job_id=?', (first,)
            )
            == 2
        )
        assert (
            await database.scalar(
                "SELECT COUNT(*) FROM upload_parts WHERE job_id=? "
                "AND xml_path IS NOT NULL AND danmaku_import_state='pending'",
                (first,),
            )
            == 2
        )

        legacy_snapshot = dict(snapshot)
        legacy_snapshot.update(
            {
                'description': '已经发布的旧简介',
                'creation_statement_id': -2,
                'copyright': 2,
                'source': 'https://www.bilibili.com/video/BV1abcdefgh',
                'danmaku_backfill': False,
            }
        )
        await database.execute(
            "UPDATE upload_jobs SET state='approved',submit_state='confirmed',"
            "danmaku_branch_state='disabled',policy_snapshot_json=? WHERE id=?",
            (json.dumps(legacy_snapshot, ensure_ascii=False), first),
        )
        await database.execute(
            "UPDATE upload_parts SET xml_path=NULL,danmaku_import_state='disabled' "
            'WHERE job_id=?',
            (first,),
        )

        refreshed = await coordinator.create_archive_migration_job(
            10,
            description='  原简介\n第二行  ',
            tags='虚荣,直播回放',
            part_titles=('第一局', '第二局'),
        )

        assert refreshed == first
        upgraded = await database.fetchone(
            'SELECT policy_snapshot_json,danmaku_branch_state '
            'FROM upload_jobs WHERE id=?',
            (first,),
        )
        assert upgraded is not None
        upgraded_snapshot = json.loads(str(upgraded['policy_snapshot_json']))
        assert upgraded_snapshot['description'] == '已经发布的旧简介'
        assert upgraded_snapshot['source'].endswith('BV1abcdefgh')
        assert upgraded_snapshot['danmaku_backfill'] is True
        assert str(upgraded['danmaku_branch_state']) == 'pending'
        assert (
            await database.scalar(
                "SELECT COUNT(*) FROM upload_parts WHERE job_id=? "
                "AND xml_path IS NOT NULL AND danmaku_import_state='pending'",
                (first,),
            )
            == 2
        )
    finally:
        await database.close()


async def async_value(value: object) -> object:
    return value
