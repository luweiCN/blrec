from __future__ import annotations

from typing import Iterator
from unittest.mock import Mock

import m3u8
import pytest
import reactivex

import blrec.setting  # noqa: F401 - initializes core imports
from blrec.hls.exceptions import FetchSegmentError
from blrec.hls.operators.segment_fetcher import (
    InitSectionData,
    SegmentData,
    SegmentFetcher,
)
from blrec.utils.hash import cksum


class FakeLive:
    stream_headers = {'User-Agent': 'fixture'}


def make_fetcher() -> SegmentFetcher:
    return SegmentFetcher(FakeLive(), Mock(), Mock())  # type: ignore[arg-type]


def test_hls_init_section_requires_two_identical_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b'valid-segment'
    segment = m3u8.Segment(
        uri='segment.m4s',
        base_uri='https://cdn.example/',
        title=f'{len(payload):x}|{cksum(payload)}',
        init_section={'uri': 'init.mp4'},
    )
    responses: Iterator[bytes] = iter(
        [b'first-init', b'second-init', b'second-init', payload]
    )
    fetcher = make_fetcher()
    fetcher._fetch_segment = lambda _url: next(responses)  # type: ignore[method-assign]
    monkeypatch.setattr(
        'blrec.hls.operators.segment_fetcher.time.sleep', lambda _: None
    )
    values: list[object] = []
    errors: list[Exception] = []

    fetcher._fetch(reactivex.just(segment)).subscribe(values.append, errors.append)

    assert errors == []
    assert [type(value) for value in values] == [InitSectionData, SegmentData]
    assert values[0].payload == b'second-init'  # type: ignore[union-attr]
    assert values[1].payload == payload  # type: ignore[union-attr]


@pytest.mark.parametrize('title', ['2|0', f'1|{cksum(b"different")}'])
def test_hls_size_and_crc_mismatches_are_rejected(title: str) -> None:
    class Segment:
        absolute_uri = 'https://cdn.example/segment.m4s'

        def __init__(self) -> None:
            self.title = title

    fetcher = make_fetcher()
    fetcher._fetch_segment = lambda _url: b'x'  # type: ignore[method-assign]
    values: list[object] = []
    errors: list[Exception] = []

    fetcher._fetch(reactivex.from_iterable([Segment()] * 4)).subscribe(
        values.append, errors.append
    )

    assert values == []
    assert len(errors) == 1
    assert isinstance(errors[0], FetchSegmentError)


def test_hls_transfer_failure_resets_then_rotates_stream_route() -> None:
    calls: list[str] = []

    class Resolver:
        def reset(self) -> None:
            calls.append('reset')

        def rotate_routes(self) -> None:
            calls.append('rotate')

    fetcher = SegmentFetcher(FakeLive(), Mock(), Resolver())  # type: ignore[arg-type]

    fetcher._before_retry(FetchSegmentError(RuntimeError('transfer failed')))

    assert calls == ['reset', 'rotate']
