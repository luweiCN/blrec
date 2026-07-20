from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from blrec.application import Application
from blrec.setting.models import Settings, TaskSettings
from blrec.setting.setting_manager import SettingsManager


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

    result = await app.submit_task_control('start', [100, 404])

    assert result is reconciler.submit.return_value
    reconciler.submit.assert_awaited_once_with(
        'start', [100], rejected={404: 'TASK_NOT_FOUND'}, force=False
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
