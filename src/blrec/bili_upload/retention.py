from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import (
    Any,
    Callable,
    Dict,
    Iterable,
    List,
    Optional,
    Sequence,
    Tuple,
    TypeVar,
)

from blrec.logging.audit import audit

from .database import BiliUploadDatabase

__all__ = ('RetentionManager', 'RetentionStatus')

_DAY_SECONDS = 24 * 60 * 60
_VIDEO_SUFFIXES = frozenset(
    ('.flv', '.mp4', '.ts', '.m4s', '.m3u8', '.mkv', '.mov', '.webm')
)
_T = TypeVar('_T')


@dataclass(frozen=True)
class RetentionStatus:
    managed_video_bytes: int
    capacity_bytes: int
    remaining_bytes: int
    warning_threshold_bytes: int
    warning: bool


@dataclass(frozen=True)
class _Candidate:
    part_id: int
    source_path: str
    final_path: Optional[str]
    reason: str
    order_at: int


class RetentionManager:
    def __init__(
        self,
        database: BiliUploadDatabase,
        recording_root: Path,
        *,
        capacity_bytes: Callable[[], int] = lambda: 0,
        warning_threshold_bytes: Callable[[], int] = lambda: 0,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._database = database
        self._root = Path(
            os.path.abspath(os.path.expanduser(str(recording_root)))
        ).resolve()
        self._capacity_bytes = capacity_bytes
        self._warning_threshold_bytes = warning_threshold_bytes
        self._clock = clock
        self._run_lock = asyncio.Lock()

    async def run_once(self) -> int:
        async with self._run_lock:
            deleted = 0
            now = int(self._clock())
            for candidate in await self._event_candidates(now):
                if await self._delete_candidate(candidate, now):
                    deleted += 1
            capacity = max(0, int(self._capacity_bytes()))
            if capacity <= 0:
                return deleted
            usage = await self._managed_video_bytes()
            if usage <= capacity:
                return deleted
            for candidate in await self._capacity_candidates():
                before = await self._candidate_size(candidate)
                if await self._delete_candidate(candidate, now):
                    deleted += 1
                    usage = max(0, usage - before)
                    if usage <= capacity:
                        break
            return deleted

    async def reclaim_for_low_space(self, required_free_bytes: int) -> bool:
        if required_free_bytes < 0:
            raise ValueError('required free space must not be negative')
        async with self._run_lock:
            if self._free_bytes() >= required_free_bytes:
                return True
            now = int(self._clock())
            for candidate in await self._capacity_candidates():
                await self._delete_candidate(candidate, now, reason='low_space')
                if self._free_bytes() >= required_free_bytes:
                    return True
            return False

    async def status(self) -> RetentionStatus:
        usage = await self._managed_video_bytes()
        capacity = max(0, int(self._capacity_bytes()))
        warning_threshold = max(0, int(self._warning_threshold_bytes()))
        remaining = max(0, capacity - usage) if capacity > 0 else 0
        return RetentionStatus(
            managed_video_bytes=usage,
            capacity_bytes=capacity,
            remaining_bytes=remaining,
            warning_threshold_bytes=warning_threshold,
            warning=(
                capacity > 0
                and warning_threshold > 0
                and remaining <= warning_threshold
            ),
        )

    async def _event_candidates(self, now: int) -> List[_Candidate]:
        rows = await self._database.fetchall(
            'SELECT part.id,part.source_path,part.final_path,session.started_at,'
            "COALESCE(policy.retention_mode,'submitted') AS retention_mode,"
            'COALESCE(policy.retention_days,5) AS retention_days,'
            'job.upload_completed_at,job.submitted_at,job.approved_at '
            'FROM recording_parts part '
            'JOIN recording_sessions session ON session.id=part.session_id '
            'JOIN upload_jobs job ON job.session_id=session.id '
            'LEFT JOIN room_upload_policies policy ON policy.room_id=session.room_id '
            "WHERE part.video_deleted_at IS NULL AND session.state='closed' "
            'ORDER BY session.started_at,part.part_index,part.id'
        )
        candidates = []
        milestone_columns = {
            'upload_completed': 'upload_completed_at',
            'submitted': 'submitted_at',
            'approved': 'approved_at',
        }
        for row in rows:
            mode = str(row['retention_mode'])
            milestone_column = milestone_columns.get(mode)
            if milestone_column is None or row[milestone_column] is None:
                continue
            milestone = int(row[milestone_column])
            due_at = milestone + int(row['retention_days']) * _DAY_SECONDS
            if due_at > now:
                continue
            candidates.append(self._candidate(row, mode, due_at))
        return candidates

    async def _capacity_candidates(self) -> List[_Candidate]:
        now = int(self._clock())
        rows = await self._database.fetchall(
            'SELECT part.id,part.source_path,part.final_path,session.started_at,'
            'CASE WHEN job.id IS NULL THEN '
            'COALESCE(session.ended_at,session.started_at) '
            'ELSE job.submitted_at END AS order_at '
            'FROM recording_parts part '
            'JOIN recording_sessions session ON session.id=part.session_id '
            'LEFT JOIN upload_jobs job ON job.session_id=session.id '
            'JOIN room_upload_policies policy ON policy.room_id=session.room_id '
            "WHERE part.video_deleted_at IS NULL AND session.state='closed' "
            "AND session.deletion_state='none' "
            "AND part.artifact_state NOT IN ('recording','postprocessing') "
            "AND policy.retention_mode='capacity' AND ((job.id IS NULL "
            "AND session.upload_intent IN ('none','skip')) OR "
            '(job.id IS NOT NULL AND job.submitted_at IS NOT NULL)) '
            'AND (job.id IS NULL OR job.lease_until IS NULL OR job.lease_until<=?) '
            'ORDER BY order_at,session.started_at,part.part_index,part.id',
            (now,),
        )
        return [self._candidate(row, 'capacity', int(row['order_at'])) for row in rows]

    @staticmethod
    def _candidate(row: Any, reason: str, order_at: int) -> _Candidate:
        return _Candidate(
            part_id=int(row['id']),
            source_path=str(row['source_path']),
            final_path=(None if row['final_path'] is None else str(row['final_path'])),
            reason=reason,
            order_at=order_at,
        )

    async def _delete_candidate(
        self, candidate: _Candidate, now: int, *, reason: Optional[str] = None
    ) -> bool:
        paths = self._unique_paths(candidate.source_path, candidate.final_path)
        try:
            await self._run_io(self._delete_paths, paths)
        except OSError as error:
            await self._database.execute(
                'UPDATE recording_parts SET video_delete_error=? WHERE id=? '
                'AND video_deleted_at IS NULL',
                (str(error)[:1000], candidate.part_id),
            )
            audit(
                'recording_video_delete_failed',
                level='ERROR',
                part_id=candidate.part_id,
                reason=reason or candidate.reason,
                error_type=type(error).__name__,
                result='failed',
            )
            return False
        updated = await self._database.execute(
            'UPDATE recording_parts SET video_deleted_at=?,video_delete_reason=?,'
            'video_delete_error=NULL WHERE id=? AND video_deleted_at IS NULL',
            (now, reason or candidate.reason, candidate.part_id),
        )
        if updated == 1:
            audit(
                'recording_video_deleted',
                part_id=candidate.part_id,
                reason=reason or candidate.reason,
                path_count=len(paths),
                result='deleted',
            )
            return True
        return False

    async def _candidate_size(self, candidate: _Candidate) -> int:
        return await self._run_io(
            self._paths_size,
            self._unique_paths(candidate.source_path, candidate.final_path),
        )

    async def _managed_video_bytes(self) -> int:
        rows = await self._database.fetchall(
            'SELECT source_path,final_path FROM recording_parts '
            'WHERE video_deleted_at IS NULL'
        )
        paths: Dict[str, Path] = {}
        for row in rows:
            for path in self._unique_paths(
                str(row['source_path']),
                None if row['final_path'] is None else str(row['final_path']),
            ):
                paths[str(path)] = path
        return await self._run_io(self._paths_size, tuple(paths.values()))

    def _unique_paths(
        self, source_path: str, final_path: Optional[str]
    ) -> Tuple[Path, ...]:
        result: Dict[str, Path] = {}
        for raw_path in (source_path, final_path):
            if raw_path:
                path = Path(os.path.abspath(os.path.expanduser(raw_path)))
                result[str(path)] = path
        return tuple(result.values())

    def _delete_paths(self, paths: Sequence[Path]) -> None:
        for path in paths:
            self._validate_video_path(path)
            try:
                path.lstat()
            except FileNotFoundError:
                continue
            if not path.is_file() and not path.is_symlink():
                raise OSError("recording video path is not a file: '{}'".format(path))
        for path in paths:
            if not path.exists() and not path.is_symlink():
                continue
            path.unlink()

    def _paths_size(self, paths: Iterable[Path]) -> int:
        size = 0
        for path in paths:
            try:
                self._validate_video_path(path)
                size += path.lstat().st_size
            except (FileNotFoundError, OSError):
                continue
        return size

    def _validate_video_path(self, path: Path) -> None:
        resolved = path.resolve(strict=False)
        try:
            resolved.relative_to(self._root)
        except ValueError:
            raise OSError("recording video path is outside root: '{}'".format(path))
        if path.suffix.lower() not in _VIDEO_SUFFIXES:
            raise OSError("recording path is not a supported video: '{}'".format(path))

    def _free_bytes(self) -> int:
        usage = os.statvfs(str(self._root))
        return usage.f_bavail * usage.f_frsize

    @staticmethod
    async def _run_io(operation: Callable[..., _T], *args: object) -> _T:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, operation, *args)
