from __future__ import annotations

import asyncio
import math
import os
import stat
import threading
from collections import OrderedDict
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any, Iterator, Literal, Mapping, Optional, Tuple

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
    'RecordingContentInvalid',
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
    iterator: Iterator[DanmakuLine]
    next_cursor: int
    pending: Optional[DanmakuLine] = None


@dataclass(frozen=True)
class FlvMediaSnapshot:
    path: str
    source_size: int
    source_tail_start: int
    prefix: bytes
    duration_ms: Optional[int]

    @property
    def size(self) -> int:
        return len(self.prefix) + self.source_size - self.source_tail_start

    @classmethod
    def create(
        cls, path: str, source_size: int, current_metadata: Mapping[str, Any]
    ) -> FlvMediaSnapshot:
        with open(path, 'rb') as file:
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
        )

    @classmethod
    def frozen(cls, path: str, source_size: int) -> FlvMediaSnapshot:
        if source_size < 0:
            raise ValueError('snapshot size must not be negative')
        return cls(
            path=path,
            source_size=source_size,
            source_tail_start=0,
            prefix=b'',
            duration_ms=None,
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
        '.mp4': 'video/mp4',
        '.ts': 'video/mp2t',
    }

    def __init__(self, database: BiliUploadDatabase) -> None:
        self._database = database
        self._danmaku_lock = threading.Lock()
        self._danmaku_streams: OrderedDict[Tuple[str, int, int], _DanmakuStream] = (
            OrderedDict()
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
            'SELECT xml_path FROM recording_parts WHERE id=?', (int(part_id),)
        )
        if row is None:
            raise RecordingContentNotFound('录制分 P 不存在')
        if row['xml_path'] is None:
            raise RecordingContentUnavailable('该分 P 没有弹幕文件')
        path = str(row['xml_path'])
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None, self._parse_danmaku, path, int(cursor), int(limit)
        )

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
        recording = False
        for path, is_recording in candidates:
            snapshot_size = cls._regular_file_size(path)
            if snapshot_size is None:
                continue
            resolved_path = path
            resolved_size = snapshot_size
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
        )

    @staticmethod
    def _regular_file_size(path: str) -> Optional[int]:
        try:
            result = os.stat(path)
        except OSError:
            return None
        if not stat.S_ISREG(result.st_mode):
            return None
        return int(result.st_size)

    def _parse_danmaku(self, path: str, cursor: int, limit: int) -> DanmakuPage:
        try:
            file_stat = os.stat(path)
        except OSError:
            raise RecordingContentUnavailable('该分 P 的弹幕文件不可用')
        if not stat.S_ISREG(file_stat.st_mode):
            raise RecordingContentUnavailable('该分 P 的弹幕文件不可用')
        key = (path, int(file_stat.st_size), int(file_stat.st_mtime_ns))
        with self._danmaku_lock:
            self._drop_stale_danmaku_streams(path, key)
            stream = self._danmaku_streams.get(key)
            if stream is None or stream.next_cursor != cursor:
                if stream is not None:
                    self._close_danmaku_stream(stream)
                stream = self._new_danmaku_stream(path, cursor)
                self._danmaku_streams[key] = stream
            self._danmaku_streams.move_to_end(key)
            while len(self._danmaku_streams) > 2:
                _old_key, old = self._danmaku_streams.popitem(last=False)
                self._close_danmaku_stream(old)
            return self._read_danmaku_page(key, stream, cursor, limit)

    def _new_danmaku_stream(self, path: str, cursor: int) -> _DanmakuStream:
        iterator = self._iter_danmaku(path)
        try:
            for _index in range(cursor):
                next(iterator)
        except StopIteration:
            return _DanmakuStream(iter(()), cursor)
        return _DanmakuStream(iterator, cursor)

    def _read_danmaku_page(
        self, key: Tuple[str, int, int], stream: _DanmakuStream, cursor: int, limit: int
    ) -> DanmakuPage:
        items = []
        try:
            if stream.pending is not None:
                items.append(stream.pending)
                stream.pending = None
            while len(items) < limit:
                items.append(next(stream.iterator))
            stream.pending = next(stream.iterator)
        except StopIteration:
            self._danmaku_streams.pop(key, None)
            self._close_danmaku_stream(stream)
            return DanmakuPage(items=tuple(items), next_cursor=None)
        except (OSError, ValueError, etree.LxmlError, RecordingContentInvalid):
            self._danmaku_streams.pop(key, None)
            self._close_danmaku_stream(stream)
            raise RecordingContentInvalid('弹幕文件格式无效') from None
        stream.next_cursor = cursor + len(items)
        return DanmakuPage(items=tuple(items), next_cursor=stream.next_cursor)

    @classmethod
    def _iter_danmaku(cls, path: str) -> Iterator[DanmakuLine]:
        try:
            with open(path, 'rb') as file:
                prefix = file.read(4_096).upper()
            if b'<!DOCTYPE' in prefix or b'<!ENTITY' in prefix:
                raise RecordingContentInvalid('弹幕文件格式无效')
            with open(path, 'rb') as file:
                context = etree.iterparse(
                    file,
                    events=('end',),
                    tag='d',
                    resolve_entities=False,
                    no_network=True,
                    huge_tree=False,
                )
                for ordinal, (_event, element) in enumerate(context):
                    yield cls._danmaku_line(ordinal, element)
                    element.clear()
                    parent = element.getparent()
                    while parent is not None and element.getprevious() is not None:
                        del parent[0]
        except RecordingContentInvalid:
            raise
        except (OSError, ValueError, etree.LxmlError):
            raise RecordingContentInvalid('弹幕文件格式无效') from None

    def _drop_stale_danmaku_streams(
        self, path: str, current_key: Tuple[str, int, int]
    ) -> None:
        for key in tuple(self._danmaku_streams):
            if key[0] != path or key == current_key:
                continue
            stream = self._danmaku_streams.pop(key)
            self._close_danmaku_stream(stream)

    @staticmethod
    def _close_danmaku_stream(stream: _DanmakuStream) -> None:
        close = getattr(stream.iterator, 'close', None)
        if callable(close):
            close()

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
