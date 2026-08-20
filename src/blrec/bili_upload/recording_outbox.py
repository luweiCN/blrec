from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import stat
import time
import uuid
import xml.etree.ElementTree as ElementTree
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, replace
from functools import partial
from pathlib import Path
from typing import (
    Any,
    Awaitable,
    Callable,
    List,
    Mapping,
    Optional,
    Protocol,
    Tuple,
    TypeVar,
)

from loguru import logger

from .artifact_recovery import RecoveredArtifact, probe_recording_artifact
from .journal import JournalConsistencyError, RecordingSessionMetadata

_T = TypeVar('_T')

RECORDING_EVENT_TYPES: Tuple[str, ...] = (
    'recording_started',
    'recording_finished',
    'recording_cancelled',
    'video_created',
    'video_completed',
    'video_postprocessed',
    'video_postprocessing_failed',
    'danmaku_completed',
    'cover_downloaded',
)


class RecordingOutboxError(RuntimeError):
    pass


class RecordingOutboxClosed(RecordingOutboxError):
    pass


class RecordingOutboxConsistencyError(RecordingOutboxError):
    pass


@dataclass(frozen=True)
class RecordingOutboxEvent:
    event_id: str
    event_type: str
    run_id: str
    room_id: int
    path: Optional[str]
    payload: Mapping[str, Any]
    occurred_at: float
    sequence: int = 0
    synced_at: Optional[float] = None
    attempt_count: int = 0
    last_attempt_at: Optional[float] = None
    last_error: Optional[str] = None


@dataclass(frozen=True)
class RecordingOutboxStatus:
    pending_count: int
    oldest_pending_at: Optional[float]
    last_synced_at: Optional[float]
    last_error: Optional[str]
    attempt_count: int


class RecordingEventProjector(Protocol):
    async def apply_recording_event(self, event: RecordingOutboxEvent) -> None:
        pass


class RemoteRecordingRuntime(Protocol):
    @property
    def journal(self) -> Optional[RecordingEventProjector]:
        pass

    async def start(self) -> bool:
        pass


class RecordingOutboxSynchronizer:
    def __init__(
        self,
        outbox: LocalRecordingOutbox,
        projector_provider: Callable[[], Optional[RecordingEventProjector]],
        *,
        retry_interval_seconds: float = 5,
    ) -> None:
        if retry_interval_seconds <= 0:
            raise ValueError('retry interval must be positive')
        self._outbox = outbox
        self._projector_provider = projector_provider
        self._retry_interval_seconds = retry_interval_seconds
        self._wake_event = asyncio.Event()
        self._task: Optional[asyncio.Task[None]] = None

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self._run())

    def wake(self) -> None:
        self._wake_event.set()

    async def close(self) -> None:
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def sync_one(self) -> bool:
        events = await self._outbox.pending_events(limit=1)
        if not events:
            return False
        projector = self._projector_provider()
        if projector is None:
            return False
        return await self._project(events[0], projector, propagate_error=False)

    async def drain(self, projector: RecordingEventProjector) -> int:
        projected = 0
        while True:
            events = await self._outbox.pending_events(limit=1)
            if not events:
                return projected
            await self._project(events[0], projector, propagate_error=True)
            projected += 1

    async def _project(
        self,
        event: RecordingOutboxEvent,
        projector: RecordingEventProjector,
        *,
        propagate_error: bool,
    ) -> bool:
        try:
            await projector.apply_recording_event(event)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            await self._outbox.mark_attempt(
                event.sequence, '{}: {}'.format(type(error).__name__, error)
            )
            logger.warning(
                'Recording outbox event sync failed: sequence={}, event_id={}, '
                'event_type={}, error={!r}',
                event.sequence,
                event.event_id,
                event.event_type,
                error,
            )
            if propagate_error:
                raise
            return False
        await self._outbox.mark_synced(event.sequence)
        return True

    async def _run(self) -> None:
        while True:
            self._wake_event.clear()
            if await self.sync_one():
                continue
            try:
                await asyncio.wait_for(
                    self._wake_event.wait(), timeout=self._retry_interval_seconds
                )
            except asyncio.TimeoutError:
                pass


class RecordingOutboxRuntime:
    """Owns the local outbox and reconnects the remote projection runtime."""

    def __init__(
        self,
        outbox: LocalRecordingOutbox,
        remote_runtime_provider: Callable[[], RemoteRecordingRuntime],
        *,
        on_remote_ready: Optional[Callable[[], Awaitable[None]]] = None,
        retry_interval_seconds: float = 30,
    ) -> None:
        if retry_interval_seconds <= 0:
            raise ValueError('retry interval must be positive')
        self._outbox = outbox
        self._remote_runtime_provider = remote_runtime_provider
        self._on_remote_ready = on_remote_ready
        self._retry_interval_seconds = retry_interval_seconds
        self._synchronizer = RecordingOutboxSynchronizer(
            outbox,
            lambda: self._remote_runtime_provider().journal,
            retry_interval_seconds=min(retry_interval_seconds, 5),
        )
        self._remote_task: Optional[asyncio.Task[None]] = None
        self._lifecycle_lock = asyncio.Lock()
        self._local_ready = False
        self._remote_ready = False

    @property
    def outbox(self) -> LocalRecordingOutbox:
        return self._outbox

    @property
    def local_ready(self) -> bool:
        return self._local_ready

    async def drain_pending(self, projector: RecordingEventProjector) -> int:
        if not self._local_ready:
            return 0
        return await self._synchronizer.drain(projector)

    async def start(self) -> None:
        async with self._lifecycle_lock:
            try:
                await self._outbox.open()
            except Exception as error:
                self._outbox.pause_automation(error)
                logger.exception(
                    'Local recording journal failed to open; continuing file recording'
                )
            else:
                self._local_ready = True
                self._synchronizer.start()
            if self._remote_ready:
                self._synchronizer.wake()
                return
            if self._remote_task is None or self._remote_task.done():
                self._remote_task = asyncio.create_task(self._run_remote())

    async def close(self) -> None:
        async with self._lifecycle_lock:
            remote_task, self._remote_task = self._remote_task, None
            if remote_task is not None and not remote_task.done():
                remote_task.cancel()
                await asyncio.gather(remote_task, return_exceptions=True)
            await self._synchronizer.close()
            await self._outbox.close()
            self._local_ready = False
            self._remote_ready = False

    async def _run_remote(self) -> None:
        while True:
            try:
                runtime = self._remote_runtime_provider()
                ready = await runtime.start()
                if ready and runtime.journal is not None:
                    callback = self._on_remote_ready
                    if callback is not None:
                        await callback()
                    self._remote_ready = True
                    self._synchronizer.wake()
                    return
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception('Remote recording projection runtime failed to start')
            await asyncio.sleep(self._retry_interval_seconds)


class LocalRecordingOutbox:
    """Durable local source of recording lifecycle events."""

    APPLICATION_ID = 0x424C524A  # BLRJ
    SCHEMA_VERSION = 1

    def __init__(
        self,
        path: Path,
        *,
        clock: Callable[[], float] = time.time,
        uuid_factory: Callable[[], str] = lambda: str(uuid.uuid4()),
        artifact_probe: Callable[
            [str], Optional[RecoveredArtifact]
        ] = probe_recording_artifact,
    ) -> None:
        self._path = Path(os.path.abspath(os.path.expanduser(str(path))))
        self._clock = clock
        self._uuid_factory = uuid_factory
        self._artifact_probe = artifact_probe
        self._executor = self._new_executor()
        self._connection: Optional[sqlite3.Connection] = None
        self._lifecycle_lock = asyncio.Lock()
        self._closed = False
        self._degraded_reason: Optional[str] = None

    @property
    def path(self) -> str:
        return str(self._path)

    @property
    def degraded_reason(self) -> Optional[str]:
        return self._degraded_reason

    def pause_automation(self, error: BaseException) -> None:
        self._degraded_reason = '{}: {}'.format(type(error).__name__, error)

    async def open(self) -> None:
        async with self._lifecycle_lock:
            if self._closed:
                self._executor = self._new_executor()
                self._closed = False
            if self._connection is not None:
                return
            await self._run(self._open_sync)
            self._degraded_reason = None

    async def close(self) -> None:
        async with self._lifecycle_lock:
            if self._closed:
                return
            try:
                if self._connection is not None:
                    await self._run(self._close_sync)
            finally:
                self._executor.shutdown(wait=True)
                self._closed = True

    async def pragma(self, name: str) -> Any:
        if name not in {
            'application_id',
            'foreign_keys',
            'journal_mode',
            'quick_check',
            'synchronous',
            'user_version',
        }:
            raise ValueError('unsupported pragma')
        return await self._run(self._pragma_sync, name)

    async def append_event(self, event: RecordingOutboxEvent) -> RecordingOutboxEvent:
        self._validate_event(event)
        normalized = replace(
            event,
            path=None if event.path is None else self._normalize_path(event.path),
            payload=dict(event.payload),
            sequence=0,
            synced_at=None,
            attempt_count=0,
            last_attempt_at=None,
            last_error=None,
        )
        return await self._run(self._append_event_sync, normalized)

    async def pending_events(self, *, limit: int = 100) -> List[RecordingOutboxEvent]:
        if not 1 <= limit <= 1000:
            raise ValueError('pending event limit must be between 1 and 1000')
        return await self._run(self._pending_events_sync, limit)

    async def mark_attempt(self, sequence: int, error: str) -> None:
        if sequence <= 0:
            raise ValueError('event sequence must be positive')
        await self._run(self._mark_attempt_sync, sequence, error[:1000])

    async def mark_synced(
        self, sequence: int, *, synced_at: Optional[float] = None
    ) -> None:
        if sequence <= 0:
            raise ValueError('event sequence must be positive')
        timestamp = self._clock() if synced_at is None else synced_at
        await self._run(self._mark_synced_sync, sequence, float(timestamp))

    async def status(self) -> RecordingOutboxStatus:
        return await self._run(self._status_sync)

    async def recording_started(
        self,
        room_id: int,
        *,
        live_start_time: int,
        metadata: Optional[RecordingSessionMetadata] = None,
    ) -> str:
        run_id = self._uuid_factory()
        occurred_at = float(self._clock())
        await self.append_event(
            RecordingOutboxEvent(
                event_id=self._uuid_factory(),
                event_type='recording_started',
                run_id=run_id,
                room_id=room_id,
                path=None,
                payload={
                    'live_start_time': int(live_start_time),
                    'metadata': None if metadata is None else asdict(metadata),
                },
                occurred_at=occurred_at,
            )
        )
        return run_id

    async def recording_finished(self, run_id: str) -> None:
        await self._append_for_run(
            'recording_finished', run_id, occurred_at=float(self._clock())
        )

    async def recording_cancelled(self, run_id: str) -> None:
        await self._append_for_run(
            'recording_cancelled', run_id, occurred_at=float(self._clock())
        )

    async def cover_downloaded(self, run_id: str, path: str) -> None:
        await self._append_for_run(
            'cover_downloaded', run_id, path=path, occurred_at=float(self._clock())
        )

    async def video_created(
        self, run_id: str, path: str, *, record_start_time: int
    ) -> None:
        occurred_at = float(self._clock())
        await self._append_for_run(
            'video_created',
            run_id,
            path=path,
            payload={
                'record_start_time': int(record_start_time),
                'timeline_start_at_ms': int(occurred_at * 1000),
            },
            occurred_at=occurred_at,
        )

    async def video_completed(self, run_id: str, path: str) -> None:
        await self._append_for_run(
            'video_completed', run_id, path=path, occurred_at=float(self._clock())
        )

    async def video_postprocessed(
        self, run_id: str, source_path: str, final_path: str
    ) -> None:
        occurred_at = float(self._clock())
        final = self._normalize_path(final_path)
        loop = asyncio.get_running_loop()
        file_size_bytes = await loop.run_in_executor(
            None, self._file_size_or_none, final
        )
        await self._append_for_run(
            'video_postprocessed',
            run_id,
            path=final,
            payload={
                'source_path': self._normalize_path(source_path),
                'file_size_bytes': file_size_bytes,
            },
            occurred_at=occurred_at,
        )

    async def video_postprocessing_failed(
        self, run_id: str, source_path: str, error: BaseException
    ) -> None:
        occurred_at = float(self._clock())
        source = self._normalize_path(source_path)
        loop = asyncio.get_running_loop()
        artifact = await loop.run_in_executor(None, self._artifact_probe, source)
        await self._append_for_run(
            'video_postprocessing_failed',
            run_id,
            path=source,
            payload={
                'error': '{}: {}'.format(type(error).__name__, error)[:500],
                'recovered_artifact': (
                    None
                    if artifact is None
                    else {
                        'path': artifact.path,
                        'size_bytes': artifact.size_bytes,
                        'duration_seconds': artifact.duration_seconds,
                    }
                ),
            },
            occurred_at=occurred_at,
        )

    async def danmaku_completed(self, run_id: str, path: str) -> None:
        occurred_at = float(self._clock())
        xml_path = self._normalize_path(path)
        loop = asyncio.get_running_loop()
        danmaku_count = await loop.run_in_executor(
            None, self._count_danmaku_sync, xml_path
        )
        await self._append_for_run(
            'danmaku_completed',
            run_id,
            path=xml_path,
            payload={'danmaku_count': danmaku_count},
            occurred_at=occurred_at,
        )

    async def run_id_for_source(self, source_path: str) -> str:
        return await self._run(
            self._run_id_for_source_sync, self._normalize_path(source_path)
        )

    async def _append_for_run(
        self,
        event_type: str,
        run_id: str,
        *,
        path: Optional[str] = None,
        payload: Optional[Mapping[str, Any]] = None,
        occurred_at: Optional[float] = None,
    ) -> None:
        room_id = await self._run(self._room_id_for_run_sync, run_id)
        await self.append_event(
            RecordingOutboxEvent(
                event_id=self._uuid_factory(),
                event_type=event_type,
                run_id=run_id,
                room_id=room_id,
                path=path,
                payload={} if payload is None else payload,
                occurred_at=(
                    float(self._clock()) if occurred_at is None else occurred_at
                ),
            )
        )

    async def _run(self, operation: Callable[..., _T], *args: Any) -> _T:
        if self._closed:
            raise RecordingOutboxClosed('recording outbox has been closed')
        loop = asyncio.get_running_loop()
        try:
            return await loop.run_in_executor(self._executor, partial(operation, *args))
        except (sqlite3.Error, OSError) as error:
            raise RecordingOutboxError('recording outbox operation failed') from error

    def _open_sync(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._prepare_database_file()
        connection = sqlite3.connect(
            str(self._path), check_same_thread=False, isolation_level=None
        )
        connection.row_factory = sqlite3.Row
        try:
            connection.execute('PRAGMA busy_timeout=5000')
            connection.execute('PRAGMA journal_mode=WAL').fetchone()
            connection.execute('PRAGMA synchronous=FULL')
            connection.execute('PRAGMA foreign_keys=ON')
            application_id = int(
                connection.execute('PRAGMA application_id').fetchone()[0]
            )
            if application_id not in (0, self.APPLICATION_ID):
                raise sqlite3.DatabaseError(
                    'unexpected recording outbox application ID'
                )
            if application_id == 0:
                connection.execute(
                    'PRAGMA application_id={}'.format(self.APPLICATION_ID)
                )
            version = int(connection.execute('PRAGMA user_version').fetchone()[0])
            if version == 0:
                self._create_schema(connection)
            elif version != self.SCHEMA_VERSION:
                raise sqlite3.DatabaseError(
                    'unsupported recording outbox schema version {}'.format(version)
                )
            result = connection.execute('PRAGMA quick_check').fetchone()
            if result is None or result[0] != 'ok':
                raise sqlite3.DatabaseError('recording outbox quick check failed')
            self._connection = connection
            self._secure_database_files()
        except BaseException:
            connection.close()
            raise

    @classmethod
    def _create_schema(cls, connection: sqlite3.Connection) -> None:
        connection.executescript(
            """
            BEGIN IMMEDIATE;
            CREATE TABLE recording_outbox_events(
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT NOT NULL UNIQUE,
                event_type TEXT NOT NULL CHECK(event_type IN (
                    'recording_started','recording_finished','recording_cancelled',
                    'video_created','video_completed','video_postprocessed',
                    'video_postprocessing_failed','danmaku_completed',
                    'cover_downloaded'
                )),
                run_id TEXT NOT NULL,
                room_id INTEGER NOT NULL CHECK(room_id>0),
                path TEXT,
                payload_json TEXT NOT NULL CHECK(json_valid(payload_json)),
                occurred_at REAL NOT NULL,
                synced_at REAL,
                attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count>=0),
                last_attempt_at REAL,
                last_error TEXT
            );
            CREATE INDEX recording_outbox_pending_idx
            ON recording_outbox_events(sequence) WHERE synced_at IS NULL;
            CREATE TABLE recording_source_runs(
                source_path TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                event_sequence INTEGER NOT NULL REFERENCES
                    recording_outbox_events(sequence) ON DELETE RESTRICT,
                updated_at REAL NOT NULL
            );
            PRAGMA user_version={};
            COMMIT;
            """.format(
                cls.SCHEMA_VERSION
            )
        )

    def _close_sync(self) -> None:
        connection, self._connection = self._connection, None
        if connection is not None:
            try:
                connection.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()
            finally:
                connection.close()
        self._secure_database_files()

    def _pragma_sync(self, name: str) -> Any:
        row = self._require_connection().execute('PRAGMA {}'.format(name)).fetchone()
        return None if row is None else row[0]

    def _append_event_sync(self, event: RecordingOutboxEvent) -> RecordingOutboxEvent:
        connection = self._require_connection()
        payload_json = self._payload_json(event.payload)
        connection.execute('BEGIN IMMEDIATE')
        try:
            existing = connection.execute(
                'SELECT sequence,event_id,event_type,run_id,room_id,path,payload_json,'
                'occurred_at,synced_at,attempt_count,last_attempt_at,last_error '
                'FROM recording_outbox_events WHERE event_id=?',
                (event.event_id,),
            ).fetchone()
            if existing is not None:
                persisted = self._event_from_row(existing)
                if not self._same_event(persisted, event):
                    raise RecordingOutboxConsistencyError(
                        "event '{}' has conflicting content".format(event.event_id)
                    )
                connection.execute('COMMIT')
                return persisted
            cursor = connection.execute(
                'INSERT INTO recording_outbox_events('
                'event_id,event_type,run_id,room_id,path,payload_json,occurred_at) '
                'VALUES(?,?,?,?,?,?,?)',
                (
                    event.event_id,
                    event.event_type,
                    event.run_id,
                    event.room_id,
                    event.path,
                    payload_json,
                    event.occurred_at,
                ),
            )
            sequence = int(cursor.lastrowid)
            if event.event_type == 'video_created':
                assert event.path is not None
                connection.execute(
                    'INSERT INTO recording_source_runs('
                    'source_path,run_id,event_sequence,updated_at) VALUES(?,?,?,?) '
                    'ON CONFLICT(source_path) DO UPDATE SET run_id=excluded.run_id,'
                    'event_sequence=excluded.event_sequence,'
                    'updated_at=excluded.updated_at',
                    (event.path, event.run_id, sequence, event.occurred_at),
                )
            connection.execute('COMMIT')
        except BaseException:
            connection.execute('ROLLBACK')
            raise
        self._secure_database_files()
        return replace(event, sequence=sequence)

    def _pending_events_sync(self, limit: int) -> List[RecordingOutboxEvent]:
        rows = (
            self._require_connection()
            .execute(
                'SELECT sequence,event_id,event_type,run_id,room_id,path,payload_json,'
                'occurred_at,synced_at,attempt_count,last_attempt_at,last_error '
                'FROM recording_outbox_events WHERE synced_at IS NULL '
                'ORDER BY sequence LIMIT ?',
                (limit,),
            )
            .fetchall()
        )
        return [self._event_from_row(row) for row in rows]

    def _mark_attempt_sync(self, sequence: int, error: str) -> None:
        now = float(self._clock())
        cursor = self._require_connection().execute(
            'UPDATE recording_outbox_events SET attempt_count=attempt_count+1,'
            'last_attempt_at=?,last_error=? WHERE sequence=? AND synced_at IS NULL',
            (now, error, sequence),
        )
        if cursor.rowcount != 1:
            raise RecordingOutboxConsistencyError(
                "pending event '{}' does not exist".format(sequence)
            )

    def _mark_synced_sync(self, sequence: int, synced_at: float) -> None:
        cursor = self._require_connection().execute(
            'UPDATE recording_outbox_events SET synced_at=?,last_error=NULL '
            'WHERE sequence=? AND synced_at IS NULL',
            (synced_at, sequence),
        )
        if cursor.rowcount != 1:
            raise RecordingOutboxConsistencyError(
                "pending event '{}' does not exist".format(sequence)
            )

    def _status_sync(self) -> RecordingOutboxStatus:
        connection = self._require_connection()
        pending = connection.execute(
            'SELECT COUNT(*) AS pending_count,MIN(occurred_at) AS oldest_pending_at '
            'FROM recording_outbox_events WHERE synced_at IS NULL'
        ).fetchone()
        synced = connection.execute(
            'SELECT MAX(synced_at) AS last_synced_at FROM recording_outbox_events'
        ).fetchone()
        failed = connection.execute(
            'SELECT last_error,attempt_count FROM recording_outbox_events '
            'WHERE synced_at IS NULL AND last_error IS NOT NULL '
            'ORDER BY last_attempt_at DESC,sequence DESC LIMIT 1'
        ).fetchone()
        return RecordingOutboxStatus(
            pending_count=int(pending['pending_count']),
            oldest_pending_at=(
                None
                if pending['oldest_pending_at'] is None
                else float(pending['oldest_pending_at'])
            ),
            last_synced_at=(
                None
                if synced['last_synced_at'] is None
                else float(synced['last_synced_at'])
            ),
            last_error=None if failed is None else str(failed['last_error']),
            attempt_count=0 if failed is None else int(failed['attempt_count']),
        )

    def _room_id_for_run_sync(self, run_id: str) -> int:
        row = (
            self._require_connection()
            .execute(
                'SELECT room_id FROM recording_outbox_events '
                "WHERE run_id=? AND event_type='recording_started' "
                'ORDER BY sequence DESC LIMIT 1',
                (run_id,),
            )
            .fetchone()
        )
        if row is None:
            raise JournalConsistencyError("unknown recording run '{}'".format(run_id))
        return int(row['room_id'])

    def _run_id_for_source_sync(self, source_path: str) -> str:
        row = (
            self._require_connection()
            .execute(
                'SELECT run_id FROM recording_source_runs WHERE source_path=?',
                (source_path,),
            )
            .fetchone()
        )
        if row is None:
            raise JournalConsistencyError(
                "cannot identify one run for '{}'".format(source_path)
            )
        return str(row['run_id'])

    def _prepare_database_file(self) -> None:
        if self._path.exists() or self._path.is_symlink():
            file_stat = os.lstat(str(self._path))
            if stat.S_ISLNK(file_stat.st_mode):
                raise ValueError('recording outbox must not be a symlink')
            if not stat.S_ISREG(file_stat.st_mode):
                raise ValueError('recording outbox must be a regular file')
            os.chmod(str(self._path), 0o600)
            return
        flags = os.O_CREAT | os.O_EXCL | os.O_RDWR
        if hasattr(os, 'O_NOFOLLOW'):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(str(self._path), flags, 0o600)
        os.close(descriptor)

    def _secure_database_files(self) -> None:
        for path in (
            self._path,
            Path(str(self._path) + '-wal'),
            Path(str(self._path) + '-shm'),
        ):
            try:
                file_stat = os.lstat(str(path))
            except FileNotFoundError:
                continue
            if not stat.S_ISREG(file_stat.st_mode):
                raise OSError("recording outbox file '{}' is not regular".format(path))
            os.chmod(str(path), 0o600)

    @staticmethod
    def _validate_event(event: RecordingOutboxEvent) -> None:
        if not event.event_id or not event.run_id:
            raise ValueError('event and run IDs must not be empty')
        if event.event_type not in RECORDING_EVENT_TYPES:
            raise ValueError(
                "unsupported recording event '{}'".format(event.event_type)
            )
        if event.room_id <= 0:
            raise ValueError('room ID must be positive')
        if event.event_type == 'video_created' and event.path is None:
            raise ValueError('video_created event requires a path')
        if not isinstance(event.payload, Mapping):
            raise ValueError('event payload must be a mapping')

    @staticmethod
    def _payload_json(payload: Mapping[str, Any]) -> str:
        try:
            return json.dumps(
                dict(payload),
                ensure_ascii=False,
                sort_keys=True,
                separators=(',', ':'),
                allow_nan=False,
            )
        except (TypeError, ValueError) as error:
            raise ValueError('event payload must be JSON serializable') from error

    @classmethod
    def _same_event(
        cls, persisted: RecordingOutboxEvent, requested: RecordingOutboxEvent
    ) -> bool:
        return (
            persisted.event_id == requested.event_id
            and persisted.event_type == requested.event_type
            and persisted.run_id == requested.run_id
            and persisted.room_id == requested.room_id
            and persisted.path == requested.path
            and cls._payload_json(persisted.payload)
            == cls._payload_json(requested.payload)
            and persisted.occurred_at == requested.occurred_at
        )

    @staticmethod
    def _event_from_row(row: sqlite3.Row) -> RecordingOutboxEvent:
        payload = json.loads(str(row['payload_json']))
        if not isinstance(payload, dict):
            raise sqlite3.DatabaseError('recording event payload is not an object')
        return RecordingOutboxEvent(
            sequence=int(row['sequence']),
            event_id=str(row['event_id']),
            event_type=str(row['event_type']),
            run_id=str(row['run_id']),
            room_id=int(row['room_id']),
            path=None if row['path'] is None else str(row['path']),
            payload=payload,
            occurred_at=float(row['occurred_at']),
            synced_at=(None if row['synced_at'] is None else float(row['synced_at'])),
            attempt_count=int(row['attempt_count']),
            last_attempt_at=(
                None
                if row['last_attempt_at'] is None
                else float(row['last_attempt_at'])
            ),
            last_error=None if row['last_error'] is None else str(row['last_error']),
        )

    def _require_connection(self) -> sqlite3.Connection:
        if self._connection is None:
            raise RecordingOutboxClosed('recording outbox is not open')
        return self._connection

    @staticmethod
    def _new_executor() -> ThreadPoolExecutor:
        return ThreadPoolExecutor(
            max_workers=1, thread_name_prefix='blrec-recording-outbox'
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
