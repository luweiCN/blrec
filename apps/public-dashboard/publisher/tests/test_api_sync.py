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


def match() -> Dict[str, Any]:
    return {'id': 10, 'resultFramePath': 'session/result.png'}


def source(matches: List[Dict[str, Any]]) -> Mapping[str, Any]:
    return {
        'schemaVersion': 1,
        'generatedAt': '2026-08-11T10:15:00Z',
        'sourceLastMatchId': max((value['id'] for value in matches), default=0),
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
    assert first.image_count == 1
    assert second.synced is False
    assert len(requests) == 1
    payload = requests[0][1]
    image = payload['images'][0]
    assert image['matchId'] == 10
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
    image_path = tmp_path / 'frames' / 'session' / 'result.png'
    image_path.parent.mkdir(parents=True)
    Image.new('RGB', (300, 200), '#102030').save(image_path)

    def fail(key: str, _content: bytes) -> Mapping[str, Any]:
        attempted_keys.append(key)
        raise OSError('temporary API failure')

    common = {
        'database_path': database,
        'state_directory': tmp_path / 'state',
        'result_frame_directory': tmp_path / 'frames',
        'public_data_base_url': 'https://vg.luwei.host/data',
        'image_store': store,
        'source_builder': lambda _connection: source([match()]),
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


def test_sync_archives_a_legacy_full_data_outbox_before_sending_assets(
    tmp_path: Path,
) -> None:
    database = tmp_path / 'source.sqlite3'
    sqlite3.connect(database).close()
    state_directory = tmp_path / 'state'
    outbox_directory = state_directory / 'api-outbox'
    outbox_directory.mkdir(parents=True)
    legacy_path = outbox_directory / 'dashboard-legacy.json'
    legacy_path.write_text(
        json.dumps(
            {
                'schemaVersion': 1,
                'batchId': 'dashboard-legacy',
                'batch': {
                    'schemaVersion': 1,
                    'players': [],
                    'matches': [],
                    'removedMatchIds': [],
                },
                'nextState': {'schemaVersion': 1, 'matches': {}},
            }
        ),
        encoding='utf-8',
    )
    image_path = tmp_path / 'frames' / 'session' / 'result.png'
    image_path.parent.mkdir(parents=True)
    Image.new('RGB', (300, 200), '#102030').save(image_path)
    requests: List[Mapping[str, Any]] = []

    def post_batch(_key: str, content: bytes) -> Mapping[str, Any]:
        requests.append(json.loads(content))
        return {'status': 'applied'}

    result = sync_dashboard_api_once(
        database_path=database,
        state_directory=state_directory,
        result_frame_directory=tmp_path / 'frames',
        public_data_base_url='https://vg.luwei.host/data',
        image_store=FakeImageStore(),
        post_batch=post_batch,
        source_builder=lambda _connection: source([match()]),
    )

    assert result.synced is True
    assert len(requests) == 1
    assert requests[0]['images'][0]['matchId'] == 10
    assert not legacy_path.exists()
    assert (state_directory / 'legacy-api-outbox' / legacy_path.name).is_file()


def test_sync_sends_removed_match_ids(tmp_path: Path) -> None:
    database = tmp_path / 'source.sqlite3'
    sqlite3.connect(database).close()
    current = source([match()])
    requests: List[Mapping[str, Any]] = []
    image_path = tmp_path / 'frames' / 'session' / 'result.png'
    image_path.parent.mkdir(parents=True)
    Image.new('RGB', (300, 200), '#102030').save(image_path)

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

    assert requests[1]['images'] == []
    assert requests[1]['removedMatchIds'] == [10]


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
