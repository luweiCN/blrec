from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import sqlite3
import time
import unicodedata
import uuid
from dataclasses import dataclass
from pathlib import Path
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

from lxml import etree

from blrec.logging.audit import audit

from .accounts import (
    AccountNotFound,
    AccountPaused,
    AccountWriteGate,
    CredentialVersionChanged,
)
from .credentials import CredentialNotFound
from .crypto import CredentialBundle, InvalidCredentialBundle, InvalidCredentialKey
from .database import BiliUploadDatabase, LeaseClaim, LeaseLost
from .errors import (
    BiliApiError,
    DefinitelyNotSent,
    ProtocolContractError,
    RemoteOutcomeUnknown,
)

__all__ = (
    'CommentItemPlan',
    'CommentPlanner',
    'CommentPublisher',
    'CommentRecord',
    'PartXml',
)


@dataclass(frozen=True)
class PartXml:
    part_index: int
    path: Path


@dataclass(frozen=True)
class CommentRecord:
    kind: str
    part_index: int
    timestamp_seconds: float
    source_index: int
    user: str
    price: int
    message: str
    gift_name: str
    guard_count: int = 1


@dataclass(frozen=True)
class CommentItemPlan:
    ordinal: int
    kind: str
    parent_ordinal: Optional[int]
    content: str
    request_fingerprint: str


@dataclass(frozen=True)
class _CommentWork:
    id: int
    job_id: int
    ordinal: int
    kind: str
    content: str
    state: str
    account_id: int
    account_uid: int
    account_state: str
    credential_version: int
    aid: int
    branch_state: str


class CommentPlanner:
    HEADER = 'SC 和上舰列表\n'
    TRUNCATION_SUFFIX = '……（内容过长已截断）'

    def __init__(
        self,
        database: Optional[BiliUploadDatabase] = None,
        *,
        max_chars: int = 1000,
        clock: Callable[[], float] = time.time,
    ) -> None:
        minimum = len(self.HEADER) + len(self.TRUNCATION_SUFFIX) + 1
        if max_chars < minimum:
            raise ValueError('comment length limit is too small')
        self._database = database
        self._max_chars = max_chars
        self._clock = clock

    def render(self, parts: Sequence[PartXml]) -> List[str]:
        return self._render_records(self.extract(parts))

    def extract(self, parts: Sequence[PartXml]) -> Tuple[CommentRecord, ...]:
        records: List[CommentRecord] = []
        for part in parts:
            records.extend(self._extract_part(part))
        records.sort(
            key=lambda record: (
                record.part_index,
                record.timestamp_seconds,
                record.source_index,
            )
        )
        return tuple(records)

    def render_record(self, record: CommentRecord) -> str:
        timestamp = self._timestamp(record.timestamp_seconds)
        prefix = '{}#{}  '.format(record.part_index, timestamp)
        if record.kind == 'sc':
            return '{}{}发送了{}元留言：{}'.format(
                prefix, record.user, record.price, record.message
            )
        if record.kind == 'guard':
            months = (
                '{}个月'.format(record.guard_count) if record.guard_count > 1 else ''
            )
            return '{}{}开通了{}{}'.format(
                prefix, record.user, months, record.gift_name
            )
        raise ValueError("unsupported comment record '{}'".format(record.kind))

    def create_items(
        self, records: Sequence[CommentRecord], *, account_uid: int, aid: int
    ) -> Tuple[CommentItemPlan, ...]:
        segments = self._render_records(records)
        if not segments:
            return ()
        items = []
        for ordinal, content in enumerate(segments):
            kind = 'root' if ordinal == 0 else 'reply'
            parent_ordinal = None if ordinal == 0 else 0
            items.append(
                CommentItemPlan(
                    ordinal=ordinal,
                    kind=kind,
                    parent_ordinal=parent_ordinal,
                    content=content,
                    request_fingerprint=self._fingerprint(
                        kind, account_uid, aid, parent_ordinal, content
                    ),
                )
            )
        root_fingerprint = items[0].request_fingerprint
        pin_ordinal = len(items)
        items.append(
            CommentItemPlan(
                ordinal=pin_ordinal,
                kind='pin',
                parent_ordinal=0,
                content=root_fingerprint,
                request_fingerprint=self._fingerprint(
                    'pin', account_uid, aid, 0, root_fingerprint
                ),
            )
        )
        return tuple(items)

    async def create(self, job_id: int) -> None:
        database = self._require_database()
        job = await database.fetchone(
            'SELECT job.id,job.account_id,job.aid,job.comment_branch_state,'
            'job.state,account.uid AS account_uid FROM upload_jobs job '
            'JOIN bili_accounts account ON account.id=job.account_id '
            'WHERE job.id=?',
            (job_id,),
        )
        if job is None:
            raise ValueError("unknown upload job '{}'".format(job_id))
        if str(job['comment_branch_state']) != 'pending':
            return
        aid = self._positive_int(job['aid'])
        if str(job['state']) != 'approved' or aid is None:
            raise ValueError('comment job is not ready')
        account_id = int(job['account_id'])
        account_uid = int(job['account_uid'])
        part_rows = await database.fetchall(
            'SELECT part_index,xml_path FROM upload_parts '
            'WHERE job_id=? ORDER BY part_index',
            (job_id,),
        )
        if not part_rows:
            await self._skip(job_id, 'skipped_source_missing')
            return
        parts = []
        for row in part_rows:
            xml_path = row['xml_path']
            if xml_path is None or not os.path.isfile(str(xml_path)):
                await self._skip(job_id, 'skipped_source_missing')
                return
            parts.append(PartXml(int(row['part_index']), Path(str(xml_path))))
        loop = asyncio.get_running_loop()
        records = await loop.run_in_executor(None, self.extract, tuple(parts))
        plans = self.create_items(records, account_uid=account_uid, aid=aid)
        if not plans:
            await self._skip(job_id, 'skipped_no_content')
            return
        now = int(self._clock())

        def persist(connection: sqlite3.Connection) -> None:
            current = connection.execute(
                'SELECT state,account_id,aid,comment_branch_state '
                'FROM upload_jobs WHERE id=?',
                (job_id,),
            ).fetchone()
            if (
                current is None
                or str(current['state']) != 'approved'
                or str(current['comment_branch_state']) != 'pending'
                or int(current['account_id']) != account_id
                or self._positive_int(current['aid']) != aid
            ):
                return
            existing = int(
                connection.execute(
                    'SELECT COUNT(*) FROM comment_items WHERE job_id=?', (job_id,)
                ).fetchone()[0]
            )
            if existing:
                raise ValueError('comment items already exist for pending branch')
            for plan in plans:
                connection.execute(
                    'INSERT INTO comment_items('
                    'job_id,ordinal,kind,parent_ordinal,content,'
                    'request_fingerprint,state) '
                    "VALUES(?,?,?,?,?,?,'prepared')",
                    (
                        job_id,
                        plan.ordinal,
                        plan.kind,
                        plan.parent_ordinal,
                        plan.content,
                        plan.request_fingerprint,
                    ),
                )
            connection.execute(
                "UPDATE upload_jobs SET comment_branch_state='running',updated_at=? "
                "WHERE id=? AND comment_branch_state='pending'",
                (now, job_id),
            )

        await database.write(persist)

    def _extract_part(self, part: PartXml) -> List[CommentRecord]:
        if part.part_index <= 0:
            raise ValueError('part index must be positive')
        records = []
        parser = etree.iterparse(
            str(part.path), events=('end',), resolve_entities=False, no_network=True
        )
        for source_index, (_event, element) in enumerate(parser):
            if element.tag in ('sc', 'guard'):
                timestamp = float(self._required_attribute(element, 'ts'))
                if timestamp < 0:
                    raise ValueError('comment timestamp must not be negative')
                user = self._clean(element.get('user') or '') or '未知用户'
                if element.tag == 'sc':
                    raw_price = int(float(self._required_attribute(element, 'price')))
                    records.append(
                        CommentRecord(
                            'sc',
                            part.part_index,
                            timestamp,
                            source_index,
                            user,
                            raw_price // 1000,
                            self._clean(element.text or ''),
                            '',
                        )
                    )
                else:
                    records.append(
                        CommentRecord(
                            'guard',
                            part.part_index,
                            timestamp,
                            source_index,
                            user,
                            0,
                            '',
                            self._clean(element.get('giftname') or '') or '舰长',
                            self._positive_int(element.get('count')) or 1,
                        )
                    )
            element.clear()
            parent = element.getparent()
            if parent is not None:
                while element.getprevious() is not None:
                    del parent[0]
        return records

    def _render_records(self, records: Sequence[CommentRecord]) -> List[str]:
        if not records:
            return []
        sorted_records = sorted(
            records,
            key=lambda record: (
                record.part_index,
                record.timestamp_seconds,
                record.source_index,
            ),
        )
        capacity = self._max_chars - len(self.HEADER)
        segments: List[str] = []
        lines: List[str] = []
        for record in sorted_records:
            line = self._truncate(self.render_record(record), capacity)
            candidate = self.HEADER + '\n'.join((*lines, line))
            if lines and len(candidate) > self._max_chars:
                segments.append(self.HEADER + '\n'.join(lines))
                lines = [line]
            else:
                lines.append(line)
        if lines:
            segments.append(self.HEADER + '\n'.join(lines))
        return segments

    def _truncate(self, line: str, capacity: int) -> str:
        if len(line) <= capacity:
            return line
        keep = capacity - len(self.TRUNCATION_SUFFIX)
        return line[:keep] + self.TRUNCATION_SUFFIX

    async def _skip(self, job_id: int, state: str) -> None:
        assert state in ('skipped_no_content', 'skipped_source_missing')
        await self._require_database().execute(
            'UPDATE upload_jobs SET comment_branch_state=?,updated_at=? '
            "WHERE id=? AND state='approved' AND comment_branch_state='pending'",
            (state, int(self._clock()), job_id),
        )

    def _require_database(self) -> BiliUploadDatabase:
        if self._database is None:
            raise RuntimeError('comment planner has no database')
        return self._database

    @staticmethod
    def _timestamp(value: float) -> str:
        seconds = max(0, int(value))
        hours, remainder = divmod(seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        return '{:02d}:{:02d}:{:02d}'.format(hours, minutes, seconds)

    @staticmethod
    def _clean(value: str) -> str:
        normalized = ''.join(
            ' ' if unicodedata.category(character) == 'Cc' else character
            for character in value
        )
        return re.sub(r'\s+', ' ', normalized).strip()

    @staticmethod
    def _required_attribute(element: Any, name: str) -> str:
        value = element.get(name)
        if not isinstance(value, str) or not value:
            raise ValueError("comment XML is missing '{}'".format(name))
        return value

    @staticmethod
    def _fingerprint(
        kind: str,
        account_uid: int,
        aid: int,
        parent_ordinal: Optional[int],
        content: str,
    ) -> str:
        payload = json.dumps(
            {
                'kind': kind,
                'account_uid': account_uid,
                'aid': aid,
                'parent_ordinal': parent_ordinal,
                'content': content,
            },
            ensure_ascii=False,
            separators=(',', ':'),
            sort_keys=True,
        ).encode('utf8')
        return hashlib.sha256(payload).hexdigest()

    @staticmethod
    def _positive_int(value: Any) -> Optional[int]:
        if type(value) is int:
            result = value
        elif isinstance(value, str) and value.isdigit():
            result = int(value)
        else:
            return None
        return result if result > 0 else None


class CommentPublisher:
    _DORMANT_UNTIL = 2_147_483_647
    _CHALLENGE_CODES = frozenset((-412, -352, 412, 429, 12015))
    _PERMANENT_CODES = frozenset(
        (-403, 403, 12002, 12003, 12009, 12016, 12025, 12035, 12045, 12052)
    )

    def __init__(
        self,
        database: BiliUploadDatabase,
        protocol: Any,
        *,
        bundle_loader: Callable[[int], Awaitable[CredentialBundle]],
        account_gates: AccountWriteGate,
        worker_id: Optional[str] = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._database = database
        self._protocol = protocol
        self._bundle_loader = bundle_loader
        self._account_gates = account_gates
        self._worker_id = worker_id or 'comment-{}'.format(uuid.uuid4().hex)
        self._clock = clock

    async def run_once(self) -> Optional[int]:
        claim = await self._database.claim(
            'comment_items',
            ('prepared', 'in_flight', 'unknown_outcome'),
            self._worker_id,
            now=int(self._clock()),
        )
        if claim is None:
            return None
        await self._process(claim)
        return claim.id

    async def _process(self, claim: LeaseClaim) -> None:
        work = await self._load(claim)
        if work.branch_state != 'running':
            await self._release(claim, next_attempt_at=self._DORMANT_UNTIL)
            return
        if work.state == 'in_flight':
            await self._mark_unknown(claim, '评论在进程中断前已发出，结果需要远端对账')
            return
        root_rpid = await self._prerequisite_root(work)
        if work.kind != 'root' and root_rpid is None:
            await self._release(claim, next_attempt_at=int(self._clock()) + 5)
            return
        if work.account_state != 'active':
            await self._pause(claim, work.job_id, '投稿账号不可用，自动评论已暂停')
            return
        try:
            gate = self._account_gates.for_account(work.account_id)
            async with gate.hold(work.credential_version):
                bundle = await self._bundle_loader(work.account_id)
                if work.state == 'unknown_outcome':
                    await self._reconcile(claim, work, bundle, root_rpid)
                elif work.kind == 'pin':
                    assert root_rpid is not None
                    await self._publish_pin(claim, work, bundle, root_rpid)
                else:
                    await self._publish_text(claim, work, bundle, root_rpid)
        except (AccountNotFound, AccountPaused, CredentialVersionChanged):
            await self._pause(claim, work.job_id, '投稿账号在评论执行期间发生变化')
        except (CredentialNotFound, InvalidCredentialBundle, InvalidCredentialKey):
            await self._pause(claim, work.job_id, '投稿账号凭据无法读取')

    async def _publish_text(
        self,
        claim: LeaseClaim,
        work: _CommentWork,
        bundle: CredentialBundle,
        root_rpid: Optional[int],
    ) -> None:
        await self._update_item(
            claim, {'state': 'in_flight', 'error_code': None, 'error_message': None}
        )
        params: Dict[str, Any] = {
            'type': 1,
            'oid': work.aid,
            'message': work.content,
            'plat': 1,
        }
        if root_rpid is not None:
            params['root'] = root_rpid
            params['parent'] = root_rpid
        try:
            response = await self._protocol.add_reply(bundle, params)
        except DefinitelyNotSent:
            await self._retry(claim, '评论请求确认未发出，将自动重试')
            return
        except RemoteOutcomeUnknown:
            await self._mark_unknown(claim, '评论请求可能已送达，等待远端对账')
            return
        except BiliApiError as error:
            await self._handle_api_error(claim, work, error, pin=False)
            return
        except ProtocolContractError:
            await self._pause(claim, work.job_id, '评论协议响应不符合预期')
            return
        rpid = self._response_rpid(response)
        if rpid is None:
            await self._mark_unknown(claim, '评论接口未返回 RPID，等待远端对账')
            return
        await self._update_item(
            claim,
            {
                'state': 'confirmed',
                'rpid': rpid,
                'error_code': None,
                'error_message': None,
            },
            release=True,
        )
        audit(
            'comment_confirmed',
            job_id=work.job_id,
            item_id=claim.id,
            kind=work.kind,
            rpid=rpid,
            attempt=claim.attempt,
            result='confirmed',
        )
        await self._complete_if_done(work.job_id)

    async def _publish_pin(
        self,
        claim: LeaseClaim,
        work: _CommentWork,
        bundle: CredentialBundle,
        root_rpid: int,
    ) -> None:
        await self._update_item(
            claim, {'state': 'in_flight', 'error_code': None, 'error_message': None}
        )
        try:
            await self._protocol.top_reply(
                bundle, {'type': 1, 'oid': work.aid, 'rpid': root_rpid, 'action': 1}
            )
        except DefinitelyNotSent:
            await self._retry(claim, '置顶请求确认未发出，将自动重试')
            return
        except RemoteOutcomeUnknown:
            await self._pause_unknown_pin(claim, work.job_id)
            return
        except BiliApiError as error:
            await self._handle_api_error(claim, work, error, pin=True)
            return
        except ProtocolContractError:
            await self._pause(claim, work.job_id, '置顶协议响应不符合预期')
            return
        await self._update_item(
            claim,
            {
                'state': 'confirmed',
                'rpid': root_rpid,
                'error_code': None,
                'error_message': None,
            },
            release=True,
        )
        audit(
            'comment_confirmed',
            job_id=work.job_id,
            item_id=claim.id,
            kind=work.kind,
            rpid=root_rpid,
            attempt=claim.attempt,
            result='confirmed',
        )
        await self._complete_if_done(work.job_id)

    async def _reconcile(
        self,
        claim: LeaseClaim,
        work: _CommentWork,
        bundle: CredentialBundle,
        root_rpid: Optional[int],
    ) -> None:
        if work.kind == 'pin':
            await self._pause_unknown_pin(claim, work.job_id)
            return
        try:
            if work.kind == 'root':
                response = await self._protocol.list_replies(
                    bundle, {'type': 1, 'oid': work.aid, 'mode': 2, 'next': 0, 'ps': 20}
                )
            else:
                assert root_rpid is not None
                response = await self._protocol.reply_detail(
                    bundle,
                    {'type': 1, 'oid': work.aid, 'root': root_rpid, 'pn': 1, 'ps': 20},
                )
            matches = [
                reply
                for reply in self._reply_entries(response)
                if self._reply_matches(work, reply, root_rpid)
            ]
        except (BiliApiError, DefinitelyNotSent, RemoteOutcomeUnknown):
            await self._pause(claim, work.job_id, '评论远端对账失败，需要人工确认')
            return
        except ProtocolContractError:
            await self._pause(claim, work.job_id, '评论远端对账响应不符合预期')
            return
        if len(matches) != 1:
            await self._pause(
                claim, work.job_id, '无法唯一确认评论是否已发送，需要人工确认'
            )
            return
        rpid = self._positive_int(matches[0].get('rpid'))
        assert rpid is not None
        await self._update_item(
            claim,
            {
                'state': 'confirmed',
                'rpid': rpid,
                'error_code': None,
                'error_message': None,
            },
            release=True,
        )
        audit(
            'comment_confirmed',
            job_id=work.job_id,
            item_id=claim.id,
            kind=work.kind,
            rpid=rpid,
            attempt=claim.attempt,
            reconciled=True,
            result='confirmed',
        )
        await self._complete_if_done(work.job_id)

    async def _handle_api_error(
        self, claim: LeaseClaim, work: _CommentWork, error: BiliApiError, *, pin: bool
    ) -> None:
        if not pin and error.code == 12051:
            await self._mark_unknown(claim, 'B 站提示重复评论，需要远端对账')
            return
        if error.code in self._PERMANENT_CODES:
            await self._fail(
                claim, work.job_id, error.code, 'B 站拒绝评论或评论区不可用'
            )
            return
        if error.code in self._CHALLENGE_CODES:
            await self._pause(
                claim,
                work.job_id,
                'B 站要求验证或触发风控，自动评论已暂停',
                error_code=error.code,
            )
            return
        await self._pause(
            claim,
            work.job_id,
            'B 站评论接口返回错误（{}），需要人工确认'.format(error.code),
            error_code=error.code,
        )

    async def _prerequisite_root(self, work: _CommentWork) -> Optional[int]:
        if work.kind == 'root':
            return None
        root = await self._database.fetchone(
            'SELECT rpid,state FROM comment_items '
            "WHERE job_id=? AND ordinal=0 AND kind='root'",
            (work.job_id,),
        )
        if root is None or str(root['state']) != 'confirmed':
            return None
        root_rpid = self._positive_int(root['rpid'])
        if root_rpid is None:
            return None
        if work.kind == 'pin':
            remaining = int(
                await self._database.scalar(
                    'SELECT COUNT(*) FROM comment_items '
                    "WHERE job_id=? AND kind IN ('root','reply') "
                    "AND state!='confirmed'",
                    (work.job_id,),
                )
            )
        else:
            remaining = int(
                await self._database.scalar(
                    'SELECT COUNT(*) FROM comment_items '
                    "WHERE job_id=? AND kind IN ('root','reply') "
                    "AND ordinal<? AND state!='confirmed'",
                    (work.job_id, work.ordinal),
                )
            )
        return root_rpid if remaining == 0 else None

    async def _load(self, claim: LeaseClaim) -> _CommentWork:
        row = await self._database.fetchone(
            'SELECT item.id,item.job_id,item.ordinal,item.kind,item.content,'
            'item.state,job.account_id,job.aid,job.comment_branch_state,'
            'account.uid AS account_uid,account.state AS account_state,'
            'account.credential_version FROM comment_items item '
            'JOIN upload_jobs job ON job.id=item.job_id '
            'JOIN bili_accounts account ON account.id=job.account_id '
            'WHERE item.id=? AND item.lease_owner=? AND item.lease_generation=?',
            (claim.id, claim.lease_owner, claim.lease_generation),
        )
        if row is None:
            raise LeaseLost('comment item lease was lost')
        aid = self._positive_int(row['aid'])
        if aid is None:
            raise ValueError('comment job has no AID')
        return _CommentWork(
            id=int(row['id']),
            job_id=int(row['job_id']),
            ordinal=int(row['ordinal']),
            kind=str(row['kind']),
            content=str(row['content']),
            state=str(row['state']),
            account_id=int(row['account_id']),
            account_uid=int(row['account_uid']),
            account_state=str(row['account_state']),
            credential_version=int(row['credential_version']),
            aid=aid,
            branch_state=str(row['comment_branch_state']),
        )

    async def _retry(self, claim: LeaseClaim, message: str) -> None:
        delay = min(300, 2 ** min(claim.attempt, 8))
        await self._update_item(
            claim,
            {
                'state': 'prepared',
                'error_code': None,
                'error_message': message,
                'next_attempt_at': int(self._clock()) + delay,
            },
            release=True,
        )

    async def _mark_unknown(self, claim: LeaseClaim, message: str) -> None:
        await self._update_item(
            claim,
            {
                'state': 'unknown_outcome',
                'error_code': None,
                'error_message': message,
                'next_attempt_at': int(self._clock()),
                'priority': 1,
            },
            release=True,
        )

    async def _pause_unknown_pin(self, claim: LeaseClaim, job_id: int) -> None:
        await self._pause(
            claim,
            job_id,
            '置顶请求结果未知；已有评论不会重发，需要人工确认',
            item_state='unknown_outcome',
        )

    async def _pause(
        self,
        claim: LeaseClaim,
        job_id: int,
        message: str,
        *,
        error_code: Optional[int] = None,
        item_state: str = 'prepared',
    ) -> None:
        await self._branch_transition(
            claim,
            job_id,
            branch_state='paused',
            item_state=item_state,
            error_code=error_code,
            message=message,
        )

    async def _fail(
        self, claim: LeaseClaim, job_id: int, error_code: int, message: str
    ) -> None:
        await self._branch_transition(
            claim,
            job_id,
            branch_state='failed',
            item_state='failed_permanent',
            error_code=error_code,
            message=message,
        )

    async def _branch_transition(
        self,
        claim: LeaseClaim,
        job_id: int,
        *,
        branch_state: str,
        item_state: str,
        error_code: Optional[int],
        message: str,
    ) -> None:
        now = int(self._clock())

        def transition(connection: sqlite3.Connection) -> None:
            updated = connection.execute(
                'UPDATE comment_items SET state=?,error_code=?,error_message=?,'
                'next_attempt_at=?,lease_owner=NULL,lease_until=NULL '
                'WHERE id=? AND lease_owner=? AND lease_generation=?',
                (
                    item_state,
                    error_code,
                    message,
                    self._DORMANT_UNTIL,
                    claim.id,
                    claim.lease_owner,
                    claim.lease_generation,
                ),
            )
            if updated.rowcount != 1:
                raise LeaseLost('comment item lease was lost')
            connection.execute(
                'UPDATE upload_jobs SET comment_branch_state=?,review_reason=?,'
                "updated_at=? WHERE id=? AND comment_branch_state='running'",
                (branch_state, message, now, job_id),
            )

        await self._database.write(transition)
        audit(
            'comment_branch_transition',
            level='ERROR' if branch_state == 'failed' else 'WARNING',
            job_id=job_id,
            item_id=claim.id,
            state=branch_state,
            error_code=error_code,
            reason=message,
            result=branch_state,
        )

    async def _release(self, claim: LeaseClaim, *, next_attempt_at: int) -> None:
        await self._update_item(
            claim, {'next_attempt_at': next_attempt_at}, release=True
        )

    async def _update_item(
        self, claim: LeaseClaim, values: Mapping[str, Any], *, release: bool = False
    ) -> None:
        allowed = {
            'error_code',
            'error_message',
            'next_attempt_at',
            'priority',
            'rpid',
            'state',
        }
        if not values or not set(values) <= allowed:
            raise ValueError('invalid comment item update')
        assignments = ['{}=?'.format(column) for column in values]
        parameters: List[Any] = list(values.values())
        if release:
            assignments.extend(('lease_owner=NULL', 'lease_until=NULL'))
        parameters.extend((claim.id, claim.lease_owner, claim.lease_generation))
        updated = await self._database.execute(
            'UPDATE comment_items SET {} WHERE id=? AND lease_owner=? '
            'AND lease_generation=?'.format(','.join(assignments)),
            parameters,
        )
        if updated != 1:
            raise LeaseLost('comment item lease was lost')

    async def _complete_if_done(self, job_id: int) -> None:
        remaining = int(
            await self._database.scalar(
                'SELECT COUNT(*) FROM comment_items '
                "WHERE job_id=? AND state!='confirmed'",
                (job_id,),
            )
        )
        if remaining == 0:
            await self._database.execute(
                "UPDATE upload_jobs SET comment_branch_state='completed',updated_at=? "
                "WHERE id=? AND comment_branch_state='running'",
                (int(self._clock()), job_id),
            )

    @classmethod
    def _reply_entries(cls, response: Mapping[str, Any]) -> List[Mapping[str, Any]]:
        data = response.get('data')
        if not isinstance(data, Mapping):
            raise ProtocolContractError('comment list response is incomplete')
        entries: List[Mapping[str, Any]] = []
        for name in ('replies', 'top_replies'):
            value = data.get(name)
            if isinstance(value, list):
                entries.extend(entry for entry in value if isinstance(entry, Mapping))
        upper = data.get('upper')
        if isinstance(upper, Mapping):
            entries.extend(
                entry for entry in upper.values() if isinstance(entry, Mapping)
            )
        return entries

    def _reply_matches(
        self, work: _CommentWork, reply: Mapping[str, Any], root_rpid: Optional[int]
    ) -> bool:
        content = reply.get('content')
        if not isinstance(content, Mapping) or content.get('message') != work.content:
            return False
        owner_uid = self._positive_int(reply.get('mid'))
        if owner_uid is None:
            member = reply.get('member')
            if isinstance(member, Mapping):
                owner_uid = self._positive_int(member.get('mid'))
        expected_parent = 0 if root_rpid is None else root_rpid
        return (
            self._positive_int(reply.get('rpid')) is not None
            and self._positive_int(reply.get('oid')) == work.aid
            and owner_uid == work.account_uid
            and self._nonnegative_int(reply.get('root')) == expected_parent
            and self._nonnegative_int(reply.get('parent')) == expected_parent
        )

    @classmethod
    def _response_rpid(cls, response: Mapping[str, Any]) -> Optional[int]:
        data = response.get('data')
        if not isinstance(data, Mapping):
            return None
        return cls._positive_int(data.get('rpid') or data.get('rpid_str'))

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
    def _nonnegative_int(value: Any) -> Optional[int]:
        if type(value) is int:
            result = value
        elif isinstance(value, str) and value.isdigit():
            result = int(value)
        else:
            return None
        return result if result >= 0 else None
