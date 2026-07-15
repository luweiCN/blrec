import json
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

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
async def test_fetch_empty_uid_list_skips_transport() -> None:
    session = FakeSession()
    client = BatchStatusClient(session)  # type: ignore[arg-type]

    result = await client.fetch([], observed_at=100.0)

    assert result.snapshots == {}
    assert result.missing_uids == frozenset()
    assert session.calls == []


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
@pytest.mark.parametrize(
    ('field', 'value'),
    [
        ('uid', True),
        ('room_id', 20.5),
        ('live_status', 1.0),
        ('live_time', False),
        ('live_time', 0.0),
    ],
)
async def test_fetch_treats_lossy_numeric_fields_as_missing(
    field: str, value: object
) -> None:
    item = room_data(uid=1, room_id=20, short_id=0, live_time=1000)
    item[field] = value
    session = FakeSession(FakeResponse({'code': 0, 'data': {'1': item}}))
    client = BatchStatusClient(session)  # type: ignore[arg-type]

    result = await client.fetch([1], observed_at=100.0)

    assert result.snapshots == {}
    assert result.missing_uids == frozenset({1})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('field', 'value', 'requested_uid', 'response_key'),
    [
        ('live_status', '00', 1, '1'),
        ('uid', 0, 0, '0'),
        ('room_id', 0, 1, '1'),
        ('uid', '01', 1, '1'),
        ('room_id', '020', 1, '1'),
        ('live_time', '01000', 1, '1'),
    ],
)
async def test_fetch_treats_noncanonical_or_nonpositive_fields_as_missing(
    field: str, value: object, requested_uid: int, response_key: str
) -> None:
    item = room_data(uid=requested_uid, room_id=20, short_id=0, live_time=1000)
    item[field] = value
    session = FakeSession(FakeResponse({'code': 0, 'data': {response_key: item}}))
    client = BatchStatusClient(session)  # type: ignore[arg-type]

    result = await client.fetch([requested_uid], observed_at=100.0)

    assert result.snapshots == {}
    assert result.missing_uids == frozenset({requested_uid})


@pytest.mark.asyncio
async def test_fetch_ignores_noncanonical_response_uid_key() -> None:
    session = FakeSession(
        FakeResponse({'code': 0, 'data': {'01': room_data(uid=1, room_id=20)}})
    )
    client = BatchStatusClient(session)  # type: ignore[arg-type]

    result = await client.fetch([1], observed_at=100.0)

    assert result.snapshots == {}
    assert result.missing_uids == frozenset({1})


@pytest.mark.asyncio
async def test_fetch_ignores_item_whose_uid_does_not_match_response_key() -> None:
    session = FakeSession(
        FakeResponse(
            {'code': 0, 'data': {'10': room_data(uid=11, room_id=20, short_id=0)}}
        )
    )
    client = BatchStatusClient(session)  # type: ignore[arg-type]

    result = await client.fetch([11], observed_at=100.0)

    assert result.snapshots == {}
    assert result.missing_uids == frozenset({11})


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
@pytest.mark.parametrize(
    ('field', 'value', 'requested_room_id'),
    [('room_id', 200.5, 200), ('short_id', 123.5, 123), ('uid', 42.5, 200)],
)
async def test_uid_mapping_ignores_lossy_numeric_fields(
    field: str, value: object, requested_room_id: int
) -> None:
    item = room_data()
    item[field] = value
    session = FakeSession(
        FakeResponse({'code': 0, 'data': {'by_room_ids': {'200': item}}})
    )
    client = AnonymousRoomClient(session)  # type: ignore[arg-type]

    mapping = await client.fetch_uid_mappings([requested_room_id])

    assert mapping == {}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('field', 'value', 'requested_room_id', 'response_key'),
    [('room_id', 0, 123, '0'), ('uid', 0, 200, '200')],
)
async def test_uid_mapping_ignores_nonpositive_identity_fields(
    field: str, value: object, requested_room_id: int, response_key: str
) -> None:
    item = room_data()
    item[field] = value
    session = FakeSession(
        FakeResponse({'code': 0, 'data': {'by_room_ids': {response_key: item}}})
    )
    client = AnonymousRoomClient(session)  # type: ignore[arg-type]

    mapping = await client.fetch_uid_mappings([requested_room_id])

    assert mapping == {}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('field', 'value', 'requested_room_id', 'response_key'),
    [
        ('room_id', '0200', 200, '200'),
        ('short_id', '0123', 123, '200'),
        ('uid', '042', 200, '200'),
        ('room_id', 200, 200, '0200'),
    ],
)
async def test_uid_mapping_ignores_noncanonical_decimal_strings(
    field: str, value: object, requested_room_id: int, response_key: str
) -> None:
    item = room_data()
    item[field] = value
    session = FakeSession(
        FakeResponse({'code': 0, 'data': {'by_room_ids': {response_key: item}}})
    )
    client = AnonymousRoomClient(session)  # type: ignore[arg-type]

    mapping = await client.fetch_uid_mappings([requested_room_id])

    assert mapping == {}


@pytest.mark.asyncio
async def test_uid_mapping_ignores_item_whose_room_id_does_not_match_key() -> None:
    session = FakeSession(
        FakeResponse(
            {'code': 0, 'data': {'by_room_ids': {'999': room_data(room_id=200)}}}
        )
    )
    client = AnonymousRoomClient(session)  # type: ignore[arg-type]

    mapping = await client.fetch_uid_mappings([200])

    assert mapping == {}


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
async def test_confirm_status_rejects_unrequested_room_identity() -> None:
    session = FakeSession(
        FakeResponse({'code': 0, 'data': room_data(room_id=200, short_id=999)})
    )
    client = AnonymousRoomClient(session)  # type: ignore[arg-type]

    with pytest.raises(BatchProtocolError, match='invalid room item'):
        await client.confirm_status(123)


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
async def test_load_room_info_normalizes_model_parsing_failure() -> None:
    data = room_data(live_status=0, live_time='0000-00-00 00:00:00')
    data['cover'] = 123
    session = FakeSession(
        FakeResponse({'code': 0, 'data': {'by_room_ids': {'200': data}}})
    )
    client = AnonymousRoomClient(session)  # type: ignore[arg-type]

    with pytest.raises(BatchProtocolError) as error:
        await client.load_room_info(123)

    assert str(error.value) == 'invalid room item'


@pytest.mark.asyncio
async def test_load_room_info_normalizes_canonical_numeric_strings() -> None:
    data = room_data()
    data.update(
        {
            'uid': '42',
            'room_id': '200',
            'short_id': '123',
            'area_id': '1',
            'area_name': 'Area 001',
            'parent_area_id': '2',
            'parent_area_name': 'Parent 002',
            'live_status': '2',
            'live_time': '1000',
            'online': '3',
            'title': 'Title 003',
            'tags': 'Tags 004',
            'description': 'Description 005',
        }
    )
    session = FakeSession(
        FakeResponse({'code': 0, 'data': {'by_room_ids': {'200': data}}})
    )
    client = AnonymousRoomClient(session)  # type: ignore[arg-type]

    room_info = await client.load_room_info(123)

    assert room_info.uid == 42
    assert room_info.room_id == 200
    assert room_info.short_room_id == 123
    assert room_info.area_id == 1
    assert room_info.parent_area_id == 2
    assert room_info.live_status is LiveStatus.ROUND
    assert room_info.live_start_time == 1000
    assert room_info.online == 3
    assert room_info.area_name == 'Area 001'
    assert room_info.parent_area_name == 'Parent 002'
    assert room_info.title == 'Title 003'
    assert room_info.tags == 'Tags 004'
    assert room_info.description == 'Description 005'


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('field', 'value'),
    [
        ('area_id', True),
        ('parent_area_id', 2.5),
        ('online', '03'),
        ('live_status', 1.0),
        ('live_status', 3),
        ('live_start_time', True),
        ('live_start_time', 1000.5),
        ('live_start_time', '01000'),
        ('live_time', '2026-07-12 08:00:00.500000'),
    ],
)
async def test_load_room_info_rejects_invalid_numeric_fields(
    field: str, value: object
) -> None:
    data = room_data()
    data[field] = value
    session = FakeSession(
        FakeResponse({'code': 0, 'data': {'by_room_ids': {'200': data}}})
    )
    client = AnonymousRoomClient(session)  # type: ignore[arg-type]

    with pytest.raises(BatchProtocolError) as error:
        await client.load_room_info(123)

    assert str(error.value) == 'invalid room item'


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
        with pytest.raises(ValueError, match='default headers'):
            BatchStatusClient(session)
        with pytest.raises(ValueError, match='default headers'):
            AnonymousRoomClient(session)
    finally:
        await session.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'session_kwargs',
    [
        {'auth': aiohttp.BasicAuth('user', 'secret')},
        {'trust_env': True},
        {'headers': {'X-Bili-Device-Id': 'secret'}},
    ],
    ids=['basic-auth', 'trust-env', 'device-header'],
)
async def test_clients_reject_real_session_with_unsafe_defaults(
    session_kwargs: Dict[str, Any]
) -> None:
    session = aiohttp.ClientSession(
        cookie_jar=aiohttp.DummyCookieJar(), **session_kwargs
    )
    try:
        with pytest.raises(ValueError, match='anonymous session'):
            BatchStatusClient(session)
        with pytest.raises(ValueError, match='anonymous session'):
            AnonymousRoomClient(session)
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_batch_client_revalidates_session_before_request() -> None:
    session = FakeSession()
    client = BatchStatusClient(session)  # type: ignore[arg-type]
    session.headers['X-Bili-Device-Id'] = 'secret'

    with pytest.raises(ValueError, match='default headers'):
        await client.fetch([10], observed_at=100.0)

    assert session.calls == []


@pytest.mark.asyncio
async def test_room_client_revalidates_session_before_request() -> None:
    session = FakeSession()
    client = AnonymousRoomClient(session)  # type: ignore[arg-type]
    session.headers['X-Bili-Device-Id'] = 'secret'

    with pytest.raises(ValueError, match='default headers'):
        await client.fetch_uid_mappings([123])

    assert session.calls == []


@pytest.mark.asyncio
async def test_real_session_outbound_headers_are_anonymous() -> None:
    received_headers: List[Dict[str, str]] = []

    async def batch_handler(request: web.Request) -> web.Response:
        received_headers.append(dict(request.headers))
        return web.json_response(
            {'code': 0, 'data': {'10': room_data(uid=10, room_id=20)}}
        )

    async def base_info_handler(request: web.Request) -> web.Response:
        received_headers.append(dict(request.headers))
        return web.json_response(
            {'code': 0, 'data': {'by_room_ids': {'200': room_data()}}}
        )

    app = web.Application()
    app.router.add_get(BatchStatusClient.PATH, batch_handler)
    app.router.add_get(AnonymousRoomClient.BASE_INFO_PATH, base_info_handler)
    server = TestServer(app)
    await server.start_server()
    session = aiohttp.ClientSession(cookie_jar=aiohttp.DummyCookieJar())
    try:
        base_url = str(server.make_url('/')).rstrip('/')
        batch_client = BatchStatusClient(session, base_url=base_url)
        room_client = AnonymousRoomClient(session, base_url=base_url)

        await batch_client.fetch([10], observed_at=100.0)
        await room_client.fetch_uid_mappings([200])
    finally:
        await session.close()
        await server.close()

    assert len(received_headers) == 2
    allowed_headers = {'accept', 'accept-encoding', 'host', 'user-agent'}
    for headers in received_headers:
        assert {name.lower() for name in headers} <= allowed_headers
