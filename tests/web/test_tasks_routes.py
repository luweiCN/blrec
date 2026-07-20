from typing import Any, Dict, Iterator, List, Optional, Tuple

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from blrec.web.routers import tasks


class FakeStep:
    def __init__(
        self, key: str, status: str = 'queued', error_code: str = None
    ) -> None:
        self.key = key
        self.status = status
        self.error_code = error_code


class FakeOperation:
    def __init__(self, room_ids: List[int]) -> None:
        self.id = 'operation-1'
        self.status = 'accepted'
        self.steps = [
            FakeStep(
                str(room_id),
                status='rejected' if room_id == 404 else 'queued',
                error_code='TASK_NOT_FOUND' if room_id == 404 else None,
            )
            for room_id in room_ids
        ]


class FakeMembershipOperation:
    id = 'membership-operation-1'
    status = 'accepted'

    def __init__(self, requested_room_id: Optional[int] = None) -> None:
        self.result = {'requestedRoomId': requested_room_id}


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

    async def submit_room_add(self, room_id: int) -> FakeMembershipOperation:
        self.calls.append(('add', room_id))
        return FakeMembershipOperation(room_id)

    async def submit_room_remove(
        self, room_ids: List[int], *, remove_all: bool = False
    ) -> FakeMembershipOperation:
        self.calls.append(('remove_all' if remove_all else 'remove', len(room_ids)))
        return FakeMembershipOperation(None if remove_all else room_ids[0])

    async def submit_task_control(
        self, action: str, room_ids: List[int], force: bool = False
    ) -> FakeOperation:
        self.calls.append((action, len(room_ids)))
        return FakeOperation(room_ids)

    def get_all_task_room_ids(self) -> Iterator[int]:
        yield 100
        yield 200


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


def test_batch_task_action_returns_per_room_results(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    audit_events: List[Tuple[str, Dict[str, Any]]] = []
    monkeypatch.setattr(
        'blrec.web.routers.tasks.audit',
        lambda event, **fields: audit_events.append((event, fields)),
    )
    response = client.post(
        '/api/v1/tasks/actions', json={'action': 'cut', 'roomIds': [100, 200]}
    )

    assert response.status_code == 200
    assert response.json() == {
        'operationId': None,
        'status': None,
        'results': [
            {
                'roomId': 100,
                'accepted': True,
                'status': 'succeeded',
                'operationId': None,
                'errorCode': None,
                'message': '已触发文件切割',
            },
            {
                'roomId': 200,
                'accepted': False,
                'status': 'rejected',
                'operationId': None,
                'errorCode': None,
                'message': '当前不能切割文件',
            },
        ],
    }
    app = tasks.app
    assert isinstance(app, FakeApplication)
    assert app.calls == [('cut', 100), ('cut', 200)]
    assert audit_events == [
        (
            'recording_task_action',
            {
                'level': 'WARNING',
                'action': 'cut',
                'room_ids': [100, 200],
                'accepted': 1,
                'rejected': 1,
            },
        )
    ]


def test_batch_task_action_rejects_duplicate_rooms(client: TestClient) -> None:
    response = client.post(
        '/api/v1/tasks/actions', json={'action': 'start', 'roomIds': [100, 100]}
    )

    assert response.status_code == 422


@pytest.mark.parametrize('action', ('force_stop', 'recorder_force_disable'))
def test_batch_task_action_supports_force_operations(
    client: TestClient, action: str
) -> None:
    response = client.post(
        '/api/v1/tasks/actions', json={'action': action, 'roomIds': [100]}
    )

    assert response.status_code == 202
    app = tasks.app
    assert isinstance(app, FakeApplication)
    assert app.calls == [(action, 1)]


def test_batch_lifecycle_action_returns_202_without_running_lifecycle(
    client: TestClient,
) -> None:
    response = client.post(
        '/api/v1/tasks/actions', json={'action': 'start', 'roomIds': [100, 404]}
    )

    assert response.status_code == 202
    assert response.json() == {
        'operationId': 'operation-1',
        'status': 'accepted',
        'results': [
            {
                'roomId': 100,
                'accepted': True,
                'status': 'queued',
                'operationId': 'operation-1',
                'errorCode': None,
                'message': '操作已提交',
            },
            {
                'roomId': 404,
                'accepted': False,
                'status': 'rejected',
                'operationId': 'operation-1',
                'errorCode': 'TASK_NOT_FOUND',
                'message': '录制任务不存在',
            },
        ],
    }
    app = tasks.app
    assert isinstance(app, FakeApplication)
    assert app.calls == [('start', 2)]


@pytest.mark.parametrize(
    ('path', 'expected_action'),
    (
        ('/api/v1/tasks/start', 'start'),
        ('/api/v1/tasks/stop', 'stop'),
        ('/api/v1/tasks/recorder/enable', 'recorder_enable'),
        ('/api/v1/tasks/recorder/disable', 'recorder_disable'),
    ),
)
def test_all_lifecycle_routes_return_operation_admission(
    client: TestClient, path: str, expected_action: str
) -> None:
    response = client.post(path, json={})

    assert response.status_code == 202
    assert response.json()['operationId'] == 'operation-1'
    assert [item['roomId'] for item in response.json()['results']] == [100, 200]
    app = tasks.app
    assert isinstance(app, FakeApplication)
    assert app.calls == [(expected_action, 2)]


@pytest.mark.parametrize(
    ('path', 'body', 'expected_action'),
    (
        ('/api/v1/tasks/100/start', None, 'start'),
        ('/api/v1/tasks/100/stop', {'force': False}, 'stop'),
        ('/api/v1/tasks/100/recorder/enable', None, 'recorder_enable'),
        ('/api/v1/tasks/100/recorder/disable', {'force': False}, 'recorder_disable'),
    ),
)
def test_single_lifecycle_routes_return_operation_admission(
    client: TestClient, path: str, body: object, expected_action: str
) -> None:
    response = client.post(path, json=body)

    assert response.status_code == 202
    assert response.json()['operationId'] == 'operation-1'
    assert response.json()['results'][0]['roomId'] == 100
    app = tasks.app
    assert isinstance(app, FakeApplication)
    assert app.calls == [(expected_action, 1)]


def test_add_and_remove_routes_return_membership_admissions(client: TestClient) -> None:
    added = client.post('/api/v1/tasks/6')
    removed = client.delete('/api/v1/tasks/100')
    removed_all = client.delete('/api/v1/tasks')

    assert added.status_code == 202
    assert added.json() == {
        'operationId': 'membership-operation-1',
        'status': 'accepted',
        'requestedRoomId': 6,
    }
    assert removed.status_code == 202
    assert removed.json()['requestedRoomId'] == 100
    assert removed_all.status_code == 202
    assert removed_all.json()['requestedRoomId'] is None
    app = tasks.app
    assert isinstance(app, FakeApplication)
    assert app.calls == [('add', 6), ('remove', 1), ('remove_all', 0)]


def test_batch_delete_returns_one_durable_operation(client: TestClient) -> None:
    response = client.post(
        '/api/v1/tasks/actions', json={'action': 'delete', 'roomIds': [100, 200]}
    )

    assert response.status_code == 202
    assert response.json()['operationId'] == 'membership-operation-1'
    assert [item['status'] for item in response.json()['results']] == [
        'queued',
        'queued',
    ]
    app = tasks.app
    assert isinstance(app, FakeApplication)
    assert app.calls == [('remove', 2)]
