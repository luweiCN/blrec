import asyncio
from enum import Enum
from typing import Dict, Optional, Set, Tuple, Union

import aiofiles
from loguru import logger

from blrec.bili.live import Live
from blrec.event.event_emitter import EventEmitter, EventListener
from blrec.exception import submit_exception
from blrec.path import cover_path
from blrec.utils.hash import sha1sum
from blrec.utils.mixins import SwitchableMixin

from .stream_recorder import StreamRecorder, StreamRecorderEventListener

__all__ = 'CoverDownloader', 'CoverDownloaderEventListener'


_FAILED = object()
_BroadcastKey = Tuple[int, int, str]


class CoverDownloaderEventListener(EventListener):
    async def on_cover_image_downloaded(self, path: str) -> None:
        pass


class CoverSaveStrategy(Enum):
    DEFAULT = 'default'
    DEDUP = 'dedup'

    def __str__(self) -> str:
        return self.value

    # workaround for value serialization
    def __repr__(self) -> str:
        return str(self)


class CoverDownloader(
    EventEmitter[CoverDownloaderEventListener],
    StreamRecorderEventListener,
    SwitchableMixin,
):
    def __init__(
        self,
        live: Live,
        stream_recorder: StreamRecorder,
        *,
        save_cover: bool = False,
        cover_save_strategy: CoverSaveStrategy = CoverSaveStrategy.DEFAULT,
    ) -> None:
        super().__init__()
        self._logger_context = {'room_id': live.room_id}
        self._logger = logger.bind(**self._logger_context)
        self._live = live
        self._stream_recorder = stream_recorder
        self._lock = asyncio.Lock()
        self._sha1_set: Set[str] = set()
        self._cover_bytes: Dict[_BroadcastKey, Union[bytes, object]] = {}
        self._broadcast_identity: Optional[Tuple[int, int]] = None
        self._cover_url = ''
        self._metadata_fallback_identity: Optional[Tuple[int, int]] = None
        self.save_cover = save_cover
        self.cover_save_strategy = cover_save_strategy

    def _do_enable(self) -> None:
        self._sha1_set.clear()
        self._stream_recorder.add_listener(self)
        self._logger.debug('Enabled cover downloader')

    def _do_disable(self) -> None:
        self._stream_recorder.remove_listener(self)
        self._logger.debug('Disabled cover downloader')

    async def on_video_file_completed(self, video_path: str) -> None:
        async with self._lock:
            if not self.save_cover:
                return
            await self._save_cover(video_path)

    async def _save_cover(self, video_path: str) -> None:
        try:
            result = await self._cover_bytes_for_part()
            if result is None:
                return
            cover_url, data = result
            sha1 = sha1sum(data)
            if (
                self.cover_save_strategy == CoverSaveStrategy.DEDUP
                and sha1 in self._sha1_set
            ):
                return
            path = cover_path(video_path, ext=cover_url.rsplit('.', 1)[-1])
            await self._save_file(path, data)
            self._sha1_set.add(sha1)
        except Exception as e:
            self._logger.error(f'Failed to save cover image: {repr(e)}')
            submit_exception(e)
        else:
            self._logger.info(f'Saved cover image: {path}')
            await self._emit('cover_image_downloaded', path)

    async def _cover_bytes_for_part(self) -> Optional[Tuple[str, bytes]]:
        room_info = self._live.room_info
        identity = (room_info.room_id, room_info.live_start_time)
        cover_url = room_info.cover
        if not cover_url and self._metadata_fallback_identity != identity:
            self._metadata_fallback_identity = identity
            await self._live.update_info()
            room_info = self._live.room_info
            identity = (room_info.room_id, room_info.live_start_time)
            cover_url = room_info.cover
            self._metadata_fallback_identity = identity

        if self._broadcast_identity != identity or self._cover_url != cover_url:
            self._cover_bytes.clear()
            self._broadcast_identity = identity
            self._cover_url = cover_url
        key = (identity[0], identity[1], cover_url)
        cached = self._cover_bytes.get(key)
        if cached is _FAILED:
            return None
        if isinstance(cached, bytes):
            return cover_url, cached
        if not cover_url:
            self._cover_bytes[key] = _FAILED
            return None
        try:
            data = await self._fetch_cover(cover_url)
        except Exception:
            self._cover_bytes[key] = _FAILED
            raise
        self._cover_bytes[key] = data
        return cover_url, data

    async def _fetch_cover(self, url: str) -> bytes:
        async with self._live.session.get(url) as response:
            return await response.read()

    async def _save_file(self, path: str, data: bytes) -> None:
        async with aiofiles.open(path, 'wb') as file:
            await file.write(data)
