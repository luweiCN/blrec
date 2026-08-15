from typing import Any, Dict, List

import pytest

from blrec.vainglory.glm_ocr import (
    GlmOcrClient,
    GlmOcrError,
    GlmOcrResponse,
    GlmOcrResultReader,
    parse_glm_result,
)
from blrec.vainglory.ocr import ResultHeader
from blrec.vainglory.vision import RgbFrame

REAL_RESULT_TEXT = """\
1 34.7k 12 胜利 21 41.1k 5
14:21
分享
8888_Weak 5/9/2 12.8k 77 8888-2_33no1 6/2/14 16.4k 119
8888_catt 6/5/3 12.6k 54 8888-2_
8888_GaKu 1/7/6 9.2k 11 8888-2_邱波 4/9/6 9.4k 26
回放
评价
出装
完成
"""


def test_parse_glm_result_maps_paired_rows_and_keeps_partial_data() -> None:
    result = parse_glm_result(REAL_RESULT_TEXT)
    players = {(item.side, item.slot): item for item in result.players}

    assert result.header.duration_seconds == 14 * 60 + 21
    assert result.header.left_kills == 12
    assert result.header.right_kills == 21
    assert players[('left', 1)].name == '8888_Weak'
    assert players[('left', 1)].stats.last_hits is None
    assert players[('right', 1)].name == '8888-2_33no1'
    assert players[('right', 1)].stats.kills == 6
    assert players[('right', 1)].stats.last_hits is None
    assert players[('right', 2)].name == '8888-2_'
    assert players[('right', 2)].stats.kills is None
    assert players[('right', 3)].name == '8888-2_邱波'
    assert players[('right', 3)].stats.last_hits is None


class _Response:
    def raise_for_status(self) -> None:
        return None

    def json(self) -> Dict[str, Any]:
        return {
            'ok': True,
            'data': {'text': REAL_RESULT_TEXT},
            'meta': {'elapsed_ms': 1234},
        }


class _Session:
    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []

    def post(self, url: str, **kwargs: Any) -> _Response:
        self.calls.append({'url': url, **kwargs})
        return _Response()


def test_glm_client_uses_bounded_http_request() -> None:
    session = _Session()
    client = GlmOcrClient(
        'http://ocr.internal:18080',
        session=session,
        connect_timeout_seconds=3,
        read_timeout_seconds=90,
    )

    response = client.recognize(RgbFrame(1, 1, b'\x00\x00\x00'))

    assert response.elapsed_ms == 1234
    assert session.calls[0]['url'] == 'http://ocr.internal:18080/v1/ocr'
    assert session.calls[0]['params'] == {'profile': 'standard'}
    assert session.calls[0]['timeout'] == (3, 90)
    assert session.calls[0]['headers'] == {'Content-Type': 'image/png'}


def test_glm_client_default_timeout_allows_cpu_inference() -> None:
    session = _Session()
    client = GlmOcrClient('http://ocr.internal:18080', session=session)

    client.recognize(RgbFrame(1, 1, b'\x00\x00\x00'))

    assert session.calls[0]['timeout'] == (5, 180)


class _Client:
    def __init__(self, texts: List[str]) -> None:
        self._texts = iter(texts)
        self.calls = 0

    def recognize(self, _frame: RgbFrame) -> GlmOcrResponse:
        self.calls += 1
        return GlmOcrResponse(next(self._texts), None)


class _Fallback:
    def read_header(self, _frame: RgbFrame, **_kwargs: Any) -> ResultHeader:
        return ResultHeader('胜利', 'normal', 861, 12, 21, 34_700, 41_100)


def test_glm_reader_uses_second_frame_only_to_fill_missing_rows() -> None:
    first = REAL_RESULT_TEXT
    second = REAL_RESULT_TEXT.replace(
        '8888_catt 6/5/3 12.6k 54 8888-2_',
        '8888_catt 6/5/3 12.6k 54 8888-2_mid 11/1/8 15.1k 56',
    )
    client = _Client([first, second])
    reader = GlmOcrResultReader(
        client, fallback=_Fallback(), maximum_remote_frames=2  # type: ignore[arg-type]
    )
    frame = RgbFrame(1, 1, b'\x00\x00\x00')

    result = reader.read(frame, name_frames=(frame,))
    players = {(item.side, item.slot): item for item in result.players}

    assert client.calls == 2
    assert players[('right', 2)].name == '8888-2_mid'
    assert players[('right', 2)].stats.kills == 11


def test_glm_reader_retries_complete_but_inconsistent_kda() -> None:
    invalid = """\
5 52.9k 16 战败 10 53.6k 5
21:17
left1 7/4/4 20.2k right1 5/6/3 22.5k
left2 9/5/4 18.3k right2 2/4/6 17.4k
left3 0/1/11 14.2k right3 8/6/55 13.6k
    """
    corrected = invalid.replace('right3 8/6/55', 'right3 3/6/5')
    client = _Client([invalid, corrected])

    class Fallback:
        def read_header(self, _frame: RgbFrame, **_kwargs: Any) -> ResultHeader:
            return ResultHeader('战败', 'normal', 1277, 16, 10, 52_900, 53_600)

    reader = GlmOcrResultReader(
        client, fallback=Fallback(), maximum_remote_frames=2  # type: ignore[arg-type]
    )
    frame = RgbFrame(1, 1, b'\x00\x00\x00')

    result = reader.read(frame, name_frames=(frame,))
    players = {(item.side, item.slot): item for item in result.players}

    assert client.calls == 2
    assert players[('right', 3)].stats.kills == 3
    assert players[('right', 3)].stats.assists == 5


def test_glm_reader_prefers_team_totals_supported_by_player_rows() -> None:
    text = """\
5 52.9k 16 战败 10 53.6k 5
21:17
left1 7/4/4 20.2k right1 5/6/3 22.5k
left2 9/5/4 18.3k right2 2/4/6 17.4k
left3 0/1/11 14.2k right3 3/6/5 13.6k
    """
    client = _Client([text])

    class Fallback:
        def read_header(self, _frame: RgbFrame, **_kwargs: Any) -> ResultHeader:
            return ResultHeader('战败', 'normal', 1277, 16, 1, 52_900, 53_600)

    reader = GlmOcrResultReader(
        client, fallback=Fallback(), maximum_remote_frames=1  # type: ignore[arg-type]
    )
    result = reader.read(RgbFrame(1, 1, b'\x00\x00\x00'))
    players = {(item.side, item.slot): item for item in result.players}

    assert result.header.right_kills == 10
    assert players[('left', 1)].stats.deaths == 4
    assert players[('right', 1)].stats.kills == 5
    assert players[('right', 3)].stats.kills == 3


def test_glm_reader_keeps_local_totals_when_remote_header_is_inconsistent() -> None:
    text = """\
5 52.9k 16 战败 1 53.6k 5
21:17
left1 7/4/4 20.2k right1 5/6/3 22.5k
left2 9/5/4 18.3k right2 2/4/6 17.4k
left3 0/1/11 14.2k right3 3/6/5 13.6k
    """
    client = _Client([text])

    class Fallback:
        def read_header(self, _frame: RgbFrame, **_kwargs: Any) -> ResultHeader:
            return ResultHeader('战败', 'normal', 1277, 16, 10, 52_900, 53_600)

    reader = GlmOcrResultReader(
        client, fallback=Fallback(), maximum_remote_frames=1  # type: ignore[arg-type]
    )
    result = reader.read(RgbFrame(1, 1, b'\x00\x00\x00'))

    assert result.header.right_kills == 10
    assert tuple(player.stats.kills for player in result.players[3:]) == (5, 2, 3)


class _IncompleteResponse(_Response):
    def json(self) -> Dict[str, Any]:
        payload = super().json()
        payload['data']['text'] = '\x1e' * 31
        payload['meta']['upstream_done'] = False
        return payload


class _IncompleteSession(_Session):
    def post(self, url: str, **kwargs: Any) -> _IncompleteResponse:
        self.calls.append({'url': url, **kwargs})
        return _IncompleteResponse()


def test_glm_client_rejects_an_incomplete_upstream_stream() -> None:
    client = GlmOcrClient('http://ocr.internal:18080', session=_IncompleteSession())

    with pytest.raises(GlmOcrError, match='未完整结束'):
        client.recognize(RgbFrame(1, 1, b'\x00\x00\x00'))


class _ControlOnlyResponse(_IncompleteResponse):
    def json(self) -> Dict[str, Any]:
        payload = super().json()
        payload['meta']['upstream_done'] = True
        return payload


class _ControlOnlySession(_Session):
    def post(self, url: str, **kwargs: Any) -> _ControlOnlyResponse:
        self.calls.append({'url': url, **kwargs})
        return _ControlOnlyResponse()


def test_glm_client_rejects_control_characters_without_text() -> None:
    client = GlmOcrClient('http://ocr.internal:18080', session=_ControlOnlySession())

    with pytest.raises(GlmOcrError, match='没有返回可用文字'):
        client.recognize(RgbFrame(1, 1, b'\x00\x00\x00'))
