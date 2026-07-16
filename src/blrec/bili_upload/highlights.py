from __future__ import annotations

import asyncio
import os
import sqlite3
import time
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Callable, List, Mapping, Optional, Sequence, Tuple

from blrec.logging.audit import audit

from .database import BiliUploadDatabase
from .highlight_cut import ClipInspection, ClipSource, LosslessClipper


class HighlightRangeUnavailable(RuntimeError):
    pass


class HighlightConfirmationRequired(RuntimeError):
    def __init__(self, inspection: ClipInspection) -> None:
        super().__init__(
            '无损剪辑会额外保留 {:.1f} 秒，请确认后继续'.format(
                inspection.extra_lead_ms / 1000.0
            )
        )
        self.inspection = inspection
        self.extra_lead_ms = inspection.extra_lead_ms


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
    recording_part_id: Optional[int] = None
    part_anchor_at_ms: Optional[int] = None
    current_time_ms: Optional[int] = None
    seekable_end_ms: Optional[int] = None
    raw_delay_ms: int = 0
    baseline_delay_ms: int = 0
    effective_rewind_ms: int = 0


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


@dataclass(frozen=True)
class HighlightClipSource:
    part_id: int
    ordinal: int
    requested_start_ms: int
    requested_end_ms: int
    actual_start_ms: Optional[int]
    actual_end_ms: Optional[int]


@dataclass(frozen=True)
class HighlightClip:
    id: int
    marker_id: Optional[int]
    room_id: int
    source_session_id: Optional[int]
    upload_session_id: Optional[int]
    name: str
    requested_start_ms: int
    requested_end_ms: int
    actual_start_ms: Optional[int]
    actual_end_ms: Optional[int]
    output_video_path: Optional[str]
    output_xml_path: Optional[str]
    state: str
    confirmation_required: bool
    confirmed: bool
    error_message: Optional[str]
    attempt: int
    created_at: int
    updated_at: int
    sources: Tuple[HighlightClipSource, ...] = ()


class HighlightService:
    ACTIVE_SAFE_TAIL_MS = 10_000

    def __init__(
        self,
        database: BiliUploadDatabase,
        *,
        recording_root: Optional[Path] = None,
        clipper: Optional[LosslessClipper] = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._database = database
        self._recording_root = (
            None if recording_root is None else Path(recording_root).resolve()
        )
        self._clipper = clipper
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
        current_time_ms: Optional[int] = None,
        seekable_end_ms: Optional[int] = None,
        raw_delay_ms: int = 0,
        baseline_delay_ms: int = 0,
        effective_rewind_ms: Optional[int] = None,
        name: str = '',
    ) -> HighlightMarker:
        if room_id <= 0:
            raise ValueError('room_id must be positive')
        if observed_at_ms <= 0:
            raise ValueError('observed_at_ms must be positive')
        if source not in ('web', 'browser_extension'):
            raise ValueError('invalid highlight source')
        delay_ms = min(300_000, max(0, int(player_delay_ms)))
        raw_delay = min(86_400_000, max(0, int(raw_delay_ms)))
        baseline_delay = min(86_400_000, max(0, int(baseline_delay_ms)))
        rewind_ms = (
            delay_ms
            if effective_rewind_ms is None
            else min(86_400_000, max(0, int(effective_rewind_ms)))
        )
        current_time = self._optional_nonnegative(current_time_ms)
        seekable_end = self._optional_nonnegative(seekable_end_ms)
        clock_now = self._clock()
        received_at_ms = int(clock_now * 1000)
        content_at_ms = received_at_ms - rewind_ms
        if content_at_ms <= 0:
            raise ValueError('highlight content time must be positive')
        now = int(clock_now)
        normalized_name = name.strip()
        if len(normalized_name) > 200:
            raise ValueError('highlight name must not exceed 200 characters')
        marker_name = normalized_name or self._default_name(title, content_at_ms)
        active_part = await self._database.fetchone(
            'SELECT part.id,COALESCE(part.timeline_start_at_ms,'
            'part.record_start_time*1000) AS anchor_at_ms '
            'FROM recording_parts part '
            'JOIN recording_sessions session ON session.id=part.session_id '
            "WHERE session.room_id=? AND session.source_kind='live' "
            "AND session.state='open' AND part.video_deleted_at IS NULL "
            "AND part.artifact_state IN ('recording','postprocessing','ready') "
            'AND COALESCE(part.timeline_start_at_ms,'
            'part.record_start_time*1000)<=? '
            'ORDER BY anchor_at_ms DESC,part.id DESC LIMIT 1',
            (room_id, received_at_ms),
        )
        recording_part_id = None if active_part is None else int(active_part['id'])
        part_anchor_at_ms = (
            None if active_part is None else int(active_part['anchor_at_ms'])
        )

        def write(connection: sqlite3.Connection) -> sqlite3.Row:
            cursor = connection.execute(
                'INSERT INTO highlight_markers('
                'room_id,observed_at_ms,player_delay_ms,content_at_ms,title,'
                'anchor_name,name,note,source,created_at,updated_at,'
                'recording_part_id,part_anchor_at_ms,current_time_ms,'
                'seekable_end_ms,raw_delay_ms,baseline_delay_ms,'
                'effective_rewind_ms) '
                "VALUES(?,?,?,?,?,?,?,'',?,?,?,?,?,?,?,?,?,?)",
                (
                    room_id,
                    int(observed_at_ms),
                    delay_ms,
                    content_at_ms,
                    title,
                    anchor_name,
                    marker_name,
                    source,
                    now,
                    now,
                    recording_part_id,
                    part_anchor_at_ms,
                    current_time,
                    seekable_end,
                    raw_delay,
                    baseline_delay,
                    rewind_ms,
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
            raw_delay_ms=raw_delay,
            baseline_delay_ms=baseline_delay,
            effective_rewind_ms=rewind_ms,
            recording_part_id=recording_part_id,
            part_anchor_at_ms=part_anchor_at_ms,
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

    async def inspect_clip(
        self,
        *,
        session_id: int,
        requested_start_ms: int,
        requested_end_ms: int,
        active_durations_ms: Mapping[int, int],
    ) -> ClipInspection:
        _timeline, _sources, inspection = await self._prepare_clip(
            session_id=session_id,
            requested_start_ms=requested_start_ms,
            requested_end_ms=requested_end_ms,
            active_durations_ms=active_durations_ms,
        )
        return inspection

    async def create_clip(
        self,
        *,
        session_id: int,
        marker_id: Optional[int],
        name: str,
        requested_start_ms: int,
        requested_end_ms: int,
        confirm_keyframe: bool,
        active_durations_ms: Mapping[int, int],
    ) -> HighlightClip:
        normalized_name = name.strip()
        if not normalized_name or len(normalized_name) > 200:
            raise ValueError('highlight clip name must contain 1 to 200 characters')
        if self._recording_root is None or self._clipper is None:
            raise RuntimeError('highlight clipping is not configured')
        _timeline, source_ranges, inspection = await self._prepare_clip(
            session_id=session_id,
            requested_start_ms=requested_start_ms,
            requested_end_ms=requested_end_ms,
            active_durations_ms=active_durations_ms,
        )
        if inspection.confirmation_required and not confirm_keyframe:
            raise HighlightConfirmationRequired(inspection)

        now = int(self._clock())
        root = self._recording_root
        inspected_by_part = {source.part_id: source for source in inspection.sources}

        def write(connection: sqlite3.Connection) -> int:
            session = connection.execute(
                'SELECT room_id,source_kind FROM recording_sessions WHERE id=?',
                (session_id,),
            ).fetchone()
            if session is None or str(session['source_kind']) != 'live':
                raise HighlightRangeUnavailable('录制场次已经不存在')
            room_id = int(session['room_id'])
            if marker_id is not None:
                marker = connection.execute(
                    'SELECT room_id FROM highlight_markers WHERE id=?', (marker_id,)
                ).fetchone()
                if marker is None or int(marker['room_id']) != room_id:
                    raise HighlightRangeUnavailable('高光标记与录像房间不匹配')
            for part, local_start_ms, local_end_ms in source_ranges:
                current = connection.execute(
                    'SELECT session_id,source_path,final_path,artifact_state,'
                    'record_duration_seconds,video_deleted_at '
                    'FROM recording_parts WHERE id=?',
                    (part.part_id,),
                ).fetchone()
                if (
                    current is None
                    or int(current['session_id']) != session_id
                    or current['video_deleted_at'] is not None
                    or str(current['artifact_state'])
                    not in ('recording', 'postprocessing', 'ready')
                    or self._available_path(current) != part.path
                ):
                    raise HighlightRangeUnavailable('源录像状态已经发生变化')
                current_duration_ms = (
                    max(0, int(active_durations_ms[part.part_id]))
                    if part.part_id in active_durations_ms
                    else (
                        0
                        if current['record_duration_seconds'] is None
                        else max(0, int(current['record_duration_seconds']) * 1000)
                    )
                )
                current_recording = (
                    str(current['artifact_state']) == 'recording'
                    or part.part_id in active_durations_ms
                )
                current_stable_ms = current_duration_ms - (
                    self.ACTIVE_SAFE_TAIL_MS if current_recording else 0
                )
                if local_end_ms > max(0, current_stable_ms):
                    raise HighlightRangeUnavailable('所选范围进入录制中的最后 10 秒')
                if local_start_ms < 0 or local_end_ms <= local_start_ms:
                    raise HighlightRangeUnavailable('源录像时间范围已经发生变化')
            cursor = connection.execute(
                'INSERT INTO highlight_clips('
                'marker_id,room_id,source_session_id,name,requested_start_ms,'
                'requested_end_ms,actual_start_ms,actual_end_ms,state,'
                'keyframe_confirmation_required,keyframe_confirmed,'
                'next_attempt_at,created_at,updated_at) '
                "VALUES(?,?,?,?,?,?,?,?,'queued',?,?,?,?,?)",
                (
                    marker_id,
                    room_id,
                    session_id,
                    normalized_name,
                    requested_start_ms,
                    requested_end_ms,
                    inspection.actual_start_ms,
                    inspection.actual_end_ms,
                    int(inspection.confirmation_required),
                    int(inspection.confirmation_required and confirm_keyframe),
                    0,
                    now,
                    now,
                ),
            )
            clip_id = int(cursor.lastrowid)
            output_directory = root / 'highlights' / str(room_id)
            output_video_path = output_directory / 'highlight-{}.mp4'.format(clip_id)
            output_xml_path = output_directory / 'highlight-{}.xml'.format(clip_id)
            connection.execute(
                'UPDATE highlight_clips SET output_video_path=?,output_xml_path=? '
                'WHERE id=?',
                (str(output_video_path), str(output_xml_path), clip_id),
            )
            for ordinal, (part, local_start_ms, local_end_ms) in enumerate(
                source_ranges, start=1
            ):
                inspected = inspected_by_part[part.part_id]
                connection.execute(
                    'INSERT INTO highlight_clip_sources('
                    'clip_id,part_id,ordinal,requested_start_ms,'
                    'requested_end_ms,actual_start_ms,actual_end_ms) '
                    'VALUES(?,?,?,?,?,?,?)',
                    (
                        clip_id,
                        part.part_id,
                        ordinal,
                        local_start_ms,
                        local_end_ms,
                        inspected.actual_start_ms,
                        inspected.actual_end_ms,
                    ),
                )
            return clip_id

        clip_id = await self._database.write(write)
        clip = await self.get_clip(clip_id)
        audit(
            'highlight_clip_queued',
            clip_id=clip_id,
            room_id=clip.room_id,
            session_id=session_id,
            requested_start_ms=requested_start_ms,
            requested_end_ms=requested_end_ms,
            actual_start_ms=inspection.actual_start_ms,
            actual_end_ms=inspection.actual_end_ms,
            source_part_ids=[source.part_id for source in clip.sources],
            confirmation_required=inspection.confirmation_required,
            confirmation_confirmed=clip.confirmed,
            result='queued',
        )
        return clip

    async def _prepare_clip(
        self,
        *,
        session_id: int,
        requested_start_ms: int,
        requested_end_ms: int,
        active_durations_ms: Mapping[int, int],
    ) -> Tuple[
        HighlightTimeline, Tuple[Tuple[TimelinePart, int, int], ...], ClipInspection
    ]:
        clipper = self._clipper
        if clipper is None:
            raise RuntimeError('highlight clipping is not configured')
        if requested_start_ms < 0 or requested_end_ms <= requested_start_ms:
            raise HighlightRangeUnavailable('高光剪辑时间范围无效')
        timeline = await self.timeline(session_id, active_durations_ms)
        if not timeline.parts:
            raise HighlightRangeUnavailable('本场没有可用的本地录像')
        if requested_end_ms > timeline.stable_end_ms:
            raise HighlightRangeUnavailable('所选范围进入录制中的最后 10 秒')
        source_ranges = self._resolve_clip_sources(
            timeline.parts, requested_start_ms, requested_end_ms
        )
        clip_sources = tuple(
            ClipSource(
                part_id=part.part_id,
                path=part.path,
                requested_start_ms=local_start_ms,
                requested_end_ms=local_end_ms,
                duration_ms=part.duration_ms,
            )
            for part, local_start_ms, local_end_ms in source_ranges
        )
        inspection = await asyncio.get_running_loop().run_in_executor(
            None,
            partial(
                clipper.inspect,
                clip_sources,
                requested_start_ms=requested_start_ms,
                requested_end_ms=requested_end_ms,
                stable_end_ms=timeline.stable_end_ms,
            ),
        )
        return timeline, source_ranges, inspection

    async def get_clip(self, clip_id: int) -> HighlightClip:
        row = await self._database.fetchone(
            'SELECT * FROM highlight_clips WHERE id=?', (clip_id,)
        )
        if row is None:
            raise ValueError("unknown highlight clip '{}'".format(clip_id))
        sources = await self._database.fetchall(
            'SELECT part_id,ordinal,requested_start_ms,requested_end_ms,'
            'actual_start_ms,actual_end_ms FROM highlight_clip_sources '
            'WHERE clip_id=? ORDER BY ordinal',
            (clip_id,),
        )
        return self._clip_from_row(
            row, tuple(self._clip_source_from_row(source) for source in sources)
        )

    async def clip_video_path(self, clip_id: int) -> Path:
        clip = await self.get_clip(clip_id)
        if clip.state != 'ready' or clip.output_video_path is None:
            raise ValueError('highlight clip is not ready')
        path = self._owned_highlight_path(clip.output_video_path)
        if not path.is_file() or path.stat().st_size <= 0:
            raise ValueError('highlight clip video is missing')
        return path

    async def delete_clip(self, clip_id: int) -> str:
        clip = await self.get_clip(clip_id)
        if clip.upload_session_id is not None:
            raise ValueError('highlight clip already has an upload task')
        if clip.state in ('queued', 'processing'):
            updated = await self._database.execute(
                "UPDATE highlight_clips SET state='cancelled',lease_owner=NULL,"
                'lease_until=NULL,next_attempt_at=0,updated_at=? WHERE id=? '
                "AND state IN ('queued','processing')",
                (int(self._clock()), clip_id),
            )
            if updated != 1:
                raise ValueError('highlight clip state changed')
            await self._remove_clip_outputs(clip, partial_only=True)
            audit(
                'highlight_clip_cancelled',
                clip_id=clip_id,
                room_id=clip.room_id,
                result='cancelled',
            )
            return 'cancelled'
        await self._remove_clip_outputs(clip, partial_only=False)
        deleted = await self._database.execute(
            'DELETE FROM highlight_clips WHERE id=?', (clip_id,)
        )
        if deleted != 1:
            raise ValueError('highlight clip state changed')
        audit(
            'highlight_clip_deleted',
            clip_id=clip_id,
            room_id=clip.room_id,
            result='deleted',
        )
        return 'deleted'

    async def ensure_upload_session(self, clip_id: int) -> int:
        clip = await self.get_clip(clip_id)
        if clip.upload_session_id is not None:
            return clip.upload_session_id
        if clip.state != 'ready' or clip.output_video_path is None:
            raise ValueError('highlight clip is not ready for upload')
        video_path = self._owned_highlight_path(clip.output_video_path)
        if not video_path.is_file() or video_path.stat().st_size <= 0:
            raise ValueError('highlight clip video is missing')
        xml_path: Optional[Path] = None
        if clip.output_xml_path is not None:
            candidate = self._owned_highlight_path(clip.output_xml_path)
            if candidate.is_file():
                xml_path = candidate
        now = int(self._clock())
        video_size = video_path.stat().st_size
        duration_seconds = max(
            1,
            int(
                round(
                    ((clip.actual_end_ms or 0) - (clip.actual_start_ms or 0)) / 1000.0
                )
            ),
        )

        def create(connection: sqlite3.Connection) -> int:
            current = connection.execute(
                'SELECT source_session_id,upload_session_id,state,'
                'output_video_path,output_xml_path FROM highlight_clips WHERE id=?',
                (clip_id,),
            ).fetchone()
            if current is None:
                raise ValueError('highlight clip does not exist')
            if current['upload_session_id'] is not None:
                return int(current['upload_session_id'])
            if (
                str(current['state']) != 'ready'
                or current['source_session_id'] is None
                or str(current['output_video_path']) != str(video_path)
            ):
                raise ValueError('highlight clip state changed')
            source = connection.execute(
                'SELECT room_id,live_start_time,started_at,title,cover_url,'
                'cover_path,anchor_uid,anchor_name,area_id,area_name,'
                'parent_area_id,parent_area_name,live_end_time '
                'FROM recording_sessions WHERE id=? AND source_kind=\'live\'',
                (int(current['source_session_id']),),
            ).fetchone()
            if source is None:
                raise ValueError('highlight source session does not exist')
            key = 'highlight:{}'.format(clip_id)
            existing = connection.execute(
                'SELECT id FROM recording_sessions WHERE broadcast_session_key=?',
                (key,),
            ).fetchone()
            if existing is None:
                cursor = connection.execute(
                    'INSERT INTO recording_sessions('
                    'room_id,broadcast_session_key,live_start_time,state,started_at,'
                    'ended_at,title,cover_url,cover_path,anchor_uid,anchor_name,'
                    'area_id,area_name,parent_area_id,parent_area_name,live_end_time,'
                    'upload_intent,source_kind) '
                    "VALUES(?,?,?,'closed',?,?,?,?,?,?,?,?,?,?,?,?,"
                    "'upload','highlight')",
                    (
                        int(source['room_id']),
                        key,
                        source['live_start_time'],
                        now,
                        now,
                        clip.name,
                        str(source['cover_url']),
                        source['cover_path'],
                        source['anchor_uid'],
                        str(source['anchor_name']),
                        source['area_id'],
                        str(source['area_name']),
                        source['parent_area_id'],
                        str(source['parent_area_name']),
                        source['live_end_time'],
                    ),
                )
                session_id = int(cursor.lastrowid)
                run_id = 'highlight:{}'.format(clip_id)
                connection.execute(
                    'INSERT INTO recording_runs('
                    'id,session_id,state,started_at,ended_at) '
                    "VALUES(?,?,'finished',?,?)",
                    (run_id, session_id, now, now),
                )
                record_start_time = int(source['started_at']) + int(
                    (clip.actual_start_ms or 0) / 1000
                )
                connection.execute(
                    'INSERT INTO recording_parts('
                    'session_id,run_id,part_index,source_path,final_path,xml_path,'
                    'record_start_time,record_end_time,record_duration_seconds,'
                    'file_size_bytes,danmaku_count,artifact_state,xml_completed,'
                    'created_at,updated_at) '
                    "VALUES(?,?,1,?,?,?,?,?,?,?,?, 'ready',?,?,?)",
                    (
                        session_id,
                        run_id,
                        str(video_path),
                        str(video_path),
                        None if xml_path is None else str(xml_path),
                        record_start_time,
                        record_start_time + duration_seconds,
                        duration_seconds,
                        video_size,
                        0,
                        int(xml_path is not None),
                        now,
                        now,
                    ),
                )
            else:
                session_id = int(existing['id'])
                valid = connection.execute(
                    "SELECT 1 FROM recording_sessions WHERE id=? "
                    "AND source_kind='highlight'",
                    (session_id,),
                ).fetchone()
                if valid is None:
                    raise ValueError('highlight upload session key conflicts')
            connection.execute(
                'UPDATE highlight_clips SET upload_session_id=?,updated_at=? '
                'WHERE id=? AND upload_session_id IS NULL',
                (session_id, now, clip_id),
            )
            return session_id

        session_id = await self._database.write(create)
        audit(
            'highlight_upload_session_created',
            clip_id=clip_id,
            session_id=session_id,
            room_id=clip.room_id,
            result='ready_for_draft',
        )
        return session_id

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

    async def _remove_clip_outputs(
        self, clip: HighlightClip, *, partial_only: bool
    ) -> None:
        paths = []
        for value in (clip.output_video_path, clip.output_xml_path):
            if value is None:
                continue
            path = self._owned_highlight_path(value)
            paths.append(Path(str(path) + '.partial'))
            if not partial_only:
                paths.append(path)

        def remove() -> None:
            for path in paths:
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass

        await asyncio.get_running_loop().run_in_executor(None, remove)

    def _owned_highlight_path(self, value: str) -> Path:
        if self._recording_root is None:
            raise ValueError('highlight recording root is not configured')
        root = (self._recording_root / 'highlights').resolve()
        path = Path(value).resolve(strict=False)
        try:
            path.relative_to(root)
        except ValueError:
            raise ValueError('highlight output path is outside recording root')
        if path.suffix.lower() not in ('.mp4', '.xml'):
            raise ValueError('invalid highlight output path')
        return path

    @staticmethod
    def _resolve_clip_sources(
        parts: Sequence[TimelinePart], start_ms: int, end_ms: int
    ) -> Tuple[Tuple[TimelinePart, int, int], ...]:
        sources = []
        for part in parts:
            part_start_ms = part.timeline_start_ms
            part_end_ms = part_start_ms + part.duration_ms
            intersection_start_ms = max(start_ms, part_start_ms)
            intersection_end_ms = min(end_ms, part_end_ms)
            if intersection_end_ms <= intersection_start_ms:
                continue
            if intersection_end_ms > part.stable_end_ms:
                raise HighlightRangeUnavailable('所选范围进入录制中的最后 10 秒')
            sources.append(
                (
                    part,
                    intersection_start_ms - part_start_ms,
                    intersection_end_ms - part_start_ms,
                )
            )
        if not sources:
            raise HighlightRangeUnavailable('所选范围没有对应的本地录像')
        if not any(
            part.timeline_start_ms
            <= start_ms
            < part.timeline_start_ms + part.duration_ms
            for part in parts
        ):
            raise HighlightRangeUnavailable('剪辑开始位置位于录像断档中')
        if not any(
            part.timeline_start_ms < end_ms <= part.timeline_start_ms + part.duration_ms
            for part in parts
        ):
            raise HighlightRangeUnavailable('剪辑结束位置位于录像断档中')
        return tuple(sources)

    @staticmethod
    def _default_name(title: str, content_at_ms: int) -> str:
        formatted = time.strftime('%H:%M:%S', time.localtime(content_at_ms / 1000.0))
        prefix = title.strip() or '直播'
        suffix = ' 高光 {}'.format(formatted)
        return '{}{}'.format(prefix[: 200 - len(suffix)], suffix)

    @staticmethod
    def _optional_nonnegative(value: Optional[int]) -> Optional[int]:
        if value is None:
            return None
        return min(604_800_000, max(0, int(value)))

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
            recording_part_id=(
                None
                if 'recording_part_id' not in row.keys()
                or row['recording_part_id'] is None
                else int(row['recording_part_id'])
            ),
            part_anchor_at_ms=(
                None
                if 'part_anchor_at_ms' not in row.keys()
                or row['part_anchor_at_ms'] is None
                else int(row['part_anchor_at_ms'])
            ),
            current_time_ms=(
                None
                if 'current_time_ms' not in row.keys() or row['current_time_ms'] is None
                else int(row['current_time_ms'])
            ),
            seekable_end_ms=(
                None
                if 'seekable_end_ms' not in row.keys() or row['seekable_end_ms'] is None
                else int(row['seekable_end_ms'])
            ),
            raw_delay_ms=(
                int(row['raw_delay_ms'])
                if 'raw_delay_ms' in row.keys()
                else int(row['player_delay_ms'])
            ),
            baseline_delay_ms=(
                int(row['baseline_delay_ms'])
                if 'baseline_delay_ms' in row.keys()
                else 0
            ),
            effective_rewind_ms=(
                int(row['effective_rewind_ms'])
                if 'effective_rewind_ms' in row.keys()
                else int(row['player_delay_ms'])
            ),
        )

    @staticmethod
    def _clip_source_from_row(row: sqlite3.Row) -> HighlightClipSource:
        return HighlightClipSource(
            part_id=int(row['part_id']),
            ordinal=int(row['ordinal']),
            requested_start_ms=int(row['requested_start_ms']),
            requested_end_ms=int(row['requested_end_ms']),
            actual_start_ms=(
                None if row['actual_start_ms'] is None else int(row['actual_start_ms'])
            ),
            actual_end_ms=(
                None if row['actual_end_ms'] is None else int(row['actual_end_ms'])
            ),
        )

    @staticmethod
    def _clip_from_row(
        row: sqlite3.Row, sources: Tuple[HighlightClipSource, ...] = ()
    ) -> HighlightClip:
        return HighlightClip(
            id=int(row['id']),
            marker_id=None if row['marker_id'] is None else int(row['marker_id']),
            room_id=int(row['room_id']),
            source_session_id=(
                None
                if row['source_session_id'] is None
                else int(row['source_session_id'])
            ),
            upload_session_id=(
                None
                if row['upload_session_id'] is None
                else int(row['upload_session_id'])
            ),
            name=str(row['name']),
            requested_start_ms=int(row['requested_start_ms']),
            requested_end_ms=int(row['requested_end_ms']),
            actual_start_ms=(
                None if row['actual_start_ms'] is None else int(row['actual_start_ms'])
            ),
            actual_end_ms=(
                None if row['actual_end_ms'] is None else int(row['actual_end_ms'])
            ),
            output_video_path=(
                None
                if row['output_video_path'] is None
                else str(row['output_video_path'])
            ),
            output_xml_path=(
                None if row['output_xml_path'] is None else str(row['output_xml_path'])
            ),
            state=str(row['state']),
            confirmation_required=bool(row['keyframe_confirmation_required']),
            confirmed=bool(row['keyframe_confirmed']),
            error_message=(
                None if row['error_message'] is None else str(row['error_message'])
            ),
            attempt=int(row['attempt']),
            created_at=int(row['created_at']),
            updated_at=int(row['updated_at']),
            sources=sources,
        )
