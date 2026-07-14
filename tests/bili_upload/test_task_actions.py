from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

import pytest

from blrec.bili_upload.accounts import AccountWriteGate
from blrec.bili_upload.database import BiliUploadDatabase, LeaseClaim, LeaseLost
from blrec.bili_upload.task_actions import (
    UploadTaskActionManager,
    UploadTaskActionRejected,
)
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
    database: BiliUploadDatabase, protocol: FakeProtocol
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
        clock=lambda: 1_000,
    )
    return manager, uploader, payload_builder


async def _async_value(value: Any) -> Any:
    return value


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
        manager, _, _ = make_manager(database, FakeProtocol(archive_response()))

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
        manager, _, _ = make_manager(database, FakeProtocol(archive_response()))

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
        manager, _, _ = make_manager(database, FakeProtocol(archive_response()))

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
        manager, uploader, payload_builder = make_manager(database, protocol)

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
            'SELECT id,remote_filename,cid,transcode_state,transcode_fail_code '
            'FROM upload_parts ORDER BY id'
        )
        assert [dict(row) for row in parts] == [
            {
                'id': 11,
                'remote_filename': 'remote-11',
                'cid': 101,
                'transcode_state': 'ready',
                'transcode_fail_code': 0,
            },
            {
                'id': 12,
                'remote_filename': 'replacement-12',
                'cid': None,
                'transcode_state': 'processing',
                'transcode_fail_code': None,
            },
        ]
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
        manager, uploader, _ = make_manager(database, protocol)

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
        manager, uploader, _ = make_manager(database, LeaseStealingProtocol(database))

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
