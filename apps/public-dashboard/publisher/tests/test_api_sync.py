from __future__ import annotations

import io
import json
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Mapping

import pytest
from blrec_dashboard_publisher.api_sync import (
    DashboardApiSyncError,
    sync_dashboard_api_once,
)
from PIL import Image


class FakeImageStore:
    def __init__(self) -> None:
        self.images: Dict[str, bytes] = {}

    def put_match_image(self, path: str, content: bytes, sha256: str) -> int:
        self.images[path] = content
        return len(content)


def player() -> Dict[str, Any]:
    return {
        'id': 7,
        'name': '茉莉',
        'initial': '茉',
        'roomLabel': '直播间 123',
        'roomIds': [123],
        'aliases': ['-Akitsuki-'],
        'avatarUrl': None,
    }


def match(title: str = '晚间排位') -> Dict[str, Any]:
    participants = [
        {
            'slot': index,
            'name': f'玩家{index}',
            'heroName': hero,
            'kills': 1,
            'deaths': 1,
            'assists': 1,
            'economy': 10000,
            'lastHits': 100,
            'isRecordedPlayer': index == 1,
        }
        for index, hero in enumerate(('剑圣', '鱼人', '鸟人'), start=1)
    ]
    enemies = [
        {**value, 'isRecordedPlayer': False, 'name': f'对手{value["slot"]}'}
        for value in participants
    ]
    return {
        'id': 10,
        'playerId': 7,
        'seasonKey': '2026-summer',
        'mode': '3v3',
        'playedAt': '2026-08-11T10:00:00Z',
        'durationSeconds': 900,
        'result': 'W',
        'streamTitle': title,
        'ally': {
            'role': 'ally',
            'side': 'left',
            'color': 'teal',
            'kills': 10,
            'economy': 40000,
            'players': participants,
        },
        'enemy': {
            'role': 'enemy',
            'side': 'right',
            'color': 'orange',
            'kills': 8,
            'economy': 35000,
            'players': enemies,
        },
        'resultFramePath': 'session/result.png',
    }


def source(matches: List[Dict[str, Any]]) -> Mapping[str, Any]:
    return {
        'schemaVersion': 1,
        'generatedAt': '2026-08-11T10:15:00Z',
        'sourceLastMatchId': max((value['id'] for value in matches), default=0),
        'players': [player()],
        'matches': matches,
    }


def test_sync_compresses_result_image_and_skips_unchanged_data(tmp_path: Path) -> None:
    database = tmp_path / 'source.sqlite3'
    sqlite3.connect(database).close()
    frames = tmp_path / 'frames'
    image_path = frames / 'session' / 'result.png'
    image_path.parent.mkdir(parents=True)
    Image.new('RGB', (2400, 1200), '#102030').save(image_path)
    store = FakeImageStore()
    requests: List[tuple[str, Mapping[str, Any]]] = []

    def post_batch(key: str, content: bytes) -> Mapping[str, Any]:
        requests.append((key, json.loads(content)))
        return {'status': 'applied'}

    arguments = {
        'database_path': database,
        'state_directory': tmp_path / 'state',
        'result_frame_directory': frames,
        'public_data_base_url': 'https://vg.luwei.host/data',
        'image_store': store,
        'post_batch': post_batch,
        'source_builder': lambda _connection: source([match()]),
    }
    first = sync_dashboard_api_once(**arguments)
    second = sync_dashboard_api_once(**arguments)

    assert first.synced is True
    assert first.match_count == 1
    assert second.synced is False
    assert len(requests) == 1
    payload = requests[0][1]
    assert payload['matches'][0]['streamTitle'] == '晚间排位'
    assert 'resultFramePath' not in payload['matches'][0]
    image = payload['matches'][0]['resultImage']
    assert image['width'] == 1600
    assert image['height'] == 800
    assert image['url'].startswith('https://vg.luwei.host/data/match-images/000/10-')
    assert len(store.images) == 1
    with Image.open(io.BytesIO(next(iter(store.images.values())))) as compressed:
        assert compressed.format == 'WEBP'
        assert compressed.size == (1600, 800)


def test_failed_request_keeps_outbox_and_reuses_the_same_idempotency_key(
    tmp_path: Path,
) -> None:
    database = tmp_path / 'source.sqlite3'
    sqlite3.connect(database).close()
    store = FakeImageStore()
    attempted_keys: List[str] = []

    def fail(key: str, _content: bytes) -> Mapping[str, Any]:
        attempted_keys.append(key)
        raise OSError('temporary API failure')

    common = {
        'database_path': database,
        'state_directory': tmp_path / 'state',
        'result_frame_directory': tmp_path / 'frames',
        'public_data_base_url': 'https://vg.luwei.host/data',
        'image_store': store,
        'source_builder': lambda _connection: source(
            [{**match(), 'resultFramePath': None}]
        ),
    }
    with pytest.raises(OSError, match='temporary API failure'):
        sync_dashboard_api_once(**common, post_batch=fail)

    outbox = list((tmp_path / 'state' / 'api-outbox').glob('*.json'))
    assert len(outbox) == 1

    def succeed(key: str, _content: bytes) -> Mapping[str, Any]:
        attempted_keys.append(key)
        return {'status': 'duplicate'}

    result = sync_dashboard_api_once(**common, post_batch=succeed)

    assert result.synced is True
    assert attempted_keys[0] == attempted_keys[1]
    assert not list((tmp_path / 'state' / 'api-outbox').glob('*.json'))
    assert (tmp_path / 'state' / 'api-sync-state.json').is_file()


def test_sync_sends_removed_match_ids(tmp_path: Path) -> None:
    database = tmp_path / 'source.sqlite3'
    sqlite3.connect(database).close()
    current = source([{**match(), 'resultFramePath': None}])
    requests: List[Mapping[str, Any]] = []

    def post_batch(_key: str, content: bytes) -> Mapping[str, Any]:
        requests.append(json.loads(content))
        return {'status': 'applied'}

    common = {
        'database_path': database,
        'state_directory': tmp_path / 'state',
        'result_frame_directory': tmp_path / 'frames',
        'public_data_base_url': 'https://vg.luwei.host/data',
        'image_store': FakeImageStore(),
        'post_batch': post_batch,
    }
    sync_dashboard_api_once(**common, source_builder=lambda _connection: current)
    sync_dashboard_api_once(**common, source_builder=lambda _connection: source([]))

    assert requests[1]['matches'] == []
    assert requests[1]['removedMatchIds'] == [10]


def test_sync_publishes_live_status_changes_without_a_new_match(tmp_path: Path) -> None:
    database = tmp_path / 'source.sqlite3'
    sqlite3.connect(database).close()
    current = source([])
    current['players'][0]['liveRooms'] = [
        {'roomId': 123, 'title': '正在直播', 'startedAt': '2026-08-11T10:00:00Z'}
    ]
    requests: List[Mapping[str, Any]] = []

    def post_batch(_key: str, content: bytes) -> Mapping[str, Any]:
        requests.append(json.loads(content))
        return {'status': 'applied'}

    arguments = {
        'database_path': database,
        'state_directory': tmp_path / 'state',
        'result_frame_directory': tmp_path / 'frames',
        'public_data_base_url': 'https://vg.luwei.host/data',
        'image_store': FakeImageStore(),
        'post_batch': post_batch,
    }
    sync_dashboard_api_once(**arguments, source_builder=lambda _connection: current)
    current['players'][0]['liveRooms'] = []
    sync_dashboard_api_once(**arguments, source_builder=lambda _connection: current)

    assert requests[0]['players'][0]['liveRooms'][0]['roomId'] == 123
    assert requests[1]['players'][0]['liveRooms'] == []
    assert requests[1]['matches'] == []


def test_sync_rejects_result_frames_outside_the_mounted_directory(
    tmp_path: Path,
) -> None:
    database = tmp_path / 'source.sqlite3'
    sqlite3.connect(database).close()
    value = match()
    value['resultFramePath'] = '../secret.png'

    with pytest.raises(DashboardApiSyncError, match='结算图路径'):
        sync_dashboard_api_once(
            database_path=database,
            state_directory=tmp_path / 'state',
            result_frame_directory=tmp_path / 'frames',
            public_data_base_url='https://vg.luwei.host/data',
            image_store=FakeImageStore(),
            post_batch=lambda _key, _content: {'status': 'applied'},
            source_builder=lambda _connection: source([value]),
        )
