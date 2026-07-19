from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Awaitable, Callable, Dict, Literal, Optional

from loguru import logger

from .exceptions import DanmakuClientAuthError

if TYPE_CHECKING:
    from .danmaku_client import DanmakuClient

ConnectionMode = Literal['anonymous', 'authenticated']

__all__ = ('DanmakuConnection',)


class DanmakuConnection:
    _MAX_AUTHENTICATED_ATTEMPTS = 6
    _MAX_ATTEMPTS_PER_CREDENTIAL = 2

    def __init__(
        self,
        client: DanmakuClient,
        *,
        configure_anonymous: Callable[[], object],
        configure_authenticated: Callable[[], Optional[str]],
        authenticated_failure_reporter: Optional[
            Callable[[str], Awaitable[None]]
        ] = None,
    ) -> None:
        self._client = client
        self._configure_anonymous = configure_anonymous
        self._configure_authenticated = configure_authenticated
        self._authenticated_failure_reporter = authenticated_failure_reporter
        self._mode: ConnectionMode = 'anonymous'
        self._logger = logger.bind(room_id=client.room_id)

    @property
    def mode(self) -> ConnectionMode:
        return self._mode

    def set_room_id(self, room_id: int) -> None:
        self._client.set_room_id(room_id)
        self._logger = logger.bind(room_id=room_id)

    async def start(self) -> None:
        credential_fingerprint = self._configure_authenticated()
        attempts = 0
        attempts_by_credential: Dict[str, int] = {}
        while (
            credential_fingerprint is not None
            and attempts < self._MAX_AUTHENTICATED_ATTEMPTS
            and attempts_by_credential.get(credential_fingerprint, 0)
            < self._MAX_ATTEMPTS_PER_CREDENTIAL
        ):
            attempts += 1
            attempts_by_credential[credential_fingerprint] = (
                attempts_by_credential.get(credential_fingerprint, 0) + 1
            )
            try:
                await self._start_client('authenticated')
            except asyncio.CancelledError:
                await self._client.stop()
                raise
            except DanmakuClientAuthError as error:
                await self._client.stop()
                await self._report_authenticated_failure(error, credential_fingerprint)
                credential_fingerprint = self._configure_authenticated()
                continue
            except Exception as error:
                await self._client.stop()
                self._logger.warning(
                    'Authenticated danmaku connection failed; trying anonymous '
                    'transport error_type={}',
                    type(error).__name__,
                )
                break
            else:
                self._mode = 'authenticated'
                return

        if attempts:
            self._logger.warning(
                'Authenticated danmaku attempts exhausted; trying anonymous '
                'transport attempts={}',
                attempts,
            )

        self._configure_anonymous()
        try:
            await self._start_client('anonymous')
        except BaseException:
            await self._client.stop()
            raise
        self._mode = 'anonymous'

    async def _report_authenticated_failure(
        self, error: DanmakuClientAuthError, credential_fingerprint: str
    ) -> None:
        reporter = self._authenticated_failure_reporter
        if reporter is None:
            return
        try:
            await reporter(credential_fingerprint)
        except Exception as report_error:
            self._logger.warning(
                'Failed to report authenticated danmaku rejection '
                'error_type={} report_error_type={}',
                type(error).__name__,
                type(report_error).__name__,
            )

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
