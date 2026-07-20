import os
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from argon2 import PasswordHasher

from blrec.web.auth_store import (
    AdminAlreadyInitialized,
    AdminAuthStore,
    AuthenticationFailed,
    AuthenticationRateLimited,
)


class Clock:
    def __init__(self, value: int = 1_000_000) -> None:
        self.value = value

    def __call__(self) -> float:
        return float(self.value)


def store(tmp_path: Path, clock: Clock) -> AdminAuthStore:
    return AdminAuthStore(
        str(tmp_path / 'auth.sqlite3'),
        admin_username='owner',
        clock=clock,
        session_ttl_seconds=30 * 24 * 3600,
        session_refresh_window_seconds=7 * 24 * 3600,
        max_failed_attempts=3,
        failure_window_seconds=60,
        lockout_seconds=120,
    )


@pytest.mark.parametrize('interval', [0, -1])
def test_activity_write_interval_must_be_positive(
    tmp_path: Path, interval: int
) -> None:
    with pytest.raises(ValueError, match='activity write interval must be positive'):
        AdminAuthStore(
            str(tmp_path / 'auth.sqlite3'), activity_write_interval_seconds=interval
        )


def test_setup_hashes_password_and_creates_private_database(tmp_path: Path) -> None:
    clock = Clock()
    auth = store(tmp_path, clock)
    auth.open()

    credentials = auth.initialize('owner', 'correct horse battery staple')

    assert auth.is_initialized()
    assert credentials.session_token
    assert credentials.csrf_token
    assert credentials.expires_at == clock.value + 30 * 24 * 3600
    assert os.stat(auth.path).st_mode & 0o777 == 0o600
    row = auth._connection.execute(  # type: ignore[union-attr]
        'SELECT password_hash FROM admin WHERE id=1'
    ).fetchone()
    assert row is not None
    assert str(row['password_hash']).startswith('$argon2id$')
    assert 'correct horse battery staple' not in str(row['password_hash'])
    with pytest.raises(AdminAlreadyInitialized):
        auth.initialize('owner', 'another secure password')

    auth.close()


def test_session_database_stores_only_token_hashes(tmp_path: Path) -> None:
    clock = Clock()
    auth = store(tmp_path, clock)
    auth.open()
    credentials = auth.initialize('owner', 'correct horse battery staple')

    row = auth._connection.execute(  # type: ignore[union-attr]
        'SELECT token_hash,csrf_hash FROM admin_sessions'
    ).fetchone()

    assert row is not None
    assert credentials.session_token not in str(row['token_hash'])
    assert credentials.csrf_token not in str(row['csrf_hash'])
    session = auth.authenticate_session(credentials.session_token)
    assert session is not None
    assert session.csrf_token == credentials.csrf_token
    assert auth.verify_csrf(credentials.session_token, credentials.csrf_token)
    assert not auth.verify_csrf(credentials.session_token, 'wrong')
    auth.close()


def test_session_activity_is_persisted_at_most_once_per_interval(
    tmp_path: Path,
) -> None:
    clock = Clock()
    auth = store(tmp_path, clock)
    auth.open()
    credentials = auth.initialize('owner', 'correct horse battery staple')
    connection = auth._connection
    assert connection is not None
    before = connection.total_changes

    assert auth.authenticate_session(credentials.session_token) is not None
    assert auth.authenticate_session(credentials.session_token) is not None
    assert connection.total_changes == before

    clock.value += 59
    assert auth.authenticate_session(credentials.session_token) is not None
    assert connection.total_changes == before

    clock.value += 1
    assert auth.authenticate_session(credentials.session_token) is not None
    assert connection.total_changes == before + 1
    auth.close()


def test_login_rate_limit_and_success_reset(tmp_path: Path) -> None:
    clock = Clock()
    auth = store(tmp_path, clock)
    auth.open()
    auth.initialize('owner', 'correct horse battery staple')

    for _ in range(2):
        with pytest.raises(AuthenticationFailed):
            auth.login('owner', 'wrong password', client_key='192.0.2.1')
    with pytest.raises(AuthenticationRateLimited) as error:
        auth.login('owner', 'wrong password', client_key='192.0.2.1')
    assert error.value.retry_after == 120
    with pytest.raises(AuthenticationRateLimited):
        auth.login('owner', 'correct horse battery staple', client_key='192.0.2.1')

    clock.value += 121
    credentials = auth.login(
        'owner', 'correct horse battery staple', client_key='192.0.2.1'
    )
    assert auth.authenticate_session(credentials.session_token) is not None
    auth.close()


def test_session_expiry_sliding_refresh_logout_and_password_reset(
    tmp_path: Path,
) -> None:
    clock = Clock()
    auth = store(tmp_path, clock)
    auth.open()
    first = auth.initialize('owner', 'correct horse battery staple')

    clock.value = first.expires_at - 6 * 24 * 3600
    refreshed = auth.authenticate_session(first.session_token)
    assert refreshed is not None
    assert refreshed.expires_at == clock.value + 30 * 24 * 3600

    auth.logout(first.session_token)
    assert auth.authenticate_session(first.session_token) is None

    second = auth.login('owner', 'correct horse battery staple', client_key='192.0.2.2')
    auth.reset_password('new correct horse battery staple')
    assert auth.authenticate_session(second.session_token) is None
    with pytest.raises(AuthenticationFailed):
        auth.login('owner', 'correct horse battery staple', client_key='192.0.2.3')
    third = auth.login(
        'owner', 'new correct horse battery staple', client_key='192.0.2.3'
    )
    assert auth.authenticate_session(third.session_token) is not None

    clock.value = third.expires_at + 1
    assert auth.authenticate_session(third.session_token) is None
    auth.close()


def test_media_signing_key_persists_across_reopen(tmp_path: Path) -> None:
    clock = Clock()
    auth = store(tmp_path, clock)
    auth.open()
    first_key = auth.media_signing_key
    auth.close()

    reopened = store(tmp_path, clock)
    reopened.open()
    assert reopened.media_signing_key == first_key
    assert len(first_key) == 32
    reopened.close()


def test_wrong_username_is_rejected_after_password_verification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = Clock()
    auth = store(tmp_path, clock)
    auth.open()
    auth.initialize('owner', 'correct horse battery staple')
    verify = PasswordHasher.verify
    calls = 0

    def counting_verify(
        hasher: PasswordHasher, encoded_hash: str, password: str
    ) -> bool:
        nonlocal calls
        calls += 1
        return bool(verify(hasher, encoded_hash, password))

    monkeypatch.setattr(PasswordHasher, 'verify', counting_verify)

    with pytest.raises(AuthenticationFailed):
        auth.login(
            'someone-else', 'correct horse battery staple', client_key='192.0.2.10'
        )

    assert calls == 1
    auth.close()


def test_password_verification_does_not_hold_the_session_store_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = Clock()
    auth = store(tmp_path, clock)
    auth.open()
    credentials = auth.initialize('owner', 'correct horse battery staple')
    verify_started = threading.Event()
    release_verify = threading.Event()
    original_verify = PasswordHasher.verify

    def blocking_verify(
        hasher: PasswordHasher, encoded_hash: str, password: str
    ) -> bool:
        verify_started.set()
        assert release_verify.wait(5)
        return bool(original_verify(hasher, encoded_hash, password))

    monkeypatch.setattr(PasswordHasher, 'verify', blocking_verify)
    with ThreadPoolExecutor(max_workers=1) as executor:
        login = executor.submit(
            auth.login, 'owner', 'correct horse battery staple', client_key='192.0.2.30'
        )
        assert verify_started.wait(5)
        try:
            lock_available = auth._lock.acquire(blocking=False)
            assert lock_available, 'password verification still owns the store lock'
            auth._lock.release()
            assert auth.authenticate_session(credentials.session_token) is not None
        finally:
            release_verify.set()
        login.result(timeout=5)

    auth.close()


def test_password_replacement_hash_does_not_hold_the_session_store_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = Clock()
    auth = store(tmp_path, clock)
    auth.open()
    credentials = auth.initialize('owner', 'correct horse battery staple')
    hash_started = threading.Event()
    release_hash = threading.Event()
    original_hash = PasswordHasher.hash

    def blocking_hash(hasher: PasswordHasher, password: str) -> str:
        hash_started.set()
        assert release_hash.wait(5)
        return str(original_hash(hasher, password))

    monkeypatch.setattr(PasswordHasher, 'hash', blocking_hash)
    with ThreadPoolExecutor(max_workers=1) as executor:
        change = executor.submit(
            auth.change_password,
            'correct horse battery staple',
            'new correct horse battery staple',
        )
        assert hash_started.wait(5)
        try:
            lock_available = auth._lock.acquire(blocking=False)
            assert lock_available, 'replacement hash still owns the store lock'
            auth._lock.release()
            assert auth.authenticate_session(credentials.session_token) is not None
        finally:
            release_hash.set()
        change.result(timeout=5)

    assert auth.authenticate_session(credentials.session_token) is None
    auth.close()


def test_login_commit_rejects_a_password_changed_after_verification(
    tmp_path: Path,
) -> None:
    clock = Clock()
    auth = store(tmp_path, clock)
    auth.open()
    auth.initialize('owner', 'correct horse battery staple')
    ticket = auth.prepare_login('owner', client_key='192.0.2.31')
    verification = auth.check_login_password(ticket, 'correct horse battery staple')

    auth.reset_password('new correct horse battery staple')

    with pytest.raises(AuthenticationFailed):
        auth.commit_login(ticket, verification)
    assert (
        auth.login(
            'owner', 'new correct horse battery staple', client_key='192.0.2.31'
        ).session_token
        != ''
    )
    auth.close()


def test_login_commit_cannot_create_a_session_after_concurrent_password_reset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = Clock()
    login_store = store(tmp_path, clock)
    reset_store = store(tmp_path, clock)
    login_store.open()
    reset_store.open()
    login_store.initialize('owner', 'correct horse battery staple')
    ticket = login_store.prepare_login('owner', client_key='192.0.2.32')
    verification = login_store.check_login_password(
        ticket, 'correct horse battery staple'
    )
    password_selected = threading.Event()
    continue_login = threading.Event()
    original_matches = login_store._login_ticket_matches

    def block_after_password_select(ticket_value, row) -> bool:
        password_selected.set()
        assert continue_login.wait(5)
        return original_matches(ticket_value, row)

    monkeypatch.setattr(
        login_store, '_login_ticket_matches', block_after_password_select
    )

    with ThreadPoolExecutor(max_workers=1) as executor:
        login = executor.submit(login_store.commit_login, ticket, verification)
        assert password_selected.wait(5)
        try:
            reset_store.reset_password('new correct horse battery staple')
        finally:
            continue_login.set()
        with pytest.raises(AuthenticationFailed):
            login.result(timeout=5)

    connection = reset_store._connection
    assert connection is not None
    assert connection.execute('SELECT COUNT(*) FROM admin_sessions').fetchone()[0] == 0
    login_store.close()
    reset_store.close()


def test_plain_login_does_not_invalidate_a_prepared_password_change(
    tmp_path: Path,
) -> None:
    clock = Clock(100)
    auth = store(tmp_path, clock)
    auth.open()
    auth.initialize('owner', 'correct horse battery staple')
    ticket = auth.prepare_password_change()
    verification = auth.check_password_change(
        ticket, 'correct horse battery staple', 'new correct horse battery staple'
    )

    clock.value = 101
    auth.login('owner', 'correct horse battery staple', client_key='192.0.2.33')
    connection = auth._connection
    assert connection is not None
    assert (
        connection.execute('SELECT updated_at FROM admin WHERE id=1').fetchone()[0]
        == 100
    )
    auth.commit_password_change(ticket, verification)

    assert (
        auth.login(
            'owner', 'new correct horse battery staple', client_key='192.0.2.34'
        ).session_token
        != ''
    )
    auth.close()


def test_bootstrap_rate_limit_is_persistent_and_separate_from_login(
    tmp_path: Path,
) -> None:
    clock = Clock()
    auth = store(tmp_path, clock)
    auth.open()
    auth.initialize('owner', 'correct horse battery staple')

    for _ in range(2):
        with pytest.raises(AuthenticationFailed):
            auth.verify_bootstrap_attempt(
                'owner', credential_valid=False, client_key='192.0.2.20'
            )

    credentials = auth.login(
        'owner', 'correct horse battery staple', client_key='192.0.2.20'
    )
    assert credentials.session_token

    with pytest.raises(AuthenticationRateLimited):
        auth.verify_bootstrap_attempt(
            'owner', credential_valid=False, client_key='192.0.2.20'
        )
    auth.close()

    reopened = store(tmp_path, clock)
    reopened.open()
    with pytest.raises(AuthenticationRateLimited):
        reopened.verify_bootstrap_attempt(
            'owner', credential_valid=True, client_key='192.0.2.20'
        )
    reopened.close()


def test_extension_pairing_stores_only_a_hash_and_supports_revocation(
    tmp_path: Path,
) -> None:
    clock = Clock()
    auth = store(tmp_path, clock)
    auth.open()
    auth.initialize('owner', 'correct horse battery staple')

    credentials = auth.issue_extension_token('owner', client_key='192.168.50.8')

    assert credentials.token.startswith('blrec_ext_')
    identity = auth.authenticate_extension(credentials.token)
    assert identity is not None
    assert identity.token_id == credentials.token_id
    row = auth._connection.execute(  # type: ignore[union-attr]
        'SELECT token_hash,created_at,last_used_at,revoked_at '
        'FROM extension_tokens WHERE id=?',
        (credentials.token_id,),
    ).fetchone()
    assert row is not None
    assert credentials.token not in '|'.join(str(value) for value in row)
    assert len(str(row['token_hash'])) == 64

    clock.value += 60
    used = auth.authenticate_extension(credentials.token)
    assert used is not None
    assert used.last_used_at == clock.value
    assert auth.list_extension_tokens()[0].last_used_at == clock.value

    assert auth.revoke_extension_token(credentials.token_id)
    assert auth.authenticate_extension(credentials.token) is None
    assert auth.list_extension_tokens()[0].revoked_at == clock.value
    events = [
        str(row['event'])
        for row in auth._connection.execute(  # type: ignore[union-attr]
            'SELECT event FROM auth_audit ORDER BY id'
        )
    ]
    assert 'extension_pair_succeeded' in events
    assert 'extension_token_used' in events
    assert 'extension_token_revoked' in events
    auth.close()


def test_extension_pairing_wrong_username_is_rate_limited_separately(
    tmp_path: Path,
) -> None:
    clock = Clock()
    auth = store(tmp_path, clock)
    auth.open()
    auth.initialize('owner', 'correct horse battery staple')

    for _ in range(2):
        with pytest.raises(AuthenticationFailed):
            auth.issue_extension_token('wrong', client_key='192.168.50.9')
    with pytest.raises(AuthenticationRateLimited):
        auth.issue_extension_token('wrong', client_key='192.168.50.9')

    credentials = auth.login(
        'owner', 'correct horse battery staple', client_key='192.168.50.9'
    )
    assert credentials.session_token
    auth.close()
