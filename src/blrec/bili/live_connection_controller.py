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
        self._lock = asyncio.Lock()

    @property
    def active(self) -> bool:
        return self._active

    async def on_wss_hint(self, status: ObservedStatus) -> None:
        if self._status_sink is not None:
            await self._status_sink(self._registration_key, status)

    async def on_confirmed_status(self, snapshot: StatusSnapshot) -> None:
        async with self._lock:
            if snapshot.status is ObservedStatus.LIVE:
                if self._active:
                    return
                room_info = await self._room_info_loader(snapshot.room_id)
                self._live.replace_room_info(room_info)
                self._danmaku.set_room_id(room_info.room_id)
                self._monitor.enable()
                try:
                    await asyncio.wait_for(
                        self._danmaku.start(), timeout=self._ACTIVATION_TIMEOUT_SECONDS
                    )
                    await self._monitor.apply_confirmed_status(ObservedStatus.LIVE)
                except BaseException:
                    self._monitor.disable()
                    await self._danmaku.stop()
                    raise
                self._active = True
                return
            if snapshot.status not in (ObservedStatus.PREPARING, ObservedStatus.ROUND):
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
