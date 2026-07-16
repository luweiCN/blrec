from __future__ import annotations

import asyncio
import time
from typing import AsyncIterator, Awaitable, Callable, Dict, Optional

from .traffic import TrafficMeter


class SharedUploadLimiter:
    def __init__(
        self,
        limit_provider: Callable[[Optional[str]], int],
        *,
        meter: Optional[TrafficMeter] = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        chunk_bytes: int = 64 * 1024,
    ) -> None:
        if chunk_bytes <= 0:
            raise ValueError('stream chunk size must be positive')
        self._limit_provider = limit_provider
        self._meter = meter
        self._clock = clock
        self._sleep = sleep
        self._chunk_bytes = chunk_bytes
        self._locks: Dict[Optional[str], asyncio.Lock] = {}
        self._next_available: Dict[Optional[str], float] = {}

    async def stream(
        self, interface_name: Optional[str], body: bytes
    ) -> AsyncIterator[bytes]:
        for offset in range(0, len(body), self._chunk_bytes):
            piece = body[offset : offset + self._chunk_bytes]
            await self._wait(interface_name, len(piece))
            if self._meter is not None:
                self._meter.record(interface_name, 'upload', 'up', len(piece))
            yield piece

    async def _wait(self, interface_name: Optional[str], byte_count: int) -> None:
        limit = self._limit_provider(interface_name)
        if limit <= 0:
            return
        lock = self._locks.setdefault(interface_name, asyncio.Lock())
        async with lock:
            now = self._clock()
            start = max(now, self._next_available.get(interface_name, now))
            finish = start + byte_count / limit
            self._next_available[interface_name] = finish
            delay = max(0.0, finish - now)
        if delay:
            await self._sleep(delay)
