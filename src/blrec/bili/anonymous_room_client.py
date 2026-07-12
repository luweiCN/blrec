import time
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple, cast

import aiohttp

from .batch_status_client import (
    BatchProtocolError,
    _decode_response_data,
    _parse_nonnegative_int,
    _parse_status_snapshot,
    _raise_for_http_status,
    _validate_anonymous_session,
)
from .live_status import StatusSnapshot, StatusSource
from .models import RoomInfo

__all__ = ('AnonymousRoomClient',)


class AnonymousRoomClient:
    BASE_INFO_PATH = '/xlive/web-room/v1/index/getRoomBaseInfo'
    ROOM_INFO_PATH = '/room/v1/Room/get_info'

    def __init__(
        self,
        session: aiohttp.ClientSession,
        base_url: str = 'https://api.live.bilibili.com',
        user_agent: str = 'BLREC batch live monitor',
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        _validate_anonymous_session(session)
        base_url = base_url.rstrip('/')
        self._session = session
        self._base_info_url = base_url + self.BASE_INFO_PATH
        self._room_info_url = base_url + self.ROOM_INFO_PATH
        self._headers = {'Accept': 'application/json', 'User-Agent': user_agent}
        self._clock = clock

    async def fetch_uid_mappings(
        self, room_ids: Sequence[int]
    ) -> Dict[int, Tuple[int, int]]:
        unique_room_ids = tuple(dict.fromkeys(room_ids))
        if not unique_room_ids:
            return {}

        items = await self._fetch_base_info(unique_room_ids)
        requested = set(unique_room_ids)
        mappings: Dict[int, Tuple[int, int]] = {}
        for key, value in items.items():
            parsed = self._parse_room_mapping(value)
            if parsed is None:
                continue
            real_room_id, short_room_id, uid = parsed
            try:
                response_room_id = _parse_nonnegative_int(key)
            except ValueError:
                continue
            if response_room_id != real_room_id:
                continue
            aliases = {real_room_id}
            if short_room_id:
                aliases.add(short_room_id)
            for requested_room_id in requested & aliases:
                mappings[requested_room_id] = (real_room_id, uid)
        return mappings

    async def confirm_status(self, room_id: int) -> StatusSnapshot:
        data = await self._fetch_data(self._room_info_url, [('room_id', str(room_id))])
        snapshot = _parse_status_snapshot(
            data, self._clock(), StatusSource.CONFIRMATION
        )
        if snapshot is None:
            raise BatchProtocolError('invalid room item')
        try:
            short_room_id = _parse_nonnegative_int(data.get('short_id', 0))
        except ValueError as exc:
            raise BatchProtocolError('invalid room item') from exc
        if room_id not in (snapshot.room_id, short_room_id):
            raise BatchProtocolError('invalid room item')
        return snapshot

    async def load_room_info(self, room_id: int) -> RoomInfo:
        items = await self._fetch_base_info((room_id,))
        item = self._find_room_item(items, room_id)
        if item is None:
            raise BatchProtocolError('room info is missing')
        try:
            return RoomInfo.from_data(dict(item))
        except Exception as exc:
            raise BatchProtocolError('invalid room item') from exc

    async def _fetch_base_info(self, room_ids: Sequence[int]) -> Mapping[str, Any]:
        params = [('req_biz', 'web_room_componet')]
        params.extend(('room_ids', str(room_id)) for room_id in room_ids)
        data = await self._fetch_data(self._base_info_url, params)
        items = data.get('by_room_ids')
        if not isinstance(items, Mapping):
            raise BatchProtocolError('unexpected response envelope')
        return cast(Mapping[str, Any], items)

    async def _fetch_data(
        self, url: str, params: List[Tuple[str, str]]
    ) -> Mapping[str, Any]:
        _validate_anonymous_session(self._session)
        async with self._session.get(
            url, params=params, headers=self._headers, allow_redirects=False
        ) as response:
            body = await response.text()
            _raise_for_http_status(response.status)
        return _decode_response_data(body)

    @staticmethod
    def _parse_room_mapping(value: object) -> Optional[Tuple[int, int, int]]:
        if not isinstance(value, Mapping):
            return None
        try:
            real_room_id = _parse_nonnegative_int(value['room_id'])
            short_room_id = _parse_nonnegative_int(value.get('short_id', 0))
            uid = _parse_nonnegative_int(value['uid'])
        except (KeyError, TypeError, ValueError):
            return None
        return real_room_id, short_room_id, uid

    @classmethod
    def _find_room_item(
        cls, items: Mapping[str, Any], requested_room_id: int
    ) -> Optional[Mapping[str, Any]]:
        for key, value in items.items():
            parsed = cls._parse_room_mapping(value)
            if parsed is None:
                continue
            real_room_id, short_room_id, _ = parsed
            try:
                response_room_id = _parse_nonnegative_int(key)
            except ValueError:
                continue
            if response_room_id != real_room_id:
                continue
            if requested_room_id in (real_room_id, short_room_id):
                return cast(Mapping[str, Any], value)
        return None
