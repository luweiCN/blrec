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
from blrec.web.routers import browser_extension


class FakeApplication:
    def __init__(self) -> None:
        self.collected = False
        self.running_status = RunningStatus.STOPPED
        self.monitor_enabled = False
        self.recorder_enabled = False
        self.add_task = AsyncMock(side_effect=self._add_task)
        self.start_task = AsyncMock(side_effect=self._start_task)
        self.enable_task_recorder = AsyncMock(side_effect=self._enable_recorder)

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
    tuple[TestClient, FakeApplication, FakePolicyManager, AsyncMock, AdminAuthStore]
]:
    store = AdminAuthStore(str(tmp_path / 'auth.sqlite3'), admin_username='owner')
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

    api = FastAPI(dependencies=[Depends(security.authenticate)])
    api.include_router(browser_extension.router, prefix='/api/v1')

    @api.get('/api/v1/settings')
    async def settings() -> dict:
        return {'private': True}

    with TestClient(api) as client:
        yield client, application, policies, highlights, store

    browser_extension.reset()
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
    client, application, _policies, _highlights, _store = extension_client
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


def test_collect_is_idempotent_and_only_creates_policy_when_requested(
    extension_client,
) -> None:
    client, application, policies, _highlights, _store = extension_client
    token = pair(client)

    collected = client.post(
        '/api/v1/browser-extension/rooms/100/collect',
        headers=extension_headers(token),
        json={'upload': False},
    )
    assert collected.status_code == 200
    assert collected.json() == {'roomId': 100, 'collected': True, 'upload': False}
    application.add_task.assert_awaited_once_with(100)
    application.start_task.assert_awaited_once_with(100)
    policies.upsert.assert_not_awaited()

    uploaded = client.post(
        '/api/v1/browser-extension/rooms/100/collect',
        headers=extension_headers(token),
        json={'upload': True},
    )
    assert uploaded.status_code == 200
    application.add_task.assert_awaited_once()
    command = policies.upsert.await_args.args[1]
    assert command.enabled is True
    assert command.tid == 21
    assert command.creation_statement_id == -2


def test_collect_preserves_existing_policy_fields_when_enabling_upload(
    extension_client,
) -> None:
    client, application, policies, _highlights, _store = extension_client
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

    assert response.status_code == 200
    command = policies.upsert.await_args.args[1]
    assert command.enabled is True
    assert command.title_template == '保留这个标题'


def test_highlight_is_saved_independently_of_current_recording_state(
    extension_client,
) -> None:
    client, application, _policies, highlights, _store = extension_client
    token = pair(client)
    application.collected = True
    application.running_status = RunningStatus.STOPPED

    response = client.post(
        '/api/v1/browser-extension/rooms/100/highlights',
        headers=extension_headers(token),
        json={
            'observedAtMs': 1_000_000,
            'playerDelayMs': 18_500,
            'title': '直播标题',
            'anchorName': '主播',
        },
    )

    assert response.status_code == 201
    highlights.create_marker.assert_awaited_once_with(
        room_id=100,
        observed_at_ms=1_000_000,
        player_delay_ms=18_500,
        title='直播标题',
        anchor_name='主播',
        source='browser_extension',
    )


def test_extension_routes_reject_missing_malformed_and_revoked_tokens(
    extension_client,
) -> None:
    client, _application, _policies, _highlights, store = extension_client
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
