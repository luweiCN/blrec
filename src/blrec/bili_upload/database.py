from __future__ import annotations

import asyncio
import fcntl
import os
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import (
    Any,
    BinaryIO,
    Callable,
    Dict,
    List,
    Mapping,
    Optional,
    Sequence,
    Set,
    Tuple,
    TypeVar,
)

__all__ = (
    'BiliUploadDatabase',
    'DatabaseLocked',
    'LeaseClaim',
    'LeaseLost',
    'UnsupportedDatabaseFilesystem',
)


class DatabaseLocked(RuntimeError):
    pass


class UnsupportedDatabaseFilesystem(RuntimeError):
    pass


class LeaseLost(RuntimeError):
    pass


@dataclass(frozen=True)
class LeaseClaim:
    table: str
    id: int
    lease_owner: str
    lease_generation: int
    lease_until: int
    attempt: int


_T = TypeVar('_T')


class BiliUploadDatabase:
    LEASE_TTL_SECONDS = 120
    RENEW_WINDOW_SECONDS = 60
    _CLAIM_TABLES = frozenset(('upload_jobs', 'comment_items', 'danmaku_items'))
    _UNSUPPORTED_FILESYSTEMS = frozenset(('nfs', 'nfs4', 'cifs', 'smb3', 'fuse.sshfs'))

    def __init__(self, path: str) -> None:
        self._path = Path(os.path.abspath(os.path.expanduser(path)))
        self._directory = self._path.parent
        self._lock_path = Path(str(self._path) + '.lock')
        self._executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix='blrec-upload-db'
        )
        self._connection: Optional[sqlite3.Connection] = None
        self._lock_file: Optional[BinaryIO] = None
        self._lifecycle_lock = asyncio.Lock()
        self._executor_closed = False

    @property
    def path(self) -> str:
        return str(self._path)

    async def open(self) -> None:
        async with self._lifecycle_lock:
            if self._executor_closed:
                raise RuntimeError('database has been closed')
            if self._connection is not None:
                return
            await self._run(self._open_sync)

    async def close(self) -> None:
        async with self._lifecycle_lock:
            if self._executor_closed:
                return
            try:
                await self._run(self._close_sync)
            finally:
                self._executor.shutdown(wait=True)
                self._executor_closed = True

    async def checkpoint(self) -> None:
        await self._run(self._checkpoint_sync)

    async def read(self, operation: Callable[[sqlite3.Connection], _T]) -> _T:
        return await self._run(self._read_sync, operation)

    async def write(self, operation: Callable[[sqlite3.Connection], _T]) -> _T:
        return await self._run(self._write_sync, operation)

    async def execute(self, sql: str, parameters: Sequence[Any] = ()) -> int:
        return await self._run(self._execute_sync, sql, tuple(parameters))

    async def fetchone(
        self, sql: str, parameters: Sequence[Any] = ()
    ) -> Optional[sqlite3.Row]:
        return await self._run(self._fetchone_sync, sql, tuple(parameters))

    async def fetchall(
        self, sql: str, parameters: Sequence[Any] = ()
    ) -> List[sqlite3.Row]:
        return await self._run(self._fetchall_sync, sql, tuple(parameters))

    async def scalar(self, sql: str, parameters: Sequence[Any] = ()) -> Any:
        row = await self.fetchone(sql, parameters)
        return None if row is None else row[0]

    async def table_names(self) -> Set[str]:
        rows = await self.fetchall(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
        return {str(row['name']) for row in rows}

    async def claim(
        self,
        table: str,
        states: Sequence[str],
        lease_owner: str,
        *,
        now: Optional[int] = None,
    ) -> Optional[LeaseClaim]:
        self._validate_claim_table(table)
        if not states:
            raise ValueError('claim states must not be empty')
        if not lease_owner:
            raise ValueError('lease owner must not be empty')
        normalized_states = tuple(getattr(state, 'value', state) for state in states)
        return await self._run(
            self._claim_sync,
            table,
            normalized_states,
            lease_owner,
            int(time.time()) if now is None else int(now),
        )

    async def renew(self, claim: LeaseClaim, *, now: Optional[int] = None) -> int:
        self._validate_claim_table(claim.table)
        return await self._run(
            self._renew_sync, claim, int(time.time()) if now is None else int(now)
        )

    async def fenced_update(
        self,
        table: str,
        row_id: int,
        lease_owner: str,
        lease_generation: int,
        values: Mapping[str, Any],
    ) -> None:
        self._validate_claim_table(table)
        if not values:
            raise ValueError('fenced update values must not be empty')
        return await self._run(
            self._fenced_update_sync,
            table,
            row_id,
            lease_owner,
            lease_generation,
            dict(values),
        )

    async def _run(self, operation: Callable[..., _T], *args: Any) -> _T:
        if self._executor_closed:
            raise RuntimeError('database has been closed')
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, partial(operation, *args))

    def _open_sync(self) -> None:
        self._directory.mkdir(parents=True, mode=0o700, exist_ok=True)
        os.chmod(self._directory, 0o700)
        filesystem_type = self._filesystem_type(self._directory)
        if filesystem_type in self._UNSUPPORTED_FILESYSTEMS:
            raise UnsupportedDatabaseFilesystem(
                "database filesystem '{}' does not provide supported local "
                'locking'.format(filesystem_type)
            )

        self._probe_lock()
        try:
            connection = sqlite3.connect(
                str(self._path), check_same_thread=False, isolation_level=None
            )
            connection.row_factory = sqlite3.Row
            self._connection = connection
            connection.execute('PRAGMA journal_mode=WAL')
            connection.execute('PRAGMA foreign_keys=ON')
            connection.execute('PRAGMA busy_timeout=5000')
            self._apply_migrations_sync(connection)
            result = connection.execute('PRAGMA quick_check').fetchone()
            if result is None or result[0] != 'ok':
                raise sqlite3.DatabaseError('database quick check failed')
            self._acquire_process_lock()
            self._secure_database_files()
        except BaseException:
            self._close_sync()
            raise

    def _close_sync(self) -> None:
        connection, self._connection = self._connection, None
        if connection is not None:
            connection.close()
        lock_file, self._lock_file = self._lock_file, None
        if lock_file is not None:
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            finally:
                lock_file.close()

    def _checkpoint_sync(self) -> None:
        connection = self._require_connection()
        connection.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()
        self._secure_database_files()

    def _read_sync(self, operation: Callable[[sqlite3.Connection], _T]) -> _T:
        return operation(self._require_connection())

    def _write_sync(self, operation: Callable[[sqlite3.Connection], _T]) -> _T:
        connection = self._require_connection()
        connection.execute('BEGIN IMMEDIATE')
        try:
            result = operation(connection)
            connection.execute('COMMIT')
        except BaseException:
            connection.execute('ROLLBACK')
            raise
        self._secure_database_files()
        return result

    def _execute_sync(self, sql: str, parameters: Tuple[Any, ...]) -> int:
        cursor = self._require_connection().execute(sql, parameters)
        self._secure_database_files()
        return cursor.rowcount

    def _fetchone_sync(
        self, sql: str, parameters: Tuple[Any, ...]
    ) -> Optional[sqlite3.Row]:
        return self._require_connection().execute(sql, parameters).fetchone()

    def _fetchall_sync(
        self, sql: str, parameters: Tuple[Any, ...]
    ) -> List[sqlite3.Row]:
        return list(self._require_connection().execute(sql, parameters).fetchall())

    def _claim_sync(
        self, table: str, states: Tuple[str, ...], lease_owner: str, now: int
    ) -> Optional[LeaseClaim]:
        connection = self._require_connection()
        placeholders = ','.join('?' for _ in states)
        connection.execute('BEGIN IMMEDIATE')
        try:
            row = connection.execute(
                'SELECT id FROM {} WHERE state IN ({}) '
                'AND next_attempt_at<=? '
                'AND (lease_until IS NULL OR lease_until<=?) '
                'ORDER BY priority DESC,next_attempt_at,id LIMIT 1'.format(
                    table, placeholders
                ),
                (*states, now, now),
            ).fetchone()
            if row is None:
                connection.execute('COMMIT')
                return None
            row_id = int(row['id'])
            lease_until = now + self.LEASE_TTL_SECONDS
            cursor = connection.execute(
                'UPDATE {} SET lease_owner=?, '
                'lease_generation=lease_generation+1,lease_until=?,attempt=attempt+1 '
                'WHERE id=? AND state IN ({}) '
                'AND next_attempt_at<=? '
                'AND (lease_until IS NULL OR lease_until<=?)'.format(
                    table, placeholders
                ),
                (lease_owner, lease_until, row_id, *states, now, now),
            )
            if cursor.rowcount != 1:
                connection.execute('ROLLBACK')
                return None
            claimed = connection.execute(
                'SELECT id,lease_owner,lease_generation,lease_until,attempt '
                'FROM {} WHERE id=?'.format(table),
                (row_id,),
            ).fetchone()
            assert claimed is not None
            connection.execute('COMMIT')
        except BaseException:
            if connection.in_transaction:
                connection.execute('ROLLBACK')
            raise
        self._secure_database_files()
        return LeaseClaim(
            table=table,
            id=int(claimed['id']),
            lease_owner=str(claimed['lease_owner']),
            lease_generation=int(claimed['lease_generation']),
            lease_until=int(claimed['lease_until']),
            attempt=int(claimed['attempt']),
        )

    def _renew_sync(self, claim: LeaseClaim, now: int) -> int:
        cursor = self._require_connection().execute(
            'UPDATE {} SET lease_until=? WHERE id=? AND lease_owner=? '
            'AND lease_generation=? AND lease_until>? AND lease_until<=?'.format(
                claim.table
            ),
            (
                now + self.LEASE_TTL_SECONDS,
                claim.id,
                claim.lease_owner,
                claim.lease_generation,
                now,
                now + self.RENEW_WINDOW_SECONDS,
            ),
        )
        self._secure_database_files()
        return cursor.rowcount

    def _fenced_update_sync(
        self,
        table: str,
        row_id: int,
        lease_owner: str,
        lease_generation: int,
        values: Dict[str, Any],
    ) -> None:
        columns = self._table_columns_sync(table)
        forbidden = {'id', 'lease_owner', 'lease_generation'}
        for column in values:
            if column not in columns or column in forbidden:
                raise ValueError("invalid fenced update column '{}'".format(column))
        assignments = ','.join('"{}"=?'.format(column) for column in values)
        parameters = tuple(values.values()) + (row_id, lease_owner, lease_generation)
        cursor = self._require_connection().execute(
            'UPDATE {} SET {} WHERE id=? AND lease_owner=? '
            'AND lease_generation=?'.format(table, assignments),
            parameters,
        )
        if cursor.rowcount != 1:
            raise LeaseLost('database lease is no longer owned')
        self._secure_database_files()

    def _table_columns_sync(self, table: str) -> Set[str]:
        rows = self._require_connection().execute('PRAGMA table_info({})'.format(table))
        return {str(row['name']) for row in rows}

    def _apply_migrations_sync(self, connection: sqlite3.Connection) -> None:
        connection.execute('BEGIN IMMEDIATE')
        try:
            exists = connection.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE type='table' AND name='schema_migrations'"
            ).fetchone()
            current_version = 0
            if exists is not None:
                row = connection.execute(
                    'SELECT COALESCE(MAX(version),0) FROM schema_migrations'
                ).fetchone()
                assert row is not None
                current_version = int(row[0])
            latest_version = 8
            if current_version > latest_version:
                raise sqlite3.DatabaseError(
                    'database schema is newer than this application'
                )
            for version in range(current_version + 1, latest_version + 1):
                self._execute_migration_script(
                    connection, self._migration_path(version).read_text(encoding='utf8')
                )
                connection.execute(
                    'INSERT INTO schema_migrations(version,applied_at) VALUES(?,?)',
                    (version, int(time.time())),
                )
            connection.execute('COMMIT')
        except BaseException:
            if connection.in_transaction:
                connection.execute('ROLLBACK')
            raise

    @staticmethod
    def _execute_migration_script(connection: sqlite3.Connection, script: str) -> None:
        statement = ''
        for line in script.splitlines(keepends=True):
            statement += line
            if sqlite3.complete_statement(statement):
                sql = statement.strip()
                if sql:
                    connection.execute(sql)
                statement = ''
        if statement.strip():
            raise sqlite3.DatabaseError('incomplete migration statement')

    def _migration_path(self, version: int) -> Path:
        return Path(__file__).with_name('migrations') / '{:04d}_initial.sql'.format(
            version
        )

    def _probe_lock(self) -> None:
        descriptor = os.open(str(self._lock_path), os.O_CREAT | os.O_RDWR, 0o600)
        os.fchmod(descriptor, 0o600)
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except (BlockingIOError, OSError) as error:
                raise DatabaseLocked('upload database is already owned') from error
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)

    def _acquire_process_lock(self) -> None:
        lock_file = open(self._lock_path, 'a+b', buffering=0)
        os.fchmod(lock_file.fileno(), 0o600)
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError) as error:
            lock_file.close()
            raise DatabaseLocked('upload database is already owned') from error
        self._lock_file = lock_file

    def _secure_database_files(self) -> None:
        os.chmod(self._directory, 0o700)
        for path in (
            self._path,
            Path(str(self._path) + '-wal'),
            Path(str(self._path) + '-shm'),
            self._lock_path,
        ):
            if path.exists():
                os.chmod(path, 0o600)

    def _require_connection(self) -> sqlite3.Connection:
        if self._connection is None:
            raise RuntimeError('database is not open')
        return self._connection

    @classmethod
    def _validate_claim_table(cls, table: str) -> None:
        if table not in cls._CLAIM_TABLES:
            raise ValueError("invalid claim table '{}'".format(table))

    @staticmethod
    def _filesystem_type(path: Path) -> Optional[str]:
        if not sys.platform.startswith('linux'):
            return None
        resolved = os.path.realpath(str(path))
        best_match: Optional[Tuple[int, str]] = None
        try:
            with open('/proc/self/mountinfo', 'rt', encoding='utf8') as mounts:
                for line in mounts:
                    fields = line.split()
                    try:
                        separator = fields.index('-')
                        mountpoint = fields[4]
                        filesystem_type = fields[separator + 1]
                    except (IndexError, ValueError):
                        continue
                    mountpoint = (
                        mountpoint.replace('\\040', ' ')
                        .replace('\\011', '\t')
                        .replace('\\012', '\n')
                        .replace('\\134', '\\')
                    )
                    if resolved == mountpoint or resolved.startswith(
                        mountpoint.rstrip('/') + '/'
                    ):
                        candidate = (len(mountpoint), filesystem_type)
                        if best_match is None or candidate[0] > best_match[0]:
                            best_match = candidate
        except OSError:
            return None
        return None if best_match is None else best_match[1]
