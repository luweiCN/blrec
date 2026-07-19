import asyncio
import importlib
from typing import List, Optional
from unittest.mock import AsyncMock, Mock

import pytest

from blrec.bili.exceptions import DanmakuClientAuthError


def connection_type():
    module = importlib.import_module('blrec.bili.danmaku_connection')
    return module.DanmakuConnection


def configured_connection(
    start_side_effect=None,
    authenticated_available=True,
    authenticated_failure_reporter=None,
    authenticated_fingerprints=None,
):
    client = Mock()
    client.start = AsyncMock(side_effect=start_side_effect)
    client.stop = AsyncMock()
    client.restart = AsyncMock()
    client.set_room_id = Mock()
    configured: List[str] = []
    fingerprints = list(authenticated_fingerprints or [])

    def configure_anonymous() -> None:
        configured.append('anonymous')

    def configure_authenticated() -> Optional[str]:
        configured.append('authenticated')
        if fingerprints:
            return fingerprints.pop(0)
        return 'credential-fingerprint' if authenticated_available else None

    connection = connection_type()(
        client,
        configure_anonymous=configure_anonymous,
        configure_authenticated=configure_authenticated,
        authenticated_failure_reporter=authenticated_failure_reporter,
    )
    return connection, client, configured


@pytest.mark.asyncio
async def test_connection_uses_authenticated_transport_first() -> None:
    connection, client, configured = configured_connection()

    await connection.start()

    assert configured == ['authenticated']
    client.start.assert_awaited_once_with()
    assert connection.mode == 'authenticated'


@pytest.mark.asyncio
async def test_connection_falls_back_to_anonymous_transport() -> None:
    failure = OSError('authenticated connection failed')
    connection, client, configured = configured_connection([failure, None])

    await connection.start()

    assert configured == ['authenticated', 'anonymous']
    assert client.start.await_count == 2
    client.stop.assert_awaited_once_with()
    assert connection.mode == 'anonymous'


@pytest.mark.asyncio
async def test_connection_reports_authenticated_token_rejection_before_fallback() -> (
    None
):
    reporter = AsyncMock()
    connection, client, configured = configured_connection(
        [DanmakuClientAuthError('token expired'), None],
        authenticated_failure_reporter=reporter,
    )

    await connection.start()

    reporter.assert_awaited_once_with('credential-fingerprint')
    assert configured == ['authenticated', 'authenticated']
    assert client.start.await_count == 2
    assert connection.mode == 'authenticated'


@pytest.mark.asyncio
async def test_connection_falls_back_when_renewed_authenticated_retry_fails() -> None:
    reporter = AsyncMock()
    connection, client, configured = configured_connection(
        [
            DanmakuClientAuthError('token expired'),
            DanmakuClientAuthError('token still expired'),
            None,
        ],
        authenticated_failure_reporter=reporter,
    )

    await connection.start()

    assert reporter.await_count == 2
    assert reporter.await_args_list[0].args == ('credential-fingerprint',)
    assert reporter.await_args_list[1].args == ('credential-fingerprint',)
    assert configured == [
        'authenticated',
        'authenticated',
        'authenticated',
        'anonymous',
    ]
    assert client.start.await_count == 3
    assert connection.mode == 'anonymous'


@pytest.mark.asyncio
async def test_connection_uses_credential_refreshed_after_standby_rejection() -> None:
    reporter = AsyncMock()
    connection, client, configured = configured_connection(
        [
            DanmakuClientAuthError('primary rejected'),
            DanmakuClientAuthError('standby needs refresh'),
            None,
        ],
        authenticated_failure_reporter=reporter,
        authenticated_fingerprints=['primary-v1', 'standby-v1', 'standby-v2'],
    )

    await connection.start()

    assert configured == ['authenticated', 'authenticated', 'authenticated']
    assert reporter.await_args_list[0].args == ('primary-v1',)
    assert reporter.await_args_list[1].args == ('standby-v1',)
    assert client.start.await_count == 3
    assert connection.mode == 'authenticated'


@pytest.mark.asyncio
async def test_connection_keeps_authenticated_mode_for_same_broadcast() -> None:
    connection, client, configured = configured_connection()
    await connection.start()
    client.start.reset_mock(side_effect=True)
    client.stop.reset_mock()

    await connection.restart()

    assert configured == ['authenticated', 'authenticated']
    client.stop.assert_awaited_once_with()
    client.start.assert_awaited_once_with()
    assert connection.mode == 'authenticated'


@pytest.mark.asyncio
async def test_connection_retries_authenticated_mode_after_broadcast_end() -> None:
    connection, client, configured = configured_connection()
    await connection.start()

    await connection.stop(reset_mode=True)
    client.start.reset_mock(side_effect=True)
    await connection.start()

    assert configured == ['authenticated', 'anonymous', 'authenticated']
    assert connection.mode == 'authenticated'


@pytest.mark.asyncio
async def test_connection_uses_anonymous_when_cookie_is_unavailable() -> None:
    connection, client, configured = configured_connection(
        authenticated_available=False
    )

    await connection.start()

    assert configured == ['authenticated', 'anonymous']
    assert client.start.await_count == 1
    assert connection.mode == 'anonymous'


@pytest.mark.asyncio
async def test_connection_cancellation_does_not_attempt_anonymous_fallback() -> None:
    connection, client, configured = configured_connection(asyncio.CancelledError())

    with pytest.raises(asyncio.CancelledError):
        await connection.start()

    assert configured == ['authenticated']
    client.stop.assert_awaited_once_with()
