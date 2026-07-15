from __future__ import annotations

import base64
import binascii
import json
import os
from dataclasses import asdict, dataclass
from typing import Any, Dict, Mapping, Optional, Tuple

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

__all__ = (
    'CookieRecord',
    'CredentialBundle',
    'CredentialCipher',
    'InvalidCredentialBundle',
    'InvalidCredentialKey',
)


class InvalidCredentialBundle(RuntimeError):
    pass


class InvalidCredentialKey(RuntimeError):
    pass


@dataclass(frozen=True, repr=False)
class CookieRecord:
    name: str
    value: str
    domain: str
    path: str
    expires_at: Optional[int]
    secure: bool
    http_only: bool

    def __repr__(self) -> str:
        return '<CookieRecord redacted>'


@dataclass(frozen=True, repr=False)
class CredentialBundle:
    access_token: str
    refresh_token: str
    mid: int
    issued_at: int
    expires_at: int
    signing_family: str
    app_client_version: str
    web_client_version: str
    app_device_source: str
    web_device_source: str
    app_device_id: str
    app_buvid: str
    web_buvid3: str
    web_buvid4: str
    web_b_nut: str
    cookies: Tuple[CookieRecord, ...]

    @property
    def csrf(self) -> str:
        values = [cookie.value for cookie in self.cookies if cookie.name == 'bili_jct']
        if len(values) != 1:
            raise InvalidCredentialBundle('expected exactly one bili_jct cookie')
        return values[0]

    def __repr__(self) -> str:
        return '<CredentialBundle redacted>'


class CredentialCipher:
    FORMAT_VERSION = 1
    NONCE_SIZE = 12
    _ENVELOPE_FIELDS = frozenset(
        ('format_version', 'key_id', 'credential_version', 'nonce', 'ciphertext')
    )
    _BUNDLE_FIELDS = frozenset(
        (
            'access_token',
            'refresh_token',
            'mid',
            'issued_at',
            'expires_at',
            'signing_family',
            'app_client_version',
            'web_client_version',
            'app_device_source',
            'web_device_source',
            'app_device_id',
            'app_buvid',
            'web_buvid3',
            'web_buvid4',
            'web_b_nut',
            'cookies',
        )
    )
    _COOKIE_FIELDS = frozenset(
        ('name', 'value', 'domain', 'path', 'expires_at', 'secure', 'http_only')
    )

    def __init__(self, keys: Mapping[str, bytes], *, current_key_id: str) -> None:
        normalized: Dict[str, bytes] = {}
        for key_id, key in keys.items():
            if not isinstance(key_id, str) or not key_id:
                raise InvalidCredentialKey('credential key id must not be empty')
            if not isinstance(key, bytes) or len(key) != 32:
                raise InvalidCredentialKey(
                    'credential key must contain exactly 32 bytes'
                )
            normalized[key_id] = key
        if current_key_id not in normalized:
            raise InvalidCredentialKey('current key is unavailable')
        self._keys = normalized
        self._current_key_id = current_key_id

    @property
    def current_key_id(self) -> str:
        return self._current_key_id

    def encrypt(
        self, bundle: CredentialBundle, *, account_uid: int, version: int
    ) -> bytes:
        self._validate_identity(account_uid, version)
        payload = asdict(bundle)
        plaintext = self._encode_json(payload)
        nonce = os.urandom(self.NONCE_SIZE)
        ciphertext = AESGCM(self._keys[self._current_key_id]).encrypt(
            nonce, plaintext, self._associated_data(account_uid, version)
        )
        envelope = {
            'format_version': self.FORMAT_VERSION,
            'key_id': self._current_key_id,
            'credential_version': version,
            'nonce': self._encode_base64(nonce),
            'ciphertext': self._encode_base64(ciphertext),
        }
        return self._encode_json(envelope)

    def decrypt(
        self,
        envelope: bytes,
        *,
        account_uid: int,
        expected_version: Optional[int] = None,
        expected_key_id: Optional[str] = None,
    ) -> CredentialBundle:
        document = self._decode_envelope(envelope)
        key_id = document['key_id']
        assert isinstance(key_id, str)
        version = document['credential_version']
        assert isinstance(version, int)
        if expected_version is not None and version != expected_version:
            raise InvalidCredentialBundle('credential envelope metadata mismatch')
        if expected_key_id is not None and key_id != expected_key_id:
            raise InvalidCredentialBundle('credential envelope metadata mismatch')
        key = self._keys.get(key_id)
        if key is None:
            raise InvalidCredentialKey('credential key is unavailable')

        self._validate_identity(account_uid, version)
        nonce = self._decode_base64(document['nonce'])
        ciphertext = self._decode_base64(document['ciphertext'])
        if len(nonce) != self.NONCE_SIZE or len(ciphertext) < 16:
            raise InvalidCredentialBundle('invalid credential envelope')
        try:
            plaintext = AESGCM(key).decrypt(
                nonce, ciphertext, self._associated_data(account_uid, version)
            )
        except (InvalidTag, ValueError):
            raise InvalidCredentialKey('credential authentication failed') from None
        try:
            payload = json.loads(plaintext.decode('utf8'))
        except (UnicodeError, json.JSONDecodeError):
            raise InvalidCredentialBundle('invalid credential payload') from None
        return self._decode_bundle(payload)

    @classmethod
    def _decode_envelope(cls, envelope: bytes) -> Dict[str, Any]:
        try:
            document = json.loads(envelope.decode('utf8'))
        except (AttributeError, UnicodeError, json.JSONDecodeError):
            raise InvalidCredentialBundle('invalid credential envelope') from None
        if not isinstance(document, dict) or set(document) != cls._ENVELOPE_FIELDS:
            raise InvalidCredentialBundle('invalid credential envelope')
        if (
            type(document['format_version']) is not int
            or document['format_version'] != cls.FORMAT_VERSION
        ):
            raise InvalidCredentialBundle('invalid credential envelope')
        if not isinstance(document['key_id'], str) or not document['key_id']:
            raise InvalidCredentialBundle('invalid credential envelope')
        version = document['credential_version']
        if type(version) is not int or version <= 0:
            raise InvalidCredentialBundle('invalid credential envelope')
        if not isinstance(document['nonce'], str) or not isinstance(
            document['ciphertext'], str
        ):
            raise InvalidCredentialBundle('invalid credential envelope')
        return document

    @classmethod
    def _decode_bundle(cls, payload: Any) -> CredentialBundle:
        if not isinstance(payload, dict) or set(payload) != cls._BUNDLE_FIELDS:
            raise InvalidCredentialBundle('invalid credential payload')
        string_fields = cls._BUNDLE_FIELDS - {
            'mid',
            'issued_at',
            'expires_at',
            'cookies',
        }
        if any(not isinstance(payload[field], str) for field in string_fields):
            raise InvalidCredentialBundle('invalid credential payload')
        if any(
            type(payload[field]) is not int
            for field in ('mid', 'issued_at', 'expires_at')
        ):
            raise InvalidCredentialBundle('invalid credential payload')
        raw_cookies = payload['cookies']
        if not isinstance(raw_cookies, list):
            raise InvalidCredentialBundle('invalid credential payload')
        cookies = tuple(cls._decode_cookie(cookie) for cookie in raw_cookies)
        values = dict(payload)
        values['cookies'] = cookies
        try:
            return CredentialBundle(**values)
        except TypeError:
            raise InvalidCredentialBundle('invalid credential payload') from None

    @classmethod
    def _decode_cookie(cls, payload: Any) -> CookieRecord:
        if not isinstance(payload, dict) or set(payload) != cls._COOKIE_FIELDS:
            raise InvalidCredentialBundle('invalid credential payload')
        if any(
            not isinstance(payload[field], str)
            for field in ('name', 'value', 'domain', 'path')
        ):
            raise InvalidCredentialBundle('invalid credential payload')
        expires_at = payload['expires_at']
        if expires_at is not None and type(expires_at) is not int:
            raise InvalidCredentialBundle('invalid credential payload')
        if (
            type(payload['secure']) is not bool
            or type(payload['http_only']) is not bool
        ):
            raise InvalidCredentialBundle('invalid credential payload')
        try:
            return CookieRecord(**payload)
        except TypeError:
            raise InvalidCredentialBundle('invalid credential payload') from None

    @staticmethod
    def _validate_identity(account_uid: int, version: int) -> None:
        if type(account_uid) is not int or account_uid <= 0:
            raise InvalidCredentialBundle('account uid must be positive')
        if type(version) is not int or version <= 0:
            raise InvalidCredentialBundle('credential version must be positive')

    @staticmethod
    def _associated_data(account_uid: int, version: int) -> bytes:
        return 'blrec:bili-account:{}:v{}'.format(account_uid, version).encode('ascii')

    @staticmethod
    def _encode_json(value: Any) -> bytes:
        return json.dumps(
            value, ensure_ascii=False, separators=(',', ':'), sort_keys=True
        ).encode('utf8')

    @staticmethod
    def _encode_base64(value: bytes) -> str:
        return base64.urlsafe_b64encode(value).decode('ascii')

    @staticmethod
    def _decode_base64(value: Any) -> bytes:
        if not isinstance(value, str):
            raise InvalidCredentialBundle('invalid credential envelope')
        try:
            return base64.b64decode(value, altchars=b'-_', validate=True)
        except (binascii.Error, ValueError):
            raise InvalidCredentialBundle('invalid credential envelope') from None
