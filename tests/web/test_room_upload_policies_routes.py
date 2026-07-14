from dataclasses import dataclass
from typing import Iterator, List, Optional

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from blrec.bili_upload.policies import (
    InvalidRoomUploadPolicy,
    RoomUploadPolicyCommand,
    RoomUploadPolicyNotFound,
    RoomUploadPolicyView,
)
from blrec.web import security
from blrec.web.routers import room_upload_policies


def policy_view(room_id: int = 100) -> RoomUploadPolicyView:
    return RoomUploadPolicyView(
        room_id=room_id,
        account_mode='primary',
        account_id=None,
        resolved_account_id=7,
        resolved_account_name='投稿账号',
        enabled=True,
        title_template='{{ title }} 录播',
        description_template='主播：{{ anchor_name }}',
        tid=17,
        tags='直播,录播',
        copyright=1,
        source='',
        auto_comment=False,
        danmaku_backfill=False,
        filters={},
        blocked_reason=None,
        created_at=1000,
        updated_at=1000,
    )


@dataclass
class FakePolicyManager:
    command: Optional[RoomUploadPolicyCommand] = None
    invalid: bool = False
    missing: bool = False

    async def list(self) -> List[RoomUploadPolicyView]:
        return [policy_view()]

    async def upsert(
        self, room_id: int, command: RoomUploadPolicyCommand
    ) -> RoomUploadPolicyView:
        if self.invalid:
            raise InvalidRoomUploadPolicy('an active upload account is required')
        self.command = command
        return policy_view(room_id)

    async def delete(self, room_id: int) -> None:
        if self.missing:
            raise RoomUploadPolicyNotFound('room upload policy not found')


@pytest.fixture(autouse=True)
def restore_router_state() -> Iterator[None]:
    old_manager = room_upload_policies.manager
    old_reason = room_upload_policies.unavailable_reason
    old_key = security.api_key
    yield
    room_upload_policies.manager = old_manager
    room_upload_policies.unavailable_reason = old_reason
    security.api_key = old_key


@pytest.fixture
def manager() -> FakePolicyManager:
    value = FakePolicyManager()
    room_upload_policies.manager = value  # type: ignore[assignment]
    room_upload_policies.unavailable_reason = None
    return value


@pytest.fixture
def client(manager: FakePolicyManager) -> Iterator[TestClient]:
    api = FastAPI()
    api.include_router(room_upload_policies.router, prefix='/api/v1')
    security.api_key = 'test-api-key'
    security.whitelist.clear()
    security.blacklist.clear()
    security.attempting_clients.clear()
    with TestClient(api) as test_client:
        yield test_client


def auth_headers() -> dict:
    return {'x-api-key': 'test-api-key'}


def test_list_room_upload_policies_returns_resolved_account(client: TestClient) -> None:
    response = client.get('/api/v1/room-upload-policies', headers=auth_headers())

    assert response.status_code == 200
    assert response.json()[0] == {
        'roomId': 100,
        'accountMode': 'primary',
        'accountId': None,
        'resolvedAccountId': 7,
        'resolvedAccountName': '投稿账号',
        'enabled': True,
        'titleTemplate': '{{ title }} 录播',
        'descriptionTemplate': '主播：{{ anchor_name }}',
        'tid': 17,
        'tags': '直播,录播',
        'copyright': 1,
        'source': '',
        'autoComment': False,
        'danmakuBackfill': False,
        'filters': {},
        'blockedReason': None,
        'createdAt': 1000,
        'updatedAt': 1000,
    }


def test_upsert_converts_request_to_domain_command(
    client: TestClient, manager: FakePolicyManager
) -> None:
    response = client.put(
        '/api/v1/room-upload-policies/100',
        headers=auth_headers(),
        json={
            'accountMode': 'fixed',
            'accountId': 7,
            'enabled': True,
            'titleTemplate': '{{ title }} 录播',
            'descriptionTemplate': '主播：{{ anchor_name }}',
            'tid': 17,
            'tags': '直播,录播',
            'copyright': 1,
            'source': '',
            'autoComment': False,
            'danmakuBackfill': True,
            'filters': {'blockedWords': ['抽奖']},
        },
    )

    assert response.status_code == 200
    assert manager.command is not None
    assert manager.command.account_mode == 'fixed'
    assert manager.command.account_id == 7
    assert manager.command.danmaku_backfill is True
    assert manager.command.filters == {'blockedWords': ['抽奖']}


def test_invalid_policy_returns_conflict(
    client: TestClient, manager: FakePolicyManager
) -> None:
    manager.invalid = True

    response = client.put(
        '/api/v1/room-upload-policies/100',
        headers=auth_headers(),
        json={
            'accountMode': 'primary',
            'accountId': None,
            'enabled': True,
            'titleTemplate': '录播',
            'descriptionTemplate': '',
            'tid': 17,
            'tags': '录播',
            'copyright': 1,
            'source': '',
            'autoComment': False,
            'danmakuBackfill': False,
            'filters': {},
        },
    )

    assert response.status_code == 409
    assert response.json()['detail'] == 'an active upload account is required'


def test_delete_missing_policy_returns_not_found(
    client: TestClient, manager: FakePolicyManager
) -> None:
    manager.missing = True

    response = client.delete('/api/v1/room-upload-policies/100', headers=auth_headers())

    assert response.status_code == 404
