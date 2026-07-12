import json
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import aiohttp
import pytest

from blrec.bili.anonymous_room_client import AnonymousRoomClient
from blrec.bili.batch_status_client import (
    BatchApiError,
    BatchProtocolError,
    BatchStatusClient,
)
from blrec.bili.live_status import ObservedStatus, StatusSource
from blrec.bili.models import LiveStatus


class FakeResponse:
    def __init__(
        self, payload: object = None, *, status: int = 200, body: Optional[str] = None
    ) -> None:
        self.status = status
        self._body = body if body is not None else json.dumps(payload)

    async def __aenter__(self) -> 'FakeResponse':
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def text(self) -> str:
        return self._body


class FakeSession:
    def __init__(self, *responses: FakeResponse) -> None:
        self.cookie_jar = aiohttp.DummyCookieJar()
        self.headers: Dict[str, str] = {}
        self.calls: List[Tuple[str, Dict[str, Any]]] = []
        self._responses = list(responses)

    def get(self, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append((url, kwargs))
        return self._responses.pop(0)


def room_data(
    *,
    uid: int = 42,
    room_id: int = 200,
    short_id: int = 123,
    live_status: int = 1,
    live_time: object = '2026-07-12 08:00:00',
) -> Dict[str, Any]:
    return {
        'uid': uid,
        'room_id': room_id,
        'short_id': short_id,
        'area_id': 1,
        'area_name': 'Area',
        'parent_area_id': 2,
        'parent_area_name': 'Parent',
        'live_status': live_status,
        'live_time': live_time,
        'online': 3,
        'title': 'Title',
        'cover': '',
        'tags': '',
        'description': '',
    }


def assert_anonymous_get(call: Tuple[str, Dict[str, Any]]) -> None:
    _, request = call
    sensitive_headers = {'cookie', 'authorization', 'x-api-key'}
    assert not sensitive_headers & {name.lower() for name in request['headers']}
    assert request['allow_redirects'] is False
    assert 'auth' not in request
    assert 'cookies' not in request


@pytest.mark.asyncio
async def test_fetch_uses_anonymous_get_and_marks_partial_items_missing() -> None:
    session = FakeSession(
        FakeResponse(
            {
                'code': 0,
                'data': {
                    '10': room_data(uid=10, room_id=20, short_id=0, live_time=1000),
                    '11': {'uid': 11, 'live_status': 0, 'live_time': 0},
                    '12': room_data(uid=12, room_id=22, live_status=9),
                },
            }
        )
    )
    client = BatchStatusClient(session)  # type: ignore[arg-type]

    result = await client.fetch([10, 11, 12, 13, 10], observed_at=100.0)

    assert len(session.calls) == 1
    url, request = session.calls[0]
    assert url.endswith('/room/v1/Room/get_status_info_by_uids')
    assert request['params'] == [
        ('uids[]', '10'),
        ('uids[]', '11'),
        ('uids[]', '12'),
        ('uids[]', '13'),
    ]
    assert_anonymous_get(session.calls[0])
    assert result.snapshots[10].status is ObservedStatus.LIVE
    assert result.snapshots[10].source is StatusSource.BATCH
    assert result.snapshots[10].observation_key == '10:1000'
    assert result.missing_uids == frozenset({11, 12, 13})


@pytest.mark.asyncio
@pytest.mark.parametrize('code', [-352, -412])
async def test_fetch_raises_sanitized_numeric_api_error_without_retry(
    code: int,
) -> None:
    session = FakeSession(FakeResponse({'code': code, 'message': 'cookie required'}))
    client = BatchStatusClient(session)  # type: ignore[arg-type]

    with pytest.raises(BatchApiError) as error:
        await client.fetch([10], observed_at=100.0)

    assert error.value.code == code
    assert 'cookie' not in str(error.value).lower()
    assert len(session.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'response',
    [
        FakeResponse(status=429),
        FakeResponse(body='not JSON'),
        FakeResponse({'code': 0, 'data': []}),
    ],
)
async def test_fetch_raises_protocol_error_for_whole_response_failure(
    response: FakeResponse,
) -> None:
    session = FakeSession(response)
    client = BatchStatusClient(session)  # type: ignore[arg-type]

    with pytest.raises(BatchProtocolError):
        await client.fetch([10], observed_at=100.0)

    assert len(session.calls) == 1


@pytest.mark.asyncio
async def test_anonymous_room_client_maps_requested_short_and_real_ids() -> None:
    session = FakeSession(
        FakeResponse(
            {
                'code': 0,
                'data': {
                    'by_room_ids': {
                        '200': room_data(),
                        '999': {'room_id': 'bad', 'uid': 99, 'short_id': 999},
                    }
                },
            }
        )
    )
    client = AnonymousRoomClient(session)  # type: ignore[arg-type]

    mapping = await client.fetch_uid_mappings([123, 200, 999, 123])

    assert mapping == {123: (200, 42), 200: (200, 42)}
    url, request = session.calls[0]
    assert url.endswith('/xlive/web-room/v1/index/getRoomBaseInfo')
    assert request['params'] == [
        ('req_biz', 'web_room_componet'),
        ('room_ids', '123'),
        ('room_ids', '200'),
        ('room_ids', '999'),
    ]
    assert_anonymous_get(session.calls[0])


@pytest.mark.asyncio
async def test_confirm_status_uses_anonymous_single_room_read_and_clock() -> None:
    session = FakeSession(FakeResponse({'code': 0, 'data': room_data()}))
    client = AnonymousRoomClient(session, clock=lambda: 321.5)  # type: ignore[arg-type]

    snapshot = await client.confirm_status(123)

    expected_live_time = int(
        datetime(2026, 7, 12, 8, tzinfo=timezone(timedelta(hours=8))).timestamp()
    )
    assert snapshot.uid == 42
    assert snapshot.room_id == 200
    assert snapshot.status is ObservedStatus.LIVE
    assert snapshot.source is StatusSource.CONFIRMATION
    assert snapshot.observed_at == 321.5
    assert snapshot.live_time == expected_live_time
    assert snapshot.observation_key == '42:{}'.format(expected_live_time)
    url, request = session.calls[0]
    assert url.endswith('/room/v1/Room/get_info')
    assert request['params'] == [('room_id', '123')]
    assert_anonymous_get(session.calls[0])


@pytest.mark.asyncio
async def test_load_room_info_uses_anonymous_base_info_read() -> None:
    data = room_data(live_status=0, live_time='0000-00-00 00:00:00')
    session = FakeSession(
        FakeResponse({'code': 0, 'data': {'by_room_ids': {str(data['room_id']): data}}})
    )
    client = AnonymousRoomClient(session)  # type: ignore[arg-type]

    room_info = await client.load_room_info(123)

    assert room_info.uid == 42
    assert room_info.room_id == 200
    assert room_info.short_room_id == 123
    assert room_info.live_status is LiveStatus.PREPARING
    url, request = session.calls[0]
    assert url.endswith('/xlive/web-room/v1/index/getRoomBaseInfo')
    assert request['params'] == [('req_biz', 'web_room_componet'), ('room_ids', '123')]
    assert_anonymous_get(session.calls[0])


@pytest.mark.asyncio
async def test_clients_reject_real_session_with_non_dummy_cookie_jar() -> None:
    session = aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar())
    try:
        with pytest.raises(ValueError, match='DummyCookieJar'):
            BatchStatusClient(session)
        with pytest.raises(ValueError, match='DummyCookieJar'):
            AnonymousRoomClient(session)
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_clients_reject_real_session_with_default_credentials() -> None:
    session = aiohttp.ClientSession(
        cookie_jar=aiohttp.DummyCookieJar(), headers={'Authorization': 'secret'}
    )
    try:
        with pytest.raises(ValueError, match='credential headers'):
            BatchStatusClient(session)
        with pytest.raises(ValueError, match='credential headers'):
            AnonymousRoomClient(session)
    finally:
        await session.close()
