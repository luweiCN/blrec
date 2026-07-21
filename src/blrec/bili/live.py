import asyncio
import json
import re
import time
from contextlib import suppress
from dataclasses import dataclass
from typing import (
    TYPE_CHECKING,
    Any,
    Awaitable,
    Callable,
    Dict,
    List,
    Optional,
    Tuple,
    cast,
)

import aiohttp
from jsonpath import jsonpath

from .api import BASE_HEADERS, AppApi, WebApi
from .exceptions import (
    ApiRequestError,
    LiveRoomEncrypted,
    LiveRoomHidden,
    LiveRoomLocked,
    NoAlternativeStreamAvailable,
    NoStreamAvailable,
    NoStreamCodecAvailable,
    NoStreamFormatAvailable,
    NoStreamQualityAvailable,
)
from .helpers import extract_codecs, extract_formats, extract_streams
from .models import LiveStatus, RoomInfo, UserInfo
from .net import connector, timeout
from .typing import ApiPlatform, QualityNumber, ResponseData, StreamCodec, StreamFormat

if TYPE_CHECKING:
    from blrec.networking.manager import NetworkRouteManager

__all__ = ('Live', 'LiveInfoSnapshot', 'LiveStreamSnapshot', 'StreamResolution')

from loguru import logger

_INFO_PATTERN = re.compile(
    rb'<script>\s*window\.__NEPTUNE_IS_MY_WAIFU__\s*=\s*(\{.*?\})\s*</script>'
)
_LIVE_STATUS_PATTERN = re.compile(rb'"live_status"\s*:\s*(\d)')


@dataclass(frozen=True)
class LiveInfoSnapshot:
    room_info: RoomInfo
    user_info: UserInfo


@dataclass(frozen=True)
class LiveStreamSnapshot:
    quality_number: QualityNumber
    api_platform: ApiPlatform
    stream_format: StreamFormat
    stream_codec: StreamCodec
    select_alternative: bool
    streams: Tuple[Any, ...]
    observed_at: float


@dataclass(frozen=True)
class StreamResolution:
    quality_number: QualityNumber
    api_platform: ApiPlatform
    stream_format: StreamFormat
    stream_codec: StreamCodec
    select_alternative: bool
    url: str
    real_quality_number: QualityNumber


class Live:
    def __init__(
        self,
        room_id: int,
        user_agent: str = '',
        cookie: str = '',
        *,
        auth_failure_reporter: Optional[Callable[[str], Awaitable[None]]] = None,
        session: Optional[Any] = None,
        network_route_manager: Optional['NetworkRouteManager'] = None,
        info_timeout_seconds: float = 10,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._logger = logger.bind(room_id=room_id)

        self._room_id = room_id
        self._user_agent = user_agent
        self._cookie = cookie
        self._update_headers()
        self._html_page_url = f'https://live.bilibili.com/{room_id}'

        self._owns_session = session is None
        self._network_route_manager = network_route_manager
        if session is None:
            self._session: Any = aiohttp.ClientSession(
                connector=connector,
                connector_owner=False,
                raise_for_status=True,
                trust_env=True,
                timeout=timeout,
            )
        else:
            self._session = session
        self._appapi = AppApi(
            self._session,
            self.headers,
            room_id=room_id,
            auth_failure_reporter=auth_failure_reporter,
        )
        self._webapi = WebApi(
            self._session,
            self.headers,
            room_id=room_id,
            auth_failure_reporter=auth_failure_reporter,
        )

        self._info_timeout_seconds = info_timeout_seconds
        self._monotonic = monotonic
        self._info_refresh_lock = asyncio.Lock()
        self._info_refresh_task: Optional['asyncio.Task[None]'] = None
        self._info_refresh_closed = False
        self._deinit_lock = asyncio.Lock()
        self._deinitialized = False
        self._info_revision = 0

        self._room_info: RoomInfo
        self._user_info: UserInfo
        self._no_flv_stream: bool
        self._real_quality_number: Optional[QualityNumber] = None

    @property
    def base_api_urls(self) -> List[str]:
        return self._webapi.base_api_urls

    @base_api_urls.setter
    def base_api_urls(self, value: List[str]) -> None:
        self._webapi.base_api_urls = value
        self._appapi.base_api_urls = value

    @property
    def base_live_api_urls(self) -> List[str]:
        return self._webapi.base_live_api_urls

    @base_live_api_urls.setter
    def base_live_api_urls(self, value: List[str]) -> None:
        self._webapi.base_live_api_urls = value
        self._appapi.base_live_api_urls = value

    @property
    def base_play_info_api_urls(self) -> List[str]:
        return self._webapi.base_play_info_api_urls

    @base_play_info_api_urls.setter
    def base_play_info_api_urls(self, value: List[str]) -> None:
        self._webapi.base_play_info_api_urls = value
        self._appapi.base_play_info_api_urls = value

    @property
    def user_agent(self) -> str:
        return self._user_agent

    @user_agent.setter
    def user_agent(self, value: str) -> None:
        self._user_agent = value
        self._update_headers()
        self._webapi.headers = self.headers
        self._appapi.headers = self.headers

    @property
    def cookie(self) -> str:
        return self._cookie

    @cookie.setter
    def cookie(self, value: str) -> None:
        self._cookie = value
        self._update_headers()
        self._webapi.headers = self.headers
        self._appapi.headers = self.headers

    @property
    def headers(self) -> Dict[str, str]:
        return self._headers

    @property
    def stream_headers(self) -> Dict[str, str]:
        return self._stream_headers

    def _update_headers(self) -> None:
        self._headers = {
            **BASE_HEADERS,
            'Referer': f'https://live.bilibili.com/{self._room_id}',
            'User-Agent': self._user_agent,
            'Cookie': self._cookie,
        }
        self._stream_headers = {
            **BASE_HEADERS,
            'Referer': f'https://live.bilibili.com/{self._room_id}',
            'User-Agent': self._user_agent,
        }

    @property
    def session(self) -> Any:
        return self._session

    @property
    def network_route_manager(self) -> Optional['NetworkRouteManager']:
        return self._network_route_manager

    @property
    def appapi(self) -> AppApi:
        return self._appapi

    @property
    def webapi(self) -> WebApi:
        return self._webapi

    @property
    def room_id(self) -> int:
        return self._room_id

    @property
    def real_quality_number(self) -> Optional[QualityNumber]:
        return self._real_quality_number

    @property
    def room_info(self) -> RoomInfo:
        return self._room_info

    @property
    def info_revision(self) -> int:
        return self._info_revision

    def replace_room_info(self, room_info: RoomInfo) -> None:
        self._room_info = room_info
        self._room_id = room_info.room_id

    @property
    def user_info(self) -> UserInfo:
        return self._user_info

    async def init(self) -> None:
        await self._refresh_info()

        self._no_flv_stream = False
        if self.is_living():
            streams = await self.get_live_streams()
            if streams:
                flv_formats = extract_formats(streams, 'flv')
                self._no_flv_stream = not flv_formats

    async def deinit(self) -> None:
        async with self._deinit_lock:
            if self._deinitialized:
                return
            async with self._info_refresh_lock:
                self._info_refresh_closed = True
                refresh_task = self._info_refresh_task
            try:
                if refresh_task is not None and not refresh_task.done():
                    refresh_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await refresh_task
            finally:
                if self._owns_session:
                    await self._session.close()
                self._deinitialized = True

    def has_no_flv_streams(self) -> bool:
        return self._no_flv_stream

    async def get_live_status(self) -> LiveStatus:
        try:
            # frequent requests will be intercepted by the server's firewall!
            live_status = await self._get_live_status_via_api()
        except Exception:
            # more cpu consumption
            live_status = await self._get_live_status_via_html_page()

        return LiveStatus(live_status)

    def is_living(self) -> bool:
        return self._room_info.live_status == LiveStatus.LIVE

    async def check_connectivity(self) -> bool:
        try:
            await self._session.head(
                'https://live.bilibili.com/',
                timeout=3,
                headers={'User-Agent': self._user_agent},
            )
            return True
        except Exception as e:
            self._logger.warning(f'Check connectivity failed: {repr(e)}')
            return False

    async def update_info(self, raise_exception: bool = False) -> bool:
        return await self._update_info('live', raise_exception)

    async def update_user_info(self, raise_exception: bool = False) -> bool:
        return await self._update_info('user', raise_exception)

    async def update_room_info(self, raise_exception: bool = False) -> bool:
        return await self._update_info('room', raise_exception)

    async def _update_info(self, projection: str, raise_exception: bool) -> bool:
        try:
            await self._refresh_info()
        except Exception as e:
            self._logger.error(f'Failed to update {projection} info: {repr(e)}')
            if raise_exception:
                raise
            return False
        else:
            return True

    async def get_room_info(self) -> RoomInfo:
        await self._refresh_info()
        return self._room_info

    async def get_user_info(self, uid: int) -> UserInfo:
        await self._refresh_info()
        return self._user_info

    async def _refresh_info(self) -> None:
        async with self._info_refresh_lock:
            if self._info_refresh_closed:
                raise RuntimeError('live information refresh is closed')
            refresh_task = self._info_refresh_task
            if refresh_task is None or refresh_task.done():
                refresh_task = asyncio.create_task(self._refresh_info_once())
                self._info_refresh_task = refresh_task
                refresh_task.add_done_callback(self._on_info_refresh_done)
        await asyncio.shield(refresh_task)

    def _on_info_refresh_done(self, refresh_task: 'asyncio.Task[None]') -> None:
        if self._info_refresh_task is refresh_task:
            self._info_refresh_task = None
        if not refresh_task.cancelled():
            refresh_task.exception()

    async def _refresh_info_once(self) -> None:
        snapshot = await asyncio.wait_for(
            self._load_info_snapshot(), timeout=self._info_timeout_seconds
        )
        self._room_info = snapshot.room_info
        self._user_info = snapshot.user_info
        self._room_id = snapshot.room_info.room_id
        self._info_revision += 1

    async def _load_info_snapshot(self) -> LiveInfoSnapshot:
        loaders: Tuple[Callable[[], Awaitable[ResponseData]], ...] = (
            lambda: self._webapi.get_info_by_room(self._room_id),
            lambda: self._appapi.get_info_by_room(self._room_id),
            self._get_room_info_res_via_html_page,
        )
        for loader in loaders:
            try:
                data = await loader()
                return LiveInfoSnapshot(
                    room_info=RoomInfo.from_data(data['room_info']),
                    user_info=UserInfo.from_info_by_room(data),
                )
            except Exception:
                continue
        raise ApiRequestError(-1, 'room information is unavailable')

    async def get_timestamp(self) -> int:
        try:
            ts = await self.get_server_timestamp()
        except Exception as e:
            self._logger.warning(f'Failed to get timestamp from server: {repr(e)}')
            ts = int(time.time())
        return ts

    async def get_server_timestamp(self) -> int:
        # the timestamp on the server at the moment in seconds
        return await self._webapi.get_timestamp()

    async def get_play_infos(
        self, qn: QualityNumber = 10000, api_platform: ApiPlatform = 'web'
    ) -> List[Any]:
        if api_platform == 'web':
            play_infos = await self._webapi.get_room_play_infos(self._room_id, qn)
        else:
            play_infos = await self._appapi.get_room_play_infos(self._room_id, qn)

        return play_infos

    async def get_live_streams(
        self, qn: QualityNumber = 10000, api_platform: ApiPlatform = 'web'
    ) -> List[Any]:
        play_infos = await self.get_play_infos(qn, api_platform)

        for info in play_infos:
            self._check_room_play_info(info)

        return extract_streams(play_infos)

    async def get_live_stream_url(
        self,
        qn: QualityNumber = 10000,
        *,
        api_platform: ApiPlatform = 'web',
        stream_format: StreamFormat = 'flv',
        stream_codec: StreamCodec = 'avc',
        select_alternative: bool = False,
    ) -> str:
        resolution = await self.resolve_live_stream(
            qn,
            api_platform=api_platform,
            stream_format=stream_format,
            stream_codec=stream_codec,
            select_alternative=select_alternative,
        )
        return resolution.url

    async def get_live_stream_snapshot(
        self,
        qn: QualityNumber = 10000,
        *,
        api_platform: ApiPlatform = 'web',
        stream_format: StreamFormat = 'flv',
        stream_codec: StreamCodec = 'avc',
        select_alternative: bool = False,
    ) -> LiveStreamSnapshot:
        streams = await self.get_live_streams(qn, api_platform=api_platform)
        return LiveStreamSnapshot(
            quality_number=qn,
            api_platform=api_platform,
            stream_format=stream_format,
            stream_codec=stream_codec,
            select_alternative=select_alternative,
            streams=tuple(streams),
            observed_at=self._monotonic(),
        )

    async def resolve_live_stream(
        self,
        qn: QualityNumber = 10000,
        *,
        api_platform: ApiPlatform = 'web',
        stream_format: StreamFormat = 'flv',
        stream_codec: StreamCodec = 'avc',
        select_alternative: bool = False,
        snapshot: Optional[LiveStreamSnapshot] = None,
    ) -> StreamResolution:
        resolution = self.resolve_live_stream_snapshot(
            snapshot,
            qn=qn,
            api_platform=api_platform,
            stream_format=stream_format,
            stream_codec=stream_codec,
            select_alternative=select_alternative,
        )
        if resolution is not None:
            return resolution
        streams = await self.get_live_streams(qn, api_platform=api_platform)
        resolution = self._select_stream(
            streams, qn, api_platform, stream_format, stream_codec, select_alternative
        )
        self._real_quality_number = resolution.real_quality_number
        return resolution

    def resolve_live_stream_snapshot(
        self,
        snapshot: Optional[LiveStreamSnapshot],
        *,
        qn: QualityNumber = 10000,
        api_platform: ApiPlatform = 'web',
        stream_format: StreamFormat = 'flv',
        stream_codec: StreamCodec = 'avc',
        select_alternative: bool = False,
    ) -> Optional[StreamResolution]:
        if not self._snapshot_matches(
            snapshot,
            qn=qn,
            api_platform=api_platform,
            stream_format=stream_format,
            stream_codec=stream_codec,
            select_alternative=select_alternative,
            max_age_seconds=2,
        ):
            return None
        assert snapshot is not None
        resolution = self._select_stream(
            list(snapshot.streams),
            qn,
            api_platform,
            stream_format,
            stream_codec,
            select_alternative,
        )
        self._real_quality_number = resolution.real_quality_number
        return resolution

    def _snapshot_matches(
        self,
        snapshot: Optional[LiveStreamSnapshot],
        *,
        qn: QualityNumber,
        api_platform: ApiPlatform,
        stream_format: StreamFormat,
        stream_codec: StreamCodec,
        select_alternative: bool,
        max_age_seconds: float,
    ) -> bool:
        if snapshot is None:
            return False
        age = self._monotonic() - snapshot.observed_at
        return (
            0 <= age <= max_age_seconds
            and snapshot.quality_number == qn
            and snapshot.api_platform == api_platform
            and snapshot.stream_format == stream_format
            and snapshot.stream_codec == stream_codec
            and snapshot.select_alternative == select_alternative
        )

    def _select_stream(
        self,
        streams: List[Any],
        qn: QualityNumber,
        api_platform: ApiPlatform,
        stream_format: StreamFormat,
        stream_codec: StreamCodec,
        select_alternative: bool,
    ) -> StreamResolution:
        if not streams:
            raise NoStreamAvailable(stream_format, stream_codec, qn)

        formats = extract_formats(streams, stream_format)
        if not formats:
            raise NoStreamFormatAvailable(stream_format, stream_codec, qn)

        codecs = extract_codecs(formats, stream_codec)
        if not codecs:
            raise NoStreamCodecAvailable(stream_format, stream_codec, qn)

        accept_qns = jsonpath(codecs, '$[*].accept_qn[*]')
        current_qns = jsonpath(codecs, '$[*].current_qn')
        if (
            not current_qns
            or not all(map(lambda value: value == current_qns[0], current_qns))
            or current_qns[0] not in accept_qns
        ):
            raise NoStreamQualityAvailable(stream_format, stream_codec, qn)
        real_quality_number = cast(QualityNumber, current_qns[0])

        def sort_by_host(info: Any) -> int:
            host = info['host']
            if match := re.search(r'gotcha(\d+)', host):
                num = match.group(1)
                if num == '04':
                    return 0
                if num == '09':
                    return 1
                if num == '08':
                    return 2
                if num == '05':
                    return 3
                if num == '07':
                    return 4
                return 1000 + int(num)
            elif 'mcdn' in host:
                return 2000
            elif re.search(r'cn-[a-z]+-[a-z]+', host):
                return 5000
            else:
                return 10000

        url_infos = sorted(
            ({**i, 'base_url': c['base_url']} for c in codecs for i in c['url_info']),
            key=sort_by_host,
        )
        urls = [i['host'] + i['base_url'] + i['extra'] for i in url_infos]

        if not select_alternative:
            url = urls[0]
        else:
            try:
                url = urls[1]
            except IndexError:
                raise NoAlternativeStreamAvailable(stream_format, stream_codec, qn)

        return StreamResolution(
            quality_number=qn,
            api_platform=api_platform,
            stream_format=stream_format,
            stream_codec=stream_codec,
            select_alternative=select_alternative,
            url=url,
            real_quality_number=real_quality_number,
        )

    def _check_room_play_info(self, data: ResponseData) -> None:
        if data.get('is_hidden'):
            raise LiveRoomHidden()
        if data.get('is_locked'):
            raise LiveRoomLocked()
        if data.get('encrypted') and not data.get('pwd_verified'):
            raise LiveRoomEncrypted()

    async def _get_live_status_via_api(self) -> int:
        room_info_data = await self._get_room_info_via_api()
        return int(room_info_data['live_status'])

    async def _get_room_info_via_api(self) -> ResponseData:
        try:
            info_data = await self._webapi.get_info_by_room(self._room_id)
            room_info_data = info_data['room_info']
        except Exception:
            try:
                info_data = await self._appapi.get_info_by_room(self._room_id)
                room_info_data = info_data['room_info']
            except Exception:
                room_info_data = await self._webapi.get_info(self._room_id)

        return room_info_data

    async def _get_live_status_via_html_page(self) -> int:
        async with self._session.get(self._html_page_url) as response:
            data = await response.read()

        m = _LIVE_STATUS_PATTERN.search(data)
        assert m is not None, data

        return int(m.group(1))

    async def _get_room_play_info_via_html_page(self) -> ResponseData:
        return await self._get_room_init_res_via_html_page()

    async def _get_room_info_res_via_html_page(self) -> ResponseData:
        info = await self._get_info_via_html_page()
        if info['roomInfoRes']['code'] != 0:
            raise ValueError(f"Invaild roomInfoRes: {info['roomInfoRes']}")
        return info['roomInfoRes']['data']

    async def _get_room_init_res_via_html_page(self) -> ResponseData:
        info = await self._get_info_via_html_page()
        if info['roomInitRes']['code'] != 0:
            raise ValueError(f"Invaild roomInitRes: {info['roomInitRes']}")
        return info['roomInitRes']['data']

    async def _get_info_via_html_page(self) -> ResponseData:
        async with self._session.get(self._html_page_url) as response:
            data = await response.read()

        match = _INFO_PATTERN.search(data)
        if not match:
            raise ValueError('Can not extract info from html page')

        string = match.group(1).decode(encoding='utf8')
        return json.loads(string)
