from typing import Iterator, List, Tuple

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from blrec.web.routers import tasks


class FakeApplication:
    def __init__(self) -> None:
        self.calls: List[Tuple[str, int]] = []

    def has_task(self, room_id: int) -> bool:
        return room_id != 404

    async def start_task(self, room_id: int) -> None:
        self.calls.append(('start', room_id))

    async def stop_task(self, room_id: int, force: bool = False) -> None:
        self.calls.append(('force_stop' if force else 'stop', room_id))

    async def enable_task_recorder(self, room_id: int) -> None:
        self.calls.append(('recorder_enable', room_id))

    async def disable_task_recorder(self, room_id: int, force: bool = False) -> None:
        self.calls.append(
            ('recorder_force_disable' if force else 'recorder_disable', room_id)
        )

    async def update_task_info(self, room_id: int) -> None:
        self.calls.append(('refresh', room_id))

    def cut_stream(self, room_id: int) -> bool:
        self.calls.append(('cut', room_id))
        return room_id != 200

    async def remove_task(self, room_id: int) -> None:
        self.calls.append(('delete', room_id))


@pytest.fixture
def client() -> Iterator[TestClient]:
    old_app = tasks.app
    api = FastAPI()
    api.include_router(tasks.router)
    tasks.app = FakeApplication()  # type: ignore[assignment]
    try:
        with TestClient(api) as value:
            yield value
    finally:
        tasks.app = old_app


def test_batch_task_action_returns_per_room_results(client: TestClient) -> None:
    response = client.post(
        '/api/v1/tasks/actions', json={'action': 'cut', 'roomIds': [100, 200]}
    )

    assert response.status_code == 200
    assert response.json() == {
        'results': [
            {'roomId': 100, 'accepted': True, 'message': '已触发文件切割'},
            {'roomId': 200, 'accepted': False, 'message': '当前不能切割文件'},
        ]
    }
    app = tasks.app
    assert isinstance(app, FakeApplication)
    assert app.calls == [('cut', 100), ('cut', 200)]


def test_batch_task_action_rejects_duplicate_rooms(client: TestClient) -> None:
    response = client.post(
        '/api/v1/tasks/actions', json={'action': 'start', 'roomIds': [100, 100]}
    )

    assert response.status_code == 422


@pytest.mark.parametrize(
    ('action', 'expected_call'),
    (
        ('force_stop', ('force_stop', 100)),
        ('recorder_force_disable', ('recorder_force_disable', 100)),
    ),
)
def test_batch_task_action_supports_force_operations(
    client: TestClient, action: str, expected_call: Tuple[str, int]
) -> None:
    response = client.post(
        '/api/v1/tasks/actions', json={'action': action, 'roomIds': [100]}
    )

    assert response.status_code == 200
    app = tasks.app
    assert isinstance(app, FakeApplication)
    assert app.calls == [expected_call]
