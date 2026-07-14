import asyncio
from pathlib import Path
from typing import Any, List, Mapping, Optional
from unittest.mock import AsyncMock, Mock

import pytest

import blrec.bili_upload.accounts as accounts_module
from blrec.bili_upload.account_lifecycle import AccountRemovalCommand, RemovalMode
from blrec.bili_upload.accounts import (
    AccountIdentityMismatch,
    AccountManager,
    AccountNotFound,
    AccountPaused,
    AccountWriteGate,
    CredentialVersionChanged,
    QrSessionForbidden,
)
from blrec.bili_upload.credentials import CredentialStore
from blrec.bili_upload.crypto import CookieRecord, CredentialBundle, CredentialCipher
from blrec.bili_upload.database import BiliUploadDatabase
from blrec.bili_upload.errors import (
    BiliApiError,
    DefinitelyNotSent,
    RemoteOutcomeUnknown,
)


class FakeClock:
    def __init__(self, value: int = 1_000_000) -> None:
        self.value = value

    def __call__(self) -> float:
        return float(self.value)

    def advance(self, seconds: int) -> None:
        self.value += seconds


def confirmed_response(
    *,
    mid: int = 42,
    cookie_uid: int = 42,
    access_token: str = 'access-new',
    refresh_token: str = 'refresh-new',
    expires_in: int = 180 * 24 * 3600,
    sessdata: str = 'sess-secret',
) -> Mapping[str, Any]:
    return {
        'code': 0,
        'data': {
            'token_info': {
                'access_token': access_token,
                'refresh_token': refresh_token,
                'mid': mid,
                'expires_in': expires_in,
            },
            'cookie_info': {
                'cookies': [
                    {'name': 'DedeUserID', 'value': str(cookie_uid)},
                    {'name': 'SESSDATA', 'value': sessdata, 'http_only': 1},
                    {'name': 'bili_jct', 'value': 'csrf-secret'},
                    {'name': 'buvid3', 'value': 'web-buvid3'},
                ]
            },
            'sso': ['bilibili.com'],
        },
    }


def stored_bundle(*, expires_at: int, suffix: str = 'old') -> CredentialBundle:
    return CredentialBundle(
        access_token='access-' + suffix,
        refresh_token='refresh-' + suffix,
        mid=42,
        issued_at=100,
        expires_at=expires_at,
        signing_family='BiliTV',
        app_client_version='BiliTV',
        web_client_version='web',
        app_device_source='qr_session',
        web_device_source='qr_cookie_info',
        app_device_id='app-device',
        app_buvid='',
        web_buvid3='web-buvid3',
        web_buvid4='',
        web_b_nut='',
        cookies=(
            CookieRecord(
                name='DedeUserID',
                value='42',
                domain='.bilibili.com',
                path='/',
                expires_at=None,
                secure=True,
                http_only=False,
            ),
            CookieRecord(
                name='SESSDATA',
                value='sess-secret',
                domain='.bilibili.com',
                path='/',
                expires_at=None,
                secure=True,
                http_only=True,
            ),
            CookieRecord(
                name='bili_jct',
                value='csrf-secret',
                domain='.bilibili.com',
                path='/',
                expires_at=None,
                secure=True,
                http_only=False,
            ),
        ),
    )


class ScriptedProtocol:
    def __init__(
        self,
        *,
        token_mid: int = 42,
        web_uid: int = 42,
        avatar_url: str = 'https://i0.hdslb.com/face.jpg',
        poll_results: Optional[List[Mapping[str, Any]]] = None,
        refresh_results: Optional[List[Any]] = None,
        oauth_results: Optional[List[Any]] = None,
    ) -> None:
        self.token_mid = token_mid
        self.web_uid = web_uid
        self.avatar_url = avatar_url
        self.poll_results = list(poll_results or [])
        self.refresh_results = list(refresh_results or [])
        self.oauth_results = list(oauth_results or [])
        self.poll_calls = 0
        self.concurrent_pollers = 0
        self.max_concurrent_pollers = 0
        self.oauth_calls = 0
        self.web_nav_calls = 0
        self.refresh_calls = 0

    async def create_qr(self, _params: Mapping[str, Any]) -> Mapping[str, Any]:
        return {
            'code': 0,
            'data': {
                'auth_code': 'raw-auth-code',
                'url': 'https://passport.example.invalid/qr',
            },
        }

    async def poll_qr(self, _params: Mapping[str, Any]) -> Mapping[str, Any]:
        self.poll_calls += 1
        self.concurrent_pollers += 1
        self.max_concurrent_pollers = max(
            self.max_concurrent_pollers, self.concurrent_pollers
        )
        try:
            await asyncio.sleep(0)
            if self.poll_results:
                return self.poll_results.pop(0)
            return {'code': 86039, 'message': 'pending'}
        finally:
            self.concurrent_pollers -= 1

    async def oauth_info(self, _bundle: CredentialBundle) -> Mapping[str, Any]:
        self.oauth_calls += 1
        if self.oauth_results:
            result = self.oauth_results.pop(0)
            if isinstance(result, BaseException):
                raise result
            return result
        return {'code': 0, 'data': {'mid': self.token_mid, 'refresh': False}}

    async def web_nav(self, _bundle: CredentialBundle) -> Mapping[str, Any]:
        self.web_nav_calls += 1
        return {
            'code': 0,
            'data': {
                'isLogin': True,
                'mid': self.web_uid,
                'uname': 'fixture',
                'face': self.avatar_url,
            },
        }

    async def refresh_token(self, _bundle: CredentialBundle) -> Mapping[str, Any]:
        self.refresh_calls += 1
        result = (
            self.refresh_results.pop(0)
            if self.refresh_results
            else confirmed_response()
        )
        if isinstance(result, BaseException):
            raise result
        return result


async def never_wake(_seconds: float) -> None:
    await asyncio.Event().wait()


async def components(
    tmp_path: Path,
    protocol: ScriptedProtocol,
    clock: FakeClock,
    *,
    on_primary_credential_changed: Optional[Any] = None,
):
    database = BiliUploadDatabase(str(tmp_path / 'blrec.sqlite3'))
    await database.open()
    store = CredentialStore(database)
    cipher = CredentialCipher({'current': b'k' * 32}, current_key_id='current')
    manager = AccountManager(
        protocol,
        store,
        database=database,
        cipher=cipher,
        clock=clock,
        sleeper=never_wake,
        on_primary_credential_changed=on_primary_credential_changed,
    )
    return database, store, cipher, manager


@pytest.mark.asyncio
async def test_one_poller_expires_after_180_seconds(tmp_path: Path) -> None:
    clock = FakeClock()
    protocol = ScriptedProtocol()
    database, _store, _cipher, manager = await components(tmp_path, protocol, clock)
    try:
        session = await manager.create_qr(manager_subject='admin')
        for _ in range(100):
            if protocol.poll_calls:
                break
            await asyncio.sleep(0.01)
        duplicate = await manager.status(session.id, manager_subject='admin')
        clock.advance(181)
        expired = await manager.status(session.id, manager_subject='admin')
        row = await database.fetchone(
            'SELECT auth_code_hash,state FROM qr_sessions WHERE id=?', (session.id,)
        )

        assert duplicate.poller_id == session.poller_id
        assert expired.state == 'expired'
        assert protocol.max_concurrent_pollers == 1
        assert row is not None
        assert row['auth_code_hash'] != 'raw-auth-code'
        assert row['state'] == 'expired'
    finally:
        await manager.close()
        await database.close()


@pytest.mark.asyncio
async def test_qr_session_is_bound_to_its_manager_subject(tmp_path: Path) -> None:
    clock = FakeClock()
    protocol = ScriptedProtocol()
    database, _store, _cipher, manager = await components(tmp_path, protocol, clock)
    try:
        session = await manager.create_qr(manager_subject='admin-a')

        with pytest.raises(QrSessionForbidden):
            await manager.status(session.id, manager_subject='admin-b')
        with pytest.raises(QrSessionForbidden):
            await manager.cancel(session.id, manager_subject='admin-b')
    finally:
        await manager.close()
        await database.close()


@pytest.mark.asyncio
async def test_unknown_qr_code_fails_without_repeated_polling(tmp_path: Path) -> None:
    clock = FakeClock()
    protocol = ScriptedProtocol(poll_results=[{'code': 99999, 'message': 'unknown'}])
    database, _store, _cipher, manager = await components(tmp_path, protocol, clock)
    try:
        session = await manager.create_qr(manager_subject='admin')
        for _ in range(100):
            current = await manager.status(session.id, manager_subject='admin')
            if current.state == 'failed':
                break
            await asyncio.sleep(0.01)

        assert current.state == 'failed'
        assert protocol.poll_calls == 1
    finally:
        await manager.close()
        await database.close()


@pytest.mark.asyncio
async def test_qr_confirmation_logs_safe_upstream_failure_details(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = FakeClock()
    protocol = ScriptedProtocol(
        poll_results=[confirmed_response()],
        oauth_results=[BiliApiError(412, operation='oauth_info')],
    )
    database, _store, _cipher, manager = await components(tmp_path, protocol, clock)
    fake_logger = Mock()
    monkeypatch.setattr(accounts_module, 'logger', fake_logger, raising=False)
    try:
        session = await manager.create_qr(manager_subject='admin')
        for _ in range(100):
            current = await manager.status(session.id, manager_subject='admin')
            if current.state == 'failed':
                break
            await asyncio.sleep(0.01)

        assert current.state == 'failed'
        fake_logger.error.assert_called_once_with(
            'Bilibili QR login failed: stage={}, error_type={}, error_code={}',
            'oauth_info',
            'BiliApiError',
            412,
        )
        assert 'access-new' not in repr(fake_logger.error.call_args)
        assert 'sess-secret' not in repr(fake_logger.error.call_args)
    finally:
        await manager.close()
        await database.close()


@pytest.mark.asyncio
async def test_startup_cancels_qr_rows_whose_raw_code_was_lost(tmp_path: Path) -> None:
    clock = FakeClock()
    protocol = ScriptedProtocol()
    database, _store, _cipher, manager = await components(tmp_path, protocol, clock)
    try:
        await database.execute(
            'INSERT INTO qr_sessions('
            'id,manager_subject,auth_code_hash,state,expires_at,created_at,updated_at'
            ") VALUES('old','admin','hash','pending',2000000,1,1)"
        )

        await manager.start()

        assert (
            await database.scalar("SELECT state FROM qr_sessions WHERE id='old'")
            == 'cancelled'
        )
    finally:
        await manager.close()
        await database.close()


@pytest.mark.asyncio
async def test_confirm_rejects_mismatched_uid_without_saving(tmp_path: Path) -> None:
    clock = FakeClock()
    protocol = ScriptedProtocol(token_mid=42, web_uid=42)
    database, _store, _cipher, manager = await components(tmp_path, protocol, clock)
    try:
        with pytest.raises(AccountIdentityMismatch):
            await manager.finish_confirmed_login(
                confirmed_response(mid=42, cookie_uid=43)
            )

        assert await database.scalar('SELECT COUNT(*) FROM bili_accounts') == 0
    finally:
        await manager.close()
        await database.close()


@pytest.mark.asyncio
async def test_confirm_saves_one_validated_credential_bundle(tmp_path: Path) -> None:
    clock = FakeClock()
    protocol = ScriptedProtocol()
    database, store, cipher, manager = await components(tmp_path, protocol, clock)
    try:
        account = await manager.finish_confirmed_login(confirmed_response())
        bundle = await store.get(account_id=account.id, cipher=cipher)
        row = await database.fetchone(
            'SELECT uid,avatar_url,credential_version,credential_expires_at,'
            'created_at,state FROM bili_accounts WHERE id=?',
            (account.id,),
        )

        assert row is not None
        assert dict(row) == {
            'uid': 42,
            'avatar_url': 'https://i0.hdslb.com/face.jpg',
            'credential_version': 1,
            'credential_expires_at': clock.value + 180 * 24 * 3600,
            'created_at': clock.value,
            'state': 'active',
        }
        assert account.avatar_url == 'https://i0.hdslb.com/face.jpg'
        assert account.credential_expires_at == clock.value + 180 * 24 * 3600
        assert account.created_at == clock.value
        assert account.is_primary
        assert bundle.mid == 42
        assert bundle.access_token == 'access-new'
        assert bundle.refresh_token == 'refresh-new'
        assert bundle.csrf == 'csrf-secret'
        assert bundle.web_buvid3 == 'web-buvid3'
        assert protocol.oauth_calls == 1
        assert protocol.web_nav_calls == 1
    finally:
        await manager.close()
        await database.close()


@pytest.mark.asyncio
async def test_primary_account_is_sticky_and_can_be_selected_explicitly(
    tmp_path: Path,
) -> None:
    clock = FakeClock()
    protocol = ScriptedProtocol()
    database, _store, _cipher, manager = await components(tmp_path, protocol, clock)
    try:
        first = await manager.finish_confirmed_login(confirmed_response())
        protocol.token_mid = 43
        protocol.web_uid = 43
        second = await manager.finish_confirmed_login(
            confirmed_response(mid=43, cookie_uid=43)
        )

        assert first.is_primary
        assert not second.is_primary

        selected = await manager.set_primary_account(second.id)
        accounts = await manager.list_accounts()

        assert selected.id == second.id
        assert selected.is_primary
        assert [account.is_primary for account in accounts] == [False, True]
    finally:
        await manager.close()
        await database.close()


@pytest.mark.asyncio
async def test_primary_cookie_header_honours_cookie_domain_and_account_state(
    tmp_path: Path,
) -> None:
    clock = FakeClock()
    protocol = ScriptedProtocol()
    database, _store, _cipher, manager = await components(tmp_path, protocol, clock)
    try:
        assert (
            await manager.primary_cookie_header('https://api.bilibili.com/x/test') == ''
        )

        account = await manager.finish_confirmed_login(confirmed_response())

        header = await manager.primary_cookie_header(
            'https://api.live.bilibili.com/x/test'
        )
        assert 'SESSDATA=sess-secret' in header
        assert 'bili_jct=csrf-secret' in header
        assert await manager.primary_cookie_header('https://example.invalid/x') == ''

        await database.execute(
            "UPDATE bili_accounts SET state='paused' WHERE id=?", (account.id,)
        )
        assert (
            await manager.primary_cookie_header('https://api.bilibili.com/x/test') == ''
        )
    finally:
        await manager.close()
        await database.close()


@pytest.mark.asyncio
async def test_recording_cookie_falls_back_without_changing_upload_account(
    tmp_path: Path,
) -> None:
    clock = FakeClock()
    protocol = ScriptedProtocol()
    database, _store, _cipher, manager = await components(tmp_path, protocol, clock)
    try:
        primary = await manager.finish_confirmed_login(
            confirmed_response(sessdata='primary-secret')
        )
        protocol.token_mid = 43
        protocol.web_uid = 43
        standby = await manager.finish_confirmed_login(
            confirmed_response(mid=43, cookie_uid=43, sessdata='standby-secret')
        )
        await database.execute(
            "UPDATE bili_accounts SET state='paused' WHERE id=?", (primary.id,)
        )

        header = await manager.recording_cookie_header(
            'https://api.live.bilibili.com/x/test'
        )
        accounts = await manager.list_accounts()

        assert 'SESSDATA=standby-secret' in header
        assert next(
            account for account in accounts if account.id == primary.id
        ).is_primary
        assert not next(
            account for account in accounts if account.id == standby.id
        ).is_primary
    finally:
        await manager.close()
        await database.close()


@pytest.mark.asyncio
async def test_failed_standby_notifies_recording_cookie_to_use_next_account(
    tmp_path: Path,
) -> None:
    clock = FakeClock()
    protocol = ScriptedProtocol()
    changed = AsyncMock()
    database, _store, _cipher, manager = await components(
        tmp_path, protocol, clock, on_primary_credential_changed=changed
    )
    try:
        primary = await manager.finish_confirmed_login(confirmed_response())
        protocol.token_mid = 43
        protocol.web_uid = 43
        standby = await manager.finish_confirmed_login(
            confirmed_response(mid=43, cookie_uid=43, sessdata='standby-secret')
        )
        protocol.token_mid = 44
        protocol.web_uid = 44
        await manager.finish_confirmed_login(
            confirmed_response(mid=44, cookie_uid=44, sessdata='next-secret')
        )
        await database.execute(
            "UPDATE bili_accounts SET state='paused' WHERE id=?", (primary.id,)
        )
        protocol.token_mid = 43
        protocol.web_uid = 43
        protocol.oauth_results.append(BiliApiError(-101, operation='oauth_info'))
        changed.reset_mock()

        await manager.report_primary_auth_failure()

        assert (
            await database.scalar(
                'SELECT state FROM bili_accounts WHERE id=?', (standby.id,)
            )
            == 'paused'
        )
        changed.assert_awaited_once_with()
        header = await manager.recording_cookie_header('https://api.bilibili.com/')
        assert 'SESSDATA=next-secret' in header
    finally:
        await manager.close()
        await database.close()


@pytest.mark.asyncio
async def test_list_accounts_returns_only_redacted_account_metadata(
    tmp_path: Path,
) -> None:
    clock = FakeClock()
    protocol = ScriptedProtocol()
    database, _store, _cipher, manager = await components(tmp_path, protocol, clock)
    try:
        saved = await manager.finish_confirmed_login(confirmed_response())

        assert await manager.list_accounts() == [saved]
        assert 'access-new' not in repr(saved)
        assert 'refresh-new' not in repr(saved)
    finally:
        await manager.close()
        await database.close()


@pytest.mark.asyncio
async def test_removed_account_is_hidden_and_same_uid_login_reactivates_it(
    tmp_path: Path,
) -> None:
    clock = FakeClock()
    protocol = ScriptedProtocol()
    changed = AsyncMock()
    database, _store, _cipher, manager = await components(
        tmp_path, protocol, clock, on_primary_credential_changed=changed
    )
    try:
        saved = await manager.finish_confirmed_login(confirmed_response())
        changed.reset_mock()

        relationships = await manager.account_relationships(saved.id)
        removed = await manager.remove_account(
            saved.id,
            AccountRemovalCommand(RemovalMode.DISABLE),
            manager_subject='admin',
        )

        assert relationships.is_primary
        assert removed.account_id == saved.id
        assert await manager.list_accounts() == []
        changed.assert_awaited_once_with()
        with pytest.raises(AccountNotFound):
            await manager.account_relationships(999)

        restored = await manager.finish_confirmed_login(confirmed_response())

        assert restored.id == saved.id
        assert restored.state == 'active'
        assert restored.credential_version == saved.credential_version + 1
        assert await manager.list_accounts() == [restored]
    finally:
        await manager.close()
        await database.close()


async def seed_account(
    store: CredentialStore, cipher: CredentialCipher, *, expires_at: int
) -> None:
    await store.put(
        account_id=1,
        account_uid=42,
        display_name='fixture',
        bundle=stored_bundle(expires_at=expires_at),
        cipher=cipher,
        now=100,
    )


@pytest.mark.asyncio
async def test_refresh_replaces_the_whole_bundle_atomically(tmp_path: Path) -> None:
    clock = FakeClock()
    protocol = ScriptedProtocol(
        refresh_results=[
            confirmed_response(access_token='access-2', refresh_token='refresh-2')
        ]
    )
    database, store, cipher, manager = await components(tmp_path, protocol, clock)
    try:
        await seed_account(store, cipher, expires_at=clock.value + 10)

        version = await manager.refresh_account(1)
        refreshed = await store.get(account_id=1, cipher=cipher)

        assert version == 2
        assert refreshed.access_token == 'access-2'
        assert refreshed.refresh_token == 'refresh-2'
        assert protocol.refresh_calls == 1
    finally:
        await manager.close()
        await database.close()


@pytest.mark.asyncio
async def test_renewal_check_keeps_valid_credentials_and_updates_metadata(
    tmp_path: Path,
) -> None:
    clock = FakeClock()
    protocol = ScriptedProtocol()
    database, store, cipher, manager = await components(tmp_path, protocol, clock)
    try:
        expires_at = clock.value + 73 * 3600
        await seed_account(store, cipher, expires_at=expires_at)

        result = await manager.check_account_renewal(1)

        row = await database.fetchone(
            'SELECT display_name,avatar_url,credential_expires_at,'
            'credential_version FROM bili_accounts WHERE id=1'
        )
        assert result.credential_version == 1
        assert result.refreshed is False
        assert protocol.refresh_calls == 0
        assert row is not None
        assert dict(row) == {
            'display_name': 'fixture',
            'avatar_url': 'https://i0.hdslb.com/face.jpg',
            'credential_expires_at': expires_at,
            'credential_version': 1,
        }
    finally:
        await manager.close()
        await database.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('refresh_requested', (False, True))
async def test_renewal_check_refreshes_only_when_due_or_requested(
    tmp_path: Path, refresh_requested: bool
) -> None:
    clock = FakeClock()
    expires_at = clock.value + (71 if not refresh_requested else 73) * 3600
    oauth_results = (
        [{'code': 0, 'data': {'mid': 42, 'refresh': True}}] if refresh_requested else []
    )
    protocol = ScriptedProtocol(
        oauth_results=oauth_results, refresh_results=[confirmed_response()]
    )
    database, store, cipher, manager = await components(tmp_path, protocol, clock)
    try:
        await seed_account(store, cipher, expires_at=expires_at)

        result = await manager.check_account_renewal(1)

        assert result.credential_version == 2
        assert result.refreshed is True
        assert protocol.refresh_calls == 1
    finally:
        await manager.close()
        await database.close()


@pytest.mark.asyncio
async def test_refresh_retries_once_only_when_request_was_not_sent(
    tmp_path: Path,
) -> None:
    clock = FakeClock()
    protocol = ScriptedProtocol(
        refresh_results=[DefinitelyNotSent(), confirmed_response()]
    )
    database, store, cipher, manager = await components(tmp_path, protocol, clock)
    try:
        await seed_account(store, cipher, expires_at=clock.value + 10)

        await manager.refresh_account(1)

        assert protocol.refresh_calls == 2
    finally:
        await manager.close()
        await database.close()


@pytest.mark.asyncio
async def test_unknown_refresh_pauses_writes_and_preserves_ciphertext(
    tmp_path: Path,
) -> None:
    clock = FakeClock()
    protocol = ScriptedProtocol(refresh_results=[RemoteOutcomeUnknown()])
    database, store, cipher, manager = await components(tmp_path, protocol, clock)
    try:
        await seed_account(store, cipher, expires_at=clock.value + 10)
        before = await store.raw_ciphertext(account_id=1)

        with pytest.raises(RemoteOutcomeUnknown):
            await manager.refresh_account(1)

        row = await database.fetchone(
            'SELECT state,credential_version FROM bili_accounts WHERE id=1'
        )
        assert row is not None
        assert dict(row) == {'state': 'refresh_unknown', 'credential_version': 1}
        assert await store.raw_ciphertext(account_id=1) == before
    finally:
        await manager.close()
        await database.close()


@pytest.mark.asyncio
async def test_unknown_second_refresh_attempt_also_pauses_the_account(
    tmp_path: Path,
) -> None:
    clock = FakeClock()
    protocol = ScriptedProtocol(
        refresh_results=[DefinitelyNotSent(), RemoteOutcomeUnknown()]
    )
    database, store, cipher, manager = await components(tmp_path, protocol, clock)
    try:
        await seed_account(store, cipher, expires_at=clock.value + 10)

        with pytest.raises(RemoteOutcomeUnknown):
            await manager.refresh_account(1)

        assert (
            await database.scalar('SELECT state FROM bili_accounts WHERE id=1')
            == 'refresh_unknown'
        )
        assert protocol.refresh_calls == 2
    finally:
        await manager.close()
        await database.close()


@pytest.mark.asyncio
async def test_refresh_validation_retries_once_then_pauses_without_saving(
    tmp_path: Path,
) -> None:
    clock = FakeClock()
    protocol = ScriptedProtocol(
        refresh_results=[confirmed_response()],
        oauth_results=[DefinitelyNotSent(), DefinitelyNotSent()],
    )
    database, store, cipher, manager = await components(tmp_path, protocol, clock)
    try:
        await seed_account(store, cipher, expires_at=clock.value + 10)
        before = await store.raw_ciphertext(account_id=1)

        with pytest.raises(DefinitelyNotSent):
            await manager.refresh_account(1)

        row = await database.fetchone(
            'SELECT state,credential_version FROM bili_accounts WHERE id=1'
        )
        assert row is not None
        assert dict(row) == {'state': 'refresh_unknown', 'credential_version': 1}
        assert await store.raw_ciphertext(account_id=1) == before
        assert protocol.oauth_calls == 2
    finally:
        await manager.close()
        await database.close()


@pytest.mark.asyncio
async def test_health_check_runs_at_most_once_per_twelve_hours(tmp_path: Path) -> None:
    clock = FakeClock(10 * 86400 + 100)
    protocol = ScriptedProtocol()
    database, store, cipher, manager = await components(tmp_path, protocol, clock)
    try:
        await seed_account(store, cipher, expires_at=clock.value + 200 * 3600)

        first = await manager.refresh_due_accounts()
        second = await manager.refresh_due_accounts()
        clock.advance(12 * 3600 - 1)
        before_due = await manager.refresh_due_accounts()
        clock.advance(1)
        after_due = await manager.refresh_due_accounts()

        assert first == []
        assert second == []
        assert before_due == []
        assert after_due == []
        assert protocol.oauth_calls == 2
        assert protocol.refresh_calls == 0
    finally:
        await manager.close()
        await database.close()


@pytest.mark.asyncio
async def test_health_check_continues_after_one_account_is_invalid(
    tmp_path: Path,
) -> None:
    clock = FakeClock()
    protocol = ScriptedProtocol()
    database, _store, _cipher, manager = await components(tmp_path, protocol, clock)
    try:
        first = await manager.finish_confirmed_login(confirmed_response())
        protocol.token_mid = 43
        protocol.web_uid = 43
        second = await manager.finish_confirmed_login(
            confirmed_response(mid=43, cookie_uid=43)
        )
        protocol.oauth_results.extend(
            [
                BiliApiError(-101, operation='oauth_info'),
                {'code': 0, 'data': {'mid': 43, 'refresh': False}},
            ]
        )

        await manager.refresh_due_accounts()

        assert (
            await database.scalar(
                'SELECT state FROM bili_accounts WHERE id=?', (first.id,)
            )
            == 'paused'
        )
        assert (
            await database.scalar(
                'SELECT state FROM bili_accounts WHERE id=?', (second.id,)
            )
            == 'active'
        )
        assert protocol.oauth_calls == 4  # two logins and two health checks
    finally:
        await manager.close()
        await database.close()


@pytest.mark.asyncio
async def test_confirmed_auth_failure_pauses_primary_account_and_notifies_once(
    tmp_path: Path,
) -> None:
    clock = FakeClock()
    protocol = ScriptedProtocol()
    changed = AsyncMock()
    database, _store, _cipher, manager = await components(
        tmp_path, protocol, clock, on_primary_credential_changed=changed
    )
    try:
        account = await manager.finish_confirmed_login(confirmed_response())
        changed.reset_mock()
        protocol.oauth_results.append(BiliApiError(-101, operation='oauth_info'))

        await asyncio.gather(
            manager.report_primary_auth_failure(), manager.report_primary_auth_failure()
        )

        row = await database.fetchone(
            'SELECT state,pause_reason FROM bili_accounts WHERE id=?', (account.id,)
        )
        assert row is not None
        assert dict(row) == {
            'state': 'paused',
            'pause_reason': 'credential is no longer authenticated',
        }
        assert protocol.oauth_calls == 2  # login validation, then failure confirmation
        changed.assert_awaited_once_with()
        assert await manager.primary_cookie_header('https://api.bilibili.com/') == ''
    finally:
        await manager.close()
        await database.close()


@pytest.mark.asyncio
async def test_account_gate_serializes_writes_and_rechecks_version(
    tmp_path: Path,
) -> None:
    clock = FakeClock()
    protocol = ScriptedProtocol()
    database, store, cipher, manager = await components(tmp_path, protocol, clock)
    gate = AccountWriteGate(database).for_account(1)
    first_entered = asyncio.Event()
    release_first = asyncio.Event()
    second_entered = asyncio.Event()
    try:
        await seed_account(store, cipher, expires_at=clock.value + 1000)

        async def first() -> None:
            async with gate.hold(1):
                first_entered.set()
                await release_first.wait()

        async def second() -> None:
            async with gate.hold(1):
                second_entered.set()

        first_task = asyncio.create_task(first())
        await first_entered.wait()
        second_task = asyncio.create_task(second())
        await asyncio.sleep(0)
        assert not second_entered.is_set()
        release_first.set()
        await asyncio.gather(first_task, second_task)
        assert second_entered.is_set()

        with pytest.raises(CredentialVersionChanged):
            async with gate.hold(2):
                pass
        await database.execute(
            "UPDATE bili_accounts SET state='paused',pause_reason='manual' WHERE id=1"
        )
        with pytest.raises(AccountPaused):
            async with gate.hold(1):
                pass
    finally:
        await manager.close()
        await database.close()
