from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Awaitable, Callable, Optional

from attr import evolve

from ..exception import exception_callback
from .live_status import ObservedStatus
from .models import LiveStatus

if TYPE_CHECKING:
    from .danmaku_client import DanmakuClient
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
        danmaku: DanmakuClient,
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
        self._stale_cleanup_task: Optional[asyncio.Task[None]] = None
        self._lock = asyncio.Lock()

    @property
    def active(self) -> bool:
        return self._active

    async def on_wss_hint(self, status: ObservedStatus) -> None:
        if status is ObservedStatus.STALE:
            self._active = False
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
                    return
                recovering = self._retry_pending
                await self._wait_for_stale_cleanup()
                room_info = await self._room_info_loader(snapshot.room_id)
                self._live.replace_room_info(room_info)
                self._danmaku.set_room_id(room_info.room_id)
                self._monitor.enable()
                try:
                    await asyncio.wait_for(
                        self._danmaku.start(), timeout=self._ACTIVATION_TIMEOUT_SECONDS
                    )
                    if not recovering:
                        await self._monitor.apply_confirmed_status(ObservedStatus.LIVE)
                except BaseException:
                    if not self._retry_pending:
                        self._monitor.disable()
                    await self._danmaku.stop()
                    raise
                self._active = True
                self._retry_pending = False
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
            if self._active:
                await self._danmaku.stop()
            self._monitor.disable()
            self._active = False
            self._retry_pending = False

    async def close(self) -> None:
        async with self._lock:
            await self._wait_for_stale_cleanup()
            if self._active:
                await self._danmaku.stop()
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

    async def _wait_for_stale_cleanup(self) -> None:
        task = self._stale_cleanup_task
        if task is None:
            return
        try:
            await task
        finally:
            if self._stale_cleanup_task is task:
                self._stale_cleanup_task = None
