from __future__ import annotations

import asyncio
import os
import sqlite3
import stat
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional, Protocol

from .database import BiliUploadDatabase

__all__ = (
    'RemoteMediaCache',
    'RemoteMediaDownloader',
    'RemoteMediaNotFound',
    'RemoteMediaStatus',
    'RemoteMediaUnavailable',
)

_TEN_DAYS_SECONDS = 10 * 24 * 60 * 60
_DOWNLOAD_CONCURRENCY = 3


class RemoteMediaNotFound(ValueError):
    pass


class RemoteMediaUnavailable(ValueError):
    pass


class RemoteMediaDownloader(Protocol):
    async def download(
        self,
        bundle: Any,
        *,
        bvid: str,
        cid: int,
        page: int,
        target: Path,
        progress: Callable[[int, Optional[int]], Awaitable[None]],
    ) -> None:
        raise NotImplementedError


@dataclass(frozen=True)
class RemoteMediaStatus:
    part_id: int
    state: str
    progress: float
    remote_available: bool
    account_id: Optional[int] = None
    bvid: Optional[str] = None
    cid: Optional[int] = None
    page: Optional[int] = None
    downloaded_bytes: int = 0
    total_bytes: Optional[int] = None
    cache_path: Optional[str] = None
    cached_at: Optional[int] = None
    expires_at: Optional[int] = None
    error: Optional[str] = None


class RemoteMediaCache:
    def __init__(
        self,
        database: BiliUploadDatabase,
        recording_root: Path,
        *,
        bundle_loader: Callable[[int], Awaitable[Any]],
        downloader: RemoteMediaDownloader,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._database = database
        self._recording_root = Path(
            os.path.abspath(os.path.expanduser(str(recording_root)))
        ).resolve()
        self._cache_root = (self._recording_root / '.remote-media').resolve()
        self._bundle_loader = bundle_loader
        self._downloader = downloader
        self._clock = clock
        self._download_slots = asyncio.Semaphore(_DOWNLOAD_CONCURRENCY)
        self._wake = asyncio.Event()
        self._task: Optional[asyncio.Task[None]] = None

    @property
    def cache_root(self) -> Path:
        return self._cache_root

    async def start(self) -> None:
        if self._task is not None:
            return
        await self.recover_interrupted()
        self._wake.set()
        self._task = asyncio.create_task(self._run(), name='bili-remote-media-cache')

    async def close(self) -> None:
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def recover_interrupted(self) -> int:
        return await self._database.execute(
            "UPDATE vainglory_video_sources SET state='pending',progress=0,"
            "downloaded_bytes=0,total_bytes=NULL,error=NULL,updated_at=? "
            "WHERE state='downloading'",
            (self._now(),),
        )

    async def request(
        self, part_id: int, *, retain_for_playback: bool = False
    ) -> RemoteMediaStatus:
        status = await self._load_status(part_id, create_source=True)
        if retain_for_playback and status.remote_available:
            await self._database.execute(
                "UPDATE vainglory_video_sources SET retention_kind='ten_day',"
                'updated_at=? WHERE part_id=?',
                (self._now(), int(part_id)),
            )
            status = await self.status(part_id)
        if status.state in ('local', 'ready', 'pending', 'downloading'):
            if status.state == 'pending':
                self._wake.set()
            return status
        if not status.remote_available:
            raise RemoteMediaUnavailable('该分 P 没有可下载的 B 站视频源')
        now = self._now()
        changed = await self._database.execute(
            "UPDATE vainglory_video_sources SET state='pending',progress=0,"
            "downloaded_bytes=0,total_bytes=NULL,error=NULL,updated_at=? "
            "WHERE part_id=? AND state IN ('missing','failed')",
            (now, int(part_id)),
        )
        if changed not in (0, 1):
            raise RuntimeError('远程视频缓存状态异常')
        self._wake.set()
        return await self.status(part_id)

    async def status(self, part_id: int) -> RemoteMediaStatus:
        return await self._load_status(part_id, create_source=True)

    async def run_once(self) -> bool:
        async with self._download_slots:
            await self.cleanup_expired()
            claim = await self._claim()
            if claim is None:
                return False
            part_id = int(claim['part_id'])
            target = self._target_path(
                int(claim['account_id']), str(claim['bvid']), int(claim['page'])
            )
            try:
                await asyncio.get_running_loop().run_in_executor(
                    None, target.parent.mkdir, 0o700, True, True
                )
                bundle = await self._bundle_loader(int(claim['account_id']))

                async def report(
                    downloaded_bytes: int, total_bytes: Optional[int]
                ) -> None:
                    await self._update_progress(part_id, downloaded_bytes, total_bytes)

                await self._downloader.download(
                    bundle,
                    bvid=str(claim['bvid']),
                    cid=int(claim['cid']),
                    page=int(claim['page']),
                    target=target,
                    progress=report,
                )
                size = await asyncio.get_running_loop().run_in_executor(
                    None, self._regular_file_size, target
                )
                if size is None or size <= 0:
                    raise RemoteMediaUnavailable('下载完成后没有生成有效视频文件')
                await self._complete(part_id, target, size)
            except (asyncio.CancelledError, KeyboardInterrupt):
                await self._reset_pending(part_id)
                raise
            except BaseException as error:
                await self._mark_failed(
                    part_id, '{}: {}'.format(type(error).__name__, error)
                )
            return True

    async def cleanup_expired(self) -> int:
        now = self._now()
        rows = await self._database.fetchall(
            'SELECT source.part_id,source.cache_path '
            'FROM vainglory_video_sources source '
            'LEFT JOIN vainglory_part_jobs analysis '
            'ON analysis.part_id=source.part_id '
            "WHERE source.state='ready' AND ("
            '(source.expires_at IS NOT NULL AND source.expires_at<=?) OR '
            "(source.retention_kind='analysis' "
            "AND analysis.state IN ('ready','failed'))"
            ') ORDER BY source.part_id',
            (now,),
        )
        cleaned = 0
        for row in rows:
            cache_path = (
                None if row['cache_path'] is None else Path(str(row['cache_path']))
            )
            try:
                if cache_path is not None:
                    await asyncio.get_running_loop().run_in_executor(
                        None, self._unlink_if_present, cache_path
                    )
            except OSError as error:
                await self._mark_failed(
                    int(row['part_id']), '缓存清理失败：{}'.format(error)
                )
                continue
            cleaned += await self._restore_part(
                int(row['part_id']),
                None if cache_path is None else str(cache_path),
                now,
            )
        return cleaned

    async def _load_status(
        self, part_id: int, *, create_source: bool
    ) -> RemoteMediaStatus:
        row = await self._source_row(part_id)
        if row is None and create_source:
            await self._ensure_upload_source(part_id)
            row = await self._source_row(part_id)
        if row is None:
            part = await self._database.fetchone(
                'SELECT source_path,final_path FROM recording_parts WHERE id=?',
                (int(part_id),),
            )
            if part is None:
                raise RemoteMediaNotFound('录制分 P 不存在')
            if await self._has_local_part(part):
                return RemoteMediaStatus(
                    part_id=int(part_id),
                    state='local',
                    progress=1,
                    remote_available=False,
                )
            return RemoteMediaStatus(
                part_id=int(part_id),
                state='unavailable',
                progress=0,
                remote_available=False,
            )

        state = str(row['state'])
        cache_path = None if row['cache_path'] is None else str(row['cache_path'])
        if state == 'ready':
            if cache_path is not None and self._regular_file_size(Path(cache_path)):
                return self._status_from_row(row)
            await self._restore_part(int(part_id), cache_path, self._now())
            row = await self._source_row(part_id)
            assert row is not None
            state = str(row['state'])
        if state not in ('ready', 'pending', 'downloading'):
            if await self._has_local_part(row):
                return RemoteMediaStatus(
                    part_id=int(part_id),
                    state='local',
                    progress=1,
                    remote_available=True,
                    account_id=int(row['account_id']),
                    bvid=str(row['bvid']),
                    cid=int(row['cid']),
                    page=int(row['page']),
                )
        return self._status_from_row(row)

    async def _source_row(self, part_id: int) -> Optional[sqlite3.Row]:
        return await self._database.fetchone(
            'SELECT source.*,part.source_path,part.final_path '
            'FROM vainglory_video_sources source '
            'JOIN recording_parts part ON part.id=source.part_id '
            'WHERE source.part_id=?',
            (int(part_id),),
        )

    async def _ensure_upload_source(self, part_id: int) -> None:
        now = self._now()

        def ensure(connection: sqlite3.Connection) -> None:
            existing = connection.execute(
                'SELECT 1 FROM vainglory_video_sources WHERE part_id=?', (int(part_id),)
            ).fetchone()
            if existing is not None:
                return
            row = connection.execute(
                'SELECT part.id,part.part_index,part.final_path,'
                'part.artifact_state,part.video_deleted_at,part.file_size_bytes,'
                'job.account_id,job.bvid,remote.cid '
                'FROM recording_parts part '
                'JOIN upload_jobs job ON job.session_id=part.session_id '
                'JOIN upload_parts remote ON remote.job_id=job.id '
                'AND remote.part_index=part.part_index '
                'WHERE part.id=? AND job.bvid IS NOT NULL '
                "AND job.bvid!='' AND remote.cid IS NOT NULL "
                "AND job.state IN ('approved','completed')",
                (int(part_id),),
            ).fetchone()
            if row is None:
                part = connection.execute(
                    'SELECT 1 FROM recording_parts WHERE id=?', (int(part_id),)
                ).fetchone()
                if part is None:
                    raise RemoteMediaNotFound('录制分 P 不存在')
                return
            connection.execute(
                'INSERT INTO vainglory_video_sources('
                'part_id,account_id,bvid,cid,page,origin,state,retention_kind,'
                'progress,downloaded_bytes,total_bytes,cache_path,'
                'original_final_path,original_artifact_state,'
                'original_video_deleted_at,original_file_size_bytes,cached_at,'
                'expires_at,error,created_at,updated_at) '
                "VALUES(?,?,?,?,?,'upload','missing','ten_day',0,0,NULL,NULL,"
                '?,?,?,?,NULL,NULL,NULL,?,?)',
                (
                    int(row['id']),
                    int(row['account_id']),
                    str(row['bvid']),
                    int(row['cid']),
                    int(row['part_index']),
                    row['final_path'],
                    str(row['artifact_state']),
                    row['video_deleted_at'],
                    row['file_size_bytes'],
                    now,
                    now,
                ),
            )

        await self._database.write(ensure)

    async def _claim(self) -> Optional[sqlite3.Row]:
        now = self._now()

        def claim(connection: sqlite3.Connection) -> Optional[sqlite3.Row]:
            row = connection.execute(
                'SELECT * FROM vainglory_video_sources '
                "WHERE state='pending' ORDER BY updated_at,part_id LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            changed = connection.execute(
                "UPDATE vainglory_video_sources SET state='downloading',"
                'progress=0,downloaded_bytes=0,total_bytes=NULL,error=NULL,'
                'updated_at=? '
                "WHERE part_id=? AND state='pending'",
                (now, int(row['part_id'])),
            )
            if changed.rowcount != 1:
                return None
            return row

        return await self._database.write(claim)

    async def _update_progress(
        self, part_id: int, downloaded_bytes: int, total_bytes: Optional[int]
    ) -> None:
        downloaded = max(0, int(downloaded_bytes))
        total = None if total_bytes is None else max(1, int(total_bytes))
        if total is not None:
            downloaded = min(downloaded, total)
        progress = 0.0 if total is None else min(0.99, float(downloaded) / float(total))
        await self._database.execute(
            'UPDATE vainglory_video_sources SET progress=?,downloaded_bytes=?,'
            'total_bytes=?,updated_at=? '
            "WHERE part_id=? AND state='downloading'",
            (progress, downloaded, total, self._now(), int(part_id)),
        )

    async def _complete(self, part_id: int, target: Path, size: int) -> None:
        now = self._now()

        def complete(connection: sqlite3.Connection) -> None:
            row = connection.execute(
                'SELECT retention_kind FROM vainglory_video_sources '
                "WHERE part_id=? AND state='downloading'",
                (int(part_id),),
            ).fetchone()
            if row is None:
                raise RemoteMediaUnavailable('下载任务状态已经变化')
            expires_at = now + _TEN_DAYS_SECONDS
            connection.execute(
                'UPDATE recording_parts SET final_path=?,artifact_state=\'ready\','
                'video_deleted_at=NULL,file_size_bytes=?,updated_at=? WHERE id=?',
                (str(target), int(size), now, int(part_id)),
            )
            connection.execute(
                "UPDATE vainglory_video_sources SET state='ready',progress=1,"
                'downloaded_bytes=?,total_bytes=?,cache_path=?,cached_at=?,'
                'expires_at=?,error=NULL,updated_at=? WHERE part_id=?',
                (int(size), int(size), str(target), now, expires_at, now, int(part_id)),
            )

        await self._database.write(complete)

    async def _restore_part(
        self, part_id: int, cache_path: Optional[str], now: int
    ) -> int:
        def restore(connection: sqlite3.Connection) -> int:
            source = connection.execute(
                'SELECT * FROM vainglory_video_sources '
                "WHERE part_id=? AND state='ready'",
                (int(part_id),),
            ).fetchone()
            if source is None:
                return 0
            current = connection.execute(
                'SELECT final_path FROM recording_parts WHERE id=?', (int(part_id),)
            ).fetchone()
            if current is not None and (
                cache_path is None or current['final_path'] == cache_path
            ):
                connection.execute(
                    'UPDATE recording_parts SET final_path=?,artifact_state=?,'
                    'video_deleted_at=?,file_size_bytes=?,updated_at=? WHERE id=?',
                    (
                        source['original_final_path'],
                        str(source['original_artifact_state']),
                        source['original_video_deleted_at'],
                        source['original_file_size_bytes'],
                        now,
                        int(part_id),
                    ),
                )
            connection.execute(
                "UPDATE vainglory_video_sources SET state='missing',progress=0,"
                'downloaded_bytes=0,total_bytes=NULL,cache_path=NULL,cached_at=NULL,'
                'expires_at=NULL,error=NULL,updated_at=? WHERE part_id=?',
                (now, int(part_id)),
            )
            return 1

        return await self._database.write(restore)

    async def _reset_pending(self, part_id: int) -> None:
        await self._database.execute(
            "UPDATE vainglory_video_sources SET state='pending',progress=0,"
            'downloaded_bytes=0,total_bytes=NULL,error=NULL,updated_at=? '
            "WHERE part_id=? AND state='downloading'",
            (self._now(), int(part_id)),
        )

    async def _mark_failed(self, part_id: int, error: str) -> None:
        message = error.strip()[:500] or '远程视频下载失败'
        await self._database.execute(
            "UPDATE vainglory_video_sources SET state='failed',progress=0,"
            'error=?,updated_at=? WHERE part_id=?',
            (message, self._now(), int(part_id)),
        )

    async def _run(self) -> None:
        await asyncio.gather(
            *(self._run_worker() for _ in range(_DOWNLOAD_CONCURRENCY))
        )

    async def _run_worker(self) -> None:
        while True:
            processed = await self.run_once()
            if processed:
                continue
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=30)
            except asyncio.TimeoutError:
                pass

    def _target_path(self, account_id: int, bvid: str, page: int) -> Path:
        target = (
            self._cache_root
            / str(int(account_id))
            / bvid
            / 'p{:04d}.mp4'.format(int(page))
        ).resolve()
        try:
            target.relative_to(self._cache_root)
        except ValueError:
            raise RemoteMediaUnavailable('远程视频缓存路径无效') from None
        return target

    async def _has_local_part(self, row: sqlite3.Row) -> bool:
        paths = []
        if row['final_path'] is not None:
            paths.append(str(row['final_path']))
        if row['source_path'] is not None:
            paths.append(str(row['source_path']))
        loop = asyncio.get_running_loop()
        for path in dict.fromkeys(paths):
            if (
                await loop.run_in_executor(None, self._regular_file_size, Path(path))
                is not None
            ):
                return True
        return False

    @staticmethod
    def _status_from_row(row: sqlite3.Row) -> RemoteMediaStatus:
        return RemoteMediaStatus(
            part_id=int(row['part_id']),
            state=str(row['state']),
            progress=float(row['progress']),
            remote_available=True,
            account_id=int(row['account_id']),
            bvid=str(row['bvid']),
            cid=int(row['cid']),
            page=int(row['page']),
            downloaded_bytes=int(row['downloaded_bytes']),
            total_bytes=(
                None if row['total_bytes'] is None else int(row['total_bytes'])
            ),
            cache_path=(None if row['cache_path'] is None else str(row['cache_path'])),
            cached_at=(None if row['cached_at'] is None else int(row['cached_at'])),
            expires_at=(None if row['expires_at'] is None else int(row['expires_at'])),
            error=None if row['error'] is None else str(row['error']),
        )

    def _now(self) -> int:
        return max(1, int(self._clock()))

    @staticmethod
    def _regular_file_size(path: Path) -> Optional[int]:
        try:
            result = path.stat()
        except OSError:
            return None
        if not stat.S_ISREG(result.st_mode):
            return None
        return int(result.st_size)

    @staticmethod
    def _unlink_if_present(path: Path) -> None:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
