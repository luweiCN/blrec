from __future__ import annotations

import asyncio
import os
import sqlite3
import stat
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Optional, Protocol, Tuple

from loguru import logger

from blrec.networking.manager import NetworkRouteManager

from .bili_download import BiliDownloadContractError, BiliDownloadRoutePaused
from .database import BiliUploadDatabase

__all__ = (
    'RemoteMediaCache',
    'RemoteMediaDownloader',
    'RemoteMediaNotFound',
    'RemoteMediaQueueItem',
    'RemoteMediaQueuePage',
    'RemoteMediaQueueStatus',
    'RemoteMediaStatus',
    'RemoteMediaUnavailable',
)

_TEN_DAYS_SECONDS = 10 * 24 * 60 * 60
_DEFAULT_DOWNLOADS_PER_INTERFACE = 3
_MAX_DOWNLOADS_PER_INTERFACE = 8
_MAX_DOWNLOAD_ATTEMPTS = 3
_DOWNLOAD_RETRY_DELAYS_SECONDS = (30, 120)
_SAME_INTERFACE_RETRY_GRACE_SECONDS = 30
_PERMANENT_DOWNLOAD_ERRORS = (
    'B 站稿件分 P 信息无效',
    'NAS 容器中没有可用的 yt-dlp',
    '账号 Cookie 格式无效',
    '账号没有可用于下载的有效 Cookie',
    '远程视频缓存路径无效',
    'yt-dlp 返回的文件路径越界',
    'yt-dlp 返回了意外的文件路径',
)


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


@dataclass(frozen=True)
class RemoteMediaQueueStatus:
    pending_download_count: int
    pending_download_archive_count: int
    active_download_count: int
    active_download_archive_count: int
    downloaded_waiting_analysis_count: int
    downloaded_waiting_analysis_archive_count: int
    active_analysis_count: int
    active_analysis_archive_count: int
    failed_download_count: int
    failed_download_archive_count: int
    downloads_per_interface: int
    interface_count: int
    total_concurrency: int
    latest_activity_at: Optional[int]


@dataclass(frozen=True)
class RemoteMediaQueueItem:
    part_id: int
    archive_import_id: Optional[int]
    account_id: int
    account_name: str
    bvid: str
    archive_title: str
    page: int
    page_count: int
    part_title: str
    queue_state: str
    source_state: str
    analysis_state: Optional[str]
    progress: float
    downloaded_bytes: int
    total_bytes: Optional[int]
    speed_bytes_per_second: Optional[int]
    error: Optional[str]
    updated_at: int


@dataclass(frozen=True)
class RemoteMediaQueuePage:
    total: int
    archive_count: int
    items: Tuple[RemoteMediaQueueItem, ...]


class RemoteMediaCache:
    def __init__(
        self,
        database: BiliUploadDatabase,
        recording_root: Path,
        *,
        bundle_loader: Callable[[int], Awaitable[Any]],
        downloader: RemoteMediaDownloader,
        network_manager: Optional[NetworkRouteManager] = None,
        download_interfaces: Tuple[str, ...] = (),
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._database = database
        self._recording_root = Path(
            os.path.abspath(os.path.expanduser(str(recording_root)))
        ).resolve()
        self._cache_root = (self._recording_root / '.remote-media').resolve()
        self._bundle_loader = bundle_loader
        self._downloader = downloader
        self._network_manager = network_manager
        self._download_interfaces = tuple(download_interfaces)
        self._clock = clock
        self._wake = asyncio.Event()
        self._claim_lock = asyncio.Lock()
        self._paused_part_interfaces: Dict[int, str] = {}
        self._download_speeds: Dict[int, Tuple[float, int, int]] = {}
        self._downloads_per_interface = _DEFAULT_DOWNLOADS_PER_INTERFACE
        self._task: Optional[asyncio.Task[None]] = None

    @property
    def cache_root(self) -> Path:
        return self._cache_root

    async def start(self) -> None:
        if self._task is not None:
            return
        self._downloads_per_interface = await self._load_downloads_per_interface()
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

    async def _recover_orphaned_downloads(self) -> int:
        active_part_ids = tuple(sorted(self._download_speeds))
        if not active_part_ids:
            return await self.recover_interrupted()
        placeholders = ','.join('?' for _part_id in active_part_ids)
        return await self._database.execute(
            "UPDATE vainglory_video_sources SET state='pending',progress=0,"
            'downloaded_bytes=0,total_bytes=NULL,error=NULL,updated_at=? '
            "WHERE state='downloading' AND part_id NOT IN ({})".format(placeholders),
            (self._now(), *active_part_ids),
        )

    async def queue_status(self) -> RemoteMediaQueueStatus:
        counts = await self._database.fetchone(
            'SELECT '
            "COALESCE(SUM(CASE WHEN source.state='pending' THEN 1 ELSE 0 END),0) "
            'AS pending_download_count,'
            "COUNT(DISTINCT CASE WHEN source.state='pending' "
            'THEN COALESCE(archive.import_id,-source.part_id) END) '
            'AS pending_download_archive_count,'
            "COALESCE(SUM(CASE WHEN source.state='downloading' THEN 1 ELSE 0 END),0) "
            'AS active_download_count,'
            "COUNT(DISTINCT CASE WHEN source.state='downloading' "
            'THEN COALESCE(archive.import_id,-source.part_id) END) '
            'AS active_download_archive_count,'
            'COALESCE(SUM(CASE WHEN source.state=\'ready\' '
            "AND (analysis.state IS NULL OR analysis.state='pending') "
            'THEN 1 ELSE 0 END),0) AS downloaded_waiting_analysis_count,'
            'COUNT(DISTINCT CASE WHEN source.state=\'ready\' '
            "AND (analysis.state IS NULL OR analysis.state='pending') "
            'THEN COALESCE(archive.import_id,-source.part_id) END) '
            'AS downloaded_waiting_analysis_archive_count,'
            'COALESCE(SUM(CASE WHEN source.state=\'ready\' '
            "AND analysis.state='analyzing' THEN 1 ELSE 0 END),0) "
            'AS active_analysis_count,'
            'COUNT(DISTINCT CASE WHEN source.state=\'ready\' '
            "AND analysis.state='analyzing' "
            'THEN COALESCE(archive.import_id,-source.part_id) END) '
            'AS active_analysis_archive_count,'
            "COALESCE(SUM(CASE WHEN source.state='failed' THEN 1 ELSE 0 END),0) "
            'AS failed_download_count,'
            "COUNT(DISTINCT CASE WHEN source.state='failed' "
            'THEN COALESCE(archive.import_id,-source.part_id) END) '
            'AS failed_download_archive_count,'
            'MAX(source.updated_at) AS latest_activity_at '
            'FROM vainglory_video_sources source '
            'LEFT JOIN vainglory_archive_parts archive '
            'ON archive.recording_part_id=source.part_id '
            'LEFT JOIN vainglory_part_jobs analysis ON analysis.part_id=source.part_id'
        )
        downloads_per_interface = await self._load_downloads_per_interface()
        self._downloads_per_interface = downloads_per_interface
        interface_count = max(1, len(self._download_interfaces))
        assert counts is not None
        return RemoteMediaQueueStatus(
            pending_download_count=int(counts['pending_download_count']),
            pending_download_archive_count=int(
                counts['pending_download_archive_count']
            ),
            active_download_count=int(counts['active_download_count']),
            active_download_archive_count=int(counts['active_download_archive_count']),
            downloaded_waiting_analysis_count=int(
                counts['downloaded_waiting_analysis_count']
            ),
            downloaded_waiting_analysis_archive_count=int(
                counts['downloaded_waiting_analysis_archive_count']
            ),
            active_analysis_count=int(counts['active_analysis_count']),
            active_analysis_archive_count=int(counts['active_analysis_archive_count']),
            failed_download_count=int(counts['failed_download_count']),
            failed_download_archive_count=int(counts['failed_download_archive_count']),
            downloads_per_interface=downloads_per_interface,
            interface_count=interface_count,
            total_concurrency=downloads_per_interface * interface_count,
            latest_activity_at=(
                None
                if counts['latest_activity_at'] is None
                else int(counts['latest_activity_at'])
            ),
        )

    async def queue_items(
        self, queue_state: str, *, limit: int = 50, offset: int = 0
    ) -> RemoteMediaQueuePage:
        conditions = {
            'pending': "source.state='pending'",
            'downloading': "source.state='downloading'",
            'downloaded_waiting_analysis': (
                "source.state='ready' AND "
                "(analysis.state IS NULL OR analysis.state='pending')"
            ),
            'analyzing': ("source.state='ready' AND analysis.state='analyzing'"),
            'failed': "source.state='failed'",
        }
        condition = conditions.get(queue_state)
        if condition is None:
            raise ValueError('下载队列状态无效')
        if not 1 <= int(limit) <= 200 or int(offset) < 0:
            raise ValueError('下载队列分页参数无效')
        joins = (
            ' FROM vainglory_video_sources source '
            'LEFT JOIN vainglory_archive_parts archive '
            'ON archive.recording_part_id=source.part_id '
            'LEFT JOIN vainglory_archive_imports imported '
            'ON imported.id=archive.import_id '
            'LEFT JOIN vainglory_part_jobs analysis '
            'ON analysis.part_id=source.part_id '
            'LEFT JOIN recording_parts part ON part.id=source.part_id '
            'LEFT JOIN recording_sessions session ON session.id=part.session_id '
            'JOIN bili_accounts account ON account.id=source.account_id '
        )
        counts = await self._database.fetchone(
            'SELECT COUNT(*) AS total,'
            'COUNT(DISTINCT COALESCE(archive.import_id,-source.part_id)) '
            'AS archive_count' + joins + 'WHERE ' + condition
        )
        rows = await self._database.fetchall(
            'SELECT source.part_id,archive.import_id AS archive_import_id,'
            'source.account_id,account.display_name AS account_name,'
            'source.bvid,COALESCE(imported.title,session.title,source.bvid) '
            'AS archive_title,source.page,COALESCE(imported.page_count,1) '
            'AS page_count,COALESCE(archive.title,\'P\' || source.page) '
            'AS part_title,source.state AS source_state,'
            'analysis.state AS analysis_state,source.progress,'
            'source.downloaded_bytes,source.total_bytes,'
            'COALESCE(source.error,source.last_attempt_error) AS error,'
            'source.updated_at'
            + joins
            + 'WHERE '
            + condition
            + ' '
            + (
                'ORDER BY source.part_id ASC '
                if queue_state == 'downloading'
                else 'ORDER BY source.updated_at DESC,source.part_id DESC '
            )
            + 'LIMIT ? OFFSET ?',
            (int(limit), int(offset)),
        )
        assert counts is not None
        return RemoteMediaQueuePage(
            total=int(counts['total']),
            archive_count=int(counts['archive_count']),
            items=tuple(self._queue_item(row, queue_state=queue_state) for row in rows),
        )

    async def failed_part_ids(self) -> Tuple[int, ...]:
        rows = await self._database.fetchall(
            'SELECT part_id FROM vainglory_video_sources '
            "WHERE state='failed' ORDER BY part_id"
        )
        return tuple(int(row['part_id']) for row in rows)

    async def update_downloads_per_interface(
        self, downloads_per_interface: int
    ) -> RemoteMediaQueueStatus:
        value = int(downloads_per_interface)
        if not 1 <= value <= _MAX_DOWNLOADS_PER_INTERFACE:
            raise ValueError('每条线路的下载并发必须在 1 到 8 之间')
        changed = await self._database.execute(
            'UPDATE vainglory_remote_media_controls '
            'SET downloads_per_interface=?,updated_at=? WHERE singleton_id=1',
            (value, self._now()),
        )
        if changed != 1:
            raise RuntimeError('远程视频下载并发配置不存在')
        self._downloads_per_interface = value
        self._wake.set()
        return await self.queue_status()

    async def request(
        self,
        part_id: int,
        *,
        retain_for_playback: bool = False,
        force_remote: bool = False,
    ) -> RemoteMediaStatus:
        status = await self._load_status(part_id, create_source=True)
        if retain_for_playback and status.remote_available:
            await self._database.execute(
                "UPDATE vainglory_video_sources SET retention_kind='ten_day',"
                'updated_at=? WHERE part_id=?',
                (self._now(), int(part_id)),
            )
            status = await self.status(part_id)
        if status.state == 'local' and not force_remote:
            return status
        if status.state in ('ready', 'pending', 'downloading'):
            if status.state == 'pending':
                self._wake.set()
            return status
        if not status.remote_available:
            raise RemoteMediaUnavailable('该分 P 没有可下载的 B 站视频源')
        now = self._now()
        changed = await self._database.execute(
            "UPDATE vainglory_video_sources SET state='pending',progress=0,"
            'downloaded_bytes=0,total_bytes=NULL,error=NULL,attempt_count=0,'
            'next_attempt_at=0,last_attempt_error=NULL,'
            'last_attempt_interface=NULL,updated_at=? '
            "WHERE part_id=? AND state IN ('missing','failed')",
            (now, int(part_id)),
        )
        if changed not in (0, 1):
            raise RuntimeError('远程视频缓存状态异常')
        self._wake.set()
        return await self.status(part_id)

    async def status(self, part_id: int) -> RemoteMediaStatus:
        return await self._load_status(part_id, create_source=True)

    async def run_once(
        self,
        *,
        network_interface: Optional[str] = None,
        worker_index: Optional[int] = None,
    ) -> bool:
        if (
            network_interface is not None
            and self._network_manager is not None
            and not self._network_manager.interface_available(
                'archive_download', network_interface
            )
        ):
            return False
        await self.cleanup_expired()
        claim = await self._claim(network_interface)
        if claim is None:
            return False
        part_id = int(claim['part_id'])
        self._download_speeds[part_id] = (time.monotonic(), 0, 0)
        target = self._target_path(
            int(claim['account_id']), str(claim['bvid']), int(claim['page'])
        )
        try:
            await asyncio.get_running_loop().run_in_executor(
                None, target.parent.mkdir, 0o700, True, True
            )
            bundle = await self._bundle_loader(int(claim['account_id']))

            async def report(downloaded_bytes: int, total_bytes: Optional[int]) -> None:
                await self._update_progress(part_id, downloaded_bytes, total_bytes)

            download_on_interface = getattr(
                self._downloader, 'download_on_interface', None
            )
            if network_interface is not None and callable(download_on_interface):
                await download_on_interface(
                    bundle,
                    bvid=str(claim['bvid']),
                    cid=int(claim['cid']),
                    page=int(claim['page']),
                    target=target,
                    progress=report,
                    interface_name=network_interface,
                    affinity_key='archive-download-slot:{}'.format(worker_index or 0),
                )
            else:
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
        except BiliDownloadRoutePaused:
            if network_interface is None:
                await self._reset_pending(part_id)
            else:
                await self._pause_download(part_id, network_interface)
        except BaseException as error:
            await self._handle_download_failure(
                claim, error, network_interface=network_interface
            )
        finally:
            self._download_speeds.pop(part_id, None)
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
            "AND analysis.state IN ('ready','failed'))) "
            'AND NOT EXISTS(SELECT 1 FROM vainglory_match_rerun_jobs rerun '
            'JOIN vainglory_matches match ON match.id=rerun.match_id '
            'WHERE match.result_part_id=source.part_id '
            "AND rerun.state IN ('pending','running')) "
            'ORDER BY source.part_id',
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

    async def _claim(self, network_interface: Optional[str]) -> Optional[sqlite3.Row]:
        now = self._now()
        async with self._claim_lock:
            excluded = tuple(
                part_id
                for part_id, interface_name in self._paused_part_interfaces.items()
                if interface_name != network_interface
            )
            placeholders = ','.join('?' for _ in excluded)
            exclusion = (
                ''
                if not excluded
                else 'AND source.part_id NOT IN ({}) '.format(placeholders)
            )

            def claim(connection: sqlite3.Connection) -> Optional[sqlite3.Row]:
                row = connection.execute(
                    'SELECT source.* FROM vainglory_video_sources source '
                    'LEFT JOIN vainglory_archive_parts archive '
                    'ON archive.recording_part_id=source.part_id '
                    'LEFT JOIN vainglory_archive_imports imported '
                    'ON imported.id=archive.import_id '
                    "WHERE source.state='pending' AND source.next_attempt_at<=? "
                    'AND (CAST(? AS TEXT) IS NULL '
                    'OR source.last_attempt_interface IS NULL '
                    'OR source.last_attempt_interface!=? '
                    'OR source.next_attempt_at+?<=?) '
                    "AND (imported.id IS NULL OR imported.state!='skipped') "
                    'AND NOT EXISTS(SELECT 1 '
                    'FROM archive_migration_items migration '
                    'JOIN upload_jobs migrated_upload '
                    'ON migrated_upload.id=migration.upload_job_id '
                    'WHERE migrated_upload.account_id=imported.account_id '
                    'AND migrated_upload.bvid=imported.bvid '
                    'AND migrated_upload.session_id=migration.session_id) '
                    + exclusion
                    + 'ORDER BY CASE WHEN EXISTS('
                    'SELECT 1 FROM vainglory_part_jobs manual_job '
                    'WHERE manual_job.part_id=source.part_id '
                    "AND manual_job.state='pending' "
                    "AND manual_job.request_kind='manual') THEN 0 "
                    "WHEN source.retention_kind='ten_day' THEN 1 ELSE 2 END,"
                    'CASE WHEN archive.import_id IS NOT NULL AND EXISTS('
                    'SELECT 1 FROM vainglory_archive_parts active_archive '
                    'JOIN vainglory_video_sources active_source '
                    'ON active_source.part_id=active_archive.recording_part_id '
                    'WHERE active_archive.import_id=archive.import_id '
                    "AND active_source.state IN ('downloading','ready')) "
                    'THEN 0 ELSE 1 END,'
                    'COALESCE(imported.recording_started_at,'
                    'imported.published_at,imported.created_at,'
                    'source.updated_at) DESC,archive.import_id,archive.page,'
                    'source.updated_at,source.part_id LIMIT 1',
                    (
                        now,
                        network_interface,
                        network_interface,
                        _SAME_INTERFACE_RETRY_GRACE_SECONDS,
                        now,
                        *excluded,
                    ),
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

            row = await self._database.write(claim)
            if row is not None:
                self._paused_part_interfaces.pop(int(row['part_id']), None)
            return row

    async def _update_progress(
        self, part_id: int, downloaded_bytes: int, total_bytes: Optional[int]
    ) -> None:
        downloaded = max(0, int(downloaded_bytes))
        total = None if total_bytes is None else max(1, int(total_bytes))
        if total is not None:
            downloaded = min(downloaded, total)
        current_at = time.monotonic()
        previous = self._download_speeds.get(int(part_id))
        speed = 0
        if previous is not None and current_at > previous[0]:
            speed = max(0, int((downloaded - previous[1]) / (current_at - previous[0])))
            if speed == 0:
                speed = previous[2]
        self._download_speeds[int(part_id)] = (current_at, downloaded, speed)
        progress = 0.0 if total is None else min(0.99, float(downloaded) / float(total))
        await self._database.execute(
            'UPDATE vainglory_video_sources SET progress=?,downloaded_bytes=?,'
            'total_bytes=?,updated_at=? '
            "WHERE part_id=? AND state='downloading'",
            (progress, downloaded, total, self._now(), int(part_id)),
        )

    def _queue_item(
        self, row: sqlite3.Row, *, queue_state: str
    ) -> RemoteMediaQueueItem:
        speed_sample = self._download_speeds.get(int(row['part_id']))
        return RemoteMediaQueueItem(
            part_id=int(row['part_id']),
            archive_import_id=(
                None
                if row['archive_import_id'] is None
                else int(row['archive_import_id'])
            ),
            account_id=int(row['account_id']),
            account_name=str(row['account_name']),
            bvid=str(row['bvid']),
            archive_title=str(row['archive_title']),
            page=int(row['page']),
            page_count=max(1, int(row['page_count'])),
            part_title=str(row['part_title']),
            queue_state=queue_state,
            source_state=str(row['source_state']),
            analysis_state=(
                None if row['analysis_state'] is None else str(row['analysis_state'])
            ),
            progress=float(row['progress']),
            downloaded_bytes=int(row['downloaded_bytes']),
            total_bytes=(
                None if row['total_bytes'] is None else int(row['total_bytes'])
            ),
            speed_bytes_per_second=(
                None if speed_sample is None else int(speed_sample[2])
            ),
            error=None if row['error'] is None else str(row['error']),
            updated_at=int(row['updated_at']),
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
                'expires_at=?,error=NULL,next_attempt_at=0,'
                'last_attempt_error=NULL,last_attempt_interface=NULL,'
                'updated_at=? WHERE part_id=?',
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
                'expires_at=NULL,error=NULL,attempt_count=0,next_attempt_at=0,'
                'last_attempt_error=NULL,last_attempt_interface=NULL,'
                'updated_at=? WHERE part_id=?',
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

    async def _pause_download(self, part_id: int, interface_name: str) -> None:
        async with self._claim_lock:
            self._paused_part_interfaces[int(part_id)] = interface_name
            await self._reset_pending(part_id)

    async def _mark_failed(self, part_id: int, error: str) -> None:
        message = error.strip()[:500] or '远程视频下载失败'
        await self._database.execute(
            "UPDATE vainglory_video_sources SET state='failed',progress=0,"
            'error=?,next_attempt_at=0,updated_at=? WHERE part_id=?',
            (message, self._now(), int(part_id)),
        )

    async def _handle_download_failure(
        self,
        claim: sqlite3.Row,
        error: BaseException,
        *,
        network_interface: Optional[str],
    ) -> None:
        part_id = int(claim['part_id'])
        attempt_count = int(claim['attempt_count']) + 1
        message = '{}: {}'.format(type(error).__name__, error).strip()[:500]
        retryable = self._download_error_is_retryable(error)
        if retryable and attempt_count < _MAX_DOWNLOAD_ATTEMPTS:
            delay = _DOWNLOAD_RETRY_DELAYS_SECONDS[attempt_count - 1]
            now = self._now()
            await self._database.execute(
                "UPDATE vainglory_video_sources SET state='pending',progress=0,"
                'downloaded_bytes=0,total_bytes=NULL,error=NULL,attempt_count=?,'
                'next_attempt_at=?,last_attempt_error=?,'
                'last_attempt_interface=?,updated_at=? '
                "WHERE part_id=? AND state='downloading'",
                (
                    attempt_count,
                    now + delay,
                    message or '远程视频下载失败',
                    network_interface,
                    now,
                    part_id,
                ),
            )
            logger.warning(
                'remote media download will retry: part_id={}, attempt={}, '
                'delay_seconds={}, interface={}',
                part_id,
                attempt_count,
                delay,
                network_interface,
            )
            self._wake.set()
            return
        await self._database.execute(
            "UPDATE vainglory_video_sources SET state='failed',progress=0,"
            'error=?,attempt_count=?,next_attempt_at=0,last_attempt_error=?,'
            'last_attempt_interface=?,updated_at=? WHERE part_id=?',
            (
                message or '远程视频下载失败',
                attempt_count,
                message or '远程视频下载失败',
                network_interface,
                self._now(),
                part_id,
            ),
        )

    @staticmethod
    def _download_error_is_retryable(error: BaseException) -> bool:
        if isinstance(error, RemoteMediaUnavailable):
            return True
        if not isinstance(error, BiliDownloadContractError):
            return False
        message = str(error)
        return not any(value in message for value in _PERMANENT_DOWNLOAD_ERRORS)

    async def _run(self) -> None:
        interfaces: Tuple[Optional[str], ...] = (
            tuple(self._download_interfaces) if self._download_interfaces else (None,)
        )
        await asyncio.gather(
            *(
                self._run_worker(
                    interface_name,
                    interface_index * _MAX_DOWNLOADS_PER_INTERFACE + slot_index,
                    slot_index,
                )
                for interface_index, interface_name in enumerate(interfaces)
                for slot_index in range(_MAX_DOWNLOADS_PER_INTERFACE)
            )
        )

    async def _run_worker(
        self, interface_name: Optional[str], worker_index: int, slot_index: int
    ) -> None:
        while True:
            if slot_index >= self._downloads_per_interface:
                await asyncio.sleep(1)
                continue
            if (
                interface_name is not None
                and self._network_manager is not None
                and not self._network_manager.interface_available(
                    'archive_download', interface_name
                )
            ):
                await asyncio.sleep(1)
                continue
            try:
                processed = await self.run_once(
                    network_interface=interface_name, worker_index=worker_index
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    'remote media download worker failed; recovering orphaned '
                    'downloads and retrying'
                )
                try:
                    await self._recover_orphaned_downloads()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception('remote media orphan recovery failed')
                await asyncio.sleep(5)
                continue
            if processed:
                continue
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=30)
            except asyncio.TimeoutError:
                pass

    async def _load_downloads_per_interface(self) -> int:
        value = await self._database.scalar(
            'SELECT downloads_per_interface '
            'FROM vainglory_remote_media_controls WHERE singleton_id=1'
        )
        if value is None:
            raise RuntimeError('远程视频下载并发配置不存在')
        normalized = int(value)
        if not 1 <= normalized <= _MAX_DOWNLOADS_PER_INTERFACE:
            raise RuntimeError('远程视频下载并发配置无效')
        return normalized

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
