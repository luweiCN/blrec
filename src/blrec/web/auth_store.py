from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import sqlite3
import stat
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from argon2.low_level import Type

__all__ = (
    'AdminAlreadyInitialized',
    'AdminAuthStore',
    'AuthenticationFailed',
    'AuthenticationRateLimited',
    'SessionCredentials',
)


class AdminAlreadyInitialized(RuntimeError):
    pass


class AuthenticationFailed(RuntimeError):
    pass


class AuthenticationRateLimited(AuthenticationFailed):
    def __init__(self, retry_after: int) -> None:
        super().__init__('too many failed login attempts')
        self.retry_after = max(1, int(retry_after))


@dataclass(frozen=True)
class SessionCredentials:
    session_token: str
    csrf_token: str
    expires_at: int


class AdminAuthStore:
    def __init__(
        self,
        path: str,
        *,
        admin_username: str = 'admin',
        clock: Callable[[], float] = time.time,
        session_ttl_seconds: int = 30 * 24 * 3600,
        session_refresh_window_seconds: int = 7 * 24 * 3600,
        max_failed_attempts: int = 5,
        failure_window_seconds: int = 5 * 60,
        lockout_seconds: int = 15 * 60,
    ) -> None:
        if session_ttl_seconds <= 0:
            raise ValueError('session TTL must be positive')
        if not 0 < session_refresh_window_seconds < session_ttl_seconds:
            raise ValueError('session refresh window must be within the TTL')
        if max_failed_attempts <= 0:
            raise ValueError('max failed attempts must be positive')
        if failure_window_seconds <= 0 or lockout_seconds <= 0:
            raise ValueError('login rate-limit windows must be positive')
        self._validate_username(admin_username)
        self.path = os.path.abspath(os.path.expanduser(path))
        self._admin_username = admin_username
        self._clock = clock
        self._session_ttl_seconds = session_ttl_seconds
        self._session_refresh_window_seconds = session_refresh_window_seconds
        self._max_failed_attempts = max_failed_attempts
        self._failure_window_seconds = failure_window_seconds
        self._lockout_seconds = lockout_seconds
        self._connection: Optional[sqlite3.Connection] = None
        self._lock = threading.RLock()
        self._password_hasher = PasswordHasher(
            time_cost=3,
            memory_cost=65536,
            parallelism=2,
            hash_len=32,
            salt_len=16,
            type=Type.ID,
        )
        self._dummy_password_hash: Optional[str] = None
        self._media_signing_key: Optional[bytes] = None

    def open(self) -> None:
        with self._lock:
            if self._connection is not None:
                return
            path = Path(self.path)
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists() or path.is_symlink():
                file_stat = os.lstat(self.path)
                if stat.S_ISLNK(file_stat.st_mode):
                    raise ValueError('auth database must not be a symlink')
                if not stat.S_ISREG(file_stat.st_mode):
                    raise ValueError('auth database must be a regular file')
            connection = sqlite3.connect(self.path, timeout=10, check_same_thread=False)
            connection.row_factory = sqlite3.Row
            try:
                os.chmod(self.path, 0o600)
                connection.execute('PRAGMA foreign_keys=ON')
                connection.execute('PRAGMA journal_mode=DELETE')
                connection.execute('PRAGMA synchronous=FULL')
                self._create_schema(connection)
                self._media_signing_key = self._load_or_create_media_key(connection)
                self._dummy_password_hash = self._password_hasher.hash(
                    secrets.token_urlsafe(32)
                )
            except BaseException:
                connection.close()
                raise
            self._connection = connection

    def close(self) -> None:
        with self._lock:
            if self._connection is not None:
                self._connection.close()
                self._connection = None
            self._dummy_password_hash = None
            self._media_signing_key = None

    @property
    def media_signing_key(self) -> bytes:
        self._require_open()
        assert self._media_signing_key is not None
        return self._media_signing_key

    def is_initialized(self) -> bool:
        connection = self._require_open()
        with self._lock:
            return (
                connection.execute('SELECT 1 FROM admin WHERE id=1').fetchone()
                is not None
            )

    def initialize(self, username: str, password: str) -> SessionCredentials:
        self._validate_password(password)
        connection = self._require_open()
        password_hash = self._password_hasher.hash(password)
        if not self._username_matches(username):
            raise AuthenticationFailed('invalid credentials')
        now = int(self._clock())
        with self._lock, connection:
            if connection.execute('SELECT 1 FROM admin WHERE id=1').fetchone():
                raise AdminAlreadyInitialized('administrator is already initialized')
            connection.execute(
                'INSERT INTO admin(id,password_hash,created_at,updated_at) '
                'VALUES(1,?,?,?)',
                (password_hash, now, now),
            )
            self._audit(connection, 'admin_initialized', now)
            return self._create_session(connection, now)

    def login(
        self, username: str, password: str, *, client_key: str
    ) -> SessionCredentials:
        connection = self._require_open()
        now = int(self._clock())
        rate_limit_key = self._rate_limit_key('login', client_key)
        with self._lock, connection:
            self._ensure_not_rate_limited(connection, rate_limit_key, now)
            row = connection.execute(
                'SELECT password_hash FROM admin WHERE id=1'
            ).fetchone()
            encoded_hash = (
                str(row['password_hash'])
                if row is not None
                else self._dummy_password_hash
            )
            assert encoded_hash is not None
            try:
                valid: bool = bool(self._password_hasher.verify(encoded_hash, password))
            except (VerifyMismatchError, InvalidHashError):
                valid = False
            if row is None or not valid or not self._username_matches(username):
                retry_after = self._record_failed_login(
                    connection, rate_limit_key, now, scope='login'
                )
                connection.commit()
                if retry_after is not None:
                    raise AuthenticationRateLimited(retry_after)
                raise AuthenticationFailed('invalid credentials')
            connection.execute(
                'DELETE FROM login_failures WHERE client_key=?', (rate_limit_key,)
            )
            if self._password_hasher.check_needs_rehash(encoded_hash):
                connection.execute(
                    'UPDATE admin SET password_hash=?,updated_at=? WHERE id=1',
                    (self._password_hasher.hash(password), now),
                )
            self._audit(connection, 'login_succeeded', now)
            return self._create_session(connection, now)

    def verify_bootstrap_attempt(
        self, username: str, credential_valid: bool, *, client_key: str
    ) -> None:
        connection = self._require_open()
        now = int(self._clock())
        rate_limit_key = self._rate_limit_key('bootstrap', client_key)
        with self._lock, connection:
            self._ensure_not_rate_limited(connection, rate_limit_key, now)
            if not credential_valid or not self._username_matches(username):
                retry_after = self._record_failed_login(
                    connection, rate_limit_key, now, scope='bootstrap'
                )
                connection.commit()
                if retry_after is not None:
                    raise AuthenticationRateLimited(retry_after)
                raise AuthenticationFailed('invalid credentials')
            connection.execute(
                'DELETE FROM login_failures WHERE client_key=?', (rate_limit_key,)
            )

    def authenticate_session(self, session_token: str) -> Optional[SessionCredentials]:
        if not session_token:
            return None
        connection = self._require_open()
        token_hash = self._token_hash(session_token)
        now = int(self._clock())
        with self._lock, connection:
            row = connection.execute(
                'SELECT expires_at FROM admin_sessions WHERE token_hash=?',
                (token_hash,),
            ).fetchone()
            if row is None:
                return None
            expires_at = int(row['expires_at'])
            if expires_at <= now:
                connection.execute(
                    'DELETE FROM admin_sessions WHERE token_hash=?', (token_hash,)
                )
                return None
            if expires_at - now <= self._session_refresh_window_seconds:
                expires_at = now + self._session_ttl_seconds
            csrf_token = self._csrf_token(session_token)
            csrf_hash = self._token_hash(csrf_token)
            updated = connection.execute(
                'UPDATE admin_sessions SET last_seen_at=?,expires_at=? '
                'WHERE token_hash=? AND csrf_hash=?',
                (now, expires_at, token_hash, csrf_hash),
            )
            if updated.rowcount != 1:
                return None
            return SessionCredentials(session_token, csrf_token, expires_at)

    def verify_csrf(self, session_token: str, csrf_token: str) -> bool:
        if not session_token or not csrf_token:
            return False
        expected = self._csrf_token(session_token)
        return secrets.compare_digest(expected, csrf_token)

    def logout(self, session_token: str) -> None:
        if not session_token:
            return
        connection = self._require_open()
        with self._lock, connection:
            connection.execute(
                'DELETE FROM admin_sessions WHERE token_hash=?',
                (self._token_hash(session_token),),
            )

    def change_password(self, current_password: str, new_password: str) -> None:
        self._validate_password(new_password)
        connection = self._require_open()
        now = int(self._clock())
        with self._lock, connection:
            row = connection.execute(
                'SELECT password_hash FROM admin WHERE id=1'
            ).fetchone()
            if row is None:
                raise AuthenticationFailed('invalid credentials')
            try:
                self._password_hasher.verify(
                    str(row['password_hash']), current_password
                )
            except (VerifyMismatchError, InvalidHashError):
                raise AuthenticationFailed('invalid credentials') from None
            self._replace_password(connection, new_password, now, 'password_changed')

    def reset_password(self, new_password: str) -> None:
        self._validate_password(new_password)
        connection = self._require_open()
        now = int(self._clock())
        with self._lock, connection:
            if connection.execute('SELECT 1 FROM admin WHERE id=1').fetchone() is None:
                raise AuthenticationFailed('administrator is not initialized')
            self._replace_password(connection, new_password, now, 'password_reset')

    @staticmethod
    def _create_schema(connection: sqlite3.Connection) -> None:
        connection.executescript(
            '''
            CREATE TABLE IF NOT EXISTS auth_metadata (
                key TEXT PRIMARY KEY,
                value BLOB NOT NULL
            );
            CREATE TABLE IF NOT EXISTS admin (
                id INTEGER PRIMARY KEY CHECK (id=1),
                password_hash TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS admin_sessions (
                token_hash TEXT PRIMARY KEY,
                csrf_hash TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                last_seen_at INTEGER NOT NULL,
                expires_at INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS admin_sessions_expiry_idx
            ON admin_sessions(expires_at);
            CREATE TABLE IF NOT EXISTS login_failures (
                client_key TEXT PRIMARY KEY,
                attempts INTEGER NOT NULL,
                window_started_at INTEGER NOT NULL,
                blocked_until INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS auth_audit (
                id INTEGER PRIMARY KEY,
                event TEXT NOT NULL,
                created_at INTEGER NOT NULL
            );
            '''
        )

    @staticmethod
    def _load_or_create_media_key(connection: sqlite3.Connection) -> bytes:
        row = connection.execute(
            "SELECT value FROM auth_metadata WHERE key='media_signing_key'"
        ).fetchone()
        if row is not None:
            value = bytes(row['value'])
            if len(value) != 32:
                raise ValueError('stored media signing key is invalid')
            return value
        value = secrets.token_bytes(32)
        with connection:
            connection.execute(
                'INSERT INTO auth_metadata(key,value) VALUES(?,?)',
                ('media_signing_key', value),
            )
        return value

    def _create_session(
        self, connection: sqlite3.Connection, now: int
    ) -> SessionCredentials:
        for _ in range(3):
            session_token = secrets.token_urlsafe(32)
            csrf_token = self._csrf_token(session_token)
            expires_at = now + self._session_ttl_seconds
            try:
                connection.execute(
                    'INSERT INTO admin_sessions('
                    'token_hash,csrf_hash,created_at,last_seen_at,expires_at'
                    ') VALUES(?,?,?,?,?)',
                    (
                        self._token_hash(session_token),
                        self._token_hash(csrf_token),
                        now,
                        now,
                        expires_at,
                    ),
                )
            except sqlite3.IntegrityError:
                continue
            return SessionCredentials(session_token, csrf_token, expires_at)
        raise RuntimeError('could not allocate a unique administrator session')

    def _ensure_not_rate_limited(
        self, connection: sqlite3.Connection, client_key: str, now: int
    ) -> None:
        row = connection.execute(
            'SELECT blocked_until FROM login_failures WHERE client_key=?', (client_key,)
        ).fetchone()
        if row is not None and int(row['blocked_until']) > now:
            raise AuthenticationRateLimited(int(row['blocked_until']) - now)

    def _record_failed_login(
        self, connection: sqlite3.Connection, client_key: str, now: int, *, scope: str
    ) -> Optional[int]:
        row = connection.execute(
            'SELECT attempts,window_started_at FROM login_failures '
            'WHERE client_key=?',
            (client_key,),
        ).fetchone()
        if (
            row is None
            or now - int(row['window_started_at']) >= self._failure_window_seconds
        ):
            attempts = 1
            window_started_at = now
        else:
            attempts = int(row['attempts']) + 1
            window_started_at = int(row['window_started_at'])
        blocked_until = (
            now + self._lockout_seconds if attempts >= self._max_failed_attempts else 0
        )
        connection.execute(
            'INSERT INTO login_failures('
            'client_key,attempts,window_started_at,blocked_until'
            ') VALUES(?,?,?,?) ON CONFLICT(client_key) DO UPDATE SET '
            'attempts=excluded.attempts,'
            'window_started_at=excluded.window_started_at,'
            'blocked_until=excluded.blocked_until',
            (client_key, attempts, window_started_at, blocked_until),
        )
        if blocked_until:
            self._audit(connection, '{}_rate_limited'.format(scope), now)
            return self._lockout_seconds
        self._audit(connection, '{}_failed'.format(scope), now)
        return None

    def _replace_password(
        self, connection: sqlite3.Connection, password: str, now: int, event: str
    ) -> None:
        connection.execute(
            'UPDATE admin SET password_hash=?,updated_at=? WHERE id=1',
            (self._password_hasher.hash(password), now),
        )
        connection.execute('DELETE FROM admin_sessions')
        connection.execute('DELETE FROM login_failures')
        self._audit(connection, event, now)

    def _csrf_token(self, session_token: str) -> str:
        digest = hmac.new(
            self.media_signing_key,
            b'csrf:' + session_token.encode('utf8'),
            hashlib.sha256,
        ).hexdigest()
        return digest

    @staticmethod
    def _token_hash(value: str) -> str:
        return hashlib.sha256(value.encode('utf8')).hexdigest()

    @staticmethod
    def _audit(connection: sqlite3.Connection, event: str, now: int) -> None:
        connection.execute(
            'INSERT INTO auth_audit(event,created_at) VALUES(?,?)', (event, now)
        )

    @staticmethod
    def _validate_password(password: str) -> None:
        if not isinstance(password, str) or not 10 <= len(password) <= 1024:
            raise ValueError('password must contain 10 to 1024 characters')

    @staticmethod
    def _validate_username(username: str) -> None:
        if (
            not isinstance(username, str)
            or not 1 <= len(username) <= 64
            or username != username.strip()
            or any(not char.isprintable() for char in username)
        ):
            raise ValueError(
                'administrator username must contain 1 to 64 visible characters'
            )

    def _username_matches(self, username: str) -> bool:
        if not isinstance(username, str):
            return False
        return secrets.compare_digest(
            username.encode('utf8'), self._admin_username.encode('utf8')
        )

    @staticmethod
    def _rate_limit_key(scope: str, client_key: str) -> str:
        normalized_client = (client_key or 'unknown')[:200]
        return '{}:{}'.format(scope, normalized_client)

    def _require_open(self) -> sqlite3.Connection:
        if self._connection is None:
            raise RuntimeError('auth store is not open')
        return self._connection
