from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, Hashable, Mapping, Tuple, TypeVar

from .errors import ProtocolContractError

__all__ = ('ArchiveReadService',)

_Value = TypeVar('_Value')


@dataclass(frozen=True)
class _PageKey:
    account_id: int
    credential_version: int
    status: str
    page_number: int
    page_size: int


@dataclass(frozen=True)
class _DetailKey:
    account_id: int
    credential_version: int
    bvid: str


class ArchiveReadService:
    FRESH_SECONDS = 30

    def __init__(
        self, protocol: Any, *, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._protocol = protocol
        self._clock = clock
        self._lock = asyncio.Lock()
        self._inflight: Dict[Hashable, asyncio.Task[Any]] = {}
        self._cache: Dict[Hashable, Tuple[float, Any]] = {}
        self._closed = False

    async def list_page(
        self,
        bundle: Any,
        *,
        account_id: int,
        credential_version: int,
        status: str,
        page_number: int,
        page_size: int,
    ) -> Tuple[Mapping[str, Any], ...]:
        key = _PageKey(account_id, credential_version, status, page_number, page_size)
        return await self._singleflight(key, lambda: self._fetch_page(bundle, key))

    async def detail(
        self, bundle: Any, *, account_id: int, credential_version: int, bvid: str
    ) -> Mapping[str, Any]:
        key = _DetailKey(account_id, credential_version, bvid)
        return await self._singleflight(key, lambda: self._fetch_detail(bundle, bvid))

    async def close(self) -> None:
        async with self._lock:
            if self._closed:
                return
            self._closed = True
            tasks = tuple(self._inflight.values())
            self._inflight.clear()
            self._cache.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _singleflight(
        self, key: Hashable, factory: Callable[[], Awaitable[_Value]]
    ) -> _Value:
        async with self._lock:
            if self._closed:
                raise RuntimeError('archive reader is closed')
            cached = self._cache.get(key)
            if cached is not None:
                expires_at, value = cached
                if self._clock() < expires_at:
                    return value
                self._cache.pop(key, None)
            task = self._inflight.get(key)
            if task is None:
                task = asyncio.create_task(self._load(key, factory))
                task.add_done_callback(self._consume_result)
                self._inflight[key] = task
        return await asyncio.shield(task)

    @staticmethod
    def _consume_result(task: asyncio.Task[Any]) -> None:
        try:
            task.exception()
        except asyncio.CancelledError:
            pass

    async def _load(
        self, key: Hashable, factory: Callable[[], Awaitable[_Value]]
    ) -> _Value:
        task = asyncio.current_task()
        try:
            value = await factory()
        except BaseException:
            async with self._lock:
                if self._inflight.get(key) is task:
                    self._inflight.pop(key, None)
            raise
        async with self._lock:
            if self._inflight.get(key) is task:
                self._inflight.pop(key, None)
                if not self._closed:
                    self._cache[key] = (self._clock() + self.FRESH_SECONDS, value)
        return value

    async def _fetch_page(
        self, bundle: Any, key: _PageKey
    ) -> Tuple[Mapping[str, Any], ...]:
        response = await self._protocol.list_archives(
            bundle, {'status': key.status, 'pn': key.page_number, 'ps': key.page_size}
        )
        if not isinstance(response, Mapping):
            raise ProtocolContractError('archive list response is invalid')
        data = response.get('data')
        entries = data.get('arc_audits') if isinstance(data, Mapping) else None
        if not isinstance(entries, list) or not all(
            isinstance(entry, Mapping) for entry in entries
        ):
            raise ProtocolContractError('archive list response is invalid')
        return tuple(dict(entry) for entry in entries)

    async def _fetch_detail(self, bundle: Any, bvid: str) -> Mapping[str, Any]:
        response = await self._protocol.archive_view(
            bundle, {'topic_grey': 1, 'bvid': bvid, 't': int(time.time() * 1000)}
        )
        if not isinstance(response, Mapping):
            raise ProtocolContractError('archive detail response is invalid')
        return dict(response)
