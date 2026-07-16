from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Mapping, Optional

from blrec.logging.audit import audit

from .database import BiliUploadDatabase
from .errors import BiliApiError, RemoteOutcomeUnknown

__all__ = ('CollectionPublisher',)


class _InvalidCollectionJob(RuntimeError):
    pass


@dataclass(frozen=True)
class _CollectionJob:
    account_id: int
    aid: int
    cid: int
    section_id: int
    title: str


class CollectionPublisher:
    def __init__(
        self,
        database: BiliUploadDatabase,
        protocol: Any,
        *,
        bundle_loader: Callable[[int], Awaitable[Any]],
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._database = database
        self._protocol = protocol
        self._bundle_loader = bundle_loader
        self._clock = clock

    async def recover_interrupted(self) -> int:
        return await self._database.execute(
            "UPDATE upload_jobs SET collection_branch_state='failed',"
            'collection_error=?,updated_at=? '
            "WHERE state='approved' AND collection_branch_state='running'",
            ('上次加入合集时程序中断，请先在 B 站确认后再重试', int(self._clock())),
        )

    async def create(self, job_id: int) -> None:
        row = await self._database.fetchone(
            'SELECT state,collection_branch_state FROM upload_jobs WHERE id=?',
            (job_id,),
        )
        if row is None:
            raise ValueError("unknown upload job '{}'".format(job_id))
        if str(row['collection_branch_state']) != 'pending':
            return
        if str(row['state']) != 'approved':
            raise ValueError('collection job is not ready')
        updated = await self._database.execute(
            "UPDATE upload_jobs SET collection_branch_state='running',"
            'collection_error=NULL,updated_at=? '
            "WHERE id=? AND state='approved' AND collection_branch_state='pending'",
            (int(self._clock()), job_id),
        )
        if updated != 1:
            return

        try:
            job = await self._load(job_id)
            bundle = await self._bundle_loader(job.account_id)
            await self._protocol.add_collection_episode(
                bundle,
                section_id=job.section_id,
                aid=job.aid,
                cid=job.cid,
                title=job.title,
            )
        except Exception as error:
            await self._fail(job_id, self._public_error(error))
            raise
        completed = await self._database.execute(
            "UPDATE upload_jobs SET collection_branch_state='completed',"
            'collection_error=NULL,updated_at=? '
            "WHERE id=? AND state='approved' AND collection_branch_state='running'",
            (int(self._clock()), job_id),
        )
        if completed == 1:
            audit(
                'collection_episode_added',
                job_id=job_id,
                account_id=job.account_id,
                aid=job.aid,
                cid=job.cid,
                section_id=job.section_id,
                result='completed',
            )

    async def _load(self, job_id: int) -> _CollectionJob:
        row = await self._database.fetchone(
            'SELECT job.account_id,job.aid,job.policy_snapshot_json,part.cid '
            'FROM upload_jobs job LEFT JOIN upload_parts part '
            'ON part.id=(SELECT first_part.id FROM upload_parts first_part '
            'WHERE first_part.job_id=job.id ORDER BY first_part.part_index LIMIT 1) '
            'WHERE job.id=?',
            (job_id,),
        )
        if row is None:
            raise _InvalidCollectionJob('upload job is missing')
        try:
            snapshot = json.loads(str(row['policy_snapshot_json']))
        except (TypeError, ValueError):
            raise _InvalidCollectionJob('upload policy snapshot is invalid') from None
        if not isinstance(snapshot, Mapping) or snapshot.get('format_version') != 4:
            raise _InvalidCollectionJob('upload policy snapshot is invalid')
        account_id = self._positive_int(row['account_id'])
        aid = self._positive_int(row['aid'])
        cid = self._positive_int(row['cid'])
        season_id = self._positive_int(snapshot.get('collection_season_id'))
        section_id = self._positive_int(snapshot.get('collection_section_id'))
        title = snapshot.get('title')
        if (
            account_id is None
            or snapshot.get('account_id') != account_id
            or aid is None
            or cid is None
            or season_id is None
            or section_id is None
            or not isinstance(title, str)
            or not title.strip()
        ):
            raise _InvalidCollectionJob('collection job is incomplete')
        return _CollectionJob(account_id, aid, cid, section_id, title.strip())

    async def _fail(self, job_id: int, message: str) -> None:
        updated = await self._database.execute(
            "UPDATE upload_jobs SET collection_branch_state='failed',"
            'collection_error=?,updated_at=? '
            "WHERE id=? AND state='approved' AND collection_branch_state='running'",
            (message, int(self._clock()), job_id),
        )
        if updated == 1:
            audit(
                'collection_episode_failed',
                level='ERROR',
                job_id=job_id,
                reason=message,
                result='failed',
            )

    @staticmethod
    def _public_error(error: Exception) -> str:
        if isinstance(error, RemoteOutcomeUnknown):
            return '加入合集结果未知，请先在 B 站确认后再重试'
        if isinstance(error, BiliApiError) and error.public_message:
            message = ' '.join(error.public_message.split())[:300]
            if message:
                return '加入合集失败：{}'.format(message)
        return '加入合集失败，请稍后重试'

    @staticmethod
    def _positive_int(value: Any) -> Optional[int]:
        return value if type(value) is int and value > 0 else None
