from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import time
from typing import Dict, Optional, Set
from urllib.parse import urlsplit

from fastapi import Header, Request, status
from fastapi.exceptions import HTTPException

from .auth_store import AdminAuthStore, ExtensionIdentity

api_key = ''
auth_store: Optional[AdminAuthStore] = None

SESSION_COOKIE_NAME = 'blrec_session'
_PUBLIC_AUTH_PATHS = frozenset(
    (
        '/api/v1/auth/status',
        '/api/v1/auth/setup',
        '/api/v1/auth/login',
        '/api/v1/auth/recover',
    )
)
_DEVELOPMENT_ORIGINS = frozenset(('http://localhost:4200', 'http://127.0.0.1:4200'))

MAX_WHITELIST = 100
MAX_BLACKLIST = 100
MAX_ATTEMPTING_CLIENTS = 100
MAX_ATTEMPTS = 3
whitelist: Set[str] = set()
blacklist: Set[str] = set()
attempting_clients: Dict[str, int] = {}

_RECORDING_MEDIA_PATH = re.compile(r'^/api/v1/recording-sessions/parts/(\d+)/media$')
_RECORDING_THUMBNAIL_PATH = re.compile(
    r'^/api/v1/recording-sessions/parts/(\d+)/thumbnail$'
)
_HIGHLIGHT_MEDIA_PATH = re.compile(r'^/api/v1/highlights/clips/(\d+)/media$')


def configure(store: AdminAuthStore, *, bootstrap_api_key: str = '') -> None:
    global api_key, auth_store
    auth_store = store
    api_key = bootstrap_api_key
    whitelist.clear()
    blacklist.clear()
    attempting_clients.clear()


def reset() -> None:
    global api_key, auth_store
    auth_store = None
    api_key = ''
    whitelist.clear()
    blacklist.clear()
    attempting_clients.clear()


def media_access_token(
    part_id: int, expires_at: int, snapshot_id: Optional[str] = None
) -> str:
    value = '{}:{}:{}'.format(int(part_id), int(expires_at), snapshot_id or '').encode(
        'ascii'
    )
    return hmac.new(_media_signing_key(), value, hashlib.sha256).hexdigest()


def valid_media_access(
    part_id: int, expires_at: int, token: str, snapshot_id: Optional[str] = None
) -> bool:
    if expires_at < int(time.time()):
        return False
    try:
        expected = media_access_token(part_id, expires_at, snapshot_id)
    except RuntimeError:
        return False
    return hmac.compare_digest(token, expected)


def require_same_origin(request: Request) -> None:
    origin = request.headers.get('origin')
    if origin is None or not valid_origin(request, origin):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail='Request origin is not allowed',
        )


def valid_origin(request: Request, origin: str) -> bool:
    parsed = urlsplit(origin)
    if parsed.scheme not in ('http', 'https') or not parsed.netloc:
        return False
    normalized = '{}://{}'.format(parsed.scheme, parsed.netloc.lower())
    expected = '{}://{}'.format(request.url.scheme, request.url.netloc.lower())
    if normalized == expected:
        return True
    return normalized in _DEVELOPMENT_ORIGINS


def manager_subject(request: Request) -> str:
    value = getattr(request.state, 'manager_subject', None)
    if value != 'administrator':
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail='Administrator session is required',
        )
    return value


async def authenticate(
    request: Request, x_api_key: Optional[str] = Header(None)
) -> None:
    if auth_store is None:
        await _legacy_test_authenticate(request, x_api_key)
        return
    if (
        request.method == 'OPTIONS'
        or request.url.path in _PUBLIC_AUTH_PATHS
        or request.url.path.startswith('/api/v1/browser-extension/')
    ):
        return
    if request.method == 'GET' and request.url.path.startswith(
        '/api/v1/control-operations/'
    ):
        extension_token = request.headers.get('x-blrec-extension-token', '')
        if extension_token:
            identity = auth_store.authenticate_extension(extension_token)
            if identity is not None:
                request.state.extension_identity = identity
                return
    if _valid_signed_media_request(request):
        return

    session_token = request.cookies.get(SESSION_COOKIE_NAME, '')
    session = auth_store.authenticate_session(session_token)
    if session is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail='Administrator session is required',
        )
    request.state.manager_subject = 'administrator'
    request.state.admin_session = session
    if request.method not in {'GET', 'HEAD', 'OPTIONS'}:
        require_same_origin(request)
        csrf_token = request.headers.get('x-csrf-token', '')
        if not auth_store.verify_csrf(session_token, csrf_token):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, detail='CSRF token is invalid'
            )


def authenticated_extension(request: Request) -> ExtensionIdentity:
    if auth_store is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail='浏览器插件授权不可用',
        )
    token = request.headers.get('x-blrec-extension-token', '')
    identity = auth_store.authenticate_extension(token)
    if identity is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail='浏览器插件授权无效'
        )
    return identity


def _media_signing_key() -> bytes:
    if auth_store is not None:
        return auth_store.media_signing_key
    if api_key:
        return api_key.encode('utf8')
    raise RuntimeError('media signing key is unavailable')


def _valid_signed_media_request(request: Request) -> bool:
    if request.method not in {'GET', 'HEAD'}:
        return False
    resource_id = _signed_media_resource_id(request.url.path)
    if resource_id is None:
        return False
    token = request.query_params.get('media_token')
    expires = request.query_params.get('media_expires')
    snapshot_id = request.query_params.get('media_snapshot')
    if token is None or expires is None:
        return False
    try:
        expires_at = int(expires)
    except ValueError:
        return False
    return valid_media_access(resource_id, expires_at, token, snapshot_id)


def _signed_media_resource_id(path: str) -> Optional[int]:
    recording = _RECORDING_MEDIA_PATH.fullmatch(path)
    if recording is not None:
        return int(recording.group(1))
    thumbnail = _RECORDING_THUMBNAIL_PATH.fullmatch(path)
    if thumbnail is not None:
        return int(thumbnail.group(1))
    highlight = _HIGHLIGHT_MEDIA_PATH.fullmatch(path)
    if highlight is not None:
        return -int(highlight.group(1))
    return None


async def _legacy_test_authenticate(request: Request, x_api_key: Optional[str]) -> None:
    if _valid_signed_media_request(request):
        return
    if not api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail='Administrator session is required',
        )
    if not x_api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail='No api key'
        )
    assert request.client is not None
    client_ip = request.client.host
    if client_ip in blacklist:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail='Blacklisted')
    if client_ip not in whitelist:
        if len(whitelist) >= MAX_WHITELIST or len(blacklist) >= MAX_BLACKLIST:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail='Max clients allowed in whitelist or blacklist will exceeded',
            )
        if len(attempting_clients) >= MAX_ATTEMPTING_CLIENTS:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail='Max attempting clients allowed exceeded',
            )
        attempting_clients[client_ip] = attempting_clients.get(client_ip, 0) + 1
        if attempting_clients[client_ip] > MAX_ATTEMPTS:
            del attempting_clients[client_ip]
            blacklist.add(client_ip)
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail='Max api key attempts exceeded',
            )
    if not secrets.compare_digest(x_api_key, api_key):
        whitelist.discard(client_ip)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail='API key is invalid'
        )
    attempting_clients.pop(client_ip, None)
    whitelist.add(client_ip)
    request.state.manager_subject = 'administrator'
