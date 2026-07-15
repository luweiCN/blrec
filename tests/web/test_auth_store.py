import os
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
