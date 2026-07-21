import asyncio
import json
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

import blrec.bili_upload.collection_publish as collection_publish_module
from blrec.bili_upload.accounts import AccountWriteGate, CredentialVersionChanged
from blrec.bili_upload.collection_publish import CollectionPublisher
from blrec.bili_upload.database import BiliUploadDatabase
from blrec.bili_upload.errors import BiliApiError, RemoteOutcomeUnknown


class FakeProtocol:
    def __init__(self, error: Exception = None) -> None:
        self.error = error
        self.calls = []

    async def add_collection_episode(self, bundle: Any, **values: Any) -> Any:
        self.calls.append((bundle, values))
        if self.error is not None:
            raise self.error
        return {'code': 0}


async def seed_job(
    database: BiliUploadDatabase, *, branch_state: str = 'pending', snapshot: Any = None
) -> None:
    await database.execute(
        'INSERT INTO bili_accounts('
        'id,uid,display_name,credential_ciphertext,credential_version,key_id,'
        "state,created_at,updated_at) VALUES(1,42,'账号',X'00',1,'k','active',1,1)"
    )
    await database.execute(
        'INSERT INTO recording_sessions('
        'id,room_id,broadcast_session_key,state,started_at) '
        "VALUES(1,100,'session','closed',1)"
    )
    policy = snapshot or {
        'format_version': 4,
        'account_id': 1,
        'title': '测试稿件',
        'collection_season_id': 20,
        'collection_section_id': 21,
    }
    await database.execute(
        'INSERT INTO upload_jobs('
        'id,session_id,account_id,policy_snapshot_json,state,submit_state,'
        'comment_branch_state,danmaku_branch_state,collection_branch_state,'
        'aid,bvid,created_at,updated_at) '
        "VALUES(1,1,1,?,'approved','confirmed','disabled','disabled',"
        "?,303,'BVfixture',1,1)",
        (json.dumps(policy), branch_state),
    )
    await database.execute(
        'INSERT INTO upload_parts('
        'id,job_id,part_index,source_path,final_path,artifact_state,'
        "upload_state,remote_filename,cid) VALUES(1,1,1,'a','a','ready',"
        "'confirmed','p1',101)"
    )


async def async_value(value: Any) -> Any:
    return 'bundle-{}'.format(value)


@pytest.mark.asyncio
async def test_publisher_adds_the_approved_archive_using_its_first_cid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events = []
    monkeypatch.setattr(
        'blrec.bili_upload.collection_publish.audit',
        lambda event, **fields: events.append((event, fields)),
    )
    database = BiliUploadDatabase(str(tmp_path / 'upload.sqlite3'))
    await database.open()
    protocol = FakeProtocol()
    try:
        await seed_job(database)
        publisher = CollectionPublisher(
            database,
            protocol,
            bundle_loader=async_value,
            account_gates=AccountWriteGate(database),
            clock=lambda: 1000,
        )

        await publisher.create(1)

        assert protocol.calls == [
            (
                'bundle-1',
                {'section_id': 21, 'aid': 303, 'cid': 101, 'title': '测试稿件'},
            )
        ]
        row = await database.fetchone(
            'SELECT collection_branch_state,collection_error '
            'FROM upload_jobs WHERE id=1'
        )
        assert row is not None
        assert dict(row) == {
            'collection_branch_state': 'completed',
            'collection_error': None,
        }
        assert any(
            event == 'collection_episode_added'
            and fields['job_id'] == 1
            and fields['section_id'] == 21
            for event, fields in events
        )
    finally:
        await database.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('error', 'message'),
    (
        (
            RemoteOutcomeUnknown('add_collection_episode'),
            '加入合集结果未知，请先在 B 站确认后再重试',
        ),
        (BiliApiError(-400, '合集已满'), '加入合集失败：合集已满'),
    ),
)
async def test_publisher_keeps_collection_failure_separate_from_archive_state(
    tmp_path: Path, error: Exception, message: str
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'upload.sqlite3'))
    await database.open()
    try:
        await seed_job(database)
        publisher = CollectionPublisher(
            database,
            FakeProtocol(error),
            bundle_loader=async_value,
            account_gates=AccountWriteGate(database),
            clock=lambda: 1000,
        )

        with pytest.raises(type(error)):
            await publisher.create(1)

        row = await database.fetchone(
            'SELECT state,collection_branch_state,collection_error '
            'FROM upload_jobs WHERE id=1'
        )
        assert row is not None
        assert dict(row) == {
            'state': 'approved',
            'collection_branch_state': 'failed',
            'collection_error': message,
        }
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_recovery_marks_an_interrupted_collection_request_as_uncertain(
    tmp_path: Path,
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'upload.sqlite3'))
    await database.open()
    try:
        await seed_job(database, branch_state='running')
        publisher = CollectionPublisher(
            database,
            FakeProtocol(),
            bundle_loader=async_value,
            account_gates=AccountWriteGate(database),
            clock=lambda: 1000,
        )

        assert await publisher.recover_interrupted() == 1

        row = await database.fetchone(
            'SELECT collection_branch_state,collection_error '
            'FROM upload_jobs WHERE id=1'
        )
        assert row is not None
        assert dict(row) == {
            'collection_branch_state': 'failed',
            'collection_error': '上次加入合集时程序中断，请先在 B 站确认后再重试',
        }
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_publisher_waits_for_account_gate_and_rechecks_credentials(
    tmp_path: Path,
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'upload.sqlite3'))
    await database.open()
    try:
        await seed_job(database)
        protocol = FakeProtocol()
        gates = AccountWriteGate(database)
        publisher = CollectionPublisher(
            database,
            protocol,
            bundle_loader=async_value,
            account_gates=gates,
            clock=lambda: 1000,
        )
        gate = gates.for_account(1)

        async with gate.hold(1):
            task = asyncio.create_task(publisher.create(1))
            lock = gates._locks[1]

            async def wait_for_gate_waiter() -> None:
                while not lock._waiters:
                    if task.done():
                        await task
                        raise AssertionError('publisher did not wait for account gate')
                    await asyncio.sleep(0)

            await asyncio.wait_for(wait_for_gate_waiter(), timeout=2)
            assert not task.done()
            assert protocol.calls == []
            await database.execute(
                'UPDATE bili_accounts SET credential_version=2 WHERE id=1'
            )

        with pytest.raises(CredentialVersionChanged):
            await task
        assert protocol.calls == []
        assert (
            await database.scalar(
                'SELECT collection_branch_state FROM upload_jobs WHERE id=1'
            )
            == 'failed'
        )
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_publisher_installs_one_operation_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'upload.sqlite3'))
    await database.open()
    deadlines = []

    @contextmanager
    def capture_deadline(seconds: float):
        deadlines.append(seconds)
        yield

    monkeypatch.setattr(
        collection_publish_module, 'protocol_request_deadline', capture_deadline
    )
    try:
        await seed_job(database)
        publisher = CollectionPublisher(
            database,
            FakeProtocol(),
            bundle_loader=async_value,
            account_gates=AccountWriteGate(database),
            operation_timeout_seconds=0.01,
            clock=lambda: 1000,
        )

        await publisher.create(1)

        assert deadlines == [0.01]
    finally:
        await database.close()
