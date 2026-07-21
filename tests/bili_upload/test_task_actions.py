from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

import pytest

from blrec.bili_upload.accounts import AccountWriteGate
from blrec.bili_upload.database import BiliUploadDatabase, LeaseClaim, LeaseLost
from blrec.bili_upload.deletion_worker import LocalDeletionWorker
from blrec.bili_upload.errors import RemoteOutcomeUnknown
from blrec.bili_upload.task_actions import (
    UploadTaskActionManager,
    UploadTaskActionRejected,
)
from blrec.bili_upload.transcode_remux import RemuxedArtifact
from blrec.bili_upload.upos import FileIdentity
from blrec.control.operations import ControlOperationJournal, ControlStepInput


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


class BlockingRemuxer(FakeRemuxer):
    def __init__(self, directory: Path) -> None:
        super().__init__(directory)
        self.started = threading.Event()
        self.release = threading.Event()

    def remux(self, source_path: str, *, part_id: int) -> RemuxedArtifact:
        self.started.set()
        if not self.release.wait(timeout=5):
            raise RuntimeError('test remux barrier timed out')
        return super().remux(source_path, part_id=part_id)


class BlockingEditProtocol(FakeProtocol):
    def __init__(self, archive: Mapping[str, Any]) -> None:
        super().__init__(archive)
        self.edit_started = asyncio.Event()
        self.edit_release = asyncio.Event()

    async def edit_archive(
        self, _bundle: Any, payload: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        self.edit_calls.append(payload)
        self.edit_started.set()
        await self.edit_release.wait()
        return {'code': 0, 'data': {'aid': 303, 'bvid': 'BVfixture'}}


class UnknownEditProtocol(FakeProtocol):
    async def edit_archive(
        self, _bundle: Any, payload: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        self.edit_calls.append(payload)
        raise RemoteOutcomeUnknown('response lost')


class SimulatedProcessCrash(BaseException):
    pass


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


def repair_reupload_snapshot(*, mode: str = 'original') -> str:
    return json.dumps(
        {
            'format_version': 1,
            'cover_url': 'https://archive.biliimg.com/fixture.jpg',
            'failed_parts': [
                {
                    'local_id': 12,
                    'part_index': 2,
                    'filename': 'remote-12',
                    'cid': 102,
                    'fail_code': 9,
                    'xcode_state': 3,
                    'fail_desc': '服务端转码失败',
                    'mode': mode,
                }
            ],
            'healthy_cids': {'11': 101},
        },
        ensure_ascii=False,
        separators=(',', ':'),
        sort_keys=True,
    )


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
    control_journal: Optional[ControlOperationJournal] = None,
    wake_uploads=lambda: None,
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
        deletion_worker=LocalDeletionWorker(
            database,
            recording_root=recording_root,
            clip_root=recording_root.parent / 'clips',
            clock=lambda: 1_000,
        ),
        control_journal=control_journal,
        wake_uploads=wake_uploads,
        clock=lambda: 1_000,
    )
    return manager, uploader, payload_builder


async def _async_value(value: Any) -> Any:
    return value


async def seed_retry_jobs(
    database: BiliUploadDatabase, tmp_path: Path, *, count: int
) -> None:
    await seed_job(
        database,
        tmp_path,
        state='paused',
        submit_state='prepared',
        second_upload_state='failed',
    )

    def seed(connection) -> None:
        for offset in range(1, count):
            job_id = 9 + offset
            connection.execute(
                'INSERT INTO recording_sessions('
                'id,room_id,broadcast_session_key,state,started_at) '
                "VALUES(?,?,?,'closed',1)",
                (job_id, 100 + job_id, 'retry:{}'.format(job_id)),
            )
            connection.execute(
                'INSERT INTO upload_jobs('
                'id,session_id,account_id,policy_snapshot_json,state,'
                'submit_state,created_at,updated_at) '
                "VALUES(?,?,1,'{}','paused','prepared',1,1)",
                (job_id, job_id),
            )
            connection.execute(
                'INSERT INTO upload_parts('
                'id,job_id,part_index,source_path,artifact_state,upload_state) '
                "VALUES(?,?,1,?,'ready','failed')",
                (10_000 + job_id, job_id, '/fixture/{}.mp4'.format(job_id)),
            )

    await database.write(seed)


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
async def test_job_batch_uses_one_transaction_and_isolates_rejected_items(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
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

        def seed_more(connection) -> None:
            for job_id in range(10, 67):
                connection.execute(
                    'INSERT INTO recording_sessions('
                    'id,room_id,broadcast_session_key,state,started_at) '
                    "VALUES(?,?,?,'closed',1)",
                    (job_id, 100 + job_id, 'batch:{}'.format(job_id)),
                )
                connection.execute(
                    'INSERT INTO upload_jobs('
                    'id,session_id,account_id,policy_snapshot_json,state,'
                    'submit_state,created_at,updated_at) '
                    "VALUES(?,?,1,'{}','paused','prepared',1,1)",
                    (job_id, job_id),
                )
                connection.execute(
                    'INSERT INTO upload_parts('
                    'id,job_id,part_index,source_path,artifact_state,upload_state) '
                    "VALUES(?,?,1,?,'ready','failed')",
                    (1000 + job_id, job_id, '/fixture/{}.mp4'.format(job_id)),
                )
            connection.execute(
                'UPDATE upload_jobs SET lease_owner=?,lease_until=? WHERE id=?',
                ('active-worker', 2_000, 38),
            )

        await database.write(seed_more)
        manager, _, _ = make_manager(
            database, FakeProtocol(archive_response()), tmp_path
        )
        wakeups = []
        manager._wake_uploads = lambda: wakeups.append('wake')  # type: ignore[attr-defined]
        statements: List[str] = []
        await database.read(
            lambda connection: connection.set_trace_callback(statements.append)
        )
        original_write = database.write
        write_calls = 0

        async def counted_write(operation):
            nonlocal write_calls
            write_calls += 1
            return await original_write(operation)

        monkeypatch.setattr(database, 'write', counted_write)

        results = await manager.run_job_batch(
            'retry_failed', tuple(range(9, 67)), manager_subject='manager'
        )

        assert write_calls == 1
        assert len(results) == 58
        assert [item.target_id for item in results if not item.accepted] == [38]
        assert (
            await database.scalar(
                "SELECT COUNT(*) FROM upload_jobs WHERE state='ready'"
            )
            == 57
        )
        assert (
            await database.scalar('SELECT state FROM upload_jobs WHERE id=38')
            == 'paused'
        )
        assert sum(statement == 'BEGIN IMMEDIATE' for statement in statements) == 1
        assert (
            sum(statement.startswith('SAVEPOINT item_') for statement in statements)
            == 58
        )
        assert any(
            statement.startswith('ROLLBACK TO SAVEPOINT item_')
            for statement in statements
        )
        assert wakeups == ['wake']
    finally:
        await database.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('action', 'state', 'submit_state', 'part_state'),
    (
        ('pause_upload', 'ready', 'prepared', 'prepared'),
        ('resume_upload', 'paused', 'prepared', 'prepared'),
        ('repair_transcode', 'waiting_review', 'confirmed', 'confirmed'),
        ('skip_upload', 'ready', 'prepared', 'prepared'),
        ('repost_as_new', 'approved', 'confirmed', 'confirmed'),
    ),
)
async def test_job_batch_supports_each_non_delete_action_in_one_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    action: str,
    state: str,
    submit_state: str,
    part_state: str,
) -> None:
    database = BiliUploadDatabase(str(tmp_path / '{}.sqlite3'.format(action)))
    await database.open()
    try:
        await seed_job(
            database,
            tmp_path,
            state=state,
            submit_state=submit_state,
            second_upload_state=part_state,
        )
        if action in ('pause_upload', 'resume_upload', 'skip_upload'):
            await database.execute(
                "UPDATE upload_parts SET upload_state='prepared',"
                'remote_filename=NULL'
            )
        if action == 'resume_upload':
            await database.execute(
                'UPDATE upload_jobs SET operator_paused=1,'
                "operator_resume_state='ready' WHERE id=9"
            )
        manager, _, _ = make_manager(
            database, FakeProtocol(archive_response()), tmp_path
        )
        original_write = database.write
        write_calls = 0

        async def counted_write(operation):
            nonlocal write_calls
            write_calls += 1
            return await original_write(operation)

        monkeypatch.setattr(database, 'write', counted_write)

        results = await manager.run_job_batch(action, (9,), manager_subject='manager')

        assert write_calls == 1
        assert [(item.target_id, item.accepted) for item in results] == [(9, True)]
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_session_batch_uses_one_transaction_and_preserves_partial_results(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'sessions.sqlite3'))
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
            'INSERT INTO recording_sessions('
            'id,room_id,broadcast_session_key,state,started_at,'
            'upload_resolution_state) '
            "VALUES(2,102,'session:2','closed',1,'job_created')"
        )
        await database.execute(
            'INSERT INTO recording_sessions('
            'id,room_id,broadcast_session_key,state,started_at) '
            "VALUES(3,103,'session:3','closed',1)"
        )
        manager, _, _ = make_manager(
            database, FakeProtocol(archive_response()), tmp_path
        )
        original_write = database.write
        write_calls = 0

        async def counted_write(operation):
            nonlocal write_calls
            write_calls += 1
            return await original_write(operation)

        monkeypatch.setattr(database, 'write', counted_write)

        results = await manager.run_session_batch(
            'set_upload', (1, 2, 3), manager_subject='manager'
        )

        assert write_calls == 1
        assert [(item.target_id, item.accepted) for item in results] == [
            (1, True),
            (2, False),
            (3, True),
        ]
        assert (
            await database.scalar(
                'SELECT upload_decision FROM recording_sessions WHERE id=3'
            )
            == 'upload'
        )
    finally:
        await database.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(('count', 'expected_quantums'), ((101, 2), (201, 3)))
async def test_retry_all_freezes_membership_and_runs_one_transaction_per_quantum(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, count: int, expected_quantums: int
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'upload.sqlite3'))
    journal = ControlOperationJournal(tmp_path / 'control.sqlite3')
    await database.open()
    await journal.open()
    try:
        await seed_retry_jobs(database, tmp_path, count=count)
        wakeups: List[str] = []
        manager, _, _ = make_manager(
            database,
            FakeProtocol(archive_response()),
            tmp_path,
            control_journal=journal,
            wake_uploads=lambda: wakeups.append('wake'),
        )

        admission = await manager.admit_retry_all_failed(manager_subject='manager')

        assert admission.total == count
        assert admission.status == 'accepted'
        assert (
            await database.scalar(
                'SELECT COUNT(*) FROM upload_retry_batch_items '
                'WHERE operation_id=? AND state=\'queued\'',
                (admission.operation_id,),
            )
            == count
        )
        assert (
            await database.scalar(
                "SELECT COUNT(*) FROM upload_jobs WHERE state='paused'"
            )
            == count
        )
        wakeups.clear()

        original_write = database.write
        write_calls = 0

        async def counted_write(operation):
            nonlocal write_calls
            write_calls += 1
            return await original_write(operation)

        monkeypatch.setattr(database, 'write', counted_write)
        processed = []
        while True:
            operation_id = await manager.run_retry_batch_once()
            if operation_id is None:
                break
            processed.append(operation_id)
            snapshot = await journal.get(admission.operation_id)
            assert snapshot is not None
            if snapshot.status == 'succeeded':
                break

        assert processed == [admission.operation_id] * expected_quantums
        assert write_calls == expected_quantums
        assert wakeups == ['wake'] * expected_quantums
        assert (
            await database.scalar(
                "SELECT COUNT(*) FROM upload_jobs WHERE state='ready'"
            )
            == count
        )
        snapshot = await journal.get(admission.operation_id)
        assert snapshot is not None
        assert snapshot.status == 'succeeded'
        assert snapshot.result is not None
        assert {
            key: snapshot.result[key]
            for key in ('processed', 'rejected', 'succeeded', 'total')
        } == {'processed': count, 'rejected': 0, 'succeeded': count, 'total': count}
        assert len(snapshot.steps) == expected_quantums
    finally:
        await journal.close()
        await database.close()


@pytest.mark.asyncio
async def test_retry_all_membership_does_not_drift_and_fences_changed_items(
    tmp_path: Path,
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'upload.sqlite3'))
    journal = ControlOperationJournal(tmp_path / 'control.sqlite3')
    await database.open()
    await journal.open()
    try:
        await seed_retry_jobs(database, tmp_path, count=3)
        await database.write(
            lambda connection: (
                connection.execute(
                    'INSERT INTO recording_sessions('
                    'id,room_id,broadcast_session_key,state,started_at) '
                    "VALUES(1000,1,'not-selected','closed',1)"
                ),
                connection.execute(
                    'INSERT INTO upload_jobs('
                    'id,session_id,account_id,policy_snapshot_json,state,'
                    'submit_state,created_at,updated_at) '
                    "VALUES(1,1000,1,'{}','ready','prepared',1,1)"
                ),
                connection.execute(
                    'INSERT INTO upload_parts('
                    'id,job_id,part_index,source_path,artifact_state,upload_state) '
                    "VALUES(1,1,1,'/fixture/1.mp4','ready','failed')"
                ),
            )
        )
        manager, _, _ = make_manager(
            database,
            FakeProtocol(archive_response()),
            tmp_path,
            control_journal=journal,
        )
        admission = await manager.admit_retry_all_failed(manager_subject='manager')
        assert admission.total == 3
        await database.execute("UPDATE upload_jobs SET state='paused' WHERE id=1")
        await database.execute(
            "UPDATE upload_jobs SET submit_state='unknown_outcome' WHERE id=10"
        )
        await database.execute(
            'UPDATE upload_jobs SET lease_owner=?,lease_until=? WHERE id=11',
            ('active-worker', 2_000),
        )

        assert await manager.run_retry_batch_once() == admission.operation_id

        assert (
            await database.scalar('SELECT state FROM upload_jobs WHERE id=1')
            == 'paused'
        )
        rows = await database.fetchall(
            'SELECT job_id,state,error_code FROM upload_retry_batch_items '
            'WHERE operation_id=? ORDER BY job_id',
            (admission.operation_id,),
        )
        assert [dict(row) for row in rows] == [
            {'job_id': 9, 'state': 'succeeded', 'error_code': None},
            {'job_id': 10, 'state': 'rejected', 'error_code': 'REMOTE_OUTCOME_UNKNOWN'},
            {'job_id': 11, 'state': 'rejected', 'error_code': 'ACTIVE_LEASE'},
        ]
        snapshot = await journal.get(admission.operation_id)
        assert snapshot is not None
        assert snapshot.status == 'succeeded'
        assert snapshot.result is not None
        assert {
            key: snapshot.result[key]
            for key in ('processed', 'rejected', 'succeeded', 'total')
        } == {'processed': 3, 'rejected': 2, 'succeeded': 1, 'total': 3}
    finally:
        await journal.close()
        await database.close()


@pytest.mark.asyncio
async def test_retry_all_duplicate_admission_reuses_frozen_operation(
    tmp_path: Path,
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'upload.sqlite3'))
    journal = ControlOperationJournal(tmp_path / 'control.sqlite3')
    await database.open()
    await journal.open()
    try:
        await seed_retry_jobs(database, tmp_path, count=3)
        manager, _, _ = make_manager(
            database,
            FakeProtocol(archive_response()),
            tmp_path,
            control_journal=journal,
        )
        first = await manager.admit_retry_all_failed(manager_subject='manager')
        await database.execute("UPDATE upload_jobs SET state='paused' WHERE id=9")

        second = await manager.admit_retry_all_failed(manager_subject='manager')

        assert second.operation_id == first.operation_id
        assert second.total == first.total == 3
        assert (
            await database.scalar(
                'SELECT COUNT(*) FROM upload_retry_batches '
                "WHERE state IN ('accepted','running')"
            )
            == 1
        )
        assert (
            await database.scalar(
                'SELECT COUNT(*) FROM upload_retry_batch_items ' 'WHERE operation_id=?',
                (first.operation_id,),
            )
            == 3
        )
    finally:
        await journal.close()
        await database.close()


@pytest.mark.asyncio
async def test_retry_all_recovers_after_restart_without_reprocessing_terminal_items(
    tmp_path: Path,
) -> None:
    upload_path = tmp_path / 'upload.sqlite3'
    control_path = tmp_path / 'control.sqlite3'
    database = BiliUploadDatabase(str(upload_path))
    journal = ControlOperationJournal(control_path)
    await database.open()
    await journal.open()
    await seed_retry_jobs(database, tmp_path, count=101)
    manager, _, _ = make_manager(
        database, FakeProtocol(archive_response()), tmp_path, control_journal=journal
    )
    admission = await manager.admit_retry_all_failed(manager_subject='manager')
    assert await manager.run_retry_batch_once() == admission.operation_id
    await journal.close()
    await database.close()

    restarted_database = BiliUploadDatabase(str(upload_path))
    restarted_journal = ControlOperationJournal(control_path)
    await restarted_database.open()
    await restarted_journal.open()
    try:
        restarted, _, _ = make_manager(
            restarted_database,
            FakeProtocol(archive_response()),
            tmp_path,
            control_journal=restarted_journal,
        )
        await restarted.recover_retry_batches()
        assert await restarted.run_retry_batch_once() == admission.operation_id

        snapshot = await restarted_journal.get(admission.operation_id)
        assert snapshot is not None
        assert snapshot.status == 'succeeded'
        assert snapshot.result is not None
        assert {
            key: snapshot.result[key]
            for key in ('processed', 'rejected', 'succeeded', 'total')
        } == {'processed': 101, 'rejected': 0, 'succeeded': 101, 'total': 101}
        assert (
            await restarted_database.scalar(
                'SELECT COUNT(*) FROM upload_retry_batch_items '
                "WHERE operation_id=? AND state='succeeded'",
                (admission.operation_id,),
            )
            == 101
        )
    finally:
        await restarted_journal.close()
        await restarted_database.close()


@pytest.mark.asyncio
async def test_retry_batch_recovers_upload_orphan_and_fails_control_orphan(
    tmp_path: Path,
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'upload.sqlite3'))
    journal = ControlOperationJournal(tmp_path / 'control.sqlite3')
    await database.open()
    await journal.open()
    try:
        await seed_retry_jobs(database, tmp_path, count=1)
        await database.write(
            lambda connection: (
                connection.execute(
                    'INSERT INTO upload_retry_batches('
                    'operation_id,state,total_items,manager_subject,created_at,'
                    "updated_at) VALUES('upload-orphan','accepted',1,'manager',1,1)"
                ),
                connection.execute(
                    'INSERT INTO upload_retry_batch_items('
                    'operation_id,job_id,state,error_code) '
                    "VALUES('upload-orphan',9,'queued',NULL)"
                ),
            )
        )
        await journal.admit(
            operation_id='control-orphan',
            lane='upload-retry',
            kind='retry-failed',
            target_key='control-orphan',
            steps=(ControlStepInput(key='quantum:0'),),
            result={'processed': 0, 'total': 1, 'succeeded': 0, 'rejected': 0},
        )
        manager, _, _ = make_manager(
            database,
            FakeProtocol(archive_response()),
            tmp_path,
            control_journal=journal,
        )

        await manager.recover_retry_batches()

        upload_orphan = await journal.get('upload-orphan')
        assert upload_orphan is not None
        assert upload_orphan.status == 'accepted'
        assert await manager.run_retry_batch_once() == 'control-orphan'
        control_orphan = await journal.get('control-orphan')
        assert control_orphan is not None
        assert control_orphan.status == 'failed'
        assert control_orphan.error_code == 'UPLOAD_RETRY_BATCH_MISSING'
        assert await manager.run_retry_batch_once() == 'upload-orphan'
        recovered = await journal.get('upload-orphan')
        assert recovered is not None
        assert recovered.status == 'succeeded'
    finally:
        await journal.close()
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
async def test_retryable_failed_jobs_exclude_operator_paused_tasks(
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
        await database.execute('UPDATE upload_jobs SET operator_paused=1 WHERE id=9')
        manager, _, _ = make_manager(
            database, FakeProtocol(archive_response()), tmp_path
        )

        assert await manager.retryable_failed_job_ids() == ()
        with pytest.raises(UploadTaskActionRejected, match='管理员暂停'):
            await manager.retry_failed(9, manager_subject='manager')
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
@pytest.mark.parametrize(
    ('interrupted_state', 'expected_outcome'),
    (('reuploading', 'cancelled_local'), ('editing', 'unknown_terminal')),
)
async def test_repair_recovery_terminally_acknowledges_the_interrupted_owner(
    tmp_path: Path, interrupted_state: str, expected_outcome: str
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'db.sqlite3'))
    await database.open()
    try:
        await seed_job(database, tmp_path)
        await database.execute(
            'UPDATE upload_jobs SET repair_state=?,lease_owner=?,lease_generation=4,'
            'lease_until=? WHERE id=9',
            (interrupted_state, 'stale-worker', 2_000),
        )
        await database.execute(
            'INSERT INTO owner_handoff_outcomes('
            'owner_kind,owner_id,side_effect_key,source_generation,'
            'outcome_state,outcome_json,acknowledged_at) '
            "VALUES('repair',9,'lease:4',0,'in_flight','{}',NULL)"
        )
        if interrupted_state == 'editing':
            await database.execute(
                'INSERT INTO owner_handoff_outcomes('
                'owner_kind,owner_id,side_effect_key,source_generation,'
                'outcome_state,outcome_json,acknowledged_at) '
                "VALUES('repair',9,'archive_edit',0,'in_flight','{}',NULL)"
            )
        manager, _, _ = make_manager(
            database, FakeProtocol(archive_response()), tmp_path
        )

        await manager.recover_interrupted()

        outcomes = await database.fetchall(
            'SELECT side_effect_key,outcome_state,acknowledged_at '
            "FROM owner_handoff_outcomes WHERE owner_kind='repair' "
            'AND owner_id=9 ORDER BY side_effect_key'
        )
        expected = [
            {
                'side_effect_key': 'lease:4',
                'outcome_state': expected_outcome,
                'acknowledged_at': 1000,
            }
        ]
        if interrupted_state == 'editing':
            expected.insert(
                0,
                {
                    'side_effect_key': 'archive_edit',
                    'outcome_state': 'unknown_terminal',
                    'acknowledged_at': 1000,
                },
            )
        assert [dict(row) for row in outcomes] == expected
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_repair_claim_captures_session_generation_and_journals_owner(
    tmp_path: Path,
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'db.sqlite3'))
    await database.open()
    try:
        await seed_job(database, tmp_path)
        manager, _, _ = make_manager(
            database, FakeProtocol(archive_response()), tmp_path
        )
        await manager.request_transcode_repair(9, manager_subject='manager')

        claim = await manager._claim_repair()

        assert claim is not None
        assert claim.cancellation_generation == 0
        outcome = await database.fetchone(
            'SELECT owner_kind,owner_id,side_effect_key,source_generation,'
            'outcome_state,acknowledged_at FROM owner_handoff_outcomes '
            "WHERE owner_kind='repair' AND owner_id=9"
        )
        assert outcome is not None
        assert dict(outcome) == {
            'owner_kind': 'repair',
            'owner_id': 9,
            'side_effect_key': 'lease:1',
            'source_generation': 0,
            'outcome_state': 'in_flight',
            'acknowledged_at': None,
        }
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_cancel_after_archive_intent_commit_clears_intent_without_edit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'db.sqlite3'))
    await database.open()
    intent_committed = threading.Event()
    release_database_thread = threading.Event()
    original_write_sync = database._write_sync

    def block_after_archive_intent_commit(operation: Any) -> Any:
        result = original_write_sync(operation)
        if getattr(operation, '__name__', '') == 'begin':
            intent_committed.set()
            if not release_database_thread.wait(timeout=5):
                raise RuntimeError('test archive intent barrier timed out')
        return result

    monkeypatch.setattr(database, '_write_sync', block_after_archive_intent_commit)
    try:
        await seed_job(database, tmp_path)
        protocol = FakeProtocol(archive_response(second_state='failed'))
        manager, _, _ = make_manager(database, protocol, tmp_path)
        await manager.request_transcode_repair(9, manager_subject='manager')
        repair = asyncio.create_task(manager.run_once())
        committed = await asyncio.get_running_loop().run_in_executor(
            None, intent_committed.wait, 2
        )
        assert committed

        repair.cancel()
        release_database_thread.set()
        with pytest.raises(asyncio.CancelledError):
            await repair

        assert protocol.edit_calls == []
        assert (
            await database.scalar(
                "SELECT COUNT(*) FROM owner_handoff_outcomes "
                "WHERE owner_kind='repair' AND owner_id=9"
            )
            == 0
        )
        job = await database.fetchone(
            'SELECT repair_state,lease_owner FROM upload_jobs WHERE id=9'
        )
        assert job is not None
        assert dict(job) == {'repair_state': 'queued', 'lease_owner': None}

        await manager.delete_session(1, manager_subject='manager')
        worker = manager._deletion_worker
        assert worker is not None
        assert await worker.run_once() == ('session', 1)
        assert await database.scalar('SELECT COUNT(*) FROM recording_sessions') == 0
    finally:
        release_database_thread.set()
        await database.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('temporary_exists', (True, False))
@pytest.mark.parametrize('remote_state', ('processing', 'ready'))
async def test_repair_recovery_restores_interrupted_remux_before_remote_noop(
    tmp_path: Path, temporary_exists: bool, remote_state: str
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'db.sqlite3'))
    await database.open()
    try:
        await seed_job(database, tmp_path)
        original_path = tmp_path / 'p12.mp4'
        original_identity = FileIdentity.from_path(str(original_path)).to_json()
        temporary_path = tmp_path / 'interrupted-remux-12.mp4'
        temporary_path.write_bytes(b'interrupted-remux')
        temporary_identity = FileIdentity.from_path(str(temporary_path)).to_json()
        if not temporary_exists:
            temporary_path.unlink()
        await database.execute(
            "UPDATE upload_jobs SET repair_state='reuploading',"
            "lease_owner='stale-worker',lease_generation=4,lease_until=2000 "
            'WHERE id=9'
        )
        await database.execute(
            "UPDATE upload_parts SET repair_stage='remux',"
            'repair_remux_attempts=1,repair_temp_path=?,repair_original_path=?,'
            'repair_original_identity=?,final_path=?,file_identity=? WHERE id=12',
            (
                str(temporary_path),
                str(original_path),
                original_identity,
                str(temporary_path),
                temporary_identity,
            ),
        )
        await database.execute(
            'INSERT INTO owner_handoff_outcomes('
            'owner_kind,owner_id,side_effect_key,source_generation,'
            'outcome_state,outcome_json,acknowledged_at) '
            "VALUES('repair',9,'lease:4',0,'in_flight','{}',NULL)"
        )
        manager, uploader, _ = make_manager(
            database,
            FakeProtocol(archive_response(second_state=remote_state)),
            tmp_path,
        )

        await manager.recover_interrupted()

        restored = await database.fetchone(
            'SELECT final_path,file_identity,repair_temp_path,'
            'repair_original_path,repair_original_identity '
            'FROM upload_parts WHERE id=12'
        )
        assert restored is not None
        assert dict(restored) == {
            'final_path': str(original_path),
            'file_identity': original_identity,
            'repair_temp_path': None,
            'repair_original_path': None,
            'repair_original_identity': None,
        }
        assert not temporary_path.exists()

        assert await manager.run_once() == 9
        assert uploader.calls == []
        assert (
            await database.scalar('SELECT repair_state FROM upload_jobs WHERE id=9')
            == 'not_needed'
        )
        assert await database.scalar(
            'SELECT final_path FROM upload_parts WHERE id=12'
        ) == str(original_path)
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_repair_recovery_does_not_restore_remux_after_deletion_generation(
    tmp_path: Path,
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'db.sqlite3'))
    await database.open()
    try:
        await seed_job(database, tmp_path)
        original_path = tmp_path / 'p12.mp4'
        original_identity = FileIdentity.from_path(str(original_path)).to_json()
        temporary_path = tmp_path / 'deleting-remux-12.mp4'
        temporary_path.write_bytes(b'interrupted-remux')
        temporary_identity = FileIdentity.from_path(str(temporary_path)).to_json()
        await database.execute(
            "UPDATE recording_sessions SET deletion_state='requested',"
            'cancellation_generation=1 WHERE id=1'
        )
        await database.execute(
            "UPDATE upload_jobs SET repair_state='reuploading',"
            "lease_owner='stale-worker',lease_generation=4,lease_until=2000 "
            'WHERE id=9'
        )
        await database.execute(
            "UPDATE upload_parts SET repair_stage='remux',"
            'repair_remux_attempts=1,repair_temp_path=?,repair_original_path=?,'
            'repair_original_identity=?,final_path=?,file_identity=? WHERE id=12',
            (
                str(temporary_path),
                str(original_path),
                original_identity,
                str(temporary_path),
                temporary_identity,
            ),
        )
        await database.execute(
            'INSERT INTO owner_handoff_outcomes('
            'owner_kind,owner_id,side_effect_key,source_generation,'
            'outcome_state,outcome_json,acknowledged_at) '
            "VALUES('repair',9,'lease:4',0,'in_flight','{}',NULL)"
        )
        manager, _, _ = make_manager(
            database, FakeProtocol(archive_response()), tmp_path
        )

        await manager.recover_interrupted()

        part = await database.fetchone(
            'SELECT final_path,file_identity,repair_temp_path,'
            'repair_original_path,repair_original_identity '
            'FROM upload_parts WHERE id=12'
        )
        assert part is not None
        assert dict(part) == {
            'final_path': str(temporary_path),
            'file_identity': temporary_identity,
            'repair_temp_path': str(temporary_path),
            'repair_original_path': str(original_path),
            'repair_original_identity': original_identity,
        }
        assert temporary_path.exists()
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_repair_restart_before_replacement_resumes_persisted_plan(
    tmp_path: Path,
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'db.sqlite3'))
    await database.open()
    try:
        await seed_job(database, tmp_path)
        manager, _, _ = make_manager(
            database, FakeProtocol(archive_response(second_state='failed')), tmp_path
        )

        async def crash_before_upload(
            part_id: int, *, bundle: Any, claim: LeaseClaim
        ) -> str:
            del part_id, bundle, claim
            raise SimulatedProcessCrash()

        manager._uploader.upload_part = crash_before_upload  # type: ignore
        await manager.request_transcode_repair(9, manager_subject='manager')
        with pytest.raises(SimulatedProcessCrash):
            await manager.run_once()

        assert (
            await database.scalar(
                'SELECT repair_reupload_snapshot_json FROM upload_jobs WHERE id=9'
            )
            == repair_reupload_snapshot()
        )
        interrupted = await database.fetchone(
            'SELECT upload_state,remote_filename,repair_stage '
            'FROM upload_parts WHERE id=12'
        )
        assert interrupted is not None
        assert dict(interrupted) == {
            'upload_state': 'prepared',
            'remote_filename': None,
            'repair_stage': 'original',
        }

        protocol = FakeProtocol(archive_response(second_state='ready'))
        restarted, uploader, _ = make_manager(database, protocol, tmp_path)
        await restarted.recover_interrupted()

        assert await restarted.run_once() == 9
        assert protocol.view_calls == []
        assert uploader.calls == [12]
        assert len(protocol.edit_calls) == 1
        assert (
            await database.scalar(
                'SELECT repair_reupload_snapshot_json FROM upload_jobs WHERE id=9'
            )
            is None
        )
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_repair_restart_after_replacement_edits_without_reupload(
    tmp_path: Path,
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'db.sqlite3'))
    await database.open()
    try:
        await seed_job(database, tmp_path)
        manager, first_uploader, _ = make_manager(
            database, FakeProtocol(archive_response(second_state='failed')), tmp_path
        )

        async def crash_before_edit(
            job_id: int, healthy_cids: Mapping[int, int], cover_url: Optional[str]
        ) -> Mapping[str, Any]:
            del job_id, healthy_cids, cover_url
            raise SimulatedProcessCrash()

        manager._edit_payload_builder = crash_before_edit
        await manager.request_transcode_repair(9, manager_subject='manager')
        with pytest.raises(SimulatedProcessCrash):
            await manager.run_once()
        assert first_uploader.calls == [12]
        assert (
            await database.scalar(
                'SELECT remote_filename FROM upload_parts WHERE id=12'
            )
            == 'replacement-12'
        )

        protocol = FakeProtocol(archive_response(second_state='ready'))
        restarted, uploader, payload_builder = make_manager(
            database, protocol, tmp_path
        )
        await restarted.recover_interrupted()

        assert await restarted.run_once() == 9
        assert protocol.view_calls == []
        assert uploader.calls == []
        assert payload_builder.calls == [{11: 101}]
        assert len(protocol.edit_calls) == 1
        assert (
            await database.scalar(
                'SELECT repair_reupload_snapshot_json FROM upload_jobs WHERE id=9'
            )
            is None
        )
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_repair_restart_normalizes_owned_in_flight_chunk_before_resume(
    tmp_path: Path,
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'db.sqlite3'))
    await database.open()
    try:
        await seed_job(database, tmp_path)
        manager, _, _ = make_manager(
            database, FakeProtocol(archive_response(second_state='failed')), tmp_path
        )

        async def crash_during_chunk(
            part_id: int, *, bundle: Any, claim: LeaseClaim
        ) -> str:
            del bundle

            def persist(connection: Any) -> None:
                connection.execute(
                    "UPDATE upload_parts SET upload_state='uploading',"
                    "remote_filename='replacement-12',upload_session_json='{}' "
                    'WHERE id=?',
                    (part_id,),
                )
                connection.execute(
                    'INSERT INTO upload_chunks('
                    "part_id,chunk_no,offset,size,state,attempt) "
                    "VALUES(?,0,0,8,'in_flight',1)",
                    (part_id,),
                )
                connection.execute(
                    'INSERT INTO owner_handoff_outcomes('
                    'owner_kind,owner_id,side_effect_key,source_generation,'
                    'outcome_state,outcome_json,acknowledged_at) '
                    "VALUES('upos',?,'chunk:0',?,'in_flight','{}',NULL)",
                    (part_id, claim.cancellation_generation),
                )

            await database.write(persist)
            raise SimulatedProcessCrash()

        manager._uploader.upload_part = crash_during_chunk  # type: ignore
        await manager.request_transcode_repair(9, manager_subject='manager')
        with pytest.raises(SimulatedProcessCrash):
            await manager.run_once()

        protocol = FakeProtocol(archive_response(second_state='ready'))
        restarted, uploader, _ = make_manager(database, protocol, tmp_path)
        deletion = restarted._deletion_worker
        assert deletion is not None
        await deletion.recover_interrupted()
        await restarted.recover_interrupted()

        part = await database.fetchone(
            'SELECT upload_state,remote_filename,upload_session_json '
            'FROM upload_parts WHERE id=12'
        )
        assert part is not None
        assert dict(part) == {
            'upload_state': 'prepared',
            'remote_filename': None,
            'upload_session_json': None,
        }
        assert await database.scalar('SELECT COUNT(*) FROM upload_chunks') == 0
        assert (
            await database.scalar(
                "SELECT COUNT(*) FROM owner_handoff_outcomes "
                "WHERE owner_kind='upos' AND outcome_state='in_flight'"
            )
            == 0
        )

        assert await restarted.run_once() == 9
        assert protocol.view_calls == []
        assert uploader.calls == [12]
        assert len(protocol.edit_calls) == 1
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_repair_restart_never_blindly_retries_unknown_completion(
    tmp_path: Path,
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'db.sqlite3'))
    await database.open()
    try:
        await seed_job(database, tmp_path)
        manager, _, _ = make_manager(
            database, FakeProtocol(archive_response(second_state='failed')), tmp_path
        )

        async def crash_during_completion(
            part_id: int, *, bundle: Any, claim: LeaseClaim
        ) -> str:
            del bundle

            def persist(connection: Any) -> None:
                connection.execute(
                    "UPDATE upload_parts SET upload_state='completing',"
                    "remote_filename='replacement-12',upload_session_json='{}' "
                    'WHERE id=?',
                    (part_id,),
                )
                connection.execute(
                    'INSERT INTO owner_handoff_outcomes('
                    'owner_kind,owner_id,side_effect_key,source_generation,'
                    'outcome_state,outcome_json,acknowledged_at) '
                    "VALUES('upos',?,'complete',?,'in_flight','{}',NULL)",
                    (part_id, claim.cancellation_generation),
                )

            await database.write(persist)
            raise SimulatedProcessCrash()

        manager._uploader.upload_part = crash_during_completion  # type: ignore
        await manager.request_transcode_repair(9, manager_subject='manager')
        with pytest.raises(SimulatedProcessCrash):
            await manager.run_once()

        protocol = FakeProtocol(archive_response(second_state='ready'))
        restarted, uploader, _ = make_manager(database, protocol, tmp_path)
        deletion = restarted._deletion_worker
        assert deletion is not None
        await deletion.recover_interrupted()
        await restarted.recover_interrupted()

        job = await database.fetchone(
            'SELECT state,repair_state,operator_paused,lease_owner '
            'FROM upload_jobs WHERE id=9'
        )
        assert job is not None
        assert dict(job) == {
            'state': 'paused',
            'repair_state': 'unknown_outcome',
            'operator_paused': 1,
            'lease_owner': None,
        }
        assert await restarted.run_once() is None
        assert protocol.view_calls == []
        assert uploader.calls == []
        assert protocol.edit_calls == []
        assert (
            await database.scalar(
                "SELECT COUNT(*) FROM owner_handoff_outcomes "
                "WHERE outcome_state='in_flight'"
            )
            == 0
        )

        await deletion.request_session(1, manager_subject='manager')
        for _ in range(8):
            await deletion.run_once()
            if not await database.scalar(
                'SELECT COUNT(*) FROM recording_sessions WHERE id=1'
            ):
                break
        assert (
            await database.scalar('SELECT COUNT(*) FROM recording_sessions WHERE id=1')
            == 0
        )
    finally:
        await database.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('temporary_exists', (True, False))
@pytest.mark.parametrize('replacement_confirmed', (True, False))
async def test_repair_restart_restores_remux_and_resumes_persisted_plan(
    tmp_path: Path, temporary_exists: bool, replacement_confirmed: bool
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'db.sqlite3'))
    await database.open()
    try:
        await seed_job(database, tmp_path)
        original_path = tmp_path / 'p12.mp4'
        original_identity = FileIdentity.from_path(str(original_path)).to_json()
        temporary_path = tmp_path / 'interrupted-remux-12.mp4'
        temporary_path.write_bytes(b'interrupted-remux')
        temporary_identity = FileIdentity.from_path(str(temporary_path)).to_json()
        if not temporary_exists:
            temporary_path.unlink()
        await database.execute(
            "UPDATE upload_jobs SET repair_state='reuploading',"
            'repair_reupload_snapshot_json=?,lease_owner=?,lease_generation=4,'
            'lease_until=2000 WHERE id=9',
            (repair_reupload_snapshot(mode='remux'), 'stale-worker'),
        )
        await database.execute(
            "UPDATE upload_parts SET repair_stage='remux',repair_remux_attempts=1,"
            'repair_temp_path=?,repair_original_path=?,repair_original_identity=?,'
            'final_path=?,file_identity=?,upload_state=?,remote_filename=? WHERE id=12',
            (
                str(temporary_path),
                str(original_path),
                original_identity,
                str(temporary_path),
                temporary_identity,
                'confirmed' if replacement_confirmed else 'prepared',
                'replacement-12' if replacement_confirmed else None,
            ),
        )
        await database.execute(
            'INSERT INTO owner_handoff_outcomes('
            'owner_kind,owner_id,side_effect_key,source_generation,'
            'outcome_state,outcome_json,acknowledged_at) '
            "VALUES('repair',9,'lease:4',0,'in_flight','{}',NULL)"
        )
        remuxer = FakeRemuxer(tmp_path)
        protocol = FakeProtocol(archive_response(second_state='ready'))
        manager, uploader, _ = make_manager(
            database, protocol, tmp_path, remuxer=remuxer
        )

        await manager.recover_interrupted()
        assert await manager.run_once() == 9

        assert not temporary_path.exists()
        assert await database.scalar(
            'SELECT final_path FROM upload_parts WHERE id=12'
        ) == str(original_path)
        assert protocol.view_calls == []
        assert uploader.calls == ([] if replacement_confirmed else [12])
        assert remuxer.calls == (
            [] if replacement_confirmed else [(str(original_path), 12)]
        )
        assert len(protocol.edit_calls) == 1
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_deleting_interrupted_repair_normalizes_upos_before_cleanup(
    tmp_path: Path,
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'db.sqlite3'))
    await database.open()
    try:
        await seed_job(database, tmp_path)
        await database.execute(
            "UPDATE recording_sessions SET deletion_state='requested',"
            'cancellation_generation=1 WHERE id=1'
        )
        await database.execute(
            "UPDATE upload_jobs SET repair_state='reuploading',"
            'repair_reupload_snapshot_json=?,lease_owner=?,lease_generation=4,'
            'lease_until=2000 WHERE id=9',
            (repair_reupload_snapshot(), 'repair-stale'),
        )
        await database.execute(
            "UPDATE upload_parts SET repair_stage='original',upload_state='uploading',"
            "remote_filename='replacement-12',upload_session_json='{}' WHERE id=12"
        )
        await database.execute(
            'INSERT INTO upload_chunks('
            "part_id,chunk_no,offset,size,state,attempt) "
            "VALUES(12,0,0,8,'in_flight',1)"
        )
        await database.execute(
            'INSERT INTO owner_handoff_outcomes('
            'owner_kind,owner_id,side_effect_key,source_generation,'
            'outcome_state,outcome_json,acknowledged_at) '
            "VALUES('repair',9,'lease:4',0,'in_flight','{}',NULL),"
            "('upos',12,'chunk:0',0,'in_flight','{}',NULL)"
        )
        manager, _, _ = make_manager(
            database, FakeProtocol(archive_response()), tmp_path
        )
        deletion = manager._deletion_worker
        assert deletion is not None

        await deletion.recover_interrupted()
        assert await database.scalar('SELECT COUNT(*) FROM upload_chunks') == 0
        await manager.recover_interrupted()

        assert (
            await database.scalar(
                "SELECT COUNT(*) FROM owner_handoff_outcomes "
                "WHERE outcome_state='in_flight'"
            )
            == 0
        )
        for _ in range(8):
            await deletion.run_once()
            if not await database.scalar(
                'SELECT COUNT(*) FROM recording_sessions WHERE id=1'
            ):
                break
        assert (
            await database.scalar('SELECT COUNT(*) FROM recording_sessions WHERE id=1')
            == 0
        )
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_deletion_during_remux_drains_and_removes_artifact_before_ack(
    tmp_path: Path,
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'db.sqlite3'))
    await database.open()
    try:
        await seed_job(database, tmp_path)
        await database.execute(
            "UPDATE upload_parts SET repair_stage='original_waiting_review',"
            'repair_original_attempts=1 WHERE id=12'
        )
        remuxer = BlockingRemuxer(tmp_path)
        manager, _, _ = make_manager(
            database,
            FakeProtocol(archive_response(second_state='failed')),
            tmp_path,
            remuxer=remuxer,
        )
        await manager.request_transcode_repair(9, manager_subject='manager')
        repair = asyncio.create_task(manager.run_once())
        started = await asyncio.get_running_loop().run_in_executor(
            None, remuxer.started.wait, 2
        )
        assert started

        await manager.delete_session(1, manager_subject='manager')
        worker = manager._deletion_worker
        assert worker is not None
        assert await worker.run_once() == ('session', 1)
        assert await database.scalar('SELECT COUNT(*) FROM upload_jobs') == 1

        remuxer.release.set()
        assert await asyncio.wait_for(repair, timeout=2) == 9

        assert not (tmp_path / 'remux-12.mp4').exists()
        owner = await database.fetchone(
            'SELECT outcome_state,acknowledged_at FROM owner_handoff_outcomes '
            "WHERE owner_kind='repair' AND owner_id=9 "
            "AND side_effect_key='lease:1'"
        )
        assert owner is not None
        assert dict(owner) == {
            'outcome_state': 'cancelled_local',
            'acknowledged_at': 1000,
        }
        job = await database.fetchone(
            'SELECT repair_state,lease_owner FROM upload_jobs WHERE id=9'
        )
        assert job is not None
        assert dict(job) == {'repair_state': 'failed', 'lease_owner': None}

        assert await worker.run_once() == ('session', 1)
        assert await database.scalar('SELECT COUNT(*) FROM recording_sessions') == 0
    finally:
        remuxer.release.set()
        await database.close()


@pytest.mark.asyncio
async def test_deletion_during_archive_edit_records_remote_handoff_before_release(
    tmp_path: Path,
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'db.sqlite3'))
    await database.open()
    try:
        await seed_job(database, tmp_path)
        protocol = BlockingEditProtocol(archive_response(second_state='failed'))
        manager, _, _ = make_manager(database, protocol, tmp_path)
        await manager.request_transcode_repair(9, manager_subject='manager')
        repair = asyncio.create_task(manager.run_once())
        await asyncio.wait_for(protocol.edit_started.wait(), timeout=2)

        intent = await database.fetchone(
            'SELECT source_generation,outcome_state,acknowledged_at '
            'FROM owner_handoff_outcomes '
            "WHERE owner_kind='repair' AND owner_id=9 "
            "AND side_effect_key='archive_edit'"
        )
        assert intent is not None
        assert dict(intent) == {
            'source_generation': 0,
            'outcome_state': 'in_flight',
            'acknowledged_at': None,
        }
        await manager.delete_session(1, manager_subject='manager')
        worker = manager._deletion_worker
        assert worker is not None
        assert await worker.run_once() == ('session', 1)
        assert await database.scalar('SELECT COUNT(*) FROM upload_jobs') == 1

        protocol.edit_release.set()
        assert await asyncio.wait_for(repair, timeout=2) == 9

        outcomes = await database.fetchall(
            'SELECT side_effect_key,outcome_state,outcome_json,acknowledged_at '
            "FROM owner_handoff_outcomes WHERE owner_kind='repair' "
            'AND owner_id=9 ORDER BY side_effect_key'
        )
        assert [dict(row) for row in outcomes] == [
            {
                'side_effect_key': 'archive_edit',
                'outcome_state': 'confirmed_success',
                'outcome_json': '{}',
                'acknowledged_at': 1000,
            },
            {
                'side_effect_key': 'lease:1',
                'outcome_state': 'cancelled_local',
                'outcome_json': '{}',
                'acknowledged_at': 1000,
            },
        ]
        job = await database.fetchone(
            'SELECT state,repair_state,lease_owner FROM upload_jobs WHERE id=9'
        )
        assert job is not None
        assert dict(job) == {
            'state': 'paused',
            'repair_state': 'failed',
            'lease_owner': None,
        }
        assert await worker.run_once() == ('session', 1)
        assert await database.scalar('SELECT COUNT(*) FROM recording_sessions') == 0
    finally:
        protocol.edit_release.set()
        await database.close()


@pytest.mark.asyncio
async def test_unknown_archive_edit_is_terminal_and_never_blindly_requeued(
    tmp_path: Path,
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'db.sqlite3'))
    await database.open()
    try:
        await seed_job(database, tmp_path)
        protocol = UnknownEditProtocol(archive_response(second_state='failed'))
        manager, _, _ = make_manager(database, protocol, tmp_path)
        await manager.request_transcode_repair(9, manager_subject='manager')

        assert await manager.run_once() == 9
        await manager.recover_interrupted()
        assert await manager.run_once() is None

        outcomes = await database.fetchall(
            'SELECT side_effect_key,outcome_state,acknowledged_at '
            "FROM owner_handoff_outcomes WHERE owner_kind='repair' "
            'AND owner_id=9 ORDER BY side_effect_key'
        )
        assert [dict(row) for row in outcomes] == [
            {
                'side_effect_key': 'archive_edit',
                'outcome_state': 'unknown_terminal',
                'acknowledged_at': 1000,
            },
            {
                'side_effect_key': 'lease:1',
                'outcome_state': 'unknown_terminal',
                'acknowledged_at': 1000,
            },
        ]
        job = await database.fetchone(
            'SELECT state,repair_state,lease_owner FROM upload_jobs WHERE id=9'
        )
        assert job is not None
        assert dict(job) == {
            'state': 'paused',
            'repair_state': 'unknown_outcome',
            'lease_owner': None,
        }
        assert len(protocol.edit_calls) == 1
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_repair_reuploads_only_failed_part_and_edits_existing_archive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events = []
    monkeypatch.setattr(
        'blrec.bili_upload.task_actions.audit',
        lambda event, **fields: events.append((event, fields)),
    )
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
        assert any(
            event == 'transcode_repair_submitted'
            and fields['job_id'] == 9
            and fields['failed_parts'] == 1
            for event, fields in events
        )
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_repair_uses_submission_order_after_short_parts_are_filtered(
    tmp_path: Path,
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'db.sqlite3'))
    await database.open()
    try:
        await seed_job(database, tmp_path)
        await database.execute('UPDATE upload_parts SET part_index=12 WHERE id=12')
        await database.execute('UPDATE upload_parts SET part_index=2 WHERE id=11')
        protocol = FakeProtocol(archive_response(second_state='failed'))
        manager, uploader, payload_builder = make_manager(database, protocol, tmp_path)

        await manager.request_transcode_repair(9, manager_subject='manager')
        assert await manager.run_once() == 9

        assert uploader.calls == [12]
        assert payload_builder.calls == [{11: 101}]
        job = await database.fetchone(
            'SELECT state,repair_state,repair_error FROM upload_jobs WHERE id=9'
        )
        assert job is not None
        assert dict(job) == {
            'state': 'waiting_review',
            'repair_state': 'waiting_review',
            'repair_error': None,
        }
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
async def test_session_batch_can_queue_manual_danmaku_backfill(tmp_path: Path) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'batch.sqlite3'))
    await database.open()
    try:
        await seed_job(database, tmp_path, state='approved')
        for part_id in (11, 12):
            xml_path = tmp_path / 'batch-p{}.xml'.format(part_id)
            xml_path.write_text('<i><d p="1,1,25,16777215">弹幕</d></i>')
            await database.execute(
                'UPDATE upload_parts SET xml_path=?,cid=? WHERE id=?',
                (str(xml_path), 100 + part_id, part_id),
            )
        manager, _, _ = make_manager(
            database, FakeProtocol(archive_response()), tmp_path
        )

        results = await manager.run_session_batch(
            'backfill_danmaku', (1,), manager_subject='manager'
        )

        assert [(item.target_id, item.accepted, item.message) for item in results] == [
            (1, True, '已排队回灌 2 个分 P 的弹幕')
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
async def test_skip_preupload_removes_confirmed_remote_state_but_keeps_files(
    tmp_path: Path,
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'db.sqlite3'))
    await database.open()
    try:
        await seed_job(
            database, tmp_path, state='waiting_artifacts', submit_state='prepared'
        )
        await database.execute(
            'UPDATE upload_jobs SET preupload_finalized=0 WHERE id=9'
        )
        manager, _, _ = make_manager(
            database, FakeProtocol(archive_response()), tmp_path
        )

        message = await manager.skip_upload(9, manager_subject='manager')

        assert message == '该场录像已设为不上传'
        assert await database.scalar('SELECT COUNT(*) FROM upload_jobs') == 0
        assert (tmp_path / 'p11.mp4').exists()
        assert (tmp_path / 'p12.mp4').exists()
        session = await database.fetchone(
            'SELECT upload_intent,upload_decision,upload_resolution_state '
            'FROM recording_sessions WHERE id=1'
        )
        assert session is not None
        assert dict(session) == {
            'upload_intent': 'skip',
            'upload_decision': 'skip',
            'upload_resolution_state': 'not_requested',
        }
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
            'UPDATE upload_jobs SET policy_snapshot_json=?,'
            "submission_verification_state='passed',"
            'submission_verified_at=123,submission_verification_json=?,'
            'repair_reupload_snapshot_json=? WHERE id=9',
            (
                json.dumps(
                    {
                        'auto_comment': True,
                        'danmaku_backfill': True,
                        'collection_section_id': 88,
                    }
                ),
                '{"state":"passed"}',
                repair_reupload_snapshot(),
            ),
        )
        manager, _, _ = make_manager(
            database, FakeProtocol(archive_response()), tmp_path
        )

        message = await manager.repost_as_new(9, manager_subject='manager')

        assert message == '已保留原稿件记录，并重新排队投稿为新稿件'
        job = await database.fetchone(
            'SELECT state,submit_state,aid,bvid,comment_branch_state,'
            'danmaku_branch_state,collection_branch_state,'
            'submission_verification_state,submission_verified_at,'
            'submission_verification_json,repair_reupload_snapshot_json '
            'FROM upload_jobs WHERE id=9'
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
            'submission_verification_state': 'pending',
            'submission_verified_at': None,
            'submission_verification_json': None,
            'repair_reupload_snapshot_json': None,
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

        assert message == '已排队删除本地任务及文件'
        assert (tmp_path / 'p11.mp4').exists()
        worker = manager._deletion_worker
        assert worker is not None
        await worker.run_once()
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

        assert upload_message == '本场录像将在录制结束后创建上传任务'
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

        assert message == '已排队删除本地场次及文件'
        assert video.exists()
        worker = manager._deletion_worker
        assert worker is not None
        await worker.run_once()
        assert not video.exists()
        assert not danmaku.exists()
        assert await database.scalar('SELECT COUNT(*) FROM recording_sessions') == 0
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_session_deletion_waits_for_owned_highlight_work(tmp_path: Path) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'db.sqlite3'))
    await database.open()
    try:
        video = tmp_path / 'source.flv'
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
        await database.execute(
            'INSERT INTO highlight_clips('
            'id,room_id,source_session_id,name,requested_start_ms,'
            'requested_end_ms,state,lease_owner,lease_until,created_at,updated_at) '
            "VALUES(1,100,1,'高光',0,1000,'processing','owner',2000,1,1)"
        )
        await database.execute(
            'INSERT INTO highlight_clip_sources('
            'clip_id,part_id,ordinal,requested_start_ms,requested_end_ms) '
            'VALUES(1,1,1,0,1000)'
        )
        manager, _, _ = make_manager(
            database, FakeProtocol(archive_response()), tmp_path
        )

        message = await manager.delete_session(1, manager_subject='manager')
        worker = manager._deletion_worker
        assert worker is not None
        await worker.run_once()

        assert message == '已排队删除本地场次及文件'
        assert video.exists()
        assert (
            await database.scalar(
                'SELECT deletion_state FROM recording_sessions WHERE id=1'
            )
            == 'requested'
        )
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_deleting_highlight_upload_keeps_original_recording(
    tmp_path: Path,
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'db.sqlite3'))
    await database.open()
    try:
        original = tmp_path / 'original.flv'
        output = tmp_path / 'highlight.mp4'
        xml = tmp_path / 'highlight.xml'
        original.write_bytes(b'original')
        output.write_bytes(b'highlight')
        xml.write_text('<i/>', encoding='utf8')
        await database.execute(
            'INSERT INTO bili_accounts('
            'id,uid,display_name,credential_ciphertext,credential_version,key_id,'
            'state,created_at,updated_at) '
            "VALUES(1,42,'账号',X'00',1,'k','active',1,1)"
        )
        await database.execute(
            'INSERT INTO recording_sessions('
            'id,room_id,broadcast_session_key,state,started_at,source_kind) '
            "VALUES(1,100,'100:1','closed',1,'live'),"
            "(2,100,'highlight:1','closed',2,'highlight')"
        )
        await database.execute(
            'INSERT INTO recording_runs(id,session_id,state,started_at,ended_at) '
            "VALUES('live-run',1,'finished',1,2),"
            "('highlight-run',2,'finished',2,3)"
        )
        await database.execute(
            'INSERT INTO recording_parts('
            'id,session_id,run_id,part_index,source_path,final_path,xml_path,'
            'record_start_time,artifact_state,created_at,updated_at) '
            "VALUES(1,1,'live-run',1,?,?,NULL,1,'ready',1,1),"
            "(2,2,'highlight-run',1,?,?,?,2,'ready',2,2)",
            (str(original), str(original), str(output), str(output), str(xml)),
        )
        await database.execute(
            'INSERT INTO highlight_clips('
            'id,room_id,source_session_id,upload_session_id,name,'
            'requested_start_ms,requested_end_ms,output_video_path,'
            'output_xml_path,state,created_at,updated_at) '
            "VALUES(1,100,1,2,'高光',0,1000,?,?,'ready',1,1)",
            (str(output), str(xml)),
        )
        await database.execute(
            'INSERT INTO highlight_clip_sources('
            'clip_id,part_id,ordinal,requested_start_ms,requested_end_ms) '
            'VALUES(1,1,1,0,1000)'
        )
        await database.execute(
            'INSERT INTO upload_jobs('
            'id,session_id,account_id,policy_snapshot_json,state,submit_state,'
            'created_at,updated_at) '
            "VALUES(1,2,1,'{}','paused','prepared',2,2)"
        )
        await database.execute(
            'INSERT INTO upload_parts('
            'id,job_id,part_index,source_path,final_path,xml_path,'
            'artifact_state) '
            "VALUES(1,1,1,?,?,?,'ready')",
            (str(output), str(output), str(xml)),
        )
        manager, _, _ = make_manager(
            database, FakeProtocol(archive_response()), tmp_path
        )

        await manager.delete_session(2, manager_subject='manager')
        worker = manager._deletion_worker
        assert worker is not None
        await worker.run_once()

        assert original.exists()
        assert not output.exists()
        assert not xml.exists()
        assert (
            await database.scalar('SELECT COUNT(*) FROM recording_sessions WHERE id=1')
            == 1
        )
        assert (
            await database.scalar('SELECT COUNT(*) FROM recording_parts WHERE id=1')
            == 1
        )
        assert await database.scalar('SELECT COUNT(*) FROM highlight_clips') == 0
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
        worker = manager._deletion_worker
        assert worker is not None
        worker._unlink = lambda _path: (_ for _ in ()).throw(OSError('busy'))

        await manager.delete_session(1, manager_subject='manager')
        await worker.run_once()
        failed = await database.fetchone(
            'SELECT deletion_state,deletion_error FROM recording_sessions WHERE id=1'
        )
        assert failed is not None
        assert failed['deletion_state'] == 'failed'
        assert failed['deletion_error'] == 'unlink_OSError'

        worker._unlink = lambda path: path.unlink()
        await worker.recover_interrupted()
        await worker.run_once()

        assert not video.exists()
        assert await database.scalar('SELECT COUNT(*) FROM recording_sessions') == 0
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_session_deletion_cancels_queued_repair_without_remote_request(
    tmp_path: Path,
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'db.sqlite3'))
    await database.open()
    try:
        await seed_job(database, tmp_path)
        protocol = FakeProtocol(archive_response())
        manager, _, _ = make_manager(database, protocol, tmp_path)
        await manager.request_transcode_repair(9, manager_subject='manager')
        await manager.delete_session(1, manager_subject='manager')
        worker = manager._deletion_worker
        assert worker is not None
        await worker.run_once()

        assert await database.scalar('SELECT COUNT(*) FROM upload_jobs') == 0
        assert protocol.view_calls == []
        assert protocol.edit_calls == []
    finally:
        await database.close()
