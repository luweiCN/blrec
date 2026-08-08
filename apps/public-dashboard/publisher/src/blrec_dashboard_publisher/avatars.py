from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Tuple, cast
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen

import cv2
import numpy as np
from numpy.typing import NDArray

__all__ = ('AvatarSyncResult', 'sync_player_avatars')


ROOM_INFO_URL = 'https://api.live.bilibili.com/room/v1/Room/get_info'
ANCHOR_INFO_URL = 'https://api.live.bilibili.com/live_user/v1/Master/info'
REQUEST_HEADERS = {
    'Accept': 'application/json,image/*',
    'Referer': 'https://live.bilibili.com/',
    'User-Agent': 'Mozilla/5.0 BLREC dashboard publisher',
}
MAX_RESPONSE_BYTES = 5 * 1024 * 1024
MAX_AVATAR_DIMENSION = 256
ROOM_ID_PATTERN = re.compile(r'\d+')


@dataclass(frozen=True)
class AvatarSyncResult:
    attempted: int
    downloaded: int

    @property
    def failed(self) -> int:
        return self.attempted - self.downloaded


def _request_bytes(request: Request, opener: Callable[..., Any]) -> bytes:
    with opener(request, timeout=10) as response:
        content = response.read(MAX_RESPONSE_BYTES + 1)
    if len(content) > MAX_RESPONSE_BYTES:
        raise ValueError('Bilibili avatar response is too large')
    return content


def _request_json(
    url: str, parameters: Mapping[str, int], opener: Callable[..., Any]
) -> Mapping[str, Any]:
    request = Request(
        '{}?{}'.format(url, urlencode(parameters)), headers=REQUEST_HEADERS
    )
    value = json.loads(_request_bytes(request, opener).decode('utf-8'))
    if not isinstance(value, Mapping) or value.get('code') != 0:
        raise ValueError('Bilibili room metadata is unavailable')
    return value


def _avatar_url(room_id: int, opener: Callable[..., Any]) -> str:
    room_response = _request_json(ROOM_INFO_URL, {'room_id': room_id}, opener)
    room_data = room_response.get('data')
    if not isinstance(room_data, Mapping):
        raise ValueError('Bilibili room metadata is invalid')
    uid = room_data.get('uid')
    if not isinstance(uid, int) or uid <= 0:
        raise ValueError('Bilibili room owner is invalid')

    anchor_response = _request_json(ANCHOR_INFO_URL, {'uid': uid}, opener)
    anchor_data = anchor_response.get('data')
    info = anchor_data.get('info') if isinstance(anchor_data, Mapping) else None
    face = info.get('face') if isinstance(info, Mapping) else None
    if not isinstance(face, str) or not face:
        raise ValueError('Bilibili room avatar is missing')
    if face.startswith('//'):
        face = 'https:' + face
    elif face.startswith('http://'):
        face = 'https://' + face[len('http://') :]
    parsed = urlsplit(face)
    if parsed.scheme != 'https' or not (parsed.hostname or '').endswith('.hdslb.com'):
        raise ValueError('Bilibili room avatar URL is invalid')
    return face


def _normalize_avatar(content: bytes) -> bytes:
    decoded = cv2.imdecode(np.frombuffer(content, dtype=np.uint8), cv2.IMREAD_COLOR)
    if decoded is None:
        raise ValueError('Bilibili room avatar is not an image')
    image = cast(NDArray[np.uint8], decoded)
    if image.size == 0:
        raise ValueError('Bilibili room avatar is not an image')
    height, width = image.shape[:2]
    largest_dimension = max(height, width)
    if largest_dimension > MAX_AVATAR_DIMENSION:
        scale = MAX_AVATAR_DIMENSION / largest_dimension
        image = cv2.resize(
            image,
            (max(1, round(width * scale)), max(1, round(height * scale))),
            interpolation=cv2.INTER_AREA,
        )
    encoded, output = cv2.imencode(
        '.jpg', image, [cv2.IMWRITE_JPEG_QUALITY, 86, cv2.IMWRITE_JPEG_OPTIMIZE, 1]
    )
    if not encoded:
        raise ValueError('Bilibili room avatar could not be encoded')
    return output.tobytes()


def _atomic_replace(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix='.{}.'.format(path.name), suffix='.tmp', dir=str(path.parent)
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, 'wb') as destination:
            destination.write(content)
            destination.flush()
            os.fsync(destination.fileno())
        os.replace(str(temporary_path), str(path))
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def _player_rooms(snapshot: Mapping[str, Any]) -> Mapping[int, Tuple[int, ...]]:
    standings = snapshot.get('standings')
    all_time = standings.get('all-time') if isinstance(standings, Mapping) else None
    players = all_time.get('players') if isinstance(all_time, Mapping) else None
    if not isinstance(players, list):
        raise ValueError('dashboard snapshot has no all-time players')

    result: Dict[int, Tuple[int, ...]] = {}
    for player in players:
        if not isinstance(player, Mapping):
            continue
        player_id = player.get('id')
        room_label = player.get('roomLabel')
        if not isinstance(player_id, int) or not isinstance(room_label, str):
            continue
        room_ids: List[int] = []
        for value in ROOM_ID_PATTERN.findall(room_label):
            room_id = int(value)
            if room_id > 0 and room_id not in room_ids:
                room_ids.append(room_id)
        if room_ids:
            result[player_id] = tuple(room_ids)
    return result


def _download_player_avatar(
    room_ids: Tuple[int, ...], destination: Path, opener: Callable[..., Any]
) -> bool:
    for room_id in room_ids:
        try:
            avatar_url = _avatar_url(room_id, opener)
            avatar_request = Request(avatar_url, headers=REQUEST_HEADERS)
            content = _request_bytes(avatar_request, opener)
            _atomic_replace(destination, _normalize_avatar(content))
            return True
        except (OSError, UnicodeError, ValueError):
            continue
    return False


def sync_player_avatars(
    snapshot_path: Path, output_directory: Path, *, opener: Callable[..., Any] = urlopen
) -> AvatarSyncResult:
    snapshot_value = json.loads(snapshot_path.read_text(encoding='utf-8'))
    if not isinstance(snapshot_value, Mapping):
        raise ValueError('dashboard snapshot is invalid')
    rooms_by_player = _player_rooms(snapshot_value)
    avatar_directory = output_directory.expanduser().resolve() / 'avatars'
    downloaded = sum(
        _download_player_avatar(
            room_ids, avatar_directory / '{}.jpg'.format(player_id), opener
        )
        for player_id, room_ids in rooms_by_player.items()
    )
    return AvatarSyncResult(attempted=len(rooms_by_player), downloaded=downloaded)
