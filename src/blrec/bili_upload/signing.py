import hashlib
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, Mapping, Tuple
from urllib.parse import urlencode, urlsplit

from blrec.bili import wbi

from .crypto import CookieRecord, CredentialBundle

__all__ = (
    'PROTOCOL_MATRIX',
    'BiliTvSigner',
    'OperationSpec',
    'WbiSigner',
    'WebSessionBuilder',
)


@dataclass(frozen=True)
class OperationSpec:
    method: str
    base: str
    path: str
    auth_mode: str
    idempotent: bool


PROTOCOL_MATRIX = {
    'create_qr': OperationSpec(
        'POST', 'passport', '/x/passport-tv-login/qrcode/auth_code', 'bilitv_sign', True
    ),
    'poll_qr': OperationSpec(
        'POST', 'passport', '/x/passport-tv-login/qrcode/poll', 'bilitv_sign', True
    ),
    'oauth_info': OperationSpec(
        'GET', 'passport', '/x/passport-login/oauth2/info', 'bilitv_token_sign', True
    ),
    'refresh_token': OperationSpec(
        'POST',
        'passport',
        '/x/passport-login/oauth2/refresh_token',
        'bilitv_token_sign',
        False,
    ),
    'preupload': OperationSpec('GET', 'member', '/preupload', 'web_cookie', True),
    'upload_chunk': OperationSpec(
        'PUT', 'server_returned', '<server-returned>', 'upos_session', True
    ),
    'complete_upload': OperationSpec(
        'POST', 'server_returned', '<server-returned>', 'upos_session', False
    ),
    'submit_archive': OperationSpec(
        'POST', 'member_api', '/x/vu/app/add', 'bilitv_token_sign', False
    ),
    'archive_pre': OperationSpec(
        'GET', 'member_api', '/x/vupre/web/archive/pre', 'web_cookie', True
    ),
    'list_archives': OperationSpec(
        'GET', 'member_api', '/x/web/archives', 'web_cookie', True
    ),
    'archive_view': OperationSpec(
        'GET', 'member_api', '/x/vupre/web/archive/view', 'web_cookie', True
    ),
    'web_nav': OperationSpec('GET', 'api', '/x/web-interface/nav', 'web_cookie', True),
    'list_replies': OperationSpec(
        'GET', 'api', '/x/v2/reply/main', 'web_cookie_wbi', True
    ),
    'reply_detail': OperationSpec(
        'GET', 'api', '/x/v2/reply/detail', 'web_cookie_wbi', True
    ),
    'add_reply': OperationSpec(
        'POST', 'api', '/x/v2/reply/add', 'web_cookie_csrf', False
    ),
    'top_reply': OperationSpec(
        'POST', 'api', '/x/v2/reply/top', 'web_cookie_csrf', False
    ),
    'post_danmaku': OperationSpec(
        'POST', 'api', '/x/v2/dm/post', 'web_cookie_csrf_wbi', False
    ),
}


class BiliTvSigner:
    APP_KEY = '4409e2ce8ffd12b8'
    APP_SECRET = '59b43e04ad6965f34319062b478f83dd'

    def sign(self, params: Mapping[str, Any]) -> Dict[str, str]:
        values = {str(key): str(value) for key, value in params.items()}
        values['appkey'] = self.APP_KEY
        ordered = dict(sorted(values.items()))
        query = urlencode(tuple(ordered.items()))
        ordered['sign'] = hashlib.md5(
            (query + self.APP_SECRET).encode('utf8')
        ).hexdigest()
        return ordered


class WbiSigner:
    def __init__(
        self,
        key_provider: Callable[[], Awaitable[Tuple[str, str]]],
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._key_provider = key_provider
        self._clock = clock

    async def sign(self, params: Mapping[str, Any]) -> Dict[str, str]:
        img_key, sub_key = await self._key_provider()
        mixin_key = wbi.make_key(img_key, sub_key)
        query = wbi.build_query(
            mixin_key, int(self._clock()), [(str(k), v) for k, v in params.items()]
        )
        signed = {}
        for part in query.split('&'):
            name, value = part.split('=', 1)
            signed[name] = value
        return signed


class WebSessionBuilder:
    def __init__(self, *, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock

    def cookie_header(self, bundle: CredentialBundle, url: str) -> str:
        target = urlsplit(url)
        host = (target.hostname or '').lower()
        path = target.path or '/'
        now = int(self._clock())
        cookies = []
        for cookie in bundle.cookies:
            if not self._matches(cookie, host, path, target.scheme, now):
                continue
            cookies.append('{}={}'.format(cookie.name, cookie.value))
        return '; '.join(cookies)

    @staticmethod
    def csrf(bundle: CredentialBundle) -> str:
        return bundle.csrf

    @classmethod
    def _matches(
        cls, cookie: CookieRecord, host: str, request_path: str, scheme: str, now: int
    ) -> bool:
        domain = cookie.domain.lower().lstrip('.')
        if not domain or not (host == domain or host.endswith('.' + domain)):
            return False
        if cookie.secure and scheme != 'https':
            return False
        if cookie.expires_at is not None and cookie.expires_at <= now:
            return False
        cookie_path = cookie.path or '/'
        if not request_path.startswith(cookie_path):
            return False
        if (
            request_path != cookie_path
            and not cookie_path.endswith('/')
            and request_path[len(cookie_path) : len(cookie_path) + 1] != '/'
        ):
            return False
        return True
