from __future__ import annotations

import asyncio
from contextlib import suppress
from typing import TYPE_CHECKING, Awaitable, Callable, Optional

from attr import evolve
from loguru import logger

from ..exception import exception_callback
from .live_status import ObservedStatus
from .models import LiveStatus

if TYPE_CHECKING:
    from .danmaku_connection import DanmakuConnection
    from .live import Live
    from .live_monitor import LiveMonitor
    from .live_status import StatusSnapshot
    from .models import RoomInfo

__all__ = ('LiveConnectionController',)


class LiveConnectionController:
    _ACTIVATION_TIMEOUT_SECONDS = 30

    def __init__(
        self,
        live: Live,
        danmaku: DanmakuConnection,
        monitor: LiveMonitor,
        room_info_loader: Callable[[int], Awaitable[RoomInfo]],
        status_sink: Optional[Callable[[int, ObservedStatus], Awaitable[None]]] = None,
        registration_key: Optional[int] = None,
    ) -> None:
        self._live = live
        self._danmaku = danmaku
        self._monitor = monitor
        self._room_info_loader = room_info_loader
        self._status_sink = status_sink
        self._registration_key = (
            live.room_id if registration_key is None else registration_key
        )
        self._active = False
        self._retry_pending = False
        self._connection_task: Optional[asyncio.Task[None]] = None
        self._stale_cleanup_task: Optional[asyncio.Task[None]] = None
        self._lock = asyncio.Lock()
        self._logger = logger.bind(room_id=live.room_id)

    @property
    def active(self) -> bool:
        return self._active

    async def on_wss_hint(self, status: ObservedStatus) -> None:
        if status is ObservedStatus.STALE:
            if not self._active:
                return
            self._retry_pending = True
            try:
                if self._status_sink is not None:
                    await self._status_sink(self._registration_key, status)
            finally:
                self._schedule_stale_cleanup()
            return
        if self._status_sink is not None:
            await self._status_sink(self._registration_key, status)

    async def on_confirmed_status(self, snapshot: StatusSnapshot) -> None:
        async with self._lock:
            if snapshot.status is ObservedStatus.LIVE:
                if self._active:
                    if self._retry_pending:
                        await self._wait_for_stale_cleanup()
                        self._retry_pending = False
                        self._schedule_connection()
                    return
                await self._wait_for_stale_cleanup()
                room_info = await self._room_info_loader(snapshot.room_id)
                self._live.replace_room_info(room_info)
                self._danmaku.set_room_id(room_info.room_id)
                self._monitor.enable()
                try:
                    await self._monitor.apply_confirmed_status(ObservedStatus.LIVE)
                except BaseException:
                    self._monitor.disable()
                    raise
                self._active = True
                self._retry_pending = False
                self._schedule_connection()
                return
            if snapshot.status not in (ObservedStatus.PREPARING, ObservedStatus.ROUND):
                return
            if not self._active and not self._retry_pending:
                return
            await self._wait_for_stale_cleanup()
            previous_room_info = self._live.room_info
            live_status = {
                ObservedStatus.PREPARING: LiveStatus.PREPARING,
                ObservedStatus.ROUND: LiveStatus.ROUND,
            }[snapshot.status]
            self._live.replace_room_info(
                evolve(previous_room_info, live_status=live_status)
            )
            try:
                await self._monitor.apply_confirmed_status(snapshot.status)
            except BaseException:
                self._live.replace_room_info(previous_room_info)
                raise
            await self._stop_connection(reset_mode=True)
            self._monitor.disable()
            self._active = False
            self._retry_pending = False

    async def close(self) -> None:
        async with self._lock:
            await self._wait_for_stale_cleanup()
            if self._active or self._retry_pending:
                await self._stop_connection(reset_mode=True)
            if self._active or self._retry_pending:
                self._monitor.disable()
            self._active = False
            self._retry_pending = False

    def _schedule_stale_cleanup(self) -> None:
        task = self._stale_cleanup_task
        if task is not None and not task.done():
            return
        task = asyncio.create_task(self._danmaku.stop())
        task.add_done_callback(exception_callback)
        self._stale_cleanup_task = task

    def _schedule_connection(self) -> None:
        task = self._connection_task
        if task is not None and not task.done():
            return
        task = asyncio.create_task(self._connect_danmaku())
        task.add_done_callback(exception_callback)
        self._connection_task = task

    async def _connect_danmaku(self) -> None:
        start_task = asyncio.create_task(self._danmaku.start())
        try:
            await asyncio.wait_for(start_task, timeout=self._ACTIVATION_TIMEOUT_SECONDS)
            if self._active and self._status_sink is not None:
                await self._status_sink(self._registration_key, ObservedStatus.LIVE)
        except asyncio.CancelledError:
            start_task.cancel()
            with suppress(asyncio.CancelledError):
                await start_task
            raise
        except Exception as error:
            self._logger.warning(
                'Danmaku connection unavailable; recording remains active '
                'error_type={}',
                type(error).__name__,
            )
            await self.on_wss_hint(ObservedStatus.STALE)

    async def _stop_connection(self, *, reset_mode: bool) -> None:
        task = self._connection_task
        self._connection_task = None
        if task is not None and not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        await self._wait_for_stale_cleanup()
        await self._danmaku.stop(reset_mode=reset_mode)

    async def _wait_for_stale_cleanup(self) -> None:
        task = self._stale_cleanup_task
        if task is None:
            return
        try:
            await task
        finally:
            if self._stale_cleanup_task is task:
                self._stale_cleanup_task = None
