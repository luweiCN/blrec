import asyncio
import importlib
from typing import List
from unittest.mock import AsyncMock, Mock

import pytest


def connection_type():
    module = importlib.import_module('blrec.bili.danmaku_connection')
    return module.DanmakuConnection


def configured_connection(start_side_effect=None, authenticated_available=True):
    client = Mock()
    client.start = AsyncMock(side_effect=start_side_effect)
    client.stop = AsyncMock()
    client.restart = AsyncMock()
    client.set_room_id = Mock()
    configured: List[str] = []

    def configure_anonymous() -> None:
        configured.append('anonymous')

    def configure_authenticated() -> bool:
        configured.append('authenticated')
        return authenticated_available

    connection = connection_type()(
        client,
        configure_anonymous=configure_anonymous,
        configure_authenticated=configure_authenticated,
    )
    return connection, client, configured


@pytest.mark.asyncio
async def test_connection_uses_anonymous_transport_first() -> None:
    connection, client, configured = configured_connection()

    await connection.start()

    assert configured == ['anonymous']
    client.start.assert_awaited_once_with()
    assert connection.mode == 'anonymous'


@pytest.mark.asyncio
async def test_connection_falls_back_to_one_authenticated_transport() -> None:
    failure = OSError('anonymous connection failed')
    connection, client, configured = configured_connection([failure, None])

    await connection.start()

    assert configured == ['anonymous', 'authenticated']
    assert client.start.await_count == 2
    client.stop.assert_awaited_once_with()
    assert connection.mode == 'authenticated'


@pytest.mark.asyncio
async def test_connection_keeps_authenticated_mode_for_same_broadcast() -> None:
    connection, client, configured = configured_connection(
        [OSError('anonymous connection failed'), None]
    )
    await connection.start()
    client.start.reset_mock(side_effect=True)
    client.stop.reset_mock()

    await connection.restart()

    assert configured == ['anonymous', 'authenticated']
    client.stop.assert_awaited_once_with()
    client.start.assert_awaited_once_with()
    assert connection.mode == 'authenticated'


@pytest.mark.asyncio
async def test_connection_resets_to_anonymous_after_broadcast_end() -> None:
    connection, client, configured = configured_connection(
        [OSError('anonymous connection failed'), None]
    )
    await connection.start()

    await connection.stop(reset_mode=True)
    client.start.reset_mock(side_effect=True)
    await connection.start()

    assert configured == ['anonymous', 'authenticated', 'anonymous', 'anonymous']
    assert connection.mode == 'anonymous'


@pytest.mark.asyncio
async def test_connection_does_not_cycle_accounts_when_fallback_is_unavailable() -> (
    None
):
    failure = OSError('anonymous connection failed')
    connection, client, configured = configured_connection(
        failure, authenticated_available=False
    )

    with pytest.raises(OSError, match='anonymous connection failed'):
        await connection.start()

    assert configured == ['anonymous', 'authenticated']
    assert client.start.await_count == 1
    client.stop.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_connection_cancellation_does_not_attempt_cookie_fallback() -> None:
    connection, client, configured = configured_connection(asyncio.CancelledError())

    with pytest.raises(asyncio.CancelledError):
        await connection.start()

    assert configured == ['anonymous']
    client.stop.assert_awaited_once_with()
