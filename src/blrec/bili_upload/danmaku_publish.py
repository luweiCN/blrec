from __future__ import annotations

import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from math import ceil
from typing import Any, Awaitable, Callable, Dict, List, Mapping, Optional, Set

from blrec.logging.audit import audit

from .accounts import (
    AccountNotFound,
    AccountPaused,
    AccountWriteGate,
    CredentialVersionChanged,
)
from .credentials import CredentialNotFound
from .crypto import InvalidCredentialBundle, InvalidCredentialKey
from .database import BiliUploadDatabase, LeaseClaim, LeaseLost
from .errors import (
    BiliApiError,
    DefinitelyNotSent,
    ProtocolContractError,
    RemoteOutcomeUnknown,
)

__all__ = ('DanmakuBreaker', 'DanmakuPublisher')


_DORMANT_UNTIL = 2_147_483_647


@dataclass
class DanmakuBreaker:
    minimum_interval: int = 25
    next_send_at: float = 0
    next_probe_at: float = 0
    rate_limited_items: Set[int] = field(default_factory=set)
    _rate_delay: int = 25

    def __post_init__(self) -> None:
        if self.minimum_interval < 25:
            self.minimum_interval = 25
        self._rate_delay = self.minimum_interval

    def ready(self, now: float) -> bool:
        return now >= max(self.next_send_at, self.next_probe_at)

    def reserve_send(self, now: float) -> None:
        self.next_send_at = now + self.minimum_interval

    def delay_after(self, code: int) -> int:
        if code == 36715:
            return 24 * 3600
        if code == 36703:
            return max(self.minimum_interval, self._rate_delay)
        return self.minimum_interval

    def rate_limited(self, item_id: int, now: float) -> int:
        self.rate_limited_items.add(item_id)
        delay = self.delay_after(36703)
        self.next_probe_at = max(self.next_probe_at, now + delay)
        self._rate_delay = min(24 * 3600, max(self.minimum_interval, delay * 2))
        return delay

    def daily_limited(self, now: float) -> int:
        delay = self.delay_after(36715)
        self.next_probe_at = max(self.next_probe_at, now + delay)
        return delay

    def succeeded(self) -> None:
        self.rate_limited_items.clear()
        self._rate_delay = self.minimum_interval
        self.next_probe_at = 0


@dataclass(frozen=True)
class _Candidate:
    item_id: int
    job_id: int
    account_id: int
    priority: int
    progress_ms: int
    was_rate_limited: bool


@dataclass(frozen=True)
class _DanmakuWork:
    id: int
    job_id: int
    part_id: int
    state: str
    progress_ms: int
    mode: int
    fontsize: int
    color: int
    content: str
    account_id: int
    account_state: str
    credential_version: int
    aid: int
    bvid: str
    cid: int
    branch_state: str


class DanmakuPublisher:
    _AUTH_CODES = frozenset((-101, -111))
    _PERMANENT_CODES = frozenset((36701, 36702, 36718)) | frozenset(range(36705, 36715))

    def __init__(
        self,
        database: BiliUploadDatabase,
        protocol: Any,
        *,
        bundle_loader: Callable[[int], Awaitable[Any]],
        account_gates: AccountWriteGate,
        interval_seconds: int = 25,
        auth_refresh: Optional[Callable[[int], Awaitable[Any]]] = None,
        worker_id: Optional[str] = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError('danmaku interval must be positive')
        self._database = database
        self._protocol = protocol
        self._bundle_loader = bundle_loader
        self._account_gates = account_gates
        self._interval_seconds = max(25, interval_seconds)
        self._auth_refresh = auth_refresh
        self._worker_id = worker_id or 'danmaku-{}'.format(uuid.uuid4().hex)
        self._clock = clock
        self._breakers: Dict[int, DanmakuBreaker] = {}
        self._last_account_id: Optional[int] = None
        self._last_job_by_account: Dict[int, int] = {}

    def breaker_for(self, account_id: int) -> DanmakuBreaker:
        return self._breakers.setdefault(
            account_id, DanmakuBreaker(self._interval_seconds)
        )

    async def recover_interrupted(self) -> int:
        now = int(self._clock())
        message = '弹幕发送被中断，已自动重新排队'

        def recover(connection: sqlite3.Connection) -> List[sqlite3.Row]:
            rows = connection.execute(
                'SELECT item.id,item.attempt,item.progress_ms,part.id AS part_id,'
                'part.job_id,part.cid FROM danmaku_items item '
                'JOIN upload_parts part ON part.id=item.part_id '
                "WHERE item.state IN ('in_flight','unknown_outcome')"
            ).fetchall()
            for row in rows:
                connection.execute(
                    "UPDATE danmaku_items SET state='prepared',"
                    'error_code=NULL,error_message=?,next_attempt_at=?,'
                    'lease_owner=NULL,lease_until=NULL WHERE id=? '
                    "AND state IN ('in_flight','unknown_outcome')",
                    (message, now, int(row['id'])),
                )
            job_ids = {int(row['job_id']) for row in rows}
            for job_id in job_ids:
                connection.execute(
                    "UPDATE upload_jobs SET danmaku_branch_state='publishing',"
                    'review_reason=NULL,updated_at=? WHERE id=? AND state='
                    "'approved' AND danmaku_branch_state='paused' AND EXISTS("
                    'SELECT 1 FROM bili_accounts account WHERE account.id='
                    "upload_jobs.account_id AND account.state='active')",
                    (now, job_id),
                )
            return rows

        recovered = await self._database.write(recover)
        for row in recovered:
            audit(
                'danmaku_requeued',
                level='WARNING',
                job_id=int(row['job_id']),
                part_id=int(row['part_id']),
                item_id=int(row['id']),
                cid=None if row['cid'] is None else int(row['cid']),
                progress_ms=int(row['progress_ms']),
                attempt=int(row['attempt']),
                reason=message,
                recovery=True,
                result='prepared',
            )
        return len(recovered)

    async def run_once(self) -> Optional[int]:
        now = self._clock()
        candidate = await self._select_candidate(now)
        if candidate is None:
            return None
        claim = await self._claim(candidate.item_id, int(now))
        if claim is None:
            return None
        self._last_account_id = candidate.account_id
        self._last_job_by_account[candidate.account_id] = candidate.job_id
        await self._process(claim)
        return claim.id

    async def _select_candidate(self, now: float) -> Optional[_Candidate]:
        rows = await self._database.fetchall(
            'SELECT item.id,part.job_id,job.account_id,item.priority,'
            'item.progress_ms,item.error_code FROM danmaku_items item '
            'JOIN upload_parts part ON part.id=item.part_id '
            'JOIN upload_jobs job ON job.id=part.job_id '
            'JOIN bili_accounts account ON account.id=job.account_id '
            "WHERE item.state IN ('prepared','in_flight') "
            'AND item.next_attempt_at<=? '
            'AND (item.lease_until IS NULL OR item.lease_until<=?) '
            "AND job.state='approved' "
            "AND job.danmaku_branch_state='publishing' "
            "AND account.state='active' AND part.cid IS NOT NULL "
            'ORDER BY job.account_id,part.job_id,item.priority DESC,'
            'item.progress_ms,item.id',
            (int(now), int(now)),
        )
        candidates = [
            _Candidate(
                item_id=int(row['id']),
                job_id=int(row['job_id']),
                account_id=int(row['account_id']),
                priority=int(row['priority']),
                progress_ms=int(row['progress_ms']),
                was_rate_limited=row['error_code'] == 36703,
            )
            for row in rows
            if self.breaker_for(int(row['account_id'])).ready(now)
        ]
        if not candidates:
            return None
        account_ids = sorted({candidate.account_id for candidate in candidates})
        account_id = self._next_value(account_ids, self._last_account_id)
        account_candidates = [
            candidate for candidate in candidates if candidate.account_id == account_id
        ]
        job_ids = sorted({candidate.job_id for candidate in account_candidates})
        job_id = self._next_value(job_ids, self._last_job_by_account.get(account_id))
        job_candidates = [
            candidate for candidate in account_candidates if candidate.job_id == job_id
        ]
        breaker = self.breaker_for(account_id)
        fresh = [
            candidate
            for candidate in job_candidates
            if not candidate.was_rate_limited
            and candidate.item_id not in breaker.rate_limited_items
        ]
        return (fresh or job_candidates)[0]

    async def _claim(self, item_id: int, now: int) -> Optional[LeaseClaim]:
        def claim(connection: sqlite3.Connection) -> Optional[LeaseClaim]:
            row = connection.execute(
                'SELECT state FROM danmaku_items WHERE id=? '
                "AND state IN ('prepared','in_flight') "
                'AND next_attempt_at<=? '
                'AND (lease_until IS NULL OR lease_until<=?)',
                (item_id, now, now),
            ).fetchone()
            if row is None:
                return None
            lease_until = now + self._database.LEASE_TTL_SECONDS
            updated = connection.execute(
                'UPDATE danmaku_items SET lease_owner=?,lease_generation='
                'lease_generation+1,lease_until=?,attempt=attempt+1 WHERE id=? '
                "AND state IN ('prepared','in_flight') "
                'AND next_attempt_at<=? '
                'AND (lease_until IS NULL OR lease_until<=?)',
                (self._worker_id, lease_until, item_id, now, now),
            )
            if updated.rowcount != 1:
                return None
            claimed = connection.execute(
                'SELECT lease_generation,attempt FROM danmaku_items WHERE id=?',
                (item_id,),
            ).fetchone()
            assert claimed is not None
            return LeaseClaim(
                table='danmaku_items',
                id=item_id,
                lease_owner=self._worker_id,
                lease_generation=int(claimed['lease_generation']),
                lease_until=lease_until,
                attempt=int(claimed['attempt']),
            )

        return await self._database.write(claim)

    async def _process(self, claim: LeaseClaim) -> None:
        work = await self._load(claim)
        if work.state == 'in_flight':
            await self._retry_uncertain(claim, work, '弹幕在发起请求阶段被中断')
            return
        if work.branch_state != 'publishing':
            await self._release(claim, _DORMANT_UNTIL)
            return
        if work.account_state != 'active':
            await self._pause_branch(claim, work, '投稿账号不可用，弹幕回灌已暂停')
            return
        try:
            gate = self._account_gates.for_account(work.account_id)
            async with gate.hold(work.credential_version):
                bundle = await self._bundle_loader(work.account_id)
                send_at = self._clock()

                async def mark_send_started() -> None:
                    await self._start_send(claim, work, send_at)
                    self.breaker_for(work.account_id).reserve_send(send_at)

                response = await self._protocol.post_danmaku(
                    bundle, self._request_params(work), on_prepared=mark_send_started
                )
        except DefinitelyNotSent:
            await self._safe_retry(claim, work, '弹幕请求确认未发出，将自动重试')
            return
        except RemoteOutcomeUnknown:
            await self._retry_uncertain(claim, work, '弹幕请求结果未返回')
            return
        except BiliApiError as error:
            await self._handle_api_error(claim, work, error)
            return
        except (AccountNotFound, AccountPaused, CredentialVersionChanged):
            await self._pause_branch(claim, work, '投稿账号在弹幕发送期间发生变化')
            return
        except (CredentialNotFound, InvalidCredentialBundle, InvalidCredentialKey):
            await self._pause_branch(claim, work, '投稿账号凭据无法读取')
            return
        except ProtocolContractError:
            await self._retry_uncertain(claim, work, '弹幕接口响应异常')
            return
        dmid = self._response_dmid(response)
        await self._confirm(claim, work, dmid)
        audit(
            'danmaku_confirmed',
            job_id=work.job_id,
            part_id=work.part_id,
            item_id=work.id,
            dmid=dmid,
            attempt=claim.attempt,
            result='confirmed',
        )
        self.breaker_for(work.account_id).succeeded()
        await self._complete_if_done(work.job_id)

    async def _handle_api_error(
        self, claim: LeaseClaim, work: _DanmakuWork, error: BiliApiError
    ) -> None:
        if error.code == 36703:
            breaker = self.breaker_for(work.account_id)
            delay = breaker.rate_limited(work.id, self._clock())
            previous = await self._rate_limited_count(
                work.account_id, excluding_item_id=work.id
            )
            if previous + 1 >= 3:
                await self._pause_account(
                    claim, work, error.code, 'B 站连续提示弹幕发送频率过快'
                )
            else:
                resume_at = self._deadline(delay)
                await self._update_item(
                    claim,
                    {
                        'state': 'prepared',
                        'error_code': error.code,
                        'error_message': 'B 站提示弹幕发送频率过快，已退避',
                        'next_attempt_at': resume_at,
                    },
                    release=True,
                )
                await self._defer_account(work.account_id, resume_at)
            return
        if error.code == 36704:
            await self._recheck_cid(claim, work, error.code)
            return
        if error.code == 36715:
            delay = self.breaker_for(work.account_id).daily_limited(self._clock())
            await self._daily_pause(claim, work, error.code, delay)
            return
        if error.code in self._AUTH_CODES:
            await self._auth_retry(claim, work, error.code)
            return
        if error.code in self._PERMANENT_CODES:
            await self._fail_item(claim, work, error.code)
            return
        await self._pause_branch(
            claim,
            work,
            'B 站弹幕接口返回错误（{}），需要人工确认'.format(error.code),
            error_code=error.code,
        )

    async def _auth_retry(
        self, claim: LeaseClaim, work: _DanmakuWork, error_code: int
    ) -> None:
        await self._update_item(
            claim,
            {
                'state': 'prepared',
                'error_code': error_code,
                'error_message': '登录凭据被拒绝，正在刷新固定投稿账号',
                'next_attempt_at': self._deadline(self._interval_seconds),
            },
            release=True,
        )
        if self._auth_refresh is None:
            await self._pause_job_without_claim(
                work.job_id, '投稿账号凭据失效且无法自动刷新'
            )
            return
        try:
            await self._auth_refresh(work.account_id)
        except Exception:
            await self._pause_account_without_claim(
                work.account_id, '投稿账号凭据刷新失败，弹幕回灌已暂停'
            )

    async def _recheck_cid(
        self, claim: LeaseClaim, work: _DanmakuWork, error_code: int
    ) -> None:
        now = int(self._clock())

        def update(connection: sqlite3.Connection) -> None:
            changed = connection.execute(
                "UPDATE danmaku_items SET state='prepared',error_code=?,"
                'error_message=?,next_attempt_at=?,attempt=MAX(0,attempt-1),'
                'lease_owner=NULL,lease_until=NULL WHERE id=? AND lease_owner=? '
                'AND lease_generation=?',
                (
                    error_code,
                    '稿件状态或 CID 需要重新核对',
                    now,
                    claim.id,
                    claim.lease_owner,
                    claim.lease_generation,
                ),
            )
            if changed.rowcount != 1:
                raise LeaseLost('danmaku item lease was lost')
            connection.execute(
                'UPDATE upload_parts SET cid=NULL WHERE id=?', (work.part_id,)
            )
            connection.execute(
                "UPDATE upload_jobs SET state='waiting_review',review_reason=?,"
                'updated_at=? WHERE id=? AND state=\'approved\'',
                (
                    'B 站暂不允许向该稿件发送弹幕，重新核对审核状态与 CID',
                    now,
                    work.job_id,
                ),
            )

        await self._database.write(update)

    async def _daily_pause(
        self, claim: LeaseClaim, work: _DanmakuWork, error_code: int, delay: int
    ) -> None:
        resume_at = self._deadline(delay)
        await self._update_item(
            claim,
            {
                'state': 'prepared',
                'error_code': error_code,
                'error_message': 'B 站提示当日操作次数已达上限，至少暂停 24 小时',
                'next_attempt_at': resume_at,
            },
            release=True,
        )
        await self._defer_account(work.account_id, resume_at)

    async def _fail_item(
        self, claim: LeaseClaim, work: _DanmakuWork, error_code: int
    ) -> None:
        await self._update_item(
            claim,
            {
                'state': 'failed_permanent',
                'error_code': error_code,
                'error_message': 'B 站拒绝该条弹幕（{}）'.format(error_code),
            },
            release=True,
        )
        audit(
            'danmaku_failed',
            level='ERROR',
            job_id=work.job_id,
            part_id=work.part_id,
            item_id=work.id,
            error_code=error_code,
            result='failed_permanent',
        )
        await self._complete_if_done(work.job_id)

    async def _safe_retry(
        self, claim: LeaseClaim, work: _DanmakuWork, message: str
    ) -> None:
        if claim.attempt >= 5:
            await self._pause_branch(
                claim, work, '弹幕连续 5 次未能发出，已暂停等待检查网络'
            )
            return
        delay = max(self._interval_seconds, min(300, 2 ** min(claim.attempt, 8)))
        await self._update_item(
            claim,
            {
                'state': 'prepared',
                'error_code': None,
                'error_message': message,
                'next_attempt_at': self._deadline(delay),
            },
            release=True,
        )

    async def _retry_uncertain(
        self, claim: LeaseClaim, work: _DanmakuWork, message: str
    ) -> None:
        delay = max(self._interval_seconds, min(300, 2 ** min(claim.attempt, 8)))
        retry_message = '{}，已自动重新排队'.format(message)
        await self._update_item(
            claim,
            {
                'state': 'prepared',
                'error_code': None,
                'error_message': retry_message,
                'next_attempt_at': self._deadline(delay),
            },
            release=True,
        )
        audit(
            'danmaku_requeued',
            level='WARNING',
            job_id=work.job_id,
            part_id=work.part_id,
            item_id=work.id,
            cid=work.cid,
            progress_ms=work.progress_ms,
            attempt=claim.attempt,
            reason=retry_message,
            recovery=False,
            result='prepared',
        )

    async def _pause_branch(
        self,
        claim: LeaseClaim,
        work: _DanmakuWork,
        message: str,
        *,
        error_code: Optional[int] = None,
    ) -> None:
        await self._update_item(
            claim,
            {
                'state': 'prepared',
                'error_code': error_code,
                'error_message': message,
                'next_attempt_at': _DORMANT_UNTIL,
            },
            release=True,
        )
        await self._pause_job_without_claim(work.job_id, message)
        audit(
            'danmaku_branch_paused',
            level='WARNING',
            job_id=work.job_id,
            part_id=work.part_id,
            item_id=work.id,
            error_code=error_code,
            reason=message,
            result='paused',
        )

    async def _pause_account(
        self, claim: LeaseClaim, work: _DanmakuWork, error_code: int, message: str
    ) -> None:
        await self._update_item(
            claim,
            {
                'state': 'prepared',
                'error_code': error_code,
                'error_message': message,
                'next_attempt_at': _DORMANT_UNTIL,
            },
            release=True,
        )
        await self._pause_account_without_claim(work.account_id, message)
        audit(
            'danmaku_account_paused',
            level='WARNING',
            account_id=work.account_id,
            job_id=work.job_id,
            item_id=work.id,
            error_code=error_code,
            reason=message,
            result='paused',
        )

    async def _pause_account_without_claim(self, account_id: int, message: str) -> None:
        now = int(self._clock())

        def pause(connection: sqlite3.Connection) -> None:
            connection.execute(
                "UPDATE bili_accounts SET state='paused',pause_reason=?,updated_at=? "
                "WHERE id=? AND state='active'",
                (message, now, account_id),
            )
            connection.execute(
                "UPDATE upload_jobs SET danmaku_branch_state='paused',"
                'review_reason=?,updated_at=? WHERE account_id=? '
                "AND danmaku_branch_state='publishing'",
                (message, now, account_id),
            )

        await self._database.write(pause)

    async def _pause_job_without_claim(self, job_id: int, message: str) -> None:
        await self._database.execute(
            "UPDATE upload_jobs SET danmaku_branch_state='paused',review_reason=?,"
            "updated_at=? WHERE id=? AND danmaku_branch_state='publishing'",
            (message, int(self._clock()), job_id),
        )

    async def _complete_if_done(self, job_id: int) -> None:
        row = await self._database.fetchone(
            'SELECT '
            "SUM(CASE WHEN item.state IN ('prepared','in_flight','unknown_outcome') "
            'THEN 1 ELSE 0 END) AS remaining,'
            "SUM(CASE WHEN item.state='failed_permanent' THEN 1 ELSE 0 END) AS failed "
            'FROM danmaku_items item JOIN upload_parts part ON part.id=item.part_id '
            'WHERE part.job_id=?',
            (job_id,),
        )
        remaining = (
            0 if row is None or row['remaining'] is None else int(row['remaining'])
        )
        failed = 0 if row is None or row['failed'] is None else int(row['failed'])
        if remaining:
            return
        branch_state = 'failed' if failed else 'completed'
        await self._database.execute(
            'UPDATE upload_jobs SET danmaku_branch_state=?,updated_at=? '
            "WHERE id=? AND danmaku_branch_state IN ('publishing','paused')",
            (branch_state, int(self._clock()), job_id),
        )

    async def _load(self, claim: LeaseClaim) -> _DanmakuWork:
        row = await self._database.fetchone(
            'SELECT item.id,item.part_id,item.state,item.progress_ms,item.mode,'
            'item.fontsize,item.color,item.content,part.job_id,part.cid,'
            'job.account_id,job.aid,job.bvid,job.danmaku_branch_state,'
            'account.state AS account_state,account.credential_version '
            'FROM danmaku_items item JOIN upload_parts part ON part.id=item.part_id '
            'JOIN upload_jobs job ON job.id=part.job_id '
            'JOIN bili_accounts account ON account.id=job.account_id '
            'WHERE item.id=? AND item.lease_owner=? AND item.lease_generation=?',
            (claim.id, claim.lease_owner, claim.lease_generation),
        )
        if row is None:
            raise LeaseLost('danmaku item lease was lost')
        aid = self._positive_int(row['aid'])
        cid = self._positive_int(row['cid'])
        bvid = str(row['bvid'] or '')
        if aid is None or cid is None or not bvid:
            raise ValueError('danmaku job has incomplete remote identity')
        return _DanmakuWork(
            id=int(row['id']),
            job_id=int(row['job_id']),
            part_id=int(row['part_id']),
            state=str(row['state']),
            progress_ms=int(row['progress_ms']),
            mode=int(row['mode']),
            fontsize=int(row['fontsize']),
            color=int(row['color']),
            content=str(row['content']),
            account_id=int(row['account_id']),
            account_state=str(row['account_state']),
            credential_version=int(row['credential_version']),
            aid=aid,
            bvid=bvid,
            cid=cid,
            branch_state=str(row['danmaku_branch_state']),
        )

    async def _start_send(
        self, claim: LeaseClaim, work: _DanmakuWork, now: float
    ) -> None:
        resume_at = int(ceil(now + self._interval_seconds))

        def start(connection: sqlite3.Connection) -> None:
            updated = connection.execute(
                "UPDATE danmaku_items SET state='in_flight',error_code=NULL,"
                'error_message=NULL,next_attempt_at=? WHERE id=? AND lease_owner=? '
                'AND lease_generation=?',
                (resume_at, claim.id, claim.lease_owner, claim.lease_generation),
            )
            if updated.rowcount != 1:
                raise LeaseLost('danmaku item lease was lost')
            connection.execute(
                'UPDATE danmaku_items SET next_attempt_at=MAX(next_attempt_at,?) '
                'WHERE part_id IN (SELECT part.id FROM upload_parts part '
                'JOIN upload_jobs job ON job.id=part.job_id WHERE job.account_id=?) '
                "AND state IN ('prepared','in_flight')",
                (resume_at, work.account_id),
            )

        await self._database.write(start)
        audit(
            'danmaku_send_started',
            job_id=work.job_id,
            part_id=work.part_id,
            item_id=work.id,
            cid=work.cid,
            progress_ms=work.progress_ms,
            attempt=claim.attempt,
            result='started',
        )

    async def _confirm(
        self, claim: LeaseClaim, work: _DanmakuWork, dmid: Optional[int]
    ) -> None:
        def confirm(connection: sqlite3.Connection) -> None:
            updated = connection.execute(
                "UPDATE danmaku_items SET state='confirmed',dmid=?,error_code=NULL,"
                'error_message=NULL,lease_owner=NULL,lease_until=NULL '
                'WHERE id=? AND lease_owner=? AND lease_generation=?',
                (dmid, claim.id, claim.lease_owner, claim.lease_generation),
            )
            if updated.rowcount != 1:
                raise LeaseLost('danmaku item lease was lost')
            connection.execute(
                'UPDATE danmaku_items SET error_code=NULL,error_message=NULL '
                'WHERE error_code=36703 AND part_id IN ('
                'SELECT part.id FROM upload_parts part JOIN upload_jobs job '
                'ON job.id=part.job_id WHERE job.account_id=?)',
                (work.account_id,),
            )

        await self._database.write(confirm)

    async def _rate_limited_count(
        self, account_id: int, *, excluding_item_id: int
    ) -> int:
        return int(
            await self._database.scalar(
                'SELECT COUNT(*) FROM danmaku_items item '
                'JOIN upload_parts part ON part.id=item.part_id '
                'JOIN upload_jobs job ON job.id=part.job_id '
                'WHERE job.account_id=? AND item.id!=? AND item.error_code=36703',
                (account_id, excluding_item_id),
            )
        )

    async def _defer_account(self, account_id: int, resume_at: int) -> None:
        await self._database.execute(
            'UPDATE danmaku_items SET next_attempt_at=MAX(next_attempt_at,?) '
            'WHERE part_id IN (SELECT part.id FROM upload_parts part '
            'JOIN upload_jobs job ON job.id=part.job_id WHERE job.account_id=?) '
            "AND state IN ('prepared','in_flight')",
            (resume_at, account_id),
        )

    def _request_params(self, work: _DanmakuWork) -> Dict[str, Any]:
        return {
            'type': 1,
            'oid': work.cid,
            'aid': work.aid,
            'msg': work.content,
            'progress': work.progress_ms,
            'color': work.color,
            'fontsize': work.fontsize,
            'pool': 0,
            'mode': work.mode if work.mode in (1, 4, 5) else 1,
            'rnd': int(self._clock() * 1_000_000),
        }

    def _deadline(self, seconds: int) -> int:
        return int(ceil(self._clock() + seconds))

    async def _release(self, claim: LeaseClaim, next_attempt_at: int) -> None:
        await self._update_item(
            claim, {'next_attempt_at': next_attempt_at}, release=True
        )

    async def _update_item(
        self, claim: LeaseClaim, values: Mapping[str, Any], *, release: bool = False
    ) -> None:
        allowed = {'dmid', 'error_code', 'error_message', 'next_attempt_at', 'state'}
        if not values or not set(values) <= allowed:
            raise ValueError('invalid danmaku item update')
        assignments = ['{}=?'.format(column) for column in values]
        parameters: List[Any] = list(values.values())
        if release:
            assignments.extend(('lease_owner=NULL', 'lease_until=NULL'))
        parameters.extend((claim.id, claim.lease_owner, claim.lease_generation))
        updated = await self._database.execute(
            'UPDATE danmaku_items SET {} WHERE id=? AND lease_owner=? '
            'AND lease_generation=?'.format(','.join(assignments)),
            parameters,
        )
        if updated != 1:
            raise LeaseLost('danmaku item lease was lost')

    @staticmethod
    def _next_value(values: List[int], previous: Optional[int]) -> int:
        if previous not in values:
            return values[0]
        index = values.index(previous)
        return values[(index + 1) % len(values)]

    @classmethod
    def _response_dmid(cls, response: Mapping[str, Any]) -> Optional[int]:
        data = response.get('data')
        if not isinstance(data, Mapping):
            return None
        return cls._positive_int(data.get('dmid') or data.get('dmid_str'))

    @staticmethod
    def _positive_int(value: Any) -> Optional[int]:
        if type(value) is int:
            result = value
        elif isinstance(value, str) and value.isdigit():
            result = int(value)
        else:
            return None
        return result if result > 0 else None
