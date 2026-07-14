from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import time
import uuid
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, List, Mapping, Optional, Tuple, Union

from liquid import Environment

from .accounts import (
    AccountNotFound,
    AccountPaused,
    AccountWriteGate,
    CredentialVersionChanged,
)
from .covers import (
    CoverAssetNotFound,
    CoverResolutionError,
    CoverResolver,
    InvalidCover,
    StoredCoverUnavailable,
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

__all__ = ('InvalidUploadPolicy', 'UploadCoordinator')

_FeatureSwitch = Union[bool, Callable[[], bool]]


class InvalidUploadPolicy(RuntimeError):
    pass


@dataclass(frozen=True)
class _CandidatePart:
    id: int
    part_index: int
    source_path: str
    final_path: str
    xml_path: Optional[str]
    artifact_state: str
    updated_at: int
    identity: FileIdentity


@dataclass(frozen=True)
class _Job:
    id: int
    account_id: int
    policy_snapshot_json: str
    state: str
    submit_state: str
    upload_completed_at: Optional[int]


class UploadCoordinator:
    def __init__(
        self,
        database: BiliUploadDatabase,
        protocol: Any,
        uploader: UposUploader,
        *,
        bundle_loader: Callable[[int], Awaitable[CredentialBundle]],
        account_gates: AccountWriteGate,
        auto_upload_enabled: _FeatureSwitch,
        auto_comment_enabled: _FeatureSwitch,
        danmaku_backfill_enabled: _FeatureSwitch,
        cover_resolver: CoverResolver,
        worker_id: Optional[str] = None,
        stability_seconds: int = 30,
        clock: Callable[[], float] = time.time,
        stop_requested: Callable[[], bool] = lambda: False,
    ) -> None:
        if stability_seconds < 0:
            raise ValueError('file stability window must not be negative')
        self._database = database
        self._protocol = protocol
        self._uploader = uploader
        self._bundle_loader = bundle_loader
        self._account_gates = account_gates
        self._auto_upload_enabled = auto_upload_enabled
        self._auto_comment_enabled = auto_comment_enabled
        self._danmaku_backfill_enabled = danmaku_backfill_enabled
        self._cover_resolver = cover_resolver
        self._worker_id = worker_id or 'upload-{}'.format(uuid.uuid4().hex)
        self._stability_seconds = stability_seconds
        self._clock = clock
        self._stop_requested = stop_requested
        self._run_lock = asyncio.Lock()
        self._liquid = Environment()

    async def create_ready_jobs(self) -> List[int]:
        candidates = await self._database.fetchall(
            'SELECT session.id AS session_id,session.room_id,'
            'session.broadcast_session_key,session.live_start_time,'
            'session.live_end_time,session.title,session.cover_url,'
            'session.cover_path,session.anchor_uid,session.anchor_name,'
            'session.area_id,session.area_name,session.parent_area_id,'
            'session.parent_area_name,policy.account_mode,policy.account_id,'
            'policy.title_template,policy.description_template,'
            'policy.part_title_template,policy.dynamic_template,policy.tid,'
            'policy.tags,policy.creation_statement_id,'
            'policy.original_authorization,policy.copyright,policy.source,'
            'policy.is_only_self,'
            'policy.publish_dynamic,policy.no_reprint,policy.up_selection_reply,'
            'policy.up_close_reply,policy.up_close_danmu,policy.auto_comment,'
            'policy.danmaku_backfill,policy.filter_json,'
            'policy.collection_season_id,policy.collection_section_id,'
            'policy.cover_mode,policy.cover_asset_id,policy.publish_delay_seconds,'
            'policy.updated_at AS policy_updated_at,'
            'account.id AS resolved_account_id,'
            'account.uid AS resolved_account_uid,'
            'account.credential_version AS credential_version '
            'FROM recording_sessions session '
            'JOIN room_upload_policies policy ON policy.room_id=session.room_id '
            'JOIN bili_accounts account ON account.id=CASE '
            "WHEN policy.account_mode='fixed' THEN policy.account_id "
            'ELSE (SELECT primary_account_id FROM bili_account_selection '
            'WHERE id=1) END '
            "WHERE session.state='closed' AND policy.enabled=1 "
            "AND account.state='active' "
            'AND NOT EXISTS(SELECT 1 FROM upload_jobs job '
            'WHERE job.session_id=session.id) '
            'ORDER BY session.started_at,session.id'
        )
        created = []
        for candidate in candidates:
            job_id = await self._create_candidate(candidate)
            if job_id is not None:
                created.append(job_id)
        return created

    async def run_once(self) -> Optional[int]:
        if not self._enabled(self._auto_upload_enabled) or self._stop_requested():
            return None
        async with self._run_lock:
            claim = await self._database.claim(
                'upload_jobs',
                ('ready', 'uploading', 'submitting'),
                self._worker_id,
                now=int(self._clock()),
            )
            if claim is None:
                return None
            await self._process(claim)
            return claim.id

    async def build_edit_payload(
        self, job_id: int, healthy_cids: Mapping[int, int], cover_url: Optional[str]
    ) -> Mapping[str, Any]:
        row = await self._database.fetchone(
            'SELECT id,account_id,policy_snapshot_json,state,submit_state,'
            'upload_completed_at,aid FROM upload_jobs WHERE id=?',
            (job_id,),
        )
        if row is None:
            raise ProtocolContractError('upload job does not exist')
        aid = row['aid']
        if type(aid) is not int or aid <= 0:
            raise ProtocolContractError('upload job has no confirmed AID')
        job = _Job(
            id=int(row['id']),
            account_id=int(row['account_id']),
            policy_snapshot_json=str(row['policy_snapshot_json']),
            state=str(row['state']),
            submit_state=str(row['submit_state']),
            upload_completed_at=(
                None
                if row['upload_completed_at'] is None
                else int(row['upload_completed_at'])
            ),
        )
        if cover_url is not None and (
            not isinstance(cover_url, str) or not cover_url.startswith('https://')
        ):
            raise ProtocolContractError('invalid archive cover URL')
        payload = dict(await self._submit_payload(job, cover_override=cover_url))
        payload.pop('dtime', None)
        payload['aid'] = aid
        payload['recreate'] = -1
        payload['topic_grey'] = 1
        payload['web_os'] = 1

        parts = await self._database.fetchall(
            'SELECT id FROM upload_parts WHERE job_id=? ORDER BY part_index', (job_id,)
        )
        videos = payload.get('videos')
        if not isinstance(videos, list) or len(videos) != len(parts):
            raise ProtocolContractError('upload parts are incomplete')
        part_ids = {int(part['id']) for part in parts}
        if not set(healthy_cids) <= part_ids or any(
            type(cid) is not int or cid <= 0 for cid in healthy_cids.values()
        ):
            raise ProtocolContractError('healthy part CID mapping is invalid')
        for part, video in zip(parts, videos):
            if not isinstance(video, dict):
                raise ProtocolContractError('upload part payload is invalid')
            cid = healthy_cids.get(int(part['id']))
            if cid is not None:
                video['cid'] = cid
        return payload

    async def _create_candidate(self, row: sqlite3.Row) -> Optional[int]:
        if bool(row['auto_comment']) and not self._enabled(self._auto_comment_enabled):
            return None
        if bool(row['danmaku_backfill']) and not self._enabled(
            self._danmaku_backfill_enabled
        ):
            return None
        part_rows = await self._database.fetchall(
            'SELECT id,part_index,source_path,final_path,xml_path,'
            'artifact_state,updated_at FROM recording_parts '
            "WHERE session_id=? AND artifact_state='ready' ORDER BY part_index",
            (int(row['session_id']),),
        )
        if not part_rows or any(
            str(part['artifact_state']) != 'ready' or not part['final_path']
            for part in part_rows
        ):
            return None
        parts = []
        for part in part_rows:
            final_path = str(part['final_path'])
            try:
                identity = await self._file_identity(final_path)
            except OSError:
                return None
            stable_before_ns = int(
                (self._clock() - self._stability_seconds) * 1_000_000_000
            )
            if identity.mtime_ns > stable_before_ns:
                return None
            parts.append(
                _CandidatePart(
                    id=int(part['id']),
                    part_index=int(part['part_index']),
                    source_path=str(part['source_path']),
                    final_path=final_path,
                    xml_path=(
                        None if part['xml_path'] is None else str(part['xml_path'])
                    ),
                    artifact_state=str(part['artifact_state']),
                    updated_at=int(part['updated_at']),
                    identity=identity,
                )
            )
        try:
            snapshot = self._policy_snapshot(row, parts)
        except InvalidUploadPolicy:
            return None
        snapshot_json = json.dumps(
            snapshot, ensure_ascii=False, separators=(',', ':'), sort_keys=True
        )
        now = int(self._clock())

        def create(connection: sqlite3.Connection) -> Optional[int]:
            existing = connection.execute(
                'SELECT id FROM upload_jobs WHERE session_id=?',
                (int(row['session_id']),),
            ).fetchone()
            if existing is not None:
                return None
            session = connection.execute(
                'SELECT state FROM recording_sessions WHERE id=?',
                (int(row['session_id']),),
            ).fetchone()
            if session is None or str(session['state']) != 'closed':
                return None
            policy = connection.execute(
                'SELECT enabled,account_mode,account_id,updated_at '
                'FROM room_upload_policies WHERE room_id=?',
                (int(row['room_id']),),
            ).fetchone()
            if (
                policy is None
                or int(policy['enabled']) != 1
                or int(policy['updated_at']) != int(row['policy_updated_at'])
            ):
                return None
            resolved_account_id = int(row['resolved_account_id'])
            if str(policy['account_mode']) == 'fixed':
                current_account_id = policy['account_id']
            else:
                selected = connection.execute(
                    'SELECT primary_account_id FROM bili_account_selection '
                    'WHERE id=1'
                ).fetchone()
                current_account_id = (
                    None if selected is None else selected['primary_account_id']
                )
            if current_account_id is None or int(current_account_id) != (
                resolved_account_id
            ):
                return None
            account = connection.execute(
                'SELECT state,credential_version FROM bili_accounts WHERE id=?',
                (resolved_account_id,),
            ).fetchone()
            if (
                account is None
                or str(account['state']) != 'active'
                or int(account['credential_version']) != int(row['credential_version'])
            ):
                return None
            current_parts = connection.execute(
                'SELECT id,part_index,source_path,final_path,artifact_state,updated_at '
                "FROM recording_parts WHERE session_id=? AND artifact_state='ready' "
                'ORDER BY part_index',
                (int(row['session_id']),),
            ).fetchall()
            expected_parts = [
                (
                    part.id,
                    part.part_index,
                    part.source_path,
                    part.final_path,
                    part.artifact_state,
                    part.updated_at,
                )
                for part in parts
            ]
            actual_parts = [
                (
                    int(part['id']),
                    int(part['part_index']),
                    str(part['source_path']),
                    str(part['final_path']),
                    str(part['artifact_state']),
                    int(part['updated_at']),
                )
                for part in current_parts
            ]
            if actual_parts != expected_parts:
                return None
            cursor = connection.execute(
                'INSERT INTO upload_jobs('
                'session_id,account_id,policy_snapshot_json,state,submit_state,'
                'comment_branch_state,danmaku_branch_state,'
                'collection_branch_state,created_at,updated_at) '
                'VALUES(?,?,?,\'ready\',\'prepared\',?,?,?,?,?)',
                (
                    int(row['session_id']),
                    resolved_account_id,
                    snapshot_json,
                    'pending' if bool(row['auto_comment']) else 'disabled',
                    'pending' if bool(row['danmaku_backfill']) else 'disabled',
                    (
                        'pending'
                        if row['collection_section_id'] is not None
                        else 'disabled'
                    ),
                    now,
                    now,
                ),
            )
            job_id = int(cursor.lastrowid)
            for part in parts:
                danmaku_state = 'disabled'
                if bool(row['danmaku_backfill']):
                    danmaku_state = 'pending' if part.xml_path else 'missing_source'
                connection.execute(
                    'INSERT INTO upload_parts('
                    'job_id,part_index,source_path,final_path,xml_path,'
                    'file_identity,artifact_state,upload_state,'
                    'danmaku_import_state) '
                    "VALUES(?,?,?,?,?,?,'ready','prepared',?)",
                    (
                        job_id,
                        part.part_index,
                        part.source_path,
                        part.final_path,
                        part.xml_path,
                        part.identity.to_json(),
                        danmaku_state,
                    ),
                )
            return job_id

        return await self._database.write(create)

    def _policy_snapshot(
        self, row: sqlite3.Row, parts: List[_CandidatePart]
    ) -> Dict[str, Any]:
        context = {
            'room_id': int(row['room_id']),
            'title': str(row['title']),
            'anchor_name': str(row['anchor_name']),
            'area_name': str(row['area_name']),
            'parent_area_name': str(row['parent_area_name']),
            'live_start_time': row['live_start_time'],
            'live_end_time': row['live_end_time'],
            'part_count': len(parts),
        }
        try:
            title = self._liquid.from_string(str(row['title_template'])).render(
                **context
            )
            description = self._liquid.from_string(
                str(row['description_template'])
            ).render(**context)
            dynamic = self._liquid.from_string(str(row['dynamic_template'])).render(
                **context
            )
            source = self._liquid.from_string(str(row['source'])).render(**context)
            tags = self._liquid.from_string(str(row['tags'])).render(**context)
            part_template = self._liquid.from_string(str(row['part_title_template']))
            part_titles = [
                part_template.render(**context, part_index=part.part_index).strip()
                for part in parts
            ]
            filters = json.loads(str(row['filter_json']))
        except Exception as error:
            raise InvalidUploadPolicy('upload policy cannot be rendered') from error
        title = title.strip()
        description = description.strip()
        dynamic = dynamic.strip()
        source = source.strip()
        tags = tags.strip()
        creation_statement_id = int(row['creation_statement_id'])
        original_authorization = bool(row['original_authorization'])
        copyright_value = int(row['copyright'])
        if not title or len(title) > 80:
            raise InvalidUploadPolicy('upload title must contain 1 to 80 characters')
        if len(description) > 2000:
            raise InvalidUploadPolicy('upload description is too long')
        if any(not part_title for part_title in part_titles):
            raise InvalidUploadPolicy('upload part titles must not be empty')
        if not tags:
            raise InvalidUploadPolicy('upload tags must not be empty')
        if copyright_value == 2 and not source:
            raise InvalidUploadPolicy('reposted archive requires a source')
        expected_copyright = (
            2 if creation_statement_id == -2 else 1 if original_authorization else 3
        )
        if copyright_value != expected_copyright or (
            creation_statement_id == -2 and original_authorization
        ):
            raise InvalidUploadPolicy('creation statement settings are inconsistent')
        if not isinstance(filters, (dict, list)):
            raise InvalidUploadPolicy('upload filters must be structured JSON')
        collection_season_id = row['collection_season_id']
        collection_section_id = row['collection_section_id']
        if (collection_season_id is None) != (collection_section_id is None):
            raise InvalidUploadPolicy('collection selection is inconsistent')
        if collection_season_id is not None and (
            int(collection_season_id) <= 0 or int(collection_section_id) <= 0
        ):
            raise InvalidUploadPolicy('collection selection is invalid')
        cover_mode = str(row['cover_mode'])
        cover_asset_id = row['cover_asset_id']
        if cover_mode == 'live':
            if cover_asset_id is not None:
                raise InvalidUploadPolicy('live cover selection is invalid')
        elif cover_mode == 'custom':
            if cover_asset_id is None or int(cover_asset_id) <= 0:
                raise InvalidUploadPolicy('custom cover selection is invalid')
        else:
            raise InvalidUploadPolicy('cover selection is invalid')
        publish_delay_seconds = int(row['publish_delay_seconds'])
        if publish_delay_seconds != 0 and not (
            7200 <= publish_delay_seconds <= 15 * 24 * 60 * 60
        ):
            raise InvalidUploadPolicy('publish delay is invalid')
        fingerprint_source = {
            'account_id': int(row['resolved_account_id']),
            'broadcast_session_key': str(row['broadcast_session_key']),
            'file_identities': [part.identity.to_json() for part in parts],
            'title': title,
        }
        fingerprint = hashlib.sha256(
            json.dumps(
                fingerprint_source,
                ensure_ascii=False,
                separators=(',', ':'),
                sort_keys=True,
            ).encode('utf8')
        ).hexdigest()
        return {
            'format_version': 4,
            'fingerprint': fingerprint,
            'session_id': int(row['session_id']),
            'room_id': int(row['room_id']),
            'broadcast_session_key': str(row['broadcast_session_key']),
            'account_id': int(row['resolved_account_id']),
            'account_uid': int(row['resolved_account_uid']),
            'account_credential_version_at_creation': int(row['credential_version']),
            'title': title,
            'description': description,
            'dynamic': dynamic,
            'tid': int(row['tid']),
            'tags': tags,
            'creation_statement_id': creation_statement_id,
            'original_authorization': original_authorization,
            'copyright': copyright_value,
            'source': source,
            'is_only_self': bool(row['is_only_self']),
            'publish_dynamic': bool(row['publish_dynamic']),
            'no_reprint': bool(row['no_reprint']),
            'up_selection_reply': bool(row['up_selection_reply']),
            'up_close_reply': bool(row['up_close_reply']),
            'up_close_danmu': bool(row['up_close_danmu']),
            'cover_url': str(row['cover_url']),
            'cover_path': None if row['cover_path'] is None else str(row['cover_path']),
            'cover_mode': cover_mode,
            'cover_asset_id': (None if cover_asset_id is None else int(cover_asset_id)),
            'collection_season_id': (
                None if collection_season_id is None else int(collection_season_id)
            ),
            'collection_section_id': (
                None if collection_section_id is None else int(collection_section_id)
            ),
            'publish_delay_seconds': publish_delay_seconds,
            'auto_comment': bool(row['auto_comment']),
            'danmaku_backfill': bool(row['danmaku_backfill']),
            'filters': filters,
            'part_titles': part_titles,
        }

    async def _process(self, claim: LeaseClaim) -> None:
        job = await self._load_job(claim)
        if job.state == 'submitting' and job.submit_state == 'in_flight':
            await self._update_job(
                claim,
                {
                    'state': 'paused',
                    'submit_state': 'unknown_outcome',
                    'review_reason': '投稿提交在重启前已发出，远端结果未知',
                    'updated_at': int(self._clock()),
                },
                release=True,
            )
            return
        account = await self._database.fetchone(
            'SELECT state,credential_version FROM bili_accounts WHERE id=?',
            (job.account_id,),
        )
        if account is None or str(account['state']) != 'active':
            await self._pause_job(claim, '投稿账号不可用')
            return
        credential_version = int(account['credential_version'])
        submit_started = False
        try:
            gate = self._account_gates.for_account(job.account_id)
            async with gate.hold(credential_version):
                bundle = await self._bundle_loader(job.account_id)
                if job.state != 'submitting':
                    await self._update_job(
                        claim, {'state': 'uploading', 'updated_at': int(self._clock())}
                    )
                parts = await self._database.fetchall(
                    'SELECT id,part_index FROM upload_parts '
                    'WHERE job_id=? ORDER BY part_index',
                    (claim.id,),
                )
                if not parts:
                    await self._pause_job(claim, '上传任务没有分 P')
                    return
                for part in parts:
                    await self._uploader.upload_part(
                        int(part['id']), bundle=bundle, claim=claim
                    )
                if self._stop_requested():
                    raise UposUploadStopped('upload stopped before archive submission')
                payload = await self._submit_payload(job)
                scheduled_publish_at = payload.get('dtime')
                now = int(self._clock())
                await self._update_job(
                    claim,
                    {
                        'state': 'submitting',
                        'submit_state': 'in_flight',
                        'scheduled_publish_at': scheduled_publish_at,
                        'upload_completed_at': job.upload_completed_at or now,
                        'updated_at': now,
                    },
                )
                submit_started = True
                response = await self._protocol.submit_archive(bundle, payload)
        except DefinitelyNotSent:
            await self._retry_not_sent(claim, submit_started=submit_started)
            return
        except RemoteOutcomeUnknown:
            if submit_started:
                await self._pause_unknown_submission(claim)
            else:
                await self._retry_not_sent(claim, submit_started=False)
            return
        except UposUploadPaused:
            state = await self._database.scalar(
                'SELECT state FROM upload_jobs WHERE id=?', (claim.id,)
            )
            if state == 'paused':
                await self._release_lease(claim)
            else:
                await self._pause_job(claim, '上传分 P 需要人工处理')
            return
        except UposUploadStopped:
            await self._release_lease(claim)
            return
        except (AccountNotFound, AccountPaused, CredentialVersionChanged):
            await self._pause_job(claim, '投稿账号在执行期间发生变化')
            return
        except (CredentialNotFound, InvalidCredentialBundle, InvalidCredentialKey):
            await self._pause_job(claim, '投稿账号凭据无法读取')
            return
        except (
            CoverAssetNotFound,
            CoverResolutionError,
            InvalidCover,
            StoredCoverUnavailable,
        ):
            await self._pause_job(claim, '投稿封面无法读取或上传')
            return
        except BiliApiError as error:
            await self._update_job(
                claim,
                {
                    'state': 'paused',
                    'submit_state': (
                        'failed_permanent' if submit_started else 'prepared'
                    ),
                    'review_reason': 'B 站接口拒绝请求（{}）'.format(error.code),
                    'updated_at': int(self._clock()),
                },
                release=True,
            )
            return
        except ProtocolContractError:
            if submit_started:
                await self._pause_unknown_submission(claim)
            else:
                await self._pause_job(claim, '上传协议响应不符合预期')
            return
        aid, bvid = self._submission_identity(response)
        if aid is None or bvid is None:
            await self._pause_unknown_submission(claim)
            return
        await self._update_job(
            claim,
            {
                'state': 'waiting_review',
                'submit_state': 'confirmed',
                'aid': aid,
                'bvid': bvid,
                'submitted_at': int(self._clock()),
                'review_reason': None,
                'updated_at': int(self._clock()),
            },
            release=True,
        )

    async def _submit_payload(
        self, job: _Job, *, cover_override: Optional[str] = None
    ) -> Mapping[str, Any]:
        try:
            snapshot = json.loads(job.policy_snapshot_json)
        except json.JSONDecodeError:
            raise ProtocolContractError('invalid upload policy snapshot') from None
        if not isinstance(snapshot, dict) or snapshot.get('format_version') not in (
            1,
            2,
            3,
            4,
        ):
            raise ProtocolContractError('invalid upload policy snapshot')
        format_version = int(snapshot['format_version'])
        parts = await self._database.fetchall(
            'SELECT part_index,remote_filename FROM upload_parts '
            'WHERE job_id=? ORDER BY part_index',
            (job.id,),
        )
        titles = snapshot.get('part_titles')
        if not isinstance(titles, list) or len(titles) != len(parts):
            raise ProtocolContractError('invalid upload policy snapshot')
        videos = []
        for index, part in enumerate(parts):
            remote_filename = part['remote_filename']
            title = titles[index]
            if (
                not isinstance(remote_filename, str)
                or not remote_filename
                or not isinstance(title, str)
                or not title
            ):
                raise ProtocolContractError('upload part is incomplete')
            videos.append(
                {'filename': remote_filename, 'title': title[:80], 'desc': ''}
            )
        copyright_value = snapshot.get('copyright')
        if type(copyright_value) is not int or copyright_value not in (1, 2, 3):
            raise ProtocolContractError('invalid upload policy snapshot')
        if format_version == 1:
            dynamic = ''
            no_disturbance = 0
            no_reprint = 1
            is_only_self = 0
            up_selection_reply = False
            up_close_reply = False
            up_close_danmu = False
        else:
            rendered_dynamic = snapshot.get('dynamic', '')
            if not isinstance(rendered_dynamic, str):
                raise ProtocolContractError('invalid upload policy snapshot')
            publish_dynamic = bool(snapshot.get('publish_dynamic'))
            dynamic = rendered_dynamic if publish_dynamic else ''
            no_disturbance = 0 if publish_dynamic else 1
            no_reprint = 1 if bool(snapshot.get('no_reprint')) else 0
            is_only_self = 1 if bool(snapshot.get('is_only_self')) else 0
            up_selection_reply = bool(snapshot.get('up_selection_reply'))
            up_close_reply = bool(snapshot.get('up_close_reply'))
            up_close_danmu = bool(snapshot.get('up_close_danmu'))
        if format_version == 3:
            creation_statement_id = snapshot.get('creation_statement_id')
            original_authorization = snapshot.get('original_authorization')
            if (
                type(creation_statement_id) is not int
                or type(original_authorization) is not bool
            ):
                raise ProtocolContractError('invalid upload policy snapshot')
            expected_copyright = (
                2 if creation_statement_id == -2 else 1 if original_authorization else 3
            )
            if copyright_value != expected_copyright or (
                creation_statement_id == -2 and original_authorization
            ):
                raise ProtocolContractError('invalid upload policy snapshot')
            no_reprint = 1 if original_authorization else 0
        else:
            creation_statement_id = -2 if copyright_value == 2 else -1
            if copyright_value == 2:
                no_reprint = 0
        if cover_override is not None:
            cover = cover_override
        elif format_version == 4:
            cover = await self._resolve_cover(job, snapshot)
        else:
            cover = snapshot.get('cover_url', '')
        payload: Dict[str, Any] = {
            'cover': cover,
            'title': snapshot.get('title'),
            'copyright': copyright_value,
            'tid': snapshot.get('tid'),
            'tag': snapshot.get('tags'),
            'desc_format_id': 0,
            'desc': snapshot.get('description', ''),
            'recreate': 0,
            'dynamic': dynamic,
            'interactive': 0,
            'videos': videos,
            'act_reserve_create': 0,
            'no_disturbance': no_disturbance,
            'no_reprint': no_reprint,
            'is_only_self': is_only_self,
            'open_elec': 0,
            'subtitle': {'open': 0, 'lan': ''},
            'dolby': 0,
            'lossless_music': 0,
            'up_selection_reply': up_selection_reply,
            'up_close_reply': up_close_reply,
            'up_close_danmu': up_close_danmu,
            'creation_statement': {'id': creation_statement_id},
        }
        if copyright_value == 2:
            source = snapshot.get('source', '')
            if not isinstance(source, str) or not source:
                raise ProtocolContractError('invalid upload policy snapshot')
            payload['source'] = source
        if format_version == 4:
            publish_delay_seconds = snapshot.get('publish_delay_seconds')
            if type(publish_delay_seconds) is not int or (
                publish_delay_seconds != 0
                and not 7200 <= publish_delay_seconds <= 15 * 24 * 60 * 60
            ):
                raise ProtocolContractError('invalid upload policy snapshot')
            if publish_delay_seconds:
                payload['dtime'] = int(self._clock()) + publish_delay_seconds
        return payload

    async def _resolve_cover(self, job: _Job, snapshot: Mapping[str, Any]) -> str:
        if snapshot.get('account_id') != job.account_id:
            raise ProtocolContractError('invalid upload policy snapshot')
        cover_mode = snapshot.get('cover_mode')
        if cover_mode == 'custom':
            asset_id = snapshot.get('cover_asset_id')
            if type(asset_id) is not int or asset_id <= 0:
                raise ProtocolContractError('invalid upload policy snapshot')
            return await self._cover_resolver.remote_url(asset_id, job.account_id)
        if cover_mode != 'live' or snapshot.get('cover_asset_id') is not None:
            raise ProtocolContractError('invalid upload policy snapshot')
        local_path = snapshot.get('cover_path')
        source_url = snapshot.get('cover_url')
        if local_path is not None and not isinstance(local_path, str):
            raise ProtocolContractError('invalid upload policy snapshot')
        if not isinstance(source_url, str) or not source_url:
            raise ProtocolContractError('invalid upload policy snapshot')
        return await self._cover_resolver.live_url(
            job.account_id, local_path=local_path, source_url=source_url
        )

    async def _load_job(self, claim: LeaseClaim) -> _Job:
        row = await self._database.fetchone(
            'SELECT id,account_id,policy_snapshot_json,state,submit_state,'
            'upload_completed_at '
            'FROM upload_jobs WHERE id=? AND lease_owner=? AND lease_generation=?',
            (claim.id, claim.lease_owner, claim.lease_generation),
        )
        if row is None:
            raise LeaseLost('upload job lease was lost')
        return _Job(
            id=int(row['id']),
            account_id=int(row['account_id']),
            policy_snapshot_json=str(row['policy_snapshot_json']),
            state=str(row['state']),
            submit_state=str(row['submit_state']),
            upload_completed_at=(
                None
                if row['upload_completed_at'] is None
                else int(row['upload_completed_at'])
            ),
        )

    async def _retry_not_sent(self, claim: LeaseClaim, *, submit_started: bool) -> None:
        delay = min(300, 2 ** min(claim.attempt, 8))
        await self._update_job(
            claim,
            {
                'state': 'submitting' if submit_started else 'uploading',
                'submit_state': 'prepared',
                'review_reason': '请求确认未发出，将自动重试',
                'next_attempt_at': int(self._clock()) + delay,
                'updated_at': int(self._clock()),
            },
            release=True,
        )

    async def _pause_unknown_submission(self, claim: LeaseClaim) -> None:
        await self._update_job(
            claim,
            {
                'state': 'paused',
                'submit_state': 'unknown_outcome',
                'review_reason': '投稿提交结果未知，需要远端对账',
                'updated_at': int(self._clock()),
            },
            release=True,
        )

    async def _pause_job(self, claim: LeaseClaim, reason: str) -> None:
        await self._update_job(
            claim,
            {
                'state': 'paused',
                'review_reason': reason,
                'updated_at': int(self._clock()),
            },
            release=True,
        )

    async def _release_lease(self, claim: LeaseClaim) -> None:
        await self._update_job(claim, {'updated_at': int(self._clock())}, release=True)

    async def _update_job(
        self, claim: LeaseClaim, values: Mapping[str, Any], *, release: bool = False
    ) -> None:
        allowed = {
            'aid',
            'bvid',
            'next_attempt_at',
            'review_reason',
            'scheduled_publish_at',
            'state',
            'submit_state',
            'submitted_at',
            'upload_completed_at',
            'updated_at',
        }
        if not values or not set(values) <= allowed:
            raise ValueError('invalid upload job update')
        assignments = ['{}=?'.format(column) for column in values]
        parameters: List[Any] = list(values.values())
        if release:
            assignments.extend(('lease_owner=NULL', 'lease_until=NULL'))
        parameters.extend((claim.id, claim.lease_owner, claim.lease_generation))
        updated = await self._database.execute(
            'UPDATE upload_jobs SET {} WHERE id=? AND lease_owner=? '
            'AND lease_generation=?'.format(','.join(assignments)),
            parameters,
        )
        if updated != 1:
            raise LeaseLost('upload job lease was lost')

    @staticmethod
    def _submission_identity(
        response: Mapping[str, Any]
    ) -> Tuple[Optional[int], Optional[str]]:
        data = response.get('data')
        if not isinstance(data, Mapping):
            return None, None
        raw_aid = data.get('aid')
        if type(raw_aid) is int:
            aid = raw_aid
        elif isinstance(raw_aid, str) and raw_aid.isdigit():
            aid = int(raw_aid)
        else:
            return None, None
        bvid = data.get('bvid')
        if aid <= 0 or not isinstance(bvid, str) or not bvid:
            return None, None
        return aid, bvid

    @staticmethod
    async def _file_identity(path: str) -> FileIdentity:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, FileIdentity.from_path, path)

    @staticmethod
    def _enabled(value: _FeatureSwitch) -> bool:
        return bool(value() if callable(value) else value)
