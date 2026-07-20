from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import time
import uuid
import xml.etree.ElementTree as ElementTree
from dataclasses import dataclass
from typing import (
    TYPE_CHECKING,
    Awaitable,
    Callable,
    Dict,
    List,
    Literal,
    Optional,
    Sequence,
    Tuple,
    TypeVar,
)

from blrec.logging.audit import audit

from .artifact_recovery import RecoveredArtifact, probe_recording_artifact
from .database import BiliUploadDatabase

UploadJobDisplayState = Literal[
    'standard', 'preuploading', 'preuploaded_waiting', 'preupload_paused'
]

if TYPE_CHECKING:
    from blrec.core.recorder import Recorder
    from blrec.postprocess.postprocessor import Postprocessor


class JournalConsistencyError(RuntimeError):
    pass


@dataclass(frozen=True)
class RecordingSessionMetadata:
    title: str
    cover_url: str
    anchor_uid: int
    anchor_name: str
    area_id: int
    area_name: str
    parent_area_id: int
    parent_area_name: str


@dataclass(frozen=True)
class RecordingPart:
    id: int
    session_id: int
    run_id: str
    part_index: int
    source_path: str
    final_path: Optional[str]
    xml_path: Optional[str]
    record_start_time: int
    artifact_state: str
    xml_completed: bool
    source_exists: bool
    final_exists: bool
    error_message: Optional[str]
    upload_excluded_reason: Optional[str] = None
    record_end_time: Optional[int] = None
    record_duration_seconds: Optional[int] = None
    file_size_bytes: Optional[int] = None
    danmaku_count: int = 0
    media_index_state: str = 'pending'
    media_index_error: Optional[str] = None
    media_index_progress: float = 0.0


@dataclass(frozen=True)
class ActiveRecordingPart:
    id: int
    part_index: int
    artifact_state: str


@dataclass(frozen=True)
class RecordingSession:
    id: int
    room_id: int
    broadcast_session_key: str
    live_start_time: Optional[int]
    state: str
    started_at: int
    ended_at: Optional[int]
    title: str = ''
    cover_url: str = ''
    cover_path: Optional[str] = None
    anchor_uid: Optional[int] = None
    anchor_name: str = ''
    area_id: Optional[int] = None
    area_name: str = ''
    parent_area_id: Optional[int] = None
    parent_area_name: str = ''
    live_end_time: Optional[int] = None
    upload_intent: str = 'none'
    upload_decision: str = 'follow_room'
    submission_inherited: bool = True
    upload_resolution_state: str = 'pending'
    upload_resolution_error: Optional[str] = None
    upload_suppressed: bool = False
    deletion_state: str = 'none'
    deletion_error: Optional[str] = None
    source_kind: str = 'live'
    highlight_clip_id: Optional[int] = None
    parts: Tuple[RecordingPart, ...] = ()

    @property
    def part_count(self) -> int:
        return len(self.parts)

    @property
    def danmaku_count(self) -> int:
        return sum(part.danmaku_count for part in self.parts)

    @property
    def total_file_size_bytes(self) -> int:
        return sum(part.file_size_bytes or 0 for part in self.parts)

    @property
    def record_duration_seconds(self) -> int:
        return sum(part.record_duration_seconds or 0 for part in self.parts)


@dataclass(frozen=True)
class UploadPartProgress:
    id: int
    job_id: int
    part_index: int
    upload_state: str
    danmaku_import_state: str
    remote_filename: Optional[str]
    cid: Optional[int]
    transcode_state: str = 'unknown'
    transcode_fail_code: Optional[int] = None
    transcode_fail_desc: Optional[str] = None
    repair_stage: str = 'none'
    repair_diagnostic: Optional[str] = None
    confirmed_bytes: int = 0
    total_bytes: int = 0


@dataclass(frozen=True)
class DanmakuItemProgress:
    id: int
    part_index: int
    progress_ms: int
    content: str
    error_message: Optional[str]


@dataclass(frozen=True)
class UploadJobProgress:
    id: int
    session_id: int
    account_id: int
    account_uid: int
    account_display_name: str
    state: str
    submit_state: str
    comment_branch_state: str
    danmaku_branch_state: str
    aid: Optional[int]
    bvid: Optional[str]
    review_reason: Optional[str]
    attempt: int
    next_attempt_at: int
    created_at: int
    updated_at: int
    parts: Tuple[UploadPartProgress, ...]
    danmaku_total: int = 0
    danmaku_confirmed: int = 0
    danmaku_pending: int = 0
    danmaku_unknown: int = 0
    danmaku_failed: int = 0
    unknown_danmaku_items: Tuple[DanmakuItemProgress, ...] = ()
    repair_state: str = 'idle'
    repair_message: Optional[str] = None
    repair_error: Optional[str] = None
    can_retry: bool = False
    can_repair: bool = False
    can_skip: bool = False
    can_repost: bool = False
    can_delete: bool = False
    operator_paused: bool = False
    scheduled_publish_at: Optional[int] = None
    collection_branch_state: str = 'disabled'
    collection_error: Optional[str] = None
    submission_verification_state: str = 'pending'
    submission_verified_at: Optional[int] = None
    submission_verification: Optional[Dict[str, object]] = None
    comment_error: Optional[str] = None
    danmaku_error: Optional[str] = None
    can_pause: bool = False
    can_resume: bool = False
    can_edit: bool = False
    confirmed_bytes: int = 0
    total_bytes: int = 0
    percent: float = 0.0
    bytes_per_second: Optional[float] = None
    eta_seconds: Optional[int] = None
    current_part_index: Optional[int] = None
    preupload_finalized: bool = True
    display_state: UploadJobDisplayState = 'standard'
    title: str = ''


@dataclass(frozen=True)
class UploadJobSummary:
    id: int
    session_id: int
    account_id: int
    account_uid: int
    account_display_name: str
    state: str
    submit_state: str
    comment_branch_state: str
    danmaku_branch_state: str
    aid: Optional[int]
    bvid: Optional[str]
    review_reason: Optional[str]
    attempt: int
    next_attempt_at: int
    created_at: int
    updated_at: int
    danmaku_total: int
    danmaku_confirmed: int
    danmaku_pending: int
    danmaku_unknown: int
    danmaku_failed: int
    repair_state: str
    repair_message: Optional[str]
    repair_error: Optional[str]
    can_retry: bool
    can_repair: bool
    can_skip: bool
    can_repost: bool
    can_delete: bool
    operator_paused: bool
    scheduled_publish_at: Optional[int]
    collection_branch_state: str
    collection_error: Optional[str]
    submission_verification_state: str
    submission_verified_at: Optional[int]
    comment_error: Optional[str]
    danmaku_error: Optional[str]
    can_pause: bool
    can_resume: bool
    can_edit: bool
    can_backfill_danmaku: bool
    confirmed_bytes: int
    total_bytes: int
    percent: float
    bytes_per_second: Optional[float]
    eta_seconds: Optional[int]
    current_part_index: Optional[int]
    confirmed_part_count: int
    discovered_part_count: int
    preupload_finalized: bool
    display_state: UploadJobDisplayState
    title: str = ''


@dataclass(frozen=True)
class RecordingSessionSummary:
    id: int
    room_id: int
    live_start_time: Optional[int]
    state: str
    started_at: int
    ended_at: Optional[int]
    title: str
    cover_url: str
    anchor_uid: Optional[int]
    anchor_name: str
    area_id: Optional[int]
    area_name: str
    parent_area_id: Optional[int]
    parent_area_name: str
    live_end_time: Optional[int]
    part_count: int
    danmaku_count: int
    total_file_size_bytes: int
    record_duration_seconds: int
    upload_intent: str
    upload_decision: str
    submission_inherited: bool
    upload_resolution_state: str
    upload_resolution_error: Optional[str]
    upload_suppressed: bool
    deletion_state: str
    deletion_error: Optional[str]
    source_kind: str
    highlight_clip_id: Optional[int]
    upload_job: Optional[UploadJobSummary]


@dataclass(frozen=True)
class _ArtifactRecoveryDecision:
    artifact: Optional[RecoveredArtifact]
    any_path_exists: bool
    used_source: bool
    existing_path: Optional[str]


@dataclass(frozen=True)
class _RecordingOwnerState:
    session_id: int
    room_id: int
    source_generation: int
    cancelled: bool


_T = TypeVar('_T')


class RecordingJournalBridge:
    def __init__(
        self,
        database: BiliUploadDatabase,
        *,
        clock: Callable[[], float] = time.time,
        uuid_factory: Callable[[], str] = lambda: str(uuid.uuid4()),
        artifact_probe: Callable[
            [str], Optional[RecoveredArtifact]
        ] = probe_recording_artifact,
    ) -> None:
        self._database = database
        self._clock = clock
        self._uuid_factory = uuid_factory
        self._artifact_probe = artifact_probe
        self._degraded_reason: Optional[str] = None
        self._upload_speed_samples: Dict[int, Tuple[float, int]] = {}

    @property
    def degraded_reason(self) -> Optional[str]:
        return self._degraded_reason

    def pause_automation(self, error: BaseException) -> None:
        self._degraded_reason = '{}: {}'.format(type(error).__name__, error)

    async def recording_started(
        self,
        room_id: int,
        *,
        live_start_time: int,
        metadata: Optional[RecordingSessionMetadata] = None,
        event_id: Optional[str] = None,
    ) -> str:
        now = int(self._clock())
        run_id = self._uuid_factory()
        journal_id = self._new_event_id(event_id)

        def write(connection: sqlite3.Connection) -> str:
            replayed = connection.execute(
                'SELECT event_type,run_id FROM event_journal WHERE id=?', (journal_id,)
            ).fetchone()
            if replayed is not None:
                if (
                    replayed['event_type'] != 'recording_started'
                    or not replayed['run_id']
                ):
                    raise JournalConsistencyError(
                        "event '{}' has conflicting content".format(journal_id)
                    )
                return str(replayed['run_id'])
            if live_start_time > 0:
                row = connection.execute(
                    'SELECT id,broadcast_session_key,cancellation_generation '
                    'FROM recording_sessions '
                    'WHERE room_id=? AND live_start_time=? '
                    "AND source_kind='live' "
                    "AND state IN ('open','cancelled') "
                    "AND deletion_state='none' "
                    'AND NOT EXISTS(SELECT 1 FROM upload_jobs '
                    'WHERE upload_jobs.session_id=recording_sessions.id) '
                    'ORDER BY id DESC LIMIT 1',
                    (room_id, live_start_time),
                ).fetchone()
                if row is not None:
                    key = str(row['broadcast_session_key'])
                else:
                    base_key = '{}:{}'.format(room_id, live_start_time)
                    existing = connection.execute(
                        'SELECT 1 FROM recording_sessions '
                        'WHERE broadcast_session_key=?',
                        (base_key,),
                    ).fetchone()
                    key = (
                        base_key
                        if existing is None
                        else '{}:continuation:{}'.format(base_key, self._uuid_factory())
                    )
            else:
                row = connection.execute(
                    'SELECT id,broadcast_session_key,cancellation_generation '
                    'FROM recording_sessions '
                    'WHERE room_id=? AND live_start_time IS NULL '
                    "AND source_kind='live' AND state=? AND deletion_state='none' "
                    'ORDER BY id DESC LIMIT 1',
                    (room_id, 'open'),
                ).fetchone()
                key = (
                    '{}:local:{}'.format(room_id, self._uuid_factory())
                    if row is None
                    else str(row['broadcast_session_key'])
                )
            if row is None:
                cursor = connection.execute(
                    'INSERT INTO recording_sessions('
                    'room_id,broadcast_session_key,live_start_time,state,started_at,'
                    'title,cover_url,anchor_uid,anchor_name,area_id,area_name,'
                    'parent_area_id,parent_area_name,upload_intent) '
                    'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    (
                        room_id,
                        key,
                        live_start_time or None,
                        'open',
                        now,
                        '' if metadata is None else metadata.title,
                        '' if metadata is None else metadata.cover_url,
                        None if metadata is None else metadata.anchor_uid,
                        '' if metadata is None else metadata.anchor_name,
                        None if metadata is None else metadata.area_id,
                        '' if metadata is None else metadata.area_name,
                        None if metadata is None else metadata.parent_area_id,
                        '' if metadata is None else metadata.parent_area_name,
                        'none',
                    ),
                )
                session_id = int(cursor.lastrowid)
                cancellation_generation = 0
            else:
                session_id = int(row['id'])
                cancellation_generation = int(row['cancellation_generation'])
                updated = connection.execute(
                    "UPDATE recording_sessions SET state='open',ended_at=NULL,"
                    'live_end_time=NULL,'
                    "title=CASE WHEN title='' THEN ? ELSE title END,"
                    "cover_url=CASE WHEN cover_url='' THEN ? ELSE cover_url END,"
                    'anchor_uid=COALESCE(anchor_uid,?),'
                    "anchor_name=CASE WHEN anchor_name='' THEN ? ELSE anchor_name END,"
                    'area_id=COALESCE(area_id,?),'
                    "area_name=CASE WHEN area_name='' THEN ? ELSE area_name END,"
                    'parent_area_id=COALESCE(parent_area_id,?),'
                    'parent_area_name=CASE WHEN parent_area_name=\'\' THEN ? '
                    "ELSE parent_area_name END WHERE id=? AND deletion_state='none' "
                    'AND cancellation_generation=?',
                    (
                        '' if metadata is None else metadata.title,
                        '' if metadata is None else metadata.cover_url,
                        None if metadata is None else metadata.anchor_uid,
                        '' if metadata is None else metadata.anchor_name,
                        None if metadata is None else metadata.area_id,
                        '' if metadata is None else metadata.area_name,
                        None if metadata is None else metadata.parent_area_id,
                        '' if metadata is None else metadata.parent_area_name,
                        session_id,
                        cancellation_generation,
                    ),
                )
                if updated.rowcount != 1:
                    raise JournalConsistencyError(
                        "recording session '{}' changed while starting".format(
                            session_id
                        )
                    )
            connection.execute(
                'INSERT INTO recording_runs(id,session_id,state,started_at) '
                "VALUES(?,?,'recording',?)",
                (run_id, session_id, now),
            )
            self._insert_event(
                connection,
                journal_id,
                'recording_started',
                room_id,
                run_id,
                None,
                {
                    'live_start_time': live_start_time,
                    'cancellation_generation': cancellation_generation,
                },
                now,
            )
            return run_id

        persisted_run_id = await self._database.write(write)
        audit(
            'recording_started',
            room_id=room_id,
            run_id=persisted_run_id,
            live_start_time=live_start_time,
            result='journaled',
        )
        return persisted_run_id

    async def cover_downloaded(
        self, run_id: str, path: str, *, event_id: Optional[str] = None
    ) -> None:
        now = int(self._clock())
        cover_path = self._normalize_path(path)
        journal_id = self._new_event_id(event_id)

        def write(connection: sqlite3.Connection) -> None:
            if self._event_was_recorded(connection, journal_id, 'cover_downloaded'):
                return
            owner = self._recording_owner_state(connection, run_id)
            connection.execute(
                'UPDATE recording_sessions SET cover_path=COALESCE(cover_path,?) '
                'WHERE id=?',
                (cover_path, owner.session_id),
            )
            if owner.cancelled:
                self._record_local_handoff(
                    connection,
                    owner=owner,
                    run_id=run_id,
                    event_type='cover_downloaded',
                    event_id=journal_id,
                    now=now,
                )
            self._insert_event(
                connection,
                journal_id,
                'cover_downloaded',
                owner.room_id,
                run_id,
                cover_path,
                {},
                now,
            )

        await self._database.write(write)
        audit(
            'recording_cover_downloaded',
            run_id=run_id,
            path=cover_path,
            result='journaled',
        )

    async def reconcile_open_sessions(self) -> None:
        now = int(self._clock())

        sessions = await self._database.fetchall(
            'SELECT id,deletion_state FROM recording_sessions '
            "WHERE source_kind='live' "
            "AND state IN ('open','cancelled','manual_review') "
            "AND (deletion_state='none' OR EXISTS("
            'SELECT 1 FROM recording_parts part '
            'WHERE part.session_id=recording_sessions.id '
            "AND part.artifact_state='postprocessing'))"
        )
        recoveries: Dict[int, _ArtifactRecoveryDecision] = {}
        loop = asyncio.get_running_loop()
        for session in sessions:
            deleting = str(session['deletion_state']) != 'none'
            parts = await self._database.fetchall(
                'SELECT id,source_path,final_path,artifact_state '
                'FROM recording_parts WHERE session_id=? '
                + ("AND artifact_state='postprocessing'" if deleting else ''),
                (int(session['id']),),
            )
            for part in parts:
                final_path = (
                    None if part['final_path'] is None else str(part['final_path'])
                )
                if deleting and final_path is None:
                    final_path = self._derived_postprocess_path(
                        str(part['source_path'])
                    )
                decision = await loop.run_in_executor(
                    None, self._recover_artifact, str(part['source_path']), final_path
                )
                if (
                    str(part['artifact_state']) == 'ready'
                    and decision.artifact is not None
                    and decision.artifact.path == final_path
                ):
                    continue
                recoveries[int(part['id'])] = decision

        def write(connection: sqlite3.Connection) -> None:
            sessions = connection.execute(
                'SELECT id,state,deletion_state FROM recording_sessions '
                "WHERE source_kind='live' "
                "AND state IN ('open','cancelled','manual_review') "
                "AND (deletion_state='none' OR EXISTS("
                'SELECT 1 FROM recording_parts part '
                'WHERE part.session_id=recording_sessions.id '
                "AND part.artifact_state='postprocessing'))"
            ).fetchall()
            for session in sessions:
                session_id = int(session['id'])
                original_state = str(session['state'])
                deleting = str(session['deletion_state']) != 'none'
                stale_run_count = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM recording_runs WHERE session_id=? "
                        "AND state='recording'",
                        (session_id,),
                    ).fetchone()[0]
                )
                connection.execute(
                    "UPDATE recording_runs SET state='cancelled',ended_at=? "
                    "WHERE session_id=? AND state='recording'",
                    (now, session_id),
                )

                parts = connection.execute(
                    'SELECT id,run_id,source_path,final_path,record_start_time,'
                    'artifact_state '
                    'FROM recording_parts WHERE session_id=?',
                    (session_id,),
                ).fetchall()
                for part in parts:
                    decision = recoveries.get(int(part['id']))
                    if decision is None:
                        continue
                    artifact = decision.artifact
                    if artifact is not None:
                        duration = artifact.duration_seconds
                        if duration is None:
                            duration = max(0, now - int(part['record_start_time']))
                        record_end_time = int(part['record_start_time']) + duration
                        message = (
                            '录制异常中断，已自动恢复原始文件'
                            if decision.used_source
                            else '录制异常中断，已自动恢复成品文件'
                        )
                        owned_final_path = artifact.path
                        if (
                            deleting
                            and decision.existing_path is not None
                            and decision.existing_path != str(part['source_path'])
                        ):
                            owned_final_path = decision.existing_path
                        connection.execute(
                            'UPDATE recording_parts SET artifact_state=?,final_path=?,'
                            'file_size_bytes=?,record_end_time=?,'
                            'record_duration_seconds=?,source_completed_at='
                            'COALESCE(source_completed_at,?),postprocessed_at='
                            'COALESCE(postprocessed_at,?),error_message=?,updated_at=? '
                            'WHERE id=?',
                            (
                                'failed' if deleting else 'ready',
                                owned_final_path,
                                artifact.size_bytes,
                                record_end_time,
                                duration,
                                now,
                                now,
                                (
                                    '本地删除已请求，启动时已收敛后处理文件'
                                    if deleting
                                    else message
                                ),
                                now,
                                int(part['id']),
                            ),
                        )
                    else:
                        state = 'failed' if decision.any_path_exists else 'missing'
                        message = (
                            '录制异常中断，文件无法解析，已自动排除'
                            if decision.any_path_exists
                            else '录制异常中断，文件缺失，已自动排除'
                        )
                        connection.execute(
                            'UPDATE recording_parts SET artifact_state=?,'
                            'final_path=?,file_size_bytes=NULL,'
                            'error_message=?,updated_at=? WHERE id=?',
                            (
                                state,
                                (
                                    decision.existing_path
                                    if deleting
                                    and decision.existing_path
                                    != str(part['source_path'])
                                    else None
                                ),
                                message,
                                now,
                                int(part['id']),
                            ),
                        )

                    if deleting:
                        owner = self._recording_owner_state(
                            connection, str(part['run_id'])
                        )
                        self._record_local_handoff(
                            connection,
                            owner=owner,
                            run_id=str(part['run_id']),
                            event_type='postprocessor_startup_recovery',
                            event_id='part-{}'.format(int(part['id'])),
                            now=now,
                        )

                if deleting:
                    continue
                if original_state == 'cancelled':
                    continue
                part_states = {
                    str(row['artifact_state'])
                    for row in connection.execute(
                        'SELECT artifact_state FROM recording_parts '
                        'WHERE session_id=?',
                        (session_id,),
                    ).fetchall()
                }
                if stale_run_count:
                    state = 'cancelled'
                else:
                    run_states = {
                        str(row['state'])
                        for row in connection.execute(
                            'SELECT state FROM recording_runs WHERE session_id=?',
                            (session_id,),
                        ).fetchall()
                    }
                    if 'ready' in part_states and part_states <= {
                        'ready',
                        'failed',
                        'missing',
                    }:
                        state = 'closed'
                    elif part_states <= {'failed', 'missing'}:
                        state = 'skipped'
                    else:
                        state = 'cancelled' if 'cancelled' in run_states else 'open'
                connection.execute(
                    'UPDATE recording_sessions SET state=?,ended_at=? WHERE id=?',
                    (state, now, session_id),
                )

        await self._database.write(write)
        audit(
            'recording_recovery_reconciled',
            sessions=len(sessions),
            recovered_parts=len(recoveries),
            result='completed',
        )

    async def finalize_cancelled_sessions(self, *, grace_seconds: int = 600) -> int:
        if grace_seconds < 0:
            raise ValueError('resume grace must not be negative')
        now = int(self._clock())
        cutoff = now - grace_seconds

        def write(connection: sqlite3.Connection) -> int:
            sessions = connection.execute(
                'SELECT id FROM recording_sessions '
                "WHERE source_kind='live' AND state='cancelled' "
                "AND deletion_state='none' "
                'AND ended_at IS NOT NULL AND ended_at<=?',
                (cutoff,),
            ).fetchall()
            finalized = 0
            for session in sessions:
                session_id = int(session['id'])
                active_runs = int(
                    connection.execute(
                        'SELECT COUNT(*) FROM recording_runs '
                        "WHERE session_id=? AND state='recording'",
                        (session_id,),
                    ).fetchone()[0]
                )
                if active_runs:
                    continue
                states = {
                    str(row['artifact_state'])
                    for row in connection.execute(
                        'SELECT artifact_state FROM recording_parts '
                        'WHERE session_id=?',
                        (session_id,),
                    ).fetchall()
                }
                if not states <= {'ready', 'failed', 'missing'}:
                    continue
                state = 'closed' if 'ready' in states else 'skipped'
                updated = connection.execute(
                    'UPDATE recording_sessions SET state=?, '
                    'live_end_time=COALESCE(live_end_time,ended_at) '
                    "WHERE id=? AND state='cancelled' AND deletion_state='none'",
                    (state, session_id),
                ).rowcount
                finalized += int(updated)
            return finalized

        return await self._database.write(write)

    def _recover_artifact(
        self, source_path: str, final_path: Optional[str]
    ) -> _ArtifactRecoveryDecision:
        candidates = []
        if final_path:
            candidates.append((final_path, False))
        if not final_path or source_path != final_path:
            candidates.append((source_path, True))
        any_path_exists = False
        existing_path: Optional[str] = None
        for path, used_source in candidates:
            if not os.path.exists(path):
                continue
            any_path_exists = True
            if existing_path is None:
                existing_path = path
            try:
                artifact = self._artifact_probe(path)
            except Exception:
                artifact = None
            if artifact is not None:
                return _ArtifactRecoveryDecision(
                    artifact=artifact,
                    any_path_exists=True,
                    used_source=used_source,
                    existing_path=existing_path,
                )
        return _ArtifactRecoveryDecision(
            artifact=None,
            any_path_exists=any_path_exists,
            used_source=True,
            existing_path=existing_path,
        )

    @staticmethod
    def _derived_postprocess_path(source_path: str) -> Optional[str]:
        stem, extension = os.path.splitext(source_path)
        if extension.lower() not in ('.flv', '.m4s'):
            return None
        return '{}.mp4'.format(stem)

    async def video_created(
        self,
        run_id: str,
        path: str,
        *,
        record_start_time: int,
        event_id: Optional[str] = None,
    ) -> None:
        clock_now = self._clock()
        now = int(clock_now)
        timeline_start_at_ms = int(clock_now * 1000)
        source_path = self._normalize_path(path)
        journal_id = self._new_event_id(event_id)

        def write(connection: sqlite3.Connection) -> None:
            if self._event_was_recorded(connection, journal_id, 'video_created'):
                return
            owner = self._recording_owner_state(connection, run_id)
            session_id = owner.session_id
            existing = connection.execute(
                'SELECT id FROM recording_parts WHERE run_id=? AND source_path=?',
                (run_id, source_path),
            ).fetchone()
            if existing is None:
                part_index = int(
                    connection.execute(
                        'SELECT COALESCE(MAX(part_index),0)+1 '
                        'FROM recording_parts WHERE session_id=?',
                        (session_id,),
                    ).fetchone()[0]
                )
                connection.execute(
                    'INSERT INTO recording_parts('
                    'session_id,run_id,part_index,source_path,record_start_time,'
                    'timeline_start_at_ms,artifact_state,error_message,created_at,'
                    'updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)',
                    (
                        session_id,
                        run_id,
                        part_index,
                        source_path,
                        int(record_start_time),
                        timeline_start_at_ms,
                        'failed' if owner.cancelled else 'recording',
                        '本地删除已请求' if owner.cancelled else None,
                        now,
                        now,
                    ),
                )
            elif owner.cancelled:
                connection.execute(
                    "UPDATE recording_parts SET artifact_state='failed',"
                    "error_message='本地删除已请求',updated_at=? WHERE id=?",
                    (now, int(existing['id'])),
                )
            if owner.cancelled:
                self._record_local_handoff(
                    connection,
                    owner=owner,
                    run_id=run_id,
                    event_type='video_created',
                    event_id=journal_id,
                    now=now,
                )
            self._insert_event(
                connection,
                journal_id,
                'video_created',
                owner.room_id,
                run_id,
                source_path,
                {
                    'record_start_time': int(record_start_time),
                    'timeline_start_at_ms': timeline_start_at_ms,
                },
                now,
            )

        await self._database.write(write)
        audit(
            'recording_part_created',
            run_id=run_id,
            path=source_path,
            record_start_time=record_start_time,
            timeline_start_at_ms=timeline_start_at_ms,
            result='journaled',
        )

    async def video_completed(
        self, run_id: str, path: str, *, event_id: Optional[str] = None
    ) -> None:
        now = int(self._clock())
        source_path = self._normalize_path(path)
        journal_id = self._new_event_id(event_id)

        def write(connection: sqlite3.Connection) -> None:
            if self._event_was_recorded(connection, journal_id, 'video_completed'):
                return
            owner = self._recording_owner_state(connection, run_id)
            cursor = connection.execute(
                'UPDATE recording_parts SET artifact_state=?,source_completed_at=?,'
                'record_end_time=?,record_duration_seconds='
                'MAX(0,?-record_start_time),error_message=?,updated_at=? '
                'WHERE run_id=? AND source_path=?',
                (
                    'postprocessing',
                    now,
                    now,
                    now,
                    '本地删除已请求' if owner.cancelled else None,
                    now,
                    run_id,
                    source_path,
                ),
            )
            if cursor.rowcount != 1:
                raise JournalConsistencyError(
                    "unknown recording part '{}'".format(path)
                )
            self._insert_event(
                connection,
                journal_id,
                'video_completed',
                owner.room_id,
                run_id,
                source_path,
                {},
                now,
            )

        await self._database.write(write)
        audit(
            'recording_part_completed',
            run_id=run_id,
            path=source_path,
            result='journaled',
        )

    async def video_postprocessed(
        self,
        run_id: str,
        source_path: str,
        final_path: str,
        *,
        event_id: Optional[str] = None,
    ) -> None:
        now = int(self._clock())
        source = self._normalize_path(source_path)
        final = self._normalize_path(final_path)
        journal_id = self._new_event_id(event_id)
        loop = asyncio.get_running_loop()
        file_size_bytes = await loop.run_in_executor(
            None, self._file_size_or_none, final
        )

        def write(connection: sqlite3.Connection) -> None:
            if self._event_was_recorded(connection, journal_id, 'video_postprocessed'):
                return
            owner = self._recording_owner_state(connection, run_id)
            cursor = connection.execute(
                'UPDATE recording_parts SET artifact_state=?,final_path=?,'
                'file_size_bytes=?,error_message=?,postprocessed_at=?,updated_at=? '
                'WHERE run_id=? AND source_path=?',
                (
                    'failed' if owner.cancelled else 'ready',
                    final,
                    file_size_bytes,
                    '本地删除已请求' if owner.cancelled else None,
                    now,
                    now,
                    run_id,
                    source,
                ),
            )
            if cursor.rowcount != 1:
                raise JournalConsistencyError(
                    "unknown recording part '{}'".format(source_path)
                )
            if owner.cancelled:
                self._record_local_handoff(
                    connection,
                    owner=owner,
                    run_id=run_id,
                    event_type='video_postprocessed',
                    event_id=journal_id,
                    now=now,
                )
            else:
                self._refresh_session_state(connection, owner.session_id, now)
            self._insert_event(
                connection,
                journal_id,
                'video_postprocessed',
                owner.room_id,
                run_id,
                final,
                {'source_path': source},
                now,
            )

        await self._database.write(write)
        audit(
            'recording_part_postprocessed',
            run_id=run_id,
            source_path=source,
            final_path=final,
            file_size_bytes=file_size_bytes,
            result='ready',
        )

    async def recording_cancelled(
        self, run_id: str, *, event_id: Optional[str] = None
    ) -> None:
        now = int(self._clock())
        journal_id = self._new_event_id(event_id)

        def write(connection: sqlite3.Connection) -> None:
            if self._event_was_recorded(connection, journal_id, 'recording_cancelled'):
                return
            owner = self._recording_owner_state(connection, run_id)
            connection.execute(
                "UPDATE recording_runs SET state='cancelled',ended_at=? WHERE id=?",
                (now, run_id),
            )
            if owner.cancelled:
                self._record_local_handoff(
                    connection,
                    owner=owner,
                    run_id=run_id,
                    event_type='recording_cancelled',
                    event_id=journal_id,
                    now=now,
                )
            else:
                connection.execute(
                    "UPDATE recording_sessions SET state='cancelled',ended_at=? "
                    "WHERE id=? AND deletion_state='none' "
                    'AND cancellation_generation=?',
                    (now, owner.session_id, owner.source_generation),
                )
            self._insert_event(
                connection,
                journal_id,
                'recording_cancelled',
                owner.room_id,
                run_id,
                None,
                {},
                now,
            )

        await self._database.write(write)
        audit('recording_cancelled', run_id=run_id, result='journaled')

    async def recording_finished(
        self, run_id: str, *, event_id: Optional[str] = None
    ) -> None:
        now = int(self._clock())
        journal_id = self._new_event_id(event_id)

        def write(connection: sqlite3.Connection) -> None:
            if self._event_was_recorded(connection, journal_id, 'recording_finished'):
                return
            owner = self._recording_owner_state(connection, run_id)
            connection.execute(
                "UPDATE recording_runs SET state='finished',ended_at=? WHERE id=?",
                (now, run_id),
            )
            if owner.cancelled:
                self._record_local_handoff(
                    connection,
                    owner=owner,
                    run_id=run_id,
                    event_type='recording_finished',
                    event_id=journal_id,
                    now=now,
                )
            else:
                connection.execute(
                    'UPDATE recording_sessions SET live_end_time=? WHERE id=? '
                    "AND deletion_state='none' AND cancellation_generation=?",
                    (now, owner.session_id, owner.source_generation),
                )
                self._refresh_session_state(connection, owner.session_id, now)
            self._insert_event(
                connection,
                journal_id,
                'recording_finished',
                owner.room_id,
                run_id,
                None,
                {},
                now,
            )

        await self._database.write(write)
        audit('recording_finished', run_id=run_id, result='journaled')

    async def video_postprocessing_failed(
        self,
        run_id: str,
        source_path: str,
        error: BaseException,
        *,
        event_id: Optional[str] = None,
    ) -> None:
        now = int(self._clock())
        source = self._normalize_path(source_path)
        journal_id = self._new_event_id(event_id)
        message = '{}: {}'.format(type(error).__name__, error)[:500]
        loop = asyncio.get_running_loop()
        recovery = await loop.run_in_executor(
            None, self._recover_artifact, source, None
        )
        artifact = recovery.artifact

        def write(connection: sqlite3.Connection) -> None:
            if self._event_was_recorded(
                connection, journal_id, 'video_postprocessing_failed'
            ):
                return
            owner = self._recording_owner_state(connection, run_id)
            if owner.cancelled:
                cursor = connection.execute(
                    "UPDATE recording_parts SET artifact_state='failed',"
                    'final_path=COALESCE(?,final_path),'
                    'file_size_bytes=COALESCE(?,file_size_bytes),'
                    "error_message='本地删除已请求',postprocessed_at=?,updated_at=? "
                    'WHERE run_id=? AND source_path=?',
                    (
                        None if artifact is None else artifact.path,
                        None if artifact is None else artifact.size_bytes,
                        now,
                        now,
                        run_id,
                        source,
                    ),
                )
            elif artifact is None:
                cursor = connection.execute(
                    'UPDATE recording_parts SET artifact_state=?,error_message=?,'
                    'postprocessed_at=?,updated_at=? '
                    'WHERE run_id=? AND source_path=?',
                    ('failed', message, now, now, run_id, source),
                )
            else:
                fallback_message = (
                    '后处理失败，已自动使用原始录制文件：{}'.format(message)
                )[:500]
                cursor = connection.execute(
                    'UPDATE recording_parts SET artifact_state=?,final_path=?,'
                    'file_size_bytes=?,record_duration_seconds='
                    'COALESCE(record_duration_seconds,?),error_message=?,'
                    'postprocessed_at=?,updated_at=? '
                    'WHERE run_id=? AND source_path=?',
                    (
                        'ready',
                        artifact.path,
                        artifact.size_bytes,
                        artifact.duration_seconds,
                        fallback_message,
                        now,
                        now,
                        run_id,
                        source,
                    ),
                )
            if cursor.rowcount != 1:
                raise JournalConsistencyError(
                    "unknown recording part '{}'".format(source_path)
                )
            if owner.cancelled:
                self._record_local_handoff(
                    connection,
                    owner=owner,
                    run_id=run_id,
                    event_type='video_postprocessing_failed',
                    event_id=journal_id,
                    now=now,
                )
            else:
                self._refresh_session_state(connection, owner.session_id, now)
            self._insert_event(
                connection,
                journal_id,
                'video_postprocessing_failed',
                owner.room_id,
                run_id,
                source,
                {'error': message},
                now,
            )

        await self._database.write(write)
        audit(
            'recording_postprocessing_failed',
            level='WARNING' if artifact is not None else 'ERROR',
            run_id=run_id,
            source_path=source,
            recovered=artifact is not None,
            error_type=type(error).__name__,
            result='recovered' if artifact is not None else 'failed',
        )

    async def danmaku_completed(
        self, run_id: str, path: str, *, event_id: Optional[str] = None
    ) -> None:
        now = int(self._clock())
        xml_path = self._normalize_path(path)
        journal_id = self._new_event_id(event_id)
        loop = asyncio.get_running_loop()
        danmaku_count = await loop.run_in_executor(
            None, self._count_danmaku_sync, xml_path
        )

        def write(connection: sqlite3.Connection) -> None:
            if self._event_was_recorded(connection, journal_id, 'danmaku_completed'):
                return
            owner = self._recording_owner_state(connection, run_id)
            rows = connection.execute(
                'SELECT id,source_path FROM recording_parts '
                'WHERE run_id=? ORDER BY part_index',
                (run_id,),
            ).fetchall()
            stem = os.path.splitext(xml_path)[0]
            matches = [
                row
                for row in rows
                if os.path.splitext(str(row['source_path']))[0] == stem
            ]
            if not matches and len(rows) == 1:
                matches = list(rows)
            if len(matches) != 1:
                raise JournalConsistencyError(
                    "cannot bind danmaku file '{}' to one recording part".format(path)
                )
            connection.execute(
                'UPDATE recording_parts SET xml_path=?,xml_completed=?,'
                'danmaku_count=?,updated_at=? '
                'WHERE id=?',
                (
                    xml_path,
                    0 if owner.cancelled else 1,
                    danmaku_count,
                    now,
                    int(matches[0]['id']),
                ),
            )
            if owner.cancelled:
                self._record_local_handoff(
                    connection,
                    owner=owner,
                    run_id=run_id,
                    event_type='danmaku_completed',
                    event_id=journal_id,
                    now=now,
                )
            self._insert_event(
                connection,
                journal_id,
                'danmaku_completed',
                owner.room_id,
                run_id,
                xml_path,
                {},
                now,
            )

        await self._database.write(write)
        audit(
            'recording_danmaku_completed',
            run_id=run_id,
            path=xml_path,
            danmaku_count=danmaku_count,
            result='journaled',
        )

    async def session_for_run(self, run_id: str) -> RecordingSession:
        row = await self._database.fetchone(
            'SELECT session.id,session.room_id,session.broadcast_session_key,'
            'session.live_start_time,session.state,session.started_at,'
            'session.ended_at,session.title,session.cover_url,session.cover_path,'
            'session.anchor_uid,session.anchor_name,session.area_id,'
            'session.area_name,session.parent_area_id,session.parent_area_name,'
            'session.live_end_time,session.upload_intent,'
            'session.upload_decision,session.upload_override_json,'
            'session.upload_resolution_state,session.upload_resolution_error,'
            'session.deletion_state,session.deletion_error,session.source_kind,'
            'clip.id AS highlight_clip_id '
            'FROM recording_sessions session '
            'JOIN recording_runs run ON run.session_id=session.id '
            'LEFT JOIN highlight_clips clip ON clip.upload_session_id=session.id '
            'WHERE run.id=?',
            (run_id,),
        )
        if row is None:
            raise ValueError("unknown recording run '{}'".format(run_id))
        return self._make_session(row, await self.parts_for_session(int(row['id'])))

    async def get_session(self, session_id: int) -> RecordingSession:
        row = await self._database.fetchone(
            'SELECT session.id,session.room_id,session.broadcast_session_key,'
            'session.live_start_time,session.state,session.started_at,'
            'session.ended_at,session.title,session.cover_url,session.cover_path,'
            'session.anchor_uid,session.anchor_name,session.area_id,'
            'session.area_name,session.parent_area_id,session.parent_area_name,'
            'session.live_end_time,'
            "CASE WHEN suppression.session_id IS NOT NULL "
            "OR session.upload_decision='skip' THEN 'skip' "
            "WHEN session.upload_decision='upload' THEN 'upload' "
            "WHEN policy.enabled=1 THEN 'auto' ELSE 'none' END AS upload_intent,"
            'session.upload_decision,session.upload_override_json,'
            'session.upload_resolution_state,session.upload_resolution_error,'
            'session.deletion_state,session.deletion_error,session.source_kind,'
            'clip.id AS highlight_clip_id,'
            'CASE WHEN suppression.session_id IS NULL THEN 0 ELSE 1 END '
            'AS upload_suppressed FROM recording_sessions session '
            'LEFT JOIN upload_jobs job ON job.session_id=session.id '
            'LEFT JOIN upload_suppressions suppression '
            'ON suppression.session_id=session.id '
            'LEFT JOIN room_upload_policies policy ON policy.room_id=session.room_id '
            'LEFT JOIN highlight_clips clip ON clip.upload_session_id=session.id '
            'WHERE session.id=?',
            (session_id,),
        )
        if row is None:
            raise ValueError("unknown recording session '{}'".format(session_id))
        return self._make_session(row, await self.parts_for_session(session_id))

    async def count_sessions(
        self,
        *,
        scope: str = 'all',
        query: str = '',
        session_state: Optional[str] = None,
        upload_state: Optional[str] = None,
        started_from: Optional[int] = None,
        started_to: Optional[int] = None,
    ) -> int:
        where_sql, parameters = self._session_filters(
            scope=scope,
            query=query,
            session_state=session_state,
            upload_state=upload_state,
            started_from=started_from,
            started_to=started_to,
        )
        value = await self._database.scalar(
            'SELECT COUNT(*) FROM recording_sessions session '
            'LEFT JOIN upload_jobs job ON job.session_id=session.id '
            'LEFT JOIN bili_accounts account ON account.id=job.account_id '
            'LEFT JOIN upload_suppressions suppression '
            'ON suppression.session_id=session.id ' + where_sql,
            parameters,
        )
        return int(value or 0)

    async def list_sessions(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
        scope: str = 'all',
        query: str = '',
        session_state: Optional[str] = None,
        upload_state: Optional[str] = None,
        started_from: Optional[int] = None,
        started_to: Optional[int] = None,
        sort_order: str = 'newest',
    ) -> Tuple[RecordingSession, ...]:
        if limit < 1 or limit > 200:
            raise ValueError('limit must be between 1 and 200')
        if offset < 0:
            raise ValueError('offset must not be negative')
        if sort_order not in ('newest', 'oldest'):
            raise ValueError('sort order must be newest or oldest')
        where_sql, parameters = self._session_filters(
            scope=scope,
            query=query,
            session_state=session_state,
            upload_state=upload_state,
            started_from=started_from,
            started_to=started_to,
        )
        direction = 'DESC' if sort_order == 'newest' else 'ASC'
        rows = await self._database.fetchall(
            'SELECT session.id,session.room_id,session.broadcast_session_key,'
            'session.live_start_time,session.state,session.started_at,'
            'session.ended_at,session.title,session.cover_url,session.cover_path,'
            'session.anchor_uid,session.anchor_name,session.area_id,'
            'session.area_name,session.parent_area_id,session.parent_area_name,'
            'session.live_end_time,'
            "CASE WHEN suppression.session_id IS NOT NULL "
            "OR session.upload_decision='skip' THEN 'skip' "
            "WHEN session.upload_decision='upload' THEN 'upload' "
            "WHEN policy.enabled=1 THEN 'auto' ELSE 'none' END AS upload_intent,"
            'session.upload_decision,session.upload_override_json,'
            'session.upload_resolution_state,session.upload_resolution_error,'
            'session.deletion_state,session.deletion_error,'
            'session.source_kind,clip.id AS highlight_clip_id,'
            'CASE WHEN suppression.session_id IS NULL THEN 0 ELSE 1 END '
            'AS upload_suppressed FROM recording_sessions session '
            'LEFT JOIN upload_jobs job ON job.session_id=session.id '
            'LEFT JOIN bili_accounts account ON account.id=job.account_id '
            'LEFT JOIN upload_suppressions suppression '
            'ON suppression.session_id=session.id '
            'LEFT JOIN room_upload_policies policy ON policy.room_id=session.room_id '
            'LEFT JOIN highlight_clips clip ON clip.upload_session_id=session.id '
            + where_sql
            + ' ORDER BY session.started_at {},session.id {} LIMIT ? OFFSET ?'.format(
                direction, direction
            ),
            (*parameters, limit, offset),
        )
        sessions = []
        for row in rows:
            session_id = int(row['id'])
            sessions.append(
                self._make_session(row, await self.parts_for_session(session_id))
            )
        return tuple(sessions)

    async def list_session_summaries(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
        scope: str = 'all',
        query: str = '',
        session_state: Optional[str] = None,
        upload_state: Optional[str] = None,
        started_from: Optional[int] = None,
        started_to: Optional[int] = None,
        sort_order: str = 'newest',
    ) -> Tuple[RecordingSessionSummary, ...]:
        if limit < 1 or limit > 200:
            raise ValueError('limit must be between 1 and 200')
        if offset < 0:
            raise ValueError('offset must not be negative')
        if sort_order not in ('newest', 'oldest'):
            raise ValueError('sort order must be newest or oldest')
        where_sql, parameters = self._session_filters(
            scope=scope,
            query=query,
            session_state=session_state,
            upload_state=upload_state,
            started_from=started_from,
            started_to=started_to,
        )
        direction = 'DESC' if sort_order == 'newest' else 'ASC'
        summary_sql = (
            'WITH selected_sessions AS ('
            'SELECT session.id AS session_id,job.id AS job_id '
            'FROM recording_sessions session '
            'LEFT JOIN upload_jobs job ON job.session_id=session.id '
            'LEFT JOIN bili_accounts account ON account.id=job.account_id '
            'LEFT JOIN upload_suppressions suppression '
            'ON suppression.session_id=session.id '
            + where_sql
            + ' ORDER BY session.started_at {0},session.id {0} '
            'LIMIT ? OFFSET ?),'
            'part_summary AS ('
            'SELECT recording_part.session_id,'
            'COUNT(*) AS part_count,'
            'COALESCE(SUM(recording_part.danmaku_count),0) AS danmaku_count,'
            'COALESCE(SUM(recording_part.file_size_bytes),0) '
            'AS total_file_size_bytes,'
            'COALESCE(SUM(recording_part.record_duration_seconds),0) '
            'AS record_duration_seconds,'
            "COUNT(CASE WHEN NULLIF(recording_part.xml_path,'') IS NOT NULL "
            'AND recording_part.xml_completed=1 THEN 1 END) '
            'AS danmaku_part_count '
            'FROM selected_sessions selected '
            'CROSS JOIN recording_parts recording_part '
            'ON recording_part.session_id=selected.session_id '
            'GROUP BY recording_part.session_id),'
            'selected_upload_parts AS ('
            'SELECT upload_part.id,upload_part.job_id,selected.session_id,'
            'upload_part.part_index,upload_part.upload_state,'
            'upload_part.remote_filename,upload_part.cid,'
            'upload_part.transcode_state '
            'FROM selected_sessions selected '
            'CROSS JOIN upload_parts upload_part '
            'ON upload_part.job_id=selected.job_id),'
            'chunk_by_part AS ('
            'SELECT selected_upload_part.id,selected_upload_part.job_id,'
            'selected_upload_part.session_id,selected_upload_part.part_index,'
            'selected_upload_part.upload_state,'
            'selected_upload_part.remote_filename,selected_upload_part.cid,'
            'selected_upload_part.transcode_state,'
            'COALESCE(SUM(upload_chunk.size),0) AS total_bytes,'
            "COALESCE(SUM(CASE WHEN upload_chunk.state='confirmed' "
            'THEN upload_chunk.size ELSE 0 END),0) AS confirmed_bytes '
            'FROM selected_upload_parts selected_upload_part '
            'LEFT JOIN upload_chunks upload_chunk '
            'ON upload_chunk.part_id=selected_upload_part.id '
            'GROUP BY selected_upload_part.id,selected_upload_part.job_id,'
            'selected_upload_part.session_id,selected_upload_part.part_index,'
            'selected_upload_part.upload_state,'
            'selected_upload_part.remote_filename,selected_upload_part.cid,'
            'selected_upload_part.transcode_state),'
            'chunk_summary AS ('
            'SELECT part.job_id,COALESCE(SUM(part.total_bytes),0) AS total_bytes,'
            'COALESCE(SUM(part.confirmed_bytes),0) AS confirmed_bytes,'
            'COUNT(*) AS discovered_part_count,'
            "COUNT(CASE WHEN part.upload_state='confirmed' THEN 1 END) "
            'AS confirmed_part_count,'
            'COALESCE('
            'MIN(CASE WHEN part.total_bytes<=0 '
            'OR part.confirmed_bytes<part.total_bytes '
            "OR part.upload_state!='confirmed' THEN part.part_index END),"
            'MAX(part.part_index)) AS current_part_index,'
            "COUNT(CASE WHEN part.transcode_state='failed' THEN 1 END) "
            'AS failed_transcode_part_count,'
            "COUNT(CASE WHEN part.upload_state='prepared' "
            'AND part.remote_filename IS NULL THEN 1 END) AS editable_part_count,'
            'COUNT(CASE WHEN part.cid IS NOT NULL THEN 1 END) '
            'AS uploaded_part_count,'
            'COUNT(CASE WHEN part.cid IS NOT NULL '
            "AND NULLIF(recording_part_match.xml_path,'') IS NOT NULL "
            'AND recording_part_match.xml_completed=1 THEN 1 END) '
            'AS backfill_match_count '
            'FROM chunk_by_part part '
            'LEFT JOIN recording_parts recording_part_match '
            'ON recording_part_match.session_id=part.session_id '
            'AND recording_part_match.part_index=part.part_index '
            'GROUP BY part.job_id),'
            'danmaku_summary AS ('
            'SELECT selected_upload_part.job_id,COUNT(*) AS total,'
            "SUM(CASE WHEN danmaku_item.state='confirmed' THEN 1 ELSE 0 END) "
            'AS confirmed,'
            "SUM(CASE WHEN danmaku_item.state IN ('prepared','in_flight') "
            'THEN 1 ELSE 0 END) AS pending,'
            "SUM(CASE WHEN danmaku_item.state='unknown_outcome' "
            'THEN 1 ELSE 0 END) AS unknown_count,'
            "SUM(CASE WHEN danmaku_item.state='failed_permanent' "
            'THEN 1 ELSE 0 END) AS failed '
            'FROM selected_upload_parts selected_upload_part '
            'CROSS JOIN danmaku_items danmaku_item '
            'ON danmaku_item.part_id=selected_upload_part.id '
            'GROUP BY selected_upload_part.job_id) '
            'SELECT session.id AS session_id,session.room_id,'
            'session.live_start_time,session.state AS session_state,'
            'session.started_at,session.ended_at,session.title AS session_title,'
            'session.cover_url,session.anchor_uid,session.anchor_name,'
            'session.area_id,session.area_name,session.parent_area_id,'
            'session.parent_area_name,session.live_end_time,'
            'COALESCE(part_summary.part_count,0) AS part_count,'
            'COALESCE(part_summary.danmaku_count,0) AS recording_danmaku_count,'
            'COALESCE(part_summary.total_file_size_bytes,0) '
            'AS total_file_size_bytes,'
            'COALESCE(part_summary.record_duration_seconds,0) '
            'AS record_duration_seconds,'
            "CASE WHEN suppression.session_id IS NOT NULL "
            "OR session.upload_decision='skip' THEN 'skip' "
            "WHEN session.upload_decision='upload' THEN 'upload' "
            "WHEN policy.enabled=1 THEN 'auto' ELSE 'none' END AS upload_intent,"
            'session.upload_decision,'
            'CASE WHEN session.upload_override_json IS NULL THEN 1 ELSE 0 END '
            'AS submission_inherited,'
            'session.upload_resolution_state,session.upload_resolution_error,'
            'CASE WHEN suppression.session_id IS NULL THEN 0 ELSE 1 END '
            'AS upload_suppressed,'
            'session.deletion_state,session.deletion_error,session.source_kind,'
            'clip.id AS highlight_clip_id,job.id AS job_id,'
            'job.session_id AS job_session_id,job.account_id,'
            'account.uid AS account_uid,'
            'account.display_name AS account_display_name,'
            'job.state AS job_state,job.submit_state,'
            'job.comment_branch_state,job.danmaku_branch_state,job.aid,job.bvid,'
            'job.review_reason,job.attempt,job.next_attempt_at,job.created_at,'
            'job.updated_at,job.repair_state,job.repair_message,job.repair_error,'
            'job.operator_paused,job.scheduled_publish_at,'
            'job.collection_branch_state,job.collection_error,'
            'job.submission_verification_state,job.submission_verified_at,'
            'job.preupload_finalized,job.policy_snapshot_json AS upload_title_source,'
            'COALESCE(chunk_summary.total_bytes,0) AS upload_total_bytes,'
            'COALESCE(chunk_summary.confirmed_bytes,0) AS upload_confirmed_bytes,'
            'COALESCE(chunk_summary.discovered_part_count,0) '
            'AS discovered_part_count,'
            'COALESCE(chunk_summary.confirmed_part_count,0) '
            'AS confirmed_part_count,'
            'chunk_summary.current_part_index,'
            'COALESCE(danmaku_summary.total,0) AS danmaku_total,'
            'COALESCE(danmaku_summary.confirmed,0) AS danmaku_confirmed,'
            'COALESCE(danmaku_summary.pending,0) AS danmaku_pending,'
            'COALESCE(danmaku_summary.unknown_count,0) AS danmaku_unknown,'
            'COALESCE(danmaku_summary.failed,0) AS danmaku_failed,'
            "CASE WHEN job.state='paused' "
            "AND job.submit_state NOT IN ('in_flight','unknown_outcome') "
            "AND job.repair_state NOT IN ('queued','checking','reuploading','editing') "
            'THEN 1 ELSE 0 END AS can_retry,'
            "CASE WHEN job.state IN ('waiting_review','approved','rejected',"
            "'paused','completed') AND job.submit_state='confirmed' "
            'AND job.aid IS NOT NULL AND COALESCE(job.bvid,\'\')!=\'\' '
            "AND job.repair_state NOT IN ('queued','checking','reuploading','editing') "
            "AND NOT (job.repair_state='waiting_review' "
            "AND job.state='waiting_review') "
            'AND COALESCE(chunk_summary.failed_transcode_part_count,0)>0 '
            'THEN 1 ELSE 0 END AS can_repair,'
            "CASE WHEN job.state IN ('waiting_artifacts','ready') "
            "AND job.submit_state='prepared' "
            'AND (job.lease_until IS NULL OR job.lease_until<=?) '
            'THEN 1 ELSE 0 END AS can_skip,'
            "CASE WHEN job.state IN ('approved','completed') "
            "AND job.submit_state='confirmed' AND job.aid IS NOT NULL "
            "AND COALESCE(job.bvid,'')!='' "
            "AND job.repair_state NOT IN ('queued','checking','reuploading','editing') "
            'THEN 1 ELSE 0 END AS can_repost,'
            'CASE WHEN job.id IS NOT NULL '
            'AND (job.lease_until IS NULL OR job.lease_until<=?) '
            "AND job.repair_state NOT IN ('queued','checking','reuploading','editing') "
            'THEN 1 ELSE 0 END AS can_delete,'
            "CASE WHEN job.state IN ('ready','uploading','submitting') "
            "AND job.submit_state='prepared' AND job.operator_paused=0 "
            'THEN 1 ELSE 0 END AS can_pause,'
            "CASE WHEN job.state='paused' AND job.submit_state='prepared' "
            'AND job.operator_paused=1 THEN 1 ELSE 0 END AS can_resume,'
            "CASE WHEN job.state IN ('waiting_artifacts','ready','paused') "
            "AND (job.state!='paused' OR job.operator_paused=1) "
            "AND job.submit_state='prepared' "
            'AND (job.lease_until IS NULL OR job.lease_until<=?) '
            'AND COALESCE(chunk_summary.discovered_part_count,0)>0 '
            'AND chunk_summary.editable_part_count='
            'chunk_summary.discovered_part_count '
            'THEN 1 ELSE 0 END AS can_edit,'
            "CASE WHEN job.state IN ('approved','completed') "
            "AND job.danmaku_branch_state='disabled' "
            'AND COALESCE(part_summary.danmaku_part_count,0)>0 '
            'AND part_summary.danmaku_part_count=chunk_summary.uploaded_part_count '
            'AND part_summary.danmaku_part_count=chunk_summary.backfill_match_count '
            'THEN 1 ELSE 0 END AS can_backfill_danmaku '
            'FROM selected_sessions selected '
            'JOIN recording_sessions session ON session.id=selected.session_id '
            'LEFT JOIN upload_jobs job ON job.id=selected.job_id '
            'LEFT JOIN bili_accounts account ON account.id=job.account_id '
            'LEFT JOIN upload_suppressions suppression '
            'ON suppression.session_id=session.id '
            'LEFT JOIN room_upload_policies policy ON policy.room_id=session.room_id '
            'LEFT JOIN highlight_clips clip ON clip.upload_session_id=session.id '
            'LEFT JOIN part_summary ON part_summary.session_id=session.id '
            'LEFT JOIN chunk_summary ON chunk_summary.job_id=job.id '
            'LEFT JOIN danmaku_summary ON danmaku_summary.job_id=job.id '
            'ORDER BY session.started_at {0},session.id {0}'
        ).format(direction)
        now = int(self._clock())
        rows = await self._database.fetchall(
            summary_sql, (*parameters, limit, offset, now, now, now)
        )
        sampled_at = self._clock()
        return tuple(self._make_session_summary(row, sampled_at) for row in rows)

    @staticmethod
    def _session_filters(
        *,
        scope: str,
        query: str,
        session_state: Optional[str],
        upload_state: Optional[str],
        started_from: Optional[int],
        started_to: Optional[int],
    ) -> Tuple[str, Tuple[object, ...]]:
        if scope not in ('all', 'recordings', 'uploads'):
            raise ValueError('invalid recording session scope')
        session_states = frozenset(
            ('open', 'closed', 'cancelled', 'manual_review', 'skipped')
        )
        upload_states = frozenset(
            (
                'waiting_artifacts',
                'ready',
                'uploading',
                'submitting',
                'waiting_review',
                'approved',
                'rejected',
                'paused',
                'completed',
                'none',
                'suppressed',
            )
        )
        if session_state is not None and session_state not in session_states:
            raise ValueError('invalid recording session state')
        if upload_state is not None and upload_state not in upload_states:
            raise ValueError('invalid upload state')
        if started_from is not None and started_from < 0:
            raise ValueError('started_from must not be negative')
        if started_to is not None and started_to < 0:
            raise ValueError('started_to must not be negative')
        if (
            started_from is not None
            and started_to is not None
            and started_from > started_to
        ):
            raise ValueError('started_from must not be after started_to')

        clauses: List[str] = []
        parameters: List[object] = []
        if scope == 'recordings':
            clauses.append("session.source_kind='live'")
        elif scope == 'uploads':
            clauses.append('job.id IS NOT NULL')
        normalized_query = query.strip()
        if normalized_query:
            escaped = (
                normalized_query.replace('\\', '\\\\')
                .replace('%', '\\%')
                .replace('_', '\\_')
            )
            pattern = '%{}%'.format(escaped)
            clauses.append(
                '(session.title LIKE ? ESCAPE \'\\\' '
                'OR session.anchor_name LIKE ? ESCAPE \'\\\' '
                'OR CAST(session.room_id AS TEXT) LIKE ? ESCAPE \'\\\' '
                'OR COALESCE(job.bvid,\'\') LIKE ? ESCAPE \'\\\' '
                'OR COALESCE(account.display_name,\'\') LIKE ? ESCAPE \'\\\')'
            )
            parameters.extend((pattern,) * 5)
        if session_state is not None:
            clauses.append('session.state=?')
            parameters.append(session_state)
        if upload_state == 'none':
            clauses.append('job.id IS NULL AND suppression.session_id IS NULL')
        elif upload_state == 'suppressed':
            clauses.append('suppression.session_id IS NOT NULL')
        elif upload_state is not None:
            clauses.append('job.state=?')
            parameters.append(upload_state)
        if started_from is not None:
            clauses.append('session.started_at>=?')
            parameters.append(started_from)
        if started_to is not None:
            clauses.append('session.started_at<=?')
            parameters.append(started_to)
        where_sql = '' if not clauses else 'WHERE ' + ' AND '.join(clauses)
        return where_sql, tuple(parameters)

    async def upload_jobs_for_sessions(
        self, session_ids: Sequence[int]
    ) -> Dict[int, UploadJobProgress]:
        unique_session_ids = tuple(dict.fromkeys(int(value) for value in session_ids))
        if not unique_session_ids:
            return {}
        placeholders = ','.join('?' for _ in unique_session_ids)
        jobs = await self._database.fetchall(
            'SELECT job.id,job.session_id,job.account_id,account.uid AS account_uid,'
            'account.display_name AS account_display_name,job.state,'
            'job.policy_snapshot_json,'
            'job.submit_state,job.comment_branch_state,job.danmaku_branch_state,'
            'job.aid,job.bvid,job.review_reason,job.attempt,job.next_attempt_at,'
            'job.created_at,job.updated_at,job.repair_state,job.repair_message,'
            'job.repair_error,job.lease_until,job.operator_paused,'
            'job.scheduled_publish_at,job.collection_branch_state,'
            'job.collection_error,job.submission_verification_state,'
            'job.submission_verified_at,job.submission_verification_json,'
            'job.preupload_finalized '
            'FROM upload_jobs job '
            'JOIN bili_accounts account ON account.id=job.account_id '
            'WHERE job.session_id IN ({})'.format(placeholders),
            unique_session_ids,
        )
        if not jobs:
            return {}
        job_ids = tuple(int(row['id']) for row in jobs)
        job_placeholders = ','.join('?' for _ in job_ids)
        part_rows = await self._database.fetchall(
            'SELECT part.id,part.job_id,part.part_index,part.upload_state,'
            'part.danmaku_import_state,part.remote_filename,part.cid,'
            'part.transcode_state,part.transcode_fail_code,'
            'part.transcode_fail_desc,part.repair_stage,part.repair_diagnostic,'
            'COALESCE(SUM(CASE WHEN chunk.state=\'confirmed\' '
            'THEN chunk.size ELSE 0 END),0) AS confirmed_bytes,'
            'COALESCE(SUM(chunk.size),0) AS total_bytes '
            'FROM upload_parts part LEFT JOIN upload_chunks chunk '
            'ON chunk.part_id=part.id WHERE part.job_id IN ({}) '
            'GROUP BY part.id ORDER BY part.job_id,part.part_index'.format(
                job_placeholders
            ),
            job_ids,
        )
        danmaku_rows = await self._database.fetchall(
            'SELECT part.job_id,COUNT(*) AS total,'
            "SUM(CASE WHEN item.state='confirmed' THEN 1 ELSE 0 END) AS confirmed,"
            "SUM(CASE WHEN item.state IN ('prepared','in_flight') "
            'THEN 1 ELSE 0 END) AS pending,'
            "SUM(CASE WHEN item.state='unknown_outcome' THEN 1 ELSE 0 END) "
            'AS unknown_count,'
            "SUM(CASE WHEN item.state='failed_permanent' THEN 1 ELSE 0 END) "
            'AS failed FROM danmaku_items item '
            'JOIN upload_parts part ON part.id=item.part_id '
            'WHERE part.job_id IN ({}) GROUP BY part.job_id'.format(job_placeholders),
            job_ids,
        )
        danmaku_by_job = {
            int(row['job_id']): (
                int(row['total']),
                int(row['confirmed']),
                int(row['pending']),
                int(row['unknown_count']),
                int(row['failed']),
            )
            for row in danmaku_rows
        }
        unknown_rows = await self._database.fetchall(
            'SELECT item.id,part.job_id,part.part_index,item.progress_ms,'
            'item.content,item.error_message FROM danmaku_items item '
            'JOIN upload_parts part ON part.id=item.part_id '
            'WHERE part.job_id IN ({}) AND item.state=\'unknown_outcome\' '
            'ORDER BY part.job_id,part.part_index,item.progress_ms,item.id'.format(
                job_placeholders
            ),
            job_ids,
        )
        unknown_by_job: Dict[int, List[DanmakuItemProgress]] = {}
        for row in unknown_rows:
            job_id = int(row['job_id'])
            unknown_by_job.setdefault(job_id, []).append(
                DanmakuItemProgress(
                    id=int(row['id']),
                    part_index=int(row['part_index']),
                    progress_ms=int(row['progress_ms']),
                    content=str(row['content']),
                    error_message=(
                        None
                        if row['error_message'] is None
                        else str(row['error_message'])
                    ),
                )
            )
        parts_by_job: Dict[int, List[UploadPartProgress]] = {}
        for row in part_rows:
            job_id = int(row['job_id'])
            parts_by_job.setdefault(job_id, []).append(
                UploadPartProgress(
                    id=int(row['id']),
                    job_id=job_id,
                    part_index=int(row['part_index']),
                    upload_state=str(row['upload_state']),
                    danmaku_import_state=str(row['danmaku_import_state']),
                    remote_filename=(
                        None
                        if row['remote_filename'] is None
                        else str(row['remote_filename'])
                    ),
                    cid=None if row['cid'] is None else int(row['cid']),
                    transcode_state=str(row['transcode_state']),
                    transcode_fail_code=(
                        None
                        if row['transcode_fail_code'] is None
                        else int(row['transcode_fail_code'])
                    ),
                    transcode_fail_desc=(
                        None
                        if row['transcode_fail_desc'] is None
                        else str(row['transcode_fail_desc'])
                    ),
                    repair_stage=str(row['repair_stage']),
                    repair_diagnostic=(
                        None
                        if row['repair_diagnostic'] is None
                        else str(row['repair_diagnostic'])
                    ),
                    confirmed_bytes=int(row['confirmed_bytes']),
                    total_bytes=int(row['total_bytes']),
                )
            )
        result = {}
        sampled_at = self._clock()
        for row in jobs:
            job_id = int(row['id'])
            session_id = int(row['session_id'])
            danmaku = danmaku_by_job.get(job_id, (0, 0, 0, 0, 0))
            parts = tuple(parts_by_job.get(job_id, ()))
            confirmed_bytes = sum(part.confirmed_bytes for part in parts)
            total_bytes = sum(part.total_bytes for part in parts)
            percent = (
                0.0
                if total_bytes <= 0
                else round(min(100.0, confirmed_bytes * 100.0 / total_bytes), 2)
            )
            bytes_per_second: Optional[float] = None
            previous_sample = self._upload_speed_samples.get(job_id)
            if previous_sample is not None:
                elapsed = sampled_at - previous_sample[0]
                byte_delta = confirmed_bytes - previous_sample[1]
                if elapsed > 0 and byte_delta > 0:
                    bytes_per_second = byte_delta / elapsed
            self._upload_speed_samples[job_id] = (sampled_at, confirmed_bytes)
            eta_seconds: Optional[int] = None
            if bytes_per_second is not None and bytes_per_second > 0:
                remaining = max(0, total_bytes - confirmed_bytes)
                eta_seconds = int((remaining / bytes_per_second) + 0.999)
            current_part = next(
                (
                    part.part_index
                    for part in parts
                    if part.total_bytes <= 0
                    or part.confirmed_bytes < part.total_bytes
                    or part.upload_state != 'confirmed'
                ),
                None if not parts else parts[-1].part_index,
            )
            preupload_finalized = bool(row['preupload_finalized'])
            display_state: UploadJobDisplayState
            if preupload_finalized:
                display_state = 'standard'
            elif str(row['state']) == 'paused':
                display_state = 'preupload_paused'
            elif (
                str(row['state']) in ('ready', 'uploading')
                or not parts
                or any(part.upload_state != 'confirmed' for part in parts)
            ):
                display_state = 'preuploading'
            else:
                display_state = 'preuploaded_waiting'
            submission_verification: Optional[Dict[str, object]] = None
            if row['submission_verification_json'] is not None:
                try:
                    decoded = json.loads(str(row['submission_verification_json']))
                    if isinstance(decoded, dict):
                        submission_verification = decoded
                except (TypeError, ValueError, json.JSONDecodeError):
                    pass
            upload_title = ''
            try:
                policy_snapshot = json.loads(str(row['policy_snapshot_json']))
                if isinstance(policy_snapshot, dict) and isinstance(
                    policy_snapshot.get('title'), str
                ):
                    upload_title = str(policy_snapshot['title']).strip()
            except (TypeError, ValueError, json.JSONDecodeError):
                pass
            result[session_id] = UploadJobProgress(
                id=job_id,
                session_id=session_id,
                account_id=int(row['account_id']),
                account_uid=int(row['account_uid']),
                account_display_name=str(row['account_display_name']),
                state=str(row['state']),
                submit_state=str(row['submit_state']),
                comment_branch_state=str(row['comment_branch_state']),
                danmaku_branch_state=str(row['danmaku_branch_state']),
                aid=None if row['aid'] is None else int(row['aid']),
                bvid=None if row['bvid'] is None else str(row['bvid']),
                review_reason=(
                    None if row['review_reason'] is None else str(row['review_reason'])
                ),
                attempt=int(row['attempt']),
                next_attempt_at=int(row['next_attempt_at']),
                created_at=int(row['created_at']),
                updated_at=int(row['updated_at']),
                parts=parts,
                title=upload_title,
                danmaku_total=danmaku[0],
                danmaku_confirmed=danmaku[1],
                danmaku_pending=danmaku[2],
                danmaku_unknown=danmaku[3],
                danmaku_failed=danmaku[4],
                unknown_danmaku_items=tuple(unknown_by_job.get(job_id, ())),
                repair_state=str(row['repair_state']),
                repair_message=(
                    None
                    if row['repair_message'] is None
                    else str(row['repair_message'])
                ),
                repair_error=(
                    None if row['repair_error'] is None else str(row['repair_error'])
                ),
                can_retry=self._can_retry_upload_job(row),
                can_repair=self._can_repair_upload_job(row, parts),
                can_skip=self._can_skip_upload_job(row),
                can_repost=self._can_repost_upload_job(row),
                can_delete=self._can_delete_upload_job(row),
                operator_paused=bool(row['operator_paused']),
                scheduled_publish_at=(
                    None
                    if row['scheduled_publish_at'] is None
                    else int(row['scheduled_publish_at'])
                ),
                collection_branch_state=str(row['collection_branch_state']),
                collection_error=(
                    None
                    if row['collection_error'] is None
                    else str(row['collection_error'])
                ),
                submission_verification_state=str(row['submission_verification_state']),
                submission_verified_at=(
                    None
                    if row['submission_verified_at'] is None
                    else int(row['submission_verified_at'])
                ),
                submission_verification=submission_verification,
                comment_error=(
                    str(row['review_reason'])
                    if str(row['comment_branch_state']) in ('paused', 'failed')
                    and row['review_reason'] is not None
                    else None
                ),
                danmaku_error=(
                    str(row['review_reason'])
                    if str(row['danmaku_branch_state']) in ('paused', 'failed')
                    and row['review_reason'] is not None
                    else None
                ),
                can_pause=self._can_pause_upload_job(row),
                can_resume=self._can_resume_upload_job(row),
                can_edit=self._can_edit_upload_job(row, parts),
                confirmed_bytes=confirmed_bytes,
                total_bytes=total_bytes,
                percent=percent,
                bytes_per_second=bytes_per_second,
                eta_seconds=eta_seconds,
                current_part_index=current_part,
                preupload_finalized=preupload_finalized,
                display_state=display_state,
            )
        return result

    async def realtime_upload_progress(self) -> List[Dict[str, object]]:
        cutoff = int(self._clock()) - 300
        jobs = await self._database.fetchall(
            'SELECT job.id,job.session_id,job.state,job.submit_state,'
            'job.preupload_finalized,job.aid,job.bvid '
            'FROM upload_jobs job WHERE '
            "job.state IN ('waiting_artifacts','ready','uploading','submitting',"
            "'waiting_review') "
            "OR job.repair_state IN ('queued','checking','reuploading','editing',"
            "'waiting_review') "
            "OR job.comment_branch_state IN ('pending','running') "
            "OR job.danmaku_branch_state IN ('pending','importing','publishing') "
            "OR job.collection_branch_state IN ('pending','running') "
            'OR job.updated_at>=? ORDER BY job.id',
            (cutoff,),
        )
        if not jobs:
            return []
        job_ids = tuple(int(row['id']) for row in jobs)
        placeholders = ','.join('?' for _ in job_ids)
        part_rows = await self._database.fetchall(
            'SELECT progress.job_id,'
            'COALESCE(SUM(progress.confirmed_bytes),0) AS confirmed_bytes,'
            'COALESCE(SUM(progress.total_bytes),0) AS total_bytes,'
            'COALESCE(MIN(CASE WHEN progress.total_bytes<=0 '
            'OR progress.confirmed_bytes<progress.total_bytes '
            "OR progress.upload_state!='confirmed' THEN progress.part_index END),"
            'MAX(progress.part_index)) AS current_part_index,'
            "SUM(CASE WHEN progress.upload_state='confirmed' THEN 1 ELSE 0 END) "
            'AS confirmed_part_count,COUNT(*) AS discovered_part_count '
            'FROM (SELECT part.id,part.job_id,part.part_index,part.upload_state,'
            "COALESCE(SUM(CASE WHEN chunk.state='confirmed' THEN chunk.size "
            'ELSE 0 END),0) AS confirmed_bytes,'
            'COALESCE(SUM(chunk.size),0) AS total_bytes '
            'FROM upload_parts part LEFT JOIN upload_chunks chunk '
            'ON chunk.part_id=part.id WHERE part.job_id IN ({}) '
            'GROUP BY part.id) progress GROUP BY progress.job_id '
            'ORDER BY progress.job_id'.format(placeholders),
            job_ids,
        )
        parts_by_job = {int(row['job_id']): row for row in part_rows}
        sampled_at = self._clock()
        result: List[Dict[str, object]] = []
        for job in jobs:
            job_id = int(job['id'])
            part = parts_by_job.get(job_id)
            confirmed_bytes = 0 if part is None else int(part['confirmed_bytes'])
            total_bytes = 0 if part is None else int(part['total_bytes'])
            confirmed_part_count = (
                0 if part is None else int(part['confirmed_part_count'])
            )
            discovered_part_count = (
                0 if part is None else int(part['discovered_part_count'])
            )
            current_part_index = (
                None if part is None else int(part['current_part_index'])
            )
            percent = (
                0.0
                if total_bytes <= 0
                else round(min(100.0, confirmed_bytes * 100.0 / total_bytes), 2)
            )
            bytes_per_second: Optional[float] = None
            previous_sample = self._upload_speed_samples.get(job_id)
            if previous_sample is not None:
                elapsed = sampled_at - previous_sample[0]
                byte_delta = confirmed_bytes - previous_sample[1]
                if elapsed > 0 and byte_delta > 0:
                    bytes_per_second = byte_delta / elapsed
            self._upload_speed_samples[job_id] = (sampled_at, confirmed_bytes)
            eta_seconds: Optional[int] = None
            if bytes_per_second is not None and bytes_per_second > 0:
                remaining = max(0, total_bytes - confirmed_bytes)
                eta_seconds = int((remaining / bytes_per_second) + 0.999)
            preupload_finalized = bool(job['preupload_finalized'])
            display_state: UploadJobDisplayState
            if preupload_finalized:
                display_state = 'standard'
            elif str(job['state']) == 'paused':
                display_state = 'preupload_paused'
            elif (
                str(job['state']) in ('ready', 'uploading')
                or discovered_part_count == 0
                or confirmed_part_count != discovered_part_count
            ):
                display_state = 'preuploading'
            else:
                display_state = 'preuploaded_waiting'
            result.append(
                {
                    'jobId': job_id,
                    'sessionId': int(job['session_id']),
                    'state': str(job['state']),
                    'submitState': str(job['submit_state']),
                    'preuploadFinalized': preupload_finalized,
                    'displayState': display_state,
                    'aid': None if job['aid'] is None else int(job['aid']),
                    'bvid': None if job['bvid'] is None else str(job['bvid']),
                    'confirmedBytes': confirmed_bytes,
                    'totalBytes': total_bytes,
                    'percent': percent,
                    'bytesPerSecond': bytes_per_second,
                    'etaSeconds': eta_seconds,
                    'currentPartIndex': current_part_index,
                    'confirmedPartCount': confirmed_part_count,
                    'discoveredPartCount': discovered_part_count,
                }
            )
        return result

    @staticmethod
    def _can_retry_upload_job(row: sqlite3.Row) -> bool:
        return (
            str(row['state']) == 'paused'
            and str(row['submit_state']) not in ('in_flight', 'unknown_outcome')
            and str(row['repair_state'])
            not in ('queued', 'checking', 'reuploading', 'editing')
        )

    @staticmethod
    def _can_repair_upload_job(
        row: sqlite3.Row, parts: Sequence[UploadPartProgress]
    ) -> bool:
        state = str(row['state'])
        repair_state = str(row['repair_state'])
        return (
            state in ('waiting_review', 'approved', 'rejected', 'paused', 'completed')
            and str(row['submit_state']) == 'confirmed'
            and row['aid'] is not None
            and bool(row['bvid'])
            and repair_state not in ('queued', 'checking', 'reuploading', 'editing')
            and not (repair_state == 'waiting_review' and state == 'waiting_review')
            and any(part.transcode_state == 'failed' for part in parts)
        )

    def _can_skip_upload_job(self, row: sqlite3.Row) -> bool:
        return (
            str(row['state']) in ('waiting_artifacts', 'ready')
            and str(row['submit_state']) == 'prepared'
            and not self._has_active_job_lease(row)
        )

    @staticmethod
    def _can_repost_upload_job(row: sqlite3.Row) -> bool:
        return (
            str(row['state']) in ('approved', 'completed')
            and str(row['submit_state']) == 'confirmed'
            and row['aid'] is not None
            and bool(row['bvid'])
            and str(row['repair_state'])
            not in ('queued', 'checking', 'reuploading', 'editing')
        )

    def _can_delete_upload_job(self, row: sqlite3.Row) -> bool:
        return not self._has_active_job_lease(row) and str(row['repair_state']) not in (
            'queued',
            'checking',
            'reuploading',
            'editing',
        )

    @staticmethod
    def _can_pause_upload_job(row: sqlite3.Row) -> bool:
        return (
            str(row['state']) in ('ready', 'uploading', 'submitting')
            and str(row['submit_state']) == 'prepared'
            and not bool(row['operator_paused'])
        )

    @staticmethod
    def _can_resume_upload_job(row: sqlite3.Row) -> bool:
        return (
            str(row['state']) == 'paused'
            and str(row['submit_state']) == 'prepared'
            and bool(row['operator_paused'])
        )

    def _can_edit_upload_job(
        self, row: sqlite3.Row, parts: Sequence[UploadPartProgress]
    ) -> bool:
        return (
            str(row['state']) in ('waiting_artifacts', 'ready', 'paused')
            and (str(row['state']) != 'paused' or bool(row['operator_paused']))
            and str(row['submit_state']) == 'prepared'
            and not self._has_active_job_lease(row)
            and bool(parts)
            and all(
                part.upload_state == 'prepared' and part.remote_filename is None
                for part in parts
            )
        )

    def _has_active_job_lease(self, row: sqlite3.Row) -> bool:
        return row['lease_until'] is not None and int(row['lease_until']) > int(
            self._clock()
        )

    async def run_id_for_source(self, source_path: str) -> str:
        rows = await self._database.fetchall(
            'SELECT run_id FROM recording_parts WHERE source_path=? '
            'ORDER BY id DESC LIMIT 2',
            (self._normalize_path(source_path),),
        )
        if len(rows) != 1:
            raise JournalConsistencyError(
                "cannot identify one run for '{}'".format(source_path)
            )
        return str(rows[0]['run_id'])

    async def parts_for_run(self, run_id: str) -> Tuple[RecordingPart, ...]:
        rows = await self._database.fetchall(
            'SELECT id,session_id,run_id,part_index,source_path,final_path,'
            'xml_path,record_start_time,record_end_time,record_duration_seconds,'
            'file_size_bytes,danmaku_count,artifact_state,xml_completed,'
            'error_message,upload_excluded_reason,media_index_state,media_index_error,'
            'media_index_progress '
            'FROM recording_parts WHERE run_id=? ORDER BY part_index',
            (run_id,),
        )
        return tuple(self._make_part(row) for row in rows)

    async def parts_for_session(self, session_id: int) -> Tuple[RecordingPart, ...]:
        rows = await self._database.fetchall(
            'SELECT id,session_id,run_id,part_index,source_path,final_path,'
            'xml_path,record_start_time,record_end_time,record_duration_seconds,'
            'file_size_bytes,danmaku_count,artifact_state,xml_completed,'
            'error_message,upload_excluded_reason,media_index_state,media_index_error,'
            'media_index_progress '
            'FROM recording_parts WHERE session_id=? ORDER BY part_index',
            (session_id,),
        )
        return tuple(self._make_part(row) for row in rows)

    async def active_part_for_session(
        self, session_id: int
    ) -> Optional[ActiveRecordingPart]:
        row = await self._database.fetchone(
            'SELECT id,part_index,artifact_state '
            'FROM recording_parts WHERE session_id=? '
            "AND artifact_state IN ('recording','postprocessing') "
            'ORDER BY part_index DESC LIMIT 1',
            (int(session_id),),
        )
        if row is None:
            return None
        return ActiveRecordingPart(
            id=int(row['id']),
            part_index=int(row['part_index']),
            artifact_state=str(row['artifact_state']),
        )

    def _make_session_summary(
        self, row: sqlite3.Row, sampled_at: float
    ) -> RecordingSessionSummary:
        upload_job = (
            None
            if row['job_id'] is None
            else self._make_upload_job_summary(row, sampled_at)
        )
        source_kind = str(row['source_kind'])
        title = str(row['session_title'])
        if source_kind == 'highlight' and upload_job is not None and upload_job.title:
            title = upload_job.title
        return RecordingSessionSummary(
            id=int(row['session_id']),
            room_id=int(row['room_id']),
            live_start_time=(
                None if row['live_start_time'] is None else int(row['live_start_time'])
            ),
            state=str(row['session_state']),
            started_at=int(row['started_at']),
            ended_at=None if row['ended_at'] is None else int(row['ended_at']),
            title=title,
            cover_url=str(row['cover_url']),
            anchor_uid=None if row['anchor_uid'] is None else int(row['anchor_uid']),
            anchor_name=str(row['anchor_name']),
            area_id=None if row['area_id'] is None else int(row['area_id']),
            area_name=str(row['area_name']),
            parent_area_id=(
                None if row['parent_area_id'] is None else int(row['parent_area_id'])
            ),
            parent_area_name=str(row['parent_area_name']),
            live_end_time=(
                None if row['live_end_time'] is None else int(row['live_end_time'])
            ),
            part_count=int(row['part_count']),
            danmaku_count=int(row['recording_danmaku_count']),
            total_file_size_bytes=int(row['total_file_size_bytes']),
            record_duration_seconds=int(row['record_duration_seconds']),
            upload_intent=str(row['upload_intent']),
            upload_decision=str(row['upload_decision']),
            submission_inherited=bool(row['submission_inherited']),
            upload_resolution_state=str(row['upload_resolution_state']),
            upload_resolution_error=(
                None
                if row['upload_resolution_error'] is None
                else str(row['upload_resolution_error'])
            ),
            upload_suppressed=bool(row['upload_suppressed']),
            deletion_state=str(row['deletion_state']),
            deletion_error=(
                None if row['deletion_error'] is None else str(row['deletion_error'])
            ),
            source_kind=source_kind,
            highlight_clip_id=(
                None
                if row['highlight_clip_id'] is None
                else int(row['highlight_clip_id'])
            ),
            upload_job=upload_job,
        )

    def _make_upload_job_summary(
        self, row: sqlite3.Row, sampled_at: float
    ) -> UploadJobSummary:
        job_id = int(row['job_id'])
        confirmed_bytes = int(row['upload_confirmed_bytes'])
        total_bytes = int(row['upload_total_bytes'])
        percent = (
            0.0
            if total_bytes <= 0
            else round(min(100.0, confirmed_bytes * 100.0 / total_bytes), 2)
        )
        bytes_per_second: Optional[float] = None
        previous_sample = self._upload_speed_samples.get(job_id)
        if previous_sample is not None:
            elapsed = sampled_at - previous_sample[0]
            byte_delta = confirmed_bytes - previous_sample[1]
            if elapsed > 0 and byte_delta > 0:
                bytes_per_second = byte_delta / elapsed
        self._upload_speed_samples[job_id] = (sampled_at, confirmed_bytes)
        eta_seconds: Optional[int] = None
        if bytes_per_second is not None and bytes_per_second > 0:
            remaining = max(0, total_bytes - confirmed_bytes)
            eta_seconds = int((remaining / bytes_per_second) + 0.999)
        discovered_part_count = int(row['discovered_part_count'])
        confirmed_part_count = int(row['confirmed_part_count'])
        preupload_finalized = bool(row['preupload_finalized'])
        if preupload_finalized:
            display_state: UploadJobDisplayState = 'standard'
        elif str(row['job_state']) == 'paused':
            display_state = 'preupload_paused'
        elif (
            str(row['job_state']) in ('ready', 'uploading')
            or discovered_part_count == 0
            or confirmed_part_count != discovered_part_count
        ):
            display_state = 'preuploading'
        else:
            display_state = 'preuploaded_waiting'
        upload_title = ''
        try:
            policy_snapshot = json.loads(str(row['upload_title_source']))
            if isinstance(policy_snapshot, dict) and isinstance(
                policy_snapshot.get('title'), str
            ):
                upload_title = str(policy_snapshot['title']).strip()
        except (TypeError, ValueError, json.JSONDecodeError):
            pass
        review_reason = (
            None if row['review_reason'] is None else str(row['review_reason'])
        )
        return UploadJobSummary(
            id=job_id,
            session_id=int(row['job_session_id']),
            account_id=int(row['account_id']),
            account_uid=int(row['account_uid']),
            account_display_name=str(row['account_display_name']),
            state=str(row['job_state']),
            submit_state=str(row['submit_state']),
            comment_branch_state=str(row['comment_branch_state']),
            danmaku_branch_state=str(row['danmaku_branch_state']),
            aid=None if row['aid'] is None else int(row['aid']),
            bvid=None if row['bvid'] is None else str(row['bvid']),
            review_reason=review_reason,
            attempt=int(row['attempt']),
            next_attempt_at=int(row['next_attempt_at']),
            created_at=int(row['created_at']),
            updated_at=int(row['updated_at']),
            danmaku_total=int(row['danmaku_total']),
            danmaku_confirmed=int(row['danmaku_confirmed']),
            danmaku_pending=int(row['danmaku_pending']),
            danmaku_unknown=int(row['danmaku_unknown']),
            danmaku_failed=int(row['danmaku_failed']),
            repair_state=str(row['repair_state']),
            repair_message=(
                None if row['repair_message'] is None else str(row['repair_message'])
            ),
            repair_error=(
                None if row['repair_error'] is None else str(row['repair_error'])
            ),
            can_retry=bool(row['can_retry']),
            can_repair=bool(row['can_repair']),
            can_skip=bool(row['can_skip']),
            can_repost=bool(row['can_repost']),
            can_delete=bool(row['can_delete']),
            operator_paused=bool(row['operator_paused']),
            scheduled_publish_at=(
                None
                if row['scheduled_publish_at'] is None
                else int(row['scheduled_publish_at'])
            ),
            collection_branch_state=str(row['collection_branch_state']),
            collection_error=(
                None
                if row['collection_error'] is None
                else str(row['collection_error'])
            ),
            submission_verification_state=str(row['submission_verification_state']),
            submission_verified_at=(
                None
                if row['submission_verified_at'] is None
                else int(row['submission_verified_at'])
            ),
            comment_error=(
                review_reason
                if str(row['comment_branch_state']) in ('paused', 'failed')
                else None
            ),
            danmaku_error=(
                review_reason
                if str(row['danmaku_branch_state']) in ('paused', 'failed')
                else None
            ),
            can_pause=bool(row['can_pause']),
            can_resume=bool(row['can_resume']),
            can_edit=bool(row['can_edit']),
            can_backfill_danmaku=bool(row['can_backfill_danmaku']),
            confirmed_bytes=confirmed_bytes,
            total_bytes=total_bytes,
            percent=percent,
            bytes_per_second=bytes_per_second,
            eta_seconds=eta_seconds,
            current_part_index=(
                None
                if row['current_part_index'] is None
                else int(row['current_part_index'])
            ),
            confirmed_part_count=confirmed_part_count,
            discovered_part_count=discovered_part_count,
            preupload_finalized=preupload_finalized,
            display_state=display_state,
            title=upload_title,
        )

    @staticmethod
    def _make_session(
        row: sqlite3.Row, parts: Tuple[RecordingPart, ...] = ()
    ) -> RecordingSession:
        return RecordingSession(
            id=int(row['id']),
            room_id=int(row['room_id']),
            broadcast_session_key=str(row['broadcast_session_key']),
            live_start_time=(
                None if row['live_start_time'] is None else int(row['live_start_time'])
            ),
            state=str(row['state']),
            started_at=int(row['started_at']),
            ended_at=None if row['ended_at'] is None else int(row['ended_at']),
            title=str(row['title']),
            cover_url=str(row['cover_url']),
            cover_path=(None if row['cover_path'] is None else str(row['cover_path'])),
            anchor_uid=(None if row['anchor_uid'] is None else int(row['anchor_uid'])),
            anchor_name=str(row['anchor_name']),
            area_id=None if row['area_id'] is None else int(row['area_id']),
            area_name=str(row['area_name']),
            parent_area_id=(
                None if row['parent_area_id'] is None else int(row['parent_area_id'])
            ),
            parent_area_name=str(row['parent_area_name']),
            live_end_time=(
                None if row['live_end_time'] is None else int(row['live_end_time'])
            ),
            upload_intent=(
                str(row['upload_intent']) if 'upload_intent' in row.keys() else 'none'
            ),
            upload_decision=(
                str(row['upload_decision'])
                if 'upload_decision' in row.keys()
                else 'follow_room'
            ),
            submission_inherited=(
                'upload_override_json' not in row.keys()
                or row['upload_override_json'] is None
            ),
            upload_resolution_state=(
                str(row['upload_resolution_state'])
                if 'upload_resolution_state' in row.keys()
                else 'pending'
            ),
            upload_resolution_error=(
                None
                if 'upload_resolution_error' not in row.keys()
                or row['upload_resolution_error'] is None
                else str(row['upload_resolution_error'])
            ),
            upload_suppressed=(
                bool(row['upload_suppressed'])
                if 'upload_suppressed' in row.keys()
                else False
            ),
            deletion_state=(
                str(row['deletion_state']) if 'deletion_state' in row.keys() else 'none'
            ),
            deletion_error=(
                None
                if 'deletion_error' not in row.keys() or row['deletion_error'] is None
                else str(row['deletion_error'])
            ),
            source_kind=(
                str(row['source_kind']) if 'source_kind' in row.keys() else 'live'
            ),
            highlight_clip_id=(
                None
                if 'highlight_clip_id' not in row.keys()
                or row['highlight_clip_id'] is None
                else int(row['highlight_clip_id'])
            ),
            parts=parts,
        )

    @staticmethod
    def _make_part(row: sqlite3.Row) -> RecordingPart:
        final_path = None if row['final_path'] is None else str(row['final_path'])
        return RecordingPart(
            id=int(row['id']),
            session_id=int(row['session_id']),
            run_id=str(row['run_id']),
            part_index=int(row['part_index']),
            source_path=str(row['source_path']),
            final_path=final_path,
            xml_path=None if row['xml_path'] is None else str(row['xml_path']),
            record_start_time=int(row['record_start_time']),
            artifact_state=str(row['artifact_state']),
            xml_completed=bool(row['xml_completed']),
            source_exists=os.path.exists(str(row['source_path'])),
            final_exists=final_path is not None and os.path.exists(final_path),
            error_message=(
                None if row['error_message'] is None else str(row['error_message'])
            ),
            upload_excluded_reason=(
                None
                if row['upload_excluded_reason'] is None
                else str(row['upload_excluded_reason'])
            ),
            record_end_time=(
                None if row['record_end_time'] is None else int(row['record_end_time'])
            ),
            record_duration_seconds=(
                None
                if row['record_duration_seconds'] is None
                else int(row['record_duration_seconds'])
            ),
            file_size_bytes=(
                None if row['file_size_bytes'] is None else int(row['file_size_bytes'])
            ),
            danmaku_count=int(row['danmaku_count']),
            media_index_state=(
                str(row['media_index_state'])
                if 'media_index_state' in row.keys()
                else 'pending'
            ),
            media_index_error=(
                None
                if 'media_index_error' not in row.keys()
                or row['media_index_error'] is None
                else str(row['media_index_error'])
            ),
            media_index_progress=(
                float(row['media_index_progress'])
                if 'media_index_progress' in row.keys()
                else 0.0
            ),
        )

    def _new_event_id(self, event_id: Optional[str]) -> str:
        return self._uuid_factory() if event_id is None else event_id

    @staticmethod
    def _event_was_recorded(
        connection: sqlite3.Connection, event_id: str, expected_type: str
    ) -> bool:
        row = connection.execute(
            'SELECT event_type FROM event_journal WHERE id=?', (event_id,)
        ).fetchone()
        if row is None:
            return False
        if row['event_type'] != expected_type:
            raise JournalConsistencyError(
                "event '{}' has conflicting content".format(event_id)
            )
        return True

    @staticmethod
    def _insert_event(
        connection: sqlite3.Connection,
        event_id: str,
        event_type: str,
        room_id: int,
        run_id: str,
        path: Optional[str],
        payload: object,
        occurred_at: int,
    ) -> None:
        connection.execute(
            'INSERT INTO event_journal('
            'id,event_type,room_id,run_id,path,payload_json,occurred_at,consumed_at) '
            'VALUES(?,?,?,?,?,?,?,?)',
            (
                event_id,
                event_type,
                room_id,
                run_id,
                path,
                json.dumps(payload, ensure_ascii=False, sort_keys=True),
                occurred_at,
                occurred_at,
            ),
        )

    @staticmethod
    def _recording_owner_state(
        connection: sqlite3.Connection, run_id: str
    ) -> _RecordingOwnerState:
        row = connection.execute(
            'SELECT session.id AS session_id,session.room_id,'
            'session.cancellation_generation,session.deletion_state '
            'FROM recording_runs run '
            'JOIN recording_sessions session ON session.id=run.session_id '
            'WHERE run.id=?',
            (run_id,),
        ).fetchone()
        if row is None:
            raise JournalConsistencyError("unknown recording run '{}'".format(run_id))
        started = connection.execute(
            "SELECT payload_json FROM event_journal WHERE run_id=? "
            "AND event_type='recording_started' ORDER BY occurred_at,id LIMIT 1",
            (run_id,),
        ).fetchone()
        current_generation = int(row['cancellation_generation'])
        source_generation = current_generation
        if started is not None:
            try:
                payload = json.loads(str(started['payload_json']))
                source_generation = int(payload['cancellation_generation'])
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                # Migration 26 added generation journaling; older runs started at 0.
                source_generation = 0
        elif str(row['deletion_state']) != 'none':
            # Runs created before generation journaling all started at generation 0.
            source_generation = 0
        return _RecordingOwnerState(
            session_id=int(row['session_id']),
            room_id=int(row['room_id']),
            source_generation=source_generation,
            cancelled=(
                str(row['deletion_state']) != 'none'
                or current_generation != source_generation
            ),
        )

    @staticmethod
    def _record_local_handoff(
        connection: sqlite3.Connection,
        *,
        owner: _RecordingOwnerState,
        run_id: str,
        event_type: str,
        event_id: str,
        now: int,
    ) -> None:
        connection.execute(
            'INSERT INTO owner_handoff_outcomes('
            'owner_kind,owner_id,side_effect_key,source_generation,'
            'outcome_state,outcome_json,acknowledged_at) '
            "VALUES('recorder',?,?,?,'cancelled_local','{}',?) "
            'ON CONFLICT(owner_kind,owner_id,side_effect_key,source_generation) '
            "DO UPDATE SET outcome_state='cancelled_local',outcome_json='{}',"
            'acknowledged_at=excluded.acknowledged_at',
            (
                owner.session_id,
                '{}:{}:{}'.format(run_id, event_type, event_id),
                owner.source_generation,
                now,
            ),
        )

    @staticmethod
    def _session_id_for_run(connection: sqlite3.Connection, run_id: str) -> int:
        row = connection.execute(
            'SELECT session_id FROM recording_runs WHERE id=?', (run_id,)
        ).fetchone()
        if row is None:
            raise JournalConsistencyError("unknown recording run '{}'".format(run_id))
        return int(row['session_id'])

    @staticmethod
    def _room_id_for_run(connection: sqlite3.Connection, run_id: str) -> int:
        row = connection.execute(
            'SELECT session.room_id FROM recording_sessions session '
            'JOIN recording_runs run ON run.session_id=session.id WHERE run.id=?',
            (run_id,),
        ).fetchone()
        if row is None:
            raise JournalConsistencyError("unknown recording run '{}'".format(run_id))
        return int(row['room_id'])

    @staticmethod
    def _refresh_session_state(
        connection: sqlite3.Connection, session_id: int, now: int
    ) -> None:
        session = connection.execute(
            'SELECT state,deletion_state FROM recording_sessions WHERE id=?',
            (session_id,),
        ).fetchone()
        if session is None:
            raise JournalConsistencyError(
                "unknown recording session '{}'".format(session_id)
            )
        if (
            session['state'] in ('cancelled', 'skipped')
            or session['deletion_state'] != 'none'
        ):
            return
        recording_runs = int(
            connection.execute(
                "SELECT COUNT(*) FROM recording_runs WHERE session_id=? "
                "AND state='recording'",
                (session_id,),
            ).fetchone()[0]
        )
        if recording_runs:
            connection.execute(
                "UPDATE recording_sessions SET state='open',ended_at=NULL "
                'WHERE id=?',
                (session_id,),
            )
            return
        states = {
            str(row['artifact_state'])
            for row in connection.execute(
                'SELECT artifact_state FROM recording_parts WHERE session_id=?',
                (session_id,),
            ).fetchall()
        }
        if 'ready' in states and states <= {'ready', 'failed', 'missing'}:
            state = 'closed'
        elif states <= {'failed', 'missing'}:
            state = 'skipped'
        else:
            state = 'open'
        connection.execute(
            'UPDATE recording_sessions SET state=?,ended_at=? WHERE id=?',
            (state, now if state != 'open' else None, session_id),
        )

    @staticmethod
    def _normalize_path(path: str) -> str:
        return os.path.abspath(os.path.expanduser(path))

    @staticmethod
    def _file_size_or_none(path: str) -> Optional[int]:
        try:
            return os.path.getsize(path)
        except OSError:
            return None

    @staticmethod
    def _count_danmaku_sync(path: str) -> int:
        count = 0
        for _, element in ElementTree.iterparse(path, events=('end',)):
            if element.tag.rsplit('}', 1)[-1] == 'd':
                count += 1
            element.clear()
        return count


class RecordingJournalListener:
    def __init__(
        self,
        journal: RecordingJournalBridge,
        recorder: Recorder,
        postprocessor: Postprocessor,
    ) -> None:
        self._journal = journal
        self._recorder = recorder
        self._postprocessor = postprocessor
        self._current_run_id: Optional[str] = None
        self._source_runs: Dict[str, str] = {}
        recorder.add_listener(self)  # type: ignore[arg-type]
        postprocessor.add_listener(self)  # type: ignore[arg-type]

    def close(self) -> None:
        self._recorder.remove_listener(self)  # type: ignore[arg-type]
        self._postprocessor.remove_listener(self)  # type: ignore[arg-type]

    async def on_recording_started(self, recorder: Recorder) -> None:
        room_info = recorder.live.room_info
        user_info = recorder.live.user_info
        self._current_run_id = await self._guard(
            self._journal.recording_started(
                int(room_info.room_id),
                live_start_time=int(room_info.live_start_time),
                metadata=RecordingSessionMetadata(
                    title=str(room_info.title),
                    cover_url=str(room_info.cover),
                    anchor_uid=int(user_info.uid),
                    anchor_name=str(user_info.name),
                    area_id=int(room_info.area_id),
                    area_name=str(room_info.area_name),
                    parent_area_id=int(room_info.parent_area_id),
                    parent_area_name=str(room_info.parent_area_name),
                ),
            )
        )

    async def on_recording_finished(self, recorder: Recorder) -> None:
        run_id = self._require_current_run()
        await self._guard(self._journal.recording_finished(run_id))
        self._current_run_id = None

    async def on_recording_cancelled(self, recorder: Recorder) -> None:
        run_id = self._require_current_run()
        await self._guard(self._journal.recording_cancelled(run_id))
        self._current_run_id = None

    async def on_video_file_created(self, recorder: Recorder, path: str) -> None:
        run_id = self._require_current_run()
        record_start_time = recorder.record_start_time
        if record_start_time is None:
            error = JournalConsistencyError('video file has no record start time')
            self._journal.pause_automation(error)
            raise error
        await self._guard(
            self._journal.video_created(
                run_id, path, record_start_time=int(record_start_time)
            )
        )
        self._source_runs[self._normalize_path(path)] = run_id

    async def on_video_file_completed(self, recorder: Recorder, path: str) -> None:
        run_id = await self._run_for_source(path)
        await self._guard(self._journal.video_completed(run_id, path))

    async def on_danmaku_file_created(self, recorder: Recorder, path: str) -> None:
        return None

    async def on_danmaku_file_completed(self, recorder: Recorder, path: str) -> None:
        run_id = self._require_current_run()
        await self._guard(self._journal.danmaku_completed(run_id, path))

    async def on_raw_danmaku_file_created(self, recorder: Recorder, path: str) -> None:
        return None

    async def on_raw_danmaku_file_completed(
        self, recorder: Recorder, path: str
    ) -> None:
        return None

    async def on_cover_image_downloaded(self, recorder: Recorder, path: str) -> None:
        run_id = self._require_current_run()
        await self._guard(self._journal.cover_downloaded(run_id, path))

    async def on_video_postprocessing_completed(
        self, postprocessor: Postprocessor, path: str
    ) -> None:
        return None

    async def on_video_postprocessing_result(
        self, postprocessor: Postprocessor, source_path: str, result_path: str
    ) -> None:
        run_id = await self._run_for_source(source_path)
        await self._guard(
            self._journal.video_postprocessed(run_id, source_path, result_path)
        )
        self._source_runs.pop(self._normalize_path(source_path), None)

    async def on_video_postprocessing_failed(
        self, postprocessor: Postprocessor, source_path: str, error: BaseException
    ) -> None:
        run_id = await self._run_for_source(source_path)
        await self._guard(
            self._journal.video_postprocessing_failed(run_id, source_path, error)
        )
        self._source_runs.pop(self._normalize_path(source_path), None)

    async def on_postprocessing_completed(
        self, postprocessor: Postprocessor, files: List[str]
    ) -> None:
        return None

    async def _run_for_source(self, path: str) -> str:
        normalized = self._normalize_path(path)
        run_id = self._source_runs.get(normalized)
        if run_id is not None:
            return run_id
        return await self._guard(self._journal.run_id_for_source(normalized))

    async def _guard(self, operation: Awaitable[_T]) -> _T:
        try:
            return await operation
        except Exception as error:
            self._journal.pause_automation(error)
            raise

    def _require_current_run(self) -> str:
        if self._current_run_id is None:
            error = JournalConsistencyError('recording event has no active run')
            self._journal.pause_automation(error)
            raise error
        return self._current_run_id

    @staticmethod
    def _normalize_path(path: str) -> str:
        return os.path.abspath(os.path.expanduser(path))
