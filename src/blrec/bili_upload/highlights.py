from __future__ import annotations

import os
import sqlite3
import time
from dataclasses import dataclass
from typing import Callable, List, Mapping, Optional, Tuple

from blrec.logging.audit import audit

from .database import BiliUploadDatabase


@dataclass(frozen=True)
class HighlightMarker:
    id: int
    room_id: int
    observed_at_ms: int
    player_delay_ms: int
    content_at_ms: int
    title: str
    anchor_name: str
    name: str
    note: str
    source: str
    created_at: int
    updated_at: int


@dataclass(frozen=True)
class TimelinePart:
    part_id: int
    part_index: int
    path: str
    absolute_start_at_ms: int
    timeline_start_ms: int
    duration_ms: int
    stable_end_ms: int
    recording: bool


@dataclass(frozen=True)
class MappedHighlight:
    marker: HighlightMarker
    part_id: int
    local_offset_ms: int
    timeline_offset_ms: int


@dataclass(frozen=True)
class HighlightTimeline:
    session_id: int
    room_id: int
    duration_ms: int
    stable_end_ms: int
    parts: Tuple[TimelinePart, ...]
    markers: Tuple[MappedHighlight, ...]


class HighlightService:
    ACTIVE_SAFE_TAIL_MS = 10_000

    def __init__(
        self, database: BiliUploadDatabase, *, clock: Callable[[], float] = time.time
    ) -> None:
        self._database = database
        self._clock = clock

    async def create_marker(
        self,
        *,
        room_id: int,
        observed_at_ms: int,
        player_delay_ms: int,
        title: str,
        anchor_name: str,
        source: str,
    ) -> HighlightMarker:
        if room_id <= 0:
            raise ValueError('room_id must be positive')
        if observed_at_ms <= 0:
            raise ValueError('observed_at_ms must be positive')
        if source not in ('web', 'browser_extension'):
            raise ValueError('invalid highlight source')
        delay_ms = min(300_000, max(0, int(player_delay_ms)))
        clock_now = self._clock()
        content_at_ms = int(clock_now * 1000) - delay_ms
        if content_at_ms <= 0:
            raise ValueError('highlight content time must be positive')
        now = int(clock_now)
        name = self._default_name(title, content_at_ms)

        def write(connection: sqlite3.Connection) -> sqlite3.Row:
            cursor = connection.execute(
                'INSERT INTO highlight_markers('
                'room_id,observed_at_ms,player_delay_ms,content_at_ms,title,'
                'anchor_name,name,note,source,created_at,updated_at) '
                "VALUES(?,?,?,?,?,?,?,'',?,?,?)",
                (
                    room_id,
                    int(observed_at_ms),
                    delay_ms,
                    content_at_ms,
                    title,
                    anchor_name,
                    name,
                    source,
                    now,
                    now,
                ),
            )
            row = connection.execute(
                'SELECT * FROM highlight_markers WHERE id=?', (int(cursor.lastrowid),)
            ).fetchone()
            assert row is not None
            return row

        row = await self._database.write(write)
        marker = self._marker_from_row(row)
        audit(
            'highlight_marker_created',
            marker_id=marker.id,
            room_id=room_id,
            observed_at_ms=observed_at_ms,
            player_delay_ms=delay_ms,
            content_at_ms=content_at_ms,
            source=source,
            result='saved',
        )
        return marker

    async def update_marker(
        self, marker_id: int, name: str, note: str
    ) -> HighlightMarker:
        normalized_name = name.strip()
        if not normalized_name or len(normalized_name) > 200:
            raise ValueError('highlight name must contain 1 to 200 characters')
        if len(note) > 1000:
            raise ValueError('highlight note must not exceed 1000 characters')
        now = int(self._clock())
        changed = await self._database.execute(
            'UPDATE highlight_markers SET name=?,note=?,updated_at=? WHERE id=?',
            (normalized_name, note, now, marker_id),
        )
        if changed != 1:
            raise ValueError("unknown highlight marker '{}'".format(marker_id))
        row = await self._database.fetchone(
            'SELECT * FROM highlight_markers WHERE id=?', (marker_id,)
        )
        assert row is not None
        marker = self._marker_from_row(row)
        audit(
            'highlight_marker_updated',
            marker_id=marker_id,
            room_id=marker.room_id,
            result='saved',
        )
        return marker

    async def delete_marker(self, marker_id: int) -> None:
        row = await self._database.fetchone(
            'SELECT room_id FROM highlight_markers WHERE id=?', (marker_id,)
        )
        if row is None:
            raise ValueError("unknown highlight marker '{}'".format(marker_id))
        await self._database.execute(
            'DELETE FROM highlight_markers WHERE id=?', (marker_id,)
        )
        audit(
            'highlight_marker_deleted',
            marker_id=marker_id,
            room_id=int(row['room_id']),
            result='deleted',
        )

    async def timeline(
        self, session_id: int, active_durations_ms: Mapping[int, int]
    ) -> HighlightTimeline:
        session = await self._database.fetchone(
            'SELECT id,room_id,source_kind FROM recording_sessions WHERE id=?',
            (session_id,),
        )
        if session is None or str(session['source_kind']) != 'live':
            raise ValueError("unknown live recording session '{}'".format(session_id))
        room_id = int(session['room_id'])
        rows = await self._database.fetchall(
            'SELECT id,part_index,source_path,final_path,record_start_time,'
            'timeline_start_at_ms,record_duration_seconds,artifact_state '
            'FROM recording_parts WHERE session_id=? '
            "AND artifact_state IN ('recording','postprocessing','ready') "
            'AND video_deleted_at IS NULL ORDER BY part_index',
            (session_id,),
        )
        candidates = []
        for row in rows:
            path = self._available_path(row)
            if path is None:
                continue
            part_id = int(row['id'])
            recording = str(row['artifact_state']) == 'recording'
            if part_id in active_durations_ms:
                duration_ms = max(0, int(active_durations_ms[part_id]))
                recording = True
            elif row['record_duration_seconds'] is not None:
                duration_ms = max(0, int(row['record_duration_seconds']) * 1000)
            else:
                duration_ms = 0
            absolute_start_at_ms = (
                int(row['timeline_start_at_ms'])
                if row['timeline_start_at_ms'] is not None
                else int(row['record_start_time']) * 1000
            )
            candidates.append(
                (
                    part_id,
                    int(row['part_index']),
                    path,
                    absolute_start_at_ms,
                    duration_ms,
                    recording,
                )
            )
        if not candidates:
            return HighlightTimeline(session_id, room_id, 0, 0, (), ())

        origin_ms = min(item[3] for item in candidates)
        parts: List[TimelinePart] = []
        for (
            part_id,
            part_index,
            path,
            absolute_start_at_ms,
            duration_ms,
            recording,
        ) in candidates:
            timeline_start_ms = absolute_start_at_ms - origin_ms
            stable_duration_ms = (
                max(0, duration_ms - self.ACTIVE_SAFE_TAIL_MS)
                if recording
                else duration_ms
            )
            parts.append(
                TimelinePart(
                    part_id=part_id,
                    part_index=part_index,
                    path=path,
                    absolute_start_at_ms=absolute_start_at_ms,
                    timeline_start_ms=timeline_start_ms,
                    duration_ms=duration_ms,
                    stable_end_ms=timeline_start_ms + stable_duration_ms,
                    recording=recording,
                )
            )

        marker_rows = await self._database.fetchall(
            'SELECT * FROM highlight_markers WHERE room_id=? '
            'ORDER BY content_at_ms,id',
            (room_id,),
        )
        mapped: List[MappedHighlight] = []
        for marker_row in marker_rows:
            marker = self._marker_from_row(marker_row)
            part = self._part_containing(parts, marker.content_at_ms)
            if part is None:
                continue
            local_offset_ms = marker.content_at_ms - part.absolute_start_at_ms
            mapped.append(
                MappedHighlight(
                    marker=marker,
                    part_id=part.part_id,
                    local_offset_ms=local_offset_ms,
                    timeline_offset_ms=part.timeline_start_ms + local_offset_ms,
                )
            )
        mapped.sort(key=lambda item: (item.timeline_offset_ms, item.marker.id))
        return HighlightTimeline(
            session_id=session_id,
            room_id=room_id,
            duration_ms=max(
                part.timeline_start_ms + part.duration_ms for part in parts
            ),
            stable_end_ms=max(part.stable_end_ms for part in parts),
            parts=tuple(parts),
            markers=tuple(mapped),
        )

    @staticmethod
    def _available_path(row: sqlite3.Row) -> Optional[str]:
        paths = (row['final_path'], row['source_path'])
        for value in paths:
            if value is not None and os.path.isfile(str(value)):
                return str(value)
        return None

    @staticmethod
    def _part_containing(
        parts: List[TimelinePart], content_at_ms: int
    ) -> Optional[TimelinePart]:
        for part in parts:
            local_offset_ms = content_at_ms - part.absolute_start_at_ms
            if 0 <= local_offset_ms <= part.duration_ms:
                return part
        return None

    @staticmethod
    def _default_name(title: str, content_at_ms: int) -> str:
        formatted = time.strftime('%H:%M:%S', time.localtime(content_at_ms / 1000.0))
        prefix = title.strip() or '直播'
        suffix = ' 高光 {}'.format(formatted)
        return '{}{}'.format(prefix[: 200 - len(suffix)], suffix)

    @staticmethod
    def _marker_from_row(row: sqlite3.Row) -> HighlightMarker:
        return HighlightMarker(
            id=int(row['id']),
            room_id=int(row['room_id']),
            observed_at_ms=int(row['observed_at_ms']),
            player_delay_ms=int(row['player_delay_ms']),
            content_at_ms=int(row['content_at_ms']),
            title=str(row['title']),
            anchor_name=str(row['anchor_name']),
            name=str(row['name']),
            note=str(row['note']),
            source=str(row['source']),
            created_at=int(row['created_at']),
            updated_at=int(row['updated_at']),
        )
