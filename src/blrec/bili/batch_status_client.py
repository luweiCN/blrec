import json
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Mapping, Optional, Sequence, cast

import aiohttp

from .live_status import BatchStatusResult, ObservedStatus, StatusSnapshot, StatusSource

__all__ = ('BatchApiError', 'BatchProtocolError', 'BatchStatusClient')


class BatchProtocolError(RuntimeError):
    pass


class BatchApiError(BatchProtocolError):
    def __init__(self, code: int) -> None:
        super().__init__('Bilibili API error {}'.format(code))
        self.code = code


def _validate_anonymous_session(session: aiohttp.ClientSession) -> None:
    if not isinstance(session.cookie_jar, aiohttp.DummyCookieJar):
        raise ValueError('anonymous session must use aiohttp.DummyCookieJar')

    sensitive_headers = {'cookie', 'authorization', 'x-api-key'}
    if sensitive_headers & {name.lower() for name in session.headers}:
        raise ValueError('anonymous session must not define credential headers')


def _raise_for_http_status(status: int) -> None:
    if status != 200:
        raise BatchProtocolError('HTTP {}'.format(status))


def _decode_response_data(body: str) -> Mapping[str, Any]:
    try:
        payload = json.loads(body)
    except (TypeError, ValueError) as exc:
        raise BatchProtocolError('response is not JSON') from exc

    if not isinstance(payload, Mapping):
        raise BatchProtocolError('unexpected response envelope')

    code = payload.get('code')
    if isinstance(code, int) and not isinstance(code, bool) and code != 0:
        raise BatchApiError(code)
    if code != 0 or isinstance(code, bool):
        raise BatchProtocolError('unexpected response envelope')

    data = payload.get('data')
    if not isinstance(data, Mapping):
        raise BatchProtocolError('unexpected response envelope')
    return cast(Mapping[str, Any], data)


def _parse_live_time(value: object) -> int:
    if value in (None, '', 0, '0', '0000-00-00 00:00:00'):
        return 0
    if isinstance(value, bool):
        raise ValueError('invalid live_time')
    if isinstance(value, int):
        if value < 0:
            raise ValueError('invalid live_time')
        return value
    if not isinstance(value, str):
        raise ValueError('invalid live_time')

    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError('invalid live_time') from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone(timedelta(hours=8)))
    return int(parsed.timestamp())


def _parse_status_snapshot(
    item: Mapping[str, Any], observed_at: float, source: StatusSource
) -> Optional[StatusSnapshot]:
    try:
        uid = int(item['uid'])
        room_id = int(item['room_id'])
        raw_status = int(item['live_status'])
        live_time = _parse_live_time(item.get('live_time'))
    except (KeyError, TypeError, ValueError):
        return None

    status_by_code = {
        0: ObservedStatus.PREPARING,
        1: ObservedStatus.LIVE,
        2: ObservedStatus.ROUND,
    }
    status = status_by_code.get(raw_status)
    if status is None:
        return None

    observation_key = '{}:{}'.format(uid, live_time) if live_time else None
    return StatusSnapshot(
        uid=uid,
        room_id=room_id,
        status=status,
        observed_at=observed_at,
        source=source,
        live_time=live_time,
        observation_key=observation_key,
    )


class BatchStatusClient:
    PATH = '/room/v1/Room/get_status_info_by_uids'

    def __init__(
        self,
        session: aiohttp.ClientSession,
        base_url: str = 'https://api.live.bilibili.com',
        user_agent: str = 'BLREC batch live monitor',
    ) -> None:
        _validate_anonymous_session(session)
        self._session = session
        self._url = base_url.rstrip('/') + self.PATH
        self._headers = {'Accept': 'application/json', 'User-Agent': user_agent}

    async def fetch(
        self, uids: Sequence[int], *, observed_at: float
    ) -> BatchStatusResult:
        unique_uids = tuple(dict.fromkeys(uids))
        params = [('uids[]', str(uid)) for uid in unique_uids]
        async with self._session.get(
            self._url, params=params, headers=self._headers, allow_redirects=False
        ) as response:
            body = await response.text()
            _raise_for_http_status(response.status)
        data = _decode_response_data(body)

        snapshots: Dict[int, StatusSnapshot] = {}
        for item in data.values():
            if not isinstance(item, Mapping):
                continue
            snapshot = _parse_status_snapshot(
                cast(Mapping[str, Any], item), observed_at, StatusSource.BATCH
            )
            if snapshot is not None and snapshot.uid in unique_uids:
                snapshots[snapshot.uid] = snapshot

        return BatchStatusResult(
            snapshots=snapshots,
            missing_uids=frozenset(set(unique_uids) - set(snapshots)),
        )
