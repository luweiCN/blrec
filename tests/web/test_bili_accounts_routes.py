from dataclasses import dataclass
from typing import Iterator, List, Optional

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from blrec.bili_upload.account_lifecycle import (
    AccountRelationships,
    AccountRemovalBlocked,
    AccountRemovalCommand,
    AccountRemovalResult,
    InvalidAccountReplacement,
    RelatedUploadJob,
    RemovalMode,
)
from blrec.bili_upload.accounts import (
    AccountNotFound,
    AccountView,
    QrSessionNotFound,
    QrSessionView,
)
from blrec.web import security
from blrec.web.routers import bili_accounts


@dataclass(frozen=True)
class FakeRenewalCheckResult:
    credential_version: int
    refreshed: bool


@dataclass
class FakeAccountManager:
    missing_session: bool = False
    missing_account: bool = False
    last_subject: Optional[str] = None
    create_calls: int = 0
    status_calls: int = 0
    last_removal_command: Optional[AccountRemovalCommand] = None
    removal_error: Optional[Exception] = None

    async def create_qr(self, *, manager_subject: str) -> QrSessionView:
        self.create_calls += 1
        self.last_subject = manager_subject
        return QrSessionView(
            id='session-1',
            state='pending',
            qr_url='https://passport.example.invalid/secret-auth-code',
            expires_at=1234,
            poller_id='internal-poller',
        )

    async def status(self, session_id: str, *, manager_subject: str) -> QrSessionView:
        self.status_calls += 1
        self.last_subject = manager_subject
        if self.missing_session:
            raise QrSessionNotFound('QR session not found')
        return QrSessionView(
            id=session_id,
            state='confirmed',
            qr_url=None,
            expires_at=1234,
            poller_id=None,
            account_id=7,
        )

    async def cancel(self, session_id: str, *, manager_subject: str) -> QrSessionView:
        self.last_subject = manager_subject
        return QrSessionView(
            id=session_id,
            state='cancelled',
            qr_url=None,
            expires_at=1234,
            poller_id=None,
        )

    async def list_accounts(self) -> List[AccountView]:
        return [
            AccountView(
                id=7,
                uid=42,
                display_name='fixture',
                avatar_url='https://i0.hdslb.com/face.jpg',
                credential_version=3,
                credential_expires_at=2_000_000,
                created_at=100,
                state='active',
                is_primary=True,
            )
        ]

    async def set_primary_account(self, account_id: int) -> AccountView:
        if self.missing_account:
            raise AccountNotFound('Bilibili account not found')
        return (await self.list_accounts())[0]

    async def account_relationships(self, account_id: int) -> AccountRelationships:
        if self.missing_account:
            raise AccountNotFound('Bilibili account not found')
        return AccountRelationships(
            account_id=account_id,
            is_primary=True,
            follow_primary_room_ids=(200,),
            fixed_room_ids=(100,),
            reassignable_jobs=(RelatedUploadJob(1, 100, 'ready'),),
            blocking_jobs=(),
            historical_job_count=2,
        )

    async def remove_account(
        self, account_id: int, command: AccountRemovalCommand, *, manager_subject: str
    ) -> AccountRemovalResult:
        self.last_subject = manager_subject
        self.last_removal_command = command
        if self.missing_account:
            raise AccountNotFound('Bilibili account not found')
        if self.removal_error is not None:
            raise self.removal_error
        return AccountRemovalResult(account_id)

    async def check_account_renewal(self, account_id: int) -> FakeRenewalCheckResult:
        if self.missing_account:
            raise AccountNotFound('Bilibili account not found')
        return FakeRenewalCheckResult(credential_version=4, refreshed=True)


@pytest.fixture(autouse=True)
def restore_router_state() -> Iterator[None]:
    old_manager = bili_accounts.manager
    old_reason = bili_accounts.unavailable_reason
    old_key = security.api_key
    whitelist = security.whitelist.copy()
    blacklist = security.blacklist.copy()
    attempting = security.attempting_clients.copy()
    yield
    bili_accounts.manager = old_manager
    bili_accounts.unavailable_reason = old_reason
    security.api_key = old_key
    security.whitelist.clear()
    security.whitelist.update(whitelist)
    security.blacklist.clear()
    security.blacklist.update(blacklist)
    security.attempting_clients.clear()
    security.attempting_clients.update(attempting)


@pytest.fixture
def manager() -> FakeAccountManager:
    value = FakeAccountManager()
    bili_accounts.manager = value  # type: ignore[assignment]
    bili_accounts.unavailable_reason = None
    return value


@pytest.fixture
def client(manager: FakeAccountManager) -> Iterator[TestClient]:
    api = FastAPI()
    api.include_router(bili_accounts.router, prefix='/api/v1')
    security.api_key = 'test-api-key'
    security.whitelist.clear()
    security.blacklist.clear()
    security.attempting_clients.clear()
    with TestClient(api) as test_client:
        yield test_client


def auth_headers() -> dict:
    return {'x-api-key': 'test-api-key'}


def test_sensitive_routes_require_a_configured_api_key(
    manager: FakeAccountManager,
) -> None:
    api = FastAPI()
    api.include_router(bili_accounts.router, prefix='/api/v1')
    security.api_key = ''

    with TestClient(api, raise_server_exceptions=False) as test_client:
        response = test_client.get('/api/v1/bili-accounts')

    assert response.status_code == 401


def test_unavailable_account_manager_fails_closed(
    client: TestClient, manager: FakeAccountManager
) -> None:
    bili_accounts.manager = None
    bili_accounts.unavailable_reason = 'credential key is required'

    response = client.get('/api/v1/bili-accounts', headers=auth_headers())

    assert response.status_code == 503
    assert response.json()['detail'] == 'credential key is required'


def test_create_and_poll_qr_session_returns_no_internal_poller(
    client: TestClient, manager: FakeAccountManager
) -> None:
    created = client.post('/api/v1/bili-accounts/qr-sessions', headers=auth_headers())
    first_subject = manager.last_subject
    reused = client.post('/api/v1/bili-accounts/qr-sessions', headers=auth_headers())
    status = client.get(
        '/api/v1/bili-accounts/qr-sessions/session-1', headers=auth_headers()
    )

    assert created.status_code == 201
    assert reused.status_code == 201
    assert reused.json()['id'] == created.json()['id']
    assert created.json() == {
        'id': 'session-1',
        'state': 'pending',
        'qrUrl': 'https://passport.example.invalid/secret-auth-code',
        'expiresAt': 1234,
        'accountId': None,
    }
    assert status.json()['state'] == 'confirmed'
    assert status.json()['accountId'] == 7
    assert 'pollerId' not in created.json()
    assert manager.last_subject
    assert first_subject == manager.last_subject
    assert manager.create_calls == 2
    assert manager.status_calls == 1
    assert 'test-api-key' not in manager.last_subject


def test_list_accounts_is_redacted(client: TestClient) -> None:
    response = client.get('/api/v1/bili-accounts', headers=auth_headers())

    assert response.status_code == 200
    assert response.json() == [
        {
            'id': 7,
            'uid': 42,
            'displayName': 'fixture',
            'avatarUrl': 'https://i0.hdslb.com/face.jpg',
            'credentialVersion': 3,
            'credentialExpiresAt': 2_000_000,
            'createdAt': 100,
            'state': 'active',
            'isPrimary': True,
        }
    ]
    assert 'token' not in response.text.lower()
    assert 'cookie' not in response.text.lower()


def test_missing_qr_session_returns_404(
    client: TestClient, manager: FakeAccountManager
) -> None:
    manager.missing_session = True

    response = client.get(
        '/api/v1/bili-accounts/qr-sessions/missing', headers=auth_headers()
    )

    assert response.status_code == 404


def test_manual_refresh_returns_new_credential_version(client: TestClient) -> None:
    response = client.post('/api/v1/bili-accounts/7/refresh', headers=auth_headers())

    assert response.status_code == 200
    assert response.json() == {'credentialVersion': 4, 'refreshed': True}


def test_select_primary_account_returns_redacted_account(client: TestClient) -> None:
    response = client.put('/api/v1/bili-accounts/7/primary', headers=auth_headers())

    assert response.status_code == 200
    assert response.json()['id'] == 7
    assert response.json()['isPrimary'] is True
    assert 'cookie' not in response.text.lower()


def test_relationships_preview_is_redacted(client: TestClient) -> None:
    response = client.get(
        '/api/v1/bili-accounts/7/relationships', headers=auth_headers()
    )

    assert response.status_code == 200
    assert response.json() == {
        'accountId': 7,
        'isPrimary': True,
        'followPrimaryRoomIds': [200],
        'fixedRoomIds': [100],
        'reassignableJobs': [{'id': 1, 'roomId': 100, 'state': 'ready'}],
        'blockingJobs': [],
        'historicalJobCount': 2,
    }
    assert 'token' not in response.text.lower()
    assert 'cookie' not in response.text.lower()


def test_remove_account_passes_explicit_policy_and_manager_subject(
    client: TestClient, manager: FakeAccountManager
) -> None:
    response = client.post(
        '/api/v1/bili-accounts/7/removal',
        headers=auth_headers(),
        json={'mode': 'fixed', 'replacementAccountId': 8, 'newPrimaryAccountId': 9},
    )

    assert response.status_code == 200
    assert response.json() == {'accountId': 7, 'state': 'archived'}
    assert manager.last_removal_command == AccountRemovalCommand(
        RemovalMode.FIXED, replacement_account_id=8, new_primary_account_id=9
    )
    assert manager.last_subject
    assert 'test-api-key' not in manager.last_subject


@pytest.mark.parametrize('path', ['relationships', 'removal'])
def test_missing_account_lifecycle_route_returns_404(
    path: str, client: TestClient, manager: FakeAccountManager
) -> None:
    manager.missing_account = True

    if path == 'relationships':
        response = client.get(
            '/api/v1/bili-accounts/7/relationships', headers=auth_headers()
        )
    else:
        response = client.post(
            '/api/v1/bili-accounts/7/removal',
            headers=auth_headers(),
            json={'mode': 'disable'},
        )

    assert response.status_code == 404


@pytest.mark.parametrize(
    'error',
    [
        AccountRemovalBlocked((RelatedUploadJob(1, 100, 'uploading'),)),
        InvalidAccountReplacement('replacement account is unavailable'),
    ],
)
def test_unsafe_account_removal_returns_409(
    error: Exception, client: TestClient, manager: FakeAccountManager
) -> None:
    manager.removal_error = error

    response = client.post(
        '/api/v1/bili-accounts/7/removal',
        headers=auth_headers(),
        json={'mode': 'disable'},
    )

    assert response.status_code == 409
