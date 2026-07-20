from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any, List, Tuple
from unittest.mock import AsyncMock, Mock

import pytest

from blrec.control.operations import ControlOperationJournal
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
        await reconciler.shutdown()
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
