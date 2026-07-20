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
    'ControlRevisionSnapshot',
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


@dataclass(frozen=True)
class ControlRevisionSnapshot:
    lane: str
    target_key: str
    kind: str
    action: str
    desired_revision: int
    applied_revision: int
    operation_id: Optional[str]


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
        result: Optional[Mapping[str, Any]] = None,
        reuse_succeeded_step_keys: Sequence[str] = (),
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
        reusable_keys = tuple(dict.fromkeys(reuse_succeeded_step_keys))
        if any(key not in keys for key in reusable_keys):
            raise ValueError('reusable control steps must exist in the new operation')
        return await self._run(
            self._admit_sync,
            lane,
            kind,
            target_key,
            tuple(steps),
            dict(result) if result is not None else None,
            reusable_keys,
        )

    async def get(self, operation_id: str) -> Optional[ControlOperationSnapshot]:
        return await self._run(self._get_sync, operation_id)

    async def submit_revision(
        self, *, lane: str, kind: str, target_key: str, action: str
    ) -> ControlOperationSnapshot:
        if not self._admitting or self._closed:
            raise ControlJournalClosed('control journal admission is closed')
        if not lane or not kind or not target_key or not action:
            raise ValueError('revision operation fields must not be empty')
        return await self._run(
            self._submit_revision_sync, lane, kind, target_key, action
        )

    async def get_revision(
        self, lane: str, target_key: str
    ) -> Optional[ControlRevisionSnapshot]:
        return await self._run(self._get_revision_sync, lane, target_key)

    async def recover_revision_gaps(
        self, *, lane: str, kind: str
    ) -> Sequence[ControlOperationSnapshot]:
        operation_ids = await self._run(self._recover_revision_gaps_sync, lane, kind)
        snapshots = []
        for operation_id in operation_ids:
            snapshot = await self.get(operation_id)
            if snapshot is not None:
                snapshots.append(snapshot)
        return tuple(snapshots)

    async def finish_revision_step(
        self, claim: ClaimedControlStep, *, applied_revision: int
    ) -> bool:
        if applied_revision <= 0:
            raise ValueError('applied revision must be positive')
        return await self._run(self._finish_revision_step_sync, claim, applied_revision)

    async def list_nonterminal(self, lane: str) -> Sequence[ControlOperationSnapshot]:
        operation_ids = await self._run(self._list_nonterminal_ids_sync, lane)
        snapshots = []
        for operation_id in operation_ids:
            snapshot = await self.get(operation_id)
            if snapshot is not None:
                snapshots.append(snapshot)
        return tuple(snapshots)

    async def claim_next(self, lane: str) -> Optional[ClaimedControlStep]:
        return await self._run(self._claim_next_sync, lane)

    async def finish_step(
        self,
        claim: ClaimedControlStep,
        *,
        status: TerminalStepStatus,
        result: Optional[Mapping[str, Any]] = None,
        error_code: Optional[str] = None,
        operation_result: Optional[Mapping[str, Any]] = None,
        append_steps: Sequence[ControlStepInput] = (),
    ) -> bool:
        if status == 'failed' and not error_code:
            raise ValueError('failed control step requires an error code')
        if append_steps and status != 'succeeded':
            raise ValueError('control steps can only be appended after success')
        append_keys = [step.key for step in append_steps]
        if any(not key for key in append_keys) or len(set(append_keys)) != len(
            append_keys
        ):
            raise ValueError('appended control step keys must be non-empty and unique')
        return await self._run(
            self._finish_step_sync,
            claim,
            status,
            dict(result) if result is not None else None,
            error_code,
            dict(operation_result) if operation_result is not None else None,
            tuple(append_steps),
        )

    async def fail_step_and_dependents(
        self, claim: ClaimedControlStep, *, error_code: str, dependent_error_code: str
    ) -> bool:
        if not error_code or not dependent_error_code:
            raise ValueError('failed control steps require error codes')
        return await self._run(
            self._fail_step_and_dependents_sync, claim, error_code, dependent_error_code
        )

    async def fail_queued_steps(self, operation_id: str, *, error_code: str) -> int:
        if not error_code:
            raise ValueError('failed control steps require an error code')
        return int(
            await self._run(self._fail_queued_steps_sync, operation_id, error_code)
        )

    async def fail_unclaimed_operation(
        self, operation_id: str, *, error_code: str
    ) -> bool:
        """Fail every queued step before an admitted operation can be claimed."""

        if not error_code:
            raise ValueError('failed control operation requires an error code')
        return await self._run(
            self._fail_unclaimed_operation_sync, operation_id, error_code
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

                CREATE TABLE IF NOT EXISTS control_revisions(
                    lane TEXT NOT NULL,
                    target_key TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    action TEXT NOT NULL,
                    desired_revision INTEGER NOT NULL CHECK(desired_revision>=0),
                    applied_revision INTEGER NOT NULL CHECK(applied_revision>=0),
                    operation_id TEXT REFERENCES control_operations(id)
                        ON DELETE SET NULL,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY(lane,target_key)
                );
                CREATE INDEX IF NOT EXISTS control_revision_gap
                ON control_revisions(lane,desired_revision,applied_revision);
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
            now = float(self._clock())
            blocked_membership_operations = connection.execute(
                'SELECT DISTINCT operation.id FROM control_operations operation '
                'JOIN control_operation_steps step '
                'ON step.operation_id=operation.id '
                "WHERE operation.lane='room-membership' "
                "AND step.status IN ('failed','rejected') "
                'AND EXISTS(SELECT 1 FROM control_operation_steps queued '
                'WHERE queued.operation_id=operation.id '
                "AND queued.status='queued')"
            ).fetchall()
            for row in blocked_membership_operations:
                operation_id = str(row['id'])
                connection.execute(
                    "UPDATE control_operation_steps SET status='failed',"
                    'result_json=NULL,error_code=\'DEPENDENCY_FAILED\',updated_at=? '
                    "WHERE operation_id=? AND status='queued'",
                    (now, operation_id),
                )
                self._refresh_operation_sync(connection, operation_id, now)
            connection.execute(
                "DELETE FROM control_operations WHERE status IN ('succeeded','failed') "
                'AND updated_at<?',
                (now - self.TERMINAL_RETENTION_SECONDS,),
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
        self,
        lane: str,
        kind: str,
        target_key: str,
        steps: Sequence[ControlStepInput],
        result: Optional[Mapping[str, Any]],
        reuse_succeeded_step_keys: Sequence[str],
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
            reused_steps: Dict[str, sqlite3.Row] = {}
            admitted_result = dict(result or {})
            if reuse_succeeded_step_keys:
                previous = connection.execute(
                    'SELECT id FROM control_operations '
                    'WHERE lane=? AND kind=? AND target_key=? AND status=\'failed\' '
                    'ORDER BY attempt DESC LIMIT 1',
                    (lane, kind, target_key),
                ).fetchone()
                if previous is not None:
                    placeholders = ','.join('?' for _ in reuse_succeeded_step_keys)
                    rows = connection.execute(
                        'SELECT key,result_json FROM control_operation_steps '
                        'WHERE operation_id=? AND status=\'succeeded\' '
                        'AND key IN ({})'.format(placeholders),
                        (str(previous['id']), *reuse_succeeded_step_keys),
                    ).fetchall()
                    reused_steps = {str(row['key']): row for row in rows}
                    for row in rows:
                        reused_result = self._decode(row['result_json'])
                        if reused_result is not None:
                            admitted_result.update(reused_result)
            connection.execute(
                'INSERT INTO control_operations('
                'id,lane,kind,target_key,attempt,generation,status,'
                'result_json,created_at,updated_at'
                ') VALUES(?,?,?,?,?,?,\'accepted\',?,?,?)',
                (
                    operation_id,
                    lane,
                    kind,
                    target_key,
                    attempt,
                    generation,
                    self._encode(admitted_result),
                    now,
                    now,
                ),
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
                        'succeeded' if step.key in reused_steps else step.status,
                        (
                            reused_steps[step.key]['result_json']
                            if step.key in reused_steps
                            else self._encode(step.result)
                        ),
                        None if step.key in reused_steps else step.error_code,
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

    def _submit_revision_sync(
        self, lane: str, kind: str, target_key: str, action: str
    ) -> ControlOperationSnapshot:
        connection = self._require_connection()
        now = float(self._clock())
        connection.execute('BEGIN IMMEDIATE')
        try:
            existing = connection.execute(
                'SELECT id FROM control_operations '
                'WHERE lane=? AND kind=? AND target_key=? '
                "AND status IN ('accepted','running')",
                (lane, kind, target_key),
            ).fetchone()
            revision_row = connection.execute(
                'SELECT desired_revision,applied_revision FROM control_revisions '
                'WHERE lane=? AND target_key=?',
                (lane, target_key),
            ).fetchone()
            desired_revision = (
                1 if revision_row is None else int(revision_row['desired_revision']) + 1
            )
            applied_revision = (
                0 if revision_row is None else int(revision_row['applied_revision'])
            )
            if existing is None:
                operation_id = self._insert_revision_operation_sync(
                    connection,
                    lane=lane,
                    kind=kind,
                    target_key=target_key,
                    desired_revision=desired_revision,
                    now=now,
                )
            else:
                operation_id = str(existing['id'])
                row = connection.execute(
                    'SELECT result_json FROM control_operations WHERE id=?',
                    (operation_id,),
                ).fetchone()
                result = dict(self._decode(row['result_json']) or {})
                result['desiredRevision'] = desired_revision
                connection.execute(
                    'UPDATE control_operations SET result_json=?,updated_at=? '
                    'WHERE id=?',
                    (self._encode(result), now, operation_id),
                )
            connection.execute(
                'INSERT INTO control_revisions('
                'lane,target_key,kind,action,desired_revision,applied_revision,'
                'operation_id,updated_at) VALUES(?,?,?,?,?,?,?,?) '
                'ON CONFLICT(lane,target_key) DO UPDATE SET '
                'kind=excluded.kind,action=excluded.action,'
                'desired_revision=excluded.desired_revision,'
                'operation_id=excluded.operation_id,updated_at=excluded.updated_at',
                (
                    lane,
                    target_key,
                    kind,
                    action,
                    desired_revision,
                    applied_revision,
                    operation_id,
                    now,
                ),
            )
            connection.execute('COMMIT')
        except BaseException:
            connection.execute('ROLLBACK')
            raise
        snapshot = self._get_sync(operation_id)
        assert snapshot is not None
        return snapshot

    def _insert_revision_operation_sync(
        self,
        connection: sqlite3.Connection,
        *,
        lane: str,
        kind: str,
        target_key: str,
        desired_revision: int,
        now: float,
    ) -> str:
        count = int(
            connection.execute(
                'SELECT COUNT(*) FROM control_operations WHERE lane=? '
                "AND status IN ('accepted','running')",
                (lane,),
            ).fetchone()[0]
        )
        if count >= self._max_nonterminal_per_lane:
            raise ControlLaneSaturated(lane)
        attempt = int(
            connection.execute(
                'SELECT COALESCE(MAX(attempt),0)+1 FROM control_operations '
                'WHERE lane=? AND kind=? AND target_key=?',
                (lane, kind, target_key),
            ).fetchone()[0]
        )
        generation = int(
            connection.execute(
                'SELECT COALESCE(MAX(generation),0)+1 FROM control_operations '
                'WHERE lane=?',
                (lane,),
            ).fetchone()[0]
        )
        operation_id = uuid.uuid4().hex
        connection.execute(
            'INSERT INTO control_operations('
            'id,lane,kind,target_key,attempt,generation,status,result_json,'
            'created_at,updated_at) VALUES(?,?,?,?,?,?,\'accepted\',?,?,?)',
            (
                operation_id,
                lane,
                kind,
                target_key,
                attempt,
                generation,
                self._encode({'desiredRevision': desired_revision}),
                now,
                now,
            ),
        )
        connection.execute(
            'INSERT INTO control_operation_steps('
            'operation_id,key,generation,status,result_json,error_code,updated_at'
            ') VALUES(?,?,?,\'queued\',NULL,NULL,?)',
            (operation_id, target_key, generation, now),
        )
        return operation_id

    def _get_revision_sync(
        self, lane: str, target_key: str
    ) -> Optional[ControlRevisionSnapshot]:
        row = (
            self._require_connection()
            .execute(
                'SELECT * FROM control_revisions WHERE lane=? AND target_key=?',
                (lane, target_key),
            )
            .fetchone()
        )
        if row is None:
            return None
        return ControlRevisionSnapshot(
            lane=str(row['lane']),
            target_key=str(row['target_key']),
            kind=str(row['kind']),
            action=str(row['action']),
            desired_revision=int(row['desired_revision']),
            applied_revision=int(row['applied_revision']),
            operation_id=(
                None if row['operation_id'] is None else str(row['operation_id'])
            ),
        )

    def _recover_revision_gaps_sync(self, lane: str, kind: str) -> Sequence[str]:
        connection = self._require_connection()
        now = float(self._clock())
        connection.execute('BEGIN IMMEDIATE')
        try:
            rows = connection.execute(
                'SELECT target_key,desired_revision,operation_id '
                'FROM control_revisions WHERE lane=? '
                'AND desired_revision>applied_revision ORDER BY target_key',
                (lane,),
            ).fetchall()
            operation_ids = []
            for row in rows:
                target_key = str(row['target_key'])
                active = connection.execute(
                    'SELECT id FROM control_operations '
                    'WHERE lane=? AND kind=? AND target_key=? '
                    "AND status IN ('accepted','running')",
                    (lane, kind, target_key),
                ).fetchone()
                if active is None:
                    operation_id = self._insert_revision_operation_sync(
                        connection,
                        lane=lane,
                        kind=kind,
                        target_key=target_key,
                        desired_revision=int(row['desired_revision']),
                        now=now,
                    )
                    connection.execute(
                        'UPDATE control_revisions SET operation_id=?,updated_at=? '
                        'WHERE lane=? AND target_key=?',
                        (operation_id, now, lane, target_key),
                    )
                else:
                    operation_id = str(active['id'])
                operation_ids.append(operation_id)
            connection.execute('COMMIT')
            return tuple(operation_ids)
        except BaseException:
            connection.execute('ROLLBACK')
            raise

    def _finish_revision_step_sync(
        self, claim: ClaimedControlStep, applied_revision: int
    ) -> bool:
        connection = self._require_connection()
        now = float(self._clock())
        connection.execute('BEGIN IMMEDIATE')
        try:
            revision = connection.execute(
                'SELECT desired_revision,applied_revision,operation_id '
                'FROM control_revisions WHERE lane=? AND target_key=?',
                (claim.lane, claim.key),
            ).fetchone()
            if revision is None or revision['operation_id'] != claim.operation_id:
                connection.execute('ROLLBACK')
                return False
            cursor = connection.execute(
                'UPDATE control_revisions SET applied_revision=?,updated_at=? '
                'WHERE lane=? AND target_key=? AND applied_revision<?',
                (applied_revision, now, claim.lane, claim.key, applied_revision),
            )
            current_applied = max(
                int(revision['applied_revision']),
                (
                    applied_revision
                    if cursor.rowcount
                    else int(revision['applied_revision'])
                ),
            )
            desired = int(revision['desired_revision'])
            terminal = current_applied >= desired
            step_status = 'succeeded' if terminal else 'queued'
            cursor = connection.execute(
                'UPDATE control_operation_steps SET status=?,result_json=?,'
                'error_code=NULL,updated_at=? WHERE operation_id=? AND key=? '
                "AND generation=? AND status='running'",
                (
                    step_status,
                    self._encode(
                        {'appliedRevision': current_applied, 'desiredRevision': desired}
                    ),
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
            return terminal
        except BaseException:
            connection.execute('ROLLBACK')
            raise

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
                "AND NOT (operation.lane='room-membership' AND EXISTS("
                'SELECT 1 FROM control_operation_steps blocked '
                'WHERE blocked.operation_id=step.operation_id '
                "AND blocked.status IN ('failed','rejected'))) "
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
        operation_result: Optional[Mapping[str, Any]],
        append_steps: Sequence[ControlStepInput],
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
            if append_steps:
                connection.executemany(
                    'INSERT INTO control_operation_steps('
                    'operation_id,key,generation,status,result_json,error_code,'
                    'updated_at) VALUES(?,?,?,?,?,?,?)',
                    [
                        (
                            claim.operation_id,
                            step.key,
                            claim.generation,
                            step.status,
                            self._encode(step.result),
                            step.error_code,
                            now,
                        )
                        for step in append_steps
                    ],
                )
            self._refresh_operation_sync(connection, claim.operation_id, now)
            if operation_result:
                row = connection.execute(
                    'SELECT result_json FROM control_operations WHERE id=?',
                    (claim.operation_id,),
                ).fetchone()
                current = self._decode(row['result_json']) if row is not None else None
                merged = dict(current or {})
                merged.update(operation_result)
                connection.execute(
                    'UPDATE control_operations SET result_json=?,updated_at=? '
                    'WHERE id=?',
                    (self._encode(merged), now, claim.operation_id),
                )
            connection.execute('COMMIT')
            return True
        except BaseException:
            connection.execute('ROLLBACK')
            raise

    def _fail_step_and_dependents_sync(
        self, claim: ClaimedControlStep, error_code: str, dependent_error_code: str
    ) -> bool:
        connection = self._require_connection()
        now = float(self._clock())
        connection.execute('BEGIN IMMEDIATE')
        try:
            cursor = connection.execute(
                "UPDATE control_operation_steps SET status='failed',"
                'result_json=NULL,error_code=?,updated_at=? '
                "WHERE operation_id=? AND key=? AND generation=? AND status='running'",
                (error_code, now, claim.operation_id, claim.key, claim.generation),
            )
            if cursor.rowcount != 1:
                connection.execute('ROLLBACK')
                return False
            connection.execute(
                "UPDATE control_operation_steps SET status='failed',"
                'result_json=NULL,error_code=?,updated_at=? '
                "WHERE operation_id=? AND status='queued'",
                (dependent_error_code, now, claim.operation_id),
            )
            self._refresh_operation_sync(connection, claim.operation_id, now)
            connection.execute('COMMIT')
            return True
        except BaseException:
            connection.execute('ROLLBACK')
            raise

    def _fail_unclaimed_operation_sync(
        self, operation_id: str, error_code: str
    ) -> bool:
        connection = self._require_connection()
        now = float(self._clock())
        connection.execute('BEGIN IMMEDIATE')
        try:
            running_count = int(
                connection.execute(
                    'SELECT COUNT(*) FROM control_operation_steps '
                    "WHERE operation_id=? AND status='running'",
                    (operation_id,),
                ).fetchone()[0]
            )
            if running_count:
                raise RuntimeError('cannot fail an operation after it was claimed')
            cursor = connection.execute(
                "UPDATE control_operation_steps SET status='failed',"
                'result_json=NULL,error_code=?,updated_at=? '
                "WHERE operation_id=? AND status='queued'",
                (error_code, now, operation_id),
            )
            if cursor.rowcount == 0:
                connection.execute('ROLLBACK')
                return False
            self._refresh_operation_sync(connection, operation_id, now)
            connection.execute('COMMIT')
            return True
        except BaseException:
            if connection.in_transaction:
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

    def _list_nonterminal_ids_sync(self, lane: str) -> Sequence[str]:
        rows = (
            self._require_connection()
            .execute(
                'SELECT id FROM control_operations WHERE lane=? '
                "AND status IN ('accepted','running') ORDER BY generation,id",
                (lane,),
            )
            .fetchall()
        )
        return tuple(str(row['id']) for row in rows)

    def _fail_queued_steps_sync(self, operation_id: str, error_code: str) -> int:
        connection = self._require_connection()
        now = float(self._clock())
        connection.execute('BEGIN IMMEDIATE')
        try:
            cursor = connection.execute(
                "UPDATE control_operation_steps SET status='failed',"
                'result_json=NULL,error_code=?,updated_at=? '
                "WHERE operation_id=? AND status='queued'",
                (error_code, now, operation_id),
            )
            self._refresh_operation_sync(connection, operation_id, now)
            connection.execute('COMMIT')
            return int(cursor.rowcount)
        except BaseException:
            connection.execute('ROLLBACK')
            raise

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
        current_row = connection.execute(
            'SELECT result_json FROM control_operations WHERE id=?', (operation_id,)
        ).fetchone()
        current_result = (
            self._decode(current_row['result_json'])
            if current_row is not None
            else None
        )
        merged_result = dict(current_result or {})
        merged_result['counts'] = counts
        connection.execute(
            'UPDATE control_operations SET '
            'status=?,result_json=?,error_code=?,updated_at=? '
            'WHERE id=?',
            (
                operation_status,
                self._encode(merged_result),
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
