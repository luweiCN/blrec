from __future__ import annotations

import asyncio
import math
import os
import stat
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path
from typing import Any, BinaryIO, Iterator, Literal, Mapping, Optional, Tuple

from lxml import etree

from blrec.flv.common import (
    create_metadata_tag,
    ensure_order,
    is_metadata_tag,
    parse_metadata,
)
from blrec.flv.io import FlvReader, FlvWriter
from blrec.flv.models import FlvHeader

from .database import BiliUploadDatabase

__all__ = (
    'DanmakuLine',
    'DanmakuPage',
    'FlvMediaSnapshot',
    'MediaResource',
    'RecordingMediaCandidate',
    'RecordingMediaDescriptor',
    'RecordingContentInvalid',
    'RecordingContentCursorStale',
    'RecordingContentNotFound',
    'RecordingContentReader',
    'RecordingContentUnavailable',
)


class RecordingContentNotFound(RuntimeError):
    pass


class RecordingContentUnavailable(RuntimeError):
    pass


class RecordingContentInvalid(RuntimeError):
    pass


class RecordingContentCursorStale(RuntimeError):
    pass


@dataclass(frozen=True)
class MediaResource:
    path: Optional[str]
    size: Optional[int]
    content_type: Optional[str]
    recording: bool
    room_id: int
    part_index: int
    bvid: Optional[str]
    remote_available: bool
    playback_mode: Literal['seekable', 'sequential', 'active_snapshot']
    index_state: str
    source_device: Optional[int] = None
    source_inode: Optional[int] = None


@dataclass(frozen=True)
class RecordingMediaCandidate:
    path: str
    content_type: str
    recording: bool
    artifact_key: str


@dataclass(frozen=True)
class RecordingMediaDescriptor:
    part_id: int
    room_id: int
    part_index: int
    candidates: Tuple[RecordingMediaCandidate, ...]
    bvid: Optional[str]
    remote_available: bool
    index_state: str
    expected_root: Optional[str] = None


@dataclass(frozen=True)
class DanmakuLine:
    index: int
    progress_ms: int
    mode: int
    font_size: int
    color: int
    content: str
    user: Optional[str] = None
    uid: Optional[int] = None


@dataclass(frozen=True)
class DanmakuPage:
    items: Tuple[DanmakuLine, ...]
    next_cursor: Optional[int]


@dataclass
class _DanmakuStream:
    part_id: int
    path: str
    identity: Tuple[int, int, int]
    file: BinaryIO
    parser: Any
    observed_size: int
    read_offset: int
    next_cursor: int
    finalized: bool
    last_access: float
    ordinal: int = 0
    unreleased_input_bytes: int = 0
    parser_events_consumed: bool = False
    pending: Optional[DanmakuLine] = None
    parser_closed: bool = False
    closed: bool = False
    prefix: bytearray = field(default_factory=bytearray)
    lock: Any = field(default_factory=threading.Lock)


@dataclass(frozen=True)
class FlvMediaSnapshot:
    path: str
    source_size: int
    source_tail_start: int
    prefix: bytes
    duration_ms: Optional[int]
    source_device: Optional[int] = None
    source_inode: Optional[int] = None

    @property
    def size(self) -> int:
        return len(self.prefix) + self.source_size - self.source_tail_start

    @classmethod
    def create(
        cls, path: str, source_size: int, current_metadata: Mapping[str, Any]
    ) -> FlvMediaSnapshot:
        with open(path, 'rb') as file:
            file_stat = os.fstat(file.fileno())
            reader = FlvReader(file)
            header = reader.read_header()
            header_end = file.tell()
            first_tag = reader.read_tag()
            if is_metadata_tag(first_tag):
                original_metadata = parse_metadata(first_tag)
                source_tail_start = file.tell()
            else:
                original_metadata = {}
                source_tail_start = header_end

        keyframes = current_metadata.get('keyframes')
        if not isinstance(keyframes, Mapping):
            raise RecordingContentUnavailable('录制中的视频索引暂时不可用')
        raw_times = keyframes.get('times')
        raw_positions = keyframes.get('filepositions')
        if not isinstance(raw_times, list) or not isinstance(raw_positions, list):
            raise RecordingContentUnavailable('录制中的视频索引暂时不可用')
        indexed = []
        for raw_time, raw_position in zip(raw_times, raw_positions):
            if isinstance(raw_time, bool) or isinstance(raw_position, bool):
                continue
            if not isinstance(raw_time, (int, float)) or not isinstance(
                raw_position, (int, float)
            ):
                continue
            timestamp = float(raw_time)
            position = float(raw_position)
            if (
                math.isfinite(timestamp)
                and math.isfinite(position)
                and timestamp >= 0
                and source_tail_start <= position < source_size
            ):
                indexed.append((timestamp, position))
        if len(indexed) < 2:
            raise RecordingContentUnavailable('录制中的视频索引暂时不可用')

        duration = current_metadata.get('duration')
        if isinstance(duration, bool) or not isinstance(duration, (int, float)):
            raise RecordingContentUnavailable('录制中的视频时长暂时不可用')
        duration_seconds = float(duration)
        if not math.isfinite(duration_seconds) or duration_seconds <= 0:
            raise RecordingContentUnavailable('录制中的视频时长暂时不可用')
        analysed_size = current_metadata.get('filesize')
        if isinstance(analysed_size, (int, float)) and analysed_size > source_size:
            duration_seconds = indexed[-1][0]

        metadata = {
            **original_metadata,
            **current_metadata,
            'duration': duration_seconds,
            'filesize': float(source_size),
            'lasttimestamp': duration_seconds,
            'lastkeyframelocation': indexed[-1][1],
            'lastkeyframetimestamp': indexed[-1][0],
            'keyframes': {
                'times': [timestamp for timestamp, _ in indexed],
                'filepositions': [position for _, position in indexed],
            },
        }
        initial_prefix = cls._make_prefix(header, metadata)
        offset = len(initial_prefix) - source_tail_start
        logical_size = source_size + offset
        logical_positions = [position + offset for _, position in indexed]
        metadata.update(
            {
                'filesize': float(logical_size),
                'lastkeyframelocation': logical_positions[-1],
                'lastkeyframetimestamp': indexed[-1][0],
                'keyframes': {
                    'times': [timestamp for timestamp, _ in indexed],
                    'filepositions': logical_positions,
                },
            }
        )
        prefix = cls._make_prefix(header, metadata)
        if len(prefix) != len(initial_prefix):
            raise RecordingContentUnavailable('录制中的视频索引暂时不可用')
        return cls(
            path=path,
            source_size=source_size,
            source_tail_start=source_tail_start,
            prefix=prefix,
            duration_ms=int(round(duration_seconds * 1_000)),
            source_device=int(file_stat.st_dev),
            source_inode=int(file_stat.st_ino),
        )

    @classmethod
    def frozen(
        cls,
        path: str,
        source_size: int,
        *,
        source_device: Optional[int] = None,
        source_inode: Optional[int] = None,
    ) -> FlvMediaSnapshot:
        if source_size < 0:
            raise ValueError('snapshot size must not be negative')
        return cls(
            path=path,
            source_size=source_size,
            source_tail_start=0,
            prefix=b'',
            duration_ms=None,
            source_device=source_device,
            source_inode=source_inode,
        )

    @staticmethod
    def _make_prefix(header: FlvHeader, metadata: Mapping[str, Any]) -> bytes:
        output = BytesIO()
        writer = FlvWriter(output)
        writer.write_header(header)
        writer.write_tag(create_metadata_tag(ensure_order(dict(metadata))))
        return output.getvalue()

    def iter_range(
        self, start: int, length: int, *, chunk_size: int = 64 * 1024
    ) -> Iterator[bytes]:
        if start < 0 or length < 0 or start + length > self.size:
            raise ValueError('snapshot range is out of bounds')
        end = start + length
        prefix_end = min(end, len(self.prefix))
        if start < prefix_end:
            yield self.prefix[start:prefix_end]
        logical_source_start = max(start, len(self.prefix))
        if logical_source_start >= end:
            return
        source_start = self.source_tail_start + (
            logical_source_start - len(self.prefix)
        )
        remaining = end - logical_source_start
        with open(self.path, 'rb') as file:
            file.seek(source_start)
            while remaining > 0:
                chunk = file.read(min(chunk_size, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk


class RecordingContentReader:
    _MEDIA_TYPES = {
        '.flv': 'video/x-flv',
        '.m4s': 'video/iso.segment',
        '.mkv': 'video/x-matroska',
        '.mov': 'video/quicktime',
        '.mp4': 'video/mp4',
        '.ts': 'video/mp2t',
        '.webm': 'video/webm',
    }
    _DANMAKU_CACHE_SIZE = 2
    _DANMAKU_CACHE_TTL_SECONDS = 10 * 60
    _DANMAKU_PENDING_BYTES = 256 * 1024
    _DANMAKU_READ_BYTES = 64 * 1024
    _DANMAKU_PREFIX_BYTES = 4_096

    def __init__(
        self,
        database: BiliUploadDatabase,
        *,
        recording_root: Optional[Path] = None,
        media_library_root: Optional[Path] = None,
    ) -> None:
        self._database = database
        resolved_recording_root = (
            None if recording_root is None else recording_root.resolve()
        )
        self._recording_root = (
            None if resolved_recording_root is None else str(resolved_recording_root)
        )
        if media_library_root is None and resolved_recording_root is not None:
            media_library_root = resolved_recording_root.parent / 'favorites'
        self._media_library_root = (
            None if media_library_root is None else media_library_root.resolve()
        )
        self._danmaku_cache_lock = threading.RLock()
        self._danmaku_closed = False
        self._danmaku_reserved_bytes = 0
        self._danmaku_streams: OrderedDict[Tuple[int, int, int], _DanmakuStream] = (
            OrderedDict()
        )

    async def media_descriptor(self, part_id: int) -> RecordingMediaDescriptor:
        row = await self._database.fetchone(
            'SELECT session.room_id,part.part_index,part.source_path,part.final_path,'
            'part.artifact_state,part.media_index_state,'
            'job.state AS job_state,job.bvid,item.storage_key '
            'FROM recording_parts part '
            'JOIN recording_sessions session ON session.id=part.session_id '
            'LEFT JOIN upload_jobs job ON job.session_id=part.session_id '
            'LEFT JOIN media_library_items item ON item.session_id=part.session_id '
            'WHERE part.id=?',
            (int(part_id),),
        )
        if row is None:
            raise RecordingContentNotFound('录制分 P 不存在')
        artifact_state = str(row['artifact_state'])
        paths = []
        if row['final_path'] is not None:
            paths.append((str(row['final_path']), False, 'final'))
        source_path = str(row['source_path'])
        if not paths or paths[0][0] != source_path:
            paths.append(
                (
                    source_path,
                    artifact_state in ('recording', 'postprocessing'),
                    'source',
                )
            )
        candidates = tuple(
            RecordingMediaCandidate(
                path=path,
                content_type=self._MEDIA_TYPES.get(
                    Path(path).suffix.lower(), 'application/octet-stream'
                ),
                recording=recording,
                artifact_key='recording-part:{}:{}'.format(part_id, role),
            )
            for path, recording, role in paths
        )
        job_state = None if row['job_state'] is None else str(row['job_state'])
        bvid = None if row['bvid'] is None else str(row['bvid'])
        expected_root = self._recording_root
        if row['storage_key'] is not None and self._media_library_root is not None:
            item_root = (self._media_library_root / str(row['storage_key'])).resolve()
            try:
                item_root.relative_to(self._media_library_root)
            except ValueError:
                raise RecordingContentInvalid('媒体库文件路径越界') from None
            expected_root = str(item_root)
        return RecordingMediaDescriptor(
            part_id=int(part_id),
            room_id=int(row['room_id']),
            part_index=int(row['part_index']),
            candidates=candidates,
            bvid=bvid,
            remote_available=bool(bvid and job_state in ('approved', 'completed')),
            index_state=str(row['media_index_state']),
            expected_root=expected_root,
        )

    async def media(self, part_id: int) -> MediaResource:
        row = await self._database.fetchone(
            'SELECT session.room_id,part.part_index,part.source_path,part.final_path,'
            'part.artifact_state,part.media_index_state,'
            'job.state AS job_state,job.bvid '
            'FROM recording_parts part '
            'JOIN recording_sessions session ON session.id=part.session_id '
            'LEFT JOIN upload_jobs job ON job.session_id=part.session_id '
            'WHERE part.id=?',
            (int(part_id),),
        )
        if row is None:
            raise RecordingContentNotFound('录制分 P 不存在')
        values = dict(row)
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._resolve_media, values)

    async def danmaku(self, part_id: int, *, cursor: int, limit: int) -> DanmakuPage:
        if cursor < 0:
            raise ValueError('cursor must not be negative')
        if limit < 1 or limit > 500:
            raise ValueError('limit must be between 1 and 500')
        row = await self._database.fetchone(
            'SELECT xml_path,xml_completed FROM recording_parts WHERE id=?',
            (int(part_id),),
        )
        if row is None:
            raise RecordingContentNotFound('录制分 P 不存在')
        if row['xml_path'] is None:
            raise RecordingContentUnavailable('该分 P 没有弹幕文件')
        path = str(row['xml_path'])
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None,
            self._parse_danmaku,
            int(part_id),
            path,
            bool(row['xml_completed']),
            int(cursor),
            int(limit),
        )

    def close(self) -> None:
        with self._danmaku_cache_lock:
            self._danmaku_closed = True
            streams = tuple(self._danmaku_streams.values())
            self._danmaku_streams.clear()
        for stream in streams:
            with stream.lock:
                self._close_danmaku_stream(stream)

    @classmethod
    def _resolve_media(cls, row: Mapping[str, Any]) -> MediaResource:
        artifact_state = str(row['artifact_state'])
        candidates = []
        if row['final_path'] is not None:
            candidates.append((str(row['final_path']), False))
        source_path = str(row['source_path'])
        if not candidates or candidates[0][0] != source_path:
            candidates.append(
                (source_path, artifact_state in ('recording', 'postprocessing'))
            )
        resolved_path: Optional[str] = None
        resolved_size: Optional[int] = None
        resolved_device: Optional[int] = None
        resolved_inode: Optional[int] = None
        recording = False
        for path, is_recording in candidates:
            identity = cls._regular_file_identity(path)
            if identity is None:
                continue
            resolved_path = path
            resolved_size, resolved_device, resolved_inode = identity
            recording = is_recording
            break
        job_state = None if row['job_state'] is None else str(row['job_state'])
        bvid = None if row['bvid'] is None else str(row['bvid'])
        remote_available = bool(bvid and job_state in ('approved', 'completed'))
        if resolved_path is None and not remote_available:
            raise RecordingContentUnavailable('该分 P 的本地视频不可用')
        index_state = str(row['media_index_state'])
        suffix = '' if resolved_path is None else Path(resolved_path).suffix.lower()
        playback_mode: Literal['seekable', 'sequential', 'active_snapshot']
        if suffix != '.flv':
            playback_mode = 'seekable'
        elif recording:
            playback_mode = 'active_snapshot'
        elif index_state == 'ready':
            playback_mode = 'seekable'
        else:
            playback_mode = 'sequential'
        return MediaResource(
            path=resolved_path,
            size=resolved_size,
            content_type=(
                None
                if resolved_path is None
                else cls._MEDIA_TYPES.get(
                    Path(resolved_path).suffix.lower(), 'application/octet-stream'
                )
            ),
            recording=recording,
            room_id=int(row['room_id']),
            part_index=int(row['part_index']),
            bvid=bvid,
            remote_available=remote_available,
            playback_mode=playback_mode,
            index_state=index_state,
            source_device=resolved_device,
            source_inode=resolved_inode,
        )

    @staticmethod
    def _regular_file_identity(path: str) -> Optional[Tuple[int, int, int]]:
        try:
            result = os.stat(path)
        except OSError:
            return None
        if not stat.S_ISREG(result.st_mode):
            return None
        return int(result.st_size), int(result.st_dev), int(result.st_ino)

    def _parse_danmaku(
        self, part_id: int, path: str, finalized: bool, cursor: int, limit: int
    ) -> DanmakuPage:
        try:
            file_stat = os.stat(path)
        except OSError:
            raise RecordingContentUnavailable('该分 P 的弹幕文件不可用')
        if not stat.S_ISREG(file_stat.st_mode):
            raise RecordingContentUnavailable('该分 P 的弹幕文件不可用')
        key = (int(part_id), int(file_stat.st_dev), int(file_stat.st_ino))
        stream = self._select_danmaku_stream(
            key, path, file_stat, finalized=finalized, cursor=cursor
        )
        with stream.lock:
            with self._danmaku_cache_lock:
                if self._danmaku_streams.get(key) is not stream:
                    self._close_danmaku_stream(stream)
                    raise RecordingContentCursorStale('danmaku cursor stale')
            if stream.next_cursor != cursor:
                raise RecordingContentCursorStale('danmaku cursor stale')
            try:
                page = self._read_danmaku_page(stream, cursor, limit)
            except RecordingContentCursorStale:
                self._discard_danmaku_stream(key, stream)
                raise
            except (OSError, ValueError, etree.LxmlError, RecordingContentInvalid):
                self._discard_danmaku_stream(key, stream)
                raise RecordingContentInvalid('弹幕文件格式无效') from None
            if page.next_cursor is None:
                self._discard_danmaku_stream(key, stream)
            else:
                detached = False
                with self._danmaku_cache_lock:
                    if self._danmaku_streams.get(key) is stream:
                        stream.last_access = time.monotonic()
                        self._danmaku_streams.move_to_end(key)
                        if self._cached_danmaku_bytes() > self._DANMAKU_PENDING_BYTES:
                            self._discard_old_danmaku_streams_locked(key)
                            if (
                                self._cached_danmaku_bytes()
                                > self._DANMAKU_PENDING_BYTES
                            ):
                                self._danmaku_streams.pop(key, None)
                                self._close_danmaku_stream(stream)
                                raise RecordingContentCursorStale(
                                    'danmaku cursor stale'
                                )
                    else:
                        detached = True
                if detached:
                    self._close_danmaku_stream(stream)
            return page

    def _select_danmaku_stream(
        self,
        key: Tuple[int, int, int],
        path: str,
        file_stat: os.stat_result,
        *,
        finalized: bool,
        cursor: int,
    ) -> _DanmakuStream:
        now = time.monotonic()
        with self._danmaku_cache_lock:
            if self._danmaku_closed:
                raise RecordingContentUnavailable('弹幕读取器已关闭')
            self._expire_danmaku_streams_locked(now)
            stream = self._danmaku_streams.get(key)
            if cursor > 0:
                if stream is None:
                    self._discard_part_danmaku_streams_locked(key[0], keep=None)
                    raise RecordingContentCursorStale('danmaku cursor stale')
                stream.finalized = finalized
                self._danmaku_streams.move_to_end(key)
                return stream
            if stream is not None:
                self._danmaku_streams.pop(key, None)
                if stream.lock.acquire(blocking=False):
                    try:
                        self._close_danmaku_stream(stream)
                    finally:
                        stream.lock.release()
            self._discard_part_danmaku_streams_locked(key[0], keep=None)
            new_stream = self._new_danmaku_stream(
                key, path, file_stat, finalized=finalized, now=now
            )
            if not self._make_danmaku_cache_room_locked():
                self._close_danmaku_stream(new_stream)
                raise RecordingContentCursorStale('danmaku cursor stale')
            self._danmaku_streams[key] = new_stream
            return new_stream

    def _new_danmaku_stream(
        self,
        key: Tuple[int, int, int],
        path: str,
        file_stat: os.stat_result,
        *,
        finalized: bool,
        now: float,
    ) -> _DanmakuStream:
        file: Optional[BinaryIO] = None
        try:
            file = open(path, 'rb')
            opened_stat = os.fstat(file.fileno())
        except OSError:
            if file is not None:
                file.close()
            raise RecordingContentUnavailable('该分 P 的弹幕文件不可用') from None
        opened_key = (key[0], int(opened_stat.st_dev), int(opened_stat.st_ino))
        if (
            opened_key != key
            or not stat.S_ISREG(opened_stat.st_mode)
            or int(opened_stat.st_size) < int(file_stat.st_size)
        ):
            file.close()
            raise RecordingContentCursorStale('danmaku cursor stale')
        parser = etree.XMLPullParser(
            events=('end',),
            tag='d',
            resolve_entities=False,
            no_network=True,
            huge_tree=False,
        )
        return _DanmakuStream(
            part_id=key[0],
            path=path,
            identity=key,
            file=file,
            parser=parser,
            observed_size=int(opened_stat.st_size),
            read_offset=0,
            next_cursor=0,
            finalized=finalized,
            last_access=now,
        )

    def _read_danmaku_page(
        self, stream: _DanmakuStream, cursor: int, limit: int
    ) -> DanmakuPage:
        self._validate_danmaku_stream(stream)
        items = []
        remaining_bytes = self._DANMAKU_PENDING_BYTES
        if stream.pending is not None:
            items.append(stream.pending)
            stream.pending = None
        while len(items) < limit:
            item, remaining_bytes = self._next_danmaku(stream, remaining_bytes)
            if item is None:
                break
            items.append(item)
        if len(items) == limit:
            stream.pending, remaining_bytes = self._next_danmaku(
                stream, remaining_bytes
            )
        stream.next_cursor = cursor + len(items)
        if stream.pending is not None or not stream.parser_closed:
            return DanmakuPage(items=tuple(items), next_cursor=stream.next_cursor)
        return DanmakuPage(items=tuple(items), next_cursor=None)

    def _validate_danmaku_stream(self, stream: _DanmakuStream) -> None:
        try:
            path_stat = os.stat(stream.path)
            file_stat = os.fstat(stream.file.fileno())
        except OSError:
            raise RecordingContentCursorStale('danmaku cursor stale') from None
        path_identity = (stream.part_id, int(path_stat.st_dev), int(path_stat.st_ino))
        file_identity = (stream.part_id, int(file_stat.st_dev), int(file_stat.st_ino))
        if (
            path_identity != stream.identity
            or file_identity != stream.identity
            or int(file_stat.st_size) < stream.observed_size
        ):
            raise RecordingContentCursorStale('danmaku cursor stale')
        stream.observed_size = int(file_stat.st_size)

    def _next_danmaku(
        self, stream: _DanmakuStream, remaining_bytes: int
    ) -> Tuple[Optional[DanmakuLine], int]:
        while True:
            event = next(stream.parser.read_events(), None)
            if event is not None:
                _event_name, element = event
                item = self._danmaku_line(stream.ordinal, element)
                stream.ordinal += 1
                stream.parser_events_consumed = True
                element.clear()
                parent = element.getparent()
                while parent is not None and element.getprevious() is not None:
                    del parent[0]
                return item, remaining_bytes
            if stream.parser_events_consumed:
                self._release_danmaku_input(
                    stream,
                    max(0, stream.unreleased_input_bytes - self._DANMAKU_READ_BYTES),
                )
                stream.parser_events_consumed = False
            available = stream.observed_size - stream.read_offset
            if (
                available > 0
                and stream.unreleased_input_bytes >= self._DANMAKU_PENDING_BYTES
            ):
                raise RecordingContentCursorStale('danmaku cursor stale')
            if available > 0 and remaining_bytes > 0:
                requested = min(
                    self._DANMAKU_READ_BYTES,
                    remaining_bytes,
                    available,
                    self._DANMAKU_PENDING_BYTES - stream.unreleased_input_bytes,
                )
                reserved = self._reserve_danmaku_input(stream, requested)
                try:
                    chunk = stream.file.read(reserved)
                except OSError:
                    self._release_danmaku_input(stream, reserved)
                    raise
                if len(chunk) < reserved:
                    self._release_danmaku_input(stream, reserved - len(chunk))
                if not chunk:
                    return None, remaining_bytes
                stream.read_offset += len(chunk)
                remaining_bytes -= len(chunk)
                if len(stream.prefix) < self._DANMAKU_PREFIX_BYTES:
                    remaining = self._DANMAKU_PREFIX_BYTES - len(stream.prefix)
                    stream.prefix.extend(chunk[:remaining])
                    upper_prefix = bytes(stream.prefix).upper()
                    if b'<!DOCTYPE' in upper_prefix or b'<!ENTITY' in upper_prefix:
                        raise RecordingContentInvalid('弹幕文件格式无效')
                stream.parser.feed(chunk)
                continue
            if available > 0:
                return None, remaining_bytes
            if stream.finalized and not stream.parser_closed:
                stream.parser.close()
                stream.parser_closed = True
                continue
            return None, remaining_bytes

    def _reserve_danmaku_input(self, stream: _DanmakuStream, requested: int) -> int:
        if requested <= 0:
            raise RecordingContentCursorStale('danmaku cursor stale')
        with self._danmaku_cache_lock:
            if (
                stream.closed
                or self._danmaku_streams.get(stream.identity) is not stream
            ):
                raise RecordingContentCursorStale('danmaku cursor stale')
            self._evict_for_danmaku_input_locked(stream.identity, requested)
            if self._danmaku_reserved_bytes + requested > self._DANMAKU_PENDING_BYTES:
                raise RecordingContentCursorStale('danmaku cursor stale')
            stream.unreleased_input_bytes += requested
            self._danmaku_reserved_bytes += requested
            return requested

    def _release_danmaku_input(self, stream: _DanmakuStream, released: int) -> None:
        if released <= 0:
            return
        with self._danmaku_cache_lock:
            actual = min(released, stream.unreleased_input_bytes)
            stream.unreleased_input_bytes -= actual
            self._danmaku_reserved_bytes = max(0, self._danmaku_reserved_bytes - actual)

    def _evict_for_danmaku_input_locked(
        self, current_key: Tuple[int, int, int], requested: int
    ) -> None:
        if self._danmaku_reserved_bytes + requested <= self._DANMAKU_PENDING_BYTES:
            return
        for key, stream in tuple(self._danmaku_streams.items()):
            if key == current_key or not stream.lock.acquire(False):
                continue
            try:
                if self._danmaku_streams.get(key) is stream:
                    self._danmaku_streams.pop(key, None)
                    self._close_danmaku_stream(stream)
            finally:
                stream.lock.release()
            if self._danmaku_reserved_bytes + requested <= self._DANMAKU_PENDING_BYTES:
                return

    def _discard_danmaku_stream(
        self, key: Tuple[int, int, int], stream: _DanmakuStream
    ) -> None:
        with self._danmaku_cache_lock:
            if self._danmaku_streams.get(key) is stream:
                self._danmaku_streams.pop(key, None)
        self._close_danmaku_stream(stream)

    def _expire_danmaku_streams_locked(self, now: float) -> None:
        deadline = now - self._DANMAKU_CACHE_TTL_SECONDS
        for key, stream in tuple(self._danmaku_streams.items()):
            if stream.last_access > deadline or not stream.lock.acquire(False):
                continue
            try:
                if self._danmaku_streams.get(key) is stream:
                    self._danmaku_streams.pop(key, None)
                    self._close_danmaku_stream(stream)
            finally:
                stream.lock.release()

    def _discard_part_danmaku_streams_locked(
        self, part_id: int, keep: Optional[Tuple[int, int, int]]
    ) -> None:
        for key, stream in tuple(self._danmaku_streams.items()):
            if key[0] != part_id or key == keep:
                continue
            if self._danmaku_streams.get(key) is stream:
                self._danmaku_streams.pop(key, None)
            if stream.lock.acquire(False):
                try:
                    self._close_danmaku_stream(stream)
                finally:
                    stream.lock.release()

    def _make_danmaku_cache_room_locked(self) -> bool:
        while len(self._danmaku_streams) >= self._DANMAKU_CACHE_SIZE:
            evicted = False
            for key, stream in tuple(self._danmaku_streams.items()):
                if not stream.lock.acquire(False):
                    continue
                try:
                    if self._danmaku_streams.get(key) is stream:
                        self._danmaku_streams.pop(key, None)
                        self._close_danmaku_stream(stream)
                        evicted = True
                        break
                finally:
                    stream.lock.release()
            if not evicted:
                return False
        return True

    def _discard_old_danmaku_streams_locked(
        self, current_key: Tuple[int, int, int]
    ) -> None:
        for key, stream in tuple(self._danmaku_streams.items()):
            if key == current_key or not stream.lock.acquire(False):
                continue
            try:
                if self._danmaku_streams.get(key) is stream:
                    self._danmaku_streams.pop(key, None)
                    self._close_danmaku_stream(stream)
            finally:
                stream.lock.release()
            if self._cached_danmaku_bytes() <= self._DANMAKU_PENDING_BYTES:
                return

    def _cached_danmaku_bytes(self) -> int:
        return self._danmaku_reserved_bytes

    def _close_danmaku_stream(self, stream: _DanmakuStream) -> None:
        with self._danmaku_cache_lock:
            if stream.closed:
                return
            stream.closed = True
            self._release_danmaku_input(stream, stream.unreleased_input_bytes)
        stream.file.close()

    @staticmethod
    def _danmaku_line(index: int, element: etree._Element) -> DanmakuLine:
        values = (element.get('p') or '').split(',')
        if len(values) < 4:
            raise RecordingContentInvalid('弹幕文件格式无效')
        try:
            progress = float(values[0])
            mode = int(values[1])
            font_size = int(values[2])
            color = int(values[3])
        except ValueError:
            raise RecordingContentInvalid('弹幕文件格式无效') from None
        if not math.isfinite(progress) or progress < 0:
            raise RecordingContentInvalid('弹幕文件格式无效')
        user_value = element.get('user')
        user = (
            user_value.strip()
            if isinstance(user_value, str) and user_value.strip()
            else None
        )
        uid_value = element.get('uid')
        try:
            uid = None if uid_value is None else int(uid_value)
        except ValueError:
            uid = None
        if uid is not None and uid < 0:
            uid = None
        return DanmakuLine(
            index=index,
            progress_ms=int(round(progress * 1_000)),
            mode=mode,
            font_size=font_size,
            color=color,
            content=element.text or '',
            user=user,
            uid=uid,
        )
