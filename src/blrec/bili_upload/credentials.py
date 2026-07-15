from __future__ import annotations

import sqlite3
import time
from typing import Optional

from .crypto import CredentialBundle, CredentialCipher, InvalidCredentialBundle
from .database import BiliUploadDatabase

__all__ = ('CredentialNotFound', 'CredentialStore')


class CredentialNotFound(RuntimeError):
    pass


class CredentialStore:
    def __init__(self, database: BiliUploadDatabase) -> None:
        self._database = database

    async def put(
        self,
        *,
        account_id: int,
        account_uid: int,
        display_name: str,
        bundle: CredentialBundle,
        cipher: CredentialCipher,
        avatar_url: str = '',
        now: Optional[int] = None,
    ) -> int:
        timestamp = int(time.time()) if now is None else int(now)

        def replace(connection: sqlite3.Connection) -> int:
            row = connection.execute(
                'SELECT uid,credential_version FROM bili_accounts WHERE id=?',
                (account_id,),
            ).fetchone()
            if bundle.mid != account_uid:
                raise InvalidCredentialBundle('credential uid mismatch')
            if row is not None and int(row['uid']) != account_uid:
                raise InvalidCredentialBundle('credential uid mismatch')
            version = 1 if row is None else int(row['credential_version']) + 1
            envelope = cipher.encrypt(bundle, account_uid=account_uid, version=version)
            restored = cipher.decrypt(
                envelope,
                account_uid=account_uid,
                expected_version=version,
                expected_key_id=cipher.current_key_id,
            )
            if restored != bundle or restored.mid != account_uid:
                raise InvalidCredentialBundle('credential verification failed')

            if row is None:
                connection.execute(
                    'INSERT INTO bili_accounts('
                    'id,uid,display_name,avatar_url,credential_ciphertext,'
                    'credential_version,credential_expires_at,key_id,state,'
                    'created_at,updated_at) '
                    "VALUES(?,?,?,?,?,?,?,?,'active',?,?)",
                    (
                        account_id,
                        account_uid,
                        display_name,
                        avatar_url,
                        envelope,
                        version,
                        bundle.expires_at,
                        cipher.current_key_id,
                        timestamp,
                        timestamp,
                    ),
                )
            else:
                cursor = connection.execute(
                    'UPDATE bili_accounts SET display_name=?,avatar_url=?,'
                    'credential_ciphertext=?,credential_version=?,'
                    'credential_expires_at=?,key_id=?,state=\'active\','
                    'pause_reason=NULL,updated_at=? WHERE id=? AND uid=?',
                    (
                        display_name,
                        avatar_url,
                        envelope,
                        version,
                        bundle.expires_at,
                        cipher.current_key_id,
                        timestamp,
                        account_id,
                        account_uid,
                    ),
                )
                if cursor.rowcount != 1:
                    raise InvalidCredentialBundle('credential replacement failed')
            return version

        return await self._database.write(replace)

    async def update_metadata(
        self,
        *,
        account_id: int,
        account_uid: int,
        display_name: str,
        avatar_url: str,
        credential_expires_at: int,
        now: Optional[int] = None,
    ) -> None:
        timestamp = int(time.time()) if now is None else int(now)
        updated = await self._database.execute(
            'UPDATE bili_accounts SET display_name=?,avatar_url=?,'
            'credential_expires_at=?,updated_at=? WHERE id=? AND uid=?',
            (
                display_name,
                avatar_url,
                credential_expires_at,
                timestamp,
                account_id,
                account_uid,
            ),
        )
        if updated != 1:
            raise InvalidCredentialBundle('credential metadata update failed')

    async def get(
        self, *, account_id: int, cipher: CredentialCipher
    ) -> CredentialBundle:
        def load(connection: sqlite3.Connection) -> CredentialBundle:
            row = self._select_account(connection, account_id)
            uid = int(row['uid'])
            bundle = cipher.decrypt(
                self._ciphertext_bytes(row['credential_ciphertext']),
                account_uid=uid,
                expected_version=int(row['credential_version']),
                expected_key_id=str(row['key_id']),
            )
            if bundle.mid != uid:
                raise InvalidCredentialBundle('credential uid mismatch')
            return bundle

        return await self._database.read(load)

    async def rotate(
        self, *, account_id: int, cipher: CredentialCipher, now: Optional[int] = None
    ) -> int:
        timestamp = int(time.time()) if now is None else int(now)

        def replace(connection: sqlite3.Connection) -> int:
            row = self._select_account(connection, account_id)
            uid = int(row['uid'])
            current_version = int(row['credential_version'])
            bundle = cipher.decrypt(
                self._ciphertext_bytes(row['credential_ciphertext']),
                account_uid=uid,
                expected_version=current_version,
                expected_key_id=str(row['key_id']),
            )
            if bundle.mid != uid:
                raise InvalidCredentialBundle('credential uid mismatch')

            version = current_version + 1
            envelope = cipher.encrypt(bundle, account_uid=uid, version=version)
            restored = cipher.decrypt(
                envelope,
                account_uid=uid,
                expected_version=version,
                expected_key_id=cipher.current_key_id,
            )
            if restored != bundle or restored.mid != uid:
                raise InvalidCredentialBundle('credential verification failed')
            cursor = connection.execute(
                'UPDATE bili_accounts SET credential_ciphertext=?,'
                'credential_version=?,key_id=?,updated_at=? '
                'WHERE id=? AND uid=? AND credential_version=?',
                (
                    envelope,
                    version,
                    cipher.current_key_id,
                    timestamp,
                    account_id,
                    uid,
                    current_version,
                ),
            )
            if cursor.rowcount != 1:
                raise InvalidCredentialBundle('credential rotation failed')
            return version

        return await self._database.write(replace)

    async def raw_ciphertext(self, *, account_id: int) -> bytes:
        def load(connection: sqlite3.Connection) -> bytes:
            row = self._select_account(connection, account_id)
            return self._ciphertext_bytes(row['credential_ciphertext'])

        return await self._database.read(load)

    @staticmethod
    def _select_account(connection: sqlite3.Connection, account_id: int) -> sqlite3.Row:
        row = connection.execute(
            'SELECT uid,credential_ciphertext,credential_version,key_id '
            'FROM bili_accounts WHERE id=?',
            (account_id,),
        ).fetchone()
        if row is None:
            raise CredentialNotFound('credential account not found')
        return row

    @staticmethod
    def _ciphertext_bytes(value: object) -> bytes:
        if isinstance(value, bytes):
            return value
        if isinstance(value, memoryview):
            return value.tobytes()
        raise InvalidCredentialBundle('invalid stored credential envelope')
