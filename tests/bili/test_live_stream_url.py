from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest
import reactivex

import blrec.application  # noqa: F401 - initializes the package import cycle
from blrec.bili.live import Live
from blrec.core.operators.stream_url_resolver import StreamURLResolver
from blrec.core.stream_param_holder import StreamParamHolder, StreamParams


def downgraded_stream() -> list[dict[str, Any]]:
    return [
        {
            'format': [
                {
                    'format_name': 'flv',
                    'codec': [
                        {
                            'codec_name': 'avc',
                            'accept_qn': [10000, 400, 250],
                            'current_qn': 250,
                            'base_url': '/live.flv',
                            'url_info': [
                                {
                                    'host': 'https://cn-gotcha04.example',
                                    'extra': '?qn=250',
                                }
                            ],
                        }
                    ],
                }
            ]
        }
    ]


def test_cookie_is_kept_out_of_media_stream_headers() -> None:
    live = object.__new__(Live)
    live._room_id = 100
    live._user_agent = 'fixture-agent'
    live._cookie = 'SESSDATA=secret'

    live._update_headers()

    assert live.headers['Cookie'] == 'SESSDATA=secret'
    assert live.stream_headers['User-Agent'] == 'fixture-agent'
    assert 'Cookie' not in live.stream_headers


@pytest.mark.asyncio
async def test_get_live_stream_url_accepts_server_selected_lower_quality(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    live = object.__new__(Live)

    async def get_live_streams(*args: object, **kwargs: object) -> list[dict[str, Any]]:
        return downgraded_stream()

    monkeypatch.setattr(live, 'get_live_streams', get_live_streams)

    url = await live.get_live_stream_url(10000)

    assert url == 'https://cn-gotcha04.example/live.flv?qn=250'
    assert live.real_quality_number == 250


@pytest.mark.asyncio
async def test_explicit_stream_snapshot_reuses_one_play_info_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = [100.0]
    live = object.__new__(Live)
    live._monotonic = lambda: clock[0]
    live._real_quality_number = None
    calls = 0

    async def get_live_streams(*args: object, **kwargs: object) -> list[dict[str, Any]]:
        nonlocal calls
        calls += 1
        return downgraded_stream()

    monkeypatch.setattr(live, 'get_live_streams', get_live_streams)

    snapshot = await live.get_live_stream_snapshot(10000)
    resolution = await live.resolve_live_stream(10000, snapshot=snapshot)

    assert calls == 1
    assert resolution.url == 'https://cn-gotcha04.example/live.flv?qn=250'
    assert resolution.quality_number == 10000
    assert resolution.real_quality_number == 250


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('field', 'value'),
    [
        ('quality_number', 250),
        ('api_platform', 'android'),
        ('stream_format', 'fmp4'),
        ('stream_codec', 'hevc'),
        ('select_alternative', True),
    ],
)
async def test_stream_snapshot_requires_complete_selection_identity(
    monkeypatch: pytest.MonkeyPatch, field: str, value: object
) -> None:
    live = object.__new__(Live)
    live._monotonic = lambda: 100.0
    live._real_quality_number = None
    calls = 0

    async def get_live_streams(*args: object, **kwargs: object) -> list[dict[str, Any]]:
        nonlocal calls
        calls += 1
        return downgraded_stream()

    monkeypatch.setattr(live, 'get_live_streams', get_live_streams)
    snapshot = await live.get_live_stream_snapshot(10000)
    mismatched = replace(snapshot, **{field: value})

    await live.resolve_live_stream(10000, snapshot=mismatched)

    assert calls == 2


@pytest.mark.asyncio
async def test_stream_snapshot_is_reused_for_at_most_two_seconds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = [100.0]
    live = object.__new__(Live)
    live._monotonic = lambda: clock[0]
    live._real_quality_number = None
    calls = 0

    async def get_live_streams(*args: object, **kwargs: object) -> list[dict[str, Any]]:
        nonlocal calls
        calls += 1
        return downgraded_stream()

    monkeypatch.setattr(live, 'get_live_streams', get_live_streams)
    snapshot = await live.get_live_stream_snapshot(10000)
    clock[0] = 102.0
    await live.resolve_live_stream(10000, snapshot=snapshot)
    clock[0] = 102.001
    await live.resolve_live_stream(10000, snapshot=snapshot)

    assert calls == 2


@pytest.mark.asyncio
async def test_missing_snapshot_always_reads_fresh_play_info(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    live = object.__new__(Live)
    live._monotonic = lambda: 100.0
    live._real_quality_number = None
    calls = 0

    async def get_live_streams(*args: object, **kwargs: object) -> list[dict[str, Any]]:
        nonlocal calls
        calls += 1
        return downgraded_stream()

    monkeypatch.setattr(live, 'get_live_streams', get_live_streams)

    await live.resolve_live_stream(10000)
    await live.resolve_live_stream(10000)

    assert calls == 2


@pytest.mark.asyncio
async def test_stream_url_resolver_reports_server_selected_quality() -> None:
    class FakeLive:
        headers: dict[str, str] = {}

        async def resolve_live_stream(self, *args: object, **kwargs: object) -> object:
            class Resolution:
                url = 'https://cn-gotcha04.example/live.flv?qn=250'
                real_quality_number = 250

            return Resolution()

    holder = StreamParamHolder(quality_number=10000)
    resolver = StreamURLResolver(
        FakeLive(), object(), object(), holder  # type: ignore[arg-type]
    )

    def call_immediately(coro: Any) -> str:
        try:
            coro.send(None)
        except StopIteration as completed:
            return completed.value
        raise AssertionError('fake coroutine unexpectedly suspended')

    resolver._call_coroutine = call_immediately  # type: ignore[method-assign]
    values: list[str] = []
    errors: list[Exception] = []
    params = StreamParams('flv', 10000, 'web', False)

    resolver(reactivex.just(params)).subscribe(values.append, errors.append)

    assert errors == []
    assert values == ['https://cn-gotcha04.example/live.flv?qn=250']
    assert holder.real_quality_number == 250
