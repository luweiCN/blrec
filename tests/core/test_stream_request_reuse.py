from __future__ import annotations

import asyncio
import io
from dataclasses import replace
from typing import Any, Optional
from unittest.mock import Mock

import pytest
import reactivex
import requests

import blrec.setting  # noqa: F401 - initializes core imports
from blrec.bili.live import Live
from blrec.bili.live_monitor import LiveEventListener, LiveMonitor
from blrec.core.operators.request_exception_handler import RequestExceptionHandler
from blrec.core.operators.stream_fetcher import StreamFetcher
from blrec.core.operators.stream_url_resolver import StreamURLResolver
from blrec.core.stream_param_holder import StreamParamHolder, StreamParams
from blrec.core.stream_recorder import StreamRecorder


def stream_data() -> list[dict[str, Any]]:
    return [
        {
            'format': [
                {
                    'format_name': 'flv',
                    'codec': [
                        {
                            'codec_name': 'avc',
                            'accept_qn': [10000],
                            'current_qn': 10000,
                            'base_url': '/live.flv',
                            'url_info': [
                                {
                                    'host': 'https://cn-gotcha04.example',
                                    'extra': '?qn=10000',
                                }
                            ],
                        }
                    ],
                },
                {
                    'format_name': 'fmp4',
                    'codec': [
                        {
                            'codec_name': 'avc',
                            'accept_qn': [10000],
                            'current_qn': 10000,
                            'base_url': '/live.m3u8',
                            'url_info': [
                                {
                                    'host': 'https://cn-gotcha04.example',
                                    'extra': '?qn=10000',
                                }
                            ],
                        }
                    ],
                },
            ]
        }
    ]


class FakeImpl:
    def __init__(self) -> None:
        self.quality_number = 10000
        self.hls_stream_available_time = None
        self.stream_available_time = None
        self.seeded = None
        self.started = 0

    def seed_stream_resolution(self, resolution: object) -> None:
        self.seeded = resolution

    async def start(self) -> None:
        self.started += 1


def make_live(
    monkeypatch: pytest.MonkeyPatch,
    clock: list[float],
    streams: Optional[list[dict[str, Any]]] = None,
) -> tuple[Live, list[int]]:
    live = object.__new__(Live)
    live._room_id = 100
    live._monotonic = lambda: clock[0]
    live._real_quality_number = None
    live._no_flv_stream = False
    live._stream_headers = {'User-Agent': 'fixture'}
    calls: list[int] = []

    async def get_live_streams(
        qn: int = 10000, api_platform: str = 'web'
    ) -> list[dict[str, Any]]:
        calls.append(qn)
        return stream_data() if streams is None else streams

    monkeypatch.setattr(live, 'get_live_streams', get_live_streams)
    return live, calls


def make_recorder(
    live: Live, monitor: LiveMonitor, stream_format: str
) -> StreamRecorder:
    recorder = object.__new__(StreamRecorder)
    recorder._stopped = True
    recorder._stopped_lock = asyncio.Lock()
    recorder._live = live
    recorder._live_monitor = monitor
    recorder._logger = Mock()
    recorder._impl = FakeImpl()
    recorder._pending_stream_snapshot = None
    recorder.stream_format = stream_format
    recorder.fmp4_stream_timeout = 10
    recorder._change_impl = lambda _stream_format: None  # type: ignore[method-assign]
    return recorder


class StartRecorderOnAvailable:
    def __init__(self, recorder: StreamRecorder) -> None:
        self._recorder = recorder

    async def on_live_stream_snapshot_available(
        self, live: Live, snapshot: object
    ) -> None:
        await self._recorder.start_with_stream_snapshot(
            snapshot  # type: ignore[arg-type]
        )


class LegacyStreamListener(LiveEventListener):
    def __init__(self) -> None:
        self.calls = 0

    async def on_live_stream_available(self, live: Live) -> None:
        self.calls += 1


@pytest.mark.asyncio
async def test_flv_monitor_snapshot_is_seeded_without_second_play_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = [100.0]
    live, play_calls = make_live(monkeypatch, clock)
    monitor = LiveMonitor(Mock(), live)
    monitor.configure_stream_request(10000, stream_format='flv')
    recorder = make_recorder(live, monitor, 'flv')
    monitor.add_listener(StartRecorderOnAvailable(recorder))  # type: ignore[arg-type]

    await monitor._check_if_stream_available()

    assert len(play_calls) == 1
    assert recorder._impl.started == 1
    assert recorder._impl.seeded.url.endswith('/live.flv?qn=10000')


@pytest.mark.asyncio
async def test_snapshot_event_bridges_to_legacy_one_argument_listener(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    live, _play_calls = make_live(monkeypatch, [100.0])
    monitor = LiveMonitor(Mock(), live)
    listener = LegacyStreamListener()
    monitor.add_listener(listener)

    await monitor._check_if_stream_available()

    assert listener.calls == 1


@pytest.mark.asyncio
async def test_unselectable_monitor_snapshot_falls_through_to_existing_resolver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    streams = stream_data()
    streams[0]['format'][0]['codec'][0]['codec_name'] = 'hevc'
    clock = [100.0]
    live, play_calls = make_live(monkeypatch, clock, streams)
    monitor = LiveMonitor(Mock(), live)
    monitor.configure_stream_request(10000, stream_format='flv')
    recorder = make_recorder(live, monitor, 'flv')
    snapshot = await live.get_live_stream_snapshot(
        10000, stream_format='flv', stream_codec='avc'
    )

    await recorder.start_with_stream_snapshot(snapshot)

    assert len(play_calls) == 1
    assert recorder._impl.seeded is None
    assert recorder._impl.started == 1


@pytest.mark.asyncio
async def test_malformed_monitor_snapshot_falls_through_to_existing_resolver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    streams = stream_data()
    streams[0]['format'][0]['codec'][0]['url_info'] = []
    live, play_calls = make_live(monkeypatch, [100.0], streams)
    monitor = LiveMonitor(Mock(), live)
    recorder = make_recorder(live, monitor, 'flv')
    snapshot = await live.get_live_stream_snapshot(10000, stream_format='flv')

    await recorder.start_with_stream_snapshot(snapshot)

    assert len(play_calls) == 1
    assert recorder._impl.seeded is None
    assert recorder._impl.started == 1


@pytest.mark.asyncio
async def test_fmp4_monitor_snapshot_counts_as_first_debounce_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = [100.0]
    live, play_calls = make_live(monkeypatch, clock)
    monitor = LiveMonitor(Mock(), live)
    monitor.configure_stream_request(10000, stream_format='fmp4')
    recorder = make_recorder(live, monitor, 'fmp4')
    monitor.add_listener(StartRecorderOnAvailable(recorder))  # type: ignore[arg-type]

    async def advance_one_second(_seconds: float) -> None:
        clock[0] += 1

    monkeypatch.setattr('blrec.core.stream_recorder.asyncio.sleep', advance_one_second)

    await monitor._check_if_stream_available()

    assert len(play_calls) == 2
    assert recorder._impl.started == 1
    assert recorder._impl.seeded.url.endswith('/live.m3u8?qn=10000')


@pytest.mark.asyncio
async def test_failed_fmp4_confirmation_does_not_reseed_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = [100.0]
    live, play_calls = make_live(monkeypatch, clock)
    monitor = LiveMonitor(Mock(), live)
    recorder = make_recorder(live, monitor, 'fmp4')
    recorder.fmp4_stream_timeout = 0
    snapshot = await live.get_live_stream_snapshot(10000, stream_format='fmp4')

    async def fail_confirmation(*args: object, **kwargs: object) -> object:
        play_calls.append(10000)
        raise RuntimeError('confirmation failed')

    async def advance_one_second(seconds: float) -> None:
        clock[0] += seconds

    monkeypatch.setattr(live, 'get_live_streams', fail_confirmation)
    monkeypatch.setattr('blrec.core.stream_recorder.asyncio.sleep', advance_one_second)
    monkeypatch.setattr('blrec.core.stream_recorder.time.monotonic', lambda: clock[0])

    await recorder.start_with_stream_snapshot(snapshot)

    assert play_calls == [10000, 10000]
    assert recorder._impl.seeded is None
    assert recorder._impl.started == 1


@pytest.mark.asyncio
async def test_no_flv_fallback_still_waits_and_confirms_fmp4(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    streams = stream_data()
    streams[0]['format'] = [streams[0]['format'][1]]
    clock = [100.0]
    live, play_calls = make_live(monkeypatch, clock, streams)
    monitor = LiveMonitor(Mock(), live)
    monitor.configure_stream_request(10000, stream_format='flv')
    recorder = make_recorder(live, monitor, 'flv')
    monitor.add_listener(StartRecorderOnAvailable(recorder))  # type: ignore[arg-type]
    sleeps: list[float] = []

    async def advance_one_second(seconds: float) -> None:
        sleeps.append(seconds)
        clock[0] += seconds

    monkeypatch.setattr('blrec.core.stream_recorder.asyncio.sleep', advance_one_second)

    await monitor._check_if_stream_available()

    assert play_calls == [10000, 10000]
    assert sleeps == [1]
    assert recorder._impl.seeded.url.endswith('/live.m3u8?qn=10000')
    assert recorder._impl.started == 1


@pytest.mark.asyncio
async def test_seeded_resolver_only_performs_the_real_stream_get(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = [100.0]
    live, _play_calls = make_live(monkeypatch, clock)
    snapshot = await live.get_live_stream_snapshot(10000)
    resolution = await live.resolve_live_stream(10000, snapshot=snapshot)
    holder = StreamParamHolder(quality_number=10000)

    class Response:
        raw = io.BytesIO(b'video')

        def raise_for_status(self) -> None:
            return None

    class Session:
        def __init__(self) -> None:
            self.get_calls = 0

        def get(self, *args: object, **kwargs: object) -> Response:
            self.get_calls += 1
            return Response()

    session = Session()
    resolver = StreamURLResolver(
        live, session, Mock(), holder  # type: ignore[arg-type]
    )
    resolver.seed(replace(resolution, real_quality_number=250))
    holder.reset()  # The real recorder creates its source before applying resolver.
    fetcher = StreamFetcher(live, session)  # type: ignore[arg-type]
    values: list[io.RawIOBase] = []
    errors: list[Exception] = []

    resolver(reactivex.just(StreamParams('flv', 10000, 'web', False))).pipe(
        fetcher
    ).subscribe(values.append, errors.append)

    assert errors == []
    assert len(values) == 1
    assert session.get_calls == 1
    assert holder.real_quality_number == 250


@pytest.mark.asyncio
async def test_real_403_resets_seed_and_rotates_before_fresh_resolution() -> None:
    class FakeLive:
        stream_headers: dict[str, str] = {}

        def __init__(self) -> None:
            self.calls: list[bool] = []

        async def resolve_live_stream(
            self, *args: object, select_alternative: bool = False, **kwargs: object
        ) -> object:
            self.calls.append(select_alternative)

            class Resolution:
                url = 'https://alternative.example/live.flv'
                real_quality_number = 10000

            return Resolution()

    live = FakeLive()
    holder = StreamParamHolder(quality_number=10000)
    session = Mock()
    session.get.side_effect = AssertionError('resolver must not validate the URL')
    resolver = StreamURLResolver(
        live, session, Mock(), holder  # type: ignore[arg-type]
    )

    def call_immediately(coro: Any) -> object:
        try:
            coro.send(None)
        except StopIteration as completed:
            return completed.value
        raise AssertionError('fake coroutine unexpectedly suspended')

    resolver._call_coroutine = call_immediately  # type: ignore[method-assign]

    class Seed:
        quality_number = 10000
        api_platform = 'web'
        stream_format = 'flv'
        stream_codec = 'avc'
        select_alternative = False
        url = 'https://primary.example/live.flv'
        real_quality_number = 10000

    resolver.seed(Seed())  # type: ignore[arg-type]
    response = requests.Response()
    response.status_code = 403
    error = requests.HTTPError(response=response)

    RequestExceptionHandler(resolver)._before_retry(error)
    values: list[str] = []
    errors: list[Exception] = []
    resolver(reactivex.just(StreamParams('flv', 10000, 'web', True))).subscribe(
        values.append, errors.append
    )

    assert errors == []
    assert values == ['https://alternative.example/live.flv']
    assert live.calls == [True]
    session.get.assert_not_called()


@pytest.mark.asyncio
async def test_connection_failure_resets_seed_before_retrying_transfer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeLive:
        stream_headers: dict[str, str] = {}

        def __init__(self) -> None:
            self.calls = 0

        async def resolve_live_stream(self, *args: object, **kwargs: object) -> object:
            self.calls += 1

            class Resolution:
                url = 'https://fresh.example/live.flv'
                real_quality_number = 10000

            return Resolution()

    live = FakeLive()
    holder = StreamParamHolder(quality_number=10000)
    resolver = StreamURLResolver(live, Mock(), Mock(), holder)  # type: ignore[arg-type]

    class Seed:
        quality_number = 10000
        api_platform = 'web'
        stream_format = 'flv'
        stream_codec = 'avc'
        select_alternative = False
        url = 'https://stale.example/live.flv'
        real_quality_number = 10000

    resolver.seed(Seed())  # type: ignore[arg-type]

    def call_immediately(coro: Any) -> object:
        try:
            coro.send(None)
        except StopIteration as completed:
            return completed.value
        raise AssertionError('fake coroutine unexpectedly suspended')

    resolver._call_coroutine = call_immediately  # type: ignore[method-assign]
    attempts = 0

    def flaky_transfer(source: object) -> object:
        def subscribe(observer: object, scheduler: object = None) -> object:
            def on_next(url: str) -> None:
                nonlocal attempts
                attempts += 1
                if attempts == 1:
                    observer.on_error(requests.ConnectionError('transfer failed'))
                else:
                    observer.on_next(url)

            return source.subscribe(  # type: ignore[attr-defined,no-any-return]
                on_next,
                observer.on_error,  # type: ignore[attr-defined]
                observer.on_completed,  # type: ignore[attr-defined]
                scheduler=scheduler,
            )

        return reactivex.create(subscribe)  # type: ignore[arg-type,no-any-return]

    monkeypatch.setattr(
        'blrec.core.operators.request_exception_handler.time.sleep', lambda _: None
    )
    values: list[str] = []
    errors: list[Exception] = []
    params = StreamParams('flv', 10000, 'web', False)

    resolver(reactivex.just(params)).pipe(
        flaky_transfer, RequestExceptionHandler(resolver)
    ).subscribe(values.append, errors.append)

    assert errors == []
    assert values == ['https://fresh.example/live.flv']
    assert attempts == 2
    assert live.calls == 1
