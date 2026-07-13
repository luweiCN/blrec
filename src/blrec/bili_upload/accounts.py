from __future__ import annotations

import asyncio
import hashlib
import secrets
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import (
    Any,
    AsyncIterator,
    Awaitable,
    Callable,
    Dict,
    List,
    Mapping,
    Optional,
)

from loguru import logger

from .credentials import CredentialStore
from .crypto import CookieRecord, CredentialBundle, CredentialCipher
from .database import BiliUploadDatabase
from .errors import BiliApiError, DefinitelyNotSent, RemoteOutcomeUnknown

__all__ = (
    'AccountIdentityMismatch',
    'AccountManager',
    'AccountNotFound',
    'AccountPaused',
    'AccountView',
    'AccountWriteGate',
    'CredentialVersionChanged',
    'QrSessionForbidden',
    'QrSessionNotFound',
    'QrSessionView',
    'RenewalCheckResult',
)


class AccountIdentityMismatch(RuntimeError):
    pass


class AccountNotFound(RuntimeError):
    pass


class AccountPaused(RuntimeError):
    pass


class CredentialVersionChanged(RuntimeError):
    pass


class QrSessionNotFound(RuntimeError):
    pass


class QrSessionForbidden(RuntimeError):
    pass


@dataclass(frozen=True)
class AccountView:
    id: int
    uid: int
    display_name: str
    avatar_url: str
    credential_version: int
    credential_expires_at: int
    created_at: int
    state: str


@dataclass(frozen=True)
class RenewalCheckResult:
    credential_version: int
    refreshed: bool


@dataclass(frozen=True)
class _IdentityView:
    display_name: str
    avatar_url: str
    refresh_requested: bool


@dataclass(frozen=True, repr=False)
class QrSessionView:
    id: str
    state: str
    qr_url: Optional[str]
    expires_at: int
    poller_id: Optional[str]
    account_id: Optional[int] = None

    def __repr__(self) -> str:
        return '<QrSessionView id={!r} state={!r}>'.format(self.id, self.state)


@dataclass
class _QrRuntime:
    id: str
    manager_subject: str
    raw_auth_code: str
    qr_url: Optional[str]
    expires_at: int
    poller_id: str
    app_device_id: str
    state: str = 'created'
    account_id: Optional[int] = None
    task: Optional[asyncio.Task[Any]] = None


class _PerAccountGate:
    def __init__(
        self, database: BiliUploadDatabase, account_id: int, lock: asyncio.Lock
    ) -> None:
        self._database = database
        self._account_id = account_id
        self._lock = lock

    @asynccontextmanager
    async def hold(self, expected_credential_version: int) -> AsyncIterator[None]:
        async with self._lock:
            row = await self._database.fetchone(
                'SELECT state,credential_version FROM bili_accounts WHERE id=?',
                (self._account_id,),
            )
            if row is None:
                raise AccountNotFound('Bilibili account not found')
            if str(row['state']) != 'active':
                raise AccountPaused('Bilibili account writes are paused')
            if int(row['credential_version']) != expected_credential_version:
                raise CredentialVersionChanged('credential version changed')
            yield


class AccountWriteGate:
    def __init__(self, database: BiliUploadDatabase) -> None:
        self._database = database
        self._locks: Dict[int, asyncio.Lock] = {}

    def for_account(self, account_id: int) -> _PerAccountGate:
        lock = self._locks.setdefault(account_id, asyncio.Lock())
        return _PerAccountGate(self._database, account_id, lock)


class AccountManager:
    _NONTERMINAL_QR_STATES = ('created', 'pending', 'scanned')
    _TERMINAL_QR_STATES = ('confirmed', 'expired', 'cancelled', 'failed')
    _REFRESH_WINDOW_SECONDS = 72 * 3600

    def __init__(
        self,
        protocol: Any,
        store: CredentialStore,
        *,
        database: BiliUploadDatabase,
        cipher: CredentialCipher,
        clock: Callable[[], float] = time.time,
        sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
        qr_ttl_seconds: int = 180,
        poll_interval_seconds: float = 1,
        write_gates: Optional[AccountWriteGate] = None,
    ) -> None:
        self._protocol = protocol
        self._store = store
        self._database = database
        self._cipher = cipher
        self._clock = clock
        self._sleeper = sleeper
        self._qr_ttl_seconds = qr_ttl_seconds
        self._poll_interval_seconds = poll_interval_seconds
        self._write_gates = write_gates or AccountWriteGate(database)
        self._runtimes: Dict[str, _QrRuntime] = {}
        self._started = False
        self._closed = False
        self._start_lock = asyncio.Lock()
        self._account_create_lock = asyncio.Lock()
        self._health_lock = asyncio.Lock()
        self._last_health_check_day: Optional[int] = None

    async def start(self) -> None:
        async with self._start_lock:
            if self._started:
                return
            now = int(self._clock())
            await self._database.execute(
                "UPDATE qr_sessions SET state='cancelled',updated_at=? "
                "WHERE state IN ('created','pending','scanned')",
                (now,),
            )
            self._started = True

    async def close(self) -> None:
        self._closed = True
        tasks = []
        for runtime in self._runtimes.values():
            if runtime.task is not None and not runtime.task.done():
                runtime.task.cancel()
                tasks.append(runtime.task)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def create_qr(self, *, manager_subject: str) -> QrSessionView:
        if not manager_subject:
            raise QrSessionForbidden('manager subject is required')
        if self._closed:
            raise RuntimeError('account manager is closed')
        await self.start()
        response = await self._protocol.create_qr({})
        data = self._response_data(response, 'QR create')
        auth_code = self._required_text(data, 'auth_code', 'QR create')
        qr_url = self._required_text(data, 'url', 'QR create')
        now = int(self._clock())
        session_id = uuid.uuid4().hex
        runtime = _QrRuntime(
            id=session_id,
            manager_subject=manager_subject,
            raw_auth_code=auth_code,
            qr_url=qr_url,
            expires_at=now + self._qr_ttl_seconds,
            poller_id=uuid.uuid4().hex,
            app_device_id=secrets.token_hex(16),
        )
        await self._database.execute(
            'INSERT INTO qr_sessions('
            'id,manager_subject,auth_code_hash,state,expires_at,created_at,updated_at'
            ') VALUES(?,?,?,?,?,?,?)',
            (
                session_id,
                manager_subject,
                hashlib.sha256(auth_code.encode('utf8')).hexdigest(),
                runtime.state,
                runtime.expires_at,
                now,
                now,
            ),
        )
        self._runtimes[session_id] = runtime
        runtime.task = asyncio.create_task(self._poll(runtime))
        await asyncio.sleep(0)
        return self._runtime_view(runtime)

    async def status(self, session_id: str, *, manager_subject: str) -> QrSessionView:
        await self.start()
        runtime = self._runtimes.get(session_id)
        if runtime is not None:
            self._authorize(runtime.manager_subject, manager_subject)
            if (
                runtime.state not in self._TERMINAL_QR_STATES
                and int(self._clock()) >= runtime.expires_at
            ):
                await self._transition(runtime, 'expired')
                self._cancel_runtime_task(runtime)
            return self._runtime_view(runtime)
        row = await self._database.fetchone(
            'SELECT manager_subject,state,expires_at FROM qr_sessions WHERE id=?',
            (session_id,),
        )
        if row is None:
            raise QrSessionNotFound('QR session not found')
        self._authorize(str(row['manager_subject']), manager_subject)
        return QrSessionView(
            id=session_id,
            state=str(row['state']),
            qr_url=None,
            expires_at=int(row['expires_at']),
            poller_id=None,
        )

    async def cancel(self, session_id: str, *, manager_subject: str) -> QrSessionView:
        view = await self.status(session_id, manager_subject=manager_subject)
        runtime = self._runtimes.get(session_id)
        if runtime is None or view.state in self._TERMINAL_QR_STATES:
            return view
        await self._transition(runtime, 'cancelled')
        self._cancel_runtime_task(runtime)
        return self._runtime_view(runtime)

    async def finish_confirmed_login(
        self,
        response: Mapping[str, Any],
        *,
        app_device_id: Optional[str] = None,
        previous_bundle: Optional[CredentialBundle] = None,
        account_id: Optional[int] = None,
    ) -> AccountView:
        bundle = self._build_bundle(
            response, app_device_id=app_device_id, previous_bundle=previous_bundle
        )
        try:
            identity = await self._validate_identity(bundle)
        except DefinitelyNotSent:
            identity = await self._validate_identity(bundle)
        async with self._account_create_lock:
            if account_id is None:
                row = await self._database.fetchone(
                    'SELECT id FROM bili_accounts WHERE uid=?', (bundle.mid,)
                )
                if row is None:
                    value = await self._database.scalar(
                        'SELECT COALESCE(MAX(id),0)+1 FROM bili_accounts'
                    )
                    account_id = int(value)
                else:
                    account_id = int(row['id'])
            version = await self._store.put(
                account_id=account_id,
                account_uid=bundle.mid,
                display_name=identity.display_name,
                avatar_url=identity.avatar_url,
                bundle=bundle,
                cipher=self._cipher,
                now=int(self._clock()),
            )
        row = await self._account_row(account_id)
        assert int(row['credential_version']) == version
        return self._account_view(row)

    async def list_accounts(self) -> List[AccountView]:
        rows = await self._database.fetchall(
            'SELECT id,uid,display_name,avatar_url,credential_version,'
            'credential_expires_at,created_at,state '
            'FROM bili_accounts ORDER BY id'
        )
        return [self._account_view(row) for row in rows]

    async def refresh_account(self, account_id: int) -> int:
        row = await self._account_row(account_id)
        version = int(row['credential_version'])
        gate = self._write_gates.for_account(account_id)
        async with gate.hold(version):
            previous = await self._store.get(account_id=account_id, cipher=self._cipher)
            return await self._refresh_locked(account_id, row, previous)

    async def check_account_renewal(self, account_id: int) -> RenewalCheckResult:
        row = await self._account_row(account_id)
        version = int(row['credential_version'])
        gate = self._write_gates.for_account(account_id)
        async with gate.hold(version):
            bundle = await self._store.get(account_id=account_id, cipher=self._cipher)
            try:
                identity = await self._validate_identity(bundle)
            except DefinitelyNotSent:
                identity = await self._validate_identity(bundle)
            due = bundle.expires_at - int(self._clock()) < self._REFRESH_WINDOW_SECONDS
            if due or identity.refresh_requested:
                refreshed_version = await self._refresh_locked(account_id, row, bundle)
                return RenewalCheckResult(
                    credential_version=refreshed_version, refreshed=True
                )
            await self._store.update_metadata(
                account_id=account_id,
                account_uid=bundle.mid,
                display_name=identity.display_name,
                avatar_url=identity.avatar_url,
                credential_expires_at=bundle.expires_at,
                now=int(self._clock()),
            )
            return RenewalCheckResult(credential_version=version, refreshed=False)

    async def refresh_due_accounts(self) -> List[int]:
        day = int(self._clock()) // 86400
        async with self._health_lock:
            if self._last_health_check_day == day:
                return []
            self._last_health_check_day = day
        refreshed = []
        rows = await self._database.fetchall(
            "SELECT id FROM bili_accounts WHERE state='active' ORDER BY id"
        )
        for row in rows:
            account_id = int(row['id'])
            try:
                result = await self.check_account_renewal(account_id)
                if result.refreshed:
                    refreshed.append(account_id)
            except (AccountPaused, CredentialVersionChanged, AccountNotFound):
                continue
        return refreshed

    async def _refresh_locked(
        self, account_id: int, row: Mapping[str, Any], previous: CredentialBundle
    ) -> int:
        try:
            try:
                response = await self._protocol.refresh_token(previous)
            except DefinitelyNotSent:
                response = await self._protocol.refresh_token(previous)
        except (DefinitelyNotSent, RemoteOutcomeUnknown):
            await self._mark_refresh_unknown(account_id)
            raise
        try:
            account = await self.finish_confirmed_login(
                response, previous_bundle=previous, account_id=account_id
            )
        except (DefinitelyNotSent, RemoteOutcomeUnknown):
            await self._mark_refresh_unknown(account_id)
            raise
        if account.uid != int(row['uid']):
            raise AccountIdentityMismatch('refreshed credential uid differs')
        return account.credential_version

    async def _poll(self, runtime: _QrRuntime) -> None:
        stage = 'poll'
        try:
            await self._transition(runtime, 'pending')
            while runtime.state not in self._TERMINAL_QR_STATES:
                if int(self._clock()) >= runtime.expires_at:
                    await self._transition(runtime, 'expired')
                    return
                response = await self._protocol.poll_qr(
                    {'auth_code': runtime.raw_auth_code}
                )
                code = response.get('code')
                data = response.get('data')
                status = data.get('status') if isinstance(data, Mapping) else None
                if code == 86038:
                    await self._transition(runtime, 'expired')
                    return
                if status == 'expired':
                    await self._transition(runtime, 'expired')
                    return
                if code in (86090, 86101) or status == 'scanned':
                    await self._transition(runtime, 'scanned')
                elif code == 0 and isinstance(data, Mapping) and data.get('token_info'):
                    await self._transition(runtime, 'scanned')
                    stage = 'credential_validation'
                    account = await self.finish_confirmed_login(
                        response, app_device_id=runtime.app_device_id
                    )
                    runtime.account_id = account.id
                    await self._transition(runtime, 'confirmed')
                    return
                elif code == 86039 or (code == 0 and status == 'pending'):
                    pass
                else:
                    await self._transition(runtime, 'failed')
                    return
                await self._sleeper(self._poll_interval_seconds)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            error_code = error.code if isinstance(error, BiliApiError) else None
            error_stage = (
                error.operation
                if isinstance(error, BiliApiError) and error.operation
                else stage
            )
            logger.error(
                'Bilibili QR login failed: stage={}, error_type={}, error_code={}',
                error_stage,
                type(error).__name__,
                error_code,
            )
            if runtime.state not in self._TERMINAL_QR_STATES:
                await self._transition(runtime, 'failed')

    async def _transition(self, runtime: _QrRuntime, state: str) -> None:
        current = runtime.state
        if current in self._TERMINAL_QR_STATES:
            return
        if state == 'pending' and current == 'scanned':
            return
        if state == 'confirmed' and current != 'scanned':
            raise RuntimeError('QR confirmation requires scanned state')
        if state == 'scanned' and current == 'created':
            await self._transition(runtime, 'pending')
        runtime.state = state
        now = int(self._clock())
        await self._database.execute(
            'UPDATE qr_sessions SET state=?,updated_at=? WHERE id=?',
            (state, now, runtime.id),
        )
        if state in self._TERMINAL_QR_STATES:
            runtime.raw_auth_code = ''
            runtime.qr_url = None

    async def _validate_identity(self, bundle: CredentialBundle) -> _IdentityView:
        cookie_values = [
            cookie.value for cookie in bundle.cookies if cookie.name == 'DedeUserID'
        ]
        if len(cookie_values) != 1:
            raise AccountIdentityMismatch('token, cookie, and account uid differ')
        try:
            cookie_uid = int(cookie_values[0])
        except ValueError:
            raise AccountIdentityMismatch(
                'token, cookie, and account uid differ'
            ) from None
        oauth = self._response_data(
            await self._protocol.oauth_info(bundle), 'OAuth info'
        )
        nav = self._response_data(await self._protocol.web_nav(bundle), 'Web nav')
        try:
            token_uid = int(oauth['mid'])
            queried_uid = int(nav['mid'])
        except (KeyError, TypeError, ValueError):
            raise AccountIdentityMismatch(
                'token, cookie, and account uid differ'
            ) from None
        if not bool(nav.get('isLogin', True)) or not (
            token_uid == cookie_uid == queried_uid == bundle.mid
        ):
            raise AccountIdentityMismatch('token, cookie, and account uid differ')
        display_name = nav.get('uname')
        avatar_url = nav.get('face')
        return _IdentityView(
            display_name=str(display_name) if display_name else str(bundle.mid),
            avatar_url=str(avatar_url) if avatar_url else '',
            refresh_requested=bool(oauth.get('refresh')),
        )

    def _build_bundle(
        self,
        response: Mapping[str, Any],
        *,
        app_device_id: Optional[str],
        previous_bundle: Optional[CredentialBundle],
    ) -> CredentialBundle:
        data = self._response_data(response, 'credential')
        token_info = data.get('token_info')
        cookie_info = data.get('cookie_info')
        if not isinstance(token_info, Mapping) or not isinstance(cookie_info, Mapping):
            raise AccountIdentityMismatch('credential response is incomplete')
        raw_cookies = cookie_info.get('cookies')
        if not isinstance(raw_cookies, list):
            raise AccountIdentityMismatch('credential response is incomplete')
        cookies = tuple(self._cookie_record(cookie) for cookie in raw_cookies)
        try:
            mid = int(token_info['mid'])
            expires_in = int(token_info['expires_in'])
        except (KeyError, TypeError, ValueError):
            raise AccountIdentityMismatch('credential response is incomplete') from None
        access_token = self._required_text(
            token_info, 'access_token', 'credential token'
        )
        refresh_token = self._required_text(
            token_info, 'refresh_token', 'credential token'
        )
        now = int(self._clock())
        cookie_map = {cookie.name: cookie.value for cookie in cookies}
        previous = previous_bundle
        return CredentialBundle(
            access_token=access_token,
            refresh_token=refresh_token,
            mid=mid,
            issued_at=now,
            expires_at=now + expires_in,
            signing_family='BiliTV',
            app_client_version=(
                previous.app_client_version if previous is not None else 'BiliTV'
            ),
            web_client_version=(
                previous.web_client_version if previous is not None else 'web'
            ),
            app_device_source=(
                previous.app_device_source if previous is not None else 'qr_session'
            ),
            web_device_source=(
                previous.web_device_source if previous is not None else 'qr_cookie_info'
            ),
            app_device_id=(
                previous.app_device_id
                if previous is not None
                else app_device_id or secrets.token_hex(16)
            ),
            app_buvid=previous.app_buvid if previous is not None else '',
            web_buvid3=cookie_map.get(
                'buvid3', previous.web_buvid3 if previous is not None else ''
            ),
            web_buvid4=cookie_map.get(
                'buvid4', previous.web_buvid4 if previous is not None else ''
            ),
            web_b_nut=cookie_map.get(
                'b_nut', previous.web_b_nut if previous is not None else ''
            ),
            cookies=cookies,
        )

    @staticmethod
    def _cookie_record(value: Any) -> CookieRecord:
        if not isinstance(value, Mapping):
            raise AccountIdentityMismatch('credential cookie is invalid')
        name = AccountManager._required_text(value, 'name', 'credential cookie')
        cookie_value = AccountManager._required_text(
            value, 'value', 'credential cookie'
        )
        expires_value = value.get('expires_at', value.get('expires'))
        try:
            expires_at = None if expires_value in (None, 0, '0') else int(expires_value)
        except (TypeError, ValueError):
            raise AccountIdentityMismatch('credential cookie is invalid') from None
        return CookieRecord(
            name=name,
            value=cookie_value,
            domain=str(value.get('domain') or '.bilibili.com'),
            path=str(value.get('path') or '/'),
            expires_at=expires_at,
            secure=bool(value.get('secure', True)),
            http_only=bool(value.get('http_only', value.get('httpOnly', False))),
        )

    async def _account_row(self, account_id: int) -> Mapping[str, Any]:
        row = await self._database.fetchone(
            'SELECT id,uid,display_name,avatar_url,credential_version,'
            'credential_expires_at,created_at,state '
            'FROM bili_accounts WHERE id=?',
            (account_id,),
        )
        if row is None:
            raise AccountNotFound('Bilibili account not found')
        return dict(row)

    @staticmethod
    def _account_view(row: Mapping[str, Any]) -> AccountView:
        return AccountView(
            id=int(row['id']),
            uid=int(row['uid']),
            display_name=str(row['display_name']),
            avatar_url=str(row['avatar_url']),
            credential_version=int(row['credential_version']),
            credential_expires_at=int(row['credential_expires_at']),
            created_at=int(row['created_at']),
            state=str(row['state']),
        )

    async def _mark_refresh_unknown(self, account_id: int) -> None:
        await self._database.execute(
            "UPDATE bili_accounts SET state='refresh_unknown',"
            "pause_reason='refresh outcome unknown',updated_at=? WHERE id=?",
            (int(self._clock()), account_id),
        )

    @staticmethod
    def _response_data(response: Mapping[str, Any], context: str) -> Mapping[str, Any]:
        data = response.get('data')
        if not isinstance(data, Mapping):
            raise AccountIdentityMismatch('{} response is incomplete'.format(context))
        return data

    @staticmethod
    def _required_text(values: Mapping[str, Any], field: str, context: str) -> str:
        value = values.get(field)
        if not isinstance(value, str) or not value:
            raise AccountIdentityMismatch('{} response is incomplete'.format(context))
        return value

    @staticmethod
    def _authorize(expected: str, actual: str) -> None:
        if not actual or expected != actual:
            raise QrSessionForbidden('QR session belongs to another manager')

    @staticmethod
    def _runtime_view(runtime: _QrRuntime) -> QrSessionView:
        return QrSessionView(
            id=runtime.id,
            state=runtime.state,
            qr_url=runtime.qr_url,
            expires_at=runtime.expires_at,
            poller_id=runtime.poller_id,
            account_id=runtime.account_id,
        )

    @staticmethod
    def _cancel_runtime_task(runtime: _QrRuntime) -> None:
        task = runtime.task
        if task is not None and task is not asyncio.current_task() and not task.done():
            task.cancel()
