from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, Mapping, Optional, Tuple, TypeVar

import aiohttp

from .. import __prog__, __version__
from .api import PypiApi
from .typing import Metadata

__all__ = (
    'UpdateMetadataClient',
    'get_project_metadata',
    'get_release_metadata',
    'get_latest_version_string',
)


_CacheKey = Tuple[str, ...]
_T = TypeVar('_T')


@dataclass(frozen=True)
class _CacheEntry:
    value: Optional[Metadata]
    stored_at: float


class UpdateMetadataClient:
    FRESH_SECONDS = 30 * 60
    STALE_SECONDS = 24 * 60 * 60

    def __init__(
        self,
        *,
        request_timeout_seconds: float = 10,
        session_factory: Callable[..., Any] = aiohttp.ClientSession,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if request_timeout_seconds <= 0 or request_timeout_seconds > 10:
            raise ValueError('update request timeout must be in (0, 10]')
        self._request_timeout_seconds = request_timeout_seconds
        self._session_factory = session_factory
        self._monotonic = monotonic
        self._session: Optional[Any] = None
        self._cache: Dict[_CacheKey, _CacheEntry] = {}
        self._inflight: Dict[_CacheKey, asyncio.Task[Optional[Metadata]]] = {}
        self._accepting = False
        self._close_task: Optional[asyncio.Task[None]] = None

    @property
    def inflight_count(self) -> int:
        return len(self._inflight)

    async def start(self) -> None:
        if self._session is not None:
            return
        self._session = self._session_factory(
            headers={'User-Agent': '{}/{}'.format(__prog__, __version__)},
            cookie_jar=aiohttp.DummyCookieJar(),
        )
        self._accepting = True

    async def close(self) -> None:
        self._accepting = False
        close_task = self._close_task
        if close_task is None:
            close_task = asyncio.create_task(self._close_owned())
            self._close_task = close_task
        cancelled = False
        try:
            while True:
                try:
                    await asyncio.shield(close_task)
                    break
                except asyncio.CancelledError:
                    if close_task.done():
                        close_task.result()
                        raise
                    cancelled = True
        finally:
            if close_task.done() and self._close_task is close_task:
                self._close_task = None
        if cancelled:
            raise asyncio.CancelledError

    async def _close_owned(self) -> None:
        tasks = tuple(self._inflight.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._inflight.clear()
        session = self._session
        if session is None:
            return
        await session.close()
        if self._session is session:
            self._session = None

    async def get_project_metadata(self, project_name: str) -> Optional[Metadata]:
        return await self._get(
            ('project', project_name),
            lambda api: api.get_project_metadata(project_name),
        )

    async def get_release_metadata(
        self, project_name: str, version: str
    ) -> Optional[Metadata]:
        return await self._get(
            ('release', project_name, version),
            lambda api: api.get_release_metadata(project_name, version),
        )

    async def get_latest_version_string(self, project_name: str) -> Optional[str]:
        metadata = await self.get_project_metadata(project_name)
        if metadata is None:
            return None
        info = metadata.get('info')
        if not isinstance(info, Mapping):
            raise ValueError('update metadata is incomplete')
        version = info.get('version')
        if not isinstance(version, str) or not version:
            raise ValueError('update metadata is incomplete')
        return version

    async def _get(
        self, key: _CacheKey, loader: Callable[[PypiApi], Awaitable[Optional[Metadata]]]
    ) -> Optional[Metadata]:
        if not self._accepting or self._session is None:
            raise RuntimeError('update metadata client is not started')
        now = self._monotonic()
        cached = self._cache.get(key)
        if cached is not None and now - cached.stored_at < self.FRESH_SECONDS:
            return cached.value
        task = self._inflight.get(key)
        if task is None:
            task = asyncio.create_task(self._refresh(key, loader))
            self._inflight[key] = task
            task.add_done_callback(
                lambda completed: self._clear_inflight(key, completed)
            )
        try:
            return await asyncio.shield(task)
        except Exception:
            cached = self._cache.get(key)
            if (
                cached is not None
                and self._monotonic() - cached.stored_at <= self.STALE_SECONDS
            ):
                return cached.value
            raise

    async def _refresh(
        self, key: _CacheKey, loader: Callable[[PypiApi], Awaitable[Optional[Metadata]]]
    ) -> Optional[Metadata]:
        session = self._session
        if session is None:
            raise RuntimeError('update metadata client is not started')
        api = PypiApi(session, request_timeout_seconds=self._request_timeout_seconds)
        value = await asyncio.wait_for(
            loader(api), timeout=self._request_timeout_seconds
        )
        if value is not None:
            if not isinstance(value, dict):
                raise ValueError('update metadata response is invalid')
            info = value.get('info')
            version = info.get('version') if isinstance(info, Mapping) else None
            if not isinstance(version, str) or not version:
                raise ValueError('update metadata response is invalid')
        self._cache[key] = _CacheEntry(value, self._monotonic())
        return value

    def _clear_inflight(
        self, key: _CacheKey, completed: asyncio.Task[Optional[Metadata]]
    ) -> None:
        if self._inflight.get(key) is completed:
            self._inflight.pop(key, None)


async def _one_shot(operation: Callable[[UpdateMetadataClient], Awaitable[_T]]) -> _T:
    client = UpdateMetadataClient()
    await client.start()
    try:
        return await operation(client)
    finally:
        await client.close()


async def get_project_metadata(project_name: str) -> Optional[Metadata]:
    return await _one_shot(lambda client: client.get_project_metadata(project_name))


async def get_release_metadata(project_name: str, version: str) -> Optional[Metadata]:
    return await _one_shot(
        lambda client: client.get_release_metadata(project_name, version)
    )


async def get_latest_version_string(project_name: str) -> Optional[str]:
    return await _one_shot(
        lambda client: client.get_latest_version_string(project_name)
    )
