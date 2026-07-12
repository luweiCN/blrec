from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Awaitable, Callable, Optional

from .live_status import ObservedStatus

if TYPE_CHECKING:
    from .danmaku_client import DanmakuClient
    from .live import Live
    from .live_monitor import LiveMonitor
    from .live_status import StatusSnapshot
    from .models import RoomInfo

__all__ = ('LiveConnectionController',)


class LiveConnectionController:
    def __init__(
        self,
        live: Live,
        danmaku: DanmakuClient,
        monitor: LiveMonitor,
        room_info_loader: Callable[[], Awaitable[RoomInfo]],
        status_sink: Optional[Callable[[int, ObservedStatus], Awaitable[None]]] = None,
    ) -> None:
        self._live = live
        self._danmaku = danmaku
        self._monitor = monitor
        self._room_info_loader = room_info_loader
        self._status_sink = status_sink
        self._active = False
        self._lock = asyncio.Lock()

    @property
    def active(self) -> bool:
        return self._active

    async def on_wss_hint(self, status: ObservedStatus) -> None:
        if self._status_sink is not None:
            await self._status_sink(self._live.room_id, status)

    async def on_confirmed_status(self, snapshot: StatusSnapshot) -> None:
        async with self._lock:
            if snapshot.status is ObservedStatus.LIVE:
                if self._active:
                    return
                room_info = await self._room_info_loader()
                self._live.replace_room_info(room_info)
                self._monitor.enable()
                try:
                    await self._danmaku.start()
                    await self._monitor.apply_confirmed_status(ObservedStatus.LIVE)
                except BaseException:
                    self._monitor.disable()
                    await self._danmaku.stop()
                    raise
                self._active = True
                return
            if not self._active:
                return
            await self._monitor.apply_confirmed_status(snapshot.status)
            await self._danmaku.stop()
            self._monitor.disable()
            self._active = False

    async def close(self) -> None:
        async with self._lock:
            if self._active:
                await self._danmaku.stop()
                self._monitor.disable()
                self._active = False
