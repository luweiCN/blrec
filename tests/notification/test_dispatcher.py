from __future__ import annotations

import asyncio
import threading
from typing import List, Optional, Tuple

import aiohttp
import pytest

from blrec.notification.dispatcher import NotificationDispatcher


class FakeSession:
    def __init__(self) -> None:
        self.cookie_jar = aiohttp.DummyCookieJar()
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class RecordingSender:
    def __init__(
        self,
        *,
        release: Optional[asyncio.Event] = None,
        errors: Optional[List[BaseException]] = None,
    ) -> None:
        self.release = release
        self.errors = list(errors or [])
        self.calls: List[Tuple[str, str, str]] = []
        self.active = 0
        self.max_active = 0

    async def send_message(self, title: str, content: str, message_type: str) -> None:
        self.calls.append((title, content, message_type))
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            if self.release is not None:
                await self.release.wait()
            if self.errors:
                raise self.errors.pop(0)
        finally:
            self.active -= 1


class BlockingSmtpSender:
    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()

    def send_with_deadline(
        self, _title: str, _content: str, _message_type: str, _deadline_at: float
    ) -> None:
        self.started.set()
        self.release.wait()


@pytest.mark.asyncio
async def test_prestart_capacity_is_bounded_without_creating_tasks() -> None:
    sender = RecordingSender()
    dispatcher = NotificationDispatcher({'email': sender}, session_factory=FakeSession)

    accepted = [
        dispatcher.enqueue('email', str(index), 'body', 'text')
        for index in range(1_000)
    ]

    assert sum(accepted) == 100
    assert dispatcher.pending_count == 100
    assert dispatcher.dropped_count == 900
    assert dispatcher.owned_task_count == 0

    await dispatcher.start()
    await dispatcher.close()

    assert [call[0] for call in sender.calls] == [str(index) for index in range(100)]
    assert dispatcher.pending_count == 0


@pytest.mark.asyncio
async def test_channel_order_and_global_concurrency_are_bounded() -> None:
    releases = {name: asyncio.Event() for name in ('a', 'b', 'c', 'd', 'e')}
    senders = {
        name: RecordingSender(release=releases[name])
        for name in ('a', 'b', 'c', 'd', 'e')
    }
    dispatcher = NotificationDispatcher(senders, session_factory=FakeSession)
    await dispatcher.start()
    for channel in senders:
        assert dispatcher.enqueue(channel, '{}-1'.format(channel), '', 'text')
        assert dispatcher.enqueue(channel, '{}-2'.format(channel), '', 'text')

    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert sum(sender.active for sender in senders.values()) == 4
    assert all(sender.max_active <= 1 for sender in senders.values())
    assert dispatcher.owned_task_count == 5

    for event in releases.values():
        event.set()
    await dispatcher.close()

    assert all(
        [call[0] for call in sender.calls] == ['{}-1'.format(name), '{}-2'.format(name)]
        for name, sender in senders.items()
    )


@pytest.mark.asyncio
async def test_keyed_pending_delivery_is_replaced_without_growing_queue() -> None:
    sender = RecordingSender()
    dispatcher = NotificationDispatcher({'email': sender}, session_factory=FakeSession)
    key = ('account_unavailable', 'account:1', 'email')

    assert dispatcher.enqueue('email', 'old', 'old body', 'text', coalesce_key=key)
    assert dispatcher.enqueue('email', 'new', 'new body', 'html', coalesce_key=key)
    assert dispatcher.pending_count == 1

    await dispatcher.start()
    await dispatcher.close()

    assert sender.calls == [('new', 'new body', 'html')]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('error', 'attempts'),
    [
        (ValueError('bad config'), 1),
        (aiohttp.ClientResponseError(None, (), status=400), 1),
        (aiohttp.ClientResponseError(None, (), status=429), 3),
        (aiohttp.ClientResponseError(None, (), status=503), 3),
        (aiohttp.ClientConnectionError('offline'), 3),
        (asyncio.TimeoutError(), 3),
    ],
)
async def test_retry_classification_is_bounded(
    error: BaseException, attempts: int
) -> None:
    sender = RecordingSender(errors=[error, error, error])
    sleeps: List[float] = []

    async def sleeper(delay: float) -> None:
        sleeps.append(delay)

    dispatcher = NotificationDispatcher(
        {'email': sender},
        sleeper=sleeper,
        jitter=lambda upper: upper,
        session_factory=FakeSession,
    )
    assert dispatcher.enqueue('email', 'title', 'body', 'text')

    await dispatcher.start()
    await dispatcher.close()

    assert len(sender.calls) == attempts
    assert sleeps == [1, 2][: attempts - 1]


@pytest.mark.asyncio
async def test_channel_adapter_only_enqueues_and_close_restarts_cleanly() -> None:
    sender = RecordingSender()
    sessions: List[FakeSession] = []

    def session_factory() -> FakeSession:
        session = FakeSession()
        sessions.append(session)
        return session

    dispatcher = NotificationDispatcher(
        {'email': sender}, session_factory=session_factory
    )
    channel = dispatcher.channel('email')

    assert channel.enqueue('first', 'body', 'text')
    await dispatcher.start()
    await dispatcher.close()
    assert sessions[0].closed

    assert channel.enqueue('second', 'body', 'text')
    await dispatcher.start()
    await dispatcher.close()

    assert len(sessions) == 2
    assert all(session.closed for session in sessions)
    assert [call[0] for call in sender.calls] == ['first', 'second']


@pytest.mark.asyncio
async def test_close_observes_running_smtp_executor_future() -> None:
    sender = BlockingSmtpSender()
    dispatcher = NotificationDispatcher(
        {'email': sender}, close_timeout_seconds=0.01, session_factory=FakeSession
    )
    await dispatcher.start()
    assert dispatcher.enqueue('email', 'title', 'body', 'text')
    await asyncio.get_running_loop().run_in_executor(None, sender.started.wait)

    close_task = asyncio.create_task(dispatcher.close())
    await asyncio.sleep(0.02)

    assert not close_task.done()
    sender.release.set()
    await close_task
    assert dispatcher.pending_count == 0
