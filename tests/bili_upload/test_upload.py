from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

import pytest

from blrec.bili_upload.accounts import AccountWriteGate
from blrec.bili_upload.database import BiliUploadDatabase, LeaseClaim
from blrec.bili_upload.errors import RemoteOutcomeUnknown
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


class FakeProtocol:
    def __init__(self) -> None:
        self.submit_calls: List[Mapping[str, Any]] = []
        self.submit_error: Optional[BaseException] = None

    async def submit_archive(
        self, _bundle: Any, payload: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        self.submit_calls.append(payload)
        if self.submit_error is not None:
            raise self.submit_error
        return {'code': 0, 'data': {'aid': 303, 'bvid': 'BVfixture'}}


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
            'file_size_bytes,danmaku_count,artifact_state,created_at,updated_at) '
            "VALUES(?,1,'run',?,?,?,?,?,?,?,?,'ready',800,900)",
            (
                index,
                index,
                str(path),
                str(path),
                800 + index,
                850 + index,
                50,
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
async def test_lost_submit_response_is_not_retried(tmp_path: Path) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'upload.sqlite3'))
    await database.open()
    try:
        await seed_ready_session(database, tmp_path)
        protocol = FakeProtocol()
        protocol.submit_error = RemoteOutcomeUnknown('submit_archive')
        worker = coordinator(
            database, protocol, FakeUploader(database), MutableClock(1000)
        )
        await worker.create_ready_jobs()

        await worker.run_once()
        assert await worker.run_once() is None

        job = await database.fetchone(
            'SELECT state,submit_state FROM upload_jobs WHERE id=1'
        )
        assert job is not None
        assert dict(job) == {'state': 'paused', 'submit_state': 'unknown_outcome'}
        assert len(protocol.submit_calls) == 1
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_restart_during_submit_is_paused_without_repeating_request(
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
        await database.execute(
            "UPDATE upload_jobs SET state='submitting',submit_state='in_flight' "
            'WHERE id=1'
        )

        await worker.run_once()

        assert protocol.submit_calls == []
        job = await database.fetchone(
            'SELECT state,submit_state FROM upload_jobs WHERE id=1'
        )
        assert job is not None
        assert dict(job) == {'state': 'paused', 'submit_state': 'unknown_outcome'}
    finally:
        await database.close()
