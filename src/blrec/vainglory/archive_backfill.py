from __future__ import annotations

import asyncio
import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Awaitable, Callable, List, Mapping, Optional, Tuple

from blrec.bili_upload.database import BiliUploadDatabase
from blrec.bili_upload.errors import BiliApiError
from blrec.compat import ZoneInfo

from .anchor_identity import infer_recorded_anchor
from .exclusions import is_excluded_title
from .repository import refresh_session_scan_job
from .title_time import current_season_started_at, resolve_recording_started_at


class ArchiveBackfillNotFound(ValueError):
    pass


class ArchiveBackfillUnavailable(ValueError):
    pass


@dataclass(frozen=True)
class ArchiveSync:
    account_id: int
    state: str
    progress: float
    discovered_count: int
    completed_count: int
    error: Optional[str]
    requested_at: int
    started_at: Optional[int]
    completed_at: Optional[int]
    updated_at: int
    operator_paused: bool = False
    daily_limit: int = 20
    daily_used: int = 0
    quota_day: Optional[str] = None
    next_page: int = 1
    discovery_complete: bool = False


@dataclass(frozen=True)
class ArchiveBackfillItem:
    id: int
    account_id: int
    aid: int
    bvid: str
    title: str
    published_at: Optional[int]
    state: str
    stage: str
    progress: float
    page_count: int
    completed_page_count: int
    current_page: Optional[int]
    current_part_title: Optional[str]
    download_progress: float
    downloaded_bytes: int
    total_bytes: Optional[int]
    analysis_state: Optional[str]
    analysis_progress: float
    match_count: int
    publication_state: Optional[str]
    description_state: Optional[str]
    comment_count: int
    confirmed_comment_count: int
    pin_state: Optional[str]
    publication_progress: float
    error: Optional[str]
    updated_at: int


@dataclass(frozen=True)
class ArchiveContentReview:
    id: int
    account_id: int
    account_name: str
    aid: int
    bvid: str
    title: str
    published_at: Optional[int]
    reason: str


@dataclass(frozen=True)
class ArchiveContentReviewPage:
    total: int
    items: Tuple[ArchiveContentReview, ...]


@dataclass(frozen=True)
class _Archive:
    aid: int
    bvid: str
    title: str
    published_at: Optional[int]


@dataclass(frozen=True)
class _ArchivePage:
    page: int
    cid: int
    title: str
    duration_seconds: Optional[int]


class ArchiveBackfillService:
    PAGE_SIZE = 50
    MAX_PAGES = 200
    DISCOVERY_INTERVAL_SECONDS = 15
    RETRY_BASE_SECONDS = 5 * 60
    RETRY_MAX_SECONDS = 6 * 60 * 60
    METADATA_COOLDOWN_SECONDS = 15 * 60

    def __init__(
        self,
        database: BiliUploadDatabase,
        archive_reader: Any,
        *,
        bundle_loader: Callable[[int], Awaitable[Any]],
        remote_media_cache: Any,
        clock: Callable[[], float] = time.time,
        idle_poll_seconds: float = 10,
    ) -> None:
        if idle_poll_seconds <= 0:
            raise ValueError('idle poll interval must be positive')
        self._database = database
        self._archive_reader = archive_reader
        self._bundle_loader = bundle_loader
        self._remote_media_cache = remote_media_cache
        self._clock = clock
        self._idle_poll_seconds = idle_poll_seconds
        self._next_discovery_at = 0
        self._wake = asyncio.Event()
        self._task: Optional[asyncio.Task[None]] = None

    async def start(self) -> None:
        if self._task is not None:
            return
        await self.recover_interrupted()
        self._wake.set()
        self._task = asyncio.create_task(self._run(), name='vainglory-archive-backfill')

    async def recover_interrupted(self) -> int:
        return await self._database.execute(
            "UPDATE vainglory_archive_imports SET state='queued',"
            'progress=0,error=NULL,updated_at=? '
            "WHERE state='downloading' AND page_count=0",
            (self._now(),),
        )

    async def close(self) -> None:
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def request(self, account_id: int) -> ArchiveSync:
        account = await self._database.fetchone(
            'SELECT state FROM bili_accounts WHERE id=?', (int(account_id),)
        )
        if account is None:
            raise ArchiveBackfillNotFound('B 站账号不存在')
        if str(account['state']) != 'active':
            raise ArchiveBackfillUnavailable('只能回填当前可用的 B 站账号')
        now = self._now()
        await self._database.execute(
            'INSERT INTO vainglory_archive_syncs('
            'account_id,state,progress,discovered_count,completed_count,error,'
            'requested_at,started_at,completed_at,updated_at) '
            "VALUES(?,'discovering',0,0,0,NULL,?,NULL,NULL,?) "
            'ON CONFLICT(account_id) DO UPDATE SET '
            "state='discovering',progress=0,discovered_count=0,"
            'completed_count=0,error=NULL,requested_at=excluded.requested_at,'
            'started_at=NULL,completed_at=NULL,updated_at=excluded.updated_at,'
            'operator_paused=0,next_page=1,discovery_complete=0,'
            'last_page_identity=NULL',
            (int(account_id), now, now),
        )
        self._next_discovery_at = 0
        self._wake.set()
        return await self.status(account_id)

    async def status(self, account_id: int) -> ArchiveSync:
        row = await self._database.fetchone(
            'SELECT * FROM vainglory_archive_syncs WHERE account_id=?',
            (int(account_id),),
        )
        if row is None:
            raise ArchiveBackfillNotFound('该账号还没有历史回填任务')
        return self._sync(row)

    async def list_statuses(self) -> Tuple[ArchiveSync, ...]:
        rows = await self._database.fetchall(
            'SELECT * FROM vainglory_archive_syncs ORDER BY account_id'
        )
        return tuple(self._sync(row) for row in rows)

    async def list_items(
        self, account_id: int, *, limit: int = 30
    ) -> Tuple[ArchiveBackfillItem, ...]:
        if not 1 <= int(limit) <= 100:
            raise ValueError('历史稿件明细数量必须在 1 到 100 之间')
        if (
            await self._database.scalar(
                'SELECT 1 FROM vainglory_archive_syncs WHERE account_id=?',
                (int(account_id),),
            )
            != 1
        ):
            raise ArchiveBackfillNotFound('该账号还没有历史回填任务')
        rows = await self._database.fetchall(
            'SELECT imported.*,current.page AS current_page,'
            'current.title AS current_part_title,'
            'current.state AS current_part_state,'
            'source.state AS source_state,source.error AS source_error,'
            'analysis.state AS analysis_state,'
            'analysis.progress AS current_analysis_progress,'
            'analysis.error AS analysis_error,'
            'COALESCE((SELECT AVG(CASE download.state '
            "WHEN 'ready' THEN 1.0 ELSE download.progress END) "
            'FROM vainglory_archive_parts download_part '
            'LEFT JOIN vainglory_video_sources download '
            'ON download.part_id=download_part.recording_part_id '
            'WHERE download_part.import_id=imported.id),0) '
            'AS download_progress,'
            'COALESCE((SELECT SUM(download.downloaded_bytes) '
            'FROM vainglory_archive_parts download_part '
            'JOIN vainglory_video_sources download '
            'ON download.part_id=download_part.recording_part_id '
            'WHERE download_part.import_id=imported.id),0) AS downloaded_bytes,'
            '(SELECT SUM(download.total_bytes) '
            'FROM vainglory_archive_parts download_part '
            'JOIN vainglory_video_sources download '
            'ON download.part_id=download_part.recording_part_id '
            'WHERE download_part.import_id=imported.id) AS total_bytes,'
            'COALESCE((SELECT AVG(CASE part_analysis.state '
            "WHEN 'ready' THEN 1.0 ELSE part_analysis.progress END) "
            'FROM vainglory_archive_parts analysis_part '
            'LEFT JOIN vainglory_part_jobs part_analysis '
            'ON part_analysis.part_id=analysis_part.recording_part_id '
            'WHERE analysis_part.import_id=imported.id),0) '
            'AS analysis_progress,'
            '(SELECT COUNT(*) FROM vainglory_matches match '
            'JOIN vainglory_archive_parts match_part '
            'ON match_part.recording_part_id=match.result_part_id '
            'WHERE match_part.import_id=imported.id) AS match_count,'
            'publication.state AS publication_state,'
            'publication.chapter_state,publication.description_state,'
            'publication.pin_state,'
            'publication.error AS publication_error,'
            'COALESCE((SELECT COUNT(*) FROM vainglory_publication_comments comment '
            'WHERE comment.publication_id=publication.id),0) AS comment_count,'
            'COALESCE((SELECT COUNT(*) FROM vainglory_publication_comments comment '
            "WHERE comment.publication_id=publication.id AND comment.state='confirmed'"
            '),0) AS confirmed_comment_count,'
            '(SELECT comment.error FROM vainglory_publication_comments comment '
            'WHERE comment.publication_id=publication.id '
            'AND comment.error IS NOT NULL ORDER BY comment.ordinal LIMIT 1) '
            'AS comment_error '
            'FROM vainglory_archive_imports imported '
            'LEFT JOIN vainglory_archive_parts current ON current.id=('
            'SELECT candidate.id FROM vainglory_archive_parts candidate '
            'WHERE candidate.import_id=imported.id '
            "ORDER BY CASE candidate.state WHEN 'downloading' THEN 0 "
            "WHEN 'analyzing' THEN 1 WHEN 'queued' THEN 2 "
            "WHEN 'failed' THEN 3 ELSE 4 END,candidate.page LIMIT 1) "
            'LEFT JOIN vainglory_video_sources source '
            'ON source.part_id=current.recording_part_id '
            'LEFT JOIN vainglory_part_jobs analysis '
            'ON analysis.part_id=current.recording_part_id '
            'LEFT JOIN vainglory_publications publication '
            'ON publication.account_id=imported.account_id '
            'AND publication.bvid=imported.bvid '
            'WHERE imported.account_id=? '
            "ORDER BY CASE imported.state WHEN 'downloading' THEN 0 "
            "WHEN 'analyzing' THEN 1 WHEN 'queued' THEN 2 ELSE 3 END,"
            'imported.updated_at DESC,imported.id DESC LIMIT ?',
            (int(account_id), int(limit)),
        )
        return tuple(self._item(row) for row in rows)

    async def list_suspected_non_vainglory(
        self, *, limit: int = 50, offset: int = 0
    ) -> ArchiveContentReviewPage:
        if limit < 1 or limit > 100:
            raise ValueError('limit must be between 1 and 100')
        if offset < 0:
            raise ValueError('offset must not be negative')
        where = "imported.content_classification='suspected_non_vainglory'"
        total = int(
            await self._database.scalar(
                'SELECT COUNT(*) FROM vainglory_archive_imports imported '
                'WHERE ' + where
            )
        )
        rows = await self._database.fetchall(
            'SELECT imported.id,imported.account_id,account.display_name '
            'AS account_name,imported.aid,imported.bvid,imported.title,'
            'imported.published_at,imported.classification_reason '
            'FROM vainglory_archive_imports imported '
            'JOIN bili_accounts account ON account.id=imported.account_id '
            'WHERE ' + where + ' ORDER BY imported.published_at DESC,imported.id DESC '
            'LIMIT ? OFFSET ?',
            (limit, offset),
        )
        return ArchiveContentReviewPage(
            total=total,
            items=tuple(
                ArchiveContentReview(
                    id=int(row['id']),
                    account_id=int(row['account_id']),
                    account_name=str(row['account_name']),
                    aid=int(row['aid']),
                    bvid=str(row['bvid']),
                    title=str(row['title']),
                    published_at=(
                        None
                        if row['published_at'] is None
                        else int(row['published_at'])
                    ),
                    reason=str(row['classification_reason'] or '未发现虚荣对局结算'),
                )
                for row in rows
            ),
        )

    async def update_control(
        self,
        account_id: int,
        *,
        paused: Optional[bool] = None,
        daily_limit: Optional[int] = None,
    ) -> ArchiveSync:
        if daily_limit is not None and not 1 <= int(daily_limit) <= 500:
            raise ValueError('每日处理上限必须在 1 到 500 之间')
        values: List[str] = []
        parameters: List[Any] = []
        if paused is not None:
            values.append('operator_paused=?')
            parameters.append(1 if paused else 0)
        if daily_limit is not None:
            values.append('daily_limit=?')
            parameters.append(int(daily_limit))
        if not values:
            return await self.status(account_id)
        values.append('updated_at=?')
        parameters.append(self._now())
        parameters.append(int(account_id))
        changed = await self._database.execute(
            'UPDATE vainglory_archive_syncs SET {} WHERE account_id=?'.format(
                ','.join(values)
            ),
            tuple(parameters),
        )
        if changed != 1:
            raise ArchiveBackfillNotFound('该账号还没有历史回填任务')
        if paused is False or daily_limit is not None:
            self._wake.set()
        return await self.status(account_id)

    async def run_once(self) -> bool:
        reconciled = await self._reconcile()
        now = self._now()
        sync = None
        if now >= self._next_discovery_at:
            sync = await self._database.fetchone(
                'SELECT account_id FROM vainglory_archive_syncs '
                "WHERE state IN ('discovering','running') AND operator_paused=0 "
                'AND discovery_complete=0 ORDER BY requested_at,account_id LIMIT 1'
            )
        if sync is not None:
            self._next_discovery_at = now + self.DISCOVERY_INTERVAL_SECONDS
            await self._discover(int(sync['account_id']))
            return True
        import_row = await self._claim_import()
        if import_row is not None:
            if int(import_row['page_count']) > 0:
                await self._retry_parts(import_row)
            else:
                await self._materialize(import_row)
            return True
        part = await self._database.fetchone(
            'SELECT archive.recording_part_id '
            'FROM vainglory_archive_parts archive '
            'JOIN vainglory_archive_imports imported '
            'ON imported.id=archive.import_id '
            'JOIN vainglory_archive_syncs sync '
            'ON sync.account_id=imported.account_id '
            "WHERE archive.state='queued' "
            "AND imported.state IN ('downloading','analyzing') "
            'AND sync.operator_paused=0 '
            'ORDER BY COALESCE(imported.recording_started_at,'
            'imported.published_at,imported.created_at) DESC,'
            'archive.import_id,archive.page LIMIT 1'
        )
        if part is not None:
            await self._queue_download(int(part['recording_part_id']))
            return True
        return reconciled

    async def _discover(self, account_id: int) -> None:
        now = self._now()
        account = await self._database.fetchone(
            'SELECT state,credential_version FROM bili_accounts WHERE id=?',
            (int(account_id),),
        )
        if account is None or str(account['state']) != 'active':
            await self._fail_sync(account_id, 'B 站账号当前不可用')
            return
        await self._database.execute(
            "UPDATE vainglory_archive_syncs SET started_at=COALESCE(started_at,?),"
            'updated_at=? WHERE account_id=?',
            (now, now, int(account_id)),
        )
        try:
            bundle = await self._bundle_loader(account_id)
            sync = await self._database.fetchone(
                'SELECT next_page,last_page_identity FROM vainglory_archive_syncs '
                'WHERE account_id=?',
                (int(account_id),),
            )
            if sync is None:
                return
            page_number = int(sync['next_page'])
            entries: Tuple[Mapping[str, Any], ...]
            if page_number > self.MAX_PAGES:
                entries = ()
            else:
                entries = await self._archive_reader.list_page(
                    bundle,
                    account_id=account_id,
                    credential_version=int(account['credential_version']),
                    status='is_pubing,pubed,not_pubed',
                    page_number=page_number,
                    page_size=self.PAGE_SIZE,
                )
            archives = tuple(
                archive
                for archive in (self._parse_archive_entry(entry) for entry in entries)
                if archive is not None and not is_excluded_title(archive.title)
            )
            page_identity = ','.join(archive.bvid for archive in archives)
            repeated = (
                bool(page_identity) and page_identity == sync['last_page_identity']
            )
            discovery_complete = (
                page_number >= self.MAX_PAGES
                or len(entries) < self.PAGE_SIZE
                or repeated
            )
        except BaseException as error:
            if isinstance(error, (asyncio.CancelledError, KeyboardInterrupt)):
                raise
            await self._fail_sync(
                account_id, '{}: {}'.format(type(error).__name__, error)
            )
            return

        def persist(connection: sqlite3.Connection) -> None:
            for archive in archives:
                recording_started_at = resolve_recording_started_at(
                    archive.title, published_at=archive.published_at, fallback=now
                )
                existing = connection.execute(
                    'SELECT id FROM vainglory_archive_imports '
                    'WHERE account_id=? AND bvid=?',
                    (account_id, archive.bvid),
                ).fetchone()
                if existing is not None:
                    connection.execute(
                        'UPDATE vainglory_archive_imports '
                        'SET aid=?,title=?,published_at=?,recording_started_at=?,'
                        'updated_at=? WHERE id=?',
                        (
                            archive.aid,
                            archive.title,
                            archive.published_at,
                            recording_started_at,
                            now,
                            int(existing['id']),
                        ),
                    )
                    continue
                uploaded = connection.execute(
                    'SELECT job.session_id,COUNT(part.id) AS page_count '
                    'FROM upload_jobs job LEFT JOIN upload_parts part '
                    'ON part.job_id=job.id '
                    'WHERE job.account_id=? AND job.bvid=? '
                    'GROUP BY job.session_id',
                    (account_id, archive.bvid),
                ).fetchone()
                connection.execute(
                    'INSERT INTO vainglory_archive_imports('
                    'account_id,aid,bvid,title,published_at,recording_started_at,'
                    'session_id,state,'
                    'progress,page_count,completed_page_count,error,created_at,'
                    'updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    (
                        account_id,
                        archive.aid,
                        archive.bvid,
                        archive.title,
                        archive.published_at,
                        recording_started_at,
                        (None if uploaded is None else int(uploaded['session_id'])),
                        'queued',
                        0,
                        0,
                        0,
                        None,
                        now,
                        now,
                    ),
                )
            counts = connection.execute(
                'SELECT COUNT(*) AS total,'
                "SUM(CASE WHEN state IN ('ready','skipped') "
                "OR (state='failed' AND retryable=0) "
                'THEN 1 ELSE 0 END) AS completed '
                'FROM vainglory_archive_imports WHERE account_id=?',
                (account_id,),
            ).fetchone()
            assert counts is not None
            total = int(counts['total'])
            completed = int(counts['completed'] or 0)
            state = 'ready' if discovery_complete and completed == total else 'running'
            progress = 1.0 if total == 0 else float(completed) / float(total)
            connection.execute(
                'UPDATE vainglory_archive_syncs SET state=?,progress=?,'
                'discovered_count=?,completed_count=?,error=NULL,'
                'completed_at=?,updated_at=?,next_page=?,discovery_complete=?,'
                'last_page_identity=? WHERE account_id=?',
                (
                    state,
                    progress,
                    total,
                    completed,
                    now if state == 'ready' else None,
                    now,
                    page_number + 1,
                    1 if discovery_complete else 0,
                    page_identity or None,
                    account_id,
                ),
            )

        await self._database.write(persist)

    async def _claim_import(self) -> Optional[sqlite3.Row]:
        now = self._now()
        quota_day = self._quota_day(now)
        season_start = current_season_started_at(now)

        def claim(connection: sqlite3.Connection) -> Optional[sqlite3.Row]:
            row = connection.execute(
                'SELECT imported.*,imported.quota_day AS import_quota_day,'
                'account.credential_version,sync.quota_day AS sync_quota_day,'
                'sync.daily_used,sync.daily_limit '
                'FROM vainglory_archive_imports imported '
                'JOIN bili_accounts account ON account.id=imported.account_id '
                'JOIN vainglory_archive_syncs sync '
                'ON sync.account_id=imported.account_id '
                "WHERE (imported.state='queued' OR ("
                "imported.state='failed' AND imported.retryable=1 "
                'AND imported.next_retry_at<=?)) '
                "AND account.state='active' "
                "AND sync.state IN ('discovering','running') "
                'AND sync.operator_paused=0 '
                'AND (sync.retry_after_at IS NULL OR sync.retry_after_at<=?) AND ('
                'imported.quota_day=? OR sync.quota_day IS NULL '
                'OR sync.quota_day<>? OR sync.daily_used<sync.daily_limit) '
                'ORDER BY CASE WHEN COALESCE(imported.recording_started_at,'
                'imported.published_at,imported.created_at)>=? THEN 0 ELSE 1 END,'
                "CASE imported.state WHEN 'failed' THEN 0 ELSE 1 END,"
                'COALESCE(imported.next_retry_at,0),'
                'COALESCE(imported.recording_started_at,imported.published_at,'
                'imported.created_at) DESC,'
                'imported.id LIMIT 1',
                (now, now, quota_day, quota_day, season_start),
            ).fetchone()
            if row is None:
                return None
            sync_quota_day = (
                None if row['sync_quota_day'] is None else str(row['sync_quota_day'])
            )
            import_quota_day = (
                None
                if row['import_quota_day'] is None
                else str(row['import_quota_day'])
            )
            daily_used = int(row['daily_used']) if sync_quota_day == quota_day else 0
            if import_quota_day != quota_day and daily_used >= int(row['daily_limit']):
                return None
            changed = connection.execute(
                "UPDATE vainglory_archive_imports SET state='downloading',"
                'progress=CASE WHEN page_count=0 THEN 0 ELSE progress END,'
                'error=NULL,retryable=0,next_retry_at=NULL,'
                'attempt_count=attempt_count+1,quota_day=?,updated_at=? '
                "WHERE id=? AND (state='queued' OR (state='failed' "
                'AND retryable=1 AND next_retry_at<=?))',
                (quota_day, now, int(row['id']), now),
            )
            if changed.rowcount != 1:
                return None
            connection.execute(
                'UPDATE vainglory_archive_syncs SET quota_day=?,daily_used=? '
                'WHERE account_id=?',
                (
                    quota_day,
                    daily_used + (1 if import_quota_day != quota_day else 0),
                    int(row['account_id']),
                ),
            )
            return row

        return await self._database.write(claim)

    async def _materialize(self, imported: sqlite3.Row) -> None:
        import_id = int(imported['id'])
        try:
            bundle = await self._bundle_loader(int(imported['account_id']))
            detail = await self._archive_reader.viewer_detail(
                bundle,
                account_id=int(imported['account_id']),
                credential_version=int(imported['credential_version']),
                bvid=str(imported['bvid']),
            )
            await self._database.execute(
                'UPDATE vainglory_archive_syncs SET retry_after_at=NULL '
                'WHERE account_id=?',
                (int(imported['account_id']),),
            )
            detail_title, description = self._detail_metadata(
                detail, fallback_title=str(imported['title'])
            )
            if is_excluded_title(str(imported['title']), detail_title):
                await self._delete_import(import_id)
                return
            pages = tuple(
                page
                for page in self._parse_detail(detail)
                if page.duration_seconds is None or page.duration_seconds >= 600
            )
            if not pages:
                await self._skip_import(import_id)
                return
            await self._persist_pages(
                imported, pages, detail_title=detail_title, description=description
            )
        except BaseException as error:
            if isinstance(error, (asyncio.CancelledError, KeyboardInterrupt)):
                raise
            await self._fail_import(
                import_id,
                self._error_text(error),
                retry_after_seconds=(
                    max(
                        self.METADATA_COOLDOWN_SECONDS,
                        int(error.retry_after_seconds or 0),
                    )
                    if isinstance(error, BiliApiError)
                    else None
                ),
                pause_account=isinstance(error, BiliApiError),
            )

    async def _persist_pages(
        self,
        imported: sqlite3.Row,
        pages: Tuple[_ArchivePage, ...],
        *,
        detail_title: str,
        description: str,
    ) -> None:
        now = self._now()

        def persist(connection: sqlite3.Connection) -> None:
            import_id = int(imported['id'])
            session_id = imported['session_id']
            started_at = resolve_recording_started_at(
                detail_title,
                published_at=(
                    None
                    if imported['published_at'] is None
                    else int(imported['published_at'])
                ),
                fallback=now,
            )
            connection.execute(
                'UPDATE vainglory_archive_imports SET recording_started_at=? '
                'WHERE id=?',
                (started_at, import_id),
            )
            duration = sum(page.duration_seconds or 0 for page in pages)
            if session_id is None:
                account = connection.execute(
                    'SELECT uid,display_name FROM bili_accounts WHERE id=?',
                    (int(imported['account_id']),),
                ).fetchone()
                if account is None:
                    raise ArchiveBackfillNotFound('B 站账号不存在')
                room_id, anchor_uid, anchor_name = self._infer_anchor(
                    connection,
                    detail_title,
                    description,
                    excluded_anchor_uid=int(account['uid']),
                    excluded_anchor_name=str(account['display_name']),
                )
                cursor = connection.execute(
                    'INSERT INTO recording_sessions('
                    'room_id,broadcast_session_key,live_start_time,state,'
                    'started_at,ended_at,title,anchor_uid,anchor_name,'
                    'live_end_time,upload_intent,source_kind,upload_decision,'
                    'upload_resolution_state,upload_resolved_at) '
                    "VALUES(?,?,?,'closed',?,?,?,?,?,?,'skip','live','skip',"
                    "'not_requested',?)",
                    (
                        room_id,
                        'bili-archive:{}:{}'.format(
                            int(imported['account_id']), str(imported['bvid'])
                        ),
                        started_at,
                        started_at,
                        started_at + duration,
                        detail_title,
                        anchor_uid,
                        anchor_name,
                        started_at + duration,
                        now,
                    ),
                )
                session_id = int(cursor.lastrowid)
                connection.execute(
                    'UPDATE vainglory_archive_imports SET session_id=? WHERE id=?',
                    (session_id, import_id),
                )
                connection.execute(
                    'INSERT INTO recording_runs('
                    "id,session_id,state,started_at,ended_at) "
                    "VALUES(? ,?,'finished',?,?)",
                    (
                        'bili-archive-run:{}:{}'.format(
                            int(imported['account_id']), str(imported['bvid'])
                        ),
                        session_id,
                        started_at,
                        started_at + duration,
                    ),
                )
            run = connection.execute(
                'SELECT id FROM recording_runs WHERE session_id=? '
                'ORDER BY started_at,id LIMIT 1',
                (int(session_id),),
            ).fetchone()
            if run is None:
                raise ArchiveBackfillUnavailable('历史稿件的录制批次不存在')
            elapsed = 0
            for page in pages:
                existing = connection.execute(
                    'SELECT recording_part_id FROM vainglory_archive_parts '
                    'WHERE import_id=? AND page=?',
                    (import_id, page.page),
                ).fetchone()
                if existing is not None:
                    elapsed += page.duration_seconds or 0
                    continue
                part_started_at = started_at + elapsed
                part_ended_at = part_started_at + (page.duration_seconds or 0)
                recorded = connection.execute(
                    'SELECT part.id,part.final_path,part.artifact_state,'
                    'part.video_deleted_at,part.file_size_bytes,'
                    'analysis.state AS analysis_state '
                    'FROM recording_parts part '
                    'LEFT JOIN upload_jobs job ON job.session_id=part.session_id '
                    'AND job.account_id=? AND job.bvid=? '
                    'LEFT JOIN upload_parts uploaded ON uploaded.job_id=job.id '
                    'AND uploaded.part_index=part.part_index '
                    'LEFT JOIN vainglory_part_jobs analysis '
                    'ON analysis.part_id=part.id '
                    'WHERE part.session_id=? '
                    'AND (uploaded.cid=? OR part.part_index=?) '
                    'ORDER BY CASE WHEN uploaded.cid=? THEN 0 ELSE 1 END,'
                    'part.id LIMIT 1',
                    (
                        int(imported['account_id']),
                        str(imported['bvid']),
                        int(session_id),
                        page.cid,
                        page.page,
                        page.cid,
                    ),
                ).fetchone()
                if recorded is None:
                    cursor = connection.execute(
                        'INSERT INTO recording_parts('
                        'session_id,run_id,part_index,source_path,final_path,'
                        'xml_path,record_start_time,artifact_state,xml_completed,'
                        'record_end_time,record_duration_seconds,file_size_bytes,'
                        'video_deleted_at,video_delete_reason,created_at,updated_at,'
                        'media_index_state,upload_excluded_reason) '
                        "VALUES(?,?,?,?,NULL,NULL,?,'missing',1,?,?,NULL,?,"
                        "'历史稿件临时视频已清理',?,?,'not_required',"
                        "'历史稿件只用于对局分析')",
                        (
                            int(session_id),
                            str(run['id']),
                            page.page,
                            'bili://{}/p{}'.format(str(imported['bvid']), page.page),
                            part_started_at,
                            part_ended_at,
                            page.duration_seconds,
                            now,
                            now,
                            now,
                        ),
                    )
                    part_id = int(cursor.lastrowid)
                    origin = 'archive'
                    original_final_path = None
                    original_artifact_state = 'missing'
                    original_video_deleted_at = now
                    original_file_size_bytes = None
                    analysis_state = None
                else:
                    part_id = int(recorded['id'])
                    origin = 'upload'
                    original_final_path = recorded['final_path']
                    original_artifact_state = str(recorded['artifact_state'])
                    original_video_deleted_at = recorded['video_deleted_at']
                    original_file_size_bytes = recorded['file_size_bytes']
                    analysis_state = recorded['analysis_state']
                    if analysis_state == 'failed':
                        connection.execute(
                            'DELETE FROM vainglory_ocr_jobs WHERE part_id=?', (part_id,)
                        )
                        connection.execute(
                            "DELETE FROM vainglory_part_jobs WHERE part_id=? "
                            "AND state='failed'",
                            (part_id,),
                        )
                        analysis_state = None
                archive_state = 'ready' if analysis_state == 'ready' else 'queued'
                archive_progress = 1 if archive_state == 'ready' else 0
                connection.execute(
                    'INSERT INTO vainglory_archive_parts('
                    'import_id,page,cid,title,duration_seconds,'
                    'recording_part_id,state,progress,error,created_at,updated_at) '
                    'VALUES(?,?,?,?,?,?,?,?,NULL,?,?)',
                    (
                        import_id,
                        page.page,
                        page.cid,
                        page.title,
                        page.duration_seconds,
                        part_id,
                        archive_state,
                        archive_progress,
                        now,
                        now,
                    ),
                )
                connection.execute(
                    'INSERT OR IGNORE INTO vainglory_video_sources('
                    'part_id,account_id,bvid,cid,page,origin,state,retention_kind,'
                    'progress,downloaded_bytes,total_bytes,cache_path,'
                    'original_final_path,original_artifact_state,'
                    'original_video_deleted_at,original_file_size_bytes,'
                    'cached_at,expires_at,error,created_at,updated_at) '
                    "VALUES(?,?,?,?,?,?,'missing','analysis',0,0,NULL,NULL,"
                    '?,?,?,?,NULL,NULL,NULL,?,?)',
                    (
                        part_id,
                        int(imported['account_id']),
                        str(imported['bvid']),
                        page.cid,
                        page.page,
                        origin,
                        original_final_path,
                        original_artifact_state,
                        original_video_deleted_at,
                        original_file_size_bytes,
                        now,
                        now,
                    ),
                )
                elapsed += page.duration_seconds or 0
            completed = int(
                connection.execute(
                    "SELECT COUNT(*) FROM vainglory_archive_parts "
                    "WHERE import_id=? AND state='ready'",
                    (import_id,),
                ).fetchone()[0]
            )
            connection.execute(
                "UPDATE vainglory_archive_imports SET state='analyzing',"
                'progress=?,page_count=?,completed_page_count=?,error=NULL,'
                "content_classification='unknown',classification_reason=NULL,"
                'retryable=0,next_retry_at=NULL,updated_at=? WHERE id=?',
                (
                    float(completed) / float(len(pages)),
                    len(pages),
                    completed,
                    now,
                    import_id,
                ),
            )

        await self._database.write(persist)

    async def _retry_parts(self, imported: sqlite3.Row) -> None:
        now = self._now()

        def retry(connection: sqlite3.Connection) -> None:
            import_id = int(imported['id'])
            failed = connection.execute(
                "SELECT recording_part_id FROM vainglory_archive_parts "
                "WHERE import_id=? AND state='failed'",
                (import_id,),
            ).fetchall()
            part_ids = tuple(
                int(row['recording_part_id'])
                for row in failed
                if row['recording_part_id'] is not None
            )
            for part_id in part_ids:
                connection.execute(
                    'DELETE FROM vainglory_ocr_jobs WHERE part_id=?', (part_id,)
                )
                connection.execute(
                    "DELETE FROM vainglory_part_jobs WHERE part_id=? "
                    "AND state='failed'",
                    (part_id,),
                )
            connection.execute(
                "UPDATE vainglory_archive_parts SET state='queued',progress=0,"
                'error=NULL,updated_at=? '
                "WHERE import_id=? AND state='failed'",
                (now, import_id),
            )
            completed = int(
                connection.execute(
                    "SELECT COUNT(*) FROM vainglory_archive_parts "
                    "WHERE import_id=? AND state='ready'",
                    (import_id,),
                ).fetchone()[0]
            )
            page_count = max(1, int(imported['page_count']))
            connection.execute(
                "UPDATE vainglory_archive_imports SET state='analyzing',"
                'progress=?,completed_page_count=?,error=NULL,retryable=0,'
                "next_retry_at=NULL,content_classification='unknown',"
                'classification_reason=NULL,updated_at=? WHERE id=?',
                (float(completed) / float(page_count), completed, now, import_id),
            )

        await self._database.write(retry)

    async def _queue_download(self, part_id: int) -> None:
        now = self._now()
        try:
            status = await self._remote_media_cache.request(part_id)
            if status.state in ('ready', 'local'):
                state = 'analyzing'
                progress = 0.5
                error = None
            elif status.state == 'failed':
                state = 'failed'
                progress = 1
                error = status.error or '历史稿件下载失败'
            else:
                state = 'downloading'
                progress = max(0.0, min(0.49, status.progress * 0.5))
                error = None
        except BaseException as raised:
            if isinstance(raised, (asyncio.CancelledError, KeyboardInterrupt)):
                raise
            state = 'failed'
            progress = 1
            error = '{}: {}'.format(type(raised).__name__, raised)[:500]
        await self._database.execute(
            'UPDATE vainglory_archive_parts SET state=?,progress=?,error=?,'
            'updated_at=? WHERE recording_part_id=?',
            (state, progress, error, now, int(part_id)),
        )

    async def _reconcile(self) -> bool:
        now = self._now()

        def reconcile(connection: sqlite3.Connection) -> bool:
            changed = False
            untimed = connection.execute(
                'SELECT id,title,published_at FROM vainglory_archive_imports '
                'WHERE recording_started_at IS NULL LIMIT 500'
            ).fetchall()
            for imported in untimed:
                recording_started_at = resolve_recording_started_at(
                    str(imported['title']),
                    published_at=(
                        None
                        if imported['published_at'] is None
                        else int(imported['published_at'])
                    ),
                    fallback=now,
                )
                connection.execute(
                    'UPDATE vainglory_archive_imports '
                    'SET recording_started_at=? WHERE id=?',
                    (recording_started_at, int(imported['id'])),
                )
                changed = True
            rows = connection.execute(
                'SELECT archive.id,archive.state,archive.progress,archive.error,'
                'source.state AS source_state,source.progress AS source_progress,'
                'source.error AS source_error,analysis.state AS analysis_state,'
                'analysis.progress AS analysis_progress,'
                'analysis.error AS analysis_error '
                'FROM vainglory_archive_parts archive '
                'JOIN vainglory_video_sources source '
                'ON source.part_id=archive.recording_part_id '
                'LEFT JOIN vainglory_part_jobs analysis '
                'ON analysis.part_id=archive.recording_part_id '
                "WHERE archive.state IN ('downloading','analyzing') OR ("
                "archive.state='queued' AND (analysis.state IS NOT NULL "
                "OR source.state IN ('pending','downloading','ready')))"
            ).fetchall()
            for row in rows:
                state, progress, error = self._derived_part_state(row)
                if (
                    state == str(row['state'])
                    and abs(progress - float(row['progress'])) < 0.001
                    and error == row['error']
                ):
                    continue
                connection.execute(
                    'UPDATE vainglory_archive_parts SET state=?,progress=?,'
                    'error=?,updated_at=? WHERE id=?',
                    (state, progress, error, now, int(row['id'])),
                )
                changed = True
            imports = connection.execute(
                'SELECT id,session_id,state,attempt_count '
                'FROM vainglory_archive_imports '
                "WHERE state IN ('downloading','analyzing')"
            ).fetchall()
            for imported in imports:
                parts = connection.execute(
                    'SELECT state,progress,error FROM vainglory_archive_parts '
                    'WHERE import_id=? ORDER BY page',
                    (int(imported['id']),),
                ).fetchall()
                if not parts:
                    continue
                terminal = all(
                    str(part['state']) in ('ready', 'failed') for part in parts
                )
                failures = [
                    str(part['error'] or '处理失败')
                    for part in parts
                    if str(part['state']) == 'failed'
                ]
                state = ('failed' if failures else 'ready') if terminal else 'analyzing'
                progress = sum(float(part['progress']) for part in parts) / len(parts)
                error = '; '.join(failures)[:500] if failures and terminal else None
                completed = sum(str(part['state']) == 'ready' for part in parts)
                classification = 'unknown'
                classification_reason: Optional[str] = None
                if state == 'ready':
                    match_count = int(
                        connection.execute(
                            'SELECT COUNT(*) FROM vainglory_matches match '
                            'JOIN vainglory_archive_parts archive '
                            'ON archive.recording_part_id=match.result_part_id '
                            'WHERE archive.import_id=?',
                            (int(imported['id']),),
                        ).fetchone()[0]
                    )
                    if match_count > 0:
                        classification = 'vainglory'
                        classification_reason = '已识别到虚荣对局结算'
                    else:
                        classification = 'suspected_non_vainglory'
                        classification_reason = '所有分P分析完成，但未发现虚荣对局结算'
                if state != str(imported['state']) or terminal or completed > 0:
                    retryable = 1 if state == 'failed' else 0
                    next_retry_at = (
                        now + self._retry_delay_seconds(int(imported['attempt_count']))
                        if retryable
                        else None
                    )
                    connection.execute(
                        'UPDATE vainglory_archive_imports SET state=?,progress=?,'
                        'completed_page_count=?,error=?,content_classification=?,'
                        'classification_reason=?,retryable=?,next_retry_at=?,'
                        'updated_at=? WHERE id=?',
                        (
                            state,
                            progress,
                            completed,
                            error,
                            classification,
                            classification_reason,
                            retryable,
                            next_retry_at,
                            now,
                            int(imported['id']),
                        ),
                    )
                    if terminal:
                        changed = True
                if imported['session_id'] is not None:
                    refresh_session_scan_job(
                        connection, int(imported['session_id']), now
                    )
            syncs = connection.execute(
                'SELECT account_id,state,discovered_count,completed_count,'
                'discovery_complete FROM vainglory_archive_syncs '
                "WHERE state='running'"
            ).fetchall()
            for sync in syncs:
                values = connection.execute(
                    'SELECT state,progress,retryable '
                    'FROM vainglory_archive_imports '
                    'WHERE account_id=?',
                    (int(sync['account_id']),),
                ).fetchall()
                total = len(values)
                completed = sum(
                    str(value['state']) in ('ready', 'skipped')
                    or (
                        str(value['state']) == 'failed' and not bool(value['retryable'])
                    )
                    for value in values
                )
                progress = (
                    1.0
                    if total == 0
                    else sum(
                        (
                            1.0
                            if str(value['state']) in ('ready', 'skipped')
                            or (
                                str(value['state']) == 'failed'
                                and not bool(value['retryable'])
                            )
                            else (
                                0.0
                                if str(value['state']) == 'failed'
                                else float(value['progress'])
                            )
                        )
                        for value in values
                    )
                    / total
                )
                state = (
                    'ready'
                    if bool(sync['discovery_complete']) and completed == total
                    else 'running'
                )
                if (
                    state != str(sync['state'])
                    or total != int(sync['discovered_count'])
                    or completed != int(sync['completed_count'])
                ):
                    connection.execute(
                        'UPDATE vainglory_archive_syncs SET state=?,progress=?,'
                        'discovered_count=?,completed_count=?,completed_at=?,'
                        'updated_at=? WHERE account_id=?',
                        (
                            state,
                            progress,
                            total,
                            completed,
                            now if state == 'ready' else None,
                            now,
                            int(sync['account_id']),
                        ),
                    )
                    if state == 'ready':
                        changed = True
            return changed

        return await self._database.write(reconcile)

    @staticmethod
    def _derived_part_state(row: sqlite3.Row) -> Tuple[str, float, Optional[str]]:
        analysis_state = row['analysis_state']
        if analysis_state == 'ready':
            return 'ready', 1, None
        if analysis_state == 'failed':
            return 'failed', 1, str(row['analysis_error'] or '对局分析失败')
        source_state = str(row['source_state'])
        if source_state == 'failed':
            return 'failed', 1, str(row['source_error'] or '历史稿件下载失败')
        if source_state in ('pending', 'downloading'):
            return (
                'downloading',
                max(0.0, min(0.49, float(row['source_progress']) * 0.5)),
                None,
            )
        if source_state == 'ready' or analysis_state in ('pending', 'analyzing'):
            analysis_progress = float(row['analysis_progress'] or 0)
            return (
                'analyzing',
                0.5 + max(0.0, min(0.49, analysis_progress * 0.5)),
                None,
            )
        return str(row['state']), float(row['progress']), row['error']

    async def _fail_sync(self, account_id: int, error: str) -> None:
        now = self._now()
        await self._database.execute(
            "UPDATE vainglory_archive_syncs SET state='failed',progress=0,"
            'error=?,completed_at=?,updated_at=? WHERE account_id=?',
            (error.strip()[:500] or '历史回填失败', now, now, int(account_id)),
        )

    async def _fail_import(
        self,
        import_id: int,
        error: str,
        *,
        retry_after_seconds: Optional[int] = None,
        pause_account: bool = False,
    ) -> None:
        normalized_error = error.strip()[:500] or '历史稿件处理失败'
        now = self._now()

        def fail(connection: sqlite3.Connection) -> None:
            imported = connection.execute(
                'SELECT account_id,attempt_count FROM vainglory_archive_imports '
                'WHERE id=?',
                (int(import_id),),
            ).fetchone()
            if imported is None:
                return
            delay = self._retry_delay_seconds(int(imported['attempt_count']))
            if retry_after_seconds is not None:
                delay = max(delay, max(1, int(retry_after_seconds)))
            retry_at = now + delay
            connection.execute(
                "UPDATE vainglory_archive_imports SET state='failed',progress=0,"
                "content_classification='unknown',classification_reason=?,"
                'error=?,retryable=1,next_retry_at=?,updated_at=? WHERE id=?',
                (
                    '处理失败，等待自动重试',
                    normalized_error,
                    retry_at,
                    now,
                    int(import_id),
                ),
            )
            if pause_account:
                connection.execute(
                    'UPDATE vainglory_archive_syncs SET retry_after_at=CASE '
                    'WHEN retry_after_at IS NULL OR retry_after_at<? THEN ? '
                    'ELSE retry_after_at END,updated_at=? WHERE account_id=?',
                    (retry_at, retry_at, now, int(imported['account_id'])),
                )

        await self._database.write(fail)

    async def _skip_import(self, import_id: int) -> None:
        await self._database.execute(
            "UPDATE vainglory_archive_imports SET state='skipped',progress=1,"
            "content_classification='unknown',"
            "classification_reason='稿件短于10分钟，未进行内容分析',"
            'page_count=0,completed_page_count=0,error=NULL,retryable=0,'
            'next_retry_at=NULL,updated_at=? '
            'WHERE id=?',
            (self._now(), int(import_id)),
        )

    async def _delete_import(self, import_id: int) -> None:
        await self._database.execute(
            'DELETE FROM vainglory_archive_imports WHERE id=?', (int(import_id),)
        )

    @staticmethod
    def _infer_anchor(
        connection: sqlite3.Connection,
        title: str,
        description: str,
        *,
        excluded_anchor_uid: Optional[int] = None,
        excluded_anchor_name: str = '',
    ) -> Tuple[int, Optional[int], str]:
        return infer_recorded_anchor(
            connection,
            title,
            description,
            excluded_anchor_uids=(
                () if excluded_anchor_uid is None else (excluded_anchor_uid,)
            ),
            excluded_anchor_names=(excluded_anchor_name,),
        )

    @classmethod
    def _parse_archive_entry(cls, entry: Mapping[str, Any]) -> Optional[_Archive]:
        value = entry.get('Archive')
        if not isinstance(value, Mapping):
            value = entry.get('archive')
        if not isinstance(value, Mapping):
            value = entry
        aid = cls._positive_int(value.get('aid'))
        bvid = cls._text(value.get('bvid'))
        title = cls._text(value.get('title'))
        if (
            aid is None
            or bvid is None
            or not 10 <= len(bvid) <= 20
            or re.fullmatch('[0-9A-Za-z]+', bvid) is None
            or title is None
        ):
            return None
        published_at = next(
            (
                parsed
                for parsed in (
                    cls._positive_int(value.get('pubtime')),
                    cls._positive_int(value.get('ctime')),
                    cls._positive_int(value.get('created')),
                )
                if parsed is not None
            ),
            None,
        )
        return _Archive(aid, bvid, title[:200], published_at)

    @classmethod
    def _parse_detail(cls, detail: Mapping[str, Any]) -> Tuple[_ArchivePage, ...]:
        data = detail.get('data')
        if not isinstance(data, Mapping):
            return ()
        videos = data.get('videos')
        if not isinstance(videos, list):
            videos = data.get('pages')
        if not isinstance(videos, list):
            return ()
        pages: List[_ArchivePage] = []
        for index, value in enumerate(videos, 1):
            if not isinstance(value, Mapping):
                continue
            cid = cls._positive_int(value.get('cid'))
            if cid is None:
                continue
            title = (
                cls._text(value.get('title'))
                or cls._text(value.get('part'))
                or cls._text(value.get('filename'))
                or 'P{}'.format(index)
            )
            pages.append(
                _ArchivePage(
                    page=cls._positive_int(value.get('page')) or index,
                    cid=cid,
                    title=title[:200],
                    duration_seconds=cls._positive_int(value.get('duration')),
                )
            )
        return tuple(pages)

    @classmethod
    def _retry_delay_seconds(cls, attempt_count: int) -> int:
        exponent = max(0, min(8, int(attempt_count) - 1))
        return min(cls.RETRY_MAX_SECONDS, cls.RETRY_BASE_SECONDS * (2**exponent))

    @staticmethod
    def _error_text(error: BaseException) -> str:
        values = ['{}: {}'.format(type(error).__name__, error)]
        if isinstance(error, BiliApiError):
            if error.operation:
                values.append('operation={}'.format(error.operation))
            if error.public_message:
                values.append(str(error.public_message))
        return '; '.join(values)[:500]

    @classmethod
    def _detail_metadata(
        cls, detail: Mapping[str, Any], *, fallback_title: str
    ) -> Tuple[str, str]:
        data = detail.get('data')
        if not isinstance(data, Mapping):
            return fallback_title, ''
        archive = data.get('archive')
        if not isinstance(archive, Mapping):
            archive = data
        title = cls._text(archive.get('title')) or fallback_title
        description = next(
            (
                value
                for value in (
                    cls._text(archive.get('desc')),
                    cls._text(archive.get('description')),
                    cls._text(archive.get('dynamic')),
                )
                if value is not None
            ),
            '',
        )
        return title[:200], description

    async def _run(self) -> None:
        while True:
            processed = await self.run_once()
            if processed:
                continue
            self._wake.clear()
            try:
                await asyncio.wait_for(
                    self._wake.wait(), timeout=self._idle_poll_seconds
                )
            except asyncio.TimeoutError:
                pass

    def _now(self) -> int:
        return max(1, int(self._clock()))

    @staticmethod
    def _quota_day(now: int) -> str:
        return datetime.fromtimestamp(now, ZoneInfo('Asia/Shanghai')).date().isoformat()

    @staticmethod
    def _positive_int(value: Any) -> Optional[int]:
        if isinstance(value, bool):
            return None
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return None
        return parsed if parsed > 0 else None

    @staticmethod
    def _text(value: Any) -> Optional[str]:
        if not isinstance(value, str):
            return None
        normalized = value.strip()
        return normalized or None

    @classmethod
    def _item(cls, row: sqlite3.Row) -> ArchiveBackfillItem:
        comment_count = int(row['comment_count'])
        confirmed_comment_count = int(row['confirmed_comment_count'])
        description_done = row['chapter_state'] in ('confirmed', 'skipped') and row[
            'description_state'
        ] in ('confirmed', 'skipped_no_room')
        publication_progress = 0.0
        if row['publication_state'] is not None:
            publication_progress += 0.34 if description_done else 0.0
            publication_progress += (
                0.32
                if comment_count == 0 and description_done
                else 0.32 * confirmed_comment_count / max(1, comment_count)
            )
            publication_progress += 0.34 if row['pin_state'] == 'confirmed' else 0.0
        if row['publication_state'] == 'confirmed':
            publication_progress = 1.0
        error = next(
            (
                str(value)
                for value in (
                    row['error'],
                    row['source_error'],
                    row['analysis_error'],
                    row['publication_error'],
                    row['comment_error'],
                )
                if value is not None and str(value).strip()
            ),
            None,
        )
        return ArchiveBackfillItem(
            id=int(row['id']),
            account_id=int(row['account_id']),
            aid=int(row['aid']),
            bvid=str(row['bvid']),
            title=str(row['title']),
            published_at=(
                None if row['published_at'] is None else int(row['published_at'])
            ),
            state=str(row['state']),
            stage=cls._item_stage(row),
            progress=float(row['progress']),
            page_count=int(row['page_count']),
            completed_page_count=int(row['completed_page_count']),
            current_page=(
                None if row['current_page'] is None else int(row['current_page'])
            ),
            current_part_title=(
                None
                if row['current_part_title'] is None
                else str(row['current_part_title'])
            ),
            download_progress=float(row['download_progress']),
            downloaded_bytes=int(row['downloaded_bytes']),
            total_bytes=(
                None if row['total_bytes'] is None else int(row['total_bytes'])
            ),
            analysis_state=(
                None if row['analysis_state'] is None else str(row['analysis_state'])
            ),
            analysis_progress=float(row['analysis_progress']),
            match_count=int(row['match_count']),
            publication_state=(
                None
                if row['publication_state'] is None
                else str(row['publication_state'])
            ),
            description_state=(
                None
                if row['description_state'] is None
                else str(row['description_state'])
            ),
            comment_count=comment_count,
            confirmed_comment_count=confirmed_comment_count,
            pin_state=None if row['pin_state'] is None else str(row['pin_state']),
            publication_progress=publication_progress,
            error=error,
            updated_at=int(row['updated_at']),
        )

    @staticmethod
    def _item_stage(row: sqlite3.Row) -> str:
        state = str(row['state'])
        if state == 'failed' or row['publication_state'] == 'failed':
            return 'failed'
        if state == 'skipped':
            return 'managed_elsewhere'
        if state == 'queued':
            return 'queued'
        if state == 'downloading' and int(row['page_count']) == 0:
            return 'reading_metadata'
        if state == 'ready':
            if int(row['match_count']) == 0:
                return 'completed'
            if row['publication_state'] is None:
                return 'publication_pending'
            if row['chapter_state'] not in ('confirmed', 'skipped'):
                return 'publishing_description'
            if row['description_state'] not in ('confirmed', 'skipped_no_room'):
                return 'publishing_description'
            if int(row['confirmed_comment_count']) < int(row['comment_count']):
                return 'publishing_comments'
            if row['pin_state'] != 'confirmed':
                return 'pinning_comment'
            return 'completed'
        source_state = row['source_state']
        analysis_state = row['analysis_state']
        if source_state == 'failed' or analysis_state == 'failed':
            return 'failed'
        if source_state in ('pending', 'downloading'):
            return 'downloading'
        if source_state in (None, 'missing') or row['current_part_state'] == 'queued':
            return 'download_pending'
        if analysis_state in (None, 'pending'):
            return 'analysis_pending'
        if analysis_state == 'analyzing':
            progress = float(row['current_analysis_progress'] or 0)
            if progress < 0.45:
                return 'scanning_video'
            if progress < 0.7:
                return 'locating_results'
            return 'ocr_recognition'
        return 'analysis_pending'

    @staticmethod
    def _sync(row: sqlite3.Row) -> ArchiveSync:
        return ArchiveSync(
            account_id=int(row['account_id']),
            state=str(row['state']),
            progress=float(row['progress']),
            discovered_count=int(row['discovered_count']),
            completed_count=int(row['completed_count']),
            error=None if row['error'] is None else str(row['error']),
            requested_at=int(row['requested_at']),
            started_at=(None if row['started_at'] is None else int(row['started_at'])),
            completed_at=(
                None if row['completed_at'] is None else int(row['completed_at'])
            ),
            updated_at=int(row['updated_at']),
            operator_paused=bool(row['operator_paused']),
            daily_limit=int(row['daily_limit']),
            daily_used=int(row['daily_used']),
            quota_day=(None if row['quota_day'] is None else str(row['quota_day'])),
            next_page=int(row['next_page']),
            discovery_complete=bool(row['discovery_complete']),
        )
