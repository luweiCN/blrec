from __future__ import annotations

import asyncio
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, Callable, Set, TypeVar

__all__ = ('PasswordWorkCoordinator', 'PasswordWorkSaturated')

T = TypeVar('T')


class PasswordWorkSaturated(RuntimeError):
    def __init__(self, retry_after: int = 1) -> None:
        super().__init__('password work capacity is exhausted')
        self.retry_after = max(1, int(retry_after))


class PasswordWorkCoordinator:
    def __init__(self, *, max_admitted: int = 5) -> None:
        if max_admitted <= 0:
            raise ValueError('maximum admitted password work must be positive')
        self._max_admitted = max_admitted
        self._executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix='blrec-password'
        )
        self._lock = threading.Lock()
        self._futures: Set[Future[Any]] = set()
        self._closed = False

    @property
    def admitted_count(self) -> int:
        with self._lock:
            return len(self._futures)

    async def run(self, work: Callable[[], T]) -> T:
        with self._lock:
            if self._closed:
                raise RuntimeError('password work coordinator is closed')
            if len(self._futures) >= self._max_admitted:
                raise PasswordWorkSaturated(retry_after=1)
            future = self._executor.submit(work)
            self._futures.add(future)
            future.add_done_callback(self._release)
        return await asyncio.wrap_future(future)

    async def shutdown(self) -> None:
        with self._lock:
            self._closed = True
            futures = tuple(self._futures)
        if futures:
            await asyncio.gather(
                *(asyncio.wrap_future(future) for future in futures),
                return_exceptions=True,
            )
        self._executor.shutdown(wait=True)

    def _release(self, future: Future[Any]) -> None:
        with self._lock:
            self._futures.discard(future)
