from __future__ import annotations

import asyncio
import smtplib
import socket
import ssl
import threading
import time
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


class BlockingCloseSession(FakeSession):
    def __init__(self) -> None:
        super().__init__()
        self.close_started = asyncio.Event()
        self.close_release = asyncio.Event()
        self.close_calls = 0
        self.active_closes = 0

    async def close(self) -> None:
        self.close_calls += 1
        self.active_closes += 1
        self.close_started.set()
        try:
            await self.close_release.wait()
            self.closed = True
        finally:
            self.active_closes -= 1


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


class DeadlineAwareSmtpSender:
    def __init__(self, attempt_seconds: float) -> None:
        self.attempt_seconds = attempt_seconds
        self.started = threading.Event()
        self.deadlines: List[float] = []

    def send_with_deadline(
        self, _title: str, _content: str, _message_type: str, deadline_at: float
    ) -> None:
        self.deadlines.append(deadline_at)
        self.started.set()
        time.sleep(min(self.attempt_seconds, max(0, deadline_at - time.monotonic())))


class SerialSmtpSender:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.release = threading.Event()
        self.started = threading.Event()
        self.calls: List[str] = []
        self.active = 0
        self.max_active = 0

    def send_with_deadline(
        self, title: str, _content: str, _message_type: str, _deadline_at: float
    ) -> None:
        with self._lock:
            self.calls.append(title)
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            self.started.set()
        self.release.wait()
        with self._lock:
            self.active -= 1


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


def test_restart_rebinds_global_concurrency_to_the_running_loop() -> None:
    senders = {name: RecordingSender() for name in ('a', 'b', 'c', 'd', 'e')}
    dispatcher = NotificationDispatcher(senders, session_factory=FakeSession)

    async def run_cycle(cycle: int) -> None:
        release = asyncio.Event()
        for sender in senders.values():
            sender.release = release
        await dispatcher.start()
        for channel in senders:
            assert dispatcher.enqueue(channel, str(cycle), '', 'text')

        for _ in range(100):
            if sum(sender.active for sender in senders.values()) == 4:
                break
            await asyncio.sleep(0)
        assert sum(sender.active for sender in senders.values()) == 4
        release.set()
        await dispatcher.close()

    asyncio.run(run_cycle(1))
    asyncio.run(run_cycle(2))

    assert all(
        [call[0] for call in sender.calls] == ['1', '2'] for sender in senders.values()
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
        (ConnectionRefusedError('connection refused'), 3),
        (socket.timeout('timed out'), 3),
        (socket.gaierror(-2, 'name lookup failed'), 3),
        (smtplib.SMTPNotSupportedError('AUTH unsupported'), 1),
        (ssl.SSLCertVerificationError(1, 'certificate verify failed'), 1),
        (
            smtplib.SMTPRecipientsRefused(
                {'permanent@example.com': (550, b'mailbox unavailable')}
            ),
            1,
        ),
        (
            smtplib.SMTPRecipientsRefused(
                {'transient@example.com': (450, b'mailbox busy')}
            ),
            3,
        ),
        (
            smtplib.SMTPRecipientsRefused(
                {
                    'transient@example.com': (450, b'mailbox busy'),
                    'permanent@example.com': (550, b'mailbox unavailable'),
                }
            ),
            1,
        ),
        (smtplib.SMTPRecipientsRefused({}), 1),
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
async def test_close_budget_does_not_wait_unbounded_for_smtp_executor() -> None:
    sender = BlockingSmtpSender()
    dispatcher = NotificationDispatcher(
        {'email': sender}, close_timeout_seconds=0.03, session_factory=FakeSession
    )
    await dispatcher.start()
    assert dispatcher.enqueue('email', 'title', 'body', 'text')
    await asyncio.get_running_loop().run_in_executor(None, sender.started.wait)

    started_at = time.monotonic()
    await dispatcher.close()
    elapsed = time.monotonic() - started_at

    assert elapsed < 0.15
    assert dispatcher.pending_count == 0
    assert dispatcher._smtp_futures

    sender.release.set()
    for _ in range(100):
        if not dispatcher._smtp_futures:
            break
        await asyncio.sleep(0.001)
    assert not dispatcher._smtp_futures


@pytest.mark.asyncio
async def test_close_deadline_limits_late_smtp_attempts() -> None:
    sender = DeadlineAwareSmtpSender(attempt_seconds=0.08)
    dispatcher = NotificationDispatcher(
        {'email': sender}, close_timeout_seconds=0.1, session_factory=FakeSession
    )
    await dispatcher.start()
    assert dispatcher.enqueue('email', 'first', 'body', 'text')
    assert dispatcher.enqueue('email', 'second', 'body', 'text')
    await asyncio.get_running_loop().run_in_executor(None, sender.started.wait)

    started_at = time.monotonic()
    await dispatcher.close()
    elapsed = time.monotonic() - started_at

    assert elapsed < 0.15
    assert len(sender.deadlines) == 2
    assert sender.deadlines[1] <= started_at + 0.11
    assert dispatcher.pending_count == 0


@pytest.mark.asyncio
async def test_restart_serializes_smtp_left_running_by_close_timeout() -> None:
    sender = SerialSmtpSender()
    dispatcher = NotificationDispatcher(
        {'email': sender}, close_timeout_seconds=0.01, session_factory=FakeSession
    )
    await dispatcher.start()
    assert dispatcher.enqueue('email', 'old', 'body', 'text')
    await asyncio.get_running_loop().run_in_executor(None, sender.started.wait)

    await dispatcher.close()
    assert sender.active == 1

    sender.started.clear()
    await dispatcher.start()
    assert dispatcher.enqueue('email', 'new', 'body', 'text')
    try:
        await asyncio.sleep(0.02)

        assert sender.calls == ['old']
        assert sender.max_active == 1
    finally:
        sender.release.set()
        for _ in range(100):
            if sender.calls == ['old', 'new'] and sender.active == 0:
                break
            await asyncio.sleep(0.001)
        await dispatcher.close(drain_timeout_seconds=0.2)

    assert sender.calls == ['old', 'new']
    assert sender.max_active == 1


@pytest.mark.asyncio
async def test_timed_out_session_close_remains_owned_until_it_finishes() -> None:
    session = BlockingCloseSession()
    dispatcher = NotificationDispatcher(
        {}, close_timeout_seconds=0.01, session_factory=lambda: session
    )
    await dispatcher.start()

    started_at = time.monotonic()
    await dispatcher.close()
    assert time.monotonic() - started_at < 0.15
    close_task = getattr(dispatcher, '_session_close_task', None)
    try:
        assert close_task is not None
        assert not close_task.done()
        assert session.active_closes == 1
    finally:
        session.close_release.set()
        await asyncio.wait_for(session.close_started.wait(), timeout=0.2)
        if close_task is not None:
            await asyncio.wait_for(asyncio.shield(close_task), timeout=0.2)

    await asyncio.sleep(0)
    assert dispatcher._session_close_task is None
    assert session.close_calls == 1


@pytest.mark.asyncio
async def test_restart_does_not_overlap_timed_out_session_close() -> None:
    first = BlockingCloseSession()
    sessions: List[FakeSession] = []

    def session_factory() -> FakeSession:
        session: FakeSession = first if not sessions else FakeSession()
        sessions.append(session)
        return session

    dispatcher = NotificationDispatcher(
        {}, close_timeout_seconds=0.01, session_factory=session_factory
    )
    await dispatcher.start()
    await dispatcher.close()

    started_at = time.monotonic()
    try:
        with pytest.raises(asyncio.TimeoutError):
            await dispatcher.start()
        assert time.monotonic() - started_at < 0.15
        assert sessions == [first]
        assert first.active_closes == 1
    finally:
        first.close_release.set()
        close_task = getattr(dispatcher, '_session_close_task', None)
        if close_task is not None:
            await asyncio.wait_for(asyncio.shield(close_task), timeout=0.2)
        if dispatcher._started:
            await dispatcher.close(drain_timeout_seconds=0.2)

    await dispatcher.start()
    assert len(sessions) == 2
    await dispatcher.close(drain_timeout_seconds=0.2)
