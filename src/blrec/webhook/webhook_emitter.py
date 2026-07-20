from __future__ import annotations

import asyncio
import random
from collections import deque
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Deque, Dict, List, Optional, Set

import aiohttp
from loguru import logger

from .. import __prog__, __version__
from ..event import Error, ErrorData, EventCenter
from ..event.typing import Event
from ..exception import ExceptionCenter
from ..utils.mixins import SwitchableMixin
from .models import WebHook

__all__ = ('WebHookEmitter',)


@dataclass(frozen=True)
class _Delivery:
    payload: Dict[str, Any]


class WebHookEmitter(SwitchableMixin):
    def __init__(
        self,
        webhooks: Optional[List[WebHook]] = None,
        *,
        capacity: int = 100,
        concurrency: int = 4,
        request_timeout_seconds: float = 10,
        delivery_timeout_seconds: float = 60,
        session_factory: Callable[..., Any] = aiohttp.ClientSession,
        sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
        jitter: Callable[[float], float] = lambda delay: random.uniform(
            delay * 0.75, delay
        ),
    ) -> None:
        super().__init__()
        if capacity <= 0:
            raise ValueError('webhook capacity must be positive')
        if concurrency <= 0:
            raise ValueError('webhook concurrency must be positive')
        if request_timeout_seconds <= 0 or request_timeout_seconds > 10:
            raise ValueError('webhook request timeout must be in (0, 10]')
        if delivery_timeout_seconds <= 0 or delivery_timeout_seconds > 60:
            raise ValueError('webhook delivery timeout must be in (0, 60]')
        self.webhooks = [] if webhooks is None else webhooks
        self.headers = {'User-Agent': '{}/{}'.format(__prog__, __version__)}
        self._capacity = capacity
        self._concurrency = concurrency
        self._request_timeout_seconds = request_timeout_seconds
        self._delivery_timeout_seconds = delivery_timeout_seconds
        self._session_factory = session_factory
        self._sleeper = sleeper
        self._jitter = jitter
        self._session: Optional[Any] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._slots: Optional[asyncio.Semaphore] = None
        self._queues: Dict[str, Deque[_Delivery]] = {}
        self._active_urls: Set[str] = set()
        self._workers: Set[asyncio.Task[Any]] = set()
        self._pending_count = 0
        self._rejected_count = 0
        self._failed_count = 0
        self._accepting = False

    @property
    def pending_count(self) -> int:
        return self._pending_count

    @property
    def rejected_count(self) -> int:
        return self._rejected_count

    @property
    def failed_count(self) -> int:
        return self._failed_count

    @property
    def worker_count(self) -> int:
        return len(self._workers)

    async def start(self) -> None:
        if self._session is not None:
            return
        self._loop = asyncio.get_running_loop()
        self._slots = asyncio.Semaphore(self._concurrency)
        self._session = self._session_factory(
            headers=self.headers, cookie_jar=aiohttp.DummyCookieJar()
        )
        self._accepting = True

    async def close(self, *, drain_timeout_seconds: float = 5) -> None:
        if drain_timeout_seconds < 0:
            raise ValueError('webhook drain timeout must not be negative')
        self._accepting = False
        workers = tuple(self._workers)
        try:
            if workers:
                group = asyncio.gather(*workers, return_exceptions=True)
                try:
                    await asyncio.wait_for(group, timeout=drain_timeout_seconds)
                except asyncio.TimeoutError:
                    for worker in workers:
                        worker.cancel()
                    await asyncio.gather(*workers, return_exceptions=True)
        finally:
            for queue in self._queues.values():
                self._pending_count -= len(queue)
                queue.clear()
            self._queues.clear()
            self._active_urls.clear()
            self._workers.clear()
            self._pending_count = 0
            session = self._session
            self._session = None
            self._slots = None
            self._loop = None
            if session is not None:
                await session.close()

    def _do_enable(self) -> None:
        events = EventCenter.get_instance().events
        self._event_subscription = events.subscribe(self._on_event)
        exceptions = ExceptionCenter.get_instance().exceptions
        self._exc_subscription = exceptions.subscribe(self._on_exception)

    def _do_disable(self) -> None:
        self._event_subscription.dispose()
        self._exc_subscription.dispose()

    def _on_event(self, event: Event) -> None:
        for webhook in self.webhooks:
            if isinstance(event, webhook.event_types):
                self._send_event(webhook.url, event)

    def _on_exception(self, exc: BaseException) -> None:
        for webhook in self.webhooks:
            if webhook.receive_exception:
                self._send_exception(webhook.url, exc)

    def _send_event(self, url: str, event: Event) -> None:
        self._send_request(url, event.asdict())

    def _send_exception(self, url: str, exc: BaseException) -> None:
        payload = Error.from_data(ErrorData.from_exc(exc)).asdict()
        self._send_request(url, payload)

    def _send_request(self, url: str, payload: Dict[str, Any]) -> bool:
        loop = self._loop
        if not self._accepting or loop is None or not loop.is_running():
            self._rejected_count += 1
            return False
        current_loop: Optional[asyncio.AbstractEventLoop]
        try:
            current_loop = asyncio.get_running_loop()
        except RuntimeError:
            current_loop = None
        if current_loop is loop:
            return self._enqueue(url, payload)
        loop.call_soon_threadsafe(self._enqueue, url, payload)
        return True

    def _enqueue(self, url: str, payload: Dict[str, Any]) -> bool:
        if not self._accepting or self._session is None:
            self._rejected_count += 1
            return False
        if self._pending_count >= self._capacity:
            self._rejected_count += 1
            return False
        queue = self._queues.setdefault(url, deque())
        queue.append(_Delivery(payload))
        self._pending_count += 1
        if url not in self._active_urls:
            self._active_urls.add(url)
            worker = asyncio.create_task(self._run_url(url))
            self._workers.add(worker)
            worker.add_done_callback(self._workers.discard)
        return True

    async def _run_url(self, url: str) -> None:
        queue = self._queues[url]
        try:
            while queue:
                delivery = queue[0]
                try:
                    slots = self._slots
                    if slots is None:
                        return
                    async with slots:
                        await self._deliver(url, delivery.payload)
                finally:
                    if queue and queue[0] is delivery:
                        queue.popleft()
                        self._pending_count -= 1
        finally:
            remaining = len(queue)
            if remaining:
                queue.clear()
                self._pending_count -= remaining
            self._queues.pop(url, None)
            self._active_urls.discard(url)

    async def _deliver(self, url: str, payload: Dict[str, Any]) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._delivery_timeout_seconds
        last_error: Optional[BaseException] = None
        attempt = 0
        for attempt in range(1, 4):
            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            try:
                await asyncio.wait_for(
                    self._post(url, payload),
                    timeout=min(self._request_timeout_seconds, remaining),
                )
                return
            except asyncio.CancelledError:
                raise
            except BaseException as error:
                last_error = error
                if not self._is_transient(error) or attempt >= 3:
                    break
            base_delay = float(attempt)
            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            delay = max(0.0, min(base_delay, float(self._jitter(base_delay))))
            if delay >= remaining:
                break
            try:
                await asyncio.wait_for(self._sleeper(delay), timeout=remaining)
            except asyncio.CancelledError:
                raise
            except asyncio.TimeoutError:
                break
        self._failed_count += 1
        error_name = 'deadline' if last_error is None else type(last_error).__name__
        logger.warning(
            'Webhook delivery failed after {} attempt(s): {}', attempt, error_name
        )

    async def _post(self, url: str, payload: Dict[str, Any]) -> None:
        session = self._session
        if session is None:
            raise RuntimeError('webhook emitter is not started')
        timeout = aiohttp.ClientTimeout(total=self._request_timeout_seconds)
        async with session.post(url, json=payload, timeout=timeout) as response:
            response.raise_for_status()

    @staticmethod
    def _is_transient(error: BaseException) -> bool:
        if isinstance(error, aiohttp.ClientResponseError):
            return error.status == 429 or error.status >= 500
        return isinstance(error, (aiohttp.ClientError, asyncio.TimeoutError, OSError))
