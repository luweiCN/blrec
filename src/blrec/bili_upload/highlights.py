from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import os
import secrets
import shutil
import sqlite3
import time
import uuid
from dataclasses import dataclass, replace
from functools import partial
from pathlib import Path
from typing import (
    Callable,
    Dict,
    List,
    Literal,
    Mapping,
    Optional,
    Sequence,
    Set,
    Tuple,
    Union,
    cast,
)

from blrec.logging.audit import audit

from .database import BiliUploadDatabase
from .highlight_cut import (
    ClipInspection,
    ClipSource,
    HighlightCutError,
    LosslessClipper,
)


class HighlightRangeUnavailable(RuntimeError):
    pass


class HighlightInspectionBusy(RuntimeError):
    pass


class HighlightInspectionConflict(RuntimeError):
    pass


class HighlightConfirmationRequired(RuntimeError):
    def __init__(self, inspection: ClipInspection) -> None:
        super().__init__(
            '无损剪辑会额外保留 {:.1f} 秒，请确认后继续'.format(
                inspection.extra_lead_ms / 1000.0
            )
        )
        self.inspection = inspection
        self.extra_lead_ms = inspection.extra_lead_ms


@dataclass(frozen=True)
class HighlightMarker:
    id: int
    room_id: int
    observed_at_ms: int
    player_delay_ms: int
    content_at_ms: int
    title: str
    anchor_name: str
    name: str
    note: str
    source: str
    created_at: int
    updated_at: int
    recording_part_id: Optional[int] = None
    part_anchor_at_ms: Optional[int] = None
    current_time_ms: Optional[int] = None
    seekable_end_ms: Optional[int] = None
    raw_delay_ms: int = 0
    baseline_delay_ms: int = 0
    effective_rewind_ms: int = 0


@dataclass(frozen=True)
class HighlightMarkerCount:
    part_id: int
    count: int


@dataclass(frozen=True)
class TimelinePart:
    part_id: int
    part_index: int
    path: str
    absolute_start_at_ms: int
    timeline_start_ms: int
    duration_ms: int
    stable_end_ms: int
    recording: bool


@dataclass(frozen=True)
class MappedHighlight:
    marker: HighlightMarker
    part_id: int
    local_offset_ms: int
    timeline_offset_ms: int


@dataclass(frozen=True)
class HighlightTimeline:
    session_id: int
    room_id: int
    duration_ms: int
    stable_end_ms: int
    parts: Tuple[TimelinePart, ...]
    markers: Tuple[MappedHighlight, ...]


@dataclass(frozen=True)
class HighlightInspectionOperation:
    operation_id: str
    state: Literal['accepted', 'running', 'succeeded', 'failed']
    retry_after_ms: int = 500
    inspection: Optional[ClipInspection] = None
    inspection_token: Optional[str] = None
    error_code: Optional[str] = None


@dataclass(frozen=True)
class HighlightClipSource:
    part_id: int
    ordinal: int
    requested_start_ms: int
    requested_end_ms: int
    actual_start_ms: Optional[int]
    actual_end_ms: Optional[int]


@dataclass(frozen=True)
class HighlightClip:
    id: int
    marker_id: Optional[int]
    room_id: int
    source_session_id: Optional[int]
    upload_session_id: Optional[int]
    name: str
    requested_start_ms: int
    requested_end_ms: int
    actual_start_ms: Optional[int]
    actual_end_ms: Optional[int]
    output_video_path: Optional[str]
    output_xml_path: Optional[str]
    state: str
    confirmation_required: bool
    confirmed: bool
    error_message: Optional[str]
    attempt: int
    created_at: int
    updated_at: int
    sources: Tuple[HighlightClipSource, ...] = ()
    upload_job_id: Optional[int] = None
    upload_state: Optional[str] = None
    upload_percent: Optional[float] = None
    upload_bvid: Optional[str] = None
    source_anchor_name: str = ''
    source_title: str = ''
    duration_ms: int = 0
    file_size_bytes: Optional[int] = None
    deletion_state: str = 'none'
    deletion_error: Optional[str] = None


@dataclass(frozen=True)
class HighlightClipSummary:
    id: int
    room_id: int
    source_session_id: Optional[int]
    name: str
    state: str
    error_message: Optional[str]
    created_at: int
    updated_at: int
    source_anchor_name: str
    source_title: str
    duration_ms: int
    file_size_bytes: Optional[int]
    upload_job_id: Optional[int]
    upload_state: Optional[str]
    upload_percent: Optional[float]
    upload_bvid: Optional[str]
    deletion_state: str = 'none'
    deletion_error: Optional[str] = None


@dataclass(frozen=True)
class HighlightClipMediaResource:
    clip_id: int
    name: str
    output_video_path: str
    file_size_bytes: Optional[int]
    expected_root: str


def _source_fingerprint(
    source: ClipSource, *, stable_end_ms: Optional[int] = None
) -> Dict[str, object]:
    path = Path(source.path).resolve(strict=True)
    stat = path.stat()
    if not path.is_file() or stat.st_size <= 0:
        raise HighlightCutError('剪辑源视频不存在或为空')
    return {
        'partId': source.part_id,
        'realpath': str(path),
        'device': stat.st_dev,
        'inode': stat.st_ino,
        'size': stat.st_size,
        'mtimeNs': stat.st_mtime_ns,
        'recording': source.recording,
        'validatedStartMs': source.requested_start_ms,
        'validatedEndMs': source.requested_end_ms,
        'stableEndMs': (
            source.requested_end_ms
            if stable_end_ms is None
            else max(0, int(stable_end_ms))
        ),
    }


def _fingerprint_json(
    source: ClipSource, *, stable_end_ms: Optional[int] = None
) -> str:
    return json.dumps(
        _source_fingerprint(source, stable_end_ms=stable_end_ms),
        ensure_ascii=True,
        sort_keys=True,
        separators=(',', ':'),
    )


def _fingerprint_matches(stored_json: str, current_json: str) -> bool:
    try:
        stored = json.loads(stored_json)
        current = json.loads(current_json)
    except (TypeError, ValueError):
        return False
    if not isinstance(stored, dict) or not isinstance(current, dict):
        return False
    identity_fields = (
        'partId',
        'realpath',
        'device',
        'inode',
        'validatedStartMs',
        'validatedEndMs',
    )
    if any(field not in stored or field not in current for field in identity_fields):
        return stored_json == current_json
    if any(stored[field] != current[field] for field in identity_fields):
        return False
    try:
        validated_end_ms = int(stored['validatedEndMs'])
        stored_stable_end_ms = int(stored['stableEndMs'])
        current_stable_end_ms = int(current['stableEndMs'])
        stored_size = int(stored['size'])
        current_size = int(current['size'])
    except (KeyError, TypeError, ValueError):
        return False
    if (
        stored_stable_end_ms < validated_end_ms
        or current_stable_end_ms < validated_end_ms
    ):
        return False
    if bool(stored.get('recording')):
        return current_size >= stored_size
    return current_size == stored_size and current.get('mtimeNs') == stored.get(
        'mtimeNs'
    )


def _stable_end_for_source(source: ClipSource, safe_tail_ms: int) -> int:
    duration_ms = (
        source.requested_end_ms
        if source.duration_ms is None
        else max(0, int(source.duration_ms))
    )
    return max(0, duration_ms - (safe_tail_ms if source.recording else 0))


def _inspection_json(inspection: ClipInspection) -> str:
    return json.dumps(
        {
            'requestedStartMs': inspection.requested_start_ms,
            'requestedEndMs': inspection.requested_end_ms,
            'actualStartMs': inspection.actual_start_ms,
            'actualEndMs': inspection.actual_end_ms,
            'extraLeadMs': inspection.extra_lead_ms,
            'confirmationRequired': inspection.confirmation_required,
            'sources': [
                {
                    'partId': source.part_id,
                    'actualStartMs': source.actual_start_ms,
                    'actualEndMs': source.actual_end_ms,
                    'outputOffsetMs': source.output_offset_ms,
                    'recording': source.recording,
                    'profile': {
                        'codecName': source.profile.codec_name,
                        'width': source.profile.width,
                        'height': source.profile.height,
                        'rFrameRate': source.profile.r_frame_rate,
                        'extradataSize': source.profile.extradata_size,
                        'durationMs': source.profile.duration_ms,
                        'hasAudio': source.profile.has_audio,
                    },
                }
                for source in inspection.sources
            ],
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(',', ':'),
    )


def _inspection_from_json(value: str, fingerprint_json: str) -> ClipInspection:
    from .highlight_cut import InspectedClipSource, MediaProfile

    document = json.loads(value)
    fingerprint = json.loads(fingerprint_json)
    sources = document['sources']
    if not isinstance(sources, list) or len(sources) != 1:
        raise HighlightCutError('高光检查结果无效')
    source = sources[0]
    profile = source['profile']
    if int(source['partId']) != int(fingerprint['partId']):
        raise HighlightCutError('高光检查结果与源视频不匹配')
    inspected = InspectedClipSource(
        part_id=int(source['partId']),
        path=str(fingerprint['realpath']),
        actual_start_ms=int(source['actualStartMs']),
        actual_end_ms=int(source['actualEndMs']),
        output_offset_ms=int(source['outputOffsetMs']),
        profile=MediaProfile(
            codec_name=str(profile['codecName']),
            width=int(profile['width']),
            height=int(profile['height']),
            r_frame_rate=str(profile['rFrameRate']),
            extradata_size=int(profile['extradataSize']),
            duration_ms=int(profile['durationMs']),
            has_audio=bool(profile['hasAudio']),
        ),
        recording=bool(source.get('recording', False)),
    )
    return ClipInspection(
        sources=(inspected,),
        requested_start_ms=int(document['requestedStartMs']),
        requested_end_ms=int(document['requestedEndMs']),
        actual_start_ms=int(document['actualStartMs']),
        actual_end_ms=int(document['actualEndMs']),
        extra_lead_ms=int(document['extraLeadMs']),
        confirmation_required=bool(document['confirmationRequired']),
    )


class HighlightService:
    ACTIVE_SAFE_TAIL_MS = 10_000
    INSPECTION_ACTIVE_LIMIT = 2
    INSPECTION_WAITING_LIMIT = 8
    INSPECTION_DEADLINE_SECONDS = 30.0
    INSPECTION_TOKEN_TTL_SECONDS = 120
    INSPECTION_TERMINAL_TTL_SECONDS = 300
    _CLIP_WITH_UPLOAD_SELECT = (
        'SELECT clip.*,job.id AS upload_job_id,job.state AS upload_state,'
        'job.bvid AS upload_bvid '
        'FROM highlight_clips clip '
        'LEFT JOIN upload_jobs job ON job.session_id=clip.upload_session_id '
    )
    _CLIP_SUMMARY_SELECT = (
        'WITH selected_clips AS ('
        'SELECT id FROM highlight_clips INDEXED BY highlight_clips_library_idx '
        "WHERE state!='cancelled' "
        "AND deletion_state IN ('none','failed') "
        'ORDER BY created_at DESC,id DESC LIMIT ? OFFSET ?),'
        'selected_jobs AS ('
        'SELECT job.id FROM selected_clips selected '
        'CROSS JOIN highlight_clips clip ON clip.id=selected.id '
        'CROSS JOIN upload_jobs job ON job.session_id=clip.upload_session_id),'
        'upload_summary AS ('
        'SELECT part.job_id,COALESCE(SUM(chunk.size),0) AS total_bytes,'
        "COALESCE(SUM(CASE WHEN chunk.state='confirmed' "
        'THEN chunk.size ELSE 0 END),0) AS confirmed_bytes '
        'FROM selected_jobs selected '
        'CROSS JOIN upload_parts part ON part.job_id=selected.id '
        'LEFT JOIN upload_chunks chunk ON chunk.part_id=part.id '
        'GROUP BY part.job_id) '
        'SELECT clip.id,clip.room_id,clip.source_session_id,clip.name,clip.state,'
        'clip.error_message,clip.created_at,clip.updated_at,'
        'clip.deletion_state,clip.deletion_error,'
        "COALESCE(source.anchor_name,'') AS source_anchor_name,"
        "COALESCE(source.title,'') AS source_title,"
        'MAX(0,COALESCE(NULLIF(clip.actual_end_ms,0),clip.requested_end_ms)-'
        'COALESCE(NULLIF(clip.actual_start_ms,0),clip.requested_start_ms)) '
        'AS duration_ms,clip.file_size_bytes,'
        'job.id AS upload_job_id,job.state AS upload_state,'
        'CASE WHEN job.id IS NULL THEN NULL '
        "WHEN job.state IN ('approved','completed') THEN 100.0 "
        'WHEN COALESCE(progress.total_bytes,0)<=0 THEN 0.0 '
        'ELSE ROUND(MIN(100.0,progress.confirmed_bytes*100.0/'
        'progress.total_bytes),2) END AS upload_percent,'
        'job.bvid AS upload_bvid '
        'FROM selected_clips selected '
        'CROSS JOIN highlight_clips clip ON clip.id=selected.id '
        'LEFT JOIN upload_jobs job ON job.session_id=clip.upload_session_id '
        'LEFT JOIN upload_summary progress ON progress.job_id=job.id '
        'LEFT JOIN recording_sessions source ON source.id=clip.source_session_id '
        'ORDER BY clip.created_at DESC,clip.id DESC'
    )

    def __init__(
        self,
        database: BiliUploadDatabase,
        *,
        recording_root: Optional[Path] = None,
        clip_root: Optional[Path] = None,
        clipper: Optional[LosslessClipper] = None,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
        inspection_secret: Optional[bytes] = None,
    ) -> None:
        self._database = database
        self._clip_root = (
            Path(clip_root).resolve()
            if clip_root is not None
            else (
                None
                if recording_root is None
                else Path(recording_root).resolve() / 'highlights'
            )
        )
        self._clipper = clipper
        self._clock = clock
        self._monotonic = monotonic
        self._inspection_secret = inspection_secret or secrets.token_bytes(32)
        self._inspection_queue: Optional[asyncio.Queue[str]] = None
        self._inspection_workers: Tuple[asyncio.Task[None], ...] = ()
        self._inspection_lifecycle_lock = asyncio.Lock()
        self._inspection_admission_lock = asyncio.Lock()
        self._inspection_admission_open = True
        self._scheduled_inspections: Set[str] = set()
        self._probe_futures: Dict[str, asyncio.Future[Tuple[str, str]]] = {}

    async def start(self) -> None:
        async with self._inspection_lifecycle_lock:
            if self._inspection_workers:
                return
            async with self._inspection_admission_lock:
                self._inspection_admission_open = True
            now = int(self._clock())
            await self._database.execute(
                "UPDATE highlight_inspections SET state='accepted',updated_at=? "
                "WHERE state='running'",
                (now,),
            )
            await self._cleanup_expired_inspections(now)
            self._inspection_queue = asyncio.Queue(
                maxsize=self.INSPECTION_WAITING_LIMIT
            )
            self._inspection_workers = tuple(
                asyncio.create_task(self._inspection_worker_loop())
                for _index in range(self.INSPECTION_ACTIVE_LIMIT)
            )
            rows = await self._database.fetchall(
                "SELECT operation_id FROM highlight_inspections "
                "WHERE state='accepted' ORDER BY created_at,operation_id"
            )
            for row in rows:
                await self._enqueue_inspection(str(row['operation_id']))

    async def shutdown(self) -> None:
        async with self._inspection_lifecycle_lock:
            async with self._inspection_admission_lock:
                self._inspection_admission_open = False
            queue = self._inspection_queue
            workers = self._inspection_workers
        if queue is None or not workers:
            return
        await queue.join()
        for _worker in workers:
            await queue.put('')
        await asyncio.gather(*workers)
        async with self._inspection_lifecycle_lock:
            self._inspection_workers = ()
            self._inspection_queue = None
            self._scheduled_inspections.clear()
            self._probe_futures.clear()

    async def submit_clip_inspection(
        self,
        *,
        session_id: int,
        requested_start_ms: int,
        requested_end_ms: int,
        active_durations_ms: Mapping[int, int],
        idempotency_key: str,
    ) -> HighlightInspectionOperation:
        self._validate_idempotency_key(idempotency_key)
        if requested_start_ms < 0 or requested_end_ms <= requested_start_ms:
            raise HighlightRangeUnavailable('高光剪辑时间范围无效')
        await self.start()
        async with self._inspection_admission_lock:
            if not self._inspection_admission_open:
                raise HighlightInspectionBusy('高光检查服务正在关闭')
            return await self._admit_clip_inspection(
                session_id=session_id,
                requested_start_ms=requested_start_ms,
                requested_end_ms=requested_end_ms,
                active_durations_ms=active_durations_ms,
                idempotency_key=idempotency_key,
            )

    async def _admit_clip_inspection(
        self,
        *,
        session_id: int,
        requested_start_ms: int,
        requested_end_ms: int,
        active_durations_ms: Mapping[int, int],
        idempotency_key: str,
    ) -> HighlightInspectionOperation:
        now = int(self._clock())
        await self._cleanup_expired_inspections(now)
        active_json = json.dumps(
            {str(key): int(value) for key, value in active_durations_ms.items()},
            sort_keys=True,
            separators=(',', ':'),
        )

        def admit(connection: sqlite3.Connection) -> Tuple[sqlite3.Row, bool]:
            existing = connection.execute(
                'SELECT * FROM highlight_inspections WHERE idempotency_key=?',
                (idempotency_key,),
            ).fetchone()
            if existing is not None:
                if (
                    int(existing['session_id']) != session_id
                    or int(existing['requested_start_ms']) != requested_start_ms
                    or int(existing['requested_end_ms']) != requested_end_ms
                ):
                    raise HighlightInspectionConflict(
                        '幂等键已经绑定到另一个高光检查范围'
                    )
                if (
                    str(existing['state']) == 'succeeded'
                    and existing['token_expires_at'] is not None
                    and int(existing['token_expires_at']) <= now
                    and existing['token_consumed_at'] is None
                ):
                    connection.execute(
                        "UPDATE highlight_inspections SET state='accepted',"
                        'active_durations_json=?,result_json=NULL,error_code=NULL,'
                        'fingerprint_json=NULL,claim_key_hash=NULL,token_hash=NULL,'
                        'token_expires_at=NULL,terminal_expires_at=NULL,updated_at=? '
                        'WHERE operation_id=?',
                        (active_json, now, str(existing['operation_id'])),
                    )
                    existing = connection.execute(
                        'SELECT * FROM highlight_inspections WHERE operation_id=?',
                        (str(existing['operation_id']),),
                    ).fetchone()
                    assert existing is not None
                    return existing, True
                return existing, False
            admitted = int(
                connection.execute(
                    "SELECT COUNT(*) FROM highlight_inspections "
                    "WHERE state IN ('accepted','running')"
                ).fetchone()[0]
            )
            if admitted >= self.INSPECTION_ACTIVE_LIMIT + self.INSPECTION_WAITING_LIMIT:
                raise HighlightInspectionBusy('高光检查队列繁忙，请稍后重试')
            operation_id = str(uuid.uuid4())
            connection.execute(
                'INSERT INTO highlight_inspections('
                'operation_id,session_id,requested_start_ms,requested_end_ms,'
                'idempotency_key,state,active_durations_json,created_at,updated_at) '
                "VALUES(?,?,?,?,?,'accepted',?,?,?)",
                (
                    operation_id,
                    session_id,
                    requested_start_ms,
                    requested_end_ms,
                    idempotency_key,
                    active_json,
                    now,
                    now,
                ),
            )
            row = connection.execute(
                'SELECT * FROM highlight_inspections WHERE operation_id=?',
                (operation_id,),
            ).fetchone()
            assert row is not None
            return row, True

        row, enqueue = await self._database.write(admit)
        if enqueue or str(row['state']) == 'accepted':
            await self._enqueue_inspection(str(row['operation_id']))
        return self._operation_from_row(row)

    async def get_clip_inspection(
        self, operation_id: str, *, claim_key: Optional[str]
    ) -> HighlightInspectionOperation:
        row = await self._database.fetchone(
            'SELECT * FROM highlight_inspections WHERE operation_id=?', (operation_id,)
        )
        if row is None:
            raise ValueError("unknown highlight inspection '{}'".format(operation_id))
        if str(row['state']) != 'succeeded':
            return self._operation_from_row(row)
        if not claim_key:
            raise HighlightInspectionConflict('领取检查结果需要 claim key')
        return await self._claim_inspection_token(operation_id, claim_key)

    async def _enqueue_inspection(self, operation_id: str) -> None:
        queue = self._inspection_queue
        if queue is None or operation_id in self._scheduled_inspections:
            return
        self._scheduled_inspections.add(operation_id)
        await queue.put(operation_id)

    async def _inspection_worker_loop(self) -> None:
        queue = self._inspection_queue
        assert queue is not None
        while True:
            operation_id = await queue.get()
            try:
                if not operation_id:
                    return
                await self._run_inspection(operation_id)
            finally:
                if operation_id:
                    self._scheduled_inspections.discard(operation_id)
                queue.task_done()

    async def _run_inspection(self, operation_id: str) -> None:
        now = int(self._clock())

        def begin(connection: sqlite3.Connection) -> Optional[sqlite3.Row]:
            updated = connection.execute(
                "UPDATE highlight_inspections SET state='running',updated_at=? "
                "WHERE operation_id=? AND state='accepted'",
                (now, operation_id),
            )
            if updated.rowcount != 1:
                return None
            return connection.execute(
                'SELECT * FROM highlight_inspections WHERE operation_id=?',
                (operation_id,),
            ).fetchone()

        row = await self._database.write(begin)
        if row is None:
            return
        try:
            durations_document = json.loads(str(row['active_durations_json']))
            durations = {
                int(key): int(value) for key, value in durations_document.items()
            }
            _timeline, _ranges, sources = await self._prepare_clip_sources(
                session_id=int(row['session_id']),
                requested_start_ms=int(row['requested_start_ms']),
                requested_end_ms=int(row['requested_end_ms']),
                active_durations_ms=durations,
            )
            source = sources[0]
            fingerprint = await asyncio.get_running_loop().run_in_executor(
                None,
                partial(
                    _fingerprint_json,
                    source,
                    stable_end_ms=_stable_end_for_source(
                        source, self.ACTIVE_SAFE_TAIL_MS
                    ),
                ),
            )
            cache_key = '{}:{}:{}'.format(
                fingerprint,
                int(row['requested_start_ms']),
                int(row['requested_end_ms']),
            )
            reusable_rows = await self._database.fetchall(
                'SELECT result_json,fingerprint_json FROM highlight_inspections '
                "WHERE state='succeeded' AND session_id=? "
                'AND requested_start_ms=? AND requested_end_ms=? '
                'AND fingerprint_json IS NOT NULL AND result_json IS NOT NULL '
                'ORDER BY updated_at DESC LIMIT 32',
                (
                    int(row['session_id']),
                    int(row['requested_start_ms']),
                    int(row['requested_end_ms']),
                ),
            )
            reusable = next(
                (
                    candidate
                    for candidate in reusable_rows
                    if _fingerprint_matches(
                        str(candidate['fingerprint_json']), fingerprint
                    )
                ),
                None,
            )
            if reusable is not None:
                result_json = str(reusable['result_json'])
                _inspection_from_json(result_json, str(reusable['fingerprint_json']))
                probe_future = None
            else:
                future = self._probe_futures.get(cache_key)
                if future is None:
                    deadline = self._monotonic() + self.INSPECTION_DEADLINE_SECONDS
                    future = asyncio.ensure_future(
                        asyncio.get_running_loop().run_in_executor(
                            None,
                            partial(
                                self._inspect_sources_sync,
                                sources,
                                int(row['requested_start_ms']),
                                int(row['requested_end_ms']),
                                deadline,
                                fingerprint,
                            ),
                        )
                    )
                    self._probe_futures[cache_key] = future
                    future.add_done_callback(
                        partial(self._forget_unsuccessful_probe_future, cache_key)
                    )
                probe_future = future
                result_json, fingerprint = await asyncio.shield(future)
            completed_at = int(self._clock())
            updated = await self._database.execute(
                "UPDATE highlight_inspections SET state='succeeded',result_json=?,"
                'fingerprint_json=?,error_code=NULL,terminal_expires_at=?,updated_at=? '
                "WHERE operation_id=? AND state='running'",
                (
                    result_json,
                    fingerprint,
                    completed_at + self.INSPECTION_TERMINAL_TTL_SECONDS,
                    completed_at,
                    operation_id,
                ),
            )
            if updated == 1 and probe_future is not None:
                self._forget_probe_future(cache_key, probe_future)
            audit(
                'highlight_inspection_completed',
                operation_id=operation_id,
                result='succeeded',
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            failed_at = int(self._clock())
            error_code = self._inspection_error_code(error)
            await self._database.execute(
                "UPDATE highlight_inspections SET state='failed',result_json=NULL,"
                'error_code=?,terminal_expires_at=?,updated_at=? '
                "WHERE operation_id=? AND state='running'",
                (
                    error_code,
                    failed_at + self.INSPECTION_TERMINAL_TTL_SECONDS,
                    failed_at,
                    operation_id,
                ),
            )
            audit(
                'highlight_inspection_failed',
                level='WARNING',
                operation_id=operation_id,
                error_code=error_code,
                result='failed',
            )

    def _inspect_sources_sync(
        self,
        sources: Tuple[ClipSource, ...],
        requested_start_ms: int,
        requested_end_ms: int,
        deadline: float,
        fingerprint: str,
    ) -> Tuple[str, str]:
        clipper = self._clipper
        if clipper is None:
            raise RuntimeError('highlight clipping is not configured')
        inspection = clipper.inspect(
            sources,
            requested_start_ms=requested_start_ms,
            requested_end_ms=requested_end_ms,
            stable_end_ms=requested_end_ms,
            deadline_monotonic=deadline,
        )
        return _inspection_json(inspection), fingerprint

    async def _claim_inspection_token(
        self, operation_id: str, claim_key: str
    ) -> HighlightInspectionOperation:
        if not claim_key or len(claim_key) > 200:
            raise HighlightInspectionConflict('claim key 无效')
        now = int(self._clock())
        claim_hash = hashlib.sha256(claim_key.encode('utf8')).hexdigest()

        def claim(connection: sqlite3.Connection) -> sqlite3.Row:
            row = connection.execute(
                'SELECT * FROM highlight_inspections WHERE operation_id=?',
                (operation_id,),
            ).fetchone()
            if row is None:
                raise ValueError(
                    "unknown highlight inspection '{}'".format(operation_id)
                )
            if str(row['state']) != 'succeeded':
                return row
            if row['token_consumed_at'] is not None:
                raise HighlightInspectionConflict('检查令牌已经使用')
            existing_claim = row['claim_key_hash']
            expires_at = (
                None
                if row['token_expires_at'] is None
                else int(row['token_expires_at'])
            )
            if existing_claim is not None and str(existing_claim) != claim_hash:
                raise HighlightInspectionConflict('检查结果已经由另一个请求领取')
            if expires_at is not None and expires_at <= now:
                raise HighlightInspectionConflict('检查令牌已经过期，请重新检查')
            if expires_at is None:
                expires_at = now + self.INSPECTION_TOKEN_TTL_SECONDS
                token = self._make_inspection_token(
                    operation_id, str(row['idempotency_key']), claim_key, expires_at
                )
                connection.execute(
                    'UPDATE highlight_inspections SET claim_key_hash=?,token_hash=?,'
                    'token_expires_at=?,terminal_expires_at='
                    'MAX(COALESCE(terminal_expires_at,0),?),updated_at=? '
                    'WHERE operation_id=?',
                    (
                        claim_hash,
                        self._token_hash(token),
                        expires_at,
                        expires_at,
                        now,
                        operation_id,
                    ),
                )
                row = connection.execute(
                    'SELECT * FROM highlight_inspections WHERE operation_id=?',
                    (operation_id,),
                ).fetchone()
                assert row is not None
            return row

        row = await self._database.write(claim)
        expires_at = int(row['token_expires_at'])
        token = self._make_inspection_token(
            operation_id, str(row['idempotency_key']), claim_key, expires_at
        )
        if not hmac.compare_digest(str(row['token_hash']), self._token_hash(token)):
            raise HighlightInspectionConflict('检查令牌状态无效')
        return self._operation_from_row(row, inspection_token=token)

    async def _cleanup_expired_inspections(self, now: int) -> None:
        await self._database.execute(
            'DELETE FROM highlight_inspections WHERE operation_id IN ('
            'SELECT operation_id FROM highlight_inspections '
            "WHERE state IN ('succeeded','failed') AND terminal_expires_at<=? "
            'AND (token_expires_at IS NULL OR token_expires_at<=? '
            'OR token_consumed_at IS NOT NULL) '
            'ORDER BY terminal_expires_at,operation_id LIMIT 32)',
            (now, now),
        )

    def _forget_probe_future(
        self, cache_key: str, future: asyncio.Future[Tuple[str, str]]
    ) -> None:
        if self._probe_futures.get(cache_key) is future:
            self._probe_futures.pop(cache_key, None)

    def _forget_unsuccessful_probe_future(
        self, cache_key: str, future: asyncio.Future[Tuple[str, str]]
    ) -> None:
        if future.cancelled():
            self._forget_probe_future(cache_key, future)
            return
        if future.exception() is not None:
            self._forget_probe_future(cache_key, future)

    def _make_inspection_token(
        self, operation_id: str, idempotency_key: str, claim_key: str, expiry: int
    ) -> str:
        message = '{}|{}|{}|{}'.format(
            operation_id, idempotency_key, claim_key, expiry
        ).encode('utf8')
        digest = hmac.new(self._inspection_secret, message, hashlib.sha256).digest()
        return base64.urlsafe_b64encode(digest).rstrip(b'=').decode('ascii')

    @staticmethod
    def _token_hash(token: str) -> str:
        return hashlib.sha256(token.encode('ascii')).hexdigest()

    @staticmethod
    def _validate_idempotency_key(value: str) -> None:
        try:
            parsed = uuid.UUID(value)
        except (AttributeError, TypeError, ValueError) as error:
            raise ValueError('idempotency key must be a UUID') from error
        if str(parsed) != value.lower():
            raise ValueError('idempotency key must use canonical UUID format')

    @staticmethod
    def _inspection_error_code(error: Exception) -> str:
        if isinstance(error, HighlightRangeUnavailable):
            return 'range_unavailable'
        if isinstance(error, HighlightCutError) and '超时' in str(error):
            return 'probe_timeout'
        if isinstance(error, HighlightCutError):
            return 'probe_failed'
        return 'inspection_failed'

    @staticmethod
    def _operation_from_row(
        row: sqlite3.Row, *, inspection_token: Optional[str] = None
    ) -> HighlightInspectionOperation:
        result_json = row['result_json']
        fingerprint_json = row['fingerprint_json']
        inspection = (
            None
            if result_json is None or fingerprint_json is None
            else _inspection_from_json(str(result_json), str(fingerprint_json))
        )
        state = str(row['state'])
        if state not in ('accepted', 'running', 'succeeded', 'failed'):
            raise RuntimeError('invalid highlight inspection state')
        return HighlightInspectionOperation(
            operation_id=str(row['operation_id']),
            state=cast(Literal['accepted', 'running', 'succeeded', 'failed'], state),
            inspection=inspection,
            inspection_token=inspection_token,
            error_code=None if row['error_code'] is None else str(row['error_code']),
        )

    async def migrate_legacy_outputs(self, recording_root: Path) -> int:
        if self._clip_root is None:
            return 0
        legacy_root = (Path(recording_root).resolve() / 'highlights').resolve()
        rows = await self._database.fetchall(
            'SELECT id,room_id,upload_session_id,output_video_path,output_xml_path '
            'FROM highlight_clips WHERE output_video_path IS NOT NULL '
            'OR output_xml_path IS NOT NULL ORDER BY id'
        )
        migrated = 0
        for row in rows:
            values = (row['output_video_path'], row['output_xml_path'])
            mappings: List[Tuple[Optional[Path], Optional[Path]]] = []
            eligible = False
            for value in values:
                if value is None:
                    mappings.append((None, None))
                    continue
                source = Path(str(value)).resolve(strict=False)
                try:
                    source.relative_to(legacy_root)
                except ValueError:
                    mappings.append((source, source))
                    continue
                eligible = True
                target = self._clip_root / str(int(row['room_id'])) / source.name
                mappings.append((source, target.resolve(strict=False)))
            if not eligible:
                continue
            copied = await asyncio.get_running_loop().run_in_executor(
                None, self._copy_legacy_outputs, tuple(mappings)
            )
            if not copied:
                audit(
                    'highlight_clip_migration_skipped',
                    level='WARNING',
                    clip_id=int(row['id']),
                    reason='source_missing',
                )
                continue
            video_target = mappings[0][1]
            xml_target = mappings[1][1]
            video_path = None if video_target is None else str(video_target)
            xml_path = None if xml_target is None else str(xml_target)

            def update_paths(connection: sqlite3.Connection) -> int:
                cursor = connection.execute(
                    'UPDATE highlight_clips SET output_video_path=?,output_xml_path=? '
                    'WHERE id=? AND output_video_path IS ? AND output_xml_path IS ?',
                    (
                        video_path,
                        xml_path,
                        int(row['id']),
                        row['output_video_path'],
                        row['output_xml_path'],
                    ),
                )
                upload_session_id = row['upload_session_id']
                if (
                    cursor.rowcount != 1
                    or upload_session_id is None
                    or video_path is None
                ):
                    return cursor.rowcount
                connection.execute(
                    'UPDATE recording_parts SET source_path=?,final_path=?,xml_path=? '
                    'WHERE session_id=?',
                    (video_path, video_path, xml_path, int(upload_session_id)),
                )
                connection.execute(
                    'UPDATE upload_parts SET source_path=?,final_path=?,xml_path=?,'
                    'file_identity=NULL WHERE job_id IN('
                    'SELECT id FROM upload_jobs WHERE session_id=?)',
                    (video_path, video_path, xml_path, int(upload_session_id)),
                )
                return cursor.rowcount

            updated = await self._database.write(update_paths)
            if updated == 1:
                migrated += 1
                audit(
                    'highlight_clip_migrated',
                    clip_id=int(row['id']),
                    room_id=int(row['room_id']),
                    result='copied_to_clip_library',
                )
        return migrated

    @staticmethod
    def _copy_legacy_outputs(
        mappings: Tuple[Tuple[Optional[Path], Optional[Path]], ...]
    ) -> bool:
        for source, target in mappings:
            if source is None or target is None or source == target:
                continue
            source_partial = Path(str(source) + '.partial')
            target_partial = Path(str(target) + '.partial')
            if not source.exists() and not target.exists():
                if not source_partial.exists() and not target_partial.exists():
                    return False
        for source, target in mappings:
            if source is None or target is None or source == target:
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            for current_source, current_target in (
                (source, target),
                (Path(str(source) + '.partial'), Path(str(target) + '.partial')),
            ):
                if not current_source.exists():
                    continue
                temporary = Path(str(current_target) + '.migrating')
                shutil.copy2(str(current_source), str(temporary))
                os.replace(str(temporary), str(current_target))
        return True

    async def create_marker(
        self,
        *,
        room_id: int,
        observed_at_ms: int,
        player_delay_ms: int,
        title: str,
        anchor_name: str,
        source: str,
        current_time_ms: Optional[int] = None,
        seekable_end_ms: Optional[int] = None,
        raw_delay_ms: int = 0,
        baseline_delay_ms: int = 0,
        effective_rewind_ms: Optional[int] = None,
        name: str = '',
    ) -> HighlightMarker:
        if room_id <= 0:
            raise ValueError('room_id must be positive')
        if observed_at_ms <= 0:
            raise ValueError('observed_at_ms must be positive')
        if source not in ('web', 'browser_extension'):
            raise ValueError('invalid highlight source')
        delay_ms = min(300_000, max(0, int(player_delay_ms)))
        raw_delay = min(86_400_000, max(0, int(raw_delay_ms)))
        baseline_delay = min(86_400_000, max(0, int(baseline_delay_ms)))
        rewind_ms = (
            delay_ms
            if effective_rewind_ms is None
            else min(86_400_000, max(0, int(effective_rewind_ms)))
        )
        current_time = self._optional_nonnegative(current_time_ms)
        seekable_end = self._optional_nonnegative(seekable_end_ms)
        clock_now = self._clock()
        received_at_ms = int(clock_now * 1000)
        content_at_ms = received_at_ms - rewind_ms
        if content_at_ms <= 0:
            raise ValueError('highlight content time must be positive')
        now = int(clock_now)
        normalized_name = name.strip()
        if len(normalized_name) > 200:
            raise ValueError('highlight name must not exceed 200 characters')
        marker_name = normalized_name or self._default_name(title, content_at_ms)
        active_part = await self._database.fetchone(
            'SELECT part.id,COALESCE(part.timeline_start_at_ms,'
            'part.record_start_time*1000) AS anchor_at_ms '
            'FROM recording_parts part '
            'JOIN recording_sessions session ON session.id=part.session_id '
            "WHERE session.room_id=? AND session.source_kind='live' "
            "AND session.state='open' AND part.video_deleted_at IS NULL "
            "AND part.artifact_state IN ('recording','postprocessing','ready') "
            'AND COALESCE(part.timeline_start_at_ms,'
            'part.record_start_time*1000)<=? '
            'ORDER BY anchor_at_ms DESC,part.id DESC LIMIT 1',
            (room_id, received_at_ms),
        )
        recording_part_id = None if active_part is None else int(active_part['id'])
        part_anchor_at_ms = (
            None if active_part is None else int(active_part['anchor_at_ms'])
        )

        def write(connection: sqlite3.Connection) -> sqlite3.Row:
            cursor = connection.execute(
                'INSERT INTO highlight_markers('
                'room_id,observed_at_ms,player_delay_ms,content_at_ms,title,'
                'anchor_name,name,note,source,created_at,updated_at,'
                'recording_part_id,part_anchor_at_ms,current_time_ms,'
                'seekable_end_ms,raw_delay_ms,baseline_delay_ms,'
                'effective_rewind_ms) '
                "VALUES(?,?,?,?,?,?,?,'',?,?,?,?,?,?,?,?,?,?)",
                (
                    room_id,
                    int(observed_at_ms),
                    delay_ms,
                    content_at_ms,
                    title,
                    anchor_name,
                    marker_name,
                    source,
                    now,
                    now,
                    recording_part_id,
                    part_anchor_at_ms,
                    current_time,
                    seekable_end,
                    raw_delay,
                    baseline_delay,
                    rewind_ms,
                ),
            )
            row = connection.execute(
                'SELECT * FROM highlight_markers WHERE id=?', (int(cursor.lastrowid),)
            ).fetchone()
            assert row is not None
            return row

        row = await self._database.write(write)
        marker = self._marker_from_row(row)
        audit(
            'highlight_marker_created',
            marker_id=marker.id,
            room_id=room_id,
            observed_at_ms=observed_at_ms,
            player_delay_ms=delay_ms,
            raw_delay_ms=raw_delay,
            baseline_delay_ms=baseline_delay,
            effective_rewind_ms=rewind_ms,
            recording_part_id=recording_part_id,
            part_anchor_at_ms=part_anchor_at_ms,
            content_at_ms=content_at_ms,
            source=source,
            result='saved',
        )
        return marker

    async def update_marker(
        self, marker_id: int, name: str, note: str
    ) -> HighlightMarker:
        normalized_name = name.strip()
        if not normalized_name or len(normalized_name) > 200:
            raise ValueError('highlight name must contain 1 to 200 characters')
        if len(note) > 1000:
            raise ValueError('highlight note must not exceed 1000 characters')
        now = int(self._clock())
        changed = await self._database.execute(
            'UPDATE highlight_markers SET name=?,note=?,updated_at=? WHERE id=?',
            (normalized_name, note, now, marker_id),
        )
        if changed != 1:
            raise ValueError("unknown highlight marker '{}'".format(marker_id))
        row = await self._database.fetchone(
            'SELECT * FROM highlight_markers WHERE id=?', (marker_id,)
        )
        assert row is not None
        marker = self._marker_from_row(row)
        audit(
            'highlight_marker_updated',
            marker_id=marker_id,
            room_id=marker.room_id,
            result='saved',
        )
        return marker

    async def delete_marker(self, marker_id: int) -> None:
        row = await self._database.fetchone(
            'SELECT room_id FROM highlight_markers WHERE id=?', (marker_id,)
        )
        if row is None:
            raise ValueError("unknown highlight marker '{}'".format(marker_id))
        await self._database.execute(
            'DELETE FROM highlight_markers WHERE id=?', (marker_id,)
        )
        audit(
            'highlight_marker_deleted',
            marker_id=marker_id,
            room_id=int(row['room_id']),
            result='deleted',
        )

    async def create_clip(
        self,
        *,
        session_id: int,
        marker_id: Optional[int],
        name: str,
        requested_start_ms: int,
        requested_end_ms: int,
        confirm_keyframe: bool,
        active_durations_ms: Mapping[int, int],
        inspection_token: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> Union[HighlightClip, HighlightInspectionOperation]:
        normalized_name = name.strip()
        if not normalized_name or len(normalized_name) > 200:
            raise ValueError('highlight clip name must contain 1 to 200 characters')
        if self._clip_root is None or self._clipper is None:
            raise RuntimeError('highlight clipping is not configured')
        inspection_row: Optional[sqlite3.Row] = None
        fingerprint: Optional[str] = None
        if idempotency_key is not None:
            self._validate_idempotency_key(idempotency_key)
            existing = await self._database.fetchone(
                'SELECT id FROM highlight_clips WHERE idempotency_key=?',
                (idempotency_key,),
            )
            if existing is not None:
                return await self.get_clip(int(existing['id']))
            if not inspection_token:
                return await self.submit_clip_inspection(
                    session_id=session_id,
                    requested_start_ms=requested_start_ms,
                    requested_end_ms=requested_end_ms,
                    active_durations_ms=active_durations_ms,
                    idempotency_key=idempotency_key,
                )
            _timeline, source_ranges, clip_sources = await self._prepare_clip_sources(
                session_id=session_id,
                requested_start_ms=requested_start_ms,
                requested_end_ms=requested_end_ms,
                active_durations_ms=active_durations_ms,
            )
            fingerprint = await asyncio.get_running_loop().run_in_executor(
                None, _fingerprint_json, clip_sources[0]
            )
            assert fingerprint is not None
            inspection_row = await self._database.fetchone(
                'SELECT * FROM highlight_inspections WHERE token_hash=?',
                (self._token_hash(inspection_token),),
            )
            if inspection_row is None:
                raise HighlightInspectionConflict('检查令牌无效')
            if str(inspection_row['idempotency_key']) != idempotency_key:
                raise HighlightInspectionConflict('检查令牌与幂等键绑定不一致')
            if (
                int(inspection_row['session_id']) != session_id
                or int(inspection_row['requested_start_ms']) != requested_start_ms
                or int(inspection_row['requested_end_ms']) != requested_end_ms
            ):
                raise HighlightInspectionConflict('检查令牌与剪辑范围绑定不一致')
            if str(inspection_row['state']) != 'succeeded':
                raise HighlightInspectionConflict('检查结果尚未就绪')
            if not _fingerprint_matches(
                str(inspection_row['fingerprint_json']), fingerprint
            ):
                return await self._restart_stale_inspection(
                    inspection_row, active_durations_ms=active_durations_ms
                )
            inspection = _inspection_from_json(
                str(inspection_row['result_json']), fingerprint
            )
        else:
            _timeline, source_ranges, inspection = await self._prepare_clip(
                session_id=session_id,
                requested_start_ms=requested_start_ms,
                requested_end_ms=requested_end_ms,
                active_durations_ms=active_durations_ms,
            )
        if inspection.confirmation_required and not confirm_keyframe:
            raise HighlightConfirmationRequired(inspection)

        now = int(self._clock())
        root = self._clip_root
        inspected_by_part = {source.part_id: source for source in inspection.sources}

        def write(connection: sqlite3.Connection) -> int:
            if idempotency_key is not None:
                existing_clip = connection.execute(
                    'SELECT id FROM highlight_clips WHERE idempotency_key=?',
                    (idempotency_key,),
                ).fetchone()
                if existing_clip is not None:
                    return int(existing_clip['id'])
                assert inspection_token is not None
                assert fingerprint is not None
                operation = connection.execute(
                    'SELECT * FROM highlight_inspections WHERE token_hash=?',
                    (self._token_hash(inspection_token),),
                ).fetchone()
                if operation is None:
                    raise HighlightInspectionConflict('检查令牌无效')
                if str(operation['idempotency_key']) != idempotency_key:
                    raise HighlightInspectionConflict('检查令牌与幂等键绑定不一致')
                expires_at = operation['token_expires_at']
                if (
                    str(operation['state']) != 'succeeded'
                    or operation['token_consumed_at'] is not None
                    or expires_at is None
                    or int(expires_at) <= now
                    or not _fingerprint_matches(
                        str(operation['fingerprint_json']), fingerprint
                    )
                ):
                    raise HighlightInspectionConflict('检查令牌已失效')
            session = connection.execute(
                'SELECT room_id,source_kind FROM recording_sessions WHERE id=?',
                (session_id,),
            ).fetchone()
            if session is None or str(session['source_kind']) != 'live':
                raise HighlightRangeUnavailable('录制场次已经不存在')
            room_id = int(session['room_id'])
            if marker_id is not None:
                marker = connection.execute(
                    'SELECT room_id FROM highlight_markers WHERE id=?', (marker_id,)
                ).fetchone()
                if marker is None or int(marker['room_id']) != room_id:
                    raise HighlightRangeUnavailable('高光标记与录像房间不匹配')
            for part, local_start_ms, local_end_ms in source_ranges:
                current = connection.execute(
                    'SELECT session_id,source_path,final_path,artifact_state,'
                    'record_duration_seconds,video_deleted_at '
                    'FROM recording_parts WHERE id=?',
                    (part.part_id,),
                ).fetchone()
                if (
                    current is None
                    or int(current['session_id']) != session_id
                    or current['video_deleted_at'] is not None
                    or str(current['artifact_state'])
                    not in ('recording', 'postprocessing', 'ready')
                    or self._available_path(current) != part.path
                ):
                    raise HighlightRangeUnavailable('源录像状态已经发生变化')
                current_duration_ms = (
                    max(0, int(active_durations_ms[part.part_id]))
                    if part.part_id in active_durations_ms
                    else (
                        0
                        if current['record_duration_seconds'] is None
                        else max(0, int(current['record_duration_seconds']) * 1000)
                    )
                )
                current_recording = (
                    str(current['artifact_state']) == 'recording'
                    or part.part_id in active_durations_ms
                )
                current_stable_ms = current_duration_ms - (
                    self.ACTIVE_SAFE_TAIL_MS if current_recording else 0
                )
                if local_end_ms > max(0, current_stable_ms):
                    raise HighlightRangeUnavailable('所选范围进入录制中的最后 10 秒')
                if local_start_ms < 0 or local_end_ms <= local_start_ms:
                    raise HighlightRangeUnavailable('源录像时间范围已经发生变化')
            cursor = connection.execute(
                'INSERT INTO highlight_clips('
                'marker_id,room_id,source_session_id,name,requested_start_ms,'
                'requested_end_ms,actual_start_ms,actual_end_ms,state,'
                'keyframe_confirmation_required,keyframe_confirmed,'
                'next_attempt_at,created_at,updated_at,inspection_json,'
                'source_fingerprint_json,idempotency_key) '
                "VALUES(?,?,?,?,?,?,?,?,'queued',?,?,?,?,?,?,?,?)",
                (
                    marker_id,
                    room_id,
                    session_id,
                    normalized_name,
                    requested_start_ms,
                    requested_end_ms,
                    inspection.actual_start_ms,
                    inspection.actual_end_ms,
                    int(inspection.confirmation_required),
                    int(inspection.confirmation_required and confirm_keyframe),
                    0,
                    now,
                    now,
                    _inspection_json(inspection),
                    fingerprint,
                    idempotency_key,
                ),
            )
            clip_id = int(cursor.lastrowid)
            if idempotency_key is not None:
                assert inspection_row is not None
                consumed = connection.execute(
                    'UPDATE highlight_inspections SET token_consumed_at=?,updated_at=? '
                    'WHERE operation_id=? AND token_consumed_at IS NULL',
                    (now, now, str(inspection_row['operation_id'])),
                )
                if consumed.rowcount != 1:
                    raise HighlightInspectionConflict('检查令牌已失效')
            output_directory = root / str(room_id)
            output_video_path = output_directory / 'highlight-{}.mp4'.format(clip_id)
            output_xml_path = output_directory / 'highlight-{}.xml'.format(clip_id)
            connection.execute(
                'UPDATE highlight_clips SET output_video_path=?,output_xml_path=? '
                'WHERE id=?',
                (str(output_video_path), str(output_xml_path), clip_id),
            )
            for ordinal, (part, local_start_ms, local_end_ms) in enumerate(
                source_ranges, start=1
            ):
                inspected = inspected_by_part[part.part_id]
                connection.execute(
                    'INSERT INTO highlight_clip_sources('
                    'clip_id,part_id,ordinal,requested_start_ms,'
                    'requested_end_ms,actual_start_ms,actual_end_ms) '
                    'VALUES(?,?,?,?,?,?,?)',
                    (
                        clip_id,
                        part.part_id,
                        ordinal,
                        local_start_ms,
                        local_end_ms,
                        inspected.actual_start_ms,
                        inspected.actual_end_ms,
                    ),
                )
            return clip_id

        clip_id = await self._database.write(write)
        clip = await self.get_clip(clip_id)
        audit(
            'highlight_clip_queued',
            clip_id=clip_id,
            room_id=clip.room_id,
            session_id=session_id,
            requested_start_ms=requested_start_ms,
            requested_end_ms=requested_end_ms,
            actual_start_ms=inspection.actual_start_ms,
            actual_end_ms=inspection.actual_end_ms,
            source_part_ids=[source.part_id for source in clip.sources],
            confirmation_required=inspection.confirmation_required,
            confirmation_confirmed=clip.confirmed,
            result='queued',
        )
        return clip

    async def _restart_stale_inspection(
        self, row: sqlite3.Row, *, active_durations_ms: Mapping[int, int]
    ) -> HighlightInspectionOperation:
        async with self._inspection_admission_lock:
            if not self._inspection_admission_open:
                raise HighlightInspectionBusy('高光检查服务正在关闭')
            return await self._restart_stale_inspection_admitted(
                row, active_durations_ms=active_durations_ms
            )

    async def _restart_stale_inspection_admitted(
        self, row: sqlite3.Row, *, active_durations_ms: Mapping[int, int]
    ) -> HighlightInspectionOperation:
        now = int(self._clock())
        active_json = json.dumps(
            {str(key): int(value) for key, value in active_durations_ms.items()},
            sort_keys=True,
            separators=(',', ':'),
        )
        await self._database.execute(
            "UPDATE highlight_inspections SET state='accepted',"
            'active_durations_json=?,result_json=NULL,error_code=NULL,'
            'fingerprint_json=NULL,claim_key_hash=NULL,token_hash=NULL,'
            'token_expires_at=NULL,token_consumed_at=NULL,'
            'terminal_expires_at=NULL,updated_at=? WHERE operation_id=?',
            (active_json, now, str(row['operation_id'])),
        )
        await self._enqueue_inspection(str(row['operation_id']))
        refreshed = await self._database.fetchone(
            'SELECT * FROM highlight_inspections WHERE operation_id=?',
            (str(row['operation_id']),),
        )
        assert refreshed is not None
        return self._operation_from_row(refreshed)

    async def _prepare_clip(
        self,
        *,
        session_id: int,
        requested_start_ms: int,
        requested_end_ms: int,
        active_durations_ms: Mapping[int, int],
    ) -> Tuple[
        HighlightTimeline, Tuple[Tuple[TimelinePart, int, int], ...], ClipInspection
    ]:
        clipper = self._clipper
        if clipper is None:
            raise RuntimeError('highlight clipping is not configured')
        timeline, source_ranges, clip_sources = await self._prepare_clip_sources(
            session_id=session_id,
            requested_start_ms=requested_start_ms,
            requested_end_ms=requested_end_ms,
            active_durations_ms=active_durations_ms,
        )
        inspection = await asyncio.get_running_loop().run_in_executor(
            None,
            partial(
                clipper.inspect,
                clip_sources,
                requested_start_ms=requested_start_ms,
                requested_end_ms=requested_end_ms,
                stable_end_ms=timeline.stable_end_ms,
            ),
        )
        return timeline, source_ranges, inspection

    async def _prepare_clip_sources(
        self,
        *,
        session_id: int,
        requested_start_ms: int,
        requested_end_ms: int,
        active_durations_ms: Mapping[int, int],
    ) -> Tuple[
        HighlightTimeline,
        Tuple[Tuple[TimelinePart, int, int], ...],
        Tuple[ClipSource, ...],
    ]:
        if self._clipper is None:
            raise RuntimeError('highlight clipping is not configured')
        if requested_start_ms < 0 or requested_end_ms <= requested_start_ms:
            raise HighlightRangeUnavailable('高光剪辑时间范围无效')
        timeline = await self.timeline(session_id, active_durations_ms)
        if not timeline.parts:
            raise HighlightRangeUnavailable('本场没有可用的本地录像')
        if requested_end_ms > timeline.stable_end_ms:
            raise HighlightRangeUnavailable('所选范围进入录制中的最后 10 秒')
        source_ranges = self._resolve_clip_sources(
            timeline.parts, requested_start_ms, requested_end_ms
        )
        if len(source_ranges) != 1:
            raise HighlightRangeUnavailable('每个高光片段只能选择一个分 P')
        clip_sources = tuple(
            ClipSource(
                part_id=part.part_id,
                path=part.path,
                requested_start_ms=local_start_ms,
                requested_end_ms=local_end_ms,
                duration_ms=part.duration_ms,
                recording=part.recording,
            )
            for part, local_start_ms, local_end_ms in source_ranges
        )
        return timeline, source_ranges, clip_sources

    async def get_clip(self, clip_id: int) -> HighlightClip:
        row = await self._database.fetchone(
            self._CLIP_WITH_UPLOAD_SELECT + 'WHERE clip.id=?', (clip_id,)
        )
        if row is None:
            raise ValueError("unknown highlight clip '{}'".format(clip_id))
        sources = await self._database.fetchall(
            'SELECT part_id,ordinal,requested_start_ms,requested_end_ms,'
            'actual_start_ms,actual_end_ms FROM highlight_clip_sources '
            'WHERE clip_id=? ORDER BY ordinal',
            (clip_id,),
        )
        clip = self._clip_from_row(
            row, tuple(self._clip_source_from_row(source) for source in sources)
        )
        progress = await self._upload_progress(
            () if clip.upload_job_id is None else (clip.upload_job_id,)
        )
        return self._apply_upload_progress(clip, progress)

    async def list_clips(self, session_id: int) -> Tuple[HighlightClip, ...]:
        session = await self._database.fetchone(
            "SELECT id FROM recording_sessions WHERE id=? AND source_kind='live'",
            (session_id,),
        )
        if session is None:
            raise ValueError("unknown recording session '{}'".format(session_id))
        rows = await self._database.fetchall(
            self._CLIP_WITH_UPLOAD_SELECT
            + "WHERE clip.source_session_id=? AND clip.state!='cancelled' "
            'ORDER BY clip.created_at,clip.id',
            (session_id,),
        )
        source_rows = await self._database.fetchall(
            'SELECT source.clip_id,source.part_id,source.ordinal,'
            'source.requested_start_ms,source.requested_end_ms,'
            'source.actual_start_ms,source.actual_end_ms '
            'FROM highlight_clip_sources source '
            'JOIN highlight_clips clip ON clip.id=source.clip_id '
            "WHERE clip.source_session_id=? AND clip.state!='cancelled' "
            'ORDER BY source.clip_id,source.ordinal',
            (session_id,),
        )
        sources_by_clip: Dict[int, List[HighlightClipSource]] = {}
        for source_row in source_rows:
            sources_by_clip.setdefault(int(source_row['clip_id']), []).append(
                self._clip_source_from_row(source_row)
            )
        clips = tuple(
            self._clip_from_row(row, tuple(sources_by_clip.get(int(row['id']), ())))
            for row in rows
        )
        progress = await self._upload_progress(
            tuple(
                clip.upload_job_id for clip in clips if clip.upload_job_id is not None
            )
        )
        return tuple(self._apply_upload_progress(clip, progress) for clip in clips)

    async def list_clip_summaries(
        self, *, limit: int, offset: int
    ) -> Tuple[int, Tuple[HighlightClipSummary, ...]]:
        if limit < 1 or limit > 100:
            raise ValueError('clip list limit must be between 1 and 100')
        if offset < 0:
            raise ValueError('clip list offset must not be negative')
        total = int(
            await self._database.scalar(
                "SELECT COUNT(*) FROM highlight_clips WHERE state!='cancelled' "
                "AND deletion_state IN ('none','failed')"
            )
        )
        rows = await self._database.fetchall(self._CLIP_SUMMARY_SELECT, (limit, offset))
        return total, tuple(self._clip_summary_from_row(row) for row in rows)

    async def _upload_progress(self, job_ids: Tuple[int, ...]) -> Dict[int, float]:
        if not job_ids:
            return {}
        placeholders = ','.join('?' for _value in job_ids)
        rows = await self._database.fetchall(
            'SELECT part.job_id,COALESCE(SUM(chunk.size),0) AS total_bytes,'
            "COALESCE(SUM(CASE WHEN chunk.state='confirmed' "
            'THEN chunk.size ELSE 0 END),0) AS confirmed_bytes '
            'FROM upload_parts part LEFT JOIN upload_chunks chunk '
            'ON chunk.part_id=part.id WHERE part.job_id IN ({}) '
            'GROUP BY part.job_id'.format(placeholders),
            job_ids,
        )
        result: Dict[int, float] = {}
        for row in rows:
            total = int(row['total_bytes'])
            confirmed = int(row['confirmed_bytes'])
            result[int(row['job_id'])] = (
                0.0 if total <= 0 else round(min(100.0, confirmed * 100.0 / total), 2)
            )
        return result

    @staticmethod
    def _apply_upload_progress(
        clip: HighlightClip, progress: Mapping[int, float]
    ) -> HighlightClip:
        if clip.upload_job_id is None:
            return clip
        percent = (
            100.0
            if clip.upload_state in ('approved', 'completed')
            else progress.get(clip.upload_job_id, 0.0)
        )
        return replace(clip, upload_percent=percent)

    async def clip_video_path(self, clip_id: int) -> Path:
        clip = await self.get_clip(clip_id)
        if clip.state != 'ready' or clip.output_video_path is None:
            raise ValueError('highlight clip is not ready')
        path = self._owned_highlight_path(clip.output_video_path)
        if not path.is_file() or path.stat().st_size <= 0:
            raise ValueError('highlight clip video is missing')
        return path

    async def clip_media_resource(self, clip_id: int) -> HighlightClipMediaResource:
        row = await self._database.fetchone(
            'SELECT id,name,output_video_path,file_size_bytes,state '
            'FROM highlight_clips WHERE id=?',
            (int(clip_id),),
        )
        if row is None:
            raise ValueError("unknown highlight clip '{}'".format(clip_id))
        if str(row['state']) != 'ready' or row['output_video_path'] is None:
            raise ValueError('highlight clip is not ready')
        if self._clip_root is None:
            raise ValueError('highlight clip root is not configured')
        output_path = str(row['output_video_path'])
        if Path(output_path).suffix.lower() != '.mp4':
            raise ValueError('invalid highlight output path')
        return HighlightClipMediaResource(
            clip_id=int(row['id']),
            name=str(row['name']),
            output_video_path=output_path,
            file_size_bytes=(
                None if row['file_size_bytes'] is None else int(row['file_size_bytes'])
            ),
            expected_root=str(self._clip_root),
        )

    async def retry_clip(self, clip_id: int) -> HighlightClip:
        clip = await self.get_clip(clip_id)
        if clip.state != 'failed':
            raise ValueError('only a failed highlight clip can be retried')
        if clip.deletion_state != 'none':
            raise ValueError('片段正在删除，不能重试生成')
        if not clip.sources:
            raise ValueError('源录像关联已丢失，无法重试，请删除后重新创建片段')
        updated = await self._database.execute(
            "UPDATE highlight_clips SET state='queued',error_message=NULL,"
            'lease_owner=NULL,lease_until=NULL,next_attempt_at=0,'
            'file_size_bytes=NULL,updated_at=? '
            "WHERE id=? AND state='failed' AND deletion_state='none' "
            'AND EXISTS(SELECT 1 FROM highlight_clip_sources source '
            'WHERE source.clip_id=highlight_clips.id)',
            (int(self._clock()), clip_id),
        )
        if updated != 1:
            raise ValueError('highlight clip state changed')
        audit(
            'highlight_clip_manually_retried',
            clip_id=clip_id,
            room_id=clip.room_id,
            previous_attempt=clip.attempt,
            result='queued',
        )
        return await self.get_clip(clip_id)

    async def delete_clip(self, clip_id: int) -> str:
        clip = await self.get_clip(clip_id)
        if clip.state in ('queued', 'processing'):
            updated = await self._database.execute(
                "UPDATE highlight_clips SET state='cancelled',lease_owner=NULL,"
                'lease_until=NULL,next_attempt_at=0,file_size_bytes=NULL,'
                'updated_at=? WHERE id=? '
                "AND state IN ('queued','processing')",
                (int(self._clock()), clip_id),
            )
            if updated != 1:
                raise ValueError('highlight clip state changed')
            await self._remove_clip_outputs(clip, partial_only=True)
            audit(
                'highlight_clip_cancelled',
                clip_id=clip_id,
                room_id=clip.room_id,
                result='cancelled',
            )
            return 'cancelled'
        self._clip_output_paths(clip, partial_only=False)

        now = int(self._clock())

        def begin_delete(
            connection: sqlite3.Connection,
        ) -> Tuple[Optional[int], Optional[int]]:
            current = connection.execute(
                'SELECT upload_session_id FROM highlight_clips WHERE id=?', (clip_id,)
            ).fetchone()
            if current is None:
                raise ValueError('highlight clip state changed')
            upload_session_id = current['upload_session_id']
            upload_job_id: Optional[int] = None
            if upload_session_id is not None:
                job = connection.execute(
                    'SELECT id FROM upload_jobs WHERE session_id=?',
                    (upload_session_id,),
                ).fetchone()
                if job is not None:
                    upload_job_id = int(job['id'])
                    connection.execute(
                        'DELETE FROM danmaku_items WHERE part_id IN('
                        'SELECT id FROM upload_parts WHERE job_id=?)',
                        (upload_job_id,),
                    )
                    connection.execute(
                        'DELETE FROM upload_chunks WHERE part_id IN('
                        'SELECT id FROM upload_parts WHERE job_id=?)',
                        (upload_job_id,),
                    )
                    connection.execute(
                        'DELETE FROM comment_items WHERE job_id=?', (upload_job_id,)
                    )
                    connection.execute(
                        'DELETE FROM upload_parts WHERE job_id=?', (upload_job_id,)
                    )
                    connection.execute(
                        'DELETE FROM upload_jobs WHERE id=?', (upload_job_id,)
                    )
            updated = connection.execute(
                "UPDATE highlight_clips SET state='cancelled',lease_owner=NULL,"
                'lease_until=NULL,next_attempt_at=0,file_size_bytes=NULL,'
                'updated_at=? WHERE id=?',
                (now, clip_id),
            )
            if updated.rowcount != 1:
                raise ValueError('highlight clip state changed')
            return (
                None if upload_session_id is None else int(upload_session_id),
                upload_job_id,
            )

        upload_session_id, upload_job_id = await self._database.write(begin_delete)
        if upload_job_id is not None:
            audit(
                'highlight_clip_upload_task_cancelled',
                clip_id=clip_id,
                room_id=clip.room_id,
                upload_job_id=upload_job_id,
                result='deleted_local_only',
            )
        try:
            await self._remove_clip_outputs(clip, partial_only=False)
        except OSError as error:
            audit(
                'highlight_clip_delete_pending',
                level='ERROR',
                clip_id=clip_id,
                room_id=clip.room_id,
                reason=str(error)[:500],
                result='cancelled',
            )
            raise

        def finish_delete(connection: sqlite3.Connection) -> int:
            current = connection.execute(
                'SELECT state,upload_session_id FROM highlight_clips WHERE id=?',
                (clip_id,),
            ).fetchone()
            if current is None or str(current['state']) != 'cancelled':
                return 0
            current_session_id = current['upload_session_id']
            expected_session_id = upload_session_id
            if current_session_id != expected_session_id:
                return 0
            if current_session_id is not None:
                has_job = connection.execute(
                    'SELECT 1 FROM upload_jobs WHERE session_id=?',
                    (current_session_id,),
                ).fetchone()
                if has_job is not None:
                    raise ValueError('highlight clip already has an upload task')
            cursor = connection.execute(
                'DELETE FROM highlight_clips WHERE id=?', (clip_id,)
            )
            if cursor.rowcount == 1 and current_session_id is not None:
                connection.execute(
                    'DELETE FROM event_journal WHERE run_id IN ('
                    'SELECT id FROM recording_runs WHERE session_id=?)',
                    (current_session_id,),
                )
                connection.execute(
                    'DELETE FROM recording_parts WHERE session_id=?',
                    (current_session_id,),
                )
                connection.execute(
                    'DELETE FROM recording_runs WHERE session_id=?',
                    (current_session_id,),
                )
                connection.execute(
                    "DELETE FROM recording_sessions WHERE id=? "
                    "AND source_kind='highlight' "
                    'AND NOT EXISTS(SELECT 1 FROM upload_jobs WHERE session_id=?)',
                    (current_session_id, current_session_id),
                )
            return cursor.rowcount

        deleted = await self._database.write(finish_delete)
        if deleted != 1:
            raise ValueError('highlight clip state changed')
        audit(
            'highlight_clip_deleted',
            clip_id=clip_id,
            room_id=clip.room_id,
            result='deleted',
        )
        return 'deleted'

    async def ensure_upload_session(self, clip_id: int) -> int:
        clip = await self.get_clip(clip_id)
        if (
            clip.state != 'ready'
            or clip.deletion_state != 'none'
            or clip.output_video_path is None
        ):
            raise ValueError('highlight clip is not ready for upload')
        if clip.upload_session_id is not None:
            return clip.upload_session_id
        video_path = self._owned_highlight_path(clip.output_video_path)
        if not video_path.is_file() or video_path.stat().st_size <= 0:
            raise ValueError('highlight clip video is missing')
        xml_path: Optional[Path] = None
        if clip.output_xml_path is not None:
            candidate = self._owned_highlight_path(clip.output_xml_path)
            if candidate.is_file():
                xml_path = candidate
        now = int(self._clock())
        video_size = video_path.stat().st_size
        duration_seconds = max(
            1,
            int(
                round(
                    ((clip.actual_end_ms or 0) - (clip.actual_start_ms or 0)) / 1000.0
                )
            ),
        )

        def create(connection: sqlite3.Connection) -> int:
            current = connection.execute(
                'SELECT source_session_id,upload_session_id,state,'
                'deletion_state,output_video_path,output_xml_path '
                'FROM highlight_clips WHERE id=?',
                (clip_id,),
            ).fetchone()
            if current is None:
                raise ValueError('highlight clip does not exist')
            if str(current['deletion_state']) != 'none':
                raise ValueError('highlight clip is not ready for upload')
            if current['upload_session_id'] is not None:
                return int(current['upload_session_id'])
            if (
                str(current['state']) != 'ready'
                or current['source_session_id'] is None
                or str(current['output_video_path']) != str(video_path)
            ):
                raise ValueError('highlight clip state changed')
            source = connection.execute(
                'SELECT room_id,live_start_time,started_at,title,cover_url,'
                'cover_path,anchor_uid,anchor_name,area_id,area_name,'
                'parent_area_id,parent_area_name,live_end_time '
                'FROM recording_sessions WHERE id=? AND source_kind=\'live\'',
                (int(current['source_session_id']),),
            ).fetchone()
            if source is None:
                raise ValueError('highlight source session does not exist')
            key = 'highlight:{}'.format(clip_id)
            existing = connection.execute(
                'SELECT id FROM recording_sessions WHERE broadcast_session_key=?',
                (key,),
            ).fetchone()
            if existing is None:
                cursor = connection.execute(
                    'INSERT INTO recording_sessions('
                    'room_id,broadcast_session_key,live_start_time,state,started_at,'
                    'ended_at,title,cover_url,cover_path,anchor_uid,anchor_name,'
                    'area_id,area_name,parent_area_id,parent_area_name,live_end_time,'
                    'upload_intent,source_kind) '
                    "VALUES(?,?,?,'closed',?,?,?,?,?,?,?,?,?,?,?,?,"
                    "'upload','highlight')",
                    (
                        int(source['room_id']),
                        key,
                        source['live_start_time'],
                        now,
                        now,
                        clip.name,
                        str(source['cover_url']),
                        source['cover_path'],
                        source['anchor_uid'],
                        str(source['anchor_name']),
                        source['area_id'],
                        str(source['area_name']),
                        source['parent_area_id'],
                        str(source['parent_area_name']),
                        source['live_end_time'],
                    ),
                )
                session_id = int(cursor.lastrowid)
                run_id = 'highlight:{}'.format(clip_id)
                connection.execute(
                    'INSERT INTO recording_runs('
                    'id,session_id,state,started_at,ended_at) '
                    "VALUES(?,?,'finished',?,?)",
                    (run_id, session_id, now, now),
                )
                record_start_time = int(source['started_at']) + int(
                    (clip.actual_start_ms or 0) / 1000
                )
                connection.execute(
                    'INSERT INTO recording_parts('
                    'session_id,run_id,part_index,source_path,final_path,xml_path,'
                    'record_start_time,record_end_time,record_duration_seconds,'
                    'file_size_bytes,danmaku_count,artifact_state,xml_completed,'
                    'created_at,updated_at) '
                    "VALUES(?,?,1,?,?,?,?,?,?,?,?, 'ready',?,?,?)",
                    (
                        session_id,
                        run_id,
                        str(video_path),
                        str(video_path),
                        None if xml_path is None else str(xml_path),
                        record_start_time,
                        record_start_time + duration_seconds,
                        duration_seconds,
                        video_size,
                        0,
                        int(xml_path is not None),
                        now,
                        now,
                    ),
                )
            else:
                session_id = int(existing['id'])
                valid = connection.execute(
                    'SELECT 1 FROM recording_sessions WHERE id=? '
                    "AND source_kind='highlight' AND deletion_state='none'",
                    (session_id,),
                ).fetchone()
                if valid is None:
                    raise ValueError('highlight upload session key conflicts')
            updated = connection.execute(
                'UPDATE highlight_clips SET upload_session_id=?,updated_at=? '
                "WHERE id=? AND upload_session_id IS NULL AND deletion_state='none'",
                (session_id, now, clip_id),
            )
            if updated.rowcount != 1:
                raise ValueError('highlight clip is not ready for upload')
            return session_id

        session_id = await self._database.write(create)
        audit(
            'highlight_upload_session_created',
            clip_id=clip_id,
            session_id=session_id,
            room_id=clip.room_id,
            result='ready_for_draft',
        )
        return session_id

    async def marker_counts(self, session_id: int) -> Sequence[HighlightMarkerCount]:
        rows = await self._database.fetchall(
            'SELECT session.room_id,part.id AS part_id,part.part_index,'
            'part.record_start_time,part.timeline_start_at_ms,'
            'part.record_duration_seconds,part.artifact_state,'
            'part.video_deleted_at '
            'FROM recording_sessions session '
            'LEFT JOIN recording_parts part ON part.session_id=session.id '
            "AND part.artifact_state IN ('recording','postprocessing','ready') "
            'AND part.video_deleted_at IS NULL '
            "WHERE session.id=? AND session.source_kind='live' "
            'ORDER BY part.part_index',
            (session_id,),
        )
        if not rows:
            raise ValueError("unknown live recording session '{}'".format(session_id))
        part_rows = [row for row in rows if row['part_id'] is not None]
        if not part_rows:
            return ()

        room_id = int(rows[0]['room_id'])
        origin_ms = min(
            (
                int(row['timeline_start_at_ms'])
                if row['timeline_start_at_ms'] is not None
                else int(row['record_start_time']) * 1000
            )
            for row in part_rows
        )
        parts: List[TimelinePart] = []
        for row in part_rows:
            absolute_start_at_ms = (
                int(row['timeline_start_at_ms'])
                if row['timeline_start_at_ms'] is not None
                else int(row['record_start_time']) * 1000
            )
            duration_ms = (
                0
                if row['record_duration_seconds'] is None
                else max(0, int(row['record_duration_seconds']) * 1000)
            )
            recording = str(row['artifact_state']) == 'recording'
            timeline_start_ms = absolute_start_at_ms - origin_ms
            stable_duration_ms = (
                max(0, duration_ms - self.ACTIVE_SAFE_TAIL_MS)
                if recording
                else duration_ms
            )
            parts.append(
                TimelinePart(
                    part_id=int(row['part_id']),
                    part_index=int(row['part_index']),
                    path='',
                    absolute_start_at_ms=absolute_start_at_ms,
                    timeline_start_ms=timeline_start_ms,
                    duration_ms=duration_ms,
                    stable_end_ms=timeline_start_ms + stable_duration_ms,
                    recording=recording,
                )
            )

        counts = {part.part_id: 0 for part in parts}
        marker_rows = await self._database.fetchall(
            'SELECT recording_part_id,content_at_ms FROM highlight_markers '
            'WHERE room_id=? ORDER BY content_at_ms,id',
            (room_id,),
        )
        for row in marker_rows:
            recording_part_id = row['recording_part_id']
            if recording_part_id is not None:
                part_id = int(recording_part_id)
                if part_id in counts:
                    counts[part_id] += 1
                continue
            part = self._part_containing(parts, int(row['content_at_ms']))
            if part is not None:
                counts[part.part_id] += 1
        return tuple(
            HighlightMarkerCount(part_id=part.part_id, count=counts[part.part_id])
            for part in parts
        )

    async def timeline(
        self, session_id: int, active_durations_ms: Mapping[int, int]
    ) -> HighlightTimeline:
        session = await self._database.fetchone(
            'SELECT id,room_id,source_kind FROM recording_sessions WHERE id=?',
            (session_id,),
        )
        if session is None or str(session['source_kind']) != 'live':
            raise ValueError("unknown live recording session '{}'".format(session_id))
        room_id = int(session['room_id'])
        rows = await self._database.fetchall(
            'SELECT id,part_index,source_path,final_path,record_start_time,'
            'timeline_start_at_ms,record_duration_seconds,artifact_state '
            'FROM recording_parts WHERE session_id=? '
            "AND artifact_state IN ('recording','postprocessing','ready') "
            'AND video_deleted_at IS NULL ORDER BY part_index',
            (session_id,),
        )
        candidates = []
        for row in rows:
            path = self._available_path(row)
            if path is None:
                continue
            part_id = int(row['id'])
            recording = str(row['artifact_state']) == 'recording'
            if part_id in active_durations_ms:
                duration_ms = max(0, int(active_durations_ms[part_id]))
                recording = True
            elif row['record_duration_seconds'] is not None:
                duration_ms = max(0, int(row['record_duration_seconds']) * 1000)
            else:
                duration_ms = 0
            absolute_start_at_ms = (
                int(row['timeline_start_at_ms'])
                if row['timeline_start_at_ms'] is not None
                else int(row['record_start_time']) * 1000
            )
            candidates.append(
                (
                    part_id,
                    int(row['part_index']),
                    path,
                    absolute_start_at_ms,
                    duration_ms,
                    recording,
                )
            )
        if not candidates:
            return HighlightTimeline(session_id, room_id, 0, 0, (), ())

        origin_ms = min(item[3] for item in candidates)
        parts: List[TimelinePart] = []
        for (
            part_id,
            part_index,
            path,
            absolute_start_at_ms,
            duration_ms,
            recording,
        ) in candidates:
            timeline_start_ms = absolute_start_at_ms - origin_ms
            stable_duration_ms = (
                max(0, duration_ms - self.ACTIVE_SAFE_TAIL_MS)
                if recording
                else duration_ms
            )
            parts.append(
                TimelinePart(
                    part_id=part_id,
                    part_index=part_index,
                    path=path,
                    absolute_start_at_ms=absolute_start_at_ms,
                    timeline_start_ms=timeline_start_ms,
                    duration_ms=duration_ms,
                    stable_end_ms=timeline_start_ms + stable_duration_ms,
                    recording=recording,
                )
            )

        marker_rows = await self._database.fetchall(
            'SELECT * FROM highlight_markers WHERE room_id=? '
            'ORDER BY content_at_ms,id',
            (room_id,),
        )
        mapped: List[MappedHighlight] = []
        for marker_row in marker_rows:
            marker = self._marker_from_row(marker_row)
            part = self._part_containing(parts, marker.content_at_ms)
            if part is None:
                continue
            local_offset_ms = marker.content_at_ms - part.absolute_start_at_ms
            mapped.append(
                MappedHighlight(
                    marker=marker,
                    part_id=part.part_id,
                    local_offset_ms=local_offset_ms,
                    timeline_offset_ms=part.timeline_start_ms + local_offset_ms,
                )
            )
        mapped.sort(key=lambda item: (item.timeline_offset_ms, item.marker.id))
        return HighlightTimeline(
            session_id=session_id,
            room_id=room_id,
            duration_ms=max(
                part.timeline_start_ms + part.duration_ms for part in parts
            ),
            stable_end_ms=max(part.stable_end_ms for part in parts),
            parts=tuple(parts),
            markers=tuple(mapped),
        )

    @staticmethod
    def _available_path(row: sqlite3.Row) -> Optional[str]:
        paths = (row['final_path'], row['source_path'])
        for value in paths:
            if value is not None and os.path.isfile(str(value)):
                return str(value)
        return None

    @staticmethod
    def _part_containing(
        parts: List[TimelinePart], content_at_ms: int
    ) -> Optional[TimelinePart]:
        for part in parts:
            local_offset_ms = content_at_ms - part.absolute_start_at_ms
            if 0 <= local_offset_ms <= part.duration_ms:
                return part
        return None

    async def _remove_clip_outputs(
        self, clip: HighlightClip, *, partial_only: bool
    ) -> None:
        paths = self._clip_output_paths(clip, partial_only=partial_only)

        def remove() -> None:
            for path in paths:
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass

        await asyncio.get_running_loop().run_in_executor(None, remove)

    def _clip_output_paths(
        self, clip: HighlightClip, *, partial_only: bool
    ) -> Tuple[Path, ...]:
        paths = []
        for value in (clip.output_video_path, clip.output_xml_path):
            if value is None:
                continue
            try:
                path = self._owned_highlight_path(value)
            except ValueError as error:
                if self._is_missing_legacy_output(value):
                    audit(
                        'highlight_clip_missing_legacy_output_skipped',
                        clip_id=clip.id,
                        room_id=clip.room_id,
                        path=value,
                        result='missing',
                    )
                    continue
                audit(
                    'highlight_clip_delete_rejected',
                    level='WARNING',
                    clip_id=clip.id,
                    room_id=clip.room_id,
                    stage='validate_output_path',
                    reason=str(error)[:500],
                    result='rejected',
                )
                raise
            paths.append(Path(str(path) + '.partial'))
            if not partial_only:
                paths.append(path)
        return tuple(paths)

    def _is_missing_legacy_output(self, value: str) -> bool:
        if self._clip_root is None:
            return False
        path = Path(value).resolve(strict=False)
        if path.suffix.lower() not in ('.mp4', '.xml'):
            return False
        try:
            path.relative_to(self._clip_root.resolve())
        except ValueError:
            return not path.exists() and not Path(str(path) + '.partial').exists()
        return False

    def _owned_highlight_path(self, value: str) -> Path:
        if self._clip_root is None:
            raise ValueError('highlight clip root is not configured')
        root = self._clip_root.resolve()
        path = Path(value).resolve(strict=False)
        try:
            path.relative_to(root)
        except ValueError:
            raise ValueError('highlight output path is outside recording root')
        if path.suffix.lower() not in ('.mp4', '.xml'):
            raise ValueError('invalid highlight output path')
        return path

    @staticmethod
    def _resolve_clip_sources(
        parts: Sequence[TimelinePart], start_ms: int, end_ms: int
    ) -> Tuple[Tuple[TimelinePart, int, int], ...]:
        sources = []
        for part in parts:
            part_start_ms = part.timeline_start_ms
            part_end_ms = part_start_ms + part.duration_ms
            intersection_start_ms = max(start_ms, part_start_ms)
            intersection_end_ms = min(end_ms, part_end_ms)
            if intersection_end_ms <= intersection_start_ms:
                continue
            if intersection_end_ms > part.stable_end_ms:
                raise HighlightRangeUnavailable('所选范围进入录制中的最后 10 秒')
            sources.append(
                (
                    part,
                    intersection_start_ms - part_start_ms,
                    intersection_end_ms - part_start_ms,
                )
            )
        if not sources:
            raise HighlightRangeUnavailable('所选范围没有对应的本地录像')
        if not any(
            part.timeline_start_ms
            <= start_ms
            < part.timeline_start_ms + part.duration_ms
            for part in parts
        ):
            raise HighlightRangeUnavailable('剪辑开始位置位于录像断档中')
        if not any(
            part.timeline_start_ms < end_ms <= part.timeline_start_ms + part.duration_ms
            for part in parts
        ):
            raise HighlightRangeUnavailable('剪辑结束位置位于录像断档中')
        return tuple(sources)

    @staticmethod
    def _default_name(title: str, content_at_ms: int) -> str:
        formatted = time.strftime('%H:%M:%S', time.localtime(content_at_ms / 1000.0))
        prefix = title.strip() or '直播'
        suffix = ' 高光 {}'.format(formatted)
        return '{}{}'.format(prefix[: 200 - len(suffix)], suffix)

    @staticmethod
    def _optional_nonnegative(value: Optional[int]) -> Optional[int]:
        if value is None:
            return None
        return min(604_800_000, max(0, int(value)))

    @staticmethod
    def _marker_from_row(row: sqlite3.Row) -> HighlightMarker:
        return HighlightMarker(
            id=int(row['id']),
            room_id=int(row['room_id']),
            observed_at_ms=int(row['observed_at_ms']),
            player_delay_ms=int(row['player_delay_ms']),
            content_at_ms=int(row['content_at_ms']),
            title=str(row['title']),
            anchor_name=str(row['anchor_name']),
            name=str(row['name']),
            note=str(row['note']),
            source=str(row['source']),
            created_at=int(row['created_at']),
            updated_at=int(row['updated_at']),
            recording_part_id=(
                None
                if 'recording_part_id' not in row.keys()
                or row['recording_part_id'] is None
                else int(row['recording_part_id'])
            ),
            part_anchor_at_ms=(
                None
                if 'part_anchor_at_ms' not in row.keys()
                or row['part_anchor_at_ms'] is None
                else int(row['part_anchor_at_ms'])
            ),
            current_time_ms=(
                None
                if 'current_time_ms' not in row.keys() or row['current_time_ms'] is None
                else int(row['current_time_ms'])
            ),
            seekable_end_ms=(
                None
                if 'seekable_end_ms' not in row.keys() or row['seekable_end_ms'] is None
                else int(row['seekable_end_ms'])
            ),
            raw_delay_ms=(
                int(row['raw_delay_ms'])
                if 'raw_delay_ms' in row.keys()
                else int(row['player_delay_ms'])
            ),
            baseline_delay_ms=(
                int(row['baseline_delay_ms'])
                if 'baseline_delay_ms' in row.keys()
                else 0
            ),
            effective_rewind_ms=(
                int(row['effective_rewind_ms'])
                if 'effective_rewind_ms' in row.keys()
                else int(row['player_delay_ms'])
            ),
        )

    @staticmethod
    def _clip_source_from_row(row: sqlite3.Row) -> HighlightClipSource:
        return HighlightClipSource(
            part_id=int(row['part_id']),
            ordinal=int(row['ordinal']),
            requested_start_ms=int(row['requested_start_ms']),
            requested_end_ms=int(row['requested_end_ms']),
            actual_start_ms=(
                None if row['actual_start_ms'] is None else int(row['actual_start_ms'])
            ),
            actual_end_ms=(
                None if row['actual_end_ms'] is None else int(row['actual_end_ms'])
            ),
        )

    @staticmethod
    def _clip_summary_from_row(row: sqlite3.Row) -> HighlightClipSummary:
        return HighlightClipSummary(
            id=int(row['id']),
            room_id=int(row['room_id']),
            source_session_id=(
                None
                if row['source_session_id'] is None
                else int(row['source_session_id'])
            ),
            name=str(row['name']),
            state=str(row['state']),
            error_message=(
                None if row['error_message'] is None else str(row['error_message'])
            ),
            created_at=int(row['created_at']),
            updated_at=int(row['updated_at']),
            source_anchor_name=str(row['source_anchor_name']),
            source_title=str(row['source_title']),
            duration_ms=max(0, int(row['duration_ms'])),
            file_size_bytes=(
                None if row['file_size_bytes'] is None else int(row['file_size_bytes'])
            ),
            upload_job_id=(
                None if row['upload_job_id'] is None else int(row['upload_job_id'])
            ),
            upload_state=(
                None if row['upload_state'] is None else str(row['upload_state'])
            ),
            upload_percent=(
                None if row['upload_percent'] is None else float(row['upload_percent'])
            ),
            upload_bvid=(
                None if row['upload_bvid'] is None else str(row['upload_bvid'])
            ),
            deletion_state=str(row['deletion_state']),
            deletion_error=(
                None if row['deletion_error'] is None else str(row['deletion_error'])
            ),
        )

    @staticmethod
    def _clip_from_row(
        row: sqlite3.Row, sources: Tuple[HighlightClipSource, ...] = ()
    ) -> HighlightClip:
        return HighlightClip(
            id=int(row['id']),
            marker_id=None if row['marker_id'] is None else int(row['marker_id']),
            room_id=int(row['room_id']),
            source_session_id=(
                None
                if row['source_session_id'] is None
                else int(row['source_session_id'])
            ),
            upload_session_id=(
                None
                if row['upload_session_id'] is None
                else int(row['upload_session_id'])
            ),
            name=str(row['name']),
            requested_start_ms=int(row['requested_start_ms']),
            requested_end_ms=int(row['requested_end_ms']),
            actual_start_ms=(
                None if row['actual_start_ms'] is None else int(row['actual_start_ms'])
            ),
            actual_end_ms=(
                None if row['actual_end_ms'] is None else int(row['actual_end_ms'])
            ),
            output_video_path=(
                None
                if row['output_video_path'] is None
                else str(row['output_video_path'])
            ),
            output_xml_path=(
                None if row['output_xml_path'] is None else str(row['output_xml_path'])
            ),
            state=str(row['state']),
            confirmation_required=bool(row['keyframe_confirmation_required']),
            confirmed=bool(row['keyframe_confirmed']),
            error_message=(
                None if row['error_message'] is None else str(row['error_message'])
            ),
            attempt=int(row['attempt']),
            created_at=int(row['created_at']),
            updated_at=int(row['updated_at']),
            sources=sources,
            upload_job_id=(
                None
                if 'upload_job_id' not in row.keys() or row['upload_job_id'] is None
                else int(row['upload_job_id'])
            ),
            upload_state=(
                None
                if 'upload_state' not in row.keys() or row['upload_state'] is None
                else str(row['upload_state'])
            ),
            upload_percent=(
                None
                if 'upload_percent' not in row.keys() or row['upload_percent'] is None
                else float(row['upload_percent'])
            ),
            upload_bvid=(
                None
                if 'upload_bvid' not in row.keys() or row['upload_bvid'] is None
                else str(row['upload_bvid'])
            ),
            deletion_state=str(row['deletion_state']),
            deletion_error=(
                None if row['deletion_error'] is None else str(row['deletion_error'])
            ),
            source_anchor_name=(
                ''
                if 'source_anchor_name' not in row.keys()
                or row['source_anchor_name'] is None
                else str(row['source_anchor_name'])
            ),
            source_title=(
                ''
                if 'source_title' not in row.keys() or row['source_title'] is None
                else str(row['source_title'])
            ),
            duration_ms=max(
                0,
                int(
                    (row['actual_end_ms'] or row['requested_end_ms'])
                    - (row['actual_start_ms'] or row['requested_start_ms'])
                ),
            ),
            file_size_bytes=(
                None
                if 'file_size_bytes' not in row.keys() or row['file_size_bytes'] is None
                else int(row['file_size_bytes'])
            ),
        )
