from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import sqlite3
import tempfile
import time
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import (
    Any,
    AsyncIterator,
    Awaitable,
    Callable,
    List,
    Mapping,
    Optional,
    Protocol,
    Tuple,
)
from zoneinfo import ZoneInfo

from loguru import logger

from blrec.networking.manager import NetworkRouteManager, NetworkUnavailable
from blrec.vainglory.anchor_identity import infer_recorded_anchor

from .bili_download import YtDlpMediaDownloader
from .crypto import CredentialBundle
from .database import BiliUploadDatabase
from .policies import default_room_upload_policy
from .session_submission import encode_submission_settings

__all__ = (
    'ArchiveDetail',
    'ArchiveListing',
    'ArchiveMigrationItem',
    'ArchiveMigrationNotFound',
    'ArchiveMigrationService',
    'ArchiveMigrationStatus',
    'ArchiveMigrationUnavailable',
    'ArchivePage',
    'BiliPublicArchiveReader',
    'YtDlpSpaceArchiveCatalog',
)


_BVID_PATTERN = re.compile(r'BV[0-9A-Za-z]{8,18}')
_DEFAULT_DAILY_LIMIT = 60
_STATUS_SELECT = (
    'SELECT job.*,('
    'SELECT session.anchor_name '
    'FROM archive_migration_items item '
    'JOIN recording_sessions session ON session.id=item.session_id '
    "WHERE item.migration_id=job.id AND session.anchor_name<>'' "
    'ORDER BY item.id LIMIT 1'
    ') AS source_name '
    'FROM archive_migration_jobs job'
)


class ArchiveMigrationNotFound(ValueError):
    pass


class ArchiveMigrationUnavailable(ValueError):
    pass


@dataclass(frozen=True)
class ArchiveListing:
    bvid: str
    title: str
    published_at: Optional[int]


@dataclass(frozen=True)
class ArchivePage:
    page: int
    cid: int
    title: str
    duration_seconds: Optional[int]


@dataclass(frozen=True)
class ArchiveDetail:
    aid: int
    bvid: str
    owner_uid: int
    owner_name: str
    title: str
    description: str
    tags: Tuple[str, ...]
    tid: int
    cover_url: str
    published_at: Optional[int]
    pages: Tuple[ArchivePage, ...]


@dataclass(frozen=True)
class ArchiveMigrationStatus:
    id: int
    source_uid: int
    source_name: Optional[str]
    download_account_id: int
    target_account_id: int
    state: str
    progress: float
    discovered_count: int
    completed_count: int
    failed_count: int
    error: Optional[str]
    requested_at: int
    started_at: Optional[int]
    completed_at: Optional[int]
    updated_at: int
    operator_paused: bool = False
    daily_limit: int = _DEFAULT_DAILY_LIMIT
    daily_used: int = 0
    quota_day: Optional[str] = None


@dataclass(frozen=True)
class ArchiveMigrationItem:
    id: int
    migration_id: int
    bvid: str
    title: str
    published_at: Optional[int]
    state: str
    progress: float
    page_count: int
    downloaded_page_count: int
    attempt_count: int
    session_id: Optional[int]
    upload_job_id: Optional[int]
    upload_state: Optional[str]
    submit_state: Optional[str]
    comment_branch_state: Optional[str]
    danmaku_branch_state: Optional[str]
    analysis_state: Optional[str]
    target_bvid: Optional[str]
    error: Optional[str]
    updated_at: int


class ArchiveCatalog(Protocol):
    def iter_archives(
        self, bundle: CredentialBundle, *, source_uid: int
    ) -> AsyncIterator[ArchiveListing]:
        raise NotImplementedError


class ArchiveDetailReader(Protocol):
    async def detail(self, bundle: CredentialBundle, *, bvid: str) -> ArchiveDetail:
        raise NotImplementedError


class ArchiveTaskCreator(Protocol):
    async def create_archive_migration_job(
        self,
        session_id: int,
        *,
        description: str,
        tags: str,
        part_titles: Tuple[str, ...],
    ) -> int:
        raise NotImplementedError


class YtDlpSpaceArchiveCatalog:
    _MAX_ERROR_BYTES = 32 * 1024

    def __init__(
        self,
        *,
        network_manager: Optional[NetworkRouteManager] = None,
        executable: str = 'yt-dlp',
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._network_manager = network_manager
        self._executable = executable
        self._clock = clock

    async def iter_archives(
        self, bundle: CredentialBundle, *, source_uid: int
    ) -> AsyncIterator[ArchiveListing]:
        if source_uid <= 0:
            raise ArchiveMigrationUnavailable('源账号 UID 无效')
        temporary_root = Path(tempfile.mkdtemp(prefix='blrec-bili-space-'))
        cookie_path = temporary_root / 'cookies.txt'
        YtDlpMediaDownloader.write_cookie_file(
            bundle.cookies, cookie_path, now=max(1, int(self._clock()))
        )
        selection = None
        if self._network_manager is not None:
            try:
                selection = self._network_manager.select(
                    'archive_download',
                    anonymous=False,
                    affinity_key='archive-space:{}'.format(source_uid),
                )
            except NetworkUnavailable:
                shutil.rmtree(temporary_root, ignore_errors=True)
                raise ArchiveMigrationUnavailable(
                    '历史稿件读取网络当前不可用'
                ) from None
        command = self.build_command(
            executable=self._executable,
            cookie_path=cookie_path,
            source_uid=source_uid,
            source_address=(None if selection is None else selection.source_address),
        )
        process: Optional[asyncio.subprocess.Process] = None
        stderr_task: Optional[asyncio.Task[str]] = None
        failed = False
        try:
            try:
                process = await asyncio.create_subprocess_exec(
                    *command,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
            except OSError:
                raise ArchiveMigrationUnavailable(
                    'NAS 容器中没有可用的 yt-dlp'
                ) from None
            assert process.stdout is not None
            assert process.stderr is not None
            stderr_task = asyncio.create_task(self._read_error(process.stderr))
            while True:
                raw = await process.stdout.readline()
                if not raw:
                    break
                entry = self.parse_entry(raw.decode('utf8', errors='replace'))
                if entry is not None:
                    yield entry
            await process.wait()
            error = await stderr_task
            stderr_task = None
            if process.returncode != 0:
                failed = True
                raise ArchiveMigrationUnavailable(
                    '读取源账号稿件失败{}'.format('：' + error if error else '')
                )
        except (asyncio.CancelledError, KeyboardInterrupt):
            failed = True
            raise
        except BaseException:
            failed = True
            raise
        finally:
            if process is not None and process.returncode is None:
                failed = True
                try:
                    process.terminate()
                except ProcessLookupError:
                    pass
                try:
                    await asyncio.wait_for(process.wait(), timeout=5)
                except asyncio.TimeoutError:
                    process.kill()
                    await process.wait()
            if stderr_task is not None:
                await asyncio.gather(stderr_task, return_exceptions=True)
            if self._network_manager is not None and selection is not None:
                if failed:
                    self._network_manager.report_failure(
                        'archive_download', selection.interface_name
                    )
                else:
                    self._network_manager.report_success(
                        'archive_download', selection.interface_name
                    )
            shutil.rmtree(temporary_root, ignore_errors=True)

    @staticmethod
    def build_command(
        *,
        executable: str,
        cookie_path: Path,
        source_uid: int,
        source_address: Optional[str],
    ) -> Tuple[str, ...]:
        if source_uid <= 0:
            raise ArchiveMigrationUnavailable('源账号 UID 无效')
        command: List[str] = [
            executable,
            '--no-config',
            '--cookies',
            str(cookie_path),
            '--flat-playlist',
            '--lazy-playlist',
            '--dump-json',
            '--no-warnings',
        ]
        if source_address:
            command.extend(('--source-address', source_address))
        command.append('https://space.bilibili.com/{}/video'.format(source_uid))
        return tuple(command)

    @staticmethod
    def parse_entry(line: str) -> Optional[ArchiveListing]:
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            return None
        if not isinstance(value, Mapping):
            return None
        bvid = next(
            (
                match.group(0)
                for candidate in (
                    value.get('bvid'),
                    value.get('id'),
                    value.get('url'),
                    value.get('webpage_url'),
                )
                if isinstance(candidate, str)
                for match in [_BVID_PATTERN.search(candidate)]
                if match is not None
            ),
            None,
        )
        if bvid is None:
            return None
        raw_title = value.get('title')
        title = raw_title.strip() if isinstance(raw_title, str) else bvid
        if not title:
            title = bvid
        published_at = next(
            (
                parsed
                for parsed in (
                    _positive_int(value.get('timestamp')),
                    _positive_int(value.get('release_timestamp')),
                )
                if parsed is not None
            ),
            None,
        )
        return ArchiveListing(bvid=bvid, title=title[:200], published_at=published_at)

    @classmethod
    async def _read_error(cls, stream: asyncio.StreamReader) -> str:
        kept = bytearray()
        while True:
            chunk = await stream.read(4096)
            if not chunk:
                break
            kept.extend(chunk)
            if len(kept) > cls._MAX_ERROR_BYTES:
                del kept[: len(kept) - cls._MAX_ERROR_BYTES]
        return bytes(kept).decode('utf8', errors='replace').strip()[:500]


class BiliPublicArchiveReader:
    def __init__(self, protocol: Any) -> None:
        self._protocol = protocol

    async def detail(self, bundle: CredentialBundle, *, bvid: str) -> ArchiveDetail:
        response = await self._protocol.public_archive_view(bundle, bvid=bvid)
        tag_response = await self._protocol.public_archive_tags(bundle, bvid=bvid)
        data = response.get('data') if isinstance(response, Mapping) else None
        if not isinstance(data, Mapping):
            raise ArchiveMigrationUnavailable('B 站没有返回有效的稿件信息')
        parsed_bvid = _text(data.get('bvid'))
        aid = _positive_int(data.get('aid'))
        owner = data.get('owner')
        owner_uid = (
            _positive_int(owner.get('mid')) if isinstance(owner, Mapping) else None
        )
        owner_name = _text(owner.get('name')) if isinstance(owner, Mapping) else None
        title = _text(data.get('title'))
        tid = _positive_int(data.get('tid'))
        raw_pages = data.get('pages')
        if (
            parsed_bvid != bvid
            or aid is None
            or owner_uid is None
            or owner_name is None
            or title is None
            or tid is None
            or not isinstance(raw_pages, list)
        ):
            raise ArchiveMigrationUnavailable('B 站稿件信息不完整')
        pages: List[ArchivePage] = []
        for index, raw_page in enumerate(raw_pages, 1):
            if not isinstance(raw_page, Mapping):
                continue
            page = _positive_int(raw_page.get('page')) or index
            cid = _positive_int(raw_page.get('cid'))
            if cid is None:
                continue
            part = _text(raw_page.get('part')) or 'P{}'.format(page)
            pages.append(
                ArchivePage(
                    page=page,
                    cid=cid,
                    title=part[:200],
                    duration_seconds=_positive_int(raw_page.get('duration')),
                )
            )
        if not pages:
            raise ArchiveMigrationUnavailable('稿件没有可下载的分 P')
        tags = self._tags(tag_response)
        cover_url = _text(data.get('pic')) or ''
        if cover_url.startswith('http://'):
            cover_url = 'https://' + cover_url[len('http://') :]
        raw_description = data.get('desc')
        description = raw_description if isinstance(raw_description, str) else ''
        if len(description) > 2000:
            raise ArchiveMigrationUnavailable('B 站稿件简介超过投稿上限')
        return ArchiveDetail(
            aid=aid,
            bvid=bvid,
            owner_uid=owner_uid,
            owner_name=owner_name[:200],
            title=title[:200],
            description=description,
            tags=tags,
            tid=tid,
            cover_url=cover_url,
            published_at=_positive_int(data.get('pubdate')),
            pages=tuple(sorted(pages, key=lambda value: value.page)),
        )

    @staticmethod
    def _tags(response: Mapping[str, Any]) -> Tuple[str, ...]:
        data = response.get('data')
        if not isinstance(data, list):
            return ()
        values: List[str] = []
        seen = set()
        for entry in data:
            if not isinstance(entry, Mapping):
                continue
            tag = _text(entry.get('tag_name'))
            if tag is None or tag in seen:
                continue
            seen.add(tag)
            values.append(tag[:40])
        return tuple(values)


class ArchiveMigrationService:
    def __init__(
        self,
        database: BiliUploadDatabase,
        *,
        recording_root: Path,
        catalog: ArchiveCatalog,
        detail_reader: ArchiveDetailReader,
        downloader: Any,
        bundle_loader: Callable[[int], Awaitable[CredentialBundle]],
        task_creator: ArchiveTaskCreator,
        clock: Callable[[], float] = time.time,
        idle_poll_seconds: float = 10,
    ) -> None:
        if idle_poll_seconds <= 0:
            raise ValueError('idle poll interval must be positive')
        self._database = database
        self._recording_root = Path(
            os.path.abspath(os.path.expanduser(str(recording_root)))
        ).resolve()
        self._catalog = catalog
        self._detail_reader = detail_reader
        self._downloader = downloader
        self._bundle_loader = bundle_loader
        self._task_creator = task_creator
        self._clock = clock
        self._idle_poll_seconds = idle_poll_seconds
        self._wake = asyncio.Event()
        self._task: Optional[asyncio.Task[None]] = None

    async def start(self) -> None:
        if self._task is not None:
            return
        await self.recover_interrupted()
        repaired = await self.repair_inferred_anchors()
        if repaired:
            logger.info(
                'archive migration corrected {} recording anchor assignments', repaired
            )
        self._wake.set()
        self._task = asyncio.create_task(self._run(), name='bili-archive-migration')

    async def recover_interrupted(self) -> int:
        now = self._now()

        def recover(connection: sqlite3.Connection) -> int:
            recovered = connection.execute(
                "UPDATE archive_migration_items SET state='queued',progress=0,"
                "error=NULL,updated_at=? WHERE state IN "
                "('downloading','creating_task')",
                (now,),
            ).rowcount
            recovered += connection.execute(
                "UPDATE archive_migration_items SET state='queued',progress=0,"
                'error=NULL,updated_at=? '
                "WHERE state='task_created' AND session_id IS NOT NULL AND EXISTS("
                'SELECT 1 FROM recording_parts part '
                'JOIN vainglory_part_jobs analysis ON analysis.part_id=part.id '
                'WHERE part.session_id=archive_migration_items.session_id '
                "AND analysis.state='failed' AND part.video_deleted_at IS NOT NULL)",
                (now,),
            ).rowcount
            rows = connection.execute(
                'SELECT item.id,upload.policy_snapshot_json '
                'FROM archive_migration_items item '
                'JOIN upload_jobs upload ON upload.id=item.upload_job_id '
                "WHERE item.state='task_created'"
            ).fetchall()
            for row in rows:
                try:
                    snapshot = json.loads(str(row['policy_snapshot_json']))
                except (TypeError, ValueError):
                    snapshot = {}
                if bool(snapshot.get('danmaku_backfill')):
                    continue
                recovered += connection.execute(
                    "UPDATE archive_migration_items SET state='queued',progress=0,"
                    'error=NULL,updated_at=? WHERE id=? '
                    "AND state='task_created'",
                    (now, int(row['id'])),
                ).rowcount
            return recovered

        return await self._database.write(recover)

    async def repair_inferred_anchors(self) -> int:
        def repair(connection: sqlite3.Connection) -> int:
            rows = connection.execute(
                'SELECT session.id,session.title,session.room_id,'
                'session.anchor_uid,session.anchor_name,job.source_uid '
                'FROM archive_migration_items item '
                'JOIN archive_migration_jobs job ON job.id=item.migration_id '
                'JOIN recording_sessions session ON session.id=item.session_id '
                'WHERE session.anchor_uid=job.source_uid '
                "AND session.anchor_name!=''"
            ).fetchall()
            repaired = 0
            for row in rows:
                room_id, anchor_uid, anchor_name = infer_recorded_anchor(
                    connection,
                    str(row['title']),
                    '',
                    excluded_anchor_uids=(int(row['source_uid']),),
                    excluded_anchor_names=(str(row['anchor_name']),),
                )
                repaired += connection.execute(
                    'UPDATE recording_sessions SET room_id=?,anchor_uid=?,'
                    'anchor_name=? WHERE id=? AND anchor_uid=?',
                    (
                        room_id,
                        anchor_uid,
                        anchor_name,
                        int(row['id']),
                        int(row['source_uid']),
                    ),
                ).rowcount
            return repaired

        return await self._database.write(repair)

    async def close(self) -> None:
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def request(
        self, *, source_uid: int, download_account_id: int, target_account_id: int
    ) -> ArchiveMigrationStatus:
        if source_uid <= 0:
            raise ArchiveMigrationUnavailable('源账号 UID 无效')
        rows = await self._database.fetchall(
            'SELECT id,uid,state FROM bili_accounts WHERE id IN (?,?)',
            (int(download_account_id), int(target_account_id)),
        )
        accounts = {int(row['id']): row for row in rows}
        download_account = accounts.get(int(download_account_id))
        target_account = accounts.get(int(target_account_id))
        if download_account is None or target_account is None:
            raise ArchiveMigrationNotFound('下载账号或目标账号不存在')
        if str(download_account['state']) != 'active':
            raise ArchiveMigrationUnavailable('下载账号当前不可用')
        if str(target_account['state']) != 'active':
            raise ArchiveMigrationUnavailable('目标账号当前不可用')
        if int(target_account['uid']) == int(source_uid):
            raise ArchiveMigrationUnavailable('源账号和目标账号不能相同')
        now = self._now()

        def persist(connection: sqlite3.Connection) -> Tuple[int, bool]:
            existing = connection.execute(
                'SELECT id,state FROM archive_migration_jobs '
                'WHERE source_uid=? AND target_account_id=?',
                (int(source_uid), int(target_account_id)),
            ).fetchone()
            if existing is not None and str(existing['state']) in (
                'discovering',
                'running',
            ):
                migration_id = int(existing['id'])
                connection.execute(
                    "UPDATE archive_migration_items SET state='queued',progress=0,"
                    'downloaded_page_count=0,error=NULL,updated_at=? '
                    "WHERE migration_id=? AND state='failed'",
                    (now, migration_id),
                )
                return migration_id, str(existing['state']) == 'running'
            if existing is None:
                cursor = connection.execute(
                    'INSERT INTO archive_migration_jobs('
                    'source_uid,download_account_id,target_account_id,state,'
                    'progress,discovered_count,completed_count,failed_count,error,'
                    'requested_at,started_at,completed_at,updated_at,daily_limit) '
                    "VALUES(?,?,?,'discovering',0,0,0,0,NULL,?,NULL,NULL,?,?)",
                    (
                        int(source_uid),
                        int(download_account_id),
                        int(target_account_id),
                        now,
                        now,
                        _DEFAULT_DAILY_LIMIT,
                    ),
                )
                return int(cursor.lastrowid), False
            migration_id = int(existing['id'])
            connection.execute(
                'UPDATE archive_migration_jobs SET download_account_id=?,'
                "state='discovering',progress=0,discovered_count=0,"
                'completed_count=0,failed_count=0,error=NULL,requested_at=?,'
                'started_at=NULL,completed_at=NULL,updated_at=? WHERE id=?',
                (int(download_account_id), now, now, migration_id),
            )
            connection.execute(
                "UPDATE archive_migration_items SET state='queued',progress=0,"
                'downloaded_page_count=0,error=NULL,updated_at=? '
                "WHERE migration_id=? AND state='failed'",
                (now, migration_id),
            )
            return migration_id, False

        migration_id, refresh_running = await self._database.write(persist)
        if refresh_running:
            await self._refresh_job(migration_id)
        self._wake.set()
        return await self.status(migration_id)

    async def status(self, migration_id: int) -> ArchiveMigrationStatus:
        row = await self._database.fetchone(
            _STATUS_SELECT + ' WHERE job.id=?', (int(migration_id),)
        )
        if row is None:
            raise ArchiveMigrationNotFound('稿件迁移任务不存在')
        return self._status(row)

    async def list_statuses(self) -> Tuple[ArchiveMigrationStatus, ...]:
        rows = await self._database.fetchall(
            _STATUS_SELECT + ' ORDER BY job.requested_at DESC,job.id DESC'
        )
        return tuple(self._status(row) for row in rows)

    async def list_items(
        self, migration_id: int, *, limit: int = 100
    ) -> Tuple[ArchiveMigrationItem, ...]:
        if not 1 <= int(limit) <= 200:
            raise ValueError('稿件明细数量必须在 1 到 200 之间')
        if (
            await self._database.scalar(
                'SELECT 1 FROM archive_migration_jobs WHERE id=?', (int(migration_id),)
            )
            != 1
        ):
            raise ArchiveMigrationNotFound('稿件迁移任务不存在')
        rows = await self._database.fetchall(
            'SELECT item.*,upload.state AS upload_state,'
            'upload.submit_state,upload.comment_branch_state,'
            'upload.danmaku_branch_state,upload.bvid AS target_bvid,'
            '(SELECT CASE '
            "WHEN SUM(CASE WHEN analysis.state='analyzing' THEN 1 ELSE 0 END)>0 "
            "THEN 'analyzing' "
            "WHEN SUM(CASE WHEN analysis.state='pending' THEN 1 ELSE 0 END)>0 "
            "THEN 'pending' "
            "WHEN SUM(CASE WHEN analysis.state='failed' THEN 1 ELSE 0 END)>0 "
            "THEN 'failed' "
            "WHEN COUNT(*)>0 THEN 'ready' ELSE NULL END "
            'FROM vainglory_part_jobs analysis '
            'WHERE analysis.session_id=item.session_id) AS analysis_state '
            'FROM archive_migration_items item '
            'LEFT JOIN upload_jobs upload ON upload.id=item.upload_job_id '
            'WHERE item.migration_id=? '
            "ORDER BY CASE item.state WHEN 'downloading' THEN 0 "
            "WHEN 'creating_task' THEN 1 ELSE 2 END,"
            'CASE WHEN item.published_at IS NULL THEN 1 ELSE 0 END,'
            'item.published_at DESC,item.id DESC LIMIT ?',
            (int(migration_id), int(limit)),
        )
        return tuple(self._item(row) for row in rows)

    async def update_control(
        self,
        migration_id: int,
        *,
        paused: Optional[bool] = None,
        daily_limit: Optional[int] = None,
    ) -> ArchiveMigrationStatus:
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
            return await self.status(migration_id)
        values.append('updated_at=?')
        parameters.append(self._now())
        parameters.append(int(migration_id))
        changed = await self._database.execute(
            'UPDATE archive_migration_jobs SET {} WHERE id=?'.format(','.join(values)),
            tuple(parameters),
        )
        if changed != 1:
            raise ArchiveMigrationNotFound('稿件迁移任务不存在')
        if paused is False or daily_limit is not None:
            self._wake.set()
        return await self.status(migration_id)

    async def run_once(self) -> bool:
        discovering = await self._database.fetchone(
            'SELECT id FROM archive_migration_jobs '
            "WHERE state='discovering' AND operator_paused=0 "
            'ORDER BY requested_at,id LIMIT 1'
        )
        if discovering is not None:
            await self._discover(int(discovering['id']))
            return True
        item = await self._claim_item()
        if item is not None:
            await self._process_item(item)
            return True
        return False

    async def _discover(self, migration_id: int) -> None:
        row = await self._database.fetchone(
            'SELECT * FROM archive_migration_jobs WHERE id=?', (migration_id,)
        )
        if row is None:
            return
        now = self._now()
        await self._database.execute(
            'UPDATE archive_migration_jobs SET started_at=COALESCE(started_at,?),'
            'updated_at=? WHERE id=?',
            (now, now, migration_id),
        )
        discovered = 0
        try:
            bundle = await self._bundle_loader(int(row['download_account_id']))
            async for archive in self._catalog.iter_archives(
                bundle, source_uid=int(row['source_uid'])
            ):
                await self._persist_listing(migration_id, archive)
                discovered += 1
        except (asyncio.CancelledError, KeyboardInterrupt):
            raise
        except BaseException as error:
            await self._fail_job(
                migration_id, '{}: {}'.format(type(error).__name__, error)
            )
            return
        logger.info(
            'archive migration discovered source_uid={} migration_id={} entries={}',
            int(row['source_uid']),
            migration_id,
            discovered,
        )
        await self._database.execute(
            "UPDATE archive_migration_jobs SET state='running',error=NULL,"
            'updated_at=? WHERE id=?',
            (self._now(), migration_id),
        )
        await self._refresh_job(migration_id)

    async def _persist_listing(
        self, migration_id: int, archive: ArchiveListing
    ) -> None:
        if _BVID_PATTERN.fullmatch(archive.bvid) is None:
            return
        title = archive.title.strip()[:200] or archive.bvid
        now = self._now()

        def persist(connection: sqlite3.Connection) -> None:
            connection.execute(
                'INSERT INTO archive_migration_items('
                'migration_id,aid,bvid,title,published_at,state,progress,page_count,'
                'downloaded_page_count,session_id,upload_job_id,error,created_at,'
                "updated_at) VALUES(?,NULL,?,?,?,'queued',0,0,0,NULL,NULL,NULL,?,?) "
                'ON CONFLICT(migration_id,bvid) DO UPDATE SET '
                'title=excluded.title,published_at=COALESCE('
                'excluded.published_at,archive_migration_items.published_at),'
                'updated_at=excluded.updated_at',
                (migration_id, archive.bvid, title, archive.published_at, now, now),
            )
            connection.execute(
                'UPDATE archive_migration_jobs SET discovered_count=('
                'SELECT COUNT(*) FROM archive_migration_items '
                'WHERE migration_id=?),updated_at=? WHERE id=?',
                (migration_id, now, migration_id),
            )

        await self._database.write(persist)

    async def _claim_item(self) -> Optional[sqlite3.Row]:
        now = self._now()
        quota_day = self._quota_day(now)

        def claim(connection: sqlite3.Connection) -> Optional[sqlite3.Row]:
            row = connection.execute(
                'SELECT item.*,item.quota_day AS item_quota_day,'
                'job.source_uid,job.download_account_id,job.target_account_id,'
                'job.quota_day AS job_quota_day,job.daily_used,job.daily_limit '
                'FROM archive_migration_items item '
                'JOIN archive_migration_jobs job ON job.id=item.migration_id '
                "WHERE item.state='queued' AND job.state='running' "
                'AND job.operator_paused=0 AND ('
                'item.quota_day=? OR job.quota_day IS NULL OR job.quota_day<>? '
                'OR job.daily_used<job.daily_limit) '
                'ORDER BY CASE WHEN item.published_at IS NULL THEN 1 ELSE 0 END,'
                'item.published_at DESC,item.id ASC LIMIT 1',
                (quota_day, quota_day),
            ).fetchone()
            if row is None:
                return None
            job_quota_day = (
                None if row['job_quota_day'] is None else str(row['job_quota_day'])
            )
            item_quota_day = (
                None if row['item_quota_day'] is None else str(row['item_quota_day'])
            )
            daily_used = int(row['daily_used']) if job_quota_day == quota_day else 0
            if item_quota_day != quota_day and daily_used >= int(row['daily_limit']):
                return None
            changed = connection.execute(
                "UPDATE archive_migration_items SET state='downloading',"
                'progress=0,error=NULL,attempt_count=attempt_count+1,'
                'quota_day=?,updated_at=? '
                "WHERE id=? AND state='queued'",
                (quota_day, now, int(row['id'])),
            )
            if changed.rowcount != 1:
                return None
            connection.execute(
                'UPDATE archive_migration_jobs SET quota_day=?,daily_used=? '
                'WHERE id=?',
                (
                    quota_day,
                    daily_used + (1 if item_quota_day != quota_day else 0),
                    int(row['migration_id']),
                ),
            )
            return row

        return await self._database.write(claim)

    async def _process_item(self, item: sqlite3.Row) -> None:
        item_id = int(item['id'])
        migration_id = int(item['migration_id'])
        try:
            bundle = await self._bundle_loader(int(item['download_account_id']))
            detail = await self._detail_reader.detail(bundle, bvid=str(item['bvid']))
            if detail.bvid != str(item['bvid']):
                raise ArchiveMigrationUnavailable('稿件详情与列表不一致')
            if detail.owner_uid != int(item['source_uid']):
                raise ArchiveMigrationUnavailable('稿件不属于指定的源账号')
            if not detail.pages:
                raise ArchiveMigrationUnavailable('稿件没有可下载的分 P')
            await self._database.execute(
                'UPDATE archive_migration_items SET aid=?,title=?,published_at=?,'
                "state='downloading',page_count=?,downloaded_page_count=0,"
                'progress=0,error=NULL,updated_at=? WHERE id=?',
                (
                    detail.aid,
                    detail.title,
                    detail.published_at,
                    len(detail.pages),
                    self._now(),
                    item_id,
                ),
            )
            paths = await self._download_pages(item, detail, bundle)
            session_id = await self._ensure_session(item, detail, paths)
            await self._database.execute(
                "UPDATE archive_migration_items SET state='creating_task',"
                'progress=0.95,session_id=?,error=NULL,updated_at=? WHERE id=?',
                (session_id, self._now(), item_id),
            )
            tags = ','.join(detail.tags) or '转载'
            job_id = await self._task_creator.create_archive_migration_job(
                session_id,
                description=detail.description,
                tags=tags,
                part_titles=tuple(page.title for page in detail.pages),
            )
            await self._verify_upload_contract(
                session_id,
                job_id,
                description=detail.description,
                part_titles=tuple(page.title for page in detail.pages),
            )
            now = self._now()
            await self._database.execute(
                "UPDATE archive_migration_items SET state='task_created',"
                'progress=1,session_id=?,upload_job_id=?,error=NULL,updated_at=? '
                'WHERE id=?',
                (session_id, job_id, now, item_id),
            )
            await self._database.execute(
                "UPDATE recording_sessions SET upload_resolution_state='job_created',"
                'upload_resolved_at=? WHERE id=?',
                (now, session_id),
            )
            logger.info(
                'archive migration task created migration_id={} bvid={} '
                'session_id={} upload_job_id={}',
                migration_id,
                detail.bvid,
                session_id,
                job_id,
            )
        except (asyncio.CancelledError, KeyboardInterrupt):
            raise
        except BaseException as error:
            await self._fail_item(item_id, '{}: {}'.format(type(error).__name__, error))
            logger.exception(
                'archive migration item failed migration_id={} bvid={}',
                migration_id,
                str(item['bvid']),
            )
        await self._refresh_job(migration_id)

    async def _verify_upload_contract(
        self,
        session_id: int,
        job_id: int,
        *,
        description: str,
        part_titles: Tuple[str, ...],
    ) -> None:
        expected = len(part_titles)
        now = self._now()

        def verify(connection: sqlite3.Connection) -> Optional[bool]:
            row = connection.execute(
                'SELECT job.policy_snapshot_json,'
                '(SELECT COUNT(*) FROM upload_jobs candidate '
                'WHERE candidate.session_id=job.session_id) AS job_count,'
                '(SELECT COUNT(*) FROM recording_parts part '
                'WHERE part.session_id=job.session_id) AS recording_part_count,'
                '(SELECT COUNT(*) FROM recording_parts part '
                'WHERE part.session_id=job.session_id AND part.xml_completed=1 '
                "AND part.xml_path IS NOT NULL AND part.xml_path<>'') "
                'AS xml_part_count,'
                '(SELECT COUNT(*) FROM upload_parts part '
                'WHERE part.job_id=job.id) AS upload_part_count '
                'FROM upload_jobs job WHERE job.id=? AND job.session_id=?',
                (int(job_id), int(session_id)),
            ).fetchone()
            if row is None:
                return None
            try:
                snapshot = json.loads(str(row['policy_snapshot_json']))
            except (TypeError, ValueError, json.JSONDecodeError):
                snapshot = None
            recording_part_indexes = (
                snapshot.get('recording_part_indexes')
                if isinstance(snapshot, dict)
                else None
            )
            contract_matches = (
                isinstance(snapshot, dict)
                and snapshot.get('description') == description
                and bool(snapshot.get('danmaku_backfill'))
                and snapshot.get('part_titles') == list(part_titles)
                and isinstance(recording_part_indexes, list)
                and len(recording_part_indexes) == expected
                and int(row['job_count']) == 1
                and int(row['recording_part_count']) == expected
                and int(row['xml_part_count']) == expected
                and int(row['upload_part_count']) == expected
            )
            if contract_matches:
                connection.execute(
                    "UPDATE upload_jobs SET state='ready',operator_paused=0,"
                    'operator_resume_state=NULL,review_reason=NULL,updated_at=? '
                    "WHERE id=? AND state='paused' AND operator_paused=1 "
                    "AND operator_resume_state='ready' "
                    "AND review_reason='迁移一致性校验中' "
                    "AND submit_state='prepared'",
                    (now, int(job_id)),
                )
                return True
            connection.execute(
                "UPDATE upload_jobs SET state='paused',operator_paused=1,"
                "operator_resume_state='ready',review_reason=?,next_attempt_at=0,"
                'lease_owner=NULL,lease_until=NULL,updated_at=? WHERE id=? '
                "AND submit_state='prepared'",
                ('多 P、简介或弹幕迁移一致性校验失败', now, int(job_id)),
            )
            return False

        result = await self._database.write(verify)
        if result is None:
            raise ArchiveMigrationUnavailable('迁移上传任务不存在')
        if not result:
            raise ArchiveMigrationUnavailable(
                '多 P、简介或弹幕迁移一致性校验失败，已禁止投稿'
            )

    async def _download_pages(
        self, item: sqlite3.Row, detail: ArchiveDetail, bundle: CredentialBundle
    ) -> Tuple[Path, ...]:
        paths: List[Path] = []
        total_pages = len(detail.pages)
        for ordinal, page in enumerate(detail.pages, 1):
            target = self._target_path(
                int(item['migration_id']), detail.bvid, page.page
            )
            danmaku_target = target.with_suffix('.xml')
            if self._regular_file_size(target) is None:

                async def progress(
                    downloaded: int,
                    total: Optional[int],
                    *,
                    completed_pages: int = ordinal - 1,
                ) -> None:
                    fraction = (
                        0.0
                        if total is None or total <= 0
                        else min(0.99, float(downloaded) / float(total))
                    )
                    overall = (
                        0.9 * (float(completed_pages) + fraction) / float(total_pages)
                    )
                    await self._database.execute(
                        'UPDATE archive_migration_items SET progress=?,updated_at=? '
                        "WHERE id=? AND state='downloading'",
                        (overall, self._now(), int(item['id'])),
                    )

                await self._downloader.download(
                    bundle,
                    bvid=detail.bvid,
                    cid=page.cid,
                    page=page.page,
                    target=target,
                    danmaku_target=danmaku_target,
                    progress=progress,
                )
            elif self._regular_file_size(danmaku_target) is None:
                await self._downloader.download_danmaku(
                    bundle,
                    bvid=detail.bvid,
                    cid=page.cid,
                    page=page.page,
                    target=danmaku_target,
                )
            if self._regular_file_size(target) is None:
                raise ArchiveMigrationUnavailable('下载完成后没有生成有效视频文件')
            if self._regular_file_size(danmaku_target) is None:
                raise ArchiveMigrationUnavailable('下载完成后没有生成弹幕文件')
            paths.append(target)
            await self._database.execute(
                'UPDATE archive_migration_items SET downloaded_page_count=?,'
                'progress=?,updated_at=? WHERE id=?',
                (
                    ordinal,
                    0.9 * float(ordinal) / float(total_pages),
                    self._now(),
                    int(item['id']),
                ),
            )
        return tuple(paths)

    async def _ensure_session(
        self, item: sqlite3.Row, detail: ArchiveDetail, paths: Tuple[Path, ...]
    ) -> int:
        now = self._now()
        started_at = int(detail.published_at or now)
        duration = sum(page.duration_seconds or 0 for page in detail.pages)
        command = replace(
            default_room_upload_policy(),
            account_mode='fixed',
            account_id=int(item['target_account_id']),
            enabled=True,
            title_template='{{ title }}',
            description_template='{{ archive_description }}',
            part_title_template='P{{ part_index }}',
            dynamic_template='{{ title }}',
            tid=detail.tid,
            tags='{{ archive_tags }}',
            creation_statement_id=-1,
            original_authorization=False,
            source='',
            auto_comment=False,
            danmaku_backfill=True,
            collection_season_id=None,
            collection_section_id=None,
            cover_mode='live',
            cover_asset_id=None,
            retention_mode='submitted',
            retention_days=0,
        )
        override_json = encode_submission_settings(command)
        key = 'bili-migration:{}:{}:{}'.format(
            int(item['source_uid']), int(item['target_account_id']), detail.bvid
        )

        def persist(connection: sqlite3.Connection) -> int:
            room_id, anchor_uid, anchor_name = infer_recorded_anchor(
                connection,
                detail.title,
                detail.description,
                excluded_anchor_uids=(detail.owner_uid,),
                excluded_anchor_names=(detail.owner_name,),
            )
            existing = connection.execute(
                'SELECT id,room_id,anchor_uid,anchor_name FROM recording_sessions '
                'WHERE broadcast_session_key=?',
                (key,),
            ).fetchone()
            if existing is not None:
                session_id = int(existing['id'])
                existing_parts = connection.execute(
                    'SELECT id,part_index FROM recording_parts '
                    'WHERE session_id=? ORDER BY part_index',
                    (session_id,),
                ).fetchall()
                if [int(row['part_index']) for row in existing_parts] != [
                    page.page for page in detail.pages
                ]:
                    raise ArchiveMigrationUnavailable('已迁移稿件的分 P 与源稿件不一致')
                for row, path in zip(existing_parts, paths):
                    size = self._regular_file_size(path)
                    if size is None:
                        raise ArchiveMigrationUnavailable('迁移视频文件不存在')
                    xml_path = path.with_suffix('.xml')
                    if self._regular_file_size(xml_path) is None:
                        raise ArchiveMigrationUnavailable('迁移弹幕文件不存在')
                    connection.execute(
                        'UPDATE recording_parts SET source_path=?,final_path=?,'
                        "xml_path=?,artifact_state='ready',xml_completed=1,"
                        'file_size_bytes=?,video_deleted_at=NULL,'
                        'video_delete_reason=NULL,video_delete_error=NULL,'
                        'updated_at=? WHERE id=?',
                        (
                            str(path),
                            str(path),
                            str(xml_path),
                            size,
                            now,
                            int(row['id']),
                        ),
                    )
                    connection.execute(
                        "UPDATE vainglory_part_jobs SET state='pending',progress=0,"
                        'error=NULL,started_at=NULL,completed_at=NULL,updated_at=? '
                        "WHERE part_id=? AND state='failed'",
                        (now, int(row['id'])),
                    )
                legacy_owner_identity = (
                    existing['anchor_uid'] is not None
                    and int(existing['anchor_uid']) == detail.owner_uid
                    and str(existing['anchor_name']) == detail.owner_name
                )
                if legacy_owner_identity:
                    connection.execute(
                        'UPDATE recording_sessions SET room_id=?,anchor_uid=?,'
                        'anchor_name=? WHERE id=?',
                        (room_id, anchor_uid, anchor_name, session_id),
                    )
                connection.execute(
                    'UPDATE recording_sessions SET upload_override_json=? WHERE id=?',
                    (override_json, session_id),
                )
                connection.execute(
                    'UPDATE archive_migration_items SET session_id=?,updated_at=? '
                    'WHERE id=?',
                    (session_id, now, int(item['id'])),
                )
                return session_id
            cursor = connection.execute(
                'INSERT INTO recording_sessions('
                'room_id,broadcast_session_key,live_start_time,state,started_at,'
                'ended_at,title,cover_url,anchor_uid,anchor_name,live_end_time,'
                'upload_intent,source_kind,upload_decision,upload_override_json,'
                'upload_resolution_state,upload_resolved_at) '
                "VALUES(?,? ,?,'closed',?,?,?,?,?,?,?,'upload','live','upload',?,"
                "'not_requested',?)",
                (
                    room_id,
                    key,
                    started_at,
                    started_at,
                    started_at + duration,
                    detail.title,
                    detail.cover_url,
                    anchor_uid,
                    anchor_name,
                    started_at + duration,
                    override_json,
                    now,
                ),
            )
            session_id = int(cursor.lastrowid)
            run_id = 'bili-migration-run:{}:{}'.format(
                int(item['migration_id']), detail.bvid
            )
            connection.execute(
                'INSERT INTO recording_runs(id,session_id,state,started_at,ended_at) '
                "VALUES(?,?,'finished',?,?)",
                (run_id, session_id, started_at, started_at + duration),
            )
            elapsed = 0
            for page, path in zip(detail.pages, paths):
                part_started_at = started_at + elapsed
                part_duration = int(page.duration_seconds or 0)
                part_ended_at = part_started_at + part_duration
                size = self._regular_file_size(path)
                if size is None:
                    raise ArchiveMigrationUnavailable('迁移视频文件不存在')
                xml_path = path.with_suffix('.xml')
                if self._regular_file_size(xml_path) is None:
                    raise ArchiveMigrationUnavailable('迁移弹幕文件不存在')
                connection.execute(
                    'INSERT INTO recording_parts('
                    'session_id,run_id,part_index,source_path,final_path,xml_path,'
                    'record_start_time,artifact_state,xml_completed,'
                    'source_completed_at,postprocessed_at,record_end_time,'
                    'record_duration_seconds,file_size_bytes,created_at,updated_at,'
                    'media_index_state) '
                    "VALUES(?,?,?,?,?,?,?,'ready',1,?,?,?,?,?,?,?,'pending')",
                    (
                        session_id,
                        run_id,
                        page.page,
                        str(path),
                        str(path),
                        str(xml_path),
                        part_started_at,
                        now,
                        now,
                        part_ended_at,
                        part_duration,
                        size,
                        now,
                        now,
                    ),
                )
                elapsed += part_duration
            connection.execute(
                'UPDATE archive_migration_items SET session_id=?,updated_at=? '
                'WHERE id=?',
                (session_id, now, int(item['id'])),
            )
            return session_id

        return await self._database.write(persist)

    async def _refresh_job(self, migration_id: int) -> None:
        now = self._now()

        def refresh(connection: sqlite3.Connection) -> None:
            row = connection.execute(
                'SELECT state FROM archive_migration_jobs WHERE id=?', (migration_id,)
            ).fetchone()
            if row is None or str(row['state']) != 'running':
                return
            counts = connection.execute(
                'SELECT COUNT(*) AS total,'
                "SUM(CASE WHEN state='task_created' THEN 1 ELSE 0 END) AS completed,"
                "SUM(CASE WHEN state='failed' THEN 1 ELSE 0 END) AS failed,"
                'SUM(progress) AS progress '
                'FROM archive_migration_items WHERE migration_id=?',
                (migration_id,),
            ).fetchone()
            assert counts is not None
            total = int(counts['total'])
            completed = int(counts['completed'] or 0)
            failed = int(counts['failed'] or 0)
            terminal = completed + failed
            progress = (
                1.0
                if total == 0
                else max(0.0, min(1.0, float(counts['progress'] or 0.0) / float(total)))
            )
            state = 'completed' if terminal == total else 'running'
            connection.execute(
                'UPDATE archive_migration_jobs SET state=?,progress=?,'
                'discovered_count=?,completed_count=?,failed_count=?,'
                'completed_at=?,updated_at=? WHERE id=?',
                (
                    state,
                    progress,
                    total,
                    completed,
                    failed,
                    now if state == 'completed' else None,
                    now,
                    migration_id,
                ),
            )

        await self._database.write(refresh)

    async def _fail_job(self, migration_id: int, error: str) -> None:
        now = self._now()
        await self._database.execute(
            "UPDATE archive_migration_jobs SET state='failed',progress=0,"
            'error=?,completed_at=?,updated_at=? WHERE id=?',
            (error.strip()[:500] or '稿件迁移失败', now, now, migration_id),
        )

    async def _fail_item(self, item_id: int, error: str) -> None:
        await self._database.execute(
            "UPDATE archive_migration_items SET state='failed',progress=1,"
            'error=?,updated_at=? WHERE id=?',
            (error.strip()[:500] or '稿件迁移失败', self._now(), item_id),
        )

    async def _run(self) -> None:
        while True:
            try:
                processed = await self.run_once()
            except asyncio.CancelledError:
                raise
            except BaseException:
                logger.exception('archive migration worker failed')
                processed = False
            if processed:
                continue
            self._wake.clear()
            try:
                await asyncio.wait_for(
                    self._wake.wait(), timeout=self._idle_poll_seconds
                )
            except asyncio.TimeoutError:
                pass

    def _target_path(self, migration_id: int, bvid: str, page: int) -> Path:
        if migration_id <= 0 or page <= 0 or _BVID_PATTERN.fullmatch(bvid) is None:
            raise ArchiveMigrationUnavailable('稿件分 P 信息无效')
        return (
            self._recording_root
            / '.archive-migrations'
            / str(migration_id)
            / bvid
            / 'p{:03d}.mp4'.format(page)
        ).resolve()

    @staticmethod
    def _regular_file_size(path: Path) -> Optional[int]:
        try:
            result = path.stat()
        except OSError:
            return None
        return result.st_size if path.is_file() and result.st_size > 0 else None

    @staticmethod
    def _status(row: sqlite3.Row) -> ArchiveMigrationStatus:
        return ArchiveMigrationStatus(
            id=int(row['id']),
            source_uid=int(row['source_uid']),
            source_name=(
                None if row['source_name'] is None else str(row['source_name'])
            ),
            download_account_id=int(row['download_account_id']),
            target_account_id=int(row['target_account_id']),
            state=str(row['state']),
            progress=float(row['progress']),
            discovered_count=int(row['discovered_count']),
            completed_count=int(row['completed_count']),
            failed_count=int(row['failed_count']),
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
        )

    @staticmethod
    def _item(row: sqlite3.Row) -> ArchiveMigrationItem:
        return ArchiveMigrationItem(
            id=int(row['id']),
            migration_id=int(row['migration_id']),
            bvid=str(row['bvid']),
            title=str(row['title']),
            published_at=(
                None if row['published_at'] is None else int(row['published_at'])
            ),
            state=str(row['state']),
            progress=float(row['progress']),
            page_count=int(row['page_count']),
            downloaded_page_count=int(row['downloaded_page_count']),
            attempt_count=int(row['attempt_count']),
            session_id=None if row['session_id'] is None else int(row['session_id']),
            upload_job_id=(
                None if row['upload_job_id'] is None else int(row['upload_job_id'])
            ),
            upload_state=(
                None if row['upload_state'] is None else str(row['upload_state'])
            ),
            submit_state=(
                None if row['submit_state'] is None else str(row['submit_state'])
            ),
            comment_branch_state=(
                None
                if row['comment_branch_state'] is None
                else str(row['comment_branch_state'])
            ),
            danmaku_branch_state=(
                None
                if row['danmaku_branch_state'] is None
                else str(row['danmaku_branch_state'])
            ),
            analysis_state=(
                None if row['analysis_state'] is None else str(row['analysis_state'])
            ),
            target_bvid=(
                None if row['target_bvid'] is None else str(row['target_bvid'])
            ),
            error=None if row['error'] is None else str(row['error']),
            updated_at=int(row['updated_at']),
        )

    def _now(self) -> int:
        return max(1, int(self._clock()))

    @staticmethod
    def _quota_day(now: int) -> str:
        return datetime.fromtimestamp(now, ZoneInfo('Asia/Shanghai')).date().isoformat()


def _positive_int(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _text(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None
