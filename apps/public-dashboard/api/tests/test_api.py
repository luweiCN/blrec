from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path
from typing import Any, Dict, List

from blrec_dashboard_api.app import create_app
from blrec_dashboard_api.settings import ApiSettings
from fastapi.testclient import TestClient

TOKEN = 'test-ingest-token'


def team(
    *,
    role: str,
    color: str,
    hero_names: List[str],
    player_names: List[str],
    recorded_slot: int = -1,
) -> Dict[str, Any]:
    return {
        'role': role,
        'side': 'left' if role == 'ally' else 'right',
        'color': color,
        'kills': 10 if role == 'ally' else 8,
        'economy': 40500 if role == 'ally' else 33000,
        'players': [
            {
                'slot': index + 1,
                'name': player_name,
                'heroName': hero_name,
                'kills': 5 if index == 0 else 2,
                'deaths': 2,
                'assists': 7,
                'economy': 13500,
                'lastHits': 100,
                'isRecordedPlayer': index == recorded_slot,
            }
            for index, (hero_name, player_name) in enumerate(
                zip(hero_names, player_names)
            )
        ],
    }


def match(
    match_id: int, *, played_at: str, result: str, title: str = '茉莉深夜排位'
) -> Dict[str, Any]:
    return {
        'id': match_id,
        'playerId': 7,
        'seasonKey': '2026-summer',
        'mode': '3v3',
        'playedAt': played_at,
        'durationSeconds': 907,
        'result': result,
        'streamTitle': title,
        'ally': team(
            role='ally',
            color='teal',
            hero_names=['剑圣', '鱼人', '鸟人'],
            player_names=['-Akitsuki-', 'Guest', '队友甲'],
            recorded_slot=0,
        ),
        'enemy': team(
            role='enemy',
            color='orange',
            hero_names=['猫女', '火龙', '女警'],
            player_names=['对手甲', '对手乙', '对手丙'],
        ),
        'replay': {
            'kind': 'match',
            'url': 'https://www.bilibili.com/video/BV1test?p=1&t=120',
        },
        'resultImage': {
            'url': 'https://vg.luwei.host/data/match-images/7.webp',
            'width': 1600,
            'height': 900,
        },
    }


def batch(matches: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {
        'schemaVersion': 1,
        'generatedAt': '2026-08-11T00:00:00Z',
        'sourceLastMatchId': max((item['id'] for item in matches), default=0),
        'players': [
            {
                'id': 7,
                'name': '茉莉',
                'initial': '茉',
                'roomLabel': '直播间 123456',
                'roomIds': [123456],
                'aliases': ['-Akitsuki-'],
                'avatarUrl': 'https://example.com/avatar.jpg',
            }
        ],
        'matches': matches,
        'removedMatchIds': [],
    }


def player(player_id: int, name: str) -> Dict[str, Any]:
    return {
        'id': player_id,
        'name': name,
        'initial': name[:1],
        'roomLabel': '直播间 {}'.format(120000 + player_id),
        'roomIds': [120000 + player_id],
        'aliases': [],
        'avatarUrl': None,
    }


def make_client(tmp_path: Path) -> TestClient:
    settings = ApiSettings(
        database_path=tmp_path / 'dashboard.sqlite3',
        ingest_token_sha256=hashlib.sha256(TOKEN.encode()).hexdigest(),
        cors_origins=('https://vg.luwei.host',),
    )
    return TestClient(create_app(settings))


def ingest(client: TestClient, payload: Dict[str, Any], key: str = 'batch-1') -> Any:
    return client.post(
        '/v1/ingest/batches',
        headers={'Authorization': f'Bearer {TOKEN}', 'X-Idempotency-Key': key},
        json=payload,
    )


def test_match_list_waits_for_first_publication(tmp_path: Path) -> None:
    client = make_client(tmp_path)

    response = client.get('/v1/matches')

    assert response.status_code == 503
    assert response.json() == {
        'detail': 'match archive is waiting for its first publication'
    }

    assert ingest(client, batch([])).status_code == 200
    published = client.get('/v1/matches')
    assert published.status_code == 200
    assert published.json()['total'] == 0


def test_dashboard_waits_for_first_publication(tmp_path: Path) -> None:
    client = make_client(tmp_path)

    response = client.get('/v1/dashboard')

    assert response.status_code == 503
    assert response.json() == {
        'detail': 'dashboard is waiting for its first publication'
    }


def test_dashboard_is_materialized_from_server_matches_without_embedding_archive(
    tmp_path: Path,
) -> None:
    client = make_client(tmp_path)
    payload = batch([match(1, played_at='2026-06-01T12:00:00Z', result='W')])

    assert ingest(client, payload).status_code == 200
    response = client.get('/v1/dashboard')

    assert response.status_code == 200
    assert response.headers['cache-control'] == (
        'public, max-age=60, stale-while-revalidate=300'
    )
    assert response.headers['etag'].startswith('"')
    document = response.json()
    snapshot = document['snapshot']
    trends = document['trends']
    assert snapshot['sourceLastMatchId'] == 1
    assert snapshot['sourceMatchCount'] == 1
    assert snapshot['currentSeasonKey'] == '2026-summer'
    assert snapshot['matches'] == []
    summer = snapshot['standings']['2026-summer']
    player = summer['players'][0]
    assert player['id'] == 7
    assert player['modes']['3v3']['matches'] == 1
    assert player['modes']['3v3']['wins'] == 1
    assert player['modes']['3v3']['ratingScore'] is not None
    assert player['modes']['3v3']['ratingForecast'] is not None
    hero = next(value for value in summer['heroes'] if value['id'] == '剑圣')
    assert hero['modes']['3v3'] == {'matches': 1, 'wins': 1, 'players': 1}
    assert trends['publications'][-1]['snapshotId'] == snapshot['snapshotId']
    assert trends['publications'][-1]['publicationDate'] == '2026-08-11'

    not_modified = client.get(
        '/v1/dashboard', headers={'If-None-Match': response.headers['etag']}
    )
    assert not_modified.status_code == 304


def test_dashboard_backfill_replaces_same_day_trend_after_chronological_replay(
    tmp_path: Path,
) -> None:
    client = make_client(tmp_path)
    recent = match(2, played_at='2026-06-02T12:00:00Z', result='W')
    historical = match(1, played_at='2026-06-01T12:00:00Z', result='L')

    assert ingest(client, batch([recent]), 'recent').status_code == 200
    before = client.get('/v1/dashboard').json()
    assert ingest(client, batch([historical]), 'historical').status_code == 200
    after = client.get('/v1/dashboard').json()

    before_player = before['snapshot']['standings']['2026-summer']['players'][0]
    after_player = after['snapshot']['standings']['2026-summer']['players'][0]
    assert after['snapshot']['snapshotId'] != before['snapshot']['snapshotId']
    assert after_player['modes']['3v3']['matches'] == 2
    assert after_player['modes']['3v3']['wins'] == 1
    assert (
        after_player['modes']['3v3']['ratingScore']
        != before_player['modes']['3v3']['ratingScore']
    )
    assert len(after['trends']['publications']) == 1
    assert (
        after['trends']['publications'][0]['snapshotId']
        == after['snapshot']['snapshotId']
    )


def test_match_summary_is_filtered_and_precomputed_on_server(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    payload = batch(
        [
            match(1, played_at='2026-06-01T12:00:00Z', result='W'),
            match(2, played_at='2026-06-02T12:00:00Z', result='L'),
        ]
    )
    assert ingest(client, payload).status_code == 200

    response = client.get('/v1/matches/summary?season=2026-summer&mode=3v3')

    assert response.status_code == 200
    assert response.json() == {
        'matches': 2,
        'wins': 1,
        'players': 1,
        'averageDurationSeconds': 907,
        'replays': 2,
    }


def test_ingest_removes_unreferenced_players_missing_from_source(
    tmp_path: Path,
) -> None:
    client = make_client(tmp_path)
    first_match = match(1, played_at='2026-06-01T12:00:00Z', result='W')
    assert ingest(client, batch([first_match])).status_code == 200

    merged_match = {**first_match, 'playerId': 8}
    merged = batch([merged_match])
    merged['players'] = [
        {
            'id': 8,
            'name': '合并后的玩家',
            'initial': '合',
            'roomLabel': '直播间 123456',
            'roomIds': [123456],
            'aliases': ['茉莉', '-Akitsuki-'],
            'avatarUrl': None,
        }
    ]

    assert ingest(client, merged, key='batch-2').status_code == 200
    connection = sqlite3.connect(tmp_path / 'dashboard.sqlite3')
    try:
        player_ids = [
            int(row[0])
            for row in connection.execute(
                'SELECT player_id FROM players ORDER BY player_id'
            ).fetchall()
        ]
    finally:
        connection.close()

    assert player_ids == [8]
    listed = client.get('/v1/matches').json()
    assert listed['items'][0]['player']['id'] == 8


def test_ingest_requires_authentication_and_is_idempotent(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    payload = batch([match(2, played_at='2026-06-02T12:00:00Z', result='W')])

    unauthorized = client.post(
        '/v1/ingest/batches', headers={'X-Idempotency-Key': 'batch-1'}, json=payload
    )
    first = ingest(client, payload)
    duplicate = ingest(client, payload)
    conflicting_payload = {**payload, 'sourceLastMatchId': 999}
    conflict = ingest(client, conflicting_payload)

    assert unauthorized.status_code == 401
    assert first.status_code == 200
    assert first.json()['status'] == 'applied'
    assert duplicate.status_code == 200
    assert duplicate.json()['status'] == 'duplicate'
    assert conflict.status_code == 409


def test_historical_backfill_replays_exact_rating_ledger(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    recent = match(2, played_at='2026-06-02T12:00:00Z', result='W')
    historical = match(1, played_at='2026-06-01T12:00:00Z', result='L')

    assert ingest(client, batch([recent]), 'recent').status_code == 200
    before_backfill = client.get('/v1/matches/2?ratingScope=3v3').json()
    assert ingest(client, batch([historical]), 'historical').status_code == 200
    after_backfill = client.get('/v1/matches/2?ratingScope=3v3').json()
    first_match = client.get('/v1/matches/1?ratingScope=3v3').json()

    assert before_backfill['rating']['scoreBefore'] == 999
    assert (
        after_backfill['rating']['scoreBefore'] == first_match['rating']['scoreAfter']
    )
    assert after_backfill['rating']['scoreDelta'] > 0
    assert first_match['rating']['scoreDelta'] < 0
    assert (
        after_backfill['rating']['scoreAfter']
        != before_backfill['rating']['scoreAfter']
    )


def test_new_match_replays_only_the_affected_players_rating_history(
    tmp_path: Path,
) -> None:
    client = make_client(tmp_path)
    first = match(1, played_at='2026-06-01T12:00:00Z', result='W')
    second = {**match(2, played_at='2026-06-01T13:00:00Z', result='L'), 'playerId': 8}
    initial = batch([first, second])
    initial['players'] = [player(7, '茉莉'), player(8, '小王子')]
    assert ingest(client, initial, 'initial-two-players').status_code == 200

    database_path = tmp_path / 'dashboard.sqlite3'
    connection = sqlite3.connect(database_path)
    try:
        connection.executescript(
            '''
            CREATE TABLE rating_delete_audit(player_id INTEGER NOT NULL);
            CREATE TRIGGER audit_rating_event_delete
            BEFORE DELETE ON rating_events
            BEGIN
                INSERT INTO rating_delete_audit(player_id) VALUES(OLD.player_id);
            END;
            '''
        )
        connection.commit()
    finally:
        connection.close()

    third = match(3, played_at='2026-06-02T12:00:00Z', result='W')
    update = batch([third])
    update['players'] = [player(7, '茉莉'), player(8, '小王子')]
    response = ingest(client, update, 'player-seven-update')

    assert response.status_code == 200
    connection = sqlite3.connect(database_path)
    try:
        deleted_player_ids = {
            int(row[0])
            for row in connection.execute(
                'SELECT player_id FROM rating_delete_audit'
            ).fetchall()
        }
        player_eight_events = int(
            connection.execute(
                'SELECT COUNT(*) FROM rating_events WHERE player_id=?', (8,)
            ).fetchone()[0]
        )
    finally:
        connection.close()

    assert deleted_player_ids == {7}
    assert player_eight_events > 0


def test_match_search_is_segmented_and_supports_pinyin_title_and_players(
    tmp_path: Path,
) -> None:
    client = make_client(tmp_path)
    payload = batch(
        [
            match(1, played_at='2026-06-01T12:00:00Z', result='W'),
            match(2, played_at='2026-06-02T12:00:00Z', result='L', title='午后练习'),
        ]
    )
    assert ingest(client, payload).status_code == 200

    assert client.get('/v1/matches?q=molishenye').json()['total'] == 1
    assert client.get('/v1/matches?q=mlsyp').json()['total'] == 1
    assert client.get('/v1/matches?q=akitsuki').json()['total'] == 2
    assert client.get('/v1/matches?q=茉莉akitsuki').json()['total'] == 0
    assert client.get('/v1/matches?heroes=剑圣,猫女').json()['total'] == 2
    assert client.get('/v1/matches?heroes=剑圣,黑羽').json()['total'] == 0


def test_match_response_separates_teams_replay_and_result_image(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    assert (
        ingest(
            client, batch([match(1, played_at='2026-06-01T12:00:00Z', result='W')])
        ).status_code
        == 200
    )

    response = client.get('/v1/matches?page=1&pageSize=10&mode=3v3').json()
    item = response['items'][0]

    assert response['total'] == 1
    assert item['ally']['color'] == 'teal'
    assert item['ally']['kills'] == 10
    assert item['enemy']['color'] == 'orange'
    assert item['enemy']['economy'] == 33000
    assert item['ally']['players'][0]['isRecordedPlayer'] is True
    assert item['streamTitle'] == '茉莉深夜排位'
    assert item['replay']['kind'] == 'match'
    assert item['resultImage']['width'] == 1600


def test_ingest_keeps_partially_recognized_lineups(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    partial = match(1, played_at='2026-06-01T12:00:00Z', result='W')
    partial['ally']['players'][1]['heroName'] = ''
    partial['enemy']['players'] = partial['enemy']['players'][:2]

    response = ingest(client, batch([partial]))

    assert response.status_code == 200
    detail = client.get('/v1/matches/1').json()
    assert detail['ally']['players'][1]['heroName'] == ''
    assert len(detail['enemy']['players']) == 2


def test_schema_uses_foreign_keys_and_player_match_index(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    client.get('/v1/health')
    connection = sqlite3.connect(tmp_path / 'dashboard.sqlite3')
    try:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        indexes = {
            row[1]
            for row in connection.execute("PRAGMA index_list('matches')").fetchall()
        }
        plan = connection.execute(
            'EXPLAIN QUERY PLAN SELECT source_match_id FROM matches '
            'WHERE player_id=? ORDER BY played_at_epoch DESC,source_match_id DESC '
            'LIMIT ?',
            (7, 10),
        ).fetchall()
    finally:
        connection.close()

    assert 'dashboard_state' in tables
    assert 'dashboard_trend_publications' in tables
    assert 'matches_player_played_idx' in indexes
    assert any('matches_player_played_idx' in str(row) for row in plan)
