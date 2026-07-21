from __future__ import annotations

import asyncio
from typing import Awaitable, Callable, Dict, Iterable, Optional, Sequence, Set, Tuple

from loguru import logger

from ..control.operations import (
    ClaimedControlStep,
    ControlJournalClosed,
    ControlJournalError,
    ControlOperationJournal,
    ControlOperationSnapshot,
    ControlStepInput,
)
from ..logging.audit import audit
from ..setting.setting_manager import SettingsManager
from .control_reconciler import TaskControlReconciler
from .task_manager import RecordTaskManager

__all__ = ('RoomMembershipReconciler',)

RoomIdResolver = Callable[[int], Awaitable[int]]
UploadPolicyEnabler = Callable[[int], Awaitable[None]]


class RoomMembershipReconciler:
    """Durably reconcile room membership outside HTTP request lifetimes."""

    LANE = 'room-membership'

    def __init__(
        self,
        journal: ControlOperationJournal,
        settings_manager: SettingsManager,
        task_manager: RecordTaskManager,
        task_control: TaskControlReconciler,
        *,
        room_id_resolver: RoomIdResolver,
        upload_policy_enabler: Optional[UploadPolicyEnabler] = None,
    ) -> None:
        self._journal = journal
        self._settings_manager = settings_manager
        self._task_manager = task_manager
        self._task_control = task_control
        self._room_id_resolver = room_id_resolver
        self._upload_policy_enabler = upload_policy_enabler
        self._wake_event = asyncio.Event()
        self._idle_event = asyncio.Event()
        self._idle_event.set()
        self._stop_event = asyncio.Event()
        self._worker: Optional[asyncio.Task[None]] = None
        self._accepting = True
        self._submission_lock = asyncio.Lock()
        self._desired_absent_room_ids: Set[int] = set()
        self._active_steps = 0

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

    async def submit_add(self, room_id: int) -> ControlOperationSnapshot:
        operation = await self._admit(
            kind='add',
            target_key=str(room_id),
            result={'requestedRoomId': room_id, 'upload': False},
            steps=('resolve', 'add', 'desired-state'),
            reuse_succeeded_step_keys=('resolve',),
        )
        self.wake()
        return operation

    async def submit_collect(
        self, room_id: int, *, upload: bool
    ) -> ControlOperationSnapshot:
        steps = ['resolve', 'add', 'desired-state']
        if upload:
            steps.append('policy')
        operation = await self._admit(
            kind='collect',
            target_key='{}:{}'.format(room_id, int(upload)),
            result={
                'requestedRoomId': room_id,
                'requestedUpload': upload,
                'upload': False,
            },
            steps=tuple(steps),
            reuse_succeeded_step_keys=('resolve',),
        )
        self.wake()
        return operation

    async def submit_remove(
        self, room_ids: Iterable[int], *, remove_all: bool = False
    ) -> ControlOperationSnapshot:
        normalized = tuple(dict.fromkeys(int(room_id) for room_id in room_ids))
        if not normalized and not remove_all:
            raise ValueError('room membership removal must contain a room')
        kind = 'remove_all' if remove_all else 'remove'
        target_key = (
            'all'
            if remove_all
            else ','.join(str(room_id) for room_id in sorted(normalized))
        )
        steps = (
            ('scope',)
            if remove_all
            else tuple(
                ['desired-absent']
                + ['teardown:{}'.format(room_id) for room_id in normalized]
                + ['settings']
            )
        )
        operation = await self._admit(
            kind=kind,
            target_key=target_key,
            result={
                'requestedRoomId': None if remove_all else normalized[0],
                'roomIds': list(normalized),
                'upload': False,
            },
            steps=steps,
        )
        if not remove_all:
            self._desired_absent_room_ids.update(normalized)
        self.wake()
        return operation

    def desires_absent(self, room_id: int) -> bool:
        return room_id in self._desired_absent_room_ids

    async def pending_removal_room_ids(self) -> Set[int]:
        room_ids: Set[int] = set()
        for operation in await self._journal.list_nonterminal(self.LANE):
            if operation.kind not in {'remove', 'remove_all'}:
                continue
            result = operation.result or {}
            value = result.get('roomIds')
            if isinstance(value, list):
                room_ids.update(
                    int(room_id) for room_id in value if isinstance(room_id, int)
                )
            scope_completed = any(
                step.key == 'scope' and step.status == 'succeeded'
                for step in operation.steps
            )
            if operation.kind == 'remove_all' and not scope_completed:
                room_ids.update(self._all_membership_room_ids())
        self._desired_absent_room_ids.update(room_ids)
        return room_ids

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

    async def _admit(
        self,
        *,
        kind: str,
        target_key: str,
        result: Dict[str, object],
        steps: Sequence[str],
        reuse_succeeded_step_keys: Sequence[str] = (),
    ) -> ControlOperationSnapshot:
        async with self._submission_lock:
            if not self._accepting:
                raise RuntimeError('room membership admission is closed')
            operation = await self._journal.admit(
                lane=self.LANE,
                kind=kind,
                target_key=target_key,
                result=result,
                steps=[ControlStepInput(key=key) for key in steps],
                reuse_succeeded_step_keys=reuse_succeeded_step_keys,
            )
        return operation

    async def _run(self) -> None:
        pending: Set[asyncio.Task[None]] = set()
        while not self._stop_event.is_set():
            claim_error: Optional[BaseException] = None
            while len(pending) < 2:
                try:
                    async with self._submission_lock:
                        claim = await self._journal.claim_next(self.LANE)
                except BaseException as claim_exception:
                    claim_error = claim_exception
                    break
                if claim is None:
                    break
                self._idle_event.clear()
                self._active_steps += 1
                pending.add(asyncio.create_task(self._reconcile_claim(claim)))
            if claim_error is not None:
                if pending:
                    await asyncio.gather(*pending, return_exceptions=True)
                    self._active_steps -= len(pending)
                    pending = set()
                raise claim_error
            if pending:
                done, pending = await asyncio.wait(
                    pending, return_when=asyncio.FIRST_COMPLETED
                )
                results = await asyncio.gather(*done, return_exceptions=True)
                self._active_steps -= len(done)
                step_error = next(
                    (result for result in results if isinstance(result, BaseException)),
                    None,
                )
                if step_error is not None:
                    await asyncio.gather(*pending, return_exceptions=True)
                    self._active_steps -= len(pending)
                    raise step_error
                continue
            else:
                self._idle_event.set()
                self._wake_event.clear()
                if await self._journal.queued_count(self.LANE) != 0:
                    self._idle_event.clear()
                    continue
                wake_task = asyncio.create_task(self._wake_event.wait())
                stop_task = asyncio.create_task(self._stop_event.wait())
                wait_done, wait_pending = await asyncio.wait(
                    (wake_task, stop_task), return_when=asyncio.FIRST_COMPLETED
                )
                for task in wait_pending:
                    task.cancel()
                await asyncio.gather(*wait_pending, return_exceptions=True)
                if stop_task in wait_done and stop_task.result():
                    return
                self._idle_event.clear()
                continue

    async def _reconcile_claim(self, claim: ClaimedControlStep) -> None:
        operation = await self._journal.get(claim.operation_id)
        if operation is None:
            return
        resolved_operation: ControlOperationSnapshot = operation
        try:
            if claim.key == 'scope':
                room_ids = self._all_membership_room_ids()
                append_steps = tuple(
                    [ControlStepInput(key='desired-absent')]
                    + [
                        ControlStepInput(key='teardown:{}'.format(room_id))
                        for room_id in room_ids
                    ]
                    + [ControlStepInput(key='settings')]
                )
                completed = await self._journal.finish_step(
                    claim,
                    status='succeeded',
                    result={'roomIds': list(room_ids)},
                    operation_result={'roomIds': list(room_ids), 'collected': False},
                    append_steps=append_steps,
                )
                if completed:
                    self._desired_absent_room_ids.update(room_ids)
                return
            room_action_key = self._room_action_key(resolved_operation, claim)
            if room_action_key is None:
                step_result, operation_result = await self._apply_step(
                    resolved_operation, claim
                )
            else:
                step_result, operation_result = (
                    await self._task_control.run_room_action(
                        room_action_key,
                        lambda: self._apply_step(resolved_operation, claim),
                    )
                )
        except (ControlJournalClosed, ControlJournalError):
            raise
        except Exception as error:
            error_code = self._error_code(claim.key)
            logger.error(
                'Room membership operation {} step {} failed: {!r}',
                claim.operation_id,
                claim.key,
                error,
            )
            audit(
                'room_membership_failed',
                level='ERROR',
                operation_id=claim.operation_id,
                kind=claim.kind,
                step=claim.key,
                error_code=error_code,
            )
            await self._journal.fail_step_and_dependents(
                claim, error_code=error_code, dependent_error_code='DEPENDENCY_FAILED'
            )
            return
        await self._journal.finish_step(
            claim,
            status='succeeded',
            result=step_result,
            operation_result=operation_result,
        )

    async def _apply_step(
        self, operation: ControlOperationSnapshot, claim: ClaimedControlStep
    ) -> Tuple[Dict[str, object], Dict[str, object]]:
        if claim.key == 'resolve':
            requested_room_id = self._requested_room_id(operation)
            resolved_room_id = await self._room_id_resolver(requested_room_id)
            resolve_result: Dict[str, object] = {
                'requestedRoomId': requested_room_id,
                'resolvedRoomId': resolved_room_id,
            }
            return resolve_result, {'resolvedRoomId': resolved_room_id}

        if claim.key == 'add':
            room_id = self._resolved_room_id(operation)
            settings = await self._settings_manager.ensure_task_settings(room_id)
            already_present = self._task_manager.has_task(room_id)
            if not already_present:
                await self._task_manager.add_task(settings, apply_desired_state=False)
            add_result: Dict[str, object] = {
                'roomId': room_id,
                'alreadyPresent': already_present,
            }
            if not already_present:
                add_result['infoRevision'] = self._task_manager.get_task_info_revision(
                    room_id
                )
            return (add_result, {'resolvedRoomId': room_id, 'collected': True})

        if claim.key == 'desired-state':
            room_id = self._resolved_room_id(operation)
            final = await self._task_control.reconcile_membership_start(
                room_id,
                membership_operation_id=operation.id,
                reuse_info_revision=self._persisted_add_info_revision(operation),
            )
            return (
                {
                    'roomId': room_id,
                    'monitorEnabled': final[0],
                    'recorderEnabled': final[1],
                },
                {'resolvedRoomId': room_id, 'collected': True},
            )

        if claim.key == 'policy':
            room_id = self._resolved_room_id(operation)
            if self._upload_policy_enabler is None:
                raise RuntimeError('upload policy service is not ready')
            await self._upload_policy_enabler(room_id)
            return (
                {'roomId': room_id, 'enabled': True},
                {'resolvedRoomId': room_id, 'collected': True, 'upload': True},
            )

        if claim.key == 'desired-absent':
            room_ids = self._removal_room_ids(operation)
            return {'roomIds': list(room_ids)}, {'collected': False}

        if claim.key.startswith('teardown:'):
            room_id = int(claim.key.split(':', 1)[1])
            already_absent = not self._task_manager.has_task(room_id)
            if not already_absent:
                await self._task_manager.remove_task(room_id)
            return {'roomId': room_id, 'alreadyAbsent': already_absent}, {}

        if claim.key == 'settings':
            room_ids = self._removal_room_ids(operation)
            removed = await self._settings_manager.remove_task_settings_batch(room_ids)
            self._desired_absent_room_ids.difference_update(room_ids)
            return (
                {'roomIds': list(room_ids), 'removedRoomIds': sorted(removed)},
                {'collected': False},
            )

        raise ValueError('unsupported room membership step: {}'.format(claim.key))

    @staticmethod
    def _requested_room_id(operation: ControlOperationSnapshot) -> int:
        value = (operation.result or {}).get('requestedRoomId')
        if not isinstance(value, int) or value <= 0:
            raise ValueError('room membership operation has no requested room ID')
        return value

    @staticmethod
    def _resolved_room_id(operation: ControlOperationSnapshot) -> int:
        value = (operation.result or {}).get('resolvedRoomId')
        if not isinstance(value, int) or value <= 0:
            raise ValueError('room membership operation has no resolved room ID')
        return value

    @staticmethod
    def _removal_room_ids(operation: ControlOperationSnapshot) -> Sequence[int]:
        value = (operation.result or {}).get('roomIds')
        if not isinstance(value, list):
            raise ValueError('room membership operation has no removal rooms')
        room_ids = tuple(int(room_id) for room_id in value)
        if not room_ids and operation.kind != 'remove_all':
            raise ValueError('room membership operation has no removal rooms')
        return room_ids

    @staticmethod
    def _persisted_add_info_revision(
        operation: ControlOperationSnapshot,
    ) -> Optional[int]:
        for step in operation.steps:
            if step.key != 'add' or step.status != 'succeeded' or step.result is None:
                continue
            value = step.result.get('infoRevision')
            if isinstance(value, int) and value > 0:
                return value
        return None

    def _all_membership_room_ids(self) -> Sequence[int]:
        settings = self._settings_manager.get_settings({'tasks'}).tasks
        assert settings is not None
        room_ids = set(self._task_manager.get_all_task_room_ids())
        room_ids.update(task.room_id for task in settings)
        return tuple(sorted(room_ids))

    def _room_action_key(
        self, operation: ControlOperationSnapshot, claim: ClaimedControlStep
    ) -> Optional[int]:
        if claim.key == 'resolve':
            return self._requested_room_id(operation)
        if claim.key in {'add', 'policy'}:
            return self._resolved_room_id(operation)
        if claim.key.startswith('teardown:'):
            return int(claim.key.split(':', 1)[1])
        return None

    @staticmethod
    def _error_code(step: str) -> str:
        if step == 'resolve':
            return 'ROOM_RESOLVE_FAILED'
        if step == 'add':
            return 'TASK_ADD_FAILED'
        if step == 'desired-state':
            return 'TASK_STATE_FAILED'
        if step == 'policy':
            return 'UPLOAD_POLICY_FAILED'
        if step.startswith('teardown:'):
            return 'TASK_TEARDOWN_FAILED'
        if step == 'settings':
            return 'SETTINGS_PERSIST_FAILED'
        if step == 'scope':
            return 'MEMBERSHIP_SCOPE_FAILED'
        return 'ROOM_MEMBERSHIP_FAILED'
