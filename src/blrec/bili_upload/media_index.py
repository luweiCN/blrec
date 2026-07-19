from __future__ import annotations

import asyncio
import math
import os
import sqlite3
import stat
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from blrec.flv.common import find_metadata_tag, parse_metadata, read_tags
from blrec.flv.helpers import get_extra_metadata
from blrec.flv.io import FlvReader
from blrec.flv.metadata_analysis import analyse_metadata
from blrec.flv.metadata_injection import inject_metadata
from blrec.logging.audit import audit

from .database import BiliUploadDatabase

__all__ = (
    'MediaIndexResult',
    'MediaIndexWorker',
    'inspect_flv_index',
    'rebuild_flv_index',
)


@dataclass(frozen=True)
class MediaIndexResult:
    duration_ms: int
    file_size_bytes: int
    keyframe_count: int


@dataclass(frozen=True)
class _ClaimedPart:
    id: int
    path: str


class MediaIndexWorker:
    _LEASE_SECONDS = 3_600

    def __init__(
        self,
        database: BiliUploadDatabase,
        *,
        inspect: Callable[[str], Optional[MediaIndexResult]] = lambda path: (
            inspect_flv_index(path)
        ),
        rebuild: Callable[
            [str, Callable[[float], None]], MediaIndexResult
        ] = lambda path, progress: rebuild_flv_index(path, progress),
        clock: Callable[[], float] = time.time,
        worker_id: Optional[str] = None,
    ) -> None:
        self._database = database
        self._inspect = inspect
        self._rebuild = rebuild
        self._clock = clock
        self._worker_id = worker_id or 'media-index-{}'.format(uuid.uuid4())

    async def recover_interrupted(self) -> int:
        now = int(self._clock())
        count = await self._database.execute(
            "UPDATE recording_parts SET media_index_state='pending',"
            'media_index_owner=NULL,media_index_lease_until=NULL,'
            'media_index_error=NULL,media_index_updated_at=? '
            "WHERE media_index_state='indexing'",
            (now,),
        )
        if count:
            audit('media_index_recovered', count=count, result='requeued')
        return count

    async def run_once(self) -> Optional[int]:
        claimed = await self._claim()
        if claimed is None:
            return None
        started = time.monotonic()
        try:
            suffix = Path(claimed.path).suffix.lower()
            if suffix != '.flv':
                await self._complete(claimed.id, state='not_required')
                return claimed.id
            loop = asyncio.get_running_loop()
            result = await loop.run_in_executor(None, self._inspect, claimed.path)
            rebuilt = False
            if result is None:
                rebuilt = True
                result = await loop.run_in_executor(
                    None, self._rebuild, claimed.path, lambda _value: None
                )
            assert result is not None
            await self._complete(claimed.id, state='ready', result=result)
            audit(
                'media_index_completed',
                part_id=claimed.id,
                rebuilt=rebuilt,
                duration_ms=result.duration_ms,
                file_size_bytes=result.file_size_bytes,
                keyframe_count=result.keyframe_count,
                elapsed_ms=int((time.monotonic() - started) * 1_000),
                result='completed',
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            message = '{}: {}'.format(type(error).__name__, error)[:500]
            await self._complete(
                claimed.id, state='failed', error=message, progress=0.0
            )
            audit(
                'media_index_failed',
                level='WARNING',
                part_id=claimed.id,
                error=message,
                elapsed_ms=int((time.monotonic() - started) * 1_000),
                result='failed',
            )
        return claimed.id

    async def _claim(self) -> Optional[_ClaimedPart]:
        now = int(self._clock())

        def claim(connection: sqlite3.Connection) -> Optional[_ClaimedPart]:
            row = connection.execute(
                'SELECT part.id,part.source_path,part.final_path '
                'FROM recording_parts part '
                'JOIN recording_sessions session ON session.id=part.session_id '
                "WHERE part.media_index_state='pending' "
                "AND part.artifact_state='ready' AND session.state='closed' "
                'AND part.video_deleted_at IS NULL '
                'AND NOT EXISTS('
                'SELECT 1 FROM upload_jobs job WHERE job.session_id=part.session_id '
                "AND job.state NOT IN ("
                "'waiting_artifacts','approved','completed','rejected')) "
                'ORDER BY part.id LIMIT 1'
            ).fetchone()
            if row is None:
                return None
            path = _existing_regular_file(row['final_path'], row['source_path'])
            part_id = int(row['id'])
            if path is None:
                connection.execute(
                    "UPDATE recording_parts SET media_index_state='failed',"
                    "media_index_error='本地视频不可用',media_index_progress=0,"
                    'media_index_updated_at=? WHERE id=?',
                    (now, part_id),
                )
                return _ClaimedPart(part_id, '')
            cursor = connection.execute(
                "UPDATE recording_parts SET media_index_state='indexing',"
                'media_index_error=NULL,media_index_progress=0,'
                'media_index_owner=?,media_index_lease_until=?,'
                'media_index_attempt=media_index_attempt+1,'
                'media_index_updated_at=? '
                "WHERE id=? AND media_index_state='pending'",
                (self._worker_id, now + self._LEASE_SECONDS, now, part_id),
            )
            if cursor.rowcount != 1:
                return None
            return _ClaimedPart(part_id, path)

        claimed = await self._database.write(claim)
        if claimed is not None and not claimed.path:
            audit(
                'media_index_failed',
                level='WARNING',
                part_id=claimed.id,
                error='本地视频不可用',
                result='failed',
            )
            return None
        if claimed is not None:
            audit('media_index_started', part_id=claimed.id, result='started')
        return claimed

    async def _complete(
        self,
        part_id: int,
        *,
        state: str,
        error: Optional[str] = None,
        progress: float = 1.0,
        result: Optional[MediaIndexResult] = None,
    ) -> None:
        now = int(self._clock())
        count = await self._database.execute(
            'UPDATE recording_parts SET media_index_state=?,media_index_error=?,'
            'media_index_progress=?,media_index_updated_at=?,'
            'media_index_owner=NULL,media_index_lease_until=NULL,'
            'file_size_bytes=COALESCE(?,file_size_bytes),'
            'record_duration_seconds=COALESCE(record_duration_seconds,?) '
            'WHERE id=? AND media_index_owner=?',
            (
                state,
                error,
                progress,
                now,
                None if result is None else result.file_size_bytes,
                None if result is None else int(math.ceil(result.duration_ms / 1_000)),
                part_id,
                self._worker_id,
            ),
        )
        if count != 1:
            raise RuntimeError('media index claim is no longer owned')


def inspect_flv_index(path: str) -> Optional[MediaIndexResult]:
    try:
        size = os.path.getsize(path)
        if size <= 0:
            return None
        with open(path, 'rb') as file:
            reader = FlvReader(file)
            reader.read_header()
            tag = find_metadata_tag(read_tags(reader, 5))
            if tag is None:
                return None
            metadata = parse_metadata(tag)
    except (OSError, EOFError, ValueError, AssertionError, RuntimeError):
        return None
    duration = _positive_number(metadata.get('duration'))
    keyframes = metadata.get('keyframes')
    if duration is None or not isinstance(keyframes, Mapping):
        return None
    times = keyframes.get('times')
    positions = keyframes.get('filepositions')
    if not isinstance(times, list) or not isinstance(positions, list):
        return None
    count = min(len(times), len(positions))
    if count < 2:
        return None
    return MediaIndexResult(
        duration_ms=int(round(duration * 1_000)),
        file_size_bytes=size,
        keyframe_count=count,
    )


def rebuild_flv_index(path: str, progress: Callable[[float], None]) -> MediaIndexResult:
    progress(0.05)
    analyse_metadata(path).run()
    progress(0.5)
    metadata = get_extra_metadata(path)
    duration = _positive_number(metadata.get('duration'))
    keyframes = metadata.get('keyframes')
    if duration is None or not isinstance(keyframes, Mapping):
        raise RuntimeError('无法从录像中生成有效索引')
    inject_metadata(path, metadata).run()
    progress(0.95)
    result = inspect_flv_index(path)
    if result is None:
        raise RuntimeError('生成的录像索引无效')
    return result


def _positive_number(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) and number > 0 else None


def _existing_regular_file(*values: Any) -> Optional[str]:
    for value in values:
        if value is None:
            continue
        path = str(value)
        try:
            result = os.stat(path)
        except OSError:
            continue
        if stat.S_ISREG(result.st_mode):
            return path
    return None
