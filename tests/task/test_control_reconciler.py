from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import List, Tuple
from unittest.mock import AsyncMock

import pytest

from blrec.control.operations import ControlOperationJournal
from blrec.setting.models import Settings, TaskSettings
from blrec.setting.setting_manager import SettingsManager
from blrec.task.control_reconciler import TaskControlReconciler


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
        Settings(tasks=[TaskSettings(room_id=100)])
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
