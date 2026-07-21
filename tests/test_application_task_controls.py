from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Iterator, List, Tuple
from unittest.mock import AsyncMock

import pytest

from blrec.application import Application
from blrec.setting.models import Settings, TaskSettings
from blrec.setting.setting_manager import SettingsManager
from blrec.task.control_reconciler import TaskControlReconciler


class FakeSettingsApplication:
    pass


class FakeLoadedTasks:
    def has_task(self, room_id: int) -> bool:
        return room_id == 100


@pytest.mark.asyncio
async def test_batch_desired_state_uses_one_dump_and_noop_uses_zero() -> None:
    settings = Settings(
        tasks=[TaskSettings(room_id=room_id) for room_id in range(1, 59)]
    )
    manager = SettingsManager(
        FakeSettingsApplication(), settings  # type: ignore[arg-type]
    )
    manager.dump_settings = AsyncMock()  # type: ignore[method-assign]

    changed = await manager.change_task_desired_states(
        range(1, 59), enable_monitor=False, enable_recorder=False
    )

    assert changed == set(range(1, 59))
    manager.dump_settings.assert_awaited_once()
    manager.dump_settings.reset_mock()

    unchanged = await manager.change_task_desired_states(
        range(1, 59), enable_monitor=False, enable_recorder=False
    )

    assert unchanged == set()
    manager.dump_settings.assert_not_awaited()


@pytest.mark.asyncio
async def test_application_admits_valid_and_rejected_rooms_together() -> None:
    app = Application(Settings())
    app._task_manager = FakeLoadedTasks()  # type: ignore[assignment]
    reconciler = AsyncMock()
    reconciler.submit.return_value = object()
    app._task_control_reconciler = reconciler

    result = await app.submit_task_control('start', [404, 100])

    assert result is reconciler.submit.return_value
    reconciler.submit.assert_awaited_once_with(
        'start', [404, 100], rejected={404: 'TASK_NOT_FOUND'}, force=False
    )


@pytest.mark.asyncio
async def test_application_delegates_membership_without_running_side_effects() -> None:
    app = Application(Settings())
    reconciler = AsyncMock()
    reconciler.submit_add.return_value = object()
    reconciler.submit_remove.return_value = object()
    reconciler.submit_collect.return_value = object()
    app._room_membership_reconciler = reconciler

    added = await app.submit_room_add(6)
    removed = await app.submit_room_remove([100, 200], remove_all=True)
    collected = await app.submit_room_collect(6, upload=True)

    assert added is reconciler.submit_add.return_value
    assert removed is reconciler.submit_remove.return_value
    assert collected is reconciler.submit_collect.return_value
    reconciler.submit_add.assert_awaited_once_with(6)
    reconciler.submit_remove.assert_awaited_once_with([100, 200], remove_all=True)
    reconciler.submit_collect.assert_awaited_once_with(6, upload=True)


@pytest.mark.asyncio
async def test_refresh_and_durable_remote_steps_share_two_slots() -> None:
    class RemoteTaskManager:
        def __init__(self) -> None:
            self.release = asyncio.Event()
            self.first_entered = asyncio.Event()
            self.two_entered = asyncio.Event()
            self.three_entered = asyncio.Event()
            self.calls: List[Tuple[str, int]] = []
            self.in_flight = 0
            self.max_in_flight = 0

        def get_all_task_room_ids(self) -> Tuple[int, ...]:
            return (100, 200, 300)

        def get_ready_task_room_ids(self) -> Tuple[int, ...]:
            return (100, 200, 300)

        def get_all_task_data(self) -> Iterator[SimpleNamespace]:
            for room_id in (100, 200, 300):
                yield SimpleNamespace(room_info=SimpleNamespace(room_id=room_id))

        async def remote(self, kind: str, room_id: int) -> None:
            self.calls.append((kind, room_id))
            self.in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)
            self.first_entered.set()
            if self.in_flight >= 2:
                self.two_entered.set()
            if self.in_flight >= 3:
                self.three_entered.set()
            try:
                await self.release.wait()
            finally:
                self.in_flight -= 1

        async def update_task_info(self, room_id: int) -> None:
            await self.remote('refresh', room_id)

        async def update_all_task_infos(self) -> None:
            await asyncio.gather(
                *(self.update_task_info(room_id) for room_id in (100, 200, 300))
            )

    manager = RemoteTaskManager()
    reconciler = TaskControlReconciler(
        AsyncMock(), AsyncMock(), manager  # type: ignore[arg-type]
    )
    app = Application(Settings())
    app._task_manager = manager  # type: ignore[assignment]
    app._task_control_reconciler = reconciler
    durable = asyncio.create_task(
        reconciler.run_room_action(900, lambda: manager.remote('membership', 900))
    )
    await asyncio.wait_for(manager.first_entered.wait(), timeout=0.2)
    refreshing = asyncio.create_task(app.update_all_task_infos())
    try:
        await asyncio.wait_for(manager.two_entered.wait(), timeout=0.2)
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(manager.three_entered.wait(), timeout=0.05)
        assert manager.max_in_flight == 2
    finally:
        manager.release.set()
        await asyncio.gather(durable, refreshing, return_exceptions=True)

    assert manager.max_in_flight == 2


@pytest.mark.asyncio
async def test_update_all_uses_the_configured_ready_task_key() -> None:
    class ShortRoomTaskManager:
        def __init__(self) -> None:
            self.calls: List[int] = []

        def get_ready_task_room_ids(self) -> Tuple[int, ...]:
            return (6,)

        def get_all_task_data(self) -> Iterator[SimpleNamespace]:
            yield SimpleNamespace(room_info=SimpleNamespace(room_id=3582149))

        async def update_task_info(self, room_id: int) -> None:
            self.calls.append(room_id)

    manager = ShortRoomTaskManager()
    app = Application(Settings())
    app._task_manager = manager  # type: ignore[assignment]

    await app.update_all_task_infos()

    assert manager.calls == [6]


@pytest.mark.asyncio
async def test_failed_desired_state_dump_restores_in_memory_values() -> None:
    settings = Settings(
        tasks=[TaskSettings(room_id=100, enable_monitor=True, enable_recorder=True)]
    )
    manager = SettingsManager(
        FakeSettingsApplication(), settings  # type: ignore[arg-type]
    )
    manager.dump_settings = AsyncMock(  # type: ignore[method-assign]
        side_effect=OSError('disk unavailable')
    )

    with pytest.raises(OSError, match='disk unavailable'):
        await manager.change_task_desired_states(
            [100], enable_monitor=False, enable_recorder=False
        )

    assert manager.get_task_desired_state(100) == (True, True)
