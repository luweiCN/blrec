import asyncio
import threading
from typing import Any, Callable, List, Optional

import pytest
from fastapi import WebSocketDisconnect

from blrec.web.routers import websockets


class FakeSubscription:
    def __init__(self) -> None:
        self.dispose_count = 0

    def dispose(self) -> None:
        self.dispose_count += 1


class FakeSource:
    def __init__(self) -> None:
        self.callback: Optional[Callable[[Any], None]] = None
        self.subscription = FakeSubscription()

    def subscribe(self, callback: Callable[[Any], None]) -> FakeSubscription:
        self.callback = callback
        return self.subscription

    def emit(self, value: Any) -> None:
        assert self.callback is not None
        self.callback(value)


class BlockingWebSocket:
    def __init__(self) -> None:
        self.send_started = asyncio.Event()
        self.release_send = asyncio.Event()
        self.sent: List[str] = []
        self.closed: List[int] = []

    async def send_text(self, value: str) -> None:
        self.send_started.set()
        await self.release_send.wait()
        self.sent.append(value)

    async def close(self, *, code: int) -> None:
        self.closed.append(code)


class DisconnectingWebSocket:
    def __init__(self, disconnect_after: int) -> None:
        self.disconnect_after = disconnect_after
        self.sent: List[str] = []
        self.closed: List[int] = []

    async def send_text(self, value: str) -> None:
        if len(self.sent) >= self.disconnect_after:
            raise WebSocketDisconnect(code=1001)
        self.sent.append(value)

    async def close(self, *, code: int) -> None:
        self.closed.append(code)


class FailingWebSocket:
    def __init__(self, message: str) -> None:
        self.message = message
        self.closed: List[int] = []

    async def send_text(self, value: str) -> None:
        raise RuntimeError(self.message)

    async def close(self, *, code: int) -> None:
        self.closed.append(code)


@pytest.mark.asyncio
async def test_connection_pump_bounds_backlog_and_closes_overflow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = FakeSource()
    socket = BlockingWebSocket()
    audits = []
    monkeypatch.setattr(
        websockets, 'audit', lambda event, **fields: audits.append((event, fields))
    )

    pump = asyncio.create_task(
        websockets._run_connection_pump(  # type: ignore[attr-defined]
            socket,
            route='events',
            subscribe=source.subscribe,
            serialize=str,
            handshake_started_at=websockets.time.monotonic(),
        )
    )
    await asyncio.sleep(0)
    source.emit(0)
    await socket.send_started.wait()
    for index in range(1, 1001):
        source.emit(index)
    await asyncio.wait_for(pump, timeout=1)

    assert socket.closed == [1013]
    assert source.subscription.dispose_count == 1
    assert audits[-1][0] == 'websocket_connection'
    assert audits[-1][1]['disconnect_reason'] == 'overflow'
    assert audits[-1][1]['peak_backlog'] == 128


@pytest.mark.asyncio
async def test_connection_pump_uses_one_ordered_sender_and_disposes_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = FakeSource()
    socket = DisconnectingWebSocket(disconnect_after=3)
    audits = []
    monkeypatch.setattr(
        websockets, 'audit', lambda event, **fields: audits.append((event, fields))
    )

    pump = asyncio.create_task(
        websockets._run_connection_pump(  # type: ignore[attr-defined]
            socket,
            route='events',
            subscribe=source.subscribe,
            serialize=lambda value: 'event-{}'.format(value),
            handshake_started_at=websockets.time.monotonic(),
        )
    )
    await asyncio.sleep(0)
    for index in range(4):
        source.emit(index)
    await asyncio.wait_for(pump, timeout=1)

    assert socket.sent == ['event-0', 'event-1', 'event-2']
    assert source.subscription.dispose_count == 1
    assert audits[-1][1]['events'] == 3
    assert audits[-1][1]['bytes'] == sum(
        len(value.encode('utf8')) for value in socket.sent
    )
    assert audits[-1][1]['disconnect_reason'] == 'client_disconnect'
    assert audits[-1][1]['disconnect_code'] == 1001


@pytest.mark.asyncio
async def test_connection_pump_accepts_events_from_another_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = FakeSource()
    socket = DisconnectingWebSocket(disconnect_after=1)
    monkeypatch.setattr(websockets, 'audit', lambda *args, **kwargs: None)
    pump = asyncio.create_task(
        websockets._run_connection_pump(  # type: ignore[attr-defined]
            socket,
            route='exceptions',
            subscribe=source.subscribe,
            serialize=str,
            handshake_started_at=websockets.time.monotonic(),
        )
    )
    await asyncio.sleep(0)

    thread = threading.Thread(target=lambda: (source.emit('one'), source.emit('two')))
    thread.start()
    thread.join()
    await asyncio.wait_for(pump, timeout=1)

    assert socket.sent == ['one']
    assert source.subscription.dispose_count == 1


@pytest.mark.asyncio
async def test_connection_metric_never_contains_event_or_exception_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = FakeSource()
    secret = 'sensitive-websocket-payload'
    socket = FailingWebSocket(secret)
    audits = []
    monkeypatch.setattr(
        websockets, 'audit', lambda event, **fields: audits.append((event, fields))
    )
    pump = asyncio.create_task(
        websockets._run_connection_pump(  # type: ignore[attr-defined]
            socket,
            route='exceptions',
            subscribe=source.subscribe,
            serialize=lambda value: secret,
            handshake_started_at=websockets.time.monotonic(),
        )
    )
    await asyncio.sleep(0)
    source.emit(RuntimeError(secret))
    await asyncio.wait_for(pump, timeout=1)

    assert socket.closed == [1011]
    assert audits[-1][1]['disconnect_reason'] == 'send_error'
    assert secret not in repr(audits)
    assert source.subscription.dispose_count == 1


@pytest.mark.asyncio
async def test_connection_pump_cancellation_closes_and_disposes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = FakeSource()
    socket = BlockingWebSocket()
    audits = []
    monkeypatch.setattr(
        websockets, 'audit', lambda event, **fields: audits.append((event, fields))
    )
    pump = asyncio.create_task(
        websockets._run_connection_pump(  # type: ignore[attr-defined]
            socket,
            route='events',
            subscribe=source.subscribe,
            serialize=str,
            handshake_started_at=websockets.time.monotonic(),
        )
    )
    await asyncio.sleep(0)

    pump.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pump

    assert socket.closed == [1001]
    assert source.subscription.dispose_count == 1
    assert audits[-1][1]['disconnect_reason'] == 'server_shutdown'
