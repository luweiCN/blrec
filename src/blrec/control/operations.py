from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any, Callable, Dict, Literal, Mapping, Optional, Sequence, TypeVar

from blrec.request_metrics import record_database_call

__all__ = (
    'ClaimedControlStep',
    'ControlJournalClosed',
    'ControlLaneSaturated',
    'ControlOperationJournal',
    'ControlOperationSnapshot',
    'ControlStepInput',
    'ControlStepSnapshot',
)

OperationStatus = Literal['accepted', 'running', 'succeeded', 'failed']
StepStatus = Literal['queued', 'rejected', 'running', 'succeeded', 'failed']
TerminalStepStatus = Literal['succeeded', 'failed']

_T = TypeVar('_T')


class ControlJournalClosed(RuntimeError):
    pass


class ControlLaneSaturated(RuntimeError):
    def __init__(self, lane: str) -> None:
        super().__init__('control lane capacity is exhausted: {}'.format(lane))
        self.lane = lane


@dataclass(frozen=True)
class ControlStepInput:
    key: str
    status: Literal['queued', 'rejected'] = 'queued'
    error_code: Optional[str] = None
    result: Optional[Mapping[str, Any]] = None


@dataclass(frozen=True)
class ControlStepSnapshot:
    key: str
    generation: int
    status: StepStatus
    result: Optional[Mapping[str, Any]]
    error_code: Optional[str]


@dataclass(frozen=True)
class ControlOperationSnapshot:
    id: str
    lane: str
    kind: str
    target_key: str
    attempt: int
    generation: int
    status: OperationStatus
    result: Optional[Mapping[str, Any]]
    error_code: Optional[str]
    created_at: float
    updated_at: float
    steps: Sequence[ControlStepSnapshot]


@dataclass(frozen=True)
class ClaimedControlStep:
    operation_id: str
    lane: str
    kind: str
    key: str
    generation: int


class ControlOperationJournal:
    """Small durable journal for local control-operation coordination."""

    TERMINAL_RETENTION_SECONDS = 30 * 24 * 60 * 60

    def __init__(
        self,
        path: Path,
        *,
        max_nonterminal_per_lane: int = 100,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if max_nonterminal_per_lane <= 0:
            raise ValueError('control lane capacity must be positive')
        self._path = Path(os.path.abspath(os.path.expanduser(str(path))))
        self._max_nonterminal_per_lane = max_nonterminal_per_lane
        self._clock = clock
        self._executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix='blrec-control-db'
        )
        self._connection: Optional[sqlite3.Connection] = None
        self._lifecycle_lock = asyncio.Lock()
        self._admitting = True
        self._closed = False

    @property
    def path(self) -> str:
        return str(self._path)

    async def open(self) -> None:
        async with self._lifecycle_lock:
            if self._closed:
                self._executor = ThreadPoolExecutor(
                    max_workers=1, thread_name_prefix='blrec-control-db'
                )
                self._closed = False
                self._admitting = True
            if self._connection is not None:
                return
            await self._run(self._open_sync)

    def close_admission(self) -> None:
        self._admitting = False

    async def close(self) -> None:
        async with self._lifecycle_lock:
            if self._closed:
                return
            self.close_admission()
            try:
                if self._connection is not None:
                    await self._run(self._close_sync)
            finally:
                self._executor.shutdown(wait=True)
                self._closed = True

    async def pragma(self, name: str) -> Any:
        if name not in {'journal_mode', 'synchronous', 'foreign_keys', 'quick_check'}:
            raise ValueError('unsupported pragma')
        return await self._run(self._pragma_sync, name)

    async def admit(
        self,
        *,
        lane: str,
        kind: str,
        target_key: str,
        steps: Sequence[ControlStepInput],
    ) -> ControlOperationSnapshot:
        if not self._admitting or self._closed:
            raise ControlJournalClosed('control journal admission is closed')
        if not lane or not kind or not target_key:
            raise ValueError('lane, kind and target key must not be empty')
        if not steps:
            raise ValueError('control operation must contain at least one step')
        keys = [step.key for step in steps]
        if any(not key for key in keys) or len(set(keys)) != len(keys):
            raise ValueError('control step keys must be non-empty and unique')
        return await self._run(self._admit_sync, lane, kind, target_key, tuple(steps))

    async def get(self, operation_id: str) -> Optional[ControlOperationSnapshot]:
        return await self._run(self._get_sync, operation_id)

    async def claim_next(self, lane: str) -> Optional[ClaimedControlStep]:
        return await self._run(self._claim_next_sync, lane)

    async def finish_step(
        self,
        claim: ClaimedControlStep,
        *,
        status: TerminalStepStatus,
        result: Optional[Mapping[str, Any]] = None,
        error_code: Optional[str] = None,
    ) -> bool:
        if status == 'failed' and not error_code:
            raise ValueError('failed control step requires an error code')
        return await self._run(
            self._finish_step_sync,
            claim,
            status,
            dict(result) if result is not None else None,
            error_code,
        )

    async def supersede_queued_steps(
        self, *, lane: str, keys: Sequence[str], keep_operation_id: str, generation: int
    ) -> int:
        if not keys:
            return 0
        return await self._run(
            self._supersede_queued_steps_sync,
            lane,
            tuple(dict.fromkeys(keys)),
            keep_operation_id,
            generation,
        )

    async def queued_count(self, lane: str) -> int:
        return int(await self._run(self._queued_count_sync, lane))

    async def _run(self, operation: Callable[..., _T], *args: Any) -> _T:
        if self._closed:
            raise ControlJournalClosed('control journal has been closed')
        loop = asyncio.get_running_loop()
        started = time.perf_counter()
        try:
            return await loop.run_in_executor(self._executor, partial(operation, *args))
        finally:
            record_database_call(time.perf_counter() - started)

    def _open_sync(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(
            str(self._path), check_same_thread=False, isolation_level=None
        )
        connection.row_factory = sqlite3.Row
        try:
            connection.execute('PRAGMA journal_mode=DELETE').fetchone()
            connection.execute('PRAGMA synchronous=FULL')
            connection.execute('PRAGMA foreign_keys=ON')
            connection.execute('PRAGMA busy_timeout=5000')
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS control_operations(
                    id TEXT PRIMARY KEY,
                    lane TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    target_key TEXT NOT NULL,
                    attempt INTEGER NOT NULL CHECK(attempt>=1),
                    generation INTEGER NOT NULL CHECK(generation>=1),
                    status TEXT NOT NULL CHECK(
                        status IN ('accepted','running','succeeded','failed')
                    ),
                    result_json TEXT,
                    error_code TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS control_operation_active_target
                ON control_operations(lane,kind,target_key)
                WHERE status IN ('accepted','running');
                CREATE INDEX IF NOT EXISTS control_operation_lane_status
                ON control_operations(lane,status,created_at,id);

                CREATE TABLE IF NOT EXISTS control_operation_steps(
                    operation_id TEXT NOT NULL REFERENCES control_operations(id)
                        ON DELETE CASCADE,
                    key TEXT NOT NULL,
                    generation INTEGER NOT NULL CHECK(generation>=1),
                    status TEXT NOT NULL CHECK(
                        status IN ('queued','rejected','running','succeeded','failed')
                    ),
                    result_json TEXT,
                    error_code TEXT,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY(operation_id,key)
                );
                CREATE INDEX IF NOT EXISTS control_step_status
                ON control_operation_steps(status,generation,operation_id,key);
                """
            )
            connection.execute('BEGIN IMMEDIATE')
            connection.execute(
                "UPDATE control_operation_steps SET status='queued' "
                "WHERE status='running'"
            )
            connection.execute(
                "UPDATE control_operations SET status='accepted' "
                "WHERE status='running'"
            )
            connection.execute(
                "DELETE FROM control_operations WHERE status IN ('succeeded','failed') "
                'AND updated_at<?',
                (float(self._clock()) - self.TERMINAL_RETENTION_SECONDS,),
            )
            connection.execute('COMMIT')
            result = connection.execute('PRAGMA quick_check').fetchone()
            if result is None or result[0] != 'ok':
                raise sqlite3.DatabaseError('control journal quick check failed')
            self._connection = connection
            os.chmod(self._path, 0o600)
        except BaseException:
            connection.close()
            raise

    def _close_sync(self) -> None:
        connection, self._connection = self._connection, None
        if connection is not None:
            connection.close()

    def _pragma_sync(self, name: str) -> Any:
        row = self._require_connection().execute('PRAGMA {}'.format(name)).fetchone()
        return None if row is None else row[0]

    def _admit_sync(
        self, lane: str, kind: str, target_key: str, steps: Sequence[ControlStepInput]
    ) -> ControlOperationSnapshot:
        connection = self._require_connection()
        now = float(self._clock())
        connection.execute('BEGIN IMMEDIATE')
        try:
            existing = connection.execute(
                'SELECT id FROM control_operations '
                "WHERE lane=? AND kind=? AND target_key=? "
                "AND status IN ('accepted','running')",
                (lane, kind, target_key),
            ).fetchone()
            if existing is not None:
                operation_id = str(existing['id'])
                connection.execute('COMMIT')
                snapshot = self._get_sync(operation_id)
                assert snapshot is not None
                return snapshot
            count = connection.execute(
                'SELECT COUNT(*) FROM control_operations WHERE lane=? '
                "AND status IN ('accepted','running')",
                (lane,),
            ).fetchone()[0]
            if int(count) >= self._max_nonterminal_per_lane:
                raise ControlLaneSaturated(lane)
            attempt_row = connection.execute(
                'SELECT COALESCE(MAX(attempt),0)+1 FROM control_operations '
                'WHERE lane=? AND kind=? AND target_key=?',
                (lane, kind, target_key),
            ).fetchone()
            generation_row = connection.execute(
                'SELECT COALESCE(MAX(generation),0)+1 FROM control_operations '
                'WHERE lane=?',
                (lane,),
            ).fetchone()
            attempt = int(attempt_row[0])
            generation = int(generation_row[0])
            operation_id = uuid.uuid4().hex
            connection.execute(
                'INSERT INTO control_operations('
                'id,lane,kind,target_key,attempt,generation,status,'
                'created_at,updated_at'
                ') VALUES(?,?,?,?,?,?,\'accepted\',?,?)',
                (operation_id, lane, kind, target_key, attempt, generation, now, now),
            )
            connection.executemany(
                'INSERT INTO control_operation_steps('
                'operation_id,key,generation,status,result_json,error_code,updated_at'
                ') VALUES(?,?,?,?,?,?,?)',
                [
                    (
                        operation_id,
                        step.key,
                        generation,
                        step.status,
                        self._encode(step.result),
                        step.error_code,
                        now,
                    )
                    for step in steps
                ],
            )
            self._refresh_operation_sync(connection, operation_id, now)
            connection.execute('COMMIT')
        except BaseException:
            connection.execute('ROLLBACK')
            raise
        snapshot = self._get_sync(operation_id)
        assert snapshot is not None
        return snapshot

    def _get_sync(self, operation_id: str) -> Optional[ControlOperationSnapshot]:
        connection = self._require_connection()
        row = connection.execute(
            'SELECT * FROM control_operations WHERE id=?', (operation_id,)
        ).fetchone()
        if row is None:
            return None
        step_rows = connection.execute(
            'SELECT key,generation,status,result_json,error_code '
            'FROM control_operation_steps WHERE operation_id=? ORDER BY rowid',
            (operation_id,),
        ).fetchall()
        return ControlOperationSnapshot(
            id=str(row['id']),
            lane=str(row['lane']),
            kind=str(row['kind']),
            target_key=str(row['target_key']),
            attempt=int(row['attempt']),
            generation=int(row['generation']),
            status=row['status'],
            result=self._decode(row['result_json']),
            error_code=row['error_code'],
            created_at=float(row['created_at']),
            updated_at=float(row['updated_at']),
            steps=tuple(
                ControlStepSnapshot(
                    key=str(step['key']),
                    generation=int(step['generation']),
                    status=step['status'],
                    result=self._decode(step['result_json']),
                    error_code=step['error_code'],
                )
                for step in step_rows
            ),
        )

    def _claim_next_sync(self, lane: str) -> Optional[ClaimedControlStep]:
        connection = self._require_connection()
        now = float(self._clock())
        connection.execute('BEGIN IMMEDIATE')
        try:
            row = connection.execute(
                'SELECT step.operation_id,step.key,step.generation,operation.kind '
                'FROM control_operation_steps step '
                'JOIN control_operations operation ON operation.id=step.operation_id '
                "WHERE operation.lane=? AND step.status='queued' "
                "AND operation.status IN ('accepted','running') "
                'ORDER BY step.generation,step.rowid LIMIT 1',
                (lane,),
            ).fetchone()
            if row is None:
                connection.execute('COMMIT')
                return None
            cursor = connection.execute(
                "UPDATE control_operation_steps SET status='running',updated_at=? "
                "WHERE operation_id=? AND key=? AND generation=? AND status='queued'",
                (
                    now,
                    str(row['operation_id']),
                    str(row['key']),
                    int(row['generation']),
                ),
            )
            if cursor.rowcount != 1:
                connection.execute('ROLLBACK')
                return None
            connection.execute(
                "UPDATE control_operations SET status='running',updated_at=? "
                "WHERE id=? AND status IN ('accepted','running')",
                (now, str(row['operation_id'])),
            )
            connection.execute('COMMIT')
            return ClaimedControlStep(
                operation_id=str(row['operation_id']),
                lane=lane,
                kind=str(row['kind']),
                key=str(row['key']),
                generation=int(row['generation']),
            )
        except BaseException:
            connection.execute('ROLLBACK')
            raise

    def _finish_step_sync(
        self,
        claim: ClaimedControlStep,
        status: TerminalStepStatus,
        result: Optional[Mapping[str, Any]],
        error_code: Optional[str],
    ) -> bool:
        connection = self._require_connection()
        now = float(self._clock())
        connection.execute('BEGIN IMMEDIATE')
        try:
            cursor = connection.execute(
                'UPDATE control_operation_steps SET '
                'status=?,result_json=?,error_code=?,updated_at=? '
                "WHERE operation_id=? AND key=? AND generation=? AND status='running'",
                (
                    status,
                    self._encode(result),
                    error_code,
                    now,
                    claim.operation_id,
                    claim.key,
                    claim.generation,
                ),
            )
            if cursor.rowcount != 1:
                connection.execute('ROLLBACK')
                return False
            self._refresh_operation_sync(connection, claim.operation_id, now)
            connection.execute('COMMIT')
            return True
        except BaseException:
            connection.execute('ROLLBACK')
            raise

    def _queued_count_sync(self, lane: str) -> int:
        row = (
            self._require_connection()
            .execute(
                'SELECT COUNT(*) FROM control_operation_steps step '
                'JOIN control_operations operation ON operation.id=step.operation_id '
                "WHERE operation.lane=? AND step.status='queued'",
                (lane,),
            )
            .fetchone()
        )
        return int(row[0])

    def _supersede_queued_steps_sync(
        self, lane: str, keys: Sequence[str], keep_operation_id: str, generation: int
    ) -> int:
        connection = self._require_connection()
        placeholders = ','.join('?' for _ in keys)
        now = float(self._clock())
        connection.execute('BEGIN IMMEDIATE')
        try:
            rows = connection.execute(
                'SELECT step.operation_id,step.key FROM control_operation_steps step '
                'JOIN control_operations operation ON operation.id=step.operation_id '
                "WHERE operation.lane=? AND step.status='queued' "
                'AND step.key IN ({}) AND step.operation_id!=? '
                'AND step.generation<?'.format(placeholders),
                (lane, *keys, keep_operation_id, generation),
            ).fetchall()
            operation_ids = set()
            for row in rows:
                operation_id = str(row['operation_id'])
                key = str(row['key'])
                connection.execute(
                    "UPDATE control_operation_steps SET status='succeeded',"
                    'result_json=?,error_code=NULL,updated_at=? '
                    "WHERE operation_id=? AND key=? AND status='queued'",
                    (
                        self._encode(
                            {'roomId': int(key), 'superseded': True}
                            if key.isdigit()
                            else {'key': key, 'superseded': True}
                        ),
                        now,
                        operation_id,
                        key,
                    ),
                )
                operation_ids.add(operation_id)
            for operation_id in operation_ids:
                self._refresh_operation_sync(connection, operation_id, now)
            connection.execute('COMMIT')
            return len(rows)
        except BaseException:
            connection.execute('ROLLBACK')
            raise

    def _refresh_operation_sync(
        self, connection: sqlite3.Connection, operation_id: str, now: float
    ) -> None:
        rows = connection.execute(
            'SELECT status,error_code FROM control_operation_steps '
            'WHERE operation_id=?',
            (operation_id,),
        ).fetchall()
        counts: Dict[str, int] = {}
        for row in rows:
            counts[str(row['status'])] = counts.get(str(row['status']), 0) + 1
        if counts.get('queued', 0) or counts.get('running', 0):
            operation_status = 'running' if counts.get('running', 0) else 'accepted'
            error_code = None
        elif counts.get('failed', 0) or counts.get('rejected', 0):
            operation_status = 'failed'
            error_row = next(
                (row for row in rows if row['error_code'] is not None), None
            )
            error_code = None if error_row is None else str(error_row['error_code'])
        else:
            operation_status = 'succeeded'
            error_code = None
        connection.execute(
            'UPDATE control_operations SET '
            'status=?,result_json=?,error_code=?,updated_at=? '
            'WHERE id=?',
            (
                operation_status,
                self._encode({'counts': counts}),
                error_code,
                now,
                operation_id,
            ),
        )

    def _require_connection(self) -> sqlite3.Connection:
        if self._connection is None:
            raise RuntimeError('control journal is not open')
        return self._connection

    @staticmethod
    def _encode(value: Optional[Mapping[str, Any]]) -> Optional[str]:
        if value is None:
            return None
        return json.dumps(value, ensure_ascii=False, separators=(',', ':'))

    @staticmethod
    def _decode(value: Optional[str]) -> Optional[Mapping[str, Any]]:
        if value is None:
            return None
        decoded = json.loads(value)
        if not isinstance(decoded, dict):
            raise ValueError('control journal result must be an object')
        return decoded
