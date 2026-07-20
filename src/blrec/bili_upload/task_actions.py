from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Mapping, Optional, Tuple

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
from .deletion_worker import LocalDeletionRejected, LocalDeletionWorker
from .errors import (
    BiliApiError,
    DefinitelyNotSent,
    ProtocolContractError,
    RemoteOutcomeUnknown,
)
from .transcode_remux import TranscodeRemuxer
from .upos import FileIdentity, UposUploader, UposUploadPaused, UposUploadStopped

__all__ = (
    'UploadTaskActionManager',
    'UploadTaskActionPreview',
    'UploadTaskActionRejected',
    'UploadTaskSettingsView',
    'UploadTaskUpdateResult',
)


class UploadTaskActionRejected(ValueError):
    pass


class _RepairDeletionRequested(RuntimeError):
    pass


@dataclass(frozen=True)
class UploadTaskActionPreview:
    job_id: int
    room_id: int
    title: str
    account_display_name: str
    reason: str


@dataclass(frozen=True)
class UploadTaskUpdateResult:
    collection_cleared: bool


@dataclass(frozen=True)
class UploadTaskSettingsView:
    job_id: int
    account_id: int
    settings: Mapping[str, Any]
    editable: bool
    blocked_reason: Optional[str]


_EditPayloadBuilder = Callable[
    [int, Mapping[int, int], Optional[str]], Awaitable[Mapping[str, Any]]
]


@dataclass(frozen=True)
class _RepairJob:
    id: int
    account_id: int
    credential_version: int
    aid: int
    bvid: str


@dataclass(frozen=True)
class _LocalPart:
    id: int
    part_index: int
    submitted_page: int
    path: str
    file_identity: Optional[str]
    remote_filename: str


@dataclass(frozen=True)
class _RemotePart:
    local_id: int
    part_index: int
    filename: str
    cid: int
    fail_code: int
    xcode_state: int
    fail_desc: str
    state: str


class UploadTaskActionManager:
    _ACTIVE_REPAIR_STATES = frozenset(('queued', 'checking', 'reuploading', 'editing'))
    _REPAIRABLE_JOB_STATES = frozenset(
        ('waiting_review', 'approved', 'rejected', 'paused', 'completed')
    )

    def __init__(
        self,
        database: BiliUploadDatabase,
        protocol: Any,
        uploader: UposUploader,
        *,
        bundle_loader: Callable[[int], Awaitable[CredentialBundle]],
        account_gates: AccountWriteGate,
        edit_payload_builder: _EditPayloadBuilder,
        recording_root: Optional[Path] = None,
        remuxer: Optional[TranscodeRemuxer] = None,
        deletion_worker: Optional[LocalDeletionWorker] = None,
        worker_id: Optional[str] = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._database = database
        self._protocol = protocol
        self._uploader = uploader
        self._bundle_loader = bundle_loader
        self._account_gates = account_gates
        self._edit_payload_builder = edit_payload_builder
        self._recording_root = (
            None
            if recording_root is None
            else Path(
                os.path.abspath(os.path.expanduser(str(recording_root)))
            ).resolve()
        )
        self._remuxer = remuxer or TranscodeRemuxer(
            Path(database.path).parent / 'transcode-remux'
        )
        self._deletion_worker = deletion_worker
        if self._deletion_worker is None and self._recording_root is not None:
            self._deletion_worker = LocalDeletionWorker(
                database,
                recording_root=self._recording_root,
                clip_root=self._recording_root.parent / 'clips',
                clock=clock,
            )
        self._worker_id = worker_id or 'repair-{}'.format(uuid.uuid4().hex)
        self._clock = clock
        self._run_lock = asyncio.Lock()

    async def retryable_failed_job_ids(self) -> Tuple[int, ...]:
        return tuple(item.job_id for item in await self.retryable_failed_jobs())

    async def pause_upload(self, job_id: int, *, manager_subject: str) -> str:
        if not manager_subject:
            raise UploadTaskActionRejected('管理员身份不能为空')
        now = int(self._clock())

        def pause(connection: sqlite3.Connection) -> str:
            job = connection.execute(
                'SELECT state,submit_state,operator_paused,lease_until '
                'FROM upload_jobs WHERE id=?',
                (job_id,),
            ).fetchone()
            if job is None:
                raise UploadTaskActionRejected('上传任务不存在')
            if bool(job['operator_paused']):
                return '上传任务已经暂停'
            state = str(job['state'])
            if state not in ('ready', 'uploading', 'submitting'):
                raise UploadTaskActionRejected('当前状态不能暂停上传')
            if str(job['submit_state']) != 'prepared':
                raise UploadTaskActionRejected('投稿请求已经开始，不能暂停上传')
            if self._has_active_lease(job, now):
                raise UploadTaskActionRejected('任务正在执行，请稍后再试')
            unsafe = connection.execute(
                'SELECT 1 FROM upload_parts WHERE job_id=? '
                "AND upload_state IN ('completing','unknown_outcome') LIMIT 1",
                (job_id,),
            ).fetchone()
            if unsafe is not None:
                raise UploadTaskActionRejected('存在结果未知的分 P，不能安全暂停')
            connection.execute(
                "UPDATE upload_jobs SET state='paused',operator_paused=1,"
                'operator_resume_state=?,review_reason=?,next_attempt_at=0,'
                'lease_owner=NULL,lease_until=NULL,updated_at=? WHERE id=?',
                (state, '管理员已暂停上传任务', now, job_id),
            )
            self._audit(
                connection,
                manager_subject=manager_subject,
                action='pause_upload_job',
                job_id=job_id,
                old_state=state,
                new_state='paused/operator',
                reason='管理员暂停上传任务',
                now=now,
            )
            return '上传任务已暂停'

        return await self._database.write(pause)

    async def resume_upload(self, job_id: int, *, manager_subject: str) -> str:
        if not manager_subject:
            raise UploadTaskActionRejected('管理员身份不能为空')
        now = int(self._clock())

        def resume(connection: sqlite3.Connection) -> str:
            job = connection.execute(
                'SELECT job.state,job.submit_state,job.operator_paused,'
                'job.lease_until,account.state AS account_state '
                'FROM upload_jobs job JOIN bili_accounts account '
                'ON account.id=job.account_id WHERE job.id=?',
                (job_id,),
            ).fetchone()
            if job is None:
                raise UploadTaskActionRejected('上传任务不存在')
            if str(job['state']) != 'paused' or not bool(job['operator_paused']):
                raise UploadTaskActionRejected('任务不是由管理员暂停的')
            if str(job['submit_state']) != 'prepared':
                raise UploadTaskActionRejected('投稿结果未知，不能盲目继续')
            if str(job['account_state']) != 'active':
                raise UploadTaskActionRejected('投稿账号当前不可用')
            if self._has_active_lease(job, now):
                raise UploadTaskActionRejected('任务正在执行，请稍后再试')
            parts = connection.execute(
                'SELECT upload_state FROM upload_parts WHERE job_id=? '
                'ORDER BY part_index',
                (job_id,),
            ).fetchall()
            if not parts:
                raise UploadTaskActionRejected('上传任务没有分 P')
            states = {str(part['upload_state']) for part in parts}
            if states & {'completing', 'unknown_outcome', 'failed'}:
                raise UploadTaskActionRejected('分 P 状态需要先处理，不能继续上传')
            new_state = 'submitting' if states == {'confirmed'} else 'ready'
            connection.execute(
                'UPDATE upload_jobs SET state=?,operator_paused=0,'
                'operator_resume_state=NULL,review_reason=?,next_attempt_at=0,'
                'lease_owner=NULL,lease_until=NULL,updated_at=? WHERE id=?',
                (new_state, '管理员已继续上传任务', now, job_id),
            )
            self._audit(
                connection,
                manager_subject=manager_subject,
                action='resume_upload_job',
                job_id=job_id,
                old_state='paused/operator',
                new_state=new_state,
                reason='管理员继续上传任务',
                now=now,
            )
            return '上传任务已继续'

        return await self._database.write(resume)

    async def update_task(
        self,
        job_id: int,
        *,
        account_id: int,
        changes: Mapping[str, Any],
        manager_subject: str,
    ) -> UploadTaskUpdateResult:
        if not manager_subject:
            raise UploadTaskActionRejected('管理员身份不能为空')
        if account_id <= 0:
            raise UploadTaskActionRejected('投稿账号无效')
        allowed = {
            'title',
            'description',
            'dynamic',
            'tid',
            'tags',
            'creation_statement_id',
            'original_authorization',
            'copyright',
            'source',
            'is_only_self',
            'publish_dynamic',
            'no_reprint',
            'up_selection_reply',
            'up_close_reply',
            'up_close_danmu',
            'cover_mode',
            'cover_asset_id',
            'collection_season_id',
            'collection_section_id',
            'publish_delay_seconds',
            'auto_comment',
            'danmaku_backfill',
            'filters',
            'part_titles',
        }
        if not changes or not set(changes) <= allowed:
            raise UploadTaskActionRejected('投稿设置包含不支持的字段')
        now = int(self._clock())

        def update(connection: sqlite3.Connection) -> UploadTaskUpdateResult:
            job = connection.execute(
                'SELECT account_id,state,submit_state,operator_paused,lease_until,'
                'policy_snapshot_json FROM upload_jobs WHERE id=?',
                (job_id,),
            ).fetchone()
            if job is None:
                raise UploadTaskActionRejected('上传任务不存在')
            if str(job['state']) not in ('waiting_artifacts', 'ready', 'paused') or (
                str(job['state']) == 'paused' and not bool(job['operator_paused'])
            ):
                raise UploadTaskActionRejected('任务已经开始上传，不能修改')
            if str(job['submit_state']) != 'prepared' or self._has_active_lease(
                job, now
            ):
                raise UploadTaskActionRejected('任务已经开始上传，不能修改')
            part_rows = connection.execute(
                'SELECT id,upload_state,remote_filename,upload_session_json,xml_path '
                'FROM upload_parts WHERE job_id=? ORDER BY part_index',
                (job_id,),
            ).fetchall()
            chunks = connection.execute(
                'SELECT 1 FROM upload_chunks WHERE part_id IN('
                'SELECT id FROM upload_parts WHERE job_id=?) LIMIT 1',
                (job_id,),
            ).fetchone()
            if (
                not part_rows
                or chunks is not None
                or any(
                    str(part['upload_state']) != 'prepared'
                    or part['remote_filename'] is not None
                    or part['upload_session_json'] is not None
                    for part in part_rows
                )
            ):
                raise UploadTaskActionRejected('任务已经开始上传，不能修改')
            account = connection.execute(
                'SELECT uid,credential_version,state FROM bili_accounts WHERE id=?',
                (account_id,),
            ).fetchone()
            if account is None or str(account['state']) != 'active':
                raise UploadTaskActionRejected('投稿账号当前不可用')
            try:
                snapshot = json.loads(str(job['policy_snapshot_json']))
            except json.JSONDecodeError:
                raise UploadTaskActionRejected('任务投稿设置损坏') from None
            if not isinstance(snapshot, dict) or snapshot.get('format_version') != 4:
                raise UploadTaskActionRejected('任务投稿设置版本不支持修改')
            snapshot.update(dict(changes))
            collection_cleared = int(job['account_id']) != account_id and (
                snapshot.get('collection_season_id') is not None
                or snapshot.get('collection_section_id') is not None
            )
            if collection_cleared:
                snapshot['collection_season_id'] = None
                snapshot['collection_section_id'] = None
            snapshot['account_id'] = account_id
            snapshot['account_uid'] = int(account['uid'])
            snapshot['account_credential_version_at_creation'] = int(
                account['credential_version']
            )
            self._validate_task_snapshot(snapshot, len(part_rows))
            auto_comment = bool(snapshot['auto_comment'])
            danmaku_backfill = bool(snapshot['danmaku_backfill'])
            has_collection = snapshot['collection_section_id'] is not None
            connection.execute(
                'UPDATE upload_jobs SET account_id=?,policy_snapshot_json=?,'
                'comment_branch_state=?,danmaku_branch_state=?,'
                'collection_branch_state=?,collection_error=NULL,updated_at=? '
                'WHERE id=?',
                (
                    account_id,
                    json.dumps(
                        snapshot,
                        ensure_ascii=False,
                        separators=(',', ':'),
                        sort_keys=True,
                    ),
                    'pending' if auto_comment else 'disabled',
                    'pending' if danmaku_backfill else 'disabled',
                    'pending' if has_collection else 'disabled',
                    now,
                    job_id,
                ),
            )
            for part in part_rows:
                danmaku_state = 'disabled'
                if danmaku_backfill:
                    danmaku_state = 'pending' if part['xml_path'] else 'missing_source'
                connection.execute(
                    'UPDATE upload_parts SET danmaku_import_state=? WHERE id=?',
                    (danmaku_state, int(part['id'])),
                )
            self._audit(
                connection,
                manager_subject=manager_subject,
                action='update_upload_job',
                job_id=job_id,
                old_state='account:{}'.format(job['account_id']),
                new_state='account:{}'.format(account_id),
                reason='管理员修改未开始上传的任务设置',
                now=now,
            )
            return UploadTaskUpdateResult(collection_cleared=collection_cleared)

        return await self._database.write(update)

    async def task_settings(self, job_id: int) -> UploadTaskSettingsView:
        job = await self._database.fetchone(
            'SELECT id,account_id,state,submit_state,operator_paused,lease_until,'
            'policy_snapshot_json FROM upload_jobs WHERE id=?',
            (job_id,),
        )
        if job is None:
            raise UploadTaskActionRejected('上传任务不存在')
        try:
            snapshot = json.loads(str(job['policy_snapshot_json']))
        except json.JSONDecodeError:
            raise UploadTaskActionRejected('任务投稿设置损坏') from None
        if not isinstance(snapshot, dict):
            raise UploadTaskActionRejected('任务投稿设置损坏')
        parts = await self._database.fetchall(
            'SELECT id,upload_state,remote_filename,upload_session_json '
            'FROM upload_parts WHERE job_id=? ORDER BY part_index',
            (job_id,),
        )
        chunks = await self._database.scalar(
            'SELECT COUNT(*) FROM upload_chunks WHERE part_id IN('
            'SELECT id FROM upload_parts WHERE job_id=?)',
            (job_id,),
        )
        editable = (
            str(job['state']) in ('waiting_artifacts', 'ready', 'paused')
            and (str(job['state']) != 'paused' or bool(job['operator_paused']))
            and str(job['submit_state']) == 'prepared'
            and not self._has_active_lease(job, int(self._clock()))
            and bool(parts)
            and int(chunks or 0) == 0
            and all(
                str(part['upload_state']) == 'prepared'
                and part['remote_filename'] is None
                and part['upload_session_json'] is None
                for part in parts
            )
        )
        return UploadTaskSettingsView(
            job_id=int(job['id']),
            account_id=int(job['account_id']),
            settings=snapshot,
            editable=editable,
            blocked_reason=None if editable else '任务已经开始上传，不能再修改',
        )

    async def retryable_failed_jobs(self) -> Tuple[UploadTaskActionPreview, ...]:
        rows = await self._database.fetchall(
            'SELECT job.id,session.room_id,session.title,'
            'account.display_name,job.review_reason FROM upload_jobs job '
            'JOIN recording_sessions session ON session.id=job.session_id '
            'JOIN bili_accounts account ON account.id=job.account_id '
            "WHERE job.state='paused' AND account.state='active' "
            'AND job.operator_paused=0 '
            "AND job.submit_state NOT IN ('in_flight','unknown_outcome') "
            "AND job.repair_state NOT IN ('queued','checking','reuploading','editing') "
            'AND NOT EXISTS('
            'SELECT 1 FROM upload_parts part WHERE part.job_id=job.id '
            "AND part.upload_state IN ('completing','unknown_outcome')) "
            'ORDER BY job.id'
        )
        return tuple(
            UploadTaskActionPreview(
                job_id=int(row['id']),
                room_id=int(row['room_id']),
                title=str(row['title']),
                account_display_name=str(row['display_name']),
                reason=(
                    '' if row['review_reason'] is None else str(row['review_reason'])
                ),
            )
            for row in rows
        )

    async def retry_failed(self, job_id: int, *, manager_subject: str) -> str:
        if not manager_subject:
            raise UploadTaskActionRejected('管理员身份不能为空')
        now = int(self._clock())

        def retry(connection: sqlite3.Connection) -> str:
            job = connection.execute(
                'SELECT job.state,job.submit_state,job.aid,job.bvid,'
                'job.repair_state,job.operator_paused,job.lease_until,'
                'account.state AS account_state '
                'FROM upload_jobs job JOIN bili_accounts account '
                'ON account.id=job.account_id WHERE job.id=?',
                (job_id,),
            ).fetchone()
            if job is None:
                raise UploadTaskActionRejected('上传任务不存在')
            if str(job['state']) != 'paused':
                raise UploadTaskActionRejected('只有已暂停的任务可以重新排队')
            if bool(job['operator_paused']):
                raise UploadTaskActionRejected('任务由管理员暂停，请使用继续上传')
            if str(job['account_state']) != 'active':
                raise UploadTaskActionRejected('投稿账号当前不可用')
            if str(job['repair_state']) in self._ACTIVE_REPAIR_STATES:
                raise UploadTaskActionRejected('转码修复正在执行')
            if job['lease_until'] is not None and int(job['lease_until']) > now:
                raise UploadTaskActionRejected('任务正在执行，请稍后再试')
            submit_state = str(job['submit_state'])
            if submit_state in ('in_flight', 'unknown_outcome'):
                raise UploadTaskActionRejected('投稿结果未知，自动重试可能产生重复稿件')
            parts = connection.execute(
                'SELECT id,artifact_state,upload_state FROM upload_parts '
                'WHERE job_id=? ORDER BY part_index',
                (job_id,),
            ).fetchall()
            if not parts:
                raise UploadTaskActionRejected('上传任务没有分 P')
            if any(
                str(part['upload_state']) in ('completing', 'unknown_outcome')
                for part in parts
            ):
                raise UploadTaskActionRejected(
                    '分 P 上传结果未知，自动重试可能造成重复上传'
                )

            old_state = '{}/{}'.format(job['state'], submit_state)
            if submit_state == 'confirmed':
                if job['aid'] is None or not job['bvid']:
                    raise UploadTaskActionRejected('已投稿任务缺少 AID/BVID')
                new_state = 'waiting_review'
            elif submit_state in ('prepared', 'failed_permanent'):
                failed = [
                    part for part in parts if str(part['upload_state']) == 'failed'
                ]
                if any(str(part['artifact_state']) != 'ready' for part in failed):
                    raise UploadTaskActionRejected('失败分 P 的本地视频不可用')
                for part in failed:
                    part_id = int(part['id'])
                    connection.execute(
                        'DELETE FROM upload_chunks WHERE part_id=?', (part_id,)
                    )
                    connection.execute(
                        "UPDATE upload_parts SET upload_state='prepared',"
                        'remote_filename=NULL,upload_session_json=NULL '
                        'WHERE id=? AND job_id=?',
                        (part_id, job_id),
                    )
                remaining = connection.execute(
                    'SELECT upload_state FROM upload_parts WHERE job_id=?', (job_id,)
                ).fetchall()
                all_confirmed = all(
                    str(part['upload_state']) == 'confirmed' for part in remaining
                )
                new_state = 'submitting' if all_confirmed else 'ready'
                submit_state = 'prepared'
            else:
                raise UploadTaskActionRejected('当前投稿状态不能安全重试')

            updated = connection.execute(
                'UPDATE upload_jobs SET state=?,submit_state=?,next_attempt_at=0,'
                'review_reason=?,lease_owner=NULL,lease_until=NULL,updated_at=? '
                'WHERE id=?',
                (new_state, submit_state, '管理员已重新排队失败任务', now, job_id),
            )
            if updated.rowcount != 1:
                raise UploadTaskActionRejected('上传任务状态已经发生变化')
            self._audit(
                connection,
                manager_subject=manager_subject,
                action='retry_upload_job',
                job_id=job_id,
                old_state=old_state,
                new_state='{}/{}'.format(new_state, submit_state),
                reason='管理员手动重试失败任务',
                now=now,
            )
            return '失败任务已重新排队'

        return await self._database.write(retry)

    async def skip_upload(self, job_id: int, *, manager_subject: str) -> str:
        if not manager_subject:
            raise UploadTaskActionRejected('管理员身份不能为空')
        now = int(self._clock())

        def skip(connection: sqlite3.Connection) -> str:
            job = connection.execute(
                'SELECT session_id,state,submit_state,repair_state,lease_until,'
                'preupload_finalized '
                'FROM upload_jobs WHERE id=?',
                (job_id,),
            ).fetchone()
            if job is None:
                raise UploadTaskActionRejected('上传任务不存在')
            if (
                str(job['state']) not in ('waiting_artifacts', 'ready')
                or str(job['submit_state']) != 'prepared'
            ):
                raise UploadTaskActionRejected('只有尚未开始上传的任务可以设为不上传')
            if self._has_active_lease(job, now):
                raise UploadTaskActionRejected('任务正在执行，请稍后再试')
            if str(job['repair_state']) in self._ACTIVE_REPAIR_STATES:
                raise UploadTaskActionRejected('转码修复正在执行')
            parts = connection.execute(
                'SELECT upload_state FROM upload_parts WHERE job_id=?', (job_id,)
            ).fetchall()
            if bool(job['preupload_finalized']) and any(
                str(part['upload_state']) != 'prepared' for part in parts
            ):
                raise UploadTaskActionRejected('任务已经开始上传，不能设为不上传')
            session_id = int(job['session_id'])
            self._delete_job_children(connection, job_id)
            connection.execute('DELETE FROM upload_jobs WHERE id=?', (job_id,))
            connection.execute(
                'INSERT OR REPLACE INTO upload_suppressions('
                'session_id,reason,manager_subject,created_at) VALUES(?,?,?,?)',
                (session_id, 'manager_skipped', manager_subject, now),
            )
            connection.execute(
                "UPDATE recording_sessions SET upload_intent='skip',"
                "upload_decision='skip',upload_resolution_state='not_requested',"
                'upload_resolution_error=NULL,upload_resolved_at=? WHERE id=?',
                (now, session_id),
            )
            self._audit(
                connection,
                manager_subject=manager_subject,
                action='skip_upload_job',
                job_id=job_id,
                old_state='{}/{}'.format(job['state'], job['submit_state']),
                new_state='suppressed',
                reason='管理员将该场录像设为不上传',
                now=now,
            )
            return '该场录像已设为不上传'

        async with self._run_lock:
            return await self._database.write(skip)

    async def repost_as_new(self, job_id: int, *, manager_subject: str) -> str:
        if not manager_subject:
            raise UploadTaskActionRejected('管理员身份不能为空')
        now = int(self._clock())

        def repost(connection: sqlite3.Connection) -> str:
            job = connection.execute(
                'SELECT job.session_id,job.account_id,job.policy_snapshot_json,'
                'job.state,job.submit_state,job.aid,job.bvid,job.repair_state,'
                'job.lease_until,account.state AS account_state '
                'FROM upload_jobs job JOIN bili_accounts account '
                'ON account.id=job.account_id WHERE job.id=?',
                (job_id,),
            ).fetchone()
            if job is None:
                raise UploadTaskActionRejected('上传任务不存在')
            if (
                str(job['state']) not in ('approved', 'completed')
                or str(job['submit_state']) != 'confirmed'
            ):
                raise UploadTaskActionRejected('只有审核通过的任务可以重新投稿')
            if job['aid'] is None or not job['bvid']:
                raise UploadTaskActionRejected('原任务缺少 AID/BVID')
            if str(job['account_state']) != 'active':
                raise UploadTaskActionRejected('投稿账号当前不可用')
            if self._has_active_lease(job, now):
                raise UploadTaskActionRejected('任务正在执行，请稍后再试')
            if str(job['repair_state']) in self._ACTIVE_REPAIR_STATES:
                raise UploadTaskActionRejected('转码修复正在执行')
            parts = connection.execute(
                'SELECT id,final_path,artifact_state FROM upload_parts '
                'WHERE job_id=? ORDER BY part_index',
                (job_id,),
            ).fetchall()
            if not parts or any(
                str(part['artifact_state']) != 'ready'
                or not part['final_path']
                or not os.path.isfile(str(part['final_path']))
                for part in parts
            ):
                raise UploadTaskActionRejected('本地成品文件不完整，不能重新投稿')
            try:
                snapshot = json.loads(str(job['policy_snapshot_json']))
            except json.JSONDecodeError:
                raise UploadTaskActionRejected('原投稿设置无法读取') from None
            if not isinstance(snapshot, dict):
                raise UploadTaskActionRejected('原投稿设置无法读取')
            comment_state = (
                'pending' if bool(snapshot.get('auto_comment')) else 'disabled'
            )
            danmaku_state = (
                'pending' if bool(snapshot.get('danmaku_backfill')) else 'disabled'
            )
            collection_state = (
                'pending'
                if snapshot.get('collection_section_id') is not None
                else 'disabled'
            )
            connection.execute(
                'INSERT INTO upload_job_archives('
                'session_id,old_job_id,account_id,aid,bvid,state,submit_state,'
                'policy_snapshot_json,reason,archived_at) VALUES(?,?,?,?,?,?,?,?,?,?)',
                (
                    int(job['session_id']),
                    job_id,
                    int(job['account_id']),
                    int(job['aid']),
                    str(job['bvid']),
                    str(job['state']),
                    str(job['submit_state']),
                    str(job['policy_snapshot_json']),
                    'repost_as_new',
                    now,
                ),
            )
            connection.execute(
                'DELETE FROM danmaku_items WHERE part_id IN('
                'SELECT id FROM upload_parts WHERE job_id=?)',
                (job_id,),
            )
            connection.execute(
                'DELETE FROM upload_chunks WHERE part_id IN('
                'SELECT id FROM upload_parts WHERE job_id=?)',
                (job_id,),
            )
            connection.execute('DELETE FROM comment_items WHERE job_id=?', (job_id,))
            connection.execute(
                "UPDATE upload_parts SET upload_state='prepared',"
                "danmaku_import_state=?,remote_filename=NULL,cid=NULL,"
                'upload_session_json=NULL,transcode_state=\'unknown\','
                'transcode_fail_code=NULL,transcode_fail_desc=NULL WHERE job_id=?',
                (danmaku_state, job_id),
            )
            connection.execute(
                "UPDATE upload_jobs SET state='ready',submit_state='prepared',"
                'comment_branch_state=?,danmaku_branch_state=?,'
                'collection_branch_state=?,aid=NULL,bvid=NULL,review_reason=?,'
                'lease_owner=NULL,lease_until=NULL,attempt=0,next_attempt_at=0,'
                'scheduled_publish_at=NULL,collection_error=NULL,'
                'upload_completed_at=NULL,submitted_at=NULL,approved_at=NULL,'
                "submission_verification_state='pending',"
                'submission_verified_at=NULL,submission_verification_json=NULL,'
                "repair_state='idle',repair_message=NULL,repair_error=NULL,"
                'repair_attempt=0,repair_requested_at=NULL,repair_completed_at=NULL,'
                'updated_at=? WHERE id=?',
                (
                    comment_state,
                    danmaku_state,
                    collection_state,
                    '管理员重新投稿；原稿件 {} 已保留'.format(job['bvid']),
                    now,
                    job_id,
                ),
            )
            self._audit(
                connection,
                manager_subject=manager_subject,
                action='repost_upload_job',
                job_id=job_id,
                old_state='{}/{}'.format(job['state'], job['bvid']),
                new_state='ready/new_archive',
                reason='管理员要求重新投稿为新稿件，原远端稿件保留',
                now=now,
            )
            return '已保留原稿件记录，并重新排队投稿为新稿件'

        async with self._run_lock:
            return await self._database.write(repost)

    async def delete_local_task(self, job_id: int, *, manager_subject: str) -> str:
        if not manager_subject:
            raise UploadTaskActionRejected('管理员身份不能为空')
        if self._recording_root is None:
            raise UploadTaskActionRejected('录像根目录未配置，无法安全删除文件')
        deletion_worker = self._deletion_worker
        if deletion_worker is None:
            raise UploadTaskActionRejected('本地删除服务当前不可用')
        row = await self._database.fetchone(
            'SELECT session_id FROM upload_jobs WHERE id=?', (job_id,)
        )
        if row is None:
            raise UploadTaskActionRejected('上传任务不存在')
        try:
            await deletion_worker.request_session(
                int(row['session_id']), manager_subject=manager_subject
            )
        except LocalDeletionRejected as error:
            raise UploadTaskActionRejected(str(error)) from None
        return '已排队删除本地任务及文件'

    async def set_session_upload_intent(
        self, session_id: int, intent: str, *, manager_subject: str
    ) -> str:
        if not manager_subject:
            raise UploadTaskActionRejected('管理员身份不能为空')
        if intent not in ('upload', 'skip'):
            raise UploadTaskActionRejected('本场上传设置无效')
        row = await self._database.fetchone(
            'SELECT session.id,job.id AS job_id FROM recording_sessions session '
            'LEFT JOIN upload_jobs job ON job.session_id=session.id '
            'WHERE session.id=?',
            (session_id,),
        )
        if row is None:
            raise UploadTaskActionRejected('录制场次不存在')
        if row['job_id'] is not None:
            if intent == 'skip':
                return await self.skip_upload(
                    int(row['job_id']), manager_subject=manager_subject
                )
            return '本场录像已经创建上传任务'
        now = int(self._clock())

        def update(connection: sqlite3.Connection) -> str:
            current = connection.execute(
                'SELECT upload_intent FROM recording_sessions WHERE id=?', (session_id,)
            ).fetchone()
            if current is None:
                raise UploadTaskActionRejected('录制场次不存在')
            old_intent = str(current['upload_intent'])
            connection.execute(
                'UPDATE recording_sessions SET upload_intent=? WHERE id=?',
                (intent, session_id),
            )
            if intent == 'skip':
                connection.execute(
                    'INSERT OR REPLACE INTO upload_suppressions('
                    'session_id,reason,manager_subject,created_at) VALUES(?,?,?,?)',
                    (session_id, 'manager_skipped', manager_subject, now),
                )
                message = '本场录像已设为不上传'
            else:
                connection.execute(
                    'DELETE FROM upload_suppressions WHERE session_id=?', (session_id,)
                )
                message = '本场录像将在文件就绪后上传'
            self._audit_session(
                connection,
                manager_subject=manager_subject,
                action='set_session_upload_intent',
                session_id=session_id,
                old_state=old_intent,
                new_state=intent,
                reason=message,
                now=now,
            )
            return message

        async with self._run_lock:
            return await self._database.write(update)

    async def delete_session(self, session_id: int, *, manager_subject: str) -> str:
        if not manager_subject:
            raise UploadTaskActionRejected('管理员身份不能为空')
        if self._recording_root is None:
            raise UploadTaskActionRejected('录像根目录未配置，无法安全删除文件')
        deletion_worker = self._deletion_worker
        if deletion_worker is None:
            raise UploadTaskActionRejected('本地删除服务当前不可用')
        try:
            await deletion_worker.request_session(
                session_id, manager_subject=manager_subject
            )
        except LocalDeletionRejected as error:
            raise UploadTaskActionRejected(str(error)) from None
        return '已排队删除本地场次及文件'

    async def request_transcode_repair(
        self, job_id: int, *, manager_subject: str
    ) -> str:
        if not manager_subject:
            raise UploadTaskActionRejected('管理员身份不能为空')
        now = int(self._clock())

        def request(connection: sqlite3.Connection) -> str:
            job = connection.execute(
                'SELECT job.state,job.submit_state,job.aid,job.bvid,'
                'job.repair_state,job.lease_until,account.state AS account_state '
                'FROM upload_jobs job JOIN bili_accounts account '
                'ON account.id=job.account_id WHERE job.id=?',
                (job_id,),
            ).fetchone()
            if job is None:
                raise UploadTaskActionRejected('上传任务不存在')
            state = str(job['state'])
            repair_state = str(job['repair_state'])
            if state not in self._REPAIRABLE_JOB_STATES:
                raise UploadTaskActionRejected('稿件尚未提交，不能检查转码状态')
            if str(job['submit_state']) != 'confirmed' or (
                job['aid'] is None or not job['bvid']
            ):
                raise UploadTaskActionRejected('任务缺少已确认的 AID/BVID')
            if str(job['account_state']) != 'active':
                raise UploadTaskActionRejected('投稿账号当前不可用')
            if repair_state in self._ACTIVE_REPAIR_STATES or (
                repair_state == 'waiting_review' and state == 'waiting_review'
            ):
                raise UploadTaskActionRejected('转码修复正在执行或等待审核')
            if job['lease_until'] is not None and int(job['lease_until']) > now:
                raise UploadTaskActionRejected('任务正在执行，请稍后再试')
            connection.execute(
                "UPDATE upload_jobs SET repair_state='queued',repair_message=?,"
                'repair_error=NULL,repair_requested_at=?,repair_completed_at=NULL,'
                'updated_at=? WHERE id=?',
                ('等待检查 B 站转码状态', now, now, job_id),
            )
            self._audit(
                connection,
                manager_subject=manager_subject,
                action='repair_upload_transcode',
                job_id=job_id,
                old_state=repair_state,
                new_state='queued',
                reason='管理员请求检查并修复转码异常分 P',
                now=now,
            )
            return '已排队检查 B 站转码状态'

        return await self._database.write(request)

    async def request_danmaku_backfill(
        self, job_id: int, *, manager_subject: str
    ) -> str:
        if not manager_subject:
            raise UploadTaskActionRejected('管理员身份不能为空')
        now = int(self._clock())

        def request(connection: sqlite3.Connection) -> str:
            job = connection.execute(
                'SELECT job.state,job.submit_state,job.aid,job.bvid,'
                'job.danmaku_branch_state,job.lease_until,'
                'account.state AS account_state FROM upload_jobs job '
                'JOIN bili_accounts account ON account.id=job.account_id '
                'WHERE job.id=?',
                (job_id,),
            ).fetchone()
            if job is None:
                raise UploadTaskActionRejected('上传任务不存在')
            if (
                str(job['state']) not in ('approved', 'completed')
                or str(job['submit_state']) != 'confirmed'
                or job['aid'] is None
                or not job['bvid']
            ):
                raise UploadTaskActionRejected('只有审核通过的稿件可以回灌弹幕')
            if str(job['danmaku_branch_state']) != 'disabled':
                raise UploadTaskActionRejected('该稿件的弹幕回灌已经启用或处理过')
            if str(job['account_state']) != 'active':
                raise UploadTaskActionRejected('投稿账号当前不可用')
            if self._has_active_lease(job, now):
                raise UploadTaskActionRejected('任务正在执行，请稍后再试')
            parts = connection.execute(
                'SELECT id,xml_path,cid,danmaku_import_state FROM upload_parts '
                'WHERE job_id=? ORDER BY part_index',
                (job_id,),
            ).fetchall()
            if not parts:
                raise UploadTaskActionRejected('上传任务没有分 P')
            if any(str(part['danmaku_import_state']) != 'disabled' for part in parts):
                raise UploadTaskActionRejected('弹幕回灌状态不一致，不能重复创建')
            if any(self._positive_int(part['cid']) is None for part in parts):
                raise UploadTaskActionRejected('稿件分 P 缺少 CID，暂时不能回灌')
            if any(
                not part['xml_path'] or not os.path.isfile(str(part['xml_path']))
                for part in parts
            ):
                raise UploadTaskActionRejected('本地弹幕文件不完整，不能回灌')
            existing_items = connection.execute(
                'SELECT 1 FROM danmaku_items WHERE part_id IN('
                'SELECT id FROM upload_parts WHERE job_id=?) LIMIT 1',
                (job_id,),
            ).fetchone()
            if existing_items is not None:
                raise UploadTaskActionRejected('已有弹幕发送记录，不能重复创建')
            connection.execute(
                "UPDATE upload_parts SET danmaku_import_state='pending' "
                'WHERE job_id=?',
                (job_id,),
            )
            updated = connection.execute(
                "UPDATE upload_jobs SET state='approved',"
                "danmaku_branch_state='importing',updated_at=? "
                "WHERE id=? AND state IN ('approved','completed') "
                "AND danmaku_branch_state='disabled'",
                (now, job_id),
            )
            if updated.rowcount != 1:
                raise UploadTaskActionRejected('上传任务状态已经发生变化')
            self._audit(
                connection,
                manager_subject=manager_subject,
                action='backfill_upload_danmaku',
                job_id=job_id,
                old_state='{}/disabled'.format(job['state']),
                new_state='approved/importing',
                reason='管理员手动启用审核通过稿件的弹幕回灌',
                now=now,
            )
            return '已排队回灌 {} 个分 P 的弹幕'.format(len(parts))

        return await self._database.write(request)

    async def recover_interrupted(self) -> None:
        now = int(self._clock())

        def recover(connection: sqlite3.Connection) -> None:
            interrupted = connection.execute(
                'SELECT job.id,job.repair_state,session.deletion_state,'
                'session.cancellation_generation '
                'FROM upload_jobs job JOIN recording_sessions session '
                'ON session.id=job.session_id WHERE job.repair_state IN '
                "('checking','reuploading','editing')"
            ).fetchall()
            for row in interrupted:
                job_id = int(row['id'])
                editing = str(row['repair_state']) == 'editing'
                deleting = str(row['deletion_state']) != 'none'
                generation_row = connection.execute(
                    'SELECT source_generation FROM owner_handoff_outcomes '
                    "WHERE owner_kind='repair' AND owner_id=? "
                    "AND side_effect_key LIKE 'lease:%' "
                    'ORDER BY id DESC LIMIT 1',
                    (job_id,),
                ).fetchone()
                source_generation = (
                    int(row['cancellation_generation'])
                    if generation_row is None
                    else int(generation_row['source_generation'])
                )
                if editing:
                    connection.execute(
                        'INSERT INTO owner_handoff_outcomes('
                        'owner_kind,owner_id,side_effect_key,source_generation,'
                        'outcome_state,outcome_json,acknowledged_at) '
                        "VALUES('repair',?,'archive_edit',?,"
                        "'unknown_terminal','{}',?) "
                        'ON CONFLICT(owner_kind,owner_id,side_effect_key,'
                        'source_generation) DO UPDATE SET '
                        "outcome_state='unknown_terminal',outcome_json='{}',"
                        'acknowledged_at=excluded.acknowledged_at',
                        (job_id, source_generation, now),
                    )
                lease_outcome = 'unknown_terminal' if editing else 'cancelled_local'
                connection.execute(
                    'UPDATE owner_handoff_outcomes SET outcome_state=?,'
                    "outcome_json='{}',acknowledged_at=? "
                    "WHERE owner_kind='repair' AND owner_id=? "
                    "AND side_effect_key LIKE 'lease:%' "
                    "AND outcome_state='in_flight'",
                    (lease_outcome, now, job_id),
                )
                if deleting:
                    connection.execute(
                        "UPDATE upload_jobs SET state='paused',"
                        "repair_state='failed',repair_message=NULL,repair_error=?,"
                        'lease_owner=NULL,lease_until=NULL,updated_at=? WHERE id=?',
                        ('任务正在删除', now, job_id),
                    )
            connection.execute(
                "UPDATE upload_jobs SET repair_state='queued',repair_message=?,"
                'repair_error=NULL,lease_owner=NULL,lease_until=NULL,updated_at=? '
                "WHERE repair_state IN ('checking','reuploading') "
                'AND operator_paused=0 AND EXISTS('
                'SELECT 1 FROM recording_sessions session '
                'WHERE session.id=upload_jobs.session_id '
                "AND session.deletion_state='none')",
                ('上次修复中断，已重新排队', now),
            )
            connection.execute(
                "UPDATE upload_jobs SET state='paused',"
                "repair_state='unknown_outcome',repair_message=NULL,repair_error=?,"
                'review_reason=?,lease_owner=NULL,lease_until=NULL,updated_at=? '
                "WHERE repair_state='editing' AND operator_paused=0 AND EXISTS("
                'SELECT 1 FROM recording_sessions session '
                'WHERE session.id=upload_jobs.session_id '
                "AND session.deletion_state='none')",
                (
                    '稿件编辑在重启前已发出，远端结果未知',
                    '转码修复的稿件编辑结果未知，需要远端核对',
                    now,
                ),
            )

        await self._database.write(recover)

    async def run_once(self) -> Optional[int]:
        async with self._run_lock:
            claim = await self._claim_repair()
            if claim is None:
                return None
            await self._process_repair(claim)
            return claim.id

    async def _claim_repair(self) -> Optional[LeaseClaim]:
        now = int(self._clock())

        def claim(connection: sqlite3.Connection) -> Optional[LeaseClaim]:
            row = connection.execute(
                'SELECT job.id,job.repair_attempt,'
                'session.cancellation_generation FROM upload_jobs job '
                'JOIN recording_sessions session ON session.id=job.session_id '
                "WHERE job.repair_state='queued' AND job.operator_paused=0 "
                "AND session.deletion_state='none' "
                'AND (job.lease_until IS NULL OR job.lease_until<=?) '
                'ORDER BY job.repair_requested_at,job.id LIMIT 1',
                (now,),
            ).fetchone()
            if row is None:
                return None
            job_id = int(row['id'])
            lease_until = now + BiliUploadDatabase.LEASE_TTL_SECONDS
            updated = connection.execute(
                "UPDATE upload_jobs SET repair_state='checking',repair_message=?,"
                'repair_error=NULL,repair_attempt=repair_attempt+1,'
                'lease_owner=?,lease_generation=lease_generation+1,lease_until=?, '
                "updated_at=? WHERE id=? AND repair_state='queued' "
                'AND operator_paused=0 '
                'AND (lease_until IS NULL OR lease_until<=?) AND EXISTS('
                'SELECT 1 FROM recording_sessions session '
                'WHERE session.id=upload_jobs.session_id '
                "AND session.deletion_state='none')",
                (
                    '正在核对 B 站分 P 转码状态',
                    self._worker_id,
                    lease_until,
                    now,
                    job_id,
                    now,
                ),
            )
            if updated.rowcount != 1:
                return None
            claimed = connection.execute(
                'SELECT job.lease_generation,job.repair_attempt,'
                'session.cancellation_generation FROM upload_jobs job '
                'JOIN recording_sessions session ON session.id=job.session_id '
                'WHERE job.id=?',
                (job_id,),
            ).fetchone()
            assert claimed is not None
            source_generation = int(claimed['cancellation_generation'])
            connection.execute(
                'INSERT INTO owner_handoff_outcomes('
                'owner_kind,owner_id,side_effect_key,source_generation,'
                'outcome_state,outcome_json,acknowledged_at) '
                "VALUES('repair',?,?,?,'in_flight','{}',NULL) "
                'ON CONFLICT(owner_kind,owner_id,side_effect_key,'
                'source_generation) DO UPDATE SET '
                "outcome_state='in_flight',outcome_json='{}',acknowledged_at=NULL",
                (
                    job_id,
                    'lease:{}'.format(int(claimed['lease_generation'])),
                    source_generation,
                ),
            )
            return LeaseClaim(
                table='upload_jobs',
                id=job_id,
                lease_owner=self._worker_id,
                lease_generation=int(claimed['lease_generation']),
                lease_until=lease_until,
                attempt=int(claimed['repair_attempt']),
                cancellation_generation=source_generation,
            )

        return await self._database.write(claim)

    async def _process_repair(self, claim: LeaseClaim) -> None:
        edit_started = False
        try:
            job = await self._load_repair_job(claim)
            audit(
                'transcode_repair_started',
                job_id=job.id,
                account_id=job.account_id,
                aid=job.aid,
                bvid=job.bvid,
                attempt=claim.attempt,
                result='started',
            )
            gate = self._account_gates.for_account(job.account_id)
            async with gate.hold(job.credential_version):
                bundle = await self._bundle_loader(job.account_id)
                await self._assert_active_repair(claim)
                response = await self._protocol.archive_view(
                    bundle,
                    {'topic_grey': 1, 'bvid': job.bvid, 't': int(self._clock() * 1000)},
                )
                remote_parts, cover_url = await self._inspect_remote(job, response)
                await self._store_transcode_inspection(claim, remote_parts)
                failed = [part for part in remote_parts if part.state == 'failed']
                processing = [
                    part for part in remote_parts if part.state == 'processing'
                ]
                if not failed:
                    message = (
                        'B 站仍在转码，暂不重传'
                        if processing
                        else '未发现需要修复的分 P'
                    )
                    await self._finish_noop(claim, message)
                    return

                repair_modes = await self._select_repair_modes(claim, failed)
                await self._verify_local_files(claim, failed)
                remux_part_ids = await self._prepare_remux_artifacts(
                    claim, failed, repair_modes
                )
                try:
                    await self._prepare_failed_parts(claim, failed, repair_modes)
                    for part in failed:
                        await self._assert_active_repair(claim)
                        await self._uploader.upload_part(
                            part.local_id, bundle=bundle, claim=claim
                        )
                finally:
                    if remux_part_ids:
                        await self._restore_remux_paths(claim, remux_part_ids)
                healthy_cids = {
                    part.local_id: part.cid
                    for part in remote_parts
                    if part.state != 'failed'
                }
                payload = await self._edit_payload_builder(
                    job.id, healthy_cids, cover_url
                )
                await self._begin_archive_edit(claim)
                edit_started = True
                await self._call_archive_edit(bundle, payload)
                await self._finish_repair(claim, failed, healthy_cids, repair_modes)
        except DefinitelyNotSent:
            if edit_started:
                await self._finish_edit_failure(
                    claim,
                    reason='稿件编辑请求未发出，可以重新尝试',
                    outcome_state='confirmed_failure',
                )
            else:
                await self._fail_repair(claim, '稿件编辑请求未发出，可以重新尝试')
        except RemoteOutcomeUnknown:
            if edit_started:
                await self._unknown_repair(claim)
            else:
                await self._fail_repair(claim, '远端查询中断，可以重新尝试')
        except (AccountNotFound, AccountPaused, CredentialVersionChanged):
            await self._fail_repair(claim, '投稿账号在修复期间发生变化')
        except (CredentialNotFound, InvalidCredentialBundle, InvalidCredentialKey):
            await self._fail_repair(claim, '投稿账号凭据无法读取')
        except UposUploadStopped:
            await self._fail_repair(claim, '转码修复已停止，可以重新尝试')
        except UposUploadPaused as error:
            await self._fail_repair(claim, str(error))
        except BiliApiError as error:
            if edit_started:
                await self._finish_edit_failure(
                    claim,
                    reason='B 站接口拒绝转码修复请求（{}）'.format(error.code),
                    outcome_state='confirmed_failure',
                )
            else:
                await self._fail_repair(
                    claim, 'B 站接口拒绝转码修复请求（{}）'.format(error.code)
                )
        except LeaseLost:
            await self._cancel_deleted_claim(claim)
        except _RepairDeletionRequested:
            return
        except asyncio.CancelledError:
            await asyncio.shield(
                self._interrupt_repair(claim, edit_started=edit_started)
            )
            raise
        except (ProtocolContractError, OSError, ValueError) as error:
            if edit_started:
                await self._unknown_repair(claim)
                return
            await self._fail_repair(claim, str(error))
        except Exception as error:
            if edit_started:
                await self._unknown_repair(claim)
                return
            await self._fail_repair(claim, str(error) or '转码修复失败')

    async def _load_repair_job(self, claim: LeaseClaim) -> _RepairJob:
        now = int(self._clock())
        row = await self._database.fetchone(
            'SELECT job.id,job.account_id,job.aid,job.bvid,'
            'account.credential_version FROM upload_jobs job '
            'JOIN bili_accounts account ON account.id=job.account_id '
            'JOIN recording_sessions session ON session.id=job.session_id '
            'WHERE job.id=? AND job.lease_owner=? AND job.lease_generation=? '
            "AND job.lease_until>? AND job.repair_state='checking' "
            "AND job.operator_paused=0 AND session.deletion_state='none' "
            'AND session.cancellation_generation=?',
            (
                claim.id,
                claim.lease_owner,
                claim.lease_generation,
                now,
                self._repair_source_generation(claim),
            ),
        )
        if row is None:
            raise LeaseLost('转码修复任务租约已失效')
        aid = self._positive_int(row['aid'])
        bvid = self._text(row['bvid'])
        if aid is None or bvid is None:
            raise ProtocolContractError('转码修复任务缺少 AID/BVID')
        return _RepairJob(
            id=int(row['id']),
            account_id=int(row['account_id']),
            credential_version=int(row['credential_version']),
            aid=aid,
            bvid=bvid,
        )

    @staticmethod
    def _repair_source_generation(claim: LeaseClaim) -> int:
        if claim.cancellation_generation is None:
            raise LeaseLost('转码修复任务缺少删除代次')
        return int(claim.cancellation_generation)

    @staticmethod
    def _repair_lease_key(claim: LeaseClaim) -> str:
        return 'lease:{}'.format(claim.lease_generation)

    def _repair_owner_active(
        self, connection: sqlite3.Connection, claim: LeaseClaim
    ) -> bool:
        row = connection.execute(
            'SELECT session.cancellation_generation,session.deletion_state '
            'FROM upload_jobs job JOIN recording_sessions session '
            'ON session.id=job.session_id WHERE job.id=? '
            'AND job.lease_owner=? AND job.lease_generation=?',
            (claim.id, claim.lease_owner, claim.lease_generation),
        ).fetchone()
        if row is None:
            raise LeaseLost('转码修复任务租约已失效')
        return (
            int(row['cancellation_generation']) == self._repair_source_generation(claim)
            and str(row['deletion_state']) == 'none'
        )

    def _ack_repair_intent(
        self,
        connection: sqlite3.Connection,
        claim: LeaseClaim,
        *,
        side_effect_key: str,
        outcome_state: str,
        now: int,
    ) -> None:
        cursor = connection.execute(
            'UPDATE owner_handoff_outcomes SET outcome_state=?,'
            "outcome_json='{}',acknowledged_at=? "
            "WHERE owner_kind='repair' AND owner_id=? AND side_effect_key=? "
            "AND source_generation=? AND outcome_state='in_flight'",
            (
                outcome_state,
                now,
                claim.id,
                side_effect_key,
                self._repair_source_generation(claim),
            ),
        )
        if cursor.rowcount != 1:
            raise LeaseLost('转码修复交接记录已失效')

    def _release_deleted_claim(
        self, connection: sqlite3.Connection, claim: LeaseClaim, *, now: int
    ) -> None:
        self._ack_repair_intent(
            connection,
            claim,
            side_effect_key=self._repair_lease_key(claim),
            outcome_state='cancelled_local',
            now=now,
        )
        cursor = connection.execute(
            "UPDATE upload_jobs SET state='paused',repair_state='failed',"
            "repair_message=NULL,repair_error='任务正在删除',"
            "review_reason='任务正在删除',lease_owner=NULL,lease_until=NULL,"
            'updated_at=? WHERE id=? AND lease_owner=? AND lease_generation=?',
            (now, claim.id, claim.lease_owner, claim.lease_generation),
        )
        if cursor.rowcount != 1:
            raise LeaseLost('转码修复任务租约已失效')

    async def _cancel_deleted_claim(self, claim: LeaseClaim) -> None:
        now = int(self._clock())

        def cancel(connection: sqlite3.Connection) -> None:
            try:
                active = self._repair_owner_active(connection, claim)
            except LeaseLost:
                return
            if active:
                return
            self._release_deleted_claim(connection, claim, now=now)

        await self._database.write(cancel)

    async def _interrupt_repair(self, claim: LeaseClaim, *, edit_started: bool) -> None:
        now = int(self._clock())

        def interrupt(connection: sqlite3.Connection) -> None:
            try:
                active = self._repair_owner_active(connection, claim)
            except LeaseLost:
                return
            if not active:
                if edit_started:
                    self._ack_repair_intent(
                        connection,
                        claim,
                        side_effect_key='archive_edit',
                        outcome_state='unknown_terminal',
                        now=now,
                    )
                self._release_deleted_claim(connection, claim, now=now)
                return
            if edit_started:
                self._ack_repair_intent(
                    connection,
                    claim,
                    side_effect_key='archive_edit',
                    outcome_state='unknown_terminal',
                    now=now,
                )
                self._ack_repair_intent(
                    connection,
                    claim,
                    side_effect_key=self._repair_lease_key(claim),
                    outcome_state='unknown_terminal',
                    now=now,
                )
                connection.execute(
                    "UPDATE upload_jobs SET state='paused',"
                    "repair_state='unknown_outcome',repair_message=NULL,"
                    'repair_error=?,review_reason=?,lease_owner=NULL,'
                    'lease_until=NULL,updated_at=? WHERE id=?',
                    (
                        '稿件编辑结果未知，请先到创作中心核对',
                        '转码修复的稿件编辑结果未知，需要远端核对',
                        now,
                        claim.id,
                    ),
                )
                return
            connection.execute(
                "UPDATE upload_jobs SET repair_state='queued',repair_message=?,"
                'repair_error=NULL,lease_owner=NULL,lease_until=NULL,updated_at=? '
                'WHERE id=?',
                ('转码修复中断，已重新排队', now, claim.id),
            )
            connection.execute(
                "DELETE FROM owner_handoff_outcomes WHERE owner_kind='repair' "
                'AND owner_id=? AND side_effect_key=? AND source_generation=?',
                (
                    claim.id,
                    self._repair_lease_key(claim),
                    self._repair_source_generation(claim),
                ),
            )

        await self._database.write(interrupt)

    async def _begin_archive_edit(self, claim: LeaseClaim) -> None:
        now = int(self._clock())

        def begin(connection: sqlite3.Connection) -> bool:
            if not self._repair_owner_active(connection, claim):
                self._release_deleted_claim(connection, claim, now=now)
                return False
            cursor = connection.execute(
                "UPDATE upload_jobs SET repair_state='editing',repair_message=?,"
                'updated_at=? WHERE id=? AND lease_owner=? AND lease_generation=?',
                (
                    '正在更新原稿件的异常分 P',
                    now,
                    claim.id,
                    claim.lease_owner,
                    claim.lease_generation,
                ),
            )
            if cursor.rowcount != 1:
                raise LeaseLost('转码修复任务租约已失效')
            connection.execute(
                'INSERT INTO owner_handoff_outcomes('
                'owner_kind,owner_id,side_effect_key,source_generation,'
                'outcome_state,outcome_json,acknowledged_at) '
                "VALUES('repair',?,'archive_edit',?,'in_flight','{}',NULL) "
                'ON CONFLICT(owner_kind,owner_id,side_effect_key,'
                'source_generation) DO UPDATE SET '
                "outcome_state='in_flight',outcome_json='{}',acknowledged_at=NULL",
                (claim.id, self._repair_source_generation(claim)),
            )
            return True

        if not await self._database.write(begin):
            raise _RepairDeletionRequested()

    async def _call_archive_edit(
        self, bundle: CredentialBundle, payload: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        request = asyncio.ensure_future(self._protocol.edit_archive(bundle, payload))
        try:
            return await asyncio.shield(request)
        except asyncio.CancelledError:
            return await request

    async def _finish_edit_failure(
        self, claim: LeaseClaim, *, reason: str, outcome_state: str
    ) -> None:
        now = int(self._clock())

        def settle(connection: sqlite3.Connection) -> bool:
            if self._repair_owner_active(connection, claim):
                connection.execute(
                    "DELETE FROM owner_handoff_outcomes WHERE owner_kind='repair' "
                    "AND owner_id=? AND side_effect_key='archive_edit' "
                    'AND source_generation=?',
                    (claim.id, self._repair_source_generation(claim)),
                )
                cursor = connection.execute(
                    "UPDATE upload_jobs SET repair_state='failed',"
                    'repair_message=NULL,repair_error=?,repair_completed_at=?,'
                    'lease_owner=NULL,lease_until=NULL,updated_at=? WHERE id=? '
                    'AND lease_owner=? AND lease_generation=?',
                    (
                        reason[:500],
                        now,
                        now,
                        claim.id,
                        claim.lease_owner,
                        claim.lease_generation,
                    ),
                )
                if cursor.rowcount != 1:
                    raise LeaseLost('转码修复任务租约已失效')
                connection.execute(
                    "DELETE FROM owner_handoff_outcomes WHERE owner_kind='repair' "
                    'AND owner_id=? AND side_effect_key=? AND source_generation=?',
                    (
                        claim.id,
                        self._repair_lease_key(claim),
                        self._repair_source_generation(claim),
                    ),
                )
                return True
            self._ack_repair_intent(
                connection,
                claim,
                side_effect_key='archive_edit',
                outcome_state=outcome_state,
                now=now,
            )
            self._release_deleted_claim(connection, claim, now=now)
            return False

        active = await self._database.write(settle)
        audit(
            'transcode_repair_failed',
            level='ERROR',
            job_id=claim.id,
            reason=reason[:500],
            result='failed' if active else outcome_state,
        )

    async def _inspect_remote(
        self, job: _RepairJob, response: Mapping[str, Any]
    ) -> Tuple[List[_RemotePart], Optional[str]]:
        data = response.get('data')
        if not isinstance(data, Mapping):
            raise ProtocolContractError('稿件详情响应结构不符合预期')
        archive = data.get('archive')
        if not isinstance(archive, Mapping):
            raise ProtocolContractError('稿件详情缺少稿件标识')
        if (
            self._positive_int(archive.get('aid')) != job.aid
            or self._text(archive.get('bvid')) != job.bvid
        ):
            raise ProtocolContractError('远端稿件标识与上传任务不一致')
        videos = data.get('videos')
        if not isinstance(videos, list):
            videos = data.get('Videos')
        if not isinstance(videos, list):
            raise ProtocolContractError('稿件详情缺少分 P 信息')
        local_rows = await self._database.fetchall(
            'SELECT id,part_index,source_path,final_path,file_identity,'
            'remote_filename FROM upload_parts WHERE job_id=? ORDER BY part_index',
            (job.id,),
        )
        local_by_filename: Dict[str, _LocalPart] = {}
        for submitted_page, row in enumerate(local_rows, 1):
            filename = self._text(row['remote_filename'])
            if filename is None or filename in local_by_filename:
                raise ProtocolContractError('本地分 P 的远端 filename 不完整或重复')
            final_path = self._text(row['final_path'])
            local_by_filename[filename] = _LocalPart(
                id=int(row['id']),
                part_index=int(row['part_index']),
                submitted_page=submitted_page,
                path=final_path or str(row['source_path']),
                file_identity=self._text(row['file_identity']),
                remote_filename=filename,
            )
        if not local_by_filename:
            raise ProtocolContractError('上传任务没有分 P')

        matched: List[_RemotePart] = []
        seen = set()
        for video in videos:
            if not isinstance(video, Mapping):
                raise ProtocolContractError('远端分 P 信息不完整')
            filename = self._text(video.get('filename'))
            if filename is None or filename in seen:
                raise ProtocolContractError('远端分 P filename 缺失或重复')
            local = local_by_filename.get(filename)
            if local is None:
                raise ProtocolContractError('远端分 P 与本地记录不能一一对应')
            page = self._positive_int(video.get('page'))
            cid = self._positive_int(video.get('cid'))
            if page != local.submitted_page or cid is None:
                raise ProtocolContractError('远端分 P 页码或 CID 不符合预期')
            video_aid = self._positive_int(video.get('aid'))
            video_bvid = self._text(video.get('bvid'))
            if (video_aid is not None and video_aid != job.aid) or (
                video_bvid is not None and video_bvid != job.bvid
            ):
                raise ProtocolContractError('远端分 P 稿件标识不一致')
            fail_code = self._integer(video.get('failCode'), default=0)
            xcode_state = self._integer(video.get('xcodeState'), default=0)
            fail_desc = self._text(video.get('failDesc')) or ''
            state = self._transcode_state(fail_code, xcode_state, fail_desc)
            matched.append(
                _RemotePart(
                    local_id=local.id,
                    part_index=local.part_index,
                    filename=filename,
                    cid=cid,
                    fail_code=fail_code,
                    xcode_state=xcode_state,
                    fail_desc=fail_desc,
                    state=state,
                )
            )
            seen.add(filename)
        if seen != set(local_by_filename):
            raise ProtocolContractError('远端分 P 与本地记录不能一一对应')
        matched.sort(key=lambda part: part.part_index)
        return matched, self._cover_url(archive.get('cover'))

    async def _store_transcode_inspection(
        self, claim: LeaseClaim, parts: List[_RemotePart]
    ) -> None:
        def store(connection: sqlite3.Connection) -> None:
            self._require_claim(connection, claim)
            for part in parts:
                connection.execute(
                    'UPDATE upload_parts SET cid=?,transcode_state=?,'
                    'transcode_fail_code=?,transcode_fail_desc=? '
                    'WHERE id=? AND job_id=?',
                    (
                        part.cid,
                        part.state,
                        part.fail_code,
                        part.fail_desc or None,
                        part.local_id,
                        claim.id,
                    ),
                )

        await self._database.write(store)

    async def _select_repair_modes(
        self, claim: LeaseClaim, failed: List[_RemotePart]
    ) -> Dict[int, str]:
        failed_by_id = {part.local_id: part for part in failed}

        def select(connection: sqlite3.Connection) -> Dict[int, str]:
            self._require_claim(connection, claim)
            placeholders = ','.join('?' for _ in failed_by_id)
            rows = connection.execute(
                'SELECT id,repair_stage,repair_original_attempts,'
                'repair_remux_attempts FROM upload_parts WHERE job_id=? '
                'AND id IN ({})'.format(placeholders),
                (claim.id, *failed_by_id),
            ).fetchall()
            if len(rows) != len(failed_by_id):
                raise ProtocolContractError('异常分 P 的修复记录不完整')
            modes: Dict[int, str] = {}
            for row in rows:
                part_id = int(row['id'])
                stage = str(row['repair_stage'])
                original_attempts = int(row['repair_original_attempts'])
                remux_attempts = int(row['repair_remux_attempts'])
                if stage in ('none', 'original'):
                    mode = 'original'
                elif stage in ('original_waiting_review', 'remux'):
                    mode = 'remux'
                else:
                    raise ProtocolContractError(
                        'P{} 的自动转码修复次数已经用完'.format(
                            failed_by_id[part_id].part_index
                        )
                    )
                if mode == 'original' and original_attempts > 1:
                    raise ProtocolContractError('原文件重传次数记录无效')
                if mode == 'remux' and remux_attempts > 1:
                    raise ProtocolContractError('重新封装次数记录无效')
                diagnostic = failed_by_id[part_id].fail_desc or (
                    'failCode={}, xcodeState={}'.format(
                        failed_by_id[part_id].fail_code,
                        failed_by_id[part_id].xcode_state,
                    )
                )
                connection.execute(
                    'UPDATE upload_parts SET repair_stage=?, '
                    'repair_original_attempts=CASE WHEN ?=\'original\' '
                    'THEN 1 ELSE repair_original_attempts END,'
                    'repair_remux_attempts=CASE WHEN ?=\'remux\' '
                    'THEN 1 ELSE repair_remux_attempts END,'
                    'repair_diagnostic=? WHERE id=? AND job_id=?',
                    (mode, mode, mode, diagnostic[:500], part_id, claim.id),
                )
                modes[part_id] = mode
            return modes

        return await self._database.write(select)

    async def _prepare_remux_artifacts(
        self,
        claim: LeaseClaim,
        failed: List[_RemotePart],
        repair_modes: Mapping[int, str],
    ) -> Tuple[int, ...]:
        remux_ids = tuple(
            part.local_id for part in failed if repair_modes[part.local_id] == 'remux'
        )
        if not remux_ids:
            return ()
        placeholders = ','.join('?' for _ in remux_ids)
        rows = await self._database.fetchall(
            'SELECT id,source_path,final_path,file_identity,repair_temp_path,'
            'repair_original_path,repair_original_identity FROM upload_parts '
            'WHERE job_id=? AND id IN ({})'.format(placeholders),
            (claim.id, *remux_ids),
        )
        by_id = {int(row['id']): row for row in rows}
        prepared: List[int] = []
        try:
            for part_id in remux_ids:
                row = by_id.get(part_id)
                if row is None:
                    raise ProtocolContractError('待重新封装的分 P 不存在')
                old_temp_path = self._text(row['repair_temp_path'])
                if old_temp_path:
                    await self._remove_remux_path(old_temp_path)
                    await self._assert_active_repair(claim)
                original_path = str(
                    row['repair_original_path']
                    or row['final_path']
                    or row['source_path']
                )
                original_identity = self._text(
                    row['repair_original_identity'] or row['file_identity']
                )
                if original_identity is None:
                    raise ProtocolContractError('待重新封装的分 P 缺少文件身份记录')
                loop = asyncio.get_running_loop()
                remux_future = loop.run_in_executor(
                    None,
                    lambda path=original_path, current_id=part_id: (
                        self._remuxer.remux(path, part_id=current_id)
                    ),
                )
                try:
                    artifact = await asyncio.shield(remux_future)
                except asyncio.CancelledError:
                    artifact = await remux_future

                def store(connection: sqlite3.Connection) -> None:
                    self._require_claim(connection, claim)
                    connection.execute(
                        'UPDATE upload_parts SET repair_temp_path=?,'
                        'repair_original_path=?,repair_original_identity=?,'
                        'final_path=?,file_identity=?,repair_diagnostic=? '
                        'WHERE id=? AND job_id=?',
                        (
                            artifact.path,
                            original_path,
                            original_identity,
                            artifact.path,
                            artifact.identity.to_json(),
                            artifact.diagnostic[:500],
                            part_id,
                            claim.id,
                        ),
                    )

                try:
                    await self._database.write(store)
                except BaseException:
                    await self._remove_remux_path(artifact.path)
                    raise
                prepared.append(part_id)
            return tuple(prepared)
        except BaseException:
            if prepared:
                await self._restore_remux_paths(claim, tuple(prepared))
            raise

    async def _restore_remux_paths(
        self, claim: LeaseClaim, part_ids: Tuple[int, ...]
    ) -> None:
        if not part_ids:
            return

        def load(connection: sqlite3.Connection) -> List[sqlite3.Row]:
            placeholders = ','.join('?' for _ in part_ids)
            return connection.execute(
                'SELECT id,repair_temp_path,repair_original_path,'
                'repair_original_identity FROM upload_parts WHERE job_id=? '
                'AND id IN ({})'.format(placeholders),
                (claim.id, *part_ids),
            ).fetchall()

        rows = await self._database.read(load)
        temporary_paths = tuple(
            str(row['repair_temp_path']) for row in rows if row['repair_temp_path']
        )
        for path in temporary_paths:
            await self._remove_remux_path(path)

        def restore(connection: sqlite3.Connection) -> None:
            if not self._repair_owner_active(connection, claim):
                return
            for row in rows:
                original_path = self._text(row['repair_original_path'])
                original_identity = self._text(row['repair_original_identity'])
                if original_path is None or original_identity is None:
                    continue
                connection.execute(
                    'UPDATE upload_parts SET final_path=?,file_identity=?,'
                    'repair_temp_path=NULL,repair_original_path=NULL,'
                    'repair_original_identity=NULL WHERE id=? AND job_id=?',
                    (original_path, original_identity, int(row['id']), claim.id),
                )

        await self._database.write(restore)

    async def _remove_remux_path(self, path: str) -> None:
        removal = asyncio.get_running_loop().run_in_executor(
            None, self._remuxer.remove, path
        )
        try:
            await asyncio.shield(removal)
        except asyncio.CancelledError:
            await removal

    async def _verify_local_files(
        self, claim: LeaseClaim, failed: List[_RemotePart]
    ) -> None:
        ids = tuple(part.local_id for part in failed)
        placeholders = ','.join('?' for _ in ids)
        rows = await self._database.fetchall(
            'SELECT id,source_path,final_path,file_identity,artifact_state,'
            'repair_original_path,repair_original_identity '
            'FROM upload_parts WHERE job_id=? AND id IN ({})'.format(placeholders),
            (claim.id, *ids),
        )
        by_id = {int(row['id']): row for row in rows}
        for part in failed:
            row = by_id.get(part.local_id)
            if row is None or str(row['artifact_state']) != 'ready':
                raise ProtocolContractError('异常分 P 的本地视频不可用')
            path = str(
                row['repair_original_path'] or row['final_path'] or row['source_path']
            )
            if not os.path.isfile(path):
                raise ProtocolContractError(
                    'P{} 的本地视频已删除，无法重传'.format(part.part_index)
                )
            stored = self._text(row['repair_original_identity'] or row['file_identity'])
            if stored is None:
                raise ProtocolContractError('异常分 P 缺少文件身份记录')
            try:
                expected = FileIdentity.from_json(stored)
                loop = asyncio.get_running_loop()
                current = await loop.run_in_executor(None, FileIdentity.from_path, path)
            except (OSError, ValueError):
                raise ProtocolContractError('异常分 P 的本地视频无法校验') from None
            if current != expected:
                raise ProtocolContractError('异常分 P 的本地视频已经发生变化')

    async def _prepare_failed_parts(
        self,
        claim: LeaseClaim,
        failed: List[_RemotePart],
        repair_modes: Mapping[int, str],
    ) -> None:
        now = int(self._clock())

        def prepare(connection: sqlite3.Connection) -> None:
            self._require_claim(connection, claim)
            for part in failed:
                connection.execute(
                    'DELETE FROM upload_chunks WHERE part_id=?', (part.local_id,)
                )
                connection.execute(
                    "UPDATE upload_parts SET upload_state='prepared',"
                    'remote_filename=NULL,cid=NULL,upload_session_json=NULL '
                    'WHERE id=? AND job_id=?',
                    (part.local_id, claim.id),
                )
            connection.execute(
                "UPDATE upload_jobs SET repair_state='reuploading',"
                'repair_message=?,updated_at=? WHERE id=?',
                (self._repair_progress_message(failed, repair_modes), now, claim.id),
            )

        await self._database.write(prepare)

    async def _finish_noop(self, claim: LeaseClaim, message: str) -> None:
        await self._finish(
            claim,
            {
                'repair_state': 'not_needed',
                'repair_message': message,
                'repair_error': None,
                'repair_completed_at': int(self._clock()),
            },
        )
        audit(
            'transcode_repair_not_needed',
            job_id=claim.id,
            message=message,
            result='not_needed',
        )

    async def _finish_repair(
        self,
        claim: LeaseClaim,
        failed: List[_RemotePart],
        healthy_cids: Mapping[int, int],
        repair_modes: Mapping[int, str],
    ) -> None:
        now = int(self._clock())

        def finish(connection: sqlite3.Connection) -> bool:
            if not self._repair_owner_active(connection, claim):
                self._ack_repair_intent(
                    connection,
                    claim,
                    side_effect_key='archive_edit',
                    outcome_state='confirmed_success',
                    now=now,
                )
                self._release_deleted_claim(connection, claim, now=now)
                return False
            for part_id, cid in healthy_cids.items():
                connection.execute(
                    "UPDATE upload_parts SET cid=?,transcode_state='ready' "
                    'WHERE id=? AND job_id=?',
                    (cid, part_id, claim.id),
                )
            for part in failed:
                connection.execute(
                    "UPDATE upload_parts SET cid=NULL,transcode_state='processing',"
                    'transcode_fail_code=NULL,transcode_fail_desc=NULL,'
                    'repair_stage=? '
                    'WHERE id=? AND job_id=?',
                    (
                        '{}_waiting_review'.format(repair_modes[part.local_id]),
                        part.local_id,
                        claim.id,
                    ),
                )
            remux_count = sum(
                1 for part in failed if repair_modes[part.local_id] == 'remux'
            )
            if remux_count:
                message = '已重新封装并重传 {} 个异常分 P，等待 B 站重新审核'.format(
                    remux_count
                )
            else:
                message = '已重传 {} 个异常分 P，等待 B 站重新审核'.format(len(failed))
            cursor = connection.execute(
                "UPDATE upload_jobs SET state='waiting_review',"
                "repair_state='waiting_review',repair_message=?,repair_error=NULL,"
                'repair_completed_at=?,review_reason=?,approved_at=NULL,'
                'lease_owner=NULL,lease_until=NULL,updated_at=? WHERE id=? '
                'AND lease_owner=? AND lease_generation=?',
                (
                    message,
                    now,
                    message,
                    now,
                    claim.id,
                    claim.lease_owner,
                    claim.lease_generation,
                ),
            )
            if cursor.rowcount != 1:
                raise LeaseLost('转码修复任务租约已失效')
            connection.execute(
                "DELETE FROM owner_handoff_outcomes WHERE owner_kind='repair' "
                'AND owner_id=? AND source_generation=?',
                (claim.id, self._repair_source_generation(claim)),
            )
            return True

        active = await self._database.write(finish)
        audit(
            'transcode_repair_submitted',
            job_id=claim.id,
            failed_parts=len(failed),
            remux_parts=sum(
                1 for part in failed if repair_modes[part.local_id] == 'remux'
            ),
            result='waiting_review' if active else 'deleted_local_only',
        )

    @staticmethod
    def _repair_progress_message(
        failed: List[_RemotePart], repair_modes: Mapping[int, str]
    ) -> str:
        remux_count = sum(
            1 for part in failed if repair_modes[part.local_id] == 'remux'
        )
        original_count = len(failed) - remux_count
        messages = []
        if original_count:
            messages.append('重传 {} 个原文件'.format(original_count))
        if remux_count:
            messages.append('重传 {} 个重新封装文件'.format(remux_count))
        return '正在{}'.format('并'.join(messages))

    async def _fail_repair(self, claim: LeaseClaim, reason: str) -> None:
        await self._finish(
            claim,
            {
                'repair_state': 'failed',
                'repair_message': None,
                'repair_error': (reason or '转码修复失败')[:500],
                'repair_completed_at': int(self._clock()),
            },
        )
        audit(
            'transcode_repair_failed',
            level='ERROR',
            job_id=claim.id,
            reason=(reason or '转码修复失败')[:500],
            result='failed',
        )

    async def _unknown_repair(self, claim: LeaseClaim) -> None:
        now = int(self._clock())

        def unknown(connection: sqlite3.Connection) -> bool:
            active = self._repair_owner_active(connection, claim)
            self._ack_repair_intent(
                connection,
                claim,
                side_effect_key='archive_edit',
                outcome_state='unknown_terminal',
                now=now,
            )
            if not active:
                self._release_deleted_claim(connection, claim, now=now)
                return False
            self._ack_repair_intent(
                connection,
                claim,
                side_effect_key=self._repair_lease_key(claim),
                outcome_state='unknown_terminal',
                now=now,
            )
            cursor = connection.execute(
                "UPDATE upload_jobs SET state='paused',"
                "repair_state='unknown_outcome',repair_message=NULL,"
                'repair_error=?,review_reason=?,repair_completed_at=?,'
                'lease_owner=NULL,lease_until=NULL,updated_at=? WHERE id=? '
                'AND lease_owner=? AND lease_generation=?',
                (
                    '稿件编辑结果未知，请先到创作中心核对',
                    '转码修复的稿件编辑结果未知，需要远端核对',
                    now,
                    now,
                    claim.id,
                    claim.lease_owner,
                    claim.lease_generation,
                ),
            )
            if cursor.rowcount != 1:
                raise LeaseLost('转码修复任务租约已失效')
            return True

        active = await self._database.write(unknown)
        audit(
            'transcode_repair_unknown',
            level='WARNING',
            job_id=claim.id,
            result='unknown_outcome' if active else 'unknown_terminal',
        )

    async def _finish(self, claim: LeaseClaim, values: Mapping[str, Any]) -> None:
        allowed = {
            'state',
            'repair_state',
            'repair_message',
            'repair_error',
            'review_reason',
            'repair_completed_at',
        }
        if not values or not set(values) <= allowed:
            raise ValueError('invalid repair update')
        now = int(self._clock())

        def finish(connection: sqlite3.Connection) -> bool:
            if not self._repair_owner_active(connection, claim):
                self._release_deleted_claim(connection, claim, now=now)
                return False
            assignments = ['{}=?'.format(column) for column in values]
            assignments.extend(('lease_owner=NULL', 'lease_until=NULL', 'updated_at=?'))
            parameters: List[Any] = list(values.values())
            parameters.append(now)
            parameters.extend((claim.id, claim.lease_owner, claim.lease_generation))
            cursor = connection.execute(
                'UPDATE upload_jobs SET {} WHERE id=? AND lease_owner=? '
                'AND lease_generation=? AND operator_paused=0'.format(
                    ','.join(assignments)
                ),
                parameters,
            )
            if cursor.rowcount != 1:
                raise LeaseLost('转码修复任务租约已失效')
            connection.execute(
                "DELETE FROM owner_handoff_outcomes WHERE owner_kind='repair' "
                'AND owner_id=? AND side_effect_key=? AND source_generation=?',
                (
                    claim.id,
                    self._repair_lease_key(claim),
                    self._repair_source_generation(claim),
                ),
            )
            return True

        await self._database.write(finish)

    @staticmethod
    def _require_claim(connection: sqlite3.Connection, claim: LeaseClaim) -> None:
        row = connection.execute(
            'SELECT 1 FROM upload_jobs job '
            'JOIN recording_sessions session ON session.id=job.session_id '
            'WHERE job.id=? AND job.lease_owner=? '
            'AND job.lease_generation=? AND job.operator_paused=0 '
            "AND session.deletion_state='none' "
            'AND session.cancellation_generation=?',
            (
                claim.id,
                claim.lease_owner,
                claim.lease_generation,
                UploadTaskActionManager._repair_source_generation(claim),
            ),
        ).fetchone()
        if row is None:
            raise LeaseLost('转码修复任务租约已失效')

    async def _assert_active_repair(self, claim: LeaseClaim) -> None:
        now = int(self._clock())
        row = await self._database.fetchone(
            'SELECT 1 FROM upload_jobs job '
            'JOIN recording_sessions session ON session.id=job.session_id '
            'WHERE job.id=? AND job.lease_owner=? '
            'AND job.lease_generation=? AND job.lease_until>? '
            "AND job.repair_state IN ('checking','reuploading','editing') "
            "AND job.operator_paused=0 AND session.deletion_state='none' "
            'AND session.cancellation_generation=?',
            (
                claim.id,
                claim.lease_owner,
                claim.lease_generation,
                now,
                self._repair_source_generation(claim),
            ),
        )
        if row is None:
            raise LeaseLost('转码修复任务租约已失效')

    @staticmethod
    def _transcode_state(fail_code: int, xcode_state: int, fail_desc: str) -> str:
        if fail_code == 0 and xcode_state == 2:
            return 'processing'
        if (fail_code, xcode_state) in ((9, 3), (14, 1)):
            return 'failed'
        if fail_code != 0:
            raise ProtocolContractError(
                '发现未识别的转码失败状态（failCode={}, xcodeState={}{}）'.format(
                    fail_code,
                    xcode_state,
                    '，{}'.format(fail_desc) if fail_desc else '',
                )
            )
        return 'ready'

    @staticmethod
    def _cover_url(value: Any) -> Optional[str]:
        if not isinstance(value, str) or not value:
            return None
        if value.startswith('//'):
            return 'https:' + value
        return value if value.startswith('https://') else None

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
    def _integer(value: Any, *, default: int) -> int:
        if value is None:
            return default
        if type(value) is int:
            return value
        if isinstance(value, str) and value.lstrip('-').isdigit():
            return int(value)
        raise ProtocolContractError('远端分 P 转码状态不是整数')

    @staticmethod
    def _has_active_lease(row: sqlite3.Row, now: int) -> bool:
        return row['lease_until'] is not None and int(row['lease_until']) > now

    @staticmethod
    def _delete_job_children(connection: sqlite3.Connection, job_id: int) -> None:
        connection.execute(
            'DELETE FROM danmaku_items WHERE part_id IN('
            'SELECT id FROM upload_parts WHERE job_id=?)',
            (job_id,),
        )
        connection.execute(
            'DELETE FROM upload_chunks WHERE part_id IN('
            'SELECT id FROM upload_parts WHERE job_id=?)',
            (job_id,),
        )
        connection.execute('DELETE FROM comment_items WHERE job_id=?', (job_id,))
        connection.execute('DELETE FROM upload_parts WHERE job_id=?', (job_id,))

    @staticmethod
    def _validate_task_snapshot(snapshot: Mapping[str, Any], part_count: int) -> None:
        title = snapshot.get('title')
        description = snapshot.get('description')
        dynamic = snapshot.get('dynamic')
        tags = snapshot.get('tags')
        source = snapshot.get('source')
        tid = snapshot.get('tid')
        if not isinstance(title, str) or not title.strip() or len(title) > 80:
            raise UploadTaskActionRejected('标题需为 1 到 80 个字符')
        if not isinstance(description, str) or len(description) > 2000:
            raise UploadTaskActionRejected('简介不能超过 2000 个字符')
        if not isinstance(dynamic, str) or len(dynamic) > 1000:
            raise UploadTaskActionRejected('动态文案不能超过 1000 个字符')
        if not isinstance(tags, str) or not tags.strip():
            raise UploadTaskActionRejected('标签不能为空')
        if type(tid) is not int or tid <= 0:
            raise UploadTaskActionRejected('投稿分区无效')
        statement = snapshot.get('creation_statement_id')
        authorization = snapshot.get('original_authorization')
        copyright_value = snapshot.get('copyright')
        no_reprint = snapshot.get('no_reprint')
        if type(statement) is not int or type(authorization) is not bool:
            raise UploadTaskActionRejected('创作声明无效')
        expected_copyright = 2 if statement == -2 else 1 if authorization else 3
        if copyright_value != expected_copyright:
            raise UploadTaskActionRejected('创作声明与稿件类型不一致')
        if statement == -2:
            if not isinstance(source, str) or not source.strip():
                raise UploadTaskActionRejected('转载稿件必须填写来源')
            if no_reprint is not False:
                raise UploadTaskActionRejected('转载稿件不能设置禁止转载')
        elif authorization and no_reprint is not True:
            raise UploadTaskActionRejected('原创授权稿件必须设置禁止转载')
        elif not authorization and no_reprint is not False:
            raise UploadTaskActionRejected('当前声明不能设置禁止转载')
        boolean_fields = (
            'is_only_self',
            'publish_dynamic',
            'up_selection_reply',
            'up_close_reply',
            'up_close_danmu',
            'auto_comment',
            'danmaku_backfill',
        )
        if any(type(snapshot.get(field)) is not bool for field in boolean_fields):
            raise UploadTaskActionRejected('投稿开关设置无效')
        collection_season = snapshot.get('collection_season_id')
        collection_section = snapshot.get('collection_section_id')
        if (collection_season is None) != (collection_section is None) or (
            collection_season is not None
            and (
                type(collection_season) is not int
                or collection_season <= 0
                or type(collection_section) is not int
                or collection_section <= 0
            )
        ):
            raise UploadTaskActionRejected('合集设置无效')
        cover_mode = snapshot.get('cover_mode')
        cover_asset_id = snapshot.get('cover_asset_id')
        if (cover_mode == 'live' and cover_asset_id is not None) or (
            cover_mode == 'custom'
            and (type(cover_asset_id) is not int or cover_asset_id <= 0)
        ):
            raise UploadTaskActionRejected('封面设置无效')
        if cover_mode not in ('live', 'custom'):
            raise UploadTaskActionRejected('封面设置无效')
        delay = snapshot.get('publish_delay_seconds')
        if type(delay) is not int or (
            delay != 0 and not 7200 <= delay <= 15 * 24 * 60 * 60
        ):
            raise UploadTaskActionRejected('定时发布设置无效')
        filters = snapshot.get('filters')
        if not isinstance(filters, (dict, list)):
            raise UploadTaskActionRejected('弹幕过滤设置无效')
        part_titles = snapshot.get('part_titles')
        if (
            not isinstance(part_titles, list)
            or len(part_titles) != part_count
            or any(
                not isinstance(value, str) or not value.strip() or len(value) > 80
                for value in part_titles
            )
        ):
            raise UploadTaskActionRejected('分 P 标题设置无效')

    @staticmethod
    def _audit_session(
        connection: sqlite3.Connection,
        *,
        manager_subject: str,
        action: str,
        session_id: int,
        old_state: str,
        new_state: str,
        reason: str,
        now: int,
    ) -> None:
        connection.execute(
            'INSERT INTO management_audit('
            'manager_subject,action,target_type,target_id,old_state,new_state,'
            'reason,created_at) VALUES(?,?,?,?,?,?,?,?)',
            (
                manager_subject,
                action,
                'recording_session',
                str(session_id),
                old_state,
                new_state,
                reason,
                now,
            ),
        )

    @staticmethod
    def _text(value: Any) -> Optional[str]:
        return value if isinstance(value, str) and value else None

    @staticmethod
    def _audit(
        connection: sqlite3.Connection,
        *,
        manager_subject: str,
        action: str,
        job_id: int,
        old_state: str,
        new_state: str,
        reason: str,
        now: int,
    ) -> None:
        connection.execute(
            'INSERT INTO management_audit('
            'manager_subject,action,target_type,target_id,old_state,new_state,'
            'reason,created_at) VALUES(?,?,?,?,?,?,?,?)',
            (
                manager_subject,
                action,
                'upload_job',
                str(job_id),
                old_state,
                new_state,
                reason,
                now,
            ),
        )
