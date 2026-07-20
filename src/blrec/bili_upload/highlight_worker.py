from __future__ import annotations

import asyncio
import os
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Mapping, Optional, Tuple

from blrec.logging.audit import audit

from .artifact_recovery import RecoveredArtifact, probe_recording_artifact
from .database import BiliUploadDatabase, LeaseClaim, LeaseLost
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


@dataclass(frozen=True)
class _WorkSource:
    part_id: int
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


@dataclass(frozen=True)
class _ProcessResult:
    inspection: ClipInspection
    artifact: CutArtifact
    danmaku: DanmakuCutResult
    output_xml_path: Optional[str]
    elapsed_seconds: float


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

    async def run_once(self) -> Optional[int]:
        claim = await self._database.claim(
            'highlight_clips', ('queued',), self._worker_id, now=int(self._clock())
        )
        if claim is None:
            return None
        await self._database.fenced_update(
            'highlight_clips',
            claim.id,
            claim.lease_owner,
            claim.lease_generation,
            {'state': 'processing', 'updated_at': int(self._clock())},
        )
        try:
            work = await self._load_work(claim)
            result = await asyncio.get_running_loop().run_in_executor(
                None, self._process_sync, work
            )
            await self._complete(claim, work, result)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            await self._handle_failure(claim, error)
        return claim.id

    async def recover_interrupted(self) -> int:
        rows = await self._database.fetchall(
            'SELECT id,state,output_video_path,output_xml_path '
            "FROM highlight_clips WHERE state IN ('queued','processing') "
            'ORDER BY id'
        )
        recovered = 0
        loop = asyncio.get_running_loop()
        for row in rows:
            clip_id = int(row['id'])
            state = str(row['state'])
            video_path = (
                None
                if row['output_video_path'] is None
                else str(row['output_video_path'])
            )
            xml_path = (
                None if row['output_xml_path'] is None else str(row['output_xml_path'])
            )
            if state == 'processing' and video_path is not None:
                artifact = await loop.run_in_executor(
                    None, self._artifact_probe, video_path
                )
                if artifact is not None and artifact.size_bytes > 0:
                    await self._database.execute(
                        "UPDATE highlight_clips SET state='ready',error_message=NULL,"
                        'output_xml_path=?,lease_owner=NULL,lease_until=NULL,'
                        'next_attempt_at=0,updated_at=? WHERE id=? '
                        "AND state='processing'",
                        (
                            (
                                xml_path
                                if xml_path is not None and os.path.isfile(xml_path)
                                else None
                            ),
                            int(self._clock()),
                            clip_id,
                        ),
                    )
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
                await self._database.execute(
                    "UPDATE highlight_clips SET state='queued',"
                    "error_message='程序重启后自动重新处理',lease_owner=NULL,"
                    'lease_until=NULL,next_attempt_at=0,updated_at=? WHERE id=? '
                    "AND state='processing'",
                    (int(self._clock()), clip_id),
                )
                recovered += 1
            elif partial_existed:
                recovered += 1
        return recovered

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

    async def _load_work(self, claim: LeaseClaim) -> _ClipWork:
        row = await self._database.fetchone(
            'SELECT requested_start_ms,requested_end_ms,keyframe_confirmed,'
            'output_video_path,output_xml_path FROM highlight_clips '
            'WHERE id=? AND lease_owner=? AND lease_generation=? '
            "AND state='processing'",
            (claim.id, claim.lease_owner, claim.lease_generation),
        )
        if row is None:
            raise LeaseLost('高光剪辑任务租约已失效')
        if row['output_video_path'] is None or row['output_xml_path'] is None:
            raise HighlightCutError('高光剪辑输出路径不存在')
        source_rows = await self._database.fetchall(
            'SELECT source.part_id,source.ordinal,source.requested_start_ms,'
            'source.requested_end_ms,source.actual_start_ms,source.actual_end_ms,'
            'part.source_path,part.final_path,part.xml_path,part.video_deleted_at,'
            'part.artifact_state '
            'FROM highlight_clip_sources source '
            'JOIN recording_parts part ON part.id=source.part_id '
            'WHERE source.clip_id=? ORDER BY source.ordinal',
            (claim.id,),
        )
        sources: List[_WorkSource] = []
        for source in source_rows:
            if source['video_deleted_at'] is not None:
                raise HighlightCutError('高光剪辑源视频已被删除')
            video_path = self._available_path(source)
            if video_path is None:
                raise HighlightCutError('高光剪辑源视频不存在')
            sources.append(
                _WorkSource(
                    part_id=int(source['part_id']),
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
        )

    def _process_sync(self, work: _ClipWork) -> _ProcessResult:
        started_at = self._monotonic()
        clip_sources = tuple(
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
        inspection = self._clipper.inspect(
            clip_sources,
            requested_start_ms=work.requested_start_ms,
            requested_end_ms=work.requested_end_ms,
            stable_end_ms=work.requested_end_ms,
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
        )

    async def _complete(
        self, claim: LeaseClaim, work: _ClipWork, result: _ProcessResult
    ) -> None:
        now = int(self._clock())

        def complete(connection: sqlite3.Connection) -> bool:
            cursor = connection.execute(
                "UPDATE highlight_clips SET state='ready',actual_start_ms=?,"
                'actual_end_ms=?,output_xml_path=?,error_message=NULL,'
                'lease_owner=NULL,lease_until=NULL,next_attempt_at=0,updated_at=? '
                'WHERE id=? AND lease_owner=? AND lease_generation=? '
                "AND state='processing'",
                (
                    result.inspection.actual_start_ms,
                    result.inspection.actual_end_ms,
                    result.output_xml_path,
                    now,
                    claim.id,
                    claim.lease_owner,
                    claim.lease_generation,
                ),
            )
            if cursor.rowcount != 1:
                return False
            for ordinal, source in enumerate(result.inspection.sources, start=1):
                connection.execute(
                    'UPDATE highlight_clip_sources SET actual_start_ms=?,'
                    'actual_end_ms=? WHERE clip_id=? AND ordinal=?',
                    (source.actual_start_ms, source.actual_end_ms, claim.id, ordinal),
                )
            return True

        completed = await self._database.write(complete)
        if not completed:
            self._remove(work.output_video_path)
            self._remove(work.output_xml_path)
            raise LeaseLost('高光剪辑任务租约已失效')
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

    async def _handle_failure(self, claim: LeaseClaim, error: Exception) -> None:
        row = await self._database.fetchone(
            'SELECT output_video_path,output_xml_path FROM highlight_clips WHERE id=?',
            (claim.id,),
        )
        if row is not None:
            self._remove_work_files(
                (
                    None
                    if row['output_video_path'] is None
                    else str(row['output_video_path'])
                ),
                None if row['output_xml_path'] is None else str(row['output_xml_path']),
                include_final=True,
            )
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
        await self._database.execute(
            'UPDATE highlight_clips SET state=?,error_message=?,lease_owner=NULL,'
            'lease_until=NULL,next_attempt_at=?,updated_at=? WHERE id=? '
            'AND lease_owner=? AND lease_generation=?',
            (
                state,
                '{}: {}'.format(type(error).__name__, error)[:1000],
                next_attempt_at,
                now,
                claim.id,
                claim.lease_owner,
                claim.lease_generation,
            ),
        )
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
