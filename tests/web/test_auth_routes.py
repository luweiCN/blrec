import asyncio
import hashlib
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from email.utils import parsedate_to_datetime
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Iterator

import pytest
from argon2 import PasswordHasher
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from blrec.web import security
from blrec.web.auth_store import AdminAuthStore
from blrec.web.middlewares.security_headers import SecurityHeadersMiddleware
from blrec.web.password_work import PasswordWorkCoordinator, PasswordWorkSaturated
from blrec.web.routers import auth as auth_router


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    store = AdminAuthStore(str(tmp_path / 'auth.sqlite3'), admin_username='owner')
    store.open()
    password_work = PasswordWorkCoordinator()
    security.configure(store, bootstrap_api_key='bootstrap-key')
    auth_router.configure(
        store, password_work=password_work, bootstrap_api_key='bootstrap-key'
    )
    api = FastAPI(dependencies=[Depends(security.authenticate)])
    api.add_middleware(SecurityHeadersMiddleware)
    api.include_router(auth_router.router, prefix='/api/v1')

    @api.get('/api/v1/protected')
    async def protected_get() -> dict:
        return {'ok': True}

    @api.post('/api/v1/protected')
    async def protected_post() -> dict:
        return {'ok': True}

    with TestClient(api, base_url='https://testserver') as value:
        yield value
    security.reset()
    auth_router.reset()
    asyncio.run(password_work.shutdown())
    store.close()


@pytest.mark.asyncio
async def test_password_worker_admits_one_active_and_four_waiting_jobs() -> None:
    coordinator = PasswordWorkCoordinator()
    first_started = threading.Event()
    release_first = threading.Event()
    active = 0
    peak_active = 0
    active_lock = threading.Lock()

    def blocking_job(value: int) -> int:
        nonlocal active, peak_active
        with active_lock:
            active += 1
            peak_active = max(peak_active, active)
        if value == 0:
            first_started.set()
            assert release_first.wait(5)
        with active_lock:
            active -= 1
        return value

    jobs = [
        asyncio.create_task(coordinator.run(lambda value=value: blocking_job(value)))
        for value in range(5)
    ]
    loop = asyncio.get_running_loop()
    assert await loop.run_in_executor(None, first_started.wait, 5)
    for _ in range(10):
        if coordinator.admitted_count == 5:
            break
        await asyncio.sleep(0)
    assert coordinator.admitted_count == 5

    with pytest.raises(PasswordWorkSaturated) as error:
        await coordinator.run(lambda: 6)
    assert error.value.retry_after == 1

    release_first.set()
    assert await asyncio.gather(*jobs) == [0, 1, 2, 3, 4]
    assert peak_active == 1
    await coordinator.shutdown()


@pytest.mark.asyncio
async def test_password_worker_shutdown_waits_for_admitted_work() -> None:
    coordinator = PasswordWorkCoordinator()
    started = threading.Event()
    release = threading.Event()

    def blocking_job() -> str:
        started.set()
        assert release.wait(5)
        return 'done'

    job = asyncio.create_task(coordinator.run(blocking_job))
    loop = asyncio.get_running_loop()
    assert await loop.run_in_executor(None, started.wait, 5)
    shutdown = asyncio.create_task(coordinator.shutdown())
    await asyncio.sleep(0)
    assert not shutdown.done()

    release.set()
    assert await job == 'done'
    await shutdown


def setup_admin(client: TestClient) -> str:
    response = client.post(
        '/api/v1/auth/setup',
        headers={'origin': 'https://testserver'},
        json={
            'username': 'owner',
            'apiKey': 'bootstrap-key',
            'password': 'correct horse battery staple',
        },
    )
    assert response.status_code == 200
    return str(response.json()['csrfToken'])


def test_setup_is_one_time_and_sets_secure_http_only_cookie(client: TestClient) -> None:
    status_response = client.get('/api/v1/auth/status')
    assert status_response.json() == {'setupRequired': True, 'authenticated': False}

    response = client.post(
        '/api/v1/auth/setup',
        headers={'origin': 'https://testserver'},
        json={
            'username': 'owner',
            'apiKey': 'bootstrap-key',
            'password': 'correct horse battery staple',
        },
    )

    assert response.status_code == 200
    cookie = response.headers['set-cookie']
    assert 'blrec_session=' in cookie
    assert 'HttpOnly' in cookie
    assert 'Secure' in cookie
    assert 'SameSite=lax' in cookie
    assert response.json()['authenticated'] is True
    second = client.post(
        '/api/v1/auth/setup',
        headers={'origin': 'https://testserver'},
        json={
            'username': 'owner',
            'apiKey': 'bootstrap-key',
            'password': 'another correct password',
        },
    )
    assert second.status_code == 409


def test_setup_rejects_bad_bootstrap_key_and_cross_site_origin(
    client: TestClient,
) -> None:
    cross_site = client.post(
        '/api/v1/auth/setup',
        headers={'origin': 'https://evil.example'},
        json={
            'username': 'owner',
            'apiKey': 'bootstrap-key',
            'password': 'correct secure password',
        },
    )
    assert cross_site.status_code == 403

    bad_key = client.post(
        '/api/v1/auth/setup',
        headers={'origin': 'https://testserver'},
        json={
            'username': 'owner',
            'apiKey': 'wrong-key',
            'password': 'correct secure password',
        },
    )
    assert bad_key.status_code == 401


def test_session_auth_and_csrf_replace_api_key_header(client: TestClient) -> None:
    csrf_token = setup_admin(client)

    assert client.get('/api/v1/protected').status_code == 200
    assert client.post('/api/v1/protected').status_code == 403
    assert (
        client.post(
            '/api/v1/protected',
            headers={'origin': 'https://testserver', 'x-csrf-token': csrf_token},
        ).status_code
        == 200
    )

    client.cookies.clear()
    assert client.get('/api/v1/protected').status_code == 401
    assert (
        client.get(
            '/api/v1/protected', headers={'x-api-key': 'bootstrap-key'}
        ).status_code
        == 401
    )


def test_login_session_logout_and_password_change(client: TestClient) -> None:
    csrf_token = setup_admin(client)
    session = client.get('/api/v1/auth/session')
    assert session.status_code == 200
    assert session.json()['csrfToken'] == csrf_token

    changed = client.post(
        '/api/v1/auth/change-password',
        headers={'origin': 'https://testserver', 'x-csrf-token': csrf_token},
        json={
            'currentPassword': 'correct horse battery staple',
            'newPassword': 'new correct horse battery staple',
        },
    )
    assert changed.status_code == 204
    assert client.get('/api/v1/protected').status_code == 401

    wrong = client.post(
        '/api/v1/auth/login',
        headers={'origin': 'https://testserver'},
        json={'username': 'owner', 'password': 'correct horse battery staple'},
    )
    assert wrong.status_code == 401
    login = client.post(
        '/api/v1/auth/login',
        headers={'origin': 'https://testserver'},
        json={'username': 'owner', 'password': 'new correct horse battery staple'},
    )
    assert login.status_code == 200
    csrf_token = str(login.json()['csrfToken'])

    logout = client.post(
        '/api/v1/auth/logout',
        headers={'origin': 'https://testserver', 'x-csrf-token': csrf_token},
    )
    assert logout.status_code == 204
    assert 'Max-Age=0' in logout.headers['set-cookie']
    assert client.get('/api/v1/protected').status_code == 401


def test_session_renewal_reissues_the_cookie_with_the_current_expiry(
    client: TestClient,
) -> None:
    setup_admin(client)
    token = client.cookies.get(security.SESSION_COOKIE_NAME)
    assert token is not None
    store = security.auth_store
    assert store is not None
    now = int(time.time())
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            'UPDATE admin_sessions SET expires_at=? WHERE token_hash=?',
            (now + 60, hashlib.sha256(token.encode('utf8')).hexdigest()),
        )

    response = client.get('/api/v1/auth/session')

    assert response.status_code == 200
    assert 'set-cookie' in response.headers
    cookies = SimpleCookie()
    cookies.load(response.headers['set-cookie'])
    renewed = cookies[security.SESSION_COOKIE_NAME]
    expires_at = int(response.json()['expiresAt'])
    assert renewed.value == token
    assert expires_at > now + 29 * 24 * 3600
    assert abs(int(renewed['max-age']) - (expires_at - int(time.time()))) <= 1
    assert int(parsedate_to_datetime(renewed['expires']).timestamp()) == expires_at
    assert renewed['path'] == '/'
    assert renewed['secure'] is True
    assert renewed['httponly'] is True
    assert renewed['samesite'].lower() == 'lax'


def test_bootstrap_key_can_reset_password_and_revokes_sessions(
    client: TestClient,
) -> None:
    setup_admin(client)
    response = client.post(
        '/api/v1/auth/recover',
        headers={'origin': 'https://testserver'},
        json={
            'username': 'owner',
            'apiKey': 'bootstrap-key',
            'newPassword': 'recovered correct password',
        },
    )
    assert response.status_code == 204
    assert client.get('/api/v1/protected').status_code == 401
    login = client.post(
        '/api/v1/auth/login',
        headers={'origin': 'https://testserver'},
        json={'username': 'owner', 'password': 'recovered correct password'},
    )
    assert login.status_code == 200


def test_login_hides_whether_username_or_password_is_wrong(client: TestClient) -> None:
    setup_admin(client)
    client.cookies.clear()

    wrong_username = client.post(
        '/api/v1/auth/login',
        headers={'origin': 'https://testserver'},
        json={'username': 'someone-else', 'password': 'correct horse battery staple'},
    )
    wrong_password = client.post(
        '/api/v1/auth/login',
        headers={'origin': 'https://testserver'},
        json={'username': 'owner', 'password': 'incorrect password'},
    )

    assert wrong_username.status_code == 401
    assert wrong_password.status_code == 401
    assert wrong_username.json()['detail'] == 'Invalid administrator credentials'
    assert wrong_password.json()['detail'] == 'Invalid administrator credentials'


def test_bootstrap_credentials_are_rate_limited_without_locking_login(
    client: TestClient,
) -> None:
    setup_admin(client)
    client.cookies.clear()

    responses = [
        client.post(
            '/api/v1/auth/recover',
            headers={'origin': 'https://testserver'},
            json={
                'username': 'owner',
                'apiKey': 'wrong-key',
                'newPassword': 'another correct password',
            },
        )
        for _ in range(5)
    ]

    assert [response.status_code for response in responses] == [401, 401, 401, 401, 429]
    assert responses[-1].headers['retry-after'] == '900'
    login = client.post(
        '/api/v1/auth/login',
        headers={'origin': 'https://testserver'},
        json={'username': 'owner', 'password': 'correct horse battery staple'},
    )
    assert login.status_code == 200


def test_security_headers_are_added_to_every_response(client: TestClient) -> None:
    response = client.get('/api/v1/auth/status')

    assert response.headers['x-content-type-options'] == 'nosniff'
    assert response.headers['x-frame-options'] == 'DENY'
    assert response.headers['referrer-policy'] == 'same-origin'
    assert "frame-ancestors 'none'" in response.headers['content-security-policy']


def test_administrator_can_list_and_revoke_extension_tokens(client: TestClient) -> None:
    csrf_token = setup_admin(client)
    store = security.auth_store
    assert store is not None
    credentials = store.issue_extension_token('owner', client_key='192.168.50.8')

    listed = client.get('/api/v1/auth/extensions')

    assert listed.status_code == 200
    assert listed.json() == [
        {
            'id': credentials.token_id,
            'createdAt': credentials.created_at,
            'lastUsedAt': credentials.created_at,
            'revokedAt': None,
        }
    ]
    assert credentials.token not in listed.text

    revoked = client.delete(
        '/api/v1/auth/extensions/{}'.format(credentials.token_id),
        headers={'origin': 'https://testserver', 'x-csrf-token': csrf_token},
    )
    assert revoked.status_code == 204
    assert store.authenticate_extension(credentials.token) is None


def test_extension_token_is_not_an_administrator_session(client: TestClient) -> None:
    setup_admin(client)
    store = security.auth_store
    assert store is not None
    credentials = store.issue_extension_token('owner', client_key='192.168.50.8')
    client.cookies.clear()

    response = client.get(
        '/api/v1/protected', headers={'x-blrec-extension-token': credentials.token}
    )

    assert response.status_code == 401


def test_password_routes_run_argon2_only_in_the_password_worker(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = []
    original_hash = PasswordHasher.hash
    original_verify = PasswordHasher.verify

    def recording_hash(hasher: PasswordHasher, password: str) -> str:
        calls.append(('hash', threading.current_thread().name))
        return str(original_hash(hasher, password))

    def recording_verify(
        hasher: PasswordHasher, encoded_hash: str, password: str
    ) -> bool:
        calls.append(('verify', threading.current_thread().name))
        return bool(original_verify(hasher, encoded_hash, password))

    monkeypatch.setattr(PasswordHasher, 'hash', recording_hash)
    monkeypatch.setattr(PasswordHasher, 'verify', recording_verify)

    setup_admin(client)
    login = client.post(
        '/api/v1/auth/login',
        headers={'origin': 'https://testserver'},
        json={'username': 'owner', 'password': 'correct horse battery staple'},
    )
    assert login.status_code == 200
    csrf_token = str(login.json()['csrfToken'])
    changed = client.post(
        '/api/v1/auth/change-password',
        headers={'origin': 'https://testserver', 'x-csrf-token': csrf_token},
        json={
            'currentPassword': 'correct horse battery staple',
            'newPassword': 'new correct horse battery staple',
        },
    )
    assert changed.status_code == 204
    recovered = client.post(
        '/api/v1/auth/recover',
        headers={'origin': 'https://testserver'},
        json={
            'username': 'owner',
            'apiKey': 'bootstrap-key',
            'newPassword': 'recovered correct password',
        },
    )
    assert recovered.status_code == 204

    assert [kind for kind, _ in calls].count('hash') == 3
    assert [kind for kind, _ in calls].count('verify') == 2
    assert all(name.startswith('blrec-password') for _, name in calls)


def test_saturated_password_worker_returns_retryable_503_without_login_failure(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    setup_admin(client)
    client.cookies.clear()
    store = security.auth_store
    assert store is not None
    connection = store._connection
    assert connection is not None
    before = connection.execute('SELECT COUNT(*) FROM login_failures').fetchone()[0]
    coordinator = auth_router.password_work
    assert coordinator is not None
    verify_started = threading.Event()
    release_verify = threading.Event()
    original_verify = PasswordHasher.verify
    first_call = True
    call_lock = threading.Lock()

    def blocking_first_verify(
        hasher: PasswordHasher, encoded_hash: str, password: str
    ) -> bool:
        nonlocal first_call
        with call_lock:
            should_block = first_call
            first_call = False
        if should_block:
            verify_started.set()
            assert release_verify.wait(5)
        return bool(original_verify(hasher, encoded_hash, password))

    monkeypatch.setattr(PasswordHasher, 'verify', blocking_first_verify)

    def request_login() -> object:
        return client.post(
            '/api/v1/auth/login',
            headers={'origin': 'https://testserver'},
            json={'username': 'owner', 'password': 'wrong password'},
        )

    with ThreadPoolExecutor(max_workers=6) as executor:
        admitted = [executor.submit(request_login)]
        assert verify_started.wait(5)
        admitted.extend(executor.submit(request_login) for _ in range(4))
        deadline = time.monotonic() + 5
        while coordinator.admitted_count != 5 and time.monotonic() < deadline:
            threading.Event().wait(0.01)
        assert coordinator.admitted_count == 5

        rejected = executor.submit(request_login)
        try:
            response = rejected.result(timeout=1)
            assert response.status_code == 503
            assert response.headers['retry-after'] == '1'
            assert response.json()['detail'] == 'Password authentication is busy'
            during = connection.execute(
                'SELECT COUNT(*) FROM login_failures'
            ).fetchone()[0]
            assert during == before
        finally:
            release_verify.set()
        statuses = [future.result(timeout=5).status_code for future in admitted]

    assert statuses.count(401) == 4
    assert statuses.count(429) == 1
