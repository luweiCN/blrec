from __future__ import annotations

import ipaddress
import secrets
import time
from datetime import datetime, timezone
from email.utils import format_datetime
from typing import Dict, Optional

from fastapi import APIRouter, Request, Response, status
from fastapi.exceptions import HTTPException
from pydantic import BaseModel, Field

from blrec.web import security
from blrec.web.auth_store import (
    AdminAlreadyInitialized,
    AdminAuthStore,
    AuthenticationFailed,
    AuthenticationRateLimited,
    SessionCredentials,
)

router = APIRouter(prefix='/auth', tags=['administrator-auth'])

store: Optional[AdminAuthStore] = None
bootstrap_api_key = ''


class SetupRequest(BaseModel):
    username: str = Field(..., min_length=1, max_length=64)
    api_key: str = Field('', alias='apiKey', max_length=1024)
    password: str = Field(..., min_length=10, max_length=1024)

    class Config:
        allow_population_by_field_name = True


class LoginRequest(BaseModel):
    username: str = Field(..., min_length=1, max_length=64)
    password: str = Field(..., min_length=1, max_length=1024)


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(..., alias='currentPassword', max_length=1024)
    new_password: str = Field(..., alias='newPassword', min_length=10, max_length=1024)

    class Config:
        allow_population_by_field_name = True


class RecoverPasswordRequest(BaseModel):
    username: str = Field(..., min_length=1, max_length=64)
    api_key: str = Field('', alias='apiKey', max_length=1024)
    new_password: str = Field(..., alias='newPassword', min_length=10, max_length=1024)

    class Config:
        allow_population_by_field_name = True


def configure(value: AdminAuthStore, *, bootstrap_api_key: str = '') -> None:
    global store
    globals()['bootstrap_api_key'] = bootstrap_api_key
    store = value


def reset() -> None:
    global bootstrap_api_key, store
    store = None
    bootstrap_api_key = ''


@router.get('/status')
async def auth_status(request: Request) -> Dict[str, object]:
    auth_store = _store()
    token = request.cookies.get(security.SESSION_COOKIE_NAME, '')
    authenticated = auth_store.authenticate_session(token) is not None
    return {
        'setupRequired': not auth_store.is_initialized(),
        'authenticated': authenticated,
    }


@router.post('/setup')
async def setup(request: Request, command: SetupRequest) -> Response:
    security.require_same_origin(request)
    auth_store = _store()
    if auth_store.is_initialized():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail='Administrator is already initialized',
        )
    _require_bootstrap(request, command.username, command.api_key)
    try:
        credentials = auth_store.initialize(command.username, command.password)
    except AdminAlreadyInitialized:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail='Administrator is already initialized',
        ) from None
    return _session_response(request, credentials)


@router.post('/login')
async def login(request: Request, command: LoginRequest) -> Response:
    security.require_same_origin(request)
    auth_store = _store()
    if not auth_store.is_initialized():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail='Administrator setup is required',
        )
    client_key = request.client.host if request.client is not None else 'unknown'
    try:
        credentials = auth_store.login(
            command.username, command.password, client_key=client_key
        )
    except AuthenticationRateLimited as error:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail='Too many failed login attempts',
            headers={'Retry-After': str(error.retry_after)},
        ) from None
    except AuthenticationFailed:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail='Invalid administrator credentials',
        ) from None
    return _session_response(request, credentials)


@router.get('/session')
async def session(request: Request) -> Response:
    credentials = getattr(request.state, 'admin_session', None)
    if not isinstance(credentials, SessionCredentials):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail='Administrator session is required',
        )
    return _session_response(request, credentials)


@router.post('/logout', status_code=status.HTTP_204_NO_CONTENT)
async def logout(request: Request) -> Response:
    token = request.cookies.get(security.SESSION_COOKIE_NAME, '')
    _store().logout(token)
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    response.delete_cookie(
        security.SESSION_COOKIE_NAME,
        path='/',
        httponly=True,
        secure=request.url.scheme == 'https',
        samesite='lax',
    )
    return response


@router.post('/change-password', status_code=status.HTTP_204_NO_CONTENT)
async def change_password(request: Request, command: ChangePasswordRequest) -> Response:
    try:
        _store().change_password(command.current_password, command.new_password)
    except AuthenticationFailed:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail='Password is invalid'
        ) from None
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    response.delete_cookie(
        security.SESSION_COOKIE_NAME,
        path='/',
        httponly=True,
        secure=request.url.scheme == 'https',
        samesite='lax',
    )
    return response


@router.post('/recover', status_code=status.HTTP_204_NO_CONTENT)
async def recover(request: Request, command: RecoverPasswordRequest) -> Response:
    security.require_same_origin(request)
    _require_bootstrap(request, command.username, command.api_key)
    try:
        _store().reset_password(command.new_password)
    except AuthenticationFailed:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail='Administrator setup is required',
        ) from None
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def _session_response(request: Request, credentials: SessionCredentials) -> Response:
    from fastapi.responses import JSONResponse

    response = JSONResponse(_session_content(credentials))
    response.set_cookie(
        security.SESSION_COOKIE_NAME,
        credentials.session_token,
        max_age=max(1, credentials.expires_at - int(time.time())),
        expires=format_datetime(
            datetime.fromtimestamp(credentials.expires_at, timezone.utc), usegmt=True
        ),  # type: ignore[arg-type]  # Starlette treats integer expiry as relative.
        path='/',
        secure=request.url.scheme == 'https',
        httponly=True,
        samesite='lax',
    )
    return response


def _session_content(credentials: SessionCredentials) -> Dict[str, object]:
    return {
        'authenticated': True,
        'csrfToken': credentials.csrf_token,
        'expiresAt': credentials.expires_at,
    }


def _require_bootstrap(request: Request, username: str, supplied: str) -> None:
    auth_store = _store()
    client_key = request.client.host if request.client is not None else 'unknown'
    if bootstrap_api_key:
        credential_valid = secrets.compare_digest(
            supplied.encode('utf8'), bootstrap_api_key.encode('utf8')
        )
        local_access_required = False
    else:
        local_access_required = True
        credential_valid = _is_loopback_request(request)
    try:
        auth_store.verify_bootstrap_attempt(
            username, credential_valid, client_key=client_key
        )
    except AuthenticationRateLimited as error:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail='Too many failed initialization attempts',
            headers={'Retry-After': str(error.retry_after)},
        ) from None
    except AuthenticationFailed:
        if local_access_required and not credential_valid:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail='Local access is required for administrator setup',
            ) from None
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail='Invalid initialization credentials',
        ) from None


def _is_loopback_request(request: Request) -> bool:
    if request.client is None:
        return False
    try:
        return ipaddress.ip_address(request.client.host).is_loopback
    except ValueError:
        return False


def _store() -> AdminAuthStore:
    if store is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail='Administrator authentication is unavailable',
        )
    return store
