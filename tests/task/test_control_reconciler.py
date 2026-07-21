from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any, List, Optional, Set, Tuple
from unittest.mock import AsyncMock, Mock

import pytest

from blrec.control.operations import (
    ControlJournalError,
    ControlOperationJournal,
    ControlStepInput,
)
from blrec.setting.models import Settings, TaskSettings
from blrec.setting.setting_manager import SettingsManager
from blrec.task.control_reconciler import TaskControlReconciler
from blrec.task.task import RecordTask


class FakeSettingsApplication:
    pass


class FakeTaskManager:
    def __init__(self) -> None:
        self.states = {
            100: SimpleNamespace(monitor_enabled=False, recorder_enabled=False)
        }
        self.calls: List[Tuple[str, int]] = []
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.start_failures = 0

    def has_task(self, room_id: int) -> bool:
        return room_id in self.states

    def get_task_control_state(self, room_id: int) -> Tuple[bool, bool]:
        state = self.states[room_id]
        return state.monitor_enabled, state.recorder_enabled

    async def enable_task_monitor(self, room_id: int) -> None:
        self.calls.append(('monitor_on', room_id))
        self.entered.set()
        await self.release.wait()
        self.states[room_id].monitor_enabled = True

    async def start_task(self, room_id: int) -> None:
        self.calls.append(('start', room_id))
        self.entered.set()
        await self.release.wait()
        self.states[room_id].monitor_enabled = True
        if self.start_failures:
            self.start_failures -= 1
            raise RuntimeError('recorder start failed')
        self.states[room_id].recorder_enabled = True

    async def stop_task(self, room_id: int, force: bool = False) -> None:
        self.calls.append(('force_stop' if force else 'stop', room_id))
        self.states[room_id].monitor_enabled = False
        self.states[room_id].recorder_enabled = False

    async def disable_task_monitor(self, room_id: int) -> None:
        self.calls.append(('monitor_off', room_id))
        self.states[room_id].monitor_enabled = False

    async def enable_task_recorder(self, room_id: int) -> None:
        self.calls.append(('recorder_on', room_id))
        self.states[room_id].recorder_enabled = True

    async def disable_task_recorder(self, room_id: int, force: bool = False) -> None:
        self.calls.append(('recorder_force_off' if force else 'recorder_off', room_id))
        self.states[room_id].recorder_enabled = False


class ConcurrentTaskManager(FakeTaskManager):
    def __init__(self, room_ids: Tuple[int, ...]) -> None:
        super().__init__()
        self.states = {
            room_id: SimpleNamespace(monitor_enabled=False, recorder_enabled=False)
            for room_id in room_ids
        }
        self.release = asyncio.Event()
        self.first_entered = asyncio.Event()
        self.two_entered = asyncio.Event()
        self.in_flight = 0
        self.max_in_flight = 0
        self.per_room_in_flight = {room_id: 0 for room_id in room_ids}
        self.per_room_max = {room_id: 0 for room_id in room_ids}

    async def start_task(
        self, room_id: int, *, reuse_info_revision: Optional[int] = None
    ) -> None:
        del reuse_info_revision
        self.calls.append(('start', room_id))
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        self.per_room_in_flight[room_id] += 1
        self.per_room_max[room_id] = max(
            self.per_room_max[room_id], self.per_room_in_flight[room_id]
        )
        self.first_entered.set()
        if self.in_flight >= 2:
            self.two_entered.set()
        try:
            await self.release.wait()
            self.states[room_id].monitor_enabled = True
            self.states[room_id].recorder_enabled = True
        finally:
            self.per_room_in_flight[room_id] -= 1
            self.in_flight -= 1


def make_record_task(*, monitor_enabled: bool, recorder_enabled: bool) -> RecordTask:
    task = object.__new__(RecordTask)
    task._monitor_enabled = monitor_enabled
    task._recorder_enabled = recorder_enabled
    task._batch_monitoring = False
    task._danmaku_client = SimpleNamespace(start=AsyncMock(), stop=AsyncMock())
    task._live_monitor = SimpleNamespace(enable=Mock(), disable=Mock())
    task._postprocessor = SimpleNamespace(start=AsyncMock(), stop=AsyncMock())
    task._recorder = SimpleNamespace(start=AsyncMock(), stop=AsyncMock())
    return task


def make_settings_manager(settings: Settings) -> SettingsManager:
    manager = SettingsManager(
        FakeSettingsApplication(), settings  # type: ignore[arg-type]
    )
    manager.dump_settings = AsyncMock()  # type: ignore[method-assign]
    return manager


async def admit_membership_operation(
    journal: ControlOperationJournal, room_id: int = 100
) -> Any:
    return await journal.admit(
        lane='room-membership',
        kind='collect',
        target_key='{}:0'.format(room_id),
        result={'requestedRoomId': room_id},
        steps=[ControlStepInput(key='desired-state')],
    )


@pytest.mark.asyncio
async def test_reconciler_serializes_lifecycle_and_exposes_terminal_step(
    tmp_path: Path,
) -> None:
    settings_manager = make_settings_manager(
        Settings(
            tasks=[
                TaskSettings(room_id=100, enable_monitor=False, enable_recorder=False)
            ]
        )
    )
    await settings_manager.change_task_desired_states(
        [100], enable_monitor=False, enable_recorder=False
    )
    journal = ControlOperationJournal(tmp_path / 'control.sqlite3')
    await journal.open()
    task_manager = FakeTaskManager()
    reconciler = TaskControlReconciler(journal, settings_manager, task_manager)
    reconciler.start()
    try:
        operation = await reconciler.submit('start', [100], rejected={}, force=False)
        await task_manager.entered.wait()
        running = await journal.get(operation.id)
        assert running is not None
        assert running.status == 'running'
        task_manager.release.set()
        await reconciler.wait_idle()

        final = await journal.get(operation.id)
        assert final is not None
        assert final.status == 'succeeded'
        assert final.steps[0].result == {
            'roomId': 100,
            'monitorEnabled': True,
            'recorderEnabled': True,
        }
        assert task_manager.calls == [('start', 100)]
    finally:
        task_manager.release.set()
        await reconciler.shutdown()
        await journal.close()


@pytest.mark.asyncio
async def test_reconciler_runs_two_room_disjoint_steps_and_keeps_input_order(
    tmp_path: Path,
) -> None:
    room_ids = (300, 100, 200)
    settings_manager = make_settings_manager(
        Settings(tasks=[TaskSettings(room_id=room_id) for room_id in room_ids])
    )
    journal = ControlOperationJournal(tmp_path / 'control.sqlite3')
    await journal.open()
    task_manager = ConcurrentTaskManager(room_ids)
    reconciler = TaskControlReconciler(journal, settings_manager, task_manager)
    reconciler.start()
    operation = await reconciler.submit('start', room_ids, rejected={}, force=False)
    try:
        await asyncio.wait_for(task_manager.two_entered.wait(), timeout=0.2)
        assert task_manager.max_in_flight == 2
        task_manager.release.set()
        await reconciler.wait_idle()

        final = await journal.get(operation.id)
        assert final is not None and final.status == 'succeeded'
        assert [step.key for step in final.steps] == ['300', '100', '200']
    finally:
        task_manager.release.set()
        await reconciler.shutdown()
        await journal.close()


@pytest.mark.asyncio
async def test_wait_idle_does_not_cross_a_claimed_task_control_step(
    tmp_path: Path,
) -> None:
    settings_manager = make_settings_manager(
        Settings(
            tasks=[
                TaskSettings(room_id=100, enable_monitor=False, enable_recorder=False)
            ]
        )
    )
    journal = ControlOperationJournal(tmp_path / 'control.sqlite3')
    await journal.open()
    task_manager = FakeTaskManager()
    reconciler = TaskControlReconciler(journal, settings_manager, task_manager)
    await reconciler.submit('start', [100], rejected={}, force=False)
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
        await asyncio.wait_for(task_manager.entered.wait(), timeout=0.2)
        await asyncio.sleep(0)
        assert not waiting.done()
        task_manager.release.set()
        await asyncio.wait_for(waiting, timeout=0.2)
    finally:
        task_manager.release.set()
        await asyncio.gather(waiting, return_exceptions=True)
        await reconciler.shutdown()
        await journal.close()


@pytest.mark.asyncio
async def test_membership_and_direct_control_serialize_the_same_room(
    tmp_path: Path,
) -> None:
    settings_manager = make_settings_manager(
        Settings(
            tasks=[
                TaskSettings(room_id=100, enable_monitor=False, enable_recorder=False)
            ]
        )
    )
    journal = ControlOperationJournal(tmp_path / 'control.sqlite3')
    await journal.open()
    membership_operation = await admit_membership_operation(journal)
    task_manager = ConcurrentTaskManager((100,))
    reconciler = TaskControlReconciler(journal, settings_manager, task_manager)
    reconciler.start()
    await reconciler.submit('start', [100], rejected={}, force=False)
    membership = None
    try:
        await asyncio.wait_for(task_manager.first_entered.wait(), timeout=0.2)
        membership = asyncio.create_task(
            reconciler.reconcile_membership_start(
                100,
                membership_operation_id=membership_operation.id,
                reuse_info_revision=9,
            )
        )
        await asyncio.sleep(0)
        assert task_manager.per_room_max[100] == 1
        task_manager.release.set()
        await membership
        await reconciler.wait_idle()
        assert task_manager.calls == [('start', 100)]
    finally:
        task_manager.release.set()
        if membership is not None:
            await asyncio.gather(membership, return_exceptions=True)
        await reconciler.shutdown()
        await journal.close()


@pytest.mark.asyncio
async def test_membership_persists_start_while_holding_the_remote_room_lock(
    tmp_path: Path,
) -> None:
    settings_manager = make_settings_manager(
        Settings(
            tasks=[
                TaskSettings(room_id=100, enable_monitor=False, enable_recorder=False)
            ]
        )
    )
    persist_entered = asyncio.Event()
    persist_release = asyncio.Event()
    real_change = settings_manager.change_task_desired_states

    async def change_desired_state(*args: Any, **kwargs: Any) -> Set[int]:
        persist_entered.set()
        await persist_release.wait()
        return await real_change(*args, **kwargs)

    settings_manager.change_task_desired_states = (  # type: ignore[method-assign]
        change_desired_state
    )
    journal = ControlOperationJournal(tmp_path / 'control.sqlite3')
    await journal.open()
    membership_operation = await admit_membership_operation(journal)
    task_manager = FakeTaskManager()
    task_manager.release.set()
    reconciler = TaskControlReconciler(journal, settings_manager, task_manager)
    membership = asyncio.create_task(
        reconciler.reconcile_membership_start(
            100,
            membership_operation_id=membership_operation.id,
            reuse_info_revision=None,
        )
    )
    await asyncio.wait_for(persist_entered.wait(), timeout=0.2)
    competitor_entered = asyncio.Event()

    async def competing_action() -> None:
        competitor_entered.set()

    competitor = asyncio.create_task(reconciler.run_room_action(100, competing_action))
    try:
        await asyncio.sleep(0)
        assert not competitor_entered.is_set()
        persist_release.set()
        assert await membership == (True, True)
        await competitor
    finally:
        persist_release.set()
        await asyncio.gather(membership, competitor, return_exceptions=True)
        await reconciler.shutdown()
        await journal.close()


@pytest.mark.asyncio
async def test_direct_submit_does_not_wait_for_membership_remote_lifecycle(
    tmp_path: Path,
) -> None:
    settings_manager = make_settings_manager(
        Settings(
            tasks=[
                TaskSettings(room_id=100, enable_monitor=False, enable_recorder=False)
            ]
        )
    )
    journal = ControlOperationJournal(tmp_path / 'control.sqlite3')
    await journal.open()
    membership_operation = await admit_membership_operation(journal)
    task_manager = FakeTaskManager()
    reconciler = TaskControlReconciler(journal, settings_manager, task_manager)
    membership = asyncio.create_task(
        reconciler.reconcile_membership_start(
            100,
            membership_operation_id=membership_operation.id,
            reuse_info_revision=None,
        )
    )
    shutdown_complete = False
    try:
        await asyncio.wait_for(task_manager.entered.wait(), timeout=0.2)
        operation = await asyncio.wait_for(
            reconciler.submit('stop', [100], rejected={}, force=False), timeout=0.2
        )
        assert operation.status == 'accepted'
        task_manager.release.set()
        assert await membership == (True, True)
        await reconciler.shutdown()
        shutdown_complete = True
        assert task_manager.get_task_control_state(100) == (False, False)
        final = await journal.get(operation.id)
        assert final is not None and final.status == 'succeeded'
    finally:
        task_manager.release.set()
        await asyncio.gather(membership, return_exceptions=True)
        if not shutdown_complete:
            await reconciler.shutdown()
        await journal.close()


@pytest.mark.asyncio
async def test_later_direct_intent_supersedes_older_membership_start(
    tmp_path: Path,
) -> None:
    settings_manager = make_settings_manager(
        Settings(
            tasks=[
                TaskSettings(room_id=100, enable_monitor=False, enable_recorder=False)
            ]
        )
    )
    journal = ControlOperationJournal(tmp_path / 'control.sqlite3')
    await journal.open()
    older_membership = await journal.admit(
        lane='room-membership',
        kind='collect',
        target_key='100:0',
        result={'requestedRoomId': 100},
        steps=[ControlStepInput(key='desired-state')],
    )
    task_manager = FakeTaskManager()
    task_manager.release.set()
    reconciler = TaskControlReconciler(journal, settings_manager, task_manager)
    try:
        await reconciler.submit('stop', [100], rejected={}, force=False)

        final = await reconciler.reconcile_membership_start(
            100, membership_operation_id=older_membership.id, reuse_info_revision=None
        )

        assert final == (False, False)
        assert settings_manager.get_task_desired_state(100) == (False, False)
        assert task_manager.calls == []
    finally:
        await reconciler.shutdown()
        await journal.close()


@pytest.mark.asyncio
async def test_reused_direct_operation_keeps_its_latest_admission_order(
    tmp_path: Path,
) -> None:
    settings_manager = make_settings_manager(
        Settings(
            tasks=[TaskSettings(room_id=100, enable_monitor=True, enable_recorder=True)]
        )
    )
    journal = ControlOperationJournal(tmp_path / 'control.sqlite3')
    await journal.open()
    task_manager = FakeTaskManager()
    task_manager.release.set()
    reconciler = TaskControlReconciler(journal, settings_manager, task_manager)
    try:
        first_stop = await reconciler.submit('stop', [100], rejected={}, force=False)
        membership = await admit_membership_operation(journal)
        repeated_stop = await reconciler.submit('stop', [100], rejected={}, force=False)
        assert repeated_stop.id == first_stop.id

        final = await reconciler.reconcile_membership_start(
            100, membership_operation_id=membership.id, reuse_info_revision=None
        )

        assert final == (False, False)
        assert settings_manager.get_task_desired_state(100) == (False, False)
        assert task_manager.calls == []
    finally:
        await reconciler.shutdown()
        await journal.close()


@pytest.mark.asyncio
async def test_reused_membership_keeps_its_original_admission_order(
    tmp_path: Path,
) -> None:
    settings_manager = make_settings_manager(
        Settings(
            tasks=[TaskSettings(room_id=100, enable_monitor=True, enable_recorder=True)]
        )
    )
    journal = ControlOperationJournal(tmp_path / 'control.sqlite3')
    await journal.open()
    membership = await admit_membership_operation(journal)
    task_manager = FakeTaskManager()
    task_manager.release.set()
    reconciler = TaskControlReconciler(journal, settings_manager, task_manager)
    try:
        await reconciler.submit('stop', [100], rejected={}, force=False)
        duplicate = await admit_membership_operation(journal)
        assert duplicate.id == membership.id

        final = await reconciler.reconcile_membership_start(
            100, membership_operation_id=membership.id, reuse_info_revision=None
        )

        assert final == (False, False)
        assert settings_manager.get_task_desired_state(100) == (False, False)
        assert task_manager.calls == []
    finally:
        await reconciler.shutdown()
        await journal.close()


@pytest.mark.asyncio
async def test_unpersisted_later_intents_do_not_supersede_membership_start(
    tmp_path: Path,
) -> None:
    settings_manager = make_settings_manager(
        Settings(
            tasks=[
                TaskSettings(room_id=100, enable_monitor=False, enable_recorder=False)
            ]
        )
    )
    journal = ControlOperationJournal(tmp_path / 'control.sqlite3')
    await journal.open()
    older_membership = await admit_membership_operation(journal)
    task_manager = FakeTaskManager()
    task_manager.release.set()
    reconciler = TaskControlReconciler(journal, settings_manager, task_manager)
    settings_manager.dump_settings.side_effect = OSError('disk unavailable')
    try:
        with pytest.raises(OSError, match='disk unavailable'):
            await reconciler.submit('start', [100], rejected={}, force=False)
        await reconciler.submit(
            'stop', [100], rejected={100: 'TASK_NOT_FOUND'}, force=False
        )
        settings_manager.dump_settings.side_effect = None

        final = await reconciler.reconcile_membership_start(
            100, membership_operation_id=older_membership.id, reuse_info_revision=None
        )

        assert final == (True, True)
        assert settings_manager.get_task_desired_state(100) == (True, True)
        assert task_manager.calls == [('start', 100)]
    finally:
        await reconciler.shutdown()
        await journal.close()


@pytest.mark.asyncio
async def test_membership_waits_for_later_intent_persistence_failure(
    tmp_path: Path,
) -> None:
    settings_manager = make_settings_manager(
        Settings(
            tasks=[
                TaskSettings(room_id=100, enable_monitor=False, enable_recorder=False)
            ]
        )
    )
    persist_entered = asyncio.Event()
    persist_release = asyncio.Event()
    dump_calls = 0

    async def fail_first_dump() -> None:
        nonlocal dump_calls
        dump_calls += 1
        if dump_calls == 1:
            persist_entered.set()
            await persist_release.wait()
            raise OSError('disk unavailable')

    settings_manager.dump_settings = AsyncMock(  # type: ignore[method-assign]
        side_effect=fail_first_dump
    )
    journal = ControlOperationJournal(tmp_path / 'control.sqlite3')
    await journal.open()
    older_membership = await admit_membership_operation(journal)
    task_manager = FakeTaskManager()
    task_manager.release.set()
    reconciler = TaskControlReconciler(journal, settings_manager, task_manager)
    later_start = asyncio.create_task(
        reconciler.submit('start', [100], rejected={}, force=False)
    )
    await asyncio.wait_for(persist_entered.wait(), timeout=0.2)
    membership = asyncio.create_task(
        reconciler.reconcile_membership_start(
            100, membership_operation_id=older_membership.id, reuse_info_revision=None
        )
    )
    try:
        await asyncio.sleep(0)
        assert not membership.done()
        persist_release.set()
        with pytest.raises(OSError, match='disk unavailable'):
            await later_start

        assert await membership == (True, True)
        assert settings_manager.get_task_desired_state(100) == (True, True)
        assert task_manager.calls == [('start', 100)]
    finally:
        persist_release.set()
        await asyncio.gather(later_start, membership, return_exceptions=True)
        await reconciler.shutdown()
        await journal.close()


@pytest.mark.asyncio
async def test_cancelled_submit_finishes_persisted_admission(tmp_path: Path) -> None:
    settings_manager = make_settings_manager(
        Settings(
            tasks=[
                TaskSettings(room_id=100, enable_monitor=False, enable_recorder=False)
            ]
        )
    )
    persist_entered = asyncio.Event()
    persist_release = asyncio.Event()

    async def delayed_dump() -> None:
        persist_entered.set()
        await persist_release.wait()

    settings_manager.dump_settings = AsyncMock(  # type: ignore[method-assign]
        side_effect=delayed_dump
    )
    journal = ControlOperationJournal(tmp_path / 'control.sqlite3')
    await journal.open()
    membership = await admit_membership_operation(journal)
    reconciler = TaskControlReconciler(journal, settings_manager, FakeTaskManager())
    submit = asyncio.create_task(
        reconciler.submit('start', [100], rejected={}, force=False)
    )
    try:
        await asyncio.wait_for(persist_entered.wait(), timeout=0.2)
        submit.cancel()
        persist_release.set()
        with pytest.raises(asyncio.CancelledError):
            await submit

        pending = await journal.list_nonterminal('task-state')
        assert len(pending) == 1
        assert pending[0].status == 'accepted'
        assert await journal.has_later_task_state_intent(membership.id, 100)
        assert settings_manager.get_task_desired_state(100) == (True, True)
    finally:
        persist_release.set()
        await asyncio.gather(submit, return_exceptions=True)
        await journal.close()


@pytest.mark.asyncio
async def test_cancelled_desired_state_lock_wait_releases_earlier_locks(
    tmp_path: Path,
) -> None:
    journal = ControlOperationJournal(tmp_path / 'control.sqlite3')
    await journal.open()
    reconciler = TaskControlReconciler(
        journal, make_settings_manager(Settings()), FakeTaskManager()
    )
    blocked = reconciler._desired_state_locks.setdefault(200, asyncio.Lock())
    await blocked.acquire()
    waiting = asyncio.create_task(
        reconciler._run_with_desired_state_locks((100, 200), AsyncMock())
    )
    try:
        for _index in range(10):
            first = reconciler._desired_state_locks.get(100)
            if first is not None and first.locked():
                break
            await asyncio.sleep(0)
        first = reconciler._desired_state_locks[100]
        assert first.locked()

        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        assert not first.locked()
    finally:
        blocked.release()
        await asyncio.gather(waiting, return_exceptions=True)
        await journal.close()


@pytest.mark.asyncio
async def test_failed_reuse_does_not_advance_the_direct_intent_order(
    tmp_path: Path,
) -> None:
    settings_manager = make_settings_manager(
        Settings(
            tasks=[
                TaskSettings(room_id=100, enable_monitor=False, enable_recorder=False)
            ]
        )
    )
    journal = ControlOperationJournal(tmp_path / 'control.sqlite3')
    await journal.open()
    task_manager = FakeTaskManager()
    task_manager.release.set()
    reconciler = TaskControlReconciler(journal, settings_manager, task_manager)
    try:
        await reconciler.submit('start', [100], rejected={}, force=False)
        running_start = await journal.claim_next('task-state')
        assert running_start is not None and running_start.kind == 'start'
        await reconciler.submit('stop', [100], rejected={}, force=False)
        membership = await admit_membership_operation(journal)
        settings_manager.dump_settings.side_effect = OSError('disk unavailable')

        with pytest.raises(RuntimeError, match='after it was claimed'):
            await reconciler.submit('start', [100], rejected={}, force=False)
        settings_manager.dump_settings.side_effect = None

        final = await reconciler.reconcile_membership_start(
            100, membership_operation_id=membership.id, reuse_info_revision=None
        )

        assert final == (True, True)
        assert settings_manager.get_task_desired_state(100) == (True, True)
        assert task_manager.calls == [('start', 100)]
    finally:
        await journal.close()


@pytest.mark.asyncio
async def test_terminal_intent_order_survives_operation_retention(
    tmp_path: Path,
) -> None:
    now = [1.0]
    path = tmp_path / 'control.sqlite3'
    settings_manager = make_settings_manager(
        Settings(
            tasks=[TaskSettings(room_id=100, enable_monitor=True, enable_recorder=True)]
        )
    )
    journal = ControlOperationJournal(path, clock=lambda: now[0])
    await journal.open()
    membership = await admit_membership_operation(journal)
    task_manager = FakeTaskManager()
    task_manager.release.set()
    reconciler = TaskControlReconciler(journal, settings_manager, task_manager)
    stop = await reconciler.submit('stop', [100], rejected={}, force=False)
    reconciler.start()
    await reconciler.wait_idle()
    await reconciler.shutdown()
    await journal.close()

    now[0] += 31 * 24 * 60 * 60
    reopened = ControlOperationJournal(path, clock=lambda: now[0])
    await reopened.open()
    recovered = TaskControlReconciler(reopened, settings_manager, task_manager)
    try:
        assert await reopened.get(stop.id) is None

        final = await recovered.reconcile_membership_start(
            100, membership_operation_id=membership.id, reuse_info_revision=None
        )

        assert final == (False, False)
        assert settings_manager.get_task_desired_state(100) == (False, False)
    finally:
        await recovered.shutdown()
        await reopened.close()


@pytest.mark.asyncio
async def test_task_control_worker_failure_wakes_shutdown_and_propagates(
    tmp_path: Path,
) -> None:
    journal = ControlOperationJournal(tmp_path / 'control.sqlite3')
    await journal.open()
    task_manager = FakeTaskManager()
    task_manager.release.set()
    reconciler = TaskControlReconciler(
        journal,
        make_settings_manager(
            Settings(
                tasks=[
                    TaskSettings(
                        room_id=100, enable_monitor=False, enable_recorder=False
                    )
                ]
            )
        ),
        task_manager,
    )
    await reconciler.submit('start', [100], rejected={}, force=False)
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
async def test_second_task_claim_failure_drains_the_first_claim(tmp_path: Path) -> None:
    settings_manager = make_settings_manager(
        Settings(
            tasks=[
                TaskSettings(room_id=100, enable_monitor=False, enable_recorder=False),
                TaskSettings(room_id=200, enable_monitor=False, enable_recorder=False),
            ]
        )
    )
    journal = ControlOperationJournal(tmp_path / 'control.sqlite3')
    await journal.open()
    task_manager = ConcurrentTaskManager((100, 200))
    task_manager.release.set()
    reconciler = TaskControlReconciler(journal, settings_manager, task_manager)
    operation = await reconciler.submit('start', [100, 200], rejected={}, force=False)
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
    try:
        with pytest.raises(ControlJournalError, match='second claim failed'):
            await asyncio.wait_for(reconciler.shutdown(), timeout=0.2)

        assert reconciler._active_steps == 0
        assert task_manager.calls == [('start', 100)]
        final = await journal.get(operation.id)
        assert final is not None
        assert [step.status for step in final.steps] == ['succeeded', 'queued']
    finally:
        worker = reconciler._worker
        if worker is not None:
            await asyncio.gather(worker, return_exceptions=True)
        await journal.close()


@pytest.mark.asyncio
async def test_start_stop_start_keeps_one_pending_room_and_converges_last_state(
    tmp_path: Path,
) -> None:
    settings_manager = make_settings_manager(
        Settings(tasks=[TaskSettings(room_id=100)])
    )
    journal = ControlOperationJournal(tmp_path / 'control.sqlite3')
    await journal.open()
    task_manager = FakeTaskManager()
    task_manager.release.set()
    reconciler = TaskControlReconciler(journal, settings_manager, task_manager)
    try:
        await reconciler.submit('start', [100], rejected={}, force=False)
        await reconciler.submit('stop', [100], rejected={}, force=False)
        last = await reconciler.submit('start', [100], rejected={}, force=False)
        assert await journal.queued_count('task-state') == 1
        reconciler.start()
        await reconciler.wait_idle()

        assert task_manager.get_task_control_state(100) == (True, True)
        final = await journal.get(last.id)
        assert final is not None
        assert final.status == 'succeeded'
    finally:
        task_manager.release.set()
        await reconciler.shutdown()
        await journal.close()


@pytest.mark.asyncio
async def test_recovery_scans_persisted_desired_state_without_a_wake(
    tmp_path: Path,
) -> None:
    settings_manager = make_settings_manager(
        Settings(
            tasks=[TaskSettings(room_id=100, enable_monitor=True, enable_recorder=True)]
        )
    )
    journal = ControlOperationJournal(tmp_path / 'control.sqlite3')
    await journal.open()
    task_manager = FakeTaskManager()
    task_manager.release.set()
    reconciler = TaskControlReconciler(journal, settings_manager, task_manager)
    try:
        operation = await reconciler.recover()
        assert operation is not None
        reconciler.start()
        await reconciler.wait_idle()
        assert task_manager.get_task_control_state(100) == (True, True)
    finally:
        await reconciler.shutdown()
        await journal.close()


@pytest.mark.asyncio
async def test_pending_membership_removal_suppresses_task_state_recovery(
    tmp_path: Path,
) -> None:
    settings_manager = make_settings_manager(
        Settings(
            tasks=[TaskSettings(room_id=100, enable_monitor=True, enable_recorder=True)]
        )
    )
    journal = ControlOperationJournal(tmp_path / 'control.sqlite3')
    await journal.open()
    task_manager = FakeTaskManager()
    reconciler = TaskControlReconciler(journal, settings_manager, task_manager)
    reconciler.set_desired_absent_provider(lambda room_id: room_id == 100)
    try:
        operation = await reconciler.recover()

        assert operation is None
        assert await journal.queued_count('task-state') == 0
    finally:
        await reconciler.shutdown()
        await journal.close()


@pytest.mark.asyncio
async def test_restart_consumes_operation_persisted_before_wake(tmp_path: Path) -> None:
    settings_manager = make_settings_manager(
        Settings(
            tasks=[
                TaskSettings(room_id=100, enable_monitor=False, enable_recorder=False)
            ]
        )
    )
    path = tmp_path / 'control.sqlite3'
    first_journal = ControlOperationJournal(path)
    await first_journal.open()
    task_manager = FakeTaskManager()
    first = TaskControlReconciler(first_journal, settings_manager, task_manager)
    operation = await first.submit('start', [100], rejected={}, force=False)
    await first_journal.close()

    recovered_journal = ControlOperationJournal(path)
    await recovered_journal.open()
    task_manager.release.set()
    recovered = TaskControlReconciler(recovered_journal, settings_manager, task_manager)
    recovered.start()
    try:
        await recovered.wait_idle()
        final = await recovered_journal.get(operation.id)
        assert final is not None
        assert final.status == 'succeeded'
        assert task_manager.get_task_control_state(100) == (True, True)
    finally:
        await recovered.shutdown()
        await recovered_journal.close()


@pytest.mark.asyncio
async def test_restart_only_resumes_the_pending_room_step(tmp_path: Path) -> None:
    path = tmp_path / 'control.sqlite3'
    first_journal = ControlOperationJournal(path)
    await first_journal.open()
    operation = await first_journal.admit(
        lane='task-state',
        kind='start',
        target_key='100,200',
        steps=[ControlStepInput(key='100'), ControlStepInput(key='200')],
    )
    first = await first_journal.claim_next('task-state')
    assert first is not None and first.key == '100'
    await first_journal.finish_step(
        first,
        status='succeeded',
        result={'roomId': 100, 'monitorEnabled': True, 'recorderEnabled': True},
    )
    second = await first_journal.claim_next('task-state')
    assert second is not None and second.key == '200'
    await first_journal.close()

    settings_manager = make_settings_manager(
        Settings(tasks=[TaskSettings(room_id=100), TaskSettings(room_id=200)])
    )
    reopened = ControlOperationJournal(path)
    await reopened.open()
    task_manager = FakeTaskManager()
    task_manager.states[100].monitor_enabled = True
    task_manager.states[100].recorder_enabled = True
    task_manager.states[200] = SimpleNamespace(
        monitor_enabled=False, recorder_enabled=False
    )
    task_manager.release.set()
    reconciler = TaskControlReconciler(reopened, settings_manager, task_manager)
    reconciler.start()
    try:
        await reconciler.wait_idle()

        final = await reopened.get(operation.id)
        assert final is not None and final.status == 'succeeded'
        assert task_manager.calls == [('start', 200)]
    finally:
        await reconciler.shutdown()
        await reopened.close()


@pytest.mark.asyncio
async def test_fifty_eight_rooms_persist_desired_state_once(tmp_path: Path) -> None:
    room_ids = tuple(range(1, 59))
    settings_manager = make_settings_manager(
        Settings(
            tasks=[
                TaskSettings(
                    room_id=room_id, enable_monitor=False, enable_recorder=False
                )
                for room_id in room_ids
            ]
        )
    )
    original_change = settings_manager.change_task_desired_states
    settings_manager.change_task_desired_states = AsyncMock(wraps=original_change)
    journal = ControlOperationJournal(tmp_path / 'control.sqlite3')
    await journal.open()
    reconciler = TaskControlReconciler(journal, settings_manager, FakeTaskManager())
    try:
        operation = await reconciler.submit('start', room_ids, rejected={}, force=False)

        assert [int(step.key) for step in operation.steps] == list(room_ids)
        settings_manager.change_task_desired_states.assert_awaited_once_with(
            room_ids, enable_monitor=True, enable_recorder=True
        )
        settings_manager.dump_settings.assert_awaited_once()
    finally:
        reconciler.close_admission()
        await journal.close()


@pytest.mark.asyncio
async def test_noop_desired_and_actual_state_skips_lifecycle_calls(
    tmp_path: Path,
) -> None:
    settings_manager = make_settings_manager(
        Settings(tasks=[TaskSettings(room_id=100)])
    )
    journal = ControlOperationJournal(tmp_path / 'control.sqlite3')
    await journal.open()
    task_manager = FakeTaskManager()
    task_manager.states[100].monitor_enabled = True
    task_manager.states[100].recorder_enabled = True
    reconciler = TaskControlReconciler(journal, settings_manager, task_manager)
    reconciler.start()
    try:
        operation = await reconciler.submit('start', [100], rejected={}, force=False)
        await reconciler.wait_idle()
        final = await journal.get(operation.id)
        assert final is not None
        assert final.status == 'succeeded'
        assert task_manager.calls == []
    finally:
        await reconciler.shutdown()
        await journal.close()


@pytest.mark.asyncio
async def test_retry_after_partial_lifecycle_failure_executes_remaining_work(
    tmp_path: Path,
) -> None:
    settings_manager = make_settings_manager(
        Settings(tasks=[TaskSettings(room_id=100)])
    )
    journal = ControlOperationJournal(tmp_path / 'control.sqlite3')
    await journal.open()
    task_manager = FakeTaskManager()
    task_manager.release.set()
    task_manager.start_failures = 1
    reconciler = TaskControlReconciler(journal, settings_manager, task_manager)
    reconciler.start()
    try:
        first = await reconciler.submit('start', [100], rejected={}, force=False)
        await reconciler.wait_idle()
        failed = await journal.get(first.id)
        assert failed is not None
        assert failed.status == 'failed'
        assert task_manager.get_task_control_state(100) == (True, False)

        retry = await reconciler.submit('start', [100], rejected={}, force=False)
        await reconciler.wait_idle()
        succeeded = await journal.get(retry.id)
        assert succeeded is not None
        assert succeeded.status == 'succeeded'
        assert task_manager.get_task_control_state(100) == (True, True)
        assert task_manager.calls == [('start', 100), ('start', 100)]
    finally:
        await reconciler.shutdown()
        await journal.close()


@pytest.mark.asyncio
async def test_settings_failure_terminates_admitted_operation_before_worker_claim(
    tmp_path: Path,
) -> None:
    settings_manager = make_settings_manager(
        Settings(
            tasks=[
                TaskSettings(room_id=100, enable_monitor=False, enable_recorder=False)
            ]
        )
    )
    settings_manager.dump_settings = AsyncMock(  # type: ignore[method-assign]
        side_effect=OSError('disk full')
    )
    journal = ControlOperationJournal(tmp_path / 'control.sqlite3')
    await journal.open()
    admitted: List[Any] = []
    real_admit = journal.admit

    async def capture_admit(**kwargs: Any) -> Any:
        operation = await real_admit(**kwargs)
        admitted.append(operation)
        return operation

    journal.admit = capture_admit  # type: ignore[method-assign]
    task_manager = FakeTaskManager()
    task_manager.release.set()
    reconciler = TaskControlReconciler(journal, settings_manager, task_manager)
    reconciler.start()
    try:
        with pytest.raises(OSError, match='disk full'):
            await reconciler.submit('start', [100], rejected={}, force=False)

        assert len(admitted) == 1
        failed = await journal.get(admitted[0].id)
        assert failed is not None
        assert failed.status == 'failed'
        assert failed.error_code == 'SETTINGS_PERSIST_FAILED'
        assert {step.status for step in failed.steps} == {'failed'}
        assert await journal.queued_count('task-state') == 0
        assert task_manager.calls == []
    finally:
        await reconciler.shutdown()
        await journal.close()


@pytest.mark.asyncio
async def test_admission_keeps_rejected_rooms_in_the_requested_order(
    tmp_path: Path,
) -> None:
    settings_manager = make_settings_manager(
        Settings(
            tasks=[
                TaskSettings(room_id=100, enable_monitor=False, enable_recorder=False)
            ]
        )
    )
    journal = ControlOperationJournal(tmp_path / 'control.sqlite3')
    await journal.open()
    task_manager = FakeTaskManager()
    reconciler = TaskControlReconciler(journal, settings_manager, task_manager)
    try:
        operation = await reconciler.submit(
            'start',
            [404, 100, 500],
            rejected={404: 'TASK_NOT_FOUND', 500: 'TASK_NOT_FOUND'},
            force=False,
        )

        assert [step.key for step in operation.steps] == ['404', '100', '500']
        assert [step.status for step in operation.steps] == [
            'rejected',
            'queued',
            'rejected',
        ]
        assert settings_manager.get_task_desired_state(100) == (True, True)
        settings_manager.dump_settings.assert_awaited_once()
    finally:
        task_manager.release.set()
        await reconciler.shutdown()
        await journal.close()


@pytest.mark.asyncio
async def test_shutdown_closes_admission_before_waiting_submitter_can_admit(
    tmp_path: Path,
) -> None:
    settings_manager = make_settings_manager(
        Settings(tasks=[TaskSettings(room_id=100)])
    )
    journal = ControlOperationJournal(tmp_path / 'control.sqlite3')
    await journal.open()
    task_manager = FakeTaskManager()
    task_manager.release.set()
    reconciler = TaskControlReconciler(journal, settings_manager, task_manager)

    await reconciler._submission_lock.acquire()
    submit = asyncio.create_task(
        reconciler.submit('start', [100], rejected={}, force=False)
    )
    shutdown = asyncio.create_task(reconciler.shutdown())
    try:
        while reconciler._accepting:
            await asyncio.sleep(0)
        reconciler._submission_lock.release()

        with pytest.raises(RuntimeError, match='admission is closed'):
            await submit
        await shutdown
        assert await journal.queued_count('task-state') == 0
        assert task_manager.calls == []
    finally:
        if reconciler._submission_lock.locked():
            reconciler._submission_lock.release()
        if not shutdown.done():
            shutdown.cancel()
            await asyncio.gather(shutdown, return_exceptions=True)
        await journal.close()


@pytest.mark.asyncio
async def test_monitor_enable_commits_state_only_after_lifecycle_succeeds() -> None:
    task = make_record_task(monitor_enabled=False, recorder_enabled=False)
    task._danmaku_client.start.side_effect = [RuntimeError('start failed'), None]

    with pytest.raises(RuntimeError, match='start failed'):
        await task.enable_monitor()
    assert task.monitor_enabled is False

    await task.enable_monitor()
    assert task.monitor_enabled is True
    assert task._danmaku_client.start.await_count == 2


@pytest.mark.asyncio
async def test_monitor_disable_retries_partial_lifecycle_work() -> None:
    task = make_record_task(monitor_enabled=True, recorder_enabled=False)
    task._danmaku_client.stop.side_effect = [RuntimeError('stop failed'), None]

    with pytest.raises(RuntimeError, match='stop failed'):
        await task.disable_monitor()
    assert task.monitor_enabled is True

    await task.disable_monitor()
    assert task.monitor_enabled is False
    assert task._danmaku_client.stop.await_count == 2


@pytest.mark.asyncio
async def test_batch_monitor_disable_retries_after_unregister_succeeds() -> None:
    task = make_record_task(monitor_enabled=True, recorder_enabled=False)
    task._batch_monitoring = True
    task._live_status_coordinator = SimpleNamespace(unregister=Mock())
    task._monitor_registration_key = 100
    task._connection_controller = SimpleNamespace(
        close=AsyncMock(side_effect=[RuntimeError('close failed'), None])
    )

    with pytest.raises(RuntimeError, match='close failed'):
        await task.disable_monitor()
    assert task.monitor_enabled is True
    assert task._monitor_registration_key is None

    await task.disable_monitor()
    assert task.monitor_enabled is False
    task._live_status_coordinator.unregister.assert_called_once_with(100)
    assert task._connection_controller.close.await_count == 2


@pytest.mark.asyncio
async def test_recorder_enable_retries_after_partial_lifecycle_failure() -> None:
    task = make_record_task(monitor_enabled=True, recorder_enabled=False)
    task._recorder.start.side_effect = [RuntimeError('start failed'), None]

    with pytest.raises(RuntimeError, match='start failed'):
        await task.enable_recorder()
    assert task.recorder_enabled is False

    await task.enable_recorder()
    assert task.recorder_enabled is True
    assert task._postprocessor.start.await_count == 2
    assert task._recorder.start.await_count == 2


@pytest.mark.asyncio
async def test_recorder_disable_retries_after_partial_lifecycle_failure() -> None:
    task = make_record_task(monitor_enabled=True, recorder_enabled=True)
    task._postprocessor.stop.side_effect = [RuntimeError('stop failed'), None]

    with pytest.raises(RuntimeError, match='stop failed'):
        await task.disable_recorder()
    assert task.recorder_enabled is True

    await task.disable_recorder()
    assert task.recorder_enabled is False
    assert task._recorder.stop.await_count == 2
    assert task._postprocessor.stop.await_count == 2
