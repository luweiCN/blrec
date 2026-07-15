from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional
from unittest.mock import AsyncMock

import pytest

from blrec.bili_upload.accounts import AccountWriteGate
from blrec.bili_upload.database import BiliUploadDatabase, LeaseClaim, LeaseLost
from blrec.bili_upload.task_actions import (
    UploadTaskActionManager,
    UploadTaskActionRejected,
)
from blrec.bili_upload.transcode_remux import RemuxedArtifact
from blrec.bili_upload.upos import FileIdentity


class FakeProtocol:
    def __init__(self, archive: Mapping[str, Any]) -> None:
        self.archive = archive
        self.view_calls: List[Mapping[str, Any]] = []
        self.edit_calls: List[Mapping[str, Any]] = []

    async def archive_view(
        self, _bundle: Any, params: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        self.view_calls.append(params)
        return self.archive

    async def edit_archive(
        self, _bundle: Any, payload: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        self.edit_calls.append(payload)
        return {'code': 0, 'data': {'aid': 303, 'bvid': 'BVfixture'}}


class FakeUploader:
    def __init__(self, database: BiliUploadDatabase) -> None:
        self.database = database
        self.calls: List[int] = []

    async def upload_part(self, part_id: int, *, bundle: Any, claim: LeaseClaim) -> str:
        del bundle
        assert claim.id == 9
        self.calls.append(part_id)
        filename = 'replacement-{}'.format(part_id)
        await self.database.execute(
            "UPDATE upload_parts SET upload_state='confirmed',remote_filename=? "
            'WHERE id=?',
            (filename, part_id),
        )
        return filename


class FakeRemuxer:
    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.calls: List[tuple[str, int]] = []

    def remux(self, source_path: str, *, part_id: int) -> RemuxedArtifact:
        self.calls.append((source_path, part_id))
        path = self.directory / 'remux-{}.mp4'.format(part_id)
        path.write_bytes(b'remuxed-video')
        return RemuxedArtifact(
            path=str(path),
            identity=FileIdentity.from_path(str(path)),
            diagnostic='fake remux ok',
        )

    @staticmethod
    def remove(path: str) -> None:
        Path(path).unlink(missing_ok=True)


class LeaseStealingProtocol(FakeProtocol):
    def __init__(self, database: BiliUploadDatabase) -> None:
        super().__init__(archive_response())
        self.database = database

    async def archive_view(
        self, _bundle: Any, params: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        self.view_calls.append(params)
        await self.database.execute(
            "UPDATE upload_jobs SET lease_owner='replacement-worker' WHERE id=9"
        )
        raise LeaseLost('转码修复任务租约已失效')


class FakeEditPayloadBuilder:
    def __init__(self, database: BiliUploadDatabase) -> None:
        self.database = database
        self.calls: List[Dict[int, int]] = []
        self.cover_urls: List[Optional[str]] = []

    async def __call__(
        self, job_id: int, healthy_cids: Mapping[int, int], cover_url: Optional[str]
    ) -> Mapping[str, Any]:
        self.calls.append(dict(healthy_cids))
        self.cover_urls.append(cover_url)
        rows = await self.database.fetchall(
            'SELECT id,part_index,remote_filename FROM upload_parts '
            'WHERE job_id=? ORDER BY part_index',
            (job_id,),
        )
        videos = []
        for row in rows:
            video: Dict[str, Any] = {
                'filename': str(row['remote_filename']),
                'title': 'P{}'.format(int(row['part_index'])),
                'desc': '',
            }
            cid = healthy_cids.get(int(row['id']))
            if cid is not None:
                video['cid'] = cid
            videos.append(video)
        return {'aid': 303, 'title': 'fixture', 'videos': videos}


def archive_response(*, second_state: str = 'failed') -> Mapping[str, Any]:
    if second_state == 'failed':
        fail_code, xcode_state, fail_desc = 9, 3, '服务端转码失败'
    elif second_state == 'processing':
        fail_code, xcode_state, fail_desc = 0, 2, ''
    else:
        fail_code, xcode_state, fail_desc = 0, 0, ''
    return {
        'code': 0,
        'data': {
            'archive': {
                'aid': 303,
                'bvid': 'BVfixture',
                'cover': '//archive.biliimg.com/fixture.jpg',
            },
            'videos': [
                {
                    'aid': 303,
                    'bvid': 'BVfixture',
                    'filename': 'remote-11',
                    'cid': 101,
                    'page': 1,
                    'failCode': 0,
                    'xcodeState': 0,
                    'failDesc': '',
                },
                {
                    'aid': 303,
                    'bvid': 'BVfixture',
                    'filename': 'remote-12',
                    'cid': 102,
                    'page': 2,
                    'failCode': fail_code,
                    'xcodeState': xcode_state,
                    'failDesc': fail_desc,
                },
            ],
        },
    }


async def seed_job(
    database: BiliUploadDatabase,
    tmp_path: Path,
    *,
    state: str = 'waiting_review',
    submit_state: str = 'confirmed',
    second_upload_state: str = 'confirmed',
) -> None:
    await database.execute(
        "INSERT INTO bili_accounts("
        "id,uid,display_name,credential_ciphertext,credential_version,key_id,"
        "state,created_at,updated_at) "
        "VALUES(1,42,'投稿账号',X'00',3,'k','active',1,1)"
    )
    await database.execute(
        "INSERT INTO recording_sessions("
        "id,room_id,broadcast_session_key,state,started_at) "
        "VALUES(1,100,'100:1','closed',1)"
    )
    await database.execute(
        'INSERT INTO upload_jobs('
        'id,session_id,account_id,policy_snapshot_json,state,submit_state,'
        'aid,bvid,created_at,updated_at) VALUES(9,1,1,?,?,?,?,?,?,?)',
        (
            '{}',
            state,
            submit_state,
            303 if submit_state == 'confirmed' else None,
            'BVfixture' if submit_state == 'confirmed' else None,
            1,
            1,
        ),
    )
    for part_id in (11, 12):
        path = tmp_path / 'p{}.mp4'.format(part_id)
        path.write_bytes(('video-{}'.format(part_id)).encode('ascii'))
        identity = FileIdentity.from_path(str(path)).to_json()
        upload_state = 'confirmed' if part_id == 11 else second_upload_state
        await database.execute(
            'INSERT INTO upload_parts('
            'id,job_id,part_index,source_path,final_path,file_identity,'
            'artifact_state,upload_state,remote_filename) '
            "VALUES(?,9,?,?,?,?,'ready',?,?)",
            (
                part_id,
                part_id - 10,
                str(path),
                str(path),
                identity,
                upload_state,
                'remote-{}'.format(part_id),
            ),
        )


def make_manager(
    database: BiliUploadDatabase,
    protocol: FakeProtocol,
    recording_root: Path,
    *,
    remuxer: Optional[FakeRemuxer] = None,
) -> tuple[UploadTaskActionManager, FakeUploader, FakeEditPayloadBuilder]:
    uploader = FakeUploader(database)
    payload_builder = FakeEditPayloadBuilder(database)
    manager = UploadTaskActionManager(
        database,
        protocol,
        uploader,
        bundle_loader=lambda _account_id: _async_value(object()),
        account_gates=AccountWriteGate(database),
        edit_payload_builder=payload_builder,
        recording_root=recording_root,
        remuxer=remuxer,
        clock=lambda: 1_000,
    )
    return manager, uploader, payload_builder


async def _async_value(value: Any) -> Any:
    return value


def editable_snapshot() -> Dict[str, Any]:
    return {
        'format_version': 4,
        'account_id': 1,
        'account_uid': 42,
        'account_credential_version_at_creation': 3,
        'title': '原标题',
        'description': '简介',
        'dynamic': '',
        'tid': 17,
        'tags': '直播,录播',
        'creation_statement_id': -2,
        'original_authorization': False,
        'copyright': 2,
        'source': '直播间',
        'is_only_self': False,
        'publish_dynamic': True,
        'no_reprint': False,
        'up_selection_reply': True,
        'up_close_reply': False,
        'up_close_danmu': False,
        'cover_mode': 'live',
        'cover_asset_id': None,
        'collection_season_id': 88,
        'collection_section_id': 99,
        'publish_delay_seconds': 0,
        'auto_comment': True,
        'danmaku_backfill': True,
        'filters': {},
        'part_titles': ['P1', 'P2'],
    }


@pytest.mark.asyncio
async def test_operator_pause_and_resume_preserve_upload_progress(
    tmp_path: Path,
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'db.sqlite3'))
    await database.open()
    try:
        await seed_job(
            database,
            tmp_path,
            state='uploading',
            submit_state='prepared',
            second_upload_state='prepared',
        )
        await database.execute(
            "UPDATE upload_parts SET upload_state='prepared',remote_filename=NULL "
            'WHERE id=11'
        )
        await database.execute(
            "INSERT INTO upload_chunks(part_id,chunk_no,offset,size,state,attempt) "
            "VALUES(11,0,0,5,'confirmed',1)"
        )
        manager, _, _ = make_manager(
            database, FakeProtocol(archive_response()), tmp_path
        )

        assert (
            await manager.pause_upload(9, manager_subject='manager') == '上传任务已暂停'
        )
        paused = await database.fetchone(
            'SELECT state,operator_paused,operator_resume_state FROM upload_jobs '
            'WHERE id=9'
        )
        assert paused is not None
        assert dict(paused) == {
            'state': 'paused',
            'operator_paused': 1,
            'operator_resume_state': 'uploading',
        }

        assert (
            await manager.resume_upload(9, manager_subject='manager')
            == '上传任务已继续'
        )
        resumed = await database.fetchone(
            'SELECT state,operator_paused,operator_resume_state FROM upload_jobs '
            'WHERE id=9'
        )
        assert resumed is not None
        assert dict(resumed) == {
            'state': 'ready',
            'operator_paused': 0,
            'operator_resume_state': None,
        }
        assert await database.scalar('SELECT COUNT(*) FROM upload_chunks') == 1
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_update_unstarted_task_changes_account_and_clears_collection(
    tmp_path: Path,
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'db.sqlite3'))
    await database.open()
    try:
        await seed_job(
            database,
            tmp_path,
            state='ready',
            submit_state='prepared',
            second_upload_state='prepared',
        )
        await database.execute(
            "UPDATE upload_parts SET upload_state='prepared',remote_filename=NULL"
        )
        await database.execute(
            'UPDATE upload_jobs SET policy_snapshot_json=? WHERE id=9',
            (json.dumps(editable_snapshot()),),
        )
        await database.execute(
            "INSERT INTO bili_accounts("
            "id,uid,display_name,credential_ciphertext,credential_version,key_id,"
            "state,created_at,updated_at) "
            "VALUES(2,84,'第二账号',X'00',5,'k','active',1,1)"
        )
        manager, _, _ = make_manager(
            database, FakeProtocol(archive_response()), tmp_path
        )

        result = await manager.update_task(
            9,
            account_id=2,
            changes={'title': '修改后的标题', 'publish_dynamic': False},
            manager_subject='manager',
        )

        assert result.collection_cleared is True
        row = await database.fetchone(
            'SELECT account_id,policy_snapshot_json,comment_branch_state,'
            'danmaku_branch_state,collection_branch_state FROM upload_jobs WHERE id=9'
        )
        assert row is not None
        snapshot = json.loads(str(row['policy_snapshot_json']))
        assert row['account_id'] == 2
        assert snapshot['account_id'] == 2
        assert snapshot['account_uid'] == 84
        assert snapshot['account_credential_version_at_creation'] == 5
        assert snapshot['title'] == '修改后的标题'
        assert snapshot['publish_dynamic'] is False
        assert snapshot['collection_season_id'] is None
        assert snapshot['collection_section_id'] is None
        assert row['collection_branch_state'] == 'disabled'
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_update_task_rejects_any_remote_upload_effect(tmp_path: Path) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'db.sqlite3'))
    await database.open()
    try:
        await seed_job(
            database,
            tmp_path,
            state='ready',
            submit_state='prepared',
            second_upload_state='prepared',
        )
        await database.execute(
            'UPDATE upload_jobs SET policy_snapshot_json=? WHERE id=9',
            (json.dumps(editable_snapshot()),),
        )
        manager, _, _ = make_manager(
            database, FakeProtocol(archive_response()), tmp_path
        )

        with pytest.raises(UploadTaskActionRejected, match='已经开始上传'):
            await manager.update_task(
                9,
                account_id=1,
                changes={'title': '不能修改'},
                manager_subject='manager',
            )
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_retry_failed_resets_only_safe_failed_parts(tmp_path: Path) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'db.sqlite3'))
    await database.open()
    try:
        await seed_job(
            database,
            tmp_path,
            state='paused',
            submit_state='prepared',
            second_upload_state='failed',
        )
        await database.execute(
            'INSERT INTO upload_chunks('
            "part_id,chunk_no,offset,size,state,attempt) VALUES(12,0,0,8,'failed',3)"
        )
        manager, _, _ = make_manager(
            database, FakeProtocol(archive_response()), tmp_path
        )

        message = await manager.retry_failed(9, manager_subject='manager')

        assert message == '失败任务已重新排队'
        job = await database.fetchone(
            'SELECT state,submit_state,review_reason FROM upload_jobs WHERE id=9'
        )
        assert job is not None
        assert dict(job) == {
            'state': 'ready',
            'submit_state': 'prepared',
            'review_reason': '管理员已重新排队失败任务',
        }
        parts = await database.fetchall(
            'SELECT id,upload_state,remote_filename FROM upload_parts ORDER BY id'
        )
        assert [dict(row) for row in parts] == [
            {'id': 11, 'upload_state': 'confirmed', 'remote_filename': 'remote-11'},
            {'id': 12, 'upload_state': 'prepared', 'remote_filename': None},
        ]
        assert await database.scalar('SELECT COUNT(*) FROM upload_chunks') == 0
        audit = await database.fetchone(
            "SELECT action,target_id,old_state,new_state FROM management_audit "
            "WHERE action='retry_upload_job'"
        )
        assert audit is not None
        assert dict(audit) == {
            'action': 'retry_upload_job',
            'target_id': '9',
            'old_state': 'paused/prepared',
            'new_state': 'ready/prepared',
        }
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_retryable_failed_job_ids_excludes_unknown_remote_outcomes(
    tmp_path: Path,
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'db.sqlite3'))
    await database.open()
    try:
        await seed_job(
            database,
            tmp_path,
            state='paused',
            submit_state='prepared',
            second_upload_state='failed',
        )
        await database.execute(
            "INSERT INTO recording_sessions("
            "id,room_id,broadcast_session_key,state,started_at) "
            "VALUES(2,200,'200:1','closed',1)"
        )
        await database.execute(
            'INSERT INTO upload_jobs('
            'id,session_id,account_id,policy_snapshot_json,state,submit_state,'
            'created_at,updated_at) '
            "VALUES(10,2,1,'{}','paused','unknown_outcome',1,1)"
        )
        manager, _, _ = make_manager(
            database, FakeProtocol(archive_response()), tmp_path
        )

        job_ids = await manager.retryable_failed_job_ids()

        assert job_ids == (9,)
    finally:
        await database.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('submit_state', 'part_state'),
    (('unknown_outcome', 'unknown_outcome'), ('prepared', 'completing')),
)
async def test_retry_refuses_unknown_remote_outcomes(
    tmp_path: Path, submit_state: str, part_state: str
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'db.sqlite3'))
    await database.open()
    try:
        await seed_job(
            database,
            tmp_path,
            state='paused',
            submit_state=submit_state,
            second_upload_state=part_state,
        )
        manager, _, _ = make_manager(
            database, FakeProtocol(archive_response()), tmp_path
        )

        with pytest.raises(UploadTaskActionRejected, match='结果未知'):
            await manager.retry_failed(9, manager_subject='manager')
    finally:
        await database.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('interrupted_state', 'expected_job_state', 'expected_repair_state'),
    (
        ('reuploading', 'waiting_review', 'queued'),
        ('editing', 'paused', 'unknown_outcome'),
    ),
)
async def test_repair_recovery_distinguishes_safe_resume_from_unknown_edit(
    tmp_path: Path,
    interrupted_state: str,
    expected_job_state: str,
    expected_repair_state: str,
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'db.sqlite3'))
    await database.open()
    try:
        await seed_job(database, tmp_path)
        await database.execute(
            'UPDATE upload_jobs SET repair_state=?,lease_owner=?,lease_until=? '
            'WHERE id=9',
            (interrupted_state, 'stale-worker', 2_000),
        )
        manager, _, _ = make_manager(
            database, FakeProtocol(archive_response()), tmp_path
        )

        await manager.recover_interrupted()

        row = await database.fetchone(
            'SELECT state,repair_state,lease_owner,lease_until FROM upload_jobs '
            'WHERE id=9'
        )
        assert row is not None
        assert dict(row) == {
            'state': expected_job_state,
            'repair_state': expected_repair_state,
            'lease_owner': None,
            'lease_until': None,
        }
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_repair_reuploads_only_failed_part_and_edits_existing_archive(
    tmp_path: Path,
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'db.sqlite3'))
    await database.open()
    try:
        await seed_job(database, tmp_path)
        protocol = FakeProtocol(archive_response(second_state='failed'))
        manager, uploader, payload_builder = make_manager(database, protocol, tmp_path)

        message = await manager.request_transcode_repair(9, manager_subject='manager')
        processed = await manager.run_once()

        assert message == '已排队检查 B 站转码状态'
        assert processed == 9
        assert uploader.calls == [12]
        assert payload_builder.calls == [{11: 101}]
        assert payload_builder.cover_urls == ['https://archive.biliimg.com/fixture.jpg']
        assert protocol.edit_calls == [
            {
                'aid': 303,
                'title': 'fixture',
                'videos': [
                    {'filename': 'remote-11', 'title': 'P1', 'desc': '', 'cid': 101},
                    {'filename': 'replacement-12', 'title': 'P2', 'desc': ''},
                ],
            }
        ]
        job = await database.fetchone(
            'SELECT state,repair_state,repair_message,repair_error '
            'FROM upload_jobs WHERE id=9'
        )
        assert job is not None
        assert dict(job) == {
            'state': 'waiting_review',
            'repair_state': 'waiting_review',
            'repair_message': '已重传 1 个异常分 P，等待 B 站重新审核',
            'repair_error': None,
        }
        parts = await database.fetchall(
            'SELECT id,remote_filename,cid,transcode_state,transcode_fail_code,'
            'repair_stage,repair_original_attempts '
            'FROM upload_parts ORDER BY id'
        )
        assert [dict(row) for row in parts] == [
            {
                'id': 11,
                'remote_filename': 'remote-11',
                'cid': 101,
                'transcode_state': 'ready',
                'transcode_fail_code': 0,
                'repair_stage': 'none',
                'repair_original_attempts': 0,
            },
            {
                'id': 12,
                'remote_filename': 'replacement-12',
                'cid': None,
                'transcode_state': 'processing',
                'transcode_fail_code': None,
                'repair_stage': 'original_waiting_review',
                'repair_original_attempts': 1,
            },
        ]
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_second_terminal_failure_remuxes_only_failed_part_and_restores_path(
    tmp_path: Path,
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'db.sqlite3'))
    await database.open()
    try:
        await seed_job(database, tmp_path)
        original_path = str(tmp_path / 'p12.mp4')
        await database.execute(
            "UPDATE upload_parts SET repair_stage='original_waiting_review',"
            'repair_original_attempts=1 WHERE id=12'
        )
        protocol = FakeProtocol(archive_response(second_state='failed'))
        remuxer = FakeRemuxer(tmp_path)
        manager, uploader, payload_builder = make_manager(
            database, protocol, tmp_path, remuxer=remuxer
        )

        await manager.request_transcode_repair(9, manager_subject='manager')
        await manager.run_once()

        assert remuxer.calls == [(original_path, 12)]
        assert uploader.calls == [12]
        assert payload_builder.calls == [{11: 101}]
        part = await database.fetchone(
            'SELECT final_path,file_identity,repair_stage,repair_original_attempts,'
            'repair_remux_attempts,repair_temp_path,repair_diagnostic '
            'FROM upload_parts WHERE id=12'
        )
        assert part is not None
        assert part['final_path'] == original_path
        assert FileIdentity.from_json(str(part['file_identity'])).canonical_path == (
            str(Path(original_path).resolve())
        )
        assert part['repair_stage'] == 'remux_waiting_review'
        assert part['repair_original_attempts'] == 1
        assert part['repair_remux_attempts'] == 1
        assert part['repair_temp_path'] is None
        assert part['repair_diagnostic'] == 'fake remux ok'
        assert not (tmp_path / 'remux-12.mp4').exists()
    finally:
        await database.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('second_state', ('ready', 'processing'))
async def test_repair_stops_after_remote_recheck_when_reupload_is_not_needed(
    tmp_path: Path, second_state: str
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'db.sqlite3'))
    await database.open()
    try:
        await seed_job(database, tmp_path)
        protocol = FakeProtocol(archive_response(second_state=second_state))
        manager, uploader, _ = make_manager(database, protocol, tmp_path)

        await manager.request_transcode_repair(9, manager_subject='manager')
        await manager.run_once()

        assert uploader.calls == []
        assert protocol.edit_calls == []
        job = await database.fetchone(
            'SELECT repair_state,repair_message FROM upload_jobs WHERE id=9'
        )
        assert job is not None
        expected = (
            'B 站仍在转码，暂不重传'
            if second_state == 'processing'
            else '未发现需要修复的分 P'
        )
        assert dict(job) == {'repair_state': 'not_needed', 'repair_message': expected}
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_repair_worker_does_not_overwrite_job_after_lease_is_lost(
    tmp_path: Path,
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'db.sqlite3'))
    await database.open()
    try:
        await seed_job(database, tmp_path)
        manager, uploader, _ = make_manager(
            database, LeaseStealingProtocol(database), tmp_path
        )

        await manager.request_transcode_repair(9, manager_subject='manager')
        processed = await manager.run_once()

        assert processed == 9
        assert uploader.calls == []
        job = await database.fetchone(
            'SELECT repair_state,repair_error,lease_owner FROM upload_jobs WHERE id=9'
        )
        assert job is not None
        assert dict(job) == {
            'repair_state': 'checking',
            'repair_error': None,
            'lease_owner': 'replacement-worker',
        }
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_manual_danmaku_backfill_queues_an_approved_disabled_branch(
    tmp_path: Path,
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'db.sqlite3'))
    await database.open()
    try:
        await seed_job(database, tmp_path, state='approved')
        for part_id in (11, 12):
            xml_path = tmp_path / 'p{}.xml'.format(part_id)
            xml_path.write_text('<i><d p="1,1,25,16777215">弹幕</d></i>')
            await database.execute(
                'UPDATE upload_parts SET xml_path=?,cid=? WHERE id=?',
                (str(xml_path), 100 + part_id, part_id),
            )
        manager, _, _ = make_manager(
            database, FakeProtocol(archive_response()), tmp_path
        )

        message = await manager.request_danmaku_backfill(9, manager_subject='manager')

        assert message == '已排队回灌 2 个分 P 的弹幕'
        job = await database.fetchone(
            'SELECT state,danmaku_branch_state FROM upload_jobs WHERE id=9'
        )
        assert job is not None
        assert dict(job) == {'state': 'approved', 'danmaku_branch_state': 'importing'}
        states = await database.fetchall(
            'SELECT danmaku_import_state FROM upload_parts '
            'WHERE job_id=9 ORDER BY part_index'
        )
        assert [str(row['danmaku_import_state']) for row in states] == [
            'pending',
            'pending',
        ]
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_skip_upload_removes_unstarted_job_but_keeps_local_files(
    tmp_path: Path,
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'db.sqlite3'))
    await database.open()
    try:
        await seed_job(
            database,
            tmp_path,
            state='ready',
            submit_state='prepared',
            second_upload_state='prepared',
        )
        await database.execute(
            "UPDATE upload_parts SET upload_state='prepared',remote_filename=NULL "
            'WHERE job_id=9'
        )
        manager, _, _ = make_manager(
            database, FakeProtocol(archive_response()), tmp_path
        )

        message = await manager.skip_upload(9, manager_subject='manager')

        assert message == '该场录像已设为不上传'
        assert await database.scalar('SELECT COUNT(*) FROM upload_jobs') == 0
        assert (
            await database.scalar(
                'SELECT COUNT(*) FROM upload_suppressions WHERE session_id=1'
            )
            == 1
        )
        assert (tmp_path / 'p11.mp4').exists()
        assert (tmp_path / 'p12.mp4').exists()
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_repost_archives_old_bvid_and_resets_job_without_remote_delete(
    tmp_path: Path,
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'db.sqlite3'))
    await database.open()
    try:
        await seed_job(database, tmp_path, state='approved')
        await database.execute(
            'UPDATE upload_jobs SET policy_snapshot_json=? WHERE id=9',
            (
                json.dumps(
                    {
                        'auto_comment': True,
                        'danmaku_backfill': True,
                        'collection_section_id': 88,
                    }
                ),
            ),
        )
        manager, _, _ = make_manager(
            database, FakeProtocol(archive_response()), tmp_path
        )

        message = await manager.repost_as_new(9, manager_subject='manager')

        assert message == '已保留原稿件记录，并重新排队投稿为新稿件'
        job = await database.fetchone(
            'SELECT state,submit_state,aid,bvid,comment_branch_state,'
            'danmaku_branch_state,collection_branch_state FROM upload_jobs WHERE id=9'
        )
        assert job is not None
        assert dict(job) == {
            'state': 'ready',
            'submit_state': 'prepared',
            'aid': None,
            'bvid': None,
            'comment_branch_state': 'pending',
            'danmaku_branch_state': 'pending',
            'collection_branch_state': 'pending',
        }
        archived = await database.fetchone(
            'SELECT aid,bvid,reason FROM upload_job_archives WHERE old_job_id=9'
        )
        assert archived is not None
        assert dict(archived) == {
            'aid': 303,
            'bvid': 'BVfixture',
            'reason': 'repost_as_new',
        }
        assert all(
            str(row['upload_state']) == 'prepared'
            for row in await database.fetchall(
                'SELECT upload_state FROM upload_parts WHERE job_id=9'
            )
        )
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_delete_local_task_removes_owned_files_and_rows_only(
    tmp_path: Path,
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'db.sqlite3'))
    await database.open()
    try:
        await seed_job(database, tmp_path, state='approved')
        xml_path = tmp_path / 'p11.xml'
        xml_path.write_text('<i />', encoding='utf8')
        await database.execute(
            "INSERT INTO recording_runs(id,session_id,state,started_at,ended_at) "
            "VALUES('run-1',1,'finished',1,2)"
        )
        await database.execute(
            'INSERT INTO recording_parts('
            'id,session_id,run_id,part_index,source_path,final_path,xml_path,'
            'record_start_time,artifact_state,created_at,updated_at) '
            "VALUES(21,1,'run-1',1,?,?,?,1,'ready',1,1)",
            (str(tmp_path / 'p11.mp4'), str(tmp_path / 'p11.mp4'), str(xml_path)),
        )
        protocol = FakeProtocol(archive_response())
        manager, _, _ = make_manager(database, protocol, tmp_path)

        message = await manager.delete_local_task(9, manager_subject='manager')

        assert message == '本地任务及其文件已删除，B 站稿件未作任何修改'
        assert not (tmp_path / 'p11.mp4').exists()
        assert not xml_path.exists()
        assert await database.scalar('SELECT COUNT(*) FROM upload_jobs') == 0
        assert await database.scalar('SELECT COUNT(*) FROM recording_sessions') == 0
        assert protocol.edit_calls == []
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_session_without_upload_job_can_switch_upload_intent(
    tmp_path: Path,
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'db.sqlite3'))
    await database.open()
    try:
        await database.execute(
            "INSERT INTO recording_sessions("
            "id,room_id,broadcast_session_key,state,started_at) "
            "VALUES(1,100,'100:1','closed',1)"
        )
        manager, _, _ = make_manager(
            database, FakeProtocol(archive_response()), tmp_path
        )

        upload_message = await manager.set_session_upload_intent(
            1, 'upload', manager_subject='manager'
        )
        upload_intent = await database.scalar(
            'SELECT upload_intent FROM recording_sessions WHERE id=1'
        )
        skip_message = await manager.set_session_upload_intent(
            1, 'skip', manager_subject='manager'
        )
        skip_intent = await database.scalar(
            'SELECT upload_intent FROM recording_sessions WHERE id=1'
        )

        assert upload_message == '本场录像将在文件就绪后上传'
        assert upload_intent == 'upload'
        assert skip_message == '本场录像已设为不上传'
        assert skip_intent == 'skip'
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_session_without_upload_job_can_be_deleted(tmp_path: Path) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'db.sqlite3'))
    await database.open()
    try:
        video = tmp_path / 'orphan.flv'
        danmaku = tmp_path / 'orphan.xml'
        video.write_bytes(b'video')
        danmaku.write_text('<i />', encoding='utf8')
        await database.execute(
            "INSERT INTO recording_sessions("
            "id,room_id,broadcast_session_key,state,started_at) "
            "VALUES(1,100,'100:1','closed',1)"
        )
        await database.execute(
            "INSERT INTO recording_runs(id,session_id,state,started_at,ended_at) "
            "VALUES('run-1',1,'finished',1,2)"
        )
        await database.execute(
            'INSERT INTO recording_parts('
            'id,session_id,run_id,part_index,source_path,final_path,xml_path,'
            'record_start_time,artifact_state,created_at,updated_at) '
            "VALUES(1,1,'run-1',1,?,?,?,1,'ready',1,1)",
            (str(video), str(video), str(danmaku)),
        )
        manager, _, _ = make_manager(
            database, FakeProtocol(archive_response()), tmp_path
        )

        message = await manager.delete_session(1, manager_subject='manager')

        assert message == '本地场次及其文件已删除，B 站稿件未作任何修改'
        assert not video.exists()
        assert not danmaku.exists()
        assert await database.scalar('SELECT COUNT(*) FROM recording_sessions') == 0
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_failed_session_deletion_resumes_after_restart(tmp_path: Path) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'db.sqlite3'))
    await database.open()
    try:
        video = tmp_path / 'resume-delete.flv'
        video.write_bytes(b'video')
        await database.execute(
            "INSERT INTO recording_sessions("
            "id,room_id,broadcast_session_key,state,started_at) "
            "VALUES(1,100,'100:1','closed',1)"
        )
        await database.execute(
            "INSERT INTO recording_runs(id,session_id,state,started_at,ended_at) "
            "VALUES('run-1',1,'finished',1,2)"
        )
        await database.execute(
            'INSERT INTO recording_parts('
            'id,session_id,run_id,part_index,source_path,final_path,'
            'record_start_time,artifact_state,created_at,updated_at) '
            "VALUES(1,1,'run-1',1,?,?,1,'ready',1,1)",
            (str(video), str(video)),
        )
        manager, _, _ = make_manager(
            database, FakeProtocol(archive_response()), tmp_path
        )
        manager._run_file_deletion = AsyncMock(  # type: ignore[method-assign]
            side_effect=OSError('busy')
        )

        with pytest.raises(UploadTaskActionRejected, match='busy'):
            await manager.delete_session(1, manager_subject='manager')
        failed = await database.fetchone(
            'SELECT deletion_state,deletion_error FROM recording_sessions WHERE id=1'
        )
        assert failed is not None
        assert failed['deletion_state'] == 'failed'
        assert 'busy' in str(failed['deletion_error'])

        manager._run_file_deletion = UploadTaskActionManager._run_file_deletion
        await manager.recover_interrupted()

        assert not video.exists()
        assert await database.scalar('SELECT COUNT(*) FROM recording_sessions') == 0
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_failed_session_deletion_permanently_stops_transcode_repair(
    tmp_path: Path,
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'db.sqlite3'))
    await database.open()
    try:
        await seed_job(database, tmp_path)
        protocol = FakeProtocol(archive_response())
        manager, _, _ = make_manager(database, protocol, tmp_path)
        await manager.request_transcode_repair(9, manager_subject='manager')
        manager._run_file_deletion = AsyncMock(  # type: ignore[method-assign]
            side_effect=OSError('busy')
        )

        with pytest.raises(UploadTaskActionRejected, match='busy'):
            await manager.delete_session(1, manager_subject='manager')

        stopped = await database.fetchone(
            'SELECT operator_paused,repair_state,lease_owner,lease_until '
            'FROM upload_jobs WHERE id=9'
        )
        assert stopped is not None
        assert dict(stopped) == {
            'operator_paused': 1,
            'repair_state': 'failed',
            'lease_owner': None,
            'lease_until': None,
        }

        restarted_protocol = FakeProtocol(archive_response())
        restarted, uploader, _ = make_manager(database, restarted_protocol, tmp_path)
        restarted._run_file_deletion = AsyncMock(  # type: ignore[method-assign]
            side_effect=OSError('still busy')
        )
        await restarted.recover_interrupted()

        assert await restarted.run_once() is None
        assert restarted_protocol.view_calls == []
        assert uploader.calls == []
        assert restarted_protocol.edit_calls == []
    finally:
        await database.close()
