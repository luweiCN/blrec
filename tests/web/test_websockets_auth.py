from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from blrec.web import security
from blrec.web.auth_store import AdminAuthStore
from blrec.web.routers.websockets import authenticate_websocket


class FakeWebSocket:
    def __init__(self, *, token: str = '', origin: str = 'https://testserver') -> None:
        self.cookies = {security.SESSION_COOKIE_NAME: token} if token else {}
        self.headers = {'origin': origin}
        self.url = SimpleNamespace(scheme='https', netloc='testserver')
        self.close = AsyncMock()


@pytest.fixture
def configured_store(tmp_path: Path):
    store = AdminAuthStore(str(tmp_path / 'auth.sqlite3'))
    store.open()
    security.configure(store, bootstrap_api_key='bootstrap-key')
    credentials = store.initialize('admin', 'correct horse battery staple')
    try:
        yield store, credentials
    finally:
        security.reset()
        store.close()


@pytest.mark.asyncio
async def test_websocket_rejects_missing_session_before_accept(
    configured_store,
) -> None:
    websocket = FakeWebSocket()

    assert not await authenticate_websocket(websocket)  # type: ignore[arg-type]
    websocket.close.assert_awaited_once_with(code=4401)


@pytest.mark.asyncio
async def test_websocket_rejects_cross_site_origin(configured_store) -> None:
    _, credentials = configured_store
    websocket = FakeWebSocket(
        token=credentials.session_token, origin='https://evil.example'
    )

    assert not await authenticate_websocket(websocket)  # type: ignore[arg-type]
    websocket.close.assert_awaited_once_with(code=4403)


@pytest.mark.asyncio
async def test_websocket_accepts_same_origin_session(configured_store) -> None:
    _, credentials = configured_store
    websocket = FakeWebSocket(token=credentials.session_token)

    assert await authenticate_websocket(websocket)  # type: ignore[arg-type]
    websocket.close.assert_not_awaited()
