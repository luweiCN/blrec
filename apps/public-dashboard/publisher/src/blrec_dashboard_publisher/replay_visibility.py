from __future__ import annotations

import re
from typing import Mapping

import requests

BVID_PATTERN = re.compile(r'^BV[0-9A-Za-z]{4,18}$')
_TRANSIENT_CODES = frozenset((-503, -509, -412, -352))
_HEADERS = {
    'Accept': 'application/json',
    'User-Agent': (
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
        'AppleWebKit/537.36 (KHTML, like Gecko) '
        'Chrome/136.0.0.0 Safari/537.36'
    ),
}


class ReplayVisibilityCheckError(RuntimeError):
    pass


class BilibiliReplayVisibilityChecker:
    def __init__(self, session: requests.Session) -> None:
        self._session = session

    def public_visible(self, bvid: str) -> bool:
        if BVID_PATTERN.fullmatch(bvid) is None:
            raise ReplayVisibilityCheckError('BVID 无效')
        try:
            response = self._session.get(
                'https://api.bilibili.com/x/web-interface/view',
                params={'bvid': bvid},
                headers=_HEADERS,
                timeout=(5, 15),
                stream=True,
            )
        except (requests.RequestException, OSError) as exc:
            raise ReplayVisibilityCheckError('B 站公开接口请求失败') from exc
        if response.status_code != 200:
            response.close()
            raise ReplayVisibilityCheckError(
                'B 站公开接口 HTTP {}'.format(response.status_code)
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise ReplayVisibilityCheckError('B 站公开接口返回了无效 JSON') from exc
        if not isinstance(payload, Mapping) or type(payload.get('code')) is not int:
            raise ReplayVisibilityCheckError('B 站公开接口响应结构无效')
        code = int(payload['code'])
        if code in _TRANSIENT_CODES:
            raise ReplayVisibilityCheckError('B 站公开接口暂时拒绝 {}'.format(code))
        if code != 0:
            return False
        data = payload.get('data')
        if not isinstance(data, Mapping):
            raise ReplayVisibilityCheckError('B 站公开稿件数据缺失')
        pages = data.get('pages')
        if (
            data.get('bvid') != bvid
            or type(data.get('aid')) is not int
            or int(data['aid']) <= 0
            or not isinstance(pages, list)
            or not pages
        ):
            raise ReplayVisibilityCheckError('B 站公开稿件数据不完整')
        return True
