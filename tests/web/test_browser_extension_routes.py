from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Iterator, Optional
from unittest.mock import AsyncMock

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from blrec.bili_upload.policies import RoomUploadPolicyCommand, RoomUploadPolicyNotFound
from blrec.task.models import RunningStatus
from blrec.web import security
from blrec.web.auth_store import AdminAuthStore
from blrec.web.routers import browser_extension, control_operations


class Clock:
    def __init__(self, value: int = 1_000_000) -> None:
        self.value = value

    def __call__(self) -> float:
        return float(self.value)


class FakeApplication:
    def __init__(self) -> None:
        self.collected = False
        self.running_status = RunningStatus.STOPPED
        self.monitor_enabled = False
        self.recorder_enabled = False
        self.add_task = AsyncMock(side_effect=self._add_task)
        self.start_task = AsyncMock(side_effect=self._start_task)
        self.enable_task_recorder = AsyncMock(side_effect=self._enable_recorder)
        self.submit_room_collect = AsyncMock(side_effect=self._submit_room_collect)

    def has_task(self, _room_id: int) -> bool:
        return self.collected

    def get_task_data(self, _room_id: int):
        return SimpleNamespace(
            task_status=SimpleNamespace(
                running_status=self.running_status,
                monitor_enabled=self.monitor_enabled,
                recorder_enabled=self.recorder_enabled,
            )
        )

    async def _add_task(self, room_id: int) -> int:
        self.collected = True
        return room_id

    async def _start_task(self, _room_id: int) -> None:
        self.monitor_enabled = True
        self.recorder_enabled = True
        self.running_status = RunningStatus.WAITING

    async def _enable_recorder(self, _room_id: int) -> None:
        self.recorder_enabled = True

    async def _submit_room_collect(self, room_id: int, *, upload: bool):
        return SimpleNamespace(
            id='membership-operation-1',
            status='accepted',
            result={'requestedRoomId': room_id, 'upload': upload},
        )


class FakePolicyManager:
    def __init__(self) -> None:
        self.current = None
        self.upsert = AsyncMock(side_effect=self._upsert)

    async def get(self, _room_id: int):
        if self.current is None:
            raise RoomUploadPolicyNotFound('missing')
        return self.current

    async def _upsert(self, room_id: int, command: RoomUploadPolicyCommand):
        self.current = SimpleNamespace(
            room_id=room_id,
            resolved_account_id=1,
            blocked_reason=None,
            **command.__dict__,
        )
        return self.current


class FakeCatalog:
    async def list(self, _account_mode: str, _account_id: Optional[int]):
        return SimpleNamespace(
            categories=(SimpleNamespace(children=(SimpleNamespace(id=21),)),),
            creation_statements=(SimpleNamespace(id=-2),),
        )


@pytest.fixture
def extension_client(
    tmp_path: Path,
) -> Iterator[
    tuple[
        TestClient, FakeApplication, FakePolicyManager, AsyncMock, AdminAuthStore, Clock
    ]
]:
    clock = Clock()
    store = AdminAuthStore(
        str(tmp_path / 'auth.sqlite3'), admin_username='owner', clock=clock
    )
    store.open()
    store.initialize('owner', 'correct horse battery staple')
    security.configure(store)
    application = FakeApplication()
    policies = FakePolicyManager()
    highlights = AsyncMock()
    highlights.create_marker.return_value = SimpleNamespace(id=9, name='精彩操作')
    browser_extension.application = application  # type: ignore[assignment]
    browser_extension.highlight_service = highlights
    browser_extension.policy_manager = policies  # type: ignore[assignment]
    browser_extension.category_catalog = FakeCatalog()  # type: ignore[assignment]
    old_control_journal = control_operations.journal
    control_journal = AsyncMock()
    control_journal.get.return_value = SimpleNamespace(
        id='membership-operation-1',
        lane='room-membership',
        kind='collect',
        target_key='100:0',
        attempt=1,
        generation=1,
        status='succeeded',
        result={
            'requestedRoomId': 100,
            'resolvedRoomId': 100,
            'collected': True,
            'upload': False,
        },
        error_code=None,
        created_at=1.0,
        updated_at=2.0,
        steps=(),
    )
    control_operations.journal = control_journal

    api = FastAPI(dependencies=[Depends(security.authenticate)])
    api.include_router(browser_extension.router, prefix='/api/v1')
    api.include_router(control_operations.router, prefix='/api/v1')

    @api.get('/api/v1/settings')
    async def settings() -> dict:
        return {'private': True}

    with TestClient(api) as client:
        yield client, application, policies, highlights, store, clock

    browser_extension.reset()
    control_operations.journal = old_control_journal
    security.reset()
    store.close()


def pair(client: TestClient) -> str:
    response = client.post('/api/v1/browser-extension/pair', json={'username': 'owner'})
    assert response.status_code == 201
    assert 'password' not in response.request.content.decode('utf8')
    return str(response.json()['token'])


def extension_headers(token: str) -> dict:
    return {'x-blrec-extension-token': token}


def test_pair_and_room_status_map_to_the_three_button_states(extension_client) -> None:
    client, application, _policies, _highlights, _store, _clock = extension_client
    token = pair(client)

    missing = client.get(
        '/api/v1/browser-extension/rooms/100', headers=extension_headers(token)
    )
    assert missing.json() == {'collected': False, 'recording': False}

    application.collected = True
    application.running_status = RunningStatus.WAITING
    waiting = client.get(
        '/api/v1/browser-extension/rooms/100', headers=extension_headers(token)
    )
    assert waiting.json() == {'collected': True, 'recording': False}

    application.running_status = RunningStatus.RECORDING
    recording = client.get(
        '/api/v1/browser-extension/rooms/100', headers=extension_headers(token)
    )
    assert recording.json() == {'collected': True, 'recording': True}


def test_collect_returns_durable_admission_without_waiting_for_side_effects(
    extension_client,
) -> None:
    client, application, policies, _highlights, _store, _clock = extension_client
    token = pair(client)

    collected = client.post(
        '/api/v1/browser-extension/rooms/100/collect',
        headers=extension_headers(token),
        json={'upload': False},
    )
    assert collected.status_code == 202
    assert collected.json() == {
        'operationId': 'membership-operation-1',
        'status': 'accepted',
        'requestedRoomId': 100,
    }
    application.submit_room_collect.assert_awaited_once_with(100, upload=False)
    application.add_task.assert_not_awaited()
    application.start_task.assert_not_awaited()
    policies.upsert.assert_not_awaited()

    uploaded = client.post(
        '/api/v1/browser-extension/rooms/100/collect',
        headers=extension_headers(token),
        json={'upload': True},
    )
    assert uploaded.status_code == 202
    application.submit_room_collect.assert_awaited_with(100, upload=True)
    policies.upsert.assert_not_awaited()


def test_collect_admission_does_not_apply_policy_inside_the_request(
    extension_client,
) -> None:
    client, application, policies, _highlights, _store, _clock = extension_client
    token = pair(client)
    application.collected = True
    from blrec.bili_upload.policies import default_room_upload_policy

    original = default_room_upload_policy()
    policies.current = SimpleNamespace(
        room_id=100,
        resolved_account_id=1,
        blocked_reason=None,
        **{**original.__dict__, 'enabled': False, 'title_template': '保留这个标题'},
    )

    response = client.post(
        '/api/v1/browser-extension/rooms/100/collect',
        headers=extension_headers(token),
        json={'upload': True},
    )

    assert response.status_code == 202
    application.submit_room_collect.assert_awaited_once_with(100, upload=True)
    policies.upsert.assert_not_awaited()


def test_extension_token_can_poll_the_shared_control_operation_route(
    extension_client,
) -> None:
    client, _application, _policies, _highlights, _store, _clock = extension_client
    token = pair(client)

    response = client.get(
        '/api/v1/control-operations/membership-operation-1',
        headers=extension_headers(token),
    )

    assert response.status_code == 200
    assert response.json()['result']['resolvedRoomId'] == 100


@pytest.mark.asyncio
async def test_upload_policy_step_observes_enabled_postcondition_before_remote_list() -> (
    None
):
    policies = FakePolicyManager()
    from blrec.bili_upload.policies import default_room_upload_policy

    command = default_room_upload_policy()
    policies.current = SimpleNamespace(
        room_id=100,
        resolved_account_id=1,
        blocked_reason=None,
        **{**command.__dict__, 'enabled': True},
    )
    catalog = AsyncMock()

    await browser_extension._enable_upload_policy(100, policies, catalog)

    catalog.list.assert_not_awaited()
    policies.upsert.assert_not_awaited()


def test_highlight_is_saved_independently_of_current_recording_state(
    extension_client,
) -> None:
    client, application, _policies, highlights, _store, _clock = extension_client
    token = pair(client)
    application.collected = True
    application.running_status = RunningStatus.STOPPED

    response = client.post(
        '/api/v1/browser-extension/rooms/100/highlights',
        headers=extension_headers(token),
        json={
            'observedAtMs': 1_000_000,
            'playerDelayMs': 18_500,
            'currentTimeMs': 100_500,
            'seekableEndMs': 119_000,
            'rawDelayMs': 18_500,
            'baselineDelayMs': 18_500,
            'effectiveRewindMs': 0,
            'name': '精彩操作',
            'title': '直播标题',
            'anchorName': '主播',
        },
    )

    assert response.status_code == 201
    highlights.create_marker.assert_awaited_once_with(
        room_id=100,
        observed_at_ms=1_000_000,
        player_delay_ms=18_500,
        current_time_ms=100_500,
        seekable_end_ms=119_000,
        raw_delay_ms=18_500,
        baseline_delay_ms=18_500,
        effective_rewind_ms=0,
        title='直播标题',
        anchor_name='主播',
        name='精彩操作',
        source='browser_extension',
    )


def test_extension_routes_reject_missing_malformed_and_revoked_tokens(
    extension_client,
) -> None:
    client, _application, _policies, _highlights, store, _clock = extension_client
    token = pair(client)

    assert client.get('/api/v1/browser-extension/rooms/100').status_code == 401
    assert (
        client.get(
            '/api/v1/browser-extension/rooms/100',
            headers=extension_headers('malformed'),
        ).status_code
        == 401
    )
    identity = store.authenticate_extension(token)
    assert identity is not None
    store.revoke_extension_token(identity.token_id)
    assert (
        client.get(
            '/api/v1/browser-extension/rooms/100', headers=extension_headers(token)
        ).status_code
        == 401
    )
    assert (
        client.get('/api/v1/settings', headers=extension_headers(token)).status_code
        == 401
    )


def test_extension_activity_is_persisted_at_most_once_per_interval(
    extension_client,
) -> None:
    client, _application, _policies, _highlights, store, clock = extension_client
    token = pair(client)
    connection = store._connection
    assert connection is not None
    before_changes = connection.total_changes
    before_audits = int(
        connection.execute(
            "SELECT count(*) FROM auth_audit WHERE event='extension_token_used'"
        ).fetchone()[0]
    )

    for _ in range(2):
        response = client.get(
            '/api/v1/browser-extension/rooms/100', headers=extension_headers(token)
        )
        assert response.status_code == 200
    assert connection.total_changes == before_changes
    assert (
        connection.execute(
            "SELECT count(*) FROM auth_audit WHERE event='extension_token_used'"
        ).fetchone()[0]
        == before_audits
    )

    clock.value += 60
    response = client.get(
        '/api/v1/browser-extension/rooms/100', headers=extension_headers(token)
    )
    assert response.status_code == 200
    assert connection.total_changes == before_changes + 2
    assert (
        connection.execute(
            "SELECT count(*) FROM auth_audit WHERE event='extension_token_used'"
        ).fetchone()[0]
        == before_audits + 1
    )
