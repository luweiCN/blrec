from __future__ import annotations

import asyncio
import math
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional, Tuple

from lxml import etree

from .database import BiliUploadDatabase

__all__ = (
    'DanmakuLine',
    'DanmakuPage',
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
    part_index: int
    bvid: Optional[str]
    remote_available: bool


@dataclass(frozen=True)
class DanmakuLine:
    index: int
    progress_ms: int
    mode: int
    font_size: int
    color: int
    content: str


@dataclass(frozen=True)
class DanmakuPage:
    items: Tuple[DanmakuLine, ...]
    next_cursor: Optional[int]


class RecordingContentReader:
    _MEDIA_TYPES = {
        '.flv': 'video/x-flv',
        '.m4s': 'video/iso.segment',
        '.mp4': 'video/mp4',
        '.ts': 'video/mp2t',
    }

    def __init__(self, database: BiliUploadDatabase) -> None:
        self._database = database

    async def media(self, part_id: int) -> MediaResource:
        row = await self._database.fetchone(
            'SELECT part.part_index,part.source_path,part.final_path,'
            'part.artifact_state,job.state AS job_state,job.bvid '
            'FROM recording_parts part '
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
        if limit < 1 or limit > 100:
            raise ValueError('limit must be between 1 and 100')
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
            part_index=int(row['part_index']),
            bvid=bvid,
            remote_available=remote_available,
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

    @classmethod
    def _parse_danmaku(cls, path: str, cursor: int, limit: int) -> DanmakuPage:
        if cls._regular_file_size(path) is None:
            raise RecordingContentUnavailable('该分 P 的弹幕文件不可用')
        try:
            with open(path, 'rb') as file:
                prefix = file.read(4_096).upper()
            if b'<!DOCTYPE' in prefix or b'<!ENTITY' in prefix:
                raise RecordingContentInvalid('弹幕文件格式无效')
            items = []
            ordinal = 0
            context = etree.iterparse(
                path,
                events=('end',),
                tag='d',
                resolve_entities=False,
                no_network=True,
                huge_tree=False,
            )
            for _event, element in context:
                if ordinal >= cursor:
                    items.append(cls._danmaku_line(ordinal, element))
                    if len(items) > limit:
                        break
                ordinal += 1
                element.clear()
                parent = element.getparent()
                while parent is not None and element.getprevious() is not None:
                    del parent[0]
            has_more = len(items) > limit
            return DanmakuPage(
                items=tuple(items[:limit]),
                next_cursor=cursor + limit if has_more else None,
            )
        except RecordingContentInvalid:
            raise
        except (OSError, ValueError, etree.LxmlError):
            raise RecordingContentInvalid('弹幕文件格式无效') from None

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
        return DanmakuLine(
            index=index,
            progress_ms=int(round(progress * 1_000)),
            mode=mode,
            font_size=font_size,
            color=color,
            content=element.text or '',
        )
