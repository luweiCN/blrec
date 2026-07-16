from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Callable, Literal

from loguru import logger

if TYPE_CHECKING:
    from .danmaku_client import DanmakuClient

ConnectionMode = Literal['anonymous', 'authenticated']

__all__ = ('DanmakuConnection',)


class DanmakuConnection:
    def __init__(
        self,
        client: DanmakuClient,
        *,
        configure_anonymous: Callable[[], None],
        configure_authenticated: Callable[[], bool],
    ) -> None:
        self._client = client
        self._configure_anonymous = configure_anonymous
        self._configure_authenticated = configure_authenticated
        self._mode: ConnectionMode = 'anonymous'
        self._logger = logger.bind(room_id=client.room_id)

    @property
    def mode(self) -> ConnectionMode:
        return self._mode

    def set_room_id(self, room_id: int) -> None:
        self._client.set_room_id(room_id)
        self._logger = logger.bind(room_id=room_id)

    async def start(self) -> None:
        if self._mode == 'authenticated':
            await self._start_client('authenticated')
            return

        self._configure_anonymous()
        try:
            await self._start_client('anonymous')
            return
        except asyncio.CancelledError:
            await self._client.stop()
            raise
        except Exception as error:
            await self._client.stop()
            self._logger.warning(
                'Anonymous danmaku connection failed; trying account Cookie '
                'error_type={}',
                type(error).__name__,
            )
            if not self._configure_authenticated():
                raise

        try:
            await self._start_client('authenticated')
        except BaseException:
            await self._client.stop()
            raise
        self._mode = 'authenticated'

    async def stop(self, *, reset_mode: bool = False) -> None:
        await self._client.stop()
        if reset_mode:
            self._configure_anonymous()
            self._mode = 'anonymous'

    async def restart(self) -> None:
        await self.stop()
        await self.start()

    async def _start_client(self, mode: ConnectionMode) -> None:
        self._logger.debug('Starting danmaku connection mode={}', mode)
        await self._client.start()
        self._logger.info('Danmaku connection established mode={}', mode)
