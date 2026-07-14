from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from typing import (
    Any,
    Awaitable,
    Callable,
    Dict,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
)

from typing_extensions import Protocol

from .database import BiliUploadDatabase

__all__ = ('PostReviewBranch', 'ReviewWatcher')


class PostReviewBranch(Protocol):
    async def create(self, job_id: int) -> None:
        pass


class _ReviewMismatch(RuntimeError):
    pass


@dataclass(frozen=True)
class _WaitingJob:
    id: int
    account_id: int
    account_uid: int
    account_state: str
    aid: Optional[int]
    bvid: Optional[str]
    comment_branch_state: str
    danmaku_branch_state: str
    collection_branch_state: str


class ReviewWatcher:
    # Bilibili uses -50 for a completed archive that is only visible to its owner.
    APPROVED_STATES = frozenset((-50, 0, 1))
    REJECTED_STATES = frozenset((-2, -3, -4, -5, -12, -14, -16, -100))

    def __init__(
        self,
        database: BiliUploadDatabase,
        protocol: Any,
        *,
        bundle_loader: Callable[[int], Awaitable[Any]],
        comment_branch: PostReviewBranch,
        danmaku_branch: PostReviewBranch,
        collection_branch: PostReviewBranch,
        poll_interval_seconds: int = 900,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if poll_interval_seconds <= 0:
            raise ValueError('review poll interval must be positive')
        self._database = database
        self._protocol = protocol
        self._bundle_loader = bundle_loader
        self._comment_branch = comment_branch
        self._danmaku_branch = danmaku_branch
        self._collection_branch = collection_branch
        self._poll_interval_seconds = poll_interval_seconds
        self._clock = clock
        self._next_poll_at: Dict[int, int] = {}

    async def run_once(self) -> int:
        rows = await self._database.fetchall(
            'SELECT job.id,job.account_id,job.aid,job.bvid,'
            'job.comment_branch_state,job.danmaku_branch_state,'
            'job.collection_branch_state,'
            'account.uid AS account_uid,account.state AS account_state '
            'FROM upload_jobs job JOIN bili_accounts account '
            'ON account.id=job.account_id '
            "WHERE job.state='waiting_review' ORDER BY job.account_id,job.id"
        )
        grouped: Dict[int, List[_WaitingJob]] = {}
        for row in rows:
            job = self._job(row)
            grouped.setdefault(job.account_id, []).append(job)

        now = int(self._clock())
        changed = 0
        for account_id, jobs in grouped.items():
            if now < self._next_poll_at.get(account_id, 0):
                continue
            self._next_poll_at[account_id] = now + self._poll_interval_seconds
            if jobs[0].account_state != 'active':
                for job in jobs:
                    if await self._pause(job, '投稿账号不可用，无法同步审核状态'):
                        changed += 1
                continue
            bundle = await self._bundle_loader(account_id)
            response = await self._protocol.list_archives(
                bundle, {'status': 'is_pubing,pubed,not_pubed', 'pn': 1}
            )
            try:
                archives = self._archives(response)
            except _ReviewMismatch as error:
                for job in jobs:
                    if await self._pause(job, str(error)):
                        changed += 1
                continue
            for job in jobs:
                if await self._process_job(job, archives, bundle):
                    changed += 1
        return changed

    async def _process_job(
        self, job: _WaitingJob, archives: Sequence[Mapping[str, Any]], bundle: Any
    ) -> bool:
        if job.aid is None or job.bvid is None:
            return await self._pause(job, '上传任务缺少 AID/BVID，无法同步审核状态')
        matches = [entry for entry in archives if self._matches(job, entry)]
        if not matches:
            return False
        if len(matches) != 1:
            return await self._pause(job, '近期稿件列表中存在重复的稿件标识')
        entry = matches[0]
        archive = self._archive(entry)
        if (
            self._positive_int(archive.get('aid')) != job.aid
            or self._text(archive.get('bvid')) != job.bvid
        ):
            return await self._pause(job, '远端稿件标识与上传任务不一致')
        remote_owner_uid = self._owner_uid(archive)
        if remote_owner_uid is not None and remote_owner_uid != job.account_uid:
            return await self._pause(job, '远端稿件账号归属与投稿账号不一致')

        state = archive.get('state')
        if type(state) is not int:
            return await self._pause(job, '审核接口缺少有效的稿件状态')
        if state in self.REJECTED_STATES:
            reason = self._public_reason(archive) or '稿件审核未通过（{}）'.format(
                state
            )
            return await self._reject(job, reason)
        if state not in self.APPROVED_STATES:
            waiting_reason = self._public_reason(archive)
            if waiting_reason is None and state == -40:
                waiting_reason = '等待定时发布'
            if waiting_reason:
                await self._waiting_reason(job, waiting_reason)
            return False

        try:
            detail = await self._protocol.archive_view(
                bundle,
                {'topic_grey': 1, 'bvid': job.bvid, 't': int(self._clock() * 1000)},
            )
            cids = await self._verified_cids(job, detail)
        except _ReviewMismatch as error:
            return await self._pause(job, str(error))
        approved = await self._approve(job, cids)
        if not approved:
            return False
        await self._create_branches(job)
        return True

    async def _verified_cids(
        self, job: _WaitingJob, response: Mapping[str, Any]
    ) -> Dict[int, int]:
        parts = await self._database.fetchall(
            'SELECT id,part_index,remote_filename FROM upload_parts '
            'WHERE job_id=? ORDER BY part_index',
            (job.id,),
        )
        local_by_filename: Dict[str, Tuple[int, int]] = {}
        for part in parts:
            filename = self._text(part['remote_filename'])
            if filename is None or filename in local_by_filename:
                raise _ReviewMismatch('本地上传分 P 的远端 filename 不完整或重复')
            local_by_filename[filename] = (int(part['id']), int(part['part_index']))
        if not local_by_filename:
            raise _ReviewMismatch('上传任务没有可核对的分 P')

        data = response.get('data')
        if not isinstance(data, Mapping):
            raise _ReviewMismatch('稿件详情接口响应结构不符合预期')
        identity_verified = False
        detail_archive = data.get('archive')
        if isinstance(detail_archive, Mapping):
            if (
                self._positive_int(detail_archive.get('aid')) != job.aid
                or self._text(detail_archive.get('bvid')) != job.bvid
            ):
                raise _ReviewMismatch('稿件详情标识与上传任务不一致')
            identity_verified = True

        videos = data.get('videos')
        if not isinstance(videos, list):
            videos = data.get('Videos')
        if not isinstance(videos, list):
            raise _ReviewMismatch('稿件详情接口未返回可核对的分 P 信息')
        remote_by_filename: Dict[str, Tuple[int, int]] = {}
        for video in videos:
            if not isinstance(video, Mapping):
                raise _ReviewMismatch('稿件详情接口返回的分 P 信息不完整')
            video_aid = self._positive_int(video.get('aid'))
            video_bvid = self._text(video.get('bvid'))
            if video_aid is not None or video_bvid is not None:
                if video_aid != job.aid or video_bvid != job.bvid:
                    raise _ReviewMismatch('稿件详情分 P 标识与上传任务不一致')
                identity_verified = True
            filename = self._text(video.get('filename'))
            cid = self._positive_int(video.get('cid'))
            page = self._positive_int(video.get('page'))
            if page is None:
                page = self._positive_int(video.get('index'))
            if (
                filename is None
                or cid is None
                or page is None
                or filename in remote_by_filename
            ):
                raise _ReviewMismatch('稿件详情返回的分 P filename/CID 重复或缺失')
            remote_by_filename[filename] = (cid, page)

        if not identity_verified:
            raise _ReviewMismatch('稿件详情缺少可核对的 AID/BVID')

        if set(remote_by_filename) != set(local_by_filename):
            raise _ReviewMismatch('远端分 P 与本地上传 filename 不能一一对应')
        cids: Dict[int, int] = {}
        for filename, (part_id, part_index) in local_by_filename.items():
            cid, page = remote_by_filename[filename]
            if page != part_index:
                raise _ReviewMismatch('远端分 P 页码与本地顺序不一致')
            cids[part_id] = cid
        return cids

    async def _approve(self, job: _WaitingJob, cids: Mapping[int, int]) -> bool:
        now = int(self._clock())

        def approve(connection: sqlite3.Connection) -> bool:
            current = connection.execute(
                'SELECT state,account_id,aid,bvid FROM upload_jobs WHERE id=?',
                (job.id,),
            ).fetchone()
            if (
                current is None
                or str(current['state']) != 'waiting_review'
                or int(current['account_id']) != job.account_id
                or current['aid'] != job.aid
                or current['bvid'] != job.bvid
            ):
                return False
            part_ids = {
                int(row['id'])
                for row in connection.execute(
                    'SELECT id FROM upload_parts WHERE job_id=?', (job.id,)
                ).fetchall()
            }
            if part_ids != set(cids):
                return False
            for part_id, cid in cids.items():
                connection.execute(
                    'UPDATE upload_parts SET cid=? WHERE id=? AND job_id=?',
                    (cid, part_id, job.id),
                )
            connection.execute(
                "UPDATE upload_jobs SET state='approved',review_reason=NULL,"
                'approved_at=?,repair_message=CASE '
                "WHEN repair_state='waiting_review' THEN '转码修复已通过审核' "
                'ELSE repair_message END,repair_error=CASE '
                "WHEN repair_state='waiting_review' THEN NULL ELSE repair_error END,"
                'repair_completed_at=CASE '
                "WHEN repair_state='waiting_review' THEN ? "
                'ELSE repair_completed_at END,repair_state=CASE '
                "WHEN repair_state='waiting_review' THEN 'completed' "
                'ELSE repair_state END,updated_at=? WHERE id=?',
                (now, now, now, job.id),
            )
            return True

        return await self._database.write(approve)

    async def _create_branches(self, job: _WaitingJob) -> None:
        branches = (
            ('comment_branch_state', job.comment_branch_state, self._comment_branch),
            ('danmaku_branch_state', job.danmaku_branch_state, self._danmaku_branch),
            (
                'collection_branch_state',
                job.collection_branch_state,
                self._collection_branch,
            ),
        )
        for column, state, branch in branches:
            if state != 'pending':
                continue
            try:
                await branch.create(job.id)
            except Exception:
                await self._branch_failed(job.id, column)

    async def _branch_failed(self, job_id: int, column: str) -> None:
        if column == 'comment_branch_state':
            reason = '审核已通过，但自动评论任务创建失败'
        elif column == 'danmaku_branch_state':
            reason = '审核已通过，但弹幕回灌任务创建失败'
        elif column == 'collection_branch_state':
            await self._database.execute(
                "UPDATE upload_jobs SET collection_branch_state='failed',"
                'collection_error=?,updated_at=? '
                "WHERE id=? AND state='approved' "
                "AND collection_branch_state='pending'",
                ('审核已通过，但加入合集失败', int(self._clock()), job_id),
            )
            return
        else:
            raise ValueError('invalid post-review branch')

        def fail(connection: sqlite3.Connection) -> None:
            row = connection.execute(
                'SELECT review_reason FROM upload_jobs WHERE id=?', (job_id,)
            ).fetchone()
            if row is None:
                return
            previous = self._text(row['review_reason'])
            combined = reason if previous is None else '{}；{}'.format(previous, reason)
            connection.execute(
                'UPDATE upload_jobs SET {}=\'failed\',review_reason=?,updated_at=? '
                "WHERE id=? AND state='approved' AND {}='pending'".format(
                    column, column
                ),
                (combined, int(self._clock()), job_id),
            )

        await self._database.write(fail)

    async def _pause(self, job: _WaitingJob, reason: str) -> bool:
        updated = await self._database.execute(
            "UPDATE upload_jobs SET state='paused',review_reason=?,updated_at=? "
            "WHERE id=? AND state='waiting_review' AND account_id=?",
            (reason, int(self._clock()), job.id, job.account_id),
        )
        return updated == 1

    async def _reject(self, job: _WaitingJob, reason: str) -> bool:
        updated = await self._database.execute(
            "UPDATE upload_jobs SET state='rejected',review_reason=?,"
            'repair_error=CASE '
            "WHEN repair_state='waiting_review' THEN ? ELSE repair_error END,"
            'repair_message=CASE '
            "WHEN repair_state='waiting_review' THEN NULL ELSE repair_message END,"
            'repair_completed_at=CASE '
            "WHEN repair_state='waiting_review' THEN ? ELSE repair_completed_at END,"
            'repair_state=CASE '
            "WHEN repair_state='waiting_review' THEN 'failed' "
            'ELSE repair_state END,updated_at=? '
            "WHERE id=? AND state='waiting_review' AND account_id=?",
            (
                reason,
                reason,
                int(self._clock()),
                int(self._clock()),
                job.id,
                job.account_id,
            ),
        )
        return updated == 1

    async def _waiting_reason(self, job: _WaitingJob, reason: str) -> None:
        await self._database.execute(
            'UPDATE upload_jobs SET review_reason=?,updated_at=? '
            "WHERE id=? AND state='waiting_review' AND account_id=?",
            (reason, int(self._clock()), job.id, job.account_id),
        )

    @classmethod
    def _archives(cls, response: Mapping[str, Any]) -> List[Mapping[str, Any]]:
        data = response.get('data')
        if not isinstance(data, Mapping):
            raise _ReviewMismatch('审核接口响应结构不符合预期')
        entries = data.get('arc_audits')
        if not isinstance(entries, list) or not all(
            isinstance(entry, Mapping) for entry in entries
        ):
            raise _ReviewMismatch('审核接口响应结构不符合预期')
        return list(entries)

    @classmethod
    def _matches(cls, job: _WaitingJob, entry: Mapping[str, Any]) -> bool:
        archive = cls._archive(entry)
        aid = cls._positive_int(archive.get('aid'))
        bvid = cls._text(archive.get('bvid'))
        return (job.aid is not None and aid == job.aid) or (
            job.bvid is not None and bvid == job.bvid
        )

    @staticmethod
    def _archive(entry: Mapping[str, Any]) -> Mapping[str, Any]:
        archive = entry.get('Archive')
        if not isinstance(archive, Mapping):
            archive = entry.get('archive')
        return archive if isinstance(archive, Mapping) else {}

    @classmethod
    def _owner_uid(cls, archive: Mapping[str, Any]) -> Optional[int]:
        direct = cls._positive_int(archive.get('mid'))
        if direct is not None:
            return direct
        owner = archive.get('owner')
        if not isinstance(owner, Mapping):
            return None
        return cls._positive_int(owner.get('mid') or owner.get('uid'))

    @classmethod
    def _public_reason(cls, archive: Mapping[str, Any]) -> Optional[str]:
        for field in ('reject_reason', 'state_desc'):
            value = cls._text(archive.get(field))
            if value:
                return value[:500]
        return None

    @staticmethod
    def _job(row: Any) -> _WaitingJob:
        return _WaitingJob(
            id=int(row['id']),
            account_id=int(row['account_id']),
            account_uid=int(row['account_uid']),
            account_state=str(row['account_state']),
            aid=None if row['aid'] is None else int(row['aid']),
            bvid=None if row['bvid'] is None else str(row['bvid']),
            comment_branch_state=str(row['comment_branch_state']),
            danmaku_branch_state=str(row['danmaku_branch_state']),
            collection_branch_state=str(row['collection_branch_state']),
        )

    @staticmethod
    def _positive_int(value: Any) -> Optional[int]:
        if type(value) is int:
            result = value
        elif isinstance(value, str) and value.isdigit():
            result = int(value)
        else:
            return None
        return result if result > 0 else None

    @staticmethod
    def _text(value: Any) -> Optional[str]:
        return value.strip() if isinstance(value, str) and value.strip() else None
