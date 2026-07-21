import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Awaitable, Callable, Optional

import pytest

import blrec.setting  # noqa: F401  # Initialize settings before its core import.
from blrec.core.cover_downloader import CoverDownloader


class _Response:
    def __init__(self, content: bytes) -> None:
        self._content = content

    async def __aenter__(self) -> '_Response':
        return self

    async def __aexit__(self, *args: Any) -> None:
        pass

    async def read(self) -> bytes:
        return self._content


class _Session:
    def __init__(self, content: bytes = b'cover') -> None:
        self.content = content
        self.calls = []
        self.failure: Optional[Exception] = None

    def get(self, url: str) -> _Response:
        self.calls.append(url)
        if self.failure is not None:
            raise self.failure
        return _Response(self.content)


class _Live:
    def __init__(
        self,
        *,
        cover: str = 'https://i0.hdslb.com/live.jpg',
        live_start_time: int = 100,
        fallback: Optional[Callable[[], Awaitable[None]]] = None,
    ) -> None:
        self.room_id = 1
        self.room_info = SimpleNamespace(
            room_id=1, live_start_time=live_start_time, cover=cover
        )
        self.session = _Session()
        self.update_info_calls = 0
        self.update_room_info_calls = 0
        self._fallback = fallback

    async def update_info(self) -> bool:
        self.update_info_calls += 1
        if self._fallback is not None:
            await self._fallback()
        return True

    async def update_room_info(self) -> bool:
        self.update_room_info_calls += 1
        return True


class _Recorder:
    def add_listener(self, listener: Any) -> None:
        pass

    def remove_listener(self, listener: Any) -> None:
        pass


def _downloader(live: _Live) -> CoverDownloader:
    return CoverDownloader(  # type: ignore[arg-type]
        live, _Recorder(), save_cover=True  # type: ignore[arg-type]
    )


@pytest.mark.asyncio
async def test_reuses_remote_cover_bytes_but_saves_every_part(tmp_path: Path) -> None:
    live = _Live()
    downloader = _downloader(live)
    videos = [tmp_path / 'p1.flv', tmp_path / 'p2.flv', tmp_path / 'p3.flv']

    for video in videos:
        await downloader.on_video_file_completed(str(video))

    assert live.session.calls == ['https://i0.hdslb.com/live.jpg']
    assert live.update_info_calls == 0
    assert live.update_room_info_calls == 0
    assert [video.with_suffix('.jpg').read_bytes() for video in videos] == [
        b'cover',
        b'cover',
        b'cover',
    ]


@pytest.mark.asyncio
async def test_caches_failed_optional_cover_for_the_broadcast(tmp_path: Path) -> None:
    live = _Live()
    live.session.failure = RuntimeError('download failed')
    downloader = _downloader(live)

    await downloader.on_video_file_completed(str(tmp_path / 'p1.flv'))
    await downloader.on_video_file_completed(str(tmp_path / 'p2.flv'))

    assert live.session.calls == ['https://i0.hdslb.com/live.jpg']
    assert not (tmp_path / 'p1.jpg').exists()
    assert not (tmp_path / 'p2.jpg').exists()


@pytest.mark.asyncio
async def test_room_change_and_new_broadcast_each_allow_one_cover_fetch(
    tmp_path: Path,
) -> None:
    live = _Live()
    downloader = _downloader(live)

    await downloader.on_video_file_completed(str(tmp_path / 'first.flv'))
    live.room_info.cover = 'https://i1.hdslb.com/changed.png'
    await downloader.on_video_file_completed(str(tmp_path / 'changed.flv'))
    live.room_info.live_start_time = 200
    await downloader.on_video_file_completed(str(tmp_path / 'next.flv'))

    assert live.session.calls == [
        'https://i0.hdslb.com/live.jpg',
        'https://i1.hdslb.com/changed.png',
        'https://i1.hdslb.com/changed.png',
    ]


@pytest.mark.asyncio
async def test_missing_metadata_uses_one_composite_fallback_per_broadcast(
    tmp_path: Path,
) -> None:
    live = _Live(cover='')

    async def fill_cover() -> None:
        live.room_info.cover = 'https://i0.hdslb.com/fallback.jpg'

    live._fallback = fill_cover
    downloader = _downloader(live)

    for part in ('p1.flv', 'p2.flv', 'p3.flv'):
        await downloader.on_video_file_completed(str(tmp_path / part))

    assert live.update_info_calls == 1
    assert live.update_room_info_calls == 0
    assert live.session.calls == ['https://i0.hdslb.com/fallback.jpg']


@pytest.mark.asyncio
async def test_fallback_counts_for_the_broadcast_returned_by_refresh(
    tmp_path: Path,
) -> None:
    live = _Live(cover='', live_start_time=100)

    async def change_broadcast_without_cover() -> None:
        live.room_info.live_start_time = 200

    live._fallback = change_broadcast_without_cover
    downloader = _downloader(live)

    await downloader.on_video_file_completed(str(tmp_path / 'p1.flv'))
    await downloader.on_video_file_completed(str(tmp_path / 'p2.flv'))

    assert live.update_info_calls == 1
    assert live.session.calls == []


@pytest.mark.asyncio
async def test_overlapping_parts_share_cover_without_blocking_event_loop(
    tmp_path: Path,
) -> None:
    live = _Live()
    downloader = _downloader(live)
    started = asyncio.Event()
    release = asyncio.Event()
    fetch_calls = 0

    async def fetch(url: str) -> bytes:
        nonlocal fetch_calls
        fetch_calls += 1
        started.set()
        await release.wait()
        return b'cover'

    downloader._fetch_cover = fetch  # type: ignore[method-assign]

    assert isinstance(downloader._lock, asyncio.Lock)
    first = asyncio.create_task(
        downloader.on_video_file_completed(str(tmp_path / 'p1.flv'))
    )
    await asyncio.wait_for(started.wait(), timeout=5)
    second = asyncio.create_task(
        downloader.on_video_file_completed(str(tmp_path / 'p2.flv'))
    )
    await asyncio.sleep(0)
    heartbeat = asyncio.Event()
    asyncio.get_running_loop().call_soon(heartbeat.set)
    await asyncio.wait_for(heartbeat.wait(), timeout=5)
    release.set()

    await asyncio.wait_for(asyncio.gather(first, second), timeout=5)
    assert fetch_calls == 1
    assert (tmp_path / 'p1.jpg').read_bytes() == b'cover'
    assert (tmp_path / 'p2.jpg').read_bytes() == b'cover'


@pytest.mark.asyncio
async def test_disable_enable_keeps_success_cache_for_same_broadcast(
    tmp_path: Path,
) -> None:
    live = _Live()
    downloader = _downloader(live)
    downloader.enable()
    try:
        await downloader.on_video_file_completed(str(tmp_path / 'p1.flv'))
        downloader.disable()
        downloader.enable()
        await downloader.on_video_file_completed(str(tmp_path / 'p2.flv'))
    finally:
        downloader.disable()

    assert live.session.calls == ['https://i0.hdslb.com/live.jpg']
    assert (tmp_path / 'p1.jpg').read_bytes() == b'cover'
    assert (tmp_path / 'p2.jpg').read_bytes() == b'cover'


@pytest.mark.asyncio
async def test_disable_enable_keeps_failed_fallback_for_same_broadcast(
    tmp_path: Path,
) -> None:
    live = _Live(cover='')
    downloader = _downloader(live)
    downloader.enable()
    try:
        await downloader.on_video_file_completed(str(tmp_path / 'p1.flv'))
        downloader.disable()
        downloader.enable()
        await downloader.on_video_file_completed(str(tmp_path / 'p2.flv'))
    finally:
        downloader.disable()

    assert live.update_info_calls == 1
    assert live.session.calls == []
