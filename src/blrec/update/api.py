from http import HTTPStatus
from typing import Any, Final, Optional

import aiohttp

from .typing import JsonResponse, Metadata

__all__ = ('PypiApi',)


class PypiApi:
    BASE_URL: Final[str] = 'https://pypi.org/pypi'

    def __init__(
        self, session: aiohttp.ClientSession, *, request_timeout_seconds: float = 10
    ):
        if request_timeout_seconds <= 0 or request_timeout_seconds > 10:
            raise ValueError('update request timeout must be in (0, 10]')
        self._session = session
        self._timeout = aiohttp.ClientTimeout(total=request_timeout_seconds)

    @classmethod
    def _make_url(cls, path: str) -> str:
        return cls.BASE_URL + path

    async def _get(self, *args: Any, **kwds: Any) -> Optional[JsonResponse]:
        try:
            async with self._session.get(
                *args, raise_for_status=True, timeout=self._timeout, **kwds
            ) as res:
                return await res.json()
        except aiohttp.ClientResponseError as e:
            if e.status == HTTPStatus.NOT_FOUND:
                return None
            else:
                raise

    async def get_project_metadata(self, project_name: str) -> Optional[Metadata]:
        url = self._make_url(f'/{project_name}/json')
        return await self._get(url)

    async def get_release_metadata(
        self, project_name: str, version: str
    ) -> Optional[Metadata]:
        url = self._make_url(f'/{project_name}/{version}/json')
        return await self._get(url)
