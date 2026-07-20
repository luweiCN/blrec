from __future__ import annotations

import asyncio
import os
import sqlite3
import time
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Dict, List, Mapping, Optional, Tuple

from blrec.logging.audit import audit

from .artifact_recovery import RecoveredArtifact, probe_recording_artifact
from .database import BiliUploadDatabase, LeaseLost
from .highlight_cut import (
    ClipInspection,
    ClipSource,
    CutArtifact,
    HighlightCutError,
    LosslessClipper,
)
from .highlight_danmaku import (
    DanmakuClipSource,
    DanmakuCutError,
    DanmakuCutResult,
    HighlightDanmakuClipper,
)
from .highlights import (
    HighlightService,
    _fingerprint_json,
    _fingerprint_matches,
    _inspection_from_json,
    _inspection_json,
)


@dataclass(frozen=True)
class _WorkSource:
    part_id: int
    session_id: int
    cancellation_generation: int
    ordinal: int
    video_path: str
    xml_path: Optional[str]
    requested_start_ms: int
    requested_end_ms: int
    actual_start_ms: Optional[int]
    actual_end_ms: Optional[int]
    recording: bool


@dataclass(frozen=True)
class _ClipWork:
    clip_id: int
    requested_start_ms: int
    requested_end_ms: int
    confirmation_confirmed: bool
    output_video_path: str
    output_xml_path: str
    sources: Tuple[_WorkSource, ...]
    inspection_json: Optional[str]
    source_fingerprint_json: Optional[str]


@dataclass(frozen=True)
class _ProcessResult:
    inspection: ClipInspection
    artifact: CutArtifact
    danmaku: DanmakuCutResult
    output_xml_path: Optional[str]
    elapsed_seconds: float
    source_fingerprint_json: Optional[str]


@dataclass(frozen=True)
class _ClaimedClip:
    id: int
    lease_owner: str
    lease_generation: int
    lease_until: int
    attempt: int
    cancellation_generation: int


class _LocalDeletionRequested(RuntimeError):
    pass


class HighlightWorker:
    def __init__(
        self,
        database: BiliUploadDatabase,
        clipper: LosslessClipper,
        danmaku_clipper: HighlightDanmakuClipper,
        *,
        worker_id: Optional[str] = None,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
        artifact_probe: Callable[
            [str], Optional[RecoveredArtifact]
        ] = probe_recording_artifact,
    ) -> None:
        self._database = database
        self._clipper = clipper
        self._danmaku_clipper = danmaku_clipper
        self._worker_id = worker_id or 'highlight-{}'.format(uuid.uuid4())
        self._clock = clock
        self._monotonic = monotonic
        self._artifact_probe = artifact_probe
        self._active_owners: Dict[int, asyncio.Task[int]] = {}

    async def run_once(self) -> Optional[int]:
        claim = await self._claim()
        if claim is None:
            return None
        work: Optional[_ClipWork] = None
        try:
            work = await self._load_work(claim)
            if await self._generation_changed(claim, work):
                await self._complete_cancelled(claim, 'ffmpeg_cut')
                return claim.id
        except _LocalDeletionRequested:
            await self._complete_cancelled(claim, 'ffmpeg_cut')
            return claim.id
        except asyncio.CancelledError:
            raise
        except Exception as error:
            await self._handle_failure(claim, work, error)
            return claim.id

        owner_task = asyncio.create_task(self._settle_claim(claim, work))
        self._active_owners[claim.id] = owner_task
        cancellation_requested = False
        try:
            while True:
                try:
                    result = await asyncio.shield(owner_task)
                    break
                except asyncio.CancelledError:
                    cancellation_requested = True
                    if owner_task.done():
                        result = owner_task.result()
                        break
        finally:
            if self._active_owners.get(claim.id) is owner_task:
                self._active_owners.pop(claim.id, None)
        if cancellation_requested:
            raise asyncio.CancelledError
        return result

    async def _settle_claim(self, claim: _ClaimedClip, work: _ClipWork) -> int:
        process = asyncio.ensure_future(
            asyncio.get_running_loop().run_in_executor(None, self._process_sync, work)
        )
        cancellation_requested = False
        try:
            while True:
                try:
                    result = await asyncio.shield(process)
                    break
                except asyncio.CancelledError:
                    cancellation_requested = True
                    if process.done():
                        result = process.result()
                        break
            completed = await self._complete(claim, work, result)
            if not completed:
                audit(
                    'highlight_clip_cancelled',
                    clip_id=claim.id,
                    reason='local_deletion_requested',
                    result='cancelled_local',
                )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            await self._handle_failure(claim, work, error)
        if cancellation_requested:
            raise asyncio.CancelledError
        return claim.id

    async def _claim(self) -> Optional[_ClaimedClip]:
        now = int(self._clock())

        def claim(connection: sqlite3.Connection) -> Optional[_ClaimedClip]:
            row = connection.execute(
                'SELECT clip.id,clip.cancellation_generation,clip.attempt '
                'FROM highlight_clips clip '
                "WHERE clip.state='queued' AND clip.deletion_state='none' "
                'AND clip.next_attempt_at<=? '
                'AND (clip.lease_until IS NULL OR clip.lease_until<=?) '
                'AND NOT EXISTS(SELECT 1 FROM recording_sessions direct_session '
                'WHERE direct_session.id=clip.source_session_id '
                "AND direct_session.deletion_state!='none') "
                'AND NOT EXISTS(SELECT 1 FROM highlight_clip_sources source '
                'JOIN recording_parts part ON part.id=source.part_id '
                'JOIN recording_sessions session ON session.id=part.session_id '
                'WHERE source.clip_id=clip.id '
                "AND session.deletion_state!='none') "
                'ORDER BY clip.priority DESC,clip.next_attempt_at,clip.id LIMIT 1',
                (now, now),
            ).fetchone()
            if row is None:
                return None
            clip_id = int(row['id'])
            lease_until = now + BiliUploadDatabase.LEASE_TTL_SECONDS
            updated = connection.execute(
                "UPDATE highlight_clips SET state='processing',file_size_bytes=NULL,"
                'lease_owner=?,lease_generation=lease_generation+1,lease_until=?,'
                'attempt=attempt+1,updated_at=? '
                "WHERE id=? AND state='queued' AND deletion_state='none' "
                'AND cancellation_generation=? AND next_attempt_at<=? '
                'AND (lease_until IS NULL OR lease_until<=?) '
                'AND NOT EXISTS(SELECT 1 FROM recording_sessions direct_session '
                'WHERE direct_session.id=highlight_clips.source_session_id '
                "AND direct_session.deletion_state!='none') "
                'AND NOT EXISTS(SELECT 1 FROM highlight_clip_sources source '
                'JOIN recording_parts part ON part.id=source.part_id '
                'JOIN recording_sessions session ON session.id=part.session_id '
                'WHERE source.clip_id=highlight_clips.id '
                "AND session.deletion_state!='none')",
                (
                    self._worker_id,
                    lease_until,
                    now,
                    clip_id,
                    int(row['cancellation_generation']),
                    now,
                    now,
                ),
            )
            if updated.rowcount != 1:
                return None
            current = connection.execute(
                'SELECT lease_generation,attempt FROM highlight_clips WHERE id=?',
                (clip_id,),
            ).fetchone()
            assert current is not None
            connection.execute(
                'INSERT INTO owner_handoff_outcomes('
                'owner_kind,owner_id,side_effect_key,source_generation,'
                'outcome_state,outcome_json,acknowledged_at) '
                "VALUES('highlight',?,'ffmpeg_cut',?,'in_flight','{}',NULL) "
                'ON CONFLICT(owner_kind,owner_id,side_effect_key,source_generation) '
                "DO UPDATE SET outcome_state='in_flight',outcome_json='{}',"
                'acknowledged_at=NULL',
                (clip_id, int(row['cancellation_generation'])),
            )
            return _ClaimedClip(
                id=clip_id,
                lease_owner=self._worker_id,
                lease_generation=int(current['lease_generation']),
                lease_until=lease_until,
                attempt=int(current['attempt']),
                cancellation_generation=int(row['cancellation_generation']),
            )

        return await self._database.write(claim)

    async def recover_interrupted(self) -> int:
        rows = await self._database.fetchall(
            'SELECT id,state,output_video_path,output_xml_path,'
            'cancellation_generation,deletion_state,lease_owner,'
            'lease_generation,lease_until,attempt,'
            '(SELECT source_generation FROM owner_handoff_outcomes outcome '
            "WHERE outcome.owner_kind='highlight' "
            'AND outcome.owner_id=highlight_clips.id '
            "AND outcome.side_effect_key='ffmpeg_cut' "
            'ORDER BY outcome.id DESC LIMIT 1) AS owner_source_generation '
            "FROM highlight_clips WHERE state IN ('queued','processing') "
            'ORDER BY id'
        )
        recovered = 0
        loop = asyncio.get_running_loop()
        for row in rows:
            clip_id = int(row['id'])
            owner_source_generation = (
                0
                if row['owner_source_generation'] is None
                else int(row['owner_source_generation'])
            )
            active_owner = self._active_owners.get(clip_id)
            if active_owner is not None:
                try:
                    await asyncio.shield(active_owner)
                except asyncio.CancelledError:
                    if not active_owner.done():
                        raise
                except Exception:
                    pass
                continue
            state = str(row['state'])
            video_path = (
                None
                if row['output_video_path'] is None
                else str(row['output_video_path'])
            )
            xml_path = (
                None if row['output_xml_path'] is None else str(row['output_xml_path'])
            )
            deleting_source = bool(
                await self._database.scalar(
                    'SELECT COUNT(*) FROM highlight_clip_sources source '
                    'JOIN recording_parts part ON part.id=source.part_id '
                    'JOIN recording_sessions session ON session.id=part.session_id '
                    'WHERE source.clip_id=? '
                    "AND session.deletion_state!='none'",
                    (clip_id,),
                )
            )
            if state == 'processing' and (
                str(row['deletion_state']) != 'none' or deleting_source
            ):
                self._remove_work_files(video_path, xml_path, include_final=False)
                owner = row['lease_owner']
                if owner is not None:
                    claim = _ClaimedClip(
                        id=clip_id,
                        lease_owner=str(owner),
                        lease_generation=int(row['lease_generation']),
                        lease_until=int(row['lease_until'] or 0),
                        attempt=int(row['attempt']),
                        cancellation_generation=owner_source_generation,
                    )
                    await self._complete_cancelled(claim, 'ffmpeg_cut')
                recovered += 1
                audit(
                    'highlight_clip_recovered',
                    clip_id=clip_id,
                    result='cancelled_local',
                )
                continue
            if state == 'processing' and video_path is not None:
                artifact = await loop.run_in_executor(
                    None, self._artifact_probe, video_path
                )
                if artifact is not None and artifact.size_bytes > 0:
                    recovered_xml_path = (
                        xml_path
                        if xml_path is not None and os.path.isfile(xml_path)
                        else None
                    )
                    changed = await self._recover_processing_state(
                        clip_id,
                        owner_source_generation,
                        (
                            "UPDATE highlight_clips SET state='ready',"
                            'error_message=NULL,output_xml_path=?,file_size_bytes=?,'
                            'lease_owner=NULL,lease_until=NULL,next_attempt_at=0,'
                            'updated_at=? WHERE id=? '
                            "AND state='processing' AND deletion_state='none' "
                            'AND cancellation_generation=? AND NOT EXISTS('
                            'SELECT 1 FROM highlight_clip_sources source '
                            'JOIN recording_parts part ON part.id=source.part_id '
                            'JOIN recording_sessions session '
                            'ON session.id=part.session_id '
                            'WHERE source.clip_id=highlight_clips.id '
                            "AND session.deletion_state!='none')"
                        ),
                        (
                            recovered_xml_path,
                            artifact.size_bytes,
                            int(self._clock()),
                            clip_id,
                            int(row['cancellation_generation']),
                        ),
                    )
                    if changed != 1:
                        continue
                    recovered += 1
                    audit(
                        'highlight_clip_recovered',
                        clip_id=clip_id,
                        output_size=artifact.size_bytes,
                        result='ready',
                    )
                    continue
            partial_existed = self._partial_exists(video_path, xml_path)
            self._remove_work_files(
                video_path, xml_path, include_final=state == 'processing'
            )
            if state == 'processing':
                changed = await self._recover_processing_state(
                    clip_id,
                    owner_source_generation,
                    (
                        "UPDATE highlight_clips SET state='queued',"
                        "error_message='程序重启后自动重新处理',lease_owner=NULL,"
                        'lease_until=NULL,next_attempt_at=0,file_size_bytes=NULL,'
                        'updated_at=? WHERE id=? '
                        "AND state='processing' AND deletion_state='none' "
                        'AND cancellation_generation=? AND NOT EXISTS('
                        'SELECT 1 FROM highlight_clip_sources source '
                        'JOIN recording_parts part ON part.id=source.part_id '
                        'JOIN recording_sessions session '
                        'ON session.id=part.session_id '
                        'WHERE source.clip_id=highlight_clips.id '
                        "AND session.deletion_state!='none')"
                    ),
                    (int(self._clock()), clip_id, int(row['cancellation_generation'])),
                )
                recovered += int(changed == 1)
            elif partial_existed:
                recovered += 1
        return recovered

    async def _recover_processing_state(
        self,
        clip_id: int,
        source_generation: int,
        sql: str,
        parameters: Tuple[object, ...],
    ) -> int:
        def update(connection: sqlite3.Connection) -> int:
            cursor = connection.execute(sql, parameters)
            if cursor.rowcount == 1:
                self._clear_owner_intent(connection, clip_id, source_generation)
            return cursor.rowcount

        return await self._database.write(update)

    async def backfill_file_sizes(self, limit: int = 100) -> int:
        bounded_limit = min(100, max(0, int(limit)))
        if bounded_limit == 0:
            return 0
        rows = await self._database.fetchall(
            'SELECT id,output_video_path FROM highlight_clips '
            "WHERE state='ready' AND file_size_bytes IS NULL "
            'AND output_video_path IS NOT NULL ORDER BY id LIMIT ?',
            (bounded_limit,),
        )
        candidates = tuple(
            (int(row['id']), str(row['output_video_path'])) for row in rows
        )

        def measure() -> Tuple[Tuple[int, Optional[int], Optional[str]], ...]:
            results: List[Tuple[int, Optional[int], Optional[str]]] = []
            for clip_id, path in candidates:
                try:
                    size = max(0, int(os.path.getsize(path)))
                except (OSError, ValueError) as error:
                    results.append((clip_id, None, type(error).__name__))
                else:
                    results.append((clip_id, size, None))
            return tuple(results)

        measured = await asyncio.get_running_loop().run_in_executor(None, measure)
        successes: List[Tuple[int, int]] = []
        for clip_id, size, error_type in measured:
            if size is None:
                audit(
                    'highlight_clip_size_backfill_skipped',
                    level='WARNING',
                    clip_id=clip_id,
                    error_type=error_type,
                    result='unknown',
                )
            else:
                successes.append((clip_id, size))
        if not successes:
            return 0

        def update_sizes(connection: sqlite3.Connection) -> int:
            updated = 0
            for clip_id, size in successes:
                cursor = connection.execute(
                    'UPDATE highlight_clips SET file_size_bytes=? '
                    'WHERE id=? AND file_size_bytes IS NULL',
                    (size, clip_id),
                )
                updated += cursor.rowcount
            return updated

        return await self._database.write(update_sizes)

    async def progress(self) -> Tuple[Mapping[str, object], ...]:
        cutoff = int(self._clock()) - 300
        rows = await self._database.fetchall(
            'SELECT id,room_id,name,state,attempt,error_message,updated_at '
            "FROM highlight_clips WHERE state IN ('queued','processing') "
            'OR updated_at>=? ORDER BY updated_at DESC,id DESC',
            (cutoff,),
        )
        return tuple(
            {
                'id': int(row['id']),
                'room_id': int(row['room_id']),
                'name': str(row['name']),
                'state': str(row['state']),
                'attempt': int(row['attempt']),
                'error_message': (
                    None if row['error_message'] is None else str(row['error_message'])
                ),
                'updated_at': int(row['updated_at']),
            }
            for row in rows
        )

    async def _load_work(self, claim: _ClaimedClip) -> _ClipWork:
        row = await self._database.fetchone(
            'SELECT requested_start_ms,requested_end_ms,keyframe_confirmed,'
            'output_video_path,output_xml_path,cancellation_generation,'
            'deletion_state,inspection_json,source_fingerprint_json '
            'FROM highlight_clips '
            'WHERE id=? AND lease_owner=? AND lease_generation=? '
            "AND state='processing'",
            (claim.id, claim.lease_owner, claim.lease_generation),
        )
        if row is None:
            raise LeaseLost('高光剪辑任务租约已失效')
        if (
            int(row['cancellation_generation']) != claim.cancellation_generation
            or str(row['deletion_state']) != 'none'
        ):
            raise _LocalDeletionRequested('高光片段正在删除')
        if row['output_video_path'] is None or row['output_xml_path'] is None:
            raise HighlightCutError('高光剪辑输出路径不存在')
        source_rows = await self._database.fetchall(
            'SELECT source.part_id,source.ordinal,source.requested_start_ms,'
            'source.requested_end_ms,source.actual_start_ms,source.actual_end_ms,'
            'part.source_path,part.final_path,part.xml_path,part.video_deleted_at,'
            'part.artifact_state,part.session_id,session.cancellation_generation,'
            'session.deletion_state '
            'FROM highlight_clip_sources source '
            'JOIN recording_parts part ON part.id=source.part_id '
            'JOIN recording_sessions session ON session.id=part.session_id '
            'WHERE source.clip_id=? ORDER BY source.ordinal',
            (claim.id,),
        )
        sources: List[_WorkSource] = []
        for source in source_rows:
            if str(source['deletion_state']) != 'none':
                raise _LocalDeletionRequested('高光源录像正在删除')
            if source['video_deleted_at'] is not None:
                raise HighlightCutError('高光剪辑源视频已被删除')
            video_path = self._available_path(source)
            if video_path is None:
                raise HighlightCutError('高光剪辑源视频不存在')
            sources.append(
                _WorkSource(
                    part_id=int(source['part_id']),
                    session_id=int(source['session_id']),
                    cancellation_generation=int(source['cancellation_generation']),
                    ordinal=int(source['ordinal']),
                    video_path=video_path,
                    xml_path=(
                        None if source['xml_path'] is None else str(source['xml_path'])
                    ),
                    requested_start_ms=int(source['requested_start_ms']),
                    requested_end_ms=int(source['requested_end_ms']),
                    actual_start_ms=(
                        None
                        if source['actual_start_ms'] is None
                        else int(source['actual_start_ms'])
                    ),
                    actual_end_ms=(
                        None
                        if source['actual_end_ms'] is None
                        else int(source['actual_end_ms'])
                    ),
                    recording=str(source['artifact_state']) == 'recording',
                )
            )
        if not sources:
            raise HighlightCutError('高光剪辑没有源视频分段')
        return _ClipWork(
            clip_id=claim.id,
            requested_start_ms=int(row['requested_start_ms']),
            requested_end_ms=int(row['requested_end_ms']),
            confirmation_confirmed=bool(row['keyframe_confirmed']),
            output_video_path=str(row['output_video_path']),
            output_xml_path=str(row['output_xml_path']),
            sources=tuple(sources),
            inspection_json=(
                None if row['inspection_json'] is None else str(row['inspection_json'])
            ),
            source_fingerprint_json=(
                None
                if row['source_fingerprint_json'] is None
                else str(row['source_fingerprint_json'])
            ),
        )

    def _process_sync(self, work: _ClipWork) -> _ProcessResult:
        started_at = self._monotonic()
        hinted_sources = tuple(
            ClipSource(
                source.part_id,
                source.video_path,
                source.requested_start_ms,
                source.requested_end_ms,
                duration_ms=max(source.requested_end_ms, source.actual_end_ms or 0),
                keyframes_ms=(
                    () if source.actual_start_ms is None else (source.actual_start_ms,)
                ),
                recording=source.recording,
            )
            for source in work.sources
        )
        if len(hinted_sources) > 1:
            inspection = self._clipper.inspect_legacy(
                hinted_sources,
                requested_start_ms=work.requested_start_ms,
                requested_end_ms=work.requested_end_ms,
                stable_end_ms=work.requested_end_ms,
                deadline_monotonic=(
                    self._monotonic() + HighlightService.INSPECTION_DEADLINE_SECONDS
                ),
            )
            fingerprint_json: Optional[str] = None
        else:
            fingerprint_json = _fingerprint_json(hinted_sources[0])
            if (
                work.inspection_json is not None
                and work.source_fingerprint_json is not None
                and _fingerprint_matches(work.source_fingerprint_json, fingerprint_json)
            ):
                persisted = _inspection_from_json(
                    work.inspection_json, work.source_fingerprint_json
                )
                persisted_source = replace(
                    persisted.sources[0],
                    path=hinted_sources[0].path,
                    recording=hinted_sources[0].recording,
                )
                inspection = replace(persisted, sources=(persisted_source,))
            else:
                reprobe_sources = tuple(
                    replace(source, keyframes_ms=()) for source in hinted_sources
                )
                inspection = self._clipper.inspect(
                    reprobe_sources,
                    requested_start_ms=work.requested_start_ms,
                    requested_end_ms=work.requested_end_ms,
                    stable_end_ms=work.requested_end_ms,
                    deadline_monotonic=(
                        self._monotonic() + HighlightService.INSPECTION_DEADLINE_SECONDS
                    ),
                )
        if inspection.confirmation_required and not work.confirmation_confirmed:
            raise HighlightCutError('关键帧偏差尚未确认')
        video_partial = self._partial_path(work.output_video_path)
        xml_partial = self._partial_path(work.output_xml_path)
        self._remove(video_partial)
        self._remove(xml_partial)
        Path(work.output_video_path).parent.mkdir(parents=True, exist_ok=True)
        artifact = self._clipper.cut(inspection, video_partial)
        danmaku_sources = tuple(
            DanmakuClipSource(
                source.xml_path or '',
                inspected.actual_start_ms,
                inspected.actual_end_ms,
                inspected.output_offset_ms,
            )
            for source, inspected in zip(work.sources, inspection.sources)
            if source.xml_path is not None
        )
        danmaku = self._danmaku_clipper.cut(danmaku_sources, xml_partial)
        output_xml_path: Optional[str] = None
        if danmaku.output_path is not None:
            if not os.path.isfile(xml_partial):
                raise DanmakuCutError('弹幕剪辑未生成输出文件')
            os.replace(xml_partial, work.output_xml_path)
            output_xml_path = work.output_xml_path
        else:
            self._remove(work.output_xml_path)
        if not os.path.isfile(video_partial) or os.path.getsize(video_partial) <= 0:
            raise HighlightCutError('高光剪辑未生成有效视频')
        os.replace(video_partial, work.output_video_path)
        return _ProcessResult(
            inspection=inspection,
            artifact=artifact,
            danmaku=danmaku,
            output_xml_path=output_xml_path,
            elapsed_seconds=max(0.0, self._monotonic() - started_at),
            source_fingerprint_json=fingerprint_json,
        )

    async def _complete(
        self, claim: _ClaimedClip, work: _ClipWork, result: _ProcessResult
    ) -> bool:
        now = int(self._clock())

        def complete(connection: sqlite3.Connection) -> Tuple[int, bool]:
            if self._generation_changed_in_transaction(connection, claim, work):
                return (
                    self._complete_cancelled_in_transaction(
                        connection, claim, 'ffmpeg_cut', now
                    ),
                    True,
                )
            cursor = connection.execute(
                "UPDATE highlight_clips SET state='ready',actual_start_ms=?,"
                'actual_end_ms=?,output_xml_path=?,file_size_bytes=?,'
                'inspection_json=?,source_fingerprint_json=?,'
                'error_message=NULL,'
                'lease_owner=NULL,lease_until=NULL,next_attempt_at=0,updated_at=? '
                'WHERE id=? AND lease_owner=? AND lease_generation=? '
                "AND state='processing' AND deletion_state='none' "
                'AND cancellation_generation=?',
                (
                    result.inspection.actual_start_ms,
                    result.inspection.actual_end_ms,
                    result.output_xml_path,
                    result.artifact.size_bytes,
                    _inspection_json(result.inspection),
                    result.source_fingerprint_json,
                    now,
                    claim.id,
                    claim.lease_owner,
                    claim.lease_generation,
                    claim.cancellation_generation,
                ),
            )
            if cursor.rowcount != 1:
                return 0, False
            for ordinal, source in enumerate(result.inspection.sources, start=1):
                connection.execute(
                    'UPDATE highlight_clip_sources SET actual_start_ms=?,'
                    'actual_end_ms=? WHERE clip_id=? AND ordinal=?',
                    (source.actual_start_ms, source.actual_end_ms, claim.id, ordinal),
                )
            self._clear_owner_intent(
                connection, claim.id, claim.cancellation_generation
            )
            return 1, False

        count, cancelled = await self._database.write(complete)
        if count != 1:
            raise LeaseLost('高光剪辑任务租约已失效')
        if cancelled:
            return False
        audit(
            'highlight_clip_completed',
            clip_id=claim.id,
            requested_start_ms=work.requested_start_ms,
            requested_end_ms=work.requested_end_ms,
            actual_start_ms=result.inspection.actual_start_ms,
            actual_end_ms=result.inspection.actual_end_ms,
            source_part_ids=[source.part_id for source in work.sources],
            output_size=result.artifact.size_bytes,
            elapsed_seconds=round(result.elapsed_seconds, 3),
            danmaku_count=result.danmaku.message_count,
            result='ready',
        )
        return True

    async def _generation_changed(
        self, claim: _ClaimedClip, work: Optional[_ClipWork]
    ) -> bool:
        return await self._database.read(
            lambda connection: self._generation_changed_in_transaction(
                connection, claim, work
            )
        )

    @staticmethod
    def _generation_changed_in_transaction(
        connection: sqlite3.Connection, claim: _ClaimedClip, work: Optional[_ClipWork]
    ) -> bool:
        clip = connection.execute(
            'SELECT cancellation_generation,deletion_state FROM highlight_clips '
            'WHERE id=? AND lease_owner=? AND lease_generation=?',
            (claim.id, claim.lease_owner, claim.lease_generation),
        ).fetchone()
        if clip is None:
            return True
        if (
            int(clip['cancellation_generation']) != claim.cancellation_generation
            or str(clip['deletion_state']) != 'none'
        ):
            return True
        if work is None:
            deleting_source = connection.execute(
                'SELECT 1 FROM highlight_clip_sources source '
                'JOIN recording_parts part ON part.id=source.part_id '
                'JOIN recording_sessions session ON session.id=part.session_id '
                'WHERE source.clip_id=? '
                "AND session.deletion_state!='none' LIMIT 1",
                (claim.id,),
            ).fetchone()
            return deleting_source is not None
        expected = {
            source.session_id: source.cancellation_generation for source in work.sources
        }
        for session_id, generation in expected.items():
            session = connection.execute(
                'SELECT cancellation_generation,deletion_state '
                'FROM recording_sessions WHERE id=?',
                (session_id,),
            ).fetchone()
            if (
                session is None
                or int(session['cancellation_generation']) != generation
                or str(session['deletion_state']) != 'none'
            ):
                return True
        return False

    async def _complete_cancelled(
        self, claim: _ClaimedClip, side_effect_key: str
    ) -> None:
        count = await self._database.write(
            lambda connection: self._complete_cancelled_in_transaction(
                connection, claim, side_effect_key, int(self._clock())
            )
        )
        if count != 1:
            raise LeaseLost('高光剪辑任务租约已失效')

    @staticmethod
    def _complete_cancelled_in_transaction(
        connection: sqlite3.Connection,
        claim: _ClaimedClip,
        side_effect_key: str,
        now: int,
    ) -> int:
        current = connection.execute(
            'SELECT deletion_state FROM highlight_clips '
            'WHERE id=? AND lease_owner=? AND lease_generation=?',
            (claim.id, claim.lease_owner, claim.lease_generation),
        ).fetchone()
        if current is None:
            return 0
        if str(current['deletion_state']) == 'none':
            connection.execute(
                "UPDATE highlight_clips SET deletion_state='requested',"
                'deletion_error=NULL,deletion_requested_at=?,'
                'cancellation_generation=cancellation_generation+1 '
                'WHERE id=? AND lease_owner=? AND lease_generation=? '
                "AND deletion_state='none'",
                (now, claim.id, claim.lease_owner, claim.lease_generation),
            )
        cursor = connection.execute(
            'UPDATE highlight_clips SET lease_owner=NULL,lease_until=NULL,'
            'next_attempt_at=0,updated_at=? '
            'WHERE id=? AND lease_owner=? AND lease_generation=?',
            (now, claim.id, claim.lease_owner, claim.lease_generation),
        )
        if cursor.rowcount != 1:
            return 0
        connection.execute(
            'INSERT INTO owner_handoff_outcomes('
            'owner_kind,owner_id,side_effect_key,source_generation,'
            'outcome_state,outcome_json,acknowledged_at) '
            "VALUES('highlight',?,?,?,'cancelled_local','{}',?) "
            'ON CONFLICT(owner_kind,owner_id,side_effect_key,source_generation) '
            "DO UPDATE SET outcome_state='cancelled_local',outcome_json='{}',"
            'acknowledged_at=excluded.acknowledged_at',
            (claim.id, side_effect_key, claim.cancellation_generation, now),
        )
        return 1

    async def _handle_failure(
        self, claim: _ClaimedClip, work: Optional[_ClipWork], error: Exception
    ) -> None:
        row = await self._database.fetchone(
            'SELECT output_video_path,output_xml_path FROM highlight_clips WHERE id=?',
            (claim.id,),
        )
        deletion_changed = await self._generation_changed(claim, work)
        if row is not None:
            self._remove_work_files(
                (
                    None
                    if row['output_video_path'] is None
                    else str(row['output_video_path'])
                ),
                None if row['output_xml_path'] is None else str(row['output_xml_path']),
                include_final=not deletion_changed,
            )
        if deletion_changed:
            await self._complete_cancelled(claim, 'ffmpeg_cut')
            audit(
                'highlight_clip_cancelled',
                clip_id=claim.id,
                reason='local_deletion_requested',
                result='cancelled_local',
            )
            return
        source_state = await self._database.fetchone(
            'SELECT MAX(part.updated_at) AS updated_at,'
            'MAX(CASE WHEN part.artifact_state IN '
            "('recording','postprocessing') THEN 1 ELSE 0 END) AS growing "
            'FROM highlight_clip_sources source '
            'JOIN recording_parts part ON part.id=source.part_id '
            'WHERE source.clip_id=?',
            (claim.id,),
        )
        now = int(self._clock())
        recently_finalized = bool(
            source_state is not None
            and source_state['updated_at'] is not None
            and now - int(source_state['updated_at']) <= 600
        )
        retry_incomplete_probe = (
            bool(source_state is not None and source_state['growing'])
            or recently_finalized
        )
        transient = self._transient(
            error, retry_incomplete_probe=retry_incomplete_probe
        )
        next_attempt_at = now + min(300, 2 ** min(claim.attempt, 8)) if transient else 0
        state = 'queued' if transient else 'failed'

        def finish(connection: sqlite3.Connection) -> Tuple[int, bool]:
            if self._generation_changed_in_transaction(connection, claim, work):
                return (
                    self._complete_cancelled_in_transaction(
                        connection, claim, 'ffmpeg_cut', now
                    ),
                    True,
                )
            cursor = connection.execute(
                'UPDATE highlight_clips SET state=?,error_message=?,lease_owner=NULL,'
                'lease_until=NULL,next_attempt_at=?,file_size_bytes=NULL,updated_at=? '
                'WHERE id=? AND lease_owner=? AND lease_generation=? '
                "AND deletion_state='none' AND cancellation_generation=?",
                (
                    state,
                    '{}: {}'.format(type(error).__name__, error)[:1000],
                    next_attempt_at,
                    now,
                    claim.id,
                    claim.lease_owner,
                    claim.lease_generation,
                    claim.cancellation_generation,
                ),
            )
            if cursor.rowcount == 1:
                self._clear_owner_intent(
                    connection, claim.id, claim.cancellation_generation
                )
            return cursor.rowcount, False

        count, cancelled = await self._database.write(finish)
        if count != 1:
            raise LeaseLost('高光剪辑任务租约已失效')
        if cancelled:
            audit(
                'highlight_clip_cancelled',
                clip_id=claim.id,
                reason='local_deletion_requested',
                result='cancelled_local',
            )
            return
        audit(
            'highlight_clip_failed' if not transient else 'highlight_clip_retry',
            level='ERROR' if not transient else 'WARNING',
            clip_id=claim.id,
            error_type=type(error).__name__,
            reason=str(error)[:500],
            next_attempt_at=next_attempt_at,
            result=state,
        )

    @staticmethod
    def _clear_owner_intent(
        connection: sqlite3.Connection, clip_id: int, source_generation: int
    ) -> None:
        connection.execute(
            'DELETE FROM owner_handoff_outcomes WHERE owner_kind=? '
            'AND owner_id=? AND side_effect_key=? AND source_generation=?',
            ('highlight', clip_id, 'ffmpeg_cut', source_generation),
        )

    @staticmethod
    def _transient(error: Exception, *, retry_incomplete_probe: bool) -> bool:
        if isinstance(error, OSError):
            return True
        if isinstance(error, (HighlightCutError, DanmakuCutError)):
            text = str(error)
            if any(token in text for token in ('超时', 'temporarily')):
                return True
            return retry_incomplete_probe and any(
                token in text
                for token in (
                    '可用时长',
                    'ffprobe 无法读取',
                    'ffprobe 返回了无效的视频信息',
                    'ffprobe 返回了无效的视频流信息',
                    'ffprobe 返回了无效的关键帧信息',
                )
            )
        return False

    @staticmethod
    def _available_path(row: sqlite3.Row) -> Optional[str]:
        for column in ('final_path', 'source_path'):
            value = row[column]
            if value is not None and os.path.isfile(str(value)):
                return str(value)
        return None

    @classmethod
    def _remove_work_files(
        cls, video_path: Optional[str], xml_path: Optional[str], *, include_final: bool
    ) -> None:
        for path in (video_path, xml_path):
            if path is None:
                continue
            cls._remove(cls._partial_path(path))
            if include_final:
                cls._remove(path)

    @classmethod
    def _partial_exists(
        cls, video_path: Optional[str], xml_path: Optional[str]
    ) -> bool:
        return any(
            path is not None and os.path.exists(cls._partial_path(path))
            for path in (video_path, xml_path)
        )

    @staticmethod
    def _partial_path(path: str) -> str:
        return '{}.partial'.format(path)

    @staticmethod
    def _remove(path: str) -> None:
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
