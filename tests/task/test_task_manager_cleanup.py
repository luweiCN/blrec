import importlib
from types import SimpleNamespace
from typing import Dict, Sequence, Tuple
from unittest.mock import Mock

import pytest

from blrec.bili.live_status import StatusSnapshot
from blrec.bili.live_status_coordinator import LiveStatusCoordinator


class FakeLive:
    def __init__(self, room_id: int, user_agent: str = '', cookie: str = '') -> None:
        self.room_info = SimpleNamespace(room_id=room_id)
        self.user_info = SimpleNamespace(uid=room_id + 1)
        self.deinitialized = False

    async def deinit(self) -> None:
        self.deinitialized = True


class FakeAnonymousRoomClient:
    async def confirm_status(self, room_id: int) -> StatusSnapshot:
        raise AssertionError('status confirmation is not expected during startup')

    async def fetch_uid_mappings(
        self, room_ids: Sequence[int]
    ) -> Dict[int, Tuple[int, int]]:
        raise AssertionError('room mapping is not expected during startup')


class FakeConnectionController:
    def __init__(self) -> None:
        self.closed = False

    async def on_confirmed_status(self, snapshot: StatusSnapshot) -> None:
        raise AssertionError('status notification is not expected during startup')

    async def close(self) -> None:
        self.closed = True


class FakePostprocessor:
    def __init__(self) -> None:
        self.started = False
        self.stopped = False

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.stopped = True


class FailingRecorder:
    def __init__(
        self, startup_error: RuntimeError, coordinator: LiveStatusCoordinator
    ) -> None:
        self.startup_error = startup_error
        self.coordinator = coordinator
        self.registered_rooms_at_start = 0
        self.stop_attempted = False

    async def start(self) -> None:
        self.registered_rooms_at_start = self.coordinator.metrics(0).registered_rooms
        raise self.startup_error

    async def stop(self) -> None:
        self.stop_attempted = True
        raise RuntimeError('recorder cleanup failed')


class FakeSettingsManager:
    def get_settings(self, include: object) -> SimpleNamespace:
        return SimpleNamespace(bili_api=object())

    async def apply_task_header_settings(self, *args: object, **kwargs: object) -> None:
        pass

    def apply_task_output_settings(self, *args: object) -> None:
        pass

    def apply_task_danmaku_settings(self, *args: object) -> None:
        pass

    def apply_task_recorder_settings(self, *args: object) -> None:
        pass

    def apply_task_postprocessing_settings(self, *args: object) -> None:
        pass


@pytest.mark.asyncio
async def test_add_task_cleans_batch_monitor_when_recorder_start_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    importlib.import_module('blrec.setting')
    task_module = importlib.import_module('blrec.task.task')
    task_manager_module = importlib.import_module('blrec.task.task_manager')
    monkeypatch.setattr(task_module, 'Live', FakeLive)
    anonymous = FakeAnonymousRoomClient()
    coordinator = LiveStatusCoordinator(Mock())
    task = task_module.RecordTask(
        1001, live_status_coordinator=coordinator, anonymous_room_client=anonymous
    )
    controller = FakeConnectionController()
    postprocessor = FakePostprocessor()
    startup_error = RuntimeError('recorder startup failed')
    recorder = FailingRecorder(startup_error, coordinator)

    async def setup() -> None:
        task._connection_controller = controller
        task._postprocessor = postprocessor
        task._recorder = recorder
        task._ready = True

    task.setup = setup  # type: ignore[method-assign]
    monkeypatch.setattr(task_manager_module, 'RecordTask', Mock(return_value=task))
    manager = task_manager_module.RecordTaskManager(
        FakeSettingsManager(),  # type: ignore[arg-type]
        coordinator,
        anonymous,  # type: ignore[arg-type]
    )
    manager.apply_task_bili_api_settings = Mock()  # type: ignore[method-assign]
    settings = SimpleNamespace(
        room_id=1001,
        header=object(),
        output=object(),
        danmaku=object(),
        recorder=object(),
        postprocessing=object(),
        enable_monitor=True,
        enable_recorder=True,
    )

    with pytest.raises(RuntimeError) as exc_info:
        await manager.add_task(settings)  # type: ignore[arg-type]

    assert exc_info.value is startup_error
    assert postprocessor.started
    assert postprocessor.stopped
    assert recorder.registered_rooms_at_start == 1
    assert recorder.stop_attempted
    assert coordinator.metrics(0).registered_rooms == 0
    assert controller.closed
    assert task._live.deinitialized
    assert not manager.has_task(1001)
