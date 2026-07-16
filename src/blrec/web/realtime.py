from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, Mapping, Optional, Set


@dataclass(frozen=True)
class RealtimeEvent:
    type: str
    data: Mapping[str, Any]


class RealtimeSubscription:
    def __init__(self, queue_size: int) -> None:
        self._queue: asyncio.Queue[RealtimeEvent] = asyncio.Queue(queue_size)

    async def get(self) -> RealtimeEvent:
        return await self._queue.get()

    def empty(self) -> bool:
        return self._queue.empty()

    def put(self, event: RealtimeEvent) -> None:
        self._queue.put_nowait(event)

    def replace_with_resync(self) -> None:
        while not self._queue.empty():
            self._queue.get_nowait()
        self._queue.put_nowait(RealtimeEvent('resync', {}))

    @property
    def full(self) -> bool:
        return self._queue.full()


class RealtimeBroker:
    def __init__(self, *, queue_size: int = 64) -> None:
        if queue_size < 1:
            raise ValueError('realtime queue size must be positive')
        self._queue_size = queue_size
        self._subscriptions: Set[RealtimeSubscription] = set()

    def subscribe(self) -> RealtimeSubscription:
        subscription = RealtimeSubscription(self._queue_size)
        self._subscriptions.add(subscription)
        return subscription

    def unsubscribe(self, subscription: RealtimeSubscription) -> None:
        self._subscriptions.discard(subscription)

    async def publish(self, event_type: str, data: Mapping[str, Any]) -> None:
        event = RealtimeEvent(event_type, dict(data))
        for subscription in tuple(self._subscriptions):
            if subscription.full:
                subscription.replace_with_resync()
            else:
                subscription.put(event)


class RealtimeSampler:
    def __init__(
        self,
        broker: RealtimeBroker,
        *,
        task_provider: Callable[[], Any],
        network_provider: Callable[[], Any],
        upload_provider: Callable[[], Awaitable[Any]],
        highlight_provider: Optional[Callable[[], Awaitable[Any]]] = None,
        interval_seconds: float = 1.0,
    ) -> None:
        self._broker = broker
        self._task_provider = task_provider
        self._network_provider = network_provider
        self._upload_provider = upload_provider
        self._highlight_provider = highlight_provider
        self._interval_seconds = interval_seconds
        self._last: Dict[str, str] = {}
        self._task: Optional[asyncio.Task[None]] = None

    async def sample_once(self) -> None:
        tasks = self._task_provider()
        network = self._network_provider()
        uploads = await self._upload_provider()
        await self._publish_changed('tasks', {'tasks': tasks})
        await self._publish_changed('network', network)
        await self._publish_changed('upload_progress', {'jobs': uploads})
        if self._highlight_provider is not None:
            highlights = await self._highlight_provider()
            await self._publish_changed('highlight_progress', {'clips': highlights})

    async def _publish_changed(
        self, event_type: str, payload: Mapping[str, Any]
    ) -> None:
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(',', ':'),
            default=str,
        )
        if self._last.get(event_type) == encoded:
            return
        self._last[event_type] = encoded
        await self._broker.publish(event_type, payload)

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def _run(self) -> None:
        while True:
            try:
                await self.sample_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                await self._broker.publish('resync', {})
            await asyncio.sleep(self._interval_seconds)
