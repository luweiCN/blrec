import asyncio
import json
from pathlib import Path
from typing import Any, Dict, List, Mapping, Tuple

import pytest

from blrec.bili_upload.crypto import CookieRecord, CredentialBundle
from blrec.bili_upload.errors import (
    BiliApiError,
    DefinitelyNotSent,
    ProtocolContractError,
    RemoteOutcomeUnknown,
)
from blrec.bili_upload.protocol import (
    AiohttpProtocolTransport,
    BiliProtocolClient,
    ProtocolRequest,
    ProtocolResponse,
    TransportFailure,
)
from blrec.bili_upload.signing import (
    PROTOCOL_MATRIX,
    BiliTvSigner,
    WbiSigner,
    WebSessionBuilder,
)

FIXTURE_PATH = Path(__file__).parent / 'fixtures' / 'protocol' / 'responses.json'


def credential_fixture() -> CredentialBundle:
    return CredentialBundle(
        access_token='access-secret',
        refresh_token='refresh-secret',
        mid=42,
        issued_at=100,
        expires_at=4102444800,
        signing_family='tv',
        app_client_version='1.0.0',
        web_client_version='2.0.0',
        app_device_source='qr',
        web_device_source='nav',
        app_device_id='app-device',
        app_buvid='app-buvid',
        web_buvid3='web-buvid3',
        web_buvid4='web-buvid4',
        web_b_nut='web-b-nut',
        cookies=(
            CookieRecord(
                name='SESSDATA',
                value='cookie-secret',
                domain='.bilibili.com',
                path='/',
                expires_at=None,
                secure=True,
                http_only=True,
            ),
            CookieRecord(
                name='bili_jct',
                value='csrf-secret',
                domain='.bilibili.com',
                path='/',
                expires_at=None,
                secure=True,
                http_only=False,
            ),
        ),
    )


async def wbi_keys() -> Tuple[str, str]:
    return ('7cd084941338484aae1ad9425b84077c', '4932caff0ff746eab6f01bf08b70ac45')


class ScriptedTransport:
    def __init__(self, fixtures: Mapping[str, Any]) -> None:
        self.fixtures = fixtures
        self.requests: List[ProtocolRequest] = []

    async def send(self, request: ProtocolRequest) -> ProtocolResponse:
        self.requests.append(request)
        payload = self.fixtures[request.operation]
        return ProtocolResponse(
            status=200, headers={}, body=json.dumps(payload).encode('utf8')
        )


def protocol_client(transport: Any) -> BiliProtocolClient:
    return BiliProtocolClient(
        transport=transport,
        wbi_signer=WbiSigner(wbi_keys, clock=lambda: 1748867128),
        web_session_builder=WebSessionBuilder(clock=lambda: 100),
        clock=lambda: 100,
    )


@pytest.mark.parametrize(
    ('operation', 'auth_mode', 'path'),
    [
        ('create_qr', 'bilitv_sign', '/x/passport-tv-login/qrcode/auth_code'),
        ('poll_qr', 'bilitv_sign', '/x/passport-tv-login/qrcode/poll'),
        ('oauth_info', 'bilitv_token_sign', '/x/passport-login/oauth2/info'),
        (
            'refresh_token',
            'bilitv_token_sign',
            '/x/passport-login/oauth2/refresh_token',
        ),
        ('preupload', 'web_cookie', '/preupload'),
        ('upload_chunk', 'upos_session', '<server-returned>'),
        ('complete_upload', 'upos_session', '<server-returned>'),
        ('submit_archive', 'web_cookie_csrf', '/x/vu/web/add/v3'),
        ('edit_archive', 'web_cookie_csrf', '/x/vu/web/edit'),
        ('upload_cover', 'web_cookie_csrf', '/x/vu/web/cover/up'),
        ('list_collections', 'web_cookie', '/x2/creative/web/seasons'),
        ('create_collection', 'web_cookie_csrf', '/x2/creative/web/season/add'),
        (
            'add_collection_episode',
            'web_cookie_csrf',
            '/x2/creative/web/season/section/episodes/add',
        ),
        ('archive_pre', 'web_cookie', '/x/vupre/web/archive/pre'),
        ('list_archives', 'web_cookie', '/x/web/archives'),
        ('archive_view', 'web_cookie', '/x/vupre/web/archive/view'),
        ('web_nav', 'web_cookie', '/x/web-interface/nav'),
        ('list_replies', 'web_cookie_wbi', '/x/v2/reply/main'),
        ('reply_detail', 'web_cookie_wbi', '/x/v2/reply/detail'),
        ('add_reply', 'web_cookie_csrf', '/x/v2/reply/add'),
        ('top_reply', 'web_cookie_csrf', '/x/v2/reply/top'),
        ('post_danmaku', 'web_cookie_csrf_wbi', '/x/v2/dm/post'),
    ],
)
def test_operation_has_one_auth_mode(operation: str, auth_mode: str, path: str) -> None:
    spec = PROTOCOL_MATRIX[operation]

    assert spec.auth_mode == auth_mode
    assert spec.path == path


def test_fixture_covers_every_protocol_operation() -> None:
    fixtures = json.loads(FIXTURE_PATH.read_text())

    assert set(PROTOCOL_MATRIX) <= set(fixtures)
    assert 'preupload_init' in fixtures


def test_tv_signing_is_canonical_and_does_not_mutate_input() -> None:
    signer = BiliTvSigner()
    original = {'z': 'last', 'a': 'first'}

    first = signer.sign(original)
    second = signer.sign({'a': 'first', 'z': 'last'})

    assert first == second
    assert original == {'z': 'last', 'a': 'first'}
    assert list(first)[-1] == 'sign'
    assert len(first['sign']) == 32


@pytest.mark.asyncio
async def test_wbi_signing_matches_the_pinned_vector() -> None:
    signer = WbiSigner(wbi_keys, clock=lambda: 1748867128)

    signed = await signer.sign({'foo': ")-_-( F**' 哔~!", 'bar': 2333})

    assert signed == {
        'bar': '2333',
        'foo': '-_-%20F%20%E5%93%94~',
        'wts': '1748867128',
        'w_rid': '6ba96e28a3f09b40e704f1e4b4f8e3e3',
    }


def test_web_cookie_jar_honours_domain_path_expiry_and_csrf() -> None:
    bundle = credential_fixture()
    builder = WebSessionBuilder(clock=lambda: 100)

    header = builder.cookie_header(bundle, 'https://api.bilibili.com/x/test')

    assert 'SESSDATA=cookie-secret' in header
    assert 'bili_jct=csrf-secret' in header
    assert builder.csrf(bundle) == 'csrf-secret'
    assert builder.cookie_header(bundle, 'https://example.invalid/x/test') == ''


@pytest.mark.asyncio
async def test_all_operations_use_only_their_allowed_auth_scope() -> None:
    fixtures: Dict[str, Any] = json.loads(FIXTURE_PATH.read_text())
    transport = ScriptedTransport(fixtures)
    client = protocol_client(transport)
    bundle = credential_fixture()

    await client.create_qr({'device_id': 'qr-device'})
    await client.poll_qr({'auth_code': 'fixture-auth-code'})
    await client.oauth_info(bundle)
    await client.refresh_token(bundle)
    prepared = await client.preupload(
        bundle, {'name': 'fixture.mp4', 'size': 4, 'r': 'upos'}
    )
    await client.upload_chunk(
        prepared.session, chunk_no=0, chunks=1, start=0, total=4, body=b'data'
    )
    await client.complete_upload(
        prepared.session, parts=({'partNumber': 1, 'eTag': 'fixture-etag'},)
    )
    await client.submit_archive(bundle, {'title': 'fixture', 'videos': 'fixture.mp4'})
    await client.edit_archive(bundle, {'aid': 303, 'title': 'fixture'})
    await client.upload_cover(
        bundle, filename='fixture.jpg', mime_type='image/jpeg', content=b'fixture-cover'
    )
    await client.list_collections(bundle)
    await client.create_collection(
        bundle,
        title='fixture collection',
        description='fixture description',
        cover_url='https://archive.biliimg.com/bfs/archive/fixture.jpg',
    )
    await client.add_collection_episode(
        bundle, section_id=502, aid=303, cid=202, title='fixture title'
    )
    await client.archive_pre(bundle)
    await client.list_archives(bundle, {'pn': 1})
    await client.archive_view(bundle, {'bvid': 'BVfixture'})
    await client.web_nav(bundle)
    await client.list_replies(bundle, {'oid': 303, 'type': 1})
    await client.reply_detail(bundle, {'oid': 303, 'root': 101, 'type': 1})
    await client.add_reply(bundle, {'oid': 303, 'message': 'fixture', 'type': 1})
    await client.top_reply(bundle, {'oid': 303, 'rpid': 101, 'type': 1})
    await client.post_danmaku(bundle, {'oid': 202, 'msg': 'fixture', 'progress': 1})

    requests = {request.operation: request for request in transport.requests}
    for name in ('create_qr', 'poll_qr', 'oauth_info', 'refresh_token'):
        request = requests[name]
        assert 'Cookie' not in request.headers
        assert 'csrf' not in dict(request.query)
        assert 'csrf' not in dict(request.form)
    for name in ('create_qr', 'poll_qr', 'oauth_info', 'refresh_token'):
        assert requests[name].headers == {
            'Referer': 'https://www.bilibili.com/',
            'User-Agent': (
                'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
                'Chrome/63.0.3239.108'
            ),
        }
    for name in (
        'preupload',
        'archive_pre',
        'list_archives',
        'archive_view',
        'submit_archive',
        'edit_archive',
        'upload_cover',
        'list_collections',
        'create_collection',
        'add_collection_episode',
        'web_nav',
        'list_replies',
        'reply_detail',
        'add_reply',
        'top_reply',
        'post_danmaku',
    ):
        request = requests[name]
        material = dict(request.query)
        material.update(dict(request.form))
        assert 'access_key' not in material
        assert 'refresh_token' not in material
        assert 'Cookie' in request.headers
        if name in (
            'submit_archive',
            'edit_archive',
            'upload_cover',
            'list_collections',
            'create_collection',
            'add_collection_episode',
        ):
            assert request.headers['Referer'].startswith('https://member.bilibili.com/')
        else:
            assert request.headers['Referer'] == 'https://www.bilibili.com/'
        assert request.headers['User-Agent'].startswith('Mozilla/5.0 ')
    for name in ('preupload_init', 'upload_chunk', 'complete_upload'):
        request = requests[name]
        rendered = json.dumps(request.safe_shape(), sort_keys=True)
        assert request.headers == {'X-Upos-Auth': 'fixture-upos-auth'}
        assert 'cookie-secret' not in rendered
        assert 'access-secret' not in rendered
        assert 'csrf-secret' not in rendered
        assert dict(request.query).get('uploadId') == 'fixture-upload-id' or name == (
            'preupload_init'
        )
    for name in ('list_replies', 'reply_detail', 'post_danmaku'):
        assert 'w_rid' in dict(requests[name].query)
    for name in (
        'preupload',
        'archive_pre',
        'list_archives',
        'archive_view',
        'add_reply',
        'top_reply',
        'submit_archive',
        'edit_archive',
        'upload_cover',
        'list_collections',
        'create_collection',
        'add_collection_episode',
    ):
        assert 'w_rid' not in dict(requests[name].query)

    submit = requests['submit_archive']
    submit_query = dict(submit.query)
    submit_body = json.loads((submit.body or b'{}').decode('utf8'))
    assert submit_query == {
        'csrf': 'csrf-secret',
        't': '100000',
        'web_location': '333.1024',
    }
    assert submit_body == {
        'title': 'fixture',
        'videos': 'fixture.mp4',
        'csrf': 'csrf-secret',
    }
    assert submit.headers['Content-Type'] == 'application/json'
    assert 'access_key' not in submit_query

    edit = requests['edit_archive']
    assert dict(edit.query) == {'csrf': 'csrf-secret', 't': '100000'}
    assert json.loads((edit.body or b'{}').decode('utf8')) == {
        'aid': 303,
        'title': 'fixture',
        'csrf': 'csrf-secret',
    }
    assert edit.headers['Content-Type'] == 'application/json'

    cover_request = requests['upload_cover']
    assert cover_request.method == 'POST'
    assert dict(cover_request.query) == {'ts': '100000'}
    cover_form = dict(cover_request.form)
    assert cover_form['csrf'] == 'csrf-secret'
    assert cover_form['cover'].startswith('data:image/jpeg;base64,')
    assert 'fixture-cover' not in cover_form['cover']

    collection_list = requests['list_collections']
    assert collection_list.method == 'GET'
    assert dict(collection_list.query) == {
        'pn': '1',
        'ps': '50',
        'order': 'mtime',
        'sort': 'desc',
        'draft': '1',
    }

    collection_create = requests['create_collection']
    assert collection_create.method == 'POST'
    assert dict(collection_create.form) == {
        'title': 'fixture collection',
        'desc': 'fixture description',
        'cover': 'https://archive.biliimg.com/bfs/archive/fixture.jpg',
        'season_price': '0',
        'csrf': 'csrf-secret',
    }

    episode_request = requests['add_collection_episode']
    assert episode_request.method == 'POST'
    assert dict(episode_request.query) == {'csrf': 'csrf-secret'}
    assert json.loads((episode_request.body or b'{}').decode('utf8')) == {
        'sectionId': 502,
        'episodes': [
            {'aid': 303, 'cid': 202, 'title': 'fixture title', 'charging_pay': 0}
        ],
        'csrf': 'csrf-secret',
    }


@pytest.mark.asyncio
async def test_upload_cover_returns_only_a_validated_https_url() -> None:
    fixtures = json.loads(FIXTURE_PATH.read_text())
    client = protocol_client(ScriptedTransport(fixtures))

    url = await client.upload_cover(
        credential_fixture(),
        filename='fixture.jpg',
        mime_type='image/jpeg',
        content=b'fixture-cover',
    )

    assert url == 'https://archive.biliimg.com/bfs/archive/fixture.jpg'

    fixtures['upload_cover'] = {'code': 0, 'data': {'url': 'http://bad.invalid/x'}}
    with pytest.raises(ProtocolContractError, match='cover'):
        await protocol_client(ScriptedTransport(fixtures)).upload_cover(
            credential_fixture(),
            filename='fixture.jpg',
            mime_type='image/jpeg',
            content=b'fixture-cover',
        )


class FailingTransport:
    def __init__(self, *, headers_sent: bool) -> None:
        self.headers_sent = headers_sent

    async def send(self, request: ProtocolRequest) -> ProtocolResponse:
        raise TransportFailure(headers_sent=self.headers_sent)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('headers_sent', 'expected'),
    [(False, DefinitelyNotSent), (True, RemoteOutcomeUnknown)],
)
async def test_non_idempotent_transport_failures_preserve_send_boundary(
    headers_sent: bool, expected: Any
) -> None:
    client = protocol_client(FailingTransport(headers_sent=headers_sent))

    with pytest.raises(expected) as error:
        await client.add_reply(
            credential_fixture(), {'oid': 303, 'message': 'body-secret', 'type': 1}
        )

    rendered = str(error.value) + repr(error.value)
    assert 'body-secret' not in rendered
    assert 'cookie-secret' not in rendered


class StaticResponseTransport:
    def __init__(self, response: ProtocolResponse) -> None:
        self.response = response

    async def send(self, request: ProtocolRequest) -> ProtocolResponse:
        return self.response


@pytest.mark.asyncio
async def test_http_client_error_retains_the_safe_operation_name() -> None:
    client = protocol_client(
        StaticResponseTransport(ProtocolResponse(status=412, headers={}, body=b''))
    )

    with pytest.raises(BiliApiError) as error:
        await client.web_nav(credential_fixture())

    assert error.value.code == 412
    assert error.value.operation == 'web_nav'


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'response',
    [
        ProtocolResponse(
            status=500,
            headers={'Set-Cookie': 'upstream-cookie-secret'},
            body=b'access_key=upstream-access-secret',
        ),
        ProtocolResponse(
            status=200,
            headers={},
            body=b'not-json refresh_token=upstream-refresh-secret',
        ),
    ],
)
async def test_unknown_outcome_errors_do_not_echo_upstream_material(
    response: ProtocolResponse,
) -> None:
    client = protocol_client(StaticResponseTransport(response))

    with pytest.raises(RemoteOutcomeUnknown) as error:
        await client.top_reply(credential_fixture(), {'oid': 303, 'rpid': 101})

    rendered = str(error.value) + repr(error.value)
    for forbidden in (
        'upstream-cookie-secret',
        'upstream-access-secret',
        'upstream-refresh-secret',
        'not-json',
    ):
        assert forbidden not in rendered


@pytest.mark.asyncio
async def test_api_business_error_is_code_only_when_message_contains_secrets() -> None:
    response = ProtocolResponse(
        status=200,
        headers={},
        body=json.dumps(
            {'code': -412, 'message': 'challenge access_key=leaked Cookie=also-leaked'}
        ).encode('utf8'),
    )
    client = protocol_client(StaticResponseTransport(response))

    with pytest.raises(BiliApiError) as error:
        await client.add_reply(credential_fixture(), {'oid': 303, 'message': 'x'})

    assert error.value.code == -412
    assert error.value.operation == 'add_reply'
    rendered = str(error.value) + repr(error.value)
    assert 'leaked' not in rendered
    assert 'Cookie' not in rendered


@pytest.mark.asyncio
async def test_preupload_rejects_non_https_dynamic_endpoint_before_sending() -> None:
    fixtures = json.loads(FIXTURE_PATH.read_text())
    fixtures['preupload'] = {
        **fixtures['preupload'],
        'endpoint': 'http://upos.example.invalid',
    }
    transport = ScriptedTransport(fixtures)
    client = protocol_client(transport)

    with pytest.raises(ProtocolContractError, match='UPOS target'):
        await client.preupload(credential_fixture(), {'name': 'fixture.mp4', 'size': 4})

    assert [request.operation for request in transport.requests] == ['preupload']


@pytest.mark.asyncio
async def test_upos_session_is_bound_to_the_client_that_preuploaded_it() -> None:
    fixtures = json.loads(FIXTURE_PATH.read_text())
    first_transport = ScriptedTransport(fixtures)
    first = protocol_client(first_transport)
    prepared = await first.preupload(
        credential_fixture(), {'name': 'fixture.mp4', 'size': 4}
    )
    second_transport = ScriptedTransport(fixtures)
    second = protocol_client(second_transport)

    with pytest.raises(ProtocolContractError, match='UPOS session'):
        await second.upload_chunk(
            prepared.session, chunk_no=0, chunks=1, start=0, total=4, body=b'data'
        )

    assert second_transport.requests == []


@pytest.mark.asyncio
async def test_upos_session_can_be_restored_without_persisting_client_owner() -> None:
    fixtures = json.loads(FIXTURE_PATH.read_text())
    first = protocol_client(ScriptedTransport(fixtures))
    prepared = await first.preupload(
        credential_fixture(), {'name': '/recordings/local-name.flv', 'size': 4}
    )
    persisted = first.export_upos_session(prepared.session)
    second_transport = ScriptedTransport(fixtures)
    second = protocol_client(second_transport)

    restored = second.restore_upos_session(persisted)
    await second.upload_chunk(
        restored, chunk_no=0, chunks=1, start=0, total=4, body=b'data'
    )

    assert 'owner_token' not in persisted
    assert restored.file_name == '/recordings/local-name.flv'
    assert restored.remote_file_name == 'fixture'
    assert [request.operation for request in second_transport.requests] == [
        'upload_chunk'
    ]


@pytest.mark.asyncio
async def test_upload_chunk_accepts_empty_success_response() -> None:
    fixtures = json.loads(FIXTURE_PATH.read_text())

    class EmptyChunkTransport(ScriptedTransport):
        async def send(self, request: ProtocolRequest) -> ProtocolResponse:
            if request.operation == 'upload_chunk':
                self.requests.append(request)
                return ProtocolResponse(status=200, headers={}, body=b'')
            return await super().send(request)

    transport = EmptyChunkTransport(fixtures)
    client = protocol_client(transport)
    prepared = await client.preupload(
        credential_fixture(), {'name': 'fixture.mp4', 'size': 4}
    )

    response = await client.upload_chunk(
        prepared.session, chunk_no=0, chunks=1, start=0, total=4, body=b'data'
    )

    assert response == {}


@pytest.mark.asyncio
async def test_upload_chunk_accepts_non_json_success_response() -> None:
    fixtures = json.loads(FIXTURE_PATH.read_text())

    class PlainTextChunkTransport(ScriptedTransport):
        async def send(self, request: ProtocolRequest) -> ProtocolResponse:
            if request.operation == 'upload_chunk':
                self.requests.append(request)
                return ProtocolResponse(
                    status=200,
                    headers={'Content-Type': 'application/octet-stream'},
                    body=b'fixture-upos-success',
                )
            return await super().send(request)

    transport = PlainTextChunkTransport(fixtures)
    client = protocol_client(transport)
    prepared = await client.preupload(
        credential_fixture(), {'name': 'fixture.mp4', 'size': 4}
    )

    response = await client.upload_chunk(
        prepared.session, chunk_no=0, chunks=1, start=0, total=4, body=b'data'
    )

    assert response == {}


def test_request_repr_and_shape_are_redacted() -> None:
    request = ProtocolRequest(
        operation='fixture',
        method='POST',
        url='https://example.invalid/path?access_key=url-secret',
        headers={'Cookie': 'cookie-secret'},
        query=(('access_key', 'query-secret'),),
        form=(('csrf', 'form-secret'),),
        body=b'body-secret',
    )

    rendered = repr(request) + json.dumps(request.safe_shape(), sort_keys=True)

    for forbidden in (
        'url-secret',
        'cookie-secret',
        'query-secret',
        'form-secret',
        'body-secret',
    ):
        assert forbidden not in rendered


@pytest.mark.asyncio
async def test_response_and_preupload_repr_are_redacted() -> None:
    response = ProtocolResponse(
        status=200,
        headers={'Set-Cookie': 'response-cookie-secret'},
        body=b'response-body-secret',
    )
    fixtures = json.loads(FIXTURE_PATH.read_text())
    prepared = await protocol_client(ScriptedTransport(fixtures)).preupload(
        credential_fixture(), {'name': 'fixture.mp4', 'size': 4}
    )

    rendered = repr(response) + repr(prepared)

    for forbidden in (
        'response-cookie-secret',
        'response-body-secret',
        'fixture-upos-auth',
        'fixture-upload-id',
    ):
        assert forbidden not in rendered


@pytest.mark.asyncio
async def test_qr_pending_code_is_returned_to_the_account_state_machine() -> None:
    response = ProtocolResponse(
        status=200,
        headers={},
        body=json.dumps({'code': 86039, 'message': 'pending'}).encode('utf8'),
    )
    client = protocol_client(StaticResponseTransport(response))

    result = await client.poll_qr({'auth_code': 'fixture-auth-code'})

    assert result['code'] == 86039


@pytest.mark.asyncio
async def test_aiohttp_transport_reports_failure_before_headers_are_sent() -> None:
    server = await asyncio.start_server(lambda _reader, _writer: None, '127.0.0.1', 0)
    port = server.sockets[0].getsockname()[1]
    server.close()
    await server.wait_closed()
    transport = AiohttpProtocolTransport(timeout_seconds=1)
    request = ProtocolRequest(
        operation='fixture',
        method='GET',
        url='http://127.0.0.1:{}/fixture'.format(port),
        headers={},
    )

    try:
        with pytest.raises(TransportFailure) as error:
            await transport.send(request)
        assert error.value.headers_sent is False
    finally:
        await transport.close()


@pytest.mark.asyncio
async def test_aiohttp_transport_reports_disconnect_after_headers_are_sent() -> None:
    async def disconnect(reader: Any, writer: Any) -> None:
        await reader.readuntil(b'\r\n\r\n')
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(disconnect, '127.0.0.1', 0)
    port = server.sockets[0].getsockname()[1]
    transport = AiohttpProtocolTransport(timeout_seconds=1)
    request = ProtocolRequest(
        operation='fixture',
        method='POST',
        url='http://127.0.0.1:{}/fixture'.format(port),
        headers={},
        form=(('value', 'secret'),),
    )

    try:
        with pytest.raises(TransportFailure) as error:
            await transport.send(request)
        assert error.value.headers_sent is True
    finally:
        await transport.close()
        server.close()
        await server.wait_closed()
