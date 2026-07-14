from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence, Set

import pytest

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
        'comment_branch_state,danmaku_branch_state,aid,bvid,created_at,updated_at) '
        'VALUES(?,?,?,\'{}\',\'waiting_review\',\'confirmed\',?,?,?,?,1,1)',
        (job_id, job_id, account_id, comment_state, danmaku_state, aid, bvid),
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


def archive_detail(
    videos: Sequence[Mapping[str, Any]], *, aid: int = 303, bvid: str = 'BVfixture'
) -> Mapping[str, Any]:
    return {
        'code': 0,
        'data': {'archive': {'aid': aid, 'bvid': bvid}, 'videos': list(videos)},
    }


def video(filename: str, cid: int, page: int) -> Mapping[str, Any]:
    return {'filename': filename, 'cid': cid, 'page': page}


def watcher(
    database: BiliUploadDatabase,
    response: Mapping[str, Any],
    *,
    detail: Optional[Mapping[str, Any]] = None,
    clock: Optional[Clock] = None,
    comment_branch: Optional[FakeBranch] = None,
    danmaku_branch: Optional[FakeBranch] = None,
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
        clock=clock or Clock(),
    )


async def async_value(value: object) -> object:
    return value


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
        assert comment.calls == [1]
        assert danmaku.calls == [1]
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
            'SELECT state,review_reason FROM upload_jobs WHERE id=1'
        )
        assert row is not None
        assert row['state'] == 'paused'
        assert '分 P' in str(row['review_reason'])
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
            clock=Clock(),
        )

        assert await review.run_once() == 0

        assert [call[0] for call in protocol.calls] == [1, 2]
    finally:
        await database.close()
