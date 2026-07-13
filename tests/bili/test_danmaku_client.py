from __future__ import annotations

import asyncio
from typing import Coroutine, List
from unittest.mock import AsyncMock, Mock

import aiohttp
import pytest
from tenacity import stop_after_attempt, stop_after_delay, wait_none

from blrec.bili import danmaku_client as danmaku_module
from blrec.bili.danmaku_client import DanmakuClient


def bare_client() -> DanmakuClient:
    client = object.__new__(DanmakuClient)
    client._logger = Mock()
    client._logger_context = {}
    client._listeners = []
    return client


@pytest.mark.asyncio
async def test_connect_persistent_client_error_uses_bounded_retry_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = bare_client()
    failure = aiohttp.ClientError('persistent websocket failure')
    client._host_index = 0
    client._danmu_info = {'host_list': [{}]}
    client._connect_websocket = AsyncMock(side_effect=failure)
    client._send_auth = AsyncMock()
    client._recieve_auth_reply = AsyncMock()
    client._handle_auth_reply = AsyncMock()
    client._update_danmu_info = AsyncMock()
    retrying = DanmakuClient._connect.retry

    assert isinstance(retrying.stop, stop_after_delay)
    monkeypatch.setattr(retrying, 'stop', stop_after_attempt(2))
    monkeypatch.setattr(retrying, 'wait', wait_none())

    with pytest.raises(aiohttp.ClientError, match='persistent websocket failure'):
        await asyncio.wait_for(client._connect(), timeout=0.1)

    assert client._connect_websocket.await_count == 2


@pytest.mark.asyncio
async def test_receive_reconnect_failure_emits_terminal_once() -> None:
    client = bare_client()
    failure = aiohttp.ClientError('reconnect exhausted')
    listener = AsyncMock()
    client._listeners = [listener]
    client._retry_count = 0
    client._retry_delay = 0
    client._MAX_RETRIES = 1
    client.reconnect = AsyncMock(side_effect=failure)

    with pytest.raises(aiohttp.ClientError, match='reconnect exhausted'):
        await client._retry()

    listener.on_client_retries_exhausted.assert_awaited_once_with(failure)


@pytest.mark.asyncio
async def test_heartbeat_restart_failure_emits_terminal_and_releases_stop_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = bare_client()
    send_failure = aiohttp.ClientError('heartbeat send failed')
    connect_failure = aiohttp.ClientError('restart connect exhausted')
    listener = AsyncMock()
    client._listeners = [listener]
    client._ws = Mock()
    client._ws.send_bytes = AsyncMock(side_effect=send_failure)
    client._stopped = False
    client._stopped_lock = asyncio.Lock()
    client._do_stop = AsyncMock()
    client._do_start = AsyncMock(side_effect=connect_failure)
    real_create_task = asyncio.create_task
    spawned: List[asyncio.Task[None]] = []

    def capture_task(coro: Coroutine[object, object, None]) -> asyncio.Task[None]:
        task = real_create_task(coro)
        spawned.append(task)
        return task

    monkeypatch.setattr(danmaku_module.asyncio, 'create_task', capture_task)

    await client._send_heartbeat()
    assert len(spawned) == 1
    result = await asyncio.gather(spawned[0], return_exceptions=True)

    assert result == [connect_failure]
    listener.on_client_retries_exhausted.assert_awaited_once_with(connect_failure)
    await asyncio.wait_for(client.stop(), timeout=0.1)
    assert client._stopped_lock.locked() is False
    assert client._do_stop.await_count == 2
    listener.on_client_retries_exhausted.assert_awaited_once_with(connect_failure)
