import asyncio
from collections import defaultdict, deque
from typing import Any, Deque, Dict, Iterable, List, Optional, Tuple

import aiohttp
import pytest

from blrec.webhook import webhook_emitter as webhook_module
from blrec.webhook.webhook_emitter import WebHookEmitter


class _Response:
    def __init__(
        self,
        session: '_Session',
        url: str,
        payload: Dict[str, Any],
        outcome: Optional[BaseException],
    ) -> None:
        self._session = session
        self._url = url
        self._payload = payload
        self._outcome = outcome

    async def __aenter__(self) -> '_Response':
        self._session.active += 1
        self._session.active_by_url[self._url] += 1
        self._session.max_active = max(self._session.max_active, self._session.active)
        self._session.max_active_by_url[self._url] = max(
            self._session.max_active_by_url[self._url],
            self._session.active_by_url[self._url],
        )
        self._session.started.set()
        if self._session.release is not None:
            await self._session.release.wait()
        if self._outcome is not None:
            self._session.active -= 1
            self._session.active_by_url[self._url] -= 1
            raise self._outcome
        return self

    async def __aexit__(self, *_args: Any) -> None:
        self._session.active -= 1
        self._session.active_by_url[self._url] -= 1

    def raise_for_status(self) -> None:
        return None


class _Session:
    def __init__(
        self,
        outcomes: Optional[Dict[str, Iterable[Optional[BaseException]]]] = None,
        *,
        release: Optional[asyncio.Event] = None,
    ) -> None:
        self.outcomes: Dict[str, Deque[Optional[BaseException]]] = {
            url: deque(values) for url, values in (outcomes or {}).items()
        }
        self.release = release
        self.started = asyncio.Event()
        self.calls: List[Tuple[str, Dict[str, Any]]] = []
        self.active = 0
        self.max_active = 0
        self.active_by_url: Dict[str, int] = defaultdict(int)
        self.max_active_by_url: Dict[str, int] = defaultdict(int)
        self.close_calls = 0

    def post(
        self, url: str, *, json: Dict[str, Any], timeout: aiohttp.ClientTimeout
    ) -> _Response:
        assert timeout.total is not None and timeout.total <= 10
        self.calls.append((url, json))
        outcomes = self.outcomes.get(url)
        outcome = outcomes.popleft() if outcomes else None
        return _Response(self, url, json, outcome)

    async def close(self) -> None:
        self.close_calls += 1


async def _wait_until(predicate: Any) -> None:
    for _ in range(200):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError('condition was not reached')


def _http_error(status: int) -> aiohttp.ClientResponseError:
    return aiohttp.ClientResponseError(None, (), status=status)


@pytest.mark.asyncio
async def test_queue_is_bounded_and_same_url_delivery_is_ordered() -> None:
    release = asyncio.Event()
    session = _Session(release=release)
    emitter = WebHookEmitter(
        session_factory=lambda **_kwargs: session,
        capacity=100,
        concurrency=4,
        sleeper=lambda _delay: asyncio.sleep(0),
    )
    await emitter.start()

    accepted = [
        emitter._send_request('https://fixture.invalid/hook', {'index': index})
        for index in range(101)
    ]

    assert accepted == [True] * 100 + [False]
    assert emitter.pending_count == 100
    await session.started.wait()
    assert session.max_active_by_url['https://fixture.invalid/hook'] == 1

    release.set()
    await emitter.close(drain_timeout_seconds=1)

    assert [payload['index'] for _url, payload in session.calls] == list(range(100))
    assert emitter.pending_count == 0
    assert emitter.rejected_count == 1
    assert session.close_calls == 1


@pytest.mark.asyncio
async def test_delivery_has_global_concurrency_four_and_per_url_concurrency_one() -> (
    None
):
    release = asyncio.Event()
    session = _Session(release=release)
    emitter = WebHookEmitter(
        session_factory=lambda **_kwargs: session,
        concurrency=4,
        sleeper=lambda _delay: asyncio.sleep(0),
    )
    await emitter.start()

    for index in range(8):
        assert emitter._send_request(
            'https://fixture-{}.invalid/hook'.format(index), {'index': index}
        )
    await _wait_until(lambda: session.active == 4)

    assert session.max_active == 4
    assert all(value == 1 for value in session.max_active_by_url.values())

    release.set()
    await emitter.close(drain_timeout_seconds=1)


@pytest.mark.asyncio
async def test_only_transient_failures_retry_and_attempts_stop_at_three() -> None:
    session = _Session(
        {
            'https://permanent.invalid': [_http_error(400)],
            'https://rate.invalid': [_http_error(429), None],
            'https://server.invalid': [
                _http_error(503),
                _http_error(503),
                _http_error(503),
            ],
            'https://transport.invalid': [OSError('secret payload'), None],
        }
    )
    emitter = WebHookEmitter(
        session_factory=lambda **_kwargs: session,
        sleeper=lambda _delay: asyncio.sleep(0),
    )
    await emitter.start()
    for url in session.outcomes:
        assert emitter._send_request(url, {'secret': 'never-log-me'})

    await emitter.close(drain_timeout_seconds=1)

    counts: Dict[str, int] = defaultdict(int)
    for url, _payload in session.calls:
        counts[url] += 1
    assert counts == {
        'https://permanent.invalid': 1,
        'https://rate.invalid': 2,
        'https://server.invalid': 3,
        'https://transport.invalid': 2,
    }


@pytest.mark.asyncio
async def test_close_cancels_blocked_delivery_and_rejects_new_work() -> None:
    session = _Session(release=asyncio.Event())
    emitter = WebHookEmitter(
        session_factory=lambda **_kwargs: session,
        request_timeout_seconds=10,
        delivery_timeout_seconds=60,
    )
    await emitter.start()
    assert emitter._send_request('https://private.invalid/path', {'token': 'secret'})
    await session.started.wait()

    await emitter.close(drain_timeout_seconds=0.01)

    assert emitter.pending_count == 0
    assert emitter.worker_count == 0
    assert session.close_calls == 1
    assert not emitter._send_request(
        'https://private.invalid/path', {'token': 'secret'}
    )


@pytest.mark.asyncio
async def test_request_timeout_and_delivery_deadline_are_bounded() -> None:
    session = _Session(release=asyncio.Event())
    emitter = WebHookEmitter(
        session_factory=lambda **_kwargs: session,
        request_timeout_seconds=0.01,
        delivery_timeout_seconds=0.03,
        sleeper=lambda _delay: asyncio.sleep(0),
    )
    await emitter.start()
    assert emitter._send_request('https://timeout.invalid', {'value': 1})

    await emitter.close(drain_timeout_seconds=1)

    assert 1 <= len(session.calls) <= 3
    assert emitter.failed_count == 1
    assert session.close_calls == 1


@pytest.mark.asyncio
async def test_failure_logging_redacts_url_payload_and_transport_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    messages: List[str] = []

    class _Logger:
        @staticmethod
        def warning(message: str, *values: Any) -> None:
            messages.append(message.format(*values))

    monkeypatch.setattr(webhook_module, 'logger', _Logger())
    session = _Session({'https://secret.invalid/private': [OSError('payload-secret')]})
    factory_options: Dict[str, Any] = {}

    def session_factory(**options: Any) -> _Session:
        factory_options.update(options)
        return session

    emitter = WebHookEmitter(
        session_factory=session_factory, sleeper=lambda _delay: asyncio.sleep(0)
    )
    await emitter.start()
    assert emitter._send_request(
        'https://secret.invalid/private', {'value': 'payload-secret'}
    )
    await emitter.close(drain_timeout_seconds=1)

    rendered = '\n'.join(messages)
    assert 'secret.invalid' not in rendered
    assert 'payload-secret' not in rendered
    assert isinstance(factory_options['cookie_jar'], aiohttp.DummyCookieJar)
