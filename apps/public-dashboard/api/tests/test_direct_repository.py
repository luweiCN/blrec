from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Mapping

import pytest
from blrec_dashboard_api import direct as direct_module
from blrec_dashboard_api.app import create_app
from blrec_dashboard_api.database import (
    _migration_text,
    connect_database,
    initialize_database,
)
from blrec_dashboard_api.direct import DirectDashboardRepository
from blrec_dashboard_api.settings import ApiSettings
from blrec_dashboard_publisher.snapshot import build_dashboard_snapshot_from_records
from fastapi.testclient import TestClient

TOKEN = 'test-asset-token'


def _runtime_source(*, match_id: int = 1, result: str = 'W') -> Mapping[str, Any]:
    played_at = 1780272000
    lineups = {
        match_id: [
            {
                'match_id': match_id,
                'side': side,
                'slot': slot,
                'player_name': name,
                'hero_name': hero,
                'kills': 5,
                'deaths': 2,
                'assists': 7,
                'economy': 13500,
                'last_hits': 100,
            }
            for side, values in (
                ('left', (('主播', '剑圣'), ('队友甲', '鱼人'), ('队友乙', '鸟人'))),
                ('right', (('对手甲', '猫女'), ('对手乙', '火龙'), ('对手丙', '女警'))),
            )
            for slot, (name, hero) in enumerate(values, start=1)
        ]
    }
    row: Dict[str, Any] = {
        'match_id': match_id,
        'player_id': 7,
        'game_mode': '3v3',
        'played_at': played_at,
        'duration_seconds': 907,
        'winner_side': 'left' if result == 'W' else 'right',
        'recorded_player_side': 'left',
        'hero_name': '剑圣',
        'kills': 5,
        'deaths': 2,
        'assists': 7,
        'economy': 13500,
        'left_kills': 10,
        'right_kills': 8,
        'left_economy': 40500,
        'right_economy': 33000,
    }
    generated_at = datetime(2026, 8, 11, tzinfo=timezone.utc)
    snapshot = build_dashboard_snapshot_from_records(
        players={7: {'name': '主播', 'rooms': [123456]}},
        aliases={7: ['-Anchor-']},
        rows=[row],
        lineups=lineups,
        public_matches=(),
        generated_at=generated_at,
    )
    teams = []
    for role, side, color in (('ally', 'left', 'teal'), ('enemy', 'right', 'orange')):
        teams.append(
            {
                'role': role,
                'side': side,
                'color': color,
                'kills': 10 if role == 'ally' else 8,
                'economy': 40500 if role == 'ally' else 33000,
                'players': [
                    {
                        'slot': int(player['slot']),
                        'name': str(player['player_name']),
                        'heroName': str(player['hero_name']),
                        'kills': player['kills'],
                        'deaths': player['deaths'],
                        'assists': player['assists'],
                        'economy': player['economy'],
                        'lastHits': player['last_hits'],
                        'isRecordedPlayer': role == 'ally' and player['slot'] == 1,
                    }
                    for player in lineups[match_id]
                    if player['side'] == side
                ],
            }
        )
    return {
        'snapshot': snapshot,
        'players': [
            {
                'id': 7,
                'name': '主播',
                'initial': '主',
                'roomLabel': '直播间 123456',
                'roomIds': [123456],
                'aliases': ['-Anchor-'],
                'avatarUrl': None,
                'liveRooms': [
                    {
                        'roomId': 123456,
                        'title': '今晚三排',
                        'startedAt': '2026-08-11T11:30:00Z',
                    }
                ],
            }
        ],
        'matches': [
            {
                'id': match_id,
                'playerId': 7,
                'seasonKey': '2026-summer',
                'mode': '3v3',
                'playedAt': '2026-06-01T00:00:00Z',
                'durationSeconds': 907,
                'result': result,
                'streamTitle': '主播深夜排位',
                'analysisProvisional': False,
                'ally': teams[0],
                'enemy': teams[1],
                'replay': {
                    'kind': 'match',
                    'url': 'https://www.bilibili.com/video/BV1test?t=120',
                },
            }
        ],
    }


def _repository(tmp_path: Path) -> tuple[DirectDashboardRepository, list[int]]:
    auxiliary = tmp_path / 'public.sqlite3'
    initialize_database(auxiliary)
    revisions = [1]
    loads: list[int] = []

    def revision_loader(_target: Any) -> int:
        return revisions[-1]

    def runtime_loader(_target: Any) -> tuple[int, Mapping[str, Any]]:
        loads.append(revisions[-1])
        return revisions[-1], _runtime_source(match_id=revisions[-1])

    repository = DirectDashboardRepository(
        source_target=tmp_path / 'unused.sqlite3',
        auxiliary_target=auxiliary,
        revision_loader=revision_loader,
        runtime_loader=runtime_loader,
    )
    repository.refresh(force=True)
    return repository, loads


def _settings(tmp_path: Path) -> ApiSettings:
    return ApiSettings(
        database_path=tmp_path / 'public.sqlite3',
        source_database_path=tmp_path / 'unused.sqlite3',
        ingest_token_sha256=hashlib.sha256(TOKEN.encode()).hexdigest(),
        cors_origins=('https://vg.luwei.host',),
    )


def test_repository_rebuilds_only_after_the_source_revision_changes(
    tmp_path: Path,
) -> None:
    repository, loads = _repository(tmp_path)

    assert repository.refresh() is False
    assert loads == [1]
    repository._revision_loader = lambda _target: 2
    repository._runtime_loader = lambda _target: (2, _runtime_source(match_id=2))

    assert repository.refresh() is True
    assert repository.get_match(2, rating_scope='3v3', rating_season=None)['id'] == 2


def test_dashboard_and_match_queries_are_computed_from_the_runtime_source(
    tmp_path: Path,
) -> None:
    repository, _loads = _repository(tmp_path)

    document, revision = repository.dashboard_document()
    listed = repository.list_matches(
        page=1,
        page_size=10,
        season='2026-summer',
        mode='3v3',
        player_id=7,
        query='zhuboshenye',
        heroes=('剑圣', '猫女'),
        rating_scope='3v3',
        rating_season='2026-summer',
    )

    assert revision == '1'
    assert document['snapshot']['sourceMatchCount'] == 1
    assert document['snapshot']['matches'] == []
    assert document['trends']['publications'][0]['standings']
    assert listed['total'] == 1
    assert listed['items'][0]['player']['name'] == '主播'
    assert listed['items'][0]['rating']['scoreDelta'] > 0


def test_current_trend_history_is_stored_as_rows_instead_of_json(
    tmp_path: Path,
) -> None:
    repository, _loads = _repository(tmp_path)
    repository.dashboard_document()
    connection = connect_database(tmp_path / 'public.sqlite3')
    try:
        columns = {
            str(row[1])
            for row in connection.execute(
                'PRAGMA table_info(dashboard_publications)'
            ).fetchall()
        }
        standing_count = int(
            connection.execute(
                'SELECT COUNT(*) FROM dashboard_publication_standings'
            ).fetchone()[0]
        )
    finally:
        connection.close()

    assert 'snapshot_json' not in columns
    assert standing_count > 0


def test_schema_upgrade_converts_legacy_trend_json_to_rows(tmp_path: Path) -> None:
    database_path = tmp_path / 'legacy.sqlite3'
    connection = sqlite3.connect(str(database_path))
    try:
        for version in range(1, 6):
            connection.executescript(
                'BEGIN IMMEDIATE;\n'
                + _migration_text('migrations', version)
                + '\nINSERT INTO schema_migrations(version,applied_at) VALUES('
                + str(version)
                + ','
                + str(int(time.time()))
                + ');\nCOMMIT;'
            )
        connection.execute(
            'INSERT INTO dashboard_trend_publications('
            'publication_date,snapshot_id,generated_at,source_last_match_id,'
            'standings_json,updated_at) VALUES(?,?,?,?,?,?)',
            (
                '2026-08-10',
                'legacy-snapshot',
                '2026-08-10T12:00:00Z',
                99,
                json.dumps(
                    {
                        '2026-summer': {
                            'all': [{'playerId': 7, 'rank': 1, 'ratingScore': 612}],
                            '3v3': [],
                            'brawl': [],
                            '5v5': [],
                        }
                    }
                ),
                int(time.time()),
            ),
        )
        connection.commit()
    finally:
        connection.close()

    initialize_database(database_path)
    upgraded = connect_database(database_path)
    try:
        publication = upgraded.execute(
            'SELECT snapshot_id,source_last_match_id FROM dashboard_publications'
        ).fetchone()
        standing = upgraded.execute(
            'SELECT season_key,mode,player_id,rank,rating_score '
            'FROM dashboard_publication_standings'
        ).fetchone()
    finally:
        upgraded.close()

    assert publication is not None
    assert dict(publication) == {
        'snapshot_id': 'legacy-snapshot',
        'source_last_match_id': 99,
    }
    assert standing is not None
    assert dict(standing) == {
        'season_key': '2026-summer',
        'mode': 'all',
        'player_id': 7,
        'rank': 1,
        'rating_score': 612,
    }


def test_asset_write_is_transactional_and_visible_without_rebuilding_source(
    tmp_path: Path,
) -> None:
    repository, loads = _repository(tmp_path)
    app = create_app(_settings(tmp_path), repository=repository)
    client = TestClient(app)
    payload = {
        'schemaVersion': 1,
        'generatedAt': '2026-08-11T12:00:00Z',
        'images': [
            {
                'matchId': 1,
                'url': 'https://vg.luwei.host/data/match-images/1.webp',
                'width': 1600,
                'height': 900,
                'sha256': 'a' * 64,
            }
        ],
        'removedMatchIds': [],
    }
    headers = {
        'Authorization': 'Bearer {}'.format(TOKEN),
        'X-Idempotency-Key': 'asset-1',
    }

    first = client.post('/v1/assets/batches', headers=headers, json=payload)
    duplicate = client.post('/v1/assets/batches', headers=headers, json=payload)
    detail = client.get('/v1/matches/1').json()

    assert first.status_code == 200
    assert first.json()['status'] == 'applied'
    assert duplicate.status_code == 200
    assert duplicate.json()['status'] == 'duplicate'
    assert detail['resultImage']['width'] == 1600
    assert loads == [1]

    conflicting = dict(payload)
    conflicting['images'] = [{**payload['images'][0], 'sha256': 'b' * 64}]
    response = client.post('/v1/assets/batches', headers=headers, json=conflicting)
    assert response.status_code == 409
    assert client.get('/v1/matches/1').json()['resultImage'] is not None


def test_dashboard_http_response_uses_etag_without_a_persisted_json_snapshot(
    tmp_path: Path,
) -> None:
    repository, _loads = _repository(tmp_path)
    client = TestClient(create_app(_settings(tmp_path), repository=repository))

    first = client.get('/v1/dashboard')
    second = client.get(
        '/v1/dashboard', headers={'If-None-Match': first.headers['etag']}
    )

    assert first.status_code == 200
    assert first.headers['content-type'].startswith('application/json')
    assert second.status_code == 304


def test_repository_keeps_the_last_good_value_when_refresh_fails(
    tmp_path: Path,
) -> None:
    repository, _loads = _repository(tmp_path)
    before = repository.dashboard_document()
    repository._revision_loader = lambda _target: 2

    def fail(_target: Any) -> tuple[int, Mapping[str, Any]]:
        raise RuntimeError('database unavailable')

    repository._runtime_loader = fail

    with pytest.raises(RuntimeError, match='database unavailable'):
        repository.refresh()
    assert repository.dashboard_document() == before


def test_trend_cache_write_failure_does_not_hide_fresh_database_results(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository, _loads = _repository(tmp_path)
    repository._revision_loader = lambda _target: 2
    repository._runtime_loader = lambda _target: (2, _runtime_source(match_id=2))

    def fail_write(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError('write failed')

    monkeypatch.setattr(direct_module, 'persist_dashboard_publication', fail_write)

    assert repository.refresh() is True
    document, revision = repository.dashboard_document()

    assert revision == '2'
    assert document['snapshot']['sourceLastMatchId'] == 2
    assert document['trends']['publications'][-1]['snapshotId'] == (
        document['snapshot']['snapshotId']
    )
