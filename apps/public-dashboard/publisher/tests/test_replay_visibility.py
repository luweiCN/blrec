from __future__ import annotations

from typing import Any, Mapping

import pytest
from blrec_dashboard_publisher.replay_visibility import (
    BilibiliReplayVisibilityChecker,
    ReplayVisibilityCheckError,
)


class _Response:
    def __init__(
        self, status_code: int, payload: Mapping[str, Any] | ValueError
    ) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> Mapping[str, Any]:
        if isinstance(self._payload, ValueError):
            raise self._payload
        return self._payload

    def close(self) -> None:
        pass


class _Session:
    def __init__(self, response: _Response) -> None:
        self.response = response
        self.requests: list[tuple[str, Mapping[str, str]]] = []

    def get(
        self,
        url: str,
        *,
        params: Mapping[str, str],
        headers: Mapping[str, str],
        timeout: tuple[int, int],
        stream: bool,
    ) -> _Response:
        assert timeout == (5, 15)
        assert stream is True
        assert headers['Accept'] == 'application/json'
        self.requests.append((url, params))
        return self.response


def test_bilibili_visibility_checker_accepts_a_public_archive() -> None:
    session = _Session(
        _Response(
            200,
            {'code': 0, 'data': {'bvid': 'BV1test00001', 'aid': 123, 'pages': [{}]}},
        )
    )
    checker = BilibiliReplayVisibilityChecker(session)  # type: ignore[arg-type]

    assert checker.public_visible('BV1test00001') is True
    assert session.requests == [
        ('https://api.bilibili.com/x/web-interface/view', {'bvid': 'BV1test00001'})
    ]


def test_bilibili_visibility_checker_treats_a_missing_archive_as_private() -> None:
    checker = BilibiliReplayVisibilityChecker(
        _Session(_Response(200, {'code': -404, 'message': '什么都没有'}))  # type: ignore[arg-type]
    )

    assert checker.public_visible('BV1test00001') is False


@pytest.mark.parametrize(
    'response',
    [
        _Response(429, {'code': -412}),
        _Response(503, {'code': -503}),
        _Response(200, {'code': -352, 'message': 'risk control'}),
        _Response(200, ValueError('invalid json')),
    ],
)
def test_bilibili_visibility_checker_retries_transient_or_invalid_results(
    response: _Response,
) -> None:
    checker = BilibiliReplayVisibilityChecker(_Session(response))  # type: ignore[arg-type]

    with pytest.raises(ReplayVisibilityCheckError):
        checker.public_visible('BV1test00001')
