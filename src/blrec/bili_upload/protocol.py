import asyncio
import json
import posixpath
import secrets
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Mapping, Optional, Sequence, Tuple
from urllib.parse import urlsplit, urlunsplit

import aiohttp

from .crypto import CredentialBundle
from .errors import (
    BiliApiError,
    DefinitelyNotSent,
    ProtocolContractError,
    RemoteOutcomeUnknown,
)
from .signing import PROTOCOL_MATRIX, BiliTvSigner, WbiSigner, WebSessionBuilder

__all__ = (
    'AiohttpProtocolTransport',
    'BiliProtocolClient',
    'PreuploadResult',
    'ProtocolRequest',
    'ProtocolResponse',
    'TransportFailure',
    'UposSession',
)


Headers = Mapping[str, str]
Parameters = Tuple[Tuple[str, str], ...]


@dataclass(frozen=True, repr=False)
class ProtocolRequest:
    operation: str
    method: str
    url: str
    headers: Headers
    query: Parameters = ()
    form: Parameters = ()
    body: Optional[bytes] = None

    def safe_shape(self) -> Dict[str, Any]:
        target = urlsplit(self.url)
        return {
            'operation': self.operation,
            'method': self.method,
            'scheme': target.scheme,
            'host': target.hostname,
            'path': target.path,
            'header_names': sorted(self.headers),
            'query_names': sorted(name for name, _value in self.query),
            'form_names': sorted(name for name, _value in self.form),
            'body_size': 0 if self.body is None else len(self.body),
        }

    def __repr__(self) -> str:
        return '<ProtocolRequest operation={!r} method={!r}>'.format(
            self.operation, self.method
        )


@dataclass(frozen=True, repr=False)
class ProtocolResponse:
    status: int
    headers: Headers
    body: bytes

    def __repr__(self) -> str:
        return '<ProtocolResponse status={} body_size={}>'.format(
            self.status, len(self.body)
        )


class TransportFailure(RuntimeError):
    def __init__(self, *, headers_sent: bool) -> None:
        self.headers_sent = headers_sent
        super().__init__('protocol transport failed')

    def __repr__(self) -> str:
        return '<TransportFailure headers_sent={}>'.format(self.headers_sent)


class AiohttpProtocolTransport:
    def __init__(self, *, timeout_seconds: float = 30) -> None:
        self._timeout = aiohttp.ClientTimeout(total=timeout_seconds)
        self._session: Optional[aiohttp.ClientSession] = None

    async def send(self, request: ProtocolRequest) -> ProtocolResponse:
        session = await self._get_session()
        trace_context = {'headers_sent': False}
        kwargs: Dict[str, Any] = {
            'headers': dict(request.headers),
            'params': list(request.query),
            'allow_redirects': False,
            'timeout': self._timeout,
            'trace_request_ctx': trace_context,
        }
        if request.form:
            kwargs['data'] = list(request.form)
        elif request.body is not None:
            kwargs['data'] = request.body
        try:
            async with session.request(
                request.method, request.url, **kwargs
            ) as response:
                body = await response.read()
                return ProtocolResponse(
                    status=response.status, headers=dict(response.headers), body=body
                )
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError):
            raise TransportFailure(
                headers_sent=bool(trace_context['headers_sent'])
            ) from None

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None:
            trace_config = aiohttp.TraceConfig()

            async def headers_sent(
                _session: aiohttp.ClientSession, context: Any, _params: Any
            ) -> None:
                request_context = getattr(context, 'trace_request_ctx', None)
                if isinstance(request_context, dict):
                    request_context['headers_sent'] = True

            headers_signal: Any = trace_config.on_request_headers_sent
            headers_signal.append(headers_sent)
            self._session = aiohttp.ClientSession(trace_configs=[trace_config])
        return self._session


@dataclass(frozen=True, repr=False)
class UposSession:
    owner_token: str
    target_url: str
    auth: str
    upload_id: str
    biz_id: str
    file_name: str
    remote_file_name: str

    def __repr__(self) -> str:
        return '<UposSession redacted>'


@dataclass(frozen=True, repr=False)
class PreuploadResult:
    payload: Mapping[str, Any]
    session: UposSession

    def __repr__(self) -> str:
        return '<PreuploadResult redacted>'


class BiliProtocolClient:
    _BASE_URLS = {
        'passport': 'https://passport.bilibili.com',
        'member': 'https://member.bilibili.com',
        'member_api': 'https://member.bilibili.com',
        'api': 'https://api.bilibili.com',
    }
    _UPOS_SESSION_FIELDS = frozenset(
        (
            'format_version',
            'target_url',
            'auth',
            'upload_id',
            'biz_id',
            'file_name',
            'remote_file_name',
        )
    )

    def __init__(
        self,
        *,
        transport: Any,
        wbi_signer: WbiSigner,
        web_session_builder: WebSessionBuilder,
        tv_signer: Optional[BiliTvSigner] = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._transport = transport
        self._wbi_signer = wbi_signer
        self._web = web_session_builder
        self._tv = tv_signer or BiliTvSigner()
        self._clock = clock
        self._owner_token = secrets.token_hex(16)

    async def create_qr(self, params: Mapping[str, Any]) -> Mapping[str, Any]:
        values = {'local_id': '0', 'ts': int(self._clock()), **params}
        return await self._standard_request(
            'create_qr', headers=self._tv_headers(), form=self._tv.sign(values)
        )

    async def poll_qr(self, params: Mapping[str, Any]) -> Mapping[str, Any]:
        values = {'local_id': '0', 'ts': int(self._clock()), **params}
        return await self._standard_request(
            'poll_qr',
            headers=self._tv_headers(),
            form=self._tv.sign(values),
            check_code=False,
        )

    async def oauth_info(self, bundle: CredentialBundle) -> Mapping[str, Any]:
        values = self._tv_token_values(bundle)
        return await self._standard_request(
            'oauth_info', headers=self._tv_headers(), query=self._tv.sign(values)
        )

    async def refresh_token(self, bundle: CredentialBundle) -> Mapping[str, Any]:
        values = {
            **self._tv_token_values(bundle),
            'refresh_token': bundle.refresh_token,
        }
        return await self._standard_request(
            'refresh_token', headers=self._tv_headers(), form=self._tv.sign(values)
        )

    async def preupload(
        self, bundle: CredentialBundle, params: Mapping[str, Any]
    ) -> PreuploadResult:
        response = await self._standard_request(
            'preupload',
            headers=self._web_headers(bundle, self._url_for('preupload')),
            query=params,
        )
        target_url = self._upos_target(response)
        auth = self._required_text(response, 'auth', 'UPOS preupload')
        init_request = ProtocolRequest(
            operation='preupload_init',
            method='POST',
            url=target_url,
            headers={'X-Upos-Auth': auth},
            query=(('uploads', ''), ('output', 'json')),
        )
        initialized = await self._execute(init_request, idempotent=True)
        upload_id = self._required_text(initialized, 'upload_id', 'UPOS init')
        session = UposSession(
            owner_token=self._owner_token,
            target_url=target_url,
            auth=auth,
            upload_id=upload_id,
            biz_id=str(response.get('biz_id', '')),
            file_name=str(params.get('name', '')),
            remote_file_name=self._upos_remote_file_name(response),
        )
        return PreuploadResult(payload=response, session=session)

    def export_upos_session(self, session: UposSession) -> Mapping[str, Any]:
        self._validate_upos_session(session)
        return {
            'format_version': 1,
            'target_url': session.target_url,
            'auth': session.auth,
            'upload_id': session.upload_id,
            'biz_id': session.biz_id,
            'file_name': session.file_name,
            'remote_file_name': session.remote_file_name,
        }

    def restore_upos_session(self, payload: Mapping[str, Any]) -> UposSession:
        if not isinstance(payload, dict) or set(payload) != self._UPOS_SESSION_FIELDS:
            raise ProtocolContractError('invalid persisted UPOS session')
        if payload['format_version'] != 1:
            raise ProtocolContractError('invalid persisted UPOS session')
        values = {
            field: payload[field]
            for field in self._UPOS_SESSION_FIELDS
            if field != 'format_version'
        }
        if any(not isinstance(value, str) or not value for value in values.values()):
            raise ProtocolContractError('invalid persisted UPOS session')
        target_url = str(values['target_url'])
        self._validate_upos_target_url(target_url)
        return UposSession(
            owner_token=self._owner_token,
            target_url=target_url,
            auth=str(values['auth']),
            upload_id=str(values['upload_id']),
            biz_id=str(values['biz_id']),
            file_name=str(values['file_name']),
            remote_file_name=str(values['remote_file_name']),
        )

    async def upload_chunk(
        self,
        session: UposSession,
        *,
        chunk_no: int,
        chunks: int,
        start: int,
        total: int,
        body: bytes,
    ) -> Mapping[str, Any]:
        self._validate_upos_session(session)
        query = {
            'uploadId': session.upload_id,
            'chunks': chunks,
            'chunk': chunk_no,
            'size': len(body),
            'partNumber': chunk_no + 1,
            'start': start,
            'end': start + len(body),
            'total': total,
        }
        request = ProtocolRequest(
            operation='upload_chunk',
            method='PUT',
            url=session.target_url,
            headers={'X-Upos-Auth': session.auth},
            query=self._parameters(query),
            body=body,
        )
        return await self._execute(request, idempotent=True, allow_empty_success=True)

    async def complete_upload(
        self, session: UposSession, *, parts: Sequence[Mapping[str, Any]]
    ) -> Mapping[str, Any]:
        self._validate_upos_session(session)
        query = {
            'name': session.file_name,
            'uploadId': session.upload_id,
            'biz_id': session.biz_id,
            'output': 'json',
            'profile': 'ugcupos/bup',
        }
        body = json.dumps(
            {'parts': list(parts)}, ensure_ascii=False, separators=(',', ':')
        ).encode('utf8')
        request = ProtocolRequest(
            operation='complete_upload',
            method='POST',
            url=session.target_url,
            headers={'X-Upos-Auth': session.auth},
            query=self._parameters(query),
            body=body,
        )
        return await self._execute(request, idempotent=False)

    async def submit_archive(
        self, bundle: CredentialBundle, payload: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        query = self._tv.sign(
            {
                **self._tv_token_values(bundle),
                'build': '7800300',
                'c_locale': 'zh-Hans_CN',
                'channel': 'bili',
                'disable_rcmd': '0',
                'mobi_app': 'android',
                'platform': 'android',
                's_locale': 'zh-Hans_CN',
            }
        )
        body = json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode(
            'utf8'
        )
        return await self._standard_request(
            'submit_archive',
            query=query,
            headers={'Content-Type': 'application/json'},
            body=body,
        )

    async def list_archives(
        self, bundle: CredentialBundle, params: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        return await self._web_request('list_archives', bundle, query=params)

    async def archive_view(
        self, bundle: CredentialBundle, params: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        return await self._web_request('archive_view', bundle, query=params)

    async def web_nav(self, bundle: CredentialBundle) -> Mapping[str, Any]:
        return await self._web_request('web_nav', bundle)

    async def list_replies(
        self, bundle: CredentialBundle, params: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        return await self._web_request(
            'list_replies', bundle, query=await self._wbi_signer.sign(params)
        )

    async def reply_detail(
        self, bundle: CredentialBundle, params: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        return await self._web_request(
            'reply_detail', bundle, query=await self._wbi_signer.sign(params)
        )

    async def add_reply(
        self, bundle: CredentialBundle, params: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        return await self._csrf_request('add_reply', bundle, params)

    async def top_reply(
        self, bundle: CredentialBundle, params: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        return await self._csrf_request('top_reply', bundle, params)

    async def post_danmaku(
        self, bundle: CredentialBundle, params: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        form = {**params, 'csrf': self._web.csrf(bundle)}
        query = await self._wbi_signer.sign(form)
        return await self._web_request('post_danmaku', bundle, query=query, form=form)

    async def _csrf_request(
        self, operation: str, bundle: CredentialBundle, params: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        form = {**params, 'csrf': self._web.csrf(bundle)}
        return await self._web_request(operation, bundle, form=form)

    async def _web_request(
        self,
        operation: str,
        bundle: CredentialBundle,
        *,
        query: Optional[Mapping[str, Any]] = None,
        form: Optional[Mapping[str, Any]] = None,
    ) -> Mapping[str, Any]:
        return await self._standard_request(
            operation,
            headers=self._web_headers(bundle, self._url_for(operation)),
            query=query,
            form=form,
        )

    async def _standard_request(
        self,
        operation: str,
        *,
        headers: Optional[Headers] = None,
        query: Optional[Mapping[str, Any]] = None,
        form: Optional[Mapping[str, Any]] = None,
        body: Optional[bytes] = None,
        check_code: bool = True,
    ) -> Mapping[str, Any]:
        spec = PROTOCOL_MATRIX[operation]
        request = ProtocolRequest(
            operation=operation,
            method=spec.method,
            url=self._url_for(operation),
            headers={} if headers is None else headers,
            query=self._parameters(query),
            form=self._parameters(form),
            body=body,
        )
        return await self._execute(
            request, idempotent=spec.idempotent, check_code=check_code
        )

    async def _execute(
        self,
        request: ProtocolRequest,
        *,
        idempotent: bool,
        check_code: bool = True,
        allow_empty_success: bool = False,
    ) -> Mapping[str, Any]:
        try:
            response = await self._transport.send(request)
        except TransportFailure as error:
            if not error.headers_sent:
                raise DefinitelyNotSent(request.operation) from None
            raise RemoteOutcomeUnknown(request.operation) from None

        if 300 <= response.status < 400:
            raise ProtocolContractError('redirect response is not allowed')
        if response.status >= 500:
            raise RemoteOutcomeUnknown(request.operation)
        if response.status >= 400:
            raise BiliApiError(response.status, operation=request.operation)
        if allow_empty_success and not response.body.strip():
            return {}
        try:
            payload = json.loads(response.body.decode('utf8'))
        except (UnicodeError, json.JSONDecodeError):
            if idempotent:
                raise ProtocolContractError('invalid upstream response') from None
            raise RemoteOutcomeUnknown(request.operation) from None
        if not isinstance(payload, dict):
            if idempotent:
                raise ProtocolContractError('invalid upstream response')
            raise RemoteOutcomeUnknown(request.operation)
        code = payload.get('code')
        if check_code and type(code) is int and code != 0:
            raise BiliApiError(
                code, self._safe_message(payload), operation=request.operation
            )
        if 'OK' in payload and payload['OK'] != 1:
            raise ProtocolContractError('UPOS operation failed')
        return payload

    def _url_for(self, operation: str) -> str:
        spec = PROTOCOL_MATRIX[operation]
        base = self._BASE_URLS.get(spec.base)
        if base is None:
            raise ProtocolContractError('operation requires a server-provided URL')
        return base + spec.path

    def _web_headers(self, bundle: CredentialBundle, url: str) -> Headers:
        cookie = self._web.cookie_header(bundle, url)
        if not cookie:
            raise ProtocolContractError('web credential has no matching cookies')
        return {**self._tv_headers(), 'Cookie': cookie}

    @staticmethod
    def _tv_headers() -> Headers:
        return {
            'Referer': 'https://www.bilibili.com/',
            'User-Agent': (
                'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
                'Chrome/63.0.3239.108'
            ),
        }

    def _tv_token_values(self, bundle: CredentialBundle) -> Dict[str, Any]:
        if bundle.signing_family.lower() not in ('tv', 'bilitv'):
            raise ProtocolContractError('credential does not use the BiliTV signer')
        return {
            'access_key': bundle.access_token,
            'actionKey': 'appkey',
            'ts': int(self._clock()),
        }

    def _validate_upos_session(self, session: UposSession) -> None:
        if session.owner_token != self._owner_token:
            raise ProtocolContractError('UPOS session belongs to another client')

    @classmethod
    def _upos_target(cls, response: Mapping[str, Any]) -> str:
        endpoint = cls._required_text(response, 'endpoint', 'UPOS preupload')
        if endpoint.startswith('//'):
            endpoint = 'https:' + endpoint
        parsed = urlsplit(endpoint)
        if (
            parsed.scheme != 'https'
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ProtocolContractError('invalid UPOS target')
        upos_uri = cls._required_text(response, 'upos_uri', 'UPOS preupload')
        if not upos_uri.startswith('upos://'):
            raise ProtocolContractError('invalid UPOS target')
        object_path = '/' + upos_uri[len('upos://') :].lstrip('/')
        base_path = parsed.path.rstrip('/')
        target_url = urlunsplit(
            (parsed.scheme, parsed.netloc, base_path + object_path, '', '')
        )
        cls._validate_upos_target_url(target_url)
        return target_url

    @staticmethod
    def _validate_upos_target_url(target_url: str) -> None:
        parsed = urlsplit(target_url)
        if (
            parsed.scheme != 'https'
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or not parsed.path
            or parsed.query
            or parsed.fragment
        ):
            raise ProtocolContractError('invalid UPOS target')

    @classmethod
    def _upos_remote_file_name(cls, response: Mapping[str, Any]) -> str:
        upos_uri = cls._required_text(response, 'upos_uri', 'UPOS preupload')
        name = posixpath.basename(urlsplit(upos_uri).path)
        remote_file_name = posixpath.splitext(name)[0]
        if not remote_file_name:
            raise ProtocolContractError('UPOS preupload response is incomplete')
        return remote_file_name

    @staticmethod
    def _required_text(payload: Mapping[str, Any], field: str, context: str) -> str:
        value = payload.get(field)
        if not isinstance(value, str) or not value:
            raise ProtocolContractError('{} response is incomplete'.format(context))
        return value

    @staticmethod
    def _parameters(values: Optional[Mapping[str, Any]]) -> Parameters:
        if values is None:
            return ()
        return tuple((str(key), str(value)) for key, value in values.items())

    @staticmethod
    def _safe_message(payload: Mapping[str, Any]) -> Optional[str]:
        message = payload.get('message') or payload.get('msg')
        if isinstance(message, str) and message.isascii() and message.isalpha():
            return message
        return None
