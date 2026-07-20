from __future__ import annotations

import asyncio
import random
import smtplib
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import (
    Any,
    Awaitable,
    Callable,
    Deque,
    Dict,
    Mapping,
    Optional,
    Set,
    Tuple,
    cast,
)

import aiohttp
from loguru import logger

CoalesceKey = Tuple[str, ...]


@dataclass
class _Delivery:
    channel: str
    title: str
    content: str
    message_type: str
    coalesce_key: Optional[CoalesceKey]
    deadline_at: float


class NotificationChannel:
    def __init__(self, dispatcher: 'NotificationDispatcher', channel: str) -> None:
        self._dispatcher = dispatcher
        self._channel = channel

    def enqueue(
        self,
        title: str,
        content: str,
        message_type: str,
        *,
        coalesce_key: Optional[CoalesceKey] = None,
    ) -> bool:
        return self._dispatcher.enqueue(
            self._channel, title, content, message_type, coalesce_key=coalesce_key
        )

    async def send_message(self, title: str, content: str, message_type: str) -> None:
        self.enqueue(title, content, message_type)


class NotificationDispatcher:
    def __init__(
        self,
        senders: Mapping[str, Any],
        *,
        capacity: int = 100,
        max_concurrency: int = 4,
        delivery_timeout_seconds: float = 60,
        attempt_timeout_seconds: float = 10,
        close_timeout_seconds: float = 15,
        sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        jitter: Callable[[float], float] = lambda upper: random.uniform(0, upper),
        session_factory: Optional[Callable[[], Any]] = None,
    ) -> None:
        if capacity <= 0:
            raise ValueError('notification capacity must be positive')
        if max_concurrency <= 0:
            raise ValueError('notification concurrency must be positive')
        if delivery_timeout_seconds <= 0 or attempt_timeout_seconds <= 0:
            raise ValueError('notification timeouts must be positive')
        if close_timeout_seconds <= 0:
            raise ValueError('notification close timeout must be positive')
        self._senders = dict(senders)
        self._capacity = capacity
        self._delivery_timeout_seconds = delivery_timeout_seconds
        self._attempt_timeout_seconds = attempt_timeout_seconds
        self._close_timeout_seconds = close_timeout_seconds
        self._sleeper = sleeper
        self._monotonic = monotonic
        self._jitter = jitter
        self._session_factory = session_factory
        self._queues: Dict[str, Deque[_Delivery]] = defaultdict(deque)
        self._pending_by_key: Dict[CoalesceKey, _Delivery] = {}
        self._workers: Dict[str, asyncio.Task[None]] = {}
        self._smtp_futures: Set[asyncio.Future[Any]] = set()
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._pending_count = 0
        self._dropped_count = 0
        self._started = False
        self._closing = False
        self._session: Optional[Any] = None

    @property
    def pending_count(self) -> int:
        return self._pending_count

    @property
    def dropped_count(self) -> int:
        return self._dropped_count

    @property
    def owned_task_count(self) -> int:
        return len(self._workers)

    def channel(self, channel: str) -> NotificationChannel:
        if channel not in self._senders:
            raise KeyError(channel)
        return NotificationChannel(self, channel)

    def enqueue(
        self,
        channel: str,
        title: str,
        content: str,
        message_type: str,
        *,
        coalesce_key: Optional[CoalesceKey] = None,
    ) -> bool:
        if self._closing or channel not in self._senders:
            self._dropped_count += 1
            return False
        deadline_at = self._monotonic() + self._delivery_timeout_seconds
        if coalesce_key is not None:
            pending = self._pending_by_key.get(coalesce_key)
            if pending is not None:
                pending.title = title
                pending.content = content
                pending.message_type = message_type
                pending.deadline_at = deadline_at
                return True
        if self._pending_count >= self._capacity:
            self._dropped_count += 1
            return False
        delivery = _Delivery(
            channel=channel,
            title=title,
            content=content,
            message_type=message_type,
            coalesce_key=coalesce_key,
            deadline_at=deadline_at,
        )
        self._queues[channel].append(delivery)
        if coalesce_key is not None:
            self._pending_by_key[coalesce_key] = delivery
        self._pending_count += 1
        if self._started:
            self._ensure_channel_worker(channel)
        return True

    async def start(self) -> None:
        if self._started:
            return
        if self._closing:
            raise RuntimeError('notification dispatcher is closing')
        self._session = self._create_session()
        self._bind_session(self._session)
        self._started = True
        for channel, queue in list(self._queues.items()):
            if queue:
                self._ensure_channel_worker(channel)

    async def close(self, *, drain_timeout_seconds: Optional[float] = None) -> None:
        if self._closing:
            return
        if not self._started and self._session is None and not self._workers:
            return
        self._closing = True
        timeout_seconds = (
            self._close_timeout_seconds
            if drain_timeout_seconds is None
            else drain_timeout_seconds
        )
        try:
            workers = list(self._workers.values())
            if workers:
                try:
                    await asyncio.wait_for(
                        asyncio.gather(*workers, return_exceptions=True),
                        timeout=timeout_seconds,
                    )
                except asyncio.TimeoutError:
                    for worker in list(self._workers.values()):
                        worker.cancel()
                    await asyncio.gather(
                        *list(self._workers.values()), return_exceptions=True
                    )
            if self._smtp_futures:
                await asyncio.gather(*list(self._smtp_futures), return_exceptions=True)
            session = self._session
            if session is not None:
                await session.close()
                if self._session is session:
                    self._session = None
            self._bind_session(None)
        finally:
            self._started = False
            self._closing = False

    def _create_session(self) -> Any:
        if self._session_factory is not None:
            return self._session_factory()
        attempt = self._attempt_timeout_seconds
        return aiohttp.ClientSession(
            cookie_jar=aiohttp.DummyCookieJar(),
            raise_for_status=True,
            trust_env=False,
            timeout=aiohttp.ClientTimeout(
                total=attempt,
                connect=min(5, attempt),
                sock_connect=min(5, attempt),
                sock_read=attempt,
            ),
        )

    def _bind_session(self, session: Optional[Any]) -> None:
        for sender in self._senders.values():
            bind = getattr(sender, 'bind_session', None)
            if bind is not None:
                bind(
                    session,
                    attempt_timeout_seconds=self._attempt_timeout_seconds,
                    monotonic=self._monotonic,
                )

    def _ensure_channel_worker(self, channel: str) -> None:
        worker = self._workers.get(channel)
        if worker is not None and not worker.done():
            return
        worker = asyncio.create_task(self._run_channel(channel))
        self._workers[channel] = worker

        def done(completed: asyncio.Task[None]) -> None:
            self._worker_done(channel, completed)

        worker.add_done_callback(done)

    def _worker_done(self, channel: str, worker: asyncio.Task[None]) -> None:
        if self._workers.get(channel) is worker:
            del self._workers[channel]
        try:
            worker.result()
        except asyncio.CancelledError:
            pass
        except BaseException as error:
            logger.warning(
                'Notification worker channel={} failed error={}',
                channel,
                type(error).__name__,
            )
        if self._started and not self._closing and self._queues[channel]:
            self._ensure_channel_worker(channel)

    async def _run_channel(self, channel: str) -> None:
        queue = self._queues[channel]
        while queue:
            delivery = queue.popleft()
            if delivery.coalesce_key is not None:
                if self._pending_by_key.get(delivery.coalesce_key) is delivery:
                    del self._pending_by_key[delivery.coalesce_key]
            try:
                async with self._semaphore:
                    await self._deliver(delivery)
            finally:
                self._pending_count -= 1

    async def _deliver(self, delivery: _Delivery) -> None:
        sender = self._senders[delivery.channel]
        for attempt in range(1, 4):
            remaining = delivery.deadline_at - self._monotonic()
            if remaining <= 0:
                return
            try:
                await self._send_once(
                    sender, delivery, min(remaining, self._attempt_timeout_seconds)
                )
                return
            except asyncio.CancelledError:
                raise
            except BaseException as error:
                if not self._is_transient(error) or attempt == 3:
                    logger.warning(
                        'Notification delivery channel={} attempt={} error={}',
                        delivery.channel,
                        attempt,
                        type(error).__name__,
                    )
                    return
                delay = min(
                    self._jitter(float(attempt)),
                    max(0, delivery.deadline_at - self._monotonic()),
                )
                if delay > 0:
                    await self._sleeper(delay)

    async def _send_once(
        self, sender: Any, delivery: _Delivery, timeout_seconds: float
    ) -> None:
        send_with_deadline = getattr(sender, 'send_with_deadline', None)
        if send_with_deadline is not None:
            loop = asyncio.get_running_loop()
            attempt_deadline = min(
                delivery.deadline_at, self._monotonic() + timeout_seconds
            )
            future = cast(
                asyncio.Future[Any],
                loop.run_in_executor(
                    None,
                    send_with_deadline,
                    delivery.title,
                    delivery.content,
                    delivery.message_type,
                    attempt_deadline,
                ),
            )
            self._smtp_futures.add(future)
            future.add_done_callback(self._smtp_future_done)
            await asyncio.shield(future)
            return
        await asyncio.wait_for(
            sender.send_message(
                delivery.title, delivery.content, delivery.message_type
            ),
            timeout=timeout_seconds,
        )

    def _smtp_future_done(self, future: asyncio.Future[Any]) -> None:
        self._smtp_futures.discard(future)
        try:
            future.exception()
        except asyncio.CancelledError:
            pass

    @staticmethod
    def _is_transient(error: BaseException) -> bool:
        if isinstance(error, aiohttp.ClientResponseError):
            return error.status == 429 or 500 <= error.status <= 599
        if isinstance(error, (aiohttp.ClientError, asyncio.TimeoutError)):
            return True
        if isinstance(error, smtplib.SMTPAuthenticationError):
            return False
        if isinstance(error, smtplib.SMTPResponseException):
            return 400 <= error.smtp_code <= 499
        return isinstance(error, smtplib.SMTPException)


__all__ = ('NotificationChannel', 'NotificationDispatcher')
