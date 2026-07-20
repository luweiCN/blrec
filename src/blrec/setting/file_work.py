from __future__ import annotations

import asyncio
import errno
import os
import tempfile
from concurrent.futures import Future, ThreadPoolExecutor
from functools import partial
from pathlib import Path
from threading import Lock
from typing import Any, Awaitable, Callable, Optional, Sequence, Set, Tuple, TypeVar

import toml

from ..control.operations import (
    ClaimedControlStep,
    ControlOperationJournal,
    ControlOperationSnapshot,
    ControlRevisionSnapshot,
)
from .models import Settings

__all__ = (
    'SettingsFileWorkClosed',
    'SettingsFileWorkCoordinator',
    'SettingsFileWorkSaturated',
    'SettingsDirectoryError',
    'SettingsApplyReconciler',
    'validate_directory_sync',
)

_T = TypeVar('_T')


class SettingsFileWorkClosed(RuntimeError):
    pass


class SettingsFileWorkSaturated(RuntimeError):
    retry_after = 1


class SettingsDirectoryError(ValueError):
    def __init__(self, path: str, code: int, message: str) -> None:
        super().__init__('{}: {}'.format(path, message))
        self.path = path
        self.code = code
        self.message = message


async def _drain_admitted_work(work: Awaitable[_T]) -> Tuple[_T, bool]:
    task = asyncio.ensure_future(work)
    cancelled = False
    while True:
        try:
            return await asyncio.shield(task), cancelled
        except asyncio.CancelledError:
            cancelled = True
            if task.done():
                return task.result(), cancelled


def validate_directory_sync(path: str) -> Tuple[int, str]:
    normalized = os.path.normpath(os.path.expanduser(path))
    if not os.path.isdir(normalized):
        return errno.ENOTDIR, 'not a directory'
    if not os.access(normalized, os.F_OK | os.R_OK | os.W_OK):
        return errno.EACCES, 'no permissions'
    return 0, 'ok'


class SettingsFileWorkCoordinator:
    """Bounded executor for settings validation and durable file replacement."""

    def __init__(self, *, max_active: int = 2, max_waiting: int = 8) -> None:
        if max_active <= 0 or max_waiting < 0:
            raise ValueError('settings file work limits are invalid')
        self._capacity = max_active + max_waiting
        self._executor = ThreadPoolExecutor(
            max_workers=max_active, thread_name_prefix='blrec-settings-file'
        )
        self._futures: Set[Future[Any]] = set()
        self._futures_lock = Lock()
        self._admission_lock = asyncio.Lock()
        self._accepting = True
        self._closed = False

    def close_admission(self) -> None:
        self._accepting = False

    async def run(self, function: Callable[..., _T], *args: Any) -> _T:
        async with self._admission_lock:
            if not self._accepting or self._closed:
                raise SettingsFileWorkClosed('settings file work is closed')
            with self._futures_lock:
                if len(self._futures) >= self._capacity:
                    raise SettingsFileWorkSaturated('settings file work is saturated')
                future = self._executor.submit(partial(function, *args))
                self._futures.add(future)
            future.add_done_callback(self._discard_future)
        result, cancelled = await _drain_admitted_work(asyncio.wrap_future(future))
        if cancelled:
            raise asyncio.CancelledError
        return result

    def _discard_future(self, future: Future[Any]) -> None:
        with self._futures_lock:
            self._futures.discard(future)

    async def atomic_dump(
        self, settings: Settings, *, validate_paths: Tuple[str, ...] = ()
    ) -> None:
        await self.run(_atomic_dump_sync, settings, validate_paths)

    async def validate_directory(self, path: str) -> Tuple[int, str]:
        return await self.run(validate_directory_sync, path)

    async def shutdown(self) -> None:
        async with self._admission_lock:
            if self._closed:
                return
            self.close_admission()
            with self._futures_lock:
                futures = tuple(self._futures)
        if futures:
            await asyncio.gather(
                *(asyncio.shield(asyncio.wrap_future(future)) for future in futures),
                return_exceptions=True,
            )
        self._executor.shutdown(wait=True)
        self._closed = True


class SettingsApplyReconciler:
    """Single durable owner for applying already-persisted settings revisions."""

    LANE = 'settings-apply'

    def __init__(
        self,
        journal: ControlOperationJournal,
        apply: Callable[[str, str], Awaitable[None]],
    ) -> None:
        self._journal = journal
        self._apply = apply
        self._wake_event = asyncio.Event()
        self._idle_event = asyncio.Event()
        self._idle_event.set()
        self._stop_event = asyncio.Event()
        self._worker: Optional[asyncio.Task[None]] = None
        self._accepting = True
        self._submission_lock = asyncio.Lock()

    def start(self) -> None:
        if self._worker is not None and not self._worker.done():
            return
        self._stop_event.clear()
        self._worker = asyncio.create_task(self._run())
        self.wake()

    def close_admission(self) -> None:
        self._accepting = False

    def wake(self) -> None:
        self._idle_event.clear()
        self._wake_event.set()

    async def submit(self, target_key: str, action: str) -> ControlOperationSnapshot:
        async with self._submission_lock:
            if not self._accepting:
                raise RuntimeError('settings apply admission is closed')
            operation = await self._journal.submit_revision(
                lane=self.LANE, kind='apply', target_key=target_key, action=action
            )
        self.wake()
        return operation

    async def commit_revisions(
        self,
        revisions: Sequence[Tuple[str, str]],
        commit: Callable[[], Awaitable[None]],
    ) -> Tuple[ControlOperationSnapshot, ...]:
        normalized = tuple(revisions)
        reserved = False
        try:
            async with self._submission_lock:
                if not self._accepting:
                    raise RuntimeError('settings apply admission is closed')
                await self._journal.reserve_revisions(
                    lane=self.LANE, kind='apply', revisions=normalized
                )
                reserved = True
                await commit()
                recovered = await self._journal.recover_revision_gaps(
                    lane=self.LANE, kind='apply'
                )
        finally:
            if reserved:
                self.wake()
        target_keys = {target_key for target_key, _action in normalized}
        return tuple(
            operation for operation in recovered if operation.target_key in target_keys
        )

    async def retry(
        self, target_keys: Sequence[str]
    ) -> Tuple[ControlOperationSnapshot, ...]:
        normalized = frozenset(target_keys)
        if not normalized:
            return ()
        async with self._submission_lock:
            if not self._accepting:
                raise RuntimeError('settings apply admission is closed')
            recovered = await self._journal.recover_revision_gaps(
                lane=self.LANE, kind='apply'
            )
        self.wake()
        return tuple(
            operation for operation in recovered if operation.target_key in normalized
        )

    async def recover(self) -> Tuple[ControlOperationSnapshot, ...]:
        recovered = tuple(
            await self._journal.recover_revision_gaps(lane=self.LANE, kind='apply')
        )
        if recovered:
            self.wake()
        return recovered

    async def wait_idle(self) -> None:
        while True:
            await self._idle_event.wait()
            if await self._journal.queued_count(self.LANE) == 0:
                return
            self._idle_event.clear()

    async def shutdown(self) -> None:
        self.close_admission()
        async with self._submission_lock:
            pass
        worker = self._worker
        if worker is None:
            if await self._journal.queued_count(self.LANE) == 0:
                return
            self.start()
            worker = self._worker
            assert worker is not None
        await self.wait_idle()
        self._stop_event.set()
        self._wake_event.set()
        await worker
        self._worker = None

    async def _run(self) -> None:
        while not self._stop_event.is_set():
            async with self._submission_lock:
                await self._journal.recover_revision_gaps(
                    lane=self.LANE, kind='apply', unassigned_only=True
                )
                claim = await self._journal.claim_next(self.LANE)
                revision = (
                    None
                    if claim is None
                    else await self._journal.get_revision(self.LANE, claim.key)
                )
            if claim is None:
                self._idle_event.set()
                self._wake_event.clear()
                if await self._journal.queued_count(self.LANE) != 0:
                    self._idle_event.clear()
                    continue
                wake_task = asyncio.create_task(self._wake_event.wait())
                stop_task = asyncio.create_task(self._stop_event.wait())
                _done, pending = await asyncio.wait(
                    (wake_task, stop_task), return_when=asyncio.FIRST_COMPLETED
                )
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
                self._idle_event.clear()
                continue
            self._idle_event.clear()
            await self._reconcile(claim, revision)

    async def _reconcile(
        self, claim: ClaimedControlStep, revision: Optional[ControlRevisionSnapshot]
    ) -> None:
        if revision is None:
            await self._journal.finish_step(
                claim, status='failed', error_code='SETTINGS_REVISION_MISSING'
            )
            return
        desired_revision = revision.desired_revision
        try:
            await self._apply(claim.key, revision.action)
        except Exception:
            await self._journal.finish_step(
                claim, status='failed', error_code='SETTINGS_APPLY_FAILED'
            )
            return
        await self._journal.finish_revision_step(
            claim, applied_revision=desired_revision
        )


def _atomic_dump_sync(settings: Settings, validate_paths: Tuple[str, ...]) -> None:
    for directory in validate_paths:
        code, message = validate_directory_sync(directory)
        if code:
            raise SettingsDirectoryError(directory, code, message)
    path = Path(os.path.abspath(os.path.expanduser(settings._path)))
    payload = toml.dumps(settings.dict(exclude_none=True)).encode('utf8')
    descriptor: Optional[int] = None
    temporary_path: Optional[str] = None
    try:
        descriptor, temporary_path = tempfile.mkstemp(
            dir=str(path.parent), prefix='.{}.'.format(path.name), suffix='.tmp'
        )
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, 'wb') as file:
            descriptor = None
            file.write(payload)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary_path, str(path))
        temporary_path = None
        _fsync_directory(path.parent)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if temporary_path is not None:
            try:
                os.unlink(temporary_path)
            except FileNotFoundError:
                pass


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY
    if hasattr(os, 'O_DIRECTORY'):
        flags |= os.O_DIRECTORY
    try:
        descriptor = os.open(str(path), flags)
    except OSError as error:
        if error.errno in {errno.EINVAL, errno.ENOTSUP}:
            return
        raise
    try:
        try:
            os.fsync(descriptor)
        except OSError as error:
            if error.errno not in {errno.EINVAL, errno.ENOTSUP}:
                raise
    finally:
        os.close(descriptor)
