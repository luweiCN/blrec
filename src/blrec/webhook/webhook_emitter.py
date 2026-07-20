from __future__ import annotations

import asyncio
import random
import threading
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
        self._lifecycle_lock = asyncio.Lock()
        self._admission_lock = threading.Lock()
        self._close_task: Optional[asyncio.Task[None]] = None
        self._queues: Dict[str, Deque[_Delivery]] = {}
        self._active_urls: Set[str] = set()
        self._workers: Set[asyncio.Task[Any]] = set()
        self._pending_count = 0
        self._rejected_count = 0
        self._failed_count = 0
        self._accepting = False

    @property
    def pending_count(self) -> int:
        with self._admission_lock:
            return self._pending_count

    @property
    def rejected_count(self) -> int:
        with self._admission_lock:
            return self._rejected_count

    @property
    def failed_count(self) -> int:
        with self._admission_lock:
            return self._failed_count

    @property
    def worker_count(self) -> int:
        return len(self._workers)

    async def start(self) -> None:
        while True:
            async with self._lifecycle_lock:
                close_task = self._close_task
                if close_task is None:
                    if self._session is not None:
                        return
                    loop = asyncio.get_running_loop()
                    slots = asyncio.Semaphore(self._concurrency)
                    session = self._session_factory(
                        headers=self.headers, cookie_jar=aiohttp.DummyCookieJar()
                    )
                    self._loop = loop
                    self._slots = slots
                    self._session = session
                    with self._admission_lock:
                        self._accepting = True
                    return
            try:
                await asyncio.shield(close_task)
            finally:
                await self._forget_close_task(close_task)

    async def close(self, *, drain_timeout_seconds: float = 5) -> None:
        if drain_timeout_seconds < 0:
            raise ValueError('webhook drain timeout must not be negative')
        with self._admission_lock:
            self._accepting = False
        async with self._lifecycle_lock:
            close_task = self._close_task
            if close_task is None:
                session = self._session
                if session is None:
                    self._loop = None
                    self._slots = None
                    return
                close_task = asyncio.create_task(
                    self._close_owned(session, drain_timeout_seconds)
                )
                self._close_task = close_task
        cancelled = False
        try:
            while True:
                try:
                    await asyncio.shield(close_task)
                    break
                except asyncio.CancelledError:
                    if close_task.done():
                        close_task.result()
                        raise
                    cancelled = True
        finally:
            await self._forget_close_task(close_task)
        if cancelled:
            raise asyncio.CancelledError

    async def _close_owned(self, session: Any, drain_timeout_seconds: float) -> None:
        loop = self._loop
        if loop is not None:
            flushed = loop.create_future()
            loop.call_soon(flushed.set_result, None)
            await flushed
        workers = tuple(self._workers)
        if workers:
            group = asyncio.gather(*workers, return_exceptions=True)
            try:
                await asyncio.wait_for(group, timeout=drain_timeout_seconds)
            except asyncio.TimeoutError:
                for worker in workers:
                    worker.cancel()
                await asyncio.gather(*workers, return_exceptions=True)
        remaining = 0
        for queue in self._queues.values():
            remaining += len(queue)
            queue.clear()
        self._queues.clear()
        self._active_urls.clear()
        self._workers.clear()
        if remaining:
            self._release_pending(remaining)
        await session.close()
        if self._session is session:
            self._session = None
            self._slots = None
            self._loop = None

    async def _forget_close_task(self, task: asyncio.Task[None]) -> None:
        if not task.done():
            return
        async with self._lifecycle_lock:
            if self._close_task is task:
                self._close_task = None

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
        current_loop: Optional[asyncio.AbstractEventLoop]
        try:
            current_loop = asyncio.get_running_loop()
        except RuntimeError:
            current_loop = None
        with self._admission_lock:
            loop = self._loop
            if (
                not self._accepting
                or loop is None
                or not loop.is_running()
                or loop.is_closed()
            ):
                self._rejected_count += 1
                return False
            if self._pending_count >= self._capacity:
                self._rejected_count += 1
                return False
            self._pending_count += 1
            if current_loop is not loop:
                try:
                    loop.call_soon_threadsafe(self._enqueue_reserved, url, payload)
                except RuntimeError:
                    self._pending_count -= 1
                    self._rejected_count += 1
                    return False
                return True
        if current_loop is loop:
            self._enqueue_reserved(url, payload)
        return True

    def _enqueue_reserved(self, url: str, payload: Dict[str, Any]) -> None:
        if self._session is None:
            self._release_pending(1, rejected=True)
            return
        queue = self._queues.setdefault(url, deque())
        queue.append(_Delivery(payload))
        if url not in self._active_urls:
            self._active_urls.add(url)
            worker = asyncio.create_task(self._run_url(url))
            self._workers.add(worker)
            worker.add_done_callback(self._workers.discard)

    def _release_pending(self, count: int, *, rejected: bool = False) -> None:
        with self._admission_lock:
            self._pending_count -= count
            if rejected:
                self._rejected_count += count

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
                        self._release_pending(1)
        finally:
            remaining = len(queue)
            if remaining:
                queue.clear()
                self._release_pending(remaining)
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
        with self._admission_lock:
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
        return isinstance(
            error, (aiohttp.ClientConnectionError, asyncio.TimeoutError, OSError)
        )
