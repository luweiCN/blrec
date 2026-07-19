from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import time
import uuid
from dataclasses import dataclass, replace
from typing import Any, Awaitable, Callable, Dict, List, Mapping, Optional, Tuple

from liquid import Environment

from blrec.logging.audit import audit

from .accounts import (
    AccountNotFound,
    AccountPaused,
    AccountWriteGate,
    CredentialVersionChanged,
)
from .artifact_recovery import RecoveredArtifact, probe_recording_artifact
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
from .policies import (
    InvalidRoomUploadPolicy,
    RoomUploadPolicyCommand,
    RoomUploadPolicyManager,
    RoomUploadPolicyNotFound,
    RoomUploadPolicyView,
    default_room_upload_policy,
    room_upload_policy_command,
)
from .session_submission import InvalidSessionSubmission, decode_submission_settings
from .upos import (
    FileIdentity,
    UposUploadDeferred,
    UposUploader,
    UposUploadPaused,
    UposUploadStopped,
)

__all__ = ('InvalidUploadPolicy', 'UploadCoordinator')


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

    @property
    def snapshot_identity(self) -> str:
        return self.identity.to_json()


@dataclass(frozen=True)
class _SnapshotPart:
    part_index: int
    snapshot_identity: str


@dataclass(frozen=True)
class _Job:
    id: int
    account_id: int
    policy_snapshot_json: str
    state: str
    submit_state: str
    upload_completed_at: Optional[int]
    preupload_finalized: bool


@dataclass(frozen=True)
class _ResolvedLiveSettings:
    command: RoomUploadPolicyCommand
    settings_source: str
    account: sqlite3.Row
    policy_updated_at: Optional[int]


class UploadCoordinator:
    _MIN_UPLOAD_PART_DURATION_SECONDS = 60
    _MEDIA_PROBE_MAX_FAILURES = 5
    _MEDIA_PROBE_PENDING_REASON = '录像媒体信息暂时无法读取，等待重新校验'
    _MEDIA_PROBE_FAILED_REASON = '录像媒体信息连续校验失败，已排除自动投稿'

    def __init__(
        self,
        database: BiliUploadDatabase,
        protocol: Any,
        uploader: UposUploader,
        *,
        bundle_loader: Callable[[int], Awaitable[CredentialBundle]],
        account_gates: AccountWriteGate,
        cover_resolver: CoverResolver,
        worker_id: Optional[str] = None,
        stability_seconds: int = 30,
        clock: Callable[[], float] = time.time,
        stop_requested: Callable[[], bool] = lambda: False,
        artifact_probe: Callable[[str], Optional[RecoveredArtifact]] = (
            probe_recording_artifact
        ),
    ) -> None:
        if stability_seconds < 0:
            raise ValueError('file stability window must not be negative')
        self._database = database
        self._protocol = protocol
        self._uploader = uploader
        self._bundle_loader = bundle_loader
        self._account_gates = account_gates
        self._cover_resolver = cover_resolver
        self._worker_id = worker_id or 'upload-{}'.format(uuid.uuid4().hex)
        self._stability_seconds = stability_seconds
        self._clock = clock
        self._stop_requested = stop_requested
        self._artifact_probe = artifact_probe
        self._artifact_probe_next_at: Dict[int, int] = {}
        self._run_lock = asyncio.Lock()
        self._liquid = Environment()
        self._policy_manager = RoomUploadPolicyManager(database, clock=clock)

    async def create_ready_jobs(self) -> List[int]:
        created = await self.sync_live_sessions()
        await self.prepare_waiting_jobs()
        return created

    async def sync_live_sessions(self) -> List[int]:
        changed: List[int] = []
        provisional = await self._database.fetchall(
            'SELECT job.id,job.session_id,session.live_end_time '
            'FROM upload_jobs job JOIN recording_sessions session '
            'ON session.id=job.session_id '
            "WHERE job.preupload_finalized=0 AND session.source_kind='live' "
            'ORDER BY session.started_at,job.id'
        )
        for row in provisional:
            job_id = int(row['id'])
            session_id = int(row['session_id'])
            if row['live_end_time'] is not None:
                if await self._finalize_preupload_job(session_id, job_id):
                    changed.append(job_id)
                continue
            session = await self._live_session(session_id)
            if session is None:
                continue
            settings, resolution_state, _error = await self._resolve_live_settings(
                session
            )
            if settings is None and resolution_state == 'not_requested':
                await self._cancel_preupload_job(
                    session_id, job_id, reason='本场已关闭自动投稿'
                )

        rows = await self._database.fetchall(
            'SELECT session.id FROM recording_sessions session '
            "WHERE session.source_kind='live' AND session.state='open' "
            'AND session.live_end_time IS NULL '
            "AND (session.upload_resolution_state='pending' OR ("
            "session.upload_resolution_state='not_requested' "
            "AND session.upload_decision='follow_room' "
            'AND NOT EXISTS(SELECT 1 FROM upload_suppressions suppression '
            'WHERE suppression.session_id=session.id))) '
            "AND session.deletion_state='none' "
            'AND EXISTS(SELECT 1 FROM recording_parts part '
            "WHERE part.session_id=session.id AND part.artifact_state='ready') "
            'AND NOT EXISTS(SELECT 1 FROM upload_jobs job '
            'WHERE job.session_id=session.id) ORDER BY session.started_at,session.id'
        )
        for row in rows:
            created_job_id = await self._resolve_live_session(
                int(row['id']), finalized=False
            )
            if created_job_id is not None:
                changed.append(created_job_id)

        changed.extend(await self.resolve_finished_sessions())
        return list(dict.fromkeys(changed))

    async def resolve_finished_sessions(self) -> List[int]:
        rows = await self._database.fetchall(
            'SELECT id FROM recording_sessions '
            "WHERE source_kind='live' AND live_end_time IS NOT NULL "
            "AND upload_resolution_state='pending' "
            'AND NOT EXISTS(SELECT 1 FROM upload_jobs '
            'WHERE upload_jobs.session_id=recording_sessions.id) '
            'ORDER BY live_end_time,id'
        )
        created: List[int] = []
        for row in rows:
            job_id = await self._resolve_finished_session(int(row['id']))
            if job_id is not None:
                created.append(job_id)
        return created

    async def prepare_waiting_jobs(self) -> List[int]:
        rows = await self._database.fetchall(
            'SELECT job.id FROM upload_jobs job '
            'JOIN recording_sessions session ON session.id=job.session_id '
            "WHERE job.state='waiting_artifacts' "
            "AND session.source_kind='live' AND session.deletion_state='none' "
            'ORDER BY job.created_at,job.id'
        )
        prepared: List[int] = []
        for row in rows:
            if await self._prepare_waiting_job(int(row['id'])):
                prepared.append(int(row['id']))
        return prepared

    async def _resolve_finished_session(self, session_id: int) -> Optional[int]:
        return await self._resolve_live_session(session_id, finalized=True)

    async def _live_session(self, session_id: int) -> Optional[sqlite3.Row]:
        return await self._database.fetchone(
            'SELECT id AS session_id,room_id,broadcast_session_key,'
            'live_start_time,live_end_time,title,cover_url,cover_path,anchor_uid,'
            'anchor_name,area_id,area_name,parent_area_id,parent_area_name,'
            'state,deletion_state,upload_decision,upload_override_json,'
            'upload_resolution_state FROM recording_sessions WHERE id=?',
            (session_id,),
        )

    async def _resolve_live_session(
        self, session_id: int, *, finalized: bool
    ) -> Optional[int]:
        session = await self._live_session(session_id)
        if (
            session is None
            or (finalized and str(session['upload_resolution_state']) != 'pending')
            or (
                not finalized
                and str(session['upload_resolution_state'])
                not in ('pending', 'not_requested')
            )
            or (finalized and session['live_end_time'] is None)
            or (
                not finalized
                and (
                    str(session['state']) != 'open'
                    or session['live_end_time'] is not None
                )
            )
        ):
            return None
        await self._refresh_ready_part_durations(session_id)
        if finalized and await self._has_pending_media_probe(session_id):
            return None
        await self._mark_short_parts_excluded(session_id)
        if not finalized and not await self._has_stable_ready_part(session_id):
            return None
        settings, resolution_state, resolution_error = (
            await self._resolve_live_settings(session)
        )
        if settings is None:
            await self._set_upload_resolution(
                session_id, resolution_state, resolution_error
            )
            return None
        resolved_settings = settings
        live_session = session
        command = resolved_settings.command
        account = resolved_settings.account
        decision = str(live_session['upload_decision'])
        override_json = live_session['upload_override_json']
        account_id = int(account['id'])
        account_credential_version = int(account['credential_version'])
        part_rows = await self._database.fetchall(
            'SELECT id,part_index,source_path FROM recording_parts '
            'WHERE session_id=? AND (artifact_state!=\'ready\' OR '
            '(upload_excluded_reason IS NULL AND '
            '(record_duration_seconds IS NULL OR record_duration_seconds>=?))) '
            'ORDER BY part_index',
            (session_id, self._MIN_UPLOAD_PART_DURATION_SECONDS),
        )
        if not part_rows:
            pending_probe = await self._database.scalar(
                'SELECT 1 FROM recording_parts WHERE session_id=? '
                'AND upload_excluded_reason=? LIMIT 1',
                (session_id, self._MEDIA_PROBE_PENDING_REASON),
            )
            if pending_probe == 1:
                return None
            recording_part_count = int(
                await self._database.scalar(
                    'SELECT COUNT(*) FROM recording_parts WHERE session_id=?',
                    (session_id,),
                )
            )
            await self._set_upload_resolution(
                session_id,
                (
                    'not_requested'
                    if recording_part_count > 0
                    else 'configuration_required'
                ),
                (
                    '录像分段均不足 60 秒，已保留本地文件'
                    if recording_part_count > 0
                    else '本场没有可用于投稿的录像分段'
                ),
            )
            return None
        parts = [
            _SnapshotPart(
                part_index=int(part['part_index']),
                snapshot_identity='recording-part:{}:{}'.format(
                    int(part['id']), str(part['source_path'])
                ),
            )
            for part in part_rows
        ]
        candidate = self._command_candidate(dict(live_session), dict(account), command)
        try:
            snapshot = self._policy_snapshot(candidate, parts)
        except InvalidUploadPolicy:
            await self._set_upload_resolution(
                session_id,
                'configuration_required',
                '投稿设置无法生成稿件，请检查标题、分区和标签',
            )
            return None
        snapshot_json = json.dumps(
            snapshot, ensure_ascii=False, separators=(',', ':'), sort_keys=True
        )
        now = int(self._clock())

        def create(connection: sqlite3.Connection) -> Optional[int]:
            current = connection.execute(
                'SELECT upload_decision,upload_override_json,'
                'upload_resolution_state,live_end_time,deletion_state,state '
                'FROM recording_sessions WHERE id=?',
                (session_id,),
            ).fetchone()
            if (
                current is None
                or (finalized and str(current['upload_resolution_state']) != 'pending')
                or (
                    not finalized
                    and str(current['upload_resolution_state'])
                    not in ('pending', 'not_requested')
                )
                or str(current['deletion_state']) != 'none'
                or str(current['upload_decision']) != decision
                or current['upload_override_json'] != override_json
                or (finalized and current['live_end_time'] is None)
                or (
                    not finalized
                    and (
                        str(current['state']) != 'open'
                        or current['live_end_time'] is not None
                    )
                )
            ):
                return None
            if (
                connection.execute(
                    'SELECT 1 FROM upload_jobs WHERE session_id=?', (session_id,)
                ).fetchone()
                is not None
            ):
                return None
            if (
                connection.execute(
                    'SELECT 1 FROM upload_suppressions WHERE session_id=?',
                    (session_id,),
                ).fetchone()
                is not None
            ):
                return None
            current_account = connection.execute(
                'SELECT state,credential_version FROM bili_accounts WHERE id=?',
                (account_id,),
            ).fetchone()
            if (
                current_account is None
                or str(current_account['state']) != 'active'
                or int(current_account['credential_version'])
                != account_credential_version
            ):
                return None
            if command.account_mode == 'primary':
                primary_id = connection.execute(
                    'SELECT primary_account_id FROM bili_account_selection WHERE id=1'
                ).fetchone()
                if (
                    primary_id is None
                    or int(primary_id['primary_account_id']) != account_id
                ):
                    return None
            if resolved_settings.settings_source == 'room':
                current_policy = connection.execute(
                    'SELECT updated_at FROM room_upload_policies WHERE room_id=?',
                    (int(live_session['room_id']),),
                ).fetchone()
                if (
                    current_policy is None
                    or int(current_policy['updated_at'])
                    != resolved_settings.policy_updated_at
                ):
                    return None
            if not finalized:
                ready = connection.execute(
                    'SELECT 1 FROM recording_parts '
                    "WHERE session_id=? AND artifact_state='ready' "
                    'AND upload_excluded_reason IS NULL '
                    'AND (record_duration_seconds IS NULL OR '
                    'record_duration_seconds>=?) LIMIT 1',
                    (session_id, self._MIN_UPLOAD_PART_DURATION_SECONDS),
                ).fetchone()
                if ready is None:
                    return None
            cursor = connection.execute(
                'INSERT INTO upload_jobs('
                'session_id,account_id,policy_snapshot_json,state,submit_state,'
                'comment_branch_state,danmaku_branch_state,'
                'collection_branch_state,preupload_finalized,created_at,updated_at) '
                "VALUES(?,?,?,'waiting_artifacts','prepared',?,?,?,?,?,?)",
                (
                    session_id,
                    account_id,
                    snapshot_json,
                    'pending' if command.auto_comment else 'disabled',
                    'pending' if command.danmaku_backfill else 'disabled',
                    (
                        'pending'
                        if command.collection_section_id is not None
                        else 'disabled'
                    ),
                    int(finalized),
                    now,
                    now,
                ),
            )
            connection.execute(
                "UPDATE recording_sessions SET upload_resolution_state='job_created',"
                'upload_resolution_error=NULL,upload_resolved_at=? WHERE id=?',
                (now, session_id),
            )
            return int(cursor.lastrowid)

        job_id = await self._database.write(create)
        if job_id is not None:
            audit(
                'upload_job_resolved',
                job_id=job_id,
                session_id=session_id,
                room_id=int(live_session['room_id']),
                decision=decision,
                settings_source=resolved_settings.settings_source,
                account_id=account_id,
                parts=len(parts),
                preupload=not finalized,
            )
            audit(
                'upload_job_created',
                job_id=job_id,
                session_id=session_id,
                room_id=int(live_session['room_id']),
                account_id=int(account['id']),
                parts=len(parts),
                state='waiting_artifacts',
                preupload=not finalized,
            )
        return job_id

    async def _mark_short_parts_excluded(self, session_id: int) -> None:
        reason = '录像不足 {} 秒，已保留本地文件但不投稿'.format(
            self._MIN_UPLOAD_PART_DURATION_SECONDS
        )
        await self._database.execute(
            'UPDATE recording_parts SET upload_excluded_reason=NULL '
            'WHERE session_id=? AND upload_excluded_reason=? '
            'AND (artifact_state!=\'ready\' OR record_duration_seconds IS NULL '
            'OR record_duration_seconds>=?)',
            (session_id, reason, self._MIN_UPLOAD_PART_DURATION_SECONDS),
        )
        rows = await self._database.fetchall(
            'SELECT id,part_index,record_duration_seconds FROM recording_parts '
            "WHERE session_id=? AND artifact_state='ready' "
            'AND record_duration_seconds<? AND upload_excluded_reason IS NULL '
            'ORDER BY part_index',
            (session_id, self._MIN_UPLOAD_PART_DURATION_SECONDS),
        )
        for row in rows:
            updated = await self._database.execute(
                'UPDATE recording_parts SET upload_excluded_reason=? '
                'WHERE id=? AND upload_excluded_reason IS NULL',
                (reason, int(row['id'])),
            )
            if updated == 1:
                audit(
                    'upload_part_excluded',
                    level='INFO',
                    session_id=session_id,
                    part_id=int(row['id']),
                    part_index=int(row['part_index']),
                    duration_seconds=int(row['record_duration_seconds']),
                    minimum_duration_seconds=self._MIN_UPLOAD_PART_DURATION_SECONDS,
                    reason='short_recording',
                )

    async def _refresh_ready_part_durations(self, session_id: int) -> None:
        rows = await self._database.fetchall(
            'SELECT part.id,part.part_index,part.final_path,'
            'part.record_duration_seconds,part.upload_probe_attempt '
            'FROM recording_parts part '
            "WHERE part.session_id=? AND part.artifact_state='ready' "
            'AND part.final_path IS NOT NULL '
            'AND (part.upload_excluded_reason IS NULL '
            'OR part.upload_excluded_reason=?) '
            'AND NOT EXISTS(SELECT 1 FROM upload_jobs job '
            'JOIN upload_parts upload ON upload.job_id=job.id '
            'WHERE job.session_id=part.session_id '
            'AND upload.part_index=part.part_index) ORDER BY part.part_index',
            (session_id, self._MEDIA_PROBE_PENDING_REASON),
        )
        loop = asyncio.get_running_loop()
        for row in rows:
            part_id = int(row['id'])
            now = int(self._clock())
            if now < self._artifact_probe_next_at.get(part_id, 0):
                continue
            path = str(row['final_path'])
            recovered = await loop.run_in_executor(None, self._artifact_probe, path)
            if recovered is None or recovered.duration_seconds is None:
                failures = int(row['upload_probe_attempt']) + 1
                if failures >= self._MEDIA_PROBE_MAX_FAILURES:
                    self._artifact_probe_next_at.pop(part_id, None)
                    await self._database.execute(
                        'UPDATE recording_parts SET upload_excluded_reason=?, '
                        'upload_probe_attempt=? '
                        "WHERE id=? AND session_id=? AND artifact_state='ready' "
                        'AND final_path=?',
                        (
                            self._MEDIA_PROBE_FAILED_REASON,
                            failures,
                            part_id,
                            session_id,
                            path,
                        ),
                    )
                    audit(
                        'upload_part_duration_probe_failed',
                        level='ERROR',
                        session_id=session_id,
                        part_id=part_id,
                        part_index=int(row['part_index']),
                        attempts=failures,
                        result='excluded_from_upload',
                    )
                    continue
                delay = min(15 * 60, 60 * (2 ** min(failures - 1, 4)))
                self._artifact_probe_next_at[part_id] = now + delay
                await self._database.execute(
                    'UPDATE recording_parts SET upload_excluded_reason=?, '
                    'upload_probe_attempt=? '
                    "WHERE id=? AND session_id=? AND artifact_state='ready' "
                    'AND final_path=?',
                    (
                        self._MEDIA_PROBE_PENDING_REASON,
                        failures,
                        part_id,
                        session_id,
                        path,
                    ),
                )
                audit(
                    'upload_part_duration_probe_unavailable',
                    level='WARNING',
                    session_id=session_id,
                    part_id=part_id,
                    part_index=int(row['part_index']),
                    retry_after_seconds=delay,
                )
                continue
            duration_seconds = int(recovered.duration_seconds)
            self._artifact_probe_next_at.pop(part_id, None)
            updated = await self._database.execute(
                'UPDATE recording_parts SET record_duration_seconds=?,'
                'upload_excluded_reason=NULL,upload_probe_attempt=0 '
                "WHERE id=? AND session_id=? AND artifact_state='ready' "
                'AND final_path=? AND (upload_excluded_reason IS NULL '
                'OR upload_excluded_reason=?)',
                (
                    duration_seconds,
                    part_id,
                    session_id,
                    path,
                    self._MEDIA_PROBE_PENDING_REASON,
                ),
            )
            if updated == 1:
                audit(
                    'upload_part_duration_probed',
                    session_id=session_id,
                    part_id=part_id,
                    part_index=int(row['part_index']),
                    previous_duration_seconds=row['record_duration_seconds'],
                    media_duration_seconds=duration_seconds,
                )

    async def _has_pending_media_probe(self, session_id: int) -> bool:
        return bool(
            await self._database.scalar(
                'SELECT 1 FROM recording_parts WHERE session_id=? '
                'AND upload_excluded_reason=? LIMIT 1',
                (session_id, self._MEDIA_PROBE_PENDING_REASON),
            )
        )

    async def _resolve_live_settings(
        self, session: sqlite3.Row
    ) -> Tuple[Optional[_ResolvedLiveSettings], str, Optional[str]]:
        session_id = int(session['session_id'])
        if str(session['deletion_state']) != 'none' or str(session['state']) in (
            'cancelled',
            'skipped',
        ):
            return None, 'not_requested', None
        suppressed = await self._database.scalar(
            'SELECT 1 FROM upload_suppressions WHERE session_id=?', (session_id,)
        )
        if suppressed == 1:
            return None, 'not_requested', None
        decision = str(session['upload_decision'])
        if decision == 'skip':
            return None, 'not_requested', None
        room_policy: Optional[RoomUploadPolicyView]
        try:
            room_policy = await self._policy_manager.get(int(session['room_id']))
        except RoomUploadPolicyNotFound:
            room_policy = None
        if decision == 'follow_room' and (
            room_policy is None or not room_policy.enabled
        ):
            return None, 'not_requested', None
        override_json = session['upload_override_json']
        try:
            if override_json is not None:
                command = decode_submission_settings(str(override_json))
                settings_source = 'session'
                policy_updated_at = None
            elif room_policy is not None:
                command = room_upload_policy_command(room_policy)
                settings_source = 'room'
                policy_updated_at = room_policy.updated_at
            else:
                command = default_room_upload_policy()
                settings_source = 'default'
                policy_updated_at = None
            command = replace(command, enabled=True)
            await self._policy_manager.validate(int(session['room_id']), command)
        except (
            InvalidRoomUploadPolicy,
            InvalidSessionSubmission,
            InvalidUploadPolicy,
            ValueError,
        ):
            return (
                None,
                'configuration_required',
                '投稿账号不可用，请在本场投稿设置中重新选择',
            )
        account = await self._resolved_account(command)
        if account is None:
            return (
                None,
                'configuration_required',
                '投稿账号不可用，请在本场投稿设置中重新选择',
            )
        return (
            _ResolvedLiveSettings(
                command=command,
                settings_source=settings_source,
                account=account,
                policy_updated_at=policy_updated_at,
            ),
            'job_created',
            None,
        )

    async def _finalize_preupload_job(self, session_id: int, job_id: int) -> bool:
        session = await self._live_session(session_id)
        if session is None or session['live_end_time'] is None:
            return False
        settings, resolution_state, resolution_error = (
            await self._resolve_live_settings(session)
        )
        if settings is None:
            if resolution_state == 'not_requested':
                return await self._cancel_preupload_job(
                    session_id, job_id, reason='直播结束时已关闭自动投稿'
                )
            await self._pause_preupload_for_configuration(
                session_id,
                job_id,
                resolution_state=resolution_state,
                error=resolution_error or '投稿设置不可用，请检查本场投稿设置',
            )
            return False

        resolved_settings = settings
        live_session = session

        await self._refresh_ready_part_durations(session_id)
        if await self._has_pending_media_probe(session_id):
            return False
        await self._mark_short_parts_excluded(session_id)
        part_rows = await self._database.fetchall(
            'SELECT id,part_index,source_path FROM recording_parts '
            'WHERE session_id=? AND (artifact_state!=\'ready\' OR '
            '(upload_excluded_reason IS NULL AND '
            '(record_duration_seconds IS NULL OR record_duration_seconds>=?))) '
            'ORDER BY part_index',
            (session_id, self._MIN_UPLOAD_PART_DURATION_SECONDS),
        )
        if not part_rows:
            return False
        snapshot_parts = [
            _SnapshotPart(
                part_index=int(part['part_index']),
                snapshot_identity='recording-part:{}:{}'.format(
                    int(part['id']), str(part['source_path'])
                ),
            )
            for part in part_rows
        ]
        candidate = self._command_candidate(
            dict(live_session),
            dict(resolved_settings.account),
            resolved_settings.command,
        )
        try:
            snapshot = self._policy_snapshot(candidate, snapshot_parts)
        except InvalidUploadPolicy:
            await self._pause_preupload_for_configuration(
                session_id,
                job_id,
                resolution_state='configuration_required',
                error='投稿设置无法生成稿件，请检查标题、分区和标签',
            )
            return False
        snapshot_json = json.dumps(
            snapshot, ensure_ascii=False, separators=(',', ':'), sort_keys=True
        )
        account_id = int(resolved_settings.account['id'])
        credential_version = int(resolved_settings.account['credential_version'])
        decision = str(live_session['upload_decision'])
        override_json = live_session['upload_override_json']
        now = int(self._clock())
        expected_parts = [
            (int(part['id']), int(part['part_index']), str(part['source_path']))
            for part in part_rows
        ]

        def finalize(connection: sqlite3.Connection) -> Tuple[bool, bool, str]:
            current_session = connection.execute(
                'SELECT upload_decision,upload_override_json,live_end_time,'
                'deletion_state FROM recording_sessions WHERE id=?',
                (session_id,),
            ).fetchone()
            job = connection.execute(
                'SELECT account_id,operator_paused,lease_until '
                'FROM upload_jobs WHERE id=? AND session_id=? '
                'AND preupload_finalized=0',
                (job_id, session_id),
            ).fetchone()
            if (
                current_session is None
                or current_session['live_end_time'] is None
                or str(current_session['deletion_state']) != 'none'
                or str(current_session['upload_decision']) != decision
                or current_session['upload_override_json'] != override_json
                or job is None
                or (job['lease_until'] is not None and int(job['lease_until']) > now)
            ):
                return False, False, 'waiting_artifacts'
            current_parts = connection.execute(
                'SELECT id,part_index,source_path FROM recording_parts '
                'WHERE session_id=? AND (artifact_state!=\'ready\' OR '
                '(upload_excluded_reason IS NULL AND '
                '(record_duration_seconds IS NULL OR '
                'record_duration_seconds>=?))) '
                'ORDER BY part_index',
                (session_id, self._MIN_UPLOAD_PART_DURATION_SECONDS),
            ).fetchall()
            if [
                (int(part['id']), int(part['part_index']), str(part['source_path']))
                for part in current_parts
            ] != expected_parts:
                return False, False, 'waiting_artifacts'
            account = connection.execute(
                'SELECT state,credential_version FROM bili_accounts WHERE id=?',
                (account_id,),
            ).fetchone()
            if (
                account is None
                or str(account['state']) != 'active'
                or int(account['credential_version']) != credential_version
            ):
                return False, False, 'waiting_artifacts'
            if resolved_settings.command.account_mode == 'primary':
                selected = connection.execute(
                    'SELECT primary_account_id FROM bili_account_selection WHERE id=1'
                ).fetchone()
                if (
                    selected is None
                    or int(selected['primary_account_id']) != account_id
                ):
                    return False, False, 'waiting_artifacts'
            if resolved_settings.settings_source == 'room':
                policy = connection.execute(
                    'SELECT updated_at FROM room_upload_policies WHERE room_id=?',
                    (int(live_session['room_id']),),
                ).fetchone()
                if (
                    policy is None
                    or int(policy['updated_at']) != resolved_settings.policy_updated_at
                ):
                    return False, False, 'waiting_artifacts'

            account_changed = int(job['account_id']) != account_id
            if account_changed:
                connection.execute(
                    'DELETE FROM upload_chunks WHERE part_id IN('
                    'SELECT id FROM upload_parts WHERE job_id=?)',
                    (job_id,),
                )
                connection.execute(
                    "UPDATE upload_parts SET upload_state='prepared',"
                    'remote_filename=NULL,cid=NULL,upload_session_json=NULL '
                    'WHERE job_id=?',
                    (job_id,),
                )
            danmaku_backfill = bool(resolved_settings.command.danmaku_backfill)
            part_rows_for_job = connection.execute(
                'SELECT id,xml_path FROM upload_parts WHERE job_id=?', (job_id,)
            ).fetchall()
            for part in part_rows_for_job:
                danmaku_state = 'disabled'
                if danmaku_backfill:
                    danmaku_state = 'pending' if part['xml_path'] else 'missing_source'
                connection.execute(
                    'UPDATE upload_parts SET danmaku_import_state=? WHERE id=?',
                    (danmaku_state, int(part['id'])),
                )
            pending_artifact = connection.execute(
                'SELECT 1 FROM recording_parts WHERE session_id=? '
                "AND artifact_state NOT IN ('ready','failed','missing') LIMIT 1",
                (session_id,),
            ).fetchone()
            missing_ready = connection.execute(
                'SELECT 1 FROM recording_parts part '
                'LEFT JOIN upload_parts upload ON upload.job_id=? '
                'AND upload.part_index=part.part_index '
                "WHERE part.session_id=? AND part.artifact_state='ready' "
                'AND part.upload_excluded_reason IS NULL '
                'AND (part.record_duration_seconds IS NULL OR '
                'part.record_duration_seconds>=?) '
                'AND upload.id IS NULL LIMIT 1',
                (job_id, session_id, self._MIN_UPLOAD_PART_DURATION_SECONDS),
            ).fetchone()
            part_count = int(
                connection.execute(
                    'SELECT COUNT(*) FROM upload_parts WHERE job_id=?', (job_id,)
                ).fetchone()[0]
            )
            resume_state = (
                'ready'
                if pending_artifact is None and missing_ready is None and part_count > 0
                else 'waiting_artifacts'
            )
            operator_paused = bool(job['operator_paused'])
            state = 'paused' if operator_paused else resume_state
            connection.execute(
                'UPDATE upload_jobs SET account_id=?,policy_snapshot_json=?,'
                'state=?,submit_state=\'prepared\',preupload_finalized=1,'
                'comment_branch_state=?,danmaku_branch_state=?,'
                'collection_branch_state=?,collection_error=NULL,'
                'operator_resume_state=?,review_reason=?,next_attempt_at=0,'
                'updated_at=? WHERE id=?',
                (
                    account_id,
                    snapshot_json,
                    state,
                    (
                        'pending'
                        if resolved_settings.command.auto_comment
                        else 'disabled'
                    ),
                    'pending' if danmaku_backfill else 'disabled',
                    (
                        'pending'
                        if resolved_settings.command.collection_section_id is not None
                        else 'disabled'
                    ),
                    resume_state if operator_paused else None,
                    '管理员已暂停上传' if operator_paused else None,
                    now,
                    job_id,
                ),
            )
            connection.execute(
                "UPDATE recording_sessions SET upload_resolution_state='job_created',"
                'upload_resolution_error=NULL,upload_resolved_at=? WHERE id=?',
                (now, session_id),
            )
            return True, account_changed, resume_state

        finalized, account_changed, state = await self._database.write(finalize)
        if finalized:
            audit(
                'upload_preupload_finalized',
                job_id=job_id,
                session_id=session_id,
                account_id=account_id,
                account_changed=account_changed,
                next_state=state,
                parts=len(snapshot_parts),
            )
        return finalized

    async def _pause_preupload_for_configuration(
        self, session_id: int, job_id: int, *, resolution_state: str, error: str
    ) -> bool:
        now = int(self._clock())

        def pause(connection: sqlite3.Connection) -> bool:
            job = connection.execute(
                'SELECT state,review_reason FROM upload_jobs '
                'WHERE id=? AND session_id=? AND preupload_finalized=0',
                (job_id, session_id),
            ).fetchone()
            if job is None:
                return False
            changed = str(job['state']) != 'paused' or job['review_reason'] != error
            if changed:
                connection.execute(
                    "UPDATE upload_jobs SET state='paused',review_reason=?,"
                    'lease_owner=NULL,lease_until=NULL,updated_at=? WHERE id=?',
                    (error, now, job_id),
                )
            connection.execute(
                'UPDATE recording_sessions SET upload_resolution_state=?,'
                'upload_resolution_error=?,upload_resolved_at=? WHERE id=?',
                (resolution_state, error, now, session_id),
            )
            return changed

        paused = await self._database.write(pause)
        if paused:
            audit(
                'upload_preupload_configuration_required',
                level='WARNING',
                job_id=job_id,
                session_id=session_id,
                error=error,
            )
        return paused

    async def _cancel_preupload_job(
        self, session_id: int, job_id: int, *, reason: str
    ) -> bool:
        now = int(self._clock())

        def cancel(connection: sqlite3.Connection) -> bool:
            job = connection.execute(
                'SELECT lease_until FROM upload_jobs WHERE id=? AND session_id=? '
                'AND preupload_finalized=0',
                (job_id, session_id),
            ).fetchone()
            if job is None or (
                job['lease_until'] is not None and int(job['lease_until']) > now
            ):
                return False
            connection.execute(
                'DELETE FROM upload_chunks WHERE part_id IN('
                'SELECT id FROM upload_parts WHERE job_id=?)',
                (job_id,),
            )
            connection.execute(
                'DELETE FROM danmaku_items WHERE part_id IN('
                'SELECT id FROM upload_parts WHERE job_id=?)',
                (job_id,),
            )
            connection.execute('DELETE FROM comment_items WHERE job_id=?', (job_id,))
            connection.execute('DELETE FROM upload_parts WHERE job_id=?', (job_id,))
            connection.execute('DELETE FROM upload_jobs WHERE id=?', (job_id,))
            connection.execute(
                "UPDATE recording_sessions SET upload_resolution_state='not_requested',"
                'upload_resolution_error=NULL,upload_resolved_at=? WHERE id=?',
                (now, session_id),
            )
            return True

        cancelled = await self._database.write(cancel)
        if cancelled:
            audit(
                'upload_preupload_cancelled',
                job_id=job_id,
                session_id=session_id,
                reason=reason,
            )
        return cancelled

    async def _prepare_waiting_job(self, job_id: int) -> bool:
        job = await self._database.fetchone(
            'SELECT session_id,policy_snapshot_json,preupload_finalized '
            'FROM upload_jobs '
            "WHERE id=? AND state='waiting_artifacts'",
            (job_id,),
        )
        if job is None:
            return False
        finalized = bool(job['preupload_finalized'])
        job_session_id = int(job['session_id'])
        try:
            snapshot = json.loads(str(job['policy_snapshot_json']))
        except json.JSONDecodeError:
            return False
        if not isinstance(snapshot, dict):
            return False
        await self._refresh_ready_part_durations(job_session_id)
        if finalized and await self._has_pending_media_probe(job_session_id):
            return False
        await self._mark_short_parts_excluded(job_session_id)
        rows = await self._database.fetchall(
            'SELECT id,part_index,source_path,final_path,xml_path,'
            'artifact_state,updated_at FROM recording_parts '
            "WHERE session_id=? AND artifact_state='ready' "
            'AND upload_excluded_reason IS NULL '
            'AND (record_duration_seconds IS NULL OR record_duration_seconds>=?) '
            'ORDER BY part_index',
            (job_session_id, self._MIN_UPLOAD_PART_DURATION_SECONDS),
        )
        if (
            finalized
            and not rows
            and await self._cancel_empty_finalized_job(job_session_id, job_id)
        ):
            return True
        if not rows and not finalized:
            return False
        existing_rows = await self._database.fetchall(
            'SELECT part_index FROM upload_parts WHERE job_id=?', (job_id,)
        )
        existing_indexes = {int(row['part_index']) for row in existing_rows}
        parts: List[_CandidatePart] = []
        stable_before_ns = int(
            (self._clock() - self._stability_seconds) * 1_000_000_000
        )
        for row in rows:
            if int(row['part_index']) in existing_indexes:
                continue
            if row['final_path'] is None:
                continue
            final_path = str(row['final_path'])
            try:
                identity = await self._file_identity(final_path)
            except OSError:
                continue
            if identity.mtime_ns > stable_before_ns:
                continue
            parts.append(
                _CandidatePart(
                    id=int(row['id']),
                    part_index=int(row['part_index']),
                    source_path=str(row['source_path']),
                    final_path=final_path,
                    xml_path=(
                        None if row['xml_path'] is None else str(row['xml_path'])
                    ),
                    artifact_state=str(row['artifact_state']),
                    updated_at=int(row['updated_at']),
                    identity=identity,
                )
            )
        if not parts and not finalized:
            return False
        now = int(self._clock())
        danmaku_backfill = bool(snapshot.get('danmaku_backfill'))

        def prepare(connection: sqlite3.Connection) -> bool:
            current = connection.execute(
                'SELECT state,preupload_finalized FROM upload_jobs WHERE id=?',
                (job_id,),
            ).fetchone()
            if (
                current is None
                or str(current['state']) != 'waiting_artifacts'
                or bool(current['preupload_finalized']) != finalized
            ):
                return False
            for part in parts:
                current_part = connection.execute(
                    'SELECT part_index,source_path,final_path,artifact_state,'
                    'updated_at '
                    'FROM recording_parts WHERE id=? AND session_id=?',
                    (part.id, job_session_id),
                ).fetchone()
                if current_part is None or (
                    int(current_part['part_index']),
                    str(current_part['source_path']),
                    str(current_part['final_path']),
                    str(current_part['artifact_state']),
                    int(current_part['updated_at']),
                ) != (
                    part.part_index,
                    part.source_path,
                    part.final_path,
                    part.artifact_state,
                    part.updated_at,
                ):
                    return False
                if (
                    connection.execute(
                        'SELECT 1 FROM upload_parts WHERE job_id=? AND part_index=?',
                        (job_id, part.part_index),
                    ).fetchone()
                    is not None
                ):
                    return False
                danmaku_state = 'disabled'
                if danmaku_backfill:
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
            next_state = 'ready'
            if finalized:
                pending_artifact = connection.execute(
                    'SELECT 1 FROM recording_parts WHERE session_id=? '
                    "AND artifact_state NOT IN ('ready','failed','missing') LIMIT 1",
                    (job_session_id,),
                ).fetchone()
                missing_ready = connection.execute(
                    'SELECT 1 FROM recording_parts part '
                    'LEFT JOIN upload_parts upload ON upload.job_id=? '
                    'AND upload.part_index=part.part_index '
                    "WHERE part.session_id=? AND part.artifact_state='ready' "
                    'AND part.upload_excluded_reason IS NULL '
                    'AND (part.record_duration_seconds IS NULL OR '
                    'part.record_duration_seconds>=?) '
                    'AND upload.id IS NULL LIMIT 1',
                    (job_id, job_session_id, self._MIN_UPLOAD_PART_DURATION_SECONDS),
                ).fetchone()
                if pending_artifact is not None or missing_ready is not None:
                    next_state = 'waiting_artifacts'
                part_count = int(
                    connection.execute(
                        'SELECT COUNT(*) FROM upload_parts WHERE job_id=?', (job_id,)
                    ).fetchone()[0]
                )
                if part_count == 0:
                    next_state = 'waiting_artifacts'
            if not parts and next_state == 'waiting_artifacts':
                return False
            connection.execute(
                'UPDATE upload_jobs SET state=?,updated_at=? WHERE id=?',
                (next_state, now, job_id),
            )
            return True

        prepared = await self._database.write(prepare)
        if prepared:
            audit(
                'upload_job_artifacts_ready',
                job_id=job_id,
                session_id=job_session_id,
                parts=len(parts),
                preupload=not finalized,
            )
        return prepared

    async def _cancel_empty_finalized_job(self, session_id: int, job_id: int) -> bool:
        now = int(self._clock())
        reason = '录像分段均不足 60 秒，已保留本地文件'

        def cancel(connection: sqlite3.Connection) -> bool:
            job = connection.execute(
                'SELECT state,lease_until FROM upload_jobs '
                'WHERE id=? AND session_id=? AND preupload_finalized=1',
                (job_id, session_id),
            ).fetchone()
            if (
                job is None
                or str(job['state']) != 'waiting_artifacts'
                or (job['lease_until'] is not None and int(job['lease_until']) > now)
            ):
                return False
            if connection.execute(
                'SELECT 1 FROM upload_parts WHERE job_id=? LIMIT 1', (job_id,)
            ).fetchone():
                return False
            if connection.execute(
                'SELECT 1 FROM recording_parts WHERE session_id=? '
                "AND artifact_state NOT IN ('ready','failed','missing') LIMIT 1",
                (session_id,),
            ).fetchone():
                return False
            if connection.execute(
                'SELECT 1 FROM recording_parts WHERE session_id=? '
                'AND upload_excluded_reason=? LIMIT 1',
                (session_id, self._MEDIA_PROBE_PENDING_REASON),
            ).fetchone():
                return False
            if connection.execute(
                'SELECT 1 FROM recording_parts WHERE session_id=? '
                "AND artifact_state='ready' AND (record_duration_seconds IS NULL "
                'OR record_duration_seconds>=?) '
                'AND upload_excluded_reason IS NULL LIMIT 1',
                (session_id, self._MIN_UPLOAD_PART_DURATION_SECONDS),
            ).fetchone():
                return False
            connection.execute('DELETE FROM upload_jobs WHERE id=?', (job_id,))
            connection.execute(
                "UPDATE recording_sessions SET upload_resolution_state='not_requested',"
                'upload_resolution_error=?,upload_resolved_at=? WHERE id=?',
                (reason, now, session_id),
            )
            return True

        cancelled = await self._database.write(cancel)
        if cancelled:
            audit(
                'upload_empty_finalized_job_cancelled',
                job_id=job_id,
                session_id=session_id,
                reason='all_parts_below_minimum_duration',
            )
        return cancelled

    async def _has_stable_ready_part(self, session_id: int) -> bool:
        rows = await self._database.fetchall(
            'SELECT final_path FROM recording_parts '
            "WHERE session_id=? AND artifact_state='ready' "
            'AND upload_excluded_reason IS NULL '
            'AND (record_duration_seconds IS NULL OR record_duration_seconds>=?) '
            'ORDER BY part_index',
            (session_id, self._MIN_UPLOAD_PART_DURATION_SECONDS),
        )
        stable_before_ns = int(
            (self._clock() - self._stability_seconds) * 1_000_000_000
        )
        for row in rows:
            if row['final_path'] is None:
                continue
            try:
                identity = await self._file_identity(str(row['final_path']))
            except OSError:
                continue
            if identity.mtime_ns <= stable_before_ns:
                return True
        return False

    async def _resolved_account(
        self, command: RoomUploadPolicyCommand
    ) -> Optional[sqlite3.Row]:
        if command.account_mode == 'primary':
            return await self._database.fetchone(
                'SELECT account.id,account.uid,account.credential_version '
                'FROM bili_account_selection selection '
                'JOIN bili_accounts account '
                'ON account.id=selection.primary_account_id '
                "WHERE selection.id=1 AND account.state='active'"
            )
        return await self._database.fetchone(
            'SELECT id,uid,credential_version FROM bili_accounts '
            "WHERE id=? AND state='active'",
            (command.account_id,),
        )

    async def _set_upload_resolution(
        self, session_id: int, state: str, error: Optional[str]
    ) -> None:
        now = int(self._clock())
        changed = await self._database.execute(
            'UPDATE recording_sessions SET upload_resolution_state=?,'
            'upload_resolution_error=?,upload_resolved_at=? '
            "WHERE id=? AND upload_resolution_state='pending' "
            'AND NOT EXISTS(SELECT 1 FROM upload_jobs '
            'WHERE upload_jobs.session_id=recording_sessions.id)',
            (state, error, now, session_id),
        )
        if changed:
            audit(
                'upload_session_resolved_without_job',
                session_id=session_id,
                resolution_state=state,
                resolution_error=error,
            )

    async def create_highlight_job(self, session_id: int) -> int:
        row, policy_source = await self._highlight_candidate(session_id)
        job_id = await self._create_candidate(
            row,
            initial_state='ready',
            required_source_kind='highlight',
            policy_source=policy_source,
            require_stability=False,
            return_existing=True,
        )
        if job_id is None:
            raise InvalidUploadPolicy('highlight upload draft could not be created')
        return job_id

    async def run_once(self) -> Optional[int]:
        if self._stop_requested():
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
            audit(
                'upload_job_claimed',
                level='DEBUG',
                job_id=claim.id,
                attempt=claim.attempt,
            )
            await self._process(claim)
            return claim.id

    async def build_edit_payload(
        self, job_id: int, healthy_cids: Mapping[int, int], cover_url: Optional[str]
    ) -> Mapping[str, Any]:
        row = await self._database.fetchone(
            'SELECT id,account_id,policy_snapshot_json,state,submit_state,'
            'upload_completed_at,preupload_finalized,aid '
            'FROM upload_jobs WHERE id=?',
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
            preupload_finalized=bool(row['preupload_finalized']),
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

    async def _create_candidate(
        self,
        row: Any,
        *,
        initial_state: str = 'ready',
        operator_paused: bool = False,
        operator_resume_state: Optional[str] = None,
        required_source_kind: str = 'live',
        policy_source: str = 'room',
        require_stability: bool = True,
        return_existing: bool = False,
    ) -> Optional[int]:
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
            if require_stability and identity.mtime_ns > stable_before_ns:
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
                return int(existing['id']) if return_existing else None
            session = connection.execute(
                'SELECT state,source_kind,upload_override_json '
                'FROM recording_sessions WHERE id=?',
                (int(row['session_id']),),
            ).fetchone()
            if (
                session is None
                or str(session['state']) != 'closed'
                or str(session['source_kind']) != required_source_kind
                or session['upload_override_json'] != row['upload_override_json']
            ):
                return None
            if required_source_kind == 'highlight':
                clip = connection.execute(
                    'SELECT state FROM highlight_clips WHERE upload_session_id=?',
                    (int(row['session_id']),),
                ).fetchone()
                if clip is None or str(clip['state']) != 'ready':
                    return None
            policy = connection.execute(
                'SELECT account_mode,account_id,updated_at '
                'FROM room_upload_policies WHERE room_id=?',
                (int(row['room_id']),),
            ).fetchone()
            if policy_source == 'room':
                if policy is None or int(policy['updated_at']) != int(
                    row['policy_updated_at']
                ):
                    return None
                account_mode = str(policy['account_mode'])
                policy_account_id = policy['account_id']
            elif policy_source == 'session':
                if session['upload_override_json'] is None:
                    return None
                account_mode = str(row['account_mode'])
                policy_account_id = row['account_id']
            elif policy_source == 'default':
                if policy is not None:
                    return None
                account_mode = str(row['account_mode'])
                policy_account_id = row['account_id']
            else:
                return None
            resolved_account_id = int(row['resolved_account_id'])
            if account_mode == 'fixed':
                current_account_id = policy_account_id
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
                'collection_branch_state,operator_paused,operator_resume_state,'
                'created_at,updated_at) '
                "VALUES(?,?,?,?,'prepared',?,?,?,?,?,?,?)",
                (
                    int(row['session_id']),
                    resolved_account_id,
                    snapshot_json,
                    initial_state,
                    'pending' if bool(row['auto_comment']) else 'disabled',
                    'pending' if bool(row['danmaku_backfill']) else 'disabled',
                    (
                        'pending'
                        if row['collection_section_id'] is not None
                        else 'disabled'
                    ),
                    int(operator_paused),
                    operator_resume_state,
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

        job_id = await self._database.write(create)
        if job_id is not None:
            audit(
                'upload_job_created',
                job_id=job_id,
                session_id=int(row['session_id']),
                room_id=int(row['room_id']),
                account_id=int(row['resolved_account_id']),
                parts=len(parts),
                tid=snapshot['tid'],
                is_only_self=snapshot['is_only_self'],
                publish_dynamic=snapshot['publish_dynamic'],
                collection_enabled=snapshot['collection_section_id'] is not None,
                publish_delay_seconds=snapshot['publish_delay_seconds'],
            )
        return job_id

    async def _highlight_candidate(self, session_id: int) -> Tuple[Any, str]:
        session = await self._database.fetchone(
            'SELECT id AS session_id,room_id,broadcast_session_key,'
            'live_start_time,live_end_time,title,cover_url,cover_path,anchor_uid,'
            'anchor_name,area_id,area_name,parent_area_id,parent_area_name,'
            'upload_override_json '
            "FROM recording_sessions WHERE id=? AND state='closed' "
            "AND source_kind='highlight'",
            (session_id,),
        )
        if session is None:
            raise InvalidUploadPolicy('highlight upload session does not exist')

        override_json = session['upload_override_json']
        if override_json is None:
            raise InvalidUploadPolicy(
                'highlight submission settings must be saved before upload'
            )
        try:
            command = decode_submission_settings(str(override_json))
        except InvalidSessionSubmission as error:
            raise InvalidUploadPolicy(
                'highlight submission settings are invalid'
            ) from error

        account = await self._resolved_account(command)
        if account is None:
            raise InvalidUploadPolicy('highlight upload account is unavailable')
        row = self._default_candidate(session, account, command)
        row['policy_updated_at'] = None
        row['upload_override_json'] = override_json
        return row, 'session'

    @staticmethod
    def _default_candidate(
        session: sqlite3.Row, account: sqlite3.Row, command: RoomUploadPolicyCommand
    ) -> Dict[str, Any]:
        return UploadCoordinator._command_candidate(
            dict(session), dict(account), command
        )

    @staticmethod
    def _command_candidate(
        session: Mapping[str, Any],
        account: Mapping[str, Any],
        command: RoomUploadPolicyCommand,
    ) -> Dict[str, Any]:
        copyright_value = (
            2
            if command.creation_statement_id == -2
            else 1 if command.original_authorization else 3
        )
        row: Dict[str, Any] = dict(session)
        row.update(
            {
                'account_mode': command.account_mode,
                'account_id': command.account_id,
                'title_template': command.title_template,
                'description_template': command.description_template,
                'part_title_template': command.part_title_template,
                'dynamic_template': command.dynamic_template,
                'tid': command.tid,
                'tags': command.tags,
                'creation_statement_id': command.creation_statement_id,
                'original_authorization': int(command.original_authorization),
                'copyright': copyright_value,
                'source': command.source,
                'is_only_self': int(command.is_only_self),
                'publish_dynamic': int(command.publish_dynamic),
                'no_reprint': int(
                    command.original_authorization
                    and command.creation_statement_id != -2
                ),
                'up_selection_reply': int(command.up_selection_reply),
                'up_close_reply': int(command.up_close_reply),
                'up_close_danmu': int(command.up_close_danmu),
                'auto_comment': int(command.auto_comment),
                'danmaku_backfill': int(command.danmaku_backfill),
                'filter_json': json.dumps(dict(command.filters)),
                'collection_season_id': command.collection_season_id,
                'collection_section_id': command.collection_section_id,
                'cover_mode': command.cover_mode,
                'cover_asset_id': command.cover_asset_id,
                'publish_delay_seconds': command.publish_delay_seconds,
                'policy_updated_at': None,
                'resolved_account_id': int(account['id']),
                'resolved_account_uid': int(account['uid']),
                'credential_version': int(account['credential_version']),
            }
        )
        return row

    def _policy_snapshot(
        self, row: Mapping[str, Any], parts: List[Any]
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
            'file_identities': [part.snapshot_identity for part in parts],
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
            'recording_part_indexes': [part.part_index for part in parts],
        }

    async def _process(self, claim: LeaseClaim) -> None:
        job = await self._load_job(claim)
        audit(
            'upload_job_processing',
            level='DEBUG',
            job_id=job.id,
            account_id=job.account_id,
            state=job.state,
            submit_state=job.submit_state,
            preupload=not job.preupload_finalized,
        )
        if job.state == 'submitting' and job.submit_state in (
            'in_flight',
            'unknown_outcome',
        ):
            await self._reconcile_unknown_submission(claim, job)
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
                audit(
                    'upload_parts_completed',
                    job_id=job.id,
                    account_id=job.account_id,
                    parts=len(parts),
                )
                finalized = await self._database.scalar(
                    'SELECT preupload_finalized FROM upload_jobs '
                    'WHERE id=? AND lease_owner=? AND lease_generation=?',
                    (claim.id, claim.lease_owner, claim.lease_generation),
                )
                if finalized == 0:
                    await self._update_job(
                        claim,
                        {
                            'state': 'waiting_artifacts',
                            'review_reason': None,
                            'updated_at': int(self._clock()),
                        },
                        release=True,
                    )
                    audit(
                        'upload_preupload_waiting_for_part',
                        job_id=job.id,
                        account_id=job.account_id,
                        parts=len(parts),
                    )
                    return
                if self._stop_requested():
                    raise UposUploadStopped('upload stopped before archive submission')
                payload = await self._submit_payload(job)
                if self._stop_requested():
                    raise UposUploadStopped('upload stopped before archive submission')
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
                if self._stop_requested():
                    await self._update_job(
                        claim,
                        {
                            'state': 'submitting',
                            'submit_state': 'prepared',
                            'review_reason': None,
                            'updated_at': int(self._clock()),
                        },
                        release=True,
                    )
                    audit(
                        'upload_archive_submission_stopped',
                        job_id=job.id,
                        account_id=job.account_id,
                        result='not_sent',
                    )
                    return
                submit_started = True
                audit(
                    'upload_archive_submitting',
                    job_id=job.id,
                    account_id=job.account_id,
                    parts=len(parts),
                    tid=payload.get('tid'),
                    is_only_self=bool(payload.get('is_only_self')),
                    publish_dynamic=not bool(payload.get('no_disturbance')),
                    scheduled_publish_at=scheduled_publish_at,
                )
                response = await self._protocol.submit_archive(bundle, payload)
        except DefinitelyNotSent:
            await self._retry_not_sent(claim, submit_started=submit_started)
            return
        except RemoteOutcomeUnknown:
            if submit_started:
                await self._mark_unknown_submission(
                    claim, reason='投稿结果暂未确认，系统将先查询远端稿件'
                )
            else:
                await self._retry_not_sent(claim, submit_started=False)
            return
        except UposUploadDeferred as error:
            await self._update_job(
                claim,
                {
                    'state': 'uploading',
                    'submit_state': 'prepared',
                    'review_reason': '预上传暂时受限，系统将在 {} 秒后自动重试'.format(
                        error.retry_after_seconds
                    ),
                    'next_attempt_at': (int(self._clock()) + error.retry_after_seconds),
                    'updated_at': int(self._clock()),
                },
                release=True,
            )
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
            if error.code in (406, 408, 425, 429):
                delay = min(15 * 60, 60 * (2 ** min(max(claim.attempt - 1, 0), 4)))
                await self._update_job(
                    claim,
                    {
                        'state': 'submitting' if submit_started else 'uploading',
                        'submit_state': 'prepared',
                        'review_reason': 'B 站暂时限制请求，系统将在 {} 秒后自动重试'.format(
                            delay
                        ),
                        'next_attempt_at': int(self._clock()) + delay,
                        'updated_at': int(self._clock()),
                    },
                    release=True,
                )
                audit(
                    'upload_preupload_rate_limited',
                    level='WARNING',
                    job_id=claim.id,
                    stage='submission' if submit_started else 'upload',
                    error_code=error.code,
                    delay_seconds=delay,
                )
                return
            rejection_reason = await self._bili_rejection_reason(claim.id, error)
            await self._update_job(
                claim,
                {
                    'state': 'paused',
                    'submit_state': (
                        'failed_permanent' if submit_started else 'prepared'
                    ),
                    'review_reason': rejection_reason,
                    'updated_at': int(self._clock()),
                },
                release=True,
            )
            return
        except ProtocolContractError:
            if submit_started:
                await self._mark_unknown_submission(
                    claim, reason='投稿响应无法确认，系统将先查询远端稿件'
                )
            else:
                await self._pause_job(claim, '上传协议响应不符合预期')
            return
        aid, bvid = self._submission_identity(response)
        if aid is None or bvid is None:
            await self._mark_unknown_submission(
                claim, reason='投稿响应缺少稿件编号，系统将先查询远端稿件'
            )
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
        audit(
            'upload_archive_submitted',
            job_id=job.id,
            account_id=job.account_id,
            aid=aid,
            bvid=bvid,
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
        source_indexes = snapshot.get('recording_part_indexes')
        if not isinstance(titles, list):
            raise ProtocolContractError('invalid upload policy snapshot')
        if source_indexes is None:
            title_by_part_index = {
                index + 1: title for index, title in enumerate(titles)
            }
        elif (
            isinstance(source_indexes, list)
            and len(source_indexes) == len(titles)
            and all(type(value) is int and value > 0 for value in source_indexes)
        ):
            title_by_part_index = dict(zip(source_indexes, titles))
        else:
            raise ProtocolContractError('invalid upload policy snapshot')
        videos = []
        for part in parts:
            remote_filename = part['remote_filename']
            title = title_by_part_index.get(int(part['part_index']))
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
        if format_version >= 3:
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
            'upload_completed_at,preupload_finalized '
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
            preupload_finalized=bool(row['preupload_finalized']),
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
        audit(
            'upload_job_retry_scheduled',
            level='WARNING',
            job_id=claim.id,
            stage='submission' if submit_started else 'upload',
            delay_seconds=delay,
        )

    async def _bili_rejection_reason(self, job_id: int, error: BiliApiError) -> str:
        checks = error.details.get('bvc_check')
        if error.code == 21588 and isinstance(checks, list):
            part_rows = await self._database.fetchall(
                'SELECT part_index,cid FROM upload_parts '
                'WHERE job_id=? AND cid IS NOT NULL',
                (job_id,),
            )
            parts_by_cid = {
                int(row['cid']): int(row['part_index']) for row in part_rows
            }
            messages = []
            for check in checks:
                if not isinstance(check, Mapping):
                    continue
                cid = check.get('cid')
                message = check.get('message')
                if type(cid) is not int or not isinstance(message, str):
                    continue
                part_index = parts_by_cid.get(cid)
                label = (
                    'P{}'.format(part_index)
                    if part_index is not None
                    else 'CID {}'.format(cid)
                )
                messages.append('{} {}'.format(label, message))
            if messages:
                return ('B 站视频检测未通过：' + '；'.join(messages))[:500]
            return 'B 站视频检测未通过（21588），未返回可匹配的分 P 原因'
        if error.public_message:
            return 'B 站接口拒绝请求（{}）：{}'.format(
                error.code, error.public_message
            )[:500]
        return 'B 站接口拒绝请求（{}）'.format(error.code)

    async def _mark_unknown_submission(self, claim: LeaseClaim, *, reason: str) -> None:
        delay = min(15 * 60, 60 * (2 ** min(max(claim.attempt - 1, 0), 4)))
        await self._update_job(
            claim,
            {
                'state': 'submitting',
                'submit_state': 'unknown_outcome',
                'review_reason': '{}（{} 秒后核对）'.format(reason, delay),
                'next_attempt_at': int(self._clock()) + delay,
                'updated_at': int(self._clock()),
            },
            release=True,
        )
        audit(
            'upload_submission_reconciliation_scheduled',
            level='WARNING',
            job_id=claim.id,
            delay_seconds=delay,
            reason=reason,
        )

    async def _reconcile_unknown_submission(self, claim: LeaseClaim, job: _Job) -> None:
        account = await self._database.fetchone(
            'SELECT state,credential_version FROM bili_accounts WHERE id=?',
            (job.account_id,),
        )
        if account is None or str(account['state']) != 'active':
            await self._pause_job(claim, '投稿账号不可用，无法核对投稿结果')
            return
        try:
            gate = self._account_gates.for_account(job.account_id)
            async with gate.hold(int(account['credential_version'])):
                bundle = await self._bundle_loader(job.account_id)
                matches = await self._find_remote_submission(job, bundle)
        except (AccountNotFound, AccountPaused, CredentialVersionChanged):
            await self._pause_job(claim, '投稿账号在核对期间发生变化')
            return
        except (CredentialNotFound, InvalidCredentialBundle, InvalidCredentialKey):
            await self._pause_job(claim, '投稿账号凭据无法读取')
            return
        except (
            BiliApiError,
            DefinitelyNotSent,
            ProtocolContractError,
            RemoteOutcomeUnknown,
        ) as error:
            await self._mark_unknown_submission(
                claim, reason='远端稿件暂时无法查询：{}'.format(type(error).__name__)
            )
            return
        if matches:
            aid, bvid = matches[0]
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
            audit(
                'upload_submission_reconciled',
                level='WARNING' if len(matches) > 1 else 'INFO',
                job_id=claim.id,
                aid=aid,
                bvid=bvid,
                matching_archives=len(matches),
            )
            return

        await self._mark_unknown_submission(
            claim, reason='近期稿件中暂未找到匹配项，继续核对且不会盲目重复投稿'
        )

    async def _find_remote_submission(
        self, job: _Job, bundle: Any
    ) -> List[Tuple[int, str]]:
        try:
            snapshot = json.loads(job.policy_snapshot_json)
        except json.JSONDecodeError:
            raise ProtocolContractError('invalid upload policy snapshot') from None
        if not isinstance(snapshot, Mapping) or not isinstance(
            snapshot.get('title'), str
        ):
            raise ProtocolContractError('invalid upload policy snapshot')
        expected_title = str(snapshot['title'])
        rows = await self._database.fetchall(
            'SELECT remote_filename FROM upload_parts '
            'WHERE job_id=? ORDER BY part_index',
            (job.id,),
        )
        expected_filenames = tuple(str(row['remote_filename'] or '') for row in rows)
        if not expected_filenames or any(not name for name in expected_filenames):
            raise ProtocolContractError('upload part is incomplete')

        candidates: List[Tuple[int, str]] = []
        for page_number in range(1, 21):
            response = await self._protocol.list_archives(
                bundle,
                {'status': 'is_pubing,pubed,not_pubed', 'pn': page_number, 'ps': 50},
            )
            entries = self._archive_entries(response)
            for entry in entries:
                archive = self._archive(entry)
                if archive.get('title') != expected_title:
                    continue
                identity = self._archive_identity(archive)
                if identity is not None:
                    candidates.append(identity)
            if len(entries) < 50:
                break

        matches: List[Tuple[int, str]] = []
        seen = set()
        for aid, bvid in candidates:
            if bvid in seen:
                continue
            seen.add(bvid)
            detail = await self._protocol.archive_view(
                bundle, {'topic_grey': 1, 'bvid': bvid, 't': int(self._clock() * 1000)}
            )
            data = detail.get('data')
            if not isinstance(data, Mapping):
                continue
            detail_identity = self._archive_identity(self._archive(data))
            videos = data.get('videos')
            if detail_identity != (aid, bvid) or not isinstance(videos, list):
                continue
            filenames = tuple(
                str(video.get('filename') or '')
                for video in videos
                if isinstance(video, Mapping)
            )
            if filenames == expected_filenames:
                matches.append((aid, bvid))
        return matches

    @staticmethod
    def _archive_entries(response: Mapping[str, Any]) -> List[Mapping[str, Any]]:
        data = response.get('data')
        entries = data.get('arc_audits') if isinstance(data, Mapping) else None
        if not isinstance(entries, list) or not all(
            isinstance(entry, Mapping) for entry in entries
        ):
            raise ProtocolContractError('archive list response is invalid')
        return list(entries)

    @staticmethod
    def _archive(value: Mapping[str, Any]) -> Mapping[str, Any]:
        archive = value.get('Archive')
        if not isinstance(archive, Mapping):
            archive = value.get('archive')
        return archive if isinstance(archive, Mapping) else value

    @staticmethod
    def _archive_identity(value: Mapping[str, Any]) -> Optional[Tuple[int, str]]:
        raw_aid = value.get('aid')
        if type(raw_aid) is int:
            aid = raw_aid
        elif isinstance(raw_aid, str) and raw_aid.isdigit():
            aid = int(raw_aid)
        else:
            return None
        bvid = value.get('bvid')
        if aid <= 0 or not isinstance(bvid, str) or not bvid:
            return None
        return aid, bvid

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
        audit('upload_job_paused', level='WARNING', job_id=claim.id, reason=reason)

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
