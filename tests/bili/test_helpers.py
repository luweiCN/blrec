import asyncio
from typing import Any, Dict, List, Mapping, Optional

import aiohttp
import pytest
from yarl import URL

from blrec.application import Application
from blrec.bili.helpers import get_nav


class _Response:
    def __init__(
        self,
        payload: Mapping[str, Any],
        *,
        status: int = 200,
        release: Optional[asyncio.Event] = None,
    ) -> None:
        self._payload = payload
        self.status = status
        self.url = URL('https://api.bilibili.com/x/web-interface/nav')
        self._release = release
        self._raise_for_status = False

    async def __aenter__(self) -> '_Response':
        if self._release is not None:
            await self._release.wait()
        if self._raise_for_status and self.status >= 400:
            raise aiohttp.ClientResponseError(None, (), status=self.status)
        return self

    async def __aexit__(self, *_args: Any) -> None:
        return None

    async def json(self) -> Mapping[str, Any]:
        return self._payload


class _Session:
    def __init__(
        self,
        payload: Optional[Mapping[str, Any]] = None,
        *,
        status: int = 200,
        release: Optional[asyncio.Event] = None,
    ) -> None:
        self.payload = payload or {'code': 0, 'message': 'ok', 'data': {}}
        self.status = status
        self.release = release
        self.cookie_jar = aiohttp.DummyCookieJar()
        self.calls: List[Dict[str, Any]] = []
        self.close_calls = 0

    def get(self, _url: str, **kwargs: Any) -> _Response:
        self.calls.append(kwargs)
        response = _Response(self.payload, status=self.status, release=self.release)
        response._raise_for_status = bool(kwargs.get('raise_for_status'))
        return response

    async def close(self) -> None:
        self.close_calls += 1


@pytest.mark.asyncio
async def test_get_nav_reuses_cookie_less_session_with_request_cookie_only() -> None:
    session = _Session()

    first = await get_nav('SESSDATA=first-secret', session)
    second = await get_nav('SESSDATA=second-secret', session)

    assert first['code'] == second['code'] == 0
    assert len(session.calls) == 2
    assert session.calls[0]['headers']['Cookie'] == 'SESSDATA=first-secret'
    assert session.calls[1]['headers']['Cookie'] == 'SESSDATA=second-secret'
    assert session.calls[0]['raise_for_status'] is True
    assert session.calls[1]['raise_for_status'] is True
    assert not any(True for _cookie in session.cookie_jar)


@pytest.mark.asyncio
@pytest.mark.parametrize('status', [401, 429, 500])
async def test_get_nav_preserves_http_status_errors(status: int) -> None:
    session = _Session(status=status)

    with pytest.raises(aiohttp.ClientResponseError) as raised:
        await get_nav('SESSDATA=secret', session)

    assert raised.value.status == status
    assert all(call['raise_for_status'] is True for call in session.calls)


@pytest.mark.asyncio
async def test_application_cookie_validation_has_no_cache_and_absolute_timeout() -> (
    None
):
    session = _Session(release=asyncio.Event())
    app = object.__new__(Application)
    app._validation_timeout_seconds = 0.01
    app._network_route_manager = None
    app._network_session_pool = None
    app._bili_validation_session = session

    with pytest.raises(asyncio.TimeoutError):
        await app.validate_bili_cookie('SESSDATA=first-secret')
    session.release.set()
    await app.validate_bili_cookie('SESSDATA=first-secret')
    await app.validate_bili_cookie('SESSDATA=second-secret')

    assert len(session.calls) == 3
    assert [call['headers']['Cookie'] for call in session.calls[-2:]] == [
        'SESSDATA=first-secret',
        'SESSDATA=second-secret',
    ]
