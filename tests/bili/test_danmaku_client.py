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
@pytest.mark.parametrize(
    'failing_stage', ['send_auth', 'receive_auth_reply', 'handle_auth_reply']
)
async def test_connect_closes_each_socket_before_retrying_auth_failure(
    monkeypatch: pytest.MonkeyPatch, failing_stage: str
) -> None:
    client = bare_client()
    failure = aiohttp.ClientError('{} failed'.format(failing_stage))
    events: List[str] = []
    sockets = [Mock(), Mock()]
    for index, socket in enumerate(sockets):

        async def close_socket(index: int = index) -> None:
            events.append('close{}'.format(index))
            if index == 1:
                raise OSError('socket close failed')

        socket.close = AsyncMock(side_effect=close_socket)

    async def connect_websocket() -> None:
        index = len([event for event in events if event.startswith('open')])
        client._ws = sockets[index]
        events.append('open{}'.format(index))

    async def fail_stage(*args: object) -> None:
        events.append('fail')
        raise failure

    async def update_danmu_info() -> None:
        events.append('update')

    client._host_index = 0
    client._danmu_info = {'host_list': [{}]}
    client._connect_websocket = AsyncMock(side_effect=connect_websocket)
    client._send_auth = AsyncMock()
    client._recieve_auth_reply = AsyncMock(return_value=object())
    client._handle_auth_reply = AsyncMock()
    client._update_danmu_info = AsyncMock(side_effect=update_danmu_info)
    if failing_stage == 'send_auth':
        client._send_auth.side_effect = fail_stage
    elif failing_stage == 'receive_auth_reply':
        client._recieve_auth_reply.side_effect = fail_stage
    else:
        client._handle_auth_reply.side_effect = fail_stage
    retrying = DanmakuClient._connect.retry
    monkeypatch.setattr(retrying, 'stop', stop_after_attempt(2))
    monkeypatch.setattr(retrying, 'wait', wait_none())

    with pytest.raises(aiohttp.ClientError, match='{} failed'.format(failing_stage)):
        await client._connect()

    assert events == [
        'open0',
        'fail',
        'close0',
        'update',
        'open1',
        'fail',
        'close1',
        'update',
    ]
    for socket in sockets:
        socket.close.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_connect_auth_cancellation_closes_socket_and_propagates() -> None:
    client = bare_client()
    socket = Mock()
    socket.close = AsyncMock()
    client._ws = socket
    client._host_index = 0
    client._danmu_info = {'host_list': [{}]}
    client._connect_websocket = AsyncMock()
    client._send_auth = AsyncMock(side_effect=asyncio.CancelledError())
    client._recieve_auth_reply = AsyncMock()
    client._handle_auth_reply = AsyncMock()
    client._update_danmu_info = AsyncMock()

    with pytest.raises(asyncio.CancelledError):
        await client._connect()

    socket.close.assert_awaited_once_with()
    client._update_danmu_info.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancel_during_failed_auth_close_stops_retry_and_host_rotation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = bare_client()
    close_entered = asyncio.Event()
    never_finish_close = asyncio.Event()
    close_count = 0
    socket = Mock()

    async def block_close() -> None:
        nonlocal close_count
        close_count += 1
        if close_count > 1:
            return
        close_entered.set()
        await never_finish_close.wait()

    socket.close = AsyncMock(side_effect=block_close)
    client._ws = socket
    client._host_index = 0
    client._danmu_info = {'host_list': [{}, {}]}
    client._connect_websocket = AsyncMock(
        side_effect=[None, AssertionError('unexpected websocket retry')]
    )
    client._send_auth = AsyncMock(side_effect=aiohttp.ClientError('auth failed'))
    client._recieve_auth_reply = AsyncMock()
    client._handle_auth_reply = AsyncMock()
    client._update_danmu_info = AsyncMock()
    retrying = DanmakuClient._connect.retry
    monkeypatch.setattr(retrying, 'stop', stop_after_attempt(2))
    monkeypatch.setattr(retrying, 'wait', wait_none())
    connecting = asyncio.create_task(client._connect())
    await close_entered.wait()

    connecting.cancel()
    try:
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(connecting, timeout=0.1)

        assert client._connect_websocket.await_count == 1
        assert client._host_index == 0
        socket.close.assert_awaited_once_with()
        client._update_danmu_info.assert_not_awaited()
    finally:
        never_finish_close.set()
        connecting.cancel()
        await asyncio.gather(connecting, return_exceptions=True)


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
