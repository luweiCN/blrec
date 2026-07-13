from __future__ import annotations

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
async def test_stream_url_resolver_reports_server_selected_quality() -> None:
    class FakeLive:
        headers: dict[str, str] = {}
        real_quality_number = 250

        async def get_live_stream_url(self, *args: object, **kwargs: object) -> str:
            return 'https://cn-gotcha04.example/live.flv?qn=250'

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
