from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any, AsyncIterator, Mapping, Set

from fastapi import Request
from fastapi.responses import StreamingResponse


@dataclass(frozen=True)
class DashboardRealtimeEvent:
    type: str
    data: Mapping[str, Any]


class DashboardRealtimeSubscription:
    def __init__(self, queue_size: int) -> None:
        self._queue: asyncio.Queue[DashboardRealtimeEvent] = asyncio.Queue(queue_size)

    async def get(self) -> DashboardRealtimeEvent:
        return await self._queue.get()

    def put(self, event: DashboardRealtimeEvent) -> None:
        self._queue.put_nowait(event)

    @property
    def full(self) -> bool:
        return self._queue.full()

    def replace_with_resync(self) -> None:
        while not self._queue.empty():
            self._queue.get_nowait()
        self._queue.put_nowait(DashboardRealtimeEvent('resync', {}))


class DashboardRealtimeBroker:
    def __init__(self, *, queue_size: int = 16) -> None:
        if queue_size < 1:
            raise ValueError('realtime queue size must be positive')
        self._queue_size = queue_size
        self._subscriptions: Set[DashboardRealtimeSubscription] = set()

    def subscribe(self) -> DashboardRealtimeSubscription:
        subscription = DashboardRealtimeSubscription(self._queue_size)
        self._subscriptions.add(subscription)
        return subscription

    def unsubscribe(self, subscription: DashboardRealtimeSubscription) -> None:
        self._subscriptions.discard(subscription)

    async def publish(self, event_type: str, data: Mapping[str, Any]) -> None:
        event = DashboardRealtimeEvent(event_type, dict(data))
        for subscription in tuple(self._subscriptions):
            if subscription.full:
                subscription.replace_with_resync()
            else:
                subscription.put(event)


def encode_event(event_type: str, data: Mapping[str, Any]) -> bytes:
    payload = json.dumps(data, ensure_ascii=False, separators=(',', ':'))
    return 'event: {}\ndata: {}\n\n'.format(event_type, payload).encode('utf-8')


async def dashboard_events(
    request: Request, broker: DashboardRealtimeBroker
) -> AsyncIterator[bytes]:
    subscription = broker.subscribe()
    try:
        yield encode_event('resync', {})
        while not await request.is_disconnected():
            try:
                event = await asyncio.wait_for(subscription.get(), timeout=15)
            except asyncio.TimeoutError:
                yield encode_event('heartbeat', {})
            else:
                yield encode_event(event.type, event.data)
    finally:
        broker.unsubscribe(subscription)


def event_response(
    request: Request, broker: DashboardRealtimeBroker
) -> StreamingResponse:
    return StreamingResponse(
        dashboard_events(request, broker),
        media_type='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'X-Accel-Buffering': 'no',
            'Connection': 'keep-alive',
            'Content-Encoding': 'identity',
        },
    )
