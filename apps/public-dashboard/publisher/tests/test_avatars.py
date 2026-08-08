import json
from pathlib import Path
from typing import Dict
from urllib.request import Request

import cv2
import numpy as np
from blrec_dashboard_publisher.avatars import sync_player_avatars


class FakeResponse:
    def __init__(self, content: bytes) -> None:
        self._content = content

    def __enter__(self) -> 'FakeResponse':
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self, _limit: int) -> bytes:
        return self._content


def test_syncs_bound_room_avatar_and_skips_unbound_player(tmp_path: Path) -> None:
    snapshot = {
        'standings': {
            'all-time': {
                'players': [
                    {'id': 10, 'roomLabel': '直播间 100 / 101'},
                    {'id': 20, 'roomLabel': '历史录播'},
                ]
            }
        }
    }
    snapshot_path = tmp_path / 'snapshot.json'
    snapshot_path.write_text(json.dumps(snapshot), encoding='utf-8')
    calls: Dict[str, int] = {}
    encoded, avatar = cv2.imencode('.jpg', np.zeros((300, 400, 3), dtype=np.uint8))
    assert encoded

    def open_url(request: Request, timeout: int) -> FakeResponse:
        assert timeout == 10
        url = request.full_url
        calls[url] = calls.get(url, 0) + 1
        if 'Room/get_info' in url:
            return FakeResponse(b'{"code":0,"data":{"uid":500}}')
        if 'Master/info' in url:
            return FakeResponse(
                b'{"code":0,"data":{"info":{"face":"https://i0.hdslb.com/avatar.jpg"}}}'
            )
        return FakeResponse(avatar.tobytes())

    result = sync_player_avatars(snapshot_path, tmp_path / 'output', opener=open_url)

    assert result.attempted == 1
    assert result.downloaded == 1
    assert result.failed == 0
    saved_avatar = cv2.imread(str(tmp_path / 'output' / 'avatars' / '10.jpg'))
    assert saved_avatar.shape[:2] == (192, 256)
    assert not (tmp_path / 'output' / 'avatars' / '20.jpg').exists()
    assert any('room_id=100' in url for url in calls)


def test_avatar_failure_does_not_abort_other_players(tmp_path: Path) -> None:
    snapshot = {
        'standings': {
            'all-time': {
                'players': [
                    {'id': 10, 'roomLabel': '直播间 100'},
                    {'id': 20, 'roomLabel': '直播间 200'},
                ]
            }
        }
    }
    snapshot_path = tmp_path / 'snapshot.json'
    snapshot_path.write_text(json.dumps(snapshot), encoding='utf-8')
    encoded, avatar = cv2.imencode('.jpg', np.zeros((32, 32, 3), dtype=np.uint8))
    assert encoded

    def open_url(request: Request, timeout: int) -> FakeResponse:
        assert timeout == 10
        url = request.full_url
        if 'room_id=100' in url:
            raise OSError('room unavailable')
        if 'Room/get_info' in url:
            return FakeResponse(b'{"code":0,"data":{"uid":500}}')
        if 'Master/info' in url:
            return FakeResponse(
                b'{"code":0,"data":{"info":{"face":"https://i0.hdslb.com/avatar.jpg"}}}'
            )
        return FakeResponse(avatar.tobytes())

    result = sync_player_avatars(snapshot_path, tmp_path / 'output', opener=open_url)

    assert result.attempted == 2
    assert result.downloaded == 1
    assert result.failed == 1
    assert not (tmp_path / 'output' / 'avatars' / '10.jpg').exists()
    assert (tmp_path / 'output' / 'avatars' / '20.jpg').exists()
