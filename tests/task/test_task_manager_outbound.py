from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import List, Optional
from unittest.mock import AsyncMock, Mock

import pytest
from tenacity import stop_after_attempt, wait_none

from blrec.bili.exceptions import ApiRequestError
from blrec.setting.models import TaskSettings
from blrec.task import task_manager as task_manager_module
from blrec.task.task_manager import RecordTaskManager


class FakeSettingsManager:
    def get_settings(self, _include: object) -> SimpleNamespace:
        return SimpleNamespace(bili_api=object())

    async def apply_task_header_settings(
        self, *_args: object, **_kwargs: object
    ) -> None:
        return None

    def apply_task_output_settings(self, *_args: object) -> None:
        return None

    def apply_task_danmaku_settings(self, *_args: object) -> None:
        return None

    def apply_task_recorder_settings(self, *_args: object) -> None:
        return None

    def apply_task_postprocessing_settings(self, *_args: object) -> None:
        return None


class StartableTask:
    ready = True
    monitor_enabled = False
    recorder_enabled = False

    def __init__(self, info_revision: int) -> None:
        self.info_revision = info_revision
        self.update_info = AsyncMock(return_value=True)
        self.enable_monitor = AsyncMock()
        self.enable_recorder = AsyncMock()


@pytest.mark.asyncio
@pytest.mark.parametrize('reuse_info_revision', (None, 6))
async def test_start_refreshes_unless_the_exact_revision_is_reused(
    reuse_info_revision: Optional[int],
) -> None:
    manager = RecordTaskManager(FakeSettingsManager())  # type: ignore[arg-type]
    task = StartableTask(info_revision=7)
    manager._tasks[100] = task  # type: ignore[assignment]

    await manager.start_task(100, reuse_info_revision=reuse_info_revision)

    task.update_info.assert_awaited_once_with(raise_exception=True)
    task.enable_monitor.assert_awaited_once_with()
    task.enable_recorder.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_start_skips_refresh_for_the_exact_revision() -> None:
    manager = RecordTaskManager(FakeSettingsManager())  # type: ignore[arg-type]
    task = StartableTask(info_revision=7)
    manager._tasks[100] = task  # type: ignore[assignment]

    await manager.start_task(100, reuse_info_revision=7)

    task.update_info.assert_not_awaited()
    task.enable_monitor.assert_awaited_once_with()
    task.enable_recorder.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_add_task_does_not_retry_the_whole_setup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup_calls: List[int] = []

    class FailingTask:
        ready = False

        def __init__(self, room_id: int, **_kwargs: object) -> None:
            self.room_id = room_id

        async def setup(self) -> None:
            setup_calls.append(self.room_id)
            raise ApiRequestError(-1, 'temporary failure')

        async def disable_recorder(self, force: bool = False) -> None:
            del force

        async def disable_monitor(self) -> None:
            return None

        async def destroy(self) -> None:
            return None

    monkeypatch.setattr(task_manager_module, 'RecordTask', FailingTask)
    monkeypatch.setattr(RecordTaskManager, 'apply_task_bili_api_settings', Mock())
    retrying = getattr(RecordTaskManager.add_task, 'retry', None)
    if retrying is not None:
        monkeypatch.setattr(retrying, 'wait', wait_none())
        monkeypatch.setattr(retrying, 'stop', stop_after_attempt(3))
    manager = RecordTaskManager(FakeSettingsManager())  # type: ignore[arg-type]

    with pytest.raises(ApiRequestError):
        await manager.add_task(TaskSettings(room_id=100))

    assert setup_calls == [100]
    assert not manager.has_task(100)


class RefreshTask:
    ready = True

    def __init__(self, room_id: int, gate: asyncio.Event) -> None:
        self.room_id = room_id
        self.gate = gate
        self.entered: Optional[asyncio.Event] = None

    async def update_info(self, raise_exception: bool = False) -> bool:
        del raise_exception
        assert self.entered is not None
        self.entered.set()
        await self.gate.wait()
        return True


@pytest.mark.asyncio
async def test_update_all_task_infos_uses_two_room_slots() -> None:
    manager = RecordTaskManager(FakeSettingsManager())  # type: ignore[arg-type]
    gate = asyncio.Event()
    entered = [asyncio.Event() for _ in range(3)]
    for index, room_id in enumerate((30, 10, 20)):
        task = RefreshTask(room_id, gate)
        task.entered = entered[index]
        manager._tasks[room_id] = task  # type: ignore[assignment]

    updating = asyncio.create_task(manager.update_all_task_infos())
    try:
        await asyncio.wait_for(
            asyncio.gather(entered[0].wait(), entered[1].wait()), timeout=0.2
        )
        assert not entered[2].is_set()
    finally:
        gate.set()
        await updating
