from pathlib import Path
from typing import Any, AsyncIterator, List, Mapping, Optional
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from blrec.bili_upload.accounts import AccountWriteGate
from blrec.bili_upload.danmaku_publish import DanmakuBreaker, DanmakuPublisher
from blrec.bili_upload.database import BiliUploadDatabase
from blrec.bili_upload.errors import (
    BiliApiError,
    DefinitelyNotSent,
    RemoteOutcomeUnknown,
)


class FakeClock:
    def __init__(self, value: float = 1_000) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


@pytest_asyncio.fixture
async def database(tmp_path: Path) -> AsyncIterator[BiliUploadDatabase]:
    value = BiliUploadDatabase(str(tmp_path / 'blrec.sqlite3'))
    await value.open()
    try:
        yield value
    finally:
        await value.close()


async def seed_account(database: BiliUploadDatabase) -> None:
    await database.execute(
        'INSERT OR IGNORE INTO bili_accounts('
        'id,uid,display_name,credential_ciphertext,credential_version,key_id,state,'
        'created_at,updated_at) '
        "VALUES(1,42,'投稿账号',X'00',1,'key','active',1,1)"
    )


async def seed_job(
    database: BiliUploadDatabase,
    job_id: int,
    priorities: List[int],
    *,
    states: Optional[List[str]] = None,
) -> None:
    await seed_account(database)
    await database.execute(
        'INSERT INTO recording_sessions('
        'id,room_id,broadcast_session_key,state,started_at) '
        "VALUES(?,?,?,'closed',1)",
        (job_id, 100 + job_id, '{}:1'.format(100 + job_id)),
    )
    await database.execute(
        'INSERT INTO upload_jobs('
        'id,session_id,account_id,policy_snapshot_json,state,submit_state,'
        'comment_branch_state,danmaku_branch_state,aid,bvid,created_at,updated_at) '
        "VALUES(?,?,1,'{}','approved','confirmed','disabled','publishing',"
        "?, ?,1,1)",
        (job_id, job_id, 300 + job_id, 'BV{}'.format(job_id)),
    )
    part_id = job_id * 10
    await database.execute(
        'INSERT INTO upload_parts('
        'id,job_id,part_index,source_path,final_path,xml_path,artifact_state,'
        'upload_state,danmaku_import_state,remote_filename,cid) '
        "VALUES(?,?,1,? ,? ,? ,'ready','confirmed','completed',?,?)",
        (
            part_id,
            job_id,
            '/rec/p{}.flv'.format(job_id),
            '/rec/p{}.mp4'.format(job_id),
            '/rec/p{}.xml'.format(job_id),
            'remote-p{}'.format(job_id),
            1000 + job_id,
        ),
    )
    item_states = states or ['prepared'] * len(priorities)
    for index, (priority, state) in enumerate(zip(priorities, item_states)):
        await database.execute(
            'INSERT INTO danmaku_items('
            'part_id,xml_identity,original_index,progress_ms,mode,fontsize,color,'
            'content,priority,request_fingerprint,state) '
            'VALUES(?,?,?,?,?,?,?,?,?,?,?)',
            (
                part_id,
                'xml-{}'.format(job_id),
                index,
                index * 1000,
                1,
                25,
                16_777_215,
                'job{}-{}'.format(job_id, index),
                priority,
                'fingerprint-{}-{}'.format(job_id, index),
                state,
            ),
        )


class FakeProtocol:
    def __init__(self) -> None:
        self.calls: List[Mapping[str, Any]] = []
        self.results: List[Any] = []

    async def post_danmaku(
        self, _bundle: object, params: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        self.calls.append(dict(params))
        result = (
            self.results.pop(0)
            if self.results
            else {'code': 0, 'data': {'dmid': 9000 + len(self.calls)}}
        )
        if isinstance(result, BaseException):
            raise result
        return result


async def bundle_loader(_account_id: int) -> object:
    return object()


def publisher(
    database: BiliUploadDatabase,
    protocol: FakeProtocol,
    clock: FakeClock,
    *,
    auth_refresh: Optional[AsyncMock] = None,
) -> DanmakuPublisher:
    return DanmakuPublisher(
        database,
        protocol,
        bundle_loader=bundle_loader,
        account_gates=AccountWriteGate(database),
        interval_seconds=25,
        auth_refresh=auth_refresh,
        worker_id='danmaku-test',
        clock=clock,
    )


@pytest.mark.asyncio
async def test_jobs_round_robin_after_priority_items(
    database: BiliUploadDatabase,
) -> None:
    await seed_job(database, 1, [100, 0, 0])
    await seed_job(database, 2, [100, 0])
    protocol = FakeProtocol()
    clock = FakeClock()
    worker = publisher(database, protocol, clock)

    for _ in range(5):
        await worker.run_once()
        clock.advance(25)

    assert [call['oid'] for call in protocol.calls] == [1001, 1002, 1001, 1002, 1001]
    assert [call['msg'] for call in protocol.calls] == [
        'job1-0',
        'job2-0',
        'job1-1',
        'job2-1',
        'job1-2',
    ]


@pytest.mark.asyncio
async def test_interval_never_goes_below_25_seconds(
    database: BiliUploadDatabase,
) -> None:
    await seed_job(database, 1, [0, 0])
    protocol = FakeProtocol()
    clock = FakeClock()
    worker = publisher(database, protocol, clock)

    await worker.run_once()
    clock.advance(24.9)
    assert await worker.run_once() is None
    assert len(protocol.calls) == 1
    clock.advance(0.1)
    await worker.run_once()
    assert len(protocol.calls) == 2


@pytest.mark.asyncio
async def test_interval_survives_publisher_restart(
    database: BiliUploadDatabase,
) -> None:
    await seed_job(database, 1, [0, 0])
    protocol = FakeProtocol()
    clock = FakeClock()

    await publisher(database, protocol, clock).run_once()
    restarted = publisher(database, protocol, clock)
    clock.advance(24.9)
    assert await restarted.run_once() is None
    clock.advance(0.1)
    await restarted.run_once()

    assert len(protocol.calls) == 2


@pytest.mark.asyncio
async def test_success_uses_recorded_style_and_saves_dmid(
    database: BiliUploadDatabase,
) -> None:
    await seed_job(database, 1, [0])
    protocol = FakeProtocol()
    clock = FakeClock()

    await publisher(database, protocol, clock).run_once()

    assert protocol.calls == [
        {
            'type': 1,
            'oid': 1001,
            'aid': 301,
            'msg': 'job1-0',
            'progress': 0,
            'color': 16_777_215,
            'fontsize': 25,
            'pool': 0,
            'mode': 1,
            'rnd': 1_000_000_000,
        }
    ]
    row = await database.fetchone('SELECT state,dmid FROM danmaku_items')
    assert dict(row) == {'state': 'confirmed', 'dmid': 9001}
    assert (
        await database.scalar('SELECT danmaku_branch_state FROM upload_jobs WHERE id=1')
        == 'completed'
    )


@pytest.mark.asyncio
async def test_definitely_not_sent_retries_safely(database: BiliUploadDatabase) -> None:
    await seed_job(database, 1, [0])
    protocol = FakeProtocol()
    protocol.results = [DefinitelyNotSent('post_danmaku')]
    clock = FakeClock()

    await publisher(database, protocol, clock).run_once()

    row = await database.fetchone(
        'SELECT state,next_attempt_at,lease_owner FROM danmaku_items'
    )
    assert row['state'] == 'prepared'
    assert row['next_attempt_at'] > clock.value
    assert row['lease_owner'] is None


@pytest.mark.asyncio
async def test_repeated_local_send_failures_pause_branch(
    database: BiliUploadDatabase,
) -> None:
    await seed_job(database, 1, [0])
    await database.execute('UPDATE danmaku_items SET attempt=4')
    protocol = FakeProtocol()
    protocol.results = [DefinitelyNotSent('post_danmaku')]

    await publisher(database, protocol, FakeClock()).run_once()

    assert (
        await database.scalar('SELECT danmaku_branch_state FROM upload_jobs WHERE id=1')
        == 'paused'
    )


@pytest.mark.asyncio
async def test_unknown_outcome_never_requeues_automatically(
    database: BiliUploadDatabase,
) -> None:
    await seed_job(database, 1, [0])
    protocol = FakeProtocol()
    protocol.results = [RemoteOutcomeUnknown('post_danmaku')]
    clock = FakeClock()
    worker = publisher(database, protocol, clock)

    await worker.run_once()
    clock.advance(10_000)
    assert await worker.run_once() is None

    assert len(protocol.calls) == 1
    assert await database.scalar('SELECT state FROM danmaku_items') == 'unknown_outcome'


@pytest.mark.asyncio
async def test_crash_interrupted_in_flight_item_is_not_sent_again(
    database: BiliUploadDatabase,
) -> None:
    await seed_job(database, 1, [0], states=['in_flight'])
    protocol = FakeProtocol()

    await publisher(database, protocol, FakeClock()).run_once()

    assert protocol.calls == []
    assert await database.scalar('SELECT state FROM danmaku_items') == 'unknown_outcome'


@pytest.mark.asyncio
async def test_manual_unknown_decisions_are_audited(
    database: BiliUploadDatabase,
) -> None:
    await seed_job(database, 1, [0], states=['unknown_outcome'])
    protocol = FakeProtocol()
    clock = FakeClock()
    worker = publisher(database, protocol, clock)
    item_id = int(await database.scalar('SELECT id FROM danmaku_items'))

    await worker.retry_accept_duplicate_risk(
        item_id, manager_subject='admin', reason='已人工核对，接受重复风险'
    )
    await worker.run_once()

    assert len(protocol.calls) == 1
    assert await database.scalar('SELECT state FROM danmaku_items') == 'confirmed'
    audit = await database.fetchone(
        'SELECT action,target_id,old_state,new_state FROM management_audit'
    )
    assert dict(audit) == {
        'action': 'retry_danmaku_accept_duplicate_risk',
        'target_id': str(item_id),
        'old_state': 'unknown_outcome',
        'new_state': 'prepared',
    }


@pytest.mark.asyncio
async def test_assume_success_resumes_remaining_items(
    database: BiliUploadDatabase,
) -> None:
    await seed_job(database, 1, [0, 0], states=['unknown_outcome', 'prepared'])
    await database.execute(
        "UPDATE upload_jobs SET danmaku_branch_state='paused' WHERE id=1"
    )
    protocol = FakeProtocol()
    clock = FakeClock()
    worker = publisher(database, protocol, clock)
    item_id = int(
        await database.scalar(
            "SELECT id FROM danmaku_items WHERE state='unknown_outcome'"
        )
    )

    await worker.assume_success(
        item_id, manager_subject='admin', reason='已在稿件页面确认存在'
    )
    await worker.run_once()

    assert len(protocol.calls) == 1
    assert (
        await database.scalar('SELECT danmaku_branch_state FROM upload_jobs WHERE id=1')
        == 'completed'
    )


@pytest.mark.asyncio
async def test_rate_limit_backs_off_and_three_distinct_items_pause_account(
    database: BiliUploadDatabase,
) -> None:
    await seed_job(database, 1, [0, 0, 0])
    protocol = FakeProtocol()
    protocol.results = [BiliApiError(36703) for _ in range(3)]
    clock = FakeClock()
    worker = publisher(database, protocol, clock)

    for _ in range(3):
        await worker.run_once()
        clock.advance(100_000)

    assert DanmakuBreaker().delay_after(36703) >= 25
    assert len(protocol.calls) == 3
    row = await database.fetchone(
        'SELECT state,pause_reason FROM bili_accounts WHERE id=1'
    )
    assert row['state'] == 'paused'
    assert '频率' in row['pause_reason']


@pytest.mark.asyncio
async def test_rate_limit_backoff_is_persisted_for_whole_account(
    database: BiliUploadDatabase,
) -> None:
    await seed_job(database, 1, [0, 0])
    protocol = FakeProtocol()
    protocol.results = [BiliApiError(36703)]
    clock = FakeClock()

    await publisher(database, protocol, clock).run_once()

    assert int(
        await database.scalar('SELECT MIN(next_attempt_at) FROM danmaku_items')
    ) >= int(clock.value + 25)


@pytest.mark.asyncio
async def test_rate_limit_history_survives_worker_restart(
    database: BiliUploadDatabase,
) -> None:
    await seed_job(database, 1, [0, 0, 0])
    protocol = FakeProtocol()
    protocol.results = [BiliApiError(36703) for _ in range(3)]
    clock = FakeClock()

    for _ in range(3):
        await publisher(database, protocol, clock).run_once()
        clock.advance(100_000)

    row = await database.fetchone(
        'SELECT state,pause_reason FROM bili_accounts WHERE id=1'
    )
    assert row['state'] == 'paused'
    assert '频率' in row['pause_reason']


@pytest.mark.asyncio
async def test_36704_rechecks_review_and_cid_without_consuming_attempt(
    database: BiliUploadDatabase,
) -> None:
    await seed_job(database, 1, [0])
    protocol = FakeProtocol()
    protocol.results = [BiliApiError(36704)]
    clock = FakeClock()

    await publisher(database, protocol, clock).run_once()

    assert await database.scalar('SELECT cid FROM upload_parts WHERE id=10') is None
    assert (
        await database.scalar('SELECT state FROM upload_jobs WHERE id=1')
        == 'waiting_review'
    )
    item = await database.fetchone('SELECT state,attempt FROM danmaku_items')
    assert dict(item) == {'state': 'prepared', 'attempt': 0}


@pytest.mark.asyncio
async def test_36715_pauses_account_bucket_for_at_least_24_hours(
    database: BiliUploadDatabase,
) -> None:
    await seed_job(database, 1, [0, 0])
    protocol = FakeProtocol()
    protocol.results = [BiliApiError(36715)]
    clock = FakeClock()
    worker = publisher(database, protocol, clock)

    await worker.run_once()

    assert worker.breaker_for(1).next_probe_at >= clock.value + 24 * 3600
    assert int(
        await database.scalar('SELECT MIN(next_attempt_at) FROM danmaku_items')
    ) >= int(clock.value + 24 * 3600)


@pytest.mark.asyncio
async def test_auth_error_requests_fixed_account_refresh(
    database: BiliUploadDatabase,
) -> None:
    await seed_job(database, 1, [0])
    protocol = FakeProtocol()
    protocol.results = [BiliApiError(-101)]
    clock = FakeClock()
    refresh = AsyncMock(return_value=2)

    await publisher(database, protocol, clock, auth_refresh=refresh).run_once()

    refresh.assert_awaited_once_with(1)
    assert await database.scalar('SELECT state FROM danmaku_items') == 'prepared'


@pytest.mark.asyncio
async def test_permanent_content_error_does_not_retry(
    database: BiliUploadDatabase,
) -> None:
    await seed_job(database, 1, [0])
    protocol = FakeProtocol()
    protocol.results = [BiliApiError(36701)]
    clock = FakeClock()

    await publisher(database, protocol, clock).run_once()

    assert (
        await database.scalar('SELECT state FROM danmaku_items') == 'failed_permanent'
    )
    assert (
        await database.scalar('SELECT danmaku_branch_state FROM upload_jobs WHERE id=1')
        == 'failed'
    )
