import asyncio
import gc
from collections import deque
from typing import Any, Deque, Dict, List, Mapping, Optional

import aiohttp
import pytest

from blrec.update.helpers import UpdateMetadataClient


class _Clock:
    def __init__(self) -> None:
        self.value = 1_000.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class _Response:
    def __init__(
        self, session: '_Session', outcome: Any, release: Optional[asyncio.Event]
    ) -> None:
        self._session = session
        self._outcome = outcome
        self._release = release

    async def __aenter__(self) -> '_Response':
        self._session.started.set()
        if self._release is not None:
            await self._release.wait()
        if isinstance(self._outcome, BaseException):
            raise self._outcome
        return self

    async def __aexit__(self, *_args: Any) -> None:
        return None

    async def json(self) -> Mapping[str, Any]:
        return self._outcome


class _Session:
    def __init__(
        self, outcomes: List[Any], *, release: Optional[asyncio.Event] = None
    ) -> None:
        self.outcomes: Deque[Any] = deque(outcomes)
        self.release = release
        self.started = asyncio.Event()
        self.calls: List[str] = []
        self.close_calls = 0

    def get(
        self, url: str, *, raise_for_status: bool, timeout: aiohttp.ClientTimeout
    ) -> _Response:
        assert raise_for_status
        assert timeout.total is not None and timeout.total <= 10
        self.calls.append(url)
        return _Response(self, self.outcomes.popleft(), self.release)

    async def close(self) -> None:
        self.close_calls += 1


class _BlockingCloseSession(_Session):
    def __init__(self) -> None:
        super().__init__([])
        self.close_started = asyncio.Event()
        self.close_release = asyncio.Event()
        self.closed = False

    async def close(self) -> None:
        self.close_calls += 1
        self.close_started.set()
        await self.close_release.wait()
        self.closed = True


class _FlakyCloseSession(_Session):
    def __init__(self) -> None:
        super().__init__([])
        self.closed = False

    async def close(self) -> None:
        self.close_calls += 1
        if self.close_calls == 1:
            raise OSError('close failed')
        self.closed = True


def _metadata(version: str) -> Dict[str, Any]:
    return {'info': {'version': version}}


def _not_found() -> aiohttp.ClientResponseError:
    return aiohttp.ClientResponseError(None, (), status=404)


@pytest.mark.asyncio
async def test_same_key_is_singleflight_and_fresh_for_thirty_minutes() -> None:
    release = asyncio.Event()
    session = _Session([_metadata('2.0.0')], release=release)
    clock = _Clock()
    client = UpdateMetadataClient(
        session_factory=lambda **_kwargs: session, monotonic=clock
    )
    await client.start()

    requests = [
        asyncio.create_task(client.get_latest_version_string('blrec'))
        for _ in range(20)
    ]
    await session.started.wait()
    assert len(session.calls) == 1
    release.set()
    assert await asyncio.gather(*requests) == ['2.0.0'] * 20

    clock.advance(1_799)
    assert await client.get_latest_version_string('blrec') == '2.0.0'
    assert len(session.calls) == 1
    await client.close()


@pytest.mark.asyncio
async def test_expiry_refreshes_once_and_error_uses_only_nonexpired_stale() -> None:
    session = _Session(
        [_metadata('1.0.0'), _metadata('2.0.0'), OSError('private'), OSError('private')]
    )
    clock = _Clock()
    client = UpdateMetadataClient(
        session_factory=lambda **_kwargs: session, monotonic=clock
    )
    await client.start()

    assert await client.get_latest_version_string('blrec') == '1.0.0'
    clock.advance(1_801)
    assert await client.get_latest_version_string('blrec') == '2.0.0'
    clock.advance(1_801)
    assert await client.get_latest_version_string('blrec') == '2.0.0'
    clock.advance(86_400)
    with pytest.raises(OSError, match='private'):
        await client.get_latest_version_string('blrec')
    assert len(session.calls) == 4
    await client.close()


@pytest.mark.asyncio
async def test_failed_refresh_is_cooled_down_for_thirty_minutes() -> None:
    session = _Session(
        [_metadata('1.0.0'), OSError('first failure'), OSError('second failure')]
    )
    clock = _Clock()
    client = UpdateMetadataClient(
        session_factory=lambda **_kwargs: session, monotonic=clock
    )
    await client.start()

    assert await client.get_latest_version_string('blrec') == '1.0.0'
    clock.advance(1_801)
    assert await client.get_latest_version_string('blrec') == '1.0.0'
    assert await client.get_latest_version_string('blrec') == '1.0.0'
    clock.advance(1_799)
    assert await client.get_latest_version_string('blrec') == '1.0.0'
    assert len(session.calls) == 2

    clock.advance(1)
    assert await client.get_latest_version_string('blrec') == '1.0.0'
    assert len(session.calls) == 3
    await client.close()


@pytest.mark.asyncio
async def test_failed_refresh_cooldown_does_not_extend_stale_lifetime() -> None:
    session = _Session(
        [_metadata('1.0.0'), OSError('near expiry'), OSError('must not be sent')]
    )
    clock = _Clock()
    client = UpdateMetadataClient(
        session_factory=lambda **_kwargs: session, monotonic=clock
    )
    await client.start()

    assert await client.get_latest_version_string('blrec') == '1.0.0'
    clock.advance(client.STALE_SECONDS - 1)
    assert await client.get_latest_version_string('blrec') == '1.0.0'
    clock.advance(1)
    assert await client.get_latest_version_string('blrec') == '1.0.0'
    assert len(session.calls) == 2

    clock.advance(0.001)
    with pytest.raises(RuntimeError, match='cooling down'):
        await client.get_latest_version_string('blrec')
    assert len(session.calls) == 2
    await client.close()


@pytest.mark.asyncio
async def test_not_found_is_cached_and_project_release_keys_do_not_collide() -> None:
    session = _Session([_not_found(), _metadata('release'), _metadata('project')])
    client = UpdateMetadataClient(session_factory=lambda **_kwargs: session)
    await client.start()

    assert await client.get_project_metadata('missing') is None
    assert await client.get_project_metadata('missing') is None
    assert await client.get_release_metadata('name', 'version') == _metadata('release')
    assert await client.get_project_metadata('name/version') == _metadata('project')
    assert len(session.calls) == 3
    await client.close()


@pytest.mark.asyncio
async def test_waiter_cancellation_does_not_cancel_shared_refresh() -> None:
    release = asyncio.Event()
    session = _Session([_metadata('3.0.0')], release=release)
    client = UpdateMetadataClient(session_factory=lambda **_kwargs: session)
    await client.start()
    cancelled = asyncio.create_task(client.get_latest_version_string('blrec'))
    survivor = asyncio.create_task(client.get_latest_version_string('blrec'))
    await session.started.wait()

    cancelled.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled
    release.set()

    assert await survivor == '3.0.0'
    assert len(session.calls) == 1
    await client.close()


@pytest.mark.asyncio
async def test_cancelled_only_waiter_consumes_background_refresh_failure() -> None:
    release = asyncio.Event()
    session = _Session([OSError('secret response body')], release=release)
    client = UpdateMetadataClient(session_factory=lambda **_kwargs: session)
    await client.start()
    loop = asyncio.get_running_loop()
    previous_handler = loop.get_exception_handler()
    contexts: List[Dict[str, Any]] = []
    loop.set_exception_handler(lambda _loop, context: contexts.append(context))
    try:
        waiter = asyncio.create_task(client.get_project_metadata('secret-project'))
        await session.started.wait()
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        del waiter

        release.set()
        for _ in range(20):
            if client.inflight_count == 0:
                break
            await asyncio.sleep(0)
        assert client.inflight_count == 0
        await client.close()
        for _ in range(5):
            gc.collect()
            await asyncio.sleep(0)

        assert contexts == []
    finally:
        loop.set_exception_handler(previous_handler)


@pytest.mark.asyncio
async def test_close_cancels_owned_refresh_and_closes_session_once() -> None:
    session = _Session([_metadata('never')], release=asyncio.Event())
    client = UpdateMetadataClient(
        session_factory=lambda **_kwargs: session, request_timeout_seconds=10
    )
    await client.start()
    refresh = asyncio.create_task(client.get_latest_version_string('blrec'))
    await session.started.wait()

    await client.close()

    with pytest.raises(asyncio.CancelledError):
        await refresh
    assert client.inflight_count == 0
    assert session.close_calls == 1


@pytest.mark.asyncio
async def test_invalid_metadata_is_not_cached() -> None:
    session = _Session([{'info': {}}, _metadata('valid')])
    clock = _Clock()
    client = UpdateMetadataClient(
        session_factory=lambda **_kwargs: session, monotonic=clock
    )
    await client.start()

    with pytest.raises(ValueError, match='invalid'):
        await client.get_project_metadata('blrec')
    with pytest.raises(RuntimeError, match='cooling down'):
        await client.get_latest_version_string('blrec')
    assert len(session.calls) == 1

    clock.advance(client.FRESH_SECONDS)
    assert await client.get_latest_version_string('blrec') == 'valid'
    assert len(session.calls) == 2
    await client.close()


@pytest.mark.asyncio
async def test_request_has_absolute_timeout_and_cookie_less_session() -> None:
    session = _Session([_metadata('never')], release=asyncio.Event())
    options: Dict[str, Any] = {}

    def session_factory(**values: Any) -> _Session:
        options.update(values)
        return session

    client = UpdateMetadataClient(
        session_factory=session_factory, request_timeout_seconds=0.01
    )
    await client.start()

    with pytest.raises(asyncio.TimeoutError):
        await client.get_latest_version_string('blrec')
    assert len(session.calls) == 1
    assert isinstance(options['cookie_jar'], aiohttp.DummyCookieJar)
    await client.close()


@pytest.mark.asyncio
async def test_cancelled_close_waiter_does_not_cancel_session_close() -> None:
    session = _BlockingCloseSession()
    client = UpdateMetadataClient(session_factory=lambda **_kwargs: session)
    await client.start()
    closing = asyncio.create_task(client.close())
    await session.close_started.wait()

    closing.cancel()
    session.close_release.set()
    with pytest.raises(asyncio.CancelledError):
        await closing

    assert session.closed
    assert session.close_calls == 1
    await client.close()
    assert session.close_calls == 1


@pytest.mark.asyncio
async def test_failed_session_close_can_be_retried() -> None:
    session = _FlakyCloseSession()
    client = UpdateMetadataClient(session_factory=lambda **_kwargs: session)
    await client.start()

    with pytest.raises(OSError, match='close failed'):
        await client.close()
    await client.close()

    assert session.closed
    assert session.close_calls == 2


@pytest.mark.asyncio
async def test_concurrent_close_callers_share_one_session_close() -> None:
    session = _BlockingCloseSession()
    client = UpdateMetadataClient(session_factory=lambda **_kwargs: session)
    await client.start()
    first = asyncio.create_task(client.close())
    second = asyncio.create_task(client.close())
    await session.close_started.wait()

    session.close_release.set()
    await asyncio.gather(first, second)

    assert session.closed
    assert session.close_calls == 1
