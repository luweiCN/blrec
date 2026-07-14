from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import time
import uuid
import xml.etree.ElementTree as ElementTree
from dataclasses import dataclass
from typing import (
    TYPE_CHECKING,
    Awaitable,
    Callable,
    Dict,
    List,
    Optional,
    Tuple,
    TypeVar,
)

from .database import BiliUploadDatabase

if TYPE_CHECKING:
    from blrec.core.recorder import Recorder
    from blrec.postprocess.postprocessor import Postprocessor


class JournalConsistencyError(RuntimeError):
    pass


@dataclass(frozen=True)
class RecordingSessionMetadata:
    title: str
    cover_url: str
    anchor_uid: int
    anchor_name: str
    area_id: int
    area_name: str
    parent_area_id: int
    parent_area_name: str


@dataclass(frozen=True)
class RecordingPart:
    id: int
    session_id: int
    run_id: str
    part_index: int
    source_path: str
    final_path: Optional[str]
    xml_path: Optional[str]
    record_start_time: int
    artifact_state: str
    xml_completed: bool
    source_exists: bool
    final_exists: bool
    error_message: Optional[str]
    record_end_time: Optional[int] = None
    record_duration_seconds: Optional[int] = None
    file_size_bytes: Optional[int] = None
    danmaku_count: int = 0


@dataclass(frozen=True)
class RecordingSession:
    id: int
    room_id: int
    broadcast_session_key: str
    live_start_time: Optional[int]
    state: str
    started_at: int
    ended_at: Optional[int]
    title: str = ''
    cover_url: str = ''
    cover_path: Optional[str] = None
    anchor_uid: Optional[int] = None
    anchor_name: str = ''
    area_id: Optional[int] = None
    area_name: str = ''
    parent_area_id: Optional[int] = None
    parent_area_name: str = ''
    live_end_time: Optional[int] = None
    parts: Tuple[RecordingPart, ...] = ()

    @property
    def part_count(self) -> int:
        return len(self.parts)

    @property
    def danmaku_count(self) -> int:
        return sum(part.danmaku_count for part in self.parts)

    @property
    def total_file_size_bytes(self) -> int:
        return sum(part.file_size_bytes or 0 for part in self.parts)

    @property
    def record_duration_seconds(self) -> int:
        return sum(part.record_duration_seconds or 0 for part in self.parts)


_T = TypeVar('_T')


class RecordingJournalBridge:
    def __init__(
        self,
        database: BiliUploadDatabase,
        *,
        clock: Callable[[], float] = time.time,
        uuid_factory: Callable[[], str] = lambda: str(uuid.uuid4()),
    ) -> None:
        self._database = database
        self._clock = clock
        self._uuid_factory = uuid_factory
        self._degraded_reason: Optional[str] = None

    @property
    def degraded_reason(self) -> Optional[str]:
        return self._degraded_reason

    def pause_automation(self, error: BaseException) -> None:
        self._degraded_reason = '{}: {}'.format(type(error).__name__, error)

    async def recording_started(
        self,
        room_id: int,
        *,
        live_start_time: int,
        metadata: Optional[RecordingSessionMetadata] = None,
        event_id: Optional[str] = None,
    ) -> str:
        now = int(self._clock())
        run_id = self._uuid_factory()
        journal_id = self._new_event_id(event_id)

        def write(connection: sqlite3.Connection) -> str:
            replayed = connection.execute(
                'SELECT event_type,run_id FROM event_journal WHERE id=?', (journal_id,)
            ).fetchone()
            if replayed is not None:
                if (
                    replayed['event_type'] != 'recording_started'
                    or not replayed['run_id']
                ):
                    raise JournalConsistencyError(
                        "event '{}' has conflicting content".format(journal_id)
                    )
                return str(replayed['run_id'])
            if live_start_time > 0:
                key = '{}:{}'.format(room_id, live_start_time)
                row = connection.execute(
                    'SELECT id,broadcast_session_key FROM recording_sessions '
                    'WHERE broadcast_session_key=?',
                    (key,),
                ).fetchone()
            else:
                row = connection.execute(
                    'SELECT id,broadcast_session_key FROM recording_sessions '
                    'WHERE room_id=? AND live_start_time IS NULL AND state=? '
                    'ORDER BY id DESC LIMIT 1',
                    (room_id, 'open'),
                ).fetchone()
                key = (
                    '{}:local:{}'.format(room_id, self._uuid_factory())
                    if row is None
                    else str(row['broadcast_session_key'])
                )
            if row is None:
                cursor = connection.execute(
                    'INSERT INTO recording_sessions('
                    'room_id,broadcast_session_key,live_start_time,state,started_at,'
                    'title,cover_url,anchor_uid,anchor_name,area_id,area_name,'
                    'parent_area_id,parent_area_name) '
                    'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    (
                        room_id,
                        key,
                        live_start_time or None,
                        'open',
                        now,
                        '' if metadata is None else metadata.title,
                        '' if metadata is None else metadata.cover_url,
                        None if metadata is None else metadata.anchor_uid,
                        '' if metadata is None else metadata.anchor_name,
                        None if metadata is None else metadata.area_id,
                        '' if metadata is None else metadata.area_name,
                        None if metadata is None else metadata.parent_area_id,
                        '' if metadata is None else metadata.parent_area_name,
                    ),
                )
                session_id = int(cursor.lastrowid)
            else:
                session_id = int(row['id'])
                connection.execute(
                    "UPDATE recording_sessions SET state='open',ended_at=NULL,"
                    'live_end_time=NULL,'
                    "title=CASE WHEN title='' THEN ? ELSE title END,"
                    "cover_url=CASE WHEN cover_url='' THEN ? ELSE cover_url END,"
                    'anchor_uid=COALESCE(anchor_uid,?),'
                    "anchor_name=CASE WHEN anchor_name='' THEN ? ELSE anchor_name END,"
                    'area_id=COALESCE(area_id,?),'
                    "area_name=CASE WHEN area_name='' THEN ? ELSE area_name END,"
                    'parent_area_id=COALESCE(parent_area_id,?),'
                    'parent_area_name=CASE WHEN parent_area_name=\'\' THEN ? '
                    'ELSE parent_area_name END WHERE id=?',
                    (
                        '' if metadata is None else metadata.title,
                        '' if metadata is None else metadata.cover_url,
                        None if metadata is None else metadata.anchor_uid,
                        '' if metadata is None else metadata.anchor_name,
                        None if metadata is None else metadata.area_id,
                        '' if metadata is None else metadata.area_name,
                        None if metadata is None else metadata.parent_area_id,
                        '' if metadata is None else metadata.parent_area_name,
                        session_id,
                    ),
                )
            connection.execute(
                'INSERT INTO recording_runs(id,session_id,state,started_at) '
                "VALUES(?,?,'recording',?)",
                (run_id, session_id, now),
            )
            self._insert_event(
                connection,
                journal_id,
                'recording_started',
                room_id,
                run_id,
                None,
                {'live_start_time': live_start_time},
                now,
            )
            return run_id

        return await self._database.write(write)

    async def cover_downloaded(
        self, run_id: str, path: str, *, event_id: Optional[str] = None
    ) -> None:
        now = int(self._clock())
        cover_path = self._normalize_path(path)
        journal_id = self._new_event_id(event_id)

        def write(connection: sqlite3.Connection) -> None:
            if self._event_was_recorded(connection, journal_id, 'cover_downloaded'):
                return
            session_id = self._session_id_for_run(connection, run_id)
            room_id = self._room_id_for_run(connection, run_id)
            connection.execute(
                'UPDATE recording_sessions SET cover_path=COALESCE(cover_path,?) '
                'WHERE id=?',
                (cover_path, session_id),
            )
            self._insert_event(
                connection,
                journal_id,
                'cover_downloaded',
                room_id,
                run_id,
                cover_path,
                {},
                now,
            )

        await self._database.write(write)

    async def reconcile_open_sessions(self) -> None:
        now = int(self._clock())

        def write(connection: sqlite3.Connection) -> None:
            sessions = connection.execute(
                'SELECT id,state FROM recording_sessions '
                "WHERE state IN ('open','cancelled')"
            ).fetchall()
            for session in sessions:
                session_id = int(session['id'])
                original_state = str(session['state'])
                stale_run_count = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM recording_runs WHERE session_id=? "
                        "AND state='recording'",
                        (session_id,),
                    ).fetchone()[0]
                )
                connection.execute(
                    "UPDATE recording_runs SET state='cancelled',ended_at=? "
                    "WHERE session_id=? AND state='recording'",
                    (now, session_id),
                )

                parts = connection.execute(
                    'SELECT id,source_path,final_path,artifact_state '
                    'FROM recording_parts WHERE session_id=?',
                    (session_id,),
                ).fetchall()
                for part in parts:
                    artifact_state = str(part['artifact_state'])
                    reconciled_state: Optional[str] = None
                    if artifact_state in ('recording', 'postprocessing'):
                        source_exists = os.path.exists(str(part['source_path']))
                        final_exists = part[
                            'final_path'
                        ] is not None and os.path.exists(str(part['final_path']))
                        reconciled_state = (
                            'manual_review'
                            if source_exists or final_exists
                            else 'missing'
                        )
                    elif artifact_state == 'ready':
                        final_path = part['final_path']
                        if final_path is None or not os.path.exists(str(final_path)):
                            reconciled_state = 'missing'
                    if reconciled_state is not None:
                        connection.execute(
                            'UPDATE recording_parts SET artifact_state=?,updated_at=? '
                            'WHERE id=?',
                            (reconciled_state, now, int(part['id'])),
                        )

                if original_state == 'cancelled':
                    continue
                part_states = {
                    str(row['artifact_state'])
                    for row in connection.execute(
                        'SELECT artifact_state FROM recording_parts '
                        'WHERE session_id=?',
                        (session_id,),
                    ).fetchall()
                }
                if part_states & {'manual_review', 'missing'}:
                    state = 'manual_review'
                elif stale_run_count:
                    state = 'cancelled'
                else:
                    run_states = {
                        str(row['state'])
                        for row in connection.execute(
                            'SELECT state FROM recording_runs WHERE session_id=?',
                            (session_id,),
                        ).fetchall()
                    }
                    if (
                        run_states
                        and run_states <= {'finished'}
                        and part_states <= {'ready', 'failed'}
                    ):
                        state = 'closed'
                    else:
                        state = 'manual_review'
                connection.execute(
                    'UPDATE recording_sessions SET state=?,ended_at=? WHERE id=?',
                    (state, now, session_id),
                )

        await self._database.write(write)

    async def video_created(
        self,
        run_id: str,
        path: str,
        *,
        record_start_time: int,
        event_id: Optional[str] = None,
    ) -> None:
        now = int(self._clock())
        source_path = self._normalize_path(path)
        journal_id = self._new_event_id(event_id)

        def write(connection: sqlite3.Connection) -> None:
            if self._event_was_recorded(connection, journal_id, 'video_created'):
                return
            row = connection.execute(
                'SELECT run.session_id,session.room_id '
                'FROM recording_runs run '
                'JOIN recording_sessions session ON session.id=run.session_id '
                'WHERE run.id=?',
                (run_id,),
            ).fetchone()
            if row is None:
                raise JournalConsistencyError(
                    "unknown recording run '{}'".format(run_id)
                )
            session_id = int(row['session_id'])
            existing = connection.execute(
                'SELECT id FROM recording_parts WHERE run_id=? AND source_path=?',
                (run_id, source_path),
            ).fetchone()
            if existing is None:
                part_index = int(
                    connection.execute(
                        'SELECT COALESCE(MAX(part_index),0)+1 '
                        'FROM recording_parts WHERE session_id=?',
                        (session_id,),
                    ).fetchone()[0]
                )
                connection.execute(
                    'INSERT INTO recording_parts('
                    'session_id,run_id,part_index,source_path,record_start_time,'
                    'artifact_state,created_at,updated_at) '
                    "VALUES(?,?,?,?,?,'recording',?,?)",
                    (
                        session_id,
                        run_id,
                        part_index,
                        source_path,
                        int(record_start_time),
                        now,
                        now,
                    ),
                )
            self._insert_event(
                connection,
                journal_id,
                'video_created',
                int(row['room_id']),
                run_id,
                source_path,
                {'record_start_time': int(record_start_time)},
                now,
            )

        await self._database.write(write)

    async def video_completed(
        self, run_id: str, path: str, *, event_id: Optional[str] = None
    ) -> None:
        now = int(self._clock())
        source_path = self._normalize_path(path)
        journal_id = self._new_event_id(event_id)

        def write(connection: sqlite3.Connection) -> None:
            if self._event_was_recorded(connection, journal_id, 'video_completed'):
                return
            room_id = self._room_id_for_run(connection, run_id)
            cursor = connection.execute(
                'UPDATE recording_parts SET artifact_state=?,source_completed_at=?,'
                'record_end_time=?,record_duration_seconds='
                'MAX(0,?-record_start_time),updated_at=? '
                'WHERE run_id=? AND source_path=?',
                ('postprocessing', now, now, now, now, run_id, source_path),
            )
            if cursor.rowcount != 1:
                raise JournalConsistencyError(
                    "unknown recording part '{}'".format(path)
                )
            self._insert_event(
                connection,
                journal_id,
                'video_completed',
                room_id,
                run_id,
                source_path,
                {},
                now,
            )

        await self._database.write(write)

    async def video_postprocessed(
        self,
        run_id: str,
        source_path: str,
        final_path: str,
        *,
        event_id: Optional[str] = None,
    ) -> None:
        now = int(self._clock())
        source = self._normalize_path(source_path)
        final = self._normalize_path(final_path)
        journal_id = self._new_event_id(event_id)
        loop = asyncio.get_running_loop()
        file_size_bytes = await loop.run_in_executor(
            None, self._file_size_or_none, final
        )

        def write(connection: sqlite3.Connection) -> None:
            if self._event_was_recorded(connection, journal_id, 'video_postprocessed'):
                return
            room_id = self._room_id_for_run(connection, run_id)
            cursor = connection.execute(
                'UPDATE recording_parts SET artifact_state=?,final_path=?,'
                'file_size_bytes=?,postprocessed_at=?,updated_at=? '
                'WHERE run_id=? AND source_path=?',
                ('ready', final, file_size_bytes, now, now, run_id, source),
            )
            if cursor.rowcount != 1:
                raise JournalConsistencyError(
                    "unknown recording part '{}'".format(source_path)
                )
            session_id = self._session_id_for_run(connection, run_id)
            self._refresh_session_state(connection, session_id, now)
            self._insert_event(
                connection,
                journal_id,
                'video_postprocessed',
                room_id,
                run_id,
                final,
                {'source_path': source},
                now,
            )

        await self._database.write(write)

    async def recording_cancelled(
        self, run_id: str, *, event_id: Optional[str] = None
    ) -> None:
        now = int(self._clock())
        journal_id = self._new_event_id(event_id)

        def write(connection: sqlite3.Connection) -> None:
            if self._event_was_recorded(connection, journal_id, 'recording_cancelled'):
                return
            row = connection.execute(
                'SELECT run.session_id,session.room_id '
                'FROM recording_runs run '
                'JOIN recording_sessions session ON session.id=run.session_id '
                'WHERE run.id=?',
                (run_id,),
            ).fetchone()
            if row is None:
                raise JournalConsistencyError(
                    "unknown recording run '{}'".format(run_id)
                )
            connection.execute(
                "UPDATE recording_runs SET state='cancelled',ended_at=? WHERE id=?",
                (now, run_id),
            )
            connection.execute(
                "UPDATE recording_sessions SET state='cancelled',ended_at=? "
                'WHERE id=?',
                (now, int(row['session_id'])),
            )
            self._insert_event(
                connection,
                journal_id,
                'recording_cancelled',
                int(row['room_id']),
                run_id,
                None,
                {},
                now,
            )

        await self._database.write(write)

    async def recording_finished(
        self, run_id: str, *, event_id: Optional[str] = None
    ) -> None:
        now = int(self._clock())
        journal_id = self._new_event_id(event_id)

        def write(connection: sqlite3.Connection) -> None:
            if self._event_was_recorded(connection, journal_id, 'recording_finished'):
                return
            session_id = self._session_id_for_run(connection, run_id)
            room_id = self._room_id_for_run(connection, run_id)
            connection.execute(
                "UPDATE recording_runs SET state='finished',ended_at=? WHERE id=?",
                (now, run_id),
            )
            connection.execute(
                'UPDATE recording_sessions SET live_end_time=? WHERE id=?',
                (now, session_id),
            )
            self._refresh_session_state(connection, session_id, now)
            self._insert_event(
                connection,
                journal_id,
                'recording_finished',
                room_id,
                run_id,
                None,
                {},
                now,
            )

        await self._database.write(write)

    async def video_postprocessing_failed(
        self,
        run_id: str,
        source_path: str,
        error: BaseException,
        *,
        event_id: Optional[str] = None,
    ) -> None:
        now = int(self._clock())
        source = self._normalize_path(source_path)
        journal_id = self._new_event_id(event_id)
        message = '{}: {}'.format(type(error).__name__, error)[:500]

        def write(connection: sqlite3.Connection) -> None:
            if self._event_was_recorded(
                connection, journal_id, 'video_postprocessing_failed'
            ):
                return
            room_id = self._room_id_for_run(connection, run_id)
            cursor = connection.execute(
                'UPDATE recording_parts SET artifact_state=?,error_message=?,'
                'postprocessed_at=?,updated_at=? '
                'WHERE run_id=? AND source_path=?',
                ('failed', message, now, now, run_id, source),
            )
            if cursor.rowcount != 1:
                raise JournalConsistencyError(
                    "unknown recording part '{}'".format(source_path)
                )
            session_id = self._session_id_for_run(connection, run_id)
            self._refresh_session_state(connection, session_id, now)
            self._insert_event(
                connection,
                journal_id,
                'video_postprocessing_failed',
                room_id,
                run_id,
                source,
                {'error': message},
                now,
            )

        await self._database.write(write)

    async def danmaku_completed(
        self, run_id: str, path: str, *, event_id: Optional[str] = None
    ) -> None:
        now = int(self._clock())
        xml_path = self._normalize_path(path)
        journal_id = self._new_event_id(event_id)
        loop = asyncio.get_running_loop()
        danmaku_count = await loop.run_in_executor(
            None, self._count_danmaku_sync, xml_path
        )

        def write(connection: sqlite3.Connection) -> None:
            if self._event_was_recorded(connection, journal_id, 'danmaku_completed'):
                return
            room_id = self._room_id_for_run(connection, run_id)
            rows = connection.execute(
                'SELECT id,source_path FROM recording_parts '
                'WHERE run_id=? ORDER BY part_index',
                (run_id,),
            ).fetchall()
            stem = os.path.splitext(xml_path)[0]
            matches = [
                row
                for row in rows
                if os.path.splitext(str(row['source_path']))[0] == stem
            ]
            if not matches and len(rows) == 1:
                matches = list(rows)
            if len(matches) != 1:
                raise JournalConsistencyError(
                    "cannot bind danmaku file '{}' to one recording part".format(path)
                )
            connection.execute(
                'UPDATE recording_parts SET xml_path=?,xml_completed=1,'
                'danmaku_count=?,updated_at=? '
                'WHERE id=?',
                (xml_path, danmaku_count, now, int(matches[0]['id'])),
            )
            self._insert_event(
                connection,
                journal_id,
                'danmaku_completed',
                room_id,
                run_id,
                xml_path,
                {},
                now,
            )

        await self._database.write(write)

    async def session_for_run(self, run_id: str) -> RecordingSession:
        row = await self._database.fetchone(
            'SELECT session.id,session.room_id,session.broadcast_session_key,'
            'session.live_start_time,session.state,session.started_at,'
            'session.ended_at,session.title,session.cover_url,session.cover_path,'
            'session.anchor_uid,session.anchor_name,session.area_id,'
            'session.area_name,session.parent_area_id,session.parent_area_name,'
            'session.live_end_time FROM recording_sessions session '
            'JOIN recording_runs run ON run.session_id=session.id '
            'WHERE run.id=?',
            (run_id,),
        )
        if row is None:
            raise ValueError("unknown recording run '{}'".format(run_id))
        return self._make_session(row, await self.parts_for_session(int(row['id'])))

    async def list_sessions(self, *, limit: int = 50) -> Tuple[RecordingSession, ...]:
        if limit < 1 or limit > 200:
            raise ValueError('limit must be between 1 and 200')
        rows = await self._database.fetchall(
            'SELECT id,room_id,broadcast_session_key,live_start_time,state,'
            'started_at,ended_at,title,cover_url,cover_path,anchor_uid,'
            'anchor_name,area_id,area_name,parent_area_id,parent_area_name,'
            'live_end_time FROM recording_sessions '
            'ORDER BY started_at DESC,id DESC LIMIT ?',
            (limit,),
        )
        sessions = []
        for row in rows:
            session_id = int(row['id'])
            sessions.append(
                self._make_session(row, await self.parts_for_session(session_id))
            )
        return tuple(sessions)

    async def run_id_for_source(self, source_path: str) -> str:
        rows = await self._database.fetchall(
            'SELECT run_id FROM recording_parts WHERE source_path=? '
            'ORDER BY id DESC LIMIT 2',
            (self._normalize_path(source_path),),
        )
        if len(rows) != 1:
            raise JournalConsistencyError(
                "cannot identify one run for '{}'".format(source_path)
            )
        return str(rows[0]['run_id'])

    async def parts_for_run(self, run_id: str) -> Tuple[RecordingPart, ...]:
        rows = await self._database.fetchall(
            'SELECT id,session_id,run_id,part_index,source_path,final_path,'
            'xml_path,record_start_time,record_end_time,record_duration_seconds,'
            'file_size_bytes,danmaku_count,artifact_state,xml_completed,'
            'error_message '
            'FROM recording_parts WHERE run_id=? ORDER BY part_index',
            (run_id,),
        )
        return tuple(self._make_part(row) for row in rows)

    async def parts_for_session(self, session_id: int) -> Tuple[RecordingPart, ...]:
        rows = await self._database.fetchall(
            'SELECT id,session_id,run_id,part_index,source_path,final_path,'
            'xml_path,record_start_time,record_end_time,record_duration_seconds,'
            'file_size_bytes,danmaku_count,artifact_state,xml_completed,'
            'error_message '
            'FROM recording_parts WHERE session_id=? ORDER BY part_index',
            (session_id,),
        )
        return tuple(self._make_part(row) for row in rows)

    @staticmethod
    def _make_session(
        row: sqlite3.Row, parts: Tuple[RecordingPart, ...] = ()
    ) -> RecordingSession:
        return RecordingSession(
            id=int(row['id']),
            room_id=int(row['room_id']),
            broadcast_session_key=str(row['broadcast_session_key']),
            live_start_time=(
                None if row['live_start_time'] is None else int(row['live_start_time'])
            ),
            state=str(row['state']),
            started_at=int(row['started_at']),
            ended_at=None if row['ended_at'] is None else int(row['ended_at']),
            title=str(row['title']),
            cover_url=str(row['cover_url']),
            cover_path=(None if row['cover_path'] is None else str(row['cover_path'])),
            anchor_uid=(None if row['anchor_uid'] is None else int(row['anchor_uid'])),
            anchor_name=str(row['anchor_name']),
            area_id=None if row['area_id'] is None else int(row['area_id']),
            area_name=str(row['area_name']),
            parent_area_id=(
                None if row['parent_area_id'] is None else int(row['parent_area_id'])
            ),
            parent_area_name=str(row['parent_area_name']),
            live_end_time=(
                None if row['live_end_time'] is None else int(row['live_end_time'])
            ),
            parts=parts,
        )

    @staticmethod
    def _make_part(row: sqlite3.Row) -> RecordingPart:
        final_path = None if row['final_path'] is None else str(row['final_path'])
        return RecordingPart(
            id=int(row['id']),
            session_id=int(row['session_id']),
            run_id=str(row['run_id']),
            part_index=int(row['part_index']),
            source_path=str(row['source_path']),
            final_path=final_path,
            xml_path=None if row['xml_path'] is None else str(row['xml_path']),
            record_start_time=int(row['record_start_time']),
            artifact_state=str(row['artifact_state']),
            xml_completed=bool(row['xml_completed']),
            source_exists=os.path.exists(str(row['source_path'])),
            final_exists=final_path is not None and os.path.exists(final_path),
            error_message=(
                None if row['error_message'] is None else str(row['error_message'])
            ),
            record_end_time=(
                None if row['record_end_time'] is None else int(row['record_end_time'])
            ),
            record_duration_seconds=(
                None
                if row['record_duration_seconds'] is None
                else int(row['record_duration_seconds'])
            ),
            file_size_bytes=(
                None if row['file_size_bytes'] is None else int(row['file_size_bytes'])
            ),
            danmaku_count=int(row['danmaku_count']),
        )

    def _new_event_id(self, event_id: Optional[str]) -> str:
        return self._uuid_factory() if event_id is None else event_id

    @staticmethod
    def _event_was_recorded(
        connection: sqlite3.Connection, event_id: str, expected_type: str
    ) -> bool:
        row = connection.execute(
            'SELECT event_type FROM event_journal WHERE id=?', (event_id,)
        ).fetchone()
        if row is None:
            return False
        if row['event_type'] != expected_type:
            raise JournalConsistencyError(
                "event '{}' has conflicting content".format(event_id)
            )
        return True

    @staticmethod
    def _insert_event(
        connection: sqlite3.Connection,
        event_id: str,
        event_type: str,
        room_id: int,
        run_id: str,
        path: Optional[str],
        payload: object,
        occurred_at: int,
    ) -> None:
        connection.execute(
            'INSERT INTO event_journal('
            'id,event_type,room_id,run_id,path,payload_json,occurred_at,consumed_at) '
            'VALUES(?,?,?,?,?,?,?,?)',
            (
                event_id,
                event_type,
                room_id,
                run_id,
                path,
                json.dumps(payload, ensure_ascii=False, sort_keys=True),
                occurred_at,
                occurred_at,
            ),
        )

    @staticmethod
    def _session_id_for_run(connection: sqlite3.Connection, run_id: str) -> int:
        row = connection.execute(
            'SELECT session_id FROM recording_runs WHERE id=?', (run_id,)
        ).fetchone()
        if row is None:
            raise JournalConsistencyError("unknown recording run '{}'".format(run_id))
        return int(row['session_id'])

    @staticmethod
    def _room_id_for_run(connection: sqlite3.Connection, run_id: str) -> int:
        row = connection.execute(
            'SELECT session.room_id FROM recording_sessions session '
            'JOIN recording_runs run ON run.session_id=session.id WHERE run.id=?',
            (run_id,),
        ).fetchone()
        if row is None:
            raise JournalConsistencyError("unknown recording run '{}'".format(run_id))
        return int(row['room_id'])

    @staticmethod
    def _refresh_session_state(
        connection: sqlite3.Connection, session_id: int, now: int
    ) -> None:
        session = connection.execute(
            'SELECT state FROM recording_sessions WHERE id=?', (session_id,)
        ).fetchone()
        if session is None:
            raise JournalConsistencyError(
                "unknown recording session '{}'".format(session_id)
            )
        if session['state'] in ('cancelled', 'manual_review', 'skipped'):
            return
        recording_runs = int(
            connection.execute(
                "SELECT COUNT(*) FROM recording_runs WHERE session_id=? "
                "AND state='recording'",
                (session_id,),
            ).fetchone()[0]
        )
        if recording_runs:
            connection.execute(
                "UPDATE recording_sessions SET state='open',ended_at=NULL "
                'WHERE id=?',
                (session_id,),
            )
            return
        states = {
            str(row['artifact_state'])
            for row in connection.execute(
                'SELECT artifact_state FROM recording_parts WHERE session_id=?',
                (session_id,),
            ).fetchall()
        }
        if states & {'manual_review', 'missing'}:
            state = 'manual_review'
        elif states <= {'ready', 'failed'}:
            state = 'closed'
        else:
            state = 'open'
        connection.execute(
            'UPDATE recording_sessions SET state=?,ended_at=? WHERE id=?',
            (state, now if state != 'open' else None, session_id),
        )

    @staticmethod
    def _normalize_path(path: str) -> str:
        return os.path.abspath(os.path.expanduser(path))

    @staticmethod
    def _file_size_or_none(path: str) -> Optional[int]:
        try:
            return os.path.getsize(path)
        except OSError:
            return None

    @staticmethod
    def _count_danmaku_sync(path: str) -> int:
        count = 0
        for _, element in ElementTree.iterparse(path, events=('end',)):
            if element.tag.rsplit('}', 1)[-1] == 'd':
                count += 1
            element.clear()
        return count


class RecordingJournalListener:
    def __init__(
        self,
        journal: RecordingJournalBridge,
        recorder: Recorder,
        postprocessor: Postprocessor,
    ) -> None:
        self._journal = journal
        self._recorder = recorder
        self._postprocessor = postprocessor
        self._current_run_id: Optional[str] = None
        self._source_runs: Dict[str, str] = {}
        recorder.add_listener(self)  # type: ignore[arg-type]
        postprocessor.add_listener(self)  # type: ignore[arg-type]

    def close(self) -> None:
        self._recorder.remove_listener(self)  # type: ignore[arg-type]
        self._postprocessor.remove_listener(self)  # type: ignore[arg-type]

    async def on_recording_started(self, recorder: Recorder) -> None:
        room_info = recorder.live.room_info
        user_info = recorder.live.user_info
        self._current_run_id = await self._guard(
            self._journal.recording_started(
                int(room_info.room_id),
                live_start_time=int(room_info.live_start_time),
                metadata=RecordingSessionMetadata(
                    title=str(room_info.title),
                    cover_url=str(room_info.cover),
                    anchor_uid=int(user_info.uid),
                    anchor_name=str(user_info.name),
                    area_id=int(room_info.area_id),
                    area_name=str(room_info.area_name),
                    parent_area_id=int(room_info.parent_area_id),
                    parent_area_name=str(room_info.parent_area_name),
                ),
            )
        )

    async def on_recording_finished(self, recorder: Recorder) -> None:
        run_id = self._require_current_run()
        await self._guard(self._journal.recording_finished(run_id))
        self._current_run_id = None

    async def on_recording_cancelled(self, recorder: Recorder) -> None:
        run_id = self._require_current_run()
        await self._guard(self._journal.recording_cancelled(run_id))
        self._current_run_id = None

    async def on_video_file_created(self, recorder: Recorder, path: str) -> None:
        run_id = self._require_current_run()
        record_start_time = recorder.record_start_time
        if record_start_time is None:
            error = JournalConsistencyError('video file has no record start time')
            self._journal.pause_automation(error)
            raise error
        await self._guard(
            self._journal.video_created(
                run_id, path, record_start_time=int(record_start_time)
            )
        )
        self._source_runs[self._normalize_path(path)] = run_id

    async def on_video_file_completed(self, recorder: Recorder, path: str) -> None:
        run_id = await self._run_for_source(path)
        await self._guard(self._journal.video_completed(run_id, path))

    async def on_danmaku_file_created(self, recorder: Recorder, path: str) -> None:
        return None

    async def on_danmaku_file_completed(self, recorder: Recorder, path: str) -> None:
        run_id = self._require_current_run()
        await self._guard(self._journal.danmaku_completed(run_id, path))

    async def on_raw_danmaku_file_created(self, recorder: Recorder, path: str) -> None:
        return None

    async def on_raw_danmaku_file_completed(
        self, recorder: Recorder, path: str
    ) -> None:
        return None

    async def on_cover_image_downloaded(self, recorder: Recorder, path: str) -> None:
        run_id = self._require_current_run()
        await self._guard(self._journal.cover_downloaded(run_id, path))

    async def on_video_postprocessing_completed(
        self, postprocessor: Postprocessor, path: str
    ) -> None:
        return None

    async def on_video_postprocessing_result(
        self, postprocessor: Postprocessor, source_path: str, result_path: str
    ) -> None:
        run_id = await self._run_for_source(source_path)
        await self._guard(
            self._journal.video_postprocessed(run_id, source_path, result_path)
        )
        self._source_runs.pop(self._normalize_path(source_path), None)

    async def on_video_postprocessing_failed(
        self, postprocessor: Postprocessor, source_path: str, error: BaseException
    ) -> None:
        run_id = await self._run_for_source(source_path)
        await self._guard(
            self._journal.video_postprocessing_failed(run_id, source_path, error)
        )
        self._source_runs.pop(self._normalize_path(source_path), None)

    async def on_postprocessing_completed(
        self, postprocessor: Postprocessor, files: List[str]
    ) -> None:
        return None

    async def _run_for_source(self, path: str) -> str:
        normalized = self._normalize_path(path)
        run_id = self._source_runs.get(normalized)
        if run_id is not None:
            return run_id
        return await self._guard(self._journal.run_id_for_source(normalized))

    async def _guard(self, operation: Awaitable[_T]) -> _T:
        try:
            return await operation
        except Exception as error:
            self._journal.pause_automation(error)
            raise

    def _require_current_run(self) -> str:
        if self._current_run_id is None:
            error = JournalConsistencyError('recording event has no active run')
            self._journal.pause_automation(error)
            raise error
        return self._current_run_id

    @staticmethod
    def _normalize_path(path: str) -> str:
        return os.path.abspath(os.path.expanduser(path))
