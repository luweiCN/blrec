import json
from dataclasses import replace

import pytest

from blrec.bili_upload import BiliUploadDatabase
from blrec.bili_upload.credentials import CredentialNotFound, CredentialStore
from blrec.bili_upload.crypto import (
    CookieRecord,
    CredentialBundle,
    CredentialCipher,
    InvalidCredentialBundle,
    InvalidCredentialKey,
)


def credential_fixture(mid: int = 42) -> CredentialBundle:
    return CredentialBundle(
        access_token='access-secret',
        refresh_token='refresh-secret',
        mid=mid,
        issued_at=100,
        expires_at=200,
        signing_family='tv',
        app_client_version='1.0.0',
        web_client_version='2.0.0',
        app_device_source='qr',
        web_device_source='nav',
        app_device_id='app-device',
        app_buvid='app-buvid',
        web_buvid3='web-buvid3',
        web_buvid4='web-buvid4',
        web_b_nut='web-b-nut',
        cookies=(
            CookieRecord(
                name='SESSDATA',
                value='cookie-secret',
                domain='.bilibili.com',
                path='/',
                expires_at=300,
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


def test_bundle_round_trip_keeps_protocol_scopes() -> None:
    bundle = credential_fixture()
    cipher = CredentialCipher({'current': b'a' * 32}, current_key_id='current')

    envelope = cipher.encrypt(bundle, account_uid=42, version=1)
    restored = cipher.decrypt(envelope, account_uid=42)

    assert restored == bundle
    assert restored.app_device_id != restored.web_buvid3
    assert restored.csrf == 'csrf-secret'


def test_encryption_uses_a_fresh_nonce() -> None:
    cipher = CredentialCipher({'current': b'a' * 32}, current_key_id='current')

    first = cipher.encrypt(credential_fixture(), account_uid=42, version=1)
    second = cipher.encrypt(credential_fixture(), account_uid=42, version=1)

    assert first != second


def test_envelope_contains_only_the_versioned_cipher_fields() -> None:
    cipher = CredentialCipher({'current': b'a' * 32}, current_key_id='current')

    envelope = json.loads(
        cipher.encrypt(credential_fixture(), account_uid=42, version=7)
    )

    assert set(envelope) == {
        'format_version',
        'key_id',
        'credential_version',
        'nonce',
        'ciphertext',
    }
    assert envelope['format_version'] == 1
    assert envelope['key_id'] == 'current'
    assert envelope['credential_version'] == 7
    assert 'access-secret' not in json.dumps(envelope)


@pytest.mark.parametrize('size', (0, 16, 31, 33))
def test_cipher_rejects_non_32_byte_keys(size: int) -> None:
    with pytest.raises(InvalidCredentialKey, match='32 bytes'):
        CredentialCipher({'current': b'a' * size}, current_key_id='current')


def test_cipher_requires_the_current_key_id() -> None:
    with pytest.raises(InvalidCredentialKey, match='current key'):
        CredentialCipher({'old': b'a' * 32}, current_key_id='current')


def test_old_key_map_can_decrypt_an_existing_envelope() -> None:
    old_cipher = CredentialCipher({'old': b'o' * 32}, current_key_id='old')
    envelope = old_cipher.encrypt(credential_fixture(), account_uid=42, version=1)
    rotating_cipher = CredentialCipher(
        {'new': b'n' * 32, 'old': b'o' * 32}, current_key_id='new'
    )

    assert rotating_cipher.decrypt(envelope, account_uid=42) == credential_fixture()


def test_wrong_key_uid_and_tampering_fail_without_crypto_details() -> None:
    cipher = CredentialCipher({'current': b'a' * 32}, current_key_id='current')
    envelope = cipher.encrypt(credential_fixture(), account_uid=42, version=1)

    wrong = CredentialCipher({'current': b'b' * 32}, current_key_id='current')
    with pytest.raises(
        InvalidCredentialKey, match='authentication failed'
    ) as wrong_error:
        wrong.decrypt(envelope, account_uid=42)
    assert 'InvalidTag' not in str(wrong_error.value)

    with pytest.raises(InvalidCredentialKey, match='authentication failed'):
        cipher.decrypt(envelope, account_uid=43)

    document = json.loads(envelope)
    document['ciphertext'] = document['ciphertext'][:-2] + 'AA'
    with pytest.raises((InvalidCredentialBundle, InvalidCredentialKey)) as tamper_error:
        cipher.decrypt(
            json.dumps(document, sort_keys=True).encode('utf8'), account_uid=42
        )
    assert 'InvalidTag' not in str(tamper_error.value)


def test_unknown_key_and_malformed_envelopes_are_rejected() -> None:
    cipher = CredentialCipher({'current': b'a' * 32}, current_key_id='current')
    envelope = json.loads(
        cipher.encrypt(credential_fixture(), account_uid=42, version=1)
    )
    envelope['key_id'] = 'missing'

    with pytest.raises(InvalidCredentialKey, match='unavailable'):
        cipher.decrypt(json.dumps(envelope).encode('utf8'), account_uid=42)
    with pytest.raises(InvalidCredentialBundle, match='invalid credential envelope'):
        cipher.decrypt(b'not-json', account_uid=42)


def test_bundle_and_cookie_repr_never_contain_secrets() -> None:
    bundle = credential_fixture()

    rendered = repr(bundle) + repr(bundle.cookies[0])

    assert 'access-secret' not in rendered
    assert 'refresh-secret' not in rendered
    assert 'cookie-secret' not in rendered


def test_csrf_requires_exactly_one_bili_jct_cookie() -> None:
    bundle = credential_fixture()
    without_csrf = replace(
        bundle,
        cookies=tuple(cookie for cookie in bundle.cookies if cookie.name != 'bili_jct'),
    )
    duplicate_csrf = replace(bundle, cookies=bundle.cookies + (bundle.cookies[1],))

    with pytest.raises(InvalidCredentialBundle, match='exactly one'):
        _ = without_csrf.csrf
    with pytest.raises(InvalidCredentialBundle, match='exactly one'):
        _ = duplicate_csrf.csrf


@pytest.mark.asyncio
async def test_store_put_get_and_replace_are_atomic(tmp_path) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'blrec.sqlite3'))
    await database.open()
    store = CredentialStore(database)
    cipher = CredentialCipher({'current': b'a' * 32}, current_key_id='current')
    try:
        first_version = await store.put(
            account_id=1,
            account_uid=42,
            display_name='first',
            bundle=credential_fixture(),
            cipher=cipher,
            now=100,
        )
        first_ciphertext = await store.raw_ciphertext(account_id=1)
        second_bundle = replace(credential_fixture(), expires_at=400)
        second_version = await store.put(
            account_id=1,
            account_uid=42,
            display_name='second',
            bundle=second_bundle,
            cipher=cipher,
            now=200,
        )

        row = await database.fetchone(
            'SELECT uid,display_name,credential_version,key_id,state,'
            'created_at,updated_at FROM bili_accounts WHERE id=?',
            (1,),
        )
        assert row is not None
        assert dict(row) == {
            'uid': 42,
            'display_name': 'second',
            'credential_version': 2,
            'key_id': 'current',
            'state': 'active',
            'created_at': 100,
            'updated_at': 200,
        }
        assert first_version == 1
        assert second_version == 2
        assert await store.get(account_id=1, cipher=cipher) == second_bundle
        assert await store.raw_ciphertext(account_id=1) != first_ciphertext
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_store_rejects_a_replayed_older_envelope(tmp_path) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'blrec.sqlite3'))
    await database.open()
    store = CredentialStore(database)
    cipher = CredentialCipher({'current': b'a' * 32}, current_key_id='current')
    try:
        await store.put(
            account_id=1,
            account_uid=42,
            display_name='account',
            bundle=credential_fixture(),
            cipher=cipher,
            now=100,
        )
        version_one = await store.raw_ciphertext(account_id=1)
        await store.put(
            account_id=1,
            account_uid=42,
            display_name='account',
            bundle=replace(credential_fixture(), expires_at=400),
            cipher=cipher,
            now=200,
        )
        await database.execute(
            'UPDATE bili_accounts SET credential_ciphertext=? WHERE id=?',
            (version_one, 1),
        )

        with pytest.raises(InvalidCredentialBundle, match='metadata mismatch'):
            await store.get(account_id=1, cipher=cipher)
        with pytest.raises(InvalidCredentialBundle, match='metadata mismatch'):
            await store.rotate(account_id=1, cipher=cipher, now=300)

        assert await store.raw_ciphertext(account_id=1) == version_one
        assert (
            await database.scalar(
                'SELECT credential_version FROM bili_accounts WHERE id=?', (1,)
            )
            == 2
        )
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_store_rejects_a_database_key_id_mismatch(tmp_path) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'blrec.sqlite3'))
    await database.open()
    store = CredentialStore(database)
    cipher = CredentialCipher({'current': b'a' * 32}, current_key_id='current')
    try:
        await store.put(
            account_id=1,
            account_uid=42,
            display_name='account',
            bundle=credential_fixture(),
            cipher=cipher,
            now=100,
        )
        original = await store.raw_ciphertext(account_id=1)
        await database.execute(
            'UPDATE bili_accounts SET key_id=? WHERE id=?', ('different', 1)
        )

        with pytest.raises(InvalidCredentialBundle, match='metadata mismatch'):
            await store.get(account_id=1, cipher=cipher)
        with pytest.raises(InvalidCredentialBundle, match='metadata mismatch'):
            await store.rotate(account_id=1, cipher=cipher, now=200)

        assert await store.raw_ciphertext(account_id=1) == original
        assert (
            await database.scalar('SELECT key_id FROM bili_accounts WHERE id=?', (1,))
            == 'different'
        )
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_store_rejects_uid_mismatch_without_creating_an_account(tmp_path) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'blrec.sqlite3'))
    await database.open()
    store = CredentialStore(database)
    cipher = CredentialCipher({'current': b'a' * 32}, current_key_id='current')
    try:
        with pytest.raises(InvalidCredentialBundle, match='uid mismatch'):
            await store.put(
                account_id=1,
                account_uid=43,
                display_name='mismatch',
                bundle=credential_fixture(mid=42),
                cipher=cipher,
                now=100,
            )
        assert await database.scalar('SELECT COUNT(*) FROM bili_accounts') == 0
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_rotation_reads_an_old_key_and_writes_the_new_key(tmp_path) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'blrec.sqlite3'))
    await database.open()
    store = CredentialStore(database)
    old_cipher = CredentialCipher({'old': b'o' * 32}, current_key_id='old')
    rotating_cipher = CredentialCipher(
        {'new': b'n' * 32, 'old': b'o' * 32}, current_key_id='new'
    )
    new_cipher = CredentialCipher({'new': b'n' * 32}, current_key_id='new')
    try:
        await store.put(
            account_id=1,
            account_uid=42,
            display_name='account',
            bundle=credential_fixture(),
            cipher=old_cipher,
            now=100,
        )

        version = await store.rotate(account_id=1, cipher=rotating_cipher, now=200)

        row = await database.fetchone(
            'SELECT credential_version,key_id,updated_at FROM bili_accounts WHERE id=?',
            (1,),
        )
        assert row is not None
        assert dict(row) == {
            'credential_version': 2,
            'key_id': 'new',
            'updated_at': 200,
        }
        assert version == 2
        assert await store.get(account_id=1, cipher=new_cipher) == credential_fixture()
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_wrong_key_never_overwrites_existing_ciphertext(tmp_path) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'blrec.sqlite3'))
    await database.open()
    store = CredentialStore(database)
    cipher = CredentialCipher({'current': b'a' * 32}, current_key_id='current')
    wrong_cipher = CredentialCipher(
        {'replacement': b'b' * 32}, current_key_id='replacement'
    )
    try:
        await store.put(
            account_id=1,
            account_uid=42,
            display_name='account',
            bundle=credential_fixture(),
            cipher=cipher,
            now=100,
        )
        original = await store.raw_ciphertext(account_id=1)

        with pytest.raises(InvalidCredentialKey):
            await store.rotate(account_id=1, cipher=wrong_cipher, now=200)

        assert await store.raw_ciphertext(account_id=1) == original
        assert (
            await database.scalar(
                'SELECT credential_version FROM bili_accounts WHERE id=?', (1,)
            )
            == 1
        )
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_failed_rotation_verification_rolls_back_the_new_envelope(
    tmp_path,
) -> None:
    class CorruptingCipher(CredentialCipher):
        def encrypt(
            self, bundle: CredentialBundle, *, account_uid: int, version: int
        ) -> bytes:
            return b'not-an-envelope'

    database = BiliUploadDatabase(str(tmp_path / 'blrec.sqlite3'))
    await database.open()
    store = CredentialStore(database)
    old_cipher = CredentialCipher({'old': b'o' * 32}, current_key_id='old')
    corrupting_cipher = CorruptingCipher(
        {'new': b'n' * 32, 'old': b'o' * 32}, current_key_id='new'
    )
    try:
        await store.put(
            account_id=1,
            account_uid=42,
            display_name='account',
            bundle=credential_fixture(),
            cipher=old_cipher,
            now=100,
        )
        original = await store.raw_ciphertext(account_id=1)

        with pytest.raises(InvalidCredentialBundle):
            await store.rotate(account_id=1, cipher=corrupting_cipher, now=200)

        assert await store.raw_ciphertext(account_id=1) == original
        row = await database.fetchone(
            'SELECT credential_version,key_id,updated_at FROM bili_accounts WHERE id=?',
            (1,),
        )
        assert row is not None
        assert dict(row) == {
            'credential_version': 1,
            'key_id': 'old',
            'updated_at': 100,
        }
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_failed_replacement_preserves_existing_ciphertext(tmp_path) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'blrec.sqlite3'))
    await database.open()
    store = CredentialStore(database)
    cipher = CredentialCipher({'current': b'a' * 32}, current_key_id='current')
    try:
        await store.put(
            account_id=1,
            account_uid=42,
            display_name='account',
            bundle=credential_fixture(),
            cipher=cipher,
            now=100,
        )
        original = await store.raw_ciphertext(account_id=1)

        with pytest.raises(InvalidCredentialBundle, match='uid mismatch'):
            await store.put(
                account_id=1,
                account_uid=42,
                display_name='changed',
                bundle=credential_fixture(mid=43),
                cipher=cipher,
                now=200,
            )

        assert await store.raw_ciphertext(account_id=1) == original
        assert (
            await database.scalar(
                'SELECT display_name FROM bili_accounts WHERE id=?', (1,)
            )
            == 'account'
        )
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_store_reports_missing_accounts_without_exposing_queries(
    tmp_path,
) -> None:
    database = BiliUploadDatabase(str(tmp_path / 'blrec.sqlite3'))
    await database.open()
    store = CredentialStore(database)
    cipher = CredentialCipher({'current': b'a' * 32}, current_key_id='current')
    try:
        with pytest.raises(CredentialNotFound, match='credential account not found'):
            await store.get(account_id=999, cipher=cipher)
        with pytest.raises(CredentialNotFound, match='credential account not found'):
            await store.rotate(account_id=999, cipher=cipher, now=100)
    finally:
        await database.close()
