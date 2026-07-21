from __future__ import annotations

import asyncio
from typing import (
    Awaitable,
    Callable,
    Dict,
    Iterable,
    Mapping,
    Optional,
    Sequence,
    Tuple,
    TypeVar,
)

from loguru import logger

from ..control.operations import (
    ClaimedControlStep,
    ControlOperationJournal,
    ControlOperationSnapshot,
    ControlStepInput,
)
from ..exception import NotFoundError
from ..logging.audit import audit
from ..setting.file_work import _drain_admitted_work
from ..setting.setting_manager import SettingsManager
from .task_manager import RecordTaskManager

__all__ = ('TaskControlReconciler',)

_T = TypeVar('_T')


class TaskControlReconciler:
    LANE = 'task-state'
    _CONTROL_KINDS = frozenset(
        (
            'start',
            'stop',
            'force_stop',
            'recorder_enable',
            'recorder_disable',
            'recorder_force_disable',
            'recover',
        )
    )

    def __init__(
        self,
        journal: ControlOperationJournal,
        settings_manager: SettingsManager,
        task_manager: RecordTaskManager,
    ) -> None:
        self._journal = journal
        self._settings_manager = settings_manager
        self._task_manager = task_manager
        self._wake_event = asyncio.Event()
        self._idle_event = asyncio.Event()
        self._idle_event.set()
        self._stop_event = asyncio.Event()
        self._worker: Optional[asyncio.Task[None]] = None
        self._accepting = True
        self._submission_lock = asyncio.Lock()
        self._desired_absent_provider: Callable[[int], bool] = lambda _room_id: False
        self._remote_slots = asyncio.Semaphore(2)
        self._room_locks: Dict[int, asyncio.Lock] = {}
        self._desired_state_locks: Dict[int, asyncio.Lock] = {}
        self._active_steps = 0

    def set_desired_absent_provider(self, provider: Callable[[int], bool]) -> None:
        self._desired_absent_provider = provider

    def start(self) -> None:
        if self._worker is not None and not self._worker.done():
            return
        self._stop_event.clear()
        self._worker = asyncio.create_task(self._run())
        self._worker.add_done_callback(lambda _worker: self._idle_event.set())
        self.wake()

    def close_admission(self) -> None:
        self._accepting = False

    def wake(self) -> None:
        self._idle_event.clear()
        self._wake_event.set()

    async def submit(
        self,
        kind: str,
        room_ids: Iterable[int],
        *,
        rejected: Mapping[int, str],
        force: bool,
    ) -> ControlOperationSnapshot:
        if kind not in self._CONTROL_KINDS - {'recover'}:
            raise ValueError('unsupported task control kind: {}'.format(kind))
        requested_room_ids = tuple(dict.fromkeys(int(room_id) for room_id in room_ids))
        accepted_room_ids = tuple(
            room_id for room_id in requested_room_ids if room_id not in rejected
        )
        if not requested_room_ids:
            raise ValueError('task control operation must contain a room')
        persisted_kind = self._force_kind(kind, force)
        target_key = ','.join(str(room_id) for room_id in sorted(requested_room_ids))
        cancelled = False
        async with self._submission_lock:
            if not self._accepting:
                raise RuntimeError('task control admission is closed')

            async def admit_and_persist() -> ControlOperationSnapshot:
                operation = await self._journal.admit(
                    lane=self.LANE,
                    kind=persisted_kind,
                    target_key=target_key,
                    steps=[
                        ControlStepInput(
                            key=str(room_id),
                            status='rejected' if room_id in rejected else 'queued',
                            error_code=rejected.get(room_id),
                            result=(
                                {'roomId': room_id} if room_id in rejected else None
                            ),
                        )
                        for room_id in requested_room_ids
                    ],
                )
                try:
                    await self._persist_desired_state(persisted_kind, accepted_room_ids)
                    await self._journal.record_task_state_intents(
                        operation.id, accepted_room_ids
                    )
                except BaseException:
                    await self._journal.fail_unclaimed_operation(
                        operation.id, error_code='SETTINGS_PERSIST_FAILED'
                    )
                    raise
                return operation

            async def commit_submission() -> ControlOperationSnapshot:
                operation = await self._run_with_desired_state_locks(
                    accepted_room_ids, admit_and_persist
                )
                await self._journal.supersede_queued_steps(
                    lane=self.LANE,
                    keys=[str(room_id) for room_id in accepted_room_ids],
                    keep_operation_id=operation.id,
                    generation=operation.generation,
                )
                refreshed = await self._journal.get(operation.id)
                assert refreshed is not None
                return refreshed

            operation, cancelled = await _drain_admitted_work(commit_submission())
        self.wake()
        if cancelled:
            raise asyncio.CancelledError
        return operation

    async def recover(self) -> Optional[ControlOperationSnapshot]:
        mismatches = []
        settings = self._settings_manager.get_settings({'tasks'}).tasks or []
        for task_settings in settings:
            room_id = task_settings.room_id
            if self._desired_absent_provider(room_id):
                continue
            if not self._task_manager.has_task(room_id):
                continue
            try:
                actual = self._task_manager.get_task_control_state(room_id)
            except (NotFoundError, RuntimeError):
                continue
            desired = (task_settings.enable_monitor, task_settings.enable_recorder)
            if actual != desired:
                mismatches.append(room_id)
        if not mismatches:
            self.wake()
            return None
        operation = await self._journal.admit(
            lane=self.LANE,
            kind='recover',
            target_key=','.join(str(room_id) for room_id in sorted(mismatches)),
            steps=[ControlStepInput(key=str(room_id)) for room_id in mismatches],
        )
        self.wake()
        return operation

    async def reconcile_membership_start(
        self,
        room_id: int,
        *,
        membership_operation_id: str,
        reuse_info_revision: Optional[int],
    ) -> Tuple[bool, bool]:
        """Persist and apply one membership operation's desired start state."""

        return await self.run_room_action(
            room_id,
            lambda: self._reconcile_membership_start_unlocked(
                room_id,
                membership_operation_id=membership_operation_id,
                reuse_info_revision=reuse_info_revision,
            ),
        )

    async def _reconcile_membership_start_unlocked(
        self,
        room_id: int,
        *,
        membership_operation_id: str,
        reuse_info_revision: Optional[int],
    ) -> Tuple[bool, bool]:
        async def capture_desired_state() -> Tuple[bool, bool]:
            if self._desired_absent_provider(room_id):
                return False, False
            has_later_intent = await self._journal.has_later_task_state_intent(
                membership_operation_id, room_id
            )
            if has_later_intent:
                return self._settings_manager.get_task_desired_state(room_id)
            await self._persist_desired_state('start', (room_id,))
            return True, True

        desired = await self._run_with_desired_state_locks(
            (room_id,), capture_desired_state
        )
        return await self._reconcile_room_unlocked(
            room_id,
            kind='start',
            force=False,
            reuse_info_revision=reuse_info_revision,
            desired_override=desired,
        )

    async def run_room_action(
        self, room_id: int, action: Callable[[], Awaitable[_T]]
    ) -> _T:
        room_lock = self._room_locks.setdefault(room_id, asyncio.Lock())
        async with room_lock:
            async with self._remote_slots:
                return await action()

    async def wait_idle(self) -> None:
        while True:
            await self._idle_event.wait()
            worker = self._worker
            if worker is not None and worker.done():
                await worker
            if (
                self._active_steps == 0
                and await self._journal.queued_count(self.LANE) == 0
            ):
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
            claim_error: Optional[BaseException] = None
            async with self._submission_lock:
                claims = []
                for _index in range(2):
                    try:
                        claim = await self._journal.claim_next(self.LANE)
                    except BaseException as claim_exception:
                        claim_error = claim_exception
                        break
                    if claim is None:
                        break
                    self._idle_event.clear()
                    self._active_steps += 1
                    claims.append(claim)
            if claims:
                results = await asyncio.gather(
                    *(self._reconcile_claim(claim) for claim in claims),
                    return_exceptions=True,
                )
                self._active_steps -= len(claims)
                if claim_error is not None:
                    raise claim_error
                step_error = next(
                    (result for result in results if isinstance(result, BaseException)),
                    None,
                )
                if step_error is not None:
                    raise step_error
                continue
            if claim_error is not None:
                raise claim_error
            if not claims:
                self._idle_event.set()
                self._wake_event.clear()
                if await self._journal.queued_count(self.LANE) != 0:
                    self._idle_event.clear()
                    continue
                wake_task = asyncio.create_task(self._wake_event.wait())
                stop_task = asyncio.create_task(self._stop_event.wait())
                done, pending = await asyncio.wait(
                    (wake_task, stop_task), return_when=asyncio.FIRST_COMPLETED
                )
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
                if stop_task in done and stop_task.result():
                    return
                self._idle_event.clear()
                continue

    async def _reconcile_claim(self, claim: ClaimedControlStep) -> None:
        room_id = int(claim.key)
        try:
            final = await self._reconcile_room(
                room_id, kind=claim.kind, force=self._is_force(claim.kind)
            )
        except NotFoundError:
            await self._journal.finish_step(
                claim, status='failed', error_code='TASK_NOT_FOUND'
            )
        except Exception as error:
            logger.error(
                'Task control operation {} failed for room {}: {!r}',
                claim.operation_id,
                room_id,
                error,
            )
            audit(
                'task_control_failed',
                level='ERROR',
                operation_id=claim.operation_id,
                room_id=room_id,
                error_code='TASK_LIFECYCLE_FAILED',
            )
            await self._journal.finish_step(
                claim, status='failed', error_code='TASK_LIFECYCLE_FAILED'
            )
        else:
            await self._journal.finish_step(
                claim,
                status='succeeded',
                result={
                    'roomId': room_id,
                    'monitorEnabled': final[0],
                    'recorderEnabled': final[1],
                },
            )

    async def _reconcile_room(
        self,
        room_id: int,
        *,
        kind: str,
        force: bool,
        reuse_info_revision: Optional[int] = None,
    ) -> Tuple[bool, bool]:
        return await self.run_room_action(
            room_id,
            lambda: self._reconcile_room_unlocked(
                room_id, kind=kind, force=force, reuse_info_revision=reuse_info_revision
            ),
        )

    async def _reconcile_room_unlocked(
        self,
        room_id: int,
        *,
        kind: str,
        force: bool,
        reuse_info_revision: Optional[int],
        desired_override: Optional[Tuple[bool, bool]] = None,
    ) -> Tuple[bool, bool]:
        if desired_override is not None:
            desired = desired_override
        else:
            desired = (
                (False, False)
                if self._desired_absent_provider(room_id)
                else self._settings_manager.get_task_desired_state(room_id)
            )
        actual = self._task_manager.get_task_control_state(room_id)
        await self._apply(
            room_id,
            actual,
            desired,
            kind=kind,
            force=force,
            reuse_info_revision=reuse_info_revision,
        )
        final = self._task_manager.get_task_control_state(room_id)
        if final != desired:
            raise RuntimeError('task lifecycle did not reach its desired state')
        return final

    async def _persist_desired_state(self, kind: str, room_ids: Sequence[int]) -> None:
        if not room_ids:
            return
        if kind == 'start':
            await self._settings_manager.change_task_desired_states(
                room_ids, enable_monitor=True, enable_recorder=True
            )
        elif kind in {'stop', 'force_stop'}:
            await self._settings_manager.change_task_desired_states(
                room_ids, enable_monitor=False, enable_recorder=False
            )
        elif kind == 'recorder_enable':
            await self._settings_manager.change_task_desired_states(
                room_ids, enable_recorder=True
            )
        else:
            await self._settings_manager.change_task_desired_states(
                room_ids, enable_recorder=False
            )

    async def _run_with_desired_state_locks(
        self, room_ids: Sequence[int], action: Callable[[], Awaitable[_T]]
    ) -> _T:
        locks = [
            self._desired_state_locks.setdefault(room_id, asyncio.Lock())
            for room_id in sorted(set(room_ids))
        ]
        acquired = []
        try:
            for lock in locks:
                await lock.acquire()
                acquired.append(lock)
            return await action()
        finally:
            for lock in reversed(acquired):
                lock.release()

    async def _apply(
        self,
        room_id: int,
        actual: Tuple[bool, bool],
        desired: Tuple[bool, bool],
        *,
        kind: str,
        force: bool,
        reuse_info_revision: Optional[int] = None,
    ) -> None:
        if actual == desired:
            return
        if kind == 'start' and desired == (True, True):
            if reuse_info_revision is None:
                await self._task_manager.start_task(room_id)
            else:
                await self._task_manager.start_task(
                    room_id, reuse_info_revision=reuse_info_revision
                )
            return
        if kind in {'stop', 'force_stop'} and desired == (False, False):
            await self._task_manager.stop_task(room_id, force)
            return
        monitor_enabled, recorder_enabled = actual
        desired_monitor, desired_recorder = desired
        if desired_monitor and not monitor_enabled:
            await self._task_manager.enable_task_monitor(room_id)
            monitor_enabled = True
        if desired_recorder and not recorder_enabled:
            await self._task_manager.enable_task_recorder(room_id)
            recorder_enabled = True
        if not desired_recorder and recorder_enabled:
            await self._task_manager.disable_task_recorder(room_id, force)
            recorder_enabled = False
        if not desired_monitor and monitor_enabled:
            await self._task_manager.disable_task_monitor(room_id)

    @staticmethod
    def _force_kind(kind: str, force: bool) -> str:
        if not force:
            return kind
        if kind == 'stop':
            return 'force_stop'
        if kind == 'recorder_disable':
            return 'recorder_force_disable'
        return kind

    @staticmethod
    def _is_force(kind: str) -> bool:
        return kind in {'force_stop', 'recorder_force_disable'}
