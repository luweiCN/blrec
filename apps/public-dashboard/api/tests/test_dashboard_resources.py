from __future__ import annotations

import gzip
import hashlib
import json
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Dict

from blrec_dashboard_api.app import create_app
from blrec_dashboard_api.dashboard_resources import (
    DashboardResourceCache,
    DashboardResourceDocument,
)
from blrec_dashboard_api.settings import ApiSettings
from fastapi.testclient import TestClient


def _player(player_id: int) -> Dict[str, Any]:
    hero_pool = [{'name': 'Hero-{}'.format(player_id % 12), 'matches': 20, 'wins': 12}]
    performance = {
        'matches': 20,
        'wins': 12,
        'topHero': hero_pool[0]['name'],
        'ratingScore': 1200 + player_id,
        'provisional': False,
    }
    return {
        'id': player_id,
        'name': 'Player {}'.format(player_id),
        'initial': 'P',
        'roomLabel': 'Room {}'.format(player_id),
        'roomIds': [player_id],
        'aliases': [],
        'trend': 0,
        'form': ['W', 'L'],
        'modes': {mode: performance for mode in ('all', '3v3', 'brawl', '5v5')},
        'heroPool': hero_pool,
        'heroPools': {mode: hero_pool for mode in ('all', '3v3', 'brawl', '5v5')},
    }


def dashboard_document(*, players: int = 3, days: int = 3) -> Dict[str, Any]:
    player_rows = [_player(player_id) for player_id in range(1, players + 1)]
    hero = {
        'id': 'hero-1',
        'name': 'Hero-1',
        'modes': {
            mode: {'matches': 20, 'wins': 12, 'players': 3}
            for mode in ('all', '3v3', 'brawl', '5v5')
        },
    }
    environment = dict(hero)
    environment['synergies'] = {
        'all': {'best': [{'name': 'Hero-2', 'matches': 10, 'wins': 8}], 'worst': []}
    }
    environment['counters'] = {'all': {'counters': [], 'counteredBy': []}}
    standings = {
        season_id: {
            'players': player_rows,
            'heroes': [hero],
            'environmentHeroes': [environment],
        }
        for season_id in ('all-time', '2026-summer')
    }
    publications = []
    for index in range(days):
        publication_date = date(2026, 1, 1) + timedelta(days=index)
        publications.append(
            {
                'snapshotId': 'snapshot-{}'.format(index),
                'publicationDate': publication_date.isoformat(),
                'sourceLastMatchId': index,
                'standings': {
                    season_id: {
                        mode: [
                            {
                                'playerId': row['id'],
                                'rank': rank,
                                'ratingScore': 1200 + row['id'] + index,
                            }
                            for rank, row in enumerate(player_rows, 1)
                        ]
                        for mode in ('all', '3v3', 'brawl', '5v5')
                    }
                    for season_id in standings
                },
            }
        )
    return {
        'snapshot': {
            'schemaVersion': 1,
            'snapshotId': 'snapshot-current',
            'contentRevision': 'content-1',
            'publicationDate': '2026-08-25',
            'generatedAt': '2026-08-25T00:00:00Z',
            'sourceLastMatchId': 9,
            'sourceMatchCount': 9,
            'ratingModel': {'version': 7},
            'currentSeasonKey': '2026-summer',
            'seasons': [
                {
                    'key': '2026-summer',
                    'label': '2026 Summer',
                    'shortLabel': 'Summer',
                    'period': '2026-06-01—2026-08-31',
                    'current': True,
                }
            ],
            'standings': standings,
            'matches': [],
        },
        'trends': {
            'schemaVersion': 1,
            'updatedAt': '2026-08-25T00:00:00Z',
            'publications': publications,
        },
    }


class StaticRepository:
    def __init__(self, document: Dict[str, Any]) -> None:
        self.document = document

    def refresh(self, *, force: bool = False) -> bool:
        return False

    def dashboard_payload(self, *, owner_view: bool = False) -> tuple[bytes, str]:
        del owner_view
        return (
            json.dumps(self.document, separators=(',', ':')).encode('utf-8'),
            'source-1',
        )


def _settings(tmp_path: Path) -> ApiSettings:
    return ApiSettings(
        database_path=tmp_path / 'api.sqlite3',
        ingest_token_sha256=hashlib.sha256(b'token').hexdigest(),
        cors_origins=('https://vg.luwei.host',),
    )


def test_v2_resources_remove_eager_and_duplicate_fields() -> None:
    source = dashboard_document()
    payload = json.dumps(source, separators=(',', ':')).encode('utf-8')
    document = DashboardResourceDocument(payload, 'source-1')

    summary = json.loads(document.summary.payload)
    standing = json.loads(document.standings('2026-summer').payload)
    environment = json.loads(document.environment('2026-summer').payload)

    assert 'standings' not in summary
    assert 'trends' not in summary
    assert 'environmentHeroes' not in summary
    assert 'environmentHeroes' not in standing
    assert 'heroPool' not in standing['players'][0]
    assert standing['players'][0]['heroPools']['all'][0]['name'] == 'Hero-1'
    assert environment['environmentHeroes'][0]['synergies']


def test_v2_trends_filter_season_mode_players_and_date_range() -> None:
    source = dashboard_document(players=4, days=5)
    payload = json.dumps(source, separators=(',', ':')).encode('utf-8')
    document = DashboardResourceDocument(payload, 'source-1')

    resource = document.trends(
        season_id='2026-summer',
        mode='3v3',
        player_ids=(2,),
        from_date='2026-01-02',
        to_date='2026-01-03',
    )
    result = json.loads(resource.payload)

    assert len(result['publications']) == 2
    for publication in result['publications']:
        assert set(publication['standings']) == {'2026-summer'}
        assert set(publication['standings']['2026-summer']) == {'3v3'}
        assert [
            row['playerId'] for row in publication['standings']['2026-summer']['3v3']
        ] == [2]


def test_v2_resources_have_independent_stable_revisions() -> None:
    first = dashboard_document()
    second = dashboard_document()
    second['snapshot']['publicationDate'] = '2026-08-26'
    cache = DashboardResourceCache()
    first_payload = json.dumps(first, separators=(',', ':')).encode('utf-8')
    second_payload = json.dumps(second, separators=(',', ':')).encode('utf-8')

    assert cache.replace((first_payload, '1'), (first_payload, '1')) == ()
    changes = cache.replace((second_payload, '2'), (second_payload, '2'))

    assert [change['resource'] for change in changes] == ['summary']


def test_v2_etag_returns_304_with_an_empty_body(tmp_path: Path) -> None:
    repository = StaticRepository(dashboard_document())
    client = TestClient(
        create_app(_settings(tmp_path), repository=repository)  # type: ignore[arg-type]
    )

    first = client.get('/v2/standings?seasonId=2026-summer')
    second = client.get(
        '/v2/standings?seasonId=2026-summer',
        headers={'If-None-Match': first.headers['etag']},
    )

    assert first.status_code == 200
    assert second.status_code == 304
    assert second.content == b''


def test_v2_first_view_payload_stays_within_budget() -> None:
    source = dashboard_document(players=300, days=180)
    payload = json.dumps(source, separators=(',', ':')).encode('utf-8')
    document = DashboardResourceDocument(payload, 'source-1')
    trends = document.trends(
        season_id='2026-summer', mode='all', player_ids=(), from_date=None, to_date=None
    )
    resources = (
        document.summary.payload,
        document.standings('2026-summer').payload,
        trends.payload,
    )

    assert sum(len(value) for value in resources) < 2_000_000
    assert (
        sum(len(gzip.compress(value, compresslevel=6)) for value in resources) < 250_000
    )
