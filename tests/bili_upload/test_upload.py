from __future__ import annotations

import asyncio
import json
import os
from dataclasses import replace
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

import pytest

from blrec.bili_upload.accounts import AccountWriteGate
from blrec.bili_upload.archive_reads import ArchiveReadService
from blrec.bili_upload.artifact_recovery import RecoveredArtifact
from blrec.bili_upload.database import BiliUploadDatabase, LeaseClaim
from blrec.bili_upload.deletion_worker import LocalDeletionWorker
from blrec.bili_upload.errors import (
    BiliApiError,
    DefinitelyNotSent,
    RemoteOutcomeUnknown,
)
from blrec.bili_upload.highlights import HighlightService
from blrec.bili_upload.policies import (
    RoomUploadPolicyManager,
    default_room_upload_policy,
)
from blrec.bili_upload.session_submission import SessionSubmissionManager
from blrec.bili_upload.upload import InvalidUploadPolicy, UploadCoordinator


class FakeUploader:
    def __init__(self, database: BiliUploadDatabase) -> None:
        self._database = database
        self.calls: List[int] = []

    async def upload_part(self, part_id: int, *, bundle: Any, claim: LeaseClaim) -> str:
        del bundle, claim
        existing = await self._database.fetchone(
            'SELECT upload_state,remote_filename FROM upload_parts WHERE id=?',
            (part_id,),
        )
        if (
            existing is not None
            and str(existing['upload_state']) == 'confirmed'
            and existing['remote_filename']
        ):
            return str(existing['remote_filename'])
        self.calls.append(part_id)
        remote = 'remote-{}'.format(part_id)
        await self._database.execute(
            "UPDATE upload_parts SET upload_state='confirmed',remote_filename=? "
            'WHERE id=?',
            (remote, part_id),
        )
        return remote


class RateLimitedUploader(FakeUploader):
    def __init__(self, database: BiliUploadDatabase) -> None:
        super().__init__(database)
        self.rate_limited = True

    async def upload_part(self, part_id: int, *, bundle: Any, claim: LeaseClaim) -> str:
        if self.rate_limited:
            self.rate_limited = False
            raise BiliApiError(406, operation='preupload', retry_after_seconds=240)
        return await super().upload_part(part_id, bundle=bundle, claim=claim)


class FakeProtocol:
    def __init__(self) -> None:
        self.submit_calls: List[Mapping[str, Any]] = []
        self.submit_error: Optional[BaseException] = None
        self.archive_entries: List[Mapping[str, Any]] = []
        self.archive_details: Dict[str, Mapping[str, Any]] = {}
        self.list_archive_calls = 0
        self.archive_view_calls: List[str] = []

    async def submit_archive(
        self, _bundle: Any, payload: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        self.submit_calls.append(payload)
        if self.submit_error is not None:
            raise self.submit_error
        return {'code': 0, 'data': {'aid': 303, 'bvid': 'BVfixture'}}

    async def list_archives(
        self, _bundle: Any, _params: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        self.list_archive_calls += 1
        return {'code': 0, 'data': {'arc_audits': self.archive_entries}}

    async def archive_view(
        self, _bundle: Any, params: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        bvid = str(params['bvid'])
        self.archive_view_calls.append(bvid)
        return self.archive_details[bvid]


class FakeCoverResolver:
    def __init__(self) -> None:
        self.custom_calls = []
        self.live_calls = []

    async def remote_url(self, asset_id: int, account_id: int) -> str:
        self.custom_calls.append((asset_id, account_id))
        return 'https://archive.biliimg.com/custom-{}-{}.jpg'.format(
            asset_id, account_id
        )

    async def live_url(
        self, account_id: int, *, local_path: Optional[str], source_url: str
    ) -> str:
        self.live_calls.append((account_id, local_path, source_url))
        return 'https://archive.biliimg.com/live.jpg'


class MutableClock:
    def __init__(self, now: int) -> None:
        self.now = now

    def __call__(self) -> float:
        return float(self.now)


async def seed_ready_session(
    database: BiliUploadDatabase,
    tmp_path: Path,
    *,
    now: int = 1000,
    stable: bool = True,
    auto_comment: bool = False,
    danmaku_backfill: bool = False,
    part_title_template: str = 'P{{ part_index }}',
    dynamic_template: str = '{{ title }}｜{{ anchor_name }}',
    tags_template: str = '直播,录播',
    creation_statement_id: int = -1,
    original_authorization: bool = True,
    source_template: str = '',
    is_only_self: bool = False,
    publish_dynamic: bool = True,
    up_selection_reply: bool = False,
    up_close_reply: bool = False,
    up_close_danmu: bool = False,
    collection_season_id: Optional[int] = None,
    collection_section_id: Optional[int] = None,
    cover_mode: str = 'live',
    cover_asset_id: Optional[int] = None,
    publish_delay_seconds: int = 0,
    cover_path: Optional[str] = None,
) -> List[Path]:
    copyright_value = (
        2 if creation_statement_id == -2 else 1 if original_authorization else 3
    )
    no_reprint = original_authorization and creation_statement_id != -2
    await database.execute(
        "INSERT INTO bili_accounts("
        "id,uid,display_name,credential_ciphertext,credential_version,key_id,"
        "state,created_at,updated_at) "
        "VALUES(1,42,'投稿账号',X'00',3,'k','active',1,1)"
    )
    if cover_asset_id is not None:
        await database.execute(
            'INSERT INTO cover_assets('
            'id,sha256,storage_path,filename,mime_type,width,height,byte_size,'
            'created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)',
            (
                cover_asset_id,
                'a' * 64,
                str(tmp_path / 'cover.jpg'),
                'cover.jpg',
                'image/jpeg',
                1600,
                1000,
                1,
                1,
                1,
            ),
        )
    await database.execute(
        'INSERT INTO bili_account_selection(id,primary_account_id) VALUES(1,1)'
    )
    await database.execute(
        'INSERT INTO room_upload_policies('
        'room_id,account_mode,account_id,enabled,title_template,'
        'description_template,part_title_template,dynamic_template,tid,tags,'
        'creation_statement_id,original_authorization,copyright,source,'
        'is_only_self,publish_dynamic,no_reprint,'
        'up_selection_reply,up_close_reply,up_close_danmu,auto_comment,'
        'danmaku_backfill,filter_json,created_at,updated_at,'
        'collection_season_id,collection_section_id,cover_mode,cover_asset_id,'
        'publish_delay_seconds) '
        "VALUES(100,'primary',NULL,1,'{{ title }} 录播',"
        "'主播 {{ anchor_name }}',?,?,17,?,?,?,?,?,?,?,?,?,?,?,?,?,'{}',1,1,"
        '?,?,?,?,?)',
        (
            part_title_template,
            dynamic_template,
            tags_template,
            creation_statement_id,
            int(original_authorization),
            copyright_value,
            source_template,
            int(is_only_self),
            int(publish_dynamic),
            int(no_reprint),
            int(up_selection_reply),
            int(up_close_reply),
            int(up_close_danmu),
            int(auto_comment),
            int(danmaku_backfill),
            collection_season_id,
            collection_section_id,
            cover_mode,
            cover_asset_id,
            publish_delay_seconds,
        ),
    )
    await database.execute(
        'INSERT INTO recording_sessions('
        'id,room_id,broadcast_session_key,live_start_time,state,started_at,'
        'ended_at,title,cover_url,cover_path,anchor_uid,anchor_name,area_id,area_name,'
        'parent_area_id,parent_area_name,live_end_time,upload_intent) '
        "VALUES(1,100,'100:800',800,'closed',800,900,'测试直播',"
        "'https://i0.hdslb.com/cover.jpg',?,42,'测试主播',17,'单机游戏',"
        "1,'游戏',900,'auto')",
        (cover_path,),
    )
    await database.execute(
        "INSERT INTO recording_runs(id,session_id,state,started_at,ended_at) "
        "VALUES('run',1,'finished',800,900)"
    )
    paths = []
    for index in (1, 2):
        path = tmp_path / 'part-{}.flv'.format(index)
        path.write_bytes(('part-{}'.format(index)).encode('ascii'))
        mtime = now - (60 if stable else 10)
        os.utime(str(path), (mtime, mtime))
        paths.append(path)
        await database.execute(
            'INSERT INTO recording_parts('
            'id,session_id,run_id,part_index,source_path,final_path,'
            'record_start_time,record_end_time,record_duration_seconds,'
            'file_size_bytes,danmaku_count,artifact_state,created_at,updated_at,'
            'media_index_state) '
            "VALUES(?,1,'run',?,?,?,?,?,?,?,?,'ready',800,900,'ready')",
            (
                index,
                index,
                str(path),
                str(path),
                800 + index,
                850 + index,
                120,
                path.stat().st_size,
                index * 10,
            ),
        )
    return paths


def coordinator(
    database: BiliUploadDatabase,
    protocol: FakeProtocol,
    uploader: FakeUploader,
    clock: MutableClock,
    *,
    cover_resolver: Optional[FakeCoverResolver] = None,
    account_ids: Tuple[int, ...] = (1,),
    artifact_probe=None,
    stop_requested=lambda: False,
    archive_reader: Optional[ArchiveReadService] = None,
    read_timeout_seconds: float = 60,
) -> UploadCoordinator:
    async def load_bundle(account_id: int) -> Any:
        assert account_id in account_ids
        return object()

    return UploadCoordinator(
        database,
        protocol,
        uploader,
        bundle_loader=load_bundle,
        account_gates=AccountWriteGate(database),
        cover_resolver=cover_resolver or FakeCoverResolver(),
        worker_id='test-worker',
        clock=clock,
        stop_requested=stop_requested,
        archive_reader=archive_reader or ArchiveReadService(protocol),
        read_timeout_seconds=read_timeout_seconds,
        artifact_probe=artifact_probe
        or (lambda path: RecoveredArtifact(path, os.path.getsize(path), 120)),
    )


async def make_session_open_with_one_closed_part(database: BiliUploadDatabase) -> None:
    await database.execute(
        "UPDATE recording_sessions SET state='open',ended_at=NULL,"
        'live_end_time=NULL WHERE id=1'
    )
    await database.execute(
        "UPDATE recording_runs SET state='recording',ended_at=NULL WHERE id='run'"
    )
    await database.execute(
        "UPDATE recording_parts SET artifact_state='recording',final_path=NULL,"
        'record_end_time=NULL,record_duration_seconds=NULL WHERE id=2'
    )


async def seed_ready_highlight(
    database: BiliUploadDatabase, tmp_path: Path, *, clip_id: int
) -> int:
    output_directory = tmp_path / 'highlights' / '100'
    output_directory.mkdir(parents=True, exist_ok=True)
    video = output_directory / 'highlight-{}.mp4'.format(clip_id)
    xml = output_directory / 'highlight-{}.xml'.format(clip_id)
    video.write_bytes(b'highlight-video')
    xml.write_text('<i/>', encoding='utf8')
    await database.execute(
        'INSERT INTO highlight_clips('
        'id,room_id,source_session_id,name,requested_start_ms,requested_end_ms,'
        'actual_start_ms,actual_end_ms,output_video_path,output_xml_path,state,'
        'created_at,updated_at) '
        "VALUES(?,100,1,?,0,10000,0,10000,?,?,'ready',1000,1000)",
        (clip_id, '高光 {}'.format(clip_id), str(video), str(xml)),
    )
    return await HighlightService(
        database, recording_root=tmp_path
    ).ensure_upload_session(clip_id)


@pytest.mark.asyncio
async def test_highlight_upload_requires_explicitly_saved_submission_settings(
    tmp_path: Path,
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'upload.sqlite3'))
    await database.open()
    try:
        await seed_ready_session(database, tmp_path)
        session_id = await seed_ready_highlight(database, tmp_path, clip_id=1)
        worker = coordinator(
            database, FakeProtocol(), FakeUploader(database), MutableClock(1000)
        )

        with pytest.raises(InvalidUploadPolicy, match='settings must be saved'):
            await worker.create_highlight_job(session_id)

        assert await database.scalar('SELECT COUNT(*) FROM upload_jobs') == 0
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_highlight_upload_rejects_clip_after_deletion_has_started(
    tmp_path: Path,
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'upload.sqlite3'))
    await database.open()
    try:
        await seed_ready_session(database, tmp_path)
        session_id = await seed_ready_highlight(database, tmp_path, clip_id=1)
        await SessionSubmissionManager(
            database,
            policy_manager=RoomUploadPolicyManager(database),
            clock=MutableClock(1000),
        ).save_override(
            session_id, default_room_upload_policy(), manager_subject='administrator'
        )
        await database.execute(
            "UPDATE highlight_clips SET state='cancelled' WHERE upload_session_id=?",
            (session_id,),
        )
        worker = coordinator(
            database, FakeProtocol(), FakeUploader(database), MutableClock(1000)
        )

        with pytest.raises(InvalidUploadPolicy, match='could not be created'):
            await worker.create_highlight_job(session_id)

        assert await database.scalar('SELECT COUNT(*) FROM upload_jobs') == 0
    finally:
        await database.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('fence_owner', ('session', 'clip'))
async def test_highlight_job_creation_rechecks_deletion_fence_in_transaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fence_owner: str
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'upload.sqlite3'))
    await database.open()
    create_waiting = asyncio.Event()
    release_create = asyncio.Event()
    try:
        await seed_ready_session(database, tmp_path)
        session_id = await seed_ready_highlight(database, tmp_path, clip_id=1)
        await SessionSubmissionManager(
            database,
            policy_manager=RoomUploadPolicyManager(database),
            clock=MutableClock(1000),
        ).save_override(
            session_id, default_room_upload_policy(), manager_subject='administrator'
        )
        worker = coordinator(
            database, FakeProtocol(), FakeUploader(database), MutableClock(1000)
        )
        original_write = database.write

        async def gated_write(operation):
            if getattr(operation, '__name__', '') == 'create':
                create_waiting.set()
                await release_create.wait()
            return await original_write(operation)

        monkeypatch.setattr(database, 'write', gated_write)
        create_task = asyncio.create_task(worker.create_highlight_job(session_id))
        await asyncio.wait_for(create_waiting.wait(), timeout=0.5)
        if fence_owner == 'session':
            await database.execute(
                "UPDATE recording_sessions SET deletion_state='requested',"
                'cancellation_generation=1,deletion_requested_at=1 WHERE id=?',
                (session_id,),
            )
        else:
            await database.execute(
                "UPDATE highlight_clips SET deletion_state='requested',"
                'cancellation_generation=1,deletion_requested_at=1 '
                'WHERE upload_session_id=?',
                (session_id,),
            )
        release_create.set()

        with pytest.raises(InvalidUploadPolicy, match='could not be created'):
            await create_task
        assert await database.scalar('SELECT COUNT(*) FROM upload_jobs') == 0
    finally:
        release_create.set()
        if 'create_task' in locals():
            await asyncio.gather(create_task, return_exceptions=True)
        await database.close()


@pytest.mark.asyncio
async def test_highlight_creates_ready_single_part_jobs_with_session_policy(
    tmp_path: Path,
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'upload.sqlite3'))
    await database.open()
    try:
        original_paths = await seed_ready_session(database, tmp_path)
        await database.execute(
            "UPDATE recording_sessions SET upload_decision='skip' WHERE id=1"
        )
        clock = MutableClock(1000)
        worker = coordinator(database, FakeProtocol(), FakeUploader(database), clock)

        first_session_id = await seed_ready_highlight(database, tmp_path, clip_id=1)
        override = replace(
            default_room_upload_policy(),
            title_template='{{ title }} 精选',
            part_title_template='片段 {{ part_index }}',
            tid=122,
            tags='片段,高光',
            collection_season_id=20,
            collection_section_id=21,
        )
        await SessionSubmissionManager(
            database, policy_manager=RoomUploadPolicyManager(database), clock=clock
        ).save_override(first_session_id, override, manager_subject='administrator')
        assert await worker.create_ready_jobs() == []
        first_job_id = await worker.create_highlight_job(first_session_id)
        assert await worker.create_highlight_job(first_session_id) == first_job_id
        first = await database.fetchone(
            'SELECT job.state,job.operator_paused,job.operator_resume_state,'
            'session.source_kind FROM upload_jobs job '
            'JOIN recording_sessions session ON session.id=job.session_id '
            'WHERE job.id=?',
            (first_job_id,),
        )
        assert first is not None
        assert dict(first) == {
            'state': 'ready',
            'operator_paused': 0,
            'operator_resume_state': None,
            'source_kind': 'highlight',
        }
        first_snapshot = json.loads(
            str(
                await database.scalar(
                    'SELECT policy_snapshot_json FROM upload_jobs WHERE id=?',
                    (first_job_id,),
                )
            )
        )
        assert first_snapshot['title'] == '高光 1 精选'
        assert first_snapshot['tid'] == 122
        assert first_snapshot['collection_season_id'] == 20
        assert first_snapshot['collection_section_id'] == 21
        first_parts = await database.fetchall(
            'SELECT part.part_index,part.artifact_state,part.source_path,'
            'part.final_path,part.xml_path FROM recording_parts part '
            'WHERE part.session_id=?',
            (first_session_id,),
        )
        assert len(first_parts) == 1
        assert first_parts[0]['part_index'] == 1
        assert first_parts[0]['artifact_state'] == 'ready'
        assert first_parts[0]['source_path'] == first_parts[0]['final_path']

        await database.execute('DELETE FROM room_upload_policies WHERE room_id=100')
        second_session_id = await seed_ready_highlight(database, tmp_path, clip_id=2)
        with pytest.raises(InvalidUploadPolicy, match='settings must be saved'):
            await worker.create_highlight_job(second_session_id)
        assert (
            await database.scalar(
                'SELECT COUNT(*) FROM room_upload_policies WHERE room_id=100'
            )
            == 0
        )
        assert (
            await database.scalar(
                "SELECT COUNT(*) FROM recording_parts WHERE session_id=1"
            )
            == 2
        )
        assert all(path.exists() for path in original_paths)
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_create_ready_job_locks_account_policy_and_part_order(
    tmp_path: Path,
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'upload.sqlite3'))
    await database.open()
    try:
        await seed_ready_session(database, tmp_path)
        clock = MutableClock(1000)
        worker = coordinator(database, FakeProtocol(), FakeUploader(database), clock)

        created = await worker.create_ready_jobs()
        duplicate = await worker.create_ready_jobs()

        assert created == [1]
        assert duplicate == []
        job = await database.fetchone(
            'SELECT account_id,policy_snapshot_json,state,submit_state '
            'FROM upload_jobs WHERE id=1'
        )
        assert job is not None
        snapshot = json.loads(str(job['policy_snapshot_json']))
        assert int(job['account_id']) == 1
        assert str(job['state']) == 'ready'
        assert str(job['submit_state']) == 'prepared'
        assert snapshot['account_id'] == 1
        assert snapshot['account_credential_version_at_creation'] == 3
        assert snapshot['format_version'] == 4
        assert snapshot['title'] == '测试直播 录播'
        assert snapshot['description'] == '主播 测试主播'
        assert snapshot['dynamic'] == '测试直播｜测试主播'
        assert snapshot['creation_statement_id'] == -1
        assert snapshot['original_authorization'] is True
        assert snapshot['publish_dynamic'] is True
        assert snapshot['cover_mode'] == 'live'
        assert snapshot['publish_delay_seconds'] == 0
        assert snapshot['part_titles'] == ['P1', 'P2']
        parts = await database.fetchall(
            'SELECT part_index,artifact_state,upload_state,file_identity '
            'FROM upload_parts WHERE job_id=1 ORDER BY part_index'
        )
        assert [int(part['part_index']) for part in parts] == [1, 2]
        assert all(str(part['artifact_state']) == 'ready' for part in parts)
        assert all(str(part['upload_state']) == 'prepared' for part in parts)
        assert all(part['file_identity'] for part in parts)
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_short_recording_parts_are_excluded_before_upload(tmp_path: Path) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'upload.sqlite3'))
    await database.open()
    try:
        paths = await seed_ready_session(database, tmp_path)
        await database.execute(
            'UPDATE recording_parts SET record_duration_seconds=59 WHERE id=1'
        )
        protocol = FakeProtocol()
        uploader = FakeUploader(database)
        worker = coordinator(
            database,
            protocol,
            uploader,
            MutableClock(1000),
            artifact_probe=lambda path: RecoveredArtifact(
                path, os.path.getsize(path), 59 if path == str(paths[0]) else 120
            ),
        )

        assert await worker.create_ready_jobs() == [1]
        assert await worker.run_once() == 1

        assert uploader.calls == [1]
        assert (
            await database.scalar('SELECT part_index FROM upload_parts WHERE job_id=1')
            == 2
        )
        assert [video['title'] for video in protocol.submit_calls[0]['videos']] == [
            'P2'
        ]
        assert (
            await database.scalar('SELECT COUNT(*) FROM upload_parts WHERE job_id=1')
            == 1
        )
        assert (
            await database.scalar(
                'SELECT upload_excluded_reason FROM recording_parts WHERE id=1'
            )
            == '录像不足 60 秒，已保留本地文件但不投稿'
        )
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_short_part_filter_uses_probed_media_duration(tmp_path: Path) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'upload.sqlite3'))
    await database.open()
    try:
        paths = await seed_ready_session(database, tmp_path)

        def probe(path: str) -> RecoveredArtifact:
            duration = 5 if path == str(paths[0]) else 120
            return RecoveredArtifact(path, os.path.getsize(path), duration)

        protocol = FakeProtocol()
        uploader = FakeUploader(database)
        worker = coordinator(
            database, protocol, uploader, MutableClock(1000), artifact_probe=probe
        )

        assert await worker.create_ready_jobs() == [1]
        assert await worker.run_once() == 1

        assert uploader.calls == [1]
        assert (
            await database.scalar(
                'SELECT record_duration_seconds FROM recording_parts WHERE id=1'
            )
            == 5
        )
        assert (
            await database.scalar('SELECT part_index FROM upload_parts WHERE job_id=1')
            == 2
        )
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_failed_media_probe_does_not_fail_open_to_wall_clock_duration(
    tmp_path: Path,
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'upload.sqlite3'))
    await database.open()
    try:
        paths = await seed_ready_session(database, tmp_path)
        first_part_available = False

        def probe(path: str) -> Optional[RecoveredArtifact]:
            if path == str(paths[0]) and not first_part_available:
                return None
            return RecoveredArtifact(path, os.path.getsize(path), 120)

        clock = MutableClock(1000)
        worker = coordinator(
            database,
            FakeProtocol(),
            FakeUploader(database),
            clock,
            artifact_probe=probe,
        )

        assert await worker.create_ready_jobs() == []
        assert await database.scalar('SELECT COUNT(*) FROM upload_jobs') == 0
        assert (
            await database.scalar(
                'SELECT upload_excluded_reason FROM recording_parts WHERE id=1'
            )
            == '录像媒体信息暂时无法读取，等待重新校验'
        )

        first_part_available = True
        clock.now += 60

        assert await worker.create_ready_jobs() == [1]
        parts = await database.fetchall(
            'SELECT part_index FROM upload_parts WHERE job_id=1 ORDER BY part_index'
        )
        assert [int(part['part_index']) for part in parts] == [1, 2]
        assert (
            await database.scalar(
                'SELECT upload_excluded_reason FROM recording_parts WHERE id=1'
            )
            is None
        )
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_permanently_unreadable_media_is_isolated_without_blocking_other_parts(
    tmp_path: Path,
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'upload.sqlite3'))
    await database.open()
    try:
        paths = await seed_ready_session(database, tmp_path)

        def probe(path: str) -> Optional[RecoveredArtifact]:
            if path == str(paths[0]):
                return None
            return RecoveredArtifact(path, os.path.getsize(path), 120)

        clock = MutableClock(1000)
        for delay in (60, 120, 240, 480):
            worker = coordinator(
                database,
                FakeProtocol(),
                FakeUploader(database),
                clock,
                artifact_probe=probe,
            )
            assert await worker.create_ready_jobs() == []
            clock.now += delay

        worker = coordinator(
            database,
            FakeProtocol(),
            FakeUploader(database),
            clock,
            artifact_probe=probe,
        )
        assert await worker.create_ready_jobs() == [1]
        parts = await database.fetchall(
            'SELECT part_index FROM upload_parts WHERE job_id=1 ORDER BY part_index'
        )
        assert [int(part['part_index']) for part in parts] == [2]
        assert (
            await database.scalar(
                'SELECT upload_excluded_reason FROM recording_parts WHERE id=1'
            )
            == '录像媒体信息连续校验失败，已排除自动投稿'
        )
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_session_with_only_short_parts_finishes_without_manual_action(
    tmp_path: Path,
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'upload.sqlite3'))
    await database.open()
    try:
        await seed_ready_session(database, tmp_path)
        await database.execute('UPDATE recording_parts SET record_duration_seconds=10')
        worker = coordinator(
            database,
            FakeProtocol(),
            FakeUploader(database),
            MutableClock(1000),
            artifact_probe=lambda path: RecoveredArtifact(
                path, os.path.getsize(path), 10
            ),
        )

        assert await worker.create_ready_jobs() == []
        session = await database.fetchone(
            'SELECT upload_resolution_state,upload_resolution_error '
            'FROM recording_sessions WHERE id=1'
        )
        assert session is not None
        assert dict(session) == {
            'upload_resolution_state': 'not_requested',
            'upload_resolution_error': '录像分段均不足 60 秒，已保留本地文件',
        }
        assert await database.scalar('SELECT COUNT(*) FROM upload_jobs') == 0
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_finalized_job_with_only_short_parts_cancels_itself(
    tmp_path: Path,
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'upload.sqlite3'))
    await database.open()
    try:
        await seed_ready_session(database, tmp_path)
        await database.execute('UPDATE recording_parts SET record_duration_seconds=10')
        await database.execute(
            "UPDATE recording_sessions SET upload_resolution_state='job_created' "
            'WHERE id=1'
        )
        await database.execute(
            'INSERT INTO upload_jobs('
            'id,session_id,account_id,policy_snapshot_json,state,submit_state,'
            'preupload_finalized,created_at,updated_at) '
            "VALUES(1,1,1,'{}','waiting_artifacts','prepared',1,1,1)"
        )
        worker = coordinator(
            database,
            FakeProtocol(),
            FakeUploader(database),
            MutableClock(1000),
            artifact_probe=lambda path: RecoveredArtifact(
                path, os.path.getsize(path), 10
            ),
        )

        assert await worker.prepare_waiting_jobs() == [1]
        assert await database.scalar('SELECT COUNT(*) FROM upload_jobs') == 0
        session = await database.fetchone(
            'SELECT upload_resolution_state,upload_resolution_error '
            'FROM recording_sessions WHERE id=1'
        )
        assert session is not None
        assert dict(session) == {
            'upload_resolution_state': 'not_requested',
            'upload_resolution_error': '录像分段均不足 60 秒，已保留本地文件',
        }
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_preupload_rate_limit_waits_and_retries_without_pausing(
    tmp_path: Path,
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'upload.sqlite3'))
    await database.open()
    try:
        await seed_ready_session(database, tmp_path)
        clock = MutableClock(1000)
        protocol = FakeProtocol()
        uploader = RateLimitedUploader(database)
        worker = coordinator(database, protocol, uploader, clock)
        await worker.create_ready_jobs()

        assert await worker.run_once() == 1
        waiting = await database.fetchone(
            'SELECT state,submit_state,next_attempt_at,review_reason,lease_owner '
            'FROM upload_jobs WHERE id=1'
        )
        assert waiting is not None
        assert waiting['state'] == 'uploading'
        assert waiting['submit_state'] == 'prepared'
        assert int(waiting['next_attempt_at']) == 1240
        assert waiting['lease_owner'] is None
        assert '自动重试' in str(waiting['review_reason'])
        assert protocol.submit_calls == []

        assert await worker.run_once() is None
        clock.now = int(waiting['next_attempt_at'])
        assert await worker.run_once() == 1
        assert len(protocol.submit_calls) == 1
        assert await database.scalar('SELECT state FROM upload_jobs WHERE id=1') == (
            'waiting_review'
        )
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_open_session_preuploads_closed_part_without_submitting(
    tmp_path: Path,
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'upload.sqlite3'))
    await database.open()
    try:
        await seed_ready_session(database, tmp_path)
        await make_session_open_with_one_closed_part(database)
        protocol = FakeProtocol()
        uploader = FakeUploader(database)
        worker = coordinator(database, protocol, uploader, MutableClock(1000))

        assert await worker.sync_live_sessions() == [1]
        assert await worker.prepare_waiting_jobs() == [1]
        job = await database.fetchone(
            'SELECT state,preupload_finalized FROM upload_jobs WHERE id=1'
        )
        assert job is not None
        assert dict(job) == {'state': 'ready', 'preupload_finalized': 0}
        assert await database.scalar('SELECT COUNT(*) FROM upload_parts') == 1

        assert await worker.run_once() == 1

        assert uploader.calls == [1]
        assert protocol.submit_calls == []
        completed = await database.fetchone(
            'SELECT state,submit_state,preupload_finalized,upload_completed_at '
            'FROM upload_jobs WHERE id=1'
        )
        assert completed is not None
        assert dict(completed) == {
            'state': 'waiting_artifacts',
            'submit_state': 'prepared',
            'preupload_finalized': 0,
            'upload_completed_at': None,
        }
        assert (
            await database.scalar(
                'SELECT remote_filename FROM upload_parts WHERE part_index=1'
            )
            == 'remote-1'
        )
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_preupload_appends_only_new_part_and_submits_latest_snapshot(
    tmp_path: Path,
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'upload.sqlite3'))
    await database.open()
    try:
        paths = await seed_ready_session(database, tmp_path)
        await make_session_open_with_one_closed_part(database)
        protocol = FakeProtocol()
        uploader = FakeUploader(database)
        worker = coordinator(database, protocol, uploader, MutableClock(1000))

        await worker.sync_live_sessions()
        await worker.prepare_waiting_jobs()
        await worker.run_once()

        await database.execute(
            "UPDATE recording_parts SET artifact_state='ready',final_path=?,"
            'record_end_time=950,record_duration_seconds=100 WHERE id=2',
            (str(paths[1]),),
        )
        assert await worker.prepare_waiting_jobs() == [1]
        await worker.run_once()
        assert uploader.calls == [1, 2]
        assert protocol.submit_calls == []

        await database.execute(
            "UPDATE room_upload_policies SET title_template='最终 {{ title }}',"
            'updated_at=2 WHERE room_id=100'
        )
        await database.execute(
            "UPDATE recording_sessions SET state='closed',ended_at=960,"
            'live_end_time=960 WHERE id=1'
        )
        assert await worker.sync_live_sessions() == [1]
        finalized = await database.fetchone(
            'SELECT state,preupload_finalized,policy_snapshot_json '
            'FROM upload_jobs WHERE id=1'
        )
        assert finalized is not None
        assert finalized['state'] == 'ready'
        assert finalized['preupload_finalized'] == 1
        assert json.loads(str(finalized['policy_snapshot_json']))['title'] == (
            '最终 测试直播'
        )

        await worker.run_once()
        assert uploader.calls == [1, 2]
        assert len(protocol.submit_calls) == 1
        assert protocol.submit_calls[0]['title'] == '最终 测试直播'
        assert [video['filename'] for video in protocol.submit_calls[0]['videos']] == [
            'remote-1',
            'remote-2',
        ]
        assert await worker.run_once() is None
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_preupload_restart_reuses_confirmed_part(tmp_path: Path) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'upload.sqlite3'))
    await database.open()
    try:
        paths = await seed_ready_session(database, tmp_path)
        await make_session_open_with_one_closed_part(database)
        protocol = FakeProtocol()
        first_uploader = FakeUploader(database)
        first = coordinator(database, protocol, first_uploader, MutableClock(1000))
        await first.sync_live_sessions()
        await first.prepare_waiting_jobs()
        await first.run_once()
        assert first_uploader.calls == [1]

        await database.execute(
            "UPDATE recording_parts SET artifact_state='ready',final_path=?,"
            'record_end_time=950,record_duration_seconds=100 WHERE id=2',
            (str(paths[1]),),
        )
        second_uploader = FakeUploader(database)
        restarted = coordinator(database, protocol, second_uploader, MutableClock(1000))
        await restarted.sync_live_sessions()
        await restarted.prepare_waiting_jobs()
        await restarted.run_once()

        assert second_uploader.calls == [2]
        assert protocol.submit_calls == []
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_finalized_preupload_advances_after_pending_tail_fails(
    tmp_path: Path,
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'upload.sqlite3'))
    await database.open()
    try:
        await seed_ready_session(database, tmp_path)
        await make_session_open_with_one_closed_part(database)
        protocol = FakeProtocol()
        worker = coordinator(
            database, protocol, FakeUploader(database), MutableClock(1000)
        )
        await worker.sync_live_sessions()
        await worker.prepare_waiting_jobs()
        await worker.run_once()

        await database.execute(
            "UPDATE recording_sessions SET state='closed',ended_at=960,"
            'live_end_time=960 WHERE id=1'
        )
        await worker.sync_live_sessions()
        assert (
            await database.scalar('SELECT state FROM upload_jobs WHERE id=1')
            == 'waiting_artifacts'
        )

        await database.execute(
            "UPDATE recording_parts SET artifact_state='failed',final_path=NULL,"
            "error_message='尾部分 P 处理失败' WHERE id=2"
        )

        assert await worker.prepare_waiting_jobs() == [1]
        assert await database.scalar('SELECT state FROM upload_jobs WHERE id=1') == (
            'ready'
        )
        await worker.run_once()
        assert len(protocol.submit_calls) == 1
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_invalid_final_preupload_settings_pause_with_actionable_error(
    tmp_path: Path,
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'upload.sqlite3'))
    await database.open()
    try:
        await seed_ready_session(database, tmp_path)
        await make_session_open_with_one_closed_part(database)
        worker = coordinator(
            database, FakeProtocol(), FakeUploader(database), MutableClock(1000)
        )
        await worker.sync_live_sessions()
        await database.execute(
            'UPDATE room_upload_policies SET title_template=?,updated_at=2 '
            'WHERE room_id=100',
            ('x' * 81,),
        )
        await database.execute(
            "UPDATE recording_parts SET artifact_state='failed',final_path=NULL "
            'WHERE id=2'
        )
        await database.execute(
            "UPDATE recording_sessions SET state='closed',ended_at=960,"
            'live_end_time=960 WHERE id=1'
        )

        await worker.sync_live_sessions()

        job = await database.fetchone(
            'SELECT state,preupload_finalized,review_reason '
            'FROM upload_jobs WHERE id=1'
        )
        assert job is not None
        assert dict(job) == {
            'state': 'paused',
            'preupload_finalized': 0,
            'review_reason': '投稿设置无法生成稿件，请检查标题、分区和标签',
        }
        session = await database.fetchone(
            'SELECT upload_resolution_state,upload_resolution_error '
            'FROM recording_sessions WHERE id=1'
        )
        assert session is not None
        assert dict(session) == {
            'upload_resolution_state': 'configuration_required',
            'upload_resolution_error': ('投稿设置无法生成稿件，请检查标题、分区和标签'),
        }

        await database.execute(
            "UPDATE recording_sessions SET upload_resolution_state='pending',"
            'upload_resolution_error=NULL WHERE id=1'
        )
        await worker.sync_live_sessions()
        assert (
            await database.scalar(
                'SELECT upload_resolution_state FROM recording_sessions WHERE id=1'
            )
            == 'configuration_required'
        )
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_disabling_upload_cancels_preupload_without_deleting_recording(
    tmp_path: Path,
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'upload.sqlite3'))
    await database.open()
    try:
        paths = await seed_ready_session(database, tmp_path)
        await make_session_open_with_one_closed_part(database)
        worker = coordinator(
            database, FakeProtocol(), FakeUploader(database), MutableClock(1000)
        )
        await worker.sync_live_sessions()
        await worker.prepare_waiting_jobs()
        await worker.run_once()

        await database.execute(
            "UPDATE recording_sessions SET upload_decision='skip',"
            "upload_resolution_state='pending' WHERE id=1"
        )
        await worker.sync_live_sessions()

        assert await database.scalar('SELECT COUNT(*) FROM upload_jobs') == 0
        assert await database.scalar('SELECT COUNT(*) FROM recording_parts') == 2
        assert all(path.exists() for path in paths)
        assert (
            await database.scalar(
                'SELECT upload_resolution_state FROM recording_sessions WHERE id=1'
            )
            == 'not_requested'
        )
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_reenabling_room_policy_recreates_cancelled_live_preupload(
    tmp_path: Path,
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'upload.sqlite3'))
    await database.open()
    try:
        await seed_ready_session(database, tmp_path)
        await make_session_open_with_one_closed_part(database)
        worker = coordinator(
            database, FakeProtocol(), FakeUploader(database), MutableClock(1000)
        )
        await worker.sync_live_sessions()

        await database.execute(
            'UPDATE room_upload_policies SET enabled=0,updated_at=2 WHERE room_id=100'
        )
        await worker.sync_live_sessions()
        assert await database.scalar('SELECT COUNT(*) FROM upload_jobs') == 0
        assert (
            await database.scalar(
                'SELECT upload_resolution_state FROM recording_sessions WHERE id=1'
            )
            == 'not_requested'
        )

        await database.execute(
            'UPDATE room_upload_policies SET enabled=1,updated_at=3 WHERE room_id=100'
        )

        assert len(await worker.sync_live_sessions()) == 1
        recreated = await database.fetchone(
            'SELECT preupload_finalized FROM upload_jobs WHERE session_id=1'
        )
        assert recreated is not None
        assert recreated['preupload_finalized'] == 0
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_final_account_change_reuploads_preuploaded_parts(tmp_path: Path) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'upload.sqlite3'))
    await database.open()
    try:
        await seed_ready_session(database, tmp_path)
        await make_session_open_with_one_closed_part(database)
        await database.execute(
            "INSERT INTO bili_accounts("
            "id,uid,display_name,credential_ciphertext,credential_version,key_id,"
            "state,created_at,updated_at) "
            "VALUES(2,84,'新投稿账号',X'00',1,'k','active',1,1)"
        )
        protocol = FakeProtocol()
        uploader = FakeUploader(database)
        worker = coordinator(
            database, protocol, uploader, MutableClock(1000), account_ids=(1, 2)
        )
        await worker.sync_live_sessions()
        await worker.prepare_waiting_jobs()
        await worker.run_once()
        await database.execute(
            "INSERT INTO upload_chunks(part_id,chunk_no,offset,size,etag,state) "
            "VALUES(1,0,0,6,'etag','confirmed')"
        )

        await database.execute(
            "UPDATE room_upload_policies SET account_mode='fixed',account_id=2,"
            'updated_at=2 WHERE room_id=100'
        )
        await database.execute(
            "UPDATE recording_parts SET artifact_state='failed',final_path=NULL "
            'WHERE id=2'
        )
        await database.execute(
            "UPDATE recording_sessions SET state='closed',ended_at=960,"
            'live_end_time=960 WHERE id=1'
        )

        await worker.sync_live_sessions()

        finalized = await database.fetchone(
            'SELECT account_id,preupload_finalized,state FROM upload_jobs WHERE id=1'
        )
        assert finalized is not None
        assert dict(finalized) == {
            'account_id': 2,
            'preupload_finalized': 1,
            'state': 'ready',
        }
        assert await database.scalar('SELECT COUNT(*) FROM upload_chunks') == 0
        assert (
            await database.scalar('SELECT remote_filename FROM upload_parts WHERE id=1')
            is None
        )

        await worker.run_once()
        assert uploader.calls == [1, 1]
        assert len(protocol.submit_calls) == 1
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_finished_session_reads_current_room_policy_before_creating_job(
    tmp_path: Path,
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'upload.sqlite3'))
    await database.open()
    try:
        await seed_ready_session(database, tmp_path)
        await database.execute(
            'UPDATE room_upload_policies SET enabled=0 WHERE room_id=100'
        )
        protocol = FakeProtocol()
        worker = coordinator(
            database, protocol, FakeUploader(database), MutableClock(1000)
        )

        assert await worker.resolve_finished_sessions() == []
        assert await database.scalar('SELECT COUNT(*) FROM upload_jobs') == 0
        assert (
            await database.scalar(
                'SELECT upload_resolution_state FROM recording_sessions WHERE id=1'
            )
            == 'not_requested'
        )
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_finished_session_creates_waiting_job_before_artifacts_are_ready(
    tmp_path: Path,
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'upload.sqlite3'))
    await database.open()
    try:
        await seed_ready_session(database, tmp_path)
        await database.execute("UPDATE recording_sessions SET state='open' WHERE id=1")
        await database.execute(
            "UPDATE recording_parts SET artifact_state='postprocessing',"
            'final_path=NULL WHERE session_id=1'
        )
        worker = coordinator(
            database, FakeProtocol(), FakeUploader(database), MutableClock(1000)
        )

        assert await worker.resolve_finished_sessions() == [1]
        assert (
            await database.scalar('SELECT state FROM upload_jobs WHERE id=1')
            == 'waiting_artifacts'
        )
        assert await worker.prepare_waiting_jobs() == []

        for part_id, path in enumerate(
            (tmp_path / 'part-1.flv', tmp_path / 'part-2.flv'), start=1
        ):
            await database.execute(
                "UPDATE recording_parts SET artifact_state='ready',final_path=? "
                'WHERE id=?',
                (str(path), part_id),
            )
        await database.execute(
            "UPDATE recording_sessions SET state='closed' WHERE id=1"
        )

        assert await worker.prepare_waiting_jobs() == [1]
        assert await worker.prepare_waiting_jobs() == []
        assert (
            await database.scalar('SELECT state FROM upload_jobs WHERE id=1') == 'ready'
        )
        assert await database.scalar('SELECT COUNT(*) FROM upload_parts') == 2
    finally:
        await database.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('media_index_state', ('pending', 'indexing'))
async def test_finished_session_waits_for_media_index_before_snapshotting_parts(
    tmp_path: Path, media_index_state: str
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'upload.sqlite3'))
    await database.open()
    try:
        paths = await seed_ready_session(database, tmp_path)
        await database.execute(
            'UPDATE recording_parts SET media_index_state=?', (media_index_state,)
        )
        worker = coordinator(
            database, FakeProtocol(), FakeUploader(database), MutableClock(1000)
        )

        assert await worker.resolve_finished_sessions() == [1]
        assert await worker.prepare_waiting_jobs() == []
        assert (
            await database.scalar('SELECT state FROM upload_jobs WHERE id=1')
            == 'waiting_artifacts'
        )
        assert await database.scalar('SELECT COUNT(*) FROM upload_parts') == 0

        paths[0].write_bytes(b'rebuilt-final-file')
        os.utime(str(paths[0]), (900, 900))
        await database.execute(
            "UPDATE recording_parts SET media_index_state='ready',updated_at=901"
        )

        assert await worker.prepare_waiting_jobs() == [1]
        stored = await database.scalar(
            'SELECT file_identity FROM upload_parts ' 'WHERE job_id=1 AND part_index=1'
        )
        assert json.loads(str(stored))['size'] == len(b'rebuilt-final-file')
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_media_index_claim_during_snapshot_aborts_upload_part_insert(
    tmp_path: Path,
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'upload.sqlite3'))
    await database.open()
    try:
        await seed_ready_session(database, tmp_path)
        worker = coordinator(
            database, FakeProtocol(), FakeUploader(database), MutableClock(1000)
        )
        assert await worker.resolve_finished_sessions() == [1]
        original_file_identity = worker._file_identity
        claimed = False

        async def claim_during_identity(path: str):
            nonlocal claimed
            identity = await original_file_identity(path)
            if not claimed:
                claimed = True
                await database.execute(
                    "UPDATE recording_parts SET media_index_state='indexing' "
                    'WHERE id=1'
                )
            return identity

        worker._file_identity = claim_during_identity  # type: ignore[method-assign]

        assert await worker.prepare_waiting_jobs() == []
        assert await database.scalar('SELECT COUNT(*) FROM upload_parts') == 0
        assert (
            await database.scalar('SELECT state FROM upload_jobs WHERE id=1')
            == 'waiting_artifacts'
        )
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_explicit_session_override_wins_over_disabled_room_policy(
    tmp_path: Path,
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'upload.sqlite3'))
    await database.open()
    try:
        await seed_ready_session(database, tmp_path)
        policy_manager = RoomUploadPolicyManager(database, clock=lambda: 1000)
        submissions = SessionSubmissionManager(
            database, policy_manager=policy_manager, clock=lambda: 1000
        )
        override = replace(
            default_room_upload_policy(), title_template='单场覆盖 {{ title }}', tid=17
        )
        await submissions.save_override(1, override, manager_subject='administrator')
        await submissions.set_decision(1, 'upload', manager_subject='administrator')
        await database.execute(
            'UPDATE room_upload_policies SET enabled=0 WHERE room_id=100'
        )
        worker = coordinator(
            database, FakeProtocol(), FakeUploader(database), MutableClock(1000)
        )

        assert await worker.resolve_finished_sessions() == [1]
        snapshot = json.loads(
            str(
                await database.scalar(
                    'SELECT policy_snapshot_json FROM upload_jobs WHERE id=1'
                )
            )
        )
        assert snapshot['title'] == '单场覆盖 测试直播'
        assert snapshot['tid'] == 17
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_missing_upload_account_sets_actionable_resolution_error(
    tmp_path: Path,
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'upload.sqlite3'))
    await database.open()
    try:
        await seed_ready_session(database, tmp_path)
        await database.execute("UPDATE bili_accounts SET state='paused' WHERE id=1")
        worker = coordinator(
            database, FakeProtocol(), FakeUploader(database), MutableClock(1000)
        )

        assert await worker.resolve_finished_sessions() == []
        row = await database.fetchone(
            'SELECT upload_resolution_state,upload_resolution_error '
            'FROM recording_sessions WHERE id=1'
        )
        assert row is not None
        assert dict(row) == {
            'upload_resolution_state': 'configuration_required',
            'upload_resolution_error': '投稿账号不可用，请在本场投稿设置中重新选择',
        }
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_unstable_file_creates_waiting_job_without_starting_upload(
    tmp_path: Path,
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'upload.sqlite3'))
    await database.open()
    try:
        await seed_ready_session(database, tmp_path, stable=False)
        worker = coordinator(
            database, FakeProtocol(), FakeUploader(database), MutableClock(1000)
        )

        assert await worker.create_ready_jobs() == [1]
        assert (
            await database.scalar('SELECT state FROM upload_jobs WHERE id=1')
            == 'waiting_artifacts'
        )
        assert await database.scalar('SELECT COUNT(*) FROM upload_parts') == 0
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_unstable_open_part_does_not_create_visible_preupload(
    tmp_path: Path,
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'upload.sqlite3'))
    await database.open()
    try:
        await seed_ready_session(database, tmp_path, stable=False)
        await make_session_open_with_one_closed_part(database)
        worker = coordinator(
            database, FakeProtocol(), FakeUploader(database), MutableClock(1000)
        )

        assert await worker.sync_live_sessions() == []
        assert await database.scalar('SELECT COUNT(*) FROM upload_jobs') == 0
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_broken_parts_are_excluded_from_upload_job(tmp_path: Path) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'upload.sqlite3'))
    await database.open()
    try:
        await seed_ready_session(database, tmp_path)
        await database.execute(
            "UPDATE recording_parts SET artifact_state='failed',final_path=NULL,"
            "error_message='已自动排除' WHERE id=2"
        )
        protocol = FakeProtocol()
        worker = coordinator(
            database, protocol, FakeUploader(database), MutableClock(1000)
        )

        assert await worker.create_ready_jobs() == [1]
        row = await database.fetchone(
            'SELECT policy_snapshot_json FROM upload_jobs WHERE id=1'
        )
        assert row is not None
        snapshot = json.loads(str(row['policy_snapshot_json']))
        assert snapshot['part_titles'] == ['P1', 'P2']
        assert snapshot['recording_part_indexes'] == [1, 2]
        parts = await database.fetchall(
            'SELECT part_index,source_path FROM upload_parts '
            'WHERE job_id=1 ORDER BY part_index'
        )
        assert [
            (int(part['part_index']), str(part['source_path'])) for part in parts
        ] == [(1, str(tmp_path / 'part-1.flv'))]
        await worker.run_once()
        assert protocol.submit_calls[0]['videos'] == [
            {'filename': 'remote-1', 'title': 'P1', 'desc': ''}
        ]
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_legacy_snapshot_maps_sparse_parts_by_submission_order(
    tmp_path: Path,
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'upload.sqlite3'))
    await database.open()
    try:
        await seed_ready_session(database, tmp_path)
        worker = coordinator(
            database, FakeProtocol(), FakeUploader(database), MutableClock(1000)
        )
        assert await worker.create_ready_jobs() == [1]
        row = await database.fetchone(
            'SELECT policy_snapshot_json FROM upload_jobs WHERE id=1'
        )
        assert row is not None
        snapshot = json.loads(str(row['policy_snapshot_json']))
        snapshot.pop('recording_part_indexes')
        snapshot['part_titles'] = ['P2', 'P12']
        await database.execute(
            'UPDATE upload_jobs SET aid=303,policy_snapshot_json=? WHERE id=1',
            (json.dumps(snapshot),),
        )
        await database.execute(
            "UPDATE upload_parts SET part_index=12,remote_filename='remote-p12' "
            'WHERE job_id=1 AND part_index=2'
        )
        await database.execute(
            "UPDATE upload_parts SET part_index=2,remote_filename='remote-p2' "
            'WHERE job_id=1 AND part_index=1'
        )

        payload = await worker.build_edit_payload(
            1, {}, 'https://archive.biliimg.com/fixture.jpg'
        )

        assert payload['videos'] == [
            {'filename': 'remote-p2', 'title': 'P2', 'desc': ''},
            {'filename': 'remote-p12', 'title': 'P12', 'desc': ''},
        ]
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_run_once_uploads_parts_in_order_and_submits_one_archive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    audit_events: List[Tuple[str, Dict[str, Any]]] = []
    monkeypatch.setattr(
        'blrec.bili_upload.upload.audit',
        lambda event, **fields: audit_events.append((event, fields)),
    )
    database = BiliUploadDatabase(str(tmp_path / 'upload.sqlite3'))
    await database.open()
    try:
        await seed_ready_session(database, tmp_path)
        protocol = FakeProtocol()
        uploader = FakeUploader(database)
        worker = coordinator(database, protocol, uploader, MutableClock(1000))
        await worker.create_ready_jobs()

        processed = await worker.run_once()

        assert processed == 1
        assert uploader.calls == [1, 2]
        assert len(protocol.submit_calls) == 1
        payload = protocol.submit_calls[0]
        assert payload['title'] == '测试直播 录播'
        assert payload['videos'] == [
            {'filename': 'remote-1', 'title': 'P1', 'desc': ''},
            {'filename': 'remote-2', 'title': 'P2', 'desc': ''},
        ]
        assert payload['dynamic'] == '测试直播｜测试主播'
        assert payload['no_disturbance'] == 0
        assert payload['no_reprint'] == 1
        assert payload['copyright'] == 1
        assert payload['creation_statement'] == {'id': -1}
        assert payload['recreate'] == 0
        assert payload['is_only_self'] == 0
        assert payload['up_selection_reply'] is False
        assert payload['up_close_reply'] is False
        assert payload['up_close_danmu'] is False
        assert payload['cover'] == 'https://archive.biliimg.com/live.jpg'
        assert 'dtime' not in payload
        job = await database.fetchone(
            'SELECT state,submit_state,aid,bvid,upload_completed_at,submitted_at '
            'FROM upload_jobs WHERE id=1'
        )
        assert job is not None
        assert dict(job) == {
            'state': 'waiting_review',
            'submit_state': 'confirmed',
            'aid': 303,
            'bvid': 'BVfixture',
            'upload_completed_at': 1000,
            'submitted_at': 1000,
        }
        assert any(
            event == 'upload_job_created' and fields['job_id'] == 1
            for event, fields in audit_events
        )
        assert any(
            event == 'upload_archive_submitted'
            and fields['aid'] == 303
            and fields['bvid'] == 'BVfixture'
            for event, fields in audit_events
        )
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_edit_payload_keeps_healthy_cids_and_replaces_only_selected_parts(
    tmp_path: Path,
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'upload.sqlite3'))
    await database.open()
    try:
        await seed_ready_session(database, tmp_path, publish_delay_seconds=7_200)
        covers = FakeCoverResolver()
        worker = coordinator(
            database,
            FakeProtocol(),
            FakeUploader(database),
            MutableClock(1_000),
            cover_resolver=covers,
        )
        await worker.create_ready_jobs()
        await worker.run_once()

        payload = await worker.build_edit_payload(
            1, {1: 201}, 'https://archive.biliimg.com/current-cover.jpg'
        )

        assert payload['aid'] == 303
        assert payload['recreate'] == -1
        assert payload['cover'] == 'https://archive.biliimg.com/current-cover.jpg'
        assert payload['videos'] == [
            {'filename': 'remote-1', 'title': 'P1', 'desc': '', 'cid': 201},
            {'filename': 'remote-2', 'title': 'P2', 'desc': ''},
        ]
        assert 'dtime' not in payload
        assert covers.live_calls == [(1, None, 'https://i0.hdslb.com/cover.jpg')]
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_custom_cover_schedule_and_collection_are_frozen_into_job(
    tmp_path: Path,
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'upload.sqlite3'))
    await database.open()
    try:
        await seed_ready_session(
            database,
            tmp_path,
            collection_season_id=20,
            collection_section_id=21,
            cover_mode='custom',
            cover_asset_id=7,
            publish_delay_seconds=7200,
        )
        clock = MutableClock(1000)
        covers = FakeCoverResolver()
        protocol = FakeProtocol()
        worker = coordinator(
            database, protocol, FakeUploader(database), clock, cover_resolver=covers
        )

        assert await worker.create_ready_jobs() == [1]
        created = await database.fetchone(
            'SELECT policy_snapshot_json,collection_branch_state,'
            'scheduled_publish_at FROM upload_jobs WHERE id=1'
        )
        assert created is not None
        snapshot = json.loads(str(created['policy_snapshot_json']))
        assert snapshot['collection_season_id'] == 20
        assert snapshot['collection_section_id'] == 21
        assert snapshot['cover_mode'] == 'custom'
        assert snapshot['cover_asset_id'] == 7
        assert snapshot['publish_delay_seconds'] == 7200
        assert created['collection_branch_state'] == 'pending'
        assert created['scheduled_publish_at'] is None

        await worker.run_once()

        assert covers.custom_calls == [(7, 1)]
        assert covers.live_calls == []
        assert protocol.submit_calls[0]['cover'] == (
            'https://archive.biliimg.com/custom-7-1.jpg'
        )
        assert protocol.submit_calls[0]['dtime'] == 8200
        assert (
            await database.scalar(
                'SELECT scheduled_publish_at FROM upload_jobs WHERE id=1'
            )
            == 8200
        )
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_live_cover_uses_recorded_local_path_before_remote_url(
    tmp_path: Path,
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'upload.sqlite3'))
    await database.open()
    try:
        local_cover = str(tmp_path / 'recorded-cover.jpg')
        await seed_ready_session(database, tmp_path, cover_path=local_cover)
        covers = FakeCoverResolver()
        worker = coordinator(
            database,
            FakeProtocol(),
            FakeUploader(database),
            MutableClock(1000),
            cover_resolver=covers,
        )
        await worker.create_ready_jobs()

        await worker.run_once()

        assert covers.live_calls == [(1, local_cover, 'https://i0.hdslb.com/cover.jpg')]
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_submission_uses_room_visibility_interaction_and_part_settings(
    tmp_path: Path,
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'upload.sqlite3'))
    await database.open()
    try:
        await seed_ready_session(
            database,
            tmp_path,
            part_title_template='第 {{ part_index }} P',
            dynamic_template='{{ title }} 的直播回放',
            is_only_self=True,
            publish_dynamic=False,
            original_authorization=False,
            up_selection_reply=True,
        )
        protocol = FakeProtocol()
        worker = coordinator(
            database, protocol, FakeUploader(database), MutableClock(1000)
        )

        await worker.create_ready_jobs()
        row = await database.fetchone(
            'SELECT policy_snapshot_json FROM upload_jobs WHERE id=1'
        )
        assert row is not None
        snapshot = json.loads(str(row['policy_snapshot_json']))
        assert snapshot['part_titles'] == ['第 1 P', '第 2 P']
        assert snapshot['dynamic'] == '测试直播 的直播回放'
        assert snapshot['is_only_self'] is True
        assert snapshot['publish_dynamic'] is False
        assert snapshot['original_authorization'] is False
        assert snapshot['no_reprint'] is False
        assert snapshot['up_selection_reply'] is True

        await worker.run_once()

        payload = protocol.submit_calls[0]
        assert payload['videos'][0]['title'] == '第 1 P'
        assert payload['dynamic'] == ''
        assert payload['no_disturbance'] == 1
        assert payload['no_reprint'] == 0
        assert payload['copyright'] == 3
        assert payload['creation_statement'] == {'id': -1}
        assert payload['is_only_self'] == 1
        assert payload['up_selection_reply'] is True
        assert payload['up_close_reply'] is False
        assert payload['up_close_danmu'] is False
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_format_four_submission_preserves_positive_creation_statement(
    tmp_path: Path,
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'upload.sqlite3'))
    await database.open()
    try:
        await seed_ready_session(
            database, tmp_path, creation_statement_id=1, original_authorization=False
        )
        protocol = FakeProtocol()
        worker = coordinator(
            database, protocol, FakeUploader(database), MutableClock(1000)
        )

        await worker.create_ready_jobs()
        await worker.run_once()

        payload = protocol.submit_calls[0]
        assert payload['copyright'] == 3
        assert payload['creation_statement'] == {'id': 1}
        assert payload['no_reprint'] == 0
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_repost_submission_renders_tags_source_and_creation_statement(
    tmp_path: Path,
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'upload.sqlite3'))
    await database.open()
    try:
        await seed_ready_session(
            database,
            tmp_path,
            tags_template='直播回放,{{ anchor_name }},{{ area_name }}',
            creation_statement_id=-2,
            original_authorization=False,
            source_template='https://live.bilibili.com/{{ room_id }}',
        )
        protocol = FakeProtocol()
        worker = coordinator(
            database, protocol, FakeUploader(database), MutableClock(1000)
        )

        await worker.create_ready_jobs()
        row = await database.fetchone(
            'SELECT policy_snapshot_json FROM upload_jobs WHERE id=1'
        )
        assert row is not None
        snapshot = json.loads(str(row['policy_snapshot_json']))
        assert snapshot['tags'] == '直播回放,测试主播,单机游戏'
        assert snapshot['source'] == 'https://live.bilibili.com/100'

        await worker.run_once()

        payload = protocol.submit_calls[0]
        assert payload['copyright'] == 2
        assert payload['source'] == 'https://live.bilibili.com/100'
        assert payload['no_reprint'] == 0
        assert payload['creation_statement'] == {'id': -2}
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_stop_requested_during_payload_prevents_archive_submission(
    tmp_path: Path,
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'upload.sqlite3'))
    await database.open()
    try:
        await seed_ready_session(database, tmp_path)
        stopped = False

        class StoppingCoverResolver(FakeCoverResolver):
            async def live_url(
                self, account_id: int, *, local_path: Optional[str], source_url: str
            ) -> str:
                nonlocal stopped
                stopped = True
                return await super().live_url(
                    account_id, local_path=local_path, source_url=source_url
                )

        protocol = FakeProtocol()
        worker = coordinator(
            database,
            protocol,
            FakeUploader(database),
            MutableClock(1000),
            cover_resolver=StoppingCoverResolver(),
            stop_requested=lambda: stopped,
        )
        await worker.create_ready_jobs()

        await worker.run_once()

        assert protocol.submit_calls == []
        job = await database.fetchone(
            'SELECT state,submit_state,lease_owner FROM upload_jobs WHERE id=1'
        )
        assert job is not None
        assert dict(job) == {
            'state': 'uploading',
            'submit_state': 'prepared',
            'lease_owner': None,
        }
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_existing_format_one_snapshot_keeps_previous_submit_defaults(
    tmp_path: Path,
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'upload.sqlite3'))
    await database.open()
    try:
        await seed_ready_session(database, tmp_path)
        protocol = FakeProtocol()
        worker = coordinator(
            database, protocol, FakeUploader(database), MutableClock(1000)
        )
        await worker.create_ready_jobs()
        row = await database.fetchone(
            'SELECT policy_snapshot_json FROM upload_jobs WHERE id=1'
        )
        assert row is not None
        snapshot = json.loads(str(row['policy_snapshot_json']))
        snapshot['format_version'] = 1
        for field in (
            'dynamic',
            'is_only_self',
            'publish_dynamic',
            'no_reprint',
            'up_selection_reply',
            'up_close_reply',
            'up_close_danmu',
        ):
            snapshot.pop(field, None)
        await database.execute(
            'UPDATE upload_jobs SET policy_snapshot_json=? WHERE id=1',
            (json.dumps(snapshot),),
        )

        await worker.run_once()

        payload = protocol.submit_calls[0]
        assert payload['dynamic'] == ''
        assert payload['no_disturbance'] == 0
        assert payload['no_reprint'] == 1
        assert payload['is_only_self'] == 0
        assert payload['up_selection_reply'] is False
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_lost_submit_response_is_reconciled_before_any_retry(
    tmp_path: Path,
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'upload.sqlite3'))
    await database.open()
    try:
        await seed_ready_session(database, tmp_path)
        protocol = FakeProtocol()
        protocol.submit_error = RemoteOutcomeUnknown('submit_archive')
        clock = MutableClock(1000)
        worker = coordinator(database, protocol, FakeUploader(database), clock)
        await worker.create_ready_jobs()

        await worker.run_once()

        job = await database.fetchone(
            'SELECT state,submit_state,next_attempt_at FROM upload_jobs WHERE id=1'
        )
        assert job is not None
        assert job['state'] == 'submitting'
        assert job['submit_state'] == 'unknown_outcome'
        assert int(job['next_attempt_at']) > clock.now
        assert len(protocol.submit_calls) == 1

        protocol.submit_error = None
        protocol.archive_entries = [
            {'Archive': {'aid': 303, 'bvid': 'BVfixture', 'title': '测试直播 录播'}}
        ]
        protocol.archive_details['BVfixture'] = {
            'code': 0,
            'data': {
                'archive': {'aid': 303, 'bvid': 'BVfixture'},
                'videos': [{'filename': 'remote-1'}, {'filename': 'remote-2'}],
            },
        }
        clock.now = int(job['next_attempt_at'])
        await worker.run_once()

        completed = await database.fetchone(
            'SELECT state,submit_state,bvid FROM upload_jobs WHERE id=1'
        )
        assert completed is not None
        assert dict(completed) == {
            'state': 'waiting_review',
            'submit_state': 'confirmed',
            'bvid': 'BVfixture',
        }
        assert len(protocol.submit_calls) == 1
        assert protocol.archive_view_calls == ['BVfixture']
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_archive_submit_hands_success_to_deletion_generation(
    tmp_path: Path,
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'upload.sqlite3'))
    await database.open()
    release_response = asyncio.Event()
    request_started = asyncio.Event()

    class BlockingSubmitProtocol(FakeProtocol):
        async def submit_archive(
            self, bundle: Any, payload: Mapping[str, Any]
        ) -> Mapping[str, Any]:
            self.submit_calls.append(payload)
            request_started.set()
            await release_response.wait()
            return {'code': 0, 'data': {'aid': 303, 'bvid': 'BVfixture'}}

    try:
        paths = await seed_ready_session(database, tmp_path)
        protocol = BlockingSubmitProtocol()
        worker = coordinator(
            database, protocol, FakeUploader(database), MutableClock(1000)
        )
        await worker.create_ready_jobs()
        process = asyncio.create_task(worker.run_once())
        await asyncio.wait_for(request_started.wait(), timeout=1)

        intents = await database.fetchall(
            'SELECT owner_kind,side_effect_key,source_generation,outcome_state '
            'FROM owner_handoff_outcomes ORDER BY side_effect_key'
        )
        assert [dict(row) for row in intents] == [
            {
                'owner_kind': 'upload',
                'side_effect_key': 'archive_submit',
                'source_generation': 0,
                'outcome_state': 'in_flight',
            },
            {
                'owner_kind': 'upload',
                'side_effect_key': 'cover_upload',
                'source_generation': 0,
                'outcome_state': 'confirmed_success',
            },
            {
                'owner_kind': 'upload',
                'side_effect_key': 'lease:1',
                'source_generation': 0,
                'outcome_state': 'in_flight',
            },
        ]

        deletion = LocalDeletionWorker(
            database, recording_root=tmp_path, clip_root=tmp_path / 'clips'
        )
        assert await deletion.request_session(1, manager_subject='manager') == 1
        release_response.set()
        assert await asyncio.wait_for(process, timeout=1) == 1

        job = await database.fetchone(
            'SELECT state,submit_state,lease_owner FROM upload_jobs WHERE id=1'
        )
        assert job is not None
        assert dict(job) == {
            'state': 'paused',
            'submit_state': 'prepared',
            'lease_owner': None,
        }
        outcome = await database.fetchone(
            "SELECT outcome_state,outcome_json,acknowledged_at "
            "FROM owner_handoff_outcomes WHERE owner_kind='upload' "
            "AND owner_id=1 AND side_effect_key='archive_submit' "
            'AND source_generation=0'
        )
        assert outcome is not None
        assert outcome['outcome_state'] == 'confirmed_success'
        assert json.loads(str(outcome['outcome_json'])) == {
            'aid': 303,
            'bvid': 'BVfixture',
        }
        assert outcome['acknowledged_at'] is not None
        assert len(protocol.submit_calls) == 1
        assert await deletion.run_once() == ('session', 1)
        assert all(not path.exists() for path in paths)
        assert await database.scalar('SELECT COUNT(*) FROM upload_jobs') == 0
        assert (
            await database.scalar(
                "SELECT COUNT(*) FROM owner_handoff_outcomes "
                "WHERE owner_kind='upload' AND owner_id=1 "
                "AND side_effect_key='archive_submit' "
                "AND outcome_state='confirmed_success'"
            )
            == 0
        )
    finally:
        release_response.set()
        if 'process' in locals():
            await asyncio.gather(process, return_exceptions=True)
        await database.close()


@pytest.mark.asyncio
async def test_archive_response_handoff_uses_original_generation_after_repeated_delete(
    tmp_path: Path,
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'upload.sqlite3'))
    await database.open()
    response_received = asyncio.Event()
    release_commit = asyncio.Event()
    try:
        await seed_ready_session(database, tmp_path)
        protocol = FakeProtocol()
        worker = coordinator(
            database, protocol, FakeUploader(database), MutableClock(1000)
        )
        original_complete = worker._complete_archive_submission

        async def complete_after_barrier(
            claim: LeaseClaim, *, aid: int, bvid: str
        ) -> bool:
            response_received.set()
            await release_commit.wait()
            return await original_complete(claim, aid=aid, bvid=bvid)

        worker._complete_archive_submission = complete_after_barrier  # type: ignore
        await worker.create_ready_jobs()
        process = asyncio.create_task(worker.run_once())
        await asyncio.wait_for(response_received.wait(), timeout=1)
        deletion = LocalDeletionWorker(
            database, recording_root=tmp_path, clip_root=tmp_path / 'clips'
        )
        assert await deletion.request_session(1, manager_subject='manager') == 1
        assert await deletion.request_session(1, manager_subject='manager') == 2
        release_commit.set()

        assert await asyncio.wait_for(process, timeout=1) == 1
        outcome = await database.fetchone(
            "SELECT source_generation,outcome_state,outcome_json "
            "FROM owner_handoff_outcomes WHERE owner_kind='upload' "
            "AND owner_id=1 AND side_effect_key='archive_submit'"
        )
        assert outcome is not None
        assert outcome['source_generation'] == 0
        assert outcome['outcome_state'] == 'confirmed_success'
        assert json.loads(str(outcome['outcome_json'])) == {
            'aid': 303,
            'bvid': 'BVfixture',
        }
        assert (
            await database.scalar(
                'SELECT cancellation_generation FROM recording_sessions WHERE id=1'
            )
            == 2
        )
        assert len(protocol.submit_calls) == 1
    finally:
        release_commit.set()
        if 'process' in locals():
            await asyncio.gather(process, return_exceptions=True)
        await database.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('remote_error', 'expected_outcome'),
    (
        (BiliApiError(400, operation='submit_archive'), 'confirmed_failure'),
        (RemoteOutcomeUnknown('submit_archive'), 'unknown_terminal'),
    ),
)
async def test_archive_failure_after_deletion_is_terminal_handoff(
    tmp_path: Path, remote_error: BaseException, expected_outcome: str
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'upload.sqlite3'))
    await database.open()
    request_started = asyncio.Event()
    release_response = asyncio.Event()

    class BlockingFailureProtocol(FakeProtocol):
        async def submit_archive(
            self, bundle: Any, payload: Mapping[str, Any]
        ) -> Mapping[str, Any]:
            del bundle
            self.submit_calls.append(payload)
            request_started.set()
            await release_response.wait()
            raise remote_error

    try:
        await seed_ready_session(database, tmp_path)
        protocol = BlockingFailureProtocol()
        worker = coordinator(
            database, protocol, FakeUploader(database), MutableClock(1000)
        )
        await worker.create_ready_jobs()
        process = asyncio.create_task(worker.run_once())
        await asyncio.wait_for(request_started.wait(), timeout=1)
        deletion = LocalDeletionWorker(
            database, recording_root=tmp_path, clip_root=tmp_path / 'clips'
        )
        await deletion.request_session(1, manager_subject='manager')
        release_response.set()

        assert await asyncio.wait_for(process, timeout=1) == 1
        outcome = await database.fetchone(
            "SELECT outcome_state,acknowledged_at FROM owner_handoff_outcomes "
            "WHERE owner_kind='upload' AND owner_id=1 "
            "AND side_effect_key='archive_submit' AND source_generation=0"
        )
        assert outcome is not None
        assert outcome['outcome_state'] == expected_outcome
        assert outcome['acknowledged_at'] is not None
        assert len(protocol.submit_calls) == 1
        assert (
            await database.scalar('SELECT lease_owner FROM upload_jobs WHERE id=1')
            is None
        )
    finally:
        release_response.set()
        if 'process' in locals():
            await asyncio.gather(process, return_exceptions=True)
        await database.close()


@pytest.mark.asyncio
async def test_recovery_never_repeats_in_flight_upos_completion(tmp_path: Path) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'upload.sqlite3'))
    await database.open()
    try:
        await seed_ready_session(database, tmp_path)
        protocol = FakeProtocol()
        worker = coordinator(
            database, protocol, FakeUploader(database), MutableClock(1000)
        )
        await worker.create_ready_jobs()
        await database.execute(
            "UPDATE upload_jobs SET state='uploading',lease_owner='upload-dead',"
            'lease_generation=3,lease_until=9999 WHERE id=1'
        )
        await database.execute(
            "UPDATE upload_parts SET upload_state='completing',"
            "upload_session_json='{}' WHERE id=1"
        )
        await database.execute(
            'INSERT INTO owner_handoff_outcomes('
            'owner_kind,owner_id,side_effect_key,source_generation,'
            'outcome_state,outcome_json,acknowledged_at) '
            "VALUES('upload',1,'lease:3',0,'in_flight','{}',NULL)"
        )
        await database.execute(
            'INSERT INTO owner_handoff_outcomes('
            'owner_kind,owner_id,side_effect_key,source_generation,'
            'outcome_state,outcome_json,acknowledged_at) '
            "VALUES('upos',1,'complete',0,'in_flight','{}',NULL)"
        )

        assert await worker.recover_interrupted() == 1

        job = await database.fetchone(
            'SELECT state,submit_state,lease_owner,review_reason '
            'FROM upload_jobs WHERE id=1'
        )
        assert job is not None
        assert dict(job) == {
            'state': 'paused',
            'submit_state': 'prepared',
            'lease_owner': None,
            'review_reason': 'UPOS 分 P 完成结果无法确认，已停止自动重试',
        }
        assert (
            await database.scalar('SELECT upload_state FROM upload_parts WHERE id=1')
            == 'unknown_outcome'
        )
        outcomes = await database.fetchall(
            'SELECT owner_kind,side_effect_key,outcome_state,acknowledged_at '
            'FROM owner_handoff_outcomes ORDER BY owner_kind'
        )
        assert [
            (row['owner_kind'], row['side_effect_key'], row['outcome_state'])
            for row in outcomes
        ] == [
            ('upload', 'lease:3', 'unknown_terminal'),
            ('upos', 'complete', 'unknown_terminal'),
        ]
        assert all(row['acknowledged_at'] is not None for row in outcomes)
        assert protocol.submit_calls == []
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_recovery_preserves_confirmed_remote_handoff(tmp_path: Path) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'upload.sqlite3'))
    await database.open()
    try:
        await seed_ready_session(database, tmp_path)
        worker = coordinator(
            database, FakeProtocol(), FakeUploader(database), MutableClock(1000)
        )
        await worker.create_ready_jobs()
        await database.execute(
            "UPDATE recording_sessions SET deletion_state='requested',"
            'cancellation_generation=1 WHERE id=1'
        )
        await database.execute(
            "UPDATE upload_jobs SET state='uploading',lease_owner='upload-dead',"
            'lease_generation=3,lease_until=9999 WHERE id=1'
        )
        await database.execute(
            "UPDATE upload_parts SET upload_state='completing',"
            "upload_session_json='{}' WHERE id=1"
        )
        await database.execute(
            'INSERT INTO owner_handoff_outcomes('
            'owner_kind,owner_id,side_effect_key,source_generation,'
            'outcome_state,outcome_json,acknowledged_at) '
            "VALUES('upload',1,'lease:3',0,'in_flight','{}',NULL)"
        )
        await database.execute(
            'INSERT INTO owner_handoff_outcomes('
            'owner_kind,owner_id,side_effect_key,source_generation,'
            'outcome_state,outcome_json,acknowledged_at) '
            "VALUES('upos',1,'complete',0,'confirmed_success',"
            "'{\"remote_filename\":\"remote-video\"}',900)"
        )

        assert await worker.recover_interrupted() == 1

        outcome = await database.fetchone(
            "SELECT outcome_state,outcome_json,acknowledged_at "
            "FROM owner_handoff_outcomes WHERE owner_kind='upos' "
            "AND owner_id=1 AND side_effect_key='complete'"
        )
        assert outcome is not None
        assert dict(outcome) == {
            'outcome_state': 'confirmed_success',
            'outcome_json': '{"remote_filename":"remote-video"}',
            'acknowledged_at': 900,
        }
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_deletion_after_parts_prevents_archive_submission(tmp_path: Path) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'upload.sqlite3'))
    await database.open()

    class DeletingUploader(FakeUploader):
        async def upload_part(
            self, part_id: int, *, bundle: Any, claim: LeaseClaim
        ) -> str:
            remote = await super().upload_part(part_id, bundle=bundle, claim=claim)
            if len(self.calls) == 2:
                await self._database.execute(
                    "UPDATE recording_sessions SET deletion_state='requested',"
                    'cancellation_generation=1 WHERE id=1'
                )
            return remote

    try:
        await seed_ready_session(database, tmp_path)
        protocol = FakeProtocol()
        worker = coordinator(
            database, protocol, DeletingUploader(database), MutableClock(1000)
        )
        await worker.create_ready_jobs()

        assert await worker.run_once() == 1

        assert protocol.submit_calls == []
        job = await database.fetchone(
            'SELECT state,submit_state,lease_owner FROM upload_jobs WHERE id=1'
        )
        assert job is not None
        assert dict(job) == {
            'state': 'paused',
            'submit_state': 'prepared',
            'lease_owner': None,
        }
        claim_outcome = await database.fetchone(
            "SELECT outcome_state,acknowledged_at FROM owner_handoff_outcomes "
            "WHERE owner_kind='upload' AND owner_id=1 "
            "AND side_effect_key='lease:1' AND source_generation=0"
        )
        assert claim_outcome is not None
        assert claim_outcome['outcome_state'] == 'cancelled_local'
        assert claim_outcome['acknowledged_at'] is not None
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_bvc_rejection_names_the_affected_local_part(tmp_path: Path) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'upload.sqlite3'))
    await database.open()
    try:
        await seed_ready_session(database, tmp_path)
        protocol = FakeProtocol()
        protocol.submit_error = BiliApiError(
            21588,
            operation='submit_archive',
            details={'bvc_check': [{'cid': 12345, 'message': '该视频时长不足 1 秒'}]},
        )
        worker = coordinator(
            database, protocol, FakeUploader(database), MutableClock(1000)
        )
        await worker.create_ready_jobs()
        await database.execute(
            'UPDATE upload_parts SET cid=12345 WHERE job_id=1 AND part_index=1'
        )

        await worker.run_once()

        job = await database.fetchone(
            'SELECT state,review_reason FROM upload_jobs WHERE id=1'
        )
        assert job is not None
        assert job['state'] == 'paused'
        assert job['review_reason'] == ('B 站视频检测未通过：P1 该视频时长不足 1 秒')
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_restart_during_submit_reconciles_without_blind_retry(
    tmp_path: Path,
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'upload.sqlite3'))
    await database.open()
    try:
        await seed_ready_session(database, tmp_path)
        protocol = FakeProtocol()
        clock = MutableClock(5000)
        worker = coordinator(database, protocol, FakeUploader(database), clock)
        await worker.create_ready_jobs()
        await database.execute(
            "UPDATE upload_parts SET upload_state='confirmed',"
            "remote_filename='remote-' || id WHERE job_id=1"
        )
        await database.execute(
            "UPDATE upload_jobs SET state='submitting',submit_state='in_flight',"
            'upload_completed_at=1 '
            'WHERE id=1'
        )

        await worker.run_once()

        assert protocol.submit_calls == []
        job = await database.fetchone(
            'SELECT state,submit_state,next_attempt_at FROM upload_jobs WHERE id=1'
        )
        assert job is not None
        assert job['state'] == 'submitting'
        assert job['submit_state'] == 'unknown_outcome'
        clock.now = int(job['next_attempt_at'])

        await worker.run_once()

        assert protocol.submit_calls == []
        assert (
            await database.scalar('SELECT submit_state FROM upload_jobs WHERE id=1')
            == 'unknown_outcome'
        )
    finally:
        await database.close()


async def prepare_unknown_submission(
    database: BiliUploadDatabase, worker: UploadCoordinator
) -> None:
    await worker.create_ready_jobs()
    await database.execute(
        "UPDATE upload_parts SET upload_state='confirmed',"
        "remote_filename='remote-' || id WHERE job_id=1"
    )
    await database.execute(
        "UPDATE upload_jobs SET state='submitting',submit_state='unknown_outcome',"
        'upload_completed_at=1,next_attempt_at=0 WHERE id=1'
    )


@pytest.mark.asyncio
async def test_submission_reconciliation_timeout_never_resubmits(
    tmp_path: Path,
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'upload.sqlite3'))
    await database.open()
    blocked = asyncio.Event()

    class BlockingArchiveProtocol(FakeProtocol):
        async def list_archives(
            self, bundle: Any, params: Mapping[str, Any]
        ) -> Mapping[str, Any]:
            del bundle, params
            self.list_archive_calls += 1
            await blocked.wait()
            return {'code': 0, 'data': {'arc_audits': []}}

    protocol = BlockingArchiveProtocol()
    reader = ArchiveReadService(protocol)
    try:
        await seed_ready_session(database, tmp_path)
        worker = coordinator(
            database,
            protocol,
            FakeUploader(database),
            MutableClock(1000),
            archive_reader=reader,
            read_timeout_seconds=0.01,
        )
        await prepare_unknown_submission(database, worker)

        assert await worker.run_once() == 1

        job = await database.fetchone(
            'SELECT state,submit_state,lease_owner FROM upload_jobs WHERE id=1'
        )
        assert job is not None
        assert dict(job) == {
            'state': 'submitting',
            'submit_state': 'unknown_outcome',
            'lease_owner': None,
        }
        assert protocol.submit_calls == []
    finally:
        blocked.set()
        await reader.close()
        await database.close()


@pytest.mark.asyncio
async def test_submission_reconciliation_stops_when_candidate_eleven_appears(
    tmp_path: Path,
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'upload.sqlite3'))
    await database.open()
    protocol = FakeProtocol()
    reader = ArchiveReadService(protocol)
    try:
        await seed_ready_session(database, tmp_path)
        protocol.archive_entries = [
            {
                'Archive': {
                    'aid': 1_000 + index,
                    'bvid': 'BVcandidate{}'.format(index),
                    'title': '测试直播 录播',
                }
            }
            for index in range(11)
        ]
        worker = coordinator(
            database,
            protocol,
            FakeUploader(database),
            MutableClock(1000),
            archive_reader=reader,
        )
        await prepare_unknown_submission(database, worker)

        assert await worker.run_once() == 1

        assert protocol.submit_calls == []
        assert protocol.archive_view_calls == []
        assert (
            await database.scalar('SELECT submit_state FROM upload_jobs WHERE id=1')
            == 'unknown_outcome'
        )
    finally:
        await reader.close()
        await database.close()


@pytest.mark.asyncio
async def test_submission_candidate_details_remain_sequential_and_bounded(
    tmp_path: Path,
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'upload.sqlite3'))
    await database.open()

    class TrackingDetailProtocol(FakeProtocol):
        def __init__(self) -> None:
            super().__init__()
            self.active_details = 0
            self.max_active_details = 0

        async def archive_view(
            self, bundle: Any, params: Mapping[str, Any]
        ) -> Mapping[str, Any]:
            del bundle
            bvid = str(params['bvid'])
            self.archive_view_calls.append(bvid)
            self.active_details += 1
            self.max_active_details = max(self.max_active_details, self.active_details)
            try:
                await asyncio.sleep(0)
                return self.archive_details[bvid]
            finally:
                self.active_details -= 1

    protocol = TrackingDetailProtocol()
    reader = ArchiveReadService(protocol)
    try:
        await seed_ready_session(database, tmp_path)
        for index in range(10):
            bvid = 'BVcandidate{}'.format(index)
            protocol.archive_entries.append(
                {
                    'Archive': {
                        'aid': 1_000 + index,
                        'bvid': bvid,
                        'title': '测试直播 录播',
                    }
                }
            )
            protocol.archive_details[bvid] = {
                'code': 0,
                'data': {
                    'archive': {'aid': 1_000 + index, 'bvid': bvid},
                    'videos': [{'filename': 'not-the-uploaded-file'}],
                },
            }
        worker = coordinator(
            database,
            protocol,
            FakeUploader(database),
            MutableClock(1000),
            archive_reader=reader,
        )
        await prepare_unknown_submission(database, worker)

        assert await worker.run_once() == 1

        assert len(protocol.archive_view_calls) == 10
        assert protocol.max_active_details == 1
        assert protocol.submit_calls == []
    finally:
        await reader.close()
        await database.close()


@pytest.mark.asyncio
async def test_submission_reconciliation_stops_on_repeated_page_identity(
    tmp_path: Path,
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'upload.sqlite3'))
    await database.open()
    protocol = FakeProtocol()
    reader = ArchiveReadService(protocol)
    try:
        await seed_ready_session(database, tmp_path)
        protocol.archive_entries = [
            {
                'Archive': {
                    'aid': 1_000 + index,
                    'bvid': 'BVfill{}'.format(index),
                    'title': 'unrelated',
                }
            }
            for index in range(50)
        ]
        worker = coordinator(
            database,
            protocol,
            FakeUploader(database),
            MutableClock(1000),
            archive_reader=reader,
        )
        await prepare_unknown_submission(database, worker)

        assert await worker.run_once() == 1

        assert protocol.list_archive_calls == 2
        assert protocol.archive_view_calls == []
        assert protocol.submit_calls == []
    finally:
        await reader.close()
        await database.close()


@pytest.mark.asyncio
async def test_submit_rate_limit_is_retried_automatically(tmp_path: Path) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'upload.sqlite3'))
    await database.open()
    try:
        await seed_ready_session(database, tmp_path)
        protocol = FakeProtocol()
        protocol.submit_error = BiliApiError(406, operation='submit_archive')
        clock = MutableClock(1000)
        worker = coordinator(database, protocol, FakeUploader(database), clock)
        await worker.create_ready_jobs()

        await worker.run_once()

        job = await database.fetchone(
            'SELECT state,submit_state,next_attempt_at FROM upload_jobs WHERE id=1'
        )
        assert job is not None
        assert job['state'] == 'submitting'
        assert job['submit_state'] == 'prepared'
        assert int(job['next_attempt_at']) > clock.now

        protocol.submit_error = None
        clock.now = int(job['next_attempt_at'])
        await worker.run_once()

        assert len(protocol.submit_calls) == 2
        assert (
            await database.scalar('SELECT state FROM upload_jobs WHERE id=1')
            == 'waiting_review'
        )
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_deletion_after_parts_prevents_cover_upload(tmp_path: Path) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'upload.sqlite3'))
    await database.open()

    class DeletingUploader(FakeUploader):
        async def upload_part(
            self, part_id: int, *, bundle: Any, claim: LeaseClaim
        ) -> str:
            remote = await super().upload_part(part_id, bundle=bundle, claim=claim)
            if len(self.calls) == 2:
                await self._database.execute(
                    "UPDATE recording_sessions SET deletion_state='requested',"
                    'cancellation_generation=1 WHERE id=1'
                )
            return remote

    try:
        await seed_ready_session(database, tmp_path)
        protocol = FakeProtocol()
        cover_resolver = FakeCoverResolver()
        worker = coordinator(
            database,
            protocol,
            DeletingUploader(database),
            MutableClock(1000),
            cover_resolver=cover_resolver,
        )
        await worker.create_ready_jobs()

        assert await worker.run_once() == 1

        assert cover_resolver.live_calls == []
        assert protocol.submit_calls == []
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_unknown_cover_upload_is_terminal_and_not_blindly_retried(
    tmp_path: Path,
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'upload.sqlite3'))
    await database.open()

    class UnknownCoverResolver(FakeCoverResolver):
        async def live_url(
            self, account_id: int, *, local_path: Optional[str], source_url: str
        ) -> str:
            self.live_calls.append((account_id, local_path, source_url))
            raise RemoteOutcomeUnknown('upload_cover')

    try:
        await seed_ready_session(database, tmp_path)
        protocol = FakeProtocol()
        cover_resolver = UnknownCoverResolver()
        worker = coordinator(
            database,
            protocol,
            FakeUploader(database),
            MutableClock(1000),
            cover_resolver=cover_resolver,
        )
        await worker.create_ready_jobs()

        assert await worker.run_once() == 1
        assert await worker.run_once() is None

        job = await database.fetchone(
            'SELECT state,submit_state,operator_paused,lease_owner '
            'FROM upload_jobs WHERE id=1'
        )
        assert job is not None
        assert dict(job) == {
            'state': 'paused',
            'submit_state': 'prepared',
            'operator_paused': 1,
            'lease_owner': None,
        }
        outcome = await database.fetchone(
            "SELECT outcome_state,acknowledged_at "
            "FROM owner_handoff_outcomes WHERE owner_kind='upload' "
            "AND owner_id=1 AND side_effect_key='cover_upload'"
        )
        assert outcome is not None
        assert outcome['outcome_state'] == 'unknown_terminal'
        assert outcome['acknowledged_at'] is not None
        assert len(cover_resolver.live_calls) == 1
        assert protocol.submit_calls == []
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_confirmed_cover_is_reused_after_crash_before_archive_submission(
    tmp_path: Path,
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'upload.sqlite3'))
    await database.open()
    try:
        await seed_ready_session(database, tmp_path)
        protocol = FakeProtocol()
        cover_resolver = FakeCoverResolver()
        worker = coordinator(
            database,
            protocol,
            FakeUploader(database),
            MutableClock(1000),
            cover_resolver=cover_resolver,
        )
        await worker.create_ready_jobs()
        original_complete = worker._complete_cover_upload

        async def complete_then_crash(*args: Any, **kwargs: Any) -> bool:
            await original_complete(*args, **kwargs)
            raise RuntimeError('simulated crash after cover response')

        worker._complete_cover_upload = complete_then_crash  # type: ignore
        with pytest.raises(RuntimeError, match='after cover response'):
            await worker.run_once()

        worker._complete_cover_upload = original_complete  # type: ignore
        assert await worker.run_once() == 1
        assert len(cover_resolver.live_calls) == 1
        assert len(protocol.submit_calls) == 1
        assert protocol.submit_calls[0]['cover'] == (
            'https://archive.biliimg.com/live.jpg'
        )
    finally:
        await database.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('remote_error', 'expected_state', 'expected_submit_state'),
    (
        (DefinitelyNotSent('submit_archive'), 'submitting', 'prepared'),
        (BiliApiError(400, operation='submit_archive'), 'paused', 'failed_permanent'),
    ),
)
async def test_archive_failure_state_is_atomic_before_followup_work(
    tmp_path: Path,
    remote_error: BaseException,
    expected_state: str,
    expected_submit_state: str,
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'upload.sqlite3'))
    await database.open()
    try:
        await seed_ready_session(database, tmp_path)
        protocol = FakeProtocol()
        protocol.submit_error = remote_error
        worker = coordinator(
            database, protocol, FakeUploader(database), MutableClock(1000)
        )
        await worker.create_ready_jobs()
        original_settle = worker._settle_archive_failure

        async def settle_then_crash(*args: Any, **kwargs: Any) -> bool:
            await original_settle(*args, **kwargs)
            raise RuntimeError('simulated crash after archive settlement')

        worker._settle_archive_failure = settle_then_crash  # type: ignore

        with pytest.raises(RuntimeError, match='after archive settlement'):
            await worker.run_once()

        job = await database.fetchone(
            'SELECT state,submit_state,lease_owner FROM upload_jobs WHERE id=1'
        )
        assert job is not None
        assert dict(job) == {
            'state': expected_state,
            'submit_state': expected_submit_state,
            'lease_owner': None,
        }
        assert (
            await database.scalar(
                "SELECT COUNT(*) FROM owner_handoff_outcomes "
                "WHERE owner_kind='upload' AND owner_id=1 "
                "AND side_effect_key IN ('archive_submit','lease:1')"
            )
            == 0
        )
    finally:
        await database.close()
