from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import (
    Awaitable,
    Callable,
    Dict,
    Iterator,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
)
from unittest.mock import AsyncMock

import pytest

from blrec.control.operations import (
    ControlJournalError,
    ControlOperationJournal,
    ControlStepInput,
)
from blrec.setting.models import Settings, TaskSettings
from blrec.setting.setting_manager import SettingsManager
from blrec.task.membership_reconciler import RoomMembershipReconciler


class FakeSettingsApplication:
    pass


class FakeTaskManager:
    def __init__(self) -> None:
        self.tasks: Dict[int, object] = {}
        self.add_calls: List[Tuple[int, bool]] = []
        self.remove_calls: List[int] = []
        self.next_info_revision = 7

    def has_task(self, room_id: int) -> bool:
        return room_id in self.tasks

    async def add_task(
        self, settings: TaskSettings, *, apply_desired_state: bool = True
    ) -> None:
        self.add_calls.append((settings.room_id, apply_desired_state))
        self.tasks[settings.room_id] = SimpleNamespace(
            info_revision=self.next_info_revision
        )

    def get_task_info_revision(self, room_id: int) -> int:
        return int(self.tasks[room_id].info_revision)

    async def remove_task(self, room_id: int) -> None:
        self.remove_calls.append(room_id)
        self.tasks.pop(room_id, None)

    def get_all_task_room_ids(self) -> Iterator[int]:
        yield from self.tasks


class FakeTaskControl:
    def __init__(self) -> None:
        self.calls: List[Tuple[str, Tuple[int, ...], Mapping[int, str], bool]] = []
        self.membership_calls: List[Tuple[int, Optional[int]]] = []
        self.membership_final = (True, True)

    async def submit(
        self,
        kind: str,
        room_ids: Sequence[int],
        *,
        rejected: Mapping[int, str],
        force: bool,
    ) -> SimpleNamespace:
        self.calls.append((kind, tuple(room_ids), rejected, force))
        return SimpleNamespace(id='task-state-operation')

    async def reconcile_membership_start(
        self,
        room_id: int,
        *,
        membership_operation_id: str,
        reuse_info_revision: Optional[int],
    ) -> Tuple[bool, bool]:
        del membership_operation_id
        self.membership_calls.append((room_id, reuse_info_revision))
        return self.membership_final

    async def run_room_action(
        self, room_id: int, action: Callable[[], Awaitable[object]]
    ) -> object:
        del room_id
        return await action()


class ConcurrentTaskManager(FakeTaskManager):
    def __init__(self) -> None:
        super().__init__()
        self.release = asyncio.Event()
        self.two_entered = asyncio.Event()
        self.in_flight = 0
        self.max_in_flight = 0

    async def add_task(
        self, settings: TaskSettings, *, apply_desired_state: bool = True
    ) -> None:
        self.add_calls.append((settings.room_id, apply_desired_state))
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        if self.in_flight >= 2:
            self.two_entered.set()
        try:
            await self.release.wait()
            self.tasks[settings.room_id] = SimpleNamespace(
                info_revision=self.next_info_revision
            )
        finally:
            self.in_flight -= 1


def make_settings_manager(settings: Settings) -> SettingsManager:
    manager = SettingsManager(FakeSettingsApplication(), settings)
    manager.dump_settings = AsyncMock()
    return manager


@pytest.mark.asyncio
async def test_collect_is_deduplicated_and_returns_resolved_terminal_result(
    tmp_path: Path,
) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()

    async def resolve(room_id: int) -> int:
        entered.set()
        await release.wait()
        return 3582149 if room_id == 6 else room_id

    policy = AsyncMock()
    settings = make_settings_manager(Settings())
    tasks = FakeTaskManager()
    controls = FakeTaskControl()
    journal = ControlOperationJournal(tmp_path / 'control.sqlite3')
    await journal.open()
    reconciler = RoomMembershipReconciler(
        journal,
        settings,
        tasks,
        controls,
        room_id_resolver=resolve,
        upload_policy_enabler=policy,
    )
    reconciler.start()
    try:
        first = await reconciler.submit_collect(6, upload=True)
        duplicate = await reconciler.submit_collect(6, upload=True)

        assert duplicate.id == first.id
        await entered.wait()
        assert first.status == 'accepted'
        release.set()
        await reconciler.wait_idle()

        final = await journal.get(first.id)
        assert final is not None
        assert final.status == 'succeeded'
        assert final.result is not None
        assert final.result['requestedRoomId'] == 6
        assert final.result['resolvedRoomId'] == 3582149
        assert final.result['collected'] is True
        assert final.result['upload'] is True
        assert [step.key for step in final.steps] == [
            'resolve',
            'add',
            'desired-state',
            'policy',
        ]
        assert {step.status for step in final.steps} == {'succeeded'}
        assert tasks.add_calls == [(3582149, False)]
        assert controls.calls == []
        assert controls.membership_calls == [(3582149, 7)]
        add_step = next(step for step in final.steps if step.key == 'add')
        assert add_step.result == {
            'roomId': 3582149,
            'alreadyPresent': False,
            'infoRevision': 7,
        }
        desired_step = next(step for step in final.steps if step.key == 'desired-state')
        assert desired_step.result == {
            'roomId': 3582149,
            'monitorEnabled': True,
            'recorderEnabled': True,
        }
        policy.assert_awaited_once_with(3582149)
        settings.dump_settings.assert_awaited_once()
    finally:
        await reconciler.shutdown()
        await journal.close()


@pytest.mark.asyncio
async def test_membership_step_records_the_actual_reconciled_state(
    tmp_path: Path,
) -> None:
    settings = make_settings_manager(Settings())
    tasks = FakeTaskManager()
    controls = FakeTaskControl()
    controls.membership_final = (False, False)
    journal = ControlOperationJournal(tmp_path / 'control.sqlite3')
    await journal.open()
    reconciler = RoomMembershipReconciler(
        journal, settings, tasks, controls, room_id_resolver=AsyncMock(return_value=100)
    )
    reconciler.start()
    try:
        operation = await reconciler.submit_add(100)
        await reconciler.wait_idle()

        final = await journal.get(operation.id)
        assert final is not None and final.status == 'succeeded'
        desired_step = next(step for step in final.steps if step.key == 'desired-state')
        assert desired_step.result == {
            'roomId': 100,
            'monitorEnabled': False,
            'recorderEnabled': False,
        }
    finally:
        await reconciler.shutdown()
        await journal.close()


@pytest.mark.asyncio
async def test_membership_runs_two_room_disjoint_operations_concurrently(
    tmp_path: Path,
) -> None:
    settings = make_settings_manager(Settings())
    tasks = ConcurrentTaskManager()
    controls = FakeTaskControl()
    journal = ControlOperationJournal(tmp_path / 'control.sqlite3')
    await journal.open()
    reconciler = RoomMembershipReconciler(
        journal,
        settings,
        tasks,
        controls,
        room_id_resolver=AsyncMock(side_effect=lambda room_id: room_id),
    )
    reconciler.start()
    first = await reconciler.submit_add(100)
    second = await reconciler.submit_add(200)
    try:
        await asyncio.wait_for(tasks.two_entered.wait(), timeout=0.2)
        assert tasks.max_in_flight == 2
        tasks.release.set()
        await reconciler.wait_idle()

        first_final = await journal.get(first.id)
        second_final = await journal.get(second.id)
        assert first_final is not None and first_final.status == 'succeeded'
        assert second_final is not None and second_final.status == 'succeeded'
    finally:
        tasks.release.set()
        await reconciler.shutdown()
        await journal.close()


@pytest.mark.asyncio
async def test_wait_idle_does_not_cross_a_claimed_membership_step(
    tmp_path: Path,
) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()

    async def resolve(room_id: int) -> int:
        entered.set()
        await release.wait()
        return room_id

    journal = ControlOperationJournal(tmp_path / 'control.sqlite3')
    await journal.open()
    await journal.admit(
        lane='room-membership',
        kind='add',
        target_key='100',
        result={'requestedRoomId': 100, 'upload': False},
        steps=(ControlStepInput(key='resolve'),),
    )
    reconciler = RoomMembershipReconciler(
        journal,
        make_settings_manager(Settings()),
        FakeTaskManager(),
        FakeTaskControl(),
        room_id_resolver=resolve,
    )
    real_claim_next = journal.claim_next

    async def claim_next(lane: str):
        claim = await real_claim_next(lane)
        if claim is not None:
            reconciler._idle_event.set()
        return claim

    journal.claim_next = claim_next  # type: ignore[method-assign]
    reconciler.start()
    waiting = asyncio.create_task(reconciler.wait_idle())
    try:
        await asyncio.wait_for(entered.wait(), timeout=0.2)
        await asyncio.sleep(0)
        assert not waiting.done()
        release.set()
        await asyncio.wait_for(waiting, timeout=0.2)
    finally:
        release.set()
        await asyncio.gather(waiting, return_exceptions=True)
        await reconciler.shutdown()
        await journal.close()


@pytest.mark.asyncio
async def test_membership_worker_failure_wakes_shutdown_and_propagates(
    tmp_path: Path,
) -> None:
    journal = ControlOperationJournal(tmp_path / 'control.sqlite3')
    await journal.open()
    reconciler = RoomMembershipReconciler(
        journal,
        make_settings_manager(Settings()),
        FakeTaskManager(),
        FakeTaskControl(),
        room_id_resolver=AsyncMock(return_value=100),
    )
    await reconciler.submit_add(100)
    journal.finish_step = AsyncMock(  # type: ignore[method-assign]
        side_effect=ControlJournalError('journal unavailable')
    )
    reconciler.start()
    try:
        with pytest.raises(ControlJournalError, match='journal unavailable'):
            await asyncio.wait_for(reconciler.shutdown(), timeout=0.2)
        assert reconciler._active_steps == 0
    finally:
        worker = reconciler._worker
        if worker is not None:
            await asyncio.gather(worker, return_exceptions=True)
        await journal.close()


@pytest.mark.asyncio
async def test_second_membership_claim_failure_drains_the_first_child(
    tmp_path: Path,
) -> None:
    resolver_entered = asyncio.Event()
    resolver_release = asyncio.Event()

    async def resolve(room_id: int) -> int:
        resolver_entered.set()
        await resolver_release.wait()
        return room_id

    journal = ControlOperationJournal(tmp_path / 'control.sqlite3')
    await journal.open()
    reconciler = RoomMembershipReconciler(
        journal,
        make_settings_manager(Settings()),
        FakeTaskManager(),
        FakeTaskControl(),
        room_id_resolver=resolve,
    )
    await reconciler.submit_add(100)
    real_claim_next = journal.claim_next
    claim_calls = 0

    async def fail_second_claim(lane: str):
        nonlocal claim_calls
        claim_calls += 1
        if claim_calls == 2:
            raise ControlJournalError('second claim failed')
        return await real_claim_next(lane)

    journal.claim_next = fail_second_claim  # type: ignore[method-assign]
    reconciler.start()
    shutdown = asyncio.create_task(reconciler.shutdown())
    try:
        await asyncio.wait_for(resolver_entered.wait(), timeout=0.2)
        await asyncio.sleep(0)
        assert not shutdown.done()
        resolver_release.set()
        with pytest.raises(ControlJournalError, match='second claim failed'):
            await asyncio.wait_for(shutdown, timeout=0.2)
        assert reconciler._active_steps == 0
    finally:
        resolver_release.set()
        await asyncio.gather(shutdown, return_exceptions=True)
        worker = reconciler._worker
        if worker is not None:
            await asyncio.gather(worker, return_exceptions=True)
        await journal.close()


@pytest.mark.asyncio
async def test_scope_journal_failure_propagates_from_the_worker(tmp_path: Path) -> None:
    journal = ControlOperationJournal(tmp_path / 'control.sqlite3')
    await journal.open()
    reconciler = RoomMembershipReconciler(
        journal,
        make_settings_manager(Settings()),
        FakeTaskManager(),
        FakeTaskControl(),
        room_id_resolver=AsyncMock(return_value=100),
    )
    await reconciler.submit_remove([], remove_all=True)
    journal.finish_step = AsyncMock(  # type: ignore[method-assign]
        side_effect=ControlJournalError('scope journal unavailable')
    )
    reconciler.start()
    try:
        with pytest.raises(ControlJournalError, match='scope journal unavailable'):
            await asyncio.wait_for(reconciler.shutdown(), timeout=0.2)
        assert reconciler._active_steps == 0
    finally:
        worker = reconciler._worker
        if worker is not None:
            await asyncio.gather(worker, return_exceptions=True)
        await journal.close()


@pytest.mark.asyncio
async def test_desired_state_infrastructure_failure_is_not_reclassified(
    tmp_path: Path,
) -> None:
    journal = ControlOperationJournal(tmp_path / 'control.sqlite3')
    await journal.open()
    controls = FakeTaskControl()
    controls.reconcile_membership_start = AsyncMock(  # type: ignore[method-assign]
        side_effect=ControlJournalError('intent journal unavailable')
    )
    reconciler = RoomMembershipReconciler(
        journal,
        make_settings_manager(Settings()),
        FakeTaskManager(),
        controls,
        room_id_resolver=AsyncMock(return_value=100),
    )
    await reconciler.submit_add(100)
    reconciler.start()
    try:
        with pytest.raises(ControlJournalError, match='intent journal unavailable'):
            await asyncio.wait_for(reconciler.shutdown(), timeout=0.2)
        assert reconciler._active_steps == 0
    finally:
        worker = reconciler._worker
        if worker is not None:
            await asyncio.gather(worker, return_exceptions=True)
        await journal.close()


@pytest.mark.asyncio
async def test_room_resolver_os_error_is_persisted_as_a_step_failure(
    tmp_path: Path,
) -> None:
    journal = ControlOperationJournal(tmp_path / 'control.sqlite3')
    await journal.open()
    reconciler = RoomMembershipReconciler(
        journal,
        make_settings_manager(Settings()),
        FakeTaskManager(),
        FakeTaskControl(),
        room_id_resolver=AsyncMock(side_effect=OSError('network unavailable')),
    )
    operation = await reconciler.submit_add(100)
    reconciler.start()
    try:
        await asyncio.wait_for(reconciler.shutdown(), timeout=0.2)
        final = await journal.get(operation.id)
        assert final is not None and final.status == 'failed'
        assert final.steps[0].error_code == 'ROOM_RESOLVE_FAILED'
    finally:
        await journal.close()


@pytest.mark.asyncio
async def test_failed_collect_retry_creates_new_attempt_and_skips_existing_add(
    tmp_path: Path,
) -> None:
    resolved_room_ids = iter((100, 200))
    resolve_calls: List[int] = []

    async def resolve(room_id: int) -> int:
        resolve_calls.append(room_id)
        return next(resolved_room_ids)

    policy = AsyncMock(side_effect=[RuntimeError('not ready'), None])
    settings = make_settings_manager(Settings())
    tasks = FakeTaskManager()
    controls = FakeTaskControl()
    journal = ControlOperationJournal(tmp_path / 'control.sqlite3')
    await journal.open()
    reconciler = RoomMembershipReconciler(
        journal,
        settings,
        tasks,
        controls,
        room_id_resolver=resolve,
        upload_policy_enabler=policy,
    )
    reconciler.start()
    try:
        first = await reconciler.submit_collect(6, upload=True)
        await reconciler.wait_idle()
        failed = await journal.get(first.id)
        assert failed is not None
        assert failed.status == 'failed'
        assert failed.error_code == 'UPLOAD_POLICY_FAILED'
        assert failed.result is not None
        assert failed.result['upload'] is False

        retry = await reconciler.submit_collect(6, upload=True)
        await reconciler.wait_idle()
        final = await journal.get(retry.id)

        assert retry.id != first.id
        assert retry.attempt == 2
        assert final is not None and final.status == 'succeeded'
        assert final.result is not None
        assert final.result['resolvedRoomId'] == 100
        assert resolve_calls == [6]
        assert tasks.add_calls == [(100, False)]
        assert settings.dump_settings.await_count == 1
        assert policy.await_count == 2
        assert [args.args for args in policy.await_args_list] == [(100,), (100,)]
    finally:
        await reconciler.shutdown()
        await journal.close()


@pytest.mark.asyncio
async def test_remove_tears_down_before_one_settings_dump_and_absent_is_noop(
    tmp_path: Path,
) -> None:
    settings = make_settings_manager(Settings(tasks=[TaskSettings(room_id=100)]))
    tasks = FakeTaskManager()
    tasks.tasks[100] = object()
    journal = ControlOperationJournal(tmp_path / 'control.sqlite3')
    await journal.open()
    reconciler = RoomMembershipReconciler(
        journal,
        settings,
        tasks,
        FakeTaskControl(),
        room_id_resolver=AsyncMock(side_effect=lambda room_id: room_id),
    )
    reconciler.start()
    try:
        operation = await reconciler.submit_remove([100])
        assert await reconciler.pending_removal_room_ids() == {100}
        await reconciler.wait_idle()

        final = await journal.get(operation.id)
        assert final is not None and final.status == 'succeeded'
        assert tasks.remove_calls == [100]
        assert not settings.has_task_settings(100)
        settings.dump_settings.assert_awaited_once()

        second = await reconciler.submit_remove([100])
        await reconciler.wait_idle()
        second_final = await journal.get(second.id)
        assert second_final is not None and second_final.status == 'succeeded'
        assert tasks.remove_calls == [100]
        assert settings.dump_settings.await_count == 1
    finally:
        await reconciler.shutdown()
        await journal.close()


@pytest.mark.asyncio
async def test_recovered_add_observes_existing_task_before_acting(
    tmp_path: Path,
) -> None:
    path = tmp_path / 'control.sqlite3'
    first_journal = ControlOperationJournal(path)
    await first_journal.open()
    operation = await first_journal.admit(
        lane='room-membership',
        kind='add',
        target_key='100',
        result={'requestedRoomId': 100, 'upload': False},
        steps=(
            # Resolve was durably completed before the process died.
            # The add side effect happened, but its CAS did not.
            # Recovery must observe the task instead of adding it twice.
            #
            # The explicit generation remains owned by the journal.
            ControlStepInput(key='resolve'),
            ControlStepInput(key='add'),
            ControlStepInput(key='desired-state'),
        ),
    )
    resolve_claim = await first_journal.claim_next('room-membership')
    assert resolve_claim is not None
    await first_journal.finish_step(
        resolve_claim,
        status='succeeded',
        result={'requestedRoomId': 100, 'resolvedRoomId': 100},
        operation_result={'resolvedRoomId': 100},
    )
    add_claim = await first_journal.claim_next('room-membership')
    assert add_claim is not None and add_claim.key == 'add'
    await first_journal.close()

    tasks = FakeTaskManager()
    tasks.tasks[100] = object()
    settings = make_settings_manager(Settings(tasks=[TaskSettings(room_id=100)]))
    reopened = ControlOperationJournal(path)
    await reopened.open()
    reconciler = RoomMembershipReconciler(
        reopened,
        settings,
        tasks,
        FakeTaskControl(),
        room_id_resolver=AsyncMock(side_effect=lambda room_id: room_id),
    )
    reconciler.start()
    try:
        await reconciler.wait_idle()
        final = await reopened.get(operation.id)
        assert final is not None and final.status == 'succeeded'
        assert tasks.add_calls == []
        settings.dump_settings.assert_not_awaited()
    finally:
        await reconciler.shutdown()
        await reopened.close()


@pytest.mark.asyncio
async def test_existing_task_does_not_invent_a_reusable_info_revision(
    tmp_path: Path,
) -> None:
    settings = make_settings_manager(Settings(tasks=[TaskSettings(room_id=100)]))
    tasks = FakeTaskManager()
    tasks.tasks[100] = SimpleNamespace(info_revision=99)
    controls = FakeTaskControl()
    journal = ControlOperationJournal(tmp_path / 'control.sqlite3')
    await journal.open()
    reconciler = RoomMembershipReconciler(
        journal, settings, tasks, controls, room_id_resolver=AsyncMock(return_value=100)
    )
    reconciler.start()
    try:
        operation = await reconciler.submit_add(100)
        await reconciler.wait_idle()

        final = await journal.get(operation.id)
        assert final is not None and final.status == 'succeeded'
        add_step = next(step for step in final.steps if step.key == 'add')
        assert add_step.result == {'roomId': 100, 'alreadyPresent': True}
        assert controls.membership_calls == [(100, None)]
    finally:
        await reconciler.shutdown()
        await journal.close()


@pytest.mark.asyncio
async def test_restart_consumes_the_revision_persisted_by_the_add_step(
    tmp_path: Path,
) -> None:
    path = tmp_path / 'control.sqlite3'
    first_journal = ControlOperationJournal(path)
    await first_journal.open()
    operation = await first_journal.admit(
        lane='room-membership',
        kind='collect',
        target_key='100:0',
        result={
            'requestedRoomId': 100,
            'resolvedRoomId': 100,
            'collected': True,
            'upload': False,
        },
        steps=(
            ControlStepInput(key='resolve'),
            ControlStepInput(key='add'),
            ControlStepInput(key='desired-state'),
        ),
    )
    resolve = await first_journal.claim_next('room-membership')
    assert resolve is not None and resolve.key == 'resolve'
    await first_journal.finish_step(
        resolve,
        status='succeeded',
        result={'requestedRoomId': 100, 'resolvedRoomId': 100},
    )
    add = await first_journal.claim_next('room-membership')
    assert add is not None and add.key == 'add'
    await first_journal.finish_step(
        add,
        status='succeeded',
        result={'roomId': 100, 'alreadyPresent': False, 'infoRevision': 13},
    )
    desired = await first_journal.claim_next('room-membership')
    assert desired is not None and desired.key == 'desired-state'
    await first_journal.close()

    reopened = ControlOperationJournal(path)
    await reopened.open()
    tasks = FakeTaskManager()
    tasks.tasks[100] = SimpleNamespace(info_revision=13)
    controls = FakeTaskControl()
    reconciler = RoomMembershipReconciler(
        reopened,
        make_settings_manager(Settings(tasks=[TaskSettings(room_id=100)])),
        tasks,
        controls,
        room_id_resolver=AsyncMock(return_value=100),
    )
    reconciler.start()
    try:
        await reconciler.wait_idle()

        final = await reopened.get(operation.id)
        assert final is not None and final.status == 'succeeded'
        assert controls.membership_calls == [(100, 13)]
    finally:
        await reconciler.shutdown()
        await reopened.close()


@pytest.mark.asyncio
async def test_remove_all_scopes_pending_add_and_persisted_settings_at_execution(
    tmp_path: Path,
) -> None:
    settings = make_settings_manager(Settings(tasks=[TaskSettings(room_id=200)]))
    tasks = FakeTaskManager()
    journal = ControlOperationJournal(tmp_path / 'control.sqlite3')
    await journal.open()
    reconciler = RoomMembershipReconciler(
        journal,
        settings,
        tasks,
        FakeTaskControl(),
        room_id_resolver=AsyncMock(side_effect=lambda room_id: room_id),
    )
    try:
        add = await reconciler.submit_add(100)
        remove_all = await reconciler.submit_remove([], remove_all=True)
        reconciler.start()
        await reconciler.wait_idle()

        added = await journal.get(add.id)
        removed = await journal.get(remove_all.id)
        assert added is not None and added.status == 'succeeded'
        assert removed is not None and removed.status == 'succeeded'
        assert removed.result is not None
        assert removed.result['roomIds'] == [100, 200]
        assert [step.key for step in removed.steps] == [
            'scope',
            'desired-absent',
            'teardown:100',
            'teardown:200',
            'settings',
        ]
        assert list(tasks.get_all_task_room_ids()) == []
        assert settings.get_settings({'tasks'}).tasks == []
    finally:
        await reconciler.shutdown()
        await journal.close()


@pytest.mark.asyncio
async def test_shutdown_drains_every_admitted_membership_operation(
    tmp_path: Path,
) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()

    async def resolve(room_id: int) -> int:
        if room_id == 100:
            entered.set()
            await release.wait()
        return room_id

    settings = make_settings_manager(Settings())
    tasks = FakeTaskManager()
    journal = ControlOperationJournal(tmp_path / 'control.sqlite3')
    await journal.open()
    reconciler = RoomMembershipReconciler(
        journal, settings, tasks, FakeTaskControl(), room_id_resolver=resolve
    )
    reconciler.start()
    try:
        first = await reconciler.submit_add(100)
        second = await reconciler.submit_add(200)
        await entered.wait()

        shutdown = asyncio.create_task(reconciler.shutdown())
        await asyncio.sleep(0)
        assert not shutdown.done()
        release.set()
        await shutdown

        first_final = await journal.get(first.id)
        second_final = await journal.get(second.id)
        assert first_final is not None and first_final.status == 'succeeded'
        assert second_final is not None and second_final.status == 'succeeded'
        assert list(tasks.get_all_task_room_ids()) == [100, 200]
    finally:
        if not release.is_set():
            release.set()
        await reconciler.shutdown()
        await journal.close()


@pytest.mark.asyncio
async def test_pending_remove_all_prevents_startup_task_recreation(
    tmp_path: Path,
) -> None:
    path = tmp_path / 'control.sqlite3'
    settings = make_settings_manager(Settings(tasks=[TaskSettings(room_id=100)]))
    journal = ControlOperationJournal(path)
    await journal.open()
    reconciler = RoomMembershipReconciler(
        journal,
        settings,
        FakeTaskManager(),
        FakeTaskControl(),
        room_id_resolver=AsyncMock(side_effect=lambda room_id: room_id),
    )
    await reconciler.submit_remove([], remove_all=True)
    await journal.close()

    reopened = ControlOperationJournal(path)
    await reopened.open()
    recovered = RoomMembershipReconciler(
        reopened,
        settings,
        FakeTaskManager(),
        FakeTaskControl(),
        room_id_resolver=AsyncMock(side_effect=lambda room_id: room_id),
    )
    try:
        assert await recovered.pending_removal_room_ids() == {100}
    finally:
        await reopened.close()
