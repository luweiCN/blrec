from __future__ import annotations

import asyncio
import os
import sqlite3
import time
import uuid
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, List, Mapping, Optional, Tuple

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
from .upos import FileIdentity, UposUploader, UposUploadPaused, UposUploadStopped

__all__ = ('UploadTaskActionManager', 'UploadTaskActionRejected')


class UploadTaskActionRejected(ValueError):
    pass


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
        worker_id: Optional[str] = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._database = database
        self._protocol = protocol
        self._uploader = uploader
        self._bundle_loader = bundle_loader
        self._account_gates = account_gates
        self._edit_payload_builder = edit_payload_builder
        self._worker_id = worker_id or 'repair-{}'.format(uuid.uuid4().hex)
        self._clock = clock
        self._run_lock = asyncio.Lock()

    async def retry_failed(self, job_id: int, *, manager_subject: str) -> str:
        if not manager_subject:
            raise UploadTaskActionRejected('管理员身份不能为空')
        now = int(self._clock())

        def retry(connection: sqlite3.Connection) -> str:
            job = connection.execute(
                'SELECT job.state,job.submit_state,job.aid,job.bvid,'
                'job.repair_state,job.lease_until,account.state AS account_state '
                'FROM upload_jobs job JOIN bili_accounts account '
                'ON account.id=job.account_id WHERE job.id=?',
                (job_id,),
            ).fetchone()
            if job is None:
                raise UploadTaskActionRejected('上传任务不存在')
            if str(job['state']) != 'paused':
                raise UploadTaskActionRejected('只有已暂停的任务可以重新排队')
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

    async def recover_interrupted(self) -> None:
        now = int(self._clock())

        def recover(connection: sqlite3.Connection) -> None:
            connection.execute(
                "UPDATE upload_jobs SET repair_state='queued',repair_message=?,"
                'repair_error=NULL,lease_owner=NULL,lease_until=NULL,updated_at=? '
                "WHERE repair_state IN ('checking','reuploading')",
                ('上次修复中断，已重新排队', now),
            )
            connection.execute(
                "UPDATE upload_jobs SET state='paused',"
                "repair_state='unknown_outcome',repair_message=NULL,repair_error=?,"
                'review_reason=?,lease_owner=NULL,lease_until=NULL,updated_at=? '
                "WHERE repair_state='editing'",
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
                'SELECT id,repair_attempt FROM upload_jobs '
                "WHERE repair_state='queued' "
                'AND (lease_until IS NULL OR lease_until<=?) '
                'ORDER BY repair_requested_at,id LIMIT 1',
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
                'AND (lease_until IS NULL OR lease_until<=?)',
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
                'SELECT lease_generation,repair_attempt FROM upload_jobs WHERE id=?',
                (job_id,),
            ).fetchone()
            assert claimed is not None
            return LeaseClaim(
                table='upload_jobs',
                id=job_id,
                lease_owner=self._worker_id,
                lease_generation=int(claimed['lease_generation']),
                lease_until=lease_until,
                attempt=int(claimed['repair_attempt']),
            )

        return await self._database.write(claim)

    async def _process_repair(self, claim: LeaseClaim) -> None:
        try:
            job = await self._load_repair_job(claim)
            gate = self._account_gates.for_account(job.account_id)
            async with gate.hold(job.credential_version):
                bundle = await self._bundle_loader(job.account_id)
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

                await self._verify_local_files(claim, failed)
                await self._prepare_failed_parts(claim, failed)
                for part in failed:
                    await self._uploader.upload_part(
                        part.local_id, bundle=bundle, claim=claim
                    )
                healthy_cids = {
                    part.local_id: part.cid
                    for part in remote_parts
                    if part.state != 'failed'
                }
                payload = await self._edit_payload_builder(
                    job.id, healthy_cids, cover_url
                )
                await self._set_repair_stage(
                    claim, 'editing', '正在更新原稿件的异常分 P'
                )
                await self._protocol.edit_archive(bundle, payload)
                await self._finish_repair(claim, failed, healthy_cids)
        except DefinitelyNotSent:
            await self._fail_repair(claim, '稿件编辑请求未发出，可以重新尝试')
        except RemoteOutcomeUnknown:
            await self._unknown_repair(claim)
        except (AccountNotFound, AccountPaused, CredentialVersionChanged):
            await self._fail_repair(claim, '投稿账号在修复期间发生变化')
        except (CredentialNotFound, InvalidCredentialBundle, InvalidCredentialKey):
            await self._fail_repair(claim, '投稿账号凭据无法读取')
        except UposUploadStopped:
            await self._fail_repair(claim, '转码修复已停止，可以重新尝试')
        except UposUploadPaused as error:
            await self._fail_repair(claim, str(error))
        except BiliApiError as error:
            await self._fail_repair(
                claim, 'B 站接口拒绝转码修复请求（{}）'.format(error.code)
            )
        except LeaseLost:
            return
        except (ProtocolContractError, OSError, ValueError) as error:
            await self._fail_repair(claim, str(error))
        except Exception as error:
            await self._fail_repair(claim, str(error) or '转码修复失败')

    async def _load_repair_job(self, claim: LeaseClaim) -> _RepairJob:
        now = int(self._clock())
        row = await self._database.fetchone(
            'SELECT job.id,job.account_id,job.aid,job.bvid,'
            'account.credential_version FROM upload_jobs job '
            'JOIN bili_accounts account ON account.id=job.account_id '
            'WHERE job.id=? AND job.lease_owner=? AND job.lease_generation=? '
            "AND job.lease_until>? AND job.repair_state='checking'",
            (claim.id, claim.lease_owner, claim.lease_generation, now),
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
        for row in local_rows:
            filename = self._text(row['remote_filename'])
            if filename is None or filename in local_by_filename:
                raise ProtocolContractError('本地分 P 的远端 filename 不完整或重复')
            final_path = self._text(row['final_path'])
            local_by_filename[filename] = _LocalPart(
                id=int(row['id']),
                part_index=int(row['part_index']),
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
            if page != local.part_index or cid is None:
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

    async def _verify_local_files(
        self, claim: LeaseClaim, failed: List[_RemotePart]
    ) -> None:
        ids = tuple(part.local_id for part in failed)
        placeholders = ','.join('?' for _ in ids)
        rows = await self._database.fetchall(
            'SELECT id,source_path,final_path,file_identity,artifact_state '
            'FROM upload_parts WHERE job_id=? AND id IN ({})'.format(placeholders),
            (claim.id, *ids),
        )
        by_id = {int(row['id']): row for row in rows}
        for part in failed:
            row = by_id.get(part.local_id)
            if row is None or str(row['artifact_state']) != 'ready':
                raise ProtocolContractError('异常分 P 的本地视频不可用')
            path = str(row['final_path'] or row['source_path'])
            if not os.path.isfile(path):
                raise ProtocolContractError(
                    'P{} 的本地视频已删除，无法重传'.format(part.part_index)
                )
            stored = self._text(row['file_identity'])
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
        self, claim: LeaseClaim, failed: List[_RemotePart]
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
                ('正在重传 {} 个异常分 P'.format(len(failed)), now, claim.id),
            )

        await self._database.write(prepare)

    async def _set_repair_stage(
        self, claim: LeaseClaim, state: str, message: str
    ) -> None:
        updated = await self._database.execute(
            'UPDATE upload_jobs SET repair_state=?,repair_message=?,updated_at=? '
            'WHERE id=? AND lease_owner=? AND lease_generation=?',
            (
                state,
                message,
                int(self._clock()),
                claim.id,
                claim.lease_owner,
                claim.lease_generation,
            ),
        )
        if updated != 1:
            raise LeaseLost('转码修复任务租约已失效')

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

    async def _finish_repair(
        self,
        claim: LeaseClaim,
        failed: List[_RemotePart],
        healthy_cids: Mapping[int, int],
    ) -> None:
        now = int(self._clock())

        def finish(connection: sqlite3.Connection) -> None:
            self._require_claim(connection, claim)
            for part_id, cid in healthy_cids.items():
                connection.execute(
                    "UPDATE upload_parts SET cid=?,transcode_state='ready' "
                    'WHERE id=? AND job_id=?',
                    (cid, part_id, claim.id),
                )
            for part in failed:
                connection.execute(
                    "UPDATE upload_parts SET cid=NULL,transcode_state='processing',"
                    'transcode_fail_code=NULL,transcode_fail_desc=NULL '
                    'WHERE id=? AND job_id=?',
                    (part.local_id, claim.id),
                )
            message = '已重传 {} 个异常分 P，等待 B 站重新审核'.format(len(failed))
            connection.execute(
                "UPDATE upload_jobs SET state='waiting_review',"
                "repair_state='waiting_review',repair_message=?,repair_error=NULL,"
                'repair_completed_at=?,review_reason=?,approved_at=NULL,'
                'lease_owner=NULL,lease_until=NULL,updated_at=? WHERE id=?',
                (message, now, message, now, claim.id),
            )

        await self._database.write(finish)

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

    async def _unknown_repair(self, claim: LeaseClaim) -> None:
        await self._finish(
            claim,
            {
                'state': 'paused',
                'repair_state': 'unknown_outcome',
                'repair_message': None,
                'repair_error': '稿件编辑结果未知，请先到创作中心核对',
                'review_reason': '转码修复的稿件编辑结果未知，需要远端核对',
                'repair_completed_at': int(self._clock()),
            },
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
        assignments = ['{}=?'.format(column) for column in values]
        assignments.extend(('lease_owner=NULL', 'lease_until=NULL', 'updated_at=?'))
        parameters: List[Any] = list(values.values())
        parameters.append(int(self._clock()))
        parameters.extend((claim.id, claim.lease_owner, claim.lease_generation))
        updated = await self._database.execute(
            'UPDATE upload_jobs SET {} WHERE id=? AND lease_owner=? '
            'AND lease_generation=?'.format(','.join(assignments)),
            parameters,
        )
        if updated != 1:
            state = await self._database.scalar(
                'SELECT repair_state FROM upload_jobs WHERE id=?', (claim.id,)
            )
            if state not in ('failed', 'unknown_outcome'):
                raise LeaseLost('转码修复任务租约已失效')

    @staticmethod
    def _require_claim(connection: sqlite3.Connection, claim: LeaseClaim) -> None:
        row = connection.execute(
            'SELECT 1 FROM upload_jobs WHERE id=? AND lease_owner=? '
            'AND lease_generation=?',
            (claim.id, claim.lease_owner, claim.lease_generation),
        ).fetchone()
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
