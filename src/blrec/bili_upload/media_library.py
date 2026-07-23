from __future__ import annotations

import asyncio
import errno
import os
import shutil
import sqlite3
import stat
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import (
    AsyncIterable,
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

from .artifact_recovery import RecoveredArtifact, probe_recording_artifact
from .database import BiliUploadDatabase
from .upos import FileIdentity

__all__ = (
    'ImportPartRequest',
    'MediaLibrary',
    'MediaLibraryConflict',
    'MediaLibraryItem',
    'MediaLibraryNotFound',
    'MediaLibraryPart',
    'SubmissionHistoryEntry',
    'media_library_move_staging_path',
)

_T = TypeVar('_T')
_VIDEO_SUFFIXES = frozenset(('.flv', '.mp4', '.ts', '.m4s', '.mkv', '.mov', '.webm'))
_OWNED_SUFFIXES = _VIDEO_SUFFIXES | frozenset(
    ('.xml', '.jpg', '.jpeg', '.png', '.webp')
)
_ACTIVE_REPAIR_STATES = frozenset(('queued', 'checking', 'reuploading', 'editing'))
_EXTERNAL_IMPORT_ROOM_ID = 2_147_483_647
_MOVE_COPY_BUFFER_BYTES = 1024 * 1024


def media_library_move_staging_path(target: Path, move_id: int) -> Path:
    return target.with_name('.move-{}-{}'.format(int(move_id), target.name))


class MediaLibraryConflict(ValueError):
    pass


class MediaLibraryNotFound(ValueError):
    pass


@dataclass(frozen=True)
class ImportPartRequest:
    filename: str
    size_bytes: int


@dataclass(frozen=True)
class MediaLibraryPart:
    item_id: int
    part_index: int
    recording_part_id: Optional[int]
    original_filename: str
    storage_path: str
    expected_size: int
    received_size: int
    state: str
    error: Optional[str]
    duration_seconds: Optional[int] = None


@dataclass(frozen=True)
class MediaLibraryItem:
    id: int
    session_id: int
    kind: str
    origin: str
    storage_key: str
    display_name: str
    note: str
    state: str
    error: Optional[str]
    created_at: int
    updated_at: int
    room_id: int
    title: str
    anchor_name: str
    started_at: int
    tags: Tuple[str, ...] = ()
    parts: Tuple[MediaLibraryPart, ...] = ()


@dataclass(frozen=True)
class SubmissionHistoryEntry:
    aid: int
    bvid: str
    state: str
    account_id: int
    account_name: str
    occurred_at: int
    current: bool


class MediaLibrary:
    def __init__(
        self,
        database: BiliUploadDatabase,
        recording_root: Path,
        *,
        clock: Callable[[], float] = time.time,
        storage_key_factory: Callable[[], str] = lambda: uuid.uuid4().hex,
        artifact_probe: Callable[[str], Optional[RecoveredArtifact]] = (
            probe_recording_artifact
        ),
        replace_file: Callable[[str, str], None] = os.replace,
        unlink_file: Callable[[str], None] = os.unlink,
    ) -> None:
        self._database = database
        self._recording_root = Path(
            os.path.abspath(os.path.expanduser(str(recording_root)))
        ).resolve()
        self._storage_root = (self._recording_root.parent / 'favorites').resolve()
        self._clock = clock
        self._storage_key_factory = storage_key_factory
        self._artifact_probe = artifact_probe
        self._replace_file = replace_file
        self._unlink_file = unlink_file
        self._move_lock = asyncio.Lock()
        self._import_locks: Dict[Tuple[int, int], asyncio.Lock] = {}

    @property
    def storage_root(self) -> Path:
        return self._storage_root

    async def favorite(
        self, session_id: int, *, manager_subject: str
    ) -> MediaLibraryItem:
        if not manager_subject:
            raise MediaLibraryConflict('管理员身份不能为空')
        existing = await self._database.fetchone(
            'SELECT id,state,origin FROM media_library_items WHERE session_id=?',
            (int(session_id),),
        )
        if existing is not None:
            if str(existing['origin']) != 'recording':
                raise MediaLibraryConflict('该场直播已经属于媒体库')
            if str(existing['state']) in ('moving', 'failed'):
                await self._resume_moves(int(existing['id']))
            return await self.get_item(int(existing['id']))

        now = int(self._clock())

        def prepare(connection: sqlite3.Connection) -> int:
            current = connection.execute(
                'SELECT id,state,origin FROM media_library_items WHERE session_id=?',
                (int(session_id),),
            ).fetchone()
            if current is not None:
                return int(current['id'])
            session = connection.execute(
                'SELECT id,state,deletion_state,source_kind,title,anchor_name,'
                'cover_path FROM recording_sessions WHERE id=?',
                (int(session_id),),
            ).fetchone()
            if session is None:
                raise MediaLibraryNotFound('录制场次不存在')
            if str(session['source_kind']) != 'live':
                raise MediaLibraryConflict('只有完整直播可以收藏')
            if str(session['state']) != 'closed':
                raise MediaLibraryConflict('请等待录制结束后再收藏')
            if str(session['deletion_state']) != 'none':
                raise MediaLibraryConflict('该场直播正在删除，不能收藏')
            job = connection.execute(
                'SELECT state,lease_until,repair_state FROM upload_jobs '
                'WHERE session_id=?',
                (int(session_id),),
            ).fetchone()
            if job is not None and (
                str(job['state']) in ('uploading', 'submitting')
                or (job['lease_until'] is not None and int(job['lease_until']) > now)
                or str(job['repair_state']) in _ACTIVE_REPAIR_STATES
            ):
                raise MediaLibraryConflict('上传或修复正在使用文件，请稍后重试')
            active_clip = connection.execute(
                'SELECT 1 FROM highlight_clip_sources source '
                'JOIN recording_parts part ON part.id=source.part_id '
                'JOIN highlight_clips clip ON clip.id=source.clip_id '
                'WHERE part.session_id=? '
                "AND clip.state IN ('queued','processing') "
                "AND clip.deletion_state='none' LIMIT 1",
                (int(session_id),),
            ).fetchone()
            if active_clip is not None:
                raise MediaLibraryConflict('片段正在生成，请稍后重试收藏')
            part_rows = connection.execute(
                'SELECT id,part_index,source_path,final_path,xml_path,'
                'artifact_state,video_deleted_at,media_index_state '
                'FROM recording_parts WHERE session_id=? ORDER BY part_index,id',
                (int(session_id),),
            ).fetchall()
            if not part_rows:
                raise MediaLibraryConflict('该场直播没有可收藏的分 P')
            key = self._allocate_storage_key_in_connection(connection)
            target_directory = self._storage_root / key
            moves: Dict[str, Path] = {}
            library_parts = []
            for part in part_rows:
                if str(part['artifact_state']) in ('recording', 'postprocessing'):
                    raise MediaLibraryConflict('分 P 仍在处理中，请稍后重试收藏')
                if part['video_deleted_at'] is not None:
                    raise MediaLibraryConflict('分 P 视频已经被清理，不能收藏')
                if str(part['media_index_state']) == 'indexing':
                    raise MediaLibraryConflict('分 P 正在建立播放索引，请稍后重试')
                source = self._existing_owned_file(part['source_path'], _VIDEO_SUFFIXES)
                final = self._existing_owned_file(part['final_path'], _VIDEO_SUFFIXES)
                primary = final or source
                if primary is None:
                    raise MediaLibraryConflict('分 P 本地视频不可用，不能收藏')
                part_index = int(part['part_index'])
                primary_target = target_directory / 'part-{:04d}{}'.format(
                    part_index, primary.suffix.lower()
                )
                moves[str(primary)] = primary_target
                if source is not None and source != primary:
                    moves[str(source)] = target_directory / (
                        'part-{:04d}-source{}'.format(part_index, source.suffix.lower())
                    )
                if final is not None and final != primary:
                    moves[str(final)] = target_directory / (
                        'part-{:04d}-final{}'.format(part_index, final.suffix.lower())
                    )
                xml = self._existing_owned_file(
                    part['xml_path'], frozenset(('.xml',)), required=False
                )
                if xml is not None:
                    moves[str(xml)] = target_directory / 'part-{:04d}.xml'.format(
                        part_index
                    )
                library_parts.append(
                    (
                        part_index,
                        int(part['id']),
                        primary.name,
                        str(primary_target),
                        primary.stat().st_size,
                    )
                )
            self._append_upload_file_moves(
                connection, int(session_id), target_directory, moves
            )
            cover = self._existing_owned_file(
                session['cover_path'],
                frozenset(('.jpg', '.jpeg', '.png', '.webp')),
                required=False,
            )
            if cover is not None:
                moves[str(cover)] = target_directory / 'cover{}'.format(
                    cover.suffix.lower()
                )
            display_name = str(session['title']).strip()
            if not display_name:
                display_name = '{} 的直播'.format(
                    str(session['anchor_name']).strip() or '未命名主播'
                )
            cursor = connection.execute(
                'INSERT INTO media_library_items('
                'session_id,kind,origin,storage_key,display_name,state,'
                'created_at,updated_at) '
                "VALUES(?,'broadcast','recording',?,?,'moving',?,?)",
                (int(session_id), key, display_name[:200], now, now),
            )
            item_id = int(cursor.lastrowid)
            connection.executemany(
                'INSERT INTO media_library_parts('
                'item_id,part_index,recording_part_id,original_filename,'
                'storage_path,expected_size,received_size,state) '
                "VALUES(?,?,?,?,?,?,?,'pending')",
                (
                    (item_id, *part_values, part_values[-1])
                    for part_values in library_parts
                ),
            )
            connection.executemany(
                'INSERT INTO media_library_file_moves('
                'item_id,source_path,target_path,state,created_at,updated_at) '
                "VALUES(?,?,?,'pending',?,?)",
                (
                    (item_id, source_path, str(target_path), now, now)
                    for source_path, target_path in moves.items()
                ),
            )
            self._management_audit(
                connection,
                manager_subject=manager_subject,
                action='favorite_recording_session',
                target_type='media_library_item',
                target_id=item_id,
                old_state='recording',
                new_state='moving',
                reason='管理员收藏完整直播',
                now=now,
            )
            return item_id

        item_id = await self._database.write(prepare)
        await self._resume_moves(item_id)
        return await self.get_item(item_id)

    async def recover_interrupted(self) -> int:
        interrupted = await self._database.fetchall(
            'SELECT item.id FROM media_library_items item '
            'JOIN recording_sessions session ON session.id=item.session_id '
            "WHERE item.origin='recording' AND item.state IN ('moving','failed') "
            "AND session.deletion_state='none' ORDER BY item.id"
        )
        recovered = 0
        for row in interrupted:
            try:
                await self._resume_moves(int(row['id']))
            except MediaLibraryConflict:
                continue
            recovered += 1
        await self._database.execute(
            "UPDATE media_library_parts SET state='failed',"
            "error='上传被应用重启中断' WHERE state='uploading'"
        )
        await self._database.execute(
            "UPDATE media_library_items SET state='uploading',error=NULL,"
            'updated_at=? '
            "WHERE origin='upload' AND state='moving'",
            (int(self._clock()),),
        )
        return recovered

    async def create_import(
        self,
        *,
        kind: str,
        display_name: str,
        parts: Sequence[ImportPartRequest],
        manager_subject: str,
        note: str = '',
        tags: Sequence[str] = (),
        room_id: int = 0,
        anchor_name: str = '',
    ) -> MediaLibraryItem:
        if not manager_subject:
            raise MediaLibraryConflict('管理员身份不能为空')
        if kind not in ('broadcast', 'clip'):
            raise MediaLibraryConflict('媒体类型无效')
        name = self._display_name(display_name)
        normalized_note = self._note(note)
        normalized_tags = self._tags(tags)
        if not parts or len(parts) > 100:
            raise MediaLibraryConflict('一次导入必须包含 1 到 100 个分 P')
        if kind == 'clip' and len(parts) != 1:
            raise MediaLibraryConflict('外部片段只能包含一个视频文件')
        normalized_parts = []
        for part in parts:
            filename = str(part.filename).strip()
            if not filename or len(filename) > 512:
                raise MediaLibraryConflict('上传文件名长度无效')
            suffix = Path(filename).suffix.lower()
            if suffix not in _VIDEO_SUFFIXES:
                raise MediaLibraryConflict('上传文件格式不受支持')
            size = int(part.size_bytes)
            if size <= 0:
                raise MediaLibraryConflict('上传文件大小必须大于零')
            normalized_parts.append((filename, suffix, size))
        if room_id < 0 or room_id >= _EXTERNAL_IMPORT_ROOM_ID:
            raise MediaLibraryConflict('来源房间号超出有效范围')
        stored_room_id = room_id or _EXTERNAL_IMPORT_ROOM_ID
        key = await self._allocate_storage_key()
        directory = self._storage_root / key
        incoming = directory / 'incoming'
        await self._run_io(self._make_import_directories, directory, incoming)
        now = int(self._clock())

        def create(connection: sqlite3.Connection) -> int:
            cursor = connection.execute(
                'INSERT INTO recording_sessions('
                'room_id,broadcast_session_key,state,started_at,title,anchor_name,'
                'upload_intent,upload_decision,upload_resolution_state,source_kind) '
                "VALUES(?,?,'manual_review',?,?,?,'skip','skip',"
                "'not_requested','live')",
                (
                    int(stored_room_id),
                    'import:{}'.format(key),
                    now,
                    name,
                    anchor_name.strip()[:200],
                ),
            )
            session_id = int(cursor.lastrowid)
            run_id = 'import:{}'.format(key)
            connection.execute(
                'INSERT INTO recording_runs('
                'id,session_id,state,started_at,ended_at) '
                "VALUES(?,?,'finished',?,?)",
                (run_id, session_id, now, now),
            )
            item_cursor = connection.execute(
                'INSERT INTO media_library_items('
                'session_id,kind,origin,storage_key,display_name,note,state,'
                'created_at,updated_at) '
                "VALUES(?,?, 'upload',?,?,?,'uploading',?,?)",
                (session_id, kind, key, name, normalized_note, now, now),
            )
            item_id = int(item_cursor.lastrowid)
            connection.executemany(
                'INSERT INTO media_library_parts('
                'item_id,part_index,original_filename,storage_path,staging_path,'
                'expected_size,received_size,state) '
                "VALUES(?,?,?,?,?,?,0,'pending')",
                (
                    (
                        item_id,
                        index,
                        filename,
                        str(directory / 'part-{:04d}{}'.format(index, suffix)),
                        str(incoming / 'part-{:04d}{}'.format(index, suffix)),
                        size,
                    )
                    for index, (filename, suffix, size) in enumerate(
                        normalized_parts, start=1
                    )
                ),
            )
            self._replace_tags(connection, item_id, normalized_tags)
            self._management_audit(
                connection,
                manager_subject=manager_subject,
                action='create_media_import',
                target_type='media_library_item',
                target_id=item_id,
                old_state=None,
                new_state='uploading',
                reason='管理员创建外部媒体导入',
                now=now,
            )
            return item_id

        try:
            item_id = await self._database.write(create)
        except BaseException:
            await self._run_io(
                self._remove_empty_import_directories, directory, incoming
            )
            raise
        return await self.get_item(item_id)

    async def upload_part(
        self, item_id: int, part_index: int, chunks: AsyncIterable[bytes]
    ) -> MediaLibraryPart:
        lock_key = (int(item_id), int(part_index))
        lock = self._import_locks.setdefault(lock_key, asyncio.Lock())
        async with lock:
            row = await self._database.fetchone(
                'SELECT part.*,item.origin,item.state AS item_state,item.storage_key,'
                'session.deletion_state '
                'FROM media_library_parts part '
                'JOIN media_library_items item ON item.id=part.item_id '
                'JOIN recording_sessions session ON session.id=item.session_id '
                'WHERE part.item_id=? AND part.part_index=?',
                (int(item_id), int(part_index)),
            )
            if row is None:
                raise MediaLibraryNotFound('导入分 P 不存在')
            if str(row['origin']) != 'upload' or str(row['item_state']) != 'uploading':
                raise MediaLibraryConflict('该媒体库条目当前不能接收文件')
            if str(row['deletion_state']) != 'none':
                raise MediaLibraryConflict('该媒体库条目正在删除')
            if str(row['state']) == 'uploaded':
                return self._part_from_row(row)
            if str(row['state']) not in ('pending', 'failed'):
                raise MediaLibraryConflict('该分 P 正在上传')
            staging = self._import_path(
                str(row['staging_path']), str(row['storage_key'])
            )
            storage = self._import_path(
                str(row['storage_path']), str(row['storage_key'])
            )
            expected_size = int(row['expected_size'])
            changed = await self._database.execute(
                "UPDATE media_library_parts SET state='uploading',error=NULL,"
                'received_size=0 WHERE item_id=? AND part_index=? '
                "AND state IN ('pending','failed') AND EXISTS("
                'SELECT 1 FROM media_library_items item '
                'JOIN recording_sessions session ON session.id=item.session_id '
                'WHERE item.id=media_library_parts.item_id '
                "AND item.state='uploading' AND session.deletion_state='none')",
                (int(item_id), int(part_index)),
            )
            if changed != 1:
                raise MediaLibraryConflict('该媒体库条目正在删除或状态已经变化')
            try:
                received = await self._write_upload(staging, chunks, expected_size)
                await self._run_io(
                    self._replace_import_file, staging, storage, expected_size
                )
            except BaseException as error:
                await self._run_io(self._unlink_if_present, staging)
                await self._mark_part_failed(item_id, part_index, str(error))
                if isinstance(error, (asyncio.CancelledError, KeyboardInterrupt)):
                    raise
                if isinstance(error, MediaLibraryConflict):
                    raise
                raise MediaLibraryConflict('上传分 P 失败：{}'.format(error)) from None
            await self._database.execute(
                "UPDATE media_library_parts SET state='uploaded',error=NULL,"
                'received_size=? WHERE item_id=? AND part_index=? '
                "AND state='uploading'",
                (received, int(item_id), int(part_index)),
            )
            item = await self.get_item(item_id)
            return next(part for part in item.parts if part.part_index == part_index)

    async def complete_import(
        self, item_id: int, *, manager_subject: str
    ) -> MediaLibraryItem:
        if not manager_subject:
            raise MediaLibraryConflict('管理员身份不能为空')
        operation_started_at = int(self._clock())

        def begin(
            connection: sqlite3.Connection,
        ) -> Optional[Tuple[sqlite3.Row, Tuple[sqlite3.Row, ...]]]:
            item_row = connection.execute(
                'SELECT item.*,session.started_at,session.deletion_state '
                'FROM media_library_items item '
                'JOIN recording_sessions session ON session.id=item.session_id '
                'WHERE item.id=?',
                (int(item_id),),
            ).fetchone()
            if item_row is None:
                raise MediaLibraryNotFound('媒体库条目不存在')
            if str(item_row['origin']) != 'upload':
                raise MediaLibraryConflict('只有外部导入可以执行完成操作')
            if str(item_row['state']) == 'ready':
                return None
            if str(item_row['state']) != 'uploading':
                raise MediaLibraryConflict('该导入当前不能完成')
            if str(item_row['deletion_state']) != 'none':
                raise MediaLibraryConflict('该媒体库条目正在删除')
            part_rows = tuple(
                connection.execute(
                    'SELECT * FROM media_library_parts WHERE item_id=? '
                    'ORDER BY part_index',
                    (int(item_id),),
                ).fetchall()
            )
            if not part_rows or any(
                str(row['state']) != 'uploaded' for row in part_rows
            ):
                raise MediaLibraryConflict('请先上传全部分 P')
            connection.execute(
                "UPDATE media_library_items SET state='moving',error=NULL,"
                'updated_at=? WHERE id=?',
                (operation_started_at, int(item_id)),
            )
            return item_row, part_rows

        prepared = await self._database.write(begin)
        if prepared is None:
            return await self.get_item(item_id)
        item_row, part_rows = prepared
        artifacts = []
        for row in part_rows:
            part_index = int(row['part_index'])
            path = self._import_path(
                str(row['storage_path']), str(item_row['storage_key'])
            )
            try:
                artifact = await self._run_io(self._artifact_probe, str(path))
            except (asyncio.CancelledError, KeyboardInterrupt):
                raise
            except BaseException as error:
                await self._fail_import_completion(
                    int(item_id),
                    part_index,
                    '{}: {}'.format(type(error).__name__, error),
                )
                raise MediaLibraryConflict(
                    '第 {} 个分 P 视频探测失败，请重新上传'.format(part_index)
                ) from None
            if artifact is None or artifact.size_bytes != int(row['expected_size']):
                await self._fail_import_completion(
                    int(item_id), part_index, '视频文件无法识别'
                )
                raise MediaLibraryConflict(
                    '第 {} 个分 P 视频文件无法识别，请重新上传'.format(part_index)
                )
            artifacts.append(artifact)
        now = int(self._clock())

        def complete(connection: sqlite3.Connection) -> None:
            current = connection.execute(
                'SELECT session_id,state,origin,display_name FROM '
                'media_library_items WHERE id=?',
                (int(item_id),),
            ).fetchone()
            if current is None:
                raise MediaLibraryNotFound('媒体库条目不存在')
            if str(current['origin']) != 'upload' or str(current['state']) != 'moving':
                raise MediaLibraryConflict('导入状态已经发生变化')
            current_parts = connection.execute(
                'SELECT * FROM media_library_parts WHERE item_id=? '
                'ORDER BY part_index',
                (int(item_id),),
            ).fetchall()
            if len(current_parts) != len(artifacts) or any(
                str(row['state']) != 'uploaded' for row in current_parts
            ):
                raise MediaLibraryConflict('导入分 P 状态已经发生变化')
            session_id = int(current['session_id'])
            session = connection.execute(
                'SELECT started_at FROM recording_sessions WHERE id=?', (session_id,)
            ).fetchone()
            assert session is not None
            base = int(session['started_at'])
            elapsed = 0
            run_id = 'import:{}'.format(str(item_row['storage_key']))
            for row, artifact in zip(current_parts, artifacts):
                duration = max(1, int(artifact.duration_seconds or 1))
                start = base + elapsed
                cursor = connection.execute(
                    'INSERT INTO recording_parts('
                    'session_id,run_id,part_index,source_path,final_path,'
                    'record_start_time,record_end_time,record_duration_seconds,'
                    'file_size_bytes,danmaku_count,artifact_state,xml_completed,'
                    'created_at,updated_at,timeline_start_at_ms,media_index_state) '
                    "VALUES(?,?,?,?,?,?,?,?,?,0,'ready',0,?,?,?,'pending')",
                    (
                        session_id,
                        run_id,
                        int(row['part_index']),
                        str(row['storage_path']),
                        str(row['storage_path']),
                        start,
                        start + duration,
                        duration,
                        int(artifact.size_bytes),
                        now,
                        now,
                        start * 1000,
                    ),
                )
                connection.execute(
                    "UPDATE media_library_parts SET recording_part_id=?,"
                    "state='ready',error=NULL,staging_path=NULL "
                    'WHERE item_id=? AND part_index=?',
                    (int(cursor.lastrowid), int(item_id), int(row['part_index'])),
                )
                elapsed += duration
            connection.execute(
                "UPDATE recording_sessions SET state='closed',ended_at=?,"
                'live_end_time=?,title=?,upload_intent=\'skip\','
                "upload_decision='skip',upload_resolution_state='not_requested',"
                'upload_resolution_error=NULL,upload_resolved_at=? WHERE id=?',
                (
                    base + elapsed,
                    base + elapsed,
                    str(current['display_name']),
                    now,
                    session_id,
                ),
            )
            connection.execute(
                "UPDATE media_library_items SET state='ready',error=NULL,"
                'updated_at=? WHERE id=?',
                (now, int(item_id)),
            )
            self._management_audit(
                connection,
                manager_subject=manager_subject,
                action='complete_media_import',
                target_type='media_library_item',
                target_id=int(item_id),
                old_state='uploading',
                new_state='ready',
                reason='管理员完成外部媒体导入',
                now=now,
            )

        await self._database.write(complete)
        audit(
            'media_library_import_completed',
            item_id=int(item_id),
            part_count=len(artifacts),
            result='ready',
        )
        return await self.get_item(item_id)

    async def get_item(self, item_id: int) -> MediaLibraryItem:
        row = await self._database.fetchone(
            'SELECT item.*,session.room_id,session.title,session.anchor_name,'
            'session.started_at FROM media_library_items item '
            'JOIN recording_sessions session ON session.id=item.session_id '
            'WHERE item.id=?',
            (int(item_id),),
        )
        if row is None:
            raise MediaLibraryNotFound('媒体库条目不存在')
        return self._item_from_row(
            row, await self._tags_for_item(item_id), await self._parts_for_item(item_id)
        )

    async def list_items(
        self,
        *,
        kind: Optional[str] = None,
        limit: int = 20,
        offset: int = 0,
        query: str = '',
    ) -> Tuple[int, Tuple[MediaLibraryItem, ...]]:
        if kind is not None and kind not in ('broadcast', 'clip'):
            raise ValueError('media library kind is invalid')
        if limit < 1 or limit > 100:
            raise ValueError('media library limit must be between 1 and 100')
        if offset < 0:
            raise ValueError('media library offset must not be negative')
        clauses = []
        parameters: List[object] = []
        if kind is not None:
            clauses.append('item.kind=?')
            parameters.append(kind)
        normalized_query = query.strip()
        if normalized_query:
            escaped = (
                normalized_query.replace('\\', '\\\\')
                .replace('%', '\\%')
                .replace('_', '\\_')
            )
            pattern = '%{}%'.format(escaped)
            clauses.append(
                '(item.display_name LIKE ? ESCAPE \'\\\' '
                'OR session.title LIKE ? ESCAPE \'\\\' '
                'OR session.anchor_name LIKE ? ESCAPE \'\\\' '
                'OR EXISTS(SELECT 1 FROM media_library_item_tags link '
                'JOIN media_library_tags tag ON tag.id=link.tag_id '
                'WHERE link.item_id=item.id AND tag.name LIKE ? ESCAPE \'\\\'))'
            )
            parameters.extend((pattern,) * 4)
        where = '' if not clauses else 'WHERE ' + ' AND '.join(clauses)
        total = int(
            await self._database.scalar(
                'SELECT COUNT(*) FROM media_library_items item '
                'JOIN recording_sessions session ON session.id=item.session_id '
                + where,
                tuple(parameters),
            )
        )
        rows = await self._database.fetchall(
            'SELECT item.*,session.room_id,session.title,session.anchor_name,'
            'session.started_at FROM media_library_items item '
            'JOIN recording_sessions session ON session.id=item.session_id '
            + where
            + ' ORDER BY item.created_at DESC,item.id DESC LIMIT ? OFFSET ?',
            (*parameters, limit, offset),
        )
        item_ids = tuple(int(row['id']) for row in rows)
        tags_by_item = await self._tags_for_items(item_ids)
        parts_by_item = await self._parts_for_items(item_ids)
        items = []
        for row in rows:
            item_id = int(row['id'])
            items.append(
                self._item_from_row(
                    row, tags_by_item.get(item_id, ()), parts_by_item.get(item_id, ())
                )
            )
        return total, tuple(items)

    async def update_item(
        self,
        item_id: int,
        *,
        manager_subject: str,
        display_name: Optional[str] = None,
        note: Optional[str] = None,
        tags: Optional[Sequence[str]] = None,
    ) -> MediaLibraryItem:
        if not manager_subject:
            raise MediaLibraryConflict('管理员身份不能为空')
        normalized_name = (
            None if display_name is None else self._display_name(display_name)
        )
        normalized_note = None if note is None else self._note(note)
        normalized_tags = None if tags is None else self._tags(tags)
        if (
            normalized_name is None
            and normalized_note is None
            and normalized_tags is None
        ):
            return await self.get_item(item_id)
        now = int(self._clock())

        def update(connection: sqlite3.Connection) -> None:
            current = connection.execute(
                'SELECT display_name,note,state FROM media_library_items WHERE id=?',
                (int(item_id),),
            ).fetchone()
            if current is None:
                raise MediaLibraryNotFound('媒体库条目不存在')
            name_value = (
                str(current['display_name'])
                if normalized_name is None
                else normalized_name
            )
            note_value = (
                str(current['note']) if normalized_note is None else normalized_note
            )
            connection.execute(
                'UPDATE media_library_items SET display_name=?,note=?,updated_at=? '
                'WHERE id=?',
                (name_value, note_value, now, int(item_id)),
            )
            if normalized_tags is not None:
                self._replace_tags(connection, int(item_id), normalized_tags)
            self._management_audit(
                connection,
                manager_subject=manager_subject,
                action='update_media_library_item',
                target_type='media_library_item',
                target_id=int(item_id),
                old_state=str(current['state']),
                new_state=str(current['state']),
                reason='管理员更新媒体库展示信息',
                now=now,
            )

        await self._database.write(update)
        return await self.get_item(item_id)

    async def submission_history(
        self, item_id: int
    ) -> Tuple[SubmissionHistoryEntry, ...]:
        item = await self._database.fetchone(
            'SELECT session_id FROM media_library_items WHERE id=?', (int(item_id),)
        )
        if item is None:
            raise MediaLibraryNotFound('媒体库条目不存在')
        return (await self.submission_histories((int(item_id),)))[int(item_id)]

    async def submission_histories(
        self, item_ids: Sequence[int]
    ) -> Dict[int, Tuple[SubmissionHistoryEntry, ...]]:
        normalized_ids = tuple(dict.fromkeys(int(item_id) for item_id in item_ids))
        if not normalized_ids:
            return {}
        placeholders = ','.join('?' for _ in normalized_ids)
        rows = await self._database.fetchall(
            'SELECT item.id AS item_id,job.account_id,account.display_name,'
            'job.aid,job.bvid,job.state,'
            'COALESCE(job.approved_at,job.submitted_at,job.updated_at) '
            'AS occurred_at,1 AS is_current '
            'FROM media_library_items item '
            'JOIN upload_jobs job ON job.session_id=item.session_id '
            'JOIN bili_accounts account ON account.id=job.account_id '
            'WHERE item.id IN ({0}) AND job.aid IS NOT NULL '
            "AND COALESCE(job.bvid,'')!='' UNION ALL "
            'SELECT item.id AS item_id,archive.account_id,account.display_name,'
            'archive.aid,archive.bvid,archive.state,'
            'archive.archived_at AS occurred_at,0 AS is_current '
            'FROM media_library_items item '
            'JOIN upload_job_archives archive '
            'ON archive.session_id=item.session_id '
            'JOIN bili_accounts account ON account.id=archive.account_id '
            'WHERE item.id IN ({0}) AND archive.aid IS NOT NULL '
            "AND COALESCE(archive.bvid,'')!='' "
            'ORDER BY item_id,is_current DESC,occurred_at DESC'.format(placeholders),
            (*normalized_ids, *normalized_ids),
        )
        collected: Dict[int, List[SubmissionHistoryEntry]] = {
            item_id: [] for item_id in normalized_ids
        }
        for row in rows:
            collected[int(row['item_id'])].append(
                self._history_from_row(row, current=bool(row['is_current']))
            )
        return {item_id: tuple(values) for item_id, values in collected.items()}

    async def _resume_moves(self, item_id: int) -> None:
        async with self._move_lock:
            row = await self._database.fetchone(
                'SELECT id,state,origin FROM media_library_items WHERE id=?',
                (int(item_id),),
            )
            if row is None:
                raise MediaLibraryNotFound('媒体库条目不存在')
            if str(row['state']) == 'ready':
                return
            if str(row['origin']) != 'recording':
                raise MediaLibraryConflict('该媒体库条目不是收藏直播')
            await self._database.execute(
                "UPDATE media_library_items SET state='moving',error=NULL,updated_at=? "
                'WHERE id=?',
                (int(self._clock()), int(item_id)),
            )
            moves = await self._database.fetchall(
                'SELECT id FROM media_library_file_moves WHERE item_id=? '
                "AND state!='ready' ORDER BY id",
                (int(item_id),),
            )
            for move in moves:
                move_id = int(move['id'])
                try:
                    await self._complete_move(move_id)
                except BaseException as error:
                    message = '{}: {}'.format(type(error).__name__, error)[:1000]
                    await self._database.execute(
                        "UPDATE media_library_file_moves SET state='failed',error=?,"
                        'updated_at=? WHERE id=?',
                        (message, int(self._clock()), move_id),
                    )
                    await self._database.execute(
                        "UPDATE media_library_items SET state='failed',error=?,"
                        'updated_at=? WHERE id=?',
                        (message, int(self._clock()), int(item_id)),
                    )
                    if isinstance(error, (asyncio.CancelledError, KeyboardInterrupt)):
                        raise
                    raise MediaLibraryConflict(
                        '收藏文件移动失败，可稍后重试：{}'.format(error)
                    ) from None

            def finish(connection: sqlite3.Connection) -> None:
                remaining = connection.execute(
                    'SELECT COUNT(*) FROM media_library_file_moves '
                    "WHERE item_id=? AND state!='ready'",
                    (int(item_id),),
                ).fetchone()
                assert remaining is not None
                if int(remaining[0]) != 0:
                    raise MediaLibraryConflict('收藏文件尚未全部移动')
                now = int(self._clock())
                connection.execute(
                    "UPDATE media_library_parts SET state='ready',error=NULL "
                    'WHERE item_id=?',
                    (int(item_id),),
                )
                connection.execute(
                    "UPDATE media_library_items SET state='ready',error=NULL,"
                    'updated_at=? WHERE id=?',
                    (now, int(item_id)),
                )

            await self._database.write(finish)
            audit('recording_session_favorited', item_id=int(item_id), result='ready')

    async def _complete_move(self, move_id: int) -> None:
        row = await self._database.fetchone(
            'SELECT move.*,item.storage_key FROM media_library_file_moves move '
            'JOIN media_library_items item ON item.id=move.item_id '
            'WHERE move.id=?',
            (int(move_id),),
        )
        if row is None or str(row['state']) == 'ready':
            return
        source, target = self._move_paths(row)
        operation = asyncio.create_task(
            self._run_io(self._move_file, int(move_id), source, target)
        )
        try:
            identity = await asyncio.shield(operation)
        except asyncio.CancelledError:
            try:
                await operation
            except BaseException:
                pass
            raise

        def finish(connection: sqlite3.Connection) -> None:
            self._complete_move_in_connection(connection, move_id, identity)

        await self._database.write(finish)

    def _complete_move_in_connection(
        self, connection: sqlite3.Connection, move_id: int, identity: Optional[str]
    ) -> None:
        row = connection.execute(
            'SELECT move.*,item.storage_key FROM media_library_file_moves move '
            'JOIN media_library_items item ON item.id=move.item_id '
            'WHERE move.id=?',
            (int(move_id),),
        ).fetchone()
        if row is None or str(row['state']) == 'ready':
            return
        source, target = self._move_paths(row)
        if self._is_regular_file(source) or not self._is_regular_file(target):
            raise MediaLibraryConflict('收藏文件移动尚未完成')
        old, new = str(source), str(target)
        if identity is not None:
            connection.execute(
                'UPDATE upload_parts SET file_identity=? '
                'WHERE final_path=? OR (final_path IS NULL AND source_path=?)',
                (identity, old, old),
            )
            connection.execute(
                'UPDATE upload_parts SET repair_original_identity=? '
                'WHERE repair_original_path=?',
                (identity, old),
            )
        for table, columns in (
            ('recording_parts', ('source_path', 'final_path', 'xml_path')),
            (
                'upload_parts',
                (
                    'source_path',
                    'final_path',
                    'xml_path',
                    'repair_temp_path',
                    'repair_original_path',
                ),
            ),
        ):
            for column in columns:
                connection.execute(
                    'UPDATE {} SET {}=? WHERE {}=?'.format(table, column, column),
                    (new, old),
                )
        connection.execute(
            'UPDATE recording_sessions SET cover_path=? WHERE cover_path=?', (new, old)
        )
        connection.execute('UPDATE event_journal SET path=? WHERE path=?', (new, old))
        connection.execute(
            'UPDATE highlight_clips SET inspection_json=NULL,'
            'source_fingerprint_json=NULL WHERE id IN('
            'SELECT source.clip_id FROM highlight_clip_sources source '
            'JOIN recording_parts part ON part.id=source.part_id '
            'JOIN media_library_items item ON item.session_id=part.session_id '
            'WHERE item.id=?)',
            (int(row['item_id']),),
        )
        connection.execute(
            "UPDATE media_library_file_moves SET state='ready',error=NULL,"
            'updated_at=? WHERE id=?',
            (int(self._clock()), int(move_id)),
        )

    def _move_paths(self, row: sqlite3.Row) -> Tuple[Path, Path]:
        source = self._owned_path(
            str(row['source_path']), _OWNED_SUFFIXES, self._recording_root
        )
        expected_directory = self._storage_root / str(row['storage_key'])
        target = self._owned_path(
            str(row['target_path']), _OWNED_SUFFIXES, expected_directory
        )
        return source, target

    def _move_file(self, move_id: int, source: Path, target: Path) -> Optional[str]:
        target.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        source_exists = self._is_regular_file(source)
        target_exists = self._is_regular_file(target)
        staging = media_library_move_staging_path(target, move_id)
        if source_exists and target_exists:
            if not self._files_equal(source, target):
                raise MediaLibraryConflict('收藏目标文件已经存在')
            self._unlink_file(str(source))
            self._unlink_regular_file_if_present(staging)
        elif target_exists:
            self._unlink_regular_file_if_present(staging)
        elif not source_exists:
            raise MediaLibraryConflict('收藏源文件和目标文件都不存在')
        else:
            self._unlink_regular_file_if_present(staging)
            try:
                self._replace_file(str(source), str(target))
            except OSError as error:
                if error.errno != errno.EXDEV:
                    raise
                self._copy_across_filesystems(source, target, staging)
                self._unlink_file(str(source))
        os.chmod(str(target), 0o600)
        return self._file_identity(target)

    @staticmethod
    def _file_identity(path: Path) -> Optional[str]:
        if path.suffix.lower() not in _VIDEO_SUFFIXES:
            return None
        return FileIdentity.from_path(str(path)).to_json()

    def _copy_across_filesystems(
        self, source: Path, target: Path, staging: Path
    ) -> None:
        flags = os.O_CREAT | os.O_TRUNC | os.O_WRONLY
        if hasattr(os, 'O_NOFOLLOW'):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(str(staging), flags, 0o600)
        try:
            with open(source, 'rb') as source_file, os.fdopen(
                descriptor, 'wb'
            ) as target_file:
                descriptor = -1
                source_before = os.fstat(source_file.fileno())
                shutil.copyfileobj(
                    source_file, target_file, length=_MOVE_COPY_BUFFER_BYTES
                )
                target_file.flush()
                os.fsync(target_file.fileno())
                source_after = os.fstat(source_file.fileno())
                target_stat = os.fstat(target_file.fileno())
                source_identity = (
                    source_before.st_dev,
                    source_before.st_ino,
                    source_before.st_size,
                    source_before.st_mtime_ns,
                )
                if (
                    source_identity
                    != (
                        source_after.st_dev,
                        source_after.st_ino,
                        source_after.st_size,
                        source_after.st_mtime_ns,
                    )
                    or source_file.tell() != source_before.st_size
                    or target_stat.st_size != source_before.st_size
                ):
                    raise MediaLibraryConflict('收藏源文件在复制期间发生变化')
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        self._replace_file(str(staging), str(target))

    @staticmethod
    def _files_equal(left: Path, right: Path) -> bool:
        if left.stat().st_size != right.stat().st_size:
            return False
        with open(left, 'rb') as left_file, open(right, 'rb') as right_file:
            while True:
                left_chunk = left_file.read(_MOVE_COPY_BUFFER_BYTES)
                right_chunk = right_file.read(_MOVE_COPY_BUFFER_BYTES)
                if left_chunk != right_chunk:
                    return False
                if not left_chunk:
                    return True

    @staticmethod
    def _unlink_regular_file_if_present(path: Path) -> None:
        try:
            result = path.lstat()
        except FileNotFoundError:
            return
        if not stat.S_ISREG(result.st_mode):
            raise MediaLibraryConflict('收藏临时文件类型无效')
        path.unlink()

    def _append_upload_file_moves(
        self,
        connection: sqlite3.Connection,
        session_id: int,
        target_directory: Path,
        moves: Dict[str, Path],
    ) -> None:
        rows = connection.execute(
            'SELECT part.id,part.part_index,part.source_path,part.final_path,'
            'part.xml_path,part.repair_temp_path,part.repair_original_path '
            'FROM upload_parts part JOIN upload_jobs job ON job.id=part.job_id '
            'WHERE job.session_id=? ORDER BY part.part_index,part.id',
            (int(session_id),),
        ).fetchall()
        for row in rows:
            for role, column in enumerate(
                (
                    'source_path',
                    'final_path',
                    'xml_path',
                    'repair_temp_path',
                    'repair_original_path',
                ),
                start=1,
            ):
                path = self._existing_owned_file(
                    row[column], _OWNED_SUFFIXES, required=False
                )
                if path is None or str(path) in moves:
                    continue
                moves[str(path)] = target_directory / (
                    'part-{:04d}-upload-{}-{}{}'.format(
                        int(row['part_index']),
                        int(row['id']),
                        role,
                        path.suffix.lower(),
                    )
                )

    async def _parts_for_item(self, item_id: int) -> Tuple[MediaLibraryPart, ...]:
        return (await self._parts_for_items((int(item_id),))).get(int(item_id), ())

    async def _parts_for_items(
        self, item_ids: Sequence[int]
    ) -> Dict[int, Tuple[MediaLibraryPart, ...]]:
        normalized_ids = tuple(dict.fromkeys(int(item_id) for item_id in item_ids))
        if not normalized_ids:
            return {}
        placeholders = ','.join('?' for _ in normalized_ids)
        rows = await self._database.fetchall(
            'SELECT part.*,recording.record_duration_seconds '
            'FROM media_library_parts part '
            'LEFT JOIN recording_parts recording '
            'ON recording.id=part.recording_part_id '
            'WHERE part.item_id IN ({}) '
            'ORDER BY part.item_id,part.part_index'.format(placeholders),
            normalized_ids,
        )
        collected: Dict[int, List[MediaLibraryPart]] = {
            item_id: [] for item_id in normalized_ids
        }
        for row in rows:
            collected[int(row['item_id'])].append(self._part_from_row(row))
        return {item_id: tuple(values) for item_id, values in collected.items()}

    async def _tags_for_item(self, item_id: int) -> Tuple[str, ...]:
        return (await self._tags_for_items((int(item_id),))).get(int(item_id), ())

    async def _tags_for_items(
        self, item_ids: Sequence[int]
    ) -> Dict[int, Tuple[str, ...]]:
        normalized_ids = tuple(dict.fromkeys(int(item_id) for item_id in item_ids))
        if not normalized_ids:
            return {}
        placeholders = ','.join('?' for _ in normalized_ids)
        rows = await self._database.fetchall(
            'SELECT link.item_id,tag.name FROM media_library_item_tags link '
            'JOIN media_library_tags tag ON tag.id=link.tag_id '
            'WHERE link.item_id IN ({}) '
            'ORDER BY link.item_id,tag.id'.format(placeholders),
            normalized_ids,
        )
        collected: Dict[int, List[str]] = {item_id: [] for item_id in normalized_ids}
        for row in rows:
            collected[int(row['item_id'])].append(str(row['name']))
        return {item_id: tuple(values) for item_id, values in collected.items()}

    @staticmethod
    def _part_from_row(row: sqlite3.Row) -> MediaLibraryPart:
        return MediaLibraryPart(
            item_id=int(row['item_id']),
            part_index=int(row['part_index']),
            recording_part_id=(
                None
                if row['recording_part_id'] is None
                else int(row['recording_part_id'])
            ),
            original_filename=str(row['original_filename']),
            storage_path=str(row['storage_path']),
            expected_size=int(row['expected_size']),
            received_size=int(row['received_size']),
            state=str(row['state']),
            error=None if row['error'] is None else str(row['error']),
            duration_seconds=(
                None
                if 'record_duration_seconds' not in row.keys()
                or row['record_duration_seconds'] is None
                else int(row['record_duration_seconds'])
            ),
        )

    @staticmethod
    def _item_from_row(
        row: sqlite3.Row, tags: Tuple[str, ...], parts: Tuple[MediaLibraryPart, ...]
    ) -> MediaLibraryItem:
        return MediaLibraryItem(
            id=int(row['id']),
            session_id=int(row['session_id']),
            kind=str(row['kind']),
            origin=str(row['origin']),
            storage_key=str(row['storage_key']),
            display_name=str(row['display_name']),
            note=str(row['note']),
            state=str(row['state']),
            error=None if row['error'] is None else str(row['error']),
            created_at=int(row['created_at']),
            updated_at=int(row['updated_at']),
            room_id=(
                0
                if str(row['origin']) == 'upload'
                and int(row['room_id']) == _EXTERNAL_IMPORT_ROOM_ID
                else int(row['room_id'])
            ),
            title=str(row['title']),
            anchor_name=str(row['anchor_name']),
            started_at=int(row['started_at']),
            tags=tags,
            parts=parts,
        )

    @staticmethod
    def _history_from_row(row: sqlite3.Row, *, current: bool) -> SubmissionHistoryEntry:
        return SubmissionHistoryEntry(
            aid=int(row['aid']),
            bvid=str(row['bvid']),
            state=str(row['state']),
            account_id=int(row['account_id']),
            account_name=str(row['display_name']),
            occurred_at=int(row['occurred_at']),
            current=current,
        )

    def _allocate_storage_key_in_connection(
        self, connection: sqlite3.Connection
    ) -> str:
        for _attempt in range(16):
            key = self._storage_key()
            exists = connection.execute(
                'SELECT 1 FROM media_library_items WHERE storage_key=?', (key,)
            ).fetchone()
            if exists is None and not (self._storage_root / key).exists():
                return key
        raise MediaLibraryConflict('无法分配媒体库存储目录')

    async def _allocate_storage_key(self) -> str:
        for _attempt in range(16):
            key = self._storage_key()
            exists = await self._database.scalar(
                'SELECT 1 FROM media_library_items WHERE storage_key=?', (key,)
            )
            if exists is None and not (self._storage_root / key).exists():
                return key
        raise MediaLibraryConflict('无法分配媒体库存储目录')

    def _storage_key(self) -> str:
        key = str(self._storage_key_factory()).strip().lower()
        if len(key) != 32 or any(
            character not in '0123456789abcdef' for character in key
        ):
            raise MediaLibraryConflict('媒体库存储键无效')
        return key

    def _existing_owned_file(
        self, raw_path: object, suffixes: Iterable[str], *, required: bool = False
    ) -> Optional[Path]:
        if raw_path is None or not str(raw_path):
            if required:
                raise MediaLibraryConflict('媒体文件路径为空')
            return None
        path = self._owned_path(str(raw_path), suffixes, self._recording_root)
        if not self._is_regular_file(path):
            if required:
                raise MediaLibraryConflict('媒体文件不存在')
            return None
        return path

    def _owned_path(
        self, raw_path: str, suffixes: Iterable[str], expected_root: Path
    ) -> Path:
        path = Path(os.path.abspath(os.path.expanduser(raw_path)))
        resolved = path.resolve(strict=False)
        try:
            resolved.relative_to(expected_root)
        except ValueError:
            raise MediaLibraryConflict('媒体文件路径超出受管目录') from None
        if path.suffix.lower() not in suffixes:
            raise MediaLibraryConflict('媒体文件格式不受支持')
        return path

    def _import_path(self, raw_path: str, storage_key: str) -> Path:
        expected = (self._storage_root / storage_key).resolve()
        return self._owned_path(raw_path, _VIDEO_SUFFIXES, expected)

    @staticmethod
    def _is_regular_file(path: Path) -> bool:
        try:
            result = path.lstat()
        except OSError:
            return False
        return stat.S_ISREG(result.st_mode)

    async def _write_upload(
        self, path: Path, chunks: AsyncIterable[bytes], expected_size: int
    ) -> int:
        await self._run_io(path.parent.mkdir, parents=True, mode=0o700, exist_ok=True)
        descriptor = await self._run_io(self._open_upload_file, path)
        file = os.fdopen(descriptor, 'wb')
        received = 0
        try:
            async for chunk in chunks:
                if not isinstance(chunk, (bytes, bytearray, memoryview)):
                    raise MediaLibraryConflict('上传数据块类型无效')
                value = bytes(chunk)
                if not value:
                    continue
                received += len(value)
                if received > expected_size:
                    raise MediaLibraryConflict('上传内容超过声明大小')
                await self._run_io(file.write, value)
            if received != expected_size:
                raise MediaLibraryConflict('上传内容大小与声明不一致')
            await self._run_io(file.flush)
            await self._run_io(os.fsync, file.fileno())
        finally:
            await self._run_io(file.close)
        return received

    @staticmethod
    def _open_upload_file(path: Path) -> int:
        flags = os.O_CREAT | os.O_TRUNC | os.O_WRONLY
        if hasattr(os, 'O_NOFOLLOW'):
            flags |= os.O_NOFOLLOW
        return os.open(str(path), flags, 0o600)

    def _replace_import_file(
        self, staging: Path, storage: Path, expected_size: int
    ) -> None:
        if (
            not self._is_regular_file(staging)
            or staging.stat().st_size != expected_size
        ):
            raise MediaLibraryConflict('上传临时文件无效')
        storage.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        self._replace_file(str(staging), str(storage))
        os.chmod(str(storage), 0o600)

    async def _mark_part_failed(
        self, item_id: int, part_index: int, error: str
    ) -> None:
        await self._database.execute(
            "UPDATE media_library_parts SET state='failed',error=?,received_size=0 "
            'WHERE item_id=? AND part_index=?',
            (error[:1000] or '上传失败', int(item_id), int(part_index)),
        )

    async def _fail_import_completion(
        self, item_id: int, part_index: int, error: str
    ) -> None:
        message = error[:1000] or '视频探测失败'

        def fail(connection: sqlite3.Connection) -> None:
            connection.execute(
                "UPDATE media_library_parts SET state='failed',error=?,"
                'received_size=0 WHERE item_id=? AND part_index=?',
                (message, int(item_id), int(part_index)),
            )
            connection.execute(
                "UPDATE media_library_items SET state='uploading',error=NULL,"
                'updated_at=? WHERE id=? AND state=\'moving\'',
                (int(self._clock()), int(item_id)),
            )

        await self._database.write(fail)

    @staticmethod
    def _make_import_directories(directory: Path, incoming: Path) -> None:
        directory.mkdir(parents=True, mode=0o700, exist_ok=False)
        incoming.mkdir(mode=0o700)

    @staticmethod
    def _remove_empty_import_directories(directory: Path, incoming: Path) -> None:
        try:
            incoming.rmdir()
        except OSError:
            pass
        try:
            directory.rmdir()
        except OSError:
            pass

    @staticmethod
    def _unlink_if_present(path: Path) -> None:
        try:
            path.unlink()
        except FileNotFoundError:
            pass

    @staticmethod
    def _display_name(value: str) -> str:
        normalized = value.strip()
        if not normalized or len(normalized) > 200:
            raise MediaLibraryConflict('名称必须包含 1 到 200 个字符')
        return normalized

    @staticmethod
    def _note(value: str) -> str:
        if len(value) > 2000:
            raise MediaLibraryConflict('备注不能超过 2000 个字符')
        return value

    @staticmethod
    def _tags(values: Sequence[str]) -> Tuple[str, ...]:
        if len(values) > 20:
            raise MediaLibraryConflict('一个条目最多包含 20 个标签')
        result = []
        observed = set()
        for value in values:
            normalized = str(value).strip()
            if not normalized or len(normalized) > 40:
                raise MediaLibraryConflict('标签必须包含 1 到 40 个字符')
            key = normalized.casefold()
            if key in observed:
                continue
            observed.add(key)
            result.append(normalized)
        return tuple(result)

    @staticmethod
    def _replace_tags(
        connection: sqlite3.Connection, item_id: int, tags: Sequence[str]
    ) -> None:
        connection.execute(
            'DELETE FROM media_library_item_tags WHERE item_id=?', (int(item_id),)
        )
        for tag in tags:
            connection.execute(
                'INSERT INTO media_library_tags(name) VALUES(?) '
                'ON CONFLICT(name) DO NOTHING',
                (tag,),
            )
            row = connection.execute(
                'SELECT id FROM media_library_tags WHERE name=? COLLATE NOCASE', (tag,)
            ).fetchone()
            assert row is not None
            connection.execute(
                'INSERT INTO media_library_item_tags(item_id,tag_id) VALUES(?,?)',
                (int(item_id), int(row['id'])),
            )

    @staticmethod
    def _management_audit(
        connection: sqlite3.Connection,
        *,
        manager_subject: str,
        action: str,
        target_type: str,
        target_id: int,
        old_state: Optional[str],
        new_state: str,
        reason: str,
        now: int,
    ) -> None:
        connection.execute(
            'INSERT INTO management_audit('
            'manager_subject,action,target_type,target_id,old_state,new_state,'
            'reason,created_at) VALUES(?,?,?,?,?,?,?,?)',
            (
                manager_subject,
                action,
                target_type,
                str(target_id),
                old_state,
                new_state,
                reason,
                now,
            ),
        )

    @staticmethod
    async def _run_io(
        operation: Callable[..., _T], *args: object, **kwargs: object
    ) -> _T:
        loop = asyncio.get_running_loop()
        if kwargs:
            return await loop.run_in_executor(None, lambda: operation(*args, **kwargs))
        return await loop.run_in_executor(None, operation, *args)
