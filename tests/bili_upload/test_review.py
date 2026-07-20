import asyncio
import json
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence, Set

import pytest

from blrec.bili_upload.archive_reads import ArchiveReadService
from blrec.bili_upload.database import BiliUploadDatabase
from blrec.bili_upload.review import ReviewWatcher


class Clock:
    def __init__(self, now: int = 1000) -> None:
        self.now = now

    def __call__(self) -> float:
        return float(self.now)


class FakeProtocol:
    def __init__(
        self,
        responses: Mapping[int, Mapping[str, Any]],
        details: Optional[Mapping[str, Mapping[str, Any]]] = None,
    ) -> None:
        self.responses = dict(responses)
        self.details = dict(details or {})
        self.calls = []
        self.detail_calls = []

    async def list_archives(
        self, bundle: object, params: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        account_id = int(bundle)
        self.calls.append((account_id, dict(params)))
        return self.responses[account_id]

    async def archive_view(
        self, bundle: object, params: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        self.detail_calls.append((int(bundle), dict(params)))
        return self.details[str(params['bvid'])]


class FakePagingProtocol(FakeProtocol):
    def __init__(
        self,
        pages: Mapping[int, Mapping[str, Any]],
        details: Optional[Mapping[str, Mapping[str, Any]]] = None,
    ) -> None:
        super().__init__({1: pages[1]}, details)
        self.pages = dict(pages)

    async def list_archives(
        self, bundle: object, params: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        account_id = int(bundle)
        self.calls.append((account_id, dict(params)))
        return self.pages[int(params['pn'])]


class FakeBranch:
    def __init__(self, *, failing_jobs: Iterable[int] = ()) -> None:
        self.calls = []
        self._failing_jobs: Set[int] = set(failing_jobs)

    async def create(self, job_id: int) -> None:
        self.calls.append(job_id)
        if job_id in self._failing_jobs:
            raise RuntimeError('branch creation failed')


async def open_database(path: Path) -> BiliUploadDatabase:
    database = BiliUploadDatabase(str(path))
    await database.open()
    return database


async def seed_waiting_job(
    database: BiliUploadDatabase,
    *,
    job_id: int = 1,
    account_id: int = 1,
    account_uid: int = 42,
    aid: int = 303,
    bvid: str = 'BVfixture',
    filenames: Sequence[str] = ('p1', 'p2'),
    comment_state: str = 'pending',
    danmaku_state: str = 'pending',
    collection_state: str = 'disabled',
) -> None:
    await database.execute(
        'INSERT OR IGNORE INTO bili_accounts('
        'id,uid,display_name,credential_ciphertext,credential_version,key_id,'
        'state,created_at,updated_at) VALUES(?,?,?,X\'00\',1,\'k\',\'active\',1,1)',
        (account_id, account_uid, '账号{}'.format(account_id)),
    )
    await database.execute(
        'INSERT INTO recording_sessions('
        'id,room_id,broadcast_session_key,state,started_at) '
        'VALUES(?,?,?,\'closed\',1)',
        (job_id, 100 + job_id, 'session-{}'.format(job_id)),
    )
    await database.execute(
        'INSERT INTO upload_jobs('
        'id,session_id,account_id,policy_snapshot_json,state,submit_state,'
        'comment_branch_state,danmaku_branch_state,collection_branch_state,'
        'aid,bvid,created_at,updated_at) '
        'VALUES(?,?,?,\'{}\',\'waiting_review\',\'confirmed\',?,?,?,?,?,1,1)',
        (
            job_id,
            job_id,
            account_id,
            comment_state,
            danmaku_state,
            collection_state,
            aid,
            bvid,
        ),
    )
    for part_index, filename in enumerate(filenames, 1):
        await database.execute(
            'INSERT INTO upload_parts('
            'id,job_id,part_index,source_path,final_path,artifact_state,'
            'upload_state,remote_filename) '
            'VALUES(?,?,?,?,?,\'ready\',\'confirmed\',?)',
            (
                job_id * 100 + part_index,
                job_id,
                part_index,
                '/source/{}'.format(filename),
                '/final/{}'.format(filename),
                filename,
            ),
        )


def archive_response(
    *,
    aid: int = 303,
    bvid: str = 'BVfixture',
    owner_uid: int = 0,
    state: int = 0,
    state_desc: str = '',
    reject_reason: str = '',
) -> Mapping[str, Any]:
    entry: Dict[str, Any] = {
        'Archive': {
            'aid': aid,
            'bvid': bvid,
            'mid': owner_uid,
            'state': state,
            'state_desc': state_desc,
            'reject_reason': reject_reason,
        }
    }
    return {'code': 0, 'data': {'arc_audits': [entry]}}


def archive_page(entries: Sequence[Mapping[str, Any]]) -> Mapping[str, Any]:
    return {'code': 0, 'data': {'arc_audits': list(entries)}}


def archive_entry(aid: int, bvid: str) -> Mapping[str, Any]:
    return {'Archive': {'aid': aid, 'bvid': bvid, 'state': 0}}


def archive_detail(
    videos: Sequence[Mapping[str, Any]], *, aid: int = 303, bvid: str = 'BVfixture'
) -> Mapping[str, Any]:
    return {
        'code': 0,
        'data': {'archive': {'aid': aid, 'bvid': bvid}, 'videos': list(videos)},
    }


def video(
    filename: str,
    cid: int,
    page: int,
    *,
    fail_code: int = 0,
    xcode_state: int = 0,
    fail_desc: str = '',
) -> Mapping[str, Any]:
    return {
        'filename': filename,
        'cid': cid,
        'page': page,
        'failCode': fail_code,
        'xcodeState': xcode_state,
        'failDesc': fail_desc,
    }


def watcher(
    database: BiliUploadDatabase,
    response: Mapping[str, Any],
    *,
    detail: Optional[Mapping[str, Any]] = None,
    clock: Optional[Clock] = None,
    comment_branch: Optional[FakeBranch] = None,
    danmaku_branch: Optional[FakeBranch] = None,
    collection_branch: Optional[FakeBranch] = None,
) -> ReviewWatcher:
    protocol = FakeProtocol(
        {1: response},
        {
            'BVfixture': detail
            or archive_detail([video('p1', 101, 1), video('p2', 202, 2)])
        },
    )
    return ReviewWatcher(
        database,
        protocol,
        bundle_loader=lambda account_id: async_value(account_id),
        comment_branch=comment_branch or FakeBranch(),
        danmaku_branch=danmaku_branch or FakeBranch(),
        collection_branch=collection_branch or FakeBranch(),
        clock=clock or Clock(),
    )


async def async_value(value: object) -> object:
    return value


@pytest.mark.asyncio
async def test_recover_legacy_page_order_pause_is_exact_and_never_reuploads(
    tmp_path: Path,
) -> None:
    database = await open_database(tmp_path / 'upload.sqlite3')
    reason = '远端分 P 页码与本地顺序不一致'
    try:
        for job_id in range(1, 6):
            await seed_waiting_job(
                database,
                job_id=job_id,
                aid=300 + job_id,
                bvid='BV{}'.format(job_id),
                filenames=('p1',),
            )
            await database.execute(
                "UPDATE upload_jobs SET state='paused',review_reason=? WHERE id=?",
                (reason, job_id),
            )
        await database.execute(
            "UPDATE upload_jobs SET lease_owner='old',lease_until=9999,"
            'next_attempt_at=9999 WHERE id=1'
        )
        await database.execute('UPDATE upload_jobs SET operator_paused=1 WHERE id=2')
        await database.execute('UPDATE upload_jobs SET bvid=NULL WHERE id=3')
        await database.execute(
            "UPDATE upload_jobs SET review_reason='另一种暂停原因' WHERE id=4"
        )
        await database.execute(
            "UPDATE upload_jobs SET submit_state='prepared' WHERE id=5"
        )
        review = watcher(database, archive_response())

        assert await review.recover_legacy_page_order_pauses() == 1

        recovered = await database.fetchone(
            'SELECT state,submit_state,review_reason,lease_owner,lease_until,'
            'next_attempt_at FROM upload_jobs WHERE id=1'
        )
        assert recovered is not None
        assert dict(recovered) == {
            'state': 'waiting_review',
            'submit_state': 'confirmed',
            'review_reason': None,
            'lease_owner': None,
            'lease_until': None,
            'next_attempt_at': 0,
        }
        untouched = await database.fetchall(
            'SELECT id,state FROM upload_jobs WHERE id BETWEEN 2 AND 5 ORDER BY id'
        )
        assert [(int(row['id']), str(row['state'])) for row in untouched] == [
            (2, 'paused'),
            (3, 'paused'),
            (4, 'paused'),
            (5, 'paused'),
        ]
        assert (
            await database.scalar(
                'SELECT COUNT(*) FROM upload_chunks WHERE part_id=101'
            )
            == 0
        )
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_review_binds_cids_by_remote_filename_not_array_position(
    tmp_path: Path,
) -> None:
    database = await open_database(tmp_path / 'upload.sqlite3')
    comment = FakeBranch()
    danmaku = FakeBranch()
    try:
        await seed_waiting_job(database)
        review = watcher(
            database,
            archive_response(),
            detail=archive_detail([video('p2', 202, 2), video('p1', 101, 1)]),
            comment_branch=comment,
            danmaku_branch=danmaku,
        )

        assert await review.run_once() == 1

        rows = await database.fetchall(
            'SELECT part_index,cid FROM upload_parts WHERE job_id=1 ORDER BY part_index'
        )
        assert {int(row['part_index']): int(row['cid']) for row in rows} == {
            1: 101,
            2: 202,
        }
        assert await database.scalar('SELECT state FROM upload_jobs WHERE id=1') == (
            'approved'
        )
        assert (
            await database.scalar('SELECT approved_at FROM upload_jobs WHERE id=1')
            == 1000
        )
        assert comment.calls == [1]
        assert danmaku.calls == [1]
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_review_uses_submission_order_after_short_parts_are_filtered(
    tmp_path: Path,
) -> None:
    database = await open_database(tmp_path / 'upload.sqlite3')
    try:
        await seed_waiting_job(database, filenames=('p2', 'p12'))
        await database.execute(
            'UPDATE upload_parts SET part_index=12 WHERE job_id=1 AND id=102'
        )
        await database.execute(
            'UPDATE upload_parts SET part_index=2 WHERE job_id=1 AND id=101'
        )
        await database.execute(
            'UPDATE upload_jobs SET policy_snapshot_json=? WHERE id=1',
            (
                json.dumps(
                    {
                        'format_version': 4,
                        'part_titles': ['P{}'.format(index) for index in range(1, 13)],
                        'recording_part_indexes': list(range(1, 13)),
                    }
                ),
            ),
        )
        review = watcher(
            database,
            archive_response(),
            detail=archive_detail(
                [
                    dict(video('p2', 202, 1), title='P2'),
                    dict(video('p12', 1212, 2), title='P12'),
                ]
            ),
        )

        assert await review.run_once() == 1

        job = await database.fetchone(
            'SELECT state,submission_verification_state FROM upload_jobs WHERE id=1'
        )
        assert job is not None
        assert dict(job) == {
            'state': 'approved',
            'submission_verification_state': 'passed',
        }
        rows = await database.fetchall(
            'SELECT part_index,cid FROM upload_parts WHERE job_id=1 ORDER BY part_index'
        )
        assert [dict(row) for row in rows] == [
            {'part_index': 2, 'cid': 202},
            {'part_index': 12, 'cid': 1212},
        ]
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_review_persists_submission_setting_verification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    audit_events = []
    monkeypatch.setattr(
        'blrec.bili_upload.review.audit',
        lambda event, **fields: audit_events.append((event, fields)),
    )
    database = await open_database(tmp_path / 'upload.sqlite3')
    try:
        await seed_waiting_job(database, filenames=('p1',))
        policy_snapshot = {
            'format_version': 4,
            'title': '测试直播 录播',
            'description': '主播：测试主播',
            'part_titles': ['P1'],
            'tid': 17,
            'tags': '直播,录播',
            'copyright': 1,
            'is_only_self': True,
            'publish_dynamic': False,
            'no_reprint': True,
            'up_selection_reply': True,
            'up_close_reply': False,
            'up_close_danmu': False,
            'creation_statement_id': -1,
        }
        await database.execute(
            'UPDATE upload_jobs SET policy_snapshot_json=?,scheduled_publish_at=? '
            'WHERE id=1',
            (json.dumps(policy_snapshot), 10_000),
        )
        detail = archive_detail([dict(video('p1', 101, 1), title='P1')])
        detail['data']['archive'].update(
            {
                'title': '测试直播 录播',
                'desc': '主播：测试主播',
                'tid': 17,
                'tag': '录播,直播',
                'copyright': 1,
                'is_only_self': 1,
                'no_disturbance': 1,
                'no_reprint': 1,
                'up_selection_reply': True,
                'up_close_reply': False,
                'up_close_danmu': False,
                'creation_statement': {'id': -1},
                'dtime': 10_000,
            }
        )
        review = watcher(database, archive_response(), detail=detail)

        assert await review.run_once() == 1

        row = await database.fetchone(
            'SELECT state,submission_verification_state,submission_verified_at,'
            'submission_verification_json FROM upload_jobs WHERE id=1'
        )
        assert row is not None
        assert row['state'] == 'approved'
        assert row['submission_verification_state'] == 'passed'
        assert row['submission_verified_at'] == 1000
        verification = json.loads(str(row['submission_verification_json']))
        assert verification['mismatches'] == []
        assert verification['differences'] == {}
        assert verification['unverifiable'] == []
        assert 'title' in verification['checked']
        assert '测试直播' not in str(row['submission_verification_json'])
        assert any(
            event == 'submission_verified'
            and fields['job_id'] == 1
            and fields['state'] == 'passed'
            for event, fields in audit_events
        )
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_review_does_not_audit_verification_lost_to_concurrent_state_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    audit_events = []
    monkeypatch.setattr(
        'blrec.bili_upload.review.audit',
        lambda event, **fields: audit_events.append((event, fields)),
    )
    database = await open_database(tmp_path / 'upload.sqlite3')

    class StateChangingProtocol(FakeProtocol):
        async def archive_view(
            self, bundle: object, params: Mapping[str, Any]
        ) -> Mapping[str, Any]:
            await database.execute("UPDATE upload_jobs SET state='paused' WHERE id=1")
            return await super().archive_view(bundle, params)

    protocol = StateChangingProtocol(
        {1: archive_response()}, {'BVfixture': archive_detail([video('p1', 101, 1)])}
    )
    try:
        await seed_waiting_job(database, filenames=('p1',))
        review = ReviewWatcher(
            database,
            protocol,
            bundle_loader=lambda account_id: async_value(account_id),
            comment_branch=FakeBranch(),
            danmaku_branch=FakeBranch(),
            collection_branch=FakeBranch(),
            clock=Clock(),
        )

        assert await review.run_once() == 0
        row = await database.fetchone(
            'SELECT state,submission_verification_state FROM upload_jobs WHERE id=1'
        )
        assert row is not None
        assert dict(row) == {
            'state': 'paused',
            'submission_verification_state': 'pending',
        }
        assert all(event != 'submission_verified' for event, _ in audit_events)
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_private_archive_state_is_complete_and_binds_cid(tmp_path: Path) -> None:
    database = await open_database(tmp_path / 'upload.sqlite3')
    try:
        await seed_waiting_job(database, filenames=('p1',))
        review = watcher(
            database,
            archive_response(state=-50, state_desc='稿件仅自己可见'),
            detail=archive_detail([{'filename': 'p1', 'cid': 101, 'index': 1}]),
        )

        assert await review.run_once() == 1
        assert await database.scalar('SELECT state FROM upload_jobs WHERE id=1') == (
            'approved'
        )
        assert (
            await database.scalar('SELECT cid FROM upload_parts WHERE job_id=1') == 101
        )
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_review_records_transcode_failures_without_an_extra_remote_check(
    tmp_path: Path,
) -> None:
    database = await open_database(tmp_path / 'upload.sqlite3')
    try:
        await seed_waiting_job(database)
        review = watcher(
            database,
            archive_response(),
            detail=archive_detail(
                [
                    video('p1', 101, 1),
                    video(
                        'p2',
                        202,
                        2,
                        fail_code=9,
                        xcode_state=3,
                        fail_desc='服务端转码失败',
                    ),
                ]
            ),
        )

        assert await review.run_once() == 1

        rows = await database.fetchall(
            'SELECT part_index,transcode_state,transcode_fail_code,'
            'transcode_fail_desc FROM upload_parts WHERE job_id=1 '
            'ORDER BY part_index'
        )
        assert [dict(row) for row in rows] == [
            {
                'part_index': 1,
                'transcode_state': 'ready',
                'transcode_fail_code': 0,
                'transcode_fail_desc': None,
            },
            {
                'part_index': 2,
                'transcode_state': 'failed',
                'transcode_fail_code': 9,
                'transcode_fail_desc': '服务端转码失败',
            },
        ]
        job = await database.fetchone(
            'SELECT state,repair_state,repair_message FROM upload_jobs WHERE id=1'
        )
        assert job is not None
        assert dict(job) == {
            'state': 'waiting_review',
            'repair_state': 'queued',
            'repair_message': '发现 1 个分 P 转码失败，等待自动修复',
        }
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_review_escalates_original_reupload_failure_to_remux(
    tmp_path: Path,
) -> None:
    database = await open_database(tmp_path / 'upload.sqlite3')
    try:
        await seed_waiting_job(database)
        await database.execute(
            "UPDATE upload_jobs SET repair_state='waiting_review' WHERE id=1"
        )
        await database.execute(
            "UPDATE upload_parts SET repair_stage='original_waiting_review',"
            'repair_original_attempts=1 WHERE job_id=1 AND part_index=2'
        )
        review = watcher(
            database,
            archive_response(),
            detail=archive_detail(
                [video('p1', 101, 1), video('p2', 202, 2, fail_code=9, xcode_state=3)]
            ),
        )

        assert await review.run_once() == 1

        job = await database.fetchone(
            'SELECT state,repair_state,repair_message FROM upload_jobs WHERE id=1'
        )
        assert job is not None
        assert dict(job) == {
            'state': 'waiting_review',
            'repair_state': 'queued',
            'repair_message': '原文件重传后仍有 1 个分 P 转码失败，等待重新封装',
        }
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_review_stops_after_remux_also_fails(tmp_path: Path) -> None:
    database = await open_database(tmp_path / 'upload.sqlite3')
    try:
        await seed_waiting_job(database, filenames=('p1',))
        await database.execute(
            "UPDATE upload_jobs SET repair_state='waiting_review' WHERE id=1"
        )
        await database.execute(
            "UPDATE upload_parts SET repair_stage='remux_waiting_review',"
            'repair_original_attempts=1,repair_remux_attempts=1 '
            'WHERE job_id=1'
        )
        review = watcher(
            database,
            archive_response(),
            detail=archive_detail([video('p1', 101, 1, fail_code=9, xcode_state=3)]),
        )

        assert await review.run_once() == 1

        job = await database.fetchone(
            'SELECT state,repair_state,repair_error FROM upload_jobs WHERE id=1'
        )
        assert job is not None
        assert dict(job) == {
            'state': 'paused',
            'repair_state': 'failed',
            'repair_error': '重新封装后 B 站转码仍失败，已停止自动修复',
        }
        assert (
            await database.scalar(
                'SELECT repair_stage FROM upload_parts WHERE job_id=1'
            )
            == 'exhausted'
        )
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_review_finishes_a_waiting_transcode_repair(tmp_path: Path) -> None:
    database = await open_database(tmp_path / 'upload.sqlite3')
    try:
        await seed_waiting_job(database, filenames=('p1',))
        await database.execute(
            "UPDATE upload_jobs SET repair_state='waiting_review',"
            "repair_message='等待重新审核' WHERE id=1"
        )
        review = watcher(
            database, archive_response(), detail=archive_detail([video('p1', 101, 1)])
        )

        await review.run_once()

        row = await database.fetchone(
            'SELECT state,repair_state,repair_message,repair_error '
            'FROM upload_jobs WHERE id=1'
        )
        assert row is not None
        assert dict(row) == {
            'state': 'approved',
            'repair_state': 'completed',
            'repair_message': '转码修复已通过审核',
            'repair_error': None,
        }
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_owner_mismatch_pauses_without_creating_children(tmp_path: Path) -> None:
    database = await open_database(tmp_path / 'upload.sqlite3')
    comment = FakeBranch()
    danmaku = FakeBranch()
    try:
        await seed_waiting_job(database, filenames=('p1',))
        review = watcher(
            database,
            archive_response(owner_uid=99),
            comment_branch=comment,
            danmaku_branch=danmaku,
        )

        await review.run_once()

        row = await database.fetchone(
            'SELECT state,review_reason FROM upload_jobs WHERE id=1'
        )
        assert row is not None
        assert row['state'] == 'paused'
        assert '账号归属' in str(row['review_reason'])
        assert comment.calls == []
        assert danmaku.calls == []
    finally:
        await database.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'videos',
    (
        [video('p1', 101, 1)],
        [video('p1', 101, 1), video('p2', 202, 2), video('p3', 303, 3)],
        [video('p1', 101, 1), video('p1', 102, 1)],
        [video('p1', 101, 2), video('p2', 202, 1)],
    ),
    ids=('missing', 'extra', 'duplicate', 'wrong-page'),
)
async def test_review_pauses_on_part_mapping_mismatch(
    tmp_path: Path, videos: Sequence[Mapping[str, Any]]
) -> None:
    database = await open_database(tmp_path / 'upload.sqlite3')
    try:
        await seed_waiting_job(database)
        review = watcher(database, archive_response(), detail=archive_detail(videos))

        await review.run_once()

        row = await database.fetchone(
            'SELECT state,review_reason,submission_verification_state '
            'FROM upload_jobs WHERE id=1'
        )
        assert row is not None
        assert row['state'] == 'paused'
        assert '分 P' in str(row['review_reason'])
        assert row['submission_verification_state'] == 'failed'
        assert (
            await database.scalar(
                'SELECT COUNT(*) FROM upload_parts WHERE job_id=1 AND cid IS NOT NULL'
            )
            == 0
        )
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_rejected_archive_stores_public_reason(tmp_path: Path) -> None:
    database = await open_database(tmp_path / 'upload.sqlite3')
    comment = FakeBranch()
    danmaku = FakeBranch()
    try:
        await seed_waiting_job(database, filenames=('p1',))
        review = watcher(
            database,
            archive_response(state=-2, reject_reason='画面内容不符合规范'),
            comment_branch=comment,
            danmaku_branch=danmaku,
        )

        await review.run_once()

        row = await database.fetchone(
            'SELECT state,review_reason FROM upload_jobs WHERE id=1'
        )
        assert row is not None
        assert dict(row) == {'state': 'rejected', 'review_reason': '画面内容不符合规范'}
        assert comment.calls == []
        assert danmaku.calls == []
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_rejected_repair_exposes_the_review_reason(tmp_path: Path) -> None:
    database = await open_database(tmp_path / 'upload.sqlite3')
    try:
        await seed_waiting_job(database, filenames=('p1',))
        await database.execute(
            "UPDATE upload_jobs SET repair_state='waiting_review' WHERE id=1"
        )
        review = watcher(
            database, archive_response(state=-2, reject_reason='修复后的分 P 仍未通过')
        )

        await review.run_once()

        row = await database.fetchone(
            'SELECT state,repair_state,repair_error FROM upload_jobs WHERE id=1'
        )
        assert row is not None
        assert dict(row) == {
            'state': 'rejected',
            'repair_state': 'failed',
            'repair_error': '修复后的分 P 仍未通过',
        }
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_still_reviewing_is_polled_at_most_once_per_interval(
    tmp_path: Path,
) -> None:
    database = await open_database(tmp_path / 'upload.sqlite3')
    clock = Clock()
    protocol = FakeProtocol({1: archive_response(state=-1, state_desc='审核中')})
    try:
        await seed_waiting_job(database, filenames=('p1',))
        review = ReviewWatcher(
            database,
            protocol,
            bundle_loader=lambda account_id: async_value(account_id),
            comment_branch=FakeBranch(),
            danmaku_branch=FakeBranch(),
            collection_branch=FakeBranch(),
            clock=clock,
        )

        await review.run_once()
        await review.run_once()
        clock.now += 899
        await review.run_once()

        assert len(protocol.calls) == 1
        assert await database.scalar('SELECT state FROM upload_jobs WHERE id=1') == (
            'waiting_review'
        )

        clock.now += 1
        await review.run_once()
        assert len(protocol.calls) == 2
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_comment_branch_failure_does_not_suppress_danmaku_branch(
    tmp_path: Path,
) -> None:
    database = await open_database(tmp_path / 'upload.sqlite3')
    comment = FakeBranch(failing_jobs=(1,))
    danmaku = FakeBranch()
    try:
        await seed_waiting_job(database, filenames=('p1',))
        review = watcher(
            database,
            archive_response(),
            detail=archive_detail([video('p1', 101, 1)]),
            comment_branch=comment,
            danmaku_branch=danmaku,
        )

        await review.run_once()

        row = await database.fetchone(
            'SELECT state,comment_branch_state,danmaku_branch_state '
            'FROM upload_jobs WHERE id=1'
        )
        assert row is not None
        assert dict(row) == {
            'state': 'approved',
            'comment_branch_state': 'failed',
            'danmaku_branch_state': 'pending',
        }
        assert comment.calls == [1]
        assert danmaku.calls == [1]
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_collection_branch_starts_only_after_review_and_cid_binding(
    tmp_path: Path,
) -> None:
    database = await open_database(tmp_path / 'upload.sqlite3')
    collection = FakeBranch()
    try:
        await seed_waiting_job(
            database,
            filenames=('p1',),
            comment_state='disabled',
            danmaku_state='disabled',
            collection_state='pending',
        )
        review = watcher(
            database,
            archive_response(),
            detail=archive_detail([video('p1', 101, 1)]),
            collection_branch=collection,
        )

        assert await review.run_once() == 1

        assert collection.calls == [1]
        assert await database.scalar('SELECT cid FROM upload_parts WHERE job_id=1') == (
            101
        )
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_collection_branch_failure_does_not_fail_an_approved_archive(
    tmp_path: Path,
) -> None:
    database = await open_database(tmp_path / 'upload.sqlite3')
    collection = FakeBranch(failing_jobs=(1,))
    try:
        await seed_waiting_job(
            database,
            filenames=('p1',),
            comment_state='disabled',
            danmaku_state='disabled',
            collection_state='pending',
        )
        review = watcher(
            database,
            archive_response(),
            detail=archive_detail([video('p1', 101, 1)]),
            collection_branch=collection,
        )

        await review.run_once()

        row = await database.fetchone(
            'SELECT state,collection_branch_state,collection_error '
            'FROM upload_jobs WHERE id=1'
        )
        assert row is not None
        assert dict(row) == {
            'state': 'approved',
            'collection_branch_state': 'failed',
            'collection_error': '审核已通过，但加入合集失败',
        }
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_scheduled_archive_is_explained_while_waiting(tmp_path: Path) -> None:
    database = await open_database(tmp_path / 'upload.sqlite3')
    try:
        await seed_waiting_job(database, filenames=('p1',))
        review = watcher(database, archive_response(state=-40))

        assert await review.run_once() == 0

        row = await database.fetchone(
            'SELECT state,review_reason FROM upload_jobs WHERE id=1'
        )
        assert row is not None
        assert dict(row) == {'state': 'waiting_review', 'review_reason': '等待定时发布'}
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_identity_conflict_pauses_instead_of_binding_archive(
    tmp_path: Path,
) -> None:
    database = await open_database(tmp_path / 'upload.sqlite3')
    try:
        await seed_waiting_job(database, filenames=('p1',))
        review = watcher(database, archive_response(bvid='BVdifferent'))

        await review.run_once()

        row = await database.fetchone(
            'SELECT state,review_reason FROM upload_jobs WHERE id=1'
        )
        assert row is not None
        assert row['state'] == 'paused'
        assert '稿件标识' in str(row['review_reason'])
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_waiting_jobs_are_grouped_into_one_read_per_account(
    tmp_path: Path,
) -> None:
    database = await open_database(tmp_path / 'upload.sqlite3')
    protocol = FakeProtocol(
        {
            1: {'code': 0, 'data': {'arc_audits': []}},
            2: {'code': 0, 'data': {'arc_audits': []}},
        }
    )
    try:
        await seed_waiting_job(database, job_id=1, account_id=1, account_uid=42)
        await seed_waiting_job(
            database, job_id=2, account_id=1, account_uid=42, aid=304, bvid='BVsecond'
        )
        await seed_waiting_job(
            database, job_id=3, account_id=2, account_uid=84, aid=305, bvid='BVthird'
        )
        review = ReviewWatcher(
            database,
            protocol,
            bundle_loader=lambda account_id: async_value(account_id),
            comment_branch=FakeBranch(),
            danmaku_branch=FakeBranch(),
            collection_branch=FakeBranch(),
            clock=Clock(),
        )

        assert await review.run_once() == 0

        assert [call[0] for call in protocol.calls] == [1, 2]
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_review_finds_an_archive_on_the_second_page(tmp_path: Path) -> None:
    database = await open_database(tmp_path / 'upload.sqlite3')
    first_page = [
        archive_entry(1_000 + index, 'BVfill{}'.format(index)) for index in range(50)
    ]
    protocol = FakePagingProtocol(
        {1: archive_page(first_page), 2: archive_response()},
        {'BVfixture': archive_detail([video('p1', 101, 1)])},
    )
    try:
        await seed_waiting_job(database, filenames=('p1',))
        review = ReviewWatcher(
            database,
            protocol,
            bundle_loader=lambda account_id: async_value(account_id),
            comment_branch=FakeBranch(),
            danmaku_branch=FakeBranch(),
            collection_branch=FakeBranch(),
            clock=Clock(),
        )

        assert await review.run_once() == 1
        assert [call[1]['pn'] for call in protocol.calls] == [1, 2]
        assert await database.scalar('SELECT state FROM upload_jobs WHERE id=1') == (
            'approved'
        )
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_review_paginates_until_multiple_jobs_are_all_found(
    tmp_path: Path,
) -> None:
    database = await open_database(tmp_path / 'upload.sqlite3')
    first_page = [archive_entry(303, 'BVfixture')]
    first_page.extend(
        archive_entry(1_000 + index, 'BVfill{}'.format(index)) for index in range(49)
    )
    protocol = FakePagingProtocol(
        {
            1: archive_page(first_page),
            2: archive_page([archive_entry(304, 'BVsecond')]),
        },
        {
            'BVfixture': archive_detail([video('p1', 101, 1)]),
            'BVsecond': archive_detail([video('p1', 201, 1)], aid=304, bvid='BVsecond'),
        },
    )
    try:
        await seed_waiting_job(database, filenames=('p1',))
        await seed_waiting_job(
            database,
            job_id=2,
            account_id=1,
            account_uid=42,
            aid=304,
            bvid='BVsecond',
            filenames=('p1',),
        )
        review = ReviewWatcher(
            database,
            protocol,
            bundle_loader=lambda account_id: async_value(account_id),
            comment_branch=FakeBranch(),
            danmaku_branch=FakeBranch(),
            collection_branch=FakeBranch(),
            clock=Clock(),
        )

        assert await review.run_once() == 2
        assert [call[1]['pn'] for call in protocol.calls] == [1, 2]
        states = await database.fetchall('SELECT state FROM upload_jobs ORDER BY id')
        assert [str(row['state']) for row in states] == ['approved', 'approved']
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_review_stops_after_a_first_page_match(tmp_path: Path) -> None:
    database = await open_database(tmp_path / 'upload.sqlite3')
    protocol = FakePagingProtocol(
        {1: archive_response()}, {'BVfixture': archive_detail([video('p1', 101, 1)])}
    )
    try:
        await seed_waiting_job(database, filenames=('p1',))
        review = ReviewWatcher(
            database,
            protocol,
            bundle_loader=lambda account_id: async_value(account_id),
            comment_branch=FakeBranch(),
            danmaku_branch=FakeBranch(),
            collection_branch=FakeBranch(),
            clock=Clock(),
        )

        assert await review.run_once() == 1
        assert protocol.calls == [
            (1, {'status': 'is_pubing,pubed,not_pubed', 'pn': 1, 'ps': 50})
        ]
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_review_stops_when_the_remote_repeats_a_full_page(tmp_path: Path) -> None:
    database = await open_database(tmp_path / 'upload.sqlite3')
    repeated = archive_page(
        [archive_entry(1_000 + index, 'BVfill{}'.format(index)) for index in range(50)]
    )
    protocol = FakePagingProtocol({1: repeated, 2: repeated})
    try:
        await seed_waiting_job(database, filenames=('p1',))
        review = ReviewWatcher(
            database,
            protocol,
            bundle_loader=lambda account_id: async_value(account_id),
            comment_branch=FakeBranch(),
            danmaku_branch=FakeBranch(),
            collection_branch=FakeBranch(),
            clock=Clock(),
        )

        assert await review.run_once() == 0
        assert [call[1]['pn'] for call in protocol.calls] == [1, 2]
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_review_page_timeout_does_not_block_the_next_account(
    tmp_path: Path,
) -> None:
    database = await open_database(tmp_path / 'upload.sqlite3')
    blocked = asyncio.Event()
    comments = FakeBranch()

    class BlockingAccountProtocol(FakeProtocol):
        async def list_archives(
            self, bundle: object, params: Mapping[str, Any]
        ) -> Mapping[str, Any]:
            account_id = int(bundle)
            self.calls.append((account_id, dict(params)))
            if account_id == 1:
                await blocked.wait()
            return self.responses[account_id]

    protocol = BlockingAccountProtocol(
        {
            1: {'code': 0, 'data': {'arc_audits': []}},
            2: archive_response(aid=305, bvid='BVthird'),
        },
        {'BVthird': archive_detail([video('p1', 3051, 1)], aid=305, bvid='BVthird')},
    )
    reader = ArchiveReadService(protocol)
    try:
        await seed_waiting_job(database, filenames=('p1',))
        await seed_waiting_job(
            database,
            job_id=3,
            account_id=2,
            account_uid=84,
            aid=305,
            bvid='BVthird',
            filenames=('p1',),
        )
        review = ReviewWatcher(
            database,
            protocol,
            bundle_loader=lambda account_id: async_value(account_id),
            archive_reader=reader,
            comment_branch=comments,
            danmaku_branch=FakeBranch(),
            collection_branch=FakeBranch(),
            read_timeout_seconds=0.01,
            clock=Clock(),
        )

        assert await review.run_once() == 1

        states = await database.fetchall(
            'SELECT id,state,submission_verification_state '
            'FROM upload_jobs ORDER BY id'
        )
        assert [dict(row) for row in states] == [
            {
                'id': 1,
                'state': 'waiting_review',
                'submission_verification_state': 'pending',
            },
            {'id': 3, 'state': 'approved', 'submission_verification_state': 'failed'},
        ]
        assert comments.calls == [3]
        assert [call[0] for call in protocol.calls] == [1, 2]
    finally:
        blocked.set()
        await reader.close()
        await database.close()


@pytest.mark.asyncio
async def test_review_detail_timeout_starts_no_local_transition(tmp_path: Path) -> None:
    database = await open_database(tmp_path / 'upload.sqlite3')
    blocked = asyncio.Event()
    comments = FakeBranch()

    class BlockingDetailProtocol(FakeProtocol):
        async def archive_view(
            self, bundle: object, params: Mapping[str, Any]
        ) -> Mapping[str, Any]:
            self.detail_calls.append((int(bundle), dict(params)))
            await blocked.wait()
            return self.details[str(params['bvid'])]

    protocol = BlockingDetailProtocol(
        {1: archive_response()}, {'BVfixture': archive_detail([video('p1', 101, 1)])}
    )
    reader = ArchiveReadService(protocol)
    try:
        await seed_waiting_job(database, filenames=('p1',))
        review = ReviewWatcher(
            database,
            protocol,
            bundle_loader=lambda account_id: async_value(account_id),
            archive_reader=reader,
            comment_branch=comments,
            danmaku_branch=FakeBranch(),
            collection_branch=FakeBranch(),
            read_timeout_seconds=0.01,
            clock=Clock(),
        )

        assert await review.run_once() == 0

        row = await database.fetchone(
            'SELECT state,submission_verification_state,comment_branch_state '
            'FROM upload_jobs WHERE id=1'
        )
        assert row is not None
        assert dict(row) == {
            'state': 'waiting_review',
            'submission_verification_state': 'pending',
            'comment_branch_state': 'pending',
        }
        assert comments.calls == []
    finally:
        blocked.set()
        await reader.close()
        await database.close()


@pytest.mark.asyncio
async def test_review_reads_at_most_twenty_archive_pages(tmp_path: Path) -> None:
    database = await open_database(tmp_path / 'upload.sqlite3')

    class EndlessPagingProtocol(FakeProtocol):
        async def list_archives(
            self, bundle: object, params: Mapping[str, Any]
        ) -> Mapping[str, Any]:
            account_id = int(bundle)
            page_number = int(params['pn'])
            self.calls.append((account_id, dict(params)))
            return archive_page(
                [
                    archive_entry(
                        page_number * 1_000 + index,
                        'BV{}-{}'.format(page_number, index),
                    )
                    for index in range(50)
                ]
            )

    protocol = EndlessPagingProtocol({1: archive_page([])})
    reader = ArchiveReadService(protocol)
    try:
        await seed_waiting_job(database, filenames=('p1',))
        review = ReviewWatcher(
            database,
            protocol,
            bundle_loader=lambda account_id: async_value(account_id),
            archive_reader=reader,
            comment_branch=FakeBranch(),
            danmaku_branch=FakeBranch(),
            collection_branch=FakeBranch(),
            clock=Clock(),
        )

        assert await review.run_once() == 0
        assert len(protocol.calls) == 20
    finally:
        await reader.close()
        await database.close()


@pytest.mark.asyncio
async def test_approved_pending_branches_recover_once_without_resubmission(
    tmp_path: Path,
) -> None:
    database = await open_database(tmp_path / 'upload.sqlite3')
    protocol = FakeProtocol(
        {1: archive_response()}, {'BVfixture': archive_detail([video('p1', 101, 1)])}
    )
    protocol.submit_calls = []

    class StatefulBranch(FakeBranch):
        def __init__(self, column: str) -> None:
            super().__init__()
            self._column = column

        async def create(self, job_id: int) -> None:
            self.calls.append(job_id)
            await database.execute(
                "UPDATE upload_jobs SET {}='completed' WHERE id=?".format(self._column),
                (job_id,),
            )

    first_reader = ArchiveReadService(protocol)
    second_reader = ArchiveReadService(protocol)
    try:
        await seed_waiting_job(database, filenames=('p1',), collection_state='pending')
        first = ReviewWatcher(
            database,
            protocol,
            bundle_loader=lambda account_id: async_value(account_id),
            archive_reader=first_reader,
            comment_branch=FakeBranch(),
            danmaku_branch=FakeBranch(),
            collection_branch=FakeBranch(),
            clock=Clock(),
        )

        async def crash_before_branches(_job: Any) -> None:
            raise RuntimeError('process interrupted after approval')

        first._create_branches = crash_before_branches  # type: ignore[method-assign]
        with pytest.raises(RuntimeError, match='interrupted after approval'):
            await first.run_once()
        assert await database.scalar('SELECT state FROM upload_jobs WHERE id=1') == (
            'approved'
        )

        comment = StatefulBranch('comment_branch_state')
        danmaku = StatefulBranch('danmaku_branch_state')
        collection = StatefulBranch('collection_branch_state')
        rebuilt = ReviewWatcher(
            database,
            protocol,
            bundle_loader=lambda account_id: async_value(account_id),
            archive_reader=second_reader,
            comment_branch=comment,
            danmaku_branch=danmaku,
            collection_branch=collection,
            clock=Clock(),
        )

        assert await rebuilt.run_once() == 1
        assert await rebuilt.recover_approved_pending_branches() == 0
        assert comment.calls == [1]
        assert danmaku.calls == [1]
        assert collection.calls == [1]
        assert protocol.submit_calls == []
        assert len(protocol.calls) == 1
    finally:
        await first_reader.close()
        await second_reader.close()
        await database.close()
